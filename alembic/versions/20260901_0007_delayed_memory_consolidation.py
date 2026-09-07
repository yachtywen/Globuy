"""Delay memory consolidation and add time-only decay state.

Revision ID: 20260901_0007
Revises: 20260901_0006
"""

from __future__ import annotations

import hashlib
import json
import os

import sqlalchemy as sa

from alembic import op
from app.database.models import UTC_DATETIME

revision = "20260901_0007"
down_revision = "20260901_0006"
branch_labels = None
depends_on = None


def _manifest(bind: sa.Connection) -> dict[str, dict[str, object]]:
    statements = {
        "current": (
            "SELECT memory_id,user_id,memory,content_hash FROM memory_entries "
            "ORDER BY memory_id"
        ),
        "history": (
            "SELECT history_id,memory_id,user_id,event,memory_version,old_memory,new_memory "
            "FROM memory_history ORDER BY history_id"
        ),
        "vectors": (
            "SELECT memory_id,embedding_model,embedding_revision,dimensions,content_hash "
            "FROM memory_embeddings ORDER BY memory_id"
        ),
    }
    result: dict[str, dict[str, object]] = {}
    for name, statement in statements.items():
        rows = list(bind.execute(sa.text(statement)).tuples())
        payload = json.dumps([list(row) for row in rows], ensure_ascii=False, default=str)
        result[name] = {
            "count": len(rows),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }
    return result


def _create_consolidation_state(inspector: sa.Inspector) -> None:
    if inspector.has_table("memory_consolidation_states"):
        return
    op.create_table(
        "memory_consolidation_states",
        sa.Column(
            "thread_id",
            sa.String(128),
            sa.ForeignKey("threads.thread_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("processed_through_ordinal", sa.Integer(), nullable=False, default=0),
        sa.Column("pending_successful_runs", sa.Integer(), nullable=False, default=0),
        sa.Column("due_at", UTC_DATETIME),
        sa.Column("claimed_at", UTC_DATETIME),
        sa.Column("claim_token", sa.String(128)),
        sa.Column("attempts", sa.Integer(), nullable=False, default=0),
        sa.Column("last_error_code", sa.String(100)),
        sa.Column("dead_lettered_at", UTC_DATETIME),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.CheckConstraint("processed_through_ordinal >= 0"),
        sa.CheckConstraint("pending_successful_runs >= 0"),
        sa.CheckConstraint("attempts >= 0"),
    )
    for name, columns in (
        ("ix_memory_consolidation_states_user_id", ["user_id"]),
        ("ix_memory_consolidation_states_due_at", ["due_at"]),
        ("ix_memory_consolidation_states_claimed_at", ["claimed_at"]),
        ("ix_memory_consolidation_states_claim_token", ["claim_token"]),
        ("ix_memory_consolidation_states_dead_lettered_at", ["dead_lettered_at"]),
    ):
        op.create_index(name, "memory_consolidation_states", columns)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    memory_columns = {item["name"] for item in inspector.get_columns("memory_entries")}
    outbox_columns = {item["name"] for item in inspector.get_columns("outbox_events")}
    if (
        "last_confirmed_at" in memory_columns
        and "status" not in memory_columns
        and "dead_lettered_at" in outbox_columns
        and inspector.has_table("memory_consolidation_states")
    ):
        return
    if os.getenv("GLOBUY_MEMORY_MIGRATION_BACKUP_CONFIRMED", "").lower() != "true":
        raise RuntimeError(
            "Back up the database, then set "
            "GLOBUY_MEMORY_MIGRATION_BACKUP_CONFIRMED=true before this one-way migration"
        )

    before = _manifest(bind)
    print(f"delayed-memory before manifest: {json.dumps(before, sort_keys=True)}")

    if "last_confirmed_at" not in memory_columns:
        op.add_column("memory_entries", sa.Column("last_confirmed_at", UTC_DATETIME))
        op.execute("UPDATE memory_entries SET last_confirmed_at=updated_at")
        op.alter_column("memory_entries", "last_confirmed_at", nullable=False)
        op.create_index(
            "ix_memory_entries_last_confirmed_at",
            "memory_entries",
            ["last_confirmed_at"],
        )

    if "status" in memory_columns:
        op.execute("DELETE FROM memory_entries WHERE status='deleted'")
        index_names = {item["name"] for item in inspector.get_indexes("memory_entries")}
        for name in ("uq_memory_entries_active_hash", "ix_memory_entries_status"):
            if name in index_names:
                op.drop_index(name, table_name="memory_entries")
        for constraint in inspector.get_check_constraints("memory_entries"):
            if "status" in str(constraint.get("sqltext") or "").lower() and constraint.get("name"):
                op.drop_constraint(
                    str(constraint["name"]), "memory_entries", type_="check"
                )
        op.drop_column("memory_entries", "status")
    if "deleted_at" in memory_columns:
        op.drop_column("memory_entries", "deleted_at")

    unique_names = {
        item.get("name") for item in sa.inspect(bind).get_unique_constraints("memory_entries")
    }
    if "uq_memory_entries_user_hash" not in unique_names:
        op.create_unique_constraint(
            "uq_memory_entries_user_hash",
            "memory_entries",
            ["user_id", "content_hash"],
        )

    if "dead_lettered_at" not in outbox_columns:
        op.add_column("outbox_events", sa.Column("dead_lettered_at", UTC_DATETIME))
        op.create_index(
            "ix_outbox_events_dead_lettered_at",
            "outbox_events",
            ["dead_lettered_at"],
        )

    _create_consolidation_state(sa.inspect(bind))
    after = _manifest(bind)
    print(f"delayed-memory after manifest: {json.dumps(after, sort_keys=True)}")
    if int(after["current"]["count"]) > int(before["current"]["count"]):
        raise RuntimeError("delayed-memory migration unexpectedly added current memories")
    if after["history"] != before["history"]:
        raise RuntimeError("delayed-memory migration unexpectedly changed memory history")


def downgrade() -> None:
    raise RuntimeError(
        "20260901_0007 is intentionally one-way; restore the required pre-migration backup"
    )
