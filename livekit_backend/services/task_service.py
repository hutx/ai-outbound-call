"""任务调度与执行服务

管理外呼任务的创建、启动、暂停、取消，以及后台并发调度逻辑。
"""
import asyncio
import logging
import uuid
from typing import Optional, List, Dict, Any

from livekit.api.twirp_client import TwirpError

from livekit_backend.core.config import settings
from livekit_backend.core.events import TaskStatus, PhoneStatus, CallState, CallResult
from livekit_backend.models.task import TaskCreate, TaskResponse, TaskStats, TaskDetail
from livekit_backend.models.call_record import CallRecordUpdate
from livekit_backend.services.sip_service import SipService, SipCallError
from livekit_backend.services import call_record_service
from livekit_backend.utils import db

logger = logging.getLogger(__name__)


# ── 运行时状态（单进程内有效） ──────────────────────────────────────

_running_tasks: Dict[str, asyncio.Task] = {}     # task_id -> asyncio.Task
_pause_events: Dict[str, asyncio.Event] = {}     # task_id -> pause 控制
_cancel_flags: Dict[str, bool] = {}              # task_id -> 取消标记
_concurrent_semaphores: Dict[str, asyncio.Semaphore] = {}  # task_id -> 并发信号量

sip_service = SipService()


# ── 任务 CRUD ────────────────────────────────────────────────────────

async def create_task(data: TaskCreate) -> Dict[str, Any]:
    """创建任务 + 批量插入号码

    Args:
        data: 任务创建请求

    Returns:
        创建后的任务信息
    """
    task_id = f"task_{uuid.uuid4().hex[:12]}"

    async with (await db.get_pool()).acquire() as conn:
        async with conn.transaction():
            # 插入任务
            row = await conn.fetchrow(
                """
                INSERT INTO lk_tasks
                    (task_id, name, script_id, status, concurrent_limit, max_retries, total_phones)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                task_id,
                data.name,
                data.script_id,
                TaskStatus.PENDING.value,
                data.concurrent_limit,
                data.max_retries,
                len(data.phone_numbers),
            )

            # 批量插入号码
            if data.phone_numbers:
                await conn.executemany(
                    """
                    INSERT INTO lk_task_phones (task_id, phone, status)
                    VALUES ($1, $2, $3)
                    """,
                    [
                        (task_id, phone, PhoneStatus.PENDING.value)
                        for phone in data.phone_numbers
                    ],
                )

    logger.info(f"任务已创建: task_id={task_id}, phones={len(data.phone_numbers)}")
    return _row_to_dict(row)


async def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """获取任务基本信息"""
    row = await db.fetchrow(
        "SELECT * FROM lk_tasks WHERE task_id = $1", task_id
    )
    return _row_to_dict(row) if row else None


async def get_tasks(status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """获取任务列表"""
    if status_filter:
        rows = await db.fetch(
            "SELECT * FROM lk_tasks WHERE status = $1 ORDER BY created_at DESC",
            status_filter,
        )
    else:
        rows = await db.fetch(
            "SELECT * FROM lk_tasks ORDER BY created_at DESC"
        )
    return [_row_to_dict(r) for r in rows]


async def get_task_stats(task_id: str) -> Optional[TaskStats]:
    """获取任务统计信息"""
    # 号码统计
    phone_stats = await db.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status = 'pending') AS pending,
            COUNT(*) FILTER (WHERE status = 'calling') AS calling,
            COUNT(*) FILTER (WHERE status = 'completed') AS completed,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed
        FROM lk_task_phones
        WHERE task_id = $1
        """,
        task_id,
    )
    if not phone_stats:
        return None

    total = phone_stats["total"]
    completed = phone_stats["completed"]
    failed = phone_stats["failed"]

    # 成功率 = completed 中成功的比例（用任务 success_count / total_phones 近似）
    task_row = await db.fetchrow(
        "SELECT success_count, total_phones FROM lk_tasks WHERE task_id = $1",
        task_id,
    )
    success_rate = 0.0
    if task_row and task_row["total_phones"] > 0:
        success_rate = round(
            task_row["success_count"] / task_row["total_phones"] * 100, 2
        )

    # 平均通话时长
    avg_row = await db.fetchrow(
        """
        SELECT COALESCE(AVG(duration_sec), 0) AS avg_duration
        FROM lk_call_records
        WHERE task_id = $1 AND duration_sec > 0
        """,
        task_id,
    )
    avg_duration = float(avg_row["avg_duration"]) if avg_row else 0.0

    return TaskStats(
        task_id=task_id,
        total=total,
        pending=phone_stats["pending"],
        calling=phone_stats["calling"],
        completed=completed,
        failed=failed,
        success_rate=success_rate,
        avg_duration_sec=round(avg_duration, 2),
    )


