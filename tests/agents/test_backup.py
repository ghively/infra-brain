"""Tests for BackupAgent (GitLab issue #96) — success, unconfigured, exception."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from infra_brain.agents.backup import BackupAgent, _parse_dt
from infra_brain.db.models import BackupJob, Resource
from infra_brain.etl.base import CollectorSkipped


def _agent(**settings):
    """Build a BackupAgent without running BaseAgent.__init__ (no LLM/DB needed)."""
    agent = BackupAgent.__new__(BackupAgent)
    defaults = dict(
        backup_backend="",
        backup_api_url="",
        backup_api_key="",
        backup_ssl_verify=True,
        backup_overdue_days=2,
        backup_restore_test_overdue_days=90,
    )
    defaults.update(settings)
    agent.settings = SimpleNamespace(**defaults)
    agent.callbacks = []
    return agent


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_collect_raises_skipped_when_unconfigured():
    """No backend/API URL configured -> CollectorSkipped (not []) — matches
    net.py/vuln.py's "skipped" is distinct from "working-empty" convention."""
    with pytest.raises(CollectorSkipped):
        _agent().collect("all")


def test_collect_raises_skipped_when_api_url_missing():
    agent = _agent(backup_backend="veeam", backup_api_url="")
    with pytest.raises(CollectorSkipped):
        agent.collect("all")


def test_collect_success_flags_overdue_backup_and_restore_test():
    """Success case with a mocked backup-API response covering: a healthy
    job, a job with an overdue last backup, and a job with an overdue (never
    recorded) restore test."""
    now = datetime.now(UTC)
    healthy = {
        "job_name": "daily-vm-backup",
        "resource_name": "web01",
        "last_success_at": _iso(now - timedelta(hours=6)),
        "last_restore_test_at": _iso(now - timedelta(days=10)),
        "retention_days": 30,
        "status": "success",
    }
    overdue_backup = {
        "job_name": "daily-vm-backup",
        "resource_name": "db02",
        "last_success_at": _iso(now - timedelta(days=10)),
        "last_restore_test_at": _iso(now - timedelta(days=10)),
        "retention_days": 30,
        "status": "failed",
    }
    overdue_restore_test = {
        "job_name": "weekly-dr-test",
        "resource_name": "web01",
        "last_success_at": _iso(now - timedelta(hours=1)),
        "last_restore_test_at": None,
        "retention_days": 90,
        "status": "success",
    }

    agent = _agent(backup_backend="veeam", backup_api_url="https://veeam.example.com")
    with patch("infra_brain.agents.backup.backup_jobs_tool") as mock_tool:
        mock_tool.invoke.return_value = [healthy, overdue_backup, overdue_restore_test]
        outcome = agent.collect("all")

    assert outcome.errors == []
    assert len(outcome.items) == 3

    by_name = {item["name"]: item for item in outcome.items}

    healthy_item = by_name["web01:daily-vm-backup"]
    assert healthy_item["type"] == "backup_job"
    assert healthy_item["data"]["backup_overdue"] is False
    assert healthy_item["data"]["restore_test_overdue"] is False

    overdue_item = by_name["db02:daily-vm-backup"]
    assert overdue_item["data"]["backup_overdue"] is True

    overdue_restore_item = by_name["web01:weekly-dr-test"]
    assert overdue_restore_item["data"]["backup_overdue"] is False
    assert overdue_restore_item["data"]["restore_test_overdue"] is True

    # Cached for the relational detail-write phase.
    assert agent._last_jobs == outcome.items


def test_collect_never_passes_api_key_as_tool_argument():
    """Regression guard (lc-safety-reviewer CRITICAL finding): tool-call args
    are persisted verbatim into AuditLog/AgentActionLog, so backup_api_key
    must never appear in the dict passed to backup_jobs_tool.invoke() —
    the tool reads it from settings internally instead, same convention as
    loadbalancer.py/secrets_inventory.py."""
    agent = _agent(
        backup_backend="veeam",
        backup_api_url="https://veeam.example.com",
        backup_api_key="SUPER-SECRET-KEY",
    )
    with patch("infra_brain.agents.backup.backup_jobs_tool") as mock_tool:
        mock_tool.invoke.return_value = []
        agent.collect("all")

    call_args = mock_tool.invoke.call_args[0][0]
    assert "api_key" not in call_args
    assert "SUPER-SECRET-KEY" not in repr(call_args)


