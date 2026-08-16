"""MCP call audit-trail middleware (#90 design / #80 implementation).

The MCP server has 19 @mcp.tool functions, 18 of which are plain functions
with no LangChain Runnable in the loop — callbacks/registry.py's
build_callbacks() is shaped for LangChain callback handlers and does not
attach to them. This middleware is the MCP-protocol-level equivalent:
one fastmcp.server.middleware.Middleware subclass, registered once via
mcp.add_middleware(...), that wraps every tools/call dispatch uniformly and
writes one AgentActionLog row per call — reusing the existing table (see
db/models/governance.py's caller_key_hash column and its docstring for why
no new table/FK was introduced).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid as _uuid_mod

from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from infra_brain.callbacks.dlp import redact_pans_preserving_uuids
from infra_brain.db.models import AgentActionLog
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

_ARGS_SUMMARY_MAX_LEN = 2000


def _caller_key_hash() -> str | None:
    """sha256 of the raw bearer token, or None if absent (unauthenticated /
    dev-mode). Matches the hashing convention already used by
    dashboard_auth.py and specified for the future McpApiKey.token_hash."""
    headers = get_http_headers(include={"authorization"})
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[len("bearer ") :].strip()
    if not token:
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def _summarize_args(arguments: dict | None) -> str:
    """Serialized tool arguments, PAN-scrubbed, capped at _ARGS_SUMMARY_MAX_LEN.

    This lands in AgentActionLog.args_summary, which the read-only
    ``get_audit_log`` MCP tool serves back verbatim — so a card number pasted
    into any tool argument would otherwise be persisted and replayed in the
    clear. redact_pans() is applied to the SERIALIZED text (not per-value) so
    it covers nested structures and non-str scalars uniformly; truncation
    happens after redaction so a mask can never be cut in half into something
    that reads like a partial PAN.
    """
    if not arguments:
        return ""
    try:
        text = json.dumps(arguments, default=str)
    except TypeError:
        text = str(arguments)
    return redact_pans_preserving_uuids(text)[:_ARGS_SUMMARY_MAX_LEN]


def _scrub_pans_in_result(result):
    """Best-effort PAN redaction over an MCP tool's return value (TRK-161).

    In production `call_next(context)` returns a fastmcp `ToolResult` —
    a pydantic model with `.content` (a list of ContentBlock, e.g.
    TextContent with a `.text` str) and `.structured_content` (a plain
    JSON-able dict or None), per `Middleware.on_call_tool`'s
    `CallNext[mt.CallToolRequestParams, ToolResult]` signature (fastmcp
    3.4.2). Both fields are mutated in place (neither ToolResult nor
    TextContent is frozen) so `.meta`/`.is_error` survive untouched.

    The recursive dict/list/str branches below are a defensive fallback for
    any other shape (tests calling this directly, or a future fastmcp
    version) — not the primary production path.

    Scope is PAN-only, reusing the existing redact_pans() primitive — no
    AWS-key/private-key/JWT detection, since no such patterns exist anywhere
    in dlp.py today and adding them is a separate scope expansion. Fail-open
    by design — see the caller's except block.
    """
    if isinstance(result, ToolResult):
        for block in result.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                block.text = redact_pans_preserving_uuids(text)
        if result.structured_content is not None:
            result.structured_content = _scrub_pans_in_result(result.structured_content)
        return result
    if isinstance(result, str):
        return redact_pans_preserving_uuids(result)
    if isinstance(result, dict):
        return {k: _scrub_pans_in_result(v) for k, v in result.items()}
    if isinstance(result, list):
        return [_scrub_pans_in_result(v) for v in result]
    return result


class McpAuditMiddleware(Middleware):
    """Writes one AgentActionLog row per MCP tool call.

    Best-effort: a DB failure while writing the audit row is logged and
    swallowed, never allowed to turn a successful (or already-failed) tool
    call into a different outcome for the caller.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        tool_name = getattr(context.message, "name", "unknown")
        arguments = getattr(context.message, "arguments", None)
        start = time.perf_counter()
        status = "ok"
        error_text: str | None = None
        try:
            result = await call_next(context)
            try:
                result = _scrub_pans_in_result(result)
            except Exception:
                # Fail-OPEN: a bug in the scrub itself must never turn a
                # legitimate MCP tool response into a broken one. This is
                # deliberately different from DLPCallbackHandler's
                # fail-closed *denial* behavior (dlp_fail_closed) for
                # LangChain agent tool calls, which assumes a human is in
                # the loop reviewing the run — an MCP client mid-request
                # is not.
                logger.exception(
                    "McpAuditMiddleware: output DLP scrub failed for tool=%s — "
                    "returning result unscrubbed (fail-open)",
                    tool_name,
                    extra={"tool": tool_name},
                )
            return result
        except Exception as exc:
            status = "error"
            error_text = str(exc)
            raise
        finally:
            latency_ms = (time.perf_counter() - start) * 1000
            # _record() opens a sync SQLAlchemy session and calls session.commit()
            # directly — a blocking DB write. Offload it to a worker thread so it
            # never blocks the event loop (CLAUDE.md constraint #2/#3; matches the
            # asyncio.to_thread(...) pattern mcp_server.py's _ApiKeyAuthMiddleware
            # already uses for its own sync DB auth check).
            await asyncio.to_thread(
                self._record, tool_name, arguments, status, error_text, latency_ms
            )

    def _record(
        self,
        tool_name: str,
        arguments: dict | None,
        status: str,
        error_text: str | None,
        latency_ms: float,
    ) -> None:
        try:
            from infra_brain.mcp_metrics import record_mcp_tool_call

            record_mcp_tool_call(tool_name, status, latency_ms / 1000.0)
        except Exception:
            logger.exception(
                "McpAuditMiddleware: failed to record Prometheus metrics for tool=%s",
                tool_name,
                extra={"tool": tool_name},
            )
        try:
            caller_hash = _caller_key_hash()
        except Exception:
            caller_hash = None  # header extraction failing must not block the audit write
        try:
            with get_session() as session:
                session.add(
                    AgentActionLog(
                        id=_uuid_mod.uuid4(),
                        agent="mcp",
                        domain="mcp",
                        tool=tool_name,
                        args_summary=_summarize_args(arguments),
                        verdict="allow",
                        status=status,
                        error=error_text,
                        latency_ms=latency_ms,
                        caller_key_hash=caller_hash,
                    )
                )
                session.commit()
        except Exception:
            logger.exception(
                "McpAuditMiddleware: failed to record audit row for tool=%s",
                tool_name,
                extra={"tool": tool_name},
            )
