"""数据模型包"""
from .call_record import (
    CallRecordCreate,
    CallRecordUpdate,
    CallRecordResponse,
    TranscriptEntry,
)
from .call_record_detail import (
    CallRecordDetail,
    CallRecordDetailCreate,
)
from .file import (
    FileRecord,
    FileRecordCreate,
)

__all__ = [
    "CallRecordCreate",
    "CallRecordUpdate",
    "CallRecordResponse",
    "TranscriptEntry",
    "CallRecordDetail",
    "CallRecordDetailCreate",
    "FileRecord",
    "FileRecordCreate",
]
