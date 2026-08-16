"""Item 1.1 acceptance tests — F-004.1.

Drives mutating tool calls through a REAL CallbackManager and a REAL
CompiledStateGraph (not handler methods called directly), which is exactly the
path the original audit proved was not blocked.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.callbacks.manager import CallbackManager
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph

from infra_brain.callbacks.boundary import enforce_tool_gate, guarded_tools_node
from infra_brain.callbacks.readonly import ReadOnlyToolValidator


@pytest.fixture
def _enforcing_no_db():
    """Patch settings to enforce and stub the DB session in both validator modules."""
    with ExitStack() as stack:
        for mod in ("readonly", "dlp"):
            ms = stack.enter_context(patch(f"infra_brain.callbacks.{mod}.get_settings"))
            ms.return_value.scan_readonly_enforce = True
            ms.return_value.dlp_fail_closed = True
            sess = stack.enter_context(patch(f"infra_brain.callbacks.{mod}.get_session"))
            sess.return_value.__enter__ = lambda s: MagicMock()
            sess.return_value.__exit__ = MagicMock(return_value=False)
        yield


def test_delete_blocked_through_callback_manager(_enforcing_no_db):
    """The literal F-004.1 repro: DELETE through a real CallbackManager.

    Fails on current code (raise_error defaults to False, the manager swallows
    the PermissionError and returns normally). Passes once raise_error=True.
    """
    cm = CallbackManager(handlers=[ReadOnlyToolValidator(agent_name="probe")])
    with pytest.raises(PermissionError, match="read-only"):
        cm.on_tool_start({"name": "sql_tool"}, '{"query": "DELETE FROM resources"}')


def test_mutating_tool_blocked_through_compiled_graph(_enforcing_no_db):
    """A DELETE through a real CompiledStateGraph is refused: the tool body never
    runs and the ToolMessage says BLOCKED. Fails today (module doesn't exist)."""
    executed: dict = {}

    @tool
    def sql_query(query: str) -> str:
        """Run a SQL query."""
        executed["ran"] = True
        return "rows"

    graph = (
        StateGraph(MessagesState)
        .add_node("tools", guarded_tools_node([sql_query], agent_name="test"))
        .add_edge(START, "tools")
        .add_edge("tools", END)
        .compile()
    )
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "sql_query", "args": {"query": "DELETE FROM resources"}, "id": "c1"}],
    )
    out = graph.invoke({"messages": [ai]})
    assert "ran" not in executed, "mutating tool body executed — gate failed"
    assert "BLOCKED by read-only boundary gate" in out["messages"][-1].content


def test_select_allowed_through_compiled_graph(_enforcing_no_db):
    """A SELECT through the same graph executes normally (no over-blocking)."""
    executed: dict = {}

    @tool
    def sql_query(query: str) -> str:
        """Run a SQL query."""
        executed["ran"] = True
        return "rows"

    graph = (
        StateGraph(MessagesState)
        .add_node("tools", guarded_tools_node([sql_query], agent_name="test"))
        .add_edge(START, "tools")
        .add_edge("tools", END)
        .compile()
    )
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "sql_query", "args": {"query": "SELECT 1"}, "id": "c1"}],
    )
    out = graph.invoke({"messages": [ai]})
    assert executed.get("ran") is True
    assert out["messages"][-1].content == "rows"


def test_enforce_tool_gate_blocks_mutating_http(_enforcing_no_db):
    """POST to a non-whitelisted URL raises before any execution."""
    with pytest.raises(PermissionError, match="read-only"):
        enforce_tool_gate(
            "http_tool",
            {"method": "POST", "url": "https://gitlab.example.com/api/v4/projects"},
            agent_name="test",
        )


def test_guarded_tools_node_writes_audit_log_for_chat_tool_call(_enforcing_no_db, monkeypatch):
    """N-1: a chat-lane tool call through guarded_tools_node must produce a real
    AgentActionLog row. Previously ``tool_obj.invoke(args)`` was called with NO
    config kwarg, so LangChain's callback propagation never reached
    AuditCallbackHandler and no row was ever written for chat tool calls."""
    from infra_brain.callbacks.audit import AuditCallbackHandler
    from infra_brain.db.models import AgentActionLog

    added: list = []

    class _FakeSession:
        def add(self, row):
            added.append(row)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("infra_brain.callbacks.audit.get_session", lambda: _FakeSession())

    @tool
    def sql_query(query: str) -> str:
        """Run a SQL query."""
        return "rows"

    audit_handler = AuditCallbackHandler(agent_name="chat", domain="chat")
    graph = (
        StateGraph(MessagesState)
        .add_node("tools", guarded_tools_node([sql_query], agent_name="chat"))
        .add_edge(START, "tools")
        .add_edge("tools", END)
        .compile()
    )
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "sql_query", "args": {"query": "SELECT 1"}, "id": "c1"}],
    )
    out = graph.invoke({"messages": [ai]}, config={"callbacks": [audit_handler]})
    assert out["messages"][-1].content == "rows"
    action_logs = [r for r in added if isinstance(r, AgentActionLog)]
    assert action_logs, "AgentActionLog row was not written for the chat tool call"
    assert action_logs[0].tool == "sql_query"
    assert action_logs[0].verdict == "allow"


