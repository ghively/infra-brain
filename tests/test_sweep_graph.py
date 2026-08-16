"""Sweep graph core tests (Phase 2 Task 2).

House pattern (tests/agents/test_supervisor.py): patch
infra_brain.supervisor.AGENT_REGISTRY with a plain dict of MagicMock classes.
The graph builds its topology from etl.spec.sweep_members(), which reads the
patched registry, so a fake COLLECTOR spec appears as its own Send-target node.
"""

import threading
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import RetryPolicy

import infra_brain.graph as graph_mod
from infra_brain.dedup import LockBackendUnavailable
from infra_brain.etl.base import RUN_STATUS_RETRY_EXHAUSTED
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.graph import (
    build_sweep_graph,
    require_postgres_checkpointer,
    run_sweep,
    run_sweep_sync,
)

_FAST_RETRY = RetryPolicy(
    max_attempts=2,
    initial_interval=0.01,
    max_interval=0.02,
    jitter=False,
    retry_on=graph_mod._default_retry_on,
)

_META_KEYS = {"sweep_id", "domains", "run_reasoners", "trigger_type", "runs", "statuses"}


@pytest.fixture(autouse=True)
def _reset_graph_module():
    graph_mod._PRODUCTION_GRAPH = None
    graph_mod._PRODUCTION_CHECKPOINTER = None
    graph_mod._ATTEMPTS.clear()
    yield
    graph_mod._PRODUCTION_GRAPH = None
    graph_mod._PRODUCTION_CHECKPOINTER = None
    graph_mod._ATTEMPTS.clear()


def _fake_agent(domain: str, tier: Tier, run_side_effect=None) -> MagicMock:
    cls = MagicMock(name=f"{domain}_agent_cls")
    cls.spec = AgentSpec(domain=domain, tier=tier, schedule=None, max_staleness=None)
    instance = cls.return_value
    if run_side_effect is not None:
        instance.run.side_effect = run_side_effect
    else:
        result = MagicMock()
        result.run_id = uuid.uuid4()
        result.status = "completed"
        instance.run.return_value = result
    return cls


@contextmanager
def sweep_env(registry: dict, disabled: str = "", paused: set[str] | None = None):
    """Patch the sweep graph's environment.

    ``paused`` stands in for live ``dispatchable__<domain>=false`` RuntimeConfig
    rows (runtime_flags.paused_domains). It is patched unconditionally — even
    when empty — so no test in this module touches a real database for the
    override read, and ``record_paused_run`` is stubbed for the same reason.
    """
    settings = MagicMock()
    settings.collection_disabled_domains = disabled
    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", registry),
        patch("infra_brain.graph.get_settings", return_value=settings),
        patch("infra_brain.graph.paused_domains", return_value=set(paused or ())),
        patch("infra_brain.graph.record_paused_run") as record_paused,
        patch("infra_brain.dedup.try_acquire", return_value="tok") as acquire,
        patch("infra_brain.dedup.release") as release,
    ):
        yield acquire, release, record_paused


def _state(**overrides) -> dict:
    base = {
        "sweep_id": str(uuid.uuid4()),
        "domains": [],
        "run_reasoners": True,
        "trigger_type": "sweep",
        "runs": {},
        "statuses": {},
    }
    base.update(overrides)
    return base


async def _invoke(graph, state):
    return await graph.ainvoke(state, config={"configurable": {"thread_id": state["sweep_id"]}})


# --------------------------------------------------------------------------- #
# Topology                                                                     #
# --------------------------------------------------------------------------- #


def test_topology_is_registry_derived_and_phase3_deferred_absent():
    registry = {
        "fakecol": _fake_agent("fakecol", Tier.COLLECTOR),
        "alpha": _fake_agent("alpha", Tier.COLLECTOR),
        "recon": _fake_agent("recon", Tier.RECONCILER),
        "drift": _fake_agent("drift", Tier.REASONER),
        "notification": _fake_agent("notification", Tier.REASONER),
        "remediation": _fake_agent("remediation", Tier.REASONER),
        "drift_learning": _fake_agent("drift_learning", Tier.REASONER),
    }
    with sweep_env(registry):
        graph = build_sweep_graph(checkpointer=MemorySaver())
    nodes = set(graph.nodes)
    # a fake COLLECTOR spec appears as its own Send-target node
    assert {"fakecol", "alpha", "recon", "drift", "notification"} <= nodes
    assert {"dispatch", "join"} <= nodes
    # Phase-3-deferred reasoners must be ABSENT as nodes
    assert "remediation" not in nodes
    assert "drift_learning" not in nodes


