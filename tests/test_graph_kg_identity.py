"""Identity-aware neighbourhood: a SAME_AS pair must answer as ONE machine.

WHY THIS FILE EXISTS
--------------------
Live production shape, measured directly against the running store:

    LinuxHost node_a           -> 33 active edges
    AnsibleManagedHost node_a  ->  2 active edges

Two ``graph_nodes`` rows share the name ``node_a``, joined by an active
symmetric ``SAME_AS`` pair (``deterministic_match``, 0.990) — the resolver
asserting *they are the same physical machine*. The dashboard chat was asked
"what is connected to node_a?" and answered:

    "the knowledge graph shows node_a has only one connection — an identity
     link between two representations of itself."

That answer came from ``resolve_roots`` returning both rows, the tool taking
``roots[0]`` (the Ansible node, 2 edges), walking depth=1, and reporting what
it found — while the LinuxHost row next door carried every real fact about the
machine. ``graph_phase3.blast_radius`` never had this bug: it traverses
``SAME_AS`` natively and returns 25 neighbours for the same host. The flagship
"ask about your infrastructure" surface was the one place that ignored the
product's own identity model.

WHAT IS PINNED HERE
-------------------
1. Asking by EITHER name/id returns the UNION of the pair's edges.
2. The identity hop is FREE — it does not consume a hop of the caller's
   requested ``depth`` (see ``graph_kg.identity_cluster``'s docstring for the
   reasoning; "the same machine" is not a step away from itself).
3. ``NOT_SAME_AS`` — the human veto — is never merged across and never
   traversed as connectivity. This is the load-bearing safety rule of the
   authority model.
4. A name collision with NO identity link is still reported as a genuine
   ambiguity rather than silently merged.
5. A neighbour reachable from both members appears ONCE.
6. Provenance survives the merge: each edge says which member it was reached
   through, so an operator can tell an Ansible-sourced fact from a
   Linux-sourced one.

The fixture is the real live shape (LinuxHost + AnsibleManagedHost +
HomelabService RUNS_ON + an inventory group), not a synthetic two-node toy, so
these cannot pass vacuously.
"""

from __future__ import annotations

import uuid as _uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    GraphEdgeAuthority,
    GraphEdgeMethod,
    GraphEdgeType,
    LinuxHost,
    LinuxService,
    Resource,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    return make_engine()


def _now():
    return datetime.now(timezone.utc)


def _same_as(session, a, b, *, confidence="0.990"):
    """Symmetric SAME_AS, written both ways exactly as the resolver writes it."""
    from infra_brain import graph_phase2

    for src, tgt in ((a, b), (b, a)):
        graph_phase2.upsert_edge(
            session,
            source_id=src.id,
            target_id=tgt.id,
            edge_type=GraphEdgeType.SAME_AS,
            method=GraphEdgeMethod.DETERMINISTIC_MATCH,
            confidence=Decimal(confidence),
            source="graph_phase3.same_as",
            evidence={"basis": "exact hostname match"},
            authority=GraphEdgeAuthority.AUTO,
        )


def _not_same_as(session, a, b):
    """Symmetric NOT_SAME_AS — human authority only (upsert_edge rule W5)."""
    from infra_brain import graph_phase2

    for src, tgt in ((a, b), (b, a)):
        graph_phase2.upsert_edge(
            session,
            source_id=src.id,
            target_id=tgt.id,
            edge_type=GraphEdgeType.NOT_SAME_AS,
            method=GraphEdgeMethod.DECLARED,
            confidence=Decimal("1.000"),
            source="graph_phase3.reject_same_as",
            evidence={"approver": "operator@example.com"},
            authority=GraphEdgeAuthority.HUMAN,
        )


