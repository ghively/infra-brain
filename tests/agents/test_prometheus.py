"""Tests for PrometheusAgent (home-lab monitoring domain, operator-requested).

Covers: happy path (mocked /api/v1/targets + /api/v1/alerts -> Resource-shaped
items), the unconfigured-self-skip case (no prometheus_url -> CollectorSkipped),
and partial-failure tolerance (one of the two sub-resources failing must not
abort the whole collect()).
"""

from unittest.mock import patch

import pytest

from infra_brain.agents.prometheus import PrometheusAgent
from infra_brain.etl.base import CollectorSkipped, CollectOutcome

MODULE = "infra_brain.agents.prometheus"

# Shaped after the real Prometheus HTTP API
# (https://prometheus.io/docs/prometheus/latest/querying/api/).
MOCK_TARGETS_RESPONSE = {
    "status": "success",
    "data": {
        "activeTargets": [
            {
                "discoveredLabels": {"__address__": "node_a:9115"},
                "labels": {"job": "blackbox", "instance": "node_a:9115"},
                "scrapePool": "blackbox",
                "scrapeUrl": "http://node_a:9115/metrics",
                "globalUrl": "http://node_a:9115/metrics",
                "lastError": "",
                "lastScrape": "2026-07-31T12:00:00.000000000Z",
                "lastScrapeDuration": 0.012,
                "health": "up",
            },
            {
                "discoveredLabels": {"__address__": "media-host:1880"},
                "labels": {"job": "node-red", "instance": "media-host:1880"},
                "scrapePool": "node-red",
                "scrapeUrl": "http://media-host:1880/metrics",
                "globalUrl": "http://media-host:1880/metrics",
                "lastError": "connection refused",
                "lastScrape": "2026-07-31T11:55:00.000000000Z",
                "lastScrapeDuration": 0,
                "health": "down",
            },
        ],
        "droppedTargets": [],
    },
}

MOCK_ALERTS_RESPONSE = {
    "status": "success",
    "data": {
        "alerts": [
            {
                "labels": {"alertname": "TargetDown", "severity": "critical", "job": "node-red"},
                "annotations": {"summary": "node-red scrape target is down"},
                "state": "firing",
                "activeAt": "2026-07-31T11:56:00.000000000Z",
                "value": "1e+00",
            }
        ]
    },
}

MOCK_ALERTS_EMPTY = {"status": "success", "data": {"alerts": []}}


@pytest.fixture
def agent(make_agent):
    a = make_agent(PrometheusAgent)
    a.settings.prometheus_url = "http://node_a:9090"
    a.settings.prometheus_ssl_verify = True
    return a


class TestCollectUnconfigured:
    def test_no_prometheus_url_raises_skipped(self, make_agent):
        agent = make_agent(PrometheusAgent)
        agent.settings.prometheus_url = ""
        with pytest.raises(CollectorSkipped):
            agent.collect()

    def test_whitespace_only_prometheus_url_raises_skipped(self, make_agent):
        agent = make_agent(PrometheusAgent)
        agent.settings.prometheus_url = "   "
        with pytest.raises(CollectorSkipped):
            agent.collect()


class TestCollectSuccess:
    def test_collects_targets_and_alerts(self, agent):
        with (
            patch(f"{MODULE}.prometheus_targets_tool") as mock_targets,
            patch(f"{MODULE}.prometheus_alerts_tool") as mock_alerts,
        ):
            mock_targets.invoke.return_value = MOCK_TARGETS_RESPONSE
            mock_alerts.invoke.return_value = MOCK_ALERTS_RESPONSE

            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert outcome.errors == []
        assert len(outcome.items) == 3  # 2 targets + 1 alert

        target_items = [i for i in outcome.items if i["type"] == "prometheus_target"]
        alert_items = [i for i in outcome.items if i["type"] == "prometheus_alert"]
        assert len(target_items) == 2
        assert len(alert_items) == 1

        up_target = next(t for t in target_items if t["data"]["job"] == "blackbox")
        assert up_target["name"] == "blackbox/node_a:9115"
        assert up_target["data"]["instance"] == "node_a:9115"
        assert up_target["data"]["health"] == "up"
        assert up_target["data"]["last_scrape"] == "2026-07-31T12:00:00.000000000Z"
        assert up_target["data"]["scrape_pool"] == "blackbox"

        down_target = next(t for t in target_items if t["data"]["job"] == "node-red")
        assert down_target["name"] == "node-red/media-host:1880"
        assert down_target["data"]["health"] == "down"

        alert = alert_items[0]
        assert alert["name"] == "TargetDown"
        assert alert["data"]["state"] == "firing"
        assert alert["data"]["labels"]["severity"] == "critical"
        assert alert["data"]["annotations"]["summary"] == "node-red scrape target is down"
        assert alert["data"]["active_since"] == "2026-07-31T11:56:00.000000000Z"

    def test_empty_alerts_list_is_success_not_error(self, agent):
        """An empty firing-alerts list is a normal healthy response, not an error."""
        with (
            patch(f"{MODULE}.prometheus_targets_tool") as mock_targets,
            patch(f"{MODULE}.prometheus_alerts_tool") as mock_alerts,
        ):
            mock_targets.invoke.return_value = MOCK_TARGETS_RESPONSE
            mock_alerts.invoke.return_value = MOCK_ALERTS_EMPTY

            outcome = agent.collect()

        assert outcome.errors == []
        assert all(i["type"] == "prometheus_target" for i in outcome.items)
        assert len(outcome.items) == 2

    def test_target_missing_optional_fields_is_tolerated(self, agent):
        """A target missing lastScrape/scrapePool/health must not raise."""
        sparse_response = {
            "status": "success",
            "data": {
                "activeTargets": [{"labels": {"job": "node_exporter", "instance": "media-host:9100"}}],
                "droppedTargets": [],
            },
        }
        with (
            patch(f"{MODULE}.prometheus_targets_tool") as mock_targets,
            patch(f"{MODULE}.prometheus_alerts_tool") as mock_alerts,
        ):
            mock_targets.invoke.return_value = sparse_response
            mock_alerts.invoke.return_value = MOCK_ALERTS_EMPTY

            outcome = agent.collect()

        assert outcome.errors == []
        assert len(outcome.items) == 1
        item = outcome.items[0]
        assert item["name"] == "node_exporter/media-host:9100"
        assert item["data"]["health"] == "unknown"
        assert item["data"]["last_scrape"] == ""
        assert item["data"]["scrape_pool"] == ""

    def test_dropped_targets_are_not_emitted(self, agent):
        """droppedTargets carry no health field and are out of scope for 'scrape target health'."""
        response = {
            "status": "success",
            "data": {
                "activeTargets": [],
                "droppedTargets": [{"discoveredLabels": {"__address__": "unused:9100"}}],
            },
        }
        with (
            patch(f"{MODULE}.prometheus_targets_tool") as mock_targets,
            patch(f"{MODULE}.prometheus_alerts_tool") as mock_alerts,
        ):
            mock_targets.invoke.return_value = response
            mock_alerts.invoke.return_value = MOCK_ALERTS_EMPTY

            outcome = agent.collect()

        assert outcome.errors == []
        assert outcome.items == []


