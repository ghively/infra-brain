"""Tests for the RAG documents / knowledge-store browser endpoints
(UI Batch 9 / GitLab #65).

Mirrors test_dashboard_fleet_software_cve.py's conventions: in-memory
SQLite, ORM schema via Base.metadata.create_all, get_session patched to the
test engine, auth-off via INFRA_BRAIN_DEV=1.
"""

import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import Document, DocumentChunk

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

    from infra_brain.api.routers.documents import documents_router

    app = FastAPI()
    app.include_router(documents_router)
    with patch("infra_brain.api.routers.documents.get_session", _get_session):
        yield TestClient(app)


def _seed(engine):
    with Session(engine) as s:
        d1 = Document(
            title="Runbook: Postgres Failover", sensitivity="internal",
            source="confluence", space="OPS", status="current",
        )
        d2 = Document(
            title="PCI Scope Definition", sensitivity="restricted",
            source="confluence", space="COMPLIANCE", status="stale",
        )
        s.add_all([d1, d2])
        s.flush()
        s.add(DocumentChunk(document_id=d1.id, chunk_index=0, text="Step 1: ...", token_count=120))
        s.add(DocumentChunk(document_id=d1.id, chunk_index=1, text="Step 2: ...", token_count=98))
        s.commit()
        return str(d1.id), str(d2.id)


def test_list_documents_returns_chunk_counts(client, engine):
    d1_id, d2_id = _seed(engine)
    resp = client.get("/api/dashboard/documents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    by_id = {r["id"]: r for r in body["items"]}
    assert by_id[d1_id]["chunk_count"] == 2
    assert by_id[d2_id]["chunk_count"] == 0
    assert by_id[d2_id]["sensitivity"] == "restricted"


def test_list_documents_filters_by_status(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/documents?status=stale")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "stale"


def test_list_documents_filters_by_sensitivity(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/documents?sensitivity=restricted")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "PCI Scope Definition"


def test_list_documents_empty_when_none_indexed(client, engine):
    resp = client.get("/api/dashboard/documents")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}


def test_document_detail_returns_chunk_previews(client, engine):
    d1_id, _ = _seed(engine)
    resp = client.get(f"/api/dashboard/documents/{d1_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Runbook: Postgres Failover"
    assert body["chunk_count"] == 2
    assert [c["chunk_index"] for c in body["chunks"]] == [0, 1]
    assert "embedding" not in body["chunks"][0]


def test_document_detail_404_for_unknown_id(client, engine):
    _seed(engine)
    resp = client.get(f"/api/dashboard/documents/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_document_detail_empty_chunks_when_none_embedded(client, engine):
    _, d2_id = _seed(engine)
    resp = client.get(f"/api/dashboard/documents/{d2_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chunks"] == []
    assert body["chunk_count"] == 0
