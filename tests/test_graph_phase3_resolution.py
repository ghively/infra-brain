"""Entity-resolution tests for Relationship Graph Phase 3 (GitLab issue #127).

Covers the four behaviours the issue makes non-negotiable:
deterministic match, fuzzy match, ambiguous -> review queue, and
NEVER-SILENT-MERGE (an ambiguous or domain-conflicting pair must not produce
an edge). Plus node materialisation and the confidence-honesty contract.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain import graph_phase2, graph_phase3
from infra_brain.db.models import (
    ZONE_CORPORATE,
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    GraphEdge,
    GraphEdgeMethod,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    IacFile,
    OctopusMachine,
    ProposedAction,
    R7Asset,
    Resource,
    VsphereVm,
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


def _same_as_edges(session):
    return (
        session.execute(
            select(GraphEdge).where(GraphEdge.edge_type == GraphEdgeType.SAME_AS.value)
        )
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


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_score_identical_normalized_names_is_one():
    assert graph_phase3._score_pair("WEB01.corp.example.com.", "web01") == 1.0


def test_score_punctuation_variant_lands_in_auto_emit_band():
    score = graph_phase3._score_pair("web01", "web-01")
    assert score >= graph_phase3.FUZZY_AUTO_EMIT_MIN


def test_score_different_numeric_suffix_is_below_auto_emit():
    """web01 vs web02 are DIFFERENT machines in a naming series — the blended
    metric must not treat a changed numeric token as a near-identical name."""
    score = graph_phase3._score_pair("web01", "web02")
    assert score < graph_phase3.FUZZY_AUTO_EMIT_MIN


def test_score_placeholder_name_is_zero():
    assert graph_phase3._score_pair("localhost", "localhost") == 0.0


def test_tokens_split_on_digit_letter_boundary():
    assert graph_phase3._tokens("web01") == {"web", "01"}
    assert graph_phase3._tokens("web-01") == {"web", "01"}


# ---------------------------------------------------------------------------
# Deterministic pass
# ---------------------------------------------------------------------------


def test_deterministic_exact_normalized_match_emits_both_directions(session):
    a = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "WEB01.corp.example.com")
    b = _node(session, GraphNodeType.R7_ASSET, "555", "web01")

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["deterministic_edges"] == 1
    edges = _same_as_edges(session)
    assert len(edges) == 2  # symmetric: A->B and B->A
    assert {(e.source_id, e.target_id) for e in edges} == {(a.id, b.id), (b.id, a.id)}
    for edge in edges:
        assert edge.method == GraphEdgeMethod.DETERMINISTIC_MATCH.value
        assert edge.confidence == Decimal("0.990")
        assert edge.evidence["basis"] == "normalized_hostname_exact"
        assert edge.evidence["normalized_key"] == "web01"


def test_deterministic_confidence_is_not_one_point_zero(session):
    """A name-derived edge may never claim 1.000 (Phase 2 honesty rule)."""
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "555", "web01")
    graph_phase3.resolve_entities(session, materialize=False)
    for edge in _same_as_edges(session):
        assert edge.confidence < Decimal("1.000")


def test_same_source_type_never_linked(session):
    """Two rows of ONE source sharing a name is within-source dedup, not
    cross-source identity — no SAME_AS, and nothing queued either."""
    _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "2", "web01")
    counts = graph_phase3.resolve_entities(session, materialize=False)
    assert counts["deterministic_edges"] == 0
    assert _same_as_edges(session) == []
    assert _queue(session) == []


def test_cross_domain_conflict_suppressed_and_not_merged(session):
    """TRK-087: same first DNS label, different domains = different machines."""
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01.corp.example.com")
    _node(session, GraphNodeType.R7_ASSET, "555", "web01.dmz.example.org")

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["domain_conflicts_suppressed"] >= 1
    assert counts["deterministic_edges"] == 0
    assert _same_as_edges(session) == []


def test_placeholder_names_are_never_a_merge_key(session):
    """KG-6: two boxes both calling themselves 'localhost' must not merge."""
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "localhost")
    _node(session, GraphNodeType.R7_ASSET, "555", "localhost")
    counts = graph_phase3.resolve_entities(session, materialize=False)
    assert counts["deterministic_edges"] == 0
    assert _same_as_edges(session) == []


def test_resolution_is_idempotent(session):
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "555", "web01")
    graph_phase3.resolve_entities(session, materialize=False)
    graph_phase3.resolve_entities(session, materialize=False)
    assert len(_same_as_edges(session)) == 2
    assert len(_queue(session)) == 0


# ---------------------------------------------------------------------------
# Probabilistic pass
# ---------------------------------------------------------------------------


def test_fuzzy_match_above_threshold_emits_probabilistic_edge(session):
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "web-01")

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["deterministic_edges"] == 0
    assert counts["probabilistic_edges"] == 1
    edges = _same_as_edges(session)
    assert len(edges) == 2
    for edge in edges:
        assert edge.method == GraphEdgeMethod.PROBABILISTIC_MATCH.value
        assert edge.confidence == Decimal("0.800")
        assert edge.evidence["basis"] == "fuzzy_hostname_similarity"
        assert edge.evidence["score"] >= graph_phase3.FUZZY_AUTO_EMIT_MIN


def test_fuzzy_confidence_is_below_deterministic(session):
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "web-01")
    graph_phase3.resolve_entities(session, materialize=False)
    assert all(e.confidence < Decimal("0.990") for e in _same_as_edges(session))


def test_low_similarity_pair_is_dropped_entirely(session):
    """Below the review floor: no edge AND no queue noise."""
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "555", "database-primary")

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["probabilistic_edges"] == 0
    assert counts["review_queued"] == 0
    assert _same_as_edges(session) == []
    assert _queue(session) == []


def test_fuzzy_pass_respects_domain_conflict_guard(session):
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01.corp.example.com")
    _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "web-01.dmz.example.org")
    counts = graph_phase3.resolve_entities(session, materialize=False)
    assert counts["probabilistic_edges"] == 0
    assert _same_as_edges(session) == []


# ---------------------------------------------------------------------------
# Ambiguous -> review queue (never a silent merge)
# ---------------------------------------------------------------------------


def test_ambiguous_key_queues_for_review_and_emits_no_edge(session):
    """One vSphere web01 and TWO Rapid7 web01s: picking either would be a
    coin flip presented as a fact."""
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "2", "web01")

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["ambiguous_keys"] == 1
    assert counts["deterministic_edges"] == 0
    assert _same_as_edges(session) == [], "an ambiguous key must NEVER auto-merge"

    queued = _queue(session)
    assert len(queued) == 1
    assert queued[0].status == "pending"
    assert queued[0].action_type == graph_phase3.REVIEW_ACTION_TYPE
    assert queued[0].agent == graph_phase3.REVIEW_AGENT
    # Only 1, not 2: the anchor is one of the two R7Assets (see
    # test_confirm_same_as_stamps_confirmed_target_node_id_on_the_resolved_row),
    # and its own-type sibling is never offered as a candidate -- confirm_same_as
    # unconditionally refuses two same-node_type nodes, so that sibling could
    # never actually be confirmed. See
    # test_ambiguous_key_candidates_never_include_a_same_node_type_sibling.
    assert len(queued[0].payload["candidate_matches"]) == 1


def test_ambiguous_key_candidates_never_include_a_same_node_type_sibling(session):
    """confirm_same_as unconditionally refuses two nodes of the same
    node_type ('SAME_AS links per-source nodes of DIFFERENT sources'). An
    ambiguous-key row must therefore never offer a same-node_type sibling of
    its anchor as a candidate -- that would queue a question that can never
    actually be confirmed as presented. Same fixture as
    test_confirm_same_as_stamps_confirmed_target_node_id_on_the_resolved_row:
    the ambiguous row ends up anchored on one of the two R7Assets."""
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a1 = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    r7a2 = _node(session, GraphNodeType.R7_ASSET, "2", "web01")

    graph_phase3.resolve_entities(session, materialize=False)

    queued = _queue(session)
    assert len(queued) == 1
    action = queued[0]
    source_node_id = action.payload["source_node"]["node_id"]
    same_type_sibling_id = str(r7a2.id if source_node_id == str(r7a1.id) else r7a1.id)
    candidate_ids = {c["node_id"] for c in action.payload["candidate_matches"]}

    assert same_type_sibling_id not in candidate_ids, (
        "the anchor's own-type sibling must never be an offered candidate -- "
        "confirm_same_as would reject it as 'both nodes are R7Asset'"
    )
    assert str(vm.id) in candidate_ids

    # Every candidate that IS offered must be confirmable as presented.
    for cid in candidate_ids:
        result = graph_phase3.confirm_same_as(
            session, uuid.UUID(source_node_id), uuid.UUID(cid), "operator"
        )
        assert "error" not in result, result


def test_fuzzy_middle_band_queues_for_review_and_emits_no_edge(session):
    """A score inside [FUZZY_REVIEW_MIN, FUZZY_AUTO_EMIT_MIN) is a question."""
    a = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "appserver01")
    b = _node(session, GraphNodeType.R7_ASSET, "555", "appserver-01x")
    score = graph_phase3._score_pair(a.name, b.name)
    assert graph_phase3.FUZZY_REVIEW_MIN <= score < graph_phase3.FUZZY_AUTO_EMIT_MIN, (
        f"fixture no longer lands in the review band (score={score})"
    )

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["review_queued"] == 1
    assert counts["probabilistic_edges"] == 0
    assert _same_as_edges(session) == []
    queued = _queue(session)
    assert len(queued) == 1
    assert queued[0].payload["source_node"]["node_id"] in {str(a.id), str(b.id)}
    assert "ambiguous band" in queued[0].payload["candidate_matches"][0]["reason"]


def test_a_bare_rejected_status_flip_reopens_rather_than_silencing(session):
    """KG-3: a legacy status flip no longer silences the node forever.

    This used to assert the opposite ("an operator's NO must not be re-asked"),
    which is where KG-3 lived: one 'rejected' row silenced every future
    question anchored on that node, permanently, while doing nothing to stop a
    later pass auto-merging the very pair that was rejected. Pair-scoped
    NOT_SAME_AS vetoes carry that meaning now (see
    tests/test_graph_edge_authority.py); a bare status flip carries no pair
    information at all, so the honest response is to re-ask.

    Still exactly ONE row: an answered row is REOPENED in place, preserving the
    prior decision under ``previous_decisions``, rather than accumulating a
    second row per target.
    """
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "2", "web01")
    graph_phase3.resolve_entities(session, materialize=False)
    queued = _queue(session)
    queued[0].status = "rejected"
    session.flush()

    graph_phase3.resolve_entities(session, materialize=False)

    rows = _queue(session)
    assert len(rows) == 1, "the answered row is reopened, never duplicated"
    assert rows[0].status == "pending"
    assert (rows[0].payload or {}).get("previous_decisions"), (
        "a reopened row must not look untouched"
    )


def test_queue_candidates_are_ranked_and_capped(session):
    src = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    cands = [
        {"node_id": str(uuid.uuid4()), "score": s / 100, "reason": "x"} for s in range(50, 70)
    ]
    action = graph_phase3.queue_for_review(session, src, cands)
    stored = action.payload["candidate_matches"]
    assert len(stored) == graph_phase3.MAX_REVIEW_CANDIDATES
    assert stored[0]["score"] > stored[-1]["score"]


def test_queue_for_review_with_no_candidates_is_a_noop(session):
    src = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    assert graph_phase3.queue_for_review(session, src, []) is None
    assert _queue(session) == []


def test_queue_for_review_refreshes_stale_candidates_on_an_existing_pending_row(session):
    """KG-4: a pending review row's candidate_matches must be refreshed on
    every call while it stays pending -- otherwise a reviewer looking at a
    "pending" row sees the day-1 evidence forever, even after the scores/
    evidence a later resolve_entities pass would compute have changed. The
    idempotency contract (no NEW question queued, return value stays None)
    is preserved; only the payload of the existing row is kept current."""
    src = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    stale_cand = _node(session, GraphNodeType.R7_ASSET, "1", "web01-old-candidate")
    first = graph_phase3.queue_for_review(
        session,
        src,
        [graph_phase3._candidate_payload(stale_cand, 0.55, "stale reason")],
    )
    assert first is not None
    assert first.payload["candidate_matches"][0]["node_id"] == str(stale_cand.id)

    fresh_cand = _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "web01-fresh-candidate")
    second = graph_phase3.queue_for_review(
        session,
        src,
        [graph_phase3._candidate_payload(fresh_cand, 0.90, "fresh reason")],
    )

    # Idempotency contract unchanged: no NEW question was queued.
    assert second is None
    assert len(_queue(session)) == 1

    row = _queue(session)[0]
    assert row.payload["candidate_matches"][0]["node_id"] == str(fresh_cand.id), (
        "KG-4: the pending row's candidates went stale -- still showing the "
        "first-ever payload instead of the latest evidence"
    )
    assert row.confidence == pytest.approx(0.90)


def test_queue_for_review_reopens_a_rejected_row_but_preserves_the_decision(session):
    """A settled (rejected) row is REOPENED, with the prior answer preserved.

    REWRITTEN AT INTEGRATION (2026-08-10). This test previously asserted the
    opposite — that a rejected row is never refreshed and stays exactly as the
    operator left it. That was correct before KG-3 and is the behaviour KG-3
    deliberately removed: a single rejection permanently silencing every future
    question about that node is precisely the "permanent false split" this
    finding is about. The two fixes were written in parallel against separate
    copies of this function, so KG-4's test encodes the pre-KG-3 contract.

    The original intent — a human decision must not be silently churned away —
    is NOT dropped, it moved. Two mechanisms now carry it, and this test pins
    the first while ``test_graph_edge_authority.py`` pins the second:

    1. HERE: reopening preserves the prior answer under ``previous_decisions``
       (the same preserve-don't-erase discipline ``retract_same_as`` applies
       with ``retraction_history``). Nothing the operator decided is lost.
    2. UPSTREAM: ``_classify_pair_for_emission`` (graph_phase3.py ~1668) only
       re-asks a vetoed pair when the CURRENT evidence class outranks the class
       the human rejected on — ``_evidence_rank(current) <= _evidence_rank(prior)``
       returns "skip". So a routine re-run does not churn the decision; only
       genuinely stronger evidence does. Calling ``queue_for_review`` directly,
       as this test does, bypasses that gate by construction.
    """
    src = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    cand = _node(session, GraphNodeType.R7_ASSET, "1", "web01-cand")
    graph_phase3.queue_for_review(
        session, src, [graph_phase3._candidate_payload(cand, 0.55, "reason")]
    )
    row = _queue(session)[0]
    row.status = "rejected"
    row.approved_by = "operator@example.com"
    session.flush()

    other_cand = _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "web01-other")
    result = graph_phase3.queue_for_review(
        session, src, [graph_phase3._candidate_payload(other_cand, 0.95, "other reason")]
    )

    assert result is not None, "KG-3: an answered row must be reopened, not left silencing the node"
    row = _queue(session)[0]
    assert row.status == "pending"
    assert row.payload["candidate_matches"][0]["node_id"] == str(other_cand.id)

    history = row.payload.get("previous_decisions") or []
    assert len(history) == 1, "the prior decision must be preserved, never erased"
    assert history[0]["status"] == "rejected"
    assert history[0]["approved_by"] == "operator@example.com", (
        "who decided, and what they decided, must survive the reopen"
    )


def test_ambiguous_key_anchor_is_deterministic_regardless_of_row_fetch_order(session):
    """KG-7: the anchor node picked for an ambiguous-key review row must not
    depend on the order GraphNode rows come back in (Postgres gives no
    ordering guarantee without ORDER BY) -- otherwise the same input can
    queue under a different review `target` on different runs, defeating
    queue_for_review's idempotency and producing duplicate questions."""
    engine_a = make_engine()
    with Session(engine_a) as session_a:
        _node(session_a, GraphNodeType.R7_ASSET, "2", "web01")
        _node(session_a, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        _node(session_a, GraphNodeType.R7_ASSET, "1", "web01")
        graph_phase3.resolve_entities(session_a, materialize=False)
        row_a = _queue(session_a)[0]
        anchor_a = (row_a.payload["source_node"]["node_type"], row_a.payload["source_node"]["natural_key"])

    engine_b = make_engine()
    with Session(engine_b) as session_b:
        # Same three logical nodes, inserted in the REVERSE order.
        _node(session_b, GraphNodeType.R7_ASSET, "1", "web01")
        _node(session_b, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        _node(session_b, GraphNodeType.R7_ASSET, "2", "web01")
        graph_phase3.resolve_entities(session_b, materialize=False)
        row_b = _queue(session_b)[0]
        anchor_b = (row_b.payload["source_node"]["node_type"], row_b.payload["source_node"]["natural_key"])

    assert anchor_a == anchor_b, (
        "KG-7: the ambiguous-key anchor changed depending on row insertion/"
        "fetch order -- it must be picked deterministically"
    )


def test_shared_mac_corroborates_vsphere_and_rapid7_match_despite_different_names(session):
    """KG-5: before the fix, materialize_host_nodes never copied any MAC
    onto a vSphere node's attributes (only uuid/instance_uuid), and never
    copied uuid/instance_uuid onto a Rapid7 node's attributes (only mac) --
    so _first_matching_identifier's per-field intersection was EMPTY for
    every real vSphere<->Rapid7 pair and hard-identifier corroboration
    between these two sources was dead code, despite being their flagship
    documented use case. The vSphere collector already fetches guest.net
    (used today only for all_ips); once its NIC MAC rides along in
    VsphereVm.details["mac_addresses"], materialize_host_nodes must copy it
    onto the node's `mac` attribute so a genuine shared MAC actually
    corroborates a match independent of name similarity."""
    vres = _resource(session, "alpha-vm", "vsphere")
    session.add(
        VsphereVm(
            id=uuid.uuid4(),
            resource_id=vres.id,
            vcenter="vc1",
            moref="vm-9",
            name="alpha-vm",
            details={"mac_addresses": ["AA:BB:CC:DD:EE:FF"]},
        )
    )
    rres = _resource(session, "zulu-asset", "rapid7")
    session.add(
        R7Asset(
            id=uuid.uuid4(),
            resource_id=rres.id,
            r7_asset_id=99,
            hostname="zulu-asset",
            mac="aa:bb:cc:dd:ee:ff",
        )
    )
    session.flush()
    assert (
        graph_phase3._score_pair("alpha-vm", "zulu-asset") < graph_phase3.FUZZY_REVIEW_MIN
    ), "fixture must be a genuine name near-miss so only mac corroboration explains an edge"

    counts = graph_phase3.resolve_entities(session, materialize=True)

    assert counts["probabilistic_edges"] == 1, (
        "KG-5: the shared MAC should have corroborated the match into the "
        "auto-emit band despite the dissimilar names"
    )
    edges = _same_as_edges(session)
    assert len(edges) == 2
    for edge in edges:
        assert edge.evidence["corroborating_identifier"] == "mac"


def test_get_reconciliation_state_shape_and_domain_filter(session):
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", source="vsphere")
    _node(session, GraphNodeType.R7_ASSET, "1", "web01", source="rapid7")
    _node(session, GraphNodeType.R7_ASSET, "2", "web01", source="rapid7")
    graph_phase3.resolve_entities(session, materialize=False)

    rows = graph_phase3.get_reconciliation_state(session)
    assert len(rows) == 1
    assert set(rows[0]) >= {"source_node", "candidate_matches", "status"}
    assert rows[0]["status"] == "pending"

    src_domain = rows[0]["source_node"]["source"]
    assert graph_phase3.get_reconciliation_state(session, domain=src_domain)
    assert graph_phase3.get_reconciliation_state(session, domain="nonexistent") == []


def test_get_reconciliation_state_empty(session):
    assert graph_phase3.get_reconciliation_state(session) == []


# ---------------------------------------------------------------------------
# confirm_same_as — approve path + queue state transition
# ---------------------------------------------------------------------------


def test_confirm_same_as_writes_declared_edge_and_resolves_queue(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "2", "web01")
    graph_phase3.resolve_entities(session, materialize=False)
    assert _queue(session)[0].status == "pending"

    result = graph_phase3.confirm_same_as(session, vm.id, r7a.id, "operator")

    assert result["confirmed"] is True
    assert result["review_resolved"] is True
    assert result["method"] == GraphEdgeMethod.DECLARED.value
    assert result["confidence"] == 1.0

    edges = _same_as_edges(session)
    assert len(edges) == 2
    for edge in edges:
        assert edge.method == GraphEdgeMethod.DECLARED.value
        assert edge.confidence == Decimal("1.000")
        assert edge.evidence["approver"] == "operator"
        assert edge.source == graph_phase3.EMITTER_SAME_AS_CONFIRMED

    action = _queue(session)[0]
    assert action.status == "approved"
    assert action.approved_by == "operator"
    assert action.approved_at is not None


def test_confirm_same_as_without_a_queued_question_still_works(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "alpha")
    oct_ = _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "zulu")

    result = graph_phase3.confirm_same_as(session, vm.id, oct_.id, "operator")

    assert result["confirmed"] is True
    assert result["review_resolved"] is False
    assert len(_same_as_edges(session)) == 2


def test_confirm_same_as_upgrades_an_existing_probabilistic_edge(session):
    """W3 escalation: the machine's claim is RETIRED, the human's is inserted.

    Confirmation used to mutate the auto row in place. It no longer does — an
    authority change is a different assertion, and the bitemporal store's whole
    point is that history shows "machine asserted 0.800 from T1-T2; operator
    declared 1.000 from T2-". Exactly one ACTIVE pair either way.
    """
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    oct_ = _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "web-01")
    graph_phase3.resolve_entities(session, materialize=False)
    assert all(e.method == GraphEdgeMethod.PROBABILISTIC_MATCH.value for e in _same_as_edges(session))

    graph_phase3.confirm_same_as(session, vm.id, oct_.id, "operator")

    edges = [e for e in _same_as_edges(session) if e.valid_to is None]
    assert len(edges) == 2, "confirmation leaves exactly one ACTIVE pair"
    for edge in edges:
        assert edge.method == GraphEdgeMethod.DECLARED.value
        assert edge.confidence == Decimal("1.000")
        assert edge.authority == "human"
    retired = [e for e in _same_as_edges(session) if e.valid_to is not None]
    assert len(retired) == 2
    assert all(e.authority == "auto" for e in retired)