def test_guarded_tools_node_survives_non_permission_error(_enforcing_no_db):
    """N-4: a non-PermissionError tool failure must not crash the whole turn —
    it becomes a graceful ToolMessage, not a propagated exception that would
    kill the chat SSE stream mid-response."""

    @tool
    def flaky_tool(query: str) -> str:
        """A tool that raises a generic error."""
        raise ValueError("boom")

    graph = (
        StateGraph(MessagesState)
        .add_node("tools", guarded_tools_node([flaky_tool], agent_name="test"))
        .add_edge(START, "tools")
        .add_edge("tools", END)
        .compile()
    )
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "flaky_tool", "args": {"query": "x"}, "id": "c1"}],
    )
    out = graph.invoke({"messages": [ai]})  # must not raise
    assert "failed" in out["messages"][-1].content
    assert "boom" in out["messages"][-1].content


def test_guarded_tools_node_still_blocks_permission_denials_distinctly(_enforcing_no_db):
    """N-4 must not blur the PermissionError denial path (F-004.1) with the new
    generic-exception path — a read-only/DLP denial still produces the
    'BLOCKED by read-only boundary gate' message, not a generic failure one."""

    @tool
    def sql_query(query: str) -> str:
        """Run a SQL query."""
        return "rows"  # never reached — gate denies before invoke

    graph = (
        StateGraph(MessagesState)
        .add_node("tools", guarded_tools_node([sql_query], agent_name="test"))
        .add_edge(START, "tools")
        .add_edge("tools", END)
        .compile()
    )
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "sql_query", "args": {"query": "DELETE FROM resources"}, "id": "c1"}],
    )
    out = graph.invoke({"messages": [ai]})
    assert "BLOCKED by read-only boundary gate" in out["messages"][-1].content
    assert "failed" not in out["messages"][-1].content


# ---------------------------------------------------------------------------
# T-runner-async-tools: async-only tools (``@tool async def``) through the gate.
#
# guarded_tools_node used to be a plain sync closure, so even on a graph driven
# with ainvoke()/astream_events() LangGraph ran it via run_in_executor and it
# called the SYNC ``tool_obj.invoke(...)``. An async-only StructuredTool has no
# sync ``_run``, so that raised
# ``NotImplementedError: StructuredTool does not support sync invocation.``,
# which the node's generic ``except Exception`` swallowed into a
# ``"tool 'X' failed: ..."`` ToolMessage — silently killing every async tool
# (the headless runner's four RUNNER_ACTION_TOOLS in particular).
# ---------------------------------------------------------------------------


def _async_tools_graph(tool_obj, agent_name="test"):
    return (
        StateGraph(MessagesState)
        .add_node("tools", guarded_tools_node([tool_obj], agent_name=agent_name))
        .add_edge(START, "tools")
        .add_edge("tools", END)
        .compile()
    )


def test_guarded_tools_node_executes_async_only_tool(_enforcing_no_db):
    """An ``@tool async def`` must actually EXECUTE through the async graph path.

    Fails before the fix: the tool body never runs and the ToolMessage reads
    "tool 'async_probe' failed: StructuredTool does not support sync invocation."
    """
    executed: dict = {}

    @tool
    async def async_probe(payload: str) -> str:
        """An async-only tool."""
        executed["ran"] = True
        return f"async-result:{payload}"

    graph = _async_tools_graph(async_probe)
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "async_probe", "args": {"payload": "ok"}, "id": "c1"}],
    )
    out = asyncio.run(graph.ainvoke({"messages": [ai]}))
    content = out["messages"][-1].content
    assert executed.get("ran") is True, "async tool body never executed"
    assert content == "async-result:ok"
    assert "does not support sync invocation" not in content
    assert "failed" not in content


