"""Initial users, projects, environments, and flags schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-14
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_projects_owner_created", "projects", ["owner_user_id", "created_at"])

    op.create_table(
        "environments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "key", name="uq_environments_project_key"),
    )
    op.create_table(
        "flags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("value_type", sa.String(length=20), nullable=False),
        sa.Column("default_value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "key", name="uq_flags_project_key"),
    )

    op.create_table(
        "environment_flags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("flag_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("kill_switch", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["flag_id"], ["flags.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("environment_id", "flag_id", name="uq_environment_flags_pair"),
    )
    op.create_index(
        "ix_environment_flags_environment_id",
        "environment_flags",
        ["environment_id"],
        unique=False,
    )
    op.create_index("ix_environment_flags_flag_id", "environment_flags", ["flag_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_environment_flags_flag_id", table_name="environment_flags")
    op.drop_index("ix_environment_flags_environment_id", table_name="environment_flags")
    op.drop_table("environment_flags")
    op.drop_table("flags")
    op.drop_table("environments")
    op.drop_index("ix_projects_owner_created", table_name="projects")
    op.drop_table("projects")
    op.drop_table("users")
