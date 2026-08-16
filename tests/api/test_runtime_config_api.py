"""RuntimeConfig CRUD dashboard routes (TRK-303 part 3/7).

Mirrors tests/test_mcp_keys_api.py's fixture patterns: a dev-mode `client`
fixture (require_session/require_admin both open) plus a real-auth fixture
with seeded admin/viewer UIUser rows exercising the 403-for-non-admin path.
"""

from contextlib import contextmanager

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import UIUser

from tests.support.pg import make_engine


@pytest.fixture
def client(monkeypatch):
    # Dev-mode opens require_session/require_admin so we can exercise the
    # routes without a signed cookie (dashboard_auth._dev_mode()).
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    from infra_brain.config import get_settings

    get_settings.cache_clear()

    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    import infra_brain.api.routers.runtime_config as mod

    monkeypatch.setattr(mod, "get_session", _get_session)

    app = FastAPI()
    app.include_router(mod.runtime_config_router)
    return TestClient(app)


class _FakeAuthRedis:
    """Minimal stateful stand-in for the Redis session-revocation check
    (mirrors tests/test_mcp_keys_api.py's _FakeAuthRedis)."""

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
        return len(keys)


@pytest.fixture
def admin_gated_app(monkeypatch):
    """A real (non-dev-mode) auth stack: cookie secret set, require_session/
    require_admin actually enforced. Seeds one admin and one non-admin
    (viewer) ui_users row so we can log in as each and assert access level."""
    monkeypatch.delenv("INFRA_BRAIN_DEV", raising=False)
    monkeypatch.setenv("UI_COOKIE_SECRET", "unit-test-secret")
    from infra_brain.config import get_settings

    get_settings.cache_clear()

    eng = make_engine()
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

    import infra_brain.api.routers.runtime_config as rc_mod

    import infra_brain.dashboard_auth as auth_mod

    monkeypatch.setattr(rc_mod, "get_session", _get_session)
    monkeypatch.setattr(auth_mod, "get_session", _get_session)
    monkeypatch.setattr(auth_mod, "get_redis", lambda: _FakeAuthRedis())

    app = FastAPI()
    app.include_router(auth_mod.auth_router)
    app.include_router(rc_mod.runtime_config_router)
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def admin_client(admin_gated_app):
    admin_gated_app.post(
        "/api/dashboard/login", json={"username": "admin-user", "password": "pw"}
    )
    return admin_gated_app


@pytest.fixture
def session_client(admin_gated_app):
    """Logged-in but non-admin (viewer role) — session-gated reads succeed,
    admin-gated writes must 403."""
    admin_gated_app.post(
        "/api/dashboard/login", json={"username": "viewer-user", "password": "pw"}
    )
    return admin_gated_app


def test_put_then_get_runtime_config(admin_client):
    resp = admin_client.put(
        "/api/dashboard/runtime-config/netdiscovery_tier2_chunk_size",
        json={"value": "750", "value_type": "int", "category": "tuning"},
    )
    assert resp.status_code == 200
    resp = admin_client.get("/api/dashboard/runtime-config")
    row = next(
        r for r in resp.json()["items"] if r["key"] == "netdiscovery_tier2_chunk_size"
    )
    assert row["value"] == "750"
    assert row["is_secret"] is False


def test_delete_reverts_to_default(admin_client):
    admin_client.put(
        "/api/dashboard/runtime-config/netdiscovery_tier2_chunk_size",
        json={"value": "750", "value_type": "int"},
    )
    resp = admin_client.delete(
        "/api/dashboard/runtime-config/netdiscovery_tier2_chunk_size"
    )
    assert resp.status_code == 204
    resp = admin_client.get("/api/dashboard/runtime-config")
    assert not any(
        r["key"] == "netdiscovery_tier2_chunk_size" for r in resp.json()["items"]
    )


def test_delete_unknown_key_is_404(admin_client):
    resp = admin_client.delete("/api/dashboard/runtime-config/does_not_exist")
    assert resp.status_code == 404


def test_non_admin_cannot_write(session_client):
    resp = session_client.put(
        "/api/dashboard/runtime-config/some_key",
        json={"value": "1", "value_type": "int"},
    )
    assert resp.status_code == 403


def test_non_admin_cannot_delete(session_client):
    resp = session_client.delete("/api/dashboard/runtime-config/some_key")
    assert resp.status_code == 403


def test_non_admin_can_read(session_client):
    resp = session_client.get("/api/dashboard/runtime-config")
    assert resp.status_code == 200


def test_put_rejects_bad_value_type(client):
    resp = client.put(
        "/api/dashboard/runtime-config/some_key",
        json={"value": "1", "value_type": "not_a_type"},
    )
    assert resp.status_code == 422


def test_put_rejects_empty_value(client):
    resp = client.put(
        "/api/dashboard/runtime-config/some_key",
        json={"value": "", "value_type": "str"},
    )
    assert resp.status_code == 422


def test_put_clears_settings_cache(client, monkeypatch):
    """A write must invalidate get_settings' cache so the TTL resolver picks
    up the new override rather than serving a stale cached value."""
    from infra_brain.config import get_settings

    calls = {"n": 0}
    real_clear = get_settings.cache_clear

    def _tracking_clear():
        calls["n"] += 1
        real_clear()

    monkeypatch.setattr(get_settings, "cache_clear", _tracking_clear)

    client.put(
        "/api/dashboard/runtime-config/some_key",
        json={"value": "1", "value_type": "int"},
    )
    assert calls["n"] >= 1
