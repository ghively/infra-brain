"""TRK-297 R6 — hybrid retrieval: RRF fusion math and SQLite degradation.

No database is needed by anything in this module. The real-PostgreSQL half
(generated column, GIN index, term-match beating a cosine-near chunk) lives in
``tests/test_rag_hybrid_pg.py``, which self-skips unless ``PG_GATE_DSN`` is set.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from infra_brain.config import get_settings
from infra_brain.rag.hybrid import RRF_K, reciprocal_rank_fusion, rrf_scores

# ---------------------------------------------------------------------------
# RRF fusion math — pinned against a hand-computed fixture
# ---------------------------------------------------------------------------


def test_rrf_k_is_the_standard_constant():
    assert RRF_K == 60


def test_rrf_scores_match_hand_computed_values():
    """Two rankings, worked out by hand with k=60 and 1-based ranks.

    cosine: [a, b, c]        fts: [c, d, a]

        a = 1/61 + 1/63  = 0.016393442622950820 + 0.015873015873015872
                         = 0.032266458495966692
        b = 1/62         = 0.016129032258064516
        c = 1/63 + 1/61  = 0.032266458495966692        (same pair as a)
        d = 1/62         = 0.016129032258064516        (same as b)
    """
    scores = rrf_scores([["a", "b", "c"], ["c", "d", "a"]])

    assert scores["a"] == pytest.approx(0.032266458495966692, abs=1e-15)
    assert scores["b"] == pytest.approx(0.016129032258064516, abs=1e-15)
    assert scores["c"] == pytest.approx(0.032266458495966692, abs=1e-15)
    assert scores["d"] == pytest.approx(0.016129032258064516, abs=1e-15)
    assert set(scores) == {"a", "b", "c", "d"}


def test_rrf_fused_order_matches_the_hand_computed_fixture():
    """Same fixture: a and c tie on score, b and d tie on score.

    Ties break by first appearance across the input lists in argument order, so
    the full expected order is [a, c, b, d]:
      * a before c   — a is first in the cosine list, c is third;
      * a, c before b, d — 0.0322... > 0.0161...;
      * b before d   — b is in the (first) cosine list, d only in the fts list.

    Note what this encodes: ``c``, which is only THIRD by cosine, is pulled level
    with the top cosine hit purely because the lexical ranking put it first. That
    is the whole point of R6.
    """
    assert reciprocal_rank_fusion([["a", "b", "c"], ["c", "d", "a"]]) == ["a", "c", "b", "d"]


def test_rrf_of_a_single_ranking_is_the_identity():
    """The no-FTS-terms / no-lexical-match case must be a STRICT no-op.

    1/(k+i) is strictly decreasing in i, so a lone ranking keeps its order and an
    empty second ranking contributes nothing.
    """
    cosine = ["a", "b", "c", "d", "e"]
    assert reciprocal_rank_fusion([cosine]) == cosine
    assert reciprocal_rank_fusion([cosine, []]) == cosine


def test_rrf_empty_input_is_empty():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_rrf_scores_a_duplicate_within_one_ranking_once():
    """A repeated item is credited its BEST position only — never twice."""
    assert rrf_scores([["a", "a", "b"]])["a"] == pytest.approx(1 / 61)
    assert rrf_scores([["a", "a", "b"]])["b"] == pytest.approx(1 / 63)


def test_rrf_rejects_non_positive_k():
    """k <= 0 makes 1/(k+rank) blow up or go negative, silently inverting order."""
    with pytest.raises(ValueError, match="must be positive"):
        rrf_scores([["a"]], k=0)
    with pytest.raises(ValueError, match="must be positive"):
        reciprocal_rank_fusion([["a"]], k=-60)


def test_rrf_agreement_beats_a_single_strong_ranking():
    """The damping constant's job: rank-1 in one list must not outrank rank-2 in
    both. 1/61 = 0.01639 < 1/62 + 1/62 = 0.03226."""
    assert reciprocal_rank_fusion([["solo", "both"], ["other", "both"]])[0] == "both"


# ---------------------------------------------------------------------------
# SQLite degradation — the FTS half must never reach a non-PostgreSQL engine
# ---------------------------------------------------------------------------


def _sqlite_search(monkeypatch, *, hybrid: bool):
    """Run search_knowledge against a real SQLite engine, capturing every SQL
    statement the engine is asked to execute. Returns (result, statements)."""
    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setenv("RAG_HYBRID_ENABLED", "true" if hybrid else "false")
    get_settings.cache_clear()

    class _FakeEmb:
        def embed_query(self, text):
            return [0.0, 0.0]

    eng = create_engine("sqlite://")
    statements: list[str] = []

    @event.listens_for(eng, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):  # noqa: ARG001
        statements.append(statement)

    @contextmanager
    def _fake_session():
        with Session(eng) as s:
            yield s

    monkeypatch.setattr("infra_brain.embeddings.get_embeddings", lambda: _FakeEmb())
    monkeypatch.setattr("infra_brain.db.session.get_session", _fake_session)

    from infra_brain.embeddings import search_knowledge

    try:
        return search_knowledge("wazuh agent disconnected on media-host"), statements
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("hybrid", [True, False])
def test_search_knowledge_on_sqlite_emits_no_tsvector_sql(monkeypatch, hybrid):
    """Both ways: with hybrid on AND off, the SQLite path is byte-identical to
    pre-R6 — it returns [] and never asks SQLite for tsvector/ts_rank/text_tsv,
    every one of which is a hard error there rather than a wrong answer."""
    out, statements = _sqlite_search(monkeypatch, hybrid=hybrid)

    assert out == []
    sql = " ".join(statements).lower()
    for token in ("tsvector", "ts_rank", "plainto_tsquery", "text_tsv"):
        assert token not in sql, f"{token!r} was emitted at a SQLite engine (hybrid={hybrid})"


def test_fts_rows_refuses_a_non_postgresql_session():
    """The defensive dialect guard inside _fts_rows itself, independent of the
    earlier return-[] in search_knowledge — so a future refactor of that early
    return cannot start pointing tsvector SQL at SQLite."""
    from infra_brain.embeddings import _fts_rows

    eng = create_engine("sqlite://")
    with Session(eng) as s:
        # base_query is deliberately None: reaching it would raise, so returning
        # [] proves the guard fired before touching the query at all.
        assert _fts_rows(s, None, "anything", 15) == []
