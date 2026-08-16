"""PersonalWikiAgent — Hermes personal-wiki -> fenced RAG knowledge-store ingestion.

A second sibling of KnowledgeAgent/LocalDocsAgent (NOT a mode bolted onto
either): this ingests a curated subset of a SEPARATE personal AI agent
system's ("Hermes") local wiki at ``settings.personal_wiki_root`` into the
same ``Document``/``DocumentChunk`` RAG store LocalDocsAgent and KnowledgeAgent
write to. Unlike those two, this content is deliberately NOT infra-scoped —
it's personal notes/journal/entity content from a separate system that happens
to be readable on the same host filesystem.

FENCING (the reason this agent needs its own file, not just another allowlist
row on LocalDocsAgent): every row this agent writes carries
``Document.source == "personal_wiki"``. ``infra_brain.embeddings.search_knowledge``
excludes that source from results and from the similarity ranking itself
unless a caller explicitly passes ``include_personal=True`` — there is
currently NO other access-control layer on ``Document``/``DocumentChunk`` or on
the ``search_knowledge`` MCP tool, so this source tag is the ONLY thing
preventing personal-wiki content from being uniformly retrievable alongside
infra content. See ``embeddings.py``'s ``_PERSONAL_DOMAIN_SOURCES``.

Design notes (mirrors LocalDocsAgent exactly, see that file for the fuller
rationale on each point):
  * GRACEFUL NO-OP — inert (empty ``CollectOutcome``, no globbing, no DB
    writes) unless ALL THREE of ``rag_enabled``, ``personal_wiki_ingest_enabled``,
    and ``personal_wiki_root`` are set. ``personal_wiki_ingest_enabled`` is a
    SEPARATE flag from ``rag_enabled`` on purpose — flipping the generic RAG
    master switch must never be sufficient, on its own, to start indexing a
    person's private notes. This is real personal data; err toward requiring
    an explicit, deliberate opt-in rather than an automatic one.
  * SCOPE — walks exactly the subdirectories of the configured root listed in
    ``_ALLOWED_SUBDIRS`` below: ``entities/``, ``wiki/``, ``raw/``,
    ``intelligence/``, ``concepts/``, ``comparisons/``, ``lessons/``,
    ``runbooks/``, ``references/``, and ``incidents/`` — all durable curated
    (or dated-raw) knowledge per that system's own docs. ``runtime/`` is
    deliberately NEVER walked — it is that system's cron-refreshed live-state
    scratch data, not durable knowledge worth indexing. Any other top-level
    subdirectory (``_archive/``, ``_meta/``, etc.) is out of scope; only
    markdown (``*.md``) files are ingested — a handful of JSON/JSONL reference
    dumps live under these trees too, but they are not prose suited to the
    markdown-aware chunkers and are skipped.
  * INCREMENTAL / STALE MARKING — identical contract to LocalDocsAgent,
    scoped to ``source="personal_wiki"`` so neither of the other two
    collectors' stale sweeps are touched, and vice versa.
  * F-007 — every per-file read/chunk failure is recorded in
    ``CollectOutcome.errors``, never silently dropped.
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

_SOURCE = "personal_wiki"

# Durable-knowledge subdirectories walked, each recursively, markdown only.
# "runtime" is intentionally absent — cron-regenerated live-state scratch data.
# Expanded 2026-08-02 from the original (entities, wiki, raw) to also cover
# the wiki's other durable-knowledge trees (curated notes, not raw/private
# journal content) -- intelligence/concepts/comparisons in particular are the
# actual LLM-knowledge content this ingestion pass was built to surface.
_ALLOWED_SUBDIRS: tuple[str, ...] = (
    "entities",
    "wiki",
    "raw",
    "intelligence",
    "concepts",
    "comparisons",
    "lessons",
    "runbooks",
    "references",
    "incidents",
)


def _title_for(rel_path: str, text: str) -> str:
    """First markdown H1 in the file, else the file's basename."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or Path(rel_path).name
    return Path(rel_path).name


