"""GitLab #161 — bulk approve/reject for the pending ProposedAction queue.

``get_remediation_suggestions(status="pending")`` returns 7,471 rows and the
existing approve/reject tools are one-at-a-time, so the queue is untriageable
in practice. These tests pin the properties that make a bulk path safe enough
to exist, reusing the Batch L (#144) machinery rather than a parallel one:

* a call with no narrowing predicate is REFUSED and writes nothing — "reject
  everything pending" must not be expressible, because the 7k queue contains
  real proposals alongside the junk;
* ``dry_run=True`` is the default and reports the count without mutating;
* THE ONE THAT MATTERS MOST — ``uq_proposed_action_target_status`` is
  (action_type, target, status), so flipping a pending row to "rejected" can
  collide with an existing rejected tombstone. A plain bulk UPDATE would lose
  the WHOLE batch to one collision; the per-item SAVEPOINT means the colliding
  item alone is skipped (and reported, never overwritten) while the rest commit;
* ``entity_resolution_same_as`` rows are NEVER selectable, even when they match
  every other predicate — the single-row ``approve_action`` already refuses
  them, and the bulk path must not be a loophole around that;
* exactly one ``agent_action_log`` row is written, in the same transaction as
  the mutations, and its id is returned as ``batch_id``.

One test at the bottom runs against REAL PostgreSQL when ``TEST_DATABASE_URL``
is set (same convention as ``tests/test_mcp_batch_closure.py``): pysqlite does
not open a real transaction around ``begin_nested()``, so the
audit-write-failure-rolls-the-batch-back property cannot be observed on sqlite.
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
from infra_brain.action_decisions import MIN_APPROVE_CONFIDENCE
from infra_brain.db.models import AgentActionLog, Base, ProposedAction
from infra_brain.graph_phase3 import REVIEW_ACTION_TYPE

from tests.support.pg import make_engine


NOW = datetime.now(UTC)


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
    """The graph resume is a post-commit side effect, not what these tests pin."""
    with patch("infra_brain.remediation_graph.resume_remediation_action_sync", return_value=False):
        yield


def _action(session, **kw) -> ProposedAction:
    a = ProposedAction(
        id=uuid.uuid4(),
        agent=kw.pop("agent", "RemediationAgent"),
        action_type=kw.pop("action_type", "config_fix"),
        target=kw.pop("target", f"host-{uuid.uuid4().hex[:8]}:risk_score"),
        payload=kw.pop("payload", {"field": "risk_score", "host": "web01"}),
        confidence=kw.pop("confidence", 0.35),
        status=kw.pop("status", "pending"),
        created_at=kw.pop("created_at", NOW),
    )
    session.add(a)
    session.flush()
    return a


def _audit_rows(engine) -> list[AgentActionLog]:
    with Session(engine) as s:
        return list(
            s.scalars(select(AgentActionLog).where(AgentActionLog.agent == "manual_mcp")).all()
        )


def _statuses(engine) -> dict[uuid.UUID, str]:
    with Session(engine) as s:
        return {a.id: a.status for a in s.scalars(select(ProposedAction)).all()}


# ---------------------------------------------------------------------------
# Registration + mutation gate
# ---------------------------------------------------------------------------


def test_bulk_tools_are_registered_as_mutations():
    """Omitting these makes the tools unreachable by every key, bootstrap included."""
    for name in ("bulk_reject_proposals", "bulk_approve_proposals"):
        assert name in mcp_auth.MUTATION_TOOL_NAMES
        assert name not in mcp_auth.READONLY_TOOL_NAMES
        assert name in mcp_auth.ALL_TOOL_NAMES
        assert name in mcp_auth.TOOL_GROUPS["Governance actions"]


def test_bulk_tools_blocked_without_flag(patched_session, monkeypatch):
    monkeypatch.delenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", raising=False)
    assert "disabled" in mcp_server.bulk_reject_proposals(agent="RemediationAgent")["error"]
    assert "disabled" in mcp_server.bulk_approve_proposals(agent="RemediationAgent")["error"]


# ---------------------------------------------------------------------------
# (a) An unscoped call is refused and writes nothing
# ---------------------------------------------------------------------------


def test_unscoped_call_refused_and_writes_nothing(patched_session, mutations_enabled, no_resume):
    with Session(patched_session) as s:
        _action(s)
        _action(s)
        s.commit()
    before = _statuses(patched_session)

    for fn in (mcp_server.bulk_reject_proposals, mcp_server.bulk_approve_proposals):
        res = fn(dry_run=False)
        assert "narrowing predicate is REQUIRED" in res["error"]
        # An empty id list is not a predicate either.
        res = fn(action_ids=[], dry_run=False)
        assert "narrowing predicate is REQUIRED" in res["error"]

    assert _statuses(patched_session) == before
    assert _audit_rows(patched_session) == []


def test_status_alone_is_not_a_predicate(patched_session, mutations_enabled):
    """There is no `status` parameter at all — pending is implicit, never a filter."""
    import inspect

    for fn in (mcp_server.bulk_reject_proposals, mcp_server.bulk_approve_proposals):
        assert "status" not in inspect.signature(fn).parameters


# ---------------------------------------------------------------------------
# (b) dry-run reports the count and mutates nothing
# ---------------------------------------------------------------------------


def test_dry_run_is_the_default_and_mutates_nothing(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        for _ in range(5):
            _action(s)
        # A different class the predicate must not touch.
        _action(s, agent="ComplianceAgent", confidence=0.9, payload={"field": "ntp"})
        s.commit()
    before = _statuses(patched_session)

    res = mcp_server.bulk_reject_proposals(
        agent="RemediationAgent", action_type="config_fix", max_confidence=0.5
    )

    assert res["dry_run"] is True
    assert res["matched_total"] == 5
    assert res["selected"] == 5
    assert res["would_reject"] == 5
    assert res["remaining"] == 0
    assert "batch_id" not in res
    assert _statuses(patched_session) == before
    assert _audit_rows(patched_session) == []


def test_payload_field_narrows_the_predicate(patched_session, mutations_enabled):
    """The JSONB field-level predicate — ->> on PostgreSQL, json_extract on SQLite."""
    with Session(patched_session) as s:
        for _ in range(3):
            _action(s, payload={"field": "risk_score"})
        for _ in range(2):
            _action(s, payload={"field": "vulnerabilities"})
        s.commit()

    res = mcp_server.bulk_reject_proposals(agent="RemediationAgent", payload_field="risk_score")
    assert res["matched_total"] == 3
    res = mcp_server.bulk_reject_proposals(
        agent="RemediationAgent", payload_field="vulnerabilities"
    )
    assert res["matched_total"] == 2


def test_max_confidence_isolates_the_derived_metric_class(patched_session, mutations_enabled):
    """The concrete #161 use case: RemediationAgent config_fix @ 0.35, not 0.8."""
    with Session(patched_session) as s:
        for _ in range(4):
            _action(s, confidence=0.35)
        for _ in range(3):
            _action(s, confidence=0.8, payload={"field": "ntp"})
        s.commit()

    res = mcp_server.bulk_reject_proposals(
        agent="RemediationAgent", action_type="config_fix", max_confidence=0.5
    )
    assert res["matched_total"] == 4


