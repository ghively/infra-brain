"""``iac`` declares ``<AnsibleInventoryFile> ─ANSIBLE_MANAGES→ <LinuxHost>``.

THE LAST hand-written relationship, and the only one whose migration was a
DECISION rather than a mechanism (TRK-354, Option A — re-anchor). Everything
mechanical had already landed by the time it was reached:

* Reach — ``ChildSpec`` walks ``resources → iac_files →
  ansible_inventory_groups → ansible_inventory_hosts`` by primary key, so the
  hostnames an inventory manages can ride onto the file node.
* Fan-out — one inventory names many hosts, which is what
  ``EdgeSpec.from_key_multi`` is for. Each name still resolves through the
  UNCHANGED key→one-target index.
* Spelling — ``key_normalizer="host"``, the same fold ``RUNS_ON`` uses.

What was left was OWNERSHIP. The deriver stored ``gitlab_project → host``: cicd
owns the source, linux owns the target, iac owned only the join rows, and
``AgentSpec`` requires ``EdgeSpec.from_node`` to be a node the declaring spec
emits. The decision taken here is to move the ANCHOR onto the inventory FILE iac
does own — the design doc's own argument, which called the project the "best
available" anchor only because the inventory group had no ``resources`` row.

WHY THERE IS NO EQUIVALENCE ORACLE IN THIS FILE
-----------------------------------------------
Every other migration in this series (BELONGS_TO, DEFINED_IN, RUNS_IMAGE) proved
the declarative path produced *the same edges* before its deriver was deleted,
and froze the deleted block here as a permanently-running oracle. That
discipline is INAPPLICABLE by construction: re-anchoring changes which edges
exist, so a byte-for-byte comparison would have to fail for the migration to be
doing its job.

It is replaced — not dropped — by a strictly weaker but still machine-checked
MAPPING claim, stated once and tested below:

    For every ``(project, host)`` pair the old deriver produced, there is at
    least one ``(inventory file, host)`` pair under the new edge such that that
    inventory file ``BELONGS_TO`` that project.

i.e. nothing the old edge asserted is lost; each old assertion is recovered,
more precisely, from an anchor one BELONGS_TO hop away. The converse is
deliberately NOT claimed: an inventory file in a project whose hosts the old
deriver could not reach is new coverage, which is the point.

``_deleted_deriver_ansible_manages`` is the old block, copied VERBATIM out of
``IaCAgent._emit_iac_edges`` (both halves — inventory groups AND playbook
plays), so the mapping claim keeps running instead of retiring the moment it was
first satisfied.

THE PLAYBOOK-PLAYS HALF, AND WHAT DROPPING IT COSTS
---------------------------------------------------
The deriver had two halves. The inventory half read
``ansible_inventory_hosts.name``; the plays half read
``ansible_playbook_plays.hosts``, a JSON-list column ``ChildSpec`` refuses
rather than flattens. Only the inventory half is re-anchored here, so the plays
half is dropped outright — and ``test_the_plays_half_contributes_no_host_the_
inventory_half_misses`` measures what that costs on the live shape rather than
assuming it costs nothing: every literal hostname a play targets is a host the
inventory already names, because a play's target must resolve through that same
inventory to run at all. Every other play target is a GROUP name or ``all``,
which the deriver already skipped (it only matched literal hostnames against
``resources.name``).
"""

from __future__ import annotations

import sys
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain.agents.cicd import CICDAgent
from infra_brain.agents.iac import IaCAgent
from infra_brain.db.models import (
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    AnsiblePlaybookPlay,
    GitlabProject,
    GraphEdge,
    GraphNode,
    IacFile,
    Resource,
)

from tests.support.pg import make_engine

ANSIBLE_MANAGES = "ANSIBLE_MANAGES"
BELONGS_TO = "BELONGS_TO"
INVENTORY_NODE = "AnsibleInventoryFile"
HOST_NODE = "LinuxHost"

PROJECT_ID = 113
PROJECT_PATH = "infra-brain/iac/homelab-ansible"
INVENTORY_PATH = "inventory/inventory.yml"

