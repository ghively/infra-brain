"""
Comprehensive tests for the rewritten VsphereAgent and vSphere tools.
All tests mock pyVmomi so they run without a real vCenter connection.
"""

from unittest.mock import MagicMock, patch

import pytest

from infra_brain.agents.base import CollectorSkipped
from infra_brain.agents.vsphere import VsphereAgent
from infra_brain.tools.vsphere import (
    _safe_val,
    _count_snapshots,
    collect_vms,
    collect_esxi_hosts,
    collect_datastores,
    collect_clusters,
    collect_datacenters,
    collect_networks,
    collect_vm_pulse,
    collect_esxi_pulse,
    collect_vm_perf_history,
    _build_counter_id_map,
)

from tests.support.pg import make_engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    vsphere_hosts="",
    vsphere_host="",
    vsphere_user="admin",
    vsphere_password="secret",
    vsphere_ssl_verify=False,
    vsphere_connect_timeout=30,
):
    agent = VsphereAgent.__new__(VsphereAgent)
    agent.callbacks = []
    s = MagicMock()
    s.vsphere_hosts = vsphere_hosts
    s.vsphere_host = vsphere_host
    s.vsphere_user = vsphere_user
    s.vsphere_password = vsphere_password
    s.vsphere_ssl_verify = vsphere_ssl_verify
    s.vsphere_connect_timeout = vsphere_connect_timeout
    return agent, s


def _make_obj_content(props_dict: dict):
    """Build a mock ObjectContent with propSet from a flat dict."""
    obj = MagicMock()
    prop_list = []
    for name, val in props_dict.items():
        p = MagicMock()
        p.name = name
        p.val = val
        prop_list.append(p)
    obj.propSet = prop_list
    return obj


def _mock_content_with(objects_by_type: dict):
    """
    Return a mock content object whose _collect_properties will yield
    objects_by_type[vimtype] when called.
    """
    content = MagicMock()
    return content


# ---------------------------------------------------------------------------
# 1. test_no_vcenter_configured
# ---------------------------------------------------------------------------


def test_no_vcenter_configured():
    agent, settings = _make_agent(vsphere_hosts="", vsphere_host="")
    with (
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        pytest.raises(CollectorSkipped),
    ):
        agent.collect()


# ---------------------------------------------------------------------------
# 2. test_pyvmomi_unavailable
# ---------------------------------------------------------------------------


def test_pyvmomi_unavailable():
    agent, settings = _make_agent(vsphere_host="vc1.example.com")
    with (
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", False),
        pytest.raises(CollectorSkipped),
    ):
        agent._collect_vcenter("vc1.example.com", settings)


# ---------------------------------------------------------------------------
# 3. test_connection_failure
# ---------------------------------------------------------------------------


def test_connection_failure():
    agent, settings = _make_agent(vsphere_host="vc1.example.com")
    with (
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere._connect", side_effect=Exception("Connection refused")),
        patch("infra_brain.tools.vsphere.Disconnect"),
    ):
        result = agent._collect_vcenter("vc1.example.com", settings)
    assert result == []


# ---------------------------------------------------------------------------
# 4. test_collect_vms
# ---------------------------------------------------------------------------


