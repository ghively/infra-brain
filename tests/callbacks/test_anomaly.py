"""Tests for anomalous agent-behavior detection (item 9): deny-verdict spikes
and runaway recursion loops.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from infra_brain.callbacks import anomaly
from infra_brain.agents.llm_base import RECURSION_LIMIT_MARKER
from infra_brain.db.models import AgentActionLog, AgentDecisionLog

from tests.support.pg import make_engine


def _engine():
    eng = make_engine()
    return eng


def _patched(eng):
    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    return patch.object(anomaly, "get_session", _get_session)


def _add_action_log(eng, agent, verdict, minutes_ago=1):
    now = datetime.now(timezone.utc)
    with Session(eng) as s:
        s.add(
            AgentActionLog(
                agent=agent,
                tool="some_tool",
                verdict=verdict,
                status="ok" if verdict == "allow" else "error",
                ts=now - timedelta(minutes=minutes_ago),
            )
        )
        s.commit()


def _add_decision_log(eng, agent, decision_summary, minutes_ago=1):
    now = datetime.now(timezone.utc)
    with Session(eng) as s:
        s.add(
            AgentDecisionLog(
                agent=agent,
                decision_summary=decision_summary,
                ts=now - timedelta(minutes=minutes_ago),
            )
        )
        s.commit()


def test_no_alerts_on_empty_logs():
    eng = _engine()
    with _patched(eng):
        alerts = anomaly.check_agent_anomalies()
    assert alerts == []


def test_deny_spike_triggers_alert():
    eng = _engine()
    for _ in range(12):
        _add_action_log(eng, "QueryAgent", "deny")
    with _patched(eng):
        alerts = anomaly.check_agent_anomalies(deny_spike_threshold=10)
    assert any("QueryAgent" in a and "deny-verdict spike" in a for a in alerts)


def test_deny_below_threshold_does_not_alert():
    eng = _engine()
    for _ in range(3):
        _add_action_log(eng, "QueryAgent", "deny")
    with _patched(eng):
        alerts = anomaly.check_agent_anomalies(deny_spike_threshold=10)
    assert alerts == []


def test_allow_verdicts_never_count_toward_deny_spike():
    eng = _engine()
    for _ in range(50):
        _add_action_log(eng, "QueryAgent", "allow")
    with _patched(eng):
        alerts = anomaly.check_agent_anomalies(deny_spike_threshold=10)
    assert alerts == []


def test_recursion_spike_triggers_alert():
    eng = _engine()
    for _ in range(4):
        _add_decision_log(eng, "DiscoveryAgent", RECURSION_LIMIT_MARKER)
    with _patched(eng):
        alerts = anomaly.check_agent_anomalies(recursion_spike_threshold=3)
    assert any("DiscoveryAgent" in a and "runaway reasoning loop" in a for a in alerts)


def test_recursion_below_threshold_does_not_alert():
    eng = _engine()
    _add_decision_log(eng, "DiscoveryAgent", RECURSION_LIMIT_MARKER)
    with _patched(eng):
        alerts = anomaly.check_agent_anomalies(recursion_spike_threshold=3)
    assert alerts == []


def test_ordinary_decision_logs_never_count_toward_recursion_spike():
    eng = _engine()
    for _ in range(10):
        _add_decision_log(eng, "DiscoveryAgent", "normal reasoning turn")
    with _patched(eng):
        alerts = anomaly.check_agent_anomalies(recursion_spike_threshold=3)
    assert alerts == []


def test_events_outside_window_are_not_counted():
    eng = _engine()
    for _ in range(20):
        _add_action_log(eng, "QueryAgent", "deny", minutes_ago=120)
    with _patched(eng):
        alerts = anomaly.check_agent_anomalies(window=timedelta(hours=1), deny_spike_threshold=10)
    assert alerts == []


def test_both_anomaly_types_can_fire_together_for_different_agents():
    eng = _engine()
    for _ in range(15):
        _add_action_log(eng, "QueryAgent", "deny")
    for _ in range(5):
        _add_decision_log(eng, "DiscoveryAgent", RECURSION_LIMIT_MARKER)
    with _patched(eng):
        alerts = anomaly.check_agent_anomalies()
    assert any("QueryAgent" in a for a in alerts)
    assert any("DiscoveryAgent" in a for a in alerts)
    assert len(alerts) == 2
