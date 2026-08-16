---
name: ci-debug
description: >-
  Diagnose a failing GitLab CI pipeline for infra-brain (project 42).
  Fetches job traces via the GitLab API, classifies the failure type from
  a known taxonomy, and outputs a copy-paste fix. Works for any pipeline
  by ID or defaults to the most recent failed run.
disable-model-invocation: false
---

# /ci-debug [pipeline_id]

Diagnose a failing infra-brain CI pipeline. Fetches job logs, classifies the failure
against a known taxonomy, and provides a copy-paste fix.

## Arguments

- `[pipeline_id]` — GitLab pipeline ID to debug. Omit to auto-select the most recent
  failed or canceled pipeline for project 42.

---

## Step 1: Find the Failing Pipeline

If no `pipeline_id` given, fetch the most recent failed pipeline:

```bash
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "$GITLAB_URL/api/v4/projects/42/pipelines?status=failed&per_page=5"
```

Also check for `canceled` status — pipelines canceled mid-run often contain the real error
in the last completed job.

---

## Step 2: Identify Failing Jobs

```bash
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "$GITLAB_URL/api/v4/projects/42/pipelines/{id}/jobs" \
  | python3 -c "
import json, sys
jobs = json.load(sys.stdin)
failed = [j for j in jobs if j['status'] in ('failed', 'canceled')]
for j in failed:
    print(f\"{j['id']:8d}  {j['stage']:12s}  {j['name']}\")
"
```

---

## Step 3: Fetch Job Trace

```bash
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "$GITLAB_URL/api/v4/projects/42/jobs/{job_id}/trace" \
  | tail -200
```

Errors almost always appear in the **last 50 lines**. Read bottom-up.

---

## Step 4: Classify Failure

Match the trace against this taxonomy and jump to the fix section.

**Current pipeline jobs.** MR pipelines run only two blocking gates: `migration-parity`
and `sql-execution-check`. Master-only stages (run after merge): `build`, `deploy`,
`backup`, `runner-disk-prune`, `rollback`, `verify-deployed-commit`. There is no longer a
lint, coverage, env-parity, registry-sync, agent-test-coverage, k8s-yaml-lint,
alembic-chain-check, design-sync-check, contract-check, no-external-origins, or
dashboard-app-lint job — do not expect them in a trace.

| Pattern in trace / failing job | Class | Jump to |
|---|---|---|
| `could not find expected ':'` | `YAML_INVALID` | Step 5-A |
| `while scanning a simple key` | `YAML_INVALID` | Step 5-A |
| `No runners are available` | `RUNNER_OFFLINE` | Step 5-B |
| `Runner ... is offline` | `RUNNER_OFFLINE` | Step 5-B |
| `sql-execution-check` job fails; `not a column of` / `FAILED tests/test_dashboard_sql_columns.py` | `SQL_COLUMN_DRIFT` | Step 5-C |
| `migration-parity` job fails; `alembic check` / `Target database is not up to date` | `MIGRATION_PARITY` | Step 5-D |
| `ERROR [docker]` or `build failed` (build stage) | `BUILD_FAILURE` | Step 5-E |
| `health check failed` or `curl: (7)` (deploy stage) | `DEPLOY_FAILURE` | Step 5-F |
| `trivy` + `CRITICAL` (build stage) | `CVE_GATE` | Step 5-G |

---

## Step 5-A: YAML_INVALID Fix

Multi-line Python in CI YAML **must use heredoc**, not `python -c "..."`.
Code at column 0 breaks the YAML parser.

**Wrong:**
```yaml
script:
  - python3 -c "
import sys
sys.exit(1)
"
```

**Correct:**
```yaml
script:
  - |
    python3 - << 'PYEOF'
    import sys
    sys.exit(1)
    PYEOF
```

Verify the fix locally before pushing:
```bash
python3 -c "
import yaml
yaml.safe_load(open('.gitlab-ci.yml').read())
print('YAML OK')
"
```

---

## Step 5-B: RUNNER_OFFLINE Fix

Check which runners are assigned to project 42:
```bash
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "$GITLAB_URL/api/v4/projects/42/runners"
```

If empty or all offline, assign runner 1 (the socket-mounted Docker runner):
```bash
curl -s -X POST -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "$GITLAB_URL/api/v4/projects/42/runners" \
  -d "runner_id=1"
```

Check runner status:
```bash
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "$GITLAB_URL/api/v4/runners/1"
```

---

## Step 5-C: SQL_COLUMN_DRIFT Fix

The `sql-execution-check` MR gate runs `tests/test_dashboard_sql_columns.py` — it fails
when raw SQL in `chat/tools.py` (or inline SQL in `api/routers/*`) references a column that
does not exist in the SQLAlchemy ORM. Reproduce and fix locally:

```bash
.venv/bin/python -m pytest tests/test_dashboard_sql_columns.py -v
```

Then invoke `/validate-sql` for a column-by-column breakdown. The ORM in
`src/infra_brain/db/models/` is the source of truth — fix the query (usually a
`real_column AS alias`), never guess a column name.

---

## Step 5-D: MIGRATION_PARITY Fix

The `migration-parity` MR gate runs `alembic check` — it fails when the ORM models in
`src/infra_brain/db/models/` have changed without a matching migration in
`alembic/versions/`. Reproduce locally:

```bash
.venv/bin/python -m alembic check
```

If it reports drift, generate the missing migration via `/migration-create <message>`
(never hand-write it), then re-run `alembic check` to confirm it is clean.

---

## Step 5-E: BUILD_FAILURE Fix

Test the Docker build locally:
```bash
docker build -t infra-brain:local .
```

Common causes:
- `COPY src/ /app/src/` fails if `src/` layout changed
- `RUN pip install -e ".[extras]"` fails if pyproject.toml is malformed
- Multi-stage build copying from wrong stage name

---

## Step 5-F: DEPLOY_FAILURE Fix

The deploy job runs a health check against `:8000/healthz` after `docker compose up`.
If the check fails:

```bash
# On the host, check what's running
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs app --tail=50
docker compose -f docker/docker-compose.yml logs migrate --tail=20
```

Common causes:
- Migration failed (check `migrate` service logs)
- `.env` missing a required variable (check `config.py` vs `.env.example`)
- Port conflict — another service on 8001

---

## Step 5-G: CVE_GATE Fix

CRITICAL CVEs hard-gate the build. Check the Trivy output:
```bash
# Trivy output is in the build job trace — grep for CRITICAL
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "$GITLAB_URL/api/v4/projects/42/jobs/{job_id}/trace" \
  | grep -A3 "CRITICAL"
```

Fixes:
- Update the affected base image in `Dockerfile` (e.g., `python:3.12-slim` → latest patch)
- Update the affected Python dependency in `pyproject.toml`
- If the CVE is in a transitive dep: pin the fixed version in `pyproject.toml` under `[project.dependencies]`
