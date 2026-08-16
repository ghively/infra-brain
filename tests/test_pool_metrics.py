"""TRK-083: intra-agent ThreadPoolExecutor queue-depth / saturation metrics.

Verifies that :func:`infra_brain.pool_metrics.observe_pool` records the pool's
queue depth, worker bound, and cumulative submitted-task count on the default
Prometheus registry, and that the metrics surface on the existing ``/metrics``
scrape endpoint (TRK-093) without any endpoint change.
"""

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from infra_brain.main import create_app
from infra_brain.pool_metrics import observe_pool

_COUNTER = "agent_pool_tasks_submitted_total"
_DEPTH = "agent_pool_queue_depth"
_WORKERS = "agent_pool_max_workers"


def _submitted(agent, pool):
    val = REGISTRY.get_sample_value(_COUNTER, {"agent": agent, "pool": pool})
    return val or 0.0


def test_observe_pool_sets_gauges_and_increments_counter():
    labels = {"agent": "cicd", "pool": "pipelines_unit"}
    before = _submitted(**labels)

    observe_pool("cicd", "pipelines_unit", 7, 10)

    assert REGISTRY.get_sample_value(_DEPTH, labels) == 7
    assert REGISTRY.get_sample_value(_WORKERS, labels) == 10
    assert _submitted(**labels) - before == 7

    # A second run updates the depth gauge (most-recent depth) and accumulates
    # the counter -- the gauge is not additive, the counter is.
    observe_pool("cicd", "pipelines_unit", 3, 10)
    assert REGISTRY.get_sample_value(_DEPTH, labels) == 3
    assert _submitted(**labels) - before == 10


def test_observe_pool_empty_batch_records_zero_depth_without_incrementing():
    labels = {"agent": "octopus", "pool": "variables_empty"}
    before = _submitted(**labels)

    observe_pool("octopus", "variables_empty", 0, 8)

    assert REGISTRY.get_sample_value(_DEPTH, labels) == 0
    assert REGISTRY.get_sample_value(_WORKERS, labels) == 8
    # Zero-task batch must not increment the cumulative submitted counter.
    assert _submitted(**labels) - before == 0


def test_observe_pool_never_raises_into_caller():
    # A bad value would raise inside prometheus_client; instrumentation must
    # swallow it so a collection run is never broken by a metrics failure.
    observe_pool("cicd", "bad_value", "not-an-int", 10)  # type: ignore[arg-type]


def test_metrics_endpoint_exposes_pool_metrics():
    # Ensure at least one child series exists, then scrape the real endpoint.
    observe_pool("octopus", "deep_processes_e2e", 5, 8)
    client = TestClient(create_app())
    resp = client.get("/metrics")

    assert resp.status_code == 200
    body = resp.text
    assert _DEPTH in body
    assert _WORKERS in body
    assert _COUNTER in body
    assert 'agent="octopus"' in body
