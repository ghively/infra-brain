"""``ChildSpec`` — the generic half of "a NodeSpec can reach a child table".

Companion to ``tests/test_graph_engine.py`` and held to the same discipline:
every declaration here is over FICTIONAL domains and FICTIONAL tables, so if
the engine ever needs to know what a "widget part" is to make these pass, the
contract is wrong and the fix belongs in ``etl/spec.py``.

The fictional child tables are declared against ``Base.metadata`` on purpose.
That is not a shortcut — it is the mechanism under test: the engine resolves a
``ChildSpec``'s path by looking table names up in the shared ``MetaData`` that
every model registers into, never by importing a model class. A table this file
invents is therefore reachable by exactly the same route
``compose_services`` is, which is what proves the reach is generic.

Four properties are load-bearing:

1. ``gathers`` puts a child column's values onto the PARENT node, as a list.
2. ``from_rows`` turns child values into shared value NODES, one per distinct
   value however many parents claim it.
3. ``from_key_multi`` fans a gathered list into one edge per value, with each
   value still resolved through the unchanged one-target index.
4. None of that moves the ambiguity rule — see
   ``test_a_multi_valued_source_key_still_refuses_an_ambiguous_target``.
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
from infra_brain.etl.spec import AgentSpec, ChildSpec, EdgeSpec, NodeSpec, Tier

from tests.support.pg import make_engine


# --- fictional child tables -------------------------------------------------
#
# ``widget_assemblies`` hangs off ``resources`` (like ``iac_files``), and
# ``widget_parts`` hangs off it (like ``compose_services``) — a TWO-hop path,
# because the shallow one-hop case would not exercise the chain walk at all.

# ``Uuid`` (not String) because that is what every real table in this schema
# uses for a primary key — a child path is walked by PK equality, so the
# fictional tables have to key the same way the real ones do or the test would
# be exercising a join the engine never actually performs.
widget_assemblies = Table(
    "widget_assemblies",
    Base.metadata,
    Column("id", Uuid(), primary_key=True),
    Column("resource_id", Uuid(), ForeignKey("resources.id"), nullable=True),
    extend_existing=True,
)

widget_parts = Table(
    "widget_parts",
    Base.metadata,
    Column("id", Uuid(), primary_key=True),
    Column("assembly_id", Uuid(), ForeignKey("widget_assemblies.id")),
    Column("part", String(128), nullable=True),
    # A JSON-list column, to pin that the engine REFUSES rather than flattens.
    Column("tags", JSONB, nullable=True),
    extend_existing=True,
)

_PART_PATH = (("widget_assemblies", "resource_id"), ("widget_parts", "assembly_id"))

_PARTS = ChildSpec(key="parts", path=_PART_PATH, column="part")
_TAGS = ChildSpec(key="tags", path=_PART_PATH, column="tags")


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        yield s
        s.rollback()


def _widget(session, name, parts, *, tags=None):
    """A widget resource with an assembly carrying ``parts`` child rows."""
    resource = Resource(
        id=uuid.uuid4(),
        domain="widgets",
        type="widget",
        name=name,
        source="test",
        metadata_={},
    )
    session.add(resource)
    session.flush()
    assembly_id = uuid.uuid4()
    session.execute(widget_assemblies.insert().values(id=assembly_id, resource_id=resource.id))
    for part in parts:
        session.execute(
            widget_parts.insert().values(
                id=uuid.uuid4(), assembly_id=assembly_id, part=part, tags=tags
            )
        )
    session.flush()
    return resource


# --- declarations -----------------------------------------------------------

_WIDGET_NODE = NodeSpec(
    type="Widget",
    resource_type="widget",
    natural_key="name",
    gathers=(_PARTS,),
)

_PART_NODE = NodeSpec(
    type="Part",
    resource_type="widget",
    natural_key="rows.parts",
    name="rows.parts",
    resource_backed=False,
    from_rows=_PARTS,
)

_USES_PART = EdgeSpec(
    type="USES_PART",
    from_node="Widget",
    to_node="Part",
    from_key="attributes.parts",
    to_key="natural_key",
    from_key_multi=True,
    method="declared",
    confidence=Decimal("1.000"),
)

_WIDGET_SPEC = AgentSpec(
    domain="widgets",
    tier=Tier.COLLECTOR,
    schedule=None,
    max_staleness=timedelta(hours=1),
    emits_nodes=(_WIDGET_NODE, _PART_NODE),
    emits_edges=(_USES_PART,),
)


def _specs():
    return {"widgets": _WIDGET_SPEC}


def _nodes(session, node_type):
    return list(
        session.execute(select(GraphNode).where(GraphNode.node_type == node_type)).scalars()
    )


def _edges(session, edge_type="USES_PART"):
    return list(
        session.execute(select(GraphEdge).where(GraphEdge.edge_type == edge_type)).scalars()
    )


# --- 1. gathers -------------------------------------------------------------


def test_gathered_child_values_ride_onto_the_parent_node(session):
    """The fact lives two tables away; the node has to carry it or no edge can.

    The engine matches GRAPH-side, so a value left in a detail table is
    invisible to an EdgeSpec. ``gathers`` is the whole of the fix.
    """
    from infra_brain import graph_engine

    _widget(session, "w1", ["bolt", "nut"])

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    assert counts["nodes"]["Widget"] == 1
    node = _nodes(session, "Widget")[0]
    assert node.attributes == {"parts": ["bolt", "nut"]}


def test_gathered_values_are_distinct_and_sorted(session):
    """Two child rows naming the same value are one value, deterministically.

    The hand-written deriver this replaces deduped on ``(anchor, node)``; a
    distinct list reproduces that without the engine knowing why.
    """
    from infra_brain import graph_engine

    _widget(session, "w1", ["nut", "bolt", "nut"])

    graph_engine.emit_all(session, specs=_specs())

    assert _nodes(session, "Widget")[0].attributes == {"parts": ["bolt", "nut"]}


def test_a_parent_with_no_child_rows_has_the_key_ABSENT(session):
    """Absent, not empty — and the difference is load-bearing.

    An empty list would be an assertion ("this widget has no parts"). Absence
    is the honest reading of "the detail table has nothing for it", which is
    also what a never-parsed file looks like. Either way no edge is written,
    but only one of the two is a claim.
    """
    from infra_brain import graph_engine

    _widget(session, "w1", [])

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    node = _nodes(session, "Widget")[0]
    assert node.attributes is None
    assert counts["edges"].get("USES_PART", 0) == 0


def test_a_child_row_of_another_resource_type_is_not_gathered(session):
    """The child population is scoped by the SAME filter the node population is."""
    from infra_brain import graph_engine

    _widget(session, "w1", ["bolt"])
    other = Resource(
        id=uuid.uuid4(), domain="widgets", type="gadget", name="g1", source="test", metadata_={}
    )
    session.add(other)
    session.flush()
    assembly_id = uuid.uuid4()
    session.execute(widget_assemblies.insert().values(id=assembly_id, resource_id=other.id))
    session.execute(
        widget_parts.insert().values(id=uuid.uuid4(), assembly_id=assembly_id, part="sprocket")
    )
    session.flush()

    graph_engine.emit_all(session, specs=_specs())

    assert _nodes(session, "Widget")[0].attributes == {"parts": ["bolt"]}
    assert {n.natural_key for n in _nodes(session, "Part")} == {"bolt"}


def test_a_retired_resource_contributes_no_child_values(session):
    """Retirement has to reach the child rows too, or a deleted file keeps voting."""
    from datetime import datetime, timezone

    from infra_brain import graph_engine

    resource = _widget(session, "w1", ["bolt"])
    resource.retired_at = datetime.now(timezone.utc)
    session.flush()

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    assert counts["nodes"] == {"Widget": 0, "Part": 0}
    assert _nodes(session, "Part") == []


def test_a_list_valued_child_column_is_refused_not_flattened(session):
    """A JSON-list column is a real shape here, and guessing at it is the risk.

    ``_resolve`` has always refused a list-valued field reference outright.
    ``ChildSpec`` keeps that refusal rather than quietly flattening: turning
    one row into N keys is a fan-out the declaration never asked for, and
    ``ansible_playbook_plays.hosts`` is exactly such a column sitting in this
    schema waiting to be mis-read.
    """
    from infra_brain import graph_engine

    _widget(session, "w1", ["bolt"], tags=["red", "blue"])
    spec = AgentSpec(
        domain="widgets",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=timedelta(hours=1),
        emits_nodes=(
            NodeSpec(type="Widget", resource_type="widget", natural_key="name", gathers=(_TAGS,)),
        ),
    )

    counts, errors = graph_engine.emit_all(session, specs={"widgets": spec})

    assert errors == [], errors
    assert counts["nodes"]["Widget"] == 1
    assert _nodes(session, "Widget")[0].attributes is None


# --- 2. from_rows -----------------------------------------------------------


def test_child_values_become_shared_value_nodes(session):
    """One node per DISTINCT value, however many parents claim it.

    This is what makes "which widgets use a bolt" answerable at all. It is also
    why the node carries no ``resource_id``: it has two owning resources.
    """
    from infra_brain import graph_engine

    _widget(session, "w1", ["bolt", "nut"])
    _widget(session, "w2", ["bolt"])

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    parts = _nodes(session, "Part")
    assert {n.natural_key for n in parts} == {"bolt", "nut"}
    assert counts["nodes"]["Part"] == 2, "the shared value is materialised ONCE"
    assert all(n.resource_id is None for n in parts)
    assert all(n.attributes == {"parts": n.natural_key} for n in parts)


def test_from_rows_nodes_are_idempotent(session):
    from infra_brain import graph_engine

    _widget(session, "w1", ["bolt"])
    graph_engine.emit_all(session, specs=_specs())
    graph_engine.emit_all(session, specs=_specs())

    assert len(_nodes(session, "Part")) == 1


# --- 3. from_key_multi ------------------------------------------------------


def test_a_multi_valued_source_key_writes_one_edge_per_value(session):
    """The fan-out the contract gained, stated as data.

    Two widgets, three parts between them, four (widget, part) facts — and each
    of the four resolved through the SAME one-target index a single-valued key
    uses. Nothing about matching changed; only how many keys a node holds.
    """
    from infra_brain import graph_engine

    w1 = _widget(session, "w1", ["bolt", "nut", "washer"])
    w2 = _widget(session, "w2", ["bolt"])

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    assert counts["edges"]["USES_PART"] == 4
    by_natural_key = {n.natural_key: n for n in _nodes(session, "Part")}
    widgets = {n.resource_id: n for n in _nodes(session, "Widget")}
    pairs = {(e.source_id, e.target_id) for e in _edges(session)}
    assert pairs == {
        (widgets[w1.id].id, by_natural_key["bolt"].id),
        (widgets[w1.id].id, by_natural_key["nut"].id),
        (widgets[w1.id].id, by_natural_key["washer"].id),
        (widgets[w2.id].id, by_natural_key["bolt"].id),
    }
    assert {e.evidence["raw_value"] for e in _edges(session)} == {"bolt", "nut", "washer"}


def test_a_single_valued_key_is_unchanged_by_the_multi_flag(session):
    """``from_key_multi`` is opt-in; without it a list still writes NOTHING.

    Left as it was on purpose. A declaration written before this field existed
    that meets a list-valued attribute gets the old answer — no edge — rather
    than silently starting to fan out.
    """
    from infra_brain import graph_engine

    _widget(session, "w1", ["bolt", "nut"])
    spec = AgentSpec(
        domain="widgets",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=timedelta(hours=1),
        emits_nodes=(_WIDGET_NODE, _PART_NODE),
        emits_edges=(
            EdgeSpec(
                type="USES_PART",
                from_node="Widget",
                to_node="Part",
                from_key="attributes.parts",
                to_key="natural_key",
                method="declared",
                confidence=Decimal("1.000"),
            ),
        ),
    )

    counts, errors = graph_engine.emit_all(session, specs={"widgets": spec})

    assert errors == [], errors
    assert counts["edges"]["USES_PART"] == 0
    assert _edges(session) == []


def test_a_multi_valued_source_key_still_refuses_an_ambiguous_target(session):
    """THE guarantee, unmoved. Several source keys, still one target each.

    A rival Part node claiming ``bolt`` makes that key a contradiction, and the
    refusal is the identical one a single-valued key gets — the other values
    are unaffected, because the ambiguity is a property of the TARGET index,
    which ``from_key_multi`` does not touch. This is the assertion that would
    fail if the fan-out had been implemented by letting a key have several
    targets.
    """
    from infra_brain import graph_engine, graph_phase2

    _widget(session, "w1", ["bolt", "nut"])
    graph_phase2.upsert_node(
        session, node_type="Part", natural_key="bolt-imposter", name="bolt", source="widgets"
    )
    # The rival answers to the same key only because the join reads ``name``.
    spec = AgentSpec(
        domain="widgets",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=timedelta(hours=1),
        emits_nodes=(_WIDGET_NODE, _PART_NODE),
        emits_edges=(
            EdgeSpec(
                type="USES_PART",
                from_node="Widget",
                to_node="Part",
                from_key="attributes.parts",
                to_key="name",
                from_key_multi=True,
                method="declared",
                confidence=Decimal("1.000"),
            ),
        ),
    )

    counts, errors = graph_engine.emit_all(session, specs={"widgets": spec})

    assert any("USES_PART" in e and "ambiguous" in e for e in errors), errors
    assert counts["edges"]["USES_PART"] == 1, "only the uncontested value resolves"
    assert {e.evidence["raw_value"] for e in _edges(session)} == {"nut"}


def test_multi_valued_edges_are_idempotent(session):
    from infra_brain import graph_engine

    _widget(session, "w1", ["bolt", "nut"])
    graph_engine.emit_all(session, specs=_specs())
    graph_engine.emit_all(session, specs=_specs())

    assert len(_edges(session)) == 2


# --- 4. declaration-time refusals ------------------------------------------


def test_an_unknown_child_table_is_reported_not_silently_empty(session):
    """A typo'd table name must be an ERROR, not "no data".

    It cannot be caught at declaration time without ``etl.spec`` importing the
    model package (which would put ``db.models`` on the import path of every
    agent), so it is caught here — inside the per-declaration SAVEPOINT, so the
    rest of the pass still runs.
    """
    from infra_brain import graph_engine

    _widget(session, "w1", ["bolt"])
    spec = AgentSpec(
        domain="widgets",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=timedelta(hours=1),
        emits_nodes=(
            NodeSpec(
                type="Widget",
                resource_type="widget",
                natural_key="name",
                gathers=(ChildSpec(key="parts", path=(("nope", "resource_id"),), column="part"),),
            ),
        ),
    )

    counts, errors = graph_engine.emit_all(session, specs={"widgets": spec})

    assert any("nope" in e for e in errors), errors
    assert counts["nodes"].get("Widget") is None


def test_an_unknown_child_column_is_reported(session):
    from infra_brain import graph_engine

    _widget(session, "w1", ["bolt"])
    spec = AgentSpec(
        domain="widgets",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=timedelta(hours=1),
        emits_nodes=(
            NodeSpec(
                type="Widget",
                resource_type="widget",
                natural_key="name",
                gathers=(ChildSpec(key="parts", path=_PART_PATH, column="nope"),),
            ),
        ),
    )

    _counts, errors = graph_engine.emit_all(session, specs={"widgets": spec})

    assert any("nope" in e for e in errors), errors


def test_a_from_rows_node_must_not_claim_an_owning_resource():
    """A shared value has many parents; claiming one would be a lie."""
    with pytest.raises(ValueError, match="resource_backed"):
        NodeSpec(
            type="Part",
            resource_type="widget",
            natural_key="rows.parts",
            name="rows.parts",
            from_rows=_PARTS,
        )


def test_a_from_rows_node_must_be_identified_by_its_child_row():
    """Reading ``resources.name`` here would give every part the file's name."""
    with pytest.raises(ValueError, match="identified by child rows"):
        NodeSpec(
            type="Part",
            resource_type="widget",
            natural_key="name",
            resource_backed=False,
            from_rows=_PARTS,
        )


