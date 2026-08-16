"""
Tests for VsphereAgent using the new _collect_vcenter / collect API.
These replace the old tool-patch tests that no longer apply after the rewrite.
"""

from unittest.mock import patch, MagicMock
from infra_brain.agents.vsphere import VsphereAgent


def _make_settings(vsphere_host="vc1.example.com", vsphere_hosts=""):
    s = MagicMock()
    s.vsphere_host = vsphere_host
    s.vsphere_hosts = vsphere_hosts
    s.vsphere_user = "admin"
    s.vsphere_password = "secret"
    s.vsphere_ssl_verify = False
    s.vsphere_connect_timeout = 30
    return s


def test_vsphere_agent_collects_vms_and_hosts():
    vms = [{"name": "vm-01", "type": "vsphere_vm", "data": {"power_state": "poweredOn"}}]
    hosts = [{"name": "esxi-01", "type": "vsphere_host", "data": {"connection_state": "connected"}}]

    agent = VsphereAgent.__new__(VsphereAgent)
    agent.callbacks = []
    settings = _make_settings()

    def fake_collect_vcenter(host, s, scope="all", errors=None):
        return vms + hosts

    agent._collect_vcenter = fake_collect_vcenter
    with patch("infra_brain.agents.vsphere.get_settings", return_value=settings):
        outcome = agent.collect()

    items = outcome.items
    assert any(i["type"] == "vsphere_vm" for i in items)
    assert any(i["type"] == "vsphere_host" for i in items)


def test_vsphere_agent_returns_empty_on_failure():
    agent = VsphereAgent.__new__(VsphereAgent)
    agent.callbacks = []
    settings = _make_settings()

    def failing_collect_vcenter(host, s, scope="all", errors=None):
        # Mirror VsphereAgent._collect_vcenter's real connect-failure path,
        # which always appends a "cannot connect to {host}" error — that
        # signal is what suppresses the vsphere_vcenter self-node (TRK-171
        # follow-up) so an unreachable host doesn't get a ghost node.
        if errors is not None:
            errors.append(f"cannot connect to {host}: simulated failure")
        return []

    agent._collect_vcenter = failing_collect_vcenter
    with patch("infra_brain.agents.vsphere.get_settings", return_value=settings):
        outcome = agent.collect()
    assert outcome.items == []


def test_vsphere_agent_domain():
    agent = VsphereAgent.__new__(VsphereAgent)
    agent.callbacks = []
    assert agent.domain == "vsphere"


def test_onprem_module_removed():
    """F-006: onprem was a broken alias whose .domain was "vsphere"; it is gone."""
    import importlib

    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("infra_brain.agents.onprem")
