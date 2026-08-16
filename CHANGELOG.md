# Changelog

All notable changes to Infra Brain are documented here.

---

## 2026-07-16 — Legacy dashboard retired + CI pipeline pruned

- **Legacy `/dashboard` retired.** The DC-shell / DC-framework dashboard has been deleted
  (`dashboard/src/**`, `src/infra_brain/dashboard/static/index.html`, and the `scripts/design_sync/`
  build pipeline). The Vite+React app in `dashboard-app/` (built to
  `src/infra_brain/dashboard/static2/`, served at `/dashboard2`) is now the sole UI; `main.py`
  redirects `/` → `/dashboard2` and the legacy `/dashboard` mount is removed. This completes the
  DR-6.1 migration. The `/ui-extend` skill and `ui-consistency-reviewer` subagent (both targeting
  the deleted DC dashboard) were removed in the same cleanup.
- **CI pipeline pruned for rapid-dev velocity.** The only MR-blocking gates are now
  `migration-parity` + `sql-execution-check`. Removed jobs: lint, test (pytest + 70% coverage
  floor), env-parity, registry-sync, agent-test-coverage, k8s-yaml-lint, alembic-chain-check,
  design-sync-check, contract-check, no-external-origins, dashboard-app-lint. Master-only jobs
  that remain: build, deploy, backup, runner-disk-prune, rollback, verify-deployed-commit. Removed
  jobs are recoverable from git history if reinstated.

---

## [Open MRs — 2026-07-08] — Dashboard2 consolidation (!127) + B7 legacy-retirement prep (!123)

These two MRs are authored and open as of 2026-07-08. Neither is merged to master yet.
`!127` must merge and be verified live before `!123` can merge. See `docs/audit/ROADMAP.md`
for the exact B6 soak-period and sign-off requirements that gate B7.

Note: !127 (`feat/dashboard2-final-consolidated`) supersedes !124 (`feat/dashboard2-full-consolidated`),
!125 (`docs/migrate-backlog-from-plugin-repo`), and !126 (`docs/sync-status-post-consolidation`),
which were consolidated into this single reviewable MR.

### MR !127 — `feat/dashboard2-final-consolidated` (open, pending review)

Consolidates six individual fix branches into a single reviewable MR. Each component was
developed on its own branch; this MR merges them in dependency order.

- **Font vendoring** (`fix/dashboard2-vendor-webfonts`, MR !114-equivalent): DM Sans and
  DM Mono vendored locally into `dashboard-app/src/assets/fonts/` with `@font-face`
  declarations in `index.css`. Closes the silent system-font fallback that diverged the
  React port visually from the legacy dashboard. External CDN font origins remain prohibited.

- **Skipped-status fix** (`fix/freshness-skipped-status`): `check_collection_health()` and
  the scheduler now distinguish intentionally-skipped collectors (domain in
  `COLLECTION_DISABLED_DOMAINS`) from actually-broken ones. Previously both showed the same
  stale/unhealthy signal, making planned maintenance indistinguishable from breakage.

- **Collection-health filter fix** (`fix/collection-health-finished-at-filter`): The MCP
  collection-health tool was filtering on `started_at` instead of `finished_at`, mismatching
  the freshness semantics used by `freshness.py`. Filter is now consistent across all callers.

- **DR-6.1 visual-parity addendum** (`docs/dr-6.1-visual-parity-addendum`): Adds an
  addendum to `docs/decisions/DR-6.1-dashboard-stack.md` recording two visual-parity
  decisions made during Wave 6 implementation: (1) typography — DM Sans/DM Mono re-vendored
  locally; (2) navigation IA — reorganized 7-section nav kept as intentional improvement,
  not reverted to legacy pixel-for-pixel. See MR description for full rationale.

- **Design foundation** (`feat/dashboard2-design-foundation`): Polish pass on the React
  dashboard — hover states, shared style primitives, sidebar icon/label alignment, chat
  drawer styling, reusable detail drawer component. Brings the `/dashboard2` visual quality
  in line with the legacy `/dashboard` rather than leaving it as a bare functional skeleton.

- **Wave-1 functionality regression fixes** (four branches: `fix/wave1-home-scanschedule`,
  `fix/wave1-remed-resources`, `fix/wave1-views`, `fix/wave1-severity-colors`):
  - `Home.tsx`: drift-trend chart restored (was rendering an empty div); ScanSchedule page
    restored the missing tile that was omitted from the initial port.
  - `Remediation.tsx`: approve/reject workflow restored (action buttons were missing from
    the React port); `Resources.tsx`: chip-count logic corrected.
  - `SavedViews.tsx`: action buttons and metadata columns restored; `CustomViews.tsx`:
    live rendering pipeline connected (views were rendering as static text in the port).
  - EOL/Octopus/Rapid7 severity color-coding restored; audit-log field display fixed
    (fields were missing from the port's detail panels).

- **CSS dedup**: A duplicate `.ib-sidebar` rule introduced by the design-foundation merge
  was removed from the consolidated branch before opening the MR.

### MR !123 — `chore/retire-legacy-dashboard-b7` (open; BLOCKED)

Wave 6.1 Part 3 B7: retire the legacy DC-framework dashboard once `dashboard2` is
confirmed production-ready. This MR deletes the entire legacy frontend tree.

**Explicitly blocked** on two preconditions that are not yet met:
1. MR !127 must merge to master AND be verified live in the deployed container.
2. The B6 soak period (≥ 1 week of side-by-side use with no P1 regression, maintainer
   sign-off required per `docs/audit/implementation/wave-6-plan.md:768-769`) cannot begin
   until !127 is live. The soak clock has not started.

**Do not merge !123** until both gates are cleared. See the B7 addendum in
`docs/decisions/DR-6.1-dashboard-stack.md` for the full deletion inventory.

---

## 2026-07-08 — Post-MR-K schema drift, n8n, and CI migration-parity fixes

Three follow-up fixes merged to master after the MR-A–MR-K wave:

- **Schema-drift reconciliation for MR-J posture/long-tail tables** (`fix/schema-drift-8-tables-live-reconcile`):
  Alembic migration `0032` reconciles unique-constraint drift introduced by MR-J's
  posture/long-tail tables. The schema-drift guard (`schema_check.py`) also had its
  raw-SQL functional/partial index exclusion tightened so it no longer flags index
  variants that Alembic's autogenerate does not model (preventing false-positive startup
  blocks on a correct schema).

