"""Tests for NetAgent SNMP collector and the SNMP parse layer."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from infra_brain.agents.base import CollectorSkipped
from infra_brain.agents.net import NetAgent
from infra_brain.tools.snmp import parse_system_facts


def _agent(**settings):
    """Build a NetAgent without running BaseAgent.__init__ (no LLM/DB needed)."""
    agent = NetAgent.__new__(NetAgent)
    defaults = dict(
        snmp_targets="",
        snmp_community="",
        snmp_port=161,
        snmp_timeout_seconds=2,
        snmp_retries=1,
    )
    defaults.update(settings)
    agent.settings = SimpleNamespace(**defaults)
    agent.callbacks = []
    return agent


def test_collect_raises_skipped_when_unconfigured():
    """No targets/community → CollectorSkipped (not [] — skipped is distinct from working-empty)."""
    with pytest.raises(CollectorSkipped):
        _agent().collect("all")


def test_collect_raises_skipped_when_community_missing():
    agent = _agent(snmp_targets="10.0.0.1", snmp_community="")
    with pytest.raises(CollectorSkipped):
        agent.collect("all")


def test_collect_skips_unreachable_devices():
    """A host that returns None (unreachable/SNMP off) is skipped, not faked."""
    agent = _agent(snmp_targets="10.0.0.1,10.0.0.2", snmp_community="public")
    with patch("infra_brain.agents.net.net_device_facts_tool") as mock_tool:
        mock_tool.invoke.return_value = None
        outcome = agent.collect("all")
    assert outcome.items == []
    assert len(outcome.errors) == 2  # both unreachable hosts recorded (F-007)
    assert mock_tool.invoke.call_count == 2  # both hosts attempted


def test_collect_parses_reachable_device():
    """A reachable device produces one real resource dict."""
    expected = {
        "name": "core-sw-01",
        "type": "net_device",
        "data": {
            "host": "10.0.0.1",
            "sys_name": "core-sw-01",
            "sys_descr": "Cisco IOS Software, C3560",
            "sys_object_id": "1.3.6.1.4.1.9.1.516",
            "sys_uptime": "12 days",
            "sys_contact": "netops@example.com",
            "sys_location": "DC1 Rack 4",
        },
    }
    agent = _agent(snmp_targets="10.0.0.1", snmp_community="public")
    with patch("infra_brain.agents.net.net_device_facts_tool") as mock_tool:
        mock_tool.invoke.return_value = expected
        outcome = agent.collect("all")
    assert len(outcome.items) == 1
    assert outcome.errors == []
    item = outcome.items[0]
    assert item["name"] == "core-sw-01"
    assert item["type"] == "net_device"
    assert item["data"]["host"] == "10.0.0.1"
    assert item["data"]["sys_descr"].startswith("Cisco IOS")


def test_parse_falls_back_to_host_when_no_sysname():
    """parse_system_facts is a pure function; missing sysName → name == host."""
    item = parse_system_facts("198.51.100.17", {"sys_descr": "PaloAlto PAN-OS"})
    assert item["name"] == "198.51.100.17"
    assert item["type"] == "net_device"
    assert item["data"]["sys_name"] == ""
    assert item["data"]["sys_descr"] == "PaloAlto PAN-OS"
