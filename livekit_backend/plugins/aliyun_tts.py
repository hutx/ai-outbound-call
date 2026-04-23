"""
阿里云百炼 CosyVoice TTS 插件 (LiveKit Agents)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

基于 DashScope WebSocket API 的 CosyVoice 语音合成实现。
协议文档: https://help.aliyun.com/zh/model-studio/cosyvoice-websocket-api

交互流程:
  1. 建立 WebSocket 连接 (Bearer Token 认证)
  2. 发送 run-task 指令 (含模型、音色、格式等参数)
  3. 收到 task-started 事件
  4. 发送 continue-task 指令 (含待合成文本)
  5. 发送 finish-task 指令 (通知服务端文本发送完毕)
  6. 接收二进制音频流 (PCM 16-bit)
  7. 收到 task-finished 事件
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import unicodedata
import uuid

import websockets

from livekit.agents import tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

from livekit_backend.core.config import settings

logger = logging.getLogger(__name__)

# 中日韩标点、通用标点、符号的 Unicode 分类
_PUNCTUATION_CATEGORIES = frozenset({"Pc", "Pd", "Pe", "Pf", "Pi", "Po", "Ps"})
# 常见中文标点（CosyVoice 不接受仅含这些字符的文本）
_CHINESE_PUNCTUATION = frozenset("，。！？、；：''（）【】《》…—～·")


def _is_punctuation_only(text: str) -> bool:
    """判断文本是否仅包含标点符号和空白（无实际语音内容）

    CosyVoice API 会拒绝仅含标点的文本（InvalidParameter），
    因此需要在发送前过滤掉这类片段。
    """
    for ch in text:
        if ch in _CHINESE_PUNCTUATION:
            continue
        cat = unicodedata.category(ch)
        if cat in _PUNCTUATION_CATEGORIES:
            continue
        if ch.isspace():
            continue
        # 存在非标点、非空白字符 → 有实际语音内容
        return False
    return True


class AliyunTTS(tts.TTS):
    """
    阿里云百炼 CosyVoice TTS 插件

    通过 DashScope WebSocket API 与 CosyVoice 模型交互，
    输出 16kHz/16bit/mono PCM 音频流。

    配置项来自 livekit_backend.core.config.settings:
      - aliyun_tts_api_key:  DashScope API Key
      - aliyun_tts_model:    模型名称 (默认 cosyvoice-v1)
      - aliyun_tts_voice:    音色 (默认 longxiaochun)
      - aliyun_tts_ws_url:   WebSocket 地址
      - aliyun_tts_speech_rate: 语速 (0.5~2.0)
      - aliyun_tts_volume:   音量 (0~100)
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "",
        voice: str = "",
        speech_rate: float = 0.0,
        volume: int = -1,
        sample_rate: int = 16000,
    ):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=1,
        )
        # 优先使用显式参数，回退到 settings 配置
        self._api_key = api_key or settings.aliyun_tts_api_key
        self._model = model or settings.aliyun_tts_model
        self._voice = voice or settings.aliyun_tts_voice
        self._speech_rate = speech_rate if speech_rate > 0 else settings.aliyun_tts_speech_rate
        self._volume = volume if volume >= 0 else settings.aliyun_tts_volume
        self._ws_url = settings.aliyun_tts_ws_url
        self._sample_rate = sample_rate

        # TTS 缓存目录
        self._cache_dir = os.path.join(os.getcwd(), "docker", "tts_cache")
        os.makedirs(self._cache_dir, exist_ok=True)

        if not self._api_key:
            logger.warning("AliyunTTS: API Key 未配置，TTS 将无法工作")

        logger.info(
            f"AliyunTTS 初始化: model={self._model}, voice={self._voice}, "
            f"rate={self._speech_rate}, volume={self._volume}, "
            f"sample_rate={self._sample_rate}"
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "aliyun_cosyvoice"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "AliyunTTSStream":
        """同步合成入口 — 接收完整文本，返回 ChunkedStream"""
        return AliyunTTSStream(
            tts=self,
            input_text=text,
            conn_options=conn_options,
        )

    def stream(
        self,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "AliyunTTSStreamAdapter":
        """流式合成入口 — 返回 SynthesizeStream，支持逐句推送文本"""
        return AliyunTTSStreamAdapter(
            tts=self,
            conn_options=conn_options,
        )

    # ── 缓存 ──────────────────────────────────────────────

    def _get_cache_path(self, text: str) -> str:
        """基于文本+音色+模型生成缓存文件路径"""
        cache_key = f"tts_{self._voice}_{self._model}_{text}"
        text_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        return os.path.join(self._cache_dir, f"tts_{text_hash}.pcm")

    # ── WebSocket 协议辅助 ──────────────────────────────────

    def _build_run_task_msg(self, task_id: str) -> dict:
        """构建 run-task 指令"""
        return {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self._model,
                "parameters": {
                    "text_type": "PlainText",
                    "voice": self._voice,
                    "format": "pcm",
                    "sample_rate": self._sample_rate,
                    "volume": self._volume,
                    "rate": self._speech_rate,
                    "pitch": 1.0,
                },
                "input": {},
            },
        }

    @staticmethod
    def _build_continue_task_msg(task_id: str, text: str) -> dict:
        """构建 continue-task 指令 (发送待合成文本)"""
        return {
            "header": {
                "action": "continue-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {
                "input": {"text": text},
            },
        }

    @staticmethod
    def _build_finish_task_msg(task_id: str) -> dict:
        """构建 finish-task 指令 (通知文本发送完毕)"""
        return {
            "header": {
                "action": "finish-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {"input": {}},
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WebSocket 合成核心逻辑 (ChunkedStream / SynthesizeStream 共用)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _ws_synthesize(
    tts_instance: AliyunTTS,
    text: str,
    output_emitter: tts.AudioEmitter,
    segment_id: str = "",
) -> None:
    """
    通过 WebSocket 连接百炼 CosyVoice 合成音频。

    流程:
      1. 建立 WebSocket 连接
      2. 发送 run-task → 等待 task-started
      3. 发送 continue-task (文本) + finish-task
      4. 接收二进制音频 → push 到 output_emitter
      5. 收到 task-finished → 结束

    Args:
        tts_instance: AliyunTTS 实例 (提供配置和协议消息构建)
        text: 待合成文本
        output_emitter: AudioEmitter，用于推送 PCM 音频数据
        segment_id: 段标识 (仅 SynthesizeStream 使用)
    """
    if not tts_instance._api_key:
        logger.error("AliyunTTS: API Key 为空，无法合成")
        return

    task_id = uuid.uuid4().hex
    logger.info(
        f"AliyunTTS _ws_synthesize: text={text!r}, "
        f"len={len(text)}, model={tts_instance._model}, "
        f"voice={tts_instance._voice}, task_id={task_id}"
    )

    try:
        async with websockets.connect(
            tts_instance._ws_url,
            additional_headers={
                "Authorization": f"bearer {tts_instance._api_key}",
            },
            max_size=10 * 1024 * 1024,  # 10MB
            ping_interval=20,
            ping_timeout=60,
        ) as ws:
            # ① 发送 run-task
            run_task_msg = tts_instance._build_run_task_msg(task_id)
            logger.info(f"AliyunTTS _ws_synthesize: sending run-task: {json.dumps(run_task_msg, ensure_ascii=False)[:300]}")
            await ws.send(json.dumps(run_task_msg))

            # ② 接收事件 + 音频流
            task_started = False
            async for message in ws:
                if isinstance(message, bytes):
                    # 二进制消息 = PCM 音频数据
                    output_emitter.push(message)
                else:
                    # 文本消息 = JSON 事件
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        logger.warning(
                            f"AliyunTTS: 无法解析 JSON: {message[:200]}"
                        )
                        continue

                    event = data.get("header", {}).get("event", "")

                    if event == "task-started":
                        task_started = True
                        logger.debug(f"AliyunTTS: task-started, id={task_id}")

                        # ③ 发送 continue-task (待合成文本)
                        continue_msg = AliyunTTS._build_continue_task_msg(task_id, text)
                        logger.info(f"AliyunTTS _ws_synthesize: sending continue-task: {json.dumps(continue_msg, ensure_ascii=False)[:300]}")
                        await ws.send(json.dumps(continue_msg))

                        # ④ 发送 finish-task (通知文本发送完毕)
                        finish_msg = AliyunTTS._build_finish_task_msg(task_id)
                        logger.info(f"AliyunTTS _ws_synthesize: sending finish-task")
                        await ws.send(json.dumps(finish_msg))

                    elif event == "result-generated":
                        # 中间结果通知，可忽略
                        pass

                    elif event == "task-finished":
                        logger.debug(
                            f"AliyunTTS: task-finished, id={task_id}"
                        )
                        break

                    elif event == "task-failed":
                        error_code = data.get("header", {}).get(
                            "error_code", ""
                        )
                        error_msg = data.get("header", {}).get(
                            "error_message", "unknown"
                        )
                        logger.error(
                            f"AliyunTTS: task-failed, id={task_id}, "
                            f"code={error_code}, msg={error_msg}, "
                            f"full_response={json.dumps(data, ensure_ascii=False)[:500]}"
                        )
                        break

                    else:
                        logger.debug(f"AliyunTTS: 未知事件: {event}")

            if not task_started:
                logger.error(
                    f"AliyunTTS: 未收到 task-started, id={task_id}"
                )

    except websockets.exceptions.WebSocketException as e:
        logger.error(f"AliyunTTS: WebSocket 异常: {e}")
    except asyncio.TimeoutError:
        logger.error(f"AliyunTTS: WebSocket 超时, id={task_id}")
    except Exception as e:
        logger.error(f"AliyunTTS: 合成异常: {e}", exc_info=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ChunkedStream — 非流式合成 (synthesize)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class AliyunTTSStream(tts.ChunkedStream):
    """
    非流式 TTS — 一次性合成完整音频

    工作流程:
      1. 检查本地缓存 (MD5 hash)
      2. 命中缓存 → 直接读取 PCM 文件推送到 output_emitter
      3. 未命中 → 建立 WebSocket 连接进行合成
      4. 将合成结果保存缓存
    """

    def __init__(
        self,
        *,
        tts: AliyunTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ):
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts_instance: AliyunTTS = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        """ChunkedStream 核心方法 — 合成音频并通过 output_emitter 发送"""
        text = self._input_text
        if not text or not text.strip():
            return

        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._tts_instance._sample_rate,
            num_channels=1,
            stream=False,  # 非流式模式
            mime_type="audio/pcm",
        )

        # 1. 检查缓存
        cache_path = self._tts_instance._get_cache_path(text)
        if os.path.exists(cache_path):
            logger.debug(f"AliyunTTS: 缓存命中 → {cache_path}")
            audio_data = await asyncio.get_event_loop().run_in_executor(
                None, self._read_cache_file, cache_path,
            )
            if audio_data:
                output_emitter.push(audio_data)
                return

        # 2. WebSocket 合成 (需要收集数据用于缓存)
        audio_data = await self._synthesize_with_cache(text, output_emitter)

        # 3. 保存缓存
        if audio_data:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._write_cache_file, cache_path, audio_data,
                )
            except Exception as e:
                logger.warning(f"AliyunTTS: 缓存写入失败: {e}")

    async def _synthesize_with_cache(
        self, text: str, output_emitter: tts.AudioEmitter
    ) -> bytes:
        """
        通过 WebSocket 合成音频，同时收集数据用于缓存。
        返回完整 PCM 数据 (用于缓存写入)。
        """
        if not self._tts_instance._api_key:
            logger.error("AliyunTTS: API Key 为空，无法合成")
            return b""

        task_id = uuid.uuid4().hex
        audio_chunks: list[bytes] = []

        try:
            async with websockets.connect(
                self._tts_instance._ws_url,
                additional_headers={
                    "Authorization": f"bearer {self._tts_instance._api_key}",
                },
                max_size=10 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=60,
            ) as ws:
                # ① run-task
                await ws.send(json.dumps(
                    self._tts_instance._build_run_task_msg(task_id)
                ))

                # ② 接收事件 + 音频
                task_started = False
                async for message in ws:
                    if isinstance(message, bytes):
                        audio_chunks.append(message)
                        output_emitter.push(message)
                    else:
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            continue

                        event = data.get("header", {}).get("event", "")

                        if event == "task-started":
                            task_started = True
                            logger.debug(
                                f"AliyunTTS: task-started, id={task_id}"
                            )
                            # continue-task + finish-task
                            await ws.send(json.dumps(
                                AliyunTTS._build_continue_task_msg(task_id, text)
                            ))
                            await ws.send(json.dumps(
                                AliyunTTS._build_finish_task_msg(task_id)
                            ))

                        elif event == "task-finished":
                            logger.debug(
                                f"AliyunTTS: task-finished, id={task_id}, "
                                f"audio={len(audio_chunks)} chunks"
                            )
                            break

                        elif event == "task-failed":
                            error_msg = data.get("header", {}).get(
                                "error_message", "unknown"
                            )
                            logger.error(
                                f"AliyunTTS: task-failed, id={task_id}, "
                                f"msg={error_msg}"
                            )
                            break

                if not task_started:
                    logger.error(
                        f"AliyunTTS: 未收到 task-started, id={task_id}"
                    )
                    return b""

        except websockets.exceptions.WebSocketException as e:
            logger.error(f"AliyunTTS: WebSocket 异常: {e}")
            return b""
        except asyncio.TimeoutError:
            logger.error(f"AliyunTTS: WebSocket 超时, id={task_id}")
            return b""
        except Exception as e:
            logger.error(f"AliyunTTS: 合成异常: {e}", exc_info=True)
            return b""

        pcm_data = b"".join(audio_chunks)
        if pcm_data:
            duration_ms = len(pcm_data) / (
                self._tts_instance._sample_rate * 2
            ) * 1000
            logger.info(
                f"AliyunTTS: 合成完成, text={text[:30]}..., "
                f"audio={len(pcm_data)} bytes, duration={duration_ms:.0f}ms"
            )
        return pcm_data

    # ── 缓存读写 (同步，用于 run_in_executor) ──────────────

    @staticmethod
    def _read_cache_file(path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    @staticmethod
    def _write_cache_file(path: str, data: bytes) -> None:
        with open(path, "wb") as f:
            f.write(data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SynthesizeStream — 流式合成 (stream)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class AliyunTTSStreamAdapter(tts.SynthesizeStream):
    """
    流式 TTS — 支持逐句输入文本，实时输出音频流

    工作流程:
      1. 从 _input_ch 读取文本片段
      2. 对每个片段建立独立的 WebSocket 连接进行合成
      3. 通过 output_emitter.push() 推送音频数据
      4. 使用 start_segment / end_segment 管理段落
    """

    def __init__(
        self,
        *,
        tts: AliyunTTS,
        conn_options: APIConnectOptions,
    ):
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts_instance: AliyunTTS = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        """SynthesizeStream 核心方法 — 处理输入文本流并输出音频流

        关键修复：LiveKit Agents SDK 会将文本按标点拆分为多个片段
        （例如将 "您好？" 拆为 "您好" + "？"），CosyVoice API 会拒绝
        纯标点片段。因此需要：
        1. 过滤掉纯标点文本片段（避免 InvalidParameter 错误）
        2. 将多个有效文本片段合并为一个合成请求（减少 WebSocket 连接数）
        """
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._tts_instance._sample_rate,
            num_channels=1,
            stream=True,  # 流式模式
            mime_type="audio/pcm",
        )

        # 收集所有有效文本片段，最后合并为一次合成
        # 这样可以避免 SDK 拆分文本导致的纯标点片段问题
        text_parts: list[str] = []
        got_flush = False

        async for data in self._input_ch:
            if isinstance(data, self._FlushSentinel):
                got_flush = True
                continue

            # data 为文本片段
            text = data.strip() if isinstance(data, str) else ""
            if not text:
                continue

            # 过滤纯标点片段（CosyVoice 会拒绝 InvalidParameter）
            if _is_punctuation_only(text):
                logger.info(
                    f"AliyunTTS stream: 跳过纯标点片段: {text!r}"
                )
                continue

            text_parts.append(text)

        # 合并所有有效文本片段为一次合成
        if text_parts:
            combined_text = " ".join(text_parts)
            segment_id = "seg_0"
            output_emitter.start_segment(segment_id=segment_id)
            logger.info(
                f"AliyunTTS stream: 合并合成, parts={len(text_parts)}, "
                f"combined_text={combined_text!r:.100}..."
            )
            await _ws_synthesize(
                self._tts_instance, combined_text, output_emitter, segment_id
            )
            output_emitter.end_segment()

        if got_flush:
            output_emitter.flush()

        # 所有文本处理完毕，_main_task 会调用 output_emitter.end_input()
