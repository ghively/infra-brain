"""Tests for VulnTriageAgent + priority scoring."""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    CollectionRun,
    EolRegistry,
    ProposedAction,
    Resource,
    Snapshot,
    VulnQueueItem,
)
from infra_brain.triage import compute_vuln_priority, is_high_priority

from tests.support.pg import make_engine


def test_priority_ordering():
    now = datetime.now(timezone.utc)
    crit_overdue = compute_vuln_priority("critical", now - timedelta(days=1), 8, now)
    low_future = compute_vuln_priority("low", now + timedelta(days=30), 0, now)
    high_soon = compute_vuln_priority("high", now + timedelta(days=3), 0, now)
    assert crit_overdue > high_soon > low_future


def test_is_high_priority():
    now = datetime.now(timezone.utc)
    assert is_high_priority("critical", None, now)
    assert is_high_priority("low", now - timedelta(days=1), now)  # overdue
    assert not is_high_priority("low", now + timedelta(days=10), now)


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.agents.vuln_triage.get_session", _get_session):
        yield engine


def _make_agent():
    from infra_brain.agents.vuln_triage import VulnTriageAgent

    agent = VulnTriageAgent.__new__(VulnTriageAgent)
    agent.settings = None
    agent.callbacks = []
    return agent


def _seed(engine):
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-01", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(EolRegistry(resource_id=r.id, asset_name="x", pci_risk_score=8))
        s.add(
            VulnQueueItem(resource_id=r.id, cve_id="CVE-CRIT", severity="critical", status="open")
        )
        s.add(
            VulnQueueItem(
                resource_id=r.id,
                cve_id="CVE-OVERDUE",
                severity="low",
                sla_due=now - timedelta(days=2),
                status="open",
            )
        )
        s.add(
            VulnQueueItem(
                resource_id=r.id,
                cve_id="CVE-LOW",
                severity="low",
                sla_due=now + timedelta(days=60),
                status="open",
            )
        )
        s.commit()


def test_triage_proposes_for_critical_and_overdue(patched_session):
    engine = patched_session
    _seed(engine)
    _make_agent().collect()
    with Session(engine) as s:
        actions = {a.payload["cve"] for a in s.query(ProposedAction).all()}
        triaged = {
            v.cve_id for v in s.query(VulnQueueItem).filter(VulnQueueItem.status == "triage")
        }
    assert actions == {"CVE-CRIT", "CVE-OVERDUE"}  # not CVE-LOW
    assert triaged == {"CVE-CRIT", "CVE-OVERDUE"}


def test_triage_summary_persisted_as_run_scoped_snapshot(patched_session):
    """TRK-064/FBS-B5: the triage summary must be readable back from the DB,
    not just logged. Persisted as a Snapshot row scoped to the run
    (resource_id=None — the same "no owning resource" pattern discovery's
    partial-progress checkpoints use), keyed off `_active_run_id`, the
    attribute `ETLConnector.run()` sets before calling `collect()`.
    """
    engine = patched_session
    _seed(engine)
    agent = _make_agent()
    run_id = uuid.uuid4()
    # A REAL collection_runs row: Snapshot.run_id is a genuine FK, which SQLite
    # ignores and PostgreSQL enforces (agent-orm-check gate, TRK-356).
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="vuln_triage", trigger_type="scheduled", status="running"
            )
        )
        s.commit()
    agent._active_run_id = run_id
    agent.collect()

    with Session(engine) as s:
        snaps = s.query(Snapshot).filter(Snapshot.run_id == run_id).all()
        assert len(snaps) == 1
        summary = snaps[0].snapshot
        assert snaps[0].resource_id is None
        assert summary["type"] == "triage_summary"
        assert summary["newly_proposed"] == 2  # CVE-CRIT + CVE-OVERDUE
        assert summary["total_scored"] == 3  # all 3 seeded queue items


def test_triage_summary_not_persisted_without_active_run_id(patched_session):
    """A direct collect() call (no `_active_run_id` set, e.g. unit tests
    calling collect() outside run()) must not attempt a Snapshot write."""
    engine = patched_session
    _seed(engine)
    _make_agent().collect()  # no _active_run_id attribute set

    with Session(engine) as s:
        assert s.query(Snapshot).count() == 0


def test_vuln_triage_domain():
    from infra_brain.agents.vuln_triage import VulnTriageAgent

    assert VulnTriageAgent.domain == "vuln_triage"


def test_no_actions_when_queue_empty(patched_session):
    """No vulnerabilities → no proposed actions, collect() returns items=[]."""
    engine = patched_session
    result = _make_agent().collect()
    assert result.items == []
    assert result.count_override == 0
    with Session(engine) as s:
        assert s.query(ProposedAction).count() == 0


def test_no_actions_when_only_low_priority(patched_session):
    """A low-severity, far-future-SLA vuln is not high-priority → no action proposed."""
    engine = patched_session
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-01", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(
            VulnQueueItem(
                resource_id=r.id,
                cve_id="CVE-LOW",
                severity="low",
                sla_due=now + timedelta(days=90),
                status="open",
            )
        )
        s.commit()

    _make_agent().collect()
    with Session(engine) as s:
        assert s.query(ProposedAction).count() == 0
        # untouched: low-priority item stays "open", not bumped to "triage"
        assert s.query(VulnQueueItem).filter_by(status="open").count() == 1


