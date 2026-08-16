"""Regression tests for CollectionRun.detail_rows_written (Commit 1 fix).

A collector that writes only detail rows (no generic Resource upserts from
collect()) must report a nonzero persisted count on its CollectionRun so that
collection-health monitors do not mark it dead.

The test uses a minimal concrete agent that:
  - returns an empty list from collect() (zero generic Resources)
  - writes 3 detail rows in a _write_details call that returns an int count

After run(), CollectionRun.detail_rows_written must equal 3 and
CollectionResult.detail_rows_written must equal 3.
"""

from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker

from infra_brain.agents.base import BaseAgent, CollectionResult
from infra_brain.db.models import CollectionRun


class DetailOnlyAgent(BaseAgent):
    """Writes zero generic Resources; returns 3 from its detail-write fn."""

    domain = "detail_only_test"

    def collect(self, scope: str = "all") -> list[dict]:
        # No generic Resource rows — all data lives in the detail phase.
        return []

    def run(self, trigger_type: str = "scheduled", scope: str = "all") -> CollectionResult:
        result = super().run(trigger_type=trigger_type, scope=scope)
        # Detail-write fn returns 3 rows written.
        self._write_details(result, lambda: 3)
        return result


def test_detail_only_agent_records_nonzero_count(engine):
    """A detail-only collector must have detail_rows_written > 0 on its CollectionRun."""
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
        agent = DetailOnlyAgent()
        result = agent.run(trigger_type="manual", scope="all")

    # Generic resource count is 0 — the collector wrote no Resource rows.
    assert result.resources_found == 0
    # But detail_rows_written must be 3.
    assert result.detail_rows_written == 3
    assert result.status == "completed"

    with Session(engine) as verify:
        run = verify.get(CollectionRun, result.run_id)
        assert run is not None
        assert run.resources_found == 0
        assert run.detail_rows_written == 3
        assert run.status == "completed"


def test_write_details_accumulates_across_multiple_phases(engine):
    """Multiple _write_details calls accumulate into a single total."""
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    class MultiPhaseAgent(BaseAgent):
        domain = "multi_phase_test"

        def collect(self, scope: str = "all") -> list[dict]:
            return []

        def run(self, trigger_type: str = "scheduled", scope: str = "all") -> CollectionResult:
            result = super().run(trigger_type=trigger_type, scope=scope)
            self._write_details(result, lambda: 5)
            self._write_details(result, lambda: 7)
            return result

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = MultiPhaseAgent()
        result = agent.run()

    assert result.detail_rows_written == 12

    with Session(engine) as verify:
        run = verify.get(CollectionRun, result.run_id)
        assert run.detail_rows_written == 12


def test_write_details_none_return_leaves_count_zero(engine):
    """A detail-write fn that returns None (old-style) does not affect the count."""
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    class OldStyleAgent(BaseAgent):
        domain = "old_style_test"

        def collect(self, scope: str = "all") -> list[dict]:
            return []

        def run(self, trigger_type: str = "scheduled", scope: str = "all") -> CollectionResult:
            result = super().run(trigger_type=trigger_type, scope=scope)
            self._write_details(result, lambda: None)
            return result

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = OldStyleAgent()
        result = agent.run()

    # Returns None → no count increment; still completed.
    assert result.detail_rows_written == 0
    assert result.status == "completed"
