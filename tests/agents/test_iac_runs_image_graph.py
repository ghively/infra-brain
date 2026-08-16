"""``iac`` declares ``<compose file> ─RUNS_IMAGE→ <ContainerImage>``.

The first relationship migrated onto ``ChildSpec``, and the reason that type
exists. RUNS_IMAGE was examined and deferred TWICE — once for direction, once
for reach — and the second deferral named three disqualifying reasons. All
three are answered here, and it is worth being explicit about which mechanism
answered which, because two of them are contract changes and one is a decision:

1. "The join key is ``compose_services.image``, a child table, and a NodeSpec's
   whole world is a ``resources`` row plus its ``metadata``." → ``ChildSpec``.
   ``NodeSpec.gathers`` walks ``resources → iac_files → compose_services`` by
   primary key and lands the images on the file node as a list, so the EdgeSpec
   — which matches graph-side and can only see what a node carries — can join
   on them.
2. "One compose file runs MANY images, so the edge fans out." → the file node's
   gathered key is many-valued and ``EdgeSpec.from_key_multi`` says so. Each of
   those values is still resolved through the UNCHANGED key→one-target index.
   ``EdgeDirection.INVERSE`` (which closed DEFINED_IN's fan-out) is the wrong
   tool here and was not used: this fans out from the side that HOLDS the keys,
   not from the side they resolve to.
3. "The TARGET entity is minted right here — no collector owns
   ``container_image``." → iac now declares it. Not a mechanism, a decision:
   the source asserting an image exists is the compose file that names it. See
   ``_CONTAINER_IMAGE_NODE``'s comment in ``agents/iac.py`` for why
   ``container_registry`` is not that owner.

THE EQUIVALENCE RULE (the same one ``test_iac_defined_in_graph.py`` states)
--------------------------------------------------------------------------
The declarative path replaces working code, so "it produces edges" is not the
bar — it must produce THE SAME edges. Every test below runs both writers over
one fixture and compares resolved triples. The deriver side is a VERBATIM COPY
of the block deleted from ``GraphMaintenanceAgent._populate_typed_relationships``
(see ``_deleted_deriver_runs_image``), frozen here so the equivalence claim
keeps running instead of retiring the moment it was first satisfied.

WHAT THE TRIPLE IS, AND WHY IT IS NOT TWO RESOURCE IDS. DEFINED_IN compared
``(from_resource, to_resource)`` because both its ends are inventory items. Only
ONE end of RUNS_IMAGE is: the deriver's target was a ``container_image``
``resources`` row it minted itself, and the declaration's target is a
``resource_backed=False`` value node with no resource at all. The image STRING
is what both stores agree on and what both key their target by, so the triple is
``(compose file resource id, image string)``. Comparing anything else would be
comparing two ids neither writer ever intended to match.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain.agents.cicd import CICDAgent
from infra_brain.agents.iac import IaCAgent
from infra_brain.db.models import (
    ComposeService,
    GitlabProject,
    GraphEdge,
    GraphNode,
    IacFile,
    Resource,
)

from tests.support.pg import make_engine

RUNS_IMAGE = "RUNS_IMAGE"
IMAGE_NODE = "ContainerImage"
COMPOSE_NODE = "DockerComposeFile"

#: This homelab's actual compose shape: one repo, several stacks, a handful of
#: services each, and the SAME image reused across stacks — which is the case
#: that makes the target a shared node rather than a per-file one.
_STACKS = {
    "roles/docker_stack/files/glance/docker-compose.yml": {
        "glance": "glanceapp/glance:latest",
        "redis": "redis:7",
    },
    "roles/docker_stack/files/media/docker-compose.yml": {
        "sonarr": "linuxserver/sonarr:latest",
        "radarr": "linuxserver/radarr:latest",
        "redis": "redis:7",
    },
    "roles/docker_stack/files/monitoring/docker-compose.yml": {
        "prometheus": "prom/prometheus:v2.51.0",
    },
}


@pytest.fixture()
def session():
    engine = make_engine()
    # NO ``PRAGMA foreign_keys=ON`` here, unlike the DEFINED_IN suite: the live
    # deriver this file compares against inserts ``resource_relationships`` rows
    # through a raw-SQL shim while the Resource row it points at is still only
    # flushed, which SQLite rejects under enforced FKs. The graph_maintenance
    # suites run without the pragma for the same reason.
    with Session(engine) as s:
        yield s
        s.rollback()


def _specs():
    return {"iac": IaCAgent.spec, "cicd": CICDAgent.spec}


def _project(session, project_id=113):
    resource = Resource(
        id=uuid.uuid4(),
        domain="cicd",
        type="gitlab_project",
        name="infra-brain/iac/homelab-ansible",
        source="CICDAgent",
        metadata_={"project_id": project_id, "default_branch": "main"},
    )
    session.add(resource)
    session.flush()
    session.add(
        GitlabProject(
            id=uuid.uuid4(),
            gitlab_project_id=project_id,
            name="homelab-ansible",
            path_with_namespace="infra-brain/iac/homelab-ansible",
            resource_id=resource.id,
        )
    )
    session.flush()
    return resource


def _compose_file(
    session, path, services, *, project_id=113, resource=True, rtype="docker_compose"
):
    """A compose file: a ``resources`` row, its ``iac_files`` row, its services.

    ``resource=False`` reproduces the live shape where ``iac_files.resource_id``
    is null — half this homelab's rows are.
    """
    res = None
    if resource:
        res = Resource(
            id=uuid.uuid4(),
            domain="iac",
            type=rtype,
            name=f"homelab-ansible/{path}",
            source="IaCAgent",
            metadata_={
                "project": "homelab-ansible",
                "project_id": project_id,
                "file_path": path,
                "ref": "main",
                # The metadata carries service NAMES and a count — never the
                # images. This is the gap ChildSpec closes, made visible.
                "services": sorted(services),
                "service_count": len(services),
            },
        )
        session.add(res)
        session.flush()
    iac = IacFile(
        id=uuid.uuid4(),
        resource_id=res.id if res is not None else None,
        gitlab_project_id=project_id,
        project_name="homelab-ansible",
        path=path,
        file_type="compose",
        ref="main",
    )
    session.add(iac)
    session.flush()
    for name, image in services.items():
        session.add(
            ComposeService(id=uuid.uuid4(), iac_file_id=iac.id, service_name=name, image=image)
        )
    session.flush()
    return res, iac


def _homelab_fixture(session):
    project = _project(session)
    files = {path: _compose_file(session, path, svcs)[0] for path, svcs in _STACKS.items()}
    return project, files


# --- the two paths ----------------------------------------------------------


def _deleted_deriver_runs_image(session) -> set[tuple[uuid.UUID, str]]:
    """The hand-written derivation this migration removed, copied verbatim.

    This is the equivalence ORACLE, and it is deliberately dead code rather than
    a call into ``graph_maintenance``: the whole point of the migration is that
    ``_populate_typed_relationships`` no longer contains this, so the test
    cannot ask it. Freezing the block here keeps the equivalence claim *running*
    instead of retiring it the moment it was satisfied.

    The body below is the ``── Container image`` block of
    ``GraphMaintenanceAgent._populate_typed_relationships`` as it stood at
    commit a9e727d, with exactly two reductions:

      * ``nid = self._get_or_create_node(session, node_cache, "container",
        "container_image", img, {"image": img})`` becomes ``nid = img``. That
        call is the very thing the declaration replaces, and it is 1:1 with the
        image string — it get-or-creates one node per distinct ``img`` — so
        substituting the string preserves the ``seen_image`` dedup exactly,
        which is the only behaviour of ``nid`` this block depends on.
      * the ``image_edges.append({...})`` payload is reduced to the triple under
        comparison.

    Do NOT "improve" it; its value is that it is unchanged — including the
    ``seen_image`` dedup, which is why two services in ONE file running the SAME
    image yield ONE edge and not two.
    """
    triples: set[tuple[uuid.UUID, str]] = set()

    seen_image: set[tuple[str, str]] = set()
    for row in (
        session.query(IacFile.resource_id, ComposeService.image)
        .join(ComposeService, ComposeService.iac_file_id == IacFile.id)
        .filter(IacFile.resource_id.isnot(None))
        .all()
    ):
        img = (row.image or "").strip()
        if not img or not row.resource_id:
            continue
        nid = img
        k = (str(row.resource_id), str(nid))
        if k in seen_image:
            continue
        seen_image.add(k)
        triples.add((row.resource_id, img))
    return triples


def _engine_runs_image(session) -> set[tuple[uuid.UUID, str]]:
    """What the declarative engine writes, resolved back to the compared triple.

    The source end resolves through ``graph_nodes.resource_id`` (the two stores
    key edges differently, so comparing raw ids would compare nothing); the
    target end is the image node's ``natural_key``, which IS the image string.
    """
    from infra_brain import graph_engine

    counts, errors = graph_engine.emit_all(session, specs=_specs())
    assert errors == [], errors
    triples = set()
    for edge in session.execute(
        select(GraphEdge).where(GraphEdge.edge_type == RUNS_IMAGE, GraphEdge.valid_to.is_(None))
    ).scalars():
        src = session.get(GraphNode, edge.source_id)
        dst = session.get(GraphNode, edge.target_id)
        assert src.resource_id is not None, (
            "the compose file end is an inventory item — a node without a "
            "resource_id means resource_backed was declared wrong"
        )
        assert dst.resource_id is None, (
            "the image end is a SHARED value node — claiming one owning "
            "resource would pick whichever compose file was materialised last"
        )
        triples.add((src.resource_id, dst.natural_key))
    assert counts["edges"].get(RUNS_IMAGE, 0) == len(triples)
    return triples


# --- the declaration --------------------------------------------------------


def test_iac_declares_the_container_image_node_it_points_at():
    """Both ends are iac's, which is what makes the edge declarable at all.

    A collector may only emit edges out of entities it owns. The deriver minted
    ``container_image`` rows inline, so deleting it would have left the target
    of a declared edge produced by nothing — the failure mode
    ``test_migrated_types_are_actually_declared_somewhere`` exists to catch.
    """
    spec = IaCAgent.spec
    image_node = next(n for n in spec.emits_nodes if n.type == IMAGE_NODE)

    assert image_node.from_rows is not None, "identity comes from compose_services rows"
    assert image_node.from_rows.column == "image"
    assert image_node.natural_key == f"rows.{image_node.from_rows.key}"
    assert image_node.resource_backed is False
    # The path is a strict parent->child descent by primary key, which is the
    # only shape ChildSpec allows — see its docstring for what it refuses.
    assert image_node.from_rows.path == (
        ("iac_files", "resource_id"),
        ("compose_services", "iac_file_id"),
    )

    edge = next(e for e in spec.emits_edges if e.type == RUNS_IMAGE)
    assert (edge.from_node, edge.to_node) == (COMPOSE_NODE, IMAGE_NODE)
    assert edge.written_as() == (COMPOSE_NODE, IMAGE_NODE), "stored file -> image"
    assert edge.from_key_multi is True
    # FK-strength: both sides are the same compose_services.image string.
    assert edge.method == "declared"
    assert float(edge.confidence) == 1.0


def test_the_join_key_is_gathered_onto_the_compose_file_node_and_nowhere_else():
    """The compose classification gathers IMAGES, and no other classification does.

    Recorded so a future ``gathers=`` copy-pasted onto every file type (which
    would run pointless joins per pass) fails a test rather than being noticed
    in review. ``AnsibleInventoryFile`` also gathers now — its own child rows,
    for ANSIBLE_MANAGES (see ``test_iac_ansible_manages_graph.py``) — so the
    claim is scoped to the ``images`` key rather than to "exactly one gatherer".
    """
    spec = IaCAgent.spec
    gathering = {n.type: n.gathers for n in spec.emits_nodes if n.gathers}

    assert set(gathering) == {COMPOSE_NODE, "AnsibleInventoryFile"}
    assert gathering[COMPOSE_NODE][0].key == "images"
    assert [c.key for n, cs in gathering.items() if n != COMPOSE_NODE for c in cs] == [
        "managed_hosts"
    ], "no other classification may gather compose images"

    edge = next(e for e in spec.emits_edges if e.type == RUNS_IMAGE)
    assert edge.from_key == f"attributes.{gathering[COMPOSE_NODE][0].key}", (
        "the edge must join on the attribute the node actually gathered — the "
        "engine matches graph-side and cannot see the child table"
    )


def test_the_images_are_not_in_the_resource_metadata(session):
    """The premise of the whole migration, asserted rather than asserted-in-prose.

    ``_extract_yaml_metadata`` stores ``services`` and ``service_count`` for a
    compose file. If a future change started storing images there too, this
    declaration could have been a plain metadata reference and the ChildSpec
    would be unnecessary complexity — so make that change fail here.
    """
    from infra_brain.agents.iac import _extract_yaml_metadata

    content = "services:\n  glance:\n    image: glanceapp/glance:latest\n"
    metadata = _extract_yaml_metadata(
        "docker_compose", "docker-compose.yml", "docker-compose.yml", content, "p", 1, "main"
    )

    assert metadata["services"] == ["glance"]
    assert not any("glanceapp" in str(v) for v in metadata.values()), (
        "the compose file's metadata carries service NAMES, not images — that "
        "is why RUNS_IMAGE needs child-table reach"
    )


def test_one_image_in_two_files_is_one_shared_node(session):
    """The reason the target is a value node: "who runs redis:7" is one hop."""
    from infra_brain import graph_engine

    _homelab_fixture(session)
    graph_engine.emit_all(session, specs=_specs())

    images = list(
        session.execute(select(GraphNode).where(GraphNode.node_type == IMAGE_NODE)).scalars()
    )
    assert {n.natural_key for n in images} == {
        image for services in _STACKS.values() for image in services.values()
    }
    redis = next(n for n in images if n.natural_key == "redis:7")
    into_redis = list(
        session.execute(
            select(GraphEdge).where(
                GraphEdge.edge_type == RUNS_IMAGE, GraphEdge.target_id == redis.id
            )
        ).scalars()
    )
    assert len(into_redis) == 2, "two stacks run redis:7 and both edges land on the one node"


# --- equivalence ------------------------------------------------------------


def test_engine_reproduces_the_deleted_deriver_exactly(session):
    """THE gate: same fixture, both writers, identical triples."""
    _homelab_fixture(session)

    deriver = _deleted_deriver_runs_image(session)
    engine = _engine_runs_image(session)

    assert deriver, "fixture produced no deriver edges — it is not exercising anything"
    assert engine == deriver


def test_equivalence_on_the_live_stack_mix(session):
    """The three stacks this homelab actually has, counted rather than trusted."""
    _project, files = _homelab_fixture(session)

    engine = _engine_runs_image(session)

    expected = {
        (files[path].id, image) for path, svcs in _STACKS.items() for image in svcs.values()
    }
    assert engine == expected
    assert engine == _deleted_deriver_runs_image(session)


def test_two_services_running_the_same_image_are_one_edge(session):
    """The deriver's ``seen_image`` dedup, reproduced by a DISTINCT gathered list.

    Same answer by two different routes — the deriver deduped after the fact on
    ``(anchor, node)``, the engine never produces the duplicate because the
    gathered attribute is distinct. That is the strongest form of equivalence.
    """
    _project(session)
    res, _iac = _compose_file(
        session,
        "roles/docker_stack/files/dupe/docker-compose.yml",
        {"web": "nginx:1.25", "web-canary": "nginx:1.25"},
    )

    engine = _engine_runs_image(session)

    assert engine == {(res.id, "nginx:1.25")}
    assert engine == _deleted_deriver_runs_image(session)


def test_a_service_with_no_image_yields_no_edge_either_way(session):
    """``compose_services.image`` is nullable — a build-only service has none."""
    _project(session)
    res, iac = _compose_file(
        session, "roles/docker_stack/files/built/docker-compose.yml", {"app": "myapp:1"}
    )
    session.add(ComposeService(id=uuid.uuid4(), iac_file_id=iac.id, service_name="builder"))
    session.add(
        ComposeService(id=uuid.uuid4(), iac_file_id=iac.id, service_name="blank", image="   ")
    )
    session.flush()

    engine = _engine_runs_image(session)

    assert engine == {(res.id, "myapp:1")}
    assert engine == _deleted_deriver_runs_image(session)


def test_a_compose_file_with_no_resource_row_yields_no_edge_either_way(session):
    """``iac_files.resource_id`` is nullable; half this homelab's rows are null.

    The deriver filtered those out explicitly. The engine never sees them at
    all — it reads ``resources``, so an unlinked file is not an entity, and its
    services are not reachable by any child path. Same answer, two routes.
    """
    _project(session)
    res, _ = _compose_file(
        session, "roles/docker_stack/files/glance/docker-compose.yml", {"a": "x:1"}
    )
    _compose_file(
        session,
        "roles/docker_stack/files/unlinked/docker-compose.yml",
        {"b": "y:2"},
        resource=False,
    )

    engine = _engine_runs_image(session)

    assert engine == {(res.id, "x:1")}
    assert engine == _deleted_deriver_runs_image(session)
    assert "y:2" not in {
        n.natural_key
        for n in session.execute(
            select(GraphNode).where(GraphNode.node_type == IMAGE_NODE)
        ).scalars()
    }


def test_a_retired_compose_file_contributes_neither_node_nor_edge(session):
    """Retirement has to reach the child rows too, or a deleted stack keeps voting.

    A DIVERGENCE FROM THE DERIVER, and a deliberate one: the deriver read
    ``iac_files`` directly and never looked at ``resources.retired_at``, so a
    retired compose file kept emitting RUNS_IMAGE forever. Every declarative
    node population excludes retired rows (``_emit_nodes``), and the child
    query reuses that same filter rather than a parallel one. Recorded here as
    the one place the two writers are ALLOWED to differ, so it stays a decision.
    """
    from infra_brain import graph_engine

    _project(session)
    res, _ = _compose_file(
        session, "roles/docker_stack/files/gone/docker-compose.yml", {"a": "x:1"}
    )
    res.retired_at = datetime.now(UTC)
    session.flush()

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    assert counts["edges"].get(RUNS_IMAGE, 0) == 0
    assert counts["nodes"][IMAGE_NODE] == 0
    assert _deleted_deriver_runs_image(session) == {(res.id, "x:1")}, (
        "the deriver DID keep emitting for a retired file — this is the divergence"
    )


def test_an_empty_source_produces_nothing_either_way(session):
    """No compose files at all: no nodes, no edges, no error.

    Trivial, and kept because the deleted ``TestContainerImage`` asserted it —
    every claim that suite made is re-made against the declarative path rather
    than dropped along with it.
    """
    from infra_brain import graph_engine

    _project(session)

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    assert counts["edges"].get(RUNS_IMAGE, 0) == 0
    assert counts["nodes"][IMAGE_NODE] == 0
    assert _deleted_deriver_runs_image(session) == set()


def test_engine_is_idempotent(session):
    """A second pass supersedes nothing — the edges are the same edges."""
    _homelab_fixture(session)
    first = _engine_runs_image(session)
    second = _engine_runs_image(session)

    assert first == second
    assert len(
        list(session.execute(select(GraphEdge).where(GraphEdge.edge_type == RUNS_IMAGE)).scalars())
    ) == len(first), "a re-run must upsert, not accumulate a second generation of edges"
    assert _deleted_deriver_runs_image(session) == second


# --- the frozen oracle is faithful ------------------------------------------
#
# ``test_the_frozen_oracle_matches_the_LIVE_deriver`` lived here and is now
# DELETED, with the deriver it drove. Its job was to run ONCE, in commit 97a42fd
# — the commit before the deletion, while ``_populate_typed_relationships``
# still contained the block — and prove by execution that
# ``_deleted_deriver_runs_image`` above is what the shipped code actually did,
# rather than what a careful reader believed it did. It drove the real method
# over ``_homelab_fixture`` and asserted
# ``live == _deleted_deriver_runs_image(session) == _engine_runs_image(session)``.
#
# It cannot outlive the code it drove, and re-adding it would mean re-adding the
# deriver. Everything above keeps running instead: the frozen copy IS the oracle
# now, and every equivalence test in this file compares against it on every run.


# --- the boundary that remains ----------------------------------------------


def test_the_old_ansible_manages_anchor_is_still_undeclarable_which_is_why_it_moved():
    """The ownership refusal that forced TRK-354's decision, kept running.

    ANSIBLE_MANAGES is declared now (``test_iac_ansible_manages_graph.py``), but
    NOT in the shape the deriver stored it — and this test is the reason why,
    preserved rather than deleted, because a future reader will otherwise ask
    "couldn't it just have been declared as-was?".

    It could not. As the deriver stored it, the edge runs ``gitlab_project
    resource → linux/windows host resource``: cicd's entity to linux's.
    ``AgentSpec`` requires ``from_node`` — the JOIN's start — to be a node the
    declaring spec emits, and both attempts below are refused for exactly that,
    one per direction. ``EdgeDirection`` cannot help: it moves the arrow, and
    here BOTH ends are foreign, so there is no orientation in which the join
    starts at something iac owns.

    NOR COULD THE DECLARATION MOVE TO EITHER OWNER. cicd's ``GitlabProject``
    reaches ``iac_files`` only through
    ``gitlab_projects.gitlab_project_id = iac_files.gitlab_project_id`` — a
    non-PK business-key join, which ``ChildSpec`` deliberately cannot express
    (its hops always join to the previous table's PRIMARY key). linux has no
    path into iac's tables at all; its link to a play is a hostname string
    match, not a descent.

    So the ANCHOR moved to the ``AnsibleInventoryFile`` iac does own — Option A
    of TRK-354, a product decision taken by the integrator under the
    maintainer's direction and vetoable by them. Because that changes which
    edges exist, the equivalence oracle was replaced by a MAPPING test; see the
    new suite's module docstring.
    """
    import re
    from datetime import timedelta

    from infra_brain.etl.spec import AgentSpec, EdgeDirection, EdgeSpec, Tier

    def _iac_spec_with(edge):
        return AgentSpec(
            domain="iac",
            tier=Tier.COLLECTOR,
            schedule=None,
            max_staleness=timedelta(hours=8),
            emits_nodes=IaCAgent.spec.emits_nodes,
            emits_edges=(edge,),
        )

    owned = {n.type for n in IaCAgent.spec.emits_nodes}
    assert "GitlabProject" not in owned and "LinuxHost" not in owned

    # Forward, as the deriver stores it: the join would start at cicd's project.
    with pytest.raises(ValueError, match=re.escape("emits_nodes")) as forward:
        _iac_spec_with(
            EdgeSpec(
                type="ANSIBLE_MANAGES",
                from_node="GitlabProject",
                to_node="LinuxHost",
                from_key="attributes.hosts",
                to_key="name",
                from_key_multi=True,
                key_normalizer="host",
                confidence="0.900",
            )
        )
    assert "GitlabProject" in str(forward.value)

    # Inverse, so the stored arrow still runs project → host: the join would
    # start at linux's host instead. Foreign either way.
    with pytest.raises(ValueError, match=re.escape("emits_nodes")) as inverse:
        _iac_spec_with(
            EdgeSpec(
                type="ANSIBLE_MANAGES",
                from_node="LinuxHost",
                to_node="GitlabProject",
                from_key="name",
                to_key="attributes.project_id",
                direction=EdgeDirection.INVERSE,
                confidence="0.900",
            )
        )
    assert "LinuxHost" in str(inverse.value)

    # And the edge IS declared — out of the re-anchored, iac-owned node.
    declared = [e for e in IaCAgent.spec.emits_edges if e.type == "ANSIBLE_MANAGES"]
    assert len(declared) == 1
    assert declared[0].from_node == "AnsibleInventoryFile"
    assert declared[0].from_node in owned
