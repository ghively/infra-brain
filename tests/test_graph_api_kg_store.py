"""Tests for the ``/api/graph/kg/*`` routes — the graph_nodes/graph_edges store.

WHY THESE EXIST
---------------
Every pre-existing ``/api/graph/*`` route reads ``resource_relationships``:
``/stats`` GROUPs that table, ``/relationships`` SELECTs it, and both
``/search`` and ``/{resource_id}`` go through ``db.relationships.get_neighborhood``,
which walks it. The Graph dashboard page — the flagship view of the product's
core — therefore rendered ONLY the legacy store, and could not show a single
edge written by ``graph_engine`` into the bitemporal ``graph_nodes`` /
``graph_edges`` tables (``RUNS_ON``, ``BELONGS_TO``, ``DEFINED_IN``).
``/blast-radius`` was the one route that touched the new store, and it returns
a ranked neighbour LIST, not a subgraph the canvas can draw.

These routes fill exactly that gap and nothing more: same ``GraphOut``-shaped
answer the page's existing renderer already consumes, sourced from the new
store, with honest server-side caps.

SQLite: the fixtures below use ``graph_phase2.upsert_node``/``upsert_edge``
(the same helpers the existing blast-radius tests in ``test_graph_api.py`` use
against SQLite) and the routes use plain SQLAlchemy Core/ORM with an iterative
BFS — no PostgreSQL-only recursive CTE — so they run on the sqlite suite.
"""

from __future__ import annotations

import uuid as _uuid_mod
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import GraphEdgeMethod

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    """Function-scoped: every test gets an empty store, so node/edge totals
    are assertable without depending on test ordering."""
    eng = make_engine()
    return eng


@pytest.fixture
def client(engine, monkeypatch):
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
# Fixture helpers — mirror test_graph_api.py's _bp_node/_bp_edge
# ---------------------------------------------------------------------------


def _node(session, node_type, key, name, source="test", attributes=None):
    from infra_brain import graph_phase2

    return graph_phase2.upsert_node(
        session,
        node_type=node_type,
        natural_key=key,
        name=name,
        source=source,
        attributes=attributes,
    )


def _edge(session, a, b, edge_type, confidence="1.000", method=GraphEdgeMethod.DECLARED):
    from infra_brain import graph_phase2

    return graph_phase2.upsert_edge(
        session,
        source_id=a.id,
        target_id=b.id,
        edge_type=edge_type,
        method=method,
        confidence=Decimal(confidence),
        source="test",
        evidence={"basis": "test fixture"},
    )


def _seed_homelab(engine):
    """The shape the real store holds: one host, three services RUNS_ON it,
    one project with two files (BELONGS_TO) and the inverse DEFINED_IN.

    Returns a dict of natural_key -> node id (str).
    """
    with Session(engine) as s:
        host = _node(s, "LinuxHost", "node_a", "node_a", source="linux")
        svc_a = _node(
            s,
            "HomelabService",
            "node_a/litellm",
            "litellm",
            source="homelab_services",
            attributes={"host": "node_a", "port": 4000},
        )
        svc_b = _node(
            s,
            "HomelabService",
            "node_a/ollama",
            "ollama",
            source="homelab_services",
            attributes={"host": "node_a"},
        )
        svc_c = _node(
            s,
            "HomelabService",
            "node_a/open-webui",
            "open-webui",
            source="homelab_services",
            attributes={"host": "node_a"},
        )
        proj = _node(s, "GitlabProject", "113", "homelab-ansible", source="cicd")
        file_a = _node(s, "IaCFile", "113:site.yml", "site.yml", source="iac")
        file_b = _node(s, "IaCFile", "113:.gitlab-ci.yml", ".gitlab-ci.yml", source="iac")

        for svc in (svc_a, svc_b, svc_c):
            _edge(s, svc, host, "RUNS_ON", "0.990", GraphEdgeMethod.DETERMINISTIC_MATCH)
        for f in (file_a, file_b):
            _edge(s, f, proj, "BELONGS_TO")
            _edge(s, proj, f, "DEFINED_IN")
        s.commit()
        return {
            "host": str(host.id),
            "svc_a": str(svc_a.id),
            "proj": str(proj.id),
            "file_a": str(file_a.id),
        }


# ---------------------------------------------------------------------------
# /api/graph/kg/stats
# ---------------------------------------------------------------------------


