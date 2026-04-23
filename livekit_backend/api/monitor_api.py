"""监控 API + WebSocket 实时推送

提供系统健康检查、活跃通话查询、实时统计，以及 WebSocket 推送。
统一响应格式: {"code": 0, "data": ..., "message": "ok"}
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from livekit_backend.core.config import settings
from livekit_backend.utils import db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["monitor"])

# ── 应用启动时间戳 ───────────────────────────────────────────────────
_start_time: float = time.time()


# ── WebSocket 连接管理器 ──────────────────────────────────────────────

class ConnectionManager:
    """WebSocket 连接管理"""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info("WebSocket 客户端已连接, 当前连接数: %d", len(self.active_connections))

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info("WebSocket 客户端已断开, 当前连接数: %d", len(self.active_connections))

    async def broadcast(self, message: dict):
        """广播消息到所有连接"""
        dead: list[WebSocket] = []
        for conn in self.active_connections:
            try:
                await conn.send_json(message)
            except Exception:
                dead.append(conn)
        for conn in dead:
            if conn in self.active_connections:
                self.active_connections.remove(conn)


manager = ConnectionManager()


# ── 统一响应 ──────────────────────────────────────────────────────────

def _ok(data=None, message: str = "ok") -> dict:
    return {"code": 0, "data": data, "message": message}


# ── 活跃通话状态集合 ─────────────────────────────────────────────────

ACTIVE_STATUSES = (
    "initiating",
    "ringing",
    "connected",
    "ai_speaking",
    "user_speaking",
    "processing",
)


# ── 路由 ──────────────────────────────────────────────────────────────

@router.get("/api/monitor/active-calls", summary="当前活跃通话列表")
async def get_active_calls():
    """查询 lk_call_records 表中状态为活跃的通话列表。

    活跃状态包括: initiating, ringing, connected,
    ai_speaking, user_speaking, processing
    """
    placeholders = ", ".join(f"${i}" for i in range(1, len(ACTIVE_STATUSES) + 1))
    rows = await db.fetch(
        f"""
        SELECT id, call_id, task_id, phone, script_id, status,
               duration_sec, rounds, started_at, answered_at
        FROM lk_call_records
        WHERE status IN ({placeholders})
        ORDER BY created_at DESC
        """,
        *ACTIVE_STATUSES,
    )
    calls = [dict(r) for r in rows]
    # 将 datetime 转为 ISO 格式字符串以便 JSON 序列化
    for call in calls:
        for key in ("started_at", "answered_at"):
            val = call.get(key)
            if val and isinstance(val, datetime):
                call[key] = val.isoformat()
    return _ok(calls)


@router.get("/api/monitor/stats", summary="系统统计")
async def get_stats():
    """系统统计数据。

    返回:
        active_calls: 当前活跃通话数
        today_total: 今日总通话数
        today_completed: 今日完成通话数
        today_success_rate: 今日成功率（百分比）
        running_tasks: 运行中任务数
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # 活跃通话数
    placeholders = ", ".join(f"${i}" for i in range(1, len(ACTIVE_STATUSES) + 1))
    active_calls = await db.fetchval(
        f"""
        SELECT COUNT(*) FROM lk_call_records
        WHERE status IN ({placeholders})
        """,
        *ACTIVE_STATUSES,
    )

    # 今日总通话
    today_total = await db.fetchval(
        """
        SELECT COUNT(*) FROM lk_call_records
        WHERE created_at >= $1
        """,
        today_start,
    )

    # 今日已完成通话（结果为 completed 或 transferred 视为成功）
    today_completed = await db.fetchval(
        """
        SELECT COUNT(*) FROM lk_call_records
        WHERE created_at >= $1 AND status = 'ended'
        """,
        today_start,
    )

    # 今日成功通话（result 为 completed 或 transferred）
    today_success = await db.fetchval(
        """
        SELECT COUNT(*) FROM lk_call_records
        WHERE created_at >= $1 AND result IN ('completed', 'transferred')
        """,
        today_start,
    )

    today_success_rate = round(today_success / today_total * 100, 2) if today_total > 0 else 0.0

    # 运行中任务数
    running_tasks = await db.fetchval(
        """
        SELECT COUNT(*) FROM lk_tasks WHERE status = 'running'
        """
    )

    return _ok({
        "active_calls": active_calls or 0,
        "today_total": today_total or 0,
        "today_completed": today_completed or 0,
        "today_success_rate": today_success_rate,
        "running_tasks": running_tasks or 0,
    })


@router.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket):
    """WebSocket 实时推送。

    客户端连接后，每 2 秒推送一次当前活跃通话与统计数据。
    推送格式:
        {"type": "update", "data": {"active_calls": [...], "stats": {...}}}
    """
    await manager.connect(websocket)
    try:
        while True:
            # 获取活跃通话
            placeholders = ", ".join(
                f"${i}" for i in range(1, len(ACTIVE_STATUSES) + 1)
            )
            rows = await db.fetch(
                f"""
                SELECT id, call_id, task_id, phone, script_id, status,
                       duration_sec, rounds, started_at, answered_at
                FROM lk_call_records
                WHERE status IN ({placeholders})
                ORDER BY created_at DESC
                """,
                *ACTIVE_STATUSES,
            )
            calls = [dict(r) for r in rows]
            for call in calls:
                for key in ("started_at", "answered_at"):
                    val = call.get(key)
                    if val and isinstance(val, datetime):
                        call[key] = val.isoformat()

            # 获取统计
            now = datetime.now()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

            active_count = len(calls)
            today_total = await db.fetchval(
                "SELECT COUNT(*) FROM lk_call_records WHERE created_at >= $1",
                today_start,
            )
            running_tasks = await db.fetchval(
                "SELECT COUNT(*) FROM lk_tasks WHERE status = 'running'"
            )

            stats = {
                "active_calls": active_count,
                "today_total": today_total or 0,
                "running_tasks": running_tasks or 0,
            }

            await websocket.send_json({
                "type": "update",
                "data": {
                    "active_calls": calls,
                    "stats": stats,
                },
            })

            # 等待 2 秒再推送下一次
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as exc:
        logger.error("WebSocket 推送异常: %s", exc)
        manager.disconnect(websocket)


@router.get("/health", summary="健康检查")
async def health_check():
    """健康检查端点。

    实际检测数据库与 Redis 的连接状态。
    返回: status, uptime, version, db_connected, redis_connected
    """
    db_connected = False
    redis_connected = False

    # 检测数据库连接
    try:
        result = await db.fetchval("SELECT 1")
        db_connected = result == 1
    except Exception as exc:
        logger.warning("数据库连接检查失败: %s", exc)

    # 检测 Redis 连接
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(settings.redis_url)
        await r.ping()
        redis_connected = True
        await r.aclose()
    except Exception as exc:
        logger.warning("Redis 连接检查失败: %s", exc)

    uptime = round(time.time() - _start_time, 1)
    status = "healthy" if (db_connected and redis_connected) else "degraded"

    return _ok({
        "status": status,
        "uptime": uptime,
        "version": "0.1.0",
        "db_connected": db_connected,
        "redis_connected": redis_connected,
    })
