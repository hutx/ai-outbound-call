"""通义千问 LLM 配置 - 通过 DashScope OpenAI 兼容接口"""
import json
import re
import logging
from typing import Optional
from dataclasses import dataclass

from livekit.plugins import openai as lk_openai

from livekit_backend.core.config import settings
from livekit_backend.core.events import CallAction, CallIntent

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """LLM 响应解析结果"""
    text: str  # 要播报的文本
    intent: CallIntent = CallIntent.UNKNOWN
    action: CallAction = CallAction.CONTINUE
    action_params: dict = None

    def __post_init__(self):
        if self.action_params is None:
            self.action_params = {}


def create_qwen_llm(
    *,
    model: str = "",
    api_key: str = "",
    base_url: str = "",
    temperature: float = 0.4,
    max_tokens: int = 500,
) -> lk_openai.LLM:
    """创建通义千问 LLM 实例（通过 OpenAI 兼容接口）"""
    return lk_openai.LLM(
        model=model or settings.aliyun_llm_model,
        api_key=api_key or settings.aliyun_llm_api_key,
        base_url=base_url or settings.aliyun_llm_base_url,
        temperature=temperature,
    )


def build_system_prompt(
    main_prompt: str,
    customer_info: dict = None,
) -> str:
    """构建 LLM 系统提示

    Args:
        main_prompt: 话术主要内容（来自 scripts 表的 main_prompt 字段）
        customer_info: 客户信息（可选）

    Returns:
        完整的系统提示文本
    """
    parts = [main_prompt]

    if customer_info:
        info_text = "\n".join(f"- {k}: {v}" for k, v in customer_info.items() if v)
        parts.append(f"\n【客户信息】\n{info_text}")

    return "\n".join(parts)


def parse_llm_response(text: str) -> LLMResponse:
    """解析 LLM 输出

    LLM 输出格式：
    正文文本... <decision>{"intent": "...", "action": "..."}</decision>

    Returns:
        LLMResponse 包含文本和决策
    """
    # 尝试提取 <decision>...</decision> 标签
    pattern = r'<decision>\s*(.*?)\s*</decision>'
    match = re.search(pattern, text, re.DOTALL)

    if match:
        # 移除 decision 标签得到纯文本
        clean_text = re.sub(pattern, '', text, flags=re.DOTALL).strip()

        try:
            decision = json.loads(match.group(1))
            intent_str = decision.get("intent", "unknown")
            action_str = decision.get("action", "continue")
            action_params = decision.get("action_params", {})

            # 安全转换枚举
            try:
                intent = CallIntent(intent_str)
            except ValueError:
                intent = CallIntent.UNKNOWN

            try:
                action = CallAction(action_str)
            except ValueError:
                action = CallAction.CONTINUE

            return LLMResponse(
                text=clean_text,
                intent=intent,
                action=action,
                action_params=action_params or {},
            )
        except json.JSONDecodeError:
            logger.warning(f"无法解析 decision JSON: {match.group(1)}")
            return LLMResponse(text=clean_text)

    # 没有 decision 标签，尝试解析纯 JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "reply" in data:
            intent_str = data.get("intent", "unknown")
            action_str = data.get("action", "continue")
            try:
                intent = CallIntent(intent_str)
            except ValueError:
                intent = CallIntent.UNKNOWN
            try:
                action = CallAction(action_str)
            except ValueError:
                action = CallAction.CONTINUE
            return LLMResponse(
                text=data["reply"],
                intent=intent,
                action=action,
                action_params=data.get("action_params", {}),
            )
    except (json.JSONDecodeError, TypeError):
        pass

    # 纯文本回复
    return LLMResponse(text=text.strip())


# 流式解析器
class StreamingResponseParser:
    """流式 LLM 输出解析器

    在流式输出中逐 token 累积，实时提取可播报文本和最终决策。
    """

    def __init__(self):
        self._buffer = ""
        self._text_parts = []
        self._decision_json = None
        self._in_decision_tag = False
        self._decision_buffer = ""

    def feed(self, token: str) -> Optional[str]:
        """输入一个 token，返回可播报的文本片段（如果有）

        Returns:
            可播报文本片段，或 None（在 decision 标签内时不输出）
        """
        self._buffer += token

        # 检测进入 <decision> 标签
        if not self._in_decision_tag:
            if "<decision>" in self._buffer:
                # 提取 <decision> 之前的文本
                before = self._buffer.split("<decision>")[0]
                self._in_decision_tag = True
                self._decision_buffer = self._buffer.split("<decision>", 1)[1]
                self._buffer = ""
                if before.strip():
                    self._text_parts.append(before.strip())
                    return before.strip()
                return None

            # 安全输出：如果 buffer 中没有 < 的前缀，可以输出
            # 保留最后几个字符以防截断标签
            safe_len = len(self._buffer) - 12  # len("<decision>") + 余量
            if safe_len > 0:
                output = self._buffer[:safe_len]
                self._buffer = self._buffer[safe_len:]
                if output.strip():
                    self._text_parts.append(output)
                    return output
            return None
        else:
            # 在 decision 标签内，累积
            self._decision_buffer += token
            if "</decision>" in self._decision_buffer:
                # 解析完成 — 先保存完整 buffer 再重置
                full_decision_buffer = self._decision_buffer
                json_str = full_decision_buffer.split("</decision>")[0].strip()
                try:
                    self._decision_json = json.loads(json_str)
                except json.JSONDecodeError:
                    logger.warning(f"流式 decision JSON 解析失败: {json_str}")
                self._in_decision_tag = False
                self._decision_buffer = ""

                # 检查 </decision> 之后是否还有文本
                after = full_decision_buffer.split("</decision>", 1)
                if len(after) > 1 and after[1].strip():
                    self._buffer = after[1]
                    return after[1].strip()
            return None

    def flush(self) -> Optional[str]:
        """刷新缓冲区，返回剩余文本"""
        remaining = self._buffer.strip()
        self._buffer = ""
        if remaining:
            self._text_parts.append(remaining)
            return remaining
        return None

    def get_result(self) -> LLMResponse:
        """获取最终解析结果"""
        full_text = " ".join(self._text_parts).strip()

        if self._decision_json:
            intent_str = self._decision_json.get("intent", "unknown")
            action_str = self._decision_json.get("action", "continue")
            try:
                intent = CallIntent(intent_str)
            except ValueError:
                intent = CallIntent.UNKNOWN
            try:
                action = CallAction(action_str)
            except ValueError:
                action = CallAction.CONTINUE
            return LLMResponse(
                text=full_text,
                intent=intent,
                action=action,
                action_params=self._decision_json.get("action_params", {}),
            )

        return LLMResponse(text=full_text)
