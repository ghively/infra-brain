"""Tests for CoverageAgent — detects uncovered infrastructure domains."""

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.coverage import CoverageAgent
from infra_brain.db.models import IntegrationProposal, ScanPoint

from tests.support.pg import make_engine


def _make_agent():
    agent = CoverageAgent.__new__(CoverageAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    agent._llm = None
    return agent


def test_coverage_agent_domain():
    agent = _make_agent()
    assert agent.domain == "coverage"


def test_coverage_agent_llm_role():
    """CoverageAgent must declare llm_role='coverage' so get_chat_model selects
    the correct Bedrock model tier for strategy-reasoning work."""
    assert CoverageAgent.llm_role == "coverage"


def test_default_schedules_no_onprem():
    """'onprem' domain was retired — its schedule entry must be absent.

    TRK-047: coverage.DEFAULT_SCHEDULES was deleted; the expected-cadence
    lookup is now derived from the AgentSpec registry."""
    from infra_brain.etl.spec import schedule_by_domain

    assert "onprem" not in schedule_by_domain()


def test_collect_delegates_to_discover():
    """collect() wraps discover() output as resource dicts."""
    agent = _make_agent()
    with patch.object(CoverageAgent, "discover", return_value=[]):
        result = agent.collect(scope="all")
    assert result == []


def test_collect_returns_resource_dicts_from_gaps():
    """collect() converts each gap dict to a resource dict with name, type, and data."""
    agent = _make_agent()
    gap = {"domain": "k8s", "proposal_id": "abc123", "strategy": "deploy k8s agent"}
    with patch.object(CoverageAgent, "discover", return_value=[gap]):
        result = agent.collect(scope="all")

    assert len(result) == 1
    assert result[0]["name"] == "k8s"
    assert result[0]["type"] == "coverage-gap"
    assert result[0]["data"] == gap


# --- R-15/TRK-068: no DB session may be held open across an LLM call ---


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


def _tracking_session_cm(engine, state):
    @contextmanager
    def _cm():
        state["open"] = True
        try:
            with Session(engine) as s:
                yield s
        finally:
            state["open"] = False

    return _cm


def test_discover_calls_reason_with_no_session_open(engine):
    """R-15/TRK-068: discover() must close its DB session before calling
    self.reason() — the LLM round-trip must never run inside an open
    `with get_session()` block."""
    state = {"open": False}
    session_cm = _tracking_session_cm(engine, state)

    agent = _make_agent()

    # T6 (rev14): discover() now also passes deadline_seconds, so a single slow
    # call cannot overrun the wall-clock budget the loop already advertised.
    # **kwargs keeps this fake agnostic to that (and to wire(), which does not).
    def _fake_reason(prompt, tools, **kwargs):
        assert state["open"] is False, "reason() called while a DB session was open"
        return '{"resource_types": ["thing"]}'

    agent.reason = MagicMock(side_effect=_fake_reason)

    with (
        patch("infra_brain.agents.coverage.get_session", session_cm),
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"cloud": object(), "k8s": object()}),
    ):
        results = agent.discover()

    assert agent.reason.call_count == 2
    assert {r["domain"] for r in results} == {"cloud", "k8s"}

    with Session(engine) as v:
        assert v.query(IntegrationProposal).count() == 2


def test_wire_calls_reason_with_no_session_open(engine):
    """R-15/TRK-068: wire() must also close its DB session before calling
    self.reason()."""
    state = {"open": False}
    session_cm = _tracking_session_cm(engine, state)

    proposal_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            IntegrationProposal(
                id=proposal_id,
                source="coverage-gap:cloud",
                type="domain-agent",
                endpoint="all",
                confidence=0.85,
                proposed_at=datetime.now(timezone.utc),
                status="approved",
            )
        )
        s.commit()

    agent = _make_agent()

    # T6 (rev14): discover() now also passes deadline_seconds, so a single slow
    # call cannot overrun the wall-clock budget the loop already advertised.
    # **kwargs keeps this fake agnostic to that (and to wire(), which does not).
    def _fake_reason(prompt, tools, **kwargs):
        assert state["open"] is False, "reason() called while a DB session was open"
        return '{"resource_types": ["thing"]}'

    agent.reason = MagicMock(side_effect=_fake_reason)

    with patch("infra_brain.agents.coverage.get_session", session_cm):
        ok = agent.wire(str(proposal_id))

    assert ok is True
    assert agent.reason.call_count == 1

    with Session(engine) as v:
        assert v.query(ScanPoint).filter_by(domain="cloud").count() == 1
        assert v.get(IntegrationProposal, proposal_id).status == "wired"


