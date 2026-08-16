"""Unit tests for scripts/deploy/downgrade_guard.sh (TRK-005).

The guard is the PURE decision core of the rollback migration-downgrade feature.
It does no I/O, so we can exercise every branch of the opt-in ladder by invoking
the POSIX-sh script directly and asserting the decision token it prints. This
locks in the SAFE-by-default posture: without explicit opt-in the guard always
declines to downgrade, and destructive paths require a second explicit opt-in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / "scripts" / "deploy" / "downgrade_guard.sh"


def decide(current, target, allow_downgrade, destructive, allow_destructive) -> str:
    """Run the guard CLI and return its single decision token."""
    result = subprocess.run(
        [
            "sh",
            str(GUARD),
            current,
            target,
            allow_downgrade,
            destructive,
            allow_destructive,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_guard_script_exists():
    assert GUARD.is_file(), f"guard script missing at {GUARD}"


@pytest.mark.parametrize(
    "current,target,allow,destructive,allow_destr,expected",
    [
        # Opt-in gate: not armed -> never downgrade, regardless of everything else.
        ("aaa", "bbb", "0", "0", "0", "SKIP_OPTOUT"),
        ("aaa", "bbb", "0", "1", "1", "SKIP_OPTOUT"),
        # No captured target revision -> nothing to downgrade to.
        ("aaa", "", "1", "0", "0", "SKIP_NO_TARGET"),
        # Already at the captured revision -> no-op.
        ("bbb", "bbb", "1", "0", "0", "SKIP_NOOP"),
        # Unknown current revision -> refuse (cannot compute a safe downgrade).
        ("", "bbb", "1", "0", "0", "REFUSE_UNKNOWN_CURRENT"),
        # Armed, non-destructive path -> downgrade allowed.
        ("aaa", "bbb", "1", "0", "0", "DOWNGRADE"),
        # Armed but destructive and NOT accepted -> refuse.
        ("aaa", "bbb", "1", "1", "0", "REFUSE_DESTRUCTIVE"),
        # Armed, destructive, explicitly accepted -> downgrade.
        ("aaa", "bbb", "1", "1", "1", "DOWNGRADE"),
    ],
)
def test_decision_ladder(current, target, allow, destructive, allow_destr, expected):
    assert decide(current, target, allow, destructive, allow_destr) == expected


def test_optout_takes_precedence_over_noop():
    # Even when already at target, an un-armed guard reports the opt-out, never
    # leaking a "safe to proceed" style token.
    assert decide("bbb", "bbb", "0", "0", "0") == "SKIP_OPTOUT"


def scan(sql: str) -> str:
    """Pipe SQL into scan_destructive_sql (sourced) and return its verdict token."""
    result = subprocess.run(
        ["sh", "-c", f". {GUARD}; scan_destructive_sql"],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    "sql,expected",
    [
        # Data-dropping DDL -> destructive ("1"). These MUST force the second
        # explicit opt-in (ALLOW_DESTRUCTIVE_DOWNGRADE) before any auto-downgrade.
        ("DROP TABLE resources;", "1"),
        ("drop   table resources;", "1"),  # case/space-insensitive
        ("ALTER TABLE resources DROP COLUMN resource_id;", "1"),
        ("DROP COLUMN foo;", "1"),
        # Non-destructive downgrade DDL -> "0" (additive/renames don't drop data).
        ("ALTER TABLE resources ADD COLUMN note text;", "0"),
        ("CREATE INDEX ix_foo ON resources (id);", "0"),
        ("UPDATE alembic_version SET version_num='abc';", "0"),
        ("", "0"),  # empty preview is not, by itself, destructive
    ],
)
def test_scan_destructive_sql(sql, expected):
    assert scan(sql) == expected


def test_scan_flags_destructive_within_larger_script():
    # A destructive statement buried in an otherwise-benign multi-statement
    # downgrade must still trip the guard (over-flagging is the safe direction).
    sql = "BEGIN;\nALTER TABLE resources ADD COLUMN tmp text;\nDROP TABLE snapshots;\nCOMMIT;\n"
    assert scan(sql) == "1"
