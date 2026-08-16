"""Emitter tests for Relationship Graph Phase 2 (issue #126).

One test per edge emitter, driven by realistic fixture data modelled on the
issue's own live finding (several VMs co-located on one ESXi host sharing a
datastore). FIXTURE-ONLY — no vCenter, no Rapid7, no network I/O.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    GraphEdge,
    GraphEdgeMethod,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    R7Asset,
    Resource,
    VsphereDatastore,
    VsphereHost,
    VsphereVm,
    VulnQueueItem,
    ZONE_CORPORATE,
)
from infra_brain import graph_phase2

from tests.support.pg import make_engine


VC = "vcenter.example.com"


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        yield s
        s.rollback()


def _resource(session, rtype, name):
    r = Resource(
        id=uuid.uuid4(),
        domain="vsphere" if rtype.startswith("vsphere") else "vuln",
        type=rtype,
        name=name,
        source="test",
        zone=ZONE_CORPORATE,
    )
    session.add(r)
    session.flush()
    return r


def _edges(session, edge_type):
    return (
        session.execute(
            select(GraphEdge).where(
                GraphEdge.edge_type == edge_type.value, GraphEdge.valid_to.is_(None)
            )
        )
        .scalars()
        .all()
    )


def _node_of(session, edge_side_id):
    return session.get(GraphNode, edge_side_id)


def _seed_vsphere(session):
    """4 VMs on one ESXi host, all sharing datastore DS-01 (the issue's case).

    Also seeds: a second host in the same vCenter (so name matching has to
    discriminate), a template VM (must be skipped), a VM whose esxi_host names
    a host we never collected (must be skipped, not guessed), and a datastore
    name referenced by a VM but absent from vsphere_datastores.
    """
    host = VsphereHost(
        id=uuid.uuid4(),
        resource_id=_resource(session, "vsphere_host", "esx01").id,
        vcenter=VC,
        moref="host-11",
        name="esx01.example.com",
        cluster_name="CL-PROD",
        datastore_names=["DS-01", "DS-02"],
    )
    other_host = VsphereHost(
        id=uuid.uuid4(),
        resource_id=_resource(session, "vsphere_host", "esx02").id,
        vcenter=VC,
        moref="host-12",
        name="esx02.example.com",
        cluster_name="CL-PROD",
        datastore_names=[],
    )
    ds1 = VsphereDatastore(
        id=uuid.uuid4(),
        resource_id=_resource(session, "vsphere_datastore", "DS-01").id,
        vcenter=VC,
        moref="datastore-21",
        name="DS-01",
        datastore_type="VMFS",
    )
    ds2 = VsphereDatastore(
        id=uuid.uuid4(),
        resource_id=_resource(session, "vsphere_datastore", "DS-02").id,
        vcenter=VC,
        moref="datastore-22",
        name="DS-02",
        datastore_type="NFS",
    )
    session.add_all([host, other_host, ds1, ds2])

    vms = []
    for i in range(1, 5):
        vms.append(
            VsphereVm(
                id=uuid.uuid4(),
                resource_id=_resource(session, "vsphere_vm", f"app0{i}").id,
                vcenter=VC,
                moref=f"vm-{100 + i}",
                name=f"app0{i}",
                esxi_host="esx01.example.com",
                power_state="poweredOn",
                datastore_names=["DS-01"],
                network_names=[],
            )
        )
    # Template — skipped by both vSphere emitters.
    vms.append(
        VsphereVm(
            id=uuid.uuid4(),
            vcenter=VC,
            moref="vm-200",
            name="tpl-rhel9",
            is_template=True,
            esxi_host="esx01.example.com",
            datastore_names=["DS-01"],
            network_names=[],
        )
    )
    # esxi_host we never collected — must NOT produce a guessed edge.
    vms.append(
        VsphereVm(
            id=uuid.uuid4(),
            vcenter=VC,
            moref="vm-201",
            name="orphan01",
            esxi_host="esx99.example.com",
            datastore_names=["DS-NOPE"],
            network_names=[],
        )
    )
    session.add_all(vms)
    session.flush()
    return host, other_host, ds1, ds2, vms


# ---------------------------------------------------------------------------
# HOSTED_ON
# ---------------------------------------------------------------------------


def test_hosted_on_emits_vm_to_esxi_host_edges(session):
    host, other_host, _ds1, _ds2, _vms = _seed_vsphere(session)

    written = graph_phase2.emit_hosted_on(session)
    session.commit()

    assert written == 4  # 4 real VMs; template + unresolved host skipped
    edges = _edges(session, GraphEdgeType.HOSTED_ON)
    assert len(edges) == 4

    host_node_ids = {e.target_id for e in edges}
    assert len(host_node_ids) == 1  # all four co-located on ONE host
    host_node = _node_of(session, host_node_ids.pop())
    assert host_node.node_type == GraphNodeType.VSPHERE_HOST.value
    assert host_node.natural_key == f"{VC}:host-11"
    assert host_node.resource_id == host.resource_id

    src_nodes = [_node_of(session, e.source_id) for e in edges]
    assert all(n.node_type == GraphNodeType.VSPHERE_VM.value for n in src_nodes)
    assert {n.name for n in src_nodes} == {"app01", "app02", "app03", "app04"}

    # Provenance: a display-name match is NOT a declared fact.
    for e in edges:
        assert e.method == GraphEdgeMethod.DETERMINISTIC_MATCH.value
        assert Decimal(str(e.confidence)) == Decimal("0.990")
        assert e.evidence["esxi_host_name"] == "esx01.example.com"
        assert e.evidence["vcenter"] == VC
        assert e.valid_to is None

    # The uncollected-host VM and the template produced no node/edge at all.
    names = {n.name for n in session.execute(select(GraphNode)).scalars()}
    assert "tpl-rhel9" not in names
    assert "orphan01" not in names
    # The second host was never named by any VM, so it has no HOSTED_ON edge.
    assert not any(
        _node_of(session, e.target_id).natural_key == f"{VC}:{other_host.moref}" for e in edges
    )


def test_hosted_on_does_not_match_across_vcenters(session):
    """Identically-named hosts in two vCenters must not cross-link."""
    session.add_all(
        [
            VsphereHost(
                id=uuid.uuid4(),
                vcenter="vc-a",
                moref="host-1",
                name="esx01.example.com",
                datastore_names=[],
            ),
            VsphereVm(
                id=uuid.uuid4(),
                vcenter="vc-b",
                moref="vm-1",
                name="vm-in-b",
                esxi_host="esx01.example.com",
                datastore_names=[],
                network_names=[],
            ),
        ]
    )
    session.flush()
    assert graph_phase2.emit_hosted_on(session) == 0


def test_hosted_on_is_idempotent(session):
    _seed_vsphere(session)
    graph_phase2.emit_hosted_on(session)
    session.commit()
    graph_phase2.emit_hosted_on(session)
    session.commit()
    assert len(_edges(session, GraphEdgeType.HOSTED_ON)) == 4
    assert len(session.execute(select(GraphNode)).scalars().all()) == 5  # 4 VMs + 1 host


# ---------------------------------------------------------------------------
# MOUNTS_DATASTORE
# ---------------------------------------------------------------------------


def test_mounts_datastore_emits_vm_and_host_edges(session):
    host, _other, ds1, ds2, _vms = _seed_vsphere(session)

    written = graph_phase2.emit_mounts_datastore(session)
    session.commit()

    # 4 VMs × DS-01, plus host esx01 × {DS-01, DS-02}. esx02 has none;
    # template skipped; DS-NOPE unresolved.
    assert written == 6
    edges = _edges(session, GraphEdgeType.MOUNTS_DATASTORE)
    assert len(edges) == 6

    by_owner_kind: dict[str, int] = {}
    for e in edges:
        by_owner_kind[e.evidence["owner_kind"]] = by_owner_kind.get(e.evidence["owner_kind"], 0) + 1
        assert e.method == GraphEdgeMethod.DETERMINISTIC_MATCH.value
        assert Decimal(str(e.confidence)) == Decimal("0.990")
        assert _node_of(session, e.target_id).node_type == GraphNodeType.VSPHERE_DATASTORE.value
    assert by_owner_kind == {"vm": 4, "host": 2}

    # Shared datastore converges on ONE node (the issue's co-location finding).
    ds1_node = session.execute(
        select(GraphNode).where(GraphNode.natural_key == f"{VC}:{ds1.moref}")
    ).scalar_one()
    assert ds1_node.resource_id == ds1.resource_id
    assert sum(1 for e in edges if e.target_id == ds1_node.id) == 5  # 4 VMs + the host

    ds2_node = session.execute(
        select(GraphNode).where(GraphNode.natural_key == f"{VC}:{ds2.moref}")
    ).scalar_one()
    host_edges = [e for e in edges if e.evidence["owner_kind"] == "host"]
    assert {e.target_id for e in host_edges} == {ds1_node.id, ds2_node.id}
    assert all(
        _node_of(session, e.source_id).natural_key == f"{VC}:{host.moref}" for e in host_edges
    )

    # DS-NOPE was never collected — no phantom datastore node.
    assert not session.execute(select(GraphNode).where(GraphNode.name == "DS-NOPE")).scalars().all()


def test_mounts_datastore_dedupes_repeated_names_and_is_idempotent(session):
    session.add_all(
        [
            VsphereDatastore(
                id=uuid.uuid4(),
                vcenter=VC,
                moref="datastore-21",
                name="DS-01",
                datastore_type="VMFS",
            ),
            VsphereVm(
                id=uuid.uuid4(),
                vcenter=VC,
                moref="vm-1",
                name="dupe-vm",
                esxi_host="",
                datastore_names=["DS-01", "DS-01", "  ", None],
                network_names=[],
            ),
        ]
    )
    session.flush()
    graph_phase2.emit_mounts_datastore(session)
    session.commit()
    graph_phase2.emit_mounts_datastore(session)
    session.commit()
    assert len(_edges(session, GraphEdgeType.MOUNTS_DATASTORE)) == 1


# ---------------------------------------------------------------------------
# AFFECTED_BY_CVE
# ---------------------------------------------------------------------------


def test_affected_by_cve_emits_declared_edges_over_shared_resource_ids(session):
    r_a = _resource(session, "r7_asset", "web01")
    r_b = _resource(session, "r7_asset", "web02")
    session.add_all(
        [
            R7Asset(
                id=uuid.uuid4(),
                resource_id=r_a.id,
                r7_asset_id=4001,
                ip="10.0.0.11",
                hostname="web01",
                os="RHEL 9",
            ),
            R7Asset(
                id=uuid.uuid4(),
                resource_id=r_b.id,
                r7_asset_id=4002,
                ip="10.0.0.12",
                hostname="web02",
                os="RHEL 9",
            ),
            # No resource_id → not resolvable, must be skipped.
            R7Asset(id=uuid.uuid4(), r7_asset_id=4003, ip="10.0.0.13", hostname="ghost"),
        ]
    )
    session.add_all(
        [
            VulnQueueItem(
                id=uuid.uuid4(), resource_id=r_a.id, cve_id="CVE-2024-0001", severity="critical"
            ),
            VulnQueueItem(
                id=uuid.uuid4(), resource_id=r_a.id, cve_id="CVE-2024-0002", severity="severe"
            ),
            # Same CVE on a second asset — must converge on one CVE node.
            VulnQueueItem(
                id=uuid.uuid4(), resource_id=r_b.id, cve_id="cve-2024-0001", severity="critical"
            ),
            # Finding for a resource with no R7Asset row — skipped.
            VulnQueueItem(
                id=uuid.uuid4(),
                resource_id=_resource(session, "linux_host", "not-an-r7-asset").id,
                cve_id="CVE-2024-9999",
                severity="moderate",
            ),
        ]
    )
    session.flush()

    written = graph_phase2.emit_affected_by_cve(session)
    session.commit()

    assert written == 3
    edges = _edges(session, GraphEdgeType.AFFECTED_BY_CVE)
    assert len(edges) == 3

    # An FK join over the shared resources.id space IS a declared fact.
    for e in edges:
        assert e.method == GraphEdgeMethod.DECLARED.value
        assert Decimal(str(e.confidence)) == Decimal("1.000")
        assert "resources.id" in e.evidence["join"]

    cve_nodes = (
        session.execute(select(GraphNode).where(GraphNode.node_type == GraphNodeType.CVE.value))
        .scalars()
        .all()
    )
    # Case-normalised: "cve-2024-0001" and "CVE-2024-0001" are one node.
    assert {n.natural_key for n in cve_nodes} == {"CVE-2024-0001", "CVE-2024-0002"}
    shared = next(n for n in cve_nodes if n.natural_key == "CVE-2024-0001")
    assert sum(1 for e in edges if e.target_id == shared.id) == 2
    # A CVE is a shared value node, not an owned resource.
    assert all(n.resource_id is None for n in cve_nodes)

    asset_nodes = (
        session.execute(
            select(GraphNode).where(GraphNode.node_type == GraphNodeType.R7_ASSET.value)
        )
        .scalars()
        .all()
    )
    assert {n.natural_key for n in asset_nodes} == {"4001", "4002"}
    assert {n.resource_id for n in asset_nodes} == {r_a.id, r_b.id}
    assert "CVE-2024-9999" not in {n.natural_key for n in cve_nodes}


def test_affected_by_cve_empty_and_idempotent(session):
    # Empty: no assets at all.
    assert graph_phase2.emit_affected_by_cve(session) == 0

    r = _resource(session, "r7_asset", "web01")
    session.add(R7Asset(id=uuid.uuid4(), resource_id=r.id, r7_asset_id=4001, hostname="web01"))
    session.add(
        VulnQueueItem(
            id=uuid.uuid4(), resource_id=r.id, cve_id="CVE-2024-0001", severity="critical"
        )
    )
    session.flush()
    graph_phase2.emit_affected_by_cve(session)
    session.commit()
    graph_phase2.emit_affected_by_cve(session)
    session.commit()
    assert len(_edges(session, GraphEdgeType.AFFECTED_BY_CVE)) == 1


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_emit_all_runs_three_emitters_and_isolates_failures(session):
    _seed_vsphere(session)
    counts, errors = graph_phase2.emit_all(session)
    session.commit()
    assert errors == []
    assert counts == {"HOSTED_ON": 4, "MOUNTS_DATASTORE": 6, "AFFECTED_BY_CVE": 0}


def test_emit_all_surfaces_an_emitter_failure_instead_of_a_silent_zero(session, monkeypatch):
    _seed_vsphere(session)
    monkeypatch.setattr(
        graph_phase2,
        "emit_hosted_on",
        lambda _s: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    counts, errors = graph_phase2.emit_all(session)
    assert counts["HOSTED_ON"] == 0
    assert any("HOSTED_ON: boom" in e for e in errors)
    assert counts["MOUNTS_DATASTORE"] == 6  # the other emitters still ran
