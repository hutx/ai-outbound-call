"""
话术脚本管理服务
负责从数据库中读取、管理和更新话术脚本
"""
import asyncio
import logging
from typing import Dict, Optional, List
from dataclasses import dataclass
import json

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from backend.utils.session_manager import session_manager
from backend.models.call_script import CallScript

logger = logging.getLogger(__name__)


@dataclass
class ScriptConfig:
    """话术配置项"""
    script_id: str
    name: str
    description: str
    script_type: str
    opening_script: str
    opening_pause: int
    main_script: Dict
    objection_handling: Dict
    closing_script: Optional[str]
    is_active: bool = True


class ScriptService:
    """话术服务类"""

    def __init__(self):
        self._cache: Dict[str, ScriptConfig] = {}
        self._cache_last_update = 0

    async def get_script(self, script_id: str) -> Optional[ScriptConfig]:
        """获取指定话术脚本"""
        # 首先尝试从缓存获取
        if script_id in self._cache:
            return self._cache[script_id]

        # 从数据库获取
        async with session_manager.get_session() as session:
            try:
                stmt = select(CallScript).where(CallScript.script_id == script_id)
                result = await session.execute(stmt)
                db_script = result.scalar_one_or_none()

                if not db_script:
                    logger.warning(f"未找到话术脚本: {script_id}")
                    return None

                script_config = ScriptConfig(
                    script_id=db_script.script_id,
                    name=db_script.name,
                    description=db_script.description,
                    script_type=db_script.script_type,
                    opening_script=db_script.opening_script,
                    opening_pause=db_script.opening_pause,
                    main_script=json.loads(db_script.main_script) if isinstance(db_script.main_script, str) else db_script.main_script,
                    objection_handling=json.loads(db_script.objection_handling) if isinstance(db_script.objection_handling, str) else db_script.objection_handling,
                    closing_script=db_script.closing_script,
                    is_active=db_script.is_active
                )

                # 加入缓存
                self._cache[script_id] = script_config
                return script_config
            except SQLAlchemyError as e:
                logger.error(f"获取话术脚本失败: {e}")
                return None

    async def get_all_scripts(self) -> List[ScriptConfig]:
        """获取所有活跃话术脚本"""
        async with session_manager.get_session() as session:
            try:
                stmt = select(CallScript).where(CallScript.is_active == True)
                result = await session.execute(stmt)
                db_scripts = result.scalars().all()

                scripts = []
                for db_script in db_scripts:
                    script_config = ScriptConfig(
                        script_id=db_script.script_id,
                        name=db_script.name,
                        description=db_script.description,
                        script_type=db_script.script_type,
                        opening_script=db_script.opening_script,
                        opening_pause=db_script.opening_pause,
                        main_script=json.loads(db_script.main_script) if isinstance(db_script.main_script, str) else db_script.main_script,
                        objection_handling=json.loads(db_script.objection_handling) if isinstance(db_script.objection_handling, str) else db_script.objection_handling,
                        closing_script=db_script.closing_script,
                        is_active=db_script.is_active
                    )
                    scripts.append(script_config)

                return scripts
            except SQLAlchemyError as e:
                logger.error(f"获取话术脚本列表失败: {e}")
                return []

    async def update_script(self, script_id: str, **kwargs) -> bool:
        """更新话术脚本"""
        try:
            async with session_manager.get_session() as session:
                update_dict = {k: v for k, v in kwargs.items() if v is not None}

                if update_dict:
                    stmt = update(CallScript).where(CallScript.script_id == script_id).values(**update_dict)
                    result = await session.execute(stmt)

                    if result.rowcount > 0:
                        await session.commit()

                        # 清除缓存
                        if script_id in self._cache:
                            del self._cache[script_id]

                        logger.info(f"话术脚本已更新: {script_id}")
                        return True
                    else:
                        logger.warning(f"未找到要更新的话术脚本: {script_id}")
                        return False
        except SQLAlchemyError as e:
            logger.error(f"更新话术脚本失败: {e}")
            return False

    async def create_script(self, script_config: ScriptConfig) -> bool:
        """创建新的话术脚本"""
        try:
            async with session_manager.get_session() as session:
                # 检查是否已存在
                existing_stmt = select(CallScript).where(CallScript.script_id == script_config.script_id)
                existing_result = await session.execute(existing_stmt)
                existing = existing_result.scalar_one_or_none()

                if existing:
                    logger.warning(f"话术脚本已存在: {script_config.script_id}")
                    return False

                # 准备插入数据
                new_script = CallScript(
                    script_id=script_config.script_id,
                    name=script_config.name,
                    description=script_config.description,
                    script_type=script_config.script_type,
                    opening_script=script_config.opening_script,
                    opening_pause=script_config.opening_pause,
                    main_script=json.dumps(script_config.main_script) if isinstance(script_config.main_script, dict) else script_config.main_script,
                    objection_handling=json.dumps(script_config.objection_handling) if isinstance(script_config.objection_handling, dict) else script_config.objection_handling,
                    closing_script=script_config.closing_script,
                    is_active=script_config.is_active
                )

                session.add(new_script)
                await session.commit()

                # 清除缓存
                if script_config.script_id in self._cache:
                    del self._cache[script_config.script_id]

                logger.info(f"话术脚本已创建: {script_config.script_id}")
                return True
        except SQLAlchemyError as e:
            logger.error(f"创建话术脚本失败: {e}")
            return False

    async def delete_script(self, script_id: str) -> bool:
        """删除话术脚本（软删除，设置is_active为False）"""
        try:
            async with session_manager.get_session() as session:
                stmt = update(CallScript).where(CallScript.script_id == script_id).values(is_active=False)
                result = await session.execute(stmt)

                if result.rowcount > 0:
                    await session.commit()

                    # 清除缓存
                    if script_id in self._cache:
                        del self._cache[script_id]

                    logger.info(f"话术脚本已删除（软删除）: {script_id}")
                    return True
                else:
                    logger.warning(f"未找到要删除的话术脚本: {script_id}")
                    return False
        except SQLAlchemyError as e:
            logger.error(f"删除话术脚本失败: {e}")
            return False

    async def refresh_cache(self):
        """刷新缓存"""
        self._cache.clear()


