"""Tests for the request-ID middleware wired into create_app() (#88)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra_brain.request_context import get_request_id, request_id_middleware_factory


def _test_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(request_id_middleware_factory())

    @app.get("/probe")
    def probe():
        return {"seen_request_id": get_request_id()}

    return app


def test_generates_a_request_id_when_none_provided():
    client = TestClient(_test_app())
    resp = client.get("/probe")
    assert resp.status_code == 200
    assert "X-Request-ID" in resp.headers
    assert resp.json()["seen_request_id"] == resp.headers["X-Request-ID"]


def test_honors_an_incoming_x_request_id_header():
    client = TestClient(_test_app())
    resp = client.get("/probe", headers={"X-Request-ID": "client-supplied-123"})
    assert resp.headers["X-Request-ID"] == "client-supplied-123"
    assert resp.json()["seen_request_id"] == "client-supplied-123"


def test_request_id_is_cleared_after_the_request_completes():
    client = TestClient(_test_app())
    client.get("/probe")
    assert get_request_id() is None
