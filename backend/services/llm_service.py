"""
LLM 对话服务 — 生产级
──────────────────────
- RateLimitError 自动退避重试（最多 3 次，指数退避）
- 话术脚本支持从 JSON 文件热加载（SCRIPTS_PATH 环境变量）
- 流式 API 改为非流式（JSON 结构输出更稳定）
- _parse_response 容错增强
"""
import json
import logging
import asyncio
import os
from typing import Optional
import anthropic

from backend.core.config import config

logger = logging.getLogger(__name__)

# ============================================================
# 话术脚本库
# 优先从 SCRIPTS_PATH 指定的 JSON 文件加载
# 文件不存在则使用内置默认脚本
# ============================================================

def _load_scripts() -> dict:
    """加载话术脚本（支持热加载）"""
    path = os.environ.get("SCRIPTS_PATH", "")
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                scripts = json.load(f)
            logger.info(f"话术脚本已从文件加载: {path} ({len(scripts)} 个)")
            return scripts
        except Exception as e:
            logger.error(f"话术脚本文件加载失败: {e}，使用内置脚本")
    return _DEFAULT_SCRIPTS


_DEFAULT_SCRIPTS = {
    "finance_product_a": {
        "product_name": "稳享理财A款",
        "product_desc": "年化收益率3.8%，起投1万元，T+1到账，无手续费",
        "target_customer": "有理财需求的储蓄型客户",
        "key_selling_points": [
            "收益稳定，高于普通存款",
            "灵活申赎，资金不被锁定",
            "银行保本，安全有保障",
        ],
        "objection_handling": {
            "利率太低": "相比活期存款年化0.35%，我们的3.8%已经是市场上同类产品中较高的，而且保本保息",
            "不需要": "完全理解，请问您目前是有其他理财渠道，还是暂时不考虑投资？",
            "没钱": "没关系，我们起投门槛只有1万元，而且随时可以赎回",
        },
    },
    "insurance_renewal": {
        "product_name": "人寿险续保提醒",
        "product_desc": "您的保单即将到期，续保享受老客户专属折扣",
        "target_customer": "即将到期的保险客户",
        "key_selling_points": [
            "老客户续保享9折优惠",
            "无需重新体检",
            "保障范围升级",
        ],
        "objection_handling": {
            "不想续了": "请问是对哪方面不满意？我们可以为您推荐更适合的方案",
            "太贵了": "老客户专属折扣后只需XX元，而且保障金额提升了",
        },
    },
    "loan_followup": {
        "product_name": "消费贷款回访",
        "product_desc": "您之前询问过我们的消费贷款，现在利率有所下调",
        "target_customer": "曾咨询过贷款的潜在客户",
        "key_selling_points": [
            "年化利率仅3.6%",
            "最高可贷50万",
            "最快当天放款",
        ],
        "objection_handling": {
            "不需要了": "好的，如果以后有资金需求欢迎联系我们",
            "利率高": "我们目前是市场上利率最低的产品之一，您方便说说您期望的利率是多少吗",
        },
    },
}

# 全局话术脚本（懒加载）
SCRIPTS: dict = {}


def get_scripts() -> dict:
    """获取话术脚本（懒加载 + 热加载支持）"""
    global SCRIPTS
    if not SCRIPTS:
        SCRIPTS = _load_scripts()
    return SCRIPTS


def build_system_prompt(script_id: str, customer_info: dict) -> str:
    """
    构建系统 Prompt
    这是整个智能外呼的核心，决定 AI 的行为边界
    """
    script = get_scripts().get(script_id, get_scripts().get("finance_product_a", {}))
    customer_name = customer_info.get("name", "您")
    customer_note = customer_info.get("note", "")

    objections_text = "\n".join([
        f'  - 若客户说"{k}"，回应："{v}"'
        for k, v in script["objection_handling"].items()
    ])

    return f"""你是一名专业的银行智能客服，名叫"小智"。你正在进行一次电话外呼。

【重要合规声明 - 必须遵守】
1. 开场白必须包含："本通话由人工智能完成" 这句话
2. 客户明确拒绝2次后，立即礼貌结束通话，不得再次推销
3. 不得承诺不实收益或夸大宣传
4. 客户要求转人工时，立即响应

【本次外呼信息】
- 客户姓名：{customer_name}
- 推介产品：{script["product_name"]}
- 产品介绍：{script["product_desc"]}
- 目标客群：{script["target_customer"]}
- 客户备注：{customer_note or "无"}

【核心卖点（自然融入对话，不要生硬罗列）】
{chr(10).join(f"  • {p}" for p in script["key_selling_points"])}

【异议处理话术】
{objections_text}

【对话风格要求】
- 语气亲切自然，像朋友聊天，不要照本宣科
- 每次回复控制在2-3句话内，不要长篇大论
- 多提问、引导客户说话，了解真实需求
- 遇到客户问具体数字，直接给出，不要回避

【输出格式 - 严格遵守】
你必须返回合法的 JSON，格式如下：
{{
  "reply": "你要说的话（纯文字，不含标点以外的特殊字符）",
  "intent": "用户意图：interested|need_more_info|not_interested|busy|request_human|callback|unknown",
  "action": "下一步动作：continue|transfer|end|send_sms",
  "action_params": {{}}
}}

不要在 JSON 外面加任何文字或 markdown 代码块。"""