# ---------------------------------------------------------------------------
# (c) THE ONE THAT MATTERS MOST — tombstone collision is skipped, batch commits
# ---------------------------------------------------------------------------


def test_tombstone_collision_skips_one_item_and_the_rest_commit(patched_session, mutations_enabled):
    """uq_proposed_action_target_status must cost ONE item, never the whole batch.

    A plain bulk UPDATE would raise IntegrityError and lose all four flips. The
    per-item SAVEPOINT confines the damage to the colliding row — and because an
    already-rejected proposal is a decision someone already made, that row is
    SKIPPED and reported rather than having its tombstone deleted.
    """
    collide_target = "web01:risk_score"
    with Session(patched_session) as s:
        # The pre-existing tombstone: same (action_type, target), already rejected.
        tombstone = _action(s, target=collide_target, status="rejected")
        # The pending row that will collide with it.
        colliding = _action(s, target=collide_target, created_at=NOW - timedelta(minutes=10))
        clean = [_action(s, created_at=NOW - timedelta(minutes=i)) for i in range(3)]
        s.commit()
        tombstone_id, colliding_id = tombstone.id, colliding.id
        clean_ids = [a.id for a in clean]

    preview = mcp_server.bulk_reject_proposals(
        agent="RemediationAgent", action_type="config_fix", max_confidence=0.5
    )
    # The preview must be honest about the collision BEFORE anything is written.
    assert [c["action_id"] for c in preview["tombstone_collisions"]] == [str(colliding_id)]
    assert preview["tombstone_collisions"][0]["conflicts_with"] == str(tombstone_id)
    assert preview["selected"] == 4
    assert preview["would_reject"] == 3

    res = mcp_server.bulk_reject_proposals(
        agent="RemediationAgent", action_type="config_fix", max_confidence=0.5, dry_run=False
    )

    assert res["rejected"] == 3
    assert [s_["id"] for s_ in res["skipped"]] == [str(colliding_id)]
    # The skip is a real per-item failure, not a pre-filter: the reason names the
    # constraint the SAVEPOINT absorbed.
    assert "uq_proposed_action_target_status" in res["skipped"][0]["reason"].lower() or (
        "IntegrityError" in res["skipped"][0]["reason"]
    )

    statuses = _statuses(patched_session)
    # The three clean rows really committed — the collision did not lose them.
    for aid in clean_ids:
        assert statuses[aid] == "rejected"
    # The colliding row is untouched, still pending, awaiting a human decision.
    assert statuses[colliding_id] == "pending"
    # And the tombstone was NOT deleted (unlike the compliance path).
    assert statuses[tombstone_id] == "rejected"
    with Session(patched_session) as s:
        assert s.get(ProposedAction, tombstone_id) is not None


