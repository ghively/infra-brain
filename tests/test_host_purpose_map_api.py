"""Tests for GET /api/dashboard/host-purpose-map (MR-J item 3)."""

import os
import uuid
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def auth_client(engine, monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from unittest.mock import patch

    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.dashboard_api import router

    app = FastAPI()
    app.include_router(router)
    app.include_router(resources_router)

    with (
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
    ):
        yield TestClient(app)


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


def test_host_purpose_map_empty_returns_empty_list(auth_client):
    resp = auth_client.get("/api/dashboard/host-purpose-map")
    assert resp.status_code == 200
    assert resp.json() == []


def test_host_purpose_map_returns_rows_sorted_by_hostname(auth_client, db_session):
    from infra_brain.db.models import HostPurposeMap

    db_session.add(
        HostPurposeMap(
            id=uuid.uuid4(),
            hostname="web-01",
            purpose="Primary web frontend",
            vlan=None,
            subnet=None,
            source="5:playbooks/system_inventory.yml",
        )
    )
    db_session.add(
        HostPurposeMap(
            id=uuid.uuid4(),
            hostname="db-01",
            purpose="Primary database",
            vlan="VLAN20-App",
            subnet="10.90.12.0/24",
            source="5:playbooks/system_inventory.yml",
        )
    )
    db_session.commit()

    resp = auth_client.get("/api/dashboard/host-purpose-map")
    assert resp.status_code == 200
    data = resp.json()
    assert [r["hostname"] for r in data] == ["db-01", "web-01"]
    assert data[0]["vlan"] == "VLAN20-App"
    assert data[1]["vlan"] is None


def test_host_purpose_map_requires_auth():
    """No INFRA_BRAIN_DEV / no auth bypass -> 401, matching every other
    dashboard endpoint's auth contract."""
    os.environ.pop("INFRA_BRAIN_DEV", None)
    eng = make_engine()

    from unittest.mock import patch

    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.dashboard_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(resources_router)

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    with (
        patch.dict(os.environ, {"UI_COOKIE_SECRET": "unit-test-secret"}),
        patch("infra_brain.dashboard_auth.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/dashboard/host-purpose-map")
    assert resp.status_code == 401
