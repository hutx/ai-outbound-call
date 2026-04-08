"""
测试通话支持模块
抽离 main.py 中的 MockESL 会话与后台执行逻辑，降低入口文件复杂度。
"""

import asyncio
import logging
import os
from typing import Optional

from backend.core.state_machine import CallResult

logger = logging.getLogger(__name__)


class MockESLCallSession:
    """模拟 ESL 通话会话，用于测试 AI 通话功能。"""

    def __init__(self, uuid: str, channel_vars: dict, asr_client, tts_client):
        self.uuid = uuid
        self.channel_vars = channel_vars
        self.asr_client = asr_client
        self.tts_client = tts_client
        self._connected = True
        self._hangup_cause = ""
        self._sip_code = 200
        self.call_state = "ACTIVE"
        self.ws_connection = None
        self._playback_done = asyncio.Event()
        self._speech_active = False
        self._event_queue = asyncio.Queue(maxsize=200)
        self._audio_queue = asyncio.Queue(maxsize=500)
        self._ws_ready = asyncio.Event()

    async def connect(self):
        logger.info(f"Mock ESL connected for call {self.uuid}")
        self._connected = True
        return self.channel_vars

    async def answer(self):
        logger.info(f"Mock call {self.uuid} answered")
        self.call_state = "ANSWERED"
        self._sip_code = 200

    async def hangup(self, cause="NORMAL_CLEARING"):
        logger.info(f"Mock call {self.uuid} hung up: {cause}")
        self._connected = False
        self._hangup_cause = cause
        self.call_state = "HUNG_UP"
        await self._safe_put(self._event_queue, {"type": "hangup", "cause": cause})

    async def playback(self, file_path: str):
        logger.info(f"Mock playback {file_path} in call {self.uuid}")

    async def play(self, audio_path: str, timeout: float = 60.0):
        logger.info(f"Mock play {audio_path} in call {self.uuid}")
        self._playback_done.clear()
        self._speech_active = True

        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"[{self.uuid}] play() 等待 WebSocket 超时，跳过发送")

        if self.ws_connection:
            text_to_send = getattr(self, "_current_tts_text", "")
            if text_to_send:
                try:
                    await self.ws_connection.send_json(
                        {"type": "ai_response", "text": text_to_send, "has_audio": True}
                    )
                except Exception:
                    pass
                try:
                    import aiofiles

                    async with aiofiles.open(audio_path, "rb") as f:
                        audio_data = await f.read()
                    await self.ws_connection.send_bytes(audio_data)
                except Exception as e:
                    logger.warning(f"[{self.uuid}] play() 发送音频失败: {e}")

        estimated_duration = self._estimate_wav_duration(audio_path)
        await self._wait_for_playback_finish(
            estimated_duration_sec=estimated_duration,
            timeout=timeout,
            mode="file",
        )
        self._speech_active = False
        self._playback_done.set()

    async def stop_playback(self):
        logger.info(f"Mock stop playback in call {self.uuid}")
        self._playback_done.set()

    def notify_playback_finished(self):
        """前端确认浏览器侧播放完成。"""
        if not self._playback_done.is_set():
            logger.info(f"[{self.uuid}] 收到前端播放完成确认")
            self._playback_done.set()

    async def play_stream(self, audio_chunks, text: str = "", timeout: float = 60.0):
        self._playback_done.clear()
        self._speech_active = True
        chunk_count = 0
        total_bytes = 0

        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"[{self.uuid}] play_stream() 等待 WebSocket 超时")
            self._speech_active = False
            self._playback_done.set()
            return

        if self.ws_connection:
            try:
                await self.ws_connection.send_json(
                    {
                        "type": "ai_response",
                        "text": text,
                        "has_audio": True,
                        "streaming": True,
                    }
                )
            except Exception:
                pass

            try:
                async with asyncio.timeout(timeout):
                    async for chunk in audio_chunks:
                        if self._playback_done.is_set():
                            logger.info(f"[{self.uuid}] play_stream() 已收到停止信号，终止发送")
                            break
                        if chunk and self.ws_connection:
                            chunk_count += 1
                            total_bytes += len(chunk)
                            await self.ws_connection.send_bytes(chunk)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.uuid}] play_stream() 超时")
            except Exception as e:
                logger.warning(f"[{self.uuid}] play_stream() 发送失败: {e}")

            try:
                duration_ms = int((total_bytes / 2 / 8000) * 1000) if total_bytes else 0
                logger.info(
                    f"[{self.uuid}] play_stream() 完成: text_len={len(text)} "
                    f"chunks={chunk_count} bytes={total_bytes} duration_ms≈{duration_ms}"
                )
                await self.ws_connection.send_json(
                    {
                        "type": "streaming_audio_done",
                        "chunks": chunk_count,
                        "bytes": total_bytes,
                        "estimated_duration_ms": duration_ms,
                    }
                )
            except Exception:
                pass

        estimated_duration = (total_bytes / 2 / 8000) if total_bytes else 0.0
        await self._wait_for_playback_finish(
            estimated_duration_sec=estimated_duration,
            timeout=timeout,
            mode="stream",
        )

        self._speech_active = False
        self._playback_done.set()

    async def speak_text(self, text: str) -> str:
        logger.info(f"Mock TTS: {text}")
        await asyncio.sleep(len(text) * 0.05)
        if self.ws_connection:
            try:
                await self.ws_connection.send_json({"type": "ai_response", "text": text})
            except Exception:
                pass
        return f"MOCK_TTS_{self.uuid}"

    async def record_audio(self, timeout: int = 10) -> tuple[bool, str]:
        logger.info(f"Mock recording in call {self.uuid}, timeout: {timeout}s")
        await asyncio.sleep(timeout)
        return False, ""

    def get_variable(self, var_name: str) -> str:
        return self.channel_vars.get(var_name, "")

    async def set_variable(self, var_name: str, value: str):
        self.channel_vars[var_name] = value
        logger.info(f"Set channel var {var_name}={value} in call {self.uuid}")

    async def start_audio_capture(self) -> asyncio.Queue:
        logger.info(f"Mock start audio capture in call {self.uuid}")
        return self._audio_queue

    async def read_events(self):
        logger.info(f"Mock read events for call {self.uuid}")
        while self._connected:
            await asyncio.sleep(1)

    async def transfer_to_human(self, extension: str = "8001"):
        logger.info(f"Mock transfer to human {extension} for call {self.uuid}")
        await self.hangup("TRANSFER_TO_HUMAN")

    async def wait_for_hangup(self, timeout: float = 3600.0) -> str:
        try:
            event = await asyncio.wait_for(
                self.wait_for_event("hangup"), timeout=timeout
            )
            return event.get("cause", "UNKNOWN") if event else "UNKNOWN"
        except asyncio.TimeoutError:
            return "TIMEOUT"

    async def wait_for_event(
        self, event_type: str, timeout: float = 30.0
    ) -> Optional[dict]:
        try:
            event = await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
            if event.get("type") == event_type:
                return event
            return None
        except asyncio.TimeoutError:
            return None

    @staticmethod
    async def _safe_put(queue: asyncio.Queue, item):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    async def _wait_for_playback_finish(
        self,
        *,
        estimated_duration_sec: float,
        timeout: float,
        mode: str,
    ):
        """等待浏览器真正播完，避免测试通话链路过早进入下一轮。"""
        if self._playback_done.is_set():
            return

        wait_timeout = min(timeout, max(1.5, estimated_duration_sec + 2.0))
        try:
            await asyncio.wait_for(self._playback_done.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                f"[{self.uuid}] {mode} 播放等待前端确认超时 "
                f"(estimate={estimated_duration_sec:.2f}s, timeout={wait_timeout:.2f}s)"
            )

    @staticmethod
    def _estimate_wav_duration(audio_path: str) -> float:
        try:
            file_size = os.path.getsize(audio_path)
        except OSError:
            return 0.0

        if file_size <= 44:
            return 0.0

        pcm_bytes = file_size - 44
        return pcm_bytes / (8000 * 2)


async def run_test_call_with_agent(
    agent,
    call_uuid: str,
    active_calls: dict[str, object],
    metrics: dict[str, int],
    broadcast_stats,
):
    """在后台运行测试通话，复用真实的 CallAgent 生命周期。"""
    try:
        logger.info(f"测试通话 {call_uuid} 已启动")
        await agent.run()
    except Exception as e:
        logger.error(f"测试通话 {call_uuid} 出现异常: {e}", exc_info=True)
    finally:
        active_calls.pop(call_uuid, None)
        metrics["calls_active"] -= 1

        ctx = agent.ctx
        if ctx.result == CallResult.COMPLETED:
            metrics["calls_completed"] += 1
        elif ctx.result == CallResult.TRANSFERRED:
            metrics["calls_transferred"] += 1
        elif ctx.result == CallResult.ERROR:
            metrics["calls_error"] += 1

        await broadcast_stats()
