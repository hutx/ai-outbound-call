"""话术数据模型

定义话术相关的 Pydantic 请求/响应模型，对应 lk_scripts 表。
"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class ScriptBase(BaseModel):
    """话术基础字段"""

    name: str = Field(..., max_length=128, description="话术名称")
    description: str = Field("", description="描述")
    script_type: str = Field("general", max_length=32, description="话术类型")

    # 开场白/结束语
    opening_text: str = Field("", description="开场白文本")
    opening_pause_ms: int = Field(2000, ge=0, le=10000, description="开场白后停顿(ms)")
    main_prompt: str = Field(..., description="LLM 系统提示（主话术）")
    closing_text: str = Field("感谢您的接听，再见！", description="结束语")

    # 打断配置
    barge_in_opening: bool = Field(False, description="开场白允许打断")
    barge_in_conversation: bool = Field(True, description="对话中允许打断")
    barge_in_closing: bool = Field(False, description="结束语允许打断")
    barge_in_protect_start_sec: float = Field(1.0, ge=0, le=10, description="打断保护起始秒")
    barge_in_protect_end_sec: float = Field(1.0, ge=0, le=10, description="打断保护结束秒")

    # 宽容期配置
    tolerance_enabled: bool = Field(True, description="宽容期开关")
    tolerance_ms: int = Field(1000, ge=0, le=5000, description="宽容期毫秒")

    # 无响应配置
    no_response_timeout_sec: int = Field(5, ge=1, le=30, description="无响应超时秒")
    no_response_mode: str = Field(
        "consecutive",
        pattern=r"^(consecutive|cumulative)$",
        description="无响应模式",
    )
    no_response_max_count: int = Field(3, ge=1, le=10, description="最大无响应次数")
    no_response_prompt: str = Field("您好，请问您还在吗？", description="追问话术")
    no_response_hangup_text: str = Field("感谢您的时间，再见！", description="挂断语")


class ScriptCreate(ScriptBase):
    """创建话术请求"""

    script_id: str = Field(
        ..., max_length=64, pattern=r"^[a-zA-Z0-9_-]+$", description="话术唯一标识"
    )


class ScriptUpdate(BaseModel):
    """更新话术请求 — 所有字段可选"""

    name: Optional[str] = Field(None, max_length=128)
    description: Optional[str] = None
    script_type: Optional[str] = Field(None, max_length=32)
    opening_text: Optional[str] = None
    opening_pause_ms: Optional[int] = Field(None, ge=0, le=10000)
    main_prompt: Optional[str] = None
    closing_text: Optional[str] = None
    barge_in_opening: Optional[bool] = None
    barge_in_conversation: Optional[bool] = None
    barge_in_closing: Optional[bool] = None
    barge_in_protect_start_sec: Optional[float] = Field(None, ge=0, le=10)
    barge_in_protect_end_sec: Optional[float] = Field(None, ge=0, le=10)
    tolerance_enabled: Optional[bool] = None
    tolerance_ms: Optional[int] = Field(None, ge=0, le=5000)
    no_response_timeout_sec: Optional[int] = Field(None, ge=1, le=30)
    no_response_mode: Optional[str] = Field(None, pattern=r"^(consecutive|cumulative)$")
    no_response_max_count: Optional[int] = Field(None, ge=1, le=10)
    no_response_prompt: Optional[str] = None
    no_response_hangup_text: Optional[str] = None
    is_active: Optional[bool] = None


class ScriptResponse(ScriptBase):
    """话术响应"""

    id: int
    script_id: str
    is_active: bool = True
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