def test_collect_vms():
    # Build mock host ref
    host_ref = MagicMock()
    host_ref.name = "esxi-01.example.com"

    # Resource pool ref (TRK-104): collector reads its _moId
    rp_ref = MagicMock()
    rp_ref._moId = "resgroup-42"

    # NIC with IPs + MAC (KG-5: the MAC rides along for hard-identifier
    # corroboration against Rapid7's mac in graph_phase3).
    nic1 = MagicMock()
    nic1.ipAddress = ["10.0.0.1", "fe80::1"]
    nic1.macAddress = "aa:bb:cc:dd:ee:ff"

    # Datastore ref
    ds_ref = MagicMock()
    ds_ref.name = "datastore1"

    # Network ref
    net_ref = MagicMock()
    net_ref.name = "VM Network"

    # Snapshot info
    snap_info = MagicMock()
    snap1 = MagicMock()
    snap1.childSnapshotList = []
    snap_info.rootSnapshotList = [snap1]

    vm1_props = {
        "name": "vm-prod-01",
        "config.uuid": "uuid-1",
        "config.instanceUuid": "iuuid-1",
        "config.guestFullName": "Ubuntu Linux (64-bit)",
        "config.guestId": "ubuntu64Guest",
        "config.hardware.numCPU": 4,
        "config.hardware.memoryMB": 8192,
        "config.template": False,
        "config.version": "vmx-19",
        "config.hardware.numCoresPerSocket": 2,
        "runtime.powerState": "poweredOn",
        "runtime.bootTime": None,
        "runtime.host": host_ref,
        "runtime.maxCpuUsage": 8000,
        "runtime.maxMemoryUsage": 8192,
        "runtime.consolidationNeeded": False,
        "guest.hostName": "vm-prod-01",
        "guest.ipAddress": "10.0.0.1",
        "guest.toolsStatus": "toolsOk",
        "guest.toolsVersion": "12345",
        "guest.toolsRunningStatus": "guestToolsRunning",
        "guest.net": [nic1],
        "summary.quickStats.overallCpuUsage": 500,
        "summary.quickStats.overallMemoryUsage": 4096,
        "summary.quickStats.uptimeSeconds": 86400,
        "summary.quickStats.balloonedMemory": 0,
        "summary.storage.committed": 53687091200,  # 50 GB
        "summary.storage.uncommitted": 10737418240,  # 10 GB
        "summary.overallStatus": "green",
        "snapshot": snap_info,
        "datastore": [ds_ref],
        "network": [net_ref],
        "resourcePool": rp_ref,
    }

    # Powered-off VM (no snapshots, no net)
    vm2_props = {
        "name": "vm-dev-01",
        "config.uuid": "uuid-2",
        "config.instanceUuid": "iuuid-2",
        "config.guestFullName": "Windows Server 2019",
        "config.guestId": "windows9Server64Guest",
        "config.hardware.numCPU": 2,
        "config.hardware.memoryMB": 4096,
        "config.template": False,
        "config.version": "vmx-19",
        "runtime.powerState": "poweredOff",
        "runtime.host": host_ref,
        "guest.net": None,
        "summary.storage.committed": 21474836480,  # 20 GB
        "summary.storage.uncommitted": 0,
        "summary.overallStatus": "yellow",
        "snapshot": None,
        "datastore": [ds_ref],
        "network": [],
    }

    # Template (should return as vsphere_template type)
    tpl_props = {
        "name": "tpl-ubuntu-22",
        "config.uuid": "uuid-tpl",
        "config.template": True,
        "config.hardware.numCPU": 2,
        "config.hardware.memoryMB": 2048,
        "runtime.powerState": "poweredOff",
        "runtime.host": host_ref,
        "guest.net": None,
        "summary.storage.committed": 0,
        "summary.storage.uncommitted": 0,
        "snapshot": None,
        "datastore": [],
        "network": [],
    }

    mock_objects = [
        _make_obj_content(vm1_props),
        _make_obj_content(vm2_props),
        _make_obj_content(tpl_props),
    ]

    with patch("infra_brain.tools.vsphere._collect_properties", return_value=mock_objects):
        content = MagicMock()
        results = collect_vms(content, "vc1.example.com")

    # 2 VMs + 1 template
    assert len(results) == 3
    vms = [r for r in results if r["type"] == "vsphere_vm"]
    templates = [r for r in results if r["type"] == "vsphere_template"]
    assert len(vms) == 2
    assert len(templates) == 1

    vm1 = next(r for r in vms if r["name"] == "vm-prod-01")
    assert vm1["data"]["power_state"] == "poweredOn"
    assert vm1["data"]["num_cpu"] == 4
    assert vm1["data"]["memory_mb"] == 8192
    assert vm1["data"]["snapshot_count"] == 1
    assert "10.0.0.1" in vm1["data"]["all_ips"]
    assert vm1["data"]["mac_addresses"] == ["aa:bb:cc:dd:ee:ff"]
    assert vm1["data"]["disk_committed_gb"] == round(53687091200 / 1e9, 1)
    assert vm1["data"]["disk_uncommitted_gb"] == round(10737418240 / 1e9, 1)
    assert vm1["data"]["esxi_host"] == "esxi-01.example.com"
    assert vm1["data"]["vcenter"] == "vc1.example.com"
    # TRK-104: resource pool moref captured from the resourcePool ref; VMs
    # without a resourcePool prop fall back to None.
    assert vm1["data"]["resource_pool_moref"] == "resgroup-42"
    vm2 = next(r for r in vms if r["name"] == "vm-dev-01")
    assert vm2["data"]["resource_pool_moref"] is None


# ---------------------------------------------------------------------------
# 5. test_collect_esxi_hosts
# ---------------------------------------------------------------------------


def test_collect_esxi_hosts():
    cluster_ref = MagicMock()
    cluster_ref.name = "cluster-prod"

    ds_ref = MagicMock()
    ds_ref.name = "shared-ds"

    vm_ref1 = MagicMock()
    vm_ref2 = MagicMock()

    host1_props = {
        "name": "esxi-01.example.com",
        "summary.hardware.uuid": "hw-uuid-1",
        "summary.hardware.vendor": "Dell Inc.",
        "summary.hardware.model": "PowerEdge R640",
        "summary.hardware.cpuModel": "Intel(R) Xeon(R) Gold 6230",
        "summary.hardware.numCpuCores": 20,
        "summary.hardware.numCpuThreads": 40,
        "summary.hardware.numCpuPkgs": 1,
        "summary.hardware.cpuMhz": 2100,
        "summary.hardware.memorySize": 137438953472,  # 128 GB
        "summary.hardware.numNics": 4,
        "summary.hardware.numHBAs": 2,
        "summary.runtime.connectionState": "connected",
        "summary.runtime.powerState": "poweredOn",
        "summary.runtime.inMaintenanceMode": False,
        "summary.runtime.bootTime": None,
        "summary.runtime.standbyMode": "none",
        "summary.config.product.version": "7.0.3",
        "summary.config.product.build": "19193900",
        "summary.config.product.fullName": "VMware ESXi 7.0.3 build-19193900",
        "summary.quickStats.overallCpuUsage": 2500,
        "summary.quickStats.overallMemoryUsage": 65536,
        "summary.quickStats.uptime": 1209600,
        "summary.overallStatus": "green",
        "config.network.dnsConfig.hostName": "esxi-01",
        "config.network.dnsConfig.domainName": "example.com",
        "config.network.dnsConfig.ipAddress": ["192.0.2.12"],
        "parent": cluster_ref,
        "datastore": [ds_ref],
        "vm": [vm_ref1, vm_ref2],
    }

    host2_props = {
        "name": "esxi-02.example.com",
        "summary.hardware.memorySize": 68719476736,  # 64 GB
        "summary.config.product.version": "8.0.0",
        "summary.runtime.connectionState": "connected",
        "parent": cluster_ref,
        "datastore": [],
        "vm": [],
    }

    mock_objects = [_make_obj_content(host1_props), _make_obj_content(host2_props)]

    with patch("infra_brain.tools.vsphere._collect_properties", return_value=mock_objects):
        content = MagicMock()
        results = collect_esxi_hosts(content, "vc1.example.com")

    assert len(results) == 2
    h1 = next(r for r in results if r["name"] == "esxi-01.example.com")
    assert h1["type"] == "vsphere_host"
    assert h1["data"]["memory_gb"] == round(137438953472 / 1e9, 1)
    assert h1["data"]["version"] == "7.0.3"
    assert h1["data"]["vm_count"] == 2
    assert h1["data"]["cluster_or_parent"] == "cluster-prod"
    assert h1["data"]["vcenter"] == "vc1.example.com"

    h2 = next(r for r in results if r["name"] == "esxi-02.example.com")
    assert h2["data"]["memory_gb"] == round(68719476736 / 1e9, 1)
    assert h2["data"]["version"] == "8.0.0"


