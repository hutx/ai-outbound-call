"""外呼语音 Agent - 基于 LiveKit AgentSession"""
import json
import logging
import asyncio
import re
import time
import uuid
from datetime import datetime
from typing import Optional

from livekit import agents, api as livekit_api, rtc
from livekit.agents import Agent, AgentSession, AutoSubscribe
from livekit.agents.voice.turn import TurnHandlingOptions
from livekit.plugins import silero

from backend.core.config import settings
from backend.core.events import CallState, CallAction, CallIntent, CallResult
from backend.plugins.aliyun_stt import AliyunSTT
from backend.plugins.aliyun_tts import AliyunTTS
from backend.plugins.qwen_llm import (
    create_qwen_llm,
    build_system_prompt,
    parse_llm_response,
    StreamingResponseParser,
    LLMResponse,
)
from backend.services.script_service import script_service
from backend.services import call_record_service
from backend.models.call_record import CallRecordCreate, CallRecordUpdate, TranscriptEntry
from backend.agent.dialog_manager import DialogManager
from backend.utils import db
from backend.services.egress_service import EgressService
from backend.services.file_service import create_file_record
from backend.services.call_record_detail_service import create_detail
from backend.services.minio_service import minio_service
from backend.models.call_record_detail import CallRecordDetailCreate
from backend.models.file import FileRecordCreate

logger = logging.getLogger(__name__)


