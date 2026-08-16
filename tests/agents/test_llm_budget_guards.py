"""T6 (rev14): tests pinning the wall-clock budgets added to the two
``reason()`` call sites that had none.

Both were the GitLab #159 failure mode that ``DiscoveryAgent`` was already
fixed for and these two never received: an LLM loop whose only bounds were
per-CALL (``llm_request_timeout_seconds``) multiplied by model turns multiplied
by ``REASON_MAX_ATTEMPTS``, run inside a collector whose external
``_call_with_timeout`` guard then killed the whole run with no diagnostic
signal attributable to anything.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# RootCauseAgent — per-event reasoning had neither max_iters nor a deadline
# ---------------------------------------------------------------------------


def _rootcause_agent(cap=20, collect_timeout=300):
    from infra_brain.agents.rootcause import RootCauseAgent

    agent = RootCauseAgent.__new__(RootCauseAgent)
    agent.settings = MagicMock(rootcause_llm_enabled=True, rootcause_llm_max_events_per_run=cap)
    agent.callbacks = []
    agent._llm = None
    return agent


def test_rootcause_reason_event_now_bounds_iterations_and_wall_clock():
    """Before: ``reason()`` inherited max_iters=8 with NO deadline, so one event
    could burn 8 x 120s x 3 attempts = 2880s, up to 20 times per run, against a
    300s collect budget."""
    from infra_brain.agents.rootcause import _ROOTCAUSE_MAX_ITERS

    agent = _rootcause_agent()
    agent.reason = MagicMock(return_value="analysis text")
    agent._finalize_finding = MagicMock(return_value="finding")

    ctx = {
        "event_id": "e1",
        "resource_name": "web-10",
        "resource_domain": "linux",
        "field": "pkg",
        "old_value": "1",
        "new_value": "2",
        "detected_at": "2026-01-01T00:00:00+00:00",
        "det_explanation": "x",
        "det_correlated": {},
        "det_top": None,
    }
    with patch("infra_brain.agents.rootcause._REASON_TASK_TEMPLATE", "task {event_id}"):
        agent._reason_event(ctx, deadline_seconds=42.0)

    kwargs = agent.reason.call_args.kwargs
    assert kwargs["max_iters"] == _ROOTCAUSE_MAX_ITERS
    assert kwargs["deadline_seconds"] == 42.0


def test_rootcause_llm_phase_deadline_tracks_the_collect_timeout():
    """The budget must be derived from the SAME source the external guard uses,
    so the two cannot drift apart and hand the loop more time than the guard
    will allow."""
    agent = _rootcause_agent()
    with patch(
        "infra_brain.agents.rootcause.get_settings",
        return_value=SimpleNamespace(collect_timeout_seconds=300),
    ):
        deadline = agent._llm_phase_deadline()
    assert deadline is not None
    import time as _time

    budget = deadline - _time.monotonic()
    # 70% of 300s, leaving real room for phases 1 and 3 plus the external guard.
    assert 200 < budget <= 210


def test_rootcause_llm_phase_deadline_is_none_when_collect_timeout_disabled():
    """A disabled collect timeout means unbounded — the prior behaviour, not a
    surprise zero-length budget that would skip every event."""
    agent = _rootcause_agent()
    with patch(
        "infra_brain.agents.rootcause.get_settings",
        return_value=SimpleNamespace(collect_timeout_seconds=0),
    ):
        assert agent._llm_phase_deadline() is None


def _fake_unnoted(n=3):
    """(drift_event, resource) pairs shaped like ``_load_unnoted``'s output."""
    from datetime import datetime, timezone

    pairs = []
    for i in range(n):
        de = SimpleNamespace(
            id=f"e{i}",
            field="pkg",
            old_value="1",
            new_value="2",
            detected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        r = SimpleNamespace(name=f"web-{i}", domain="linux")
        pairs.append((de, r))
    return pairs


def _run_collect_llm(agent, deadline, n=3):
    """Drive ``_collect_llm`` with phase 1 and phase 3 stubbed, so only the
    phase-2 budget loop under test actually executes."""
    det = SimpleNamespace(top=None, explanation="x", correlated={})
    session_cm = MagicMock()
    # session.get(DriftEvent, ...) -> None makes phase 3 skip every event.
    session_cm.return_value.__enter__.return_value.get.return_value = None

    with (
        patch.object(type(agent), "_llm_phase_deadline", return_value=deadline),
        patch.object(type(agent), "_load_unnoted", return_value=(_fake_unnoted(n), [])),
        patch("infra_brain.agents.rootcause.correlate_recent_deploys", return_value=det),
        patch("infra_brain.agents.rootcause.get_session", session_cm),
    ):
        return agent._collect_llm()


def test_rootcause_exhausted_budget_skips_the_model_entirely(caplog):
    """Running out of wall clock must be graceful degradation to the
    deterministic path — the same treatment the token ceiling already gets —
    not an error that flips the run to 'partial'."""
    import logging
    import time as _time

    agent = _rootcause_agent()
    calls = []
    agent._reason_event = MagicMock(
        side_effect=lambda ctx, deadline_seconds=None: calls.append(ctx["event_id"])
    )

    with caplog.at_level(logging.WARNING):
        outcome = _run_collect_llm(agent, deadline=_time.monotonic() - 1.0)

    assert calls == [], "budget was already gone; no event should reach the model"
    assert not getattr(outcome, "errors", None), "degradation is not an error"
    assert "wall-clock budget exhausted" in caplog.text


def test_rootcause_within_budget_still_reasons_over_every_event():
    """The guard must not become a blanket skip: with budget available, every
    capped event still goes through the model, each handed the remainder."""
    import time as _time

    agent = _rootcause_agent()
    seen = []

    def _reason(ctx, deadline_seconds=None):
        seen.append((ctx["event_id"], deadline_seconds))
        return "finding"

    agent._reason_event = MagicMock(side_effect=_reason)
    _run_collect_llm(agent, deadline=_time.monotonic() + 200.0)

    assert [e for e, _ in seen] == ["e0", "e1", "e2"]
    # Each call gets a positive, shrinking-or-equal slice of the same budget.
    deadlines = [d for _, d in seen]
    assert all(d is not None and d > 0 for d in deadlines)
    assert deadlines == sorted(deadlines, reverse=True)


def test_rootcause_unbounded_when_collect_timeout_disabled():
    """``deadline=None`` must mean unbounded (prior behaviour), not zero."""
    agent = _rootcause_agent()
    seen = []
    agent._reason_event = MagicMock(
        side_effect=lambda ctx, deadline_seconds=None: seen.append(deadline_seconds)
    )
    _run_collect_llm(agent, deadline=None)

    assert len(seen) == 3
    assert all(d is None for d in seen)


# ---------------------------------------------------------------------------
# CoverageAgent — the loop advertised a time budget it could not enforce
# ---------------------------------------------------------------------------


def test_coverage_passes_the_remaining_budget_to_each_reason_call():
    """The budget used to be checked only BEFORE each call, while the call
    itself was unbounded: 3 retries x 120s = 360s against a 180s budget."""
    from infra_brain.agents.coverage import CoverageAgent

    agent = CoverageAgent.__new__(CoverageAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    agent._llm = None
    agent.reason = MagicMock(return_value='{"resource_types": ["thing"]}')

    session_cm = MagicMock()
    session_cm.return_value.__enter__.return_value.query.return_value.filter_by.return_value.first.return_value = None
    session_cm.return_value.__enter__.return_value.query.return_value.filter_by.return_value.all.return_value = []

    with (
        patch("infra_brain.agents.coverage.get_session", session_cm),
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"alpha": object()}),
        patch("infra_brain.agents.coverage.get_settings") as gs,
    ):
        gs.return_value.coverage_gap_llm_cap = 5
        gs.return_value.collect_timeout_seconds = 300  # budget = 180s
        gs.return_value.default_zone = "corporate"
        agent.discover()

    assert agent.reason.call_count == 1
    deadline = agent.reason.call_args.kwargs["deadline_seconds"]
    # Whatever remains of the 180s budget — never unbounded, never negative.
    assert deadline is not None
    assert 0 < deadline <= 180


