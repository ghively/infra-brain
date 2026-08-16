"""P5: ``graph_phase3.resolve_entities`` IS a coverage superset of the two
``IS_SAME_AS`` emission passes ``host_reconcile`` used to run — which is what
made deleting them safe.

History, because the shape of this file only makes sense with it. T1 wrote these
tests to PIN two gaps, so they were executable rather than a paragraph in a
report, and so the day the gaps closed the file would go RED and become the
green light for P5. It did. This is the same file rewritten to assert the CLOSED
state, and it is now the permanent proof that keeps it closed:

* GAP 1 — six of nine identity legs had no resolver node type, so a pair whose
  endpoints were not both in ``HOST_NODE_TYPES`` was not "uncertain, therefore
  queued" but INVISIBLE. Closed for every leg that can carry data on this estate
  (``linux``, ``net``); recorded as a standing revival obligation for the legs
  whose domains are retired or unregistered.
* GAP 2 — the TRK-062 class (different hostnames, one shared IP) was DROPPED,
  not queued, even between two fully-covered node types. Closed by flooring such
  a pair into the review band.

``host_reconcile`` no longer writes ``IS_SAME_AS`` at all
(``graph_phase3.resolve_entities`` is the sole identity writer), and the last
section here is the deletion's own regression test: a fixture the deleted pass
WOULD have emitted for, asserted to reach the resolver instead.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain import graph_phase2, graph_phase3
from infra_brain.agents.host_reconcile import HostReconcileAgent
from infra_brain.db.models import (
    ZONE_CORPORATE,
    GraphEdge,
    GraphEdgeType,
    GraphNodeType,
    ProposedAction,
    Resource,
)
from tests.support.pg import make_engine


@pytest.fixture()
def session():
    # make_engine (TRK-356): in-memory SQLite normally, the real PostgreSQL
    # under PG_GATE_DSN. This module asserts on what a resolver pass DOES with
    # graph_nodes rows, so it is an ORM-layer test and belongs on both dialects.
    engine = make_engine()
    with Session(engine) as s:
        yield s
        s.rollback()


def _resource(session, name, domain):
    """A real ``resources`` row to hang a node off.

    Not optional: ``graph_nodes.resource_id`` carries an FK that SQLite ignores
    and PostgreSQL enforces, so a bare ``uuid4()`` passes the default suite and
    raises ``ForeignKeyViolation`` under ``PG_GATE_DSN`` (TRK-356's whole point).
    """
    r = Resource(
        id=uuid.uuid4(), name=name, domain=domain, type="host", source="test", zone=ZONE_CORPORATE
    )
    session.add(r)
    session.flush()
    return r


def _same_as_edges(session):
    return (
        session.execute(select(GraphEdge).where(GraphEdge.edge_type == GraphEdgeType.SAME_AS.value))
        .scalars()
        .all()
    )


def _review_rows(session):
    return (
        session.execute(
            select(ProposedAction).where(ProposedAction.action_type == "entity_resolution_same_as")
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# GAP 1 — leg coverage
# ---------------------------------------------------------------------------

#: host_reconcile source label -> the graph node type that represents it, or
#: None where the leg's source cannot currently produce a single row (its domain
#: is ``retired=True``, or the collector is not registered at all) and no
#: speculative NodeSpec was invented for it.
#:
#: Coverage is computed against ``HOST_NODE_TYPES`` rather than hardcoded, so
#: narrowing that tuple fails the assertions below instead of silently
#: re-opening the hole.
_LEG_TO_NODE_TYPE: dict[str, str | None] = {
    "r7": GraphNodeType.R7_ASSET.value,
    "vsphere": GraphNodeType.VSPHERE_VM.value,
    "octopus": GraphNodeType.OCTOPUS_MACHINE.value,
    # Declared by agents/linux.py and POPULATED on the live estate. This is the
    # leg the whole gap was about: declared, materialised, and still never
    # loaded by the resolver until HOST_NODE_TYPES was widened.
    "linux": "LinuxHost",
    # Declared by agents/netdiscovery.py (P5). Keyed on the hostname, never on
    # the IP — see that spec for the reasoning, and
    # test_bare_ip_netdiscovery_rows_get_no_node below for the assertion.
    "net": "NetDiscoveredHost",
    # No node, on purpose. See _RETIRED_LEGS.
    "windows": None,
    "cloud": None,
    "k8s": None,
    "netdevice": None,
}

#: Legs whose source cannot currently produce a row, mapped to the module whose
#: spec carries the revival obligation. VACUOUSLY covered: there is nothing to
#: resolve, and a speculative NodeSpec for a machine class that has never
#: existed would be a guess about a schema nobody has seen.
#:
#: The obligation is that REVIVING one of these is not just a settings flip —
#: it must also add ``NodeSpec(is_host_identity=True)`` and the matching
#: ``HOST_NODE_TYPES`` entry, or the revived leg's hosts are invisible to
#: identity exactly as ``linux``'s were. ``test_retired_legs_carry_the_revival_
#: obligation`` asserts the note exists; ``test_every_declared_host_node_type_
#: is_in_host_node_types`` is what actually fires the day one is revived.
_RETIRED_LEGS: dict[str, str] = {
    "windows": "infra_brain/agents/windows.py",
    "cloud": "infra_brain/agents/cloud.py",
    "k8s": "infra_brain/agents/k8s.py",
    # NOT retired=True — NetAgent is dispatchable=False, i.e. absent from
    # AGENT_REGISTRY entirely, which is a stronger form of off: the coverage
    # guard cannot even see its spec.
    "netdevice": "infra_brain/agents/net.py",
}

#: Legs the resolver can see. Derived, so it tracks the tuple.
COVERED_LEGS = frozenset(
    label
    for label, node_type in _LEG_TO_NODE_TYPE.items()
    if node_type in graph_phase3.HOST_NODE_TYPES
)


def test_leg_map_matches_host_reconcile_source_keys():
    """The map above is the real ``_SOURCE_KEYS``, not a stale copy."""
    assert {label for _key, label in HostReconcileAgent._SOURCE_KEYS} == set(_LEG_TO_NODE_TYPE)


def test_every_leg_that_can_carry_data_has_a_resolver_node_type():
    """GAP 1, CLOSED. No live leg is invisible to entity resolution.

    RED here means one of two things, and they need opposite responses:

    * a leg lost its node type (``HOST_NODE_TYPES`` narrowed, or a NodeSpec
      dropped) — the coverage hole is back, and ``host_reconcile`` is no longer
      there to catch the pairs. Fix the resolver.
    * a retired domain was REVIVED without declaring a host node — do that
      declaration; the obligation is written next to its ``retired=True``.
    """
    live_legs = set(_LEG_TO_NODE_TYPE) - set(_RETIRED_LEGS)
    assert live_legs == {"r7", "vsphere", "octopus", "linux", "net"}
    assert live_legs <= COVERED_LEGS, (
        f"legs with no resolver node type: {sorted(live_legs - COVERED_LEGS)} — "
        "pairs on these are not queued for a human, they are never scored at all"
    )
    assert COVERED_LEGS.isdisjoint(_RETIRED_LEGS), (
        "a retired leg with a node type means a speculative NodeSpec was declared "
        "for a source that has never produced a row"
    )


def test_every_declared_host_node_type_is_in_host_node_types():
    """The guard that makes this gap CLASS impossible, not just this instance.

    ``HOST_NODE_TYPES`` stays an explicit tuple (deriving it would mean importing
    every collector module inside the resolver — see its own comment). This is
    the other half of that bargain: the declaration side,
    ``NodeSpec.is_host_identity``, compared against it. A collector that declares
    a host node and forgets the tuple fails HERE, loudly, instead of contributing
    nodes nothing ever looks at.
    """
    from infra_brain.etl.spec import declared_host_identity_node_types

    declared = declared_host_identity_node_types()
    missing = {t: d for t, d in declared.items() if t not in graph_phase3.HOST_NODE_TYPES}
    assert not missing, (
        f"declared host node type(s) absent from graph_phase3.HOST_NODE_TYPES: {missing}. "
        "Add them there — a declared-but-unlisted host node is invisible to "
        "resolve_entities, which is exactly how the LinuxHost gap happened."
    )
    # And the live declarations are the two P5 added, so a new one is a
    # deliberate edit here rather than a silent widening.
    assert declared == {"LinuxHost": "linux", "NetDiscoveredHost": "netdiscovery"}


@pytest.mark.parametrize(("leg", "module"), sorted(_RETIRED_LEGS.items()))
def test_retired_legs_carry_the_revival_obligation(leg, module):
    """A vacuously-covered leg must SAY why, next to the switch that revives it.

    Not decoration: the person who flips ``COLLECTION_REVIVED_DOMAINS=windows``
    is reading that spec, not this test, and the failure they would otherwise
    cause is silent.
    """
    from pathlib import Path

    import infra_brain

    source = (Path(infra_brain.__file__).parent.parent / module).read_text()
    assert "P5 REVIVAL OBLIGATION" in source, f"{module} must record the P5 revival obligation"
    assert "is_host_identity" in source
    assert "HOST_NODE_TYPES" in source


def test_linux_vsphere_convergence_is_resolved_by_the_resolver(session):
    """GAP 1, CLOSED behaviourally, on the exact pair T1 measured.

    A linux leg and a vSphere leg converging on one normalized hostname is what
    ``_emit_is_same_as_edges`` emitted at 0.95. The resolver now emits the same
    pair as ``deterministic_match`` at 0.990 — a SUPERSET, not a substitute:
    stronger provenance (method + evidence + authority), and in the store the
    review queue and the human-decision gates actually read.
    """
    graph_phase2.upsert_node(
        session,
        node_type="LinuxHost",
        natural_key="web01",
        name="web01",
        source="linux",
        resource_id=_resource(session, "web01", "linux").id,
        attributes={"ip": "10.10.0.5"},
    )
    graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.VSPHERE_VM,
        natural_key="vc1:vm-101",
        name="web01",
        source="vsphere",
        resource_id=_resource(session, "web01", "vsphere").id,
        attributes={"ip": "10.10.0.5"},
    )
    session.commit()

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["nodes_considered"] == 2, "the LinuxHost node is loaded now"
    assert counts["deterministic_edges"] == 1
    assert counts["probabilistic_edges"] == 0
    assert counts["review_queued"] == 0

    edges = _same_as_edges(session)
    assert len(edges) == 2, "identity is symmetric — one pair, two directed rows"
    assert {float(e.confidence) for e in edges} == {0.99}
    assert {e.method for e in edges} == {"deterministic_match"}
    assert {e.authority for e in edges} == {"auto"}


def test_netdiscovery_hostname_leg_reaches_the_resolver(session):
    """The ``net`` leg's covered half: a hostname-bearing probe IS a candidate."""
    graph_phase2.upsert_node(
        session,
        node_type="NetDiscoveredHost",
        natural_key="web01",
        name="web01",
        source="netdiscovery",
        resource_id=_resource(session, "10.10.0.5", "netdiscovery").id,
        attributes={"ip": "10.10.0.5", "hostname": "web01"},
    )
    graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.R7_ASSET,
        natural_key="4477",
        name="web01",
        source="rapid7",
        resource_id=_resource(session, "web01", "rapid7").id,
        attributes={"ip": "10.10.0.5"},
    )
    session.commit()

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["nodes_considered"] == 2
    assert counts["deterministic_edges"] == 1


