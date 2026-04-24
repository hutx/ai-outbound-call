"""话术管理 API

提供话术的增删改查 REST 接口，统一响应格式 {"code": 0, "data": ..., "message": "ok"}。
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from backend.models.script import ScriptCreate, ScriptUpdate, ScriptResponse
from backend.services.script_service import script_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scripts", tags=["scripts"])


# ------------------------------------------------------------------
# 统一响应辅助
# ------------------------------------------------------------------

def _ok(data=None, message: str = "ok") -> dict:
    """成功响应。"""
    return {"code": 0, "data": data, "message": message}


def _err(code: int, message: str) -> dict:
    """错误响应（仅用于 body 构造，HTTP 状态码由 HTTPException 控制）。"""
    return {"code": code, "data": None, "message": message}


# ------------------------------------------------------------------
# 路由
# ------------------------------------------------------------------


@router.get("", summary="获取话术列表")
async def list_scripts(
    active_only: bool = Query(True, description="仅返回活跃话术"),
) -> dict:
    """获取话术列表。

    Query 参数:
        active_only: 是否只返回 is_active=true 的话术，默认 true

    返回:
        {"code": 0, "data": [...], "message": "ok"}
    """
    try:
        scripts = await script_service.get_all_scripts(active_only=active_only)
        return _ok(data=scripts)
    except Exception as exc:
        logger.exception("获取话术列表失败")
        raise HTTPException(status_code=500, detail=_err(500, f"获取话术列表失败: {exc}"))


@router.post("", summary="创建话术", status_code=201)
async def create_script(body: ScriptCreate) -> dict:
    """创建话术。

    请求体: ScriptCreate（script_id 必填）
    冲突时返回 409。
    """
    try:
        created = await script_service.create_script(body)
        return _ok(data=created, message="话术创建成功")
    except ValueError as exc:
        # script_id 冲突
        raise HTTPException(status_code=409, detail=_err(409, str(exc)))
    except Exception as exc:
        logger.exception("创建话术失败")
        raise HTTPException(status_code=500, detail=_err(500, f"创建话术失败: {exc}"))


@router.get("/{script_id}", summary="获取话术详情", response_model=None)
async def get_script(script_id: str) -> dict:
    """获取指定话术详情。

    不存在时返回 404。
    """
    try:
        script = await script_service.get_script(script_id)
        if script is None:
            raise HTTPException(
                status_code=404,
                detail=_err(404, f"话术不存在: {script_id}"),
            )
        return _ok(data=script)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("获取话术失败: %s", script_id)
        raise HTTPException(status_code=500, detail=_err(500, f"获取话术失败: {exc}"))


@router.put("/{script_id}", summary="更新话术")
async def update_script(script_id: str, body: ScriptUpdate) -> dict:
    """更新话术。

    请求体: ScriptUpdate（所有字段可选，仅更新非 None 字段）
    不存在时返回 404。
    """
    try:
        updated = await script_service.update_script(script_id, body)
        if updated is None:
            raise HTTPException(
                status_code=404,
                detail=_err(404, f"话术不存在: {script_id}"),
            )
        return _ok(data=updated, message="话术更新成功")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("更新话术失败: %s", script_id)
        raise HTTPException(status_code=500, detail=_err(500, f"更新话术失败: {exc}"))


@router.delete("/{script_id}", summary="删除话术（软删除）")
async def delete_script(script_id: str) -> dict:
    """软删除话术（设置 is_active=false）。

    不存在时返回 404。
    """
    try:
        success = await script_service.delete_script(script_id)
        if not success:
            raise HTTPException(
                status_code=404,
                detail=_err(404, f"话术不存在: {script_id}"),
            )
        return _ok(data={"script_id": script_id}, message="话术已删除")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("删除话术失败: %s", script_id)
        raise HTTPException(status_code=500, detail=_err(500, f"删除话术失败: {exc}"))