@pytest.fixture
def live_shape(engine):
    """The node_a shape, as it exists in production.

    graph_nodes
        LinuxHost 'node_a'            (source=linux)     <- carries the facts
        AnsibleManagedHost 'node_a'   (source=ansible)   <- carries almost none
        HomelabService 'litellm'      (source=homelab_services)
        EolProduct 'debian-11'        (source=eol)
        InventoryGroup 'arm_hosts'    (source=ansible)

    graph_edges (all active)
        litellm      --RUNS_ON--> LinuxHost node_a
        LinuxHost    --RUNS_EOL--> debian-11
        AnsibleHost  --MEMBER_OF--> arm_hosts
        LinuxHost   <--SAME_AS-->  AnsibleHost   (deterministic_match, 0.990)
    """
    from infra_brain import graph_phase2

    with Session(engine) as s:
        host_res = Resource(
            domain="linux",
            type="linux_host",
            name="node_a",
            source="linux",
            zone="lab",
            last_seen=_now(),
        )
        s.add(host_res)
        s.flush()
        lh = LinuxHost(resource_id=host_res.id, distro="debian", kernel="6.1", arch="aarch64")
        s.add(lh)
        s.flush()
        s.add(LinuxService(host_id=lh.id, name="docker", state="running", enabled=True))

        linux_node = graph_phase2.upsert_node(
            s,
            node_type="LinuxHost",
            natural_key="node_a",
            name="node_a",
            source="linux",
            resource_id=host_res.id,
        )
        ansible_node = graph_phase2.upsert_node(
            s,
            node_type="AnsibleManagedHost",
            natural_key="node_a",
            name="node_a",
            source="ansible",
        )
        svc_node = graph_phase2.upsert_node(
            s,
            node_type="HomelabService",
            natural_key="node_a/litellm",
            name="litellm",
            source="homelab_services",
        )
        eol_node = graph_phase2.upsert_node(
            s,
            node_type="EolProduct",
            natural_key="debian-11",
            name="debian-11",
            source="eol",
        )
        group_node = graph_phase2.upsert_node(
            s,
            node_type="InventoryGroup",
            natural_key="arm_hosts",
            name="arm_hosts",
            source="ansible",
        )

        graph_phase2.upsert_edge(
            s,
            source_id=svc_node.id,
            target_id=linux_node.id,
            edge_type="RUNS_ON",
            method=GraphEdgeMethod.DETERMINISTIC_MATCH,
            confidence=Decimal("0.990"),
            source="homelab_services",
            evidence={"basis": "host attr"},
        )
        graph_phase2.upsert_edge(
            s,
            source_id=linux_node.id,
            target_id=eol_node.id,
            edge_type="RUNS_EOL",
            method=GraphEdgeMethod.DETERMINISTIC_MATCH,
            confidence=Decimal("0.950"),
            source="eol",
            evidence={"basis": "distro match"},
        )
        graph_phase2.upsert_edge(
            s,
            source_id=ansible_node.id,
            target_id=group_node.id,
            edge_type="MEMBER_OF",
            method=GraphEdgeMethod.DECLARED,
            confidence=Decimal("1.000"),
            source="ansible",
            evidence={"basis": "inventory"},
        )
        _same_as(s, linux_node, ansible_node)

        s.commit()
        return {
            "linux_node_id": str(linux_node.id),
            "ansible_node_id": str(ansible_node.id),
            "host_resource_id": str(host_res.id),
        }


