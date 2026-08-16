"""vsphere_phaseB_vm_governance_security_disks_snapshots

Revision ID: eeda3039b7b1
Revises: 00cb512bde05
Create Date: 2026-07-13 17:58:01.196584

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "eeda3039b7b1"
down_revision: Union[str, Sequence[str], None] = "00cb512bde05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Guards every CREATE TABLE + its CREATE INDEX with an existence check so
    that the migration is idempotent on a database where Base.metadata.create_all()
    was called before this migration was authored. op.add_column calls are also
    guarded: migrations 0002/0003/0014 call Base.metadata.create_all() with no
    table filter, pre-creating vsphere_vms with the current model on a fresh DB.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "vsphere_snapshots" not in tables:
        op.create_table(
            "vsphere_snapshots",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("vcenter", sa.String(length=256), nullable=False),
            sa.Column("vm_moref", sa.String(length=128), nullable=False),
            sa.Column("vm_name", sa.String(length=256), nullable=True),
            sa.Column("snapshot_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=256), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("age_days", sa.Integer(), nullable=True),
            sa.Column("is_current", sa.Boolean(), nullable=True),
            sa.Column("state", sa.String(length=32), nullable=True),
            sa.Column(
                "details",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "vcenter", "vm_moref", "snapshot_id", name="uq_vsphere_snapshot_natkey"
            ),
        )
        op.create_index(
            op.f("ix_vsphere_snapshots_vm_moref"), "vsphere_snapshots", ["vm_moref"], unique=False
        )

    if "vsphere_vm_disks" not in tables:
        op.create_table(
            "vsphere_vm_disks",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("vcenter", sa.String(length=256), nullable=False),
            sa.Column("vm_moref", sa.String(length=128), nullable=False),
            sa.Column("vm_name", sa.String(length=256), nullable=True),
            sa.Column("disk_key", sa.Integer(), nullable=False),
            sa.Column("label", sa.String(length=128), nullable=True),
            sa.Column("capacity_gb", sa.Float(), nullable=True),
            sa.Column("thin_provisioned", sa.Boolean(), nullable=True),
            sa.Column("backing_type", sa.String(length=64), nullable=True),
            sa.Column("datastore_name", sa.String(length=256), nullable=True),
            sa.Column("file_path", sa.String(length=512), nullable=True),
            sa.Column(
                "details",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "vcenter", "vm_moref", "disk_key", name="uq_vsphere_vm_disk_natkey"
            ),
        )
        op.create_index(
            op.f("ix_vsphere_vm_disks_vm_moref"), "vsphere_vm_disks", ["vm_moref"], unique=False
        )

    # add_column targets vsphere_vms, created by 0007_vsphere_relational.
    # Guard: migrations 0002/0003/0014 call Base.metadata.create_all() with NO
    # table filter. After 0001 was fixed to exclude later-migration tables, those
    # unbounded calls now pre-create vsphere_vms from the CURRENT model — which
    # includes every column this migration is supposed to add. Without this guard
    # the migration fails with DuplicateColumn on every clean-DB CI run.
    existing_cols_vms = {col["name"] for col in inspector.get_columns("vsphere_vms")}
    if "cpu_reservation_mhz" not in existing_cols_vms:
        op.add_column("vsphere_vms", sa.Column("cpu_reservation_mhz", sa.Integer(), nullable=True))
        op.add_column("vsphere_vms", sa.Column("cpu_limit_mhz", sa.Integer(), nullable=True))
        op.add_column("vsphere_vms", sa.Column("mem_reservation_mb", sa.Integer(), nullable=True))
        op.add_column("vsphere_vms", sa.Column("mem_limit_mb", sa.Integer(), nullable=True))
        op.add_column("vsphere_vms", sa.Column("firmware", sa.String(length=16), nullable=True))
        op.add_column("vsphere_vms", sa.Column("secure_boot", sa.Boolean(), nullable=True))
        op.add_column("vsphere_vms", sa.Column("encrypted", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("vsphere_vms", "encrypted")
    op.drop_column("vsphere_vms", "secure_boot")
    op.drop_column("vsphere_vms", "firmware")
    op.drop_column("vsphere_vms", "mem_limit_mb")
    op.drop_column("vsphere_vms", "mem_reservation_mb")
    op.drop_column("vsphere_vms", "cpu_limit_mhz")
    op.drop_column("vsphere_vms", "cpu_reservation_mhz")
    op.drop_index(op.f("ix_vsphere_vm_disks_vm_moref"), table_name="vsphere_vm_disks")
    op.drop_table("vsphere_vm_disks")
    op.drop_index(op.f("ix_vsphere_snapshots_vm_moref"), table_name="vsphere_snapshots")
    op.drop_table("vsphere_snapshots")
