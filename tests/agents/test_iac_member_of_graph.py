"""``iac`` declares ``<LinuxHost> ─MEMBER_OF→ <AnsibleInventoryGroup>`` (TRK-359).

The FIRST junction declaration, restoring the first of P5's two accepted
losses. The group is a value in ``ansible_inventory_groups`` with no
``resources`` row of its own; its members live one table further down. Until
``NodeSpec.row_gathers`` (the junction grammar), a ``from_rows`` node could
not carry that member list, so the fact was derived by nothing after P5
deleted ``graph_maintenance``'s hand-written block.

THE ORACLE IS THE P5 AUDIT'S RECONSTRUCTION QUERY, NOT THE DELETED DERIVER.
The two-commit equivalence discipline froze each deleted deriver in a test as
a permanently-running oracle — but this deriver was already deleted a phase
ago, with the P5 drop migration (``95d988b2bc3c``) recording in its place the
reconstruction that PROVED the loss was recoverable:

    MEMBER_OF: "PROVEN row-for-row: ansible_inventory_hosts JOIN
    ansible_inventory_groups reproduces all 10 (host, group) pairs; both
    symmetric differences empty"

``_reconstruction_member_of`` below is that query, computed INDEPENDENTLY of
the engine (a plain join over the two inventory tables, matched to linux
hosts through the same ``host`` normaliser the declaration names), and the
suite asserts the engine's emitted edges equal it over a fixture that carries
every awkward live shape: a host in TWO groups, a group with ZERO members, a
member no linux host answers to, and the case/hyphen-underscore spelling
variants this homelab genuinely produces.

ONE DELIBERATE DIVERGENCE from the deleted deriver, on the record: it matched
members by bare ``.lower()`` and claimed confidence 1.0 into a store with no
honesty gate. The declaration matches through ``graph_match_key`` (folds
``node_a`` == ``node_a``, which ``.lower()`` could not) and claims 0.900,
because a hostname join is a display-name join. Folds more, claims less.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain.agents.iac import IaCAgent
from infra_brain.db.models import (
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    GraphEdge,
    GraphNode,
    IacFile,
    Resource,
)
from infra_brain.tools.hostmatch import graph_match_key

from tests.support.pg import make_engine

MEMBER_OF = "MEMBER_OF"
GROUP_NODE = "AnsibleInventoryGroup"
HOST_NODE = "LinuxHost"

PROJECT_ID = 113

#: The fixture inventory, shaped like the live one plus every edge case the
#: TRK-359 contract has to survive:
#:   * ``node_a`` is in TWO groups (docker_hosts + linux) — the fan-in case;
#:   * ``decommissioned`` has ZERO members — node yes, member key absent,
#:     edges none;
#:   * ``media_host`` vs the resource's ``media-host`` — case AND underscore vs
#:     hyphen, the exact spelling split ``graph_match_key`` exists for;
#:   * ``cloudflare_tunnel_a`` matches NO linux host resource — a managed
#:     name that is not a machine this estate collects.
_INVENTORY = {
    "docker_hosts": ["node_a", "media_host"],
    "linux": ["node_a", "git_runner", "cloudflare_tunnel_a"],
    "decommissioned": [],
}

#: ``linux_host`` resources — hyphen spellings, the manifest/services side.
_HOSTS = ["node_a", "media-host", "git_runner"]


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        yield s
        s.rollback()


class _LinuxSpecStub:
    """``LinuxAgent.spec`` without importing the agent (and its ansible tools).

    Same device, same reason and same keep-in-step guard as
    ``tests/agents/test_iac_ansible_manages_graph.py``.
    """

    from infra_brain.etl.spec import AgentSpec, NodeSpec, Tier

    spec = AgentSpec(
        domain="linux",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=None,
        emits_nodes=(NodeSpec(type=HOST_NODE, resource_type="linux_host", natural_key="name"),),
    )


def _specs():
    return {"iac": IaCAgent.spec, "linux": _LinuxSpecStub.spec}


# --- fixture ----------------------------------------------------------------


def _inventory_file(session, path="inventory/inventory.yml", groups=None):
    res = Resource(
        id=uuid.uuid4(),
        domain="iac",
        type="ansible_inventory_file",
        name=f"homelab-ansible/{path}",
        source="IaCAgent",
        metadata_={"project_id": PROJECT_ID, "file_path": path, "ref": "main"},
    )
    session.add(res)
    session.flush()
    iac = IacFile(
        id=uuid.uuid4(),
        resource_id=res.id,
        gitlab_project_id=PROJECT_ID,
        project_name="homelab-ansible",
        path=path,
        file_type="inventory",
        ref="main",
    )
    session.add(iac)
    session.flush()
    for gname, hosts in (groups if groups is not None else _INVENTORY).items():
        group = AnsibleInventoryGroup(id=uuid.uuid4(), iac_file_id=iac.id, name=gname)
        session.add(group)
        session.flush()
        for host in hosts:
            session.add(AnsibleInventoryHost(id=uuid.uuid4(), group_id=group.id, name=host))
    session.flush()
    return res, iac


def _hosts(session, names=None):
    out = {}
    for name in names or _HOSTS:
        res = Resource(
            id=uuid.uuid4(),
            domain="linux",
            type="linux_host",
            name=name,
            source="LinuxAgent",
            metadata_={"distro": "Ubuntu"},
        )
        session.add(res)
        out[name] = res
    session.flush()
    return out


# --- the oracle -------------------------------------------------------------


def _reconstruction_member_of(session) -> set[tuple[uuid.UUID, str]]:
    """The P5 audit's MEMBER_OF reconstruction, computed independently.

    ``ansible_inventory_hosts JOIN ansible_inventory_groups`` gives the
    (member, group) pairs; each member is matched to a live ``linux_host``
    resource through the SAME ``host`` normaliser the declaration names —
    which is the one engine-shared ingredient, deliberately: the oracle's
    claim is about WHICH pairs exist, and the normaliser is part of the
    declared meaning of "which host", not of the engine's mechanics.

    Returns ``{(host resource id, group name)}``.
    """
    hosts_by_key: dict[str, uuid.UUID] = {}
    for res in (
        session.query(Resource)
        .filter(
            Resource.domain == "linux",
            Resource.type == "linux_host",
            Resource.retired_at.is_(None),
        )
        .all()
    ):
        key = graph_match_key(res.name or "")
        if key:
            hosts_by_key[key] = res.id

    pairs: set[tuple[uuid.UUID, str]] = set()
    rows = session.execute(
        select(AnsibleInventoryGroup.name, AnsibleInventoryHost.name).join(
            AnsibleInventoryHost, AnsibleInventoryHost.group_id == AnsibleInventoryGroup.id
        )
    ).all()
    for group_name, member in rows:
        rid = hosts_by_key.get(graph_match_key(member or ""))
        if rid is not None:
            pairs.add((rid, group_name))
    return pairs


def _emitted_member_of(session) -> set[tuple[uuid.UUID, str]]:
    """The engine's answer, lifted into the oracle's ``(host resource id,
    group name)`` vocabulary through the graph rows themselves."""
    nodes = {n.id: n for n in session.execute(select(GraphNode)).scalars()}
    pairs: set[tuple[uuid.UUID, str]] = set()
    for edge in session.execute(
        select(GraphEdge).where(GraphEdge.edge_type == MEMBER_OF)
    ).scalars():
        source = nodes[edge.source_id]
        target = nodes[edge.target_id]
        assert source.node_type == HOST_NODE, "MEMBER_OF must be STORED host -> group"
        assert target.node_type == GROUP_NODE
        pairs.add((source.resource_id, target.natural_key))
    return pairs


# --- equivalence ------------------------------------------------------------


def test_emitted_edges_equal_the_p5_reconstruction(session):
    """Requirement 4 of TRK-359: engine output == the audit's oracle, exactly."""
    from infra_brain import graph_engine

    _inventory_file(session)
    _hosts(session)

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    oracle = _reconstruction_member_of(session)
    emitted = _emitted_member_of(session)
    assert emitted == oracle, (
        f"symmetric difference must be EMPTY (audit standard): "
        f"engine-only={emitted - oracle}, oracle-only={oracle - emitted}"
    )
    # And the fixture actually exercised the shapes it claims to: node_a in
    # two groups, media-host reached across a case+separator split, the
    # unmatched cloudflare name absent, four pairs total.
    hosts = {
        r.name: r.id
        for r in session.query(Resource).filter_by(domain="linux", type="linux_host").all()
    }
    assert oracle == {
        (hosts["node_a"], "docker_hosts"),
        (hosts["node_a"], "linux"),
        (hosts["media-host"], "docker_hosts"),
        (hosts["git_runner"], "linux"),
    }
    assert counts["edges"][MEMBER_OF] == 4


