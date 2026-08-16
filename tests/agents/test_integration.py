"""Tests for CoverageAgent and QueryAgent — replacements for the removed IntegrationAgent.

Integration.py was deleted in the knowledge-graph branch; its functionality is now split:
  - Gap detection / proposal wiring  → CoverageAgent  (agents/coverage.py)
  - NL-to-SQL queries                → QueryAgent     (agents/query.py)

QueryAgent imports langchain>=0.3 symbols (AgentExecutor, create_tool_calling_agent) which
are absent in the hermes venv (langchain 1.3.x Claude SDK build).  Tests that exercise
QueryAgent use pytest.importorskip so they are skipped in that environment rather than
erroring at collection time.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from infra_brain.agents.coverage import CoverageAgent


def test_coverage_discover_finds_missing_domains():
    """CoverageAgent.discover() must create proposals for uncovered domains."""
    mock_session = MagicMock()
    # scan_points has linux, cloud — no k8s
    mock_session.query.return_value.filter_by.return_value.all.return_value = [
        MagicMock(domain="linux"),
        MagicMock(domain="cloud"),
    ]
    # No existing pending proposal for any domain
    mock_session.query.return_value.filter_by.return_value.first.return_value = None

    fake_registry = {"linux": object, "cloud": object, "k8s": object, "net": object}

    agent = CoverageAgent.__new__(CoverageAgent)
    agent.llm = MagicMock()
    agent.callbacks = []
    agent.settings = MagicMock()
    # reason() returns a JSON-like strategy
    agent.reason = MagicMock(
        return_value='{"resource_types": ["host"], "frequency_hint": "hourly"}'
    )

    with (
        patch("infra_brain.agents.coverage.get_session") as mock_ctx,
        patch("infra_brain.agents.coverage.get_settings") as mock_settings,
        patch("infra_brain.supervisor.AGENT_REGISTRY", fake_registry),
    ):
        mock_settings.return_value.default_zone = "corpor"
        mock_settings.return_value.coverage_gap_llm_cap = 5
        mock_settings.return_value.collect_timeout_seconds = 300
        cm = MagicMock()
        cm.__enter__ = lambda s: mock_session
        cm.__exit__ = MagicMock(return_value=False)
        mock_ctx.return_value = cm

        results = agent.discover()

    # k8s and net should have proposals created
    domains_found = {r["domain"] for r in results}
    assert "k8s" in domains_found or "net" in domains_found
    assert "linux" not in domains_found
    assert "cloud" not in domains_found


def test_coverage_wire_activates_approved_proposal():
    """CoverageAgent.wire() must create a ScanPoint for an approved proposal."""
    proposal_id = uuid.uuid4()
    mock_proposal = MagicMock()
    mock_proposal.id = proposal_id
    mock_proposal.status = "approved"
    mock_proposal.source = "coverage-gap:linux"
    mock_proposal.endpoint = "new-hosts"
    mock_proposal.confidence = 0.85

    mock_session = MagicMock()
    mock_session.get.return_value = mock_proposal
    mock_session.query.return_value.filter_by.return_value.first.return_value = None
    mock_session.query.return_value.filter_by.return_value.all.return_value = []

    agent = CoverageAgent.__new__(CoverageAgent)
    agent.llm = MagicMock()
    agent.callbacks = []
    agent.settings = MagicMock()
    agent.reason = MagicMock(return_value='{"resource_types": ["host"]}')

    with (
        patch("infra_brain.agents.coverage.get_session") as mock_ctx,
        patch("infra_brain.agents.coverage.get_settings") as mock_settings,
    ):
        mock_settings.return_value.default_zone = "corpor"
        mock_settings.return_value.coverage_gap_llm_cap = 5
        mock_settings.return_value.collect_timeout_seconds = 300
        cm = MagicMock()
        cm.__enter__ = lambda s: mock_session
        cm.__exit__ = MagicMock(return_value=False)
        mock_ctx.return_value = cm

        result = agent.wire(str(proposal_id))

    # Should add a ScanPoint
    assert mock_session.add.called
    assert result is True


def _llm_configured():
    """Patch query.get_settings past the GitLab #165 credential preflight.

    query() now refuses to invoke the model when no LLM provider is configured,
    which is the correct behavior but short-circuits any test that wants to
    exercise the reason() delegation path.
    """
    return patch(
        "infra_brain.agents.query.get_settings",
        return_value=SimpleNamespace(
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-test",
            mcp_tool_timeout_seconds=60,
        ),
    )


def test_query_returns_dict():
    """QueryAgent.query() must return a dict result with 'result' key.

    Updated for F-01 (langchain 1.3.10): the old AgentExecutor / SQLDatabaseToolkit
    pattern has been replaced with reason() + execute_sql @tool. query() now
    returns a dict, not a string. This test replaces the stale test_query_returns_string.
    """
    from infra_brain.agents.query import QueryAgent

    agent = QueryAgent.__new__(QueryAgent)
    agent.llm = MagicMock()
    agent.callbacks = []
    agent.settings = MagicMock()

    with _llm_configured(), patch.object(agent, "reason", return_value="Found 3 linux hosts"):
        result = agent.query("list all linux hosts")

    assert isinstance(result, dict)
    assert result["result"] == "Found 3 linux hosts"
    assert "question" in result
    assert result["question"] == "list all linux hosts"


def test_query_passes_callbacks_via_reason():
    """QueryAgent.query() must delegate to reason(), which carries self.callbacks.

    Updated for F-01: callbacks now travel through self.reason(), not AgentExecutor.invoke().
    """
    from infra_brain.agents.query import QueryAgent

    sentinel_callbacks = [MagicMock()]
    agent = QueryAgent.__new__(QueryAgent)
    agent.llm = MagicMock()
    agent.callbacks = sentinel_callbacks
    agent.settings = MagicMock()

    with _llm_configured(), patch.object(agent, "reason", return_value="result text") as mock_reason:
        agent.query("list all linux hosts")

    mock_reason.assert_called_once()
    # Callbacks are on self.callbacks — reason() inherits them from the agent instance.
    assert agent.callbacks is sentinel_callbacks
