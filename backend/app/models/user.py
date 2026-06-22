"""User model for authentication and data isolation."""
from sqlalchemy import Column, String, Boolean, DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
import uuid
from app.database import Base


class User(Base):
    __tablename__ = 'users'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_super_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

    # Versioning counter for revocable sync tokens. Each "撤销所有同步 Token"
    # bumps this; the JWT embeds the version at signing time, and is rejected
    # if it doesn't match the current value. Does NOT affect 24h login JWTs
    # (those have purpose=None and skip the check).
    sync_token_version = Column(Integer, nullable=False, default=0, server_default='0')

    # 知识库定位/用途(用户自述)。注入到 RAG 问答与深度研究的 system prompt。
    kb_purpose = Column(Text, nullable=True)

    # 是否允许 MCP 写入(新增/修改内容)。默认关:MCP 默认只读,开了才暴露写工具。
    mcp_write_enabled = Column(Boolean, nullable=False, default=False, server_default='false')

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
