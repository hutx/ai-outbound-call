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
        endpoint, target_type, _ = self._build_originate_target(
            phone=phone,
            gateway=gateway,
            internal_domain=internal_domain,
        )

        # FreeSWITCH 变量传递规则：
        # - {var=val} 设置 A-leg 变量，值中不能有空格
        # - [var=val] channel 变量，允许值中包含空格
        # - {export_var=val} 导出到 bridge 后的 B-leg（仅对 sofia/gateway 有效）
        # - loopback B-leg 不继承 export_ 变量，需要在 bridge 命令中显式传递
        #
        # execute_on_bridge 值中包含空格（audio_fork ws://...），必须用 [ ] 语法
        simple_vars = (
            f"origination_uuid={call_uuid},"
            f"export_origination_uuid={call_uuid},"
            f"ai_agent=true,"
            f"task_id={task_id},"
            f"script_id={script_id},"
            f"origination_caller_id_number={caller_id},"
            f"originate_timeout={originate_timeout},"
            f"proxy_media=true,"
            f"bypass_media=false"
        )

        endpoint, endpoint_type, ext_num = self._build_originate_target(
            phone=phone,
            gateway=gateway,
            internal_domain=internal_domain,
        )

        if endpoint_type == "internal_extension":
            # 内部分机：拨打用户，接通后桥接到 AI 拨号计划扩展
            # audio_fork 通过 uuid_audio_fork API 在 B-leg socket 连接后启动
            # proxy_media=true + bypass_media=false 确保 RTP 经过 FreeSWITCH 软件层
            cmd = (
                f"originate {{{simple_vars}}}{endpoint} "
                f"&bridge(loopback/AI_CALL)"
            )
        else:
            # PSTN 外呼：通过运营商网关导出，接通后 uuid_transfer 到 AI_Handler
            # 与内部分机统一：先建立通道，再 uuid_transfer 到 AI_Handler
            pstn_vars = (
                f"origination_uuid={call_uuid},"
                f"ai_agent=true,"
                f"export_ai_agent=true,"
                f"export_origination_uuid={call_uuid},"
                f"task_id={task_id},"
                f"script_id={script_id},"
                f"origination_caller_id_number={caller_id},"
                f"originate_timeout={originate_timeout}"
            )
            cmd = (
                f"originate {{{pstn_vars}}}{endpoint} &park()"
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
        内部分机应答后：通过 uuid_transfer 转到 AI_Handler (9998 XML internal)。

        AI_Handler 执行 bypass_media=false + record_session + socket(async full)，
        socket 连接在 sofia A-leg 上（用户电话侧），媒体路径经过 FreeSWITCH，
        确保 uuid_broadcast TTS 和 uuid_audio_fork 都能正确工作。

        PSTN 外呼：同样 uuid_transfer 到 AI_Handler（保持统一）。
        """
        try:
            # 先等待 originate 完成，通道被创建
            await asyncio.sleep(2)

            # 轮询等待通道出现（originate 需要时间建立通道）
            channel_exists = False
            for attempt in range(60):  # 最多等 60 秒
                try:
                    result = await self.api(f"uuid_dump {call_uuid}")
                    if result and "-ERR No such channel" not in result:
                        channel_exists = True
                        logger.info(f"[{call_uuid[:8]}] 通道已建立，启动 AI 流程")
                        break
                    elif result and "-ERR No such channel" in result:
                        await asyncio.sleep(1)
                        continue
                except Exception:
                    await asyncio.sleep(1)

            if not channel_exists:
                logger.warning(f"[{call_uuid[:8]}] 外呼等待通道建立超时")
                return

            if target_type == "internal_extension":
                # ★ 内部分机：uuid_transfer 到 AI_Handler (9998 XML internal)
                # sofia A-leg 转到 AI_Handler 后执行：
                # bypass_media=false + proxy_media=true + record_session + socket(async)
                # socket 直接连接在 sofia A-leg（用户电话侧）上。
                result = await self.api(
                    f"uuid_transfer {call_uuid} 9998 XML internal"
                )
                logger.info(f"[{call_uuid[:8]}] uuid_transfer 到 AI_Handler 结果: {result.strip()[:100]}")
            else:
                # PSTN 外呼：广播 socket 应用连接后端 ESL Outbound 服务
                socket_addr = "backend:9999 async full"
                try:
                    result = await self.api(
                        f"uuid_broadcast {call_uuid} {socket_addr} both"
                    )
                    logger.info(f"[{call_uuid[:8]}] uuid_broadcast socket 结果: {result.strip()[:100]}")
                except Exception as e:
                    logger.error(f"[{call_uuid[:8]}] uuid_broadcast socket 失败: {e}")

        except Exception as e:
            logger.error(f"[{call_uuid[:8]}] _on_call_answered 异常: {e}", exc_info=True)

    async def _pre_generate_opening(self, script_id: str) -> Optional[str]:
        """预生成开场白 TTS 文件，供 dialplan playback 使用。

        dialplan 的 ai_call_handler 在 socket 之前执行 playback(${opening_greeting})，
        此时媒体路径正常（sofia A-leg 直接到 loopback-a），TTS 能播放到用户电话。

        TTS 文件必须生成到 /recordings（FreeSWITCH 可访问的共享目录）。
        返回 TTS 文件路径，失败返回 None。
        """
        try:
            from backend.services.async_script_utils import get_opening_for_call
            from backend.services.tts_service import create_tts_client

            opening = await get_opening_for_call(script_id, {})
            opening_text = opening.get("reply", "")
            if not opening_text:
                logger.warning("预生成开场白：开场白文本为空")
                return None

            tts = create_tts_client()
            tts_path = await tts.synthesize(opening_text)
            if not tts_path or not os.path.exists(tts_path):
                logger.warning(f"预生成开场白：TTS 合成失败或文件不存在: {tts_path}")
                return None

            # TTS 默认输出到 /tmp/tts_cache，FreeSWITCH 无法访问
            # 需要复制到 /recordings（FreeSWITCH 共享目录）
            shared_dir = os.environ.get("FS_RECORDING_PATH", "/recordings")
            os.makedirs(shared_dir, exist_ok=True)
            if not tts_path.startswith(shared_dir):
                import shutil
                dest = os.path.join(shared_dir, os.path.basename(tts_path))
                shutil.copy2(tts_path, dest)
                tts_path = dest
                logger.debug(f"已复制 TTS 到共享目录: {tts_path}")

            return tts_path
        except Exception as e:
            logger.warning(f"预生成开场白异常: {e}")
            return None

    @staticmethod
    def _build_originate_target(phone: str, gateway: str, internal_domain: str) -> tuple[str, str, str]:
        """构建 originate 目标端点。

        内部分机：使用 user/{ext}@{domain}，通过 FreeSWITCH 目录解析
        为已注册的 sofia 联系人地址，避免 sofia/internal 直接寻址
        时产生独立 inbound 通道导致无法桥接的问题。
        PSTN：通过运营商网关导出。
        """
        normalized = (phone or "").strip()
        if INTERNAL_EXTENSION_RE.fullmatch(normalized):
            domain = (internal_domain or "192.168.5.15").strip()
            endpoint = f"user/{normalized}@{domain}"
            logger.debug(f"ESL originate → {phone} (internal_extension) → {endpoint}")
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
        ws_server=None,
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
        self._ws_server = ws_server  # AudioStreamWebSocket for uuid_audio_fork
        # call_uuid(origination_uuid) → B-leg UUID 映射（由 _wait_and_start_ai_from_queue 设置）
        self._aleg_uuids: dict[str, str] = {}

    @property
    def ws_server(self):
        return self._ws_server

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
        拨号计划会处理 socket，无需额外操作。
        对于 PSTN 外呼：&park() 后等待后续处理。
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
        logger.info(f"[{call_uuid[:8]}] 收到 CHANNEL_ANSWER (实际 uuid={answered_uuid[:8] if len(str(answered_uuid)) > 8 else answered_uuid})")

        event_data = result.get("event", {})
        full_uuid = event_data.get("Unique-ID", answered_uuid)
        if len(str(full_uuid)) < 30:
            full_uuid = call_uuid

        self._aleg_uuids[call_uuid] = full_uuid

        # 判断是否为内部分机呼叫
        phone = kwargs.get("phone", "")
        is_internal = self._is_internal_extension(phone)

        if is_internal:
            # 内部分机：bridge(loopback/AI_CALL) 已匹配 ai_call_handler dialplan
            # socket(async full) 已建立，CallAgent 由 ESL socket 自动启动
            logger.info(f"[{call_uuid[:8]}] 内部分机已接通，socket 连接由 dialplan ai_call_handler 处理")
        else:
            # PSTN 外呼：B-leg (sofia/gateway) 已匹配 ai_outbound_bleg dialplan
            # dialplan 已执行 answer → record_session → socket(async full)
            # CallAgent 由 B-leg 的 ESL socket 自动启动，A-leg 停留在 park
            logger.info(f"[{call_uuid[:8]}] PSTN 外呼已接通，socket 连接由 B-leg dialplan ai_outbound_bleg 处理")

    async def _transfer_to_ai_handler(self, call_uuid: str):
        """执行 uuid_transfer 将通道转到 AI_Handler (9998 XML internal)。

        AI_Handler dialplan 执行：
        - answer() + sleep(200) + record_session
        - socket(backend:9999 async full) ← ESL Outbound
        """
        try:
            result = await self.api(f"uuid_transfer {call_uuid} 9998 XML internal")
            logger.info(f"[{call_uuid[:8]}] uuid_transfer 到 AI_Handler 结果: {result.strip()[:200]}")
        except Exception as e:
            logger.error(f"[{call_uuid[:8]}] uuid_transfer 失败: {e}")

    async def _wait_for_bridge(self, call_uuid: str, timeout: float = 60.0) -> bool:
        """轮询等待通道桥接完成。返回 True=已桥接，False=超时。

        只检查 BRIDGE_UUID — 这是桥接完成的唯一可靠标志。
        CS_EXCHANGE_MEDIA 可能是振铃/early media，不代表已桥接。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                dump = await self.api(f"uuid_dump {call_uuid}")
                if dump and "-ERR" not in dump and "BRIDGE_UUID" in dump:
                    logger.info(f"[{call_uuid[:8]}] 检测到 BRIDGE_UUID，桥接完成")
                    return True
            except Exception as e:
                logger.debug(f"[{call_uuid[:8]}] 轮询桥接状态失败: {e}")
            await asyncio.sleep(1)
        return False

    def _is_internal_extension(self, phone: str) -> bool:
        """判断是否为内部分机号码"""
        normalized = (phone or "").strip()
        return bool(INTERNAL_EXTENSION_RE.fullmatch(normalized))

    async def get_aleg_uuid(self, call_uuid: str) -> Optional[str]:
        """获取 A-leg UUID（用于 A-leg 录音路径查询）"""
        return self._aleg_uuids.get(call_uuid)

    async def _wait_and_start_ai(self, call_uuid: str, phone: str, task_id: str, script_id: str, target_type: str):
        """
        等待 CHANNEL_ANSWER 事件，收到后启动 uuid_audio_fork + uuid_broadcast。
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

        # 1. 启动 mod_audio_fork (stereo 模式)
        try:
            result = await self.api(f"uuid_audio_fork {answered_uuid} start {ws_url} stereo 8000")
            if result.strip().startswith("+OK"):
                logger.info(f"[{call_uuid[:8]}] uuid_audio_fork(stereo) 启动成功: {ws_url}")
            else:
                logger.warning(f"[{call_uuid[:8]}] uuid_audio_fork 返回: {result.strip()[:200]}")
        except Exception as e:
            logger.warning(f"[{call_uuid[:8]}] uuid_audio_fork 失败: {e}")

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
        self._audio_subscribers: list[asyncio.Queue] = []  # 每个 adapter 独立队列
        self._connected = True
        self._playback_done = asyncio.Event()
        self._hangup_cause: Optional[str] = None
        self._sip_code: Optional[int] = None
        self.esl_pool = esl_pool  # ESL Inbound pool for uuid_eavesdrop
        self.ws_server = ws_server  # AudioStreamWebSocket for mod_audio_fork
        self._audio_mode: str = "unknown"  # "websocket" | "file_poll" | "custom"
        self._audio_started: bool = False  # 防止重复启动 uuid_audio_fork
        self._active_uuid: Optional[str] = None  # CHANNEL_ANSWER 中捕获的实际通话 UUID
        self._aleg_uuid: Optional[str] = None  # sofia A-leg UUID (bridge_partner)

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

        # 通过 uuid_dump 获取完整 channel 信息，查找 A-leg UUID
        if self.esl_pool:
            try:
                dump = await self.esl_pool.api(f"uuid_dump {self._uuid}")
                import re
                for var in ("other_loopback_leg_uuid", "signal_bond", "bridge_partner_uuid_str"):
                    pattern = rf"Variable: {var}: ([\w-]+)"
                    match = re.search(pattern, dump)
                    if match and match.group(1) not in ("-ERR", "_undef_"):
                        self._channel_vars[var] = match.group(1)
                        logger.debug(f"[{self._uuid}] uuid_dump {var}={match.group(1)[:8]}...")
            except Exception as e:
                logger.debug(f"[{self._uuid}] uuid_dump 查询失败: {e}")

        logger.info(
            f"ESL Outbound 握手 uuid={self._uuid} "
            f"task={self._channel_vars.get('task_id','?')} "
            f"script={self._channel_vars.get('script_id','?')} "
            f"orig_uuid={self._channel_vars.get('origination_uuid','?')} "
            f"export_orig_uuid={self._channel_vars.get('export_origination_uuid','?')}"
        )
        # 调试：打印所有 UUID 相关的 channel vars
        uuid_keys = [k for k in self._channel_vars if 'uuid' in k.lower() or 'bond' in k.lower() or 'partner' in k.lower() or 'loopback' in k.lower() or 'bridge' in k.lower()]
        if uuid_keys:
            logger.info(f"[{self._uuid}] UUID 相关 channel_vars: { {k: self._channel_vars[k][:16] if len(self._channel_vars[k]) > 16 else self._channel_vars[k] for k in uuid_keys} }")

        # 尽早发现 A-leg UUID（sofia 用户侧），确保 TTS 能送到用户电话
        await self._discover_aleg_uuid()

        # ★ audio_fork 现在由 dialplan 的 audio_fork 应用启动（AI_Handler 扩展），
        #   不再需要从 ESL API 调用 uuid_audio_fork。
        #   如果 dialplan 未设置 audio_fork（如 PSTN 外呼），
        #   start_audio_capture() 会作为 fallback 处理。

        # 订阅此通话所有事件
        await self._send("myevents\n\n")
        await self._read_event()
        await self._send("divert_events on\n\n")

        return data

    async def _early_start_audio_fork(self):
        """在 connect() 中提前启动 uuid_audio_fork（幂等）。

        挂载在 B-leg（当前 socket 通道）上，
        后续 TTS uuid_broadcast 的音频会被 audio_fork 实时捕获。
        这确保 audio_fork 在 greeting 播放前已就绪。
        """
        if self._audio_started or not self._uuid:
            return
        if not self.ws_server or not self.esl_pool:
            logger.debug(f"[{self._uuid}] ws_server 或 esl_pool 不可用，跳过早期 audio_fork")
            return

        try:
            ws_url = f"ws://backend:8765/{self._uuid}"
            # 使用 read 模式：只捕获从用户方向来的音频（用户语音 → FreeSWITCH）
            # 不用 mixed（默认），避免 TTS 回声混入
            result = await self.esl_pool.api(
                f"uuid_audio_fork {self._uuid} start {ws_url} mono 8000"
            )
            if result.strip().startswith("+OK"):
                logger.info(f"[{self._uuid}] 早期 uuid_audio_fork 启动成功: {ws_url}")
                self._audio_started = True
                self._audio_mode = "websocket"
                # 预先注册 queue，避免 start_audio_capture 重复启动
                if self.ws_server:
                    try:
                        self._audio_queue = await self.ws_server.get_session_queue(self._uuid, timeout=5.0) or self._audio_queue
                    except Exception:
                        pass
            else:
                logger.debug(f"[{self._uuid}] 早期 uuid_audio_fork 返回: {result.strip()[:200]}")
        except Exception as e:
            logger.debug(f"[{self._uuid}] 早期 uuid_audio_fork 失败: {e}")

    async def _discover_aleg_uuid(self):
        """尽早发现用于 TTS uuid_broadcast 的目标 UUID。

        bridge(loopback/AI_CALL) 架构下：
        - sofia A-leg (用户电话) — 在 bridge 中 RTP 被旁路
        - loopback-a (桥接端点) — 和 sofia A-leg 交换媒体
        - loopback-b (socket 通道) — 当前 socket 所在的通道

        TTS 必须广播到 loopback-a，因为它才是和 sofia A-leg
        直接交换媒体的端点。广播到 sofia A-leg 在 bridge 中不生效。
        """
        if self._aleg_uuid:
            return

        aleg_uuid = None

        # 1. other_loopback_from_uuid — sofia A-leg UUID（uuid_displace 的正确目标）
        #    uuid_displace 必须写到 sofia A-leg，用户电话才能听到 TTS
        aleg_uuid = self._channel_vars.get("other_loopback_from_uuid", "")
        if aleg_uuid and aleg_uuid not in ("-ERR", "_undef_"):
            logger.info(f"[{self._uuid}] 从 other_loopback_from_uuid 找到 sofia A-leg: {aleg_uuid[:8]}...")

        # 2. export_origination_uuid — sofia A-leg UUID
        if not aleg_uuid:
            aleg_uuid = self._channel_vars.get("export_origination_uuid", "")
            if aleg_uuid and aleg_uuid not in ("-ERR", "_undef_"):
                logger.info(f"[{self._uuid}] 从 export_origination_uuid 找到 sofia A-leg: {aleg_uuid[:8]}...")

        # 3. origination_uuid — sofia A-leg UUID
        if not aleg_uuid:
            aleg_uuid = self._channel_vars.get("origination_uuid", "")
            if aleg_uuid and aleg_uuid not in ("-ERR", "_undef_"):
                logger.info(f"[{self._uuid}] 从 origination_uuid 找到 A-leg: {aleg_uuid[:8]}...")

        # 4. signal_bond — 桥接对端
        if not aleg_uuid:
            aleg_uuid = self._channel_vars.get("signal_bond", "")
            if aleg_uuid and aleg_uuid not in ("-ERR", "_undef_"):
                logger.info(f"[{self._uuid}] 从 signal_bond 找到桥接对端: {aleg_uuid[:8]}...")

        # 5. other_loopback_leg_uuid — loopback-a（fallback，仅在以上都找不到时用）
        if not aleg_uuid:
            aleg_uuid = self._channel_vars.get("other_loopback_leg_uuid", "")
            if aleg_uuid and aleg_uuid not in ("-ERR", "_undef_"):
                logger.warning(f"[{self._uuid}] 只能用 other_loopback_leg_uuid (loopback-a): {aleg_uuid[:8]}... uuid_displace 可能不生效")

        # 6. 其他 channel_vars
        if not aleg_uuid:
            for var in ("asr_aleg_uuid", "bridge_partner_uuid_str", "signal_bond",
                        "other_loopback_leg_uuid"):
                val = self._channel_vars.get(var, "")
                if val and val not in ("-ERR", "_undef_"):
                    aleg_uuid = val
                    logger.info(f"[{self._uuid}] 从 {var} 找到 A-leg: {aleg_uuid[:8]}...")
                    break

        # 5. 通过 uuid_getvar 查询（bridge 建立后才有值）
        if not aleg_uuid and self.esl_pool:
            for var in ("bridge_partner_uuid_str", "signal_bond"):
                try:
                    result = await self.esl_pool.api(f"uuid_getvar {self._uuid} {var}")
                    val = result.strip()
                    if val and val not in ("-ERR", "_undef_"):
                        aleg_uuid = val
                        logger.info(f"[{self._uuid}] 从 uuid_getvar {var} 找到 A-leg: {aleg_uuid[:8]}...")
                        break
                except Exception:
                    pass

        if aleg_uuid:
            self._aleg_uuid = aleg_uuid
            logger.info(f"[{self._uuid}] A-leg UUID 已发现: {aleg_uuid[:8]}...")
        else:
            logger.debug(f"[{self._uuid}] 暂未发现 A-leg UUID，稍后 start_audio_capture 会重试")

    async def _start_audio_fork_bleg(self):
        """在 B-leg 上启动 uuid_audio_fork（已知只能捕获 TTS 回声）"""
        ws_url = f"ws://backend:8765/{self._uuid}"
        try:
            result = await self.esl_pool.api(
                f"uuid_audio_fork {self._uuid} start {ws_url} channels=1 sample-rate=8000"
            )
            if result.strip().startswith("+OK"):
                logger.info(f"[{self._uuid[:8]}] uuid_audio_fork(mono) on B-leg: {ws_url}")
                self._audio_started = True
                self._audio_mode = "websocket"
            else:
                logger.warning(f"[{self._uuid[:8]}] B-leg uuid_audio_fork 返回: {result.strip()[:200]}")
        except Exception as e:
            logger.warning(f"[{self._uuid[:8]}] B-leg uuid_audio_fork 失败: {e}")

    async def _start_audio_fork_on_aleg(self, aleg_uuid: str = None):
        """
        在 sofia A-leg 上启动 uuid_audio_fork 捕获用户语音。
        B-leg 的 socket 模式下无法捕获用户语音（read 被 socket 截获）。

        参数:
            aleg_uuid: 如果调用方已经通过 channel_vars 找到了 A-leg UUID，直接传入即可。
                       如果为 None，则通过 uuid_getvar 实时查询。
        """
        if not self.esl_pool or not self.ws_server:
            return False

        a_leg_uuid = aleg_uuid

        # 如果没有提供 aleg_uuid，通过 uuid_getvar 实时查询
        if not a_leg_uuid:
            # 方式 1：通过 uuid_getvar 获取 bridge_partner_uuid_str（bridge 建立后才有值）
            try:
                result = await self.esl_pool.api(
                    f"uuid_getvar {self._uuid} bridge_partner_uuid_str"
                )
                val = result.strip()
                if val and val != "-ERR" and val != "_undef_":
                    a_leg_uuid = val
                    logger.info(f"[{self._uuid[:8]}] 通过 bridge_partner_uuid_str 找到 A-leg: {a_leg_uuid[:8]}")
            except Exception as e:
                logger.debug(f"[{self._uuid[:8]}] bridge_partner_uuid_str 查询失败: {e}")

        # 方式 2：通过 uuid_getvar 获取 signal_bond
        if not a_leg_uuid:
            try:
                result = await self.esl_pool.api(
                    f"uuid_getvar {self._uuid} signal_bond"
                )
                val = result.strip()
                if val and val != "-ERR" and val != "_undef_":
                    a_leg_uuid = val
                    logger.info(f"[{self._uuid[:8]}] 通过 signal_bond 找到 A-leg: {a_leg_uuid[:8]}")
            except Exception as e:
                logger.debug(f"[{self._uuid[:8]}] signal_bond 查询失败: {e}")

        if not a_leg_uuid:
            logger.debug(f"[{self._uuid[:8]}] Bridge 尚未建立，稍后重试")
            return False

        # 存储 A-leg UUID 供后续使用
        self._aleg_uuid = a_leg_uuid
        ws_url = f"ws://backend:8765/{a_leg_uuid}"
        try:
            result = await self.esl_pool.api(
                f"uuid_audio_fork {a_leg_uuid} start {ws_url} mixed 8000"
            )
            if result.strip().startswith("+OK"):
                logger.info(f"[{self._uuid[:8]}] uuid_audio_fork(mixed) on A-leg {a_leg_uuid[:8]}")
                self._audio_started = True
                self._audio_mode = "websocket"
                return True
            else:
                logger.warning(f"[{self._uuid[:8]}] A-leg uuid_audio_fork 返回: {result.strip()[:200]}")
        except Exception as e:
            logger.warning(f"[{self._uuid[:8]}] A-leg uuid_audio_fork 失败: {e}")
        return False

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
        """播放音频。

        bridge(loopback/AI_CALL) 架构下：
        - Outbound socket 在 loopback-b 上
        - 媒体路径：sofia A-leg ↔ loopback-a ↔ loopback-b(socket)

        策略：使用 sofia A-leg UUID 的 uuid_displace（socket 接管后有效）。
        首次播放需等待 socket(async full) 完全接管媒体路径。
        """
        import os
        # 计算播放时长
        try:
            file_size = os.path.getsize(audio_path)
            audio_bytes = max(0, file_size - 44)
            estimated_duration = audio_bytes / 16000.0 if audio_bytes > 0 else 1.0
        except Exception:
            estimated_duration = 3.0

        # 查找 sofia A-leg UUID（用于 uuid_displace 目标）
        # 优先从 _channel_vars 直接获取，避免 uuid_dump 正则匹配失败
        target_uuid = None
        for var in ("other_loopback_from_uuid", "export_origination_uuid", "origination_uuid"):
            val = self._channel_vars.get(var, "")
            if val and val not in ("-ERR", "_undef_"):
                target_uuid = val
                logger.info(f"[{self._uuid}] 从 _channel_vars.{var} 找到 sofia A-leg: {target_uuid[:8]}...")
                break

        # 如果 _channel_vars 没有，再尝试 uuid_dump
        if not target_uuid and self.esl_pool:
            try:
                dump = await self.esl_pool.api(f"uuid_dump {self._uuid}")
                import re
                for var in ("other_loopback_from_uuid", "export_origination_uuid", "origination_uuid"):
                    pattern = rf"Variable: {var}: ([\w-]+)"
                    match = re.search(pattern, dump)
                    if match and match.group(1) not in ("-ERR", "_undef_"):
                        target_uuid = match.group(1)
                        logger.info(f"[{self._uuid}] 从 uuid_dump {var} 找到 sofia A-leg: {target_uuid[:8]}...")
                        break
            except Exception as e:
                logger.debug(f"[{self._uuid}] uuid_dump 查询失败: {e}")

        if not target_uuid:
            target_uuid = self._aleg_uuid or self._uuid

        # 等待 socket(async full) 完全接管媒体路径
        # 首次播放（开场白）需要更长等待，后续播放用较短延迟
        if target_uuid and target_uuid != self._uuid:
            if not hasattr(self, '_play_count'):
                self._play_count = 0
            if self._play_count == 0:
                logger.info(f"[{self._uuid}] 首次播放，等待 socket 接管媒体路径（1秒）...")
                await asyncio.sleep(1)
            else:
                logger.info(f"[{self._uuid}] 等待 socket 接管媒体路径（1秒）...")
                await asyncio.sleep(1)
            self._play_count += 1

        if self.esl_pool and target_uuid and target_uuid != self._uuid:
            # 使用 uuid_displace 替换 sofia A-leg 的写入媒体流
            try:
                result = await self.esl_pool.api(
                    f"uuid_displace {target_uuid} start {audio_path} write"
                )
                logger.info(f"[{self._uuid}] uuid_displace({target_uuid[:8]}, write) 结果: {result.strip()[:100]}")
                await asyncio.sleep(estimated_duration + 0.5)
                # 停止 displace
                try:
                    await self.esl_pool.api(f"uuid_displace {target_uuid} stop")
                except Exception:
                    pass
                return
            except Exception as e:
                logger.warning(f"[{self._uuid}] uuid_displace 失败: {e}，降级 execute")

        # 降级：通过 Outbound socket 的 execute playback
        self._playback_done.clear()
        await self.execute("playback", audio_path)
        try:
            await asyncio.wait_for(self._playback_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[{self._uuid}] 播放超时 ({timeout}s)")
            await self.stop_playback()

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

        桥接架构：sofia A-leg (用户电话) ↔ loopback B-leg (AI socket)

        方案：
          1. 找到 A-leg UUID，查找 A-leg 录音文件
          2. 如果 A-leg 文件不存在，在 A-leg 上启动 uuid_record
          3. 降级：在 B-leg 上启动 uuid_record

        每个调用者获得独立的队列，文件轮询/WebSocket 中继会广播到所有订阅者队列。
        """
        import os

        if not self._uuid:
            logger.warning("无法启动音频捕获: UUID 为空")
            self._audio_mode = "none"
            return asyncio.Queue()

        asr_path = None

        # 方案 1：找到 sofia A-leg UUID（用户语音来源：RTP 从用户电话传入）
        # 优先级：
        #   1. export_origination_uuid — sofia A-leg UUID（通过 originate export 传递）
        #   2. other_loopback_from_uuid — sofia A-leg UUID（loopback 自动设置）
        #   3. origination_uuid — dialplan 中通过 set 恢复
        #   4. pool _aleg_uuids 映射 — 由 _wait_and_start_ai_from_queue 记录
        #   5. other_loopback_leg_uuid — loopback A-leg UUID（备用）
        #   6. uuid_getvar 查询
        aleg_uuid = None
        aleg_type = ""

        # 1. export_origination_uuid = sofia A-leg UUID（最可靠，直接录用户 RTP）
        aleg_uuid = self._channel_vars.get("export_origination_uuid", "")
        if aleg_uuid and aleg_uuid not in ("-ERR", "_undef_"):
            aleg_type = "sofia"
            logger.info(f"[{self._uuid}] 从 export_origination_uuid 找到 sofia A-leg: {aleg_uuid[:8]}...")

        # 1b. other_loopback_from_uuid = sofia A-leg UUID
        if not aleg_uuid:
            aleg_uuid = self._channel_vars.get("other_loopback_from_uuid", "")
            if aleg_uuid and aleg_uuid not in ("-ERR", "_undef_"):
                aleg_type = "sofia"
                logger.info(f"[{self._uuid}] 从 other_loopback_from_uuid 找到 sofia A-leg: {aleg_uuid[:8]}...")

        # 1c. origination_uuid（dialplan 中通过 set 恢复）
        if not aleg_uuid:
            aleg_uuid = self._channel_vars.get("origination_uuid", "")
            if aleg_uuid and aleg_uuid not in ("-ERR", "_undef_"):
                aleg_type = "orig"
                logger.info(f"[{self._uuid}] 从 origination_uuid 找到 A-leg: {aleg_uuid[:8]}...")

        # 2. 通过 pool 映射
        if not aleg_uuid and self.esl_pool:
            for key in ("export_origination_uuid", "origination_uuid"):
                orig_uuid = self._channel_vars.get(key, "")
                if orig_uuid:
                    aleg_uuid = await self.esl_pool.get_aleg_uuid(orig_uuid)
                    if aleg_uuid:
                        aleg_type = f"pool({key})"
                        logger.info(f"[{self._uuid}] 从 pool 映射({key})找到 A-leg: {aleg_uuid[:8]}...")
                        break

        # 3. other_loopback_leg_uuid（loopback A-leg，备用）
        if not aleg_uuid:
            aleg_uuid = self._channel_vars.get("other_loopback_leg_uuid", "")
            if aleg_uuid and aleg_uuid not in ("-ERR", "_undef_"):
                aleg_type = "loopback"
                logger.info(f"[{self._uuid}] 从 other_loopback_leg_uuid 找到 loopback A-leg: {aleg_uuid[:8]}...")

        # 4. 从 channel_vars 查询其他变量
        if not aleg_uuid and self.esl_pool:
            for var in ("asr_aleg_uuid", "bridge_partner_uuid_str", "signal_bond"):
                val = self._channel_vars.get(var, "")
                if val and val not in ("-ERR", "_undef_"):
                    aleg_uuid = val
                    logger.info(f"[{self._uuid}] 从 channel var {var} 找到 A-leg: {aleg_uuid[:8]}...")
                    break

        # 5. 通过 uuid_getvar 查询
        if not aleg_uuid and self.esl_pool:
            for var in ("other_loopback_leg_uuid", "signal_bond", "bridge_partner_uuid_str"):
                try:
                    result = await self.esl_pool.api(f"uuid_getvar {self._uuid} {var}")
                    val = result.strip()
                    if val and val not in ("-ERR", "_undef_"):
                        aleg_uuid = val
                        logger.info(f"[{self._uuid}] 从 uuid_getvar {var} 找到 A-leg: {aleg_uuid[:8]}...")
                        break
                except Exception as e:
                    logger.debug(f"[{self._uuid}] {var} 查询失败: {e}")

        if aleg_uuid:
            self._aleg_uuid = aleg_uuid
            logger.info(f"[{self._uuid}] A-leg UUID: {aleg_uuid[:8]}... (来源: {aleg_type})")
        else:
            logger.debug(f"[{self._uuid}] 未找到 A-leg UUID")

        # ★ 通过 uuid_audio_fork API 在 sofia A-leg 上启动音频采集
        #    这是内部分机外呼的主要路径：
        #    1. 找到 sofia A-leg UUID（other_loopback_from_uuid）
        #    2. 执行 uuid_audio_fork {aleg_uuid} start ws://backend:8765/{call_uuid} mono 8000
        #    3. 等待 WebSocket 连接建立
        #    4. 创建订阅队列，启动 _relay_ws_audio 广播
        #
        #    注意：不能用 connections_active > 0 判断 dialplan audio_fork 是否就绪，
        #    因为该连接可能来自之前的调用或其他来源，无法代表当前通话有音频流。
        logger.info(f"[{self._uuid}] 准备启动 uuid_audio_fork (esl_pool={self.esl_pool is not None}, _audio_started={self._audio_started})")
        if self.esl_pool and not self._audio_started:
            stream_uuid = aleg_uuid or self._aleg_uuid
            if not stream_uuid and self._channel_vars:
                for var in ("other_loopback_from_uuid", "signal_bond"):
                    val = self._channel_vars.get(var, "")
                    if val and val not in ("-ERR", "_undef_"):
                        stream_uuid = val
                        break
            if not stream_uuid:
                stream_uuid = self._uuid
                logger.warning(f"[{self._uuid}] 未找到 A-leg UUID，降级到 B-leg uuid_audio_fork")

            ws_url = f"ws://backend:8765/{stream_uuid}"
            try:
                result = await self.esl_pool.api(
                    f"uuid_audio_fork {stream_uuid} start {ws_url} mono 8000"
                )
                result_stripped = result.strip()
                if result_stripped.startswith("+OK"):
                    logger.info(
                        f"[{self._uuid}] uuid_audio_fork 启动在 {stream_uuid[:8]}: {ws_url}"
                    )
                    # 等待 WebSocket 连接建立（最多等 5 秒）
                    for attempt in range(50):
                        await asyncio.sleep(0.1)
                        if self.ws_server and self.ws_server.stats.get("connections_active", 0) > 0:
                            queue = await self.ws_server.get_session_queue(stream_uuid, timeout=1.0)
                            if queue is not None:
                                # ★ 关键修复：WebSocket 连接后，验证帧内容是否为有效音频
                                #    mod_audio_fork 在 sofia A-leg 上可能只推送静音（ff ff ff），
                                #    需要抽样检查帧内容，不能仅依赖 frame_count > 0
                                logger.info(f"[{self._uuid}] WebSocket 已连接，等待 3 秒验证音频流...")
                                await asyncio.sleep(3.0)

                                ws_stats = self.ws_server.stats
                                conn_count = ws_stats.get("connections_active", 0)
                                frame_count = ws_stats.get("frames_received", 0)

                                if conn_count > 0 and frame_count > 0:
                                    # 从 WebSocket 队列抽样检查帧内容
                                    silent_count = 0
                                    total_checked = 0
                                    for _ in range(min(20, queue.qsize())):
                                        try:
                                            frame = queue.get_nowait()
                                            total_checked += 1
                                            # 计算整帧 RMS：静音帧（ff ff ff = -1 in signed 16-bit）的 RMS≈1
                                            if len(frame) >= 4:
                                                import struct as _struct
                                                samples = [
                                                    s for (s,) in _struct.iter_unpack(
                                                        '<h', frame
                                                    )
                                                ]
                                                rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                                                if rms < 10:  # RMS < 10 视为静音
                                                    silent_count += 1
                                        except Exception:
                                            break

                                    silent_ratio = silent_count / total_checked if total_checked > 0 else 1.0

                                    if silent_ratio < 0.5:
                                        # 超过 50% 帧有有效音频，使用 WebSocket 模式
                                        sub_queue = asyncio.Queue(maxsize=500)
                                        self._audio_subscribers.append(sub_queue)
                                        asyncio.create_task(self._relay_ws_audio(queue))
                                        self._audio_started = True
                                        self._audio_mode = "websocket"
                                        logger.info(
                                            f"[{self._uuid}] WebSocket audio_fork 已连接且有音频流 "
                                            f"(frames={frame_count}, silent_ratio={silent_ratio:.1%})"
                                        )
                                        return sub_queue
                                    else:
                                        # 超过 50% 帧为静音，降级到文件轮询
                                        logger.warning(
                                            f"[{self._uuid}] WebSocket 帧全部为静音 "
                                            f"(silent_ratio={silent_ratio:.1%}, frames={frame_count})，降级到文件轮询"
                                        )
                                        break  # 跳出 WebSocket 等待循环，进入文件轮询
                    logger.warning(f"[{self._uuid}] uuid_audio_fork 启动但 WebSocket 无有效音频流")
                else:
                    logger.warning(f"[{self._uuid}] uuid_audio_fork 返回: {result_stripped[:200]}")
            except Exception as e:
                logger.warning(f"[{self._uuid}] uuid_audio_fork 启动失败: {e}")

        # ★ 文件轮询：使用 sofia A-leg 的 record_session WAV 文件
        #    originate 命令中在 sofia A-leg 上执行了 record_session(/recordings/{call_uuid}.wav)，
        #    录制到 /recordings/{origination_uuid}.wav。
        #    B-leg 通过 export_origination_uuid 获取 call_uuid。

        # 优先使用 export_origination_uuid（originate 命令传递的 call_uuid）
        record_uuid = self._channel_vars.get("export_origination_uuid", "")
        if record_uuid and record_uuid not in ("-ERR", "_undef_"):
            record_path = f"/recordings/{record_uuid}.wav"
            logger.info(f"[{self._uuid}] 使用 export_origination_uuid 录音文件: {record_path}")
        else:
            # 降级：使用 B-leg UUID 的 record_session 文件
            record_uuid = self._uuid
            record_path = f"/recordings/{record_uuid}.wav"
            logger.info(f"[{self._uuid}] 使用 B-leg 录音文件: {record_path}")

        asr_path = record_path
        self._audio_mode = "file_poll"
        self._audio_started = True
        logger.info(f"[{self._uuid}] 音频采集: 文件轮询 {asr_path}")

        # 为当前调用者创建独立的订阅队列
        sub_queue = asyncio.Queue(maxsize=500)
        self._audio_subscribers.append(sub_queue)
        logger.info(f"[{self._uuid}] 音频订阅队列已创建 (总数: {len(self._audio_subscribers)}, 模式: {self._audio_mode})")

        asyncio.create_task(self._poll_audio_file(asr_path))
        return sub_queue

    async def _poll_audio_file(self, path: str):
        """轮询音频文件，读取新增数据送入音频队列

        ★ 关键修复：以实时速率（1x）发送音频帧，不超实时。
        ASR 服务端（百炼/阿里云）期望音频以真实通话速率输入，
        如果发送速度过快，VAD 和句子切分算法无法正确工作。

        每次读取的新数据按 320 bytes（20ms @ 8kHz mono 16bit）分帧广播。
        """
        import os
        import time
        import aiofiles

        # 等待文件创建（FreeSWITCH record_session 创建文件）
        for _ in range(60):  # 最多等 6 秒
            if os.path.exists(path):
                break
            await asyncio.sleep(0.1)
        else:
            logger.warning(f"[{self._uuid}] 音频文件未创建: {path}")
            return

        # WAV 文件需要跳过 44 字节头
        is_wav = path.endswith(".wav")
        header_offset = 44 if is_wav else 0
        offset = header_offset
        if is_wav:
            logger.info(f"[{self._uuid}] 检测到 WAV 文件，跳过 {header_offset} 字节头")

        frame_size = 320  # 20ms @ 8kHz mono 16bit
        frame_interval = 0.020  # 20ms 每帧，实时速率
        total_broadcast = 0
        while self._connected:
            try:
                current_size = os.path.getsize(path)
                if current_size > offset:
                    async with aiofiles.open(path, "rb") as f:
                        await f.seek(offset)
                        data = await f.read(current_size - offset)
                        if data:
                            offset += len(data)
                            total_broadcast += len(data)
                            # 按 320 bytes 分帧，以实时速率广播
                            for i in range(0, len(data), frame_size):
                                frame = data[i:i + frame_size]
                                if len(frame) >= frame_size:
                                    await self._broadcast_audio(frame)
                                    # ★ 实时速率控制：每帧间隔 20ms，不超实时
                                    await asyncio.sleep(frame_interval)
                                else:
                                    # 不足一帧的剩余数据，延迟后发送
                                    await asyncio.sleep(frame_interval)
                                    await self._broadcast_audio(frame)
                else:
                    # 文件暂无新数据，等待下一次轮询
                    await asyncio.sleep(0.02)
            except FileNotFoundError:
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"[{self._uuid}] 音频文件读取失败: {e}")
                await asyncio.sleep(0.1)

        logger.info(f"[{self._uuid}] 文件轮询结束: 共广播 {total_broadcast} bytes, 清理 {len(self._audio_subscribers)} 个订阅者")

        # 清理订阅者
        logger.debug(f"[{self._uuid}] 文件轮询结束，清理 {len(self._audio_subscribers)} 个订阅者")
        self._audio_subscribers.clear()

        # raw PCM 文件才需要转换为 WAV；WAV 文件已经是有效格式
        if not is_wav:
            await self._convert_raw_to_wav(path)

    async def _relay_ws_audio(self, ws_queue: asyncio.Queue):
        """从 WebSocket 队列消费音频帧，送入 ASR 音频队列

        由 uuid_audio_fork 在 sofia A-leg 上启动后，FreeSWITCH 通过
        mod_audio_fork 将 RTP 音频帧推送到 WebSocket 服务器。
        此 task 负责从 ws_queue 取出帧并放入 self._audio_queue。
        """
        frame_count = 0
        try:
            while self._connected:
                try:
                    frame = await asyncio.wait_for(ws_queue.get(), timeout=30.0)
                    frame_count += 1
                    await self._broadcast_audio(frame)
                    if frame_count % 100 == 0:
                        logger.debug(f"[{self._uuid}] 已通过 WebSocket 转发 {frame_count} 帧用户语音")
                except asyncio.TimeoutError:
                    # 30s 没有音频，可能是通话结束或 stream 停止
                    logger.debug(f"[{self._uuid}] WebSocket 音频队列超时，共 {frame_count} 帧")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self._uuid}] WebSocket 音频中继异常: {e}")
        logger.info(f"[{self._uuid}] WebSocket 音频中继结束，共转发 {frame_count} 帧")

    @staticmethod
    async def _convert_raw_to_wav(raw_path: str):
        """将 raw PCM 文件转为 WAV 格式（16bit, 8kHz, mono）"""
        import os
        import wave

        if not raw_path or not raw_path.endswith(".raw") or not os.path.exists(raw_path):
            return

        wav_path = raw_path[:-4] + ".wav"
        try:
            pcm_data = open(raw_path, "rb").read()
            if not pcm_data:
                return
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(8000)
                wf.writeframes(pcm_data)
            duration = len(pcm_data) / 16000  # 8000Hz × 2 bytes = 16000 bytes/s
            logger.info(f"[{os.path.basename(raw_path)}] WAV 转换: {wav_path} ({duration:.1f}s, {len(pcm_data)} bytes)")
        except Exception as e:
            logger.warning(f"[{os.path.basename(raw_path)}] WAV 转换失败: {e}")

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
                        await self._broadcast_audio(pcm)
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

    async def _broadcast_audio(self, chunk: bytes):
        """广播音频帧到所有订阅者队列"""
        if not self._audio_subscribers:
            return
        # 快照订阅者列表（避免遍历时被修改）
        subs = list(self._audio_subscribers)
        full_subs = []
        for q in subs:
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                full_subs.append(q)
        # 队满的订阅者：丢弃最老帧后重试
        for q in full_subs:
            try:
                q.get_nowait()
                q.put_nowait(chunk)
            except (asyncio.QueueFull, asyncio.QueueEmpty):
                pass
        # 清理已断开的订阅者（队列异常）
        self._audio_subscribers = [q for q in subs if q not in full_subs or not q.empty()]

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
