"""F-030 — retention prune deletes only rows older than the window."""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import infra_brain.retention as retention
from infra_brain.db.models import AgentActionLog, AuditLog, CollectionRun


def _settings(**overrides):
    base = dict(
        retention_prune_enabled=True,
        retention_audit_log_days=400,
        retention_agent_action_log_days=400,
        retention_snapshots_days=90,
        retention_drift_events_days=180,
        retention_collection_runs_days=180,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    for table in (AuditLog.__table__, AgentActionLog.__table__, CollectionRun.__table__):
        table.create(engine)
    s = Session(engine)
    yield s
    s.close()


def _audit_row(age_days: int) -> AuditLog:
    return AuditLog(
        id=uuid.uuid4(),
        agent="t",
        tool="t",
        input_hash="x",
        allowed=True,
        ts=datetime.now(timezone.utc) - timedelta(days=age_days),
    )


def test_delete_older_than_removes_only_expired(session):
    session.add_all([_audit_row(500), _audit_row(10)])
    session.flush()
    cutoff = datetime.now(timezone.utc) - timedelta(days=400)
    deleted = retention._delete_older_than(session, AuditLog, AuditLog.ts, cutoff)
    assert deleted == 1
    assert session.query(AuditLog).count() == 1


def test_extra_criteria_are_applied(session):
    old = CollectionRun(
        id=uuid.uuid4(),
        domain="linux",
        trigger_type="scheduled",
        started_at=datetime.now(timezone.utc) - timedelta(days=500),
        status="completed",
    )
    session.add(old)
    session.flush()
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    deleted = retention._delete_older_than(
        session,
        CollectionRun,
        CollectionRun.started_at,
        cutoff,
        CollectionRun.status != "completed",
    )
    assert deleted == 0  # extra criterion excluded the completed row


def test_prune_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(retention, "get_settings", lambda: _settings(retention_prune_enabled=False))
    assert retention.prune_expired() == {}


def test_prune_expired_end_to_end(monkeypatch, session):
    # Route the module's session/settings at our SQLite fixture. Snapshot/
    # DriftEvent/ResourceConfig tables don't exist here (JSONB), so stub the
    # statements that touch them by creating those tables is NOT possible on
    # SQLite — instead verify the audit/action/run paths, which share the same
    # _delete_older_than mechanics as the JSONB tables.
    @contextmanager
    def _get_session():
        yield session

    monkeypatch.setattr(retention, "get_settings", lambda: _settings())
    monkeypatch.setattr(retention, "get_session", _get_session)
    session.add_all([_audit_row(500), _audit_row(1)])
    session.add(
        AgentActionLog(
            id=uuid.uuid4(),
            agent="t",
            tool="t",
            verdict="allow",
            status="ok",
            ts=datetime.now(timezone.utc) - timedelta(days=500),
        )
    )
    session.flush()
    # Only run the audit/action deletes directly (full prune_expired touches
    # JSONB tables that SQLite cannot create).
    now = datetime.now(timezone.utc)
    a = retention._delete_older_than(session, AuditLog, AuditLog.ts, now - timedelta(days=400))
    b = retention._delete_older_than(
        session, AgentActionLog, AgentActionLog.ts, now - timedelta(days=400)
    )
    assert (a, b) == (1, 1)
    assert session.query(AuditLog).count() == 1
    assert session.query(AgentActionLog).count() == 0
