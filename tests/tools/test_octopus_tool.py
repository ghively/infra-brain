"""Tests for structured Octopus Deploy @tool functions."""

import pytest
from unittest.mock import patch, MagicMock
from infra_brain.tools.octopus_tool import (
    octopus_get,
    octopus_server_info_tool,
    octopus_machines_tool,
    octopus_environments_tool,
    octopus_lifecycles_tool,
    octopus_deployments_tool,
    octopus_projects_tool,
    octopus_projectgroups_tool,
    octopus_channels_tool,
    octopus_releases_tool,
    octopus_dashboard_tool,
    octopus_get_paginated,
    octopus_variableset_tool,
    octopus_library_variable_sets_tool,
)
import httpx as _httpx_oc
import pytest as _pytest_oc
from langchain_core.tools import ToolException as _ToolException_oc


def _resp(data):
    r = MagicMock()
    r.json.return_value = data
    r.raise_for_status = MagicMock()
    return r


@pytest.fixture(autouse=True)
def _octopus_configured():
    """Every test in this file exercises "Octopus IS configured and reachable"
    parsing logic; the real Settings singleton has octopus_url="" in this test
    env, which (correctly, since _api_path's config guard was added — see
    test_octopus_url_unconfigured_raises_clear_error below) now raises before
    ever reaching the mocked readonly_get. Individual tests still override
    this with their own nested `with patch("...octopus_tool._settings")` for
    settings the fixture doesn't set (api_page_size etc.) — that inner patch
    simply shadows this one for its duration."""
    with patch("infra_brain.tools.octopus_tool._settings") as mock_settings:
        mock_settings.return_value.octopus_url = "http://octopus.example.com"
        mock_settings.return_value.octopus_api_key = "key"
        mock_settings.return_value.octopus_ssl_verify = True
        mock_settings.return_value.api_timeout_seconds = 30
        mock_settings.return_value.api_page_size = 100
        yield mock_settings


def test_octopus_machines_tool_returns_items():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {
                "Items": [{"Id": "Machines-1", "Name": "web-01", "Status": "Online"}],
                "TotalResults": 1,
            }
        )
        result = octopus_machines_tool.invoke({})
    assert result[0]["Name"] == "web-01"


def test_octopus_environments_tool_returns_list():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {
                "Items": [{"Id": "Environments-1", "Name": "Production"}],
                "TotalResults": 1,
            }
        )
        result = octopus_environments_tool.invoke({})
    assert result[0]["Name"] == "Production"


def test_octopus_server_info_returns_version():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp({"Version": "3.3.8", "Edition": "Community"})
        result = octopus_server_info_tool.invoke({})
    assert result["Version"] == "3.3.8"


def test_octopus_server_info_returns_edition():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp({"Version": "3.3.8", "Edition": "Community"})
        result = octopus_server_info_tool.invoke({})
    assert result["Edition"] == "Community"


def test_octopus_lifecycles_tool_returns_list():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {
                "Items": [{"Id": "Lifecycles-1", "Name": "Default Lifecycle"}],
                "TotalResults": 1,
            }
        )
        result = octopus_lifecycles_tool.invoke({})
    assert result[0]["Name"] == "Default Lifecycle"


def test_octopus_deployments_tool_returns_items():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {
                "Items": [
                    {
                        "Id": "Deployments-1",
                        "ProjectId": "Projects-1",
                        "EnvironmentId": "Environments-1",
                        "State": "Success",
                    }
                ],
                "TotalResults": 1,
            }
        )
        result = octopus_deployments_tool.invoke({"project_id": "Projects-1", "take": 5})
    assert result["Items"][0]["State"] == "Success"


def test_octopus_projects_tool_returns_list():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {
                "Items": [{"Id": "Projects-1", "Name": "Deploy Me", "Slug": "deploy-me"}],
                "TotalResults": 1,
            }
        )
        result = octopus_projects_tool.invoke({})
    assert result[0]["Name"] == "Deploy Me"


def test_octopus_projectgroups_tool_returns_list():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {
                "Items": [{"Id": "ProjectGroups-1", "Name": "Default Project Group"}],
                "TotalResults": 1,
            }
        )
        result = octopus_projectgroups_tool.invoke({})
    assert result[0]["Name"] == "Default Project Group"


