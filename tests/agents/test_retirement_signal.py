"""A durable entity disappearing from the fleet must produce a signal.

On 2026-08-11, two real machines (media_host, storage_node) dropped off the fleet.
The system produced ZERO signal: no drift event, no alert — only a silent
``retired_at`` stamp that both callers of ``detect_state_drift()`` discard.

That silence is the over-correction of GitLab #137, which rightly removed
per-resource presence DriftEvents after a 60k-row bookkeeping flood, but went
from "60k noise rows" to "nothing at all". These tests pin the middle ground:

  * a NEWLY-retired resource of a durable type (linux_host, net_device, …)
    fires ``send_ops_alert`` — once, because the retirement stamp is idempotent
    and only newly-retired rows enter the list;
  * ephemeral types (the churny kinds whose noise caused #137) stay silent;
  * the alert is bounded by a cap, so a mass-retirement pass cannot flood;
  * an alerting failure never fails the drift pass that detected the
    disappearance.

No DriftEvent is created — #137's decision stands; the signal path is
``send_ops_alert``, which pages when a webhook is configured and persists to
the dashboard-queryable AuditLog when it is not (TRK-329).
"""

import uuid
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy.orm import Session

from infra_brain.agents.drift import DriftDetector
from infra_brain.db.models import CollectionRun, DriftEvent, Resource, Snapshot

from tests.support.pg import make_engine

MODULE = "infra_brain.agents.drift"


def _engine():
    eng = make_engine()
    return eng


def _session_factory(eng):
    factory_engine = eng

    @contextmanager
    def _get():
        with Session(factory_engine) as s:
            yield s
            s.commit()

    return _get


def _seed_disappearance(eng, rtype: str, name: str, domain: str = "linux"):
    """One resource observed in run 1, then absent from run 2 (the latest)."""
    with Session(eng) as s:
        r = Resource(id=uuid.uuid4(), domain=domain, type=rtype, name=name, source="test")
        run1 = CollectionRun(id=uuid.uuid4(), domain=domain, trigger_type="scheduled",
                             status="completed")
        run2 = CollectionRun(id=uuid.uuid4(), domain=domain, trigger_type="scheduled",
                             status="completed")
        s.add_all([r, run1, run2])
        s.flush()
        s.add(Snapshot(id=uuid.uuid4(), resource_id=r.id, run_id=run1.id, snapshot={}))
        # The detector picks the latest completed run per domain by
        # finished_at DESC — set both explicitly, run2 STRICTLY later, or the
        # NULL defaults make the ordering (and therefore the test) arbitrary.
        run1.finished_at = run1.started_at
        run2.started_at = run1.started_at + timedelta(minutes=5)
        run2.finished_at = run2.started_at
        s.commit()
        return r.id


def _run_detect(eng):
    agent = DriftDetector()
    with patch(f"{MODULE}.get_session", _session_factory(eng)):
        return agent.detect_state_drift()


def test_a_disappeared_host_fires_an_ops_alert():
    eng = _engine()
    _seed_disappearance(eng, "linux_host", "media_host")

    with patch(f"{MODULE}.send_ops_alert", create=True) as _unused, patch(
        "infra_brain.tools.ops_webhook.send_ops_alert"
    ) as alert:
        retired = _run_detect(eng)

    assert len(retired) == 1
    with Session(eng) as s:
        names = [r.name for r in s.query(Resource).filter(Resource.retired_at.isnot(None))]
    assert names == ["media_host"]
    assert alert.called, (
        "a linux_host vanishing from the fleet is the single most alarm-worthy "
        "event this system can observe — it must not be a silent retired_at stamp"
    )
    category = alert.call_args.args[0]
    messages = alert.call_args.args[1]
    assert category == "entity_retired"
    assert any("media_host" in m for m in messages), "the alert must NAME the host"


def test_an_ephemeral_type_retires_silently():
    """The #137 flood class — churny bookkeeping types must never alert."""
    eng = _engine()
    _seed_disappearance(eng, "wazuh_alert", "sshd: something", domain="wazuh")

    with patch("infra_brain.tools.ops_webhook.send_ops_alert") as alert:
        retired = _run_detect(eng)

    assert len(retired) == 1, "it still retires — only the ALERT is filtered"
    assert not alert.called


