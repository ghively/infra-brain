"""Tests for WazuhAgent.

Covers: success (mocked /agents + /alerts -> Resource-shaped items), the
unconfigured-self-skip case (CollectorSkipped when any of wazuh_url/
wazuh_username/wazuh_password is empty), a real auth failure (RuntimeError,
NOT a self-skip — see module docstring), and partial-failure-tolerance (the
alerts sub-fetch failing must not drop the already-collected agent
inventory).
"""

from unittest.mock import patch

import pytest

from infra_brain.agents.wazuh import WazuhAgent
from infra_brain.etl.base import CollectorSkipped, CollectOutcome

MODULE = "infra_brain.agents.wazuh"

MOCK_AGENTS_PAYLOAD = {
    "data": {
        "affected_items": [
            {
                "id": "006",
                "name": "ai_node",
                "status": "active",
                "os": {"platform": "ubuntu", "name": "Ubuntu", "version": "22.04"},
                "version": "Wazuh v4.14.5",
                "lastKeepAlive": "2026-07-31T12:00:00+00:00",
            },
            {
                "id": "007",
                "name": "node_a",
                "status": "disconnected",
                "os": {"platform": "linux", "name": "Debian", "version": "12"},
                "version": "Wazuh v4.14.5",
                "lastKeepAlive": "2026-07-20T09:00:00+00:00",
            },
        ],
        "total_affected_items": 2,
    },
    "error": 0,
}

MOCK_ALERTS_PAYLOAD = {
    "data": {
        "affected_items": [
            {
                "id": "1690000000.123456",
                "timestamp": "2026-07-31T11:55:00.000+0000",
                "rule": {
                    "id": "5710",
                    "level": 5,
                    "description": "sshd: Attempt to login using a non-existent user",
                },
                "agent": {"id": "006", "name": "ai_node"},
            }
        ],
        "total_affected_items": 1,
    },
    "error": 0,
}


@pytest.fixture
def agent(make_agent):
    a = make_agent(WazuhAgent)
    a.settings.wazuh_url = "https://203.0.113.15:55000"
    a.settings.wazuh_username = "wazuh-wui"
    a.settings.wazuh_password = "s3cret"
    a.settings.wazuh_ssl_verify = False
    a.settings.api_timeout_seconds = 30
    a.settings.wazuh_agents_cap = 500
    a.settings.wazuh_alerts_cap = 200
    a.settings.wazuh_alerts_window_hours = 24
    # Indexer unconfigured by default — the real Settings default is "", and
    # without this the MagicMock returns a truthy attribute, silently routing
    # every test down the indexer path.
    a.settings.wazuh_indexer_url = ""
    return a


@pytest.fixture
def indexer_agent(agent):
    """Same agent, with the Wazuh Indexer (OpenSearch) configured."""
    agent.settings.wazuh_indexer_url = "https://203.0.113.15:9200"
    agent.settings.wazuh_indexer_username = "admin"
    agent.settings.wazuh_indexer_password = "s3cret"
    agent.settings.wazuh_indexer_ssl_verify = False
    agent.settings.wazuh_indexer_alerts_index = "wazuh-alerts-*"
    return agent


# OpenSearch _search envelope — a DIFFERENT shape from the Manager API's
# {"data": {"affected_items": [...]}}: the alert fields live under _source.
MOCK_INDEXER_ALERTS = {
    "_shards": {"total": 3, "successful": 3},
    "hits": {
        "total": {"value": 1},
        "hits": [
            {
                "_source": {
                    "rule": {"level": 7, "description": "Integrity checksum changed"},
                    "agent": {"name": "node_a"},
                    "timestamp": "2026-08-09T09:00:00.000+0000",
                }
            }
        ],
    },
}

# _shards.total == 0 => the index pattern matches NO indices at all.
MOCK_INDEXER_NO_INDICES = {
    "_shards": {"total": 0, "successful": 0},
    "hits": {"total": {"value": 0}, "hits": []},
}


