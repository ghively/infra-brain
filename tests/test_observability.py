"""Tests for Task 28: Full Action Logging & Observability."""

import logging
import os
import uuid
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# init_tracing tests
# ---------------------------------------------------------------------------


def test_init_tracing_sets_env_vars_when_enabled(monkeypatch):
    """init_tracing() sets LangSmith env vars when langsmith_tracing=True."""
    # Clear any pre-existing env vars so we get a clean slate
    for var in (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
        "LANGCHAIN_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)

    from infra_brain.config import Settings
    from infra_brain.observability import init_tracing

    settings = Settings(
        langsmith_tracing=True,
        langsmith_api_key="test-key-123",
        langsmith_project="my-project",
        langsmith_endpoint="https://api.smith.langchain.com",
    )
    init_tracing(settings)

    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == "test-key-123"
    assert os.environ["LANGCHAIN_PROJECT"] == "my-project"
    assert os.environ["LANGCHAIN_ENDPOINT"] == "https://api.smith.langchain.com"


def test_init_tracing_noop_when_disabled(monkeypatch):
    """init_tracing() does NOT set env vars when langsmith_tracing=False."""
    for var in (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
        "LANGCHAIN_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)

    from infra_brain.config import Settings
    from infra_brain.observability import init_tracing

    settings = Settings(langsmith_tracing=False)
    init_tracing(settings)

    assert "LANGCHAIN_TRACING_V2" not in os.environ
    assert "LANGCHAIN_API_KEY" not in os.environ


def test_init_tracing_refuses_empty_endpoint(monkeypatch, caplog):
    """OB-7/TRK-042: tracing enabled + empty endpoint -> env vars NOT set, loud log."""
    for var in (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
        "LANGCHAIN_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)

    from infra_brain.config import Settings
    from infra_brain.observability import init_tracing

    settings = Settings(
        langsmith_tracing=True,
        langsmith_api_key="test-key-123",
        langsmith_project="my-project",
        langsmith_endpoint="",
    )

    # Root-cause fix: alembic/env.py now calls fileConfig(..., disable_existing_loggers=False),
    # so test_migration_zone no longer disables infra_brain.* loggers session-wide. The
    # previous test-side workaround (a dedicated handler + forcing obs_logger.disabled=False)
    # is no longer needed — caplog alone is sufficient proof the fix holds across the full
    # suite.
    with caplog.at_level(logging.WARNING, logger="infra_brain.observability"):
        init_tracing(settings)

    assert "LANGCHAIN_TRACING_V2" not in os.environ
    assert "LANGCHAIN_API_KEY" not in os.environ
    assert "LANGCHAIN_PROJECT" not in os.environ
    assert "LANGCHAIN_ENDPOINT" not in os.environ
    assert any(
        "explicit endpoint required" in r.getMessage() and "cloud default removed" in r.getMessage()
        for r in caplog.records
    )


def test_init_tracing_unsets_stale_vars_when_disabled(monkeypatch):
    """A previously-enabled process must stop leaking LANGCHAIN_* vars once tracing
    is disabled — init_tracing must actively unset them, not just skip setting new ones."""
    from infra_brain.config import Settings
    from infra_brain.observability import init_tracing

    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "stale-key")
    monkeypatch.setenv("LANGCHAIN_PROJECT", "stale-project")
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "https://stale.example.com")

    settings = Settings(langsmith_tracing=False)
    init_tracing(settings)

    assert "LANGCHAIN_TRACING_V2" not in os.environ
    assert "LANGCHAIN_API_KEY" not in os.environ
    assert "LANGCHAIN_PROJECT" not in os.environ
    assert "LANGCHAIN_ENDPOINT" not in os.environ


def test_init_tracing_unsets_stale_vars_when_endpoint_empty(monkeypatch):
    """Same leak, other trigger: tracing enabled but endpoint empty must also unset
    any stale LANGCHAIN_* vars left over from a previously-enabled process."""
    from infra_brain.config import Settings
    from infra_brain.observability import init_tracing

    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "stale-key")
    monkeypatch.setenv("LANGCHAIN_PROJECT", "stale-project")
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "https://stale.example.com")

    settings = Settings(
        langsmith_tracing=True,
        langsmith_api_key="test-key-123",
        langsmith_project="my-project",
        langsmith_endpoint="",
    )
    init_tracing(settings)

    assert "LANGCHAIN_TRACING_V2" not in os.environ
    assert "LANGCHAIN_API_KEY" not in os.environ
    assert "LANGCHAIN_PROJECT" not in os.environ
    assert "LANGCHAIN_ENDPOINT" not in os.environ


