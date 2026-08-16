"""Tests for AlertmanagerAgent.

Covers: unconfigured self-skip (no alertmanager_url -> CollectorSkipped), the
happy path (mocked v2 API responses -> Resource-shaped items for status +
alerts + silences, including the empty-alerts-is-success case), and
partial-failure tolerance (one sub-call failing must not kill the others).

Mocked response shapes follow Alertmanager's published v2 OpenAPI spec
(https://github.com/prometheus/alertmanager/blob/main/api/v2/openapi.yaml):
AlertmanagerStatus (versionInfo/cluster/uptime), GettableAlert
(labels/annotations/status/startsAt/endsAt/fingerprint), and GettableSilence
(matchers/startsAt/endsAt/createdBy/comment/status).
"""

from unittest.mock import patch

import pytest

from infra_brain.agents.alertmanager import AlertmanagerAgent
from infra_brain.etl.base import CollectorSkipped, CollectOutcome

MODULE = "infra_brain.agents.alertmanager"

MOCK_STATUS = {
    "cluster": {"name": "01HXYZ", "peers": [], "status": "ready"},
    "config": {"original": "global:\n  resolve_timeout: 5m\n"},
    "uptime": "2026-07-01T00:00:00.000Z",
    "versionInfo": {
        "branch": "main",
        "buildDate": "20260601-12:00:00",
        "buildUser": "root@build",
        "goVersion": "go1.22.0",
        "revision": "abc1234",
        "version": "0.27.0",
    },
}

MOCK_ALERTS = [
    {
        "annotations": {"summary": "Host down for 5m"},
        "endsAt": "0001-01-01T00:00:00.000Z",
        "fingerprint": "abc123def456",
        "receivers": [{"name": "ntfy"}],
        "startsAt": "2026-07-31T10:00:00.000Z",
        "status": {"inhibitedBy": [], "silencedBy": [], "state": "active"},
        "updatedAt": "2026-07-31T10:05:00.000Z",
        "generatorURL": "http://prometheus:9090/graph?g0.expr=up%3D%3D0",
        "labels": {"alertname": "TargetDown", "instance": "node_a:9100", "severity": "critical"},
    },
    {
        "annotations": {"summary": "Disk usage above 90%"},
        "endsAt": "0001-01-01T00:00:00.000Z",
        "fingerprint": "ffeeddccbb",
        "receivers": [{"name": "ntfy"}],
        "startsAt": "2026-07-31T09:00:00.000Z",
        "status": {"inhibitedBy": [], "silencedBy": ["silence-1"], "state": "suppressed"},
        "updatedAt": "2026-07-31T09:05:00.000Z",
        "generatorURL": "http://prometheus:9090/graph?g0.expr=disk",
        "labels": {
            "alertname": "DiskSpaceLow",
            "instance": "deploy-host-01",
            "severity": "warning",
        },
    },
]

MOCK_SILENCES = [
    {
        "id": "silence-1",
        "matchers": [
            {"name": "alertname", "value": "DiskSpaceLow", "isRegex": False, "isEqual": True}
        ],
        "startsAt": "2026-07-31T08:00:00.000Z",
        "endsAt": "2026-08-01T08:00:00.000Z",
        "createdBy": "operator",
        "comment": "known cleanup pending",
        "status": {"state": "active"},
        "updatedAt": "2026-07-31T08:00:00.000Z",
    }
]


@pytest.fixture
def agent(make_agent):
    a = make_agent(AlertmanagerAgent)
    a.settings.alertmanager_url = "http://127.0.0.1:9093"
    return a


class TestUnconfiguredSkip:
    def test_no_url_configured_raises_collector_skipped(self, make_agent):
        agent = make_agent(AlertmanagerAgent)
        agent.settings.alertmanager_url = ""
        with pytest.raises(CollectorSkipped):
            agent.collect()

    def test_whitespace_only_url_raises_collector_skipped(self, make_agent):
        agent = make_agent(AlertmanagerAgent)
        agent.settings.alertmanager_url = "   "
        with pytest.raises(CollectorSkipped):
            agent.collect()


