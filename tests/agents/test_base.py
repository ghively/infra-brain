from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session, sessionmaker

from infra_brain.agents.base import BaseAgent, CollectionResult
from infra_brain.db.models import CollectionRun, Resource, Snapshot


class ConcreteAgent(BaseAgent):
    domain = "test"

    def collect(self, scope: str) -> list[dict]:
        return [{"name": "host01", "type": "host", "data": {"ip": "10.0.0.1"}}]


def test_run_persists_real_rows_with_correct_fields(engine):
    """Run against a real in-memory DB and assert the actual rows written —
    not just that session.add() was called N times."""
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = ConcreteAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert isinstance(result, CollectionResult)
    assert result.domain == "test"
    assert result.resources_found == 1
    assert result.status == "completed"

    with Session(engine) as verify:
        resource = verify.query(Resource).filter_by(name="host01").one()
        assert resource.domain == "test"
        assert resource.type == "host"
        assert resource.source == "ConcreteAgent"
        assert resource.metadata_ == {"ip": "10.0.0.1"}

        snapshot = verify.query(Snapshot).filter_by(resource_id=resource.id).one()
        assert snapshot.snapshot == {"ip": "10.0.0.1"}
        assert snapshot.run_id == result.run_id

        run = verify.get(CollectionRun, result.run_id)
        assert run.status == "completed"
        assert run.resources_found == 1


def test_run_counts_drift_events():
    """drift_count reflects DriftEvent rows created during the run, not hardcoded 0."""

    class _A(BaseAgent):
        domain = "test"

        def collect(self, scope="all"):
            return []

    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = None

    with (
        patch("infra_brain.etl.base.get_session") as mock_get_session,
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.etl.base.count_drift_events_for_run", return_value=3) as mock_count,
    ):
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

        agent = _A()
        result = agent.run()

    assert result.drift_count == 3
    mock_count.assert_called_once()