# ---------------------------------------------------------------------------
# (d) entity_resolution_same_as is never selectable — the loophole test
# ---------------------------------------------------------------------------


def test_review_rows_are_never_selected_even_when_they_match_everything(
    patched_session, mutations_enabled, no_resume
):
    """The hard refusal. approve_action refuses these one at a time; bulk must too.

    The review row here matches the predicate on EVERY other axis — same agent,
    same target prefix, same payload field, same confidence window, same
    creation window — so nothing but the action_type exclusion can keep it out.
    """
    with Session(patched_session) as s:
        review = _action(
            s,
            action_type=REVIEW_ACTION_TYPE,
            target="web01:identity",
            payload={"field": "risk_score"},
            confidence=0.35,
        )
        normal = _action(s, target="web01:risk_score", payload={"field": "risk_score"})
        s.commit()
        review_id, normal_id = review.id, normal.id

    # Every predicate shape that could plausibly sweep it up.
    for kwargs in (
        {"agent": "RemediationAgent"},
        {"target_prefix": "web01"},
        {"payload_field": "risk_score"},
        {"max_confidence": 0.5},
        {"created_after": (NOW - timedelta(days=1)).isoformat()},
        {"action_ids": [str(review_id)]},
    ):
        for fn in (mcp_server.bulk_reject_proposals, mcp_server.bulk_approve_proposals):
            res = fn(dry_run=True, **kwargs)
            ids = {row["id"] for row in res["sample"]}
            assert str(review_id) not in ids, f"review row leaked into {fn.__name__} {kwargs}"

    # Naming the type explicitly is refused outright, with nothing written.
    for fn in (mcp_server.bulk_reject_proposals, mcp_server.bulk_approve_proposals):
        res = fn(action_type=REVIEW_ACTION_TYPE, dry_run=False)
        assert REVIEW_ACTION_TYPE in res["error"]
        assert "cannot be" in res["error"]

    # An id list containing ONLY the review row selects nothing and flips nothing.
    res = mcp_server.bulk_reject_proposals(action_ids=[str(review_id)], dry_run=False)
    assert res["matched_total"] == 0
    assert res["rejected"] == 0

    # Executing the broad predicate still leaves the review row pending.
    mcp_server.bulk_reject_proposals(agent="RemediationAgent", dry_run=False)
    statuses = _statuses(patched_session)
    assert statuses[review_id] == "pending"
    assert statuses[normal_id] == "rejected"
    assert _audit_rows(patched_session) != []


# ---------------------------------------------------------------------------
# (e) exactly one audit row, in the same transaction as the mutations
# ---------------------------------------------------------------------------