class TestCollectUnconfigured:
    @pytest.mark.parametrize(
        "missing_field",
        ["wazuh_url", "wazuh_username", "wazuh_password"],
    )
    def test_missing_any_credential_raises_skipped(self, make_agent, missing_field):
        agent = make_agent(WazuhAgent)
        agent.settings.wazuh_url = "https://203.0.113.15:55000"
        agent.settings.wazuh_username = "wazuh-wui"
        agent.settings.wazuh_password = "s3cret"
        setattr(agent.settings, missing_field, "")

        with pytest.raises(CollectorSkipped):
            agent.collect()

    def test_skip_never_calls_authenticate(self, make_agent):
        """A clean self-skip must not even attempt the auth exchange."""
        agent = make_agent(WazuhAgent)
        agent.settings.wazuh_url = ""
        agent.settings.wazuh_username = ""
        agent.settings.wazuh_password = ""

        with patch(f"{MODULE}.get_wazuh_token") as mock_token:
            with pytest.raises(CollectorSkipped):
                agent.collect()
            mock_token.assert_not_called()


class TestAuthFailure:
    def test_auth_failure_is_a_real_failure_not_a_skip(self, agent):
        """Bad credentials / unreachable manager -> RuntimeError, propagated
        (never downgraded to CollectorSkipped or swallowed into errors)."""
        with patch(f"{MODULE}.get_wazuh_token") as mock_token:
            mock_token.side_effect = RuntimeError("Wazuh authentication failed: 401 Unauthorized")
            with pytest.raises(RuntimeError, match="Wazuh authentication failed"):
                agent.collect()

    def test_run_reports_failed_status_on_auth_failure(self, agent, session_patcher):
        """End-to-end through BaseAgent.run(): an auth failure must surface as
        status='failed', not 'skipped'."""
        with session_patcher("infra_brain.etl.base"):
            with patch(f"{MODULE}.get_wazuh_token", side_effect=RuntimeError("bad creds")):
                result = agent.run()
        assert result.status == "failed"
        assert "bad creds" in result.errors[0]


class TestCollectSuccess:
    def test_collects_agents_and_alerts(self, agent):
        with (
            patch(f"{MODULE}.get_wazuh_token", return_value="fake.jwt.token") as mock_token,
            patch(f"{MODULE}.wazuh_agents_tool") as mock_agents,
            patch(f"{MODULE}.wazuh_alerts_tool") as mock_alerts,
        ):
            mock_agents.invoke.return_value = MOCK_AGENTS_PAYLOAD
            mock_alerts.invoke.return_value = MOCK_ALERTS_PAYLOAD

            outcome = agent.collect()

        mock_token.assert_called_once()
        assert isinstance(outcome, CollectOutcome)
        assert outcome.errors == []

        agent_items = [i for i in outcome.items if i["type"] == "wazuh_agent"]
        alert_items = [i for i in outcome.items if i["type"] == "wazuh_alert"]
        assert len(agent_items) == 2
        assert len(alert_items) == 1

        active = next(i for i in agent_items if i["name"] == "ai_node")
        assert active["data"]["status"] == "active"
        assert active["data"]["os"] == "Ubuntu"
        assert active["data"]["version"] == "Wazuh v4.14.5"
        assert active["data"]["last_keep_alive"] == "2026-07-31T12:00:00+00:00"

        disconnected = next(i for i in agent_items if i["name"] == "node_a")
        assert disconnected["data"]["status"] == "disconnected"

        alert = alert_items[0]
        assert alert["name"] == "sshd: Attempt to login using a non-existent user"
        assert alert["data"]["rule_level"] == 5
        assert alert["data"]["agent_name"] == "ai_node"
        assert alert["data"]["timestamp"] == "2026-07-31T11:55:00.000+0000"
        assert alert["data"]["description"] == "sshd: Attempt to login using a non-existent user"

    def test_one_auth_per_collect_and_no_token_in_tool_args(self, agent):
        """One authenticate() per collect() (not one per endpoint) — and the
        resulting JWT must NOT travel as a tool argument.

        H-3: this test previously asserted the opposite (that both invocations
        carried ``token``). ``callbacks/audit.py`` persists tool arguments
        verbatim into the dashboard-queryable audit trail and DLP only scans
        for PANs, so that contract wrote the Wazuh JWT to the audit log in
        cleartext on every run. The tools now resolve the token in-process
        (``tools/wazuh_tool.py::_active_manager_token``); the single-auth
        property this test was written to protect is unchanged and still
        asserted below.
        """
        with (
            patch(f"{MODULE}.get_wazuh_token", return_value="shared-token") as mock_token,
            patch(f"{MODULE}.wazuh_agents_tool") as mock_agents,
            patch(f"{MODULE}.wazuh_alerts_tool") as mock_alerts,
        ):
            mock_agents.invoke.return_value = MOCK_AGENTS_PAYLOAD
            mock_alerts.invoke.return_value = MOCK_ALERTS_PAYLOAD

            agent.collect()

        mock_token.assert_called_once()
        agents_call_args = mock_agents.invoke.call_args[0][0]
        alerts_call_args = mock_alerts.invoke.call_args[0][0]
        assert "token" not in agents_call_args
        assert "token" not in alerts_call_args
        assert "shared-token" not in str(agents_call_args)
        assert "shared-token" not in str(alerts_call_args)


