"""Phase 2 Task 1: sweep_id stamping + TIER_ORDER/sweep_members() groundwork.

Covers the prerequisites landed ahead of the LangGraph sweep graph (later
tasks): CollectionRun.sweep_id column, ETLConnector.run() sweep_id
passthrough (default None = zero behavior change), and spec.py's
TIER_ORDER / sweep_members().
"""

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import CollectionRun
from infra_brain.etl.base import (
    RUN_STATUS_INTERRUPT_PENDING,
    RUN_STATUS_RETRY_EXHAUSTED,
    CollectOutcome,
    ETLConnector,
)
from infra_brain.etl.spec import TIER_ORDER, Tier, sweep_members
from infra_brain.supervisor import AGENT_REGISTRY

from tests.support.pg import make_engine


@pytest.fixture
def sqlite_engine():
    eng = make_engine()
    return eng


@contextmanager
def _session_cm(engine):
    with Session(engine) as s:
        yield s


class _StubConnector(ETLConnector):
    domain = "stub_sweep_groundwork"
    schedule = None

    def collect(self, scope: str = "all"):
        return CollectOutcome(items=[])


def _make_connector():
    agent = _StubConnector.__new__(_StubConnector)
    agent.settings = MagicMock(collection_disabled_domains="")
    agent.callbacks = []
    return agent


def test_run_stamps_sweep_id_when_passed(sqlite_engine):
    agent = _make_connector()
    sweep_id = uuid.uuid4()

    with patch("infra_brain.etl.base.get_session", lambda: _session_cm(sqlite_engine)):
        result = agent.run(sweep_id=sweep_id)

    with Session(sqlite_engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run.sweep_id == sweep_id


def test_run_leaves_sweep_id_null_when_not_passed(sqlite_engine):
    """Default None is the existing-caller contract: zero behavior change."""
    agent = _make_connector()

    with patch("infra_brain.etl.base.get_session", lambda: _session_cm(sqlite_engine)):
        result = agent.run()

    with Session(sqlite_engine) as s:
        run = s.get(CollectionRun, result.run_id)
        assert run.sweep_id is None


def test_run_state_constants_are_distinct_strings():
    assert RUN_STATUS_RETRY_EXHAUSTED == "retry_exhausted"
    assert RUN_STATUS_INTERRUPT_PENDING == "interrupt_pending"
    assert RUN_STATUS_RETRY_EXHAUSTED != RUN_STATUS_INTERRUPT_PENDING


def test_tier_order_excludes_reporter_and_on_demand():
    assert TIER_ORDER == (Tier.COLLECTOR, Tier.RECONCILER, Tier.REASONER)
    assert Tier.REPORTER not in TIER_ORDER
    assert Tier.ON_DEMAND not in TIER_ORDER


def test_sweep_members_excludes_reporter_and_on_demand_domains():
    members = sweep_members()
    all_members = {d for domains in members.values() for d in domains}
    for excluded in (
        "fleet_health",
        "learning_feedback",
        "coverage",
        "discovery",
        "query",
        "inventory_mr",
    ):
        assert excluded not in all_members, excluded


def test_sweep_members_includes_the_three_reconciler_domains():
    """Decision (spec deviation, recorded in TRACKER): inventory_reconcile IS
    a sweep member — its shipped tier is RECONCILER."""
    members = sweep_members()
    assert set(members[Tier.RECONCILER]) == {
        "host_reconcile",
        "inventory_reconcile",
        "graph_maintenance",
    }


def test_sweep_members_only_contains_tiers_in_tier_order():
    members = sweep_members()
    assert set(members.keys()) == set(TIER_ORDER)


def test_sweep_members_agrees_with_live_registry():
    """Cross-check against the real AGENT_REGISTRY: every sweep_members()
    domain is dispatchable and its spec.tier matches the bucket it landed in."""
    members = sweep_members()
    for tier, domains in members.items():
        for domain in domains:
            cls = AGENT_REGISTRY[domain]
            assert cls.spec.tier == tier
            assert cls.dispatchable is True
