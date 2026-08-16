"""TDD guard for Task 4.4 — single ``upsert_resource()`` for HTTP + MCP + collectors.

Three divergent write paths (base agent, api/_helpers seed, mcp_server seed)
must collapse into one ``infra_brain.api._seeding.upsert_resource``. This test
module is written FIRST and must fail until that module/function exists.
"""

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from unittest.mock import patch

from infra_brain.db.models import Resource, ZONE_CORPORATE

from tests.support.pg import make_engine


@pytest.fixture(autouse=True)
def _enable_mcp_mutations(monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "1")


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def session_cm(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    return _get_session


@pytest.fixture
def http_client(engine, session_cm, monkeypatch):
    """Not an auth test — this client exercises HTTP-vs-MCP write-path
    convergence, so it needs the explicit dev-mode bypass (item 1.5b made
    dev-mode require INFRA_BRAIN_DEV=1 rather than "no UI_COOKIE_SECRET set").
    """
    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(resources_router)
    with patch("infra_brain.api.routers.hosts.get_session", session_cm):
        yield TestClient(app)


def test_upsert_resource_module_exists():
    """The canonical helper must live in the new module."""
    from infra_brain.api._seeding import upsert_resource  # noqa: F401


def test_http_and_mcp_seed_paths_produce_identical_rows(engine, session_cm, http_client):
    """Driving the HTTP /resources seed and the MCP seed_resource tool with the
    same inputs must produce exactly ONE row with identical columns + metadata_,
    regardless of which path ran first."""
    payload = {
        "hostname": "converge-host-01",
        "domain": "manual",
        "resource_type": "host",
        "ip_address": "10.9.9.9",
        "os_name": "Ubuntu",
        "environment": "production",
        "source": "manual",
    }

    resp = http_client.post("/api/dashboard/resources", json=payload)
    assert resp.status_code == 200

    # Now drive the MCP path with the SAME inputs against the SAME engine.
    with patch("infra_brain.mcp_server.get_session", session_cm):
        from infra_brain.mcp_server import seed_resource

        mcp_result = seed_resource(
            hostname=payload["hostname"],
            domain=payload["domain"],
            resource_type=payload["resource_type"],
            ip_address=payload["ip_address"],
            os_name=payload["os_name"],
            environment=payload["environment"],
            source=payload["source"],
        )
    assert "error" not in mcp_result

    with Session(engine) as s:
        rows = (
            s.query(Resource)
            .filter(Resource.name == "converge-host-01", Resource.domain == "manual")
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.type == "host"
        assert row.zone == ZONE_CORPORATE
        assert row.metadata_["ip_address"] == "10.9.9.9"
        assert row.metadata_["os_name"] == "Ubuntu"
        assert row.metadata_["environment"] == "production"


def test_upsert_resource_merges_metadata_on_repeat_calls(engine):
    from infra_brain.api._seeding import upsert_resource

    with Session(engine) as s:
        upsert_resource(
            s,
            name="merge-host",
            domain="manual",
            resource_type="host",
            metadata={"alpha": 1},
        )
        s.commit()

    with Session(engine) as s:
        upsert_resource(
            s,
            name="merge-host",
            domain="manual",
            resource_type="host",
            metadata={"beta": 2},
        )
        s.commit()

    with Session(engine) as s:
        row = (
            s.query(Resource)
            .filter(Resource.name == "merge-host", Resource.domain == "manual")
            .one()
        )
        # Both keys must survive — merge, not overwrite.
        assert row.metadata_["alpha"] == 1
        assert row.metadata_["beta"] == 2


def test_upsert_resource_enforces_zone_default(engine):
    from infra_brain.api._seeding import upsert_resource

    with Session(engine) as s:
        resource = upsert_resource(
            s,
            name="zone-default-host",
            domain="manual",
            resource_type="host",
        )
        s.commit()
        assert resource.zone == ZONE_CORPORATE

    with Session(engine) as s:
        row = (
            s.query(Resource)
            .filter(Resource.name == "zone-default-host", Resource.domain == "manual")
            .one()
        )
        assert row.zone == ZONE_CORPORATE


def test_add_eol_product_routes_through_upsert_resource(engine, session_cm):
    """Task 4.4 review fix (CRITICAL 1): the MCP ``add_eol_product`` tool must
    create its backing Resource row via ``upsert_resource`` — canonical key
    ``(domain="eol", resource_type="product", name=asset_name)`` and
    ``zone == ZONE_CORPORATE`` — not a direct ``Resource(...)`` construction.
    """
    with patch("infra_brain.mcp_server.get_session", session_cm):
        from infra_brain.mcp_server import add_eol_product

        result = add_eol_product(asset_name="widget-os", eol_date="2027-01-01")

    assert "error" not in result
    assert "registered" in result

    with Session(engine) as s:
        row = s.query(Resource).filter_by(domain="eol", type="product", name="widget-os").one()
        assert row.zone == ZONE_CORPORATE
        assert row.source == "mcp"


def test_upsert_resource_canonical_key_includes_resource_type(engine):
    """Two rows with the same (domain, name) but different resource_type must
    remain DISTINCT rows — the canonical key is (domain, resource_type, name)."""
    from infra_brain.api._seeding import upsert_resource

    with Session(engine) as s:
        upsert_resource(s, name="dual-typed", domain="manual", resource_type="host")
        upsert_resource(s, name="dual-typed", domain="manual", resource_type="vm")
        s.commit()

    with Session(engine) as s:
        rows = (
            s.query(Resource)
            .filter(Resource.name == "dual-typed", Resource.domain == "manual")
            .all()
        )
        assert len(rows) == 2
        assert {r.type for r in rows} == {"host", "vm"}
