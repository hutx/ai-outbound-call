"""外呼语音 Agent - 基于 LiveKit AgentSession"""
import json
import logging
import asyncio
import time
from datetime import datetime
from typing import Optional

from livekit import agents, rtc
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

_MAX_CONVERSATION_ROUNDS = 50


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
                script.get("main_prompt", "你是一个智能外呼助手。"),
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
            self._session.on(agents.UserInputTranscribedEvent, self._on_user_input)

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
            await self._conversation_loop(script)

            # 15. 播放结束语
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

    async def _conversation_loop(self, script: dict):
        """多轮对话主循环"""
        for round_num in range(_MAX_CONVERSATION_ROUNDS):
            logger.info(f"--- 第 {round_num + 1} 轮对话 ---")

            # A. 等待用户说话（带超时）
            user_text = await self._wait_for_user_input()

            if user_text is None:
                # 无响应处理
                result = self.dialog_mgr.on_no_response()
                logger.info(
                    f"无响应: consecutive={result['consecutive_count']}, "
                    f"cumulative={result['cumulative_count']}, hangup={result['should_hangup']}"
                )
                await self._speak(result["prompt_text"])
                if result["should_hangup"]:
                    break
                continue

            # B. 宽容期处理
            if self.dialog_mgr.tolerance.enabled:
                user_text = await self.dialog_mgr.wait_with_tolerance(
                    user_text,
                    listen_func=self._listen_additional,
                )

            # C. 记录用户输入
            self.dialog_mgr.on_user_responded(user_text)
            self.messages.append({"role": "user", "content": user_text})
            self.transcript.append({
                "role": "user",
                "text": user_text,
                "timestamp": time.time(),
                "duration_sec": 0,
            })
            logger.info(f"用户: {user_text}")

            # D. LLM 推理
            llm_response = await self._get_llm_response()

            # E. 播报 AI 回复
            if llm_response.text:
                logger.info(f"AI: {llm_response.text[:100]}...")
                self.dialog_mgr.on_ai_speaking_start()
                await self._speak(
                    llm_response.text,
                    allow_interruptions=self.dialog_mgr.barge_in.enabled,
                )
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
                logger.info("LLM 决策: 结束通话")
                break
            elif llm_response.action == CallAction.TRANSFER:
                logger.info("LLM 决策: 转接人工")
                # TODO: 实现转接逻辑
                break
            elif llm_response.action == CallAction.CALLBACK:
                logger.info("LLM 决策: 预约回拨")
                # TODO: 实现回拨逻辑
                break
            elif llm_response.action == CallAction.BLACKLIST:
                logger.info("LLM 决策: 加入黑名单")
                # TODO: 实现黑名单逻辑
                break

    # ------------------------------------------------------------------
    # 用户输入监听
    # ------------------------------------------------------------------

    def _on_user_input(self, evt: agents.UserInputTranscribedEvent):
        """处理用户语音转录事件"""
        if not evt.is_final:
            return
        text = (evt.transcript or "").strip()
        if not text:
            return
        logger.debug(f"STT 转录: {text}")
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
    # LLM 调用
    # ------------------------------------------------------------------

    async def _get_llm_response(self) -> LLMResponse:
        """调用 LLM 获取回复（流式）"""
        # 构建 ChatContext
        chat_ctx = agents.ChatContext()
        # 系统提示
        if self._system_prompt:
            chat_ctx.add_message(role="system", content=self._system_prompt)
        # 对话历史
        for msg in self.messages:
            chat_ctx.add_message(role=msg["role"], content=msg["content"])

        parser = StreamingResponseParser()
        full_text_parts: list[str] = []

        try:
            stream = self._llm.chat(chat_ctx=chat_ctx)
            async for chunk in stream:
                delta = chunk.delta
                if delta and delta.content:
                    token = delta.content
                    out = parser.feed(token)
                    if out:
                        full_text_parts.append(out)

            # 刷新缓冲区
            remaining = parser.flush()
            if remaining:
                full_text_parts.append(remaining)

        except Exception as e:
            logger.error(f"LLM 调用异常: {e}", exc_info=True)
            return LLMResponse(text="抱歉，我这边有点问题，请您稍后再试。")

        result = parser.get_result()
        # 如果流式解析器没有提取到文本，使用完整拼接文本
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
                "你是一个智能外呼助手。请用自然、礼貌的中文与用户对话。"
                "每次回复后，在 <decision>{\"intent\": \"...\", \"action\": \"...\"}</decision> "
                "中标注用户意向和你的决策动作。"
            ),
            "closing_text": "感谢您的接听，再见！",
            "barge_in_opening": False,
            "barge_in_conversation": True,
            "barge_in_closing": False,
            "barge_in_protect_start_sec": 1.0,
            "barge_in_protect_end_sec": 1.0,
            "tolerance_enabled": True,
            "tolerance_ms": 1000,
            "no_response_timeout_sec": 5,
            "no_response_mode": "consecutive",
            "no_response_max_count": 3,
            "no_response_prompt": "您好，请问您还在吗？",
            "no_response_hangup_text": "感谢您的时间，再见！",
        }
