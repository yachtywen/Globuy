"""Add bounded catalog hydration and product projection state."""

import sqlalchemy as sa
from alembic import op

from app.database.models import UTC_DATETIME

revision = "20260819_0002"
down_revision = "20260721_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("category_key", sa.String(128)))
    op.add_column("products", sa.Column("category_path", sa.JSON()))
    op.add_column("products", sa.Column("semantic_hash", sa.String(64)))
    op.create_index("ix_products_category_key", "products", ["category_key"])
    op.create_index("ix_products_semantic_hash", "products", ["semantic_hash"])
    for name, column in (
        ("provider_query", sa.Column("provider_query", sa.String(255))),
        ("scope_id", sa.Column("scope_id", sa.String(128))),
        ("page_number", sa.Column("page_number", sa.Integer())),
        ("cursor_json", sa.Column("cursor_json", sa.JSON())),
        ("response_sha256", sa.Column("response_sha256", sa.String(64))),
    ):
        op.add_column("source_snapshots", column)
    op.create_index("ix_source_snapshots_scope_id", "source_snapshots", ["scope_id"])
    op.add_column("offers", sa.Column("inactive_reason", sa.String(100)))
    op.add_column("offers", sa.Column("projection_hash", sa.String(64)))
    op.add_column("offers", sa.Column("projected_at", UTC_DATETIME))
    op.create_index("ix_offers_projection_hash", "offers", ["projection_hash"])

    op.create_table(
        "catalog_scopes",
        sa.Column("scope_id", sa.String(128), primary_key=True),
        sa.Column("category_key", sa.String(128), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("scope_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("newest_captured_at", UTC_DATETIME),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", UTC_DATETIME),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.UniqueConstraint("category_key", "platform", "currency", "provider", "scope_version", name="uq_catalog_scope_identity"),
    )
    op.create_index("ix_catalog_scopes_category_key", "catalog_scopes", ["category_key"])
    op.create_index("ix_catalog_scopes_platform", "catalog_scopes", ["platform"])
    op.create_index("ix_catalog_scopes_provider", "catalog_scopes", ["provider"])
    op.create_index("ix_catalog_scopes_status", "catalog_scopes", ["status"])
    op.create_index("ix_catalog_scopes_lease_expires_at", "catalog_scopes", ["lease_expires_at"])
    op.create_table(
        "catalog_scope_offers",
        sa.Column("scope_id", sa.String(128), sa.ForeignKey("catalog_scopes.scope_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("offer_id", sa.String(128), sa.ForeignKey("offers.offer_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_seen_at", UTC_DATETIME, nullable=False),
        sa.Column("expires_at", UTC_DATETIME, nullable=False),
    )
    op.create_index("ix_catalog_scope_offers_expires_at", "catalog_scope_offers", ["expires_at"])
    op.create_table(
        "catalog_hydration_runs",
        sa.Column("hydration_run_id", sa.String(128), primary_key=True),
        sa.Column("group_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("intent_json", sa.JSON(), nullable=False),
        sa.Column("thresholds_json", sa.JSON(), nullable=False),
        sa.Column("platform_counts_json", sa.JSON(), nullable=False),
        sa.Column("stop_reason", sa.String(64)),
        sa.Column("lease_expires_at", UTC_DATETIME),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("started_at", UTC_DATETIME),
        sa.Column("finished_at", UTC_DATETIME),
    )
    op.create_index("ix_catalog_hydration_runs_group_key", "catalog_hydration_runs", ["group_key"])
    op.create_index("ix_catalog_hydration_runs_status", "catalog_hydration_runs", ["status"])
    op.create_table(
        "provider_request_ledger",
        sa.Column("request_key", sa.String(64), primary_key=True),
        sa.Column("hydration_run_id", sa.String(128), sa.ForeignKey("catalog_hydration_runs.hydration_run_id", ondelete="SET NULL")),
        sa.Column("scope_id", sa.String(128), sa.ForeignKey("catalog_scopes.scope_id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("normalized_query", sa.String(255), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("provider_request_id", sa.String(255)),
        sa.Column("response_sha256", sa.String(64)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("completed_at", UTC_DATETIME),
    )
    op.create_index("ix_provider_request_ledger_scope_id", "provider_request_ledger", ["scope_id"])
    op.create_index("ix_provider_request_ledger_platform", "provider_request_ledger", ["platform"])
    op.create_index("ix_provider_request_ledger_status", "provider_request_ledger", ["status"])
    for name in ("available_at", "claimed_at"):
        op.add_column("outbox_events", sa.Column(name, UTC_DATETIME))
        op.create_index(f"ix_outbox_events_{name}", "outbox_events", [name])
    op.add_column("outbox_events", sa.Column("claim_token", sa.String(128)))
    op.create_index("ix_outbox_events_claim_token", "outbox_events", ["claim_token"])


def downgrade() -> None:
    op.drop_table("provider_request_ledger")
    op.drop_table("catalog_hydration_runs")
    op.drop_table("catalog_scope_offers")
    op.drop_table("catalog_scopes")
    for table, names in (
        ("outbox_events", ["claim_token", "claimed_at", "available_at"]),
        ("offers", ["projected_at", "projection_hash", "inactive_reason"]),
        ("source_snapshots", ["response_sha256", "cursor_json", "page_number", "scope_id", "provider_query"]),
        ("products", ["semantic_hash", "category_path", "category_key"]),
    ):
        for name in names:
            op.drop_column(table, name)
