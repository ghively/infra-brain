"""Decision logic for the migration-parity CI gate (TRK-022).

The migration-parity job runs ``test_migrated_schema_matches_models`` (and the
table-name check in ``test_migration.py``) against an ephemeral PostgreSQL to
catch stamped-but-unapplied schema drift. Both checks historically *skipped*
themselves when no PostgreSQL was reachable — correct for a local dev box, but a
silent hole in CI: a skipped test is a pytest *pass* (exit 0), so if the Postgres
service was down, the DSN was misconfigured, or ``get_settings().postgres_url``
resolved to a non-postgres scheme, the gate went **green while never running** —
one of only two remaining hard MR gates could go dark undetected.

This module makes the reachability decision explicit and shared. When the CI job
declares Postgres mandatory (``MIGRATION_PARITY_REQUIRE_DB`` set truthy), an
unreachable or unusable DB must FAIL RED instead of skip-as-pass. Locally, with
the flag unset, the legitimate dev skip is preserved.

``classify`` is a pure function (no pytest, no I/O) so the fail-not-skip contract
can be unit-tested without a live database.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# Env flag the migration-parity CI job sets to mark PostgreSQL as mandatory.
# When truthy, an unreachable/unusable DB is a hard failure, never a skip.
REQUIRE_DB_ENV = "MIGRATION_PARITY_REQUIRE_DB"

_TRUTHY = {"1", "true", "yes", "on"}

# action constants
RUN = "run"
SKIP = "skip"
FAIL = "fail"


def require_db_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True if the caller (the CI job) declared a live PostgreSQL mandatory."""
    env = os.environ if environ is None else environ
    return env.get(REQUIRE_DB_ENV, "").strip().lower() in _TRUTHY


def classify(url: str, connect_ok: bool, *, require_db: bool) -> tuple[str, str]:
    """Decide whether the parity check should run, skip, or hard-fail.

    Args:
        url: the DSN the test resolved (``get_settings().postgres_url``).
        connect_ok: whether a live connection to ``url`` actually succeeded.
        require_db: whether PostgreSQL was declared mandatory (CI). See
            ``require_db_enabled``.

    Returns:
        ``(action, message)`` where action is ``RUN`` (proceed), ``SKIP``
        (benign local absence of Postgres), or ``FAIL`` (mandatory DB is
        unreachable/unusable — the silent-DB-down hole this gate closes).
    """
    is_pg = url.startswith("postgresql")

    if is_pg and connect_ok:
        return RUN, ""

    if not is_pg:
        reason = (
            "migration parity requires a PostgreSQL DSN, but the resolved "
            f"POSTGRES_URL scheme is {url.split(':', 1)[0]!r}"
        )
    else:
        reason = "PostgreSQL DSN is set but no connection could be established"

    if require_db:
        return FAIL, (
            f"migration-parity gate FAILED: {reason}. This job requires a live "
            f"PostgreSQL ({REQUIRE_DB_ENV} is set); refusing to skip-as-pass so a "
            "down/misconfigured database can never let the gate pass silently. "
            "Fix the Postgres service or the POSTGRES_URL DSN."
        )
    return SKIP, f"{reason} (skipping; {REQUIRE_DB_ENV} not set — local dev)"
