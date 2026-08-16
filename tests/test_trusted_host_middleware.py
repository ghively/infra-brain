"""TRK-095 residual: Starlette TrustedHostMiddleware host-header validation.

Hardening is OPT-IN. When ``trusted_hosts`` (env ``TRUSTED_HOSTS``) is unset/empty
the app defaults to ``["*"]`` so any Host header is accepted (no breakage for
existing deployments). When set, only the listed hosts are allowed and any other
Host header is rejected with HTTP 400 by the middleware, before routing.
"""

from fastapi.testclient import TestClient

from infra_brain.config import get_settings, parse_trusted_hosts
from infra_brain.main import create_app

# A no-auth, zero-I/O endpoint that always 200s once the request passes the
# host-header gate — lets us isolate the middleware's allow/deny behavior.
_PROBE = "/metrics"


def _build_client(monkeypatch, trusted_hosts_value: str | None):
    if trusted_hosts_value is None:
        monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    else:
        monkeypatch.setenv("TRUSTED_HOSTS", trusted_hosts_value)
    get_settings.cache_clear()
    return TestClient(create_app())


def test_parse_trusted_hosts_defaults_to_wildcard_when_empty():
    assert parse_trusted_hosts("") == ["*"]
    assert parse_trusted_hosts(None) == ["*"]
    assert parse_trusted_hosts("   ") == ["*"]


def test_parse_trusted_hosts_splits_and_strips():
    assert parse_trusted_hosts("a.example, b.example ,c.example") == [
        "a.example",
        "b.example",
        "c.example",
    ]
    # blank entries from trailing/double commas are dropped
    assert parse_trusted_hosts("a.example,,") == ["a.example"]


def test_allowed_host_passes_when_configured(monkeypatch):
    client = _build_client(monkeypatch, "goodhost.example")
    resp = client.get(_PROBE, headers={"Host": "goodhost.example"})
    assert resp.status_code == 200


def test_disallowed_host_rejected_with_400_when_configured(monkeypatch):
    client = _build_client(monkeypatch, "goodhost.example")
    resp = client.get(_PROBE, headers={"Host": "evil.example"})
    assert resp.status_code == 400


def test_any_host_accepted_when_unset(monkeypatch):
    """Backward-compat: unset TRUSTED_HOSTS => wildcard => any Host accepted."""
    client = _build_client(monkeypatch, None)
    for host in ("anything.example", "goodhost.example", "127.0.0.1:8000"):
        resp = client.get(_PROBE, headers={"Host": host})
        assert resp.status_code == 200, host


def test_empty_host_accepted_when_unset(monkeypatch):
    client = _build_client(monkeypatch, "")
    resp = client.get(_PROBE, headers={"Host": "whatever.example"})
    assert resp.status_code == 200
