"""P2 (cont.): ``iac`` declares ``<GitlabProject> ─DEFINED_IN→ <IaC file>``.

The first relationship migrated onto ``EdgeDirection.INVERSE``, and the reason
that field exists. ``DEFINED_IN`` is the EXACT INVERSE of ``BELONGS_TO`` (see
``tests/agents/test_iac_belongs_to_graph.py``): same relational join — the
file's ``project_id`` — opposite arrow. P2 migrated BELONGS_TO and had to leave
this one hand-written in ``graph_maintenance``, because one project defines
MANY files, so the fan-out end has to be the edge's SOURCE and an ``EdgeSpec``
could only ever store ``from_node → to_node``.

WHAT ACTUALLY CHANGED, AND WHAT DID NOT. ``direction`` moves the stored arrow.
It does not move the join: the engine still resolves each FILE to its ONE
project through the same key-to-one-target index, and still refuses to guess if
two projects claim a key. Nothing about the ambiguity rule was relaxed to make
this work — see ``tests/test_graph_engine.py``'s
``test_a_many_to_many_relationship_is_still_not_expressible``, which pins the
boundary that remains.

THE EQUIVALENCE RULE (the whole point of this file)
---------------------------------------------------
The declarative path replaces working code, so "it produces edges" is not the
bar — it must produce THE SAME edges. Every test below runs both writers over
one fixture and compares the resolved ``(from_resource, to_resource, type)``
triples. The deriver side is a VERBATIM COPY of the block deleted from
``GraphMaintenanceAgent._populate_typed_relationships`` (see
``_deleted_deriver_defined_in``), frozen here so the equivalence claim keeps
running instead of retiring the moment it was first satisfied. It was checked
against the live shipped code in the commit immediately before the deletion;
this file is what keeps it true afterwards.

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
from infra_brain.etl.spec import EdgeDirection

from tests.support.pg import make_engine

DEFINED_IN = "DEFINED_IN"
BELONGS_TO = "BELONGS_TO"
PROJECT_NODE = "GitlabProject"

#: Same live shape as the BELONGS_TO suite — the two relationships are one join
#: read two ways, so they must be exercised over the same estate.
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
    """Build this homelab's actual IaC shape: one repo, one project, N files."""
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


def _deleted_deriver_defined_in(session) -> set[tuple[uuid.UUID, uuid.UUID, str]]:
    """The hand-written derivation this migration removed, copied verbatim.

    This is the equivalence ORACLE, and it is deliberately dead code rather than
    a call into ``graph_maintenance``: the whole point of the migration is that
    ``_populate_typed_relationships`` no longer contains this, so the test
    cannot ask it. Freezing the block here keeps the equivalence claim *running*
    instead of retiring it the moment it was satisfied — the engine has to keep
    matching the deriver, not merely have matched it once on the day of the
    change.

    The body below is the ``── 5. DEFINED_IN`` block of
    ``GraphMaintenanceAgent._populate_typed_relationships`` as it stood at
    commit 8123689, with only the ``defined_in_edges.append(...)`` payload
    reduced to the triple under comparison. Do NOT "improve" it; its value is
    that it is unchanged — including the ``seen_di`` dedup, which is why a
    project with two files still yields two DISTINCT edges and not one.
    """
    triples: set[tuple[uuid.UUID, uuid.UUID, str]] = set()

    iac_files = (
        session.query(IacFile.resource_id, IacFile.gitlab_project_id)
        .filter(IacFile.resource_id.isnot(None))
        .all()
    )

    if iac_files:
        project_rid_map: dict[int, object] = {}
        for gp in (
            session.query(GitlabProject.gitlab_project_id, GitlabProject.resource_id)
            .filter(GitlabProject.resource_id.isnot(None))
            .all()
        ):
            project_rid_map[gp.gitlab_project_id] = gp.resource_id

        seen_di: set[tuple[str, str]] = set()
        for iac in iac_files:
            proj_rid = project_rid_map.get(iac.gitlab_project_id)
            if proj_rid and iac.resource_id:
                # proj_resource DEFINED_IN iac_file_resource
                key = (str(proj_rid), str(iac.resource_id))
                if key not in seen_di:
                    seen_di.add(key)
                    triples.add((proj_rid, iac.resource_id, DEFINED_IN))
    return triples


