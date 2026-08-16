"""
VMware vSphere tools using pyVmomi — read-only.
Gracefully degrades if pyVmomi is not installed or vSphere is not configured.
"""

import logging
from datetime import datetime, timezone
from langchain_core.tools import ToolException, tool
from infra_brain.config import get_settings

logger = logging.getLogger(__name__)

try:
    # NOTE: SmartConnectNoSSL was removed in pyVmomi 8.0+. The no-verify path now
    # goes through SmartConnect(disableSslCertValidation=True) (see _connect).
    # Importing the removed name silently set _PYVMOMI_AVAILABLE=False → vsphere
    # skipped forever once the image shipped a modern pyVmomi.
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim, vmodl

    _PYVMOMI_AVAILABLE = True
except ImportError:
    _PYVMOMI_AVAILABLE = False

    # Provide stub objects so that collect_* functions can be imported and
    # tested (with _collect_properties patched) even when pyVmomi is absent.
    class _VimStub:
        """Minimal stub — attribute access returns a sentinel string for type dispatch."""

        def __getattr__(self, name):
            child = _VimStub()
            child._name = name
            return child

        def __repr__(self):
            return f"vim.{getattr(self, '_name', '?')}"

    class _VmodlStub:
        def __getattr__(self, name):
            return _VmodlStub()

        def __call__(self, **kwargs):
            return self

    vim = _VimStub()  # type: ignore[assignment]
    vmodl = _VmodlStub()  # type: ignore[assignment]

    # Also stub connect/disconnect so _connect() import guard works
    def SmartConnect(**kwargs):  # type: ignore[misc]
        raise ImportError("pyVmomi not installed")

    def Disconnect(si):  # type: ignore[misc]
        pass


def _connect(host: str, user: str, pwd: str, ssl_verify: bool, timeout: int = 30):
    """Open a SmartConnect session to a vCenter with explicit parameters."""
    # pyVmomi 8.0+ renamed the socket timeout kwarg connectionTimeout →
    # httpConnectionTimeout, and replaced SmartConnectNoSSL with the
    # disableSslCertValidation kwarg below.
    kwargs = dict(host=host, user=user, pwd=pwd, port=443, httpConnectionTimeout=timeout)
    if not ssl_verify:
        kwargs["disableSslCertValidation"] = True
    return SmartConnect(**kwargs)


def _collect_properties(content, vimtype, properties: list[str]) -> list:
    """
    Batch-fetch all objects of vimtype with the given properties using
    the PropertyCollector API (RetrievePropertiesEx + ContinueRetrievePropertiesEx).
    Returns a list of ObjectContent objects.
    """
    pc = content.propertyCollector
    view_ref = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
    try:
        obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
            obj=view_ref,
            skip=True,
            selectSet=[
                vmodl.query.PropertyCollector.TraversalSpec(
                    name="traverseEntries",
                    type=type(view_ref),
                    path="view",
                    skip=False,
                )
            ],
        )
        options = vmodl.query.PropertyCollector.RetrieveOptions(maxObjects=500)
        # Version-resilient property fetch: a single property path that this
        # vCenter's API version doesn't recognize (e.g. quickStats.
        # overallMemoryUsage was removed in vCenter 8.0) makes the WHOLE
        # RetrievePropertiesEx fail with InvalidProperty, zeroing out the entire
        # type. Instead, drop the offending property (named in the fault) and
        # retry — the field degrades to null rather than losing every object.
        remaining = list(properties)
        dropped: list[str] = []
        while True:
            prop_specs = [
                vmodl.query.PropertyCollector.PropertySpec(type=vimtype, pathSet=remaining)
            ]
            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=prop_specs
            )
            try:
                result = pc.RetrievePropertiesEx(specSet=[filter_spec], options=options)
                break
            except vmodl.query.InvalidProperty as exc:
                bad = getattr(exc, "name", None)
                if not bad or bad not in remaining:
                    raise
                remaining.remove(bad)
                dropped.append(bad)
                if not remaining:
                    raise
        if dropped:
            logger.warning(
                "VsphereAgent: %s — dropped %d property(ies) not supported by this vCenter: %s",
                getattr(vimtype, "__name__", vimtype),
                len(dropped),
                dropped,
            )
        objects = []
        if result is None:
            return objects
        objects.extend(result.objects)
        token = result.token
        while token:
            cont = pc.ContinueRetrievePropertiesEx(token=token)
            objects.extend(cont.objects)
            token = cont.token
        return objects
    finally:
        view_ref.Destroy()


def _safe_val(val):
    """Convert pyVmomi types to JSON-serializable values."""
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, datetime):
        return val.isoformat()
    if hasattr(val, "_moId"):
        return getattr(val, "name", val._moId)
    if isinstance(val, (list, tuple)) or hasattr(val, "__iter__") and not isinstance(val, str):
        try:
            return [_safe_val(v) for v in val]
        except TypeError:
            return str(val)
    return str(val)


def _count_snapshots(snapshot_info) -> int:
    """Recursively count total snapshots from a vim.vm.SnapshotInfo tree."""
    if snapshot_info is None:
        return 0

    def _count_tree(snapshots) -> int:
        count = 0
        for snap in snapshots or []:
            count += 1
            count += _count_tree(getattr(snap, "childSnapshotList", None))
        return count

    return _count_tree(getattr(snapshot_info, "rootSnapshotList", None))


def _extract_vm_disks(devices) -> list[dict]:
    """Plain-dict per VirtualDisk from a VM's config.hardware.device list.
    Extraction happens HERE (tool side) where pyVmomi types exist — the agent
    never sees raw pyVmomi objects."""
    disks: list[dict] = []
    for d in devices or []:
        if not isinstance(d, vim.vm.device.VirtualDisk):
            continue
        backing = getattr(d, "backing", None)
        cap_kb = getattr(d, "capacityInKB", None)
        ds_ref = getattr(backing, "datastore", None)
        disks.append(
            {
                "disk_key": getattr(d, "key", None),
                "label": getattr(getattr(d, "deviceInfo", None), "label", None),
                "capacity_gb": round(cap_kb / (1024 * 1024), 2) if cap_kb else None,
                "thin_provisioned": getattr(backing, "thinProvisioned", None),
                "backing_type": type(backing).__name__ if backing else None,
                "datastore_name": getattr(ds_ref, "name", None) if ds_ref else None,
                "file_path": getattr(backing, "fileName", None),
            }
        )
    return disks


