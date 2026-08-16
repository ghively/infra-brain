"""TRK-341 — the host_reconcile → graph_phase3 return direction (spec §4.4).

`host_reconcile` persists `host_identities.identity_ambiguous_sources`: the
source labels whose leg for a short hostname was the coin-flip survivor of a
same-source collision. Its own emitters already refuse to assert an
`IS_SAME_AS` over such a leg. What was missing is the RETURN direction:
`graph_phase3._score_candidate` scored a candidate pair as if every identity
leg were settled, so persisted ambiguity suppressed emission but never lowered
the score that decides whether the pair looks like a match at all.

The load-bearing assertion here is behavioural, not structural: the SAME pair
of nodes must score STRICTLY LOWER when one of its legs is known-ambiguous
than when every leg is settled, and the pair must stop auto-emitting.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from infra_brain import graph_phase2, graph_phase3
from infra_brain.db.models import (
    GraphEdge,
    GraphEdgeType,
    GraphNodeType,
    HostIdentity,
    ProposedAction,
    Resource,
)

from tests.support.pg import make_engine


@pytest.fixture()
def engine():
    eng = make_engine()
    return eng


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s
        s.rollback()


def _resource(session, name, source):
    r = Resource(id=uuid.uuid4(), domain="vsphere", type="vm", name=name, source=source)
    session.add(r)
    session.flush()
    return r


def _node(session, node_type, key, name, source, resource_id=None, attributes=None):
    return graph_phase2.upsert_node(
        session,
        node_type=node_type,
        natural_key=key,
        name=name,
        source=source,
        resource_id=resource_id,
        attributes=attributes,
    )


def _same_as_edges(session):
    return (
        session.execute(select(GraphEdge).where(GraphEdge.edge_type == GraphEdgeType.SAME_AS.value))
        .scalars()
        .all()
    )


def _queue(session):
    return (
        session.execute(
            select(ProposedAction).where(
                ProposedAction.action_type == graph_phase3.REVIEW_ACTION_TYPE
            )
        )
        .scalars()
        .all()
    )


def _pair(session, *, ambiguous_legs):
    """A vSphere/Rapid7 node pair joined by one `host_identities` row.

    `ambiguous_legs` is written verbatim to `identity_ambiguous_sources`, so
    `None` is the settled control and `["vsphere"]` the unsettled case — the
    two fixtures differ in NOTHING else.
    """
    r_vs = _resource(session, "web01", "vsphere")
    r_r7 = _resource(session, "web-01", "rapid7")
    session.add(
        HostIdentity(
            id=uuid.uuid4(),
            short_hostname="web01",
            vsphere_resource_id=r_vs.id,
            r7_resource_id=r_r7.id,
            identity_ambiguous_sources=ambiguous_legs,
        )
    )
    a = _node(
        session,
        GraphNodeType.VSPHERE_VM,
        "vc1:vm-1",
        "web01",
        source="vsphere",
        resource_id=r_vs.id,
    )
    b = _node(
        session,
        GraphNodeType.R7_ASSET,
        "42",
        "web-01",
        source="rapid7",
        resource_id=r_r7.id,
    )
    session.flush()
    return a, b


def test_schema_derived_leg_labels_match_host_reconcile_source_keys():
    """graph_phase3 derives leg-column -> source-label from the HostIdentity
    schema because it cannot import host_reconcile (which imports FROM it).
    That derivation must stay identical to the agent's own _SOURCE_KEYS, or a
    node's leg silently stops being recognised as unsettled."""
    from infra_brain.agents.host_reconcile import HostReconcileAgent

    assert graph_phase3._HOST_IDENTITY_LEG_COLUMNS == dict(HostReconcileAgent._SOURCE_KEYS)


# ---------------------------------------------------------------------------
# The load-bearing test: an unsettled leg must LOWER the score
# ---------------------------------------------------------------------------


def test_known_ambiguous_leg_scores_strictly_lower_than_a_settled_leg(engine):
    """Same two nodes, same attributes, same names — the ONLY difference is
    whether host_reconcile flagged one leg as unsettled."""
    with Session(engine) as settled:
        a, b = _pair(settled, ambiguous_legs=None)
        settled_score, _ev, settled_counter = graph_phase3._score_candidate(
            a, b, ambiguity=graph_phase3.ambiguous_leg_index(settled)
        )
        settled.rollback()

    with Session(engine) as unsettled:
        a, b = _pair(unsettled, ambiguous_legs=["vsphere"])
        amb_score, _ev, amb_counter = graph_phase3._score_candidate(
            a, b, ambiguity=graph_phase3.ambiguous_leg_index(unsettled)
        )
        unsettled.rollback()

    assert settled_counter == []
    assert amb_score < settled_score, (
        f"an unsettled identity leg must lower the score: "
        f"settled={settled_score} ambiguous={amb_score}"
    )
    assert any("unsettled identity leg for vsphere" in c for c in amb_counter), amb_counter


