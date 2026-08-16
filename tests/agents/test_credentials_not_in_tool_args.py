"""H-3 regression tests — Grafana/Wazuh credentials must never be passed as
TOOL ARGUMENTS.

``callbacks/audit.py`` persists ``args_summary = input_str[:256]`` VERBATIM
into ``AgentActionLog``/``AuditLog``, both of which are queryable from the
dashboard. DLP only scans for PANs, so nothing redacts a Grafana service-account
token or Wazuh indexer username/password that appears in a tool's input dict —
they land in the audit trail in cleartext, forever.

``agents/secrets_inventory.py`` documents this exact hazard as the reason its
tools read credentials from settings instead of taking them as arguments;
``tools/prometheus_tool.py`` and ``tools/alertmanager_tool.py`` already follow
that pattern. These tests assert the REAL behaviour: what the collector hands
to ``tool.invoke`` — i.e. exactly the dict that becomes ``input_str`` in
``AuditCallbackHandler.on_tool_start`` — carries no credential material.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from infra_brain.agents.grafana import GrafanaAgent
from infra_brain.agents.wazuh import WazuhAgent

GRAFANA_MODULE = "infra_brain.agents.grafana"
WAZUH_MODULE = "infra_brain.agents.wazuh"

GRAFANA_TOKEN = "glsa_TOPSECRETgrafanatoken_0001"  # noqa: S105 — test fixture
WAZUH_INDEXER_PASSWORD = "IndexerTopSecret!42"  # noqa: S105 — test fixture
WAZUH_INDEXER_USERNAME = "wazuh-indexer-admin"
WAZUH_JWT = "eyJhbGciOi.TOPSECRETjwtpayload.signature"  # noqa: S105 — test fixture

MOCK_HEALTH = {"commit": "abc123", "database": "ok", "version": "10.4.1"}
MOCK_AGENTS_PAYLOAD = {"data": {"affected_items": [], "total_affected_items": 0}, "error": 0}
MOCK_INDEXER_ALERTS = {"_shards": {"total": 3}, "hits": {"hits": []}}


def _audit_view(call_args) -> str:
    """Render a recorded ``tool.invoke`` call the way callbacks/audit.py would.

    audit.py receives the tool input as ``input_str`` and stores
    ``input_str[:256]``. Serializing the invoke payload here reproduces what
    would be persisted, so the assertion is against the real leak path rather
    than against the shape of the patch.
    """
    payload = call_args[0][0] if call_args[0] else call_args[1]
    return json.dumps(payload, default=str)


class TestGrafanaTokenNeverInToolArgs:
    @pytest.fixture
    def agent(self, make_agent):
        a = make_agent(GrafanaAgent)
        a.settings.grafana_url = "http://grafana.internal:3002"
        a.settings.grafana_api_token = GRAFANA_TOKEN
        return a

    def test_dashboards_and_alerts_invocations_carry_no_token(self, agent):
        with (
            patch(f"{GRAFANA_MODULE}.grafana_health_tool") as mock_health,
            patch(f"{GRAFANA_MODULE}.grafana_dashboards_tool") as mock_dash,
            patch(f"{GRAFANA_MODULE}.grafana_alerts_tool") as mock_alerts,
        ):
            mock_health.invoke.return_value = MOCK_HEALTH
            mock_dash.invoke.return_value = []
            mock_alerts.invoke.return_value = []
            agent.collect()

        for mock in (mock_health, mock_dash, mock_alerts):
            mock.invoke.assert_called_once()
            recorded = _audit_view(mock.invoke.call_args)
            assert GRAFANA_TOKEN not in recorded, (
                f"{mock} was invoked with the Grafana token in its arguments; "
                f"audit.py would persist it verbatim: {recorded}"
            )
            assert "token" not in json.loads(recorded)

    def test_token_still_gates_the_authenticated_tier(self, agent):
        """Removing the argument must not remove the two-tier self-skip: with
        no token configured, dashboards/alerts are still not attempted."""
        agent.settings.grafana_api_token = ""
        with (
            patch(f"{GRAFANA_MODULE}.grafana_health_tool") as mock_health,
            patch(f"{GRAFANA_MODULE}.grafana_dashboards_tool") as mock_dash,
            patch(f"{GRAFANA_MODULE}.grafana_alerts_tool") as mock_alerts,
        ):
            mock_health.invoke.return_value = MOCK_HEALTH
            outcome = agent.collect()
        mock_dash.invoke.assert_not_called()
        mock_alerts.invoke.assert_not_called()
        assert outcome.errors == []


class TestWazuhCredentialsNeverInToolArgs:
    @pytest.fixture
    def agent(self, make_agent):
        a = make_agent(WazuhAgent)
        a.settings.wazuh_url = "https://wazuh.internal:55000"
        a.settings.wazuh_username = "wazuh-wui"
        a.settings.wazuh_password = "manager-secret"
        a.settings.wazuh_ssl_verify = False
        a.settings.api_timeout_seconds = 30
        a.settings.wazuh_agents_cap = 500
        a.settings.wazuh_alerts_cap = 200
        a.settings.wazuh_alerts_window_hours = 24
        a.settings.wazuh_indexer_url = "https://wazuh.internal:9200"
        a.settings.wazuh_indexer_username = WAZUH_INDEXER_USERNAME
        a.settings.wazuh_indexer_password = WAZUH_INDEXER_PASSWORD
        a.settings.wazuh_indexer_ssl_verify = False
        a.settings.wazuh_indexer_alerts_index = "wazuh-alerts-*"
        return a

    def test_indexer_invocation_carries_no_username_or_password(self, agent):
        with (
            patch(f"{WAZUH_MODULE}.get_wazuh_token", return_value=WAZUH_JWT),
            patch(f"{WAZUH_MODULE}.wazuh_agents_tool") as mock_agents,
            patch(f"{WAZUH_MODULE}.wazuh_indexer_alerts_tool") as mock_indexer,
        ):
            mock_agents.invoke.return_value = MOCK_AGENTS_PAYLOAD
            mock_indexer.invoke.return_value = MOCK_INDEXER_ALERTS
            outcome = agent.collect()

        assert outcome.errors == []  # guard: the indexer path really ran
        mock_indexer.invoke.assert_called_once()
        recorded = _audit_view(mock_indexer.invoke.call_args)
        assert WAZUH_INDEXER_PASSWORD not in recorded, (
            f"indexer password reached the audit trail via tool args: {recorded}"
        )
        assert WAZUH_INDEXER_USERNAME not in recorded
        payload = json.loads(recorded)
        assert "password" not in payload
        assert "username" not in payload

    def test_manager_invocations_carry_no_jwt(self, agent):
        with (
            patch(f"{WAZUH_MODULE}.get_wazuh_token", return_value=WAZUH_JWT),
            patch(f"{WAZUH_MODULE}.wazuh_agents_tool") as mock_agents,
            patch(f"{WAZUH_MODULE}.wazuh_indexer_alerts_tool") as mock_indexer,
        ):
            mock_agents.invoke.return_value = MOCK_AGENTS_PAYLOAD
            mock_indexer.invoke.return_value = MOCK_INDEXER_ALERTS
            outcome = agent.collect()

        assert outcome.errors == []  # guard: the /agents fetch really ran
        mock_agents.invoke.assert_called_once()
        recorded = _audit_view(mock_agents.invoke.call_args)
        assert WAZUH_JWT not in recorded, (
            f"Wazuh JWT reached the audit trail via tool args: {recorded}"
        )
        assert "token" not in json.loads(recorded)

    def test_manager_fallback_alerts_invocation_carries_no_jwt(self, agent):
        """No indexer configured -> Manager-API /alerts fallback path."""
        agent.settings.wazuh_indexer_url = ""
        with (
            patch(f"{WAZUH_MODULE}.get_wazuh_token", return_value=WAZUH_JWT),
            patch(f"{WAZUH_MODULE}.wazuh_agents_tool") as mock_agents,
            patch(f"{WAZUH_MODULE}.wazuh_alerts_tool") as mock_alerts,
        ):
            mock_agents.invoke.return_value = MOCK_AGENTS_PAYLOAD
            mock_alerts.invoke.return_value = MOCK_AGENTS_PAYLOAD
            outcome = agent.collect()

        assert outcome.errors == []  # guard: the Manager-API fallback really ran
        mock_alerts.invoke.assert_called_once()
        recorded = _audit_view(mock_alerts.invoke.call_args)
        assert WAZUH_JWT not in recorded
        assert "token" not in json.loads(recorded)

    def test_auth_failure_still_propagates_out_of_collect(self, agent):
        """The credential move must NOT turn an auth failure into a partial
        run: WazuhAgent's spec says auth failure is a real failure so
        BaseAgent.run() records status="failed"."""
        with patch(
            f"{WAZUH_MODULE}.get_wazuh_token",
            side_effect=RuntimeError("Wazuh authentication failed: 401 Unauthorized"),
        ):
            with pytest.raises(RuntimeError, match="Wazuh authentication failed"):
                agent.collect()
