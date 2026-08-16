"""infra_brain.api.routers.vsphere — vSphere / Virtualization API router.

Extracted from dashboard_api.py (Task 3 — Phase 3 god-file split).
Handler bodies are byte-identical to the originals; no logic changes.

Import rules:
  * MAY import from infra_brain.api.schemas, infra_brain.api._helpers,
    infra_brain.db.models, infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func

from infra_brain.api.schemas import (
    VsphereAlarmOut,
    VsphereClusterOut,
    VsphereDatacenterOut,
    VsphereDatastoreOut,
    VsphereHostOut,
    VsphereLicenseOut,
    VsphereNetworkOut,
    VsphereOverviewOut,
    VspherePermissionOut,
    VsphereResourcePoolOut,
    VsphereSecondaryOut,
    VsphereSecondarySummaryOut,
    VsphereSnapshotOut,
    VsphereSummaryOut,
    VsphereVmDiskOut,
    VsphereVmOut,
)
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import (
    VsphereAlarm,
    VsphereCluster,
    VsphereDatacenter,
    VsphereDatastore,
    VsphereHost,
    VsphereLicense,
    VsphereNetwork,
    VspherePermission,
    VsphereResourcePool,
    VsphereSnapshot,
    VsphereVm,
    VsphereVmDisk,
    VsphereVmMetric,
)
from infra_brain.db.session import get_session

# Dedicated vSphere / Virtualization view. Same session gate, distinct prefix —
# surfaces the vSphere relational inventory (datacenters, clusters, hosts, VMs,
# datastores, networks) plus the latest time-series metric timestamp, with its
# own purpose-built response shape rather than the generic Resource contract.
# Read-only; renders an empty state cleanly when the connector is paused.
vsphere_router = APIRouter(
    prefix="/api/vsphere",
    tags=["vsphere"],
    dependencies=[Depends(require_session)],
)

# VMs can be numerous; cap the per-response row count and report the true total
# in the summary so the UI can show "showing N of M".
_VSPHERE_VM_LIMIT = 500

# VM disks and snapshots are capped the same way VMs are (_VSPHERE_VM_LIMIT
# above); true totals are reported in VsphereSecondaryOut.summary.
_VSPHERE_SECONDARY_LIST_LIMIT = 500

# Stale-snapshot hygiene threshold (UI-3/#59): a snapshot older than this many
# days is flagged as stale in the summary count. Conventional vSphere-hygiene
# threshold — long-lived snapshots bloat datastores and degrade VM I/O.
_STALE_SNAPSHOT_AGE_DAYS = 7


@vsphere_router.get("/overview", response_model=VsphereOverviewOut)
def vsphere_overview() -> VsphereOverviewOut:
    """Assemble the vSphere estate for the dashboard's Virtualization view.

    Read-only (SELECT only). Returns the slow-changing relational inventory
    (datacenters → clusters → hosts → VMs, plus datastores) and a ``summary``
    header strip. VM rows are capped at ``_VSPHERE_VM_LIMIT``; the true total is
    in ``summary.vm_count``. Renders cleanly when every table is empty (the
    vSphere connector is paused until a host is configured).
    """
    with get_session() as s:
        # --- Hosts: build a cluster_name → live VM count rollup. The host row's
        # own ``vm_count`` may be stale/null, so prefer counting real VM rows. ---
        host_rows = s.query(VsphereHost).order_by(VsphereHost.name).all()
        hosts = [
            VsphereHostOut(
                name=h.name,
                cluster_name=h.cluster_name or "",
                vendor=h.vendor or "",
                model=h.model or "",
                version=h.version or "",
                connection_state=h.connection_state or "",
                power_state=h.power_state or "",
                in_maintenance_mode=h.in_maintenance_mode,
                vm_count=h.vm_count,
                memory_gb=h.memory_gb,
                overall_status=h.overall_status or "",
            )
            for h in host_rows
        ]

        # --- VMs: count totals + templates, then cap the returned rows. --------
        vm_total = s.query(func.count(VsphereVm.id)).scalar() or 0
        template_count = (
            s.query(func.count(VsphereVm.id)).filter(VsphereVm.is_template.is_(True)).scalar() or 0
        )
        vm_rows = s.query(VsphereVm).order_by(VsphereVm.name).limit(_VSPHERE_VM_LIMIT).all()
        vms = [
            VsphereVmOut(
                name=v.name,
                esxi_host=v.esxi_host or "",
                power_state=v.power_state or "",
                guest_full_name=v.guest_full_name or "",
                num_cpu=v.num_cpu,
                memory_mb=v.memory_mb,
                ip_address=v.ip_address or "",
                tools_status=v.tools_status or "",
                overall_status=v.overall_status or "",
            )
            for v in vm_rows
        ]

        # --- VM rollups per ESXi host (for the datacenter tree). Count real VMs,
        # not templates, so the tree's VM totals line up with running guests. ----
        vms_per_host: dict[str, int] = {}
        for (esxi_host,) in (
            s.query(VsphereVm.esxi_host).filter(VsphereVm.is_template.is_(False)).all()
        ):
            if esxi_host:
                vms_per_host[esxi_host] = vms_per_host.get(esxi_host, 0) + 1

        # --- Clusters ----------------------------------------------------------
        cluster_rows = s.query(VsphereCluster).order_by(VsphereCluster.name).all()
        clusters = [
            VsphereClusterOut(
                name=c.name,
                datacenter_name=c.datacenter_name or "",
                num_hosts=c.num_hosts,
                drs_enabled=c.drs_enabled,
                ha_enabled=c.ha_enabled,
                total_memory_gb=c.total_memory_gb,
                overall_status=c.overall_status or "",
            )
            for c in cluster_rows
        ]

        # --- Datacenter rollups: clusters/hosts/VMs grouped by datacenter ------
        # Hosts → datacenter resolved via their cluster's datacenter_name.
        dc_of_cluster = {c.name: (c.datacenter_name or "") for c in cluster_rows}
        clusters_per_dc: dict[str, int] = {}
        for c in cluster_rows:
            dc = c.datacenter_name or ""
            clusters_per_dc[dc] = clusters_per_dc.get(dc, 0) + 1
        hosts_per_dc: dict[str, int] = {}
        vms_per_dc: dict[str, int] = {}
        for h in host_rows:
            dc = dc_of_cluster.get(h.cluster_name or "", "")
            hosts_per_dc[dc] = hosts_per_dc.get(dc, 0) + 1
            vms_per_dc[dc] = vms_per_dc.get(dc, 0) + (vms_per_host.get(h.name, 0))

        dc_rows = s.query(VsphereDatacenter).order_by(VsphereDatacenter.name).all()
        datacenters = [
            VsphereDatacenterOut(
                name=d.name,
                cluster_count=clusters_per_dc.get(d.name, 0),
                host_count=hosts_per_dc.get(d.name, 0),
                vm_count=vms_per_dc.get(d.name, 0),
            )
            for d in dc_rows
        ]

        # --- Datastores --------------------------------------------------------
        ds_rows = s.query(VsphereDatastore).order_by(VsphereDatastore.name).all()
        datastores = [
            VsphereDatastoreOut(
                name=d.name,
                datastore_type=d.datastore_type or "",
                capacity_gb=d.capacity_gb,
                free_gb=d.free_gb,
                used_pct=d.used_pct,
                accessible=d.accessible,
            )
            for d in ds_rows
        ]

        # --- Summary -----------------------------------------------------------
        network_count = s.query(func.count(VsphereNetwork.id)).scalar() or 0
        total_capacity_gb = float(
            s.query(func.coalesce(func.sum(VsphereDatastore.capacity_gb), 0.0)).scalar() or 0.0
        )
        total_free_gb = float(
            s.query(func.coalesce(func.sum(VsphereDatastore.free_gb), 0.0)).scalar() or 0.0
        )
        latest_metric_at = s.query(func.max(VsphereVmMetric.collected_at)).scalar()

        summary = VsphereSummaryOut(
            datacenter_count=len(dc_rows),
            cluster_count=len(cluster_rows),
            host_count=len(host_rows),
            vm_count=int(vm_total),
            template_count=int(template_count),
            datastore_count=len(ds_rows),
            network_count=int(network_count),
            total_capacity_gb=total_capacity_gb,
            total_free_gb=total_free_gb,
            latest_metric_at=latest_metric_at,
        )

        return VsphereOverviewOut(
            datacenters=datacenters,
            clusters=clusters,
            hosts=hosts,
            vms=vms,
            datastores=datastores,
            summary=summary,
        )


@vsphere_router.get("/secondary", response_model=VsphereSecondaryOut)
def vsphere_secondary() -> VsphereSecondaryOut:
    """Secondary vSphere inventory (UI-3/#59): full network rows (the
    overview above only surfaces a network_count aggregate), resource pools,
    VM disks, snapshots (with a stale-snapshot hygiene flag), licenses,
    alarms, permissions.

    Read-only (SELECT only). VM disks and snapshots are capped at
    _VSPHERE_SECONDARY_LIST_LIMIT; true totals are in `summary`. Renders
    cleanly when every table is empty (same convention as /overview).
    """
    with get_session() as s:
        networks = s.query(VsphereNetwork).order_by(VsphereNetwork.name).all()
        pools = s.query(VsphereResourcePool).order_by(VsphereResourcePool.name).all()

        disk_total = s.query(func.count(VsphereVmDisk.id)).scalar() or 0
        disks = (
            s.query(VsphereVmDisk)
            .order_by(VsphereVmDisk.vm_name)
            .limit(_VSPHERE_SECONDARY_LIST_LIMIT)
            .all()
        )

        snap_total = s.query(func.count(VsphereSnapshot.id)).scalar() or 0
        stale_total = (
            s.query(func.count(VsphereSnapshot.id))
            .filter(VsphereSnapshot.age_days >= _STALE_SNAPSHOT_AGE_DAYS)
            .scalar()
            or 0
        )
        snapshots = (
            s.query(VsphereSnapshot)
            .order_by(VsphereSnapshot.age_days.desc().nullslast())
            .limit(_VSPHERE_SECONDARY_LIST_LIMIT)
            .all()
        )

        licenses = s.query(VsphereLicense).order_by(VsphereLicense.name).all()
        alarms = s.query(VsphereAlarm).order_by(VsphereAlarm.triggered_at.desc().nullslast()).all()
        permissions = s.query(VspherePermission).order_by(VspherePermission.principal).all()

        return VsphereSecondaryOut(
            networks=[
                VsphereNetworkOut(
                    name=n.name,
                    network_kind=n.network_kind,
                    accessible=n.accessible,
                    num_ports=n.num_ports,
                    host_count=n.host_count,
                    vm_count=n.vm_count,
                )
                for n in networks
            ],
            resource_pools=[
                VsphereResourcePoolOut(
                    name=p.name,
                    cpu_limit=p.cpu_limit,
                    cpu_reservation=p.cpu_reservation,
                    memory_limit=p.memory_limit,
                    memory_reservation=p.memory_reservation,
                    vm_count=p.vm_count,
                )
                for p in pools
            ],
            vm_disks=[
                VsphereVmDiskOut(
                    vm_name=d.vm_name or "",
                    label=d.label,
                    capacity_gb=d.capacity_gb,
                    thin_provisioned=d.thin_provisioned,
                    backing_type=d.backing_type,
                    datastore_name=d.datastore_name,
                )
                for d in disks
            ],
            snapshots=[
                VsphereSnapshotOut(
                    vm_name=sn.vm_name or "",
                    name=sn.name,
                    created_at=sn.created_at,
                    age_days=sn.age_days,
                    is_current=sn.is_current,
                    state=sn.state,
                )
                for sn in snapshots
            ],
            licenses=[
                VsphereLicenseOut(
                    name=lic.name,
                    edition_key=lic.edition_key,
                    total=lic.total,
                    used=lic.used,
                    expiration=lic.expiration,
                )
                for lic in licenses
            ],
            alarms=[
                VsphereAlarmOut(
                    alarm_name=a.alarm_name,
                    entity_name=a.entity_name,
                    entity_type=a.entity_type,
                    overall_status=a.overall_status,
                    acknowledged=a.acknowledged,
                    triggered_at=a.triggered_at,
                )
                for a in alarms
            ],
            permissions=[
                VspherePermissionOut(
                    principal=pm.principal,
                    role_name=pm.role_name,
                    is_group=pm.is_group,
                    propagate=pm.propagate,
                    entity=pm.entity,
                )
                for pm in permissions
            ],
            summary=VsphereSecondarySummaryOut(
                network_count=len(networks),
                resource_pool_count=len(pools),
                vm_disk_count=int(disk_total),
                snapshot_count=int(snap_total),
                stale_snapshot_count=int(stale_total),
                license_count=len(licenses),
                alarm_count=len(alarms),
                permission_count=len(permissions),
            ),
        )
