"""F18 — the embedded APScheduler must be OFF by default in the API process.

The dedicated scheduler service (compose `scheduler` / k8s/scheduler.yaml,
replicas:1) is the only scheduler by default; running it inside every API
replica too would fan every unlocked job out once per replica (CLAUDE.md
constraint #9). The dead-man heartbeat CHECK loop must still run regardless of
the flag so a stalled dedicated scheduler is still detected.
"""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from infra_brain import main


def _patched_lifespan_deps():
    """Patch out every heavy lifespan side effect except the scheduler gate."""

    async def _fake_deadman():
        # Stand-in for the real check loop: proves the task was created.
        await asyncio.sleep(3600)

    return [
        patch("infra_brain.main.load_secrets_into_env", lambda: None),
        patch("infra_brain.main.init_tracing", lambda: None),
        patch("infra_brain.db.schema_check.assert_schema_current", lambda engine: None),
        patch("infra_brain.db.session.get_engine", lambda: MagicMock()),
        patch("infra_brain.main.check_freshness", lambda: []),
        patch("infra_brain.main.get_async_checkpointer", new=_make_async_none()),
        patch("infra_brain.callbacks.audit.start_audit_writer", lambda: None),
        patch("infra_brain.callbacks.audit.stop_audit_writer", new=_make_async_none()),
        patch("infra_brain.callbacks.observation.start_observation_writer", lambda: None),
        patch("infra_brain.callbacks.observation.stop_observation_writer", new=_make_async_none()),
        patch("infra_brain.main._scheduler_deadman_loop", new=_fake_deadman),
    ]


def _make_async_none():
    async def _f(*args, **kwargs):
        return None

    return _f


@pytest.mark.asyncio
async def test_embedded_scheduler_off_registers_no_jobs_but_deadman_runs():
    mock_sched = MagicMock()
    settings = SimpleNamespace(embedded_scheduler_enabled=False)

    with ExitStack() as stack:
        stack.enter_context(patch.object(main, "_scheduler", mock_sched))
        stack.enter_context(patch("infra_brain.config.get_settings", return_value=settings))
        for ctx in _patched_lifespan_deps():
            stack.enter_context(ctx)
        async with main.lifespan(MagicMock()):
            # Flag OFF: the embedded scheduler must never be started.
            mock_sched.start.assert_not_called()
            # But the dead-man heartbeat CHECK task must still be running
            # (it is created independently of the embedded scheduler).
            tasks = [t for t in asyncio.all_tasks() if not t.done()]
            assert any("_fake_deadman" in t.get_coro().__qualname__ for t in tasks), (
                "dead-man heartbeat check task was not created"
            )

    # And stop() is never called on a scheduler that was never started.
    mock_sched.stop.assert_not_called()


@pytest.mark.asyncio
async def test_embedded_scheduler_on_starts_and_stops():
    mock_sched = MagicMock()
    settings = SimpleNamespace(embedded_scheduler_enabled=True)

    with ExitStack() as stack:
        stack.enter_context(patch.object(main, "_scheduler", mock_sched))
        stack.enter_context(patch("infra_brain.config.get_settings", return_value=settings))
        for ctx in _patched_lifespan_deps():
            stack.enter_context(ctx)
        async with main.lifespan(MagicMock()):
            mock_sched.start.assert_called_once()

    # Started → must be drained on shutdown.
    mock_sched.stop.assert_called_once()
