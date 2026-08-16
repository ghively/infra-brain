"""record_rootcause_notes_bulk (Phase 2.3, 2026-07-29 implementation plan §4.3).

Covers, against a real (sqlite) session — never MagicMock, since
``sqlalchemy.inspect()`` breaks under it:

* dry_run=True is the default and validates without writing.
* the 100-item hard cap refuses the whole call before any DB work.
* per-item SAVEPOINT isolation: one item's failure doesn't roll back others.
* duplicate-note skip, both pre-existing and within-the-same-batch.
* attribution is derived from the authenticated key, per item.
* the mutation gate blocks the tool like every other mutating tool.
* the MUTATION_TOOL_NAMES catalog parity (TRK-231 regression guard).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain import mcp_auth, mcp_server
from infra_brain.db.models import ZONE_CORPORATE, DriftEvent, Resource, RootCauseNote

from tests.support.pg import make_engine


# Written before the TRK-247 runtime guard (Phase 3.1) existed. With that
# guard now integrated, a call with no HTTP request context mocked at all
# (the shape used below, no bearer token AND no request) correctly resolves
# to the direct-invocation sentinel, not the plain-unauthenticated-over-HTTP
# one -- updated to match, not reverted.
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


def _drift(session, field: str = "ntp") -> DriftEvent:
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
        field=field,
        old_value="a",
        new_value="b",
        status="open",
        detected_at=datetime.now(UTC),
    )
    session.add(de)
    session.commit()
    return de


# ---------------------------------------------------------------------------
# Mutation gate
# ---------------------------------------------------------------------------


def test_blocked_without_mutation_flag(patched_session, monkeypatch):
    monkeypatch.delenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", raising=False)

    result = mcp_server.record_rootcause_notes_bulk(
        [{"drift_event_id": str(uuid.uuid4()), "explanation": "why"}]
    )

    assert "disabled" in result["error"]


# ---------------------------------------------------------------------------
# dry_run is the default
# ---------------------------------------------------------------------------


def test_dry_run_is_the_default_and_writes_nothing(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    result = mcp_server.record_rootcause_notes_bulk(
        [{"drift_event_id": str(de_id), "explanation": "why", "author_label": "operator"}]
    )

    assert result["dry_run"] is True
    assert result["written"] == 0
    assert result["results"][0]["status"] == "valid"
    with Session(patched_session) as s:
        assert s.execute(select(RootCauseNote)).scalars().all() == []


def test_dry_run_reports_not_found_and_validation_errors(patched_session, mutations_enabled):
    missing = str(uuid.uuid4())
    result = mcp_server.record_rootcause_notes_bulk(
        [
            {"drift_event_id": "not-a-uuid", "explanation": "why"},
            {"drift_event_id": missing, "explanation": "why"},
            {"drift_event_id": missing, "explanation": " "},
        ]
    )

    assert result["errors"] == 3
    assert result["written"] == 0
    assert "must be a UUID" in result["results"][0]["error"]
    assert "not found" in result["results"][1]["error"]
    assert "explanation" in result["results"][2]["error"]


def test_dry_run_flags_existing_note_as_skipped(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id
        s.add(RootCauseNote(drift_event_id=de_id, explanation="agent wrote this", correlated={}))
        s.commit()

    result = mcp_server.record_rootcause_notes_bulk(
        [{"drift_event_id": str(de_id), "explanation": "manual take"}]
    )

    assert result["skipped"] == 1
    assert result["written"] == 0
    assert result["results"][0]["status"] == "skipped"
    assert result["results"][0]["existing_note_id"]


# ---------------------------------------------------------------------------
# Hard cap: refused whole, before any DB work
# ---------------------------------------------------------------------------


def test_cap_refuses_the_whole_call_before_any_db_work(patched_session, mutations_enabled):
    oversized = [
        {"drift_event_id": str(uuid.uuid4()), "explanation": "why"} for _ in range(101)
    ]

    result = mcp_server.record_rootcause_notes_bulk(oversized, dry_run=False)

    assert "101" in result["error"]
    assert "100" in result["error"]
    with Session(patched_session) as s:
        assert s.execute(select(RootCauseNote)).scalars().all() == []


def test_cap_boundary_is_inclusive(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        ids = [str(_drift(s, field=f"f{i}").id) for i in range(100)]

    result = mcp_server.record_rootcause_notes_bulk(
        [{"drift_event_id": i, "explanation": "why"} for i in ids]
    )

    assert "error" not in result
    assert result["total"] == 100


# ---------------------------------------------------------------------------
# Execute: written / skipped / error, per-item SAVEPOINT isolation
# ---------------------------------------------------------------------------


def test_execute_writes_valid_items_and_marks_provenance(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    result = mcp_server.record_rootcause_notes_bulk(
        [
            {
                "drift_event_id": str(de_id),
                "explanation": "someone rebooted the box",
                "author_label": "operator",
                "correlated": {"runs": ["r1"]},
            }
        ],
        dry_run=False,
    )

    assert result["dry_run"] is False
    assert result["written"] == 1
    assert result["results"][0]["status"] == "written"
    assert result["results"][0]["note_id"]
    with Session(patched_session) as s:
        note = s.execute(select(RootCauseNote)).scalars().one()
        assert note.correlated["source"] == "manual_mcp"
        assert note.correlated["runs"] == ["r1"]
        assert note.explanation.startswith(f"[MANUAL/MCP-authored by {ANON}")


def test_per_item_savepoint_isolates_one_failure_from_the_rest(patched_session, mutations_enabled):
    """One item's failure must not roll back the others in the same call."""
    with Session(patched_session) as s:
        good_id = _drift(s, field="good").id
        also_good_id = _drift(s, field="also-good").id
    missing_id = str(uuid.uuid4())

    result = mcp_server.record_rootcause_notes_bulk(
        [
            {"drift_event_id": str(good_id), "explanation": "first is fine"},
            {"drift_event_id": missing_id, "explanation": "this one errors"},
            {"drift_event_id": str(also_good_id), "explanation": "third is fine too"},
        ],
        dry_run=False,
    )

    assert result["written"] == 2
    assert result["errors"] == 1
    statuses = [r["status"] for r in result["results"]]
    assert statuses == ["written", "error", "written"]
    with Session(patched_session) as s:
        notes = s.execute(select(RootCauseNote)).scalars().all()
        assert len(notes) == 2
        recorded_ids = {str(n.drift_event_id) for n in notes}
        assert recorded_ids == {str(good_id), str(also_good_id)}


