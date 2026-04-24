"""通义千问 LLM 配置 - 通过 DashScope OpenAI 兼容接口"""
import json
import re
import logging
from typing import Optional
from dataclasses import dataclass

from livekit.plugins import openai as lk_openai
from livekit.agents.types import NOT_GIVEN

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
    # 性能延迟指标（由 _stream_llm_and_speak 填充）
    llm_latency_ms: Optional[int] = None    # LLM 首 token 延迟
    tts_latency_ms: Optional[int] = None    # 首句 TTS 延迟（首句从提交到播放的耗时）
    was_interrupted: bool = False            # AI 回答是否被用户打断

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
    enable_thinking: bool = False,
) -> lk_openai.LLM:
    """创建通义千问 LLM 实例（通过 OpenAI 兼容接口）

    Args:
        enable_thinking: 是否启用思维链模式。默认 False。
            Qwen3/Qwen3.5 模型默认启用 thinking，会导致首 token 延迟达 15-20s。
            设为 False 可将 TTFT 降至 1-3s，显著降低端到端延迟。
    """
    extra_body: dict = {
        "enable_thinking": enable_thinking,
    }
    # 如果禁用 thinking，不需要设置 thinking_budget
    # 注意：thinking_budget 必须是正整数(1~38912)，设 0 会报错

    return lk_openai.LLM(
        model=model or settings.aliyun_llm_model,
        api_key=api_key or settings.aliyun_llm_api_key,
        base_url=base_url or settings.aliyun_llm_base_url,
        temperature=temperature,
        max_completion_tokens=max_tokens if max_tokens > 0 else NOT_GIVEN,
        extra_body=extra_body,
    )


# 内置的 decision 输出格式指令（从话术表中剥离，统一由系统代码管理）
_DECISION_FORMAT_INSTRUCTION = """\
在每次回复时，你需要输出两部分：
1. 你要说的话（纯文本）
2. 决策信息（JSON格式，用 <decision> 标签包裹）

决策JSON格式：
<decision>{"intent": "interested|need_more_info|not_interested|busy|request_human|callback|unknown", "action": "continue|transfer|end|callback|send_sms|blacklist"}</decision>

注意：
- 保持回复简洁，每次不超过3句话
- 根据用户意图判断下一步动作
- 如果用户明确拒绝，action 设为 end
- 如果用户要求转人工，action 设为 transfer"""