def _extract_snapshots(snapshot_info, now: datetime) -> list[dict]:
    """Flatten a vim.vm.SnapshotInfo tree into plain dicts with denormalized
    age_days. ``now`` is passed in (never call datetime.now here — keeps the
    tool deterministic/testable)."""
    if snapshot_info is None:
        return []
    current_ref = getattr(snapshot_info, "currentSnapshot", None)
    current_id = getattr(current_ref, "_moId", None)
    out: list[dict] = []

    def _walk(nodes):
        for s in nodes or []:
            created = getattr(s, "createTime", None)
            age = None
            if created is not None:
                try:
                    age = (now - created).days
                except TypeError:
                    age = None
            snap_ref = getattr(s, "snapshot", None)
            out.append(
                {
                    "snapshot_id": getattr(s, "id", None),
                    "name": getattr(s, "name", None),
                    "description": getattr(s, "description", None),
                    "created_at": created.isoformat() if created else None,
                    "age_days": age,
                    "state": getattr(s, "state", None),
                    "is_current": (
                        getattr(snap_ref, "_moId", None) == current_id
                        if snap_ref is not None and current_id
                        else None
                    ),
                }
            )
            _walk(getattr(s, "childSnapshotList", None))

    _walk(getattr(snapshot_info, "rootSnapshotList", None))
    return out


def _host_hardening(props: dict) -> dict:
    """Extract ESXi hardening/firmware/health fields (Phase C) from a host's
    property dict into plain values. config.option is a flat OptionValue list;
    config.service.service is the host services list (running flag)."""
    opts = {o.key: o.value for o in (props.get("config.option") or []) if hasattr(o, "key")}
    svc = {
        sv.key: bool(getattr(sv, "running", False))
        for sv in (props.get("config.service.service") or [])
        if hasattr(sv, "key")
    }
    ntp = props.get("config.dateTimeInfo.ntpConfig.server") or []
    # hardware health: worst state + count of non-green numeric sensors
    health = props.get("runtime.healthSystemRuntime.systemHealthInfo")
    sensors = getattr(health, "numericSensorInfo", None) if health else None
    worst = None
    issues = None
    if sensors is not None:
        states = [getattr(getattr(s, "healthState", None), "key", None) for s in sensors]
        states = [x for x in states if x]
        issues = sum(1 for x in states if x not in ("green", "unknown"))
        if any(x == "red" for x in states):
            worst = "red"
        elif any(x == "yellow" for x in states):
            worst = "yellow"
        elif states:
            worst = "green"
    return {
        "ntp_servers": [str(x) for x in ntp],
        "syslog_host": opts.get("Syslog.global.logHost") or None,
        "lockdown_mode": _safe_val(props.get("config.lockdownMode")),
        "ssh_enabled": svc.get("TSM-SSH"),
        "esxi_shell_enabled": svc.get("TSM"),
        "shell_timeout_sec": opts.get("UserVars.ESXiShellTimeOut"),
        "account_lock_failures": opts.get("Security.AccountLockFailures"),
        "bios_version": _safe_val(props.get("hardware.biosInfo.biosVersion")),
        "bios_date": _safe_val(props.get("hardware.biosInfo.releaseDate")),
        "hw_health": worst,
        "hw_health_issues": issues,
    }


def _datastore_storage(props: dict) -> dict:
    """Phase E datastore attributes. ``info`` is VmfsDatastoreInfo (block) or
    NasDatastoreInfo (NFS) — extract whichever fields apply."""
    info = props.get("info")
    vmfs = getattr(info, "vmfs", None)
    nas = getattr(info, "nas", None)
    return {
        "sioc_enabled": _safe_val(props.get("iormConfiguration.enabled")),
        "vmfs_version": _safe_val(getattr(vmfs, "version", None)) if vmfs else None,
        "ssd": getattr(vmfs, "ssd", None) if vmfs else None,
        "remote_host": _safe_val(getattr(nas, "remoteHost", None)) if nas else None,
        "remote_path": _safe_val(getattr(nas, "remotePath", None)) if nas else None,
    }


def _cluster_governance(props: dict) -> dict:
    """Extract Phase D cluster governance: EVC mode, HA admission control, and
    DRS rules (affinity/anti-affinity/VM-host) as plain dicts."""
    cx = props.get("configurationEx")
    das = getattr(cx, "dasConfig", None) if cx else None
    rules = []
    for r in (getattr(cx, "rule", None) or []) if cx else []:
        rules.append(
            {
                "name": getattr(r, "name", None),
                "type": type(r).__name__,
                "enabled": getattr(r, "enabled", None),
                "mandatory": getattr(r, "mandatory", None),
                "vm_count": len(getattr(r, "vm", None) or []),
            }
        )
    return {
        "evc_mode": _safe_val(props.get("summary.currentEVCModeKey")),
        "ha_admission_control": getattr(das, "admissionControlEnabled", None) if das else None,
        "drs_rules": rules,
    }


def _props_to_dict(obj_content) -> dict:
    """Convert an ObjectContent's propSet into a plain dict."""
    props = {}
    for prop in obj_content.propSet or []:
        props[prop.name] = prop.val
    return props


def _moref(obj_content) -> str:
    """Managed Object Reference id (e.g. 'datastore-45', 'vm-12') for an
    ObjectContent. The relational inventory rows and topology edges are keyed by
    ``(vcenter, moref)`` — ``VsphereAgent._upsert_inventory_item`` raises and skips
    any item without one. pyVmomi exposes it as ``ObjectContent.obj._moId``."""
    ref = getattr(obj_content, "obj", None)
    return getattr(ref, "_moId", "") or ""


def _ancestor_datacenter_name(obj_content) -> str:
    """Walk an ObjectContent's managed-object parent chain up to its enclosing
    vim.Datacenter and return its name ("" if none/unavailable). Clusters/hosts
    sit under a Datacenter's hostFolder, so their parent chain resolves the DC.
    Each ``.parent`` hop is a lazy property fetch; the depth is tiny (2-3)."""
    mo = getattr(obj_content, "obj", None)
    for _ in range(20):  # bounded: guards against a cycle / broken hierarchy
        if mo is None:
            break
        try:
            if isinstance(mo, vim.Datacenter):
                return getattr(mo, "name", "") or ""
            mo = getattr(mo, "parent", None)
        except Exception:  # noqa: BLE001 — a broken ref must not fail the collect
            break
    return ""


