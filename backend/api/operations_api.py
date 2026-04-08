"""
任务、通话记录、黑名单管理路由
"""

import logging
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from backend.core.auth import require_auth
from backend.services.crm_service import crm
from backend.utils.db import get_call_stats, list_call_records

logger = logging.getLogger(__name__)


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
        return v

    @field_validator("concurrent_limit")
    @classmethod
    def validate_concurrent(cls, v):
        from backend.core.config import config

        return max(1, min(v, config.max_concurrent_calls))


class BlacklistRequest(BaseModel):
    phone: str
    reason: str = "手动添加"


class OperationsAPI:
    def __init__(self, *, scheduler_getter: Callable[[], object], metrics: dict[str, int]):
        self._scheduler_getter = scheduler_getter
        self._metrics = metrics
        self.router = APIRouter()
        self._register_routes()

    def _register_routes(self):
        @self.router.post("/api/tasks", dependencies=[Depends(require_auth)])
        async def create_task(req: CreateTaskRequest):
            scheduler = self._scheduler_getter()
            if not scheduler:
                raise HTTPException(503, "调度器未初始化")

            task = scheduler.create_task(
                name=req.name,
                phone_numbers=req.phone_numbers,
                script_id=req.script_id,
                concurrent_limit=req.concurrent_limit,
                max_retries=req.max_retries,
                caller_id=req.caller_id,
            )

            await scheduler.start_task(task.task_id)
            self._metrics["tasks_created"] += 1
            logger.info(f"创建任务 {task.task_id}: {req.name} ({len(req.phone_numbers)} 个号码)")
            return {
                "task_id": task.task_id,
                "name": task.name,
                "status": task.status.name,
                "total": task.total,
                "message": f"已创建，共 {task.total} 个号码",
            }

        @self.router.get("/api/tasks", dependencies=[Depends(require_auth)])
        async def list_tasks():
            scheduler = self._scheduler_getter()
            if not scheduler:
                return {"tasks": []}
            return {"tasks": scheduler.list_tasks()}

        @self.router.get("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
        async def get_task(task_id: str):
            scheduler = self._scheduler_getter()
            if not scheduler:
                raise HTTPException(503, "调度器未初始化")
            task = scheduler.get_task(task_id)
            if not task:
                raise HTTPException(404, "任务不存在")
            return task.to_dict()

        @self.router.post("/api/tasks/{task_id}/pause", dependencies=[Depends(require_auth)])
        async def pause_task(task_id: str):
            scheduler = self._scheduler_getter()
            if not scheduler or not scheduler.pause_task(task_id):
                raise HTTPException(404, "任务不存在或无法暂停")
            return {"message": "已暂停"}

        @self.router.post("/api/tasks/{task_id}/resume", dependencies=[Depends(require_auth)])
        async def resume_task(task_id: str):
            scheduler = self._scheduler_getter()
            if not scheduler or not scheduler.resume_task(task_id):
                raise HTTPException(404, "任务不存在或无法恢复")
            return {"message": "已恢复"}

        @self.router.delete("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
        async def cancel_task(task_id: str):
            scheduler = self._scheduler_getter()
            if not scheduler or not scheduler.cancel_task(task_id):
                raise HTTPException(404, "任务不存在")
            return {"message": "已取消"}

        @self.router.get("/api/calls", dependencies=[Depends(require_auth)])
        async def list_calls(
            task_id: Optional[str] = None,
            phone: Optional[str] = None,
            result: Optional[str] = None,
            limit: int = 50,
            offset: int = 0,
        ):
            try:
                records = await list_call_records(
                    task_id=task_id,
                    phone=phone,
                    result=result,
                    limit=limit,
                    offset=offset,
                )
                return {"records": records, "total": len(records)}
            except Exception as e:
                logger.error(f"查询通话记录失败: {e}")
                return {"records": [], "total": 0}

        @self.router.get("/api/calls/stats", dependencies=[Depends(require_auth)])
        async def call_stats(task_id: Optional[str] = None):
            try:
                return await get_call_stats(task_id=task_id)
            except Exception as e:
                logger.error(f"统计查询失败: {e}")
                return {
                    "total": 0,
                    "connected": 0,
                    "intent": 0,
                    "connect_rate": 0.0,
                    "intent_rate": 0.0,
                }

        @self.router.get("/api/blacklist", dependencies=[Depends(require_auth)])
        async def list_blacklist():
            try:
                items = await crm.list_blacklist()
                return {"items": items, "total": len(items)}
            except Exception:
                return {"items": [], "total": 0}

        @self.router.post("/api/blacklist", dependencies=[Depends(require_auth)])
        async def add_blacklist(req: BlacklistRequest):
            await crm.add_to_blacklist(req.phone, req.reason)
            return {"message": f"{req.phone} 已加入黑名单"}

        @self.router.delete("/api/blacklist/{phone}", dependencies=[Depends(require_auth)])
        async def remove_blacklist(phone: str):
            await crm.remove_from_blacklist(phone)
            return {"message": f"{phone} 已从黑名单移除"}
