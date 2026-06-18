"""
享客虾 v2 — 数据模型
"""
from sqlalchemy import Column, BigInteger, String, Text, DateTime, JSON, Index, func
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

class BotSessionMessage(Base):
    """对话消息（核心 session 表）"""
    __tablename__ = "bot_session_messages_v2"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(128), nullable=False, index=True)
    role = Column(String(16), nullable=False)  # system/user/assistant/tool
    content = Column(Text, nullable=False)
    tool_calls = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_v2_session_time", "session_id", "created_at"),
    )


class UserMemory(Base):
    """用户长期记忆"""
    __tablename__ = "user_memories_v2"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, unique=True)
    preferences = Column(JSON, default=dict)
    summary = Column(Text, nullable=True)
    facts = Column(JSON, nullable=True)  # list of strings
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
