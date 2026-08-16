import uuid
import pytest
import httpx
from unittest.mock import patch, MagicMock
from infra_brain.tools.jira import create_compliance_jira_ticket, create_jira_ticket, ticket_exists


def test_create_jira_ticket_returns_key():
    drift_id = uuid.uuid4()
    mock_response = MagicMock()
    mock_response.json.return_value = {"key": "INFRA-42"}
    mock_response.raise_for_status = MagicMock()

    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = None

    with (
        patch("infra_brain.tools.jira.httpx.post", return_value=mock_response),
        patch("infra_brain.tools.jira.get_session") as mock_ctx,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        key = create_jira_ticket("Server drift detected", "instance_type changed", drift_id)

    assert key == "INFRA-42"
    mock_session.add.assert_called()


def test_db_write_not_committed_if_http_fails():
    """If Jira HTTP POST raises, jira.py's own session.add must never be called (no orphaned row).

    The write gate uses a *separate* session for its audit row, so we mock the two
    get_session call sites distinctly and assert only on the jira session.
    """
    drift_id = uuid.uuid4()
    jira_session = MagicMock()
    # ticket_exists returns None — no idempotency short-circuit
    jira_session.query.return_value.filter_by.return_value.first.return_value = None
    gate_session = MagicMock()

    with (
        patch("infra_brain.tools.jira.get_session") as mock_ctx,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
        patch("infra_brain.tools.jira.httpx.post", side_effect=httpx.TimeoutException("timeout")),
    ):
        mock_ctx.return_value.__enter__ = lambda s: jira_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_gate_ctx.return_value.__enter__ = lambda s: gate_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(httpx.TimeoutException):
            create_jira_ticket("Test Summary", "Test Body", drift_id)

    # jira.py must NOT persist its own row when the HTTP call fails.
    jira_session.add.assert_not_called()


def test_ticket_exists_returns_key_if_exists():
    drift_id = uuid.uuid4()
    mock_ticket = MagicMock()
    mock_ticket.jira_key = "INFRA-42"
    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = mock_ticket

    with patch("infra_brain.tools.jira.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        result = ticket_exists(drift_id)

    assert result == "INFRA-42"


# ---------------------------------------------------------------------------
# create_compliance_jira_ticket (GitLab #106) — parallel path used by
# NotificationAgent.notify_all() for open ComplianceViolation rows. Reuses
# the same write-gated POST as create_jira_ticket, but deliberately writes
# no JiraTicket row (that table's drift_event_id column is FK'd to
# drift_events, which a compliance violation id is not).
# ---------------------------------------------------------------------------


def test_create_compliance_jira_ticket_returns_key():
    mock_response = MagicMock()
    mock_response.json.return_value = {"key": "INFRA-COMPLIANCE-1"}
    mock_response.raise_for_status = MagicMock()

    with (
        patch("infra_brain.tools.jira.httpx.post", return_value=mock_response) as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: MagicMock()
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        key = create_compliance_jira_ticket("Compliance violation found", "rule detail")

    assert key == "INFRA-COMPLIANCE-1"
    mock_post.assert_called_once()


def test_create_compliance_jira_ticket_never_writes_jira_ticket_row():
    """Unlike create_jira_ticket, this must never call session.add — there is
    no valid drift_events FK target for a compliance violation id."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"key": "INFRA-COMPLIANCE-2"}
    mock_response.raise_for_status = MagicMock()

    with (
        patch("infra_brain.tools.jira.httpx.post", return_value=mock_response),
        patch("infra_brain.tools.jira.get_session") as mock_ctx,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_session = MagicMock()
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_gate_ctx.return_value.__enter__ = lambda s: MagicMock()
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        create_compliance_jira_ticket("Compliance violation found", "rule detail")

    mock_session.add.assert_not_called()


def test_create_compliance_jira_ticket_propagates_http_failure():
    with (
        patch(
            "infra_brain.tools.jira.httpx.post",
            side_effect=httpx.TimeoutException("timeout"),
        ),
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: MagicMock()
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(httpx.TimeoutException):
            create_compliance_jira_ticket("Compliance violation found", "rule detail")
