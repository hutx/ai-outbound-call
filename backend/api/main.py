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
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse

from backend.api.monitor_api import MonitorAPI
from backend.api.operations_api import OperationsAPI
from backend.core.config import config
from backend.core.scheduler import TaskScheduler
from backend.services.esl_service import AsyncESLPool, ESLEventListener
from backend.services.asr_service import create_asr_client
from backend.services.forkzstream_ws import ForkzstreamWebSocketServer
from backend.services.tts_service import create_tts_client
from backend.services.llm_service import LLMService
from backend.services.crm_service import crm
from backend.api.scripts_api import router as scripts_router
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
_forkzstream_ws_server: Optional[ForkzstreamWebSocketServer] = None
_asr = None
_tts = None
_llm = None
_scheduler: Optional[TaskScheduler] = None

# 活跃通话 uuid → CallAgent（注：CallAgent 现由 ForkzstreamWebSocketServer 内部管理）
_active_calls: dict = {}

# Prometheus-style 计数器
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
# 生命周期
# ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _esl_pool, _esl_listener, _forkzstream_ws_server, _asr, _tts, _llm, _scheduler

    # ★ 显式配置日志
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

    # CRM 黑名单预热
    await crm.startup()
    logger.info("✓ CRM 服务就绪")

    # 服务单例
    _asr = create_asr_client()
    _tts = create_tts_client()
    _llm = LLMService()
    logger.info("✓ ASR / TTS / LLM 服务初始化完成")

    # forkzstream WebSocket Server（接收 FreeSWITCH forkzstream 实时音频流，内置 CallAgent）
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

    # ESL 事件监听器（持久订阅 CHANNEL_ANSWER 等事件，用于 originate 结果确认）
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

    # ESL 连接池（Inbound，用于 originate / API 调用）
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

    # 将 ESL pool 引用设置给 forkzstream server（用于 CallAgent 通话控制）
    if _forkzstream_ws_server:
        _forkzstream_ws_server._esl_pool = _esl_pool

    # 任务调度器
    _scheduler = TaskScheduler(esl_pool=_esl_pool)

    # SIGTERM 优雅退出
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_graceful_shutdown()))

    logger.info("  系统就绪，等待外呼任务")
    logger.info("━" * 50)

    yield

    # ── 关闭流程 ──────────────────────────────────────────────
    logger.info("开始优雅关闭...")

    if _active_calls:
        logger.info(f"等待 {len(_active_calls)} 路活跃通话结束...")
        await asyncio.wait_for(
            asyncio.gather(*[a.session.hangup() for a in _active_calls.values()],
                           return_exceptions=True),
            timeout=10.0,
        )

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
    docs_url="/docs" if os.environ.get("DEBUG") else None,
    redoc_url=None,
)

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
