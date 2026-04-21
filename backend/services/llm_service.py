"""
LLM 对话服务 — 支持多模型（Anthropic Claude / 通义千问）
──────────────────────
- 支持多种 LLM 提供商，根据配置自动选择
- RateLimitError 自动退避重试（最多 3 次，指数退避）
- 话术脚本支持从数据库加载（新增功能）
- 流式 API 改为非流式（JSON 结构输出更稳定）
- _parse_response 容错增强
"""

import asyncio
import json
import logging
from typing import AsyncGenerator

import anthropic

from backend.core.config import config, LLMConfig

logger = logging.getLogger(__name__)


class LLMService:
    """
    LLM 对话服务
    根据模型名称自动选择对应的提供商（Claude / 通义千问等）
    """

    SUPPORTED_PROVIDERS = {
        "auto",
        "anthropic",
        "dashscope_compatible",
        "anthropic_compatible",
    }

    def __init__(self, cfg: LLMConfig | None = None):
        self._cfg = cfg or config.llm
        self.provider = self._resolve_provider(self._cfg)
        self.transport = "anthropic_sdk"
        self.client = anthropic.AsyncAnthropic(
            api_key=self._cfg.anthropic_api_key,
            base_url=self._cfg.anthropic_base_url or None,
        )

    @classmethod
    def _resolve_provider(cls, cfg: LLMConfig) -> str:
        explicit = (getattr(cfg, "provider", "auto") or "auto").strip().lower()
        if explicit != "auto":
            if explicit not in cls.SUPPORTED_PROVIDERS:
                raise ValueError(f"不支持的 LLM_PROVIDER: {explicit}")
            return explicit

        base_url = (cfg.anthropic_base_url or "").lower()
        model = (cfg.model or "").lower()
        if "dashscope" in base_url or "qwen" in model:
            return "dashscope_compatible"
        if "claude" in model:
            return "anthropic"
        if base_url and "anthropic" not in base_url:
            return "anthropic_compatible"
        return "anthropic"

    async def chat(
        self,
        messages: list,
        system_prompt: str,
    ) -> dict:
        """
        非流式对话，支持多模型提供商
        """
        cfg = self._cfg
        trimmed = self._trim_history(messages)
        last_error = None

        logger.info(
            "LLM 调用 "
            f"(provider={self.provider}, transport={self.transport}, "
            f"model={cfg.model}, messages={len(trimmed)})"
        )

        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=cfg.model,
                    max_tokens=cfg.max_tokens,
                    temperature=cfg.temperature,
                    system=system_prompt,
                    messages=trimmed,
                    thinking={"type": "disabled"},
                )

                raw_text = self._extract_text_content(response)
                logger.info(
                    f"LLM 原始响应 (attempt={attempt+1}, provider={self.provider}, raw_text={raw_text[:120]})"
                )
                return self._parse_response(raw_text)

            except Exception as e:
                logger.error(
                    f"LLM 调用异常 (attempt={attempt+1}, provider={self.provider}): {e}"
                )
                last_error = e
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s

        logger.error(f"LLM 重试 3 次均失败: {last_error}")
        return self._fallback_response()

    async def chat_stream(
        self,
        messages: list,
        system_prompt: str,
        timeout: float = 30.0,
    ) -> AsyncGenerator[str | dict, None]:
        """流式对话，逐 token 产出文本。

        Yields:
            str: 普通文本 token（调用者可逐句拼接并播放）
            dict: 最终元素，解析后的 LLM 决策
        """
        cfg = self._cfg
        trimmed = self._trim_history(messages)
        reply_buffer = ""
        decision_buffer = ""
        in_decision = False
        decision_json = None

        logger.info(
            f"LLM 流式调用 (provider={self.provider}, transport={self.transport}, "
            f"model={cfg.model}, messages={len(trimmed)}, timeout={timeout}s)"
        )

        try:
            async with asyncio.timeout(timeout):
                message_stream = await self.client.messages.create(
                    model=cfg.model,
                    max_tokens=cfg.max_tokens,
                    temperature=cfg.temperature,
                    system=system_prompt,
                    messages=trimmed,
                    thinking={"type": "disabled"},
                    stream=True,
                )

                async for event in message_stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta and getattr(delta, "type", "") == "text_delta":
                            text = getattr(delta, "text", "")
                            if not text:
                                continue

                            if in_decision:
                                decision_buffer += text
                                logger.debug(f"[LLM流式] <决策>内: {text!r}, 累计: {decision_buffer[:200]!r}")
                                if "</决策>" in decision_buffer:
                                    json_str = decision_buffer.split("</决策>")[0].strip()
                                    logger.info(f"[LLM流式] 决策标签完整: {json_str[:200]!r}")
                                    try:
                                        decision_json = json.loads(json_str)
                                        logger.info(f"[LLM流式] 决策解析成功: intent={decision_json.get('intent')}, action={decision_json.get('action')}")
                                    except json.JSONDecodeError:
                                        logger.warning(f"[LLM流式] 决策 JSON 解析失败: {json_str[:200]}")
                                    break
                                continue

                            reply_buffer += text
                            if "<决策>" in reply_buffer:
                                pre, post = reply_buffer.split("<决策>", 1)
                                logger.info(f"[LLM流式] 发现<决策>标签, reply={pre.strip()[:100]!r}")
                                if pre.strip():
                                    yield pre.strip()
                                reply_buffer = ""
                                decision_buffer = post
                                in_decision = True
                                if "</决策>" in decision_buffer:
                                    json_str = decision_buffer.split("</决策>")[0].strip()
                                    logger.info(f"[LLM流式] 决策标签同行完整: {json_str[:200]!r}")
                                    try:
                                        decision_json = json.loads(json_str)
                                        logger.info(f"[LLM流式] 决策解析成功: intent={decision_json.get('intent')}, action={decision_json.get('action')}")
                                    except json.JSONDecodeError:
                                        logger.warning(f"[LLM流式] 决策 JSON 解析失败: {json_str[:200]}")
                                    break
                                continue

                            if any(c in text for c in "。！？\n"):
                                logger.debug(f"[LLM流式] 切句: {reply_buffer!r}")
                                yield reply_buffer
                                reply_buffer = ""

        except asyncio.TimeoutError:
            logger.error(f"LLM 流式调用超时 ({timeout}s)")
            if reply_buffer.strip():
                yield self._parse_response(reply_buffer)
                return
            logger.info("LLM 流式调用超时，降级到非流式调用")
            try:
                yield await self.chat(messages, system_prompt)
            except Exception as e:
                logger.error(f"LLM 降级调用也失败: {e}")
                yield self._fallback_response()
            return
        except Exception as e:
            logger.error(f"LLM 流式调用异常: {e}")
            yield self._fallback_response()
            return

        if reply_buffer.strip():
            logger.debug(f"[LLM流式] 最终回复: {reply_buffer.strip()[:200]!r}")
            yield reply_buffer.strip()

        if decision_json:
            logger.info(f"[LLM流式] 最终输出: reply={reply_buffer.strip()[:50]!r}, intent={decision_json.get('intent')}, action={decision_json.get('action')}")
            yield {
                "reply": reply_buffer.strip() or "好的，请稍等。",
                "intent": decision_json.get("intent", "unknown"),
                "action": decision_json.get("action", "continue"),
                "action_params": decision_json.get("action_params", {}),
            }
        else:
            full_text = reply_buffer.strip()
            if full_text:
                logger.warning("LLM 流式输出未找到 <决策> 标签，使用旧格式兜底")
            yield self._parse_response(full_text)

    def _trim_history(self, messages: list) -> list:
        """裁剪对话历史，保留最近 N 轮"""
        cfg = getattr(self, "_cfg", config.llm)
        max_msgs = cfg.max_history_turns * 2  # 每轮 = user + assistant
        if len(messages) > max_msgs:
            # 永远保留第一条（通常是背景信息）
            return [messages[0]] + messages[-(max_msgs - 1) :]
        return messages

    def _extract_text_content(self, response) -> str:
        """兼容 Anthropic 风格 content block，提取第一个文本块。"""
        for block in getattr(response, "content", []):
            block_type = getattr(block, "type", "")
            if block_type == "text":
                return getattr(block, "text", "").strip()
        return ""

    def _parse_response(self, text: str) -> dict:
        """解析 LLM 返回的响应。

        新格式：纯文本回复 + <决策>JSON</决策>
        旧格式：纯 JSON（兜底兼容）
        """
        text = text.strip()
        # 清理可能的 markdown 代码块
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]).strip()

        # 尝试解析新格式：<决策> 标签
        if "<决策>" in text and "</决策>" in text:
            reply = text.split("<决策>")[0].strip()
            json_str = text.split("<决策>")[1].split("</决策>")[0].strip()
            try:
                data = json.loads(json_str)
                return {
                    "reply": reply or "好的，请稍等。",
                    "intent": data.get("intent", "unknown"),
                    "action": data.get("action", "continue"),
                    "action_params": data.get("action_params", {}),
                }
            except json.JSONDecodeError:
                logger.warning(f"决策标签内 JSON 解析失败: {json_str[:120]}")
                return {
                    "reply": reply or "好的，请稍等。",
                    "intent": "unknown",
                    "action": "continue",
                    "action_params": {},
                }

        # 兜底：旧格式（纯 JSON）
        try:
            data = json.loads(text)
            return {
                "reply": data.get("reply", "好的，请稍等。"),
                "intent": data.get("intent", "unknown"),
                "action": data.get("action", "continue"),
                "action_params": data.get("action_params", {}),
            }
        except json.JSONDecodeError:
            logger.warning(f"LLM 响应 JSON 解析失败，原文: {text[:200]}")
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
