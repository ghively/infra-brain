# infra-brain — Claude Code Instructions

## What This Project Is

Read-only infrastructure audit, drift-detection, and documentation agent built on
LangChain/LangGraph + FastAPI + PostgreSQL + Redis, with a FastAPI-served single-page
web dashboard. The UI is the Vite+React app in `dashboard-app/` (built to
`src/infra_brain/dashboard/static2/`), served at `/dashboard2` — `/` redirects there.
This migration (per [DR-6.1](docs/decisions/DR-6.1-dashboard-stack.md)) is complete:
the legacy DC-shell `/dashboard` has been retired and deleted, and `/dashboard2` is now
the sole UI.

**Core guarantee:** infra-brain never mutates infrastructure. Read-only is enforced in three
layers (see `docs/READONLY-MODEL.md`): (1) **structural** — collectors hold GET-only HTTP
clients (`tools/http_readonly.py`) and the SQL agent uses a SELECT-only DB role; (2) **boundary
gate** — every LLM tool call is checked pre-execution by `callbacks/boundary.py`, and every
sanctioned external write (GitLab MR / Jira / Confluence) passes `callbacks/write_gate.py`;
(3) **audit** — the callback chain (`AuditCallbackHandler → ReadOnlyToolValidator →
DLPCallbackHandler → ObservationCallbackHandler`, plus an optional Langfuse handler
appended last when `langfuse_enabled`) records every call. vSphere (pyvmomi), Kubernetes (client lib), and
nmap/`ip route` subprocesses cannot be made GET-only structurally — they are read-only **by
convention**, audited per call (netdiscovery is gated by `_gate_nmap_targets`). Do not bypass
or weaken any of these layers under any circumstances.

---

## How Work Gets Done (read this first)

**Orchestration is a top-level MODE, not a spawnable subagent** (redesigned
2026-07-22 — see `docs/decisions/2026-07-22-orchestrator-redesign-plan.md`).
**For any substantive task, invoke `Skill(skill="orchestrator")`** — this loads
`.claude/skills/orchestrator/SKILL.md` into the current session, which then does
the decomposition and dispatches specialist `Agent()` calls **itself**, in parallel
batches, directly from wherever it's running (almost always the top-level session).

**Why not a subagent:** subagents in this harness have **no `Agent`/`Task` tool
available to themselves** (confirmed empirically — a probe subagent's own tool list
came back with no `Agent`/`Task`). The old `Agent(subagent_type="orchestrator", ...)`
pattern is gone — that agent definition has been retired, because it could never
actually do its documented job (fan out to specialists) once spawned as a subagent;
it silently degraded to serial self-work with every specialist review step skipped.
**Never call `Agent(subagent_type="orchestrator")` — that type no longer exists.**

**When to invoke the orchestrator skill:**
- Building or modifying any agent, tool, callback, or route
- Debugging (sweeps, errors, test failures)
- Database schema changes
- Deployment and pre-deploy checks
- Any request with multiple steps or multiple files

**When to work directly (skip it):**
- Simple questions or explanations → `/lc-explain`, `/lc-docs`
- Single-file read or status check → `/dev-status`, `/sweep-debug`
- You are already a subagent mid-task — you have no `Agent`/`Task` tool at all;
  do your assigned work directly and report results upward, don't try to dispatch
  further.