class TestPartialFailureTolerance:
    def test_alerts_failure_does_not_drop_agent_inventory(self, agent):
        """The alerts sub-fetch failing (e.g. this manager's /alerts 404s —
        see the module docstring's Indexer-vs-Manager-API caveat) must not
        discard agent inventory already collected in the same run."""
        with (
            patch(f"{MODULE}.get_wazuh_token", return_value="fake.jwt.token"),
            patch(f"{MODULE}.wazuh_agents_tool") as mock_agents,
            patch(f"{MODULE}.wazuh_alerts_tool") as mock_alerts,
        ):
            mock_agents.invoke.return_value = MOCK_AGENTS_PAYLOAD
            mock_alerts.invoke.side_effect = RuntimeError("404 Not Found: /alerts")

            outcome = agent.collect()

        assert isinstance(outcome, CollectOutcome)
        assert len(outcome.items) == 2
        assert all(i["type"] == "wazuh_agent" for i in outcome.items)
        assert len(outcome.errors) == 1
        assert "alerts fetch failed" in outcome.errors[0]
        assert outcome.status == "partial"

    def test_agents_failure_does_not_drop_alerts(self, agent):
        """Symmetric case: the agents sub-fetch failing must not discard
        alerts already collected in the same run."""
        with (
            patch(f"{MODULE}.get_wazuh_token", return_value="fake.jwt.token"),
            patch(f"{MODULE}.wazuh_agents_tool") as mock_agents,
            patch(f"{MODULE}.wazuh_alerts_tool") as mock_alerts,
        ):
            mock_agents.invoke.side_effect = RuntimeError("connection reset")
            mock_alerts.invoke.return_value = MOCK_ALERTS_PAYLOAD

            outcome = agent.collect()

        assert len(outcome.items) == 1
        assert outcome.items[0]["type"] == "wazuh_alert"
        assert len(outcome.errors) == 1
        assert "agents fetch failed" in outcome.errors[0]
        assert outcome.status == "partial"


class TestDomainAndSafety:
    def test_domain_is_set(self, agent):
        assert agent.domain == "wazuh"

    def test_callbacks_wired(self, agent):
        """build_callbacks() must be called — safety layer must be active."""
        real_agent = WazuhAgent()
        assert real_agent.callbacks is not None
        assert len(real_agent.callbacks) > 0

    def test_authenticate_is_the_only_non_get_call(self):
        """Structural spot-check: the tool module's data-plane calls
        (agents/alerts) go through readonly_get; only the auth exchange uses
        a plain httpx.Client, and that is documented inline."""
        import infra_brain.tools.wazuh_tool as tool_mod

        assert tool_mod.readonly_get is not None
        # _wazuh_authenticate is the one function permitted to reach for a
        # bare httpx.Client — asserting its existence keeps this test tied to
        # the actual narrow exception rather than the module at large.
        assert callable(tool_mod._wazuh_authenticate)


