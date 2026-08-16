"""F20 — reaper for orphaned in_progress collection_runs.

A process death (OOM/SIGKILL) leaves a CollectionRun stuck in
status="in_progress" forever. reap_stale_collection_runs() marks runs older
than settings.collection_run_stale_after_hours as failed; fresh in_progress
runs and terminal runs are untouched.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import infra_brain.scheduler as scheduler
from infra_brain.db.models import CollectionRun


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    CollectionRun.__table__.create(engine)
    s = Session(engine)
    yield s
    s.close()


def _run(session, status: str, age_hours: float) -> uuid.UUID:
    run = CollectionRun(
        domain="linux",
        trigger_type="scheduled",
        status=status,
        started_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run.id


@pytest.fixture()
def patched(session, monkeypatch):
    @contextmanager
    def _get_session():
        yield session

    monkeypatch.setattr("infra_brain.db.session.get_session", _get_session)
    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: SimpleNamespace(collection_run_stale_after_hours=6),
    )


def test_stale_in_progress_row_is_reaped(session, patched):
    run_id = _run(session, "in_progress", age_hours=9)

    reaped = scheduler.reap_stale_collection_runs()

    assert reaped == 1
    row = session.get(CollectionRun, run_id)
    assert row.status == "failed"
    assert row.finished_at is not None
    assert "reaped" in (row.error_message or "")


def test_fresh_in_progress_row_is_untouched(session, patched):
    run_id = _run(session, "in_progress", age_hours=1)

    reaped = scheduler.reap_stale_collection_runs()

    assert reaped == 0
    assert session.get(CollectionRun, run_id).status == "in_progress"


def test_terminal_rows_untouched(session, patched):
    completed = _run(session, "completed", age_hours=48)
    failed = _run(session, "failed", age_hours=48)

    reaped = scheduler.reap_stale_collection_runs()

    assert reaped == 0
    assert session.get(CollectionRun, completed).status == "completed"
    assert session.get(CollectionRun, failed).status == "failed"


def test_reaper_runs_from_collection_health_job(session, patched, monkeypatch):
    """The hourly _collection_health_job must invoke the reaper."""
    run_id = _run(session, "in_progress", age_hours=12)

    monkeypatch.setattr("infra_brain.callbacks.freshness.record_scheduler_heartbeat", lambda: None)
    monkeypatch.setattr("infra_brain.callbacks.freshness.check_collection_health", lambda: [])
    monkeypatch.setattr("infra_brain.callbacks.anomaly.check_agent_anomalies", lambda: [])

    scheduler._collection_health_job()

    assert session.get(CollectionRun, run_id).status == "failed"
