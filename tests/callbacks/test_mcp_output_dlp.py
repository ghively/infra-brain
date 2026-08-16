"""TRK-161: MCP output DLP scrub — PAN-shaped substrings in an MCP tool's
return value must be redacted before the result reaches the MCP client.

Scope (deliberate, see task-16 brief): this closes the PAN gap only, reusing
the existing infra_brain.callbacks.dlp.redact_pans() primitive. It does NOT
add AWS-key/private-key/JWT detection — no such patterns exist anywhere in
dlp.py today, and adding them is a separate scope expansion.

Fail-open by design: a bug in the scrub itself must never turn a legitimate
MCP tool response into a broken one. This is deliberately different from
DLPCallbackHandler's fail-closed *denial* behavior for LangChain agent tool
calls (dlp_fail_closed) — that path assumes a human reviews the run; an MCP
client mid-request has no such review loop.

Uses fastmcp's in-memory Client transport (same pattern as
tests/test_mcp_audit_middleware.py) rather than hand-built context/result
stubs — `call_next(context)` in production returns a fastmcp `ToolResult`
(pydantic model with `.content: list[ContentBlock]` and
`.structured_content: dict | None`, per `Middleware.on_call_tool`'s
`CallNext[mt.CallToolRequestParams, ToolResult]` signature, confirmed
against the installed fastmcp==3.4.2 source), not a bare dict — driving a
real tool call through a real FastMCP server is the only way to exercise
the actual shape rather than guessing it.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client, FastMCP

from infra_brain.mcp_audit_middleware import McpAuditMiddleware

_TEST_PAN = "4111111111111111"  # Luhn-valid Visa test number
# Luhn-invalid 16-digit run — reuses the same false-positive guard
# tests/callbacks/test_dlp.py already proves for redact_pans(): a real
# posture tool must not have unrelated 16-digit identifiers mangled.
_NON_PAN = "1234567890123456"


def _make_test_server() -> FastMCP:
    mcp = FastMCP("test-server")
    mcp.add_middleware(McpAuditMiddleware())

    @mcp.tool
    def notes_tool() -> dict:
        return {"notes": f"card on file {_TEST_PAN} for this asset"}

    @mcp.tool
    def serial_tool() -> dict:
        return {"serial": _NON_PAN}

    return mcp


@pytest.mark.asyncio
async def test_pan_in_tool_output_gets_redacted():
    mcp = _make_test_server()
    mock_session = MagicMock()
    with patch("infra_brain.mcp_audit_middleware.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        async with Client(mcp) as client:
            result = await client.call_tool("notes_tool", {})

    assert _TEST_PAN not in str(result.data)
    assert _TEST_PAN not in result.content[0].text
    assert "REDACTED" in result.content[0].text


@pytest.mark.asyncio
async def test_non_pan_numbers_pass_through_untouched():
    mcp = _make_test_server()
    mock_session = MagicMock()
    with patch("infra_brain.mcp_audit_middleware.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        async with Client(mcp) as client:
            result = await client.call_tool("serial_tool", {})

    assert result.data == {"serial": _NON_PAN}
    assert _NON_PAN in result.content[0].text


@pytest.mark.asyncio
async def test_scan_failure_fails_open_not_closed(monkeypatch):
    """A bug in the scrub itself must never break a legitimate MCP response."""
    import infra_brain.mcp_audit_middleware as mam

    monkeypatch.setattr(mam, "redact_pans_preserving_uuids", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

    mcp = _make_test_server()
    mock_session = MagicMock()
    with patch("infra_brain.mcp_audit_middleware.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        async with Client(mcp) as client:
            result = await client.call_tool("serial_tool", {})

    # Scrub raised internally but the tool call must still succeed with the
    # original, unscrubbed data — never a 500 / broken response.
    assert result.data == {"serial": _NON_PAN}
