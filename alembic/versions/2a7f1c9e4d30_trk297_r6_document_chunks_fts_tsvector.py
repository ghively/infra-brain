"""trk297_r6_document_chunks_fts_tsvector

TRK-297 R6 — give the RAG knowledge store a lexical ranking to sit alongside its
pgvector cosine ranking, so ``search_knowledge`` can fuse the two with Reciprocal
Rank Fusion (see ``infra_brain/rag/hybrid.py``). Dense embeddings blur exact
tokens — an error string, a hostname, a ``TRK-nnn`` id — which is precisely what
a full-text index is good at.

Adds ONE column and ONE index, both PostgreSQL-only:

  * ``document_chunks.text_tsv`` — ``tsvector GENERATED ALWAYS AS
    (to_tsvector('english', coalesce(text, ''))) STORED``. Generated, not
    trigger-maintained: the database keeps it correct by construction, so no
    ingestion path can forget to update it and no backfill job can drift.
  * ``ix_document_chunks_text_tsv`` — a GIN index over it, which is what makes
    the ``@@`` match a lookup rather than a seq scan.

Why the column is NOT in ``Base.metadata`` (the thing that kept R6 deferred).
SQLite validates generated-column expressions at ``CREATE TABLE`` time and has
no ``to_tsvector``, so a ``Computed()`` on the model breaks
``Base.metadata.create_all()`` — i.e. the entire SQLite suite and
``/pg-gate-check``. Declaring the column *without* ``Computed`` instead (plain
column in the ORM, GENERATED applied only here) does work, but makes alembic's
``_compare_computed_default`` emit ``Computed default on document_chunks.text_tsv
cannot be modified`` on EVERY ``compare_metadata`` run — migration-parity,
``alembic check``, and the runtime ``assert_schema_current`` deploy gate —
permanently normalising a warning that is supposed to mean something; and a
future ``--autogenerate`` would see a plain column and propose recreating it
without the GENERATED clause. So the column and its index are excluded from the
comparison instead, exactly like ``ix_resources_name_trgm``:
``infra_brain/db/schema_check.py`` (``_UNMODELED_COLUMNS`` /
``_UNMODELED_INDEXES``) and ``alembic/env.py`` (``_IGNORE_COLUMNS`` /
``_IGNORE_INDEXES``) both carry the entry, and must stay aligned.

The identical DDL is also emitted by an ``after_create`` listener in
``infra_brain/db/models/core.py``, so ``create_all`` paths (gate-4 ORM tests on
real PostgreSQL, fresh dev DBs) get the column too. The strings are deliberately
duplicated rather than imported — a migration must be a frozen snapshot, and
importing a live app constant is how ``_EMBED_DIM`` ended up needing
``06e3b86260d5`` and then ``7aae7d4d33eb`` to undo it.
``tests/test_rag_hybrid_ddl_parity.py`` asserts the two copies match.

Danger-pattern review (per /migration-create step 2):
  * NOT NULL without default — N/A, the column is nullable. (It will in practice
    never be NULL: ``coalesce(text, '')`` means ``to_tsvector`` always returns a
    value. Declaring NOT NULL would buy nothing and would add a validation scan.)
  * Table rewrite — YES, and deliberately accepted. ``ADD COLUMN ... GENERATED
    ALWAYS AS ... STORED`` must materialise a value for every existing row, so
    PostgreSQL rewrites the table under ACCESS EXCLUSIVE. ``document_chunks``
    holds thousands of ~1 KB rows at homelab scale and this runs in the compose
    ``migration`` service with the app stopped, so the lock is uncontended and
    the rewrite is seconds. Revisit if the corpus ever reaches the millions —
    at that size the safe shape is a plain column + trigger + backfill in
    batches, not a generated column.
  * DROP TABLE / DROP COLUMN — none in ``upgrade()``. ``downgrade()`` drops
    exactly what ``upgrade()`` added; the only thing lost is DERIVED data that
    re-upgrading regenerates from ``document_chunks.text`` in full. Retrieval
    falls back to cosine-only in the meantime (``search_knowledge`` handles a
    missing column by design), so a downgrade degrades ranking, never breaks it.
  * Index creation without CONCURRENTLY — deliberate, on two grounds. (1) The
    table is already locked ACCESS EXCLUSIVE by the ADD COLUMN rewrite in this
    same transaction, so ``CONCURRENTLY`` could not reduce the blocking window;
    it would only lengthen it. (2) ``CONCURRENTLY`` cannot run inside a
    transaction block at all, so it would force this migration out of the
    transaction alembic commits — the exact shape that produced the silent
    "migrations logged success, database stayed empty" incident documented in
    ``alembic/env.py``. The app is down during migration and the table is small;
    plain CREATE INDEX is the correct call here.
  * lock_timeout — already set globally to 4s by ``alembic/env.py`` inside
    alembic's transaction block; nothing to add per-migration. Note this means a
    contended run FAILS FAST rather than blocking collection, which is the
    intended behaviour.
  * Single head — verified: this revises 69a24298167a, the sole prior head.

Revision ID: 2a7f1c9e4d30
Revises: 69a24298167a
Create Date: 2026-08-13 16:30:14.682827

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2a7f1c9e4d30'
down_revision: Union[str, Sequence[str], None] = '69a24298167a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies of infra_brain.db.models.core.DOCUMENT_CHUNKS_TSV_* — kept in
# sync by tests/test_rag_hybrid_ddl_parity.py, never imported (see docstring).
# IF NOT EXISTS on both, so this is a clean no-op on a database whose schema was
# built by create_all() (which runs the same DDL via an after_create listener)
# and then stamped.
_ADD_COLUMN = (
    "ALTER TABLE document_chunks "
    "ADD COLUMN IF NOT EXISTS text_tsv tsvector "
    "GENERATED ALWAYS AS (to_tsvector('english', coalesce(text, ''))) STORED"
)
_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_document_chunks_text_tsv "
    "ON document_chunks USING gin (text_tsv)"
)


def upgrade() -> None:
    """Upgrade schema."""
    if op.get_bind().dialect.name != "postgresql":
        # tsvector/to_tsvector/GIN are PostgreSQL-only. The suite runs on SQLite
        # and the migration chain is never applied there (tests/test_migration.py
        # gates on a postgresql:// DSN), but guarding is cheaper than assuming.
        return
    op.execute(_ADD_COLUMN)
    op.execute(_CREATE_INDEX)


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_text_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS text_tsv")
