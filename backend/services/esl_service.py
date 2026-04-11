"""
FreeSWITCH ESL 服务 — 生产级实现
─────────────────────────────────
Inbound ESL  : 后端主动连 FS:8021，下发 originate 命令
Outbound ESL : FS 接通后主动连后端:9999，每路通话独立 session
连接池        : AsyncESLPool — 复用 Inbound 连接，避免每次外呼都新建 TCP
"""
import asyncio
import base64
import logging
import os
import re
import tempfile
import time
import uuid
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

INTERNAL_EXTENSION_RE = re.compile(r"^\d{3,6}$")


class ESLError(Exception):
    pass


# ─────────────────────────────────────────────────────────────
# 底层 ESL 连接（Inbound 模式）
# ─────────────────────────────────────────────────────────────
class AsyncESLConnection:
    """单条 Inbound ESL 连接"""

    def __init__(self, host: str, port: int, password: str, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._lock = asyncio.Lock()
        self._last_used = 0.0

    @property
    def is_connected(self) -> bool:
        return (
            self._connected
            and self._writer is not None
            and not self._writer.is_closing()
        )

    async def connect(self):
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.timeout,
        )
        event = await asyncio.wait_for(self._read_event(), timeout=self.timeout)
        if event.get("Content-Type") != "auth/request":
            raise ESLError(f"期望 auth/request，收到: {event}")
        await self._send(f"auth {self.password}\n\n")
        reply = await asyncio.wait_for(self._read_event(), timeout=self.timeout)
        if "+OK" not in reply.get("Reply-Text", ""):
            raise ESLError(f"ESL 认证失败: {reply}")
        self._connected = True
        self._last_used = time.time()
        logger.debug(f"ESL Inbound 连接成功: {self.host}:{self.port}")

    async def api(self, command: str) -> str:
        if not self.is_connected:
            raise ESLError("ESL 未连接")
        async with self._lock:
            await self._send(f"api {command}\n\n")
            event = await asyncio.wait_for(self._read_event(), timeout=30.0)
            self._last_used = time.time()
            return event.get("_body", "")

    async def bgapi(self, command: str, job_uuid: Optional[str] = None) -> str:
        if not self.is_connected:
            raise ESLError("ESL 未连接")
        job_uuid = job_uuid or uuid.uuid4().hex
        async with self._lock:
            await self._send(f"bgapi {command}\nJob-UUID: {job_uuid}\n\n")
            event = await asyncio.wait_for(self._read_event(), timeout=10.0)
            self._last_used = time.time()
        reply = event.get("Reply-Text", "")
        if "+OK" in reply:
            return job_uuid
        raise ESLError(f"bgapi 失败: {reply}")

    async def originate(
        self,
        phone: str,
        gateway: str,
        call_uuid: str,
        task_id: str,
        script_id: str,
        caller_id: str,
        originate_timeout: int = 30,
        socket_host: str = "127.0.0.1",
        socket_port: int = 9999,
        internal_domain: str = "$${local_ip_v4}",
    ) -> str:
        """
        发起外呼。所有业务参数作为 channel 变量传入，
        CallAgent 通过 ESL channel_vars 读取。

        流程：
          1. bgapi originate → B-leg 振铃后 &park()
          2. 监听 CHANNEL_ANSWER 事件
          3. uuid_audio_stream 启动 WebSocket 音频流
          4. uuid_broadcast socket 连接后端:9999（ESL Outbound 模式）
        """
        endpoint, target_type, _ = self._build_originate_target(
            phone=phone,
            gateway=gateway,
            internal_domain=internal_domain,
        )

        channel_vars = (
            f"origination_uuid={call_uuid},"
            f"ai_agent=true,"
            f"export:task_id={task_id},"
            f"export:script_id={script_id},"
            f"export:call_target_type={target_type},"
            f"export:original_destination={phone},"
            f"continue_on_fail=USER_BUSY,NO_ANSWER,TIMEOUT,NO_ROUTE_DESTINATION,"
            f"origination_caller_id_number={caller_id},"
            f"originate_timeout={originate_timeout},"
        )

        cmd = (
            f"originate [{channel_vars}]"
            f"{endpoint} &park()"
        )
        logger.info(
            f"ESL originate → {phone} "
            f"(uuid={call_uuid[:8]}, task={task_id}, target={target_type})"
        )
        result = await self.bgapi(cmd, job_uuid=call_uuid)

        # B-leg 应答后启动 AI 流程
        asyncio.create_task(
            self._on_call_answered(call_uuid, phone, task_id, script_id, target_type)
        )

        return result

    async def _on_call_answered(
        self, call_uuid: str, phone: str, task_id: str, script_id: str, target_type: str
    ):
        """
        B-leg 应答后：
        1. 启动 uuid_audio_stream WebSocket 音频流
        2. 广播 socket 应用连接后端 ESL Outbound 服务
        通过轮询通道状态来检测应答（避免 ESL event 订阅时序问题）。
        """
        ws_url = f"ws://backend:8765/{call_uuid}"
        socket_addr = "backend:9999 async full"

        try:
            # 先等待 originate 完成，通道被创建
            await asyncio.sleep(2)

            # 轮询等待通道出现（originate 需要时间建立 B-leg）
            channel_exists = False
            for attempt in range(60):  # 最多等 60 秒
                try:
                    result = await self.api(f"uuid_dump {call_uuid}")
                    if result and "-ERR No such channel" not in result:
                        channel_exists = True
                        logger.info(f"[{call_uuid[:8]}] B-leg 已建立，启动 AI 流程")
                        break
                    elif result and "-ERR No such channel" in result:
                        await asyncio.sleep(1)
                        continue
                except Exception:
                    await asyncio.sleep(1)

            if not channel_exists:
                logger.warning(f"[{call_uuid[:8]}] 外呼等待通道建立超时")
                return

            # 1. 启动 mod_audio_stream WebSocket 音频流
            try:
                result = await self.api(
                    f"uuid_audio_stream {call_uuid} start {ws_url} mono 8000"
                )
                if result.strip().startswith("+OK"):
                    logger.info(f"[{call_uuid[:8]}] uuid_audio_stream 启动成功: {ws_url}")
                else:
                    logger.warning(f"[{call_uuid[:8]}] uuid_audio_stream 返回: {result.strip()[:200]}")
            except Exception as e:
                logger.warning(f"[{call_uuid[:8]}] uuid_audio_stream 失败: {e}，降级文件轮询")

            # 2. 广播 socket 应用连接后端 ESL Outbound 服务
            try:
                result = await self.api(
                    f"uuid_broadcast {call_uuid} {socket_addr} both"
                )
                logger.info(f"[{call_uuid[:8]}] uuid_broadcast socket 结果: {result.strip()[:100]}")
            except Exception as e:
                logger.error(f"[{call_uuid[:8]}] uuid_broadcast socket 失败: {e}")

        except Exception as e:
            logger.error(f"[{call_uuid[:8]}] _on_call_answered 异常: {e}", exc_info=True)

    @staticmethod
    def _build_originate_target(phone: str, gateway: str, internal_domain: str) -> tuple[str, str, str]:
        normalized = (phone or "").strip()
        if INTERNAL_EXTENSION_RE.fullmatch(normalized):
            domain = (internal_domain or "$${local_ip_v4}").strip()
            logger.debug(f"ESL originate → {phone} (internal_extension) → {normalized}@{domain}")
            # 对于AI代理呼叫，使用拨号计划扩展而不是直接连接用户
            # 这样可以确保AI处理逻辑被执行
            return f"user/{normalized}@{domain}", "internal_extension", normalized

        dest = f"97776{normalized}"
        return f"sofia/gateway/{gateway}/{dest}", "pstn", dest

    async def close(self):
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=2.0)
            except Exception:
                pass

    async def _send(self, data: str):
        self._writer.write(data.encode())
        await self._writer.drain()

    async def _read_event(self) -> dict:
        headers: dict = {}
        while True:
            try:
                line = await self._reader.readline()
            except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
                self._connected = False
                raise ESLError("ESL 连接断开")
            if not line:
                self._connected = False
                raise ESLError("ESL 连接断开（EOF）")
            line = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                break
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip()] = v.strip()
        content_length = int(headers.get("Content-Length", 0))
        if content_length > 0:
            body = await self._reader.readexactly(content_length)
            headers["_body"] = body.decode("utf-8", errors="replace")
        return headers


