"""Tests for runner/tools.py (P6.1/P6.2, D10).

runner/mcp_client.py's call_mcp_tool is mocked -- these tests verify each
tool wraps its arguments correctly and that a HeadlessRunnerMcpError DOES
propagate as an exception, rather than being swallowed into a plain
{"error": ...} dict. Per mcp_client.py's own module docstring, this is NOT
fail-open: a standing task's whole point is to take a real action, so a
failure must reach runner/agent_task.py's run_agent_task, which records it
on the AgentTaskRun row as a failed run (see tests/runner/test_agent_task.py
for that half of the contract) instead of the failure silently looking like
a normal, successful ToolMessage to the LLM.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from infra_brain.runner.mcp_client import HeadlessRunnerMcpError
from infra_brain.runner.tools import (
    RUNNER_ACTION_TOOLS,
    runner_create_gitlab_issue,
    runner_propose_instinct,
    runner_record_client_state,
    runner_record_governance_event,
)


def test_runner_action_tools_registry_has_all_four():
    names = {t.name for t in RUNNER_ACTION_TOOLS}
    assert names == {
        "runner_create_gitlab_issue",
        "runner_record_governance_event",
        "runner_record_client_state",
        "runner_propose_instinct",
    }


def test_create_gitlab_issue_success():
    mock_call = AsyncMock(return_value={"issue_iid": 7, "web_url": "https://x/issues/7"})
    with patch("infra_brain.runner.tools.call_mcp_tool", mock_call):
        result = asyncio.run(
            runner_create_gitlab_issue.ainvoke(
                {"project_id": 42, "title": "Drift detected", "description": "details"}
            )
        )
    assert result == {"issue_iid": 7, "web_url": "https://x/issues/7"}
    mock_call.assert_called_once_with(
        "create_gitlab_issue",
        project_id=42, title="Drift detected", description="details", labels=None,
    )


def test_create_gitlab_issue_failure_propagates_not_swallowed():
    mock_call = AsyncMock(side_effect=HeadlessRunnerMcpError("key_lacks_scope"))
    with patch("infra_brain.runner.tools.call_mcp_tool", mock_call):
        with pytest.raises(HeadlessRunnerMcpError, match="key_lacks_scope"):
            asyncio.run(
                runner_create_gitlab_issue.ainvoke({"project_id": 42, "title": "x"})
            )


def test_create_gitlab_issue_args_schema_requires_int_project_id():
    """project_id must be int, matching the underlying MCP tool's strict int
    typing (mcp_server.py's create_gitlab_issue) -- a project *path* string
    (e.g. "group/infra"), which the old docstring wrongly implied was
    accepted, must not validate as a bare passthrough string field."""
    schema_fields = runner_create_gitlab_issue.args_schema.model_fields
    assert schema_fields["project_id"].annotation is int


def test_create_gitlab_issue_rejects_missing_or_zero_project_id():
    """No standing-task prompt ever supplies a project_id, and there is no
    config default -- rather than silently forwarding an unset/zero id into
    the MCP boundary (risking a hallucinated/wrong-project file), the tool
    must fail closed with a clear error and never call the MCP client."""
    mock_call = AsyncMock()
    with patch("infra_brain.runner.tools.call_mcp_tool", mock_call):
        result = asyncio.run(
            runner_create_gitlab_issue.ainvoke({"project_id": 0, "title": "x"})
        )
    assert "error" in result
    mock_call.assert_not_called()


def test_record_governance_event_uses_fixed_client_id():
    mock_call = AsyncMock(return_value={"id": "g1", "hash": "abc"})
    with patch("infra_brain.runner.tools.call_mcp_tool", mock_call):
        result = asyncio.run(
            runner_record_governance_event.ainvoke(
                {"event_type": "drift-triage-run", "payload": {"findings": 2}}
            )
        )
    assert result == {"id": "g1", "hash": "abc"}
    mock_call.assert_called_once_with(
        "record_governance_event",
        client_id="headless-runner", event_type="drift-triage-run", payload={"findings": 2},
    )


def test_record_governance_event_defaults_payload():
    mock_call = AsyncMock(return_value={})
    with patch("infra_brain.runner.tools.call_mcp_tool", mock_call):
        asyncio.run(runner_record_governance_event.ainvoke({"event_type": "x"}))
    assert mock_call.call_args.kwargs["payload"] == {}


def test_record_governance_event_failure_propagates_not_swallowed():
    mock_call = AsyncMock(side_effect=HeadlessRunnerMcpError("server unreachable"))
    with patch("infra_brain.runner.tools.call_mcp_tool", mock_call):
        with pytest.raises(HeadlessRunnerMcpError, match="server unreachable"):
            asyncio.run(runner_record_governance_event.ainvoke({"event_type": "x"}))


def test_record_client_state_uses_fixed_client_id():
    mock_call = AsyncMock(return_value={"id": "cs1"})
    with patch("infra_brain.runner.tools.call_mcp_tool", mock_call):
        result = asyncio.run(
            runner_record_client_state.ainvoke(
                {"collection": "x", "entry_id": "e1", "payload": {}}
            )
        )
    assert result == {"id": "cs1"}
    mock_call.assert_called_once_with(
        "record_client_state", collection="x", entry_id="e1", payload={}, client_id="headless-runner"
    )


def test_record_client_state_failure_propagates_not_swallowed():
    mock_call = AsyncMock(side_effect=HeadlessRunnerMcpError("unscoped collection"))
    with patch("infra_brain.runner.tools.call_mcp_tool", mock_call):
        with pytest.raises(HeadlessRunnerMcpError, match="unscoped collection"):
            asyncio.run(
                runner_record_client_state.ainvoke(
                    {"collection": "nope", "entry_id": "e1", "payload": {}}
                )
            )


def test_propose_instinct_uses_fixed_proposer_label():
    mock_call = AsyncMock(return_value={"proposed": "p1", "status": "pending"})
    with patch("infra_brain.runner.tools.call_mcp_tool", mock_call):
        result = asyncio.run(
            runner_propose_instinct.ainvoke(
                {"zone": "corpor", "domain": "linux", "pattern": "x", "confidence": 0.8}
            )
        )
    assert result == {"proposed": "p1", "status": "pending"}
    mock_call.assert_called_once_with(
        "propose_instinct",
        zone="corpor", domain="linux", pattern="x", confidence=0.8,
        evidence="", citation="", proposer_label="headless-runner",
    )


def test_propose_instinct_failure_propagates_not_swallowed():
    mock_call = AsyncMock(side_effect=HeadlessRunnerMcpError("mutations disabled"))
    with patch("infra_brain.runner.tools.call_mcp_tool", mock_call):
        with pytest.raises(HeadlessRunnerMcpError, match="mutations disabled"):
            asyncio.run(
                runner_propose_instinct.ainvoke(
                    {"zone": "corpor", "domain": "linux", "pattern": "x", "confidence": 0.8}
                )
            )
