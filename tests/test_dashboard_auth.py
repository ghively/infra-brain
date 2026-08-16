"""Tests for dashboard session-cookie auth (infra_brain.dashboard_auth)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import UIUser

from tests.support.pg import make_engine


class _FakeAuthRedis:
    """Stateful in-process stand-in for the Redis auth uses: revocation keys
    (set/exists) and the login-lockout sorted set (zadd/zremrangebyscore/zcard/
    expire/delete). One instance behaves like one Redis shared across requests.

    The test environment provisions no Redis (CI has only Postgres), so tests that
    exercise the authenticated path need a fake here — since TRK-096 the revocation
    check fails CLOSED, so a genuinely-absent Redis now (correctly) denies every
    session. Tests that want the Redis-DOWN path patch get_redis to raise instead.
    """

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.z: dict[str, dict[str, float]] = {}

    # revocation key ops
    def set(self, key, value, ex=None):
        self.kv[key] = value
        return True

    def exists(self, key):
        return 1 if key in self.kv else 0

    # login-lockout sorted-set ops
    def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zremrangebyscore(self, key, mn, mx):
        d = self.z.get(key, {})
        removed = [m for m, s in list(d.items()) if mn <= s <= mx]
        for m in removed:
            del d[m]
        return len(removed)

    def zcard(self, key):
        return len(self.z.get(key, {}))

    def expire(self, key, ttl):
        return True

    def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)
            self.z.pop(k, None)
        return len(keys)


@pytest.fixture
def engine():
    eng = make_engine()
    with Session(eng) as s:
        s.add(
            UIUser(
                username="alice",
                password_hash=bcrypt.hashpw(b"s3cret", bcrypt.gensalt()).decode(),
                name="Alice",
                role="admin",
                active=True,
            )
        )
        s.commit()
    return eng


@pytest.fixture
def app_client(engine, monkeypatch):
    """App with both routers, a secret set (auth enforced), seeded ui_users."""
    monkeypatch.setenv("UI_COOKIE_SECRET", "unit-test-secret")

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.dashboard_api import router as data_router
    from infra_brain.dashboard_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(data_router)
    app.include_router(resources_router)
    with (
        patch("infra_brain.dashboard_auth.get_session", _get_session),
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
        # Revocation now fails CLOSED (TRK-096): the authenticated path needs a
        # reachable Redis, so stand one in. Tests wanting Redis-DOWN re-patch this.
        patch("infra_brain.dashboard_auth.get_redis", return_value=_FakeAuthRedis()),
    ):
        # F-031: the login cookie now carries Secure. httpx (which TestClient
        # wraps) only resends Secure cookies over an https:// base_url — the
        # default plain-http "testserver" base silently drops them on the
        # client's next request, which looked like broken auth. Use an https
        # base_url so the cookie round-trips like it would in production.
        yield TestClient(app, base_url="https://testserver")


def test_protected_endpoint_401_without_cookie(app_client):
    resp = app_client.get("/api/dashboard/resources")
    assert resp.status_code == 401


def test_login_bad_credentials(app_client):
    resp = app_client.post("/api/dashboard/login", json={"username": "alice", "password": "wrong"})
    assert resp.status_code == 401


def test_login_sets_cookie_and_unlocks(app_client):
    resp = app_client.post("/api/dashboard/login", json={"username": "alice", "password": "s3cret"})
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True
    assert "infra_brain_session" in resp.cookies
    # TestClient keeps the cookie jar → subsequent protected call now works.
    ok = app_client.get("/api/dashboard/resources")
    assert ok.status_code == 200


def test_me_reflects_auth_state(app_client):
    assert app_client.get("/api/dashboard/me").json()["authenticated"] is False
    app_client.post("/api/dashboard/login", json={"username": "alice", "password": "s3cret"})
    me = app_client.get("/api/dashboard/me").json()
    assert me["authenticated"] is True
    assert me["username"] == "alice"
    assert me["name"] == "Alice"
    assert me["role"] == "admin"


def test_logout_clears_session(app_client):
    app_client.post("/api/dashboard/login", json={"username": "alice", "password": "s3cret"})
    assert app_client.get("/api/dashboard/resources").status_code == 200
    app_client.post("/api/dashboard/logout")
    assert app_client.get("/api/dashboard/resources").status_code == 401


def test_dev_mode_open_without_secret(engine, monkeypatch):
    """INFRA_BRAIN_DEV=1 → dashboard is open (local/test parity)."""
    from infra_brain.config import get_settings

    monkeypatch.delenv("UI_COOKIE_SECRET", raising=False)
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.dashboard_api import router as data_router

    app = FastAPI()
    app.include_router(data_router)
    app.include_router(resources_router)
    with (
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
    ):
        client = TestClient(app)
        assert client.get("/api/dashboard/resources").status_code == 200


def test_eol_migration_requires_admin(engine, monkeypatch):
    """F-039: a non-admin session must get 403 on PATCH /eol/{id}/migration."""
    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.dashboard_auth import auth_router
    from unittest.mock import patch

    monkeypatch.setenv("UI_COOKIE_SECRET", "unit-test-secret")

    # Seed a NON-admin viewer user.
    with Session(engine) as s:
        s.add(
            UIUser(
                username="viewer",
                password_hash=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
                name="Viewer",
                role="viewer",
                active=True,
            )
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.dashboard_api import router as data_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(data_router)
    app.include_router(resources_router)
    with (
        patch("infra_brain.dashboard_auth.get_session", _get_session),
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
        # TRK-096: revocation fails closed, so the authenticated PATCH needs Redis.
        patch("infra_brain.dashboard_auth.get_redis", return_value=_FakeAuthRedis()),
    ):
        client = TestClient(app, base_url="https://testserver")
        client.post("/api/dashboard/login", json={"username": "viewer", "password": "pw"})
        import uuid as _uuid

        resp = client.patch(
            f"/api/dashboard/eol/{_uuid.uuid4()}/migration",
            json={"migration_path": "upgrade to X"},
        )
        assert resp.status_code == 403


def test_non_dev_weak_secret_refuses_startup(monkeypatch):
    """F-027: create_app must raise in non-dev when UI_COOKIE_SECRET is weak/empty."""
    from infra_brain.config import get_settings
    import infra_brain.dashboard_auth as da

    monkeypatch.delenv("INFRA_BRAIN_DEV", raising=False)
    monkeypatch.setenv("UI_COOKIE_SECRET", "changeme")
    get_settings.cache_clear()
    try:
        import pytest as _pytest

        with _pytest.raises(RuntimeError, match="UI_COOKIE_SECRET"):
            da.assert_cookie_secret_ok()
    finally:
        get_settings.cache_clear()


def test_old_constant_salt_cookie_rejected(monkeypatch):
    """F-027: a cookie signed with the OLD fixed salt must fail to verify now."""
    from itsdangerous import URLSafeTimedSerializer
    import infra_brain.dashboard_auth as da

    monkeypatch.setenv("UI_COOKIE_SECRET", "unit-test-secret")
    # Forge a token the OLD way: same secret, old constant salt.
    old = URLSafeTimedSerializer("unit-test-secret", salt="infra-brain-dashboard-session")
    forged = old.dumps({"username": "attacker", "role": "admin"})

    class _Req:
        cookies = {da.COOKIE_NAME: forged}

    assert da.current_user(_Req()) is None


def test_login_cookie_is_secure(app_client):
    """F-031: the session cookie must carry the Secure attribute."""
    resp = app_client.post("/api/dashboard/login", json={"username": "alice", "password": "s3cret"})
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "Secure" in set_cookie


def test_login_cookie_not_secure_when_disabled(engine, monkeypatch):
    """When COOKIE_SECURE=false, Set-Cookie must NOT carry Secure (HTTP lab mode).

    This is the fix for the login bounce-loop: the dashboard is served over plain
    HTTP (:8001) so browsers silently drop Secure cookies, preventing session
    persistence. Setting COOKIE_SECURE=false via CI variable lets the lab stack
    work over HTTP without requiring TLS termination.
    """
    from infra_brain.config import get_settings
    from unittest.mock import patch

    monkeypatch.setenv("UI_COOKIE_SECRET", "unit-test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.dashboard_auth import auth_router
    from infra_brain.dashboard_api import router as data_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(data_router)
    try:
        with (
            patch("infra_brain.dashboard_auth.get_session", _get_session),
            patch("infra_brain.dashboard_api.get_session", _get_session),
        ):
            # Plain HTTP base_url — no TLS, matching the lab deployment.
            client = TestClient(app, base_url="http://testserver")
            resp = client.post(
                "/api/dashboard/login", json={"username": "alice", "password": "s3cret"}
            )
            assert resp.status_code == 200
            set_cookie = resp.headers.get("set-cookie", "")
            assert "Secure" not in set_cookie, (
                f"Secure flag must be absent when COOKIE_SECURE=false; got: {set_cookie!r}"
            )
            # Cookie must still round-trip over HTTP so the session persists.
            assert "infra_brain_session" in resp.cookies
    finally:
        get_settings.cache_clear()


class _FakeSortedSetRedis:
    """Stateful stand-in for the sorted-set ops the Redis-backed lockout uses
    (TRK-095). A shared instance behaves like one Redis seen by many processes."""

    def __init__(self):
        self.z: dict[str, dict[str, float]] = {}

    def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zremrangebyscore(self, key, mn, mx):
        d = self.z.get(key, {})
        removed = [m for m, s in list(d.items()) if mn <= s <= mx]
        for m in removed:
            del d[m]
        return len(removed)

    def zcard(self, key):
        return len(self.z.get(key, {}))

    def expire(self, key, ttl):
        return True

    def delete(self, *keys):
        for k in keys:
            self.z.pop(k, None)
        return len(keys)


def test_login_lockout_after_threshold(app_client, monkeypatch):
    """F-031: N failed logins from the same client lock further attempts with 429."""
    import infra_brain.dashboard_auth as da

    fake = _FakeSortedSetRedis()
    with patch.object(da, "get_redis", return_value=fake):
        # 5 failures (threshold) — each returns 401.
        for _ in range(da._LOCKOUT_THRESHOLD):
            r = app_client.post(
                "/api/dashboard/login", json={"username": "alice", "password": "wrong"}
            )
            assert r.status_code == 401
        # 6th attempt is locked out even with the CORRECT password.
        locked = app_client.post(
            "/api/dashboard/login", json={"username": "alice", "password": "s3cret"}
        )
    assert locked.status_code == 429


def test_lockout_state_is_shared_across_processes(monkeypatch):
    """TRK-095: lockout state lives in Redis, so two independent 'process' views
    of the lockout functions (sharing one Redis) observe the SAME counter — unlike
    the old per-process in-memory dict, where each process had a fresh, empty map."""
    import infra_brain.dashboard_auth as da

    shared = _FakeSortedSetRedis()

    class _Req:
        client = type("C", (), {"host": "10.0.0.9"})()

    key = da._login_key("alice", _Req())

    # "Process A" records the threshold's worth of failures.
    with patch.object(da, "get_redis", return_value=shared):
        for _ in range(da._LOCKOUT_THRESHOLD):
            da._record_failure(key)

    # "Process B" — a completely separate invocation — sees the SAME failures via
    # the shared Redis and reports locked out. (With the old in-memory dict this
    # would be False: process B would start from an empty _login_failures map.)
    with patch.object(da, "get_redis", return_value=shared):
        assert da._is_locked_out(key) is True

    # Clearing in one process is visible to the other.
    with patch.object(da, "get_redis", return_value=shared):
        da._clear_failures(key)
        assert da._is_locked_out(key) is False


def test_lockout_fails_open_when_redis_unavailable(monkeypatch):
    """TRK-095: a Redis outage must not deny all logins — lockout fails open."""
    import redis as _redis

    import infra_brain.dashboard_auth as da

    broken = MagicMock()
    broken.zremrangebyscore.side_effect = _redis.RedisError("down")
    with patch.object(da, "get_redis", return_value=broken):
        assert da._is_locked_out("infra_brain:login_lockout:alice:1.2.3.4") is False


# ── TRK-095: proxy-aware, anti-spoofing client-IP extraction ─────────────────


class _FakeHeaders:
    """Case-insensitive header mapping like Starlette Headers, minimal .get()."""

    def __init__(self, mapping=None):
        self._d = {k.lower(): v for k, v in (mapping or {}).items()}

    def get(self, name, default=None):
        return self._d.get(name.lower(), default)


def _fake_request(peer_host, xff=None):
    headers = _FakeHeaders({"x-forwarded-for": xff} if xff is not None else {})

    class _Req:
        client = type("C", (), {"host": peer_host})()

        def __init__(self):
            self.headers = headers

    return _Req()


def _set_trusted(monkeypatch, value):
    """Point da.get_settings at a settings stub carrying trusted_proxy_ips=value."""
    import infra_brain.dashboard_auth as da

    stub = type("S", (), {"trusted_proxy_ips": value})()
    monkeypatch.setattr(da, "get_settings", lambda: stub)


def test_lockout_ip_default_ignores_spoofed_xff(monkeypatch):
    """(a) No trusted proxies: a spoofed X-Forwarded-For is ignored; peer IP used."""
    import infra_brain.dashboard_auth as da

    _set_trusted(monkeypatch, "")
    key = da._login_key("alice", _fake_request("1.2.3.4", xff="9.9.9.9"))
    assert key == "infra_brain:login_lockout:alice:1.2.3.4"


def test_lockout_ip_trusted_peer_honors_xff(monkeypatch):
    """(b) Peer is a trusted proxy: its X-Forwarded-For is honored."""
    import infra_brain.dashboard_auth as da

    _set_trusted(monkeypatch, "1.2.3.4")
    key = da._login_key("alice", _fake_request("1.2.3.4", xff="5.6.7.8"))
    assert key == "infra_brain:login_lockout:alice:5.6.7.8"


def test_lockout_ip_peels_trusted_proxy_from_right(monkeypatch):
    """(c) Trusted proxy appended on the right of XFF is peeled; real client used."""
    import infra_brain.dashboard_auth as da

    # Both the immediate peer and the right-most XFF hop are trusted proxies.
    _set_trusted(monkeypatch, "1.2.3.4, 10.0.0.1")
    key = da._login_key("alice", _fake_request("1.2.3.4", xff="5.6.7.8, 10.0.0.1"))
    assert key == "infra_brain:login_lockout:alice:5.6.7.8"


def test_lockout_ip_untrusted_peer_ignores_xff(monkeypatch):
    """(d) Untrusted peer sending XFF: header ignored, peer IP used (anti-spoof)."""
    import infra_brain.dashboard_auth as da

    _set_trusted(monkeypatch, "10.0.0.1")  # a different, real proxy
    key = da._login_key("alice", _fake_request("1.2.3.4", xff="5.6.7.8"))
    assert key == "infra_brain:login_lockout:alice:1.2.3.4"


def test_lockout_ip_cidr_trust_and_malformed_xff_fallback(monkeypatch):
    """CIDR trust matches; a malformed XFF entry falls back safely to the peer IP."""
    import infra_brain.dashboard_auth as da

    _set_trusted(monkeypatch, "10.0.0.0/8")
    # Peer 10.5.6.7 is within the trusted CIDR; XFF has only a malformed token.
    key = da._login_key("bob", _fake_request("10.5.6.7", xff="not-an-ip"))
    assert key == "infra_brain:login_lockout:bob:10.5.6.7"


class _MultiHeaders:
    """Headers stub exposing getlist() like Starlette, for multiple XFF headers."""

    def __init__(self, xff_list):
        self._xff = list(xff_list)

    def getlist(self, name):
        return list(self._xff) if name.lower() == "x-forwarded-for" else []

    def get(self, name, default=None):
        vals = self.getlist(name)
        return vals[0] if vals else default


def _fake_request_multi(peer_host, xff_list):
    headers = _MultiHeaders(xff_list)

    class _Req:
        client = type("C", (), {"host": peer_host})()

        def __init__(self):
            self.headers = headers

    return _Req()


def test_lockout_ip_forged_then_real_chain_uses_real_client(monkeypatch):
    """Trusted peer, XFF='9.9.9.9, <untrusted-client>' → the untrusted client (right),
    NOT the attacker-forged leading 9.9.9.9, is used."""
    import infra_brain.dashboard_auth as da

    _set_trusted(monkeypatch, "1.2.3.4")
    key = da._login_key("alice", _fake_request("1.2.3.4", xff="9.9.9.9, 8.8.8.8"))
    assert key == "infra_brain:login_lockout:alice:8.8.8.8"


def test_lockout_ip_all_trusted_chain_falls_back_to_peer(monkeypatch):
    """Trusted peer with an XFF chain composed entirely of trusted proxies → peer IP."""
    import infra_brain.dashboard_auth as da

    _set_trusted(monkeypatch, "1.2.3.4, 10.0.0.1, 10.0.0.2")
    key = da._login_key("alice", _fake_request("1.2.3.4", xff="10.0.0.1, 10.0.0.2"))
    assert key == "infra_brain:login_lockout:alice:1.2.3.4"


def test_lockout_ip_no_client_is_unknown(monkeypatch):
    """request.client is None → the key uses the 'unknown' sentinel, never raises."""
    import infra_brain.dashboard_auth as da

    _set_trusted(monkeypatch, "")

    class _Req:
        client = None
        headers = _FakeHeaders({"x-forwarded-for": "9.9.9.9"})

    key = da._login_key("alice", _Req())
    assert key == "infra_brain:login_lockout:alice:unknown"


def test_lockout_ip_multiple_xff_headers_joined_before_peel(monkeypatch):
    """Multiple separate X-Forwarded-For headers from a trusted peer are concatenated
    left-to-right, so a spoofed leading header cannot win the attribution."""
    import infra_brain.dashboard_auth as da

    _set_trusted(monkeypatch, "1.2.3.4")
    # Two headers: attacker-controlled first, real client second. Joined = "9.9.9.9,8.8.8.8".
    req = _fake_request_multi("1.2.3.4", ["9.9.9.9", "8.8.8.8"])
    assert da._login_key("alice", req) == "infra_brain:login_lockout:alice:8.8.8.8"


def test_revoke_session_sets_redis_key_with_ttl():
    import infra_brain.dashboard_auth as da

    mock_redis = MagicMock()
    with patch("infra_brain.dashboard_auth.get_redis", return_value=mock_redis):
        da.revoke_session("abc123", ttl_seconds=60)
    mock_redis.set.assert_called_once_with("infra_brain:revoked_session:abc123", "1", ex=60)


def test_logout_revokes_session_jti_so_stolen_cookie_is_rejected(app_client):
    """Verifies logout's server-side revocation actually blocks a replayed cookie.

    Deviation from the literal brief test: the brief's version calls the real
    `get_redis()` with no mock, which requires a live Redis reachable at
    ``redis_url``. Neither this sandbox nor the GitLab CI `test` job provisions
    a Redis service (only Postgres — see `.gitlab-ci.yml` `services:` blocks),
    and every other Redis-touching test in this suite (e.g. `tests/test_dedup.py`)
    patches `get_redis`. A stateful fake (shared dict) is patched in here so the
    SET done by `revoke_session` (on logout) and the EXISTS done by
    `_is_revoked` (on the next request) observe the same store, exactly like a
    real Redis would within one test process — this exercises the real
    revoke-then-check code path without requiring network I/O.
    """
    store: dict[str, str] = {}

    class _FakeRedis:
        def set(self, key, value, ex=None):
            store[key] = value
            return True

        def exists(self, key):
            return 1 if key in store else 0

        # TRK-095: the login flow now also drives the Redis-backed lockout
        # (sorted-set ops); no-op them so this revocation test still exercises
        # the login→logout→replay path without a real Redis.
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
                store.pop(k, None)
            return len(keys)

    with patch("infra_brain.dashboard_auth.get_redis", return_value=_FakeRedis()):
        login = app_client.post(
            "/api/dashboard/login", json={"username": "alice", "password": "s3cret"}
        )
        stolen_cookie = login.cookies["infra_brain_session"]
        app_client.post("/api/dashboard/logout")
        # Simulate an attacker replaying the stolen (pre-logout) cookie afterward.
        resp = app_client.get(
            "/api/dashboard/resources", cookies={"infra_brain_session": stolen_cookie}
        )
    assert resp.status_code == 401


def test_revocation_check_fails_closed_when_redis_unavailable(app_client):
    """TRK-096: a Redis outage must NOT let a session through unchecked.

    A valid, non-revoked session that logged in while Redis was up is denied once
    Redis goes down, because revocation cannot be confirmed. This is the accepted
    blast-radius trade-off (FAVOR SECURITY): during a Redis outage the dashboard
    denies ALL sessions rather than risk honoring a revoked one. Replaces the prior
    fail-OPEN behavior (which returned 200 here) fixed under TRK-096.
    """
    import redis as _redis

    app_client.post("/api/dashboard/login", json={"username": "alice", "password": "s3cret"})
    mock_redis = MagicMock()
    mock_redis.exists.side_effect = _redis.RedisError("down")
    with patch("infra_brain.dashboard_auth.get_redis", return_value=mock_redis):
        resp = app_client.get("/api/dashboard/resources")
    # Fail CLOSED: cannot confirm the token is not revoked → deny (401).
    assert resp.status_code == 401


def test_is_revoked_fails_closed_when_redis_raises(monkeypatch):
    """TRK-096 unit: _is_revoked returns True (treat as revoked) on a Redis error."""
    import redis as _redis

    import infra_brain.dashboard_auth as da

    broken = MagicMock()
    broken.exists.side_effect = _redis.RedisError("down")
    with patch.object(da, "get_redis", return_value=broken):
        assert da._is_revoked("some-jti") is True


def test_is_revoked_true_for_revoked_jti_with_redis_up():
    """A revoked jti (present in Redis) is reported revoked when Redis is reachable."""
    import infra_brain.dashboard_auth as da

    up = MagicMock()
    up.exists.return_value = 1
    with patch.object(da, "get_redis", return_value=up):
        assert da._is_revoked("revoked-jti") is True


def test_is_revoked_false_for_live_jti_with_redis_up():
    """A live (never-revoked) jti is reported not-revoked when Redis is reachable —
    fail-closed must not reject valid sessions while Redis is healthy."""
    import infra_brain.dashboard_auth as da

    up = MagicMock()
    up.exists.return_value = 0
    with patch.object(da, "get_redis", return_value=up):
        assert da._is_revoked("live-jti") is False


# ─────────────────────────────────────────────────────────────────────────────
# TRK-321 — settings surface split: admin-only full view vs. the non-sensitive
# subset readable by any signed-in session.
# ─────────────────────────────────────────────────────────────────────────────


@contextmanager
def _viewer_client(engine):
    """A logged-in NON-admin ("viewer") session against the routers backing the
    two real consumers of the settings surface: governance_ops (the settings
    routes) and governance_intelligence (integration proposals — the other leg
    of Intprops.tsx's ``Promise.all``).

    Deliberately does NOT set ``INFRA_BRAIN_DEV``: ``require_admin`` is open in
    dev mode, which is precisely why the first attempt at gating this route
    looked fine locally and only broke real deployments carrying non-admin
    ``ui_users`` rows (see the TRK-321 tracker row).
    """
    from infra_brain.api.routers.governance import (
        governance_intelligence_router,
        governance_ops_router,
    )
    from infra_brain.dashboard_auth import auth_router

    with Session(engine) as s:
        s.add(
            UIUser(
                username="viewer",
                password_hash=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
                name="Viewer",
                role="viewer",
                active=True,
            )
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(governance_ops_router)
    app.include_router(governance_intelligence_router)
    with (
        patch("infra_brain.dashboard_auth.get_session", _get_session),
        patch("infra_brain.api.routers.governance_ops.get_session", _get_session),
        patch("infra_brain.api.routers.governance_intelligence.get_session", _get_session),
        patch("infra_brain.dashboard_auth.get_redis", return_value=_FakeAuthRedis()),
    ):
        client = TestClient(app, base_url="https://testserver")
        resp = client.post("/api/dashboard/login", json={"username": "viewer", "password": "pw"})
        assert resp.status_code == 200, resp.text
        yield client


def test_settings_full_view_requires_admin(engine, monkeypatch):
    """TRK-321: the full ``model_dump()`` of Settings is an elevated view. A
    signed-in non-admin must not be able to enumerate every configuration field
    in the system."""
    monkeypatch.setenv("UI_COOKIE_SECRET", "unit-test-secret")
    with _viewer_client(engine) as client:
        resp = client.get("/api/dashboard/settings")
        assert resp.status_code == 403, resp.text


def test_ui_settings_subset_readable_by_non_admin(engine, monkeypatch):
    """TRK-321: gating the full view must NOT take the one non-sensitive value
    the Integrations page needs down with it — that is what got the first
    attempt at this fix reverted."""
    monkeypatch.setenv("UI_COOKIE_SECRET", "unit-test-secret")
    with _viewer_client(engine) as client:
        resp = client.get("/api/dashboard/settings/ui")
        assert resp.status_code == 200, resp.text
        keys = {r["k"] for r in resp.json()["items"]}
        assert "INTEGRATION_CONFIDENCE_GATE" in keys


def test_ui_settings_subset_does_not_leak_secrets_to_non_admin(engine, monkeypatch):
    """TRK-321: the subset is an ALLOWLIST, so a secret-bearing field must not
    reach a non-admin through it — neither by key nor by value."""
    monkeypatch.setenv("UI_COOKIE_SECRET", "unit-test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-trk321-must-not-leak")
    dsn_password = "trk321DsnPassword"  # noqa: S105 — test fixture
    monkeypatch.setenv(
        "POSTGRES_URL",
        f"postgresql+psycopg://ibuser:{dsn_password}@db.internal:5432/infra_brain",
    )
    from infra_brain.config import get_settings

    get_settings.cache_clear()
    try:
        with _viewer_client(engine) as client:
            resp = client.get("/api/dashboard/settings/ui")
            assert resp.status_code == 200, resp.text
            body = resp.text
            payload = resp.json()
    finally:
        get_settings.cache_clear()

    assert "sk-trk321-must-not-leak" not in body
    assert dsn_password not in body
    assert "ibuser" not in body
    keys = {r["k"] for r in payload["items"]}
    assert "ANTHROPIC_API_KEY" not in keys
    assert "POSTGRES_URL" not in keys
    assert not any(r["type"] == "secret" for r in payload["items"])


def test_intprops_data_path_intact_for_non_admin(engine, monkeypatch):
    """TRK-321 regression guard for the exact failure that reverted the first
    fix: Intprops.tsx loads its page from a ``Promise.all`` of the proposals
    list AND the settings read. BOTH legs must succeed for a non-admin, or the
    whole Integrations page — proposals included — blanks."""
    monkeypatch.setenv("UI_COOKIE_SECRET", "unit-test-secret")
    with _viewer_client(engine) as client:
        proposals = client.get("/api/dashboard/integration_proposals?limit=500")
        gate = client.get("/api/dashboard/settings/ui")
        assert proposals.status_code == 200, proposals.text
        assert gate.status_code == 200, gate.text
        row = next(r for r in gate.json()["items"] if r["k"] == "INTEGRATION_CONFIDENCE_GATE")
        # Intprops parses this with Number(row.v) — it must be numeric text.
        assert float(row["v"]) == 0.7
