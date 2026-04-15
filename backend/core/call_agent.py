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
import os
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
# ★ 优化：12s → 30s，文件轮询模式下录音开头可能有较长静音（TTS 准备期），
#    用户可能在 15-20 秒后才开始说话，12s 太短导致 ASR 在到达语音段前就超时
ASR_TIMEOUT = 30.0


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
        # ★ 连续语音检测：防止前几个噪音 chunk 误触发 _started_speaking
        #    需要连续 5 个语音 chunk（100ms 持续语音）才认为用户真正开始说话
        self._consecutive_speech_frames = 0
        self._speech_onset_threshold = 5  # 5 帧 = 100ms
        self._vad = SimpleVAD(
            sample_rate=8000,
            frame_ms=20,
            energy_threshold=120,     # 降低阈值：文件轮询的音频能量较低（max_rms≈134），需确保能触发语音检测
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

        # 🎵 调试：收集送入 ASR 的音频
        import tempfile
        dump_dir = os.environ.get("FS_RECORDING_PATH", "/recordings") + "/debug"
        os.makedirs(dump_dir, exist_ok=True)
        dump_path = os.path.join(dump_dir, f"asr_input_{int(time.time())}.pcm")
        dump_file = open(dump_path, "wb")
        dump_bytes = 0

        # ★ 修复：移除 30 秒队列等待逻辑，立即开始消费
        #    实时通话场景中，音频是持续推送的，不应该等待队列就绪
        #    如果队列为空，VAD 超时会自动退出

        try:
            while not self._stopped:
                try:
                    chunk = await asyncio.wait_for(self._queue.get(), timeout=0.08)
                except asyncio.TimeoutError:
                    timeout_count += 1
                    elapsed_silence_ms = (time.time() - last_audio_ts) * 1000
                    if timeout_count % 50 == 0:
                        logger.debug(f"[adapter] 等待音频: 超时次数={timeout_count}, 队列大小={self._queue.qsize()}")
                    if self._started_speaking and elapsed_silence_ms >= self._vad_silence_ms:
                        # 用户已开始说话且静音超过阈值，认为说完
                        break
                    # ★ 修复：移除 5 秒初始等待超时
                    #    实时通话中用户可能在任何时间说话，不应因为前 5 秒无语音就退出
                    #    改为：如果队列持续为空且从未检测到语音，最多等待 ASR_TIMEOUT 后退出
                    continue
                if not chunk:
                    break

                chunk_duration_ms = self._chunk_duration_ms(chunk)
                rms = self._vad.frame_rms(chunk)
                has_speech = rms > self._vad.energy_threshold
                self._total_chunks += 1
                self._max_rms = max(self._max_rms, rms)

                if has_speech:
                    self._consecutive_speech_frames += 1
                    self._speech_chunks += 1
                    self._speech_ms += chunk_duration_ms

                    # ★ 关键修复：连续语音帧达到阈值才认为用户真正开始说话
                    #    防止前几个噪音 chunk（RMS 略高于阈值）误触发
                    if self._consecutive_speech_frames >= self._speech_onset_threshold:
                        if not self._started_speaking:
                            self._started_speaking = True
                            logger.info(f"[adapter] 用户开始说话（连续 {self._consecutive_speech_frames} 帧语音）")
                        self._silence_ms = 0.0
                        last_audio_ts = time.time()

                    if self._speech_chunks <= 3:
                        logger.info(f"[adapter] 检测到语音 #{self._speech_chunks}: rms={rms} threshold={self._vad.energy_threshold} chunk={len(chunk)}B duration={chunk_duration_ms:.1f}ms")
                else:
                    self._consecutive_speech_frames = 0  # 重置连续计数
                    self._silence_ms += chunk_duration_ms
                    if not self._started_speaking:
                        # ★ 优化：初始静音超时从 2s → 15s
                        #    文件轮询模式下，录音开头可能有较长静音（TTS 准备期），
                        #    2s 太短导致适配器在收到有效语音前就退出。
                        #    15s 足够等待文件轮询到达语音段。
                        if self._silence_ms >= 15000:
                            break
                        # ★ 关键修复：即使当前帧是静音，也要 yield 给 ASR
                        #    之前这里有个 continue 导致静音帧不送入 ASR，
                        #    但如果后续没有语音，adapter 会在 2s 后 break 且不 yield 任何数据，
                        #    导致 ASR 收到 0 chunks → NO_VALID_AUDIO_ERROR
                        #    ASR 服务端自己能处理静音音频，不需要我们过滤

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

                # 写入调试文件
                dump_bytes += len(chunk)
                dump_file.write(chunk)

                yield chunk
                if self._started_speaking and not has_speech and self._silence_ms >= self._vad_silence_ms:
                    break
            yield b""  # ASR 结束信号
        finally:
            dump_file.close()
            if dump_bytes > 0:
                wav_path = dump_path.replace(".pcm", ".wav")
                try:
                    from backend.utils.audio import write_wav
                    pcm_data = open(dump_path, "rb").read()
                    write_wav(wav_path, pcm_data, sample_rate=8000)
                    dur = len(pcm_data) / 16000.0
                    logger.info(
                        f"[adapter] ASR 输入已保存: {wav_path} "
                        f"({dump_bytes} bytes, {dur:.1f}s, "
                        f"speech={self._speech_ms:.0f}ms, max_rms={self._max_rms})"
                    )
                except Exception as e:
                    logger.warning(f"[adapter] WAV 保存失败: {e}")
            try:
                os.remove(dump_path)
            except OSError:
                pass


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
        # ★ 优化5：主 ASR 监听就绪信号（由 _say 在 TTS 播放结束前设置）
        self._main_asr_ready = asyncio.Event()
        self._main_asr_ready.set()  # 初始置位（第一次监听不需要等待）
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
            # 监听用户（继续 _say 期间未完成的 ASR 监听）
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

        # 通话已断开时跳过 ASR
        if not self.session._connected:
            logger.info(f"[{self.ctx.uuid}] 会话已断开，跳过 ASR")
            return ""

        # ★ 优化5：如果 _say 设置了提前启动信号，等待它
        if not self._main_asr_ready.is_set():
            logger.info(f"[{self.ctx.uuid}] 🔊 等待 _say 提前启动 ASR...")
            try:
                await asyncio.wait_for(self._main_asr_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.ctx.uuid}] 等待主 ASR 就绪超时，直接启动")
            # 重置信号，供下次使用
            self._main_asr_ready.clear()

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
        last_intermediate_text = ""  # 兜底：如果 ASR 未产出 is_final，使用最后一条中间结果
        asr_result_count = 0
        try:
            async with asyncio.timeout(ASR_TIMEOUT):
                async for result in self._asr_with_retry(adapter.stream()):
                    asr_result_count += 1
                    logger.info(f"[{self.ctx.uuid}] ASR 中间结果 #{asr_result_count}: text={result.text!r} is_final={result.is_final} conf={result.confidence:.2f}")
                    if result.text:
                        last_intermediate_text = result.text
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

        # 兜底：未产出 is_final 但有人声 + 中间结果，使用最后一条中间结果
        if not final_text and last_intermediate_text and audio_stats.get("speech_detected"):
            final_text = last_intermediate_text
            logger.info(
                f"[{self.ctx.uuid}] ASR 兜底：使用最后一条中间结果 {last_intermediate_text!r} "
                f"(speech_ms={audio_stats['speech_ms']}, max_rms={audio_stats['max_rms']})"
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

    async def _barge_in_asr_loop_with_queue(self, adapter: AudioStreamAdapter,
                                             audio_capture_task: asyncio.Task):
        """后台 ASR 循环，等待音频队列就绪后再开始 barge-in 检测。

        与 _barge_in_asr_loop 的区别：
        - 接收一个 Task（start_audio_capture），等待其完成后获取真实队列
        - 不阻塞 TTS 播放
        """
        try:
            # 等待音频采集就绪（最多 10 秒）
            try:
                audio_queue = await asyncio.wait_for(audio_capture_task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.ctx.uuid}] barge-in 音频采集超时，使用空队列")
                audio_queue = asyncio.Queue()
            except Exception as e:
                logger.warning(f"[{self.ctx.uuid}] barge-in 音频采集异常: {e}，使用空队列")
                audio_queue = asyncio.Queue()

            # 替换 adapter 的队列
            adapter._audio_queue = audio_queue
            logger.info(f"[{self.ctx.uuid}] barge-in 音频队列已就绪, 大小={audio_queue.qsize()}")

            async with asyncio.timeout(ASR_TIMEOUT):
                async for result in self._asr_with_retry(adapter.stream()):
                    if result.is_final and result.text:
                        logger.info(f"[{self.ctx.uuid}] barge-in ASR: {result.text!r}")
                        if not self._barge_in.is_set():
                            self._barge_in.set()
        except asyncio.TimeoutError:
            logger.debug(f"[{self.ctx.uuid}] barge-in ASR 超时")
        except Exception as e:
            logger.debug(f"[{self.ctx.uuid}] barge-in ASR 异常: {e}")
        finally:
            adapter.stop()

    async def _barge_in_asr_loop(self, adapter: AudioStreamAdapter):
        """
        后台 ASR 循环，用于 TTS 播放期间的 barge-in 检测。
        此方法不关心 ASR 最终结果，只关心是否检测到用户语音（用于打断）。
        """
        try:
            async with asyncio.timeout(ASR_TIMEOUT):
                async for result in self._asr_with_retry(adapter.stream()):
                    if result.is_final and result.text:
                        logger.info(f"[{self.ctx.uuid}] barge-in ASR: {result.text!r}")
                        # 用户说话了，设置打断事件
                        if not self._barge_in.is_set():
                            self._barge_in.set()
        except asyncio.TimeoutError:
            logger.debug(f"[{self.ctx.uuid}] barge-in ASR 超时")
        except Exception as e:
            logger.debug(f"[{self.ctx.uuid}] barge-in ASR 异常: {e}")
        finally:
            adapter.stop()

    async def _asr_with_retry(self, audio_gen: AsyncGenerator[bytes, None]):
        """ASR 带超时的流式识别"""
        async def _gen_with_timeout():
            async for chunk in audio_gen:
                yield chunk

        async for result in self.asr.recognize_stream(_gen_with_timeout(), call_uuid=self.ctx.uuid):
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

            # ★ 关键修复：在 TTS 播放期间启动后台 ASR 监听
            #    这样用户可以在 TTS 播放期间（除保护期外）打断 AI
            #    后台 ASR 检测到用户语音后会设置 _barge_in 事件
            #    ★ 启动为后台任务，不阻塞 TTS 播放
            barge_in_adapter = AudioStreamAdapter(
                asyncio.Queue(),  # 占位队列，后续替换
                vad_silence_ms=config.asr.vad_silence_ms,
                barge_in_cb=self._barge_in,
                barge_in_enabled=barge_in_enabled,
                total_duration_ms=total_duration_ms,
                protect_start_ms=protect_start_ms,
                protect_end_ms=protect_end_ms,
            )
            # 后台启动音频采集（不阻塞 TTS）
            audio_capture_task = asyncio.create_task(self.session.start_audio_capture())
            # 启动后台 ASR 任务（不阻塞 TTS 播放）
            barge_in_asr_task = asyncio.create_task(
                self._barge_in_asr_loop_with_queue(barge_in_adapter, audio_capture_task)
            )
            logger.info(f"[{self.ctx.uuid}] barge-in ASR 任务已启动（后台音频采集中）")

            # ★ 优化4：流式 TTS — 边接收边写文件，消除 join+写文件延迟
            import os
            import tempfile
            import wave
            from backend.utils.audio import write_wav

            shared_dir = os.environ.get("FS_RECORDING_PATH", "/recordings")
            os.makedirs(shared_dir, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=f"tts_{self.ctx.uuid or 'call'}_",
                suffix=".wav",
                dir=shared_dir,
            )
            os.close(fd)

            # 边接收 TTS chunk 边写入文件
            # 先用 wave 模块打开文件，然后边接收边写 PCM
            pcm_size = 0
            async with asyncio.timeout(60):
                try:
                    with wave.open(temp_path, "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(8000)
                        async for chunk in audio_chunks:
                            if chunk:
                                wf.writeframes(chunk)
                                pcm_size += len(chunk)
                            # 每收到一个 TTS chunk 都检查 barge-in
                            if self._barge_in.is_set():
                                logger.debug(f"[{self.ctx.uuid}] barge-in 检测到，停止 TTS 收集")
                                barge_in_adapter.stop()
                                break
                except StopAsyncIteration:
                    pass

            if pcm_size == 0:
                logger.warning(f"[{self.ctx.uuid}] TTS 流式播放收到空音频")
                barge_in_adapter.stop()
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                return

            estimated_duration = pcm_size / 16000.0  # 8000Hz * 2 bytes = 16000 bytes/s
            logger.info(
                f"[{self.ctx.uuid}] TTS WAV: {temp_path} "
                f"({pcm_size} bytes PCM, ~{estimated_duration:.1f}s)"
            )

            # ★ 优化5：在 TTS 播放期间提前启动主 ASR 监听
            #    计算 ASR 提前启动时间（TTS 播放结束前 2 秒）
            asr_prestart_time = max(0, estimated_duration - 2.0)

            async def _play_with_early_asr():
                """播放 TTS 并在结束前启动主 ASR 监听。"""
                play_task = asyncio.create_task(self.session.play(temp_path, timeout=60.0))
                # 等待预启动时间后标记主 ASR 就绪
                if asr_prestart_time > 0:
                    await asyncio.sleep(asr_prestart_time)
                # 标记主 ASR 可以启动（在 _listen_user 中检查）
                self._main_asr_ready.set()
                await play_task

            try:
                await _play_with_early_asr()
            except Exception:
                self._main_asr_ready.set()  # 确保即使播放失败也释放等待
                raise

            logger.debug(f"[{self.ctx.uuid}] TTS 播放完成，耗时 {(time.time()-t0)*1000:.0f}ms")

            # 停止后台 ASR 任务
            barge_in_adapter.stop()
            await asyncio.gather(barge_in_asr_task, return_exceptions=True)

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