@pytest.mark.parametrize("approver", ["", "   "])
def test_confirm_same_as_rejects_blank_approver(session, approver):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    result = graph_phase3.confirm_same_as(session, vm.id, r7a.id, approver)
    assert "error" in result
    assert _same_as_edges(session) == []


def test_confirm_same_as_rejects_unknown_node(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    result = graph_phase3.confirm_same_as(session, vm.id, uuid.uuid4(), "operator")
    assert "not found" in result["error"]
    assert _same_as_edges(session) == []


def test_confirm_same_as_rejects_self_link(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    result = graph_phase3.confirm_same_as(session, vm.id, vm.id, "operator")
    assert "same node" in result["error"]
    assert _same_as_edges(session) == []


def test_confirm_same_as_rejects_same_source_type(session):
    a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    b = _node(session, GraphNodeType.R7_ASSET, "2", "web01")
    result = graph_phase3.confirm_same_as(session, a.id, b.id, "operator")
    assert "DIFFERENT sources" in result["error"]
    assert _same_as_edges(session) == []


def test_confirm_same_as_stamps_confirmed_target_node_id_on_the_resolved_row(session):
    """Nothing else records WHICH candidate a human picked once the row
    leaves 'pending' -- retract_same_as needs this stamp to find the right
    edge pair later."""
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "2", "web01")
    graph_phase3.resolve_entities(session, materialize=False)

    graph_phase3.confirm_same_as(session, vm.id, r7a.id, "operator")

    # The ambiguous-key row is keyed on r7a (natural_key "1"), not vm -- the
    # R7Asset bucket, not the VsphereVM one, is what's ambiguous here (two R7
    # assets both named "web01"). Its "confirmed_target_node_id" is therefore
    # the OTHER side of the pairing, vm.
    action = _queue(session)[0]
    assert action.payload["confirmed_target_node_id"] == str(vm.id)


# ---------------------------------------------------------------------------
# retract_same_as — undo a confirmed pairing
# ---------------------------------------------------------------------------


def test_retract_same_as_closes_edges_and_reopens_review_queue(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "2", "web01")
    graph_phase3.resolve_entities(session, materialize=False)
    graph_phase3.confirm_same_as(session, vm.id, r7a.id, "operator")
    assert all(e.valid_to is None for e in _same_as_edges(session))
    assert _queue(session)[0].status == "approved"

    result = graph_phase3.retract_same_as(session, vm.id, r7a.id, "operator-again", reason="wrong match")

    assert result["retracted"] is True
    assert result["review_reopened"] is True
    edges = _same_as_edges(session)
    assert len(edges) == 2
    for edge in edges:
        assert edge.valid_to is not None
        assert edge.evidence["retracted_by"] == "operator-again"
        assert edge.evidence["retraction_reason"] == "wrong match"
        # Original declared-approval evidence is preserved, not overwritten.
        assert edge.evidence["approver"] == "operator"

    action = _queue(session)[0]
    assert action.status == "pending"
    assert action.approved_by is None
    assert action.approved_at is None
    assert "confirmed_target_node_id" not in (action.payload or {})


def test_retract_same_as_without_a_queued_question_still_closes_edges(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "alpha")
    oct_ = _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "zulu")
    graph_phase3.confirm_same_as(session, vm.id, oct_.id, "operator")

    result = graph_phase3.retract_same_as(session, vm.id, oct_.id, "operator")

    assert result["retracted"] is True
    assert result["review_reopened"] is False
    assert all(e.valid_to is not None for e in _same_as_edges(session))