- **CI backup-job scope** (`fix/audit-remediation-2026-07-07`): The `pg-backup` CI job
  was running on every pipeline (including MR pipelines), causing unnecessary failures
  unrelated to the MR under review. Restricted to master/scheduled pipelines only.
  Also: `registry-sync` schedule check now exempts the query agent (which has no
  collection domain). Ruff reformatted all touched files to CI-pinned 0.15.18.

- **n8n healthcheck IPv6 fix** (`fix/n8n-healthcheck-ipv6`): n8n container healthcheck
  forced to IPv4 loopback (`127.0.0.1`) to prevent IPv6-first resolution failure on
  some Docker daemon configurations.

- **CI migration-parity chain** (`feat/ci-migration-parity-chain`): The `migration-parity`
  CI job now exercises real DDL (Alembic `upgrade head` against a live ephemeral Postgres)
  instead of the `create_all()` shortcut — ensuring the chain of applied migrations is
  actually tested, not just the final schema shape.

---

## 2026-07-07 — MR-A through MR-K: full audit remediation wave

Eleven MRs merged to master on 2026-07-07 implementing the ranked remediation plan from
`docs/audit/IMPLEMENTATION_PLAN_2026-07-07.md`. Coverage below is at wave granularity;
individual commit details are in `git log`.

### MR-A — Deploy safety (SEC-1, session revocation, logging, compose/CI hardening, backup, deploy gate)
- SEC-1: MCP bearer token now uses `hmac.compare_digest` (constant-time comparison).
- Server-side session revocation via Redis JWT-ID (`jti`) store — logout now invalidates
  the token server-side, not just client-side.
- Shared JSON logging config module (`logging_config.py`) used by `main.py`, `scheduler.py`,
  `mcp_server.py` — consistent format/level across all processes (OB-4).
- Compose resource limits (CPU/mem) on all services; correct `restart: always` policies;
  log size caps via `logging.max-size/max-file`; network segmentation hardened (DEPLOY-1/3, OB-2).
- `pg_dump` backup job added to CI with 14-day retention (DEPLOY-2).
- Manual-approval gate added to CI deploy job; production environment tracked (CICD-1).
- Trivy/ruff pinned to fixed versions in CI; Postgres-wait script deduplicated (CICD-4 partial).
- Collection-health alerts, dead-man's-switch, and agent-anomaly alerts wired to
  `NotificationAgent` (OB-1).

### MR-B — Graph edges, severity vocabulary, vSphere hardening
- Canonical severity vocabulary + contract test: all severity strings now pass through a
  single normalizer; the casing mismatch that zeroed fleet-health vuln metrics and
  remediation CVE counts (AA-C-1, DL-C-4) is closed (TEST-1).
- Contradictory `DEPLOYS_TO`/`DEPLOYED_TO` edge directions reconciled; KG-2 `TRIGGERED_BY`
  fallback removed (KG-1, KG-2).
- vSphere session hardening: `finally: Disconnect(si)` added; vCenter-scoped identity; 
  `IN_DATACENTER` edge emitter added (AA-C-4).

### MR-C — Drift pipeline + scheduler alerting
- `drift_count` always-0 bug fixed: collectors now stamp `collection_run_id` on
  `DriftEvent` rows (AA-C-2).
- `diff_snapshots` nested-path mangling fixed: correct `DriftEvent.field` values (AA-C-6).
- QueryAgent health-check scheduled with `scope="health"` instead of `scope="all"` (AA-C-3).
- Hourly collection-health alerts via scheduler.

### MR-D — Collector-writers hardening (SAVEPOINT, dedup, DSN scrub)
- Octopus 6-of-8 writer blocks now wrapped in SAVEPOINTs; `_resource_id()` None guard (DL-C-5).
- Rapid7 `R7Asset` writer: SAVEPOINT-guarded, truncation added (DL-C-6).
- IaC secret-scan drift events: dedup added, `collection_run_id` stamped (AA-C-8 / DL-C-8).
- DSN/password no longer persisted to `collection_runs.error_message` (SEC-2).

