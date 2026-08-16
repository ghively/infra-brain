"""LangGraph chat agent for Infra Brain (UI-independent).

Exports:
- ``build_chat_agent()`` — builds and caches the compiled LangGraph agent.
- ``stream_response(graph, thread_id, message)`` — async generator of token strings.

The agent is read-only: it can query resources, drift, collection runs, and fleet
health, but cannot mutate infrastructure. It is consumed by the FastAPI dashboard
(streaming SSE endpoint) and, during the migration window, by the legacy Streamlit
sidebar via a thin re-export shim.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlsplit

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph

from infra_brain.callbacks.boundary import guarded_tools_node
from infra_brain.chat.tools import (
    _truncate_tool_result,
    propose_host_purpose_update,
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
from infra_brain.chat.limits import CHAT_MAX_PROVENANCE_ITEMS, CHAT_MAX_TURNS
from infra_brain.checkpointer import get_async_checkpointer as _build_checkpointer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Checkpointer — shared ASYNC factory: AsyncPostgresSaver (langgraph-checkpoint-
# postgres, .aio submodule) when POSTGRES_URL is a real Postgres DSN, MemorySaver
# fallback otherwise. This graph is driven with astream_events()/ainvoke(), so it
# MUST use the async-capable checkpointer — the sync PostgresSaver does not
# implement aget_tuple/aput/alist and raises NotImplementedError on first use.
# agents/llm_base.py's sync .invoke() ReAct loop keeps using the sync
# get_checkpointer() from infra_brain.checkpointer; do not swap that one.
# See infra_brain/checkpointer.py (Wave 4 item 4.2 — F-026, critical fix follow-up).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 7th tool: knowledge-graph neighborhood query
# ---------------------------------------------------------------------------


@tool
def query_graph_neighborhood(resource_id: str, depth: int = 2) -> dict:
    """Query the knowledge graph around an entity — connected entities and relationship types.

    Reads graph_nodes/graph_edges: RUNS_ON (service -> host), BELONGS_TO /
    DEFINED_IN (IaC file <-> project), RUNS_IMAGE, and identity links, each
    with its confidence and derivation method.

    IMPORTANT: the knowledge graph holds relationships BETWEEN entities, not
    containment. A host's packages, ports, users and crons are detail-table
    facts and never appear as edges — an empty edge list does NOT mean the host
    is idle. The returned `facts` key carries the host's systemd units for that
    reason; use query_linux_detail for packages/ports/users/crons.

    Args:
        resource_id: what to centre on — a host/service/project/file NAME, a
            graph-node UUID, or a resources.id UUID (bridged automatically)
        depth: Maximum hops to traverse (default 2, clamped to 4)
    """
    try:
        from infra_brain.chat.tools import kg_neighborhood_payload

        return _truncate_tool_result(kg_neighborhood_payload(resource_id, depth=depth))
    except Exception as e:
        logger.error("Graph neighborhood query failed: %s", e)
        return {"error": str(e), "resource_id": resource_id}


# ---------------------------------------------------------------------------
# Prompt + tool list
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the Infra Brain assistant — an expert on this organisation's infrastructure.
You have read-only access to the infrastructure database. You can query resources, drift events, collection runs, fleet health, Linux host details, drift trends, compliance violations, vulnerabilities/CVEs, EOL status, and the knowledge graph.
When asked about specific hosts, CVEs, or drift, always use your tools to get current data rather than guessing.
Keep answers concise. Format tabular data as markdown tables when helpful.
There is a knowledge base of internal documentation — runbooks, decision records, architecture docs — searchable with the search_knowledge tool. For any question that turns on documentation, policy, or "how/why is this set up", search it FIRST and ground your answer in the retrieved passages, citing them as [Source N: title]. If search_knowledge returns nothing relevant, say "I don't have documentation on that" rather than inventing details.
You cannot directly modify infrastructure, create tickets, run scans, or merge changes. The one action you can take is to PROPOSE a correction to a host's purpose, VLAN, or subnet using the propose_host_purpose_update tool, which opens a GitLab merge request for a human to review — it does not change any live data. Direct all other change requests to the engineering team."""

