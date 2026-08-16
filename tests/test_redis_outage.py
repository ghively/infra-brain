"""Tests for the shared Redis-outage CollectionRun recorder (#89).

Without this, scheduler.py's _make_job and graph.py's collector node both
skip the domain entirely on LockBackendUnavailable — logged loudly (F-029)
but with NO CollectionRun row written, so check_collection_health()'s hourly
alert batch cannot catch it via the "latest run failed" condition; it can
only catch it once the domain's own staleness window eventually elapses,
which for a daily/weekly-cadence domain could be many hours to a day+.
"""

import uuid
from unittest.mock import MagicMock, patch

from infra_brain.redis_outage import record_redis_outage_run


def test_writes_a_failed_collection_run_row():
    mock_session = MagicMock()
    with patch("infra_brain.redis_outage.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        record_redis_outage_run("linux")

    mock_session.add.assert_called_once()
    row = mock_session.add.call_args[0][0]
    assert row.domain == "linux"
    assert row.status == "failed"
    assert row.error_message == "redis lock backend unavailable"
    assert row.finished_at is not None
    mock_session.commit.assert_called_once()


def test_writes_sweep_id_when_provided():
    mock_session = MagicMock()
    sweep_id = str(uuid.uuid4())
    with patch("infra_brain.redis_outage.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        record_redis_outage_run("linux", sweep_id=sweep_id)

    row = mock_session.add.call_args[0][0]
    assert str(row.sweep_id) == sweep_id


def test_db_failure_while_recording_outage_is_swallowed_not_raised(caplog):
    """Recording the outage is itself best-effort — a DB blip here must not
    raise into the caller (which is already on the degraded Redis-down path;
    the hourly stale-run reaper / next successful run are the backstops)."""
    import logging

    with (
        patch("infra_brain.redis_outage.get_session", side_effect=RuntimeError("db also down")),
        caplog.at_level(logging.ERROR, logger="infra_brain.redis_outage"),
    ):
        record_redis_outage_run("linux")  # must not raise

    assert any("db also down" in r.getMessage() or "failed to record" in r.getMessage() for r in caplog.records)
