"""TRK-297 R6 — hybrid retrieval proven against a REAL PostgreSQL.

Everything here needs things SQLite structurally cannot provide — a
``GENERATED ALWAYS AS (to_tsvector(...)) STORED`` column, a GIN index,
``plainto_tsquery``/``ts_rank``, and pgvector's ``<=>`` — so the module follows
the gate-4 convention: build the engine through ``tests/support/pg.py``'s
:func:`make_engine`, which is in-memory SQLite by default and a real PostgreSQL
when ``PG_GATE_DSN`` is set, and skip cleanly when it is not.

Run it the way CI does::

    PG_GATE_DSN=postgresql://... PYTHONPATH=src pytest tests/test_rag_hybrid_pg.py

The module is named in the ``agent-orm-check`` CI job and in
``.claude/skills/pg-gate-check/run.sh`` gate 4 alongside ``tests/agents`` /
``tests/etl``, for the same reason those are: it asserts SQL behaviour that only
means something on the dialect it will be believed on.

Note the DDL under test here arrives via ``Base.metadata.create_all()`` (the
``after_create`` listener in ``db/models/core.py``), not via alembic —
``make_engine`` builds schemas with ``create_all``. That the MIGRATION produces
the same thing is what ``tests/test_rag_hybrid_ddl_parity.py`` (static) plus the
``migration-parity`` gate (real chain, real PostgreSQL) cover between them.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from infra_brain.config import get_settings
from tests.support.pg import make_engine, pg_enabled

pytestmark = pytest.mark.skipif(
    not pg_enabled(),
    reason="needs a real PostgreSQL — set PG_GATE_DSN (gate 4 / agent-orm-check)",
)

_DIM = 1024


def _vector(*, axis: int, magnitude: float = 1.0) -> list[float]:
    """A deterministic unit-ish vector pointing along one axis.

    Embeddings are never called in this module — every vector is handed in
    explicitly — so cosine distance is fully under the test's control and the
    assertions are about SQL, not about an embedding model's behaviour.
    """
    v = [0.0] * _DIM
    v[axis] = magnitude
    return v


@pytest.fixture
def engine():
    return make_engine()


@pytest.fixture
def rag_on(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setenv("RAG_HYBRID_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _bind_search(monkeypatch, engine, query_vector):
    """Point ``search_knowledge`` at *engine* with a fixed query embedding."""

    class _FakeEmb:
        def embed_query(self, text):  # noqa: ARG002
            return query_vector

    @contextmanager
    def _fake_session():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr("infra_brain.embeddings.get_embeddings", lambda: _FakeEmb())
    monkeypatch.setattr("infra_brain.db.session.get_session", _fake_session)


def _seed(engine, rows):
    """Insert (title, text, vector) rows as one Document + DocumentChunk each."""
    from infra_brain.db.models import Document, DocumentChunk

    with Session(engine) as s:
        for i, (title, body, vec) in enumerate(rows):
            doc = Document(
                title=title,
                source="local_docs",
                external_id=f"docs/{title}.md",
                space="docs",
                status="current",
                sensitivity="internal",
            )
            s.add(doc)
            s.flush()
            s.add(
                DocumentChunk(
                    document_id=doc.id, chunk_index=0, text=body, embedding=vec, token_count=i
                )
            )
        s.commit()


# ---------------------------------------------------------------------------
# Schema: the generated column and its index actually exist and populate
# ---------------------------------------------------------------------------


def test_generated_tsvector_column_exists_and_is_generated(engine):
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT data_type, is_generated, generation_expression "
                "FROM information_schema.columns "
                "WHERE table_name = 'document_chunks' AND column_name = 'text_tsv'"
            )
        ).first()

    assert row is not None, "document_chunks.text_tsv is missing on PostgreSQL"
    data_type, is_generated, expression = row
    assert data_type == "tsvector"
    assert is_generated == "ALWAYS"
    # The literal-regconfig two-argument form — the one-argument form is not
    # IMMUTABLE and PostgreSQL would have rejected the column outright.
    assert "to_tsvector" in expression
    assert "english" in expression


def test_gin_index_exists_on_the_tsvector_column(engine):
    with engine.connect() as conn:
        indexdef = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'document_chunks' "
                "AND indexname = 'ix_document_chunks_text_tsv'"
            )
        ).scalar()

    assert indexdef, "ix_document_chunks_text_tsv is missing"
    assert "gin" in indexdef.lower()
    assert "text_tsv" in indexdef


def test_tsvector_populates_on_insert_without_anyone_writing_it(engine):
    """Generated, not trigger-maintained and not written by the ingest path: the
    column is correct by construction, so no collector can forget to fill it."""
    _seed(engine, [("gen", "the wazuh manager rejected an enrolment request", _vector(axis=0))])

    with engine.connect() as conn:
        tsv, matched = conn.execute(
            text(
                "SELECT text_tsv::text, "
                "       text_tsv @@ plainto_tsquery('english', 'wazuh enrolment') "
                "FROM document_chunks"
            )
        ).first()

    assert "wazuh" in tsv  # stemmed lexemes, and 'the'/'an' dropped as stop words
    assert "'the'" not in tsv
    assert matched is True


# ---------------------------------------------------------------------------
# Ranking: the reason R6 exists
# ---------------------------------------------------------------------------


def test_keyword_match_beats_a_cosine_nearer_chunk(engine, rag_on, monkeypatch):
    """The R6 acceptance case.

    ``decoy`` is the closest chunk by cosine (identical to the query vector) but
    shares no lexical content with the query. ``lexical`` is FARTHER by cosine
    yet contains the exact term. Pure cosine ranks decoy first; hybrid RRF must
    surface the term match ahead of it.
    """
    from infra_brain.embeddings import search_knowledge

    query_vector = _vector(axis=0)
    _seed(
        engine,
        [
            ("decoy", "general notes about unrelated background material", query_vector),
            # Deliberately off-axis: an exact-orthogonal vector, i.e. cosine
            # similarity 0.0 — as cosine-irrelevant as a chunk can be while
            # still clearing the default rag_min_similarity floor of 0.0.
            ("lexical", "restarting the kanidm daemon clears the stale token", _vector(axis=1)),
        ],
    )
    _bind_search(monkeypatch, engine, query_vector)

    hits = search_knowledge("kanidm daemon token", k=5)
    titles = [h["title"] for h in hits]

    assert titles[:2] == ["lexical", "decoy"], (
        "hybrid RRF should promote the exact-term chunk above the cosine-nearest "
        f"chunk; got {titles}"
    )

    # And the control: with hybrid off, cosine alone puts the decoy first again.
    monkeypatch.setenv("RAG_HYBRID_ENABLED", "false")
    get_settings.cache_clear()
    cosine_only = [h["title"] for h in search_knowledge("kanidm daemon token", k=5)]
    assert cosine_only[0] == "decoy", (
        f"cosine-only ranking should be unchanged from pre-R6; got {cosine_only}"
    )


def test_hybrid_is_a_no_op_when_the_query_has_no_lexical_terms(engine, rag_on, monkeypatch):
    """A stop-word-only query compiles to an EMPTY tsquery, so the FTS ranking is
    empty and RRF over one list is the identity — the fused order must equal the
    cosine order exactly, not merely resemble it."""
    from infra_brain.embeddings import search_knowledge

    query_vector = _vector(axis=0)
    mid = _vector(axis=0)
    mid[1] = 1.0  # 45 degrees off the query: cosine similarity ~0.707
    _seed(
        engine,
        [
            ("near", "alpha content", query_vector),  # similarity 1.0
            ("mid", "beta content", mid),  # similarity ~0.707
            ("far", "gamma content", _vector(axis=2)),  # orthogonal, similarity 0.0
        ],
    )
    _bind_search(monkeypatch, engine, query_vector)

    hybrid = [h["title"] for h in search_knowledge("the of and", k=5)]

    monkeypatch.setenv("RAG_HYBRID_ENABLED", "false")
    get_settings.cache_clear()
    cosine_only = [h["title"] for h in search_knowledge("the of and", k=5)]

    assert hybrid == cosine_only
    assert hybrid[0] == "near"


def test_empty_store_returns_empty(engine, rag_on, monkeypatch):
    from infra_brain.embeddings import search_knowledge

    _bind_search(monkeypatch, engine, _vector(axis=0))
    assert search_knowledge("wazuh manager enrolment") == []


def test_hybrid_still_honours_the_personal_wiki_fence(engine, rag_on, monkeypatch):
    """The lexical ranking must not become a side door around the R8 fence: the
    FTS query is built on top of the SAME filtered base query, so a personal-wiki
    chunk that is a perfect TERM match still must not surface by default."""
    from infra_brain.db.models import Document, DocumentChunk
    from infra_brain.embeddings import search_knowledge

    query_vector = _vector(axis=0)
    with Session(engine) as s:
        personal = Document(
            title="personal",
            source="personal_wiki",
            external_id="entities/x.md",
            space="entities",
            status="current",
            sensitivity="internal",
        )
        infra = Document(
            title="infra",
            source="local_docs",
            external_id="docs/x.md",
            space="docs",
            status="current",
            sensitivity="internal",
        )
        s.add_all([personal, infra])
        s.flush()
        s.add(
            DocumentChunk(
                document_id=personal.id,
                chunk_index=0,
                text="kanidm daemon token rotation notes",
                embedding=query_vector,
            )
        )
        s.add(
            DocumentChunk(
                document_id=infra.id,
                chunk_index=0,
                text="unrelated background material",
                embedding=query_vector,
            )
        )
        s.commit()

    _bind_search(monkeypatch, engine, query_vector)

    default_titles = {h["title"] for h in search_knowledge("kanidm daemon token")}
    assert "personal" not in default_titles
    assert "infra" in default_titles

    opted_in = {h["title"] for h in search_knowledge("kanidm daemon token", include_personal=True)}
    assert "personal" in opted_in
