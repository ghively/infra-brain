"""LocalDocsAgent — curated repo-documentation → RAG knowledge-store collector.

TRK-122 R1. A sibling of ``KnowledgeAgent`` (NOT a second mode bolted onto it):
where KnowledgeAgent ingests Confluence pages, this ingests a *curated allowlist*
of in-repo markdown (runbooks, decision records, architecture docs) into the same
``Document``/``DocumentChunk`` RAG store, so ``search_knowledge`` retrieves the
institutional knowledge that already lives in the repository.

Design mirrors KnowledgeAgent exactly:
  * GRACEFUL NO-OP — with ``rag_enabled`` off OR ``repo_docs_root`` unset, the
    agent is inert (empty ``CollectOutcome``, no globbing, no DB writes).
  * INCREMENTAL — a file whose ``content_hash`` (sha256 of its bytes) is unchanged
    since the last run is skipped (no re-embed).
  * STALE MARKING — a previously-ingested ``source="local_docs"`` Document whose
    file is gone this run is marked ``status="stale"`` (never deleted), scoped to
    THIS source so Confluence's own stale sweep is untouched.
  * F-007 — every per-file read/chunk failure is recorded in
    ``CollectOutcome.errors`` (→ status "partial"), never silently dropped.

No schema migration is needed: ``Document.source`` is a free string,
``external_id`` holds the repo-relative path, ``space`` holds the subdir label,
``content_hash`` is the source-agnostic incremental key, ``source_version`` holds
the file mtime epoch (nullable).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from infra_brain.db.models import Document, DocumentChunk
from infra_brain.db.session import get_session
from infra_brain.embeddings import get_embeddings
from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.rag.ingest import (
    chunk_markdown,
    chunk_table_rows,
    estimate_tokens,
    looks_like_markdown_table,
)

logger = logging.getLogger(__name__)

_SOURCE = "local_docs"

# --- Curated in-scope allowlist (NOT a blanket docs/**) -----------------------
# (glob relative to repo_docs_root, space label). ``docs/*.md`` is top-level
# only; the subdir globs are recursive. Encoded as real data, not comments.
_ALLOW_GLOBS: tuple[tuple[str, str], ...] = (
    ("docs/*.md", "docs"),  # top-level docs only (non-recursive)
    ("docs/decisions/**/*.md", "docs/decisions"),
    ("docs/runbooks/**/*.md", "docs/runbooks"),
    ("docs/specs/**/*.md", "docs/specs"),
    ("docs/diagnostics/**/*.md", "docs/diagnostics"),
    # Populated by scripts/sync_fleet_wiki_knowledge.py — a one-off, manually-run
    # sync that copies curated markdown (runbooks/incidents/compliance/concepts/
    # confluence) from the sibling fleet-wiki repo into this repo tree. Not
    # infra-brain's own curated docs (those stay under docs/runbooks/ etc.) —
    # kept in a separate subtree so the two are never confused.
    ("docs/knowledge-sync/**/*.md", "docs/knowledge-sync"),
)
# Root-level docs (the durable, institutional ones). Filtered against what
# actually exists at collect time.
_ALLOW_ROOT_FILES: tuple[str, ...] = ("AGENTS.md", "README.md", "DEPLOYMENT.md", "CHANGELOG.md")

# --- Explicit denylist (defense-in-depth; reasons, not just a comment) --------
#   docs/agents/, docs/audit/ — CLAUDE.md declares these "frozen historical
#     evidence"; retrieving stale historical analysis as if current is exactly
#     the "confidently wrong" failure this review exists to prevent.
#   docs/superpowers/ — historical planning notes, same reasoning.
#   CLAUDE.md — operational instructions TO Claude Code (merge policy, test
#     commands, orchestrator routing), not institutional infra knowledge; its
#     durable factual content already lives in READONLY-MODEL.md/ARCHITECTURE.md
#     which ARE in scope.
_DENY_DIR_PREFIXES: tuple[str, ...] = ("docs/agents/", "docs/audit/", "docs/superpowers/")
_DENY_FILES: frozenset[str] = frozenset({"CLAUDE.md"})

# Filenames whose content is a fact-dense pipe-delimited table (TRACKER.md-shaped)
# and should be chunked row-group-wise so each finding row stays retrievable.
_TABLE_SHAPED_FILES: frozenset[str] = frozenset({"TRACKER.md", "BACKLOG.md"})


def _is_denied(rel_path: str) -> bool:
    posix = rel_path.replace("\\", "/")
    if posix in _DENY_FILES or Path(posix).name in _DENY_FILES:
        return True
    return any(posix.startswith(prefix) for prefix in _DENY_DIR_PREFIXES)


def _title_for(rel_path: str, text: str) -> str:
    """First markdown H1 in the file, else the file's basename."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or Path(rel_path).name
    return Path(rel_path).name