@contextmanager
def _chat_db(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with (
        patch("infra_brain.chat.tools.get_session", _get_session),
        patch("infra_brain.chat.tools.get_engine", lambda: engine),
    ):
        yield


def _edge_types(payload):
    return {e["edge_type"] for e in payload["edges"]}


def _node_names(payload):
    return {n["name"] for n in payload["nodes"]}


# ---------------------------------------------------------------------------
# (1) The bug itself: either name must answer for the whole machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ident_key", ["linux_node_id", "ansible_node_id"])
def test_either_member_returns_the_union_of_the_pair(engine, live_shape, ident_key):
    """The exact live failure. Asking from the 2-edge Ansible side used to
    report "one connection"; it must now report the whole machine."""
    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        payload = kg_neighborhood_payload(live_shape[ident_key], depth=1)

    assert payload["found"] is True, payload
    assert _edge_types(payload) >= {"RUNS_ON", "RUNS_EOL", "MEMBER_OF"}, payload
    assert {"litellm", "debian-11", "arm_hosts"} <= _node_names(payload), payload


def test_by_bare_name_returns_the_union_not_one_arbitrary_side(engine, live_shape):
    """`resolve_roots("node_a")` matches both rows. They are one machine, so
    this is not an ambiguity to punt on — it is one entity to answer for."""
    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        payload = kg_neighborhood_payload("node_a", depth=1)

    assert _edge_types(payload) >= {"RUNS_ON", "RUNS_EOL", "MEMBER_OF"}, payload
    assert payload["identity_merged"] is True, payload
    assert {m["type"] for m in payload["identity_members"]} == {
        "LinuxHost",
        "AnsibleManagedHost",
    }, payload
    # An identity link is not an ambiguity — nothing to disambiguate.
    assert not payload.get("other_matches"), payload


def test_text_tool_no_longer_says_walked_the_first(engine, live_shape):
    from infra_brain.chat.tools import query_resource_neighborhood

    with _chat_db(engine):
        out = query_resource_neighborhood.invoke({"resource_name": "node_a", "depth": 1})

    assert "Walked the first" not in out, out
    assert "litellm" in out and "arm_hosts" in out, out


# ---------------------------------------------------------------------------
# (2) Depth accounting — the identity hop is FREE
# ---------------------------------------------------------------------------


def test_identity_hop_does_not_consume_a_hop_of_requested_depth(engine, live_shape):
    """depth=1 from the Ansible side must reach the Linux side's OWN hop-1
    neighbours. Charging a hop for "the same machine" is precisely why depth=1
    returned nothing useful in production."""
    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        payload = kg_neighborhood_payload(live_shape["ansible_node_id"], depth=1)

    assert "litellm" in _node_names(payload), payload
    assert payload["depth"] == 1, payload


def test_free_identity_hop_does_not_leak_a_second_real_hop(engine, live_shape):
    """Free does not mean unbounded: a node two REAL hops out is still out of
    range at depth=1."""
    from infra_brain import graph_phase2, graph_kg

    with Session(engine) as s:
        far = graph_phase2.upsert_node(
            s,
            node_type="ContainerImage",
            natural_key="ghcr.io/litellm:latest",
            name="litellm-image",
            source="homelab_services",
        )
        svc = graph_kg.resolve_roots(s, "litellm")[0]
        graph_phase2.upsert_edge(
            s,
            source_id=svc.id,
            target_id=far.id,
            edge_type="RUNS_IMAGE",
            method=GraphEdgeMethod.DECLARED,
            confidence=Decimal("1.000"),
            source="homelab_services",
        )
        s.commit()

    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        shallow = kg_neighborhood_payload(live_shape["ansible_node_id"], depth=1)
        deep = kg_neighborhood_payload(live_shape["ansible_node_id"], depth=2)

    assert "litellm-image" not in _node_names(shallow), shallow
    assert "litellm-image" in _node_names(deep), deep


# ---------------------------------------------------------------------------
# (3) NOT_SAME_AS — the human veto. Never merged, never traversed.
# ---------------------------------------------------------------------------


@pytest.fixture
def vetoed_pair(engine):
    """Two same-named nodes a human has explicitly declared DIFFERENT."""
    from infra_brain import graph_phase2

    with Session(engine) as s:
        a = graph_phase2.upsert_node(
            s,
            node_type="LinuxHost",
            natural_key="twin",
            name="twin",
            source="linux",
        )
        b = graph_phase2.upsert_node(
            s,
            node_type="AnsibleManagedHost",
            natural_key="twin",
            name="twin",
            source="ansible",
        )
        secret = graph_phase2.upsert_node(
            s,
            node_type="HomelabService",
            natural_key="twin/only-on-b",
            name="only-on-b",
            source="homelab_services",
        )
        graph_phase2.upsert_edge(
            s,
            source_id=secret.id,
            target_id=b.id,
            edge_type="RUNS_ON",
            method=GraphEdgeMethod.DECLARED,
            confidence=Decimal("1.000"),
            source="homelab_services",
        )
        _not_same_as(s, a, b)
        s.commit()
        return {"a": str(a.id), "b": str(b.id)}


def test_not_same_as_is_never_merged_across(engine, vetoed_pair):
    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        payload = kg_neighborhood_payload(vetoed_pair["a"], depth=1)

    assert payload["identity_merged"] is False, payload
    assert [m["node_id"] for m in payload["identity_members"]] == [vetoed_pair["a"]], payload
    assert "only-on-b" not in _node_names(payload), payload


def test_not_same_as_is_never_traversed_as_connectivity(engine, vetoed_pair):
    """A negative assertion is not a relationship. Walking it would make
    "these are definitely different machines" propagate connectivity between
    them — the exact opposite of what the edge means (the same rule
    ``graph_phase3._graph_edge_walk`` has always enforced)."""
    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        payload = kg_neighborhood_payload(vetoed_pair["a"], depth=2)

    assert "NOT_SAME_AS" not in _edge_types(payload), payload
    assert "twin" not in {
        n["name"] for n in payload["nodes"] if n["node_id"] != vetoed_pair["a"]
    }, payload


def test_veto_blocks_a_transitive_same_as_chain(engine):
    """A--SAME_AS--B--SAME_AS--C with a human A/C veto: the closure is
    transitive, but it must stop at the veto rather than launder a human "no"
    through two machine "yes"es."""
    from infra_brain import graph_kg, graph_phase2

    with Session(engine) as s:
        a = graph_phase2.upsert_node(
            s, node_type="LinuxHost", natural_key="chain", name="chain", source="linux"
        )
        b = graph_phase2.upsert_node(
            s, node_type="AnsibleManagedHost", natural_key="chain", name="chain", source="ansible"
        )
        c = graph_phase2.upsert_node(
            s, node_type="OctopusMachine", natural_key="chain", name="chain", source="octopus"
        )
        _same_as(s, a, b)
        _same_as(s, b, c)
        _not_same_as(s, a, c)
        ids = {"a": a.id, "b": b.id, "c": c.id}
        s.commit()

    with Session(engine) as s:
        members, vetoed = graph_kg.identity_cluster(s, [ids["a"]])

    assert str(ids["b"]) in {str(m) for m in members}, members
    assert str(ids["c"]) not in {str(m) for m in members}, members
    assert str(ids["c"]) in {str(v) for v in vetoed}, vetoed


# ---------------------------------------------------------------------------
# (4) A genuine, non-identity name collision is still an ambiguity
# ---------------------------------------------------------------------------


def test_unrelated_name_collision_still_reports_ambiguity(engine):
    from infra_brain import graph_phase2

    with Session(engine) as s:
        graph_phase2.upsert_node(
            s, node_type="LinuxHost", natural_key="web-1.lab", name="web", source="linux"
        )
        graph_phase2.upsert_node(
            s,
            node_type="HomelabService",
            natural_key="media_host/web",
            name="web",
            source="homelab_services",
        )
        s.commit()

    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        payload = kg_neighborhood_payload("web", depth=1)

    assert payload["identity_merged"] is False, payload
    assert payload.get("other_matches"), payload
    assert len(payload["other_matches"]) == 1, payload


def test_ambiguity_note_survives_in_the_text_tool(engine):
    from infra_brain import graph_phase2

    with Session(engine) as s:
        graph_phase2.upsert_node(
            s, node_type="LinuxHost", natural_key="web-1.lab", name="web", source="linux"
        )
        graph_phase2.upsert_node(
            s,
            node_type="HomelabService",
            natural_key="media_host/web",
            name="web",
            source="homelab_services",
        )
        s.commit()

    from infra_brain.chat.tools import query_resource_neighborhood

    with _chat_db(engine):
        out = query_resource_neighborhood.invoke({"resource_name": "web", "depth": 1})

    assert "AMBIGUOUS" in out.upper(), out


# ---------------------------------------------------------------------------
# (5) Dedup, and (6) provenance
# ---------------------------------------------------------------------------


def test_a_neighbour_shared_by_both_members_appears_once(engine, live_shape):
    from infra_brain import graph_phase2

    with Session(engine) as s:
        shared = graph_phase2.upsert_node(
            s, node_type="Cve", natural_key="CVE-2026-0001", name="CVE-2026-0001", source="vuln"
        )
        for node_id in (live_shape["linux_node_id"], live_shape["ansible_node_id"]):
            graph_phase2.upsert_edge(
                s,
                source_id=_uuid.UUID(node_id),
                target_id=shared.id,
                edge_type=GraphEdgeType.AFFECTED_BY_CVE,
                method=GraphEdgeMethod.DECLARED,
                confidence=Decimal("1.000"),
                source="vuln",
            )
        s.commit()

    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        payload = kg_neighborhood_payload("node_a", depth=1)

    cve_nodes = [n for n in payload["nodes"] if n["name"] == "CVE-2026-0001"]
    assert len(cve_nodes) == 1, payload["nodes"]
    # Both edges are distinct rows and both are real; neither is invented twice.
    cve_edges = [e for e in payload["edges"] if e["to"] == "CVE-2026-0001"]
    assert len(cve_edges) == 2, cve_edges


def test_each_edge_says_which_member_it_was_reached_through(engine, live_shape):
    """Merging must not cost provenance: an operator has to be able to tell an
    Ansible-sourced fact from a Linux-sourced one."""
    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        payload = kg_neighborhood_payload("node_a", depth=1)

    via_by_type = {e["edge_type"]: e["via"] for e in payload["edges"]}
    assert via_by_type["RUNS_ON"] == "node_a (LinuxHost)", via_by_type
    assert via_by_type["MEMBER_OF"] == "node_a (AnsibleManagedHost)", via_by_type


def test_the_same_as_edge_is_kept_but_no_longer_reads_as_a_self_link(engine, live_shape):
    """The evidence for the merge stays visible — but "node_a -> node_a" is
    what the model read as "an identity link between two representations of
    itself", so member endpoints are qualified by node type."""
    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        payload = kg_neighborhood_payload("node_a", depth=1)

    same_as = [e for e in payload["edges"] if e["edge_type"] == "SAME_AS"]
    assert same_as, payload["edges"]
    for edge in same_as:
        assert edge["from"] != edge["to"], edge
        assert {edge["from"], edge["to"]} == {
            "node_a (LinuxHost)",
            "node_a (AnsibleManagedHost)",
        }, edge


def test_facts_are_unioned_across_the_identity_cluster(engine, live_shape):
    """The Ansible node is not resource-backed; the Linux one is. Asking from
    the Ansible side must still surface the machine's systemd units."""
    from infra_brain.chat.tools import kg_neighborhood_payload

    with _chat_db(engine):
        payload = kg_neighborhood_payload(live_shape["ansible_node_id"], depth=1)

    assert any(f["service"] == "docker" for f in payload["facts"]["linux_services"]), payload
