"""阿里云 Paraformer 实时语音识别 - LiveKit STT 插件

基于阿里云百炼平台 Paraformer 实时语音识别 WebSocket API 实现。
支持流式识别与非流式识别，适配 LiveKit Agents 0.12.x STT 接口。

协议文档: https://help.aliyun.com/zh/model-studio/websocket-for-paraformer-real-time-service
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import uuid

import websockets

from livekit.agents import stt
from livekit.agents.stt import SpeechData, SpeechEvent, SpeechEventType, STTCapabilities
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr
from livekit.agents.utils import AudioBuffer, is_given
from livekit import rtc

from livekit_backend.core.config import settings

logger = logging.getLogger(__name__)

# Paraformer WebSocket 协议默认采样率
_PARAFORMER_SAMPLE_RATE = 16000
# 每次 WebSocket 发送的音频 chunk 大小（约 100ms @ 16kHz 16bit mono = 3200 bytes）
_SEND_CHUNK_SIZE = 3200
# WebSocket 接收超时（秒）
_RECV_TIMEOUT = 30.0
# 断线重连最大次数
_MAX_RECONNECT_ATTEMPTS = 3
# 重连间隔（秒）
_RECONNECT_INTERVAL = 2.0


def _upsample_8k_to_16k(pcm_8k: bytes) -> bytes:
    """8kHz → 16kHz 线性插值上采样"""
    if len(pcm_8k) < 2:
        return pcm_8k
    n = len(pcm_8k) // 2
    samples = struct.unpack(f"<{n}h", pcm_8k[: n * 2])
    out: list[int] = []
    for i in range(n):
        out.append(samples[i])
        if i < n - 1:
            out.append((samples[i] + samples[i + 1]) // 2)
    out.append(samples[-1])
    return struct.pack(f"<{len(out)}h", *out)


def _build_run_task_message(
    task_id: str,
    model: str,
    sample_rate: int = 16000,
    format: str = "pcm",
    language_hints: list[str] | None = None,
) -> str:
    """构造 Paraformer run-task 指令"""
    parameters: dict = {
        "format": format,
        "sample_rate": sample_rate,
    }
    if language_hints:
        parameters["language_hints"] = language_hints

    msg = {
        "header": {
            "action": "run-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": model,
            "parameters": parameters,
            "input": {},
        },
    }
    return json.dumps(msg)


def _build_finish_task_message(task_id: str) -> str:
    """构造 Paraformer finish-task 指令"""
    msg = {
        "header": {
            "action": "finish-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {
            "input": {},
        },
    }
    return json.dumps(msg)


class AliyunSTT(stt.STT):
    """阿里云 Paraformer 实时语音识别 — LiveKit STT 插件

    支持流式（stream）和非流式（recognize）两种模式。
    非流式模式内部仍使用 WebSocket 流式协议，收集完所有结果后返回。
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "",
        ws_url: str = "",
        language_hints: list[str] | None = None,
    ) -> None:
        super().__init__(
            capabilities=STTCapabilities(
                streaming=True,
                interim_results=True,
            )
        )
        self._api_key = api_key or settings.aliyun_asr_access_token
        self._model = model or settings.aliyun_asr_model
        self._ws_url = ws_url or settings.aliyun_asr_ws_url
        self._language_hints = language_hints or ["zh", "en"]

        if not self._api_key:
            raise ValueError(
                "AliyunSTT: API Key 未配置。"
                "请设置 aliyun_asr_access_token 配置项或传入 api_key 参数。"
            )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "aliyun"

    def _sanitize_options(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
    ) -> str:
        """处理语言选项，返回 language hints 列表"""
        if is_given(language):
            return language  # type: ignore[return-value]
        return ",".join(self._language_hints or ["zh", "en"])

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> SpeechEvent:
        """非流式识别：将完整音频发送到 Paraformer 并返回最终结果"""
        combined = rtc.combine_audio_frames(buffer)
        audio_bytes = combined.data.tobytes() if hasattr(combined.data, "tobytes") else bytes(combined.data)

        # 如果输入采样率不是 16kHz，进行重采样
        if combined.sample_rate < _PARAFORMER_SAMPLE_RATE:
            audio_bytes = _upsample_8k_to_16k(audio_bytes)
            send_sample_rate = _PARAFORMER_SAMPLE_RATE
        else:
            send_sample_rate = combined.sample_rate

        task_id = uuid.uuid4().hex
        language_hints = (
            [language] if is_given(language) and isinstance(language, str) else self._language_hints
        )

        final_text = ""
        final_confidence = 0.0

        try:
            async with websockets.connect(
                self._ws_url,
                additional_headers={
                    "Authorization": f"bearer {self._api_key}",
                },
                ping_interval=20,
                ping_timeout=10,
                max_size=10 * 1024 * 1024,
            ) as ws:
                # 发送 run-task
                run_task_msg = _build_run_task_message(
                    task_id=task_id,
                    model=self._model,
                    sample_rate=send_sample_rate,
                    language_hints=language_hints,
                )
                await ws.send(run_task_msg)

                # 等待 task-started
                task_started = False
                async for message in ws:
                    data = json.loads(message)
                    event = data.get("header", {}).get("event", "")

                    if event == "task-started":
                        task_started = True
                        # 开始发送音频
                        await self._send_audio_static(ws, audio_bytes)
                        # 发送 finish-task
                        await ws.send(_build_finish_task_message(task_id))

                    elif event == "result-generated":
                        sentence = data.get("payload", {}).get("output", {}).get("sentence", {})
                        text = sentence.get("text", "").strip()
                        sentence_end = sentence.get("sentence_end", False)
                        confidence = sentence.get("confidence", 0.0)
                        if text and sentence_end:
                            final_text = text
                            final_confidence = confidence

                    elif event == "task-finished":
                        break

                    elif event == "task-failed":
                        error_msg = data.get("header", {}).get("error_message", "unknown error")
                        logger.error(f"AliyunSTT: task-failed, {error_msg}")
                        break

                if not task_started:
                    logger.error("AliyunSTT: 未收到 task-started 事件")

        except (websockets.exceptions.WebSocketException, OSError) as e:
            logger.error(f"AliyunSTT: WebSocket 连接失败: {e}")
        except Exception as e:
            logger.error(f"AliyunSTT: 非流式识别异常: {e}", exc_info=True)

        lang = language if is_given(language) and isinstance(language, str) else "zh"
        return SpeechEvent(
            type=SpeechEventType.FINAL_TRANSCRIPT,
            request_id=task_id,
            alternatives=[
                SpeechData(
                    language=lang,  # type: ignore[arg-type]
                    text=final_text,
                    confidence=final_confidence,
                )
            ],
        )

    @staticmethod
    async def _send_audio_static(ws, audio_bytes: bytes) -> None:
        """发送完整音频数据到 WebSocket"""
        offset = 0
        while offset < len(audio_bytes):
            chunk = audio_bytes[offset : offset + _SEND_CHUNK_SIZE]
            await ws.send(chunk)
            offset += _SEND_CHUNK_SIZE
            # 模拟实时流，每次发送间隔约 50ms
            await asyncio.sleep(0.05)

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> AliyunSTTStream:
        """创建流式识别 stream"""
        return AliyunSTTStream(
            stt=self,
            api_key=self._api_key,
            model=self._model,
            ws_url=self._ws_url,
            language_hints=(
                [language] if is_given(language) and isinstance(language, str) else self._language_hints
            ),
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        """关闭 STT 实例"""
        pass


class AliyunSTTStream(stt.RecognizeStream):
    """阿里云 Paraformer 流式识别 Stream

    通过 WebSocket 连接百炼 Paraformer，持续接收音频帧并产出识别事件。
    支持断线自动重连。
    """

    def __init__(
        self,
        *,
        stt: AliyunSTT,
        api_key: str,
        model: str,
        ws_url: str,
        language_hints: list[str] | None,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=_PARAFORMER_SAMPLE_RATE)
        self._api_key = api_key
        self._model = model
        self._ws_url = ws_url
        self._language_hints = language_hints or ["zh", "en"]
        self._conn_options = conn_options
        self._task_id = uuid.uuid4().hex
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._speech_started = False
        self._reconnect_attempts = 0

    async def _run(self) -> None:
        """主运行循环 - 建立 WebSocket 连接，处理输入音频帧并产出识别事件"""
        try:
            await self._connect_and_run()
        except Exception as e:
            logger.error(f"AliyunSTTStream: 运行异常: {e}", exc_info=True)
        finally:
            await self._close_ws()

    async def _connect_and_run(self) -> None:
        """建立 WebSocket 连接并运行识别任务"""
        # 建立 WebSocket 连接
        self._ws = await self._connect_ws()

        # 发送 run-task 指令
        run_task_msg = _build_run_task_message(
            task_id=self._task_id,
            model=self._model,
            sample_rate=_PARAFORMER_SAMPLE_RATE,
            language_hints=self._language_hints,
        )
        await self._ws.send(run_task_msg)

        # 等待 task-started 事件
        task_started = False
        try:
            async for message in self._ws:
                data = json.loads(message)
                event = data.get("header", {}).get("event", "")
                if event == "task-started":
                    task_started = True
                    logger.debug(f"AliyunSTTStream: task-started, task_id={self._task_id}")
                    break
                elif event == "task-failed":
                    error_msg = data.get("header", {}).get("error_message", "unknown")
                    logger.error(f"AliyunSTTStream: task-failed before start, {error_msg}")
                    return
        except websockets.exceptions.WebSocketException as e:
            logger.error(f"AliyunSTTStream: 等待 task-started 时连接断开: {e}")
            return

        if not task_started:
            logger.error("AliyunSTTStream: 未收到 task-started 事件")
            return

        # 启动两个并发任务：发送音频 + 接收结果
        send_task = asyncio.create_task(self._send_audio_loop())
        recv_task = asyncio.create_task(self._recv_results_loop())

        try:
            # 等待任一任务完成（通常是接收任务先结束）
            done, pending = await asyncio.wait(
                [send_task, recv_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 取消未完成的任务
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        except Exception as e:
            logger.error(f"AliyunSTTStream: 并发任务异常: {e}", exc_info=True)
            send_task.cancel()
            recv_task.cancel()

    async def _connect_ws(self) -> websockets.WebSocketClientProtocol:
        """建立 WebSocket 连接，支持重试"""
        last_error = None
        for attempt in range(self._conn_options.max_retry + 1):
            try:
                ws = await websockets.connect(
                    self._ws_url,
                    additional_headers={
                        "Authorization": f"bearer {self._api_key}",
                    },
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=10 * 1024 * 1024,
                )
                logger.debug(f"AliyunSTTStream: WebSocket 连接成功 (attempt={attempt + 1})")
                self._reconnect_attempts = 0
                return ws
            except (websockets.exceptions.WebSocketException, OSError) as e:
                last_error = e
                logger.warning(
                    f"AliyunSTTStream: WebSocket 连接失败 (attempt={attempt + 1}): {e}"
                )
                if attempt < self._conn_options.max_retry:
                    await asyncio.sleep(self._conn_options.retry_interval)

        raise ConnectionError(f"AliyunSTTStream: WebSocket 连接失败，已重试 {self._conn_options.max_retry + 1} 次: {last_error}")

    async def _send_audio_loop(self) -> None:
        """从 input_ch 读取 AudioFrame 并发送到 WebSocket"""
        assert self._ws is not None

        try:
            # 使用 RecognizeStream 的 _input_ch 读取音频帧
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    # flush 信号：发送 finish-task 并等待剩余结果
                    await self._ws.send(_build_finish_task_message(self._task_id))
                    continue

                frame: rtc.AudioFrame = item
                # 基类 RecognizeStream 已处理重采样到 16kHz
                audio_bytes = frame.data.tobytes() if hasattr(frame.data, "tobytes") else bytes(frame.data)

                # 分块发送
                offset = 0
                while offset < len(audio_bytes):
                    chunk = audio_bytes[offset : offset + _SEND_CHUNK_SIZE]
                    try:
                        await self._ws.send(chunk)
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("AliyunSTTStream: 发送音频时连接已关闭")
                        return
                    offset += _SEND_CHUNK_SIZE
                    # 控制发送速率，避免过快
                    await asyncio.sleep(0.02)

        except websockets.exceptions.WebSocketException as e:
            logger.warning(f"AliyunSTTStream: 发送音频时连接断开: {e}")
            # 尝试重连
            await self._try_reconnect()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"AliyunSTTStream: 发送音频异常: {e}", exc_info=True)

    async def _recv_results_loop(self) -> None:
        """从 WebSocket 接收识别结果并发送 SpeechEvent"""
        assert self._ws is not None

        try:
            async for message in self._ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning(f"AliyunSTTStream: 无法解析消息: {message[:200]}")
                    continue

                event = data.get("header", {}).get("event", "")

                if event == "result-generated":
                    await self._handle_result_generated(data)
                elif event == "task-finished":
                    logger.debug(f"AliyunSTTStream: task-finished, task_id={self._task_id}")
                    # 发送 END_OF_SPEECH
                    if self._speech_started:
                        self._event_ch.send_nowait(
                            SpeechEvent(
                                type=SpeechEventType.END_OF_SPEECH,
                                request_id=self._task_id,
                            )
                        )
                        self._speech_started = False
                    break
                elif event == "task-failed":
                    error_msg = data.get("header", {}).get("error_message", "unknown")
                    error_code = data.get("header", {}).get("error_code", "")
                    logger.error(
                        f"AliyunSTTStream: task-failed, code={error_code}, msg={error_msg}"
                    )
                    break
                else:
                    logger.debug(f"AliyunSTTStream: 未知事件: {event}")

        except websockets.exceptions.WebSocketException as e:
            logger.warning(f"AliyunSTTStream: 接收结果时连接断开: {e}")
            # 尝试重连
            await self._try_reconnect()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"AliyunSTTStream: 接收结果异常: {e}", exc_info=True)

    async def _handle_result_generated(self, data: dict) -> None:
        """处理 result-generated 事件，发送对应的 SpeechEvent"""
        sentence = data.get("payload", {}).get("output", {}).get("sentence", {})
        text = sentence.get("text", "").strip()
        sentence_end = sentence.get("sentence_end", False)
        begin_time = sentence.get("begin_time", 0)
        end_time = sentence.get("end_time", 0)
        confidence = sentence.get("confidence", 0.0)

        if not text:
            return

        lang = self._language_hints[0] if self._language_hints else "zh"

        # 语音开始事件（首次识别到内容时）
        if not self._speech_started:
            self._speech_started = True
            self._event_ch.send_nowait(
                SpeechEvent(
                    type=SpeechEventType.START_OF_SPEECH,
                    request_id=self._task_id,
                )
            )

        if sentence_end:
            # 最终结果
            self._event_ch.send_nowait(
                SpeechEvent(
                    type=SpeechEventType.FINAL_TRANSCRIPT,
                    request_id=self._task_id,
                    alternatives=[
                        SpeechData(
                            language=lang,  # type: ignore[arg-type]
                            text=text,
                            start_time=begin_time / 1000.0 if begin_time else 0.0,
                            end_time=end_time / 1000.0 if end_time else 0.0,
                            confidence=confidence,
                        )
                    ],
                )
            )
            # sentence_end 也意味着一段话结束
            self._speech_started = False
            self._event_ch.send_nowait(
                SpeechEvent(
                    type=SpeechEventType.END_OF_SPEECH,
                    request_id=self._task_id,
                )
            )
        else:
            # 中间结果
            self._event_ch.send_nowait(
                SpeechEvent(
                    type=SpeechEventType.INTERIM_TRANSCRIPT,
                    request_id=self._task_id,
                    alternatives=[
                        SpeechData(
                            language=lang,  # type: ignore[arg-type]
                            text=text,
                            start_time=begin_time / 1000.0 if begin_time else 0.0,
                            end_time=end_time / 1000.0 if end_time else 0.0,
                            confidence=confidence,
                        )
                    ],
                )
            )

    async def _try_reconnect(self) -> None:
        """断线重连"""
        if self._reconnect_attempts >= _MAX_RECONNECT_ATTEMPTS:
            logger.error(
                f"AliyunSTTStream: 已达最大重连次数 ({_MAX_RECONNECT_ATTEMPTS})，放弃重连"
            )
            return

        self._reconnect_attempts += 1
        logger.info(
            f"AliyunSTTStream: 尝试重连 ({self._reconnect_attempts}/{_MAX_RECONNECT_ATTEMPTS})..."
        )

        await self._close_ws()
        await asyncio.sleep(_RECONNECT_INTERVAL)

        try:
            self._task_id = uuid.uuid4().hex
            await self._connect_and_run()
        except Exception as e:
            logger.error(f"AliyunSTTStream: 重连失败: {e}")

    async def _close_ws(self) -> None:
        """安全关闭 WebSocket 连接"""
        if self._ws is not None:
            try:
                # websockets 15.x: 使用 close_code 检查连接状态
                if self._ws.close_code is None:
                    await self._ws.close()
            except Exception:
                pass
            self._ws = None
