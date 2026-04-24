"""通话记录详情服务

管理 lk_call_record_details 表的 CRUD 操作，记录每轮问答的详细信息。
"""
import json
import logging
from typing import Optional, List, Dict, Any

from backend.models.call_record_detail import CallRecordDetailCreate
from backend.utils import db

logger = logging.getLogger(__name__)


async def create_detail(data: CallRecordDetailCreate) -> Dict[str, Any]:
    """创建通话记录详情（单轮问答）

    Args:
        data: 详情创建请求

    Returns:
        创建后的记录
    """
    metadata_json = json.dumps(data.metadata) if data.metadata else "{}"

    row = await db.fetchrow(
        """
        INSERT INTO lk_call_record_details
            (call_record_id, call_id, round_num, question,
             question_audio_file_id, question_duration_sec, answer_content,
             answer_audio_file_id, answer_duration_sec, is_interrupted,
             interrupted_at_sec, start_time, end_time, stt_latency_ms,
             llm_latency_ms, tts_latency_ms, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17::jsonb)
        RETURNING *
        """,
        data.call_record_id,
        data.call_id,
        data.round_num,
        data.question,
        data.question_audio_file_id,
        data.question_duration_sec,
        data.answer_content,
        data.answer_audio_file_id,
        data.answer_duration_sec,
        data.is_interrupted,
        data.interrupted_at_sec,
        data.start_time,
        data.end_time,
        data.stt_latency_ms,
        data.llm_latency_ms,
        data.tts_latency_ms,
        metadata_json,
    )
    logger.info(
        "通话详情已创建: call_id=%s, round_num=%d",
        data.call_id,
        data.round_num,
    )
    return _row_to_dict(row)


async def get_details_by_call_id(call_id: str) -> List[Dict[str, Any]]:
    """根据 call_id 获取所有问答详情

    Args:
        call_id: 通话唯一标识

    Returns:
        问答详情列表，按轮次排序
    """
    rows = await db.fetch(
        """
        SELECT * FROM lk_call_record_details
        WHERE call_id = $1
        ORDER BY round_num ASC
        """,
        call_id,
    )
    return [_row_to_dict(r) for r in rows]


async def get_details_by_record_id(call_record_id: int) -> List[Dict[str, Any]]:
    """根据通话记录 ID 获取所有问答详情

    Args:
        call_record_id: 通话记录主键 ID

    Returns:
        问答详情列表，按轮次排序
    """
    rows = await db.fetch(
        """
        SELECT * FROM lk_call_record_details
        WHERE call_record_id = $1
        ORDER BY round_num ASC
        """,
        call_record_id,
    )
    return [_row_to_dict(r) for r in rows]


async def get_detail_by_round(call_id: str, round_num: int) -> Optional[Dict[str, Any]]:
    """根据 call_id 和轮次获取单条详情

    Args:
        call_id: 通话唯一标识
        round_num: 对话轮次

    Returns:
        单条详情，找不到返回 None
    """
    row = await db.fetchrow(
        """
        SELECT * FROM lk_call_record_details
        WHERE call_id = $1 AND round_num = $2
        """,
        call_id,
        round_num,
    )
    return _row_to_dict(row) if row else None


async def update_detail(id: int, **kwargs) -> Optional[Dict[str, Any]]:
    """部分更新通话记录详情

    Args:
        id: 详情记录主键 ID
        **kwargs: 要更新的字段及其值

    Returns:
        更新后的记录，找不到返回 None
    """
    if not kwargs:
        return await _get_detail_by_id(id)

    allowed_fields = {
        "question",
        "question_audio_file_id",
        "question_duration_sec",
        "answer_content",
        "answer_audio_file_id",
        "answer_duration_sec",
        "is_interrupted",
        "interrupted_at_sec",
        "start_time",
        "end_time",
        "stt_latency_ms",
        "llm_latency_ms",
        "tts_latency_ms",
        "metadata",
    }

    updates: List[str] = []
    args: List[Any] = []
    idx = 1

    for key, value in kwargs.items():
        if key not in allowed_fields:
            logger.warning("忽略不允许更新的字段: %s", key)
            continue

        if key == "metadata" and isinstance(value, dict):
            updates.append(f"{key} = ${idx}::jsonb")
            args.append(json.dumps(value))
        else:
            updates.append(f"{key} = ${idx}")
            args.append(value)
        idx += 1

    if not updates:
        return await _get_detail_by_id(id)

    query = f"""
        UPDATE lk_call_record_details
        SET {', '.join(updates)}
        WHERE id = ${idx}
        RETURNING *
    """
    args.append(id)

    row = await db.fetchrow(query, *args)
    if not row:
        logger.warning("通话详情不存在: id=%d", id)
        return None

    logger.info("通话详情已更新: id=%d, fields=%s", id, list(kwargs.keys()))
    return _row_to_dict(row)


# ── 内部辅助 ──────────────────────────────────────────────────────────


async def _get_detail_by_id(id: int) -> Optional[Dict[str, Any]]:
    """根据主键 ID 获取单条详情"""
    row = await db.fetchrow(
        "SELECT * FROM lk_call_record_details WHERE id = $1",
        id,
    )
    return _row_to_dict(row) if row else None


def _row_to_dict(row: Any) -> Dict[str, Any]:
    """将 asyncpg Record 转为 dict"""
    if row is None:
        return {}
    return dict(row)