def build_opening(script_id: str, customer_info: dict) -> dict:
    """
    生成开场白（不经过 LLM，直接返回固定话术）
    开场白包含合规声明，必须固定
    """
    script = get_scripts().get(script_id, get_scripts().get("finance_product_a", {}))
    customer_name = customer_info.get("name", "")
    name_part = f"{customer_name}您好" if customer_name else "您好"

    return {
        "reply": (
            f"{name_part}，我是XX银行的智能客服小智，"
            f"本通话由人工智能完成，请放心。"
            f"请问您现在方便说话吗？"
        ),
        "intent": "unknown",
        "action": "continue",
        "action_params": {},
    }


class LLMService:
    """
    LLM 对话服务
    封装 Claude API 调用，提供流式和非流式两种模式
    """

    def __init__(self):
        self.client = anthropic.AsyncAnthropic(
            api_key=config.llm.anthropic_api_key,
            base_url=config.llm.anthropic_base_url,
        )
        self._cfg = config.llm

    async def chat(
        self,
        messages: list,
        system_prompt: str,
    ) -> dict:
        """
        非流式对话，带指数退避重试（RateLimitError 最多重试 3 次）
        """
        trimmed = self._trim_history(messages)
        last_error = None

        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=self._cfg.model,
                    max_tokens=self._cfg.max_tokens,
                    temperature=self._cfg.temperature,
                    system=system_prompt,
                    messages=trimmed,
                )
                raw_text = response.content[0].text.strip()
                logger.debug(f"LLM 原始响应 (attempt={attempt+1}): {raw_text[:120]}")
                return self._parse_response(raw_text)

            except anthropic.RateLimitError as e:
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(f"LLM RateLimitError，{wait}s 后重试 (attempt={attempt+1}): {e}")
                last_error = e
                await asyncio.sleep(wait)

            except Exception as e:
                logger.error(f"LLM 调用异常 (attempt={attempt+1}): {e}")
                last_error = e
                if attempt < 2:
                    await asyncio.sleep(1)

        logger.error(f"LLM 重试 3 次均失败: {last_error}")
        return self._fallback_response()

    async def stream_chat(
        self,
        messages: list,
        system_prompt: str,
    ) -> dict:
        """
        流式收集 + 完整返回
        收集全部流式 token 后一次性解析 JSON 返回
        """
        trimmed = self._trim_history(messages)
        full_text = ""

        async with self.client.messages.stream(
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            temperature=self._cfg.temperature,
            system=system_prompt,
            messages=trimmed,
        ) as stream:
            async for text in stream.text_stream:
                full_text += text

        return self._parse_response(full_text)

    def _trim_history(self, messages: list) -> list:
        """裁剪对话历史，保留最近 N 轮"""
        max_msgs = self._cfg.max_history_turns * 2  # 每轮 = user + assistant
        if len(messages) > max_msgs:
            # 永远保留第一条（通常是背景信息）
            return [messages[0]] + messages[-(max_msgs - 1):]
        return messages

    def _parse_response(self, text: str) -> dict:
        """解析 LLM 返回的 JSON"""
        # 清理可能的 markdown 代码块
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])

        try:
            data = json.loads(text)
            # 确保必要字段存在
            return {
                "reply": data.get("reply", "好的，请稍等。"),
                "intent": data.get("intent", "unknown"),
                "action": data.get("action", "continue"),
                "action_params": data.get("action_params", {}),
            }
        except json.JSONDecodeError:
            logger.warning(f"LLM 响应 JSON 解析失败，原文: {text[:200]}")
            # 如果 JSON 解析失败，尝试直接把文本当作回复
            return {
                "reply": text[:200] if text else "好的，请稍等。",
                "intent": "unknown",
                "action": "continue",
                "action_params": {},
            }

    def _fallback_response(self) -> dict:
        """降级回复（LLM 不可用时）"""
        return {
            "reply": "抱歉，我这边信号不太好，请问您方便稍后再聊吗？",
            "intent": "unknown",
            "action": "end",
            "action_params": {},
        }
