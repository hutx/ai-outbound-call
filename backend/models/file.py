"""文件信息数据模型

定义文件记录相关的 Pydantic 模型，对应 lk_files 表。
"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class FileRecord(BaseModel):
    """文件信息"""

    id: Optional[int] = Field(None, description="主键ID")
    file_id: str = Field(..., max_length=128, description="文件唯一标识")
    call_id: Optional[str] = Field(None, max_length=128, description="关联通话ID")
    file_type: str = Field(..., max_length=32, description="文件类型: full_recording | user_audio | ai_audio")
    storage_path: str = Field(..., description="存储路径")
    storage_bucket: Optional[str] = Field(None, max_length=128, description="存储桶")
    file_name: Optional[str] = Field(None, max_length=256, description="文件名")
    mime_type: Optional[str] = Field(None, max_length=64, description="MIME类型")
    file_size_bytes: int = Field(0, ge=0, description="文件大小（字节）")
    duration_sec: float = Field(0.0, ge=0, description="时长（秒）")
    sample_rate: Optional[int] = Field(None, description="采样率")
    download_url: Optional[str] = Field(None, description="下载URL")
    egress_id: Optional[str] = Field(None, max_length=128, description="LiveKit Egress ID")
    created_at: Optional[datetime] = Field(None, description="创建时间")

    model_config = {"from_attributes": True}


class FileRecordCreate(BaseModel):
    """创建文件记录"""

    file_id: str = Field(..., max_length=128, description="文件唯一标识")
    call_id: Optional[str] = Field(None, max_length=128, description="关联通话ID")
    file_type: str = Field(..., max_length=32, description="文件类型: full_recording | user_audio | ai_audio")
    storage_path: str = Field(..., description="存储路径")
    storage_bucket: Optional[str] = Field(None, max_length=128, description="存储桶")
    file_name: Optional[str] = Field(None, max_length=256, description="文件名")
    mime_type: Optional[str] = Field(None, max_length=64, description="MIME类型")
    file_size_bytes: int = Field(0, ge=0, description="文件大小（字节）")
    duration_sec: float = Field(0.0, ge=0, description="时长（秒）")
    sample_rate: Optional[int] = Field(None, description="采样率")
    egress_id: Optional[str] = Field(None, max_length=128, description="LiveKit Egress ID")
