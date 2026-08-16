"""``RowGather`` — the junction half of "a from_rows node can carry a fan-out".

TRK-359. P5 deleted two genuine derivations (``MEMBER_OF``, ``RUNS_EOL``) as
accepted losses because the contract could not express them: a node
materialised FROM child rows (``from_rows``) could not also carry the member
list an edge would fan out over. ``NodeSpec.gathers`` was rightly refused
there — a per-PARENT gathered list on a node whose identity is shared across
parents would mean "whichever parent was materialised last". The junction
grammar closes the gap the other way round: a ``RowGather`` groups the
gathered values by the junction VALUE itself, so a value that recurs under
many parents accumulates the deterministic UNION, and the refusal that
guarded the old gap stays in place.

Companion to ``tests/test_graph_engine_child_rows.py`` and held to the same
discipline: every declaration here is over FICTIONAL domains and FICTIONAL
tables (declared against ``Base.metadata``, the same shared-``MetaData``
mechanism under test), so if the engine ever needs to know what a "crate" is
to make these pass, the contract is wrong and the fix belongs in
``etl/spec.py``.

Two junction shapes are load-bearing, one per accepted loss:

1. DEEPER DESCENT (the ``MEMBER_OF`` shape): the junction table has child rows
   of its own, and the gather's ``path`` descends into them —
   ``resources → depot_crates → depot_crate_boxes`` here,
   ``resources → iac_files → ansible_inventory_groups →
   ansible_inventory_hosts`` in the real declaration.
2. ANCHOR GATHER (the ``RUNS_EOL`` shape): ``path=()`` and the gathered value
   is a field of the ANCHORING ``resources`` row itself —
   the registry row's host here, ``eol_registry.resource_id``'s host in the
   real declaration.

And two properties must NOT move:

3. The one-target ambiguity index is untouched — a fanned value two targets
   claim is still refused, never guessed.
4. Malformed junction declarations fail AT DECLARATION TIME with a message
   naming the rule, never by silently materialising nothing.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Column, ForeignKey, String, Table, Uuid, select
from sqlalchemy.orm import Session

from infra_brain.db.models import Base, GraphEdge, GraphNode, Resource
from infra_brain.db.models._base import JSONB
from infra_brain.etl.spec import AgentSpec, ChildSpec, EdgeDirection, EdgeSpec, NodeSpec, Tier

from tests.support.pg import make_engine


# --- fictional tables -------------------------------------------------------
#
# ``depot_crates`` hangs off ``resources`` (like ``ansible_inventory_groups``
# hangs off ``iac_files``), and ``depot_crate_boxes`` hangs off it (like
# ``ansible_inventory_hosts``). ``depot_registry`` hangs DIRECTLY off
# ``resources`` (like ``eol_registry``) — its anchor IS the entity the edge
# needs, which is what the anchor-gather shape exists for.

depot_crates = Table(
    "depot_crates",
    Base.metadata,
    Column("id", Uuid(), primary_key=True),
    Column("resource_id", Uuid(), ForeignKey("resources.id"), nullable=True),
    Column("crate", String(128), nullable=True),
    extend_existing=True,
)

depot_crate_boxes = Table(
    "depot_crate_boxes",
    Base.metadata,
    Column("id", Uuid(), primary_key=True),
    Column("crate_id", Uuid(), ForeignKey("depot_crates.id")),
    Column("box", String(128), nullable=True),
    # A JSON-list column, to pin that a junction gather REFUSES rather than
    # flattens — the same refusal ``ChildSpec`` gives.
    Column("labels", JSONB, nullable=True),
    extend_existing=True,
)

depot_registry = Table(
    "depot_registry",
    Base.metadata,
    Column("id", Uuid(), primary_key=True),
    Column("resource_id", Uuid(), ForeignKey("resources.id")),
    Column("flavor", String(128), nullable=True),
    extend_existing=True,
)

_CRATE = ChildSpec(key="crate", path=(("depot_crates", "resource_id"),), column="crate")
_FLAVOR = ChildSpec(key="flavor", path=(("depot_registry", "resource_id"),), column="flavor")


def _row_gather(**kwargs):
    """Build a ``RowGather`` — imported lazily so the declaration-shape tests
    below fail RED (ImportError) on a contract that does not have it yet,
    while the boundary-pinning tests keep passing."""
    from infra_brain.etl.spec import RowGather

    return RowGather(**kwargs)


def _crate_node(**overrides):
    kwargs = dict(
        type="Crate",
        resource_type="manifest",
        natural_key="rows.crate",
        name="rows.crate",
        resource_backed=False,
        from_rows=_CRATE,
        row_gathers=(
            _row_gather(key="boxes", path=(("depot_crate_boxes", "crate_id"),), column="box"),
        ),
    )
    kwargs.update(overrides)
    return NodeSpec(**kwargs)


def _depot_spec(crate_node=None):
    node = crate_node if crate_node is not None else _crate_node()
    return AgentSpec(
        domain="depots",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=timedelta(hours=1),
        emits_nodes=(node,),
        emits_edges=(
            # host-shaped Box ─STORED_IN→ Crate, the MEMBER_OF shape: the join
            # STARTS at the crate (which carries the member list) and the edge
            # is STORED box → crate. Neither ``from_key_multi`` nor ``INVERSE``
            # is new; what is new is only that a from_rows node can carry the
            # list at all.
            EdgeSpec(
                type="STORED_IN",
                from_node="Crate",
                to_node="Box",
                from_key="attributes.boxes",
                to_key="name",
                from_key_multi=True,
                direction=EdgeDirection.INVERSE,
                method="deterministic_match",
                confidence=Decimal("0.900"),
            ),
        ),
    )


def _flavor_spec():
    """The anchor-gather (RUNS_EOL) shape: the junction row's ANCHOR is the
    entity the edge needs, so the gather reads the anchoring resources row."""
    return AgentSpec(
        domain="flavors",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=timedelta(hours=1),
        emits_nodes=(
            NodeSpec(
                type="Flavor",
                resource_type="box",
                domain="boxes",
                natural_key="rows.flavor",
                name="rows.flavor",
                resource_backed=False,
                from_rows=_FLAVOR,
                row_gathers=(_row_gather(key="holders", path=(), column="name"),),
            ),
        ),
        emits_edges=(
            EdgeSpec(
                type="TASTES_OF",
                from_node="Flavor",
                to_node="Box",
                from_key="attributes.holders",
                to_key="name",
                from_key_multi=True,
                direction=EdgeDirection.INVERSE,
                method="deterministic_match",
                confidence=Decimal("0.900"),
            ),
        ),
    )


_BOX_SPEC = AgentSpec(
    domain="boxes",
    tier=Tier.COLLECTOR,
    schedule=None,
    max_staleness=timedelta(hours=1),
    emits_nodes=(NodeSpec(type="Box", resource_type="box", natural_key="name"),),
)


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        yield s
        s.rollback()


def _manifest(session, name, crates):
    """A depots manifest resource with crate junction rows and their box rows.

    ``crates`` is ``{crate_name: [box_name, ...]}``.
    """
    resource = Resource(
        id=uuid.uuid4(),
        domain="depots",
        type="manifest",
        name=name,
        source="test",
        metadata_={},
    )
    session.add(resource)
    session.flush()
    for crate_name, boxes in crates.items():
        crate_id = uuid.uuid4()
        session.execute(
            depot_crates.insert().values(id=crate_id, resource_id=resource.id, crate=crate_name)
        )
        for box in boxes:
            session.execute(
                depot_crate_boxes.insert().values(id=uuid.uuid4(), crate_id=crate_id, box=box)
            )
    session.flush()
    return resource


def _box(session, name):
    resource = Resource(
        id=uuid.uuid4(), domain="boxes", type="box", name=name, source="test", metadata_={}
    )
    session.add(resource)
    session.flush()
    return resource


def _registry_row(session, resource, flavor):
    session.execute(
        depot_registry.insert().values(id=uuid.uuid4(), resource_id=resource.id, flavor=flavor)
    )
    session.flush()


def _nodes(session, node_type):
    return list(
        session.execute(select(GraphNode).where(GraphNode.node_type == node_type)).scalars()
    )


def _edges(session, edge_type):
    return list(
        session.execute(select(GraphEdge).where(GraphEdge.edge_type == edge_type)).scalars()
    )


# --- 0. the boundary the junction grammar replaces --------------------------


def test_a_from_rows_node_still_may_not_gather_per_parent():
    """The OLD refusal stands — ``RowGather`` supersedes it, not relaxes it.

    A per-parent ``gathers`` list on a shared-identity node would mean
    "whichever parent was materialised last". That stays refused; the junction
    grammar exists because the fix was a DIFFERENT grouping (per value), not a
    softer rule.
    """
    with pytest.raises(ValueError, match="gather"):
        NodeSpec(
            type="Crate",
            resource_type="manifest",
            natural_key="rows.crate",
            name="rows.crate",
            resource_backed=False,
            from_rows=_CRATE,
            gathers=(
                ChildSpec(key="boxes", path=(("depot_crate_boxes", "crate_id"),), column="box"),
            ),
        )


# --- 1. deeper descent (the MEMBER_OF shape) --------------------------------


def test_junction_members_ride_onto_the_value_node(session):
    from infra_brain import graph_engine

    _manifest(session, "m1", {"heavy": ["b1", "b2"], "light": ["b3"]})

    counts, errors = graph_engine.emit_all(session, specs={"depots": _depot_spec()})

    assert errors == [], errors
    assert counts["nodes"]["Crate"] == 2
    by_key = {n.natural_key: n for n in _nodes(session, "Crate")}
    assert by_key["heavy"].attributes == {"crate": "heavy", "boxes": ["b1", "b2"]}
    assert by_key["light"].attributes == {"crate": "light", "boxes": ["b3"]}
    assert all(n.resource_id is None for n in by_key.values())


def test_a_value_under_two_anchors_accumulates_the_union(session):
    """THE rule that unblocked the grammar: grouping is per VALUE, per parent.

    One crate name in two manifests is ONE node carrying BOTH manifests'
    boxes — a deterministic union, not "whichever parent came last". This is
    exactly the ``MEMBER_OF`` case: one inventory group name in two inventory
    files manages the union of their hosts.
    """
    from infra_brain import graph_engine

    _manifest(session, "m1", {"heavy": ["b1", "b2"]})
    _manifest(session, "m2", {"heavy": ["b2", "b3"]})

    counts, errors = graph_engine.emit_all(session, specs={"depots": _depot_spec()})

    assert errors == [], errors
    assert counts["nodes"]["Crate"] == 1
    (node,) = _nodes(session, "Crate")
    assert node.attributes == {"crate": "heavy", "boxes": ["b1", "b2", "b3"]}


def test_junction_edges_fan_out_of_the_members(session):
    """host ─MEMBER_OF→ group, spelled generically: box ─STORED_IN→ crate."""
    from infra_brain import graph_engine

    _manifest(session, "m1", {"heavy": ["b1", "b2"], "light": ["b1"]})
    boxes = {name: _box(session, name) for name in ("b1", "b2")}

    counts, errors = graph_engine.emit_all(
        session, specs={"depots": _depot_spec(), "boxes": _BOX_SPEC}
    )

    assert errors == [], errors
    # b1 is in TWO crates — the host-in-two-groups case — so 3 edges, not 2.
    assert counts["edges"]["STORED_IN"] == 3
    crates = {n.natural_key: n for n in _nodes(session, "Crate")}
    box_nodes = {n.natural_key: n for n in _nodes(session, "Box")}
    pairs = {(e.source_id, e.target_id) for e in _edges(session, "STORED_IN")}
    assert pairs == {
        (box_nodes["b1"].id, crates["heavy"].id),
        (box_nodes["b2"].id, crates["heavy"].id),
        (box_nodes["b1"].id, crates["light"].id),
    }
    assert boxes  # fixture used


def test_an_empty_junction_value_has_the_key_absent_and_no_edges(session):
    """A group with ZERO members: node exists, member key ABSENT, no edge.

    Absent, not an empty list — the same absent-key signal ``gathers`` gives a
    parent with no child rows, and load-bearing the same way: an edge over an
    absent key writes nothing.
    """
    from infra_brain import graph_engine

    _manifest(session, "m1", {"empty": []})
    _box(session, "b1")

    counts, errors = graph_engine.emit_all(
        session, specs={"depots": _depot_spec(), "boxes": _BOX_SPEC}
    )

    assert errors == [], errors
    (node,) = _nodes(session, "Crate")
    assert node.attributes == {"crate": "empty"}, "no members -> key ABSENT, not []"
    assert counts["edges"].get("STORED_IN", 0) == 0


def test_a_list_valued_gather_column_is_refused_not_flattened(session):
    from infra_brain import graph_engine

    resource = _manifest(session, "m1", {"heavy": []})
    crate_id = session.execute(select(depot_crates.c.id)).scalar_one()
    session.execute(
        depot_crate_boxes.insert().values(
            id=uuid.uuid4(), crate_id=crate_id, box=None, labels=["a", "b"]
        )
    )
    session.flush()
    node = _crate_node(
        row_gathers=(
            _row_gather(key="labels", path=(("depot_crate_boxes", "crate_id"),), column="labels"),
        ),
    )

    counts, errors = graph_engine.emit_all(session, specs={"depots": _depot_spec(node)})

    assert errors == [], errors
    (crate,) = _nodes(session, "Crate")
    assert crate.attributes == {"crate": "heavy"}, "a JSON-list value is skipped, never flattened"
    assert resource is not None
    assert counts["nodes"]["Crate"] == 1


def test_a_retired_anchor_contributes_no_junction_rows(session):
    from datetime import datetime, timezone

    from infra_brain import graph_engine

    live = _manifest(session, "m1", {"heavy": ["b1"]})
    dead = _manifest(session, "m2", {"heavy": ["b2"], "ghost": ["b3"]})
    dead.retired_at = datetime.now(timezone.utc)
    session.flush()

    counts, errors = graph_engine.emit_all(session, specs={"depots": _depot_spec()})

    assert errors == [], errors
    by_key = {n.natural_key: n for n in _nodes(session, "Crate")}
    assert set(by_key) == {"heavy"}, "a retired manifest's crates must not materialise"
    assert by_key["heavy"].attributes == {"crate": "heavy", "boxes": ["b1"]}
    assert live is not None
    assert counts["nodes"]["Crate"] == 1


def test_junction_nodes_and_edges_are_idempotent(session):
    from infra_brain import graph_engine

    _manifest(session, "m1", {"heavy": ["b1"]})
    _box(session, "b1")
    specs = {"depots": _depot_spec(), "boxes": _BOX_SPEC}

    graph_engine.emit_all(session, specs=specs)
    graph_engine.emit_all(session, specs=specs)

    assert len(_nodes(session, "Crate")) == 1
    assert len(_edges(session, "STORED_IN")) == 1


# --- 2. anchor gather (the RUNS_EOL shape) ----------------------------------


def test_an_anchor_gather_collects_the_anchoring_resources_field(session):
    """``path=()``: the gathered value is the junction row's OWN anchor.

    This is the registry-table shape: ``eol_registry.resource_id`` IS the host
    that runs the product, so the fan-out list is the anchors themselves.
    """
    from infra_brain import graph_engine

    b1, b2, b3 = (_box(session, n) for n in ("b1", "b2", "b3"))
    _registry_row(session, b1, "mint")
    _registry_row(session, b2, "mint")
    _registry_row(session, b3, "plain")

    counts, errors = graph_engine.emit_all(
        session, specs={"flavors": _flavor_spec(), "boxes": _BOX_SPEC}
    )

    assert errors == [], errors
    flavors = {n.natural_key: n for n in _nodes(session, "Flavor")}
    assert flavors["mint"].attributes == {"flavor": "mint", "holders": ["b1", "b2"]}
    assert flavors["plain"].attributes == {"flavor": "plain", "holders": ["b3"]}
    # And the edges: box ─TASTES_OF→ flavor, fanning out of each flavor's list.
    assert counts["edges"]["TASTES_OF"] == 3
    box_nodes = {n.natural_key: n for n in _nodes(session, "Box")}
    pairs = {(e.source_id, e.target_id) for e in _edges(session, "TASTES_OF")}
    assert pairs == {
        (box_nodes["b1"].id, flavors["mint"].id),
        (box_nodes["b2"].id, flavors["mint"].id),
        (box_nodes["b3"].id, flavors["plain"].id),
    }


# --- 3. the guarantees that must not move -----------------------------------


def test_a_fanned_member_with_two_claimant_targets_is_still_refused(session):
    """The one-target ambiguity index is byte-for-byte untouched by junctions."""
    from infra_brain import graph_engine, graph_phase2

    _manifest(session, "m1", {"heavy": ["b1", "b2"]})
    _box(session, "b1")
    _box(session, "b2")
    # A rival Box node claiming b1's key through the joined field.
    graph_phase2.upsert_node(
        session, node_type="Box", natural_key="b1-imposter", name="b1", source="boxes"
    )
    spec = _depot_spec()
    rival_edge = EdgeSpec(
        type="STORED_IN",
        from_node="Crate",
        to_node="Box",
        from_key="attributes.boxes",
        to_key="name",
        from_key_multi=True,
        direction=EdgeDirection.INVERSE,
        method="deterministic_match",
        confidence=Decimal("0.900"),
    )
    spec = AgentSpec(
        domain="depots",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=timedelta(hours=1),
        emits_nodes=spec.emits_nodes,
        emits_edges=(rival_edge,),
    )

    counts, errors = graph_engine.emit_all(session, specs={"depots": spec, "boxes": _BOX_SPEC})

    assert any("STORED_IN" in e and "ambiguous" in e for e in errors), errors
    assert counts["edges"]["STORED_IN"] == 1, "only the uncontested member resolves"


# --- 4. declaration-time refusals -------------------------------------------


def test_row_gathers_without_from_rows_is_refused():
    """A per-value gather is meaningless without a junction to group by."""
    with pytest.raises(ValueError, match="row_gathers requires from_rows"):
        NodeSpec(
            type="Crate",
            resource_type="manifest",
            natural_key="name",
            row_gathers=(
                _row_gather(key="boxes", path=(("depot_crate_boxes", "crate_id"),), column="box"),
            ),
        )


def test_a_row_gather_key_may_not_shadow_the_junction_key():
    with pytest.raises(ValueError, match="from_rows key"):
        _crate_node(
            row_gathers=(
                _row_gather(key="crate", path=(("depot_crate_boxes", "crate_id"),), column="box"),
            ),
        )


def test_duplicate_row_gather_keys_are_refused():
    gather = _row_gather(key="boxes", path=(("depot_crate_boxes", "crate_id"),), column="box")
    with pytest.raises(ValueError, match="duplicate row_gathers key"):
        _crate_node(row_gathers=(gather, gather))


def test_a_row_gather_rejects_a_malformed_path():
    with pytest.raises(ValueError, match="table_name, fk_column"):
        _row_gather(key="boxes", path=(("depot_crate_boxes",),), column="box")
    with pytest.raises(ValueError, match="non-empty string"):
        _row_gather(key="", path=(), column="name")


def test_an_anchor_gather_column_must_be_a_resources_field_ref():
    """``path=()`` reads the resources row, so the column is a FIELD REF and a
    typo must fail at declaration time, not resolve to nothing per row."""
    with pytest.raises(ValueError, match="field reference"):
        _row_gather(key="holders", path=(), column="nmae")


def test_row_gathers_must_be_a_tuple():
    with pytest.raises(ValueError, match="row_gathers must be a tuple"):
        _crate_node(
            row_gathers=[
                _row_gather(key="boxes", path=(("depot_crate_boxes", "crate_id"),), column="box")
            ],
        )


def test_an_unknown_gather_table_is_reported_not_silently_empty(session):
    from infra_brain import graph_engine

    _manifest(session, "m1", {"heavy": ["b1"]})
    node = _crate_node(
        row_gathers=(_row_gather(key="boxes", path=(("nope", "crate_id"),), column="box"),),
    )

    counts, errors = graph_engine.emit_all(session, specs={"depots": _depot_spec(node)})

    assert any("nope" in e for e in errors), errors
    assert counts["nodes"].get("Crate") is None


def test_an_unknown_gather_column_is_reported(session):
    from infra_brain import graph_engine

    _manifest(session, "m1", {"heavy": ["b1"]})
    node = _crate_node(
        row_gathers=(
            _row_gather(key="boxes", path=(("depot_crate_boxes", "crate_id"),), column="nope"),
        ),
    )

    _counts, errors = graph_engine.emit_all(session, specs={"depots": _depot_spec(node)})

    assert any("nope" in e for e in errors), errors