def test_per_item_savepoint_actually_rolls_back_a_real_db_error(
    patched_session, mutations_enabled
):
    """The test above (not-found) never raises inside begin_nested() -- the
    `not_found` branch returns normally, so neither the except-Exception
    handler nor a real SAVEPOINT rollback ever runs. This test injects a
    genuine DB-level failure (a PK collision -> IntegrityError on flush,
    the scenario the design doc justifies the savepoint by) for the middle
    item only, and asserts the other two still land -- proving the rollback
    is real, not just documented (lc-safety-reviewer finding)."""
    with Session(patched_session) as s:
        good_id = _drift(s, field="good").id
        colliding_id = _drift(s, field="collides").id
        also_good_id = _drift(s, field="also-good").id

    # A note that already exists for a DIFFERENT drift event, whose id we'll
    # force the colliding item's new note to reuse -- the existence check
    # only filters on drift_event_id, so this slips past it and fails at
    # flush() on the primary key instead, exactly like a concurrent duplicate
    # note landing between dry-run preview and real execution.
    forced_id = uuid.uuid4()
    with Session(patched_session) as s:
        # The occupying row needs a REAL drift event: root_cause_notes.
        # drift_event_id is a live FK, which SQLite ignores and PostgreSQL
        # enforces. Any event other than `colliding_id` keeps the point of the
        # test (the existence check filters on drift_event_id, so this row
        # slips past it and collides on the PK at flush()).
        unrelated_id = _drift(s, field="unrelated").id
        s.add(
            RootCauseNote(
                id=forced_id,
                drift_event_id=unrelated_id,  # unrelated event, just occupies the PK
                explanation="pre-existing row occupying the PK we'll collide into",
                correlated={},
            )
        )
        s.commit()

    real_build = mcp_server._build_rootcause_note

    def _colliding_build(deid, explanation, correlated, author):
        note = real_build(deid, explanation, correlated, author)
        if str(deid) == str(colliding_id):
            note.id = forced_id  # forces IntegrityError on this item's flush only
        return note

    with patch("infra_brain.mcp_server._build_rootcause_note", _colliding_build):
        result = mcp_server.record_rootcause_notes_bulk(
            [
                {"drift_event_id": str(good_id), "explanation": "first is fine"},
                {"drift_event_id": str(colliding_id), "explanation": "this one collides"},
                {"drift_event_id": str(also_good_id), "explanation": "third is fine too"},
            ],
            dry_run=False,
        )

    assert result["written"] == 2
    assert result["errors"] == 1
    statuses = [r["status"] for r in result["results"]]
    assert statuses == ["written", "error", "written"]
    with Session(patched_session) as s:
        notes = s.execute(select(RootCauseNote)).scalars().all()
        # the pre-existing PK-occupying row, plus exactly the 2 good writes --
        # the colliding item's attempted note must not have persisted, and
        # the batch's final commit must not have been poisoned by it.
        assert len(notes) == 3
        recorded_event_ids = {str(n.drift_event_id) for n in notes}
        assert {str(good_id), str(also_good_id)} <= recorded_event_ids
        assert str(colliding_id) not in recorded_event_ids


