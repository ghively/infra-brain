"""Phase 3 Task 4 — remediation interrupt mini-graph tests.

Covers: interrupt round-trip (draft → interrupt → resume approved → execute
through the write-gated MR path), resume-rejected → no execute, stale
checkpoint vs unapproved DB row → no execute (DB is source of truth),
MemorySaver hard-fail, flag-OFF byte-identity of the drafting flow,
branch-collision idempotency, resume NON-fatality + poll fallback, and both
endpoint surfaces resuming via the shared helper.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy.orm import Session

from infra_brain import remediation_graph as rg
from infra_brain.db.models import DriftEvent, ProposedAction, Resource

from tests.support.pg import make_engine


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_sessions(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with (
        patch("infra_brain.remediation_graph.get_session", _get_session),
        patch("infra_brain.agents.remediation.get_session", _get_session),
    ):
        yield engine


@pytest.fixture
def memory_graph(monkeypatch):
    """Compile the mini-graph on a MemorySaver, bypassing the durable-saver
    guard (which is pinned separately by test_memorysaver_hard_fails)."""
    compiled = rg._build().compile(checkpointer=MemorySaver())
    monkeypatch.setattr(rg, "_GRAPH", compiled)
    return compiled


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(
        rg, "get_settings", lambda: SimpleNamespace(remediation_interrupt_enabled=True)
    )


def _stub_agent(project_id: int = 42):
    from infra_brain.agents.remediation import RemediationAgent

    agent = RemediationAgent.__new__(RemediationAgent)
    agent.settings = SimpleNamespace(
        remediation_project_id=project_id,
        remediation_branch="main",
        gitlab_url="https://gitlab.example.com",
    )
    agent.callbacks = []
    return agent


def _seed_action(engine, status: str = "pending") -> str:
    with Session(engine) as s:
        action = ProposedAction(
            agent="RemediationAgent",
            action_type="config_fix",
            target=f"drift:{uuid.uuid4()}",
            payload={"host": "web-01", "field": "kernel", "plan": "# fix kernel"},
            confidence=0.8,
            status=status,
        )
        s.add(action)
        s.commit()
        return str(action.id)


def _get_action(engine, action_id: str) -> ProposedAction:
    with Session(engine) as s:
        return s.get(ProposedAction, uuid.UUID(action_id))


def _snapshot(engine, action_id: str) -> SimpleNamespace:
    a = _get_action(engine, action_id)
    return SimpleNamespace(id=a.id, thread_id=a.thread_id, graph_version=a.graph_version)


# --------------------------------------------------------------------------- #
# Checkpointer guard                                                           #
# --------------------------------------------------------------------------- #


def test_memorysaver_hard_fails():
    with pytest.raises(RuntimeError, match="durable Postgres checkpointer"):
        rg.build_remediation_graph(MemorySaver())
    with pytest.raises(RuntimeError, match="durable Postgres checkpointer"):
        rg.build_remediation_graph(None)


# --------------------------------------------------------------------------- #
# Interrupt round-trip                                                         #
# --------------------------------------------------------------------------- #


def test_start_parks_on_interrupt_and_stamps_row(patched_sessions, memory_graph):
    engine = patched_sessions
    action_id = _seed_action(engine)

    result = rg.start_remediation_action_sync(action_id)

    interrupts = result.get("__interrupt__") or []
    assert interrupts, "graph must park on the wait_approval interrupt"
    assert interrupts[0].value["action_id"] == action_id
    assert interrupts[0].value["host"] == "web-01"

    a = _get_action(engine, action_id)
    assert a.thread_id == f"remediation:{action_id}"
    assert a.graph_version == rg.GRAPH_VERSION
    assert a.status == "pending"  # starting the graph never flips status


async def test_round_trip_approved_executes_through_write_gate(
    patched_sessions, memory_graph, flag_on, monkeypatch
):
    """draft → interrupt → approve row → resume → execute, with the REAL
    create_inventory_mr proving the write gate is traversed (not bypassed):
    gate_external_write must run BEFORE any GitLab HTTP call."""
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")
    engine = patched_sessions
    action_id = _seed_action(engine)
    rg.start_remediation_action_sync(action_id)

    # Approve the row in the DB (what the endpoint's to_thread body does).
    with Session(engine) as s:
        s.get(ProposedAction, uuid.UUID(action_id)).status = "approved"
        s.commit()

    calls: list[str] = []

    def _resp(status=200, json_data=None, text=""):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_data if json_data is not None else {}
        resp.text = text
        resp.raise_for_status.return_value = None
        return resp

    fake_httpx = MagicMock()
    fake_httpx.post.side_effect = lambda *a, **k: (
        calls.append("http"),
        _resp(json_data={"web_url": "http://gitlab/mr/7"}),
    )[1]
    fake_httpx.get.side_effect = lambda *a, **k: (calls.append("http"), _resp(404, []))[1]
    fake_httpx.put.side_effect = lambda *a, **k: (calls.append("http"), _resp())[1]

    gate = MagicMock(side_effect=lambda **kw: calls.append("gate"))
    gitlab_settings = SimpleNamespace(
        gitlab_url="https://gitlab.example.com",
        gitlab_token="tok",
        gitlab_ssl_verify=False,
        api_timeout_seconds=5,
    )

    with (
        patch("infra_brain.remediation_graph._make_agent", _stub_agent),
        patch("infra_brain.tools.gitlab_mr.gate_external_write", gate),
        patch("infra_brain.tools.gitlab_mr.httpx", fake_httpx),
        patch("infra_brain.tools.gitlab_mr.get_settings", lambda: gitlab_settings),
    ):
        resumed = await rg.resume_remediation_action(_snapshot(engine, action_id), approved=True)

    assert resumed is True
    assert gate.called, "write gate must be traversed"
    assert calls and calls[0] == "gate", "gate must run BEFORE any GitLab HTTP call"
    a = _get_action(engine, action_id)
    assert a.status == "executed"
    assert a.result_url == "http://gitlab/mr/7"


async def test_resume_rejected_never_executes(patched_sessions, memory_graph, flag_on, monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")
    engine = patched_sessions
    action_id = _seed_action(engine)
    rg.start_remediation_action_sync(action_id)
    with Session(engine) as s:
        s.get(ProposedAction, uuid.UUID(action_id)).status = "rejected"
        s.commit()

    mr = MagicMock()
    with (
        patch("infra_brain.remediation_graph._make_agent", _stub_agent),
        patch("infra_brain.agents.remediation.create_inventory_mr", mr),
    ):
        resumed = await rg.resume_remediation_action(_snapshot(engine, action_id), approved=False)

    assert resumed is True  # graph resumed and finished cleanly ...
    assert not mr.called  # ... but nothing was executed
    assert _get_action(engine, action_id).status == "rejected"


async def test_stale_checkpoint_unapproved_row_never_executes(
    patched_sessions, memory_graph, flag_on, monkeypatch
):
    """DB is the source of truth: a resume claiming approval against a row
    that is NOT status='approved' must refuse to execute."""
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")
    engine = patched_sessions
    action_id = _seed_action(engine)
    rg.start_remediation_action_sync(action_id)
    # Row deliberately left "pending" — the checkpoint's approved=True is stale.

    mr = MagicMock()
    with (
        patch("infra_brain.remediation_graph._make_agent", _stub_agent),
        patch("infra_brain.agents.remediation.create_inventory_mr", mr),
    ):
        resumed = await rg.resume_remediation_action(_snapshot(engine, action_id), approved=True)

    assert resumed is True
    assert not mr.called
    assert _get_action(engine, action_id).status == "pending"


# --------------------------------------------------------------------------- #
# Resume helper policy                                                         #
# --------------------------------------------------------------------------- #


async def test_resume_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(
        rg, "get_settings", lambda: SimpleNamespace(remediation_interrupt_enabled=False)
    )
    resume = AsyncMock()
    monkeypatch.setattr(rg, "_resume", resume)
    snap = SimpleNamespace(id=uuid.uuid4(), thread_id="t", graph_version=rg.GRAPH_VERSION)
    assert await rg.resume_remediation_action(snap, approved=True) is False
    assert not resume.called


async def test_resume_skips_foreign_graph_version(flag_on, monkeypatch):
    """Threads from an older/foreign graph version are never resumed — the
    _execute_approved poll handles them (startup/fallback sweep policy)."""
    resume = AsyncMock()
    monkeypatch.setattr(rg, "_resume", resume)
    snap = SimpleNamespace(id=uuid.uuid4(), thread_id="t", graph_version="remediation-v0")
    assert await rg.resume_remediation_action(snap, approved=True) is False
    assert not resume.called


async def test_resume_failure_is_nonfatal_and_poll_executes_later(
    patched_sessions, flag_on, monkeypatch
):
    """A resume error must not raise — the row is already approved, and the
    _execute_approved poll (registered in BOTH flag modes) executes it later."""
    engine = patched_sessions
    action_id = _seed_action(engine, status="approved")
    with Session(engine) as s:
        a = s.get(ProposedAction, uuid.UUID(action_id))
        a.thread_id = f"remediation:{action_id}"
        a.graph_version = rg.GRAPH_VERSION
        s.commit()

    async def _boom(thread_id, approved):
        raise RuntimeError("checkpointer down")

    monkeypatch.setattr(rg, "_resume", _boom)
    assert await rg.resume_remediation_action(_snapshot(engine, action_id), approved=True) is False

    # Poll fallback picks it up.
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")
    mr = MagicMock(return_value="http://gitlab/mr/9")
    with patch("infra_brain.agents.remediation.create_inventory_mr", mr):
        executed = _stub_agent()._execute_approved()
    assert executed == 1
    assert _get_action(engine, action_id).status == "executed"


# --------------------------------------------------------------------------- #
# Branch-collision idempotency                                                 #
# --------------------------------------------------------------------------- #


def _branch_exists_error():
    req = httpx.Request("POST", "https://gitlab.example.com/api/v4/projects/42/repository/branches")
    resp = httpx.Response(400, request=req, text='{"message": "Branch already exists"}')
    return httpx.HTTPStatusError("HTTP 400", request=req, response=resp)


def test_execute_one_treats_branch_exists_as_executed(monkeypatch):
    """M-8: _execute_one no longer takes a live ProposedAction/session — it
    takes plain values and returns (new_status, result_url, error); the
    caller (_execute_sync / _execute_approved) owns persisting the outcome
    in its own fresh, short-lived session opened AFTER this call returns."""
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")
    action_id = uuid.uuid4()
    payload = {"host": "web-01", "field": "kernel", "plan": "# fix kernel"}

    mr = MagicMock(side_effect=_branch_exists_error())
    with patch("infra_brain.agents.remediation.create_inventory_mr", mr):
        new_status, result_url, error = _stub_agent()._execute_one(
            action_id, "config_fix", payload
        )
    assert mr.call_count == 1  # 400 is non-retryable — no retries burned
    assert new_status == "executed"
    assert result_url is None
    assert error is None


def test_execute_one_already_exists_without_branch_word_is_not_treated_as_executed(monkeypatch):
    """Nit fix: the match must be anchored on "branch" AND "already exists" —
    an unrelated 400 that happens to say "already exists" (e.g. an existing
    file/commit conflict) must NOT be misread as the branch-collision
    idempotency case."""
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")
    action_id = uuid.uuid4()
    payload = {"host": "web-01", "field": "kernel", "plan": "# fix kernel"}
    req = httpx.Request("POST", "https://gitlab.example.com/mr")
    err = httpx.HTTPStatusError(
        "HTTP 400",
        request=req,
        response=httpx.Response(400, request=req, text='{"message": "File already exists"}'),
    )
    with patch("infra_brain.agents.remediation.create_inventory_mr", MagicMock(side_effect=err)):
        new_status, result_url, error = _stub_agent()._execute_one(
            action_id, "config_fix", payload
        )
    assert new_status is None
    assert result_url is None
    assert error is not None  # a real failure, not a benign no-op


def test_execute_one_other_400_is_failure_not_executed(monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")
    action_id = uuid.uuid4()
    payload = {"host": "web-01", "field": "kernel", "plan": "# fix kernel"}
    req = httpx.Request("POST", "https://gitlab.example.com/mr")
    err = httpx.HTTPStatusError(
        "HTTP 400", request=req, response=httpx.Response(400, request=req, text="bad ref")
    )
    with patch("infra_brain.agents.remediation.create_inventory_mr", MagicMock(side_effect=err)):
        new_status, result_url, error = _stub_agent()._execute_one(
            action_id, "config_fix", payload
        )
    assert new_status is None
    assert result_url is None
    assert error is not None  # a real failure, not a benign no-op


# --------------------------------------------------------------------------- #
# Flag OFF byte-identity / flag ON graph kick-off                              #
# --------------------------------------------------------------------------- #


def _seed_open_drift(engine):
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-01", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(
            DriftEvent(
                resource_id=r.id,
                drift_type="config_drift",
                field="kernel",
                old_value={"v": "5.15"},
                new_value={"v": "5.19"},
                status="open",
            )
        )
        s.commit()


def test_flag_off_drafting_never_touches_the_graph(patched_sessions, monkeypatch):
    """Byte-identity: with the flag off (or absent) drafting is exactly
    today's flow — no graph start, rows unchanged in shape."""
    engine = patched_sessions
    _seed_open_drift(engine)
    start = MagicMock()
    monkeypatch.setattr(rg, "start_remediation_action_sync", start)

    agent = _stub_agent()  # settings has NO remediation_interrupt_enabled attr
    assert agent._draft_proposals() == 1
    assert not start.called
    with Session(engine) as s:
        a = s.query(ProposedAction).one()
        assert a.status == "pending"
        assert a.thread_id is None and a.graph_version is None


