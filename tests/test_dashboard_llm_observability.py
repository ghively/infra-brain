"""Tests for the T7 LLM observability endpoints.

  GET /api/dashboard/llm/summary
  GET /api/dashboard/llm/runs
  GET /api/dashboard/llm/runs/{run_id}

Conventions mirror tests/test_dashboard_sweeps.py: in-memory SQLite via
tests.support.pg.make_engine, ORM schema from Base.metadata, get_session
patched to the test engine, dev-mode auth-off client.

The aggregation assertions use a HAND-COMPUTED fixture (see
``_seed_reference_runs``) — the expected totals below are written out by hand
from the seeded rows, not derived by re-running the implementation's own
arithmetic.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.agents.llm_base import RECURSION_LIMIT_MARKER
from infra_brain.db.models import AgentDecisionLog

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    return make_engine()


@contextmanager
def _session_factory(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine, monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    def _get_session():
        return _session_factory(engine)

    from infra_brain.api.routers.llm_observability import llm_observability_router

    app = FastAPI()
    app.include_router(llm_observability_router)
    with patch("infra_brain.api.routers.llm_observability.get_session", _get_session):
        yield TestClient(app)
    get_settings.cache_clear()


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Hand-computed fixture                                                        #
# --------------------------------------------------------------------------- #
#
# RUN A — DiscoveryAgent / discovery — RECURSION LIMIT
#   it 0: 100 tok, tools [t1, t1, t2]           reasoning "" (silent, tool turn)
#   it 1: 250 tok, tools [t1]                   reasoning "thought about t1"
#   it -1: marker row (no tokens, no tools)
#   => turns 2, tokens_billed 350, peak 250, tool_calls 4, distinct 2,
#      max_tool_repeat 2 (t1 twice in iteration 0), narrated 1, silent 1,
#      outcome recursion_limit
#
# RUN B — DiscoveryAgent / discovery — COMPLETED
#   it 0: 40 tok, tools [t2]                    reasoning "look it up"
#   it 1: 60 tok, tools []                      reasoning "final answer"
#   => turns 2, tokens 100, peak 60, tool_calls 1, distinct 1,
#      max_tool_repeat 1, narrated 2, silent 0, outcome completed
#
# RUN C — CoverageAgent / coverage — TRUNCATED
#   it 0: 10 tok, tools [t3]                    reasoning "" (silent tool turn)
#   => turns 1, tokens 10, peak 10, tool_calls 1, distinct 1,
#      max_tool_repeat 1, narrated 0, silent 1, outcome truncated
#
# FLEET TOTALS: runs 3, turns 5, tokens 460, peak 250, tool_calls 6,
#               narrated 3, silent 2, outcomes {completed 1, recursion 1,
#               truncated 1}
# TOOL TOTALS:  t1 3 calls, t2 2 calls, t3 1 call

RUN_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
RUN_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
RUN_C = uuid.UUID("cccccccc-0000-4000-8000-000000000003")


def _row(run_id, agent, domain, iteration, ts, tokens, tools, reasoning, summary=""):
    return AgentDecisionLog(
        run_id=run_id,
        agent=agent,
        domain=domain,
        iteration=iteration,
        ts=ts,
        token_count=tokens,
        tools_chosen=tools,
        reasoning_text=reasoning,
        decision_summary=summary,
    )


def _seed_reference_runs(engine):
    t0 = _now() - timedelta(hours=3)
    with Session(engine) as s:
        s.add_all(
            [
                # RUN C is oldest, RUN A newest, so /llm/runs must return A, B, C.
                _row(RUN_C, "CoverageAgent", "coverage", 0, t0, 10, ["t3"], ""),
                _row(
                    RUN_B,
                    "DiscoveryAgent",
                    "discovery",
                    0,
                    t0 + timedelta(minutes=10),
                    40,
                    ["t2"],
                    "look it up",
                    "look it up",
                ),
                _row(
                    RUN_B,
                    "DiscoveryAgent",
                    "discovery",
                    1,
                    t0 + timedelta(minutes=11),
                    60,
                    [],
                    "final answer",
                    "final answer",
                ),
                _row(
                    RUN_A,
                    "DiscoveryAgent",
                    "discovery",
                    0,
                    t0 + timedelta(minutes=20),
                    100,
                    ["t1", "t1", "t2"],
                    "",
                ),
                _row(
                    RUN_A,
                    "DiscoveryAgent",
                    "discovery",
                    1,
                    t0 + timedelta(minutes=21),
                    250,
                    ["t1"],
                    "thought about t1",
                    "thought about t1",
                ),
                _row(
                    RUN_A,
                    "DiscoveryAgent",
                    "discovery",
                    -1,
                    t0 + timedelta(minutes=22),
                    None,
                    [],
                    "",
                    RECURSION_LIMIT_MARKER,
                ),
            ]
        )
        s.commit()


# --------------------------------------------------------------------------- #
# Empty database                                                               #
# --------------------------------------------------------------------------- #


def test_summary_on_empty_db_is_zeroed_but_still_reports_flags_and_metric(client):
    body = client.get("/api/dashboard/llm/summary").json()
    assert body["runs"] == 0
    assert body["turns"] == 0
    assert body["tokens_billed"] == 0
    assert body["outcomes"] == {
        "completed": 0,
        "recursion_limit": 0,
        "truncated": 0,
        "unknown": 0,
    }
    assert body["by_agent"] == []
    assert body["top_tools"] == []
    # An empty page must still explain WHY a section could be empty.
    names = {f["name"] for f in body["flags"]}
    assert names == {
        "rootcause_llm_enabled",
        "compliance_gap_finder_enabled",
        "remediation_interrupt_enabled",
        "langfuse_enabled",
    }
    assert all(f["enabled"] is False for f in body["flags"])
    assert all(f["effect"] for f in body["flags"])
    assert "per-CALL" in body["token_metric"]
    assert body["truncated_scan"] is False


def test_runs_on_empty_db_returns_empty_page(client):
    body = client.get("/api/dashboard/llm/runs").json()
    assert body == {
        "items": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
        "token_metric": body["token_metric"],
    }


# --------------------------------------------------------------------------- #
# Aggregation math                                                             #
# --------------------------------------------------------------------------- #


def test_summary_totals_match_hand_computed_fixture(engine, client):
    _seed_reference_runs(engine)
    body = client.get("/api/dashboard/llm/summary").json()

    assert body["runs"] == 3
    assert body["turns"] == 5, "the iteration=-1 marker row must not count as a turn"
    assert body["tokens_billed"] == 460
    assert body["peak_call_tokens"] == 250
    assert body["tool_calls"] == 6
    assert body["narrated_turns"] == 3
    assert body["silent_turns"] == 2
    assert body["outcomes"] == {
        "completed": 1,
        "recursion_limit": 1,
        "truncated": 1,
        "unknown": 0,
    }
    assert body["rows_scanned"] == 6


def test_summary_per_agent_breakdown_matches_hand_computed_fixture(engine, client):
    _seed_reference_runs(engine)
    by_agent = {a["agent"]: a for a in client.get("/api/dashboard/llm/summary").json()["by_agent"]}

    disc = by_agent["DiscoveryAgent"]
    assert disc["domain"] == "discovery"
    assert disc["runs"] == 2
    assert disc["turns"] == 4
    assert disc["tokens_billed"] == 450  # 350 (A) + 100 (B)
    assert disc["peak_call_tokens"] == 250
    assert disc["tool_calls"] == 5
    assert disc["narrated_turns"] == 3
    assert disc["silent_turns"] == 1
    assert disc["completed"] == 1
    assert disc["recursion_limit"] == 1
    assert disc["truncated"] == 0

    cov = by_agent["CoverageAgent"]
    assert cov["runs"] == 1
    assert cov["turns"] == 1
    assert cov["tokens_billed"] == 10
    assert cov["truncated"] == 1
    assert cov["completed"] == 0


def test_summary_tool_frequency_counts_repeats_within_one_iteration(engine, client):
    _seed_reference_runs(engine)
    tools = {t["tool"]: t for t in client.get("/api/dashboard/llm/summary").json()["top_tools"]}
    # t1 is called twice inside iteration 0 of RUN A and once in iteration 1 —
    # a naive DISTINCT would report 2.
    assert tools["t1"]["calls"] == 3
    assert tools["t1"]["max_in_one_iteration"] == 2
    assert tools["t2"]["calls"] == 2
    assert tools["t3"]["calls"] == 1


def test_summary_window_excludes_older_rows(engine, client):
    _seed_reference_runs(engine)
    old = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            _row(
                old,
                "DiscoveryAgent",
                "discovery",
                0,
                _now() - timedelta(days=40),
                9_999,
                [],
                "ancient",
            )
        )
        s.commit()

    recent = client.get("/api/dashboard/llm/summary?window_hours=24").json()
    assert recent["runs"] == 3
    assert recent["tokens_billed"] == 460

    wide = client.get("/api/dashboard/llm/summary?window_hours=2000").json()
    assert wide["runs"] == 4
    assert wide["tokens_billed"] == 460 + 9_999


def test_summary_rejects_out_of_range_window(client):
    assert client.get("/api/dashboard/llm/summary?window_hours=0").status_code == 422
    assert client.get("/api/dashboard/llm/summary?window_hours=999999").status_code == 422


# --------------------------------------------------------------------------- #
# Outcome classification                                                       #
# --------------------------------------------------------------------------- #


def test_recursion_limit_run_is_distinguishable_from_completed_run(engine, client):
    _seed_reference_runs(engine)
    runs = {r["run_id"]: r for r in client.get("/api/dashboard/llm/runs").json()["items"]}

    assert runs[str(RUN_A)]["outcome"] == "recursion_limit"
    assert runs[str(RUN_B)]["outcome"] == "completed"
    assert runs[str(RUN_C)]["outcome"] == "truncated"


def test_run_detail_reports_outcome_reason_and_omits_the_marker_row(engine, client):
    _seed_reference_runs(engine)
    body = client.get(f"/api/dashboard/llm/runs/{RUN_A}").json()

    assert body["outcome"] == "recursion_limit"
    assert "recursion limit" in body["outcome_reason"]
    # The marker is an outcome fact, not a model turn — it must not appear as a
    # phantom iteration in the ladder.
    assert [s["iteration"] for s in body["steps"]] == [0, 1]
    assert body["turns"] == 2
    assert body["max_tool_repeat"] == 2


def test_run_with_only_a_marker_row_is_unknown_not_completed(engine, client):
    rid = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            _row(
                rid, "DiscoveryAgent", "discovery", -1, _now(), None, [], "", RECURSION_LIMIT_MARKER
            )
        )
        s.commit()
    # A marker with no turns still classifies as recursion_limit (the marker is
    # the strongest signal); a run with neither turns nor marker is "unknown".
    body = client.get("/api/dashboard/llm/summary").json()
    assert body["outcomes"]["recursion_limit"] == 1
    assert body["turns"] == 0


# --------------------------------------------------------------------------- #
# Reasoning absence                                                            #
# --------------------------------------------------------------------------- #


def test_step_reasoning_state_distinguishes_tool_turn_from_no_narration(engine, client):
    _seed_reference_runs(engine)
    steps = {
        s["iteration"]: s for s in client.get(f"/api/dashboard/llm/runs/{RUN_A}").json()["steps"]
    }

    # iteration 0: empty reasoning WITH tool calls -> the model acted silently.
    assert steps[0]["reasoning_text"] == ""
    assert steps[0]["reasoning_state"] == "absent_tool_call_turn"
    assert steps[0]["tool_repeats"] == {"t1": 2}
    # iteration 1: narrated.
    assert steps[1]["reasoning_state"] == "present"
    assert steps[1]["reasoning_text"] == "thought about t1"
    assert steps[1]["tool_repeats"] == {}


def test_step_with_no_tools_and_no_reasoning_is_no_narration(engine, client):
    rid = uuid.uuid4()
    with Session(engine) as s:
        s.add(_row(rid, "QueryAgent", "query", 0, _now(), 5, [], ""))
        s.commit()
    steps = client.get(f"/api/dashboard/llm/runs/{rid}").json()["steps"]
    assert steps[0]["reasoning_state"] == "absent_no_narration"


# --------------------------------------------------------------------------- #
# Pagination / filtering / errors                                              #
# --------------------------------------------------------------------------- #


def test_runs_are_paginated_newest_first(engine, client):
    _seed_reference_runs(engine)

    page1 = client.get("/api/dashboard/llm/runs?limit=2&offset=0").json()
    assert page1["total"] == 3
    assert page1["limit"] == 2
    assert [r["run_id"] for r in page1["items"]] == [str(RUN_A), str(RUN_B)]

    page2 = client.get("/api/dashboard/llm/runs?limit=2&offset=2").json()
    assert [r["run_id"] for r in page2["items"]] == [str(RUN_C)]


def test_runs_limit_is_clamped_and_never_unbounded(engine, client):
    _seed_reference_runs(engine)
    body = client.get("/api/dashboard/llm/runs?limit=100000").json()
    assert body["limit"] == 200
    body = client.get("/api/dashboard/llm/runs?limit=0&offset=-5").json()
    assert body["limit"] == 1
    assert body["offset"] == 0


def test_runs_filter_by_agent_and_outcome(engine, client):
    _seed_reference_runs(engine)
    only_cov = client.get("/api/dashboard/llm/runs?agent=CoverageAgent").json()
    assert [r["run_id"] for r in only_cov["items"]] == [str(RUN_C)]

    completed = client.get("/api/dashboard/llm/runs?outcome=completed").json()
    assert [r["run_id"] for r in completed["items"]] == [str(RUN_B)]


def test_run_detail_404s_on_unknown_and_malformed_ids(client):
    assert client.get(f"/api/dashboard/llm/runs/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/dashboard/llm/runs/not-a-uuid").status_code == 404


def test_tools_chosen_holding_junk_does_not_500(engine, client):
    rid = uuid.uuid4()
    with Session(engine) as s:
        s.add(_row(rid, "QueryAgent", "query", 0, _now(), 5, ["ok", 7, None], "hi"))
        s.commit()
    body = client.get(f"/api/dashboard/llm/runs/{rid}").json()
    assert body["steps"][0]["tools_chosen"] == ["ok"]


# --------------------------------------------------------------------------- #
# Auth                                                                         #
# --------------------------------------------------------------------------- #


def test_endpoints_require_a_session_when_not_in_dev_mode(engine, monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.delenv("INFRA_BRAIN_DEV", raising=False)
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "1")
    get_settings.cache_clear()

    from infra_brain.api.routers.llm_observability import llm_observability_router

    app = FastAPI()
    app.include_router(llm_observability_router)

    def _get_session():
        return _session_factory(engine)

    with patch("infra_brain.api.routers.llm_observability.get_session", _get_session):
        c = TestClient(app)
        for url in (
            "/api/dashboard/llm/summary",
            "/api/dashboard/llm/runs",
            f"/api/dashboard/llm/runs/{uuid.uuid4()}",
        ):
            assert c.get(url).status_code == 401, url
    get_settings.cache_clear()
