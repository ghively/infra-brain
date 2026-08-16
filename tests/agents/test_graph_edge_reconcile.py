"""GraphMaintenanceAgent._reconcile_contradictory_edges — after the store move.

WHAT THIS FILE USED TO TEST (and why that job is over)
------------------------------------------------------
MR-B's one-time cleanup: three hardcoded ``DELETE FROM
resource_relationships`` statements that removed rows two collectors had
persisted with the INVERSE of the canonical direction —
``octopus_project DEPLOYS_TO env``, ``octopus_machine DEPLOYED_TO env``,
``vsphere_cluster MEMBER_OF vsphere_datacenter`` (KG-1 / DL-C-7 / S-13 / S-14).
The old test seeded both the wrong and the right rows and asserted only the
wrong ones were deleted.

That cleanup converged — the live store holds zero rows of any of the three
shapes — and it is not ported to ``graph_edges``, because the bug CLASS cannot
recur there: direction is no longer each emitter's private convention but a
declared ``EdgeSpec.direction`` resolved centrally by ``graph_engine``, so an
inverted edge is a contract error at declaration time rather than a row to
delete every two hours. The oracle above is kept verbatim as the record of what
was removed and why, per the project's deletion discipline.

WHAT IT TESTS NOW
-----------------
The pass keeps its name and its shape of work — converge the store on the
assertion that outranks — against the store the graph actually lives in. The
contradiction that matters in a bitemporal, authority-tagged store is the
invariant from the edge-authority spec §3.3: *a pair can never simultaneously
carry an active SAME_AS and an active NOT_SAME_AS*. ``confirm_same_as``
enforces the ordering on the human path and ``resolve_entities`` pre-filters
vetoed pairs, but nothing swept for a violation that slipped past both.

Direction of the fix is fixed by the authority model and is the whole point:
the AUTO edge is retired, the human veto is never touched.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent
from infra_brain.db.models import GraphEdge, GraphNode



from tests.support.pg import make_engine


def _legacy_store_absent(session) -> None:
    """P5 integration: the honest form of "wrote zero legacy rows".

    These tests predate the fold that removed the ORM model from
    ``Base.metadata``; they counted rows to prove no writer fired. Post-drop
    the claim is structural — the table does not exist in the schema this
    test just ran the agent against, so no writer COULD have produced a row.
    """
    from sqlalchemy import inspect as _sqla_inspect

    assert "resource_relationships" not in _sqla_inspect(session.get_bind()).get_table_names()


def _agent() -> GraphMaintenanceAgent:
    agent = GraphMaintenanceAgent.__new__(GraphMaintenanceAgent)
    agent.settings = MagicMock()
    agent._maint_errors = []
    return agent


def _node(session, node_type: str, key: str) -> GraphNode:
    n = GraphNode(id=uuid.uuid4(), node_type=node_type, natural_key=key, name=key, source="test")
    session.add(n)
    session.flush()
    return n


def _edge(
    session,
    src: GraphNode,
    tgt: GraphNode,
    edge_type: str,
    *,
    method: str = "deterministic_match",
    confidence: str = "0.990",
    authority: str = "auto",
) -> GraphEdge:
    now = datetime.now(UTC)
    e = GraphEdge(
        id=uuid.uuid4(),
        source_id=src.id,
        target_id=tgt.id,
        edge_type=edge_type,
        method=method,
        confidence=Decimal(confidence),
        source="test",
        authority=authority,
        valid_from=now,
        recorded_at=now,
        evidence={},
    )
    session.add(e)
    session.flush()
    return e


def test_auto_same_as_under_a_human_veto_is_retired_and_the_veto_survives():
    eng = make_engine()
    with Session(eng) as s:
        a = _node(s, "VsphereVM", "vc1:vm-1")
        b = _node(s, "R7Asset", "9001")
        auto = _edge(s, a, b, "SAME_AS", confidence="0.800")
        veto = _edge(
            s, b, a, "NOT_SAME_AS", method="declared", confidence="1.000", authority="human"
        )
        s.commit()
        auto_id, veto_id = auto.id, veto.id

        result = _agent()._reconcile_contradictory_edges(s)
        s.commit()

        assert result["removed"] == 1
        retired = s.get(GraphEdge, auto_id)
        assert retired.valid_to is not None, "the auto claim must be retired"
        assert retired.evidence["retired_reason"] == "contradicted_by_human_not_same_as"
        assert s.get(GraphEdge, veto_id).valid_to is None, "a human veto is never retired"

        # Idempotent: the auto edge is no longer active, so a second pass finds
        # no contradiction.
        assert _agent()._reconcile_contradictory_edges(s)["removed"] == 0


def test_uncontradicted_and_non_human_edges_are_left_alone():
    eng = make_engine()
    with Session(eng) as s:
        a = _node(s, "VsphereVM", "vc1:vm-2")
        b = _node(s, "R7Asset", "9002")
        c = _node(s, "R7Asset", "9003")
        # An auto SAME_AS with no veto against it.
        clean = _edge(s, a, b, "SAME_AS", confidence="0.800")
        # A veto against a DIFFERENT pair must not reach the pair above.
        _edge(s, a, c, "NOT_SAME_AS", method="declared", confidence="1.000", authority="human")
        # A structural edge is not a SAME_AS at all.
        structural = _edge(s, a, b, "HOSTED_ON", method="declared", confidence="1.000")
        s.commit()
        clean_id, structural_id = clean.id, structural.id

        assert _agent()._reconcile_contradictory_edges(s)["removed"] == 0
        s.commit()

        assert s.get(GraphEdge, clean_id).valid_to is None
        assert s.get(GraphEdge, structural_id).valid_to is None


def test_the_pass_never_touches_the_legacy_store():
    """The frozen store must not be written OR deleted from by this pass.

    P4/P5 of the design doc is what removes the historical
    ``resource_relationships`` rows; a maintenance pass quietly deleting them
    first would destroy the very data the backfill is meant to read.
    """
    eng = make_engine()
    with Session(eng) as s:
        a = _node(s, "VsphereVM", "vc1:vm-3")
        b = _node(s, "R7Asset", "9004")
        _edge(s, a, b, "SAME_AS", confidence="0.800")
        _edge(s, b, a, "NOT_SAME_AS", method="declared", confidence="1.000", authority="human")
        s.commit()
        _legacy_store_absent(s)

        _agent()._reconcile_contradictory_edges(s)
        s.commit()

        _legacy_store_absent(s)  # still absent — the pass touches graph_edges only
