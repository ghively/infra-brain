"""Startup schema-drift detection (the deploy-correctness keystone).

The production 500 (``column r7_vulnerabilities.resource_id does not exist``)
shipped *green* because nothing asserted that the live DB matched what the code
declares: migration 0018 stamped ``alembic_version`` past itself while running
against a divergent/empty DB, so ``alembic upgrade head`` became a no-op and
``create_all()`` issued no ALTER on the existing (drifted) table. SELECTs then
500'd in prod.

This module gives the FastAPI lifespan a loud, deploy-blocking assertion:

  * the live DB's ``alembic_version`` must equal the migration ScriptDirectory
    head, AND
  * the live schema has NO column/type/index differences vs ``Base.metadata``
    (full ``compare_metadata`` scan — same method used by the CI
    ``migration-parity`` job, so the runtime guard and CI use identical logic).

The previous implementation used a hardcoded allowlist of previously-known-drifted
columns.  That allowlist approach fails silently for any new drift column that isn't
listed.  The full ``compare_metadata`` approach catches ANY drift — missing/extra
columns, type mismatches, missing indexes or FKs — regardless of which table is
affected.

On drift it logs every difference and raises ``SchemaDriftError`` so the process
exits non-zero and the deploy health gate goes red — converting silent column drift
into an obvious failure regardless of how the DSN resolved.

CRITICAL — must NOT false-trip in tests. The suite runs on in-memory SQLite via
``Base.metadata.create_all`` (no alembic_version table, no stamped revision). The
check is therefore gated to PostgreSQL only: ``assert_schema_current`` is a no-op
on any non-postgresql dialect, so TestClient app startup and the full suite are
unaffected. It enforces solely against the real Postgres deploy.

Performance note: ``compare_metadata`` reflects all DB tables once per startup.
Against a ~30-table Alembic-managed schema this takes well under 100 ms; it is not
a startup bottleneck.

Exclusions: langgraph-checkpoint-postgres owns ``checkpoints``, ``checkpoint_blobs``,
``checkpoint_writes``, and ``checkpoint_migrations`` (created by
``PostgresSaver``/``AsyncPostgresSaver.setup()`` in ``infra_brain/checkpointer.py``).
They are intentionally absent from ``Base.metadata`` and must NOT be reported as
drift — see ``_LANGGRAPH_MANAGED_TABLES`` / ``_include_name``.

Same pattern applies to individual indexes that can't be expressed as a plain
SQLAlchemy ``Index()``, all built ``CONCURRENTLY`` via raw ``op.execute()`` and
intentionally absent from ``Base.metadata`` — see ``_UNMODELED_INDEXES``:
  * ``ix_resources_name_trgm`` (migration 0029) — a Postgres GIN trigram index
    (``USING gin (lower(name) gin_trgm_ops)``); no SQLAlchemy-metadata
    equivalent for a functional/expression index with a custom operator class.
  * ``ix_rr_status_archived`` (migration 0030) — a partial index
    (``WHERE status = 'archived'``); SQLAlchemy's declarative ``Index()``
    doesn't have a first-class way to express a partial-index predicate that
    round-trips through ``compare_metadata`` cleanly either.

The same escape hatch now covers one *column* — see ``_UNMODELED_COLUMNS``:
  * ``document_chunks.text_tsv`` (TRK-297 R6, migration ``2a7f1c9e4d30``) — a
    ``GENERATED ALWAYS AS (to_tsvector(...)) STORED`` column that exists only on
    PostgreSQL, together with its GIN index ``ix_document_chunks_text_tsv``.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


class SchemaDriftError(RuntimeError):
    """Raised when the live DB schema does not match the migrated/declared schema."""


def _script_head() -> str | None:
    """The single head revision from the alembic ScriptDirectory, or None."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    sd = ScriptDirectory.from_config(cfg)
    heads = sd.get_heads()
    if len(heads) != 1:
        # A branched/empty history is itself a problem worth surfacing.
        log.error("[schema-check] expected exactly one alembic head, found: %s", heads)
        return None
    return heads[0]


def _db_revision(engine: Engine) -> str | None:
    """The version_num stamped in the live DB's alembic_version table, or None."""
    insp = inspect(engine)
    if "alembic_version" not in insp.get_table_names():
        return None
    with engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


# Tables created and owned entirely by langgraph-checkpoint-postgres's
# PostgresSaver/AsyncPostgresSaver.setup() (see infra_brain/checkpointer.py) —
# not declared in infra_brain.db.models.Base.metadata and not managed by our
# Alembic migrations. compare_metadata() has no way to know these are
# intentional: it reports each one as ``remove_table``/``remove_index`` drift
# purely because they exist in the live DB but not in Base.metadata. Excluding
# them here is the correct fix — NOT a migration to drop them, which would
# destroy live chat-memory/thread state.
#
# ``legacy_edges_archive`` rides along in this set despite not being a LangGraph
# table, because it is the same KIND of object: present in the database, owned
# by something other than ``Base.metadata``, and correctly excluded rather than
# dropped. The P5 migration (``95d988b2bc3c``) creates it only where its
# derivability audit met a relationship type it had not classified — so it is
# absent on production and on every from-empty replay, and giving it an ORM
# model would model a table whose purpose is to be inert. ``alembic/env.py``
# carries the mirror entry; the two comparison configs must stay aligned (see
# the 2026-07-30 server_default incident note there for what happens when they
# drift apart).
_LANGGRAPH_MANAGED_TABLES = frozenset(
    {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
        "legacy_edges_archive",
    }
)