async def get_task_detail(task_id: str) -> Optional[TaskDetail]:
    """获取任务详情（基本信息 + 统计）"""
    task = await get_task(task_id)
    if not task:
        return None

    stats = await get_task_stats(task_id)
    return TaskDetail(
        id=task["id"],
        task_id=task["task_id"],
        name=task["name"],
        script_id=task["script_id"],
        status=task["status"],
        concurrent_limit=task["concurrent_limit"],
        max_retries=task["max_retries"],
        total_phones=task["total_phones"],
        completed_count=task["completed_count"],
        success_count=task["success_count"],
        failed_count=task["failed_count"],
        created_at=task["created_at"],
        updated_at=task["updated_at"],
        stats=stats,
    )


# ── 任务控制 ─────────────────────────────────────────────────────────

async def start_task(task_id: str) -> Dict[str, Any]:
    """启动任务

    将状态改为 running，并启动后台调度协程。
    如果任务已在运行则跳过。
    """
    task = await get_task(task_id)
    if not task:
        raise ValueError(f"任务不存在: {task_id}")

    if task["status"] == TaskStatus.RUNNING.value:
        return task  # 已在运行

    if task["status"] not in (TaskStatus.PENDING.value, TaskStatus.PAUSED.value):
        raise ValueError(f"任务状态不允许启动: {task['status']}")

    # 更新状态
    await db.execute(
        "UPDATE lk_tasks SET status = $1, updated_at = NOW() WHERE task_id = $2",
        TaskStatus.RUNNING.value,
        task_id,
    )

    # 初始化运行时控制
    _pause_events[task_id] = asyncio.Event()
    _pause_events[task_id].set()  # 初始不暂停
    _cancel_flags[task_id] = False
    _concurrent_semaphores[task_id] = asyncio.Semaphore(task["concurrent_limit"])

    # 启动后台调度协程
    at = asyncio.create_task(_dispatch_loop(task_id), name=f"task_{task_id}")
    _running_tasks[task_id] = at

    logger.info(f"任务已启动: {task_id}")
    return await get_task(task_id)  # type: ignore


async def pause_task(task_id: str) -> Dict[str, Any]:
    """暂停任务"""
    task = await get_task(task_id)
    if not task:
        raise ValueError(f"任务不存在: {task_id}")

    if task["status"] != TaskStatus.RUNNING.value:
        raise ValueError(f"只有运行中的任务可以暂停: {task['status']}")

    # 暂停调度
    if task_id in _pause_events:
        _pause_events[task_id].clear()

    await db.execute(
        "UPDATE lk_tasks SET status = $1, updated_at = NOW() WHERE task_id = $2",
        TaskStatus.PAUSED.value,
        task_id,
    )
    logger.info(f"任务已暂停: {task_id}")
    return await get_task(task_id)  # type: ignore


async def cancel_task(task_id: str) -> Dict[str, Any]:
    """取消任务"""
    task = await get_task(task_id)
    if not task:
        raise ValueError(f"任务不存在: {task_id}")

    if task["status"] in (TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value):
        raise ValueError(f"任务已结束，无法取消: {task['status']}")

    # 设置取消标记
    _cancel_flags[task_id] = True

    # 恢复暂停（让调度循环退出）
    if task_id in _pause_events:
        _pause_events[task_id].set()

    await db.execute(
        "UPDATE lk_tasks SET status = $1, updated_at = NOW() WHERE task_id = $2",
        TaskStatus.CANCELLED.value,
        task_id,
    )

    # 将仍在 calling 的号码回退为 pending
    await db.execute(
        """
        UPDATE lk_task_phones
        SET status = $1, updated_at = NOW()
        WHERE task_id = $2 AND status = $3
        """,
        PhoneStatus.PENDING.value,
        task_id,
        PhoneStatus.CALLING.value,
    )

    _cleanup_runtime(task_id)
    logger.info(f"任务已取消: {task_id}")
    return await get_task(task_id)  # type: ignore


