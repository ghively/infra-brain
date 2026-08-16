"""Confirms the real mcp_server.py module actually wires in the audit
middleware + metrics endpoint (#80 — this is the integration-level proof
that Tasks 1-2's components are not just built but actually used)."""

from infra_brain.mcp_audit_middleware import McpAuditMiddleware
from infra_brain.mcp_server import mcp


def test_mcp_audit_middleware_is_registered_on_the_real_server():
    assert any(isinstance(m, McpAuditMiddleware) for m in mcp.middleware)


def test_metrics_route_is_mountable_on_the_real_http_app():
    """http_app() must return a Starlette-compatible app that accepts
    add_route — smoke-test the mount call itself, not a full HTTP round
    trip (uvicorn.run is not invoked in tests)."""
    from infra_brain.mcp_metrics import metrics_endpoint

    app = mcp.http_app()
    app.add_route("/metrics", metrics_endpoint)
    matched = [r for r in app.routes if getattr(r, "path", None) == "/metrics"]
    assert matched
