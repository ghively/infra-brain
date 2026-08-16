---
name: deploy-check
description: >
  Full pre-deployment validation checklist for infra-brain. Runs tests, migration checks,
  env parity, docker lint, k8s manifest review, and security hardening verification.
  Run before any production deployment or merging a release branch.
disable-model-invocation: false
---

# /deploy-check

Runs the complete pre-deployment validation sequence. Fail fast — fix each error before
continuing to the next step.

> **CI vs local:** The MR pipeline enforces only two blocking gates — `migration-parity`
> (Step 4 here) and `sql-execution-check` (Step 3 here). Every other step below (full test
> suite, lint, env parity, Docker/k8s/security checks) is a local pre-deploy check that CI
> no longer runs as a gate, so running this skill before opening an MR is how those stay
> honest. The master-only stages that run after merge are build, deploy, backup,
> runner-disk-prune, rollback, and verify-deployed-commit.

## Step 1: Full Test Suite

```bash
.venv/bin/python -m pytest tests/ -q --tb=short
```

All ~4500 tests must pass (exact count drifts as the suite grows -- check `pytest --collect-only -q` for the current number); ~21 skipped without a real Postgres/optional-dependency configured is normal.

## Step 2: Lint

```bash
.venv/bin/python -m ruff check src/ tests/
.venv/bin/python -m ruff format --check src/ tests/
```

## Step 3: SQL Column Validation

```bash
.venv/bin/python -m pytest tests/test_dashboard_sql_columns.py -v
```

Catches silent column drift in raw dashboard/chat SQL (8 such bugs were found June 2026).

## Step 4: Migration Sync Check

```bash
# No uncommitted model changes
.venv/bin/python -m alembic check

# Exactly one head (no merge conflicts)
.venv/bin/python -m alembic heads | python -c "
import sys
lines = [l.strip() for l in sys.stdin if l.strip()]
if len(lines) != 1:
    print(f'ERROR: expected 1 head, found {len(lines)}: {lines}')
    sys.exit(1)
print('OK: single migration head')
"
```

**Alembic round-trip gate** (run when migrations changed):

```bash
# Requires TEST_DATABASE_URL — verifies upgrade→downgrade→upgrade succeeds cleanly
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/infra_brain_test \
  .venv/bin/python -c "
import subprocess, sys
def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print('FAIL:', cmd)
        print(r.stderr)
        sys.exit(1)
    print('OK:', cmd)

run('.venv/bin/python -m alembic upgrade head')
run('.venv/bin/python -m alembic downgrade base')
run('.venv/bin/python -m alembic upgrade head')
print('Round-trip OK')
"
```

## Step 5: Environment Variable Parity

```bash
.venv/bin/python -m pytest tests/test_env_example_parity.py -v
```

Confirms `.env.example` documents every variable that `config.py` reads.

## Step 6: Docker Image Validation

Run these checks in order before pushing to registry:

```bash
# 1. Structure test — imports resolve, no .env files baked in, non-root user
# Requires: pip install container-structure-test  (or docker run on Linux)
container-structure-test test --image infra-brain:${IMAGE_TAG} \
  --config docker/structure-test.yaml

# 2. Trivy CVE scan — two-pass: report (no block) then hard gate on CRITICAL
trivy image --format table --exit-code 0 infra-brain:${IMAGE_TAG}
trivy image --exit-code 1 --severity CRITICAL infra-brain:${IMAGE_TAG}
# Note: Grype exit code is 2 (not 1) — if using Grype, adjust accordingly

# 3. Secret leak scan — catches keys/tokens accidentally baked into layers
trivy image --scanners secret --exit-code 1 infra-brain:${IMAGE_TAG}

# 4. Layer efficiency (optional but recommended before major releases)
# Requires: brew install dive  or  go install github.com/wagoodman/dive@latest
dive infra-brain:${IMAGE_TAG} --ci --lowestEfficiency=0.95 --highestWastedBytes=20MB

# 5. Smoke test — build and verify /healthz responds
docker run -d --name smoke-test -p 18000:8000 \
  -e DATABASE_URL=sqlite:///tmp/test.db \
  infra-brain:${IMAGE_TAG}
for i in $(seq 1 30); do
  if curl -sf http://localhost:18000/healthz > /dev/null; then
    echo "OK: /healthz responded"
    break
  fi
  sleep 2
done
docker rm -f smoke-test 2>/dev/null
```

