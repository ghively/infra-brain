import sqlalchemy as sa
import pytest
from alembic.config import Config
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from infra_brain.config import get_settings
from tests.support.migration_gate import FAIL, RUN, classify, require_db_enabled


def _db_reachable() -> bool:
    """True if a real PostgreSQL migration target accepts a connection.

    alembic/env.py binds the migration URL unconditionally to
    ``get_settings().postgres_url`` (single source of truth — migrate and app can
    never diverge), so this test runs migrations against the live DSN. It must
    only run against PostgreSQL: the full migration chain includes ALTER-TABLE /
    add-FK steps (e.g. 0018) that SQLite cannot apply, so a SQLite DSN would error
    rather than meaningfully test. The test config now supplies a SQLite DSN by
    default (postgres_url is required), so gate strictly on a postgres scheme;
    locally/CI-without-Postgres this skips, and the ephemeral-Postgres
    `migration-parity` CI job runs it for real.
    """
    url = get_settings().postgres_url
    if not url.startswith("postgresql"):
        return False
    try:
        engine = sa.create_engine(url)
        with engine.connect():
            return True
    except sa.exc.OperationalError:
        return False
    except Exception:
        return False


def _require_db_or_gate() -> None:
    """Skip locally, but HARD-FAIL in CI when a mandatory Postgres is unreachable.

    Mirrors the migration-parity gate policy (TRK-022): when
    MIGRATION_PARITY_REQUIRE_DB is set (the CI job sets it), an unreachable or
    non-postgres DSN fails red instead of skip-as-pass, so the table-name parity
    check cannot go dark when Postgres is down. Without the flag (local dev), the
    benign skip is preserved.
    """
    url = get_settings().postgres_url
    action, message = classify(url, _db_reachable(), require_db=require_db_enabled())
    if action == RUN:
        return
    if action == FAIL:
        pytest.fail(message)
    pytest.skip(message)


def test_migration_creates_all_model_tables(tmp_path):
    _require_db_or_gate()

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    from infra_brain.db.models import Base

    engine = sa.create_engine(get_settings().postgres_url)
    inspector = sa.inspect(engine)
    actual = set(inspector.get_table_names())
    expected = set(Base.metadata.tables.keys())
    missing = expected - actual
    assert not missing, f"migration missing tables: {missing}"


def _run_op(engine, fn):
    """Drive a bare migration upgrade()/downgrade() against a live engine.

    Binds alembic ``op`` to a MigrationContext on the connection so the
    migration's inspector-guarded create/drop logic runs verbatim (no alembic
    version table or full chain needed) — used to round-trip a single revision
    against in-memory SQLite without a Postgres service.
    """
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            fn()


def test_0010_rapid7_relational_sqlite_round_trip():
    """0010 creates r7_assets/sites/tags on SQLite and downgrade drops them."""
    import importlib.util  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    mig_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0010_rapid7_relational.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0010", mig_path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    engine = sa.create_engine("sqlite://")
    # resources must exist for the r7_assets.resource_id FK reference.
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE resources (id CHAR(32) PRIMARY KEY)")

    _run_op(engine, mig.upgrade)
    tables = set(sa.inspect(engine).get_table_names())
    assert {"r7_assets", "r7_sites", "r7_tags"} <= tables
    cols = {c["name"] for c in sa.inspect(engine).get_columns("r7_assets")}
    # OS bug-fix columns + the six Rapid7 bands are present.
    assert {"os", "os_family", "os_product", "os_version", "os_vendor", "os_architecture"} <= cols
    assert {
        "vuln_critical",
        "vuln_severe",
        "vuln_moderate",
        "vuln_total",
        "vuln_exploits",
        "vuln_malware_kits",
    } <= cols

    _run_op(engine, mig.downgrade)
    tables_after = set(sa.inspect(engine).get_table_names())
    assert not ({"r7_assets", "r7_sites", "r7_tags"} & tables_after)


