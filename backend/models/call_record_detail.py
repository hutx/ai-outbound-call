"""通话记录详情数据模型（问答形式）

定义每轮问答记录的 Pydantic 模型，对应 lk_call_record_details 表。
"""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime


class CallRecordDetail(BaseModel):
    """通话记录详情"""

    id: Optional[int] = Field(None, description="主键ID")
    call_record_id: int = Field(..., description="关联通话记录ID")
    call_id: str = Field(..., max_length=128, description="通话唯一标识")
    round_num: int = Field(..., ge=1, description="对话轮次")
    question: Optional[str] = Field(None, description="用户提问文本")
    question_audio_file_id: Optional[str] = Field(None, max_length=128, description="用户提问音频文件ID")
    question_duration_sec: float = Field(0.0, ge=0, description="用户提问时长（秒）")
    answer_content: Optional[str] = Field(None, description="AI回答文本")
    answer_audio_file_id: Optional[str] = Field(None, max_length=128, description="AI回答音频文件ID")
    answer_duration_sec: float = Field(0.0, ge=0, description="AI回答时长（秒）")
    is_interrupted: bool = Field(False, description="是否被打断")
    interrupted_at_sec: Optional[float] = Field(None, ge=0, description="打断时间点（秒）")
    start_time: Optional[datetime] = Field(None, description="轮次开始时间")
    end_time: Optional[datetime] = Field(None, description="轮次结束时间")
    stt_latency_ms: Optional[int] = Field(None, ge=0, description="STT延迟（毫秒）")
    llm_latency_ms: Optional[int] = Field(None, ge=0, description="LLM延迟（毫秒）")
    tts_latency_ms: Optional[int] = Field(None, ge=0, description="TTS延迟（毫秒）")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="扩展元数据")
    created_at: Optional[datetime] = Field(None, description="创建时间")

    model_config = {"from_attributes": True}


class CallRecordDetailCreate(BaseModel):
    """创建通话记录详情"""

    call_record_id: int = Field(..., description="关联通话记录ID")
    call_id: str = Field(..., max_length=128, description="通话唯一标识")
    round_num: int = Field(..., ge=1, description="对话轮次")
    question: Optional[str] = Field(None, description="用户提问文本")
    question_audio_file_id: Optional[str] = Field(None, max_length=128, description="用户提问音频文件ID")
    question_duration_sec: float = Field(0.0, ge=0, description="用户提问时长（秒）")
    answer_content: Optional[str] = Field(None, description="AI回答文本")
    answer_audio_file_id: Optional[str] = Field(None, max_length=128, description="AI回答音频文件ID")
    answer_duration_sec: float = Field(0.0, ge=0, description="AI回答时长（秒）")
    is_interrupted: bool = Field(False, description="是否被打断")
    interrupted_at_sec: Optional[float] = Field(None, ge=0, description="打断时间点（秒）")
    start_time: Optional[datetime] = Field(None, description="轮次开始时间")
    end_time: Optional[datetime] = Field(None, description="轮次结束时间")
    stt_latency_ms: Optional[int] = Field(None, ge=0, description="STT延迟（毫秒）")
    llm_latency_ms: Optional[int] = Field(None, ge=0, description="LLM延迟（毫秒）")
    tts_latency_ms: Optional[int] = Field(None, ge=0, description="TTS延迟（毫秒）")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="扩展元数据")
