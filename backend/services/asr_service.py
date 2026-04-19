"""
ASR 语音识别服务
支持：FunASR 本地、Qwen 实时语音识别（百炼 dashscope SDK）
"""
import asyncio
import json
import logging
import os
import re
import struct
import time
import uuid
import wave
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional

import websockets

from backend.core.config import config, ASRConfig

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 公共
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ASRResult:
    """ASR 识别结果"""
    def __init__(self, text: str, is_final: bool = False, confidence: float = 1.0):
        self.text = text.strip()
        self.is_final = is_final
        self.confidence = confidence

    def __repr__(self):
        flag = "✓" if self.is_final else "..."
        return f"ASRResult({flag} '{self.text}')"


class BaseASR(ABC):
    """ASR 基类"""

    @abstractmethod
    async def recognize_stream(
        self, audio_gen: AsyncGenerator[bytes, None],
        call_uuid: Optional[str] = None,
    ) -> AsyncGenerator[ASRResult, None]:
        """
        流式识别
        输入: 音频数据生成器（PCM bytes）
        输出: 识别结果生成器
        call_uuid: 通话 UUID（用于音频文件命名，便于追溯）
        """
        pass

    async def recognize_once(self, audio_data: bytes, call_uuid: Optional[str] = None) -> str:
        results = []

        async def _gen():
            yield audio_data
            yield b""  # 结束信号

        async for result in self.recognize_stream(_gen(), call_uuid=call_uuid):
            if result.is_final:
                results.append(result.text)

        return " ".join(results)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FunASR 本地服务
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FunASRClient(BaseASR):
    """
    FunASR 本地部署客户端 — WebSocket 流式接口
    项目: https://github.com/modelscope/FunASR
    """

    def __init__(self, cfg: ASRConfig):
        self.host = cfg.funasr_host
        self.port = cfg.funasr_port
        self.sample_rate = cfg.sample_rate
        self.vad_silence_ms = cfg.vad_silence_ms
        self._ws_uri = f"ws://{self.host}:{self.port}"

    async def recognize_stream(
        self, audio_gen: AsyncGenerator[bytes, None],
        call_uuid: Optional[str] = None,
    ) -> AsyncGenerator[ASRResult, None]:
        try:
            async with websockets.connect(
                self._ws_uri, ping_interval=None, max_size=10 * 1024 * 1024,
            ) as ws:
                # 发送配置
                config_msg = {
                    "mode": "2pass",
                    "chunk_size": [5, 10, 5],
                    "chunk_interval": 10,
                    "wav_name": "stream",
                    "is_speaking": True,
                    "hotwords": "",
                    "itn": True,
                }
                await ws.send(json.dumps(config_msg))

                # 并发：发送音频 + 接收结果
                send_task = asyncio.create_task(self._send_audio(ws, audio_gen))

                async for message in ws:
                    try:
                        data = json.loads(message)
                        text = data.get("text", "").strip()
                        is_final = data.get("is_final", False)
                        mode = data.get("mode", "")
                        if text:
                            yield ASRResult(
                                text=text, is_final=is_final or mode == "2pass-offline",
                                confidence=data.get("confidence", 1.0),
                            )
                    except json.JSONDecodeError:
                        pass

                await send_task

        except (websockets.exceptions.WebSocketException, OSError) as e:
            logger.error(f"FunASR 连接失败: {e}")
            yield ASRResult(text="", is_final=True)

    async def _send_audio(self, ws, audio_gen):
        chunk_size = 960  # 60ms @ 8000Hz 16bit
        buffer = b""
        async for chunk in audio_gen:
            if not chunk:
                break
            buffer += chunk
            while len(buffer) >= chunk_size:
                await ws.send(buffer[:chunk_size])
                buffer = buffer[chunk_size:]
                await asyncio.sleep(0)
        if buffer:
            await ws.send(buffer)
        await ws.send(json.dumps({"is_speaking": False}))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Qwen 实时语音识别（百炼平台，dashscope OmniRealtimeConversation）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class QwenRealtimeASRClient(BaseASR):
    """
    Qwen 实时语音识别 — dashscope SDK OmniRealtimeConversation 接口
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    文档: https://help.aliyun.com/zh/model-studio/qwen-real-time-speech-recognition

    使用 OmniRealtimeConversation 进行流式识别：
      1. conversation.connect() 建立 WebSocket 连接
      2. conversation.update_session() 配置转录参数
      3. conversation.append_audio(base64) 发送音频帧
      4. OmniRealtimeCallback.on_event() 接收识别结果
      5. conversation.end_session() + close() 关闭

    音频: PCM 16-bit, 16kHz, mono（8kHz 需上采样）
    模型: qwen3-asr-flash-realtime
    """

    WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    SEND_SAMPLE_RATE = 16000  # Qwen ASR 要求 16kHz

    def __init__(self, cfg: ASRConfig):
        self.api_key = cfg.bailian_access_token
        self.model_name = cfg.qwen_asr_model or "qwen3-asr-flash-realtime"
        self.input_sample_rate = cfg.sample_rate  # 通常 8000Hz

    @staticmethod
    def _upsample_8k_to_16k(pcm_8k: bytes) -> bytes:
        """8kHz → 16kHz 线性插值上采样"""
        if len(pcm_8k) < 2:
            return pcm_8k
        n = len(pcm_8k) // 2
        samples = struct.unpack(f'<{n}h', pcm_8k[:n * 2])
        out = []
        for i in range(n):
            out.append(samples[i])
            if i < n - 1:
                out.append((samples[i] + samples[i + 1]) // 2)
        out.append(samples[-1])
        return struct.pack(f'<{len(out)}h', *out)

    async def recognize_stream(
        self, audio_gen: AsyncGenerator[bytes, None],
        call_uuid: Optional[str] = None,
    ) -> AsyncGenerator[ASRResult, None]:
        import base64
        import queue as _queue

        try:
            from dashscope.audio.qwen_omni import (
                MultiModality, OmniRealtimeCallback, OmniRealtimeConversation,
            )
            from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams
        except ImportError:
            logger.error("Qwen ASR: dashscope SDK 未安装或版本过低")
            yield ASRResult(text="", is_final=True)
            return

        if not self.api_key:
            logger.error("Qwen ASR: BAILIAN_ACCESS_TOKEN 未配置")
            yield ASRResult(text="", is_final=True)
            return

        import dashscope
        dashscope.api_key = self.api_key

        # 捕获原始音频用于调试
        dump_dir = "/recordings/debug"
        os.makedirs(dump_dir, exist_ok=True)
        dump_list = []
        final_text = ""

        result_queue: _queue.Queue = _queue.Queue(maxsize=200)

        class _Callback(OmniRealtimeCallback):
            def __init__(self):
                self.conversation = None  # 稍后注入

            def on_open(self):
                logger.debug(f"Qwen ASR: WebSocket 连接已建立, model={self._outer.model_name}")

            def on_event(self, response):
                try:
                    event_type = response.get('type', '')

                    if event_type == 'conversation.item.input_audio_transcription.text':
                        # 中间结果（实时转录文本）
                        text = (response.get('text', '') + response.get('stash', '')).strip()
                        if text:
                            try:
                                result_queue.put_nowait(ASRResult(text=text, is_final=False))
                            except _queue.Full:
                                pass

                    elif event_type == 'conversation.item.input_audio_transcription.completed':
                        # 最终结果
                        text = response.get('transcript', '').strip()
                        if text:
                            try:
                                result_queue.put_nowait(ASRResult(text=text, is_final=True))
                            except _queue.Full:
                                pass

                    elif event_type == 'input_audio_buffer.speech_started':
                        logger.debug("Qwen ASR: 检测到语音开始")

                    elif event_type == 'input_audio_buffer.speech_stopped':
                        logger.debug("Qwen ASR: 检测到语音结束")

                    elif event_type == 'session.created':
                        sid = response.get('session', {}).get('id', '')
                        logger.debug(f"Qwen ASR: session created, id={sid}")

                except Exception as e:
                    logger.error(f"Qwen ASR on_event 异常: {e}")

            def on_close(self, code, msg):
                logger.debug(f"Qwen ASR: WebSocket 关闭, code={code}, msg={msg}")
                try:
                    result_queue.put_nowait(None)  # 哨兵
                except _queue.Full:
                    pass

        callback = _Callback()
        callback._outer = self

        conversation = OmniRealtimeConversation(
            model=self.model_name,
            url=self.WS_URL,
            callback=callback,
        )
        callback.conversation = conversation

        loop = asyncio.get_event_loop()

        try:
            # 建立连接
            await loop.run_in_executor(None, conversation.connect)

            # 配置转录参数
            transcription_params = TranscriptionParams(
                language='zh',
                sample_rate=self.SEND_SAMPLE_RATE,
                input_audio_format="pcm",
            )
            conversation.update_session(
                output_modalities=[MultiModality.TEXT],
                enable_input_audio_transcription=True,
                enable_turn_detection=False,  # 禁用对话模式，只做纯转录
                transcription_params=transcription_params,
            )

            # 发送音频
            async def _send():
                buffer = b""
                input_chunk_count = 0
                total_input_bytes = 0
                upsample_count = 0

                # 每次发送 3200 bytes（约 100ms @ 16kHz 16bit mono）
                send_chunk_size = 3200

                async for chunk in audio_gen:
                    if not chunk:
                        break

                    input_chunk_count += 1
                    total_input_bytes += len(chunk)
                    dump_list.append(chunk)

                    # 上采样 8k → 16k
                    if self.input_sample_rate < self.SEND_SAMPLE_RATE:
                        chunk_16k = self._upsample_8k_to_16k(chunk)
                        upsample_count += 1
                    else:
                        chunk_16k = chunk
                    buffer += chunk_16k

                    while len(buffer) >= send_chunk_size:
                        audio_b64 = base64.b64encode(buffer[:send_chunk_size]).decode('ascii')
                        conversation.append_audio(audio_b64)
                        buffer = buffer[send_chunk_size:]
                        await asyncio.sleep(0.05)  # 50ms 间隔

                # 发送剩余数据
                if buffer:
                    audio_b64 = base64.b64encode(buffer).decode('ascii')
                    conversation.append_audio(audio_b64)

                total_input_ms = total_input_bytes / (self.input_sample_rate * 2) * 1000
                logger.info(
                    f"Qwen ASR: 音频发送完毕, "
                    f"输入 {input_chunk_count} chunks / {total_input_bytes} bytes ({total_input_ms:.0f}ms @ {self.input_sample_rate}Hz), "
                    f"上采样 {upsample_count}/{input_chunk_count}"
                )

            send_task = asyncio.create_task(_send())

            # 接收结果
            result_count = 0
            t0 = time.time()
            while True:
                try:
                    item = await asyncio.wait_for(
                        loop.run_in_executor(None, result_queue.get, True, 15.0),
                        timeout=20.0,
                    )
                except asyncio.TimeoutError:
                    elapsed = time.time() - t0
                    logger.warning(f"Qwen ASR 接收超时 (已等待 {elapsed:.1f}s)")
                    break

                if item is None:  # 哨兵
                    break
                result_count += 1
                yield item
                if item.is_final:
                    final_text = item.text

            elapsed = time.time() - t0
            logger.info(
                f"Qwen ASR: 识别会话结束, 总耗时 {elapsed:.1f}s, "
                f"产出 {result_count} 条结果, 最终文本={final_text!r}"
            )

        except Exception as e:
            logger.error(f"Qwen ASR 异常: {e}", exc_info=True)
            yield ASRResult(text="", is_final=True)
        finally:
            try:
                conversation.end_session()
            except Exception:
                pass
            try:
                conversation.close()
            except Exception:
                pass
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)

        # 保存调试音频
        if dump_list:
            try:
                dump_pcm = b"".join(dump_list)
                dur = len(dump_pcm) / (self.input_sample_rate * 2)
                text_tag = re.sub(r'[^\w\u4e00-\u9fff]', '', final_text)[:20] if final_text else "silent"
                prefix = call_uuid[:8] if call_uuid else f"asr_{int(time.time())}"
                dump_path = os.path.join(dump_dir, f"asr_{prefix}_{text_tag}_{uuid.uuid4().hex[:4]}.wav")
                with wave.open(dump_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(self.input_sample_rate)
                    wf.writeframes(dump_pcm)
                logger.info(f"Qwen ASR 音频已保存: {dump_path} ({len(dump_pcm)} bytes, {dur:.1f}s)")
            except Exception as e:
                logger.warning(f"Qwen ASR WAV 保存失败: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 工厂
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def create_asr_client(cfg: Optional[ASRConfig] = None) -> BaseASR:
    """工厂函数：根据配置创建对应的 ASR 客户端"""
    cfg = cfg or config.asr

    if cfg.provider == "funasr_local":
        logger.info("使用 FunASR 本地服务")
        return FunASRClient(cfg)
    elif cfg.provider == "qwen":
        logger.info(f"使用 Qwen 实时语音识别 (model={cfg.qwen_asr_model})")
        return QwenRealtimeASRClient(cfg)
    else:
        raise ValueError(
            f"不支持的 ASR 提供商: {cfg.provider}。"
            f"支持的选项: funasr_local, qwen"
        )