#: The live inventory, group for group. Reproduced from the deployed database
#: (``ansible_inventory_groups`` joined to ``ansible_inventory_hosts``) so the
#: mapping claim is measured against the shape it actually has to hold for —
#: note ``ai_node``/``node_a``/``media_host`` each appear in TWO groups, which is why
#: the gathered attribute has to be distinct.
_INVENTORY = {
    "adopted_dns": ["cloudflare_tunnel_a", "cloudflare_tunnel_b"],
    "docker_hosts": ["ai_node", "node_a", "media_host"],
    "linux": ["ai_node", "node_a", "git_runner", "media_host"],
    "synology": ["storage_node"],
}

#: The live playbooks, play for play — ``site.yml`` targeting groups, and
#: ``services-deploy.yml`` targeting two literal hosts. ``docker-deploy.yml``'s
#: ``all`` is included because "the deriver skipped it" is part of the claim.
_PLAYS = {
    "site.yml": [
        (0, "Base — All Linux hosts", ["linux"]),
        (1, "Docker Engine — Docker-capable hosts", ["docker_hosts"]),
        (4, "Cloudflare DNS — Zone records", ["adopted_dns"]),
        (5, "Synology — NAS services", ["synology"]),
        (6, "GitLab CE — git_runner configuration", ["git_runner"]),
        (7, "Host-specific stub roles — per host_vars", ["all"]),
    ],
    "services-deploy.yml": [
        (0, "Deploy Hermes agent services", ["ai_node"]),
        (1, "Deploy ARM systemd services and nginx", ["node_a"]),
    ],
    "docker-deploy.yml": [(0, "Deploy Docker stacks", ["all"])],
}

#: Every ``linux_host`` on the live box. ``storage_node`` is in the inventory but
#: named by no play; ``cloudflare_*`` likewise — both are why the two halves are
#: not interchangeable.
_HOSTS = [
    "cloudflare_tunnel_a",
    "cloudflare_tunnel_b",
    "ai_node",
    "node_a",
    "git_runner",
    "media_host",
    "storage_node",
]


@pytest.fixture()
def session():
    engine = make_engine()
    # No ``PRAGMA foreign_keys=ON``: the live deriver below inserts
    # ``resource_relationships`` rows through a raw-SQL shim, the same reason
    # the RUNS_IMAGE suite runs without it.
    with Session(engine) as s:
        yield s
        s.rollback()


def _specs():
    return {"iac": IaCAgent.spec, "cicd": CICDAgent.spec, "linux": _LinuxSpecStub.spec}


class _LinuxSpecStub:
    """``LinuxAgent.spec`` without importing the agent (and its ansible tools).

    The target node type has to exist for the edge to resolve, and it is
    linux's declaration — copying the one NodeSpec keeps this suite's import
    surface to the two collectors it is actually about. Kept in step by
    ``test_the_target_node_matches_the_linux_declaration``.
    """

    from infra_brain.etl.spec import AgentSpec, NodeSpec, Tier

    spec = AgentSpec(
        domain="linux",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=None,
        emits_nodes=(NodeSpec(type=HOST_NODE, resource_type="linux_host", natural_key="name"),),
    )


# --- fixture ---------------------------------------------------------------


def _project(session):
    resource = Resource(
        id=uuid.uuid4(),
        domain="cicd",
        type="gitlab_project",
        name=PROJECT_PATH,
        source="CICDAgent",
        metadata_={"project_id": PROJECT_ID, "default_branch": "main"},
    )
    session.add(resource)
    session.flush()
    session.add(
        GitlabProject(
            id=uuid.uuid4(),
            gitlab_project_id=PROJECT_ID,
            name="homelab-ansible",
            path_with_namespace=PROJECT_PATH,
            resource_id=resource.id,
        )
    )
    session.flush()
    return resource


def _iac_file(session, path, file_type, *, resource=True):
    res = None
    if resource:
        res = Resource(
            id=uuid.uuid4(),
            domain="iac",
            type={"inventory": "ansible_inventory_file", "playbook": "ansible_playbook"}[file_type],
            name=f"homelab-ansible/{path}",
            source="IaCAgent",
            metadata_={
                "project": "homelab-ansible",
                "project_id": PROJECT_ID,
                "file_path": path,
                "ref": "main",
            },
        )
        session.add(res)
        session.flush()
    row = IacFile(
        id=uuid.uuid4(),
        resource_id=res.id if res is not None else None,
        gitlab_project_id=PROJECT_ID,
        project_name="homelab-ansible",
        path=path,
        file_type=file_type,
        ref="main",
    )
    session.add(row)
    session.flush()
    return res, row


