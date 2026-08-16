"""Remediation interrupt mini-graph (Phase 3 Task 4, spec §2.7).

A 3-node LangGraph ``StateGraph`` per ProposedAction::

    START → draft → wait_approval (interrupt) → execute → END

* ``draft`` stamps ``thread_id``/``graph_version`` onto the ProposedAction row
  (migration 0034's columns) and builds an action summary. It has NO external
  side effects — the row itself was already created idempotently by
  ``RemediationAgent._draft_proposals`` (target+status guard) before the graph
  starts.
* ``wait_approval`` calls ``interrupt({...summary...})`` and parks the thread on
  the durable Postgres checkpointer until a human decision arrives via
  ``Command(resume={"approved": bool})``. On resume LangGraph re-executes this
  node from its start (langgraph 1.2.x semantics) — it is side-effect-free.
* ``execute`` re-reads the ProposedAction from Postgres and proceeds ONLY when
  ``status == "approved"`` — the DB row is the source of truth; a stale
  checkpoint resumed against an unapproved row must NOT execute. Execution is
  ``RemediationAgent._execute_one`` — the same MR path as the daily poll,
  still behind ``INFRA_BRAIN_MR_ENABLED`` + the write gate, untouched. No DB
  session is held open across that call (M-8) — see ``_execute_sync``.

Durability is LangGraph's DEFAULT (checkpoint per step) — NOT ``"exit"`` like
plain sweeps: an interrupt is only resumable if the pre-interrupt checkpoint
was persisted. ``require_postgres_checkpointer`` hard-fails a MemorySaver.

Event-loop discipline: the async Postgres checkpointer singleton
(``checkpointer.get_async_checkpointer``) binds its connection pool to the
FIRST event loop that awaits it. Sync callers (RemediationAgent drafting under
APScheduler) and async callers (FastAPI approve/reject handlers) must
therefore all drive this graph from ONE loop — Phase 2's dedicated-loop
thread (``infra_brain.graph._get_sync_loop``). Sync entry points block on
``run_coroutine_threadsafe(...).result()``; the async
``resume_remediation_action`` helper awaits the cross-thread future via
``asyncio.wrap_future`` so the app loop is never blocked (CLAUDE.md #2/#3).

Fallback guarantee (spec §2.7 — "no approval is ever stranded"): the
``_execute_approved()`` daily poll stays registered in BOTH flag modes.
Resume failures are logged and NON-fatal — the DB row is already flipped, so
the poll executes it later. Threads whose ``graph_version`` differs from
``GRAPH_VERSION`` are never resumed; the poll handles those too.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from infra_brain.checkpointer import get_async_checkpointer
from infra_brain.config import get_settings
from infra_brain.db.models import ProposedAction
from infra_brain.db.session import get_session
from infra_brain.graph import _get_sync_loop, require_postgres_checkpointer

logger = logging.getLogger(__name__)

GRAPH_VERSION = "remediation-v1"


class RemediationState(TypedDict, total=False):
    """Metadata only — the ProposedAction row in Postgres holds the payload."""

    action_id: str
    summary: dict
    approved: bool
    executed: bool


def thread_id_for(action_id: Any) -> str:
    return f"remediation:{action_id}"


# --------------------------------------------------------------------------- #
# Nodes                                                                        #
# --------------------------------------------------------------------------- #


def _stamp_and_summarize(action_id: str) -> dict:
    """Stamp thread_id/graph_version on the row; return the interrupt summary."""
    with get_session() as session:
        action = session.get(ProposedAction, uuid.UUID(action_id))
        if action is None:
            raise RuntimeError(
                f"ProposedAction {action_id} not found — cannot run remediation graph"
            )
        action.thread_id = thread_id_for(action_id)
        action.graph_version = GRAPH_VERSION
        payload = action.payload or {}
        summary = {
            "action_id": action_id,
            "action_type": action.action_type,
            "target": action.target,
            "host": payload.get("host"),
            "field": payload.get("field"),
            "confidence": action.confidence,
            "status": action.status,
        }
        session.commit()
    return summary


async def _draft(state: RemediationState) -> dict:
    summary = await asyncio.to_thread(_stamp_and_summarize, state["action_id"])
    return {"summary": summary}


def _wait_approval(state: RemediationState) -> dict:
    """Park on the checkpointer until Command(resume={"approved": bool})."""
    decision = interrupt(state.get("summary") or {"action_id": state.get("action_id")})
    approved = bool(decision.get("approved", False)) if isinstance(decision, dict) else False
    return {"approved": approved}


def _make_agent():
    """Factory hook — tests replace this to inject a stub RemediationAgent."""
    from infra_brain.agents.remediation import RemediationAgent

    return RemediationAgent()


def _execute_sync(action_id: str, approved: bool) -> bool:
    """Execute the approved action IF (and only if) the DB row agrees.

    Pre-execute re-check: the ProposedAction row in Postgres is the source of
    truth. A resume carrying approved=True against a row that is no longer
    ``status == "approved"`` (stale checkpoint, concurrent rejection, already
    executed by the poll) must NOT execute.

    M-8: ``RemediationAgent._execute_one`` can sleep up to 90s (30s + 60s
    backoff) across retries inside ``_create_mr_with_retry`` — this session
    must be closed BEFORE that call, never held open across it (same hazard,
    and same fix, as ``RemediationAgent._execute_approved``'s own read/
    execute/persist split). A fresh, short-lived session is opened only to
    persist the outcome afterward.
    """
    if not approved:
        logger.info("remediation graph %s: decision was rejection — not executing", action_id)
        return False
    with get_session() as session:
        action = session.get(ProposedAction, uuid.UUID(action_id))
        if action is None:
            logger.warning(
                "remediation graph %s: ProposedAction row vanished — not executing", action_id
            )
            return False
        if action.status != "approved":
            logger.warning(
                "remediation graph %s: DB status is %r, not 'approved' — refusing to "
                "execute (DB is source of truth; checkpoint may be stale)",
                action_id,
                action.status,
            )
            return False
        action_type = action.action_type
        payload = dict(action.payload or {})

    agent = _make_agent()
    new_status, result_url, error = agent._execute_one(uuid.UUID(action_id), action_type, payload)
    if new_status is None:
        if error:
            logger.warning("remediation graph %s: execution failed: %s", action_id, error)
        return False

    with get_session() as session:
        row = session.get(ProposedAction, uuid.UUID(action_id))
        if row is not None:
            row.status = new_status
            if result_url is not None:
                row.result_url = result_url
        session.commit()
    return True


async def _execute(state: RemediationState) -> dict:
    executed = await asyncio.to_thread(
        _execute_sync, state["action_id"], bool(state.get("approved", False))
    )
    return {"executed": executed}


# --------------------------------------------------------------------------- #
# Graph assembly                                                               #
# --------------------------------------------------------------------------- #


def _build() -> StateGraph:
    graph = StateGraph(RemediationState)
    graph.add_node("draft", _draft)
    graph.add_node("wait_approval", _wait_approval)
    graph.add_node("execute", _execute)
    graph.add_edge(START, "draft")
    graph.add_edge("draft", "wait_approval")
    graph.add_edge("wait_approval", "execute")
    graph.add_edge("execute", END)
    return graph


def build_remediation_graph(checkpointer: Any) -> CompiledStateGraph:
    """Compile the mini-graph. Hard-fails on a non-durable checkpointer:
    an interrupt parked on a MemorySaver silently loses its pending approval
    on restart (spec §2.5 — first real consumer of the Phase 2 guard)."""
    require_postgres_checkpointer(checkpointer)
    return _build().compile(checkpointer=checkpointer)


_GRAPH: CompiledStateGraph | None = None
_GRAPH_LOCK = asyncio.Lock()


async def _get_graph() -> CompiledStateGraph:
    global _GRAPH
    if _GRAPH is None:
        async with _GRAPH_LOCK:
            if _GRAPH is None:
                _GRAPH = build_remediation_graph(await get_async_checkpointer())
    return _GRAPH


# --------------------------------------------------------------------------- #
# Entry points                                                                 #
# --------------------------------------------------------------------------- #


def _stamp_interrupt_id(action_id: str, interrupt_id: str) -> None:
    """Best-effort: record which interrupt() call produced this pending approval."""
    try:
        with get_session() as session:
            action = session.get(ProposedAction, uuid.UUID(action_id))
            if action is not None:
                action.interrupt_id = interrupt_id[:128]
                session.commit()
    except Exception:  # noqa: BLE001
        logger.debug("interrupt_id stamp failed for %s", action_id, exc_info=True)


async def _start(action_id: str) -> dict:
    """Run the graph until it parks on the wait_approval interrupt."""
    graph = await _get_graph()
    config = {"configurable": {"thread_id": thread_id_for(action_id)}}
    result = await graph.ainvoke({"action_id": action_id}, config)
    interrupts = result.get("__interrupt__") or []
    if interrupts:
        iid = getattr(interrupts[0], "id", None)
        if iid:
            await asyncio.to_thread(_stamp_interrupt_id, action_id, str(iid))
    return result


async def _resume(thread_id: str, approved: bool) -> dict:
    graph = await _get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    return await graph.ainvoke(Command(resume={"approved": bool(approved)}), config)


def start_remediation_action_sync(action_id: Any) -> dict:
    """Start (and park) one action's graph from a sync caller — drafting under
    APScheduler / RemediationAgent.collect(). Blocks the calling worker thread,
    never an event loop: the coroutine runs on Phase 2's dedicated-loop thread
    so the async checkpointer pool only ever sees ONE loop."""
    future = asyncio.run_coroutine_threadsafe(_start(str(action_id)), _get_sync_loop())
    return future.result()


def _resume_thread_id(action: Any) -> str | None:
    """Shared preflight for both resume entry points: returns the thread id to
    resume, or None when this action must NOT be resumed (flag off, or a
    foreign/older graph_version). Callers treat None as "leave it to the
    ``_execute_approved()`` poll"."""
    if not get_settings().remediation_interrupt_enabled:
        return None
    action_id = getattr(action, "id", None)
    graph_version = getattr(action, "graph_version", None)
    if graph_version != GRAPH_VERSION:
        logger.info(
            "action %s has graph_version=%r != %r — not resuming; the "
            "_execute_approved poll will handle it",
            action_id,
            graph_version,
            GRAPH_VERSION,
        )
        return None
    return getattr(action, "thread_id", None) or thread_id_for(action_id)


