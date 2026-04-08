"""
测试通话路由
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from websockets.exceptions import ConnectionClosed

from backend.api.test_call_support import MockESLCallSession, run_test_call_with_agent
from backend.core.auth import require_auth
from backend.core.call_agent import CallAgent
from backend.core.state_machine import CallContext, CallIntent, CallResult, CallState

logger = logging.getLogger(__name__)


class TestCallRequest(BaseModel):
    phone_number: str = "test_001"
    script_id: str = "default"
    customer_info: dict = {}


class TTSRequest(BaseModel):
    text: str = Field(default="您好，这是AI智能外呼系统的语音测试")


class TestCallAPI:
    def __init__(
        self,
        *,
        config,
        active_calls: dict[str, CallAgent],
        metrics: dict[str, int],
        broadcast_stats,
        asr_getter: Callable[[], object],
        tts_getter: Callable[[], object],
        llm_getter: Callable[[], object],
    ):
        self._config = config
        self._active_calls = active_calls
        self._metrics = metrics
        self._broadcast_stats = broadcast_stats
        self._asr_getter = asr_getter
        self._tts_getter = tts_getter
        self._llm_getter = llm_getter
        self.router = APIRouter()
        self._register_routes()

    def _register_routes(self):
        @self.router.post("/api/test-call/start", dependencies=[Depends(require_auth)])
        async def start_test_call(req: TestCallRequest):
            call_uuid = str(uuid.uuid4())
            ctx = CallContext(
                uuid=call_uuid,
                task_id="test_task_" + call_uuid[:8],
                phone_number=req.phone_number,
                script_id=req.script_id,
                state=CallState.DIALING,
                intent=CallIntent.UNKNOWN,
                result=CallResult.NOT_ANSWERED,
                created_at=datetime.now(),
                answered_at=None,
                ended_at=None,
                user_utterances=0,
                ai_utterances=0,
                recording_path=None,
                messages=[],
                customer_info=req.customer_info,
            )

            session = MockESLCallSession(
                uuid=call_uuid,
                channel_vars={
                    "destination_number": req.phone_number,
                    "Caller-Destination-Number": req.phone_number,
                    "context": "test_context",
                    "task_id": ctx.task_id,
                    "script_id": req.script_id,
                },
                asr_client=self._asr_getter(),
                tts_client=self._tts_getter(),
            )

            agent = CallAgent(
                session=session,
                context=ctx,
                asr=self._asr_getter(),
                tts=self._tts_getter(),
                llm=self._llm_getter(),
            )

            self._active_calls[call_uuid] = agent
            self._metrics["calls_total"] += 1
            self._metrics["calls_active"] += 1
            await self._broadcast_stats()

            asyncio.create_task(
                run_test_call_with_agent(
                    agent,
                    call_uuid,
                    self._active_calls,
                    self._metrics,
                    self._broadcast_stats,
                )
            )

            logger.info(f"启动测试通话 {call_uuid} -> {req.phone_number}")
            return {
                "call_id": call_uuid,
                "phone_number": req.phone_number,
                "message": "测试通话已启动",
            }

        @self.router.websocket("/ws/test-call/{call_id}")
        async def test_call_ws(websocket: WebSocket, call_id: str):
            token = websocket.query_params.get("token", "")
            if self._config.api_token.strip() and token != self._config.api_token.strip():
                await websocket.close(code=4001, reason="Unauthorized")
                return

            await websocket.accept()
            logger.info(f"测试通话WebSocket连接: {call_id}")

            agent = None
            waited_time = 0.0
            while waited_time < 10:
                agent = self._active_calls.get(call_id)
                if agent:
                    break
                await asyncio.sleep(0.5)
                waited_time += 0.5

            if not agent:
                logger.warning(f"测试通话WebSocket连接失败: 通话不存在或超时 - {call_id}")
                await websocket.close(code=4004, reason="通话不存在或尚未初始化")
                return

            agent.session.ws_connection = websocket
            agent.session._ws_ready.set()

            try:
                await websocket.send_json(
                    {"type": "call_status", "status": "connected", "message": "通话连接成功"}
                )

                while True:
                    try:
                        data = await asyncio.wait_for(websocket.receive(), timeout=1.0)
                        if data["type"] != "websocket.receive":
                            continue

                        if "text" in data:
                            msg = json.loads(data["text"])
                            if msg.get("type") == "audio_end":
                                logger.info(
                                    f"[{call_id}] 收到 audio_end，触发 ASR 结算 "
                                    f"(queue_size={agent.session._audio_queue.qsize()})"
                                )
                                await agent.session._audio_queue.put(b"")
                            elif msg.get("type") == "playback_finished":
                                logger.info(f"[{call_id}] 收到 playback_finished，结束当前播报等待")
                                notify_done = getattr(agent.session, "notify_playback_finished", None)
                                if notify_done:
                                    notify_done()
                            elif msg.get("type") == "hangup":
                                logger.info(f"[{call_id}] 收到 hangup 请求")
                                await agent.session.hangup()
                                break
                        elif "bytes" in data:
                            audio_data = data["bytes"]
                            logger.info(
                                f"[{call_id}] 收到音频 {len(audio_data)} bytes, "
                                f"queue_size={agent.session._audio_queue.qsize()}"
                            )
                            await agent.session._audio_queue.put(audio_data)
                    except asyncio.TimeoutError:
                        if call_id not in self._active_calls:
                            logger.info(f"通话已结束，WebSocket连接关闭: {call_id}")
                            break
                        continue
                    except (WebSocketDisconnect, ConnectionClosed):
                        logger.info(f"测试通话WebSocket断开: {call_id}")
                        break
                    except RuntimeError as e:
                        if "disconnect" in str(e).lower():
                            logger.info(f"测试通话WebSocket已断开: {call_id}")
                        else:
                            logger.error(f"测试通话WebSocket错误: {e}")
                        break
                    except Exception as e:
                        logger.error(f"测试通话WebSocket接收数据错误: {e}")
                        break

            except (WebSocketDisconnect, ConnectionClosed):
                logger.info(f"测试通话WebSocket正常断开: {call_id}")
            except RuntimeError as e:
                if "disconnect" not in str(e).lower():
                    logger.error(f"测试通话WebSocket异常: {e}")
            except Exception as e:
                logger.error(f"测试通话WebSocket异常: {e}")
            finally:
                if agent and hasattr(agent.session, "ws_connection"):
                    agent.session.ws_connection = None
                try:
                    await websocket.close()
                except Exception:
                    pass

        @self.router.post("/api/test-call/{call_id}/hangup", dependencies=[Depends(require_auth)])
        async def hangup_test_call(call_id: str):
            agent = self._active_calls.get(call_id)
            if not agent:
                raise HTTPException(404, "通话不存在")

            try:
                await agent.session.hangup()
                return {"message": f"通话 {call_id} 已挂断"}
            except Exception as e:
                logger.error(f"挂断通话 {call_id} 失败: {e}")
                raise HTTPException(500, f"挂断失败: {e}")

        @self.router.get("/api/test-call/{call_id}/messages", dependencies=[Depends(require_auth)])
        async def get_test_call_messages(call_id: str):
            agent = self._active_calls.get(call_id)
            if not agent:
                raise HTTPException(404, "通话不存在")

            return {
                "call_id": call_id,
                "messages": agent.ctx.messages,
                "state": agent.ctx.state.name,
                "intent": agent.ctx.intent.value,
                "result": agent.ctx.result.value,
            }

        @self.router.post("/api/tts/synthesize")
        async def synthesize_tts(req: TTSRequest):
            if not req.text.strip():
                raise HTTPException(400, "文本为空")

            wav_path = await self._tts_getter().synthesize(req.text)
            if not wav_path or not os.path.exists(wav_path):
                raise HTTPException(500, "TTS 合成失败")

            return FileResponse(
                wav_path,
                media_type="audio/wav",
                filename="tts_output.wav",
            )
