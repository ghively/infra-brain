from unittest.mock import patch, MagicMock
from infra_brain.tools.rapid7 import (
    rapid7_assets_tool,
    rapid7_asset_detail_tool,
    rapid7_vulnerabilities_tool,
    rapid7_asset_vulnerabilities_tool,
)
import httpx as _httpx_r7
import pytest as _pytest_r7
from langchain_core.tools import ToolException as _ToolException_r7


def _resp(data):
    r = MagicMock()
    r.json.return_value = data
    r.raise_for_status = MagicMock()
    return r


def _mock_settings(rapid7_url="http://r7.example.com"):
    s = MagicMock()
    s.rapid7_url = rapid7_url
    s.rapid7_username = "user"
    s.rapid7_api_key = "key"
    s.rapid7_ssl_verify = False
    s.api_timeout_seconds = 30
    return s


def test_rapid7_assets_tool_returns_resources():
    with patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_settings()):
        with patch("infra_brain.tools.rapid7.ReadOnlyClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _resp(
                {
                    "resources": [{"id": 1, "hostName": "srv-01", "ip": "10.0.0.1"}],
                    "page": {"totalPages": 1},
                }
            )
            result = rapid7_assets_tool.invoke({"page": 0, "size": 10})
    assert result["resources"][0]["hostName"] == "srv-01"


def test_rapid7_assets_tool_skips_when_unconfigured():
    with patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_settings(rapid7_url="")):
        result = rapid7_assets_tool.invoke({"page": 0, "size": 10})
    assert result == {"resources": [], "error": "rapid7_url not configured"}


def test_rapid7_asset_detail_tool_returns_asset():
    with patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_settings()):
        with patch("infra_brain.tools.rapid7.ReadOnlyClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _resp(
                {"id": 42, "hostName": "srv-42", "ip": "10.0.0.42"}
            )
            result = rapid7_asset_detail_tool.invoke({"asset_id": 42})
    assert result["hostName"] == "srv-42"


def test_rapid7_asset_detail_tool_skips_when_unconfigured():
    with patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_settings(rapid7_url="")):
        result = rapid7_asset_detail_tool.invoke({"asset_id": 1})
    assert result == {"error": "rapid7_url not configured"}


def test_rapid7_vulnerabilities_tool_returns_resources():
    with patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_settings()):
        with patch("infra_brain.tools.rapid7.ReadOnlyClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _resp(
                {
                    "resources": [{"id": "cve-1", "title": "Test CVE", "severity": "Critical"}],
                    "page": {"totalPages": 1},
                }
            )
            result = rapid7_vulnerabilities_tool.invoke({"page": 0, "size": 10})
    assert result["resources"][0]["severity"] == "Critical"


def test_rapid7_vulnerabilities_tool_skips_when_unconfigured():
    with patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_settings(rapid7_url="")):
        result = rapid7_vulnerabilities_tool.invoke({"page": 0, "size": 10})
    assert result == {"resources": [], "error": "rapid7_url not configured"}


def test_rapid7_asset_vulnerabilities_tool_returns_data():
    with patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_settings()):
        with patch("infra_brain.tools.rapid7.ReadOnlyClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _resp(
                {"resources": [{"id": "cve-2"}], "page": {"totalPages": 1}}
            )
            result = rapid7_asset_vulnerabilities_tool.invoke({"asset_id": 5})
    assert result["resources"][0]["id"] == "cve-2"


def test_rapid7_asset_vulnerabilities_tool_skips_when_unconfigured():
    with patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_settings(rapid7_url="")):
        result = rapid7_asset_vulnerabilities_tool.invoke({"asset_id": 1})
    assert result == {"error": "rapid7_url not configured"}


# ---------------------------------------------------------------------------
# TRK-110 — PAN-shaped digit runs are redacted at the tool boundary
# ---------------------------------------------------------------------------


def _call_assets_tool_with(payload):
    with patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_settings()):
        with patch("infra_brain.tools.rapid7.ReadOnlyClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _resp(payload)
            return rapid7_assets_tool.invoke({"page": 0, "size": 10})


def test_pan_shaped_version_string_is_redacted():
    """The live false positive: a bare Luhn-valid, Visa-IIN 13-digit build
    number in a software version field must be redacted before the payload
    leaves the tool — otherwise the DLP callback fail-closes the whole run."""
    payload = {
        "resources": [
            {
                "id": 1,
                "hostName": "web-01",
                "software": [
                    {
                        "product": "acme-agent",
                        "version": "4222222222222",  # Visa test number shape (13, Luhn-valid)
                        "description": "acme-agent 4222222222222",
                    }
                ],
            }
        ],
        "page": {"totalPages": 1},
    }
    out = _call_assets_tool_with(payload)
    sw = out["resources"][0]["software"][0]
    assert "4222222222222" not in sw["version"]
    assert "4222222222222" not in sw["description"]
    assert "REDACTED" in sw["version"]
    # Non-matching fields pass through untouched.
    assert sw["product"] == "acme-agent"
    assert out["resources"][0]["hostName"] == "web-01"


def test_non_pan_digit_runs_pass_through():
    """Luhn-invalid or non-card-IIN digit runs (serials, build ids) survive."""
    payload = {
        "resources": [
            {
                "id": 2,
                "software": [
                    # 13 digits, starts with 9 — no card network matches.
                    {"product": "tool", "version": "9999999999999"},
                ],
            }
        ],
        "page": {"totalPages": 1},
    }
    out = _call_assets_tool_with(payload)
    assert out["resources"][0]["software"][0]["version"] == "9999999999999"


def test_pan_shaped_numeric_value_is_redacted():
    """A PAN-shaped value arriving as a bare JSON number must also be
    neutralized — the DLP callback stringifies the whole payload, so a
    numeric leaf would otherwise re-trigger the daily fail-close."""
    payload = {
        "resources": [{"id": 3, "software": [{"product": "x", "version": 4222222222222}]}],
        "page": {"totalPages": 1},
    }
    out = _call_assets_tool_with(payload)
    v = out["resources"][0]["software"][0]["version"]
    assert "4222222222222" not in str(v)
    assert "REDACTED" in str(v)
    # Ordinary numbers (ids, scores) pass through with type intact.
    assert out["resources"][0]["id"] == 3


# ---------------------------------------------------------------------------
# TRK-024 — error path raises ToolException (consistency with @tool_http_errors)
# ---------------------------------------------------------------------------
def _r7_status_error_resp(code=404):
    req = _httpx_r7.Request("GET", "http://r7.example.com/x")
    resp = _httpx_r7.Response(code, request=req)
    r = MagicMock()
    r.raise_for_status.side_effect = _httpx_r7.HTTPStatusError(
        str(code), request=req, response=resp
    )
    return r


def test_rapid7_http_error_raises_toolexception():
    """TRK-024: an httpx HTTP error surfaces as ToolException via @tool_http_errors.
    404 is non-transient so tenacity does not retry it. Redaction path untouched."""
    with patch("infra_brain.tools.rapid7.get_settings", return_value=_mock_settings()):
        with patch("infra_brain.tools.rapid7.ReadOnlyClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _r7_status_error_resp(404)
            with _pytest_r7.raises(_ToolException_r7):
                rapid7_assets_tool.invoke({"page": 0, "size": 10})
