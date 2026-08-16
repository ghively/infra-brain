"""KnowledgeAgent — Confluence → RAG knowledge-store ingestion collector.

TRK-067 (RAG revival). A deterministic ``ETLConnector`` that pages through
Confluence spaces (GET-only, via ``tools.http_readonly.readonly_get`` — the R2
layer-1 structural read-only requirement; never raw httpx for reads), then in
its detail-write phase upserts ``Document`` rows and re-embeds their text into
``DocumentChunk`` rows for semantic search (``embeddings.search_knowledge``).

Design notes:
  * SELF-SKIP WHEN UNCONFIGURED — with ``rag_enabled`` off, or Confluence
    unconfigured, the agent raises ``CollectorSkipped`` (no DB writes, no HTTP).
    This keeps the flag-off deployment dark and lets tests run without
    Confluence. It previously returned an empty ``CollectOutcome``, which
    records status="completed" with 0 rows — the R3 monitor correctly reads
    that as "silently empty?" and had escalated it 200x in a row on a fleet
    where ``rag_enabled`` is on but ``confluence_url`` was never set. "skipped"
    is the outcome that actually describes an absent dependency.
  * INCREMENTAL — a page whose ``content_hash`` is unchanged since the last run
    is skipped (no re-embed). Pages that vanish from a scanned space are marked
    ``status="stale"`` (never deleted).
  * F-007 — every per-space/per-page fetch failure is recorded in
    ``CollectOutcome.errors`` (→ status "partial"), never silently dropped.
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

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
from infra_brain.tools.http_readonly import readonly_get

logger = logging.getLogger(__name__)

_PAGE_LIMIT = 50

# TRK-122 R5: block-level tags whose end emits a paragraph boundary, and heading
# tags rendered as markdown ``#`` lines, so structure survives into chunking.
_BLOCK_END_TAGS = {"p", "li", "tr", "div", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# TRK-122 R5 (Finding 8): table cell end tags emit a pipe delimiter so cell
# boundaries survive extraction and the table-aware chunker can see columns.
_CELL_END_TAGS = {"td", "th"}


class _TextExtractor(HTMLParser):
    """Collect *structured* text from Confluence storage-format XHTML (stdlib only).

    bs4/lxml are NOT project dependencies (checked pyproject), so HTML is
    stripped with the standard-library parser rather than adding one.

    TRK-122 R5: previously every text node was joined with a single space,
    destroying paragraph/heading boundaries before the splitter ever ran. Now
    block-level end tags emit ``\\n\\n`` and heading tags are rendered as
    ``#``-prefixed markdown lines, giving the header-aware chunker (``chunk_markdown``)
    real structure to split on. Table cell end tags (``td``/``th``) emit ``" | "``
    so cell boundaries survive into ``chunk_table_rows``.

    Fable Finding 3: Confluence code macros put their body inside a
    ``<![CDATA[...]]>`` section, which the default ``HTMLParser`` silently drops.
    ``unknown_decl`` now captures CDATA content and stores it as an already-formed
    fenced segment, preserving intra-code newlines (it bypasses the space-join /
    strip that regular text nodes go through).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._row_cols = 0
        self._row_is_header = False

    def handle_starttag(self, tag: str, attrs) -> None:
        level = _HEADING_LEVELS.get(tag)
        if level is not None:
            # Start a fresh markdown heading line.
            self._parts.append("\n\n" + "#" * level)
        elif tag == "tr":
            # Begin a markdown table row with a leading pipe; track columns so a
            # header row (<th> cells) can emit a separator row on close.
            self._row_cols = 0
            self._row_is_header = False
            self._parts.append("\n|")

    def handle_endtag(self, tag: str) -> None:
        # Finding 8: close each table cell with a trailing pipe so cell
        # boundaries survive extraction and the table-aware chunker sees columns.
        if tag in _CELL_END_TAGS:
            self._row_cols += 1
            if tag == "th":
                self._row_is_header = True
            self._parts.append(" |")
            return
        if tag == "tr":
            # A header row (<th> cells) gets a markdown separator row so the
            # extracted text is a real markdown table that routes to
            # chunk_table_rows via looks_like_markdown_table().
            if self._row_is_header and self._row_cols:
                self._parts.append("\n|" + "---|" * self._row_cols)
            self._parts.append("\n\n")
            return
        if tag in _BLOCK_END_TAGS:
            self._parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if data and data.strip():
            self._parts.append(data.strip())

    def unknown_decl(self, data: str) -> None:
        # Confluence code/config macros store their body as <![CDATA[...]]>.
        # HTMLParser routes marked sections here; ``data`` is e.g.
        # "CDATA[systemctl restart nginx". Capture the inner body wrapped in a
        # fenced block so chunk_markdown treats it as a distinct code segment,
        # and store it fully formed so the space-join in ``text`` cannot mangle
        # its internal newlines.
        if data.startswith("CDATA["):
            inner = data[len("CDATA[") :]
            if inner.endswith("]"):  # some parsers include the trailing bracket
                inner = inner[:-1]
            if inner.strip():
                self._parts.append("\n\n```\n" + inner + "\n```\n\n")

    @property
    def text(self) -> str:
        # Parts are space-joined (inline runs), then whitespace around the
        # explicit "\n\n" block markers is collapsed to clean paragraph breaks.
        raw = " ".join(self._parts)
        raw = re.sub(r"[ \t]*\n\n[ \t]*", "\n\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _strip_html(html_text: str) -> str:
    """Reduce storage-format XHTML to a structured plain-text (markdown-ish) string."""
    if not html_text:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html_text)
    except Exception:  # a malformed fragment must not abort the whole run
        logger.debug("KnowledgeAgent: HTML parse fell back to raw text", exc_info=True)
        return html_text
    return parser.text