@pytest.mark.parametrize("retractor", ["", "   "])
def test_retract_same_as_rejects_blank_retractor(session, retractor):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    graph_phase3.confirm_same_as(session, vm.id, r7a.id, "operator")

    result = graph_phase3.retract_same_as(session, vm.id, r7a.id, retractor)

    assert "error" in result
    assert all(e.valid_to is None for e in _same_as_edges(session))


def test_retract_same_as_rejects_self_link(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    result = graph_phase3.retract_same_as(session, vm.id, vm.id, "operator")
    assert "same node" in result["error"]


def test_retract_same_as_rejects_when_no_active_edge_exists(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    result = graph_phase3.retract_same_as(session, vm.id, r7a.id, "operator")
    assert "no active, human-confirmed SAME_AS edge" in result["error"]


def test_retract_same_as_only_reopens_the_matching_confirmed_row(session):
    """Two review rows for two DIFFERENT source nodes must not cross-wire --
    retracting one pairing must not reopen the other's already-resolved row."""
    vm1 = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    r7a1 = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "1b", "web01")
    vm2 = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-2", "web02")
    r7a2 = _node(session, GraphNodeType.R7_ASSET, "2", "web02")
    _node(session, GraphNodeType.R7_ASSET, "2b", "web02")
    graph_phase3.resolve_entities(session, materialize=False)
    graph_phase3.confirm_same_as(session, vm1.id, r7a1.id, "operator")
    graph_phase3.confirm_same_as(session, vm2.id, r7a2.id, "operator")

    graph_phase3.retract_same_as(session, vm1.id, r7a1.id, "operator")

    # Each ambiguous-key row is keyed on the R7Asset side (r7a1/r7a2), not the
    # VsphereVM side -- see test_confirm_same_as_stamps_confirmed_target_node_id.
    rows_by_target = {a.payload.get("source_node", {}).get("node_id"): a for a in _queue(session)}
    assert rows_by_target[str(r7a1.id)].status == "pending"
    assert rows_by_target[str(r7a2.id)].status == "approved"


# ---------------------------------------------------------------------------
# Node materialisation
# ---------------------------------------------------------------------------


def _resource(session, name, domain):
    r = Resource(
        id=uuid.uuid4(), name=name, domain=domain, type="host", source="test", zone=ZONE_CORPORATE
    )
    session.add(r)
    session.flush()
    return r


def _iac_file(session):
    f = IacFile(
        id=uuid.uuid4(),
        gitlab_project_id=1,
        path="inventory/hosts.yml",
        file_type="ansible_inventory",
        ref="main",
    )
    session.add(f)
    session.flush()
    return f


def test_materialize_creates_ansible_and_octopus_nodes(session):
    group = AnsibleInventoryGroup(
        id=uuid.uuid4(), iac_file_id=_iac_file(session).id, name="webservers"
    )
    session.add(group)
    session.flush()
    session.add_all(
        [
            AnsibleInventoryHost(id=uuid.uuid4(), group_id=group.id, name="web01.corp.example.com"),
            AnsibleInventoryHost(id=uuid.uuid4(), group_id=group.id, name="localhost"),
        ]
    )
    res = _resource(session, "web01", "octopus")
    session.add(
        OctopusMachine(
            id=uuid.uuid4(),
            resource_id=res.id,
            octopus_id="Machines-9",
            name="web01",
            status="Online",
        )
    )
    session.flush()

    counts = graph_phase3.materialize_host_nodes(session)

    assert counts[GraphNodeType.ANSIBLE_MANAGED_HOST.value] == 1, "placeholder host skipped"
    assert counts[GraphNodeType.OCTOPUS_MACHINE.value] == 1
    nodes = session.execute(select(GraphNode)).scalars().all()
    kinds = {n.node_type: n for n in nodes}
    assert kinds[GraphNodeType.ANSIBLE_MANAGED_HOST.value].natural_key == "web01.corp.example.com"
    assert kinds[GraphNodeType.OCTOPUS_MACHINE.value].natural_key == "Machines-9"
    assert kinds[GraphNodeType.OCTOPUS_MACHINE.value].resource_id == res.id


def test_materialize_dedupes_a_host_in_multiple_groups(session):
    iac = _iac_file(session)
    g1 = AnsibleInventoryGroup(id=uuid.uuid4(), iac_file_id=iac.id, name="web")
    g2 = AnsibleInventoryGroup(id=uuid.uuid4(), iac_file_id=iac.id, name="prod")
    session.add_all([g1, g2])
    session.flush()
    session.add_all(
        [
            AnsibleInventoryHost(id=uuid.uuid4(), group_id=g1.id, name="web01"),
            AnsibleInventoryHost(id=uuid.uuid4(), group_id=g2.id, name="web01"),
        ]
    )
    session.flush()

    counts = graph_phase3.materialize_host_nodes(session)

    assert counts[GraphNodeType.ANSIBLE_MANAGED_HOST.value] == 1


def test_materialize_skips_vm_templates(session):
    res = _resource(session, "tmpl", "vsphere")
    session.add(
        VsphereVm(
            id=uuid.uuid4(),
            resource_id=res.id,
            vcenter="vc1",
            moref="vm-1",
            name="golden-template",
            is_template=True,
        )
    )
    session.flush()
    counts = graph_phase3.materialize_host_nodes(session)
    assert counts[GraphNodeType.VSPHERE_VM.value] == 0


def test_end_to_end_resolution_over_real_source_tables(session):
    """The full path: real source rows -> nodes -> a deterministic SAME_AS."""
    vres = _resource(session, "web01", "vsphere")
    session.add(
        VsphereVm(
            id=uuid.uuid4(),
            resource_id=vres.id,
            vcenter="vc1",
            moref="vm-1",
            name="WEB01.corp.example.com",
            is_template=False,
        )
    )
    rres = _resource(session, "web01", "rapid7")
    session.add(
        R7Asset(id=uuid.uuid4(), resource_id=rres.id, r7_asset_id=42, hostname="web01", ip="10.0.0.1")
    )
    session.flush()

    counts = graph_phase3.resolve_entities(session)

    assert counts["nodes_considered"] == 2
    assert counts["deterministic_edges"] == 1
    assert len(_same_as_edges(session)) == 2


def test_resolution_with_fewer_than_two_nodes_is_a_clean_noop(session):
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    counts = graph_phase3.resolve_entities(session, materialize=False)
    assert counts["nodes_considered"] == 1
    assert counts["deterministic_edges"] == 0
    assert _same_as_edges(session) == []


def test_cve_and_datastore_nodes_are_never_resolution_candidates(session):
    _node(session, GraphNodeType.CVE, "CVE-2024-1", "CVE-2024-1")
    _node(session, GraphNodeType.VSPHERE_DATASTORE, "vc1:ds-1", "CVE-2024-1")
    counts = graph_phase3.resolve_entities(session, materialize=False)
    assert counts["nodes_considered"] == 0
    assert _same_as_edges(session) == []


# ---------------------------------------------------------------------------
# GitLab issue #168 — hard-identifier corroboration
# ---------------------------------------------------------------------------


def test_score_candidate_matching_uuid_floors_score_into_auto_emit_band():
    a = graph_phase3.GraphNode(
        node_type=GraphNodeType.VSPHERE_VM.value,
        natural_key="vc1:vm-1",
        name="alpha",
        source="vsphere",
        attributes={"uuid": "abc-123"},
    )
    b = graph_phase3.GraphNode(
        node_type=GraphNodeType.OCTOPUS_MACHINE.value,
        natural_key="Machines-1",
        name="zulu",
        source="octopus",
        attributes={"uuid": "abc-123"},
    )
    score, evidence, counter_evidence = graph_phase3._score_candidate(a, b)
    assert score >= graph_phase3.FUZZY_AUTO_EMIT_MIN
    assert evidence["corroborating_identifier"] == "uuid"
    assert evidence["uuid"] == "abc-123"
    assert counter_evidence == []


def test_uuid_corroboration_auto_emits_despite_different_names(session):
    """(a) two nodes with DIFFERENT names but the SAME vSphere uuid must
    score into the auto-emit band, with the uuid recorded in evidence."""
    _node(
        session,
        GraphNodeType.VSPHERE_VM,
        "vc1:vm-1",
        "alpha-vm",
        source="vsphere",
        attributes={"uuid": "shared-uuid-1"},
    )
    _node(
        session,
        GraphNodeType.OCTOPUS_MACHINE,
        "Machines-1",
        "zulu-machine",
        source="octopus",
        attributes={"uuid": "shared-uuid-1"},
    )
    assert (
        graph_phase3._score_pair("alpha-vm", "zulu-machine") < graph_phase3.FUZZY_REVIEW_MIN
    ), "fixture must be a genuine name near-miss so only uuid corroboration explains an edge"

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["probabilistic_edges"] == 1
    assert counts["review_queued"] == 0
    edges = _same_as_edges(session)
    assert len(edges) == 2
    for edge in edges:
        assert edge.method == GraphEdgeMethod.PROBABILISTIC_MATCH.value
        assert edge.evidence["corroborating_identifier"] == "uuid"
        assert edge.evidence["uuid"] == "shared-uuid-1"


def test_identical_name_conflicting_ip_is_queued_not_auto_emitted(session):
    """(b) two nodes with IDENTICAL names but CONFLICTING IPs must be queued
    for review with populated counter_evidence, never auto-emitted."""
    _node(
        session,
        GraphNodeType.VSPHERE_VM,
        "vc1:vm-1",
        "web01",
        source="vsphere",
        attributes={"ip": "10.0.0.1"},
    )
    _node(
        session,
        GraphNodeType.R7_ASSET,
        "555",
        "web01",
        source="rapid7",
        attributes={"ip": "10.0.0.99"},
    )

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["deterministic_edges"] == 0
    assert _same_as_edges(session) == [], "a conflicting IP must block the auto-merge"
    assert counts["review_queued"] == 1
    queued = _queue(session)
    assert len(queued) == 1
    cand = queued[0].payload["candidate_matches"][0]
    assert cand["counter_evidence"], "the conflicting IP must be recorded"
    assert any("IP" in c for c in cand["counter_evidence"])
    assert cand["confidence_band"] == "fuzzy_review"


def test_mutually_exclusive_candidates_are_cross_referenced(session):
    """(c) one target claimed by two different sources -> both queue rows
    carry mutually_exclusive pointing at each other's review target."""
    # Node-type sort order ("R7Asset" < "VsphereVM") makes the R7Asset side
    # play pass 2's outer ("a"/source) loop role when paired against a
    # VsphereVM -- so the two claimants here must be R7Asset nodes and the
    # shared, doubly-claimed target a VsphereVM node, to land each claimant
    # in its OWN pending_review row (keyed by a.id) rather than both landing
    # as candidates under one shared anchor.
    a1 = _node(session, GraphNodeType.R7_ASSET, "1", "appserver-01a")
    a2 = _node(session, GraphNodeType.R7_ASSET, "2", "appserver-1x")
    shared = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "appserver-01x")

    s1 = graph_phase3._score_pair(a1.name, shared.name)
    s2 = graph_phase3._score_pair(a2.name, shared.name)
    assert graph_phase3.FUZZY_REVIEW_MIN <= s1 < graph_phase3.FUZZY_AUTO_EMIT_MIN
    assert graph_phase3.FUZZY_REVIEW_MIN <= s2 < graph_phase3.FUZZY_AUTO_EMIT_MIN

    counts = graph_phase3.resolve_entities(session, materialize=False)
    assert counts["review_queued"] == 2

    rows = {row.payload["source_node"]["node_id"]: row for row in _queue(session)}
    row1, row2 = rows[str(a1.id)], rows[str(a2.id)]
    cand1 = next(c for c in row1.payload["candidate_matches"] if c["node_id"] == str(shared.id))
    cand2 = next(c for c in row2.payload["candidate_matches"] if c["node_id"] == str(shared.id))
    assert cand1["mutually_exclusive"] == [graph_phase3._review_target(a2)]
    assert cand2["mutually_exclusive"] == [graph_phase3._review_target(a1)]


