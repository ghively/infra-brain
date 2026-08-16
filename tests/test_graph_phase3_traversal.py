"""Traversal tests for Relationship Graph Phase 3 (GitLab issue #127).

Covers each traversal path against a real (sqlite) database rather than a mock
session: a node with real neighbours, a node with none, confidence filtering,
hop-distance capping, cycle safety. Single-store since P5: the walks read
``graph_edges`` only (``resource_relationships`` is dropped — see the epitaphs
at the former dual-store sections below).
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from infra_brain import graph_phase2, graph_phase3
from infra_brain.db.models import (
    GraphEdgeAuthority,
    DriftEvent,
    GraphEdgeMethod,
    GraphEdgeType,
    GraphNodeType,
    Resource,
    ZONE_CORPORATE,
)

from tests.support.pg import make_engine


#: The recursive-CTE traversal is the load-bearing SQL in this file, and per
#: CLAUDE.md a green sqlite run is not evidence for Postgres — the dialects
#: differ on recursive-CTE typing and string concatenation. This file used to
#: carry a private ``INFRA_BRAIN_TEST_PG_DSN`` switch that nothing ever set;
#: it now rides the standard ``make_engine`` / ``PG_GATE_DSN`` chooser, so the
#: ``agent-orm-check`` gate runs it on real PostgreSQL on every MR.
@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        yield s
        s.rollback()
    engine.dispose()


def _resource(session, name, domain="vsphere"):
    r = Resource(
        id=uuid.uuid4(), name=name, domain=domain, type="host", source="test", zone=ZONE_CORPORATE
    )
    session.add(r)
    session.flush()
    return r


def _node(session, node_type, key, name, resource_id=None):
    return graph_phase2.upsert_node(
        session,
        node_type=node_type,
        natural_key=key,
        name=name,
        source="test",
        resource_id=resource_id,
    )


def _edge(session, a, b, edge_type, method, confidence):
    return graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=edge_type,
        method=method,
        confidence=Decimal(confidence),
        source="test",
        evidence={"basis": "test fixture"},
    )


def _declared(session, a, b, edge_type=GraphEdgeType.HOSTED_ON):
    return _edge(session, a, b, edge_type, GraphEdgeMethod.DECLARED, "1.000")


# ---------------------------------------------------------------------------
# blast_radius
# ---------------------------------------------------------------------------


def test_blast_radius_unknown_node_returns_error(session):
    result = graph_phase3.blast_radius(session, uuid.uuid4())
    assert "not found" in result["error"]


def test_blast_radius_node_with_no_neighbors(session):
    lonely = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "lonely")
    result = graph_phase3.blast_radius(session, lonely.id)
    assert result["neighbors"] == []
    assert result["count"] == 0
    assert result["total_found"] == 0
    assert result["truncated"] is False
    assert result["root"]["name"] == "lonely"


def test_blast_radius_finds_one_hop_neighbor_in_either_direction(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    esxi = _node(session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
    _declared(session, vm, esxi)

    # Outbound from the VM ...
    out = graph_phase3.blast_radius(session, vm.id)
    assert [n["node"]["name"] for n in out["neighbors"]] == ["esxi01"]
    assert out["neighbors"][0]["hop_distance"] == 1
    assert out["neighbors"][0]["edge_type"] == GraphEdgeType.HOSTED_ON.value

    # ... and inbound from the host: reachability is undirected.
    back = graph_phase3.blast_radius(session, esxi.id)
    assert [n["node"]["name"] for n in back["neighbors"]] == ["web01"]


def test_blast_radius_walks_two_hops_and_reports_distance(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    esxi = _node(session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
    ds = _node(session, GraphNodeType.VSPHERE_DATASTORE, "vc1:ds-1", "datastore1")
    _declared(session, vm, esxi)
    _declared(session, esxi, ds, GraphEdgeType.MOUNTS_DATASTORE)

    result = graph_phase3.blast_radius(session, vm.id, max_hops=2)

    hops = {n["node"]["name"]: n["hop_distance"] for n in result["neighbors"]}
    assert hops == {"esxi01": 1, "datastore1": 2}


def test_blast_radius_hop_cap_excludes_farther_nodes(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    esxi = _node(session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
    ds = _node(session, GraphNodeType.VSPHERE_DATASTORE, "vc1:ds-1", "datastore1")
    _declared(session, vm, esxi)
    _declared(session, esxi, ds, GraphEdgeType.MOUNTS_DATASTORE)

    result = graph_phase3.blast_radius(session, vm.id, max_hops=1)

    assert [n["node"]["name"] for n in result["neighbors"]] == ["esxi01"]


def test_blast_radius_clamps_max_hops_to_the_hard_ceiling(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    result = graph_phase3.blast_radius(session, vm.id, max_hops=99)
    assert result["hops"] == graph_phase3.MAX_HOPS


def test_blast_radius_default_min_confidence_is_declared_only(session):
    """The default must NOT surface a fuzzy identity claim."""
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    esxi = _node(session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
    fuzzy = _node(session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "web-01")
    _declared(session, vm, esxi)
    _edge(
        session,
        vm,
        fuzzy,
        GraphEdgeType.SAME_AS,
        GraphEdgeMethod.PROBABILISTIC_MATCH,
        "0.800",
    )

    default = graph_phase3.blast_radius(session, vm.id)
    assert [n["node"]["name"] for n in default["neighbors"]] == ["esxi01"]

    relaxed = graph_phase3.blast_radius(session, vm.id, min_confidence=0.8)
    assert {n["node"]["name"] for n in relaxed["neighbors"]} == {"esxi01", "web-01"}


def test_blast_radius_confidence_filter_boundary(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    other = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    _edge(
        session,
        vm,
        other,
        GraphEdgeType.SAME_AS,
        GraphEdgeMethod.DETERMINISTIC_MATCH,
        "0.990",
    )
    assert graph_phase3.blast_radius(session, vm.id, min_confidence=0.99)["count"] == 1
    assert graph_phase3.blast_radius(session, vm.id, min_confidence=1.0)["count"] == 0


def test_blast_radius_confidence_floor_is_honored_transitively(session):
    """KG-6: min_confidence must gate TRAVERSAL, not just the final row list.
    vm --(0.500, sub-threshold)--> mid --(1.000, declared)--> far. At
    min_confidence=1.0 ("declared edges only"), `mid` is not reachable at
    all, so `far` must not appear either -- even though the SECOND edge on
    its own is a perfectly good 1.000 edge. Before the fix, the recursive
    walk expanded through the sub-threshold edge to reach `mid` and then
    found `far` via the declared edge, silently overstating certainty."""
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    mid = _node(session, GraphNodeType.R7_ASSET, "1", "web01-fuzzy")
    far = _node(session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
    _edge(session, vm, mid, GraphEdgeType.SAME_AS, GraphEdgeMethod.PROBABILISTIC_MATCH, "0.500")
    _declared(session, mid, far)

    result = graph_phase3.blast_radius(session, vm.id, max_hops=2, min_confidence=1.0)

    names = {n["node"]["name"] for n in result["neighbors"]}
    assert names == set(), (
        "KG-6: `far` was reached only via the sub-threshold edge to `mid` -- "
        f"the confidence floor was not honored transitively (got {names!r})"
    )

    # Sanity: at a relaxed threshold both hops are legitimately reachable.
    relaxed = graph_phase3.blast_radius(session, vm.id, max_hops=2, min_confidence=0.5)
    assert {n["node"]["name"] for n in relaxed["neighbors"]} == {"web01-fuzzy", "esxi01"}


def test_blast_radius_top_n_caps_and_flags_truncation(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    for i in range(6):
        ds = _node(session, GraphNodeType.VSPHERE_DATASTORE, f"vc1:ds-{i}", f"ds{i}")
        _declared(session, vm, ds, GraphEdgeType.MOUNTS_DATASTORE)

    result = graph_phase3.blast_radius(session, vm.id, top_n=3)

    assert result["count"] == 3
    assert len(result["neighbors"]) == 3
    assert result["total_found"] == 6
    assert result["truncated"] is True


def test_blast_radius_top_n_clamped_to_hard_ceiling(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    ds = _node(session, GraphNodeType.VSPHERE_DATASTORE, "vc1:ds-1", "ds")
    _declared(session, vm, ds, GraphEdgeType.MOUNTS_DATASTORE)
    result = graph_phase3.blast_radius(session, vm.id, top_n=100000)
    assert result["count"] == 1  # no crash, and the cap is applied internally


def test_blast_radius_keeps_shortest_hop_when_reachable_two_ways(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    mid = _node(session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
    far = _node(session, GraphNodeType.VSPHERE_DATASTORE, "vc1:ds-1", "ds1")
    _declared(session, vm, mid)
    _declared(session, mid, far, GraphEdgeType.MOUNTS_DATASTORE)
    _declared(session, vm, far, GraphEdgeType.MOUNTS_DATASTORE)  # also 1 hop

    result = graph_phase3.blast_radius(session, vm.id, max_hops=2)

    hops = {n["node"]["name"]: n["hop_distance"] for n in result["neighbors"]}
    assert hops["ds1"] == 1


def test_blast_radius_is_cycle_safe_with_symmetric_same_as(session):
    """SAME_AS is stored both ways; an unguarded walk would loop forever."""
    a = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    b = _node(session, GraphNodeType.R7_ASSET, "1", "web01")
    _declared(session, a, b, GraphEdgeType.SAME_AS)
    _declared(session, b, a, GraphEdgeType.SAME_AS)

    result = graph_phase3.blast_radius(session, a.id, max_hops=3)

    assert [n["node"]["name"] for n in result["neighbors"]] == ["web01"]
    assert result["neighbors"][0]["hop_distance"] == 1


def test_blast_radius_excludes_retired_edges(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    esxi = _node(session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
    edge = _declared(session, vm, esxi)
    graph_phase2.retire_edges(session, [edge])

    assert graph_phase3.blast_radius(session, vm.id)["neighbors"] == []


def test_blast_radius_why_is_a_one_line_summary_not_json(session):
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    esxi = _node(session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
    _declared(session, vm, esxi)

    why = graph_phase3.blast_radius(session, vm.id)["neighbors"][0]["why"]

    assert isinstance(why, str)
    assert "\n" not in why
    assert "HOSTED_ON" in why and "declared" in why and "confidence=1.000" in why


# ---------------------------------------------------------------------------
# Dual-store read — RETIRED IN P5 (there is one store now)
# ---------------------------------------------------------------------------
#
# Five tests stood here pinning the TRK-196 "read from both, migrate neither"
# era: legacy neighbours returned, both stores merged, archived legacy edges
# excluded, and the GRAPH_SERVED_EDGE_TYPES dedup in both directions. P5
# dropped ``resource_relationships`` and deleted the legacy half of the walk
# (``_resource_edge_walk`` and ``GRAPH_SERVED_EDGE_TYPES``), so every one of
# those claims is about code and a table that no longer exist. The surviving
# walk is single-store and is pinned by the blast_radius section above.


def test_blast_radius_never_traverses_not_same_as(session):
    """A human "these are NOT the same machine" must be non-traversable.

    Replaces the retired identity-seeding veto test with the walk-level
    statement that outlives it: ``_graph_edge_walk`` filters ``NOT_SAME_AS``
    out of the walk entirely, so a veto edge neither appears as a neighbour
    nor acts as a bridge to the vetoed node's own neighbourhood. This is the
    load-bearing half of the authority model in traversal — a chain of machine
    ``SAME_AS`` guesses can never ride through a recorded human "no".
    """
    a = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
    b = _node(session, GraphNodeType.R7_ASSET, "1", "web01-imposter")
    c = _node(session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
    # Human veto between a and b; b has its own declared neighbour c.
    graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=GraphEdgeType.NOT_SAME_AS,
        method=GraphEdgeMethod.DECLARED,
        confidence=Decimal("1.000"),
        source="test",
        evidence={"basis": "human veto"},
        authority=GraphEdgeAuthority.HUMAN,
    )
    _declared(session, b, c)

    result = graph_phase3.blast_radius(session, a.id, max_hops=3)

    names = [n["node"]["name"] for n in result["neighbors"]]
    assert "web01-imposter" not in names, "NOT_SAME_AS must not be reported as a neighbour"
    assert "esxi01" not in names, "NOT_SAME_AS must not act as a bridge"
    assert result["neighbors"] == []


# ---------------------------------------------------------------------------
# P5 GAP 3 identity re-entry — BUILT AND RETIRED IN THE SAME WAVE
# ---------------------------------------------------------------------------
#
# Seven tests stood here pinning ``_identity_sibling_seeds`` /
# ``_seeded_legacy_rows``: the machinery that re-entered the legacy store at
# every SAME_AS sibling so an identity hop reached the sibling's containment
# facts too. It was scaffolding for the months when identity lived in
# ``graph_edges`` while facts still lived in ``resource_relationships``; with
# the P5 drop there are no legacy facts to re-enter and the machinery is
# deleted. What the tests actually protected survives elsewhere:
#
#   * sibling reachability — ``SAME_AS`` is a native graph edge, walked like
#     any other (``test_blast_radius_is_cycle_safe_with_symmetric_same_as``);
#   * shortest-hop dedup — ``test_blast_radius_keeps_shortest_hop_when_reachable_two_ways``;
#   * transitive confidence flooring — ``test_blast_radius_confidence_floor_is_honored_transitively``;
#   * the human NOT_SAME_AS veto — ``test_blast_radius_never_traverses_not_same_as``
#     (added as the direct walk-level replacement for the seeding-level veto).


def test_node_without_resource_id_walks_like_any_other(session):
    """A shared Cve node has no owning resource row — the walk neither needs
    one (single-store since P5) nor crashes on its absence."""
    cve = _node(session, GraphNodeType.CVE, "CVE-2024-1", "CVE-2024-1")
    result = graph_phase3.blast_radius(session, cve.id)
    assert result["root"]["resource_id"] is None
    assert result["neighbors"] == []


# ---------------------------------------------------------------------------
# root_cause_candidates
# ---------------------------------------------------------------------------


def _drift(session, resource, field, old, new, when):
    event = DriftEvent(
        id=uuid.uuid4(),
        resource_id=resource.id,
        drift_type="config_drift",
        field=field,
        old_value={"v": old},
        new_value={"v": new},
        detected_at=when,
        status="open",
    )
    session.add(event)
    session.flush()
    return event


def test_root_cause_unknown_node_returns_error(session):
    result = graph_phase3.root_cause_candidates(
        session, uuid.uuid4(), datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert "not found" in result["error"]


def test_root_cause_node_with_no_neighbors_or_drift(session):
    node = _node(session, GraphNodeType.CVE, "CVE-2024-1", "CVE-2024-1")
    result = graph_phase3.root_cause_candidates(
        session, node.id, datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert result["candidates"] == []
    assert result["count"] == 0


def test_root_cause_finds_neighbor_drift_with_hop_and_delta(session):
    vm_res = _resource(session, "web01")
    esxi_res = _resource(session, "esxi01")
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", resource_id=vm_res.id)
    esxi = _node(
        session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01", resource_id=esxi_res.id
    )
    _declared(session, vm, esxi)
    now = datetime.now(timezone.utc)
    _drift(session, esxi_res, "ntp_servers", "a", "b", now - timedelta(hours=1))

    result = graph_phase3.root_cause_candidates(session, vm.id, now - timedelta(days=1))

    assert result["count"] == 1
    cand = result["candidates"][0]
    assert cand["change_event"]["field"] == "ntp_servers"
    assert cand["change_event"]["resource_name"] == "esxi01"
    assert cand["hop_distance"] == 1
    assert cand["delta"].startswith("ntp_servers: ")
    assert "->" in cand["delta"]


def test_root_cause_includes_the_node_itself_at_hop_zero(session):
    vm_res = _resource(session, "web01")
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", resource_id=vm_res.id)
    now = datetime.now(timezone.utc)
    _drift(session, vm_res, "cpu_count", 2, 4, now - timedelta(hours=2))

    result = graph_phase3.root_cause_candidates(session, vm.id, now - timedelta(days=1))

    assert result["count"] == 1
    assert result["candidates"][0]["hop_distance"] == 0
    assert result["candidates"][0]["edge_type"] == "SELF"


def test_root_cause_excludes_drift_before_since(session):
    vm_res = _resource(session, "web01")
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", resource_id=vm_res.id)
    now = datetime.now(timezone.utc)
    _drift(session, vm_res, "old_change", 1, 2, now - timedelta(days=30))

    result = graph_phase3.root_cause_candidates(session, vm.id, now - timedelta(days=1))

    assert result["candidates"] == []


def test_root_cause_ranks_closer_hops_first(session):
    vm_res = _resource(session, "web01")
    esxi_res = _resource(session, "esxi01")
    ds_res = _resource(session, "ds1")
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", resource_id=vm_res.id)
    esxi = _node(
        session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01", resource_id=esxi_res.id
    )
    ds = _node(session, GraphNodeType.VSPHERE_DATASTORE, "vc1:ds-1", "ds1", resource_id=ds_res.id)
    _declared(session, vm, esxi)
    _declared(session, esxi, ds, GraphEdgeType.MOUNTS_DATASTORE)
    now = datetime.now(timezone.utc)
    # The FARTHER change is more recent — hop must still win.
    _drift(session, ds_res, "capacity", 1, 2, now - timedelta(minutes=1))
    _drift(session, esxi_res, "ntp", "a", "b", now - timedelta(hours=5))

    result = graph_phase3.root_cause_candidates(session, vm.id, now - timedelta(days=1))

    assert [c["hop_distance"] for c in result["candidates"]] == [1, 2]
    assert result["candidates"][0]["change_event"]["field"] == "ntp"


def test_root_cause_within_a_hop_tier_ranks_most_recent_detected_at_first(session):
    """Docstring promise: 'ranked by (hop asc, detected_at desc)'. Two drift
    events tied at the SAME hop distance must come back freshest-first, not
    oldest-first -- the whole point of root-cause investigation is 'what
    changed nearby, recently'."""
    vm_res = _resource(session, "web01")
    esxi_res = _resource(session, "esxi01")
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", resource_id=vm_res.id)
    esxi = _node(
        session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01", resource_id=esxi_res.id
    )
    _declared(session, vm, esxi)
    now = datetime.now(timezone.utc)
    # Both events land at hop 1 (esxi_res); the OLDER one is written first so
    # a naive "sort by detected_at ascending, then re-sort by hop" bug would
    # leave it first within the tier.
    _drift(session, esxi_res, "old_field", "a", "b", now - timedelta(hours=5))
    _drift(session, esxi_res, "new_field", "c", "d", now - timedelta(minutes=1))

    result = graph_phase3.root_cause_candidates(session, vm.id, now - timedelta(days=1))

    assert [c["hop_distance"] for c in result["candidates"]] == [1, 1]
    assert [c["change_event"]["field"] for c in result["candidates"]] == [
        "new_field",
        "old_field",
    ], "within one hop tier, the most recently detected event must sort first"