def test_a_group_with_zero_members_is_a_node_with_no_edges(session):
    from infra_brain import graph_engine

    _inventory_file(session)
    _hosts(session)

    graph_engine.emit_all(session, specs=_specs())

    node = session.execute(
        select(GraphNode).where(
            GraphNode.node_type == GROUP_NODE, GraphNode.natural_key == "decommissioned"
        )
    ).scalar_one()
    assert node.attributes == {"group": "decommissioned"}, "no members -> key ABSENT"
    assert node.resource_id is None, "the group is a shared value node, owned by no one row"


def test_a_group_name_in_two_inventory_files_manages_the_union(session):
    """The grouping rule that made the junction grammar sound: per VALUE.

    One group name in two inventory files is ONE node whose members are the
    deterministic union of both files' rows — never whichever file was
    materialised last.
    """
    from infra_brain import graph_engine

    _inventory_file(session, path="inventories/site-a.yml", groups={"linux": ["node_a"]})
    _inventory_file(session, path="inventories/site-b.yml", groups={"linux": ["git_runner"]})
    _hosts(session)

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    node = session.execute(
        select(GraphNode).where(GraphNode.node_type == GROUP_NODE, GraphNode.natural_key == "linux")
    ).scalar_one()
    assert node.attributes == {"group": "linux", "members": ["git_runner", "node_a"]}
    assert counts["edges"][MEMBER_OF] == 2
    assert _emitted_member_of(session) == _reconstruction_member_of(session)


