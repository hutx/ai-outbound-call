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
from backend.services.forkzstream_ws import ForkzstreamWebSocketServer
from backend.services.tts_service import create_tts_client
from backend.services.llm_service import LLMService
from backend.services.crm_service import crm
from backend.api.scripts_api import router as scripts_router  # 新增：话术脚本API路由
from backend.utils.db import init_db, dispose_db


logging.basicConfig(
    level=logging.DEBUG if config.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 全局单例
# ─────────────────────────────────────────────────────────────
_esl_pool: Optional[AsyncESLPool] = None
_esl_listener: Optional[ESLEventListener] = None
_ws_server: Optional[AudioStreamWebSocket] = None
_forkzstream_ws_server: Optional[ForkzstreamWebSocketServer] = None
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
    # 调试：打印所有 channel_vars 的 key
    logger.debug(f"[{call_uuid}] channel_vars keys: {sorted(session.channel_vars.keys())}")

    # 优先使用 forkzstream 握手帧中的 botid（话术 ID）
    if _forkzstream_ws_server:
        forkz_script_id = await _forkzstream_ws_server.get_script_id(call_uuid, timeout=3.0)
        if forkz_script_id:
            logger.info(f"[{call_uuid}] forkzstream botid={forkz_script_id}，覆盖 channel_vars 中的 script_id")
            script_id = forkz_script_id

    phone_number = (
        session.channel_vars.get("callee_number", "")
        or session.channel_vars.get("export_callee_number", "")
        or session.channel_vars.get("origination_caller_id_number", "")
        or session.channel_vars.get("caller_id_number", "")
        or "unknown"
    )
    logger.info(f"[{call_uuid}] phone_number={phone_number}, task_id={task_id}")

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
    global _esl_pool, _esl_listener, _ws_server, _forkzstream_ws_server, _asr, _tts, _llm, _scheduler

    # ★ 显式配置日志（uvicorn 的 --log-level 不会配置根日志器）
    _log_level = logging.DEBUG if config.debug else logging.INFO
    _root = logging.getLogger()
    _root.setLevel(_log_level)
    if not _root.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        _root.addHandler(_h)
    logging.getLogger("backend").setLevel(_log_level)

    print("━" * 50, flush=True)
    print("  智能外呼系统启动", flush=True)
    print("━" * 50, flush=True)
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

    # mod_forkzstream WebSocket Server（接收 FreeSWITCH forkzstream 实时音频流）
    _forkzstream_ws_server = ForkzstreamWebSocketServer(
        host="0.0.0.0",
        port=config.freeswitch.forkzstream_port,
    )
    try:
        await _forkzstream_ws_server.start()
        logger.info(f"✓ Forkzstream WebSocket 监听 :{config.freeswitch.forkzstream_port}")
    except Exception as e:
        logger.warning(f"✗ Forkzstream WebSocket 启动失败: {e}")
        _forkzstream_ws_server = None

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

    # ESL 连接池（传入事件监听器用于应答检测 + ws_server 用于音频流）
    _esl_pool = AsyncESLPool(
        host=config.freeswitch.host,
        port=config.freeswitch.port,
        password=config.freeswitch.password,
        pool_size=5,
        event_listener=_esl_listener,
        ws_server=_ws_server,
        forkzstream_ws_server=_forkzstream_ws_server,
    )
    try:
        await _esl_pool.start()
        logger.info("✓ ESL 连接池就绪")
    except Exception as e:
        logger.warning(f"✗ ESL 连接池启动失败（FreeSWITCH 未运行？）: {e}")

    # 将 ESL pool 引用设置给 forkzstream server（用于 CallAgent 通话控制）
    if _forkzstream_ws_server:
        _forkzstream_ws_server._esl_pool = _esl_pool

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
        forkzstream_ws_server=_forkzstream_ws_server,
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

    if _forkzstream_ws_server:
        await _forkzstream_ws_server.stop()

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
