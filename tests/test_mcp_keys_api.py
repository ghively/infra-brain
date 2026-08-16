"""MCP key-management dashboard routes (session/admin gated; dev-mode open)."""
from contextlib import contextmanager

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import UIUser

from tests.support.pg import make_engine


class _FakeAuthRedis:
    """Minimal stateful stand-in for the Redis session-revocation check
    (mirrors tests/test_dashboard_auth.py's _FakeAuthRedis) — current_user()
    calls exists() on login/logout, and require_admin needs a working
    revocation store to accept a freshly-issued session cookie."""

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
def client(monkeypatch):
    # Dev-mode opens require_session/require_admin so we can exercise the routes
    # without a signed cookie (dashboard_auth._dev_mode()).
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    from infra_brain.config import get_settings

    get_settings.cache_clear()

    eng = make_engine()

    from contextlib import contextmanager

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    import infra_brain.api.routers.mcp_keys as mod

    monkeypatch.setattr(mod, "get_session", _get_session)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(mod.mcp_keys_router)
    return TestClient(app)


def test_tools_catalog_lists_readonly_and_mutation(client):
    r = client.get("/api/dashboard/mcp-keys/tools")
    assert r.status_code == 200
    body = r.json()
    assert "query_resources" in body["readonly"]
    assert "seed_resource" in body["mutation"]


def test_create_then_list_then_revoke(client):
    created = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "ops-a", "allowed_tools": ["query_resources"]},
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["token"].startswith("ibmcp_")  # shown once
    key_id = payload["id"]

    listed = client.get("/api/dashboard/mcp-keys")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert any(k["id"] == key_id and k["revoked"] is False for k in items)
    # The raw token is never returned by the list view.
    assert all("token" not in k for k in items)

    revoked = client.post(f"/api/dashboard/mcp-keys/{key_id}/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True

    listed2 = client.get("/api/dashboard/mcp-keys")
    assert any(k["id"] == key_id and k["revoked"] is True for k in listed2.json()["items"])


def test_revoke_unknown_id_is_404(client):
    import uuid

    r = client.post(f"/api/dashboard/mcp-keys/{uuid.uuid4()}/revoke")
    assert r.status_code == 404


def test_create_rejects_overlong_name(client):
    """Fix 2: DB column is String(128) — an overlong name must be a clean 422,
    not an unhandled DataError/500."""
    r = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "x" * 129, "allowed_tools": ["query_resources"]},
    )
    assert r.status_code == 422


def test_create_rejects_empty_name(client):
    r = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "", "allowed_tools": ["query_resources"]},
    )
    assert r.status_code == 422


def test_create_rejects_unknown_tool_name(client):
    """Fix 2: allowed_tools entries must be real tool names from
    infra_brain.mcp_auth.ALL_TOOL_NAMES — a typo should 422, not silently persist."""
    r = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "ops-b", "allowed_tools": ["not_a_real_tool"]},
    )
    assert r.status_code == 422


def test_create_accepts_valid_name_and_tools(client):
    """Regression: a well-formed request (real tool names, in-bounds name) still
    succeeds after adding the Fix 2 validators."""
    r = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "ops-c", "allowed_tools": ["query_resources", "seed_resource"]},
    )
    assert r.status_code == 200
    assert r.json()["allowed_tools"] == ["query_resources", "seed_resource"]


# ---------------------------------------------------------------------------
# TRK-160: optional key expiry over the dashboard API.
# ---------------------------------------------------------------------------


def test_create_without_expires_days_never_expires(client):
    """The default and the back-compat guarantee: omitting expires_days keeps
    the pre-TRK-160 behavior exactly — expires_at is null and the list view
    reports the key as not expired."""
    r = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "no-expiry", "allowed_tools": ["query_resources"]},
    )
    assert r.status_code == 200
    assert r.json()["expires_at"] is None

    item = next(k for k in client.get("/api/dashboard/mcp-keys").json()["items"] if k["name"] == "no-expiry")
    assert item["expires_at"] is None
    assert item["expired"] is False
    assert item["revoked"] is False


def test_create_with_expires_days_returns_computed_expires_at(client):
    r = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "expiring", "allowed_tools": ["query_resources"], "expires_days": 30},
    )
    assert r.status_code == 200
    expires_at = r.json()["expires_at"]
    assert expires_at is not None

    # ~30 days out, not the raw day count echoed back.
    from datetime import UTC, datetime, timedelta

    parsed = datetime.fromisoformat(expires_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = parsed - datetime.now(UTC)
    assert timedelta(days=29) < delta < timedelta(days=31)

    item = next(k for k in client.get("/api/dashboard/mcp-keys").json()["items"] if k["name"] == "expiring")
    assert item["expires_at"] is not None
    assert item["expired"] is False  # not yet


@pytest.mark.parametrize("bad", [0, -1, 3651, 100000])
def test_create_rejects_out_of_range_expires_days(client, bad):
    """Positive int with a reasonable ceiling (mcp_auth.MAX_EXPIRES_DAYS=3650);
    anything else is a clean 422, never a key that is dead on arrival."""
    r = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": f"bad-{bad}", "allowed_tools": ["query_resources"], "expires_days": bad},
    )
    assert r.status_code == 422


def test_create_accepts_expires_days_at_the_ceiling(client):
    from infra_brain import mcp_auth

    r = client.post(
        "/api/dashboard/mcp-keys",
        json={
            "name": "max-expiry",
            "allowed_tools": ["query_resources"],
            "expires_days": mcp_auth.MAX_EXPIRES_DAYS,
        },
    )
    assert r.status_code == 200
    assert r.json()["expires_at"] is not None


