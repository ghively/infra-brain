"""Octopus deep capture (MR9).

Revision ID: 0013_octopus_deep
Revises: 0012_r7_vuln_cves
Create Date: 2026-06-24

Adds twelve new tables that deepen the Octopus 3.3.8 capture beyond the MR2
core (projects/environments/machines/deployments/releases/channels/lifecycles/
project-groups):

  * octopus_deployment_steps     — per-project deployment-process steps/actions
  * octopus_variables            — variable METADATA ONLY (NO value column, ever)
  * octopus_library_variable_sets
  * octopus_action_templates
  * octopus_machine_roles
  * octopus_feeds
  * octopus_interruptions
  * octopus_tasks                — collector bounds to last 365 days
  * octopus_accounts             — METADATA ONLY (NO secret/key material, ever)
  * octopus_teams
  * octopus_users
  * octopus_events               — collector bounds to last 365 days

Most tables key off the upstream Octopus string id (no FK) and are queried
directly, matching the existing octopus pattern. Each create is inspector-
guarded so brand-new tables that also appear via 0001's
``Base.metadata.create_all()`` on a fresh DB do not collide (mirrors 0005-0012).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

revision = "0013_octopus_deep"
down_revision = "0012_r7_vuln_cves"
branch_labels = None
depends_on = None

# Cross-dialect JSON: JSONB on PostgreSQL, JSON elsewhere (mirrors models.py).
_JSONB = sa.JSON().with_variant(_PG_JSONB(), "postgresql")

# Created in dependency-free order (all string-id keyed, no inter-table FK).
_TABLES = [
    "octopus_deployment_steps",
    "octopus_variables",
    "octopus_library_variable_sets",
    "octopus_action_templates",
    "octopus_machine_roles",
    "octopus_feeds",
    "octopus_interruptions",
    "octopus_tasks",
    "octopus_accounts",
    "octopus_teams",
    "octopus_users",
    "octopus_events",
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "octopus_deployment_steps" not in tables:
        op.create_table(
            "octopus_deployment_steps",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("project_octopus_id", sa.String(64), nullable=False),
            sa.Column("project_name", sa.String(256), nullable=True),
            sa.Column("deployment_process_id", sa.String(64), nullable=True),
            sa.Column("step_order", sa.Integer(), nullable=False),
            sa.Column("step_name", sa.String(256), nullable=False),
            sa.Column("action_name", sa.String(256), nullable=True),
            sa.Column("action_type", sa.String(128), nullable=True),
            sa.Column("target_roles", _JSONB, nullable=False),
            sa.Column("channels", _JSONB, nullable=False),
            sa.Column("environments", _JSONB, nullable=False),
            sa.Column("feed_id", sa.String(64), nullable=True),
            sa.Column("package_id", sa.String(256), nullable=True),
            sa.Column("condition", sa.String(32), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint(
                "project_octopus_id",
                "step_order",
                "action_name",
                name="uq_octopus_deployment_step_natkey",
            ),
        )
        op.create_index(
            "ix_octopus_deployment_steps_project_octopus_id",
            "octopus_deployment_steps",
            ["project_octopus_id"],
        )
        op.create_index(
            "ix_octopus_deployment_steps_natkey",
            "octopus_deployment_steps",
            ["project_octopus_id", "step_order"],
        )

    if "octopus_variables" not in tables:
        # METADATA ONLY: no value column, ever.
        op.create_table(
            "octopus_variables",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("owner_type", sa.String(16), nullable=False),
            sa.Column("owner_octopus_id", sa.String(64), nullable=False),
            sa.Column("owner_name", sa.String(256), nullable=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("scope", _JSONB, nullable=True),
            sa.Column("is_sensitive", sa.Boolean(), nullable=False),
            sa.Column("is_editable", sa.Boolean(), nullable=False),
            sa.UniqueConstraint(
                "owner_octopus_id", "name", "ordinal", name="uq_octopus_variable_natkey"
            ),
        )
        op.create_index(
            "ix_octopus_variables_owner_octopus_id",
            "octopus_variables",
            ["owner_octopus_id"],
        )
        op.create_index(
            "ix_octopus_variables_natkey",
            "octopus_variables",
            ["owner_octopus_id", "name"],
        )

    if "octopus_library_variable_sets" not in tables:
        op.create_table(
            "octopus_library_variable_sets",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("variable_set_id", sa.String(64), nullable=True),
            sa.Column("content_type", sa.String(32), nullable=True),
        )

    if "octopus_action_templates" not in tables:
        op.create_table(
            "octopus_action_templates",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("action_type", sa.String(128), nullable=True),
            sa.Column("version", sa.String(32), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
        )

    if "octopus_machine_roles" not in tables:
        op.create_table(
            "octopus_machine_roles",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("role_name", sa.String(256), nullable=False, unique=True),
        )

    if "octopus_feeds" not in tables:
        op.create_table(
            "octopus_feeds",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("feed_type", sa.String(64), nullable=True),
            sa.Column("feed_uri", sa.String(512), nullable=True),
        )

    if "octopus_interruptions" not in tables:
        op.create_table(
            "octopus_interruptions",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("title", sa.String(512), nullable=True),
            sa.Column("is_pending", sa.Boolean(), nullable=False),
            sa.Column("task_id", sa.String(64), nullable=True),
            sa.Column("responsible_team_ids", _JSONB, nullable=True),
            sa.Column("related_document_ids", _JSONB, nullable=True),
            sa.Column("created", sa.DateTime(timezone=True), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
        )
        op.create_index("ix_octopus_interruptions_task_id", "octopus_interruptions", ["task_id"])

    if "octopus_tasks" not in tables:
        op.create_table(
            "octopus_tasks",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(128), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("task_type", sa.String(64), nullable=True),
            sa.Column("state", sa.String(32), nullable=True),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration", sa.String(64), nullable=True),
            sa.Column("finished_successfully", sa.Boolean(), nullable=True),
            sa.Column("has_warnings_or_errors", sa.Boolean(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("server_node", sa.String(128), nullable=True),
        )

    if "octopus_accounts" not in tables:
        # METADATA ONLY: no secret/key material, ever.
        op.create_table(
            "octopus_accounts",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("account_type", sa.String(64), nullable=True),
            sa.Column("username", sa.String(256), nullable=True),
            sa.Column("environment_ids", _JSONB, nullable=False),
        )

    if "octopus_teams" not in tables:
        op.create_table(
            "octopus_teams",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("member_user_ids", _JSONB, nullable=True),
            sa.Column("project_ids", _JSONB, nullable=True),
            sa.Column("environment_ids", _JSONB, nullable=True),
            sa.Column("user_role_ids", _JSONB, nullable=True),
        )

    if "octopus_users" not in tables:
        op.create_table(
            "octopus_users",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("username", sa.String(256), nullable=False),
            sa.Column("display_name", sa.String(256), nullable=True),
            sa.Column("email", sa.String(256), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.Column("is_service", sa.Boolean(), nullable=True),
        )

    if "octopus_events" not in tables:
        op.create_table(
            "octopus_events",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("category", sa.String(128), nullable=True),
            sa.Column("username", sa.String(256), nullable=True),
            sa.Column("occurred", sa.DateTime(timezone=True), nullable=True),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("related_document_ids", _JSONB, nullable=True),
        )
        op.create_index("ix_octopus_events_occurred", "octopus_events", ["occurred"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for table in reversed(_TABLES):
        if table in tables:
            op.drop_table(table)