def collect_vms(content, vcenter_host: str) -> list[dict]:
    """Collect all VMs and templates from a vCenter."""
    properties = [
        "name",
        "config.uuid",
        "config.instanceUuid",
        "config.guestFullName",
        "config.guestId",
        # numCpu/memorySizeMB live on config.hardware (VirtualHardware), NOT
        # directly on config — config.numCpu/config.memorySizeMB are invalid
        # paths (vCenter 8 InvalidProperty → dropped → 100% null). Siblings of
        # config.hardware.numCoresPerSocket, which already worked.
        "config.hardware.numCPU",
        "config.hardware.memoryMB",
        "config.annotation",
        "config.template",
        "config.version",
        "config.hardware.numCoresPerSocket",
        # Phase B — resource governance + security posture + disks
        "config.cpuAllocation.reservation",
        "config.cpuAllocation.limit",
        "config.memoryAllocation.reservation",
        "config.memoryAllocation.limit",
        "config.firmware",
        "config.bootOptions.efiSecureBootEnabled",
        "config.keyId",
        "config.hardware.device",
        "runtime.powerState",
        "runtime.bootTime",
        "runtime.host",
        "runtime.maxCpuUsage",
        "runtime.maxMemoryUsage",
        "runtime.consolidationNeeded",
        "guest.hostName",
        "guest.ipAddress",
        "guest.toolsStatus",
        "guest.toolsVersion",
        "guest.toolsRunningStatus",
        "guest.net",
        "summary.quickStats.overallCpuUsage",
        "summary.quickStats.hostMemoryUsage",
        "summary.quickStats.uptimeSeconds",
        "summary.quickStats.balloonedMemory",
        "summary.storage.committed",
        "summary.storage.uncommitted",
        "summary.overallStatus",
        "snapshot",
        "datastore",
        "network",
        "parent",
        "resourcePool",
    ]
    objects = _collect_properties(content, vim.VirtualMachine, properties)
    result = []
    for obj in objects:
        props = _props_to_dict(obj)
        is_template = bool(props.get("config.template", False))
        resource_type = "vsphere_template" if is_template else "vsphere_vm"

        committed = props.get("summary.storage.committed") or 0
        uncommitted = props.get("summary.storage.uncommitted") or 0

        # Extract all IPs (+ NIC MACs, KG-5) from guest.net NicInfo list.
        # MACs ride into VsphereVm.details["mac_addresses"] (no typed column,
        # no schema change) so graph_phase3.materialize_host_nodes can copy
        # the primary one onto the node's `mac` attribute -- the one field
        # Rapid7 also populates, letting hard-identifier corroboration
        # actually engage on a real vSphere<->Rapid7 pair (previously dead
        # code: vSphere only ever populated uuid/instance_uuid).
        all_ips = []
        mac_addresses = []
        guest_net = props.get("guest.net")
        if guest_net:
            for nic in guest_net if isinstance(guest_net, (list, tuple)) else [guest_net]:
                ip_addrs = getattr(nic, "ipAddress", None)
                if ip_addrs:
                    all_ips.extend(list(ip_addrs))
                mac = getattr(nic, "macAddress", None)
                if mac:
                    mac_addresses.append(mac)

        # Resolve ref lists to names
        datastore_names = []
        for ds in props.get("datastore") or []:
            datastore_names.append(getattr(ds, "name", str(ds)))

        network_names = []
        for net in props.get("network") or []:
            network_names.append(getattr(net, "name", str(net)))

        host_ref = props.get("runtime.host")
        esxi_host = getattr(host_ref, "name", str(host_ref)) if host_ref else ""

        # TRK-104: capture the VM's owning resource pool moref (e.g. "resgroup-42")
        # so the KG can emit a vsphere_vm IN_POOL vsphere_resource_pool edge. The
        # ManagedObjectReference is resolved to its _moId, matching the (vcenter,
        # moref) natural key vsphere_resource_pools rows are stored under.
        rp_ref = props.get("resourcePool")
        resource_pool_moref = getattr(rp_ref, "_moId", None) if rp_ref else None

        # Phase B: per-disk + per-snapshot child records, extracted to plain
        # dicts here (tool side); the agent writes them to vsphere_vm_disks /
        # vsphere_snapshots. keyId presence ⇒ VM is encrypted.
        vm_disks = _extract_vm_disks(props.get("config.hardware.device"))
        vm_snapshots = _extract_snapshots(props.get("snapshot"), datetime.now(timezone.utc))

        data = {
            "uuid": _safe_val(props.get("config.uuid")),
            "instance_uuid": _safe_val(props.get("config.instanceUuid")),
            "guest_full_name": _safe_val(props.get("config.guestFullName")),
            "guest_id": _safe_val(props.get("config.guestId")),
            "num_cpu": _safe_val(props.get("config.hardware.numCPU")),
            "memory_mb": _safe_val(props.get("config.hardware.memoryMB")),
            "cores_per_socket": _safe_val(props.get("config.hardware.numCoresPerSocket")),
            "annotation": _safe_val(props.get("config.annotation")),
            "template": is_template,
            "hw_version": _safe_val(props.get("config.version")),
            "power_state": _safe_val(props.get("runtime.powerState")),
            "boot_time": _safe_val(props.get("runtime.bootTime")),
            "esxi_host": esxi_host,
            "max_cpu_usage": _safe_val(props.get("runtime.maxCpuUsage")),
            "max_memory_usage": _safe_val(props.get("runtime.maxMemoryUsage")),
            "consolidation_needed": _safe_val(props.get("runtime.consolidationNeeded")),
            "guest_hostname": _safe_val(props.get("guest.hostName")),
            "ip_address": _safe_val(props.get("guest.ipAddress")),
            "all_ips": all_ips,
            "mac_addresses": mac_addresses,
            "tools_status": _safe_val(props.get("guest.toolsStatus")),
            "tools_version": _safe_val(props.get("guest.toolsVersion")),
            "tools_running_status": _safe_val(props.get("guest.toolsRunningStatus")),
            "cpu_usage_mhz": _safe_val(props.get("summary.quickStats.overallCpuUsage")),
            "memory_usage_mb": _safe_val(props.get("summary.quickStats.hostMemoryUsage")),
            "uptime_seconds": _safe_val(props.get("summary.quickStats.uptimeSeconds")),
            "ballooned_memory_mb": _safe_val(props.get("summary.quickStats.balloonedMemory")),
            "disk_committed_gb": round(committed / 1e9, 1) if committed else 0,
            "disk_uncommitted_gb": round(uncommitted / 1e9, 1) if uncommitted else 0,
            "overall_status": _safe_val(props.get("summary.overallStatus")),
            "snapshot_count": _count_snapshots(props.get("snapshot")),
            "datastore_names": datastore_names,
            "network_names": network_names,
            # Phase B — resource governance + security posture
            "cpu_reservation_mhz": _safe_val(props.get("config.cpuAllocation.reservation")),
            "cpu_limit_mhz": _safe_val(props.get("config.cpuAllocation.limit")),
            "mem_reservation_mb": _safe_val(props.get("config.memoryAllocation.reservation")),
            "mem_limit_mb": _safe_val(props.get("config.memoryAllocation.limit")),
            "firmware": _safe_val(props.get("config.firmware")),
            "secure_boot": _safe_val(props.get("config.bootOptions.efiSecureBootEnabled")),
            "encrypted": props.get("config.keyId") is not None,
            # child records (written to vsphere_vm_disks / vsphere_snapshots)
            "disks": vm_disks,
            "snapshots": vm_snapshots,
            "vcenter": vcenter_host,
            "moref": _moref(obj),
            "resource_pool_moref": resource_pool_moref,
        }
        result.append(
            {"name": _safe_val(props.get("name", "")), "type": resource_type, "data": data}
        )
    return result


