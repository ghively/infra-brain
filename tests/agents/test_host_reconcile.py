"""Tests for HostReconcileAgent — cross-source canonical host identity reconciliation."""

from contextlib import contextmanager
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.host_reconcile import HostReconcileAgent
from infra_brain.db.models import CollectionRun, HostIdentity

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


def _inventory_group_id(session):
    """Create a REAL ansible_inventory_groups row and return its id.

    ``AnsibleInventoryHost.group_id`` is a genuine FK (as is the group's own
    ``iac_file_id``). SQLite does not enforce either, so these tests seeded a
    bare ``uuid4()``; PostgreSQL rejects it. Caught by the agent-orm-check
    gate (TRK-356).
    """
    import uuid as _u

    from infra_brain.db.models import AnsibleInventoryGroup, IacFile

    iac_file = IacFile(
        id=_u.uuid4(),
        gitlab_project_id=1,
        path="inventories/hosts.yml",
        file_type="inventory",
        ref="main",
    )
    session.add(iac_file)
    session.flush()
    group = AnsibleInventoryGroup(id=_u.uuid4(), iac_file_id=iac_file.id, name="all")
    session.add(group)
    session.flush()
    return group.id


def _make_agent():
    agent = HostReconcileAgent.__new__(HostReconcileAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    return agent


def _session_ctx(engine):
    """Return a zero-argument callable that yields a Session on the given engine."""

    @contextmanager
    def _get():
        with Session(engine) as s:
            yield s

    return _get


def test_host_reconcile_domain():
    agent = _make_agent()
    assert agent.domain == "host_reconcile"


def test_collect_returns_empty_list():
    """collect() is a no-op stub — HostReconcileAgent reads existing tables directly."""
    agent = _make_agent()
    assert agent.collect(scope="all") == []


def test_run_creates_collection_run_record(engine):
    """run() must open a CollectionRun row and mark it completed on success."""
    get_session = _session_ctx(engine)

    with (
        patch("infra_brain.agents.host_reconcile.get_session", get_session),
        patch("infra_brain.etl.base.get_session", get_session),
        patch.object(HostReconcileAgent, "_build_merged_hosts", return_value=({}, [], [])),
        patch.object(HostReconcileAgent, "_upsert_identities", return_value=(0, 0)),
        patch.object(HostReconcileAgent, "_emit_ip_conflict_events", return_value=None),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = HostReconcileAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "completed"
    assert result.domain == "host_reconcile"
    assert result.resources_found == 0

    with Session(engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.finished_at is not None


def test_run_populates_detail_rows_written(engine):
    """GitLab #148/#149: the HostIdentity upsert count is the run's detail-row
    count — host_identities is the queryable table behind resources_found.
    Before this fix detail_rows_written stayed 0 on every run, making the
    2,535-per-run count a provenance dead-end (no counter traced it to a
    real table)."""
    get_session = _session_ctx(engine)

    with (
        patch("infra_brain.agents.host_reconcile.get_session", get_session),
        patch("infra_brain.etl.base.get_session", get_session),
        patch.object(HostReconcileAgent, "_build_merged_hosts", return_value=({}, [], [])),
        patch.object(HostReconcileAgent, "_upsert_identities", return_value=(2, 1)),
        patch.object(HostReconcileAgent, "_emit_ip_conflict_events", return_value=None),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = HostReconcileAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "completed"
    assert result.resources_found == 3
    assert result.detail_rows_written == 3

    with Session(engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run is not None
        assert run.detail_rows_written == 3


def test_run_marks_collection_run_failed_on_exception(engine):
    """run() must record status='failed' on the CollectionRun when an exception occurs."""
    get_session = _session_ctx(engine)

    with (
        patch("infra_brain.agents.host_reconcile.get_session", get_session),
        patch("infra_brain.etl.base.get_session", get_session),
        patch.object(
            HostReconcileAgent,
            "_build_merged_hosts",
            side_effect=RuntimeError("db timeout"),
        ),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = HostReconcileAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "failed"
    assert any("db timeout" in e for e in result.errors)

    with Session(engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_message is not None


def test_run_sets_drift_count_from_ip_conflict_events(engine):
    """AA-C-2: drift_count must reflect real DriftEvent rows, not a hardcoded 0.

    _emit_ip_conflict_events must also stamp collection_run_id on the DriftEvent
    it writes, since count_drift_events_for_run() filters by that column.
    """
    from infra_brain.db.models import Resource

    get_session = _session_ctx(engine)

    with Session(engine) as s:
        resource = Resource(domain="vsphere", name="web-01", type="vm", source="vsphere")
        s.add(resource)
        s.commit()
        resource_id = resource.id

    ip_conflicts = [
        {
            "short_hostname": "web-01",
            "resource_id": resource_id,
            "existing_ip": "10.1.2.3",
            "new_ip": "10.1.2.4",
            "existing_source": "r7",
            "new_source": "vsphere",
        }
    ]

    with (
        patch("infra_brain.agents.host_reconcile.get_session", get_session),
        patch("infra_brain.etl.base.get_session", get_session),
        patch.object(
            HostReconcileAgent, "_build_merged_hosts", return_value=({}, ip_conflicts, [])
        ),
        patch.object(HostReconcileAgent, "_upsert_identities", return_value=(0, 0)),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = HostReconcileAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "completed"
    assert result.drift_count == 1, "drift_count must count the real IP-conflict DriftEvent"

    with Session(engine) as s:
        from infra_brain.db.models import DriftEvent

        event = s.query(DriftEvent).filter_by(drift_type="identity_conflict").one()
        assert event.collection_run_id == result.run_id


def test_ip_conflict_update_path_preserves_source(engine):
    """TRK-269 (GitLab #152): when a pre-existing open identity_conflict/ip_address
    DriftEvent is refreshed (not first-created), new_value must still include the
    'source' key alongside 'ip' -- matching the first-conflict creation path below.
    Previously the update branch wrote new_value={"ip": ...} only, silently
    dropping provenance on every subsequent refresh of an already-open conflict.
    """
    from infra_brain.db.models import DriftEvent, Resource

    get_session = _session_ctx(engine)

    with Session(engine) as s:
        resource = Resource(domain="vsphere", name="web-02", type="vm", source="vsphere")
        s.add(resource)
        s.commit()
        resource_id = resource.id
        s.add(
            DriftEvent(
                resource_id=resource_id,
                drift_type="identity_conflict",
                field="ip_address",
                old_value={"ip": "10.1.2.3", "source": "r7"},
                new_value={"ip": "10.1.2.4", "source": "vsphere"},
                status="open",
            )
        )
        s.commit()

    conflict = {
        "short_hostname": "web-02",
        "resource_id": resource_id,
        "existing_ip": "10.1.2.3",
        "new_ip": "10.1.2.5",
        "existing_source": "r7",
        "new_source": "netdisco",
    }

    with get_session() as s:
        agent = _make_agent()
        agent._upsert_ip_conflict_event(s, conflict, run_id=None)
        s.commit()

    with Session(engine) as s:
        event = s.query(DriftEvent).filter_by(drift_type="identity_conflict").one()
        assert event.new_value == {"ip": "10.1.2.5", "source": "netdisco"}, (
            "update path must carry 'source' through, same as the first-conflict creation path"
        )


def test_unchanged_ip_conflict_bumps_last_seen_only(engine):
    """GitLab #163 defect 1, ip_address twin: _upsert_ip_conflict_event must not
    reset detected_at on every sweep for an unchanged, already-open conflict —
    mirrors test_unchanged_identity_conflict_bumps_last_seen_only for
    _upsert_identity_conflict_event. Re-emitting the SAME conflict must leave
    detected_at and collection_run_id untouched and only advance last_seen_at.

    Regression test for the bug where the refresh branch stamped
    ``detected_at = now`` unconditionally on every 30-minute sweep, so an IP
    conflict open for months always looked freshly detected to every
    age/staleness consumer.
    """
    import uuid as _uuid

    from infra_brain.db.models import DriftEvent, Resource

    get_session = _session_ctx(engine)
    first_run = _uuid.uuid4()
    second_run = _uuid.uuid4()

    with Session(engine) as s:
        resource = Resource(domain="vsphere", name="web-03", type="vm", source="vsphere")
        s.add(resource)
        for rid in (first_run, second_run):
            s.add(CollectionRun(id=rid, domain="host_reconcile", trigger_type="scheduled"))
        s.commit()
        resource_id = resource.id

    conflict = {
        "short_hostname": "web-03",
        "resource_id": resource_id,
        "existing_ip": "10.1.2.3",
        "new_ip": "10.1.2.5",
        "existing_source": "r7",
        "new_source": "netdisco",
    }
    agent = _make_agent()

    with get_session() as s:
        agent._upsert_ip_conflict_event(s, conflict, run_id=first_run)
        s.commit()

    with Session(engine) as s:
        row = s.query(DriftEvent).one()
        detected_before = row.detected_at
        last_seen_before = row.last_seen_at
        assert last_seen_before is not None, "a new row must stamp last_seen_at too"

    # Same conflict, next sweep — nothing about the finding changed.
    with get_session() as s:
        agent._upsert_ip_conflict_event(s, dict(conflict), run_id=second_run)
        s.commit()

    with Session(engine) as s:
        rows = s.query(DriftEvent).all()
        assert len(rows) == 1, "an unchanged re-observation must refresh, not duplicate"
        row = rows[0]
        assert row.detected_at == detected_before, "detected_at must stay at FIRST observation"
        assert row.collection_run_id == first_run, "collection_run_id must not advance either"
        assert row.last_seen_at > last_seen_before, "last_seen_at must advance on re-observation"


def test_changed_ip_conflict_advances_detected_at_and_run_id(engine):
    """GitLab #163 defect 1, ip_address twin: when the finding's DATA genuinely
    changed (a different observed IP), BOTH detected_at and collection_run_id
    advance — that is a new observation, not a re-observation of the old one."""
    import uuid as _uuid

    from infra_brain.db.models import DriftEvent, Resource

    get_session = _session_ctx(engine)
    first_run = _uuid.uuid4()
    second_run = _uuid.uuid4()

    with Session(engine) as s:
        resource = Resource(domain="vsphere", name="web-03", type="vm", source="vsphere")
        s.add(resource)
        for rid in (first_run, second_run):
            s.add(CollectionRun(id=rid, domain="host_reconcile", trigger_type="scheduled"))
        s.commit()
        resource_id = resource.id

    agent = _make_agent()
    v1 = {
        "short_hostname": "web-03",
        "resource_id": resource_id,
        "existing_ip": "10.1.2.3",
        "new_ip": "10.1.2.5",
        "existing_source": "r7",
        "new_source": "netdisco",
    }
    v2 = {**v1, "new_ip": "10.1.2.9"}

    with get_session() as s:
        agent._upsert_ip_conflict_event(s, v1, run_id=first_run)
        s.commit()
    with Session(engine) as s:
        detected_before = s.query(DriftEvent).one().detected_at

    with get_session() as s:
        agent._upsert_ip_conflict_event(s, v2, run_id=second_run)
        s.commit()

    with Session(engine) as s:
        rows = s.query(DriftEvent).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.detected_at >= detected_before
        assert row.collection_run_id == second_run, "a changed finding must re-anchor to this run"
        assert row.new_value == {"ip": "10.1.2.9", "source": "netdisco"}
        assert row.last_seen_at is not None


def test_run_honours_collection_disabled_domains(engine):
    """AA-R-11/12: a run()-override collector must still respect the
    collection_disabled_domains maintenance-pause knob like ETLConnector.run()."""
    get_session = _session_ctx(engine)

    with (
        patch("infra_brain.agents.host_reconcile.get_session", get_session),
        patch("infra_brain.etl.base.get_session", get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = HostReconcileAgent()
        agent.settings.collection_disabled_domains = "host_reconcile,other"
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "skipped"

    with Session(engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run.status == "skipped"
        assert "collection_disabled_domains" in (run.error_message or "")


def test_upsert_identities_creates_new_rows(engine):
    """_upsert_identities inserts new HostIdentity rows when none exist."""
    get_session = _session_ctx(engine)

    merged = {
        "web-01": {
            "short_hostname": "web-01",
            "fqdn": "web-01.corp.local",
            "ip_addresses": ["10.1.2.3"],
            "os_family": "linux",
            "risk_score": 250,
            "vuln_count": 5,
        }
    }

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        n_new, n_updated = agent._upsert_identities(merged)

    assert n_new == 1
    assert n_updated == 0

    with Session(engine) as s:
        row = s.query(HostIdentity).filter_by(short_hostname="web-01").one()
        assert row.fqdn == "web-01.corp.local"
        assert row.os_family == "linux"
        assert row.risk_score == 250


def test_upsert_identities_updates_existing_rows(engine):
    """_upsert_identities updates an existing HostIdentity row on second call."""
    get_session = _session_ctx(engine)

    merged_v1 = {
        "db-01": {
            "short_hostname": "db-01",
            "fqdn": "db-01.corp.local",
            "ip_addresses": ["10.1.2.4"],
        }
    }
    merged_v2 = {
        "db-01": {
            "short_hostname": "db-01",
            "fqdn": "db-01.corp.local",
            "ip_addresses": ["10.1.2.4"],
            "risk_score": 500,
        }
    }

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        n_new1, n_up1 = agent._upsert_identities(merged_v1)
        n_new2, n_up2 = agent._upsert_identities(merged_v2)

    assert n_new1 == 1 and n_up1 == 0
    assert n_new2 == 0 and n_up2 == 1

    with Session(engine) as s:
        rows = s.query(HostIdentity).filter_by(short_hostname="db-01").all()
        assert len(rows) == 1
        assert rows[0].risk_score == 500


def test_stale_vsphere_leg_clears_when_vm_disappears_from_the_merge(engine):
    """KG-8: HostIdentity.vsphere_resource_id must clear on the next
    reconcile once the vSphere VM genuinely disappears (deleted/decommission
    -ed), not keep pointing at the resource forever. The host stays present
    in the merge via its still-live Rapid7 leg, so `_upsert_identity_item`'s
    update path runs and must reflect the current merge truthfully."""
    from infra_brain.db.models import Resource, VsphereVm

    get_session = _session_ctx(engine)
    with Session(engine) as s:
        r7_rid = _seed_r7(s, hostname="dual-01", ip="10.0.0.5", r7_asset_id=1)
        vm_res = Resource(domain="vsphere", name="dual-01", type="vm", source="vsphere")
        s.add(vm_res)
        s.flush()
        s.add(
            VsphereVm(
                vcenter="vc1",
                moref="vm-1",
                name="dual-01",
                guest_hostname="dual-01",
                resource_id=vm_res.id,
            )
        )
        s.commit()
        vsphere_rid = vm_res.id

    agent = _make_agent()
    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        merged1, _c1, _ic1 = agent._build_merged_hosts()
        agent._upsert_identities(merged1)

    with Session(engine) as s:
        row = s.query(HostIdentity).filter_by(short_hostname="dual-01").one()
        assert row.r7_resource_id == r7_rid
        assert row.vsphere_resource_id == vsphere_rid

    # The vSphere VM is gone; Rapid7 still reports the same host, so
    # "dual-01" stays in the merge and its row IS revisited.
    with Session(engine) as s:
        s.query(VsphereVm).filter_by(moref="vm-1").delete()
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        merged2, _c2, _ic2 = agent._build_merged_hosts()
        assert "vsphere_resource_id" not in merged2["dual-01"]
        agent._upsert_identities(merged2)

    with Session(engine) as s:
        row = s.query(HostIdentity).filter_by(short_hostname="dual-01").one()
        assert row.r7_resource_id == r7_rid, "the unrelated, still-live leg must be untouched"
        assert row.vsphere_resource_id is None, (
            "KG-8: a leg whose source genuinely disappeared from this run's "
            "merge must clear, not keep pointing at a stale resource_id forever"
        )


# ---------------------------------------------------------------------------
# KG-9 / TRK-102: guarded IP-based attach of bare-IP netdiscovery hosts
# ---------------------------------------------------------------------------


def _seed_r7(session, *, hostname, ip, r7_asset_id):
    """Create a Resource + R7Asset pair and return the R7Asset.resource_id."""
    from infra_brain.db.models import R7Asset, Resource

    res = Resource(domain="rapid7", name=hostname, type="asset", source="rapid7")
    session.add(res)
    session.flush()
    session.add(R7Asset(r7_asset_id=r7_asset_id, hostname=hostname, ip=ip, resource_id=res.id))
    session.flush()
    return res.id


def _seed_net(session, *, ip, hostname=None):
    """Create a Resource + NetDiscoveryHost pair and return the resource_id."""
    from infra_brain.db.models import NetDiscoveryHost, Resource

    res = Resource(domain="netdiscovery", name=ip, type="host", source="netdiscovery")
    session.add(res)
    session.flush()
    session.add(NetDiscoveryHost(ip=ip, hostname=hostname, resource_id=res.id))
    session.flush()
    return res.id


def _seed_netdevice(session, *, ip, name):
    """Create a Resource + NetDevice pair and return the resource_id."""
    from infra_brain.db.models import NetDevice, Resource

    res = Resource(domain="net", name=name, type="net_device", source="net")
    session.add(res)
    session.flush()
    session.add(NetDevice(ip=ip, name=name, resource_id=res.id))
    session.flush()
    return res.id


# ---------------------------------------------------------------------------
# Netdiscovery leg attachment (KG-9 / TRK-102)
#
# These used to assert the merge AND the IS_SAME_AS edge it produced. P5 deleted
# the emitters — identity is graph_phase3's alone — so what is asserted here is
# the half this agent still owns: which leg attaches to which merged host, and on
# what basis. The edge half moved to tests/test_p5_issameas_resolver_coverage.py
# and tests/test_multi_collector_convergence.py, against the resolver.
# ---------------------------------------------------------------------------


def test_bare_ip_netdiscovery_unique_match_attaches(engine):
    """(a) A hostname-less netdiscovery host whose IP uniquely matches one merged
    host attaches net_resource_id, tagged ``net_match_basis="ip"`` so the lower
    trust of an IP-derived attachment stays visible on the record."""
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        r7_rid = _seed_r7(s, hostname="web-01", ip="10.1.2.3", r7_asset_id=1)
        net_rid = _seed_net(s, ip="10.1.2.3", hostname=None)
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    assert merged["web-01"]["net_resource_id"] == net_rid
    assert merged["web-01"]["net_match_basis"] == "ip"
    assert merged["web-01"]["r7_resource_id"] == r7_rid


def test_bare_ip_netdiscovery_ambiguous_match_skipped(engine):
    """(b) A hostname-less netdiscovery host whose IP matches TWO merged hosts is
    ambiguous and must NOT be merged (TRK-087 false-merge guard)."""
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_r7(s, hostname="host-a", ip="10.9.9.9", r7_asset_id=10)
        _seed_r7(s, hostname="host-b", ip="10.9.9.9", r7_asset_id=11)
        _seed_net(s, ip="10.9.9.9", hostname=None)
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    assert merged["host-a"].get("net_resource_id") is None
    assert merged["host-b"].get("net_resource_id") is None
    assert "net_match_basis" not in merged["host-a"]
    assert "net_match_basis" not in merged["host-b"]


def test_hostname_netdiscovery_match_basis_hostname(engine):
    """(c) The hostname path still works and carries match_basis='hostname'."""
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_r7(s, hostname="web-01", ip="10.1.2.3", r7_asset_id=20)
        # netdiscovery host WITH a resolvable hostname → merges on the name key.
        _seed_net(s, ip="10.5.5.5", hostname="web-01")
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    assert merged["web-01"].get("net_resource_id") is not None
    assert merged["web-01"]["net_match_basis"] == "hostname"


def test_netdevice_source_populates_merged_host(engine):
    """(KG-4) NetDevice must merge into the host dict exactly like the other
    seven _SOURCE_KEYS entries."""
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_r7(s, hostname="switch-01", ip="10.1.1.1", r7_asset_id=40)
        _seed_netdevice(s, ip="10.1.1.1", name="switch-01")
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    assert merged["switch-01"].get("netdevice_resource_id") is not None


def test_netdevice_and_another_source_converge_on_one_record(engine):
    """(KG-4) NetDevice + another source sharing a hostname land on ONE merged
    record carrying both legs, exactly like every other source pair.

    Previously also asserted the resulting IS_SAME_AS edge. That edge no longer
    exists anywhere: the ``netdevice`` leg's collector (``agents/net.py``) is
    unregistered with zero rows ever, so P5 recorded a revival obligation on its
    spec instead of declaring a speculative host NodeSpec for it —
    tests/test_p5_issameas_resolver_coverage.py asserts that note exists.
    """
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_r7(s, hostname="switch-01", ip="10.1.1.1", r7_asset_id=41)
        _seed_netdevice(s, ip="10.1.1.1", name="switch-01")
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    rec = merged["switch-01"]
    assert rec.get("r7_resource_id") is not None
    assert rec.get("netdevice_resource_id") is not None


def test_bare_ip_netdiscovery_no_match_is_not_attached(engine):
    """(d) A hostname-less netdiscovery host with no IP match is not attached.

    Absence, not a guess: an unmatched bare-IP probe stays off every merged host
    rather than being attached to the nearest one.
    """
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_r7(s, hostname="web-01", ip="10.1.2.3", r7_asset_id=30)
        _seed_net(s, ip="10.99.99.99", hostname=None)
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    assert merged["web-01"].get("net_resource_id") is None
    assert "net_match_basis" not in merged["web-01"]


# ---------------------------------------------------------------------------
# TRK-087: cross-domain and IP-conflict false-merge guards
#
# WHERE THESE GUARDS LIVE NOW (P5). All three were enforced twice: once inside
# ``_emit_is_same_as_edges`` before it asserted, and once in the resolver. The
# emitter is gone, so each is asserted here against the merged RECORD (this
# agent's surviving output) and against the resolver where it decides an edge:
#
#   * cross-domain conflict -> ``graph_phase3.hosts_domain_conflict``, in BOTH
#     resolver passes. tests/test_p5_issameas_resolver_coverage.py::
#     test_the_ip_floor_does_not_bypass_the_domain_conflict_guard.
#   * open IP identity conflict -> the resolver does not need the derived
#     DriftEvent at all: ``_score_candidate`` reads the two nodes' live
#     addresses and raises "conflicting IP" counter-evidence, which diverts the
#     pair to review instead of merging it. Reading the values beats reading a
#     flag computed from them, and it cannot go stale. The DETECTION half still
#     lives here and is asserted by
#     ``test_vsphere_ip_wins_precedence_conflict`` below.
#   * legitimate convergence (short<->FQDN, same-domain FQDNs) -> the
#     deterministic pass, at 0.990.
# ---------------------------------------------------------------------------


def test_cross_domain_same_short_name_lands_on_one_record_with_both_names(engine):
    """TRK-087 (a): two sources sharing a first DNS label but sitting in
    DIFFERENT domains (web01.corp.example.com vs web01.dmz.example.org) bucket
    under the same ``normalize_host`` short key.

    That is exactly why the per-source ORIGINAL hostnames must survive onto the
    record: the short key alone cannot tell these two apart, and the resolver's
    domain-conflict guard needs the qualified names to refuse them.
    """
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_r7(s, hostname="web01.corp.example.com", ip="10.1.2.3", r7_asset_id=40)
        _seed_net(s, ip="10.9.9.9", hostname="web01.dmz.example.org")
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    rec = merged["web01"]
    assert rec.get("r7_resource_id") is not None
    assert rec.get("net_resource_id") is not None
    assert rec.get("r7_hostname") == "web01.corp.example.com"
    assert rec.get("net_hostname") == "web01.dmz.example.org"

    from infra_brain.tools.hostmatch import hosts_domain_conflict

    assert hosts_domain_conflict(rec["r7_hostname"], rec["net_hostname"]), (
        "the record must carry enough to let the resolver refuse this pair"
    )


def test_short_name_vs_fqdn_still_merges(engine):
    """TRK-087: the legitimate convergence case is unaffected — one source
    reports the unqualified short name, another the FQDN. At least one side is
    unqualified, so there is no domain conflict and both legs land on one
    record."""
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_r7(s, hostname="web01", ip="10.1.2.3", r7_asset_id=43)  # short/unqualified
        _seed_net(s, ip="10.5.5.5", hostname="web01.corp.example.com")  # FQDN
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    rec = merged["web01"]
    assert rec.get("r7_resource_id") is not None
    assert rec.get("net_resource_id") is not None


def test_same_domain_fqdns_still_merge(engine):
    """TRK-087: two FQDNs sharing the SAME domain are the same host and must
    still converge on one record."""
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_r7(s, hostname="web01.corp.example.com", ip="10.1.2.3", r7_asset_id=44)
        _seed_net(s, ip="10.5.5.5", hostname="web01.corp.example.com")
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    rec = merged["web01"]
    assert rec.get("r7_resource_id") is not None
    assert rec.get("net_resource_id") is not None


# ---------------------------------------------------------------------------
# TRK-132: per-item SAVEPOINT isolation
# ---------------------------------------------------------------------------


def test_upsert_identities_isolates_per_item_failure(engine):
    """TRK-132: a single bad item's write must not roll back sibling items'
    writes in the same _upsert_identities call (per-item SAVEPOINT, mirroring
    net.py::_write_net_details / k8s.py::_write_k8s_details)."""
    get_session = _session_ctx(engine)

    merged = {
        "good-01": {
            "short_hostname": "good-01",
            "fqdn": "good-01.corp.local",
            "ip_addresses": ["10.1.2.5"],
        },
        "bad-01": {
            "short_hostname": "bad-01",
            "fqdn": "bad-01.corp.local",
            "ip_addresses": ["10.1.2.6"],
        },
        "good-02": {
            "short_hostname": "good-02",
            "fqdn": "good-02.corp.local",
            "ip_addresses": ["10.1.2.7"],
        },
    }

    original = HostReconcileAgent._upsert_identity_item

    def _boom_for_bad(self, session, short, data):
        if short == "bad-01":
            raise ValueError("simulated bad row")
        return original(self, session, short, data)

    with (
        patch("infra_brain.agents.host_reconcile.get_session", get_session),
        patch.object(HostReconcileAgent, "_upsert_identity_item", _boom_for_bad),
    ):
        agent = _make_agent()
        n_new, n_updated = agent._upsert_identities(merged)

    # Only the two good items were actually persisted; the bad one was
    # skipped (not counted) rather than aborting the whole phase.
    assert n_new == 2
    assert n_updated == 0

    with Session(engine) as s:
        shorts = {row.short_hostname for row in s.query(HostIdentity).all()}
        assert shorts == {"good-01", "good-02"}, (
            "a bad item's failure must not roll back sibling items' writes"
        )


def test_run_downgrades_status_when_every_host_identity_write_fails(engine):
    """M-2 (F-007): ``_upsert_identities``' per-item SAVEPOINT swallows a bad
    row's exception (TRK-132) so sibling writes survive — but that resilience
    must not make a run that dropped EVERY item look identical to one that
    dropped nothing. Before the fix, ``run()`` finalized status="completed"
    immediately after ``_upsert_identities`` and never revisited it once the
    per-item write loop's own skip-and-log swallowed every failure — so a run
    where every host_identity write failed still read "completed" with an
    empty ``errors`` list on both the returned ``CollectionResult`` and the
    persisted ``CollectionRun`` row.
    """
    get_session = _session_ctx(engine)

    merged = {
        "bad-01": {"short_hostname": "bad-01", "ip_addresses": []},
        "bad-02": {"short_hostname": "bad-02", "ip_addresses": []},
    }

    def _boom(self, session, short, data):
        raise ValueError(f"simulated bad row for {short}")

    with (
        patch("infra_brain.agents.host_reconcile.get_session", get_session),
        patch("infra_brain.etl.base.get_session", get_session),
        patch.object(HostReconcileAgent, "_build_merged_hosts", return_value=(merged, [], [])),
        patch.object(HostReconcileAgent, "_upsert_identity_item", _boom),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = HostReconcileAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status != "completed", (
        "a run that dropped every host_identity write must never report 'completed'"
    )
    assert result.errors, "the dropped items must be recorded in result.errors"
    assert any("bad-01" in e or "bad-02" in e for e in result.errors)

    with Session(engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run is not None
        assert run.status != "completed", (
            "the persisted CollectionRun row must also reflect the dropped work, "
            "not just the in-memory CollectionResult"
        )


# ---------------------------------------------------------------------------
# TRK-138: vSphere-wins IP precedence rule
# ---------------------------------------------------------------------------


def _seed_vsphere_vm(session, *, hostname, ip, moref):
    """Create a Resource + VsphereVm pair and return the VsphereVm.resource_id."""
    from infra_brain.db.models import Resource, VsphereVm

    res = Resource(domain="vsphere", name=hostname, type="vm", source="vsphere")
    session.add(res)
    session.flush()
    session.add(
        VsphereVm(
            resource_id=res.id,
            vcenter="vc-01",
            moref=moref,
            name=hostname,
            guest_hostname=hostname,
            ip_address=ip,
        )
    )
    session.flush()
    return res.id


def test_vsphere_ip_wins_precedence_conflict(engine):
    """TRK-138: when vSphere reports a different IP than Rapid7 for the same
    host, primary_ip must end up as vSphere's IP and primary_ip_source must
    reflect vsphere — while the conflict is still recorded (existing
    DriftEvent-feeding ip_conflicts behavior is unchanged)."""
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_r7(s, hostname="web-01", ip="10.1.2.3", r7_asset_id=50)
        _seed_vsphere_vm(s, hostname="web-01", ip="10.1.2.99", moref="vm-50")
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, ip_conflicts, _identity_conflicts = agent._build_merged_hosts()

    rec = merged["web-01"]
    assert rec["primary_ip"] == "10.1.2.99", "vSphere's IP must win the precedence dispute"
    assert rec["primary_ip_source"] == "vsphere"

    # The conflict must still be recorded exactly as before.
    assert len(ip_conflicts) == 1
    conflict = ip_conflicts[0]
    assert conflict["short_hostname"] == "web-01"
    assert conflict["existing_ip"] == "10.1.2.3"
    assert conflict["existing_source"] == "r7"
    assert conflict["new_ip"] == "10.1.2.99"
    assert conflict["new_source"] == "vsphere"


class _StubExisting:
    """Minimal stand-in for a HostIdentity row — only needs attribute access."""

    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)


def test_coalesce_resource_id_overwrites_with_truthy_data():
    """TRK-063: a truthy value in data replaces the existing value."""
    existing = _StubExisting(r7_resource_id="old-r7")
    result = HostReconcileAgent._coalesce_resource_id(
        {"r7_resource_id": "new-r7"}, existing, "r7_resource_id"
    )
    assert result == "new-r7"


def test_coalesce_resource_id_clears_when_source_is_genuinely_absent():
    """KG-8 (supersedes the old TRK-063 "falls back to existing" contract): a
    missing/None/empty value in `data` means THIS reconcile's full merge
    found no matching row for that source, right now -- e.g. the vSphere VM
    was deleted or the Resource was retired (`_build_merged_hosts` already
    excludes those on purpose: "retired Resources are excluded so a
    decommissioned host's leg is not resurrected"). `_upsert_identities` only
    ever runs on the result of a COMPLETE merge (`_call_with_timeout` aborts
    the whole run, before `_upsert_identities` is reached, on a partial/timed
    -out `_build_merged_hosts`), so an absent leg is trustworthy -- it must
    clear the stale FK, not resurrect it forever from the previous row."""
    existing = _StubExisting(vsphere_resource_id="stale-old-value")
    # missing key
    assert HostReconcileAgent._coalesce_resource_id({}, existing, "vsphere_resource_id") is None
    # explicit None
    assert (
        HostReconcileAgent._coalesce_resource_id(
            {"vsphere_resource_id": None}, existing, "vsphere_resource_id"
        )
        is None
    )
    # empty string
    assert (
        HostReconcileAgent._coalesce_resource_id(
            {"vsphere_resource_id": ""}, existing, "vsphere_resource_id"
        )
        is None
    )


def test_coalesce_resource_id_returns_none_when_both_empty():
    """TRK-063: both data and existing falsy -> None."""
    existing = _StubExisting(k8s_resource_id=None)
    assert HostReconcileAgent._coalesce_resource_id({}, existing, "k8s_resource_id") is None


def test_host_identity_has_netdevice_resource_id_column(engine):
    """HostIdentity must carry a netdevice_resource_id column (KG-4) —
    confirms the migration/model change landed before wiring _SOURCE_KEYS."""
    from infra_brain.db.models import HostIdentity

    with Session(engine) as s:
        row = HostIdentity(short_hostname="switch-01", netdevice_resource_id=None)
        s.add(row)
        s.commit()
        fetched = s.query(HostIdentity).filter_by(short_hostname="switch-01").first()
        assert hasattr(fetched, "netdevice_resource_id")
        assert fetched.netdevice_resource_id is None


# ---------------------------------------------------------------------------
# TRK-187: host_identities completeness — Resource-sourced linux/windows legs
# and Ansible-inventory / host_purpose_map seeding
# ---------------------------------------------------------------------------


def _seed_linux_resource(session, *, hostname, with_detail=False, retired=False):
    """Create a linux-domain Resource (optionally + LinuxHost detail row)."""
    from datetime import datetime

    from infra_brain.db.models import LinuxHost, Resource

    res = Resource(
        domain="linux",
        name=hostname,
        type="linux_host",
        source="ansible",
        retired_at=datetime.now(UTC) if retired else None,
    )
    session.add(res)
    session.flush()
    if with_detail:
        session.add(LinuxHost(resource_id=res.id, distro="Debian", kernel="6.1", arch="x86_64"))
        session.flush()
    return res.id


def _seed_windows_resource(session, *, hostname, retired=False):
    """Create a windows-domain Resource WITHOUT a WindowsPatchState detail row."""
    from datetime import datetime

    from infra_brain.db.models import Resource

    res = Resource(
        domain="windows",
        name=hostname,
        type="windows_host",
        source="ansible",
        retired_at=datetime.now(UTC) if retired else None,
    )
    session.add(res)
    session.flush()
    return res.id


def test_linux_resource_without_detail_row_populates_leg(engine):
    """TRK-187 regression: a host present as a real linux-domain Resource gets
    linux_resource_id populated even when its LinuxHost detail row is missing
    (detail rows only exist for hosts that answered the latest fact gather)."""
    get_session = _session_ctx(engine)
    with Session(engine) as s:
        rid = _seed_linux_resource(s, hostname="web-01.corp.example.com", with_detail=False)
        s.commit()

    agent = _make_agent()
    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    assert "web-01" in merged
    assert merged["web-01"]["linux_resource_id"] == rid


def test_windows_resource_without_patch_state_populates_leg(engine):
    """TRK-187 regression: a host present as a real windows-domain Resource gets
    windows_resource_id populated even with no WindowsPatchState row (WinRM
    skipped / credentials unset)."""
    get_session = _session_ctx(engine)
    with Session(engine) as s:
        rid = _seed_windows_resource(s, hostname="WINSRV01")
        s.commit()

    agent = _make_agent()
    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    assert "winsrv01" in merged
    assert merged["winsrv01"]["windows_resource_id"] == rid


def test_retired_linux_windows_resources_are_excluded(engine):
    """TRK-187: retired Resources must not resurrect a leg."""
    get_session = _session_ctx(engine)
    with Session(engine) as s:
        _seed_linux_resource(s, hostname="old-lnx", retired=True)
        _seed_windows_resource(s, hostname="old-win", retired=True)
        s.commit()

    agent = _make_agent()
    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    assert "old-lnx" not in merged
    assert "old-win" not in merged


def test_linux_resource_with_detail_row_still_populates_leg(engine):
    """The pre-TRK-187 happy path (Resource + LinuxHost both present) keeps
    working after the source query moved from the detail join to Resource."""
    get_session = _session_ctx(engine)
    with Session(engine) as s:
        rid = _seed_linux_resource(s, hostname="db-01", with_detail=True)
        s.commit()

    agent = _make_agent()
    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    assert merged["db-01"]["linux_resource_id"] == rid


def test_inventory_and_purpose_map_host_gets_identity_row(engine):
    """TRK-187 regression (the SITEB-SRV-02 case): a host present in >=2 source
    domains — the Ansible inventory AND host_purpose_map — but in no collector
    table must still get a host_identities row (previously: zero row, and
    get_host_profile returned 'not found')."""

    from infra_brain.db.models import AnsibleInventoryHost, HostIdentity, HostPurposeMap

    get_session = _session_ctx(engine)
    with Session(engine) as s:
        s.add(AnsibleInventoryHost(group_id=_inventory_group_id(s), name="SITEB-SRV-02"))
        s.add(HostPurposeMap(hostname="SITEB-SRV-02", purpose="PMPv4 Application/DB Server"))
        s.commit()

    agent = _make_agent()
    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()
        assert "siteb-srv-02" in merged
        n_new, n_updated = agent._upsert_identities(merged)

    assert n_new == 1
    with Session(engine) as s:
        row = s.query(HostIdentity).filter_by(short_hostname="siteb-srv-02").first()
        assert row is not None
        # Seed-only row: known host, no collector legs yet.
        assert row.linux_resource_id is None
        assert row.r7_resource_id is None


def test_seed_only_host_carries_no_source_legs(engine):
    """A seeded inventory/purpose-map host exists but has no collector leg.

    It used to be asserted via "emits no IS_SAME_AS edges"; since P5 this agent
    emits none for anything, so the assertion is made where the fact actually
    lives — the merged record. A seed-only host still gets a HostIdentity row
    (that is the point of seeding, TRK-187), it just has nothing to be the same
    AS yet.
    """

    from infra_brain.db.models import AnsibleInventoryHost

    get_session = _session_ctx(engine)
    with Session(engine) as s:
        s.add(AnsibleInventoryHost(group_id=_inventory_group_id(s), name="lonely-host"))
        s.commit()

    agent = _make_agent()
    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    rec = merged["lonely-host"]
    assert rec["ansible_inventory_hostname"] == "lonely-host"
    assert [label for key, label in HostReconcileAgent._SOURCE_KEYS if rec.get(key)] == []


def test_inventory_seed_merges_with_collector_leg(engine):
    """When a collector DOES know the host, the inventory seed and the
    collector leg converge onto one merged record via normalize_host()."""

    from infra_brain.db.models import AnsibleInventoryHost

    get_session = _session_ctx(engine)
    with Session(engine) as s:
        rid = _seed_linux_resource(s, hostname="app-01.corp.example.com")
        s.add(AnsibleInventoryHost(group_id=_inventory_group_id(s), name="APP-01"))
        s.commit()

    agent = _make_agent()
    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        merged, _conflicts, _identity_conflicts = agent._build_merged_hosts()

    assert list(merged) == ["app-01"]
    assert merged["app-01"]["linux_resource_id"] == rid


# ---------------------------------------------------------------------------
# TRK-304 / GitLab #158: same-source identity ambiguity must be surfaced,
# never silently first-write-wins-settled.
# ---------------------------------------------------------------------------


def test_vsphere_same_source_collision_recorded_not_silently_dropped(engine):
    """Two DIFFERENT VsphereVm rows (e.g. a live VM and a stale
    snapshot/clone) that normalize to the SAME short hostname must not be
    silently first-write-wins-merged. The first-seen resource_id is still
    kept on the field (no guessing which is "right"), but the collision is
    (a) recorded into identity_conflicts and (b) flagged on the merged record
    via identity_ambiguous_sources so it is inspectable, not just dropped."""
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        rid_first = _seed_vsphere_vm(s, hostname="web-77", ip="10.5.5.10", moref="vm-live")
        rid_second = _seed_vsphere_vm(s, hostname="web-77", ip="10.5.5.11", moref="vm-stale-clone")
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _ip_conflicts, identity_conflicts = agent._build_merged_hosts()

    # No crash, and exactly one merged record for the colliding short hostname.
    assert list(merged) == ["web-77"]
    rec = merged["web-77"]

    # First-write-wins is preserved for the FIELD VALUE — no guessing.
    assert rec["vsphere_resource_id"] == rid_first

    # The ambiguity must be surfaced, not silently resolved.
    assert rec.get("identity_ambiguous_sources") == ["vsphere"]

    assert len(identity_conflicts) == 1
    conflict = identity_conflicts[0]
    assert conflict["short_hostname"] == "web-77"
    assert conflict["source"] == "vsphere"
    assert conflict["field"] == "vsphere_resource_id"
    assert conflict["kept_resource_id"] == rid_first
    assert conflict["dropped_resource_id"] == rid_second


def test_r7_same_source_collision_recorded(engine):
    """Same coverage as the vSphere case but for the r7 source, whose
    dict-seeding shape (initial value set inline in .setdefault(), not via a
    separate 'if is None' check) is different from every other source loop —
    make sure the collision branch fires correctly there too."""
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        rid_first = _seed_r7(s, hostname="dup-host", ip="10.1.1.1", r7_asset_id=201)
        rid_second = _seed_r7(s, hostname="dup-host", ip="10.1.1.2", r7_asset_id=202)
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _ip_conflicts, identity_conflicts = agent._build_merged_hosts()

    rec = merged["dup-host"]
    assert rec["r7_resource_id"] == rid_first
    assert rec.get("identity_ambiguous_sources") == ["r7"]
    assert len(identity_conflicts) == 1
    assert identity_conflicts[0]["field"] == "r7_resource_id"
    assert identity_conflicts[0]["dropped_resource_id"] == rid_second


def test_non_ambiguous_single_row_per_source_unaffected(engine):
    """Regression: unrelated hosts, one row per source each, must behave
    exactly as before — no identity_conflicts, no identity_ambiguous_sources
    key at all, and every leg populated normally."""
    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_r7(s, hostname="host-a", ip="10.2.2.1", r7_asset_id=301)
        _seed_vsphere_vm(s, hostname="host-b", ip="10.2.2.2", moref="vm-b")
        s.commit()

    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent = _make_agent()
        merged, _ip_conflicts, identity_conflicts = agent._build_merged_hosts()

    assert identity_conflicts == []
    assert "identity_ambiguous_sources" not in merged["host-a"]
    assert "identity_ambiguous_sources" not in merged["host-b"]
    assert merged["host-a"]["r7_resource_id"] is not None
    assert merged["host-b"]["vsphere_resource_id"] is not None


def test_identity_conflict_event_written_and_run_completes(engine):
    """End-to-end: run() must write an identity-conflict DriftEvent with
    field='vsphere_resource_id' for a same-source vSphere collision, and the
    run must still complete (not fail) — ambiguity is surfaced, not fatal.

    GitLab #163 defect 3: the two seeded VMs share a guest hostname but carry
    different morefs, so the flat ``identity_conflict`` drift_type this test
    originally asserted is now the more specific
    ``identity_conflict_distinct_object``."""
    from infra_brain.db.models import DriftEvent

    get_session = _session_ctx(engine)

    with Session(engine) as s:
        _seed_vsphere_vm(s, hostname="dup-vm", ip="10.9.1.1", moref="vm-a")
        _seed_vsphere_vm(s, hostname="dup-vm", ip="10.9.1.2", moref="vm-b")
        s.commit()

    with (
        patch("infra_brain.agents.host_reconcile.get_session", get_session),
        patch("infra_brain.etl.base.get_session", get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = HostReconcileAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "completed"

    with Session(engine) as s:
        event = s.query(DriftEvent).filter_by(drift_type="identity_conflict_distinct_object").one()
        assert event.field == "vsphere_resource_id"
        assert event.status == "open"
        assert event.new_value["source"] == "vsphere"
        assert event.new_value["short_hostname"] == "dup-vm"
        assert event.new_value["conflict_class"] == "distinct_object"


def test_identity_conflict_event_idempotent_refresh(engine):
    """Repeat runs against an ongoing same-source collision must REFRESH the
    existing open identity_conflict DriftEvent rather than piling up a new
    one every reconcile cycle (mirrors _upsert_ip_conflict_event's open/
    refresh semantics, TRK-132-adjacent idempotency contract)."""
    from infra_brain.db.models import DriftEvent, Resource

    get_session = _session_ctx(engine)

    with Session(engine) as s:
        resource = Resource(domain="vsphere", name="dup-vm", type="vm", source="vsphere")
        s.add(resource)
        s.commit()
        kept_id = resource.id

    conflict_v1 = {
        "short_hostname": "dup-vm",
        "source": "vsphere",
        "field": "vsphere_resource_id",
        "kept_resource_id": kept_id,
        "dropped_resource_id": "11111111-1111-1111-1111-111111111111",
        "kept_hostname": "dup-vm",
        "dropped_hostname": "dup-vm-clone-1",
    }
    conflict_v2 = dict(conflict_v1, dropped_resource_id="22222222-2222-2222-2222-222222222222")

    with get_session() as s:
        agent = _make_agent()
        agent._upsert_identity_conflict_event(s, conflict_v1, run_id=None)
        s.commit()

    with get_session() as s:
        agent._upsert_identity_conflict_event(s, conflict_v2, run_id=None)
        s.commit()

    with Session(engine) as s:
        events = s.query(DriftEvent).filter_by(drift_type="identity_conflict").all()
        assert len(events) == 1, "second collision on the same field must refresh, not duplicate"
        assert events[0].new_value["dropped_resource_id"] == str(conflict_v2["dropped_resource_id"])


# ---------------------------------------------------------------------------
# GitLab #163 — detected_at/last_seen_at split, persisted drift_count, and the
# identity_conflict drift_type sub-classing.
# ---------------------------------------------------------------------------


def _identity_conflict(**overrides) -> dict:
    """A minimal identity-collision dict as _note_identity_collision builds one."""
    base = {
        "short_hostname": "dup-vm",
        "source": "vsphere",
        "field": "vsphere_resource_id",
        "dropped_resource_id": "11111111-1111-1111-1111-111111111111",
        "kept_hostname": "dup-vm",
        "dropped_hostname": "dup-vm",
        "conflict_class": "unclassified",
        "kept_object_ids": {},
        "dropped_object_ids": {},
    }
    base.update(overrides)
    return base


def test_unchanged_identity_conflict_bumps_last_seen_only(engine):
    """GitLab #163 defect 1 (a): re-emitting the SAME unchanged conflict must
    leave exactly one row with detected_at and collection_run_id untouched, and
    only last_seen_at advanced.

    This is the regression test for the old behaviour, where the refresh branch
    stamped ``detected_at = now`` unconditionally on every 30-minute sweep — so
    a conflict open for months always looked brand new to every age/staleness
    consumer — while never advancing collection_run_id at all.
    """
    import uuid as _uuid

    from infra_brain.db.models import DriftEvent, Resource

    get_session = _session_ctx(engine)
    first_run = _uuid.uuid4()
    second_run = _uuid.uuid4()

    with Session(engine) as s:
        resource = Resource(domain="vsphere", name="dup-vm", type="vm", source="vsphere")
        s.add(resource)
        for rid in (first_run, second_run):
            s.add(CollectionRun(id=rid, domain="host_reconcile", trigger_type="scheduled"))
        s.commit()
        kept_id = resource.id

    conflict = _identity_conflict(kept_resource_id=kept_id)
    agent = _make_agent()

    with get_session() as s:
        agent._upsert_identity_conflict_event(s, conflict, run_id=first_run)
        s.commit()

    with Session(engine) as s:
        row = s.query(DriftEvent).one()
        detected_before = row.detected_at
        last_seen_before = row.last_seen_at
        assert last_seen_before is not None, "a new row must stamp last_seen_at too"

    # Same conflict, next sweep — nothing about the finding changed.
    with get_session() as s:
        agent._upsert_identity_conflict_event(s, dict(conflict), run_id=second_run)
        s.commit()

    with Session(engine) as s:
        rows = s.query(DriftEvent).all()
        assert len(rows) == 1, "an unchanged re-observation must refresh, not duplicate"
        row = rows[0]
        assert row.detected_at == detected_before, "detected_at must stay at FIRST observation"
        assert row.collection_run_id == first_run, "collection_run_id must not advance either"
        assert row.last_seen_at > last_seen_before, "last_seen_at must advance on re-observation"


def test_changed_identity_conflict_advances_detected_at_and_run_id(engine):
    """GitLab #163 defect 1 (b): when the finding's DATA genuinely changed (a
    different dropped resource), BOTH detected_at and collection_run_id advance
    — that is a new observation, not a re-observation of the old one."""
    import uuid as _uuid

    from infra_brain.db.models import DriftEvent, Resource

    get_session = _session_ctx(engine)
    first_run = _uuid.uuid4()
    second_run = _uuid.uuid4()

    with Session(engine) as s:
        resource = Resource(domain="vsphere", name="dup-vm", type="vm", source="vsphere")
        s.add(resource)
        for rid in (first_run, second_run):
            s.add(CollectionRun(id=rid, domain="host_reconcile", trigger_type="scheduled"))
        s.commit()
        kept_id = resource.id

    agent = _make_agent()
    v1 = _identity_conflict(kept_resource_id=kept_id)
    v2 = _identity_conflict(
        kept_resource_id=kept_id,
        dropped_resource_id="22222222-2222-2222-2222-222222222222",
    )

    with get_session() as s:
        agent._upsert_identity_conflict_event(s, v1, run_id=first_run)
        s.commit()
    with Session(engine) as s:
        detected_before = s.query(DriftEvent).one().detected_at

    with get_session() as s:
        agent._upsert_identity_conflict_event(s, v2, run_id=second_run)
        s.commit()

    with Session(engine) as s:
        rows = s.query(DriftEvent).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.detected_at >= detected_before
        assert row.collection_run_id == second_run, "a changed finding must re-anchor to this run"
        assert row.new_value["dropped_resource_id"] == str(v2["dropped_resource_id"])
        assert row.last_seen_at is not None


def test_run_persists_drift_count_on_the_collection_run_row(engine):
    """GitLab #163 defect 2: drift_count must be PERSISTED onto the
    CollectionRun row, not merely returned in the in-memory CollectionResult.

    Asserting on ``result.drift_count`` is exactly what let this bug live: the
    in-memory value was always right while the DB column every health/dashboard
    consumer actually reads stayed at its default of 0. So this reads the row
    back from the database.
    """
    from infra_brain.db.models import DriftEvent

    get_session = _session_ctx(engine)

    with Session(engine) as s:
        # Two same-source vSphere collisions -> two identity-conflict events.
        _seed_vsphere_vm(s, hostname="dup-one", ip="10.9.2.1", moref="vm-a1")
        _seed_vsphere_vm(s, hostname="dup-one", ip="10.9.2.2", moref="vm-b1")
        _seed_vsphere_vm(s, hostname="dup-two", ip="10.9.3.1", moref="vm-a2")
        _seed_vsphere_vm(s, hostname="dup-two", ip="10.9.3.2", moref="vm-b2")
        s.commit()

    with (
        patch("infra_brain.agents.host_reconcile.get_session", get_session),
        patch("infra_brain.etl.base.get_session", get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = HostReconcileAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "completed"

    with Session(engine) as s:
        written = s.query(DriftEvent).filter(DriftEvent.collection_run_id == result.run_id).count()
        assert written > 0, "fixture must actually produce drift for this test to mean anything"
        persisted = s.get(CollectionRun, result.run_id)
        assert persisted.drift_count == written, (
            "CollectionRun.drift_count must be written back to the DB, not just returned"
        )
        assert persisted.drift_count == result.drift_count


def test_suffix_variant_and_distinct_object_get_different_drift_types(engine):
    """GitLab #163 defect 3: a DNS-suffix-variant collision and a
    same-hostname/different-moref collision must land on DIFFERENT drift_type
    values so they can be triaged (and bulk-resolved) separately."""
    from infra_brain.db.models import DriftEvent

    get_session = _session_ctx(engine)

    with Session(engine) as s:
        # Same short name, different DNS domains -> suffix variant.
        _seed_vsphere_vm(s, hostname="svar.corp.example.com", ip="10.8.1.1", moref="vm-s1")
        _seed_vsphere_vm(s, hostname="svar.dmz.example.org", ip="10.8.1.2", moref="vm-s2")
        # Identical guest hostname, different moref -> genuinely distinct object.
        _seed_vsphere_vm(s, hostname="dobj", ip="10.8.2.1", moref="vm-d1")
        _seed_vsphere_vm(s, hostname="dobj", ip="10.8.2.2", moref="vm-d2")
        s.commit()

    with (
        patch("infra_brain.agents.host_reconcile.get_session", get_session),
        patch("infra_brain.etl.base.get_session", get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = HostReconcileAgent()
        agent.run(trigger_type="scheduled", scope="all")

    with Session(engine) as s:
        by_host = {
            e.new_value["short_hostname"]: e.drift_type
            for e in s.query(DriftEvent).filter(DriftEvent.field == "vsphere_resource_id").all()
        }

    assert by_host["svar"] == "identity_conflict_suffix_variant"
    assert by_host["dobj"] == "identity_conflict_distinct_object"


def test_distinct_object_conflict_suppressed_when_review_already_queued(engine):
    """GitLab #163 defect 3: a distinct-object collision whose pair already has a
    live entity_resolution_same_as ProposedAction must NOT also emit a
    DriftEvent — that would put one signal in two inboxes."""
    from infra_brain.db.models import DriftEvent, ProposedAction, Resource
    from infra_brain.graph_phase3 import REVIEW_ACTION_TYPE, REVIEW_AGENT

    get_session = _session_ctx(engine)

    with Session(engine) as s:
        resource = Resource(domain="vsphere", name="dup-vm", type="vm", source="vsphere")
        s.add(resource)
        s.add(
            ProposedAction(
                agent=REVIEW_AGENT,
                action_type=REVIEW_ACTION_TYPE,
                target="same-as:VsphereVM:vc-01:vm-a",
                payload={},
                status="pending",
            )
        )
        s.commit()
        kept_id = resource.id

    conflict = _identity_conflict(
        kept_resource_id=kept_id,
        conflict_class="distinct_object",
        kept_object_ids={"vcenter": "vc-01", "moref": "vm-a", "uuid": "u-a"},
        dropped_object_ids={"vcenter": "vc-01", "moref": "vm-b", "uuid": "u-b"},
    )
    agent = _make_agent()
    with get_session() as s:
        agent._upsert_identity_conflict_event(s, conflict, run_id=None)
        s.commit()

    with Session(engine) as s:
        assert s.query(DriftEvent).count() == 0

    # Fail-open control: no vCenter identity to match on -> nothing suppressed.
    bare = _identity_conflict(kept_resource_id=kept_id, conflict_class="distinct_object")
    with get_session() as s:
        agent._upsert_identity_conflict_event(s, bare, run_id=None)
        s.commit()
    with Session(engine) as s:
        assert s.query(DriftEvent).count() == 1
