"""infra_brain.etl — the deterministic collection pipeline (no LLM, no graph).

Wave 4 item 4.1 (F-009, F-004.4 / ARCHITECTURE-RECOMMENDATIONS R1): the
deterministic collectors are plain scheduled ETL connectors, not agents.

Adding a data source = writing a connector:

    1. Subclass ``ETLConnector`` in ``infra_brain/agents/<name>.py`` and set
       ``domain``.
    2. Implement ``collect(scope) -> list[dict]`` (items: ``{name, type, data}``).
       Raise ``CollectorSkipped("reason")`` when a required dependency is
       unconfigured. Never swallow-and-return-[] on failure (F-007).
    3. Optionally write normalized detail rows via ``self._write_details(...)``.
    4. Register the class in ``supervisor.AGENT_REGISTRY`` and schedule it in
       ``scheduler._DEFAULT_SCHEDULES`` (or use ``/agent-register``).

Structural guarantee: nothing in this package imports ``infra_brain.llm`` or
``langgraph`` — a connector cannot construct a model or a graph. LLM-capable
agents subclass ``infra_brain.agents.base.BaseAgent`` instead, which layers the
lazy model on top of this class.
"""

from infra_brain.etl.base import (
    CollectionResult,
    CollectorSkipped,
    ETLConnector,
    count_drift_events_for_run,
)

__all__ = [
    "CollectionResult",
    "CollectorSkipped",
    "ETLConnector",
    "count_drift_events_for_run",
]