def test_bare_ip_netdiscovery_rows_get_no_node():
    """The ``net`` leg's UNCOVERED half, asserted rather than assumed.

    A netdiscovery probe that never resolved a name has an ADDRESS, not an
    identity. ``resources.name`` for it is the IP, and this codebase has spent
    TRK-062 / TRK-087 / KG-9 establishing that an IP is not a stable key: DHCP
    churn, NAT and VM reassignment mean the same value names a different machine
    later, so a node keyed on it would silently rewrite one host's identity into
    another's. The NodeSpec keys on ``metadata.hostname`` and ``graph_engine``
    skips a row whose natural key resolves empty, so such probes get NO node.

    That is a deliberate absence, and it is the honest answer: the resolver
    cannot assert identity for a thing that has none. What the deleted 0.75
    ``net_match_basis="ip"`` attach used to do for these rows is now covered only
    where the probe has a hostname; where it does not, nothing is asserted, which
    is better than asserting something unstable.
    """
    from infra_brain.agents.netdiscovery import NetDiscoveryAgent

    (node,) = NetDiscoveryAgent.spec.emits_nodes
    assert node.type == "NetDiscoveredHost"
    assert node.natural_key == "metadata.hostname", (
        "keying on 'name' would key on the IP — the exact unstable identity this "
        "leg must not mint nodes from"
    )
    assert node.is_host_identity is True
    # The correlatable address still rides along as an ATTRIBUTE, which is what
    # lets GAP 2's shared-IP promotion see it without it becoming the key.
    assert "ip" in node.attributes
    assert "mac" in node.attributes, "the one hard identifier this source carries"


