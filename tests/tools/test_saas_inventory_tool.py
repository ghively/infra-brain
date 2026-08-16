"""Tests for SaaS admin/management API @tool functions (read-only, metadata-only)."""

from unittest.mock import MagicMock, patch

from infra_brain.tools.saas_inventory_tool import (
    saas_api_keys_tool,
    saas_applications_tool,
)


def _resp(data):
    r = MagicMock()
    r.json.return_value = data
    r.raise_for_status = MagicMock()
    return r


def test_saas_applications_tool_returns_empty_list_when_unconfigured():
    """Bug: with saas_admin_url unset (its "" default), _api_path built a
    schemeless relative URL that httpx rejected — the tool raised a
    ToolException instead of returning [] like every sibling read-only tool
    module (backup.py, identity.py, context7.py). This must be a clean
    no-op, not an error, and must never reach readonly_get."""
    with (
        patch("infra_brain.tools.saas_inventory_tool.get_settings") as gs,
        patch("infra_brain.tools.saas_inventory_tool.readonly_get") as mock_get,
    ):
        gs.return_value.saas_admin_url = ""
        result = saas_applications_tool.invoke({})
    assert result == []
    mock_get.assert_not_called()


def test_saas_api_keys_tool_returns_empty_list_when_unconfigured():
    """Same unconfigured-is-a-no-op contract for the api-keys tool."""
    with (
        patch("infra_brain.tools.saas_inventory_tool.get_settings") as gs,
        patch("infra_brain.tools.saas_inventory_tool.readonly_get") as mock_get,
    ):
        gs.return_value.saas_admin_url = ""
        result = saas_api_keys_tool.invoke({"app": "some-app"})
    assert result == []
    mock_get.assert_not_called()


def test_saas_applications_tool_returns_records_when_configured():
    with (
        patch("infra_brain.tools.saas_inventory_tool.get_settings") as gs,
        patch("infra_brain.tools.saas_inventory_tool.readonly_get") as mock_get,
    ):
        gs.return_value.saas_admin_url = "https://saas-admin.example.com"
        gs.return_value.saas_admin_token = "tok"
        gs.return_value.api_timeout_seconds = 30
        mock_get.return_value = _resp(
            {"items": [{"name": "Slack", "vendor": "Salesforce", "client_secret": "shh"}]}
        )
        result = saas_applications_tool.invoke({})
    assert result[0]["name"] == "Slack"
    assert "client_secret" not in result[0]
    mock_get.assert_called_once()


def test_saas_api_keys_tool_returns_records_when_configured():
    with (
        patch("infra_brain.tools.saas_inventory_tool.get_settings") as gs,
        patch("infra_brain.tools.saas_inventory_tool.readonly_get") as mock_get,
    ):
        gs.return_value.saas_admin_url = "https://saas-admin.example.com"
        gs.return_value.saas_admin_token = "tok"
        gs.return_value.api_timeout_seconds = 30
        mock_get.return_value = _resp(
            {"items": [{"scope": "read", "value": "sk-abc123"}]}
        )
        result = saas_api_keys_tool.invoke({"app": "slack"})
    assert result[0]["scope"] == "read"
    assert "value" not in result[0]
    mock_get.assert_called_once()
