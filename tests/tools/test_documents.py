"""Tests for tools/documents.py — MCP-facing document ingest (P4.3c).

Mirrors test_dashboard_documents.py / test_rootcause.py's conventions:
in-memory SQLite via StaticPool, ORM schema built with
Base.metadata.create_all, get_session patched to a session factory bound
to that engine.
"""

import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import Document
from infra_brain.tools.documents import ingest_document, update_document_metadata

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture(autouse=True)
def _patch_get_session(engine, monkeypatch):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr("infra_brain.tools.documents.get_session", _get_session)
    return _get_session


def _count_documents(engine) -> int:
    with Session(engine) as s:
        return s.query(Document).count()


# ---------------------------------------------------------------------------
# ingest_document
# ---------------------------------------------------------------------------


def test_ingest_document_success(engine):
    result = ingest_document(
        title="Runbook: Postgres Failover",
        content_hash="abc123",
        source="manual",
        client_origin="claude-code",
        ingested_by="mcp:headless-runner-key",
        url="https://example.internal/runbook",
        external_id="conf-456",
    )

    assert result["title"] == "Runbook: Postgres Failover"
    assert result["content_hash"] == "abc123"
    assert result["source"] == "manual"
    assert result["sensitivity"] == "internal"
    assert result["url"] == "https://example.internal/runbook"
    assert result["external_id"] == "conf-456"
    assert result["client_origin"] == "claude-code"
    assert result["ingested_by"] == "mcp:headless-runner-key"
    assert "_already_existed" not in result
    assert result["id"]

    assert _count_documents(engine) == 1


def test_ingest_document_idempotent_duplicate_by_hash(engine):
    first = ingest_document(
        title="Doc A",
        content_hash="samehash",
        source="manual",
        client_origin="claude-code",
        ingested_by="mcp:key-1",
    )

    # Same content_hash, even with different title/source — should NOT
    # write a second row; the existing row wins and is annotated.
    second = ingest_document(
        title="Doc A (retry)",
        content_hash="samehash",
        source="headless-runner",
        client_origin="headless-runner",
        ingested_by="mcp:key-2",
    )

    assert second["_already_existed"] is True
    assert second["id"] == first["id"]
    # The original values are preserved, not overwritten by the retry.
    assert second["title"] == "Doc A"
    assert second["client_origin"] == "claude-code"

    assert _count_documents(engine) == 1


def test_ingest_document_rejects_blank_attribution(engine):
    with pytest.raises(ValueError):
        ingest_document(
            title="Doc",
            content_hash="hash1",
            source="manual",
            client_origin="claude-code",
            ingested_by="   ",  # blank attribution must be rejected
        )

    assert _count_documents(engine) == 0


# ---------------------------------------------------------------------------
# update_document_metadata
# ---------------------------------------------------------------------------


def test_update_document_metadata_success(engine):
    seeded = ingest_document(
        title="Doc B",
        content_hash="hash-b",
        source="manual",
        client_origin="claude-code",
        ingested_by="mcp:key-1",
    )

    updated = update_document_metadata(
        seeded["id"],
        url="https://example.internal/doc-b",
        external_id="ext-1",
        client_origin="headless-runner",
        ingested_by="mcp:key-2",
    )

    assert updated["id"] == seeded["id"]
    assert updated["url"] == "https://example.internal/doc-b"
    assert updated["external_id"] == "ext-1"
    assert updated["client_origin"] == "headless-runner"
    assert updated["ingested_by"] == "mcp:key-2"
    # Unrelated fields are untouched.
    assert updated["title"] == "Doc B"


def test_update_document_metadata_rejects_unlisted_field(engine):
    seeded = ingest_document(
        title="Doc C",
        content_hash="hash-c",
        source="manual",
        client_origin="claude-code",
        ingested_by="mcp:key-1",
    )

    with pytest.raises(ValueError):
        update_document_metadata(seeded["id"], title="Renamed via back door")

    # Confirm nothing was mutated.
    with Session(engine) as s:
        doc = s.get(Document, uuid.UUID(seeded["id"]))
        assert doc.title == "Doc C"


def test_update_document_metadata_not_found(engine):
    with pytest.raises(ValueError):
        update_document_metadata(str(uuid.uuid4()), url="https://nope")