**Image tag checks:**

```bash
# Image tag must NOT be 'latest' in docker-compose for production builds
grep "image:" docker/docker-compose.yml | grep ":latest" && echo "ERROR: :latest tag found" || echo "OK"

# LLM_MODEL must be present (not ANTHROPIC_MODEL — that was a prior bug)
grep "LLM_MODEL" docker/docker-compose.yml || echo "WARNING: LLM_MODEL not set"

# BWS_ACCESS_TOKEN must NOT be hard-coded
grep -r "BWS_ACCESS_TOKEN" docker/ | grep -v "secretKeyRef\|valueFrom\|example\|#" && echo "ERROR: hard-coded BWS token" || echo "OK"
```

## Step 7: Kubernetes Manifest Checks

```bash
# Secret name consistency — all must reference 'bws-access-token', not 'bws-token'
grep -r "secretName\|name:" k8s/ | grep -i "bws"
# Should show 'bws-access-token' everywhere

# Image tags — must be pinned for production (not 'latest')
grep "image:" k8s/*.yaml
# Should show ${CI_REGISTRY_IMAGE}:${IMAGE_TAG} patterns, not :latest

# Probe separation — liveness MUST be /healthz (zero I/O), readiness MUST be /health
grep -A3 "livenessProbe" k8s/agent-core.yaml | grep "path:"
# Must show /healthz — if /health, the liveness probe checks Postgres/Redis and will
# restart pods on DB blips instead of just removing them from load balancer rotation

# Health probe timeoutSeconds — must be set (default 1s causes false restarts under GC)
grep "timeoutSeconds" k8s/agent-core.yaml

# Scheduler: single replica (APScheduler has no inter-process duplicate-execution guard)
grep "replicas:" k8s/scheduler.yaml
# Must be 1 — multiple replicas = every job runs N times, once per pod

# Scheduler: terminationGracePeriodSeconds must be > longest job runtime
grep "terminationGracePeriodSeconds" k8s/scheduler.yaml

# PDB check — verify no PDB sets minAvailable ≥ replicas (silently blocks node drain)
kubectl get pdb -n infra-brain -o yaml 2>/dev/null | grep -E "minAvailable|maxUnavailable"
# If minAvailable == replicas, node drain is permanently blocked (not the rollout)
```

**Memory headroom check** — limits should be ≤2× requests (50% headroom prevents OOMKilled
workers that leave the pod in `Running` state):

```bash
grep -A6 "resources:" k8s/agent-core.yaml
# memory requests: 512Mi → limit should be ≤ 1Gi for 2x ratio
# Current: 512Mi→2Gi (4x) — acceptable but monitor; reduce if OOMKilled events occur
```

## Step 8: Security Hardening Checklist

Verify these are set in `k8s/configmap.yaml` or environment:

| Setting | Required Value | Risk if Wrong |
|---|---|---|
| `WEBHOOK_AUTH_REQUIRED` | `1` | Unauthenticated webhook triggers |
| `DLP_FAIL_CLOSED` | `1` | PII leaked in LLM outputs |
| `SCAN_READONLY_ENFORCE` | `1` | Infrastructure mutation allowed |
| `INTEGRATION_APPROVAL_REQUIRED` | `1` | Auto-approval of integration changes |
| `UI_COOKIE_SECRET` | non-default strong secret | Session hijacking |
| `SCRIPTS_ENABLED` | `false` (unless isolated) | Arbitrary code execution |

