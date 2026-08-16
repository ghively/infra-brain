"""Session-cookie authentication for the FastAPI dashboard.

Reuses the existing ``ui_users`` table (bcrypt password hashes) that the legacy
Streamlit UI used, and issues a signed, HTTP-only session cookie. The data router
(`/api/dashboard/*`) depends on :func:`require_session`; the login/logout/me
endpoints live on a separate, ungated router.

Dev-mode (open) when ``UI_COOKIE_SECRET`` is unset — secure cookies cannot be
signed/verified without it, so enforcing would lock everyone out. Every deployed
environment (docker-compose, k8s) sets ``UI_COOKIE_SECRET``, so the dashboard is
gated in production while local runs and the test suite stay open.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import time as _time
import uuid as _uuid_mod

import redis
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from infra_brain.api.schemas import LoginOut, LogoutOut, MeOut
from infra_brain.config import get_settings
from infra_brain.db.models import UIUser
from infra_brain.db.session import get_session
from infra_brain.dedup import get_redis

log = logging.getLogger(__name__)

COOKIE_NAME = "infra_brain_session"
COOKIE_EXPIRY_DAYS = 1
_MAX_AGE = COOKIE_EXPIRY_DAYS * 86400
_SALT_BASE = "infra-brain-dashboard-session"
# Secrets that must NEVER sign a real cookie. If UI_COOKIE_SECRET is one of these
# in a non-dev environment, the app refuses to start (see assert_cookie_secret_ok).
_WEAK_SECRETS = {"", "changeme", "dev-insecure"}


def _cookie_secret() -> str | None:
    return get_settings().ui_cookie_secret or None


def _dev_mode() -> bool:
    """Dev-mode is EXPLICIT: only INFRA_BRAIN_DEV=1 opens the no-auth path.

    Previously dev-mode meant "no cookie secret configured", which silently
    opened production when an env var was missing. Now dev must opt in.
    """
    return get_settings().infra_brain_dev


def _salt() -> str:
    """Per-deploy salt: bind the itsdangerous salt to the configured secret so a
    cookie forged under the old fixed constant salt fails verification."""
    secret = _cookie_secret() or "dev-insecure"
    fingerprint = hashlib.sha256(secret.encode()).hexdigest()[:16]
    return f"{_SALT_BASE}:{fingerprint}"


def assert_cookie_secret_ok() -> None:
    """Fail closed at startup: in non-dev, refuse a missing/weak cookie secret."""
    if _dev_mode():
        return
    secret = get_settings().ui_cookie_secret
    if secret in _WEAK_SECRETS:
        raise RuntimeError(
            "UI_COOKIE_SECRET is unset or a known-weak default "
            "(refusing to start outside dev). Set a strong random secret, "
            "or set INFRA_BRAIN_DEV=1 for local/dev runs."
        )


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_cookie_secret() or "dev-insecure", salt=_salt())


def _revoked_key(jti: str) -> str:
    return f"infra_brain:revoked_session:{jti}"


def revoke_session(jti: str, ttl_seconds: int = _MAX_AGE) -> None:
    """Mark a session jti as revoked for the remainder of its natural TTL.

    A Redis error is logged, not raised: logout must still clear the client-side
    cookie even if the revocation store is briefly unavailable.
    """
    try:
        get_redis().set(_revoked_key(jti), "1", ex=ttl_seconds)
    except redis.RedisError:
        log.exception("Redis unavailable revoking session %s; client cookie still cleared", jti)


def _is_revoked(jti: str | None) -> bool:
    """Return True when this session must be rejected — revoked, or unverifiable.

    Fails CLOSED (TRK-096). If Redis is unreachable we cannot prove the token is
    NOT revoked, so we treat it as revoked and deny. A session that was explicitly
    revoked (logout, or a stolen-cookie kill) must never become usable again just
    because the revocation store blipped — that is exactly the fail-OPEN hole this
    replaces (a revoked session used to stay valid for the whole outage).

    Blast-radius trade-off — deliberately accepted, FAVOR SECURITY per TRK-096:
    the revocation record lives ONLY in Redis. The session cookie is self-contained
    and signed and carries no revocation state, so without Redis a revoked session
    and a live one are indistinguishable. Failing closed therefore denies EVERY
    session for the duration of a Redis outage, not only revoked ones — an
    availability cliff for the dashboard (re-login does not help: the fresh cookie's
    jti also cannot be confirmed-unrevoked while Redis is down). This is acceptable
    because infra-brain is a READ-ONLY audit dashboard: briefly denying reads during
    a Redis outage is strictly preferable to honoring a session that was explicitly
    revoked. Distinguishing the two without Redis would require a parallel revocation
    store (e.g. in Postgres), which is deliberately out of scope here.

    Deliberate asymmetry with the login-lockout path (_is_locked_out, TRK-095), which
    fails OPEN: failing open there only forgoes a rate-limit (defense-in-depth) for the
    outage, whereas failing open HERE would defeat the entire logout/revocation
    security boundary. Opposite fail directions are the correct choice for each.
    """
    if jti is None:
        # No jti to key on (pre-jti or jti-less signed cookie). Such a session can
        # never be revoked, so this is not a Redis-outage decision — leave as-is.
        return False
    try:
        return bool(get_redis().exists(_revoked_key(jti)))
    except redis.RedisError:
        log.exception(
            "Redis unavailable checking session revocation; failing CLOSED "
            "(session denied) — cannot confirm the token is not revoked"
        )
        return True


def verify_credentials(username: str, password: str) -> dict | None:
    """Return a user dict if username/password match an active ui_users row."""
    import bcrypt

    with get_session() as s:
        user = (
            s.query(UIUser)
            .filter(UIUser.username == username, UIUser.active.is_(True))
            .one_or_none()
        )
        if user is None:
            return None
        try:
            ok = bcrypt.checkpw(password.encode(), user.password_hash.encode())
        except (ValueError, TypeError):
            return None
        if not ok:
            return None
        return {"username": user.username, "name": user.name, "role": user.role}


def current_user(request: Request) -> dict | None:
    """Return the signed-in user (from the session cookie) or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if _is_revoked(payload.get("jti")):
        return None
    return payload


