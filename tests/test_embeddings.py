"""Tests for the provider-agnostic embeddings factory (TRK-067).

Covers provider auto-resolution and the OpenAI-compatible-gateway construction
path. The gateway kwarg matters operationally: Ollama (and some vLLM builds)
reject the tiktoken token-ID arrays OpenAIEmbeddings sends by default
("invalid input type"), so a configured openai_base_url must flip
check_embedding_ctx_length off — verified empirically against Ollama 0.21.0
on the dev host (2026-07-22).
"""

import uuid as _uuid
from contextlib import contextmanager
from pathlib import Path as _Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from infra_brain.config import get_settings
from infra_brain.embeddings import get_embeddings, resolve_provider


class _S:
    """Minimal settings stub for resolve_provider."""

    def __init__(self, embedding_provider="", llm_provider="anthropic"):
        self.embedding_provider = embedding_provider
        self.llm_provider = llm_provider


def test_embedding_dim_matches_db_model_column_width():
    """Settings.embedding_dim MUST equal db.models._EMBED_DIM -- they back the
    same pgvector column width from two different modules. config.py's own
    comment has claimed this is cross-checked by a test since TRK-067; no
    such test actually existed until now."""
    from infra_brain.db.models import _EMBED_DIM

    assert get_settings().embedding_dim == _EMBED_DIM


def test_resolve_provider_explicit_override_wins():
    assert resolve_provider(_S(embedding_provider="openai", llm_provider="bedrock")) == "openai"
    assert resolve_provider(_S(embedding_provider="bedrock", llm_provider="openai")) == "bedrock"


def test_resolve_provider_rejects_unknown_explicit():
    with pytest.raises(ValueError, match="Unknown EMBEDDING_PROVIDER"):
        resolve_provider(_S(embedding_provider="anthropic"))


def test_resolve_provider_mirrors_llm_provider_when_it_can_embed():
    assert resolve_provider(_S(llm_provider="openai")) == "openai"
    assert resolve_provider(_S(llm_provider="bedrock")) == "bedrock"


def test_resolve_provider_falls_back_to_bedrock_for_anthropic():
    assert resolve_provider(_S(llm_provider="anthropic")) == "bedrock"