**What the skill actually covers now** (full detail in
`.claude/skills/orchestrator/SKILL.md`): request classification and task-graph
decomposition (unchanged from before); execution-substrate choice — plain `Agent()`
batches by default, or the `Workflow` tool if you explicitly opt in (say "use a
workflow" / "ultracode") for task graphs with ≥4 code-writing tasks or a multi-stage
per-item shape; a 4-tier model-routing rubric (Fable for judgment/investigation,
Opus for high-blast-radius implementation, Sonnet for scoped implementation, Haiku
for mechanical checks); **mandatory `isolation:"worktree"` for every code-writing
dispatch**, mechanically enforced by the `require-worktree-isolation` `PreToolUse`
hook (override with `NO_WORKTREE_OK` in the prompt when deliberate); a rolling
integrate-as-they-finish branch-consolidation procedure (not wait-for-all-then-
reconcile) with a shared-file playbook for the findings tracker (not present in
this public copy)/`AGENTS.md`/`config.py`;
and the evidence-gated synthesis rule (F-015) from before, unchanged.

The `review-coordinator` agent is also retired — its reviewer-routing matrix and
prompt templates are absorbed into the skill's "Review batches" section; dispatch
the applicable reviewers yourself in one parallel batch instead of spawning a
coordinator to do it (which had the identical spawn-capability problem).

---

## Working Here

**Doc map:** `docs/decisions/` holds the architecture decision records — read
these first for *why* a subsystem is shaped the way it is (start with
`2026-08-11-graph-first-architecture.md` for the knowledge-graph model and
`2026-08-10-graph-edge-authority-spec.md` for the identity-authority rules).
`docs/ARCHITECTURE.md`, `docs/READONLY-MODEL.md`, and `docs/PATTERNS.md` cover
the standing subsystems. This is a curated, sanitized copy of a longer-running
project — its day-to-day findings tracker, session handoff notes, and an
embedded ops-tooling plugin (originally tracked separately, referenced
elsewhere in this codebase as `agent/`) were intentionally not carried over;
see the top-level README for what was omitted and why.

### Test command
```bash
.venv/bin/python -m pytest tests/ -q
```
All tests must stay green. Run a focused subset when touching specific areas:
```bash
# Agents only
.venv/bin/python -m pytest tests/agents/ -q
# Dashboard API + auth
.venv/bin/python -m pytest tests/test_dashboard_api.py tests/test_dashboard_auth.py -q
# SQL column guard (no DB required)
.venv/bin/python -m pytest tests/test_dashboard_sql_columns.py -v
# Safety callbacks
.venv/bin/python -m pytest tests/callbacks/ -q
```

### Linting
Requires the `dev` extra (`ruff` is not in the default install): `uv sync --extra dev`.
```bash
.venv/bin/python -m ruff check --fix src/ tests/
.venv/bin/python -m ruff format src/ tests/
```

### Merge policy
**Commit locally early and often** — small, focused local commits on a feature branch are
encouraged throughout a work session and need no approval. **Do NOT push or open an MR after
every commit.** Push the branch and open a GitLab MR only when a unit of work is *done* or at
the end of the working day — batch the session's local commits into that one push. Confirm
with the user before pushing / opening the MR (it is not automatic).

Flow: feature branch → (many local commits) → push → GitLab MR → merge. `git log --merges`
shows the actual workflow is MR-gated (20+ merges), consistent with
`docs/DE-BRITTLING-PLAN.md`'s "propose-only / MR discipline." Do not push directly to
`master` — the MR-blocking CI gates (`migration-parity` + `sql-execution-check`) only run on
MR pipelines. The gate set was intentionally pruned for rapid-dev velocity (lint,
test/coverage, env-parity, contract-check, and others were removed — recoverable from git
history if reinstated). **Reaffirmed 2026-07-22 (the maintainer):** this pruning is a deliberate,
standing decision, not a pending TODO — do not spend effort reinstating gates
proactively; reinstate an individual gate only if/when a specific concrete need for it
arises.

### Dev-host live-patch workflow (deploy-host-01, pre-production)
**Reaffirmed 2026-07-24 (the maintainer) — this is a standing workflow, not a one-off exception.**
The commit→push→MR→pipeline→deploy round-trip is correct as the system of record, but
using it as the *only* path to ship a small change while the user is actively working
adds a full pipeline wait for no benefit, while deploy-host-01 remains pre-production.
**Broadened same day** from bugfixes-only to also cover small scoped frontend tweaks —
the same "wait for the whole pipeline just to see if this works" friction applies to a
small UI addition mid-session, not just to bugs.

**Eligible for live-patch-first (patch → verify → then push):**
- Any bug fix — code that's misbehaving relative to its own intent (wrong computation,
  swallowed error shown as success, wrong status mapping, broken link, wrong sort, etc.)
  — **regardless of layer**, including backend Python. A backend fix is still "just a bug
  fix" even though the live-patch mechanics differ from frontend (see Procedure below).