# ---------------------------------------------------------------------------
# GAP 2 — the TRK-062 shared-IP class
# ---------------------------------------------------------------------------


def test_shared_ip_with_dissimilar_names_is_queued_for_review(session):
    """GAP 2, CLOSED. host_reconcile ASSERTED this at 0.70; the resolver ASKS.

    Note precisely what changed and what did not. The pair is no longer dropped
    unasked — it reaches the human queue. It is still never auto-emitted: the
    shared-IP nudge is capped strictly below ``FUZZY_AUTO_EMIT_MIN``, so this
    cannot become the 0.70-tier auto-assertion under a new name. A spoofable,
    reused address justifies a question and never a claim.
    """
    graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.VSPHERE_VM,
        natural_key="vc1:vm-77",
        name="app-fileshare",
        source="vsphere",
        resource_id=_resource(session, "app-fileshare", "vsphere").id,
        attributes={"ip": "10.20.0.9", "all_ips": ["10.20.0.9"]},
    )
    graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.R7_ASSET,
        natural_key="4477",
        name="nas01",
        source="rapid7",
        resource_id=_resource(session, "nas01", "rapid7").id,
        attributes={"ip": "10.20.0.9"},
    )
    session.commit()

    a, b = sorted(
        session.execute(select(graph_phase3.GraphNode)).scalars().all(),
        key=lambda n: n.node_type,
    )
    score, evidence, counter_evidence = graph_phase3._score_candidate(a, b)
    assert evidence.get("ip_overlap") is True
    assert not counter_evidence
    # EXACTLY the review floor: the weakest thing the system will ask about.
    assert score == graph_phase3.FUZZY_REVIEW_MIN
    assert score < graph_phase3.FUZZY_AUTO_EMIT_MIN, "must never be able to auto-emit"
    assert evidence.get("ip_floor_applied") is True, (
        "the reviewer must be able to tell 0.75 is a floor, not a name similarity"
    )

    counts = graph_phase3.resolve_entities(session, materialize=False)
    assert counts["nodes_considered"] == 2
    assert counts["deterministic_edges"] == 0
    assert counts["probabilistic_edges"] == 0
    assert counts["review_queued"] == 1
    assert _same_as_edges(session) == [], "queued, never asserted"

    (review,) = _review_rows(session)
    (candidate,) = review.payload["candidate_matches"]
    assert candidate["evidence"]["ip_overlap"] is True
    assert "shared IP" in candidate["reason"]
    assert "never an assertion" in candidate["reason"]


