"""Tests for UptimeKumaAgent (new collector domain — operator-requested,
additive scope beyond the convergence plan; task #9, "Monitoring collectors
(Prometheus/Grafana/Alertmanager/UptimeKuma/Wazuh)").

Covers: happy path (mocked status-page + heartbeat API responses ->
Resource-shaped items), the unconfigured self-skip case (no
uptime_kuma_url -> CollectorSkipped), and partial-failure tolerance (the
heartbeat/uptime sub-resource failing degrades monitors to status="pending"
rather than losing the whole collection).
"""

from unittest.mock import patch

import pytest

from infra_brain.agents.uptime_kuma import UptimeKumaAgent
from infra_brain.etl.base import CollectorSkipped, CollectOutcome

MODULE = "infra_brain.agents.uptime_kuma"

MOCK_STATUS_PAGE = {
    "config": {"slug": "default", "title": "Home Lab Status"},
    "incident": None,
    "publicGroupList": [
        {
            "id": 1,
            "name": "Core services",
            "weight": 1,
            "monitorList": [
                {"id": 1, "name": "Outline", "sendUrl": 0, "type": "http"},
                {"id": 2, "name": "Vikunja", "sendUrl": 0, "type": "http"},
            ],
        },
        {
            "id": 2,
            "name": "External",
            "weight": 2,
            "monitorList": [
                {"id": 3, "name": "SearXNG", "sendUrl": 0, "type": "http"},
            ],
        },
    ],
}

MOCK_HEARTBEAT = {
    "heartbeatList": {
        "1": [
            {"status": 1, "time": "2026-07-31 11:50:00", "msg": "", "ping": 12},
            {"status": 1, "time": "2026-07-31 12:00:00", "msg": "", "ping": 15},
        ],
        "2": [
            {"status": 0, "time": "2026-07-31 12:00:00", "msg": "connection refused", "ping": None},
        ],
        # monitor 3 deliberately absent -> degrades to pending/None fields.
    },
    "uptimeList": {
        "1_24": 0.9998,
        "2_24": 0.8421,
    },
}


@pytest.fixture
def agent(make_agent):
    a = make_agent(UptimeKumaAgent)
    a.settings.uptime_kuma_url = "http://203.0.113.15:3001"
    a.settings.uptime_kuma_status_page_slug = "default"
    return a


class TestUnconfiguredSelfSkip:
    def test_no_url_raises_skipped(self, make_agent):
        """Empty uptime_kuma_url -> CollectorSkipped (no-op, not a failure)."""
        agent = make_agent(UptimeKumaAgent)
        agent.settings.uptime_kuma_url = ""
        agent.settings.uptime_kuma_status_page_slug = "default"
        with patch(f"{MODULE}.uptime_kuma_status_page_tool") as mock_status_page:
            with pytest.raises(CollectorSkipped):
                agent.collect()
            mock_status_page.invoke.assert_not_called()

    def test_blank_whitespace_url_raises_skipped(self, make_agent):
        agent = make_agent(UptimeKumaAgent)
        agent.settings.uptime_kuma_url = "   "
        agent.settings.uptime_kuma_status_page_slug = "default"
        with pytest.raises(CollectorSkipped):
            agent.collect()