def build_system_prompt(
    main_prompt: str,
    customer_info: dict = None,
) -> str:
    """构建 LLM 系统提示

    组装顺序：
    1. 系统角色定义
    2. 话术业务内容（来自 main_prompt）
    3. decision 格式指令（内置）
    4. 行为约束
    5. 客户信息（可选）

    Args:
        main_prompt: 话术主要内容（来自 scripts 表的 main_prompt 字段）
        customer_info: 客户信息（可选）

    Returns:
        完整的系统提示文本
    """
    # 1. 系统角色定义
    parts = ["你是一个智能外呼助手。请用自然、礼貌的中文与用户对话。"]

    # 2. 话术业务内容
    parts.append(main_prompt)

    # 3. decision 格式指令（内置，不需要话术表包含）
    parts.append(_DECISION_FORMAT_INSTRUCTION)

    # 4. 行为约束
    parts.append(
        "重要：请直接回复用户，不要进行思考推理过程，不要输出思考内容。"
        "立即开始你的正式回复。"
    )

    # 5. 客户信息
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
    支持 Qwen3/Qwen3.5 的 <think>...</think> thinking 标签过滤
    以及 <decision>...</decision> 决策标签提取。
    """

    # Qwen3 thinking 标签（可能以 <think> 或 💭 形式出现）
    _THINK_START = "<think>"
    _THINK_END = "</think>"

    def __init__(self):
        self._buffer = ""
        self._text_parts = []
        self._decision_json = None
        self._in_decision_tag = False
        self._decision_buffer = ""
        self._in_think_tag = False
        self._think_buffer = ""

    def feed(self, token: str) -> Optional[str]:
        """输入一个 token，返回可播报的文本片段（如果有）

        Returns:
            可播报文本片段，或 None（在 decision/think 标签内时不输出）
        """
        # 在 decision 标签内时：仅往 decision_buffer 累积
        if self._in_decision_tag:
            self._decision_buffer += token
            if "</decision>" in self._decision_buffer:
                full_decision_buffer = self._decision_buffer
                json_str = full_decision_buffer.split("</decision>")[0].strip()
                try:
                    self._decision_json = json.loads(json_str)
                except json.JSONDecodeError:
                    logger.warning(f"流式 decision JSON 解析失败: {json_str}")
                self._in_decision_tag = False
                self._decision_buffer = ""
                after_text = full_decision_buffer.split("</decision>", 1)
                self._buffer = ""
                if len(after_text) > 1 and after_text[1].strip():
                    self._buffer = after_text[1]
                    self._text_parts.append(after_text[1].strip())
                    return after_text[1].strip()
            return None

        # 在 think 标签内时：仅往 think_buffer 累积，不输出
        if self._in_think_tag:
            self._think_buffer += token
            if self._THINK_END in self._think_buffer:
                # think 标签结束，丢弃思考内容
                logger.debug(f"StreamingParser: 过滤 think 内容 ({len(self._think_buffer)} chars)")
                after_text = self._think_buffer.split(self._THINK_END, 1)
                self._in_think_tag = False
                self._think_buffer = ""
                self._buffer = ""
                if len(after_text) > 1 and after_text[1].strip():
                    self._buffer = after_text[1]
                    self._text_parts.append(after_text[1].strip())
                    return after_text[1].strip()
            return None

        # 正常模式：往 buffer 累积
        self._buffer += token

        # 检测进入 <think> 标签
        if self._THINK_START in self._buffer:
            before = self._buffer.split(self._THINK_START)[0]
            self._in_think_tag = True
            self._think_buffer = self._buffer.split(self._THINK_START, 1)[1]
            self._buffer = ""
            if before.strip():
                self._text_parts.append(before.strip())
                return before.strip()
            return None

        # 检测进入 <decision> 标签
        if "<decision>" in self._buffer:
            before = self._buffer.split("<decision>")[0]
            self._in_decision_tag = True
            self._decision_buffer = self._buffer.split("<decision>", 1)[1]
            self._buffer = ""
            if before.strip():
                self._text_parts.append(before.strip())
                return before.strip()
            return None

        # 安全输出：保留最后 12 字符以防截断标签
        safe_len = len(self._buffer) - len(self._THINK_START) - 2  # -2 余量
        if safe_len > 0:
            output = self._buffer[:safe_len]
            self._buffer = self._buffer[safe_len:]
            if output.strip():
                self._text_parts.append(output)
                return output
        return None

    def flush(self) -> Optional[str]:
        """刷新缓冲区，返回剩余文本"""
        # 如果仍在 decision 模式中，丢弃缓冲区内容
        if self._in_decision_tag:
            logger.debug(f"flush 时仍在 decision 模式中，丢弃缓冲区: {self._buffer[:100]}")
            self._buffer = ""
            self._in_decision_tag = False
            return None

        # 如果仍在 think 模式中，丢弃思考内容
        if self._in_think_tag:
            logger.debug(f"flush 时仍在 think 模式中，丢弃缓冲区 ({len(self._think_buffer)} chars)")
            self._buffer = ""
            self._in_think_tag = False
            self._think_buffer = ""
            return None

        remaining = self._buffer.strip()
        self._buffer = ""
        if remaining:
            self._text_parts.append(remaining)
            return remaining
        return None

    def get_result(self) -> LLMResponse:
        """获取最终解析结果"""
        full_text = " ".join(self._text_parts).strip()

        # 安全兜底：移除残留的 <decision>...</decision> 标签及内容
        full_text = re.sub(r'<decision>[\s\S]*?</decision>', '', full_text).strip()
        # 同时清理不完整的标签残留（如只有 </decision> 的情况）
        full_text = re.sub(r'</decision>', '', full_text).strip()

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