- Small, scoped **frontend-only** changes made while the user is actively iterating in a
  session — copy/text additions, small new UI elements or panels, layout tweaks, adding
  info/instructions to an existing page. Judgment call: if it's the kind of thing you'd
  describe in one sentence and it doesn't touch anything in the "not eligible" list below,
  it qualifies.

**NOT eligible — pipeline-only, no live-patching:**
- **New backend capability**, as opposed to a fix to existing backend behavior:
  new/modified API routes, database schema, migrations, new config fields
  (`config.py`/`.env.example`), or any *addition* to the safety chain (`callbacks/`,
  `supervisor.py`, `tools/`, MCP auth). A bugfix that touches one of these files (e.g.
  correcting a wrong status-mapping dict already in `callbacks/`) is still eligible per
  the bug-fix rule above — it's *new* capability in these areas that's excluded, not any
  edit that happens to land in them.
- Large or structural new features (new pages wired into routing, new backend
  capabilities, anything that needs real review before it's "done") — even if the surface
  touched is technically just frontend files, size/structure is what disqualifies it here,
  not file location.
- If genuinely unsure which side of the line something is on, ask rather than guess.

**Procedure — frontend:**
1. Implement + test locally, same as always.
2. `npm run build` (the served bundle is `static2/`, not source — a source-only patch has
   no effect), then `docker cp` the rebuilt `static2/` output into the running container.
   No restart needed — static files serve fresh per request.
3. Verify on the live host (reload the page, exercise the fix) before calling it done.

**Procedure — backend (Python):**
1. Implement + test locally, same as always.
2. `docker cp` the fixed `.py` file(s) into the running container at their `/app/src/...`
   path. **Then restart that container's process** (`docker compose restart <service>`, or
   equivalent) — unlike frontend static files, Python source is only re-read at process
   start; copying the file alone changes nothing live. Acceptable on a pre-production
   single-replica dev host; do not do this to a real multi-replica production deployment
   without rethinking the whole approach.
3. Verify on the live host (re-run the failing check, re-hit the endpoint) before calling
   it done.

**Then, for both:**
4. Commit the source change to git, push, and open the MR as usual (§Merge policy,
   above — still needs user confirmation before push). The eventual pipeline deploy
   re-syncs git-as-source-of-truth over the hand-patch; that overwrite is expected, not a
   conflict, and does not need to happen before the user is unblocked.

**Revisit once deploy-host-01 (or its successor) is real production** — hand-patching a
running container that real users/uptime depend on is a different risk calculus than
doing it on a pre-production dev host; do not carry this workflow forward unexamined once
that changes.

### PostgreSQL MCP
`.mcp.json` at repo root configures the `postgres-infra` MCP server automatically.
Requires: `POSTGRES_URL` set in `.env` (host port 5433) and Node.js installed for `npx`.
Once connected, Claude can query `collection_runs`, `drift_events`, `resources`, and
`snapshots` directly — powers the `sweep-health` and `drift-analyst` subagents.

---

## Critical Files — Handle with Care

| File | Why it's critical | What to do |
|---|---|---|
| `src/infra_brain/config.py` | Widely imported (~40+ modules) — a breaking change cascades everywhere | Run full pytest after any change |
| `src/infra_brain/db/models/*.py` (package — split from the former single `models.py` in the god-file decomposition) | Schema source of truth, imported repo-wide via `db/models/__init__.py` | Run the CI `migration-parity` job (or `alembic check` + full pytest) after any column change; new columns need a migration |
| `src/infra_brain/db/session.py` | Widely imported — a breaking change cascades everywhere | Run full pytest after any change |
| `src/infra_brain/callbacks/readonly.py` | Read-only enforcement boundary | Never remove a `raise` or weaken a guard; run callback tests |
| `src/infra_brain/callbacks/dlp.py` | PII/secret scanner | Do not remove pattern checks; run callback tests |
| `src/infra_brain/callbacks/registry.py` | Wires all safety callbacks into agents | Every new agent must flow through this |
| `src/infra_brain/supervisor.py` | Routes all agents in AGENT_REGISTRY — see AGENTS.md for the roster — single point of failure | Test routing logic; don't add conditional edges without tests |
| `src/infra_brain/graph.py` | Sweep orchestration topology — registry-derived (nodes come from `etl.spec.sweep_members()`); a single point of failure for every sweep once `sweep_graph_enabled` is on | Changes need tests in `tests/test_sweep_graph.py`; see `docs/ARCHITECTURE.md`'s "Sweep graph" section |