@pytest.fixture
def _openai_env(monkeypatch):
    """Point the factory at the openai provider with a dummy key."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_get_embeddings_gateway_base_url_disables_token_input(_openai_env):
    """A configured gateway base URL must send raw strings, not token arrays.

    get_embeddings() wraps the gateway path in _TruncatedEmbeddings, so the
    underlying OpenAIEmbeddings instance (with these attributes) is ._inner.
    """
    _openai_env.setenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1")
    _openai_env.setenv("EMBEDDING_OPENAI_MODEL", "mxbai-embed-large")
    get_settings.cache_clear()

    emb = get_embeddings()
    from infra_brain.embeddings import _TruncatedEmbeddings

    assert isinstance(emb, _TruncatedEmbeddings)
    assert emb._inner.check_embedding_ctx_length is False
    assert emb._inner.openai_api_base == "http://host.docker.internal:11434/v1"
    assert emb._inner.model == "mxbai-embed-large"


def test_get_embeddings_gateway_base_url_never_sends_dimensions(_openai_env):
    """Custom gateways must NOT receive `dimensions` -- some (verified live
    against omniroute) mistranslate it into the upstream provider's own API
    and 400 on ANY value, not just a truncation request. Truncation to
    EMBEDDING_DIM happens client-side instead (_TruncatedEmbeddings)."""
    _openai_env.setenv("OPENAI_BASE_URL", "http://203.0.113.15:20129/v1")
    _openai_env.setenv("EMBEDDING_OPENAI_MODEL", "gemini/gemini-embedding-001")
    get_settings.cache_clear()

    emb = get_embeddings()
    assert emb._inner.dimensions is None


def test_truncated_embeddings_truncates_and_renormalizes():
    """_TruncatedEmbeddings must slice to the leading N dims and restore unit
    length -- this is what makes a 3072-dim Gemini vector usable in a
    1536-dim pgvector column (the HNSW index cap is 2000, so the model's
    native 3072 output can never be indexed directly)."""
    from infra_brain.embeddings import _TruncatedEmbeddings

    class _FakeInner:
        def embed_query(self, text):
            return [1.0] * 3072  # unit-ish native vector, deliberately not unit length

        def embed_documents(self, texts):
            return [[1.0] * 3072 for _ in texts]

    wrapped = _TruncatedEmbeddings(_FakeInner(), dim=1536)

    vec = wrapped.embed_query("test")
    assert len(vec) == 1536
    norm = sum(x * x for x in vec) ** 0.5
    assert abs(norm - 1.0) < 1e-9

    docs = wrapped.embed_documents(["a", "b"])
    assert len(docs) == 2
    assert all(len(v) == 1536 for v in docs)


def test_truncated_embeddings_batches_and_paces_large_document_sets(monkeypatch):
    """embed_documents() must split into <=_MAX_TEXTS_PER_EMBED_BATCH-sized
    calls and sleep between them -- verified live that a single oversized
    batch exceeds omniroute's proxied Gemini free-tier quota (100 req/min)
    on the first attempt, and that retrying the SAME oversized batch just
    hits the same wall again. No sleep after the LAST batch (nothing left
    to pace for)."""
    from infra_brain import embeddings as embeddings_module
    from infra_brain.embeddings import _TruncatedEmbeddings

    call_sizes: list[int] = []
    sleep_calls: list[float] = []

    class _FakeInner:
        def embed_documents(self, texts):
            call_sizes.append(len(texts))
            return [[2.0, 2.0] for _ in texts]  # norm=2 -> renormalize is exercised too

    monkeypatch.setattr(embeddings_module.time, "sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr(embeddings_module, "_MAX_TEXTS_PER_EMBED_BATCH", 3)

    wrapped = _TruncatedEmbeddings(_FakeInner(), dim=2, pacing_seconds=65)
    texts = [f"chunk-{i}" for i in range(7)]  # 3 + 3 + 1 -> 3 batches, 2 sleeps
    vectors = wrapped.embed_documents(texts)

    assert call_sizes == [3, 3, 1]
    assert sleep_calls == [65, 65]
    assert len(vectors) == 7
    for v in vectors:
        norm = sum(x * x for x in v) ** 0.5
        assert abs(norm - 1.0) < 1e-9


def test_truncated_embeddings_default_pacing_is_off(monkeypatch):
    """pacing_seconds defaults to 0 -- NO sleep between batches unless a
    caller explicitly opts in. A local/unlimited embedding endpoint (e.g.
    Ollama) has no rate limit to pace around; defaulting pacing on made a
    real ingestion run needlessly ~20-40x slower before this was caught, so
    this specifically guards against that regression recurring."""
    from infra_brain import embeddings as embeddings_module
    from infra_brain.embeddings import _TruncatedEmbeddings

    sleep_calls: list[float] = []

    class _FakeInner:
        def embed_documents(self, texts):
            return [[1.0] for _ in texts]

    monkeypatch.setattr(embeddings_module.time, "sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr(embeddings_module, "_MAX_TEXTS_PER_EMBED_BATCH", 2)

    wrapped = _TruncatedEmbeddings(_FakeInner(), dim=1)  # pacing_seconds not passed -> default
    wrapped.embed_documents([f"chunk-{i}" for i in range(9)])  # 5 batches, would sleep 4x if paced

    assert sleep_calls == []


def test_get_embeddings_official_openai_keeps_token_input(_openai_env):
    """No base URL (official OpenAI) keeps the library default behavior."""
    get_settings.cache_clear()

    emb = get_embeddings()
    assert emb.check_embedding_ctx_length is True
    assert emb.dimensions == get_settings().embedding_dim


def test_get_embeddings_gateway_respects_caller_override(_openai_env):
    """setdefault semantics: an explicit caller kwarg wins over the gateway flip."""
    _openai_env.setenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1")
    get_settings.cache_clear()

    emb = get_embeddings(check_embedding_ctx_length=True)
    assert emb._inner.check_embedding_ctx_length is True


# ---------------------------------------------------------------------------
# TRK-122 R3/R4/R8: search_knowledge retrieval hardening
# ---------------------------------------------------------------------------


def test_search_knowledge_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "false")
    get_settings.cache_clear()
    from infra_brain.embeddings import search_knowledge

    assert search_knowledge("anything") == []
    get_settings.cache_clear()


def test_search_knowledge_blank_query_returns_empty(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "true")
    get_settings.cache_clear()
    from infra_brain.embeddings import search_knowledge

    assert search_knowledge("   ") == []
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fable Finding 7: per-document diversity cap in the post-query filter
# ---------------------------------------------------------------------------


def _doc(i):
    return SimpleNamespace(id=i, title=f"doc{i}", space="s", url="u", source="local_docs")


def _chunk(ci):
    return SimpleNamespace(chunk_index=ci, text=f"chunk{ci}")


def test_select_hits_caps_two_per_document():
    """No more than 2 hits from one document, so near-duplicate chunks (made more
    similar by the shared breadcrumb prefix) don't crowd out other documents."""
    from collections import Counter

    from infra_brain.embeddings import _select_hits

    d1, d2, d3 = _doc(1), _doc(2), _doc(3)
    # dist ascending = most similar first; document 1 dominates the buffer top.
    rows = [
        (_chunk(0), d1, 0.01),
        (_chunk(1), d1, 0.02),
        (_chunk(2), d1, 0.03),
        (_chunk(3), d1, 0.04),
        (_chunk(4), d1, 0.05),
        (_chunk(5), d2, 0.06),
        (_chunk(6), d3, 0.07),
    ]
    hits = _select_hits(rows, floor=0.0, top_k=5)
    per_doc = Counter(h["title"] for h in hits)
    assert per_doc["doc1"] == 2  # capped despite 5 candidates
    assert per_doc["doc2"] == 1
    assert per_doc["doc3"] == 1  # other documents still surface
    assert len(hits) == 4