def test_real_registry_topology():
    """Against the real AGENT_REGISTRY: the live collectors, 3 reconcilers, and
    the 5 sweep reasoners — remediation/drift_learning excluded.

    net is disabled (dispatchable=False, POC 2026-07-15) and must therefore NOT
    appear as a sweep-graph node. 2026-08-12: the enterprise-remnant collectors
    (cloud, k8s, windows, vsphere, vuln, octopus, identity) are retired, which
    drops them out of sweep_members() and so out of the topology too — the
    graph derives every node from that function, so they are never added and
    then filtered, they are simply not there.
    """
    graph = build_sweep_graph(checkpointer=MemorySaver())
    nodes = set(graph.nodes)
    collectors = {"cicd", "iac", "linux", "netdiscovery"}
    assert collectors <= nodes
    # dispatchable=False: net (POC). retired: the enterprise remnants.
    assert {"net"}.isdisjoint(nodes)
    assert {
        "cloud", "k8s", "windows", "vsphere", "vuln", "octopus", "identity",
    }.isdisjoint(nodes)  # fmt: skip
    assert {"host_reconcile", "inventory_reconcile", "graph_maintenance"} <= nodes
    # eol reclassified COLLECTOR -> REASONER (2026-07-15): it derives+materializes
    # from collected inventory rather than enumerating an external fleet.
    assert {"drift", "notification", "rootcause", "vuln_triage", "compliance", "eol"} <= nodes
    assert "remediation" not in nodes
    assert "drift_learning" not in nodes


def test_no_arg_build_is_cached_and_explicit_checkpointer_bypasses_cache():
    first = build_sweep_graph()
    second = build_sweep_graph()
    assert first is second
    bypass = build_sweep_graph(checkpointer=MemorySaver())
    assert bypass is not first
    assert graph_mod._PRODUCTION_GRAPH is first


# --------------------------------------------------------------------------- #
# Execution semantics                                                          #
# --------------------------------------------------------------------------- #


async def test_kill_one_collector_join_still_fires():
    registry = {
        "alpha": _fake_agent("alpha", Tier.COLLECTOR, run_side_effect=OSError("conn refused")),
        "beta": _fake_agent("beta", Tier.COLLECTOR),
        "recon": _fake_agent("recon", Tier.RECONCILER),
    }
    with sweep_env(registry), patch.object(graph_mod, "_DEFAULT_RETRY", _FAST_RETRY):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state(run_reasoners=False))
    assert final["statuses"]["alpha"] == RUN_STATUS_RETRY_EXHAUSTED
    assert final["statuses"]["beta"] == "completed"
    # the defer=True join fired despite alpha's terminal failure: recon ran
    assert final["statuses"]["recon"] == "completed"
    # retry actually happened: max_attempts=2 -> run() called twice for alpha
    assert registry["alpha"].return_value.run.call_count == 2
    assert "alpha" not in final["runs"]
    assert "beta" in final["runs"]


async def test_kill_one_collector_excludes_it_from_drift_succeeded_domains():
    """Task 3 DoD: a retry-exhausted collector's domain must not appear in the
    succeeded_domains set passed to detect_state_drift — its stale resources
    must not be scanned for "disappeared" drift.
    """
    registry = {
        "alpha": _fake_agent("alpha", Tier.COLLECTOR, run_side_effect=OSError("conn refused")),
        "beta": _fake_agent("beta", Tier.COLLECTOR),
        "drift": _fake_agent("drift", Tier.REASONER),
        "notification": _fake_agent("notification", Tier.REASONER),
    }
    with sweep_env(registry), patch.object(graph_mod, "_DEFAULT_RETRY", _FAST_RETRY):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state())
    assert final["statuses"]["alpha"] == RUN_STATUS_RETRY_EXHAUSTED
    assert final["statuses"]["beta"] == "completed"
    drift_instance = registry["drift"].return_value
    drift_instance.detect_state_drift.assert_called_once()
    _, kwargs = drift_instance.detect_state_drift.call_args
    assert kwargs["succeeded_domains"] == {"beta"}
    assert "alpha" not in kwargs["succeeded_domains"]


