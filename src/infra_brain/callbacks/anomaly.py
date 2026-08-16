"""Anomalous agent-behavior detection (item 9).

Nothing currently alerts when an agent starts behaving abnormally rather than
merely failing outright — e.g. the boundary gate (``callbacks/boundary.py``)
denies an unusual volume of tool calls in a short window, or an LLM agent
repeatedly hits its LangGraph recursion limit (a runaway reasoning loop that
never converges). Both are already durably logged —
``AgentActionLog.verdict == "deny"`` per tool call, and
``AgentDecisionLog.decision_summary == RECURSION_LIMIT_MARKER`` per recursion
event (see ``agents/llm_base.py``) — but nothing reads them proactively.

``check_agent_anomalies()`` is the read side: called on the same cadence as
``check_collection_health()`` (the hourly ``_collection_health_job`` in
``scheduler.py``), delivered through the same ``NotificationAgent`` /
ops-webhook path wired for OB-1.
"""

from datetime import datetime, timedelta, timezone

from infra_brain.agents.llm_base import RECURSION_LIMIT_MARKER
from infra_brain.db.models import AgentActionLog, AgentDecisionLog
from infra_brain.db.session import get_session

# Defaults chosen to be loud enough to page on a real spike (a healthy agent
# occasionally hits a single deny from a stray tool call; the read-only
# boundary gate denying double digits in an hour means something is
# systematically wrong) while staying quiet on ordinary single denies.
DEFAULT_DENY_SPIKE_THRESHOLD = 10
DEFAULT_RECURSION_SPIKE_THRESHOLD = 3


def check_agent_anomalies(
    window: timedelta = timedelta(hours=1),
    deny_spike_threshold: int = DEFAULT_DENY_SPIKE_THRESHOLD,
    recursion_spike_threshold: int = DEFAULT_RECURSION_SPIKE_THRESHOLD,
) -> list[str]:
    """Return alert strings for agents showing anomalous behavior in *window*.

    Two patterns are watched:
      1. Deny-verdict spikes — an agent whose tool calls are being blocked by
         the boundary gate (``AgentActionLog.verdict == "deny"``) far more
         than the rare stray denial.
      2. Runaway reasoning loops — an agent repeatedly hitting its LangGraph
         recursion limit instead of converging on an answer.

    Returns [] when nothing crosses either threshold.
    """
    alerts: list[str] = []
    since = datetime.now(timezone.utc) - window

    with get_session() as session:
        deny_rows = (
            session.query(AgentActionLog.agent)
            .filter(AgentActionLog.verdict == "deny", AgentActionLog.ts >= since)
            .all()
        )
        deny_counts: dict[str, int] = {}
        for (agent,) in deny_rows:
            deny_counts[agent] = deny_counts.get(agent, 0) + 1
        for agent, count in sorted(deny_counts.items()):
            if count >= deny_spike_threshold:
                alerts.append(
                    f"{agent}: {count} denied tool call(s) in the last {window} "
                    "(deny-verdict spike — boundary gate is blocking this agent far "
                    "more than an occasional stray denial)"
                )

        recursion_rows = (
            session.query(AgentDecisionLog.agent)
            .filter(
                AgentDecisionLog.decision_summary == RECURSION_LIMIT_MARKER,
                AgentDecisionLog.ts >= since,
            )
            .all()
        )
        recursion_counts: dict[str, int] = {}
        for (agent,) in recursion_rows:
            recursion_counts[agent] = recursion_counts.get(agent, 0) + 1
        for agent, count in sorted(recursion_counts.items()):
            if count >= recursion_spike_threshold:
                alerts.append(
                    f"{agent}: hit the recursion limit {count} time(s) in the last "
                    f"{window} (possible runaway reasoning loop — not converging on tasks)"
                )

    return alerts
