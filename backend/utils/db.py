"""
数据库操作层 — 生产级
──────────────────────
- 引擎懒初始化：导入时不连接，init_db() 时才建连接（避免启动崩溃）
- save_call_record 用 upsert（ON CONFLICT DO UPDATE），防止重复写入
- list_call_records 返回完整字段（含 task_id、utterances）
- 连接池参数调优（pool_size=10, overflow=20, recycle=3600）
- SQLite 兼容（DATABASE_URL=sqlite+aiosqlite://...，用于测试/开发）
- 增强 PostgreSQL 连接参数以解决认证问题
"""
import logging
from datetime import datetime
from typing import Optional, List
import urllib.parse

from sqlalchemy import String, Integer, DateTime, JSON, text, select, update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.core.config import config

logger = logging.getLogger(__name__)

# ── 懒初始化全局对象 ──────────────────────────────────────────
_engine = None
_session_factory = None


def _get_engine():
    global _engine
    if _engine is None:
        url = config.db.url
        # 自动补充异步驱动前缀
        if url.startswith("postgresql://"):
            # 移除URL中的sslmode参数，因为asyncpg不支持
            parsed = urllib.parse.urlparse(url)
            query_params = urllib.parse.parse_qs(parsed.query)

            # 从查询参数中移除sslmode
            if 'sslmode' in query_params:
                del query_params['sslmode']

            # 重组URL，不包含sslmode
            new_query = urllib.parse.urlencode(query_params, doseq=True)
            parsed_url = parsed._replace(query=new_query)
            url = urllib.parse.urlunparse(parsed_url)

            # 替换驱动
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("sqlite://"):
            url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)

        is_sqlite = "sqlite" in url
        kwargs = {"echo": config.debug}
        if not is_sqlite:
            kwargs.update({
                "pool_size": 10,
                "max_overflow": 20,
                "pool_recycle": 3600,   # 1h 重建连接，防止 MySQL/PG idle 断开
                "pool_pre_ping": True,  # 每次取连接前 ping 一次
                # 添加更多连接参数以解决认证问题
                "connect_args": {
                    "server_settings": {
                        "application_name": "ai-outbound-call-system",
                    }
                }
            })
        _engine = create_async_engine(url, **kwargs)
    return _engine


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            _get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


# ── ORM 模型 ─────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# 导入话术脚本模型
from backend.models.call_script import CallScript

class CallRecord(Base):
    """通话详单（CDR）"""
    __tablename__ = "call_records"

    id:               Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid:             Mapped[str]            = mapped_column(String(64), unique=True, index=True)
    task_id:          Mapped[str]            = mapped_column(String(64), index=True)
    phone_number:     Mapped[str]            = mapped_column(String(20), index=True)
    script_id:        Mapped[str]            = mapped_column(String(64))

    state:            Mapped[str]            = mapped_column(String(20))
    intent:           Mapped[str]            = mapped_column(String(30))
    result:           Mapped[str]            = mapped_column(String(30), index=True)

    # 外呼结果详情（SIP 层面的原始反馈）
    sip_code:         Mapped[Optional[int]]  = mapped_column(Integer, nullable=True, index=True)
    hangup_cause:     Mapped[Optional[str]]  = mapped_column(String(64), nullable=True, index=True)
    dial_attempts:    Mapped[int]            = mapped_column(Integer, default=0)

    created_at:       Mapped[datetime]       = mapped_column(DateTime)
    answered_at:      Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ended_at:         Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)

    user_utterances:  Mapped[int]            = mapped_column(Integer, default=0)
    ai_utterances:    Mapped[int]            = mapped_column(Integer, default=0)
    recording_path:   Mapped[Optional[str]]  = mapped_column(String(512), nullable=True)

    # 对话历史（保留最近 30 条消息）
    messages:         Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    customer_info:    Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class BlacklistRecord(Base):
    """黑名单表（持久化，重启不丢失）"""
    __tablename__ = "blacklist"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone:      Mapped[str] = mapped_column(String(20), unique=True, index=True)
    reason:     Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class CallbackSchedule(Base):
    """回拨计划表"""
    __tablename__ = "callback_schedules"

    id:            Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone:         Mapped[str] = mapped_column(String(20), index=True)
    task_id:       Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    callback_time: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    note:          Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    status:        Mapped[str] = mapped_column(String(20), default="pending")
    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ── 初始化 ───────────────────────────────────────────────────
async def init_db():
    """建表（幂等，已存在则跳过）"""
    try:
        engine = _get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("数据库表初始化完成")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        # 如果 PostgreSQL 连接失败，输出更详细的错误信息
        if "password authentication failed" in str(e).lower():
            logger.error("数据库认证失败，请检查: ")
            logger.error("- 数据库用户名和密码是否正确")
            logger.error("- PostgreSQL 服务是否正在运行")
            logger.error("- 数据库是否已创建")
            logger.error("- 用户是否具有数据库访问权限")
        raise