async def test_non_retryable_failure_is_failed_without_retry():
    registry = {
        "alpha": _fake_agent("alpha", Tier.COLLECTOR, run_side_effect=ValueError("boom")),
    }
    with sweep_env(registry), patch.object(graph_mod, "_DEFAULT_RETRY", _FAST_RETRY):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state(run_reasoners=False))
    assert final["statuses"]["alpha"] == "failed"
    assert registry["alpha"].return_value.run.call_count == 1


async def test_disabled_domain_never_dispatched_and_recorded_skipped():
    registry = {
        "alpha": _fake_agent("alpha", Tier.COLLECTOR),
        "beta": _fake_agent("beta", Tier.COLLECTOR),
    }
    with sweep_env(registry, disabled="alpha"):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state(run_reasoners=False))
    assert final["statuses"]["alpha"] == "skipped"
    assert final["statuses"]["beta"] == "completed"
    registry["alpha"].assert_not_called()  # never instantiated, never Sent


async def test_paused_domain_never_dispatched_and_recorded_skipped():
    """A live dispatchable__<domain>=false override must be honored HERE too.

    The sweep graph fans collectors out itself and never calls
    supervisor.dispatch(), so while the override lookup lived only in
    supervisor.py the dashboard's Disable Collector toggle was a silent no-op
    whenever sweep_graph_enabled was on.
    """
    registry = {
        "alpha": _fake_agent("alpha", Tier.COLLECTOR),
        "beta": _fake_agent("beta", Tier.COLLECTOR),
    }
    with sweep_env(registry, paused={"alpha"}) as (_a, _r, record_paused):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state(run_reasoners=False))
    assert final["statuses"]["alpha"] == "skipped"
    assert final["statuses"]["beta"] == "completed"
    registry["alpha"].assert_not_called()  # never instantiated, never Sent
    # and the pause is persisted so the roster/dashboard can see it
    assert record_paused.call_count == 1
    assert record_paused.call_args.args[0] == "alpha"


async def test_paused_domain_writes_no_run_row_when_also_statically_disabled():
    """collection_disabled_domains keeps its existing write-nothing behavior."""
    registry = {
        "alpha": _fake_agent("alpha", Tier.COLLECTOR),
        "beta": _fake_agent("beta", Tier.COLLECTOR),
    }
    with sweep_env(registry, disabled="alpha", paused={"alpha"}) as (_a, _r, record_paused):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state(run_reasoners=False))
    assert final["statuses"]["alpha"] == "skipped"
    record_paused.assert_not_called()


async def test_unpaused_sweep_writes_no_paused_run_row():
    registry = {"alpha": _fake_agent("alpha", Tier.COLLECTOR)}
    with sweep_env(registry) as (_a, _r, record_paused):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state(run_reasoners=False))
    assert final["statuses"]["alpha"] == "completed"
    record_paused.assert_not_called()


async def test_run_reasoners_false_touches_no_reasoner():
    registry = {
        "alpha": _fake_agent("alpha", Tier.COLLECTOR),
        "drift": _fake_agent("drift", Tier.REASONER),
        "notification": _fake_agent("notification", Tier.REASONER),
        "rootcause": _fake_agent("rootcause", Tier.REASONER),
    }
    with sweep_env(registry):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state(run_reasoners=False))
    assert final["statuses"] == {"alpha": "completed"}
    registry["drift"].assert_not_called()
    registry["notification"].assert_not_called()
    registry["rootcause"].assert_not_called()


