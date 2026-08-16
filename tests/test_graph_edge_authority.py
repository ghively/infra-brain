"""Authority model + rejection semantics for knowledge-graph identity edges.

Covers KG-1 (an automatic pass overwriting a human-confirmed edge, destroying
approver attribution and making the edge permanently unretractable) and KG-3
(asymmetric rejection: a NO permanently silenced future questions about the
node yet did nothing to stop a later auto-merge of the very pair rejected).

The load-bearing case is
:func:`test_confirmed_edge_survives_a_later_resolve_entities_pass` — an
operator confirms, the 2-hourly ``graph_maintenance`` pass re-runs
``resolve_entities`` over the same pair, and afterwards the edge must STILL be
human-authored, STILL carry the approver, and STILL be retractable.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain import graph_phase2, graph_phase3
from infra_brain.db.models import (
    GraphEdge,
    GraphEdgeMethod,
    GraphEdgeType,
    GraphNodeType,
    ProposedAction,
)

from tests.support.pg import make_engine


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        yield s
        s.rollback()


def _node(session, node_type, key, name, source="test", resource_id=None, attributes=None):
    return graph_phase2.upsert_node(
        session,
        node_type=node_type,
        natural_key=key,
        name=name,
        source=source,
        resource_id=resource_id,
        attributes=attributes,
    )


def _edges(session, edge_type=GraphEdgeType.SAME_AS, active_only=True):
    stmt = select(GraphEdge).where(GraphEdge.edge_type == edge_type.value)
    if active_only:
        stmt = stmt.where(GraphEdge.valid_to.is_(None))
    return session.execute(stmt).scalars().all()


def _offer_for_pair(session, node_a, node_b):
    """The candidate payload offering this PAIR in any pending row, or None.

    Direction-agnostic on purpose: which of the two ends up as a question's
    source node depends on ``resolve_entities``' ambiguous-group anchor, which
    is not a behaviour worth pinning.
    """
    wanted = {str(node_a.id), str(node_b.id)}
    for row in _queue(session):
        if row.status != "pending":
            continue
        src = (row.payload or {}).get("source_node", {}).get("node_id")
        for cand in (row.payload or {}).get("candidate_matches", []):
            if {src, cand.get("node_id")} == wanted:
                return cand
    return None


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


# ---------------------------------------------------------------------------
# KG-1 — a human edge outranks every automatic emitter
# ---------------------------------------------------------------------------


def test_confirmed_edge_survives_a_later_resolve_entities_pass(session):
    """THE regression test for KG-1.

    ``web01`` (vSphere) and ``web01`` (Rapid7) normalize to the same key, so
    ``resolve_entities``' deterministic pass would emit an auto SAME_AS for
    exactly this pair. An operator has already confirmed it. After the pass the
    edge must be untouched: still ``declared``/1.000, still carrying the
    approver, still human authority — and crucially still RETRACTABLE, which on
    the pre-fix code it was not (``retract_same_as`` filtered on the confirm
    emitter's source string, which the auto pass had overwritten).
    """
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")

    confirmed = graph_phase3.confirm_same_as(session, vm.id, r7a.id, "operator")
    assert confirmed.get("confirmed") is True

    counts = graph_phase3.resolve_entities(session, materialize=False)

    edges = _edges(session)
    assert len(edges) == 2, "the confirmed pair must still be exactly one active pair"
    for edge in edges:
        # Behaviour first: the human assertion itself must be intact.
        assert edge.method == GraphEdgeMethod.DECLARED.value
        assert edge.confidence == Decimal("1.000")
        assert (edge.evidence or {}).get("approver") == "operator"
        assert (edge.evidence or {}).get("basis") == "human_confirmation"
        assert edge.authority == "human"

    # The automatic pass must report that it stood down, not silently no-op.
    assert counts["human_confirmed_skipped"] >= 1
    assert counts["deterministic_edges"] == 0

    # And the whole point of provenance: it is still undoable.
    retracted = graph_phase3.retract_same_as(session, vm.id, r7a.id, "operator-again")
    assert retracted.get("retracted") is True, retracted
    assert all(e.valid_to is not None for e in _edges(session, active_only=False))


def test_repeated_maintenance_passes_never_erode_the_human_edge(session):
    """graph_maintenance re-runs resolve_entities every 2h — N passes, same result."""
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    graph_phase3.confirm_same_as(session, vm.id, r7a.id, "operator")

    for _ in range(3):
        graph_phase3.resolve_entities(session, materialize=False)

    edges = _edges(session)
    assert len(edges) == 2
    assert {e.source for e in edges} == {graph_phase3.EMITTER_SAME_AS_CONFIRMED}
    assert all((e.evidence or {}).get("approver") == "operator" for e in edges)


def test_confirming_over_an_auto_edge_retires_it_and_inserts_a_human_row(session):
    """W3 escalation: an authority change is a NEW assertion, not a mutation."""
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    graph_phase3.resolve_entities(session, materialize=False)
    auto = _edges(session)
    assert len(auto) == 2
    auto_ids = {e.id for e in auto}
    assert all(e.authority == "auto" for e in auto)

    graph_phase3.confirm_same_as(session, vm.id, r7a.id, "operator")

    active = _edges(session)
    assert len(active) == 2
    assert {e.id for e in active}.isdisjoint(auto_ids), (
        "the auto row must be retired and a new human row inserted, not mutated in place"
    )
    assert all(e.authority == "human" for e in active)

    everything = _edges(session, active_only=False)
    assert len(everything) == 4, "history must show both the machine and the human claim"
    retired = [e for e in everything if e.valid_to is not None]
    assert {e.id for e in retired} == auto_ids
    assert all(e.authority == "auto" for e in retired)
    assert all(e.method == GraphEdgeMethod.DETERMINISTIC_MATCH.value for e in retired)


def test_auto_writer_declines_to_overwrite_a_human_edge(session):
    """W4 backstop at the single write choke point — prevent and log, never raise."""
    a = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "alpha")
    b = _node(session, GraphNodeType.R7_ASSET, "1", "bravo")
    human = graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=GraphEdgeType.SAME_AS,
        method=GraphEdgeMethod.DECLARED,
        confidence=Decimal("1.000"),
        source="some.future.confirm.path",
        evidence={"basis": "human_confirmation", "approver": "operator"},
        authority="human",
    )

    returned = graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=GraphEdgeType.SAME_AS,
        method=GraphEdgeMethod.PROBABILISTIC_MATCH,
        confidence=Decimal("0.800"),
        source=graph_phase3.EMITTER_SAME_AS,
        evidence={"basis": "fuzzy_hostname_similarity"},
    )

    assert returned.id == human.id
    assert returned.method == GraphEdgeMethod.DECLARED.value
    assert returned.confidence == Decimal("1.000")
    assert returned.source == "some.future.confirm.path"
    assert (returned.evidence or {}).get("approver") == "operator"
    assert len(_edges(session)) == 1


def test_same_authority_write_still_refreshes_in_place(session):
    """W2 unchanged: re-observation by the same authority stays cheap."""
    a = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "alpha")
    b = _node(session, GraphNodeType.R7_ASSET, "1", "bravo")
    first = graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=GraphEdgeType.SAME_AS,
        method=GraphEdgeMethod.PROBABILISTIC_MATCH,
        confidence=Decimal("0.800"),
        source=graph_phase3.EMITTER_SAME_AS,
    )
    valid_from = first.valid_from
    second = graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=GraphEdgeType.SAME_AS,
        method=GraphEdgeMethod.DETERMINISTIC_MATCH,
        confidence=Decimal("0.990"),
        source=graph_phase3.EMITTER_SAME_AS,
    )
    assert second.id == first.id
    assert second.valid_from == valid_from
    assert second.method == GraphEdgeMethod.DETERMINISTIC_MATCH.value
    assert len(_edges(session, active_only=False)) == 1


def test_not_same_as_may_never_be_written_by_an_automatic_writer(session):
    """W5: negative identity assertions are human-only."""
    a = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "alpha")
    b = _node(session, GraphNodeType.R7_ASSET, "1", "bravo")
    with pytest.raises(ValueError, match="NOT_SAME_AS"):
        graph_phase2.upsert_edge(
            session,
            source_id=a.id,
            target_id=b.id,
            edge_type=GraphEdgeType.NOT_SAME_AS,
            method=GraphEdgeMethod.DECLARED,
            confidence=Decimal("1.000"),
            source="somewhere.automatic",
        )


def test_retract_accepts_any_human_authority_edge_not_one_emitter_string(session):
    """KG-1's brittleness class: a second human path must be retractable too."""
    a = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "alpha")
    b = _node(session, GraphNodeType.R7_ASSET, "1", "bravo")
    for src, tgt in ((a, b), (b, a)):
        graph_phase2.upsert_edge(
            session,
            source_id=src.id,
            target_id=tgt.id,
            edge_type=GraphEdgeType.SAME_AS,
            method=GraphEdgeMethod.DECLARED,
            confidence=Decimal("1.000"),
            source="mcp.bulk_confirm",  # NOT EMITTER_SAME_AS_CONFIRMED
            evidence={"basis": "human_confirmation", "approver": "operator"},
            authority="human",
        )

    result = graph_phase3.retract_same_as(session, a.id, b.id, "operator")

    assert result.get("retracted") is True, result