def _engine_defined_in(session) -> set[tuple[uuid.UUID, uuid.UUID, str]]:
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
        select(GraphEdge).where(GraphEdge.edge_type == DEFINED_IN, GraphEdge.valid_to.is_(None))
    ).scalars():
        src = session.get(GraphNode, edge.source_id)
        dst = session.get(GraphNode, edge.target_id)
        assert src.resource_id is not None and dst.resource_id is not None, (
            "both ends of DEFINED_IN are inventory items — a node without a "
            "resource_id means resource_backed was declared wrong"
        )
        triples.add((src.resource_id, dst.resource_id, DEFINED_IN))
    assert counts["edges"].get(DEFINED_IN, 0) == len(triples)
    return triples


# --- the declaration --------------------------------------------------------


def test_iac_declares_defined_in_for_every_file_classification():
    """One declaration per classification, exactly as BELONGS_TO has.

    ``NodeSpec.resource_type`` takes ONE ``resources.type``, so the single
    concept "an IaC file" costs one node type and one EdgeSpec per
    classification. Recorded here so a new classification added to ``iac.py``
    without a matching declaration fails a test instead of silently vanishing
    from the graph.
    """
    from infra_brain.agents.iac import _IAC_FILE_NODE_TYPES, _TYPE_TO_FILE_TYPE

    spec = IaCAgent.spec
    # The FILE nodes. ``ContainerImage`` also reads ``docker_compose``
    # resources, but as the PARENT of the child rows it is identified by — it
    # is a value node, not a file classification, so it is not a DEFINED_IN
    # source. See tests/agents/test_iac_runs_image_graph.py.
    node_types = set(_IAC_FILE_NODE_TYPES.values())
    assert {n.resource_type for n in spec.emits_nodes} == set(_TYPE_TO_FILE_TYPE)

    edges = [e for e in spec.emits_edges if e.type == DEFINED_IN]
    assert {e.from_node for e in edges} == node_types
    for edge in edges:
        assert edge.to_node == PROJECT_NODE
        assert edge.direction is EdgeDirection.INVERSE
        # The STORED arrow runs project -> file. That is the fan-out.
        assert edge.written_as() == (PROJECT_NODE, edge.from_node)
        # FK-strength on GitLab's immutable numeric id, which is what the
        # deleted deriver claimed too (confidence 1.0).
        assert edge.method == "declared"
        assert float(edge.confidence) == 1.0


def test_defined_in_is_belongs_to_with_the_arrow_reversed():
    """The two declarations must differ ONLY in type and direction.

    They are one relational join read two ways. If their keys, normalizer,
    method or confidence ever diverge, one of the two is describing a join that
    does not exist — and because both are FK-strength 1.000, a divergence would
    be asserted at full confidence.
    """
    spec = IaCAgent.spec
    belongs = {e.from_node: e for e in spec.emits_edges if e.type == BELONGS_TO}
    defined = {e.from_node: e for e in spec.emits_edges if e.type == DEFINED_IN}
    assert set(belongs) == set(defined)
    for node_type, d in defined.items():
        b = belongs[node_type]
        assert (d.from_key, d.to_key) == (b.from_key, b.to_key)
        assert (d.key_normalizer, d.method, d.confidence) == (
            b.key_normalizer,
            b.method,
            b.confidence,
        )
        assert b.direction is EdgeDirection.FORWARD
        assert d.direction is EdgeDirection.INVERSE
        assert d.written_as() == tuple(reversed(b.written_as()))


def test_the_join_still_resolves_to_one_project_per_file(session):
    """INVERSE reversed the arrow, not the guarantee.

    A second GitlabProject node claiming the same ``project_id`` must collapse
    DEFINED_IN to zero edges with an ambiguity error — the identical refusal
    BELONGS_TO gets, because it is the identical index. This is the assertion
    that would fail if someone "fixed" one-to-many by letting a key have several
    targets.

    The rival node is planted directly rather than grown from a duplicate
    ``resources`` row, because cicd's numeric natural key makes the natural
    route impossible: two rows carrying ``project_id`` 113 upsert the SAME node
    (that is the point of ``test_a_project_rename_does_not_split_the_node`` in
    the BELONGS_TO suite). The contradiction still has to be refused if it ever
    arrives by another route.
    """
    from infra_brain import graph_engine, graph_phase2

    _homelab_fixture(session)
    graph_phase2.upsert_node(
        session,
        node_type=PROJECT_NODE,
        natural_key="113-from-somewhere-else",
        name="rival/homelab-ansible",
        source="cicd",
        attributes={"project_id": 113},
    )
    session.flush()

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert counts["edges"].get(DEFINED_IN, 0) == 0
    assert counts["edges"].get(BELONGS_TO, 0) == 0
    assert any(DEFINED_IN in e and "ambiguous" in e for e in errors), errors


