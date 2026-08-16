"""TRK-258 (2) — proposed_actions/drift_events planner-freshness after bulk flips.

A 12,948-row bulk status flip on ``proposed_actions`` (via the bulk MCP tools)
left ``pg_stat_user_tables.n_live_tup`` 11x stale, because autovacuum's own
analyze-scale-factor threshold can lag well behind one large bulk write.
``mcp_server._maybe_analyze_after_bulk_write`` fixes this AT THE SOURCE: every
bulk-write tool that can flip a large slice of a table in one transaction
(``bulk_reject_proposals`` / ``bulk_approve_proposals`` / the closure loop
inside ``resolve_drift_events``) calls it right after its own commit.

These tests pin:
* postgres-dialect sessions get ``ANALYZE <table>`` when the flip exceeds
  ``_BULK_ANALYZE_THRESHOLD``, and NOT when at/under it;
* sqlite-dialect sessions (the whole rest of the suite, and CI's default
  runner) never get an ANALYZE, over threshold or not -- ANALYZE has
  different semantics there and pg_stat_user_tables staleness doesn't apply;
* an unlisted table name is a no-op (the allow-list is deliberately closed,
  not a caller-controlled f-string);
* a failing ``execute()`` (e.g. a transient DB blip) is swallowed, never
  raised -- the bulk decision has ALREADY committed by the time this runs, so
  failing here must not fail the tool call;
* the three real call sites (bulk_reject_proposals, bulk_approve_proposals,
  resolve_drift_events) actually invoke the helper with the right table name
  and the right row count, end to end against sqlite (dialect-guarded off, so
  no real ANALYZE fires, but the call arguments are what matter here).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain import mcp_server
from infra_brain.db.models import DriftEvent, ProposedAction, Resource

from tests.support.pg import make_engine


NOW = datetime.now(UTC)


# ---------------------------------------------------------------------------
# Unit tests: _maybe_analyze_after_bulk_write in isolation (mocked session)
# ---------------------------------------------------------------------------


def _mock_session(dialect_name: str) -> MagicMock:
    session = MagicMock()
    session.bind.dialect.name = dialect_name
    return session


def test_analyze_skipped_at_or_below_threshold_on_postgres():
    session = _mock_session("postgresql")
    mcp_server._maybe_analyze_after_bulk_write(
        session, "proposed_actions", mcp_server._BULK_ANALYZE_THRESHOLD
    )
    session.execute.assert_not_called()
    session.commit.assert_not_called()


def test_analyze_fires_over_threshold_on_postgres():
    session = _mock_session("postgresql")
    mcp_server._maybe_analyze_after_bulk_write(
        session, "proposed_actions", mcp_server._BULK_ANALYZE_THRESHOLD + 1
    )
    session.execute.assert_called_once()
    (stmt,), _kwargs = session.execute.call_args
    assert str(stmt) == "ANALYZE proposed_actions"
    # Explicitly committed -- get_session()'s context manager does not commit
    # on exit, so a bare execute() here would be silently discarded.
    session.commit.assert_called_once()


def test_analyze_uses_the_right_statement_for_drift_events():
    session = _mock_session("postgresql")
    mcp_server._maybe_analyze_after_bulk_write(
        session, "drift_events", mcp_server._BULK_ANALYZE_THRESHOLD + 1
    )
    (stmt,), _kwargs = session.execute.call_args
    assert str(stmt) == "ANALYZE drift_events"


def test_analyze_skipped_on_sqlite_even_over_threshold():
    """The whole non-postgres test suite must never see a real ANALYZE attempt."""
    session = _mock_session("sqlite")
    mcp_server._maybe_analyze_after_bulk_write(
        session, "proposed_actions", mcp_server._BULK_ANALYZE_THRESHOLD + 1
    )
    session.execute.assert_not_called()


def test_analyze_skipped_for_a_table_not_on_the_allow_list():
    """Closed allow-list, not an f-string over the caller-supplied table name."""
    session = _mock_session("postgresql")
    mcp_server._maybe_analyze_after_bulk_write(
        session, "compliance_violations", mcp_server._BULK_ANALYZE_THRESHOLD + 1
    )
    session.execute.assert_not_called()


def test_analyze_failure_is_swallowed_not_raised():
    """The bulk decision already committed; a blip here must not fail the call."""
    session = _mock_session("postgresql")
    session.execute.side_effect = RuntimeError("connection reset")
    mcp_server._maybe_analyze_after_bulk_write(
        session, "proposed_actions", mcp_server._BULK_ANALYZE_THRESHOLD + 1
    )  # must not raise


# ---------------------------------------------------------------------------
# Integration: the real call sites invoke the helper with the right args
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextlib.contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.mcp_server.get_session", _get_session):
        yield engine


@pytest.fixture
def mutations_enabled(monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "true")


@pytest.fixture
def no_resume():
    with patch("infra_brain.remediation_graph.resume_remediation_action_sync", return_value=False):
        yield


def _action(session, **kw) -> ProposedAction:
    a = ProposedAction(
        id=uuid.uuid4(),
        agent=kw.pop("agent", "RemediationAgent"),
        action_type=kw.pop("action_type", "config_fix"),
        target=kw.pop("target", f"host-{uuid.uuid4().hex[:8]}:risk_score"),
        payload=kw.pop("payload", {"field": "risk_score", "host": "web01"}),
        confidence=kw.pop("confidence", 0.9),
        status=kw.pop("status", "pending"),
        created_at=kw.pop("created_at", NOW),
    )
    session.add(a)
    session.flush()
    return a


def test_bulk_reject_proposals_calls_analyze_hook_with_rejected_count(
    patched_session, mutations_enabled
):
    with Session(patched_session) as s:
        for _ in range(3):
            _action(s, confidence=0.35)
        s.commit()

    with patch("infra_brain.mcp_server._maybe_analyze_after_bulk_write") as hook:
        res = mcp_server.bulk_reject_proposals(agent="RemediationAgent", dry_run=False)

    assert res["rejected"] == 3
    hook.assert_called_once()
    args = hook.call_args.args
    assert args[1] == "proposed_actions"
    assert args[2] == 3


def test_bulk_approve_proposals_calls_analyze_hook_with_approved_count(
    patched_session, mutations_enabled, no_resume
):
    with Session(patched_session) as s:
        for _ in range(4):
            _action(s, confidence=0.9)
        s.commit()

    with patch("infra_brain.mcp_server._maybe_analyze_after_bulk_write") as hook:
        res = mcp_server.bulk_approve_proposals(agent="RemediationAgent", dry_run=False)

    assert res["approved"] == 4
    hook.assert_called_once()
    args = hook.call_args.args
    assert args[1] == "proposed_actions"
    assert args[2] == 4


def _resource(session, **kw) -> Resource:
    r = Resource(
        id=uuid.uuid4(),
        domain=kw.pop("domain", "linux"),
        type=kw.pop("type", "host"),
        name=kw.pop("name", f"host-{uuid.uuid4().hex[:8]}"),
        source=kw.pop("source", "test"),
        zone=kw.pop("zone", "corporate"),
        last_seen=NOW,
    )
    session.add(r)
    session.flush()
    return r


def _drift_event(session, resource, **kw) -> DriftEvent:
    e = DriftEvent(
        id=uuid.uuid4(),
        resource_id=resource.id,
        field=kw.pop("field", "os_version"),
        drift_type=kw.pop("drift_type", "config"),
        status=kw.pop("status", "open"),
        detected_at=kw.pop("detected_at", NOW),
    )
    session.add(e)
    session.flush()
    return e


def test_resolve_drift_events_calls_analyze_hook_with_resolved_count(
    patched_session, mutations_enabled
):
    with Session(patched_session) as s:
        resource = _resource(s, domain="linux")
        for _ in range(5):
            _drift_event(s, resource, field="patch_level")
        s.commit()

    with patch("infra_brain.mcp_server._maybe_analyze_after_bulk_write") as hook:
        res = mcp_server.resolve_drift_events(
            resolution="fixed", domain="linux", dry_run=False
        )

    assert res["resolved"] == 5
    hook.assert_called_once()
    args = hook.call_args.args
    assert args[1] == "drift_events"
    assert args[2] == 5