def test_a_shared_ip_can_never_lift_a_pair_into_the_auto_emit_band(session):
    """The cap, which the new floor must not have weakened.

    ``node_a``/``node_a2`` is the case that actually exercises the ceiling: on
    names alone it already sits in the review band, and the +0.15 shared-IP nudge
    would carry it PAST ``FUZZY_AUTO_EMIT_MIN`` if the cap were not there — an
    automatic merge of two machines whose names differ by a trailing digit,
    justified by a reused address. The floor added for GAP 2 only ever raises a
    score UP TO ``FUZZY_REVIEW_MIN``, so it can never interact with this ceiling;
    this test is what proves the two rules stayed independent.
    """
    a = graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.VSPHERE_VM,
        natural_key="vc1:vm-9",
        name="node_a",
        source="vsphere",
        resource_id=_resource(session, "node_a", "vsphere").id,
        attributes={"ip": "10.30.0.4"},
    )
    b = graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.R7_ASSET,
        natural_key="991",
        name="node_a2",
        source="rapid7",
        resource_id=_resource(session, "node_a2", "rapid7").id,
        attributes={"ip": "10.30.0.4"},
    )
    session.commit()

    name_only = graph_phase3._score_pair("node_a", "node_a2")
    score, evidence, _counter = graph_phase3._score_candidate(a, b)
    assert graph_phase3.FUZZY_REVIEW_MIN <= name_only < graph_phase3.FUZZY_AUTO_EMIT_MIN
    assert name_only + 0.15 > graph_phase3.FUZZY_AUTO_EMIT_MIN, (
        "the fixture must actually reach the ceiling, or this test proves nothing"
    )
    assert score > name_only, "the shared IP is real evidence and does raise the score"
    assert score == graph_phase3.FUZZY_AUTO_EMIT_MIN - 0.01, "clamped to the ceiling"
    assert score < graph_phase3.FUZZY_AUTO_EMIT_MIN, "and never far enough to auto-merge"
    assert evidence.get("ip_overlap") is True
    assert "ip_floor_applied" not in evidence, "the names carried this one, not the floor"

    counts = graph_phase3.resolve_entities(session, materialize=False)
    assert counts["probabilistic_edges"] == 0
    assert counts["review_queued"] == 1


