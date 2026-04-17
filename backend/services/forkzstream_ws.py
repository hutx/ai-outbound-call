"""
mod_forkzstream WebSocket 接收服务
───────────────────────────────────────
FreeSWITCH 通过 mod_forkzstream 将实时音频流推送到此 WebSocket Server。
mod_forkzstream 作为 WebSocket 客户端主动连接后端，直接发送二进制音频帧。

与 mod_audio_fork 的区别：
  - mod_audio_fork: metadata 帧 + 二进制音频帧
  - mod_forkzstream: JSON 握手帧（含 callId）+ 二进制音频帧（PCM 格式）

设计目标：
  - 低延迟：实时 PCM 帧直接入队，供 ASR 消费
  - 双向：支持后端发送 TTS 二进制帧回 FreeSWITCH
  - 控制：支持发送文本控制命令（ttsstop_clean 等）
  - AI 对话：forkzstream 连接建立后自动启动 CallAgent 处理 ASR→LLM→TTS 完整流程
"""
import asyncio
import json
import logging
import websockets
from typing import Optional

logger = logging.getLogger(__name__)


class ForkzstreamWebSocketServer:
    """接收 mod_forkzstream 推送的实时音频流 + 发送 TTS 音频

    WebSocket 协议：
    - FS 连接后发送 JSON 握手帧（包含 callId、session_id 等）
    - FS → 后端：二进制帧（ASR 音频，PCM 格式 @ 8kHz mono）
    - 后端 → FS：二进制帧（TTS 音频，8kHz L16 PCM）
    - 后端 → FS 文本帧：控制命令（ttsstop_clean、ttsstart 等）

    AI 对话集成：
    - 握手成功后自动创建 ForkzstreamCallSession → 启动 CallAgent.run()
    - CallAgent 通过 session.start_audio_capture() 获取 ASR 音频队列
    - CallAgent 通过 session.play_stream() 发送 TTS 音频帧
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8766,
                 max_queue_size: int = 500,
                 esl_pool=None):
        self.host = host
        self.port = port
        self.max_queue_size = max_queue_size

        # call_uuid → asyncio.Queue[bytes] 映射（ASR 音频队列）
        self._sessions: dict[str, asyncio.Queue] = {}
        # call_uuid → WebSocket 连接映射（用于发送 TTS/控制命令）
        self._ws_connections: dict[str, websockets.WebSocketServerProtocol] = {}
        # call_uuid → 连接就绪事件
        self._ready_events: dict[str, asyncio.Event] = {}
        # call_uuid → script_id 映射（从握手帧的 botid 字段提取）
        self._script_ids: dict[str, str] = {}
        # call_uuid → CallAgent task 映射
        self._call_agent_tasks: dict[str, asyncio.Task] = {}
        # 统计信息
        self._stats: dict = {
            "connections_total": 0,
            "connections_active": 0,
            "frames_received": 0,
            "frames_sent": 0,
            "commands_sent": 0,
        }
        # ESL Inbound pool（用于通话控制 + 获取 channel 变量）
        self._esl_pool = esl_pool

        self._server = None
        self._running = False

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    async def start(self):
        """启动 WebSocket 服务器"""
        self._running = True
        self._server = await websockets.serve(
            self._handle_connection,
            self.host,
            self.port,
            max_size=64 * 1024,
            ping_interval=30,
            ping_timeout=10,
        )
        addr = self._server.sockets[0].getsockname()
        logger.info(f"Forkzstream WebSocket Server 监听 {addr[0]}:{addr[1]}")

    async def stop(self):
        """关闭 WebSocket 服务器"""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # 清理所有状态
        for ws in self._ws_connections.values():
            try:
                await ws.close()
            except Exception:
                pass
        self._sessions.clear()
        self._ws_connections.clear()
        self._ready_events.clear()
        self._script_ids.clear()
        logger.info("Forkzstream WebSocket Server 已关闭")

    async def get_session_queue(self, call_uuid: str, timeout: float = 5.0) -> Optional[asyncio.Queue]:
        """获取指定通话的 ASR 音频队列

        参数：
            call_uuid: FreeSWITCH channel UUID（即 forkzstream 的 callId）
            timeout: 等待连接就绪的最大秒数

        返回：
            asyncio.Queue 对象，或 None（超时或不可用）
        """
        # ★ 先检查连接是否已存在（处理竞态条件：握手可能在 get_session_queue 之前完成）
        queue = self._sessions.get(call_uuid)
        if queue is not None:
            return queue

        # 注册就绪事件
        if call_uuid not in self._ready_events:
            self._ready_events[call_uuid] = asyncio.Event()

        # 等待连接建立
        try:
            await asyncio.wait_for(self._ready_events[call_uuid].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[forkzstream] 等待 {call_uuid[:8]} 连接超时 ({timeout}s)")
            return None

        return self._sessions.get(call_uuid)

    async def get_script_id(self, call_uuid: str, timeout: float = 5.0) -> Optional[str]:
        """获取指定通话的话术 ID（从 forkzstream 握手帧的 botid 字段提取）

        参数：
            call_uuid: FreeSWITCH channel UUID
            timeout: 等待连接就绪的最大秒数

        返回：
            script_id 字符串，或 None（超时或未设置）
        """
        # ★ 先检查是否已存在（处理竞态条件）
        script_id = self._script_ids.get(call_uuid)
        if script_id:
            return script_id

        # 注册就绪事件
        if call_uuid not in self._ready_events:
            self._ready_events[call_uuid] = asyncio.Event()

        # 等待连接建立
        try:
            await asyncio.wait_for(self._ready_events[call_uuid].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

        return self._script_ids.get(call_uuid)

    async def send_audio(self, call_uuid: str, chunk: bytes) -> bool:
        """发送 TTS 音频帧到 FreeSWITCH（二进制帧）

        参数：
            call_uuid: A-leg UUID（forkzstream 连接使用的 callId）
            chunk: PCM 音频数据（8kHz L16 mono）

        返回：
            True 表示发送成功，False 表示连接不可用
        """
        ws = self._ws_connections.get(call_uuid)
        if ws is None:
            logger.debug(f"[forkzstream] 发送 TTS 音频失败: {call_uuid[:8]} 无连接")
            return False
        try:
            await ws.send(chunk)
            self._stats["frames_sent"] += 1
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"[forkzstream] 发送 TTS 音频时连接关闭: {call_uuid[:8]}")
            self._ws_connections.pop(call_uuid, None)
            return False
        except Exception as e:
            logger.warning(f"[forkzstream] 发送 TTS 音频异常: {e}")
            return False

    async def send_command(self, call_uuid: str, cmd: str) -> bool:
        """发送控制命令到 FreeSWITCH（文本帧）

        支持的命令：
            - ttsstop_clean: 清空 TTS 队列 + 停止播放（打断）
            - ttsstop_only: 停止播放不清空
            - ttsstart: 恢复 TTS 播放
            - asrstop / asrstart: 停止/恢复 ASR 发送
            - close: 关闭 WebSocket 连接

        参数：
            call_uuid: A-leg UUID
            cmd: 命令字符串

        返回：
            True 表示发送成功
        """
        ws = self._ws_connections.get(call_uuid)
        if ws is None:
            logger.debug(f"[forkzstream] 发送命令失败: {call_uuid[:8]} 无连接")
            return False
        try:
            await ws.send(cmd)
            self._stats["commands_sent"] += 1
            logger.debug(f"[forkzstream] 发送命令: {cmd} → {call_uuid[:8]}")
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"[forkzstream] 发送命令时连接关闭: {call_uuid[:8]}")
            self._ws_connections.pop(call_uuid, None)
            return False
        except Exception as e:
            logger.warning(f"[forkzstream] 发送命令异常: {e}")
            return False

    async def is_connected(self, call_uuid: str) -> bool:
        """检查指定通话的 forkzstream 连接是否可用"""
        ws = self._ws_connections.get(call_uuid)
        if ws is None:
            return False
        # websockets 库没有直接的 is_open 属性，检查连接是否已关闭
        return ws.open

    async def _start_call_agent(self, call_uuid: str, script_id: str):
        """在 forkzstream 连接上启动 CallAgent。

        流程：
        1. 创建 ForkzstreamCallSession
        2. 创建 CallContext + CallAgent
        3. 运行 CallAgent.run() → 开场白 → ASR → LLM → TTS 循环
        """
        try:
            from backend.services.forkzstream_session import ForkzstreamCallSession
            from backend.core.state_machine import CallContext, CallResult
            from backend.core.call_agent import CallAgent
            from backend.services.asr_service import create_asr_client
            from backend.services.tts_service import create_tts_client
            from backend.services.llm_service import LLMService

            session = ForkzstreamCallSession(
                call_uuid=call_uuid,
                phone="",  # 稍后从 channel_vars 补充
                task_id="",  # 稍后从 channel_vars 补充
                script_id=script_id or "finance_product_a",
                forkzstream_ws_server=self,
                esl_pool=self._esl_pool,
            )

            context = CallContext(
                uuid=call_uuid,
                phone_number="",
                task_id="",
                script_id=script_id or "finance_product_a",
                result=CallResult.NOT_ANSWERED,
            )

            asr = create_asr_client()
            tts = create_tts_client()
            llm = LLMService()

            agent = CallAgent(
                session=session,
                context=context,
                asr=asr,
                tts=tts,
                llm=llm,
                esl_pool=self._esl_pool,
            )

            self._call_agent_tasks[call_uuid] = asyncio.current_task()
            await agent.run()

        except Exception as e:
            logger.error(f"[{call_uuid[:8]}] CallAgent 启动异常: {e}", exc_info=True)
        finally:
            self._call_agent_tasks.pop(call_uuid, None)

    async def _handle_connection(self, websocket):
        """处理 mod_forkzstream 连接"""
        ws_id = id(websocket)
        self._stats["connections_total"] += 1
        self._stats["connections_active"] += 1

        call_uuid = None
        handshake_received = False

        try:
            async for frame in websocket:
                if isinstance(frame, str):
                    # JSON 握手帧或控制命令
                    try:
                        data = json.loads(frame)
                        if not handshake_received:
                            # 首次文本帧：JSON 握手
                            call_uuid = data.get("callId", data.get("call_uuid", ""))
                            if call_uuid:
                                botid = data.get("botid", "")
                                logger.info(
                                    f"[forkzstream#{ws_id}] 握手成功: callId={call_uuid[:8]}... "
                                    f"botid={botid} (keys={list(data.keys())})"
                                )
                                # 注册此连接的队列
                                queue = asyncio.Queue(maxsize=self.max_queue_size)
                                self._sessions[call_uuid] = queue
                                self._ws_connections[call_uuid] = websocket
                                # 存储话术 ID
                                if botid:
                                    self._script_ids[call_uuid] = botid
                                # 通知等待者连接已就绪
                                if call_uuid in self._ready_events:
                                    self._ready_events[call_uuid].set()
                                handshake_received = True

                                # 发送连接成功确认（文本帧）
                                await websocket.send("connect succeed!")
                                # 启用 TTS 和 ASR（forkzstream 默认 is_asr/is_tts 为 false）
                                await websocket.send("ttsstart")
                                await websocket.send("asrstart")
                                logger.info(f"[forkzstream#{ws_id}] 已发送 'connect succeed!' + 'ttsstart' + 'asrstart' 确认")

                                # ★ 启动 CallAgent 处理 AI 对话流程
                                asyncio.create_task(
                                    self._start_call_agent(call_uuid, botid)
                                )
                            else:
                                logger.warning(f"[forkzstream#{ws_id}] 握手帧缺少 callId: {data}")
                        else:
                            # 后续文本帧：控制命令（如 FS 发送的 detect_end 等）
                            logger.debug(f"[forkzstream#{ws_id}] 收到文本帧: {frame[:100]}")
                    except json.JSONDecodeError:
                        logger.debug(f"[forkzstream#{ws_id}] 非 JSON 文本帧: {frame[:100]}")
                    continue

                # 二进制帧：ASR 音频
                if not call_uuid:
                    logger.warning(f"[forkzstream#{ws_id}] 收到二进制帧但尚未握手")
                    continue

                self._stats["frames_received"] += 1

                if self._stats["frames_received"] <= 3:
                    logger.info(
                        f"[forkzstream#{ws_id}] 收到音频帧 #{self._stats['frames_received']}: "
                        f"{len(frame)} bytes"
                    )

                # 入队
                queue = self._sessions.get(call_uuid)
                if queue is not None:
                    if queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        pass

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"[forkzstream#{ws_id}] 连接已关闭")
        except Exception as e:
            logger.error(f"[forkzstream#{ws_id}] 连接异常: {e}")
        finally:
            if call_uuid:
                self._sessions.pop(call_uuid, None)
                self._ws_connections.pop(call_uuid, None)
                self._script_ids.pop(call_uuid, None)
                logger.info(f"[forkzstream#{ws_id}] 清理: callId={call_uuid[:8]}...")

            self._stats["connections_active"] = max(0, self._stats["connections_active"] - 1)
            logger.info(f"[forkzstream#{ws_id}] 连接结束，共接收 {self._stats['frames_received']} 帧")