# ---------------------------------------------------------------------------
# 6. test_collect_datastores
# ---------------------------------------------------------------------------


def test_collect_datastores():
    mount1 = MagicMock()
    mount1.key = MagicMock()  # HostSystem ref
    mount2 = MagicMock()
    mount2.key = MagicMock()

    vm_ref = MagicMock()

    ds1_props = {
        "name": "datastore-prod-01",
        "summary.type": "VMFS",
        "summary.capacity": 10737418240000,  # ~10 TB
        "summary.freeSpace": 5368709120000,  # ~5 TB -> 50% used
        "summary.accessible": True,
        "summary.maintenanceMode": "normal",
        "summary.url": "ds:///vmfs/volumes/abc123/",
        "summary.multipleHostAccess": True,
        "overallStatus": "green",
        "host": [mount1, mount2],
        "vm": [vm_ref],
    }

    ds2_props = {
        "name": "datastore-nfs-01",
        "summary.type": "NFS",
        "summary.capacity": 21474836480000,  # ~20 TB
        "summary.freeSpace": 17179869184000,  # ~16 TB
        "summary.accessible": True,
        "summary.maintenanceMode": "normal",
        "overallStatus": "yellow",
        "host": [],
        "vm": [],
    }

    mock_objects = [_make_obj_content(ds1_props), _make_obj_content(ds2_props)]

    with patch("infra_brain.tools.vsphere._collect_properties", return_value=mock_objects):
        content = MagicMock()
        results = collect_datastores(content, "vc1.example.com")

    assert len(results) == 2
    ds1 = next(r for r in results if r["name"] == "datastore-prod-01")
    assert ds1["type"] == "vsphere_datastore"
    assert ds1["data"]["capacity_gb"] == round(10737418240000 / 1e9, 1)
    assert ds1["data"]["free_gb"] == round(5368709120000 / 1e9, 1)
    assert ds1["data"]["used_pct"] == 50.0
    assert ds1["data"]["host_count"] == 2
    assert ds1["data"]["vm_count"] == 1
    assert ds1["data"]["vcenter"] == "vc1.example.com"


# ---------------------------------------------------------------------------
# 7. test_collect_clusters
# ---------------------------------------------------------------------------


def test_collect_clusters():
    host_ref = MagicMock()
    host_ref.name = "esxi-01.example.com"

    ds_ref = MagicMock()
    ds_ref.name = "shared-ds"

    cluster_props = {
        "name": "cluster-prod",
        "configuration.dasConfig.enabled": True,
        "configuration.dasConfig.failoverLevel": 1,
        "configuration.dasConfig.hostMonitoring": "enabled",
        "configuration.drsConfig.enabled": True,
        "configuration.drsConfig.defaultVmBehavior": "fullyAutomated",
        "configuration.drsConfig.vmotionRate": 3,
        "summary.numHosts": 4,
        "summary.numEffectiveHosts": 4,
        "summary.totalCpu": 168000,
        "summary.totalMemory": 549755813888,  # 512 GB
        "summary.numCpuCores": 80,
        "summary.numCpuThreads": 160,
        "summary.overallStatus": "green",
        "overallStatus": "green",
        "host": [host_ref],
        "datastore": [ds_ref],
        "network": [],
    }

    mock_objects = [_make_obj_content(cluster_props)]

    with patch("infra_brain.tools.vsphere._collect_properties", return_value=mock_objects):
        content = MagicMock()
        results = collect_clusters(content, "vc1.example.com")

    assert len(results) == 1
    c = results[0]
    assert c["type"] == "vsphere_cluster"
    assert c["data"]["ha_enabled"] is True
    assert c["data"]["drs_enabled"] is True
    assert c["data"]["total_memory_gb"] == round(549755813888 / 1e9, 1)
    assert "esxi-01.example.com" in c["data"]["host_names"]
    assert "shared-ds" in c["data"]["datastore_names"]
    assert c["data"]["vcenter"] == "vc1.example.com"


# ---------------------------------------------------------------------------
# 8. test_collect_datacenters
# ---------------------------------------------------------------------------


def test_collect_datacenters():
    parent_ref = MagicMock()
    parent_ref.name = "root-folder"

    dc1_props = {"name": "datacenter-prod", "overallStatus": "green", "parent": parent_ref}
    dc2_props = {"name": "datacenter-dev", "overallStatus": "yellow", "parent": parent_ref}

    mock_objects = [_make_obj_content(dc1_props), _make_obj_content(dc2_props)]

    with patch("infra_brain.tools.vsphere._collect_properties", return_value=mock_objects):
        content = MagicMock()
        results = collect_datacenters(content, "vc2.example.com")

    assert len(results) == 2
    names = {r["name"] for r in results}
    assert "datacenter-prod" in names
    assert "datacenter-dev" in names
    for r in results:
        assert r["type"] == "vsphere_datacenter"
        assert r["data"]["vcenter"] == "vc2.example.com"


# ---------------------------------------------------------------------------
# 9. test_collect_networks
# ---------------------------------------------------------------------------