# 全局话术服务实例
script_service = ScriptService()


async def build_system_prompt_from_db(script_id: str, customer_info: dict) -> str:
    """
    从数据库构建系统Prompt
    """
    script_config = await script_service.get_script(script_id)
    if not script_config:
        logger.warning(f"话术脚本未找到: {script_id}，使用默认话术")
        # 返回默认话术
        script = {
            "product_name": "默认产品",
            "product_desc": "默认产品描述",
            "target_customer": "默认客户",
            "key_selling_points": ["默认卖点1", "默认卖点2"],
        }
        objection_handling = {"默认异议": "默认回应"}
        opening_pause = 2000
        customer_name = customer_info.get("name", "您")
        customer_note = customer_info.get("note", "")
    else:
        script = script_config.main_script
        objection_handling = script_config.objection_handling if isinstance(script_config.objection_handling, dict) else {}
        opening_pause = script_config.opening_pause
        customer_name = customer_info.get("name", "您")
        customer_note = customer_info.get("note", "")

    # 构建异议处理文本
    objections_text = "\n".join([
        f'  - 若客户说"{k}"，回应："{v}"'
        for k, v in objection_handling.items()
    ])

    # 使用从数据库获取的停顿时长
    opening_pause_desc = f"（停顿{opening_pause}毫秒后继续）"

    return f"""你是一名专业的银行智能客服，名叫"小智"。你正在进行一次电话外呼。

【重要合规声明 - 必须遵守】
1. 开场白必须包含："本通话由人工智能完成" 这句话
2. 客户明确拒绝2次后，立即礼貌结束通话，不得再次推销
3. 不得承诺不实收益或夸大宣传
4. 客户要求转人工时，立即响应

{opening_pause_desc}

【本次外呼信息】
- 客户姓名：{customer_name}
- 推介产品：{script["product_name"]}
- 产品介绍：{script["product_desc"]}
- 目标客群：{script["target_customer"]}
- 客户备注：{customer_note or "无"}

【核心卖点（自然融入对话，不要生硬罗列）】
{chr(10).join(f"  • {p}" for p in script["key_selling_points"])}

【异议处理话术】
{objections_text if objections_text else '  （暂无异议处理话术）'}

【对话风格要求】
- 语气亲切自然，像朋友聊天，不要照本宣科
- 每次回复控制在2-3句话内，不要长篇大论
- 多提问、引导客户说话，了解真实需求
- 遇到客户问具体数字，直接给出，不要回避

【输出格式 - 严格遵守】
你必须返回合法的 JSON，格式如下：
{{
  "reply": "你要说的话（纯文字，不含标点以外的 special 字符）",
  "intent": "用户意图：interested|need_more_info|not_interested|busy|request_human|callback|unknown",
  "action": "下一步动作：continue|transfer|end|send_sms",
  "action_params": {{}}
}}

不要在 JSON 外面加任何文字或 markdown 代码块。"""


async def build_opening_from_db(script_id: str, customer_info: dict) -> dict:
    """
    从数据库生成开场白
    """
    script_config = await script_service.get_script(script_id)
    if not script_config:
        logger.warning(f"话术脚本未找到: {script_id}，使用默认开场白")
        # 返回默认开场白
        customer_name = customer_info.get("name", "")
        name_part = f"{customer_name}您好" if customer_name else "您好"
        opening_text = f"{name_part}，我是XX银行的智能客服小智，本通话由人工智能完成，请放心。请问您现在方便说话吗？"
        pause_time = 2000  # 默认2秒
    else:
        # 使用数据库中的开场白
        opening_text = script_config.opening_script
        pause_time = script_config.opening_pause

    return {
        "reply": opening_text,
        "pause_ms": pause_time,  # 停顿时长
        "intent": "unknown",
        "action": "continue",
        "action_params": {},
    }


async def init_scripts_if_empty():
    """
    初始化话术脚本（如果数据库中没有数据）
    """
    scripts = await script_service.get_all_scripts()
    if not scripts:
        logger.info("数据库中没有话术脚本，正在初始化种子数据...")
        try:
            from backend.scripts.seed_scripts import seed_scripts
            results = await seed_scripts()
            for r in results:
                logger.info(f"  {r}")
            logger.info(f"话术脚本初始化完成")
        except Exception as e:
            logger.error(f"话术脚本初始化失败: {e}")
    else:
        logger.info(f"已从数据库加载 {len(scripts)} 个话术脚本")