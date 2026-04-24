"""话术管理服务

负责话术的 CRUD 操作，使用 asyncpg 访问 lk_scripts 表，
并提供带 TTL 的内存缓存。
"""
import logging
import time
from typing import Optional

from backend.utils.db import fetch, fetchrow, execute
from backend.models.script import ScriptCreate, ScriptUpdate

logger = logging.getLogger(__name__)

# lk_scripts 表所有列（与 init.sql 一致），用于 SELECT / INSERT
_COLUMNS = (
    "id, script_id, name, description, script_type, "
    "opening_text, opening_pause_ms, main_prompt, closing_text, "
    "barge_in_opening, barge_in_conversation, barge_in_closing, "
    "barge_in_protect_start_sec, barge_in_protect_end_sec, "
    "tolerance_enabled, tolerance_ms, "
    "no_response_timeout_sec, no_response_mode, no_response_max_count, "
    "no_response_prompt, no_response_hangup_text, "
    "is_active, created_at, updated_at"
)

# 缓存默认 TTL（秒）
_CACHE_TTL = 60


def _record_to_dict(record) -> dict:
    """将 asyncpg Record 转换为字典，并确保所有字段都包含在内。"""
    if record is None:
        return None
    return dict(record)


class ScriptService:
    """话术管理服务

    提供 CRUD 操作，内置带 TTL 的内存缓存。
    """

    def __init__(self, cache_ttl: int = _CACHE_TTL) -> None:
        self._cache: dict[str, dict] = {}
        self._cache_ts: dict[str, float] = {}
        self._cache_ttl = cache_ttl

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------

    def _is_cache_valid(self, script_id: str) -> bool:
        """检查缓存是否在 TTL 内。"""
        ts = self._cache_ts.get(script_id)
        if ts is None:
            return False
        return (time.monotonic() - ts) < self._cache_ttl

    def _set_cache(self, script_id: str, data: dict) -> None:
        """写入缓存。"""
        self._cache[script_id] = data
        self._cache_ts[script_id] = time.monotonic()

    def _invalidate(self, script_id: str) -> None:
        """清除指定话术缓存。"""
        self._cache.pop(script_id, None)
        self._cache_ts.pop(script_id, None)

    def clear_cache(self) -> None:
        """清除全部缓存。"""
        self._cache.clear()
        self._cache_ts.clear()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def get_script(self, script_id: str) -> Optional[dict]:
        """获取指定话术。

        先查缓存（TTL 内有效），未命中则查数据库。

        Args:
            script_id: 话术唯一标识

        Returns:
            话术字典，不存在返回 None
        """
        # 缓存命中
        if self._is_cache_valid(script_id):
            return self._cache[script_id]

        # 查询数据库
        row = await fetchrow(
            f"SELECT {_COLUMNS} FROM lk_scripts WHERE script_id = $1",
            script_id,
        )
        if row is None:
            logger.warning("话术未找到: %s", script_id)
            return None

        data = _record_to_dict(row)
        self._set_cache(script_id, data)
        return data

    async def get_all_scripts(self, active_only: bool = True) -> list[dict]:
        """获取话术列表。

        Args:
            active_only: 是否只返回活跃话术（is_active=true）

        Returns:
            话术字典列表
        """
        if active_only:
            rows = await fetch(
                f"SELECT {_COLUMNS} FROM lk_scripts WHERE is_active = true ORDER BY id"
            )
        else:
            rows = await fetch(
                f"SELECT {_COLUMNS} FROM lk_scripts ORDER BY id"
            )
        return [_record_to_dict(r) for r in rows]

    async def create_script(self, data: ScriptCreate) -> dict:
        """创建话术。

        Args:
            data: 创建请求模型

        Returns:
            创建后的完整话术字典

        Raises:
            ValueError: script_id 已存在
        """
        # 检查 script_id 是否冲突
        existing = await fetchrow(
            "SELECT script_id FROM lk_scripts WHERE script_id = $1",
            data.script_id,
        )
        if existing is not None:
            raise ValueError(f"话术标识已存在: {data.script_id}")

        # 插入
        await execute(
            """
            INSERT INTO lk_scripts (
                script_id, name, description, script_type,
                opening_text, opening_pause_ms, main_prompt, closing_text,
                barge_in_opening, barge_in_conversation, barge_in_closing,
                barge_in_protect_start_sec, barge_in_protect_end_sec,
                tolerance_enabled, tolerance_ms,
                no_response_timeout_sec, no_response_mode, no_response_max_count,
                no_response_prompt, no_response_hangup_text
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8,
                $9, $10, $11,
                $12, $13,
                $14, $15,
                $16, $17, $18,
                $19, $20
            )
            """,
            data.script_id,
            data.name,
            data.description,
            data.script_type,
            data.opening_text,
            data.opening_pause_ms,
            data.main_prompt,
            data.closing_text,
            data.barge_in_opening,
            data.barge_in_conversation,
            data.barge_in_closing,
            data.barge_in_protect_start_sec,
            data.barge_in_protect_end_sec,
            data.tolerance_enabled,
            data.tolerance_ms,
            data.no_response_timeout_sec,
            data.no_response_mode,
            data.no_response_max_count,
            data.no_response_prompt,
            data.no_response_hangup_text,
        )

        # 查询完整记录并写入缓存
        created = await self.get_script(data.script_id)
        logger.info("话术已创建: %s", data.script_id)
        return created  # type: ignore[return-value]

    async def update_script(self, script_id: str, data: ScriptUpdate) -> Optional[dict]:
        """更新话术。

        仅更新请求体中非 None 的字段。

        Args:
            script_id: 话术唯一标识
            data: 更新请求模型

        Returns:
            更新后的完整话术字典，话术不存在返回 None
        """
        # 检查话术是否存在
        existing = await fetchrow(
            "SELECT script_id FROM lk_scripts WHERE script_id = $1",
            script_id,
        )
        if existing is None:
            return None

        # 构建动态 SET 子句
        update_fields = data.model_dump(exclude_none=True)
        if not update_fields:
            # 没有需要更新的字段，直接返回当前数据
            return await self.get_script(script_id)

        set_clauses: list[str] = []
        args: list = []
        idx = 1
        for col, val in update_fields.items():
            set_clauses.append(f"{col} = ${idx}")
            args.append(val)
            idx += 1

        # updated_at 直接写在 SQL 中，不作为参数传入
        set_clauses.append("updated_at = NOW()")

        args.append(script_id)
        sql = (
            f"UPDATE lk_scripts SET {', '.join(set_clauses)} "
            f"WHERE script_id = ${idx}"
        )
        await execute(sql, *args)

        # 清除缓存并重新加载
        self._invalidate(script_id)
        updated = await self.get_script(script_id)
        logger.info("话术已更新: %s", script_id)
        return updated

    async def delete_script(self, script_id: str) -> bool:
        """软删除话术（设置 is_active=false）。

        Args:
            script_id: 话术唯一标识

        Returns:
            是否删除成功（话术不存在返回 False）
        """
        result = await execute(
            """
            UPDATE lk_scripts
            SET is_active = false, updated_at = NOW()
            WHERE script_id = $1
            """,
            script_id,
        )
        # asyncpg execute 返回类似 "UPDATE 1" 的字符串
        affected = int(result.split()[-1]) if result else 0
        if affected == 0:
            logger.warning("软删除话术未命中: %s", script_id)
            return False

        # 清除缓存
        self._invalidate(script_id)
        logger.info("话术已软删除: %s", script_id)
        return True


# 全局话术服务实例
script_service = ScriptService()