async def require_session(request: Request) -> None:
    """FastAPI dependency: allow signed-in users; allow all in dev-mode; else 401."""
    if _dev_mode():
        return
    if current_user(request) is not None:
        return
    raise HTTPException(status_code=401, detail="authentication required")


async def require_admin(request: Request) -> None:
    """FastAPI dependency: allow admin-role users only; dev-mode is open; else 403."""
    if _dev_mode():
        return
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin role required")


# ─────────────────────────────────────────────────────────────────────────────
# Auth endpoints (ungated)
# ─────────────────────────────────────────────────────────────────────────────

auth_router = APIRouter(prefix="/api/dashboard", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


# ── Login rate-limit / lockout (F-031, TRK-095) ──────────────────────────────
# Redis-backed sliding window keyed by (username, client_ip). After
# _LOCKOUT_THRESHOLD failures inside _LOCKOUT_WINDOW_S, /login returns 429 without
# checking the password.
#
# TRK-095: state moved from a per-process in-memory dict to Redis so the effective
# threshold is process-/pod-count-invariant (the old dict multiplied the threshold
# by the worker count under a multi-process deployment). Implemented as a Redis
# sorted set of failure timestamps — the exact sliding-window semantics of the old
# list-of-timestamps, just shared across processes. Threshold/window UNCHANGED.
# Fails OPEN (never locks) on a Redis outage — a Redis blip must not deny all
# logins. NOTE this DELIBERATELY DIFFERS from _is_revoked, which fails CLOSED
# (TRK-096): failing open here only forgoes a rate-limit (defense-in-depth),
# whereas failing open on revocation would defeat the logout/kill boundary.
# Do NOT "restore consistency" by flipping either — the opposite directions are
# each intentional.
#
# X-Forwarded-For handling (TRK-095, was "STILL OPEN"): the client IP used in the
# lockout key is now derived by _lockout_client_ip(). It is SAFE-BY-DEFAULT and
# anti-spoofing: X-Forwarded-For is honored ONLY when the immediate peer
# (request.client.host) is a configured trusted proxy (settings.trusted_proxy_ips,
# empty by default = trust none), mirroring uvicorn's ProxyHeadersMiddleware
# forwarded_allow_ips. With the default empty setting the raw peer IP is used and
# the header is ignored, so behind no proxy (or an unconfigured one) a client cannot
# spoof its attributed IP by injecting X-Forwarded-For — and the key format is
# byte-for-byte identical to the pre-TRK-095 behavior. Configure trusted_proxy_ips
# with the fronting proxy/LB address(es) to recover real client IPs behind a proxy.
# NOTE: Host-header validation is a separate concern handled elsewhere — Starlette's
# TrustedHostMiddleware is wired into the FastAPI app factory (main.create_app) via the
# trusted_hosts config setting (TRK-095 residual). It is opt-in: the default empty
# setting parses to ["*"] (accept any Host, pre-TRK-095 behavior); set TRUSTED_HOSTS to
# the deployment's hostname(s) to reject a spoofed/mismatched Host with HTTP 400.
_LOCKOUT_THRESHOLD = 5
_LOCKOUT_WINDOW_S = 300


def _parse_trusted_proxies() -> list[ipaddress._BaseNetwork]:
    """Parse settings.trusted_proxy_ips (comma-separated IPs/CIDRs) into networks.

    Bare IPs become /32 (or /128) networks. Blank or malformed entries are skipped
    defensively so a typo in configuration can never raise at request time — a bad
    entry simply is not trusted (safe direction).
    """
    raw = getattr(get_settings(), "trusted_proxy_ips", "") or ""
    nets: list[ipaddress._BaseNetwork] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("Ignoring malformed trusted_proxy_ips entry: %r", part)
    return nets


def _ip_in_networks(ip_str: str, nets: list[ipaddress._BaseNetwork]) -> bool:
    """True if ip_str parses and falls within any of nets. Never raises."""
    if not nets:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def _lockout_client_ip(request: Request) -> str:
    """Derive the client IP for the login-lockout key, safe-by-default.

    - If the immediate peer (request.client.host) is NOT a configured trusted proxy
      (or none are configured), return the peer IP and IGNORE X-Forwarded-For. This
      prevents a direct client from spoofing its attributed IP via the header.
    - If the peer IS trusted, parse X-Forwarded-For (left = original client,
      right = nearest proxy) and return the RIGHTMOST address that is NOT itself a
      trusted proxy — i.e. peel trusted proxies off the right. This stops a client
      from injecting extra XFF entries to forge an IP.
    - ALL X-Forwarded-For headers are read and concatenated left-to-right before the
      peel: a misconfigured proxy that emits multiple XFF headers (instead of one
      appended chain) cannot let an attacker-controlled first header win.
    - Any missing/empty header or malformed address falls back to the peer IP; this
      function never raises.
    """
    peer = request.client.host if request.client else "unknown"
    trusted = _parse_trusted_proxies()
    if not _ip_in_networks(peer, trusted):
        # Peer is not a trusted proxy (or trust-none default): use it, ignore XFF.
        return peer
    # Peer is trusted — consult X-Forwarded-For. Read EVERY X-Forwarded-For header
    # (a proxy may emit several separate headers rather than one appended chain) and
    # join them into a single left-to-right chain, so a spoofed leading header cannot
    # short-circuit the peel-from-right logic. getlist() is the Starlette Headers API;
    # guard defensively in case some call path passes a plain mapping.
    headers = request.headers
    xff_values: list[str] = []
    if headers is not None:
        getlist = getattr(headers, "getlist", None)
        if callable(getlist):
            xff_values = [v for v in getlist("x-forwarded-for") if v]
        else:
            single = headers.get("x-forwarded-for")
            if single:
                xff_values = [single]
    xff = ",".join(xff_values)
    if not xff:
        return peer
    candidates = [c.strip() for c in xff.split(",") if c.strip()]
    # Walk right-to-left, peeling off trusted proxies. The first non-trusted hop is
    # the attributed client — but only if it is a well-formed IP; a malformed token
    # (a broken/forged chain) is never returned, we fall back to the peer instead.
    for candidate in reversed(candidates):
        if _ip_in_networks(candidate, trusted):
            continue  # trusted proxy — peel it off
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            break  # malformed non-trusted hop — safe fallback to peer
        return candidate
    # Every hop was a trusted proxy / malformed / list empty — fall back to peer IP.
    return peer


def _login_key(username: str, request: Request) -> str:
    client = _lockout_client_ip(request)
    return f"infra_brain:login_lockout:{username or ''}:{client}"


def _is_locked_out(key: str) -> bool:
    now = _time.time()
    try:
        client = get_redis()
        # Drop failures older than the window, then count what remains.
        client.zremrangebyscore(key, 0, now - _LOCKOUT_WINDOW_S)
        return client.zcard(key) >= _LOCKOUT_THRESHOLD
    except redis.RedisError:
        log.exception(
            "Redis unavailable checking login lockout for %s; failing open (attempt allowed)",
            key,
        )
        return False


def _record_failure(key: str) -> None:
    now = _time.time()
    try:
        client = get_redis()
        # Unique member per failure so repeated same-instant failures all count.
        client.zadd(key, {f"{now}:{_uuid_mod.uuid4().hex}": now})
        client.expire(key, _LOCKOUT_WINDOW_S)
    except redis.RedisError:
        log.exception("Redis unavailable recording login failure for %s", key)


def _clear_failures(key: str) -> None:
    try:
        get_redis().delete(key)
    except redis.RedisError:
        log.exception("Redis unavailable clearing login failures for %s", key)


@auth_router.post("/login", response_model=LoginOut)
def login(req: LoginRequest, request: Request, response: Response):
    if _dev_mode():
        # Dev-mode (INFRA_BRAIN_DEV=1) — nothing to sign against; report open mode.
        return {"authenticated": True, "dev_mode": True, "username": req.username or "dev"}
    key = _login_key(req.username, request)
    if _is_locked_out(key):
        raise HTTPException(status_code=429, detail="too many failed attempts — try again later")
    user = verify_credentials(req.username, req.password)
    if user is None:
        _record_failure(key)
        raise HTTPException(status_code=401, detail="invalid username or password")
    _clear_failures(key)
    token = _serializer().dumps({**user, "jti": _uuid_mod.uuid4().hex})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=_MAX_AGE,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"authenticated": True, "dev_mode": False, "username": user["username"]}


@auth_router.post("/logout", response_model=LogoutOut)
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            payload = _serializer().loads(token, max_age=_MAX_AGE)
        except (BadSignature, SignatureExpired):
            payload = None
        jti = (payload or {}).get("jti")
        if jti:
            revoke_session(jti)
    # Mirror the Secure flag so browsers that received a Secure cookie can delete it.
    response.delete_cookie(COOKIE_NAME, path="/", secure=get_settings().cookie_secure)
    return {"authenticated": False}


@auth_router.get("/me", response_model=MeOut)
def me(request: Request):
    """Report auth state so the SPA can decide whether to show the login screen."""
    if _dev_mode():
        return {
            "authenticated": True,
            "dev_mode": True,
            "username": "dev",
            "name": "Dev User",
            "role": "admin",
        }
    user = current_user(request)
    return {
        "authenticated": user is not None,
        "dev_mode": False,
        "username": (user or {}).get("username"),
        "name": (user or {}).get("name"),
        "role": (user or {}).get("role"),
    }


# Re-exported for wiring in main.py and for the data router's dependency.
__all__ = ["auth_router", "require_session", "require_admin", "current_user", "verify_credentials"]
require_session_dep = Depends(require_session)
