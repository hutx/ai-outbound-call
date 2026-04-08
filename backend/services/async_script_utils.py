"""
异步话术脚本服务工具
为AI通话代理提供异步的话术脚本相关功能
"""

import logging
from typing import Dict, Optional

from backend.services.script_service import script_service

logger = logging.getLogger(__name__)


async def get_system_prompt_for_call(script_id: str, customer_info: dict) -> str:
    """
    为通话获取系统提示
    优先从数据库获取，如果失败则使用默认脚本
    """
    script_config = await script_service.get_script(script_id)
    if not script_config:
        logger.warning(f"话术脚本未找到: {script_id}，使用默认话术")
        main_script_text = (
            "【推介产品】默认产品\n【产品介绍】默认产品描述\n【目标客群】默认客户"
        )
        opening_pause = 2000
    else:
        main_script_text = script_config.main_script
        opening_pause = script_config.opening_pause

    customer_name = customer_info.get("name", "您")
    customer_note = customer_info.get("note", "")

    opening_pause_desc = f"（停顿{opening_pause}毫秒后继续）"

    return f"""
{main_script_text}

【输出格式 - 严格遵守】
你必须返回合法的 JSON，格式如下：
{{
  "reply": "你要说的话（纯文字，不含标点以外的 special 字符）",
  "intent": "用户意图：interested|need_more_info|not_interested|busy|request_human|callback|unknown",
  "action": "下一步动作：continue|transfer|end|send_sms",
  "action_params": {{}}
}}

不要在 JSON 外面加任何文字或 markdown 代码块。"""


async def get_opening_for_call(script_id: str, customer_info: dict) -> dict:
    """
    为通话获取开场白
    优先从数据库获取，如果失败则使用默认开场白
    """
    script_config = await script_service.get_script(script_id)
    if not script_config:
        logger.warning(f"话术脚本未找到: {script_id}，使用默认开场白")
        # 返回默认开场白
        customer_name = customer_info.get("name", "")
        name_part = f"{customer_name}您好" if customer_name else "您好"
        opening_text = f"{name_part}，我是XX银行的智能客服小智，本通话由人工智能完成，请放心。请问您现在方便说话吗？"
        pause_time = 2000  # 默认2秒
        opening_barge_in = False
        protect_start = 3
        protect_end = 3
    else:
        # 使用数据库中的开场白
        opening_text = script_config.opening_script
        pause_time = script_config.opening_pause
        opening_barge_in = script_config.opening_barge_in
        protect_start = script_config.barge_in_protect_start
        protect_end = script_config.barge_in_protect_end

    return {
        "reply": opening_text,
        "pause_ms": pause_time,  # 停顿时长
        "intent": "unknown",
        "action": "continue",
        "action_params": {},
        # 打断策略配置
        "barge_in_enabled": opening_barge_in,
        "protect_start_sec": protect_start,
        "protect_end_sec": protect_end,
    }


async def get_barge_in_config(
    script_id: str, speech_type: str = "conversation"
) -> dict:
    """
    根据语音类型获取打断策略配置

    Args:
        script_id: 话术脚本ID
        speech_type: 语音类型 ("opening" / "closing" / "conversation")

    Returns:
        dict with keys: barge_in_enabled, protect_start_sec, protect_end_sec
    """
    script_config = await script_service.get_script(script_id)
    if not script_config:
        return {
            "barge_in_enabled": (
                False if speech_type in ("opening", "closing") else True
            ),
            "protect_start_sec": 3,
            "protect_end_sec": 3,
        }

    if speech_type == "opening":
        return {
            "barge_in_enabled": script_config.opening_barge_in,
            "protect_start_sec": script_config.barge_in_protect_start,
            "protect_end_sec": script_config.barge_in_protect_end,
        }
    elif speech_type == "closing":
        return {
            "barge_in_enabled": script_config.closing_barge_in,
            "protect_start_sec": script_config.barge_in_protect_start,
            "protect_end_sec": script_config.barge_in_protect_end,
        }
    else:  # conversation
        return {
            "barge_in_enabled": script_config.conversation_barge_in,
            "protect_start_sec": script_config.barge_in_protect_start,
            "protect_end_sec": script_config.barge_in_protect_end,
        }