async def test_reasoner_tier_runs_with_sweep_kwargs():
    registry = {
        "alpha": _fake_agent("alpha", Tier.COLLECTOR),
        "drift": _fake_agent("drift", Tier.REASONER),
        "notification": _fake_agent("notification", Tier.REASONER),
        "rootcause": _fake_agent("rootcause", Tier.REASONER),
        "vuln_triage": _fake_agent("vuln_triage", Tier.REASONER),
        "compliance": _fake_agent("compliance", Tier.REASONER),
    }
    with sweep_env(registry):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        state = _state()
        final = await _invoke(graph, state)
    sweep_uuid = uuid.UUID(state["sweep_id"])
    # drift node mirrors _post_collection_hook's FULL-sweep path
    drift_instance = registry["drift"].return_value
    drift_instance.detect_all.assert_called_once_with(scoped=False)
    # succeeded_domains = domains with status "completed" or "partial" in state["statuses"]
    drift_instance.detect_state_drift.assert_called_once_with(succeeded_domains={"alpha"})
    drift_instance.run.assert_not_called()
    # notification
    registry["notification"].return_value.notify_all.assert_called_once_with()
    # generic reasoners: agent.run(trigger_type="sweep", sweep_id=...)
    for domain in ("rootcause", "vuln_triage", "compliance"):
        registry[domain].return_value.run.assert_called_once_with(
            trigger_type="sweep", sweep_id=sweep_uuid
        )
        assert final["statuses"][domain] == "completed"
    assert final["statuses"]["drift"] == "completed"
    assert final["statuses"]["notification"] == "completed"


async def test_collector_called_with_sweep_metadata_and_lock_lifecycle():
    registry = {"alpha": _fake_agent("alpha", Tier.COLLECTOR)}
    with sweep_env(registry) as (acquire, release, _rec):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        state = _state(trigger_type="webhook", run_reasoners=False)
        await _invoke(graph, state)
    registry["alpha"].return_value.run.assert_called_once_with(
        trigger_type="webhook", scope="all", sweep_id=uuid.UUID(state["sweep_id"])
    )
    acquire.assert_called_once_with("alpha", "all")
    release.assert_called_once_with("alpha", "all", "tok")


async def test_lock_held_by_concurrent_run_skips_domain():
    registry = {
        "alpha": _fake_agent("alpha", Tier.COLLECTOR),
        "beta": _fake_agent("beta", Tier.COLLECTOR),
    }
    with sweep_env(registry) as (acquire, release, _rec):
        acquire.side_effect = lambda domain, scope: None if domain == "alpha" else "tok"
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state(run_reasoners=False))
    assert final["statuses"]["alpha"] == "skipped"
    assert final["statuses"]["beta"] == "completed"
    registry["alpha"].assert_not_called()
    release.assert_called_once_with("beta", "all", "tok")


async def test_redis_outage_cannot_crash_a_sweep():
    registry = {"alpha": _fake_agent("alpha", Tier.COLLECTOR)}
    with sweep_env(registry) as (acquire, _, _rec):
        acquire.side_effect = LockBackendUnavailable("redis down")
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state(run_reasoners=False))
    assert final["statuses"]["alpha"] == "skipped"
    registry["alpha"].assert_not_called()


async def test_state_carries_only_metadata():
    registry = {
        "alpha": _fake_agent("alpha", Tier.COLLECTOR),
        "recon": _fake_agent("recon", Tier.RECONCILER),
        "drift": _fake_agent("drift", Tier.REASONER),
        "notification": _fake_agent("notification", Tier.REASONER),
    }
    with sweep_env(registry):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        final = await _invoke(graph, _state())
    assert set(final.keys()) <= _META_KEYS  # no payload keys, ever
    assert all(isinstance(v, str) for v in final["runs"].values())
    assert all(isinstance(v, str) for v in final["statuses"].values())


async def test_reconcilers_run_sequentially_in_declared_order():
    calls: list[str] = []

    def _tracking_reconciler(domain):
        cls = _fake_agent(domain, Tier.RECONCILER)
        original = cls.return_value.run

        def run(**kwargs):
            calls.append(domain)
            return original(**kwargs)

        cls.return_value.run = MagicMock(side_effect=run)
        cls.return_value.run.return_value = original.return_value
        return cls

    registry = {
        "graph_maintenance": _tracking_reconciler("graph_maintenance"),
        "inventory_reconcile": _tracking_reconciler("inventory_reconcile"),
        "host_reconcile": _tracking_reconciler("host_reconcile"),
    }
    with sweep_env(registry):
        graph = build_sweep_graph(checkpointer=MemorySaver())
        await _invoke(graph, _state(run_reasoners=False))
    assert calls == ["host_reconcile", "inventory_reconcile", "graph_maintenance"]


