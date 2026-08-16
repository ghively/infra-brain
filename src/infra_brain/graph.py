"""Sweep graph — the orchestration v2.1 core (Phase 2 Task 2, spec §2.1-2.6, 2.9).

One LangGraph ``StateGraph`` runs a full sweep:

    START → dispatch ─Send─▶ {one node per COLLECTOR domain, parallel}
                     └──────────────────────────────┐ (nothing to send)
                                                    ▼
            join (defer=True — fires on ANY terminal status mix, never hangs)
              → host_reconcile → inventory_reconcile → graph_maintenance
              → [run_reasoners?] drift → notification → {rootcause, vuln_triage,
                 compliance — parallel} → END

Design notes:

* **Topology is registry-derived**: nodes come from ``etl.spec.sweep_members()``
  (which reads ``supervisor.AGENT_REGISTRY``), so adding agent #29 with a
  COLLECTOR/RECONCILER/REASONER tier automatically adds its node. ``remediation``
  and ``drift_learning`` are excluded via ``_PHASE3_DEFERRED`` (below).
* **Per-domain RetryPolicy** (spec §2.6): a single shared collector node would
  force one ``retry_on`` on all 12 collectors; per-domain nodes let vsphere
  retry pyVmomi/socket connection classes while HTTP collectors retry
  httpx/requests classes. This is ONE retry layer for connection-setup /
  auth-expiry failures — tenacity already owns transient in-request HTTP
  retries inside the collectors themselves.
* **Retry-exhaustion never crashes the sweep**: LangGraph's RetryPolicy re-raises
  the final failure out of the node, which would abort the whole graph run. The
  collector wrapper therefore counts its own invocations per (sweep_id, domain)
  and, on the policy's final attempt, swallows the exception and records
  ``retry_exhausted`` in state instead of re-raising — so the ``defer=True``
  join always fires (spec §2.3).
* **State carries metadata only** (domain → run_id / status), never payloads —
  collected data lives in Postgres, written by the collectors themselves.
* Read-only guarantee unchanged: this module only *dispatches* the same
  ETLConnector.run() path supervisor.dispatch() uses; every agent's tool calls
  still flow through callbacks/registry.build_callbacks().
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from itertools import pairwise
from typing import Annotated, Any, TypedDict

import httpx
import requests
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy, Send

from infra_brain import dedup, supervisor
from infra_brain.checkpointer import get_async_checkpointer
from infra_brain.config import get_settings
from infra_brain.etl.base import RUN_STATUS_RETRY_EXHAUSTED, CollectorSkipped
from infra_brain.etl.spec import Tier, sweep_members
from infra_brain.runtime_flags import paused_domains, record_paused_run

logger = logging.getLogger(__name__)

# TODO(Phase 3): remediation joins the graph behind the interrupt/approval gate
# (RUN_STATUS_INTERRUPT_PENDING) and drift_learning joins after it; until then
# both keep their existing crons in sweep mode — remediation "30 5 * * *",
# drift_learning "0 4 * * 0" — unchanged.
_PHASE3_DEFERRED = {"remediation", "drift_learning"}

# Reasoners with a mandated sequential position (drift feeds notification);
# every other reasoner runs in parallel after them.
_ORDERED_REASONERS = ("drift", "notification")

# Reconciler-tier execution order (spec §2.4). Registry-derived members not in
# this list are appended alphabetically after it.
_RECONCILER_ORDER = ("host_reconcile", "inventory_reconcile", "graph_maintenance")

_JOIN = "join"
_DISPATCH = "dispatch"


def _merge_dicts(left: dict | None, right: dict | None) -> dict:
    """Reducer for the runs/statuses maps — parallel collector branches each
    contribute their own {domain: ...} entry."""
    return {**(left or {}), **(right or {})}


class SweepState(TypedDict, total=False):
    """Sweep metadata only — never collected payloads (those live in Postgres)."""

    sweep_id: str
    domains: list[str]  # requested collectors ([] = all sweep collectors)
    run_reasoners: bool  # single-domain webhook policy switch
    trigger_type: str  # stamped onto each CollectionRun (e.g. "sweep", "webhook")
    runs: Annotated[dict[str, str], _merge_dicts]  # domain -> run_id
    statuses: Annotated[dict[str, str], _merge_dicts]  # domain -> terminal status


# --------------------------------------------------------------------------- #
# Per-domain retry policies (spec §2.6)                                        #
# --------------------------------------------------------------------------- #


def _vsphere_retry_on(exc: Exception) -> bool:
    """pyVmomi connection/auth-expiry classes + socket-level OSError."""
    if isinstance(exc, CollectorSkipped):
        return False
    if isinstance(exc, OSError):  # ConnectionError/TimeoutError are OSError subclasses
        return True
    module = type(exc).__module__ or ""
    return module.startswith(("pyVmomi", "pyVim"))


def _http_retry_on(exc: Exception) -> bool:
    """httpx/requests transport classes + connection-setup OSError."""
    if isinstance(exc, CollectorSkipped):
        return False
    return isinstance(exc, (httpx.HTTPError, requests.RequestException, OSError))


def _default_retry_on(exc: Exception) -> bool:
    """Connection-setup classes only; ConnectionError/TimeoutError ⊂ OSError."""
    if isinstance(exc, CollectorSkipped):
        return False
    return isinstance(exc, OSError)


_DEFAULT_RETRY = RetryPolicy(retry_on=_default_retry_on)
_RETRY_POLICIES: dict[str, RetryPolicy] = {
    "vsphere": RetryPolicy(retry_on=_vsphere_retry_on),
    **{
        d: RetryPolicy(retry_on=_http_retry_on) for d in ("octopus", "vuln", "cicd", "iac", "cloud")
    },
}


def _policy_for(domain: str) -> RetryPolicy:
    return _RETRY_POLICIES.get(domain, _DEFAULT_RETRY)


def _is_retryable(policy: RetryPolicy, exc: Exception) -> bool:
    retry_on = policy.retry_on
    if isinstance(retry_on, type):
        return isinstance(exc, retry_on)
    if callable(retry_on):
        return bool(retry_on(exc))
    return isinstance(exc, tuple(retry_on))


# Per-(sweep_id, domain) attempt counter — see module docstring: lets the
# collector wrapper convert the policy's FINAL failure into a state entry
# instead of a graph-aborting re-raise. Keys are removed on any terminal path
# (success, or the final non-retryable/exhausted failure) — see the two
# `_ATTEMPTS.pop(key, None)` sites inside `collector()`.
#
# Minor known gap (Minor-4a): only `except Exception` is handled inside the
# wrapper, so a BaseException that is NOT an Exception (KeyboardInterrupt,
# asyncio.CancelledError, GeneratorExit, SystemExit) raised mid-attempt skips
# both the increment-and-reraise branch AND the terminal pop, stranding the
# key at its pre-exception count. A wrapping `try/finally` that pops
# unconditionally would fix the leak but would also incorrectly pop while a
# retryable attempt is still in flight (the `raise` path for retry is
# intentionally NOT supposed to clear the counter — the next attempt needs to
# read it). Expressing "pop only when this invocation is the wrapper's
# terminal return, but not when it's an intentional re-raise for retry, but
# also always on a stray BaseException" cleanly needs either a sentinel
# return value or restructuring the retry re-raise into its own helper: left
# as a documented gap rather than a speculative rewrite. In practice a
# leaked entry here only affects the next sweep_id's key lookup (the tuple
# includes sweep_id, so it self-heals every sweep) — it does not corrupt a
# future collector run for the same sweep.
_ATTEMPTS: dict[tuple[str, str], int] = {}
_ATTEMPTS_LOCK = threading.Lock()


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Nodes                                                                        #
# --------------------------------------------------------------------------- #


def _disabled_domains() -> set[str]:
    raw = get_settings().collection_disabled_domains or ""
    return {d.strip() for d in raw.split(",") if d.strip()}


def _plan_collectors(state: SweepState) -> tuple[list[str], list[str], list[str], list[str]]:
    """(to_run, disabled_skipped, paused_skipped, unknown) for the collector fan-out.

    Two independent skip reasons, kept separate because they behave
    differently downstream: ``collection_disabled_domains`` is a static
    settings list, while ``paused`` comes from the live
    ``dispatchable__<domain>`` RuntimeConfig override that the dashboard's
    Disable Collector toggle writes. That override is also honored by
    ``supervisor.dispatch()``; wiring it here too is what makes the toggle
    real under ``sweep_graph_enabled`` — the graph fans collectors out itself
    and never calls ``dispatch()``, so before this it was silently a no-op.

    Deterministic on (state, settings) for the disabled/unknown split; the
    paused split additionally reads the DB, so a toggle flipped in the
    sub-second window between the dispatch node's call and the conditional
    edge's could in principle disagree between the two. Harmless in practice
    (worst case one extra or one missing collector for a single sweep) and
    strictly better than the toggle not working at all, but it is why the
    paused set is read fresh rather than cached across a sweep.
    """
    all_collectors = sweep_members()[Tier.COLLECTOR]
    requested = state.get("domains") or list(all_collectors)
    known = [d for d in requested if d in all_collectors]
    unknown = [d for d in requested if d not in all_collectors]
    disabled = _disabled_domains()
    paused = paused_domains()
    skipped = [d for d in known if d in disabled]
    paused_skipped = [d for d in known if d not in disabled and d in paused]
    to_run = [d for d in known if d not in disabled and d not in paused]
    return to_run, skipped, paused_skipped, unknown


def _dispatch(state: SweepState) -> dict:
    """Record disabled/paused/unknown domains as skipped (R-11: NEVER Sent)."""
    _, skipped, paused_skipped, unknown = _plan_collectors(state)
    statuses: dict[str, str] = {}
    for domain in skipped:
        logger.info(
            "sweep %s: domain %r is in collection_disabled_domains — skipped, not dispatched",
            state.get("sweep_id"),
            domain,
        )
        statuses[domain] = "skipped"
    for domain in paused_skipped:
        logger.info(
            "sweep %s: domain %r has dispatchable__%s=false — paused, not dispatched",
            state.get("sweep_id"),
            domain,
            domain,
        )
        # Persist the pause the same way supervisor.dispatch() does, so the
        # roster/dashboard see a fresh "skipped" run instead of a frozen
        # last_run that reads as dormant. Only for the live toggle — the
        # static collection_disabled_domains skip above keeps its existing
        # write-nothing behavior deliberately unchanged.
        record_paused_run(
            domain,
            trigger_type=state.get("trigger_type", "sweep"),
            sweep_id=_as_uuid(state.get("sweep_id", "")),
        )
        statuses[domain] = "skipped"
    for domain in unknown:
        logger.warning(
            "sweep %s: requested domain %r is not a sweep collector — skipped",
            state.get("sweep_id"),
            domain,
        )
        statuses[domain] = "skipped"
    return {"statuses": statuses} if statuses else {}


def _fan_out(state: SweepState) -> list[Send | str]:
    """Send({metadata}) per runnable collector; straight to join when none."""
    to_run, _, _, _ = _plan_collectors(state)
    if not to_run:
        return [_JOIN]
    payload = {
        "sweep_id": state.get("sweep_id", ""),
        "trigger_type": state.get("trigger_type", "sweep"),
    }
    return [Send(domain, payload) for domain in to_run]


def _make_collector(domain: str, policy: RetryPolicy):
    """Collector node: Redis dedup → AGENT_REGISTRY[domain]().run(...) → metadata.

    Notes (Minor-4b):
    * ``CollectorSkipped`` is only ever raised and caught *inside* an agent's
      own ``run()`` today, so it never reaches this wrapper's ``except
      Exception`` — the ``retryable = False`` branch for it in
      ``_is_retryable``/the ``_*_retry_on`` predicates is dead code as of
      this writing. It exists so that if a future ``run()`` override lets
      ``CollectorSkipped`` propagate instead of swallowing it, this wrapper
      still does the right thing: it will land in the non-retryable branch
      and be recorded as ``status = "failed"`` (not silently retried, not
      silently skipped).
    * The Redis lock is held only around ``agent.run()`` — it is released in
      the ``finally`` block below on every path, including between retry
      attempts (the node re-enters ``collector()`` from scratch on each
      ``RetryPolicy`` attempt, re-acquiring the lock). That means a
      concurrent sweep can acquire this domain's lock during the gap between
      this sweep's failed attempt and its retry; if it does, this sweep's
      retry will observe the lock as already held and record the domain as
      ``"skipped"`` rather than completing its own retry.
    """

    def collector(state: SweepState) -> dict:
        sweep_id = state.get("sweep_id", "")
        key = (sweep_id, domain)
        token: str | None = None
        try:
            try:
                token = dedup.try_acquire(domain, "all")
            except dedup.LockBackendUnavailable:
                # A Redis outage must never crash a sweep — treat it like
                # lock-unavailable, but LOUDLY (F-029).
                logger.error(
                    "sweep %s: Redis lock backend UNAVAILABLE — skipping domain %r "
                    "(collection for this domain did not run)",
                    sweep_id,
                    domain,
                    exc_info=True,
                    extra={"domain": domain, "sweep_id": sweep_id},
                )
                from infra_brain.redis_outage import record_redis_outage_run

                record_redis_outage_run(domain, sweep_id=sweep_id or None)
                return {"statuses": {domain: "skipped"}}
            if token is None:
                logger.warning(
                    "sweep %s: lock %s/all already held by a concurrent run — skipping",
                    sweep_id,
                    domain,
                    extra={"domain": domain, "sweep_id": sweep_id},
                )
                return {"statuses": {domain: "skipped"}}
            agent = supervisor.AGENT_REGISTRY[domain]()
            result = agent.run(
                trigger_type=state.get("trigger_type", "sweep"),
                scope="all",
                sweep_id=_as_uuid(sweep_id),
            )
            with _ATTEMPTS_LOCK:
                _ATTEMPTS.pop(key, None)
            return {
                "runs": {domain: str(result.run_id)},
                "statuses": {domain: result.status},
            }
        except Exception as exc:
            with _ATTEMPTS_LOCK:
                attempts = _ATTEMPTS.get(key, 0) + 1
                _ATTEMPTS[key] = attempts
            retryable = _is_retryable(policy, exc)
            if retryable and attempts < policy.max_attempts:
                raise  # hand back to the node's RetryPolicy (backoff + jitter)
            with _ATTEMPTS_LOCK:
                _ATTEMPTS.pop(key, None)
            status = RUN_STATUS_RETRY_EXHAUSTED if retryable else "failed"
            logger.error(
                "sweep %s: collector %r terminal failure (status=%s, attempts=%d): %s",
                sweep_id,
                domain,
                status,
                attempts,
                exc,
            )
            return {"statuses": {domain: status}}
        finally:
            if token is not None:
                dedup.release(domain, "all", token)

    collector.__name__ = f"collect_{domain}"
    return collector


def _join(state: SweepState) -> dict:
    """defer=True barrier: fires once every dispatched collector branch has
    reached a terminal status — completed/partial/failed/skipped/retry_exhausted
    in any mix (spec §2.3: the sweep never hangs on one domain)."""
    return {}


def _make_reconciler(domain: str):
    def reconciler(state: SweepState) -> dict:
        sweep_id = state.get("sweep_id", "")
        try:
            agent_cls = supervisor.AGENT_REGISTRY[domain]
        except KeyError:
            # graph_maintenance is an optional-dependency domain — degrade
            # just this node, not the sweep (same policy as supervisor's hook).
            logger.warning("sweep %s: reconciler %r unavailable — skipped", sweep_id, domain)
            return {"statuses": {domain: "skipped"}}
        try:
            result = agent_cls().run(
                trigger_type=state.get("trigger_type", "sweep"),
                scope="all",
                sweep_id=_as_uuid(sweep_id),
            )
            return {
                "runs": {domain: str(result.run_id)},
                "statuses": {domain: result.status},
            }
        except Exception:
            logger.exception("sweep %s: reconciler %r failed", sweep_id, domain)
            return {"statuses": {domain: "failed"}}

    reconciler.__name__ = f"reconcile_{domain}"
    return reconciler


def _drift_node(state: SweepState) -> dict:
    """FULL-sweep drift semantics — mirrors supervisor._post_collection_hook's
    unscoped fallback path (detect_all() fleet-wide + detect_state_drift()),
    NOT the per-collection run_id-scoped path: a sweep just refreshed every
    domain, so the fleet-wide scan is the correct (and intended) scope here.

    Task 3 (partial-sweep drift suppression): the disappearance scan
    (detect_state_drift) is restricted to domains that actually completed this
    sweep — "completed" or "partial" — so a failed/retry-exhausted collector's
    stale data can't manufacture false "disappeared asset" drift events.
    "partial" is included alongside "completed" harmlessly: the domain-last-run
    map inside detect_state_drift only ever considers CollectionRun rows with
    status="completed" (drift.py), so a "partial" status here doesn't grant a
    domain any baseline it wouldn't already have — it only says "don't exclude
    this domain's resources from the scan."
    """
    sweep_id = state.get("sweep_id", "")
    try:
        succeeded = {
            d for d, s in state.get("statuses", {}).items() if s in ("completed", "partial")
        }
        detector = supervisor.AGENT_REGISTRY["drift"]()
        detector.detect_all(scoped=False)
        detector.detect_state_drift(succeeded_domains=succeeded)
        return {"statuses": {"drift": "completed"}}
    except Exception:
        logger.exception("sweep %s: drift detection failed", sweep_id)
        return {"statuses": {"drift": "failed"}}


def _notification_node(state: SweepState) -> dict:
    sweep_id = state.get("sweep_id", "")
    try:
        supervisor.AGENT_REGISTRY["notification"]().notify_all()
        return {"statuses": {"notification": "completed"}}
    except Exception:
        logger.exception("sweep %s: notification failed", sweep_id)
        return {"statuses": {"notification": "failed"}}


def _make_reasoner(domain: str):
    def reasoner(state: SweepState) -> dict:
        sweep_id = state.get("sweep_id", "")
        try:
            result = supervisor.AGENT_REGISTRY[domain]().run(
                trigger_type="sweep",
                sweep_id=_as_uuid(sweep_id),
            )
            return {
                "runs": {domain: str(result.run_id)},
                "statuses": {domain: result.status},
            }
        except Exception:
            logger.exception("sweep %s: reasoner %r failed", sweep_id, domain)
            return {"statuses": {domain: "failed"}}

    reasoner.__name__ = f"reason_{domain}"
    return reasoner


# --------------------------------------------------------------------------- #
# Graph assembly                                                               #
# --------------------------------------------------------------------------- #


def _ordered(members: list[str], preferred: tuple[str, ...]) -> list[str]:
    """Sort *members* by position in *preferred*; unknown members go last, alphabetically."""
    return sorted(
        members, key=lambda d: (preferred.index(d) if d in preferred else len(preferred), d)
    )


def _reasoner_stages(reasoners: list[str]) -> list[list[str]]:
    """Sequential head (drift → notification) then one parallel tail stage."""
    stages = [[d] for d in _ORDERED_REASONERS if d in reasoners]
    parallel = sorted(d for d in reasoners if d not in _ORDERED_REASONERS)
    if parallel:
        stages.append(parallel)
    return stages


def _build() -> StateGraph:
    members = sweep_members()
    collectors = members[Tier.COLLECTOR]
    reconcilers = _ordered(members[Tier.RECONCILER], _RECONCILER_ORDER)
    reasoners = [d for d in members[Tier.REASONER] if d not in _PHASE3_DEFERRED]

    graph = StateGraph(SweepState)
    graph.add_node(_DISPATCH, _dispatch)
    graph.add_edge(START, _DISPATCH)

    for domain in collectors:
        policy = _policy_for(domain)
        graph.add_node(domain, _make_collector(domain, policy), retry_policy=policy)
        graph.add_edge(domain, _JOIN)
    # defer=True is belt-and-braces: in the current uniform-depth topology
    # (every collector is exactly one superstep from dispatch) LangGraph's
    # BSP semantics already synchronize the join without it — no test can
    # pin this flag today because nothing regresses if it's removed right
    # now. It becomes load-bearing once Phase 3 adds branches of unequal
    # depth feeding into this join. DO NOT REMOVE.
    graph.add_node(_JOIN, _join, defer=True)
    graph.add_conditional_edges(_DISPATCH, _fan_out, [*collectors, _JOIN])

    previous = _JOIN
    for domain in reconcilers:
        graph.add_node(domain, _make_reconciler(domain))
        graph.add_edge(previous, domain)
        previous = domain

    stages = _reasoner_stages(reasoners)
    if not stages:
        graph.add_edge(previous, END)
        return graph

    special = {"drift": _drift_node, "notification": _notification_node}
    for stage in stages:
        for domain in stage:
            graph.add_node(domain, special.get(domain) or _make_reasoner(domain))

    first_stage = stages[0]

    def _reasoner_gate(state: SweepState) -> list[str]:
        if state.get("run_reasoners", True):
            return list(first_stage)
        return [END]

    graph.add_conditional_edges(previous, _reasoner_gate, [*first_stage, END])
    for current, upcoming in pairwise(stages):
        for src in current:
            for dst in upcoming:
                graph.add_edge(src, dst)
    for domain in stages[-1]:
        graph.add_edge(domain, END)
    return graph


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

# Cached production compile (no-arg build_sweep_graph() path only). run_sweep()
# installs the async Postgres checkpointer before the first no-arg build —
# build_sweep_graph() itself is sync and cannot await get_async_checkpointer().
_PRODUCTION_GRAPH: CompiledStateGraph | None = None
_PRODUCTION_CHECKPOINTER: Any = None


def build_sweep_graph(checkpointer: Any = None) -> CompiledStateGraph:
    """Compile the sweep graph.

    No-arg call: the cached production singleton, compiled with the module's
    production checkpointer (installed by run_sweep()). An explicit
    ``checkpointer=`` argument BYPASSES the cache and returns a fresh compile —
    tests pass MemorySaver freely without polluting the production graph.
    """
    global _PRODUCTION_GRAPH
    if checkpointer is not None:
        return _build().compile(checkpointer=checkpointer)
    if _PRODUCTION_GRAPH is None:
        _PRODUCTION_GRAPH = _build().compile(checkpointer=_PRODUCTION_CHECKPOINTER)
    return _PRODUCTION_GRAPH


def require_postgres_checkpointer(cp: Any) -> None:
    """Hard-fail on a non-durable checkpointer (spec §2.5).

    Used by the Phase 3 interrupt path: an interrupted sweep resumed against a
    MemorySaver would silently lose its pending approval state on restart.
    run_sweep() itself only WARNS for now — plain sweeps use durability="exit"
    and a lost checkpoint merely forfeits resumability, not correctness.
    """
    if cp is None or isinstance(cp, MemorySaver):
        raise RuntimeError(
            "Sweep interrupts require a durable Postgres checkpointer "
            "(AsyncPostgresSaver); got "
            f"{type(cp).__name__ if cp is not None else None!r}. "
            "Check POSTGRES_URL / langgraph-checkpoint-postgres."
        )


async def run_sweep(
    domains: list[str] | None = None,
    run_reasoners: bool = True,
    trigger_type: str = "sweep",
) -> dict:
    """Mint a sweep_id, run the full sweep graph, return the final state."""
    global _PRODUCTION_CHECKPOINTER, _PRODUCTION_GRAPH
    # Gate on the checkpointer alone (Important-1): an earlier no-arg
    # build_sweep_graph() call (e.g. from a health check or another module
    # import) can populate _PRODUCTION_GRAPH before run_sweep ever runs,
    # compiling it with checkpointer=None. If we also required
    # _PRODUCTION_GRAPH is None here, that pre-existing checkpointer-less
    # graph would be reused forever and every sweep would silently run
    # without durable checkpointing. Instead: only _PRODUCTION_CHECKPOINTER
    # gates whether we resolve one, and if a graph was already cached before
    # we had a checkpointer to install, that graph is stale — drop it so the
    # next build_sweep_graph() call below recompiles WITH the saver.
    if _PRODUCTION_CHECKPOINTER is None:
        checkpointer = await get_async_checkpointer()
        if isinstance(checkpointer, MemorySaver):
            logger.warning(
                "sweep checkpointer fell back to MemorySaver — sweep checkpoints "
                "will not survive a restart (durability='exit' state is lost). "
                "Phase 3 interrupt-gated sweeps will hard-require Postgres "
                "(require_postgres_checkpointer)."
            )
        _PRODUCTION_CHECKPOINTER = checkpointer
        if _PRODUCTION_GRAPH is not None:
            # It was necessarily compiled with checkpointer=None (the only
            # way _PRODUCTION_GRAPH could exist while _PRODUCTION_CHECKPOINTER
            # was still None) — invalidate so it recompiles with the saver.
            _PRODUCTION_GRAPH = None
    graph = build_sweep_graph()
    sweep_id = uuid.uuid4()
    state: SweepState = {
        "sweep_id": str(sweep_id),
        "domains": list(domains or []),
        "run_reasoners": run_reasoners,
        "trigger_type": trigger_type,
        "runs": {},
        "statuses": {},
    }
    logger.info(
        "sweep %s starting (domains=%s, run_reasoners=%s, trigger_type=%s)",
        sweep_id,
        domains or "ALL",
        run_reasoners,
        trigger_type,
    )
    final_state = await graph.ainvoke(
        state,
        config={"configurable": {"thread_id": str(sweep_id)}},
        durability="exit",
    )
    logger.info("sweep %s finished: statuses=%s", sweep_id, final_state.get("statuses"))
    return final_state


# --------------------------------------------------------------------------- #
# Sync entry point — dedicated long-lived event loop                          #
# --------------------------------------------------------------------------- #

# Module singleton: one daemon thread running one event loop, forever, for
# the lifetime of the process. See run_sweep_sync's docstring for why this
# has to be a single shared loop rather than a per-call asyncio.run().
_SYNC_LOOP: asyncio.AbstractEventLoop | None = None
_SYNC_LOOP_LOCK = threading.Lock()


def _get_sync_loop() -> asyncio.AbstractEventLoop:
    """Return the module-singleton event-loop, starting its daemon thread on
    first use. Thread-safe: the double-checked lock ensures concurrent first
    callers (e.g. scheduler + webhook threads racing at startup) still only
    start one loop/thread."""
    global _SYNC_LOOP
    if _SYNC_LOOP is not None:
        return _SYNC_LOOP
    with _SYNC_LOOP_LOCK:
        if _SYNC_LOOP is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="sweep-graph-sync-loop",
                daemon=True,
            )
            thread.start()
            _SYNC_LOOP = loop
    return _SYNC_LOOP


def _sync_loop_for_testing() -> asyncio.AbstractEventLoop | None:
    """Private accessor for tests only: read the current singleton loop
    (or None if run_sweep_sync has never been called) without triggering a
    start, so tests can assert identity across calls/threads."""
    return _SYNC_LOOP


def run_sweep_sync(
    domains: list[str] | None = None,
    run_reasoners: bool = True,
    trigger_type: str = "sweep",
) -> dict:
    """Synchronous wrapper around run_sweep for callers that are not already
    inside an event loop — APScheduler jobs and sync webhook handlers.

    Why this exists instead of ``asyncio.run(run_sweep(...))`` at each call
    site: ``get_async_checkpointer()`` (checkpointer.py) hands back a
    process-wide ``AsyncPostgresSaver`` singleton wrapping one psycopg async
    connection pool, and that pool binds to whichever event loop first awaits
    it. ``asyncio.run()`` creates a brand-new event loop for every call and
    tears it down on return; a scheduler thread and a webhook thread each
    calling ``asyncio.run(run_sweep(...))`` would each spin up their own
    fresh loop, and the SECOND such call would hand the pool (bound to the
    FIRST call's now-closed loop) coroutines scheduled on a different loop —
    silently corrupting the pool (cross-loop "Future attached to a different
    loop" errors, or worse, connections reused across loops). Routing every
    sync caller through ONE persistent daemon-thread event loop via
    ``asyncio.run_coroutine_threadsafe`` guarantees the checkpointer
    singleton is only ever awaited from the same loop for the entire process
    lifetime, no matter which OS thread calls ``run_sweep_sync``.
    """
    loop = _get_sync_loop()
    future = asyncio.run_coroutine_threadsafe(
        run_sweep(domains=domains, run_reasoners=run_reasoners, trigger_type=trigger_type),
        loop,
    )
    return future.result()
