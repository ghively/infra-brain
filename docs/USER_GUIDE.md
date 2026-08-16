# Infra Brain — Operator Guide

## Contents

1. [Installation](#installation)
2. [Configuration](#configuration)
3. [Running the System](#running-the-system)
4. [Operating](#operating)
5. [Enabling Optional Features](#enabling-optional-features)
6. [Troubleshooting / FAQ](#troubleshooting--faq)

See also: [MCP Server Reference](MCP_SERVER.md) — full tool reference for the
Claude Code / infra-ops integration.

---

## Installation

### Docker Compose (recommended for dev / small deployments)

```bash
git clone <repo> infra-brain && cd infra-brain

# Copy example env and fill in required values (see Configuration below)
cp .env.example .env
$EDITOR .env

# Start everything: postgres, redis, n8n, migrate, seed, app, scheduler, ui
docker compose -f docker/docker-compose.yml up -d
```

The compose file runs services in dependency order:

1. `postgres` + `redis` — datastores
2. `n8n` — event normaliser (optional in dev; safe to skip)
3. `migrate` — runs `alembic upgrade head` (must complete before app starts)
4. `seed` — seeds `scan_points` and baseline data
5. `app` — FastAPI API **and** the web dashboard (`/dashboard2`) on port **8000**
6. `scheduler` — APScheduler cron runner
7. `mcp` — MCP server for Claude Code integration on port **8002** (see [MCP Server Reference](MCP_SERVER.md))

### Kubernetes

Apply the manifests in `k8s/` in order:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/secret-bws.yaml      # contains BWS_ACCESS_TOKEN only
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/redis.yaml
kubectl apply -f k8s/migration-job.yaml   # wait for Completed before proceeding
kubectl apply -f k8s/agent-core.yaml
kubectl apply -f k8s/scheduler.yaml
kubectl apply -f k8s/ui-deployment.yaml
```

Migration **must complete** before the app or scheduler start. The init container
in `agent-core.yaml` runs `alembic upgrade head`; watch for `Job completed` in
`migration-job` before applying the remaining manifests.

---

## Configuration

All configuration is read by `src/infra_brain/config.py` via Pydantic Settings.
Values come from (in priority order): environment variables > `.env` file > defaults.

**Secrets on the deployed stack** are managed by Bitwarden Secrets Manager. At startup,
`secrets.load_secrets_into_env()` authenticates with `BWS_ACCESS_TOKEN` and injects
all project secrets into `os.environ`. `BWS_ACCESS_TOKEN` is the only secret that
must be present as a real k8s Secret / docker environment variable.

For local development, put secrets directly in `.env`.

### Full Environment Variable Reference

| Variable | Default | Secret? | Description |
|---|---|---|---|
| **LLM** | | | |
| `ANTHROPIC_API_KEY` | `""` | Yes (BWS) | Anthropic API key for Claude |
| **Database** | | | |
| `POSTGRES_URL` | `postgresql://infra:infra@localhost:5432/infra_brain` | Yes (BWS) | PostgreSQL DSN |
| `REDIS_URL` | `redis://localhost:6379/0` | No | Redis DSN |
| **GitLab** | | | |
| `GITLAB_URL` | `""` | No | GitLab instance base URL |
| `GITLAB_TOKEN` | `""` | Yes (BWS) | GitLab personal/project access token |
| `GITLAB_SSL_VERIFY` | `true` | No | Verify TLS for GitLab API calls |
| **Octopus Deploy** | | | |
| `OCTOPUS_URL` | `""` | No | Octopus server URL |
| `OCTOPUS_API_KEY` | `""` | Yes (BWS) | Octopus API key |
| `OCTOPUS_SSL_VERIFY` | `true` | No | Verify TLS for Octopus API calls |
| **SaaS / API-key inventory (GitLab #103)** | | | |
| `SAAS_ADMIN_URL` | `""` | No | SaaS admin/management API base URL — empty means the collector is a clean no-op |
| `SAAS_ADMIN_TOKEN` | `""` | Yes (BWS), if enabled | SaaS admin API token |
| `SAAS_VENDOR` | `""` | No | Optional label for the configured admin API |
| **Rapid7** | | | |
| `RAPID7_URL` | `""` | No | Rapid7 InsightVM base URL |
| `RAPID7_USERNAME` | `api_key_auth` | No | Username for basic auth (use `api_key_auth` for key-based) |
| `RAPID7_API_KEY` | `""` | Yes (BWS) | Rapid7 API key |
| `RAPID7_SSL_VERIFY` | `true` | No | Verify TLS for Rapid7 API calls |
| **vSphere** | | | |
| `VSPHERE_HOST` | `""` | No | vCenter hostname / IP |
| `VSPHERE_USER` | `""` | No | vSphere username |
| `VSPHERE_PASSWORD` | `""` | Yes (BWS) | vSphere password |
| `VSPHERE_SSL_VERIFY` | `true` | No | Verify TLS for vSphere API calls |
| **Ansible** | | | |
| `ANSIBLE_INVENTORY_PATH` | `""` | No | Path to Ansible inventory file or directory |
| **Jira** | | | |
| `JIRA_URL` | `""` | No | Jira instance base URL |
| `JIRA_TOKEN` | `""` | Yes (BWS) | Jira API token |
| `JIRA_PROJECT_KEY` | `INFRA` | No | Default Jira project key for ticket creation |
| `JIRA_USER_EMAIL` | `""` | No | Email address for Jira API basic auth |
| **Confluence** | | | |
| `CONFLUENCE_URL` | `""` | No | Confluence base URL |
| `CONFLUENCE_TOKEN` | `""` | Yes (BWS) | Confluence API token |
| `CONFLUENCE_SPACE_KEY` | `INFRA` | No | Default Confluence space key |
| `CONFLUENCE_USER_EMAIL` | `""` | No | Email address for Confluence API basic auth |
| **n8n / Automation** | | | |
| `N8N_URL` | `""` | No | n8n instance URL |
| `N8N_API_KEY` | `""` | Yes (BWS) | n8n API key |
| `N8N_WEBHOOK_SECRET` | `""` | Yes (BWS) | Shared secret for n8n webhook calls |
| **Context7** | | | |
| `CONTEXT7_API_KEY` | `""` | Yes (BWS) | Context7 documentation API key |
| **Paths** | | | |
| `INFRA_OPS_ROOT` | `C:\path\to\infra-ops` | No | Root of the infra-ops source repo (used by IaC reader) |
| **API Tunables** | | | |
| `API_PAGE_SIZE` | `100` | No | Page size for paginated API requests |
| `API_TIMEOUT_SECONDS` | `30` | No | Per-request HTTP timeout in seconds |
| `API_MAX_RETRIES` | `3` | No | Max retry attempts for transient HTTP errors |
| **MCP Server** | | | |
| `INFRA_BRAIN_MCP_TOKEN` | `""` | Yes | Bearer token for the MCP server. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. Must also be set in the infra-ops repo's `.env` for infra-ops to connect. |
| `INFRA_BRAIN_MR_ENABLED` | `false` | No | Set `true` to allow `RemediationAgent` and `InventoryReconcileAgent` to open GitLab MRs. Default `false` — Claude Code / infra-ops opens MRs. |
| **Security** | | | |
| `DLP_FAIL_CLOSED` | `true` | No | Block tool output when DLP detects PAN data |
| `SCAN_READONLY_ENFORCE` | `true` | No | Enforce read-only tool validator (should stay `true`) |
| `INTEGRATION_APPROVAL_REQUIRED` | `true` | No | Require human approval before wiring new integrations |
| **Zones** | | | |
| `DEFAULT_ZONE` | `corpor` | No | Default zone tag for discovered resources |
| `INFRA_HSA_ZONE` | `false` | No | Enable HSA (high-security) zone mode (dual-control instinct gate) |
| `OLLAMA_BASE_URL` | `""` | No | Ollama base URL for local LLM fallback |
| **Script Execution** | | | |
| `SCRIPTS_ENABLED` | `false` | No | Enable ScriptRunnerTool (see safety note below) |
| `SCRIPT_TIMEOUT_SECONDS` | `30` | No | Max execution time for generated scripts |
| **Script Library** | | | |
| `SCRIPT_LIBRARY_PROJECT_ID` | `0` | No | GitLab project ID for script persistence (0 = disabled) |
| `SCRIPT_LIBRARY_BRANCH` | `main` | No | GitLab branch for script storage |
| **AWS** | | | |
| `AWS_ENABLED` | `false` | No | Enable CloudAgent AWS collection |
| **Operational** | | | |
| `INFRA_OPS_ENV_MAX_AGE_DAYS` | `30` | No | Max age (days) before a domain snapshot is considered stale |
| `INFRA_OPS_OBSERVE` | `true` | No | Enable ObservationCallbackHandler (tool-usage recording) |
| `COLLECTION_DISABLED_DOMAINS` | `""` | No | Comma-separated domain names to skip during scheduled collection without raising an error (e.g. `"vsphere,windows"`). Use for planned maintenance windows. Empty = all domains enabled. (F-022, added MR !95) |
| **Webhook Secrets** | | | |
| `WEBHOOK_GITLAB_SECRET` | `""` | Yes (BWS) | Shared secret validated against `X-Gitlab-Token` header |
| `WEBHOOK_OCTOPUS_SECRET` | `""` | Yes (BWS) | Shared secret validated against `X-Octopus-Webhook-Token` header |
| `WEBHOOK_ANSIBLE_SECRET` | `""` | Yes (BWS) | Shared secret validated against `X-Ansible-Token` header |
| `WEBHOOK_GENERIC_SECRET` | `""` | Yes (BWS) | Shared secret for `/webhooks/generic`, `/sweeps/*`, `/integrations/*/approve` |
| `WEBHOOK_AUTH_REQUIRED` | `false` | No | When `true`: if a secret is unset, the endpoint returns 403 (fail-closed). Default `false` = dev-mode permissive. |
| **LangSmith** | | | |
| `LANGSMITH_TRACING` | `false` | No | Enable LangSmith trace export |
| `LANGSMITH_API_KEY` | `""` | Yes (BWS) | LangSmith API key |
| `LANGSMITH_PROJECT` | `infra-brain` | No | LangSmith project name |
| `LANGSMITH_ENDPOINT` | `""` | No | LangSmith ingest endpoint. Default is empty so tracing does **not** silently egress to the public LangSmith cloud — you must set an explicit endpoint (e.g. self-hosted) to opt back in (TRK-042). |
| **Orchestration v2.1 (Phase 2–4 flags — all strictly opt-in)** | | | |
| `SWEEP_GRAPH_ENABLED` | `false` | No | Route sweeps through the LangGraph `StateGraph` instead of `supervisor.dispatch()`-in-a-loop. `false` = byte-identical per-domain cron behavior. |
| `SWEEP_GRAPH_SCHEDULE` | `0 */4 * * *` | No | Cron for the single full-sweep job; only registered when `SWEEP_GRAPH_ENABLED=true`. |
| `ROOTCAUSE_LLM_ENABLED` | `false` | No | Enable LLM root-cause reasoning in `RootCauseAgent`. `false` = deterministic timeline correlation. |
| `ROOTCAUSE_LLM_MAX_EVENTS_PER_RUN` | `20` | No | Per-run cap on LLM reasoning loops; overflow events fall back to the deterministic path. |
| `COMPLIANCE_GAP_FINDER_ENABLED` | `false` | No | Add an LLM-assisted propose-only rule-gap suggester to `ComplianceAgent`. `false` = only the 4 deterministic rules run. |
| `REMEDIATION_INTERRUPT_ENABLED` | `false` | No | Park a durable interrupt graph per drafted remediation action. `false` = poll-only approval flow. Requires a real Postgres checkpointer. |
| `RETENTION_CHECKPOINTS_DAYS` | `30` | No | Age (days) after which stale LangGraph checkpoints are pruned; threads with a pending/approved `ProposedAction` are never pruned (TRK-069). |
| **Langfuse (Phase 4 — self-hosted tracing)** | | | |
| `LANGFUSE_ENABLED` | `false` | No | Append the Langfuse callback handler to both chains. No-op unless the flag is `true` AND all three settings below are set. |
| `LANGFUSE_HOST` | `""` | No | Langfuse base URL (e.g. `https://langfuse.internal.example.com`). |
| `LANGFUSE_PUBLIC_KEY` | `""` | Yes (BWS) | Langfuse public key. |
| `LANGFUSE_SECRET_KEY` | `""` | Yes (BWS) | Langfuse secret key. |
| **Logging** | | | |
| `LOG_FORMAT` | `json` | No | Log line format. Default (unset) = JSON envelope, required for `docker logs`-based triage. Set `text` for plain-text lines (local dev only). |

---

## Running the System

### FastAPI Application

The API is defined in `src/infra_brain/main.py`. It embeds the scheduler (starts
on app lifespan startup). To run with uvicorn directly:

```bash
# With venv activated
uvicorn infra_brain.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

Or use the Docker Compose `app` service.

### Scheduler (standalone)

The scheduler can also run as a standalone process (e.g., in a separate k8s
Deployment). Use the console script installed by `pyproject.toml`:

```bash
infra-brain-scheduler
```

Or call directly:

```bash
python -m infra_brain.scheduler
# equivalent:
python -c "from infra_brain.scheduler import run_scheduler_forever; run_scheduler_forever()"
```

`run_scheduler_forever()` calls `load_secrets_into_env()` + `init_tracing()` then
blocks, running APScheduler in the background.

### MCP Server (Claude Code Integration)

The MCP server is the integration layer between Infra Brain and Claude Code
(infra-ops). It runs as the `mcp` Docker Compose service on port **8002**.

```bash
# Start (after the app service is healthy)
docker compose -f docker/docker-compose.yml up -d mcp

# Verify
curl -i http://localhost:8002/mcp               # → 401 (auth required)
curl -i -H "Authorization: Bearer <token>" \
  http://localhost:8002/mcp                     # → 406 (correct — MCP protocol, not plain HTTP)
```

For the full tool reference — query tools, management tools, auth setup, and
troubleshooting — see **[docs/MCP_SERVER.md](MCP_SERVER.md)**.

### Web dashboard

The dashboard is served by the FastAPI app — no separate process. Start the app:

```bash
.venv/bin/python -m uvicorn infra_brain.main:create_app --factory --port 8000
```

then open `http://localhost:8000/dashboard2`. Authentication is enabled when
`UI_COOKIE_SECRET` is set (sign in with a row from the `ui_users` table); leave it
unset for open dev-mode. Pass `?demo` (any value) to disable the dashboard's 5-minute
background auto-refresh (`dashboard-app/src/hooks/useAutoRefresh.ts`) — there is no
built-in sample-data mode; the app always reads from the real backend.

#### Dashboard admin login (required when auth is gated)

When `UI_COOKIE_SECRET` is set, login requires a row in `ui_users`. **You must set
both `ADMIN_USERNAME` and `ADMIN_PASSWORD` in the deployed environment** — on first
run `scripts/seed_db.py` uses them to create the admin user. If `ADMIN_PASSWORD` is
empty, **no user is created and every login returns 401**, even though the app is
otherwise healthy. (The legacy Streamlit UI used a single `UI_PASSWORD`, which does
not populate `ui_users`, so adopting an old Postgres volume yields no usable login.)

To unlock an **already-running** stack without a redeploy, run the idempotent
bootstrap inside the app container — it creates the admin, or resets the password if
the user already exists:

```bash
docker compose -p infra-brain \
  -f docker/docker-compose.yml -f docker/docker-compose.deploy.yml \
  exec app python scripts/create_admin.py --username admin --password '<strong-password>'
```

(Omit `--username`/`--password` to fall back to `ADMIN_USERNAME`/`ADMIN_PASSWORD`
from the container environment.) For a permanent fix, set `ADMIN_USERNAME` /
`ADMIN_PASSWORD` in the deployed `.env` so future deploys self-heal via the seed step.

---

## Operating

### Trigger a Manual Sweep

```bash
curl -X POST http://localhost:8000/sweeps/linux \
  -H "X-Infra-Token: <webhook_generic_secret>"
```

Replace `linux` with any known domain: `vsphere`, `windows`, `cicd`, `octopus`,
`vuln`, `eol`, `iac`, `fleet_health`, `discovery`, `drift_learning`, `rootcause`,
`compliance`, `vuln_triage`, `remediation`, `learning_feedback`, `query`,
`netdiscovery`, `graph_maintenance`, `inventory_reconcile`, `coverage`.

If `webhook_generic_secret` is not configured, the token header is not required
(dev mode). If `webhook_auth_required=true`, a missing or wrong token returns 403.

A `409 Conflict` response means a collection run for that domain is already
in progress (Redis dedup lock held).

### Reading Drift Events

Via the UI: open the **Drift Events** page, filter by status / domain / time window.

Via the API / SQL:

```sql
SELECT * FROM drift_events WHERE status = 'open' ORDER BY detected_at DESC LIMIT 50;
```

### Approving Integration Proposals

The DiscoveryAgent proposes new integrations (MCP servers, APIs, skills) which land
in `integration_proposals` with `status='pending'`. Review them in the
**Integration Proposals** UI page, then approve via:

```bash
curl -X POST http://localhost:8000/integrations/<proposal-id>/approve \
  -H "X-Infra-Token: <webhook_generic_secret>" \
  -H "Content-Type: application/json" \
  -d '{"approved_by": "your-username"}'
```

Proposals with `confidence < 0.7` cannot be approved (422 response).

### UI Pages Reference

The dashboard has 33 pages under `/dashboard2` (see `dashboard-app/src/App.tsx` for
the full route list and `dashboard-app/src/pages/` for the source — one `.tsx` file
per page). Grouped by area: fleet/inventory (Home, Resources, Fleet, Hosts, Software,
Vsphere, Octopus, Octopushistory), compliance/security (Compl, Security, Vulns, Eol,
Iac), change tracking (Drift, CollRuns, Sweeps, ScanSchedule), governance
(Agents, AgentConfig, Remed, Invrec, Intprops, Instincts, Notifications, Decisions,
Activity, Observations, R7detail), knowledge (Graph, CustomViews, SavedViews), and
settings (Settings). Every page has a sidebar chat panel backed by a read-only SQL
agent, unchanged from the legacy shell's per-page chat panel.

---

## Enabling Optional Features

### vSphere

1. Install the optional extra: `pip install "infra-brain[vsphere]"` (adds `pyVmomi`).
2. Set `VSPHERE_HOST`, `VSPHERE_USER`, `VSPHERE_PASSWORD`.
3. Optionally set `VSPHERE_SSL_VERIFY=false` for self-signed certs.
4. The `vsphere` domain will be scheduled automatically (every 6 hours).

### Script Generation / ScriptRunnerTool

> **SAFETY NOTE:** `ScriptRunnerTool` is a **deny-list filter, not a sandbox jail.**
> Blocked commands (e.g., `rm`, `dd`, `mkfs`) are refused, but the filter is not
> exhaustive. Keep `scripts_enabled=false` unless:
> - The container runs as a non-root user, AND
> - The container is isolated from live infrastructure networks.
>
> The real safety boundary is `scripts_enabled=false` by default. Do not enable in
> a deployed stack without an isolated, non-root environment.

To enable:
```
SCRIPTS_ENABLED=true
SCRIPT_TIMEOUT_SECONDS=30
```

To persist generated scripts to a GitLab project:
```
SCRIPT_LIBRARY_PROJECT_ID=<gitlab-project-id>
SCRIPT_LIBRARY_BRANCH=main
```

### LangSmith Tracing

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<your-key>
LANGSMITH_PROJECT=infra-brain
LANGSMITH_ENDPOINT=<explicit-endpoint>   # required — default is "" (no cloud egress)
```

`LANGSMITH_ENDPOINT` defaults to `""` on purpose (TRK-042): with tracing enabled
but no endpoint set, traces are emitted against an empty endpoint rather than
silently egressing to the public LangSmith cloud. Set it explicitly — e.g.
`https://api.smith.langchain.com` for the public cloud, or a self-hosted
LangSmith-compatible URL — to opt back in.

All LangChain/LangGraph calls will then emit traces to that endpoint.

### Sweep Graph (Orchestration v2.1, Phase 2)

Routes a full sweep through a single LangGraph `StateGraph` instead of the
`supervisor.dispatch()`-in-a-loop path. Strictly opt-in; `false` keeps today's
per-domain cron + dispatch behavior byte-identical.

```
SWEEP_GRAPH_ENABLED=true
SWEEP_GRAPH_SCHEDULE=0 */4 * * *   # cron for the single full-sweep job (only registered when enabled)
```

See the "Sweep graph" cutover runbook in `docs/ARCHITECTURE.md` before flipping
this on. Sweeps must run through `run_sweep_sync()` (dedicated loop) — never
`asyncio.run()`.

### Reasoner-tier LLM Features (Phase 3)

Three strictly opt-in flags, each defaulting `false` so the agent's prior
deterministic behavior stays byte-identical. **Each requires a real-model smoke
run before enabling anywhere with live drift/compliance data** — the
structured-output / tool-call paths have only been exercised against a stub
(`FakeListChatModel`) in CI.

```
ROOTCAUSE_LLM_ENABLED=true
ROOTCAUSE_LLM_MAX_EVENTS_PER_RUN=20   # per-run cap; overflow uses the deterministic path
COMPLIANCE_GAP_FINDER_ENABLED=true
REMEDIATION_INTERRUPT_ENABLED=true
```

Prerequisites (from `docs/ARCHITECTURE.md`, "Reasoner-tier LLM features"):
- `ROOTCAUSE_LLM_ENABLED` — real-model structured-output smoke run (TRK-077).
- `COMPLIANCE_GAP_FINDER_ENABLED` — real-model smoke run of the LLM gap-proposal
  method; confirm `_stable_gap_hash` wording consistency (TRK-078/079).
- `REMEDIATION_INTERRUPT_ENABLED` — requires a real (non-`MemorySaver`) Postgres
  checkpointer (`require_postgres_checkpointer()` hard-fails otherwise) and a
  healthy dedicated sync-loop thread.

Related: `RETENTION_CHECKPOINTS_DAYS` (default `30`) controls how long stale
LangGraph checkpoints are kept before the daily prune job removes them; threads
with a pending/approved `ProposedAction` are never pruned (TRK-069).

### Langfuse Tracing (Phase 4, self-hosted)

A self-hosted-friendly tracing alternative to LangSmith. Strictly opt-in: when
disabled the `langfuse` package is never imported. The flag is a **silent no-op
unless all three settings below are also set** (`maybe_langfuse_handler()`
returns `None`).

```
LANGFUSE_ENABLED=true
LANGFUSE_HOST=https://langfuse.internal.example.com
LANGFUSE_PUBLIC_KEY=<public-key>
LANGFUSE_SECRET_KEY=<secret-key>
```

Prerequisites (from `docs/ARCHITECTURE.md`, "Observability (Phase 4)"):
- The self-hosted Langfuse v3 compose stack (`docker/langfuse/`) must be deployed
  and reachable (operator-only manual deploy, never CI). It has hard resource
  requirements (~25 GiB disk / ~11 cpus nominal, ClickHouse's 8 GiB floor) —
  check `df -h` before standing it up.
- `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` must be
  provisioned via Bitwarden, never hard-coded or committed
  (`docker/langfuse/.env` is gitignored).

### Log Format

Logs default to a JSON envelope (required for `docker logs`-based triage). For
local dev, plain-text lines are available:

```
LOG_FORMAT=text   # unset/any other value = JSON (default)
```

### AWS CloudAgent

```
AWS_ENABLED=true
```

Standard AWS credential resolution applies (env vars, `~/.aws/credentials`, instance
role). The CloudAgent makes read-only API calls only.

### Webhook Authentication (Fail-Closed Mode)

By default, webhooks with no configured secret are allowed through (dev mode).
To enable fail-closed authentication:

```
WEBHOOK_AUTH_REQUIRED=true
WEBHOOK_GITLAB_SECRET=<secret>
WEBHOOK_OCTOPUS_SECRET=<secret>
WEBHOOK_ANSIBLE_SECRET=<secret>
WEBHOOK_GENERIC_SECRET=<secret>
```

With `WEBHOOK_AUTH_REQUIRED=true`, any endpoint whose corresponding secret is empty
returns `403` rather than allowing the request through.

---

## Troubleshooting / FAQ

### Health check

```bash
curl http://localhost:8000/health
```

Returns `{"status": "ok", "postgres": "ok", "redis": "ok"}` when healthy.
`"status": "degraded"` with error details means Postgres or Redis is unreachable.

### "scan pending" / 409 on manual sweep

A Redis dedup lock is held for the domain. The previous collection run is still
in progress (or crashed without releasing the lock). The lock has a TTL; wait for
it to expire (default: tied to collection run duration) or clear manually:

```bash
redis-cli DEL "infra_brain:lock:<domain>:all"
```

### Migration must run before first start

The app will fail with a SQLAlchemy error if the database schema does not exist.
Ensure the `migrate` Docker Compose service (or `migration-job` k8s Job) completes
successfully before starting `app` or `scheduler`.

Manually:
```bash
alembic upgrade head
```

### Secrets not loading

If `BWS_ACCESS_TOKEN` is not set, the app logs:
```
[secrets] BWS_ACCESS_TOKEN not set — skipping Bitwarden bootstrap
```
and falls back to `.env`. This is normal for local development.

If the Bitwarden SDK is not installed:
```
[secrets] bitwarden-sdk not installed — cannot load secrets
```
Install with: `pip install "infra-brain[secrets]"`

### Stale domain warning on startup

```
[freshness] Stale domains on startup: ['linux', 'windows']
```

Means those domains have not been collected within `INFRA_OPS_ENV_MAX_AGE_DAYS`.
Trigger a manual sweep or wait for the next scheduled run.

### LLM tool call blocked (PermissionError)

`ReadOnlyToolValidator` blocked a non-GET infrastructure call attempted by the LLM.
The denial is logged to `audit_log` with `allowed=false` and a `denial_reason`.
Check the Agent Activity UI page or query:

```sql
SELECT * FROM audit_log WHERE allowed = false ORDER BY ts DESC LIMIT 20;
```

### DLP blocked output

`DLPCallbackHandler` detected PAN-shaped data (Luhn-valid card number) in a tool's
output. With `DLP_FAIL_CLOSED=true` (default), the tool result is suppressed.
Check `audit_log` for the blocked call.
