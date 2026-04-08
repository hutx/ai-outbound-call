"""
话术脚本数据库模型
"""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON
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
    main_script = Column(JSON, nullable=False)  # 主要话术内容
    objection_handling = Column(JSON, nullable=False, default=lambda: {})  # 异议处理话术
    closing_script = Column(Text)  # 结束语话术
    created_at = Column(DateTime(timezone=True), server_default=func.now())  # 创建时间
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())  # 更新时间
    is_active = Column(Boolean, nullable=False, default=True)  # 是否激活