def _inventory(session, path=INVENTORY_PATH, groups=None):
    res, iac = _iac_file(session, path, "inventory")
    for gname, hosts in (groups or _INVENTORY).items():
        group = AnsibleInventoryGroup(id=uuid.uuid4(), iac_file_id=iac.id, name=gname)
        session.add(group)
        session.flush()
        for host in hosts:
            session.add(AnsibleInventoryHost(id=uuid.uuid4(), group_id=group.id, name=host))
    session.flush()
    return res, iac


def _playbooks(session):
    out = {}
    for path, plays in _PLAYS.items():
        res, iac = _iac_file(session, path, "playbook")
        for index, name, hosts in plays:
            session.add(
                AnsiblePlaybookPlay(
                    id=uuid.uuid4(),
                    iac_file_id=iac.id,
                    play_index=index,
                    name=name,
                    hosts=hosts,
                )
            )
        out[path] = res
    session.flush()
    return out


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


def _homelab_fixture(session):
    project = _project(session)
    inventory_res, _ = _inventory(session)
    playbooks = _playbooks(session)
    hosts = _hosts(session)
    return project, inventory_res, playbooks, hosts


# --- the two paths ----------------------------------------------------------


def _deleted_deriver_ansible_manages(session) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """The hand-written derivation this migration removed, copied verbatim.

    Both halves of ``IaCAgent._emit_iac_edges``' ANSIBLE_MANAGES derivation as
    they stood at commit 24f18b3 — deliberately dead code rather than a call
    into the agent, because the whole point of this commit is that
    ``_emit_iac_edges`` no longer contains it. Exactly one reduction: the
    ``edges.append({...})`` payload becomes the ``(from_id, to_id)`` pair under
    comparison. Do NOT "improve" it — its value is that it is unchanged,
    including the ``.lower()`` name match (which is NOT the ``graph_match_key``
    fold the declaration uses; see
    ``test_the_declaration_folds_a_spelling_the_deriver_could_not``).
    """
    pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()

    project_rid_map: dict[int, object] = {}
    for gp in session.query(GitlabProject).filter(GitlabProject.resource_id.isnot(None)).all():
        project_rid_map[gp.gitlab_project_id] = gp.resource_id

    linux_win_resources: dict[str, object] = {}
    for res in session.query(Resource).filter(Resource.domain.in_(["linux", "windows"])).all():
        linux_win_resources[res.name.lower()] = res.id

    if linux_win_resources:
        for group in session.query(AnsibleInventoryGroup).all():
            iac = session.query(IacFile).filter_by(id=group.iac_file_id).first()
            proj_rid = project_rid_map.get(iac.gitlab_project_id) if iac else None
            if not proj_rid:
                continue
            for host in session.query(AnsibleInventoryHost).filter_by(group_id=group.id).all():
                host_rid = linux_win_resources.get(host.name.lower())
                if host_rid:
                    pairs.add((proj_rid, host_rid))

    if linux_win_resources:
        for play in session.query(AnsiblePlaybookPlay).all():
            iac = session.query(IacFile).filter_by(id=play.iac_file_id).first()
            proj_rid = project_rid_map.get(iac.gitlab_project_id) if iac else None
            if not proj_rid:
                continue
            targets = play.hosts or []
            if isinstance(targets, str):
                targets = [targets]
            for target in targets:
                host_rid = linux_win_resources.get(str(target).lower())
                if host_rid:
                    pairs.add((proj_rid, host_rid))
    return pairs


def _deriver_halves(session) -> tuple[set, set]:
    """The same oracle, split so each half's contribution can be counted."""
    project_rid_map = {
        gp.gitlab_project_id: gp.resource_id
        for gp in session.query(GitlabProject).filter(GitlabProject.resource_id.isnot(None)).all()
    }
    linux_win = {
        res.name.lower(): res.id
        for res in session.query(Resource).filter(Resource.domain.in_(["linux", "windows"])).all()
    }
    inventory: set = set()
    plays: set = set()
    for group in session.query(AnsibleInventoryGroup).all():
        iac = session.query(IacFile).filter_by(id=group.iac_file_id).first()
        proj = project_rid_map.get(iac.gitlab_project_id) if iac else None
        if not proj:
            continue
        for host in session.query(AnsibleInventoryHost).filter_by(group_id=group.id).all():
            rid = linux_win.get(host.name.lower())
            if rid:
                inventory.add((proj, rid))
    for play in session.query(AnsiblePlaybookPlay).all():
        iac = session.query(IacFile).filter_by(id=play.iac_file_id).first()
        proj = project_rid_map.get(iac.gitlab_project_id) if iac else None
        if not proj:
            continue
        for target in play.hosts or []:
            rid = linux_win.get(str(target).lower())
            if rid:
                plays.add((proj, rid))
    return inventory, plays


