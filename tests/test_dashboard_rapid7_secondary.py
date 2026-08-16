"""Tests for the Rapid7 secondary-views endpoints (UI Batch 5 / GitLab #61):
per-asset config/users/addresses detail, and the site/tag browsers.

Mirrors test_dashboard_fleet_software_cve.py's conventions: in-memory SQLite,
ORM schema via Base.metadata.create_all, get_session patched to the test
engine, auth-off via INFRA_BRAIN_DEV=1.
"""

import uuid
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    Base,
    R7Asset,
    R7AssetAddress,
    R7AssetConfig,
    R7AssetUser,
    R7Site,
    R7Tag,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
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

    from infra_brain.api.routers.fleet import fleet_router
    from unittest.mock import patch

    app = FastAPI()
    app.include_router(fleet_router)
    with patch("infra_brain.api.routers.fleet.get_session", _get_session):
        yield TestClient(app)


def _seed(engine):
    with Session(engine) as s:
        a = R7Asset(r7_asset_id=501, hostname="prod-db-01", ip="10.0.0.20")
        s.add(a)
        s.flush()
        s.add(R7AssetConfig(asset_id=a.id, name="cpu.vendor", value="Intel"))
        s.add(R7AssetConfig(asset_id=a.id, name="memory.size", value="32GB"))
        s.add(R7AssetUser(asset_id=a.id, username="root", full_name="Superuser"))
        s.add(R7AssetAddress(asset_id=a.id, ip="10.0.0.20", mac="aa:bb:cc:dd:ee:ff"))
        s.add(R7Site(r7_site_id=9001, name="Datacenter East", asset_count=1, risk_score=1200.0))
        s.add(R7Tag(r7_tag_id=7001, name="PCI Scope", tag_type="custom"))
        s.commit()
        return str(a.id)


def test_asset_detail_returns_config_users_addresses(client, engine):
    asset_id = _seed(engine)
    resp = client.get(f"/api/dashboard/fleet/{asset_id}/detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["hostname"] == "prod-db-01"
    assert {c["name"]: c["value"] for c in body["configs"]} == {
        "cpu.vendor": "Intel",
        "memory.size": "32GB",
    }
    assert body["users"][0]["username"] == "root"
    assert body["addresses"][0]["mac"] == "aa:bb:cc:dd:ee:ff"


def test_asset_detail_404_for_unknown_asset(client, engine):
    _seed(engine)
    resp = client.get(f"/api/dashboard/fleet/{uuid.uuid4()}/detail")
    assert resp.status_code == 404


def test_asset_detail_empty_children_for_asset_with_no_config(client, engine):
    with Session(engine) as s:
        a = R7Asset(r7_asset_id=502, hostname="bare-asset")
        s.add(a)
        s.commit()
        asset_id = str(a.id)
    resp = client.get(f"/api/dashboard/fleet/{asset_id}/detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configs"] == []
    assert body["users"] == []
    assert body["addresses"] == []


def test_sites_endpoint_lists_seeded_site(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/sites")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Datacenter East"


def test_sites_endpoint_empty_when_none_collected(client, engine):
    Base.metadata.create_all(engine)  # tables exist, no rows
    resp = client.get("/api/dashboard/sites")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}


def test_tags_endpoint_lists_seeded_tag(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/tags")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "PCI Scope"
