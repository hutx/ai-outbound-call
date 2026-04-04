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
import time
import uuid
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)


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
            await asyncio.wait_for(self._read_event(), timeout=10.0)
            self._last_used = time.time()
        return job_uuid

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
    ) -> str:
        """
        发起外呼。所有业务参数作为 channel 变量传入，
        CallAgent 通过 ESL channel_vars 读取。
        """
        cmd = (
            f"originate {{"
            f"origination_uuid={call_uuid},"
            f"task_id={task_id},"
            f"script_id={script_id},"
            f"ai_agent=true,"
            f"origination_caller_id_number={caller_id},"
            f"originate_timeout={originate_timeout},"
            f"ignore_early_media=true,"
            f"hangup_after_bridge=false"
            f"}}"
            f"sofia/gateway/{gateway}/{phone} "
            f"&socket({socket_host}:{socket_port} async full)"
        )
        logger.info(f"ESL originate → {phone} (uuid={call_uuid[:8]}, task={task_id})")
        return await self.bgapi(cmd, job_uuid=call_uuid)

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
            return await conn.originate(**kwargs)

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

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._uuid: Optional[str] = None
        self._channel_vars: dict = {}
        self._lock = asyncio.Lock()
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._connected = True
        self._playback_done = asyncio.Event()

    @property
    def uuid(self) -> Optional[str]:
        return self._uuid

    @property
    def channel_vars(self) -> dict:
        """所有 FreeSWITCH channel 变量（variable_xxx 去掉前缀后）"""
        return self._channel_vars

    async def connect(self) -> dict:
        """完成握手，返回全部 channel 数据"""
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
        if lock:
            msg += "event-lock: true\n"
        msg += f"Event-UUID: {event_uuid}\n\n"
        async with self._lock:
            await self._send(msg)
        return event_uuid

    async def play(self, audio_path: str, timeout: float = 60.0):
        """播放音频，阻塞直到播放完成或超时"""
        self._playback_done.clear()
        await self.execute("playback", audio_path)
        try:
            await asyncio.wait_for(self._playback_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[{self._uuid}] 播放超时 ({timeout}s)")
            await self.stop_playback()

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

    async def start_audio_capture(self) -> asyncio.Queue:
        """开始捕获通话音频，返回 PCM 数据队列"""
        await self.execute("start_dtmf")
        return self._audio_queue

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
                logger.info(f"[{self._uuid}] 通话挂断: {cause}")
                self._connected = False
                await self._safe_put(self._event_queue, {"type": "hangup", "cause": cause})
                break

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
                if body:
                    try:
                        pcm = base64.b64decode(body)
                        await self._safe_put(self._audio_queue, pcm)
                    except Exception:
                        pass

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
    ):
        self.host = host
        self.port = port
        self.call_handler = call_handler
        self._sem = asyncio.Semaphore(max_connections)
        self._server: Optional[asyncio.Server] = None

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
            session = ESLSocketCallSession(reader, writer)
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