# --------------------------------------------------------------------------- #
# run_sweep / checkpointer                                                     #
# --------------------------------------------------------------------------- #


def test_require_postgres_checkpointer_rejects_memorysaver_and_none():
    with pytest.raises(RuntimeError, match="durable Postgres checkpointer"):
        require_postgres_checkpointer(MemorySaver())
    with pytest.raises(RuntimeError, match="durable Postgres checkpointer"):
        require_postgres_checkpointer(None)
    require_postgres_checkpointer(MagicMock(name="AsyncPostgresSaver"))  # no raise


async def test_run_sweep_mints_sweep_id_and_returns_final_state():
    registry = {"alpha": _fake_agent("alpha", Tier.COLLECTOR)}
    with (
        sweep_env(registry),
        patch(
            "infra_brain.graph.get_async_checkpointer",
            new=AsyncMock(return_value=MemorySaver()),
        ),
    ):
        final = await run_sweep(domains=["alpha"], run_reasoners=False)
    assert final["statuses"]["alpha"] == "completed"
    sweep_uuid = uuid.UUID(final["sweep_id"])  # a real minted UUID
    registry["alpha"].return_value.run.assert_called_once_with(
        trigger_type="sweep", scope="all", sweep_id=sweep_uuid
    )
    # production no-arg graph got cached with the resolved checkpointer
    assert graph_mod._PRODUCTION_GRAPH is not None
    assert isinstance(graph_mod._PRODUCTION_CHECKPOINTER, MemorySaver)


async def test_run_sweep_recompiles_with_checkpointer_after_early_no_arg_build():
    """Important-1 regression: a no-arg build_sweep_graph() call BEFORE
    run_sweep ever runs (e.g. some other module/health-check path) must not
    permanently strand the production graph without a checkpointer. Gating
    run_sweep on _PRODUCTION_CHECKPOINTER alone (not _PRODUCTION_GRAPH) and
    invalidating a pre-existing checkpointer-less _PRODUCTION_GRAPH must
    force a fresh compile WITH the real checkpointer."""
    registry = {"alpha": _fake_agent("alpha", Tier.COLLECTOR)}

    class _FakePostgresSaver(MemorySaver):
        """Stand-in for AsyncPostgresSaver: a real BaseCheckpointSaver (so
        langgraph's compile() accepts it) that is deliberately NOT a
        MemorySaver instance for the purposes of this test's identity
        assertions — a distinct subclass instance."""

    real_checkpointer = _FakePostgresSaver()

    with sweep_env(registry):
        # Simulate the early caller: build_sweep_graph() with no args, before
        # run_sweep has installed any checkpointer. This caches a graph
        # compiled with checkpointer=None.
        stale_graph = build_sweep_graph()
        assert graph_mod._PRODUCTION_GRAPH is stale_graph
        assert graph_mod._PRODUCTION_CHECKPOINTER is None

        captured_checkpointers = []
        original_compile = graph_mod.StateGraph.compile

        def _tracking_compile(self, *args, **kwargs):
            captured_checkpointers.append(kwargs.get("checkpointer"))
            return original_compile(self, *args, **kwargs)

        with (
            patch(
                "infra_brain.graph.get_async_checkpointer",
                new=AsyncMock(return_value=real_checkpointer),
            ),
            patch.object(graph_mod.StateGraph, "compile", _tracking_compile),
        ):
            final = await run_sweep(domains=["alpha"], run_reasoners=False)

    assert final["statuses"]["alpha"] == "completed"
    # The stale checkpointer-less graph must have been dropped and rebuilt.
    assert graph_mod._PRODUCTION_GRAPH is not stale_graph
    assert graph_mod._PRODUCTION_CHECKPOINTER is real_checkpointer
    # The recompile that produced the new production graph must have been
    # compiled WITH the resolved checkpointer, not None.
    assert real_checkpointer in captured_checkpointers
    # The cached graph itself carries the real checkpointer through to
    # future no-arg build_sweep_graph() callers.
    assert build_sweep_graph() is graph_mod._PRODUCTION_GRAPH


