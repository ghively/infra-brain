---
name: onboard
description: >-
  infra-brain-specific onboarding guide. Explains what infra-brain is,
  the safety model, the 25-domain agent architecture (see AGENTS.md,
  generated from AgentSpec), and what read-only
  means in practice. Runs /dev-status to verify the environment is healthy.
  Use on first clone or when introducing a new team member to the repo.
disable-model-invocation: false
---

# /onboard

Welcome to infra-brain. This guide explains what the system is, what it isn't,
how it's built, and how to make your first contribution safely.

---

## What infra-brain Is (and Isn't)

infra-brain is a **read-only infrastructure audit and drift-detection platform**.

It collects the current state of infrastructure across 25 domains — see AGENTS.md,
generated from AgentSpec — (cloud, k8s, IaC,
Linux, Windows, network, CI/CD, vSphere, vulnerability, EOL, etc.), stores it in
PostgreSQL, detects drift from baseline, and surfaces findings in a FastAPI-served
Vite+React dashboard (`/dashboard2`).

**It never mutates infrastructure.** There are no Ansible playbooks that write
configuration, no API calls that create or delete resources, no scripts that push changes.
Every interaction is read-only by design — and that design is enforced at the code level.

---

## Safety Model

Every LLM call in this codebase routes through the **safety callback chain**:

```
ReadOnlyToolValidator → DLPCallbackHandler → AuditCallbackHandler → ObservationCallbackHandler
```

These four handlers live in `src/infra_brain/callbacks/`. Here is what each does:

| Handler | Responsibility |
|---|---|
| `ReadOnlyToolValidator` | Raises on any tool call with a write/create/delete verb — hard block |
| `DLPCallbackHandler` | Scans inputs/outputs for PII and secrets (SSN, credit card, API keys) |
| `AuditCallbackHandler` | Logs every tool call with agent name, domain, tool, input hash, verdict, timestamp |
| `ObservationCallbackHandler` | Aggregates tool usage patterns for the Observations table |

**The invariant:** Every agent must call `build_callbacks(agent_name, domain)` in its
`__init__` and pass the result to `get_chat_model(callbacks=...)`. This is not optional.
The `safety-guard.py` hook warns before you edit any of these files.

---

## Architecture Map

```
Scheduler (APScheduler, single pod)
  ↓
Supervisor (LangGraph StateGraph router)
  ↓ dispatches by domain key
25 Domain Agents (each extends ETLConnector or LLMAgent)
  ↓ collect(), upsert resources + snapshots
PostgreSQL (port 5433 on host)
  ↑
FastAPI (:8001)   ←→ React dashboard SPA (served at /dashboard2)
Redis (:6380)  ←  deduplication lock for scheduler
```

**Critical constraint — APScheduler is single-pod only.** `k8s/scheduler.yaml` must
stay at `replicas: 1`. APScheduler 3.x has no distributed execution lock. Multiple pods
run every job N times. The `lint-k8s.py` hook enforces this at edit time.

---

## The Agents (25 domains — see AGENTS.md, generated from AgentSpec)

| Domain key | What it collects | Cadence |
|---|---|---|
| `linux` | Linux host facts via Ansible | Every 6h |
| `windows` | Windows host facts | Every 6h |
| `net` | Network device state via SNMP | Every 6h |
| `k8s` | Kubernetes cluster state | Every 6h |
| `iac` | IaC repo state (Terraform/Ansible) | Every 6h |
| `vsphere` / `onprem` | vSphere VMs and hosts | Every 6h |
| `cloud` | Cloud resource inventory | Daily 02:00 |
| `cicd` | GitLab pipelines and runners | Daily 02:00 |
| `octopus` | Octopus Deploy projects/deployments | Daily 02:00 |
| `vuln` | Vulnerability scan results | Daily 02:00 |
| `eol` | End-of-life status for components | Daily 02:00 |
| `fleet_health` | Cross-domain fleet health report | Daily 02:00 |
| `drift` | Drift detection (compares snapshots) | Post-collection |
| `discovery` | LLM-driven topology discovery | Weekly |
| `drift_learning` | Drift pattern learning | Weekly |
| `inventory_reconcile` | GitLab source-of-truth reconciliation | Daily 05:00 |
| `remediation` | Closed-loop remediation drafting | Daily 05:30 |
| `vuln_triage` | CVE prioritization | Daily 06:00 |
| `compliance` | Policy-as-code evaluation | Daily 06:30 |
| `rootcause` | Root-cause correlation | Daily 07:00 |
| `notification` | Alert dispatch | Post-collection |
| `integration` | External system sync | On demand |
| `learning_feedback` | Outcome feedback loop | Weekly |

