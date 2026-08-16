"""F-008: a forced graph_maintenance emitter exception flips the run to failed.

Fails against the old code: emitter exceptions were logged and the run
reported status="completed".
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session, sessionmaker

from infra_brain.db.models import CollectionRun

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    """In-memory SQLite engine shared across threads.

    _call_with_timeout (added in 2.1, used here per 2.6) runs collect() in a
    background thread via ThreadPoolExecutor. Plain ``sqlite:///:memory:``
    engines use a SingletonThreadPool, so a background thread gets its own
    empty database and every table lookup fails with "no such table". Use
    StaticPool + check_same_thread=False (the pattern already used by
    test_host_reconcile.py and test_graph_maintenance_backfill.py) so the
    schema created here is visible from the collect() background thread too.
    """
    eng = make_engine()
    return eng


def test_forced_emitter_exception_fails_the_run(engine, make_agent, monkeypatch):
    from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent

    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    agent = make_agent(GraphMaintenanceAgent)
    # make_agent gives a MagicMock settings; _decay_confidence does
    # int(getattr(settings, "is_same_as_decay_days", ...)) — pin a real int so
    # the full pass reaches the forced failure point.
    agent.settings.is_same_as_decay_days = 14

    def _boom(session):
        raise RuntimeError("forced emitter failure")

    # Force a full-pass phase to blow up. P5 (rev11-T5-B): this used to force
    # ``_populate_typed_relationships``, which is deleted. ``_decay_confidence``
    # is the equivalent hook — a scope="all"-only phase whose exception is NOT
    # caught inside collect(), which is exactly what F-008 is about.
    monkeypatch.setattr(agent, "_decay_confidence", _boom)

    with (
        patch("infra_brain.agents.graph_maintenance.get_session", _session),
        patch("infra_brain.etl.base.get_session", _session),
    ):
        result = agent.run(scope="all")

    assert result.status == "failed"
    assert any("forced emitter failure" in e for e in result.errors)
    with Session(engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run.status == "failed"
        assert "forced emitter failure" in run.error_message


def test_recorded_emitter_error_raises_after_commit(engine, make_agent):
    """An error recorded in _maint_errors (the 16 except blocks) fails collect()."""
    from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent

    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    agent = make_agent(GraphMaintenanceAgent)

    orig_prune = GraphMaintenanceAgent._prune_stale_edges

    def _prune_and_record(self, session):
        self._maint_errors.append("VULNERABLE_TO: simulated")
        return orig_prune(self, session)

    with (
        patch("infra_brain.agents.graph_maintenance.get_session", _session),
        patch("infra_brain.etl.base.get_session", _session),
        patch.object(GraphMaintenanceAgent, "_prune_stale_edges", _prune_and_record),
    ):
        result = agent.run(scope="quick")

    assert result.status == "failed"
    assert any("VULNERABLE_TO: simulated" in e for e in result.errors)
