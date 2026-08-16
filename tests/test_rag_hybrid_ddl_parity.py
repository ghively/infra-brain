"""TRK-297 R6 — the two emitters of the ``text_tsv`` DDL must not drift apart.

``document_chunks.text_tsv`` is created from two places on purpose:

  * ``infra_brain/db/models/core.py`` — an ``after_create`` listener, so
    ``Base.metadata.create_all()`` paths (the gate-4 ORM tests on real
    PostgreSQL, fresh dev DBs) get the column; and
  * ``alembic/versions/2a7f1c9e4d30_*.py`` — the production upgrade path.

The migration deliberately carries FROZEN literal copies rather than importing
the constants: a migration is a point-in-time snapshot, and importing a live app
constant is exactly how ``_EMBED_DIM`` came to need ``06e3b86260d5`` and then
``7aae7d4d33eb`` to undo it.

The cost of that choice is that the copies can silently diverge — and nothing
else would catch it, because the column is excluded from ``compare_metadata`` (it
is unmodeled by design), so migration-parity is structurally blind to it. This
static check is the thing that catches it. No database required.
"""

from pathlib import Path

import pytest

from infra_brain.db.models import (
    DOCUMENT_CHUNKS_TSV_ADD_COLUMN_DDL,
    DOCUMENT_CHUNKS_TSV_COLUMN,
    DOCUMENT_CHUNKS_TSV_EXPR,
    DOCUMENT_CHUNKS_TSV_INDEX,
    DOCUMENT_CHUNKS_TSV_INDEX_DDL,
)

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "2a7f1c9e4d30_trk297_r6_document_chunks_fts_tsvector.py"
)


def _migration_source() -> str:
    assert _MIGRATION.is_file(), f"migration not found at {_MIGRATION}"
    return _MIGRATION.read_text(encoding="utf-8")


def _normalised(sql: str) -> str:
    """Compare the SQL, not the Python that spells it.

    The migration writes its copies as adjacent string literals
    (``"ALTER TABLE ... " "ADD COLUMN ..."``), so the double quotes at each
    wrap point are dropped and runs of whitespace collapsed. The DDL itself
    contains only single quotes (``'english'``), which survive untouched.
    """
    return " ".join(sql.replace('"', " ").split())


@pytest.mark.parametrize(
    "ddl",
    [DOCUMENT_CHUNKS_TSV_ADD_COLUMN_DDL, DOCUMENT_CHUNKS_TSV_INDEX_DDL],
    ids=["add_column", "create_index"],
)
def test_migration_carries_the_same_ddl_as_the_orm_listener(ddl):
    """Normalising both sides, the migration's frozen copy must reproduce the
    model-side DDL exactly. If this fails, one of the two was edited alone —
    decide which is right, then fix the other (and if the EXPRESSION changed,
    that needs a NEW migration, not an edit to this one)."""
    source = _normalised(_migration_source())
    assert _normalised(ddl) in source, (
        f"alembic/versions/{_MIGRATION.name} no longer contains the DDL declared in "
        f"infra_brain/db/models/core.py:\n  {_normalised(ddl)}"
    )


def test_ddl_constants_are_internally_consistent():
    """Guards against renaming the column/index constant without rebuilding the
    DDL strings that embed them."""
    assert DOCUMENT_CHUNKS_TSV_COLUMN in DOCUMENT_CHUNKS_TSV_ADD_COLUMN_DDL
    assert DOCUMENT_CHUNKS_TSV_EXPR in DOCUMENT_CHUNKS_TSV_ADD_COLUMN_DDL
    assert DOCUMENT_CHUNKS_TSV_INDEX in DOCUMENT_CHUNKS_TSV_INDEX_DDL
    assert DOCUMENT_CHUNKS_TSV_COLUMN in DOCUMENT_CHUNKS_TSV_INDEX_DDL
    # The two-argument, literal-regconfig form is what makes to_tsvector
    # IMMUTABLE, which is what makes it legal in a generated column at all.
    assert "to_tsvector('english'" in DOCUMENT_CHUNKS_TSV_EXPR


def test_unmodeled_column_is_excluded_from_both_comparison_configs():
    """``schema_check`` (runtime deploy gate + migration-parity) and
    ``alembic/env.py`` (autogenerate + ``alembic check``) must BOTH ignore the
    column and its index. Missing either one turns every future autogenerate
    into a proposal to drop them."""
    from infra_brain.db.schema_check import (
        _include_name,
        _UNMODELED_COLUMNS,
        _UNMODELED_INDEXES,
    )

    table = "document_chunks"
    assert (table, DOCUMENT_CHUNKS_TSV_COLUMN) in _UNMODELED_COLUMNS
    assert (table, DOCUMENT_CHUNKS_TSV_INDEX) in _UNMODELED_INDEXES

    # The hook itself, not just the data it reads from.
    parents = {"table_name": table}
    assert _include_name(DOCUMENT_CHUNKS_TSV_COLUMN, "column", parents) is False
    assert _include_name(DOCUMENT_CHUNKS_TSV_INDEX, "index", parents) is False
    assert _include_name("text", "column", parents) is True  # ordinary columns unaffected

    # env.py executes migrations on import, so it is read as text, not imported.
    env_source = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )
    assert f'"{DOCUMENT_CHUNKS_TSV_INDEX}"' in env_source
    assert f'("{table}", "{DOCUMENT_CHUNKS_TSV_COLUMN}")' in env_source
