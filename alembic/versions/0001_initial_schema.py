"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-18 00:00:00.000000

This migration creates the base schema from the SQLAlchemy models via
``Base.metadata.create_all`` — BUT only for tables that 0001 "owns", meaning
tables that are NOT created by any later migration in the chain.

Why the exclusion matters (2026-07-08 incident, MR !110 / !111):
-----------------------------------------------------------------
The original ``Base.metadata.create_all(bind=op.get_bind())`` created ALL tables
— including ones that later migrations (e.g. 0031) were supposed to introduce
via ``op.create_table`` or existence-guarded DDL. This caused those migrations'
``if "<table>" not in existing_tables`` guards to evaluate False in CI, so their
unique-index DDL silently never executed. The CI DB ended up in the model-correct
state (via create_all) while the production DB ended up in the migration-correct
state (via actual migration DDL) — and the two diverged (unique INDEX vs unique
CONSTRAINT). The app's ``compare_metadata`` startup gate caught the divergence on
deploy (not in CI), causing a crash loop.

The fix: 0001 must create ONLY the tables it owns. Later migrations own the
tables they introduce. ``_tables_owned_by_later_migrations()`` scans the sibling
migration files to build this exclusion set automatically — it is self-maintaining
as new migrations are added.

Invariant enforced by ``tests/test_migration_parity.py``:
  * test_0001_does_not_precreate_later_migration_tables — static check (no PG)
  * test_migrated_schema_matches_models — full alembic head vs Base.metadata diff

To generate incremental migrations against a live PostgreSQL database, run:
    alembic revision --autogenerate -m "migration description"
"""

from __future__ import annotations

import ast
import glob
import os
import re

from alembic import op

from infra_brain.db.models import Base

# revision identifiers, used by Alembic.
revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def _tables_owned_by_later_migrations() -> frozenset[str]:
    """Return the set of table names introduced by migrations other than 0001.

    Scans every sibling ``*.py`` file in the ``alembic/versions/`` directory
    for two patterns:

    1. Unconditional: ``op.create_table("tablename", ...)``
    2. Conditional:  ``if "tablename" not in existing_tables:``

    Both patterns indicate that the migration is responsible for creating that
    table. 0001 must NOT pre-create these via ``create_all`` or the guards in
    those migrations will short-circuit in CI and their DDL (indexes, constraints,
    column defaults) will silently never execute — causing index-vs-constraint
    drift between CI and production.

    This function is self-maintaining: adding a new migration that uses either
    pattern will automatically cause 0001 to exclude the new table.

    RETIRED TABLES ARE SUBTRACTED, and that is not a convenience. A table can
    be created by one migration and DROPPED by a later one —
    ``resource_relationships`` (created by ``0017``, dropped by P5's
    ``95d988b2bc3c``) is the first case in this chain. Such a table is correctly
    absent from ``Base.metadata`` at head, so leaving it in this set breaks the
    invariant ``tests/test_migration_parity.py`` checks third: "every name this
    scanner returns is a real model table". Subtracting retirements keeps that
    invariant a genuine sanity check on the PATTERNS (its actual purpose —
    catching a match that is not a table name) instead of a tripwire that fires
    every time a table is legitimately retired. Nothing is lost on the exclusion
    side either: a table absent from ``Base.metadata`` cannot be created by
    ``create_all``, so excluding it from 0001 was already a no-op.

    WHY THE RETIREMENT SCAN IS AST-BASED WHILE THE OTHER TWO ARE REGEX. Almost
    every migration that creates a table also DROPS it — in ``downgrade()``.
    A regex for ``op.drop_table("x")`` therefore matches nearly the whole
    schema, and subtracting that set makes 0001 pre-create tables their own
    migrations then fail to create ("relation already exists"). The distinction
    that matters is not create-vs-drop, it is WHICH FUNCTION the call is in, and
    that is a question about structure, not text. So retirements are read from
    the ``upgrade()`` body only, via ``ast``. The two additive patterns stay on
    regex because a false positive there is harmless (an extra exclusion) and
    they must keep matching the conditional ``not in existing_tables`` form,
    which is a comparison rather than a call.
    """
    versions_dir = os.path.dirname(os.path.abspath(__file__))
    owned: set[str] = set()
    retired: set[str] = set()
    for path in sorted(glob.glob(os.path.join(versions_dir, "*.py"))):
        fname = os.path.basename(path)
        # Skip this file and __init__.py / __pycache__ entries.
        if fname.startswith("0001") or fname.startswith("__"):
            continue
        text = open(path).read()  # noqa: WPS515  (open without context manager — intentional hot-path)
        # Pattern 1: op.create_table("tablename", ...)
        for m in re.finditer(r'op\.create_table\(\s*["\'](\w+)["\']', text):
            owned.add(m.group(1))
        # Pattern 2: if "tablename" not in existing_tables:
        for m in re.finditer(r'"(\w+)"\s+not in existing_tables', text):
            owned.add(m.group(1))
        # Pattern 3 (subtractive): op.drop_table("tablename") inside upgrade().
        retired |= _tables_dropped_by_upgrade(text)
    return frozenset(owned - retired)


def _tables_dropped_by_upgrade(source: str) -> set[str]:
    """Table names passed to ``op.drop_table("literal")`` inside ``upgrade()``.

    A string literal is required: a call like ``op.drop_table(TABLE)`` resolves
    a module constant, which static analysis cannot follow, so such a migration
    must spell the name out to be seen as a retirement. ``95d988b2bc3c`` does
    exactly that and says why at the call site.

    A file that does not parse (or has no ``upgrade``) contributes nothing —
    this scanner runs inside ``0001.upgrade()`` on every from-empty replay, and
    a syntax error in some unrelated version file should not take the whole
    migration chain down with it.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    dropped: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "upgrade":
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (isinstance(func, ast.Attribute) and func.attr == "drop_table"):
                continue
            if call.args and isinstance(call.args[0], ast.Constant):
                value = call.args[0].value
                if isinstance(value, str):
                    dropped.add(value)
    return dropped


def upgrade() -> None:
    later_tables = _tables_owned_by_later_migrations()
    # Only create the tables that 0001 owns. Tables belonging to later migrations
    # must be left for those migrations to create with their own DDL — which may
    # include unique indexes, specific column defaults, or FK variants that differ
    # from what create_all would emit for the current model definition.
    tables_to_create = [t for t in Base.metadata.sorted_tables if t.name not in later_tables]
    Base.metadata.create_all(bind=op.get_bind(), tables=tables_to_create)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