def _load_migration(filename: str):
    import importlib.util  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    mig_path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), mig_path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def test_0021_repair_r7_resource_id_drift_sqlite_round_trip():
    """0021 idempotently adds resource_id to r7_vulnerabilities/r7_solutions.

    Regression guard for the production 500 (column r7_vulnerabilities.resource_id
    does not exist). Simulates the drifted live DB: the r7 tables exist WITHOUT
    resource_id (as they did before 0018 was authored, and as the live DB stayed
    because 0018's ALTER never landed). The repair migration must add the column;
    running it a second time must be a no-op (idempotent existence guard).
    """
    mig = _load_migration("0021_repair_r7_resource_id_drift.py")

    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE resources (id CHAR(32) PRIMARY KEY)")
        # r7 tables WITHOUT resource_id — the drifted live-DB shape.
        conn.exec_driver_sql(
            "CREATE TABLE r7_vulnerabilities (id CHAR(32) PRIMARY KEY, r7_vuln_id VARCHAR(128))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE r7_solutions (id CHAR(32) PRIMARY KEY, r7_solution_id VARCHAR(128))"
        )

    def _has_resource_id(table: str) -> bool:
        return "resource_id" in {c["name"] for c in sa.inspect(engine).get_columns(table)}

    assert not _has_resource_id("r7_vulnerabilities")
    assert not _has_resource_id("r7_solutions")

    _run_op(engine, mig.upgrade)
    assert _has_resource_id("r7_vulnerabilities")
    assert _has_resource_id("r7_solutions")

    # Idempotent: a second run is a no-op (existence guards short-circuit).
    _run_op(engine, mig.upgrade)
    assert _has_resource_id("r7_vulnerabilities")
    assert _has_resource_id("r7_solutions")


def test_0021_repair_is_noop_when_column_present():
    """When 0018 already landed, 0021 leaves the existing column untouched."""
    mig = _load_migration("0021_repair_r7_resource_id_drift.py")

    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE resources (id CHAR(32) PRIMARY KEY)")
        conn.exec_driver_sql(
            "CREATE TABLE r7_vulnerabilities "
            "(id CHAR(32) PRIMARY KEY, r7_vuln_id VARCHAR(128), resource_id CHAR(32))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE r7_solutions "
            "(id CHAR(32) PRIMARY KEY, r7_solution_id VARCHAR(128), resource_id CHAR(32))"
        )

    # Must not raise even though the column already exists.
    _run_op(engine, mig.upgrade)
    cols = {c["name"] for c in sa.inspect(engine).get_columns("r7_vulnerabilities")}
    assert "resource_id" in cols


# --------------------------------------------------------------------------- #
# 0027 — pagination indexes, dead-index cleanup, risk_score widening (MR-F)
# --------------------------------------------------------------------------- #

# Indexes 0027 must ADD (backing API pagination + retention run-cascade queries).
_0027_NEW_INDEXES = {
    "octopus_deployments": {
        "ix_octopus_deployments_completed_at",
        "ix_octopus_deployments_project_id",
        "ix_octopus_deployments_environment_id",
    },
    "r7_software": {"ix_r7_software_vendor"},
    "resource_configs": {"ix_resource_configs_run_id"},
    "drift_events": {"ix_drift_events_collection_run_id"},
}

# Indexes 0027 must DROP (confirmed dead / redundant).
_0027_DEAD_INDEXES = {
    "custom_views": "ix_custom_views_share_token",
    "observations": "ix_observations_agent_tool",
    "octopus_events": "ix_octopus_events_occurred",
    "octopus_interruptions": "ix_octopus_interruptions_task_id",
    "r7_vulnerabilities": "ix_r7_vuln_resource",
    "r7_solutions": "ix_r7_solution_resource",
}


def _index_names(engine, table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(engine).get_indexes(table)}


