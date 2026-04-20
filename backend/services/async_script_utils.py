"""
异步话术脚本服务工具
为AI通话代理提供异步的话术脚本相关功能
"""

import logging
from typing import Dict, Optional

from backend.services.script_service import script_service

logger = logging.getLogger(__name__)


ALLOWED_INTENTS = (
    "interested|need_more_info|not_interested|busy|request_human|callback|unknown"
)
ALLOWED_ACTIONS = "continue|transfer|callback|send_sms|blacklist|end"


def _build_customer_profile(customer_info: dict) -> str:
    customer_name = customer_info.get("name", "未知")
    customer_note = customer_info.get("note", "") or "无"
    risk_level = customer_info.get("risk_level", "unknown")
    found = "是" if customer_info.get("found") else "否"
    return (
        "【客户信息】\n"
        f"- 姓名：{customer_name}\n"
        f"- 是否命中客户资料：{found}\n"
        f"- 风险等级：{risk_level}\n"
        f"- 备注：{customer_note}\n"
    )


def _build_output_contract() -> str:
    return f"""【输出格式 - 严格遵守】
你的回复分为两部分：

第一部分：你要对客户说的话（纯文本，自然语言，不含 markdown）

第二部分：决策标签，格式如下：
<决策>
{{"intent": "...", "action": "...", "action_params": {{...}}}}
</决策>

示例：
您好，这款产品确实很适合您目前的资金情况。
<决策>
{{"intent": "interested", "action": "continue", "action_params": {{}}}}
</决策>

intent 可选值：{ALLOWED_INTENTS}
action 可选值：{ALLOWED_ACTIONS}
action_params 字段：extension（转人工分机）/ callback_time（回拨时间）/ note（备注）/ template_id（短信模板）/ vars（变量）/ farewell（告别语）/ reason（黑名单原因）

规则：
1. 用户要求人工客服时，intent 用 request_human，action 用 transfer。
2. 用户要求稍后再联系时，intent 用 callback，action 用 callback。
3. 用户明确要求不要再联系时，action 优先用 blacklist。
4. 只有明确要结束通话时才使用 end。
5. 必须先输出纯文本回复内容，然后换行输出 <决策> 标签及内部 JSON。
6. JSON 中不再包含 reply 字段。"""


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
    else:
        main_script_text = script_config.main_script
    customer_profile = _build_customer_profile(customer_info)
    output_contract = _build_output_contract()

    return f"""
{main_script_text}

{customer_profile}

{output_contract}


"""


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
            "tolerance_enabled": True,
            "tolerance_ms": 1000,
        }

    if speech_type == "opening":
        return {
            "barge_in_enabled": script_config.opening_barge_in,
            "protect_start_sec": script_config.barge_in_protect_start,
            "protect_end_sec": script_config.barge_in_protect_end,
            "tolerance_enabled": script_config.tolerance_enabled,
            "tolerance_ms": script_config.tolerance_ms,
        }
    elif speech_type == "closing":
        return {
            "barge_in_enabled": script_config.closing_barge_in,
            "protect_start_sec": script_config.barge_in_protect_start,
            "protect_end_sec": script_config.barge_in_protect_end,
            "tolerance_enabled": script_config.tolerance_enabled,
            "tolerance_ms": script_config.tolerance_ms,
        }
    else:  # conversation
        return {
            "barge_in_enabled": script_config.conversation_barge_in,
            "protect_start_sec": script_config.barge_in_protect_start,
            "protect_end_sec": script_config.barge_in_protect_end,
            "tolerance_enabled": script_config.tolerance_enabled,
            "tolerance_ms": script_config.tolerance_ms,
        }
