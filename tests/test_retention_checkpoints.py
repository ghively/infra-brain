"""F-030 / TRK-069 — checkpoint-row retention prunes stale, unprotected threads.

Verified schema (langgraph-checkpoint-postgres 3.1.0,
``.venv/Lib/site-packages/langgraph/checkpoint/postgres/base.py`` MIGRATIONS):

- ``checkpoints(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
  type, checkpoint JSONB, metadata JSONB)`` — PK (thread_id, checkpoint_ns,
  checkpoint_id). NO ``created_at`` column; the checkpoint's timestamp lives
  at ``checkpoint->>'ts'`` (ISO 8601 string — see
  ``langgraph.checkpoint.base.Checkpoint.ts``).
- ``checkpoint_blobs(thread_id, checkpoint_ns, channel, version, type,
  blob BYTEA)`` — PK (thread_id, checkpoint_ns, channel, version).
- ``checkpoint_writes(thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
  channel, type, blob BYTEA, task_path)`` — PK (thread_id, checkpoint_ns,
  checkpoint_id, task_id, idx).

The sqlite stand-in tables below mirror those column names/types exactly
(sqlite has no native JSONB, so ``checkpoint``/``metadata`` are stored as TEXT
containing JSON — ``json_extract`` in place of postgres's ``->>``, but the
test double's SQL under test uses ``->>`` only in the real prune_checkpoints
SQL path guarded by a dialect check; see retention.py).
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import infra_brain.retention as retention
from infra_brain.db.models import ProposedAction


def _settings(**overrides):
    base = dict(retention_checkpoints_days=30)
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE checkpoints (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL DEFAULT '',
                    checkpoint_id TEXT NOT NULL,
                    parent_checkpoint_id TEXT,
                    type TEXT,
                    checkpoint TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE checkpoint_blobs (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL,
                    version TEXT NOT NULL,
                    type TEXT NOT NULL,
                    blob BLOB,
                    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE checkpoint_writes (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL DEFAULT '',
                    checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    type TEXT,
                    blob BLOB NOT NULL,
                    task_path TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                )
                """
            )
        )
    ProposedAction.__table__.create(engine)
    s = Session(engine)
    yield s
    s.close()


def _insert_checkpoint(session, thread_id: str, age_days: int, ns: str = ""):
    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    cp_id = str(uuid.uuid4())
    session.execute(
        text(
            """
            INSERT INTO checkpoints
                (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)
            VALUES (:thread_id, :ns, :cp_id, :checkpoint, '{}')
            """
        ),
        {
            "thread_id": thread_id,
            "ns": ns,
            "cp_id": cp_id,
            "checkpoint": json.dumps({"v": 1, "id": cp_id, "ts": ts}),
        },
    )
    session.execute(
        text(
            """
            INSERT INTO checkpoint_blobs
                (thread_id, checkpoint_ns, channel, version, type, blob)
            VALUES (:thread_id, :ns, 'messages', '1', 'json', NULL)
            """
        ),
        {"thread_id": thread_id, "ns": ns},
    )
    session.execute(
        text(
            """
            INSERT INTO checkpoint_writes
                (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob)
            VALUES (:thread_id, :ns, :cp_id, 'task-1', 0, 'messages', 'json', x'00')
            """
        ),
        {"thread_id": thread_id, "ns": ns, "cp_id": cp_id},
    )
    session.commit()


def _counts(session, thread_id: str) -> dict[str, int]:
    return {
        table: session.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE thread_id = :t"),
            {"t": thread_id},
        ).scalar()
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
    }


def test_old_unprotected_thread_pruned_from_all_three_tables(session, monkeypatch):
    monkeypatch.setattr(retention, "get_settings", lambda: _settings())
    _insert_checkpoint(session, "thread-old", age_days=60)

    deleted = retention.prune_checkpoints(session)
    session.commit()

    assert deleted == 1
    assert _counts(session, "thread-old") == {
        "checkpoints": 0,
        "checkpoint_blobs": 0,
        "checkpoint_writes": 0,
    }


def test_young_thread_survives(session, monkeypatch):
    monkeypatch.setattr(retention, "get_settings", lambda: _settings())
    _insert_checkpoint(session, "thread-young", age_days=5)

    deleted = retention.prune_checkpoints(session)
    session.commit()

    assert deleted == 0
    assert _counts(session, "thread-young")["checkpoints"] == 1


def test_protected_thread_with_pending_proposed_action_survives(session, monkeypatch):
    monkeypatch.setattr(retention, "get_settings", lambda: _settings())
    _insert_checkpoint(session, "thread-protected", age_days=90)
    session.add(
        ProposedAction(
            id=uuid.uuid4(),
            agent="remediation",
            action_type="config_fix",
            target="host1",
            status="pending",
            thread_id="thread-protected",
        )
    )
    session.commit()

    deleted = retention.prune_checkpoints(session)
    session.commit()

    assert deleted == 0
    assert _counts(session, "thread-protected")["checkpoints"] == 1


def test_approved_status_also_protects_thread(session, monkeypatch):
    monkeypatch.setattr(retention, "get_settings", lambda: _settings())
    _insert_checkpoint(session, "thread-approved", age_days=90)
    session.add(
        ProposedAction(
            id=uuid.uuid4(),
            agent="remediation",
            action_type="config_fix",
            target="host1",
            status="approved",
            thread_id="thread-approved",
        )
    )
    session.commit()

    deleted = retention.prune_checkpoints(session)
    session.commit()

    assert deleted == 0
    assert _counts(session, "thread-approved")["checkpoints"] == 1


def test_rejected_status_does_not_protect_thread(session, monkeypatch):
    monkeypatch.setattr(retention, "get_settings", lambda: _settings())
    _insert_checkpoint(session, "thread-rejected", age_days=90)
    session.add(
        ProposedAction(
            id=uuid.uuid4(),
            agent="remediation",
            action_type="config_fix",
            target="host1",
            status="rejected",
            thread_id="thread-rejected",
        )
    )
    session.commit()

    deleted = retention.prune_checkpoints(session)
    session.commit()

    assert deleted == 1
    assert _counts(session, "thread-rejected")["checkpoints"] == 0


def test_missing_tables_skip_cleanly(monkeypatch):
    engine = create_engine("sqlite://")
    ProposedAction.__table__.create(engine)
    s = Session(engine)
    monkeypatch.setattr(retention, "get_settings", lambda: _settings())

    deleted = retention.prune_checkpoints(s)

    assert deleted == 0
    s.close()


def test_prune_expired_wires_in_checkpoint_pruning(monkeypatch):
    """prune_expired() must call prune_checkpoints() and surface the count."""
    calls = []

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            return SimpleNamespace(rowcount=0)

        def commit(self):
            pass

    monkeypatch.setattr(retention, "get_session", lambda: _FakeSession())
    monkeypatch.setattr(
        retention,
        "get_settings",
        lambda: SimpleNamespace(
            retention_prune_enabled=True,
            retention_audit_log_days=400,
            retention_agent_action_log_days=400,
            retention_snapshots_days=90,
            retention_drift_events_days=180,
            retention_collection_runs_days=180,
            retention_checkpoints_days=30,
        ),
    )

    def _fake_prune_checkpoints(session):
        calls.append(session)
        return 7

    monkeypatch.setattr(retention, "prune_checkpoints", _fake_prune_checkpoints)

    counts = retention.prune_expired()

    assert len(calls) == 1
    assert counts["checkpoints"] == 7