def test_alerts_only_once_because_retirement_is_idempotent():
    eng = _engine()
    _seed_disappearance(eng, "linux_host", "storage_node")

    with patch("infra_brain.tools.ops_webhook.send_ops_alert") as alert:
        _run_detect(eng)
        _run_detect(eng)  # second pass: already retired, not "newly retired"

    assert alert.call_count == 1, (
        "a dead host must page once, not once per drift pass forever — the "
        "idempotence guard is what makes this alert flood-proof"
    )


def test_mass_retirement_is_capped():
    eng = _engine()
    for i in range(DriftDetector._RETIREMENT_ALERT_CAP + 10):
        _seed_disappearance(eng, "linux_host", f"host-{i:03}")

    with patch("infra_brain.tools.ops_webhook.send_ops_alert") as alert:
        _run_detect(eng)

    messages = alert.call_args.args[1]
    # cap + one summary line, never one message per resource unbounded
    assert len(messages) == DriftDetector._RETIREMENT_ALERT_CAP + 1
    assert "more durable" in messages[-1], "truncation must be stated, not silent"


def test_alert_failure_never_fails_the_drift_pass():
    eng = _engine()
    _seed_disappearance(eng, "linux_host", "media_host")

    with patch(
        "infra_brain.tools.ops_webhook.send_ops_alert",
        side_effect=RuntimeError("webhook exploded"),
    ):
        retired = _run_detect(eng)  # must not raise

    assert len(retired) == 1
    with Session(eng) as s:
        assert s.query(Resource).filter(Resource.retired_at.isnot(None)).count() == 1, (
            "the retirement itself is committed even when alerting fails — "
            "an alerting outage must never mask the detection"
        )


# ---------------------------------------------------------------------------
# Event-shaped drift suppression (the other half of the same signal problem:
# 91% of a week's live drift feed was wazuh_alert churn)
# ---------------------------------------------------------------------------


def _seed_two_snapshots(eng, rtype: str, name: str, domain: str = "wazuh"):
    """One resource with TWO differing snapshots — genuine diffable change."""
    with Session(eng) as s:
        r = Resource(id=uuid.uuid4(), domain=domain, type=rtype, name=name, source="test")
        run1 = CollectionRun(id=uuid.uuid4(), domain=domain, trigger_type="scheduled",
                             status="completed")
        run2 = CollectionRun(id=uuid.uuid4(), domain=domain, trigger_type="scheduled",
                             status="completed")
        s.add_all([r, run1, run2])
        s.flush()
        run1.finished_at = run1.started_at
        run2.started_at = run1.started_at + timedelta(minutes=5)
        run2.finished_at = run2.started_at
        s.add(Snapshot(id=uuid.uuid4(), resource_id=r.id, run_id=run1.id,
                       snapshot={"agent_name": "old"}))
        s.add(Snapshot(id=uuid.uuid4(), resource_id=r.id, run_id=run2.id,
                       snapshot={"agent_name": "new"}))
        s.commit()
        return r.id


def test_event_shaped_resources_never_generate_drift():
    """An alert is an occurrence, not an asset — its field changes are
    ingestion bookkeeping. Measured live: 846 of 931 drift events in one week
    (91%) were wazuh_alert churn, drowning every real change.

    Two alerts rather than one, because one event-shaped resource is not a
    realistic fixture for a table that held 846 of them — see
    ``test_detect_all_survives_a_fleet_of_event_shaped_resources`` for what
    that row count is load-bearing for.
    """
    eng = _engine()
    _seed_two_snapshots(eng, "wazuh_alert", "sshd: authentication success.")
    _seed_two_snapshots(eng, "wazuh_alert", "sshd: authentication failure.")

    agent = DriftDetector()
    with patch(f"{MODULE}.get_session", _session_factory(eng)):
        events = agent.detect_all()

    assert events == [], "event-shaped types must be filtered at GENERATION"


