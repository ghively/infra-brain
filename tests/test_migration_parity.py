"""Migration parity gate -- two complementary tests.

test_0001_does_not_precreate_later_migration_tables: Fast static check (no
PostgreSQL required). Verifies that the _tables_owned_by_later_migrations()
function in 0001_initial_schema returns a non-empty set and that those tables
are NOT in the set 0001 would create. Root cause prevented (2026-07-08
incident, MR !110/!111): 0001 originally used Base.metadata.create_all() which
pre-created ALL tables; later migrations' existence guards then silently skipped
their DDL in CI, hiding unique INDEX vs unique CONSTRAINT drift from compare_metadata.

test_migrated_schema_matches_models: Full Postgres-backed check. Applies alembic
upgrade head to an empty database (0001 restricted create_all first, then each
migration applies real DDL), then compare_metadata diffs result vs Base.metadata.
Fails on ANY difference. Skips locally without POSTGRES_URL; CI always sets it.
"""

import importlib.util
import os

import pytest
import sqlalchemy as sa

from infra_brain.config import get_settings
from infra_brain.db.models import Base
from tests.support.migration_gate import FAIL, RUN, classify, require_db_enabled


def _load_0001():
    """Dynamically import 0001_initial_schema without going through alembic."""
    candidates = [
        os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions", "0001_initial_schema.py"
        ),
    ]
    for c in candidates:
        path = os.path.normpath(c)
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("_migration_0001", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    pytest.fail("Could not locate alembic/versions/0001_initial_schema.py relative to tests/")


def _postgres_engine_or_skip():
    """Return a connectable PostgreSQL engine, or skip/fail per the CI gate policy.

    Skips when no PostgreSQL is reachable *and* the DB was not declared mandatory
    (local dev). When the migration-parity CI job sets MIGRATION_PARITY_REQUIRE_DB,
    an unreachable/unusable DB HARD-FAILS instead of skip-as-pass (TRK-022) — so a
    down or misconfigured Postgres can never let this gate go green silently.
    """
    url = get_settings().postgres_url
    engine = None
    connect_ok = False
    detail = ""
    if url.startswith("postgresql"):
        try:
            engine = sa.create_engine(url)
            with engine.connect():
                connect_ok = True
        except Exception as exc:  # noqa: BLE001
            detail = f" ({exc})"

    action, message = classify(url, connect_ok, require_db=require_db_enabled())
    if action == RUN:
        return engine
    if action == FAIL:
        pytest.fail(message + detail)
    pytest.skip(message + detail)


def test_0001_does_not_precreate_later_migration_tables():
    """0001 must not pre-create tables owned by later migrations.

    Structural guard against the 2026-07-08 index-vs-constraint drift class.
    Verifies three properties without needing a live database:

    1. _tables_owned_by_later_migrations() returns a non-empty set -- confirms
       the scanner in 0001 is wired and finding real tables.
    2. None of the later-migration tables appear in what 0001 would create
       (Base.metadata tables NOT in the later-migration exclusion set). If any
       overlap exists, those migrations' existence-guard DDL is silently skipped
       in CI, hiding index-vs-constraint drift until the production deploy gate.
    3. Every table returned by the scanner exists in Base.metadata -- sanity-checks
       that the regex patterns match only real table names.
    """
    mod = _load_0001()

    later_tables: frozenset = mod._tables_owned_by_later_migrations()

    # 1. Scanner must find at least one table.
    assert later_tables, (
        "_tables_owned_by_later_migrations() returned an empty set. "
        "Either 0001 no longer calls this function, or all later migrations "
        "were removed. The structural guard against pre-creation drift is inactive."
    )

    # 2. No later-migration table should be in what 0001 would create.
    tables_0001_would_create = {
        t.name for t in Base.metadata.sorted_tables if t.name not in later_tables
    }
    overlap = later_tables & tables_0001_would_create
    assert not overlap, (
        f"0001 would still pre-create {len(overlap)} table(s) owned by later "
        f"migrations: {sorted(overlap)}. "
        "Those migrations existence-guard DDL will be silently skipped in CI, "
        "hiding index-vs-constraint drift. "
        "Fix: ensure _tables_owned_by_later_migrations() scanner covers the "
        "op.create_table / table not in existing_tables patterns."
    )

    # 3. Every detected table must be in Base.metadata.
    all_model_tables = {t.name for t in Base.metadata.sorted_tables}
    unknown = later_tables - all_model_tables
    assert not unknown, (
        f"_tables_owned_by_later_migrations() returned {len(unknown)} name(s) "
        f"not found in Base.metadata: {sorted(unknown)}. "
        "The scanner matched a string that is not a real model table. "
        "Review the regex patterns in _tables_owned_by_later_migrations()."
    )


def test_migrated_schema_matches_models():
    """alembic head schema must be column-for-column identical to Base.metadata.

    Applies every migration in the chain to a clean PostgreSQL database (starting
    from empty -- not from a create_all shortcut), then uses alembic
    compare_metadata to diff the result against the current models. Fails on ANY
    difference: missing/extra column, type mismatch, wrong index/constraint form.

    Because 0001_initial_schema.upgrade() no longer pre-creates tables owned by
    later migrations, this test now exercises the REAL historical migration chain:
    0001 creates only the base tables; each subsequent migration applies its own
    DDL (CREATE TABLE, CREATE INDEX, ADD CONSTRAINT, etc.) in sequence. Index-vs-
    constraint drift introduced in any migration surfaces here as a remove_index +
    add_constraint diff pair, exactly as it would at the production deploy gate --
    not after a crash-loop on deploy.
    """
    engine = _postgres_engine_or_skip()

    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext

    from alembic import command
    from infra_brain.db.schema_check import _include_name

    # Apply every migration to the live (ephemeral CI) database.
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    # Same include_name hook the runtime assert_schema_current() startup gate
    # uses -- both must agree on what counts as drift.
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={"include_name": _include_name, "compare_server_default": True},
        )
        diffs = compare_metadata(ctx, Base.metadata)

    if diffs:
        rendered = "\n".join(f"  - {d}" for d in diffs)
        pytest.fail(
            "Schema drift between migrations (alembic head) and models "
            f"(Base.metadata):\n{rendered}\n\n"
            "Every model change needs a matching migration. Generate one with "
            "`alembic revision --autogenerate` and review it."
        )
