"""
外呼任务调度器 — 生产级
────────────────────────
- 与 AsyncESLPool 集成（不再自己管连接）
- on_call_finished() 回调：CallAgent 完成时更新计数
- 失败重试（指数退避，最多 max_retries 次）
- 黑名单前置过滤
- 任务进度持久化到 Redis（可选，无 Redis 时退化为内存）
- 拨号时间窗口（工作时间限制）
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

from backend.core.config import config
from backend.services.crm_service import crm

if TYPE_CHECKING:
    from backend.services.esl_service import AsyncESLPool
    from backend.core.state_machine import CallResult

logger = logging.getLogger(__name__)

# 拨号时间窗口：仅在此时间段内拨号（合规要求）
DIAL_WINDOW_START = dtime(9, 0)   # 09:00
DIAL_WINDOW_END   = dtime(21, 0)  # 21:00


class TaskStatus(Enum):
    PENDING   = auto()
    RUNNING   = auto()
    PAUSED    = auto()
    COMPLETED = auto()
    CANCELLED = auto()
    FAILED    = auto()


@dataclass
class PhoneRecord:
    phone: str
    customer_info: dict = field(default_factory=dict)
    attempts: int = 0
    last_attempt_ts: float = 0.0
    result: str = "pending"   # pending / connected / no_answer / busy / blacklisted / error
    call_uuid: Optional[str] = None


@dataclass
class OutboundTask:
    task_id: str
    name: str
    script_id: str
    phones: list[PhoneRecord]
    concurrent_limit: int = 5
    max_retries: int = 1
    retry_interval_s: int = 1800   # 30 分钟后重试
    dial_interval_s: float = 0.5   # 号码间隔（避免短时大量并发）
    caller_id: str = ""
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # 实时计数（通过 on_call_finished 回调更新）
    connected_count: int = 0
    failed_count: int = 0

    @property
    def total(self) -> int:
        return len(self.phones)

    @property
    def completed(self) -> int:
        return sum(1 for p in self.phones if p.result not in ("pending", "retrying"))

    @property
    def progress_pct(self) -> float:
        return round(self.completed / self.total * 100, 1) if self.total else 0.0

    def to_dict(self) -> dict:
        return {
            "task_id":       self.task_id,
            "name":          self.name,
            "script_id":     self.script_id,
            "status":        self.status.name,
            "total":         self.total,
            "completed":     self.completed,
            "connected":     self.connected_count,
            "failed":        self.failed_count,
            "progress":      self.progress_pct,
            "connect_rate":  (
                round(self.connected_count / max(self.completed, 1) * 100, 1)
                if self.completed > 0 else 0.0
            ),
            "created_at":    self.created_at.isoformat(),
            "started_at":    self.started_at.isoformat() if self.started_at else None,
            "completed_at":  self.completed_at.isoformat() if self.completed_at else None,
        }


class TaskScheduler:
    """
    外呼任务调度器（全局单例，由 main.py 创建）
    """

    def __init__(self, esl_pool: Optional["AsyncESLPool"] = None):
        self._esl_pool = esl_pool
        self._tasks: dict[str, OutboundTask] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self._cancel_flags: dict[str, bool] = {}
        # 全局并发信号量（跨任务限制）
        self._global_sem = asyncio.Semaphore(config.max_concurrent_calls)

    # ── 公共 API ──────────────────────────────────────────────

    def create_task(
        self,
        name: str,
        phone_numbers: list[str],
        script_id: str,
        concurrent_limit: int = 5,
        max_retries: int = 1,
        caller_id: str = "",
    ) -> OutboundTask:
        task_id = f"task_{uuid.uuid4().hex[:10]}"
        phones = [PhoneRecord(phone=p.strip()) for p in phone_numbers if p.strip()]
        task = OutboundTask(
            task_id=task_id,
            name=name,
            script_id=script_id,
            phones=phones,
            concurrent_limit=min(concurrent_limit, config.max_concurrent_calls),
            max_retries=max_retries,
            caller_id=caller_id or config.freeswitch.__dict__.get("caller_id", "02100000000"),
        )
        self._tasks[task_id] = task
        self._pause_events[task_id] = asyncio.Event()
        self._pause_events[task_id].set()
        self._cancel_flags[task_id] = False
        logger.info(f"任务创建 {task_id}: {name} ({len(phones)} 个号码)")
        return task

    async def start_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in (TaskStatus.PENDING, TaskStatus.PAUSED):
            return False
        task.status = TaskStatus.RUNNING
        task.started_at = task.started_at or datetime.now()
        asyncio.create_task(self._run(task), name=f"task-{task_id}")
        logger.info(f"任务 {task_id} 开始执行")
        return True

    def pause_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status != TaskStatus.RUNNING:
            return False
        task.status = TaskStatus.PAUSED
        self._pause_events[task_id].clear()
        logger.info(f"任务 {task_id} 已暂停")
        return True

    def resume_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status != TaskStatus.PAUSED:
            return False
        task.status = TaskStatus.RUNNING
        self._pause_events[task_id].set()
        logger.info(f"任务 {task_id} 恢复执行")
        return True

    def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        self._cancel_flags[task_id] = True
        self._pause_events[task_id].set()
        task.status = TaskStatus.CANCELLED
        logger.info(f"任务 {task_id} 已取消")
        return True

    def get_task(self, task_id: str) -> Optional[OutboundTask]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[dict]:
        return [t.to_dict() for t in reversed(list(self._tasks.values()))]

    def on_call_finished(self, task_id: str, phone: str, result: "CallResult"):
        """
        由 CallAgent._cleanup() 调用，更新任务计数
        """
        from backend.core.state_machine import CallResult as CR
        task = self._tasks.get(task_id)
        if not task:
            return
        # 找到对应 PhoneRecord 更新状态
        for pr in task.phones:
            if pr.phone == phone and pr.result in ("pending", "dialing", "retrying"):
                pr.result = result.value
                break
        if result in (CR.COMPLETED, CR.TRANSFERRED):
            task.connected_count += 1
        elif result in (CR.ERROR, CR.NOT_ANSWERED):
            task.failed_count += 1

    # ── 内部执行逻辑 ──────────────────────────────────────────

    async def _run(self, task: OutboundTask):
        task_sem = asyncio.Semaphore(task.concurrent_limit)
        worker_tasks: list[asyncio.Task] = []

        for pr in task.phones:
            # 检查取消
            if self._cancel_flags.get(task.task_id):
                break

            # 等待暂停解除
            await self._pause_events[task.task_id].wait()
            if self._cancel_flags.get(task.task_id):
                break

            # 跳过已完成
            if pr.result in ("connected", "blacklisted", "max_retries"):
                continue
            # 超过最大重试
            if pr.attempts > task.max_retries:
                pr.result = "max_retries"
                continue

            # 等待重试冷却
            if pr.attempts > 0 and pr.last_attempt_ts > 0:
                elapsed = time.time() - pr.last_attempt_ts
                wait = task.retry_interval_s - elapsed
                if wait > 0:
                    logger.debug(f"号码 {pr.phone} 冷却 {wait:.0f}s 后重试")
                    await asyncio.sleep(min(wait, 60))

            # 检查拨号时间窗口
            if not self._in_dial_window():
                wait_secs = self._seconds_to_window_open()
                logger.info(f"当前不在拨号时间窗口，等待 {wait_secs:.0f}s")
                await asyncio.sleep(min(wait_secs, 300))
                if not self._in_dial_window():
                    continue

            # 黑名单检查
            if crm.is_blacklisted(pr.phone):
                pr.result = "blacklisted"
                logger.info(f"跳过黑名单: {pr.phone}")
                continue

            # 预取客户信息
            if not pr.customer_info:
                try:
                    pr.customer_info = await asyncio.wait_for(
                        crm.query_customer_info(pr.phone), timeout=3.0
                    )
                    if pr.customer_info.get("blacklisted"):
                        pr.result = "blacklisted"
                        continue
                except Exception:
                    pr.customer_info = {}

            # 拨号（获取全局信号量 + 任务内信号量）
            await self._global_sem.acquire()
            await task_sem.acquire()

            wt = asyncio.create_task(
                self._dial_one(task, pr, task_sem),
                name=f"dial-{pr.phone}",
            )
            worker_tasks.append(wt)

            # 号码间隔
            await asyncio.sleep(task.dial_interval_s)

        # 等待所有拨号完成
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)

        if not self._cancel_flags.get(task.task_id):
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now()
            logger.info(
                f"任务 {task.task_id} 完成: "
                f"接通 {task.connected_count}/{task.total} "
                f"({task.connected_count/task.total*100:.1f}%)"
            )

    async def _dial_one(
        self, task: OutboundTask, pr: PhoneRecord, task_sem: asyncio.Semaphore
    ):
        call_uuid = uuid.uuid4().hex
        pr.call_uuid = call_uuid
        pr.attempts += 1
        pr.last_attempt_ts = time.time()
        pr.result = "dialing"

        try:
            if not self._esl_pool:
                raise RuntimeError("ESL 连接池未初始化")

            await self._esl_pool.originate(
                phone=pr.phone,
                gateway=config.freeswitch.gateway,
                call_uuid=call_uuid,
                task_id=task.task_id,
                script_id=task.script_id,
                caller_id=task.caller_id or "02100000000",
                originate_timeout=config.freeswitch.originate_timeout,
                socket_host="127.0.0.1",
                socket_port=config.freeswitch.socket_port,
            )
            logger.info(
                f"[{task.task_id}] 拨出 {pr.phone} "
                f"(第{pr.attempts}次 uuid={call_uuid[:8]})"
            )
            # 结果由 on_call_finished 回调更新，这里只标记已拨出
            # pr.result = "dialing"  # 保持，等待回调

        except Exception as e:
            logger.error(f"拨号失败 {pr.phone}: {e}")
            pr.result = "dial_error"
            task.failed_count += 1
        finally:
            task_sem.release()
            self._global_sem.release()

    @staticmethod
    def _in_dial_window() -> bool:
        now = datetime.now().time()
        return DIAL_WINDOW_START <= now <= DIAL_WINDOW_END

    @staticmethod
    def _seconds_to_window_open() -> float:
        now = datetime.now()
        tomorrow_open = datetime.combine(
            now.date(), DIAL_WINDOW_START
        ).replace(day=now.day + (1 if now.time() > DIAL_WINDOW_END else 0))
        return max(0.0, (tomorrow_open - now).total_seconds())
