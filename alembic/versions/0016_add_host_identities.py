"""Add host_identities table — unified host model.

Revision ID: 0016_add_host_identities
Revises: 0015_repair_alter_table_ddl
Create Date: 2026-06-28

Adds the host_identities table (Phase 6 — unified host model):
  * NEW table: host_identities
    - id: UUID primary key
    - short_hostname: String(256), not null, indexed (unique)
    - fqdn: String(512), nullable
    - ip_addresses: JSONB array, default []
    - r7_resource_id: UUID FK → resources.id, nullable
    - vsphere_resource_id: UUID FK → resources.id, nullable
    - octopus_resource_id: UUID FK → resources.id, nullable
    - linux_resource_id: UUID FK → resources.id, nullable
    - windows_resource_id: UUID FK → resources.id, nullable
    - os_family: String(64), nullable
    - risk_score: Integer, nullable
    - vuln_count: Integer, nullable
    - patch_status: String(64), nullable
    - vsphere_power_state: String(32), nullable
    - octopus_machine_status: String(64), nullable
    - last_reconciled: timestamptz, nullable
  * Unique constraint on short_hostname
  * Index: ix_host_identities_short_hostname

Safety checklist:
  ✅ New table only — no NOT NULL added to existing tables
  ✅ No DROP TABLE or DROP COLUMN on existing tables
  ✅ No CONCURRENTLY concern — new table, no concurrent traffic
  ✅ No lock_timeout concern — new table
  ✅ Downgrade present
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "0016_add_host_identities"
down_revision = "0015_repair_alter_table_ddl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "host_identities" not in inspector.get_table_names():
        op.create_table(
            "host_identities",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("short_hostname", sa.String(256), nullable=False),
            sa.Column("fqdn", sa.String(512), nullable=True),
            sa.Column("ip_addresses", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("r7_resource_id", sa.UUID(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column(
                "vsphere_resource_id", sa.UUID(), sa.ForeignKey("resources.id"), nullable=True
            ),
            sa.Column(
                "octopus_resource_id", sa.UUID(), sa.ForeignKey("resources.id"), nullable=True
            ),
            sa.Column(
                "linux_resource_id", sa.UUID(), sa.ForeignKey("resources.id"), nullable=True
            ),
            sa.Column(
                "windows_resource_id", sa.UUID(), sa.ForeignKey("resources.id"), nullable=True
            ),
            sa.Column("os_family", sa.String(64), nullable=True),
            sa.Column("risk_score", sa.Integer(), nullable=True),
            sa.Column("vuln_count", sa.Integer(), nullable=True),
            sa.Column("patch_status", sa.String(64), nullable=True),
            sa.Column("vsphere_power_state", sa.String(32), nullable=True),
            sa.Column("octopus_machine_status", sa.String(64), nullable=True),
            sa.Column("last_reconciled", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("short_hostname", name="uq_host_identities_short_hostname"),
        )
        op.create_index(
            "ix_host_identities_short_hostname",
            "host_identities",
            ["short_hostname"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "host_identities" in inspector.get_table_names():
        op.drop_index("ix_host_identities_short_hostname", table_name="host_identities")
        op.drop_table("host_identities")
