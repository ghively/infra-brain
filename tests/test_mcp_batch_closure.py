"""Batch L — audited, predicate-scoped batch closure (GitLab #144).

Covers, against a real (sqlite) session:

* the mutation gate on both tools;
* the CORE safety property — a call with no narrowing predicate is refused, and
  ``from_status`` alone does not count as one;
* the closed ``resolution`` vocabulary (an unknown reason is refused);
* ``dry_run=True`` as the default (nothing is written unless asked);
* the hard per-call cap: over-cap matches are truncated oldest-first with
  ``remaining`` reported, never silently unbounded and never refused outright;
* the in-transaction ``agent_action_log`` closure record (predicate, reason,
  actor, and the exact id list) — the thing that makes a batch inspectable;
* the ``uq_compliance_rule_host_status`` tombstone collision, which is the one
  place closing a violation must DELETE a row, previewed in dry-run;
* per-row savepoint isolation on the compliance path, including that a failed
  row's rolled-back tombstone DELETE is NOT reported as deleted;
* per-row error strings being clamped (no raw driver SQL/parameter dump);
* server-derived attribution (never caller-supplied).

One test at the bottom runs against REAL PostgreSQL when ``TEST_DATABASE_URL``
is set (same convention as ``tests/test_dashboard_sql_columns.py``), because the
unique-constraint collision this tool exists to survive, and the
``[SQL: ...] [parameters: ...]`` driver dump the error clamp exists to suppress,
are both psycopg behaviours that sqlite cannot reproduce.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from infra_brain import mcp_auth, mcp_server
from infra_brain.db.models import (
    ZONE_CORPORATE,
    AgentActionLog,
    Base,
    ComplianceViolation,
    DriftEvent,
    Resource,
    RootCauseNote,
)

from tests.support.pg import make_engine

# These tests call the batch-closure tools directly in-process with no
# ASGI/MCP HTTP request context — the TRK-247 direct-invocation shape — so
# attribution resolves to that sentinel, not the unauthenticated-over-HTTP one.
ANON = mcp_server.DIRECT_INVOCATION_IDENTITY
NOW = datetime.now(UTC)


@pytest.fixture
def engine():
    return make_engine()


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


def _resource(session, name="web01", domain="linux") -> Resource:
    r = Resource(
        id=uuid.uuid4(),
        name=name,
        domain=domain,
        type="host",
        source="test",
        zone=ZONE_CORPORATE,
    )
    session.add(r)
    session.flush()
    return r


def _drift(session, resource, **kw) -> DriftEvent:
    de = DriftEvent(
        id=uuid.uuid4(),
        resource_id=resource.id,
        drift_type=kw.pop("drift_type", "config_drift"),
        field=kw.pop("field", "ntp"),
        old_value=kw.pop("old_value", None),
        new_value=kw.pop("new_value", {"v": 1}),
        status=kw.pop("status", "open"),
        detected_at=kw.pop("detected_at", NOW),
        collection_run_id=kw.pop("collection_run_id", None),
    )
    session.add(de)
    return de


def _violation(session, **kw) -> ComplianceViolation:
    cv = ComplianceViolation(
        id=uuid.uuid4(),
        rule=kw.pop("rule", "ntp_configured"),
        severity=kw.pop("severity", "medium"),
        host=kw.pop("host", "web01"),
        detail=kw.pop("detail", ""),
        status=kw.pop("status", "open"),
        detected_at=kw.pop("detected_at", NOW),
    )
    session.add(cv)
    return cv


def _audit_rows(engine) -> list[AgentActionLog]:
    with Session(engine) as s:
        return list(
            s.scalars(select(AgentActionLog).where(AgentActionLog.agent == "manual_mcp")).all()
        )


# ---------------------------------------------------------------------------
# Mutation gate + catalog registration
# ---------------------------------------------------------------------------


def test_closure_tools_blocked_without_flag(patched_session, monkeypatch):
    monkeypatch.delenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", raising=False)
    assert "disabled" in mcp_server.resolve_drift_events("fixed", domain="linux")["error"]
    assert "disabled" in mcp_server.close_compliance_violations("fixed", rule="x")["error"]


def test_closure_tools_are_registered_as_mutations():
    for name in ("resolve_drift_events", "close_compliance_violations"):
        assert name in mcp_auth.MUTATION_TOOL_NAMES
        assert name not in mcp_auth.READONLY_TOOL_NAMES


# ---------------------------------------------------------------------------
# The core #144 property: no predicate -> refused
# ---------------------------------------------------------------------------


def test_drift_close_refuses_unscoped_call(patched_session, mutations_enabled):
    res = mcp_server.resolve_drift_events("never_valid")
    assert "narrowing predicate is REQUIRED" in res["error"]
    # from_status is explicitly not a predicate
    res = mcp_server.resolve_drift_events("never_valid", from_status="open")
    assert "narrowing predicate is REQUIRED" in res["error"]
    # an empty id list is not a predicate either
    res = mcp_server.resolve_drift_events("never_valid", event_ids=[])
    assert "narrowing predicate is REQUIRED" in res["error"]


def test_compliance_close_refuses_unscoped_call(patched_session, mutations_enabled):
    for kwargs in ({}, {"from_status": "open"}, {"violation_ids": []}):
        res = mcp_server.close_compliance_violations("never_valid", **kwargs)
        assert "narrowing predicate is REQUIRED" in res["error"]


def test_unscoped_call_changes_nothing(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        r = _resource(s)
        _drift(s, r)
        _violation(s)
        s.commit()
    mcp_server.resolve_drift_events("never_valid", dry_run=False)
    mcp_server.close_compliance_violations("never_valid", dry_run=False)
    with Session(patched_session) as s:
        assert s.scalars(select(DriftEvent)).one().status == "open"
        assert s.scalars(select(ComplianceViolation)).one().status == "open"
    assert _audit_rows(patched_session) == []


# ---------------------------------------------------------------------------
# Reason vocabulary + input validation
# ---------------------------------------------------------------------------


def test_unknown_resolution_refused(patched_session, mutations_enabled):
    res = mcp_server.resolve_drift_events("because-i-said-so", domain="linux")
    assert "resolution must be one of" in res["error"]
    assert "never_valid" in res["resolution_meanings"]
    res = mcp_server.close_compliance_violations("whatever", rule="x")
    assert "resolution must be one of" in res["error"]


def test_bad_ids_and_timestamps_refused(patched_session, mutations_enabled):
    assert "non-UUID" in mcp_server.resolve_drift_events("fixed", event_ids=["nope"])["error"]
    assert (
        "ISO-8601"
        in mcp_server.resolve_drift_events("fixed", domain="linux", detected_before="soon")["error"]
    )
    over = [str(uuid.uuid4()) for _ in range(mcp_server._CLOSURE_MAX_IDS + 1)]
    assert "per-call cap" in mcp_server.resolve_drift_events("fixed", event_ids=over)["error"]
    assert "note" in mcp_server.resolve_drift_events("fixed", domain="linux", note="  ")["error"]


def test_wildcard_prefix_cannot_widen_to_everything(patched_session, mutations_enabled):
    """A LIKE metacharacter in *_prefix must match literally, not match-all.

    Unescaped, ``field_prefix="%"`` would expand to "every row", turning the
    mandatory-predicate check into a formality and permitting exactly the
    unscoped bulk close #144 says must not be expressible.
    """
    with Session(patched_session) as s:
        r = _resource(s)
        _drift(s, r, field="ntp")
        _drift(s, r, field="dns")
        _drift(s, r, field="%literal")
        _violation(s, rule="ntp_configured", host="h1")
        _violation(s, rule="fw_default_deny", host="h2")
        _violation(s, rule="_underscore", host="h3")
        s.commit()

    # "%" and "_" select ONLY the rows whose text really starts with them.
    assert mcp_server.resolve_drift_events("never_valid", field_prefix="%")["would_resolve"] == 1
    assert mcp_server.resolve_drift_events("never_valid", field_prefix="_")["would_resolve"] == 0
    assert (
        mcp_server.close_compliance_violations("never_valid", rule_prefix="_")["would_close"] == 1
    )
    assert (
        mcp_server.close_compliance_violations("never_valid", rule_prefix="%")["would_close"] == 0
    )
    # A blank prefix is refused outright rather than matching everything.
    assert "field_prefix" in mcp_server.resolve_drift_events("fixed", field_prefix="  ")["error"]
    assert "rule_prefix" in mcp_server.close_compliance_violations("fixed", rule_prefix="")["error"]


def test_from_status_is_a_closed_vocabulary(patched_session, mutations_enabled):
    """An empty/unknown from_status must NOT silently mean "no status filter"."""
    with Session(patched_session) as s:
        r = _resource(s)
        _drift(s, r, field="a", status="open")
        _drift(s, r, field="b", status="resolved")
        _violation(s, rule="r", host="h1", status="open")
        s.commit()

    for bad in ("", "opne", "resolved", "all"):
        assert (
            "from_status must be one of"
            in (mcp_server.resolve_drift_events("fixed", domain="linux", from_status=bad)["error"])
        )
        assert (
            "from_status must be one of"
            in (mcp_server.close_compliance_violations("fixed", rule="r", from_status=bad)["error"])
        )
    # nothing was touched by any of those refusals
    with Session(patched_session) as s:
        assert {e.field: e.status for e in s.scalars(select(DriftEvent)).all()} == {
            "a": "open",
            "b": "resolved",
        }
    assert _audit_rows(patched_session) == []
    # "acknowledged" IS accepted — NotificationAgent writes it.
    assert (
        mcp_server.resolve_drift_events("fixed", domain="linux", from_status="acknowledged")[
            "would_resolve"
        ]
        == 0
    )


# ---------------------------------------------------------------------------
# dry-run default
# ---------------------------------------------------------------------------


def test_drift_dry_run_is_the_default_and_writes_nothing(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        r = _resource(s)
        _drift(s, r)
        _drift(s, r, field="dns")
        s.commit()

    res = mcp_server.resolve_drift_events("never_valid", domain="linux")
    assert res["dry_run"] is True
    assert res["would_resolve"] == 2
    assert res["matched_total"] == 2
    assert res["remaining"] == 0
    assert res["truncated"] is False
    assert len(res["sample"]) == 2

    with Session(patched_session) as s:
        assert {e.status for e in s.scalars(select(DriftEvent)).all()} == {"open"}
    assert _audit_rows(patched_session) == []


def test_compliance_dry_run_previews_tombstone_deletions(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        _violation(s, rule="ntp_configured", host="web01", status="open")
        tomb = _violation(s, rule="ntp_configured", host="web01", status="resolved")
        s.commit()
        tomb_id = str(tomb.id)

    res = mcp_server.close_compliance_violations("never_valid", rule="ntp_configured")
    assert res["dry_run"] is True
    assert res["would_close"] == 1
    assert [t["id"] for t in res["tombstones_to_delete"]] == [tomb_id]
    with Session(patched_session) as s:
        assert s.query(ComplianceViolation).count() == 2


# ---------------------------------------------------------------------------
# Execution: drift
# ---------------------------------------------------------------------------


def test_drift_execute_resolves_and_audits(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        r = _resource(s)
        keep = _drift(s, r, field="keep-me", domain=None)
        target = _drift(s, r, field="junk", collection_run_id=None)
        s.commit()
        keep_id, target_id = keep.id, target.id

    res = mcp_server.resolve_drift_events(
        "never_valid",
        field="junk",
        unlinked_only=True,
        note="artifact of the #142 first-observation bug",
        dry_run=False,
    )
    assert res["resolved"] == 1
    assert res["to_status"] == "resolved"
    assert res["resolved_by"] == ANON

    with Session(patched_session) as s:
        assert s.get(DriftEvent, target_id).status == "resolved"
        assert s.get(DriftEvent, keep_id).status == "open"

    rows = _audit_rows(patched_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.tool == "resolve_drift_events"
    assert row.verdict == "allow" and row.status == "ok"
    assert str(target_id) in row.args_summary
    assert "never_valid" in row.args_summary
    assert "#142" in row.args_summary
    assert ANON in row.args_summary
    # the predicate itself is recorded, not just the outcome
    assert "unlinked_only" in row.args_summary


def test_drift_only_touches_requested_from_status(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        r = _resource(s)
        already = _drift(s, r, field="a", status="acknowledged")
        s.commit()
        already_id = already.id
    res = mcp_server.resolve_drift_events("fixed", domain="linux", dry_run=False)
    assert res["resolved"] == 0
    with Session(patched_session) as s:
        assert s.get(DriftEvent, already_id).status == "acknowledged"
    # a no-match execute records nothing — there is nothing to attribute
    assert _audit_rows(patched_session) == []


def test_drift_cap_truncates_oldest_first_and_reports_remaining(
    patched_session, mutations_enabled, monkeypatch
):
    monkeypatch.setattr(mcp_server, "_CLOSURE_BATCH_CAP", 2)
    with Session(patched_session) as s:
        r = _resource(s)
        for i in range(5):
            _drift(s, r, field=f"f{i}", detected_at=NOW - timedelta(days=10 - i))
        s.commit()

    res = mcp_server.resolve_drift_events("never_valid", domain="linux", limit=999, dry_run=False)
    assert res["cap"] == 2
    assert res["resolved"] == 2
    assert res["matched_total"] == 5
    assert res["remaining"] == 3
    assert res["truncated"] is True
    with Session(patched_session) as s:
        resolved = [
            e.field
            for e in s.scalars(select(DriftEvent).where(DriftEvent.status == "resolved")).all()
        ]
    assert sorted(resolved) == ["f0", "f1"]  # oldest first


def test_drift_preview_flags_investigated_events(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        r = _resource(s)
        de = _drift(s, r)
        s.flush()
        s.add(RootCauseNote(id=uuid.uuid4(), drift_event_id=de.id, explanation="looked at it"))
        s.commit()
    res = mcp_server.resolve_drift_events("never_valid", domain="linux")
    assert res["with_root_cause_note"] == 1


def test_drift_explicit_id_list_is_a_valid_predicate(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        r = _resource(s)
        a = _drift(s, r, field="a")
        _drift(s, r, field="b")
        s.commit()
        a_id = a.id
    res = mcp_server.resolve_drift_events("fixed", event_ids=[str(a_id)], dry_run=False)
    assert res["resolved"] == 1
    with Session(patched_session) as s:
        assert s.get(DriftEvent, a_id).status == "resolved"
        assert s.scalars(select(DriftEvent).where(DriftEvent.field == "b")).one().status == "open"


# ---------------------------------------------------------------------------
# Execution: compliance (incl. the tombstone collision)
# ---------------------------------------------------------------------------


def test_compliance_execute_drops_tombstone_then_flips(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        live = _violation(s, rule="ntp_configured", host="web01", status="open")
        tomb = _violation(s, rule="ntp_configured", host="web01", status="resolved")
        s.commit()
        live_id, tomb_id = live.id, tomb.id

    res = mcp_server.close_compliance_violations(
        "never_valid", rule="ntp_configured", dry_run=False
    )
    assert res["closed"] == 1
    assert res["errors"] == []
    assert [t["id"] for t in res["tombstones_deleted"]] == [str(tomb_id)]

    with Session(patched_session) as s:
        assert s.get(ComplianceViolation, tomb_id) is None
        assert s.get(ComplianceViolation, live_id).status == "resolved"

    rows = _audit_rows(patched_session)
    assert len(rows) == 1
    assert rows[0].tool == "close_compliance_violations"
    assert str(tomb_id) in rows[0].args_summary
    assert str(live_id) in rows[0].args_summary


def test_compliance_close_without_tombstone(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        _violation(s, rule="fw_default_deny", host="db01", severity="high", status="open")
        _violation(s, rule="fw_default_deny", host="db02", severity="low", status="open")
        s.commit()
    res = mcp_server.close_compliance_violations("wont_fix", severity="high", dry_run=False)
    assert res["closed"] == 1
    assert res["tombstones_deleted"] == []
    with Session(patched_session) as s:
        by_host = {v.host: v.status for v in s.scalars(select(ComplianceViolation)).all()}
    assert by_host == {"db01": "resolved", "db02": "open"}


def test_compliance_per_row_failure_is_isolated_and_reported(patched_session, mutations_enabled):
    """One row failing must not lose the batch, and must never be silent."""
    with Session(patched_session) as s:
        good = _violation(s, rule="r_ok", host="h1", status="open")
        bad = _violation(s, rule="r_bad", host="h2", status="open")
        s.commit()
        good_id, bad_id = good.id, bad.id

    real_flush = Session.flush

    def flaky_flush(self, *a, **kw):
        # blow up only while the r_bad row is being flipped
        dirty = [o for o in self.dirty if isinstance(o, ComplianceViolation)]
        if any(o.id == bad_id and o.status == "resolved" for o in dirty):
            raise RuntimeError("simulated concurrent collision")
        return real_flush(self, *a, **kw)

    with patch.object(Session, "flush", flaky_flush):
        res = mcp_server.close_compliance_violations("never_valid", rule_prefix="r_", dry_run=False)

    assert res["closed"] == 1
    assert [e["id"] for e in res["errors"]] == [str(bad_id)]
    with Session(patched_session) as s:
        assert s.get(ComplianceViolation, good_id).status == "resolved"
        assert s.get(ComplianceViolation, bad_id).status == "open"
    assert "simulated concurrent collision" in _audit_rows(patched_session)[0].args_summary


def test_failed_row_does_not_report_its_rolled_back_tombstone_as_deleted(
    patched_session, mutations_enabled
):
    """A failed row's tombstone DELETE is rolled back — so it must not be claimed.

    The savepoint that isolates the failing flip also undoes the DELETE that ran
    inside it. Reporting that tombstone in ``tombstones_deleted`` (in the response
    OR the audit row) would be a false claim that a row which still exists is
    gone — exactly the kind of thing an operator would later trust when
    reconstructing what a batch did.
    """
    with Session(patched_session) as s:
        good = _violation(s, rule="r_ok", host="h1", status="open", detected_at=NOW)
        good_tomb = _violation(s, rule="r_ok", host="h1", status="resolved")
        bad = _violation(
            s, rule="r_bad", host="h2", status="open", detected_at=NOW + timedelta(minutes=1)
        )
        bad_tomb = _violation(s, rule="r_bad", host="h2", status="resolved")
        s.commit()
        good_id, good_tomb_id = good.id, good_tomb.id
        bad_id, bad_tomb_id = bad.id, bad_tomb.id

    real_flush = Session.flush

    def flaky_flush(self, *a, **kw):
        # Fail only the FLIP of the bad row — after its tombstone DELETE has
        # already been staged and flushed inside the same savepoint.
        dirty = [o for o in self.dirty if isinstance(o, ComplianceViolation)]
        if any(o.id == bad_id and o.status == "resolved" for o in dirty):
            raise RuntimeError("simulated concurrent collision")
        return real_flush(self, *a, **kw)

    with patch.object(Session, "flush", flaky_flush):
        res = mcp_server.close_compliance_violations("never_valid", rule_prefix="r_", dry_run=False)

    # The good row closed and its tombstone really is gone.
    assert res["closed"] == 1
    assert [t["id"] for t in res["tombstones_deleted"]] == [str(good_tomb_id)]
    assert [e["id"] for e in res["errors"]] == [str(bad_id)]

    with Session(patched_session) as s:
        assert s.get(ComplianceViolation, good_id).status == "resolved"
        assert s.get(ComplianceViolation, good_tomb_id) is None
        assert s.get(ComplianceViolation, bad_id).status == "open"
        # THE POINT: the failed row's tombstone survived the rollback.
        assert s.get(ComplianceViolation, bad_tomb_id) is not None

    # ...and is absent from the audit row's tombstones_deleted, not merely from
    # the response.
    args = _audit_rows(patched_session)[0].args_summary
    assert str(good_tomb_id) in args
    assert str(bad_tomb_id) not in args


def test_per_row_error_string_is_clamped(patched_session, mutations_enabled):
    """Per-row errors carry a bounded type-prefixed string, not a raw driver dump.

    On real PostgreSQL ``str(IntegrityError)`` appends ``[SQL: ...]
    [parameters: ...]`` — the statement plus every bound parameter, i.e. violation
    ``detail``/``host`` text verbatim — into both the tool's return value and the
    audit row. ``_clamp_error`` is what stops that; this pins its shape.
    """
    long_secret = "sk-live-" + "A" * 4000
    with Session(patched_session) as s:
        bad = _violation(s, rule="r_bad", host="h2", status="open")
        s.commit()
        bad_id = bad.id

    real_flush = Session.flush

    def flaky_flush(self, *a, **kw):
        dirty = [o for o in self.dirty if isinstance(o, ComplianceViolation)]
        if any(o.id == bad_id and o.status == "resolved" for o in dirty):
            raise RuntimeError(f"boom [SQL: UPDATE ...] [parameters: {long_secret}]")
        return real_flush(self, *a, **kw)

    with patch.object(Session, "flush", flaky_flush):
        res = mcp_server.close_compliance_violations("never_valid", rule_prefix="r_", dry_run=False)

    (err,) = res["errors"]
    assert err["error"].startswith("RuntimeError: ")
    assert len(err["error"]) <= len("RuntimeError: ") + 200
    assert long_secret not in err["error"]
    assert long_secret not in _audit_rows(patched_session)[0].args_summary


def test_clamp_error_names_the_type_and_bounds_the_message():
    clamped = mcp_server._clamp_error(ValueError("x" * 1000))
    assert clamped.startswith("ValueError: ")
    assert len(clamped) == len("ValueError: ") + 200


# ---------------------------------------------------------------------------
# Real-PostgreSQL integration: a genuine mid-batch IntegrityError
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_engine():
    """Real Postgres, or skip. Creates tables if absent; never drops the schema.

    Deliberately NOT ``drop_all()`` (unlike test_dashboard_sql_columns.py's
    fixture): this file may share ``TEST_DATABASE_URL`` with other runs, and the
    test below cleans up only the rows it created.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("set TEST_DATABASE_URL=postgresql://... to run the Postgres closure test")
    eng = create_engine(url)
    Base.metadata.create_all(eng)
    yield eng
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM compliance_violations WHERE rule LIKE 'pgtest\\_%'"))
        conn.execute(text("DELETE FROM agent_action_log WHERE agent = 'manual_mcp'"))
    eng.dispose()


