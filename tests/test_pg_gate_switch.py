"""The agent-orm-check gate's own switch (TRK-356).

``tests/support/pg.py`` decides, from one env var, whether the whole ORM test
surface runs on in-memory SQLite or on real PostgreSQL. That decision is the
gate: get it wrong and ``agent-orm-check`` runs the identical tests on SQLite
and reports green while catching nothing — the skip-as-pass hole TRK-022
closed for ``migration-parity``, re-opened in a new place.

These are pure env/branching assertions with no database of any kind, so they
run identically in both modes and in the DB-free ``test-suite`` job.
"""

from __future__ import annotations

import pytest

from tests.support import pg


def test_unset_means_sqlite(monkeypatch):
    monkeypatch.delenv(pg.PG_DSN_ENV, raising=False)
    assert pg.pg_dsn() is None
    assert pg.pg_enabled() is False


def test_blank_and_whitespace_mean_sqlite(monkeypatch):
    for value in ("", "   "):
        monkeypatch.setenv(pg.PG_DSN_ENV, value)
        assert pg.pg_enabled() is False, f"{value!r} must not count as a DSN"


def test_a_postgres_dsn_enables_the_gate(monkeypatch):
    monkeypatch.setenv(pg.PG_DSN_ENV, "postgresql://u:p@host:5432/db")
    assert pg.pg_enabled() is True
    assert pg.pg_dsn() == "postgresql://u:p@host:5432/db"


def test_a_non_postgres_dsn_raises_instead_of_falling_back(monkeypatch):
    """The load-bearing one.

    A typo'd or wrong-scheme DSN must ABORT, not quietly degrade to SQLite.
    Silent fallback is what would let the gate go green while testing nothing.
    """
    monkeypatch.setenv(pg.PG_DSN_ENV, "sqlite:///:memory:")
    with pytest.raises(RuntimeError, match="must be a postgresql:// DSN"):
        pg.pg_dsn()
    with pytest.raises(RuntimeError):
        pg.pg_enabled()
