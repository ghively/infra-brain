"""vsphere_phaseE_datastore_attrs_metrics

Revision ID: 65f1043a2504
Revises: 7af94c999f92
Create Date: 2026-07-13 18:23:36.484392

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "65f1043a2504"
down_revision: Union[str, Sequence[str], None] = "7af94c999f92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Guards the new vsphere_datastore_metrics CREATE TABLE + indexes with an
    existence check for idempotency on persistent CI runners.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "vsphere_datastore_metrics" not in tables:
        op.create_table(
            "vsphere_datastore_metrics",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("vcenter", sa.String(length=256), nullable=False),
            sa.Column("moref", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=256), nullable=False),
            sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("capacity_gb", sa.Float(), nullable=True),
            sa.Column("free_gb", sa.Float(), nullable=True),
            sa.Column("used_gb", sa.Float(), nullable=True),
            sa.Column("used_pct", sa.Float(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_vsphere_datastore_metrics_collected_at"),
            "vsphere_datastore_metrics",
            ["collected_at"],
            unique=False,
        )
        op.create_index(
            op.f("ix_vsphere_datastore_metrics_moref"),
            "vsphere_datastore_metrics",
            ["moref"],
            unique=False,
        )
        op.create_index(
            "ix_vsphere_datastore_metrics_moref_collected_at",
            "vsphere_datastore_metrics",
            ["moref", "collected_at"],
            unique=False,
        )

    # add_column targets vsphere_datastores, created by 0007_vsphere_relational.
    # Guard: migrations 0002/0003/0014 call Base.metadata.create_all() with no
    # table filter, pre-creating vsphere_datastores with the current model on a
    # fresh DB — which already includes these columns.
    existing_cols_ds = {col["name"] for col in inspector.get_columns("vsphere_datastores")}
    if "sioc_enabled" not in existing_cols_ds:
        op.add_column("vsphere_datastores", sa.Column("sioc_enabled", sa.Boolean(), nullable=True))
        op.add_column(
            "vsphere_datastores", sa.Column("vmfs_version", sa.String(length=32), nullable=True)
        )
        op.add_column("vsphere_datastores", sa.Column("ssd", sa.Boolean(), nullable=True))
        op.add_column(
            "vsphere_datastores", sa.Column("remote_host", sa.String(length=256), nullable=True)
        )
        op.add_column(
            "vsphere_datastores", sa.Column("remote_path", sa.String(length=512), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("vsphere_datastores", "remote_path")
    op.drop_column("vsphere_datastores", "remote_host")
    op.drop_column("vsphere_datastores", "ssd")
    op.drop_column("vsphere_datastores", "vmfs_version")
    op.drop_column("vsphere_datastores", "sioc_enabled")
    op.drop_index(
        "ix_vsphere_datastore_metrics_moref_collected_at", table_name="vsphere_datastore_metrics"
    )
    op.drop_index(
        op.f("ix_vsphere_datastore_metrics_moref"), table_name="vsphere_datastore_metrics"
    )
    op.drop_index(
        op.f("ix_vsphere_datastore_metrics_collected_at"), table_name="vsphere_datastore_metrics"
    )
    op.drop_table("vsphere_datastore_metrics")