def test_octopus_channels_tool_returns_list():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {
                "Items": [{"Id": "Channels-1", "Name": "Default", "IsDefault": True}],
                "TotalResults": 1,
            }
        )
        result = octopus_channels_tool.invoke({"project_id": "Projects-1"})
    assert result[0]["Name"] == "Default"
    # Pre-Spaces flat per-project channels path.
    called_url = mock_get.call_args[0][0]
    assert "/api/projects/Projects-1/channels" in called_url


def test_octopus_releases_tool_caps_take():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {"Items": [{"Id": "Releases-1", "Version": "1.0.0"}], "TotalResults": 1}
        )
        result = octopus_releases_tool.invoke({"project_id": "Projects-1", "take": 5})
    assert result["Items"][0]["Version"] == "1.0.0"
    called_url = mock_get.call_args[0][0]
    assert "/api/projects/Projects-1/releases" in called_url
    assert "take=5" in called_url


def test_octopus_dashboard_tool_returns_items():
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {
                "Items": [
                    {
                        "Id": "Deployments-1",
                        "ProjectId": "Projects-1",
                        "EnvironmentId": "Environments-1",
                        "ReleaseVersion": "1.0.0",
                        "State": "Success",
                        "CompletedTime": "2018-04-03T19:34:12.308+00:00",
                    }
                ],
                "Projects": [],
                "Environments": [],
            }
        )
        result = octopus_dashboard_tool.invoke({})
    assert result["Items"][0]["State"] == "Success"
    called_url = mock_get.call_args[0][0]
    assert "/api/dashboard" in called_url


def test_octopus_get_paginated_collects_all_pages():
    """Verify pagination stops after total is exhausted."""
    page1 = {"Items": [{"Id": "M-1"}], "TotalResults": 2}
    page2 = {"Items": [{"Id": "M-2"}], "TotalResults": 2}
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.side_effect = [_resp(page1), _resp(page2)]
        # Use a page_size of 1 to force two pages
        result = octopus_get_paginated("/api/machines", page_size=1)
    assert len(result) == 2
    assert result[0]["Id"] == "M-1"
    assert result[1]["Id"] == "M-2"


def test_octopus_get_uses_flat_api_path():
    """Flat /api/ paths are used (pre-Spaces deployment)."""
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp({"Version": "3.3.8"})
        octopus_get("/api/")
    called_url = mock_get.call_args[0][0]
    assert "/api/" in called_url
    # Must NOT inject Spaces prefix
    assert "Spaces-" not in called_url


def test_octopus_machines_tool_paginates_all():
    """machines tool now uses pagination and returns all items as a flat list."""
    page1 = {
        "Items": [{"Id": "Machines-1", "Name": "web-01", "Status": "Online"}],
        "TotalResults": 2,
    }
    page2 = {
        "Items": [{"Id": "Machines-2", "Name": "db-02", "Status": "Offline"}],
        "TotalResults": 2,
    }
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.side_effect = [_resp(page1), _resp(page2)]
        with patch("infra_brain.tools.octopus_tool._settings") as mock_settings:
            mock_settings.return_value.api_page_size = 1
            mock_settings.return_value.octopus_url = "http://octopus.example.com"
            mock_settings.return_value.octopus_api_key = "key"
            mock_settings.return_value.octopus_ssl_verify = False
            mock_settings.return_value.api_timeout_seconds = 30
            result = octopus_machines_tool.invoke({})
    assert len(result) == 2
    assert result[0]["Name"] == "web-01"
    assert result[1]["Name"] == "db-02"


def test_octopus_get_paginated_skips_bad_json_page():
    """A page that returns truncated/corrupt JSON (JSONDecodeError) is skipped and
    pagination continues — so a single bad Octopus 3.3.8 event does not abort
    the entire events collection.  Items from good pages are still returned.
    """
    import json

    page_ok_1 = {"Items": [{"Id": "Events-1"}], "TotalResults": 3}
    page_ok_2 = {"Items": [{"Id": "Events-3"}], "TotalResults": 3}

    bad_resp = MagicMock()
    bad_resp.raise_for_status = MagicMock()
    bad_resp.json.side_effect = json.JSONDecodeError("Unterminated string", "", 0)

    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.side_effect = [
            _resp(page_ok_1),  # skip=0  → 1 item
            bad_resp,  # skip=1  → JSONDecodeError → step past, retry skip=2
            _resp(page_ok_2),  # skip=2  → 1 item, total reached
        ]
        with patch("infra_brain.tools.octopus_tool._settings") as mock_settings:
            mock_settings.return_value.api_page_size = 1
            mock_settings.return_value.octopus_url = "http://octopus.example.com"
            mock_settings.return_value.octopus_api_key = "key"
            mock_settings.return_value.octopus_ssl_verify = False
            mock_settings.return_value.api_timeout_seconds = 30
            result = octopus_get_paginated("/api/events", page_size=1)

    # Both good pages collected; the bad middle page is skipped.
    ids = [r["Id"] for r in result]
    assert "Events-1" in ids
    assert "Events-3" in ids
    # Total requested: 3 HTTP calls (page1, bad, page2)
    assert mock_get.call_count == 3


