"""Tests for /api/graph/* endpoints.

Uses SQLite in-memory + FastAPI TestClient (same pattern as test_dashboard_api.py).
Seeds the DB via ORM inserts (NOT emit_edge which uses PostgreSQL-only CAST).

Graph-first P5: every route in this module now reads graph_nodes/graph_edges.
The ``get_neighborhood`` stub these tests used to install is gone with the
function it stubbed — which is itself the point. That stub existed because the
legacy walk was a PostgreSQL-only recursive CTE that SQLite cannot run, so the
real traversal was never once exercised by this suite. The replacement walk is
plain Core/ORM and runs for real here.

``_seed_legacy_edge`` (the decoy that proved a legacy row could NOT influence
a response) was deleted at the P5 integration: the table it wrote to no longer
exists — ``create_all`` does not build it and the ORM model is an
instantiation-refusing shim — so the decoy cannot even be seeded. The claim it
protected is now structural: a route cannot read a table that is not there.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import uuid as _uuid_mod
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from infra_brain.db.models import Base, UIUser


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def client(engine, monkeypatch):
    """Auth-off client (explicit dev-mode) for /api/graph/*.

    Item 1.5b (F-027/F-031) made dev-mode explicit: it used to be implied by
    "no UI_COOKIE_SECRET configured", now it requires INFRA_BRAIN_DEV=1.
    """
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.graph_api import graph_router

    app = FastAPI()
    app.include_router(graph_router)

    with patch("infra_brain.graph_api.get_session", _get_session):
        yield TestClient(app)


# ---------------------------------------------------------------------------
# /api/graph/stats — graph-first P5: the PATH survived, the STORE behind it did
# not. The three sibling legacy routes (/relationships, /search,
# /{resource_id}) were removed outright; see test_legacy_routes_are_gone below,
# which is the executable half of that decision.
# ---------------------------------------------------------------------------


def test_stats_empty_db_returns_empty_list(client):
    resp = client.get("/api/graph/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    # The `engine` fixture is module-scoped, so "empty" here means "no crash and
    # a well-formed envelope", not "zero rows" — asserting emptiness would make
    # this test depend on execution order.
    assert isinstance(body["items"], list)


def test_stats_counts_graph_edges_not_resource_relationships(client, engine):
    """The route answers the same QUESTION from the new store.

    Pre-drop this seeded a decoy row in EACH store and asserted only the
    graph_edges one surfaced. The decoy went with the table (see the module
    docstring); what remains is the live half of the claim — /stats serves
    graph_edges — plus the structural fact that the legacy table cannot even
    be created here any more.
    """
    with Session(engine) as s:
        a = _bp_node(s, "LinuxHost", "kgstats-a", "kgstats-a")
        b = _bp_node(s, "HomelabService", "kgstats-b", "kgstats-b")
        _bp_edge(s, b, a, "RUNS_ON")
        s.commit()

    body = client.get("/api/graph/stats").json()
    types = {row["type"]: row["count"] for row in body["items"]}
    assert "RUNS_ON" in types
    assert types["RUNS_ON"] >= 1


def test_stats_no_longer_claims_the_store_is_frozen(client):
    """P4's frozen/deprecated/frozen_reason flags are gone with the store.

    They said "this data is frozen and scheduled for removal". That sentence is
    false about graph_edges, so leaving the flags on would be worse than never
    having shipped them — a client would label live data as dying.
    """
    body = client.get("/api/graph/stats").json()
    assert "frozen" not in body
    assert "deprecated" not in body
    assert "frozen_reason" not in body


def test_stats_counts_active_edges_only(client, engine):
    """A retired edge is history; counting it would inflate "what does the
    graph currently assert". The full bitemporal census stays at
    /kg/stats?active_only=false."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from infra_brain.db.models import GraphEdge as GraphEdgeRow

    with Session(engine) as s:
        a = _bp_node(s, "LinuxHost", "retired-host", "retired-host")
        b = _bp_node(s, "HomelabService", "retired-svc", "retired-svc")
        e = _bp_edge(s, b, a, "STATS_RETIRED_TYPE")
        s.commit()
        eid = e.id
    with Session(engine) as s:
        s.get(GraphEdgeRow, eid).valid_to = _dt.now(_tz.utc)
        s.commit()

    body = client.get("/api/graph/stats").json()
    assert "STATS_RETIRED_TYPE" not in {row["type"] for row in body["items"]}
    full = client.get("/api/graph/kg/stats?active_only=false").json()
    assert "STATS_RETIRED_TYPE" in {row["type"] for row in full["edge_types"]}


# ---------------------------------------------------------------------------
# The removed legacy read surface (graph-first P5)
# ---------------------------------------------------------------------------


def test_legacy_routes_are_gone(client):
    """/relationships, /search and the /{resource_id} ego-network are removed.

    Not deprecated, not aliased: their response bodies were keyed in the
    ``resources.id`` space (``from_resource_id``/``to_resource_id``/
    ``relationship_type``), so re-pointing them at graph_edges would have kept
    the URL while silently changing the id space underneath it. /kg/edges,
    /kg/search and /kg/{node_id} are the honestly-renamed replacements.

    404 (not 405): the paths do not exist at all. The single-segment wildcard
    that used to swallow every unmatched path is gone too, which is what makes
    "/api/graph/relationships" a clean 404 rather than a UUID-parse 422.
    """
    assert client.get("/api/graph/relationships").status_code == 404
    assert client.get("/api/graph/search?q=anything").status_code == 404
    assert client.get(f"/api/graph/{_uuid_mod.uuid4()}").status_code == 404


def test_graph_api_module_holds_no_reference_to_the_dropped_table(client):
    """Belt-and-braces on the P5 precondition: no executable line in this
    module may name ``resource_relationships``. The module docstring may (and
    does) explain the removal — this checks code, not prose."""
    import ast
    import inspect

    from infra_brain import graph_api

    tree = ast.parse(inspect.getsource(graph_api))
    # Docstrings are the first statement of a module/class/function body.
    # Identify them by NODE, not by text: ast.get_docstring() returns a cleaned
    # (dedented) copy that never compares equal to the raw Constant.
    doc_nodes: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            doc_nodes.add(id(first.value))

    offenders = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "resource_relationships" in n.value
        and id(n) not in doc_nodes
    ]
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# /api/graph/kg/edges — the flat edge census that replaces /relationships
# ---------------------------------------------------------------------------


def test_kg_edges_unmatched_filter_is_an_empty_page(client):
    body = client.get("/api/graph/kg/edges?type=NO_SUCH_EDGE_TYPE_AT_ALL").json()
    assert body["items"] == []
    assert body["total"] == 0


def test_kg_edges_returns_named_endpoints(client, engine):
    """Names alongside ids, never instead of them: a client rendering an edge
    list must not have to N+1 the node endpoint to show two labels."""
    with Session(engine) as s:
        host = _bp_node(s, "LinuxHost", "edges-host", "edges-host")
        svc = _bp_node(s, "HomelabService", "edges-svc", "edges-svc")
        _bp_edge(s, svc, host, "EDGELIST_TYPE")
        s.commit()

    body = client.get("/api/graph/kg/edges?type=EDGELIST_TYPE").json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["source_name"] == "edges-svc"
    assert row["target_name"] == "edges-host"
    assert row["edge_type"] == "EDGELIST_TYPE"
    assert _uuid_mod.UUID(row["source_id"])
    assert _uuid_mod.UUID(row["target_id"])


def test_kg_edges_type_filter_and_total(client, engine):
    with Session(engine) as s:
        a = _bp_node(s, "LinuxHost", "filter-a", "filter-a")
        b = _bp_node(s, "HomelabService", "filter-b", "filter-b")
        c = _bp_node(s, "HomelabService", "filter-c", "filter-c")
        _bp_edge(s, b, a, "FILTER_ONE")
        _bp_edge(s, c, a, "FILTER_TWO")
        s.commit()

    one = client.get("/api/graph/kg/edges?type=FILTER_ONE").json()
    assert one["total"] == 1
    assert {r["edge_type"] for r in one["items"]} == {"FILTER_ONE"}


def test_kg_edges_keyset_pagination_covers_the_full_set(client, engine):
    """Keyset mode walks the whole set exactly once, no gaps and no repeats.

    Keyset mode orders by ``id`` (that is what makes ``id > cursor`` a valid
    page boundary), so a caller uses it from the FIRST page — seeded here with
    the zero UUID — rather than switching to it mid-run from an offset page
    ordered by name. Mixing the two would skip rows, which is exactly the trap
    the route's docstring warns about.
    """
    with Session(engine) as s:
        hub = _bp_node(s, "LinuxHost", "page-hub", "page-hub")
        for i in range(5):
            leaf = _bp_node(s, "HomelabService", f"page-leaf-{i}", f"page-leaf-{i}")
            _bp_edge(s, leaf, hub, "PAGE_TYPE")
        s.commit()

    seen: list[str] = []
    cursor = "00000000-0000-0000-0000-000000000000"
    for _ in range(10):
        page = client.get(f"/api/graph/kg/edges?type=PAGE_TYPE&limit=2&after_id={cursor}").json()[
            "items"
        ]
        if not page:
            break
        seen.extend(r["id"] for r in page)
        cursor = page[-1]["id"]
    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_kg_edges_offset_pagination_covers_the_full_set(client, engine):
    with Session(engine) as s:
        hub = _bp_node(s, "LinuxHost", "off-hub", "off-hub")
        for i in range(5):
            leaf = _bp_node(s, "HomelabService", f"off-leaf-{i}", f"off-leaf-{i}")
            _bp_edge(s, leaf, hub, "OFFSET_TYPE")
        s.commit()

    seen: list[str] = []
    for offset in range(0, 6, 2):
        page = client.get(f"/api/graph/kg/edges?type=OFFSET_TYPE&limit=2&offset={offset}").json()[
            "items"
        ]
        seen.extend(r["id"] for r in page)
    assert len(set(seen)) == 5


def test_kg_edges_invalid_cursor_returns_422(client):
    assert client.get("/api/graph/kg/edges?after_id=not-a-uuid").status_code == 422


def test_kg_edges_route_is_not_swallowed_by_the_node_wildcard(client):
    """``/kg/edges`` sits before ``/kg/{node_id}`` in the router; if that
    ordering is ever broken this returns 422 (bad UUID) instead of 200."""
    assert client.get("/api/graph/kg/edges").status_code == 200


# ---------------------------------------------------------------------------
# /api/graph/blast-radius/{node_id}
# ---------------------------------------------------------------------------
# Real (sqlite) graph_phase2/graph_phase3 traversal — same node/edge fixture
# pattern as tests/test_graph_phase3_traversal.py, run through the FastAPI
# route rather than calling graph_phase3.blast_radius directly. This is the
# route the RelationshipMiniGraph widget's data will come from (Phase 2 task,
# dashboard-app/src/components/ui/RelationshipMiniGraph.tsx) — its
# node/resource/edge_type/hop_distance/confidence/why shape is exercised here.


def _bp_node(session, node_type, key, name, resource_id=None):
    from infra_brain import graph_phase2

    return graph_phase2.upsert_node(
        session,
        node_type=node_type,
        natural_key=key,
        name=name,
        source="test",
        resource_id=resource_id,
    )


def _bp_edge(session, a, b, edge_type, confidence="1.000"):
    from decimal import Decimal

    from infra_brain import graph_phase2
    from infra_brain.db.models import GraphEdgeMethod

    return graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=edge_type,
        method=GraphEdgeMethod.DECLARED,
        confidence=Decimal(confidence),
        source="test",
        evidence={"basis": "test fixture"},
    )


def test_blast_radius_unknown_node_returns_404(client):
    fresh = str(_uuid_mod.uuid4())
    resp = client.get(f"/api/graph/blast-radius/{fresh}")
    assert resp.status_code == 404


def test_blast_radius_invalid_uuid_returns_422(client):
    resp = client.get("/api/graph/blast-radius/not-a-valid-uuid")
    assert resp.status_code == 422


def test_blast_radius_node_with_no_neighbors_returns_empty_list(client, engine):
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        lonely = _bp_node(s, GraphNodeType.VSPHERE_VM, "bp:lonely-1", "bp-lonely")
        s.commit()
        node_id = str(lonely.id)

    resp = client.get(f"/api/graph/blast-radius/{node_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["neighbors"] == []
    assert body["count"] == 0
    assert body["total_found"] == 0
    assert body["truncated"] is False
    assert body["root"]["name"] == "bp-lonely"


def test_blast_radius_returns_real_neighbors_with_expected_shape(client, engine):
    from infra_brain.db.models import GraphEdgeType, GraphNodeType

    with Session(engine) as s:
        vm = _bp_node(s, GraphNodeType.VSPHERE_VM, "bp:vm-1", "bp-web01")
        esxi = _bp_node(s, GraphNodeType.VSPHERE_HOST, "bp:host-1", "bp-esxi01")
        _bp_edge(s, vm, esxi, GraphEdgeType.HOSTED_ON)
        s.commit()
        node_id = str(vm.id)

    resp = client.get(f"/api/graph/blast-radius/{node_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["root"]["name"] == "bp-web01"
    assert body["count"] == 1
    assert body["total_found"] == 1
    assert body["truncated"] is False
    neighbor = body["neighbors"][0]
    assert neighbor["node"]["name"] == "bp-esxi01"
    assert neighbor["edge_type"] == GraphEdgeType.HOSTED_ON.value
    assert neighbor["hop_distance"] == 1
    assert neighbor["confidence"] == pytest.approx(1.0)
    assert isinstance(neighbor["why"], str) and neighbor["why"]
    # RelationshipMiniGraph (dashboard-app) expects exactly these fields on
    # every neighbour so it can build {hub, satellites, edges} client-side.
    assert set(neighbor.keys()) == {
        "node",
        "resource",
        "edge_type",
        "hop_distance",
        "confidence",
        "why",
    }


def test_blast_radius_query_params_are_honored(client, engine):
    from infra_brain.db.models import GraphEdgeType, GraphNodeType

    with Session(engine) as s:
        vm = _bp_node(s, GraphNodeType.VSPHERE_VM, "bp:vm-2", "bp-web02")
        esxi = _bp_node(s, GraphNodeType.VSPHERE_HOST, "bp:host-2", "bp-esxi02")
        ds = _bp_node(s, GraphNodeType.VSPHERE_DATASTORE, "bp:ds-2", "bp-datastore02")
        _bp_edge(s, vm, esxi, GraphEdgeType.HOSTED_ON)
        _bp_edge(s, esxi, ds, GraphEdgeType.MOUNTS_DATASTORE)
        s.commit()
        node_id = str(vm.id)

    # max_hops=1 must not reach the 2-hop datastore.
    resp = client.get(f"/api/graph/blast-radius/{node_id}?max_hops=1")
    assert resp.status_code == 200
    names = {n["node"]["name"] for n in resp.json()["neighbors"]}
    assert names == {"bp-esxi02"}

    # max_hops=2 reaches both.
    resp2 = client.get(f"/api/graph/blast-radius/{node_id}?max_hops=2")
    names2 = {n["node"]["name"] for n in resp2.json()["neighbors"]}
    assert names2 == {"bp-esxi02", "bp-datastore02"}

    # top_n=1 caps the result and reports truncation.
    resp3 = client.get(f"/api/graph/blast-radius/{node_id}?max_hops=2&top_n=1")
    body3 = resp3.json()
    assert body3["count"] == 1
    assert body3["total_found"] == 2
    assert body3["truncated"] is True


# ---------------------------------------------------------------------------
# /entity-resolution/queue + /entity-resolution/{action_id}/confirm (TRK-226)
# ---------------------------------------------------------------------------


def _seed_review_action(session, source_node, candidate_node, score=0.85, status="pending"):
    from infra_brain.db.models import ProposedAction
    from infra_brain.graph_phase3 import EMITTER_SAME_AS, REVIEW_ACTION_TYPE

    action = ProposedAction(
        id=_uuid_mod.uuid4(),
        agent=EMITTER_SAME_AS,
        action_type=REVIEW_ACTION_TYPE,
        target=f"same-as:{source_node.node_type}:{source_node.natural_key}",
        payload={
            "source_node": {
                "node_id": str(source_node.id),
                "node_type": source_node.node_type,
                "natural_key": source_node.natural_key,
                "name": source_node.name,
                "source": source_node.source,
            },
            "candidate_matches": [
                {
                    "node_id": str(candidate_node.id),
                    "node_type": candidate_node.node_type,
                    "natural_key": candidate_node.natural_key,
                    "name": candidate_node.name,
                    "source": candidate_node.source,
                    "score": score,
                    "reason": "fuzzy name match (test fixture)",
                }
            ],
        },
        confidence=score,
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    session.add(action)
    session.commit()
    return action


def test_entity_resolution_queue_empty(client):
    resp = client.get("/api/graph/entity-resolution/queue")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_entity_resolution_queue_returns_pending_row(client, engine):
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:src-1", "er-src-1")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:cand-1", "er-cand-1")
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id = str(action.id)

    resp = client.get("/api/graph/entity-resolution/queue")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["action_id"] == action_id
    assert row["status"] == "pending"
    assert row["source_node"]["name"] == "er-src-1"
    assert len(row["candidate_matches"]) == 1
    assert row["candidate_matches"][0]["name"] == "er-cand-1"


def test_entity_resolution_queue_domain_filter(client, engine):
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:src-domainfilter", "er-src-df")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:cand-domainfilter", "er-cand-df")
        s.commit()
        _seed_review_action(s, src, cand)

    # src.source == "test" (set by _bp_node), so filtering on an unrelated
    # domain must exclude it.
    resp = client.get("/api/graph/entity-resolution/queue?domain=vsphere")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_confirm_entity_resolution_success(client, engine):
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:confirm-src", "er-confirm-src")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:confirm-cand", "er-confirm-cand")
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id = str(action.id)
        cand_id = str(cand.id)

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/confirm",
        json={"target_node_id": cand_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed"] is True
    assert body["review_resolved"] is True
    assert len(body["edge_ids"]) == 2
    assert body["method"] == "declared"
    assert body["confidence"] == 1.0

    # The review-queue row must now be resolved, not still pending. The
    # engine fixture is module-scoped (shared across every test in this
    # file), so other tests' still-pending rows may also be in the queue —
    # find this test's own row by action_id rather than assuming items[0].
    resp2 = client.get("/api/graph/entity-resolution/queue")
    row = next(r for r in resp2.json()["items"] if r["action_id"] == action_id)
    assert row["status"] == "approved"


def test_confirm_entity_resolution_unknown_action_returns_404(client):
    resp = client.post(
        f"/api/graph/entity-resolution/{_uuid_mod.uuid4()}/confirm",
        json={"target_node_id": str(_uuid_mod.uuid4())},
    )
    assert resp.status_code == 404


def test_confirm_entity_resolution_invalid_action_uuid_returns_422(client):
    resp = client.post(
        "/api/graph/entity-resolution/not-a-uuid/confirm",
        json={"target_node_id": str(_uuid_mod.uuid4())},
    )
    assert resp.status_code == 422


def test_confirm_entity_resolution_invalid_target_uuid_returns_422(client, engine):
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:badtarget-src", "er-badtarget-src")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:badtarget-cand", "er-badtarget-cand")
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id = str(action.id)

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/confirm",
        json={"target_node_id": "not-a-uuid"},
    )
    assert resp.status_code == 422


def test_confirm_entity_resolution_already_resolved_returns_409(client, engine):
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:already-src", "er-already-src")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:already-cand", "er-already-cand")
        s.commit()
        action = _seed_review_action(s, src, cand, status="rejected")
        action_id = str(action.id)
        cand_id = str(cand.id)

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/confirm",
        json={"target_node_id": cand_id},
    )
    assert resp.status_code == 409


def test_confirm_entity_resolution_refused_pairing_returns_422(client, engine):
    """confirm_same_as refuses (422, not 500) when both nodes share a node_type."""
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:sametype-src", "er-sametype-src")
        # Same node_type as src -- confirm_same_as must refuse this pairing.
        cand = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:sametype-cand", "er-sametype-cand")
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id = str(action.id)
        cand_id = str(cand.id)

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/confirm",
        json={"target_node_id": cand_id},
    )
    assert resp.status_code == 422


def test_reject_entity_resolution_writes_an_attributed_veto(client, engine):
    """KG-3: rejecting a review row is an EXECUTING write, not a status flip."""
    from infra_brain.db.models import GraphEdge, GraphEdgeType, GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:reject-src", "er-reject-src")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:reject-cand", "er-reject-cand")
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id, src_id, cand_id = str(action.id), src.id, cand.id

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/reject",
        json={"target_node_id": str(cand_id), "reason": "two distinct boxes"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rejected"] is True
    assert body["status"] == "rejected"  # sole candidate, so the row closes
    assert [v["node_id"] for v in body["vetoed"]] == [str(cand_id)]

    with Session(engine) as s:
        vetoes = (
            s.execute(
                select(GraphEdge).where(
                    GraphEdge.edge_type == GraphEdgeType.NOT_SAME_AS.value,
                    GraphEdge.valid_to.is_(None),
                    GraphEdge.source_id.in_([src_id, cand_id]),
                    GraphEdge.target_id.in_([src_id, cand_id]),
                )
            )
            .scalars()
            .all()
        )
    assert len(vetoes) == 2, "the veto is symmetric"
    for edge in vetoes:
        assert edge.authority == "human"
        assert (edge.evidence or {}).get("basis") == "human_rejection"
        assert (edge.evidence or {}).get("reason") == "two distinct boxes"


def test_reject_entity_resolution_unknown_action_returns_404(client):
    resp = client.post(f"/api/graph/entity-resolution/{_uuid_mod.uuid4()}/reject", json={})
    assert resp.status_code == 404


def test_reject_entity_resolution_already_decided_returns_409(client, engine):
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:reject-409-src", "er-r409-src")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:reject-409-cand", "er-r409-cand")
        s.commit()
        action = _seed_review_action(s, src, cand, status="rejected")
        action_id = str(action.id)

    resp = client.post(f"/api/graph/entity-resolution/{action_id}/reject", json={})
    assert resp.status_code == 409


def test_reject_entity_resolution_target_must_be_a_queued_candidate(client, engine):
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:reject-oob-src", "er-roob-src")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:reject-oob-cand", "er-roob-cand")
        stranger = _bp_node(
            s, GraphNodeType.OCTOPUS_MACHINE, "er:reject-oob-other", "er-roob-other"
        )
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id, stranger_id = str(action.id), str(stranger.id)

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/reject",
        json={"target_node_id": stranger_id},
    )
    assert resp.status_code == 422


def test_confirm_entity_resolution_rejects_non_review_action_type(client, engine):
    """action_id must point at an entity_resolution_same_as row, not any
    other ProposedAction type (e.g. a remediation) -- else 404, not 500/200."""
    from infra_brain.db.models import ProposedAction

    with Session(engine) as s:
        other = ProposedAction(
            id=_uuid_mod.uuid4(),
            agent="some_other_agent",
            action_type="config_fix",
            target="not-a-review-row",
            payload={},
            confidence=0.9,
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        s.add(other)
        s.commit()
        other_id = str(other.id)

    resp = client.post(
        f"/api/graph/entity-resolution/{other_id}/confirm",
        json={"target_node_id": str(_uuid_mod.uuid4())},
    )
    assert resp.status_code == 404


def test_confirm_entity_resolution_target_must_be_a_queued_candidate(client, engine):
    """A target_node_id NOT among this action's own candidate_matches must be
    refused (422), even if it's a real, valid, unrelated graph node -- else
    action_id degrades to a mere pending-state gate rather than actually
    constraining WHICH pairing a human may confirm."""
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:notcand-src", "er-notcand-src")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:notcand-cand", "er-notcand-cand")
        unrelated = _bp_node(
            s, GraphNodeType.R7_ASSET, "er:notcand-unrelated", "er-notcand-unrelated"
        )
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id = str(action.id)
        unrelated_id = str(unrelated.id)

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/confirm",
        json={"target_node_id": unrelated_id},
    )
    assert resp.status_code == 422
    assert "candidate_matches" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Admin-gate enforcement for POST /entity-resolution/{action_id}/confirm.
# The `client` fixture above runs in dev-mode (INFRA_BRAIN_DEV=1), which
# bypasses require_admin entirely -- this exercises the real (non-dev-mode)
# auth stack, mirroring tests/test_mcp_keys_api.py's admin_gated_app fixture.
# ---------------------------------------------------------------------------


class _FakeAuthRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}

    def set(self, key, value, ex=None):
        self.kv[key] = value
        return True

    def exists(self, key):
        return 1 if key in self.kv else 0

    def zadd(self, key, mapping):
        return len(mapping)

    def zremrangebyscore(self, key, mn, mx):
        return 0

    def zcard(self, key):
        return 0

    def expire(self, key, ttl):
        return True

    def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)


@pytest.fixture
def admin_gated_client(monkeypatch):
    """Real (non-dev-mode) auth stack over graph_router, seeded with one
    admin and one viewer user."""
    import bcrypt

    monkeypatch.delenv("INFRA_BRAIN_DEV", raising=False)
    monkeypatch.setenv("UI_COOKIE_SECRET", "unit-test-secret")
    from infra_brain.config import get_settings

    get_settings.cache_clear()

    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(
            UIUser(
                username="admin-user",
                password_hash=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
                name="Admin",
                role="admin",
                active=True,
            )
        )
        s.add(
            UIUser(
                username="viewer-user",
                password_hash=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
                name="Viewer",
                role="viewer",
                active=True,
            )
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    import infra_brain.dashboard_auth as auth_mod
    from infra_brain.graph_api import graph_router

    monkeypatch.setattr(auth_mod, "get_session", _get_session)
    monkeypatch.setattr(auth_mod, "get_redis", lambda: _FakeAuthRedis())

    app = FastAPI()
    app.include_router(auth_mod.auth_router)
    app.include_router(graph_router)

    with patch("infra_brain.graph_api.get_session", _get_session):
        yield TestClient(app, base_url="https://testserver")


def test_confirm_entity_resolution_requires_admin_403_for_viewer(admin_gated_client):
    client = admin_gated_client
    client.post("/api/dashboard/login", json={"username": "viewer-user", "password": "pw"})
    resp = client.post(
        f"/api/graph/entity-resolution/{_uuid_mod.uuid4()}/confirm",
        json={"target_node_id": str(_uuid_mod.uuid4())},
    )
    assert resp.status_code == 403


def test_confirm_entity_resolution_admin_session_passes_the_gate(admin_gated_client):
    """Regression: admins keep write access. Uses an unknown action_id so the
    assertion is purely about the auth gate (403 vs. past-the-gate), not the
    business logic already covered above -- past the admin gate, an unknown
    action_id 404s rather than 403ing."""
    client = admin_gated_client
    client.post("/api/dashboard/login", json={"username": "admin-user", "password": "pw"})
    resp = client.post(
        f"/api/graph/entity-resolution/{_uuid_mod.uuid4()}/confirm",
        json={"target_node_id": str(_uuid_mod.uuid4())},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /entity-resolution/{action_id}/retract (TRK-228)
# ---------------------------------------------------------------------------


def test_retract_entity_resolution_success(client, engine):
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:retract-src", "er-retract-src")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:retract-cand", "er-retract-cand")
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id = str(action.id)
        cand_id = str(cand.id)

    confirm_resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/confirm",
        json={"target_node_id": cand_id},
    )
    assert confirm_resp.status_code == 200

    retract_resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/retract",
        json={"reason": "wrong match, picked the wrong candidate"},
    )
    assert retract_resp.status_code == 200
    body = retract_resp.json()
    assert body["retracted"] is True
    assert body["review_reopened"] is True
    assert len(body["edge_ids"]) == 2

    row = next(
        r
        for r in client.get("/api/graph/entity-resolution/queue").json()["items"]
        if r["action_id"] == action_id
    )
    assert row["status"] == "pending"
    assert row["retraction_history"][0]["retracted_by"] == "dashboard"
    assert row["retraction_history"][0]["reason"] == "wrong match, picked the wrong candidate"


def test_retract_entity_resolution_ignores_caller_supplied_node_ids(client, engine):
    """The core security property this route rests on: the pairing to
    retract is read from the STORED row, never from request input. A request
    body carrying attacker-supplied node ids must have zero effect -- the
    exact pairing confirmed is what gets retracted, nothing else."""
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:retract-noforge-src", "x")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:retract-noforge-cand", "y")
        # A real, valid node this request will try to smuggle in as the target.
        decoy = _bp_node(s, GraphNodeType.OCTOPUS_MACHINE, "er:retract-decoy", "z")
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id = str(action.id)
        cand_id, decoy_id = str(cand.id), str(decoy.id)

    client.post(
        f"/api/graph/entity-resolution/{action_id}/confirm",
        json={"target_node_id": cand_id},
    )

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/retract",
        # RetractSameAsBody only declares `reason` -- extra fields are
        # ignored by Pydantic, which is exactly the property under test.
        json={"reason": "x", "source_node_id": decoy_id, "target_node_id": decoy_id},
    )

    assert resp.status_code == 200
    # If the decoy ids had been honoured, this would 422 (decoy has no active
    # edge to src) instead of the real confirmed pairing succeeding.
    assert resp.json()["retracted"] is True


def test_retract_entity_resolution_double_retract_returns_409(client, engine):
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:retract-double-src", "x")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:retract-double-cand", "y")
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id = str(action.id)
        cand_id = str(cand.id)

    client.post(
        f"/api/graph/entity-resolution/{action_id}/confirm",
        json={"target_node_id": cand_id},
    )
    first = client.post(f"/api/graph/entity-resolution/{action_id}/retract", json={})
    assert first.status_code == 200

    second = client.post(f"/api/graph/entity-resolution/{action_id}/retract", json={})
    assert second.status_code == 409


def test_retract_then_reconfirm_round_trip(client, engine):
    """Reopening to pending is pointless if a re-confirm can't actually work
    afterward -- candidate_matches must survive the payload prune well enough
    for the confirm route's own candidate-membership check to pass again."""
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:reconfirm-src", "x")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:reconfirm-cand", "y")
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id = str(action.id)
        cand_id = str(cand.id)

    client.post(
        f"/api/graph/entity-resolution/{action_id}/confirm",
        json={"target_node_id": cand_id},
    )
    client.post(f"/api/graph/entity-resolution/{action_id}/retract", json={})

    second_confirm = client.post(
        f"/api/graph/entity-resolution/{action_id}/confirm",
        json={"target_node_id": cand_id},
    )

    assert second_confirm.status_code == 200
    assert second_confirm.json()["confirmed"] is True


def test_retract_entity_resolution_unknown_action_returns_404(client):
    resp = client.post(
        f"/api/graph/entity-resolution/{_uuid_mod.uuid4()}/retract",
        json={},
    )
    assert resp.status_code == 404


def test_retract_entity_resolution_invalid_uuid_returns_422(client):
    resp = client.post(
        "/api/graph/entity-resolution/not-a-uuid/retract",
        json={},
    )
    assert resp.status_code == 422


def test_retract_entity_resolution_still_pending_returns_409(client, engine):
    """A row that was never confirmed has nothing to retract."""
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:retract-pending-src", "x")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:retract-pending-cand", "y")
        s.commit()
        action = _seed_review_action(s, src, cand)
        action_id = str(action.id)

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/retract",
        json={},
    )
    assert resp.status_code == 409


def test_retract_entity_resolution_rejected_row_returns_409(client, engine):
    """A rejected (not approved) row also has nothing to retract."""
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:retract-rejected-src", "x")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:retract-rejected-cand", "y")
        s.commit()
        action = _seed_review_action(s, src, cand, status="rejected")
        action_id = str(action.id)

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/retract",
        json={},
    )
    assert resp.status_code == 409


def test_retract_entity_resolution_missing_confirmation_stamp_returns_422(client, engine):
    """An 'approved' row with no confirmed_target_node_id (e.g. confirmed via
    the MCP tool directly, before this field existed, or hand-edited) has
    nothing for retract to target -- 422, not a 500 KeyError."""
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:retract-nostamp-src", "x")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:retract-nostamp-cand", "y")
        s.commit()
        action = _seed_review_action(s, src, cand, status="approved")
        action_id = str(action.id)

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/retract",
        json={},
    )
    assert resp.status_code == 422
    assert "confirmed_target_node_id" in resp.json()["detail"]


def test_retract_entity_resolution_no_active_edge_returns_422(client, engine):
    """The row is approved and correctly stamped, but the edge it points at
    was already independently retracted/retired -- retract_same_as's own
    'no active SAME_AS edge' refusal must surface as 422, not crash."""
    from infra_brain.db.models import GraphNodeType

    with Session(engine) as s:
        src = _bp_node(s, GraphNodeType.VSPHERE_VM, "er:retract-noedge-src", "x")
        cand = _bp_node(s, GraphNodeType.R7_ASSET, "er:retract-noedge-cand", "y")
        s.commit()
        action = _seed_review_action(s, src, cand, status="approved")
        action.payload = {**action.payload, "confirmed_target_node_id": str(cand.id)}
        s.commit()
        action_id = str(action.id)
        # Deliberately no SAME_AS edge was ever written between src/cand --
        # confirm_same_as normally writes one, but this simulates the stamp
        # existing without the edge (e.g. a prior retract already ran, or an
        # inconsistent hand-edited row).

    resp = client.post(
        f"/api/graph/entity-resolution/{action_id}/retract",
        json={},
    )
    assert resp.status_code == 422
    assert "no active, human-confirmed SAME_AS edge" in resp.json()["detail"]


def test_retract_entity_resolution_requires_admin_403_for_viewer(admin_gated_client):
    client = admin_gated_client
    client.post("/api/dashboard/login", json={"username": "viewer-user", "password": "pw"})
    resp = client.post(
        f"/api/graph/entity-resolution/{_uuid_mod.uuid4()}/retract",
        json={},
    )
    assert resp.status_code == 403


def test_retract_entity_resolution_admin_session_passes_the_gate(admin_gated_client):
    """Regression: admins keep write access. Unknown action_id -> 404, not 403."""
    client = admin_gated_client
    client.post("/api/dashboard/login", json={"username": "admin-user", "password": "pw"})
    resp = client.post(
        f"/api/graph/entity-resolution/{_uuid_mod.uuid4()}/retract",
        json={},
    )
    assert resp.status_code == 404
