"""Tests for McpAuditMiddleware (#90 design / #80 implementation).

Uses fastmcp's in-memory Client transport (Client(transport=<FastMCP
instance>)) — no HTTP, no uvicorn — to exercise the middleware exactly as
it runs in production, verified against the installed fastmcp==3.4.2 API.
"""

import hashlib
import threading
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client, FastMCP

from infra_brain.mcp_audit_middleware import (
    _ARGS_SUMMARY_MAX_LEN,
    McpAuditMiddleware,
    _summarize_args,
)


def _make_test_server() -> FastMCP:
    mcp = FastMCP("test-server")
    mcp.add_middleware(McpAuditMiddleware())

    @mcp.tool
    def ok_tool(x: int) -> dict:
        return {"doubled": x * 2}

    @mcp.tool
    def boom_tool() -> dict:
        raise RuntimeError("kaboom")

    return mcp


@pytest.mark.asyncio
async def test_successful_call_writes_ok_agent_action_log_row():
    mcp = _make_test_server()
    mock_session = MagicMock()
    with patch("infra_brain.mcp_audit_middleware.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        async with Client(mcp) as client:
            result = await client.call_tool("ok_tool", {"x": 21})

    assert result.data == {"doubled": 42}
    mock_session.add.assert_called_once()
    row = mock_session.add.call_args[0][0]
    assert row.agent == "mcp"
    assert row.tool == "ok_tool"
    assert row.status == "ok"
    assert row.verdict == "allow"
    assert row.latency_ms >= 0
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_tool_exception_writes_error_row_and_still_propagates():
    mcp = _make_test_server()
    mock_session = MagicMock()
    with patch("infra_brain.mcp_audit_middleware.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        async with Client(mcp) as client:
            with pytest.raises(Exception):  # fastmcp wraps tool exceptions in a ToolError
                await client.call_tool("boom_tool", {})

    mock_session.add.assert_called_once()
    row = mock_session.add.call_args[0][0]
    assert row.tool == "boom_tool"
    assert row.status == "error"
    assert "kaboom" in (row.error or "")


@pytest.mark.asyncio
async def test_caller_key_hash_is_none_when_no_authorization_header():
    mcp = _make_test_server()
    mock_session = MagicMock()
    with (
        patch("infra_brain.mcp_audit_middleware.get_session") as mock_get_session,
        patch("infra_brain.mcp_audit_middleware.get_http_headers", return_value={}),
    ):
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        async with Client(mcp) as client:
            await client.call_tool("ok_tool", {"x": 1})

    row = mock_session.add.call_args[0][0]
    assert row.caller_key_hash is None


@pytest.mark.asyncio
async def test_caller_key_hash_is_sha256_of_bearer_token():
    mcp = _make_test_server()
    mock_session = MagicMock()
    with (
        patch("infra_brain.mcp_audit_middleware.get_session") as mock_get_session,
        patch(
            "infra_brain.mcp_audit_middleware.get_http_headers",
            return_value={"authorization": "Bearer sometoken123"},
        ),
    ):
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        async with Client(mcp) as client:
            await client.call_tool("ok_tool", {"x": 1})

    row = mock_session.add.call_args[0][0]
    expected = hashlib.sha256(b"sometoken123").hexdigest()
    assert row.caller_key_hash == expected


@pytest.mark.asyncio
async def test_db_failure_while_recording_audit_row_does_not_break_the_tool_call():
    """The audit write is best-effort — a DB blip must not turn a successful
    tool call into a failed one from the caller's perspective."""
    mcp = _make_test_server()
    with patch(
        "infra_brain.mcp_audit_middleware.get_session", side_effect=RuntimeError("db down")
    ):
        async with Client(mcp) as client:
            result = await client.call_tool("ok_tool", {"x": 5})
    assert result.data == {"doubled": 10}


@pytest.mark.asyncio
async def test_record_is_offloaded_to_a_worker_thread_not_the_event_loop():
    """The sync SQLAlchemy session/commit inside _record() must run via
    asyncio.to_thread(...), not directly on the event loop thread — a
    blocking DB write on the loop thread would stall every concurrent
    MCP tool call for the duration of the INSERT (CLAUDE.md constraint
    #2/#3; same fix already applied to mcp_server.py's
    _ApiKeyAuthMiddleware for its sync DB auth check)."""
    mcp = _make_test_server()
    mock_session = MagicMock()
    record_thread_ident: list[int] = []

    real_record = McpAuditMiddleware._record

    def _spy_record(self, *args, **kwargs):
        record_thread_ident.append(threading.get_ident())
        return real_record(self, *args, **kwargs)

    with (
        patch("infra_brain.mcp_audit_middleware.get_session") as mock_get_session,
        patch.object(McpAuditMiddleware, "_record", _spy_record),
    ):
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        async with Client(mcp) as client:
            result = await client.call_tool("ok_tool", {"x": 7})

    assert result.data == {"doubled": 14}
    assert len(record_thread_ident) == 1
    # asyncio.to_thread() runs the target in a worker thread, never the
    # thread running the event loop.
    assert record_thread_ident[0] != threading.get_ident()
    mock_session.add.assert_called_once()
    mock_session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# args_summary is PAN-scrubbed before it is persisted
# ---------------------------------------------------------------------------

# Luhn-valid Visa test number — redact_pans() only masks probable PANs.
_TEST_PAN = "4111111111111111"


def test_summarize_args_redacts_pans():
    """args_summary is served back verbatim by the read-only get_audit_log MCP
    tool, so a card number pasted into any tool argument must not be stored in
    the clear."""
    out = _summarize_args({"note": f"card {_TEST_PAN} here"})

    assert _TEST_PAN not in out
    assert "card" in out


def test_summarize_args_redacts_pans_nested_in_structures():
    out = _summarize_args({"correlated": {"evidence": [f"pan {_TEST_PAN}"]}})

    assert _TEST_PAN not in out


def test_summarize_args_truncates_after_redaction():
    """Redaction happens BEFORE the 2000-char cap, so a mask can never be cut
    in half into something that reads like a partial PAN."""
    out = _summarize_args({"blob": "x" * 5000})

    assert len(out) == _ARGS_SUMMARY_MAX_LEN


def test_summarize_args_leaves_non_pan_digits_alone():
    """Infra identifiers (asset ids, ports, IPs) must survive untouched."""
    out = _summarize_args({"host": "10.20.30.40", "port": 8443, "asset": "1234567890123"})

    assert "10.20.30.40" in out
    assert "8443" in out
    assert "1234567890123" in out
