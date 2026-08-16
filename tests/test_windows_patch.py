"""Tests for GET /api/dashboard/resources/{resource_id}/windows."""

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


@pytest.fixture
def client(engine):
    """Client with auth enforced (UI_COOKIE_SECRET set)."""

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from unittest.mock import patch

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
        yield TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


def test_windows_requires_auth(client):
    resp = client.get("/api/dashboard/resources/some-id/windows")
    assert resp.status_code == 401


def test_windows_404_missing(auth_client):
    resp = auth_client.get("/api/dashboard/resources/00000000-0000-0000-0000-000000000000/windows")
    assert resp.status_code == 404


def test_windows_success(auth_client, db_session):
    from datetime import datetime, timezone

    from infra_brain.db.models import Resource, WindowsPatchState

    rid = uuid.uuid4()
    r = Resource(id=rid, name="win-server-01", domain="windows", type="host", source="WindowsAgent")
    db_session.add(r)
    db_session.flush()
    ps = WindowsPatchState(
        id=uuid.uuid4(),
        resource_id=rid,
        hostname="win-server-01",
        kb_list=["KB5001234", "KB5001235"],
        pending_count=2,
        last_patched=datetime.now(timezone.utc),
        winrm_status="online",
    )
    db_session.add(ps)
    db_session.commit()
    resp = auth_client.get(f"/api/dashboard/resources/{rid}/windows")
    assert resp.status_code == 200
    data = resp.json()
    assert data["hostname"] == "win-server-01"
    assert len(data["kb_list"]) == 2
    assert data["winrm_status"] == "online"


def test_windows_null_last_patched(auth_client, db_session):
    from infra_brain.db.models import Resource, WindowsPatchState

    rid = uuid.uuid4()
    r = Resource(id=rid, name="win-bare", domain="windows", type="host", source="WindowsAgent")
    db_session.add(r)
    db_session.flush()
    ps = WindowsPatchState(
        id=uuid.uuid4(),
        resource_id=rid,
        hostname="win-bare",
        kb_list=[],
        pending_count=0,
        last_patched=None,
        winrm_status="unknown",
    )
    db_session.add(ps)
    db_session.commit()
    resp = auth_client.get(f"/api/dashboard/resources/{rid}/windows")
    assert resp.status_code == 200
    data = resp.json()
    assert data["last_patched"] is None
    assert data["kb_list"] == []
