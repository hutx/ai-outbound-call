"""
mod_audio_fork WebSocket 接收服务
───────────────────────────────────────
FreeSWITCH 通过 mod_audio_fork 将实时音频流推送到此 WebSocket Server。
mod_audio_fork 作为 WebSocket 客户端主动连接后端，直接发送二进制音频帧。
如果 ESL 命令中传入了 metadata 参数，FreeSWITCH 会先发送一个 JSON 文本帧。

设计目标：
  - 低延迟：20ms 帧大小，直接入队
  - 高可靠：连接断开自动清理，queue 满时丢弃最老帧
  - 可观测：连接状态、帧计数、丢帧率
"""
import asyncio
import logging
import websockets
from typing import Optional

logger = logging.getLogger(__name__)


class AudioStreamWebSocket:
    """
    接收 mod_audio_fork 推送的音频帧

    工作流程：
      1. FreeSWITCH 拨号计划执行 audio_fork 指令，或 ESL 执行 uuid_audio_fork API
      2. mod_audio_fork 连接此 WebSocket 服务器
      3. 如果传入了 metadata 参数，首帧为 JSON 元数据帧（含 uuid/channel_uuid）
      4. 后续每 20ms 推送一帧 PCM（8000Hz 16bit mono，L16 线性编码）
      5. PCM 帧直接送入对应通话的 audio_queue

    与 ESLSocketCallSession 的集成：
      - 通过 get_session_queue(uuid) 获取对应通话的音频队列
      - ESLSocketCallSession.start_audio_capture() 优先使用此队列
      - 若 WebSocket 无对应 session，降级到文件轮询
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8765,
                 max_queue_size: int = 500):
        self.host = host
        self.port = port
        self.max_queue_size = max_queue_size

        # uuid → asyncio.Queue[bytes] 映射
        self._sessions: dict[str, asyncio.Queue] = {}
        # 连接的 UUID → session UUID 映射（用于断连清理）
        self._ws_to_uuid: dict[int, str] = {}
        # 单一全局队列（单通话场景最简单）
        self._global_queue: Optional[asyncio.Queue] = None
        self._global_uuid: Optional[str] = None
        # 全局队列的活跃连接计数（防止一个断连清空其他人的队列）
        self._global_conn_count: int = 0
        # 统计信息
        self._stats: dict = {
            "connections_total": 0,
            "connections_active": 0,
            "frames_received": 0,
            "frames_dropped": 0,
            "sessions_cleaned": 0,
        }

        self._server = None
        self._running = False

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    async def start(self):
        """启动 WebSocket 服务器"""
        self._running = True
        self._server = await websockets.serve(
            self._handle_connection_simple,
            self.host,
            self.port,
            max_size=64 * 1024,  # 单帧最大 64KB
            ping_interval=30,
            ping_timeout=10,
        )
        addr = self._server.sockets[0].getsockname()
        logger.info(f"AudioStream WebSocket Server 监听 {addr[0]}:{addr[1]}")

    async def stop(self):
        """关闭 WebSocket 服务器"""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # 清理所有 session
        self._sessions.clear()
        self._ws_to_uuid.clear()
        # 清理单一队列
        self._global_queue = None
        self._global_uuid = None
        self._global_conn_count = 0
        logger.info("AudioStream WebSocket Server 已关闭")

    async def get_session_queue(self, call_uuid: str, timeout: float = 5.0) -> Optional[asyncio.Queue]:
        """
        获取指定通话的音频队列

        参数：
            call_uuid: FreeSWITCH channel UUID
            timeout: 等待队列就绪的最大秒数

        返回：
            asyncio.Queue 对象，或 None（超时或不可用）
        """
        # 优先检查全局队列（单通话场景，无需匹配 UUID）
        if self._global_queue is not None:
            if self._global_uuid != call_uuid:
                logger.debug(f"[{call_uuid}] 全局队列 UUID 不匹配: {self._global_uuid}，但仍返回队列")
            return self._global_queue

        if call_uuid in self._sessions:
            return self._sessions[call_uuid]

        # 等待 mod_audio_fork 连接建立
        logger.debug(f"[{call_uuid}] 等待 audio_fork WebSocket 连接...")
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if call_uuid in self._sessions:
                logger.info(f"[{call_uuid}] audio_fork WebSocket 连接已就绪")
                return self._sessions[call_uuid]
            if self._global_queue is not None:
                logger.info(f"[{call_uuid}] audio_fork 全局队列已就绪")
                return self._global_queue
            await asyncio.sleep(0.1)

        logger.warning(f"[{call_uuid}] audio_fork WebSocket 连接超时 ({timeout}s)")
        return None

    def register_session(self, call_uuid: str, queue: asyncio.Queue):
        """预先注册会话（用于 UUID 通过 URL 传递的场景）"""
        self._sessions[call_uuid] = queue
        self._global_queue = queue
        self._global_uuid = call_uuid
        logger.info(f"[{call_uuid}] audio_fork 会话已预注册（全局队列）")

    async def _handle_connection_simple(self, websocket):
        """极简处理器：从 URL 路径提取 UUID，注册到 _sessions"""
        ws_id = id(websocket)
        self._stats["connections_total"] += 1
        self._stats["connections_active"] += 1

        # 从 URL 路径提取 UUID（如 /d5d13469-... 或 /ws/d5d13469-...）
        call_uuid = None
        try:
            path = getattr(websocket, 'path', '/') or '/'
            # 去除查询参数
            path = path.split('?')[0]
            # 去除前导 /，可能是 UUID 或 ws/UUID
            path_parts = path.strip("/").split("/")
            for part in path_parts:
                if len(part) >= 30:  # UUID 长度
                    call_uuid = part
                    break
        except Exception:
            pass

        # 创建或使用全局队列
        if self._global_queue is None:
            self._global_queue = asyncio.Queue(maxsize=self.max_queue_size)
            self._global_conn_count = 0
            logger.info(f"[ws#{ws_id}] 创建全局音频队列")
        self._global_conn_count += 1
        queue = self._global_queue

        # 注册 UUID → 队列映射
        if call_uuid:
            self._sessions[call_uuid] = queue
            self._ws_to_uuid[ws_id] = call_uuid
            self._global_queue = queue
            self._global_uuid = call_uuid
            logger.info(f"[ws#{ws_id}] URL 路径 UUID: {call_uuid[:8]}... (全局连接数: {self._global_conn_count})")

        # 🎵 调试：原始音频转储
        import os
        import time as _time
        dump_dir = "/recordings/debug"
        os.makedirs(dump_dir, exist_ok=True)
        dump_path = os.path.join(dump_dir, f"ws_raw_{ws_id}_{int(_time.time())}.pcm")
        dump_file = open(dump_path, "wb")
        dump_bytes = 0

        logger.info(f"[ws#{ws_id}] 连接已建立，等待音频帧... (全局连接数: {self._global_conn_count})")

        frame_count = 0
        try:
            async for frame in websocket:
                if isinstance(frame, str):
                    # mod_audio_fork 可选发送 JSON metadata 帧（如果 ESL 命令中传入了 metadata 参数）
                    # 否则直接发送二进制音频帧，没有首帧
                    try:
                        data = json.loads(frame)
                        call_uuid = data.get("uuid", data.get("channel_uuid", data.get("callId", "")))
                        if call_uuid and call_uuid not in self._sessions:
                            self._sessions[call_uuid] = queue
                            self._ws_to_uuid[ws_id] = call_uuid
                            self._global_uuid = call_uuid
                        logger.info(f"[ws#{ws_id}] mod_audio_fork metadata: {list(data.keys())}")
                    except json.JSONDecodeError:
                        # 可能是纯文本 UUID（兼容旧格式）
                        extracted = self._extract_uuid(frame)
                        if extracted:
                            call_uuid = extracted
                            self._sessions[call_uuid] = queue
                            self._ws_to_uuid[ws_id] = call_uuid
                            self._global_uuid = call_uuid
                    continue

                frame_count += 1
                self._stats["frames_received"] += 1
                dump_bytes += len(frame)

                # 检测 stereo 模式（640 bytes = 20ms × 2声道 × 2字节 @ 8000Hz）
                left_samples = []
                right_samples = []
                audio_to_queue = frame
                if len(frame) >= 640 and len(frame) % 4 == 0:
                    import struct
                    sample_count = len(frame) // 4  # 每声道样本数
                    # 左声道：offset 0, 4, 8, ...
                    left_samples = []
                    right_samples = []
                    for i in range(sample_count):
                        left_samples.append(struct.unpack_from('<h', frame, i * 4)[0])
                        right_samples.append(struct.unpack_from('<h', frame, i * 4 + 2)[0])
                    # 尝试使用左声道（用户语音）送入 ASR
                    audio_to_queue = struct.pack(f'<{sample_count}h', *left_samples)
                    dump_file.write(frame)  # 保存原始 stereo 帧
                    # 额外保存两个单声道文件用于验证
                    if frame_count <= 100:
                        left_path = os.path.join(dump_dir, f"ws_left_{ws_id}.pcm")
                        right_path = os.path.join(dump_dir, f"ws_right_{ws_id}.pcm")
                        with open(left_path, "ab") as lf:
                            lf.write(struct.pack(f'<{sample_count}h', *left_samples))
                        with open(right_path, "ab") as rf:
                            rf.write(struct.pack(f'<{sample_count}h', *right_samples))
                else:
                    dump_file.write(frame)

                if frame_count <= 3:
                    inferred_rate = "unknown"
                    if len(frame) >= 600:
                        inferred_rate = f"~{len(frame) / 0.02 / 2:.0f} Hz (帧 {len(frame)} bytes)"
                    else:
                        inferred_rate = f"~{len(frame) / 0.02 / 2:.0f} Hz (帧 {len(frame)} bytes)"
                    mode = "stereo→mono(L)" if len(frame) >= 640 else "mono"
                    # 计算两个声道的 RMS 用于调试
                    left_rms = (sum(x*x for x in left_samples) / len(left_samples)) ** 0.5 if left_samples else 0
                    right_rms = (sum(x*x for x in right_samples) / len(right_samples)) ** 0.5 if right_samples else 0
                    logger.info(f"[ws#{ws_id}] 收到音频帧 #{frame_count}: {len(frame)} bytes [{mode}], 推断采样率: {inferred_rate}, 左RMS={left_rms:.1f}, 右RMS={right_rms:.1f}")

                # 非阻塞入队，满时丢弃最老
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self._stats["frames_dropped"] += 1

                try:
                    queue.put_nowait(audio_to_queue)
                except asyncio.QueueFull:
                    self._stats["frames_dropped"] += 1

                if frame_count % 100 == 0:
                    logger.info(f"[ws#{ws_id}] 已接收 {frame_count} 帧, {dump_bytes} bytes")

        except websockets.exceptions.ConnectionClosed:
            logger.debug(f"[ws#{ws_id}] 连接已关闭")
        except Exception as e:
            logger.error(f"[ws#{ws_id}] 连接异常: {e}")
        finally:
            dump_file.close()
            # 将 PCM 转 WAV 方便播放分析
            wav_path = dump_path.replace(".pcm", ".wav")
            try:
                import wave as _wave
                with _wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(8000)
                    wf.writeframes(open(dump_path, "rb").read())
                logger.info(
                    f"[ws#{ws_id}] 音频已保存: {wav_path} "
                    f"({frame_count} 帧, {dump_bytes} bytes, "
                    f"{dump_bytes / 16000:.1f}s)"
                )
            except Exception as e:
                logger.warning(f"[ws#{ws_id}] WAV 转换失败: {e}")
            try:
                os.remove(dump_path)
            except OSError:
                pass
            self._stats["sessions_cleaned"] += 1
            self._stats["connections_active"] = max(0, self._stats["connections_active"] - 1)
            self._global_conn_count -= 1
            if self._global_conn_count <= 0:
                self._global_queue = None
                self._global_uuid = None
                self._global_conn_count = 0
                logger.info(f"[ws#{ws_id}] 最后一个连接断开，清空全局队列")
            # 清理 UUID 会话映射
            if call_uuid:
                self._sessions.pop(call_uuid, None)
                self._ws_to_uuid.pop(ws_id, None)
            logger.info(f"[ws#{ws_id}] 连接已断开，共接收 {frame_count} 帧 (剩余连接: {self._global_conn_count})")

    @staticmethod
    def _extract_uuid(message) -> Optional[str]:
        """
        从首帧消息中提取 Channel UUID

        mod_audio_fork 不同 fork 的首帧格式不同：
          1. 纯文本 UUID：直接返回
          2. JSON 格式：解析 JSON 中的 channel_uuid / uuid / call_id 字段
          3. 其他：尝试匹配 UUID 格式字符串
        """
        import re
        import json

        # 情况 1：纯文本 UUID
        if isinstance(message, str):
            message = message.strip()
            # UUID 格式匹配
            uuid_match = re.match(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                message,
                re.IGNORECASE,
            )
            if uuid_match:
                return message

            # JSON 格式尝试解析
            if message.startswith("{"):
                try:
                    data = json.loads(message)
                    for key in ("channel_uuid", "uuid", "call_id", "channel_uuid_raw"):
                        if key in data and data[key]:
                            return data[key]
                except json.JSONDecodeError:
                    pass

            # 尝试从文本中提取 UUID
            uuid_search = re.search(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                message,
                re.IGNORECASE,
            )
            if uuid_search:
                return uuid_search.group()

            # 可能是通道名称格式，如 "sofia/internal/xxx" 等
            # 返回完整消息由上层处理
            if len(message) > 5:
                logger.debug(f"首帧非标准 UUID 格式: {message[:100]}")
                return message

        elif isinstance(message, bytes):
            # 二进制帧：尝试解码为文本
            try:
                text = message.decode("utf-8", errors="replace").strip()
                return AudioStreamWebSocket._extract_uuid(text)
            except Exception:
                pass

        return None