def test_real_integrity_error_mid_batch_is_isolated_and_reported(pg_engine, mutations_enabled):
    """Provoke a REAL uq_compliance_rule_host_status violation mid-batch.

    A concurrent committed writer inserts a ``resolved`` row for the second
    selected row's (rule, host) AFTER the tool computed its tombstone set — so
    that row is not in ``tombstone_ids``, is not deleted, and the flip collides
    for real inside psycopg. Asserts the four properties that matter: good rows
    in the same batch still commit, the bad row keeps its original status, the
    colliding row is not destroyed, and neither the response nor the audit row
    claims a deletion that did not happen or leaks the driver's SQL/parameters.
    """

    @contextlib.contextmanager
    def _get_session():
        with Session(pg_engine) as s:
            yield s

    with Session(pg_engine) as s:
        good = _violation(s, rule="pgtest_ok", host="pg_h1", status="open", detected_at=NOW)
        good_tomb = _violation(s, rule="pgtest_ok", host="pg_h1", status="resolved")
        bad = _violation(
            s,
            rule="pgtest_bad",
            host="pg_h2",
            status="open",
            detected_at=NOW + timedelta(minutes=1),
            detail="secret-token-do-not-leak",
        )
        s.commit()
        good_id, good_tomb_id, bad_id = good.id, good_tomb.id, bad.id

    real_begin_nested = Session.begin_nested
    calls = {"n": 0}
    intruder_id = uuid.uuid4()

    def hooked_begin_nested(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:  # about to process the second (bad) row
            with pg_engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO compliance_violations "
                        "(id, rule, severity, host, detail, status, detected_at) VALUES "
                        "(:id, 'pgtest_bad', 'medium', 'pg_h2', '', 'resolved', :ts)"
                    ),
                    {"id": intruder_id, "ts": NOW},
                )
        return real_begin_nested(self, *a, **kw)

    with (
        patch("infra_brain.mcp_server.get_session", _get_session),
        patch.object(Session, "begin_nested", hooked_begin_nested),
    ):
        res = mcp_server.close_compliance_violations(
            "never_valid", rule_prefix="pgtest_", dry_run=False
        )

    assert res["closed"] == 1
    assert [e["id"] for e in res["errors"]] == [str(bad_id)]
    (err,) = res["errors"]
    assert "IntegrityError" in err["error"]
    assert "[parameters:" not in err["error"]
    assert len(err["error"]) <= 256  # type name + the 200-char clamp

    with Session(pg_engine) as s:
        assert s.get(ComplianceViolation, good_id).status == "resolved"
        assert s.get(ComplianceViolation, good_tomb_id) is None
        assert s.get(ComplianceViolation, bad_id).status == "open"
        # The concurrently inserted colliding row was never deleted.
        assert s.get(ComplianceViolation, intruder_id) is not None

    args = _audit_rows(pg_engine)[0].args_summary
    assert [t["id"] for t in res["tombstones_deleted"]] == [str(good_tomb_id)]
    assert str(intruder_id) not in args
    assert "[parameters:" not in args
    assert "secret-token-do-not-leak" not in args


