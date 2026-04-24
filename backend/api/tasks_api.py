"""外呼任务 + 通话记录 REST API

统一响应格式: {"code": 0, "data": ..., "message": "ok"}
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from backend.models.task import TaskCreate, TaskResponse, TaskStats, TaskDetail
from backend.models.call_record import CallRecordCreate, CallRecordUpdate, CallRecordResponse
from backend.services import task_service, call_record_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])


# ── 通用响应 ─────────────────────────────────────────────────────────

def _ok(data=None, message: str = "ok") -> dict:
    return {"code": 0, "data": data, "message": message}


def _fail(code: int = -1, message: str = "error") -> dict:
    return {"code": code, "data": None, "message": message}


# ── 任务 API ──────────────────────────────────────────────────────────

@router.post("/api/tasks", summary="创建任务")
async def create_task(data: TaskCreate):
    """创建外呼任务，包含号码列表"""
    try:
        result = await task_service.create_task(data)
        return _ok(result)
    except Exception as e:
        logger.error(f"创建任务失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/tasks", summary="任务列表")
async def list_tasks(
    status: Optional[str] = Query(None, description="按状态过滤"),
):
    """获取任务列表"""
    result = await task_service.get_tasks(status_filter=status)
    return _ok(result)


@router.get("/api/tasks/{task_id}", summary="任务详情")
async def get_task(task_id: str):
    """获取任务详情（含统计信息）"""
    detail = await task_service.get_task_detail(task_id)
    if not detail:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _ok(detail.model_dump())


@router.post("/api/tasks/{task_id}/start", summary="启动任务")
async def start_task(task_id: str):
    """启动外呼任务，开始调度拨打"""
    try:
        result = await task_service.start_task(task_id)
        return _ok(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"启动任务失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tasks/{task_id}/pause", summary="暂停任务")
async def pause_task(task_id: str):
    """暂停外呼任务"""
    try:
        result = await task_service.pause_task(task_id)
        return _ok(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"暂停任务失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tasks/{task_id}/cancel", summary="取消任务")
async def cancel_task(task_id: str):
    """取消外呼任务"""
    try:
        result = await task_service.cancel_task(task_id)
        return _ok(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"取消任务失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/tasks/{task_id}/stats", summary="任务统计")
async def get_task_stats(task_id: str):
    """获取任务统计信息"""
    stats = await task_service.get_task_stats(task_id)
    if not stats:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _ok(stats.model_dump())


# ── 通话记录 API ──────────────────────────────────────────────────────

@router.get("/api/calls", summary="通话记录列表")
async def list_calls(
    task_id: Optional[str] = Query(None, description="按任务ID过滤"),
    phone: Optional[str] = Query(None, description="按号码过滤"),
    limit: int = Query(50, ge=1, le=200, description="每页数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
):
    """查询通话记录列表，支持过滤和分页"""
    records = await call_record_service.get_records(
        task_id=task_id, phone=phone, limit=limit, offset=offset
    )
    total = await call_record_service.get_records_count(
        task_id=task_id, phone=phone
    )
    return _ok({
        "items": records,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@router.get("/api/calls/active", summary="进行中的通话")
async def list_active_calls():
    """获取当前进行中的通话列表"""
    result = await call_record_service.get_active_calls()
    return _ok(result)


@router.get("/api/calls/{call_id}", summary="通话详情")
async def get_call(call_id: str):
    """获取通话记录详情"""
    result = await call_record_service.get_record(call_id)
    if not result:
        raise HTTPException(status_code=404, detail="通话记录不存在")
    return _ok(result)


@router.post("/api/calls", summary="创建通话记录")
async def create_call_record(data: CallRecordCreate):
    """创建通话记录（通常由系统内部调用）"""
    try:
        result = await call_record_service.create_record(data)
        return _ok(result)
    except Exception as e:
        logger.error(f"创建通话记录失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/calls/{call_id}", summary="更新通话记录")
async def update_call_record(call_id: str, data: CallRecordUpdate):
    """更新通话记录（由 Agent 回调使用）"""
    result = await call_record_service.update_record(call_id, data)
    if not result:
        raise HTTPException(status_code=404, detail="通话记录不存在")
    return _ok(result)
