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
from backend.services.async_script_utils import get_system_prompt_for_call, get_opening_for_call

logger = logging.getLogger(__name__)

# 单路通话最长时长（秒），防止 LLM 循环或静音时挂死
MAX_CALL_DURATION = int(config.__dict__.get("max_call_duration", 300))
# 用户无响应后最大重问次数
MAX_SILENCE_RETRIES = 5
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
    """

    def __init__(
        self,
        audio_queue: asyncio.Queue,
        vad_silence_ms: int = 500,
        barge_in_cb: Optional[asyncio.Event] = None,
    ):
        self._queue = audio_queue
        self._vad_silence_ms = vad_silence_ms
        self._barge_in_event = barge_in_cb
        self._stopped = False
        self._started_speaking = False  # 是否已经检测到用户开始说话

    def stop(self):
        self._stopped = True

    async def stream(self) -> AsyncGenerator[bytes, None]:
        last_audio_ts = time.time()
        while not self._stopped:
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=0.08)
            except asyncio.TimeoutError:
                silence_ms = (time.time() - last_audio_ts) * 1000
                # 首次等待：2 秒没音频说明用户没说话
                # 说话中：500ms 停顿认为用户说完
                threshold = 2000 if not self._started_speaking else self._vad_silence_ms
                if silence_ms >= threshold:
                    break
                continue
            if not chunk:
                break
            # 触发 barge-in（如果 AI 正在说话）
            if self._barge_in_event and not self._barge_in_event.is_set():
                self._barge_in_event.set()
            if not self._started_speaking:
                self._started_speaking = True
            last_audio_ts = time.time()
            yield chunk
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
    ):
        self.session = session
        self.ctx = context
        self.sm = StateMachine(context)

        self.asr = asr or create_asr_client()
        self.tts = tts or create_tts_client()
        self.llm = llm or LLMService()

        self._system_prompt: str = ""  # 在 connect() 后从 channel vars 构建
        self._barge_in = asyncio.Event()
        self._barge_in.set()  # 初始置位（表示目前没有在播放）

        # 注册状态机动作
        self.sm.register_handler("transfer",  self._do_transfer)
        self.sm.register_handler("end",       self._do_hangup)
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

            # 4. 构建 System Prompt
            self._system_prompt = await get_system_prompt_for_call(
                self.ctx.script_id, self.ctx.customer_info
            )

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

            if self.ctx.result == CallResult.NOT_ANSWERED:
                self.ctx.result = CallResult.COMPLETED

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
            user_text = await self._listen_user()
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
        await self._say(text, record=False)

    # ─────────────────────────────────────────────────────────
    # 监听用户（ASR）
    # ─────────────────────────────────────────────────────────
    async def _listen_user(self) -> str:
        self.sm.transition(CallState.USER_SPEAKING)
        audio_queue = await self.session.start_audio_capture()
        queue_size = audio_queue.qsize()
        logger.info(f"[{self.ctx.uuid}] 🔊 开始监听, 音频队列当前大小={queue_size}")

        # 注意：不清空音频队列。在测试通话场景中，前端在 TTS 播放期间
        # 已持续发送音频到队列，清空会导致丢失用户语音。
        # 在真实 ESL 场景中，音频来自 FreeSWITCH 实时流，不会有残留问题。

        adapter = AudioStreamAdapter(
            audio_queue,
            vad_silence_ms=config.asr.vad_silence_ms,
            barge_in_cb=self._barge_in,
        )

        final_text = ""
        asr_result_count = 0
        try:
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
    # TTS + 播放（支持 barge-in）
    # ─────────────────────────────────────────────────────────
    async def _say(self, text: str, record: bool = True):
        if not text or not self.session._connected:
            return
        self.sm.transition(CallState.AI_SPEAKING)
        self._barge_in.clear()  # 开始播放，barge-in 检测生效

        try:
            t0 = time.time()
            audio_path = await asyncio.wait_for(
                self.tts.synthesize(text), timeout=TTS_TIMEOUT
            )
            logger.debug(f"[{self.ctx.uuid}] TTS {(time.time()-t0)*1000:.0f}ms → {audio_path}")

            # barge-in：如果用户已经开始说话则跳过播放
            if self._barge_in.is_set():
                logger.debug(f"[{self.ctx.uuid}] barge-in 检测到，跳过播放")
                return

            await self.session.play(audio_path)
        except asyncio.TimeoutError:
            logger.error(f"[{self.ctx.uuid}] TTS 超时")
        except Exception as e:
            logger.error(f"[{self.ctx.uuid}] TTS/播放失败: {e}")
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
        await self._say("好的，正在为您转接人工客服，请稍等。")
        await asyncio.sleep(0.3)
        await self.session.transfer_to_human(ext)
        # 写入转接原因供 CDR
        await self.session.set_variable("transfer_reason", self.ctx.intent.value)

    async def _do_hangup(self, params: dict):
        farewell = params.get("farewell", "好的，感谢您的时间，祝您生活愉快，再见！")
        logger.info(f"[{self.ctx.uuid}] 主动挂断")
        await self._say(farewell)
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

    async def _do_blacklist(self, params: dict):
        reason = params.get("reason", "用户拒绝")
        logger.info(f"[{self.ctx.uuid}] 加入黑名单: {self.ctx.phone_number}")
        await crm.add_to_blacklist(self.ctx.phone_number, reason)

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
