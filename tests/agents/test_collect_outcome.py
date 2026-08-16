"""R3 contract tests: BaseAgent.run() maps CollectOutcome -> run status.

Each test would FAIL against the pre-2.1 code because run() only knew
list[dict] and always wrote status="completed" on a non-raising collect().
"""

from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker

from infra_brain.agents.base import BaseAgent, CollectOutcome
from infra_brain.db.models import CollectionRun


def _session_ctx(engine):
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return _session


def _make(outcome):
    class _A(BaseAgent):
        domain = "outcometest"

        def collect(self, scope="all"):
            return outcome

    return _A


def _run(engine, outcome):
    with (
        patch("infra_brain.etl.base.get_session", _session_ctx(engine)),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        return _make(outcome)().run()


def test_ok_outcome_maps_to_completed(engine):
    outcome = CollectOutcome(items=[{"name": "h1", "type": "host", "data": {}}])
    result = _run(engine, outcome)
    assert result.status == "completed"
    assert result.resources_found == 1
    with Session(engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run.status == "completed"
        assert run.error_message is None


def test_partial_outcome_maps_to_partial_with_error_message(engine):
    outcome = CollectOutcome(
        items=[{"name": "h1", "type": "host", "data": {}}],
        errors=["project X: 403 Forbidden"],
    )
    result = _run(engine, outcome)
    assert result.status == "partial"
    assert result.errors == ["project X: 403 Forbidden"]
    with Session(engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run.status == "partial"
        assert "403 Forbidden" in run.error_message
        assert run.resources_found == 1


def test_failed_outcome_maps_to_failed(engine):
    outcome = CollectOutcome(items=[], errors=["upstream dead"])
    result = _run(engine, outcome)
    assert result.status == "failed"
    with Session(engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run.status == "failed"
        assert "upstream dead" in run.error_message


def test_legacy_list_return_still_completed(engine):
    result = _run(engine, [{"name": "h1", "type": "host", "data": {}}])
    assert result.status == "completed"
    assert result.resources_found == 1


def test_count_override_sets_resources_found(engine):
    result = _run(engine, CollectOutcome(items=[], count_override=7))
    assert result.status == "completed"
    assert result.resources_found == 7
    with Session(engine) as s:
        assert s.get(CollectionRun, result.run_id).resources_found == 7