def test_get_reconciliation_state_returns_confidence_band_on_every_row(session):
    """(d) get_reconciliation_state must return a confidence_band on every
    row, whether it came from the ambiguous-key branch or the fuzzy band."""
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", source="vsphere")
    _node(session, GraphNodeType.R7_ASSET, "1", "web01", source="rapid7")
    _node(session, GraphNodeType.R7_ASSET, "2", "web01", source="rapid7")
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-2", "appserver01", source="vsphere")
    _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-9", "appserver-01x", source="octopus")

    graph_phase3.resolve_entities(session, materialize=False)

    rows = graph_phase3.get_reconciliation_state(session)
    assert len(rows) == 2
    for row in rows:
        assert row["confidence_band"] in {"exact_ambiguous", "fuzzy_review", "corroborated"}
        assert row["candidates_to_disambiguate"] == len(row["candidate_matches"])


def test_ambiguous_key_regression_still_queues_and_emits_no_edge(session):
    """(e) regression: an existing ambiguous-key group still queues and
    still emits no edge after the corroboration fix."""
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    _node(session, GraphNodeType.R7_ASSET, "2", "web01")

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["ambiguous_keys"] == 1
    assert counts["deterministic_edges"] == 0
    assert _same_as_edges(session) == []
    queued = _queue(session)
    assert len(queued) == 1
    for cand in queued[0].payload["candidate_matches"]:
        assert cand["confidence_band"] == "exact_ambiguous"


