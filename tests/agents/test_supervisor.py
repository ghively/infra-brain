from unittest.mock import MagicMock, patch

import pytest

from infra_brain.supervisor import dispatch


def test_dispatch_routes_to_linux_agent():
    mock_result = MagicMock()
    mock_result.domain = "linux"
    mock_result.status = "completed"

    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = mock_result
    MockLinuxAgent = MagicMock(return_value=mock_agent_instance)

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"linux": MockLinuxAgent}),
        patch("infra_brain.supervisor._post_collection_hook") as hook,
    ):
        result = dispatch(domain="linux", trigger_type="scheduled", scope="all")

    assert result.domain == "linux"
    assert result.status == "completed"
    mock_agent_instance.run.assert_called_once_with(trigger_type="scheduled", scope="all")
    hook.assert_called_once_with(mock_result)


def test_dispatch_skips_hook_for_skip_hook_domains():
    mock_agent_instance = MagicMock()
    MockDriftAgent = MagicMock(return_value=mock_agent_instance)

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"drift": MockDriftAgent}),
        patch("infra_brain.supervisor._post_collection_hook") as hook,
    ):
        dispatch(domain="drift")

    hook.assert_not_called()


def test_dispatch_unknown_domain_raises():
    with patch("infra_brain.supervisor.AGENT_REGISTRY", {}):
        with pytest.raises(ValueError, match="Unknown domain"):
            dispatch(domain="unknown_domain", trigger_type="manual", scope="all")


def test_integration_not_in_agent_registry():
    """CoverageAgent and QueryAgent are on-demand — 'integration' key must not be in AGENT_REGISTRY."""
    from infra_brain.supervisor import AGENT_REGISTRY

    assert "integration" not in AGENT_REGISTRY


def test_dispatchable_false_domains_not_in_agent_registry():
    """``dispatchable=False`` still removes a domain from AGENT_REGISTRY entirely.

    Only ``net`` uses this lever now (POC: netdiscovery covers network-device
    inventory, so net's shallow SNMPv2 system group adds nothing). cloud, k8s
    and windows moved OFF it on 2026-08-12 — see the retired tests below.
    """
    from infra_brain.supervisor import AGENT_REGISTRY

    assert "net" not in AGENT_REGISTRY
    assert "integration" not in AGENT_REGISTRY


def test_retired_domains_stay_registered_but_refuse_dispatch():
    """The retired lever is the opposite trade to ``dispatchable=False``: the
    domain STAYS in AGENT_REGISTRY (so the operator can see it is off on
    purpose, and its tests still run) but cannot be collected.

    Previously cloud/k8s/windows were switched off by commenting them out of
    _AGENT_SPECS, which made them invisible and made re-enabling a two-file
    source edit. The error message must name the way back.
    """
    from infra_brain.supervisor import AGENT_REGISTRY

    for domain in ("cloud", "k8s", "windows", "vsphere", "vuln", "octopus", "identity"):
        assert domain in AGENT_REGISTRY, f"{domain} must stay registered — retired is not deletion"
        assert AGENT_REGISTRY[domain].spec.retired is True
        with patch("infra_brain.supervisor._post_collection_hook") as hook:
            with pytest.raises(ValueError, match="retired"):
                dispatch(domain=domain, trigger_type="manual", scope="all")
        hook.assert_not_called()


def test_dispatch_windows_is_refused_as_retired():
    """TRK-278 / GitLab #140: the Ansible Windows collection is retired.
    Dispatching it must fail loudly rather than run a collection — but now with
    a message that says WHY and how to reverse it, not 'Unknown domain'."""
    with patch("infra_brain.supervisor._post_collection_hook") as hook:
        with pytest.raises(ValueError, match="COLLECTION_REVIVED_DOMAINS=windows"):
            dispatch(domain="windows", trigger_type="manual", scope="all")
    hook.assert_not_called()


def test_supervisor_does_not_import_stategraph():
    """ROADMAP 4.4 acceptance: the graph veneer is gone; dispatch is a typed function."""
    import inspect

    import infra_brain.supervisor as sup

    src = inspect.getsource(sup)
    assert "StateGraph" not in src
    assert "build_supervisor" not in src
