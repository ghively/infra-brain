"""Tests for GET /api/dashboard/resources/{resource_id}/linux."""

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import LinuxHost, LinuxPackage, Resource

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def client(engine, monkeypatch):
    """Client with require_session bypassed via explicit dev-mode flag.

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


def test_linux_detail_404_missing_resource(client):
    resp = client.get("/api/dashboard/resources/00000000-0000-0000-0000-000000000000/linux")
    assert resp.status_code == 404


def test_linux_detail_404_non_linux_resource(client, engine):
    rid = uuid.uuid4()
    with Session(engine) as s:
        r = Resource(id=rid, domain="windows", type="host", name="win-box", source="test")
        s.add(r)
        s.commit()
    resp = client.get(f"/api/dashboard/resources/{rid}/linux")
    assert resp.status_code == 404


def test_linux_detail_success(client, engine):
    rid = uuid.uuid4()
    with Session(engine) as s:
        r = Resource(
            id=rid,
            domain="linux",
            type="host",
            name="lnx-01",
            source="LinuxAgent",
        )
        s.add(r)
        s.flush()
        lh = LinuxHost(
            id=uuid.uuid4(),
            resource_id=rid,
            distro="Ubuntu 22.04",
            kernel="5.15.0",
            arch="x86_64",
        )
        s.add(lh)
        s.flush()
        pkg = LinuxPackage(
            id=uuid.uuid4(),
            host_id=lh.id,
            name="openssl",
            version="3.0.2",
            manager="apt",
            installed_at=datetime.now(timezone.utc),
        )
        s.add(pkg)
        s.commit()
    resp = client.get(f"/api/dashboard/resources/{rid}/linux")
    assert resp.status_code == 200
    data = resp.json()
    assert data["hostname"] == "lnx-01"
    assert data["distro"] == "Ubuntu 22.04"
    assert len(data["packages"]) == 1
    assert data["packages"][0]["name"] == "openssl"


def test_linux_detail_empty_arrays(client, engine):
    rid = uuid.uuid4()
    with Session(engine) as s:
        r = Resource(
            id=rid,
            domain="linux",
            type="host",
            name="lnx-empty",
            source="LinuxAgent",
        )
        s.add(r)
        s.flush()
        lh = LinuxHost(
            id=uuid.uuid4(),
            resource_id=rid,
            distro="",
            kernel="",
            arch="",
        )
        s.add(lh)
        s.commit()
    resp = client.get(f"/api/dashboard/resources/{rid}/linux")
    assert resp.status_code == 200
    data = resp.json()
    assert data["packages"] == []
    assert data["services"] == []
    assert data["ports"] == []
    assert data["crons"] == []


def test_linux_detail_requires_auth(engine):
    """With UI_COOKIE_SECRET set, unauthenticated requests return 401."""
    import os
    from contextlib import contextmanager
    from unittest.mock import patch

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.dashboard_api import router
    from infra_brain.dashboard_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(resources_router)
    with (
        patch.dict(os.environ, {"UI_COOKIE_SECRET": "unit-test-secret"}),
        patch("infra_brain.dashboard_auth.get_session", _get_session),
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
    ):
        tc = TestClient(app, raise_server_exceptions=True)
        resp = tc.get("/api/dashboard/resources/some-id/linux")
        assert resp.status_code == 401