def collect_esxi_hosts(content, vcenter_host: str) -> list[dict]:
    """Collect all ESXi hosts from a vCenter."""
    properties = [
        "name",
        "summary.hardware.uuid",
        "summary.hardware.vendor",
        "summary.hardware.model",
        "summary.hardware.cpuModel",
        "summary.hardware.numCpuCores",
        "summary.hardware.numCpuThreads",
        "summary.hardware.numCpuPkgs",
        "summary.hardware.cpuMhz",
        "summary.hardware.memorySize",
        "summary.hardware.numNics",
        "summary.hardware.numHBAs",
        "summary.runtime.connectionState",
        "summary.runtime.powerState",
        "summary.runtime.inMaintenanceMode",
        "summary.runtime.bootTime",
        "summary.runtime.standbyMode",
        "summary.config.product.version",
        "summary.config.product.build",
        "summary.config.product.fullName",
        "summary.quickStats.overallCpuUsage",
        "summary.quickStats.hostMemoryUsage",
        "summary.quickStats.uptime",
        "summary.overallStatus",
        "config.network.dnsConfig.hostName",
        "config.network.dnsConfig.domainName",
        "config.network.dnsConfig.ipAddress",
        # Phase C — hardening posture + firmware + hardware health
        "config.dateTimeInfo.ntpConfig.server",
        "config.option",
        "config.service.service",
        "config.lockdownMode",
        "hardware.biosInfo.biosVersion",
        "hardware.biosInfo.releaseDate",
        "runtime.healthSystemRuntime.systemHealthInfo",
        "parent",
        "datastore",
        "vm",
    ]
    objects = _collect_properties(content, vim.HostSystem, properties)
    result = []
    for obj in objects:
        props = _props_to_dict(obj)
        memory_size = props.get("summary.hardware.memorySize") or 0

        datastore_names = [getattr(ds, "name", str(ds)) for ds in (props.get("datastore") or [])]
        vm_refs = props.get("vm") or []
        parent_ref = props.get("parent")

        data = {
            "hardware_uuid": _safe_val(props.get("summary.hardware.uuid")),
            "vendor": _safe_val(props.get("summary.hardware.vendor")),
            "model": _safe_val(props.get("summary.hardware.model")),
            "cpu_model": _safe_val(props.get("summary.hardware.cpuModel")),
            "num_cpu_cores": _safe_val(props.get("summary.hardware.numCpuCores")),
            "num_cpu_threads": _safe_val(props.get("summary.hardware.numCpuThreads")),
            "num_cpu_pkgs": _safe_val(props.get("summary.hardware.numCpuPkgs")),
            "cpu_mhz": _safe_val(props.get("summary.hardware.cpuMhz")),
            "memory_gb": round(memory_size / 1e9, 1),
            "num_nics": _safe_val(props.get("summary.hardware.numNics")),
            "num_hbas": _safe_val(props.get("summary.hardware.numHBAs")),
            "connection_state": _safe_val(props.get("summary.runtime.connectionState")),
            "power_state": _safe_val(props.get("summary.runtime.powerState")),
            "in_maintenance_mode": _safe_val(props.get("summary.runtime.inMaintenanceMode")),
            "boot_time": _safe_val(props.get("summary.runtime.bootTime")),
            "standby_mode": _safe_val(props.get("summary.runtime.standbyMode")),
            "version": _safe_val(props.get("summary.config.product.version")),
            "build": _safe_val(props.get("summary.config.product.build")),
            "full_name": _safe_val(props.get("summary.config.product.fullName")),
            "cpu_usage_mhz": _safe_val(props.get("summary.quickStats.overallCpuUsage")),
            "memory_usage_mb": _safe_val(props.get("summary.quickStats.hostMemoryUsage")),
            "uptime_seconds": _safe_val(props.get("summary.quickStats.uptime")),
            "overall_status": _safe_val(props.get("summary.overallStatus")),
            "dns_hostname": _safe_val(props.get("config.network.dnsConfig.hostName")),
            "dns_domain": _safe_val(props.get("config.network.dnsConfig.domainName")),
            "dns_ips": _safe_val(props.get("config.network.dnsConfig.ipAddress")),
            "vm_count": len(vm_refs),
            "datastore_names": datastore_names,
            "cluster_or_parent": getattr(parent_ref, "name", str(parent_ref)) if parent_ref else "",
            "vcenter": vcenter_host,
            "moref": _moref(obj),
        }
        data.update(_host_hardening(props))  # Phase C
        result.append(
            {"name": _safe_val(props.get("name", "")), "type": "vsphere_host", "data": data}
        )
    return result