def test_retract_still_refuses_a_machine_emitted_edge(session):
    """Unchanged guarantee: an auto edge is not retractable (the pass re-emits it)."""
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    graph_phase3.resolve_entities(session, materialize=False)

    result = graph_phase3.retract_same_as(session, vm.id, r7a.id, "operator")

    assert "error" in result
    assert all(e.valid_to is None for e in _edges(session))


# ---------------------------------------------------------------------------
# KG-3 — rejection is a pair-scoped, attributed, emission-blocking veto
# ---------------------------------------------------------------------------


def _queue_a_question(session):
    """A vSphere VM with two tied Rapid7 candidates, queued for a human.

    Built through ``queue_for_review`` rather than by letting
    ``resolve_entities`` pick the ambiguous group's anchor: the anchor is
    ``group[0]``, i.e. whichever row the node select happens to return first,
    so which node ends up as the question's SOURCE is not a property these
    tests should depend on. The shape is identical to what pass 1 queues.
    """
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    r7b = _node(session, GraphNodeType.R7_ASSET, "2", "web01")
    candidates = [
        graph_phase3._candidate_payload(
            other,
            1.0,
            "exact normalized-name match on 'web01', but the key is ambiguous",
            confidence_band="exact_ambiguous",
        )
        for other in (r7a, r7b)
    ]
    action = graph_phase3.queue_for_review(session, vm, candidates)
    assert action is not None and action.status == "pending"
    return vm, r7a, r7b, action


