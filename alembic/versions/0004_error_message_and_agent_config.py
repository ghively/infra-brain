"""Add error_message to collection_runs and agent_config_settings table.

Revision ID: 0004_err_msg_agent_cfg
Revises: 0003_closed_loop_tables
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0004_err_msg_agent_cfg"
down_revision = "0003_closed_loop_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # Guard: 0001 uses Base.metadata.create_all() against the live ORM, so on a
    # fresh install these may already exist before this migration runs.
    existing_cols = {c["name"] for c in inspector.get_columns("collection_runs")}
    if "error_message" not in existing_cols:
        op.add_column(
            "collection_runs",
            sa.Column("error_message", sa.Text(), nullable=True),
        )

    if "agent_config_settings" not in inspector.get_table_names():
        op.create_table(
            "agent_config_settings",
            sa.Column("key", sa.String(128), primary_key=True),
            sa.Column("value", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
                onupdate=sa.func.now(),
            ),
        )


def downgrade() -> None:
    op.drop_table("agent_config_settings")
    op.drop_column("collection_runs", "error_message")