def collect_datastores(content, vcenter_host: str) -> list[dict]:
    """Collect all datastores from a vCenter."""
    properties = [
        "name",
        "summary.type",
        "summary.capacity",
        "summary.freeSpace",
        "summary.accessible",
        "summary.maintenanceMode",
        "summary.url",
        "summary.multipleHostAccess",
        "overallStatus",
        "host",
        "vm",
        # Phase E — storage attributes
        "iormConfiguration.enabled",
        "info",
    ]
    objects = _collect_properties(content, vim.Datastore, properties)
    result = []
    for obj in objects:
        props = _props_to_dict(obj)
        capacity = props.get("summary.capacity") or 0
        free = props.get("summary.freeSpace") or 0

        # host is ArrayOfDatastoreHostMount — each element has .key (HostSystem ref)
        host_mounts = props.get("host") or []
        try:
            host_count = len(list(host_mounts))
        except TypeError:
            host_count = 0

        vm_refs = props.get("vm") or []

        capacity_gb = round(capacity / 1e9, 1)
        free_gb = round(free / 1e9, 1)
        used_gb = round(capacity_gb - free_gb, 1)
        used_pct = round((1 - free / capacity) * 100, 1) if capacity > 0 else 0

        data = {
            "datastore_type": _safe_val(props.get("summary.type")),
            "capacity_gb": capacity_gb,
            "free_gb": free_gb,
            "used_gb": used_gb,
            "used_pct": used_pct,
            "accessible": _safe_val(props.get("summary.accessible")),
            "maintenance_mode": _safe_val(props.get("summary.maintenanceMode")),
            "url": _safe_val(props.get("summary.url")),
            "multiple_host_access": _safe_val(props.get("summary.multipleHostAccess")),
            "overall_status": _safe_val(props.get("overallStatus")),
            "host_count": host_count,
            "vm_count": len(list(vm_refs)) if vm_refs else 0,
            "vcenter": vcenter_host,
            "moref": _moref(obj),
        }
        data.update(_datastore_storage(props))  # Phase E
        result.append(
            {"name": _safe_val(props.get("name", "")), "type": "vsphere_datastore", "data": data}
        )
    return result


def collect_clusters(content, vcenter_host: str) -> list[dict]:
    """Collect all cluster compute resources from a vCenter."""
    properties = [
        "name",
        "configuration.dasConfig.enabled",
        "configuration.dasConfig.failoverLevel",
        "configuration.dasConfig.hostMonitoring",
        "configuration.drsConfig.enabled",
        "configuration.drsConfig.defaultVmBehavior",
        "configuration.drsConfig.vmotionRate",
        "summary.numHosts",
        "summary.numEffectiveHosts",
        "summary.totalCpu",
        "summary.totalMemory",
        "summary.numCpuCores",
        "summary.numCpuThreads",
        "summary.overallStatus",
        "overallStatus",
        # Phase D — DRS rules + EVC + HA admission control
        "configurationEx",
        "summary.currentEVCModeKey",
        "parent",
        "host",
        "datastore",
        "network",
    ]
    objects = _collect_properties(content, vim.ClusterComputeResource, properties)
    result = []
    for obj in objects:
        props = _props_to_dict(obj)
        total_memory = props.get("summary.totalMemory") or 0

        host_names = [getattr(h, "name", str(h)) for h in (props.get("host") or [])]
        datastore_names = [getattr(ds, "name", str(ds)) for ds in (props.get("datastore") or [])]

        data = {
            "ha_enabled": _safe_val(props.get("configuration.dasConfig.enabled")),
            "ha_failover_level": _safe_val(props.get("configuration.dasConfig.failoverLevel")),
            "ha_host_monitoring": _safe_val(props.get("configuration.dasConfig.hostMonitoring")),
            "drs_enabled": _safe_val(props.get("configuration.drsConfig.enabled")),
            "drs_default_behavior": _safe_val(
                props.get("configuration.drsConfig.defaultVmBehavior")
            ),
            "drs_vmotion_rate": _safe_val(props.get("configuration.drsConfig.vmotionRate")),
            "num_hosts": _safe_val(props.get("summary.numHosts")),
            "num_effective_hosts": _safe_val(props.get("summary.numEffectiveHosts")),
            "total_cpu_mhz": _safe_val(props.get("summary.totalCpu")),
            "total_memory_gb": round(total_memory / 1e9, 1),
            "num_cpu_cores": _safe_val(props.get("summary.numCpuCores")),
            "num_cpu_threads": _safe_val(props.get("summary.numCpuThreads")),
            "overall_status": _safe_val(
                props.get("summary.overallStatus") or props.get("overallStatus")
            ),
            "host_names": host_names,
            "datastore_names": datastore_names,
            "datacenter_name": _ancestor_datacenter_name(obj),
            "vcenter": vcenter_host,
            "moref": _moref(obj),
        }
        data.update(_cluster_governance(props))  # Phase D
        result.append(
            {"name": _safe_val(props.get("name", "")), "type": "vsphere_cluster", "data": data}
        )
    return result


def collect_datacenters(content, vcenter_host: str) -> list[dict]:
    """Collect all datacenter objects from a vCenter."""
    properties = ["name", "overallStatus", "parent"]
    objects = _collect_properties(content, vim.Datacenter, properties)
    result = []
    for obj in objects:
        props = _props_to_dict(obj)
        data = {
            "overall_status": _safe_val(props.get("overallStatus")),
            "parent": _safe_val(props.get("parent")),
            "vcenter": vcenter_host,
            "moref": _moref(obj),
        }
        result.append(
            {"name": _safe_val(props.get("name", "")), "type": "vsphere_datacenter", "data": data}
        )
    return result