class TestCollectSuccess:
    def test_status_alerts_silences_produce_resource_shaped_items(self, agent):
        with (
            patch(f"{MODULE}.alertmanager_status_tool") as mock_status,
            patch(f"{MODULE}.alertmanager_alerts_tool") as mock_alerts,
            patch(f"{MODULE}.alertmanager_silences_tool") as mock_silences,
        ):
            mock_status.invoke.return_value = MOCK_STATUS
            mock_alerts.invoke.return_value = MOCK_ALERTS
            mock_silences.invoke.return_value = MOCK_SILENCES

            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert outcome.errors == []
        # 1 status + 2 alerts + 1 silence
        assert len(outcome.items) == 4

        by_type = {}
        for item in outcome.items:
            by_type.setdefault(item["type"], []).append(item)

        # --- status ---------------------------------------------------
        assert len(by_type["alertmanager_status"]) == 1
        status_item = by_type["alertmanager_status"][0]
        assert status_item["name"] == "alertmanager"
        assert status_item["data"]["version"] == "0.27.0"
        assert status_item["data"]["cluster_status"] == "ready"
        assert status_item["data"]["uptime"] == "2026-07-01T00:00:00.000Z"

        # --- alerts -----------------------------------------------------
        assert len(by_type["alertmanager_alert"]) == 2
        names = {i["name"] for i in by_type["alertmanager_alert"]}
        assert names == {"TargetDown", "DiskSpaceLow"}
        target_down = next(i for i in by_type["alertmanager_alert"] if i["name"] == "TargetDown")
        assert target_down["data"]["status"]["state"] == "active"
        assert target_down["data"]["status"]["silenced_by"] == []
        assert target_down["data"]["labels"]["instance"] == "node_a:9100"
        assert target_down["data"]["annotations"]["summary"] == "Host down for 5m"
        assert target_down["data"]["startsAt"] == "2026-07-31T10:00:00.000Z"

        disk_low = next(i for i in by_type["alertmanager_alert"] if i["name"] == "DiskSpaceLow")
        assert disk_low["data"]["status"]["state"] == "suppressed"
        assert disk_low["data"]["status"]["silenced_by"] == ["silence-1"]

        # --- silences -----------------------------------------------------
        assert len(by_type["alertmanager_silence"]) == 1
        silence_item = by_type["alertmanager_silence"][0]
        assert silence_item["name"] == "silence-1"
        assert silence_item["data"]["createdBy"] == "operator"
        assert silence_item["data"]["comment"] == "known cleanup pending"
        assert silence_item["data"]["matchers"][0]["value"] == "DiskSpaceLow"

    def test_empty_alerts_and_silences_is_success_not_error(self, agent):
        """An empty active-alerts / active-silences list is a normal quiet state."""
        with (
            patch(f"{MODULE}.alertmanager_status_tool") as mock_status,
            patch(f"{MODULE}.alertmanager_alerts_tool") as mock_alerts,
            patch(f"{MODULE}.alertmanager_silences_tool") as mock_silences,
        ):
            mock_status.invoke.return_value = MOCK_STATUS
            mock_alerts.invoke.return_value = []
            mock_silences.invoke.return_value = []

            outcome = agent.collect()

        assert outcome.errors == []
        assert len(outcome.items) == 1  # status only
        assert outcome.items[0]["type"] == "alertmanager_status"

    def test_alert_missing_alertname_falls_back_to_fingerprint(self, agent):
        weird_alert = {
            "annotations": {},
            "endsAt": "0001-01-01T00:00:00.000Z",
            "fingerprint": "deadbeef",
            "startsAt": "2026-07-31T10:00:00.000Z",
            "status": {"inhibitedBy": [], "silencedBy": [], "state": "active"},
            "labels": {"severity": "critical"},  # no alertname key
        }
        with (
            patch(f"{MODULE}.alertmanager_status_tool") as mock_status,
            patch(f"{MODULE}.alertmanager_alerts_tool") as mock_alerts,
            patch(f"{MODULE}.alertmanager_silences_tool") as mock_silences,
        ):
            mock_status.invoke.return_value = MOCK_STATUS
            mock_alerts.invoke.return_value = [weird_alert]
            mock_silences.invoke.return_value = []

            outcome = agent.collect()

        alert_items = [i for i in outcome.items if i["type"] == "alertmanager_alert"]
        assert len(alert_items) == 1
        assert alert_items[0]["name"] == "alert-deadbeef"


class TestPartialFailureTolerance:
    def test_silences_failure_does_not_kill_status_and_alerts(self, agent):
        """One sub-resource (silences) failing must not abort the whole collect()."""
        with (
            patch(f"{MODULE}.alertmanager_status_tool") as mock_status,
            patch(f"{MODULE}.alertmanager_alerts_tool") as mock_alerts,
            patch(f"{MODULE}.alertmanager_silences_tool") as mock_silences,
        ):
            mock_status.invoke.return_value = MOCK_STATUS
            mock_alerts.invoke.return_value = MOCK_ALERTS
            mock_silences.invoke.side_effect = RuntimeError("connection refused")

            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert len(outcome.errors) == 1
        assert "silences fetch failed" in outcome.errors[0]
        # status (1) + alerts (2) still collected despite silences failing
        assert len(outcome.items) == 3
        types = {i["type"] for i in outcome.items}
        assert types == {"alertmanager_status", "alertmanager_alert"}
        # partial (errors present but data also present)
        assert outcome.status == "partial"

    def test_status_failure_does_not_kill_alerts_and_silences(self, agent):
        with (
            patch(f"{MODULE}.alertmanager_status_tool") as mock_status,
            patch(f"{MODULE}.alertmanager_alerts_tool") as mock_alerts,
            patch(f"{MODULE}.alertmanager_silences_tool") as mock_silences,
        ):
            mock_status.invoke.side_effect = RuntimeError("timeout")
            mock_alerts.invoke.return_value = MOCK_ALERTS
            mock_silences.invoke.return_value = MOCK_SILENCES

            outcome = agent.collect()

        assert len(outcome.errors) == 1
        assert "status fetch failed" in outcome.errors[0]
        # 2 alerts + 1 silence still collected
        assert len(outcome.items) == 3
        assert outcome.status == "partial"

    def test_all_three_failing_yields_failed_status_with_no_items(self, agent):
        with (
            patch(f"{MODULE}.alertmanager_status_tool") as mock_status,
            patch(f"{MODULE}.alertmanager_alerts_tool") as mock_alerts,
            patch(f"{MODULE}.alertmanager_silences_tool") as mock_silences,
        ):
            mock_status.invoke.side_effect = RuntimeError("boom")
            mock_alerts.invoke.side_effect = RuntimeError("boom")
            mock_silences.invoke.side_effect = RuntimeError("boom")

            outcome = agent.collect()

        assert outcome.items == []
        assert len(outcome.errors) == 3
        assert outcome.status == "failed"