async def test_run_sweep_unknown_domain_recorded_skipped_never_sent():
    registry = {"alpha": _fake_agent("alpha", Tier.COLLECTOR)}
    with (
        sweep_env(registry),
        patch(
            "infra_brain.graph.get_async_checkpointer",
            new=AsyncMock(return_value=MemorySaver()),
        ),
    ):
        final = await run_sweep(domains=["alpha", "nosuch"], run_reasoners=False)
    assert final["statuses"]["nosuch"] == "skipped"
    assert final["statuses"]["alpha"] == "completed"


# --------------------------------------------------------------------------- #
# run_sweep_sync — dedicated event-loop thread                                #
# --------------------------------------------------------------------------- #


def test_run_sweep_sync_shares_one_loop_across_threads():
    """New public API regression: two run_sweep_sync calls from different
    OS threads must observe the identical module-singleton event loop (never
    a fresh asyncio.run() loop per call), because the AsyncPostgresSaver
    singleton binds to whichever loop first awaits it."""
    registry = {"alpha": _fake_agent("alpha", Tier.COLLECTOR)}
    loops_seen: list = []
    errors: list[BaseException] = []

    def _call():
        try:
            with (
                sweep_env(registry),
                patch(
                    "infra_brain.graph.get_async_checkpointer",
                    new=AsyncMock(return_value=MemorySaver()),
                ),
            ):
                run_sweep_sync(domains=["alpha"], run_reasoners=False)
            loops_seen.append(graph_mod._sync_loop_for_testing())
        except BaseException as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    try:
        t1 = threading.Thread(target=_call)
        t1.start()
        t1.join(timeout=10)
        t2 = threading.Thread(target=_call)
        t2.start()
        t2.join(timeout=10)

        assert not errors, f"run_sweep_sync raised in a worker thread: {errors}"
        assert len(loops_seen) == 2
        assert loops_seen[0] is not None
        # Same singleton loop instance observed from both threads.
        assert loops_seen[0] is loops_seen[1]
    finally:
        loop = graph_mod._sync_loop_for_testing()
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        graph_mod._SYNC_LOOP = None


def test_collector_redis_unavailable_writes_failed_collection_run_row():
    """#89: the Redis-unavailable skip path must write a CollectionRun row so
    check_collection_health() can alert on it immediately, not only once
    staleness eventually elapses."""
    from unittest.mock import MagicMock, patch

    from infra_brain import dedup
    from infra_brain.graph import _make_collector, _policy_for

    sweep_id = str(uuid.uuid4())
    node = _make_collector("linux", _policy_for("linux"))
    mock_session = MagicMock()
    with (
        patch("infra_brain.graph.dedup.try_acquire", side_effect=dedup.LockBackendUnavailable()),
        patch("infra_brain.redis_outage.get_session") as mock_get_session,
    ):
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        node({"sweep_id": sweep_id, "trigger_type": "sweep"})

    mock_session.add.assert_called_once()
    row = mock_session.add.call_args[0][0]
    assert row.domain == "linux"
    assert row.status == "failed"


def test_collector_redis_unavailable_logs_domain_and_sweep_id_as_extra(caplog):
    """#87: graph.py's collector node's Redis-unavailable ERROR log (the F-029
    loud-failure path) must carry domain/sweep_id via extra={}."""
    import logging
    import uuid
    from unittest.mock import patch

    from infra_brain import dedup
    from infra_brain.graph import _make_collector, _policy_for

    sweep_id = str(uuid.uuid4())
    node = _make_collector("linux", _policy_for("linux"))
    with (
        patch("infra_brain.graph.dedup.try_acquire", side_effect=dedup.LockBackendUnavailable()),
        caplog.at_level(logging.ERROR, logger="infra_brain.graph"),
    ):
        result = node({"sweep_id": sweep_id, "trigger_type": "sweep"})

    assert result == {"statuses": {"linux": "skipped"}}
    matching = [r for r in caplog.records if "Redis lock backend UNAVAILABLE" in r.getMessage()]
    assert matching
    assert any(getattr(r, "domain", None) == "linux" for r in matching)
    assert any(getattr(r, "sweep_id", None) == sweep_id for r in matching)
