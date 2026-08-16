from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import sessionmaker


def test_cloud_agent_disabled_raises_collector_skipped():
    """The aws_enabled guard raises CollectorSkipped (not [] — skipped ≠ working-empty)."""
    from infra_brain.agents.base import CollectorSkipped
    from infra_brain.agents.cloud import CloudAgent

    agent = CloudAgent.__new__(CloudAgent)
    agent.settings = MagicMock()
    agent.settings.aws_enabled = False
    with pytest.raises(CollectorSkipped):
        agent.collect("all")


def test_k8s_agent_degrades_to_list_when_client_unusable():
    """With a non-functional kubernetes client, K8sAgent degrades to a list
    (never raises, never fabricates) rather than crashing the scheduler."""
    from infra_brain.agents.k8s import K8sAgent

    agent = K8sAgent.__new__(K8sAgent)
    agent.settings = MagicMock()
    with patch("infra_brain.agents.k8s.kubernetes", MagicMock()):
        result = agent.collect("all")
    assert isinstance(result, list)


def test_fleet_health_reporter_collect_returns_list(engine):
    """F-008 (TRK-268 / GitLab #155): FleetHealthReporter.collect() writes its
    fleet-posture snapshot as a Resource directly and reports it via
    count_override — no generic items=[...] list (which would otherwise make
    ETLConnector.run() write a Snapshot for it, poisoning DriftAgent's diff)."""
    from infra_brain.agents.fleet_health import FleetHealthReporter
    from infra_brain.db.models import ZONE_CORPORATE, Resource

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

    assert isinstance(outcome.items, list)
    assert outcome.items == []
    assert outcome.count_override == 1

    with factory() as s:
        resource = (
            s.query(Resource)
            .filter_by(domain="fleet_health", type="health_snapshot", name="fleet-health")
            .one()
        )
    data = resource.metadata_
    assert "open_drift_events" in data
    assert "open_vulnerabilities" in data
    assert "eol_assets" in data