async def save_call_record(ctx) -> None:
    """
    保存通话记录
    使用 upsert：同一 uuid 再次写入时更新，防止重试导致的重复记录
    """
    from backend.utils.session_manager import session_manager

    try:
        async with session_manager.get_session() as session:
            # 检查是否已存在
            existing = (await session.execute(
                select(CallRecord).where(CallRecord.uuid == ctx.uuid)
            )).scalar_one_or_none()

            if existing:
                # 更新（通话结束时补充完整信息）
                existing.state            = ctx.state.name
                existing.intent           = ctx.intent.value
                existing.result           = ctx.result.value
                existing.ended_at         = ctx.ended_at
                existing.duration_seconds = ctx.duration_seconds
                existing.user_utterances  = ctx.user_utterances
                existing.ai_utterances    = ctx.ai_utterances
                existing.recording_path   = ctx.recording_path
                existing.messages         = ctx.messages[-30:] if ctx.messages else []
                existing.sip_code         = ctx.sip_code
                existing.hangup_cause     = ctx.hangup_cause
                existing.dial_attempts    = ctx.dial_attempts
            else:
                record = CallRecord(
                    uuid             = ctx.uuid,
                    task_id          = ctx.task_id,
                    phone_number     = ctx.phone_number,
                    script_id        = ctx.script_id,
                    state            = ctx.state.name,
                    intent           = ctx.intent.value,
                    result           = ctx.result.value,
                    created_at       = ctx.created_at,
                    answered_at      = ctx.answered_at,
                    ended_at         = ctx.ended_at,
                    duration_seconds = ctx.duration_seconds,
                    user_utterances  = ctx.user_utterances,
                    ai_utterances    = ctx.ai_utterances,
                    recording_path   = ctx.recording_path,
                    messages         = ctx.messages[-30:] if ctx.messages else [],
                    customer_info    = ctx.customer_info or {},
                    sip_code         = ctx.sip_code,
                    hangup_cause     = ctx.hangup_cause,
                    dial_attempts    = ctx.dial_attempts,
                )
                session.add(record)

            await session.commit()
            logger.debug(f"CDR 已写入: {ctx.uuid} result={ctx.result.value}")
    except Exception as e:
        logger.error(f"保存通话记录失败: {e}")
        # 仅记录错误，不要影响通话流程


