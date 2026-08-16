"""Headless runner's real MCP client (P6.2, D10).

D10 requires the runner's mutating actions to attribute as
``mcp:headless-runner`` and be independently revocable via its own scoped
key — that only happens for a call that actually goes through the MCP
server's HTTP transport and its ``_authorize()``/``_caller_identity()``
machinery (``mcp_server.py``). An in-process Python call (the pattern
``chat/agent.py``'s ``propose_host_purpose_update`` uses) never touches
that machinery at all and stamps ``direct:unattributed`` instead — the
exact failure mode D10 exists to avoid.

FastMCP's own ``Client`` (built on the official ``mcp`` SDK, already an
installed dependency via the ``fastmcp`` package) handles the streamable-http
transport's stateful protocol (initialize handshake, session id, SSE
framing) — no need to hand-roll a JSON-RPC client the way the infra-ops
plugin's stdlib-only hooks had to (see P5.1's ``state_backend_client.py``
docstring for why hooks couldn't use this same approach: a fresh, one-shot
process under a ~1s fail-open budget can't afford a stateful handshake).
The runner is a long-running backend process, not a one-shot hook, so it
can afford this properly.

Unlike the plugin hooks' D9 fail-open contract, this is NOT fail-open —
a standing task's whole point is to take a real action (file a GitLab
issue); a silently-dropped failure here would defeat the task, so errors
propagate to the caller (``runner/agent_task.py``), which records them on
the ``AgentTaskRun`` row as a failed run instead of swallowing them.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Client

from infra_brain.config import get_settings


class HeadlessRunnerMcpError(RuntimeError):
    """Raised when the MCP server rejects a call or is unreachable."""


async def call_mcp_tool(tool_name: str, **kwargs: Any) -> Any:
    """Call one MCP tool as the headless runner, over real HTTP.

    Args:
        tool_name: the MCP tool name (e.g. ``"create_gitlab_issue"``).
        **kwargs: tool arguments, passed through as the JSON-RPC ``arguments``.

    Returns:
        The tool's structured result (``CallToolResult.data``) — for every
        tool this runner calls, that is the same JSON-safe dict the MCP
        tool function itself returns.

    Raises:
        HeadlessRunnerMcpError: the token is unconfigured, the server is
            unreachable, or the call itself failed (wrong scope, mutations
            disabled, tool-level ``ValueError``, etc.) — never silently
            swallowed; the caller decides how to record the failure.
    """
    settings = get_settings()
    token = settings.headless_runner_mcp_token
    if not token:
        raise HeadlessRunnerMcpError(
            "HEADLESS_RUNNER_MCP_TOKEN is not configured — the headless "
            "runner cannot take any MCP-attributed action"
        )

    try:
        async with Client(settings.headless_runner_mcp_url, auth=token) as client:
            result = await client.call_tool(tool_name, kwargs)
    except Exception as exc:  # noqa: BLE001 - normalize every failure mode to one type
        raise HeadlessRunnerMcpError(f"MCP call to {tool_name!r} failed: {exc}") from exc

    if result.is_error:
        raise HeadlessRunnerMcpError(f"MCP tool {tool_name!r} returned an error: {result.data}")
    return result.data


__all__ = ["call_mcp_tool", "HeadlessRunnerMcpError"]
