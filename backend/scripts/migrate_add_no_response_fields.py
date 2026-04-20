"""
数据库迁移脚本：为 call_scripts 表新增无回应配置字段
运行方式: python -m backend.scripts.migrate_add_no_response_fields
"""
import asyncio
import logging

from sqlalchemy import text
from backend.utils.session_manager import session_manager

logger = logging.getLogger(__name__)

COLUMNS = [
    ("no_response_timeout", "ALTER TABLE call_scripts ADD COLUMN no_response_timeout INTEGER NOT NULL DEFAULT 3"),
    ("no_response_mode", "ALTER TABLE call_scripts ADD COLUMN no_response_mode VARCHAR(20) NOT NULL DEFAULT 'consecutive'"),
    ("no_response_max_count", "ALTER TABLE call_scripts ADD COLUMN no_response_max_count INTEGER NOT NULL DEFAULT 3"),
    ("no_response_hangup_msg", "ALTER TABLE call_scripts ADD COLUMN no_response_hangup_msg TEXT"),
    ("no_response_hangup_enabled", "ALTER TABLE call_scripts ADD COLUMN no_response_hangup_enabled BOOLEAN NOT NULL DEFAULT TRUE"),
]


async def migrate():
    async with session_manager.get_session() as session:
        try:
            for col_name, alter_sql in COLUMNS:
                result = await session.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'call_scripts' AND column_name = :col_name"
                ), {"col_name": col_name})
                if result.fetchone():
                    logger.info(f"{col_name} 字段已存在，跳过")
                else:
                    await session.execute(text(alter_sql))
                    logger.info(f"{col_name} 字段已添加")

            await session.commit()
            logger.info("无回应配置字段迁移完成")
        except Exception as e:
            logger.error(f"数据库迁移失败: {e}")
            await session.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(migrate())