def test_rejection_writes_an_attributed_pair_scoped_veto(session):
    vm, r7a, _r7b, action = _queue_a_question(session)

    result = graph_phase3.reject_same_as(
        session, action.id, "operator", target_node_id=r7a.id, reason="different racks"
    )

    assert result.get("rejected") is True, result
    vetoes = _edges(session, edge_type=GraphEdgeType.NOT_SAME_AS)
    assert len(vetoes) == 2, "the veto is symmetric, like SAME_AS"
    assert {(e.source_id, e.target_id) for e in vetoes} == {
        (vm.id, r7a.id),
        (r7a.id, vm.id),
    }
    for edge in vetoes:
        assert edge.authority == "human"
        assert edge.method == GraphEdgeMethod.DECLARED.value
        assert edge.confidence == Decimal("1.000")
        assert (edge.evidence or {}).get("basis") == "human_rejection"
        assert (edge.evidence or {}).get("rejector") == "operator"
        assert (edge.evidence or {}).get("reason") == "different racks"
        assert (edge.evidence or {}).get("rejected_evidence_class") == "exact_name"


@pytest.mark.parametrize("rejector", ["", "   "])
def test_rejection_refuses_a_blank_rejector(session, rejector):
    _vm, r7a, _r7b, action = _queue_a_question(session)
    result = graph_phase3.reject_same_as(session, action.id, rejector, target_node_id=r7a.id)
    assert "error" in result
    assert _edges(session, edge_type=GraphEdgeType.NOT_SAME_AS) == []


def test_a_rejected_pair_is_never_auto_emitted_afterwards(session):
    """THE KG-3 asymmetry: today a NO blocks questions but not merges."""
    vm, r7a, r7b, action = _queue_a_question(session)
    graph_phase3.reject_same_as(session, action.id, "operator")

    # The data shifts: the OTHER ambiguous claimant disappears, so the pair the
    # human rejected is now a clean, unambiguous deterministic match.
    session.delete(r7b)
    session.flush()

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["human_vetoed_skipped"] >= 1
    assert counts["deterministic_edges"] == 0
    assert _edges(session, edge_type=GraphEdgeType.SAME_AS) == [], (
        "a human NO must block emission of that pair, not merely re-asking"
    )
    # The pair that still HAS both endpoints keeps its symmetric veto. The r7b
    # legs are deliberately not counted: graph_edges.source_id/target_id are
    # ON DELETE CASCADE into graph_nodes, so ``session.delete(r7b)`` above
    # removes r7b's two veto rows on PostgreSQL (and in production). SQLite
    # does not enforce foreign keys, which is the only reason this used to
    # read `== 4`.
    vetoes = {
        (e.source_id, e.target_id) for e in _edges(session, edge_type=GraphEdgeType.NOT_SAME_AS)
    }
    assert {(vm.id, r7a.id), (r7a.id, vm.id)} <= vetoes
    assert vm is not None and r7a is not None