### MR-E — Counts integrity, graph hardening, convergence tests
- FE-6/7/8/9/16 counts+status bugs fixed: `/counts` endpoint now the single authoritative
  source for badge/pill metrics; compliance "resolved" state viewable; EOL definitions
  reconciled to one definition; "Open CVEs" labeling corrected.
- `get_neighborhood`/`_WALK_SQL`: server-side node/edge cap + cycle-safe termination (KG-3).
- Multi-collector convergence test added (TEST-2); module tests for previously-untested
  tools added (TEST-4 partial).

### MR-F — Migration 0027 (pagination indexes, dead-index cleanup, risk_score guard)

### MR-G — LLM runtime (audit-trail, PAN redaction, checkpointer locking, LangGraph fixes)
- `AgentDecisionLog` writes promoted from best-effort debug to `warning` + counter (OB-3).
- PAN redaction in custom-view stream and LLM output (SEC-3 partial).
- Postgres checkpointer serialization across scheduler threads; checkpointer scoped to
  (user, thread) preventing cross-user chat contamination.
- Chat graph compile fixed via `create_agent()`; callbacks propagated to tool calls.

### MR-H — Knowledge-graph enrichment (new edge families, IS_SAME_AS decay fix, confidence filtering)
- New edge types: `HAS_CERT`, `HAS_PORT`, `RUNS_CRON`, `IN_VLAN`, `HAS_FIREWALL_RULE`,
  `HAS_USER`, `HAS_GROUP`, plus Ansible playbook-play→host linkage.
- `IS_SAME_AS` soft-delete status; IS_SAME_AS decay fix; Rapid7/Linux enrichment.
- Consumer confidence filtering: graph queries now filter on `confidence` threshold.
- Identity reconciliation hardened for net/cloud/k8s sources (KG-6).

### MR-I — React migration + renderer (nav, KG-7, a11y, usability) — merged 2026-07-07

This is the large React-migration completion MR. Covers:
- Navigation shell (`Sidebar.tsx`, `nav.ts`): 7-section structure, deliberate UX
  improvement over the legacy 9-section layout (FE-10).
- vSphere (`Vsphere.tsx`) and Octopus (`Octopus.tsx`) pages ported — these had no React
  equivalent before this MR (closes FE-13 parity gap).
- Graph layout time-sliced across `requestAnimationFrame` callbacks (8ms/frame budget,
  early-exit on convergence) — replaces the 150-iteration synchronous blocking pass (FE-15, KG-7).
- Graph truncation cap raised from 100 to 200 (matching the backend `max_nodes=200` default).
- `ChatDrawer.tsx` focus trap + Escape-to-close + return-focus (FE-14 partial).
- Sortable columns, row drill-in, domain grouping, attention widget added.
- `dashboard-app/README.md` rewritten with project-specific docs (FE-2).
- Top-level docs (`README.md`, `DEPLOYMENT.md`, `CLAUDE.md`) updated to mention `/dashboard2`
  and its status (FE-1).

### MR-J — Inventory enrichment (INV-1/INV-4, VLAN map, WinRM fix, long-tail collectors)
- WinRM client wiring bug fixed; INV-4 posture tables + collectors added (certificates,
  firewall/AV, shares, local admins).
- INV-1 Linux enrichment gather wired: NSS users/groups, listen ports, crontab (tables
  were empty despite the models existing).
- Curated host-purpose/VLAN map ingested from static config (item 3).
- Long-tail `LinuxMount`/`LinuxNic` collectors wired.
- Alembic migration `0031` (renumbered from 0030 to chain onto MR-H).

### MR-K — Docs reconciliation (PATTERNS/UI_GUIDE, stale comment fixes, AA-D-27) — merged 2026-07-07
- `docs/PATTERNS.md` reconciled: ETLConnector/BaseAgent split documented; `reason()` loop
  and knowledge-graph edge model sections added; stale `dashboard_api.py` references removed.
- `docs/USER_GUIDE.md` reconciled: DC shell and `dashboard-app/` split documented (FE-11);
  18-page nav listing updated.
- Stale inline code comments fixed for AA-D-25 (netdiscovery), AA-D-26 (netdiscovery),
  KG-8 (relationships.py PART_OF pointer), FE-12 (README page count), DEPLOY-5 (k8s notes).
- File/line citations in the three 2026-07-07 planning docs flagged as historical (they
  pointed at specific line numbers that shift as code is edited).
- `fleet_health.py`: Octopus staleness threshold corrected from 3600s to 86400s to match
  its actual nightly schedule (AA-D-27 — the one code fix in an otherwise docs-only MR).

---

## 2026-07-07 — Audit documentation: security/CI-CD/testing/deployment + fleet-inventory/vSphere (docs only)
Two audit-findings documents added under `docs/audit/`. No code was changed; these record findings for
future remediation planning.

- `docs/audit/SECURITY_CICD_TESTING_DEPLOYMENT_AUDIT_2026-07-06.md` — covers secrets/DLP posture,
  CI/CD pipeline design gaps (no deploy gate, downtime-first deploy, no caching/parallelization),
  test coverage gaps (no severity contract test, no multi-collector convergence test, migration-parity
  skips silently), and deployment/infra config issues (no resource limits, no postgres backup, MCP
  binding inconsistency, stale k8s manifests).
