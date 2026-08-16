"""F-004.4: run()-overriding agents keep the collect timeout guard.

Fails against the old code: host_reconcile/drift/netdiscovery called their
phase methods directly, so a hang ran forever and never produced
status="failed".
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from infra_brain.agents.base import BaseAgent


def _slow(*_args, **_kwargs):
    import time

    time.sleep(5)


def _fast_settings():
    s = MagicMock()
    s.collect_timeout_seconds = 1
    return s


def _patch_timeout_settings(monkeypatch):
    # _call_with_timeout reads get_settings() at call time inside base.py
    monkeypatch.setattr("infra_brain.etl.base.get_settings", lambda: _fast_settings())


@contextmanager
def _null_sessions(module_path):
    """Neutralize get_session in the agent module AND base (CollectionRun writes)."""
    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = None

    @contextmanager
    def _fake():
        yield mock_session

    with (
        patch(f"{module_path}.get_session", _fake),
        patch("infra_brain.etl.base.get_session", _fake),
    ):
        yield


def test_call_with_timeout_raises_runtimeerror(monkeypatch, make_agent):
    _patch_timeout_settings(monkeypatch)

    class _A(BaseAgent):
        domain = "guardtest"

        def collect(self, scope="all"):
            return []

    agent = make_agent(_A)
    with pytest.raises(RuntimeError, match="timed out after 1s"):
        agent._call_with_timeout(_slow)


def test_call_with_timeout_prefers_per_domain_spec_override(monkeypatch, make_agent):
    """TRK-117: a per-domain AgentSpec.collect_timeout_seconds override wins over
    the global settings default.

    Global is deliberately large (999s); the per-domain override is 1s. If the
    override is honored, ``_slow`` (5s) trips the 1s guard and the message says
    "timed out after 1s" — proving the 1s override, not the 999s global, drove
    the timeout.
    """
    from infra_brain.etl.spec import AgentSpec, Tier

    big = MagicMock()
    big.collect_timeout_seconds = 999
    monkeypatch.setattr("infra_brain.etl.base.get_settings", lambda: big)

    class _OverrideAgent(BaseAgent):
        spec = AgentSpec(
            domain="timeout_override_test",
            tier=Tier.RECONCILER,
            schedule=None,
            max_staleness=None,
            collect_timeout_seconds=1,
        )

        def collect(self, scope="all"):
            return []

    agent = make_agent(_OverrideAgent)
    with pytest.raises(RuntimeError, match="timed out after 1s"):
        agent._call_with_timeout(_slow)


def test_call_with_timeout_falls_back_to_global_without_override(monkeypatch, make_agent):
    """A spec with no collect_timeout_seconds override uses the global default."""
    from infra_brain.etl.spec import AgentSpec, Tier

    _patch_timeout_settings(monkeypatch)  # global = 1s

    class _NoOverrideAgent(BaseAgent):
        spec = AgentSpec(
            domain="no_override_test",
            tier=Tier.RECONCILER,
            schedule=None,
            max_staleness=None,
        )

        def collect(self, scope="all"):
            return []

    agent = make_agent(_NoOverrideAgent)
    with pytest.raises(RuntimeError, match="timed out after 1s"):
        agent._call_with_timeout(_slow)


def test_host_reconcile_hang_becomes_failed(monkeypatch, make_agent):
    from infra_brain.agents.host_reconcile import HostReconcileAgent

    _patch_timeout_settings(monkeypatch)
    agent = make_agent(HostReconcileAgent)
    monkeypatch.setattr(agent, "_build_merged_hosts", _slow)
    with _null_sessions("infra_brain.agents.host_reconcile"):
        result = agent.run()
    assert result.status == "failed"
    assert any("timed out after 1s" in e for e in result.errors)


def test_drift_hang_becomes_failed(monkeypatch, make_agent):
    from infra_brain.agents.drift import DriftDetector

    _patch_timeout_settings(monkeypatch)
    agent = make_agent(DriftDetector)
    monkeypatch.setattr(agent, "detect_all", _slow)
    with _null_sessions("infra_brain.agents.drift"):
        result = agent.run()
    assert result.status == "failed"
    assert any("timed out after 1s" in e for e in result.errors)


def test_netdiscovery_tier0_hang_becomes_failed(monkeypatch, make_agent):
    from infra_brain.agents.netdiscovery import NetDiscoveryAgent

    _patch_timeout_settings(monkeypatch)
    agent = make_agent(NetDiscoveryAgent)
    monkeypatch.setattr(agent, "_tier0_passive", _slow)
    with _null_sessions("infra_brain.agents.netdiscovery"):
        result = agent.run()
    assert result.status == "failed"
    assert any("timed out after 1s" in e for e in result.errors)