def test_select_hits_respects_floor_and_top_k():
    from infra_brain.embeddings import _select_hits

    d1, d2, d3 = _doc(1), _doc(2), _doc(3)
    rows = [
        (_chunk(0), d1, 0.1),  # similarity 0.9
        (_chunk(1), d2, 0.7),  # similarity 0.3 — below the 0.5 floor
    ]
    assert [h["title"] for h in _select_hits(rows, floor=0.5, top_k=5)] == ["doc1"]

    rows2 = [(_chunk(i), d, 0.1) for i, d in enumerate((d1, d2, d3))]
    assert len(_select_hits(rows2, floor=0.0, top_k=2)) == 2  # top_k truncation


def test_search_knowledge_prefixes_query_and_embeds_before_session(monkeypatch):
    """R4: the query prefix is prepended to the QUERY; R8: embedding happens
    before the DB session opens. On the sqlite dialect the search returns []
    (postgres-only cosine), but the prefixed embed call still fires first."""
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_QUERY_PREFIX", "PREFIX: ")
    get_settings.cache_clear()

    captured: dict = {}

    class _FakeEmb:
        def embed_query(self, text):
            captured["text"] = text
            return [0.0, 0.0]

    eng = create_engine("sqlite://")

    @contextmanager
    def _fake_session():
        with Session(eng) as s:
            yield s

    monkeypatch.setattr("infra_brain.embeddings.get_embeddings", lambda: _FakeEmb())
    monkeypatch.setattr("infra_brain.db.session.get_session", _fake_session)

    from infra_brain.embeddings import search_knowledge

    out = search_knowledge("how do backups work")
    assert out == []  # sqlite -> postgres-only cosine path returns []
    assert captured["text"] == "PREFIX: how do backups work"  # R4 + R8
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Personal-wiki domain fencing (search_knowledge ``include_personal``)
# ---------------------------------------------------------------------------
#
# search_knowledge's postgres-only cosine path (`if s.bind.dialect.name !=
# "postgresql": return []`, see above) means the fence can only be proven
# end-to-end against a REAL pgvector-backed Postgres — sqlite structurally
# short-circuits before the fence's own filter is ever reached, so a sqlite
# test could only prove "sqlite returns []", not that the fence works. This
# test therefore opts out of the suite's usual autouse sqlite override and
# talks to the real local dev Postgres described by the repo's own .env
# (127.0.0.1:15432 — confirmed live in this session), reading credentials
# from .env AT TEST TIME (never hardcoded in source) and skipping cleanly
# wherever that instance isn't reachable (e.g. CI, which runs the rest of
# this suite on sqlite per repo convention — see CLAUDE.md). It inserts two
# throwaway Document/DocumentChunk rows and deletes them in a `finally`, so
# nothing persists in the database either way.