# --- equivalence ------------------------------------------------------------


def test_engine_reproduces_the_deleted_deriver_exactly(session):
    """THE gate: same fixture, both writers, identical triples."""
    _homelab_fixture(session, include_unpopulated=True)

    deriver = _deleted_deriver_defined_in(session)
    engine = _engine_defined_in(session)

    assert deriver, "fixture produced no deriver edges — it is not exercising anything"
    assert engine == deriver


def test_equivalence_holds_for_the_live_classification_mix(session):
    """The five classifications this homelab actually has, and only those."""
    proj, made = _homelab_fixture(session)

    deriver = _deleted_deriver_defined_in(session)
    engine = _engine_defined_in(session)

    assert len(engine) == len(_LIVE_FILES)
    assert {t[0] for t in engine} == {proj.id}, "every edge starts at the ONE project"
    assert {t[1] for t in engine} == {r.id for r in made.values()}
    assert engine == deriver


def test_defined_in_is_the_exact_inverse_of_belongs_to_on_live_data(session):
    """Same fixture, both edge types, mirrored triples.

    This is the claim the whole migration rests on stated as data rather than
    as prose: the declarative path emits DEFINED_IN edges that are precisely
    BELONGS_TO's edges with the endpoints swapped.
    """
    _homelab_fixture(session)

    from infra_brain import graph_engine

    counts, errors = graph_engine.emit_all(session, specs=_specs())
    assert errors == [], errors

    def _triples(edge_type):
        out = set()
        for edge in session.execute(
            select(GraphEdge).where(GraphEdge.edge_type == edge_type, GraphEdge.valid_to.is_(None))
        ).scalars():
            src = session.get(GraphNode, edge.source_id)
            dst = session.get(GraphNode, edge.target_id)
            out.add((src.resource_id, dst.resource_id))
        return out

    belongs = _triples(BELONGS_TO)
    defined = _triples(DEFINED_IN)
    assert belongs, "fixture produced no BELONGS_TO edges"
    assert defined == {(b, a) for a, b in belongs}
    assert counts["edges"][DEFINED_IN] == counts["edges"][BELONGS_TO] == len(_LIVE_FILES)


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

    assert _deleted_deriver_defined_in(session) == _engine_defined_in(session)


def test_project_with_no_resource_row_yields_no_edge_either_way(session):
    """A GitLab project cicd has not (yet) inventoried is not a source node."""
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

    deriver = _deleted_deriver_defined_in(session)
    engine = _engine_defined_in(session)
    assert engine == deriver
    assert orphan.id not in {t[1] for t in engine}
    # ...but the file is still an entity in its own right.
    assert session.execute(select(GraphNode).where(GraphNode.resource_id == orphan.id)).scalar_one()


def test_retired_duplicate_project_resource_is_not_a_source(session):
    """Live data really has one: a project renamed between two cicd runs.

    ``resources`` holds both the old name and the new one with the SAME
    ``metadata.project_id``, the old one carrying ``retired_at``. If the engine
    read retired rows, that id would be claimed by two nodes, the join would
    (correctly) refuse to guess, and DEFINED_IN would collapse to zero edges
    instead of picking wrong.
    """
    proj, _made = _homelab_fixture(session)
    _project_resource(session, "homelab-ansible", 113, retired=True)
    session.flush()

    engine = _engine_defined_in(session)

    assert len(engine) == len(_LIVE_FILES)
    assert {t[0] for t in engine} == {proj.id}


def test_engine_is_idempotent(session):
    """A second pass supersedes nothing — the edges are the same edges."""
    _homelab_fixture(session)
    first = _engine_defined_in(session)
    second = _engine_defined_in(session)

    assert first == second
    assert len(
        list(session.execute(select(GraphEdge).where(GraphEdge.edge_type == DEFINED_IN)).scalars())
    ) == len(first), "a re-run must upsert, not accumulate a second generation of edges"
    assert _deleted_deriver_defined_in(session) == second
