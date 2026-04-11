"""
FastAPI 主入口 — 生产级
────────────────────────
REST API   : 任务管理、通话记录、黑名单
WebSocket  : /ws/monitor 实时推送
Metrics    : /metrics  Prometheus 格式
Auth       : Bearer Token（API_TOKEN 环境变量）
Graceful   : SIGTERM → 等待活跃通话完成 → 关闭连接池
"""
import asyncio
import logging
import os
import signal
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
# from dotenv import load_dotenv
# load_dotenv()

from backend.api.monitor_api import MonitorAPI
from backend.api.operations_api import OperationsAPI
from backend.core.config import config
from backend.core.call_agent import CallAgent
from backend.core.scheduler import TaskScheduler
from backend.core.state_machine import CallContext, CallResult
from backend.services.esl_service import AsyncESLPool, ESLSocketServer, ESLSocketCallSession, ESLEventListener
from backend.services.asr_service import create_asr_client
from backend.services.audio_stream_ws import AudioStreamWebSocket
from backend.services.tts_service import create_tts_client
from backend.services.llm_service import LLMService
from backend.services.crm_service import crm
from backend.api.scripts_api import router as scripts_router  # 新增：话术脚本API路由
from backend.utils.db import init_db, dispose_db


logging.basicConfig(
    level=logging.DEBUG if config.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 全局单例
# ─────────────────────────────────────────────────────────────
_esl_pool: Optional[AsyncESLPool] = None
_esl_listener: Optional[ESLEventListener] = None
_ws_server: Optional[AudioStreamWebSocket] = None
_asr = None
_tts = None
_llm = None
_scheduler: Optional[TaskScheduler] = None

# 活跃通话 uuid → CallAgent
_active_calls: dict[str, CallAgent] = {}

# Prometheus-style 计数器（轻量，不依赖 prometheus_client）
_metrics = {
    "calls_total": 0,
    "calls_active": 0,
    "calls_completed": 0,
    "calls_transferred": 0,
    "calls_error": 0,
    "tasks_created": 0,
    "tts_errors": 0,
    "asr_errors": 0,
    "llm_errors": 0,
}
_start_time = time.time()

# ─────────────────────────────────────────────────────────────
# API Token 鉴权
# ─────────────────────────────────────────────────────────────
# _API_TOKEN = os.environ.get("API_TOKEN", "")


# ─────────────────────────────────────────────────────────────
# ESL Outbound — 每路通话的入口
# ─────────────────────────────────────────────────────────────
async def _handle_call_session(session: ESLSocketCallSession):
    """FreeSWITCH 接通后主动连入，每路通话走这里"""
    # 完成握手，获取 channel 变量
    try:
        await session.connect()
    except Exception as e:
        logger.error(f"ESL 握手失败: {e}")
        return

    # 优先使用 originate 时注入的 origination_uuid
    call_uuid = (
        session.channel_vars.get("origination_uuid")
        or session.channel_vars.get("orig_uuid")
        or str(uuid.uuid4())
    )
    task_id = session.channel_vars.get("task_id", "unknown")
    script_id = session.channel_vars.get("script_id", "finance_product_a")
    phone_number = session.channel_vars.get("caller_id_number", "unknown")

    ctx = CallContext(
        uuid=call_uuid,
        task_id=task_id,
        phone_number=phone_number,
        script_id=script_id,
    )

    agent = CallAgent(
        session=session,
        context=ctx,
        asr=_asr,
        tts=_tts,
        llm=_llm,
        esl_pool=_esl_pool,
    )

    _active_calls[call_uuid] = agent
    _metrics["calls_total"] += 1
    _metrics["calls_active"] += 1
    await _monitor_api.broadcast_stats()

    try:
        await agent.run()
    finally:
        _active_calls.pop(call_uuid, None)
        _metrics["calls_active"] -= 1

        # 更新结果计数
        if ctx.result == CallResult.COMPLETED:
            _metrics["calls_completed"] += 1
        elif ctx.result == CallResult.TRANSFERRED:
            _metrics["calls_transferred"] += 1
        elif ctx.result == CallResult.ERROR:
            _metrics["calls_error"] += 1

        # 通知任务调度器该号码已完成
        if _scheduler and ctx.task_id != "unknown":
            _scheduler.on_call_finished(
                ctx.task_id, ctx.phone_number, ctx.result,
                dial_attempts=ctx.dial_attempts,
                hangup_cause=ctx.hangup_cause,
                sip_code=ctx.sip_code,
            )

        await _monitor_api.broadcast_stats()


# ─────────────────────────────────────────────────────────────
# 生命周期
# ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _esl_pool, _esl_listener, _ws_server, _asr, _tts, _llm, _scheduler

    logger.info("━" * 50)
    logger.info("  智能外呼系统启动")
    logger.info("━" * 50)

    config.validate_runtime()

    # 数据库
    try:
        await init_db()
        logger.info("✓ 数据库初始化完成")
    except Exception as e:
        logger.warning(f"✗ 数据库连接失败（将在写入时重试）: {e}")

    # CRM 黑名单预热（从 DB 加载到内存）
    await crm.startup()
    logger.info("✓ CRM 服务就绪")

    # 服务单例
    _asr = create_asr_client()
    _tts = create_tts_client()
    _llm = LLMService()
    logger.info("✓ ASR / TTS / LLM 服务初始化完成")

    # mod_audio_stream WebSocket Server（接收 FreeSWITCH 实时音频流）
    _ws_server = AudioStreamWebSocket(
        host="0.0.0.0",
        port=config.freeswitch.audio_stream_port,
    )
    try:
        await _ws_server.start()
        logger.info(f"✓ AudioStream WebSocket 监听 :{config.freeswitch.audio_stream_port}")
    except Exception as e:
        logger.warning(f"✗ AudioStream WebSocket 启动失败（将降级到文件轮询）: {e}")
        _ws_server = None

    # ESL 事件监听器（持久订阅 CHANNEL_ANSWER 等事件）
    _esl_listener = ESLEventListener(
        host=config.freeswitch.host,
        port=config.freeswitch.port,
        password=config.freeswitch.password,
    )
    try:
        await _esl_listener.start()
        logger.info("✓ ESL 事件监听器就绪")
    except Exception as e:
        logger.warning(f"✗ ESL 事件监听器启动失败（将降级轮询）: {e}")
        _esl_listener = None

    # ESL 连接池（传入事件监听器用于应答检测）
    _esl_pool = AsyncESLPool(
        host=config.freeswitch.host,
        port=config.freeswitch.port,
        password=config.freeswitch.password,
        pool_size=5,
        event_listener=_esl_listener,
    )
    try:
        await _esl_pool.start()
        logger.info("✓ ESL 连接池就绪")
    except Exception as e:
        logger.warning(f"✗ ESL 连接池启动失败（FreeSWITCH 未运行？）: {e}")

    # 任务调度器
    _scheduler = TaskScheduler(esl_pool=_esl_pool)

    # ESL Socket Server（接受 FS 主动连入）
    esl_server = ESLSocketServer(
        host="0.0.0.0",
        port=config.freeswitch.socket_port,
        call_handler=_handle_call_session,
        max_connections=config.max_concurrent_calls + 50,
        esl_pool=_esl_pool,
        ws_server=_ws_server,
    )
    server_task = asyncio.create_task(esl_server.start())
    logger.info(f"✓ ESL Socket Server 监听 :{config.freeswitch.socket_port}")

    # TTS 缓存后台清理
    from backend.utils.tts_cache import start_cache_cleaner
    cache_cleaner_task = asyncio.create_task(start_cache_cleaner())
    logger.info("✓ TTS 缓存清理器已启动")

    # SIGTERM 优雅退出
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_graceful_shutdown()))

    logger.info("  系统就绪，等待外呼任务")
    logger.info("━" * 50)

    yield

    # ── 关闭流程 ──────────────────────────────────────────────
    logger.info("开始优雅关闭...")
    server_task.cancel()
    cache_cleaner_task.cancel()
    await asyncio.gather(server_task, cache_cleaner_task, return_exceptions=True)

    if _active_calls:
        logger.info(f"等待 {len(_active_calls)} 路活跃通话结束...")
        await asyncio.wait_for(
            asyncio.gather(*[a.session.hangup() for a in _active_calls.values()],
                           return_exceptions=True),
            timeout=10.0,
        )

    if _ws_server:
        await _ws_server.stop()

    if _esl_pool:
        await _esl_pool.stop()

    if _esl_listener:
        await _esl_listener.stop()

    await dispose_db()
    logger.info("服务已关闭")


async def _graceful_shutdown():
    logger.info("收到 SIGTERM，准备优雅退出")
    for agent in list(_active_calls.values()):
        try:
            await agent.session.hangup("MANAGER_REQUEST")
        except Exception:
            pass


app = FastAPI(
    title="智能外呼平台",
    description="FreeSWITCH + Claude LLM 智能外呼系统",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if os.environ.get("DEBUG") else None,  # 生产关闭 Swagger
    redoc_url=None,
)

# 新增：话术脚本路由
app.include_router(scripts_router)
_monitor_api = MonitorAPI(
    config=config,
    metrics=_metrics,
    scheduler_getter=lambda: _scheduler,
    esl_pool_getter=lambda: _esl_pool,
    start_time=_start_time,
)
_operations_api = OperationsAPI(
    scheduler_getter=lambda: _scheduler,
    metrics=_metrics,
)
app.include_router(_monitor_api.router)
app.include_router(_operations_api.router)


# ─────────────────────────────────────────────────────────────
# 前端静态文件
# ─────────────────────────────────────────────────────────────
@app.get("/")
async def serve_console():
    path = "frontend/index.html"
    if os.path.exists(path):
        return FileResponse(path)
    return {"message": "Frontend not found"}
