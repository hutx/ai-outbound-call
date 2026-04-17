"""
Forkzstream 通话会话 — 替代 ESL Outbound Socket
─────────────────────────────────────────────────
当 dialplan 使用 forkzstream + sleep（无 socket）时，
此模块在 forkzstream WebSocket 连接上创建 CallAgent 兼容的会话接口。

架构：
  forkzstream WS 连接 → ForkzstreamCallSession → CallAgent.run()
  - ASR 音频：从 forkzstream WebSocket 接收 → asyncio.Queue
  - TTS 音频：CallAgent → play_stream() → forkzstream.send_audio()
  - 通话控制：通过 ESL Inbound pool 的 uuid_* API
"""
import asyncio
import logging
import os
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


class ForkzstreamCallSession:
    """forkzstream 模式下的通话会话，兼容 ESLSocketCallSession 接口。

    与 ESL socket 的区别：
    - 没有 ESL Outbound 连接，通话控制通过 ESL Inbound pool API
    - channel_vars 从 originate 时传递的变量 + uuid_dump 获取
    - 音频流直接来自 forkzstream WebSocket queue
    - TTS 通过 forkzstream WebSocket 发送二进制帧
    """

    def __init__(
        self,
        call_uuid: str,
        phone: str,
        task_id: str,
        script_id: str,
        forkzstream_ws_server,
        esl_pool=None,
    ):
        self._uuid = call_uuid
        self._phone = phone
        self._task_id = task_id
        self._script_id = script_id
        self._channel_vars: dict = {
            "task_id": task_id,
            "script_id": script_id,
            "ai_agent": "true",
        }
        self._connected = True
        self._hangup_cause: Optional[str] = None
        self._sip_code: Optional[int] = None
        self._aleg_uuid: Optional[str] = None

        self.forkzstream_ws_server = forkzstream_ws_server
        self.esl_pool = esl_pool
        self.ws_server = None  # 兼容旧接口，不用

        # 音频状态
        self._audio_started = False
        self._audio_mode = "forkzstream"
        self._audio_queue: Optional[asyncio.Queue] = None
        self._audio_subscribers: list[asyncio.Queue] = []
        self._audio_relay_task: Optional[asyncio.Task] = None
        self._play_count = 0

    @property
    def uuid(self) -> Optional[str]:
        return self._uuid

    @property
    def channel_vars(self) -> dict:
        return self._channel_vars

    async def connect(self) -> dict:
        """完成握手，填充 channel 变量。"""
        # 通过 ESL Inbound pool 获取完整 channel 信息
        if self.esl_pool:
            try:
                dump = await self.esl_pool.api(f"uuid_dump {self._uuid}")
                import re
                for var in (
                    "other_loopback_from_uuid", "export_origination_uuid",
                    "origination_uuid", "signal_bond", "other_loopback_leg_uuid",
                    "bridge_partner_uuid_str", "callee_number",
                ):
                    pattern = rf"Variable: {var}: ([\w-]+)"
                    match = re.search(pattern, dump)
                    if match and match.group(1) not in ("-ERR", "_undef_"):
                        self._channel_vars[var] = match.group(1)
                        logger.debug(f"[{self._uuid}] {var}={match.group(1)[:8]}...")

                # 如果 dialplan 设置了 callee_number，补充 phone_number
                if not self._phone:
                    self._phone = self._channel_vars.get("callee_number", "")
            except Exception as e:
                logger.debug(f"[{self._uuid}] uuid_dump 查询失败: {e}")

        logger.info(
            f"[{self._uuid}] ForkzstreamCallSession 握手: "
            f"phone={self._phone} task={self._task_id} script={self._script_id}"
        )
        return self._channel_vars

    async def start_audio_capture(self) -> asyncio.Queue:
        """获取 forkzstream ASR 音频队列。"""
        if self._audio_started:
            sub_queue = asyncio.Queue(maxsize=500)
            self._audio_subscribers.append(sub_queue)
            return sub_queue

        # 从 forkzstream WebSocket 获取 ASR 队列
        fz_queue = await self.forkzstream_ws_server.get_session_queue(
            self._uuid, timeout=10.0
        )
        if fz_queue is None:
            logger.warning(f"[{self._uuid}] forkzstream 音频队列不可用")
            return asyncio.Queue()

        self._audio_queue = fz_queue
        self._audio_started = True

        # 创建订阅队列
        sub_queue = asyncio.Queue(maxsize=500)
        self._audio_subscribers.append(sub_queue)

        # 启动中继任务：forkzstream queue → 所有订阅者 queue
        self._audio_relay_task = asyncio.create_task(
            self._relay_forkzstream_audio(fz_queue)
        )
        logger.info(f"[{self._uuid}] forkzstream 音频采集已启动")
        return sub_queue

    async def _relay_forkzstream_audio(self, source_queue: asyncio.Queue):
        """将 forkzstream ASR 音频广播到所有订阅者队列。"""
        try:
            while self._connected:
                try:
                    chunk = await asyncio.wait_for(source_queue.get(), timeout=30.0)
                    for q in self._audio_subscribers:
                        if not q.full():
                            try:
                                q.put_nowait(chunk)
                            except asyncio.QueueFull:
                                pass
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[{self._uuid}] forkzstream 音频中继异常: {e}")

    async def play_stream(self, audio_chunks, text: str = "", timeout: float = 60.0):
        """流式 TTS：收集全部 PCM → 写 WAV → uuid_broadcast 到 sofia A-leg。

        forkzstream 的 z_play_thread 在 bridge 架构下无法正确将音频送到用户侧
        （loopback B-leg write_frame 通过 bridge 转发有定时/缓冲冲突），
        因此 TTS 降级为 uuid_broadcast（FreeSWITCH 原生文件播放引擎，流畅无卡顿）。
        """
        from backend.utils.audio import write_wav

        shared_dir = os.environ.get("FS_RECORDING_PATH", "/recordings")
        os.makedirs(shared_dir, exist_ok=True)

        # 收集全部 TTS chunk
        all_audio = b""
        total_bytes = 0
        chunk_count = 0
        async for chunk in audio_chunks:
            if chunk:
                all_audio += chunk
                total_bytes += len(chunk)
                chunk_count += 1

        logger.info(f"[{self._uuid}] TTS 收集完成: {chunk_count} 块, {total_bytes} 字节")

        if total_bytes == 0:
            logger.warning(f"[{self._uuid}] TTS 收到空音频")
            return

        # 保存为 WAV 文件
        if not hasattr(self, '_debug_tts_counter'):
            self._debug_tts_counter = 0
        self._debug_tts_counter += 1
        wav_path = os.path.join(shared_dir, f"tts_fz_{self._uuid}_{self._debug_tts_counter}.wav")
        try:
            write_wav(wav_path, all_audio, sample_rate=8000)
            logger.info(f"[{self._uuid}] TTS WAV 已保存: {wav_path}")
        except Exception as e:
            logger.warning(f"[{self._uuid}] 保存 WAV 失败: {e}")
            return

        # 查找 sofia A-leg UUID（用户电话侧）
        target_uuid = None
        for var in ("other_loopback_from_uuid", "export_origination_uuid", "origination_uuid"):
            val = self._channel_vars.get(var, "")
            if val and val not in ("-ERR", "_undef_"):
                target_uuid = val
                logger.info(f"[{self._uuid}] 从 _channel_vars.{var} 找到 sofia A-leg: {target_uuid[:8]}...")
                break

        if not target_uuid:
            target_uuid = self._aleg_uuid or self._uuid
            logger.info(f"[{self._uuid}] TTS 目标 UUID 使用 fallback: {target_uuid[:8]}...")

        est_duration = total_bytes / 16000.0

        # 通过 ESL Inbound pool 执行 uuid_broadcast
        if self.esl_pool and target_uuid:
            try:
                result = await self.esl_pool.api(
                    f"uuid_broadcast {target_uuid} {wav_path} aleg"
                )
                logger.info(f"[{self._uuid}] uuid_broadcast({target_uuid[:8]}) 结果: {result.strip()[:100]}")
                # 等待播放完成
                await asyncio.sleep(max(est_duration + 0.3, 0.5))
                return
            except Exception as e:
                logger.warning(f"[{self._uuid}] uuid_broadcast 失败: {e}")

        # 降级：通过 forkzstream 发送（已知可能有卡顿问题）
        logger.warning(f"[{self._uuid}] uuid_broadcast 不可用，降级使用 forkzstream TTS")
        await self.forkzstream_ws_server.send_command(self._uuid, "ttsstart")
        sent = await self.forkzstream_ws_server.send_audio(self._uuid, all_audio)
        if sent:
            await asyncio.sleep(min(est_duration + 0.5, timeout))

    async def stop_playback(self):
        """打断 TTS 播放。"""
        await self.forkzstream_ws_server.send_command(self._uuid, "ttsstop_clean")

    async def execute(self, app: str, arg: str = "", lock: bool = True) -> str:
        """通过 ESL Inbound pool 执行 FreeSWITCH API。"""
        if not self._connected:
            raise Exception("通话已结束")
        if not self.esl_pool:
            logger.warning(f"[{self._uuid}] ESL pool 不可用，execute({app}) 跳过")
            return ""
        try:
            result = await self.esl_pool.api(f"uuid_execute {self._uuid} {app} {arg}")
            return result.strip()
        except Exception as e:
            logger.warning(f"[{self._uuid}] execute({app}) 失败: {e}")
            return ""

    async def set_variable(self, name: str, value: str):
        """设置 channel 变量。"""
        if self.esl_pool:
            try:
                await self.esl_pool.api(f"uuid_setvar {self._uuid} {name} {value}")
                self._channel_vars[name] = value
            except Exception as e:
                logger.debug(f"[{self._uuid}] set_variable({name}) 失败: {e}")

    async def hangup(self, cause: str = "NORMAL_CLEARING"):
        """挂断通话。"""
        if not self._connected:
            return
        self._connected = False
        if self.esl_pool:
            try:
                await self.esl_pool.api(f"uuid_kill {self._uuid} {cause}")
                logger.info(f"[{self._uuid}] 已发送挂断命令: {cause}")
            except Exception as e:
                logger.warning(f"[{self._uuid}] 挂断失败: {e}")

    async def transfer_to_human(self, extension: str = "8001"):
        """转接人工坐席。"""
        if self.esl_pool:
            try:
                await self.esl_pool.api(
                    f"uuid_transfer {self._uuid} {extension} XML agents"
                )
                logger.info(f"[{self._uuid}] 转接人工 {extension}")
            except Exception as e:
                logger.warning(f"[{self._uuid}] 转接失败: {e}")
        self._connected = False

    async def read_events(self):
        """空实现 — forkzstream 模式下没有 ESL 事件流。"""
        # 保持一个长时间运行的空任务，避免 CallAgent 报错
        try:
            while self._connected:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