---

## Step 1: Verify Your Environment

Run `/dev-status` — it runs 10 checks and tells you exactly what's broken.

```bash
python .claude/scripts/dev_status.py
```

Every check must pass before contributing. Common failures and their fixes:

| Check | Failure | Fix |
|---|---|---|
| Registry sync | Agent in AGENT_REGISTRY but not in scheduler | `/agent-register` |
| Migration drift | Models.py changed without migration | `/migration-create` |
| Test coverage | New agent file without test | `/agent-scaffold` |
| Callback wiring | Agent not calling build_callbacks() | Add to `__init__` |

---

## Step 2: Your Task Map

Use this to find the right skill for whatever you need to do:

| I want to... | Use |
|---|---|
| Add coverage for a new infrastructure domain | `/agent-register <name>` |
| Add a new API integration to an existing agent | `/tool-register <name> <agent>` |
| Find out why a sweep returned no data | `/sweep-debug [domain]` |
| See the health of all 25 domains at once | spawn `sweep-health` agent |
| Compare drift between two sweep runs | spawn `drift-analyst` agent with domain name |
| Change the database schema | `/migration-create <message>` |
| Validate changes before pushing to master | `/deploy-check` |
| Debug a failing CI pipeline | `/ci-debug [pipeline_id]` |
| Review code for safety/completeness | Classify the changed files against the reviewer matrix and dispatch the applicable `lc-*-reviewer` agents yourself, in one parallel batch (see the "Review batches" section of `.claude/skills/orchestrator/SKILL.md` for the routing matrix + prompt templates) |
| Anything multi-step or unclear | `Skill(skill="orchestrator")` (the `/orchestrator` mode decomposes it) |

---

## Step 3: Critical Constraints

These constraints are enforced by hooks — violating them blocks the edit:

1. **`.env` is never edited by Claude** — the `block-env.py` hook hard-blocks it.
   Edit `.env.example` instead, then update Bitwarden and `config.py`.

2. **All LLM calls must use `build_callbacks()`** — call `build_callbacks(agent_name, domain)`
   in `__init__` and pass `callbacks=` to `get_chat_model()`. No exceptions.

3. **No sync `invoke()` inside FastAPI routes** — use `ainvoke()` / `astream()`.
   Sync calls block the event loop and cause timeouts under load.

4. **Liveness = `/healthz`, Readiness = `/health`** — these are different endpoints:
   - `/healthz` is zero-I/O (no DB, no Redis). Failing it restarts the pod.
   - `/health` checks Postgres + Redis. Failing it removes the pod from LB rotation.
   Using `/health` for liveness means a DB hiccup restarts pods unnecessarily.

5. **New agents need a test file** — the `test-coverage-guard.py` hook warns if an
   agent file is created without a matching `tests/agents/test_*.py`.

6. **Alembic migrations must be generated, not hand-written** — use `/migration-create`
   which runs `alembic check` and reviews for dangerous patterns.

---

## Step 4: Your First Contribution

The cleanest first contribution is a new domain agent for a system not yet covered.
Pick a domain key, run `/agent-register`, and follow the checklist it produces.

```
/agent-register <domain> "<one line description of what it collects>"
```

This scaffolds the agent file, test file, wires it into `supervisor.py` and `scheduler.py`,
and runs `lc-agent-completeness` to verify the wiring is correct.

If you want to add a new data source to an **existing** agent instead:

```
/tool-register <tool_name> <agent_domain> "<what it does>"
```

---

## Where Things Live

| What | Where |
|---|---|
| Domain agents | `src/infra_brain/agents/` |
| Tool integrations | `src/infra_brain/tools/` |
| Safety callbacks | `src/infra_brain/callbacks/` |
| LangGraph supervisor | `src/infra_brain/supervisor.py` |
| Scheduler config | `src/infra_brain/scheduler.py` |
| DB schema | `src/infra_brain/db/models/` (package, split by domain) |
| Alembic migrations | `alembic/versions/` |
| FastAPI routes | `src/infra_brain/api/routers/` (`dashboard_api.py` is now a re-export shim only) |
| Web dashboard | `dashboard-app/` (Vite+React SPA, served at `/dashboard2` as the sole UI, built to `src/infra_brain/dashboard/static2/`) |
| Tests | `tests/` (mirrors src/ structure) |
| Claude Code skills | `.claude/skills/` |
| Claude Code agents | `.claude/agents/` |
| Claude Code hooks | `.claude/hooks/` |