class PersonalWikiAgent(ETLConnector):
    spec = AgentSpec(
        domain="personal_wiki",
        tier=Tier.COLLECTOR,
        # Hour 4, minute 35: distinct from both other RAG-embedding-calling
        # collectors (KnowledgeAgent 2:35, LocalDocsAgent 3:50) so all three
        # never contend for the embedding gateway at the same minute, and
        # distinct from every other hour-4 slot (drift_learning 4:00,
        # container_registry 4:15, retention_prune 4:30, the ad-hoc
        # agent_task:eol-report 4:05, licensing 4:50) —
        # tests/scheduler/test_scheduler.py::test_no_exact_schedule_collisions
        # checks both _DEFAULT_SCHEDULES and _ADHOC_SCHEDULES.
        schedule="35 4 * * *",
        max_staleness=timedelta(hours=26),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        self._docs: list[dict] = []
        self._scanned = False

        # M-6: self-skip, not an empty success — mirrors KnowledgeAgent /
        # SaaSInventoryAgent / LocalDocsAgent exactly. Returning an empty
        # CollectOutcome here recorded status="completed" with 0 rows, which
        # the R3 completeness monitor correctly reads as "silently empty?" —
        # the same false-alarm pattern (200x/168x escalations) that
        # CollectorSkipped was introduced to fix on those two siblings; this
        # collector was simply never aligned to it. "skipped" is the outcome
        # that actually describes flag-off/unconfigured, not a genuinely
        # quiet run — see ZERO_OK_DOMAINS in callbacks/freshness.py for the
        # DIFFERENT case (a collector that ran and legitimately found
        # nothing), which this is not.
        #
        # personal_wiki_ingest_enabled is deliberately checked separately from
        # rag_enabled (see module docstring) rather than folded into one flag.
        settings = self.settings
        if not settings.rag_enabled:
            reason = "rag_enabled is off"
        elif not settings.personal_wiki_ingest_enabled:
            reason = "personal_wiki_ingest_enabled is off"
        elif not settings.personal_wiki_root:
            reason = "personal_wiki_root not configured; set it to enable personal-wiki ingestion"
        else:
            reason = None
        if reason is not None:
            logger.info("PersonalWikiAgent: skipping — %s", reason)
            raise CollectorSkipped(reason)

        root = Path(settings.personal_wiki_root).expanduser()
        if not root.is_dir():
            msg = f"personal_wiki_root does not exist or is not a directory: {root}"
            logger.warning("PersonalWikiAgent: %s", msg)
            return CollectOutcome(items=[], errors=[msg])

        errors: list[str] = []
        seen_rel: set[str] = set()

        candidates: list[tuple[Path, str]] = []
        for subdir in _ALLOWED_SUBDIRS:
            subroot = root / subdir
            if not subroot.is_dir():
                continue
            for path in sorted(subroot.glob("**/*.md")):
                if path.is_file():
                    candidates.append((path, subdir))

        for path, space in candidates:
            try:
                rel_path = path.relative_to(root).as_posix()
            except ValueError:
                rel_path = path.name
            if rel_path in seen_rel:
                continue
            seen_rel.add(rel_path)
            try:
                self._record_file(path, rel_path, space)
            except Exception as exc:  # F-007: record, never silently drop
                msg = f"personal-wiki read failed for {rel_path}: {exc}"
                logger.warning("PersonalWikiAgent: %s", msg)
                errors.append(msg)

        # A clean pass (no read error) authorizes the stale sweep.
        self._scanned = not errors

        items = [
            {
                "name": doc["external_id"],
                "type": "personal_wiki_doc",
                "data": {
                    "title": doc["title"],
                    "space": doc["space"],
                    "content_hash": doc["content_hash"],
                },
            }
            for doc in self._docs
        ]
        return CollectOutcome(items=items, errors=errors)

    def _record_file(self, path: Path, rel_path: str, space: str) -> None:
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
                # Not a fetchable URL (this is a local personal filesystem, not
                # a repo or Confluence) — a stable pseudo-URI for provenance/
                # citation that can never be confused with a real fetchable
                # local_docs/confluence URL.
                "url": f"personal-wiki:{rel_path}",
                "source_version": mtime,
                "content_hash": content_hash,
                "text": text,
                "rel_path": rel_path,
            }
        )

    # --- detail-write phase: documents + document_chunks -------------------

    def _detail_writers(self, scope, result):
        return [self._write_personal_wiki_docs]

    def _write_personal_wiki_docs(self) -> int:
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
                # Incremental: unchanged content -> skip (no re-embed), with the
                # same stale-resurrection fix as KnowledgeAgent/LocalDocsAgent
                # (Fable Finding 4) — a doc marked stale in a prior run that
                # returns byte-identical must flip back to "current" even on
                # the skip path, or it stays excluded from search_knowledge
                # forever (which filters status != "stale").
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

            # STALE MARKING — scoped to source="personal_wiki" so the other two
            # RAG collectors' stale sweeps are untouched. Only after a clean scan.
            if scanned:
                for stale in session.query(Document).filter(Document.source == _SOURCE).all():
                    if stale.external_id not in seen_ids and stale.status != "stale":
                        stale.status = "stale"

            session.commit()
        return chunk_rows_written

    def _chunk(self, doc_rec: dict) -> list[str]:
        text = doc_rec["text"]
        label = doc_rec["title"] or doc_rec["external_id"]
        chunk_size = self.settings.rag_chunk_size
        if looks_like_markdown_table(text):
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
