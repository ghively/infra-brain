"""Batch K MCP tools — relationship-graph traversal (GitLab issue #127).

Covers each of the four tools at the MCP boundary: input validation, the
read-only traversal shapes, the review queue, and confirm_same_as' mutation
gate + commit path.
"""

import contextlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain import graph_phase2, graph_phase3, mcp_server
from infra_brain.db.models import (
    DriftEvent,
    GraphEdge,
    GraphEdgeMethod,
    GraphEdgeType,
    GraphNodeType,
    ProposedAction,
    Resource,
    ZONE_CORPORATE,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextlib.contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.mcp_server.get_session", _get_session):
        yield engine


@pytest.fixture
def mutations_enabled(monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "true")


def _node(session, node_type, key, name, resource_id=None):
    return graph_phase2.upsert_node(
        session,
        node_type=node_type,
        natural_key=key,
        name=name,
        source="test",
        resource_id=resource_id,
    )


def _resource(session, name, domain="vsphere"):
    r = Resource(
        id=uuid.uuid4(), name=name, domain=domain, type="host", source="test", zone=ZONE_CORPORATE
    )
    session.add(r)
    session.flush()
    return r


# ---------------------------------------------------------------------------
# get_blast_radius
# ---------------------------------------------------------------------------


def test_get_blast_radius_rejects_non_uuid(patched_session):
    result = mcp_server.get_blast_radius("not-a-uuid")
    assert "must be a UUID" in result["error"]


def test_get_blast_radius_unknown_node(patched_session):
    result = mcp_server.get_blast_radius(str(uuid.uuid4()))
    assert "not found" in result["error"]


def test_get_blast_radius_with_neighbors(patched_session):
    with Session(patched_session) as s:
        vm = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        esxi = _node(s, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
        graph_phase2.upsert_edge(
            s,
            source_id=vm.id,
            target_id=esxi.id,
            edge_type=GraphEdgeType.HOSTED_ON,
            method=GraphEdgeMethod.DECLARED,
            confidence=Decimal("1.000"),
            source="test",
            evidence={"basis": "fixture"},
        )
        s.commit()
        vm_id = str(vm.id)

    result = mcp_server.get_blast_radius(vm_id)

    assert result["count"] == 1
    assert result["neighbors"][0]["node"]["name"] == "esxi01"
    assert result["neighbors"][0]["hop_distance"] == 1
    assert isinstance(result["neighbors"][0]["why"], str)


def test_get_blast_radius_node_with_no_neighbors(patched_session):
    with Session(patched_session) as s:
        lonely = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-9", "lonely")
        s.commit()
        node_id = str(lonely.id)

    result = mcp_server.get_blast_radius(node_id)

    assert result["neighbors"] == []
    assert result["count"] == 0


def test_get_blast_radius_confidence_filter(patched_session):
    with Session(patched_session) as s:
        vm = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        peer = _node(s, GraphNodeType.R7_ASSET, "1", "web01")
        graph_phase2.upsert_edge(
            s,
            source_id=vm.id,
            target_id=peer.id,
            edge_type=GraphEdgeType.SAME_AS,
            method=GraphEdgeMethod.PROBABILISTIC_MATCH,
            confidence=Decimal("0.800"),
            source="test",
            evidence={"basis": "fixture"},
        )
        s.commit()
        vm_id = str(vm.id)

    assert mcp_server.get_blast_radius(vm_id)["count"] == 0  # declared-only default
    assert mcp_server.get_blast_radius(vm_id, min_confidence=0.8)["count"] == 1


def test_get_blast_radius_hop_cap(patched_session):
    with Session(patched_session) as s:
        vm = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        mid = _node(s, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01")
        far = _node(s, GraphNodeType.VSPHERE_DATASTORE, "vc1:ds-1", "ds1")
        for a, b, t in (
            (vm, mid, GraphEdgeType.HOSTED_ON),
            (mid, far, GraphEdgeType.MOUNTS_DATASTORE),
        ):
            graph_phase2.upsert_edge(
                s,
                source_id=a.id,
                target_id=b.id,
                edge_type=t,
                method=GraphEdgeMethod.DECLARED,
                confidence=Decimal("1.000"),
                source="test",
                evidence={"basis": "fixture"},
            )
        s.commit()
        vm_id = str(vm.id)

    assert mcp_server.get_blast_radius(vm_id, max_hops=1)["count"] == 1
    assert mcp_server.get_blast_radius(vm_id, max_hops=2)["count"] == 2
    assert mcp_server.get_blast_radius(vm_id, max_hops=99)["hops"] == graph_phase3.MAX_HOPS


# ---------------------------------------------------------------------------
# get_root_cause_candidates
# ---------------------------------------------------------------------------


def test_get_root_cause_rejects_non_uuid(patched_session):
    assert "must be a UUID" in mcp_server.get_root_cause_candidates("nope", "2026-07-01")["error"]


def test_get_root_cause_rejects_bad_timestamp(patched_session):
    result = mcp_server.get_root_cause_candidates(str(uuid.uuid4()), "last tuesday")
    assert "ISO-8601" in result["error"]


def test_get_root_cause_unknown_node(patched_session):
    result = mcp_server.get_root_cause_candidates(str(uuid.uuid4()), "2026-07-01T00:00:00")
    assert "not found" in result["error"]


def test_get_root_cause_returns_neighbor_change(patched_session):
    with Session(patched_session) as s:
        vm_res = _resource(s, "web01")
        esxi_res = _resource(s, "esxi01")
        vm = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01", resource_id=vm_res.id)
        esxi = _node(
            s, GraphNodeType.VSPHERE_HOST, "vc1:host-1", "esxi01", resource_id=esxi_res.id
        )
        graph_phase2.upsert_edge(
            s,
            source_id=vm.id,
            target_id=esxi.id,
            edge_type=GraphEdgeType.HOSTED_ON,
            method=GraphEdgeMethod.DECLARED,
            confidence=Decimal("1.000"),
            source="test",
            evidence={"basis": "fixture"},
        )
        s.add(
            DriftEvent(
                id=uuid.uuid4(),
                resource_id=esxi_res.id,
                drift_type="config_drift",
                field="ntp_servers",
                old_value={"v": "a"},
                new_value={"v": "b"},
                detected_at=datetime.now(timezone.utc) - timedelta(hours=1),
                status="open",
            )
        )
        s.commit()
        vm_id = str(vm.id)

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    result = mcp_server.get_root_cause_candidates(vm_id, since)

    assert result["count"] == 1
    assert result["candidates"][0]["change_event"]["field"] == "ntp_servers"
    assert result["candidates"][0]["hop_distance"] == 1
    assert "->" in result["candidates"][0]["delta"]


def test_get_root_cause_node_with_no_changes(patched_session):
    with Session(patched_session) as s:
        node = _node(s, GraphNodeType.CVE, "CVE-2024-1", "CVE-2024-1")
        s.commit()
        node_id = str(node.id)

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    result = mcp_server.get_root_cause_candidates(node_id, since)

    assert result["candidates"] == []
    assert result["count"] == 0


def test_get_root_cause_naive_timestamp_is_read_as_utc(patched_session):
    with Session(patched_session) as s:
        node = _node(s, GraphNodeType.CVE, "CVE-2024-1", "CVE-2024-1")
        s.commit()
        node_id = str(node.id)

    result = mcp_server.get_root_cause_candidates(node_id, "2026-01-01T00:00:00")

    assert "error" not in result
    assert result["since"].endswith("+00:00")


# ---------------------------------------------------------------------------
# get_reconciliation_state
# ---------------------------------------------------------------------------


def test_get_reconciliation_state_empty(patched_session):
    assert mcp_server.get_reconciliation_state() == []


def test_get_reconciliation_state_returns_pending_queue(patched_session):
    with Session(patched_session) as s:
        _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        _node(s, GraphNodeType.R7_ASSET, "1", "web01")
        _node(s, GraphNodeType.R7_ASSET, "2", "web01")
        graph_phase3.resolve_entities(s, materialize=False)
        s.commit()

    rows = mcp_server.get_reconciliation_state()

    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    # Whichever R7_ASSET row becomes the anchor (group order is not
    # guaranteed), the OTHER R7_ASSET shares its node_type and is excluded
    # from the candidate list (confirm_same_as unconditionally refuses a
    # same-node_type pairing) — only the VSPHERE_VM remains.
    assert len(rows[0]["candidate_matches"]) == 1
    assert rows[0]["candidate_matches"][0]["node_type"] == GraphNodeType.VSPHERE_VM.value
    assert rows[0]["source_node"]["name"] == "web01"


def test_get_reconciliation_state_domain_filter_and_limit(patched_session):
    with Session(patched_session) as s:
        _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        _node(s, GraphNodeType.R7_ASSET, "1", "web01")
        _node(s, GraphNodeType.R7_ASSET, "2", "web01")
        graph_phase3.resolve_entities(s, materialize=False)
        s.commit()

    assert mcp_server.get_reconciliation_state(domain="nope") == []
    assert len(mcp_server.get_reconciliation_state(limit=1)) == 1


# ---------------------------------------------------------------------------
# confirm_same_as
# ---------------------------------------------------------------------------


def test_confirm_same_as_blocked_without_mutation_flag(patched_session, monkeypatch):
    monkeypatch.delenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", raising=False)
    result = mcp_server.confirm_same_as(str(uuid.uuid4()), str(uuid.uuid4()), "operator")
    assert "disabled" in result["error"]


def test_confirm_same_as_rejects_non_uuid(patched_session, mutations_enabled):
    assert "must be UUIDs" in mcp_server.confirm_same_as("a", "b", "operator")["error"]


def test_confirm_same_as_unknown_node_writes_nothing(patched_session, mutations_enabled):
    result = mcp_server.confirm_same_as(str(uuid.uuid4()), str(uuid.uuid4()), "operator")
    assert "not found" in result["error"]
    with Session(patched_session) as s:
        assert s.execute(select(GraphEdge)).scalars().all() == []


def test_confirm_same_as_approve_path_commits(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        vm = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        r7a = _node(s, GraphNodeType.R7_ASSET, "1", "web01")
        _node(s, GraphNodeType.R7_ASSET, "2", "web01")
        graph_phase3.resolve_entities(s, materialize=False)
        s.commit()
        vm_id, r7_id = str(vm.id), str(r7a.id)

    result = mcp_server.confirm_same_as(vm_id, r7_id, "operator")

    assert result["confirmed"] is True
    assert result["review_resolved"] is True
    assert result["confidence"] == 1.0

    with Session(patched_session) as s:
        edges = (
            s.execute(select(GraphEdge).where(GraphEdge.edge_type == GraphEdgeType.SAME_AS.value))
            .scalars()
            .all()
        )
        assert len(edges) == 2, "committed, symmetric, and not duplicated"
        assert all(e.method == GraphEdgeMethod.DECLARED.value for e in edges)
        assert all(e.evidence["approver"] == "operator" for e in edges)

        action = s.execute(select(ProposedAction)).scalars().one()
        assert action.status == "approved"
        assert action.approved_by == "operator"


def test_confirm_same_as_blank_approver_rejected(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        vm = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        r7a = _node(s, GraphNodeType.R7_ASSET, "1", "web01")
        s.commit()
        vm_id, r7_id = str(vm.id), str(r7a.id)

    result = mcp_server.confirm_same_as(vm_id, r7_id, "  ")

    assert "approver" in result["error"]
    with Session(patched_session) as s:
        assert s.execute(select(GraphEdge)).scalars().all() == []


def test_confirm_same_as_same_source_type_rejected(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        a = _node(s, GraphNodeType.R7_ASSET, "1", "web01")
        b = _node(s, GraphNodeType.R7_ASSET, "2", "web01")
        s.commit()
        a_id, b_id = str(a.id), str(b.id)

    result = mcp_server.confirm_same_as(a_id, b_id, "operator")

    assert "DIFFERENT sources" in result["error"]
    with Session(patched_session) as s:
        assert s.execute(select(GraphEdge)).scalars().all() == []


def test_confirm_same_as_rejects_a_target_not_in_the_queued_candidates(
    patched_session, mutations_enabled
):
    """The MCP tool has no per-route candidate-membership guard the way the
    dashboard's API route does -- this proves the invariant now lives in
    confirm_same_as itself, so an MCP caller can't bypass a queued question's
    candidate list just because the API route isn't in the way.

    Which of the three ambiguous "web01" nodes becomes the review row's
    anchor (``source_node``) is not guaranteed by resolve_entities' unordered
    query, so this reads the actual persisted row rather than assuming."""
    with Session(patched_session) as s:
        _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        _node(s, GraphNodeType.R7_ASSET, "1", "web01")
        _node(s, GraphNodeType.R7_ASSET, "2", "web01")
        # A real, unrelated node NOT part of the ambiguous "web01" group.
        decoy = _node(s, GraphNodeType.OCTOPUS_MACHINE, "Machines-9", "totally-different")
        graph_phase3.resolve_entities(s, materialize=False)
        s.commit()
        decoy_id = str(decoy.id)
        action = s.execute(select(ProposedAction)).scalars().one()
        anchor_id = action.payload["source_node"]["node_id"]

    result = mcp_server.confirm_same_as(anchor_id, decoy_id, "operator")

    assert "not one of the candidates" in result["error"]
    with Session(patched_session) as s:
        assert s.execute(select(GraphEdge)).scalars().all() == []


def test_confirm_same_as_allows_a_queued_candidate(patched_session, mutations_enabled):
    """Regression: the new guard must not block the LEGITIMATE case -- a
    target that IS one of the queued row's own candidates still confirms.

    Reads the actual anchor and one of its real candidate_matches from the
    persisted review row, rather than assuming which seeded node becomes the
    anchor (see the "rejects" test above for why that assumption is unsafe)."""
    with Session(patched_session) as s:
        _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        _node(s, GraphNodeType.R7_ASSET, "1", "web01")
        _node(s, GraphNodeType.R7_ASSET, "2", "web01")
        graph_phase3.resolve_entities(s, materialize=False)
        s.commit()
        action = s.execute(select(ProposedAction)).scalars().one()
        anchor_type = action.payload["source_node"]["node_type"]
        anchor_id = action.payload["source_node"]["node_id"]
        # Pick a candidate of a DIFFERENT node_type than the anchor --
        # confirm_same_as separately rejects same-source-type pairs, and the
        # candidate list can contain one of each (see the "rejects" test's
        # docstring for why the anchor's type isn't assumed either).
        candidate_id = next(
            c["node_id"]
            for c in action.payload["candidate_matches"]
            if c["node_type"] != anchor_type
        )

    result = mcp_server.confirm_same_as(anchor_id, candidate_id, "operator")

    assert result["confirmed"] is True


def test_confirmed_edge_is_visible_to_default_blast_radius(patched_session, mutations_enabled):
    """A human confirmation is declared/1.000, so it passes the strict default."""
    with Session(patched_session) as s:
        vm = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "alpha")
        oct_ = _node(s, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "zulu")
        s.commit()
        vm_id, oct_id = str(vm.id), str(oct_.id)

    mcp_server.confirm_same_as(vm_id, oct_id, "operator")
    result = mcp_server.get_blast_radius(vm_id)

    assert result["count"] == 1
    assert result["neighbors"][0]["node"]["name"] == "zulu"
    assert "confirmed by operator" in result["neighbors"][0]["why"]


# ---------------------------------------------------------------------------
# retract_same_as
# ---------------------------------------------------------------------------


def test_retract_same_as_blocked_without_mutation_flag(patched_session, monkeypatch):
    monkeypatch.delenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", raising=False)
    result = mcp_server.retract_same_as(str(uuid.uuid4()), str(uuid.uuid4()), "operator")
    assert "disabled" in result["error"]


def test_retract_same_as_rejects_non_uuid(patched_session, mutations_enabled):
    assert "must be UUIDs" in mcp_server.retract_same_as("a", "b", "operator")["error"]


def test_retract_same_as_no_active_edge_writes_nothing(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        vm = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        r7a = _node(s, GraphNodeType.R7_ASSET, "1", "web01")
        s.commit()
        vm_id, r7_id = str(vm.id), str(r7a.id)

    result = mcp_server.retract_same_as(vm_id, r7_id, "operator")

    assert "no active, human-confirmed SAME_AS edge" in result["error"]


def test_retract_same_as_undoes_a_confirmation_and_reopens_the_queue(
    patched_session, mutations_enabled
):
    with Session(patched_session) as s:
        vm = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        r7a = _node(s, GraphNodeType.R7_ASSET, "1", "web01")
        _node(s, GraphNodeType.R7_ASSET, "2", "web01")
        graph_phase3.resolve_entities(s, materialize=False)
        s.commit()
        vm_id, r7_id = str(vm.id), str(r7a.id)

    mcp_server.confirm_same_as(vm_id, r7_id, "operator")
    result = mcp_server.retract_same_as(vm_id, r7_id, "operator-again", reason="wrong match")

    assert result["retracted"] is True
    assert result["review_reopened"] is True

    with Session(patched_session) as s:
        edges = (
            s.execute(select(GraphEdge).where(GraphEdge.edge_type == GraphEdgeType.SAME_AS.value))
            .scalars()
            .all()
        )
        assert all(e.valid_to is not None for e in edges)
        assert all(e.evidence["retracted_by"] == "operator-again" for e in edges)

        action = s.execute(select(ProposedAction)).scalars().one()
        assert action.status == "pending"
        assert action.approved_by is None

    # Retracted edges no longer feed blast-radius.
    assert mcp_server.get_blast_radius(vm_id)["count"] == 0


def test_retract_same_as_blank_retractor_rejected(patched_session, mutations_enabled):
    with Session(patched_session) as s:
        vm = _node(s, GraphNodeType.VSPHERE_VM, "vc1:vm-1", "web01")
        oct_ = _node(s, GraphNodeType.OCTOPUS_MACHINE, "Machines-1", "web01")
        s.commit()
        vm_id, oct_id = str(vm.id), str(oct_.id)

    mcp_server.confirm_same_as(vm_id, oct_id, "operator")
    result = mcp_server.retract_same_as(vm_id, oct_id, "  ")

    assert "retractor" in result["error"]
    with Session(patched_session) as s:
        assert all(e.valid_to is None for e in s.execute(select(GraphEdge)).scalars().all())
