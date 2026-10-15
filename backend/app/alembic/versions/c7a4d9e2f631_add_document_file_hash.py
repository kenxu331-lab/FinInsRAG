"""Add a stable raw-file fingerprint to canonical documents.

Revision ID: c7a4d9e2f631
Revises: b2177e8e5a31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7a4d9e2f631"
down_revision: Union[str, None] = "b2177e8e5a31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("file_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_documents_file_hash", "documents", ["file_hash"])


def downgrade() -> None:
    op.drop_index("ix_documents_file_hash", table_name="documents")
    op.drop_column("documents", "file_hash")