---

## Architecture Constraints

1. **All tool calls must flow through `build_callbacks()`** from `callbacks/registry.py`.
   Never call `get_chat_model()` without passing `callbacks=`.
2. **No sync `invoke()` inside FastAPI routes** — use `ainvoke()` / `astream()`. Sync
   calls block the event loop and cause request timeouts under load.
3. **Callback handlers must be async** — subclass `AsyncCallbackHandler` in any async
   context. Mixing sync handlers into async chains silently blocks the event loop.
4. **Raw SQL must reference real column names** — run `/validate-sql` before merging any
   change to `src/infra_brain/chat/tools.py` or `src/infra_brain/api/routers/*.py`. The
   ORM is the source of truth. (Most dashboard endpoints use the ORM directly; the chat
   tools carry the raw SQL. `dashboard_api.py` is now a re-export shim only — the real
   route handlers live in `api/routers/`.)
5. **Alembic migrations must be generated, not hand-written** — run
   `/migration-create` which validates with `alembic check` and reviews for dangerous
   patterns (NOT NULL without default, DROP TABLE, missing CONCURRENTLY).
   **A green local suite is NOT pre-push verification for schema changes** — the
   full suite runs on sqlite; the four hard MR gates (`lock-freshness`,
   `migration-parity`, `sql-execution-check`, `agent-orm-check`) run on real
   PostgreSQL/the real lock file. Before pushing ANY change to `db/models/`,
   `alembic/versions/`, dialect-specific types, raw dashboard/chat SQL, **or an
   ORM query in `agents/`/`etl/`**, replicate all four gates locally with
   **`/pg-gate-check`** (`bash .claude/skills/pg-gate-check/run.sh`; add
   `PG_GATE_SKIP_ORM=1` for the fast schema-only loop). Reviewer approval does not
   substitute — all three reviewers approved the 2026-07-21 MR !195 failure.
   `agent-orm-check` (TRK-356) is the fourth gate: `PG_GATE_DSN` flips
   `tests/support/pg.py::make_engine` from in-memory sqlite to real PostgreSQL so
   `tests/agents` + `tests/etl` execute their ORM constructs on the real dialect —
   added after TRK-350's `IN (SELECT (SELECT …))` passed 5,215 green tests and
   raised `CardinalityViolation` on the first live drift run.
6. **New domain agents need a test file** — the test must cover at least: success case,
   empty result, exception. Use `/agent-scaffold` to get both files at once.
7. **The `.env` file must never be edited by Claude** — it contains secrets. Edit
   `.env.example` instead, then update Bitwarden and `config.py`.
8. **Liveness probe must be `/healthz`, readiness probe must be `/health`** — they are
   different endpoints. `/healthz` is zero-I/O (no DB, no Redis) and restarts the pod if
   it fails. `/health` checks Postgres + Redis and removes the pod from LB rotation. Using
   `/health` for liveness means a DB blip restarts pods instead of just pulling them from
   the load balancer. See `k8s/agent-core.yaml`.
9. **APScheduler must run as a single pod** — `k8s/scheduler.yaml` must stay at
   `replicas: 1`. APScheduler 3.x has no inter-process execution lock; multiple pods run
   every job N times. Do not scale the scheduler. Use `shutdown(wait=True)` only —
   `shutdown(wait=True, timeout=30)` raises `TypeError` (timeout param was never
   implemented in 3.x).
10. **The sweep graph is opt-in via `sweep_graph_enabled`** — sweeps must go through
    `run_sweep_sync()` (dedicated loop) — never `asyncio.run()`.
