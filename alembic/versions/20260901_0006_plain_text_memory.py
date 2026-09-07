"""Replace slot memories with current plain-text memories and linear history.

Revision ID: 20260901_0006
Revises: 20260827_0005

This is intentionally a one-way migration. Operators must take a database backup
before running it; downgrade cannot reconstruct removed slot/candidate semantics.
"""

from __future__ import annotations

import hashlib
import json
import os

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op
from app.database.models import KEYWORDS_TYPE, UTC_DATETIME

revision = "20260901_0006"
down_revision = "20260827_0005"
branch_labels = None
depends_on = None


def _manifest(rows: list[tuple]) -> tuple[int, str]:
    payload = json.dumps(
        sorted([list(row) for row in rows]),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return len(rows), hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _create_history() -> None:
    op.create_table(
        "memory_history",
        sa.Column("history_id", sa.String(128), primary_key=True),
        sa.Column("memory_id", sa.String(128), nullable=False),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.String(24), nullable=False),
        sa.Column("memory_version", sa.Integer()),
        sa.Column("old_memory", sa.Text()),
        sa.Column("new_memory", sa.Text()),
        sa.Column("source_thread_id", sa.String(128)),
        sa.Column("source_run_id", sa.String(128)),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.CheckConstraint(
            "event IN ('ADD','UPDATE','DELETE','UNDO','LEGACY_IMPORT')",
            name="ck_memory_history_event",
        ),
    )
    op.create_index("ix_memory_history_memory_id", "memory_history", ["memory_id"])
    op.create_index("ix_memory_history_user_id", "memory_history", ["user_id"])
    op.create_index("ix_memory_history_event", "memory_history", ["event"])


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("memory_entries")}

    # Clean installs may already have been created from current ORM metadata.
    if "memory" in columns:
        if not inspector.has_table("memory_history"):
            _create_history()
        return
    if os.getenv("GLOBUY_MEMORY_MIGRATION_BACKUP_CONFIRMED", "").lower() != "true":
        raise RuntimeError(
            "Back up the database, then set "
            "GLOBUY_MEMORY_MIGRATION_BACKUP_CONFIRMED=true before this one-way migration"
        )

    active_rows = list(
        bind.execute(
            sa.text(
                "SELECT memory_id, user_id, content FROM memory_entries "
                "WHERE status='active' AND lifecycle_status='active' ORDER BY memory_id"
            )
        ).tuples()
    )
    before_count, before_hash = _manifest(active_rows)
    print(
        "plain-memory migration before manifest: "
        f"count={before_count} sha256={before_hash}"
    )

    _create_history()
    op.create_table(
        "memory_entries_plain",
        sa.Column("memory_id", sa.String(128), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("memory", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("keywords", KEYWORDS_TYPE, nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("source_thread_id", sa.String(128)),
        sa.Column("source_run_id", sa.String(128)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.Column("deleted_at", UTC_DATETIME),
        sa.CheckConstraint("source IN ('user','agent','import')"),
        sa.CheckConstraint("status IN ('active','deleted')"),
    )
    op.execute(
        "INSERT INTO memory_entries_plain "
        "(memory_id,user_id,memory,content_hash,keywords,source,status,source_thread_id,"
        "source_run_id,version,created_at,updated_at,deleted_at) "
        "SELECT memory_id,user_id,content,content_hash,"
        "COALESCE(keywords,ARRAY[]::varchar[]),"
        "'import','active',source_thread_id,source_run_id,version,created_at,updated_at,NULL "
        "FROM memory_entries WHERE status='active' AND lifecycle_status='active'"
    )
    op.execute(
        "INSERT INTO memory_history "
        "(history_id,memory_id,user_id,event,memory_version,old_memory,new_memory,source_thread_id,"
        "source_run_id,created_at) "
        "SELECT 'legacy-entry-' || memory_id,memory_id,user_id,'LEGACY_IMPORT',"
        "version,NULL,content,"
        "source_thread_id,source_run_id,updated_at FROM memory_entries"
    )
    if inspector.has_table("memory_versions"):
        op.execute(
            "INSERT INTO memory_history "
            "(history_id,memory_id,user_id,event,memory_version,old_memory,new_memory,source_thread_id,"
            "source_run_id,created_at) "
            "SELECT 'legacy-version-' || v.memory_version_id,v.memory_id,e.user_id,"
            "'LEGACY_IMPORT',v.version,NULL,"
            "COALESCE(v.snapshot_json ->> 'content', CAST(v.snapshot_json AS TEXT)),"
            "NULL,NULL,v.created_at FROM memory_versions v "
            "JOIN memory_entries e ON e.memory_id=v.memory_id"
        )
    if inspector.has_table("memory_candidates"):
        op.execute(
            "INSERT INTO memory_history "
            "(history_id,memory_id,user_id,event,memory_version,old_memory,new_memory,source_thread_id,"
            "source_run_id,created_at) "
            "SELECT 'legacy-candidate-' || candidate_id,candidate_id,user_id,'LEGACY_IMPORT',"
            "NULL,NULL,content,source_thread_id,source_run_id,created_at FROM memory_candidates "
            "WHERE status='pending'"
        )

    if inspector.has_table("memory_embeddings"):
        op.drop_table("memory_embeddings")
    if inspector.has_table("memory_candidates"):
        op.drop_table("memory_candidates")
    if inspector.has_table("memory_versions"):
        op.drop_table("memory_versions")
    op.drop_table("memory_entries")
    op.rename_table("memory_entries_plain", "memory_entries")

    op.create_index("ix_memory_entries_user_id", "memory_entries", ["user_id"])
    op.create_index("ix_memory_entries_status", "memory_entries", ["status"])
    op.create_index("ix_memory_entries_content_hash", "memory_entries", ["content_hash"])
    op.create_index(
        "uq_memory_entries_active_hash",
        "memory_entries",
        ["user_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("status='active'"),
    )
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

    # Existing projections were dropped; replay current rows through the durable outbox.
    op.execute(
        "INSERT INTO outbox_events "
        "(event_id,aggregate_type,aggregate_id,event_type,aggregate_version,payload_json,"
        "created_at,attempts) "
        "SELECT 'plain-memory-reindex-' || memory_id,'memory',memory_id,'memory.upserted',"
        "version,'{}',updated_at,0 FROM memory_entries"
    )
    after_rows = list(
        bind.execute(
            sa.text(
                "SELECT memory_id, user_id, memory FROM memory_entries "
                "WHERE status='active' ORDER BY memory_id"
            )
        ).tuples()
    )
    after_count, after_hash = _manifest(after_rows)
    print(
        "plain-memory migration after manifest: "
        f"count={after_count} sha256={after_hash}"
    )
    if (after_count, after_hash) != (before_count, before_hash):
        raise RuntimeError("plain-memory migration manifest mismatch")


def downgrade() -> None:
    raise RuntimeError(
        "20260901_0006 is intentionally one-way; restore the required pre-migration backup"
    )
