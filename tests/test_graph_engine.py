"""The generic graph engine — P0 of docs/decisions/2026-08-11-graph-first-architecture.md.

The engine materialises ``graph_nodes`` / ``graph_edges`` from what collectors
DECLARE on their ``AgentSpec``. It must contain zero domain knowledge: every
test here drives it with synthetic node/edge declarations over synthetic
``resources`` rows, never with a real collector's spec (that is
``tests/agents/test_homelab_services_graph.py``'s job).

Three properties are load-bearing and each has a test:

1. A collector that declares nodes/edges gets them materialised.
2. A collector that declares NOTHING produces NOTHING — P0 is a no-op for
   every one of the ~30 existing specs, which is what makes it reversible.
3. An engine-written (``authority='auto'``) edge NEVER overwrites a
   human-authored one, per docs/decisions/2026-08-10-graph-edge-authority-spec.md.
4. A relationship that fans OUT is declarable (``EdgeDirection.INVERSE``)
   WITHOUT the key->one-target ambiguity rule moving, and a many-to-many one
   still is not. See ``test_a_one_to_many_edge_is_declared_as_an_inverse`` and
   ``test_a_many_to_many_relationship_is_still_not_expressible`` — the latter
   replaces the boundary the former used to be.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain import graph_phase2
from infra_brain.db.models import (
    GraphEdge,
    GraphEdgeAuthority,
    GraphEdgeMethod,
    GraphNode,
    Resource,
)
from infra_brain.etl.spec import AgentSpec, EdgeDirection, EdgeSpec, NodeSpec, Tier

from tests.support.pg import make_engine


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        if engine.dialect.name == "sqlite":
            # SQLite ignores FKs unless asked; PostgreSQL enforces them always.
            s.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
        yield s
        s.rollback()


def _resource(session, *, domain, rtype, name, metadata=None):
    r = Resource(
        id=uuid.uuid4(),
        domain=domain,
        type=rtype,
        name=name,
        source="test",
        metadata_=metadata or {},
    )
    session.add(r)
    session.flush()
    return r


# --- synthetic declarations -------------------------------------------------
#
# Deliberately fictional domains/types. If the engine ever needs to know what
# "widget" means to make these pass, the contract is wrong.

_WIDGET_SPEC = AgentSpec(
    domain="widgets",
    tier=Tier.COLLECTOR,
    schedule=None,
    max_staleness=timedelta(hours=1),
    emits_nodes=(
        NodeSpec(
            type="Widget",
            resource_type="widget",
            natural_key="name",
            attributes=("rack",),
        ),
    ),
    emits_edges=(
        EdgeSpec(
            type="MOUNTED_IN",
            from_node="Widget",
            to_node="Rack",
            from_key="attributes.rack",
            to_key="name",
            method=GraphEdgeMethod.DETERMINISTIC_MATCH,
            confidence=Decimal("0.990"),
        ),
    ),
)

_RACK_SPEC = AgentSpec(
    domain="racks",
    tier=Tier.COLLECTOR,
    schedule=None,
    max_staleness=timedelta(hours=1),
    emits_nodes=(NodeSpec(type="Rack", resource_type="rack", natural_key="name"),),
)

_SILENT_SPEC = AgentSpec(
    domain="silent",
    tier=Tier.COLLECTOR,
    schedule=None,
    max_staleness=timedelta(hours=1),
)

#: The SAME relationship as ``_WIDGET_SPEC``'s, stored the other way round:
#: one rack HOLDS many widgets. Declared by the WIDGET collector, because the
#: join key (which rack a widget sits in) is the widget's — ``direction`` moves
#: the arrow, never the join. This is how one-to-many is expressed.
_WIDGET_SPEC_BOTH_DIRECTIONS = AgentSpec(
    domain="widgets",
    tier=Tier.COLLECTOR,
    schedule=None,
    max_staleness=timedelta(hours=1),
    emits_nodes=_WIDGET_SPEC.emits_nodes,
    emits_edges=(
        *_WIDGET_SPEC.emits_edges,
        EdgeSpec(
            type="HOLDS",
            from_node="Widget",
            to_node="Rack",
            from_key="attributes.rack",
            to_key="name",
            direction=EdgeDirection.INVERSE,
            method=GraphEdgeMethod.DETERMINISTIC_MATCH,
            confidence=Decimal("0.990"),
        ),
    ),
)

#: The NAIVE attempt at the same thing: fan out by joining FROM the "one" side.
#: Every widget claims the rack's key, so the join has many candidate targets
#: and is refused. Kept as the counter-example the contract's shape exists to
#: rule out — see ``test_a_many_to_many_relationship_is_still_not_expressible``.
_RACK_SPEC_FORWARD_FANOUT = AgentSpec(
    domain="racks",
    tier=Tier.COLLECTOR,
    schedule=None,
    max_staleness=timedelta(hours=1),
    emits_nodes=(
        NodeSpec(type="Rack", resource_type="rack", natural_key="name", attributes=("rack",)),
    ),
    emits_edges=(
        EdgeSpec(
            type="HOLDS",
            from_node="Rack",
            to_node="Widget",
            from_key="attributes.rack",
            to_key="attributes.rack",
            method=GraphEdgeMethod.DETERMINISTIC_MATCH,
            confidence=Decimal("0.990"),
        ),
    ),
)


def test_declared_nodes_and_edges_are_materialised(session):
    """The whole point: declaring is enough — no central edit anywhere."""
    from infra_brain import graph_engine

    _resource(session, domain="racks", rtype="rack", name="r1")
    _resource(session, domain="widgets", rtype="widget", name="w1", metadata={"rack": "r1"})

    counts, errors = graph_engine.emit_all(
        session, specs={"widgets": _WIDGET_SPEC, "racks": _RACK_SPEC}
    )

    assert errors == [], errors
    types = {n.node_type for n in session.execute(select(GraphNode)).scalars()}
    assert types == {"Widget", "Rack"}, (
        "both declared node types must be materialised from the generic resources table"
    )
    edges = list(session.execute(select(GraphEdge)).scalars())
    assert len(edges) == 1, "the declared MOUNTED_IN edge must be materialised"
    assert edges[0].edge_type == "MOUNTED_IN"
    assert edges[0].authority == GraphEdgeAuthority.AUTO.value
    assert counts["nodes"]["Widget"] == 1
    assert counts["edges"]["MOUNTED_IN"] == 1


def test_node_is_resource_backed_and_carries_declared_attributes(session):
    from infra_brain import graph_engine

    r = _resource(session, domain="widgets", rtype="widget", name="w1", metadata={"rack": "r1"})
    graph_engine.emit_all(session, specs={"widgets": _WIDGET_SPEC})

    node = session.execute(select(GraphNode).where(GraphNode.node_type == "Widget")).scalar_one()
    assert node.resource_id == r.id, "a resource-backed node must link to its resource row"
    assert node.source == "widgets", "node.source is the declaring collector's domain"
    assert node.attributes == {"rack": "r1"}, "declared attributes must be copied onto the node"


def test_a_one_to_many_edge_is_declared_as_an_inverse(session):
    """The gap this file used to pin as a boundary, now closed — and how.

    THIS TEST REPLACES ``test_a_one_to_many_edge_cannot_be_declared``. That test
    asserted 3 edges one way and 0 the other, because ``EdgeSpec`` could only
    ever store the arrow in join order and ``_target_index`` resolves a key to
    ONE target. The fix was in the CONTRACT, exactly as that test said it had to
    be: ``EdgeDirection.INVERSE`` keeps the functional many-to-one join and
    stores the resulting edge backwards, so N join rows resolving to one node
    become N edges fanning OUT of it.

    Both directions of the same relationship are declared here simultaneously,
    over one fixture, to make the equivalence visible: ``MOUNTED_IN`` (forward,
    widget -> rack) and ``HOLDS`` (inverse, rack -> widget) are the same three
    facts stored twice, and neither required the ambiguity rule to move.

    ``_target_index`` is UNCHANGED by this feature. What remains impossible is
    pinned in ``test_a_many_to_many_relationship_is_still_not_expressible`` and
    ``test_inverse_still_refuses_to_guess_between_two_join_targets``.
    """
    from infra_brain import graph_engine

    rack = _resource(session, domain="racks", rtype="rack", name="r1")
    widgets = [
        _resource(session, domain="widgets", rtype="widget", name=name, metadata={"rack": "r1"})
        for name in ("w1", "w2", "w3")
    ]

    counts, errors = graph_engine.emit_all(
        session, specs={"widgets": _WIDGET_SPEC_BOTH_DIRECTIONS, "racks": _RACK_SPEC}
    )

    assert errors == [], errors
    # Many-to-one, unchanged.
    assert counts["edges"]["MOUNTED_IN"] == 3
    # One-to-many, now expressible — three edges out of the ONE rack.
    assert counts["edges"]["HOLDS"] == 3

    rack_node = session.execute(select(GraphNode).where(GraphNode.node_type == "Rack")).scalar_one()
    widget_nodes = {
        n.resource_id: n
        for n in session.execute(select(GraphNode).where(GraphNode.node_type == "Widget")).scalars()
    }
    holds = list(session.execute(select(GraphEdge).where(GraphEdge.edge_type == "HOLDS")).scalars())
    assert len(holds) == 3
    assert {e.source_id for e in holds} == {rack_node.id}, (
        "every HOLDS edge must START at the one rack — that is the fan-out"
    )
    assert {e.target_id for e in holds} == {widget_nodes[w.id].id for w in widgets}
    assert rack_node.resource_id == rack.id

    # The stored arrow is the reverse of the join, so the evidence has to say
    # so — otherwise a reader cannot tell which end carried the join key, and
    # therefore which end the ambiguity guarantee applies to.
    assert {e.evidence["direction"] for e in holds} == {EdgeDirection.INVERSE.value}
    mounted = list(
        session.execute(select(GraphEdge).where(GraphEdge.edge_type == "MOUNTED_IN")).scalars()
    )
    assert {e.evidence["direction"] for e in mounted} == {EdgeDirection.FORWARD.value}


def test_inverse_still_refuses_to_guess_between_two_join_targets(session):
    """INVERSE buys a direction, NOT permission to guess. The guarantee holds.

    Two Rack nodes answer to the join key. Under FORWARD that is the classic
    ambiguity case; under INVERSE it would mean "these widgets belong to one of
    two racks and we do not know which", which is exactly as unanswerable. The
    engine runs the identical index and gives the identical refusal.
    """
    from infra_brain import graph_engine

    graph_phase2.upsert_node(session, node_type="Rack", natural_key="a", name="r1", source="racks")
    graph_phase2.upsert_node(session, node_type="Rack", natural_key="b", name="r1", source="racks")
    for name in ("w1", "w2", "w3"):
        _resource(session, domain="widgets", rtype="widget", name=name, metadata={"rack": "r1"})

    counts, errors = graph_engine.emit_all(session, specs={"widgets": _WIDGET_SPEC_BOTH_DIRECTIONS})

    assert counts["edges"].get("HOLDS", 0) == 0
    assert (
        list(session.execute(select(GraphEdge).where(GraphEdge.edge_type == "HOLDS")).scalars())
        == []
    )
    assert any("HOLDS" in e and "ambiguous" in e for e in errors), errors


def test_a_many_to_many_relationship_is_still_not_expressible(session):
    """THE BOUNDARY THAT REMAINS, pinned so it stays a decision.

    ``EdgeDirection`` makes one-to-many declarable because a one-to-many join is
    functional from its "many" side — the key lives there (a widget knows its
    rack; one project's files each carry ``project_id``). A MANY-TO-MANY
    relationship is functional from NEITHER side, so there is no direction to
    declare it from: two racks and three widgets all answering to key "r1" is
    six possible pairings and no evidence for any of them.

    Both directions are attempted below over the same fixture and both are
    refused, loudly. This must not be "fixed" by indexing a key to a LIST of
    targets: the engine cannot distinguish a legitimate fan-out from a
    contradiction, which is the whole reason the direction is declared instead
    of inferred. A genuine many-to-many needs a join TABLE — an entity of its
    own, with its own ``resources`` rows and two functional edges — not a
    softer matching rule.

    The error is reported, not swallowed: a declaration that silently produced
    nothing would look like "no data" instead of "not expressible".
    """
    from infra_brain import graph_engine

    for name in ("rackA", "rackB"):
        _resource(session, domain="racks", rtype="rack", name=name, metadata={"rack": "r1"})
    for name in ("w1", "w2", "w3"):
        _resource(session, domain="widgets", rtype="widget", name=name, metadata={"rack": "r1"})

    # Forward, from the rack side: three candidate Widget targets for key "r1".
    counts, errors = graph_engine.emit_all(
        session, specs={"racks": _RACK_SPEC_FORWARD_FANOUT, "widgets": _WIDGET_SPEC}
    )
    assert counts["edges"]["HOLDS"] == 0
    assert any("HOLDS" in e and "ambiguous" in e for e in errors), errors

    # Inverse, from the widget side: two candidate Rack targets for key "r1".
    # ``_RACK_SPEC_FORWARD_FANOUT``'s Rack node is reused so the join key is on
    # both sides, which is what makes this many-to-many rather than one-to-many.
    inverse_only = AgentSpec(
        domain="widgets",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=timedelta(hours=1),
        emits_nodes=_WIDGET_SPEC.emits_nodes,
        emits_edges=(
            EdgeSpec(
                type="HELD_BY",
                from_node="Widget",
                to_node="Rack",
                from_key="attributes.rack",
                to_key="attributes.rack",
                direction=EdgeDirection.INVERSE,
                method=GraphEdgeMethod.DETERMINISTIC_MATCH,
                confidence=Decimal("0.990"),
            ),
        ),
    )
    counts, errors = graph_engine.emit_all(session, specs={"widgets": inverse_only})
    assert counts["edges"]["HELD_BY"] == 0
    assert any("HELD_BY" in e and "ambiguous" in e for e in errors), errors


def test_collector_declaring_nothing_produces_nothing(session):
    """P0 must be a strict no-op for every existing spec.

    Every one of the ~30 shipped AgentSpecs declares nothing, so the engine
    must write zero rows for them. This is what makes P0 trivially reversible.
    """
    from infra_brain import graph_engine

    _resource(session, domain="silent", rtype="anything", name="s1", metadata={"x": "y"})

    counts, errors = graph_engine.emit_all(session, specs={"silent": _SILENT_SPEC})

    assert errors == []
    assert counts == {"nodes": {}, "edges": {}}
    assert session.execute(select(GraphNode)).scalars().all() == []
    assert session.execute(select(GraphEdge)).scalars().all() == []


def test_real_registry_declares_nothing_beyond_the_migrated_domains(session):
    """Guard against an accidental fleet-wide behaviour change.

    Each phase migrates a deliberately small set. If a future change makes
    another domain start emitting, that must be a conscious edit to this list.

    P1: homelab_services + linux (Service -RUNS_ON-> Host).
    P2: iac + cicd (IaC file -BELONGS_TO-> GitlabProject, its inverse
        DEFINED_IN, and compose file -RUNS_IMAGE-> ContainerImage). The set is
        unchanged by the RUNS_IMAGE migration — iac was already emitting — which
        is the point: closing a contract gap should widen what a collector can
        SAY, not which collectors are involved.
    P4: pki (Certificate -ISSUED_BY-> CertificateAuthority) — the FIFTH domain,
        and the first one added since P1, so this line is exactly the conscious
        edit the guard exists to demand. ANSIBLE_MANAGES landed in the same
        phase and did NOT widen the set: it stayed iac's, re-anchored onto the
        AnsibleInventoryFile iac owns (TRK-354 Option A), which is the
        difference between answering an ownership question and routing around
        it.
    P5: netdiscovery — the SIXTH domain, and the first added for a reason other
        than an edge. It declares one node and no relationships, because what
        the identity resolver needed was not a new fact ABOUT a host but the
        host itself: ``graph_phase3.resolve_entities`` can only consider node
        types that exist, so retiring ``host_reconcile``'s IS_SAME_AS writer
        required this leg to have a node first. See
        tests/test_p5_issameas_resolver_coverage.py.
    TRK-359: eol — the SEVENTH domain, restoring the second of P5's two
        accepted losses (RUNS_EOL) via the junction grammar; the first
        (MEMBER_OF) did not widen the set because it is iac's, like every
        inventory-derived fact before it. See
        tests/test_graph_engine_junction.py for the grammar and
        tests/agents/test_eol_runs_eol_graph.py /
        tests/agents/test_iac_member_of_graph.py for the reconstructions.
    """
    from infra_brain.etl.spec import graph_emitting_domains

    assert graph_emitting_domains() == {
        "homelab_services",
        "linux",
        "iac",
        "cicd",
        "pki",
        "netdiscovery",
        "eol",
    }


def test_engine_never_overwrites_a_human_authored_edge(session):
    """Authority model (2026-08-10-graph-edge-authority-spec.md), rule W4.

    A human's assertion outranks the engine's. The engine writes ``auto``;
    upsert_edge must decline rather than downgrade the row.
    """
    from infra_brain import graph_engine

    _resource(session, domain="racks", rtype="rack", name="r1")
    _resource(session, domain="widgets", rtype="widget", name="w1", metadata={"rack": "r1"})
    specs = {"widgets": _WIDGET_SPEC, "racks": _RACK_SPEC}

    # First pass creates the nodes; then a human asserts the edge by hand.
    graph_engine.emit_all(session, specs={"racks": _RACK_SPEC})
    graph_engine.emit_all(
        session,
        specs={
            "widgets": AgentSpec(
                domain="widgets",
                tier=Tier.COLLECTOR,
                schedule=None,
                max_staleness=timedelta(hours=1),
                emits_nodes=_WIDGET_SPEC.emits_nodes,
            )
        },
    )
    widget = session.execute(select(GraphNode).where(GraphNode.node_type == "Widget")).scalar_one()
    rack = session.execute(select(GraphNode).where(GraphNode.node_type == "Rack")).scalar_one()
    human = graph_phase2.upsert_edge(
        session,
        source_id=widget.id,
        target_id=rack.id,
        edge_type="MOUNTED_IN",
        method=GraphEdgeMethod.DECLARED,
        confidence=Decimal("1.000"),
        source="operator:operator",
        authority=GraphEdgeAuthority.HUMAN,
    )
    human_id = human.id

    graph_engine.emit_all(session, specs=specs)

    edges = list(session.execute(select(GraphEdge).where(GraphEdge.valid_to.is_(None))).scalars())
    assert len(edges) == 1, "the engine must not add a rival active edge"
    assert edges[0].id == human_id, "the human edge row must survive untouched"
    assert edges[0].authority == GraphEdgeAuthority.HUMAN.value
    assert edges[0].confidence == Decimal("1.000")
    assert edges[0].source == "operator:operator"


def test_engine_is_idempotent(session):
    from infra_brain import graph_engine

    _resource(session, domain="racks", rtype="rack", name="r1")
    _resource(session, domain="widgets", rtype="widget", name="w1", metadata={"rack": "r1"})
    specs = {"widgets": _WIDGET_SPEC, "racks": _RACK_SPEC}

    graph_engine.emit_all(session, specs=specs)
    graph_engine.emit_all(session, specs=specs)

    assert len(session.execute(select(GraphNode)).scalars().all()) == 2
    assert len(session.execute(select(GraphEdge)).scalars().all()) == 1


def test_missing_target_node_produces_no_edge(session):
    """No target, no edge — never a dangling or invented one."""
    from infra_brain import graph_engine

    _resource(session, domain="widgets", rtype="widget", name="w1", metadata={"rack": "nowhere"})

    counts, errors = graph_engine.emit_all(
        session, specs={"widgets": _WIDGET_SPEC, "racks": _RACK_SPEC}
    )

    assert errors == []
    assert counts["edges"].get("MOUNTED_IN", 0) == 0
    assert session.execute(select(GraphEdge)).scalars().all() == []


def test_null_join_key_produces_no_edge(session):
    """A resource whose join attribute is null must yield no edge at all."""
    from infra_brain import graph_engine

    _resource(session, domain="racks", rtype="rack", name="r1")
    _resource(session, domain="widgets", rtype="widget", name="w1", metadata={"rack": None})

    graph_engine.emit_all(session, specs={"widgets": _WIDGET_SPEC, "racks": _RACK_SPEC})

    assert session.execute(select(GraphEdge)).scalars().all() == [], (
        "a null join key must produce NO edge, not a broken one"
    )


def test_ambiguous_target_is_refused_not_guessed(session):
    """Two candidate targets for one key is a contradiction, not a coin flip."""
    from infra_brain import graph_engine

    # Two Rack nodes normalising to the same key.
    graph_phase2.upsert_node(session, node_type="Rack", natural_key="a", name="r1", source="racks")
    graph_phase2.upsert_node(session, node_type="Rack", natural_key="b", name="r1", source="racks")
    _resource(session, domain="widgets", rtype="widget", name="w1", metadata={"rack": "r1"})

    counts, errors = graph_engine.emit_all(session, specs={"widgets": _WIDGET_SPEC})

    assert counts["edges"].get("MOUNTED_IN", 0) == 0
    assert any("ambiguous" in e.lower() for e in errors), errors


def test_one_failing_declaration_does_not_sink_the_others(session):
    """Each declaration gets its own SAVEPOINT, so a failure is contained.

    Asserted through a failure raised from inside the write path the engine
    actually calls, so the exception it has to survive is a real one rather
    than a convenient mock.

    HONEST LIMIT: this suite runs on sqlite, which tolerates continuing after
    a failed statement. The SAVEPOINT matters on PostgreSQL, where a failed
    flush leaves the connection in InFailedSqlTransaction and every LATER
    declaration would fail too. sqlite can prove the error is reported and the
    healthy collector's work survives; it cannot prove the PostgreSQL half.
    """
    from infra_brain import graph_engine

    _resource(session, domain="racks", rtype="rack", name="r1")
    _resource(session, domain="widgets", rtype="widget", name="w1", metadata={"rack": "r1"})

    real_upsert = graph_phase2.upsert_node
    boom = RuntimeError("collector declaration is broken")

    def _explode(sess, **kw):
        if kw.get("node_type") == "Widget":
            raise boom
        return real_upsert(sess, **kw)

    graph_engine.graph_phase2.upsert_node = _explode
    try:
        counts, errors = graph_engine.emit_all(
            session, specs={"widgets": _WIDGET_SPEC, "racks": _RACK_SPEC}
        )
    finally:
        graph_engine.graph_phase2.upsert_node = real_upsert

    assert any("collector declaration is broken" in e for e in errors), errors
    assert "Widget" not in counts["nodes"]
    # The healthy collector's node survived the other's failure.
    assert counts["nodes"]["Rack"] == 1
    assert [n.node_type for n in session.execute(select(GraphNode)).scalars()] == ["Rack"]


def test_retired_resources_are_not_materialised(session):
    from datetime import datetime, timezone

    from infra_brain import graph_engine

    r = _resource(session, domain="widgets", rtype="widget", name="w1", metadata={"rack": "r1"})
    r.retired_at = datetime.now(timezone.utc)
    session.flush()

    graph_engine.emit_all(session, specs={"widgets": _WIDGET_SPEC})

    assert session.execute(select(GraphNode)).scalars().all() == []


def _engine_ast():
    import ast
    from pathlib import Path

    import infra_brain.graph_engine as ge

    return ast.parse(Path(ge.__file__).read_text())


def test_engine_imports_no_domain_models():
    """The engine must contain ZERO domain knowledge (design doc §5, P0).

    Checked over the AST, not the raw text, so the module is free to *name*
    a domain model in prose while being mechanically prevented from importing
    or referencing one. The moment this fails, the contract has stopped
    abstracting the thing it exists to abstract and the fix belongs in
    ``etl/spec.py``, not here.
    """
    import ast

    tree = _engine_ast()

    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")
            imported_names |= {a.name for a in node.names}

    assert not any(m.startswith("infra_brain.agents") for m in imported_modules), (
        f"the engine must not import any collector: {sorted(imported_modules)}"
    )
    # Only the two GENERIC tables are allowed out of db.models: the resource
    # store every collector writes, and the graph store the engine writes.
    assert imported_names & {"GraphNode", "Resource"} == imported_names & set(_ALL_MODEL_NAMES()), (
        f"the engine imported a domain model: {sorted(imported_names)}"
    )

    identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    identifiers |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    banned = {"LinuxHost", "HomelabService", "VsphereVm", "R7Asset", "OctopusMachine"}
    assert not (identifiers & banned), f"the engine referenced {sorted(identifiers & banned)}"


def _ALL_MODEL_NAMES():
    import infra_brain.db.models as models

    return set(getattr(models, "__all__", dir(models)))


def test_engine_branches_on_no_domain_or_type_name(session):
    """No ``if domain == "linux"`` anywhere — the failure mode being prevented.

    A string literal that equals a live domain / node type / edge type inside
    the engine is the first step back towards a hand-maintained deriver.
    """
    import ast

    from infra_brain.etl.spec import agent_specs

    literals = {
        n.value
        for n in ast.walk(_engine_ast())
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    domains = set(agent_specs())
    assert not (literals & domains), f"the engine hardcodes domain(s) {sorted(literals & domains)}"


def test_edgespec_rejects_dishonest_confidence_at_declaration_time():
    """The confidence-honesty rule holds through the declarative path too.

    ``upsert_edge`` already raises ValueError for confidence 1.000 on a
    non-``declared`` method. Catching it at DECLARATION time is strictly
    better: the mistake is in the spec, and a spec is read long before any
    session exists.
    """
    with pytest.raises(ValueError, match="declared"):
        EdgeSpec(
            type="MOUNTED_IN",
            from_node="Widget",
            to_node="Rack",
            from_key="attributes.rack",
            to_key="name",
            method=GraphEdgeMethod.DETERMINISTIC_MATCH,
            confidence=Decimal("1.000"),
        )


def test_edgespec_direction_defaults_to_forward():
    """Every declaration written before ``direction`` existed keeps its meaning."""
    assert _WIDGET_SPEC.emits_edges[0].direction is EdgeDirection.FORWARD
    assert _WIDGET_SPEC.emits_edges[0].written_as() == ("Widget", "Rack")
    assert _WIDGET_SPEC_BOTH_DIRECTIONS.emits_edges[1].written_as() == ("Rack", "Widget")


def test_edgespec_rejects_an_unknown_direction():
    """A typo'd direction must fail at declaration, not store every edge backwards."""
    with pytest.raises(ValueError, match="direction"):
        EdgeSpec(
            type="HOLDS",
            from_node="Widget",
            to_node="Rack",
            from_key="attributes.rack",
            to_key="name",
            direction="reverse",
        )


def test_an_inverse_edge_is_still_declared_out_of_an_owned_node():
    """``direction`` moves the arrow; it does not loosen the ownership rule.

    ``from_node`` is the JOIN's start and must still be a type the declaring
    spec owns. Reversing the stored arrow is not a way to assert relationships
    out of another collector's entities — the engine still enumerates only this
    domain's nodes, and still makes exactly one assertion per node it collected.
    """
    with pytest.raises(ValueError, match="emits_nodes"):
        AgentSpec(
            domain="widgets",
            tier=Tier.COLLECTOR,
            schedule=None,
            max_staleness=timedelta(hours=1),
            emits_edges=(
                EdgeSpec(
                    type="HOLDS",
                    from_node="NotDeclared",
                    to_node="Rack",
                    from_key="attributes.rack",
                    to_key="name",
                    direction=EdgeDirection.INVERSE,
                ),
            ),
        )


def test_nodespec_rejects_an_unknown_field_reference():
    """A typo'd field ref must fail loudly at declaration, not silently emit nothing."""
    with pytest.raises(ValueError, match="field reference"):
        NodeSpec(type="Widget", resource_type="widget", natural_key="nmae")


def test_edgespec_from_node_must_be_declared_by_the_same_spec():
    """A collector may only emit edges FROM nodes it owns."""
    with pytest.raises(ValueError, match="emits_nodes"):
        AgentSpec(
            domain="widgets",
            tier=Tier.COLLECTOR,
            schedule=None,
            max_staleness=timedelta(hours=1),
            emits_edges=(
                EdgeSpec(
                    type="MOUNTED_IN",
                    from_node="NotDeclared",
                    to_node="Rack",
                    from_key="attributes.rack",
                    to_key="name",
                    method=GraphEdgeMethod.DETERMINISTIC_MATCH,
                    confidence=Decimal("0.990"),
                ),
            ),
        )
