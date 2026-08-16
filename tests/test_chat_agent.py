"""Tests for the UI-independent chat agent (infra_brain.chat)."""

import asyncio
import json
from unittest.mock import patch


def test_query_resources_formats_rows():
    rows = [{"hostname": "srv01", "domain": "linux", "resource_type": "host"}]
    with patch("infra_brain.chat.tools._rows", return_value=rows):
        from infra_brain.chat.tools import query_resources

        result = query_resources.invoke({"domain": "linux", "limit": 10})
    assert "srv01" in result
    assert "linux" in result


def test_query_drift_events_empty():
    with patch("infra_brain.chat.tools._rows", return_value=[]):
        from infra_brain.chat.tools import query_drift_events

        result = query_drift_events.invoke({"hours": 24, "status": "open"})
    assert "no drift" in result.lower()


def test_query_collection_runs_formats_rows():
    rows = [{"domain": "linux", "status": "success", "records_collected": 42}]
    with patch("infra_brain.chat.tools._rows", return_value=rows):
        from infra_brain.chat.tools import query_collection_runs

        result = query_collection_runs.invoke({"domain": "linux", "limit": 5})
    assert "linux" in result
    assert "42" in result


def test_query_fleet_health_summary():
    rows = [
        {"open_drift": 3, "open_cves": 1, "eol_resources": 2, "last_successful_run": "2026-06-21"}
    ]
    with patch("infra_brain.chat.tools._rows", return_value=rows):
        from infra_brain.chat.tools import query_fleet_health

        result = query_fleet_health.invoke({})
    assert "Open drift events: 3" in result
    assert "Open CVEs: 1" in result


def test_build_chat_agent_is_singleton():
    """build_chat_agent caches one compiled graph per process."""
    import infra_brain.chat.agent as agent_mod

    sentinel = object()
    call_count = 0

    async def fake_build():
        nonlocal call_count
        call_count += 1
        return sentinel

    async def run():
        g1 = await agent_mod.build_chat_agent()
        g2 = await agent_mod.build_chat_agent()
        return g1, g2

    with patch.object(agent_mod, "_build", fake_build):
        agent_mod._graph = None  # reset cache for the test
        try:
            g1, g2 = asyncio.run(run())
        finally:
            agent_mod._graph = None
    assert g1 is g2 is sentinel
    assert call_count == 1


def test_chat_module_exports():
    """The chat package exposes the agent entry points used by the dashboard."""
    import infra_brain.chat.agent as agent_mod

    assert callable(agent_mod.build_chat_agent)
    assert callable(agent_mod.stream_response)
    assert len(agent_mod.TOOLS) == 12  # 9 previous (see #81 fix) + query_compliance
    # + query_vulnerabilities + query_eol_status
    tool_names = {t.name for t in agent_mod.TOOLS}
    assert "query_compliance" in tool_names
    assert "query_vulnerabilities" in tool_names
    assert "query_eol_status" in tool_names


def test_stream_response_wires_safety_callbacks():
    """stream_response must attach the safety callback chain to every graph
    invocation -- ReadOnlyToolValidator/DLPCallbackHandler/AsyncAuditCallbackHandler
    fire on tool calls the chat agent makes. Regression test for a bypass where
    the chat LLM was built with no callbacks= at all.

    B2: the chat path runs on the FastAPI event loop, so it wires the async,
    batched AsyncAuditCallbackHandler (via build_async_callbacks()) instead of
    the sync AuditCallbackHandler -- the sync variant stays reserved for
    agents/llm_base.py's sync ReAct loop."""
    from infra_brain.callbacks.audit import AsyncAuditCallbackHandler
    from infra_brain.callbacks.dlp import DLPCallbackHandler
    from infra_brain.callbacks.readonly import ReadOnlyToolValidator
    from infra_brain.chat.agent import stream_response

    captured_config = {}

    class FakeGraph:
        async def astream_events(self, _input, config=None, version="v2"):
            captured_config.update(config or {})
            if False:
                yield None  # pragma: no cover -- makes this an async generator

    async def run():
        async for _ in stream_response(FakeGraph(), "thread-1", "hello"):
            pass

    asyncio.run(run())

    callbacks = captured_config.get("callbacks", [])
    handler_types = {type(cb) for cb in callbacks}
    assert AsyncAuditCallbackHandler in handler_types
    assert ReadOnlyToolValidator in handler_types
    assert DLPCallbackHandler in handler_types


