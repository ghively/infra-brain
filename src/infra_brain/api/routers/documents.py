"""infra_brain.api.routers.documents — RAG Documents / Knowledge-Store
Browser API router (UI Batch 9 / GitLab #65).

A health-browse view over the RAG knowledge store — "is my knowledge base
healthy", not semantic search (that already exists via ui.py's /chat and
/custom-view). Distinct from those: this surfaces Document/DocumentChunk
METADATA (freshness, sensitivity, source, last-indexed, chunk count), never
the raw embedding vectors, which have no value in a UI table.

Import rules:
  * MAY import from infra_brain.api.schemas, infra_brain.api._envelope,
    infra_brain.db.models, infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func

from infra_brain.api._envelope import paginate
from infra_brain.api.schemas import (
    DocumentChunkPreviewOut,
    DocumentDetailOut,
    DocumentOut,
    DocumentsPageOut,
)
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import Document, DocumentChunk
from infra_brain.db.session import get_session

# Same prefix/gate as the shared dashboard router — a dedicated sub-path
# (/documents, /documents/{id}) rather than a top-level prefix, matching the
# cve_router precedent for a purpose-built dedicated view.
documents_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)

# Chunk text can run long; a UI list/detail preview only needs the opening
# slice, never the full chunk (full text is available via the existing RAG
# chat/search surfaces, not duplicated here).
_PREVIEW_CHARS = 240


@documents_router.get("/documents", response_model=DocumentsPageOut)
def list_documents(
    status: Optional[str] = None,
    sensitivity: Optional[str] = None,
    space: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    """Paged document list for the knowledge-store browser.

    Read-only (SELECT only). Each row is decorated with its live chunk
    count via a grouped rollup query (never loads chunk text). Empty
    items/total=0 when nothing has been indexed yet — never a 500.

    Filters:
      * ``status`` — exact match (``current`` | ``stale`` | ...).
      * ``sensitivity`` — exact match.
      * ``space`` — exact match.
      * ``q`` — case-insensitive substring over the document title.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        base = s.query(Document)
        if status:
            base = base.filter(Document.status == status)
        if sensitivity:
            base = base.filter(Document.sensitivity == sensitivity)
        if space:
            base = base.filter(Document.space == space)
        if q:
            base = base.filter(Document.title.ilike(f"%{q}%"))

        q_ordered = base.order_by(Document.indexed_at.desc())
        items, total = paginate(q_ordered, limit=limit, offset=offset)

        doc_ids = [d.id for d in items]
        chunk_counts: dict = {}
        if doc_ids:
            for doc_id, n in (
                s.query(DocumentChunk.document_id, func.count(DocumentChunk.id))
                .filter(DocumentChunk.document_id.in_(doc_ids))
                .group_by(DocumentChunk.document_id)
                .all()
            ):
                chunk_counts[doc_id] = n

        return DocumentsPageOut(
            items=[
                DocumentOut(
                    id=str(d.id),
                    title=d.title,
                    sensitivity=d.sensitivity,
                    source=d.source or "",
                    status=d.status,
                    space=d.space or "",
                    url=d.url or "",
                    indexed_at=d.indexed_at,
                    last_updated=d.last_updated,
                    chunk_count=chunk_counts.get(d.id, 0),
                )
                for d in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@documents_router.get("/documents/{document_id}", response_model=DocumentDetailOut)
def get_document_detail(document_id: str):
    """Detail for one document: full metadata plus a chunk-preview list.

    Read-only (SELECT only). 404 when the id does not resolve to a
    Document row. Chunk previews are truncated to ``_PREVIEW_CHARS`` and
    NEVER include the raw ``embedding`` vector — irrelevant to a health-
    browse view.
    """
    try:
        did = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Document not found")
    with get_session() as s:
        doc = s.get(Document, did)
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")
        chunks = (
            s.query(DocumentChunk)
            .filter(DocumentChunk.document_id == did)
            .order_by(DocumentChunk.chunk_index)
            .all()
        )
        return DocumentDetailOut(
            id=str(doc.id),
            title=doc.title,
            sensitivity=doc.sensitivity,
            source=doc.source or "",
            status=doc.status,
            space=doc.space or "",
            url=doc.url or "",
            external_id=doc.external_id or "",
            source_version=doc.source_version,
            content_hash=doc.content_hash or "",
            indexed_at=doc.indexed_at,
            last_updated=doc.last_updated,
            chunks=[
                DocumentChunkPreviewOut(
                    chunk_index=c.chunk_index,
                    text_preview=(c.text or "")[:_PREVIEW_CHARS],
                    token_count=c.token_count,
                )
                for c in chunks
            ],
            chunk_count=len(chunks),
        )
