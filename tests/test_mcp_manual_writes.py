"""MCP approve/reject parity + the provenance-marked manual write tools.

Covers, against a real (sqlite) session:

* ``approve_proposal`` parity with the dashboard route — entity-resolution
  guard, confidence floor, approved_by/approved_at, graph resume.
* the new ``reject_proposal`` tool.
* ``record_rootcause_note`` — provenance marking + the one-note-per-drift-event
  behavior RootCauseAgent itself has.
* ``record_compliance_gap`` — the gap-finder's exact write shape/target,
  provenance marking, and its rejected-included idempotency.
* the mutation gate on every one of them.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain import mcp_auth, mcp_server
from infra_brain.db.models import (
    ZONE_CORPORATE,
    DriftEvent,
    ProposedAction,
    Resource,
    RootCauseNote,
)
from infra_brain.graph_phase3 import REVIEW_ACTION_TYPE

from tests.support.pg import make_engine


# These tests call tool functions directly in-process, with no ASGI/MCP HTTP
# request context at all — precisely the TRK-247 direct-invocation shape (the
# same shape as the `docker exec ... python -c "from infra_brain.mcp_server
# import ..."` bypass). The server-derived caller identity therefore resolves
# to the direct-invocation sentinel, not the unauthenticated-over-real-HTTP
# one (see the dedicated identity tests below, which patch fastmcp's request
# context to distinguish the two). Attribution is NEVER the caller-supplied
# label either way.
ANON = mcp_server.DIRECT_INVOCATION_IDENTITY


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


@pytest.fixture(autouse=True)
def no_graph_resume():
    """The interrupt graph is a separate concern (and flag-gated off); stub the
    resume so these tests exercise the DB decision path only."""
    with patch(
        "infra_brain.remediation_graph.resume_remediation_action_sync", return_value=False
    ) as m:
        yield m


def _action(session, **kw) -> ProposedAction:
    row = ProposedAction(
        id=uuid.uuid4(),
        agent="remediation",
        action_type=kw.pop("action_type", "config_fix"),
        target=kw.pop("target", "web01:ntp"),
        payload={},
        confidence=kw.pop("confidence", 0.9),
        status=kw.pop("status", "pending"),
        created_at=datetime.now(UTC),
        **kw,
    )
    session.add(row)
    session.commit()
    return row


def _drift(session) -> DriftEvent:
    r = Resource(
        id=uuid.uuid4(),
        name="web01",
        domain="linux",
        type="host",
        source="test",
        zone=ZONE_CORPORATE,
    )
    session.add(r)
    session.flush()
    de = DriftEvent(
        id=uuid.uuid4(),
        resource_id=r.id,
        drift_type="config",
        field="ntp",
        old_value="a",
        new_value="b",
        status="open",
        detected_at=datetime.now(UTC),
    )
    session.add(de)
    session.commit()
    return de


# ---------------------------------------------------------------------------
# Mutation gate — every new/modified tool
# ---------------------------------------------------------------------------


def test_all_mutating_tools_blocked_without_flag(patched_session, monkeypatch):
    monkeypatch.delenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", raising=False)
    aid = str(uuid.uuid4())
    assert "disabled" in mcp_server.approve_proposal(aid)["error"]
    assert "disabled" in mcp_server.reject_proposal(aid)["error"]
    assert "disabled" in mcp_server.record_rootcause_note(aid, "why", "operator")["error"]
    assert "disabled" in mcp_server.record_compliance_gap("d", "c", "why", "operator")["error"]


# ---------------------------------------------------------------------------
# approve_proposal — dashboard parity
# ---------------------------------------------------------------------------


def test_approve_proposal_rejects_non_uuid(patched_session, mutations_enabled):
    assert mcp_server.approve_proposal("nope")["error"] == "action_id must be a UUID"


def test_approve_proposal_blank_label_is_not_an_error(patched_session, mutations_enabled):
    """A blank label is no longer refused — attribution never depended on it.

    The identity comes from the authenticated key, so an absent/whitespace
    label just means "no extra human hint", not "anonymous approval".
    """
    assert mcp_server.approve_proposal(str(uuid.uuid4()), "  ")["error"] == "Action not found"


def test_approve_proposal_missing_action(patched_session, mutations_enabled):
    assert mcp_server.approve_proposal(str(uuid.uuid4()))["error"] == "Action not found"


def test_approve_proposal_sets_approver_and_timestamp(
    patched_session, mutations_enabled, no_graph_resume
):
    with Session(patched_session) as s:
        action_id = _action(s).id

    result = mcp_server.approve_proposal(str(action_id), "operator")

    assert result["approved"] == str(action_id)
    assert result["target"] == "web01:ntp"
    # The caller's label is quoted as a CLAIM behind the server-derived
    # identity — it never becomes the attribution on its own.
    assert result["approved_by"] == f"{ANON} (says: operator)"
    with Session(patched_session) as s:
        row = s.get(ProposedAction, action_id)
        assert row.status == "approved"
        assert row.approved_by == f"{ANON} (says: operator)"
        assert row.approved_at is not None
    # the parked graph is resumed immediately, not left to the poll
    assert no_graph_resume.call_count == 1
    assert no_graph_resume.call_args.kwargs["approved"] is True


def test_approve_proposal_refuses_entity_resolution_review_row(
    patched_session, mutations_enabled, no_graph_resume
):
    with Session(patched_session) as s:
        action_id = _action(s, action_type=REVIEW_ACTION_TYPE, target="review:1").id

    result = mcp_server.approve_proposal(str(action_id))

    assert "entity-resolution review rows cannot be approved here" in result["error"]
    with Session(patched_session) as s:
        assert s.get(ProposedAction, action_id).status == "pending"
    assert no_graph_resume.call_count == 0


def test_approve_proposal_enforces_confidence_floor(
    patched_session, mutations_enabled, no_graph_resume
):
    with Session(patched_session) as s:
        action_id = _action(s, confidence=0.5).id

    assert "Confidence < 0.7" in mcp_server.approve_proposal(str(action_id))["error"]
    with Session(patched_session) as s:
        assert s.get(ProposedAction, action_id).status == "pending"
    assert no_graph_resume.call_count == 0


def test_approve_proposal_refuses_non_pending(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        action_id = _action(s, status="approved").id

    assert "not pending" in mcp_server.approve_proposal(str(action_id))["error"]


# ---------------------------------------------------------------------------
# reject_proposal
# ---------------------------------------------------------------------------


def test_reject_proposal_rejects_non_uuid(patched_session, mutations_enabled):
    assert mcp_server.reject_proposal("nope")["error"] == "action_id must be a UUID"


def test_reject_proposal_flips_status_and_resumes(
    patched_session, mutations_enabled, no_graph_resume
):
    with Session(patched_session) as s:
        action_id = _action(s).id

    result = mcp_server.reject_proposal(str(action_id))

    assert result["rejected"] == str(action_id)
    with Session(patched_session) as s:
        row = s.get(ProposedAction, action_id)
        assert row.status == "rejected"
        assert row.approved_by is None
    assert no_graph_resume.call_args.kwargs["approved"] is False


def test_reject_proposal_refuses_non_pending(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        action_id = _action(s, status="executed").id

    assert "not pending" in mcp_server.reject_proposal(str(action_id))["error"]


def test_reject_proposal_missing_action(patched_session, mutations_enabled):
    assert mcp_server.reject_proposal(str(uuid.uuid4()))["error"] == "Action not found"


# ---------------------------------------------------------------------------
# record_rootcause_note
# ---------------------------------------------------------------------------


def test_record_rootcause_note_validates_input(patched_session, mutations_enabled):
    assert "must be a UUID" in mcp_server.record_rootcause_note("x", "why", "operator")["error"]
    aid = str(uuid.uuid4())
    # A blank author LABEL is fine now — identity comes from the auth key.
    assert "not found" in mcp_server.record_rootcause_note(aid, "why", " ")["error"]
    assert "explanation" in mcp_server.record_rootcause_note(aid, " ", "operator")["error"]
    assert "not found" in mcp_server.record_rootcause_note(aid, "why", "operator")["error"]


def test_record_rootcause_note_marks_provenance(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    result = mcp_server.record_rootcause_note(
        str(de_id), "someone rebooted the box", "operator", {"runs": ["r1"]}
    )

    expected_author = f"{ANON} (says: operator)"
    assert result["source"] == "manual_mcp"
    assert result["authored_by"] == expected_author
    with Session(patched_session) as s:
        note = s.execute(select(RootCauseNote)).scalars().one()
        assert note.explanation.startswith(f"[MANUAL/MCP-authored by {expected_author}] ")
        assert "someone rebooted the box" in note.explanation
        assert note.correlated["source"] == "manual_mcp"
        assert note.correlated["authored_by"] == expected_author
        assert note.correlated["recorded_at"]
        # caller-supplied data is preserved alongside the markers
        assert note.correlated["runs"] == ["r1"]


def test_record_rootcause_note_marker_cannot_be_spoofed(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    mcp_server.record_rootcause_note(
        str(de_id),
        "why",
        "operator",
        {"source": "rootcause_agent", "authored_by": "RootCauseAgent"},
    )

    with Session(patched_session) as s:
        note = s.execute(select(RootCauseNote)).scalars().one()
        assert note.correlated["source"] == "manual_mcp"
        assert note.correlated["authored_by"] == f"{ANON} (says: operator)"


def test_record_rootcause_note_never_overwrites_existing(patched_session, mutations_enabled):
    """RootCauseAgent only ever writes notes for events that have none; this
    tool mirrors that — the unique constraint is never hit."""
    with Session(patched_session) as s:
        de_id = _drift(s).id
        s.add(RootCauseNote(drift_event_id=de_id, explanation="agent wrote this", correlated={}))
        s.commit()

    result = mcp_server.record_rootcause_note(str(de_id), "manual take", "operator")

    assert "already exists" in result["error"]
    assert result["existing_note_id"]
    with Session(patched_session) as s:
        notes = s.execute(select(RootCauseNote)).scalars().all()
        assert len(notes) == 1
        assert notes[0].explanation == "agent wrote this"


# ---------------------------------------------------------------------------
# record_compliance_gap
# ---------------------------------------------------------------------------


def test_record_compliance_gap_validates_input(patched_session, mutations_enabled):
    assert "rule_domain" in mcp_server.record_compliance_gap(" ", "c", "d", "operator")["error"]
    assert "condition_type" in mcp_server.record_compliance_gap("r", " ", "d", "operator")["error"]
    assert "description" in mcp_server.record_compliance_gap("r", "c", " ", "operator")["error"]
    # A blank author LABEL is no longer a validation error — the identity that
    # actually gets recorded comes from the authenticated key, not from here.
    assert "error" not in mcp_server.record_compliance_gap("r", "c", "d", " ")


def test_record_compliance_gap_writes_gap_finder_shape(patched_session, mutations_enabled):
    from infra_brain.agents.compliance import _stable_gap_hash

    result = mcp_server.record_compliance_gap(
        "backup_retention", "missing_backup_verification", "no one checks restores", "operator"
    )

    expected_target = (
        f"rule-gap:{_stable_gap_hash('backup_retention', 'missing_backup_verification')}"
    )
    assert result["target"] == expected_target
    with Session(patched_session) as s:
        row = s.execute(select(ProposedAction)).scalars().one()
        assert row.action_type == "compliance_rule_gap"
        assert row.target == expected_target
        assert row.status == "pending"
        assert row.confidence == 0.5
        # provenance: the agent column is NOT "compliance"
        assert row.agent == "manual_mcp"
        assert row.payload["source"] == "manual_mcp"
        assert row.payload["authored_by"] == f"{ANON} (says: operator)"
        assert row.payload["description"].startswith(
            f"[MANUAL/MCP-authored by {ANON} (says: operator)] "
        )
        assert row.payload["rule_domain"] == "backup_retention"
        assert row.payload["condition_type"] == "missing_backup_verification"


def test_record_compliance_gap_is_idempotent(patched_session, mutations_enabled):
    first = mcp_server.record_compliance_gap("d", "c", "why", "operator")
    second = mcp_server.record_compliance_gap("d", "c", "why again", "operator")

    assert "action_id" in first
    assert second["skipped"] is True
    assert second["existing_action_id"] == first["action_id"]
    with Session(patched_session) as s:
        assert len(s.execute(select(ProposedAction)).scalars().all()) == 1


def test_record_compliance_gap_does_not_repropose_rejected(patched_session, mutations_enabled):
    """Mirrors test_gap_finder_idempotent_rejected_not_reproposed: an
    operator-rejected gap must never come back."""
    first = mcp_server.record_compliance_gap("d", "c", "why", "operator")
    with Session(patched_session) as s:
        row = s.get(ProposedAction, uuid.UUID(first["action_id"]))
        row.status = "rejected"
        s.commit()

    second = mcp_server.record_compliance_gap("d", "c", "why", "operator")

    assert second["skipped"] is True
    with Session(patched_session) as s:
        assert len(s.execute(select(ProposedAction)).scalars().all()) == 1


# ---------------------------------------------------------------------------
# Attribution is bound to the AUTHENTICATED KEY, not to caller input
# ---------------------------------------------------------------------------


# Real ANON: what a genuinely unauthenticated call over the ACTUAL ASGI/MCP
# HTTP transport resolves to (a live request context exists, it just carries
# no usable bearer token). Distinct from module-level `ANON` above, which is
# the direct-invocation sentinel for the no-HTTP-context in-process shape.
_HTTP_ANON = mcp_server.UNAUTHENTICATED_CALLER_IDENTITY


@contextlib.contextmanager
def _with_http_request():
    """Simulate the presence of a real ASGI/MCP HTTP request context.

    Patches ``get_http_request`` (used by the TRK-247 guard,
    ``_has_active_http_request``) to not raise, so ``_caller_identity()``
    proceeds into the header-resolution path instead of short-circuiting to
    the direct-invocation sentinel. A bare truthy sentinel object is enough —
    nothing downstream inspects its attributes.
    """
    with patch("fastmcp.server.dependencies.get_http_request", return_value=object()):
        yield


@contextlib.contextmanager
def _as_key(engine, name: str):
    """Present a live McpApiKey's raw bearer token for the duration of a call.

    Mirrors how a real request reaches a tool body: a real HTTP request
    context is present (see ``_with_http_request``), the ASGI auth middleware
    has already authenticated the Authorization header, and the tool resolves
    the SAME header via fastmcp's request-scoped get_http_headers().
    """
    with Session(engine) as s:
        _row, raw = mcp_auth.create_key(s, name, ["approve_proposal"], created_by="test")
        s.commit()
    with (
        _with_http_request(),
        patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value={"authorization": f"Bearer {raw}"},
        ),
    ):
        yield


def test_caller_identity_resolves_the_authenticated_key_name(patched_session):
    """Normal HTTP-path case: a real request context + a valid bearer token
    resolves to the real, unforgeable key identity — the TRK-247 guard must
    not interfere with this path at all."""
    with _as_key(patched_session, "reporting-bot"):
        assert mcp_server._caller_identity() == "mcp:reporting-bot"


def test_caller_identity_ignores_a_revoked_key(patched_session):
    with Session(patched_session) as s:
        row, raw = mcp_auth.create_key(s, "old-bot", ["approve_proposal"], created_by="test")
        mcp_auth.revoke_key(s, row.id)
        s.commit()
    with (
        _with_http_request(),
        patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value={"authorization": f"Bearer {raw}"},
        ),
    ):
        assert mcp_server._caller_identity() == _HTTP_ANON


def test_caller_identity_ignores_an_unknown_token(patched_session):
    with (
        _with_http_request(),
        patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value={"authorization": "Bearer ibmcp_not-a-real-token"},
        ),
    ):
        assert mcp_server._caller_identity() == _HTTP_ANON


# ---------------------------------------------------------------------------
# TRK-247: the direct-invocation runtime guard
# ---------------------------------------------------------------------------


def test_caller_identity_stamps_direct_invocation_when_no_http_context(patched_session, caplog):
    """Direct-invocation case: no HTTP request context at all (the docker-exec
    / in-process bypass shape) is detected and stamped with the dedicated
    sentinel instead of silently landing on the unauthenticated-over-HTTP one
    — and a loud warning is logged so the gap is operator-visible."""
    import logging

    with caplog.at_level(logging.WARNING, logger="infra_brain.mcp_server"):
        identity = mcp_server._caller_identity()

    assert identity == mcp_server.DIRECT_INVOCATION_IDENTITY
    assert identity != mcp_server.UNAUTHENTICATED_CALLER_IDENTITY
    assert any("TRK-247" in r.message for r in caplog.records)


def test_direct_invocation_guard_does_not_block_the_call(patched_session, mutations_enabled):
    """Detect-and-stamp ONLY: direct invocation must still succeed and write,
    never be refused — legitimate ops/debug use stays possible."""
    with Session(patched_session) as s:
        action_id = _action(s).id

    result = mcp_server.approve_proposal(str(action_id))

    assert "error" not in result
    assert result["approved_by"] == mcp_server.DIRECT_INVOCATION_IDENTITY
    with Session(patched_session) as s:
        row = s.get(ProposedAction, action_id)
        assert row.status == "approved"
        assert row.approved_by == mcp_server.DIRECT_INVOCATION_IDENTITY


def test_direct_invocation_guard_is_independent_of_http_header_state(patched_session):
    """The guard fires on absent HTTP context regardless of what headers a
    caller might separately fabricate — it is checked BEFORE header
    resolution, so it cannot be bypassed by supplying a fake Authorization
    value without an actual request context behind it."""
    with patch(
        "fastmcp.server.dependencies.get_http_headers",
        return_value={"authorization": "Bearer whatever"},
    ):
        assert mcp_server._caller_identity() == mcp_server.DIRECT_INVOCATION_IDENTITY


def test_mutations_enabled_alone_detects_direct_invocation(patched_session, caplog, monkeypatch):
    """lc-safety-reviewer finding: the guard originally only fired for tools
    that call _caller_identity() for attribution -- several mutating tools
    (seed_resource, seed_resources_bulk, seed_drift_event, seed_vulnerability,
    promote_instinct, add_eol_product, confirm_same_as) never do, so direct
    in-process invocation of THOSE tools got no warning and no stamp at all.
    _mutations_enabled() now triggers the same detection every mutating tool
    already passes through via its own gate check -- proven here by calling
    ONLY _mutations_enabled(), never _caller_identity(), and still seeing the
    warning fire."""
    import logging

    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "true")
    with caplog.at_level(logging.WARNING, logger="infra_brain.mcp_server"):
        enabled = mcp_server._mutations_enabled()

    assert enabled is True
    assert any("TRK-247" in r.message for r in caplog.records)


def test_mutations_enabled_silent_when_mutations_disabled(caplog, monkeypatch):
    """No point warning about attribution on a call that's about to be
    refused outright for having mutations disabled."""
    import logging

    monkeypatch.delenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", raising=False)
    with caplog.at_level(logging.WARNING, logger="infra_brain.mcp_server"):
        enabled = mcp_server._mutations_enabled()

    assert enabled is False
    assert not any("TRK-247" in r.message for r in caplog.records)


def test_direct_invocation_still_allowed_when_hardening_flag_off(
    patched_session, mutations_enabled, monkeypatch
):
    """P4.4a residual gap (b), default state: INFRA_BRAIN_MCP_DENY_DIRECT_INVOCATION
    unset/false must leave today's documented behavior exactly unchanged --
    direct invocation still succeeds."""
    monkeypatch.delenv("INFRA_BRAIN_MCP_DENY_DIRECT_INVOCATION", raising=False)
    assert mcp_server._mutations_enabled() is True


def test_direct_invocation_denied_when_hardening_flag_on(
    patched_session, mutations_enabled, monkeypatch
):
    """P4.4a residual gap (b), opt-in: with the hardening flag set, a direct
    in-process call (no HTTP request context) is refused outright instead of
    only being detect-and-stamped."""
    monkeypatch.setenv("INFRA_BRAIN_MCP_DENY_DIRECT_INVOCATION", "true")
    assert mcp_server._mutations_enabled() is False
    response = mcp_server._mutation_disabled_response()
    assert "direct in-process" in response["error"]


def test_hardening_flag_does_not_affect_a_real_http_call(
    patched_session, mutations_enabled, monkeypatch
):
    """The hardening flag only targets the no-HTTP-context shape -- a real
    (even unauthenticated-looking-to-this-check) HTTP call is unaffected."""
    monkeypatch.setenv("INFRA_BRAIN_MCP_DENY_DIRECT_INVOCATION", "true")
    with patch("fastmcp.server.dependencies.get_http_request", return_value=object()):
        assert mcp_server._mutations_enabled() is True


def test_full_access_key_write_is_logged_for_visibility(caplog):
    """P4.4a residual gap (a): a full-access key (allowed_tools == every known
    tool, the --bootstrap shape) authorizing a write is not blocked (that
    would break the documented bootstrap path) but IS logged, closing the
    "silent" half of the gap. A scoped key with the identical tool in its
    allowed_tools must NOT trigger this warning."""
    import logging

    from infra_brain.mcp_auth import ALL_TOOL_NAMES

    with (
        patch("infra_brain.mcp_server.lookup_active_key", return_value=(uuid.uuid4(), ALL_TOOL_NAMES)),
        patch("infra_brain.mcp_server.touch_last_used"),
        patch("infra_brain.mcp_server.get_session") as mock_session,
    ):
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)
        with caplog.at_level(logging.WARNING, logger="infra_brain.mcp_server"):
            ok, status, reason = mcp_server._authorize("sometoken", ["promote_instinct_v2"])

    assert ok is True and status == 200 and reason is None
    assert any("full-access MCP key" in r.message for r in caplog.records)


def test_scoped_key_write_is_not_flagged_as_full_access(caplog):
    """A key scoped to only the tools it needs (not the full catalog) must
    not trigger the full-access warning even though it can also mutate."""
    import logging

    with (
        patch(
            "infra_brain.mcp_server.lookup_active_key",
            return_value=(uuid.uuid4(), ["promote_instinct_v2", "get_environment_notes"]),
        ),
        patch("infra_brain.mcp_server.touch_last_used"),
        patch("infra_brain.mcp_server.get_session") as mock_session,
    ):
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)
        with caplog.at_level(logging.WARNING, logger="infra_brain.mcp_server"):
            ok, status, reason = mcp_server._authorize("sometoken", ["promote_instinct_v2"])

    assert ok is True and status == 200 and reason is None
    assert not any("full-access MCP key" in r.message for r in caplog.records)


def test_approve_proposal_attributes_to_the_key_not_the_caller_label(
    patched_session, mutations_enabled, no_graph_resume
):
    """THE core guard: a key scoped to approve_proposal cannot claim to be a
    human on the one gate in front of a sanctioned external write."""
    with Session(patched_session) as s:
        action_id = _action(s).id

    with _as_key(patched_session, "reporting-bot"):
        result = mcp_server.approve_proposal(str(action_id), "youruser")

    with Session(patched_session) as s:
        recorded = s.get(ProposedAction, action_id).approved_by
    assert recorded == result["approved_by"]
    assert recorded.startswith("mcp:reporting-bot")
    # The spoof attempt survives only as an explicitly-quoted caller claim.
    assert recorded == "mcp:reporting-bot (says: youruser)"


def test_manual_writes_attribute_to_the_key_not_the_caller_label(
    patched_session, mutations_enabled
):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    with _as_key(patched_session, "reporting-bot"):
        note = mcp_server.record_rootcause_note(str(de_id), "why", "RootCauseAgent")
        gap = mcp_server.record_compliance_gap("d", "c", "why", "ComplianceAgent")

    assert note["authored_by"] == "mcp:reporting-bot (says: RootCauseAgent)"
    assert gap["authored_by"] == "mcp:reporting-bot (says: ComplianceAgent)"
    with Session(patched_session) as s:
        row = s.execute(select(RootCauseNote)).scalars().one()
        assert row.correlated["authored_by"].startswith("mcp:reporting-bot")


def test_author_label_cannot_forge_the_identity_prefix(patched_session, mutations_enabled):
    """A label crafted to look like an identity is still only ever a suffix."""
    with Session(patched_session) as s:
        de_id = _drift(s).id

    with _as_key(patched_session, "reporting-bot"):
        result = mcp_server.record_rootcause_note(str(de_id), "why", "mcp:youruser")

    assert result["authored_by"].startswith("mcp:reporting-bot (says: ")


def test_attributed_author_is_bounded_to_the_approved_by_column(patched_session):
    """approved_by is String(128) — a long label must not overflow it."""
    with _as_key(patched_session, "reporting-bot"):
        composed = mcp_server._attributed_author("x" * 5000)
    assert len(composed) <= 128
    assert composed.startswith("mcp:reporting-bot (says: ")


# ---------------------------------------------------------------------------
# DLP: free-text fields are PAN-scrubbed at write time
# ---------------------------------------------------------------------------

# Luhn-valid Visa test number — redact_pans() only masks probable PANs.
_TEST_PAN = "4111111111111111"


def test_rootcause_note_scrubs_pans_in_explanation_and_correlated(
    patched_session, mutations_enabled
):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    mcp_server.record_rootcause_note(
        str(de_id),
        f"card {_TEST_PAN} was in the log",
        "operator",
        {"evidence": [f"also {_TEST_PAN}"], "nested": {"deep": _TEST_PAN}},
    )

    with Session(patched_session) as s:
        note = s.execute(select(RootCauseNote)).scalars().one()
        assert _TEST_PAN not in note.explanation
        # recursive over the JSONB blob, including inside lists
        assert _TEST_PAN not in str(note.correlated)


def test_compliance_gap_scrubs_pans_in_description(patched_session, mutations_enabled):
    mcp_server.record_compliance_gap("d", "c", f"leaked {_TEST_PAN} here", "operator")

    with Session(patched_session) as s:
        row = s.execute(select(ProposedAction)).scalars().one()
        assert _TEST_PAN not in row.payload["description"]


# ---------------------------------------------------------------------------
# Caller input is bounded before it reaches the DB
# ---------------------------------------------------------------------------


def test_oversized_explanation_is_refused(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    result = mcp_server.record_rootcause_note(str(de_id), "x" * 8001, "operator")

    assert "exceeds" in result["error"]
    with Session(patched_session) as s:
        assert s.execute(select(RootCauseNote)).scalars().all() == []


def test_oversized_description_is_refused(patched_session, mutations_enabled):
    result = mcp_server.record_compliance_gap("d", "c", "x" * 8001, "operator")

    assert "exceeds" in result["error"]
    with Session(patched_session) as s:
        assert s.execute(select(ProposedAction)).scalars().all() == []


def test_oversized_correlated_is_refused(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    result = mcp_server.record_rootcause_note(
        str(de_id), "why", "operator", {"blob": "x" * 20000}
    )

    assert "exceeds" in result["error"]
    with Session(patched_session) as s:
        assert s.execute(select(RootCauseNote)).scalars().all() == []


def test_deeply_nested_correlated_is_refused(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    deep: dict = {"leaf": 1}
    for _ in range(30):
        deep = {"n": deep}

    result = mcp_server.record_rootcause_note(str(de_id), "why", "operator", deep)

    assert "nested deeper" in result["error"]
    with Session(patched_session) as s:
        assert s.execute(select(RootCauseNote)).scalars().all() == []


def test_non_dict_correlated_is_refused(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    result = mcp_server.record_rootcause_note(str(de_id), "why", "operator", ["not", "a", "dict"])

    assert "object/dict" in result["error"]


def test_correlated_within_bounds_is_accepted(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    result = mcp_server.record_rootcause_note(str(de_id), "why", "operator", {"runs": ["r1"]})

    assert "error" not in result


# ---------------------------------------------------------------------------
# Catalog parity (TRK-231 regression guard, per-tool)
# ---------------------------------------------------------------------------


def test_new_tools_are_registered_in_the_mutation_catalog():
    """A tool missing from MUTATION_TOOL_NAMES 403s for every key, including
    the bootstrap key (TRK-231). test_mcp_auth_helpers asserts the whole-file
    parity; this pins the four tools this change touched."""

    for name in (
        "approve_proposal",
        "reject_proposal",
        "record_rootcause_note",
        "record_compliance_gap",
    ):
        assert name in mcp_auth.MUTATION_TOOL_NAMES
        assert name in mcp_auth.ALL_TOOL_NAMES
        assert name not in mcp_auth.READONLY_TOOL_NAMES