def test_duplicate_note_within_the_same_batch_is_skipped_not_errored(
    patched_session, mutations_enabled
):
    """Two items targeting the SAME drift event in one call: the first
    writes, the second is a clean skip (uq_rootcause_drift is never hit)."""
    with Session(patched_session) as s:
        de_id = _drift(s).id

    result = mcp_server.record_rootcause_notes_bulk(
        [
            {"drift_event_id": str(de_id), "explanation": "first writer wins"},
            {"drift_event_id": str(de_id), "explanation": "second is redundant"},
        ],
        dry_run=False,
    )

    assert result["written"] == 1
    assert result["skipped"] == 1
    assert result["errors"] == 0
    assert result["results"][0]["status"] == "written"
    assert result["results"][1]["status"] == "skipped"
    with Session(patched_session) as s:
        notes = s.execute(select(RootCauseNote)).scalars().all()
        assert len(notes) == 1
        assert notes[0].explanation.endswith("first writer wins")


def test_duplicate_note_pre_existing_is_skipped_on_execute(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id
        s.add(RootCauseNote(drift_event_id=de_id, explanation="agent wrote this", correlated={}))
        s.commit()

    result = mcp_server.record_rootcause_notes_bulk(
        [{"drift_event_id": str(de_id), "explanation": "manual take"}], dry_run=False
    )

    assert result["skipped"] == 1
    assert result["written"] == 0
    with Session(patched_session) as s:
        notes = s.execute(select(RootCauseNote)).scalars().all()
        assert len(notes) == 1
        assert notes[0].explanation == "agent wrote this"


def test_execute_reports_not_found_as_error(patched_session, mutations_enabled):
    result = mcp_server.record_rootcause_notes_bulk(
        [{"drift_event_id": str(uuid.uuid4()), "explanation": "why"}], dry_run=False
    )

    assert result["errors"] == 1
    assert result["written"] == 0
    assert "not found" in result["results"][0]["error"]


def test_empty_notes_list_is_a_no_op(patched_session, mutations_enabled):
    result = mcp_server.record_rootcause_notes_bulk([], dry_run=False)

    assert result["total"] == 0
    assert result["written"] == 0
    assert result["results"] == []


# ---------------------------------------------------------------------------
# Attribution — derived from the authenticated key, per item
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _as_key(engine, name: str):
    """Present a live McpApiKey's bearer token AND a real HTTP request context.

    Written before the TRK-247 runtime guard (Phase 3.1) existed -- that
    guard checks ``_has_active_http_request()`` first and short-circuits to
    the direct-invocation sentinel if it's absent, so mocking headers alone
    (this helper's original form) is no longer sufficient once both branches
    are integrated together. Mirrors test_mcp_manual_writes.py's
    ``_with_http_request()``.
    """
    with Session(engine) as s:
        _row, raw = mcp_auth.create_key(s, name, ["record_rootcause_notes_bulk"], created_by="t")
        s.commit()
    with (
        patch("fastmcp.server.dependencies.get_http_request", return_value=object()),
        patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value={"authorization": f"Bearer {raw}"},
        ),
    ):
        yield


def test_attribution_is_bound_to_the_authenticated_key_not_the_caller_label(
    patched_session, mutations_enabled
):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    with _as_key(patched_session, "reporting-bot"):
        result = mcp_server.record_rootcause_notes_bulk(
            [
                {
                    "drift_event_id": str(de_id),
                    "explanation": "why",
                    "author_label": "RootCauseAgent",
                }
            ],
            dry_run=False,
        )

    assert result["results"][0]["authored_by"] == "mcp:reporting-bot (says: RootCauseAgent)"
    with Session(patched_session) as s:
        note = s.execute(select(RootCauseNote)).scalars().one()
        assert note.correlated["authored_by"] == "mcp:reporting-bot (says: RootCauseAgent)"


# ---------------------------------------------------------------------------
# Bounds/DLP — same guards as record_rootcause_note (shared helper)
# ---------------------------------------------------------------------------

_TEST_PAN = "4111111111111111"


def test_scrubs_pans_and_enforces_size_caps(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        de_id = _drift(s).id

    result = mcp_server.record_rootcause_notes_bulk(
        [
            {
                "drift_event_id": str(de_id),
                "explanation": f"card {_TEST_PAN} was in the log",
                "correlated": {"evidence": [_TEST_PAN]},
            },
            {"drift_event_id": str(uuid.uuid4()), "explanation": "x" * 8001},
        ],
        dry_run=False,
    )

    assert result["written"] == 1
    assert result["errors"] == 1
    assert "exceeds" in result["results"][1]["error"]
    with Session(patched_session) as s:
        note = s.execute(select(RootCauseNote)).scalars().one()
        assert _TEST_PAN not in note.explanation
        assert _TEST_PAN not in str(note.correlated)


# ---------------------------------------------------------------------------
# Catalog parity (TRK-231 regression guard)
# ---------------------------------------------------------------------------


def test_registered_in_the_mutation_catalog():
    assert "record_rootcause_notes_bulk" in mcp_auth.MUTATION_TOOL_NAMES
    assert "record_rootcause_notes_bulk" in mcp_auth.ALL_TOOL_NAMES
    assert "record_rootcause_notes_bulk" not in mcp_auth.READONLY_TOOL_NAMES
