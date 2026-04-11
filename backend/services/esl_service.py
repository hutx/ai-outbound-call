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
        logger.debug(f"bgapi 回复 event keys: {list(event.keys())}, Reply-Text: {event.get('Reply-Text', '')[:100]}")
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
        发起外呼。使用同步 api originate。
        变量通过 originate 的 [ ] 语法传入。
        """
        endpoint, endpoint_type, target_type = self._build_originate_target(
            phone=phone,
            gateway=gateway,
            internal_domain=internal_domain,
        )

        # FreeSWITCH 变量继承规则：
        # - [var=val] 仅设置 A-leg
        # - [export_var=val] 导出到 B-leg（bridge 后的通道）
        channel_vars = (
            f"origination_uuid={call_uuid},"
            f"ai_agent=true,"
            f"export_ai_agent=true,"
            f"export_task_id={task_id},"
            f"export_script_id={script_id},"
            f"export_call_target_type={target_type},"
            f"export_original_destination={phone},"
            f"export_origination_uuid={call_uuid},"
            f"origination_caller_id_number={caller_id},"
            f"originate_timeout={originate_timeout}"
        )

        if endpoint_type == "internal_extension":
            # 内部分机：B-leg 接通后执行 &park() 进入 dialplan。
            # park 让 B-leg 带着 ai_agent=true 等变量进入 internal 拨号计划，
            # 匹配 AI_Agent_Call 扩展 → audio_stream + socket。
            # 不使用 bridge（bridge 会跳过 dialplan 直接桥接），也不使用 loopback（bridged channel 上无法启动音频流）。
            cmd = (
                f"originate [{channel_vars}] "
                f"{endpoint} &park()"
            )
        else:
            # PSTN 外呼：导出 ai_agent=true，default context 的 ai_outbound_bleg
            # 扩展会 answer → record_session → socket(backend:9999)
            # 添加 export_ 前缀使变量传递到 B-leg（PSTN 通道）
            pstn_vars = (
                f"origination_uuid={call_uuid},"
                f"ai_agent=true,"
                f"export_ai_agent=true,"
                f"export_task_id={task_id},"
                f"export_script_id={script_id},"
                f"export_call_target_type={target_type},"
                f"export_original_destination={phone},"
                f"export_origination_uuid={call_uuid},"
                f"origination_caller_id_number={caller_id},"
                f"originate_timeout={originate_timeout}"
            )
            cmd = (
                f"originate [{pstn_vars}] "
                f"{endpoint}"
            )
        logger.debug(f"ESL 发送 originate 命令: {cmd[:200]}")
        logger.info(
            f"ESL originate → {phone} "
            f"(uuid={call_uuid[:8]}, task={task_id}, target={target_type})"
        )
        # 使用 bgapi（非阻塞，&socket 会长时间阻塞 api 调用）
        job_uuid = await self.bgapi(cmd, job_uuid=call_uuid)
        logger.info(f"ESL originate 回复: {job_uuid}")
        if job_uuid.startswith("-ERR"):
            raise ESLError(f"originate 失败: {job_uuid}")

        return call_uuid

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
            logger.debug(f"ESL originate → {phone} (internal_extension) → user/{normalized}@{domain}")
            # 使用 user/ endpoint 查找注册信息（directory），FreeSWITCH 自动处理 NAT。
            # 用户的 user_context=internal（directory.xml），B-leg 进入 internal 拨号计划。
            # ai_agent=true 通过 channel variable 传递，dialplan 条件匹配。
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
                    continue  # Skip leading empty lines (FreeSWITCH auth response has leading \n)
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
    """
    专用 ESL 连接，持久订阅 FreeSWITCH 事件。
    在应用启动时建立连接并订阅， originate 前注册回调，
    避免 originate 后再订阅导致事件丢失。
    """

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._running = True  # 控制重连循环
        # uuid → asyncio.Queue (等待该 uuid 的 CHANNEL_ANSWER)
        self._waiters: dict[str, asyncio.Queue] = {}
        self._job_waiters: dict[str, asyncio.Queue] = {}  # job_uuid → Queue (BACKGROUND_JOB)
        self._listener_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._connected and self._writer is not None and not self._writer.is_closing()

    async def start(self):
        """建立连接、认证、订阅事件，开始后台监听循环（含自动重连）。"""
        self._listener_task = asyncio.create_task(self._connect_and_listen())
        logger.info(f"ESL 事件监听器启动中: {self.host}:{self.port}")

    async def _connect_and_listen(self):
        """带重连的连接-监听循环。"""
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

                # 进入监听循环
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
        # 唤醒所有等待者
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
        """注册一个等待队列， originate 前调用。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._waiters[call_uuid] = queue
        return queue

    def register_job_waiter(self, job_uuid: str) -> asyncio.Queue:
        """注册 BACKGROUND_JOB 等待队列。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._job_waiters[job_uuid] = queue
        return queue

    def unregister_job_waiter(self, job_uuid: str):
        self._job_waiters.pop(job_uuid, None)

    def unregister_waiter(self, call_uuid: str):
        """移除等待队列。"""
        self._waiters.pop(call_uuid, None)

    async def wait_for_answer(self, call_uuid: str, timeout: float = 60.0) -> Optional[dict]:
        """等待指定 UUID 的 CHANNEL_ANSWER 事件。"""
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
        """持续读取 ESL 事件，分发给等待者。"""
        while self._connected and self._running:
            try:
                event = await asyncio.wait_for(self._read_event(), timeout=120.0)
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                # 长时间无事件，保持连接继续监听（非致命）
                continue
            except Exception:
                self._connected = False
                logger.warning("ESL 事件监听器连接断开")
                break

            # text/event-plain 格式：事件数据在 _body 中
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
                        # 没有精确匹配的 waiter，尝试 origination_uuid 匹配
                        for uuid_key, q in list(self._waiters.items()):
                            if uuid_key != call_uuid and (
                                event.get(f"variable_origination_uuid", "") == uuid_key
                                or event.get("Channel-Call-UUID", "") == uuid_key
                            ):
                                try:
                                    q.put_nowait({"type": "answered", "uuid": call_uuid, "event": event})
                                except asyncio.QueueFull:
                                    pass
                                break

            elif event_name in ("CHANNEL_HANGUP", "CHANNEL_HANGUP_COMPLETE"):
                # 唤醒可能还在等待的 waiter
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
                    # Skip leading empty lines (FreeSWITCH may send extra \n)
                    continue
                # End of headers
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
        """
        使用专用连接发起 originate，避免事件订阅干扰命令回复。
        每次 originate 创建独立连接，命令完成后关闭。
        """
        call_uuid = kwargs.get("call_uuid", "")
        job_uuid = kwargs.get("job_uuid", "") or call_uuid

        # 在 originate 之前就注册 waiter，避免事件丢失
        answer_queue = None
        job_queue = None
        if self._event_listener and self._event_listener.is_connected and call_uuid:
            answer_queue = self._event_listener.register_waiter(call_uuid)
            job_queue = self._event_listener.register_job_waiter(job_uuid)

        # 创建专用连接
        conn = AsyncESLConnection(self._host, self._port, self._password)
        try:
            await asyncio.wait_for(conn.connect(), timeout=5.0)
            returned_job_uuid = await conn.originate(**kwargs)
        finally:
            await conn.close()

        # 检查 BACKGROUND_JOB 获取真实 originate 结果
        if job_queue is not None:
            asyncio.create_task(
                self._wait_and_validate_originate_result(call_uuid, returned_job_uuid, answer_queue, job_queue, kwargs)
            )
        elif answer_queue is not None:
            # 降级：只等待 CHANNEL_ANSWER
            asyncio.create_task(
                self._wait_and_start_ai_from_queue(call_uuid, answer_queue, kwargs)
            )
        else:
            logger.warning(f"ESL 事件监听器不可用，originate 后将无法可靠检测应答")

        return returned_job_uuid

    async def _wait_and_validate_originate_result(
        self, call_uuid: str, job_uuid: str,
        answer_queue: asyncio.Queue | None,
        job_queue: asyncio.Queue,
        kwargs: dict,
    ):
        """等待 BACKGROUND_JOB 事件确认 originate 结果，成功后再等 CHANNEL_ANSWER。"""
        try:
            job_result = await asyncio.wait_for(job_queue.get(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(f"[{call_uuid[:8]}] 等待 BACKGROUND_JOB 超时 (30.0s)")
            # 超时不代表失败，继续等 CHANNEL_ANSWER
            if answer_queue is not None:
                asyncio.create_task(
                    self._wait_and_start_ai_from_queue(call_uuid, answer_queue, kwargs)
                )
            return
        except asyncio.CancelledError:
            return

        event = job_result.get("event", {})
        job_action = event.get("Job-Action", "")
        reply_text = event.get("Job-Response", "") or event.get("Reply-Text", "")

        # 检查 originate 实际结果：originate 失败时 err 字段会有错误原因
        err_cause = event.get("err", "") or event.get("Error", "") or event.get("Cause", "")
        job_status = event.get("Job-Status", "")

        if "-ERR" in reply_text or "ERR" in reply_text or err_cause:
            logger.error(f"[{call_uuid[:8]}] originate 失败 (BACKGROUND_JOB): reply={reply_text.strip()[:100]}, err={err_cause}, event_keys={list(event.keys())[:15]}")
            return

        logger.info(f"[{call_uuid[:8]}] originate 成功 (BACKGROUND_JOB): {job_action}")

        # originate 成功，继续等待 CHANNEL_ANSWER
        if answer_queue is not None:
            await self._wait_and_start_ai_from_queue(call_uuid, answer_queue, kwargs)

    async def _wait_and_start_ai_from_queue(self, call_uuid: str, queue: asyncio.Queue, kwargs: dict):
        """从已注册的 queue 等待 CHANNEL_ANSWER。

        对于内部分机：originate 使用 &bridge(loopback/AI_CALL)，
        拨号计划会处理 audio_stream + socket，无需额外操作。
        对于 PSTN 外呼：需要后续完善（目前 &park() + transfer）。
        """
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
        logger.info(f"[{call_uuid[:8]}] 收到 CHANNEL_ANSWER (实际 uuid={answered_uuid[:8]})")
        # 拨号计划已处理 audio_stream + socket，CallAgent 由 ESL socket 自动启动

    async def _wait_and_start_ai(self, call_uuid: str, phone: str, task_id: str, script_id: str, target_type: str):
        """
        等待 CHANNEL_ANSWER 事件，收到后启动 uuid_audio_stream + uuid_broadcast。
        使用事件监听器的持久连接，不依赖 pool 连接。
        """
        assert self._event_listener is not None
        event = await self._event_listener.wait_for_answer(call_uuid, timeout=60.0)
        if event is None or event.get("type") != "answered":
            logger.warning(f"[{call_uuid[:8]}] 未收到 CHANNEL_ANSWER 事件")
            return

        answered_uuid = event.get("uuid", call_uuid)
        logger.info(f"[{call_uuid[:8]}] 收到 CHANNEL_ANSWER (实际 uuid={answered_uuid[:8]})")

        # 使用 pool 的任意连接执行 API 调用
        ws_url = f"ws://backend:8765/{answered_uuid}"
        socket_addr = "backend:9999 async full"

        # 1. 启动 mod_audio_stream
        try:
            result = await self.api(f"uuid_audio_stream {answered_uuid} start {ws_url} mono 8000")
            if result.strip().startswith("+OK"):
                logger.info(f"[{call_uuid[:8]}] uuid_audio_stream 启动成功: {ws_url}")
            else:
                logger.warning(f"[{call_uuid[:8]}] uuid_audio_stream 返回: {result.strip()[:200]}")
        except Exception as e:
            logger.warning(f"[{call_uuid[:8]}] uuid_audio_stream 失败: {e}")

        # 2. 广播 socket 应用连接后端 ESL Outbound
        try:
            result = await self.api(f"uuid_broadcast {answered_uuid} {socket_addr} both")
            logger.info(f"[{call_uuid[:8]}] uuid_broadcast socket 结果: {result.strip()[:100]}")
        except Exception as e:
            logger.error(f"[{call_uuid[:8]}] uuid_broadcast socket 失败: {e}")

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
        # Also log loopback-specific vars for debugging
        for k in data:
            if "loopback" in k.lower() or k in ("Other-Leg-Channel-Name", "Other-Type", "Other-Leg-Source"):
                logger.debug(
                    f"[{self._uuid[:8]}] channel var {k} = {data[k][:100]}"
                )

        logger.info(
            f"ESL Outbound 握手 uuid={self._uuid} "
            f"task={self._channel_vars.get('task_id','?')} "
            f"script={self._channel_vars.get('script_id','?')}"
        )

        # 订阅此通话所有事件
        await self._send("myevents\n\n")
        await self._read_event()
        await self._send("divert_events on\n\n")

        # 用户音频捕获由 dialplan 中的 audio_stream 应用处理，
        # 此 socket 连接仅用于通话控制（TTS 播放、DTMF 等）。
        logger.debug(
            f"[{self._uuid[:8]}] 音频捕获由 dialplan audio_stream 处理"
        )

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

        优先级：
        1. sofia 通道 mod_audio_stream 队列（dialplan 中启动，通过 ws_server 全局队列）
        2. WebSocket 音频流队列（mod_audio_stream 备用方案）
        3. 降级文件轮询（用 uuid_record 启动）
        """
        if not self._uuid:
            logger.warning("无法启动音频捕获: UUID 为空")
            self._audio_mode = "none"
            return asyncio.Queue()

        # 如果 sofia 通道的 mod_audio_stream 已在 connect() 中绑定
        if self._audio_mode == "websocket_sofia":
            logger.info(f"[{self._uuid}] 音频采集: 复用 sofia 通道的 mod_audio_stream 队列")
            # 返回 ws_server 的全局队列（包含 sofia 麦克风音频）
            sofia_uuid = self._channel_vars.get("other_loopback_from_uuid")
            if sofia_uuid and self.ws_server:
                try:
                    ws_queue = await self.ws_server.get_session_queue(sofia_uuid, timeout=1.0)
                    if ws_queue is not None:
                        return ws_queue
                except Exception:
                    pass
            # ws_server 队列不可用，降级
            self._audio_mode = "unknown"

        # 如果文件轮询已在运行（connect() 中的 uuid_record read），直接返回队列
        if self._audio_mode == "file_poll_read":
            logger.info(f"[{self._uuid}] 音频采集: 复用 connect() 中已启动的 sofia read 录制")
            return self._audio_queue

        # 如果音频流已在 connect() 中启动，直接返回队列
        if self.ws_server:
            try:
                ws_queue = await self.ws_server.get_session_queue(self._uuid, timeout=5.0)
                if ws_queue is not None:
                    self._audio_mode = "websocket"
                    self._audio_started = True
                    logger.info(f"[{self._uuid}] 音频采集: 复用 connect() 中已启动的 mod_audio_stream")
                    return ws_queue
            except Exception as e:
                logger.warning(f"[{self._uuid}] 获取音频队列失败: {e}")

        # 降级 — 文件轮询（用 uuid_record 启动录音）
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
        found_headers = False
        while True:
            try:
                line = await self._reader.readline()
            except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
                raise ESLError("连接断开")
            if not line:
                raise ESLError("连接断开（EOF）")
            line = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if not found_headers:
                    continue
                break
            found_headers = True
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
