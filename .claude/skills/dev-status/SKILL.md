---
name: dev-status
description: >
  Run the 10-check developer tooling health audit. Validates agent registry, test coverage,
  schedule collisions, callbacks wiring, migration state, k8s manifests, env parity, and
  CLAUDE.md staleness — all without Docker or a live database. Use before merging or when
  the tooling ecosystem feels out of sync.
disable-model-invocation: false
---

# /dev-status

Runs `.claude/scripts/dev_status.py` — a 10-check audit that validates the full developer
tooling ecosystem in ~30 seconds.

## Quick Run

```bash
.venv/bin/python .claude/scripts/dev_status.py
```

Exit codes: **0** = all green, **1** = warnings only, **2** = at least one error.

## Auto-fix Mode

Rewrites stale test/agent counts in `CLAUDE.md` automatically:

```bash
.venv/bin/python .claude/scripts/dev_status.py --fix
```

---

## The 10 Checks

| # | Check | What it catches |
|---|---|---|
| 1 | **Agent Registry Consistency** | Domain in AGENT_REGISTRY with no schedule; orphan SKIP_HOOK entry |
| 2 | **Test File Coverage** | Agent merged without a matching `tests/agents/test_*.py` |
| 3 | **Schedule Collision Detection** | Two agents sharing an exact (hour, minute) slot → Redis lock contention |
| 4 | **Callbacks Wiring** | Agent that doesn't inherit BaseAgent (bypasses safety callback chain) |
| 5 | **Migration State** | Model drift (`alembic check`) or merge conflict (`alembic heads`) |
| 6 | **Alembic Lock Timeout** | Missing `lock_timeout` in `alembic/env.py` → migration hangs indefinitely |
| 7 | **k8s Manifest Validation** | Scheduler replicas > 1; liveness on `/health` instead of `/healthz`; `:latest` tag |
| 8 | **Environment Parity** | New `config.py` Settings field not documented in `.env.example` |
| 9 | **CLAUDE.md Staleness** | Hardcoded test/agent counts in CLAUDE.md drifted from reality |
| 10 | **SQL Validator** | raw dashboard/chat query references a column that doesn't exist in the ORM |

---

## Remediation by Check

| Check fails | Remediation skill |
|---|---|
| Check 1 — unscheduled domain | `/agent-register` or manually add to `_DEFAULT_SCHEDULES` |
| Check 2 — missing test file | `/agent-scaffold` to generate both files |
| Check 3 — schedule collision | Stagger by 30 min in `scheduler.py`; collisions are INFO-level unless Redis lock is thin |
| Check 5 — migration drift | `/migration-create <message>` to generate + review a new migration |
| Check 7 — k8s issues | Check `k8s/agent-core.yaml` (probes) and `k8s/scheduler.yaml` (replicas) |
| Check 8 — env parity | `tests/test_env_example_parity.py` will show the missing key; add to `.env.example` |
| Check 9 — stale counts | Run with `--fix` flag; review the diff before committing |
| Check 10 — SQL column drift | `/validate-sql` for details; fix the query or add the column via `/migration-create` |

---

## Known Pre-existing WARNs

These WARNs exist by design and are not bugs:

- **Check 3**: Multiple agents share `02:00`, `05:00`, and `*/6:00` UTC slots — intentional
  groupings; Redis dedup handles it.
- **Check 6**: `alembic/env.py` has no `lock_timeout` — pre-existing gap; tracked but not
  blocking.

---

## When to Run

- Before merging any feature branch
- After adding a new agent (`/agent-register` does this automatically)
- After any CLAUDE.md or scheduler.py edit
- As part of `/deploy-check` (step 0)
