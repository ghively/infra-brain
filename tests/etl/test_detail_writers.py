"""TRK-061/062: the detail-write phase is driven by ETLConnector.run() via the
``_detail_writers()`` hook, replacing the per-collector run() override wrapper
that ~10 collectors each hand-rolled.

These tests exercise the shared mechanism directly (order, no-op default,
failure surfacing, row-count accumulation, and the linux/windows drift-recompute
flag) so the collapse is proven behavior-preserving at the base level; the
existing per-collector tests (tests/agents/test_*.py) prove each migrated
collector still behaves. Uses the in-memory SQLite engine like
test_run_finalization.py so we can assert the actual CollectionRun row.
"""

from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker

from infra_brain.agents.base import BaseAgent
from infra_brain.db.models import CollectionRun

from tests.support.pg import make_engine


def _engine():
    eng = make_engine()
    return eng


def _session_factory(eng):
    factory = sessionmaker(bind=eng)

    @contextmanager
    def _get():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return _get


def _run(agent_cls, eng):
    _get_session = _session_factory(eng)
    with (
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        return agent_cls().run(trigger_type="scheduled", scope="all")


def test_base_default_detail_writers_is_empty_noop():
    """A collector overriding neither hook runs with no detail phase (default)."""
    eng = _engine()

    class _Plain(BaseAgent):
        domain = "test_dw_plain"

        def collect(self, scope: str = "all") -> list[dict]:
            return [{"name": "h1", "type": "host", "data": {}}]

    # base default returns an empty tuple -> nothing to run
    assert tuple(_Plain()._detail_writers("all", None)) == ()
    result = _run(_Plain, eng)
    assert result.status == "completed"
    assert result.resources_found == 1
    assert result.detail_rows_written == 0


def test_detail_writers_run_in_declared_order():
    """Each writer runs, in the order _detail_writers() returns them."""
    eng = _engine()
    calls: list[str] = []

    class _Ordered(BaseAgent):
        domain = "test_dw_order"

        def collect(self, scope: str = "all") -> list[dict]:
            return []

        def _detail_writers(self, scope, result):
            return [
                lambda: calls.append("first"),
                lambda: calls.append("second"),
                lambda: calls.append("third"),
            ]

    result = _run(_Ordered, eng)
    assert result.status == "completed"
    assert calls == ["first", "second", "third"]


def test_detail_writer_failure_surfaces_on_run_and_row():
    """A raising detail writer flips result AND CollectionRun to failed.

    Proves the hook still routes through _write_details (no silent data loss) —
    the invisible-data-loss guard the per-collector overrides used to provide.
    """
    eng = _engine()

    class _Boom(BaseAgent):
        domain = "test_dw_boom"

        def collect(self, scope: str = "all") -> list[dict]:
            return [{"name": "h1", "type": "host", "data": {}}]

        def _detail_writers(self, scope, result):
            def _explode():
                raise RuntimeError("detail kaboom")

            return [_explode]

    result = _run(_Boom, eng)
    assert result.status == "failed"
    assert any("detail" in e for e in result.errors)

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_dw_boom").one()
        assert run.status == "failed"
        assert "kaboom" in (run.error_message or "")


def test_multiple_detail_writer_failures_all_persisted_on_run():
    """Two independent failing phases (e.g. octopus/vuln-style) must BOTH land
    in CollectionRun.error_message, not just the last one.

    Regression test: _write_details used to overwrite run.error_message with
    only the most recent phase's message on each call, silently dropping
    earlier phases' failures from the DB row even though result.errors kept
    all of them.
    """
    eng = _engine()

    class _DoubleBoom(BaseAgent):
        domain = "test_dw_double_boom"

        def collect(self, scope: str = "all") -> list[dict]:
            return []

        def _detail_writers(self, scope, result):
            def _first():
                raise RuntimeError("first phase exploded")

            def _second():
                raise RuntimeError("second phase exploded")

            return [_first, _second]

    result = _run(_DoubleBoom, eng)
    assert result.status == "failed"
    assert any("first phase exploded" in e for e in result.errors)
    assert any("second phase exploded" in e for e in result.errors)

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_dw_double_boom").one()
        assert run.status == "failed"
        assert "first phase exploded" in (run.error_message or "")
        assert "second phase exploded" in (run.error_message or "")


def test_detail_writer_int_return_accumulates_row_count():
    """Int returns from writers accumulate into detail_rows_written (result+row)."""
    eng = _engine()

    class _Counter(BaseAgent):
        domain = "test_dw_count"

        def collect(self, scope: str = "all") -> list[dict]:
            return []

        def _detail_writers(self, scope, result):
            return [lambda: 3, lambda: 4]

    result = _run(_Counter, eng)
    assert result.status == "completed"
    assert result.detail_rows_written == 7

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_dw_count").one()
        assert run.detail_rows_written == 7


def test_recompute_drift_flag_recomputes_after_details():
    """_recompute_drift_after_details=True -> run() recounts drift AFTER writers.

    Mirrors the linux/windows tail: their detail phase writes DriftEvent rows
    after the base run computed drift_count, so it must be recomputed.
    """
    eng = _engine()

    class _Drift(BaseAgent):
        domain = "test_dw_drift"
        _recompute_drift_after_details = True

        def collect(self, scope: str = "all") -> list[dict]:
            return []

        def _detail_writers(self, scope, result):
            return [lambda: None]

    # first call (during run) returns 0, second call (the recompute) returns 7
    with patch("infra_brain.etl.base.count_drift_events_for_run", side_effect=[0, 7]):
        result = _run(_Drift, eng)
    assert result.drift_count == 7


def test_no_recompute_when_flag_false():
    """Default flag=False -> drift counted once, not recomputed after details."""
    eng = _engine()

    class _NoDrift(BaseAgent):
        domain = "test_dw_nodrift"

        def collect(self, scope: str = "all") -> list[dict]:
            return []

        def _detail_writers(self, scope, result):
            return [lambda: None]

    with patch("infra_brain.etl.base.count_drift_events_for_run", side_effect=[0, 7]):
        result = _run(_NoDrift, eng)
    assert result.drift_count == 0
