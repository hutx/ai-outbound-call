"""
话术脚本管理API
提供增删改查话术脚本的REST API接口
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import List

from backend.services.script_service import script_service, ScriptConfig
from backend.core.auth import require_auth

router = APIRouter(prefix="/api/scripts", tags=["scripts"])


@router.get("/", response_model=List[dict])
async def list_scripts(current_user: dict = Depends(require_auth)):
    """
    获取所有话术脚本
    """
    try:
        scripts = await script_service.get_all_scripts()
        return [
            {
                "script_id": s.script_id,
                "name": s.name,
                "description": s.description,
                "script_type": s.script_type,
                "opening_script": s.opening_script,
                "opening_pause": s.opening_pause,
                "closing_script": s.closing_script,
                "opening_barge_in": s.opening_barge_in,
                "closing_barge_in": s.closing_barge_in,
                "conversation_barge_in": s.conversation_barge_in,
                "barge_in_protect_start": s.barge_in_protect_start,
                "barge_in_protect_end": s.barge_in_protect_end,
                "tolerance_enabled": s.tolerance_enabled,
                "tolerance_ms": s.tolerance_ms,
                "no_response_timeout": s.no_response_timeout,
                "no_response_mode": s.no_response_mode,
                "no_response_max_count": s.no_response_max_count,
                "no_response_hangup_msg": s.no_response_hangup_msg,
                "no_response_hangup_enabled": s.no_response_hangup_enabled,
                "is_active": s.is_active
            }
            for s in scripts
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取话术脚本列表失败: {str(e)}")


@router.get("/{script_id}", response_model=dict)
async def get_script(script_id: str, current_user: dict = Depends(require_auth)):
    """
    获取指定话术脚本
    """
    try:
        script = await script_service.get_script(script_id)
        if not script:
            raise HTTPException(status_code=404, detail=f"话术脚本不存在: {script_id}")

        return {
            "script_id": script.script_id,
            "name": script.name,
            "description": script.description,
            "script_type": script.script_type,
            "opening_script": script.opening_script,
            "opening_pause": script.opening_pause,
            "main_script": script.main_script,
            "closing_script": script.closing_script,
            "opening_barge_in": script.opening_barge_in,
            "closing_barge_in": script.closing_barge_in,
            "conversation_barge_in": script.conversation_barge_in,
            "barge_in_protect_start": script.barge_in_protect_start,
            "barge_in_protect_end": script.barge_in_protect_end,
            "tolerance_enabled": script.tolerance_enabled,
            "tolerance_ms": script.tolerance_ms,
            "no_response_timeout": script.no_response_timeout,
            "no_response_mode": script.no_response_mode,
            "no_response_max_count": script.no_response_max_count,
            "no_response_hangup_msg": script.no_response_hangup_msg,
            "no_response_hangup_enabled": script.no_response_hangup_enabled,
            "is_active": script.is_active
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取话术脚本失败: {str(e)}")


@router.post("/", response_model=dict)
async def create_script(script_data: dict, current_user: dict = Depends(require_auth)):
    """
    创建新的话术脚本
    """
    try:
        required_fields = ["script_id", "name", "opening_script", "main_script"]
        for field in required_fields:
            if field not in script_data:
                raise HTTPException(status_code=400, detail=f"缺少必需字段: {field}")

        script_id = script_data["script_id"]
        name = script_data["name"]
        description = script_data.get("description", "")
        script_type = script_data.get("script_type", "financial")
        opening_script = script_data["opening_script"]
        opening_pause = script_data.get("opening_pause", 2000)
        main_script = script_data["main_script"]
        closing_script = script_data.get("closing_script")
        is_active = script_data.get("is_active", True)

        # 验证数据
        if not script_id or not name or not opening_script:
            raise HTTPException(status_code=400, detail="script_id、name和opening_script不能为空")

        # 检查是否已存在
        existing = await script_service.get_script(script_id)
        if existing:
            raise HTTPException(status_code=409, detail=f"话术脚本已存在: {script_id}")

        # 创建脚本配置对象
        script_config = ScriptConfig(
            script_id=script_id,
            name=name,
            description=description,
            script_type=script_type,
            opening_script=opening_script,
            opening_pause=opening_pause,
            main_script=main_script,
            closing_script=closing_script,
            is_active=is_active,
            opening_barge_in=script_data.get("opening_barge_in", False),
            closing_barge_in=script_data.get("closing_barge_in", False),
            conversation_barge_in=script_data.get("conversation_barge_in", True),
            barge_in_protect_start=script_data.get("barge_in_protect_start", 3),
            barge_in_protect_end=script_data.get("barge_in_protect_end", 3),
            tolerance_enabled=script_data.get("tolerance_enabled", True),
            tolerance_ms=script_data.get("tolerance_ms", 1000),
            no_response_timeout=script_data.get("no_response_timeout", 3),
            no_response_mode=script_data.get("no_response_mode", "consecutive"),
            no_response_max_count=script_data.get("no_response_max_count", 3),
            no_response_hangup_msg=script_data.get("no_response_hangup_msg"),
            no_response_hangup_enabled=script_data.get("no_response_hangup_enabled", True)
        )

        # 调用服务创建脚本
        success = await script_service.create_script(script_config)
        if not success:
            raise HTTPException(status_code=500, detail="创建话术脚本失败")

        return {
            "message": "话术脚本创建成功",
            "script_id": script_id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建话术脚本失败: {str(e)}")


@router.put("/{script_id}", response_model=dict)
async def update_script(script_id: str, script_data: dict, current_user: dict = Depends(require_auth)):
    """
    更新话术脚本
    """
    try:
        # 检查脚本是否存在
        existing = await script_service.get_script(script_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"话术脚本不存在: {script_id}")

        # 准备更新数据
        update_data = {}
        if "name" in script_data:
            update_data["name"] = script_data["name"]
        if "description" in script_data:
            update_data["description"] = script_data["description"]
        if "script_type" in script_data:
            update_data["script_type"] = script_data["script_type"]
        if "opening_script" in script_data:
            update_data["opening_script"] = script_data["opening_script"]
        if "opening_pause" in script_data:
            update_data["opening_pause"] = script_data["opening_pause"]
        if "main_script" in script_data:
            update_data["main_script"] = script_data["main_script"]
        if "closing_script" in script_data:
            update_data["closing_script"] = script_data["closing_script"]
        if "is_active" in script_data:
            update_data["is_active"] = script_data["is_active"]
        if "opening_barge_in" in script_data:
            update_data["opening_barge_in"] = script_data["opening_barge_in"]
        if "closing_barge_in" in script_data:
            update_data["closing_barge_in"] = script_data["closing_barge_in"]
        if "conversation_barge_in" in script_data:
            update_data["conversation_barge_in"] = script_data["conversation_barge_in"]
        if "barge_in_protect_start" in script_data:
            update_data["barge_in_protect_start"] = script_data["barge_in_protect_start"]
        if "barge_in_protect_end" in script_data:
            update_data["barge_in_protect_end"] = script_data["barge_in_protect_end"]
        if "tolerance_enabled" in script_data:
            update_data["tolerance_enabled"] = script_data["tolerance_enabled"]
        if "tolerance_ms" in script_data:
            update_data["tolerance_ms"] = script_data["tolerance_ms"]
        if "no_response_timeout" in script_data:
            update_data["no_response_timeout"] = script_data["no_response_timeout"]
        if "no_response_mode" in script_data:
            update_data["no_response_mode"] = script_data["no_response_mode"]
        if "no_response_max_count" in script_data:
            update_data["no_response_max_count"] = script_data["no_response_max_count"]
        if "no_response_hangup_msg" in script_data:
            update_data["no_response_hangup_msg"] = script_data["no_response_hangup_msg"]
        if "no_response_hangup_enabled" in script_data:
            update_data["no_response_hangup_enabled"] = script_data["no_response_hangup_enabled"]

        # 调用服务更新脚本
        success = await script_service.update_script(script_id, **update_data)
        if not success:
            raise HTTPException(status_code=500, detail="更新话术脚本失败")

        # 清除缓存，下次获取时重新从数据库加载
        if script_id in script_service._cache:
            del script_service._cache[script_id]

        return {
            "message": "话术脚本更新成功",
            "script_id": script_id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新话术脚本失败: {str(e)}")


@router.delete("/{script_id}", response_model=dict)
async def delete_script(script_id: str, current_user: dict = Depends(require_auth)):
    """
    删除话术脚本（软删除，设置is_active为False）
    """
    try:
        # 检查脚本是否存在
        existing = await script_service.get_script(script_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"话术脚本不存在: {script_id}")

        # 调用服务删除脚本（软删除）
        success = await script_service.delete_script(script_id)
        if not success:
            raise HTTPException(status_code=500, detail="删除话术脚本失败")

        # 清除缓存
        if script_id in script_service._cache:
            del script_service._cache[script_id]

        return {
            "message": "话术脚本删除成功",
            "script_id": script_id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除话术脚本失败: {str(e)}")