"""通话记录服务

管理 lk_call_records 表的 CRUD 操作，供 Agent 回调和 API 查询使用。
"""
import json
import logging
from typing import Optional, List, Dict, Any

from backend.models.call_record import CallRecordCreate, CallRecordUpdate
from backend.utils import db

logger = logging.getLogger(__name__)


async def create_record(data: CallRecordCreate) -> Dict[str, Any]:
    """创建通话记录

    在发起外呼时调用，记录 call_id 与任务/号码的关联。

    Args:
        data: 通话记录创建请求

    Returns:
        创建后的记录
    """
    row = await db.fetchrow(
        """
        INSERT INTO lk_call_records
            (call_id, task_id, phone, script_id, status, started_at)
        VALUES ($1, $2, $3, $4, $5, NOW())
        RETURNING *
        """,
        data.call_id,
        data.task_id,
        data.phone,
        data.script_id,
        "initiating",
    )
    logger.info(f"通话记录已创建: call_id={data.call_id}")
    return _row_to_dict(row)


async def update_record(call_id: str, data: CallRecordUpdate) -> Optional[Dict[str, Any]]:
    """更新通话记录

    由 Agent 回调在通话状态变化时调用。

    Args:
        call_id: 通话ID
        data: 更新数据

    Returns:
        更新后的记录，找不到返回 None
    """
    # 构建动态 SET 子句
    updates: List[str] = []
    args: List[Any] = []
    idx = 1

    field_map = {
        "status": data.status,
        "intent": data.intent,
        "result": data.result,
        "duration_sec": data.duration_sec,
        "user_talk_time_sec": data.user_talk_time_sec,
        "ai_talk_time_sec": data.ai_talk_time_sec,
        "rounds": data.rounds,
        "sip_code": data.sip_code,
        "hangup_cause": data.hangup_cause,
        "recording_url": data.recording_url,
        "recording_file_id": data.recording_file_id,
        "egress_id": data.egress_id,
        "total_duration_sec": data.total_duration_sec,
        "answered_at": data.answered_at,
        "ended_at": data.ended_at,
    }

    for field, value in field_map.items():
        if value is not None:
            updates.append(f"{field} = ${idx}")
            args.append(value)
            idx += 1

    # transcript 需要特殊处理（JSONB）
    if data.transcript is not None:
        transcript_json = json.dumps([entry.model_dump() for entry in data.transcript])
        updates.append(f"transcript = ${idx}::jsonb")
        args.append(transcript_json)
        idx += 1

    if not updates:
        return await get_record(call_id)

    # updated_at 直接在 SQL 中写 NOW()，不作为参数
    updates.append("updated_at = NOW()")

    # call_id 作为最后一个参数
    query = f"""
        UPDATE lk_call_records
        SET {', '.join(updates)}
        WHERE call_id = ${idx}
        RETURNING *
    """
    args.append(call_id)

    row = await db.fetchrow(query, *args)
    if not row:
        logger.warning(f"通话记录不存在: call_id={call_id}")
        return None

    # 如果通话结束，同步更新任务号码状态
    if data.status in ("ended",) and data.result:
        await _sync_phone_status(row)

    logger.info(f"通话记录已更新: call_id={call_id}, updates={list(field_map.keys())}")
    return _row_to_dict(row)


async def get_record(call_id: str) -> Optional[Dict[str, Any]]:
    """获取单条通话记录"""
    row = await db.fetchrow(
        "SELECT * FROM lk_call_records WHERE call_id = $1",
        call_id,
    )
    return _row_to_dict(row) if row else None