# ---------------------------------------------------------------------------
# Attribution is server-derived
# ---------------------------------------------------------------------------


def test_attribution_cannot_be_supplied_by_caller(patched_session, mutations_enabled):
    """There is no actor parameter at all, and the recorded actor is the key."""
    import inspect

    for fn in (mcp_server.resolve_drift_events, mcp_server.close_compliance_violations):
        params = set(inspect.signature(fn).parameters)
        assert not params & {
            "resolved_by",
            "closed_by",
            "actor",
            "approver",
            "authored_by",
        }

    with Session(patched_session) as s:
        r = _resource(s)
        _drift(s, r)
        s.commit()
    with patch.object(mcp_server, "_caller_identity", return_value="mcp:real-key"):
        res = mcp_server.resolve_drift_events("fixed", domain="linux", dry_run=False)
    assert res["resolved_by"] == "mcp:real-key"
    assert "mcp:real-key" in _audit_rows(patched_session)[0].args_summary


# ---------------------------------------------------------------------------
# GitLab #166 — batch_id surfacing + server-side auto_continue
#
# The issue asked for "a real bulk mode": a way to clear a whole known-bad
# class without N manual round-trips, and a handle on what each batch did.
# The per-transaction cap deliberately does NOT move (a single 63k-row
# transaction is strictly worse than 128 bounded ones), so the property under
# test is that auto_continue LOOPS the existing unit of work rather than
# widening it — same cap, same predicate, one audit row per iteration.
# ---------------------------------------------------------------------------