# #81: query_resource_neighborhood (chat/tools.py) is deliberately NOT bound
# here — it is the TEXT-TABLE rendering of the same answer query_graph_neighborhood
# returns as structured data, and since graph-first P5 both go through the one
# shared kg_neighborhood_payload(), so they can no longer drift apart. Binding
# both would give the LLM two tools for one capability; the text one stays
# available for direct/API reuse.
TOOLS = [
    query_resources,
    query_drift_events,
    query_collection_runs,
    query_fleet_health,
    query_linux_detail,
    query_drift_trend,
    query_graph_neighborhood,
    propose_host_purpose_update,
    search_knowledge,
    query_compliance,
    query_vulnerabilities,
    query_eol_status,
]

_graph = None
_graph_lock = asyncio.Lock()


async def _build() -> object:
    from infra_brain.llm import get_chat_model

    llm = get_chat_model(role="chat", temperature=0, streaming=True).bind_tools(TOOLS)

    async def agent_node(state: MessagesState) -> dict:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + state["messages"]
        return {"messages": [await llm.ainvoke(messages)]}

    # Building the checkpointer here (await, not a sync call) keeps any Postgres
    # connect + DDL setup() work inside the coroutine, so it never runs as a
    # blocking sync call on the event loop thread (CLAUDE.md constraint #2).
    checkpointer = await _build_checkpointer()

    return (
        StateGraph(MessagesState)
        .add_node("agent", agent_node)
        .add_node("tools", guarded_tools_node(TOOLS, agent_name="chat"))
        .add_edge(START, "agent")
        .add_conditional_edges(
            "agent",
            lambda s: "tools" if s["messages"][-1].tool_calls else END,
        )
        .add_edge("tools", "agent")
        # recursion_limit is a RUNTIME config key (stream_response passes it per
        # invocation) — compile() accepts no such kwarg (F-026: TypeError).
        .compile(checkpointer=checkpointer)
    )


async def build_chat_agent():
    """Build and cache the LangGraph chat agent (one shared instance per process).

    The compiled graph carries the shared ASYNC checkpointer from
    infra_brain.checkpointer.get_async_checkpointer() — AsyncPostgresSaver
    (persistent thread history across restarts) when POSTGRES_URL is a real
    Postgres DSN, MemorySaver otherwise. Cached as a module-level singleton —
    rebuilt only on server restart.

    This is async (and must be awaited) because the first call performs the
    checkpointer's connect + setup() work, which — for AsyncPostgresSaver — is
    itself async; awaiting it here keeps that work off the event loop thread
    instead of blocking it (CLAUDE.md constraint #2).
    """
    global _graph
    if _graph is None:
        async with _graph_lock:
            if _graph is None:
                _graph = await _build()
    return _graph


MAX_RECURSION = 25


# ---------------------------------------------------------------------------
# Provenance extraction
#
# An operator has to be able to tell an answer FROM THE GRAPH apart from an
# answer the model made up. Only one bound tool returns machine-readable
# provenance: ``query_graph_neighborhood``, whose payload (from
# ``chat.tools.kg_neighborhood_payload`` → ``graph_kg.neighborhood``) carries
# the root node, the visited nodes, and the edges WITH each edge's
# ``method``/``confidence``/``source``. Every other bound tool returns a
# pre-rendered fixed-width TEXT table (``chat.tools._fmt``) with no structured
# rows behind it, so for those the honest provenance is the fact of the call
# itself — tool name + arguments — and nothing more. Do not synthesize row-level
# citations for the text tools; there is nothing to synthesize them from.
# ---------------------------------------------------------------------------

#: The one tool whose output is structured enough to cite.
GRAPH_TOOL_NAME = "query_graph_neighborhood"


def _as_payload(output: Any) -> dict | None:
    """Best-effort unwrap of an ``on_tool_end`` output into the tool's own dict.

    LangChain has moved this shape around between versions (raw return value in
    some, a ``ToolMessage`` wrapper in others), so this probes rather than
    assumes, and returns ``None`` — meaning "no provenance available" — rather
    than guessing when it cannot find a dict. A missing provenance frame renders
    as "no graph citation" in the UI, which is the correct thing to show.
    """
    if isinstance(output, dict) and "nodes" in output:
        return output
    content = getattr(output, "content", None)
    if isinstance(content, dict) and "nodes" in content:
        return content
    return None


