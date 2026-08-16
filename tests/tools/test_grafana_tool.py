"""Tests for tools/grafana_tool.py.

H-3: the Grafana API token used to be a TOOL ARGUMENT, which
``callbacks/audit.py`` persisted verbatim (``args_summary = input_str[:256]``)
into the dashboard-queryable ``AgentActionLog``/``AuditLog``. It is now
resolved from settings inside the tool body, matching
``tools/prometheus_tool.py`` / ``tools/alertmanager_tool.py``.

These tests pin the behaviour on both sides of that move: the token must no
longer be accepted as an argument, and it must STILL reach the wire — dropping
it would silently turn every authenticated call into a 401, which is the
obvious way this fix could regress.
"""

from unittest.mock import MagicMock, patch

import pytest

from infra_brain.tools.grafana_tool import (
    grafana_alerts_tool,
    grafana_dashboards_tool,
    grafana_health_tool,
)

MODULE = "infra_brain.tools.grafana_tool"


def _capturing_get(captured: dict):
    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = []
        return resp

    return _fake_get


def _settings(token: str = "glsa_realtoken", grafana_url: str = "http://grafana.test:3002"):
    s = MagicMock()
    s.grafana_api_token = token
    s.grafana_url = grafana_url
    s.api_timeout_seconds = 30
    return s


@pytest.mark.parametrize("tool", [grafana_dashboards_tool, grafana_alerts_tool])
def test_authenticated_tools_still_send_the_bearer_token_from_settings(tool):
    captured: dict = {}
    with (
        patch(f"{MODULE}.readonly_get", side_effect=_capturing_get(captured)),
        patch(f"{MODULE}.get_settings", return_value=_settings("glsa_realtoken")),
    ):
        tool.func(base_url="http://grafana.test:3002")

    assert captured["kwargs"]["headers"] == {"Authorization": "Bearer glsa_realtoken"}


@pytest.mark.parametrize("tool", [grafana_dashboards_tool, grafana_alerts_tool])
def test_authenticated_tools_reject_a_token_argument(tool):
    """The credential must not be re-addable as a parameter by accident."""
    with pytest.raises(TypeError):
        tool.func(base_url="http://grafana.test:3002", token="glsa_leaked")


def test_health_tool_stays_anonymous():
    """/api/health is anonymous-accessible and must send no Authorization
    header even when a token IS configured — that is what keeps GrafanaAgent's
    tier-1 self-skip working on a token-less deployment."""
    captured: dict = {}
    with (
        patch(f"{MODULE}.readonly_get", side_effect=_capturing_get(captured)),
        patch(f"{MODULE}.get_settings", return_value=_settings("glsa_realtoken")),
    ):
        grafana_health_tool.func(base_url="http://grafana.test:3002")

    assert captured["kwargs"]["headers"] == {}
    assert captured["url"].endswith("/api/health")


def test_no_token_configured_sends_no_authorization_header():
    captured: dict = {}
    with (
        patch(f"{MODULE}.readonly_get", side_effect=_capturing_get(captured)),
        patch(f"{MODULE}.get_settings", return_value=_settings("")),
    ):
        grafana_dashboards_tool.func(base_url="http://grafana.test:3002")

    assert captured["kwargs"]["headers"] == {}


# --- MEDIUM-1 — credential/host binding -------------------------------------
#
# ``_auth_headers()`` used to attach the resolved Grafana token to WHATEVER
# base_url the caller passed, with no relationship enforced between the
# credential and the target host. These tools are not in any LLM-facing TOOLS
# list and ReadOnlyToolValidator inspects `inp["url"]` (not `inp["base_url"]`),
# so nothing else backstops a mis-targeted base_url. Each authenticated tool
# must now refuse (loudly) to attach the token when base_url doesn't match
# settings.grafana_url.


@pytest.mark.parametrize("tool", [grafana_dashboards_tool, grafana_alerts_tool])
def test_authenticated_tools_refuse_mismatched_base_url(tool):
    captured: dict = {}
    with (
        patch(f"{MODULE}.readonly_get", side_effect=_capturing_get(captured)),
        patch(f"{MODULE}.get_settings", return_value=_settings("glsa_realtoken")),
    ):
        with pytest.raises(Exception, match="grafana_url"):
            tool.func(base_url="http://attacker.example/grafana")

    # The token must never have reached the HTTP layer at all.
    assert captured == {}


def test_health_tool_ignores_base_url_mismatch():
    """/api/health is anonymous and unauthenticated — it must NOT be gated by
    the host-binding check, since it never attaches a credential in the first
    place (there is nothing to bind)."""
    captured: dict = {}
    with (
        patch(f"{MODULE}.readonly_get", side_effect=_capturing_get(captured)),
        patch(f"{MODULE}.get_settings", return_value=_settings("glsa_realtoken")),
    ):
        grafana_health_tool.func(base_url="http://not-the-configured-host.example")

    assert captured["kwargs"]["headers"] == {}
