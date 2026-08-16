"""Tests for main.py's OB-1 scheduler dead-man switch asyncio task.

This is an INDEPENDENT execution path from the APScheduler jobs in
scheduler.py — it must be able to detect and alert on a stale/missing
scheduler heartbeat regardless of what the scheduler itself is doing.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from infra_brain import main


@pytest.mark.asyncio
async def test_deadman_loop_alerts_and_delivers_on_stale_heartbeat():
    mock_agent = MagicMock()

    with (
        patch.object(main, "_DEADMAN_CHECK_INTERVAL_SECONDS", 0.01),
        patch(
            "infra_brain.callbacks.freshness.check_scheduler_deadman",
            return_value="scheduler: heartbeat stale — last seen 5:00:00 ago (max 2:00:00)",
        ),
        patch("infra_brain.agents.notification.NotificationAgent", return_value=mock_agent),
    ):
        task = asyncio.create_task(main._scheduler_deadman_loop())
        # Let at least one iteration run.
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    mock_agent.notify_ops_alerts.assert_called()
    call_args = mock_agent.notify_ops_alerts.call_args
    assert call_args.args[0] == "scheduler_deadman"
    assert "stale" in call_args.args[1][0]


@pytest.mark.asyncio
async def test_deadman_loop_does_not_deliver_when_heartbeat_is_healthy():
    mock_agent = MagicMock()

    with (
        patch.object(main, "_DEADMAN_CHECK_INTERVAL_SECONDS", 0.01),
        patch("infra_brain.callbacks.freshness.check_scheduler_deadman", return_value=None),
        patch("infra_brain.agents.notification.NotificationAgent", return_value=mock_agent),
    ):
        task = asyncio.create_task(main._scheduler_deadman_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    mock_agent.notify_ops_alerts.assert_not_called()


@pytest.mark.asyncio
async def test_deadman_loop_survives_check_exception():
    """A bug in check_scheduler_deadman() must not kill the loop — it should
    keep trying on the next interval instead of silently stopping forever."""
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        raise RuntimeError("db hiccup")

    with (
        patch.object(main, "_DEADMAN_CHECK_INTERVAL_SECONDS", 0.01),
        patch("infra_brain.callbacks.freshness.check_scheduler_deadman", side_effect=_flaky),
    ):
        task = asyncio.create_task(main._scheduler_deadman_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls["n"] >= 1, "loop must keep calling check_scheduler_deadman despite failures"


@pytest.mark.asyncio
async def test_deadman_loop_cancels_cleanly():
    """Cancelling the task (as lifespan teardown does) must not hang or raise
    anything other than CancelledError."""
    with patch.object(main, "_DEADMAN_CHECK_INTERVAL_SECONDS", 60):
        task = asyncio.create_task(main._scheduler_deadman_loop())
        await asyncio.sleep(0)  # let it start sleeping
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