def collect_networks(content, vcenter_host: str) -> list[dict]:
    """Collect standard portgroups, distributed virtual switches, and DVS portgroups."""
    result = []

    # Standard port groups
    pg_properties = [
        "name",
        "summary.accessible",
        "summary.ipPoolName",
        "overallStatus",
        "parent",
        "host",
        "vm",
    ]
    for obj in _collect_properties(content, vim.Network, pg_properties):
        props = _props_to_dict(obj)
        host_refs = props.get("host") or []
        vm_refs = props.get("vm") or []
        data = {
            "accessible": _safe_val(props.get("summary.accessible")),
            "ip_pool_name": _safe_val(props.get("summary.ipPoolName")),
            "overall_status": _safe_val(props.get("overallStatus")),
            "host_count": len(list(host_refs)) if host_refs else 0,
            "vm_count": len(list(vm_refs)) if vm_refs else 0,
            "vcenter": vcenter_host,
            "moref": _moref(obj),
        }
        result.append(
            {"name": _safe_val(props.get("name", "")), "type": "vsphere_portgroup", "data": data}
        )

    # Distributed Virtual Switches
    dvs_properties = [
        "name",
        "summary.numPorts",
        "summary.uuid",
        "summary.productInfo.version",
        "config.description",
        "config.numStandalonePorts",
        "config.maxPorts",
        "overallStatus",
        "parent",
    ]
    for obj in _collect_properties(content, vim.dvs.VmwareDistributedVirtualSwitch, dvs_properties):
        props = _props_to_dict(obj)
        data = {
            "num_ports": _safe_val(props.get("summary.numPorts")),
            "uuid": _safe_val(props.get("summary.uuid")),
            "version": _safe_val(props.get("summary.productInfo.version")),
            "description": _safe_val(props.get("config.description")),
            "num_standalone_ports": _safe_val(props.get("config.numStandalonePorts")),
            "max_ports": _safe_val(props.get("config.maxPorts")),
            "overall_status": _safe_val(props.get("overallStatus")),
            "vcenter": vcenter_host,
            "moref": _moref(obj),
        }
        result.append(
            {"name": _safe_val(props.get("name", "")), "type": "vsphere_dvswitch", "data": data}
        )

    # DVS Port Groups
    dvpg_properties = [
        "name",
        "summary.numPorts",
        "config.numPorts",
        "config.type",
        "config.description",
        "overallStatus",
        "parent",
    ]
    for obj in _collect_properties(content, vim.dvs.DistributedVirtualPortgroup, dvpg_properties):
        props = _props_to_dict(obj)
        data = {
            "num_ports": _safe_val(props.get("summary.numPorts") or props.get("config.numPorts")),
            "portgroup_type": _safe_val(props.get("config.type")),
            "description": _safe_val(props.get("config.description")),
            "overall_status": _safe_val(props.get("overallStatus")),
            "vcenter": vcenter_host,
            "moref": _moref(obj),
        }
        result.append(
            {"name": _safe_val(props.get("name", "")), "type": "vsphere_dvportgroup", "data": data}
        )

    return result


def collect_resource_pools(content, vcenter_host: str) -> list[dict]:
    """Collect resource pools (excluding root 'Resources' pools)."""
    properties = [
        "name",
        "config.cpuAllocation.limit",
        "config.cpuAllocation.reservation",
        "config.cpuAllocation.expandableReservation",
        "config.memoryAllocation.limit",
        "config.memoryAllocation.reservation",
        "config.memoryAllocation.expandableReservation",
        "summary.quickStats.overallCpuUsage",
        "summary.quickStats.hostMemoryUsage",
        "overallStatus",
        "parent",
        "vm",
        "resourcePool",
    ]
    objects = _collect_properties(content, vim.ResourcePool, properties)
    result = []
    for obj in objects:
        props = _props_to_dict(obj)
        name = _safe_val(props.get("name", ""))
        if name == "Resources":
            continue  # skip root resource pool noise

        vm_refs = props.get("vm") or []
        data = {
            "cpu_limit": _safe_val(props.get("config.cpuAllocation.limit")),
            "cpu_reservation": _safe_val(props.get("config.cpuAllocation.reservation")),
            "cpu_expandable": _safe_val(props.get("config.cpuAllocation.expandableReservation")),
            "memory_limit": _safe_val(props.get("config.memoryAllocation.limit")),
            "memory_reservation": _safe_val(props.get("config.memoryAllocation.reservation")),
            "memory_expandable": _safe_val(
                props.get("config.memoryAllocation.expandableReservation")
            ),
            "cpu_usage_mhz": _safe_val(props.get("summary.quickStats.overallCpuUsage")),
            "memory_usage_mb": _safe_val(props.get("summary.quickStats.hostMemoryUsage")),
            "overall_status": _safe_val(props.get("overallStatus")),
            "vm_count": len(list(vm_refs)) if vm_refs else 0,
            "vcenter": vcenter_host,
            "moref": _moref(obj),
        }
        result.append({"name": name, "type": "vsphere_resource_pool", "data": data})
    return result


# ---------------------------------------------------------------------------
# Two-speed metrics — Speed 1: pulse (every 15 min)
# ---------------------------------------------------------------------------

_VM_PULSE_PROPS = [
    "name",
    "config.template",
    "runtime.powerState",
    "runtime.host",
    "summary.quickStats.overallCpuUsage",
    "summary.quickStats.hostMemoryUsage",
    "summary.quickStats.uptimeSeconds",
    "summary.quickStats.balloonedMemory",
    "summary.overallStatus",
    "guest.ipAddress",
]

_ESXI_PULSE_PROPS = [
    "name",
    "summary.runtime.connectionState",
    "summary.runtime.inMaintenanceMode",
    "summary.runtime.powerState",
    "summary.quickStats.overallCpuUsage",
    "summary.quickStats.hostMemoryUsage",
    "summary.quickStats.uptime",
    "summary.overallStatus",
]


# ---------------------------------------------------------------------------
# Phase F — vCenter-scoped lists (licenses / alarms / permissions / sessions).
# Not keyed by moref; the agent delete-reinserts them per vcenter each run.
# All read-only manager queries. datetimes are isoformat'd for the agent.
# ---------------------------------------------------------------------------
def _iso(dt) -> str | None:
    return dt.isoformat() if hasattr(dt, "isoformat") else None


def collect_licenses(content, vcenter_host: str) -> list[dict]:
    out = []
    for lic in content.licenseManager.licenses or []:
        props = {
            p.key: p.value for p in (getattr(lic, "properties", None) or []) if hasattr(p, "key")
        }
        out.append(
            {
                "name": getattr(lic, "name", None),
                "edition_key": getattr(lic, "editionKey", None),
                "total": getattr(lic, "total", None),
                "used": getattr(lic, "used", None),
                "expiration": _iso(props.get("expirationDate")),
            }
        )
    return out


