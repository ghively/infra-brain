"""Real-PostgreSQL opt-in for ORM-layer tests (TRK-356).

Why this exists
---------------
The three hard MR gates (``lock-freshness``, ``migration-parity``,
``sql-execution-check``) all touch a real PostgreSQL — but between them they
only exercise the *migration chain* and the *raw dashboard/chat SQL*.
Agent-layer ORM constructs (``agents/``, ``etl/``) only ever met in-memory
SQLite in the suite, and SQLite silently tolerates SQL that PostgreSQL
rejects. That gap has now shipped a production failure: TRK-350's
``IN (SELECT (SELECT ...))`` self-heal filter passed 5,215 green tests and
raised ``CardinalityViolation`` on the FIRST live drift run (MR !36 hotfix).

This module is the opt-in switch that closes it. Test modules build their
engine through :func:`make_engine` instead of calling ``create_engine`` with a
hardcoded ``"sqlite://"``:

* **Default (no env var):** behaviour is bit-for-bit what it was — an
  in-memory SQLite engine on a ``StaticPool`` with the full ORM schema
  created. Local runs and the DB-free ``test-suite`` CI job are unaffected.
* **``PG_GATE_DSN`` set:** the same call returns a shared engine bound to a
  real PostgreSQL, wiped clean before each handout. This is what the
  ``agent-orm-check`` CI gate (and ``/pg-gate-check`` gate 4) sets.

Design notes
------------
*One shared engine, not one per call.* ``Base.metadata.create_all`` against
PostgreSQL costs ~5s for the 146-table schema — per test that would be
unusable. The schema is built once per pytest session and each handout
instead **resets the data**: a single round-trip ``EXISTS`` probe finds the
non-empty tables (~35ms across all 146) and only those are emptied.

*DELETE, not TRUNCATE.* Blind-truncating all 146 tables costs ~2.7s a call.
Even truncating just the dirty ones is dominated by ``DataFileImmediateSync``
— TRUNCATE allocates a new relfilenode and fsyncs it, so its cost is per
*table*, not per *row*, and these tables hold single-digit row counts.
``DELETE`` on a handful of tiny tables is an order of magnitude cheaper and
needs no server-side ``fsync=off`` tuning to stay usable. No table in the
schema has an autoincrement integer primary key, so nothing depends on
``RESTART IDENTITY``. Deletion runs children-first (reverse
``sorted_tables``); a residual FK cycle falls back to ``TRUNCATE ... CASCADE``.

*StaticPool on PostgreSQL too.* In-memory SQLite on a ``StaticPool`` hands
every checkout the *same* DBAPI connection, so a test's uncommitted writes are
visible to a separate ``Session`` opened by the code under test. Using
``StaticPool`` for PostgreSQL preserves that visibility semantic, so switching
dialects tests the SQL — not an unrelated transaction-isolation difference.
``dispose()`` before each reset drops any connection a test left mid-
transaction (which would otherwise deadlock the reset against its own locks).
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.pool import StaticPool

from infra_brain.db.models import Base

#: Env var the ``agent-orm-check`` CI gate sets to point the ORM fixtures at a
#: real PostgreSQL. Unset (the default) keeps every helper on in-memory SQLite.
PG_DSN_ENV = "PG_GATE_DSN"

_ENGINE: Engine | None = None
_RESET_PROBE: str | None = None
_DELETE_ORDER: list[str] | None = None


def pg_dsn() -> str | None:
    """The PostgreSQL DSN the gate asked for, or ``None`` when not in gate mode."""
    dsn = os.environ.get(PG_DSN_ENV, "").strip()
    if not dsn:
        return None
    if not dsn.startswith("postgres"):
        # Fail loud rather than silently falling back to SQLite: a typo'd DSN
        # must never let the gate go green while testing nothing (the exact
        # skip-as-pass hole TRK-022 closed for migration-parity).
        raise RuntimeError(
            f"{PG_DSN_ENV} must be a postgresql:// DSN (got {dsn!r}). "
            "Unset it to run the suite on SQLite."
        )
    return dsn


def pg_enabled() -> bool:
    """True when the suite has been pointed at a real PostgreSQL."""
    return pg_dsn() is not None


def _worker_dsn(dsn: str) -> str:
    """Give each pytest-xdist worker its own database on the same server.

    The reset strategy assumes one process owns the database — under ``-n`` the
    workers would truncate each other's rows mid-test. Each worker therefore
    gets ``<db>_gw0``, ``<db>_gw1``, … created on demand. Without xdist (or on
    the controller) the DSN is returned untouched.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker or worker == "master":
        return dsn

    url = make_url(dsn)
    target = f"{url.database}_{worker}"
    admin = create_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target}
            ).scalar()
            if not exists:
                try:
                    conn.execute(text(f'CREATE DATABASE "{target}"'))
                except ProgrammingError:
                    # Another worker won the race; its database is ours too.
                    pass
    finally:
        admin.dispose()
    return url.set(database=target).render_as_string(hide_password=False)