# ── 通话记录查询 ─────────────────────────────────────────────
async def list_call_records(
    task_id: Optional[str] = None,
    phone: Optional[str] = None,
    result: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[dict]:
    from backend.utils.session_manager import session_manager

    try:
        async with session_manager.get_session() as session:
            q = select(CallRecord).order_by(CallRecord.created_at.desc())
            if task_id:
                q = q.where(CallRecord.task_id == task_id)
            if phone:
                q = q.where(CallRecord.phone_number == phone)
            if result:
                q = q.where(CallRecord.result == result)
            q = q.limit(limit).offset(offset)
            rows = (await session.execute(q)).scalars().all()
            return [_record_to_dict(r) for r in rows]
    except Exception as e:
        logger.error(f"查询通话记录失败: {e}")
        return []


async def get_call_record(uuid: str) -> Optional[dict]:
    from backend.utils.session_manager import session_manager

    try:
        async with session_manager.get_session() as session:
            r = (await session.execute(
                select(CallRecord).where(CallRecord.uuid == uuid)
            )).scalar_one_or_none()
            return _record_to_dict(r) if r else None
    except Exception as e:
        logger.error(f"查询通话记录失败: {e}")
        return None


async def get_call_stats(task_id: Optional[str] = None) -> dict:
    """获取聚合统计（接通率、意向率、平均时长）"""
    from backend.utils.session_manager import session_manager

    try:
        async with session_manager.get_session() as session:
            where = f"WHERE task_id = '{task_id}'" if task_id else ""
            sql = text(f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN answered_at IS NOT NULL THEN 1 ELSE 0 END) AS connected,
                    SUM(CASE WHEN intent IN ('interested','high','medium') THEN 1 ELSE 0 END) AS intent,
                    SUM(CASE WHEN result = 'transferred' THEN 1 ELSE 0 END) AS transferred,
                    AVG(CASE WHEN duration_seconds > 0 THEN duration_seconds END) AS avg_duration
                FROM call_records {where}
            """)
            row = (await session.execute(sql)).fetchone()
            if not row or row[0] == 0:
                return {"total": 0, "connected": 0, "intent": 0, "transferred": 0, "avg_duration": 0,
                        "connect_rate": 0.0, "intent_rate": 0.0}
            total = row[0] or 0
            connected = row[1] or 0
            return {
                "total":        total,
                "connected":    connected,
                "intent":       row[2] or 0,
                "transferred":  row[3] or 0,
                "avg_duration": round(row[4] or 0, 1),
                "connect_rate": round(connected / total * 100, 1) if total else 0.0,
                "intent_rate":  round((row[2] or 0) / max(connected, 1) * 100, 1),
            }
    except Exception as e:
        logger.error(f"查询统计失败: {e}")
        return {"total": 0, "connected": 0, "intent": 0, "transferred": 0, "avg_duration": 0,
                "connect_rate": 0.0, "intent_rate": 0.0}


def _record_to_dict(r: CallRecord) -> dict:
    return {
        "uuid":             r.uuid,
        "task_id":          r.task_id,
        "phone_number":     r.phone_number,
        "script_id":        r.script_id,
        "state":            r.state,
        "intent":           r.intent,
        "result":           r.result,
        "sip_code":         r.sip_code,
        "hangup_cause":     r.hangup_cause,
        "dial_attempts":    r.dial_attempts,
        "created_at":       r.created_at.isoformat() if r.created_at else None,
        "answered_at":      r.answered_at.isoformat() if r.answered_at else None,
        "ended_at":         r.ended_at.isoformat() if r.ended_at else None,
        "duration_seconds": r.duration_seconds,
        "user_utterances":  r.user_utterances,
        "ai_utterances":    r.ai_utterances,
        "recording_path":   r.recording_path,
    }


# ── 黑名单持久化 ─────────────────────────────────────────────
async def db_add_blacklist(phone: str, reason: str = "") -> bool:
    from backend.utils.session_manager import session_manager

    try:
        async with session_manager.get_session() as session:
            existing = (await session.execute(
                select(BlacklistRecord).where(BlacklistRecord.phone == phone)
            )).scalar_one_or_none()
            if not existing:
                session.add(BlacklistRecord(phone=phone, reason=reason))
                await session.commit()
        return True
    except Exception as e:
        logger.error(f"添加黑名单失败: {e}")
        return False


async def db_load_blacklist() -> set:
    """启动时从数据库加载黑名单到内存"""
    from backend.utils.session_manager import session_manager

    try:
        async with session_manager.get_session() as session:
            rows = (await session.execute(select(BlacklistRecord.phone))).scalars().all()
            return set(rows)
    except Exception as e:
        logger.warning(f"加载黑名单失败（数据库未就绪？）: {e}")
        return set()


async def db_remove_blacklist(phone: str) -> bool:
    from backend.utils.session_manager import session_manager

    try:
        async with session_manager.get_session() as session:
            await session.execute(
                text(f"DELETE FROM blacklist WHERE phone = :phone"),
                {"phone": phone}
            )
            await session.commit()
        return True
    except Exception as e:
        logger.error(f"删除黑名单失败: {e}")
        return False


async def db_list_blacklist() -> List[dict]:
    from backend.utils.session_manager import session_manager

    try:
        async with session_manager.get_session() as session:
            rows = (await session.execute(
                select(BlacklistRecord).order_by(BlacklistRecord.created_at.desc())
            )).scalars().all()
            return [{"phone": r.phone, "reason": r.reason,
                     "created_at": r.created_at.isoformat() if r.created_at else None}
                    for r in rows]
    except Exception as e:
        logger.error(f"查询黑名单失败: {e}")
        return []


# ── 回拨计划 ─────────────────────────────────────────────────
async def db_save_callback(phone: str, task_id: str, callback_time: str, note: str = "") -> bool:
    from backend.utils.session_manager import session_manager

    try:
        async with session_manager.get_session() as session:
            session.add(CallbackSchedule(
                phone=phone, task_id=task_id,
                callback_time=callback_time, note=note,
            ))
            await session.commit()
        return True
    except Exception as e:
        logger.error(f"保存回拨计划失败: {e}")
        return False


async def db_list_callbacks(status: str = "pending") -> List[dict]:
    from backend.utils.session_manager import session_manager

    try:
        async with session_manager.get_session() as session:
            rows = (await session.execute(
                select(CallbackSchedule)
                .where(CallbackSchedule.status == status)
                .order_by(CallbackSchedule.callback_time)
            )).scalars().all()
            return [{"id": r.id, "phone": r.phone, "task_id": r.task_id,
                     "callback_time": r.callback_time, "note": r.note}
                    for r in rows]
    except Exception as e:
        logger.error(f"查询回拨计划失败: {e}")
        return []


# ── 关闭数据库连接池 ─────────────────────────────────────────────
async def dispose_db():
    """关闭连接池（优雅退出时调用）"""
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None