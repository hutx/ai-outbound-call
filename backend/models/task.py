"""外呼任务数据模型

定义任务相关的 Pydantic 请求/响应模型，对应 lk_tasks / lk_task_phones 表。
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class TaskCreate(BaseModel):
    """创建任务请求"""

    name: str = Field(..., max_length=128, description="任务名称")
    script_id: str = Field(..., description="关联话术ID")
    phone_numbers: List[str] = Field(..., min_length=1, description="外呼号码列表")
    concurrent_limit: int = Field(5, ge=1, le=50, description="并发限制")
    max_retries: int = Field(1, ge=0, le=5, description="最大重试次数")


class TaskResponse(BaseModel):
    """任务响应"""

    id: int
    task_id: str
    name: str
    script_id: str
    status: str
    concurrent_limit: int
    max_retries: int
    total_phones: int
    completed_count: int
    success_count: int
    failed_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TaskStats(BaseModel):
    """任务统计"""

    task_id: str
    total: int
    pending: int
    calling: int
    completed: int
    failed: int
    success_rate: float
    avg_duration_sec: float


class TaskDetail(TaskResponse):
    """任务详情（含统计）"""

    stats: Optional[TaskStats] = None