def graph_provenance(payload: dict) -> dict:
    """Reduce a ``kg_neighborhood_payload`` dict to what the UI cites.

    Truncation is reported, never silent — the same posture ``graph_kg`` itself
    takes: ``node_total``/``edge_total`` are what the walk found, the lists are
    what fits under ``CHAT_MAX_PROVENANCE_ITEMS``.
    """
    if not payload.get("found"):
        return {
            "found": False,
            "identifier": payload.get("identifier"),
            "reason": payload.get("reason", "not found in the knowledge graph"),
            "nodes": [],
            "edges": [],
        }
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []
    root = payload.get("root") or {}
    return {
        "found": True,
        "identifier": payload.get("identifier"),
        "root": {"name": root.get("name"), "type": root.get("type")},
        "nodes": [
            {"name": n.get("name"), "type": n.get("type"), "source": n.get("source")}
            for n in nodes[:CHAT_MAX_PROVENANCE_ITEMS]
        ],
        "edges": [
            {
                "from": e.get("from"),
                "to": e.get("to"),
                "edge_type": e.get("edge_type"),
                "method": e.get("method"),
                "confidence": e.get("confidence"),
                "source": e.get("source"),
            }
            for e in edges[:CHAT_MAX_PROVENANCE_ITEMS]
        ],
        "node_total": payload.get("node_total", len(nodes)),
        "edge_total": payload.get("edge_total", len(edges)),
        "truncated": bool(payload.get("truncated"))
        or len(nodes) > CHAT_MAX_PROVENANCE_ITEMS
        or len(edges) > CHAT_MAX_PROVENANCE_ITEMS,
    }


# ---------------------------------------------------------------------------
# Honest failure
# ---------------------------------------------------------------------------

#: Fixed, non-leaking copy per failure class. The exception's own text is NEVER
#: included (N-3: it can carry internal detail); the real error is logged
#: server-side by the caller.
_ERROR_COPY: dict[str, str] = {
    "llm_unreachable": "Could not reach the language-model endpoint.",
    "llm_auth": "The language-model endpoint rejected our credentials.",
    "llm_model_not_found": "The configured model id was not found at the endpoint.",
    "llm_timeout": "The language-model endpoint did not answer in time.",
    "llm_rate_limited": "The language-model endpoint is rate-limiting us.",
    "conversation_too_long": f"This conversation has reached its {CHAT_MAX_TURNS}-turn limit.",
    "chat_failed": "The assistant could not complete that request.",
}


def llm_endpoint_facts() -> dict[str, str]:
    """Non-secret facts about the configured chat model, for the error panel.

    Returns provider, model id, and endpoint — never an API key, and never the
    userinfo half of a URL (``urlsplit().hostname``/``port`` are used precisely
    so a ``https://user:pass@host`` style base URL cannot leak its credentials
    into a browser-rendered error).
    """
    from infra_brain.config import get_settings

    try:
        s = get_settings()
    except Exception:  # noqa: BLE001 -- diagnostics must never raise
        return {"provider": "unknown", "model": "unknown", "endpoint": "unknown"}

    provider = getattr(s, "llm_provider", "unknown")
    if provider == "openai":
        model = getattr(s, "openai_model_chat", "") or getattr(s, "openai_model", "")
        raw = getattr(s, "openai_base_url", "") or "https://api.openai.com/v1"
    elif provider == "anthropic":
        model = getattr(s, "anthropic_model_chat", "") or getattr(s, "llm_model", "")
        raw = getattr(s, "anthropic_base_url", "") or "https://api.anthropic.com"
    elif provider == "bedrock":
        model = getattr(s, "bedrock_model_chat", "") or getattr(s, "bedrock_model_id", "")
        raw = f"bedrock://{getattr(s, 'bedrock_region', '')}"
    else:
        model, raw = "unknown", ""

    parts = urlsplit(raw) if raw else None
    if parts and parts.hostname:
        endpoint = f"{parts.scheme}://{parts.hostname}"
        if parts.port:
            endpoint += f":{parts.port}"
        if parts.path and parts.path != "/":
            endpoint += parts.path
    else:
        endpoint = raw or "unknown"
    return {"provider": provider, "model": model or "unknown", "endpoint": endpoint}