def test_langsmith_endpoint_default_is_empty():
    """OB-7/TRK-042: the Settings default must not be the public LangSmith cloud URL."""
    from infra_brain.config import Settings

    assert Settings().langsmith_endpoint == ""


# ---------------------------------------------------------------------------
# AgentActionLog persistence tests
# ---------------------------------------------------------------------------


def test_on_tool_end_writes_agent_action_log():
    """on_tool_end should write an AgentActionLog row (verdict=allow, status=ok)."""
    from infra_brain.db.models import AgentActionLog
    from infra_brain.callbacks.audit import AuditCallbackHandler

    mock_session = MagicMock()
    handler = AuditCallbackHandler(agent_name="TestAgent", domain="linux")
    run_id = uuid.uuid4()

    with patch("infra_brain.callbacks.audit.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        handler.on_tool_start({"name": "AnsibleTool"}, '{"host": "web01"}', run_id=run_id)
        handler.on_tool_end("Success", run_id=run_id)

    # Two calls: one for AuditLog, one for AgentActionLog
    assert mock_session.add.call_count == 2
    calls = [c[0][0] for c in mock_session.add.call_args_list]
    action_logs = [c for c in calls if isinstance(c, AgentActionLog)]
    assert len(action_logs) == 1

    al = action_logs[0]
    assert al.agent == "TestAgent"
    assert al.domain == "linux"
    assert al.tool == "AnsibleTool"
    assert al.verdict == "allow"
    assert al.status == "ok"
    assert al.error is None
    assert al.latency_ms is not None and al.latency_ms >= 0


def test_on_tool_error_writes_deny_verdict():
    """on_tool_error should write AgentActionLog row with verdict=deny, status=error."""
    from infra_brain.db.models import AgentActionLog
    from infra_brain.callbacks.audit import AuditCallbackHandler

    mock_session = MagicMock()
    handler = AuditCallbackHandler(agent_name="TestAgent", domain="linux")
    run_id = uuid.uuid4()

    with patch("infra_brain.callbacks.audit.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        handler.on_tool_start({"name": "AnsibleTool"}, '{"host": "web01"}', run_id=run_id)
        handler.on_tool_error(PermissionError("access denied"), run_id=run_id)

    calls = [c[0][0] for c in mock_session.add.call_args_list]
    action_logs = [c for c in calls if isinstance(c, AgentActionLog)]
    assert len(action_logs) == 1

    al = action_logs[0]
    assert al.verdict == "deny"
    assert al.status == "error"
    assert "access denied" in al.error


def test_on_tool_start_does_not_write_agent_action_log():
    """on_tool_start should NOT write AgentActionLog — only populates _pending."""
    from infra_brain.callbacks.audit import AuditCallbackHandler

    mock_session = MagicMock()
    handler = AuditCallbackHandler(agent_name="TestAgent", domain="linux")
    run_id = uuid.uuid4()

    with patch("infra_brain.callbacks.audit.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        handler.on_tool_start({"name": "AnsibleTool"}, '{"host": "web01"}', run_id=run_id)

    mock_session.add.assert_not_called()
    assert str(run_id) in handler._pending


def test_no_pending_leak_after_end():
    """_pending should be empty after on_tool_end — no memory leak."""
    from infra_brain.callbacks.audit import AuditCallbackHandler

    mock_session = MagicMock()
    handler = AuditCallbackHandler(agent_name="TestAgent", domain="linux")
    run_id = uuid.uuid4()

    with patch("infra_brain.callbacks.audit.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        handler.on_tool_start({"name": "AnsibleTool"}, '{"host": "web01"}', run_id=run_id)
        handler.on_tool_end("ok", run_id=run_id)

    assert str(run_id) not in handler._pending


def test_no_pending_leak_after_error():
    """_pending should be empty after on_tool_error — no memory leak."""
    from infra_brain.callbacks.audit import AuditCallbackHandler

    mock_session = MagicMock()
    handler = AuditCallbackHandler(agent_name="TestAgent", domain="linux")
    run_id = uuid.uuid4()

    with patch("infra_brain.callbacks.audit.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        handler.on_tool_start({"name": "AnsibleTool"}, '{"host": "web01"}', run_id=run_id)
        handler.on_tool_error(RuntimeError("fail"), run_id=run_id)

    assert str(run_id) not in handler._pending
