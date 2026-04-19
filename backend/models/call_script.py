"""
话术脚本数据库模型
"""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
from sqlalchemy.sql import func
from backend.utils.db import Base


class CallScript(Base):
    """
    话术脚本表模型
    """
    __tablename__ = "call_scripts"

    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(String(64), unique=True, nullable=False, index=True)  # 脚本ID
    name = Column(String(128), nullable=False)  # 脚本名称
    description = Column(String(512))  # 描述
    script_type = Column(String(30), nullable=False, default='financial')  # 脚本类型
    opening_script = Column(Text, nullable=False)  # 开场白话术
    opening_pause = Column(Integer, nullable=False, default=2000)  # 开场白后停顿时长(毫秒)
    main_script = Column(Text, nullable=False)  # 主要话术内容（纯文本）
    closing_script = Column(Text)  # 结束语话术
    created_at = Column(DateTime(timezone=True), server_default=func.now())  # 创建时间
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())  # 更新时间
    is_active = Column(Boolean, nullable=False, default=True)  # 是否激活

    # 打断策略配置
    opening_barge_in = Column(Boolean, nullable=False, default=False)  # 开场白支持打断
    closing_barge_in = Column(Boolean, nullable=False, default=False)  # 结束语支持打断
    conversation_barge_in = Column(Boolean, nullable=False, default=True)  # 其他对话支持打断
    barge_in_protect_start = Column(Integer, nullable=False, default=3)  # 开始 N 秒内不打断
    barge_in_protect_end = Column(Integer, nullable=False, default=3)  # 结束前 N 秒不打断

    # 宽容时间配置
    tolerance_enabled = Column(Boolean, nullable=False, default=True)  # 是否启用宽容时间
    tolerance_ms = Column(Integer, nullable=False, default=1000)  # TTS 播放后宽容时间（毫秒）