def _real_pg_engine_or_none():
    from dotenv import dotenv_values
    from sqlalchemy import create_engine as _create_engine

    env_path = _Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return None
    values = dotenv_values(env_path)
    user = values.get("POSTGRES_USER")
    password = values.get("POSTGRES_PASSWORD")
    db = values.get("POSTGRES_DB")
    if not (user and password and db):
        return None
    dsn = f"postgresql://{user}:{password}@127.0.0.1:15432/{db}"
    try:
        engine = _create_engine(dsn)
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return engine
    except Exception:
        return None


@pytest.fixture
def real_pg_engine():
    engine = _real_pg_engine_or_none()
    if engine is None:
        pytest.skip(
            "real local Postgres (127.0.0.1:15432, per .env) not reachable — "
            "skipping live pgvector personal-wiki fencing test"
        )
    yield engine
    engine.dispose()


def _fixed_vector(seed: float, dim: int = 1024) -> list[float]:
    """A deterministic non-embedding vector. Both seeded chunks below use the
    SAME vector as the (also mocked) query embedding, so both are an exact
    cosine match (similarity 1.0) — this isolates the test to the SQL/ORM
    fence itself, not embedding-model behavior."""
    v = [0.0] * dim
    v[0] = seed
    return v


def test_search_knowledge_fences_personal_wiki_by_default_and_opts_in(
    real_pg_engine, monkeypatch
):
    """(a) a seeded personal-wiki document is NOT returned by a default
    search_knowledge call even though its content is an exact cosine match for
    the query; (b) it IS returned when the caller passes
    include_personal=True. Exercises the real pgvector cosine path."""
    from infra_brain.db.models import Document, DocumentChunk

    monkeypatch.setenv("RAG_ENABLED", "true")
    get_settings.cache_clear()

    query_vector = _fixed_vector(1.0)

    class _FakeEmb:
        def embed_query(self, text):
            return query_vector

    monkeypatch.setattr("infra_brain.embeddings.get_embeddings", lambda: _FakeEmb())

    @contextmanager
    def _fake_session():
        with Session(real_pg_engine) as s:
            yield s

    monkeypatch.setattr("infra_brain.db.session.get_session", _fake_session)

    marker = _uuid.uuid4().hex[:8]
    personal_title = f"__test_personal_wiki_fence_{marker}__"
    infra_title = f"__test_infra_fence_{marker}__"
    inserted_doc_ids: list = []

    from infra_brain.embeddings import search_knowledge

    try:
        with Session(real_pg_engine) as s:
            personal_doc = Document(
                title=personal_title,
                source="personal_wiki",
                external_id=f"entities/{marker}.md",
                space="entities",
                status="current",
            )
            infra_doc = Document(
                title=infra_title,
                source="local_docs",
                external_id=f"docs/{marker}.md",
                space="docs",
                status="current",
            )
            s.add_all([personal_doc, infra_doc])
            s.flush()
            inserted_doc_ids = [personal_doc.id, infra_doc.id]
            s.add(
                DocumentChunk(
                    document_id=personal_doc.id,
                    chunk_index=0,
                    text=f"personal wiki fencing regression test content {marker}",
                    embedding=query_vector,
                    token_count=5,
                )
            )
            s.add(
                DocumentChunk(
                    document_id=infra_doc.id,
                    chunk_index=0,
                    text=f"infra fencing regression test content {marker}",
                    embedding=query_vector,
                    token_count=5,
                )
            )
            s.commit()

        default_hits = search_knowledge(f"fencing regression test {marker}")
        default_titles = {h["title"] for h in default_hits}
        assert infra_title in default_titles  # infra content surfaces normally
        assert personal_title not in default_titles  # (a) fenced by default

        opted_in_hits = search_knowledge(
            f"fencing regression test {marker}", include_personal=True
        )
        opted_in_titles = {h["title"] for h in opted_in_hits}
        assert infra_title in opted_in_titles
        assert personal_title in opted_in_titles  # (b) explicit opt-in includes it
    finally:
        with Session(real_pg_engine) as s:
            if inserted_doc_ids:
                s.query(DocumentChunk).filter(
                    DocumentChunk.document_id.in_(inserted_doc_ids)
                ).delete(synchronize_session=False)
                s.query(Document).filter(Document.id.in_(inserted_doc_ids)).delete(
                    synchronize_session=False
                )
                s.commit()
        get_settings.cache_clear()
