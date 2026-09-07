"""Resize long-term-memory vectors from 1024d BGE-M3 to 512d BGE-small.

Revision ID: 20260906_0008
Revises: 20260901_0007
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260906_0008"
down_revision = "20260901_0007"
branch_labels = None
depends_on = None

_INDEX = "ix_memory_embeddings_hnsw_cosine"


def _resize(bind, target_dim: int, source_dim: int) -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    rows = bind.execute(
        sa.text(
            "SELECT count(*) FROM memory_embeddings "
            "WHERE embedding IS NOT NULL AND vector_dims(embedding) <> :dim"
        ),
        {"dim": target_dim},
    ).scalar()
    if rows:
        removed = bind.execute(
            sa.text(
                "DELETE FROM memory_embeddings "
                "WHERE embedding IS NOT NULL AND vector_dims(embedding) <> :dim"
            ),
            {"dim": target_dim},
        ).rowcount
        print(
            f"memory-embedding-{target_dim} migration: removed {removed} legacy "
            f"{source_dim}d projections; keyword lane remains (outbox must reproject "
            "memories after the model switch to restore the vector lane)"
        )
    op.execute(
        f"ALTER TABLE memory_embeddings ALTER COLUMN embedding "
        f"TYPE vector({target_dim}) USING embedding::vector({target_dim})"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} ON memory_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite-only test fixtures store the JSON variant; nothing to resize.
        return
    _resize(bind, target_dim=512, source_dim=1024)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _resize(bind, target_dim=1024, source_dim=512)