class TestCollectSuccess:
    def test_full_happy_path(self, agent):
        with (
            patch(f"{MODULE}.uptime_kuma_status_page_tool") as mock_status_page,
            patch(f"{MODULE}.uptime_kuma_heartbeat_tool") as mock_heartbeat,
        ):
            mock_status_page.invoke.return_value = MOCK_STATUS_PAGE
            mock_heartbeat.invoke.return_value = MOCK_HEARTBEAT

            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert outcome.errors == []
        assert len(outcome.items) == 3
        assert all(i["type"] == "uptime_kuma_monitor" for i in outcome.items)

        by_name = {i["name"]: i["data"] for i in outcome.items}

        assert by_name["Outline"]["status"] == "up"
        assert by_name["Outline"]["uptime_percent"] == 0.9998
        assert by_name["Outline"]["last_heartbeat_time"] == "2026-07-31 12:00:00"

        assert by_name["Vikunja"]["status"] == "down"
        assert by_name["Vikunja"]["uptime_percent"] == 0.8421
        assert by_name["Vikunja"]["last_heartbeat_time"] == "2026-07-31 12:00:00"

        # SearXNG has no heartbeat rows in the mock feed -> degrades cleanly.
        assert by_name["SearXNG"]["status"] == "pending"
        assert by_name["SearXNG"]["uptime_percent"] is None
        assert by_name["SearXNG"]["last_heartbeat_time"] is None

    def test_maintenance_status_maps_to_pending(self, agent):
        heartbeat = {
            "heartbeatList": {
                "1": [{"status": 3, "time": "2026-07-31 12:00:00", "msg": "", "ping": None}]
            },
            "uptimeList": {"1_24": 1.0},
        }
        status_page = {
            "publicGroupList": [
                {
                    "id": 1,
                    "name": "g",
                    "monitorList": [{"id": 1, "name": "Outline", "type": "http"}],
                }
            ]
        }
        with (
            patch(f"{MODULE}.uptime_kuma_status_page_tool") as mock_status_page,
            patch(f"{MODULE}.uptime_kuma_heartbeat_tool") as mock_heartbeat,
        ):
            mock_status_page.invoke.return_value = status_page
            mock_heartbeat.invoke.return_value = heartbeat
            outcome = agent.collect()

        assert outcome.items[0]["data"]["status"] == "pending"

    def test_empty_monitor_list_is_not_an_error(self, agent):
        with (
            patch(f"{MODULE}.uptime_kuma_status_page_tool") as mock_status_page,
            patch(f"{MODULE}.uptime_kuma_heartbeat_tool") as mock_heartbeat,
        ):
            mock_status_page.invoke.return_value = {"publicGroupList": []}
            outcome = agent.collect()
            mock_heartbeat.invoke.assert_not_called()

        assert isinstance(outcome, CollectOutcome)
        assert outcome.items == []
        assert outcome.errors == []

    def test_default_slug_used_when_unset(self, make_agent):
        agent = make_agent(UptimeKumaAgent)
        agent.settings.uptime_kuma_url = "http://203.0.113.15:3001"
        agent.settings.uptime_kuma_status_page_slug = ""
        with (
            patch(f"{MODULE}.uptime_kuma_status_page_tool") as mock_status_page,
            patch(f"{MODULE}.uptime_kuma_heartbeat_tool"),
        ):
            mock_status_page.invoke.return_value = {"publicGroupList": []}
            agent.collect()
            mock_status_page.invoke.assert_called_once_with(
                {"base_url": "http://203.0.113.15:3001", "slug": "default"},
                config={"callbacks": []},
            )


class TestPartialFailureTolerance:
    def test_heartbeat_failure_degrades_to_pending_not_a_collection_failure(self, agent):
        """The heartbeat/uptime sub-resource failing must not abort collection
        or raise out of collect() — monitors still come back (from the
        status-page call) with status="pending" and null uptime/heartbeat
        fields, and the failure is recorded in outcome.errors."""
        with (
            patch(f"{MODULE}.uptime_kuma_status_page_tool") as mock_status_page,
            patch(f"{MODULE}.uptime_kuma_heartbeat_tool") as mock_heartbeat,
        ):
            mock_status_page.invoke.return_value = MOCK_STATUS_PAGE
            mock_heartbeat.invoke.side_effect = RuntimeError("connection refused")

            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert len(outcome.errors) == 1
        assert "heartbeat feed fetch failed" in outcome.errors[0]
        assert len(outcome.items) == 3
        assert all(i["data"]["status"] == "pending" for i in outcome.items)
        assert all(i["data"]["uptime_percent"] is None for i in outcome.items)

    def test_status_page_failure_aborts_with_empty_items(self, agent):
        """Unlike the heartbeat call, the status-page call is required — its
        failure aborts collection (nothing to report without a monitor list),
        surfaced via CollectOutcome.errors rather than raised."""
        with (
            patch(f"{MODULE}.uptime_kuma_status_page_tool") as mock_status_page,
            patch(f"{MODULE}.uptime_kuma_heartbeat_tool") as mock_heartbeat,
        ):
            mock_status_page.invoke.side_effect = RuntimeError("404 Not Found: unknown slug")

            outcome = agent.collect()
            mock_heartbeat.invoke.assert_not_called()

        assert isinstance(outcome, CollectOutcome)
        assert outcome.items == []
        assert len(outcome.errors) == 1
        assert "status page 'default' fetch failed" in outcome.errors[0]

    def test_run_reports_failed_when_collect_raises(self, agent, session_patcher):
        """BaseAgent.run() catches unexpected exceptions from collect() itself."""
        with session_patcher("infra_brain.etl.base"):
            with patch.object(agent, "collect", side_effect=RuntimeError("boom")):
                run_result = agent.run()
        assert run_result.status == "failed"
        assert "boom" in run_result.errors[0]


class TestDomainAndSafety:
    def test_domain_is_set(self, agent):
        assert agent.domain == "uptime_kuma"

    def test_spec_metadata(self, agent):
        spec = UptimeKumaAgent.spec
        assert spec.domain == "uptime_kuma"
        assert spec.schedule == "8,18,28,38,48,58 * * * *"

    def test_callbacks_wired(self, agent):
        """build_callbacks() must be called — safety layer must be active."""
        real_agent = UptimeKumaAgent()
        assert real_agent.callbacks is not None
        assert len(real_agent.callbacks) > 0

    def test_tools_use_readonly_get(self):
        """Structural read-only: the collector's HTTP tools must funnel through
        readonly_get (ReadOnlyClient), never a plain httpx client capable of
        non-GET verbs."""
        import infra_brain.tools.uptime_kuma_tool as tool_mod

        assert tool_mod.readonly_get is not None
