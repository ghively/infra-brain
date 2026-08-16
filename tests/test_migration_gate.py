"""Unit tests for the migration-parity gate decision logic (TRK-022).

Proves the silent-DB-down hole is closed: when the CI job declares PostgreSQL
mandatory (MIGRATION_PARITY_REQUIRE_DB set), an unreachable or non-postgres DSN
resolves to a HARD FAILURE, never a skip-as-pass. Without the flag (local dev),
the benign skip is preserved. No live database required — classify() is pure.
"""

import pytest

from tests.support.migration_gate import (
    FAIL,
    REQUIRE_DB_ENV,
    RUN,
    SKIP,
    classify,
    require_db_enabled,
)

PG = "postgresql://infra:infra@postgres:5432/infra_brain_ci"
SQLITE = "sqlite:///:memory:"


# --- require_db_enabled ----------------------------------------------------- #


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "on", " on "])
def test_require_db_enabled_truthy(val):
    assert require_db_enabled({REQUIRE_DB_ENV: val}) is True


@pytest.mark.parametrize(
    "env", [{}, {REQUIRE_DB_ENV: ""}, {REQUIRE_DB_ENV: "0"}, {REQUIRE_DB_ENV: "no"}]
)
def test_require_db_enabled_falsy(env):
    assert require_db_enabled(env) is False


# --- classify: the core skip-vs-fail contract ------------------------------- #


def test_reachable_postgres_runs_regardless_of_flag():
    assert classify(PG, connect_ok=True, require_db=True)[0] == RUN
    assert classify(PG, connect_ok=True, require_db=False)[0] == RUN


def test_unreachable_postgres_fails_when_required():
    """THE hole this gate closes: DB down + mandatory => FAIL, not skip."""
    action, message = classify(PG, connect_ok=False, require_db=True)
    assert action == FAIL
    assert "FAILED" in message
    assert "skip-as-pass" in message


def test_unreachable_postgres_skips_when_not_required():
    """Local dev without Postgres still skips (legitimate)."""
    action, _ = classify(PG, connect_ok=False, require_db=False)
    assert action == SKIP


def test_non_postgres_dsn_fails_when_required():
    """A misconfigured (non-postgres) DSN in CI must fail, not silently pass."""
    action, message = classify(SQLITE, connect_ok=False, require_db=True)
    assert action == FAIL
    assert "PostgreSQL DSN" in message


def test_non_postgres_dsn_skips_when_not_required():
    action, _ = classify(SQLITE, connect_ok=False, require_db=False)
    assert action == SKIP


def test_non_postgres_dsn_never_runs_even_if_connect_ok():
    """connect_ok on a non-postgres URL must not fool the gate into running."""
    assert classify(SQLITE, connect_ok=True, require_db=True)[0] == FAIL
    assert classify(SQLITE, connect_ok=True, require_db=False)[0] == SKIP
