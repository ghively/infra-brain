"""Tests for tools/agent_task_runs.py (P6.1, convergence plan Phase 6).

Mirrors test_client_state.py's conventions: in-memory SQLite via StaticPool,
ORM schema built with Base.metadata.create_all, get_session patched to a
session factory bound to that engine.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import AgentTaskRun
from infra_brain.tools.agent_task_runs import (
    complete_agent_task_run,
    get_agent_task_runs,
    start_agent_task_run,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture(autouse=True)
def _patch_get_session(engine, monkeypatch):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr("infra_brain.tools.agent_task_runs.get_session", _get_session)
    return _get_session


def _count(engine) -> int:
    with Session(engine) as s:
        return s.query(AgentTaskRun).count()


# ---------------------------------------------------------------------------
# start_agent_task_run
# ---------------------------------------------------------------------------


def test_start_agent_task_run_success(engine):
    result = start_agent_task_run("drift-triage", "Summarize new drift events.")

    assert result["task_name"] == "drift-triage"
    assert result["status"] == "running"
    assert result["id"]
    assert result["completed_at"] is None
    assert _count(engine) == 1


def test_start_agent_task_run_rejects_empty_task_name(engine):
    with pytest.raises(ValueError, match="task_name"):
        start_agent_task_run("   ", "prompt")
    assert _count(engine) == 0


def test_start_agent_task_run_rejects_empty_prompt(engine):
    with pytest.raises(ValueError, match="prompt"):
        start_agent_task_run("drift-triage", "   ")
    assert _count(engine) == 0


def test_start_agent_task_run_rejects_oversized_task_name(engine):
    with pytest.raises(ValueError, match="128 characters"):
        start_agent_task_run("x" * 129, "prompt")
    assert _count(engine) == 0


# ---------------------------------------------------------------------------
# complete_agent_task_run
# ---------------------------------------------------------------------------


def test_complete_agent_task_run_success(engine):
    started = start_agent_task_run("drift-triage", "prompt")

    result = complete_agent_task_run(
        started["id"], transcript="No new drift found.", result_meta={"findings": 0}
    )

    assert result["status"] == "completed"
    assert result["transcript"] == "No new drift found."
    assert result["error"] is None
    assert result["result_meta"] == {"findings": 0}
    assert result["completed_at"] is not None


def test_complete_agent_task_run_failure(engine):
    started = start_agent_task_run("drift-triage", "prompt")

    result = complete_agent_task_run(started["id"], error="LLM call timed out")

    assert result["status"] == "failed"
    assert result["error"] == "LLM call timed out"
    assert result["transcript"] is None


def test_complete_agent_task_run_error_wins_over_transcript(engine):
    """If both transcript and error are given, error determines failed status."""
    started = start_agent_task_run("drift-triage", "prompt")

    result = complete_agent_task_run(
        started["id"], transcript="partial output", error="crashed mid-run"
    )

    assert result["status"] == "failed"


def test_complete_agent_task_run_defaults_result_meta_to_empty_dict(engine):
    started = start_agent_task_run("drift-triage", "prompt")
    result = complete_agent_task_run(started["id"], transcript="done")
    assert result["result_meta"] == {}


def test_complete_agent_task_run_rejects_invalid_uuid(engine):
    with pytest.raises(ValueError, match="not a valid UUID"):
        complete_agent_task_run("not-a-uuid", transcript="x")


def test_complete_agent_task_run_rejects_unknown_id(engine):
    with pytest.raises(ValueError, match="no agent task run found"):
        complete_agent_task_run("00000000-0000-0000-0000-000000000000", transcript="x")


# ---------------------------------------------------------------------------
# get_agent_task_runs
# ---------------------------------------------------------------------------


def test_get_agent_task_runs_newest_first(engine):
    start_agent_task_run("drift-triage", "p1")
    start_agent_task_run("drift-triage", "p2")

    results = get_agent_task_runs("drift-triage")

    assert len(results) == 2
    assert results[0]["prompt"] == "p2"


def test_get_agent_task_runs_scoped_by_task_name(engine):
    start_agent_task_run("drift-triage", "p1")
    start_agent_task_run("eol-report", "p2")

    results = get_agent_task_runs("drift-triage")

    assert len(results) == 1
    assert results[0]["task_name"] == "drift-triage"


def test_get_agent_task_runs_none_filter_returns_all(engine):
    start_agent_task_run("drift-triage", "p1")
    start_agent_task_run("eol-report", "p2")

    assert len(get_agent_task_runs()) == 2


def test_get_agent_task_runs_respects_limit(engine):
    for i in range(5):
        start_agent_task_run("drift-triage", f"p{i}")

    assert len(get_agent_task_runs(limit=2)) == 2


def test_get_agent_task_runs_empty_when_no_runs(engine):
    assert get_agent_task_runs() == []
