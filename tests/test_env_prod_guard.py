"""F23 — the INFRA_BRAIN_DEV master kill-switch must be refused in hardened envs.

INFRA_BRAIN_DEV disables dashboard auth, webhook auth, and MCP auth. Both the
FastAPI app (create_app) and the MCP server must refuse to boot when that flag
is truthy AND ENVIRONMENT names a hardened deployment. "deployed" is the
canonical value; "production" is a legacy alias that must keep guarding so an
older host-side .env can never silently weaken the check.
"""

import pytest

from infra_brain.config import get_settings, is_hardened_environment


def _clear():
    get_settings.cache_clear()


@pytest.mark.parametrize("env", ["deployed", "production", " Deployed ", "PRODUCTION"])
def test_hardened_environment_values(env):
    assert is_hardened_environment(env)


@pytest.mark.parametrize("env", ["development", "", "dev", "staging"])
def test_non_hardened_environment_values(env):
    assert not is_hardened_environment(env)


@pytest.mark.parametrize("env", ["deployed", "production"])
def test_create_app_refuses_dev_in_hardened_env(monkeypatch, env):
    from infra_brain import main

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("ENVIRONMENT", env)
    _clear()

    with pytest.raises(RuntimeError, match="INFRA_BRAIN_DEV"):
        main.create_app()
    _clear()


def test_create_app_boots_dev_in_development(monkeypatch):
    from infra_brain import main

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("ENVIRONMENT", "development")
    _clear()

    app = main.create_app()  # must not raise
    assert app is not None
    _clear()


@pytest.mark.parametrize("env", ["deployed", "production"])
def test_create_app_boots_when_dev_off_in_hardened_env(monkeypatch, env):
    from infra_brain import main

    monkeypatch.setenv("INFRA_BRAIN_DEV", "0")
    monkeypatch.setenv("ENVIRONMENT", env)
    _clear()

    app = main.create_app()  # must not raise
    assert app is not None
    _clear()


@pytest.mark.parametrize("env", ["deployed", "production"])
def test_mcp_server_refuses_dev_in_hardened_env(monkeypatch, env):
    from infra_brain import mcp_server

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("ENVIRONMENT", env)
    _clear()

    with pytest.raises(RuntimeError, match="INFRA_BRAIN_DEV"):
        mcp_server.assert_dev_not_in_hardened_env()
    _clear()


@pytest.mark.parametrize("env", ["deployed", "production"])
def test_mcp_server_boots_when_dev_off_in_hardened_env(monkeypatch, env):
    from infra_brain import mcp_server

    monkeypatch.setenv("INFRA_BRAIN_DEV", "0")
    monkeypatch.setenv("ENVIRONMENT", env)
    _clear()

    mcp_server.assert_dev_not_in_hardened_env()  # must not raise
    _clear()


def test_mcp_server_boots_dev_in_development(monkeypatch):
    from infra_brain import mcp_server

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("ENVIRONMENT", "development")
    _clear()

    mcp_server.assert_dev_not_in_hardened_env()  # must not raise
    _clear()


# --- /docs, /redoc, /openapi.json must be gated the same way in a hardened env ---
# Every other route is gated (session cookie / HMAC / bearer token), but
# create_app() used to always pass FastAPI its default docs_url="/docs" and
# openapi_url="/openapi.json", leaving Swagger UI and the full route/schema
# catalog reachable with zero auth even on a deployed/hardened stack. This
# mirrors _assert_dev_not_in_hardened_env's own is_hardened_environment(...)
# gate: docs/schema stay on for local dev, off for deployed/production.


@pytest.mark.parametrize("env", ["deployed", "production"])
def test_docs_and_openapi_disabled_in_hardened_env(monkeypatch, env):
    from fastapi.testclient import TestClient

    from infra_brain import main

    monkeypatch.setenv("INFRA_BRAIN_DEV", "0")
    monkeypatch.setenv("ENVIRONMENT", env)
    _clear()

    app = main.create_app()

    # docs_url/openapi_url are what create_app() actually sets on the FastAPI
    # instance; the client round-trip additionally proves the routes are gone
    # (openapi_url=None also removes /redoc's route, since FastAPI only
    # registers it when both openapi_url and redoc_url are truthy).
    assert app.docs_url is None
    assert app.openapi_url is None

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    _clear()


@pytest.mark.parametrize("env", ["development", "staging", ""])
def test_docs_and_openapi_enabled_outside_hardened_env(monkeypatch, env):
    from fastapi.testclient import TestClient

    from infra_brain import main

    monkeypatch.setenv("INFRA_BRAIN_DEV", "0")
    monkeypatch.setenv("ENVIRONMENT", env)
    _clear()

    app = main.create_app()

    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    _clear()
