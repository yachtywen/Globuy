"""Add user-owned memory Skills and associate existing memories with them."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0002"
down_revision = "20260721_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("memory_skills"):
        op.create_table(
        "memory_skills",
        sa.Column("skill_id", sa.String(length=128), primary_key=True),
        sa.Column("user_id", sa.String(length=128), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("trigger_keywords", sa.JSON(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('active','deleted')"),
        sa.UniqueConstraint("user_id", "name", name="uq_memory_skill_user_name"),
        )
        op.create_index("ix_memory_skills_user_id", "memory_skills", ["user_id"])
        op.create_index("ix_memory_skills_is_enabled", "memory_skills", ["is_enabled"])
        op.create_index("ix_memory_skills_status", "memory_skills", ["status"])
    columns = {item["name"] for item in inspector.get_columns("memory_entries")}
    if "skill_id" not in columns:
        op.add_column("memory_entries", sa.Column("skill_id", sa.String(length=128), nullable=True))
        op.create_index("ix_memory_entries_skill_id", "memory_entries", ["skill_id"])
        op.create_foreign_key(
            "fk_memory_entries_skill_id", "memory_entries", "memory_skills", ["skill_id"], ["skill_id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "skill_id" in {item["name"] for item in inspector.get_columns("memory_entries")}:
        op.drop_constraint("fk_memory_entries_skill_id", "memory_entries", type_="foreignkey")
        op.drop_index("ix_memory_entries_skill_id", table_name="memory_entries")
        op.drop_column("memory_entries", "skill_id")
    if inspector.has_table("memory_skills"):
        op.drop_table("memory_skills")
