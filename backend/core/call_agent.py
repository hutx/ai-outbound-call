"""
CallAgent — 生产级通话代理
串联 ESL ↔ ASR ↔ LLM ↔ TTS 的完整生命周期

生产特性：
  - 从 ESL channel 变量读取 task_id / script_id / phone（不硬编码）
  - ASR / LLM / TTS 三级错误降级 + 超时保护
  - barge-in：用户说话时自动打断 AI 播报
  - 通话最大时长限制（防超时挂死）
  - 结束时写 CDR + 更新 Redis 任务计数器
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Optional, AsyncGenerator

from backend.core.config import config
from backend.core.state_machine import CallState, CallResult, CallContext, StateMachine
from backend.services.asr_service import BaseASR, create_asr_client, ASRResult
from backend.services.tts_service import BaseTTS, create_tts_client
from backend.services.llm_service import LLMService
from backend.services.esl_service import ESLSocketCallSession, ESLError
from backend.services.crm_service import crm
from backend.utils.db import save_call_record
from backend.utils.audio import SimpleVAD
from backend.services.async_script_utils import get_system_prompt_for_call, get_opening_for_call, get_barge_in_config
from backend.services.script_service import script_service

logger = logging.getLogger(__name__)

# 单路通话最长时长（秒），防止 LLM 循环或静音时挂死
MAX_CALL_DURATION = int(config.__dict__.get("max_call_duration", 300))
# 用户无响应后最大重问次数
MAX_SILENCE_RETRIES = 3
# LLM 超时（秒）
LLM_TIMEOUT = 30.0
# TTS 超时（秒）
TTS_TIMEOUT = 10.0
# ASR 单句最长等待（秒）
ASR_TIMEOUT = 12.0


class AudioStreamAdapter:
    """
    将 FreeSWITCH 音频 Queue 转为 ASR 可消费的 AsyncGenerator
    支持 barge-in 检测：AI 播报期间检测到有声帧即触发打断

    两段式 VAD：
    - 首次等待超时（2s）：给用户提供时间开始说话
    - 说话中 VAD（500ms）：用户说话后，停顿 500ms 即认为说完

    打断保护期：
    - barge_in_enabled=False：完全不允许打断
    - protect_start_ms：播放前 N 毫秒不打断
    - protect_end_ms：播放最后 N 毫秒不打断
    """

    def __init__(
        self,
        audio_queue: asyncio.Queue,
        vad_silence_ms: int = 500,
        barge_in_cb: Optional[asyncio.Event] = None,
        barge_in_enabled: bool = True,
        total_duration_ms: float = 0,
        protect_start_ms: int = 3000,
        protect_end_ms: int = 3000,
    ):
        self._queue = audio_queue
        self._vad_silence_ms = vad_silence_ms
        self._barge_in_event = barge_in_cb
        self._stopped = False
        self._started_speaking = False  # 是否已经检测到用户开始说话
        # 打断策略
        self._barge_in_enabled = barge_in_enabled
        self._total_duration_ms = total_duration_ms
        self._protect_start_ms = protect_start_ms
        self._protect_end_ms = protect_end_ms
        self._play_start_time: Optional[float] = None  # 播放开始时间
        self._listen_start_time: Optional[float] = None
        self._silence_ms = 0.0
        self._total_chunks = 0
        self._speech_chunks = 0
        self._speech_ms = 0.0
        self._max_rms = 0
        self._vad = SimpleVAD(
            sample_rate=8000,
            frame_ms=20,
            energy_threshold=250,
            speech_min_frames=1,
            silence_min_frames=max(1, int(vad_silence_ms / 20)),
        )

    def stop(self):
        self._stopped = True

    def audio_stats(self) -> dict:
        return {
            "total_chunks": self._total_chunks,
            "speech_chunks": self._speech_chunks,
            "speech_ms": round(self._speech_ms, 1),
            "max_rms": self._max_rms,
            "speech_detected": self._speech_chunks > 0,
        }

    @staticmethod
    def _chunk_duration_ms(chunk: bytes, sample_rate: int = 8000) -> float:
        if not chunk:
            return 0.0
        sample_count = len(chunk) / 2.0
        return (sample_count / sample_rate) * 1000.0

    async def stream(self) -> AsyncGenerator[bytes, None]:
        last_audio_ts = time.time()
        self._play_start_time = time.time()
        self._listen_start_time = self._play_start_time
        timeout_count = 0
        while not self._stopped:
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=0.08)
            except asyncio.TimeoutError:
                timeout_count += 1
                elapsed_silence_ms = (time.time() - last_audio_ts) * 1000
                initial_wait_ms = (
                    (time.time() - self._listen_start_time) * 1000
                    if self._listen_start_time is not None
                    else 0
                )
                if timeout_count % 50 == 0:
                    logger.debug(f"[adapter] 等待音频: {initial_wait_ms/1000:.1f}s, 超时次数={timeout_count}, 队列大小={self._queue.qsize()}")
                if self._started_speaking and elapsed_silence_ms >= self._vad_silence_ms:
                    break
                if not self._started_speaking and initial_wait_ms >= 15000:
                    break
                continue
            if not chunk:
                break

            chunk_duration_ms = self._chunk_duration_ms(chunk)
            rms = self._vad.frame_rms(chunk)
            has_speech = rms > self._vad.energy_threshold
            self._total_chunks += 1
            self._max_rms = max(self._max_rms, rms)

            if has_speech:
                self._started_speaking = True
                self._silence_ms = 0.0
                last_audio_ts = time.time()
                self._speech_chunks += 1
                self._speech_ms += chunk_duration_ms
            else:
                self._silence_ms += chunk_duration_ms
                if not self._started_speaking:
                    if self._silence_ms >= 2000:
                        break
                    continue

            # 检查打断策略：只有 enabled 且不在保护期才允许 barge-in
            should_barge_in = False
            if has_speech and self._barge_in_enabled and self._play_start_time is not None:
                elapsed_ms = (time.time() - self._play_start_time) * 1000
                in_protect_start = elapsed_ms < self._protect_start_ms
                in_protect_end = False
                if self._total_duration_ms > 0:
                    remaining_ms = self._total_duration_ms - elapsed_ms
                    in_protect_end = remaining_ms < self._protect_end_ms
                should_barge_in = not in_protect_start and not in_protect_end

            if should_barge_in and self._barge_in_event and not self._barge_in_event.is_set():
                self._barge_in_event.set()
            yield chunk
            if self._started_speaking and not has_speech and self._silence_ms >= self._vad_silence_ms:
                break
        yield b""  # ASR 结束信号


class CallAgent:
    """单路通话的完整生命周期代理"""

    def __init__(
        self,
        session: ESLSocketCallSession,
        context: CallContext,
        asr: Optional[BaseASR] = None,
        tts: Optional[BaseTTS] = None,
        llm: Optional[LLMService] = None,
        esl_pool=None,
    ):
        self.session = session
        self.ctx = context
        self.sm = StateMachine(context)

        self.asr = asr or create_asr_client()
        self.tts = tts or create_tts_client()
        self.llm = llm or LLMService()
        self.esl_pool = esl_pool

        self._system_prompt: str = ""  # 在 connect() 后从 channel vars 构建
        self._script_config = None  # 话术脚本配置，用于获取打断策略
        self._barge_in = asyncio.Event()
        self._barge_in.set()  # 初始置位（表示目前没有在播放）
        # 最近一次 _say 的打断配置，用于传递给 _listen_user
        self._last_barge_in_config = {
            "enabled": True, "protect_start_ms": 3000, "protect_end_ms": 3000, "duration_ms": 0
        }

        # 注册状态机动作
        self.sm.register_handler("transfer",  self._do_transfer)
        self.sm.register_handler("end",       self._do_hangup)
        self.sm.register_handler("callback",  self._do_schedule_callback)
        self.sm.register_handler("send_sms",  self._do_send_sms)
        self.sm.register_handler("blacklist", self._do_blacklist)

    # ─────────────────────────────────────────────────────────
    # 主循环
    # ─────────────────────────────────────────────────────────
    async def run(self):
        logger.info(f"[{self.ctx.uuid}] CallAgent 启动: {self.ctx.phone_number}")
        try:
            # 1. ESL 握手，读取 channel 变量
            channel_data = await asyncio.wait_for(
                self.session.connect(), timeout=10.0
            )
            self.ctx.answered_at = datetime.now()
            self.sm.transition(CallState.CONNECTED)

            # 2. 从 channel 变量补充 context（覆盖占位符）
            cv = self.session.channel_vars
            if cv.get("task_id"):
                self.ctx.task_id = cv["task_id"]
            if cv.get("script_id"):
                self.ctx.script_id = cv["script_id"]
            dest = channel_data.get("Caller-Destination-Number", "")
            if dest and dest != "unknown":
                self.ctx.phone_number = dest

            # 3. 查询客户信息（CRM）
            if not self.ctx.customer_info:
                self.ctx.customer_info = await crm.query_customer_info(self.ctx.phone_number)

            # 4. 构建 System Prompt + 缓存话术配置
            self._system_prompt = await get_system_prompt_for_call(
                self.ctx.script_id, self.ctx.customer_info
            )
            self._script_config = await script_service.get_script(self.ctx.script_id)

            # 5. 启动事件监听
            event_task = asyncio.create_task(self.session.read_events())

            # 6. 写通话开始变量到 CDR
            await self.session.set_variable("ai_call_started", "true")

            # 7. 主对话循环，整体超时保护
            try:
                await asyncio.wait_for(
                    self._conversation_loop(),
                    timeout=MAX_CALL_DURATION,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[{self.ctx.uuid}] 通话超过最大时长 {MAX_CALL_DURATION}s，强制结束")
                await self._say("感谢您的耐心，通话时间已到，再见！", record=False)

            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)

        except ESLError as e:
            logger.warning(f"[{self.ctx.uuid}] ESL 异常: {e}")
            self.ctx.result = CallResult.ERROR
        except asyncio.CancelledError:
            logger.info(f"[{self.ctx.uuid}] 通话被外部取消")
            self.ctx.result = CallResult.ERROR
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] 未预期异常: {e}", exc_info=True)
            self.ctx.result = CallResult.ERROR
        finally:
            await self._cleanup()

    async def _conversation_loop(self):
        """多轮对话主循环"""
        # 开场白
        logger.info(f"[{self.ctx.uuid}] 🎙️ 播放开场白")
        await self._say_opening()
        logger.info(f"[{self.ctx.uuid}] 🎙️ 开场白播放完毕，进入监听模式")
        silence_retries = 0

        while self.sm.should_continue() and self.session._connected:
            # 监听用户
            logger.info(f"[{self.ctx.uuid}] 🔊 开始监听用户 (session._connected={self.session._connected})")
            cfg = self._last_barge_in_config
            user_text = await self._listen_user(
                barge_in_enabled=cfg["enabled"],
                protect_start_ms=cfg["protect_start_ms"],
                protect_end_ms=cfg["protect_end_ms"],
                total_duration_ms=cfg["duration_ms"],
            )
            logger.info(f"[{self.ctx.uuid}] 🔊 监听结束, user_text={user_text!r}")

            if not user_text:
                silence_retries += 1
                if silence_retries > MAX_SILENCE_RETRIES:
                    logger.info(f"[{self.ctx.uuid}] 连续 {silence_retries} 次无响应，结束通话")
                    break
                logger.info(f"[{self.ctx.uuid}] 第 {silence_retries} 次无响应，追问")
                await self._say("您好，请问您还在吗？")
                continue
            silence_retries = 0

            logger.info(f"[{self.ctx.uuid}] 👤 {user_text}")

            # LLM 推理
            logger.info(f"[{self.ctx.uuid}] 🧠 开始调用 LLM...")
            reply_text, action = await self._think_and_reply(user_text)
            logger.info(f"[{self.ctx.uuid}] 🧠 LLM 返回: reply={reply_text[:50]!r} action={action}")

            # 播报（transfer 时先播再转，end 时先说再挂）
            if reply_text and action not in ("transfer",):
                await self._say(reply_text)

            if action == "end":
                break

    # ─────────────────────────────────────────────────────────
    # 开场白
    # ─────────────────────────────────────────────────────────
    async def _say_opening(self):
        self.sm.transition(CallState.AI_SPEAKING)
        opening = await get_opening_for_call(self.ctx.script_id, self.ctx.customer_info)
        text = opening["reply"]
        self.ctx.messages.append({"role": "assistant", "content": text})
        self.ctx.ai_utterances += 1
        await self._say(text, record=False, speech_type="opening")

    # ─────────────────────────────────────────────────────────
    # 监听用户（ASR）
    # ─────────────────────────────────────────────────────────
    async def _listen_user(self, *, barge_in_enabled: bool = True,
                           protect_start_ms: int = 3000, protect_end_ms: int = 3000,
                           total_duration_ms: float = 0) -> str:
        self.sm.transition(CallState.USER_SPEAKING)
        audio_queue = await self.session.start_audio_capture()

        queue_size = audio_queue.qsize()
        logger.info(f"[{self.ctx.uuid}] 🔊 开始监听, 音频队列当前大小={queue_size}")

        adapter = AudioStreamAdapter(
            audio_queue,
            vad_silence_ms=config.asr.vad_silence_ms,
            barge_in_cb=self._barge_in,
            barge_in_enabled=barge_in_enabled,
            total_duration_ms=total_duration_ms,
            protect_start_ms=protect_start_ms,
            protect_end_ms=protect_end_ms,
        )

        final_text = ""
        asr_result_count = 0
        try:
            async with asyncio.timeout(ASR_TIMEOUT):
                async for result in self._asr_with_retry(adapter.stream()):
                    asr_result_count += 1
                    logger.info(f"[{self.ctx.uuid}] ASR 中间结果 #{asr_result_count}: text={result.text!r} is_final={result.is_final} conf={result.confidence:.2f}")
                    if result.is_final and result.text:
                        final_text = result.text
                        logger.info(f"[{self.ctx.uuid}] ASR ✓ {result.text!r} (conf={result.confidence:.2f}, is_final={result.is_final})")
                        # 通话场景中，一句话即为一轮对话，拿到 is_final 后立即退出
                        # 避免前端持续送音频导致 VAD 无法触发、ASR 无限识别多句
                        break
        except asyncio.TimeoutError:
            logger.warning(f"[{self.ctx.uuid}] ASR 超时")
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] ASR 异常: {e}")
        finally:
            adapter.stop()

        audio_stats = adapter.audio_stats()
        logger.info(
            f"[{self.ctx.uuid}] 音频检测: speech_detected={audio_stats['speech_detected']} "
            f"total_chunks={audio_stats['total_chunks']} "
            f"speech_chunks={audio_stats['speech_chunks']} "
            f"speech_ms={audio_stats['speech_ms']} "
            f"max_rms={audio_stats['max_rms']}"
        )
        if not audio_stats["speech_detected"]:
            logger.warning(f"[{self.ctx.uuid}] 本轮进入 ASR 的音频未检测到有效人声")
        elif not final_text:
            logger.warning(
                f"[{self.ctx.uuid}] 已检测到人声，但 ASR 未产出最终文本 "
                f"(speech_ms={audio_stats['speech_ms']}, max_rms={audio_stats['max_rms']})"
            )

        logger.info(f"[{self.ctx.uuid}] 🔊 ASR 完成: final_text={final_text!r}, 共 {asr_result_count} 条结果")

        if final_text:
            self.ctx.user_utterances += 1
            self.ctx.messages.append({"role": "user", "content": final_text})

        return final_text

    async def _asr_with_retry(self, audio_gen: AsyncGenerator[bytes, None]):
        """ASR 带超时的流式识别"""
        async def _gen_with_timeout():
            async for chunk in audio_gen:
                yield chunk

        async for result in self.asr.recognize_stream(_gen_with_timeout()):
            yield result

    async def _tts_stream_with_timeout(self, text: str) -> AsyncGenerator[bytes, None]:
        """限制单个 TTS chunk 的等待时间，避免卡在合成阶段。"""
        audio_gen = self.tts.synthesize_stream(text)
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(audio_gen.__anext__(), timeout=TTS_TIMEOUT)
                except StopAsyncIteration:
                    break
                yield chunk
        finally:
            aclose = getattr(audio_gen, "aclose", None)
            if aclose is not None:
                await aclose()

    # ─────────────────────────────────────────────────────────
    # LLM 推理
    # ─────────────────────────────────────────────────────────
    async def _think_and_reply(self, user_text: str) -> tuple[str, str]:
        self.sm.transition(CallState.PROCESSING)
        t0 = time.time()
        try:
            response = await asyncio.wait_for(
                self.llm.chat(
                    messages=self.ctx.messages,
                    system_prompt=self._system_prompt,
                ),
                timeout=LLM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"[{self.ctx.uuid}] LLM 超时 ({LLM_TIMEOUT}s)")
            response = {
                "reply": "抱歉，我这边网络有些问题，请问您方便稍后再聊吗？",
                "intent": "unknown",
                "action": "end",
                "action_params": {},
            }
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] LLM 异常: {e}")
            response = self.llm._fallback_response()

        logger.debug(f"[{self.ctx.uuid}] LLM 耗时 {(time.time()-t0)*1000:.0f}ms")
        reply_text, action = await self.sm.process_llm_response(response)

        if reply_text:
            self.ctx.messages.append({"role": "assistant", "content": reply_text})
            self.ctx.ai_utterances += 1
            logger.info(f"[{self.ctx.uuid}] 🤖 {reply_text[:80]} [action={action}]")

        return reply_text, action

    # ─────────────────────────────────────────────────────────
    # TTS + 播放（支持 barge-in，流式输出）
    # ─────────────────────────────────────────────────────────
    async def _say(self, text: str, record: bool = True,
                   speech_type: str = "conversation"):
        if not text or not self.session._connected:
            return
        self.sm.transition(CallState.AI_SPEAKING)

        # 获取打断策略配置
        barge_in_config = await get_barge_in_config(self.ctx.script_id, speech_type)
        barge_in_enabled = barge_in_config["barge_in_enabled"]
        protect_start_ms = int(barge_in_config["protect_start_sec"] * 1000)
        protect_end_ms = int(barge_in_config["protect_end_sec"] * 1000)

        # 流式模式下总时长未知，设为 0（barge-in 仅依赖 protect_start_ms 判断）
        total_duration_ms = 0.0

        # 存储打断配置到实例变量，供 _listen_user 使用
        self._last_barge_in_config = {
            "enabled": barge_in_enabled,
            "protect_start_ms": protect_start_ms,
            "protect_end_ms": protect_end_ms,
            "duration_ms": total_duration_ms,
        }

        self._barge_in.clear()  # 开始播放，barge-in 检测生效

        try:
            t0 = time.time()
            # 流式 TTS：边合成边播
            audio_chunks = self._tts_stream_with_timeout(text)
            logger.debug(f"[{self.ctx.uuid}] TTS 流式开始")

            # barge-in：如果用户已经开始说话则跳过播放
            if self._barge_in.is_set():
                logger.debug(f"[{self.ctx.uuid}] barge-in 检测到，跳过播放")
                return

            # 收集 TTS 音频
            pcm_parts = []
            total_bytes = 0
            async for chunk in audio_chunks:
                if chunk:
                    pcm_parts.append(chunk)
                    total_bytes += len(chunk)

            if not pcm_parts:
                logger.warning(f"[{self.ctx.uuid}] TTS 流式播放收到空音频")
                return

            from backend.utils.audio import write_wav
            import os
            import tempfile

            shared_dir = os.environ.get("FS_RECORDING_PATH", "/recordings")
            os.makedirs(shared_dir, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=f"tts_{self.ctx.uuid or 'call'}_",
                suffix=".wav",
                dir=shared_dir,
            )
            os.close(fd)

            pcm_data = b"".join(pcm_parts)
            write_wav(temp_path, pcm_data, sample_rate=8000)
            file_size = os.path.getsize(temp_path)
            # 估算播放时长（8000Hz 16bit mono = 16000 bytes/sec）
            estimated_duration = len(pcm_data) / 16000.0

            logger.info(
                f"[{self.ctx.uuid}] TTS WAV: {temp_path} "
                f"({file_size} bytes, ~{estimated_duration:.1f}s)"
            )

            # 通过 outbound session 直接执行 playback（sendmsg execute）
            await self.session.play(temp_path, timeout=60.0)

            logger.debug(f"[{self.ctx.uuid}] TTS 播放完成，耗时 {(time.time()-t0)*1000:.0f}ms")
        except asyncio.TimeoutError:
            logger.error(f"[{self.ctx.uuid}] TTS 超时")
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] TTS/播放失败: {e}", exc_info=True)
        finally:
            self._barge_in.set()  # 播放结束，允许新一轮录音

    # ─────────────────────────────────────────────────────────
    # 动作处理器
    # ─────────────────────────────────────────────────────────
    async def _do_transfer(self, params: dict):
        logger.info(f"[{self.ctx.uuid}] 转接人工坐席")
        self.sm.transition(CallState.TRANSFERRING)
        self.ctx.result = CallResult.TRANSFERRED
        ext = params.get("extension", "8001")
        await self._say("好的，正在为您转接人工客服，请稍等。", speech_type="closing")
        await asyncio.sleep(0.3)
        await self.session.transfer_to_human(ext)
        # 写入转接原因供 CDR
        await self.session.set_variable("transfer_reason", self.ctx.intent.value)

    async def _do_hangup(self, params: dict):
        farewell = params.get("farewell", "好的，感谢您的时间，祝您生活愉快，再见！")
        logger.info(f"[{self.ctx.uuid}] 主动挂断")
        if self.ctx.result not in (CallResult.ERROR, CallResult.TRANSFERRED):
            self.ctx.result = CallResult.COMPLETED
        await self._say(farewell, speech_type="closing")
        await asyncio.sleep(0.4)  # 让 TTS 播放完
        await self.session.hangup()

    async def _do_send_sms(self, params: dict):
        phone = params.get("phone", self.ctx.phone_number)
        template = params.get("template_id", "product_intro")
        logger.info(f"[{self.ctx.uuid}] 发送短信 {phone} tmpl={template}")
        try:
            await crm.send_sms(phone, template, params.get("vars", {}))
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] 短信发送失败: {e}")

    async def _do_schedule_callback(self, params: dict):
        callback_time = params.get("callback_time") or params.get("time")
        note = params.get("note", "用户要求稍后回拨")
        logger.info(
            f"[{self.ctx.uuid}] 安排回拨: {self.ctx.phone_number} at {callback_time or 'default'}"
        )
        try:
            await crm.schedule_callback(
                self.ctx.phone_number,
                task_id=self.ctx.task_id,
                callback_time=callback_time,
                note=note,
            )
            if self.ctx.result == CallResult.NOT_ANSWERED:
                self.ctx.result = CallResult.COMPLETED
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] 安排回拨失败: {e}")
        self.sm.transition(CallState.ENDING)

    async def _do_blacklist(self, params: dict):
        reason = params.get("reason", "用户拒绝")
        logger.info(f"[{self.ctx.uuid}] 加入黑名单: {self.ctx.phone_number}")
        await crm.add_to_blacklist(self.ctx.phone_number, reason)
        self.ctx.result = CallResult.BLACKLISTED
        self.sm.transition(CallState.ENDING)

    # ─────────────────────────────────────────────────────────
    # 清理
    # ─────────────────────────────────────────────────────────
    async def _cleanup(self):
        self.sm.transition(CallState.ENDED)
        self.ctx.ended_at = datetime.now()
        self.ctx.recording_path = (
            self.session.channel_vars.get("record_file")
            or f"{config.freeswitch.recording_path}/{self.ctx.task_id}/{self.ctx.uuid}.wav"
        )

        # 捕获挂断原因和 SIP 状态码
        cv = self.session.channel_vars
        if cv.get("sip_hangup_cause"):
            self.ctx.hangup_cause = cv["sip_hangup_cause"]
        if cv.get("sip_response_code"):
            try:
                self.ctx.sip_code = int(cv["sip_response_code"])
            except (ValueError, TypeError):
                pass
        # 如果 channel_vars 中没有，尝试从 session 属性获取
        if not self.ctx.hangup_cause and hasattr(self.session, "_hangup_cause"):
            self.ctx.hangup_cause = self.session._hangup_cause
        if not self.ctx.sip_code and hasattr(self.session, "_sip_code"):
            self.ctx.sip_code = self.session._sip_code

        dur = self.ctx.duration_seconds or 0
        logger.info(
            f"[{self.ctx.uuid}] ✓ 结束 "
            f"result={self.ctx.result.value} "
            f"intent={self.ctx.intent.value} "
            f"dur={dur}s turns={self.ctx.user_utterances} "
            f"sip={self.ctx.sip_code} cause={self.ctx.hangup_cause}"
        )

        # 写 CDR
        try:
            await save_call_record(self.ctx)
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] CDR 写入失败: {e}")

        # 写意向到 CRM
        if self.ctx.intent.value in ("interested", "high", "medium"):
            try:
                await crm.record_intent(
                    self.ctx.phone_number,
                    self.ctx.intent.value,
                    self.ctx.uuid,
                    note=f"通话时长{dur}s",
                )
            except Exception:
                pass

        # 确保已挂断
        try:
            if self.session._connected:
                await self.session.hangup()
        except Exception:
            pass
