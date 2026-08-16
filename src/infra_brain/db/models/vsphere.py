"""vSphere relational inventory + time-series metrics.

Bucket: VsphereDatacenter, VsphereCluster, VsphereHost, VsphereVm,
        VsphereDatastore, VsphereNetwork, VsphereResourcePool,
        VsphereVmMetric, VsphereHostMetric.

Design notes (preserved from original models.py):
- vCenter morefs (managed object references, e.g. "vm-1234") are the stable
  vCenter identity, so every vSphere row keys on the natural pair
  (vcenter, moref).
- INVENTORY tables are slow-changing — the collector upserts them by that
  natural key.
- METRICS tables are append-only time-series, one row per collection sample,
  indexed on (moref, collected_at).
- ``resource_id`` is a nullable FK back to the canonical ``Resource`` handle.
- A JSONB ``details`` column on each inventory table carries nested/rarely-
  queried extras. Resolved name columns are denormalized from morefs by the
  collector so queries need not re-resolve them.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONB, _now, _uuid


class VsphereDatacenter(Base):
    __tablename__ = "vsphere_datacenters"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    vcenter: Mapped[str] = mapped_column(String(256))
    moref: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    overall_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    parent: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("vcenter", "moref", name="uq_vsphere_datacenter_natkey"),)


class VsphereCluster(Base):
    __tablename__ = "vsphere_clusters"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    vcenter: Mapped[str] = mapped_column(String(256))
    moref: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    overall_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    datacenter_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    ha_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    drs_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    drs_default_behavior: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    num_hosts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    num_effective_hosts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_cpu_mhz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_memory_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    num_cpu_cores: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    host_names: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    datastore_names: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # Phase D — DRS/EVC/HA governance.
    evc_mode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ha_admission_control: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    drs_rules: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True, default=list)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("vcenter", "moref", name="uq_vsphere_cluster_natkey"),)


class VsphereHost(Base):
    __tablename__ = "vsphere_hosts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    vcenter: Mapped[str] = mapped_column(String(256))
    moref: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    overall_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    hardware_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    cluster_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    vendor: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    cpu_model: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    num_cpu_cores: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    num_cpu_threads: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_mhz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    num_nics: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    num_hbas: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    build: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    connection_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    power_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    in_maintenance_mode: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    dns_hostname: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    vm_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    datastore_names: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # Phase C — hardening posture (PCI/CIS) + firmware + hardware health.
    # nullable: added to an already-populated table, so no NOT NULL (a JSONB
    # default=list is Python-side only, not a DB server_default).
    ntp_servers: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True, default=list)
    syslog_host: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    lockdown_mode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ssh_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    esxi_shell_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    shell_timeout_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    account_lock_failures: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bios_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    bios_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    hw_health: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    hw_health_issues: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("vcenter", "moref", name="uq_vsphere_host_natkey"),)


class VsphereVm(Base):
    __tablename__ = "vsphere_vms"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    vcenter: Mapped[str] = mapped_column(String(256))
    moref: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    overall_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    instance_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    guest_full_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    guest_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    num_cpu: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cores_per_socket: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hw_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    power_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    boot_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    esxi_host: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    guest_hostname: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    all_ips: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    tools_status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tools_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    datastore_names: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    network_names: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    snapshot_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    annotation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Resource governance (config.cpu/memoryAllocation). limit -1 = unlimited.
    cpu_reservation_mhz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_limit_mhz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mem_reservation_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mem_limit_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Security posture (compliance): firmware bios|efi, EFI secure boot, VM encryption.
    firmware: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    secure_boot: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    encrypted: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # TRK-104: owning resource pool moref (e.g. "resgroup-42"); joins to
    # vsphere_resource_pools.(vcenter, moref) for the KG IN_POOL edge.
    resource_pool_moref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("vcenter", "moref", name="uq_vsphere_vm_natkey"),)


class VsphereDatastore(Base):
    __tablename__ = "vsphere_datastores"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    vcenter: Mapped[str] = mapped_column(String(256))
    moref: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    overall_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    datastore_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    capacity_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    free_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    used_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    used_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    accessible: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    maintenance_mode: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    host_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    vm_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Phase E — storage attributes.
    sioc_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    vmfs_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ssd: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    remote_host: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    remote_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("vcenter", "moref", name="uq_vsphere_datastore_natkey"),)


class VsphereNetwork(Base):
    __tablename__ = "vsphere_networks"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    vcenter: Mapped[str] = mapped_column(String(256))
    moref: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    overall_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # portgroup | dvswitch | dvportgroup
    network_kind: Mapped[str] = mapped_column(String(32))
    accessible: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    num_ports: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dvs_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    host_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    vm_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("vcenter", "moref", name="uq_vsphere_network_natkey"),)


class VsphereResourcePool(Base):
    __tablename__ = "vsphere_resource_pools"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    vcenter: Mapped[str] = mapped_column(String(256))
    moref: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    overall_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    cpu_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_reservation: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_reservation: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_usage_mhz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_usage_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    vm_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("vcenter", "moref", name="uq_vsphere_resource_pool_natkey"),)


class VsphereVmDisk(Base):
    """One virtual disk (VMDK) attached to a VM. Keyed by (vcenter, vm_moref,
    disk_key) — disk_key is the device key, stable per VM."""

    __tablename__ = "vsphere_vm_disks"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    vcenter: Mapped[str] = mapped_column(String(256))
    vm_moref: Mapped[str] = mapped_column(String(128), index=True)
    vm_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    disk_key: Mapped[int] = mapped_column(Integer)
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    capacity_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    thin_provisioned: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    backing_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    datastore_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    file_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    __table_args__ = (
        UniqueConstraint("vcenter", "vm_moref", "disk_key", name="uq_vsphere_vm_disk_natkey"),
    )


class VsphereSnapshot(Base):
    """One VM snapshot. Keyed by (vcenter, vm_moref, snapshot_id). ``age_days``
    is denormalized at collection time so hygiene rules (old-snapshot findings)
    can query without recomputing from created_at."""

    __tablename__ = "vsphere_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    vcenter: Mapped[str] = mapped_column(String(256))
    vm_moref: Mapped[str] = mapped_column(String(128), index=True)
    vm_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    snapshot_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    age_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_current: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    __table_args__ = (
        UniqueConstraint("vcenter", "vm_moref", "snapshot_id", name="uq_vsphere_snapshot_natkey"),
    )


class VsphereLicense(Base):
    """vCenter/ESXi license assignment. vCenter-scoped snapshot (delete-reinsert
    per vcenter each run)."""

    __tablename__ = "vsphere_licenses"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    vcenter: Mapped[str] = mapped_column(String(256), index=True)
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    edition_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expiration: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class VsphereAlarm(Base):
    """A currently-triggered vCenter alarm. vCenter-scoped snapshot."""

    __tablename__ = "vsphere_alarms"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    vcenter: Mapped[str] = mapped_column(String(256), index=True)
    alarm_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    entity_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    overall_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    acknowledged: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class VspherePermission(Base):
    """A vCenter permission (principal → role on an entity). vCenter-scoped snapshot."""

    __tablename__ = "vsphere_permissions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    vcenter: Mapped[str] = mapped_column(String(256), index=True)
    principal: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    role_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    role_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    is_group: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    propagate: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    entity: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)


class VsphereSession(Base):
    """An active vCenter login session (point-in-time snapshot per run)."""

    __tablename__ = "vsphere_sessions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    vcenter: Mapped[str] = mapped_column(String(256), index=True)
    user_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    login_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_active: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)


class VsphereDatastoreMetric(Base):
    """Append-only capacity/free time-series per datastore (storage-growth trend)."""

    __tablename__ = "vsphere_datastore_metrics"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    vcenter: Mapped[str] = mapped_column(String(256))
    moref: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    capacity_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    free_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    used_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    used_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    __table_args__ = (
        Index("ix_vsphere_datastore_metrics_moref_collected_at", "moref", "collected_at"),
    )


class VsphereVmMetric(Base):
    """Append-only time-series sample for a VM (one row per collection)."""

    __tablename__ = "vsphere_vm_metrics"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    vcenter: Mapped[str] = mapped_column(String(256))
    moref: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    # pulse | perf
    source: Mapped[str] = mapped_column(String(16))
    power_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    cpu_usage_mhz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_usage_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    uptime_seconds: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    ballooned_memory_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    overall_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    perf_cpu_usage_pct_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    perf_cpu_ready_ms_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    perf_mem_usage_pct_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    perf_disk_read_kbps_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    perf_disk_write_kbps_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    perf_net_rx_kbps_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    perf_net_tx_kbps_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    perf_samples: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    perf_interval_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    __table_args__ = (Index("ix_vsphere_vm_metrics_moref_collected_at", "moref", "collected_at"),)


class VsphereHostMetric(Base):
    """Append-only time-series sample for an ESXi host (one row per collection)."""

    __tablename__ = "vsphere_host_metrics"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    vcenter: Mapped[str] = mapped_column(String(256))
    moref: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    # pulse | perf
    source: Mapped[str] = mapped_column(String(16))
    connection_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    in_maintenance_mode: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    power_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    cpu_usage_mhz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_usage_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    uptime_seconds: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    overall_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    __table_args__ = (Index("ix_vsphere_host_metrics_moref_collected_at", "moref", "collected_at"),)


# vsphere inventory: natural-key (vcenter, moref) lookups for upsert
Index("ix_vsphere_datacenters_natkey", VsphereDatacenter.vcenter, VsphereDatacenter.moref)
Index("ix_vsphere_clusters_natkey", VsphereCluster.vcenter, VsphereCluster.moref)
Index("ix_vsphere_hosts_natkey", VsphereHost.vcenter, VsphereHost.moref)
Index("ix_vsphere_vms_natkey", VsphereVm.vcenter, VsphereVm.moref)
Index("ix_vsphere_datastores_natkey", VsphereDatastore.vcenter, VsphereDatastore.moref)
Index("ix_vsphere_networks_natkey", VsphereNetwork.vcenter, VsphereNetwork.moref)
Index(
    "ix_vsphere_resource_pools_natkey",
    VsphereResourcePool.vcenter,
    VsphereResourcePool.moref,
)


__all__ = [
    "VsphereDatacenter",
    "VsphereCluster",
    "VsphereHost",
    "VsphereVm",
    "VsphereDatastore",
    "VsphereNetwork",
    "VsphereResourcePool",
    "VsphereVmDisk",
    "VsphereSnapshot",
    "VsphereVmMetric",
    "VsphereHostMetric",
    "VsphereDatastoreMetric",
    "VsphereLicense",
    "VsphereAlarm",
    "VspherePermission",
    "VsphereSession",
]
