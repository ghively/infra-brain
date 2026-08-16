"""Stage-2 relational-model tests for VsphereAgent.

These exercise the detail-write phase added in stage 2:
  * scope="all" UPSERTs the 7 inventory tables keyed on (vcenter, moref),
    re-running produces no duplicates, resolved fields land in typed columns,
    leftovers land in ``details``, and ``resource_id`` links the canonical Resource.
  * scope="pulse" APPENDs vsphere_vm_metrics / vsphere_host_metrics (two pulses
    => two rows per entity).
  * perf-history fields on VM items append a vsphere_vm_metrics row with source="perf".
  * idle: empty VSPHERE_HOST/HOSTS => zero writes and NO vCenter connection.
  * retention prune deletes rows older than the window.
  * a simulated detail-write failure is SURFACED on the CollectionRun.

All tests run against in-memory SQLite via the shared ``session_patcher`` /
``sqlite_engine`` fixtures (tests/agents/conftest.py). They mock the vCenter tool
outputs entirely, so no pyVmomi / live vCenter is required.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.vsphere import VsphereAgent
from infra_brain.db.models import (
    CollectionRun,
    Resource,
    VsphereCluster,
    VsphereDatacenter,
    VsphereDatastore,
    VsphereHost,
    VsphereHostMetric,
    VsphereNetwork,
    VsphereResourcePool,
    VsphereVm,
    VsphereVmMetric,
)

MODULE = "infra_brain.agents.vsphere"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _settings(host="vc1.example.com", hosts="", retention_days=30):
    s = MagicMock()
    s.vsphere_host = host
    s.vsphere_hosts = hosts
    s.vsphere_metrics_retention_days = retention_days
    return s


def _agent(settings=None):
    agent = VsphereAgent.__new__(VsphereAgent)
    agent.callbacks = []
    agent.settings = settings or _settings()
    return agent


def _vm_item(name="vm-01", moref="vm-1001", vcenter="vc1.example.com", **extra):
    data = {
        "vcenter": vcenter,
        "moref": moref,
        "uuid": "uuid-1",
        "num_cpu": 4,
        "memory_mb": 8192,
        "power_state": "poweredOn",
        "esxi_host": "esxi-01",
        "ip_address": "10.0.0.1",
        "all_ips": ["10.0.0.1"],
        "datastore_names": ["ds1"],
        "network_names": ["VM Network"],
        "snapshot_count": 1,
        "overall_status": "green",
        # an unmapped extra that must land in details JSONB
        "max_cpu_usage": 8000,
    }
    data.update(extra)
    return {"name": name, "type": "vsphere_vm", "data": data}


def _host_item(name="esxi-01", moref="host-2001", vcenter="vc1.example.com", **extra):
    data = {
        "vcenter": vcenter,
        "moref": moref,
        "memory_gb": 128.0,
        "version": "7.0.3",
        "vm_count": 12,
        "connection_state": "connected",
        "cluster_or_parent": "cluster-prod",
        "overall_status": "green",
        "standby_mode": "none",  # unmapped -> details
    }
    data.update(extra)
    return {"name": name, "type": "vsphere_host", "data": data}


def _all_items():
    """One of each inventory type with a distinct moref."""
    return [
        _vm_item(),
        {
            "name": "tpl-ubuntu",
            "type": "vsphere_template",
            "data": {"vcenter": "vc1.example.com", "moref": "vm-9001", "num_cpu": 2},
        },
        _host_item(),
        {
            "name": "ds-prod",
            "type": "vsphere_datastore",
            "data": {
                "vcenter": "vc1.example.com",
                "moref": "datastore-3001",
                "capacity_gb": 1000.0,
                "free_gb": 400.0,
                "used_pct": 60.0,
                "maintenance_mode": "normal",
                "datastore_type": "VMFS",
            },
        },
        {
            "name": "cluster-prod",
            "type": "vsphere_cluster",
            "data": {
                "vcenter": "vc1.example.com",
                "moref": "domain-c4001",
                "ha_enabled": True,
                "drs_enabled": True,
                "num_hosts": 4,
                "host_names": ["esxi-01"],
                "datastore_names": ["ds-prod"],
            },
        },
        {
            "name": "dc-prod",
            "type": "vsphere_datacenter",
            "data": {
                "vcenter": "vc1.example.com",
                "moref": "datacenter-5001",
                "overall_status": "green",
                "parent": "root",
            },
        },
        {
            "name": "VM Network",
            "type": "vsphere_portgroup",
            "data": {
                "vcenter": "vc1.example.com",
                "moref": "network-6001",
                "accessible": True,
                "vm_count": 3,
            },
        },
        {
            "name": "dvs-prod",
            "type": "vsphere_dvswitch",
            "data": {
                "vcenter": "vc1.example.com",
                "moref": "dvs-6002",
                "num_ports": 128,
                "uuid": "dvs-uuid",
                "version": "7.0",
            },
        },
        {
            "name": "rp-prod",
            "type": "vsphere_resource_pool",
            "data": {
                "vcenter": "vc1.example.com",
                "moref": "resgroup-7001",
                "cpu_limit": -1,
                "memory_usage_mb": 2048,
                "vm_count": 5,
            },
        },
    ]


@pytest.fixture
def patched(session_patcher):
    """Patch get_session in the vsphere agent module; yields the engine."""
    with session_patcher(MODULE) as engine:
        yield engine


def _seed_resources(engine, items):
    """Create the canonical Resource rows the base run() would have upserted.

    Base run() upserts vSphere resources with a vCenter-qualified name (S-9), so
    the seeded rows mirror that — otherwise ``_resource_id`` (which qualifies its
    lookup) would fail to link them.
    """
    with Session(engine) as s:
        for it in items:
            vcenter = (it.get("data") or {}).get("vcenter") or ""
            name = f"{it['name']} ({vcenter})" if vcenter else it["name"]
            s.add(
                Resource(
                    id=uuid.uuid4(),
                    domain="vsphere",
                    type=it["type"],
                    name=name,
                    source="VsphereAgent",
                    zone="corpor",
                )
            )
        s.commit()


# ---------------------------------------------------------------------------
# scope="all": inventory upsert
# ---------------------------------------------------------------------------


def test_all_scope_upserts_each_inventory_table(patched):
    engine = patched
    items = _all_items()
    _seed_resources(engine, items)

    agent = _agent()
    agent._last_items = items
    agent._write_vsphere_details("all")

    with Session(engine) as s:
        assert s.query(VsphereVm).count() == 2  # vm + template
        assert s.query(VsphereHost).count() == 1
        assert s.query(VsphereDatastore).count() == 1
        assert s.query(VsphereCluster).count() == 1
        assert s.query(VsphereDatacenter).count() == 1
        assert s.query(VsphereNetwork).count() == 2  # portgroup + dvswitch
        assert s.query(VsphereResourcePool).count() == 1

        vm = s.query(VsphereVm).filter_by(moref="vm-1001").one()
        assert vm.name == "vm-01"
        assert vm.num_cpu == 4
        assert vm.is_template is False
        assert vm.resource_id is not None  # linked to Resource
        # unmapped field landed in details JSONB
        assert vm.details and vm.details.get("max_cpu_usage") == 8000

        tpl = s.query(VsphereVm).filter_by(moref="vm-9001").one()
        assert tpl.is_template is True

        net_kinds = {n.network_kind for n in s.query(VsphereNetwork).all()}
        assert net_kinds == {"portgroup", "dvswitch"}

        ds = s.query(VsphereDatastore).one()
        assert ds.maintenance_mode is False  # "normal" -> False


def test_all_scope_is_idempotent(patched):
    """Re-running the same inventory upsert produces no duplicate rows."""
    engine = patched
    items = _all_items()
    _seed_resources(engine, items)

    agent = _agent()
    agent._last_items = items
    agent._write_vsphere_details("all")
    agent._write_vsphere_details("all")  # second pass — same morefs

    with Session(engine) as s:
        assert s.query(VsphereVm).count() == 2
        assert s.query(VsphereHost).count() == 1
        assert s.query(VsphereDatastore).count() == 1
        assert s.query(VsphereCluster).count() == 1
        assert s.query(VsphereNetwork).count() == 2
        assert s.query(VsphereResourcePool).count() == 1


def test_all_scope_updates_in_place_on_change(patched):
    engine = patched
    items = [_host_item(vm_count=12)]
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_vsphere_details("all")

    # Same moref, changed value
    agent._last_items = [_host_item(vm_count=99)]
    agent._write_vsphere_details("all")

    with Session(engine) as s:
        rows = s.query(VsphereHost).all()
        assert len(rows) == 1
        assert rows[0].vm_count == 99


def test_bad_item_skips_without_aborting_rest(patched):
    """An item missing its moref logs+skips; the good items still persist."""
    engine = patched
    good = _vm_item(name="good-vm", moref="vm-1")
    bad = {
        "name": "bad-vm",
        "type": "vsphere_vm",
        "data": {"vcenter": "vc1.example.com"},
    }  # no moref
    items = [bad, good]
    _seed_resources(engine, items)

    agent = _agent()
    agent._last_items = items
    agent._write_vsphere_details("all")

    with Session(engine) as s:
        rows = s.query(VsphereVm).all()
        assert len(rows) == 1
        assert rows[0].name == "good-vm"


# ---------------------------------------------------------------------------
# perf-history -> vsphere_vm_metrics source="perf"
# ---------------------------------------------------------------------------


def test_perf_fields_append_perf_metric_row(patched):
    engine = patched
    items = [_vm_item(perf_cpu_usage_pct_avg=42.5, perf_samples=12, perf_interval_id=300)]
    _seed_resources(engine, items)

    agent = _agent()
    agent._last_items = items
    agent._write_vsphere_details("all")

    with Session(engine) as s:
        # inventory row still upserted
        assert s.query(VsphereVm).count() == 1
        perf_rows = s.query(VsphereVmMetric).filter_by(source="perf").all()
        assert len(perf_rows) == 1
        assert perf_rows[0].perf_cpu_usage_pct_avg == 42.5
        assert perf_rows[0].perf_samples == 12


def test_no_perf_metric_when_no_perf_fields(patched):
    engine = patched
    items = [_vm_item()]
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_vsphere_details("all")
    with Session(engine) as s:
        assert s.query(VsphereVmMetric).count() == 0


# ---------------------------------------------------------------------------
# scope="pulse": append-only metrics
# ---------------------------------------------------------------------------


def _pulse_items():
    return [
        {
            "name": "vm-01",
            "type": "vsphere_vm",
            "data": {
                "vcenter": "vc1.example.com",
                "moref": "vm-1001",
                "pulse": True,
                "power_state": "poweredOn",
                "cpu_usage_mhz": 800,
                "memory_usage_mb": 4096,
                "overall_status": "green",
            },
        },
        {
            "name": "esxi-01",
            "type": "vsphere_host",
            "data": {
                "vcenter": "vc1.example.com",
                "moref": "host-2001",
                "pulse": True,
                "connection_state": "connected",
                "cpu_usage_mhz": 3000,
                "overall_status": "green",
            },
        },
    ]


def test_pulse_appends_metrics_rows(patched):
    engine = patched
    agent = _agent()
    agent._last_items = _pulse_items()
    agent._write_vsphere_details("pulse")

    with Session(engine) as s:
        vm_rows = s.query(VsphereVmMetric).all()
        host_rows = s.query(VsphereHostMetric).all()
        assert len(vm_rows) == 1
        assert len(host_rows) == 1
        assert vm_rows[0].source == "pulse"
        assert vm_rows[0].cpu_usage_mhz == 800
        assert host_rows[0].source == "pulse"
        assert host_rows[0].connection_state == "connected"
        # pulse must NOT write inventory rows
        assert s.query(VsphereVm).count() == 0
        assert s.query(VsphereHost).count() == 0


def test_two_pulses_append_two_rows_per_entity(patched):
    engine = patched
    agent = _agent()
    agent._last_items = _pulse_items()
    agent._write_vsphere_details("pulse")
    agent._last_items = _pulse_items()
    agent._write_vsphere_details("pulse")

    with Session(engine) as s:
        assert s.query(VsphereVmMetric).filter_by(moref="vm-1001").count() == 2
        assert s.query(VsphereHostMetric).filter_by(moref="host-2001").count() == 2


# ---------------------------------------------------------------------------
# idle-safety: host unset => zero writes, no connection
# ---------------------------------------------------------------------------


def test_idle_no_writes_when_host_unset(patched):
    engine = patched
    agent = _agent(settings=_settings(host="", hosts=""))
    # Even if items somehow lingered, the idle guard must short-circuit first.
    agent._last_items = _all_items()
    agent._write_vsphere_details("all")

    with Session(engine) as s:
        assert s.query(VsphereVm).count() == 0
        assert s.query(VsphereHost).count() == 0
        assert s.query(VsphereVmMetric).count() == 0


def test_idle_collect_never_connects(monkeypatch):
    """collect() with no host raises CollectorSkipped and never opens a
    vCenter connection."""
    agent = _agent(settings=_settings(host="", hosts=""))

    import infra_brain.tools.vsphere as toolmod
    from infra_brain.agents.base import CollectorSkipped

    called = {"connect": False}

    def _boom(*a, **k):
        called["connect"] = True
        raise AssertionError("must not connect when host unset")

    monkeypatch.setattr(toolmod, "_connect", _boom)
    with (
        patch(f"{MODULE}.get_settings", return_value=agent.settings),
        pytest.raises(CollectorSkipped),
    ):
        agent.collect("all")
    assert called["connect"] is False


# ---------------------------------------------------------------------------
# retention prune
# ---------------------------------------------------------------------------


def test_retention_prunes_old_metric_rows(patched):
    engine = patched
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    recent = now - timedelta(days=1)
    with Session(engine) as s:
        s.add(
            VsphereVmMetric(
                vcenter="vc1", moref="vm-1", name="old", collected_at=old, source="pulse"
            )
        )
        s.add(
            VsphereVmMetric(
                vcenter="vc1", moref="vm-1", name="new", collected_at=recent, source="pulse"
            )
        )
        s.add(
            VsphereHostMetric(
                vcenter="vc1", moref="h-1", name="old", collected_at=old, source="pulse"
            )
        )
        s.commit()

    agent = _agent(settings=_settings(retention_days=30))
    agent._prune_metrics(agent.settings)

    with Session(engine) as s:
        vm_rows = s.query(VsphereVmMetric).all()
        assert len(vm_rows) == 1
        assert vm_rows[0].name == "new"
        assert s.query(VsphereHostMetric).count() == 0


def test_retention_disabled_when_days_nonpositive(patched):
    engine = patched
    old = datetime.now(UTC) - timedelta(days=400)
    with Session(engine) as s:
        s.add(
            VsphereVmMetric(
                vcenter="vc1", moref="vm-1", name="old", collected_at=old, source="pulse"
            )
        )
        s.commit()
    agent = _agent(settings=_settings(retention_days=0))
    agent._prune_metrics(agent.settings)
    with Session(engine) as s:
        assert s.query(VsphereVmMetric).count() == 1  # untouched


# ---------------------------------------------------------------------------
# GitLab #148: _write_vsphere_details must RETURN the row count so
# ETLConnector._write_details can persist CollectionRun.detail_rows_written.
# It used to return None, leaving the counter at 0 on every run even though
# the relational tables were being populated.
# ---------------------------------------------------------------------------


def test_all_scope_returns_detail_row_count(patched):
    engine = patched
    items = _all_items()
    _seed_resources(engine, items)

    agent = _agent()
    agent._last_items = items
    count = agent._write_vsphere_details("all")

    # Every item in _all_items maps to a relational inventory table.
    assert count == len(items)


def test_pulse_returns_detail_row_count(patched):
    agent = _agent()
    agent._last_items = _pulse_items()
    assert agent._write_vsphere_details("pulse") == 2  # 1 VM + 1 host metric


def test_idle_returns_zero_detail_rows(patched):
    agent = _agent(settings=_settings(host="", hosts=""))
    agent._last_items = _all_items()
    assert agent._write_vsphere_details("all") == 0


def test_detail_count_persisted_on_collection_run(sqlite_engine):
    """End-to-end through _write_details: the returned count must land on
    CollectionRun.detail_rows_written (GitLab #148)."""
    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="vsphere",
                trigger_type="scheduled",
                trigger_source="all",
                status="completed",
            )
        )
        s.commit()

    from infra_brain.agents.base import CollectionResult

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _agent()
    result = CollectionResult(
        run_id=run_id, domain="vsphere", resources_found=9, drift_count=0, status="completed"
    )

    with patch("infra_brain.etl.base.get_session", _get_session):
        agent._write_details(result, lambda: 9)

    assert result.detail_rows_written == 9
    with Session(engine) as s:
        run = s.get(CollectionRun, run_id)
        assert run.detail_rows_written == 9