def test_a_non_routable_shared_address_is_not_correlation_evidence(session):
    """The routability guard, ported from the deleted ``_is_correlatable_ip``.

    Every host reports its own ``127.0.0.1``. Scoring that as a shared address
    would promote every pair in the estate into the review queue; scoring it as
    a CONFLICT would be equally wrong the other way. Neither happens: it is not
    an address either side has claimed.
    """
    a = graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.VSPHERE_VM,
        natural_key="vc1:vm-lo",
        name="lo-host-a",
        source="vsphere",
        resource_id=_resource(session, "lo-host-a", "vsphere").id,
        attributes={"ip": "127.0.0.1"},
    )
    b = graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.R7_ASSET,
        natural_key="lo-1",
        name="nas-elsewhere",
        source="rapid7",
        resource_id=_resource(session, "nas-elsewhere", "rapid7").id,
        attributes={"ip": "127.0.0.1"},
    )
    session.commit()

    score, evidence, counter_evidence = graph_phase3._score_candidate(a, b)
    assert "ip_overlap" not in evidence
    assert "ip_floor_applied" not in evidence
    assert score < graph_phase3.FUZZY_REVIEW_MIN
    assert counter_evidence == [], "and a shared loopback is not a CONFLICT either"

    assert graph_phase3.resolve_entities(session, materialize=False)["review_queued"] == 0


def test_an_address_three_hosts_claim_is_reported_but_never_scored(session):
    """The 3+-claimant guard, ported from the deleted pass into ``SharedIpIndex``.

    An address three or more hosts report is a NAT gateway, a reused DHCP lease
    or a shared VIP — real, and evidence of nothing. Promoting those pairs would
    fan out C(n,2) questions from one address; the guard withholds the score
    while still SHOWING the reviewer the overlap, flagged as ambiguous, so the
    suppression is visible rather than silent.
    """
    for node_type, key, name, source in (
        (GraphNodeType.R7_ASSET.value, "nat-a", "nat-host-a", "rapid7"),
        (GraphNodeType.VSPHERE_VM.value, "vc1:nat-b", "nat-host-b", "vsphere"),
        (GraphNodeType.OCTOPUS_MACHINE.value, "Machines-3", "nat-host-c", "octopus"),
    ):
        graph_phase2.upsert_node(
            session,
            node_type=node_type,
            natural_key=key,
            name=name,
            source=source,
            resource_id=_resource(session, name, source).id,
            attributes={"ip": "10.9.9.9"},
        )
    session.commit()

    nodes = session.execute(select(graph_phase3.GraphNode)).scalars().all()
    index = graph_phase3.shared_ip_index(nodes)
    assert index.any_uniquely_shared({"10.9.9.9"}) is False

    a, b = sorted(nodes, key=lambda n: n.node_type)[:2]
    _score, evidence, _counter = graph_phase3._score_candidate(a, b, shared_ips=index)
    assert evidence["ip_overlap"] is True, "the reviewer still sees the shared address"
    assert evidence["ip_overlap_ambiguous"] is True, "flagged as why it was not scored"
    assert "ip_floor_applied" not in evidence

    counts = graph_phase3.resolve_entities(session, materialize=False)
    assert counts["nodes_considered"] == 3
    assert counts["review_queued"] == 0, "3 hosts on one IP is 3 questions of pure noise"
    assert counts["probabilistic_edges"] == 0


def test_the_ip_floor_does_not_bypass_the_domain_conflict_guard(session):
    """TRK-087 still wins. Evidence that positively says NO is not a question."""
    graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.VSPHERE_VM,
        natural_key="vc1:vm-5",
        name="fileshare.corp.example.com",
        source="vsphere",
        resource_id=_resource(session, "fileshare.corp", "vsphere").id,
        attributes={"ip": "10.40.0.7"},
    )
    graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.R7_ASSET,
        natural_key="55",
        name="nas01.dmz.example.org",
        source="rapid7",
        resource_id=_resource(session, "nas01.dmz", "rapid7").id,
        attributes={"ip": "10.40.0.7"},
    )
    session.commit()

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["domain_conflicts_suppressed"] == 1
    assert counts["review_queued"] == 0
    assert _same_as_edges(session) == []
    assert _review_rows(session) == []


# ---------------------------------------------------------------------------
# The deletion itself
# ---------------------------------------------------------------------------


