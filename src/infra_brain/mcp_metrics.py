"""Prometheus metrics for the MCP server process (#90 design / #80
implementation). A SECOND, independent metrics surface from main.py's
/metrics — the MCP server runs as its own uvicorn process on port 8002 with
its own process-local default prometheus_client REGISTRY, so there is no
cross-process registry to share or collide with; this mirrors main.py's
existing pattern (default global REGISTRY, served via generate_latest())
rather than inventing a second convention.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response

MCP_TOOL_CALLS_TOTAL = Counter(
    "mcp_tool_calls_total",
    "Total MCP tool calls",
    labelnames=("tool", "status"),
)

MCP_TOOL_CALL_DURATION_SECONDS = Histogram(
    "mcp_tool_call_duration_seconds",
    "MCP tool call latency in seconds",
    labelnames=("tool",),
)

# GitLab #171 follow-up: incremented for every auth-layer deny BEFORE the
# in-process rate limiter decides whether to also write an agent_action_log
# row (mcp_server.py's _record_auth_denial), so total denial VOLUME stays
# observable even in the windows where most of those denials' audit rows are
# suppressed. "audited" distinguishes a denial whose row was actually written
# from one the rate limiter dropped.
MCP_AUTH_DENIALS_TOTAL = Counter(
    "mcp_auth_denials_total",
    "Total MCP auth-layer denials (401/403), before audit-row rate limiting",
    labelnames=("reason", "audited"),
)


def record_mcp_tool_call(tool: str, status: str, duration_seconds: float) -> None:
    """Called once per completed tool call (success or error) from
    McpAuditMiddleware.on_call_tool — see mcp_server.py's wiring (Task 4)."""
    MCP_TOOL_CALLS_TOTAL.labels(tool=tool, status=status).inc()
    MCP_TOOL_CALL_DURATION_SECONDS.labels(tool=tool).observe(duration_seconds)


async def metrics_endpoint(request: Request) -> Response:
    """No-auth, zero-extra-I/O scrape endpoint — same trust tier as main.py's
    /metrics and /healthz (CLAUDE.md constraint #8's spirit: cheap, no DB/
    Redis I/O). Mounted directly onto the StarletteWithLifespan app object
    mcp.http_app() returns (see mcp_server.py's entrypoint, Task 4)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def healthz_endpoint(request: Request) -> Response:
    """No-auth, zero-I/O liveness probe for the container healthcheck
    (TRK-258(1)). Mirrors main.py's /healthz — no DB/Redis check, matching
    CLAUDE.md constraint #8 (liveness must be zero-I/O; only /health checks
    dependencies). Before this existed, the mcp container's healthcheck
    probed the auth-gated /mcp endpoint, which returns 401 on every single
    check — 972+ harmless 401 log entries since the last restart, burying any
    real 401 (e.g. TRK-250's infra-ops MCP connection failures) in the noise.
    Mounted directly onto the StarletteWithLifespan app object mcp.http_app()
    returns, exempted from _ApiKeyAuthMiddleware by path (see mcp_server.py's
    entrypoint and _ApiKeyAuthMiddleware.__call__)."""
    return Response('{"status":"ok"}', media_type="application/json")
