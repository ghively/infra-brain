"""Provider-agnostic embeddings factory for Infra Brain's RAG knowledge store.

Mirrors infra_brain.llm: every embedding model is built through get_embeddings()
so the provider swaps with one env var. Anthropic has no embeddings API, so when
LLM_PROVIDER=anthropic the embedding provider auto-resolves to bedrock (Titan).
Set EMBEDDING_PROVIDER to override. EMBEDDING_DIM must match the model and the
document_chunks.embedding column; changing it after indexing needs a migration +
re-embed.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from infra_brain.config import get_settings

logger = logging.getLogger(__name__)

VALID_PROVIDERS = ("bedrock", "openai")

# Some OpenAI-compatible gateways proxy a rate-limited upstream free tier
# (e.g. omniroute -> Gemini's embed_content free tier: 100 requests/minute,
# verified live -- a single embed_documents() call for a few hundred
# documents' chunks exceeds it in one shot, and retrying the SAME oversized
# batch just hits the same wall again; the openai SDK's built-in retry does
# NOT fix this, it only helps with transient/small overages). Batching to a
# safely-sub-quota size and pacing batches a beat past a full quota window
# is the only thing that actually works for THAT case -- but a local/
# unlimited endpoint (e.g. Ollama) has no such quota, so pacing must be
# opt-in (Settings.embedding_batch_pacing_seconds, default 0 = off), not a
# blanket default -- a hardcoded sleep here would make every gateway pay
# rate-limit-avoidance cost even when nothing is rate-limited (verified:
# this exact mistake made a local Ollama ingestion run needlessly ~20-40x
# slower before being caught). Batch SIZE stays fixed regardless -- it's
# harmless without pacing and still bounds any single HTTP request's size.
_MAX_TEXTS_PER_EMBED_BATCH = 80


class _TruncatedEmbeddings:
    """Wraps any LangChain ``Embeddings`` to truncate + L2-renormalize each
    vector to ``dim``, for gateways that can't be trusted to honor (or even
    accept) the OpenAI ``dimensions`` request param server-side.

    Valid only for Matryoshka-trained models (OpenAI's text-embedding-3-*
    and Google's gemini-embedding-* families both are) — truncating the
    leading N dimensions of such a model's output and renormalizing to unit
    length is the same operation the `dimensions` param performs server-side,
    just done client-side instead. Truncating a non-MRL model's output would
    silently degrade retrieval quality without erroring, so only wrap models
    known/documented to support this.

    Also batches embed_documents() calls to _MAX_TEXTS_PER_EMBED_BATCH, and
    optionally paces them (``pacing_seconds`` > 0) for gateways proxying a
    rate-limited upstream. Default pacing is 0 (off) -- see the module-level
    comment on _MAX_TEXTS_PER_EMBED_BATCH for why this must not default on.
    """

    def __init__(self, inner: Any, dim: int, pacing_seconds: float = 0) -> None:
        self._inner = inner
        self._dim = dim
        self._pacing_seconds = pacing_seconds

    def _truncate(self, vec: list[float]) -> list[float]:
        head = vec[: self._dim]
        norm = math.sqrt(sum(x * x for x in head))
        if norm == 0:
            return head
        return [x / norm for x in head]

    def embed_query(self, text: str) -> list[float]:
        return self._truncate(self._inner.embed_query(text))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        total = len(texts)
        for start in range(0, total, _MAX_TEXTS_PER_EMBED_BATCH):
            batch = texts[start : start + _MAX_TEXTS_PER_EMBED_BATCH]
            vectors = self._inner.embed_documents(batch)
            results.extend(self._truncate(v) for v in vectors)
            remaining = total - (start + len(batch))
            if remaining > 0 and self._pacing_seconds > 0:
                logger.info(
                    "embed_documents: paced batch %d-%d/%d done, %ss before next batch",
                    start,
                    start + len(batch),
                    total,
                    self._pacing_seconds,
                )
                time.sleep(self._pacing_seconds)
        return results


def resolve_provider(settings: Any) -> str:
    explicit = (settings.embedding_provider or "").lower()
    if explicit:
        if explicit not in VALID_PROVIDERS:
            raise ValueError(
                f"Unknown EMBEDDING_PROVIDER '{explicit}'. Valid: {list(VALID_PROVIDERS)} "
                "(anthropic has no embeddings API)."
            )
        return explicit
    chat = (settings.llm_provider or "anthropic").lower()
    return chat if chat in VALID_PROVIDERS else "bedrock"


def get_embeddings(**kwargs: Any):
    settings = get_settings()
    provider = resolve_provider(settings)
    if provider == "openai":
        from langchain_openai import OpenAIEmbeddings

        embed_kwargs: dict[str, Any] = {
            "model": settings.embedding_openai_model,
            "api_key": settings.openai_api_key,
        }
        gateway_url = settings.embedding_openai_base_url or settings.openai_base_url
        if gateway_url:
            kwargs["base_url"] = gateway_url
            # OpenAI-compatible gateways (Ollama, some vLLM builds) reject the
            # tiktoken token-ID arrays OpenAIEmbeddings sends by default
            # ("invalid input type") — send raw strings instead.
            kwargs.setdefault("check_embedding_ctx_length", False)
            # Some gateways proxy a rate-limited upstream free tier (e.g.
            # omniroute -> Gemini's embed_content free tier: 100 req/min,
            # verified live -- a single bulk-ingest run of a few hundred
            # documents exhausts it on the first batch). The openai SDK's
            # own retry logic honors a 429's Retry-After header, so a higher
            # max_retries lets a large ingest run ride out the quota window
            # instead of failing outright; the wiki/local-docs agents run as
            # background collection jobs, not interactive requests, so the
            # extra wall-clock time this can add is an acceptable tradeoff.
            kwargs.setdefault("max_retries", 8)
            # Do NOT send `dimensions` through a custom gateway: some (e.g.
            # omniroute, verified live) mistranslate it into the upstream
            # provider's native API and 400 on ANY value, even the model's own
            # native size, not just a truncation request. Others (Ollama)
            # silently ignore it. Instead, always fetch the model's native
            # vector and truncate+renormalize client-side to EMBEDDING_DIM
            # (_TruncatedEmbeddings) — safe even when native size already
            # equals EMBEDDING_DIM (a no-op slice + idempotent renormalize).
            inner = OpenAIEmbeddings(**embed_kwargs, **kwargs)
            return _TruncatedEmbeddings(
                inner, settings.embedding_dim, pacing_seconds=settings.embedding_batch_pacing_seconds
            )
        # Official OpenAI: `dimensions` is well-supported and is the only way
        # to get a non-native width out of e.g. text-embedding-3-*.
        embed_kwargs["dimensions"] = settings.embedding_dim
        return OpenAIEmbeddings(**embed_kwargs, **kwargs)
    # bedrock
    from langchain_aws import BedrockEmbeddings

    if settings.bedrock_profile:
        import boto3

        session = boto3.Session(
            profile_name=settings.bedrock_profile, region_name=settings.bedrock_region
        )
        kwargs["client"] = session.client("bedrock-runtime")
    else:
        kwargs["region_name"] = settings.bedrock_region
    return BedrockEmbeddings(model_id=settings.embedding_model, **kwargs)


# TRK-122 R8/DLP: Document.sensitivity values must never surface by default.
# "restricted" docs are excluded from retrieval; the column existed but nothing
# read it (a real DLP-adjacent gap).
_EXCLUDED_SENSITIVITY = ("restricted",)

# Personal-wiki fencing (Hermes wiki ingestion). The RAG store has no per-caller
# access control on search_knowledge — every hit for every ``Document.source``
# is uniformly retrievable by any caller holding the tool. Personal content
# (PersonalWikiAgent, ``Document.source="personal_wiki"``) MUST NOT be mixed
# into the default infra-scoped search surface: it is opt-in per call via
# ``include_personal``, never merged in silently. ``Document.source`` (already
# a free string distinguishing "confluence"/"local_docs" provenance) is reused
# as the domain tag rather than adding a new column — it already means exactly
# "which ingestion pipeline wrote this row", which is exactly the distinction
# needed here, and reusing it avoids an unnecessary migration.
_PERSONAL_DOMAIN_SOURCES = frozenset({"personal_wiki"})

# Fable Finding 7: cap on hits returned from any single document, so near-
# duplicate chunks from one document (made more similar to a title-like query by
# the shared breadcrumb prefix) cannot crowd out other genuinely relevant
# documents.
_MAX_CHUNKS_PER_DOC = 2


def _select_hits(rows, floor: float, top_k: int) -> list[dict]:
    """Threshold, per-document cap, and truncate the oversampled kNN buffer.

    ``rows`` are ``(DocumentChunk, Document, distance)`` tuples ordered by
    ascending cosine distance (most similar first). Drops chunks whose cosine
    similarity is below ``floor``; caps each ``document_id`` to
    ``_MAX_CHUNKS_PER_DOC`` hits (Finding 7) while continuing to evaluate the
    rest of the buffer for other documents; returns at most ``top_k`` hits.
    """
    results: list[dict] = []
    per_doc: dict = {}
    for chunk, doc, dist_val in rows:
        similarity = 1.0 - dist_val
        if similarity < floor:
            continue
        seen = per_doc.get(doc.id, 0)
        if seen >= _MAX_CHUNKS_PER_DOC:
            continue
        per_doc[doc.id] = seen + 1
        results.append(
            {
                "title": doc.title,
                "space": doc.space,
                "url": doc.url,
                "chunk_index": chunk.chunk_index,
                "text": chunk.text,
                "source": doc.source,
                "similarity": round(similarity, 4),
            }
        )
        if len(results) >= top_k:
            break
    return results


def _fts_rows(session, base_query, query: str, limit: int) -> list:
    """The lexical half of hybrid retrieval: PostgreSQL FTS over ``text_tsv``.

    Returns ``(DocumentChunk, Document, distance)`` rows — the SAME shape the
    cosine query returns — ordered by ``ts_rank`` descending, so the two
    rankings are fusible and ``_select_hits`` stays dialect- and
    ranking-agnostic.

    ``text_tsv`` is a deliberately unmodeled column (see ``db/models/core.py``),
    so it is referenced as a literal column rather than an ORM attribute. The
    ``'english'::regconfig`` cast is explicit because only the two-argument,
    literal-config form of ``to_tsvector``/``plainto_tsquery`` is IMMUTABLE —
    the same reason the generated column uses it.

    ``plainto_tsquery`` is what makes the "no FTS terms" case a clean no-op: a
    query of pure stop-words (or punctuation) compiles to an EMPTY tsquery,
    which ``@@`` matches against nothing, so this returns ``[]`` and fusion
    degenerates to the untouched cosine order.
    """
    from sqlalchemy import func, literal_column
    from sqlalchemy import text as sa_text

    from infra_brain.db.models import DOCUMENT_CHUNKS_TSV_COLUMN

    if session.bind.dialect.name != "postgresql":
        # Belt-and-braces: search_knowledge already returns [] off PostgreSQL, so
        # this is unreachable today. It stays because the cost of being wrong is
        # emitting `tsvector` SQL at SQLite — a hard error in the middle of
        # retrieval — and the cost of the guard is one attribute read.
        return []

    tsv = literal_column(f"document_chunks.{DOCUMENT_CHUNKS_TSV_COLUMN}")
    tsquery = func.plainto_tsquery(sa_text("'english'::regconfig"), query)
    rank = func.ts_rank(tsv, tsquery)
    return base_query.filter(tsv.op("@@")(tsquery)).order_by(rank.desc()).limit(limit).all()


def _fuse(cosine_rows: list, fts_rows: list) -> list:
    """RRF-fuse two ``(chunk, doc, distance)`` row lists into one ordered list.

    Rows are keyed by ``DocumentChunk.id`` — the same chunk reached by both
    rankings must score as ONE item, not two. The cosine row wins on collision
    (identical payload either way; keeping the cosine one makes the
    FTS-list-empty case byte-identical to the pre-R6 path).
    """
    from infra_brain.rag.hybrid import reciprocal_rank_fusion

    by_id: dict = {}
    for row in (*fts_rows, *cosine_rows):  # cosine last => cosine wins
        by_id[row[0].id] = row

    fused_ids = reciprocal_rank_fusion(
        [[row[0].id for row in cosine_rows], [row[0].id for row in fts_rows]]
    )
    return [by_id[chunk_id] for chunk_id in fused_ids]


def search_knowledge(
    query: str,
    k: int | None = None,
    min_similarity: float | None = None,
    include_personal: bool = False,
) -> list[dict]:
    """Hybrid (cosine + full-text, RRF-fused) top-k search over the knowledge store.

    SELECT-only. Returns [] (never raises for the disabled/unsupported case)
    when rag is off, the query is blank, or the DB is not PostgreSQL (e.g.
    sqlite tests) — pgvector's cosine operator only exists on PostgreSQL.

    TRK-297 R6 — hybrid retrieval. On PostgreSQL, TWO rankings are computed over
    the same filtered candidate set: the pgvector cosine kNN (unchanged) and a
    ``plainto_tsquery``/``ts_rank`` full-text ranking over the generated
    ``document_chunks.text_tsv`` column. They are fused with standard Reciprocal
    Rank Fusion (k=60, see ``rag/hybrid.py``), which recovers the exact-keyword
    hits — an error string, a hostname, a ``TRK-nnn`` id — that a dense
    embedding blurs away. Degradation is graceful and total:

      * non-PostgreSQL dialect: never reached (the function already returns []
        above); ``_fts_rows`` is additionally never called off PostgreSQL, so no
        ``tsvector`` SQL can be emitted at a SQLite engine;
      * ``rag_hybrid_enabled=false``: cosine-only, byte-identical to pre-R6;
      * query with no lexical terms, or no lexical match: the FTS ranking is
        empty and RRF over a single list preserves cosine order exactly;
      * the FTS query erroring at all (e.g. a database that predates the
        migration — ``text_tsv`` is unmodeled, so the schema-drift gate is
        structurally unable to notice it missing): logged once and treated as an
        empty FTS ranking, i.e. cosine-only. Retrieval degrading is always
        better than retrieval 500ing.

    The similarity floor still applies to the FUSED list: an FTS-only hit whose
    cosine similarity is under ``rag_min_similarity`` is dropped like any other.
    With the default floor of 0.0 that is a no-op; raising the floor deliberately
    narrows what lexical matching can contribute, which is the intended reading
    of a *similarity* floor.

    Each hit carries a rounded cosine ``similarity`` (TRK-122 R3) so callers can
    cite and threshold. A similarity floor (``settings.rag_min_similarity``,
    overridable per call) drops weak matches; with the default 0.0 floor the
    result set is identical to pre-TRK-122 pure top-k. ``restricted``-sensitivity
    documents are always excluded (TRK-122 R8).

    Personal-domain fencing: documents from a personal-domain source (currently
    ``Document.source == "personal_wiki"``, the Hermes-wiki ingestion) are
    EXCLUDED from both results and the similarity ranking unless the caller
    passes ``include_personal=True``. This is a hard requirement, not a
    convenience default — search_knowledge has no other access-control layer,
    so this is the only thing standing between "personal notes" and "anyone
    holding the MCP tool, by default, silently."
    """
    settings = get_settings()
    if not settings.rag_enabled or not (query or "").strip():
        return []
    from infra_brain.db.models import Document, DocumentChunk
    from infra_brain.db.session import get_session

    top_k = k or settings.rag_top_k
    floor = settings.rag_min_similarity if min_similarity is None else min_similarity

    # TRK-122 R4: prepend the asymmetric-retrieval query instruction (mxbai) when
    # configured — QUERY text only, never document text. TRK-122 R8: embed BEFORE
    # opening the session so a slow CPU-bound embedding call does not hold a DB
    # pool connection open.
    query_text = (settings.embedding_query_prefix or "") + query
    vector = get_embeddings().embed_query(query_text)

    with get_session() as s:
        if s.bind.dialect.name != "postgresql":
            return []
        dist = DocumentChunk.embedding.cosine_distance(vector).label("distance")

        # Cosine kNN, oversample (top_k*3), then threshold-filter by the floor
        # and cap per-document (Finding 7) before truncating to top_k.
        q = (
            s.query(DocumentChunk, Document, dist)
            .join(Document, DocumentChunk.document_id == Document.id)
            .filter(DocumentChunk.embedding.isnot(None))
            .filter(Document.status != "stale")
            .filter(Document.sensitivity.notin_(_EXCLUDED_SENSITIVITY))
        )
        if not include_personal:
            # Applied at the SQL level (not a post-filter) so a fenced document
            # is excluded from the similarity ranking itself, not merely hidden
            # from the final list — it never competes for a top-k slot or
            # crowds out an infra-domain hit.
            q = q.filter(Document.source.notin_(_PERSONAL_DOMAIN_SOURCES))

        buffer_size = max(top_k * 3, 15)
        rows = q.order_by(dist).limit(buffer_size).all()

        if settings.rag_hybrid_enabled:
            # SAVEPOINT, not a bare try: on PostgreSQL a failed statement aborts
            # the whole transaction, so without one a missing ``text_tsv`` would
            # poison the session we still want to return cosine results from.
            # The cosine rows are already materialised above, so the fallback
            # genuinely costs nothing.
            fts = []
            try:
                with s.begin_nested():
                    fts = _fts_rows(s, q, query, buffer_size)
            except SQLAlchemyError:
                logger.warning(
                    "search_knowledge: full-text ranking failed — falling back to "
                    "cosine-only. Is migration 2a7f1c9e4d30 "
                    "(document_chunks.text_tsv) applied to this database?",
                    exc_info=True,
                )
                fts = []
            if fts:
                rows = _fuse(rows, fts)

        return _select_hits(rows, floor, top_k)