def test_collect_networks():
    pg_props = {
        "name": "VM Network",
        "summary.accessible": True,
        "summary.ipPoolName": "",
        "overallStatus": "green",
        "host": [MagicMock(), MagicMock()],
        "vm": [MagicMock()],
    }
    dvs_props = {
        "name": "dvs-prod",
        "summary.numPorts": 128,
        "summary.uuid": "dvs-uuid-1",
        "overallStatus": "green",
    }

    def mock_collect_props(content, vimtype, properties):
        # Identify which vimtype is being collected by its string representation
        vimtype_name = str(vimtype)
        if "Distributed" in vimtype_name and "Portgroup" not in vimtype_name:
            return [_make_obj_content(dvs_props)]
        elif "Portgroup" in vimtype_name or "DistributedVirtualPortgroup" in vimtype_name:
            return []
        else:
            # Standard Network
            return [_make_obj_content(pg_props)]

    with patch("infra_brain.tools.vsphere._collect_properties", side_effect=mock_collect_props):
        content = MagicMock()
        results = collect_networks(content, "vc1.example.com")

    # Should have at least portgroup and dvswitch
    types = {r["type"] for r in results}
    assert "vsphere_portgroup" in types or "vsphere_dvswitch" in types


# ---------------------------------------------------------------------------
# 10. test_multi_vcenter
# ---------------------------------------------------------------------------


def test_multi_vcenter():
    agent, settings = _make_agent(vsphere_hosts="vc1.example.com,vc2.example.com")

    collect_calls = []

    def fake_collect_vcenter(host, s, scope="all", errors=None):
        collect_calls.append(host)
        return [{"name": f"vm-from-{host}", "type": "vsphere_vm", "data": {"vcenter": host}}]

    agent._collect_vcenter = fake_collect_vcenter
    with patch("infra_brain.agents.vsphere.get_settings", return_value=settings):
        outcome = agent.collect()

    assert len(collect_calls) == 2
    assert "vc1.example.com" in collect_calls
    assert "vc2.example.com" in collect_calls
    results = outcome.items
    # 2 VM items (from the faked collector) + 2 vsphere_vcenter self-nodes
    # (one per successfully "connected" host — TRK-171 follow-up).
    assert len(results) == 4
    vcenters = {r["data"]["vcenter"] for r in results}
    assert "vc1.example.com" in vcenters
    assert "vc2.example.com" in vcenters
    vcenter_nodes = [r for r in results if r["type"] == "vsphere_vcenter"]
    assert {n["name"] for n in vcenter_nodes} == {"vc1.example.com", "vc2.example.com"}
    for n in vcenter_nodes:
        assert n["data"]["moref"] == f"vcenter:{n['name']}"


# ---------------------------------------------------------------------------
# 10b. test_vcenter_node_suppressed_on_connect_failure
# ---------------------------------------------------------------------------


def test_vcenter_node_suppressed_on_connect_failure():
    """An unreachable vCenter must not get a ghost vsphere_vcenter node —
    only a host this cycle actually connected to should be represented."""
    agent, settings = _make_agent(vsphere_hosts="vc1.example.com,vc2.example.com")

    def fake_collect_vcenter(host, s, scope="all", errors=None):
        if host == "vc2.example.com":
            errors.append(f"cannot connect to {host}: timed out")
            return []
        return [{"name": f"vm-from-{host}", "type": "vsphere_vm", "data": {"vcenter": host}}]

    agent._collect_vcenter = fake_collect_vcenter
    with patch("infra_brain.agents.vsphere.get_settings", return_value=settings):
        outcome = agent.collect()

    vcenter_nodes = {r["name"] for r in outcome.items if r["type"] == "vsphere_vcenter"}
    assert vcenter_nodes == {"vc1.example.com"}
    assert any("cannot connect to vc2.example.com" in e for e in outcome.errors)


# ---------------------------------------------------------------------------
# 10c. test_qualified_name_exempts_vcenter_self_node
# ---------------------------------------------------------------------------


def test_qualified_name_exempts_vcenter_self_node():
    """The vcenter's own Resource must keep its bare hostname as the name —
    not '<host> (<host>)' — so ASSIGNED_TO's exact-match lookup against
    vsphere_licenses.vcenter can resolve it (TRK-171 follow-up)."""
    agent, _ = _make_agent()
    assert (
        agent._qualified_name(None, "vc1.example.com", "vc1.example.com", "vsphere_vcenter")
        == "vc1.example.com"
    )
    # Every other type is still qualified as before (no moref -> no collision
    # check, session is never touched).
    assert (
        agent._qualified_name(None, "esx01", "vc1.example.com", "vsphere_host")
        == "esx01 (vc1.example.com)"
    )


# ---------------------------------------------------------------------------
# 10d. test_qualified_name_disambiguates_same_name_different_moref (#184)
# ---------------------------------------------------------------------------


def _seed_vsphere_resource(session, *, name, moref, item_type="vsphere_vm"):
    import uuid as uuid_mod
    from datetime import UTC, datetime

    from infra_brain.db.models import Resource

    res = Resource(
        id=uuid_mod.uuid4(),
        domain="vsphere",
        type=item_type,
        name=name,
        source="test",
        zone="corp",
        last_seen=datetime.now(UTC),
        metadata_={"moref": moref, "vcenter": "vc1.example.com"},
    )
    session.add(res)
    session.commit()
    return res


def test_qualified_name_unchanged_when_no_collision():
    """No existing row at this vCenter-qualified name -> unchanged, byte-
    identical behaviour to before #184 (the overwhelming common case)."""
    from sqlalchemy.orm import Session


    engine = make_engine()
    agent, _ = _make_agent()

    with Session(engine) as s:
        result = agent._qualified_name(s, "web01", "vc1.example.com", "vsphere_vm", "vm-100")

    assert result == "web01 (vc1.example.com)"