def test_collect_skips_jobs_missing_names():
    """A raw job record with no job_name/resource_name is skipped, not faked."""
    agent = _agent(backup_backend="bacula", backup_api_url="https://bacula.example.com")
    with patch("infra_brain.agents.backup.backup_jobs_tool") as mock_tool:
        mock_tool.invoke.return_value = [
            {"job_name": "", "resource_name": "", "last_success_at": None},
        ]
        outcome = agent.collect("all")
    assert outcome.items == []
    assert outcome.errors == []


def test_collect_empty_when_api_returns_no_jobs():
    """A working-but-empty poll returns a real (empty) CollectOutcome, not skipped."""
    agent = _agent(backup_backend="veeam", backup_api_url="https://veeam.example.com")
    with patch("infra_brain.agents.backup.backup_jobs_tool") as mock_tool:
        mock_tool.invoke.return_value = []
        outcome = agent.collect("all")
    assert outcome.items == []
    assert outcome.errors == []


def test_collect_handles_tool_exception():
    """A raised exception from the backup API tool is captured as an error,
    not propagated — matches the CollectOutcome partial/failed contract."""
    agent = _agent(backup_backend="veeam", backup_api_url="https://veeam.example.com")
    with patch("infra_brain.agents.backup.backup_jobs_tool") as mock_tool:
        mock_tool.invoke.side_effect = RuntimeError("connection refused")
        outcome = agent.collect("all")
    assert outcome.items == []
    assert len(outcome.errors) == 1
    assert "connection refused" in outcome.errors[0]
    assert agent._last_jobs == []


def test_write_backup_details_noop_when_no_items():
    """Idle-safety: no cached items -> the relational write phase is a no-op
    that touches no DB session (returns 0, does not raise)."""
    agent = _agent()
    assert agent._write_backup_details() == 0


def test_upsert_backup_job_bumps_collected_at_on_repeat_poll(db):
    """`collected_at` must be refreshed on every poll of the same job, not
    just frozen at first-insert — mirrors pki.py's `existing.collected_at =
    now` convention for the analogous column (finding: collected_at was
    omitted from the upsert row dict, so `_upsert_detail` never touched it
    on updates after the initial INSERT's model default).

    Uses two distinct, controlled "now" values (rather than relying on wall
    clock resolution between two calls) so the assertion is deterministic
    and would fail against the pre-fix code, which left `collected_at`
    frozen at its first-insert value on every subsequent poll.
    """
    agent = _agent(backup_backend="veeam", backup_api_url="https://veeam.example.com")

    resource = Resource(
        domain="backup",
        type="backup_job",
        name="web01:daily-vm-backup",
        source="BackupAgent",
    )
    db.add(resource)
    db.flush()

    item = {
        "name": "web01:daily-vm-backup",
        "data": {
            "backend": "veeam",
            "job_name": "daily-vm-backup",
            "resource_name": "web01",
            "last_success_at": _iso(datetime.now(UTC) - timedelta(hours=6)),
            "last_restore_test_at": None,
            "retention_days": 30,
            "status": "success",
        },
    }

    first_poll_at = datetime(2026, 1, 1, tzinfo=UTC)
    second_poll_at = datetime(2026, 1, 2, tzinfo=UTC)

    with patch("infra_brain.agents.backup.datetime") as mock_dt:
        mock_dt.now.return_value = first_poll_at
        mock_dt.fromisoformat = datetime.fromisoformat
        agent._upsert_backup_job(db, item)
    db.flush()
    row = db.query(BackupJob).filter_by(resource_id=resource.id).one()
    # SQLite has no native tz-aware datetime type, so a round trip through
    # it drops tzinfo — compare as naive UTC, which is all this sqlite test
    # DB can round-trip (real Postgres preserves tzinfo).
    assert row.collected_at.replace(tzinfo=UTC) == first_poll_at

    # Simulate a later poll of the SAME job (second call).
    with patch("infra_brain.agents.backup.datetime") as mock_dt:
        mock_dt.now.return_value = second_poll_at
        mock_dt.fromisoformat = datetime.fromisoformat
        agent._upsert_backup_job(db, item)
    db.flush()
    db.refresh(row)

    assert row.collected_at.replace(tzinfo=UTC) == second_poll_at
    # Still exactly one row for this natural key — this is an update, not a
    # duplicate insert.
    assert db.query(BackupJob).filter_by(resource_id=resource.id).count() == 1


def test_parse_dt_handles_missing_and_bad_values():
    assert _parse_dt(None) is None
    assert _parse_dt("") is None
    assert _parse_dt("not-a-date") is None
    dt = _parse_dt("2026-01-01T00:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
