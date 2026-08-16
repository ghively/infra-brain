"""Tests for agent roster MCP tool — surface hook-driven status (TRK-271)."""

import contextlib
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain import mcp_server
from infra_brain.etl.spec import AgentSpec, Tier

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextlib.contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.mcp_server.get_session", _get_session):
        yield engine


def test_hook_driven_agents_flagged_not_dormant(patched_session):
    """drift/notification/inventory_mr have schedule=None by design (hook-driven,
    invoked by _post_collection_hook, never write a CollectionRun row) — the
    roster must say so explicitly instead of leaving last_run: null to imply
    the agent is unwired (TRK-271)."""

    # Create mock agent classes with appropriate specs
    drift_agent = MagicMock()
    drift_agent.__name__ = "DriftDetector"
    drift_agent.spec = AgentSpec(
        domain="drift",
        tier=Tier.REASONER,
        schedule=None,  # hook-driven
        max_staleness=None,
        skip_hook=True,
    )

    notification_agent = MagicMock()
    notification_agent.__name__ = "NotificationAgent"
    notification_agent.spec = AgentSpec(
        domain="notification",
        tier=Tier.REASONER,
        schedule=None,  # hook-driven
        max_staleness=None,
        skip_hook=True,
    )

    inventory_mr_agent = MagicMock()
    inventory_mr_agent.__name__ = "InventoryMRAgent"
    inventory_mr_agent.spec = AgentSpec(
        domain="inventory_mr",
        tier=Tier.ON_DEMAND,
        schedule=None,  # hook-driven
        max_staleness=None,
        skip_hook=True,
    )

    linux_agent = MagicMock()
    linux_agent.__name__ = "LinuxAgent"
    linux_agent.spec = AgentSpec(
        domain="linux",
        tier=Tier.COLLECTOR,
        schedule="0 */6 * * *",  # normally scheduled
        max_staleness=None,
    )

    fake_registry = {
        "drift": drift_agent,
        "notification": notification_agent,
        "inventory_mr": inventory_mr_agent,
        "linux": linux_agent,
    }

    with patch("infra_brain.supervisor.AGENT_REGISTRY", fake_registry):
        roster = {row["domain"]: row for row in mcp_server.get_agent_roster()}

    assert roster["drift"]["hook_driven"] is True
    assert roster["notification"]["hook_driven"] is True
    assert roster["inventory_mr"]["hook_driven"] is True
    # a normally-scheduled agent must NOT be flagged hook_driven
    assert roster["linux"]["hook_driven"] is False


def _collector(domain: str) -> MagicMock:
    cls = MagicMock()
    cls.__name__ = f"{domain.title()}Agent"
    cls.spec = AgentSpec(
        domain=domain, tier=Tier.COLLECTOR, schedule="0 */6 * * *", max_staleness=None
    )
    return cls


def test_paused_field_reflects_an_active_dispatchable_override(patched_session):
    """A domain paused via dispatchable__<domain>=false must be visibly paused.

    Otherwise it is indistinguishable from a dormant/broken agent — the same
    false signal hook_driven above was added to eliminate.
    """
    fake_registry = {"linux": _collector("linux"), "dns": _collector("dns")}

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", fake_registry),
        patch("infra_brain.runtime_flags.paused_domains", return_value={"linux"}),
    ):
        roster = {row["domain"]: row for row in mcp_server.get_agent_roster()}

    assert roster["linux"]["paused"] is True
    assert roster["dns"]["paused"] is False


def test_paused_field_defaults_false_with_no_overrides(patched_session):
    fake_registry = {"linux": _collector("linux")}

    with (
        patch("infra_brain.supervisor.AGENT_REGISTRY", fake_registry),
        patch("infra_brain.runtime_flags.paused_domains", return_value=set()),
    ):
        roster = {row["domain"]: row for row in mcp_server.get_agent_roster()}

    assert roster["linux"]["paused"] is False