def _engine_edges(session, edge_type=ANSIBLE_MANAGES) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """What the declarative engine writes, as ``(source resource, target resource)``.

    Both ends of this edge are resource-backed, so — unlike RUNS_IMAGE — the
    comparison can be made in resource space directly.
    """
    from infra_brain import graph_engine

    counts, errors = graph_engine.emit_all(session, specs=_specs())
    assert errors == [], errors
    pairs = set()
    for edge in session.execute(
        select(GraphEdge).where(GraphEdge.edge_type == edge_type, GraphEdge.valid_to.is_(None))
    ).scalars():
        src = session.get(GraphNode, edge.source_id)
        dst = session.get(GraphNode, edge.target_id)
        pairs.add((src.resource_id, dst.resource_id))
    if edge_type == ANSIBLE_MANAGES:
        assert counts["edges"].get(ANSIBLE_MANAGES, 0) == len(pairs)
    return pairs


# --- the declaration --------------------------------------------------------


def test_the_edge_is_declared_out_of_a_node_iac_owns():
    """The whole content of Option A, asserted rather than described.

    The ownership rule constrains ``from_node`` — the side the JOIN starts at —
    to a node the declaring spec emits. Under the old anchor that was
    ``GitlabProject``, cicd's; under this one it is ``AnsibleInventoryFile``,
    which iac declares three lines above the edge.
    """
    spec = IaCAgent.spec
    edge = next(e for e in spec.emits_edges if e.type == ANSIBLE_MANAGES)

    assert edge.from_node == INVENTORY_NODE
    assert edge.from_node in {n.type for n in spec.emits_nodes}, (
        "from_node must be a node THIS spec emits — that is the rule the old "
        "project anchor could not satisfy"
    )
    assert edge.to_node == HOST_NODE, "a foreign TARGET is fine; BELONGS_TO already has one"
    assert edge.written_as() == (INVENTORY_NODE, HOST_NODE), "stored inventory -> host"


def test_the_type_name_is_unchanged():
    """``ANSIBLE_MANAGES``, not a new ``MANAGES``. The anchor moved, not the meaning.

    Pinned because a rename is the tempting way to signal "this is a different
    edge now", and it would silently break continuity with the 14 legacy rows
    and with every blast-radius query that names the type.
    """
    types = {e.type for e in IaCAgent.spec.emits_edges}
    assert ANSIBLE_MANAGES in types
    assert "MANAGES" not in types