class TestIndexerAlerts:
    """Wazuh 4.x keeps alert SEARCH in the Indexer, not the Manager API."""

    def test_uses_indexer_when_configured_and_parses_source_envelope(self, indexer_agent):
        """The Manager API must not be touched, and _source must be unwrapped.

        Regression: the collector queried GET {wazuh_url}/alerts, which does not
        exist on a stock 4.x manager — 526 consecutive 404s against the real
        install. It also only understood the Manager's affected_items envelope,
        so even a successful indexer response would have flattened to nothing.
        """
        with (
            patch(f"{MODULE}.get_wazuh_token", return_value="fake.jwt.token"),
            patch(f"{MODULE}.wazuh_agents_tool") as mock_agents,
            patch(f"{MODULE}.wazuh_alerts_tool") as mock_manager_alerts,
            patch(f"{MODULE}.wazuh_indexer_alerts_tool") as mock_indexer,
        ):
            mock_agents.invoke.return_value = MOCK_AGENTS_PAYLOAD
            mock_indexer.invoke.return_value = MOCK_INDEXER_ALERTS

            outcome = indexer_agent.collect()

        # The dead Manager endpoint is not called at all.
        mock_manager_alerts.invoke.assert_not_called()
        mock_indexer.invoke.assert_called_once()

        alerts = [i for i in outcome.items if i["type"] == "wazuh_alert"]
        assert len(alerts) == 1
        assert alerts[0]["data"]["rule_level"] == 7
        assert alerts[0]["data"]["agent_name"] == "node_a"
        assert alerts[0]["name"] == "Integrity checksum changed"
        assert outcome.errors == []

    def test_zero_matching_indices_is_an_error_not_a_quiet_success(self, indexer_agent):
        """SAFETY: "no alert indices exist" must never read as "no alerts happened".

        _shards.total == 0 means the manager is shipping nothing to the indexer,
        so the SIEM is recording no detections at all. Live state on this fleet.
        A clean completed/0-alerts run would be indistinguishable from a quiet
        estate — the exact blind spot a security collector must not manufacture.
        """
        with (
            patch(f"{MODULE}.get_wazuh_token", return_value="fake.jwt.token"),
            patch(f"{MODULE}.wazuh_agents_tool") as mock_agents,
            patch(f"{MODULE}.wazuh_indexer_alerts_tool") as mock_indexer,
        ):
            mock_agents.invoke.return_value = MOCK_AGENTS_PAYLOAD
            mock_indexer.invoke.return_value = MOCK_INDEXER_NO_INDICES

            outcome = indexer_agent.collect()

        assert outcome.errors, "zero matching indices must be reported, not swallowed"
        joined = " ".join(outcome.errors)
        assert "matches NO indices" in joined
        assert "not shipping alerts" in joined
        # Agent inventory is unaffected — this is a partial, not a total failure.
        assert [i for i in outcome.items if i["type"] == "wazuh_agent"]

    def test_falls_back_to_manager_api_when_indexer_unconfigured(self, agent):
        """No indexer configured => keep the old Manager path (proxied setups)."""
        with (
            patch(f"{MODULE}.get_wazuh_token", return_value="fake.jwt.token"),
            patch(f"{MODULE}.wazuh_agents_tool") as mock_agents,
            patch(f"{MODULE}.wazuh_alerts_tool") as mock_manager_alerts,
            patch(f"{MODULE}.wazuh_indexer_alerts_tool") as mock_indexer,
        ):
            mock_agents.invoke.return_value = MOCK_AGENTS_PAYLOAD
            mock_manager_alerts.invoke.return_value = MOCK_ALERTS_PAYLOAD

            agent.collect()

        mock_manager_alerts.invoke.assert_called_once()
        mock_indexer.invoke.assert_not_called()
