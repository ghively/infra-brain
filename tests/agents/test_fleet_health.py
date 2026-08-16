import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import sessionmaker

from infra_brain.agents.fleet_health import FleetHealthReporter
from infra_brain.db.models import ZONE_CORPORATE, CollectionRun, DriftEvent, Resource


def test_fleet_health_agent_domain():
    assert FleetHealthReporter.domain == "fleet_health"


def test_fleet_health_collect_returns_count_override_no_items(engine):
    """F-008 (TRK-268 / GitLab #155): collect() must report its work via
    count_override, not a generic items=[...] list — returning the snapshot
    as an item would make ETLConnector.run() write a Snapshot for it, and
    DriftAgent would then diff this agent's own ever-changing counters as
    fleet drift."""
    agent = FleetHealthReporter.__new__(FleetHealthReporter)
    agent.settings = MagicMock()
    agent.settings.default_zone = ZONE_CORPORATE

    factory = sessionmaker(bind=engine)

    @contextmanager
    def _mock_session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with patch("infra_brain.agents.fleet_health.get_session", _mock_session):
        outcome = agent.collect("all")

    assert outcome.items == []
    assert outcome.count_override == 1

    with factory() as s:
        resource = (
            s.query(Resource)
            .filter_by(domain="fleet_health", type="health_snapshot", name="fleet-health")
            .one()
        )
    assert resource.metadata_ is not None
    assert "open_drift_events" in resource.metadata_


def test_fleet_health_collect_snapshot_has_required_keys(engine):
    data = _collect(engine)
    assert "open_drift_events" in data
    assert "open_vulnerabilities" in data
    assert "eol_assets" in data


def test_fleet_health_snapshot_writes_no_new_snapshot_rows(engine):
    """Regression for TRK-268 / GitLab #155: the health_snapshot Resource must
    never gain a Snapshot row from collect() itself, so DriftAgent's
    last-two-snapshots diff never sees it as a resource with changing state
    (i.e. it can never be poisoned into fleet drift)."""
    from infra_brain.db.models import Snapshot

    agent = FleetHealthReporter.__new__(FleetHealthReporter)
    agent.settings = MagicMock()
    agent.settings.default_zone = ZONE_CORPORATE

    factory = sessionmaker(bind=engine)

    @contextmanager
    def _mock_session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with patch("infra_brain.agents.fleet_health.get_session", _mock_session):
        agent.collect("all")
        agent.collect("all")

    with factory() as s:
        resource = (
            s.query(Resource)
            .filter_by(domain="fleet_health", type="health_snapshot", name="fleet-health")
            .one()
        )
        snapshot_count = s.query(Snapshot).filter_by(resource_id=resource.id).count()

    assert snapshot_count == 0


def _add_run(engine, domain, status, minutes_ago=5):
    factory = sessionmaker(bind=engine)
    now = datetime.now(UTC)
    with factory() as s:
        s.add(
            CollectionRun(
                id=uuid.uuid4(),
                domain=domain,
                trigger_type="scheduled",
                status=status,
                started_at=now - timedelta(minutes=minutes_ago + 1),
                finished_at=now - timedelta(minutes=minutes_ago),
                resources_found=1,
            )
        )
        s.commit()


def _collect(engine):
    agent = FleetHealthReporter.__new__(FleetHealthReporter)
    agent.settings = MagicMock()
    agent.settings.default_zone = ZONE_CORPORATE
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _mock_session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with patch("infra_brain.agents.fleet_health.get_session", _mock_session):
        agent.collect("all")

    with factory() as s:
        resource = (
            s.query(Resource)
            .filter_by(domain="fleet_health", type="health_snapshot", name="fleet-health")
            .one()
        )
        return resource.metadata_


# ---------------------------------------------------------------------------
# Phase 2 sweep-graph run-state constants (etl/base.py): retry_exhausted is
# failed-equivalent; interrupt_pending is in-progress-equivalent.
# ---------------------------------------------------------------------------


def test_retry_exhausted_domain_classified_as_failed(engine):
    _add_run(engine, "cicd", "retry_exhausted")
    data = _collect(engine)
    assert "cicd" in data["failed_domains"]
    assert "cicd" not in data["in_progress_domains"]


def test_interrupt_pending_domain_classified_as_in_progress(engine):
    _add_run(engine, "linux", "interrupt_pending")
    data = _collect(engine)
    assert "linux" in data["in_progress_domains"]
    assert "linux" not in data["failed_domains"]


# ---------------------------------------------------------------------------
# drift_trend regression: must reflect real open DriftEvent counts, not
# fleet_health's own (always-zero) CollectionRun.drift_count.
# ---------------------------------------------------------------------------


def test_drift_trend_reflects_real_open_drift_events(engine):
    """FleetHealthReporter never creates DriftEvent rows tied to its own
    run_id, so CollectionRun.drift_count is always 0 on fleet_health's own
    runs. drift_trend must instead be computed from the live open
    DriftEvent count (as vuln_trend/eol_trend already are from their own
    live counts), trended against the "drift" domain's real run history --
    otherwise it is hardcoded to "stable" no matter how much real drift is
    open."""
    factory = sessionmaker(bind=engine)
    now = datetime.now(UTC)

    with factory() as s:
        # Two completed fleet_health runs so _trend_data's length gate passes.
        for i in range(2):
            s.add(
                CollectionRun(
                    id=uuid.uuid4(),
                    domain="fleet_health",
                    trigger_type="scheduled",
                    status="completed",
                    started_at=now - timedelta(minutes=(i + 1) * 10),
                    finished_at=now - timedelta(minutes=(i + 1) * 10 - 1),
                )
            )

        # Low historical drift-detection volume from the real "drift" domain.
        for i in range(3):
            s.add(
                CollectionRun(
                    id=uuid.uuid4(),
                    domain="drift",
                    trigger_type="scheduled",
                    status="completed",
                    started_at=now - timedelta(minutes=(i + 1) * 5),
                    finished_at=now - timedelta(minutes=(i + 1) * 5 - 1),
                    drift_count=1,
                )
            )

        # A pile of currently-open drift events: the real fleet-drift signal.
        resource = Resource(
            domain="linux",
            type="host",
            name="drift-trend-regression-host",
            source="test",
        )
        s.add(resource)
        s.flush()
        for _ in range(5):
            s.add(
                DriftEvent(
                    resource_id=resource.id,
                    status="open",
                    drift_type="config_drift",
                    field="test_field",
                )
            )
        s.commit()

    data = _collect(engine)

    # avg(history) == 1, current == 5 => degrading. Before the fix, current
    # was always fleet_health's own CollectionRun.drift_count (0), so this
    # would have come back "stable" regardless of the 5 open drift events.
    assert data["drift_trend"] == "degrading"