def test_qualified_name_reprocessing_same_object_is_unchanged():
    """Re-sweeping the SAME object (same moref) on a later run must not be
    treated as a collision against itself."""
    from sqlalchemy.orm import Session


    engine = make_engine()
    agent, _ = _make_agent()

    with Session(engine) as s:
        _seed_vsphere_resource(s, name="web01 (vc1.example.com)", moref="vm-100")
        result = agent._qualified_name(s, "web01", "vc1.example.com", "vsphere_vm", "vm-100")

    assert result == "web01 (vc1.example.com)"


def test_qualified_name_disambiguates_same_name_different_moref():
    """#184: two DIFFERENT vSphere objects (different morefs) sharing a
    display name in the SAME vCenter must not collapse into one Resource row
    -- the vCenter-only qualifier was identical for both, so the second
    upsert silently overwrote the first object's data. A detected collision
    (existing row's stored moref differs) now gets a moref suffix instead."""
    from sqlalchemy.orm import Session


    engine = make_engine()
    agent, _ = _make_agent()

    with Session(engine) as s:
        # First object ("web01", moref vm-100) already has the plain qualified name.
        _seed_vsphere_resource(s, name="web01 (vc1.example.com)", moref="vm-100")
        # A second, genuinely different object shares the same display name
        # and vCenter, but has a different moref -> must be disambiguated.
        result = agent._qualified_name(s, "web01", "vc1.example.com", "vsphere_vm", "vm-200")

    assert result == "web01 (vc1.example.com) [vm-200]"
    # The first object's own name/moref must be completely untouched.
    assert result != "web01 (vc1.example.com)"


# ---------------------------------------------------------------------------
# 11. test_single_connection_per_vcenter
# ---------------------------------------------------------------------------


def test_single_connection_per_vcenter():
    agent, settings = _make_agent(vsphere_host="vc1.example.com")
    settings.vsphere_hosts = ""
    settings.vsphere_collect_perf_history = False

    mock_si = MagicMock()
    mock_content = MagicMock()
    mock_si.RetrieveContent.return_value = mock_content

    with (
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere._connect", return_value=mock_si) as mock_connect,
        patch("infra_brain.tools.vsphere.Disconnect"),
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        patch("infra_brain.tools.vsphere.collect_vms", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_esxi_hosts", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datastores", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_clusters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datacenters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_networks", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_resource_pools", return_value=[]),
    ):
        agent._collect_vcenter("vc1.example.com", settings)

    mock_connect.assert_called_once_with(
        "vc1.example.com",
        settings.vsphere_user,
        settings.vsphere_password,
        settings.vsphere_ssl_verify,
        settings.vsphere_connect_timeout,
    )


# ---------------------------------------------------------------------------
# 12. test_vsphere_agent_domain
# ---------------------------------------------------------------------------


def test_vsphere_agent_domain():
    agent = VsphereAgent.__new__(VsphereAgent)
    agent.callbacks = []
    assert agent.domain == "vsphere"


# ---------------------------------------------------------------------------
# Additional: _safe_val unit tests
# ---------------------------------------------------------------------------


def test_safe_val_primitives():
    assert _safe_val(42) == 42
    assert _safe_val(3.14) == 3.14
    assert _safe_val("hello") == "hello"
    assert _safe_val(True) is True
    assert _safe_val(None) is None


def test_safe_val_datetime():
    from datetime import datetime

    dt = datetime(2024, 1, 15, 10, 30, 0)
    result = _safe_val(dt)
    assert "2024-01-15" in result
    assert "T" in result


def test_safe_val_mo_ref():
    mo = MagicMock()
    mo._moId = "vm-123"
    mo.name = "my-vm"
    result = _safe_val(mo)
    assert result == "my-vm"


def test_safe_val_list():
    result = _safe_val([1, "two", 3.0])
    assert result == [1, "two", 3.0]


# ---------------------------------------------------------------------------
# Additional: _count_snapshots unit tests
# ---------------------------------------------------------------------------


def test_count_snapshots_none():
    assert _count_snapshots(None) == 0


def test_count_snapshots_single():
    snap_info = MagicMock()
    snap = MagicMock()
    snap.childSnapshotList = []
    snap_info.rootSnapshotList = [snap]
    assert _count_snapshots(snap_info) == 1


def test_count_snapshots_nested():
    snap_info = MagicMock()
    child = MagicMock()
    child.childSnapshotList = []
    snap = MagicMock()
    snap.childSnapshotList = [child]
    snap_info.rootSnapshotList = [snap]
    assert _count_snapshots(snap_info) == 2


# ---------------------------------------------------------------------------
# Two-speed metrics: pulse tests
# ---------------------------------------------------------------------------


def test_collect_vm_pulse_excludes_templates():
    host_ref = MagicMock()
    host_ref.name = "esxi-01"

    vm_props = {
        "name": "vm-prod-01",
        "config.template": False,
        "runtime.powerState": "poweredOn",
        "runtime.host": host_ref,
        "summary.quickStats.overallCpuUsage": 800,
        "summary.quickStats.overallMemoryUsage": 4096,
        "summary.quickStats.uptimeSeconds": 86400,
        "summary.quickStats.balloonedMemory": 0,
        "summary.overallStatus": "green",
        "guest.ipAddress": "10.0.0.1",
    }
    tpl_props = {
        "name": "tpl-ubuntu",
        "config.template": True,
        "runtime.powerState": "poweredOff",
        "runtime.host": host_ref,
        "summary.overallStatus": "green",
    }
    mock_objects = [_make_obj_content(vm_props), _make_obj_content(tpl_props)]

    with patch("infra_brain.tools.vsphere._collect_properties", return_value=mock_objects):
        content = MagicMock()
        results = collect_vm_pulse(content, "vc1.example.com")

    assert len(results) == 1
    r = results[0]
    assert r["name"] == "vm-prod-01"
    assert r["type"] == "vsphere_vm"
    assert r["data"]["pulse"] is True
    assert r["data"]["power_state"] == "poweredOn"
    assert r["data"]["cpu_usage_mhz"] == 800
    assert r["data"]["esxi_host"] == "esxi-01"
    assert r["data"]["vcenter"] == "vc1.example.com"


