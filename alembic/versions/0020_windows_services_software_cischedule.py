"""windows_services_software_cischedule — add relational tables for Windows
services, Windows installed software, and GitLab CI schedules.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-28

Creates three new tables:

  * windows_services  — per-resource Windows service inventory
                        (name, state, start_type, path, collected_at)
  * windows_software  — per-resource Windows installed software
                        (name, version, publisher, install_date, collected_at)
  * ci_schedules      — per-resource GitLab CI pipeline schedule records
                        (project_id, schedule_id, description, ref, cron,
                         active, created_at, collected_at)

All three have a nullable FK to resources.id (ON DELETE CASCADE) and a
UniqueConstraint that forms the natural upsert key for the collector.

Safety checklist:
  ✅ Only CREATE TABLE — no ALTER to existing tables
  ✅ Downgrade is inspector-guarded (drop tables only if they exist)
  ✅ Uses op.create_table() DDL — NOT Base.metadata.create_all()
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # ── windows_services ──────────────────────────────────────────────────────
    if "windows_services" not in existing_tables:
        op.create_table(
            "windows_services",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "resource_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("resources.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("state", sa.String(64), nullable=True),
            sa.Column("start_type", sa.String(64), nullable=True),
            sa.Column("path", sa.Text, nullable=True),
            sa.Column(
                "collected_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint("resource_id", "name", name="uq_windows_services_resource_name"),
        )

    # ── windows_software ──────────────────────────────────────────────────────
    if "windows_software" not in existing_tables:
        op.create_table(
            "windows_software",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "resource_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("resources.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("name", sa.String(512), nullable=False),
            sa.Column("version", sa.String(128), nullable=True),
            sa.Column("publisher", sa.String(256), nullable=True),
            sa.Column("install_date", sa.String(32), nullable=True),
            sa.Column(
                "collected_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint("resource_id", "name", name="uq_windows_software_resource_name"),
        )

    # ── ci_schedules ──────────────────────────────────────────────────────────
    if "ci_schedules" not in existing_tables:
        op.create_table(
            "ci_schedules",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "resource_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("resources.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("project_id", sa.Integer, nullable=False),
            sa.Column("schedule_id", sa.Integer, nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("ref", sa.String(256), nullable=True),
            sa.Column("cron", sa.String(128), nullable=True),
            sa.Column("active", sa.Boolean, nullable=True, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column(
                "collected_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint("project_id", "schedule_id", name="uq_ci_schedules_project_schedule"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())

    for table_name in ("ci_schedules", "windows_software", "windows_services"):
        if table_name in existing:
            op.drop_table(table_name)