def test_execute_returns_batch_id_resolving_to_the_audit_row(patched_session, mutations_enabled):
    """batch_id must name a REAL AgentActionLog row that reconstructs the batch."""
    with Session(patched_session) as s:
        r = _resource(s)
        ids = {str(_drift(s, r).id) for _ in range(3)}
        s.commit()

    res = mcp_server.resolve_drift_events("fixed", domain="linux", dry_run=False)
    assert res["resolved"] == 3
    batch_id = res["batch_id"]

    with Session(patched_session) as s:
        row = s.get(AgentActionLog, uuid.UUID(batch_id))
        assert row is not None
        assert row.tool == "resolve_drift_events"
        assert set(json.loads(row.args_summary)["resolved_ids"]) == ids


def test_batch_id_audit_row_survives_a_pan_shaped_uuid(patched_session, mutations_enabled):
    """TRK-346: the DLP scrub must never rewrite the ids it is recording.

    ``_PAN_RE`` accepts ``-`` as a digit-group separator, and a UUID's first
    three groups are 8+4+4 = 16 hyphen-separated characters. When those happen
    to be all-digits, Luhn-valid and IIN-matching — roughly 1 random uuid4 in
    40,000 — the old ``redact_pans(json.dumps(record))`` rewrote the middle of
    the row id as ``****-REDACTED-****`` inside ``resolved_ids``, so the audit
    row no longer reconstructed its own batch. At 1,200 ids per
    ``auto_continue`` run that is a ~3% chance per run, which is exactly what
    ``test_auto_continue_batch_ids_reconstruct_every_row`` was tripping over.

    The same id is used deterministically here so the regression is a hard
    failure rather than a 1-in-40,000 one.
    """
    pan_shaped = uuid.UUID("52307540-7105-4335-a4c6-9ed9ec3fbf05")
    with Session(patched_session) as s:
        r = _resource(s)
        de = _drift(s, r)
        de.id = pan_shaped
        s.commit()

    res = mcp_server.resolve_drift_events("fixed", domain="linux", dry_run=False)
    assert res["resolved"] == 1

    with Session(patched_session) as s:
        row = s.get(AgentActionLog, uuid.UUID(res["batch_id"]))
        assert row is not None
        payload = json.loads(row.args_summary)
    assert "REDACTED" not in row.args_summary
    assert payload["resolved_ids"] == [str(pan_shaped)]


