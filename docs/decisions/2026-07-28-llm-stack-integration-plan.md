# llm-stack integration plan — LiteLLM + Langfuse + mem0 for infra-brain

- **Date:** 2026-07-28
- **Status:** PROPOSED — planning only. Nothing in this document has been built or
  deployed. Do not read any phase below as "accepted and implemented"; every phase is
  explicitly a future, separate execution effort.
- **Decider:** A. Operator
- **Author:** via Claude Code session

**Supersedes/extends:** [2026-07-22 — LiteLLM + Langfuse capability
evaluation](2026-07-22-litellm-langfuse-evaluation.md). That document was a
capability-only evaluation (Langfuse: real win, zero code work remaining once deployed;
LiteLLM/Presidio guardrails: real gap but domain-fit risk; resource sizing: the "~25 GiB"
figure debunked). This document does not re-derive any of that — it references it,
picks the open items back up, and adds a concrete phased plan informed by a live
investigation of a real, already-running `llm-stack` deployment (GitLab
`containers/llm-stack`) on `fedora-fleet`, which the 2026-07-22 document only knew about
in passing (it names the same personal stack but evaluates tool *capability*, not this
specific running deployment's shape).

## Context

infra-brain currently has three things blocked or incomplete, independent of each other:

1. **Langfuse tracing** — code-complete
   (`src/infra_brain/callbacks/langfuse_handler.py`), never deployed. Per the 2026-07-22
   evaluation, activation is 4 env vars (`LANGFUSE_ENABLED`, `LANGFUSE_HOST`,
   `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`) once an instance exists to point at —
   zero code work remaining.
2. **A real path to reasoner-tier LLM smoke tests** — `rootcause_llm_enabled`,
   `compliance_gap_finder_enabled`, and `remediation_interrupt_enabled` (CLAUDE.md
   constraint 11) each require a real-model smoke run before enabling against live
   drift/compliance data, and this has been blocked on AWS Bedrock procurement (see
   `dont-overengineer-add-on-need` / Bedrock-hold memory entries).
3. **No way to audit or observe infra-brain's own LLM usage or spend** — the existing
   `audit_log`/`AgentDecisionLog` tables are compliance-shaped, not cost/session/trace
   shaped (same gap the 2026-07-22 doc identified for Langfuse specifically).

Separately, the maintainer already runs a mature, actively-used `llm-stack` repo (GitLab
`containers/llm-stack`), Ansible-driven, currently deployed on `fedora-fleet` (this
machine), with three services:

- **LiteLLM** — OpenAI+Anthropic-compatible gateway at `:4000`, `network_mode: host`.
  Models include local Ollama-backed `qwen3-4b`/`phi4-mini`/`gemma3-4b`/
  `qwen2.5-coder-3b` requiring **no API key**, plus `claude-*` model IDs that require
  pass-through Anthropic auth — LiteLLM holds no stored Anthropic key of its own.
- **Langfuse** — tracing UI at `:3000`. Official `langfuse/langfuse:3` image +
  `langfuse-worker` + Postgres 17 + ClickHouse + MinIO + Redis.
- **mem0** — per-user memory service at `:8080`, its own vendored FastAPI app:
  `/memories`, `/search`, `/entities`, `/api-keys`, `/requests` (built-in request audit
  log), `/memories/{id}/history` (per-memory audit trail). Backed by its own pgvector
  Postgres.

This plan proposes wiring infra-brain to this existing stack rather than standing up a
separate dedicated deployment, and identifies the local Ollama-backed LiteLLM models as
a genuine zero-key path to *real* (not mocked) reasoner-tier smoke tests — satisfying
CLAUDE.md constraint 11's "real-model smoke run" requirement independent of
Bedrock/Anthropic procurement timing.

## Decision

Seven phases. **Phases 1–5 are the core integration; Phases 6–7 are explicitly-flagged
extensions, least scoped, deliberately not designed in detail here.** All seven are
PROPOSED — none started.

### Phase 1 — Deploy/reconcile llm-stack alongside infra-brain

Two real open items, **not yet resolved**, requiring an explicit decision from the maintainer
before any execution (see Open Risks 1 and 4 below):

- (a) **Host choice** — `deploy-host-01` vs. a dedicated small VM.
- (b) **Reconciling `mem0-mem0-1`** — a container by that name is already running on
  `deploy-host-01` (~5 weeks uptime) — against llm-stack's own `mem0` Ansible role, before
  any redeploy touches that host.

If `deploy-host-01` is the chosen host: remap the two colliding ports (`mem0` off `8080`,
`langfuse-postgres` off `5432`), and consolidate llm-stack's own three Postgres
instances (Langfuse, LiteLLM, mem0) into one shared pgvector-enabled server —
**explicitly NOT merged with infra-brain's own separate Postgres** (blast-radius
isolation preserved).