class KnowledgeAgent(ETLConnector):
    spec = AgentSpec(
        domain="knowledge",
        tier=Tier.COLLECTOR,
        schedule="35 2 * * *",
        max_staleness=timedelta(hours=26),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        self._pages: list[dict] = []
        self._scanned_spaces: list[str] = []

        # Unconfigured -> self-skip, not an empty success. Returning an empty
        # CollectOutcome recorded status="completed" with 0 rows, which the R3
        # completeness monitor correctly reads as "silently empty?" — it had
        # escalated 200x in a row for an integration that was simply never set
        # up (rag_enabled is on here, but confluence_url is ""). "skipped" is
        # the outcome that describes an absent dependency, and it is what every
        # sibling collector raises.
        if not self.settings.rag_enabled or not self.settings.confluence_url:
            reason = (
                "rag_enabled is off"
                if not self.settings.rag_enabled
                else "confluence_url not configured; set it to enable Confluence ingestion"
            )
            logger.info("KnowledgeAgent: skipping — %s", reason)
            raise CollectorSkipped(reason)

        spaces = [
            s.strip() for s in (self.settings.confluence_rag_spaces or "").split(",") if s.strip()
        ]
        if not spaces:
            spaces = [self.settings.confluence_space_key]

        errors: list[str] = []
        auth = (self.settings.confluence_user_email, self.settings.confluence_token)
        base_url = self.settings.confluence_url.rstrip("/")

        for space in spaces:
            space_ok = True
            start = 0
            while True:
                try:
                    resp = readonly_get(
                        f"{base_url}/rest/api/content",
                        params={
                            "spaceKey": space,
                            "type": "page",
                            "expand": "body.storage,version",
                            "start": start,
                            "limit": _PAGE_LIMIT,
                        },
                        auth=auth,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    # F-007: record, do not silently drop; abandon this space
                    # (its data is now incomplete, so it is NOT marked scanned
                    # and no stale-sweep runs against it).
                    msg = f"confluence fetch failed for space {space} (start={start}): {exc}"
                    logger.warning("KnowledgeAgent: %s", msg)
                    errors.append(msg)
                    space_ok = False
                    break

                results = data.get("results", []) or []
                for page in results:
                    self._record_page(page, space, base_url)

                links = data.get("_links", {}) or {}
                if links.get("next") and results:
                    start += _PAGE_LIMIT
                else:
                    break

            if space_ok:
                self._scanned_spaces.append(space)

        items = [
            {
                "name": page["id"],
                "type": "confluence_page",
                "data": {
                    "title": page["title"],
                    "space": page["space"],
                    "url": page["url"],
                    "version": page["version"],
                    "content_hash": page["content_hash"],
                },
            }
            for page in self._pages
        ]
        return CollectOutcome(items=items, errors=errors)

    def _record_page(self, page: dict, space: str, base_url: str) -> None:
        """Normalize one Confluence content result and stash it for the writer."""
        page_id = str(page.get("id", "")) if page.get("id") is not None else ""
        if not page_id:
            return
        storage_html = (page.get("body", {}) or {}).get("storage", {}).get("value", "") or ""
        webui = (page.get("_links", {}) or {}).get("webui", "") or ""
        url = f"{base_url}{webui}" if webui else ""
        version = (page.get("version", {}) or {}).get("number")
        content_hash = hashlib.sha256(storage_html.encode("utf-8")).hexdigest()
        self._pages.append(
            {
                "id": page_id,
                "title": page.get("title", "") or "",
                "space": space,
                "url": url,
                "version": version,
                "content_hash": content_hash,
                "storage_html": storage_html,
            }
        )

    # --- detail-write phase: documents + document_chunks -------------------

    def _detail_writers(self, scope, result):
        # Surface any structural detail-write failure on the run (never silent)
        # via ETLConnector.run()'s _write_details.
        return [self._write_knowledge_documents]

    def _write_knowledge_documents(self) -> int:
        pages = getattr(self, "_pages", None) or []
        scanned_spaces = getattr(self, "_scanned_spaces", None) or []
        seen_ids: set[str] = set()
        chunk_rows_written = 0

        with get_session() as session:
            for page in pages:
                seen_ids.add(page["id"])
                existing = (
                    session.query(Document)
                    .filter_by(external_id=page["id"], space=page["space"])
                    .first()
                )
                # Incremental: unchanged content -> skip (no re-embed). Finding 4:
                # a page previously marked "stale" (e.g. temporarily left a scanned
                # space) that returns byte-identical must be RESURRECTED to
                # "current" before the skip — otherwise it stays permanently
                # excluded from search_knowledge (which filters status != "stale").
                if existing is not None and existing.content_hash == page["content_hash"]:
                    if existing.status != "current":
                        existing.status = "current"
                        existing.last_updated = datetime.now(timezone.utc)
                    continue

                doc = self._upsert_document(session, existing, page)
                if existing is not None:
                    session.query(DocumentChunk).filter_by(document_id=doc.id).delete()

                text = _strip_html(page["storage_html"])
                label = page["title"] or page["id"]
                # TRK-122 R5: header-aware chunking with a breadcrumb prefix
                # (doc title > heading path) embedded in each chunk. Finding 8:
                # table-shaped pages route to the table-aware chunker (mirroring
                # LocalDocsAgent) so a page that is mostly a table keeps its columns
                # instead of flattening to prose. Finding 6: chunk size is config-
                # driven, not a hardcoded model window.
                chunk_size = self.settings.rag_chunk_size
                if looks_like_markdown_table(text):
                    chunks = chunk_table_rows(text, label, chunk_size=chunk_size)
                else:
                    chunks = chunk_markdown(text, label, chunk_size=chunk_size)
                if chunks:
                    vectors = get_embeddings().embed_documents(chunks)
                    # Finding 5: a gateway returning fewer vectors than chunks is a
                    # structural malfunction — fail loudly (surfaced as a failed run
                    # by _write_details) rather than silently storing None
                    # embeddings that search_knowledge then drops.
                    if len(vectors) != len(chunks):
                        raise RuntimeError(
                            f"embedding count mismatch for page {page['id']!r}: "
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

            # STALE MARKING: only within spaces we FULLY scanned this run. A
            # confluence Document whose external_id was not seen is marked
            # stale (never deleted). Skipped entirely when no space was fully
            # scanned (e.g. the disabled no-op) so nothing is spuriously staled.
            if scanned_spaces:
                stale_candidates = (
                    session.query(Document)
                    .filter(Document.source == "confluence")
                    .filter(Document.space.in_(scanned_spaces))
                    .all()
                )
                for doc in stale_candidates:
                    if doc.external_id not in seen_ids and doc.status != "stale":
                        doc.status = "stale"

            session.commit()
        return chunk_rows_written

    def _upsert_document(self, session, existing: "Document | None", page: dict) -> "Document":
        """Insert or update the Document row for a page, returning the row."""
        now = datetime.now(timezone.utc)
        if existing is None:
            doc = Document(
                title=page["title"],
                source="confluence",
                external_id=page["id"],
                space=page["space"],
                url=page["url"],
                source_version=page["version"],
                content_hash=page["content_hash"],
                last_updated=now,
                status="current",
            )
            session.add(doc)
            session.flush()  # assign doc.id before chunk rows reference it
            return doc
        existing.title = page["title"]
        existing.source = "confluence"
        existing.url = page["url"]
        existing.source_version = page["version"]
        existing.content_hash = page["content_hash"]
        existing.last_updated = now
        existing.status = "current"
        return existing