# ── 后台调度逻辑 ─────────────────────────────────────────────────────

async def _dispatch_loop(task_id: str) -> None:
    """后台调度循环

    从 lk_task_phones 取 pending 号码，并发发起外呼。
    使用 asyncio.Semaphore 控制并发数。
    通过 asyncio.Event 控制暂停/恢复。
    """
    logger.info(f"调度循环启动: {task_id}")

    try:
        # 确保 SIP 服务已初始化
        if not sip_service._client:
            await sip_service.initialize()

        pending_calls: List[asyncio.Task] = []

        while not _cancel_flags.get(task_id, False):
            # 等待暂停恢复
            pause_event = _pause_events.get(task_id)
            if pause_event and not pause_event.is_set():
                await pause_event.wait()
                if _cancel_flags.get(task_id, False):
                    break

            # 获取下一个待拨打号码
            phone_row = await db.fetchrow(
                """
                SELECT id, phone, retry_count
                FROM lk_task_phones
                WHERE task_id = $1 AND status = $2
                ORDER BY id
                LIMIT 1
                """,
                task_id,
                PhoneStatus.PENDING.value,
            )

            if not phone_row:
                # 没有待拨打号码，检查是否还有进行中的通话
                active_count = await db.fetchval(
                    """
                    SELECT COUNT(*) FROM lk_task_phones
                    WHERE task_id = $1 AND status = $2
                    """,
                    task_id,
                    PhoneStatus.CALLING.value,
                )
                if active_count == 0:
                    logger.info(f"所有号码已处理完成: {task_id}")
                    break
                # 等待进行中的通话完成
                await asyncio.sleep(2)
                continue

            sem = _concurrent_semaphores.get(task_id)
            if not sem:
                break

            # 等待并发信号量
            await sem.acquire()

            # 再次检查取消标记
            if _cancel_flags.get(task_id, False):
                sem.release()
                break

            phone = phone_row["phone"]
            phone_id = phone_row["id"]

            # 标记号码为 calling
            await db.execute(
                """
                UPDATE lk_task_phones
                SET status = $1, updated_at = NOW()
                WHERE id = $2
                """,
                PhoneStatus.CALLING.value,
                phone_id,
            )

            # 启动外呼协程
            call_task = asyncio.create_task(
                _make_call(task_id, phone, phone_id, sem),
                name=f"call_{task_id}_{phone}",
            )
            pending_calls.append(call_task)

            # 短暂间隔避免过快提交
            await asyncio.sleep(0.3)

        # 等待所有进行中的外呼完成
        if pending_calls:
            await asyncio.gather(*pending_calls, return_exceptions=True)

        # 检查是否所有号码都已完成，自动标记任务完成
        await _check_task_completion(task_id)

    except Exception as e:
        logger.error(f"调度循环异常: task_id={task_id}, error={e}", exc_info=True)
    finally:
        _cleanup_runtime(task_id)
        logger.info(f"调度循环结束: {task_id}")


