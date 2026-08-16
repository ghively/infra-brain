"""Direct coverage for ``resume_remediation_action_sync`` — the sync bridge.

The MCP ``approve_proposal``/``reject_proposal`` tools are plain ``def``
functions, so this bridge is the ONLY path by which a human decision made over
MCP resumes a parked remediation graph. Every other test in the suite either
exercises the async sibling or monkeypatches this function away, so its own
contract — non-fatal on timeout, non-fatal on error, and never a second event
loop — was entirely unverified.

The three guarantees pinned here:

1. ``FuturesTimeoutError`` (the 10s bound) is swallowed and returns False, so
   the ``_execute_approved()`` poll backstops it. The DB row is already
   flipped by the caller, so this must never propagate.
2. Any other exception from the coroutine is likewise non-fatal — same
   "defers to poll" semantics the async version documents.
3. Single-loop identity: the bridge reuses ``graph._get_sync_loop()``'s ONE
   module-singleton loop and never creates a second one, which is what keeps
   the async checkpointer pool bound to a single loop.

Plus the mechanical event-loop guard: calling it from inside a running loop is
a programming error and must raise, not silently block that loop for 10s.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FuturesTimeoutError
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from infra_brain import graph as graph_mod
from infra_brain import remediation_graph as rg


@pytest.fixture
def action():
    """A detached snapshot in the shape the bridge expects, on the CURRENT
    graph version so ``_resume_thread_id`` returns a thread id."""
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        thread_id="remediation:11111111-1111-1111-1111-111111111111",
        graph_version=rg.GRAPH_VERSION,
    )


@pytest.fixture
def interrupt_enabled():
    """The bridge no-ops unless remediation_interrupt_enabled is on.

    ``_resume`` is stubbed with a plain (non-async) MagicMock: every test here
    mocks out ``run_coroutine_threadsafe``, so a real coroutine object would be
    built and then never awaited, producing RuntimeWarning noise that has
    nothing to do with what is under test.
    """
    with (
        patch.object(rg, "get_settings") as gs,
        patch.object(rg, "_resume", MagicMock(name="_resume")),
    ):
        gs.return_value = SimpleNamespace(remediation_interrupt_enabled=True)
        yield


# ---------------------------------------------------------------------------
# 1. Timeout path — non-fatal
# ---------------------------------------------------------------------------


def test_timeout_is_non_fatal_and_returns_false(action, interrupt_enabled):
    """A future that never resolves inside 10s must NOT raise: the graph keeps
    running on the dedicated loop and the poll backstops execution."""
    never_resolves = MagicMock()
    never_resolves.result.side_effect = FuturesTimeoutError()

    with (
        patch.object(rg.asyncio, "run_coroutine_threadsafe", return_value=never_resolves),
        patch.object(rg, "_get_sync_loop", return_value=MagicMock()),
    ):
        result = rg.resume_remediation_action_sync(action, approved=True)

    assert result is False
    # the 10s bound is the one actually applied
    assert never_resolves.result.call_args.kwargs["timeout"] == 10.0


# ---------------------------------------------------------------------------
# 2. Broad-except path — non-fatal
# ---------------------------------------------------------------------------


def test_coroutine_error_is_non_fatal_and_returns_false(action, interrupt_enabled):
    """Same "defers to poll" semantics as the async version: a resume failure
    never propagates to the caller that already flipped the DB row."""
    boom = MagicMock()
    boom.result.side_effect = RuntimeError("checkpointer exploded")

    with (
        patch.object(rg.asyncio, "run_coroutine_threadsafe", return_value=boom),
        patch.object(rg, "_get_sync_loop", return_value=MagicMock()),
    ):
        result = rg.resume_remediation_action_sync(action, approved=True)

    assert result is False


def test_scheduling_error_is_non_fatal(action, interrupt_enabled):
    """Failing before the future even exists is equally non-fatal."""
    with (
        patch.object(
            rg.asyncio, "run_coroutine_threadsafe", side_effect=RuntimeError("loop is closed")
        ),
        patch.object(rg, "_get_sync_loop", return_value=MagicMock()),
    ):
        assert rg.resume_remediation_action_sync(action, approved=False) is False


def test_successful_resume_returns_true(action, interrupt_enabled):
    ok = MagicMock()
    ok.result.return_value = {"executed": True}

    with (
        patch.object(rg.asyncio, "run_coroutine_threadsafe", return_value=ok),
        patch.object(rg, "_get_sync_loop", return_value=MagicMock()),
    ):
        assert rg.resume_remediation_action_sync(action, approved=True) is True


def test_flag_off_short_circuits_without_touching_the_loop(action):
    with patch.object(rg, "get_settings") as gs:
        gs.return_value = SimpleNamespace(remediation_interrupt_enabled=False)
        with patch.object(rg, "_get_sync_loop") as loop:
            assert rg.resume_remediation_action_sync(action, approved=True) is False
        loop.assert_not_called()


def test_foreign_graph_version_is_left_to_the_poll(interrupt_enabled):
    stale = SimpleNamespace(id="x", thread_id="t", graph_version="remediation-v0")
    with patch.object(rg, "_get_sync_loop") as loop:
        assert rg.resume_remediation_action_sync(stale, approved=True) is False
    loop.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Single-loop identity
# ---------------------------------------------------------------------------


def test_get_sync_loop_returns_one_stable_loop_instance():
    """The bridge's whole reason for existing: ONE loop, so the async
    checkpointer pool never sees a second one."""
    first = graph_mod._get_sync_loop()
    assert first is graph_mod._get_sync_loop()
    assert first is graph_mod._get_sync_loop()
    assert first.is_running()
    # remediation_graph imports the very same singleton accessor
    assert rg._get_sync_loop is graph_mod._get_sync_loop


def test_bridge_schedules_onto_the_shared_loop_and_creates_no_second_loop(
    action, interrupt_enabled
):
    expected_loop = graph_mod._get_sync_loop()
    ok = MagicMock()
    ok.result.return_value = {}

    with (
        patch.object(rg.asyncio, "run_coroutine_threadsafe", return_value=ok) as sched,
        patch.object(rg.asyncio, "new_event_loop") as new_loop,
    ):
        rg.resume_remediation_action_sync(action, approved=True)

    # scheduled onto the existing singleton, and no new loop was constructed
    assert sched.call_args.args[1] is expected_loop
    new_loop.assert_not_called()
    assert graph_mod._get_sync_loop() is expected_loop


# ---------------------------------------------------------------------------
# Event-loop misuse guard (INFO #6)
# ---------------------------------------------------------------------------


def test_raises_when_called_from_inside_a_running_event_loop(action, interrupt_enabled):
    """Blocking the caller's loop for up to 10s is a programming error, and the
    guard must sit ABOVE the broad except so it is not swallowed into a benign
    False (which would silently look like "resume failed, poll will retry")."""

    async def _from_a_loop():
        return rg.resume_remediation_action_sync(action, approved=True)

    with pytest.raises(RuntimeError, match="must not be called from within an event loop"):
        asyncio.run(_from_a_loop())


def test_guard_fires_before_any_scheduling_work(action, interrupt_enabled):
    async def _from_a_loop():
        return rg.resume_remediation_action_sync(action, approved=True)

    with patch.object(rg.asyncio, "run_coroutine_threadsafe") as sched:
        with pytest.raises(RuntimeError):
            asyncio.run(_from_a_loop())
    sched.assert_not_called()
