"""Tests for VulnCveBridge — CVE-detail and solution enrichment."""

from unittest.mock import MagicMock

from infra_brain.agents.vuln_cve import VulnCveBridge


def _make_bridge():
    """Construct a VulnCveBridge with a mocked agent dependency."""
    agent = MagicMock()
    agent.settings = MagicMock()
    agent.callbacks = []
    bridge = VulnCveBridge(agent)
    return bridge


def test_vuln_cve_bridge_instantiates():
    """VulnCveBridge can be constructed from a mock agent."""
    bridge = _make_bridge()
    assert bridge is not None


def test_vuln_cve_bridge_has_write_vuln_details():
    """VulnCveBridge exposes the write_vuln_details public API."""
    bridge = _make_bridge()
    assert callable(getattr(bridge, "write_vuln_details", None))


def test_vuln_cve_bridge_has_write_solutions():
    """VulnCveBridge exposes the write_solutions public API."""
    bridge = _make_bridge()
    assert callable(getattr(bridge, "write_solutions", None))
