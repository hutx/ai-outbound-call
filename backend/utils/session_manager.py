"""
数据库会话管理器
──────────────────────
解决跨事件循环的数据库连接问题
"""
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from sqlalchemy import event

from backend.utils.db import _get_engine

logger = logging.getLogger(__name__)


class SessionManager:
    """数据库会话管理器，支持跨事件循环的安全访问"""

    def __init__(self):
        self._engine = None
        self._session_factory = None

    def get_engine(self):
        """获取引擎实例（线程安全）"""
        if self._engine is None:
            self._engine = _get_engine()
        return self._engine

    def get_session_factory(self):
        """获取会话工厂实例（线程安全）"""
        if self._session_factory is None:
            self._session_factory = async_sessionmaker(
                self.get_engine(),
                class_=AsyncSession,
                expire_on_commit=False,
            )
        return self._session_factory

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """获取数据库会话的上下文管理器"""
        session_factory = self.get_session_factory()
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()


# 全局会话管理器实例
session_manager = SessionManager()


# 简化版本的数据库操作函数
async def get_session() -> AsyncSession:
    """获取数据库会话"""
    async with session_manager.get_session() as session:
        yield session