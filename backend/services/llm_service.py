"""
LLM 对话服务 — 支持多模型（Anthropic Claude / 通义千问）
──────────────────────
- 支持多种 LLM 提供商，根据配置自动选择
- RateLimitError 自动退避重试（最多 3 次，指数退避）
- 话术脚本支持从数据库加载（新增功能）
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


class LLMService:
    """
    LLM 对话服务
    根据模型名称自动选择对应的提供商（Claude / 通义千问等）
    """

    def __init__(self):
        self.client = anthropic.AsyncAnthropic(
            api_key=config.llm.anthropic_api_key,
            base_url=config.llm.anthropic_base_url,
        )
        # Determine provider based on model name
        if "qwen" in config.llm.model.lower():
            self.provider = "dashscope"
        else:
            self.provider = "anthropic"

    async def chat(
        self,
        messages: list,
        system_prompt: str,
    ) -> dict:
        """
        非流式对话，支持多模型提供商
        """
        trimmed = self._trim_history(messages)
        last_error = None

        logger.info(f"LLM 调用 (provider={self.provider}, system_prompt={system_prompt}, messages={trimmed})")

        for attempt in range(3):
            try:
                # 通过 DashScope 兼容接口或原生 Anthropic API 调用
                response = await self.client.messages.create(
                    model=config.llm.model,
                    max_tokens=config.llm.max_tokens,
                    temperature=config.llm.temperature,
                    system=system_prompt,
                    messages=trimmed,
                    thinking={"type": "disabled"},
                )

                # 遍历 content 块，找到第一个 TextBlock（跳过 ThinkingBlock）
                raw_text = ""
                for block in response.content:
                    if block.type == "text":
                        raw_text = block.text.strip()
                        break
                    elif hasattr(block, "thinking"):
                        continue
                logger.info(
                    f"LLM 原始响应 (attempt={attempt+1}, provider={self.provider}, raw_text={raw_text[:120]}):"
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

    def _trim_history(self, messages: list) -> list:
        """裁剪对话历史，保留最近 N 轮"""
        max_msgs = config.llm.max_history_turns * 2  # 每轮 = user + assistant
        if len(messages) > max_msgs:
            # 永远保留第一条（通常是背景信息）
            return [messages[0]] + messages[-(max_msgs - 1) :]
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