def test_list_reports_expired_distinctly_from_revoked(client):
    """An expired key and a revoked key both deny, but the list must say WHICH
    — 'expired' is the clock, 'revoked' is somebody's deliberate act, and an
    operator diagnosing a broken integration needs to tell them apart."""
    from datetime import UTC, datetime, timedelta

    from infra_brain.db.models import McpApiKey
    import infra_brain.api.routers.mcp_keys as mod

    created = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "gone-stale", "allowed_tools": ["query_resources"], "expires_days": 1},
    )
    key_id = created.json()["id"]

    # Backdate it past its expiry through the same session factory the routes use.
    import uuid as _uuid

    with mod.get_session() as s:
        row = s.get(McpApiKey, _uuid.UUID(key_id))
        row.expires_at = datetime.now(UTC) - timedelta(hours=1)
        s.commit()

    item = next(k for k in client.get("/api/dashboard/mcp-keys").json()["items"] if k["id"] == key_id)
    assert item["expired"] is True
    assert item["revoked"] is False  # nobody revoked it; it simply ran out


# ---------------------------------------------------------------------------
# Fix 1: GET /mcp-keys must be admin-gated, same as POST / and POST /revoke —
# a non-admin logged-in user must NOT be able to enumerate MCP API key metadata.
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_gated_app(monkeypatch):
    """A real (non-dev-mode) auth stack: cookie secret set, require_session/
    require_admin actually enforced. Seeds one admin and one non-admin (viewer)
    ui_users row so we can log in as each and assert the resulting access level."""
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

    import infra_brain.api.routers.mcp_keys as mcp_keys_mod
    import infra_brain.dashboard_auth as auth_mod

    monkeypatch.setattr(mcp_keys_mod, "get_session", _get_session)
    monkeypatch.setattr(auth_mod, "get_session", _get_session)
    monkeypatch.setattr(auth_mod, "get_redis", lambda: _FakeAuthRedis())

    app = FastAPI()
    app.include_router(auth_mod.auth_router)
    app.include_router(mcp_keys_mod.mcp_keys_router)
    return TestClient(app, base_url="https://testserver")


def test_list_mcp_keys_requires_admin_403_for_viewer(admin_gated_app):
    client = admin_gated_app
    client.post("/api/dashboard/login", json={"username": "viewer-user", "password": "pw"})
    r = client.get("/api/dashboard/mcp-keys")
    assert r.status_code == 403


def test_list_mcp_keys_200_for_admin(admin_gated_app):
    """Regression: admins keep read access to the key-listing endpoint."""
    client = admin_gated_app
    client.post("/api/dashboard/login", json={"username": "admin-user", "password": "pw"})
    r = client.get("/api/dashboard/mcp-keys")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body


# ---------------------------------------------------------------------------
# PATCH /{key_id} — amend an existing key's allowed_tools (implementation
# plan section 4.4).
# ---------------------------------------------------------------------------


def test_patch_amends_allowed_tools_and_echoes_new_scope(client):
    created = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "ops-patch", "allowed_tools": ["query_resources"]},
    )
    assert created.status_code == 200
    key_id = created.json()["id"]

    patched = client.patch(
        f"/api/dashboard/mcp-keys/{key_id}",
        json={"allowed_tools": ["query_resources", "seed_resource"]},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["id"] == key_id
    assert sorted(body["allowed_tools"]) == sorted(["query_resources", "seed_resource"])

    # Token hash / issuance is untouched — list view still shows the key active
    # and the token is never re-exposed.
    listed = client.get("/api/dashboard/mcp-keys")
    item = next(k for k in listed.json()["items"] if k["id"] == key_id)
    assert item["revoked"] is False
    assert item["allowed_tools_count"] == 2


def test_patch_rejects_unknown_tool_name(client):
    created = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "ops-patch-bad", "allowed_tools": ["query_resources"]},
    )
    key_id = created.json()["id"]

    r = client.patch(
        f"/api/dashboard/mcp-keys/{key_id}",
        json={"allowed_tools": ["not_a_real_tool"]},
    )
    assert r.status_code == 422


def test_patch_rejects_revoked_key(client):
    created = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "ops-patch-revoked", "allowed_tools": ["query_resources"]},
    )
    key_id = created.json()["id"]
    revoked = client.post(f"/api/dashboard/mcp-keys/{key_id}/revoke")
    assert revoked.status_code == 200

    r = client.patch(
        f"/api/dashboard/mcp-keys/{key_id}",
        json={"allowed_tools": ["seed_resource"]},
    )
    assert r.status_code == 409


def test_patch_unknown_id_is_404(client):
    import uuid

    r = client.patch(
        f"/api/dashboard/mcp-keys/{uuid.uuid4()}",
        json={"allowed_tools": ["query_resources"]},
    )
    assert r.status_code == 404


def test_patch_logs_warning_with_amendment_details(client, caplog):
    created = client.post(
        "/api/dashboard/mcp-keys",
        json={"name": "ops-patch-log", "allowed_tools": ["query_resources"]},
    )
    key_id = created.json()["id"]

    with caplog.at_level("WARNING", logger="infra_brain.api.routers.mcp_keys"):
        r = client.patch(
            f"/api/dashboard/mcp-keys/{key_id}",
            json={"allowed_tools": ["query_resources", "seed_resource"]},
        )
    assert r.status_code == 200
    warnings = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert any(
        "MCP key amended" in rec.message
        and key_id in rec.message
        and "before_tools=1" in rec.message
        and "after_tools=2" in rec.message
        for rec in warnings
    )
