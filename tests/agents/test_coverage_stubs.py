"""Real tests for agents that were previously covered by except:pass stubs."""

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# FleetHealthReporter
# ---------------------------------------------------------------------------


def test_fleet_health_collect_returns_one_snapshot():
    """F-008: collect() reports its single health_snapshot Resource via
    count_override (no generic items=[...] — see TRK-268 / GitLab #155)."""
    from infra_brain.agents.fleet_health import FleetHealthReporter

    mock_session = MagicMock()
    # DriftEvent.filter_by(status="open").count() -> 3
    # VulnQueueItem.filter_by(status="open").count() -> 7
    # EolRegistry.count() -> 5
    mock_session.query.return_value.filter_by.return_value.count.return_value = 3
    mock_session.query.return_value.count.return_value = 5

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_session)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("infra_brain.agents.fleet_health.get_session", return_value=mock_ctx),
        patch("infra_brain.api._seeding.upsert_resource") as mock_upsert,
    ):
        reporter = FleetHealthReporter()
        outcome = reporter.collect()

    assert outcome.items == []
    assert outcome.count_override == 1

    _, kwargs = mock_upsert.call_args
    assert kwargs["resource_type"] == "health_snapshot"
    assert kwargs["name"] == "fleet-health"
    assert "open_drift_events" in kwargs["metadata"]
    assert "open_vulnerabilities" in kwargs["metadata"]
    assert "eol_assets" in kwargs["metadata"]


def test_fleet_health_collect_reflects_counts():
    """collect() data values match the mocked DB counts."""
    from infra_brain.agents.fleet_health import FleetHealthReporter

    mock_session = MagicMock()
    # _summary_counts uses .filter(...).count() (not filter_by) for drift + vuln
    mock_session.query.return_value.filter.return_value.count.return_value = 4
    # plain .count() used for total_resources and EolRegistry
    mock_session.query.return_value.count.return_value = 11

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_session)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("infra_brain.agents.fleet_health.get_session", return_value=mock_ctx),
        patch("infra_brain.api._seeding.upsert_resource") as mock_upsert,
    ):
        FleetHealthReporter().collect()

    data = mock_upsert.call_args.kwargs["metadata"]
    assert data["open_drift_events"] == 4
    assert data["open_vulnerabilities"] == 4
    assert data["eol_assets"] == 11


# ---------------------------------------------------------------------------
# EOLAgent
# ---------------------------------------------------------------------------


def test_eol_agent_collect_empty_when_no_products():
    """collect() returns [] when eol_registry has no rows."""
    from infra_brain.agents.eol import EOLAgent

    mock_session = MagicMock()
    mock_session.query.return_value.all.return_value = []

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_session)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    with patch("infra_brain.agents.eol.get_session", return_value=mock_ctx):
        agent = EOLAgent()
        items = agent.collect()

    assert items == []


def test_eol_agent_collect_returns_eol_cycles(tmp_path):
    """collect() emits an eol_cycle item per distinct OS derived from inventory.

    MR3 inverted the flow: collect() now derives products from collected OS
    inventory (via _derive_os_products) rather than re-reading the registry, then
    looks up the EOL date per product. We mock the derivation + the EOL tool.
    """
    from infra_brain.agents.eol import EOLAgent

    api_cycles = [
        {"cycle": "22.04", "eol": "2027-04-01", "latest": "22.04.3", "lts": True},
    ]

    agent = EOLAgent.__new__(EOLAgent)
    agent.callbacks = []
    derived = {
        "Ubuntu 22.04": {
            "product": "ubuntu",
            "cycle": "22.04",
            "label": "Ubuntu 22.04",
            "count": 1,
            "resource_id": None,
        }
    }

    with (
        patch.object(EOLAgent, "_derive_os_products", return_value=derived),
        patch("infra_brain.agents.eol.eol_cycles_tool") as mock_tool,
    ):
        mock_tool.invoke.return_value = api_cycles
        items = agent.collect()

    assert len(items) == 1
    item = items[0]
    assert item["type"] == "eol_cycle"
    assert item["data"]["product"] == "ubuntu"
    assert item["data"]["cycle"] == "22.04"
    assert item["data"]["asset_name"] == "Ubuntu 22.04"
    assert item["data"]["eol"] == "2027-04-01"


