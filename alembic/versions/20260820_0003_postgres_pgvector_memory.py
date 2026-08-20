"""Add PostgreSQL pgvector-backed long-term memory lifecycle.

Revision ID: 20260820_0003
Revises: 20260819_0002
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op
from app.database.models import KEYWORDS_TYPE, UTC_DATETIME

revision = "20260820_0003"
down_revision = "20260819_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # See revision 0001: clean installs are created from current metadata,
    # while upgraded installations reach this revision incrementally.
    if sa.inspect(bind).has_table("memory_embeddings"):
        if bind.dialect.name == "postgresql":
            op.execute(
                "CREATE INDEX IF NOT EXISTS ix_memory_entries_keywords_gin "
                "ON memory_entries USING gin (keywords)"
            )
            op.execute(
                "CREATE INDEX IF NOT EXISTS ix_memory_embeddings_hnsw_cosine "
                "ON memory_embeddings USING hnsw (embedding vector_cosine_ops)"
            )
        return

    op.add_column("memory_entries", sa.Column("keywords", KEYWORDS_TYPE, nullable=True))
    op.add_column(
        "memory_entries",
        sa.Column("lifecycle_status", sa.String(24), nullable=False, server_default="active"),
    )
    op.add_column("memory_entries", sa.Column("last_reinforced_at", UTC_DATETIME))
    op.add_column(
        "memory_entries",
        sa.Column("reinforcement_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("memory_entries", sa.Column("archived_at", UTC_DATETIME))
    op.add_column("memory_entries", sa.Column("purge_after", UTC_DATETIME))
    op.execute(
        "UPDATE memory_entries SET last_reinforced_at = updated_at WHERE last_reinforced_at IS NULL"
    )
    op.alter_column("memory_entries", "last_reinforced_at", nullable=False)
    op.create_index("ix_memory_entries_lifecycle_status", "memory_entries", ["lifecycle_status"])
    op.create_index("ix_memory_entries_purge_after", "memory_entries", ["purge_after"])
    if bind.dialect.name == "postgresql":
        op.create_index(
            "ix_memory_entries_keywords_gin",
            "memory_entries",
            ["keywords"],
            postgresql_using="gin",
        )

    vector_type = Vector(1024) if bind.dialect.name == "postgresql" else sa.JSON()
    op.create_table(
        "memory_embeddings",
        sa.Column(
            "memory_id",
            sa.String(128),
            sa.ForeignKey("memory_entries.memory_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("embedding", vector_type, nullable=False),
        sa.Column("embedding_model", sa.String(255), nullable=False),
        sa.Column("embedding_revision", sa.String(128), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("normalized", sa.Boolean(), nullable=False),
        sa.Column("semantic_text_version", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedded_at", UTC_DATETIME, nullable=False),
    )
    op.create_index("ix_memory_embeddings_content_hash", "memory_embeddings", ["content_hash"])
    if bind.dialect.name == "postgresql":
        op.create_index(
            "ix_memory_embeddings_hnsw_cosine",
            "memory_embeddings",
            ["embedding"],
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        )

    op.create_table(
        "memory_candidates",
        sa.Column("candidate_id", sa.String(128), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("keywords", KEYWORDS_TYPE, nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_thread_id", sa.String(128)),
        sa.Column("source_run_id", sa.String(128)),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("expires_at", UTC_DATETIME, nullable=False),
        sa.Column("decided_at", UTC_DATETIME),
        sa.UniqueConstraint("user_id", "content_hash", "status", name="uq_memory_candidate_state"),
    )
    for name in ("user_id", "category", "content_hash", "status", "expires_at"):
        op.create_index(f"ix_memory_candidates_{name}", "memory_candidates", [name])


def downgrade() -> None:
    op.drop_table("memory_candidates")
    op.drop_table("memory_embeddings")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_index("ix_memory_entries_keywords_gin", table_name="memory_entries")
    for name in (
        "purge_after",
        "archived_at",
        "reinforcement_count",
        "last_reinforced_at",
        "lifecycle_status",
        "keywords",
    ):
        op.drop_column("memory_entries", name)