class LocalDocsAgent(ETLConnector):
    spec = AgentSpec(
        domain="local_docs",
        tier=Tier.COLLECTOR,
        # Offset from KnowledgeAgent's 35 2 (Confluence) so the two RAG
        # collectors don't contend for the embedding gateway at the same minute.
        # Hour 3, not 2: graph_maintenance's full pass (TRK-117) also runs at
        # minute :50 (every even hour) — an odd hour keeps this daily run off
        # that slot entirely rather than picking a different minute.
        schedule="50 3 * * *",
        max_staleness=timedelta(hours=26),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        self._docs: list[dict] = []
        self._scanned = False

        # M-6: self-skip, not an empty success — mirrors KnowledgeAgent /
        # SaaSInventoryAgent exactly. Returning an empty CollectOutcome here
        # recorded status="completed" with 0 rows, which the R3 completeness
        # monitor correctly reads as "silently empty?" — the same false-alarm
        # pattern (200x/168x escalations) that CollectorSkipped was
        # introduced to fix on those two siblings; this collector was simply
        # never aligned to it. "skipped" is the outcome that actually
        # describes flag-off/unconfigured, not a genuinely quiet run — see
        # ZERO_OK_DOMAINS in callbacks/freshness.py for the DIFFERENT case
        # (a collector that ran and legitimately found nothing), which this
        # is not.
        if not self.settings.rag_enabled or not self.settings.repo_docs_root:
            reason = (
                "rag_enabled is off"
                if not self.settings.rag_enabled
                else "repo_docs_root not configured; set it to enable local-docs ingestion"
            )
            logger.info("LocalDocsAgent: skipping — %s", reason)
            raise CollectorSkipped(reason)

        root = Path(self.settings.repo_docs_root)
        if not root.is_dir():
            msg = f"repo_docs_root does not exist or is not a directory: {root}"
            logger.warning("LocalDocsAgent: %s", msg)
            return CollectOutcome(items=[], errors=[msg])

        errors: list[str] = []
        seen_rel: set[str] = set()

        # Build the curated candidate set from the allowlist.
        candidates: list[tuple[Path, str]] = []
        for pattern, space in _ALLOW_GLOBS:
            for path in sorted(root.glob(pattern)):
                if path.is_file():
                    candidates.append((path, space))
        for name in _ALLOW_ROOT_FILES:
            path = root / name
            if path.is_file():
                candidates.append((path, "root"))

        for path, space in candidates:
            try:
                rel_path = path.relative_to(root).as_posix()
            except ValueError:
                rel_path = path.name
            if _is_denied(rel_path) or rel_path in seen_rel:
                continue
            seen_rel.add(rel_path)
            try:
                self._record_file(path, rel_path, space, root)
            except Exception as exc:  # F-007: record, never silently drop
                msg = f"local-docs read failed for {rel_path}: {exc}"
                logger.warning("LocalDocsAgent: %s", msg)
                errors.append(msg)

        # A clean pass (no read error) authorizes the stale sweep.
        self._scanned = not errors

        items = [
            {
                "name": doc["external_id"],
                "type": "repo_doc",
                "data": {
                    "title": doc["title"],
                    "space": doc["space"],
                    "url": doc["url"],
                    "content_hash": doc["content_hash"],
                },
            }
            for doc in self._docs
        ]
        return CollectOutcome(items=items, errors=errors)

    def _record_file(self, path: Path, rel_path: str, space: str, root: Path) -> None:
        raw = path.read_bytes()
        content_hash = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", errors="replace")
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            mtime = None
        self._docs.append(
            {
                "external_id": rel_path,
                "title": _title_for(rel_path, text),
                "space": space,
                "url": self._blob_url(rel_path),
                "source_version": mtime,
                "content_hash": content_hash,
                "text": text,
                "rel_path": rel_path,
            }
        )

    def _blob_url(self, rel_path: str) -> str:
        """GitLab blob URL when gitlab_url is configured, else the relative path."""
        base = (self.settings.gitlab_url or "").rstrip("/")
        if not base:
            return rel_path
        return f"{base}/-/blob/master/{rel_path}"

    # --- detail-write phase: documents + document_chunks -------------------

    def _detail_writers(self, scope, result):
        return [self._write_local_docs]

    def _write_local_docs(self) -> int:
        docs = getattr(self, "_docs", None) or []
        scanned = getattr(self, "_scanned", False)
        seen_ids: set[str] = set()
        chunk_rows_written = 0

        with get_session() as session:
            for doc_rec in docs:
                seen_ids.add(doc_rec["external_id"])
                existing = (
                    session.query(Document)
                    .filter_by(external_id=doc_rec["external_id"], source=_SOURCE)
                    .first()
                )
                # Incremental: unchanged content -> skip (no re-embed). Finding 4:
                # a doc previously marked "stale" (e.g. the file was briefly
                # deleted) that returns byte-identical must be RESURRECTED to
                # "current" before the skip — otherwise it stays permanently
                # excluded from search_knowledge (which filters status != "stale").
                if existing is not None and existing.content_hash == doc_rec["content_hash"]:
                    if existing.status != "current":
                        existing.status = "current"
                        existing.last_updated = datetime.now(timezone.utc)
                    continue

                doc = self._upsert_document(session, existing, doc_rec)
                if existing is not None:
                    session.query(DocumentChunk).filter_by(document_id=doc.id).delete()

                chunks = self._chunk(doc_rec)
                if chunks:
                    vectors = get_embeddings().embed_documents(chunks)
                    # Finding 5: fewer vectors than chunks is a structural gateway
                    # malfunction — fail loudly (surfaced as a failed run by
                    # _write_details) rather than silently storing None embeddings
                    # that search_knowledge then drops.
                    if len(vectors) != len(chunks):
                        raise RuntimeError(
                            f"embedding count mismatch for {doc_rec['external_id']!r}: "
                            f"{len(vectors)} vectors for {len(chunks)} chunks"
                        )
                    for idx, chunk_text in enumerate(chunks):
                        embedding = vectors[idx]
                        session.add(
                            DocumentChunk(
                                document_id=doc.id,
                                chunk_index=idx,
                                text=chunk_text,
                                embedding=embedding,
                                token_count=estimate_tokens(chunk_text),
                            )
                        )
                        chunk_rows_written += 1

            # STALE MARKING — scoped to source="local_docs" so Confluence's own
            # stale sweep is untouched. Only runs after a clean scan.
            if scanned:
                for stale in session.query(Document).filter(Document.source == _SOURCE).all():
                    if stale.external_id not in seen_ids and stale.status != "stale":
                        stale.status = "stale"

            session.commit()
        return chunk_rows_written

    def _chunk(self, doc_rec: dict) -> list[str]:
        text = doc_rec["text"]
        label = doc_rec["title"] or doc_rec["external_id"]
        # TRK-122 R5: TRACKER.md-shaped files chunk row-group-wise (header on
        # every chunk); keyed on filename, guarded by an actual table check.
        # Finding 6: chunk size is config-driven, not a hardcoded model window.
        chunk_size = self.settings.rag_chunk_size
        name = Path(doc_rec["rel_path"]).name
        if name in _TABLE_SHAPED_FILES and looks_like_markdown_table(text):
            return chunk_table_rows(text, label, chunk_size=chunk_size)
        return chunk_markdown(text, label, chunk_size=chunk_size)

    def _upsert_document(self, session, existing: "Document | None", doc_rec: dict) -> "Document":
        now = datetime.now(timezone.utc)
        if existing is None:
            doc = Document(
                title=doc_rec["title"],
                source=_SOURCE,
                external_id=doc_rec["external_id"],
                space=doc_rec["space"],
                url=doc_rec["url"],
                source_version=doc_rec["source_version"],
                content_hash=doc_rec["content_hash"],
                last_updated=now,
                status="current",
            )
            session.add(doc)
            session.flush()
            return doc
        existing.title = doc_rec["title"]
        existing.source = _SOURCE
        existing.space = doc_rec["space"]
        existing.url = doc_rec["url"]
        existing.source_version = doc_rec["source_version"]
        existing.content_hash = doc_rec["content_hash"]
        existing.last_updated = now
        existing.status = "current"
        return existing
