"""Add evaluation rules, SDK keys, audit logs, events, and experiments.

Revision ID: 0002_evaluation_events_experiments
Revises: 0001_initial_schema
Create Date: 2026-11-14
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_evaluation_events_experiments"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "environment_flags",
        sa.Column("rollout_percentage", sa.Float(), nullable=False, server_default="100"),
    )
    op.add_column(
        "environment_flags",
        sa.Column("targeting_rules", sa.JSON(), nullable=True),
    )
    op.add_column("environment_flags", sa.Column("on_value", sa.JSON(), nullable=True))

    op.create_table(
        "sdk_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("key_hash", name="uq_sdk_keys_hash"),
    )
    op.create_index("ix_sdk_keys_environment_id", "sdk_keys", ["environment_id"])
    op.create_index(
        "ix_sdk_keys_environment_revoked", "sdk_keys", ["environment_id", "revoked_at"]
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_audit_logs_project_id", "audit_logs", ["project_id"])
    op.create_index("ix_audit_logs_project_created", "audit_logs", ["project_id", "created_at"])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("event_name", sa.String(length=120), nullable=False),
        sa.Column("user_id", sa.String(length=200), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("flag_key", sa.String(length=120), nullable=True),
        sa.Column("variant", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("properties", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_id", name="uq_events_event_id"),
    )
    op.create_index(
        "ix_events_project_name_time", "events", ["project_id", "event_name", "created_at"]
    )
    op.create_index("ix_events_flag_user", "events", ["flag_key", "user_id"])

    op.create_table(
        "experiments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("flag_id", sa.Integer(), nullable=False),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("primary_metric", sa.String(length=120), nullable=False),
        sa.Column("variants", sa.JSON(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["flag_id"], ["flags.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "key", name="uq_experiments_project_key"),
    )
    op.create_index(
        "ix_experiments_project_status", "experiments", ["project_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_experiments_project_status", table_name="experiments")
    op.drop_table("experiments")
    op.drop_index("ix_events_flag_user", table_name="events")
    op.drop_index("ix_events_project_name_time", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_audit_logs_project_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_project_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_sdk_keys_environment_revoked", table_name="sdk_keys")
    op.drop_index("ix_sdk_keys_environment_id", table_name="sdk_keys")
    op.drop_table("sdk_keys")
    op.drop_column("environment_flags", "on_value")
    op.drop_column("environment_flags", "targeting_rules")
    op.drop_column("environment_flags", "rollout_percentage")