# ---------------------------------------------------------------------------
# _write_details surfaces a detail-write failure on the CollectionRun
# ---------------------------------------------------------------------------


def test_detail_write_failure_marks_run_failed(sqlite_engine):
    """A structural failure in the detail-write phase must flip the run to failed.

    ``_write_details`` lives on BaseAgent and opens its own get_session to record
    the failure on the CollectionRun, so we patch get_session in the base module.
    """
    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="vsphere",
                trigger_type="scheduled",
                trigger_source="all",
                status="completed",
            )
        )
        s.commit()

    from infra_brain.agents.base import CollectionResult

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _agent()
    result = CollectionResult(
        run_id=run_id, domain="vsphere", resources_found=1, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("simulated relational write failure")

    with patch("infra_brain.etl.base.get_session", _get_session):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("simulated relational write failure" in e for e in result.errors)
    with Session(engine) as s:
        run = s.get(CollectionRun, run_id)
        assert run.status == "failed"
        assert "simulated relational write failure" in (run.error_message or "")


# ---------------------------------------------------------------------------
# S-9: vCenter-scoped Resource identity
# ---------------------------------------------------------------------------


def test_qualified_name_scopes_by_vcenter():
    agent = _agent()
    # No moref -> no collision check, session is never touched.
    assert agent._qualified_name(None, "web01", "vc1") == "web01 (vc1)"
    # No vCenter → name unchanged (legacy / unset).
    assert agent._qualified_name(None, "web01", "") == "web01"


def test_resource_id_distinguishes_same_name_across_vcenters(patched):
    """Two vCenters with an identically-named cluster resolve to DISTINCT rows."""
    engine = patched
    with Session(engine) as s:
        for vc in ("vcA.example.com", "vcB.example.com"):
            s.add(
                Resource(
                    id=uuid.uuid4(),
                    domain="vsphere",
                    type="vsphere_cluster",
                    name=f"Production ({vc})",
                    source="VsphereAgent",
                    zone="corpor",
                )
            )
        s.commit()

    agent = _agent()
    with Session(engine) as s:
        # Task 4: _resource_id now lives on ETLConnector and takes a `qualify`
        # keyword hook instead of a positional vcenter arg.
        rid_a = agent._resource_id(
            s,
            "vsphere_cluster",
            "Production",
            qualify=lambda n: agent._qualified_name(s, n, "vcA.example.com"),
        )
        rid_b = agent._resource_id(
            s,
            "vsphere_cluster",
            "Production",
            qualify=lambda n: agent._qualified_name(s, n, "vcB.example.com"),
        )
        assert rid_a is not None and rid_b is not None
        assert rid_a != rid_b  # no collapse across vCenters
        # A bare (unqualified) lookup finds neither — proves qualification is required.
        assert agent._resource_id(s, "vsphere_cluster", "Production") is None
