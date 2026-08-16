import uuid
import hashlib
from unittest.mock import patch, MagicMock
from infra_brain.callbacks.audit import AuditCallbackHandler
from infra_brain.db.models import AuditLog


def test_on_tool_start_does_not_write_to_db():
    """on_tool_start should only populate _pending, not write to DB."""
    mock_session = MagicMock()
    handler = AuditCallbackHandler(agent_name="TestAgent")
    run_id = uuid.uuid4()

    with patch("infra_brain.callbacks.audit.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        handler.on_tool_start({"name": "AnsibleTool"}, '{"host": "web01"}', run_id=run_id)

    # on_tool_start should NOT call session.add
    mock_session.add.assert_not_called()
    # But should populate _pending
    assert str(run_id) in handler._pending
    assert handler._pending[str(run_id)]["tool"] == "AnsibleTool"
    assert (
        handler._pending[str(run_id)]["input_hash"]
        == hashlib.sha256(b'{"host": "web01"}').hexdigest()
    )


def test_on_tool_end_writes_audit_log():
    """on_tool_end should write an AuditLog row with allowed=True to DB."""
    mock_session = MagicMock()
    handler = AuditCallbackHandler(agent_name="TestAgent")
    run_id = uuid.uuid4()

    with patch("infra_brain.callbacks.audit.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        handler.on_tool_start({"name": "AnsibleTool"}, '{"host": "web01"}', run_id=run_id)
        handler.on_tool_end("Success", run_id=run_id)

    # on_tool_end should call session.add at least once (AuditLog + AgentActionLog)
    assert mock_session.add.call_count >= 1
    calls = [c[0][0] for c in mock_session.add.call_args_list]
    audit_logs = [c for c in calls if isinstance(c, AuditLog)]
    assert len(audit_logs) == 1
    logged = audit_logs[0]
    assert logged.agent == "TestAgent"
    assert logged.tool == "AnsibleTool"
    assert logged.allowed is True
    assert logged.input_hash == hashlib.sha256(b'{"host": "web01"}').hexdigest()


def test_on_tool_error_marks_allowed_false():
    mock_session = MagicMock()
    handler = AuditCallbackHandler(agent_name="TestAgent")

    with patch("infra_brain.callbacks.audit.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        handler.on_tool_error(ValueError("denied"), run_id=uuid.uuid4())

    calls = [c[0][0] for c in mock_session.add.call_args_list]
    audit_logs = [c for c in calls if isinstance(c, AuditLog)]
    assert len(audit_logs) == 1
    logged = audit_logs[0]
    assert logged.allowed is False
    assert "denied" in logged.denial_reason
