"""LiveKit SIP API 封装

通过 LiveKit Server API 创建 Room + SIP Participant 发起外呼，
Agent Worker 会监听 Room 加入事件并自动接管对话。
"""
import json
import logging
import uuid
from typing import Optional

from livekit import api as livekit_api
from livekit.api.twirp_client import TwirpError

from livekit_backend.core.config import settings

logger = logging.getLogger(__name__)


class SipCallError(Exception):
    """SIP 外呼失败异常

    当 Room 已创建但 SIP 呼叫失败时抛出，
    携带 call_id 以便调用方更新 CDR。
    """

    def __init__(self, call_id: str, message: str, *, sip_code: int = None, hangup_cause: str = None):
        super().__init__(message)
        self.call_id = call_id
        self.sip_code = sip_code
        self.hangup_cause = hangup_cause


class SipService:
    """LiveKit SIP 操作封装"""

    def __init__(self) -> None:
        self._client: Optional[livekit_api.LiveKitAPI] = None

    async def initialize(self) -> None:
        """初始化 LiveKit API 客户端，并自动发现 SIP Trunk ID（如未配置）。"""
        # LiveKitAPI 需要 HTTP URL，将 ws:// 转换为 http://
        api_url = settings.livekit_url.replace("ws://", "http://").replace("wss://", "https://")
        self._client = livekit_api.LiveKitAPI(
            url=api_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
        logger.info("LiveKit API 客户端已初始化")

        # 如果 SIP Trunk ID 未配置，自动从 LiveKit Server 获取
        if not settings.sip_trunk_id:
            await self._auto_discover_trunk_id()

    async def _auto_discover_trunk_id(self) -> None:
        """自动发现 Outbound SIP Trunk ID 并写入 settings。"""
        if not self._client:
            return
        try:
            resp = await self._client.sip.list_sip_outbound_trunk(
                livekit_api.ListSIPOutboundTrunkRequest()
            )
            if resp.items:
                trunk = resp.items[0]
                settings.sip_trunk_id = trunk.sip_trunk_id
                logger.info(
                    "自动发现 SIP Trunk: name=%s, id=%s",
                    trunk.name,
                    trunk.sip_trunk_id,
                )
            else:
                logger.warning(
                    "未找到 Outbound SIP Trunk，请运行 "
                    "'python -m livekit_backend.scripts.init_sip_trunk' 创建"
                )
        except Exception as e:
            logger.warning("自动发现 SIP Trunk 失败: %s", e)

    async def create_outbound_call(
        self,
        phone: str,
        script_id: str,
        task_id: str,
    ) -> dict:
        """发起外呼

        1. 创建 LiveKit Room（metadata 携带 script_id/task_id/phone）
        2. 创建 SIP Participant，通过 SIP Trunk 呼叫电话号码
        Agent Worker 监听 Room 事件后自动加入并处理对话。

        Args:
            phone: 被叫号码
            script_id: 话术ID
            task_id: 任务ID

        Returns:
            {"call_id": room_name, "sip_participant_id": ...}

        Raises:
            RuntimeError: 客户端未初始化或 SIP 呼叫失败
        """
        if not self._client:
            raise RuntimeError("SipService 未初始化，请先调用 initialize()")

        call_id = f"call_{uuid.uuid4().hex[:12]}"

        # 创建 Room，metadata 传递业务参数供 Agent Worker 使用
        metadata = json.dumps({
            "script_id": script_id,
            "task_id": task_id,
            "phone": phone,
        })

        room = await self._client.room.create_room(
            livekit_api.CreateRoomRequest(
                name=call_id,
                metadata=metadata,
                empty_timeout=30,   # 空房间 30s 后关闭
                max_participants=3,  # Agent + SIP Participant + 可能的转接
            )
        )
        logger.info(f"Room 已创建: {call_id}")

        # 创建 SIP 参与者（发起电话呼叫）
        # wait_until_answered=True：API 调用阻塞直到被叫接听才返回；
        # 若振铃超时或对端拒接，会抛出 TwirpError（携带 SIP 状态码）。
        # 这样 Agent 侧等到 SIP 参与者加入时已是接听状态，不会在振铃阶段播放开场白。
        from google.protobuf import duration_pb2
        ringing_timeout_proto = duration_pb2.Duration()
        ringing_timeout_proto.FromSeconds(settings.originate_timeout_sec)

        try:
            sip_participant = await self._client.sip.create_sip_participant(
                livekit_api.CreateSIPParticipantRequest(
                    room_name=call_id,
                    sip_trunk_id=settings.sip_trunk_id,
                    sip_call_to=phone,
                    participant_identity=f"sip_{phone}",
                    participant_name=f"Phone {phone}",
                    play_dialtone=False,
                    wait_until_answered=True,
                    ringing_timeout=ringing_timeout_proto,
                ),
                timeout=settings.originate_timeout_sec + 5,  # HTTP 超时略长于振铃超时
            )
        except Exception as sip_err:
            # Room 已创建但 SIP 呼叫失败（未接听/忙线/拒接等），
            # 尝试提取 SIP 状态码并封装为 SipCallError
            sip_code = None
            hangup_cause = None
            meta = {}
            if isinstance(sip_err, TwirpError):
                meta = sip_err.metadata or {}
                sip_status_code_str = meta.get("sip_status_code", "")
                if sip_status_code_str:
                    try:
                        sip_code = int(sip_status_code_str)
                    except ValueError:
                        pass
                hangup_cause = meta.get("sip_status", "")[:64] or None
            logger.warning(
                f"SIP 呼叫失败: call_id={call_id}, phone={phone}, "
                f"error={sip_err}, sip_code={sip_code}, hangup_cause={hangup_cause}"
            )
            # 清理：删除已创建的空 Room，避免 Agent Worker 空等
            try:
                await self._client.room.delete_room(
                    livekit_api.DeleteRoomRequest(room=call_id)
                )
            except Exception:
                pass
            raise SipCallError(
                call_id, str(sip_err),
                sip_code=sip_code, hangup_cause=hangup_cause,
            ) from sip_err

        logger.info(f"外呼已发起（已接听）: call_id={call_id}, phone={phone}")
        return {
            "call_id": call_id,
            "sip_participant_id": sip_participant.participant_identity,
            "room_sid": room.sid,
        }

    async def hangup_call(self, call_id: str) -> None:
        """挂断通话（删除 Room，所有参与者断开）"""
        if not self._client:
            raise RuntimeError("SipService 未初始化")

        try:
            await self._client.room.delete_room(
                livekit_api.DeleteRoomRequest(room=call_id)
            )
            logger.info(f"通话已挂断: {call_id}")
        except Exception as e:
            logger.error(f"挂断失败: {call_id}, error={e}")
            raise

    async def close(self) -> None:
        """关闭 API 客户端连接"""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("LiveKit API 客户端已关闭")
