"""Tests for the Phase 4 state-backend REST counterparts (P4.3e,
api/routers/state_backend.py). Mirrors test_dashboard_documents.py's
conventions: in-memory SQLite, ORM schema via Base.metadata.create_all,
auth-off via INFRA_BRAIN_DEV=1 -- but each underlying tools/*.py module
resolves its own get_session, so each is patched at its own import site
rather than at the router module (which never imports get_session itself).
"""

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from unittest.mock import patch

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def client(engine, monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.state_backend import state_backend_router

    app = FastAPI()
    app.include_router(state_backend_router)
    with (
        patch("infra_brain.tools.environment_notes.get_session", _get_session),
        patch("infra_brain.tools.instinct_governance.get_session", _get_session),
        patch("infra_brain.tools.documents.get_session", _get_session),
    ):
        yield TestClient(app)


def test_create_and_list_environment_note(client):
    resp = client.post("/api/dashboard/environment-notes", json={"note": "test note"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["note"] == "test note"
    assert body["status"] == "open"
    assert body["author"].startswith("dashboard:")

    listing = client.get("/api/dashboard/environment-notes")
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_create_environment_note_rejects_blank(client):
    resp = client.post("/api/dashboard/environment-notes", json={"note": "   "})
    assert resp.status_code == 400


def test_resolve_environment_note(client):
    created = client.post("/api/dashboard/environment-notes", json={"note": "x"}).json()
    resp = client.post(f"/api/dashboard/environment-notes/{created['id']}/resolve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert resp.json()["resolved_by"].startswith("dashboard:")


def test_resolve_environment_note_not_found(client):
    resp = client.post("/api/dashboard/environment-notes/not-a-uuid/resolve")
    assert resp.status_code == 400


def test_propose_instinct(client):
    resp = client.post(
        "/api/dashboard/instincts/propose",
        json={"zone": "corpor", "domain": "linux", "pattern": "x", "confidence": 0.5},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


def test_instinct_history_not_found(client):
    resp = client.get("/api/dashboard/instincts/00000000-0000-0000-0000-000000000000/history")
    assert resp.status_code == 404


def test_ingest_document(client):
    resp = client.post(
        "/api/dashboard/documents",
        json={"title": "Runbook", "content_hash": "abc123", "source": "manual"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Runbook"
    assert body["client_origin"] == "dashboard"
    assert body["ingested_by"].startswith("dashboard:")


def test_ingest_document_idempotent_by_hash(client):
    first = client.post(
        "/api/dashboard/documents",
        json={"title": "A", "content_hash": "dup", "source": "manual"},
    ).json()
    second = client.post(
        "/api/dashboard/documents",
        json={"title": "B", "content_hash": "dup", "source": "manual"},
    ).json()
    assert first["id"] == second["id"]
    assert second["title"] == "A"  # first-write-wins