def test_coverage_deadline_never_goes_negative():
    """``max(0.0, ...)`` matters: a negative deadline would read as 'unbounded'
    to ``invoke_reason_with_retry``, silently restoring the bug."""
    from infra_brain.agents.coverage import CoverageAgent

    agent = CoverageAgent.__new__(CoverageAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    agent._llm = None
    captured = []

    def _reason(prompt, tools, deadline_seconds=None):
        captured.append(deadline_seconds)
        return '{"resource_types": ["thing"]}'

    agent.reason = MagicMock(side_effect=_reason)

    session_cm = MagicMock()
    session_cm.return_value.__enter__.return_value.query.return_value.filter_by.return_value.first.return_value = None
    session_cm.return_value.__enter__.return_value.query.return_value.filter_by.return_value.all.return_value = []

    fake_clock = MagicMock()
    # start, budget check (inside), deadline calc (already way past the budget)
    fake_clock.monotonic.side_effect = [0.0, 1.0, 9999.0, 9999.0, 9999.0]

    with (
        patch("infra_brain.agents.coverage.get_session", session_cm),
        patch("infra_brain.supervisor.AGENT_REGISTRY", {"alpha": object()}),
        patch("infra_brain.agents.coverage.get_settings") as gs,
        patch("infra_brain.agents.coverage.time", fake_clock),
    ):
        gs.return_value.coverage_gap_llm_cap = 5
        gs.return_value.collect_timeout_seconds = 300
        gs.return_value.default_zone = "corporate"
        agent.discover()

    assert captured, "reason() should still have been called once"
    # A deadline must actually be supplied (BASE passed none at all) and must
    # never be negative — invoke_reason_with_retry reads `<= 0` as "unbounded",
    # which would silently restore the very bug this guards.
    assert all(d is not None for d in captured)
    assert all(d >= 0 for d in captured)


@pytest.mark.parametrize("max_iters", [1, 4, 6, 8, 10, 18])
def test_react_recursion_limit_still_derives_from_max_iters(max_iters):
    """Guard the invariant the budget wording in the system prompt depends on:
    one ReAct iteration is two graph steps, so recursion_limit = 2n + 1."""
    from unittest.mock import MagicMock as MM

    from langchain_core.messages import AIMessage

    from infra_brain.agents.llm_base import LLMAgent

    agent = LLMAgent.__new__(LLMAgent)
    agent.callbacks = []
    agent.llm = MM()
    fake_compiled = MM()
    fake_compiled.invoke.return_value = {"messages": [AIMessage(content="ok")]}

    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
    ):
        agent.reason("task", tools=[], max_iters=max_iters)

    config = fake_compiled.invoke.call_args.kwargs["config"]
    assert config["recursion_limit"] == 2 * max_iters + 1
