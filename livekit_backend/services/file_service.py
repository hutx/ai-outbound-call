"""文件记录服务

管理 lk_files 表的 CRUD 操作，供录音/音频文件元数据管理使用。
"""
import logging
from typing import Optional, List, Dict, Any

from livekit_backend.models.file import FileRecordCreate
from livekit_backend.utils import db

logger = logging.getLogger(__name__)


async def create_file_record(data: FileRecordCreate) -> Dict[str, Any]:
    """创建文件记录

    Args:
        data: 文件记录创建请求

    Returns:
        创建后的记录
    """
    row = await db.fetchrow(
        """
        INSERT INTO lk_files
            (file_id, call_id, file_type, storage_path, storage_bucket,
             file_name, mime_type, file_size_bytes, duration_sec,
             sample_rate, egress_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING *
        """,
        data.file_id,
        data.call_id,
        data.file_type,
        data.storage_path,
        data.storage_bucket,
        data.file_name,
        data.mime_type,
        data.file_size_bytes,
        data.duration_sec,
        data.sample_rate,
        data.egress_id,
    )
    logger.info("文件记录已创建: file_id=%s, call_id=%s", data.file_id, data.call_id)
    return _row_to_dict(row)


async def get_file_by_id(file_id: str) -> Optional[Dict[str, Any]]:
    """根据 file_id 获取单条文件记录

    Args:
        file_id: 文件唯一标识

    Returns:
        文件记录，找不到返回 None
    """
    row = await db.fetchrow(
        "SELECT * FROM lk_files WHERE file_id = $1",
        file_id,
    )
    return _row_to_dict(row) if row else None


async def get_files_by_call_id(call_id: str) -> List[Dict[str, Any]]:
    """根据 call_id 获取关联的所有文件记录

    Args:
        call_id: 通话唯一标识

    Returns:
        文件记录列表
    """
    rows = await db.fetch(
        """
        SELECT * FROM lk_files
        WHERE call_id = $1
        ORDER BY created_at DESC
        """,
        call_id,
    )
    return [_row_to_dict(r) for r in rows]


async def delete_file_record(file_id: str) -> bool:
    """删除文件记录

    Args:
        file_id: 文件唯一标识

    Returns:
        是否成功删除
    """
    result = await db.execute(
        "DELETE FROM lk_files WHERE file_id = $1",
        file_id,
    )
    deleted = "DELETE 1" in result
    if deleted:
        logger.info("文件记录已删除: file_id=%s", file_id)
    else:
        logger.warning("文件记录不存在，删除失败: file_id=%s", file_id)
    return deleted


# ── 内部辅助 ──────────────────────────────────────────────────────────


def _row_to_dict(row: Any) -> Dict[str, Any]:
    """将 asyncpg Record 转为 dict"""
    if row is None:
        return {}
    return dict(row)