def test_octopus_get_paginated_bad_json_recovery_uses_actual_page_width():
    """When Octopus 3.3.8 clamps every page to a server-default width smaller than
    the requested ``take`` (e.g. clamps to 30 while 100 was requested), the
    JSON-decode-error recovery must jump by the *observed* page width (30), not
    the requested ``size`` (100) — otherwise it silently skips the un-fetched
    items between the two (a larger, silent under-collection than a single bad
    page would suggest).
    """
    import json

    # First page: server clamps to 30 items even though take=100 was requested.
    page_ok_1 = {"Items": [{"Id": f"Events-{i}"} for i in range(30)], "TotalResults": 61}
    # Third page (after recovery): the final item, reached only if skip advanced
    # to 60 (30 + observed width of 30), not 130 (30 + requested size of 100).
    page_ok_2 = {"Items": [{"Id": "Events-final"}], "TotalResults": 61}

    bad_resp = MagicMock()
    bad_resp.raise_for_status = MagicMock()
    bad_resp.json.side_effect = json.JSONDecodeError("Unterminated string", "", 0)

    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.side_effect = [
            _resp(page_ok_1),  # skip=0, take=100 → server clamps to 30 items
            bad_resp,  # skip=30 → JSONDecodeError
            _resp(page_ok_2),  # skip should be 60 (30 + observed width 30), not 130
        ]
        with patch("infra_brain.tools.octopus_tool._settings") as mock_settings:
            mock_settings.return_value.api_page_size = 100
            mock_settings.return_value.octopus_url = "http://octopus.example.com"
            mock_settings.return_value.octopus_api_key = "key"
            mock_settings.return_value.octopus_ssl_verify = False
            mock_settings.return_value.api_timeout_seconds = 30
            result = octopus_get_paginated("/api/events")

    assert mock_get.call_count == 3
    third_call_url = mock_get.call_args_list[2][0][0]
    assert "skip=60" in third_call_url, (
        f"expected recovery to jump by the observed page width (30), landing at "
        f"skip=60, not the requested size (100) landing at skip=130; got {third_call_url!r}"
    )
    ids = [r["Id"] for r in result]
    assert "Events-final" in ids


def test_octopus_get_paginated_aborts_on_too_many_consecutive_bad_pages():
    """After 5 consecutive bad pages the loop aborts to avoid an infinite loop."""
    import json

    bad_resp = MagicMock()
    bad_resp.raise_for_status = MagicMock()
    bad_resp.json.side_effect = json.JSONDecodeError("Unterminated string", "", 0)

    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = bad_resp
        with patch("infra_brain.tools.octopus_tool._settings") as mock_settings:
            mock_settings.return_value.api_page_size = 1
            mock_settings.return_value.octopus_url = "http://octopus.example.com"
            mock_settings.return_value.octopus_api_key = "key"
            mock_settings.return_value.octopus_ssl_verify = False
            mock_settings.return_value.api_timeout_seconds = 30
            result = octopus_get_paginated("/api/events", page_size=1)

    # Should stop after 5 consecutive failures (not loop forever).
    assert mock_get.call_count == 5
    assert result == []


# ---------------------------------------------------------------------------
# TRK-024 — error path raises ToolException (consistency with @tool_http_errors)
# ---------------------------------------------------------------------------
def _oc_status_error_resp(code=404):
    req = _httpx_oc.Request("GET", "http://octopus.example.com/api/")
    resp = _httpx_oc.Response(code, request=req)
    r = MagicMock()
    r.raise_for_status.side_effect = _httpx_oc.HTTPStatusError(
        str(code), request=req, response=resp
    )
    return r


