"""Headless-runner agent task transcripts (P6.1, convergence plan Phase 6).

The scheduler's new agent-task job type (``runner/agent_task.py``) drives a
LangGraph ReAct turn against a task prompt with no interactive session
behind it — unlike the dashboard chat agent (``chat/agent.py``), whose
Postgres checkpointer preserves per-thread message history for a human to
scroll back through, a standing task's "thread" is one-shot and disposable
(fresh MemorySaver per run — see that module's docstring for why: a
process-wide Postgres-backed checkpointer singleton is unsafe to reuse
across the repeated ``asyncio.run()`` calls a recurring cron job makes,
since asyncpg/psycopg connection pools are bound to the event loop they were
created on). This table is the durable record of what a standing task
actually did, independent of that disposable in-memory thread state.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ._base import JSONB, Base, _now, _uuid


class AgentTaskRun(Base):
    """One headless-runner standing-task execution.

    ``status`` is one of ``running`` / ``completed`` / ``failed`` — set to
    ``running`` at insert, updated in place on completion (this table is the
    one exception in the P4.2g/P6.1 governance surfaces that DOES update an
    existing row: a task run's own lifecycle status, not audit history).
    ``transcript`` holds the final assistant message text (or an error
    string on failure); the full LangChain message list is NOT persisted
    here — every tool call already lands its own row in ``AgentActionLog``
    via the existing audit callback chain (``callbacks/registry.py``'s
    ``build_async_callbacks``), so this table only needs the human-readable
    summary, not a duplicate of that audit trail.
    """

    __tablename__ = "agent_task_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    task_name: Mapped[str] = mapped_column(String(128))
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="running")
    transcript: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_agent_task_runs_task_name", AgentTaskRun.task_name)
Index("ix_agent_task_runs_started_at", AgentTaskRun.started_at)


__all__ = ["AgentTaskRun"]