def collect_triggered_alarms(content, vcenter_host: str) -> list[dict]:
    out = []
    for a in content.rootFolder.triggeredAlarmState or []:
        info = getattr(getattr(a, "alarm", None), "info", None)
        ent = getattr(a, "entity", None)
        out.append(
            {
                "alarm_name": getattr(info, "name", None),
                "entity_name": getattr(ent, "name", None),
                "entity_type": type(ent).__name__ if ent else None,
                "overall_status": _safe_val(getattr(a, "overallStatus", None)),
                "acknowledged": getattr(a, "acknowledged", None),
                "triggered_at": _iso(getattr(a, "time", None)),
            }
        )
    return out


def collect_permissions(content, vcenter_host: str) -> list[dict]:
    am = content.authorizationManager
    roles = {r.roleId: r.name for r in (getattr(am, "roleList", None) or [])}
    out = []
    for p in am.RetrieveAllPermissions() or []:
        ent = getattr(p, "entity", None)
        out.append(
            {
                "principal": getattr(p, "principal", None),
                "role_id": getattr(p, "roleId", None),
                "role_name": roles.get(getattr(p, "roleId", None)),
                "is_group": getattr(p, "group", None),
                "propagate": getattr(p, "propagate", None),
                "entity": getattr(ent, "name", None),
            }
        )
    return out


def collect_sessions(content, vcenter_host: str) -> list[dict]:
    out = []
    for sess in content.sessionManager.sessionList or []:
        out.append(
            {
                "user_name": getattr(sess, "userName", None),
                "full_name": getattr(sess, "fullName", None),
                "login_time": _iso(getattr(sess, "loginTime", None)),
                "last_active": _iso(getattr(sess, "lastActiveTime", None)),
                "ip_address": getattr(sess, "ipAddress", None),
                "user_agent": getattr(sess, "userAgent", None),
            }
        )
    return out


def collect_vm_pulse(content, vcenter_host: str) -> list[dict]:
    """Lightweight pulse: power state + quickStats for non-template VMs only."""
    objects = _collect_properties(content, vim.VirtualMachine, _VM_PULSE_PROPS)
    result = []
    for obj in objects:
        props = _props_to_dict(obj)
        if bool(props.get("config.template", False)):
            continue
        host_ref = props.get("runtime.host")
        esxi_host = getattr(host_ref, "name", str(host_ref)) if host_ref else ""
        data = {
            "power_state": _safe_val(props.get("runtime.powerState")),
            "esxi_host": esxi_host,
            "cpu_usage_mhz": _safe_val(props.get("summary.quickStats.overallCpuUsage")),
            "memory_usage_mb": _safe_val(props.get("summary.quickStats.hostMemoryUsage")),
            "uptime_seconds": _safe_val(props.get("summary.quickStats.uptimeSeconds")),
            "ballooned_memory_mb": _safe_val(props.get("summary.quickStats.balloonedMemory")),
            "overall_status": _safe_val(props.get("summary.overallStatus")),
            "ip_address": _safe_val(props.get("guest.ipAddress")),
            "vcenter": vcenter_host,
            "moref": _moref(obj),
            "pulse": True,
        }
        result.append(
            {"name": _safe_val(props.get("name", "")), "type": "vsphere_vm", "data": data}
        )
    return result


def collect_esxi_pulse(content, vcenter_host: str) -> list[dict]:
    """Lightweight pulse: connection state + quickStats for ESXi hosts."""
    objects = _collect_properties(content, vim.HostSystem, _ESXI_PULSE_PROPS)
    result = []
    for obj in objects:
        props = _props_to_dict(obj)
        data = {
            "connection_state": _safe_val(props.get("summary.runtime.connectionState")),
            "in_maintenance_mode": _safe_val(props.get("summary.runtime.inMaintenanceMode")),
            "power_state": _safe_val(props.get("summary.runtime.powerState")),
            "cpu_usage_mhz": _safe_val(props.get("summary.quickStats.overallCpuUsage")),
            "memory_usage_mb": _safe_val(props.get("summary.quickStats.hostMemoryUsage")),
            "uptime_seconds": _safe_val(props.get("summary.quickStats.uptime")),
            "overall_status": _safe_val(props.get("summary.overallStatus")),
            "vcenter": vcenter_host,
            "moref": _moref(obj),
            "pulse": True,
        }
        result.append(
            {"name": _safe_val(props.get("name", "")), "type": "vsphere_host", "data": data}
        )
    return result


# ---------------------------------------------------------------------------
# Two-speed metrics — Speed 2: QueryPerf history (6h collection, opt-in)
# ---------------------------------------------------------------------------

# {full_counter_key: (output_field_name, aggregation)}
# full_counter_key = "<group>.<name>.<rollupType>" matching vim.PerformanceManager counter attrs.
_PERF_COUNTER_SPECS: dict[str, tuple[str, str]] = {
    "cpu.usage.average": ("perf_cpu_usage_pct_avg", "avg"),
    "cpu.ready.summation": ("perf_cpu_ready_ms_sum", "sum"),
    "mem.usage.average": ("perf_mem_usage_pct_avg", "avg"),
    "disk.read.average": ("perf_disk_read_kbps_avg", "avg"),
    "disk.write.average": ("perf_disk_write_kbps_avg", "avg"),
    "net.received.average": ("perf_net_rx_kbps_avg", "avg"),
    "net.transmitted.average": ("perf_net_tx_kbps_avg", "avg"),
}


def _build_counter_id_map(perf_mgr) -> dict[int, tuple[str, str]]:
    """Return {counterId: (output_field, agg)} for counters matching _PERF_COUNTER_SPECS."""
    wanted = set(_PERF_COUNTER_SPECS)
    result: dict[int, tuple[str, str]] = {}
    for counter in perf_mgr.perfCounter or []:
        full_key = f"{counter.groupInfo.key}.{counter.nameInfo.key}.{counter.rollupType}"
        if full_key in wanted:
            result[counter.key] = _PERF_COUNTER_SPECS[full_key]
    return result