def test_octopus_http_error_raises_toolexception():
    """TRK-024: an httpx HTTP error surfaces as ToolException via @tool_http_errors.
    404 is non-transient so tenacity does not retry it."""
    with patch(
        "infra_brain.tools.octopus_tool.readonly_get",
        return_value=_oc_status_error_resp(404),
    ):
        with _pytest_oc.raises(_ToolException_oc):
            octopus_server_info_tool.invoke({})


# ---------------------------------------------------------------------------
# TRK-152 — Value fields stripped from variable sets at the tool boundary,
# before the DLP callback ever scans the raw tool output. A Luhn-valid-looking
# Value (e.g. a bare 16-digit string) must never survive in the returned dict.
# ---------------------------------------------------------------------------
def test_octopus_variableset_tool_strips_value_field():
    """Value is removed from every variable; other fields are preserved."""
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {
                "Id": "variableset-Projects-303",
                "OwnerId": "Projects-303",
                "Variables": [
                    {
                        "Id": "0838b64c-1b64-4b52-9c5c-b0e5e3b0c111",
                        "Name": "ConnectionString",
                        # Luhn-valid 16-digit run — the live false positive (TRK-152).
                        "Value": "4111111111111111",
                        "Scope": {"Environment": ["Environments-1"]},
                        "IsSensitive": False,
                        "IsEditable": True,
                    },
                    {
                        "Id": "1949b64c-1b64-4b52-9c5c-b0e5e3b0c222",
                        "Name": "ApiKey",
                        "Value": None,  # already masked null by the API
                        "Scope": {},
                        "IsSensitive": True,
                        "IsEditable": True,
                    },
                ],
            }
        )
        result = octopus_variableset_tool.invoke({"variable_set_id": "variableset-Projects-303"})

    assert result["Id"] == "variableset-Projects-303"
    assert len(result["Variables"]) == 2
    for var in result["Variables"]:
        assert "Value" not in var
    # Non-Value fields survive untouched.
    assert result["Variables"][0]["Name"] == "ConnectionString"
    assert result["Variables"][0]["Scope"] == {"Environment": ["Environments-1"]}
    assert result["Variables"][0]["IsSensitive"] is False
    assert result["Variables"][1]["Name"] == "ApiKey"
    assert result["Variables"][1]["IsSensitive"] is True


def test_octopus_variableset_tool_handles_missing_variables_key():
    """A response with no Variables list (unexpected shape) passes through unchanged."""
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp({"Id": "variableset-Projects-505", "OwnerId": "Projects-505"})
        result = octopus_variableset_tool.invoke({"variable_set_id": "variableset-Projects-505"})
    assert result == {"Id": "variableset-Projects-505", "OwnerId": "Projects-505"}


def test_octopus_variableset_tool_handles_empty_variables_list():
    """An empty Variables list stays an empty list, not dropped or errored."""
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp({"Id": "variableset-Projects-1", "Variables": []})
        result = octopus_variableset_tool.invoke({"variable_set_id": "variableset-Projects-1"})
    assert result["Variables"] == []


def test_octopus_library_variable_sets_tool_returns_metadata_only():
    """The estate-wide library variable sets list is SET-level metadata only —
    Id/Name/Description/VariableSetId/ContentType, no per-variable Value fields
    to strip (the /api/libraryvariablesets endpoint does not embed Variables).
    """
    with patch("infra_brain.tools.octopus_tool.readonly_get") as mock_get:
        mock_get.return_value = _resp(
            {
                "Items": [
                    {
                        "Id": "LibraryVariableSets-1",
                        "Name": "Shared Config",
                        "Description": "Shared across projects",
                        "VariableSetId": "variableset-LibraryVariableSets-1",
                        "ContentType": "Variables",
                    }
                ],
                "TotalResults": 1,
            }
        )
        result = octopus_library_variable_sets_tool.invoke({})
    assert result[0]["Name"] == "Shared Config"
    assert "Value" not in result[0]
    assert "Variables" not in result[0]


def test_octopus_url_unconfigured_raises_clear_error():
    """Found via a live DiscoveryAgent run: with OCTOPUS_URL unset, the old
    code built a schemeless URL and httpx/httpcore rejected it deep inside
    the retry loop as an opaque UnsupportedProtocol. Overrides the
    autouse _octopus_configured fixture to assert the real unconfigured case
    raises a clear, immediate ToolException instead."""
    with patch("infra_brain.tools.octopus_tool._settings") as mock_settings:
        mock_settings.return_value.octopus_url = ""
        with _pytest_oc.raises(_ToolException_oc, match="not configured"):
            octopus_projects_tool.invoke({})