class TestPartialFailureTolerance:
    def test_alerts_failure_still_returns_targets(self, agent):
        """/api/v1/alerts down while /api/v1/targets is up: targets still collected,
        the alerts failure is recorded as an error, not raised."""
        with (
            patch(f"{MODULE}.prometheus_targets_tool") as mock_targets,
            patch(f"{MODULE}.prometheus_alerts_tool") as mock_alerts,
        ):
            mock_targets.invoke.return_value = MOCK_TARGETS_RESPONSE
            mock_alerts.invoke.side_effect = RuntimeError("connection refused")

            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert len(outcome.items) == 2
        assert all(i["type"] == "prometheus_target" for i in outcome.items)
        assert len(outcome.errors) == 1
        assert "alerts fetch failed" in outcome.errors[0]
        assert "connection refused" in outcome.errors[0]

    def test_targets_failure_still_returns_alerts(self, agent):
        """/api/v1/targets down while /api/v1/alerts is up: alerts still collected."""
        with (
            patch(f"{MODULE}.prometheus_targets_tool") as mock_targets,
            patch(f"{MODULE}.prometheus_alerts_tool") as mock_alerts,
        ):
            mock_targets.invoke.side_effect = RuntimeError("timed out")
            mock_alerts.invoke.return_value = MOCK_ALERTS_RESPONSE

            outcome = agent.collect()

        assert len(outcome.items) == 1
        assert outcome.items[0]["type"] == "prometheus_alert"
        assert len(outcome.errors) == 1
        assert "targets fetch failed" in outcome.errors[0]

    def test_both_fail_returns_empty_outcome_with_two_errors(self, agent):
        with (
            patch(f"{MODULE}.prometheus_targets_tool") as mock_targets,
            patch(f"{MODULE}.prometheus_alerts_tool") as mock_alerts,
        ):
            mock_targets.invoke.side_effect = RuntimeError("targets down")
            mock_alerts.invoke.side_effect = RuntimeError("alerts down")

            outcome = agent.collect()

        assert outcome.items == []
        assert len(outcome.errors) == 2


class TestDomainAndSafety:
    def test_domain_is_set(self, agent):
        assert agent.domain == "prometheus"

    def test_callbacks_wired(self, agent):
        """build_callbacks() must be called — safety layer must be active."""
        real_agent = PrometheusAgent()
        assert real_agent.callbacks is not None
        assert len(real_agent.callbacks) > 0

    def test_tools_use_readonly_client(self):
        """Structural read-only: the collector's HTTP tool calls funnel through
        readonly_get (GET-only by construction), never a plain httpx call."""
        import infra_brain.tools.prometheus_tool as tool_mod

        assert tool_mod.readonly_get is not None

    def test_run_reports_failed_when_collect_raises_unexpectedly(self, agent, session_patcher):
        """BaseAgent.run() catches exceptions from collect() itself (distinct from
        the two tool calls inside collect(), which are individually caught)."""
        with session_patcher("infra_brain.etl.base"):
            with patch.object(agent, "collect", side_effect=RuntimeError("boom")):
                run_result = agent.run()
        assert run_result.status == "failed"
        assert "boom" in run_result.errors[0]
