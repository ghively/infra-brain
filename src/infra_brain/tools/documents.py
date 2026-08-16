"""infra_brain.tools.documents — MCP-facing document ingest for client-origin
docs (convergence plan P4.3c).

Distinct from the scheduled path: ``KnowledgeAgent``/``LocalDocsAgent`` crawl
Confluence/local sources on a schedule and populate ``documents`` themselves
once ``rag_enabled`` is on. THIS module is for the other path — a client that
already holds a document in hand (a headless runner, Claude Code itself, ...)
pushing it into the knowledge store right now, rather than waiting for the
next scheduled sweep.

These are plain functions, not LangChain ``@tool``s and not yet wired to the
MCP tool decorator — that wiring (and the actual attribution derivation from
the authenticated MCP key, mirroring ``docs/MCP_SERVER.md``'s "Attribution"
convention: ``mcp:<key name>``) happens in a later, separate integration
pass. What lives here is the mutation-safety that belongs in the function
itself regardless of caller: attribution (``client_origin``/``ingested_by``)
is a required, non-blank argument rather than something silently defaulted
or trusted-if-present, and duplicate ingests of the same content are
detected server-side rather than trusted to the caller's own bookkeeping.

No external network call is made here (the document's content/hash is
already in the caller's hand) — so, unlike ``tools/gitlab_mr.py`` or
``tools/confluence.py``, there is nothing to route through
``callbacks/write_gate.py.gate_external_write``; that gate exists for
outbound HTTP writes, and this module only ever writes to the local DB.

``client_origin``/``ingested_by`` are columns being added to the ``Document``
model concurrently by a parallel change (see module docstring note in the
convergence plan, P4.3c). This module deliberately sets them via
``setattr()`` after construction rather than as ``Document(...)`` constructor
kwargs: SQLAlchemy's declarative ``__init__`` only accepts kwargs for columns
that exist on the mapped class at import time, so passing them as
constructor kwargs would raise ``TypeError`` today (columns not yet mapped)
even though the same code will correctly persist them once the other
change lands. ``setattr`` degrades gracefully in the interim (a harmless,
non-persisted instance attribute) and needs no further edits once the
columns exist.
"""

from __future__ import annotations

import uuid
from typing import Any

from infra_brain.db.models import Document
from infra_brain.db.session import get_session

# Allowlist for update_document_metadata — deliberately narrow. Widening this
# to arbitrary column names would turn the function into a general-purpose
# arbitrary-column-write primitive; keep it to the four provenance/link
# fields this tool actually needs to correct after the fact.
_UPDATABLE_FIELDS = frozenset({"url", "external_id", "client_origin", "ingested_by"})


def _require_nonblank(name: str, value: Any) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{name} is required and cannot be blank")
    return value


def _coerce_document_id(document_id: Any) -> uuid.UUID:
    if isinstance(document_id, uuid.UUID):
        return document_id
    try:
        return uuid.UUID(str(document_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Document {document_id!r} not found") from exc


def _serialize(document: Document, *, already_existed: bool = False) -> dict:
    out = {
        "id": str(document.id),
        "title": document.title,
        "sensitivity": document.sensitivity,
        "source": document.source,
        "status": document.status,
        "url": document.url,
        "external_id": document.external_id,
        "content_hash": document.content_hash,
        # New provenance columns (added concurrently — see module docstring).
        # getattr(..., None) keeps this function safe to import/run before
        # those columns exist on Document; once they do, real values flow
        # through unchanged.
        "client_origin": getattr(document, "client_origin", None),
        "ingested_by": getattr(document, "ingested_by", None),
        "indexed_at": document.indexed_at.isoformat() if document.indexed_at else None,
    }
    if already_existed:
        out["_already_existed"] = True
    return out


def ingest_document(
    title: str,
    content_hash: str,
    source: str,
    client_origin: str,
    ingested_by: str,
    url: str | None = None,
    external_id: str | None = None,
) -> dict:
    """Write a new ``Document`` row for a client-supplied document.

    Idempotent by ``content_hash``: if a ``Document`` with the same
    ``content_hash`` already exists, that row is returned annotated with
    ``_already_existed: True`` instead of writing a duplicate (mirrors the
    idempotent-by-title pattern in ``tools/gitlab_mr.py``'s MR-lookup and
    ``agent/scripts/lib/gitlab_client.py``'s ``create_issue``).

    Args:
        title: Human-readable document title.
        content_hash: Caller-computed content hash (the idempotency key —
            required and non-blank, not merely nullable-in-the-DB).
        source: Where this document came from, e.g. "manual", "headless-runner".
        client_origin: Identifies the pushing client, e.g. "claude-code",
            "headless-runner". Required — this is attribution, not a free-text
            frill; do not default it.
        ingested_by: Attribution for who/what performed the ingest. Required
            and validated non-blank here; deriving it from an authenticated
            identity rather than trusting arbitrary caller text is the later
            MCP-wiring pass's job (see docs/MCP_SERVER.md's "Attribution"
            convention) — this function's contract is simply that it can
            never be silently blank.
        url: Optional source URL.
        external_id: Optional external system id (e.g. Confluence page id).

    Returns:
        A dict of the (possibly pre-existing) Document's fields. Sets
        ``_already_existed: True`` only on the duplicate-hash path.

    Raises:
        ValueError: if title/content_hash/source/client_origin/ingested_by
            is missing or blank.
    """
    _require_nonblank("title", title)
    _require_nonblank("content_hash", content_hash)
    _require_nonblank("source", source)
    _require_nonblank("client_origin", client_origin)
    _require_nonblank("ingested_by", ingested_by)

    with get_session() as session:
        existing = (
            session.query(Document).filter(Document.content_hash == content_hash).first()
        )
        if existing is not None:
            return _serialize(existing, already_existed=True)

        document = Document(
            title=title,
            source=source,
            content_hash=content_hash,
            sensitivity="internal",  # vestigial per D7 — no new branching on it
            url=url,
            external_id=external_id,
            status="current",
        )
        document.client_origin = client_origin
        document.ingested_by = ingested_by

        session.add(document)
        session.commit()
        session.refresh(document)
        return _serialize(document)


def update_document_metadata(document_id: Any, **fields: Any) -> dict:
    """Update only url/external_id/client_origin/ingested_by on a Document.

    Allowlisted to exactly those four fields so this can't become an
    arbitrary-column-write primitive — any other field name raises
    ``ValueError`` before touching the DB.

    Args:
        document_id: The Document's id (str or uuid.UUID).
        **fields: Any subset of url/external_id/client_origin/ingested_by.

    Returns:
        The updated Document's fields as a dict.

    Raises:
        ValueError: if an unlisted field name is passed, or if no Document
            with that id exists.
    """
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(
            f"Cannot update field(s) {sorted(unknown)} — allowed fields are "
            f"{sorted(_UPDATABLE_FIELDS)}"
        )

    did = _coerce_document_id(document_id)

    with get_session() as session:
        document = session.get(Document, did)
        if document is None:
            raise ValueError(f"Document {document_id!r} not found")

        for field, value in fields.items():
            setattr(document, field, value)

        session.commit()
        session.refresh(document)
        return _serialize(document)
