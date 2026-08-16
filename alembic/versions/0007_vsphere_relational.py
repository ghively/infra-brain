"""vSphere relational inventory + time-series metrics model.

Revision ID: 0007_vsphere_relational
Revises: 0006_iac_cicd_relational
Create Date: 2026-06-23

Adds the pragmatic-relational vSphere model. INVENTORY tables are slow-changing
and key on the natural pair (vcenter, moref) — vCenter morefs are the stable
vCenter identity. METRICS tables are append-only time-series, one row per
collection sample, indexed on (moref, collected_at).

  Inventory (UniqueConstraint + index on vcenter+moref):
    * vsphere_datacenters
    * vsphere_clusters
    * vsphere_hosts
    * vsphere_vms              (+ indexed uuid column)
    * vsphere_datastores
    * vsphere_networks
    * vsphere_resource_pools

  Metrics (append-only; index on moref and (moref, collected_at)):
    * vsphere_vm_metrics
    * vsphere_host_metrics

Each inventory table carries a JSONB ``details`` column for nested extras and a
nullable ``resource_id`` FK back to ``resources``.

Note: 0001 seeds a fresh DB via ``Base.metadata.create_all()`` against the live
ORM, so on a brand-new install these tables already exist when this migration
runs. We therefore guard every create with the inspector (mirroring 0005/0006).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

revision = "0007_vsphere_relational"
down_revision = "0006_iac_cicd_relational"
branch_labels = None
depends_on = None

# Cross-dialect JSON: JSONB on PostgreSQL, JSON elsewhere (mirrors models.py).
_JSONB = sa.JSON().with_variant(_PG_JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "vsphere_datacenters" not in tables:
        op.create_table(
            "vsphere_datacenters",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("vcenter", sa.String(256), nullable=False),
            sa.Column("moref", sa.String(128), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("overall_status", sa.String(32), nullable=True),
            sa.Column("parent", sa.String(256), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("vcenter", "moref", name="uq_vsphere_datacenter_natkey"),
        )
        op.create_index(
            "ix_vsphere_datacenters_natkey", "vsphere_datacenters", ["vcenter", "moref"]
        )

    if "vsphere_clusters" not in tables:
        op.create_table(
            "vsphere_clusters",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("vcenter", sa.String(256), nullable=False),
            sa.Column("moref", sa.String(128), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("overall_status", sa.String(32), nullable=True),
            sa.Column("datacenter_name", sa.String(256), nullable=True),
            sa.Column("ha_enabled", sa.Boolean(), nullable=True),
            sa.Column("drs_enabled", sa.Boolean(), nullable=True),
            sa.Column("drs_default_behavior", sa.String(64), nullable=True),
            sa.Column("num_hosts", sa.Integer(), nullable=True),
            sa.Column("num_effective_hosts", sa.Integer(), nullable=True),
            sa.Column("total_cpu_mhz", sa.Integer(), nullable=True),
            sa.Column("total_memory_gb", sa.Float(), nullable=True),
            sa.Column("num_cpu_cores", sa.Integer(), nullable=True),
            sa.Column("host_names", _JSONB, nullable=True),
            sa.Column("datastore_names", _JSONB, nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("vcenter", "moref", name="uq_vsphere_cluster_natkey"),
        )
        op.create_index("ix_vsphere_clusters_natkey", "vsphere_clusters", ["vcenter", "moref"])

    if "vsphere_hosts" not in tables:
        op.create_table(
            "vsphere_hosts",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("vcenter", sa.String(256), nullable=False),
            sa.Column("moref", sa.String(128), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("overall_status", sa.String(32), nullable=True),
            sa.Column("hardware_uuid", sa.String(64), nullable=True),
            sa.Column("cluster_name", sa.String(256), nullable=True),
            sa.Column("vendor", sa.String(256), nullable=True),
            sa.Column("model", sa.String(256), nullable=True),
            sa.Column("cpu_model", sa.String(256), nullable=True),
            sa.Column("num_cpu_cores", sa.Integer(), nullable=True),
            sa.Column("num_cpu_threads", sa.Integer(), nullable=True),
            sa.Column("cpu_mhz", sa.Integer(), nullable=True),
            sa.Column("memory_gb", sa.Float(), nullable=True),
            sa.Column("num_nics", sa.Integer(), nullable=True),
            sa.Column("num_hbas", sa.Integer(), nullable=True),
            sa.Column("version", sa.String(64), nullable=True),
            sa.Column("build", sa.String(64), nullable=True),
            sa.Column("connection_state", sa.String(32), nullable=True),
            sa.Column("power_state", sa.String(32), nullable=True),
            sa.Column("in_maintenance_mode", sa.Boolean(), nullable=True),
            sa.Column("dns_hostname", sa.String(256), nullable=True),
            sa.Column("vm_count", sa.Integer(), nullable=True),
            sa.Column("datastore_names", _JSONB, nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("vcenter", "moref", name="uq_vsphere_host_natkey"),
        )
        op.create_index("ix_vsphere_hosts_natkey", "vsphere_hosts", ["vcenter", "moref"])

    if "vsphere_vms" not in tables:
        op.create_table(
            "vsphere_vms",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("vcenter", sa.String(256), nullable=False),
            sa.Column("moref", sa.String(128), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("overall_status", sa.String(32), nullable=True),
            sa.Column("uuid", sa.String(64), nullable=True),
            sa.Column("instance_uuid", sa.String(64), nullable=True),
            sa.Column("is_template", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("guest_full_name", sa.String(256), nullable=True),
            sa.Column("guest_id", sa.String(128), nullable=True),
            sa.Column("num_cpu", sa.Integer(), nullable=True),
            sa.Column("memory_mb", sa.Integer(), nullable=True),
            sa.Column("cores_per_socket", sa.Integer(), nullable=True),
            sa.Column("hw_version", sa.String(32), nullable=True),
            sa.Column("power_state", sa.String(32), nullable=True),
            sa.Column("boot_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("esxi_host", sa.String(256), nullable=True),
            sa.Column("guest_hostname", sa.String(256), nullable=True),
            sa.Column("ip_address", sa.String(64), nullable=True),
            sa.Column("all_ips", _JSONB, nullable=True),
            sa.Column("tools_status", sa.String(64), nullable=True),
            sa.Column("tools_version", sa.String(64), nullable=True),
            sa.Column("datastore_names", _JSONB, nullable=True),
            sa.Column("network_names", _JSONB, nullable=True),
            sa.Column("snapshot_count", sa.Integer(), nullable=True),
            sa.Column("annotation", sa.Text(), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("vcenter", "moref", name="uq_vsphere_vm_natkey"),
        )
        op.create_index("ix_vsphere_vms_natkey", "vsphere_vms", ["vcenter", "moref"])
        op.create_index("ix_vsphere_vms_uuid", "vsphere_vms", ["uuid"])

    if "vsphere_datastores" not in tables:
        op.create_table(
            "vsphere_datastores",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("vcenter", sa.String(256), nullable=False),
            sa.Column("moref", sa.String(128), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("overall_status", sa.String(32), nullable=True),
            sa.Column("datastore_type", sa.String(32), nullable=True),
            sa.Column("capacity_gb", sa.Float(), nullable=True),
            sa.Column("free_gb", sa.Float(), nullable=True),
            sa.Column("used_gb", sa.Float(), nullable=True),
            sa.Column("used_pct", sa.Float(), nullable=True),
            sa.Column("accessible", sa.Boolean(), nullable=True),
            sa.Column("maintenance_mode", sa.Boolean(), nullable=True),
            sa.Column("host_count", sa.Integer(), nullable=True),
            sa.Column("vm_count", sa.Integer(), nullable=True),
            sa.Column("url", sa.String(512), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("vcenter", "moref", name="uq_vsphere_datastore_natkey"),
        )
        op.create_index("ix_vsphere_datastores_natkey", "vsphere_datastores", ["vcenter", "moref"])

    if "vsphere_networks" not in tables:
        op.create_table(
            "vsphere_networks",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("vcenter", sa.String(256), nullable=False),
            sa.Column("moref", sa.String(128), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("overall_status", sa.String(32), nullable=True),
            sa.Column("network_kind", sa.String(32), nullable=False),
            sa.Column("accessible", sa.Boolean(), nullable=True),
            sa.Column("num_ports", sa.Integer(), nullable=True),
            sa.Column("dvs_uuid", sa.String(64), nullable=True),
            sa.Column("version", sa.String(64), nullable=True),
            sa.Column("host_count", sa.Integer(), nullable=True),
            sa.Column("vm_count", sa.Integer(), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("vcenter", "moref", name="uq_vsphere_network_natkey"),
        )
        op.create_index("ix_vsphere_networks_natkey", "vsphere_networks", ["vcenter", "moref"])

    if "vsphere_resource_pools" not in tables:
        op.create_table(
            "vsphere_resource_pools",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("vcenter", sa.String(256), nullable=False),
            sa.Column("moref", sa.String(128), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("overall_status", sa.String(32), nullable=True),
            sa.Column("cpu_limit", sa.Integer(), nullable=True),
            sa.Column("cpu_reservation", sa.Integer(), nullable=True),
            sa.Column("memory_limit", sa.Integer(), nullable=True),
            sa.Column("memory_reservation", sa.Integer(), nullable=True),
            sa.Column("cpu_usage_mhz", sa.Integer(), nullable=True),
            sa.Column("memory_usage_mb", sa.Integer(), nullable=True),
            sa.Column("vm_count", sa.Integer(), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("vcenter", "moref", name="uq_vsphere_resource_pool_natkey"),
        )
        op.create_index(
            "ix_vsphere_resource_pools_natkey",
            "vsphere_resource_pools",
            ["vcenter", "moref"],
        )

    if "vsphere_vm_metrics" not in tables:
        op.create_table(
            "vsphere_vm_metrics",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("vcenter", sa.String(256), nullable=False),
            sa.Column("moref", sa.String(128), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("power_state", sa.String(32), nullable=True),
            sa.Column("cpu_usage_mhz", sa.Integer(), nullable=True),
            sa.Column("memory_usage_mb", sa.Integer(), nullable=True),
            sa.Column("uptime_seconds", sa.BigInteger(), nullable=True),
            sa.Column("ballooned_memory_mb", sa.Integer(), nullable=True),
            sa.Column("overall_status", sa.String(32), nullable=True),
            sa.Column("ip_address", sa.String(64), nullable=True),
            sa.Column("perf_cpu_usage_pct_avg", sa.Float(), nullable=True),
            sa.Column("perf_cpu_ready_ms_sum", sa.Float(), nullable=True),
            sa.Column("perf_mem_usage_pct_avg", sa.Float(), nullable=True),
            sa.Column("perf_disk_read_kbps_avg", sa.Float(), nullable=True),
            sa.Column("perf_disk_write_kbps_avg", sa.Float(), nullable=True),
            sa.Column("perf_net_rx_kbps_avg", sa.Float(), nullable=True),
            sa.Column("perf_net_tx_kbps_avg", sa.Float(), nullable=True),
            sa.Column("perf_samples", sa.Integer(), nullable=True),
            sa.Column("perf_interval_id", sa.Integer(), nullable=True),
        )
        op.create_index("ix_vsphere_vm_metrics_moref", "vsphere_vm_metrics", ["moref"])
        op.create_index(
            "ix_vsphere_vm_metrics_collected_at", "vsphere_vm_metrics", ["collected_at"]
        )
        op.create_index(
            "ix_vsphere_vm_metrics_moref_collected_at",
            "vsphere_vm_metrics",
            ["moref", "collected_at"],
        )

    if "vsphere_host_metrics" not in tables:
        op.create_table(
            "vsphere_host_metrics",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("vcenter", sa.String(256), nullable=False),
            sa.Column("moref", sa.String(128), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("connection_state", sa.String(32), nullable=True),
            sa.Column("in_maintenance_mode", sa.Boolean(), nullable=True),
            sa.Column("power_state", sa.String(32), nullable=True),
            sa.Column("cpu_usage_mhz", sa.Integer(), nullable=True),
            sa.Column("memory_usage_mb", sa.Integer(), nullable=True),
            sa.Column("uptime_seconds", sa.BigInteger(), nullable=True),
            sa.Column("overall_status", sa.String(32), nullable=True),
        )
        op.create_index("ix_vsphere_host_metrics_moref", "vsphere_host_metrics", ["moref"])
        op.create_index(
            "ix_vsphere_host_metrics_collected_at", "vsphere_host_metrics", ["collected_at"]
        )
        op.create_index(
            "ix_vsphere_host_metrics_moref_collected_at",
            "vsphere_host_metrics",
            ["moref", "collected_at"],
        )


def downgrade() -> None:
    # Metrics first, then inventory (no inter-table FKs, but keep it tidy).
    for tbl in (
        "vsphere_host_metrics",
        "vsphere_vm_metrics",
        "vsphere_resource_pools",
        "vsphere_networks",
        "vsphere_datastores",
        "vsphere_vms",
        "vsphere_hosts",
        "vsphere_clusters",
        "vsphere_datacenters",
    ):
        op.drop_table(tbl)