def test_build_real_graph_compiles_and_replies():
    """Regression for F-026: StateGraph.compile() accepts no recursion_limit kwarg,
    so _build() raised TypeError on first use and every /api/dashboard/chat request
    was dead (masked as an [error: ...] token). This test builds the REAL graph —
    no _build mock — with a fake LLM and drives one full turn through it."""
    from langchain_core.language_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    import infra_brain.chat.agent as agent_mod

    class _FakeToolLLM(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    fake_llm = _FakeToolLLM(messages=iter([AIMessage(content="fleet is healthy")]))

    async def run():
        graph = await agent_mod.build_chat_agent()
        return await graph.ainvoke(
            {"messages": [HumanMessage(content="how is the fleet?")]},
            config={
                "configurable": {"thread_id": "t-real"},
                "recursion_limit": 25,
            },
        )

    with patch("infra_brain.llm.get_chat_model", return_value=fake_llm):
        agent_mod._graph = None  # force a real _build()
        try:
            result = asyncio.run(run())
        finally:
            agent_mod._graph = None
    assert result["messages"][-1].content == "fleet is healthy"


# ---------------------------------------------------------------------------
# Typed event stream, provenance, honest failure, conversation bounds
# (dashboard chat page — T2)
# ---------------------------------------------------------------------------


class _EventGraph:
    """Fake graph whose astream_events replays a canned event list."""

    def __init__(self, events):
        self._events = events

    async def astream_events(self, _input, config=None, version="v2"):
        for e in self._events:
            yield e


def _drain(gen):
    async def run():
        return [item async for item in gen]

    return asyncio.run(run())


def _chunk(text):
    from langchain_core.messages import AIMessageChunk

    return AIMessageChunk(content=text)


def test_stream_chat_events_emits_tokens_tools_and_provenance():
    from infra_brain.chat.agent import stream_chat_events

    kg_payload = {
        "identifier": "node_a",
        "found": True,
        "root": {"name": "node_a", "type": "host", "natural_key": "node_a"},
        "nodes": [{"name": "litellm", "type": "service", "source": "homelab"}],
        "edges": [
            {
                "from": "litellm",
                "to": "node_a",
                "edge_type": "RUNS_ON",
                "method": "compose_file",
                "confidence": 0.95,
                "source": "homelab",
                "retired": False,
            }
        ],
        "node_total": 2,
        "edge_total": 1,
        "truncated": False,
    }
    graph = _EventGraph(
        [
            {
                "event": "on_tool_start",
                "name": "query_graph_neighborhood",
                "data": {"input": {"resource_id": "node_a"}},
            },
            {
                "event": "on_tool_end",
                "name": "query_graph_neighborhood",
                "data": {"output": kg_payload},
            },
            {
                "event": "on_chat_model_stream",
                "data": {"chunk": _chunk("litellm runs on node_a.")},
            },
        ]
    )

    events = _drain(stream_chat_events(graph, "t-prov", "what runs on node_a?"))
    kinds = [e["type"] for e in events]
    assert kinds == ["tool", "provenance", "token"]
    assert events[0]["name"] == "query_graph_neighborhood"
    prov = events[1]["provenance"]
    assert prov["found"] is True
    assert prov["root"]["name"] == "node_a"
    assert prov["edges"][0]["edge_type"] == "RUNS_ON"
    assert prov["edges"][0]["method"] == "compose_file"
    assert events[2]["text"] == "litellm runs on node_a."


def test_text_tools_yield_no_provenance_frame():
    """Only query_graph_neighborhood returns structured rows; the text tools
    return a pre-rendered table, so nothing is invented for them."""
    from infra_brain.chat.agent import stream_chat_events

    graph = _EventGraph(
        [
            {"event": "on_tool_start", "name": "query_drift_events", "data": {"input": {}}},
            {"event": "on_tool_end", "name": "query_drift_events", "data": {"output": "a table"}},
        ]
    )
    events = _drain(stream_chat_events(graph, "t-text", "what drifted?"))
    assert [e["type"] for e in events] == ["tool"]


def test_stream_response_is_token_view_of_events():
    """stream_response must stay a pure token view of stream_chat_events — the
    chatops bot (answer_message) depends on its string contract."""
    from infra_brain.chat.agent import stream_response

    graph = _EventGraph(
        [
            {"event": "on_tool_start", "name": "query_fleet_health", "data": {"input": {}}},
            {"event": "on_chat_model_stream", "data": {"chunk": _chunk("all ")}},
            {"event": "on_chat_model_stream", "data": {"chunk": _chunk("green")}},
        ]
    )
    assert _drain(stream_response(graph, "t-tok", "how is the fleet?")) == ["all ", "green"]


def test_graph_provenance_reports_truncation():
    from infra_brain.chat.agent import graph_provenance
    from infra_brain.chat.limits import CHAT_MAX_PROVENANCE_ITEMS

    payload = {
        "found": True,
        "identifier": "big",
        "root": {"name": "big", "type": "host"},
        "nodes": [
            {"name": f"n{i}", "type": "service"} for i in range(CHAT_MAX_PROVENANCE_ITEMS + 5)
        ],
        "edges": [],
        "node_total": CHAT_MAX_PROVENANCE_ITEMS + 5,
        "edge_total": 0,
        "truncated": False,
    }
    prov = graph_provenance(payload)
    assert len(prov["nodes"]) == CHAT_MAX_PROVENANCE_ITEMS
    assert prov["truncated"] is True
    assert prov["node_total"] == CHAT_MAX_PROVENANCE_ITEMS + 5


def test_graph_provenance_not_found_is_explicit():
    from infra_brain.chat.agent import graph_provenance

    prov = graph_provenance({"found": False, "identifier": "nope", "reason": "no such node"})
    assert prov["found"] is False
    assert prov["nodes"] == []
    assert prov["edges"] == []


def test_classify_chat_error_codes():
    from infra_brain.chat.agent import classify_chat_error

    assert classify_chat_error(ConnectionError("Connection refused"))["code"] == "llm_unreachable"
    assert classify_chat_error(TimeoutError("timed out"))["code"] == "llm_timeout"
    assert (
        classify_chat_error(RuntimeError("Error code: 401 - invalid api key"))["code"] == "llm_auth"
    )
    assert (
        classify_chat_error(RuntimeError("Error code: 404 - model_not_found"))["code"]
        == "llm_model_not_found"
    )
    assert classify_chat_error(ValueError("something odd"))["code"] == "chat_failed"


def test_classify_chat_error_never_echoes_exception_text():
    """N-3: the classified payload is built from a fixed copy table plus
    non-secret config facts — never from the exception's own text."""
    from infra_brain.chat.agent import classify_chat_error

    detail = classify_chat_error(ConnectionError("super-secret-internal-detail"))
    assert "super-secret-internal-detail" not in json.dumps(detail)
    assert detail["message"]
    assert detail["hints"]


def test_llm_endpoint_facts_strips_url_credentials():
    """A base URL carrying userinfo must never render its credentials into a
    browser-visible error panel."""
    from infra_brain.chat.agent import llm_endpoint_facts

    class _S:
        llm_provider = "openai"
        openai_model_chat = "auto/best-reasoning"
        openai_model = "gpt-4o"
        openai_base_url = "https://user:hunter2@gw.internal:4000/v1"

    with patch("infra_brain.config.get_settings", return_value=_S()):
        facts = llm_endpoint_facts()
    assert facts["endpoint"] == "https://gw.internal:4000/v1"
    assert "hunter2" not in facts["endpoint"]
    assert facts["model"] == "auto/best-reasoning"


def test_count_user_turns_counts_human_messages():
    from langchain_core.messages import AIMessage, HumanMessage

    from infra_brain.chat.agent import count_user_turns

    class _State:
        values = {
            "messages": [
                HumanMessage(content="a"),
                AIMessage(content="b"),
                HumanMessage(content="c"),
            ]
        }

    class _G:
        async def aget_state(self, config):
            return _State()

    assert asyncio.run(count_user_turns(_G(), "t")) == 2


def test_count_user_turns_fails_open():
    """A checkpointer that cannot answer must not block a legitimate question —
    the fail-closed cost layers are the rate limit and daily token budget."""
    from infra_brain.chat.agent import count_user_turns

    class _Boom:
        async def aget_state(self, config):
            raise RuntimeError("no checkpointer")

    assert asyncio.run(count_user_turns(_Boom(), "t")) == 0
    assert asyncio.run(count_user_turns(object(), "t")) == 0
