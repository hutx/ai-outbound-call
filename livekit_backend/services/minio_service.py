"""MinIO 对象存储客户端封装

提供 bucket 管理、文件上传/下载/删除及预签名 URL 生成。
"""
import logging
import os
from datetime import timedelta
from io import BytesIO
from typing import Optional, Union

from minio import Minio
from minio.error import S3Error

from livekit_backend.core.config import settings

logger = logging.getLogger(__name__)


class MinioService:
    """MinIO 服务封装"""

    def __init__(self) -> None:
        self._client: Optional[Minio] = None

    def _get_client(self) -> Minio:
        """获取或创建 MinIO 客户端"""
        if self._client is None:
            self._client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
            )
            logger.info(
                "MinIO 客户端已初始化: endpoint=%s, secure=%s",
                settings.minio_endpoint,
                settings.minio_secure,
            )
        return self._client

    async def ensure_bucket(self, bucket: Optional[str] = None) -> None:
        """确保 bucket 存在，不存在则创建

        Args:
            bucket: bucket 名称，默认使用配置中的 bucket
        """
        bucket_name = bucket or settings.minio_bucket
        client = self._get_client()
        try:
            if not client.bucket_exists(bucket_name):
                client.make_bucket(bucket_name)
                logger.info("MinIO bucket 已创建: %s", bucket_name)
            else:
                logger.debug("MinIO bucket 已存在: %s", bucket_name)
        except S3Error as e:
            logger.error("MinIO bucket 操作失败: %s, error=%s", bucket_name, e)
            raise

    async def upload_file(
        self,
        bucket: str,
        object_name: str,
        file_path_or_data: Union[str, bytes],
        content_type: str = "application/octet-stream",
    ) -> str:
        """上传文件到 MinIO

        Args:
            bucket: 存储桶名称
            object_name: 对象名称（路径）
            file_path_or_data: 本地文件路径或字节数据
            content_type: MIME 类型

        Returns:
            object_name
        """
        client = self._get_client()
        try:
            if isinstance(file_path_or_data, str):
                client.fput_object(
                    bucket_name=bucket,
                    object_name=object_name,
                    file_path=file_path_or_data,
                    content_type=content_type,
                )
            else:
                data = BytesIO(file_path_or_data)
                client.put_object(
                    bucket_name=bucket,
                    object_name=object_name,
                    data=data,
                    length=len(file_path_or_data),
                    content_type=content_type,
                )
            logger.info(
                "文件已上传: bucket=%s, object_name=%s, content_type=%s",
                bucket,
                object_name,
                content_type,
            )
            return object_name
        except S3Error as e:
            logger.error(
                "文件上传失败: bucket=%s, object_name=%s, error=%s",
                bucket,
                object_name,
                e,
            )
            raise

    async def upload_bytes(
        self,
        bucket: str,
        object_name: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """上传字节数据到 MinIO

        Args:
            bucket: 存储桶名称
            object_name: 对象名称（路径）
            data: 字节数据
            content_type: MIME 类型

        Returns:
            object_name
        """
        return await self.upload_file(bucket, object_name, data, content_type)

    async def get_presigned_url(
        self,
        bucket: str,
        object_name: str,
        expires: int = 3600,
    ) -> str:
        """获取预签名下载 URL

        Args:
            bucket: 存储桶名称
            object_name: 对象名称
            expires: URL 有效期（秒），默认 1 小时

        Returns:
            预签名 URL
        """
        client = self._get_client()
        try:
            url = client.presigned_get_object(
                bucket_name=bucket,
                object_name=object_name,
                expires=timedelta(seconds=expires),
            )
            logger.debug(
                "预签名 URL 已生成: bucket=%s, object_name=%s, expires=%ds",
                bucket,
                object_name,
                expires,
            )
            return url
        except S3Error as e:
            logger.error(
                "预签名 URL 生成失败: bucket=%s, object_name=%s, error=%s",
                bucket,
                object_name,
                e,
            )
            raise

    async def delete_file(self, bucket: str, object_name: str) -> None:
        """删除 MinIO 中的文件

        Args:
            bucket: 存储桶名称
            object_name: 对象名称
        """
        client = self._get_client()
        try:
            client.remove_object(bucket, object_name)
            logger.info(
                "文件已删除: bucket=%s, object_name=%s",
                bucket,
                object_name,
            )
        except S3Error as e:
            logger.error(
                "文件删除失败: bucket=%s, object_name=%s, error=%s",
                bucket,
                object_name,
                e,
            )
            raise


# 模块级单例
minio_service = MinioService()

