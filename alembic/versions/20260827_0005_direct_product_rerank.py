"""Add direct-search provenance and conservative cross-platform product groups.

Revision ID: 20260827_0005
Revises: 20260823_0004
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260827_0005"
down_revision = "20260823_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("catalog_scope_offers", sa.Column("source_rank", sa.Integer()))
    op.add_column("catalog_scope_offers", sa.Column("source_request_key", sa.String(64)))
    op.create_table(
        "product_groups",
        sa.Column("product_group_id", sa.String(128), primary_key=True),
        sa.Column("identity_key_hash", sa.String(64), nullable=False),
        sa.Column("identity_version", sa.String(32), nullable=False),
        sa.Column("brand_normalized", sa.String(255)),
        sa.Column("model_normalized", sa.String(255)),
        sa.Column("variant_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("identity_key_hash", name="uq_product_groups_identity_key_hash"),
    )
    op.create_index("ix_product_groups_brand_normalized", "product_groups", ["brand_normalized"])
    op.create_index("ix_product_groups_model_normalized", "product_groups", ["model_normalized"])
    op.create_table(
        "product_group_members",
        sa.Column(
            "product_id",
            sa.String(128),
            sa.ForeignKey("products.product_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "product_group_id",
            sa.String(128),
            sa.ForeignKey("product_groups.product_group_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("match_method", sa.String(32), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "match_method IN ('singleton','gtin_exact','brand_model_variant_exact')",
            name="ck_product_group_members_match_method",
        ),
    )
    op.create_index(
        "ix_product_group_members_product_group_id",
        "product_group_members",
        ["product_group_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_group_members_product_group_id", table_name="product_group_members")
    op.drop_table("product_group_members")
    op.drop_index("ix_product_groups_model_normalized", table_name="product_groups")
    op.drop_index("ix_product_groups_brand_normalized", table_name="product_groups")
    op.drop_table("product_groups")
    op.drop_column("catalog_scope_offers", "source_request_key")
    op.drop_column("catalog_scope_offers", "source_rank")
