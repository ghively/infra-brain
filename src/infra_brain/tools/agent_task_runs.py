"""Headless-runner agent task run records (P6.1, convergence plan Phase 6).

Plain functions (not the MCP ``@mcp.tool`` decorator — the headless runner is
infra-brain's own process, not an external MCP client; it calls these
directly in-process, the same way ``chat/agent.py``'s tools call
``chat/tools.py`` functions directly). See ``db/models/agent_task_runs.py``
for why this table exists separately from the ``AgentActionLog`` audit trail
every tool call already produces.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Optional

from infra_brain.db.models import AgentTaskRun
from infra_brain.db.session import get_session

_STATUS_RUNNING = "running"
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _row_to_dict(row: AgentTaskRun) -> dict:
    return {
        "id": str(row.id),
        "task_name": row.task_name,
        "prompt": row.prompt,
        "status": row.status,
        "transcript": row.transcript,
        "error": row.error,
        "result_meta": row.result_meta,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def start_agent_task_run(task_name: str, prompt: str) -> dict:
    """Insert a new ``running`` task-run row. Returns the row as a dict.

    Args:
        task_name: short identifier for the standing task (e.g.
            ``"drift-triage"``). Non-empty, max 128 characters.
        prompt: the task prompt handed to the agent. Non-empty.

    Raises:
        ValueError: ``task_name`` or ``prompt`` is empty/whitespace-only,
            or ``task_name`` exceeds 128 characters.
    """
    cleaned_name = (task_name or "").strip()
    if not cleaned_name:
        raise ValueError("task_name must not be empty")
    if len(cleaned_name) > 128:
        raise ValueError("task_name must be at most 128 characters")
    cleaned_prompt = (prompt or "").strip()
    if not cleaned_prompt:
        raise ValueError("prompt must not be empty")

    with get_session() as session:
        row = AgentTaskRun(task_name=cleaned_name, prompt=cleaned_prompt, status=_STATUS_RUNNING)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def complete_agent_task_run(
    run_id: str,
    *,
    transcript: Optional[str] = None,
    error: Optional[str] = None,
    result_meta: Optional[dict[str, Any]] = None,
) -> dict:
    """Mark a task run completed or failed. Returns the updated row as a dict.

    Args:
        run_id: the run's UUID (string form), from ``start_agent_task_run``'s
            return value.
        transcript: the final assistant message text. Presence (vs.
            ``error``) determines the resulting status: given ``error`` ->
            ``status="failed"``; otherwise -> ``status="completed"``.
        error: an error string when the run failed. Mutually exclusive with
            treating the run as a success — if both are given, ``error``
            wins (status is ``failed``).
        result_meta: optional small JSON-serializable summary (e.g.
            ``{"issue_url": "...", "findings_count": 3}``). Defaults to
            ``{}``.

    Raises:
        ValueError: ``run_id`` is not a valid UUID, or no run exists with
            that id.
    """
    try:
        parsed_id = uuid.UUID(str(run_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"run_id {run_id!r} is not a valid UUID") from exc

    with get_session() as session:
        row = session.get(AgentTaskRun, parsed_id)
        if row is None:
            raise ValueError(f"no agent task run found with id {run_id}")
        row.status = _STATUS_FAILED if error else _STATUS_COMPLETED
        row.transcript = transcript
        row.error = error
        row.result_meta = result_meta or {}
        row.completed_at = _now_utc()
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def get_agent_task_runs(task_name: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Return task runs, most recently started first, optionally filtered.

    Args:
        task_name: when given, only rows with this exact ``task_name`` are
            returned.
        limit: maximum rows to return (default 50).

    Returns:
        A list of dicts (see ``_row_to_dict``). Empty list if nothing
        matches (never raises for "no rows").
    """
    with get_session() as session:
        query = session.query(AgentTaskRun)
        if task_name:
            query = query.filter(AgentTaskRun.task_name == task_name)
        rows = query.order_by(AgentTaskRun.started_at.desc()).limit(limit).all()
        return [_row_to_dict(row) for row in rows]


__all__ = [
    "start_agent_task_run",
    "complete_agent_task_run",
    "get_agent_task_runs",
]