class OutboundCallAgent:
    """外呼语音 Agent

    负责管理一次完整的外呼通话：
    1. 解析 Room metadata 获取 script_id / task_id / phone
    2. 创建 CDR 记录
    3. 加载话术并构建系统提示
    4. 创建 AgentSession（STT + LLM + TTS + VAD）
    5. 播放开场白并进入多轮对话循环
    6. 处理打断/宽容期/无响应
    7. 保存最终 CDR
    """

    def __init__(self, ctx: agents.JobContext):
        self.ctx = ctx
        self.call_id = ctx.room.name

        # 解析 room metadata
        meta = json.loads(ctx.room.metadata or "{}")
        self.script_id = meta.get("script_id", "default")
        self.task_id = meta.get("task_id")
        self.phone = meta.get("phone") or ""
        self.customer_info = meta.get("customer_info", {})

        if not self.phone:
            logger.warning(f"Room metadata 中缺少 phone 字段，call_id={self.call_id}")

        # 对话历史
        self.messages: list[dict] = []  # [{"role": "user"/"assistant", "content": "..."}]
        self.transcript: list[dict] = []  # CDR transcript entries

        # Agent 组件（在 run 中初始化）
        self._agent: Optional[Agent] = None
        self._session: Optional[AgentSession] = None
        self._llm = None
        self._system_prompt: str = ""
        self._transcript_queue: Optional[asyncio.Queue] = None
        self.dialog_mgr: Optional[DialogManager] = None

        # 录音 & 详情记录
        self._call_record_id: Optional[int] = None   # CDR 主键 ID
        self._egress_service: Optional[EgressService] = None
        self._egress_id: Optional[str] = None          # Egress 任务 ID
        self._answered_at: Optional[datetime] = None   # 接听时间（用于计算总时长）

    async def run(self):
        """主运行函数"""
        logger.info(
            f"Agent 启动: call_id={self.call_id}, script_id={self.script_id}, "
            f"task_id={self.task_id}, phone={self.phone}"
        )

        try:
            # 1. 初始化数据库连接
            await db.get_pool()

            # 2. 创建 CDR 记录（初始状态为 RINGING，等真正接听后再设 CONNECTED）
            cdr = await call_record_service.create_record(
                CallRecordCreate(
                    call_id=self.call_id,
                    task_id=self.task_id,
                    phone=self.phone,
                    script_id=self.script_id,
                )
            )
            self._call_record_id = cdr.get("id")
            await call_record_service.update_record(
                self.call_id,
                CallRecordUpdate(status=CallState.RINGING),
            )

            # 3. 加载话术
            script = await script_service.get_script(self.script_id)
            if script is None:
                logger.error(f"话术未找到: {self.script_id}，使用默认配置")
                script = self._default_script()

            # 4. 初始化对话管理器
            self.dialog_mgr = DialogManager(script)

            # 5. 构建系统提示
            self._system_prompt = build_system_prompt(
                script.get("main_prompt", "请根据用户的回答进行自然的对话。"),
                self.customer_info,
            )

            # 6. 创建 AI 组件
            stt = AliyunSTT()
            self._llm = create_qwen_llm()
            tts_engine = AliyunTTS()
            vad = silero.VAD.load(
                activation_threshold=0.5,
                min_speech_duration=0.2,   # 200ms → 0.2s
                min_silence_duration=0.5,   # 500ms → 0.5s
            )

            # 7. 创建 Agent（手动回合模式，由我们控制对话循环）
            self._agent = Agent(
                instructions=self._system_prompt,
                stt=stt,
                llm=self._llm,
                tts=tts_engine,
                vad=vad,
                turn_handling=TurnHandlingOptions(
                    turn_detection="manual",
                    interruption={
                        "enabled": True,
                        "mode": "vad",
                        "min_duration": 0.3,   # 用户说话≥300ms 触发打断
                        "min_words": 0,         # 无需等待关键词，开口即打断
                    },
                    preemptive_generation={
                        "enabled": False,        # 非预测模式（节省 Token）
                    },
                ),
            )

            # 8. 创建 AgentSession
            self._session = AgentSession()

            # 9. 设置用户输入监听队列
            self._transcript_queue = asyncio.Queue()
            # 注意：LiveKit Agents SDK 1.5.x 的 EventEmitter 不允许直接注册 async 回调
            # 必须用同步包装器，内部通过 asyncio.create_task() 调用异步方法
            def _on_user_input_sync(evt):
                asyncio.create_task(self._on_user_input(evt))
            self._session.on("user_input_transcribed", _on_user_input_sync)

            # 10. 连接到 Room
            await self.ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

            # 10.1 重新解析 Room metadata（connect 后 metadata 更可靠）
            meta = json.loads(self.ctx.room.metadata or "{}")
            if meta.get("phone"):
                self.script_id = meta.get("script_id", self.script_id)
                self.task_id = meta.get("task_id", self.task_id)
                self.phone = meta.get("phone") or self.phone
                self.customer_info = meta.get("customer_info", self.customer_info)
                logger.info(f"Room metadata 已重新解析: script_id={self.script_id}, task_id={self.task_id}, phone={self.phone}")

            # 10.2 修正 CDR 中的 task_id 和 phone（step 2 创建时可能为空）
            if self.task_id:
                try:
                    await db.execute(
                        """
                        UPDATE lk_call_records
                        SET task_id = $1, phone = $2
                        WHERE call_id = $3 AND (task_id IS NULL OR task_id = '')
                        """,
                        self.task_id, self.phone, self.call_id,
                    )
                    logger.info(f"CDR task_id 已修正: call_id={self.call_id}, task_id={self.task_id}, phone={self.phone}")
                except Exception as e:
                    logger.warning(f"修正 CDR task_id 失败: {e}")

            # 11. 等待 SIP 参与者加入
            #    由于 sip_service 启用了 wait_until_answered=True，
            #    SIP 参与者加入 Room 即意味着被叫已真正接听。
            #    但实际中参与者可能在振铃阶段就加入，因此还需等待音频轨道发布
            #    作为真正接通的确认信号。
            try:
                participant = await asyncio.wait_for(
                    self._wait_for_sip_participant(),
                    timeout=settings.originate_timeout_sec + 10,  # 略长于 SIP 振铃超时
                )
                logger.info(f"SIP 参与者已加入: {participant.identity}, 等待音频轨道确认接通...")

                # 11a. 等待音频轨道发布（真正接通的确认信号）
                await asyncio.wait_for(
                    self._wait_for_audio_track(participant),
                    timeout=settings.originate_timeout_sec + 10,
                )
                logger.info(f"SIP 参与者音频轨道已就绪（已接听）: {participant.identity}")

            except (asyncio.TimeoutError, RuntimeError) as wait_err:
                reason = "超时（未接听）" if isinstance(wait_err, asyncio.TimeoutError) else f"房间断开（{wait_err}）"
                logger.warning(f"等待 SIP 参与者失败: {reason}")
                await call_record_service.update_record(
                    self.call_id,
                    CallRecordUpdate(
                        status=CallState.ENDED,
                        result=CallResult.NOT_ANSWERED,
                        sip_code=408,
                        hangup_cause="no_answer_timeout" if isinstance(wait_err, asyncio.TimeoutError) else "room_disconnected",
                    ),
                )
                return

            # 11.1 SIP 参与者音频就绪 = 被叫已接听，更新 CDR 状态（后台，不阻塞）
            self._answered_at = datetime.now()
            asyncio.create_task(self._update_cdr_connected())

            # 11.5 启动 Egress 录制（后台，不阻塞开场白）
            asyncio.create_task(self._start_egress_background())

            # 12. 启动 AgentSession（必须等待完成，才能播报）
            await self._session.start(self._agent, room=self.ctx.room)

            # 13. 播放开场白
            opening_text = script.get("opening_text", "")
            if opening_text:
                logger.info(f"播放开场白: {opening_text[:50]}...")
                await self._speak(opening_text, allow_interruptions=script.get("barge_in_opening", False))
                pause_ms = script.get("opening_pause_ms", 0)
                if pause_ms > 0:
                    await asyncio.sleep(pause_ms / 1000.0)

            # 14. 多轮对话循环
            loop_result = await self._conversation_loop(script)

            # 15. 根据结束原因决定是否播放结束语
            # - "no_response_hangup": 无响应挂断，hangup_text 已在循环内播完，不再播 closing_text
            # - "intent_end" / 其他: 用户意图结束，播放结束语并等待完毕后再断开
            if loop_result != "no_response_hangup":
                closing_text = script.get("closing_text", "")
                # 达到最大轮次时，在结束语前加上提示
                if loop_result == "max_rounds" and closing_text:
                    closing_text = "您的通话时长已到，" + closing_text
                if closing_text:
                    logger.info(f"播放结束语: {closing_text[:50]}...")
                    await self._speak(closing_text, allow_interruptions=script.get("barge_in_closing", False))

        except Exception as e:
            logger.error(f"Agent 异常: {e}", exc_info=True)
            try:
                await call_record_service.update_record(
                    self.call_id,
                    CallRecordUpdate(status=CallState.ENDED, result=CallResult.ERROR),
                )
            except Exception as ex:
                logger.error(f"保存异常 CDR 失败: {ex}")
        finally:
            # 15.5 停止 Egress 录制并保存录音文件信息
            # 使用 asyncio.shield 防止 SDK 取消 entrypoint 时中断清理逻辑
            try:
                await asyncio.shield(self._stop_recording())
            except asyncio.CancelledError:
                pass  # shield 内部已完成，忽略外层取消
            # 16. 保存最终 CDR
            await self._finalize_cdr()
            # 17. 挂断 SIP 通话（删除 Room → SIP BYE → Zoiper 端通话结束）
            await self._hangup_sip_call()
            # 清理 session
            if self._session is not None:
                try:
                    await self._session.aclose()
                except Exception as e:
                    logger.warning(f"关闭 AgentSession 时出错: {e}")
            logger.info(f"Agent 结束: call_id={self.call_id}")

    # ------------------------------------------------------------------
    # 对话循环
    # ------------------------------------------------------------------

    def _is_session_alive(self) -> bool:
        """检查 AgentSession 是否仍在运行"""
        if self._session is None:
            return False
        # AgentSession 在关闭后 _running 标志为 False
        # 兼容不同版本：优先检查 _running 属性，若不存在则假设存活
        return getattr(self._session, '_running', True)

    async def _conversation_loop(self, script: dict) -> str:
        """多轮对话主循环

        Returns:
            结束原因字符串:
            - "no_response_hangup": 连续无响应达到阈值，hangup_text 已播报完毕
            - "intent_end": LLM 决策结束（closing_text 将在循环外播报）
            - "session_closed": Session 已关闭（SIP 断开等）
            - "max_rounds": 达到最大轮次
        """
        for round_num in range(settings.max_conversation_rounds):
            # 每轮开始前检查 session 是否存活
            if not self._is_session_alive():
                logger.warning("AgentSession 已关闭，退出对话循环")
                return "session_closed"

            logger.info(f"--- 第 {round_num + 1} 轮对话 ---")

            # 轮次计时起点
            round_start_time = datetime.now()
            t_stt_start = time.monotonic()

            try:
                # A. 等待用户说话（带超时）
                t_wait_start = time.monotonic()
                user_text = await self._wait_for_user_input()
                t_wait_end = time.monotonic()

                if user_text is None:
                    # 无响应处理
                    result = self.dialog_mgr.on_no_response()
                    logger.info(
                        f"无响应: consecutive={result['consecutive_count']}, "
                        f"cumulative={result['cumulative_count']}, hangup={result['should_hangup']}"
                    )
                    await self._speak(result["prompt_text"])
                    if result["should_hangup"]:
                        logger.info("无响应达到阈值，hangup_text 已播报完毕，直接结束通话（不再播 closing_text）")
                        return "no_response_hangup"
                    continue

                # B. 宽容期处理
                t_tolerance_start = time.monotonic()
                if self.dialog_mgr.tolerance.enabled:
                    user_text = await self.dialog_mgr.wait_with_tolerance(
                        user_text,
                        listen_func=self._listen_additional,
                    )
                t_tolerance_end = time.monotonic()

                # STT 延迟：从轮次开始（等待用户输入开始）到用户文字完全接收
                t_stt_end = time.monotonic()
                stt_latency_ms = int((t_stt_end - t_stt_start) * 1000)

                # C. 记录用户输入
                self.dialog_mgr.on_user_responded(user_text)
                self.messages.append({"role": "user", "content": user_text})
                self.transcript.append({
                    "role": "user",
                    "text": user_text,
                    "timestamp": time.time(),
                    "duration_sec": 0,
                })
                t_user_received = time.monotonic()
                logger.info(
                    f"用户: {user_text} "
                    f"[perf: 等待={(t_wait_end - t_wait_start)*1000:.0f}ms, "
                    f"宽容期={(t_tolerance_end - t_tolerance_start)*1000:.0f}ms]"
                )

                # D. LLM 推理 + 流式播报（边生成边播，降低延迟）
                llm_response = await self._stream_llm_and_speak(
                    allow_interruptions=self.dialog_mgr.barge_in.enabled,
                )

                # E. 记录 AI 回复
                if llm_response.text:
                    logger.info(f"AI: {llm_response.text[:100]}...")
                    self.dialog_mgr.on_ai_speaking_end()

                    self.messages.append({"role": "assistant", "content": llm_response.text})
                    self.transcript.append({
                        "role": "ai",
                        "text": llm_response.text,
                        "timestamp": time.time(),
                        "duration_sec": 0,
                    })

                # 轮次结束时间
                round_end_time = datetime.now()

                # E2. 记录本轮问答详情
                try:
                    if self._call_record_id and llm_response.text:
                        detail = CallRecordDetailCreate(
                            call_record_id=self._call_record_id,
                            call_id=self.call_id,
                            round_num=round_num + 1,
                            question=user_text,
                            answer_content=llm_response.text,
                            is_interrupted=llm_response.was_interrupted,
                            start_time=round_start_time,
                            end_time=round_end_time,
                            stt_latency_ms=stt_latency_ms,
                            llm_latency_ms=llm_response.llm_latency_ms,
                            tts_latency_ms=llm_response.tts_latency_ms,
                        )
                        await create_detail(detail)
                        logger.info(
                            f"轮次详情已记录: round={round_num + 1}, "
                            f"stt={stt_latency_ms}ms, "
                            f"llm={llm_response.llm_latency_ms}ms, "
                            f"tts={llm_response.tts_latency_ms}ms, "
                            f"interrupted={llm_response.was_interrupted}"
                        )
                except Exception as e:
                    logger.warning(f"记录轮次详情失败（不影响通话）: {e}")

                # F. 处理动作
                if llm_response.action == CallAction.END:
                    logger.info("LLM 决策: 结束通话，将在循环外播报 closing_text")
                    return "intent_end"
                elif llm_response.action == CallAction.TRANSFER:
                    logger.info("LLM 决策: 转接人工")
                    # TODO: 实现转接逻辑
                    return "intent_end"
                elif llm_response.action == CallAction.CALLBACK:
                    logger.info("LLM 决策: 预约回拨")
                    # TODO: 实现回拨逻辑
                    return "intent_end"
                elif llm_response.action == CallAction.BLACKLIST:
                    logger.info("LLM 决策: 加入黑名单")
                    # TODO: 实现黑名单逻辑
                    return "intent_end"

            except RuntimeError as e:
                logger.warning(f"对话循环中 Session 异常，退出循环: {e}")
                return "session_closed"

        # 达到最大轮次
        logger.info(f"达到最大对话轮次 ({settings.max_conversation_rounds})，结束通话")
        return "max_rounds"

    # ------------------------------------------------------------------
    # 用户输入监听
    # ------------------------------------------------------------------

    async def _on_user_input(self, evt):
        """处理用户语音转录事件"""
        if not evt.is_final:
            return
        text = (evt.transcript or "").strip()
        if not text:
            return
        t_now = time.monotonic()
        logger.info(f"STT 转录（final）: {text} [perf: monotonic={t_now:.3f}]")
        if self._transcript_queue is not None:
            self._transcript_queue.put_nowait(text)

    async def _wait_for_user_input(self) -> Optional[str]:
        """等待用户输入（带无响应超时）"""
        if self._transcript_queue is None:
            return None
        try:
            text = await asyncio.wait_for(
                self._transcript_queue.get(),
                timeout=self.dialog_mgr.no_response.timeout_sec,
            )
            return text
        except asyncio.TimeoutError:
            return None

    async def _listen_additional(self) -> Optional[str]:
        """宽容期内监听额外输入"""
        if self._transcript_queue is None:
            return None
        parts: list[str] = []
        deadline = asyncio.get_event_loop().time() + self.dialog_mgr.tolerance.duration_ms / 1000.0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                text = await asyncio.wait_for(
                    self._transcript_queue.get(),
                    timeout=remaining,
                )
                parts.append(text)
            except asyncio.TimeoutError:
                break
        return " ".join(parts) if parts else None

    # ------------------------------------------------------------------
    # LLM 调用（流式 + 实时 TTS）
    # ------------------------------------------------------------------

    # 句末标点：在这些标点处可以切分句子交给 TTS
    _SENTENCE_END_RE = re.compile(r'[。！？;；]')

    @staticmethod
    def _split_at_sentence(text: str):
        """在句末标点处切分，返回 (complete_sentence, remaining)。

        如果没有句末标点则返回 (None, text)，让调用方继续缓冲。
        """
        m = OutboundCallAgent._SENTENCE_END_RE.search(text)
        if m is None:
            return None, text
        idx = m.end()  # 包含句末标点
        return text[:idx], text[idx:]

    async def _stream_llm_and_speak(
        self,
        allow_interruptions: bool = True,
    ) -> LLMResponse:
        """流式调用 LLM，边生成边播报（首句即播），降低端到端延迟。

        与旧逻辑 _get_llm_response + _speak 的区别：
          - 旧：LLM 全部输出 → 再全部交给 TTS → 等待播完
          - 新：LLM 每产出一个完整句子 → 立即 session.say() 交给 TTS
                  用户在 LLM 仍在生成后续句子时就能听到第一句
        """
        # 构建 ChatContext
        chat_ctx = agents.ChatContext()
        if self._system_prompt:
            chat_ctx.add_message(role="system", content=self._system_prompt)
        for msg in self.messages:
            chat_ctx.add_message(role=msg["role"], content=msg["content"])

        parser = StreamingResponseParser()
        sentence_buf = ""
        speech_handles: list = []
        t0 = time.monotonic()  # 整体起始时间
        t_llm_start = t0
        first_token_received = False
        first_sentence_sent = False
        t_first_token = None
        t_first_sentence = None

        def _speak_now(txt: str):
            """将文本交给 session.say()（非阻塞），同时记录 handle"""
            if not txt or not txt.strip():
                return
            if self._session is None:
                return
            t_speak_now = time.monotonic()
            try:
                handle = self._session.say(
                    txt.strip(),
                    allow_interruptions=allow_interruptions,
                    add_to_chat_ctx=False,
                )
            except RuntimeError as e:
                logger.warning(f"Session 已关闭，跳过流式语音播放: {e}")
                return
            speech_handles.append(handle)
            logger.info(
                f"[perf] session.say() 调度: text={txt.strip()[:30]}... "
                f"耗时={(time.monotonic() - t_speak_now)*1000:.0f}ms"
            )

        try:
            t_stream_start = time.monotonic()
            stream = self._llm.chat(chat_ctx=chat_ctx)
            logger.info(f"[perf] LLM chat() 创建: {(time.monotonic() - t_stream_start)*1000:.0f}ms")

            chunk_count = 0
            async for chunk in stream:
                delta = chunk.delta
                if not (delta and delta.content):
                    continue
                chunk_count += 1
                if not first_token_received:
                    first_token_received = True
                    t_first_token = time.monotonic()
                    ttft = t_first_token - t_llm_start
                    logger.info(f"[perf] LLM TTFT(首token延迟): {ttft*1000:.0f}ms")

                out = parser.feed(delta.content)
                if not out:
                    continue
                sentence_buf += out
                # 尽早切分并播报
                while True:
                    sentence, sentence_buf = self._split_at_sentence(sentence_buf)
                    if sentence is None:
                        break
                    if not first_sentence_sent:
                        t_first_sentence = time.monotonic()
                        latency = t_first_sentence - t0
                        logger.info(
                            f"[perf] 首句延迟: {latency*1000:.0f}ms "
                            f"(TTFT={((t_first_token or t0) - t_llm_start)*1000:.0f}ms + "
                            f"句子累积={(t_first_sentence - (t_first_token or t0))*1000:.0f}ms)"
                        )
                        first_sentence_sent = True
                    _speak_now(sentence)

            # 刷新解析器
            remaining = parser.flush()
            if remaining:
                sentence_buf += remaining

            # 把剩余文本也播报
            if sentence_buf.strip():
                _speak_now(sentence_buf)

            t_llm_done = time.monotonic()
            logger.info(
                f"[perf] LLM 流式完成: 总耗时={(t_llm_done - t_llm_start)*1000:.0f}ms, "
                f"chunks={chunk_count}, sentences={len(speech_handles)}"
            )

        except Exception as e:
            logger.error(f"LLM 调用异常: {e}", exc_info=True)
            return LLMResponse(text="抱歉，我这边有点问题，请您稍后再试。")

        # 等待所有播报完成
        t_playout_start = time.monotonic()
        for h in speech_handles:
            try:
                await h.wait_for_playout()
            except Exception as e:
                logger.warning(f"等待播报完成时出错: {e}")
        t_playout_done = time.monotonic()
        logger.info(f"[perf] 播放完成: 耗时={(t_playout_done - t_playout_start)*1000:.0f}ms")

        result = parser.get_result()
        if not result.text:
            # 兜底：parser 没提取到文本但 TTS 可能已经播了一些
            all_spoken = []
            for h in speech_handles:
                txt = getattr(h, 'text', '') or ''
                if txt:
                    all_spoken.append(txt)
            if all_spoken:
                result.text = ' '.join(all_spoken)

        total_e2e = time.monotonic() - t0
        logger.info(
            f"[perf] 端到端: {total_e2e*1000:.0f}ms | "
            f"LLM 响应: intent={result.intent.value}, action={result.action.value}, "
            f"text={result.text[:80]}..."
        )

        # 填充延迟指标供 CallRecordDetail 使用
        if t_first_token is not None:
            result.llm_latency_ms = int((t_first_token - t_llm_start) * 1000)
        if t_first_sentence is not None and t_first_token is not None:
            # TTS 延迟 = 首句发出时间 - 首 token 时间（包含句子累积 + TTS 合成）
            result.tts_latency_ms = int((t_first_sentence - t_first_token) * 1000)

        # 检查是否被用户打断：如果有 speech handle 被中断
        for h in speech_handles:
            if getattr(h, 'interrupted', False):
                result.was_interrupted = True
                break

        return result

    async def _get_llm_response(self) -> LLMResponse:
        """调用 LLM 获取回复（流式，仅收集不播报）

        仅用于无响应追问等不需要流式播报的简单场景。
        """
        chat_ctx = agents.ChatContext()
        if self._system_prompt:
            chat_ctx.add_message(role="system", content=self._system_prompt)
        for msg in self.messages:
            chat_ctx.add_message(role=msg["role"], content=msg["content"])

        parser = StreamingResponseParser()
        full_text_parts: list[str] = []

        try:
            stream = self._llm.chat(chat_ctx=chat_ctx)
            async for chunk in stream:
                delta = chunk.delta
                if delta and delta.content:
                    out = parser.feed(delta.content)
                    if out:
                        full_text_parts.append(out)
            remaining = parser.flush()
            if remaining:
                full_text_parts.append(remaining)
        except Exception as e:
            logger.error(f"LLM 调用异常: {e}", exc_info=True)
            return LLMResponse(text="抱歉，我这边有点问题，请您稍后再试。")

        result = parser.get_result()
        if not result.text and full_text_parts:
            result.text = " ".join(full_text_parts).strip()

        logger.info(
            f"LLM 响应: intent={result.intent.value}, action={result.action.value}, "
            f"text={result.text[:80]}..."
        )
        return result

    # ------------------------------------------------------------------
    # TTS / 播报
    # ------------------------------------------------------------------

    async def _speak(self, text: str, allow_interruptions: bool = True):
        """播报文本（TTS）"""
        if not text or not text.strip():
            return
        if self._session is None:
            logger.warning("Session 未初始化，无法播报")
            return

        try:
            handle = self._session.say(
                text,
                allow_interruptions=allow_interruptions,
                add_to_chat_ctx=False,  # 我们自己管理对话历史
            )
        except RuntimeError as e:
            logger.warning(f"Session 已关闭，跳过语音播放: {e}")
            return

        # 等待播报完成
        try:
            await handle.wait_for_playout()
        except Exception as e:
            logger.warning(f"等待播报完成时出错: {e}")

    # ------------------------------------------------------------------
    # SIP 参与者检测
    # ------------------------------------------------------------------

    async def _wait_for_sip_participant(self) -> rtc.RemoteParticipant:
        """等待 SIP 参与者加入 Room"""
        # 先检查是否已有 SIP 参与者
        for participant in self.ctx.room.remote_participants.values():
            if "sip" in participant.identity.lower() or participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                return participant

        # 等待新参与者加入
        future: asyncio.Future[rtc.RemoteParticipant] = asyncio.get_event_loop().create_future()

        def on_participant_connected(participant: rtc.RemoteParticipant):
            if not future.done():
                future.set_result(participant)

        def on_disconnected():
            if not future.done():
                future.set_exception(RuntimeError("Room disconnected before SIP participant joined"))

        self.ctx.room.on("participant_connected", on_participant_connected)
        self.ctx.room.on("disconnected", on_disconnected)
        try:
            return await future
        finally:
            self.ctx.room.off("participant_connected", on_participant_connected)
            self.ctx.room.off("disconnected", on_disconnected)

    async def _wait_for_audio_track(self, participant: rtc.RemoteParticipant) -> None:
        """等待 SIP 参与者的音频轨道发布（接通确认）

        SIP 通话中，音频轨道只有在对方真正接听后才会发布。
        振铃阶段参与者可能已加入 Room 但没有音频轨道。
        """
        # 先检查是否已有音频轨道
        for pub in participant.track_publications.values():
            if pub.kind == rtc.TrackKind.KIND_AUDIO:
                return

        # 等待音频轨道发布
        future: asyncio.Future[None] = asyncio.get_event_loop().create_future()

        def on_track_published(publication: rtc.RemoteTrackPublication, pub_participant: rtc.RemoteParticipant):
            if pub_participant.identity == participant.identity and publication.kind == rtc.TrackKind.KIND_AUDIO:
                if not future.done():
                    future.set_result(None)

        def on_disconnected():
            if not future.done():
                future.set_exception(RuntimeError("Room disconnected while waiting for audio track"))

        self.ctx.room.on("track_published", on_track_published)
        self.ctx.room.on("disconnected", on_disconnected)
        try:
            await future
        finally:
            self.ctx.room.off("track_published", on_track_published)
            self.ctx.room.off("disconnected", on_disconnected)

    async def _update_cdr_connected(self):
        """后台更新 CDR 为已接通状态"""
        try:
            await call_record_service.update_record(
                self.call_id,
                CallRecordUpdate(status=CallState.CONNECTED, answered_at=self._answered_at),
            )
            logger.info(f"CDR 已更新为 CONNECTED: call_id={self.call_id}")
        except Exception as e:
            logger.warning(f"更新 CDR 接通状态失败（不影响通话）: {e}")

    async def _start_egress_background(self):
        """后台启动 Egress 录制（不阻塞开场白播报）"""
        try:
            await minio_service.ensure_bucket()
            self._egress_service = EgressService()
            await self._egress_service.initialize()
            self._egress_id = await self._egress_service.start_room_composite_egress(
                room_name=self.call_id, call_id=self.call_id,
            )
            await call_record_service.update_record(
                self.call_id,
                CallRecordUpdate(egress_id=self._egress_id),
            )
            logger.info(f"Egress 录制已启动（后台）: egress_id={self._egress_id}")
        except Exception as e:
            logger.warning(f"启动 Egress 录制失败（不影响通话）: {e}")
            self._egress_id = None

    # ------------------------------------------------------------------
    # Egress 录制管理
    # ------------------------------------------------------------------

    async def _stop_recording(self):
        """停止 Egress 录制，等待完成，创建文件记录并更新 CDR"""
        if not self._egress_id or not self._egress_service:
            logger.info("无活跃 Egress 任务，跳过录制停止")
            return

        recording_file_id = None
        recording_url = None

        try:
            # 1. 尝试停止 Egress（如果已自动完成则忽略错误，继续后续处理）
            try:
                await self._egress_service.stop_egress(self._egress_id)
                logger.info(f"Egress 已停止: egress_id={self._egress_id}")
            except Exception as stop_err:
                # 房间删除后 Egress 会自动完成录制，此时调用 stop 会返回
                # "cannot be stopped" 错误，属于正常情况，不应中断后续文件处理
                logger.info(
                    f"停止 Egress 时收到响应（可能已自动完成）: "
                    f"egress_id={self._egress_id}, resp={stop_err}"
                )

            # 2. 等待 Egress 完成文件写入
            egress_info = await self._egress_service.wait_for_egress_complete(
                self._egress_id, timeout=30,
            )
            logger.info(f"Egress 完成信息: egress_id={self._egress_id}, raw_status={egress_info.get('status')}, file_results_count={len(egress_info.get('file_results', []))}")

            # 3. 检查 Egress 状态 — ABORTED / FAILED 则跳过文件创建
            status = egress_info.get("status")
            # EgressStatus 枚举值: 0=STARTING, 1=ACTIVE, 2=ENDING, 3=COMPLETE,
            #                       4=FAILED, 5=ABORTED, 6=LIMIT_REACHED
            # MessageToDict 将 protobuf 枚举转换为字符串名称（如 "EGRESS_COMPLETE"），
            # 因此统一按字符串比较
            _ABORTED_STATUS_NAMES = frozenset({"EGRESS_ABORTED", "EGRESS_FAILED"})
            status_str = status if isinstance(status, str) else str(status)
            if status_str in _ABORTED_STATUS_NAMES:
                error_msg = egress_info.get("error", "")
                logger.warning(
                    f"Egress 录制异常终止: egress_id={self._egress_id}, "
                    f"status={status}, error={error_msg}，跳过文件记录创建"
                )
                # 跳过后续文件创建，直接进入 CDR 更新
            else:
                # 4. 从 egress info 中提取输出文件信息
                file_results = egress_info.get("file_results", [])
                if file_results:
                    fr = file_results[0]
                    storage_path = fr.get("filename", "")
                    file_size = fr.get("size", 0)
                    duration_sec = fr.get("duration", 0.0)

                    # 5. 检查文件是否有效（size=0 或 duration=0 说明录制为空）
                    if file_size == 0 or duration_sec == 0:
                        logger.warning(
                            f"Egress 录制结果为空: egress_id={self._egress_id}, "
                            f"size={file_size}, duration={duration_sec}，跳过文件记录创建"
                        )
                    else:
                        # 6. 确保 MinIO bucket 存在后创建 lk_files 记录
                        await minio_service.ensure_bucket()
                        recording_file_id = str(uuid.uuid4())
                        file_name = storage_path.split("/")[-1] if storage_path else f"full_{self.call_id}.ogg"
                        file_record = FileRecordCreate(
                            file_id=recording_file_id,
                            call_id=self.call_id,
                            file_type="full_recording",
                            storage_path=storage_path,
                            storage_bucket=settings.minio_bucket,
                            file_name=file_name,
                            mime_type="audio/ogg",
                            file_size_bytes=file_size or 0,
                            duration_sec=duration_sec or 0.0,
                            egress_id=self._egress_id,
                        )
                        await create_file_record(file_record)
                        logger.info(f"录音文件记录已创建: file_id={recording_file_id}, path={storage_path}")

                        # 7. 生成预签名 URL
                        try:
                            recording_url = await minio_service.get_presigned_url(
                                bucket=settings.minio_bucket,
                                object_name=storage_path,
                                expires=3600,
                            )
                        except Exception as e:
                            logger.warning(f"生成录音预签名 URL 失败: {e}")
                else:
                    logger.warning(f"Egress 完成但无文件结果: egress_id={self._egress_id}")

        except BaseException as e:
            # 捕获 BaseException（含 CancelledError）而非仅 Exception，
            # 因为 LiveKit Agents SDK 在 room 断开后会取消 entrypoint，
            # 导致 CancelledError 在 await 调用中抛出。
            # 作为清理方法，必须确保清理逻辑不被中断。
            if isinstance(e, asyncio.CancelledError):
                logger.info(f"_stop_recording 被 CancelledError 中断，尝试继续完成清理: egress_id={self._egress_id}")
            else:
                logger.warning(f"停止 Egress 录制时出错（不影响通话结束）: {e}")
        finally:
            # 关闭 Egress 服务客户端
            if self._egress_service:
                try:
                    await self._egress_service.close()
                except Exception:
                    pass
                self._egress_service = None

        # 8. 更新 CDR 中的录音信息
        try:
            update = CallRecordUpdate()
            if recording_file_id:
                update.recording_file_id = recording_file_id
            if recording_url:
                update.recording_url = recording_url
            if update.recording_file_id or update.recording_url:
                await call_record_service.update_record(self.call_id, update)
                logger.info(f"CDR 录音信息已更新: call_id={self.call_id}")
        except Exception as e:
            logger.warning(f"更新 CDR 录音信息失败: {e}")

    # ------------------------------------------------------------------
    # CDR 管理
    # ------------------------------------------------------------------

    async def _finalize_cdr(self):
        """保存最终 CDR"""
        try:
            current = await call_record_service.get_record(self.call_id)
            if not current:
                return

            stats = self.dialog_mgr.get_stats() if self.dialog_mgr else {}
            transcript_entries = [
                TranscriptEntry(
                    role=entry["role"],
                    text=entry["text"],
                    timestamp=entry["timestamp"],
                    duration_sec=entry.get("duration_sec", 0),
                )
                for entry in self.transcript
            ]

            update = CallRecordUpdate(
                rounds=stats.get("rounds", 0),
                user_talk_time_sec=stats.get("user_talk_time_sec", 0),
                ai_talk_time_sec=stats.get("ai_talk_time_sec", 0),
                transcript=transcript_entries,
                ended_at=datetime.now(),
            )

            # 计算通话总时长
            if self._answered_at:
                total_duration = (datetime.now() - self._answered_at).total_seconds()
                update.total_duration_sec = round(total_duration, 1)

            # 只在尚未设置终态结果时才标记为 completed
            current_result = current.get("result", "")
            if not current_result or current_result == "":
                update.status = CallState.ENDED
                update.result = CallResult.COMPLETED

            await call_record_service.update_record(self.call_id, update)
            logger.info(f"最终 CDR 已保存: call_id={self.call_id}")
        except Exception as e:
            logger.error(f"保存最终 CDR 失败: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # SIP 挂断
    # ------------------------------------------------------------------

    async def _hangup_sip_call(self):
        """挂断 SIP 通话：通过 LiveKit API 删除 Room，触发 SIP BYE 信令。

        Agent 进程无法访问 API 服务的 SipService 实例，
        因此在此处直接创建 LiveKit API 客户端来删除 Room。
        删除 Room 后，所有参与者（包括 SIP 参与者）都会被断开，
        LiveKit SIP 网关会向对端发送 SIP BYE，Zoiper 端通话结束。
        """
        try:
            api_url = settings.livekit_url.replace("ws://", "http://").replace("wss://", "https://")
            lk_api = livekit_api.LiveKitAPI(
                url=api_url,
                api_key=settings.livekit_api_key,
                api_secret=settings.livekit_api_secret,
            )
            try:
                await lk_api.room.delete_room(
                    livekit_api.DeleteRoomRequest(room=self.call_id)
                )
                logger.info(f"SIP 通话已挂断（Room 已删除）: call_id={self.call_id}")
            finally:
                await lk_api.aclose()
        except Exception as e:
            # Room 可能已被删除（如用户先挂断），忽略此类错误
            logger.warning(f"挂断 SIP 通话时出错（可能 Room 已不存在）: {e}")

    # ------------------------------------------------------------------
    # 默认话术
    # ------------------------------------------------------------------

    @staticmethod
    def _default_script() -> dict:
        """默认话术配置（当数据库中找不到话术时使用）"""
        return {
            "name": "默认话术",
            "description": "系统默认话术",
            "script_type": "general",
            "opening_text": "您好，我是智能客服助手。",
            "opening_pause_ms": 2000,
            "main_prompt": (
                "你是一个专业的电话客服助手，请根据用户的回答进行自然的对话。"
            ),
            "closing_text": "感谢您的接听，再见！",
            "barge_in_opening": False,
            "barge_in_conversation": True,
            "barge_in_closing": False,
            "barge_in_protect_start_sec": 1.0,
            "barge_in_protect_end_sec": 1.0,
            "tolerance_enabled": True,
            "tolerance_ms": 500,
            "no_response_timeout_sec": 5,
            "no_response_mode": "consecutive",
            "no_response_max_count": 3,
            "no_response_prompt": "您好，请问您还在吗？",
            "no_response_hangup_text": "感谢您的时间，再见！",
        }