def test_the_deriver_is_gone_and_the_type_is_recorded_as_migrated():
    """Both halves deleted, and the store's own bookkeeping says so.

    ``MIGRATED_TO_GRAPH_EDGES`` means "no agent derives this into
    ``resource_relationships`` any more"; the declared-vs-emitted CI guard reads
    it. Asserting the source directly as well catches the half-migration where
    the set is updated but a block survives.

    P5 STRENGTHENED THIS. The source assertion used to read
    ``inspect.getsource(IaCAgent._emit_iac_edges)`` and check that the
    ANSIBLE_MANAGES block was absent from that method's body. P5 deleted
    ``_emit_iac_edges`` outright — the last hand-written edge deriver in this
    collector — so the check is now the stronger one it was always approximating:
    the method does not exist at all, and no code path in ``agents/iac.py``
    reaches the legacy emit functions. A block cannot survive in a method that
    is gone.
    """
    import inspect

    from infra_brain.db.relationships import MIGRATED_TO_GRAPH_EDGES, RelationshipType

    assert RelationshipType.ANSIBLE_MANAGES in MIGRATED_TO_GRAPH_EDGES

    assert not hasattr(IaCAgent, "_emit_iac_edges"), (
        "_emit_iac_edges was deleted in P5; a reappearance means a hand-written "
        "deriver came back into the collector"
    )

    module_src = inspect.getsource(sys.modules[IaCAgent.__module__])
    code_lines = [ln for ln in module_src.splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "RelationshipType.ANSIBLE_MANAGES" not in code
    for banned in ("emit_edge(", "emit_edges_batch("):
        assert banned not in code, (
            f"agents/iac.py must not call {banned} — the legacy edge store is dropped"
        )


def test_the_join_key_is_gathered_from_the_inventory_child_tables():
    """Reach: three PK hops, and only the inventory classification gathers them."""
    spec = IaCAgent.spec
    node = next(n for n in spec.emits_nodes if n.type == INVENTORY_NODE)

    (child,) = node.gathers
    assert child.key == "managed_hosts"
    assert child.column == "name"
    assert child.path == (
        ("iac_files", "resource_id"),
        ("ansible_inventory_groups", "iac_file_id"),
        ("ansible_inventory_hosts", "group_id"),
    )

    gathering = {n.type: n.gathers for n in spec.emits_nodes if n.gathers}
    assert set(gathering) == {"DockerComposeFile", INVENTORY_NODE}, (
        "only the two classifications with graph-bearing child rows gather"
    )

    edge = next(e for e in spec.emits_edges if e.type == ANSIBLE_MANAGES)
    assert edge.from_key == f"attributes.{child.key}"
    assert edge.from_key_multi is True, "one inventory names many hosts"


def test_the_confidence_is_the_derivers_and_may_not_claim_declared():
    """A hostname STRING matched against a separately-collected host entity."""
    edge = next(e for e in IaCAgent.spec.emits_edges if e.type == ANSIBLE_MANAGES)
    assert edge.method == "deterministic_match"
    assert float(edge.confidence) == 0.9, "the deriver's inventory-half confidence"
    assert edge.key_normalizer == "host", "same fold RUNS_ON uses"


def test_the_target_node_matches_the_linux_declaration():
    """The stub above must stay identical to what ``LinuxAgent`` actually declares."""
    from infra_brain.agents.linux import LinuxAgent

    real = next(n for n in LinuxAgent.spec.emits_nodes if n.type == HOST_NODE)
    stub = next(n for n in _LinuxSpecStub.spec.emits_nodes if n.type == HOST_NODE)
    assert (real.type, real.resource_type, real.natural_key) == (
        stub.type,
        stub.resource_type,
        stub.natural_key,
    )


# --- the mapping claim (this migration's replacement for equivalence) -------


def test_every_old_pair_maps_onto_a_new_pair_through_belongs_to(session):
    """THE gate. Not equality — a mapping, because the anchor deliberately moved.

    For each ``(project, host)`` the deriver produced, find ≥1
    ``(inventory file, host)`` the declaration produced whose file BELONGS_TO
    that project. That is exactly "nothing the old edge asserted is lost".
    """
    _homelab_fixture(session)

    old = _deleted_deriver_ansible_manages(session)
    new = _engine_edges(session)
    belongs_to = _engine_edges(session, edge_type=BELONGS_TO)

    assert old, "fixture produced no deriver edges — it is not exercising anything"
    assert new, "fixture produced no declared edges — it is not exercising anything"

    # file -> the project it belongs to, straight out of the graph.
    project_of = dict(belongs_to)

    unmapped = []
    for project_rid, host_rid in sorted(old, key=str):
        witnesses = [
            file_rid
            for (file_rid, new_host_rid) in new
            if new_host_rid == host_rid and project_of.get(file_rid) == project_rid
        ]
        if not witnesses:
            unmapped.append((project_rid, host_rid))
    assert unmapped == [], (
        f"{len(unmapped)} old (project, host) assertion(s) have no re-anchored "
        "counterpart — the migration would be LOSING coverage, not moving it"
    )


def test_the_new_edges_are_the_live_inventory_exactly(session):
    """Counted, not trusted: 1 inventory file x 7 distinct hosts = 7 edges.

    The live database holds 14 ``ANSIBLE_MANAGES`` rows, which is 7 distinct
    ``(project, host)`` pairs doubled by cicd's L-8b project rename (two
    ``resources`` rows share one ``gitlab_project_id``). The re-anchored edge
    keys on the FILE, which the rename never touched, so the doubling
    disappears: one row per real assertion.
    """
    _project, inventory_res, _playbooks, hosts = _homelab_fixture(session)

    new = _engine_edges(session)

    assert new == {(inventory_res.id, hosts[name].id) for name in _HOSTS}
    assert len(new) == len(_HOSTS) == 7


def test_a_host_in_two_groups_is_one_edge(session):
    """``ai_node`` is in ``docker_hosts`` AND ``linux``; the gathered list is distinct.

    The deriver deduped after the fact via the legacy store's
    ``UNIQUE(from, to, type)``. The declaration never produces the duplicate,
    because ``_child_values`` collapses repeats before the edge is written —
    the same answer by a better route.
    """
    _project(session)
    inventory_res, _ = _inventory(session)
    hosts = _hosts(session)

    _engine_edges(session)
    node = session.execute(
        select(GraphNode).where(GraphNode.node_type == INVENTORY_NODE)
    ).scalar_one()

    assert node.attributes["managed_hosts"] == sorted(set(_HOSTS))
    into_ai_node = [
        e
        for e in session.execute(
            select(GraphEdge).where(GraphEdge.edge_type == ANSIBLE_MANAGES)
        ).scalars()
        if session.get(GraphNode, e.target_id).resource_id == hosts["ai_node"].id
    ]
    assert len(into_ai_node) == 1


# --- what dropping the plays half costs -------------------------------------


def test_the_plays_half_contributes_no_host_the_inventory_half_misses(session):
    """The coverage question, MEASURED on the live shape rather than assumed.

    ``ansible_playbook_plays.hosts`` is a JSON-list column ``ChildSpec`` refuses
    rather than flattens, so the plays half cannot be re-anchored and is dropped.
    The claim that costs nothing is checked here: every host the plays half
    reached (``ai_node``, ``node_a``, ``git_runner`` — the only literal hostnames any
    play targets) is one the inventory half already names. The rest of the play
    targets are GROUP names (``linux``, ``docker_hosts``, ``adopted_dns``,
    ``synology``) or ``all``, which the deriver skipped too — it only matched a
    target against ``resources.name``.
    """
    _homelab_fixture(session)

    inventory_half, plays_half = _deriver_halves(session)

    assert plays_half, "the fixture must actually exercise the plays half"
    assert plays_half <= inventory_half, (
        "a play target the inventory does not name would be REAL lost coverage: "
        f"{sorted(map(str, plays_half - inventory_half))}"
    )
    # And concretely: three literal hostnames, all of them inventory members.
    hosts_from_plays = {
        session.get(Resource, rid).name
        for _project_rid, rid in plays_half  # noqa: F841
    }
    assert hosts_from_plays == {"ai_node", "node_a", "git_runner"}
    assert hosts_from_plays <= set(_HOSTS)


def test_a_play_naming_a_host_outside_the_inventory_would_be_caught(session):
    """The guard above is not vacuous — it fails when coverage really is lost.

    Deliberately constructs the case the live data does not have (a playbook
    targeting a literal host no inventory group lists) and asserts the plays
    half then reaches something the inventory half does not, so
    ``test_the_plays_half_contributes_no_host_the_inventory_half_misses``
    would fail rather than passing on an empty set.
    """
    _project(session)
    _inventory(session, groups={"linux": ["git_runner"]})
    _iac_file(session, "orphan.yml", "playbook")
    orphan = session.query(IacFile).filter_by(path="orphan.yml").one()
    session.add(
        AnsiblePlaybookPlay(
            id=uuid.uuid4(), iac_file_id=orphan.id, play_index=0, name="x", hosts=["media_host"]
        )
    )
    session.flush()
    _hosts(session, names=["git_runner", "media_host"])

    inventory_half, plays_half = _deriver_halves(session)
    assert not (plays_half <= inventory_half)


def test_the_json_list_column_is_still_refused_not_flattened(session):
    """Why the plays half cannot simply be re-anchored the same way.

    ``ansible_playbook_plays.hosts`` is JSON; ``_child_values`` skips a
    non-scalar rather than inventing a fan-out the declaration never asked for.
    Asserted directly against the engine so the refusal stays a property of the
    contract, not a claim in a comment.
    """
    from infra_brain.etl.spec import ChildSpec, NodeSpec
    from infra_brain.graph_engine import _child_values

    _project(session)
    _playbooks(session)

    node_spec = NodeSpec(type="AnsiblePlaybook", resource_type="ansible_playbook")
    values = _child_values(
        session,
        "iac",
        node_spec,
        ChildSpec(
            key="play_hosts",
            path=(("iac_files", "resource_id"), ("ansible_playbook_plays", "iac_file_id")),
            column="hosts",
        ),
    )
    assert values == {}, "a JSON-list child column yields no keys, by design"