def test_the_declaration_matches_what_this_suite_assumes():
    """Keep-in-step guard for the spec constants this oracle relies on."""
    spec = IaCAgent.spec
    group = next(n for n in spec.emits_nodes if n.type == GROUP_NODE)
    assert group.from_rows.path == (
        ("iac_files", "resource_id"),
        ("ansible_inventory_groups", "iac_file_id"),
    )
    assert group.from_rows.column == "name"
    (members,) = group.row_gathers
    assert members.path == (("ansible_inventory_hosts", "group_id"),)
    assert members.column == "name"
    edge = next(e for e in spec.emits_edges if e.type == MEMBER_OF)
    assert edge.from_node == GROUP_NODE
    assert edge.to_node == HOST_NODE
    assert edge.from_key == f"attributes.{members.key}"
    assert edge.from_key_multi is True
    assert edge.is_inverse, "MEMBER_OF is STORED host -> group; the join starts at the group"
    assert edge.written_as() == (HOST_NODE, GROUP_NODE)
    # The oracle above uses graph_match_key BECAUSE the declaration names it.
    assert edge.key_normalizer == "host"
    assert edge.method == "deterministic_match"
    assert float(edge.confidence) == 0.9, (
        "a hostname join is a display-name join; 1.000 is reserved for declared FKs"
    )

    from infra_brain.agents.linux import LinuxAgent

    real = next(n for n in LinuxAgent.spec.emits_nodes if n.type == HOST_NODE)
    stub = _LinuxSpecStub.spec.emits_nodes[0]
    assert (stub.type, stub.resource_type, stub.natural_key) == (
        real.type,
        real.resource_type,
        real.natural_key,
    ), "the stub drifted from linux's real declaration"