def test_host_reconcile_is_no_longer_an_is_same_as_writer():
    """The writer is GONE, not renamed, not re-anchored onto graph_edges.

    Re-anchoring was the tempting move and the wrong one: it would have made a
    second writer of the resolver-owned ``SAME_AS`` type with different
    thresholds and a different authority model — the two-writers-one-truth
    problem the graph-first architecture exists to remove.
    """
    for gone in (
        "_emit_is_same_as_edges",
        "_emit_cross_hostname_ip_edges",
        # helpers that existed only to serve those two passes
        "_open_ip_conflict_ids",
        "_resource_pair_gate",
        "_anchor_source",
        "_is_correlatable_ip",
        "_CONF_HOSTNAME",
        "_CONF_NET_IP_ATTACH",
        "_CONF_CROSS_HOSTNAME_IP",
    ):
        assert not hasattr(HostReconcileAgent, gone), (
            f"HostReconcileAgent.{gone} is back — identity has exactly one writer"
        )

    # What it still does, all of which FEEDS the resolver rather than competing.
    for kept in (
        "_build_merged_hosts",
        "_upsert_identities",
        "_ambiguous_sources",
        "_emit_ip_conflict_events",
        "_emit_identity_conflict_events",
        "_same_as_review_exists",
    ):
        assert hasattr(HostReconcileAgent, kept), f"HostReconcileAgent.{kept} must survive P5"


def test_the_pair_host_reconcile_would_have_emitted_now_reaches_the_resolver(session):
    """The permanent coverage proof T1 specified, in one fixture.

    Two source legs converging on one normalized short hostname is the input
    ``_emit_is_same_as_edges`` turned into a 0.95 legacy edge. Same input,
    resolver-only system: a ``SAME_AS`` edge exists, in ``graph_edges``, with
    provenance the legacy row never carried — and the legacy store stays empty,
    which is the point of deleting rather than re-anchoring.
    """
    for node_type, key, name, domain, source in (
        ("LinuxHost", "db01", "db01", "linux", "linux"),
        (GraphNodeType.R7_ASSET.value, "7788", "db01.corp.example.com", "rapid7", "rapid7"),
    ):
        graph_phase2.upsert_node(
            session,
            node_type=node_type,
            natural_key=key,
            name=name,
            source=source,
            resource_id=_resource(session, name, domain).id,
            attributes={},
        )
    session.commit()

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["deterministic_edges"] == 1, (
        "normalize_host collapses db01 and db01.corp.example.com to one key — the "
        "exact convergence the deleted pass emitted for"
    )
    edges = _same_as_edges(session)
    assert len(edges) == 2
    assert edges[0].evidence["basis"] == "normalized_hostname_exact"
    assert edges[0].evidence["normalized_key"] == "db01"


def test_an_unsettled_leg_still_blocks_the_assertion_after_the_switch(session):
    """host_reconcile's ambiguity signal still stops a merge — via the resolver.

    The deleted pass refused to assert 0.95 over the coin-flip survivor of a
    same-source collision (KG-2 gate (c)). That refusal did not go with it:
    ``host_identities.identity_ambiguous_sources`` is read back by
    ``graph_phase3.ambiguous_leg_index`` and becomes counter-evidence, so the
    pair is diverted to review instead of merged. The cross-module contract is
    the surviving half of what the agent used to do inline.
    """
    from infra_brain.db.models import HostIdentity

    linux_res = _resource(session, "app01", "linux")
    r7_res = _resource(session, "app01", "rapid7")
    session.add(
        HostIdentity(
            id=uuid.uuid4(),
            short_hostname="app01",
            linux_resource_id=linux_res.id,
            r7_resource_id=r7_res.id,
            identity_ambiguous_sources=["linux"],
        )
    )
    for node_type, key, name, source, rid in (
        ("LinuxHost", "app01", "app01", "linux", linux_res.id),
        (GraphNodeType.R7_ASSET.value, "9001", "app01", "rapid7", r7_res.id),
    ):
        graph_phase2.upsert_node(
            session,
            node_type=node_type,
            natural_key=key,
            name=name,
            source=source,
            resource_id=rid,
            attributes={},
        )
    session.commit()

    counts = graph_phase3.resolve_entities(session, materialize=False)

    assert counts["deterministic_edges"] == 0, "an unsettled leg is never asserted over"
    assert counts["review_queued"] == 1
    assert counts["unsettled_legs_flagged"] >= 1
    assert _same_as_edges(session) == []
