"""LiveKit Egress 服务封装

通过 LiveKit Server API 启动/停止/查询房间录制（Egress），
录制文件输出到 S3 兼容存储（MinIO）。
"""
import asyncio
import logging
import time
from typing import Optional

from google.protobuf.json_format import MessageToDict
from livekit import api as livekit_api

from backend.core.config import settings

logger = logging.getLogger(__name__)


class EgressService:
    """LiveKit Egress 操作封装"""

    def __init__(self) -> None:
        self._client: Optional[livekit_api.LiveKitAPI] = None

    async def initialize(self) -> None:
        """初始化 LiveKit API 客户端。"""
        api_url = settings.livekit_url.replace("ws://", "http://").replace("wss://", "https://")
        self._client = livekit_api.LiveKitAPI(
            url=api_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
        logger.info("LiveKit Egress API 客户端已初始化")

    def _build_s3_endpoint(self) -> str:
        """构建 S3 endpoint URL，确保不重复添加协议前缀。"""
        endpoint_url = settings.minio_endpoint
        if not endpoint_url.startswith(('http://', 'https://')):
            endpoint_url = f"http://{endpoint_url}"
        return endpoint_url

    async def start_room_composite_egress(self, room_name: str, call_id: str) -> str:
        """启动整房间混合录制（RoomCompositeEgress）。

        Args:
            room_name: LiveKit 房间名称
            call_id: 业务通话 ID，用于构建 S3 存储路径

        Returns:
            egress_id: Egress 任务 ID

        Raises:
            RuntimeError: 客户端未初始化或启动录制失败
        """
        if not self._client:
            raise RuntimeError("EgressService 未初始化，请先调用 initialize()")

        timestamp = int(time.time())
        filepath = f"recordings/{call_id}/full_{timestamp}.ogg"

        s3_upload = livekit_api.S3Upload(
            access_key=settings.minio_access_key,
            secret=settings.minio_secret_key,
            region="us-east-1",
            endpoint=self._build_s3_endpoint(),
            bucket=settings.minio_bucket,
            force_path_style=True,
        )

        file_output = livekit_api.EncodedFileOutput(
            file_type=livekit_api.EncodedFileType.OGG,
            filepath=filepath,
            s3=s3_upload,
        )

        req = livekit_api.RoomCompositeEgressRequest(
            room_name=room_name,
            audio_only=True,
        )
        req.file_outputs.append(file_output)

        try:
            logger.info(
                "启动 Egress 录制: room=%s, bucket=%s, endpoint=%s, filepath=%s",
                room_name, settings.minio_bucket, self._build_s3_endpoint(), filepath,
            )
            info = await self._client.egress.start_room_composite_egress(req)
            logger.info(
                "Egress 已启动: egress_id=%s, room=%s, path=%s",
                info.egress_id,
                room_name,
                filepath,
            )
            return info.egress_id
        except Exception as e:
            logger.error("启动 Egress 失败: room=%s, error=%s", room_name, e)
            raise

    async def stop_egress(self, egress_id: str) -> dict:
        """停止指定的 Egress 任务。

        Args:
            egress_id: Egress 任务 ID

        Returns:
            Egress 信息字典

        Raises:
            RuntimeError: 客户端未初始化或停止失败
        """
        if not self._client:
            raise RuntimeError("EgressService 未初始化")

        req = livekit_api.StopEgressRequest(egress_id=egress_id)
        try:
            info = await self._client.egress.stop_egress(req)
            logger.info("Egress 已停止: egress_id=%s, status=%s", egress_id, info.status)
            return MessageToDict(info, preserving_proto_field_name=True)
        except Exception as e:
            logger.error("停止 Egress 失败: egress_id=%s, error=%s", egress_id, e)
            raise

    async def get_egress_info(self, egress_id: str) -> dict:
        """查询 Egress 任务状态。

        Args:
            egress_id: Egress 任务 ID

        Returns:
            包含状态和输出文件信息的字典。
            关键字段:
                - egress_id: str
                - room_name: str
                - status: int (0=STARTING, 1=ACTIVE, 2=ENDING, 3=COMPLETE, 4=FAILED, 5=ABORTED, 6=LIMIT_REACHED)
                - file_results: list[{"filename", "location", "size", "duration"}]
                - error: str

        Raises:
            RuntimeError: 客户端未初始化或查询失败
        """
        if not self._client:
            raise RuntimeError("EgressService 未初始化")

        req = livekit_api.ListEgressRequest(egress_id=egress_id)
        try:
            resp = await self._client.egress.list_egress(req)
            if not resp.items:
                logger.warning("未找到 Egress 任务: egress_id=%s", egress_id)
                return {}

            info = resp.items[0]
            result = MessageToDict(info, preserving_proto_field_name=True)
            logger.debug(
                "Egress 状态查询: egress_id=%s, status=%s",
                egress_id,
                info.status,
            )
            return result
        except Exception as e:
            logger.error("查询 Egress 失败: egress_id=%s, error=%s", egress_id, e)
            raise

    # MessageToDict 将 protobuf 枚举转换为字符串名称，
    # 因此这里必须用字符串形式比较，而非整数枚举值
    _FINAL_STATUS_NAMES = frozenset({
        "EGRESS_COMPLETE",
        "EGRESS_FAILED",
        "EGRESS_ABORTED",
        "EGRESS_LIMIT_REACHED",
    })

    async def wait_for_egress_complete(
        self,
        egress_id: str,
        timeout: int = 60,
        poll_interval: float = 2.0,
    ) -> dict:
        """轮询等待 Egress 任务完成。

        Args:
            egress_id: Egress 任务 ID
            timeout: 最大等待时间（秒），默认 60
            poll_interval: 轮询间隔（秒），默认 2.0

        Returns:
            最终 Egress 信息字典。若超时返回当前状态字典（可能为空）。

        Raises:
            RuntimeError: 客户端未初始化
        """
        if not self._client:
            raise RuntimeError("EgressService 未初始化")

        start_time = time.time()

        while time.time() - start_time < timeout:
            info_dict = await self.get_egress_info(egress_id)
            if not info_dict:
                await asyncio.sleep(poll_interval)
                continue

            status = info_dict.get("status")
            # MessageToDict 返回字符串枚举名 ("EGRESS_COMPLETE") 或整数
            # 统一转换为字符串进行比较
            if isinstance(status, int):
                _INT_TO_NAME = {0: "EGRESS_STARTING", 1: "EGRESS_ACTIVE", 2: "EGRESS_ENDING",
                                3: "EGRESS_COMPLETE", 4: "EGRESS_FAILED", 5: "EGRESS_ABORTED",
                                6: "EGRESS_LIMIT_REACHED"}
                status_str = _INT_TO_NAME.get(status, str(status))
            else:
                status_str = str(status)
            if status_str in self._FINAL_STATUS_NAMES:
                logger.info(
                    "Egress 已完成: egress_id=%s, status=%s, duration=%.1fs",
                    egress_id,
                    status_str,
                    time.time() - start_time,
                )
                return info_dict

            await asyncio.sleep(poll_interval)

        logger.warning(
            "等待 Egress 完成超时: egress_id=%s, timeout=%ds",
            egress_id,
            timeout,
        )
        return await self.get_egress_info(egress_id)

    async def close(self) -> None:
        """关闭 API 客户端连接。"""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("LiveKit Egress API 客户端已关闭")
