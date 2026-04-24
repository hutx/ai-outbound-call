"""通话详情与录音 REST API

提供通话对话轮次查询、录音下载、文件信息等接口。
统一响应格式: {"code": 0, "data": ..., "message": "ok"}
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.services import call_record_detail_service, call_record_service, file_service
from backend.services.minio_service import minio_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["calls"])


# ── 通用响应 ─────────────────────────────────────────────────────────

def _ok(data=None, message: str = "ok") -> dict:
    return {"code": 0, "data": data, "message": message}


# ── 通话详情 API ─────────────────────────────────────────────────────

@router.get(
    "/api/calls/{call_id}/details",
    summary="查询通话详情（对话轮次）",
)
async def get_call_details(call_id: str):
    """查询指定通话的所有对话轮次，按 round_num 排序。

    返回格式:
        {"call_id": "xxx", "details": [...]}
    """
    # 先确认通话记录存在
    record = await call_record_service.get_record(call_id)
    if not record:
        raise HTTPException(status_code=404, detail="通话记录不存在")

    details = await call_record_detail_service.get_details_by_call_id(call_id)
    return _ok({
        "call_id": call_id,
        "details": details,
    })


# ── 录音下载 URL API ────────────────────────────────────────────────

@router.get(
    "/api/calls/{call_id}/recording",
    summary="获取通话录音下载 URL",
)
async def get_call_recording(call_id: str):
    """获取指定通话的完整录音下载链接。

    通过 call_record 中的 recording_file_id 关联 lk_files，
    再用 MinIO 预签名 URL 生成临时下载链接。

    返回格式:
        {"call_id": "xxx", "recording_url": "https://...", "duration_sec": 120.5}
    """
    # 查询通话记录
    record = await call_record_service.get_record(call_id)
    if not record:
        raise HTTPException(status_code=404, detail="通话记录不存在")

    recording_file_id = record.get("recording_file_id")
    if not recording_file_id:
        raise HTTPException(status_code=404, detail="该通话无录音文件")

    # 查询文件记录
    file_record = await file_service.get_file_by_id(recording_file_id)
    if not file_record:
        logger.warning("录音文件记录不存在: file_id=%s", recording_file_id)
        raise HTTPException(status_code=404, detail="录音文件记录不存在")

    # 生成预签名 URL
    try:
        storage_bucket = file_record.get("storage_bucket") or "recordings"
        storage_path = file_record.get("storage_path", "")
        recording_url = await minio_service.get_presigned_url(
            bucket=storage_bucket,
            object_name=storage_path,
        )
    except Exception as e:
        logger.error("生成录音下载 URL 失败: call_id=%s, error=%s", call_id, e)
        raise HTTPException(status_code=500, detail=f"生成下载链接失败: {e}")

    return _ok({
        "call_id": call_id,
        "recording_url": recording_url,
        "duration_sec": file_record.get("duration_sec", 0.0),
    })


# ── 文件信息 API ────────────────────────────────────────────────────

@router.get(
    "/api/files/{file_id}",
    summary="获取文件信息及下载 URL",
)
async def get_file_info(file_id: str):
    """查询文件记录并生成预签名下载 URL。

    返回文件元信息 + 下载 URL。
    """
    file_record = await file_service.get_file_by_id(file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")

    # 生成预签名 URL
    try:
        storage_bucket = file_record.get("storage_bucket") or "recordings"
        storage_path = file_record.get("storage_path", "")
        download_url = await minio_service.get_presigned_url(
            bucket=storage_bucket,
            object_name=storage_path,
        )
    except Exception as e:
        logger.error("生成文件下载 URL 失败: file_id=%s, error=%s", file_id, e)
        raise HTTPException(status_code=500, detail=f"生成下载链接失败: {e}")

    # 组装响应，附带下载 URL
    result = dict(file_record)
    result["download_url"] = download_url
    return _ok(result)
