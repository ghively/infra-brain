"""TRK-121: empirically prove ``graph._get_sync_loop()`` is safe under
concurrent dispatch from BOTH driver paths.

HANDOFF open question (blocking ``remediation_interrupt_enabled``): do
APScheduler (scheduled agent dispatch, via ``run_sweep_sync``) and FastAPI
(on-demand/webhook dispatch, via ``start_remediation_action_sync`` /
``resume_remediation_action``) contend when both drive
``graph._get_sync_loop``?

Answer, verified here: NO — there is no contention. Neither caller ever *drives*
the loop. Both submit coroutines to ONE module-singleton event loop that runs
``run_forever`` on ONE dedicated daemon thread, via
``asyncio.run_coroutine_threadsafe``. That is the canonical safe pattern: many
submitter threads, exactly one loop with exactly one driver thread. asyncio
serializes the submitted coroutines onto that single loop; the submitters only
block on a thread-safe ``concurrent.futures.Future``. Singleton creation itself
is guarded by a double-checked lock (``_SYNC_LOOP_LOCK``), so a concurrent
first-call race still starts only one loop.

These tests exercise that empirically rather than by inspection:
  * concurrent dispatch from both real sync entry points completes with no
    deadlock, no error, and every coroutine observed running on the SAME single
    loop instance (no cross-loop corruption);
  * a concurrent first-call race starts exactly one loop.

Conclusion: verified safe — no production code change required.
"""

from __future__ import annotations

import asyncio
import threading

from infra_brain import graph as graph_mod
from infra_brain import remediation_graph as rg_mod


def _stop_and_reset_singleton() -> None:
    """Stop the module-singleton loop (if any) and clear it, mirroring
    tests/test_sweep_graph.py's teardown. A stopped loop left assigned to
    ``_SYNC_LOOP`` would deadlock the next caller, so it must be cleared, not
    just stopped."""
    loop = graph_mod._sync_loop_for_testing()
    if loop is not None:
        loop.call_soon_threadsafe(loop.stop)
    graph_mod._SYNC_LOOP = None


def test_concurrent_dispatch_from_both_paths_shares_one_loop(monkeypatch):
    """APScheduler path (run_sweep_sync) and FastAPI path
    (start_remediation_action_sync) dispatched concurrently from many OS threads
    all route onto ONE shared loop, complete without deadlock, and never see a
    second loop — the empirical proof there is no contention."""
    running_loops: list = []
    lock = threading.Lock()

    async def _fake_sweep(**kwargs):
        # Record which loop actually runs this coroutine; a tiny await forces a
        # real scheduling round-trip so overlapping submissions genuinely
        # interleave on the loop.
        with lock:
            running_loops.append(asyncio.get_running_loop())
        await asyncio.sleep(0.02)
        return {"path": "sweep", "kwargs": kwargs}

    async def _fake_start(action_id):
        with lock:
            running_loops.append(asyncio.get_running_loop())
        await asyncio.sleep(0.02)
        return {"path": "start", "action_id": action_id}

    # Patch the coroutines the two real sync entry points wrap, so the test
    # drives the ACTUAL run_sweep_sync / start_remediation_action_sync machinery
    # (and thus the real _get_sync_loop + run_coroutine_threadsafe) without a DB
    # or checkpointer.
    monkeypatch.setattr(graph_mod, "run_sweep", _fake_sweep)
    monkeypatch.setattr(rg_mod, "_start", _fake_start)

    n_per_path = 6
    results: dict[str, object] = {}
    errors: list[BaseException] = []
    res_lock = threading.Lock()
    start_barrier = threading.Barrier(2 * n_per_path)

    def _scheduler_dispatch(i: int) -> None:
        try:
            start_barrier.wait()  # maximize overlap
            out = graph_mod.run_sweep_sync(domains=[f"d{i}"], run_reasoners=False)
            with res_lock:
                results[f"sched-{i}"] = out
        except BaseException as exc:  # pragma: no cover - surfaced via errors
            errors.append(exc)

    def _fastapi_dispatch(i: int) -> None:
        try:
            start_barrier.wait()
            out = rg_mod.start_remediation_action_sync(f"act-{i}")
            with res_lock:
                results[f"api-{i}"] = out
        except BaseException as exc:  # pragma: no cover - surfaced via errors
            errors.append(exc)

    threads: list[threading.Thread] = []
    for i in range(n_per_path):
        threads.append(threading.Thread(target=_scheduler_dispatch, args=(i,)))
        threads.append(threading.Thread(target=_fastapi_dispatch, args=(i,)))

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # 1. No deadlock: every dispatch thread finished.
        assert not any(t.is_alive() for t in threads), "a dispatch thread deadlocked"
        # 2. No cross-loop / corruption errors surfaced.
        assert not errors, f"dispatch raised in a worker thread: {errors!r}"
        # 3. Every dispatch from both paths returned a result.
        assert len(results) == 2 * n_per_path
        # 4. Every coroutine ran on the SAME single loop — the crux: both the
        #    scheduler and FastAPI paths share one loop, so the checkpointer
        #    pool is never handed a second loop.
        assert len(running_loops) == 2 * n_per_path
        assert len({id(loop) for loop in running_loops}) == 1
        # 5. That one loop is the module singleton, and it is the driver thread's.
        assert running_loops[0] is graph_mod._sync_loop_for_testing()
    finally:
        for t in threads:
            t.join(timeout=5)
        _stop_and_reset_singleton()


def test_sync_loop_singleton_created_once_under_concurrent_first_call():
    """The double-checked lock in _get_sync_loop must start exactly one loop
    even when many threads race to be the first caller (scheduler + webhook
    threads at startup)."""
    # Force a genuine first-call race: clear the singleton first.
    _stop_and_reset_singleton()

    observed: list = []
    obs_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def _race() -> None:
        barrier.wait()
        loop = graph_mod._get_sync_loop()
        with obs_lock:
            observed.append(loop)

    threads = [threading.Thread(target=_race) for _ in range(8)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not any(t.is_alive() for t in threads)
        assert len(observed) == 8
        # Exactly one loop despite 8 concurrent first-callers.
        assert len({id(loop) for loop in observed}) == 1
        assert observed[0].is_running()
    finally:
        _stop_and_reset_singleton()