11. **Three reasoner-tier LLM flags default off** — `rootcause_llm_enabled`,
    `compliance_gap_finder_enabled`, `remediation_interrupt_enabled`. Each requires a
    real-model smoke run (structured-output / tool-call) before enabling in an
    environment with live drift/compliance data; `remediation_interrupt_enabled`
    additionally requires a real (non-`MemorySaver`) Postgres checkpointer. See
    `docs/ARCHITECTURE.md`'s "Reasoner-tier LLM features" section. A fourth flag,
    `langfuse_enabled` (also default off), gates Langfuse tracing — it requires the
    self-hosted Langfuse v3 compose stack (`docker/langfuse/`) deployed and reachable
    plus `LANGFUSE_HOST`/`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` in Bitwarden; see
    `docs/ARCHITECTURE.md`'s "Observability (Phase 4)" section.

---

## Available Skills (`/skill-name`)

### Orchestration
| Skill | What it does |
|---|---|
| `/orchestrator` | **Start here.** Top-level orchestration mode (not a subagent) — decomposition, model routing, worktree lifecycle, 20-scenario routing tree. |

### infra-brain Project Skills
| Skill | What it does |
|---|---|
| `/agent-scaffold <name> <description>` | Create new domain agent + test from project template |
| `/agent-register <name> [--skip-hook] [--schedule <cron>]` | Full agent wiring: scaffold + supervisor + scheduler + completeness check |
| `/validate-sql` | Run the static SQL column validator on all raw dashboard/chat queries |
| `/openui-sync` | Audit OpenUI component library vs API endpoints — cross-check components have backing endpoints, report orphans and opportunities |
| `/migration-create <message>` | Safe Alembic migration workflow with danger-pattern review |
| `/pg-gate-check` | Replicate the three hard MR gates (lock-freshness + migration-parity + sql-execution-check) locally against the real CI postgres image — REQUIRED before pushing any schema/dialect-type/raw-SQL change; the sqlite suite cannot see these failures |
| `/deploy-check` | Full pre-deployment validation checklist |
| `/sweep-debug [domain] [run_id]` | Diagnose failed/empty sweeps: 5-layer triage (DB → audit_log → Redis → logs → LangSmith) |
| `/dev-status` | 10-check tooling health audit: registry, tests, schedules, callbacks, migrations, k8s, env parity, SQL |
| `/ci-debug [pipeline_id]` | Diagnose failing CI pipelines: fetch job logs, classify failure type, suggest fix |
| `/tool-register <name> <agent>` | Register a new @tool: create file + test + wire into agent + safety review |
| `/onboard` | infra-brain onboarding: safety model, agent architecture (`AGENT_REGISTRY`), critical constraints, first contribution guide |

