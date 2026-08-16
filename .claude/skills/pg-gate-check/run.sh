#!/usr/bin/env bash
# pg-gate-check: replicate the four MR-blocking CI gates (lock-freshness +
# migration-parity + sql-execution-check + agent-orm-check) locally against the
# SAME images/tools CI uses.
# Run from the repo/worktree root of the branch you are about to push.
#
#   bash .claude/skills/pg-gate-check/run.sh
#
# Gate 4 (agent-orm-check) is the slow one — it re-runs every module listed in
# tests/support/agent_orm_check_paths.txt against real PostgreSQL (~5 min).
# Skip it for a quick schema-only check with:
#
#   PG_GATE_SKIP_ORM=1 bash .claude/skills/pg-gate-check/run.sh
#
# Exit 0 = every gate replicated green. See .claude/skills/pg-gate-check/SKILL.md.
set -euo pipefail

REPO_ROOT="$(pwd)"
[ -f "$REPO_ROOT/.gitlab-ci.yml" ] || { echo "ERROR: run from the repo root (no .gitlab-ci.yml here)"; exit 2; }

# Python: prefer the main-checkout venv (worktrees share it); PYTHONPATH=src below
# guarantees we test THIS tree's code, not the venv's editable install target.
PY="${PG_GATE_PYTHON:-}"
if [ -z "$PY" ]; then
  # A worktree has no .venv of its own. Derive the MAIN checkout from git's
  # common dir (…/<main>/.git) rather than assuming a hardcoded home path —
  # the old $HOME/projects/infra-brain guess silently missed on any machine
  # that checked the repo out somewhere else, and gate 4 is meant to be run
  # from the branch worktree.
  MAIN_CHECKOUT="$(dirname "$(git rev-parse --git-common-dir 2>/dev/null || echo /nonexistent)")"
  for cand in "$REPO_ROOT/.venv/bin/python" "$MAIN_CHECKOUT/.venv/bin/python" \
              "$HOME/projects/infra-brain/.venv/bin/python"; do
    [ -x "$cand" ] && PY="$cand" && break
  done
fi
[ -n "$PY" ] || { echo "ERROR: no venv python found (set PG_GATE_PYTHON)"; exit 2; }

# Read the postgres service image from .gitlab-ci.yml — never hardcode it.
PG_IMAGE=$(grep -A1 "services:" "$REPO_ROOT/.gitlab-ci.yml" | grep -o 'pgvector/[^" ]*\|postgres:[^" ]*' | head -1)
[ -n "$PG_IMAGE" ] || { echo "ERROR: could not parse postgres service image from .gitlab-ci.yml"; exit 2; }

PORT="${PG_GATE_PORT:-55432}"
NAME="pg-gate-check-$$"
DSN="postgresql://infra:infra@localhost:${PORT}/infra_brain_ci"

cleanup() { docker stop "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== pg-gate-check: image=$PG_IMAGE port=$PORT"
docker run -d --rm --name "$NAME" \
  -e POSTGRES_DB=infra_brain_ci -e POSTGRES_USER=infra -e POSTGRES_PASSWORD=infra \
  -p "${PORT}:5432" "$PG_IMAGE" >/dev/null

echo "-- waiting for postgres"
for _ in $(seq 1 30); do
  docker exec "$NAME" pg_isready -U infra >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$NAME" pg_isready -U infra >/dev/null 2>&1 || { echo "ERROR: postgres never became ready"; exit 2; }
sleep 1

# Mirrors the migration-parity job's before_script reset. NOTE: DROP SCHEMA
# CASCADE also drops extensions — that is intentional; CI's reused service DB
# means the gate must survive a from-nothing schema, and so must we.
reset_schema() {
  "$PY" - "$DSN" <<'EOF'
import sys, psycopg2
conn = psycopg2.connect(sys.argv[1]); conn.autocommit = True
cur = conn.cursor()
cur.execute("DROP SCHEMA public CASCADE")
cur.execute("CREATE SCHEMA public")
cur.execute("GRANT ALL ON SCHEMA public TO PUBLIC")
print("schema reset")
EOF
}

echo "== gate 1/4: lock-freshness"
uv lock --check

echo "== gate 2/4: migration-parity"
reset_schema
POSTGRES_URL="$DSN" MIGRATION_PARITY_REQUIRE_DB=1 PYTHONPATH=src \
  "$PY" -m pytest tests/test_migration_parity.py tests/test_migration.py -q

echo "== gate 3/4: sql-execution-check"
reset_schema
TEST_DATABASE_URL="$DSN" PYTHONPATH=src \
  "$PY" -m pytest tests/test_dashboard_sql_columns.py -q

# TRK-356. Gates 2 and 3 prove the migration chain and the raw dashboard SQL on
# real PostgreSQL; neither ever executes an agent-layer ORM query. PG_GATE_DSN
# switches tests/support/pg.py::make_engine from in-memory SQLite to this
# container, so the listed modules run their real ORM constructs against
# PostgreSQL. Note PG_GATE_DSN, not POSTGRES_URL: the latter is what application
# code reads and conftest pins it to SQLite deliberately.
#
# The selection is read from tests/support/agent_orm_check_paths.txt — the SAME
# file .gitlab-ci.yml's agent-orm-check job reads, so this local replica cannot
# drift from the gate it is replicating.
ORM_PATHS_FILE="$REPO_ROOT/tests/support/agent_orm_check_paths.txt"
if [ "${PG_GATE_SKIP_ORM:-}" = "1" ]; then
  echo "== gate 4/4: agent-orm-check  SKIPPED (PG_GATE_SKIP_ORM=1)"
else
  [ -f "$ORM_PATHS_FILE" ] || { echo "ERROR: missing $ORM_PATHS_FILE"; exit 2; }
  echo "== gate 4/4: agent-orm-check  (slowest gate — ~5 min)"
  reset_schema
  # shellcheck disable=SC2046  # deliberate word-splitting: one path per arg
  PG_GATE_DSN="$DSN" PYTHONPATH=src \
    "$PY" -m pytest $(grep -vE '^[[:space:]]*(#|$)' "$ORM_PATHS_FILE" | tr '\n' ' ') -q
fi

echo "== pg-gate-check: ALL GATES GREEN"