def test_root_cause_includes_low_confidence_edges_on_purpose(session):
    """Unlike blast radius, a root-cause search wants weak leads too."""
    vm_res = _resource(session, "web01")
    peer_res = _resource(session, "web-01", domain="octopus")
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", resource_id=vm_res.id)
    peer = _node(
        session, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "web-01", resource_id=peer_res.id
    )
    _edge(
        session,
        vm,
        peer,
        GraphEdgeType.SAME_AS,
        GraphEdgeMethod.PROBABILISTIC_MATCH,
        "0.800",
    )
    now = datetime.now(timezone.utc)
    _drift(session, peer_res, "tentacle_version", "6.0", "6.1", now - timedelta(hours=1))

    result = graph_phase3.root_cause_candidates(session, vm.id, now - timedelta(days=1))

    assert result["count"] == 1
    assert result["candidates"][0]["change_event"]["field"] == "tentacle_version"


def test_root_cause_excludes_graph_maintenance_self_telemetry(session):
    """TRK-191: the graph agent's own bookkeeping is not infra change."""
    vm_res = _resource(session, "web01")
    noise_res = _resource(session, "graph-health", domain="graph_maintenance")
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", resource_id=vm_res.id)
    noise = _node(
        session, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "graph-health", resource_id=noise_res.id
    )
    _declared(session, vm, noise)
    now = datetime.now(timezone.utc)
    _drift(session, noise_res, "edge_count", 1, 2, now - timedelta(hours=1))

    result = graph_phase3.root_cause_candidates(session, vm.id, now - timedelta(days=1))

    assert result["candidates"] == []


def test_root_cause_top_n_caps_and_flags_truncation(session):
    vm_res = _resource(session, "web01")
    vm = _node(session, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", resource_id=vm_res.id)
    now = datetime.now(timezone.utc)
    for i in range(5):
        _drift(session, vm_res, f"field{i}", 1, 2, now - timedelta(hours=i + 1))

    result = graph_phase3.root_cause_candidates(session, vm.id, now - timedelta(days=1), top_n=2)

    assert result["count"] == 2
    assert result["total_found"] == 5
    assert result["truncated"] is True


# ``test_root_cause_reaches_legacy_store_neighbors`` was DELETED in P5: it
# seeded a ``resource_relationships`` row and asserted root-cause candidates
# reached it. Single-store now; graph-edge neighbour drift is pinned by
# ``test_root_cause_finds_neighbor_drift_with_hop_and_delta`` above.


def test_short_renders_values_on_one_truncated_line(session):
    assert graph_phase3._short(None) == "None"
    long = graph_phase3._short({"k": "x" * 200})
    assert len(long) <= 60
    assert "\n" not in long
