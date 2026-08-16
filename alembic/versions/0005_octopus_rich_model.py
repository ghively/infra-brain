"""Octopus Deploy rich relational model.

Revision ID: 0005_octopus_rich_model
Revises: 0004_err_msg_agent_cfg
Create Date: 2026-06-23

Adds the rich Octopus Deploy representation:
  * NEW reference/detail tables: octopus_project_groups, octopus_lifecycles,
    octopus_projects, octopus_environments, octopus_machines, octopus_channels.
  * REPLACES the minimal octopus_releases / octopus_deployments tables with the
    richer shape. Both are EMPTY in every environment (the collector never
    populated them), so we drop and recreate rather than ALTER column-by-column.

Note: 0001 seeds a fresh DB via ``Base.metadata.create_all()`` against the live
ORM, so on a brand-new install the *new-shape* octopus_* tables already exist
when this migration runs. We therefore guard every create with ``checkfirst`` /
the inspector, and only drop+recreate octopus_releases/octopus_deployments when
they exist with the OLD shape (detected by a column that only the old shape has).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

revision = "0005_octopus_rich_model"
down_revision = "0004_err_msg_agent_cfg"
branch_labels = None
depends_on = None

# Cross-dialect JSON: JSONB on PostgreSQL, JSON elsewhere (mirrors models.py).
_JSONB = sa.JSON().with_variant(_PG_JSONB(), "postgresql")


def _new_release_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
        sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("project_name", sa.String(256), nullable=True),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("channel_id", sa.String(64), nullable=True),
        sa.Column("channel_name", sa.String(256), nullable=True),
        sa.Column("assembled", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_notes", sa.Text(), nullable=True),
    ]


def _new_deployment_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
        sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("project_name", sa.String(256), nullable=True),
        sa.Column("release_id", sa.String(64), nullable=True),
        sa.Column("release_version", sa.String(128), nullable=True),
        sa.Column("environment_id", sa.String(64), nullable=True),
        sa.Column("environment_name", sa.String(256), nullable=True),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("state", sa.String(64), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration", sa.String(64), nullable=True),
        sa.Column("finished_successfully", sa.Boolean(), nullable=True),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # --- NEW reference / detail tables (guarded for fresh-install create_all) ---
    if "octopus_project_groups" not in tables:
        op.create_table(
            "octopus_project_groups",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("retention_policy_id", sa.String(64), nullable=True),
        )

    if "octopus_lifecycles" not in tables:
        op.create_table(
            "octopus_lifecycles",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("phases", _JSONB, nullable=True),
        )

    if "octopus_projects" not in tables:
        op.create_table(
            "octopus_projects",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=False, unique=True
            ),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("slug", sa.String(256), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("project_group_id", sa.String(64), nullable=True),
            sa.Column("project_group_name", sa.String(256), nullable=True),
            sa.Column("lifecycle_id", sa.String(64), nullable=True),
            sa.Column("lifecycle_name", sa.String(256), nullable=True),
            sa.Column("deployment_process_id", sa.String(64), nullable=True),
        )

    if "octopus_environments" not in tables:
        op.create_table(
            "octopus_environments",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=False, unique=True
            ),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        )

    if "octopus_machines" not in tables:
        op.create_table(
            "octopus_machines",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=False, unique=True
            ),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("status_summary", sa.Text(), nullable=True),
            sa.Column("is_disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("communication_style", sa.String(64), nullable=True),
            sa.Column("tentacle_version", sa.String(64), nullable=True),
            sa.Column("uri", sa.String(512), nullable=True),
            sa.Column("roles", _JSONB, nullable=True),
            sa.Column("environment_ids", _JSONB, nullable=True),
            sa.Column("environment_names", _JSONB, nullable=True),
        )

    if "octopus_channels" not in tables:
        op.create_table(
            "octopus_channels",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("octopus_id", sa.String(64), nullable=False, unique=True),
            sa.Column("project_id", sa.String(64), nullable=False),
            sa.Column("project_name", sa.String(256), nullable=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("lifecycle_id", sa.String(64), nullable=True),
        )

    # --- REPLACE octopus_releases / octopus_deployments (both empty) ---
    # Old shape is detected by a column unique to it; if the table is absent or
    # already new-shape (fresh create_all), we just (re)create as needed.
    rel_cols = (
        {c["name"] for c in inspector.get_columns("octopus_releases")}
        if "octopus_releases" in tables
        else set()
    )
    if "octopus_id" not in rel_cols:  # absent or old-shape
        if "octopus_releases" in tables:
            op.drop_table("octopus_releases")
        op.create_table("octopus_releases", *_new_release_columns())

    dep_cols = (
        {c["name"] for c in inspector.get_columns("octopus_deployments")}
        if "octopus_deployments" in tables
        else set()
    )
    if "octopus_id" not in dep_cols:  # absent or old-shape
        if "octopus_deployments" in tables:
            op.drop_table("octopus_deployments")
        op.create_table("octopus_deployments", *_new_deployment_columns())


def _old_release_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("project", sa.String(256), nullable=False),
        sa.Column("channel", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _old_deployment_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=False),
        sa.Column("release_version", sa.String(128), nullable=False),
        sa.Column("project", sa.String(256), nullable=False),
        sa.Column("environment", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=True),
    ]


def downgrade() -> None:
    # Drop the new tables; restore the minimal octopus_releases/deployments shape.
    for tbl in (
        "octopus_channels",
        "octopus_machines",
        "octopus_environments",
        "octopus_projects",
        "octopus_lifecycles",
        "octopus_project_groups",
    ):
        op.drop_table(tbl)

    op.drop_table("octopus_deployments")
    op.drop_table("octopus_releases")
    op.create_table("octopus_releases", *_old_release_columns())
    op.create_table("octopus_deployments", *_old_deployment_columns())
