"""通话记录数据模型

定义 CDR 相关的 Pydantic 请求/响应模型，对应 lk_call_records 表。
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class CallRecordCreate(BaseModel):
    """创建通话记录"""

    call_id: str = Field(..., max_length=128, description="通话唯一标识")
    task_id: Optional[str] = Field(None, max_length=64, description="关联任务ID")
    phone: str = Field(..., max_length=32, description="被叫号码")
    script_id: str = Field(..., max_length=64, description="话术ID")


class TranscriptEntry(BaseModel):
    """对话文本条目"""

    role: str = Field(..., description="角色: user | ai")
    text: str = Field(..., description="文本内容")
    timestamp: float = Field(..., description="相对时间（秒）")
    duration_sec: float = Field(0, ge=0, description="说话时长（秒）")


class CallRecordUpdate(BaseModel):
    """更新通话记录"""

    status: Optional[str] = Field(None, max_length=32, description="通话状态")
    intent: Optional[str] = Field(None, max_length=32, description="用户意向")
    result: Optional[str] = Field(None, max_length=32, description="通话结果")
    duration_sec: Optional[float] = Field(None, ge=0, description="通话时长（秒）")
    user_talk_time_sec: Optional[float] = Field(None, ge=0, description="用户说话时长")
    ai_talk_time_sec: Optional[float] = Field(None, ge=0, description="AI说话时长")
    rounds: Optional[int] = Field(None, ge=0, description="对话轮次")
    transcript: Optional[List[TranscriptEntry]] = Field(None, description="对话记录")
    sip_code: Optional[int] = Field(None, description="SIP 响应码")
    hangup_cause: Optional[str] = Field(None, max_length=64, description="挂断原因")
    recording_url: Optional[str] = Field(None, description="录音URL")
    recording_file_id: Optional[str] = Field(None, max_length=128, description="录音文件ID")
    egress_id: Optional[str] = Field(None, max_length=128, description="LiveKit Egress ID")
    total_duration_sec: Optional[float] = Field(None, ge=0, description="总时长（秒）")
    answered_at: Optional[datetime] = Field(None, description="接听时间")
    ended_at: Optional[datetime] = Field(None, description="结束时间")


class CallRecordResponse(BaseModel):
    """通话记录响应"""

    id: int
    call_id: str
    task_id: Optional[str]
    phone: str
    script_id: str
    status: str
    intent: str
    result: str
    duration_sec: float
    user_talk_time_sec: float
    ai_talk_time_sec: float
    rounds: int
    transcript: List[Dict[str, Any]]
    sip_code: Optional[int]
    hangup_cause: Optional[str]
    recording_url: Optional[str]
    recording_file_id: Optional[str]
    egress_id: Optional[str]
    total_duration_sec: Optional[float]
    started_at: Optional[datetime]
    answered_at: Optional[datetime]
    ended_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}