# (table_name, index_name) pairs that exist in the migrated DB but can never
# appear in Base.metadata — raw-SQL functional/partial indexes with no
# SQLAlchemy Index() equivalent. See module docstring.
#
# ``("resource_relationships", "ix_rr_status_archived")`` was the second entry
# here until P5 dropped that table
# (docs/decisions/2026-08-11-graph-first-architecture.md). Both the index and
# its table are gone from the database, and the model is gone from
# ``Base.metadata``, so there is nothing left for either side of the comparison
# to disagree about — an exclusion for a pair that exists nowhere is dead
# weight that reads as "this index is still out there somewhere".
_UNMODELED_INDEXES = frozenset(
    {
        ("resources", "ix_resources_name_trgm"),
        # TRK-297 R6: GIN index over the generated tsvector column below. A GIN
        # index on a column that isn't in Base.metadata can't be in
        # Base.metadata either.
        ("document_chunks", "ix_document_chunks_text_tsv"),
    }
)

# (table_name, column_name) pairs that exist on PostgreSQL but are deliberately
# absent from Base.metadata. Same class of object as _UNMODELED_INDEXES: the DB
# owns the definition because the ORM cannot express it portably.
#
# ``document_chunks.text_tsv`` (TRK-297 R6) is a
# ``GENERATED ALWAYS AS (to_tsvector('english', ...)) STORED`` column. SQLite
# validates generated-column expressions at CREATE TABLE time and has no
# ``to_tsvector``, so a ``Computed()`` on the model would break
# ``create_all()`` for the whole SQLite suite; declaring it *without*
# ``Computed`` instead makes alembic warn "Computed default on
# document_chunks.text_tsv cannot be modified" on every single compare — here,
# in the migration-parity gate, and in ``alembic check``. Excluding it outright
# is the honest option. See ``db/models/core.py`` for the full rationale and the
# DDL, and ``alembic/env.py`` for the mirror entry.
_UNMODELED_COLUMNS = frozenset(
    {
        ("document_chunks", "text_tsv"),
    }
)


def _include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """``compare_metadata`` ``include_name`` hook — skip langgraph-owned objects
    and known unmodeled raw-SQL columns/indexes."""
    if type_ == "table":
        return name not in _LANGGRAPH_MANAGED_TABLES
    if type_ in ("index", "unique_constraint", "column", "foreign_key_constraint"):
        table_name = parent_names.get("table_name")
        if table_name in _LANGGRAPH_MANAGED_TABLES:
            return False
        if type_ == "index" and (table_name, name) in _UNMODELED_INDEXES:
            return False
        if type_ == "column" and (table_name, name) in _UNMODELED_COLUMNS:
            return False
        return True
    return True


def _full_compare(engine: Engine) -> list:
    """Diff the live DB schema against Base.metadata using alembic compare_metadata.

    Returns a list of diff tuples (same contract as the CI ``migration-parity``
    job's ``compare_metadata`` call).  An empty list means no drift.

    Excludes langgraph-checkpoint-postgres-managed tables (see
    ``_LANGGRAPH_MANAGED_TABLES``) via ``include_name`` — those tables are
    intentionally absent from ``Base.metadata`` and their presence in the live
    DB is expected, not drift.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from infra_brain.db.models import Base

    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={"include_name": _include_name, "compare_server_default": True},
        )
        return compare_metadata(ctx, Base.metadata)


def assert_schema_current(engine: Engine) -> None:
    """Assert the live DB matches the migrated schema; raise SchemaDriftError on drift.

    No-op on any non-PostgreSQL dialect (SQLite test/TestClient env) so it never
    false-trips the suite. Enforces only against the real Postgres deploy.

    Uses alembic ``compare_metadata`` for a full schema diff — the same method the
    CI ``migration-parity`` job uses — rather than a hardcoded column allowlist.
    This catches ANY drift (missing/extra column, type mismatch, missing index or
    FK) on ANY table, not only previously-known-drifted columns.
    """
    dialect = engine.dialect.name
    if dialect != "postgresql":
        log.debug("[schema-check] dialect=%s — skipping drift assertion (non-postgres)", dialect)
        return

    problems: list[str] = []

    # 1. Revision parity: live alembic_version == ScriptDirectory head.
    head = _script_head()
    current = _db_revision(engine)
    if head is None:
        problems.append("could not resolve a single alembic ScriptDirectory head")
    elif current is None:
        problems.append(
            "live DB has no alembic_version stamp (migrations never ran against this DB)"
        )
    elif current != head:
        problems.append(
            f"alembic revision drift: live DB at {current!r} but repo head is {head!r} "
            f"(run `alembic upgrade head` against the live DB)"
        )

    # 2. Full schema diff: compare live DB column-for-column against Base.metadata.
    #    This is the same compare_metadata call used by the CI migration-parity job.
    #    An empty diff list means no drift; any entry is a concrete schema difference.
    diffs = _full_compare(engine)
    for diff in diffs:
        problems.append(
            f"schema diff detected: {diff} "
            f"(model declares this; a migration was never applied — "
            f"the exact drift class that caused the production 500)"
        )

    if problems:
        for p in problems:
            log.error("[schema-check] SCHEMA DRIFT: %s", p)
        raise SchemaDriftError(
            "Startup aborted — live DB schema does not match the migrated schema:\n  - "
            + "\n  - ".join(problems)
        )

    log.info(
        "[schema-check] live DB schema is current (head=%s, compare_metadata found no diffs)", head
    )
