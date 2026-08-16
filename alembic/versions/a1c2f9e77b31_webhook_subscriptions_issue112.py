"""Outbound webhook subscription/event-publish system (GitLab #112)

Revision ID: a1c2f9e77b31
Revises: 9d83fb71e295
Create Date: 2026-07-30

Adds the subscription-table half of the webhook-out system described in
GitLab issue #112: today ``config.py``'s ``ops_webhook_url`` is a single
hardcoded destination for ALL ``send_ops_alert`` calls. This migration adds:

  * webhook_subscriptions — third-party-registered destination + event
    filter (event_pattern glob match, optional domain_filter AND-condition,
    active flag, optional bearer secret_token).
  * webhook_deliveries — one row per delivery attempt, driving the retry
    worker (tools/webhook_publish.py's retry_pending_deliveries, wired into
    scheduler.py's hourly _webhook_retry_job).

Guarded with inspector existence checks (mirrors 0009-0011/b4d40e...): a
fresh install's 0001_initial_schema.py create_all() already builds these
tables from the current ORM.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "a1c2f9e77b31"
down_revision = "9d83fb71e295"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "webhook_subscriptions" not in tables:
        op.create_table(
            "webhook_subscriptions",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("target_url", sa.String(1024), nullable=False),
            sa.Column("secret_token", sa.String(256), nullable=True),
            sa.Column("event_pattern", sa.String(128), nullable=False, server_default="*"),
            sa.Column("domain_filter", sa.String(64), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(128), nullable=False, server_default=""),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_webhook_subscriptions_active", "webhook_subscriptions", ["active"]
        )
        op.create_index(
            "ix_webhook_subscriptions_event_pattern",
            "webhook_subscriptions",
            ["event_pattern"],
        )

    if "webhook_deliveries" not in tables:
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "subscription_id",
                sa.Uuid(),
                sa.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("category", sa.String(128), nullable=False),
            sa.Column("domain", sa.String(64), nullable=True),
            sa.Column("dedup_key", sa.String(256), nullable=True),
            sa.Column("payload_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_webhook_deliveries_status_next_attempt",
            "webhook_deliveries",
            ["status", "next_attempt_at"],
        )
        op.create_index(
            "ix_webhook_deliveries_subscription_id",
            "webhook_deliveries",
            ["subscription_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "webhook_deliveries" in tables:
        op.drop_table("webhook_deliveries")
    if "webhook_subscriptions" in tables:
        op.drop_table("webhook_subscriptions")
