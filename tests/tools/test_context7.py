"""Tests for Context7 read-only documentation/best-practice lookup tools."""

from unittest.mock import patch, MagicMock
from infra_brain.tools.context7 import context7_docs_tool, context7_resolve_library_tool
import httpx as _httpx_c7
import pytest as _pytest_c7
from langchain_core.tools import ToolException as _ToolException_c7


def _resp(data):
    r = MagicMock()
    r.json.return_value = data
    r.raise_for_status = MagicMock()
    return r


def test_context7_returns_message_when_unconfigured():
    with patch("infra_brain.tools.context7.get_settings") as gs:
        gs.return_value.context7_api_key = ""
        out = context7_docs_tool.invoke({"library_id": "/ansible/ansible", "topic": "inventory"})
    assert "not configured" in out


def test_context7_docs_returns_text():
    with (
        patch("infra_brain.tools.context7.get_settings") as gs,
        patch("infra_brain.tools.context7.readonly_get") as mock_get,
    ):
        gs.return_value.context7_api_key = "k"
        gs.return_value.api_timeout_seconds = 30
        mock_get.return_value = _resp({"content": "Use --syntax-check for read-only validation."})
        out = context7_docs_tool.invoke({"library_id": "/ansible/ansible", "topic": "inventory"})
    assert "syntax-check" in out


def test_context7_resolve_returns_not_configured_when_key_missing():
    with patch("infra_brain.tools.context7.get_settings") as gs:
        gs.return_value.context7_api_key = ""
        out = context7_resolve_library_tool.invoke({"name": "ansible"})
    assert "not configured" in str(out)


def test_context7_resolve_returns_dict_on_success():
    with (
        patch("infra_brain.tools.context7.get_settings") as gs,
        patch("infra_brain.tools.context7.readonly_get") as mock_get,
    ):
        gs.return_value.context7_api_key = "k"
        gs.return_value.api_timeout_seconds = 30
        mock_get.return_value = _resp({"results": [{"id": "/ansible/ansible", "name": "Ansible"}]})
        out = context7_resolve_library_tool.invoke({"name": "ansible"})
    assert "results" in out


def test_context7_docs_with_no_topic():
    with (
        patch("infra_brain.tools.context7.get_settings") as gs,
        patch("infra_brain.tools.context7.readonly_get") as mock_get,
    ):
        gs.return_value.context7_api_key = "testkey"
        gs.return_value.api_timeout_seconds = 30
        mock_get.return_value = _resp({"content": "General docs content."})
        out = context7_docs_tool.invoke({"library_id": "/python/python", "topic": ""})
    assert "General docs" in out


# ---------------------------------------------------------------------------
# TRK-024 — error path raises ToolException (consistency with @tool_http_errors)
# ---------------------------------------------------------------------------
def _c7_status_error_resp(code=404):
    req = _httpx_c7.Request("GET", "https://context7.com/api/v1/x")
    resp = _httpx_c7.Response(code, request=req)
    r = MagicMock()
    r.raise_for_status.side_effect = _httpx_c7.HTTPStatusError(
        str(code), request=req, response=resp
    )
    return r


def test_context7_http_error_raises_toolexception():
    """TRK-024: an httpx HTTP error surfaces as ToolException via @tool_http_errors."""
    with (
        patch("infra_brain.tools.context7.get_settings") as gs,
        patch(
            "infra_brain.tools.context7.readonly_get",
            return_value=_c7_status_error_resp(404),
        ),
    ):
        gs.return_value.context7_api_key = "k"
        gs.return_value.api_timeout_seconds = 30
        with _pytest_c7.raises(_ToolException_c7):
            context7_docs_tool.invoke({"library_id": "/ansible/ansible", "topic": "inventory"})