def test_rejection_does_not_silence_the_node(session):
    """The other half of KG-3: the node stays askable about DIFFERENT candidates."""
    vm, r7a, r7b, action = _queue_a_question(session)
    graph_phase3.reject_same_as(session, action.id, "operator")
    assert session.get(ProposedAction, action.id).status == "rejected"

    # Both rejected candidates go away; two NEW tied candidates appear from a
    # source the operator was never asked about.
    session.delete(r7a)
    session.delete(r7b)
    _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "web01")
    _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-2", "web01")
    session.flush()

    graph_phase3.resolve_entities(session, materialize=False)

    pending = [r for r in _queue(session) if r.status == "pending"]
    involved = [
        r
        for r in pending
        if (r.payload or {}).get("source_node", {}).get("node_id") == str(vm.id)
        or str(vm.id) in {c["node_id"] for c in (r.payload or {}).get("candidate_matches", [])}
    ]
    assert involved, "a rejection must not permanently silence the node"


def test_stronger_evidence_requeues_a_rejected_pair_but_still_emits_nothing(session):
    """The evidence-class ladder: hard_identifier outranks exact_name."""
    vm, r7a, r7b, action = _queue_a_question(session)
    graph_phase3.reject_same_as(session, action.id, "operator")
    session.delete(r7b)
    session.flush()

    # New, strictly stronger evidence: the two now share a vSphere instance uuid.
    vm.attributes = {"uuid": "42000000-0000-0000-0000-000000000001"}
    r7a.attributes = {"uuid": "42000000-0000-0000-0000-000000000001"}
    session.flush()

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert _edges(session, edge_type=GraphEdgeType.SAME_AS) == [], (
        "stronger evidence earns a RE-ASK, never an auto-merge over a human NO"
    )
    assert counts["veto_requeued"] >= 1
    offer = _offer_for_pair(session, vm, r7a)
    assert offer, "the vetoed pair must be re-offered under stronger evidence"
    prev = offer.get("previously_rejected")
    assert prev, "the reviewer must be told they are overruling a colleague"
    assert prev.get("rejector") == "operator"
    assert prev.get("rejected_evidence_class") == "exact_name"
    assert prev.get("new_evidence_class") == "hard_identifier"
    # The veto stays ACTIVE until a human actually answers the re-ask.
    still_vetoed = [
        e
        for e in _edges(session, edge_type=GraphEdgeType.NOT_SAME_AS)
        if {e.source_id, e.target_id} == {vm.id, r7a.id}
    ]
    assert len(still_vetoed) == 2


def test_equal_strength_evidence_does_not_requeue_a_rejected_pair(session):
    vm, r7a, r7b, action = _queue_a_question(session)
    graph_phase3.reject_same_as(session, action.id, "operator")
    session.delete(r7b)
    session.flush()

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert _offer_for_pair(session, vm, r7a) is None
    assert counts["veto_requeued"] == 0


def test_confirming_a_previously_rejected_pair_retires_the_veto(session):
    vm, r7a, r7b, action = _queue_a_question(session)
    graph_phase3.reject_same_as(session, action.id, "operator")
    session.delete(r7b)
    session.flush()

    result = graph_phase3.confirm_same_as(session, vm.id, r7a.id, "sam")

    assert result.get("confirmed") is True, result
    assert result.get("veto_overridden") is True
    pair = {vm.id, r7a.id}
    active_vetoes = [
        e
        for e in _edges(session, edge_type=GraphEdgeType.NOT_SAME_AS)
        if {e.source_id, e.target_id} == pair
    ]
    assert active_vetoes == [], (
        "a pair may never carry an active SAME_AS and NOT_SAME_AS at once"
    )
    retired = [
        e
        for e in _edges(session, edge_type=GraphEdgeType.NOT_SAME_AS, active_only=False)
        if {e.source_id, e.target_id} == pair
    ]
    assert len(retired) == 2 and all(e.valid_to is not None for e in retired)
    assert all((e.evidence or {}).get("overridden_by") == "sam" for e in retired)
    assert len(_edges(session, edge_type=GraphEdgeType.SAME_AS)) == 2


def test_pair_gate_reports_the_blocking_reason(session):
    vm, r7a, _r7b, action = _queue_a_question(session)

    assert graph_phase3.pair_gate(session, vm, r7a) == "pending_review"

    graph_phase3.reject_same_as(session, action.id, "operator", target_node_id=r7a.id)
    assert graph_phase3.pair_gate(session, vm, r7a) == "human_veto"

    graph_phase3.confirm_same_as(session, vm.id, r7a.id, "sam")
    assert graph_phase3.pair_gate(session, vm, r7a) == "human_confirmed"


