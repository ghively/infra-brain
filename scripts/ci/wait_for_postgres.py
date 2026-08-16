"""Poll a Postgres DSN until it accepts connections or a timeout elapses.

Deduplicates the identical inline heredoc that used to appear in both the
migration-parity and sql-execution-check CI jobs (CICD-4).

Usage: python3 scripts/ci/wait_for_postgres.py <db_url> [timeout_seconds]
"""

from __future__ import annotations

import sys
import time

import sqlalchemy as sa


def wait_for_postgres(url: str, timeout: int = 30) -> bool:
    for i in range(timeout):
        try:
            with sa.create_engine(url).connect():
                print(f"postgres reachable after {i}s")
                return True
        except Exception as exc:  # noqa: BLE001 — CI polling loop, any failure just retries
            print(f"  waiting for postgres ({i}/{timeout}): {exc}")
            time.sleep(1)
    return False


if __name__ == "__main__":
    db_url = sys.argv[1]
    wait_timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    if not wait_for_postgres(db_url, wait_timeout):
        print(f"ERROR: postgres service never became reachable within {wait_timeout}s")
        sys.exit(1)