- `docs/audit/FLEET_INVENTORY_AND_VSPHERE_AUDIT_2026-07-06.md` — covers the full data inventory of
  `system_inventory.yml` (playbooks/fleet-ansible, ~40 data categories), the missing data path from
  that playbook into infra-brain (zero coupling; rich data is human-readable-only), the "wired but
  never fed" fact-key gaps in infra-brain's own collectors, Windows collection being fully off in the
  live deployment, and vSphere connector/seed state (deterministically skipped, seed compatible,
  edge contradictions deferred to knowledge-graph audit fix #1).
ROADMAP.md updated with open items for the Critical/high-value findings from both audits.

---

## 2026-07-06 — Knowledge graph + observability/Langfuse audit findings documented

Two new audit documents added under `docs/audit/`. No code changes in this entry — findings only.

- **`docs/audit/KNOWLEDGE_GRAPH_AUDIT_2026-07-06.md`** — Full inventory of the 25-type edge taxonomy against every live emitter. Key findings: `DEPLOYS_TO`/`DEPLOYED_TO` are actively inverted between `octopus.py` and `graph_maintenance.py`; the host→software→CVE→solution traversal chain is structurally impossible today; ~two-thirds of collectors (K8s, cloud, net, netdiscovery, EOL, drift, Ansible runs) create Resource nodes but emit zero edges; `get_neighborhood`'s recursive CTE has no node/edge cap and no cycle-detection clause; the SVG/DOM force-layout renderer is O(n²) and cannot scale beyond a few hundred nodes. 8 prioritized fix items documented.

- **`docs/audit/OBSERVABILITY_LANGFUSE_AUDIT_2026-07-06.md`** — Audit of logging, aggregation, tracing, and alerting across the infra-brain container and plugin-side planes. Key findings: logging is plain-text stdout only with no aggregation, unbounded log growth, and silent container-recreation erasure; 10 failure modes are fully silent with no push notification; `AgentDecisionLog` writes are best-effort at debug level; LangSmith is scaffolded but never enabled; Langfuse cloud is a non-starter (PAN egress risk); self-hosted Langfuse v3 is heavier than warranted at this stage. Recommendation: wire `check_collection_health()` alerts first, then enrich the existing DB path before any new tracing infrastructure. 7 prioritized actions documented.

---

## 2026-07-06 — Frontend/UX audit documented (`docs/audit-2026-07-06-frontend`)

Full audit of the DC dashboard (`/dashboard`) and Wave-6.1 React rebuild (`/dashboard2`)
recorded in `docs/audit/FRONTEND_UX_AUDIT_2026-07-06.md`. Findings only — no code
changed. Covers: sidebar/nav reorganization proposal, data accuracy issues (capped
counts sold as totals, three conflicting EOL definitions, dead logic), documentation
gaps (stock Vite README, top-level docs contradict DR-6.1), robustness issues
(stale-response race in paged loaders, chat re-entrancy, ~17 unwired empty states),
and usability/display quality notes. High-priority items tracked in
`docs/audit/ROADMAP.md`.

---

## 2026-07-06 — Agent architecture + data layer audits (`docs/audit-2026-07-06-agent-and-data-layer`)

Full audit of the agent layer and data layer/knowledge graph performed. Findings
documented in `docs/audit/`; nothing fixed in this pass — findings are open work
items tracked in `docs/audit/ROADMAP.md`.

- `docs/audit/AGENT_ARCHITECTURE_AUDIT_2026-07-06.md` — 8 critical bugs, 12
  robustness gaps, 7 documentation gaps across all agent classes, scheduler, and
  checkpointer.
- `docs/audit/DATA_LAYER_AUDIT_2026-07-06.md` — 8 critical items (migration drift,
  drift-agent crash, Rapid7 severity SLA miscalculation, unguarded writers, swapped
  graph edge types), 9 structural gaps, minor index/constraint findings.

---

## 2026-07-06 — Close deferred 4.2: chat-memory Postgres checkpointer eager startup

Resolves ROADMAP item 4.2 deferred hold: "AsyncPostgresSaver.setup() + live table setup
deferred." The code already called `setup()` when Postgres was configured and the package
importable, but `get_async_checkpointer()` was only ever called lazily on the first chat
request -- meaning connection failures were invisible until a user hit the chat, and the
first cold-start request paid all setup latency.

- **`src/infra_brain/main.py`**: Added `await get_async_checkpointer()` call in the
  FastAPI `lifespan` startup block. Non-fatal: the factory already degrades to MemorySaver
  with a warning on any error, so a missing Postgres connection does not block the app from
  starting. Startup log line confirms which saver was selected.
- **`tests/test_checkpointer.py`**: Added `test_checkpoint_persistence_round_trip` -- writes
  a checkpoint via `graph.ainvoke()`, then reads it back via `checkpointer.aget_tuple()` and
  asserts the persisted state matches what was written. Also asserts the second turn resumes
  from the stored state (not a fresh thread). Added `test_lifespan_source_contains_eager_checkpointer_call`
  to guard the startup call from regression via AST check.
- **`docs/audit/ROADMAP.md`**: Removed "deferred" language from item 4.2 and the deferred
  hold list; marked resolved.

---

## 2026-07-06 — Close deferred: upgrade startup schema-drift guard to full compare_metadata

Resolves the deferred item from the 2026-06-29 De-Brittling Wave 1 entry:
"Startup schema-drift assertion in `main.py` lifespan (P0-6) — currently a hardcoded
allowlist of previously-drifted columns, not a full `compare_metadata`."

The runtime `db/schema_check.py` guard now uses `alembic.autogenerate.compare_metadata`
(the same call the CI `migration-parity` job uses) instead of the hardcoded
`_REQUIRED_COLUMNS` allowlist.  Any drift — missing/extra column, type mismatch,
missing index or FK — on any table blocks startup, not just columns that were
explicitly added to the allowlist after a previous incident.

The old allowlist is removed.  Performance impact is negligible (sub-100 ms full
table reflection on a ~30-table schema).

Tests updated to mock `_full_compare` directly (same contract as before — the check
still raises `SchemaDriftError` on drift and is a no-op on non-PostgreSQL dialects).
New test `test_raises_when_unlisted_column_drift` proves the new approach catches drift
on a table that was never in the old `_REQUIRED_COLUMNS` dict (`resources.some_new_field`),
which the previous allowlist implementation would have silently passed.

---

## 2026-07-06 — Governance router split (`refactor/split-governance-router`)

`api/routers/governance.py` (1035 LOC, 19 routes across ~9 sub-domains) split into four
focused sub-routers; the original file is now a thin re-export shim for backwards
compatibility:

- `governance_drift.py` — drift events, domains, trend, notifications (4 routes)
- `governance_audit.py` — activity log, decisions, audit log (3 routes)
- `governance_intelligence.py` — instincts, observations, generated scripts, integration
  proposals (4 routes)
- `governance_ops.py` — agents roster, settings, agent-config, compliance, inventory
  reconcile, action approvals (8 routes)

`main.py` updated to register all four sub-routers directly. Five test files updated to
patch `get_session` on the correct sub-module (`governance_drift`, `governance_audit`,
`governance_intelligence`, `governance_ops`) instead of the now-empty shim. All external
URL paths unchanged; no breaking API changes. 1644 tests pass.

---

## 2026-07-06 — Audit remediation wave (!87–!95) + design-sync CRLF fix (!97)

Nine targeted fixes merged to master after the `docs/audit/` fast-track remediation
plan. All verified against the full test suite. MR !97 (`fix/design-sync-build-regression`)
was subsequently merged to master (commit `6639c48`).

### !87 — QueryAgent: drop `AgentExecutor` + `langchain_community` (`fix/query-agent-langchain-1x`)
- Ported `agents/query.py` from the deprecated `AgentExecutor` /
  `create_tool_calling_agent` / `langchain_community.utilities.SQLDatabase` stack to
  the current pattern: `@tool`-decorated functions + `LLMAgent.reason()` loop.
- SQL execution now uses plain SQLAlchemy (`get_readonly_engine()` + `text()`) — no
  `langchain_community` dependency.
- AST-level SQL deny-list (`sqlglot`) added in addition to the regex pre-check.
- Callbacks now travel through `self.reason()`, keeping ReadOnly/DLP/Audit chain intact.
- Source: `src/infra_brain/agents/query.py`; tests: `tests/agents/test_query.py`.

### !88 — Compliance upsert + llm_role + dead schedule cleanup (`fix/compliance-upsert-and-coverage-deps`)
- `agents/compliance.py` upserts via raw `INSERT ... ON CONFLICT (rule, host, status)
  DO UPDATE` instead of ORM query-then-insert, eliminating `UniqueViolation` under
  concurrent runs.
- Added `llm_role` to `ComplianceAgent` so the LLM is wired when reasoning about
  violations.
- Removed stale `compliance` entry from scheduler `DEFAULT_SCHEDULES` that pointed at
  a non-existent frequency key.
- Source: `src/infra_brain/agents/compliance.py`.

### !89 — Ansible @tool functions: bare exceptions → `ToolException` (`fix/tools/ansible`)
- All `@tool`-decorated functions in `tools/ansible.py` now raise
  `langchain_core.tools.ToolException` instead of bare `ValueError`/`RuntimeError`.
- Bare exceptions crash the `reason()` loop and bypass DLP/Audit callbacks.
- Source: `src/infra_brain/tools/ansible.py`.

### !90 — graph_maintenance: savepoints around `emit_edges_batch` (`fix/graph-maintenance-txn-rollback`)
- `graph_maintenance.py`'s `emit_edges_batch()` calls are now wrapped in
  `session.begin_nested()` savepoints so a constraint violation on one edge batch
  rolls back only that batch, not the entire graph-maintenance transaction.
- Source: `src/infra_brain/agents/graph_maintenance.py`.

### !91 — Octopus ETL: Luhn-validated PAN masking (`fix/octopus-pan-shape-masking`)
- Added `_luhn_valid()` + `_mask_pan_shapes()` + `_mask_record()` to
  `agents/octopus.py`. Every Octopus API response record is run through
  `_mask_record()` before reaching the ORM or the dashboard.
- Digit sequences 14–19 chars that pass Luhn validation are replaced with
  `[PAN-MASKED]`; a WARNING logs the field count (no values).
- Source: `src/infra_brain/agents/octopus.py` lines 61–128.

### !92 — Windows collector: WinRM cred injection via subprocess env (`fix/windows-collector-winrm`)
- `agents/windows.py` now injects the Windows password as `ANSIBLE_WIN_PASSWORD` into
  the subprocess environment (`extra_env`) rather than exposing it in the argv string.
- Prevents credential leak in process listings and shell history.
- Source: `src/infra_brain/agents/windows.py` lines 23–40.

### !93 — Lint hygiene + refactor safety (`chore/lint-hygiene-refactor-safety`)
- Cleaned up ruff violations introduced by the remediation wave; removed stale TDD
  docstrings and a relative-path test failure.
- No behavior changes.

### !94 — Tenacity retry on rapid7/octopus/gitlab HTTP helpers (`fix/http-retry-tenacity`)
- `tools/rapid7.py`, `tools/octopus_tool.py`, `tools/gitlab.py` now decorate their
  GET helpers with `@retry` from `tenacity`.
- Retries on 429 + 5xx + `TimeoutException` (up to 4 attempts, exponential jitter,
  max 30 s backoff). 4xx errors other than 429 are **not** retried.
- `API_MAX_RETRIES` config field documents the intent; actual attempt count is
  hard-coded at 4 per helper (jitter keeps retries bounded). Source: each tool file.

### !95 — `collection_disabled_domains` config option (`feat/collection-disabled-domains`)
- New `config.py` field `collection_disabled_domains: str = ""` (env:
  `COLLECTION_DISABLED_DOMAINS`).
- Comma-separated list of domain names that `BaseAgent.run()` skips without raising
  an error — designed for planned maintenance pauses.
- Source: `src/infra_brain/config.py` line 194; `src/infra_brain/agents/base.py`.

### !97 — Design-sync CRLF/SRI fix (`fix/design-sync-build-regression`) — MERGED
- Root cause: vendored React assets (`dashboard/src/vendor/`) had CRLF line endings
  from a pre-`.gitattributes`-fix checkout. `update_sri.py` and
  `test_vendored_assets.py` computed SRI hashes over CRLF content; after a fresh
  clone (LF) the hashes diverged and `test_build_reproduces_committed_index` failed.
- Fix: `update_sri.py` and `test_vendored_assets.py` normalize CRLF→LF before
  hashing, so SRI is line-ending-agnostic.
- Merged to master: commit `6639c48`.

---

## 2026-07-01 — Full-bugcheck regression fixes

Fixed the same day they were found by a 5-domain parallel bugcheck (frontend/backend/
database/agents+graph/docs). All changes verified against the full test suite (1475
passed, 58 skipped — unchanged pass/skip count from before, plus 5 new regression tests).

- **Critical: chat safety-chain bypass.** `chat/agent.py`'s `stream_response()` built the
  LLM/graph invocation with no `callbacks=` at all, so `ReadOnlyToolValidator`/
  `DLPCallbackHandler`/`AuditCallbackHandler` never fired on `/api/dashboard/chat`'s tool
  calls. Fixed by attaching `build_callbacks()` to the graph's invocation `config` (not the
  LLM constructor — config-level callbacks propagate to `ToolNode` tool execution too,
  verified empirically). New test: `test_stream_response_wires_safety_callbacks`.
