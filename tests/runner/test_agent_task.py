"""Tests for runner/agent_task.py (P6.1).

Mirrors test_chat_agent.py's conventions (GenericFakeChatModel, patching
infra_brain.llm.get_chat_model) but for the runner's own dedicated graph.
tools/agent_task_runs.py's own functions are mocked here (already thoroughly
tested in tests/tools/test_agent_task_runs.py) so these tests focus on the
graph/task-orchestration behavior in isolation.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph

import infra_brain.runner.agent_task as runner_agent
from infra_brain.callbacks.boundary import guarded_tools_node
from infra_brain.runner.tools import RUNNER_ACTION_TOOLS


class _FakeToolLLM(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_get_runner_graph_is_singleton():
    fake_llm = _FakeToolLLM(messages=iter([AIMessage(content="ok")]))
    with patch("infra_brain.llm.get_chat_model", return_value=fake_llm):
        runner_agent._graph = None
        try:
            g1 = asyncio.run(runner_agent.get_runner_graph())
            g2 = asyncio.run(runner_agent.get_runner_graph())
        finally:
            runner_agent._graph = None
    assert g1 is g2


def test_build_real_graph_compiles_and_replies():
    fake_llm = _FakeToolLLM(messages=iter([AIMessage(content="no drift found")]))

    async def run():
        graph = await runner_agent.get_runner_graph()
        return await graph.ainvoke(
            {"messages": [HumanMessage(content="check for drift")]},
            config={"configurable": {"thread_id": "t-1"}, "recursion_limit": 25},
        )

    with patch("infra_brain.llm.get_chat_model", return_value=fake_llm):
        runner_agent._graph = None
        try:
            result = asyncio.run(run())
        finally:
            runner_agent._graph = None
    assert result["messages"][-1].content == "no drift found"


def test_run_agent_task_records_completed_run(monkeypatch):
    fake_llm = _FakeToolLLM(messages=iter([AIMessage(content="all clear")]))

    monkeypatch.setattr(
        runner_agent, "start_agent_task_run",
        lambda task_name, prompt: {"id": "run-1", "task_name": task_name, "prompt": prompt},
    )
    completed = {}
    monkeypatch.setattr(
        runner_agent, "complete_agent_task_run",
        lambda run_id, **kwargs: completed.update(id=run_id, **kwargs) or {},
    )

    with patch("infra_brain.llm.get_chat_model", return_value=fake_llm):
        runner_agent._graph = None
        try:
            result = asyncio.run(runner_agent.run_agent_task("drift-triage", "check for drift"))
        finally:
            runner_agent._graph = None

    assert result["status"] == "completed"
    assert result["run_id"] == "run-1"
    assert result["transcript"] == "all clear"
    assert completed["transcript"] == "all clear"
    assert "error" not in completed or completed.get("error") is None


def test_run_agent_task_records_failed_run_on_exception(monkeypatch):
    monkeypatch.setattr(
        runner_agent, "start_agent_task_run",
        lambda task_name, prompt: {"id": "run-2", "task_name": task_name, "prompt": prompt},
    )
    completed = {}
    monkeypatch.setattr(
        runner_agent, "complete_agent_task_run",
        lambda run_id, **kwargs: completed.update(id=run_id, **kwargs) or {},
    )

    async def _boom():
        raise RuntimeError("LLM unreachable")

    monkeypatch.setattr(runner_agent, "get_runner_graph", _boom)

    result = asyncio.run(runner_agent.run_agent_task("drift-triage", "check for drift"))

    assert result["status"] == "failed"
    assert result["run_id"] == "run-2"
    assert "LLM unreachable" in result["error"]
    assert "LLM unreachable" in completed["error"]


# ---------------------------------------------------------------------------
# T-runner-async-tools: the four RUNNER_ACTION_TOOLS must actually EXECUTE.
#
# All four are declared ``@tool async def`` — async-only StructuredTools with no
# sync ``_run``. guarded_tools_node used to be a plain sync closure that called
# ``tool_obj.invoke(...)``, which raises
# ``NotImplementedError: StructuredTool does not support sync invocation.`` for
# such a tool. The node's generic ``except Exception`` swallowed it into a
# ``"tool 'X' failed: ..."`` ToolMessage, so the runner's ENTIRE write capability
# (file a GitLab issue / record a governance event / record client state /
# propose an instinct) was dead in production while ``run_agent_task`` still
# recorded the AgentTaskRun as status="completed". These tests drive a real
# tool_call through the real graph wiring and would have caught that.
# ---------------------------------------------------------------------------

_ACTION_TOOL_CALLS = {
    "runner_create_gitlab_issue": {
        "project_id": "42",
        "title": "Drift detected on web-01",
        "description": "3 unresolved drift events since 2026-08-01",
    },
    "runner_record_governance_event": {
        "event_type": "drift-triage-run",
        "payload": {"events": 3},
    },
    "runner_record_client_state": {
        "collection": "drift-triage",
        "entry_id": "2026-08-04",
        "payload": {"events": 3},
    },
    "runner_propose_instinct": {
        "zone": "dmz",
        "domain": "linux",
        "pattern": "web-01 drifts after every apt upgrade",
        "confidence": 0.8,
    },
}


def test_runner_action_tool_call_executes_through_the_real_graph():
    """A RUNNER_ACTION_TOOLS tool_call driven through the runner's own graph must
    reach ``call_mcp_tool`` and return its result — not a swallowed
    "tool 'runner_create_gitlab_issue' failed: ... sync invocation" ToolMessage."""
    tool_call = {
        "name": "runner_create_gitlab_issue",
        "args": _ACTION_TOOL_CALLS["runner_create_gitlab_issue"],
        "id": "call-1",
    }
    fake_llm = _FakeToolLLM(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[tool_call]),
                AIMessage(content="filed issue 7"),
            ]
        )
    )
    mcp = AsyncMock(return_value={"issue_iid": 7, "web_url": "https://gitlab/x/issues/7"})

    async def run():
        graph = await runner_agent.get_runner_graph()
        return await graph.ainvoke(
            {"messages": [HumanMessage(content="triage drift")]},
            config={"configurable": {"thread_id": "t-action"}, "recursion_limit": 25},
        )

    with (
        patch("infra_brain.llm.get_chat_model", return_value=fake_llm),
        patch("infra_brain.runner.tools.call_mcp_tool", mcp),
    ):
        runner_agent._graph = None
        try:
            result = asyncio.run(run())
        finally:
            runner_agent._graph = None

    mcp.assert_awaited_once()
    assert mcp.await_args.args[0] == "create_gitlab_issue"
    assert mcp.await_args.kwargs["title"] == "Drift detected on web-01"

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1, "the action tool never produced a ToolMessage"
    content = tool_messages[0].content
    assert "issue_iid" in content, f"action tool did not execute: {content}"
    assert "does not support sync invocation" not in content
    assert "failed" not in content
    assert "BLOCKED" not in content
    assert result["messages"][-1].content == "filed issue 7"


@pytest.mark.parametrize("tool_name", sorted(_ACTION_TOOL_CALLS))
def test_every_runner_action_tool_executes_through_the_guarded_node(tool_name):
    """Each of the four action tools must execute through the production wiring
    (``guarded_tools_node(TOOLS, agent_name=AGENT_NAME)``) on the async path."""
    assert tool_name in {t.name for t in RUNNER_ACTION_TOOLS}

    graph = (
        StateGraph(MessagesState)
        .add_node(
            "tools",
            guarded_tools_node(runner_agent.TOOLS, agent_name=runner_agent.AGENT_NAME),
        )
        .add_edge(START, "tools")
        .add_edge("tools", END)
        .compile()
    )
    ai = AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": _ACTION_TOOL_CALLS[tool_name], "id": "c1"}],
    )
    mcp = AsyncMock(return_value={"ok": True, "recorded": tool_name})
    with patch("infra_brain.runner.tools.call_mcp_tool", mcp):
        out = asyncio.run(graph.ainvoke({"messages": [ai]}))

    mcp.assert_awaited_once()
    content = out["messages"][-1].content
    assert "recorded" in content, f"{tool_name} did not execute: {content}"
    assert "does not support sync invocation" not in content
    assert "BLOCKED" not in content


def test_run_agent_task_sync_wraps_asyncio_run(monkeypatch):
    async def _fake_run_agent_task(task_name, prompt):
        return {"status": "completed", "run_id": "run-3", "transcript": "ok"}

    monkeypatch.setattr(runner_agent, "run_agent_task", _fake_run_agent_task)

    result = runner_agent.run_agent_task_sync("drift-triage", "prompt")

    assert result == {"status": "completed", "run_id": "run-3", "transcript": "ok"}
