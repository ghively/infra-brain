"""Tests for the MCP server's Prometheus metrics (#90 design / #80 implementation)."""

from infra_brain.mcp_metrics import (
    MCP_AUTH_DENIALS_TOTAL,
    MCP_TOOL_CALLS_TOTAL,
    MCP_TOOL_CALL_DURATION_SECONDS,
    record_mcp_tool_call,
)


def test_record_mcp_tool_call_increments_counter_with_labels():
    before = MCP_TOOL_CALLS_TOTAL.labels(tool="query_resources", status="ok")._value.get()
    record_mcp_tool_call("query_resources", "ok", 0.05)
    after = MCP_TOOL_CALLS_TOTAL.labels(tool="query_resources", status="ok")._value.get()
    assert after == before + 1


def test_record_mcp_tool_call_observes_duration_histogram():
    before_count = MCP_TOOL_CALL_DURATION_SECONDS.labels(tool="seed_resource")._sum.get()
    record_mcp_tool_call("seed_resource", "error", 0.25)
    after_count = MCP_TOOL_CALL_DURATION_SECONDS.labels(tool="seed_resource")._sum.get()
    assert after_count == before_count + 0.25


def test_mcp_auth_denials_total_counts_regardless_of_audit_row_suppression():
    """GitLab #171 follow-up: denial VOLUME stays visible even in the windows
    where the rate limiter suppresses most of those denials' audit rows."""
    before_audited = MCP_AUTH_DENIALS_TOTAL.labels(reason="key_invalid_or_expired", audited="true")._value.get()
    before_suppressed = MCP_AUTH_DENIALS_TOTAL.labels(
        reason="key_invalid_or_expired", audited="false"
    )._value.get()
    MCP_AUTH_DENIALS_TOTAL.labels(reason="key_invalid_or_expired", audited="true").inc()
    MCP_AUTH_DENIALS_TOTAL.labels(reason="key_invalid_or_expired", audited="false").inc()
    assert (
        MCP_AUTH_DENIALS_TOTAL.labels(reason="key_invalid_or_expired", audited="true")._value.get()
        == before_audited + 1
    )
    assert (
        MCP_AUTH_DENIALS_TOTAL.labels(reason="key_invalid_or_expired", audited="false")._value.get()
        == before_suppressed + 1
    )


def test_metrics_endpoint_returns_prometheus_text_format():
    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route

    from infra_brain.mcp_metrics import metrics_endpoint

    record_mcp_tool_call("query_resources", "ok", 0.01)
    app = Starlette(routes=[Route("/metrics", metrics_endpoint)])
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "mcp_tool_calls_total" in resp.text
    assert resp.headers["content-type"].startswith("text/plain")
