"""The persisted CollectionRun.drift_count must match reality for detail-phase writers.

Regression test for a bug found during live post-deploy verification, not by any
static review: ``ETLConnector.run()`` recomputed ``result.drift_count`` after the
detail phase (correct — linux/windows write every DriftEvent row there) but only
updated the IN-MEMORY result. The persisted ``CollectionRun.drift_count`` column
kept its pre-detail value, which for those collectors is always 0.

Live evidence at the time of the fix: a linux run returned drift_count=14 and
wrote 14 drift_events rows, while its collection_runs row said 0. Historic rows
showed the same divergence (2026-08-02 12:00 UTC: column 0, actual 6). Run-list
readers show that column, so linux/windows drift was invisible there.

This asserts the DATABASE, not the returned object — the returned object was
already correct, and that is exactly what let the bug survive unnoticed.
"""

import uuid
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

from infra_brain.db.models import CollectionRun, DriftEvent, Resource
from infra_brain.etl.base import CollectOutcome, ETLConnector

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


class _DetailPhaseDriftWriter(ETLConnector):
    """Minimal stand-in for linux/windows: writes its DriftEvent rows in the
    DETAIL phase, i.e. after run() has already computed and stored drift_count."""

    domain = "test_detail_drift"
    _recompute_drift_after_details = True

    def __init__(self, get_session):
        super().__init__()
        self._gs = get_session

    def collect(self, scope: str = "all") -> CollectOutcome:
        return CollectOutcome(
            items=[{"name": "host-1", "type": "test_host", "source": "test"}]
        )

    def _detail_writers(self, scope, result):
        # run() invokes each writer with no arguments, so close over `result`.
        return [lambda: self._write_drift_in_detail_phase(result)]

    def _write_drift_in_detail_phase(self, result) -> int:
        with self._gs() as session:
            resource = (
                session.query(Resource).filter(Resource.domain == self.domain).first()
            )
            for i in range(3):
                session.add(
                    DriftEvent(
                        id=uuid.uuid4(),
                        resource_id=resource.id,
                        collection_run_id=result.run_id,
                        drift_type="new_listening_port",
                        field=f"port_{i}",
                        old_value=None,
                        new_value={"port": 8000 + i},
                    )
                )
            session.commit()
        return 3


def test_detail_phase_drift_is_persisted_not_just_returned():
    eng = _engine()
    get_session = _session_factory(eng)

    with (
        patch("infra_brain.etl.base.get_session", get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        result = _DetailPhaseDriftWriter(get_session).run(trigger_type="manual")

    assert result.drift_count == 3, "the returned result was already correct before the fix"

    with get_session() as session:
        row = session.get(CollectionRun, result.run_id)
        actual = (
            session.query(DriftEvent)
            .filter(DriftEvent.collection_run_id == result.run_id)
            .count()
        )
        assert actual == 3, "the detail writer should have written 3 DriftEvent rows"
        assert row.drift_count == actual, (
            f"persisted CollectionRun.drift_count={row.drift_count} disagrees with the "
            f"{actual} drift_events rows actually written for this run — the stored row "
            f"and the returned result must not disagree about what the run found"
        )