async def _make_call(
    task_id: str,
    phone: str,
    phone_id: int,
    sem: asyncio.Semaphore,
) -> None:
    """执行单次外呼"""
    task = await get_task(task_id)
    if not task:
        sem.release()
        return

    script_id = task["script_id"]
    max_retries = task["max_retries"]

    try:
        # 发起 SIP 外呼
        call_id = None  # 预初始化，防止异常时 NameError
        result = await sip_service.create_outbound_call(
            phone=phone,
            script_id=script_id,
            task_id=task_id,
        )
        call_id = result["call_id"]

        # 更新号码的最后呼叫ID 和 SIP 状态
        await db.execute(
            """
            UPDATE lk_task_phones
            SET last_call_id = $1, sip_call_status = $2, sip_response_message = $3, updated_at = NOW()
            WHERE id = $4
            """,
            call_id,
            "success",
            "SIP 200: Call Answered",
            phone_id,
        )

        logger.info(f"外呼发起成功: task_id={task_id}, phone={phone}, call_id={call_id}")

        # 等待通话完成（通过简单超时机制，实际由 Agent 回调更新状态）
        # 通话结束由 call_record_service 更新号码状态
        # 轮询等待通话结束（每 5 秒检查一次 CDR 状态），最长等待 call_timeout_sec
        poll_interval = 5
        elapsed = 0
        timeout = settings.call_timeout_sec
        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                cdr_row = await db.fetchrow(
                    "SELECT status FROM lk_call_records WHERE call_id = $1", call_id
                )
                if cdr_row and cdr_row["status"] == "ended":
                    logger.info(f"CDR 已结束，提前退出等待: call_id={call_id}, elapsed={elapsed}s")
                    break
            except Exception:
                pass

        # 安全网：sleep 结束后检查号码状态是否仍为 calling
        # 如果 Agent 的 CDR 回调已触发 _sync_phone_status，status 应已更新
        # 否则说明回调失败，需要手动兜底
        try:
            phone_row = await db.fetchrow(
                "SELECT status FROM lk_task_phones WHERE id = $1", phone_id
            )
            if phone_row and phone_row["status"] == PhoneStatus.CALLING.value:
                logger.warning(
                    f"安全网触发：通话超时后号码仍为 calling，手动更新: "
                    f"phone={phone}, phone_id={phone_id}, call_id={call_id}"
                )
                # 查询 CDR 获取实际通话结果
                cdr = await db.fetchrow(
                    "SELECT status, result, total_duration_sec, sip_code, hangup_cause FROM lk_call_records WHERE call_id = $1",
                    call_id,
                )
                if cdr and cdr["status"] == "ended":
                    cdr_result = cdr["result"] or ""
                    success_results = {"completed", "transferred"}
                    final_status = "completed" if cdr_result in success_results else "failed"
                    duration = cdr["total_duration_sec"] or 0
                    sip_code_val = cdr["sip_code"]
                    hangup_val = cdr["hangup_cause"] or ""
                    sip_msg = f"SIP {sip_code_val}: {hangup_val}" if sip_code_val else f"Call {cdr_result}"
                    await db.execute(
                        """
                        UPDATE lk_task_phones
                        SET status = $1, sip_call_status = $2, sip_response_message = $3,
                            last_call_duration_sec = $4, updated_at = NOW()
                        WHERE id = $5
                        """,
                        final_status, cdr_result, sip_msg, duration, phone_id,
                    )
                else:
                    # CDR 也没有结束记录，标记为 failed
                    await db.execute(
                        """
                        UPDATE lk_task_phones
                        SET status = $1, sip_call_status = $2, sip_response_message = $3, updated_at = NOW()
                        WHERE id = $4
                        """,
                        PhoneStatus.FAILED.value, "timeout", "Call timeout (no CDR update)", phone_id,
                    )
        except Exception as safety_err:
            logger.warning(f"安全网更新失败: {safety_err}")

    except Exception as e:
        logger.error(f"外呼失败: task_id={task_id}, phone={phone}, error={e}")

        sip_code = None
        hangup_cause = None
        call_result = CallResult.ERROR

        # 优先处理 SipCallError（由 sip_service 封装，携带 call_id 和 SIP 详情）
        if isinstance(e, SipCallError):
            call_id = e.call_id
            sip_code = e.sip_code
            hangup_cause = e.hangup_cause
        elif isinstance(e, TwirpError):
            meta = e.metadata or {}
            sip_status_code_str = meta.get("sip_status_code", "")
            sip_status_text = meta.get("sip_status", "")
            if sip_status_code_str:
                try:
                    sip_code = int(sip_status_code_str)
                except ValueError:
                    pass
            if sip_status_text:
                hangup_cause = sip_status_text[:64]

        # SIP 状态码 → CallResult 映射
        if sip_code == 480:
            call_result = CallResult.NOT_ANSWERED
            if not hangup_cause:
                hangup_cause = "Temporarily Unavailable"
        elif sip_code == 486:
            call_result = CallResult.BUSY
            if not hangup_cause:
                hangup_cause = "Busy Here"
        elif sip_code == 408:
            call_result = CallResult.TIMEOUT
            if not hangup_cause:
                hangup_cause = "Request Timeout"
        elif sip_code == 603:
            call_result = CallResult.REJECTED
            if not hangup_cause:
                hangup_cause = "Decline"

        logger.info(
            f"SIP 外呼失败详情: phone={phone}, sip_code={sip_code}, "
            f"hangup_cause={hangup_cause}, result={call_result.value}"
        )

        # 构建 sip_call_status 和 sip_response_message
        sip_call_status = call_result.value  # 如 'not_answered', 'busy', 'rejected', 'timeout', 'error'
        sip_response_message = f"SIP {sip_code}: {hangup_cause}" if sip_code else str(e)[:500]

        # 更新 CDR：保存 SIP 状态码和挂断原因
        try:
            if call_id:
                await call_record_service.update_record(
                    call_id,
                    CallRecordUpdate(
                        status=CallState.ENDED,
                        result=call_result,
                        sip_code=sip_code,
                        hangup_cause=hangup_cause,
                    ),
                )
        except Exception as cdr_err:
            logger.warning(f"更新外呼失败 CDR 失败: {cdr_err}")

        # 设置 last_call_id（便于后续 _sync_phone_status 匹配）
        if call_id:
            try:
                await db.execute(
                    "UPDATE lk_task_phones SET last_call_id = $1 WHERE id = $2",
                    call_id,
                    phone_id,
                )
            except Exception:
                pass

        # 获取当前重试次数
        row = await db.fetchrow(
            "SELECT retry_count FROM lk_task_phones WHERE id = $1",
            phone_id,
        )
        retry_count = row["retry_count"] if row else 0

        logger.info(
            f"准备更新号码状态: phone_id={phone_id}, retry_count={retry_count}, "
            f"max_retries={max_retries}, sip_call_status={sip_call_status}, "
            f"sip_response_message={sip_response_message}"
        )

        if retry_count < max_retries:
            # 重试：状态回退为 pending
            await db.execute(
                """
                UPDATE lk_task_phones
                SET status = $1, retry_count = $2, last_error = $3,
                    sip_call_status = $4, sip_response_message = $5, updated_at = NOW()
                WHERE id = $6
                """,
                PhoneStatus.PENDING.value,
                retry_count + 1,
                str(e)[:500],
                sip_call_status,
                sip_response_message,
                phone_id,
            )
            logger.info(f"号码将重试: phone={phone}, retry={retry_count + 1}/{max_retries}")
            logger.info(f"号码状态已更新(重试): phone_id={phone_id}, status=pending, sip_call_status={sip_call_status}")
        else:
            # 超过重试次数，标记失败
            await db.execute(
                """
                UPDATE lk_task_phones
                SET status = $1, last_error = $2,
                    sip_call_status = $3, sip_response_message = $4, updated_at = NOW()
                WHERE id = $5
                """,
                PhoneStatus.FAILED.value,
                str(e)[:500],
                sip_call_status,
                sip_response_message,
                phone_id,
            )
            # 更新任务失败计数
            await db.execute(
                """
                UPDATE lk_tasks
                SET failed_count = failed_count + 1, updated_at = NOW()
                WHERE task_id = $1
                """,
                task_id,
            )
            logger.info(f"号码状态已更新(失败): phone_id={phone_id}, status=failed, sip_call_status={sip_call_status}")

    finally:
        sem.release()


