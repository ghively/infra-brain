"""TRK-083: intra-agent ThreadPoolExecutor queue-depth / saturation metrics.

The octopus and cicd collectors each fan work out across a bounded
``ThreadPoolExecutor`` (fixed ``max_workers``). Nothing measured how deep the
work queue got under load, so pool saturation could not be judged from data.
TRK-083 was previously deferred as "not a suspected bottleneck"; this module
simply adds the observability so future load can be judged from data -- it is
ADDITIVE and read-only, and does NOT change pool sizing or execution behaviour.

The collectors call :func:`observe_pool` once per pool run to record the batch
size (queue depth at submit time -- both the ``submit`` + ``as_completed`` and
``pool.map`` idioms front-load their full batch, so the submit-time depth is the
peak depth), the configured worker bound, and a cumulative submitted-task
counter.

Metrics register on prometheus_client's default global ``REGISTRY`` and are
therefore serialized automatically by the existing ``/metrics`` endpoint in
``main.py`` (TRK-093) -- no endpoint change is needed. They are declared at
module scope (not inside a function) so repeated imports reuse the same
singletons instead of hitting prometheus_client's duplicate-registration error
on the shared default REGISTRY. Metric names follow the TRK-093 convention:
bare snake_case, no app-specific prefix.

Caveat (documented, not fixed here): collector sweeps run in the scheduler
process while ``/metrics`` is served by the API process. Without
prometheus_client multiprocess mode these gauges reflect only pool activity in
whichever process serves the scrape (e.g. an API-triggered sweep). Cross-process
aggregation is a deployment concern out of scope for this additive change.
"""

import logging

from prometheus_client import Counter, Gauge

log = logging.getLogger(__name__)

# Low cardinality by construction: two agents ("octopus", "cicd") and three
# fixed pool names -> at most three label series ever exist.
POOL_QUEUE_DEPTH = Gauge(
    "agent_pool_queue_depth",
    "Tasks submitted to an intra-agent ThreadPoolExecutor in its most recent "
    "pool run (work-queue depth at submit time).",
    ["agent", "pool"],
)
POOL_MAX_WORKERS = Gauge(
    "agent_pool_max_workers",
    "Configured max_workers bound for an intra-agent ThreadPoolExecutor pool.",
    ["agent", "pool"],
)
POOL_TASKS_SUBMITTED = Counter(
    "agent_pool_tasks_submitted_total",
    "Cumulative tasks submitted to an intra-agent ThreadPoolExecutor pool.",
    ["agent", "pool"],
)


def observe_pool(agent: str, pool: str, task_count: int, max_workers: int) -> None:
    """Record one pool run's queue depth and worker bound.

    Call once, immediately before submitting the batch to the executor. Purely
    additive: sets/increments Prometheus metrics only -- no I/O, no effect on the
    pool, its sizing, or execution. Safe to call with an empty batch (records
    depth 0). Never raises into the caller -- instrumentation must never break a
    read-only collection run.

    Args:
        agent: collector domain owning the pool (e.g. ``"octopus"``, ``"cicd"``).
        pool: fixed, low-cardinality name for this pool site within the agent.
        task_count: number of tasks about to be submitted (the queue depth).
        max_workers: the pool's configured ``max_workers`` bound.
    """
    try:
        labels = {"agent": agent, "pool": pool}
        POOL_MAX_WORKERS.labels(**labels).set(max_workers)
        POOL_QUEUE_DEPTH.labels(**labels).set(task_count)
        if task_count:
            POOL_TASKS_SUBMITTED.labels(**labels).inc(task_count)
    except Exception as exc:  # pragma: no cover - defensive; must not break a sweep
        log.warning("observe_pool(%s/%s) metric update failed: %s", agent, pool, exc)