def test_domain_conflict_pair_still_suppressed_exactly_as_before(session):
    """(e) regression: an existing hosts_domain_conflict pair is still fully
    suppressed -- not merged, not queued -- exactly as before the fix."""
    _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01.corp.example.com")
    _node(session, GraphNodeType.R7_ASSET, "555", "web01.dmz.example.org")

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["domain_conflicts_suppressed"] >= 1
    assert counts["deterministic_edges"] == 0
    assert counts["review_queued"] == 0
    assert _same_as_edges(session) == []
    assert _queue(session) == []


def test_materialize_copies_hard_identifiers_onto_vsphere_and_rapid7_nodes(session):
    """materialize_host_nodes must now copy the vSphere uuid/instance_uuid/
    ip_address/all_ips/guest_hostname and the Rapid7 mac onto attributes."""
    vres = _resource(session, "web01", "vsphere")
    session.add(
        VsphereVm(
            id=uuid.uuid4(),
            resource_id=vres.id,
            vcenter="vc1",
            moref="vm-1",
            name="web01",
            uuid="vm-uuid-1",
            instance_uuid="vm-instance-uuid-1",
            ip_address="10.0.0.1",
            all_ips=["10.0.0.1", "fe80::1"],
            guest_hostname="web01.corp.example.com",
        )
    )
    rres = _resource(session, "web01-r7", "rapid7")
    session.add(
        R7Asset(
            id=uuid.uuid4(),
            resource_id=rres.id,
            r7_asset_id=42,
            hostname="web01-r7",
            mac="AA:BB:CC:DD:EE:FF",
        )
    )
    session.flush()

    graph_phase3.materialize_host_nodes(session)

    nodes = {n.node_type: n for n in session.execute(select(GraphNode)).scalars().all()}
    vm_node = nodes[GraphNodeType.VSPHERE_VM.value]
    assert vm_node.attributes["uuid"] == "vm-uuid-1"
    assert vm_node.attributes["instance_uuid"] == "vm-instance-uuid-1"
    assert vm_node.attributes["ip_address"] == "10.0.0.1"
    assert vm_node.attributes["all_ips"] == ["10.0.0.1", "fe80::1"]
    assert vm_node.attributes["guest_hostname"] == "web01.corp.example.com"
    r7_node = nodes[GraphNodeType.R7_ASSET.value]
    assert r7_node.attributes["mac"] == "AA:BB:CC:DD:EE:FF"


