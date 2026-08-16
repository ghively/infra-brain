"""Item 1.4 tests — F-004.4: collectors hold GET-only clients by construction."""

from pathlib import Path

import httpx
import pytest

from infra_brain.config import get_settings
from infra_brain.tools.http_readonly import ReadOnlyClient, ReadOnlyHTTPError, readonly_get


def test_post_refused_structurally():
    """POST raises before any network I/O (no transport needed). Fails today:
    the module does not exist."""
    client = ReadOnlyClient()
    with pytest.raises(ReadOnlyHTTPError, match="read-only"):
        client.post("https://example.invalid/x", json={"a": 1})


def test_put_patch_delete_refused():
    client = ReadOnlyClient()
    for method in ("put", "patch", "delete"):
        with pytest.raises(ReadOnlyHTTPError):
            getattr(client, method)("https://example.invalid/x")


def test_get_passes_through():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    client = ReadOnlyClient(transport=transport)
    assert client.get("https://example.invalid/x").json() == {"ok": True}


def test_readonly_get_defaults_timeout_to_settings(monkeypatch):
    """F22: with no explicit timeout, the client must use
    api_timeout_seconds (not None = wait forever)."""
    expected = get_settings().api_timeout_seconds
    captured = {}

    def handler(request):
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    orig_init = ReadOnlyClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(ReadOnlyClient, "__init__", patched_init)
    readonly_get("https://example.invalid/x")
    # httpx expresses the timeout config as per-op seconds
    assert captured["timeout"]["connect"] == float(expected)


def test_readonly_get_explicit_timeout_preserved(monkeypatch):
    """An explicit caller-passed timeout must win over the settings default."""
    captured = {}

    def handler(request):
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    orig_init = ReadOnlyClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(ReadOnlyClient, "__init__", patched_init)
    readonly_get("https://example.invalid/x", timeout=1.5)
    assert captured["timeout"]["connect"] == 1.5


def test_collector_tool_modules_hold_no_raw_httpx():
    """Static lint: the five converted modules must never regress to raw httpx.

    Fails today: all five still call httpx.get / httpx.Client directly.
    """
    root = Path(__file__).resolve().parents[2] / "src" / "infra_brain" / "tools"
    for name in ("gitlab.py", "octopus_tool.py", "rapid7.py", "eol.py", "context7.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "httpx.get(" not in text, f"{name} regressed to raw httpx.get"
        assert "httpx.Client(" not in text, f"{name} regressed to raw httpx.Client"
        assert "httpx.post(" not in text, f"{name} must never POST"


# ---------------------------------------------------------------------------
# TRK-024 — tool_http_errors decorator contract (shared canonical helper)
# ---------------------------------------------------------------------------
def test_tool_http_errors_wraps_httpx_error():
    """httpx transport/HTTP errors become ToolException — the mechanism the four
    HTTP-backed tool modules rely on for consistent error surfacing (TRK-024)."""
    import httpx
    from langchain_core.tools import ToolException
    from infra_brain.tools.http_readonly import tool_http_errors

    @tool_http_errors
    def boom():
        raise httpx.ConnectError("no route to host")

    with pytest.raises(ToolException, match="no route"):
        boom()


def test_tool_http_errors_passes_through_readonly_denial():
    """A ReadOnlyHTTPError (PermissionError read-only denial) must NOT be downgraded
    to a generic ToolException — the R2 boundary gate relies on PermissionError."""
    from infra_brain.tools.http_readonly import tool_http_errors, ReadOnlyHTTPError

    @tool_http_errors
    def denied():
        raise ReadOnlyHTTPError("[read-only] POST refused")

    with pytest.raises(ReadOnlyHTTPError):
        denied()


def test_tool_http_errors_carries_status_code_onto_tool_exception():
    """The HTTP status must survive the ToolException conversion.

    Only the message survived before, so a caller needing to tell "the file
    does not exist" (404) from "the server broke" (5xx) had to substring-match
    httpx's text. inventory_reconcile needs exactly that distinction.
    """
    import httpx
    from langchain_core.tools import ToolException

    from infra_brain.tools.http_readonly import tool_http_errors

    @tool_http_errors
    def _boom():
        request = httpx.Request("GET", "https://example.test/missing.yml")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("404 Not Found", request=request, response=response)

    with pytest.raises(ToolException) as exc_info:
        _boom()
    assert exc_info.value.status_code == 404


def test_tool_http_errors_omits_status_code_for_transport_errors():
    """A transport error never got a response — the attribute stays absent."""
    import httpx
    from langchain_core.tools import ToolException

    from infra_brain.tools.http_readonly import tool_http_errors

    @tool_http_errors
    def _boom():
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ToolException) as exc_info:
        _boom()
    assert getattr(exc_info.value, "status_code", None) is None
