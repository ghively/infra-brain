"""P2: ``iac`` declares ``<IaC file> ─BELONGS_TO→ GitlabProject``.

The second real declaration against the P0 contract
(docs/decisions/2026-08-11-graph-first-architecture.md §5, phase P2), and the
first one migrated OUT of a hand-written deriver rather than added greenfield.

Why this relationship and not one of the other three P2 candidates: of the four
genuine cross-entity relationships in this database (`BELONGS_TO` 68 rows,
`DEFINED_IN` 68, `RUNS_IMAGE` 33, `ANSIBLE_MANAGES` 14) it is the only one whose
join is **functional** — many IaC files, one owning project. The contract's
``EdgeSpec`` builds a ``key -> single target node`` index and drops any key two
targets claim, so it can express many-to-one and nothing else. The other three
all fan OUT from one node (a project defines many files, a compose file runs
many images, a project manages many hosts) and are deliberately left in
``graph_maintenance`` — see that module and the P2 report for the exact gap.

THE EQUIVALENCE RULE (the whole point of this file)
---------------------------------------------------
The declarative path replaces working code, so "it produces edges" is not the
bar — it must produce THE SAME edges. Every test below runs both writers over
one fixture and compares the resolved ``(from_resource, to_resource, type)``
triples. The deriver side is a VERBATIM COPY of the block deleted from
``IaCAgent._emit_iac_edges`` (see ``_deleted_deriver_belongs_to``), frozen here
so the equivalence claim keeps running instead of retiring the moment it was
first satisfied. It was checked against the live shipped code in the commit
immediately before the deletion; this file is what keeps it true afterwards.

This environment is a homelab. Its GitLab holds Ansible, compose and CI files;
the terraform and k8s classifications are live code paths with no rows here, so
they are declared and fixtured but not invented into the live-data claims.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain.agents.cicd import CICDAgent
from infra_brain.agents.iac import IaCAgent
from infra_brain.db.models import GitlabProject, GraphEdge, GraphNode, IacFile, Resource

from tests.support.pg import make_engine

BELONGS_TO = "BELONGS_TO"
PROJECT_NODE = "GitlabProject"

#: The live shape of this homelab's IaC estate, as five (resource_type,
#: file_type, name) triples. ``file_type`` is iac.py's ``_TYPE_TO_FILE_TYPE``
#: translation — the deriver joins on ``iac_files`` rows, so the fixture has
#: to carry both spellings for the two writers to be comparing the same files.
_LIVE_FILES = [
    (
        "docker_compose",
        "compose",
        "homelab-ansible/roles/docker_stack/files/glance/docker-compose.yml",
    ),
    ("ansible_inventory_file", "inventory", "homelab-ansible/inventory/group_vars/all.yml"),
    ("ansible_playbook", "playbook", "homelab-ansible/docker-deploy.yml"),
    ("ansible_requirements", "requirements", "homelab-ansible/requirements.yml"),
    ("gitlab_ci_pipeline", "gitlab_ci", "homelab-ansible/.gitlab-ci.yml"),
]
#: Classifications iac.py can still produce that this homelab has no rows for.
#: Declared anyway: the deriver's join is type-agnostic, so omitting them would
#: make the declarative path silently narrower the day someone commits a .tf.
_UNPOPULATED_FILES = [
    ("terraform_file", "terraform", "homelab-ansible/tf/main.tf"),
    ("k8s_manifest", "k8s_manifest", "homelab-ansible/k8s/deploy.yml"),
]


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
    return {"iac": IaCAgent.spec, "cicd": CICDAgent.spec}


def _project_resource(session, name, project_id, *, retired=False):
    r = Resource(
        id=uuid.uuid4(),
        domain="cicd",
        type="gitlab_project",
        name=name,
        source="CICDAgent",
        metadata_={"project_id": project_id, "default_branch": "main"},
        retired_at=datetime.now(UTC) if retired else None,
    )
    session.add(r)
    session.flush()
    return r


def _iac_resource(session, rtype, name, project_id):
    r = Resource(
        id=uuid.uuid4(),
        domain="iac",
        type=rtype,
        name=name,
        source="IaCAgent",
        metadata_={
            "project": "homelab-ansible",
            "project_id": project_id,
            "file_path": name.split("/", 1)[1],
            "ref": "main",
        },
    )
    session.add(r)
    session.flush()
    return r


def _iac_file(session, *, project_id, path, file_type, resource_id):
    session.add(
        IacFile(
            id=uuid.uuid4(),
            resource_id=resource_id,
            gitlab_project_id=project_id,
            project_name="homelab-ansible",
            path=path,
            file_type=file_type,
            ref="main",
        )
    )


def _homelab_fixture(session, *, include_unpopulated=False):
    """Build this homelab's actual IaC shape: one repo, one project, N files.

    Returns ``(project_resource, {resource_type: Resource})``.
    """
    proj = _project_resource(session, "infra-brain/iac/homelab-ansible", 113)
    session.add(
        GitlabProject(
            id=uuid.uuid4(),
            gitlab_project_id=113,
            name="homelab-ansible",
            path_with_namespace="infra-brain/iac/homelab-ansible",
            resource_id=proj.id,
        )
    )
    files = _LIVE_FILES + (_UNPOPULATED_FILES if include_unpopulated else [])
    made: dict[str, Resource] = {}
    for rtype, file_type, name in files:
        res = _iac_resource(session, rtype, name, 113)
        _iac_file(
            session,
            project_id=113,
            path=name.split("/", 1)[1],
            file_type=file_type,
            resource_id=res.id,
        )
        made[rtype] = res
    session.flush()
    return proj, made


# --- the two paths ----------------------------------------------------------


def _deleted_deriver_belongs_to(session) -> set[tuple[uuid.UUID, uuid.UUID, str]]:
    """The hand-written derivation this migration removed, copied verbatim.

    This is the equivalence ORACLE, and it is deliberately dead code rather than
    a call into ``iac.py``: the whole point of P2 is that ``_emit_iac_edges`` no
    longer contains this, so the test cannot ask it. Freezing the block here
    keeps the equivalence claim *running* instead of retiring it the moment it
    was satisfied — the engine has to keep matching the deriver, not merely have
    matched it once on the day of the change.

    The body below is the ``--- iac_file BELONGS_TO gitlab_project ---`` block
    of ``IaCAgent._emit_iac_edges`` as it stood at commit 0a6d9e5, with only the
    ``edges.append(...)`` payload reduced to the triple under comparison.
    Do NOT "improve" it; its value is that it is unchanged.
    """
    iac_files = session.query(IacFile).filter(IacFile.resource_id.isnot(None)).all()
    project_rid_map: dict[int, object] = {}
    for gp in session.query(GitlabProject).filter(GitlabProject.resource_id.isnot(None)).all():
        project_rid_map[gp.gitlab_project_id] = gp.resource_id

    triples = set()
    for iac in iac_files:
        proj_rid = project_rid_map.get(iac.gitlab_project_id)
        if proj_rid:
            triples.add((iac.resource_id, proj_rid, BELONGS_TO))
    return triples


# ``_emitted_edge_types`` — DELETED (P5). It captured every relationship
# ``IaCAgent._emit_iac_edges`` still wrote by monkeypatching
# ``agents.iac.emit_edges_batch`` and invoking the method on a bare instance.
# P5 deleted ``_emit_iac_edges`` (and the module's import of the legacy emit
# functions) so there is nothing to invoke and nothing to patch. The one test
# that used it now asserts the absence structurally instead — see
# ``test_the_collector_no_longer_derives_belongs_to``.


def _engine_belongs_to(session) -> set[tuple[uuid.UUID, uuid.UUID, str]]:
    """What the declarative engine writes, resolved back to resource ids.

    Resolving through ``graph_nodes.resource_id`` is what makes the comparison
    meaningful: the two stores key edges differently (resource ids vs node ids),
    so comparing raw ids would compare nothing.
    """
    from infra_brain import graph_engine

    counts, errors = graph_engine.emit_all(session, specs=_specs())
    assert errors == [], errors
    triples = set()
    for edge in session.execute(
        select(GraphEdge).where(GraphEdge.edge_type == BELONGS_TO, GraphEdge.valid_to.is_(None))
    ).scalars():
        src = session.get(GraphNode, edge.source_id)
        dst = session.get(GraphNode, edge.target_id)
        assert src.resource_id is not None and dst.resource_id is not None, (
            "both ends of BELONGS_TO are inventory items — a node without a "
            "resource_id means resource_backed was declared wrong"
        )
        triples.add((src.resource_id, dst.resource_id, BELONGS_TO))
    assert counts["edges"].get(BELONGS_TO, 0) == len(triples)
    return triples


# --- the declaration --------------------------------------------------------


def test_iac_declares_belongs_to_for_every_file_classification():
    """Every type ``_classify_yaml`` can emit needs its own node + edge.

    ``NodeSpec.resource_type`` takes ONE ``resources.type``, so the single
    concept "an IaC file" costs one node type and one EdgeSpec per
    classification. That is a contract cost, recorded here so a new
    classification added to ``iac.py`` without a matching declaration fails a
    test instead of silently vanishing from the graph.
    """
    from infra_brain.agents.iac import _IAC_FILE_NODE_TYPES, _TYPE_TO_FILE_TYPE

    spec = IaCAgent.spec
    declared = {n.resource_type for n in spec.emits_nodes}
    assert declared == set(_TYPE_TO_FILE_TYPE), (
        "iac emits a resource type it does not declare a node for (or vice versa)"
    )
    # FILE node types, not every node iac declares: ``ContainerImage`` is a
    # shared VALUE node read out of ``compose_services`` (see
    # tests/agents/test_iac_runs_image_graph.py), not a file classification, so
    # it has no project of its own to belong to.
    node_types = set(_IAC_FILE_NODE_TYPES.values())
    edges = [e for e in spec.emits_edges if e.type == BELONGS_TO]
    assert {e.from_node for e in edges} == node_types
    for edge in edges:
        assert edge.to_node == PROJECT_NODE
        assert edge.from_key == "attributes.project_id"
        assert edge.to_key == "attributes.project_id"
        # GitLab's numeric project id is an immutable key, not a display name,
        # so this join really is FK-strength and may honestly claim 1.000 —
        # which is what the deleted deriver emitted.
        assert edge.method == "declared"
        assert float(edge.confidence) == 1.0


def test_every_iac_node_carries_the_join_key():
    """The engine matches graph-side: a metadata key not copied is invisible.

    Scoped to the FILE nodes. ``ContainerImage`` is a shared value node — one
    ``nginx:1.25`` for however many compose files name it — so it cannot carry
    a ``project_id`` without picking one of them, which is also why it is not
    resource-backed. Asserted rather than skipped, so "it has no attributes" is
    a recorded decision instead of an omission.
    """
    from infra_brain.agents.iac import _IAC_FILE_NODE_TYPES

    file_nodes = set(_IAC_FILE_NODE_TYPES.values())
    for node in IaCAgent.spec.emits_nodes:
        if node.type in file_nodes:
            assert "project_id" in node.attributes, node.type
        else:
            assert node.from_rows is not None and not node.resource_backed, node.type
            assert node.attributes == (), node.type


def test_cicd_declares_the_project_node():
    """The target entity belongs to the cicd collector, not to iac.

    iac knows a project *id*; the project itself is cicd's to name. Declaring
    it there is what keeps ``EdgeSpec.from_node`` ownership honest.
    """
    spec = CICDAgent.spec
    assert [n.type for n in spec.emits_nodes] == [PROJECT_NODE]
    node = spec.emits_nodes[0]
    assert node.resource_type == "gitlab_project"
    assert "project_id" in node.attributes
    assert node.natural_key == "metadata.project_id", (
        "the node's identity must be GitLab's immutable numeric id, not the "
        "mutable path_with_namespace — see the NodeSpec's comment and "
        "test_a_project_rename_does_not_split_the_node"
    )
    assert node.name == "name", "the DISPLAY name is still the readable path"
    assert spec.emits_edges == (), "cicd contributes the entity, not the relationship"


# --- the removal ------------------------------------------------------------


def _with_ansible_inventory(session):
    """Give the fixture a managed host, so ANSIBLE_MANAGES has something to find."""
    from infra_brain.db.models import AnsibleInventoryGroup, AnsibleInventoryHost

    inv = session.query(IacFile).filter_by(file_type="inventory").first()
    group = AnsibleInventoryGroup(id=uuid.uuid4(), iac_file_id=inv.id, name="docker_hosts")
    session.add(group)
    session.flush()
    session.add(AnsibleInventoryHost(id=uuid.uuid4(), group_id=group.id, name="node_a"))
    session.add(
        Resource(
            id=uuid.uuid4(),
            domain="linux",
            type="linux_host",
            name="node_a",
            source="LinuxAgent",
            metadata_={},
        )
    )
    session.flush()


def test_the_collector_no_longer_derives_belongs_to():
    """One writer per relationship. Two would be a slow-motion contradiction.

    Both stores upsert, so a lingering second writer looks fine — right up
    until the day the two disagree and each keeps insisting on its own answer
    in its own table.

    ANSIBLE_MANAGES used to be asserted STILL PRESENT in the same breath — it
    was the relationship that could not be declared. TRK-354 Option A answered
    that (re-anchored onto the ``AnsibleInventoryFile`` iac owns), so the claim
    INVERTED rather than disappearing: it had to be ABSENT too, for the same
    one-writer reason BELONGS_TO is.

    P5 MADE THE CLAIM STRUCTURAL. This used to seed a fixture, run
    ``_emit_iac_edges`` with a patched ``emit_edges_batch``, and assert neither
    type appeared in what it wrote. P5 deleted that method — the collector's
    last hand-written deriver — so "does it still derive BELONGS_TO?" is
    answered by the absence of any deriver at all, and of any call into the
    legacy emit path. That is a strictly stronger assertion than the behavioural
    one it replaces, and it cannot be defeated by a new block in a new method
    (the module-wide scan below catches that too).
    """
    import inspect
    import sys

    assert not hasattr(IaCAgent, "_emit_iac_edges"), (
        "_emit_iac_edges was deleted in P5; its return means a hand-written "
        "deriver came back and BELONGS_TO may have two writers again"
    )

    module_src = inspect.getsource(sys.modules[IaCAgent.__module__])
    code = "\n".join(ln for ln in module_src.splitlines() if not ln.lstrip().startswith("#"))
    for banned in ("emit_edge(", "emit_edges_batch("):
        assert banned not in code, (
            f"agents/iac.py must not call {banned} — every relationship it owns is "
            "declared on spec.emits_edges and materialised into graph_edges"
        )
    assert f"RelationshipType.{BELONGS_TO}" not in code
    assert "RelationshipType.ANSIBLE_MANAGES" not in code


# --- equivalence ------------------------------------------------------------


def test_engine_reproduces_the_deleted_deriver_exactly(session):
    """THE P2 gate: same fixture, both writers, identical triples."""
    _homelab_fixture(session, include_unpopulated=True)

    deriver = _deleted_deriver_belongs_to(session)
    engine = _engine_belongs_to(session)

    assert deriver, "fixture produced no deriver edges — it is not exercising anything"
    assert engine == deriver


def test_equivalence_holds_for_the_live_classification_mix(session):
    """The five classifications this homelab actually has, and only those."""
    _proj, made = _homelab_fixture(session)

    deriver = _deleted_deriver_belongs_to(session)
    engine = _engine_belongs_to(session)

    assert len(engine) == len(_LIVE_FILES)
    assert {t[0] for t in engine} == {r.id for r in made.values()}
    assert engine == deriver


def test_file_with_no_resource_row_yields_no_edge_either_way(session):
    """``iac_files.resource_id`` is nullable; half this homelab's rows are null.

    The deriver filtered those out. The engine never sees them at all — it
    reads ``resources``, so an unlinked file is not an entity. Same answer by
    two different routes, which is the strongest form of the equivalence.
    """
    _homelab_fixture(session)
    _iac_file(
        session,
        project_id=113,
        path="unlinked/docker-compose.yml",
        file_type="compose",
        resource_id=None,
    )
    session.flush()

    assert _deleted_deriver_belongs_to(session) == _engine_belongs_to(session)


def test_project_with_no_resource_row_yields_no_edge_either_way(session):
    """A GitLab project cicd has not (yet) inventoried is not a target."""
    _homelab_fixture(session)
    session.add(
        GitlabProject(
            id=uuid.uuid4(),
            gitlab_project_id=999,
            name="uninventoried",
            resource_id=None,
        )
    )
    orphan = _iac_resource(session, "docker_compose", "uninventoried/docker-compose.yml", 999)
    _iac_file(
        session,
        project_id=999,
        path="docker-compose.yml",
        file_type="compose",
        resource_id=orphan.id,
    )
    session.flush()

    deriver = _deleted_deriver_belongs_to(session)
    engine = _engine_belongs_to(session)
    assert engine == deriver
    assert orphan.id not in {t[0] for t in engine}
    # ...but the file is still an entity in its own right.
    assert session.execute(select(GraphNode).where(GraphNode.resource_id == orphan.id)).scalar_one()


def test_retired_duplicate_project_resource_is_not_a_target(session):
    """Live data really has one: a project renamed between two cicd runs.

    ``resources`` holds both the old name and the new one with the SAME
    ``metadata.project_id``, the old one carrying ``retired_at``. If the engine
    read retired rows, that id would be claimed by two nodes, the target index
    would (correctly) refuse to guess, and BELONGS_TO would collapse to zero
    edges instead of picking wrong. It reads live rows only, so there is
    nothing to guess between.
    """
    proj, _made = _homelab_fixture(session)
    _project_resource(session, "homelab-ansible", 113, retired=True)
    session.flush()

    engine = _engine_belongs_to(session)

    assert len(engine) == len(_LIVE_FILES)
    assert {t[1] for t in engine} == {proj.id}


def test_a_project_rename_does_not_split_the_node(session):
    """The failure mode the numeric natural key exists to prevent.

    Renaming a repo (or cicd's own L-8b resource-naming change) retires the old
    ``resources`` row and creates a new one with a new name and the SAME
    ``project_id``. Nothing retires the old NODE — node retirement is
    deliberately deferred. Had the node been keyed on the name, the rename would
    leave two GitlabProject nodes claiming one join key, the target index would
    correctly refuse to guess between them, and every BELONGS_TO edge for that
    project would vanish. Keyed on the id, the rename updates one node in place.
    """
    proj, _made = _homelab_fixture(session)
    _engine_belongs_to(session)

    # The rename: yesterday's row retired, today's row created, same id.
    proj.retired_at = datetime.now(UTC)
    renamed = _project_resource(session, "platform/homelab-ansible", 113)
    session.flush()

    engine = _engine_belongs_to(session)

    assert len(engine) == len(_LIVE_FILES), (
        "a rename must not cost this project its BELONGS_TO edges"
    )
    node = session.execute(
        select(GraphNode).where(GraphNode.node_type == PROJECT_NODE)
    ).scalar_one()
    assert node.natural_key == "113"
    assert node.name == "platform/homelab-ansible", "the node followed the rename"
    assert node.resource_id == renamed.id
    assert {t[1] for t in engine} == {renamed.id}


def test_engine_is_idempotent(session):
    """A second pass supersedes nothing — the edges are the same edges."""
    _homelab_fixture(session)
    first = _engine_belongs_to(session)
    second = _engine_belongs_to(session)

    assert first == second
    assert len(
        list(session.execute(select(GraphEdge).where(GraphEdge.edge_type == BELONGS_TO)).scalars())
    ) == len(first), "a re-run must upsert, not accumulate a second generation of edges"
    assert _deleted_deriver_belongs_to(session) == second