# ─────────────────────────────────────────────────────────────
# ESL 连接池（生产级，自动保活/重连）
# ─────────────────────────────────────────────────────────────
class AsyncESLPool:
    """
    ESL Inbound 连接池
    - 固定 pool_size 条连接，初始化时建立
    - 信号量控制并发，避免锁争抢
    - 后台 keepalive 循环：idle 超时发 status ping，断开自动重建
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        pool_size: int = 5,
        idle_timeout: float = 60.0,
    ):
        self._host = host
        self._port = port
        self._password = password
        self._pool_size = pool_size
        self._idle_timeout = idle_timeout
        self._conns: list[AsyncESLConnection] = []
        self._sem = asyncio.Semaphore(pool_size)
        self._pool_lock = asyncio.Lock()
        self._keepalive_task: Optional[asyncio.Task] = None

    async def start(self):
        for _ in range(self._pool_size):
            conn = AsyncESLConnection(self._host, self._port, self._password)
            try:
                await asyncio.wait_for(conn.connect(), timeout=5.0)
            except Exception as e:
                logger.warning(f"ESL 池初始化失败（将在首次使用时重试）: {e}")
            self._conns.append(conn)
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        alive = sum(1 for c in self._conns if c.is_connected)
        logger.info(f"ESL 连接池就绪: {alive}/{self._pool_size} 条连接")

    async def stop(self):
        if self._keepalive_task:
            self._keepalive_task.cancel()
            await asyncio.gather(self._keepalive_task, return_exceptions=True)
        for conn in self._conns:
            await conn.close()
        self._conns.clear()
        logger.info("ESL 连接池已关闭")

    async def originate(self, **kwargs) -> str:
        async with self._sem:
            conn = await self._acquire()
            job_uuid = await conn.originate(**kwargs)
            return job_uuid

    async def wait_for_event(self, event_name: str, filter_key: str, filter_value: str, timeout: float = 30.0) -> Optional[dict]:
        async with self._sem:
            conn = await self._acquire()
            return await conn.wait_for_event(event_name, filter_key, filter_value, timeout)

    async def api(self, command: str) -> str:
        async with self._sem:
            conn = await self._acquire()
            return await conn.api(command)

    async def _acquire(self) -> AsyncESLConnection:
        """获取一条健康的连接，不健康则尝试重建"""
        async with self._pool_lock:
            # 优先找空闲且健康的连接
            for conn in self._conns:
                if conn.is_connected:
                    return conn
            # 全部断开，重建第一条
            if not self._conns:
                self._conns.append(
                    AsyncESLConnection(self._host, self._port, self._password)
                )
            conn = self._conns[0]
            try:
                await asyncio.wait_for(conn.connect(), timeout=5.0)
                return conn
            except Exception as e:
                raise ESLError(f"ESL 池无可用连接，重连也失败: {e}")

    async def _keepalive_loop(self):
        while True:
            await asyncio.sleep(30)
            for conn in self._conns:
                try:
                    if not conn.is_connected:
                        await asyncio.wait_for(conn.connect(), timeout=5.0)
                        logger.info("ESL 连接已重建")
                    elif time.time() - conn._last_used > self._idle_timeout:
                        await asyncio.wait_for(conn.api("status"), timeout=3.0)
                except Exception as e:
                    logger.warning(f"ESL keepalive 失败: {e}")
                    conn._connected = False


# ─────────────────────────────────────────────────────────────
# ESL Socket Session（Outbound — 每路通话一个实例）
# ─────────────────────────────────────────────────────────────
class ESLSocketCallSession:
    """
    FreeSWITCH 通话接通后主动连接后端的 Session
    提供对单路通话的完整控制能力
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 esl_pool=None, ws_server=None):
        self._reader = reader
        self._writer = writer
        self._uuid: Optional[str] = None
        self._channel_vars: dict = {}
        self._lock = asyncio.Lock()
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._connected = True
        self._playback_done = asyncio.Event()
        self._hangup_cause: Optional[str] = None
        self._sip_code: Optional[int] = None
        self.esl_pool = esl_pool  # ESL Inbound pool for uuid_eavesdrop
        self.ws_server = ws_server  # AudioStreamWebSocket for mod_audio_stream
        self._audio_mode: str = "unknown"  # "websocket" | "file_poll" | "custom"
        self._audio_started: bool = False  # 防止重复启动 uuid_audio_stream
        self._active_uuid: Optional[str] = None  # CHANNEL_ANSWER 中捕获的实际通话 UUID

    @property
    def uuid(self) -> Optional[str]:
        return self._uuid

    @property
    def channel_vars(self) -> dict:
        """所有 FreeSWITCH channel 变量（variable_xxx 去掉前缀后）"""
        return self._channel_vars

    async def connect(self) -> dict:
        """完成握手，返回全部 channel 数据。可安全重复调用（幂等）。"""
        # 如果已经握手完成，直接返回缓存的 channel 变量
        if self._uuid is not None:
            return self._channel_vars

        await self._send("connect\n\n")
        data = await self._read_event()

        # 提取 UUID
        self._uuid = (
            data.get("Unique-ID")
            or data.get("Channel-Unique-ID")
            or data.get("variable_origination_uuid")
        )
        if not self._uuid:
            raise ESLError(f"握手中无法提取 UUID，headers: {list(data.keys())[:15]}")

        # 提取 channel 变量（variable_xxx → xxx）
        for k, v in data.items():
            if k.startswith("variable_"):
                self._channel_vars[k[9:]] = v

        logger.info(
            f"ESL Outbound 握手 uuid={self._uuid} "
            f"task={self._channel_vars.get('task_id','?')} "
            f"script={self._channel_vars.get('script_id','?')}"
        )

        # 订阅此通话所有事件
        await self._send("myevents\n\n")
        await self._read_event()
        await self._send("divert_events on\n\n")
        return data

    # ── 通话控制 ──────────────────────────────────────────────

    async def execute(self, app: str, arg: str = "", lock: bool = True) -> str:
        if not self._connected:
            raise ESLError("通话已结束")
        event_uuid = uuid.uuid4().hex
        msg = (
            f"sendmsg {self._uuid}\n"
            f"call-command: execute\n"
            f"execute-app-name: {app}\n"
        )
        if arg:
            msg += f"execute-app-arg: {arg}\n"
        # Outbound socket 模式下 event-lock 会阻止 CHANNEL_EXECUTE_COMPLETE 返回
        # playback 和 start_dtmf 必须设为 false，否则无法收到完成事件
        if lock and app not in ("playback", "start_dtmf", "set"):
            msg += "event-lock: true\n"
        msg += f"Event-UUID: {event_uuid}\n\n"
        async with self._lock:
            await self._send(msg)
        return event_uuid

    async def play(self, audio_path: str, timeout: float = 60.0):
        """播放音频：通过 ESL Inbound pool 的 uuid_broadcast API 播放

        sendmsg execute playback 在 Outbound socket 模式下有编解码/媒体路径问题，
        改用 uuid_broadcast 更可靠。
        """
        import os
        if not self.esl_pool:
            # 降级：使用 sendmsg execute
            logger.warning(f"[{self._uuid}] ESL pool 不可用，降级 sendmsg execute playback")
            self._playback_done.clear()
            await self.execute("playback", audio_path)
            try:
                await asyncio.wait_for(self._playback_done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"[{self._uuid}] 播放超时 ({timeout}s)")
                await self.stop_playback()
            return

        # 计算播放时长（用于等待）
        try:
            file_size = os.path.getsize(audio_path)
            # 8000Hz 16bit mono WAV: 44 header + 16000 bytes/sec
            audio_bytes = max(0, file_size - 44)
            estimated_duration = audio_bytes / 16000.0 if audio_bytes > 0 else 1.0
        except Exception:
            estimated_duration = 3.0

        target_uuid = self._active_uuid or self._uuid
        try:
            result = await self.esl_pool.api(
                f"uuid_broadcast {target_uuid} {audio_path} both"
            )
            logger.info(f"[{self._uuid}] uuid_broadcast({target_uuid}) 结果: {result.strip()[:100]}")
        except Exception as e:
            logger.error(f"[{self._uuid}] uuid_broadcast 失败: {e}，降级 sendmsg")
            self._playback_done.clear()
            await self.execute("playback", audio_path)
            try:
                await asyncio.wait_for(self._playback_done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"[{self._uuid}] 播放超时 ({timeout}s)")
                await self.stop_playback()
            return

        # 等待播放完成
        await asyncio.sleep(estimated_duration)

    async def play_stream(self, audio_chunks, text: str = "", timeout: float = 60.0):
        """
        兼容流式 TTS：先收集 PCM，再写临时 WAV 文件播放。
        这不是严格的实时流式，但能保证真实 FreeSWITCH 路径可运行。
        """
        pcm_parts = []
        total_bytes = 0
        async for chunk in audio_chunks:
            if chunk:
                pcm_parts.append(chunk)
                total_bytes += len(chunk)

        logger.info(f"[{self._uuid}] TTS 流式收集完成: {len(pcm_parts)} 块, {total_bytes} 字节")

        if not pcm_parts:
            logger.warning(f"[{self._uuid}] TTS 流式播放收到空音频")
            return

        from backend.utils.audio import write_wav

        # 写入两个容器共享的 /recordings 目录，FreeSWITCH 才能访问
        shared_dir = os.environ.get("FS_RECORDING_PATH", "/recordings")
        os.makedirs(shared_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f"tts_{self._uuid or 'call'}_",
            suffix=".wav",
            dir=shared_dir,
        )
        os.close(fd)

        total_pcm = len(b"".join(pcm_parts))
        logger.info(f"[{self._uuid}] 写入 WAV: {temp_path} ({total_pcm} bytes PCM)")

        try:
            write_wav(temp_path, b"".join(pcm_parts), sample_rate=8000)
            file_size = os.path.getsize(temp_path)
            logger.info(f"[{self._uuid}] WAV 文件大小: {file_size} bytes, 开始播放")
            await self.play(temp_path, timeout=timeout)
            logger.info(f"[{self._uuid}] 播放完成")
        except Exception as e:
            logger.error(f"[{self._uuid}] 播放异常: {e}", exc_info=True)
        finally:
            try:
                if os.path.exists(temp_path):
                    logger.debug(f"[{self._uuid}] 删除临时文件: {temp_path}")
                    os.remove(temp_path)
            except OSError:
                pass

    async def stop_playback(self):
        """打断当前播放（barge-in）"""
        try:
            await self.execute("break", "", lock=False)
        except Exception:
            pass
        self._playback_done.set()

    async def set_variable(self, name: str, value: str):
        await self.execute("set", f"{name}={value}", lock=False)

    async def transfer_to_human(self, extension: str = "8001"):
        await self.execute("transfer", f"{extension} XML agents")
        self._connected = False

    async def hangup(self, cause: str = "NORMAL_CLEARING"):
        if not self._connected:
            return
        try:
            await self._send(
                f"sendmsg {self._uuid}\n"
                f"call-command: hangup\n"
                f"hangup-cause: {cause}\n\n"
            )
        except Exception:
            pass
        self._connected = False

    async def start_recording(self, record_path: Optional[str] = None) -> str:
        """通过 uuid_record 启动会话录音，供 ASR 消费。"""
        path = record_path or f"/recordings/asr_{self._uuid}.raw"
        if not self.esl_pool:
            return ""
        try:
            result = await self.esl_pool.api(f"uuid_record {self._uuid} start {path}")
            logger.info(f"[{self._uuid}] uuid_record: {result.strip()[:100]}")
            return path
        except Exception as e:
            logger.error(f"[{self._uuid}] 录音启动失败: {e}")
            return ""

    async def start_audio_capture(self) -> asyncio.Queue:
        """开始捕获通话音频，返回 PCM 数据队列

        优先通过 ESL API 的 uuid_audio_stream 启动实时音频流（mod_audio_stream），
        降级为文件轮询（uuid_record 录音）。
        """
        if not self._uuid:
            logger.warning("无法启动音频捕获: UUID 为空")
            self._audio_mode = "none"
            return asyncio.Queue()

        # 幂等：如果已经启动成功，直接返回音频队列
        if self._audio_started and self._audio_mode != "none":
            logger.debug(f"[{self._uuid}] 音频采集已启动，模式={self._audio_mode}，跳过重复启动")
            return self._audio_queue

        # 方案 1：通过 ESL API 启动 mod_audio_stream 实时音频流
        if self.esl_pool and self.ws_server:
            try:
                target_uuid = self._active_uuid or self._uuid
                ws_url = f"ws://backend:8765/{target_uuid}"
                result = await self.esl_pool.api(
                    f"uuid_audio_stream {target_uuid} start {ws_url} mono 8000"
                )
                if result.strip().startswith("+OK"):
                    ws_queue = await self.ws_server.get_session_queue(target_uuid, timeout=5.0)
                    if ws_queue is not None:
                        logger.info(
                            f"[{self._uuid}] 音频采集: mod_audio_stream WebSocket (ESL API), "
                            f"target={target_uuid}"
                        )
                        self._audio_mode = "websocket"
                        self._audio_started = True
                        return ws_queue
                    logger.warning(f"[{self._uuid}] WebSocket 会话未建立，降级文件轮询")
                else:
                    logger.warning(f"[{self._uuid}] uuid_audio_stream 未返回 +OK: {result.strip()[:200]}")
            except Exception as e:
                logger.warning(f"[{self._uuid}] uuid_audio_stream 失败: {e}")

        # 方案 2：降级 — 文件轮询（用 uuid_record 启动录音）
        asr_path = f"/recordings/asr_{self._uuid}.raw"
        if self.esl_pool:
            try:
                result = await self.esl_pool.api(f"uuid_record {self._uuid} start {asr_path}")
                logger.info(f"[{self._uuid}] uuid_record 降级: {result.strip()[:100]}")
            except Exception as e:
                logger.warning(f"[{self._uuid}] uuid_record 失败: {e}")

        self._audio_mode = "file_poll"
        self._audio_started = True
        logger.info(f"[{self._uuid}] 音频采集: 文件轮询 {asr_path}")
        asyncio.create_task(self._poll_audio_file(asr_path))
        return self._audio_queue

    async def _poll_audio_file(self, path: str):
        """轮询音频文件，读取新增数据送入音频队列"""
        import os
        import aiofiles

        # 等待文件创建（FreeSWITCH record_session 创建文件）
        for _ in range(60):  # 最多等 6 秒
            if os.path.exists(path):
                break
            await asyncio.sleep(0.1)
        else:
            logger.warning(f"[{self._uuid}] 音频文件未创建: {path}")
            return

        offset = 0
        while self._connected:
            try:
                current_size = os.path.getsize(path)
                if current_size > offset:
                    async with aiofiles.open(path, "rb") as f:
                        await f.seek(offset)
                        chunk = await f.read(current_size - offset)
                        if chunk:
                            offset += len(chunk)
                            await self._safe_put(self._audio_queue, chunk)
                await asyncio.sleep(0.05)  # 50ms 轮询间隔
            except FileNotFoundError:
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"[{self._uuid}] 音频文件读取失败: {e}")
                await asyncio.sleep(0.1)

    # ── 事件主循环 ────────────────────────────────────────────

    async def read_events(self):
        """持续读取并分发 ESL 事件（由 CallAgent 以 create_task 启动）"""
        while self._connected:
            try:
                event = await asyncio.wait_for(self._read_event(), timeout=45.0)
            except asyncio.TimeoutError:
                try:
                    await self._send("\n\n")
                except Exception:
                    self._connected = False
                    break
                continue
            except (ESLError, Exception) as e:
                logger.error(f"[{self._uuid}] 事件读取中断: {e}")
                self._connected = False
                break

            name = event.get("Event-Name", "")

            if name in ("CHANNEL_HANGUP", "CHANNEL_HANGUP_COMPLETE"):
                cause = event.get("Hangup-Cause", "UNKNOWN")
                sip_code = event.get("sip_hangup_disposition", "")
                # 存储到 session 属性，供 CallAgent._cleanup 读取
                self._hangup_cause = cause
                if "recv_" in sip_code:
                    # sip_hangup_disposition 格式如 "recv_refused" / "recv_403" 等
                    self._sip_code = sip_code
                logger.info(f"[{self._uuid}] 通话挂断: {cause} (sip_disposition={sip_code})")
                self._connected = False
                await self._safe_put(self._event_queue, {"type": "hangup", "cause": cause})
                break

            elif name == "CHANNEL_ANSWER":
                self._active_uuid = event.get("Unique-ID", self._uuid)
                logger.info(f"[{self._uuid}] Channel answered, active_uuid={self._active_uuid}")

            elif name == "CHANNEL_EXECUTE_COMPLETE":
                app = event.get("Application", "")
                if app == "playback":
                    self._playback_done.set()
                await self._safe_put(self._event_queue, {"type": "execute_complete", "app": app})

            elif name == "DTMF":
                digit = event.get("DTMF-Digit", "")
                logger.debug(f"[{self._uuid}] DTMF: {digit}")
                await self._safe_put(self._event_queue, {"type": "dtmf", "digit": digit})

            elif name == "CUSTOM":
                body = event.get("_body", "")
                event_subclass = event.get("Event-Subclass", "")
                if body:
                    try:
                        pcm = base64.b64decode(body)
                        logger.debug(f"[{self._uuid}] CUSTOM audio: {len(pcm)} bytes, subclass={event_subclass}")
                        await self._safe_put(self._audio_queue, pcm)
                    except Exception:
                        logger.debug(f"[{self._uuid}] CUSTOM event decode failed: subclass={event_subclass}, body_len={len(body)}")

        # 确保所有等待者能退出
        self._playback_done.set()
        await self._safe_put(self._event_queue, {"type": "hangup", "cause": "SESSION_END"})

    async def wait_for_hangup(self, timeout: float = 3600.0) -> str:
        """等待通话挂断，返回 hangup cause"""
        event = await self.wait_for_event("hangup", timeout=timeout)
        return event.get("cause", "UNKNOWN") if event else "TIMEOUT"

    async def wait_for_event(self, event_type: str, timeout: float = 30.0) -> Optional[dict]:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(), timeout=min(remaining, 1.0)
                )
                if event.get("type") == event_type:
                    return event
                if event.get("type") == "hangup":
                    return None
            except asyncio.TimeoutError:
                pass
        return None

    @staticmethod
    async def _safe_put(queue: asyncio.Queue, item):
        """non-blocking put，队满时丢弃最老的"""
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    # ── I/O 底层 ─────────────────────────────────────────────

    async def _send(self, data: str):
        self._writer.write(data.encode("utf-8"))
        await self._writer.drain()

    async def _read_event(self) -> dict:
        headers: dict = {}
        while True:
            try:
                line = await self._reader.readline()
            except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
                raise ESLError("连接断开")
            if not line:
                raise ESLError("连接断开（EOF）")
            line = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                break
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip()] = v.strip()
        cl = int(headers.get("Content-Length", 0))
        if cl > 0:
            body = await self._reader.readexactly(cl)
            headers["_body"] = body.decode("utf-8", errors="replace")
        return headers