def _error_code(exc: BaseException) -> str:
    """Classify *exc* into one of ``_ERROR_COPY``'s keys.

    Matches on exception TYPE NAME plus message text rather than importing
    ``openai``/``anthropic``/``botocore`` exception classes — the provider is a
    runtime choice (``LLM_PROVIDER``), so importing any one provider's exception
    tree here would make classification depend on a package that may not be
    installed in the deployment actually running.
    """
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    blob = f"{name} {text}"
    if isinstance(exc, TimeoutError) or "timeout" in blob or "timedout" in blob:
        return "llm_timeout"
    if "ratelimit" in blob or "429" in blob or "too many requests" in blob:
        return "llm_rate_limited"
    if (
        "authentication" in blob
        or "unauthorized" in blob
        or "permissiondenied" in blob
        or "invalid api key" in blob
        or "401" in blob
        or "403" in blob
    ):
        return "llm_auth"
    if "notfound" in blob or "404" in blob or "model_not_found" in blob:
        return "llm_model_not_found"
    if (
        isinstance(exc, (ConnectionError, OSError))
        or "connect" in blob
        or "unreachable" in blob
        or "name or service not known" in blob
        or "getaddrinfo" in blob
        or "refused" in blob
        or "ssl" in blob
    ):
        return "llm_unreachable"
    return "chat_failed"


def classify_chat_error(exc: BaseException) -> dict:
    """Turn *exc* into a structured, renderable error — never a stack trace.

    The dashboard's chat page renders ``message`` as the headline and ``hints``
    as a checklist, so a first-time operator whose model endpoint is simply down
    sees "the model isn't reachable — here's what to check" instead of a blank
    reply or an opaque 500. Deliberately says NOTHING derived from ``exc``'s own
    text (N-3).
    """
    code = _error_code(exc)
    facts = llm_endpoint_facts()
    hints: list[str] = [
        f"Provider: {facts['provider']} · model: {facts['model']} · endpoint: {facts['endpoint']}",
    ]
    if code == "llm_unreachable":
        hints += [
            f"Check the endpoint is up and routable from this container: "
            f"curl -sS {facts['endpoint']}/models",
            "Check LLM_PROVIDER / OPENAI_BASE_URL (or ANTHROPIC_BASE_URL) point at a "
            "gateway that is actually running.",
            "Secrets load from Bitwarden at startup — a restart after a gateway move "
            "may be needed for the new address to take effect.",
        ]
    elif code == "llm_auth":
        hints += [
            "Check the provider API key in Bitwarden, and that the running container "
            "picked it up (secrets are loaded once, at startup).",
        ]
    elif code == "llm_model_not_found":
        hints += [
            f"The endpoint does not publish a model called '{facts['model']}'. "
            f"List what it does publish: curl -sS {facts['endpoint']}/models",
            "Set OPENAI_MODEL (or OPENAI_MODEL_CHAT) to an id the gateway actually serves.",
        ]
    elif code in ("llm_timeout", "llm_rate_limited"):
        hints += ["Retry in a moment; if it persists, check the gateway's own load and quota."]
    else:
        hints += ["The full error was logged server-side — check the API container logs."]
    return {"code": code, "message": _ERROR_COPY[code], "hints": hints, **facts}


# ---------------------------------------------------------------------------
# Conversation bounds
# ---------------------------------------------------------------------------


