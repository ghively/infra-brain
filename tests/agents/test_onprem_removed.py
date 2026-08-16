"""F-006/F-001: the onprem alias is gone; every registered domain can emit a
CollectionRun under its own registry key; freshness only monitors real domains.

Fails against the old code: "onprem" was registered/scheduled/monitored while
its agent's domain attr was "vsphere".
"""

import importlib

import pytest


def test_onprem_not_in_registry():
    from infra_brain.supervisor import AGENT_REGISTRY

    assert "onprem" not in AGENT_REGISTRY


def test_onprem_module_deleted():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("infra_brain.agents.onprem")


def test_onprem_not_scheduled_or_monitored():
    from infra_brain.callbacks.freshness import DOMAIN_EXPECTED_MAX_AGE
    from infra_brain.scheduler import _DEFAULT_SCHEDULES

    assert "onprem" not in _DEFAULT_SCHEDULES
    assert "onprem" not in DOMAIN_EXPECTED_MAX_AGE


def test_registry_domains_match_agent_domain_attrs():
    """No registry key may map to an agent whose .domain differs (the F-006 class)."""
    from infra_brain.supervisor import AGENT_REGISTRY

    mismatches = {key: cls.domain for key, cls in AGENT_REGISTRY.items() if cls.domain != key}
    assert mismatches == {}, f"registry key != agent.domain: {mismatches}"
