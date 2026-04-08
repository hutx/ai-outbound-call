"""
异步话术脚本服务工具
为AI通话代理提供异步的话术脚本相关功能
"""
import logging
from typing import Dict, Optional
import json

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
        script = script_config.main_script if hasattr(script_config, 'main_script') else script_config
        objection_handling = script_config.objection_handling if isinstance(script_config.objection_handling, dict) else {}
        opening_pause = script_config.opening_pause
        customer_name = customer_info.get("name", "您")
        customer_note = customer_info.get("note", "")

    # 构建异议处理文本
    objections_text = "\n".join([
        f'  - 若客户说"{k}"，回应："{v}"'
        for k, v in objection_handling.items()
    ])

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
- 推介产品：{script.get("product_name", "默认产品")}
- 产品介绍：{script.get("product_desc", "默认产品描述")}
- 目标客群：{script.get("target_customer", "默认客户")}
- 客户备注：{customer_note or "无"}

【核心卖点（自然融入对话，不要生硬罗列）】
{chr(10).join(f"  • {p}" for p in script.get("key_selling_points", ["默认卖点"]))}

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