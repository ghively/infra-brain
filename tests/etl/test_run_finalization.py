"""TRK-106: ETLConnector.run() must finalize the CollectionRun row on EVERY exit.

The bug: run() finalized the row (terminal status + finished_at) in three
separate except-branch blocks with no try/finally. Any exit path that missed
those branches — a BaseException like KeyboardInterrupt/SystemExit (not caught
by `except Exception`), or a failure inside a finalize commit itself — left the
row permanently status="in_progress"/finished_at=NULL (a live prod P0).

The fix wraps the collection logic in a single try/finally so the row is always
finalized. These tests exercise the exit paths that the old three-block design
could miss, plus confirm the pre-existing paths still work.

Uses the in-memory SQLite engine so we can assert the actual CollectionRun row.
"""

import logging
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session, sessionmaker

from infra_brain.agents.base import BaseAgent, CollectorSkipped
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


def test_run_finalizes_row_on_generic_exception():
    """collect() raising a generic Exception → row finalized status='failed'."""
    eng = _engine()
    _get_session = _session_factory(eng)

    class _BoomAgent(BaseAgent):
        domain = "test_boom"

        def collect(self, scope: str = "all") -> list[dict]:
            raise RuntimeError("kaboom during collection")

    with (
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = _BoomAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "failed"

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_boom").one()
        assert run.status == "failed", "row must not be left in_progress"
        assert run.finished_at is not None
        assert "kaboom" in run.error_message


def test_run_finalizes_row_on_base_exception_and_reraises():
    """collect() raising a BaseException (KeyboardInterrupt) → the finally STILL
    finalizes the row AND the BaseException propagates out of run().

    This is the core regression the TRK-106 fix adds: `except Exception` never
    catches a BaseException, so the old three-block design left the row stuck
    in_progress on Ctrl-C / SIGTERM-driven SystemExit."""
    eng = _engine()
    _get_session = _session_factory(eng)

    class _InterruptAgent(BaseAgent):
        domain = "test_interrupt"

        def collect(self, scope: str = "all") -> list[dict]:
            raise KeyboardInterrupt("operator hit ctrl-c mid-collection")

    with (
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = _InterruptAgent()
        with pytest.raises(KeyboardInterrupt):
            agent.run(trigger_type="scheduled", scope="all")

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_interrupt").one()
        assert run.status != "in_progress", "BaseException must not leave row in_progress"
        assert run.finished_at is not None, "finally must set finished_at even on BaseException"
        # pessimistic default: an interrupt is recorded as failed (never left running)
        assert run.status == "failed"


def test_run_finalizes_row_on_success():
    """Successful collect → status='completed', finished_at set (path preserved)."""
    eng = _engine()
    _get_session = _session_factory(eng)

    class _OkAgent(BaseAgent):
        domain = "test_ok"

        def collect(self, scope: str = "all") -> list[dict]:
            return [{"name": "host01", "type": "host", "data": {"ip": "10.0.0.1"}}]

    with (
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = _OkAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "completed"
    assert result.resources_found == 1

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_ok").one()
        assert run.status == "completed"
        assert run.resources_found == 1
        assert run.finished_at is not None
        assert run.error_message is None


def test_run_finalizes_row_on_collector_skipped():
    """CollectorSkipped → status='skipped', finished_at set (path preserved)."""
    eng = _engine()
    _get_session = _session_factory(eng)

    class _SkipAgent(BaseAgent):
        domain = "test_skip2"

        def collect(self, scope: str = "all") -> list[dict]:
            raise CollectorSkipped("dependency unconfigured")

    with (
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = _SkipAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "skipped"

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_skip2").one()
        assert run.status == "skipped"
        assert run.finished_at is not None
        assert run.error_message == "dependency unconfigured"


def test_run_resets_resources_found_on_mid_batch_rollback():
    """A mid-loop exception while upserting collected items must not leave a
    nonzero resources_found on a "failed" CollectionRun.

    get_session() is a plain `with Session(engine) as session: yield session`
    (no per-item SAVEPOINT like `_write_each` uses) — a bad item partway
    through the loop rolls back EVERY item in that batch, including ones
    upserted successfully before it. The in-memory `resources_found` counter
    must not survive that rollback: it must be reset to reflect the true
    (empty) DB state, not the count reached before the exception fired.
    """
    eng = _engine()
    _get_session = _session_factory(eng)

    class _PartialBoomAgent(BaseAgent):
        domain = "test_partial_boom"

        def collect(self, scope: str = "all") -> list[dict]:
            return [
                {"name": "host01", "type": "host", "data": {"ip": "10.0.0.1"}},
                # missing "name" -> KeyError inside _upsert_resource, AFTER
                # host01 above was already added to the (uncommitted) session.
                {"type": "host", "data": {"ip": "10.0.0.2"}},
            ]

    with (
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = _PartialBoomAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "failed"
    assert result.resources_found == 0, (
        "a rolled-back batch must not report a nonzero resources_found on the result"
    )

    from infra_brain.db.models import Resource

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_partial_boom").one()
        assert run.status == "failed"
        assert run.resources_found == 0, (
            "CollectionRun.resources_found must match the rolled-back DB state "
            "(zero rows), not an in-memory count from before the exception"
        )
        # Confirm the rollback actually happened: host01 was never persisted.
        assert (
            s.query(Resource).filter_by(domain="test_partial_boom", name="host01").first()
            is None
        )


def test_run_success_path_logs_domain_and_run_id_as_extra_fields(caplog):
    """#87: the highest-value log call sites (ETLConnector.run()) must attach
    domain/run_id via extra={} so a log aggregator can filter/group on them as
    first-class fields, not just substring-match inside the message text."""
    eng = _engine()

    class _SkippedAgent(BaseAgent):
        domain = "teststub"

        def collect(self, scope: str = "all"):
            raise CollectorSkipped("unconfigured, for this test")

    with (
        patch("infra_brain.etl.base.get_session", _session_factory(eng)),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        caplog.at_level(logging.INFO, logger="infra_brain.etl.base"),
    ):
        agent = _SkippedAgent()
        agent.run(trigger_type="test")

    # At least one record from this run must carry domain="teststub" as a
    # structured field (LogRecord attribute), not merely inside the message text.
    assert any(getattr(r, "domain", None) == "teststub" for r in caplog.records), (
        "expected at least one log record with domain='teststub' set via extra={}"
    )


def test_run_finalize_exception_logs_domain_as_extra_field(caplog):
    """The finalize-commit-failure log (etl/base.py:357-362) is one of the two
    call sites explicitly named in #87 — it must carry domain via extra={}.

    Forces the finalize block's get_session() to fail on its SECOND call
    (the first call, inside run()'s opening block, must still succeed so a
    real CollectionRun row exists to attempt finalizing)."""
    eng = _engine()
    real_factory = _session_factory(eng)
    call_count = {"n": 0}

    @contextmanager
    def _flaky_get_session():
        call_count["n"] += 1
        # Call order in ETLConnector.run(): 1) create the in_progress row,
        # 2) the resource-write session (entered unconditionally even when
        # outcome.items is empty — confirmed against current etl/base.py),
        # 3) the finalize block's get_session() call. The plan's draft test
        # assumed the finalize call was #2; it is actually #3 for an agent
        # whose collect() returns [].
        if call_count["n"] == 3:  # the finalize block's get_session() call
            raise RuntimeError("db down during finalize")
        with real_factory() as s:
            yield s

    class _OkAgent(BaseAgent):
        domain = "teststub"

        def collect(self, scope: str = "all"):
            return []

    with (
        patch("infra_brain.etl.base.get_session", _flaky_get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        caplog.at_level(logging.ERROR, logger="infra_brain.etl.base"),
    ):
        agent = _OkAgent()
        agent.run(trigger_type="test")

    finalize_records = [r for r in caplog.records if "failed to finalize" in r.getMessage()]
    assert finalize_records, "expected the finalize-failure log line to fire"
    assert any(getattr(r, "domain", None) == "teststub" for r in finalize_records)
