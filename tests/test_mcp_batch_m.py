"""Batch M MCP tool — backup / DR-drill posture (GitLab #96).

Covers get_backup_status: empty backup_jobs returns an empty list (not an
error, since BackupAgent is dormant/unconfigured by default -- same pattern
as vSphere), and days_overdue filtering correctly identifies an overdue job
(including the NULL-last_success_at "never backed up" case, mirroring
get_eol_status's NULL-inclusive convention -- see TestGetEolStatusNullDate
in test_mcp_server.py).
"""

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from unittest.mock import patch

from infra_brain import mcp_server
from infra_brain.db.models import BackupJob, Resource

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextlib.contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.mcp_server.get_session", _get_session):
        yield engine


def _seed_job(engine, resource_name, backend, job_name, last_success_at, **extra):
    with Session(engine) as s:
        r = Resource(domain="backup", type="backup_job", name=resource_name, source="BackupAgent")
        s.add(r)
        s.flush()
        s.add(
            BackupJob(
                resource_id=r.id,
                backend=backend,
                job_name=job_name,
                last_success_at=last_success_at,
                **extra,
            )
        )
        s.commit()


def test_get_backup_status_empty_table_returns_empty_list(patched_session):
    """The backup_jobs table is expected to be empty until a real backup
    backend is configured (BackupAgent.collect() raises CollectorSkipped
    otherwise) -- that must read as an empty list, never an error."""
    assert mcp_server.get_backup_status() == []


def test_get_backup_status_unfiltered_returns_all_jobs(patched_session):
    now = datetime.now(UTC)
    _seed_job(patched_session, "db-1", "veeam", "nightly-vm", now - timedelta(days=1))
    _seed_job(patched_session, "db-2", "bacula", "db-dump", now - timedelta(days=10))
    rows = mcp_server.get_backup_status()
    assert len(rows) == 2
    assert {r["job_name"] for r in rows} == {"nightly-vm", "db-dump"}
    # resource_name is joined in from Resource, not a backup_jobs column.
    assert {r["resource_name"] for r in rows} == {"db-1", "db-2"}


def test_get_backup_status_filters_by_days_overdue(patched_session):
    now = datetime.now(UTC)
    # Fresh — succeeded 1 day ago, well inside a 5-day overdue window.
    _seed_job(patched_session, "fresh-host", "veeam", "nightly-vm", now - timedelta(days=1))
    # Overdue — succeeded 10 days ago, outside a 5-day window.
    _seed_job(patched_session, "stale-host", "veeam", "nightly-vm", now - timedelta(days=10))
    # Never succeeded — NULL last_success_at must be included as overdue,
    # not silently dropped by the filter (GitLab #186 lesson, same fix as
    # get_eol_status's days_until_eol NULL-inclusive filter).
    _seed_job(patched_session, "never-backed-up", "veeam", "nightly-vm", None)

    rows = mcp_server.get_backup_status(days_overdue=5)
    names = {r["resource_name"] for r in rows}
    assert names == {"stale-host", "never-backed-up"}
    assert "fresh-host" not in names


def test_get_backup_status_computes_backup_overdue_flag(patched_session):
    """backup_overdue is a computed field (not a stored column), using
    config.py's backup_overdue_days (2) default threshold -- same freshness
    fact BackupAgent.collect() derives at collection time."""
    now = datetime.now(UTC)
    _seed_job(patched_session, "fresh-host", "veeam", "nightly-vm", now - timedelta(hours=1))
    _seed_job(patched_session, "stale-host", "veeam", "nightly-vm", now - timedelta(days=5))

    rows = {r["resource_name"]: r for r in mcp_server.get_backup_status()}
    assert rows["fresh-host"]["backup_overdue"] is False
    assert rows["stale-host"]["backup_overdue"] is True
    # No restore test ever recorded -> restore_test_overdue must be True.
    assert rows["fresh-host"]["restore_test_overdue"] is True


def test_get_backup_status_respects_limit(patched_session):
    now = datetime.now(UTC)
    for i in range(5):
        _seed_job(patched_session, f"host-{i}", "veeam", "nightly-vm", now - timedelta(days=i))
    rows = mcp_server.get_backup_status(limit=2)
    assert len(rows) == 2