def test_flag_on_drafting_starts_one_graph_per_action(patched_sessions, monkeypatch):
    engine = patched_sessions
    _seed_open_drift(engine)
    start = MagicMock()
    monkeypatch.setattr(rg, "start_remediation_action_sync", start)

    agent = _stub_agent()
    agent.settings.remediation_interrupt_enabled = True
    assert agent._draft_proposals() == 1
    with Session(engine) as s:
        action_id = str(s.query(ProposedAction).one().id)
    start.assert_called_once_with(action_id)


def test_flag_on_graph_start_failure_never_breaks_drafting(patched_sessions, monkeypatch):
    engine = patched_sessions
    _seed_open_drift(engine)
    monkeypatch.setattr(
        rg, "start_remediation_action_sync", MagicMock(side_effect=RuntimeError("no loop"))
    )
    agent = _stub_agent()
    agent.settings.remediation_interrupt_enabled = True
    assert agent._draft_proposals() == 1  # drafting still succeeds
    with Session(engine) as s:
        assert s.query(ProposedAction).count() == 1


# --------------------------------------------------------------------------- #
# Both endpoint surfaces resume via the shared helper                          #
# --------------------------------------------------------------------------- #


def _webhook_client(engine, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from infra_brain.config import get_settings
    from infra_brain.webhooks import router

    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    app = FastAPI()
    app.include_router(router)
    return TestClient(app), _get_session


def test_webhook_surface_resumes_via_shared_helper(engine, monkeypatch):
    client, _get_session = _webhook_client(engine, monkeypatch)
    approve_id = _seed_action(engine)
    reject_id = _seed_action(engine)
    resume = AsyncMock(return_value=True)
    with (
        patch("infra_brain.webhooks.get_session", _get_session),
        patch("infra_brain.webhooks.resume_remediation_action", resume),
    ):
        assert client.post(f"/actions/{approve_id}/approve", json={}).status_code == 200
        assert client.post(f"/actions/{reject_id}/reject", json={}).status_code == 200

    assert resume.await_count == 2
    (snap1,), kw1 = resume.await_args_list[0]
    (snap2,), kw2 = resume.await_args_list[1]
    assert str(snap1.id) == approve_id and kw1 == {"approved": True}
    assert str(snap2.id) == reject_id and kw2 == {"approved": False}
    assert _get_action(engine, approve_id).status == "approved"
    assert _get_action(engine, reject_id).status == "rejected"


def test_dashboard_surface_resumes_via_shared_helper(engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from infra_brain.api.routers.governance_ops import governance_ops_router
    from infra_brain.dashboard_auth import require_admin, require_session

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    app = FastAPI()
    app.include_router(governance_ops_router)
    app.dependency_overrides[require_session] = lambda: None
    app.dependency_overrides[require_admin] = lambda: None

    approve_id = _seed_action(engine)
    reject_id = _seed_action(engine)
    resume = AsyncMock(return_value=True)
    with (
        patch("infra_brain.api.routers.governance_ops.get_session", _get_session),
        patch("infra_brain.api.routers.governance_ops.resume_remediation_action", resume),
    ):
        client = TestClient(app)
        r = client.post(f"/api/dashboard/actions/{approve_id}/approve", json={})
        assert r.status_code == 200
        r = client.post(f"/api/dashboard/actions/{reject_id}/reject")
        assert r.status_code == 200

    assert resume.await_count == 2
    (snap1,), kw1 = resume.await_args_list[0]
    (snap2,), kw2 = resume.await_args_list[1]
    assert str(snap1.id) == approve_id and kw1 == {"approved": True}
    assert str(snap2.id) == reject_id and kw2 == {"approved": False}
    assert _get_action(engine, approve_id).status == "approved"
    assert _get_action(engine, reject_id).status == "rejected"


def test_dashboard_approve_refuses_entity_resolution_review_rows(engine):
    """TRK-226: the generic approve endpoint only flips ProposedAction.status
    -- no agent ever re-checks entity_resolution_same_as rows (this file's
    RemediationAgent executor only handles config_fix/vuln_patch), so
    approving one here would permanently close the identity question with no
    SAME_AS edge ever written and no way to recover. Must 409, pointing at the
    real confirm route, and must NOT flip status."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from infra_brain.api.routers.governance_ops import governance_ops_router
    from infra_brain.dashboard_auth import require_admin, require_session

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with Session(engine) as s:
        review_action = ProposedAction(
            agent="graph_phase3.queue_for_review",
            action_type="entity_resolution_same_as",
            target="same-as:VsphereVM:web01",
            payload={"source_node": {"node_id": str(uuid.uuid4())}, "candidate_matches": []},
            confidence=0.85,
            status="pending",
        )
        s.add(review_action)
        s.commit()
        review_id = str(review_action.id)

    app = FastAPI()
    app.include_router(governance_ops_router)
    app.dependency_overrides[require_session] = lambda: None
    app.dependency_overrides[require_admin] = lambda: None

    with patch("infra_brain.api.routers.governance_ops.get_session", _get_session):
        client = TestClient(app)
        resp = client.post(f"/api/dashboard/actions/{review_id}/approve", json={})

    assert resp.status_code == 409
    assert "entity-resolution" in resp.json()["detail"]
    assert _get_action(engine, review_id).status == "pending"


# --------------------------------------------------------------------------- #
# MEDIUM-1: reject is guarded — only a pending action may be rejected          #
# --------------------------------------------------------------------------- #


def test_webhook_reject_guard_blocks_executed_row(engine, monkeypatch):
    """Rejecting an already-executed row would corrupt the audit record of the
    external-write agent — must 409, and the row must not be mutated."""
    client, _get_session = _webhook_client(engine, monkeypatch)
    action_id = _seed_action(engine, status="executed")
    resume = AsyncMock(return_value=True)
    with (
        patch("infra_brain.webhooks.get_session", _get_session),
        patch("infra_brain.webhooks.resume_remediation_action", resume),
    ):
        resp = client.post(f"/actions/{action_id}/reject", json={})
    assert resp.status_code == 409
    assert not resume.called
    assert _get_action(engine, action_id).status == "executed"


def test_webhook_reject_guard_allows_pending_row(engine, monkeypatch):
    client, _get_session = _webhook_client(engine, monkeypatch)
    action_id = _seed_action(engine, status="pending")
    resume = AsyncMock(return_value=True)
    with (
        patch("infra_brain.webhooks.get_session", _get_session),
        patch("infra_brain.webhooks.resume_remediation_action", resume),
    ):
        resp = client.post(f"/actions/{action_id}/reject", json={})
    assert resp.status_code == 200
    assert _get_action(engine, action_id).status == "rejected"


def _dashboard_client(engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from infra_brain.api.routers.governance_ops import governance_ops_router
    from infra_brain.dashboard_auth import require_admin, require_session

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    app = FastAPI()
    app.include_router(governance_ops_router)
    app.dependency_overrides[require_session] = lambda: None
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app), _get_session


def test_dashboard_reject_guard_blocks_executed_row(engine):
    client, _get_session = _dashboard_client(engine)
    action_id = _seed_action(engine, status="executed")
    resume = AsyncMock(return_value=True)
    with (
        patch("infra_brain.api.routers.governance_ops.get_session", _get_session),
        patch("infra_brain.api.routers.governance_ops.resume_remediation_action", resume),
    ):
        resp = client.post(f"/api/dashboard/actions/{action_id}/reject")
    assert resp.status_code == 409
    assert not resume.called
    assert _get_action(engine, action_id).status == "executed"


def test_dashboard_reject_guard_allows_pending_row(engine):
    client, _get_session = _dashboard_client(engine)
    action_id = _seed_action(engine, status="pending")
    resume = AsyncMock(return_value=True)
    with (
        patch("infra_brain.api.routers.governance_ops.get_session", _get_session),
        patch("infra_brain.api.routers.governance_ops.resume_remediation_action", resume),
    ):
        resp = client.post(f"/api/dashboard/actions/{action_id}/reject")
    assert resp.status_code == 200
    assert _get_action(engine, action_id).status == "rejected"


# --------------------------------------------------------------------------- #
# MEDIUM-2: resume_remediation_action bounds the wait on a slow graph          #
# --------------------------------------------------------------------------- #


async def test_resume_times_out_on_slow_graph_and_returns_promptly(
    patched_sessions, flag_on, monkeypatch
):
    """A graph stuck resuming (e.g. a slow node) must not hang the caller
    indefinitely — resume_remediation_action bounds the wait at 10s and
    returns False (non-fatal; the poll backstops it), while the graph itself
    keeps running to completion on the dedicated loop in the background.

    Asserts on ``rg.logger.warning`` directly (patched, not via pytest's
    ``caplog``) — ``infra_brain.main`` calls ``configure_logging()`` at
    module import time with ``force=True``, which can strip whichever root
    handler caplog installed depending on suite run order, making log-level
    assertions order-dependent."""
    engine = patched_sessions
    action_id = _seed_action(engine, status="approved")
    with Session(engine) as s:
        a = s.get(ProposedAction, uuid.UUID(action_id))
        a.thread_id = f"remediation:{action_id}"
        a.graph_version = rg.GRAPH_VERSION
        s.commit()

    async def _slow_resume(thread_id, approved):
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(rg, "_resume", _slow_resume)

    async def _fake_wait_for(aw, timeout):
        assert timeout == 10.0
        raise asyncio.TimeoutError()

    warn = MagicMock()
    with (
        patch("infra_brain.remediation_graph.asyncio.wait_for", _fake_wait_for),
        patch.object(rg.logger, "warning", warn),
    ):
        result = await rg.resume_remediation_action(_snapshot(engine, action_id), approved=True)

    assert result is False
    assert warn.called, "must log a warning on timeout (non-fatal, poll backstops it)"
    assert "exceeded 10s" in warn.call_args[0][0]
