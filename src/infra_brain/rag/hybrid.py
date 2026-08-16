"""Reciprocal Rank Fusion for hybrid retrieval (TRK-297 R6).

Pure ranking math — no database, no SQLAlchemy, no config. The PostgreSQL side
of hybrid retrieval (the ``tsvector``/``ts_rank`` query and the dialect gate)
lives in :mod:`infra_brain.embeddings`; this module only answers "given N
ranked lists of the same items, what is the fused order?".

Why RRF and not score blending
------------------------------
Cosine distance and ``ts_rank`` are not on a common scale — cosine similarity
is bounded in ``[-1, 1]`` and ``ts_rank`` is an unbounded, corpus- and
length-dependent float. Normalising them against each other needs a corpus-wide
calibration this store does not have (and would have to re-derive whenever the
embedding model or the document mix changes). RRF sidesteps that entirely by
throwing away the scores and fusing on *rank position* only, which is exactly
why it is the standard baseline for this problem (Cormack, Clarke & Buettcher,
SIGIR 2009).

    score(d) = sum over rankings r that contain d of  1 / (k + rank_r(d))

with ``rank`` 1-based. ``k`` damps the influence of the very top positions so a
single ranking cannot unilaterally decide the fused order; ``k = 60`` is the
value from the original paper and the de-facto default (pgvector, Elastic,
Weaviate all ship it). No repo-established constant existed to prefer over it.

Determinism
-----------
Ties are broken by first appearance across the input lists, in argument order.
That is not cosmetic: it is what makes "the FTS ranking came back empty" a
*strict no-op*. Fusing a single list leaves its order untouched (``1/(k+i)`` is
strictly decreasing in ``i``), so a query with no FTS terms — or a corpus with
no lexical match — returns exactly the pre-R6 cosine ordering rather than a
subtly reshuffled one.
"""

from __future__ import annotations

from typing import Hashable, Iterable, Sequence, TypeVar

K = TypeVar("K", bound=Hashable)

#: Standard RRF damping constant (Cormack et al. 2009). See module docstring.
RRF_K = 60


def rrf_scores(rankings: Sequence[Iterable[K]], k: int = RRF_K) -> dict[K, float]:
    """Map each item to its summed reciprocal-rank score.

    ``rankings`` is a sequence of ranked lists, best-first. An item repeated
    within one ranking is scored on its FIRST (best) position only — a duplicate
    would otherwise be credited twice for being one result.

    ``k`` must be positive; ``k <= 0`` can make ``1 / (k + rank)`` negative or
    undefined for the top ranks, silently inverting the ranking.
    """
    if k <= 0:
        raise ValueError(f"RRF k must be positive, got {k!r}")
    scores: dict[K, float] = {}
    for ranking in rankings:
        seen: set[K] = set()
        for rank, key in enumerate(ranking, start=1):
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


def reciprocal_rank_fusion(rankings: Sequence[Iterable[K]], k: int = RRF_K) -> list[K]:
    """Fuse ranked lists into one order, best-first.

    Highest fused score first; ties broken by first appearance across
    ``rankings`` in argument order, so passing a single list returns it
    unchanged (see the module docstring on why that matters).
    """
    materialised = [list(r) for r in rankings]
    scores = rrf_scores(materialised, k=k)

    order: dict[K, int] = {}
    for ranking in materialised:
        for key in ranking:
            order.setdefault(key, len(order))

    return sorted(scores, key=lambda key: (-scores[key], order[key]))
