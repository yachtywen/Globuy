"""Add structured long-term-memory fact semantics.

Revision ID: 20260823_0004
Revises: 20260820_0003
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260823_0004"
down_revision = "20260820_0003"
branch_labels = None
depends_on = None

_COMMON = (
    ("subject", sa.String(128)),
    ("predicate", sa.String(64)),
    ("value_json", sa.JSON()),
    ("polarity", sa.String(16)),
    ("scope_type", sa.String(16)),
    ("scope_value", sa.String(128)),
    ("evidence_type", sa.String(16)),
    ("fact_slot", sa.String(64)),
    ("extraction_version", sa.String(32)),
)

_CHECKS = {
    "memory_entries": {
        "ck_memory_entries_polarity": "polarity IS NULL OR polarity IN ('positive','negative')",
        "ck_memory_entries_scope_type": (
            "scope_type IS NULL OR scope_type IN ('global','category','brand','product')"
        ),
        "ck_memory_entries_evidence_type": (
            "evidence_type IS NULL OR evidence_type IN ('explicit','inferred','imported')"
        ),
    },
    "memory_candidates": {
        "ck_memory_candidates_persistence_scope": (
            "persistence_scope IN ('long_term','session_only')"
        ),
        "ck_memory_candidates_polarity": (
            "polarity IS NULL OR polarity IN ('positive','negative')"
        ),
        "ck_memory_candidates_scope_type": (
            "scope_type IS NULL OR scope_type IN ('global','category','brand','product')"
        ),
        "ck_memory_candidates_evidence_type": (
            "evidence_type IS NULL OR evidence_type IN ('explicit','inferred','imported')"
        ),
    },
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("memory_entries", "memory_candidates"):
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, type_ in _COMMON:
            if name not in existing:
                op.add_column(table, sa.Column(name, type_, nullable=True))
    entry_columns = {column["name"] for column in sa.inspect(bind).get_columns("memory_entries")}
    if "supersedes_memory_id" not in entry_columns:
        op.add_column("memory_entries", sa.Column("supersedes_memory_id", sa.String(128)))
    candidate_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("memory_candidates")
    }
    if "persistence_scope" not in candidate_columns:
        op.add_column(
            "memory_candidates",
            sa.Column(
                "persistence_scope",
                sa.String(16),
                nullable=False,
                server_default="long_term",
            ),
        )
    if "conflicts_with_memory_id" not in candidate_columns:
        op.add_column(
            "memory_candidates", sa.Column("conflicts_with_memory_id", sa.String(128))
        )
    op.execute(
        "UPDATE memory_entries SET evidence_type='imported', extraction_version='legacy-v1' "
        "WHERE evidence_type IS NULL"
    )
    for table, checks in _CHECKS.items():
        present = {
            item["name"]
            for item in sa.inspect(bind).get_check_constraints(table)
            if item["name"]
        }
        for name, condition in checks.items():
            if name not in present:
                op.create_check_constraint(name, table, condition)
    entry_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("memory_entries")}
    if "ix_memory_entries_active_fact_slot" not in entry_indexes:
        op.create_index(
            "ix_memory_entries_active_fact_slot",
            "memory_entries",
            ["user_id", "fact_slot"],
            postgresql_where=sa.text(
                "status='active' AND lifecycle_status='active' "
                "AND category <> 'history' AND fact_slot IS NOT NULL"
            ),
        )
    candidate_indexes = {
        item["name"] for item in sa.inspect(bind).get_indexes("memory_candidates")
    }
    if "ix_memory_candidates_user_fact_slot" not in candidate_indexes:
        op.create_index(
            "ix_memory_candidates_user_fact_slot",
            "memory_candidates",
            ["user_id", "fact_slot", "status"],
        )
    # Structured v2 permits archived versions to retain the same user-facing key.
    constraints = {
        item["name"]
        for item in sa.inspect(bind).get_unique_constraints("memory_entries")
    }
    if "uq_memory_user_key" in constraints:
        op.drop_constraint("uq_memory_user_key", "memory_entries", type_="unique")
    candidate_constraints = {
        item["name"]
        for item in sa.inspect(bind).get_unique_constraints("memory_candidates")
    }
    if "uq_memory_candidate_state" in candidate_constraints:
        op.drop_constraint(
            "uq_memory_candidate_state", "memory_candidates", type_="unique"
        )


def downgrade() -> None:
    op.drop_index("ix_memory_candidates_user_fact_slot", table_name="memory_candidates")
    op.drop_index("ix_memory_entries_active_fact_slot", table_name="memory_entries")
    for table, checks in reversed(_CHECKS.items()):
        for name in reversed(checks):
            op.drop_constraint(name, table, type_="check")
    op.create_unique_constraint("uq_memory_user_key", "memory_entries", ["user_id", "key"])
    op.create_unique_constraint(
        "uq_memory_candidate_state",
        "memory_candidates",
        ["user_id", "content_hash", "status"],
    )
    for name in ("conflicts_with_memory_id", "persistence_scope"):
        op.drop_column("memory_candidates", name)
    op.drop_column("memory_entries", "supersedes_memory_id")
    for table in ("memory_candidates", "memory_entries"):
        for name, _ in reversed(_COMMON):
            op.drop_column(table, name)