def test_collect_vm_pulse_minimal_fields():
    """Pulse records must NOT contain full-inventory fields (disk_committed_gb etc.)."""
    props = {
        "name": "vm-test",
        "config.template": False,
        "runtime.powerState": "poweredOn",
        "runtime.host": None,
        "summary.overallStatus": "green",
    }
    with patch(
        "infra_brain.tools.vsphere._collect_properties", return_value=[_make_obj_content(props)]
    ):
        content = MagicMock()
        results = collect_vm_pulse(content, "vc1.example.com")

    assert len(results) == 1
    data = results[0]["data"]
    assert "disk_committed_gb" not in data
    assert "snapshot_count" not in data
    assert "num_cpu" not in data
    assert data["pulse"] is True


def test_collect_esxi_pulse_fields():
    props = {
        "name": "esxi-01.example.com",
        "summary.runtime.connectionState": "connected",
        "summary.runtime.inMaintenanceMode": False,
        "summary.runtime.powerState": "poweredOn",
        "summary.quickStats.overallCpuUsage": 3000,
        "summary.quickStats.overallMemoryUsage": 98304,
        "summary.quickStats.uptime": 604800,
        "summary.overallStatus": "green",
    }
    with patch(
        "infra_brain.tools.vsphere._collect_properties", return_value=[_make_obj_content(props)]
    ):
        content = MagicMock()
        results = collect_esxi_pulse(content, "vc1.example.com")

    assert len(results) == 1
    r = results[0]
    assert r["type"] == "vsphere_host"
    assert r["data"]["pulse"] is True
    assert r["data"]["connection_state"] == "connected"
    assert r["data"]["cpu_usage_mhz"] == 3000
    assert r["data"]["uptime_seconds"] == 604800
    assert "memory_gb" not in r["data"]  # full-inventory field absent from pulse


def test_vsphere_agent_pulse_scope_uses_pulse_collectors():
    """scope='pulse' must call only pulse collectors, not the 7-type inventory."""
    agent, settings = _make_agent(vsphere_host="vc1.example.com")
    settings.vsphere_collect_perf_history = False

    pulse_vm_result = [{"name": "vm-01", "type": "vsphere_vm", "data": {"pulse": True}}]
    pulse_esxi_result = [{"name": "esxi-01", "type": "vsphere_host", "data": {"pulse": True}}]

    mock_si = MagicMock()
    mock_content = MagicMock()
    mock_si.RetrieveContent.return_value = mock_content

    with (
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere._connect", return_value=mock_si),
        patch("infra_brain.tools.vsphere.Disconnect"),
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        patch(
            "infra_brain.tools.vsphere.collect_vm_pulse", return_value=pulse_vm_result
        ) as mock_vm_pulse,
        patch(
            "infra_brain.tools.vsphere.collect_esxi_pulse", return_value=pulse_esxi_result
        ) as mock_esxi_pulse,
        patch("infra_brain.tools.vsphere.collect_vms", return_value=[]) as mock_vms,
        patch("infra_brain.tools.vsphere.collect_esxi_hosts", return_value=[]) as mock_hosts,
    ):
        result = agent._collect_vcenter("vc1.example.com", settings, scope="pulse")

    mock_vm_pulse.assert_called_once()
    mock_esxi_pulse.assert_called_once()
    mock_vms.assert_not_called()
    mock_hosts.assert_not_called()
    assert len(result) == 2
    assert all(r["data"]["pulse"] for r in result)


# ---------------------------------------------------------------------------
# Two-speed metrics: QueryPerf history tests
# ---------------------------------------------------------------------------


def _make_counter(group_key, name_key, rollup_type, counter_key):
    c = MagicMock()
    c.groupInfo.key = group_key
    c.nameInfo.key = name_key
    c.rollupType = rollup_type
    c.key = counter_key
    return c


def test_build_counter_id_map_matches_specs():
    counters = [
        _make_counter("cpu", "usage", "average", 6),
        _make_counter("cpu", "ready", "summation", 105),
        _make_counter("mem", "usage", "average", 24),
        _make_counter("disk", "read", "average", 125),
        _make_counter("disk", "write", "average", 126),
        _make_counter("net", "received", "average", 143),
        _make_counter("net", "transmitted", "average", 144),
        _make_counter("cpu", "wait", "summation", 200),  # not in specs — should be excluded
    ]
    perf_mgr = MagicMock()
    perf_mgr.perfCounter = counters

    result = _build_counter_id_map(perf_mgr)

    assert len(result) == 7
    assert result[6] == ("perf_cpu_usage_pct_avg", "avg")
    assert result[105] == ("perf_cpu_ready_ms_sum", "sum")
    assert result[24] == ("perf_mem_usage_pct_avg", "avg")
    assert 200 not in result


def test_collect_vm_perf_history_disabled_when_no_pyvmomi():
    with patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", False):
        result = collect_vm_perf_history(MagicMock(), "vc1.example.com")
    assert result == {}


def test_collect_vm_perf_history_skips_powered_off_and_templates():
    vm_on = _make_obj_content(
        {"name": "vm-on", "runtime.powerState": "poweredOn", "config.template": False}
    )
    vm_off = _make_obj_content(
        {"name": "vm-off", "runtime.powerState": "poweredOff", "config.template": False}
    )
    vm_tpl = _make_obj_content(
        {"name": "tpl-01", "runtime.powerState": "poweredOff", "config.template": True}
    )
    vm_on.obj = object()  # unique MOR sentinel

    counter = _make_counter("cpu", "usage", "average", 6)
    perf_mgr = MagicMock()
    perf_mgr.perfCounter = [counter]
    perf_mgr.QueryPerf.return_value = []

    content = MagicMock()
    content.perfManager = perf_mgr

    with (
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere.vim", MagicMock()),
        patch(
            "infra_brain.tools.vsphere._collect_properties", return_value=[vm_on, vm_off, vm_tpl]
        ),
    ):
        collect_vm_perf_history(content, "vc1.example.com")

    # QueryPerf was called with only one VM (the powered-on non-template)
    call_args = perf_mgr.QueryPerf.call_args
    assert call_args is not None
    specs = call_args[1].get("querySpec") or call_args[0][0]
    assert len(specs) == 1