def _build_pre_0027_sqlite() -> sa.Engine:
    """A SQLite DB in the pre-0027 (drifted-live) shape: dead indexes present,
    new indexes absent."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        # Tables that RECEIVE new indexes (created without them).
        conn.exec_driver_sql(
            "CREATE TABLE octopus_deployments "
            "(id CHAR(32) PRIMARY KEY, completed_at DATETIME, "
            "project_id VARCHAR(64), environment_id VARCHAR(64))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE r7_software (id CHAR(32) PRIMARY KEY, vendor VARCHAR(128))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE resource_configs (id CHAR(32) PRIMARY KEY, run_id CHAR(32))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE drift_events (id CHAR(32) PRIMARY KEY, collection_run_id CHAR(32))"
        )
        # Tables that carry a DEAD index (created WITH it, to be dropped).
        conn.exec_driver_sql(
            "CREATE TABLE custom_views (id CHAR(32) PRIMARY KEY, share_token VARCHAR(32))"
        )
        conn.exec_driver_sql(
            "CREATE INDEX ix_custom_views_share_token ON custom_views (share_token)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE observations (id CHAR(32) PRIMARY KEY, agent VARCHAR(64), tool VARCHAR(128))"
        )
        conn.exec_driver_sql(
            "CREATE INDEX ix_observations_agent_tool ON observations (agent, tool)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE octopus_events (id CHAR(32) PRIMARY KEY, occurred DATETIME)"
        )
        conn.exec_driver_sql("CREATE INDEX ix_octopus_events_occurred ON octopus_events (occurred)")
        conn.exec_driver_sql(
            "CREATE TABLE octopus_interruptions (id CHAR(32) PRIMARY KEY, task_id VARCHAR(64))"
        )
        conn.exec_driver_sql(
            "CREATE INDEX ix_octopus_interruptions_task_id ON octopus_interruptions (task_id)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE r7_vulnerabilities (id CHAR(32) PRIMARY KEY, resource_id CHAR(32))"
        )
        conn.exec_driver_sql("CREATE INDEX ix_r7_vuln_resource ON r7_vulnerabilities (resource_id)")
        conn.exec_driver_sql(
            "CREATE TABLE r7_solutions (id CHAR(32) PRIMARY KEY, resource_id CHAR(32))"
        )
        conn.exec_driver_sql("CREATE INDEX ix_r7_solution_resource ON r7_solutions (resource_id)")
    return engine


def test_0027_adds_pagination_and_retention_indexes():
    """0027 creates every missing pagination / retention index; idempotent."""
    mig = _load_migration("0027_pagination_indexes_and_dead_index_cleanup.py")
    engine = _build_pre_0027_sqlite()

    # Precondition: none of the new indexes exist yet.
    for table, names in _0027_NEW_INDEXES.items():
        assert not (names & _index_names(engine, table))

    _run_op(engine, mig.upgrade)
    for table, names in _0027_NEW_INDEXES.items():
        assert names <= _index_names(engine, table), f"missing new indexes on {table}"

    # Idempotent: re-running is a guarded no-op, not an error.
    _run_op(engine, mig.upgrade)
    for table, names in _0027_NEW_INDEXES.items():
        assert names <= _index_names(engine, table)


def test_0027_drops_dead_indexes_and_round_trips():
    """0027 drops every confirmed-dead index; downgrade restores them."""
    mig = _load_migration("0027_pagination_indexes_and_dead_index_cleanup.py")
    engine = _build_pre_0027_sqlite()

    # Precondition: all dead indexes present.
    for table, name in _0027_DEAD_INDEXES.items():
        assert name in _index_names(engine, table)

    _run_op(engine, mig.upgrade)
    for table, name in _0027_DEAD_INDEXES.items():
        assert name not in _index_names(engine, table), f"dead index {name} not dropped"

    # Idempotent drop.
    _run_op(engine, mig.upgrade)
    for table, name in _0027_DEAD_INDEXES.items():
        assert name not in _index_names(engine, table)

    # Downgrade restores the dead indexes and removes the added ones.
    _run_op(engine, mig.downgrade)
    for table, name in _0027_DEAD_INDEXES.items():
        assert name in _index_names(engine, table), f"downgrade did not restore {name}"
    for table, names in _0027_NEW_INDEXES.items():
        assert not (names & _index_names(engine, table))


def test_0027_is_integer_type_helper():
    """The risk_score widening guard fires on integer types, not float types."""
    mig = _load_migration("0027_pagination_indexes_and_dead_index_cleanup.py")
    assert mig._is_integer_type(sa.Integer())
    assert mig._is_integer_type(sa.SmallInteger())
    assert mig._is_integer_type(sa.BigInteger())
    assert not mig._is_integer_type(sa.Float())
    assert not mig._is_integer_type(sa.Numeric())
    # Reflected type strings (what the inspector actually returns).
    assert mig._is_integer_type("INTEGER")
    assert not mig._is_integer_type("DOUBLE PRECISION")


def test_risk_score_columns_are_float_in_models():
    """Regression guard: r7 risk_score columns must never narrow to an integer
    type (integer columns silently truncate the float scores collectors write)."""
    from infra_brain.db.models import R7Asset, R7Site, R7Vulnerability

    checks = [
        (R7Asset, "risk_score"),
        (R7Asset, "raw_risk_score"),
        (R7Site, "risk_score"),
        (R7Vulnerability, "risk_score"),
    ]
    for model, column in checks:
        col_type = model.__table__.c[column].type
        assert isinstance(col_type, sa.Float), (
            f"{model.__name__}.{column} must be Float, got {type(col_type).__name__}"
        )


def test_0027_model_metadata_matches_migration_intent():
    """Base.metadata declares exactly the indexes 0027 reconciles: the new ones
    present, the dead ones absent — so a fresh create_all + 0027 stay in sync."""
    from infra_brain.db.models import Base

    all_index_names = {ix.name for t in Base.metadata.tables.values() for ix in t.indexes}
    for names in _0027_NEW_INDEXES.values():
        assert names <= all_index_names, (
            f"model missing declared indexes: {names - all_index_names}"
        )
    for name in _0027_DEAD_INDEXES.values():
        assert name not in all_index_names, f"dead index {name} still declared in models"