def test_one_audit_row_in_the_same_transaction_as_the_flips(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        ids = {str(_action(s).id) for _ in range(4)}
        s.commit()

    res = mcp_server.bulk_reject_proposals(
        agent="RemediationAgent", max_confidence=0.5, dry_run=False
    )

    rows = _audit_rows(patched_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.tool == "bulk_reject_proposals"
    assert str(row.id) == res["batch_id"]

    payload = json.loads(row.args_summary)
    assert payload["action"] == "bulk_reject_proposals"
    assert payload["to_status"] == "rejected"
    assert set(payload["rejected_ids"]) == ids
    assert payload["predicate"]["agent"] == "RemediationAgent"
    assert payload["decided_by"] == mcp_server.DIRECT_INVOCATION_IDENTITY

    with Session(patched_session) as s:
        assert s.get(AgentActionLog, uuid.UUID(res["batch_id"])) is not None


@pytest.fixture
def pg_engine():
    """Real Postgres, or skip. Creates tables if absent; never drops the schema.

    Same convention as ``tests/test_mcp_batch_closure.py``: cleans up only the
    rows it created.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("set TEST_DATABASE_URL=postgresql://... to run the Postgres bulk test")
    eng = create_engine(url)
    Base.metadata.create_all(eng)
    yield eng
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM proposed_actions WHERE target LIKE 'pgtest:%'"))
        conn.execute(text("DELETE FROM agent_action_log WHERE agent = 'manual_mcp'"))
    eng.dispose()


def test_audit_row_rollback_takes_the_flips_with_it(pg_engine, mutations_enabled):
    """Not best-effort: a failed audit write must undo the batch, never orphan it.

    POSTGRES-ONLY, and not for want of trying on sqlite. pysqlite does not open
    a real transaction around ``begin_nested()``, so a savepoint-flushed UPDATE
    SURVIVES an aborted outer session there — reproducible with plain SQLAlchemy
    and no infra-brain code at all. Asserting this property on sqlite would
    therefore assert the driver's bug, not the tool's behaviour.
    """

    @contextlib.contextmanager
    def _get_session():
        with Session(pg_engine) as s:
            yield s

    with Session(pg_engine) as s:
        a = _action(s, target="pgtest:risk_score")
        s.commit()
        aid = a.id

    with patch("infra_brain.mcp_server.get_session", _get_session):
        with patch.object(mcp_server, "_record_closure_audit", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                mcp_server.bulk_reject_proposals(
                    target_prefix="pgtest:", max_confidence=0.5, dry_run=False
                )

    with Session(pg_engine) as s:
        assert s.get(ProposedAction, aid).status == "pending"
        assert (
            s.scalars(select(AgentActionLog).where(AgentActionLog.agent == "manual_mcp")).all()
            == []
        )


# ---------------------------------------------------------------------------
# Approve-specific: the confidence floor and the post-commit resume
# ---------------------------------------------------------------------------


def test_approve_preserves_the_confidence_floor_per_item(
    patched_session, mutations_enabled, no_resume
):
    """The bulk path must NOT approve in batch what the single-row path refuses."""
    with Session(patched_session) as s:
        low = [_action(s, confidence=0.35) for _ in range(3)]
        high = _action(s, confidence=0.9, payload={"field": "ntp"})
        s.commit()
        low_ids = [a.id for a in low]
        high_id = high.id

    preview = mcp_server.bulk_approve_proposals(agent="RemediationAgent")
    assert preview["selected"] == 4
    assert preview["below_confidence_floor"] == 3
    assert preview["would_approve"] == 1

    res = mcp_server.bulk_approve_proposals(agent="RemediationAgent", dry_run=False)
    assert res["approved"] == 1
    assert len(res["skipped"]) == 3
    assert all("Confidence" in s_["reason"] for s_ in res["skipped"])

    statuses = _statuses(patched_session)
    assert statuses[high_id] == "approved"
    for aid in low_ids:
        assert statuses[aid] == "pending"


def test_approve_dry_run_would_approve_does_not_double_subtract_overlap(
    patched_session, mutations_enabled, no_resume
):
    """GitLab #172: a row that is BOTH a tombstone collision AND below the
    confidence floor must only be excluded once from would_approve, not twice.

    len(selected) - len(collisions) - len(below_floor) undercounts (and can go
    negative) whenever the two sets overlap; set-difference on ids does not.
    """
    collide_target = "overlap-host:risk_score"
    with Session(patched_session) as s:
        # Pre-existing approved tombstone at the same (action_type, target) --
        # the approve path collides on status="approved", not "rejected".
        _action(s, target=collide_target, status="approved")
        # This row is in BOTH exclusion sets: it collides with the tombstone
        # above AND sits below the confidence floor (default confidence=0.35).
        both = _action(s, target=collide_target, created_at=NOW - timedelta(minutes=5))
        # A clean row that should be the only one actually approvable.
        clean = _action(s, confidence=0.9, payload={"field": "ntp"})
        s.commit()
        both_id, clean_id = both.id, clean.id

    preview = mcp_server.bulk_approve_proposals(agent="RemediationAgent")
    # selected excludes the tombstone itself (status="rejected", not "pending"),
    # so it's [both, clean] = 2.
    assert preview["selected"] == 2
    assert [c["action_id"] for c in preview["tombstone_collisions"]] == [str(both_id)]
    assert preview["below_confidence_floor"] == 1
    # The bug: `len(selected) - len(collisions) - len(below_floor)` computes
    # `2 - 1 - 1 = 0`, undercounting the one genuinely approvable row (clean)
    # because `both` was subtracted twice. Set-difference on ids gives 1.
    assert preview["would_approve"] == 1

    res = mcp_server.bulk_approve_proposals(agent="RemediationAgent", dry_run=False)
    assert res["approved"] == 1
    statuses = _statuses(patched_session)
    assert statuses[clean_id] == "approved"
    assert statuses[both_id] == "pending"


def test_approve_attribution_is_server_derived(patched_session, mutations_enabled, no_resume):
    """approver_label is a quoted claim; it can never replace the key identity."""
    with Session(patched_session) as s:
        a = _action(s, confidence=0.9)
        s.commit()
        aid = a.id

    with patch.object(mcp_server, "_caller_identity", return_value="mcp:real-key"):
        res = mcp_server.bulk_approve_proposals(
            agent="RemediationAgent", approver_label="operator", dry_run=False
        )

    assert res["approved_by"] == "mcp:real-key (says: operator)"
    with Session(patched_session) as s:
        assert s.get(ProposedAction, aid).approved_by == "mcp:real-key (says: operator)"


def test_approve_resume_failure_is_not_fatal_to_the_batch(patched_session, mutations_enabled):
    """The rows are already committed; a resume failure only defers execution."""
    with Session(patched_session) as s:
        for _ in range(2):
            _action(s, confidence=0.9)
        s.commit()

    with patch(
        "infra_brain.remediation_graph.resume_remediation_action_sync",
        side_effect=RuntimeError("graph unreachable"),
    ):
        res = mcp_server.bulk_approve_proposals(
            agent="RemediationAgent", max_confidence=1.0, dry_run=False
        )

    assert res["approved"] == 2
    assert len(res["resumed"]) == 2
    assert all(r["resumed"] is False for r in res["resumed"])
    assert all(statuses == "approved" for statuses in _statuses(patched_session).values())


# ---------------------------------------------------------------------------
# Cap + determinism, inherited from Batch L
# ---------------------------------------------------------------------------


def test_over_cap_match_is_truncated_oldest_first_not_refused(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        for i in range(12):
            _action(s, created_at=NOW - timedelta(minutes=12 - i))
        s.commit()

    res = mcp_server.bulk_reject_proposals(
        agent="RemediationAgent", max_confidence=0.5, limit=5, dry_run=False
    )
    assert res["rejected"] == 5
    assert res["matched_total"] == 12
    assert res["remaining"] == 7
    assert res["truncated"] is True

    # Repeated calls make monotonic progress rather than re-selecting the same page.
    res = mcp_server.bulk_reject_proposals(
        agent="RemediationAgent", max_confidence=0.5, limit=5, dry_run=False
    )
    assert res["rejected"] == 5
    assert res["remaining"] == 2


def test_limit_cannot_exceed_the_hard_cap(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        _action(s)
        s.commit()
    res = mcp_server.bulk_reject_proposals(agent="RemediationAgent", limit=10_000)
    assert res["cap"] == mcp_server._CLOSURE_BATCH_CAP


def test_target_prefix_matches_literally(patched_session, mutations_enabled):
    """A LIKE metacharacter must narrow, never expand to match-everything."""
    with Session(patched_session) as s:
        _action(s, target="web01:risk_score")
        _action(s, target="db01:risk_score")
        s.commit()

    assert mcp_server.bulk_reject_proposals(target_prefix="web")["matched_total"] == 1
    # "%" is escaped, so it matches a literal percent sign — i.e. nothing here.
    assert mcp_server.bulk_reject_proposals(target_prefix="%")["matched_total"] == 0


def test_min_approve_confidence_is_not_re_declared():
    """The preview's floor must be the same constant approve_action enforces."""
    assert MIN_APPROVE_CONFIDENCE == 0.7