### Phase 2 — Wire Langfuse tracing

Mint a dedicated infra-brain project/key pair in the deployed Langfuse instance (not
reusing the stack's default "litellm" project). Set `LANGFUSE_ENABLED=true` plus
host/keys in infra-brain's `.env` via Bitwarden, per the existing secrets policy. Zero
code work — `callbacks/langfuse_handler.py` is already complete per the 2026-07-22
evaluation. Retire the idea of infra-brain deploying its own separate
`docker/langfuse/` stack (see resource-sizing note below).

### Phase 3 — LiteLLM as the reasoner-tier smoke-test path

Point `LLM_PROVIDER=openai` + `OPENAI_BASE_URL=http://<host>:4000/v1` at a minted
LiteLLM virtual key, model `qwen3-4b` or `phi4-mini` — real local-model calls, zero
Bedrock/Anthropic key needed, auto-traced in Langfuse via Phase 2. Run
`rootcause_llm_enabled`/`compliance_gap_finder_enabled`/remediation-interrupt LLM
drafting against this for genuine (not mocked — see `scripts/reasoner_tier_dry_run.py`
for the mocked version already built) smoke tests. Claude-quality models remain gated on
real Anthropic/Bedrock key procurement — LiteLLM cannot supply that itself (pass-through
auth only, confirmed via its own User Guide).

**Open risk, not yet resolved (see Open Risks below):** LiteLLM's Presidio-based PII
guardrail runs `default_on: true`, fail-closed, proxy-wide (llm-stack repo's own
ADR-0004/0005) — must be scoped/excluded for infra-brain's traffic or tuned first, per
the domain-fit risk the 2026-07-22 evaluation already identified (Presidio's `PERSON`
detector can't distinguish a hostname/service-account name from a real person's name,
and infra-brain's prompts are full of exactly that content).

### Phase 4 — New observability collector agent

Standard `/agent-register` pattern (read-only HTTP client, `ETLConnector` subclass)
pulling:

- Langfuse's `/api/public/traces` (trace-level detail)
- LiteLLM's `/spend/logs` and `/global/spend/report` (spend/usage/rate-limit metrics)
- mem0's `/requests` (memory-access audit log)

— writing into new infra-brain tables. Spend/usage thresholds feed into the **existing**
`NotificationAgent` escalation pattern (same shape as TRK-241's escalating alerts,
TRK-230's compliance-violation notifications) rather than a new notification mechanism —
proactive alerting, not just passive dashboard visibility.

### Phase 5 — mem0 via MCP, tied to caller identity

New MCP tools:

- `remember(memory_text, user_id)` — mutation-gated, wraps mem0's `POST /memories`.
- `recall(query, user_id)` — read-only, wraps mem0's `POST /search`.
- `get_memory_history(memory_id)` — read-only, wraps mem0's own
  `/memories/{id}/history`.

A shared internal Python client module (e.g. `src/infra_brain/memory_client.py`) used by
**both** the MCP tools and, eventually, internal reasoner-tier agents directly — so
agent-driven memory use comes for free once this exists.

**Explicit design note:** mem0 (per-user conversational memory) is deliberately kept
separate from infra-brain's existing `instincts` mechanism (`promote_instinct` MCP tool,
`drift_learning`/`learning_feedback` domains) — instincts encode learned
**infrastructure** patterns; mem0 encodes learned **user context** across chat sessions.
Do not merge these two systems.

**Open decision, not yet made:** should one MCP key map 1:1 to one mem0 identity, or
should a single key act on behalf of multiple distinct end-users via an explicit
`user_id` parameter?

**Auditability:** mem0's own `/requests` + `/history` endpoints already provide
first-layer audit; every `remember`/`recall` call should **also** flow into
infra-brain's own audit log the same way every existing MCP mutation tool does
(matching `approve_proposal`/`confirm_same_as`'s pattern), so infra-brain's own audit
trail has these calls too, not just mem0's internal one.

### Phase 6 (extension, PROPOSED, least scoped) — Dashboard UI + new routes/APIs

New dashboard pages (following the existing `PageShell`/`Panel`/`DataTable` design system
from the dashboard redesign — see [DR-6.1](DR-6.1-dashboard-stack.md)) to surface
Langfuse trace summaries, LiteLLM spend/usage, and mem0 memory browsing — backed by new
FastAPI routes in `api/routers/` mirroring the existing pattern (async, `response_model`,
auth-gated). **Intentionally the least detailed phase here** — it depends on Phase 4's
collector agent actually landing data to display first, and should be scoped properly
(its own design pass) once Phases 1–5 are further along, not designed blind now.

### Phase 7 (extension, PROPOSED, least scoped) — Knowledge graph edges for LLM activity

Explore whether trace/memory data should surface as graph edges (e.g. linking a
host/deployment/drift-event node to the LLM traces/memories that reference it) via the
existing `graph_phase2`/`graph_phase3`/`graph_role_tagging` machinery. **Explicitly
flagged as unscoped and requiring its own design pass** — what edge types would even be
meaningful here is a genuine open question, not something to default into without a real
answer to "what would someone actually query this graph edge to find out."

## Open Risks

**Not resolved by this document.** All four require an explicit decision from the maintainer
before Phase 1 execution begins.

1. **Host choice conflicts with the 2026-07-22 evaluation's explicit recommendation.**
   That evaluation recommended *against* `deploy-host-01` for hosting this stack
   specifically due to disk posture (was 74% used, documented prior 100%-disk incident,
   ClickHouse/MinIO volumes grow unboundedly) and recommended a small dedicated VM
   (4 vCPU / 8–12 GiB / 100 GB disk) instead. As of this integration-planning session
   (2026-07-28), `deploy-host-01`'s disk usage has gotten **worse** — 88% used, only 30 GB
   free — not better. This document does not resolve this conflict: it requires an
   explicit decision from the maintainer before any Phase 1 deployment work begins — proceed on
   `deploy-host-01` anyway (accepting the risk explicitly), provision the recommended
   dedicated VM, or find a third option.
2. **LiteLLM's Presidio PII guardrail is proxy-wide and fail-closed by default**, with a
   documented domain-fit risk against infra-brain's actual prompt shapes
   (hostnames/service-account names misidentified as `PERSON` entities). Phase 3 must
   not proceed without either scoping infra-brain's traffic out of that guardrail or
   tuning Presidio against real infra-brain prompt samples first.
3. **mem0 identity-mapping scheme** (1:1 key-to-identity vs. explicit per-call
   `user_id`) is an open design choice, not yet decided.
4. **The existing `mem0-mem0-1` container on `deploy-host-01`** (~5 weeks uptime) needs
   its provenance/config reconciled against llm-stack's own `mem0` role before any
   redeploy touches that host — is it already managed by this same Ansible playbook, or
   a separate one-off deployment that would need migrating/replacing?

## Resource sizing (carried forward, not re-derived)

Per the 2026-07-22 evaluation: the "~25 GiB RAM" figure in infra-brain's own
`docker/langfuse/README.md`/`docker-compose.yml` (currently unused, to be retired per
Phase 2) was traced to a mis-applied 3-node HA ClickHouse cluster sizing guide. Real
single-node usage is ~11 GiB / ~7.5 cpus ceiling, ~5.5–6 GiB steady. That figure applies
to a *dedicated* instance sizing estimate — actual footprint when reusing the existing
shared `fedora-fleet`/`deploy-host-01` instance depends on the host-choice decision in Open
Risk 1 above, and is not re-derived here.

## Consequences

Once these phases land: infra-brain gains real (non-mocked) reasoner-tier LLM smoke
tests without needing Bedrock/Anthropic keys (Phase 3), full trace/spend observability
for its own LLM usage (Phases 2 and 4), and per-user conversational memory available to
both chat sessions and internal agents via one shared client (Phase 5) — closing all
three gaps named in Context.

**Nothing in this document has been built or deployed.** This is a planning artifact
only; execution is a separate, deliberate future effort. A Claude Code session on
2026-07-28 evaluated bundling this integration work with that same night's verified
bugfix batch (TRK-234/236/237/238, MR !236) and **explicitly recommended against it** —
the two efforts have different risk profiles (a planning/infrastructure-provisioning
effort with unresolved host/disk and guardrail risks vs. a verified, narrowly-scoped
bugfix batch) and should stay separate rather than being bundled into one deploy.
