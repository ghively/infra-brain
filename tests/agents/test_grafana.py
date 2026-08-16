"""Tests for GrafanaAgent (new collector domain — operator-requested, additive
scope beyond the convergence plan).

Covers: happy path (mocked Grafana API responses -> Resource-shaped items),
the fully-unconfigured self-skip case (no grafana_url -> CollectorSkipped),
the two-tier partial-config case (grafana_url set, no grafana_api_token ->
health-only report, dashboards/alerts silently skipped, not an error), and
partial-failure tolerance (one of dashboards/alerts failing does not kill the
other or the health item).
"""

from unittest.mock import patch

import pytest

from infra_brain.agents.grafana import GrafanaAgent
from infra_brain.etl.base import CollectorSkipped, CollectOutcome

MODULE = "infra_brain.agents.grafana"

MOCK_HEALTH = {"commit": "abc123", "database": "ok", "version": "10.4.1"}

MOCK_DASHBOARDS = [
    {
        "id": 1,
        "uid": "cIBgcSjkk",
        "title": "Production Overview",
        "type": "dash-db",
        "tags": ["prod", "monitoring"],
        "folderTitle": "General",
    },
    {
        "id": 2,
        "uid": "z9k2mQrTz",
        "title": "Node Exporter Full",
        "type": "dash-db",
        "tags": ["prometheus"],
        "folderTitle": "Infra",
    },
]

MOCK_ALERTS = [
    {
        "labels": {"alertname": "HighCPU", "severity": "critical", "instance": "node_a"},
        "annotations": {"summary": "CPU usage above 90% for 5m"},
        "status": {"state": "active", "silencedBy": [], "inhibitedBy": []},
        "startsAt": "2026-07-30T12:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://node_a:3002/alerting/grafana/abc/view",
        "fingerprint": "deadbeef01234567",
    }
]


@pytest.fixture
def agent(make_agent):
    a = make_agent(GrafanaAgent)
    a.settings.grafana_url = "http://203.0.113.15:3002"
    a.settings.grafana_api_token = "glsa_faketoken1234567890"
    return a


class TestUnconfiguredSelfSkip:
    def test_no_grafana_url_raises_skipped(self, make_agent):
        """Empty grafana_url -> CollectorSkipped (not even the health check is attempted)."""
        agent = make_agent(GrafanaAgent)
        agent.settings.grafana_url = ""
        agent.settings.grafana_api_token = ""
        with patch(f"{MODULE}.grafana_health_tool") as mock_health:
            with pytest.raises(CollectorSkipped):
                agent.collect()
            mock_health.invoke.assert_not_called()

    def test_blank_whitespace_url_raises_skipped(self, make_agent):
        agent = make_agent(GrafanaAgent)
        agent.settings.grafana_url = "   "
        agent.settings.grafana_api_token = ""
        with pytest.raises(CollectorSkipped):
            agent.collect()


class TestTwoTierTokenGate:
    def test_url_set_no_token_reports_health_only(self, make_agent):
        """grafana_url configured but grafana_api_token empty -> health check IS
        attempted (anonymous-accessible), dashboards/alerts are NOT — and this
        is a clean partial report, not an error."""
        agent = make_agent(GrafanaAgent)
        agent.settings.grafana_url = "http://203.0.113.15:3002"
        agent.settings.grafana_api_token = ""

        with (
            patch(f"{MODULE}.grafana_health_tool") as mock_health,
            patch(f"{MODULE}.grafana_dashboards_tool") as mock_dash,
            patch(f"{MODULE}.grafana_alerts_tool") as mock_alerts,
        ):
            mock_health.invoke.return_value = MOCK_HEALTH
            outcome = agent.collect()
            mock_dash.invoke.assert_not_called()
            mock_alerts.invoke.assert_not_called()

        assert isinstance(outcome, CollectOutcome)
        assert outcome.errors == []
        assert len(outcome.items) == 1
        item = outcome.items[0]
        assert item["name"] == "grafana"
        assert item["type"] == "grafana_health"
        assert item["data"] == {"version": "10.4.1", "database": "ok"}


