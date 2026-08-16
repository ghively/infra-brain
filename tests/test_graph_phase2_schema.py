"""Schema/model tests for the Relationship Graph Phase 2 tables (issue #126).

Covers: the tables exist with the spec'd columns, node identity is unique,
edge FKs point at graph_nodes, the active-edge partial unique index holds
while retired duplicates are allowed, and the confidence/method honesty rule
is enforced by the store helper.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    GraphEdge,
    GraphEdgeMethod,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    Resource,
    ZONE_CORPORATE,
)
from infra_brain import graph_phase2

from tests.support.pg import make_engine


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        # sqlite does not enforce FKs unless asked; PostgreSQL always does and
        # rejects PRAGMA as a syntax error, so the opt-in is sqlite-only.
        s.execute(select(1))
        if engine.dialect.name == "sqlite":
            s.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
        yield s
        s.rollback()


def _node(session, node_type=GraphNodeType.VSPHERE_VM, key="vc1:vm-1", name="vm-1"):
    return graph_phase2.upsert_node(
        session, node_type=node_type, natural_key=key, name=name, source="vsphere"
    )


def test_tables_and_columns_exist(session):
    insp = inspect(session.get_bind())
    assert "graph_nodes" in insp.get_table_names()
    assert "graph_edges" in insp.get_table_names()

    node_cols = {c["name"] for c in insp.get_columns("graph_nodes")}
    assert {
        "id",
        "node_type",
        "natural_key",
        "name",
        "source",
        "resource_id",
        "attributes",
        "first_seen",
        "last_seen",
    } <= node_cols

    edge_cols = {c["name"]: c for c in insp.get_columns("graph_edges")}
    assert {
        "id",
        "source_id",
        "target_id",
        "edge_type",
        "method",
        "confidence",
        "evidence",
        "source",
        "valid_from",
        "valid_to",
        "recorded_at",
    } <= set(edge_cols)
    # Spec: only valid_to and evidence are nullable on graph_edges.
    assert edge_cols["valid_to"]["nullable"] is True
    assert edge_cols["evidence"]["nullable"] is True
    for required in ("source_id", "target_id", "edge_type", "method", "confidence", "source"):
        assert edge_cols[required]["nullable"] is False, required


def test_confidence_is_millesimal_numeric(session):
    """NUMERIC(4,3) per spec — three decimal places must survive a round-trip."""
    insp = inspect(session.get_bind())
    col = {c["name"]: c for c in insp.get_columns("graph_edges")}["confidence"]
    assert col["type"].precision == 4
    assert col["type"].scale == 3

    a, b = _node(session), _node(session, key="vc1:host-1", name="esx1")
    graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=GraphEdgeType.HOSTED_ON,
        method=GraphEdgeMethod.DETERMINISTIC_MATCH,
        confidence=Decimal("0.875"),
        source="test",
    )
    session.commit()
    stored = session.execute(select(GraphEdge.confidence)).scalar_one()
    assert Decimal(str(stored)) == Decimal("0.875")


def test_node_identity_is_unique_per_type_and_key(session):
    first = _node(session)
    again = graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.VSPHERE_VM,
        natural_key="vc1:vm-1",
        name="vm-1-renamed",
        source="vsphere",
    )
    # Upsert, not duplicate.
    assert again.id == first.id
    assert again.name == "vm-1-renamed"
    assert session.execute(select(GraphNode)).scalars().all() == [first]

    # Same natural key under a DIFFERENT node type is a different node —
    # per-source node types are the whole point (no unified Host).
    other = _node(session, node_type=GraphNodeType.R7_ASSET, key="vc1:vm-1", name="asset")
    assert other.id != first.id

    # And the constraint really is at the DB level.
    session.add(
        GraphNode(
            id=uuid.uuid4(),
            node_type=GraphNodeType.VSPHERE_VM.value,
            natural_key="vc1:vm-1",
            name="dupe",
            source="vsphere",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_node_links_back_to_existing_resource_uuid_space(session):
    r = Resource(
        id=uuid.uuid4(),
        domain="vsphere",
        type="vsphere_vm",
        name="vm-1",
        source="test",
        zone=ZONE_CORPORATE,
    )
    session.add(r)
    session.flush()
    node = graph_phase2.upsert_node(
        session,
        node_type=GraphNodeType.VSPHERE_VM,
        natural_key="vc1:vm-1",
        name="vm-1",
        source="vsphere",
        resource_id=r.id,
    )
    session.commit()
    assert session.get(GraphNode, node.id).resource_id == r.id


def test_edge_endpoints_must_be_real_nodes(session):
    a = _node(session)
    session.commit()
    session.add(
        GraphEdge(
            id=uuid.uuid4(),
            source_id=a.id,
            target_id=uuid.uuid4(),  # no such node
            edge_type=GraphEdgeType.HOSTED_ON.value,
            method=GraphEdgeMethod.DECLARED.value,
            confidence=Decimal("1.000"),
            source="test",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_one_active_edge_per_triple_but_history_is_retained(session):
    a, b = _node(session), _node(session, key="vc1:host-1", name="esx1")
    e1 = graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=GraphEdgeType.HOSTED_ON,
        method=GraphEdgeMethod.DETERMINISTIC_MATCH,
        confidence=Decimal("0.990"),
        source="test",
    )
    # Re-observation refreshes in place; valid_from keeps the first sighting.
    e2 = graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=GraphEdgeType.HOSTED_ON,
        method=GraphEdgeMethod.DETERMINISTIC_MATCH,
        confidence=Decimal("0.990"),
        source="test",
    )
    assert e1.id == e2.id
    assert e2.valid_from == e1.valid_from
    session.commit()

    # A second ACTIVE row for the same triple is rejected by the partial index.
    session.add(
        GraphEdge(
            id=uuid.uuid4(),
            source_id=a.id,
            target_id=b.id,
            edge_type=GraphEdgeType.HOSTED_ON.value,
            method=GraphEdgeMethod.DETERMINISTIC_MATCH.value,
            confidence=Decimal("0.990"),
            source="test",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    # Retire the active edge, then the same triple may be asserted again —
    # history accumulates instead of being overwritten.
    assert graph_phase2.retire_edges(session, [session.get(GraphEdge, e1.id)]) == 1
    e3 = graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=GraphEdgeType.HOSTED_ON,
        method=GraphEdgeMethod.DETERMINISTIC_MATCH,
        confidence=Decimal("0.990"),
        source="test",
    )
    session.commit()
    assert e3.id != e1.id
    rows = session.execute(select(GraphEdge)).scalars().all()
    assert len(rows) == 2
    assert sum(1 for r in rows if r.valid_to is None) == 1


def test_confidence_one_requires_declared_method(session):
    a, b = _node(session), _node(session, key="CVE-2024-0001", name="CVE-2024-0001")
    with pytest.raises(ValueError, match="only permitted for method='declared'"):
        graph_phase2.upsert_edge(
            session,
            source_id=a.id,
            target_id=b.id,
            edge_type=GraphEdgeType.AFFECTED_BY_CVE,
            method=GraphEdgeMethod.DETERMINISTIC_MATCH,
            confidence=Decimal("1.000"),
            source="test",
        )
    with pytest.raises(ValueError, match=r"within \[0, 1.000\]"):
        graph_phase2.upsert_edge(
            session,
            source_id=a.id,
            target_id=b.id,
            edge_type=GraphEdgeType.AFFECTED_BY_CVE,
            method=GraphEdgeMethod.DECLARED,
            confidence=Decimal("1.500"),
            source="test",
        )


def test_scope_boundary_edge_type_vocabulary_is_closed():
    """Hard scope boundary — DEFINED_IN_IAC must stay unbuilt.

    Phase 2 (#126) shipped the first three. Phase 3 (#127) added ``SAME_AS``
    for cross-source entity resolution, which Phase 2's own module docstring
    explicitly anticipated as separate later work ("Cross-source entity
    resolution ... is explicitly separate, later work"). That is a deliberate,
    reviewed extension of the vocabulary — NOT a licence to add more: this
    assertion stays exact so any further edge type has to be argued for in a
    review rather than appearing quietly.

    The authority/rejection design (KG-1/KG-3) added ``NOT_SAME_AS`` as the
    second — and, again, argued-for — extension: the missing NEGATIVE half of
    the ``SAME_AS`` vocabulary, writable only by an accountable human
    (``graph_phase2.upsert_edge`` rule W5). Without it a human "no" had
    nowhere to live except a ProposedAction status the emitters could not see,
    which is precisely why a rejected pair could still be auto-merged.

    ``DEFINED_IN_IAC`` remains the named prohibition: Ansible playbook
    ``hosts:`` fields are unresolved raw strings / Jinja defaults with no
    structured link to inventory groups, so such an edge would be a guess
    dressed as a fact.
    """
    assert {e.value for e in GraphEdgeType} == {
        "HOSTED_ON",
        "MOUNTS_DATASTORE",
        "AFFECTED_BY_CVE",
        "SAME_AS",
        "NOT_SAME_AS",
    }
    assert "DEFINED_IN_IAC" not in {e.value for e in GraphEdgeType}
    assert {m.value for m in GraphEdgeMethod} == {
        "declared",
        "deterministic_match",
        "probabilistic_match",
    }
    # No unified "Host" node type — per-source types only.
    assert "Host" not in {n.value for n in GraphNodeType}
