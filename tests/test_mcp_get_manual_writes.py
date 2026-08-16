"""Tests for the read-only get_manual_writes() MCP tool (Phase 2, TRK-247
mitigation — see docs/decisions/2026-07-29-implementation-plan.md section
4.1).

Covers: kind filtering (rootcause/compliance_gap/all), authored_by substring
filter, since filter, and the limit-cap clamp behavior — against a real
(sqlite) session, exactly like the other manual-write tool tests.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain import mcp_server
from infra_brain.db.models import DriftEvent, ProposedAction, Resource, RootCauseNote

from tests.support.pg import make_engine


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


def _drift(session) -> DriftEvent:
    r = Resource(domain="linux", type="host", name="web01", source="test")
    session.add(r)
    session.flush()
    de = DriftEvent(resource_id=r.id, drift_type="config", field="ntp")
    session.add(de)
    session.commit()
    return de


def _manual_note(session, drift_event_id, authored_by="mcp:unauthenticated", recorded_at=None):
    note = RootCauseNote(
        drift_event_id=drift_event_id,
        explanation="[MANUAL/MCP-authored] backfill",
        correlated={
            "source": "manual_mcp",
            "authored_by": authored_by,
            "recorded_at": (recorded_at or datetime.now(UTC)).isoformat(),
        },
    )
    if recorded_at is not None:
        note.created_at = recorded_at
    session.add(note)
    session.commit()
    return note


def _agent_note(session, drift_event_id):
    note = RootCauseNote(
        drift_event_id=drift_event_id,
        explanation="agent-authored",
        correlated={"source": "rootcause_agent"},
    )
    session.add(note)
    session.commit()
    return note


def _manual_gap(session, authored_by="mcp:unauthenticated", target="rule-gap:1"):
    action = ProposedAction(
        id=uuid.uuid4(),
        agent="manual_mcp",
        action_type="compliance_rule_gap",
        target=target,
        payload={"source": "manual_mcp", "authored_by": authored_by, "description": "d"},
        confidence=0.5,
        status="pending",
        created_at=datetime.now(UTC),
    )
    session.add(action)
    session.commit()
    return action


def _agent_gap(session, target="rule-gap:2"):
    action = ProposedAction(
        id=uuid.uuid4(),
        agent="compliance",
        action_type="compliance_rule_gap",
        target=target,
        payload={"description": "d"},
        confidence=0.9,
        status="pending",
        created_at=datetime.now(UTC),
    )
    session.add(action)
    session.commit()
    return action


# ---------------------------------------------------------------------------
# kind filtering
# ---------------------------------------------------------------------------


def test_kind_all_returns_both_categories(patched_session):
    with Session(patched_session) as s:
        de1 = _drift(s)
        _manual_note(s, de1.id)
        de2 = _drift(s)
        _agent_note(s, de2.id)  # not manual — must not appear
        _manual_gap(s)
        _agent_gap(s)  # not manual — must not appear

    result = mcp_server.get_manual_writes(kind="all")

    assert len(result["rootcause"]) == 1
    assert len(result["compliance_gap"]) == 1
    assert result["total"] == 2


def test_kind_rootcause_excludes_compliance_gap(patched_session):
    with Session(patched_session) as s:
        de = _drift(s)
        _manual_note(s, de.id)
        _manual_gap(s)

    result = mcp_server.get_manual_writes(kind="rootcause")

    assert len(result["rootcause"]) == 1
    assert result["compliance_gap"] == []


def test_kind_compliance_gap_excludes_rootcause(patched_session):
    with Session(patched_session) as s:
        de = _drift(s)
        _manual_note(s, de.id)
        _manual_gap(s)

    result = mcp_server.get_manual_writes(kind="compliance_gap")

    assert result["rootcause"] == []
    assert len(result["compliance_gap"]) == 1


def test_only_manual_mcp_provenance_rows_are_returned(patched_session):
    """The whole point: filters on the server-generated marker, not just
    'any row in these tables'."""
    with Session(patched_session) as s:
        de = _drift(s)
        _agent_note(s, de.id)
        _agent_gap(s)

    result = mcp_server.get_manual_writes(kind="all")

    assert result["rootcause"] == []
    assert result["compliance_gap"] == []
    assert result["total"] == 0


def test_invalid_kind_is_refused(patched_session):
    result = mcp_server.get_manual_writes(kind="bogus")
    assert "error" in result


# ---------------------------------------------------------------------------
# authored_by substring filter
# ---------------------------------------------------------------------------


def test_authored_by_substring_filters_rootcause(patched_session):
    with Session(patched_session) as s:
        de = _drift(s)
        _manual_note(s, de.id, authored_by="mcp:unauthenticated")
        de2 = _drift(s)
        _manual_note(s, de2.id, authored_by="mcp:reporting-bot (says: operator)")

    result = mcp_server.get_manual_writes(kind="rootcause", authored_by="reporting-bot")

    assert len(result["rootcause"]) == 1


def test_authored_by_substring_filters_compliance_gap(patched_session):
    with Session(patched_session) as s:
        _manual_gap(s, authored_by="mcp:unauthenticated", target="rule-gap:a")
        _manual_gap(s, authored_by="mcp:reporting-bot (says: operator)", target="rule-gap:b")

    result = mcp_server.get_manual_writes(kind="compliance_gap", authored_by="reporting-bot")

    assert len(result["compliance_gap"]) == 1


def test_authored_by_no_match_returns_empty(patched_session):
    with Session(patched_session) as s:
        de = _drift(s)
        _manual_note(s, de.id, authored_by="mcp:unauthenticated")

    result = mcp_server.get_manual_writes(kind="rootcause", authored_by="nonexistent-key")

    assert result["rootcause"] == []


# ---------------------------------------------------------------------------
# since filter
# ---------------------------------------------------------------------------


def test_since_filters_out_older_rows(patched_session):
    old = datetime.now(UTC) - timedelta(days=30)
    recent = datetime.now(UTC) - timedelta(hours=1)
    with Session(patched_session) as s:
        de_old = _drift(s)
        _manual_note(s, de_old.id, recorded_at=old)
        de_new = _drift(s)
        _manual_note(s, de_new.id, recorded_at=recent)

    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    result = mcp_server.get_manual_writes(kind="rootcause", since=since)

    assert len(result["rootcause"]) == 1


def test_since_invalid_timestamp_is_refused(patched_session):
    result = mcp_server.get_manual_writes(since="not-a-timestamp")
    assert "error" in result


# ---------------------------------------------------------------------------
# limit + hard cap
# ---------------------------------------------------------------------------


def test_default_limit_is_100(patched_session):
    with Session(patched_session) as s:
        for _ in range(150):
            de = _drift(s)
            _manual_note(s, de.id)

    result = mcp_server.get_manual_writes(kind="rootcause")

    assert len(result["rootcause"]) == 100
    assert result["limit_applied"] == 100
    assert result["limit_clamped"] is False


def test_limit_above_cap_is_clamped_and_reported(patched_session):
    with Session(patched_session) as s:
        for _ in range(10):
            de = _drift(s)
            _manual_note(s, de.id)

    result = mcp_server.get_manual_writes(kind="rootcause", limit=10_000)

    assert result["limit_requested"] == 10_000
    assert result["limit_applied"] == 500
    assert result["limit_clamped"] is True
    # only 10 rows exist, so the clamp itself doesn't truncate anything here
    assert len(result["rootcause"]) == 10


def test_explicit_limit_under_cap_is_honored(patched_session):
    with Session(patched_session) as s:
        for _ in range(5):
            de = _drift(s)
            _manual_note(s, de.id)

    result = mcp_server.get_manual_writes(kind="rootcause", limit=2)

    assert len(result["rootcause"]) == 2
    assert result["limit_applied"] == 2
    assert result["limit_clamped"] is False


# ---------------------------------------------------------------------------
# Catalog parity (TRK-231-style regression guard)
# ---------------------------------------------------------------------------


def test_get_manual_writes_registered_as_readonly():
    from infra_brain import mcp_auth

    assert "get_manual_writes" in mcp_auth.READONLY_TOOL_NAMES
    assert "get_manual_writes" in mcp_auth.ALL_TOOL_NAMES
    assert "get_manual_writes" not in mcp_auth.MUTATION_TOOL_NAMES