def collect_vm_perf_history(
    content,
    vcenter_host: str,
    interval_id: int = 300,
    max_samples: int = 12,
    batch_size: int = 25,
) -> dict[str, dict]:
    """
    Query historical performance metrics for powered-on VMs via QueryPerf.
    Returns {vm_name: {perf_field: value, ...}} for merging into inventory records.
    Only called when vsphere_collect_perf_history=True (default off).
    """
    if not _PYVMOMI_AVAILABLE:
        return {}

    perf_mgr = content.perfManager
    id_to_field = _build_counter_id_map(perf_mgr)
    if not id_to_field:
        logger.warning("VsphereAgent perf: no matching counters on %s", vcenter_host)
        return {}

    vm_ref_props = ["name", "runtime.powerState", "config.template"]
    objects = _collect_properties(content, vim.VirtualMachine, vm_ref_props)

    powered_on: list[tuple[str, object]] = []
    for obj in objects:
        props = _props_to_dict(obj)
        if bool(props.get("config.template", False)):
            continue
        if _safe_val(props.get("runtime.powerState")) != "poweredOn":
            continue
        powered_on.append((_safe_val(props.get("name", "")), obj.obj))

    if not powered_on:
        return {}

    metric_ids = [
        vim.PerformanceManager.MetricId(counterId=cid, instance="") for cid in id_to_field
    ]

    perf_data: dict[str, dict] = {}

    for batch_start in range(0, len(powered_on), batch_size):
        batch = powered_on[batch_start : batch_start + batch_size]
        mor_to_name: dict = {mor: name for name, mor in batch}
        query_specs = [
            vim.PerformanceManager.QuerySpec(
                entity=mor,
                metricId=metric_ids,
                intervalId=interval_id,
                maxSample=max_samples,
                format="normal",
            )
            for _, mor in batch
        ]
        try:
            results = perf_mgr.QueryPerf(querySpec=query_specs)
        except Exception as exc:
            logger.warning("VsphereAgent perf: QueryPerf batch failed on %s: %s", vcenter_host, exc)
            continue

        for entity_result in results or []:
            vm_name = mor_to_name.get(entity_result.entity)
            if vm_name is None:
                continue
            vm_perf: dict = {"perf_samples": 0, "perf_interval_id": interval_id}
            for series in entity_result.value or []:
                mapping = id_to_field.get(series.id.counterId)
                if mapping is None:
                    continue
                out_field, agg = mapping
                values = [v for v in (series.value or []) if v is not None and v >= 0]
                if not values:
                    continue
                vm_perf["perf_samples"] = max(vm_perf["perf_samples"], len(values))
                if agg == "sum":
                    vm_perf[out_field] = sum(values)
                else:
                    avg = sum(values) / len(values)
                    # vSphere's PerformanceManager reports 'percent'-unit counters
                    # (cpu.usage.average, mem.usage.average) as integers in
                    # hundredths of a percent — e.g. a raw value of 4500 means
                    # 45.00%. Divide by 100 so *_pct_avg fields hold an actual
                    # 0-100 percentage, matching both the field name and the DB
                    # column semantics (db/models/vsphere.py). Non-percent 'avg'
                    # counters (disk/net kBps) are stored as-is.
                    if out_field.endswith("_pct_avg"):
                        avg /= 100
                    vm_perf[out_field] = round(avg, 2)
            if vm_perf["perf_samples"] > 0:
                perf_data[vm_name] = vm_perf

    return perf_data


# ---------------------------------------------------------------------------
# Backward-compatible @tool decorated functions (used by LangChain chat agent)
# ---------------------------------------------------------------------------


def _configured_vcenter_hosts(s) -> list[str]:
    """Return every configured vCenter hostname. ``vsphere_hosts`` (comma-
    separated) takes precedence over the single ``vsphere_host``, matching
    ``VsphereAgent._get_vcenter_hosts`` — the chat tools must scan the same
    set of vCenters the primary ETL collector does, not just the first one."""
    hosts_str = s.vsphere_hosts or s.vsphere_host
    return [h.strip() for h in hosts_str.split(",") if h.strip()]


def _collect_across_vcenters(s, collect_fn, label: str) -> list[dict]:
    """Connect to each configured vCenter in turn, run ``collect_fn(content,
    host)`` against it, and concatenate the results. A single unreachable
    vCenter is logged and skipped (so one bad entry in a multi-vCenter list
    doesn't blank the whole answer); if every vCenter fails, raises
    ToolException (TRK-024 behavior, preserved for the single-vCenter case)."""
    hosts = _configured_vcenter_hosts(s)
    result: list[dict] = []
    errors: list[str] = []
    for host in hosts:
        try:
            si = _connect(
                host,
                s.vsphere_user,
                s.vsphere_password,
                s.vsphere_ssl_verify,
                s.vsphere_connect_timeout,
            )
            try:
                content = si.RetrieveContent()
                result.extend(collect_fn(content, host))
            finally:
                Disconnect(si)
        except Exception as exc:
            logger.warning("vSphere %s collection failed for %s: %s", label, host, exc)
            errors.append(f"{host}: {exc}")
    if errors and not result:
        raise ToolException(
            f"vSphere {label} collection failed: {'; '.join(errors)}"
        )
    return result


@tool
def vsphere_vms_tool() -> list[dict]:
    """List all VMware vSphere VMs with power state and guest info (read-only)."""
    s = get_settings()
    if not s.vsphere_host and not s.vsphere_hosts:
        logger.info("vsphere_host not configured — skipping VM collection")
        return []
    if not _PYVMOMI_AVAILABLE:
        logger.warning("pyVmomi not installed — skipping vSphere VM collection")
        return []
    return _collect_across_vcenters(s, collect_vms, "VM")


@tool
def vsphere_hosts_tool() -> list[dict]:
    """List all VMware ESXi hosts with connection state and capacity (read-only)."""
    s = get_settings()
    if not s.vsphere_host and not s.vsphere_hosts:
        logger.info("vsphere_host not configured — skipping host collection")
        return []
    if not _PYVMOMI_AVAILABLE:
        logger.warning("pyVmomi not installed — skipping vSphere host collection")
        return []
    return _collect_across_vcenters(s, collect_esxi_hosts, "host")


@tool
def vsphere_datastores_tool() -> list[dict]:
    """List all VMware datastores with capacity and free space (read-only)."""
    s = get_settings()
    if not s.vsphere_host and not s.vsphere_hosts:
        logger.info("vsphere_host not configured — skipping datastore collection")
        return []
    if not _PYVMOMI_AVAILABLE:
        logger.warning("pyVmomi not installed — skipping vSphere datastore collection")
        return []
    return _collect_across_vcenters(s, collect_datastores, "datastore")
