"""MR-J: INV-4 host posture tables, host purpose/VLAN map, and INV long-tail
(LinuxMount/LinuxNic) tables.

Revision ID: 1eaf3df70543
Revises: 8de46a9884f2
Create Date: 2026-07-07

Merge-time reconciliation (done): originally chained onto 0026's descendant
d1e2f3a4b5c6 directly, in parallel with MR-H's 0030 migration (also chained
there). Renumbered 0030 -> 0031 and down_revision repointed onto MR-H's real
0030 (``8de46a9884f2``), the correct end of the merged chain.

docs/audit/FLEET_INVENTORY_AND_VSPHERE_AUDIT_2026-07-06.md flagged several
categories of PCI-relevant data (certificates, firewall/AV/RDP/UAC/SSH/
SELinux posture, SMB/NFS shares, Windows local admins/groups) that the
fleet-ansible playbook already collects but that had no home anywhere in
infra-brain's schema, plus a curated host-purpose/VLAN map that exists only
as tribal knowledge, plus two long-tail categories (per-volume disk usage,
per-NIC network detail) that ``ansible -m setup`` already gathers but nothing
persists per-host.

Idempotent, additive migration: every CREATE TABLE is guarded by an
inspector existence check so re-running on a DB where it already landed is a
no-op. No existing table is altered.

Safety checklist:
  All resource_id FKs use ON DELETE CASCADE — these are all detail rows that
    have no independent identity once their host Resource is gone (same
    convention as windows_services/windows_software in migration c3d4e5f6a7b8).
  linux_mounts/linux_nics FK to linux_hosts.id with no explicit ondelete
    (matches the existing linux_packages/linux_services/... convention in the
    original models.py migration, which also has no ondelete set).
  All columns nullable where the model marks them Optional.
  No NOT NULL added to any existing table.
  FK constraints are skipped on SQLite (no ALTER TABLE ADD CONSTRAINT support),
    matching every prior migration in this chain.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
from sqlalchemy.dialects.postgresql import UUID as _PG_UUID

# revision identifiers, used by Alembic.
revision = "1eaf3df70543"
down_revision = "8de46a9884f2"
branch_labels = None
depends_on = None

# Cross-dialect UUID: PostgreSQL UUID, CHAR(32) elsewhere.
_UUID = sa.Uuid().with_variant(_PG_UUID(as_uuid=True), "postgresql")
# Cross-dialect JSON: JSONB on PostgreSQL, JSON elsewhere (mirrors db/models/_base.py).
_JSONB = sa.JSON().with_variant(_PG_JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    is_sqlite = bind.dialect.name == "sqlite"

    # ------------------------------------------------------------------ #
    # host_certificates
    # ------------------------------------------------------------------ #
    if "host_certificates" not in existing_tables:
        op.create_table(
            "host_certificates",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("resource_id", _UUID, nullable=False),
            sa.Column("store", sa.String(128), nullable=False),
            sa.Column("subject", sa.Text, nullable=True),
            sa.Column("issuer", sa.Text, nullable=True),
            sa.Column("thumbprint", sa.String(64), nullable=False, server_default=""),
            sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
            sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
            sa.Column("days_until_expiry", sa.Integer, nullable=True),
            sa.Column("is_expired", sa.Boolean, server_default="false", nullable=False),
            sa.Column(
                "collected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index("ix_host_certificates_resource_id", "host_certificates", ["resource_id"])
        op.create_index(
            "uq_host_cert_natkey",
            "host_certificates",
            ["resource_id", "store", "thumbprint"],
            unique=True,
        )
        if not is_sqlite:
            op.create_foreign_key(
                "fk_host_certificates_resource",
                "host_certificates",
                "resources",
                ["resource_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # ------------------------------------------------------------------ #
    # host_security_posture
    # ------------------------------------------------------------------ #
    if "host_security_posture" not in existing_tables:
        op.create_table(
            "host_security_posture",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("resource_id", _UUID, nullable=False),
            sa.Column("firewall_enabled", sa.Boolean, nullable=True),
            sa.Column("firewall_service", sa.String(64), nullable=True),
            sa.Column("av_enabled", sa.Boolean, nullable=True),
            sa.Column("av_product", sa.String(128), nullable=True),
            sa.Column("av_signature_date", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rdp_enabled", sa.Boolean, nullable=True),
            sa.Column("uac_enabled", sa.Boolean, nullable=True),
            sa.Column("ssh_password_auth", sa.Boolean, nullable=True),
            sa.Column("ssh_permit_root_login", sa.Boolean, nullable=True),
            sa.Column("ssh_pubkey_auth", sa.Boolean, nullable=True),
            sa.Column("selinux_mode", sa.String(32), nullable=True),
            sa.Column("apparmor_status", sa.String(32), nullable=True),
            sa.Column(
                "collected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index(
            "ix_host_security_posture_resource_id",
            "host_security_posture",
            ["resource_id"],
            unique=True,
        )
        if not is_sqlite:
            op.create_foreign_key(
                "fk_host_security_posture_resource",
                "host_security_posture",
                "resources",
                ["resource_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # ------------------------------------------------------------------ #
    # host_shares
    # ------------------------------------------------------------------ #
    if "host_shares" not in existing_tables:
        op.create_table(
            "host_shares",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("resource_id", _UUID, nullable=False),
            sa.Column("share_type", sa.String(16), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("path", sa.Text, nullable=True),
            sa.Column("permissions", _JSONB, nullable=True),
            sa.Column(
                "collected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index("ix_host_shares_resource_id", "host_shares", ["resource_id"])
        op.create_index(
            "uq_host_share_natkey", "host_shares", ["resource_id", "share_type", "name"], unique=True
        )
        if not is_sqlite:
            op.create_foreign_key(
                "fk_host_shares_resource",
                "host_shares",
                "resources",
                ["resource_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # ------------------------------------------------------------------ #
    # windows_local_users
    # ------------------------------------------------------------------ #
    if "windows_local_users" not in existing_tables:
        op.create_table(
            "windows_local_users",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("resource_id", _UUID, nullable=False),
            sa.Column("username", sa.String(256), nullable=False),
            sa.Column("enabled", sa.Boolean, nullable=True),
            sa.Column("is_admin", sa.Boolean, server_default="false", nullable=False),
            sa.Column("last_logon", sa.DateTime(timezone=True), nullable=True),
            sa.Column("password_required", sa.Boolean, nullable=True),
            sa.Column("password_never_expires", sa.Boolean, nullable=True),
            sa.Column(
                "collected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index("ix_windows_local_users_resource_id", "windows_local_users", ["resource_id"])
        op.create_index(
            "uq_windows_local_user_natkey",
            "windows_local_users",
            ["resource_id", "username"],
            unique=True,
        )
        if not is_sqlite:
            op.create_foreign_key(
                "fk_windows_local_users_resource",
                "windows_local_users",
                "resources",
                ["resource_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # ------------------------------------------------------------------ #
    # windows_local_group_members
    # ------------------------------------------------------------------ #
    if "windows_local_group_members" not in existing_tables:
        op.create_table(
            "windows_local_group_members",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("resource_id", _UUID, nullable=False),
            sa.Column("group_name", sa.String(128), nullable=False),
            sa.Column("member_name", sa.String(256), nullable=False),
            sa.Column(
                "collected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index(
            "ix_windows_local_group_members_resource_id",
            "windows_local_group_members",
            ["resource_id"],
        )
        op.create_index(
            "uq_windows_local_group_member_natkey",
            "windows_local_group_members",
            ["resource_id", "group_name", "member_name"],
            unique=True,
        )
        if not is_sqlite:
            op.create_foreign_key(
                "fk_windows_local_group_members_resource",
                "windows_local_group_members",
                "resources",
                ["resource_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # ------------------------------------------------------------------ #
    # windows_logon_events (schema-only in this MR — no writer wired yet)
    # ------------------------------------------------------------------ #
    if "windows_logon_events" not in existing_tables:
        op.create_table(
            "windows_logon_events",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("resource_id", _UUID, nullable=False),
            sa.Column("event_type", sa.String(16), nullable=False),
            sa.Column("username", sa.String(256), nullable=True),
            sa.Column("logon_type", sa.String(32), nullable=True),
            sa.Column("source_ip", sa.String(64), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "collected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index("ix_windows_logon_events_resource_id", "windows_logon_events", ["resource_id"])
        if not is_sqlite:
            op.create_foreign_key(
                "fk_windows_logon_events_resource",
                "windows_logon_events",
                "resources",
                ["resource_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # ------------------------------------------------------------------ #
    # host_purpose_map (MR-J item 3 — curated VLAN/host-purpose knowledge)
    # ------------------------------------------------------------------ #
    if "host_purpose_map" not in existing_tables:
        op.create_table(
            "host_purpose_map",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("hostname", sa.String(256), nullable=False),
            sa.Column("purpose", sa.Text, nullable=True),
            sa.Column("vlan", sa.String(64), nullable=True),
            sa.Column("subnet", sa.String(64), nullable=True),
            sa.Column("source", sa.String(256), server_default="", nullable=False),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index(
            "uq_host_purpose_map_hostname", "host_purpose_map", ["hostname"], unique=True
        )

    # ------------------------------------------------------------------ #
    # linux_mounts (INV long-tail — already-gathered ansible_mounts fact)
    # ------------------------------------------------------------------ #
    if "linux_mounts" not in existing_tables:
        op.create_table(
            "linux_mounts",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("host_id", _UUID, nullable=False),
            sa.Column("mount", sa.String(256), nullable=False),
            sa.Column("device", sa.String(256), nullable=True),
            sa.Column("fstype", sa.String(64), nullable=True),
            sa.Column("size_total_gb", sa.Float, nullable=True),
            sa.Column("size_available_gb", sa.Float, nullable=True),
        )
        op.create_index("ix_linux_mounts_host_id", "linux_mounts", ["host_id"])
        op.create_index(
            "uq_linux_mount_natkey", "linux_mounts", ["host_id", "mount"], unique=True
        )
        if not is_sqlite:
            op.create_foreign_key(
                "fk_linux_mounts_host",
                "linux_mounts",
                "linux_hosts",
                ["host_id"],
                ["id"],
            )

    # ------------------------------------------------------------------ #
    # linux_nics (INV long-tail — already-gathered setup interface facts)
    # ------------------------------------------------------------------ #
    if "linux_nics" not in existing_tables:
        op.create_table(
            "linux_nics",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("host_id", _UUID, nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("mac", sa.String(32), nullable=True),
            sa.Column("ipv4", sa.String(64), nullable=True),
            sa.Column("ipv6", sa.String(64), nullable=True),
            sa.Column("speed_mbps", sa.Integer, nullable=True),
        )
        op.create_index("ix_linux_nics_host_id", "linux_nics", ["host_id"])
        op.create_index("uq_linux_nic_natkey", "linux_nics", ["host_id", "name"], unique=True)
        if not is_sqlite:
            op.create_foreign_key(
                "fk_linux_nics_host",
                "linux_nics",
                "linux_hosts",
                ["host_id"],
                ["id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    for table in (
        "linux_nics",
        "linux_mounts",
        "host_purpose_map",
        "windows_logon_events",
        "windows_local_group_members",
        "windows_local_users",
        "host_shares",
        "host_security_posture",
        "host_certificates",
    ):
        if table in existing_tables:
            op.drop_table(table)
