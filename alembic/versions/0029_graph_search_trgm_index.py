"""KG-3: trigram index backing the graph /search leading-wildcard LIKE.

Revision ID: d1e2f3a4b5c6
Revises: a76a9b9fa04d
Create Date: 2026-07-07

Merge-time reconciliation (done): originally chained onto 0026 directly (like
MR-F's 0027 and MR-G's 0028, each authored in parallel worktrees). Renumbered
0027 -> 0029 and down_revision repointed onto MR-G's 0028 (``a76a9b9fa04d``),
the correct end of the merged chain: 0026 -> 0027 (MR-F, d0e1f2a3b4c5) ->
0028 (MR-G, a76a9b9fa04d) -> 0029 (this file).

``GET /api/graph/search?q=...`` (graph_api.py) matches
``LOWER(resources.name) LIKE '%<fragment>%'`` — a leading-wildcard pattern
that a plain B-tree index cannot support at all (the leading ``%`` defeats
prefix matching), so every call was a full sequential scan of ``resources``.
This adds the ``pg_trgm`` extension plus a GIN trigram index on
``lower(name)``, which *can* accelerate a leading-wildcard ``LIKE``/``ILIKE``.

Postgres-only (guarded): SQLite (the test suite's engine) has no ``pg_trgm``
equivalent and does not need one — ``tests/test_graph_api.py`` exercises
``/search`` against a stubbed ``get_neighborhood`` and a small in-memory
dataset where index usage is irrelevant to correctness.

Built ``CONCURRENTLY`` (via ``autocommit_block``) because ``resources`` is
continuously written by every collector agent; a plain (locking) index build
would block those writers for the build's duration on a large table.
"""

from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = "a76a9b9fa04d"
branch_labels = None
depends_on = None

_IDX_NAME = "ix_resources_name_trgm"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # pg_trgm / GIN trigram indexes are Postgres-only.

    inspector = inspect(bind)
    if "resources" not in set(inspector.get_table_names()):
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    existing = {ix["name"] for ix in inspector.get_indexes("resources")}
    if _IDX_NAME in existing:
        return

    # CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_IDX_NAME} "
            "ON resources USING gin (lower(name) gin_trgm_ops)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    inspector = inspect(bind)
    if "resources" not in set(inspector.get_table_names()):
        return

    existing = {ix["name"] for ix in inspector.get_indexes("resources")}
    if _IDX_NAME not in existing:
        return

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_IDX_NAME}")