def test_materialize_copies_vsphere_guest_os_onto_attributes(session):
    """_score_candidate's differing-OS counter-evidence check reads
    attrs["os"] on BOTH sides of a pair. Before this fix, R7Asset was the
    ONLY host-shaped node type that ever populated "os" -- VsphereVM's
    attributes never copied guest_full_name into it -- so os_a and os_b could
    never both be truthy for any real cross-source candidate pair and the
    check silently never fired. This asserts the VM side is now populated
    too, matching R7Asset."""
    vres = _resource(session, "web01", "vsphere")
    session.add(
        VsphereVm(
            id=uuid.uuid4(),
            resource_id=vres.id,
            vcenter="vc1",
            moref="vm-1",
            name="web01",
            guest_full_name="Ubuntu Linux (64-bit)",
        )
    )
    session.flush()

    graph_phase3.materialize_host_nodes(session)

    vm_node = session.execute(
        select(GraphNode).where(GraphNode.node_type == GraphNodeType.VSPHERE_VM.value)
    ).scalar_one()
    assert vm_node.attributes["os"] == "Ubuntu Linux (64-bit)"


def test_differing_os_between_vm_and_r7asset_diverts_exact_match_to_review(session):
    """End-to-end regression for the same fix: an otherwise-exact
    normalized-name match between a VsphereVM and an R7Asset whose OS
    genuinely disagrees must be diverted to the review queue, not
    auto-emitted -- this is the real-world case the differing-OS
    counter-evidence check exists to catch (GitLab issue #168), and it was
    silently dead before materialize_host_nodes copied "os" onto VsphereVM
    nodes."""
    vres = _resource(session, "web01", "vsphere")
    session.add(
        VsphereVm(
            id=uuid.uuid4(),
            resource_id=vres.id,
            vcenter="vc1",
            moref="vm-1",
            name="web01",
            guest_full_name="Ubuntu Linux (64-bit)",
        )
    )
    rres = _resource(session, "web01-r7", "rapid7")
    session.add(
        R7Asset(
            id=uuid.uuid4(),
            resource_id=rres.id,
            r7_asset_id=42,
            hostname="web01",
            os="Microsoft Windows Server 2019",
        )
    )
    session.flush()

    counts = graph_phase3.resolve_entities(session)

    assert counts["deterministic_edges"] == 0
    assert _same_as_edges(session) == [], "a genuine OS mismatch must block the auto-merge"
    assert counts["review_queued"] == 1
    cand = _queue(session)[0].payload["candidate_matches"][0]
    assert any("differing OS" in c for c in cand["counter_evidence"])
