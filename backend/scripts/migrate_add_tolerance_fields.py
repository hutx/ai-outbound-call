"""
数据库迁移脚本：为 call_scripts 表新增 tolerance_enabled 和 tolerance_ms 字段
运行方式: python -m backend.scripts.migrate_add_tolerance_fields
"""
import asyncio
import logging

from sqlalchemy import text
from backend.utils.session_manager import session_manager

logger = logging.getLogger(__name__)


async def migrate():
    async with session_manager.get_session() as session:
        try:
            # 检查字段是否已存在
            result = await session.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'call_scripts' AND column_name = 'tolerance_enabled'"
            ))
            if result.fetchone():
                logger.info("tolerance_enabled 字段已存在，跳过")
            else:
                await session.execute(text(
                    "ALTER TABLE call_scripts ADD COLUMN tolerance_enabled BOOLEAN NOT NULL DEFAULT TRUE"
                ))
                logger.info("tolerance_enabled 字段已添加")

            result = await session.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'call_scripts' AND column_name = 'tolerance_ms'"
            ))
            if result.fetchone():
                logger.info("tolerance_ms 字段已存在，跳过")
            else:
                await session.execute(text(
                    "ALTER TABLE call_scripts ADD COLUMN tolerance_ms INTEGER NOT NULL DEFAULT 1000"
                ))
                logger.info("tolerance_ms 字段已添加")

            await session.commit()
            logger.info("宽容时间字段迁移完成")
        except Exception as e:
            logger.error(f"数据库迁移失败: {e}")
            await session.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(migrate())
