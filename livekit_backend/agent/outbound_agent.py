"""外呼语音 Agent - 基于 LiveKit AgentSession"""
import json
import logging
import asyncio
import re
import time
from datetime import datetime
from typing import Optional

from livekit import agents, api as livekit_api, rtc
from livekit.agents import Agent, AgentSession, AutoSubscribe
from livekit.agents.voice.turn import TurnHandlingOptions
from livekit.plugins import silero

from livekit_backend.core.config import settings
from livekit_backend.core.events import CallState, CallAction, CallIntent, CallResult
from livekit_backend.plugins.aliyun_stt import AliyunSTT
from livekit_backend.plugins.aliyun_tts import AliyunTTS
from livekit_backend.plugins.qwen_llm import (
    create_qwen_llm,
    build_system_prompt,
    parse_llm_response,
    StreamingResponseParser,
    LLMResponse,
)
from livekit_backend.services.script_service import script_service
from livekit_backend.services import call_record_service
from livekit_backend.models.call_record import CallRecordCreate, CallRecordUpdate, TranscriptEntry
from livekit_backend.agent.dialog_manager import DialogManager
from livekit_backend.utils import db

logger = logging.getLogger(__name__)

_MAX_CONVERSATION_ROUNDS = 6


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

    async def run(self):
        """主运行函数"""
        logger.info(
            f"Agent 启动: call_id={self.call_id}, script_id={self.script_id}, "
            f"task_id={self.task_id}, phone={self.phone}"
        )

        try:
            # 1. 初始化数据库连接
            await db.get_pool()

            # 2. 创建 CDR 记录
            await call_record_service.create_record(
                CallRecordCreate(
                    call_id=self.call_id,
                    task_id=self.task_id,
                    phone=self.phone,
                    script_id=self.script_id,
                )
            )
            await call_record_service.update_record(
                self.call_id,
                CallRecordUpdate(status=CallState.CONNECTED),
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
            vad = silero.VAD.load()

            # 7. 创建 Agent（手动回合模式，由我们控制对话循环）
            self._agent = Agent(
                instructions=self._system_prompt,
                stt=stt,
                llm=self._llm,
                tts=tts_engine,
                vad=vad,
                turn_handling=TurnHandlingOptions(turn_detection="manual"),
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

            # 11. 等待 SIP 参与者加入
            try:
                participant = await asyncio.wait_for(
                    self._wait_for_sip_participant(),
                    timeout=settings.originate_timeout_sec,
                )
                logger.info(f"SIP 参与者已加入: {participant.identity}")
            except asyncio.TimeoutError:
                logger.warning("等待 SIP 参与者超时")
                await call_record_service.update_record(
                    self.call_id,
                    CallRecordUpdate(status=CallState.ENDED, result=CallResult.TIMEOUT),
                )
                return

            # 12. 启动 AgentSession
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

    async def _conversation_loop(self, script: dict) -> str:
        """多轮对话主循环

        Returns:
            结束原因字符串:
            - "no_response_hangup": 连续无响应达到阈值，hangup_text 已播报完毕
            - "intent_end": LLM 决策结束（closing_text 将在循环外播报）
            - "max_rounds": 达到最大轮次
        """
        for round_num in range(_MAX_CONVERSATION_ROUNDS):
            logger.info(f"--- 第 {round_num + 1} 轮对话 ---")

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

        # 达到最大轮次
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
            handle = self._session.say(
                txt.strip(),
                allow_interruptions=allow_interruptions,
                add_to_chat_ctx=False,
            )
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

        handle = self._session.say(
            text,
            allow_interruptions=allow_interruptions,
            add_to_chat_ctx=False,  # 我们自己管理对话历史
        )
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

        self.ctx.room.on("participant_connected", on_participant_connected)
        try:
            return await future
        finally:
            self.ctx.room.off("participant_connected", on_participant_connected)

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