def test_compliance_execute_returns_batch_id(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        _violation(s, rule="ntp_configured", host="web01")
        s.commit()
    res = mcp_server.close_compliance_violations("fixed", rule="ntp_configured", dry_run=False)
    assert res["closed"] == 1
    with Session(patched_session) as s:
        row = s.get(AgentActionLog, uuid.UUID(res["batch_id"]))
        assert row is not None
        assert row.tool == "close_compliance_violations"
        assert json.loads(row.args_summary)["closed_ids"] == [res["sample"][0]["id"]]


def test_dry_run_has_no_batch_id(patched_session, mutations_enabled):
    """Nothing was written, so there is no audit row to name."""
    with Session(patched_session) as s:
        r = _resource(s)
        _drift(s, r)
        s.commit()
    assert "batch_id" not in mcp_server.resolve_drift_events("fixed", domain="linux")


def test_auto_continue_clears_a_class_larger_than_the_cap(patched_session, mutations_enabled):
    """1,200 rows (> 2x the 500 cap) cleared across multiple audited transactions."""
    total = 1200
    with Session(patched_session) as s:
        r = _resource(s)
        for i in range(total):
            _drift(s, r, detected_at=NOW - timedelta(minutes=total - i))
        s.commit()

    res = mcp_server.resolve_drift_events(
        "never_valid", domain="linux", dry_run=False, auto_continue=True
    )

    assert res["auto_continue"] is True
    assert res["resolved"] == total
    assert res["remaining"] == 0
    assert res["stopped_because"] == "complete"
    # 1200 / 500 -> 3 capped transactions, never one 1200-row transaction.
    assert res["iterations"] == 3
    assert len(res["batch_ids"]) == 3
    assert len(set(res["batch_ids"])) == 3

    with Session(patched_session) as s:
        assert s.query(DriftEvent).filter(DriftEvent.status == "open").count() == 0
        assert s.query(DriftEvent).filter(DriftEvent.status == "resolved").count() == total


def test_auto_continue_batch_ids_reconstruct_every_row(patched_session, mutations_enabled):
    """Each batch id is a real audit row; together they account for all 1,200."""
    total = 1200
    with Session(patched_session) as s:
        r = _resource(s)
        for i in range(total):
            _drift(s, r, detected_at=NOW - timedelta(minutes=total - i))
        s.commit()

    res = mcp_server.resolve_drift_events(
        "never_valid", domain="linux", dry_run=False, auto_continue=True
    )

    seen: set[str] = set()
    with Session(patched_session) as s:
        for bid in res["batch_ids"]:
            row = s.get(AgentActionLog, uuid.UUID(bid))
            assert row is not None, f"batch_id {bid} does not name a real audit row"
            assert row.tool == "resolve_drift_events"
            payload = json.loads(row.args_summary)
            assert payload["to_status"] == "resolved"
            assert len(payload["resolved_ids"]) <= mcp_server._CLOSURE_BATCH_CAP
            seen |= set(payload["resolved_ids"])
        all_ids = {str(i) for i in s.scalars(select(DriftEvent.id)).all()}
    # Instrumented (TRK-346): the historical intermittent failure here was the
    # DLP scrub mangling a PAN-shaped row id inside args_summary, which reads as
    # "an id that is in the audit row but not in the table". Report both
    # directions with the offending values so a future failure is diagnosable
    # from the log alone rather than needing another reproduction hunt.
    assert seen == all_ids, (
        f"audit rows did not reconstruct the batch: "
        f"in_audit_not_in_table={sorted(seen - all_ids)!r} "
        f"in_table_not_in_audit={sorted(all_ids - seen)[:5]!r} "
        f"(cap={mcp_server._CLOSURE_BATCH_CAP}, iterations={res['iterations']}, "
        f"resolved={res['resolved']}, stopped_because={res['stopped_because']})"
    )
    # One audit row per iteration -- not one for the whole loop.
    rows = _audit_rows(patched_session)
    assert len(rows) == res["iterations"], (
        f"expected {res['iterations']} audit rows, got {len(rows)}: "
        f"{[(str(r.id), r.tool, r.ts) for r in rows]!r}"
    )


def test_auto_continue_dry_run_mutates_nothing(patched_session, mutations_enabled):
    """dry_run wins over auto_continue, and must not spin on an unchanging page."""
    total = 1200
    with Session(patched_session) as s:
        r = _resource(s)
        for i in range(total):
            _drift(s, r, detected_at=NOW - timedelta(minutes=total - i))
        s.commit()

    res = mcp_server.resolve_drift_events(
        "never_valid", domain="linux", dry_run=True, auto_continue=True
    )

    assert res["dry_run"] is True
    assert res["matched_total"] == total
    assert res["would_resolve"] == mcp_server._CLOSURE_BATCH_CAP
    assert res["would_resolve_total"] == total
    assert res["projected_iterations"] == 3
    assert "batch_ids" not in res
    with Session(patched_session) as s:
        assert s.query(DriftEvent).filter(DriftEvent.status == "open").count() == total
    assert _audit_rows(patched_session) == []


def test_auto_continue_still_refuses_an_unscoped_call(patched_session, mutations_enabled):
    """auto_continue must not become a way around the mandatory predicate."""
    with Session(patched_session) as s:
        r = _resource(s)
        _drift(s, r)
        s.commit()

    for kwargs in ({}, {"from_status": "open"}, {"event_ids": []}):
        res = mcp_server.resolve_drift_events(
            "never_valid", dry_run=False, auto_continue=True, **kwargs
        )
        assert "narrowing predicate is REQUIRED" in res["error"]

    with Session(patched_session) as s:
        assert s.scalars(select(DriftEvent)).one().status == "open"
    assert _audit_rows(patched_session) == []


def test_auto_continue_stops_at_the_iteration_ceiling(patched_session, mutations_enabled):
    """A loop that cannot finish stops cleanly and says so, rather than spinning."""
    with Session(patched_session) as s:
        r = _resource(s)
        for i in range(30):
            _drift(s, r, detected_at=NOW - timedelta(minutes=30 - i))
        s.commit()

    with patch.object(mcp_server, "_AUTO_CONTINUE_MAX_ITERATIONS", 2):
        res = mcp_server.resolve_drift_events(
            "never_valid", domain="linux", dry_run=False, auto_continue=True, limit=5
        )

    assert res["iterations"] == 2
    assert res["resolved"] == 10
    assert res["remaining"] == 20
    assert res["stopped_because"] == "iteration_ceiling"
    assert len(res["batch_ids"]) == 2