async def get_records(
    task_id: Optional[str] = None,
    phone: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """查询通话记录列表

    Args:
        task_id: 按任务ID过滤
        phone: 按号码过滤
        limit: 每页数量
        offset: 偏移量

    Returns:
        通话记录列表
    """
    conditions: List[str] = []
    args: List[Any] = []
    idx = 0

    if task_id:
        idx += 1
        conditions.append(f"task_id = ${idx}")
        args.append(task_id)

    if phone:
        idx += 1
        conditions.append(f"phone = ${idx}")
        args.append(phone)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    idx += 1
    args.append(limit)
    idx += 1
    args.append(offset)

    query = f"""
        SELECT * FROM lk_call_records
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ${idx - 1} OFFSET ${idx}
    """

    rows = await db.fetch(query, *args)
    return [_row_to_dict(r) for r in rows]


async def get_active_calls() -> List[Dict[str, Any]]:
    """获取进行中的通话列表"""
    rows = await db.fetch(
        """
        SELECT * FROM lk_call_records
        WHERE status NOT IN ('ended', 'completed', 'failed', 'not_answered',
                             'busy', 'rejected', 'timeout', 'error')
        ORDER BY created_at DESC
        """
    )
    return [_row_to_dict(r) for r in rows]


async def get_records_count(
    task_id: Optional[str] = None,
    phone: Optional[str] = None,
) -> int:
    """获取通话记录总数（分页用）"""
    conditions: List[str] = []
    args: List[Any] = []
    idx = 0

    if task_id:
        idx += 1
        conditions.append(f"task_id = ${idx}")
        args.append(task_id)

    if phone:
        idx += 1
        conditions.append(f"phone = ${idx}")
        args.append(phone)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"SELECT COUNT(*) AS cnt FROM lk_call_records {where_clause}"
    result = await db.fetchval(query, *args)
    return result or 0


# ── 内部辅助 ──────────────────────────────────────────────────────────

async def _sync_phone_status(record: Dict[str, Any]) -> None:
    """通话结束后同步更新任务号码状态和计数

    将 CDR 中的通话结果、SIP 状态、通话时长等信息同步到 lk_task_phones 表。
    """
    task_id = record.get("task_id")
    call_id = record.get("call_id")
    result = record.get("result", "")

    if not task_id or not call_id:
        return

    # 根据通话结果判断号码状态
    success_results = {"completed", "transferred"}
    phone_status = "completed" if result in success_results else "failed"

    # 构建 SIP 状态信息
    sip_code = record.get("sip_code")
    hangup_cause = record.get("hangup_cause", "")
    total_duration_sec = record.get("total_duration_sec", 0) or 0

    # sip_call_status: 通话正常结束为 result 值，如 completed/error/not_answered 等
    sip_call_status = result if result else phone_status
    # sip_response_message: 组合 SIP 状态码和原因
    if sip_code:
        sip_response_message = f"SIP {sip_code}: {hangup_cause}" if hangup_cause else f"SIP {sip_code}"
    elif result:
        sip_response_message = f"Call {result}"
    else:
        sip_response_message = None

    # 更新号码状态、SIP 信息、通话时长
    await db.execute(
        """
        UPDATE lk_task_phones
        SET status = $1,
            sip_call_status = $2,
            sip_response_message = $3,
            last_call_duration_sec = $4,
            updated_at = NOW()
        WHERE task_id = $5 AND last_call_id = $6
        """,
        phone_status,
        sip_call_status,
        sip_response_message,
        total_duration_sec,
        task_id,
        call_id,
    )

    # 更新任务计数
    if phone_status == "completed":
        await db.execute(
            """
            UPDATE lk_tasks
            SET success_count = success_count + 1,
                completed_count = completed_count + 1,
                updated_at = NOW()
            WHERE task_id = $1
            """,
            task_id,
        )
    else:
        await db.execute(
            """
            UPDATE lk_tasks
            SET completed_count = completed_count + 1,
                updated_at = NOW()
            WHERE task_id = $1
            """,
            task_id,
        )

    logger.info(
        f"号码状态已同步: task_id={task_id}, call_id={call_id}, "
        f"phone_status={phone_status}, sip_call_status={sip_call_status}, "
        f"duration={total_duration_sec}s"
    )


def _row_to_dict(row: Any) -> Dict[str, Any]:
    """将 asyncpg Record 转为 dict"""
    if row is None:
        return {}
    return dict(row)
