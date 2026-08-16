"""Task 2: /api/graphql is mounted and session-gated like every other
dashboard router."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra_brain.api.graphql.router import graphql_router
from infra_brain.config import get_settings


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(graphql_router)
    return application


def test_graphql_endpoint_open_in_dev_mode(app, monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()
    client = TestClient(app)
    resp = client.post("/api/graphql", json={"query": "{ health }"})
    assert resp.status_code == 200
    assert resp.json()["data"] == {"health": "ok"}
    get_settings.cache_clear()


def test_graphql_endpoint_requires_session_when_not_dev(app, monkeypatch):
    monkeypatch.delenv("INFRA_BRAIN_DEV", raising=False)
    monkeypatch.setenv("UI_COOKIE_SECRET", "a-real-non-weak-secret-value")
    get_settings.cache_clear()
    client = TestClient(app)
    resp = client.post("/api/graphql", json={"query": "{ health }"})
    assert resp.status_code in (401, 403)
    get_settings.cache_clear()


def test_graphql_introspection_query_returns_schema(app, monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()
    client = TestClient(app)
    resp = client.post(
        "/api/graphql",
        json={"query": "{ __schema { queryType { name } mutationType { name } } }"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["__schema"]["queryType"]["name"] == "Query"
    # No Mutation type exposed via introspection either.
    assert body["data"]["__schema"]["mutationType"] is None
    get_settings.cache_clear()