### LangChain Lab Skills (bundled — always available)
| Skill | What it does |
|---|---|
| `/lc-start` | Full onboarding for new LangChain projects — goal, scaffold, first run, LangSmith |
| `/lc-agent` | ReAct, Supervisor, Plan-and-Execute, Reflection, Send API patterns |
| `/lc-graph` | StateGraph, nodes, edges, checkpointing, interrupts, streaming, subgraphs |
| `/lc-rag` | 8 RAG variants: naive, multi-query, compression, self-RAG, CRAG, agentic |
| `/lc-memory` | Buffer, summary, checkpointing, vector memory, entity, Store API |
| `/lc-tools` | @tool, StructuredTool, async tools, ToolNode, toolkits, MCP |
| `/lc-lcel` | Runnable interface, pipe composition, branching, streaming, retry/fallback |
| `/lc-deploy` | Local dev, LangGraph Platform, Docker, Kubernetes deployment |
| `/lc-test` | Unit, state, integration tests; LangSmith eval; CI/CD integration |
| `/lc-debug` | Structured triage for all LangChain/LangGraph error categories |
| `/lc-monitor` | LangSmith setup, tracing, dashboards, online eval, Prompt Hub |
| `/lc-guardrails` | Prompt injection, PII protection, cost circuit breaker, HITL |
| `/lc-resilience` | Retry with jitter, fallback chains, circuit breaker, connection pooling |
| `/lc-compliance` | GDPR, HIPAA, EU AI Act patterns |
| `/lc-audit` | Immutable audit tables, cryptographic hash chains, compliance views |
| `/lc-providers` | Configure/swap LLM providers: Anthropic, OpenAI, Azure, Bedrock, Gemini, Ollama |
| `/lc-ui` | Streamlit, Chainlit, Gradio, FastAPI+HTMX streaming UI patterns |
| `/lc-data` | Text-to-SQL, Pandas agent, OpenAPI agent, multi-source data agents |
| `/lc-vectorstore` | pgvector, Chroma, Pinecone, embeddings, hybrid search, multi-tenant |
| `/lc-multimodal` | Image analysis, PDF loaders, table extraction, audio, multimodal RAG |
| `/lc-context-engineer` | Prompt templates, few-shot, structured output, Prompt Hub |
| `/lc-patterns` | Recommend the right pattern for your use case |
| `/lc-explain <concept>` | Explain any LangChain/LangGraph concept with code + analogies |
| `/lc-docs <topic>` | Fetch live documentation via Context7 |
| `/lc-review [file]` | Review LangChain code across 5 dimensions |
| `/lc-scaffold [type]` | Scaffold project/agent/graph/RAG/tool/chain/evaluator files |
| `/lc-trace <file>` | Inject LangSmith tracing into an existing Python file |
| `/lc-guard [path]` | Audit for 8 security gaps; generate guardrails layer |
| `/lc-erase <user_id>` | GDPR Article 17 right-to-erasure workflow |
| `/lc-antipatterns` | Catalog of 15 LangChain antipatterns with fixes |
| `/lc-architect` | Deep architecture spec — invoked when design decisions exceed a single skill's pattern heuristics; outputs a self-contained architecture doc, no code |
| `/lc-coder` | Specialist code-generation — produces complete production-quality LangChain/LangGraph files, not snippets |
| `/lc-reviewer` | Seven-dimension LangChain/LangGraph code review with file:line findings; used by `/lc-review` and as a pre-merge gate |
| `/context-engineer` | Prompt templates, system-prompt authorship, few-shot, structured output, context-window management, output parsers |
| `/design-system` | Structured technical interview → LangChain/LangGraph architecture spec doc (`docs/specs/`); no code written |
| `/graph` | LangGraph StateGraph design: state, nodes, edges, checkpointing, interrupts, streaming, subgraphs, Send |
| `/rag` | Scaffold the right RAG pattern — naive through Self-RAG/Agentic RAG on LangGraph |
| `/start` | langchain-lab plugin onboarding for complete beginners — scaffold, hello-world, LangSmith setup |

## Available Subagents

**Note: `orchestrator` and `review-coordinator` are retired as subagents** (2026-07-22
— see "How Work Gets Done" above). Orchestration is now the `/orchestrator` skill,
executed by whoever has the `Agent` tool; multi-reviewer dispatch is the skill's
"Review batches" section — dispatch these leaf agents yourself, in parallel, rather
than through a coordinator.

| Agent | When to use |
|---|---|
| `lc-safety-reviewer` | Any change to callbacks/, supervisor.py, tools/ — checks callback wiring, DLP bypass paths |
| `lc-migration-reviewer` | Any generated Alembic migration — checks NOT NULL, DROP TABLE, missing CONCURRENTLY |
| `lc-api-reviewer` | Any new/modified FastAPI route — checks async correctness, auth, response_model |
| `lc-agent-completeness` | Any new agent file — validates all 4 wiring points: file, test, AGENT_REGISTRY, schedule |
| `sweep-health` | Cross-domain sweep status — queries collection_runs via PostgreSQL MCP, flags overdue/failed domains |
| `drift-analyst` | Per-domain drift delta — compares last 2 completed runs: new/resolved/persistent events (needs MCP) |
| `infra-researcher` | Parallel context-gathering subagent, dispatched directly (from orchestrator-mode decomposition) for independent research subtasks |

---

## Secrets and Configuration

- Only `BWS_ACCESS_TOKEN` lives outside Bitwarden (k8s Secret / Docker env var).
- All other secrets are pulled at startup via `secrets.py → load_secrets_into_env()`.
- To add a new secret: add to `.env.example`, document in `docs/USER_GUIDE.md`, add to
  Bitwarden, update `config.py`.
- Never hard-code credentials, tokens, or API keys anywhere in the codebase.
