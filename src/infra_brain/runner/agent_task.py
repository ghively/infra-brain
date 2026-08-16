"""Headless runner's LangGraph agent (P6.1, convergence plan Phase 6).

A dedicated ReAct graph for standing tasks — NOT chat/agent.py's shared
dashboard graph. Two reasons this can't just reuse ``build_chat_agent()``:

1. **System prompt.** chat/agent.py's ``SYSTEM_PROMPT`` explicitly tells the
   model "You cannot directly modify infrastructure... Direct all other
   change requests to the engineering team" — reusing it verbatim would
   actively instruct the model it can't do the very actions (file an issue,
   record a governance event) this runner exists to take.

2. **Checkpointer loop affinity.** chat/agent.py's graph is compiled with
   ``get_async_checkpointer()`` — ``AsyncPostgresSaver`` when Postgres is
   configured, backed by an ``AsyncConnectionPool`` bound to the event loop
   it was created on. The scheduler invokes this graph via a fresh
   ``asyncio.run()`` per run (APScheduler's ``BackgroundScheduler`` runs
   jobs on a plain thread pool, not a persistent event loop — see
   ``run_agent_task_sync`` below), so a pool created inside run N's loop
   would be invalid by run N+1's fresh loop. Standing tasks don't need
   cross-run thread history anyway (each run is a one-shot task, not a
   resumable conversation), so this graph always compiles with a plain
   ``MemorySaver`` — no loop affinity, safe to build once and reuse across
   every ``asyncio.run()`` call for the life of the process.
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from infra_brain.callbacks.boundary import guarded_tools_node
from infra_brain.chat.tools import (
    query_collection_runs,
    query_compliance,
    query_drift_events,
    query_drift_trend,
    query_eol_status,
    query_fleet_health,
    query_linux_detail,
    query_resources,
    query_vulnerabilities,
    search_knowledge,
)
from infra_brain.runner.tools import RUNNER_ACTION_TOOLS
from infra_brain.tools.agent_task_runs import complete_agent_task_run, start_agent_task_run

logger = logging.getLogger(__name__)

AGENT_NAME = "headless-runner"

SYSTEM_PROMPT = """You are the Infra Brain headless runner — an unattended agent that performs standing infrastructure-monitoring tasks with no human present.
You have read-only access to query resources, drift events, collection runs, fleet health, Linux host details, drift trends, compliance violations, vulnerabilities/CVEs, EOL status, and the knowledge base.
Your only actions are: file a GitLab issue (runner_create_gitlab_issue) for anything that needs human attention, record a governance event (runner_record_governance_event) to log an audit trail of what you found, record client state (runner_record_client_state), and propose an instinct (runner_propose_instinct) for later human approval.
You NEVER seed inventory, resolve/close drift events, or approve a proposal — those actions are not available to you.
Be concise and factual. When you file an issue, put concrete evidence (host names, dates, counts) in the body, not vague language. If a GitLab issue with the same title already exists, filing again is a safe no-op (idempotent by title) — don't avoid filing out of caution about duplicates.
Always finish your final message by stating plainly what you did, or that you found nothing needing attention."""

READ_TOOLS = [
    query_resources,
    query_drift_events,
    query_collection_runs,
    query_fleet_health,
    query_linux_detail,
    query_drift_trend,
    query_compliance,
    query_vulnerabilities,
    query_eol_status,
    search_knowledge,
]

TOOLS = READ_TOOLS + RUNNER_ACTION_TOOLS

MAX_RECURSION = 25

_graph = None
_graph_lock = asyncio.Lock()


async def _build():
    from infra_brain.llm import get_chat_model

    llm = get_chat_model(role="chat", temperature=0, streaming=False).bind_tools(TOOLS)

    async def agent_node(state: MessagesState) -> dict:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + state["messages"]
        return {"messages": [await llm.ainvoke(messages)]}

    return (
        StateGraph(MessagesState)
        .add_node("agent", agent_node)
        .add_node("tools", guarded_tools_node(TOOLS, agent_name=AGENT_NAME))
        .add_edge(START, "agent")
        .add_conditional_edges(
            "agent",
            lambda s: "tools" if s["messages"][-1].tool_calls else END,
        )
        .add_edge("tools", "agent")
        .compile(checkpointer=MemorySaver())
    )


async def get_runner_graph():
    """Build and cache the runner's LangGraph agent (one shared instance per
    process) — see module docstring for why this is a separate graph from
    chat/agent.py's, and why MemorySaver specifically."""
    global _graph
    if _graph is None:
        async with _graph_lock:
            if _graph is None:
                _graph = await _build()
    return _graph


async def _answer(graph, thread_id: str, message: str) -> str:
    from infra_brain.callbacks.registry import build_async_callbacks

    callbacks = build_async_callbacks(agent_name=AGENT_NAME, domain=AGENT_NAME)
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": MAX_RECURSION,
        "callbacks": callbacks,
    }
    result = await graph.ainvoke({"messages": [HumanMessage(content=message)]}, config=config)
    final = result["messages"][-1]
    return getattr(final, "content", None) or str(final)


async def run_agent_task(task_name: str, prompt: str) -> dict:
    """Run one standing task end-to-end.

    Starts an ``AgentTaskRun`` row, drives the runner's agent graph against
    *prompt*, and records the outcome (transcript on success, error on
    failure) on that same row. Never raises — a failure is recorded and
    also returned in the result dict, so a bare scheduler callable never
    needs anything beyond the outer ``try/except Exception: log.exception``
    every other bespoke scheduler job in ``scheduler.py`` already wraps
    itself in.

    Returns:
        ``{"status": "completed"|"failed", "run_id": str, "transcript": str}``
        or ``{"status": "failed", "run_id": str, "error": str}``.
    """
    run = start_agent_task_run(task_name, prompt)
    thread_id = f"agent-task-{run['id']}"
    try:
        graph = await get_runner_graph()
        transcript = await _answer(graph, thread_id, prompt)
        complete_agent_task_run(run["id"], transcript=transcript)
        return {"status": "completed", "run_id": run["id"], "transcript": transcript}
    except Exception as exc:
        logger.exception("[headless-runner] task %r failed", task_name)
        complete_agent_task_run(run["id"], error=str(exc))
        return {"status": "failed", "run_id": run["id"], "error": str(exc)}


def run_agent_task_sync(task_name: str, prompt: str) -> dict:
    """Sync wrapper for scheduler jobs.

    APScheduler's ``BackgroundScheduler`` runs jobs on a plain thread pool,
    not a persistent event loop, so a fresh ``asyncio.run()`` per call is
    the correct pattern here — matching the existing precedent in
    ``tools/snmp.py``'s ``_async_get_system`` wrapper (a one-shot async call
    from a sync context, outside CLAUDE.md constraint #10's sweep-graph-
    specific "never asyncio.run()" carve-out, which is about NOT
    single-shot calls but about concurrent overlapping sweep runs needing a
    SHARED loop — see graph.py's ``run_sweep_sync`` docstring). Safe here
    specifically because ``get_runner_graph()`` uses ``MemorySaver`` (no
    event-loop-bound connection pool) — see module docstring.
    """
    return asyncio.run(run_agent_task(task_name, prompt))


__all__ = [
    "get_runner_graph",
    "run_agent_task",
    "run_agent_task_sync",
    "SYSTEM_PROMPT",
    "TOOLS",
]