# ─────────────────────────────────────────────────────────────
# ESL Socket Server
# ─────────────────────────────────────────────────────────────
class ESLSocketServer:
    def __init__(
        self,
        host: str,
        port: int,
        call_handler: Callable[[ESLSocketCallSession], Awaitable[None]],
        max_connections: int = 200,
        esl_pool=None,
        ws_server=None,
    ):
        self.host = host
        self.port = port
        self.call_handler = call_handler
        self._sem = asyncio.Semaphore(max_connections)
        self._server: Optional[asyncio.Server] = None
        self._esl_pool = esl_pool
        self._ws_server = ws_server

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle,
            self.host,
            self.port,
            reuse_address=True,
            backlog=256,
        )
        addr = self._server.sockets[0].getsockname()
        logger.info(f"ESL Socket Server 监听 {addr[0]}:{addr[1]}")
        async with self._server:
            await self._server.serve_forever()

    async def _handle(self, reader, writer):
        async with self._sem:
            session = ESLSocketCallSession(reader, writer, esl_pool=self._esl_pool,
                                           ws_server=self._ws_server)
            try:
                await self.call_handler(session)
            except Exception as e:
                logger.error(f"通话处理异常: {e}", exc_info=True)
            finally:
                try:
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
                except Exception:
                    pass