def test_detect_all_survives_a_fleet_of_event_shaped_resources():
    """The MR !36 production crash, pinned (TRK-356).

    The version of this filter that shipped built its subquery with
    ``.scalar_subquery()`` and then re-wrapped it in ``select()``, compiling
    the self-heal predicate to ``IN (SELECT (SELECT ...))``. PostgreSQL
    evaluates that inner select once per candidate ``drift_events`` row, as a
    scalar, and raises ``CardinalityViolation`` the moment it matches a second
    resource. Both conditions have to hold for the bug to fire: at least one
    open drift row (or the subquery is never evaluated) AND at least two
    event-shaped resources (or it returns exactly one value and is legal).
    The live database had 846 alerts and hundreds of open rows, so the FIRST
    drift pass after deploy hit both and the whole sweep died.

    SQLite satisfies the same query by quietly returning the first row of the
    inner select, so ``detect_all()`` returns normally and this test is GREEN
    on SQLite even with the broken code. That is the point: it is red only
    when run against real PostgreSQL, which is what the ``agent-orm-check``
    gate (``PG_GATE_DSN``) does. It is the acceptance test for that gate — if
    this passes on PostgreSQL with the pre-hotfix ``drift.py``, the gate is
    not doing its job.
    """
    eng = _engine()
    first = _seed_two_snapshots(eng, "wazuh_alert", "sshd: authentication success.")
    _seed_two_snapshots(eng, "wazuh_alert", "sshd: authentication failure.")
    _seed_two_snapshots(eng, "wazuh_alert", "PAM: Login session opened.")
    with Session(eng) as s:
        s.add(DriftEvent(id=uuid.uuid4(), resource_id=first, drift_type="config_drift",
                         field="agent_name", status="open"))
        s.commit()

    agent = DriftDetector()
    with patch(f"{MODULE}.get_session", _session_factory(eng)):
        # The assertion is that this RETURNS. The production failure was an
        # exception out of the drift pass, not a wrong answer.
        events = agent.detect_all()

    assert events == []


def test_asset_resources_still_generate_drift():
    """The control: the same change on an ASSET type still drifts."""
    eng = _engine()
    _seed_two_snapshots(eng, "linux_host", "node_a", domain="linux")

    agent = DriftDetector()
    with patch(f"{MODULE}.get_session", _session_factory(eng)):
        events = agent.detect_all()

    assert len(events) == 1
    with Session(eng) as s:
        assert s.query(DriftEvent).one().field == "agent_name"


def test_existing_event_shaped_drift_rows_self_heal():
    """The 846 rows already in the feed resolve on the next pass — the same
    self-heal precedent as detect_state_drift's rule 3 legacy cleanup.

    Seeds **two** event-shaped resources, not one, and that is load-bearing
    (TRK-356). The shipped version of this filter built its subquery with
    ``.scalar_subquery()`` and then re-wrapped it in ``select()``, compiling to
    ``IN (SELECT (SELECT ...))``. PostgreSQL evaluates that inner select as a
    per-row scalar and raises ``CardinalityViolation`` the moment the subquery
    matches a SECOND row — with a single seeded alert the bug is invisible on
    every dialect. The real feed had 846 of them. One row is not a fixture
    detail here; it is the difference between a gate that catches the bug and
    a gate that watches it ship.
    """
    eng = _engine()
    rid = _seed_two_snapshots(eng, "wazuh_alert", "PAM: Login session opened.")
    other = _seed_two_snapshots(eng, "wazuh_alert", "sshd: authentication failure.")
    with Session(eng) as s:
        for resource_id in (rid, other):
            s.add(DriftEvent(id=uuid.uuid4(), resource_id=resource_id,
                             drift_type="config_drift", field="agent_name",
                             status="open"))
        s.commit()

    agent = DriftDetector()
    with patch(f"{MODULE}.get_session", _session_factory(eng)):
        agent.detect_all()

    with Session(eng) as s:
        rows = s.query(DriftEvent).all()
        assert len(rows) == 2
        assert all(row.status == "resolved" for row in rows), (
            "historical event-shaped pollution must self-heal, not linger as "
            "open noise the operator has to bulk-close by hand"
        )