def test_only_the_unsettled_leg_is_flagged_not_the_settled_one(session):
    a, b = _pair(session, ambiguous_legs=["vsphere"])
    _score, _ev, counter = graph_phase3._score_candidate(
        a, b, ambiguity=graph_phase3.ambiguous_leg_index(session)
    )
    flags = [c for c in counter if "unsettled identity leg" in c]
    assert len(flags) == 1, flags
    assert "vsphere" in flags[0]
    assert "r7" not in flags[0]


def test_penalty_never_demotes_a_pair_out_of_the_review_band(session):
    """Cautious, but never so cautious the human stops being asked."""
    a, b = _pair(session, ambiguous_legs=["vsphere", "r7"])
    score, _ev, counter = graph_phase3._score_candidate(
        a, b, ambiguity=graph_phase3.ambiguous_leg_index(session)
    )
    assert len(([c for c in counter if "unsettled identity leg" in c])) == 2
    assert score >= graph_phase3.FUZZY_REVIEW_MIN


# ---------------------------------------------------------------------------
# End-to-end: the pair stops auto-emitting and becomes a question
# ---------------------------------------------------------------------------


def test_unsettled_leg_diverts_a_would_be_auto_emit_to_review(session):
    """`web01` vs `web-01` is a >= FUZZY_AUTO_EMIT_MIN fuzzy match that emits a
    probabilistic edge today. With one leg unsettled it must not."""
    _pair(session, ambiguous_legs=["vsphere"])

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert _same_as_edges(session) == []
    assert counts["probabilistic_edges"] == 0
    assert counts["deterministic_edges"] == 0
    assert counts["review_queued"] == 1
    assert counts["unsettled_legs_flagged"] >= 1
    rows = _queue(session)
    assert len(rows) == 1
    reasons = [
        c for cand in rows[0].payload["candidate_matches"] for c in cand.get("counter_evidence", [])
    ]
    assert any("unsettled identity leg for vsphere" in r for r in reasons), reasons


def test_settled_legs_still_auto_emit_exactly_as_before(session):
    """The control: identical fixture, no ambiguity flag → unchanged behaviour."""
    _pair(session, ambiguous_legs=None)

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["probabilistic_edges"] == 1
    assert counts["review_queued"] == 0
    assert counts["unsettled_legs_flagged"] == 0
    assert len(_same_as_edges(session)) == 2


def test_a_node_with_no_host_identity_leg_is_not_flagged(session):
    """AnsibleManagedHost nodes carry no `resource_id` at all — absence of a
    leg is not evidence of an unsettled one."""
    a = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-9", "web01", source="vsphere")
    b = _node(session, GraphNodeType.ANSIBLE_MANAGED_HOST, "web-01", "web-01", source="ansible")
    session.flush()
    _score, _ev, counter = graph_phase3._score_candidate(
        a, b, ambiguity=graph_phase3.ambiguous_leg_index(session)
    )
    assert counter == []


# ---------------------------------------------------------------------------
# Fail closed: unknown ambiguity state must never mean "emit freely"
# ---------------------------------------------------------------------------


def test_resolve_entities_fails_closed_when_ambiguity_state_cannot_be_loaded(engine):
    """A real load failure (the table is gone) must abort the pass BEFORE any
    edge is written — never be swallowed into "no ambiguity, emit freely"."""
    with Session(engine) as s:
        _pair(s, ambiguous_legs=None)
        s.commit()

    # The drop must be undone before this test returns. On SQLite each test
    # owns a private in-memory database, so leaking a dropped table was
    # invisible; under PG_GATE_DSN every test shares ONE PostgreSQL and an
    # un-recreated host_identities poisons the whole rest of the session
    # (every later make_engine() reset probe raises UndefinedTable).
    HostIdentity.__table__.drop(engine)
    try:
        with Session(engine) as s:
            # SQLite reports the missing table as OperationalError, PostgreSQL
            # as ProgrammingError(UndefinedTable); DatabaseError is their
            # common parent, so the fail-closed claim is dialect-independent.
            with pytest.raises(DatabaseError):
                graph_phase3.resolve_entities(s, materialize=False)
            s.rollback()

        with Session(engine) as s:
            assert _same_as_edges(s) == []
    finally:
        HostIdentity.__table__.create(engine)
