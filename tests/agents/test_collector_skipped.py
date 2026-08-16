"""Tests for the CollectorSkipped mechanism.

Verifies that:
  - An unconfigured collector raises CollectorSkipped from collect()
  - BaseAgent.run() records status="skipped" (not "completed" or "failed") with a reason
  - A configured-but-empty collector still records status="completed"

These tests use the in-memory SQLite engine so we can assert the actual
CollectionRun row — not just mock calls.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session, sessionmaker

from infra_brain.agents.base import BaseAgent, CollectorSkipped
from infra_brain.agents.cloud import CloudAgent
from infra_brain.agents.k8s import K8sAgent
from infra_brain.agents.net import NetAgent
from infra_brain.db.models import CollectionRun

from tests.support.pg import make_engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine():
    eng = make_engine()
    return eng


def _session_factory(eng):
    factory = sessionmaker(bind=eng)

    @contextmanager
    def _get():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return _get


# ---------------------------------------------------------------------------
# CollectorSkipped signal — unit-level (no DB)
# ---------------------------------------------------------------------------


class _SkippingAgent(BaseAgent):
    """Minimal agent that always raises CollectorSkipped."""

    domain = "test_skip"

    def collect(self, scope: str = "all") -> list[dict]:
        raise CollectorSkipped("test dependency not configured")


class _EmptyAgent(BaseAgent):
    """Minimal agent that is configured but finds nothing."""

    domain = "test_empty"

    def collect(self, scope: str = "all") -> list[dict]:
        return []


# ---------------------------------------------------------------------------
# run() integration: status="skipped" vs status="completed"
# ---------------------------------------------------------------------------


def test_run_records_skipped_status_when_collect_raises_collector_skipped():
    """BaseAgent.run() catches CollectorSkipped and records status='skipped'."""
    eng = _engine()
    _get_session = _session_factory(eng)

    with (
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = _SkippingAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "skipped", f"expected status='skipped', got {result.status!r}"
    assert result.resources_found == 0
    assert result.errors == [], "CollectorSkipped is not an error — errors list must be empty"

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_skip").one()
        assert run.status == "skipped"
        assert run.error_message == "test dependency not configured"
        assert run.finished_at is not None


def test_run_records_skipped_reason_on_collection_run_row():
    """The reason string from CollectorSkipped lands in CollectionRun.error_message."""
    eng = _engine()
    _get_session = _session_factory(eng)

    class _ReasonAgent(BaseAgent):
        domain = "test_reason"

        def collect(self, scope: str = "all") -> list[dict]:
            raise CollectorSkipped("SNMP_TARGETS env var is empty")

    with (
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = _ReasonAgent()
        agent.run()

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_reason").one()
        assert "SNMP_TARGETS" in run.error_message


def test_run_records_completed_when_configured_but_empty():
    """A configured collector that finds nothing → status='completed', not 'skipped'."""
    eng = _engine()
    _get_session = _session_factory(eng)

    with (
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        agent = _EmptyAgent()
        result = agent.run(trigger_type="manual", scope="all")

    assert result.status == "completed"
    assert result.resources_found == 0

    with Session(eng) as s:
        run = s.query(CollectionRun).filter_by(domain="test_empty").one()
        assert run.status == "completed"
        assert run.error_message is None


# ---------------------------------------------------------------------------
# Per-agent: unconfigured → CollectorSkipped
# ---------------------------------------------------------------------------


def test_net_agent_unconfigured_raises_collector_skipped():
    """NetAgent with no targets → CollectorSkipped, not []."""
    agent = NetAgent.__new__(NetAgent)
    agent.settings = SimpleNamespace(
        snmp_targets="",
        snmp_community="",
        snmp_port=161,
        snmp_timeout_seconds=2,
        snmp_retries=1,
    )
    agent.callbacks = []
    with pytest.raises(CollectorSkipped, match="SNMP"):
        agent.collect("all")


def test_k8s_agent_no_kubeconfig_raises_collector_skipped():
    """K8sAgent with no kubeconfig → CollectorSkipped, not []."""
    from unittest.mock import MagicMock

    agent = K8sAgent.__new__(K8sAgent)
    mock_k8s = MagicMock()
    mock_k8s.config.load_incluster_config.side_effect = Exception("not in cluster")
    mock_k8s.config.load_kube_config.side_effect = Exception("no kubeconfig")
    with patch("infra_brain.agents.k8s.kubernetes", mock_k8s):
        with pytest.raises(CollectorSkipped):
            agent.collect()


def test_cloud_agent_disabled_raises_collector_skipped():
    """CloudAgent with aws_enabled=False → CollectorSkipped, not []."""
    agent = CloudAgent.__new__(CloudAgent)
    agent.settings = MagicMock()
    agent.settings.aws_enabled = False
    agent.callbacks = []
    with pytest.raises(CollectorSkipped):
        agent.collect()


# ---------------------------------------------------------------------------
# Per-agent: configured path still works (returns list, not skipped)
# ---------------------------------------------------------------------------


def test_net_agent_configured_returns_list_not_skipped():
    """NetAgent with targets+community configured returns a list (may be empty if hosts unreachable)."""
    agent = NetAgent.__new__(NetAgent)
    agent.settings = SimpleNamespace(
        snmp_targets="10.0.0.1",
        snmp_community="public",
        snmp_port=161,
        snmp_timeout_seconds=2,
        snmp_retries=1,
    )
    agent.callbacks = []
    with patch("infra_brain.agents.net.net_device_facts_tool") as mock_tool:
        mock_tool.invoke.return_value = None  # unreachable
        result = agent.collect("all")
    assert isinstance(result.items, list)
    # items is [] because the host was unreachable — not CollectorSkipped (it was configured).
    # The unreachable host is recorded in errors (F-007), so status is "partial".


def test_cloud_agent_enabled_but_no_regions_returns_empty_list():
    """CloudAgent with aws_enabled=True but no regions returns [] (configured, empty)."""
    with (
        patch("infra_brain.agents.cloud.aws_ec2_regions_tool") as mock_regions,
        patch("infra_brain.agents.cloud.aws_ec2_instances_tool"),
        patch("infra_brain.agents.cloud.aws_vpcs_tool"),
        patch("infra_brain.agents.cloud.aws_security_groups_tool"),
    ):
        mock_regions.invoke.return_value = []
        agent = CloudAgent.__new__(CloudAgent)
        agent.settings = MagicMock()
        agent.settings.aws_enabled = True
        agent.callbacks = []
        result = agent.collect("all")
    assert result.items == []