def test_a_rows_reference_without_from_rows_is_refused():
    with pytest.raises(ValueError, match="no from_rows"):
        NodeSpec(type="Part", resource_type="widget", natural_key="rows.parts")


def test_a_rows_reference_must_name_the_declared_key():
    with pytest.raises(ValueError, match="from_rows key"):
        NodeSpec(
            type="Part",
            resource_type="widget",
            natural_key="rows.nope",
            name="rows.nope",
            resource_backed=False,
            from_rows=_PARTS,
        )


def test_a_gathered_key_may_not_shadow_a_metadata_attribute():
    with pytest.raises(ValueError, match="also named in attributes"):
        NodeSpec(
            type="Widget",
            resource_type="widget",
            natural_key="name",
            attributes=("parts",),
            gathers=(_PARTS,),
        )


def test_childspec_rejects_a_malformed_path():
    with pytest.raises(ValueError, match="non-empty tuple"):
        ChildSpec(key="parts", path=(), column="part")
    with pytest.raises(ValueError, match="table_name, fk_column"):
        ChildSpec(key="parts", path=(("widget_parts",),), column="part")


def test_from_key_multi_defaults_off_so_old_declarations_keep_their_meaning():
    edge = EdgeSpec(
        type="USES_PART", from_node="Widget", to_node="Part", from_key="name", to_key="name"
    )
    assert edge.from_key_multi is False