def test_kg_stats_empty_store(client):
    resp = client.get("/api/graph/kg/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["edge_types"] == []
    assert body["node_types"] == []
    assert body["total_nodes"] == 0
    assert body["total_edges"] == 0


def test_kg_stats_reports_the_three_new_edge_types(client, engine):
    """The regression this whole route set exists for: these edge types live
    ONLY in graph_edges, so /api/graph/stats (resource_relationships) can never
    show them."""
    _seed_homelab(engine)
    body = client.get("/api/graph/kg/stats").json()
    counts = {row["type"]: row["count"] for row in body["edge_types"]}
    assert counts["RUNS_ON"] == 3
    assert counts["BELONGS_TO"] == 2
    assert counts["DEFINED_IN"] == 2
    assert body["total_edges"] == 7


def test_kg_stats_reports_node_types_so_the_legend_can_be_typed(client, engine):
    _seed_homelab(engine)
    body = client.get("/api/graph/kg/stats").json()
    counts = {row["type"]: row["count"] for row in body["node_types"]}
    assert counts["HomelabService"] == 3
    assert counts["LinuxHost"] == 1
    assert counts["IaCFile"] == 2
    assert counts["GitlabProject"] == 1
    assert body["total_nodes"] == 7


def test_kg_stats_counts_only_active_edges_by_default(client, engine):
    """valid_to IS NULL is the default view — a retired edge is history, not
    the current state of the estate."""
    ids = _seed_homelab(engine)
    from infra_brain.db.models import GraphEdge

    with Session(engine) as s:
        edge = (
            s.query(GraphEdge)
            .filter(GraphEdge.edge_type == "RUNS_ON", GraphEdge.source_id == _uuid(ids["svc_a"]))
            .one()
        )
        edge.valid_to = _now()
        s.commit()

    active = client.get("/api/graph/kg/stats").json()
    assert {r["type"]: r["count"] for r in active["edge_types"]}["RUNS_ON"] == 2
    everything = client.get("/api/graph/kg/stats?active_only=false").json()
    assert {r["type"]: r["count"] for r in everything["edge_types"]}["RUNS_ON"] == 3


# ---------------------------------------------------------------------------
# /api/graph/kg/search
# ---------------------------------------------------------------------------


def test_kg_search_no_match_returns_empty_candidates(client, engine):
    _seed_homelab(engine)
    body = client.get("/api/graph/kg/search?q=nothing-matches-this").json()
    assert body["candidates"] == []


def test_kg_search_finds_a_host_by_name(client, engine):
    ids = _seed_homelab(engine)
    body = client.get("/api/graph/kg/search?q=node_a").json()
    by_id = {c["id"]: c for c in body["candidates"]}
    assert ids["host"] in by_id
    host = by_id[ids["host"]]
    assert host["type"] == "LinuxHost"
    assert host["name"] == "node_a"


def test_kg_search_matches_natural_key_not_only_display_name(client, engine):
    """A service's natural_key is "<host>/<service>" while its name is just the
    service — searching the host name must still surface it."""
    _seed_homelab(engine)
    body = client.get("/api/graph/kg/search?q=node_a/litellm").json()
    assert [c["name"] for c in body["candidates"]] == ["litellm"]


def test_kg_search_is_capped(client, engine):
    _seed_homelab(engine)
    body = client.get("/api/graph/kg/search?q=&limit=2").json()
    assert len(body["candidates"]) <= 2


# ---------------------------------------------------------------------------
# /api/graph/kg/{node_id} — the neighbourhood the canvas draws
# ---------------------------------------------------------------------------


def test_kg_neighborhood_invalid_uuid_returns_422(client):
    assert client.get("/api/graph/kg/not-a-uuid").status_code == 422


def test_kg_neighborhood_unknown_node_returns_404(client):
    resp = client.get(f"/api/graph/kg/{_uuid_mod.uuid4()}")
    assert resp.status_code == 404


def test_pick_a_host_and_see_its_services(client, engine):
    """The headline user story: select node_a, see what runs on it."""
    ids = _seed_homelab(engine)
    body = client.get(f"/api/graph/kg/{ids['host']}?depth=1").json()

    names = sorted(n["name"] for n in body["nodes"])
    assert names == sorted(["node_a", "litellm", "ollama", "open-webui"])
    assert {e["edge_type"] for e in body["edges"]} == {"RUNS_ON"}
    # Direction is preserved: service RUNS_ON host, never the reverse.
    assert all(e["target_id"] == ids["host"] for e in body["edges"])


def test_pick_a_project_and_see_its_files(client, engine):
    ids = _seed_homelab(engine)
    body = client.get(f"/api/graph/kg/{ids['proj']}?depth=1").json()
    names = sorted(n["name"] for n in body["nodes"])
    assert names == [".gitlab-ci.yml", "homelab-ansible", "site.yml"]
    assert {e["edge_type"] for e in body["edges"]} == {"BELONGS_TO", "DEFINED_IN"}


def test_kg_neighborhood_nodes_carry_type_source_and_attributes(client, engine):
    """Node detail: the side panel needs more than a name to be useful."""
    ids = _seed_homelab(engine)
    body = client.get(f"/api/graph/kg/{ids['svc_a']}?depth=1").json()
    svc = next(n for n in body["nodes"] if n["id"] == ids["svc_a"])
    assert svc["type"] == "HomelabService"
    assert svc["source"] == "homelab_services"
    assert svc["natural_key"] == "node_a/litellm"
    assert svc["attributes"]["port"] == 4000


def test_kg_neighborhood_edges_carry_type_confidence_and_method(client, engine):
    ids = _seed_homelab(engine)
    body = client.get(f"/api/graph/kg/{ids['host']}?depth=1").json()
    edge = body["edges"][0]
    assert edge["edge_type"] == "RUNS_ON"
    assert edge["confidence"] == pytest.approx(0.99)
    assert edge["method"] == "deterministic_match"
    assert edge["authority"] == "auto"


def test_kg_neighborhood_depth_two_reaches_across_a_hop(client, engine):
    """From a file: hop 1 is its project, hop 2 is the project's other file."""
    ids = _seed_homelab(engine)
    one = client.get(f"/api/graph/kg/{ids['file_a']}?depth=1").json()
    assert sorted(n["name"] for n in one["nodes"]) == ["homelab-ansible", "site.yml"]
    two = client.get(f"/api/graph/kg/{ids['file_a']}?depth=2").json()
    assert sorted(n["name"] for n in two["nodes"]) == [
        ".gitlab-ci.yml",
        "homelab-ansible",
        "site.yml",
    ]


def test_kg_neighborhood_excludes_retired_edges_by_default(client, engine):
    ids = _seed_homelab(engine)
    from infra_brain.db.models import GraphEdge

    with Session(engine) as s:
        edge = (
            s.query(GraphEdge)
            .filter(GraphEdge.edge_type == "RUNS_ON", GraphEdge.source_id == _uuid(ids["svc_a"]))
            .one()
        )
        edge.valid_to = _now()
        s.commit()

    active = client.get(f"/api/graph/kg/{ids['host']}?depth=1").json()
    assert "litellm" not in [n["name"] for n in active["nodes"]]
    historical = client.get(f"/api/graph/kg/{ids['host']}?depth=1&active_only=false").json()
    assert "litellm" in [n["name"] for n in historical["nodes"]]


def test_kg_neighborhood_reports_totals_so_truncation_is_never_silent(client, engine):
    """Scale honesty: a capped result must say how much it is NOT showing."""
    ids = _seed_homelab(engine)
    body = client.get(f"/api/graph/kg/{ids['host']}?depth=1&max_nodes=2").json()
    assert len(body["nodes"]) == 2
    assert body["node_total"] == 4
    assert body["truncated"] is True
    # Untruncated results say so, rather than leaving the client to infer it.
    full = client.get(f"/api/graph/kg/{ids['host']}?depth=1").json()
    assert full["truncated"] is False
    assert full["node_total"] == len(full["nodes"]) == 4


def test_kg_neighborhood_root_survives_truncation(client, engine):
    """Truncating away the node the user asked for would be absurd — the root
    is always first in BFS order and therefore always kept."""
    ids = _seed_homelab(engine)
    body = client.get(f"/api/graph/kg/{ids['host']}?depth=1&max_nodes=1").json()
    assert [n["id"] for n in body["nodes"]] == [ids["host"]]


def test_kg_neighborhood_edges_are_confined_to_returned_nodes(client, engine):
    """A dangling edge whose endpoint was truncated away would render as an
    arrow to nowhere on the canvas."""
    ids = _seed_homelab(engine)
    body = client.get(f"/api/graph/kg/{ids['host']}?depth=1&max_nodes=2").json()
    returned = {n["id"] for n in body["nodes"]}
    for e in body["edges"]:
        assert e["source_id"] in returned
        assert e["target_id"] in returned


def test_kg_neighborhood_isolated_node_returns_just_itself(client, engine):
    with Session(engine) as s:
        lonely = _node(s, "LinuxHost", "orphan", "orphan")
        s.commit()
        lonely_id = str(lonely.id)
    body = client.get(f"/api/graph/kg/{lonely_id}").json()
    assert [n["id"] for n in body["nodes"]] == [lonely_id]
    assert body["edges"] == []


def test_kg_neighborhood_depth_is_clamped(client, engine):
    """Same clamp the legacy routes apply — an unbounded depth on a connected
    graph is a whole-store scan."""
    ids = _seed_homelab(engine)
    assert client.get(f"/api/graph/kg/{ids['host']}?depth=99").status_code == 422


def test_kg_neighborhood_min_confidence_filters_weak_edges(client, engine):
    ids = _seed_homelab(engine)
    # RUNS_ON here is 0.990; 1.0 (declared only) must exclude it.
    body = client.get(f"/api/graph/kg/{ids['host']}?depth=1&min_confidence=1.0").json()
    assert body["edges"] == []
    assert [n["id"] for n in body["nodes"]] == [ids["host"]]


# ---------------------------------------------------------------------------
# small local helpers
# ---------------------------------------------------------------------------


def _uuid(value):
    return _uuid_mod.UUID(value)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