def test_triage_confidence_pinned_to_compute_vuln_priority(patched_session):
    """TRK-057: confidence must derive from the single consolidated
    `compute_vuln_priority()` formula (the one the dashboard API also uses),
    not an ad hoc severity/recency formula. Pins the value for a fixture CVE
    so a future regression back to a second formula is caught.
    """
    from infra_brain.agents.vuln_triage import _CONFIDENCE_PRIORITY_CEILING

    engine = patched_session
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-01", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(EolRegistry(resource_id=r.id, asset_name="x", pci_risk_score=8))
        s.add(
            VulnQueueItem(
                resource_id=r.id,
                cve_id="CVE-CRIT",
                severity="critical",
                sla_due=now - timedelta(days=1),
                status="open",
            )
        )
        s.commit()

    _make_agent().collect()

    expected_priority = compute_vuln_priority("critical", now - timedelta(days=1), 8, now)
    expected_confidence = round(min(0.99, expected_priority / _CONFIDENCE_PRIORITY_CEILING), 3)

    with Session(engine) as s:
        actions = s.query(ProposedAction).all()
        assert len(actions) == 1
        action = actions[0]
        assert action.payload["priority"] == expected_priority
        assert action.confidence == expected_confidence


def test_bad_item_savepoint_isolation_does_not_cascade(patched_session):
    """TRK-132: a single bad VulnQueueItem must not abort the whole run's
    writes. Before the fix, `collect()` committed once at the very end of one
    big loop — a mid-batch exception (e.g. an `InFailedSqlTransaction`
    cascade on Postgres) lost every ProposedAction/status write for the run,
    not just the bad row's. The per-item `session.begin_nested()` SAVEPOINT
    (mirroring net.py::_write_net_details / k8s.py::_write_k8s_details) must
    isolate the bad item so its siblings still commit.
    """
    engine = patched_session
    _seed(engine)  # CVE-CRIT, CVE-OVERDUE (high-priority), CVE-LOW (skipped)

    agent = _make_agent()
    real_triage_one = agent._triage_one

    def _flaky(session, v, r, pci_by_resource):
        if v.cve_id == "CVE-OVERDUE":
            raise RuntimeError("simulated bad vuln queue item")
        return real_triage_one(session, v, r, pci_by_resource)

    with patch.object(agent, "_triage_one", side_effect=_flaky):
        result = agent.collect()

    with Session(engine) as s:
        actions = {a.payload["cve"] for a in s.query(ProposedAction).all()}
        triaged = {
            v.cve_id for v in s.query(VulnQueueItem).filter(VulnQueueItem.status == "triage")
        }

    # The bad row (CVE-OVERDUE) is guarded out by its own SAVEPOINT; the
    # sibling high-priority row (CVE-CRIT) still commits despite it.
    assert actions == {"CVE-CRIT"}
    assert triaged == {"CVE-CRIT"}
    # The bad row's status flip (open -> triage) is rolled back with the rest
    # of its SAVEPOINT, not left half-applied.
    with Session(engine) as s:
        overdue = s.query(VulnQueueItem).filter_by(cve_id="CVE-OVERDUE").one()
        assert overdue.status == "open"
    assert result.count_override == 1


def test_bad_item_savepoint_failure_downgrades_status_to_partial(patched_session):
    """M-2 (F-007): the per-item SAVEPOINT skip loop in ``collect()`` logs and
    skips a bad ``VulnQueueItem`` (TRK-132) but never recorded the failure —
    ``CollectOutcome`` came back with ``errors=[]`` regardless of how many
    items were dropped, so a run that dropped items still reported "ok" (->
    "completed"). The dropped item must now show up in ``result.errors`` and
    flip the outcome to "partial" (there IS other data — CVE-CRIT still
    proposed) per the existing CollectOutcome status contract.
    """
    engine = patched_session
    _seed(engine)  # CVE-CRIT, CVE-OVERDUE (high-priority), CVE-LOW (skipped)

    agent = _make_agent()
    real_triage_one = agent._triage_one

    def _flaky(session, v, r, pci_by_resource):
        if v.cve_id == "CVE-OVERDUE":
            raise RuntimeError("simulated bad vuln queue item")
        return real_triage_one(session, v, r, pci_by_resource)

    with patch.object(agent, "_triage_one", side_effect=_flaky):
        result = agent.collect()

    assert result.status != "ok", "a run that dropped an item must never report 'ok'"
    assert result.status == "partial"
    assert result.errors, "the dropped item must be recorded in result.errors"
    assert any("CVE-OVERDUE" in e for e in result.errors)


def test_all_items_failing_never_reports_ok(patched_session):
    """M-2: when EVERY high-priority item's write fails, count_override stays
    0 — CollectOutcome's own contract (errors + no data -> "failed") already
    covers that shape correctly as long as the failures actually land in
    ``errors``, which is exactly what this fix adds.
    """
    engine = patched_session
    _seed(engine)

    agent = _make_agent()

    with patch.object(agent, "_triage_one", side_effect=RuntimeError("simulated failure")):
        result = agent.collect()

    assert result.status != "ok"
    assert result.status == "failed"
    assert result.errors
    assert result.count_override == 0


def test_triage_idempotent(patched_session):
    engine = patched_session
    _seed(engine)
    _make_agent().collect()
    _make_agent().collect()
    with Session(engine) as s:
        assert s.query(ProposedAction).count() == 2  # no duplicates on re-run