def test_guarded_tools_node_async_path_still_blocks_permission_denials(_enforcing_no_db):
    """The async path must keep the fail-closed read-only gate: a mutating call is
    refused BEFORE the (async) tool body runs, with the same BLOCKED message."""
    executed: dict = {}

    @tool
    async def sql_query(query: str) -> str:
        """Run a SQL query."""
        executed["ran"] = True
        return "rows"

    graph = _async_tools_graph(sql_query)
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "sql_query", "args": {"query": "DELETE FROM resources"}, "id": "c1"}],
    )
    out = asyncio.run(graph.ainvoke({"messages": [ai]}))
    assert "ran" not in executed, "mutating async tool body executed — gate failed"
    assert "BLOCKED by read-only boundary gate" in out["messages"][-1].content
    assert "failed" not in out["messages"][-1].content


def test_guarded_tools_node_async_path_scans_output_for_dlp(_enforcing_no_db):
    """The async path must keep the output DLP scan (scan_tool_output)."""

    @tool
    async def leaky_tool(q: str) -> str:
        """Returns a Luhn-valid PAN."""
        return "card 4111111111111111"

    graph = _async_tools_graph(leaky_tool)
    ai = AIMessage(
        content="", tool_calls=[{"name": "leaky_tool", "args": {"q": "x"}, "id": "c1"}]
    )
    out = asyncio.run(graph.ainvoke({"messages": [ai]}))
    assert "BLOCKED by read-only boundary gate" in out["messages"][-1].content


def test_guarded_tools_node_async_path_writes_audit_log(_enforcing_no_db, monkeypatch):
    """N-1 must hold on the async path too: the callback chain is forwarded into
    the tool call, so AuditCallbackHandler still writes an AgentActionLog row."""
    from infra_brain.callbacks.audit import AuditCallbackHandler
    from infra_brain.db.models import AgentActionLog

    added: list = []

    class _FakeSession:
        def add(self, row):
            added.append(row)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("infra_brain.callbacks.audit.get_session", lambda: _FakeSession())

    @tool
    async def async_probe(payload: str) -> str:
        """An async-only tool."""
        return "ok"

    graph = _async_tools_graph(async_probe, agent_name="chat")
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "async_probe", "args": {"payload": "x"}, "id": "c1"}],
    )
    audit_handler = AuditCallbackHandler(agent_name="chat", domain="chat")
    out = asyncio.run(
        graph.ainvoke({"messages": [ai]}, config={"callbacks": [audit_handler]})
    )
    assert out["messages"][-1].content == "ok"
    action_logs = [r for r in added if isinstance(r, AgentActionLog)]
    assert action_logs, "AgentActionLog row was not written for the async tool call"
    assert action_logs[0].tool == "async_probe"
    assert action_logs[0].verdict == "allow"


def test_guarded_tools_node_sync_path_unchanged_for_sync_tools(_enforcing_no_db):
    """Regression guard for the other callers: the SYNC graph path must keep
    working exactly as before for ordinary sync tools."""
    executed: dict = {}

    @tool
    def sync_probe(payload: str) -> str:
        """A sync tool."""
        executed["ran"] = True
        return f"sync-result:{payload}"

    graph = _async_tools_graph(sync_probe)
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "sync_probe", "args": {"payload": "ok"}, "id": "c1"}],
    )
    out = graph.invoke({"messages": [ai]})
    assert executed.get("ran") is True
    assert out["messages"][-1].content == "sync-result:ok"


def test_guarded_tools_node_async_path_handles_unknown_tool(_enforcing_no_db):
    """Unknown-tool handling is identical on both paths."""

    @tool
    async def async_probe(payload: str) -> str:
        """An async-only tool."""
        return "ok"

    graph = _async_tools_graph(async_probe)
    ai = AIMessage(
        content="", tool_calls=[{"name": "nope", "args": {}, "id": "c1"}]
    )
    out = asyncio.run(graph.ainvoke({"messages": [ai]}))
    assert out["messages"][-1].content == "unknown tool: nope"


def test_llm_agent_run_tool_gated(_enforcing_no_db):
    """LLMAgent._run_tool refuses a mutating call before the tool body runs.

    Fails today: _run_tool invokes with callbacks only, and the CallbackManager
    swallows the validator's raise.
    """
    from infra_brain.agents.llm_base import LLMAgent

    executed: dict = {}

    @tool
    def sql_query(query: str) -> str:
        """Run a SQL query."""
        executed["ran"] = True
        return "rows"

    class Probe(LLMAgent):
        domain = "probe"

    agent = Probe()
    with pytest.raises(PermissionError, match="read-only"):
        agent._run_tool(sql_query, {"query": "DROP TABLE resources"})
    assert "ran" not in executed
