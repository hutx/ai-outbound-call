"""
监控与指标路由
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from websockets.exceptions import ConnectionClosed

from backend.core.auth import require_auth

logger = logging.getLogger(__name__)


class MonitorAPI:
    def __init__(
        self,
        *,
        config,
        metrics: dict[str, int],
        scheduler_getter: Callable[[], object],
        esl_pool_getter: Callable[[], object],
        start_time: float,
    ):
        self._config = config
        self._metrics = metrics
        self._scheduler_getter = scheduler_getter
        self._esl_pool_getter = esl_pool_getter
        self._start_time = start_time
        self._ws_clients: list[WebSocket] = []
        self.router = APIRouter()
        self._register_routes()

    async def broadcast_stats(self):
        if not self._ws_clients:
            return
        data = await self.build_monitor_data()
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._ws_clients:
                self._ws_clients.remove(ws)

    async def build_monitor_data(self) -> dict:
        scheduler = self._scheduler_getter()
        tasks = scheduler.list_tasks() if scheduler else []
        return {
            "type": "stats",
            "ts": datetime.now().isoformat(),
            "active_calls": self._metrics["calls_active"],
            "calls_total": self._metrics["calls_total"],
            "active_tasks": sum(1 for t in tasks if t["status"] == "RUNNING"),
            "tasks": tasks[:10],
        }

    def _register_routes(self):
        @self.router.get("/api/stats")
        async def get_stats():
            scheduler = self._scheduler_getter()
            tasks = scheduler.list_tasks() if scheduler else []
            return {
                "active_calls": self._metrics["calls_active"],
                "calls_total": self._metrics["calls_total"],
                "calls_completed": self._metrics["calls_completed"],
                "calls_transferred": self._metrics["calls_transferred"],
                "calls_error": self._metrics["calls_error"],
                "active_tasks": sum(1 for t in tasks if t["status"] == "RUNNING"),
                "total_tasks": len(tasks),
                "max_concurrent": self._config.max_concurrent_calls,
                "uptime_seconds": int(time.time() - self._start_time),
                "tasks": tasks[:20],
            }

        @self.router.get("/health")
        async def health():
            esl_pool = self._esl_pool_getter()
            esl_ok = bool(
                esl_pool is not None
                and esl_pool._conns
                and any(c.is_connected for c in esl_pool._conns)
            )
            return {
                "status": "ok" if esl_ok else "degraded",
                "esl_pool": esl_ok,
                "active_calls": self._metrics["calls_active"],
                "timestamp": datetime.now().isoformat(),
            }

        @self.router.get("/metrics", response_class=PlainTextResponse)
        async def prometheus_metrics():
            scheduler = self._scheduler_getter()
            tasks = scheduler.list_tasks() if scheduler else []
            active_tasks = sum(1 for t in tasks if t["status"] == "RUNNING")
            uptime = int(time.time() - self._start_time)
            lines = [
                "# HELP outbound_calls_total Total outbound calls initiated",
                "# TYPE outbound_calls_total counter",
                f'outbound_calls_total {self._metrics["calls_total"]}',
                "# HELP outbound_calls_active Currently active calls",
                "# TYPE outbound_calls_active gauge",
                f'outbound_calls_active {self._metrics["calls_active"]}',
                "# HELP outbound_calls_completed_total Completed calls",
                "# TYPE outbound_calls_completed_total counter",
                f'outbound_calls_completed_total {self._metrics["calls_completed"]}',
                "# HELP outbound_calls_transferred_total Transferred to human calls",
                "# TYPE outbound_calls_transferred_total counter",
                f'outbound_calls_transferred_total {self._metrics["calls_transferred"]}',
                "# HELP outbound_calls_error_total Error calls",
                "# TYPE outbound_calls_error_total counter",
                f'outbound_calls_error_total {self._metrics["calls_error"]}',
                "# HELP outbound_tasks_active Active running tasks",
                "# TYPE outbound_tasks_active gauge",
                f"outbound_tasks_active {active_tasks}",
                "# HELP outbound_tasks_total Total tasks created",
                "# TYPE outbound_tasks_total counter",
                f'outbound_tasks_total {self._metrics["tasks_created"]}',
                "# HELP outbound_uptime_seconds Service uptime in seconds",
                "# TYPE outbound_uptime_seconds gauge",
                f"outbound_uptime_seconds {uptime}",
                "",
            ]
            return "\n".join(lines)

        @self.router.websocket("/ws/monitor")
        async def monitor_ws(websocket: WebSocket):
            token = websocket.query_params.get("token", "")
            if self._config.api_token.strip() and token != self._config.api_token.strip():
                await websocket.close(code=4001, reason="Unauthorized")
                return

            await websocket.accept()
            self._ws_clients.append(websocket)
            logger.debug(f"监控 WS 连接，当前 {len(self._ws_clients)} 个客户端")

            try:
                await websocket.send_json(await self.build_monitor_data())
                while True:
                    try:
                        msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                        if msg == "ping":
                            await websocket.send_text("pong")
                    except asyncio.TimeoutError:
                        await websocket.send_text("ping")
            except (WebSocketDisconnect, ConnectionClosed, Exception):
                pass
            finally:
                if websocket in self._ws_clients:
                    self._ws_clients.remove(websocket)

        @self.router.get("/api/callbacks", dependencies=[Depends(require_auth)])
        async def list_callbacks():
            try:
                from backend.utils.db import db_list_callbacks

                items = await db_list_callbacks(status="pending")
                return {"items": items, "total": len(items)}
            except Exception:
                return {"items": [], "total": 0}
