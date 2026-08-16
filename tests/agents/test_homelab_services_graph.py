"""P1: ``homelab_services`` declares ``Service ─RUNS_ON→ Host``.

The first real declaration against the P0 contract
(docs/decisions/2026-08-11-graph-first-architecture.md). It is chosen because
it is the one relationship in this homelab that is genuinely declared by a
source rather than guessed: the manifest states each service's ``host``, which
commit 8289227 started carrying into ``resources.metadata.host``.

The join is the interesting part and the reason this is a real test of the
contract rather than a rehearsal of it. The manifest spells hosts with HYPHENS
(``node-a``); the Ansible inventory that produces ``linux_host`` resources
spells the same machines with UNDERSCORES (``node_a``). Nothing joins those two
spellings unless the declared key normaliser folds the separator — so this is
the test that proves the declarative path can express a real-world
normalisation, not only an exact string match.

This environment is a homelab. There is no vSphere, Rapid7, Octopus,
Kubernetes, cloud or Windows here and there never will be, so nothing below
fabricates one.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain.agents.homelab_services import DEFAULT_MANIFEST_PATH, HomelabServicesAgent
from infra_brain.agents.linux import LinuxAgent
from infra_brain.db.models import GraphEdge, GraphNode, Resource

from tests.support.pg import make_engine

SERVICE_NODE = "HomelabService"
HOST_NODE = "LinuxHost"
RUNS_ON = "RUNS_ON"


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        if engine.dialect.name == "sqlite":
            # SQLite ignores FKs unless asked; PostgreSQL enforces them always
            # (and rejects the PRAGMA outright).
            s.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
        yield s
        s.rollback()


def _specs():
    return {
        "homelab_services": HomelabServicesAgent.spec,
        "linux": LinuxAgent.spec,
    }


def _service(session, name, host, **extra):
    session.add(
        Resource(
            id=uuid.uuid4(),
            domain="homelab_services",
            type="homelab_service",
            name=name,
            source="HomelabServicesAgent",
            metadata_={"host": host, **extra},
        )
    )


def _host(session, name):
    session.add(
        Resource(
            id=uuid.uuid4(),
            domain="linux",
            type="linux_host",
            name=name,
            source="LinuxAgent",
            metadata_={"distro": "Ubuntu"},
        )
    )


def _edges(session):
    return list(
        session.execute(
            select(GraphEdge).where(GraphEdge.edge_type == RUNS_ON, GraphEdge.valid_to.is_(None))
        ).scalars()
    )


# --- the declaration itself -------------------------------------------------


def test_homelab_services_declares_the_runs_on_edge():
    spec = HomelabServicesAgent.spec
    assert [n.type for n in spec.emits_nodes] == [SERVICE_NODE]
    assert [e.type for e in spec.emits_edges] == [RUNS_ON]
    edge = spec.emits_edges[0]
    assert edge.from_node == SERVICE_NODE
    assert edge.to_node == HOST_NODE
    assert edge.from_key == "attributes.host", (
        "the join key is the manifest's declared host, carried by commit 8289227"
    )


def test_linux_declares_the_host_node():
    """The edge needs a target. The host entity belongs to the linux collector.

    homelab_services must NOT mint host nodes of its own — it only knows a
    hostname string, not the machine.
    """
    spec = LinuxAgent.spec
    assert [n.type for n in spec.emits_nodes] == [HOST_NODE]
    assert spec.emits_edges == ()


def test_homelab_services_declares_its_identity_keys():
    assert HomelabServicesAgent.spec.identity_keys == ("name",)
    assert "name" in LinuxAgent.spec.identity_keys


def test_the_whole_graph_contribution_is_introspectable():
    """What the graph knows how to build must be answerable without reading source.

    That question is exactly what a 6,367-line hand-maintained deriver made
    unanswerable, so the declarative path has to answer it by construction.

    Pinned exactly, not loosely: this dict IS the migration's progress meter
    (``etl.spec.graph_emitting_domains``'s docstring says to assert on it rather
    than trust a phase table in a design doc), so every phase that adds a
    declaration must show up here as a deliberate edit.

    Currently: P1's ``homelab_services``/``linux`` pair, P2's ``iac``/``cicd``
    pair — BELONGS_TO (tests/agents/test_iac_belongs_to_graph.py) and its
    inverse DEFINED_IN (tests/agents/test_iac_defined_in_graph.py) — and P4's
    two additions, ANSIBLE_MANAGES (still iac, re-anchored per TRK-354 Option A;
    tests/agents/test_iac_ansible_manages_graph.py) and ``pki``, the FIFTH
    contributing domain (tests/agents/test_pki_issued_by_graph.py).

    Note the DEFINED_IN entries render ``GitlabProject-DEFINED_IN-><file>``,
    the direction the edge is STORED, while the declaration's ``from_node`` is
    the file. That is ``EdgeDirection.INVERSE`` doing its job: the join runs
    from the side that owns the key, the arrow runs the other way, and a reader
    asking what the graph looks like wants the arrow. ANSIBLE_MANAGES renders
    forward for the opposite reason: its fan-out comes from ``from_key_multi``
    (one node making many assertions), not from a reversed arrow.
    """
    from infra_brain.agents.iac import _IAC_FILE_NODE_TYPES
    from infra_brain.graph_engine import declared_contributions

    iac_nodes = list(_IAC_FILE_NODE_TYPES.values())
    compose_node = _IAC_FILE_NODE_TYPES["docker_compose"]
    inventory_node = _IAC_FILE_NODE_TYPES["ansible_inventory_file"]
    assert declared_contributions() == {
        "homelab_services": {
            "nodes": [SERVICE_NODE],
            "edges": [f"{SERVICE_NODE}-{RUNS_ON}->{HOST_NODE}"],
            "identity_keys": ["name"],
        },
        "linux": {"nodes": [HOST_NODE], "edges": [], "identity_keys": ["name"]},
        "iac": {
            # ``ContainerImage`` is last-but-one because it is declared in that
            # order, and declared at all because the compose collector owns the
            # assertion "this image exists" — the deriver used to mint that
            # entity inline with nothing declaring it. See
            # tests/agents/test_iac_runs_image_graph.py.
            # ``AnsibleInventoryGroup`` is TRK-359's junction node: identified
            # from ``ansible_inventory_groups`` rows, carrying its member hosts
            # via ``row_gathers``. Its MEMBER_OF edge renders in STORED order
            # (host -> group) because it is INVERSE, like DEFINED_IN.
            "nodes": iac_nodes + ["ContainerImage", "AnsibleInventoryGroup"],
            "edges": [f"{n}-BELONGS_TO->GitlabProject" for n in iac_nodes]
            + [f"GitlabProject-DEFINED_IN->{n}" for n in iac_nodes]
            + [f"{compose_node}-RUNS_IMAGE->ContainerImage"]
            + [f"{inventory_node}-ANSIBLE_MANAGES->{HOST_NODE}"]
            + [f"{HOST_NODE}-MEMBER_OF->AnsibleInventoryGroup"],
            "identity_keys": [],
        },
        # TRK-359's second junction declaration, restoring the second P5
        # accepted loss: the EolProduct node is identified from eol_registry
        # rows anchored on linux hosts, and the hosts that run it are gathered
        # off those anchors (RowGather(path=())). Stored host -> product.
        "eol": {
            "nodes": ["EolProduct"],
            "edges": [f"{HOST_NODE}-RUNS_EOL->EolProduct"],
            "identity_keys": [],
        },
        "cicd": {"nodes": ["GitlabProject"], "edges": [], "identity_keys": []},
        "pki": {
            "nodes": ["Certificate", "CertificateAuthority"],
            "edges": ["Certificate-ISSUED_BY->CertificateAuthority"],
            "identity_keys": ["name", "thumbprint"],
        },
        # P5's addition, and the first declaration made for the IDENTITY
        # resolver rather than for an edge: netdiscovery is the only live
        # source behind host_reconcile's "net" leg, so retiring that agent's
        # IS_SAME_AS writer needed this node to exist first. No edges — the
        # collector asserts what a host IS, not what it is related to.
        # tests/test_p5_issameas_resolver_coverage.py owns the coverage proof.
        "netdiscovery": {
            "nodes": ["NetDiscoveredHost"],
            "edges": [],
            "identity_keys": ["hostname", "mac"],
        },
    }


# --- behaviour --------------------------------------------------------------


def test_hyphen_service_joins_underscore_host(session):
    """THE point of P1: node_a (manifest) must reach node_a (inventory)."""
    from infra_brain import graph_engine

    _host(session, "node_a")
    _service(session, "grafana", "node_a")
    session.flush()

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    edges = _edges(session)
    assert len(edges) == 1, (
        "the hyphen/underscore spelling difference must be normalised away — "
        "without it this environment's services can never reach their hosts"
    )
    svc = session.get(GraphNode, edges[0].source_id)
    host = session.get(GraphNode, edges[0].target_id)
    assert (svc.node_type, svc.name) == (SERVICE_NODE, "grafana")
    assert (host.node_type, host.name) == (HOST_NODE, "node_a")
    assert counts["edges"][RUNS_ON] == 1


def test_service_with_null_host_emits_no_edge(session):
    """`pi-hole` and `takeoff` carry host: null. No edge, not a broken one."""
    from infra_brain import graph_engine

    _host(session, "node_a")
    _service(session, "pi-hole", None)
    session.flush()

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == []
    assert _edges(session) == [], "a host:null service must produce NO edge"
    assert counts["edges"].get(RUNS_ON, 0) == 0
    # ...but the service is still an entity in its own right.
    assert (
        session.execute(select(GraphNode).where(GraphNode.node_type == SERVICE_NODE))
        .scalar_one()
        .name
        == "pi-hole"
    )


def test_service_on_a_host_we_do_not_collect_emits_no_edge(session):
    """Not every manifest host is an Ansible-managed linux host."""
    from infra_brain import graph_engine

    _host(session, "node_a")
    _service(session, "synology-photos", "storage_node")
    session.flush()

    graph_engine.emit_all(session, specs=_specs())

    assert _edges(session) == []


def test_edges_are_auto_authority_and_honestly_scored(session):
    """The manifest declares the host *name*; we MATCH it to a host entity.

    That is a deterministic_match, not a declared FK join, so it may not
    claim 1.000 — the honesty rule from the Phase 2 store docstring.
    """
    from decimal import Decimal

    from infra_brain import graph_engine
    from infra_brain.db.models import GraphEdgeAuthority, GraphEdgeMethod

    _host(session, "node_a")
    _service(session, "grafana", "node_a")
    session.flush()
    graph_engine.emit_all(session, specs=_specs())

    edge = _edges(session)[0]
    assert edge.authority == GraphEdgeAuthority.AUTO.value
    assert edge.method == GraphEdgeMethod.DETERMINISTIC_MATCH.value
    assert edge.confidence < Decimal("1.000")
    assert edge.evidence and edge.evidence.get("match_key") == "node-a"


def test_a_human_runs_on_edge_survives_the_engine(session):
    """An operator who corrects a RUNS_ON must not be silently overruled."""
    from decimal import Decimal

    from infra_brain import graph_engine, graph_phase2
    from infra_brain.db.models import GraphEdgeAuthority, GraphEdgeMethod

    _host(session, "node_a")
    _host(session, "media_host")
    _service(session, "grafana", "node_a")
    session.flush()
    graph_engine.emit_all(session, specs=_specs())

    svc = session.execute(select(GraphNode).where(GraphNode.node_type == SERVICE_NODE)).scalar_one()
    media = session.execute(
        select(GraphNode).where(GraphNode.node_type == HOST_NODE, GraphNode.name == "media_host")
    ).scalar_one()
    human = graph_phase2.upsert_edge(
        session,
        source_id=svc.id,
        target_id=media.id,
        edge_type=RUNS_ON,
        method=GraphEdgeMethod.DECLARED,
        confidence=Decimal("1.000"),
        source="operator:operator",
        authority=GraphEdgeAuthority.HUMAN,
    )
    human_id = human.id

    graph_engine.emit_all(session, specs=_specs())

    surviving = {e.id: e for e in _edges(session)}
    assert human_id in surviving
    assert surviving[human_id].authority == GraphEdgeAuthority.HUMAN.value
    assert surviving[human_id].source == "operator:operator"


# --- against the real manifest ---------------------------------------------


def _manifest_entries():
    raw = json.loads(Path(DEFAULT_MANIFEST_PATH).read_text())
    return raw["services"] if isinstance(raw, dict) else raw


_REAL_MANIFEST_NOT_PUBLISHED = pytest.mark.skip(
    reason="asserts exact shape (count, host spellings) of the real 92-entry "
    "homelab service manifest, which is intentionally not published in this "
    "sanitized copy — see src/infra_brain/homelab_services_manifest.json's "
    "$comment. These tests document what CI validates against the real, "
    "private manifest; they cannot pass against the synthetic sample."
)


@_REAL_MANIFEST_NOT_PUBLISHED
def test_real_manifest_shape_is_what_the_declaration_assumes():
    """Ground the declaration in the actual shipped manifest, not a fixture."""
    entries = _manifest_entries()
    assert len(entries) == 92
    assert sum(1 for e in entries if e.get("url") is None) == 32
    assert sum(1 for e in entries if e.get("host") is None) == 2
    assert all("-" in e["host"] for e in entries if e.get("host")), (
        "manifest hosts are hyphen-spelled; the whole normalisation exists for this"
    )


@_REAL_MANIFEST_NOT_PUBLISHED
def test_whole_manifest_materialises_against_underscore_hosts(session):
    """End-to-end over all 92 real services and the real inventory spellings.

    Every manifest host that has a matching ``linux_host`` resource must get
    exactly its own services' worth of edges; hosts we do not collect and
    ``host: null`` entries must contribute none.
    """
    from infra_brain import graph_engine

    entries = _manifest_entries()
    # The four hosts the Ansible inventory actually produces linux_host rows
    # for, spelled the way the inventory spells them.
    inventory = ["node_a", "media_host", "ai_node", "git_runner"]
    for h in inventory:
        _host(session, h)
    for e in entries:
        _service(session, e["name"], e.get("host"))
    session.flush()

    counts, errors = graph_engine.emit_all(session, specs=_specs())
    assert errors == [], errors

    expected = Counter((e["host"] or "").replace("-", "_") for e in entries if e.get("host"))
    expected = {h: expected[h] for h in inventory}

    actual: Counter[str] = Counter()
    for edge in _edges(session):
        actual[session.get(GraphNode, edge.target_id).name] += 1

    assert dict(actual) == expected, (
        "every collected host must receive exactly the services the manifest "
        f"puts on it; got {dict(actual)} want {expected}"
    )
    # Sanity floor: node_a is by far the busiest host in this homelab.
    assert actual["node_a"] >= 15
    assert counts["nodes"][SERVICE_NODE] == 92
    assert sum(actual.values()) == counts["edges"][RUNS_ON]
