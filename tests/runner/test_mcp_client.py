"""Tests for runner/mcp_client.py (P6.2, D10).

fastmcp.Client itself is mocked -- no live MCP server in tests. Verifies the
headless runner's calls are shaped correctly (bearer auth, tool name,
arguments) and that every failure mode normalizes to HeadlessRunnerMcpError.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infra_brain.runner.mcp_client import HeadlessRunnerMcpError, call_mcp_tool


def _mock_client(result_data=None, is_error=False, raises=None):
    mock_result = MagicMock(data=result_data, is_error=is_error)
    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_client_instance
    mock_client_instance.__aexit__.return_value = False
    if raises is not None:
        mock_client_instance.call_tool.side_effect = raises
    else:
        mock_client_instance.call_tool.return_value = mock_result
    return mock_client_instance


@pytest.mark.asyncio
async def test_call_mcp_tool_unconfigured_token_raises(monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.setenv("HEADLESS_RUNNER_MCP_TOKEN", "")
    get_settings.cache_clear()
    with pytest.raises(HeadlessRunnerMcpError, match="not configured"):
        await call_mcp_tool("create_gitlab_issue", project_id=1, title="x")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_call_mcp_tool_success(monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.setenv("HEADLESS_RUNNER_MCP_TOKEN", "ibmcp_test")
    monkeypatch.setenv("HEADLESS_RUNNER_MCP_URL", "http://mcp:8002/mcp")
    get_settings.cache_clear()

    mock_instance = _mock_client(result_data={"issue_iid": 42, "web_url": "https://x"})
    with patch("infra_brain.runner.mcp_client.Client", return_value=mock_instance) as MockClient:
        result = await call_mcp_tool("create_gitlab_issue", project_id=1, title="Drift found")

    assert result == {"issue_iid": 42, "web_url": "https://x"}
    MockClient.assert_called_once_with("http://mcp:8002/mcp", auth="ibmcp_test")
    mock_instance.call_tool.assert_called_once_with(
        "create_gitlab_issue", {"project_id": 1, "title": "Drift found"}
    )
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_call_mcp_tool_transport_failure_raises_headless_error(monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.setenv("HEADLESS_RUNNER_MCP_TOKEN", "ibmcp_test")
    get_settings.cache_clear()

    mock_instance = _mock_client(raises=ConnectionError("connection refused"))
    with patch("infra_brain.runner.mcp_client.Client", return_value=mock_instance):
        with pytest.raises(HeadlessRunnerMcpError, match="failed"):
            await call_mcp_tool("create_gitlab_issue", project_id=1, title="x")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_call_mcp_tool_error_result_raises(monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.setenv("HEADLESS_RUNNER_MCP_TOKEN", "ibmcp_test")
    get_settings.cache_clear()

    mock_instance = _mock_client(result_data={"error": "key_lacks_scope"}, is_error=True)
    with patch("infra_brain.runner.mcp_client.Client", return_value=mock_instance):
        with pytest.raises(HeadlessRunnerMcpError, match="returned an error"):
            await call_mcp_tool("seed_resource", hostname="x")
    get_settings.cache_clear()