```bash
# Check the configmap
grep -E "WEBHOOK_AUTH|DLP_FAIL|SCAN_READONLY|INTEGRATION_APPROVAL" k8s/configmap.yaml
```

## Step 9: Health Endpoint Verification (post-deploy)

```bash
# Liveness (zero I/O — should always be fast)
curl -sf http://<host>:8000/healthz
# Returns: {"status": "ok"}

# Readiness (full dep check — Postgres + Redis)
curl -sf http://<host>:8000/health | python -m json.tool
# Returns: {"status":"ok","postgres":"ok","redis":"ok"}

# Trigger a test sweep to confirm the agent pipeline works end-to-end
curl -X POST http://<host>:8000/sweeps/linux \
  -H "X-Infra-Token: <webhook_generic_secret>"
```

## Step 10: Bitwarden Secrets Verification

Five-tier validation — checks presence, non-empty, format, length, and connectivity:

```bash
.venv/bin/python -c "
import re, sys
from infra_brain.config import get_settings

s = get_settings()

checks = [
    ('database_url', r'^postgresql://', 20),
    ('redis_url', r'^redis://', 10),
    ('anthropic_api_key', r'^sk-ant-', 20),
]

failed = False
for attr, pattern, min_len in checks:
    val = getattr(s, attr, None)
    if not val:
        print(f'MISSING: {attr}')
        failed = True
    elif len(str(val)) < min_len:
        print(f'TOO SHORT: {attr} ({len(str(val))} chars, expected >{min_len})')
        failed = True
    elif not re.match(pattern, str(val)):
        print(f'WRONG FORMAT: {attr} (expected pattern {pattern!r})')
        failed = True
    else:
        print(f'OK [{len(str(val))} chars]: {attr}')

sys.exit(1 if failed else 0)
"
```

## Checklist Summary

- [ ] All ~4500 tests green
- [ ] ruff check passes
- [ ] SQL column validator passes
- [ ] Single migration head, no drift
- [ ] Alembic round-trip (upgrade→downgrade→upgrade) clean
- [ ] .env.example parity
- [ ] Docker image: no CRITICAL CVEs, no secrets in layers, structure test passes
- [ ] Docker image tag pinned (not :latest)
- [ ] k8s liveness → `/healthz`, readiness → `/health` (separated)
- [ ] k8s scheduler replicas = 1 (APScheduler single-instance)
- [ ] k8s terminationGracePeriodSeconds set on scheduler pod
- [ ] Security hardening flags set in configmap
- [ ] `/healthz` and `/health` both return ok after deploy
- [ ] Test sweep creates a `collection_runs` row

## Known Gotchas

**APScheduler duplicate execution** — APScheduler 3.x has no inter-process job lock. A shared
PostgreSQL job store does NOT prevent two pods running the same job. `scheduler.yaml` must
always run `replicas: 1`. Never scale it. If HA failover is needed, use Kubernetes leader
election (`python-kubernetes` ConfigMapLock) instead.

**APScheduler shutdown** — `scheduler.shutdown(wait=True, timeout=30)` raises `TypeError` in
all 3.x releases — the `timeout` parameter was never implemented. Use `shutdown(wait=True)`
only. Set `terminationGracePeriodSeconds` > longest scheduled job runtime.

**Init container silence** — The Alembic migration init container exits 0 when Postgres is
unreachable (nothing to migrate = no error). Always check:
```bash
kubectl logs -n infra-brain <pod> -c migration  # or bws-bootstrap
```

**Secret envFrom rotation** — `envFrom` values are frozen at container start. Rotating a
Bitwarden secret and letting the operator sync to K8s does NOT update running pods. The
`bws-bootstrap` init container pattern used here reads secrets fresh each pod start, which
is correct. Do not change it to `envFrom` for BWS-sourced secrets.
