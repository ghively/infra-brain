"""Tests for /api/dashboard/views CRUD endpoints."""

import os
from contextlib import contextmanager
from unittest.mock import patch

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
    """Client in explicit dev mode — require_session passes, current_user returns None.

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

    from infra_brain.api.routers.ui import ui_public_router, ui_router
    from infra_brain.dashboard_api import router

    app = FastAPI()
    app.include_router(router)
    app.include_router(ui_router)
    # TRK-306: the public share route lives on its own un-gated router now;
    # without this the GET /views/{share_token} route is absent from the test
    # app entirely and PATCH/DELETE on the same path answer with 405.
    app.include_router(ui_public_router)

    with (
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.ui.get_session", _get_session),
    ):
        yield TestClient(app)


@pytest.fixture
def client(engine):
    """Client with auth enforced (UI_COOKIE_SECRET set) — unauthenticated requests get 401."""

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.ui import ui_public_router, ui_router
    from infra_brain.dashboard_api import router
    from infra_brain.dashboard_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(ui_router)
    # TRK-306: the public share route lives on its own un-gated router now;
    # without this the GET /views/{share_token} route is absent from the test
    # app entirely and PATCH/DELETE on the same path answer with 405.
    app.include_router(ui_public_router)

    with (
        patch.dict(os.environ, {"UI_COOKIE_SECRET": "unit-test-secret"}),
        patch("infra_brain.dashboard_auth.get_session", _get_session),
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.ui.get_session", _get_session),
    ):
        yield TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def shared_clients():
    """Returns (signed_client, anon_client) sharing the same in-memory SQLite engine.

    signed_client  — sends a valid signed session cookie; current_user() returns the user.
    anon_client    — sends no cookie; current_user() returns None → 401 on private routes.

    Both apps use UI_COOKIE_SECRET so require_session enforces auth on all routes.
    The signed_client has a pre-signed cookie that passes the auth check.
    """
    import uuid as _uuid

    from itsdangerous import URLSafeTimedSerializer

    from infra_brain.db.models import UIUser

    _SECRET = "unit-test-secret"
    _COOKIE = "infra_brain_session"

    eng = make_engine()

    # Seed a UIUser so _get_user_id can resolve the username to a UUID.
    user_id = _uuid.uuid4()
    with Session(eng) as s:
        s.add(
            UIUser(
                id=user_id,
                username="testuser",
                password_hash="$2b$12$unused",
                name="Test User",
                role="admin",
            )
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    from infra_brain.api.routers.ui import ui_public_router, ui_router
    from infra_brain.dashboard_api import router
    from infra_brain.dashboard_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(ui_router)
    # TRK-306: the public share route lives on its own un-gated router now;
    # without this the GET /views/{share_token} route is absent from the test
    # app entirely and PATCH/DELETE on the same path answer with 405.
    app.include_router(ui_public_router)

    with (
        patch.dict(os.environ, {"UI_COOKIE_SECRET": _SECRET}),
        patch("infra_brain.dashboard_auth.get_session", _get_session),
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.ui.get_session", _get_session),
    ):
        # Sign a valid session cookie using the SAME salt derivation the server
        # uses (item 1.5b bound the itsdangerous salt to a fingerprint of the
        # configured secret — see dashboard_auth._salt — so a cookie signed
        # under the old constant salt would now be rejected). Compute it via
        # the real _salt() so this fixture tracks the server's scheme.
        from infra_brain.dashboard_auth import _salt

        _serializer = URLSafeTimedSerializer(_SECRET, salt=_salt())
        _token = _serializer.dumps({"username": "testuser", "name": "Test User", "role": "admin"})

        signed_client = TestClient(app, cookies={_COOKIE: _token}, raise_server_exceptions=True)
        anon_client = TestClient(app, raise_server_exceptions=True)
        yield signed_client, anon_client


# ─────────────────────────────────────────────────────────────────────────────
# Auth gate
# ─────────────────────────────────────────────────────────────────────────────


def test_create_view_requires_auth(client):
    resp = client.post(
        "/api/dashboard/views",
        json={"title": "T", "prompt": "P", "openui_lang": "C"},
    )
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# CRUD happy-paths (dev mode: user_id == None throughout)
# ─────────────────────────────────────────────────────────────────────────────


def test_create_view_success(auth_client, db_session):
    resp = auth_client.post(
        "/api/dashboard/views",
        json={
            "title": "My View",
            "prompt": "show fleet stats",
            "openui_lang": "<FleetStatCard title='x' value={1} />",
            "is_public": True,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "My View"
    assert "share_token" in data
    assert data["share_url"] == f"/dashboard2/views/{data['share_token']}"


def test_list_views_own_only(auth_client, db_session):
    auth_client.post(
        "/api/dashboard/views",
        json={"title": "V1", "prompt": "p", "openui_lang": "c"},
    )
    auth_client.post(
        "/api/dashboard/views",
        json={"title": "V2", "prompt": "p", "openui_lang": "c"},
    )
    resp = auth_client.get("/api/dashboard/views")
    assert resp.status_code == 200
    # Task 5.3: /views now returns CustomViewPageOut envelope {items, total, ...}
    body = resp.json()
    assert isinstance(body, dict)
    data = body["items"]
    assert len(data) >= 2


def test_get_public_view_with_auth(shared_clients):
    """Public view accessible to any authenticated user (is_public bypasses ownership check)."""
    signed_client, _anon_client = shared_clients
    create_resp = signed_client.post(
        "/api/dashboard/views",
        json={"title": "Public View", "prompt": "p", "openui_lang": "c", "is_public": True},
    )
    assert create_resp.status_code == 201
    token = create_resp.json()["share_token"]
    # Authenticated fetch of a public view succeeds (no ownership required)
    resp = signed_client.get(f"/api/dashboard/views/{token}")
    assert resp.status_code == 200


def test_get_private_view_no_auth_401(shared_clients):
    """Private view returns 401 when the caller has no session cookie."""
    signed_client, anon_client = shared_clients
    create_resp = signed_client.post(
        "/api/dashboard/views",
        json={"title": "Private View", "prompt": "p", "openui_lang": "c", "is_public": False},
    )
    assert create_resp.status_code == 201
    token = create_resp.json()["share_token"]
    resp = anon_client.get(f"/api/dashboard/views/{token}")
    assert resp.status_code == 401


def test_delete_view_owner(auth_client, db_session):
    create_resp = auth_client.post(
        "/api/dashboard/views",
        json={"title": "Del", "prompt": "p", "openui_lang": "c"},
    )
    assert create_resp.status_code == 201
    view_id = create_resp.json()["id"]
    resp = auth_client.delete(f"/api/dashboard/views/{view_id}")
    assert resp.status_code == 204


def test_patch_view_title(auth_client, db_session):
    create_resp = auth_client.post(
        "/api/dashboard/views",
        json={"title": "Old", "prompt": "p", "openui_lang": "c"},
    )
    assert create_resp.status_code == 201
    view_id = create_resp.json()["id"]
    resp = auth_client.patch(f"/api/dashboard/views/{view_id}", json={"title": "New"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "New"


def test_patch_view_toggle_public(auth_client, db_session):
    create_resp = auth_client.post(
        "/api/dashboard/views",
        json={"title": "T", "prompt": "p", "openui_lang": "c", "is_public": False},
    )
    assert create_resp.status_code == 201
    view_id = create_resp.json()["id"]
    resp = auth_client.patch(f"/api/dashboard/views/{view_id}", json={"is_public": True})
    assert resp.status_code == 200
    assert resp.json()["is_public"] is True
