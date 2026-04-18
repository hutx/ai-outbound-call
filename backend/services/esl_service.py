"""
FreeSWITCH ESL 服务 — Inbound 连接池
─────────────────────────────────
Inbound ESL  : 后端主动连 FS:8021，下发 originate / API 命令
连接池        : AsyncESLPool — 复用 Inbound 连接，避免每次外呼都新建 TCP
"""
import asyncio
import logging
import re
import time
import uuid
from typing import Optional

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
        internal_domain: str = "$${local_ip_v4}",
    ) -> str:
        """发起外呼。使用 bgapi originate。"""
        endpoint, target_type, _ = self._build_originate_target(
            phone=phone, gateway=gateway, internal_domain=internal_domain,
        )

        simple_vars = (
            f"export_origination_uuid={call_uuid},"
            f"export_callee_number={phone},"
            f"export_script_id={script_id},"
            f"ai_agent=true,"
            f"proxy_media=true,"
            f"task_id={task_id},"
            f"script_id={script_id},"
            f"origination_caller_id_number={caller_id},"
            f"originate_timeout={originate_timeout}"
        )

        cmd = f"originate {{{simple_vars}}}{endpoint} &bridge(loopback/AI_CALL)"
        logger.debug(f"ESL 发送 originate 命令: {cmd[:200]}")
        logger.info(
            f"ESL originate → {phone} "
            f"(uuid={call_uuid[:8]}, task={task_id}, target={target_type})"
        )
        job_uuid = await self.bgapi(cmd, job_uuid=call_uuid)
        logger.info(f"ESL originate 回复: {job_uuid}")
        if job_uuid.startswith("-ERR"):
            raise ESLError(f"originate 失败: {job_uuid}")
        return call_uuid

    @staticmethod
    def _build_originate_target(phone: str, gateway: str, internal_domain: str) -> tuple[str, str, str]:
        """构建 originate 目标端点。"""
        normalized = (phone or "").strip()
        if INTERNAL_EXTENSION_RE.fullmatch(normalized):
            domain = (internal_domain or "192.168.5.15").strip()
            endpoint = f"user/{normalized}@{domain}"
            return endpoint, "internal_extension", normalized
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
        found_headers = False
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
                if not found_headers:
                    continue
                break
            found_headers = True
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip()] = v.strip()
        content_length = int(headers.get("Content-Length", 0))
        if content_length > 0:
            body = await self._reader.readexactly(content_length)
            headers["_body"] = body.decode("utf-8", errors="replace")
        return headers