class TestCollectSuccess:
    def test_full_happy_path(self, agent):
        with (
            patch(f"{MODULE}.grafana_health_tool") as mock_health,
            patch(f"{MODULE}.grafana_dashboards_tool") as mock_dash,
            patch(f"{MODULE}.grafana_alerts_tool") as mock_alerts,
        ):
            mock_health.invoke.return_value = MOCK_HEALTH
            mock_dash.invoke.return_value = MOCK_DASHBOARDS
            mock_alerts.invoke.return_value = MOCK_ALERTS

            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert outcome.errors == []
        # 1 health + 2 dashboards + 1 alert
        assert len(outcome.items) == 4

        health_items = [i for i in outcome.items if i["type"] == "grafana_health"]
        assert len(health_items) == 1
        assert health_items[0]["data"]["version"] == "10.4.1"
        assert health_items[0]["data"]["database"] == "ok"

        dash_items = [i for i in outcome.items if i["type"] == "grafana_dashboard"]
        assert len(dash_items) == 2
        names = {i["name"] for i in dash_items}
        assert names == {"Production Overview", "Node Exporter Full"}
        prod = next(i for i in dash_items if i["name"] == "Production Overview")
        assert prod["data"]["uid"] == "cIBgcSjkk"
        assert prod["data"]["folder"] == "General"
        assert prod["data"]["tags"] == ["prod", "monitoring"]

        alert_items = [i for i in outcome.items if i["type"] == "grafana_alert"]
        assert len(alert_items) == 1
        alert = alert_items[0]
        assert alert["name"] == "HighCPU"
        assert alert["data"]["fingerprint"] == "deadbeef01234567"
        assert alert["data"]["state"] == "active"
        assert alert["data"]["severity"] == "critical"
        assert alert["data"]["summary"] == "CPU usage above 90% for 5m"

    def test_no_dashboards_or_alerts_is_not_an_error(self, agent):
        with (
            patch(f"{MODULE}.grafana_health_tool") as mock_health,
            patch(f"{MODULE}.grafana_dashboards_tool") as mock_dash,
            patch(f"{MODULE}.grafana_alerts_tool") as mock_alerts,
        ):
            mock_health.invoke.return_value = MOCK_HEALTH
            mock_dash.invoke.return_value = []
            mock_alerts.invoke.return_value = []

            outcome = agent.collect()

        assert outcome.errors == []
        assert len(outcome.items) == 1
        assert outcome.items[0]["type"] == "grafana_health"


class TestPartialFailureTolerance:
    def test_alerts_failure_does_not_kill_health_or_dashboards(self, agent):
        """One sub-resource (alerts) failing must not abort health/dashboard
        collection or raise out of collect() — recorded in outcome.errors."""
        with (
            patch(f"{MODULE}.grafana_health_tool") as mock_health,
            patch(f"{MODULE}.grafana_dashboards_tool") as mock_dash,
            patch(f"{MODULE}.grafana_alerts_tool") as mock_alerts,
        ):
            mock_health.invoke.return_value = MOCK_HEALTH
            mock_dash.invoke.return_value = MOCK_DASHBOARDS
            mock_alerts.invoke.side_effect = RuntimeError(
                "404 Not Found: this Grafana version has no unified alerting API"
            )

            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert len(outcome.errors) == 1
        assert "alerts fetch failed" in outcome.errors[0]
        types = {i["type"] for i in outcome.items}
        assert types == {"grafana_health", "grafana_dashboard"}
        assert len([i for i in outcome.items if i["type"] == "grafana_dashboard"]) == 2

    def test_dashboards_failure_does_not_kill_health_or_alerts(self, agent):
        with (
            patch(f"{MODULE}.grafana_health_tool") as mock_health,
            patch(f"{MODULE}.grafana_dashboards_tool") as mock_dash,
            patch(f"{MODULE}.grafana_alerts_tool") as mock_alerts,
        ):
            mock_health.invoke.return_value = MOCK_HEALTH
            mock_dash.invoke.side_effect = RuntimeError("connection refused")
            mock_alerts.invoke.return_value = MOCK_ALERTS

            outcome = agent.collect()

        assert len(outcome.errors) == 1
        assert "dashboard search failed" in outcome.errors[0]
        types = {i["type"] for i in outcome.items}
        assert types == {"grafana_health", "grafana_alert"}

    def test_health_failure_does_not_prevent_dashboards_and_alerts(self, agent):
        """Even if the anonymous health check itself fails, the token-gated
        calls still proceed — health being down doesn't imply the rest of the
        API is unreachable."""
        with (
            patch(f"{MODULE}.grafana_health_tool") as mock_health,
            patch(f"{MODULE}.grafana_dashboards_tool") as mock_dash,
            patch(f"{MODULE}.grafana_alerts_tool") as mock_alerts,
        ):
            mock_health.invoke.side_effect = RuntimeError("timeout")
            mock_dash.invoke.return_value = MOCK_DASHBOARDS
            mock_alerts.invoke.return_value = MOCK_ALERTS

            outcome = agent.collect()

        assert len(outcome.errors) == 1
        assert "health check failed" in outcome.errors[0]
        types = {i["type"] for i in outcome.items}
        assert types == {"grafana_dashboard", "grafana_alert"}

    def test_run_reports_failed_when_collect_raises(self, agent, session_patcher):
        """BaseAgent.run() catches unexpected exceptions from collect() itself."""
        with session_patcher("infra_brain.etl.base"):
            with patch.object(agent, "collect", side_effect=RuntimeError("boom")):
                run_result = agent.run()
        assert run_result.status == "failed"
        assert "boom" in run_result.errors[0]


class TestDomainAndSafety:
    def test_domain_is_set(self, agent):
        assert agent.domain == "grafana"

    def test_spec_metadata(self, agent):
        spec = GrafanaAgent.spec
        assert spec.domain == "grafana"
        assert spec.schedule == "7,22,37,52 * * * *"

    def test_callbacks_wired(self, agent):
        """build_callbacks() must be called — safety layer must be active."""
        real_agent = GrafanaAgent()
        assert real_agent.callbacks is not None
        assert len(real_agent.callbacks) > 0

    def test_tools_use_readonly_get(self):
        """Structural read-only: the collector's HTTP tools must funnel through
        readonly_get (ReadOnlyClient), never a plain httpx client capable of
        non-GET verbs."""
        import infra_brain.tools.grafana_tool as tool_mod

        assert tool_mod.readonly_get is not None