async def _check_task_completion(task_id: str) -> None:
    """检查任务是否所有号码都已完成，自动标记为 completed"""
    pending_count = await db.fetchval(
        """
        SELECT COUNT(*) FROM lk_task_phones
        WHERE task_id = $1 AND status IN ('pending', 'calling')
        """,
        task_id,
    )
    if pending_count == 0:
        # 更新已完成计数
        completed = await db.fetchval(
            """
            SELECT COUNT(*) FROM lk_task_phones
            WHERE task_id = $1 AND status = 'completed'
            """,
            task_id,
        )
        await db.execute(
            """
            UPDATE lk_tasks
            SET status = $1, completed_count = $2, updated_at = NOW()
            WHERE task_id = $3
            """,
            TaskStatus.COMPLETED.value,
            completed,
            task_id,
        )
        logger.info(f"任务已完成: {task_id}")


def _cleanup_runtime(task_id: str) -> None:
    """清理运行时状态"""
    _running_tasks.pop(task_id, None)
    _pause_events.pop(task_id, None)
    _cancel_flags.pop(task_id, None)
    _concurrent_semaphores.pop(task_id, None)


# ── 工具函数 ─────────────────────────────────────────────────────────

def _row_to_dict(row: Any) -> Dict[str, Any]:
    """将 asyncpg Record 转为 dict"""
    if row is None:
        return {}
    return dict(row)
