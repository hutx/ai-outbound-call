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
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi import status as http_status
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, field_validator

from backend.core.config import config
from backend.core.call_agent import CallAgent
from backend.core.scheduler import TaskScheduler, OutboundTask
from backend.core.state_machine import CallContext, CallResult
from backend.services.esl_service import AsyncESLPool, ESLSocketServer, ESLSocketCallSession
from backend.services.asr_service import create_asr_client
from backend.services.tts_service import create_tts_client
from backend.services.llm_service import LLMService
from backend.services.crm_service import crm
from backend.utils.db import (
    init_db, dispose_db, list_call_records, get_call_stats,
    db_add_blacklist, db_remove_blacklist, db_list_blacklist,
    db_list_callbacks,
)

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
_asr = None
_tts = None
_llm = None
_scheduler: Optional[TaskScheduler] = None

# 活跃通话 uuid → CallAgent
_active_calls: dict[str, CallAgent] = {}
# WebSocket 监控客户端
_ws_clients: list[WebSocket] = []

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
_API_TOKEN = os.environ.get("API_TOKEN", "")
_bearer = HTTPBearer(auto_error=False)


def require_auth(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Bearer Token 验证；若未配置 API_TOKEN 则跳过验证（开发模式）"""
    if not _API_TOKEN:
        return  # 未设置 token = 开放模式（仅供开发）
    if not creds or creds.credentials != _API_TOKEN:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ─────────────────────────────────────────────────────────────
# ESL Outbound — 每路通话的入口
# ─────────────────────────────────────────────────────────────
async def _handle_call_session(session: ESLSocketCallSession):
    """FreeSWITCH 接通后主动连入，每路通话走这里"""
    # 从 channel 变量构造 context（task_id 等由 originate 时注入）
    call_uuid = str(uuid.uuid4())
    ctx = CallContext(
        uuid=call_uuid,
        task_id="unknown",          # connect() 后从 channel_vars 覆盖
        phone_number="unknown",
        script_id="finance_product_a",
    )

    agent = CallAgent(
        session=session,
        context=ctx,
        asr=_asr,
        tts=_tts,
        llm=_llm,
    )

    _active_calls[call_uuid] = agent
    _metrics["calls_total"] += 1
    _metrics["calls_active"] += 1
    await _broadcast_stats()

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
            _scheduler.on_call_finished(ctx.task_id, ctx.phone_number, ctx.result)

        await _broadcast_stats()


# ─────────────────────────────────────────────────────────────
# 生命周期
# ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _esl_pool, _asr, _tts, _llm, _scheduler

    logger.info("━" * 50)
    logger.info("  智能外呼系统启动")
    logger.info("━" * 50)

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

    # ESL 连接池
    _esl_pool = AsyncESLPool(
        host=config.freeswitch.host,
        port=config.freeswitch.port,
        password=config.freeswitch.password,
        pool_size=5,
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

    if _esl_pool:
        await _esl_pool.stop()

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


# ─────────────────────────────────────────────────────────────
# Pydantic 模型
# ─────────────────────────────────────────────────────────────
class CreateTaskRequest(BaseModel):
    name: str
    phone_numbers: list[str]
    script_id: str = "finance_product_a"
    concurrent_limit: int = 5
    max_retries: int = 1
    caller_id: str = ""

    @field_validator("phone_numbers")
    @classmethod
    def validate_phones(cls, v):
        import re
        valid = [p.strip() for p in v if re.match(r"^1[3-9]\d{9}$", p.strip())]
        if not valid:
            raise ValueError("至少需要一个有效的手机号码（11位，1开头）")
        return valid

    @field_validator("concurrent_limit")
    @classmethod
    def validate_concurrent(cls, v):
        return max(1, min(v, config.max_concurrent_calls))


class BlacklistRequest(BaseModel):
    phone: str
    reason: str = "手动添加"


# ─────────────────────────────────────────────────────────────
# 任务管理 API
# ─────────────────────────────────────────────────────────────
@app.post("/api/tasks", dependencies=[Depends(require_auth)])
async def create_task(req: CreateTaskRequest):
    """创建并启动外呼任务"""
    if not _scheduler:
        raise HTTPException(503, "调度器未初始化")

    task = _scheduler.create_task(
        name=req.name,
        phone_numbers=req.phone_numbers,
        script_id=req.script_id,
        concurrent_limit=req.concurrent_limit,
        max_retries=req.max_retries,
    )
    # 覆盖 caller_id
    if req.caller_id:
        for pr in task.phones:
            pr.__dict__.setdefault("caller_id", req.caller_id)

    started = await _scheduler.start_task(task.task_id)
    _metrics["tasks_created"] += 1

    logger.info(f"创建任务 {task.task_id}: {req.name} ({len(req.phone_numbers)} 个号码)")
    return {
        "task_id": task.task_id,
        "name": task.name,
        "status": task.status.name,
        "total": task.total,
        "message": f"已创建，共 {task.total} 个号码",
    }


@app.get("/api/tasks", dependencies=[Depends(require_auth)])
async def list_tasks():
    if not _scheduler:
        return {"tasks": []}
    return {"tasks": _scheduler.list_tasks()}


@app.get("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def get_task(task_id: str):
    if not _scheduler:
        raise HTTPException(503, "调度器未初始化")
    task = _scheduler.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task.to_dict()


@app.post("/api/tasks/{task_id}/pause", dependencies=[Depends(require_auth)])
async def pause_task(task_id: str):
    if not _scheduler or not _scheduler.pause_task(task_id):
        raise HTTPException(404, "任务不存在或无法暂停")
    return {"message": "已暂停"}


@app.post("/api/tasks/{task_id}/resume", dependencies=[Depends(require_auth)])
async def resume_task(task_id: str):
    if not _scheduler or not _scheduler.resume_task(task_id):
        raise HTTPException(404, "任务不存在或无法恢复")
    return {"message": "已恢复"}


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def cancel_task(task_id: str):
    if not _scheduler or not _scheduler.cancel_task(task_id):
        raise HTTPException(404, "任务不存在")
    return {"message": "已取消"}


# ─────────────────────────────────────────────────────────────
# 通话记录 API
# ─────────────────────────────────────────────────────────────
@app.get("/api/calls", dependencies=[Depends(require_auth)])
async def list_calls(
    task_id: Optional[str] = None,
    phone: Optional[str] = None,
    result: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    try:
        records = await list_call_records(
            task_id=task_id, phone=phone, result=result,
            limit=limit, offset=offset,
        )
        return {"records": records, "total": len(records)}
    except Exception as e:
        logger.error(f"查询通话记录失败: {e}")
        return {"records": [], "total": 0}


@app.get("/api/calls/stats", dependencies=[Depends(require_auth)])
async def call_stats(task_id: Optional[str] = None):
    """聚合统计：接通率、意向率、平均时长"""
    try:
        return await get_call_stats(task_id=task_id)
    except Exception as e:
        logger.error(f"统计查询失败: {e}")
        return {"total": 0, "connected": 0, "intent": 0,
                "connect_rate": 0.0, "intent_rate": 0.0}


# ─────────────────────────────────────────────────────────────
# 黑名单管理 API
# ─────────────────────────────────────────────────────────────
@app.get("/api/blacklist", dependencies=[Depends(require_auth)])
async def list_blacklist():
    """查看所有黑名单号码"""
    try:
        items = await crm.list_blacklist()
        return {"items": items, "total": len(items)}
    except Exception as e:
        return {"items": [], "total": 0}


@app.post("/api/blacklist", dependencies=[Depends(require_auth)])
async def add_blacklist(req: BlacklistRequest):
    await crm.add_to_blacklist(req.phone, req.reason)
    return {"message": f"{req.phone} 已加入黑名单"}


@app.delete("/api/blacklist/{phone}", dependencies=[Depends(require_auth)])
async def remove_blacklist(phone: str):
    await crm.remove_from_blacklist(phone)
    return {"message": f"{phone} 已从黑名单移除"}


# ─────────────────────────────────────────────────────────────
# 话术脚本管理 API
# ─────────────────────────────────────────────────────────────
@app.get("/api/scripts", dependencies=[Depends(require_auth)])
async def list_scripts():
    """列出所有话术模板"""
    from backend.services.llm_service import get_scripts
    scripts = get_scripts()
    return {
        "scripts": [
            {
                "id":           sid,
                "product_name": s.get("product_name", sid),
                "product_desc": s.get("product_desc", ""),
                "target":       s.get("target_customer", ""),
                "points_count": len(s.get("key_selling_points", [])),
            }
            for sid, s in scripts.items()
        ]
    }


# ─────────────────────────────────────────────────────────────
# 回拨计划 API
# ─────────────────────────────────────────────────────────────
@app.get("/api/callbacks", dependencies=[Depends(require_auth)])
async def list_callbacks():
    """查看待回拨名单"""
    try:
        items = await db_list_callbacks(status="pending")
        return {"items": items, "total": len(items)}
    except Exception as e:
        return {"items": [], "total": 0}


# ─────────────────────────────────────────────────────────────
# 统计 + 健康检查
# ─────────────────────────────────────────────────────────────
@app.get("/api/stats")
async def get_stats():
    tasks = _scheduler.list_tasks() if _scheduler else []
    return {
        "active_calls":      _metrics["calls_active"],
        "calls_total":       _metrics["calls_total"],
        "calls_completed":   _metrics["calls_completed"],
        "calls_transferred": _metrics["calls_transferred"],
        "calls_error":       _metrics["calls_error"],
        "active_tasks":      sum(1 for t in tasks if t["status"] == "RUNNING"),
        "total_tasks":       len(tasks),
        "max_concurrent":    config.max_concurrent_calls,
        "uptime_seconds":    int(time.time() - _start_time),
        "tasks":             tasks[:20],
    }


@app.get("/health")
async def health():
    esl_ok = bool(
        _esl_pool is not None
        and _esl_pool._conns
        and any(c.is_connected for c in _esl_pool._conns)
    )
    return {
        "status":       "ok" if esl_ok else "degraded",
        "esl_pool":     esl_ok,
        "active_calls": _metrics["calls_active"],
        "timestamp":    datetime.now().isoformat(),
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Prometheus 文本格式指标"""
    tasks = _scheduler.list_tasks() if _scheduler else []
    active_tasks = sum(1 for t in tasks if t["status"] == "RUNNING")
    uptime = int(time.time() - _start_time)
    lines = [
        "# HELP outbound_calls_total Total outbound calls initiated",
        "# TYPE outbound_calls_total counter",
        f'outbound_calls_total {_metrics["calls_total"]}',
        "# HELP outbound_calls_active Currently active calls",
        "# TYPE outbound_calls_active gauge",
        f'outbound_calls_active {_metrics["calls_active"]}',
        "# HELP outbound_calls_completed_total Completed calls",
        "# TYPE outbound_calls_completed_total counter",
        f'outbound_calls_completed_total {_metrics["calls_completed"]}',
        "# HELP outbound_calls_transferred_total Transferred to human calls",
        "# TYPE outbound_calls_transferred_total counter",
        f'outbound_calls_transferred_total {_metrics["calls_transferred"]}',
        "# HELP outbound_calls_error_total Error calls",
        "# TYPE outbound_calls_error_total counter",
        f'outbound_calls_error_total {_metrics["calls_error"]}',
        "# HELP outbound_tasks_active Active running tasks",
        "# TYPE outbound_tasks_active gauge",
        f'outbound_tasks_active {active_tasks}',
        "# HELP outbound_tasks_total Total tasks created",
        "# TYPE outbound_tasks_total counter",
        f'outbound_tasks_total {_metrics["tasks_created"]}',
        "# HELP outbound_uptime_seconds Service uptime in seconds",
        "# TYPE outbound_uptime_seconds gauge",
        f'outbound_uptime_seconds {uptime}',
        "",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# WebSocket 实时监控
# ─────────────────────────────────────────────────────────────
@app.websocket("/ws/monitor")
async def monitor_ws(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    if _API_TOKEN and token != _API_TOKEN:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    _ws_clients.append(websocket)
    logger.debug(f"监控 WS 连接，当前 {len(_ws_clients)} 个客户端")

    try:
        await websocket.send_json(await _build_monitor_data())
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if msg == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                await websocket.send_text("ping")
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


async def _broadcast_stats():
    if not _ws_clients:
        return
    data = await _build_monitor_data()
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


async def _build_monitor_data() -> dict:
    tasks = _scheduler.list_tasks() if _scheduler else []
    return {
        "type":         "stats",
        "ts":           datetime.now().isoformat(),
        "active_calls": _metrics["calls_active"],
        "calls_total":  _metrics["calls_total"],
        "active_tasks": sum(1 for t in tasks if t["status"] == "RUNNING"),
        "tasks":        tasks[:10],
    }


# ─────────────────────────────────────────────────────────────
# 前端静态文件
# ─────────────────────────────────────────────────────────────
@app.get("/")
async def serve_console():
    path = "frontend/index.html"
    if os.path.exists(path):
        return FileResponse(path)
    return {"message": "Frontend not found"}