def _build_pg_engine(dsn: str) -> Engine:
    eng = create_engine(dsn, poolclass=StaticPool, future=True)
    with eng.connect() as conn:
        # Mirrors the CI service image (pgvector/pgvector:pg15). Harmless when
        # the extension is already present or unused by the models.
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    # Idempotent: the gate resets the schema in before_script, but a local
    # re-run against a warm container must not explode on existing tables.
    Base.metadata.create_all(eng)
    return eng


def _reset_probe() -> str:
    """One statement that names every table currently holding at least one row."""
    global _RESET_PROBE
    if _RESET_PROBE is None:
        _RESET_PROBE = " UNION ALL ".join(
            f"SELECT '{name}' AS t WHERE EXISTS (SELECT 1 FROM \"{name}\")"
            for name in Base.metadata.tables
        )
    return _RESET_PROBE


def _delete_order() -> list[str]:
    """Table names children-first, so plain DELETEs don't trip foreign keys."""
    global _DELETE_ORDER
    if _DELETE_ORDER is None:
        _DELETE_ORDER = [t.name for t in reversed(Base.metadata.sorted_tables)]
    return _DELETE_ORDER


def _reset(eng: Engine) -> None:
    # Drop whatever connection the previous test left behind — an abandoned
    # open transaction would block the reset indefinitely instead of failing.
    eng.dispose()
    with eng.connect() as conn:
        dirty = {row[0] for row in conn.execute(text(_reset_probe()))}
        if not dirty:
            return
        try:
            for name in _delete_order():
                if name in dirty:
                    conn.execute(text(f'DELETE FROM "{name}"'))
            conn.commit()
        except DBAPIError:
            # A foreign-key cycle the topological order can't linearise. Rare
            # and slow, but always correct.
            conn.rollback()
            names = ", ".join(f'"{name}"' for name in dirty)
            conn.execute(text(f"TRUNCATE TABLE {names} CASCADE"))
            conn.commit()


def make_engine(**sqlite_kwargs) -> Engine:
    """Return a schema-ready, empty engine for an ORM test.

    In-memory SQLite by default; a wiped-clean real PostgreSQL when
    ``PG_GATE_DSN`` is set. ``sqlite_kwargs`` are forwarded to
    ``create_engine`` in SQLite mode only (callers rarely need them — the
    ``StaticPool`` / ``check_same_thread`` pair is already the default).

    Each call yields a database with no rows, so a test that builds two
    "separate" engines gets two handles to the *same* PostgreSQL data in gate
    mode. That is fine for the single-engine-per-test shape every converted
    module uses; convert a multi-engine test only after checking it.
    """
    dsn = pg_dsn()
    if dsn is None:
        kwargs = {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
            **sqlite_kwargs,
        }
        eng = create_engine("sqlite://", **kwargs)
        Base.metadata.create_all(eng)
        return eng

    global _ENGINE
    if _ENGINE is None:
        _ENGINE = _build_pg_engine(_worker_dsn(dsn))
    _reset(_ENGINE)
    return _ENGINE