def test_collect_vm_perf_history_aggregates_values():
    vm_obj = _make_obj_content(
        {"name": "vm-prod", "runtime.powerState": "poweredOn", "config.template": False}
    )
    sentinel_mor = object()
    vm_obj.obj = sentinel_mor

    cpu_series = MagicMock()
    cpu_series.id.counterId = 6
    cpu_series.value = [1000, 2000, 1500]  # avg → 1500

    ready_series = MagicMock()
    ready_series.id.counterId = 105
    ready_series.value = [100, 200, 300]  # sum → 600

    entity_result = MagicMock()
    entity_result.entity = sentinel_mor
    entity_result.value = [cpu_series, ready_series]

    counter_cpu = _make_counter("cpu", "usage", "average", 6)
    counter_ready = _make_counter("cpu", "ready", "summation", 105)
    perf_mgr = MagicMock()
    perf_mgr.perfCounter = [counter_cpu, counter_ready]
    perf_mgr.QueryPerf.return_value = [entity_result]

    content = MagicMock()
    content.perfManager = perf_mgr

    with (
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere.vim", MagicMock()),
        patch("infra_brain.tools.vsphere._collect_properties", return_value=[vm_obj]),
    ):
        result = collect_vm_perf_history(content, "vc1.example.com")

    assert "vm-prod" in result
    data = result["vm-prod"]
    # vSphere reports 'percent' counters in hundredths of a percent; the raw
    # average is divided by 100 before rounding (see tools/vsphere.py).
    assert data["perf_cpu_usage_pct_avg"] == round((1000 + 2000 + 1500) / 3 / 100, 2)
    assert data["perf_cpu_ready_ms_sum"] == 600
    assert data["perf_samples"] == 3