def resume_remediation_action_sync(action: Any, approved: bool) -> bool:
    """Sync sibling of :func:`resume_remediation_action`, for sync callers.

    The MCP tools (``approve_proposal`` / ``reject_proposal``) are plain
    ``def`` functions with NO event loop of their own to await on, so they
    cannot call the async helper. This blocks the calling worker thread on the
    same cross-thread future the async version awaits — the coroutine still
    runs on Phase 2's ONE dedicated graph loop, so the async checkpointer pool
    never sees a second loop (same discipline as
    ``start_remediation_action_sync``).

    Never call this from inside an event loop — it would block that loop for up
    to 10s. That is enforced MECHANICALLY below, not just documented: calling
    it from a running loop raises RuntimeError immediately. The check sits
    ABOVE the broad ``except Exception`` so the programming error surfaces
    instead of being swallowed as a benign "resume failed, poll will retry".

    Identical semantics otherwise: NON-fatal, bounded at 10s, returns True only
    when the graph was actually resumed; every other outcome returns False and
    is backstopped by the ``_execute_approved()`` poll.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no running loop — the correct (sync) calling context
    else:
        raise RuntimeError(
            "resume_remediation_action_sync must not be called from within an "
            "event loop — use the async resume_remediation_action instead"
        )

    action_id = getattr(action, "id", None)
    try:
        thread_id = _resume_thread_id(action)
        if thread_id is None:
            return False
        future = asyncio.run_coroutine_threadsafe(_resume(thread_id, approved), _get_sync_loop())
        try:
            future.result(timeout=10.0)
        except FuturesTimeoutError:
            logger.warning(
                "remediation graph resume for action %s exceeded 10s — NOT fatal: the "
                "graph continues running to completion on the dedicated loop in the "
                "background, the DB row is already updated, and the _execute_approved "
                "poll backstops it if the graph never finishes",
                action_id,
            )
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "remediation graph resume failed for action %s — NON-fatal: the DB row "
            "is already updated and the _execute_approved poll will execute it",
            action_id,
        )
        return False


async def resume_remediation_action(action: Any, approved: bool) -> bool:
    """Resume one action's parked graph after a human decision. NON-fatal.

    *action* needs ``id``/``thread_id``/``graph_version`` attributes (a
    detached snapshot is fine). Returns True only when the graph was actually
    resumed. Every other outcome — flag off, foreign/older graph_version, or a
    resume error — returns False and is left to the ``_execute_approved()``
    poll fallback: the DB row was already flipped by the caller, so no
    approval is ever stranded (spec §2.7).

    The graph coroutine is scheduled onto the dedicated graph loop and awaited
    via ``asyncio.wrap_future`` — the caller's loop (FastAPI app loop) never
    blocks, and the checkpointer pool never sees a second loop. The await is
    bounded by a 10s ``asyncio.wait_for``: a slow/stuck resume must not hang
    the request indefinitely. On timeout the graph keeps running to
    completion on the dedicated loop in the background — only the caller's
    wait gives up — and this is treated the same as any other non-fatal
    resume failure (the poll backstops it).
    """
    action_id = getattr(action, "id", None)
    try:
        thread_id = _resume_thread_id(action)
        if thread_id is None:
            return False
        future = asyncio.run_coroutine_threadsafe(_resume(thread_id, approved), _get_sync_loop())
        try:
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(
                "remediation graph resume for action %s exceeded 10s — NOT fatal: the "
                "graph continues running to completion on the dedicated loop in the "
                "background, the DB row is already updated, and the _execute_approved "
                "poll backstops it if the graph never finishes",
                action_id,
            )
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "remediation graph resume failed for action %s — NON-fatal: the DB row "
            "is already updated and the _execute_approved poll will execute it",
            action_id,
        )
        return False
