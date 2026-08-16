"""Tests for MCP server tool fixes."""

import uuid
from unittest.mock import patch, MagicMock

import pytest

from infra_brain.mcp_server import approve_proposal, add_eol_product

from tests.support.pg import make_engine


@pytest.fixture(autouse=True)
def _enable_mcp_mutations(monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "1")


def test_approve_proposal_rejects_invalid_uuid():
    """Fix 1.4: approve_proposal must reject non-UUID action_id."""
    result = approve_proposal("not-a-uuid")
    assert "error" in result
    assert result["error"] == "action_id must be a UUID"


def test_approve_proposal_coerces_uuid_string():
    """Fix 1.4: approve_proposal must coerce valid UUID strings.

    Uses a real (sqlite) session rather than a MagicMock: approve_proposal now
    shares the dashboard route's guards via ``action_decisions.approve_action``
    (status/confidence/action_type are all read), which a bare MagicMock row
    cannot satisfy. The behavior under test — a valid UUID string is coerced
    and the action is approved — is unchanged. Full parity coverage lives in
    tests/test_mcp_manual_writes.py.
    """
    import contextlib
    from datetime import datetime, timezone
    from sqlalchemy.orm import Session

    from infra_brain.db.models import ProposedAction

    engine = make_engine()

    @contextlib.contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    test_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            ProposedAction(
                id=test_id,
                agent="remediation",
                action_type="config_fix",
                target="some-target",
                payload={},
                confidence=0.9,
                status="pending",
                created_at=datetime.now(timezone.utc),
            )
        )
        s.commit()

    with (
        patch("infra_brain.mcp_server.get_session", _get_session),
        patch(
            "infra_brain.remediation_graph.resume_remediation_action_sync",
            return_value=False,
        ),
    ):
        result = approve_proposal(str(test_id))

    assert "error" not in result
    assert result["approved"] == str(test_id)
    assert result["target"] == "some-target"


def test_add_eol_product_rejects_invalid_resource_id():
    """Fix 1.4: add_eol_product must reject non-UUID resource_id."""
    result = add_eol_product(
        asset_name="test-product", eol_date="2025-12-31", resource_id="not-a-uuid"
    )
    assert "error" in result
    assert result["error"] == "resource_id must be a UUID"


def test_add_eol_product_coerces_uuid_resource_id():
    """Fix 1.4: add_eol_product must coerce valid UUID resource_id."""
    test_rid = str(uuid.uuid4())
    mock_resource = MagicMock()
    mock_resource.id = uuid.UUID(test_rid)
    mock_entry = MagicMock()
    mock_entry.id = uuid.uuid4()

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    # Mock the query chain for resource lookup
    resource_query_mock = MagicMock()
    resource_query_mock.filter.return_value.first.return_value = mock_resource

    # Mock the query chain for eol entry lookup
    entry_query_mock = MagicMock()
    entry_query_mock.filter.return_value.first.return_value = mock_entry

    # Set up the session to return different query objects for each query call
    query_calls = [resource_query_mock, entry_query_mock]
    mock_session.query = MagicMock(side_effect=query_calls)
    mock_session.commit = MagicMock()
    mock_session.refresh = MagicMock()

    with patch("infra_brain.mcp_server.get_session", return_value=mock_session):
        result = add_eol_product(
            asset_name="test-product", eol_date="2025-12-31", resource_id=test_rid
        )

    assert "error" not in result
    assert "updated" in result