def test_pending_review_blocks_the_machine_answering_its_own_question(session):
    """A queued question must not be answered by the next pass's own emitter."""
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    r7b = _node(session, GraphNodeType.R7_ASSET, "2", "web01")
    graph_phase3.resolve_entities(session, materialize=False)
    assert _edges(session, edge_type=GraphEdgeType.SAME_AS) == []

    # The ambiguity clears, but the human's question is still open.
    session.delete(r7b)
    session.flush()

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["pending_gated"] >= 1
    assert _edges(session, edge_type=GraphEdgeType.SAME_AS) == []
    assert vm is not None and r7a is not None


def test_veto_never_appears_in_blast_radius(session):
    vm, r7a, _r7b, action = _queue_a_question(session)
    graph_phase3.reject_same_as(session, action.id, "operator", target_node_id=r7a.id)

    out = graph_phase3.blast_radius(session, vm.id, max_hops=2, min_confidence=0.0)

    types = {n.get("edge_type") for n in out.get("neighbors", [])}
    assert GraphEdgeType.NOT_SAME_AS.value not in types
    assert out.get("count") == 0


def test_retract_not_same_as_undoes_a_rejection(session):
    vm, r7a, _r7b, action = _queue_a_question(session)
    graph_phase3.reject_same_as(session, action.id, "operator", target_node_id=r7a.id)

    result = graph_phase3.retract_not_same_as(session, vm.id, r7a.id, "sam", reason="my error")

    assert result.get("retracted") is True, result
    assert _edges(session, edge_type=GraphEdgeType.NOT_SAME_AS) == []
    retired = _edges(session, edge_type=GraphEdgeType.NOT_SAME_AS, active_only=False)
    assert all((e.evidence or {}).get("retracted_by") == "sam" for e in retired)
    # The pair is undecided again — the veto no longer gates it. (The question
    # itself is not re-opened: reject_same_as removed that candidate, and
    # re-offering it is the re-ask ladder's job, not the retraction's.)
    assert graph_phase3.pair_gate(session, vm, r7a) is None


def test_reconciliation_state_surfaces_active_vetoes(session):
    vm, r7a, _r7b, action = _queue_a_question(session)
    graph_phase3.reject_same_as(session, action.id, "operator", target_node_id=r7a.id)

    rows = graph_phase3.get_reconciliation_state(session)

    assert rows, "the rejected row stays visible as history"
    vetoed = [v for r in rows for v in (r.get("active_vetoes") or [])]
    assert str(r7a.id) in {v.get("node_id") for v in vetoed}
    assert vm is not None


def test_rejecting_without_a_target_vetoes_every_listed_candidate(session):
    vm, r7a, r7b, action = _queue_a_question(session)

    result = graph_phase3.reject_same_as(session, action.id, "operator")

    assert result.get("rejected") is True, result
    vetoes = _edges(session, edge_type=GraphEdgeType.NOT_SAME_AS)
    # r7b shares vm's node_type-distinct source, so both were queued candidates.
    vetoed_ids = {e.target_id for e in vetoes if e.source_id == vm.id}
    assert r7a.id in vetoed_ids and r7b.id in vetoed_ids
    assert session.get(ProposedAction, action.id).status == "rejected"


def test_partial_rejection_leaves_the_row_pending(session):
    vm, r7a, r7b, action = _queue_a_question(session)

    graph_phase3.reject_same_as(session, action.id, "operator", target_node_id=r7a.id)

    row = session.get(ProposedAction, action.id)
    assert row.status == "pending", "candidates remain, so the question is still open"
    remaining = {c["node_id"] for c in (row.payload or {}).get("candidate_matches", [])}
    assert remaining == {str(r7b.id)}
    assert vm is not None


def test_reject_refuses_a_target_outside_the_rows_candidates(session):
    _vm, _r7a, _r7b, action = _queue_a_question(session)
    stranger = _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-9", "unrelated")

    result = graph_phase3.reject_same_as(
        session, action.id, "operator", target_node_id=stranger.id
    )

    assert "error" in result
    assert _edges(session, edge_type=GraphEdgeType.NOT_SAME_AS) == []


def test_reject_refuses_an_unknown_action(session):
    result = graph_phase3.reject_same_as(session, uuid.uuid4(), "operator")
    assert "error" in result