- **Critical: vSphere seeded-graph gap closed.** Added
  `_populate_vsphere_topology_from_metadata()` to `graph_maintenance.py` — a fallback that
  reads `Resource.metadata_` directly (host/cluster/datacenter/datastore_names/
  network_names) for vsphere resources with no `VsphereVm`/`VsphereHost`/`VsphereCluster`
  row, i.e. data that only exists because of the `seed_resource` MCP tool. Closes V1/V2
  from `docs/DE-BRITTLING-PLAN.md` §6 — previously only live-collector data produced
  topology edges. New tests reproduce the "1057 seeded resources, 0 edges" scenario and
  prove the fallback closes it without double-emitting edges for typed-table resources.
- **High: `list_vulns` pagination fixed.** `total` was computed via `q.count()` before the
  `has_exploit`/`pci_only` Python-side filters ran, so `total` and `len(items)` diverged
  whenever either filter was active — the same bug class commit `a3055f5` fixed for
  `list_runs` but never applied to this sibling route. Now filters the full candidate set
  before computing `total` and slicing the page (same pattern `list_cves` already used).
  Strengthened test now seeds a second, non-matching vuln row so `total == 2` /
  filtered-`total == 1` is actually exercised (the old test's total was always 1 either way).
- **High: `create_admin.py` regression fixed.** Removed a `create_tables()`
  (`Base.metadata.create_all()`) call that reintroduced the exact D1 drift-masking
  anti-pattern P0-4 removed from `seed_db.py` — this script runs against the live
  production stack per its own docstring.
- **Medium: `/scan_points` auth gap fixed.** `GET /scan_points` (`webhooks.py`) had no auth
  gate at all, despite `test_contract_map.py`'s comment claiming "webhook-secret auth" —
  added the same `X-Infra-Token` gate its `/api/dashboard/scan_points` sibling already had.
- **Medium: CI now runs the SQL-execution tests.** Added a `sql-execution-check` CI job
  (separate Postgres service from `migration-parity` — its fixture does
  `Base.metadata.drop_all()`/`create_all()`, which would stomp the alembic-built schema if
  shared) so `tests/test_dashboard_sql_columns.py`'s Layer-2 tests, previously permanently
  skipped because `TEST_DATABASE_URL` was never set anywhere, actually execute.
- **Medium: stale frontend preview pages regenerated.** All 30 `dashboard/src/pages/*.dc.html`
  standalone-preview docs were stale relative to `shell.dc.html` (not just the 3 originally
  flagged) — regenerated via `build_pages.write_sources()`. Verified the forward build still
  round-trips to a byte-identical `index.html`.
- **Medium: pre-commit now auto-stages the rebuilt artifact.** New
  `scripts/design_sync/build_and_stage.py` wraps the build + `git add`s
  `src/infra_brain/dashboard/static/index.html`; `.pre-commit-config.yaml`'s
  `design-sync-build` hook now calls it instead of the bare build script.
- **Low: documented, not rewritten.** Added a comment to the already-applied
  `alembic/versions/0008_vuln_eol_writer_indexes.py` documenting its missing
  `postgresql_concurrently=True` (rewriting an applied migration would desync it from what's
  live), and extended the migration-create skill's CONCURRENTLY guidance to cover
  `vuln_queue`/`eol_registry`.

Deferred (separate design calls, per the bugcheck triage): further `governance.py`
decomposition, upgrading the startup schema-drift assertion from an allowlist to a full
`compare_metadata` check, and expanding the `validate-sql` skill's scan root to
`api/routers/`.

## 2026-06-29 → 2026-07-01 — De-Brittling Waves (see `docs/DE-BRITTLING-PLAN.md`)

Most of the plan's Wave 1–3 items shipped in this window. **The plan document itself
still reads as all-pending — treat the table below as the actual status, not the plan.**

### Deploy correctness (Wave 1 — `feat/phase1-stabilize` and related, 2026-06-29)
- Required `postgres_url` (raises if unset) — no more silent localhost-DSN fallback (P0-5/D2)
- `create_tables()`/`create_all()` removed from `scripts/seed_db.py` (P0-4/D1) — **note:**
  `scripts/create_admin.py` was NOT covered by this fix and still calls `create_tables()`;
  see the open bugcheck item below
- Global FastAPI exception handler degrades DB errors per-route instead of crashing the
  whole app (P0-3/B1)
- Startup schema-drift assertion in `main.py` lifespan (P0-6) — currently a hardcoded
  allowlist of previously-drifted columns, not a full `compare_metadata`; general drift
  detection still relies on the CI gate below
- CI `migration-parity` job: ephemeral Postgres + `alembic upgrade head` +
  `compare_metadata`, fails on any diff (P0-7/D3); deploy stage asserts migrate exit code
  + applied revision (P1-2/D5)

### God-file decomposition (`refactor/phase3-godfile-split`, 2026-06-30)
- `dashboard_api.py` (4017 LOC) collapsed to a re-export shim; routes split into
  `src/infra_brain/api/routers/{cve,fleet,governance,hosts,iac,octopus,ui,vsphere,vuln}.py`
  (B2/P1-7). `governance.py` initially ended up as a new 1035-LOC/19-route file spanning
  ~9 sub-domains — split into four focused sub-routers in `refactor/split-governance-router`
  (see below).
- `db/models.py` (1768 LOC) split into `db/models/{core,rapid7,octopus,vsphere,
  cloud_k8s_net,ansible,os_inventory,governance}.py`, re-exported from `db/models/__init__.py`

### Pagination envelope + contract (`feat/phase5-contract`, 2026-07-01)
- ~30 list endpoints migrated to the shared `{items,total,limit,offset}` `PageEnvelope`
  (C1/C2/P2-4), enforced by `tests/test_contract_map.py` across all dashboard/octopus/iac/
  vsphere/graph routes. Two intentional bare-list exceptions remain (`/software/vendors`,
  `/drift_events/domains`).
- OpenAPI/TS contract snapshot generation (`scripts/contract/`) + CI check (§5)

### Frontend hardening (`refactor/phase2-frontend-vendoring`, `feat/phase2-frontend-hygiene`, 2026-06-29/30)
- React/ReactDOM self-hosted with verified SRI, no runtime CDN dependency (F4)
- Single `fetchJson()` + `rows()` chokepoint in `shell.dc.html` guarantees array/envelope
  shape for every list prop entering state (F1/F2/P0-1/P0-2)

### Knowledge graph (`feat/phase6-graph`, 2026-07-01)
- `STORED_ON`/`ATTACHED_TO`/`IN_DATACENTER` relationship types added, live emitters wired
  (V4)
- vSphere topology builder added to `graph_maintenance.py` — **only populates edges from
  the live-collector-written typed tables** (`VsphereVm`/`VsphereHost`/`VsphereCluster`);
  data arriving via the `seed_resource` MCP tool (writes generic `Resource.metadata_` only)
  still produces zero topology edges. The "1057 seeded resources, 0 edges" scenario the
  plan was written to fix is not actually resolved yet (V1/V2 open).

### Known regressions / gaps found in the 2026-07-01 full bugcheck
- `chat/agent.py` builds its LLM without `callbacks=` — bypasses the entire
  ReadOnly/DLP/Audit safety chain on `/api/dashboard/chat` (critical)
- `list_vulns` (`api/routers/vuln.py`) computes `total` before applying enrichment
  filters — same total/items mismatch class as the `a3055f5` fix, but in a sibling route
  never corrected
- `scripts/create_admin.py` still calls `create_tables()` in a production-facing script
- `GET /scan_points` is unauthenticated despite a test comment claiming otherwise
- Custom-view streaming endpoint attaches callbacks but uses sync handlers with blocking
  DB writes inside an async generator

---

## [Unreleased] — 2026-06-19 Expansion

### Added

#### Startup / Infrastructure Fixes
- Fixed migration entry point: `migrate` compose service now runs `alembic upgrade head` correctly
- Fixed scheduler entry point: `run_scheduler_forever()` console script (`infra-brain-scheduler`) calls `load_secrets_into_env()` + `init_tracing()` before starting APScheduler
- Added `infra-brain-scheduler` console script to `pyproject.toml`

#### Security Hardening (Phase A)
- `ReadOnlyToolValidator` callback — hard-denies non-GET infrastructure calls on every LangChain tool invocation
- `DLPCallbackHandler` — Luhn/PAN scan on tool outputs; `dlp_fail_closed=true` blocks PAN egress
- `AuditCallbackHandler` — append-only `audit_log` table for every tool call (allowed + denied)
- `ObservationCallbackHandler` — records tool-usage patterns to `observations` table
- `webhook_auth_required` flag — fail-closed mode for webhook endpoints when secret is unset
- `integration_approval_required` gate — new integrations require `POST /integrations/{id}/approve`

#### Tool Expansion (Phase B)
- `tools/gitlab.py` — GitLab API read-only tool
- `tools/gitlab_mr.py` — GitLab MR creation (InventoryMRAgent write path)
- `tools/vsphere.py` — pyVmomi vSphere read tool (optional extra `vsphere`)
- `tools/rapid7.py` — Rapid7 InsightVM vulnerability API tool
- `tools/context7.py` — Context7 documentation lookup tool
- `tools/iac_reader.py` — HCL2 + YAML IaC file reader (optional extra `iac`)
- `tools/script_runner.py` — deny-list-filtered script execution (disabled by default)
- `tools/confluence.py` — Confluence page creation/update tool
- `tools/jira.py` — Jira ticket creation tool
- `tools/octopus_tool.py` — Octopus Deploy read tool
- `tools/ansible.py` — Ansible inventory/facts tool

#### New Domain Agents (Phase C)
- `agents/vsphere.py` — vSphere/on-prem VM and datastore collector
- `agents/iac.py` — Infrastructure-as-Code HCL2/YAML scanner
- `agents/inventory_mr.py` — GitLab MR inventory proposal agent

#### Hybrid LLM Layer (Phase E)
- `agents/llm_base.py` — `LLMAgent` base class with enforced-callback tool-calling loop (`reason()`)
- All LLM tool calls route through `self.callbacks` — ReadOnlyToolValidator fires on every call
- `AgentDecisionLog` DB model — logs every reasoning iteration (agent, domain, iteration, tools chosen, reasoning text)
- Discovery agent, DriftLearningAgent, and InventoryMRAgent use `LLMAgent.reason()`

#### Closed Self-Improvement Loop (Phase F)
- F1: `GeneratedScript` DB model + `scripts_store.py` — script persistence to Postgres + optional GitLab push
- F2: `ScriptRunnerTool` — deny-list-filtered execution of generated scripts
- F3: `AgentDecisionLog` — reasoning-trace persistence per `LLMAgent.reason()` iteration
- F4: `learning.build_context()` — reads Instincts + Observations + GeneratedScripts into a preamble injected before every `reason()` call
- F5: `agents/drift_learning.py` — `DriftLearningAgent` weekly pattern analysis
- F6: UI pages 8 (Generated Scripts) + 9 (Decisions) for operator visibility

#### Observability
- `observability.py` — `init_tracing()` configures LangSmith when `langsmith_tracing=true`
- `langsmith_*` config fields: `langsmith_tracing`, `langsmith_api_key`, `langsmith_project`, `langsmith_endpoint`
- UI page 7 — Agent Activity (AgentActionLog)

#### Documentation (Phase H)
- `README.md` — root README: read-only guarantee, 4 write paths, architecture, feature list, repo layout, quick start
- `docs/USER_GUIDE.md` — operator guide: install (Docker Compose + k8s), full env-var table, running, operating, optional features, troubleshooting/FAQ
- `docs/superpowers/specs/2026-06-18-infra-brain-agent-design.md` — "2026-06-19 Expansion Update" section appended; superseded assumptions marked
- `.env.example` — regenerated to include all `config.py` fields; stale `SEMAPHORE_*` lines removed; secrets annotated
- `tests/test_env_example_parity.py` — parity test: asserts every `Settings` field present in `.env.example`; asserts no `SEMAPHORE_*` keys

### Changed
- `.env.example` — replaced stale `SEMAPHORE_URL` / `SEMAPHORE_SSL_VERIFY` with all current `config.py` fields
- Scheduler cron schedules updated: domains grouped as every-6h (linux/windows/net/k8s/iac/vsphere), daily-02:00 (cloud/cicd/octopus/vuln/eol/fleet_health), weekly-Sun (discovery, drift_learning)
- Manual sweep endpoint corrected to `/sweeps/{domain}` (was `/sweep/{domain}` in original spec)

### Removed
- `SEMAPHORE_*` config fields — not implemented; removed from env templates
