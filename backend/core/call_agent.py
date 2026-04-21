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
import re
import time
from datetime import datetime
from typing import AsyncGenerator, Optional

from backend.core.config import config
from backend.core.state_machine import CallState, CallResult, CallContext, StateMachine
from backend.services.asr_service import BaseASR, create_asr_client, ASRResult
from backend.services.tts_service import BaseTTS, create_tts_client
from backend.services.llm_service import LLMService
from backend.services.esl_service import ESLError
from backend.services.forkzstream_session import ForkzstreamCallSession
from backend.services.crm_service import crm
from backend.utils.db import save_call_record
from backend.utils.audio import SimpleVAD
from backend.services.async_script_utils import get_system_prompt_for_call, get_opening_for_call, get_barge_in_config, get_no_response_config
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
# 15s：百炼流式 ASR 首包约 2-5s，完整识别 5-15s。
# 8s 太短（日志显示百炼 SDK 超时为 23s），导致识别未完成就被中断。
ASR_TIMEOUT = 15.0


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
        server_vad_mode: bool = False,
    ):
        self._queue = audio_queue
        self._vad_silence_ms = vad_silence_ms
        self._barge_in_event = barge_in_cb
        self._stopped = False
        self._started_speaking = False  # 是否已经检测到用户开始说话
        self._is_all_silent = False     # stream() 退出时：从未检测到语音 = True
        self._server_vad_mode = server_vad_mode  # 服务端 VAD 模式：不依赖本地静音检测退出
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
        #    需要连续 2 个语音帧（40ms 持续语音）才认为用户真正开始说话
        self._consecutive_speech_frames = 0
        self._speech_onset_threshold = 2  # 2 帧 = 40ms
        self._vad = SimpleVAD(
            sample_rate=8000,
            frame_ms=40,              # 40ms 帧：比 20ms 更稳定的能量检测
            energy_threshold=120,     # 降低阈值：音频能量较低（max_rms≈134），需确保能触发语音检测
            speech_min_frames=1,
            silence_min_frames=max(1, int(vad_silence_ms / 40)),
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
                    logger.warning(f"[adapter] 收到空 chunk，音频流已断开（forkzstream WS 可能已断开）")
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
                    if not self._started_speaking and not self._server_vad_mode:
                        # ★ 优化：初始静音超时从 2s → 15s
                        #    通话开头可能有较长静音（TTS 准备期），
                        #    2s 太短导致适配器在收到有效语音前就退出。
                        #    15s 足够等待到达语音段。
                        #    server_vad_mode 下禁用，让 Qwen 服务端 VAD 控制语音段结束
                        if self._silence_ms >= 15000:
                            break
                        # ★ 关键修复：即使当前帧是静音，也要 yield 给 ASR
                        #    之前这里有个 continue 导致静音帧不送入 ASR，
                        #    但如果后续没有语音，adapter 会在 2s 后 break 且不 yield 任何数据，
                        #    导致 ASR 收到 0 chunks → NO_VALID_AUDIO_ERROR
                        #    ASR 服务端自己能处理静音音频，不需要我们过滤

                # ★ 修复：禁用本地 VAD 触发 barge-in
                #    只让 ASR 识别到有意义文本后才触发 barge-in（在 _barge_in_asr_loop_with_queue 中）
                #    本地 VAD 太敏感，会在 ASR 识别前就触发，导致 barge-in 文本为空

                # 写入调试文件
                dump_bytes += len(chunk)
                dump_file.write(chunk)

                yield chunk
                # ★ server_vad_mode 下不依赖本地 VAD 静音退出，让 Qwen 服务端 VAD 控制语音段结束
                if not self._server_vad_mode and self._started_speaking and not has_speech and self._silence_ms >= self._vad_silence_ms:
                    break
            yield b""  # ASR 结束信号
        finally:
            self._is_all_silent = not self._started_speaking
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
        session: ForkzstreamCallSession,
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
        self._barge_in_text: str = ""  # barge-in ASR 识别到的文本
        self._barge_in_asr_task: Optional[asyncio.Task] = None  # 当前 barge-in ASR 任务
        # ★ 优化5：主 ASR 监听就绪信号（由 _say 在 TTS 播放结束前设置）
        self._main_asr_ready = asyncio.Event()
        self._main_asr_ready.set()  # 初始置位（第一次监听不需要等待）
        # 最近一次 _say 的打断配置，用于传递给 _listen_user
        self._last_barge_in_config = {
            "enabled": True, "protect_start_ms": 3000, "protect_end_ms": 3000, "duration_ms": 0
        }
        # 记录上一次 _say 是否触发 barge-in（用于 ttsstop_clean 后恢复 TTS）
        self._last_say_had_barge_in = False

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
            # Caller-Destination-Number 在内部分机场景下是 "AI_CALL"（loopback 端点名），
            # 只有纯数字的有效号码才覆盖
            if dest and dest != "unknown" and dest.isdigit():
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
        """多轮对话主循环 — 先发后改策略"""
        # 开场白
        logger.info(f"[{self.ctx.uuid}] 🎙️ 播放开场白")
        await self._say_opening()
        logger.info(f"[{self.ctx.uuid}] 🎙️ 开场白播放完毕，进入监听模式")
        no_response_config = await get_no_response_config(self.ctx.script_id)
        no_response_mode = no_response_config["mode"]
        no_response_max_count = no_response_config["max_count"]
        no_response_hangup_msg = no_response_config["hangup_msg"]
        no_response_hangup_enabled = no_response_config["hangup_enabled"]
        closing_script = no_response_config["closing_script"]
        no_response_count = 0

        while self.sm.should_continue() and self.session._connected:
            # ★ 先发：监听用户，收到第一个有效文本后立即返回
            logger.info(f"[{self.ctx.uuid}] 🔊 开始监听用户 (session._connected={self.session._connected})")
            cfg = self._last_barge_in_config
            user_text, is_all_silent, is_asr_timeout = await self._listen_user(
                barge_in_enabled=cfg["enabled"],
                protect_start_ms=cfg["protect_start_ms"],
                protect_end_ms=cfg["protect_end_ms"],
                total_duration_ms=cfg["duration_ms"],
            )
            logger.info(f"[{self.ctx.uuid}] 🔊 监听结束, user_text={user_text!r} is_all_silent={is_all_silent} asr_timeout={is_asr_timeout}")

            if not user_text:
                # 无回应判定：VAD 全程静音 或 ASR 超时
                if is_all_silent:
                    logger.info(f"[{self.ctx.uuid}] 本轮全静音，不计入无回应，继续监听")
                    continue

                no_response_count += 1
                if no_response_count >= no_response_max_count:
                    # 达到最大次数，播放挂断语并结束
                    if no_response_hangup_enabled and no_response_hangup_msg:
                        hangup_msg = no_response_hangup_msg
                    elif closing_script:
                        hangup_msg = closing_script
                    else:
                        hangup_msg = "感谢接听，再见！"
                    logger.info(f"[{self.ctx.uuid}] {no_response_mode} {no_response_count} 次无回应，播放挂断语并结束通话")
                    await self._say(hangup_msg)
                    break

                logger.info(f"[{self.ctx.uuid}] 第 {no_response_count} 次无回应（{no_response_mode}），追问")
                await self._say("您好，请问您还在吗？")
                continue

            # 用户有效回应
            if no_response_mode == "consecutive":
                no_response_count = 0

            logger.info(f"[{self.ctx.uuid}] 👤 {user_text}")

            # ★ 先发后改：启动 LLM 任务 + 宽容期监听并行
            tolerance_config = await get_barge_in_config(self.ctx.script_id, "conversation")
            tolerance_enabled = tolerance_config.get("tolerance_enabled", False)
            tolerance_ms = tolerance_config.get("tolerance_ms", 1000)

            if tolerance_enabled:
                # 先发：立即启动 LLM 流式任务
                llm_task = asyncio.create_task(self._think_and_reply_stream(user_text))
                # 后改：并行启动宽容期监听
                tolerance_task = asyncio.create_task(
                    self._listen_tolerance(tolerance_ms)
                )

                done, pending = await asyncio.wait(
                    [llm_task, tolerance_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if tolerance_task in done:
                    # 宽容期内收到新文本 → 取消 LLM，合并后重新调用
                    new_text = tolerance_task.result()
                    if new_text:
                        combined = f"{user_text} {new_text}".strip()
                        logger.info(f"[{self.ctx.uuid}] 宽容期内合并文本: {combined!r}")
                        for t in pending:
                            t.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        # 用合并后的文本重新调用 LLM
                        reply_text, action = await self._think_and_reply_stream(combined)
                    else:
                        # 宽容期无新文本，等待 LLM 完成
                        reply_text, action = await llm_task
                else:
                    # LLM 先完成（宽容期内没收到新文本或超时）
                    tolerance_task.cancel()
                    await asyncio.gather(tolerance_task, return_exceptions=True)
                    reply_text, action = await llm_task
            else:
                # 无宽容期：直接调用 LLM 流式
                reply_text, action = await self._think_and_reply_stream(user_text)

            logger.info(f"[{self.ctx.uuid}] 🧠 LLM 返回: reply={reply_text[:50]!r} action={action}")

            # 播报（transfer 时先播再转，end 时先说再挂）
            if reply_text and action not in ("transfer",):
                barge_in_occurred, barge_text = await self._say(reply_text)
                if barge_in_occurred and barge_text:
                    # TTS 被打断，但 ASR 已识别到有效文本，用该文本走 LLM
                    user_text = barge_text
                    logger.info(f"[{self.ctx.uuid}] ⚡ TTS 被打断，使用 barge-in 文本走 LLM: {user_text!r}")
                    reply_text, action = await self._think_and_reply_stream(user_text)
                    if reply_text:
                        await self._say(reply_text)
                    if action == "end":
                        break
                    continue
                elif barge_in_occurred:
                    # TTS 被打断但没拿到文本，进入下一轮监听
                    logger.info(f"[{self.ctx.uuid}] ⚡ TTS 被打断但无 barge-in 文本，进入下一轮监听")
                    continue

            if action == "end":
                break

    # ─────────────────────────────────────────────────────────
    # 开场白
    # ─────────────────────────────────────────────────────────
    async def _say_opening(self):
        self.sm.transition(CallState.AI_SPEAKING)
        opening = await get_opening_for_call(self.ctx.script_id, self.ctx.customer_info)
        text = opening["reply"]
        self.ctx.messages.append({"role": "assistant", "content": text, "timestamp": int(time.time() * 1000)})
        self.ctx.ai_utterances += 1
        await self._say(text, record=False, speech_type="opening")

    # ─────────────────────────────────────────────────────────
    # 监听用户（ASR）
    # ─────────────────────────────────────────────────────────
    async def _listen_user(self, *, barge_in_enabled: bool = True,
                           protect_start_ms: int = 3000, protect_end_ms: int = 3000,
                           total_duration_ms: float = 0) -> tuple[str, bool, bool]:
        """监听用户语音。

        Returns:
            (text, is_all_silent, is_asr_timeout)
            - is_all_silent=True: 全程静音，不算无响应
            - is_asr_timeout=True: ASR 超时（可能是百炼服务慢，不应立即追问）
        """
        self.sm.transition(CallState.USER_SPEAKING)

        # 通话已断开时跳过 ASR
        if not self.session._connected:
            logger.info(f"[{self.ctx.uuid}] 会话已断开，跳过 ASR")
            return "", False, False

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

        # ★ 取消上一轮残留的 barge-in ASR 任务（防止两个 ASR 同时运行）
        if self._barge_in_asr_task and not self._barge_in_asr_task.done():
            self._barge_in_asr_task.cancel()
            await asyncio.gather(self._barge_in_asr_task, return_exceptions=True)
            self._barge_in_asr_task = None
            logger.info(f"[{self.ctx.uuid}] 已取消残留的 barge-in ASR 任务")

        queue_size = audio_queue.qsize()
        logger.info(f"[{self.ctx.uuid}] 🔊 开始监听, 音频队列当前大小={queue_size}")

        # ★ 清空队列中积累的残留音频（TTS 播放期间 forkzstream 持续捕获，
        #    队列中可能包含 TTS 回声，导致 ASR 先识别到噪声而非用户语音）
        drained = 0
        while not audio_queue.empty():
            try:
                audio_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained > 0:
            logger.info(f"[{self.ctx.uuid}] 🔊 清空残留音频队列: {drained} chunks")

        adapter = AudioStreamAdapter(
            audio_queue,
            vad_silence_ms=config.asr.vad_silence_ms,
            barge_in_cb=self._barge_in,
            barge_in_enabled=barge_in_enabled,
            total_duration_ms=total_duration_ms,
            protect_start_ms=protect_start_ms,
            protect_end_ms=protect_end_ms,
            server_vad_mode=True,  # ★ 流式 ASR：Qwen 服务端 VAD 控制语音段结束
        )

        # 噪声词过滤
        NOISE_WORDS = {
            # 基础单字
            "嗯", "嗯。", "哦", "哦。", "啊", "啊。", "呃", "呃。", "哎", "哎。",
            "你", "你。", "对", "对。", "行", "行。", "喂", "喂。",
            "好", "好。", "是", "是。",
            "你好", "你好。",
            # 重复语气词
            "嗯嗯", "嗯嗯。", "好好", "好好。", "对对", "对对。", "哦哦", "哦哦。",
            # 常见短语
            "嗯，对吧。", "嗯，可以。", "就是。", "就是", "好吧。", "好吧",
            "没事。", "没事",
            # 常见英文
            "ok", "ok。", "yes", "yes。", "no", "no。", "system", "system。",
        }

        def _is_noise_word(text: str) -> bool:
            t = text.strip()
            if not t:
                return True
            if t in NOISE_WORDS:
                return True

            # 规则1：纯英文单词（无中文字符，长度 < 10）
            if not any('\u4e00' <= c <= '\u9fff' for c in t) and len(t) < 10:
                return True

            # 规则2：重复字符模式（如"嗯嗯。"），同一字符连续出现 ≥2 次 + 可选标点
            stripped = t.rstrip('。！？?!，,、')
            if len(stripped) >= 2 and len(set(stripped)) == 1:
                return True

            # 规则3：逗号分隔的语气词组合（如"嗯，对吧。"）
            import re
            if re.match(r'^[嗯哦啊呃哎喂好是对行你]+，[嗯哦啊呃哎喂好是对行你]+[。！？]?$', t):
                return True

            # 规则4：单字 + 标点（原有逻辑）
            if len(t) <= 3 and t[0] in {"嗯", "哦", "啊", "呃", "哎", "喂", "好", "是", "对", "行", "你"} and len(t) >= 2 and t[1] in {"。", "！", "?", "…", "，", "、"}:
                return True

            return False

        final_text = ""
        asr_timed_out = False

        try:
            async with asyncio.timeout(ASR_TIMEOUT):
                async for result in self._asr_with_retry(adapter.stream(), adapter=adapter):
                    if result.is_final and result.text:
                        # ★ 过滤 1：噪声词
                        if _is_noise_word(result.text):
                            logger.info(f"[{self.ctx.uuid}] ASR 过滤噪声: {result.text!r}")
                            continue  # 噪声词不 break，继续消费下一个语音段
                        # ★ 过滤 2：RMS 能量过低（ASR VAD 误识别环境噪音）
                        #    真实语音 RMS > 1500，噪音 RMS < 1000
                        if adapter._max_rms < 1500:
                            logger.info(f"[{self.ctx.uuid}] ASR 过滤低能量段: {result.text!r} (max_rms={adapter._max_rms} < 1500)")
                            continue
                        # ★ 先发后改：第一次拿到有效语音后立即返回
                        final_text = result.text
                        logger.info(f"[{self.ctx.uuid}] ASR ✓ {result.text!r} (conf={result.confidence:.2f}, speech_ms={adapter._speech_ms:.0f}ms)")
                        break
        except asyncio.TimeoutError:
            logger.warning(f"[{self.ctx.uuid}] ASR 超时")
            asr_timed_out = True
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] ASR 异常: {e}")
        finally:
            adapter.stop()

        audio_stats = adapter.audio_stats()
        logger.info(
            f"[{self.ctx.uuid}] 音频检测: speech_detected={audio_stats['speech_detected']} "
            f"speech_ms={audio_stats['speech_ms']} max_rms={audio_stats['max_rms']}"
        )

        if not audio_stats["speech_detected"] and not asr_timed_out:
            logger.warning(f"[{self.ctx.uuid}] 本轮进入 ASR 的音频未检测到有效人声")

        logger.info(f"[{self.ctx.uuid}] 🔊 ASR 完成: final_text={final_text!r}")

        if final_text:
            self.ctx.user_utterances += 1
            self.ctx.messages.append({"role": "user", "content": final_text, "timestamp": int(time.time() * 1000)})

        return final_text, adapter._is_all_silent, asr_timed_out

    async def _listen_tolerance(self, tolerance_ms: int) -> str:
        """宽容期内继续监听补充语音。

        在 _listen_user 返回第一个有效文本后启动，
        如果在 tolerance_ms 内收到新的非噪声有效文本，返回补充文本；
        否则超时返回空字符串。

        Returns:
            补充文本（有新内容）或空字符串（超时/无有效内容）
        """
        # 复用 _is_noise_word 逻辑
        NOISE_WORDS = {
            "嗯", "嗯。", "哦", "哦。", "啊", "啊。", "呃", "呃。", "哎", "哎。",
            "你", "你。", "对", "对。", "行", "行。", "喂", "喂。",
            "好", "好。", "是", "是。",
            "你好", "你好。",
            "嗯嗯", "嗯嗯。", "好好", "好好。", "对对", "对对。", "哦哦", "哦哦。",
            "嗯，对吧。", "嗯，可以。", "就是。", "就是", "好吧。", "好吧",
            "没事。", "没事",
            "ok", "ok。", "yes", "yes。", "no", "no。", "system", "system。",
        }

        def _is_noise(text: str) -> bool:
            t = text.strip()
            if not t:
                return True
            if t in NOISE_WORDS:
                return True
            if not any('\u4e00' <= c <= '\u9fff' for c in t) and len(t) < 10:
                return True
            stripped = t.rstrip('。！？?!，,、')
            if len(stripped) >= 2 and len(set(stripped)) == 1:
                return True
            import re
            if re.match(r'^[嗯哦啊呃哎喂好是对行你]+，[嗯哦啊呃哎喂好是对行你]+[。！？]?$', t):
                return True
            if len(t) <= 3 and t[0] in {"嗯", "哦", "啊", "呃", "哎", "喂", "好", "是", "对", "行", "你"} and len(t) >= 2 and t[1] in {"。", "！", "?", "…", "，", "、"}:
                return True
            return False

        try:
            async with asyncio.timeout(tolerance_ms / 1000.0):
                # 重新启动音频捕获和 ASR
                audio_queue = await self.session.start_audio_capture()
                # 清空残留
                while not audio_queue.empty():
                    try:
                        audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                adapter = AudioStreamAdapter(
                    audio_queue,
                    vad_silence_ms=config.asr.vad_silence_ms,
                    server_vad_mode=True,
                )

                async for result in self._asr_with_retry(adapter.stream(), adapter=adapter):
                    if result.is_final and result.text:
                        if _is_noise(result.text):
                            logger.debug(f"[{self.ctx.uuid}] 宽容期过滤噪声: {result.text!r}")
                            continue
                        # ★ RMS 能量过滤（同主路径）
                        if adapter._max_rms < 1500:
                            logger.info(f"[{self.ctx.uuid}] 宽容期过滤低能量段: {result.text!r} (max_rms={adapter._max_rms})")
                            continue
                        adapter.stop()
                        logger.info(f"[{self.ctx.uuid}] 宽容期收到补充语音: {result.text!r}")
                        return result.text
        except asyncio.TimeoutError:
            logger.debug(f"[{self.ctx.uuid}] 宽容期超时 ({tolerance_ms}ms)")
        except Exception as e:
            logger.debug(f"[{self.ctx.uuid}] 宽容期异常: {e}")

        return ""

    async def _barge_in_asr_loop_with_queue(self, adapter: AudioStreamAdapter,
                                             audio_capture_task: asyncio.Task,
                                             protect_start_ms: int = 0,
                                             protect_end_ms: int = 0,
                                             tts_start_time: float = 0):
        """后台 ASR 循环，等待音频队列就绪后再开始 barge-in 检测。

        与 _barge_in_asr_loop 的区别：
        - 接收一个 Task（start_audio_capture），等待其完成后获取真实队列
        - 不阻塞 TTS 播放
        - 增加保护期检查：仅在保护期外才触发 barge-in
        """
        # barge-in 噪声词过滤 — 比主路径宽松，但也要过滤纯语气词
        # barge-in 场景：用户试图打断时需要有一定语义内容
        # 过滤：纯英文 + 空文本 + 纯单字语气词（用户听电话时的"嗯"不算打断）
        NOISE_WORDS = {
            "",  # 空文本
            # 单字语气词 + 可选标点：用户听电话时的回应，不算有效打断
            "嗯", "嗯。", "哦", "哦。", "啊", "啊。", "呃", "呃。", "哎", "哎。",
            "喂", "喂。", "好", "好。", "对", "对。", "是", "是。",
            "行", "行。", "你", "你。",
            # 重复语气词
            "嗯嗯", "嗯嗯。", "好好", "好好。", "对对", "对对。",
        }

        def _is_noise(text: str) -> bool:
            """barge-in 噪声过滤：丢弃纯英文、空文本、纯单字语气词"""
            t = text.strip()
            if not t:
                return True
            if t in NOISE_WORDS:
                return True
            # 纯英文单词（无中文字符，长度 < 10）→ 可能是系统识别噪声
            if not any('\u4e00' <= c <= '\u9fff' for c in t) and len(t) < 10:
                return True
            # 单字 + 标点（如"嗯！""啊？"等变体）
            if len(t) <= 3 and t[0] in {"嗯", "哦", "啊", "呃", "哎", "喂", "好", "是", "对", "行", "你"} and len(t) >= 2 and t[1] in {"。", "！", "?", "…", "，", "、"}:
                return True
            # 不是噪声，这是有效的打断内容
            return False

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
            adapter._queue = audio_queue
            logger.info(f"[{self.ctx.uuid}] barge-in 音频队列已就绪, 大小={audio_queue.qsize()}")

            async with asyncio.timeout(ASR_TIMEOUT):
                # ★ 调试：跟踪 barge-in ASR 中间结果和音频统计
                _barge_asr_chunk_count = 0
                _barge_asr_interim_count = 0

                async for result in self._asr_with_retry(adapter.stream(), adapter=adapter):
                    _barge_asr_chunk_count += 1
                    if not result.is_final:
                        _barge_asr_interim_count += 1
                        # 中间结果仅用于调试，不触发 barge-in
                        logger.debug(f"[{self.ctx.uuid}] barge-in ASR 中间结果 #{_barge_asr_interim_count}: {result.text!r}")
                        continue
                    if result.text:
                        # ★ 用 is_final 最终结果触发 barge-in，确保识别准确
                        if _is_noise(result.text):
                            logger.debug(f"[{self.ctx.uuid}] barge-in ASR 过滤噪声: {result.text!r}")
                            continue
                        # 检查保护期
                        if tts_start_time > 0:
                            elapsed_ms = (time.time() - tts_start_time) * 1000
                            if elapsed_ms < protect_start_ms:
                                logger.debug(f"[{self.ctx.uuid}] barge-in ASR: 在保护期内（起始 {elapsed_ms:.0f}ms < {protect_start_ms}ms）")
                                continue
                            if protect_end_ms > 0:
                                pass

                        logger.info(f"[{self.ctx.uuid}] ⚡ barge-in ASR 触发(is_final): {result.text!r}")
                        # 保存识别文本，供 _say() 返回给主循环使用
                        self._barge_in_text = result.text
                        if not self._barge_in.is_set():
                            self._barge_in.set()

                # ★ 调试：ASR 循环结束统计
                logger.info(
                    f"[{self.ctx.uuid}] barge-in ASR 循环结束: "
                    f"chunks={_barge_asr_chunk_count}, interim={_barge_asr_interim_count}, "
                    f"adapter_chunks={adapter._total_chunks}, adapter_speech={adapter._speech_chunks}, "
                    f"adapter_max_rms={adapter._max_rms}, adapter_speech_ms={adapter._speech_ms:.0f}ms"
                )
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
                async for result in self._asr_with_retry(adapter.stream(), adapter=adapter):
                    if result.is_final and result.text:
                        # 过滤噪音词（背景噪音被 ASR 误识别）
                        if result.text.strip() in {"嗯", "嗯。", "哦", "哦。", "啊", "啊。", "呃", "呃。", "哎", "哎。", "喂", "喂。"}:
                            logger.debug(f"[{self.ctx.uuid}] barge-in ASR 过滤噪声: {result.text!r}")
                            continue
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

    async def _asr_with_retry(self, audio_gen: AsyncGenerator[bytes, None],
                               adapter=None):
        """ASR 带超时的流式识别"""
        async def _gen_with_timeout():
            async for chunk in audio_gen:
                yield chunk

        async for result in self.asr.recognize_stream(_gen_with_timeout(), call_uuid=self.ctx.uuid, adapter=adapter):
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
            self.ctx.messages.append({"role": "assistant", "content": reply_text, "timestamp": int(time.time() * 1000)})
            self.ctx.ai_utterances += 1
            logger.info(f"[{self.ctx.uuid}] 🤖 {reply_text[:80]} [action={action}]")

        return reply_text, action

    async def _think_and_reply_stream(self, user_text: str) -> tuple[str, str]:
        """流式 LLM → 按句切分 → 逐句 TTS 播报 → 解析决策。

        流程:
        1. 启动 LLM 流式输出
        2. 按句子边界（。！？）切分文本
        3. 每凑够一句 → 立即调 _say() 播放（复用现有 TTS 流式 + barge-in）
        4. 每句播放完检查 barge-in，如果被打断 → 取消 LLM stream，返回 barge-in 文本
        5. LLM 流结束 → 解析 <决策> 获取 intent/action
        6. 执行 action（transfer/end 等）

        Returns:
            ("", action) — reply_text 为空表示已逐句播报，调用方不应再调 _say()
            或 (barge_text, "continue") — 被打断时返回 barge-in 文本
        """
        self.sm.transition(CallState.PROCESSING)
        t0 = time.time()
        sentence_buffer = ""
        full_reply = ""
        final_decision = None

        try:
            async for item in self.llm.chat_stream(
                messages=self.ctx.messages,
                system_prompt=self._system_prompt,
            ):
                if isinstance(item, dict):
                    # 最终决策
                    final_decision = item
                    break

                # item 是文本 token
                sentence_buffer += item
                full_reply += item

                # 按句子边界切分
                sentences = re.split(r'(?<=[。！？])', sentence_buffer)
                if len(sentences) > 1:
                    # 最后一项可能是不完整的句子（没有结束标点）
                    complete_sentences = sentences[:-1]
                    remainder = sentences[-1]
                    sentence_buffer = remainder or ""

                    for sentence in complete_sentences:
                        sentence = sentence.strip()
                        if not sentence:
                            continue

                        barge_occurred, barge_text = await self._say(sentence)
                        if barge_occurred:
                            # 播放期间被打断，停止后续句子，进入下一轮监听
                            logger.info(f"[{self.ctx.uuid}] ⚡ 流式播报中被用户打断, text={barge_text!r}")
                            return ("", "continue")

            # 播放剩余的未完成句子（如有）
            sentence_buffer = sentence_buffer.strip()
            if sentence_buffer:
                barge_occurred, _ = await self._say(sentence_buffer)
                if barge_occurred:
                    logger.info(f"[{self.ctx.uuid}] ⚡ 流式播报末尾被打断")
                    return ("", "continue")

            # 解析决策
            if final_decision:
                action = final_decision.get("action", "continue")
            else:
                # 未收到决策，用旧方法解析完整回复
                parsed = self.llm._parse_response(full_reply)
                action = parsed["action"]

        except asyncio.CancelledError:
            logger.info(f"[{self.ctx.uuid}] 流式 LLM 任务被取消")
            return ("", "continue")
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] 流式 LLM 异常: {e}")
            return ("抱歉，我这边网络有些问题，请问您方便稍后再聊吗？", "end")

        elapsed = (time.time() - t0) * 1000
        logger.info(f"[{self.ctx.uuid}] 流式 LLM 总耗时 {elapsed:.0f}ms, action={action}")

        # 记录到对话历史
        if full_reply.strip():
            self.ctx.messages.append(
                {"role": "assistant", "content": full_reply.strip(), "timestamp": int(time.time() * 1000)}
            )
            self.ctx.ai_utterances += 1
            logger.info(f"[{self.ctx.uuid}] 🤖 {full_reply.strip()[:80]} [action={action}]")

        # 返回空 reply_text 标记已逐句播报，_conversation_loop 不应再调 _say()
        return ("", action)

    # ─────────────────────────────────────────────────────────
    # TTS + 播放（支持 barge-in，流式输出）
    # ─────────────────────────────────────────────────────────
    async def _say(self, text: str, record: bool = True,
                   speech_type: str = "conversation") -> tuple[bool, str]:
        """播放 TTS 文本。

        Returns:
            (barge_in_occurred, barge_in_text)
            - barge_in_occurred=True: 被打断，barge_in_text 为 ASR 识别到的文本（可能为空）
            - barge_in_occurred=False: 正常播放完成，barge_in_text 为空
        """
        if not text or not self.session._connected:
            return False, ""
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

        # ★ 修复：如果上一次 _say 触发了 barge-in（发送了 ttsstop_clean），
        #    需要发送 ttsstart 来恢复 FreeSWITCH 端的 TTS 管道
        if self._last_say_had_barge_in:
            self._last_say_had_barge_in = False
            await self.session.restart_playback()
            logger.info(f"[{self.ctx.uuid}] 已发送 ttsstart，恢复 TTS 管道")

        try:
            t0 = time.time()
            # 流式 TTS：边合成边播
            audio_chunks = self._tts_stream_with_timeout(text)
            logger.debug(f"[{self.ctx.uuid}] TTS 流式开始")

            # barge-in：如果用户已经开始说话则跳过播放
            if self._barge_in.is_set():
                barge_text = self._barge_in_text
                self._barge_in_text = ""  # 清空
                logger.info(f"[{self.ctx.uuid}] ⚡ barge-in 检测到，跳过播放, text={barge_text!r}")
                return True, barge_text

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
            self._barge_in_asr_task = asyncio.create_task(
                self._barge_in_asr_loop_with_queue(
                    barge_in_adapter, audio_capture_task,
                    protect_start_ms=protect_start_ms,
                    protect_end_ms=protect_end_ms,
                    tts_start_time=t0,
                )
            )
            logger.info(f"[{self.ctx.uuid}] barge-in ASR 任务已启动（后台音频采集中）")

            # ★ TTS 播放：逐 chunk 检查 barge-in，被打断则立即停止
            barge_in_detected = False
            async for chunk in audio_chunks:
                if not chunk:
                    continue
                # 每收到一个 TTS chunk 就检查是否被用户打断
                if self._barge_in.is_set():
                    logger.info(f"[{self.ctx.uuid}] ⚡ barge-in 触发，停止 TTS 播放 (已播 {t0:.1f}s)")
                    barge_in_detected = True
                    # 停止 FreeSWITCH 端播放
                    await self.session.stop_playback()
                    break
                # 发送音频 chunk
                sent = await self.session.forkzstream_ws_server.send_audio(self.session._uuid, chunk)
                if not sent:
                    logger.warning(f"[{self.ctx.uuid}] TTS 音频发送失败")
                    break

            if barge_in_detected:
                # 被打断：立即取消后台 ASR 任务，不等待超时
                self._last_say_had_barge_in = True  # 标记，供下次 _say 发送 ttsstart
                barge_in_adapter.stop()
                self._barge_in_asr_task.cancel()
                await asyncio.gather(self._barge_in_asr_task, return_exceptions=True)
                self._barge_in_asr_task = None
                # 获取 barge-in ASR 识别到的文本
                barge_text = self._barge_in_text
                self._barge_in_text = ""  # 清空
                logger.info(f"[{self.ctx.uuid}] ⚡ barge-in 处理完成, text={barge_text!r}")
                return True, barge_text

            # TTS 播放完成后设置主 ASR 就绪
            self._last_say_had_barge_in = False  # 正常播放完成，清除标记
            self._main_asr_ready.set()

            logger.debug(f"[{self.ctx.uuid}] TTS 播放完成，耗时 {(time.time()-t0)*1000:.0f}ms")

            # ★ 关键修复：TTS 播放完成后不取消 barge-in ASR 任务
            #    让它在 _listen_user 启动前继续监听用户语音（填补 TTS→listen 的空窗期）
            #    barge-in ASR 会在 ASR_TIMEOUT 后自行退出，或被 _listen_user 的新 ASR 替代
            # barge_in_adapter.stop()  ← 已移除
            # await asyncio.gather(...) ← 已移除

        except asyncio.TimeoutError:
            logger.error(f"[{self.ctx.uuid}] TTS 超时")
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] TTS/播放失败: {e}", exc_info=True)
        finally:
            self._barge_in.set()  # 播放结束，允许新一轮录音

        return False, ""

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

        # sofia A-leg 录音（用户侧语音）
        aleg_uuid = self.session.channel_vars.get("other_loopback_from_uuid", "")
        if aleg_uuid and aleg_uuid not in ("-ERR", "_undef_"):
            self.ctx.aleg_recording_path = f"{config.freeswitch.recording_path}/{aleg_uuid}.wav"

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

        # 写 CDR（异步后台任务，不阻塞挂断流程）
        try:
            asyncio.create_task(save_call_record(self.ctx))
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