async def count_user_turns(graph, thread_id: str) -> int:
    """How many user turns this checkpointer thread already holds.

    Returns 0 — i.e. "no reason to refuse" — for any graph or checkpointer that
    cannot answer (``MemorySaver`` on a fresh thread, a test double, a graph
    without ``aget_state``). Failing OPEN here is deliberate: this is a cost
    guard, and the per-user rate limit and daily token budget (TRK-090/091) are
    the fail-closed layers. Refusing a legitimate question because a state read
    hiccuped would be the worse failure.
    """
    getter = getattr(graph, "aget_state", None)
    if getter is None:
        return 0
    try:
        state = await getter({"configurable": {"thread_id": thread_id}})
    except Exception:  # noqa: BLE001
        logger.debug("could not read chat thread state for turn count", exc_info=True)
        return 0
    values = getattr(state, "values", None) or {}
    messages = values.get("messages") or []
    return sum(1 for m in messages if getattr(m, "type", None) == "human")


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def stream_chat_events(
    graph, thread_id: str, message: str, config: dict | None = None
) -> AsyncGenerator[dict, None]:
    """Drive one chat turn and yield typed events.

    Event shapes:
      ``{"type": "tool", "name": str, "args": dict}`` — a tool is about to run.
      ``{"type": "provenance", "tool": str, "provenance": dict}`` — structured
        citations for the one tool that has them (see ``graph_provenance``).
      ``{"type": "token", "text": str}`` — a model token, PAN-redacted.

    This is the single driver for the chat lane. ``stream_response`` below is a
    token-only view of it, so the SSE dashboard route (which wants tool +
    provenance frames) and the chatops bot (which wants one joined string) still
    share one graph, one tool set, and one safety-callback chain — there is no
    second invocation path.
    """
    from infra_brain.callbacks.registry import build_async_callbacks

    config = dict(config) if config else {}
    config.setdefault("configurable", {})
    config["configurable"].setdefault("thread_id", thread_id)
    recursion_limit = config.get("recursion_limit", 15)
    if not isinstance(recursion_limit, int) or not (1 <= recursion_limit <= MAX_RECURSION):
        recursion_limit = 15  # safe default
    # Every tool call must flow through the safety callback chain (ReadOnlyToolValidator
    # -> DLPCallbackHandler -> AuditCallbackHandler) per the project's core read-only
    # guarantee. Attaching them here (graph invocation config) rather than to the LLM
    # constructor propagates them to every node's tool execution, including ToolNode.
    # B2: this runs on the FastAPI event loop, so use the async/batched Audit +
    # Observation handlers instead of the sync per-event-commit ones.
    callbacks = build_async_callbacks(agent_name="chat", domain="chat")
    config = {
        **config,
        "recursion_limit": recursion_limit,
        "callbacks": [*config.get("callbacks", []), *callbacks],
    }
    from infra_brain.callbacks.dlp import redact_pans_preserving_uuids

    async for event in graph.astream_events(
        {"messages": [HumanMessage(content=message)]},
        config=config,
        version="v2",
    ):
        kind = event.get("event")
        if kind == "on_chat_model_stream":
            chunk = event["data"]["chunk"]
            if chunk.content:
                yield {"type": "token", "text": redact_pans_preserving_uuids(chunk.content)}
        elif kind == "on_tool_start":
            yield {
                "type": "tool",
                "name": event.get("name", "?"),
                "args": (event.get("data") or {}).get("input") or {},
            }
        elif kind == "on_tool_end" and event.get("name") == GRAPH_TOOL_NAME:
            payload = _as_payload((event.get("data") or {}).get("output"))
            if payload is not None:
                yield {
                    "type": "provenance",
                    "tool": GRAPH_TOOL_NAME,
                    "provenance": graph_provenance(payload),
                }


async def stream_response(
    graph, thread_id: str, message: str, config: dict | None = None
) -> AsyncGenerator[str, None]:
    """Yield token strings from the chat agent for *message* under *thread_id*.

    Token-only view of :func:`stream_chat_events`; kept as its own name because
    the chatops bot (``answer_message``) and existing callers only ever wanted
    the text.
    """
    async for event in stream_chat_events(graph, thread_id, message, config=config):
        if event["type"] == "token":
            yield event["text"]


async def answer_message(
    graph, thread_id: str, message: str, config: dict | None = None
) -> str:
    """Non-streaming convenience wrapper over ``stream_response`` — joins every
    token into a single reply string.

    Used by request/response integrations that cannot consume an SSE stream
    (e.g. the chatops bot, GitLab #111 — a Slack ``chat.postMessage`` call or
    a Teams outgoing-webhook response body both need one final string, not a
    token stream). Drives the exact same LangGraph agent, tool set, and
    safety-callback chain as the dashboard's streaming ``/chat`` endpoint —
    no separate invocation path.
    """
    parts = [token async for token in stream_response(graph, thread_id, message, config=config)]
    return "".join(parts)
