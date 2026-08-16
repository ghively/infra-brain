"""TRK-093: RED-style HTTP metrics exposed at /metrics.

Verifies the @app.middleware("http") instrumentation registered in create_app()
records requests and that the /metrics scrape endpoint returns Prometheus text
format with no auth (same trust tier as /healthz).
"""

from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from infra_brain.main import create_app


def test_metrics_endpoint_exposes_prometheus_text():
    client = TestClient(create_app())

    # Make a request first so the RED counter has at least one sample, then
    # scrape /metrics twice (the scrape itself is also instrumented).
    client.get("/")
    client.get("/metrics")
    resp = client.get("/metrics")

    assert resp.status_code == 200
    # CONTENT_TYPE_LATEST is the Prometheus text exposition format
    # ("text/plain; version=0.0.4; charset=utf-8").
    assert resp.headers["content-type"].startswith("text/plain")
    assert CONTENT_TYPE_LATEST.startswith("text/plain")

    body = resp.text
    # The request-count metric name must be present (HELP/TYPE lines emit the
    # base name even before any labeled sample, and by now samples exist too).
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


def test_metrics_labels_use_route_template_not_raw_path():
    """Cardinality guard: metrics label on the matched route template / a bounded
    fallback, never the raw request path with its params."""
    client = TestClient(create_app())

    # An unmatched path must not leak into the label set as its raw value.
    client.get("/definitely/not/a/real/route/12345")
    body = client.get("/metrics").text

    assert "/definitely/not/a/real/route/12345" not in body
    assert "__unmatched__" in body