# ---------------------------------------------------------------------------
# WindowsAgent
# ---------------------------------------------------------------------------


def test_windows_agent_collect_returns_host_items():
    """collect() maps ansible_facts to windows_host items."""
    raw = {
        "win-host-01": {
            "ansible_facts": {
                "ansible_os_name": "Windows Server 2019",
                "ansible_os_version": "10.0.17763",
                "ansible_architecture": "64-bit",
                "ansible_hostname": "win-host-01",
                "ansible_domain": "corp.local",
                "ansible_services": {},
                "ansible_pending_updates": [],
            }
        }
    }

    mock_settings = MagicMock()
    mock_settings.ansible_inventory_path = "/etc/ansible/hosts"
    # F-021: non-empty password so collect() passes the WinRM credential guard.
    mock_settings.ansible_win_password = "testwinpass"
    mock_settings.ansible_win_user = "Administrator"

    with patch("infra_brain.agents.windows.run_windows_ansible_facts") as mock_fn:
        mock_fn.return_value = raw
        from infra_brain.agents.windows import WindowsAgent

        agent = WindowsAgent.__new__(WindowsAgent)
        agent.settings = mock_settings
        agent.callbacks = []
        items = agent.collect()

    assert len(items) == 1
    item = items[0]
    assert item["type"] == "windows_host"
    assert item["name"] == "win-host-01"
    assert item["data"]["os_name"] == "Windows Server 2019"
    assert item["data"]["hostname"] == "win-host-01"
    assert item["data"]["domain"] == "corp.local"
    assert item["data"]["winrm_status"] == "reachable"
    assert item["data"]["services"] == []
    assert item["data"]["pending_updates"] == []


def test_windows_agent_collect_raises_on_error():
    """collect() propagates RuntimeError when run_windows_ansible_facts fails.
    Previously it swallowed the exception and returned [] (the bug this fixes)."""
    mock_settings = MagicMock()
    mock_settings.ansible_inventory_path = "/etc/ansible/hosts"
    # F-021: non-empty password so collect() passes the WinRM credential guard.
    mock_settings.ansible_win_password = "testwinpass"
    mock_settings.ansible_win_user = "Administrator"

    with patch("infra_brain.agents.windows.run_windows_ansible_facts") as mock_fn:
        mock_fn.side_effect = RuntimeError("unreachable")
        from infra_brain.agents.windows import WindowsAgent

        agent = WindowsAgent.__new__(WindowsAgent)
        agent.settings = mock_settings
        agent.callbacks = []
        import pytest as _pytest

        with _pytest.raises(RuntimeError, match="unreachable"):
            agent.collect()


# ---------------------------------------------------------------------------
# CloudAgent — boto3 absent path
# ---------------------------------------------------------------------------


def test_cloud_agent_returns_empty_when_boto3_missing():
    """collect() returns [] when the regions tool returns [] (boto3 absent inside tool)."""
    from infra_brain.agents.cloud import CloudAgent

    with patch("infra_brain.agents.cloud.aws_ec2_regions_tool") as mock_regions:
        mock_regions.invoke.return_value = []
        agent = CloudAgent.__new__(CloudAgent)
        agent.settings = MagicMock()
        agent.settings.aws_enabled = True
        agent.callbacks = []
        outcome = agent.collect()
    assert outcome.items == []


def test_cloud_agent_returns_empty_on_region_error():
    """collect() returns [] (with the failure recorded) when the regions tool
    raises (e.g. no credentials)."""
    from infra_brain.agents.cloud import CloudAgent

    with patch("infra_brain.agents.cloud.aws_ec2_regions_tool") as mock_regions:
        mock_regions.invoke.side_effect = Exception("no credentials")
        agent = CloudAgent.__new__(CloudAgent)
        agent.settings = MagicMock()
        agent.settings.aws_enabled = True
        agent.callbacks = []
        outcome = agent.collect()
    assert outcome.items == []
    assert len(outcome.errors) == 1
