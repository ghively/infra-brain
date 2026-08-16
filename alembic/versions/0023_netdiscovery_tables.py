"""Add net_discovery_hosts and net_discovery_services tables.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-30

Idempotent, additive migration: creates the two new tables for the netdiscovery
agent only when they do not already exist.  No existing tables are altered.

Safety checklist:
  All columns that reference resources.id use ON DELETE SET NULL — deleting a
    Resource never cascades a host delete.
  net_discovery_services.host_id uses ON DELETE CASCADE — services are owned
    by their host row and have no independent identity.
  All columns are nullable where the spec marks them optional.
  No NOT NULL constraints added to existing tables.
  Every CREATE TABLE / ADD INDEX is guarded by an inspector existence check so
    re-running the migration on a DB where it already landed is a no-op.

Renumbered from 0022 to 0023: migration 0022 (e5f6a7b8c9d0) was claimed by the
merged P1 fix (add_detail_rows_written). down_revision updated accordingly.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID as _PG_UUID

# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

# Cross-dialect UUID: PostgreSQL UUID, CHAR(32) elsewhere.
_UUID = sa.Uuid().with_variant(_PG_UUID(as_uuid=True), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    is_sqlite = bind.dialect.name == "sqlite"

    # ------------------------------------------------------------------ #
    # net_discovery_hosts
    # ------------------------------------------------------------------ #
    if "net_discovery_hosts" not in existing_tables:
        op.create_table(
            "net_discovery_hosts",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("ip", sa.String(45), nullable=False),
            sa.Column("mac", sa.String(17), nullable=True),
            sa.Column("mac_vendor", sa.String(128), nullable=True),
            sa.Column("hostname", sa.String(256), nullable=True),
            sa.Column(
                "first_seen",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "last_seen",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("responded", sa.Boolean, server_default="false", nullable=False),
            sa.Column("discovery_tier", sa.String(16), server_default="passive", nullable=False),
            sa.Column("is_fragile", sa.Boolean, server_default="false", nullable=False),
            sa.Column("is_known", sa.Boolean, server_default="false", nullable=False),
            sa.Column("is_shadow_it", sa.Boolean, server_default="false", nullable=False),
            sa.Column("threat_level", sa.String(8), server_default="none", nullable=False),
            sa.Column("resource_id", _UUID, nullable=True),
            sa.Column("zone", sa.String(32), server_default="corpor", nullable=False),
        )
        op.create_index("ix_net_discovery_hosts_ip", "net_discovery_hosts", ["ip"], unique=True)
        op.create_index(
            "ix_net_discovery_hosts_resource_id", "net_discovery_hosts", ["resource_id"]
        )

        # FK constraints — skip on SQLite (no ALTER TABLE … ADD CONSTRAINT support).
        if not is_sqlite:
            op.create_foreign_key(
                "fk_net_discovery_hosts_resource",
                "net_discovery_hosts",
                "resources",
                ["resource_id"],
                ["id"],
                ondelete="SET NULL",
            )

    # ------------------------------------------------------------------ #
    # net_discovery_services
    # ------------------------------------------------------------------ #
    if "net_discovery_services" not in existing_tables:
        op.create_table(
            "net_discovery_services",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("host_id", _UUID, nullable=False),
            sa.Column("port", sa.Integer, nullable=False),
            sa.Column("proto", sa.String(8), nullable=False),
            sa.Column("service", sa.String(128), nullable=True),
            sa.Column("banner", sa.Text, nullable=True),
            sa.Column("fingerprint", sa.Text, nullable=True),
            sa.Column("is_dangerous", sa.Boolean, server_default="false", nullable=False),
            sa.Column("is_suspicious", sa.Boolean, server_default="false", nullable=False),
            sa.Column(
                "last_seen",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_net_discovery_services_host_id", "net_discovery_services", ["host_id"]
        )
        op.create_index(
            "uq_net_discovery_services_host_port_proto",
            "net_discovery_services",
            ["host_id", "port", "proto"],
            unique=True,
        )

        if not is_sqlite:
            op.create_foreign_key(
                "fk_net_discovery_services_host",
                "net_discovery_services",
                "net_discovery_hosts",
                ["host_id"],
                ["id"],
                ondelete="CASCADE",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "net_discovery_services" in existing_tables:
        op.drop_table("net_discovery_services")

    if "net_discovery_hosts" in existing_tables:
        op.drop_table("net_discovery_hosts")