# ─────────────────────────────────────────────────────────────
# ESL 事件监听器（专用持久连接）
# ─────────────────────────────────────────────────────────────
class ESLEventListener:
    """专用 ESL 连接，持久订阅 FreeSWITCH 事件。"""

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._running = True
        self._waiters: dict[str, asyncio.Queue] = {}
        self._job_waiters: dict[str, asyncio.Queue] = {}
        self._listener_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._connected and self._writer is not None and not self._writer.is_closing()

    async def start(self):
        self._listener_task = asyncio.create_task(self._connect_and_listen())
        logger.info(f"ESL 事件监听器启动中: {self.host}:{self.port}")

    async def _connect_and_listen(self):
        retry_delay = 3.0
        while self._running:
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=5.0
                )
                event = await asyncio.wait_for(self._read_event(), timeout=5.0)
                if event.get("Content-Type") != "auth/request":
                    raise ESLError(f"期望 auth/request，收到: {event}")
                await self._send(f"auth {self.password}\n\n")
                reply = await asyncio.wait_for(self._read_event(), timeout=5.0)
                if "+OK" not in reply.get("Reply-Text", ""):
                    raise ESLError(f"ESL 认证失败: {reply}")

                await self._send("event CHANNEL_ANSWER CHANNEL_HANGUP CHANNEL_HANGUP_COMPLETE BACKGROUND_JOB\n\n")
                reply = await asyncio.wait_for(self._read_event(), timeout=5.0)
                if "+OK" not in reply.get("Reply-Text", ""):
                    logger.warning(f"事件订阅回复异常: {reply.get('Reply-Text', '')}")

                self._connected = True
                retry_delay = 3.0
                logger.info(f"ESL 事件监听器就绪: {self.host}:{self.port}")
                await self._listen_loop()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                if self._running:
                    logger.warning(f"ESL 事件监听器连接失败，{retry_delay}s 后重试: {e}")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)

    async def stop(self):
        self._running = False
        self._connected = False
        if self._listener_task:
            self._listener_task.cancel()
            await asyncio.gather(self._listener_task, return_exceptions=True)
        for queue in self._waiters.values():
            await queue.put({"type": "listener_stopped"})
        self._waiters.clear()
        if self._writer:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=2.0)
            except Exception:
                pass
        logger.info("ESL 事件监听器已关闭")

    def register_waiter(self, call_uuid: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._waiters[call_uuid] = queue
        return queue

    def register_job_waiter(self, job_uuid: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._job_waiters[job_uuid] = queue
        return queue

    def unregister_job_waiter(self, job_uuid: str):
        self._job_waiters.pop(job_uuid, None)

    def unregister_waiter(self, call_uuid: str):
        self._waiters.pop(call_uuid, None)

    async def wait_for_answer(self, call_uuid: str, timeout: float = 60.0) -> Optional[dict]:
        queue = self.register_waiter(call_uuid)
        try:
            event = await asyncio.wait_for(queue.get(), timeout=timeout)
            if event.get("type") == "listener_stopped":
                logger.warning(f"[{call_uuid[:8]}] 事件监听器已停止")
                return None
            return event
        except asyncio.TimeoutError:
            logger.warning(f"[{call_uuid[:8]}] 等待 CHANNEL_ANSWER 超时 ({timeout}s)")
            return None
        finally:
            self.unregister_waiter(call_uuid)

    async def _listen_loop(self):
        while self._connected and self._running:
            try:
                event = await asyncio.wait_for(self._read_event(), timeout=120.0)
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                continue
            except Exception:
                self._connected = False
                logger.warning("ESL 事件监听器连接断开")
                break

            if "_body" in event:
                for line in event["_body"].split("\n"):
                    if ":" in line:
                        k, _, v = line.partition(":")
                        event[k.strip()] = v.strip()

            event_name = event.get("Event-Name", "")
            unique_id = event.get("Unique-ID", "")
            orig_uuid = event.get("variable_origination_uuid", "")
            call_uuid = unique_id or orig_uuid

            if event_name == "CHANNEL_ANSWER":
                async with self._lock:
                    waiter = self._waiters.get(call_uuid)
                    if waiter:
                        try:
                            waiter.put_nowait({"type": "answered", "uuid": call_uuid, "event": event})
                        except asyncio.QueueFull:
                            pass
                    else:
                        for uuid_key, q in list(self._waiters.items()):
                            if uuid_key != call_uuid and (
                                event.get("variable_origination_uuid", "") == uuid_key
                                or event.get("Channel-Call-UUID", "") == uuid_key
                            ):
                                try:
                                    q.put_nowait({"type": "answered", "uuid": call_uuid, "event": event})
                                except asyncio.QueueFull:
                                    pass
                                break

            elif event_name in ("CHANNEL_HANGUP", "CHANNEL_HANGUP_COMPLETE"):
                async with self._lock:
                    waiter = self._waiters.get(call_uuid)
                    if waiter:
                        try:
                            waiter.put_nowait({"type": "hangup", "uuid": call_uuid, "event": event})
                        except asyncio.QueueFull:
                            pass

            elif event_name == "BACKGROUND_JOB":
                job_uuid = event.get("Job-UUID", "")
                async with self._lock:
                    job_waiter = self._job_waiters.get(job_uuid)
                    if job_waiter:
                        try:
                            job_waiter.put_nowait({"type": "bgapi_result", "event": event})
                        except asyncio.QueueFull:
                            pass
                        finally:
                            self._job_waiters.pop(job_uuid, None)

            logger.debug(
                f"ESL Event: {event_name} uuid={call_uuid[:8] if call_uuid else '???'}"
            )

    async def _send(self, data: str):
        self._writer.write(data.encode())
        await self._writer.drain()

    async def _read_event(self) -> dict:
        headers: dict = {}
        found_headers = False
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
                if not found_headers:
                    continue
                break
            found_headers = True
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
    """ESL Inbound 连接池"""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        pool_size: int = 5,
        idle_timeout: float = 60.0,
        event_listener: Optional[ESLEventListener] = None,
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
        self._event_listener = event_listener
        self._aleg_uuids: dict[str, str] = {}

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
        """发起外呼。每次创建专用连接，命令完成后关闭。"""
        call_uuid = kwargs.get("call_uuid", "")
        job_uuid = kwargs.get("job_uuid", "") or call_uuid

        answer_queue = None
        job_queue = None
        if self._event_listener and self._event_listener.is_connected and call_uuid:
            answer_queue = self._event_listener.register_waiter(call_uuid)
            job_queue = self._event_listener.register_job_waiter(job_uuid)

        conn = AsyncESLConnection(self._host, self._port, self._password)
        try:
            await asyncio.wait_for(conn.connect(), timeout=5.0)
            returned_job_uuid = await conn.originate(**kwargs)
        finally:
            await conn.close()

        if job_queue is not None:
            asyncio.create_task(
                self._wait_and_validate_originate_result(call_uuid, returned_job_uuid, answer_queue, job_queue, kwargs)
            )
        elif answer_queue is not None:
            asyncio.create_task(
                self._wait_and_start_ai_from_queue(call_uuid, answer_queue, kwargs)
            )
        else:
            logger.warning(f"ESL 事件监听器不可用，originate 后将无法可靠检测应答")

        return returned_job_uuid

    async def _wait_and_validate_originate_result(
        self, call_uuid: str, job_uuid: str,
        answer_queue: Optional[asyncio.Queue],
        job_queue: asyncio.Queue,
        kwargs: dict,
    ):
        """等待 BACKGROUND_JOB 事件确认 originate 结果。"""
        try:
            job_result = await asyncio.wait_for(job_queue.get(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(f"[{call_uuid[:8]}] 等待 BACKGROUND_JOB 超时 (30.0s)")
            if answer_queue is not None:
                asyncio.create_task(
                    self._wait_and_start_ai_from_queue(call_uuid, answer_queue, kwargs)
                )
            return
        except asyncio.CancelledError:
            return

        event = job_result.get("event", {})
        reply_text = event.get("Job-Response", "") or event.get("Reply-Text", "")
        err_cause = event.get("err", "") or event.get("Error", "") or event.get("Cause", "")

        if "-ERR" in reply_text or "ERR" in reply_text or err_cause:
            logger.error(f"[{call_uuid[:8]}] originate 失败: reply={reply_text.strip()[:100]}, err={err_cause}")
            return

        logger.info(f"[{call_uuid[:8]}] originate 成功")

        if answer_queue is not None:
            await self._wait_and_start_ai_from_queue(call_uuid, answer_queue, kwargs)

    async def _wait_and_start_ai_from_queue(self, call_uuid: str, queue: asyncio.Queue, kwargs: dict):
        """等待 CHANNEL_ANSWER 事件（仅日志，AI 由 dialplan forkzstream 处理）。"""
        try:
            result = await asyncio.wait_for(queue.get(), timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning(f"[{call_uuid[:8]}] 等待 CHANNEL_ANSWER 超时 (60.0s)")
            return
        except asyncio.CancelledError:
            return

        if result.get("type") != "answered":
            logger.warning(f"[{call_uuid[:8]}] 未收到 CHANNEL_ANSWER 事件")
            return

        answered_uuid = result.get("uuid", call_uuid)
        event_data = result.get("event", {})
        full_uuid = event_data.get("Unique-ID", answered_uuid)
        if len(str(full_uuid)) < 30:
            full_uuid = call_uuid

        self._aleg_uuids[call_uuid] = full_uuid
        logger.info(f"[{call_uuid[:8]}] 收到 CHANNEL_ANSWER，forkzstream dialplan 已处理 AI 流程")

    def _is_internal_extension(self, phone: str) -> bool:
        normalized = (phone or "").strip()
        return bool(INTERNAL_EXTENSION_RE.fullmatch(normalized))

    async def get_aleg_uuid(self, call_uuid: str) -> Optional[str]:
        return self._aleg_uuids.get(call_uuid)

    async def api(self, command: str) -> str:
        async with self._sem:
            conn = await self._acquire()
            return await conn.api(command)

    async def _acquire(self) -> AsyncESLConnection:
        async with self._pool_lock:
            for conn in self._conns:
                if conn.is_connected:
                    return conn
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
                    elif time.time() - conn._last_used > self._idle_timeout:
                        await asyncio.wait_for(conn.api("status"), timeout=3.0)
                except Exception as e:
                    logger.warning(f"ESL keepalive 失败: {e}")
                    conn._connected = False