def test_discover_llm_cap_defers_remaining_gaps(engine):
    """TRK-109: at most coverage_gap_llm_cap gaps get an LLM strategy per run;
    the remainder is deferred and drained by subsequent runs."""
    state = {"open": False}
    session_cm = _tracking_session_cm(engine, state)
    registry = {f"dom{i}": object() for i in range(4)}

    def _run_once():
        agent = _make_agent()
        agent.reason = MagicMock(return_value='{"resource_types": ["thing"]}')
        with (
            patch("infra_brain.agents.coverage.get_session", session_cm),
            patch("infra_brain.supervisor.AGENT_REGISTRY", registry),
            patch("infra_brain.agents.coverage.get_settings") as gs,
        ):
            gs.return_value.coverage_gap_llm_cap = 2
            gs.return_value.collect_timeout_seconds = 300
            results = agent.discover()
        return agent, results

    agent1, results1 = _run_once()
    assert agent1.reason.call_count == 2
    assert len(results1) == 2

    # Second run: the 2 drafted gaps now have pending proposals and are
    # skipped; the deferred 2 get drafted.
    agent2, results2 = _run_once()
    assert agent2.reason.call_count == 2
    assert len(results2) == 2

    with Session(engine) as v:
        assert v.query(IntegrationProposal).count() == 4


def test_discover_time_budget_defers_remaining_gaps(engine):
    """TRK-109: the wall-clock budget stops LLM drafting even under the count
    cap — a slow local model must not eat the whole collect() guard."""
    state = {"open": False}
    session_cm = _tracking_session_cm(engine, state)
    registry = {"alpha": object(), "beta": object()}

    agent = _make_agent()
    agent.reason = MagicMock(return_value='{"resource_types": ["thing"]}')

    fake_clock = MagicMock()
    # start, gap1 check (inside budget), gap1 deadline calc, gap2 check (budget
    # blown), warning log. The third tick is T6 (rev14): discover() now computes
    # a per-call deadline from the SAME clock so a slow call self-terminates
    # inside the budget instead of overrunning it.
    fake_clock.monotonic.side_effect = [0.0, 10.0, 11.0, 400.0, 401.0]

    with (
        patch("infra_brain.agents.coverage.get_session", session_cm),
        patch("infra_brain.supervisor.AGENT_REGISTRY", registry),
        patch("infra_brain.agents.coverage.get_settings") as gs,
        patch("infra_brain.agents.coverage.time", fake_clock),
    ):
        gs.return_value.coverage_gap_llm_cap = 5
        gs.return_value.collect_timeout_seconds = 300  # budget = 180s
        results = agent.discover()

    assert agent.reason.call_count == 1
    assert len(results) == 1


def test_discover_defers_domain_when_reason_raises(engine):
    """TRK-106 follow-up: a single erroring/hung LLM call (e.g. the per-call
    request timeout firing, or a provider connection error) must not abort the
    whole run — the domain is deferred and the loop keeps draining the backlog.
    """
    state = {"open": False}
    session_cm = _tracking_session_cm(engine, state)
    # sorted(gaps) is deterministic: "alpha" first (raises), "beta" second (ok).
    registry = {"alpha": object(), "beta": object()}

    agent = _make_agent()
    agent.reason = MagicMock(
        side_effect=[
            TimeoutError("simulated per-call LLM timeout"),
            '{"resource_types": ["thing"]}',
        ]
    )

    with (
        patch("infra_brain.agents.coverage.get_session", session_cm),
        patch("infra_brain.supervisor.AGENT_REGISTRY", registry),
        patch("infra_brain.agents.coverage.get_settings") as gs,
    ):
        gs.return_value.coverage_gap_llm_cap = 5
        gs.return_value.collect_timeout_seconds = 300
        # Must NOT raise even though the first reason() call raised.
        results = agent.discover()

    # Both gaps were attempted; the erroring one is deferred (no proposal), the
    # healthy one is drafted and persisted.
    assert agent.reason.call_count == 2
    assert [r["domain"] for r in results] == ["beta"]
    with Session(engine) as v:
        proposals = v.query(IntegrationProposal).all()
        assert len(proposals) == 1
        assert proposals[0].source == "coverage-gap:beta"