def test_perf_history_enriches_vm_records_in_full_collect():
    """scope='all' with perf flag set must merge perf data into VM items."""
    agent, settings = _make_agent(vsphere_host="vc1.example.com")
    settings.vsphere_collect_perf_history = True
    settings.vsphere_perf_interval_id = 300
    settings.vsphere_perf_max_samples = 12
    settings.vsphere_perf_batch_size = 25

    vm_items = [{"name": "vm-prod", "type": "vsphere_vm", "data": {"power_state": "poweredOn"}}]
    perf_result = {
        "vm-prod": {"perf_cpu_usage_pct_avg": 42.5, "perf_samples": 12, "perf_interval_id": 300}
    }

    mock_si = MagicMock()
    mock_si.RetrieveContent.return_value = MagicMock()

    with (
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere._connect", return_value=mock_si),
        patch("infra_brain.tools.vsphere.Disconnect"),
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        patch("infra_brain.tools.vsphere.collect_vms", return_value=vm_items),
        patch("infra_brain.tools.vsphere.collect_esxi_hosts", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datastores", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_clusters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datacenters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_networks", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_resource_pools", return_value=[]),
        patch(
            "infra_brain.tools.vsphere.collect_vm_perf_history", return_value=perf_result
        ) as mock_perf,
    ):
        result = agent._collect_vcenter("vc1.example.com", settings, scope="all")

    mock_perf.assert_called_once()
    vm = next(r for r in result if r["type"] == "vsphere_vm")
    assert vm["data"]["perf_cpu_usage_pct_avg"] == 42.5
    assert vm["data"]["perf_samples"] == 12


def test_perf_history_not_called_when_flag_off():
    """vsphere_collect_perf_history=False must not call collect_vm_perf_history."""
    agent, settings = _make_agent(vsphere_host="vc1.example.com")
    settings.vsphere_collect_perf_history = False

    mock_si = MagicMock()
    mock_si.RetrieveContent.return_value = MagicMock()

    with (
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere._connect", return_value=mock_si),
        patch("infra_brain.tools.vsphere.Disconnect"),
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        patch("infra_brain.tools.vsphere.collect_vms", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_esxi_hosts", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datastores", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_clusters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datacenters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_networks", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_resource_pools", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_vm_perf_history") as mock_perf,
    ):
        agent._collect_vcenter("vc1.example.com", settings, scope="all")

    mock_perf.assert_not_called()


def test_disconnect_failure_is_logged_not_silent(caplog):
    """#86: Disconnect(si) failing in the finally block must log, not
    silently swallow the exception with a bare `except Exception: pass`."""
    import logging

    agent, settings = _make_agent(vsphere_host="vc1.example.com")
    mock_si = MagicMock()
    mock_si.RetrieveContent.return_value = MagicMock()

    with (
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere._connect", return_value=mock_si),
        patch("infra_brain.tools.vsphere.Disconnect", side_effect=Exception("disconnect boom")),
        patch("infra_brain.tools.vsphere.collect_vms", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_esxi_hosts", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datastores", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_clusters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datacenters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_networks", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_resource_pools", return_value=[]),
        caplog.at_level(logging.DEBUG, logger="infra_brain.agents.vsphere"),
    ):
        agent._collect_vcenter("vc1.example.com", settings, scope="all")

    assert any("disconnect boom" in r.message for r in caplog.records), (
        "Disconnect() failure must be logged, not silently swallowed"
    )


# ---------------------------------------------------------------------------
# 14. test_phase_f_scoped_fetch_failure_surfaces_as_error (M-7)
# ---------------------------------------------------------------------------


def test_phase_f_scoped_fetch_failure_surfaces_as_error():
    """M-7: a Phase-F scoped fetch failure (licenses/alarms/permissions/
    sessions — e.g. a permanent privilege gap) must be recorded in the
    ``errors`` list passed into ``_collect_vcenter`` so the run downgrades to
    'partial' and the gap is visible in collection_runs, instead of being
    swallowed with only a log line.

    Preserving existing rows on a scoped-fetch failure (``scoped[kind] =
    None`` — "not collected, leave existing rows") is deliberate and CORRECT
    and must be unaffected by this fix — asserted explicitly below.
    """
    agent, settings = _make_agent(vsphere_host="vc1.example.com")
    settings.vsphere_hosts = ""
    settings.vsphere_collect_perf_history = False

    mock_si = MagicMock()
    mock_si.RetrieveContent.return_value = MagicMock()

    with (
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere._connect", return_value=mock_si),
        patch("infra_brain.tools.vsphere.Disconnect"),
        patch("infra_brain.tools.vsphere.collect_vms", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_esxi_hosts", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datastores", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_clusters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datacenters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_networks", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_resource_pools", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_licenses", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_triggered_alarms", return_value=[]),
        patch(
            "infra_brain.tools.vsphere.collect_permissions",
            side_effect=Exception("permission denied: no System.Read privilege"),
        ),
        patch("infra_brain.tools.vsphere.collect_sessions", return_value=[]),
    ):
        errors: list[str] = []
        agent._collect_vcenter("vc1.example.com", settings, scope="all", errors=errors)

    # Preserve-on-failure behavior is unchanged: permissions stayed None
    # (existing rows left untouched by _write_vcenter_scoped), the other
    # three scoped kinds collected normally.
    assert len(agent._vcenter_scoped) == 1
    scoped = agent._vcenter_scoped[0]
    assert scoped["permissions"] is None
    assert scoped["licenses"] == []
    assert scoped["alarms"] == []
    assert scoped["sessions"] == []

    # The failure must now be visible on the run — this is the fix.
    assert any(
        "permissions" in e and "vc1.example.com" in e and "permission denied" in e for e in errors
    ), f"expected a permissions fetch failure in errors, got: {errors}"


def test_phase_f_multiple_scoped_fetch_failures_all_surface():
    """All 4 scoped kinds failing independently must each contribute their
    own error — mirrors the per-kind try/except (one privilege gap must not
    hide another)."""
    agent, settings = _make_agent(vsphere_host="vc1.example.com")
    settings.vsphere_hosts = ""
    settings.vsphere_collect_perf_history = False

    mock_si = MagicMock()
    mock_si.RetrieveContent.return_value = MagicMock()

    with (
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere._connect", return_value=mock_si),
        patch("infra_brain.tools.vsphere.Disconnect"),
        patch("infra_brain.tools.vsphere.collect_vms", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_esxi_hosts", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datastores", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_clusters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datacenters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_networks", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_resource_pools", return_value=[]),
        patch(
            "infra_brain.tools.vsphere.collect_licenses", side_effect=Exception("licenses boom")
        ),
        patch(
            "infra_brain.tools.vsphere.collect_triggered_alarms",
            side_effect=Exception("alarms boom"),
        ),
        patch(
            "infra_brain.tools.vsphere.collect_permissions",
            side_effect=Exception("permissions boom"),
        ),
        patch(
            "infra_brain.tools.vsphere.collect_sessions", side_effect=Exception("sessions boom")
        ),
    ):
        errors: list[str] = []
        agent._collect_vcenter("vc1.example.com", settings, scope="all", errors=errors)

    scoped = agent._vcenter_scoped[0]
    assert scoped == {
        "vcenter": "vc1.example.com",
        "licenses": None,
        "alarms": None,
        "permissions": None,
        "sessions": None,
    }
    for kind in ("licenses", "alarms", "permissions", "sessions"):
        assert any(kind in e and f"{kind} boom" in e for e in errors), (
            f"missing surfaced error for {kind}: {errors}"
        )
    assert len(errors) == 4


def test_phase_f_failure_downgrades_run_to_partial_via_collect():
    """End-to-end through collect(): a Phase-F scoped fetch failure must
    downgrade the whole run's CollectOutcome (status computed by run() from
    .errors) to 'partial' — never a silent 'completed' that hides a
    permanent privilege gap from collection_runs."""
    agent, settings = _make_agent(vsphere_host="vc1.example.com")
    settings.vsphere_hosts = ""
    settings.vsphere_collect_perf_history = False

    mock_si = MagicMock()
    mock_si.RetrieveContent.return_value = MagicMock()

    with (
        patch("infra_brain.agents.vsphere.get_settings", return_value=settings),
        patch("infra_brain.tools.vsphere._PYVMOMI_AVAILABLE", True),
        patch("infra_brain.tools.vsphere._connect", return_value=mock_si),
        patch("infra_brain.tools.vsphere.Disconnect"),
        patch("infra_brain.tools.vsphere.collect_vms", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_esxi_hosts", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datastores", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_clusters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_datacenters", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_networks", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_resource_pools", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_licenses", return_value=[]),
        patch("infra_brain.tools.vsphere.collect_triggered_alarms", return_value=[]),
        patch(
            "infra_brain.tools.vsphere.collect_permissions",
            side_effect=Exception("permission denied"),
        ),
        patch("infra_brain.tools.vsphere.collect_sessions", return_value=[]),
    ):
        outcome = agent.collect()

    assert outcome.errors, "the Phase-F failure must reach CollectOutcome.errors"
    assert any("permissions" in e for e in outcome.errors)
    # R3 mapping: errors + a real vsphere_vcenter self-node item -> "partial".
    assert outcome.status == "partial"
