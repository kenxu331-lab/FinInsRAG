"""Add canonical documents table.

Revision ID: b2177e8e5a31
Revises: 43c6db52e840
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2177e8e5a31"
down_revision: Union[str, None] = "43c6db52e840"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("parse_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("scope IN ('user', 'session')", name="ck_documents_scope"),
        sa.PrimaryKeyConstraint("document_id"),
    )
    op.create_index("ix_documents_user_id", "documents", ["user_id"])
    op.create_index("ix_documents_session_id", "documents", ["session_id"])
    op.create_index("ix_documents_scope", "documents", ["scope"])
    op.create_index("ix_documents_content_hash", "documents", ["content_hash"])
    op.create_index("ix_documents_parse_status", "documents", ["parse_status"])
    op.create_index("ix_documents_expires_at", "documents", ["expires_at"])

    # Preserve legacy knowledge-base rows as metadata. Empty paths/status make it
    # explicit that the reindex command still needs to locate the source file.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "knowledgebases" in inspector.get_table_names():
        bind.execute(
            sa.text(
                """
                INSERT INTO documents (
                    document_id, user_id, scope, file_name, file_path,
                    parse_status, chunk_count, created_at, updated_at
                )
                SELECT gen_random_uuid()::text, user_id, 'user', file_name, '',
                       'legacy', 0, created_at, updated_at
                FROM knowledgebases
                """
            )
        )


def downgrade() -> None:
    op.drop_index("ix_documents_expires_at", table_name="documents")
    op.drop_index("ix_documents_parse_status", table_name="documents")
    op.drop_index("ix_documents_content_hash", table_name="documents")
    op.drop_index("ix_documents_scope", table_name="documents")
    op.drop_index("ix_documents_session_id", table_name="documents")
    op.drop_index("ix_documents_user_id", table_name="documents")
    op.drop_table("documents")
