from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from models.base import Base


class Document(Base):
    """Canonical metadata for both persistent and session-scoped documents."""

    __tablename__ = "documents"

    document_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(255), nullable=False, index=True)
    session_id = Column(String(64), nullable=True, index=True)
    scope = Column(String(16), nullable=False, default="user", index=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(Text, nullable=False)
    mime_type = Column(String(100), nullable=True)
    file_size = Column(Integer, nullable=True)
    file_hash = Column(String(64), nullable=True, index=True)
    content_hash = Column(String(64), nullable=True, index=True)
    parse_status = Column(String(20), nullable=False, default="pending", index=True)
    chunk_count = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
