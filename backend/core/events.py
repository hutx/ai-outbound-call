"""事件定义"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from datetime import datetime


class CallState(str, Enum):
    """通话状态"""
    INITIATING = "initiating"
    RINGING = "ringing"
    CONNECTED = "connected"
    AI_SPEAKING = "ai_speaking"
    USER_SPEAKING = "user_speaking"
    PROCESSING = "processing"
    TRANSFERRING = "transferring"
    ENDING = "ending"
    ENDED = "ended"


class CallIntent(str, Enum):
    """用户意向"""
    INTERESTED = "interested"
    NEED_MORE_INFO = "need_more_info"
    NOT_INTERESTED = "not_interested"
    BUSY = "busy"
    REQUEST_HUMAN = "request_human"
    CALLBACK = "callback"
    UNKNOWN = "unknown"


class CallAction(str, Enum):
    """LLM 决策动作"""
    CONTINUE = "continue"
    TRANSFER = "transfer"
    END = "end"
    CALLBACK = "callback"
    SEND_SMS = "send_sms"
    BLACKLIST = "blacklist"


class CallResult(str, Enum):
    """通话结果"""
    NOT_ANSWERED = "not_answered"
    BUSY = "busy"
    REJECTED = "rejected"
    COMPLETED = "completed"
    TRANSFERRED = "transferred"
    ERROR = "error"
    TIMEOUT = "timeout"


class TaskStatus(str, Enum):
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PhoneStatus(str, Enum):
    """号码状态"""
    PENDING = "pending"
    CALLING = "calling"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY = "retry"
    SKIPPED = "skipped"
