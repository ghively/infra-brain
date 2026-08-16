# Infra-Brain Operational Backlog

Tracking document for open work items, audit-remediation follow-on, and operational
drills. Previously tracked in the infra-ops plugin repo; migrated here 2026-07-08
so that the infra-brain repo reflects its own true operational status independent of
external tooling.

---

## Audit Remediation — Wave 5 (2026-07-08 batch)

These items were part of the 11-MR audit-remediation wave merged to master on 2026-07-08.

- [x] **Chat memory persistence gap** — resolved via `fix/chat-memory-postgres-checkpointer`,
  merged MR !110 (2026-07-08). `AsyncPostgresSaver.setup()` is wired at eager startup init;
  persistence round-trip test added.

- [x] **governance.py god-file** — split via `refactor/split-governance-router`, merged MR !110
  (2026-07-08). 1035-LOC router split into 4 focused sub-routers.

- [x] **InventoryMRAgent not wired into AGENT_REGISTRY** — resolved via
  `feat/wire-inventory-mr-agent`, merged MR !110 (2026-07-08). Registration complete; agent
  is now dispatchable.

- [x] **Schema-drift check was a hardcoded allowlist, not full compare_metadata** — resolved via
  `fix/schema-drift-full-compare-metadata`, merged MR !110 (2026-07-08).

- [x] **migration-parity CI job structurally could not catch index-vs-constraint drift** —
  resolved via MR !113 (`feat/ci-migration-parity-chain`, merged 2026-07-08).
  `0001_initial_schema.py` now scans sibling migration files and excludes their tables
  automatically (self-maintaining), so migration-parity exercises real DDL instead of the
  `create_all()` shortcut.

- [x] **MR !111 deploy verified in production** — confirmed 2026-07-08 15:43 UTC.

- [x] **2 remote branches awaiting deletion** — `fix/collector-hygiene-p2p3` and
  `fix/dashboard-api-errors` deleted 2026-07-08.

- [x] **`fix/design-sync-build-regression` local branch** — deleted 2026-07-08 (confirmed
  merged via MR !97).

- [x] **infra-brain-n8n-1 container unhealthy** — resolved via MR !112
  (`fix/n8n-healthcheck-localhost`), verified live 2026-07-08.

---

## Open Work Items

### Freshness / Collection Health Reporting

- [ ] **Collectors stale beyond freshness threshold (misdiagnosed as broken)** — root cause
  identified 2026-07-08: `check_freshness()` / `check_collection_health()` in
  `src/infra_brain/callbacks/freshness.py` treat `status='skipped'` (e.g. vsphere, k8s, net,
  cloud, windows — all intentionally unconfigured in this dev environment) as
  indistinguishable from "never ran", reporting them as broken when they are actually
  behaving as designed.

  Fix authored: MR !116 (`fix/freshness-skipped-status`, **closed** — superseded) adds an
  "unconfigured (skipped) — {reason}" alert path using the already-existing
  `CollectionRun.error_message` field. A separate bug in `get_collection_health()` in
  `mcp_server.py` that filtered on `started_at` instead of `finished_at` was also authored
  as MR !117 (`fix/mcp: collection-health filters`, **closed** — superseded).

  Both !116 and !117 were closed and their changes incorporated into MR !124
  (`feat/dashboard2-full-consolidated`), which **merged to master 2026-07-11**. The
  remaining open question is behavioral: whether the health dashboard correctly
  distinguishes unconfigured collectors from failing ones.

### Wave 5 Operational Drills

These acceptance criteria were never exercised for the items marked done in the
remediation wave above. They remain open work.

- [ ] **Redis-stop readiness test** — does the app degrade gracefully when Redis is down?
  Steps: stop the Redis container, exercise all API endpoints that touch the cache layer,
  verify 503/degraded responses (not 500 crashes), restart Redis and confirm recovery.

- [ ] **Stale-pipeline safety review** — the `git reset --hard` in the post-deploy CI job
  runs against a shared ops checkout. This needs a review before it touches a real pipeline
  to confirm there is no risk of clobbering in-flight work or leaving the checkout in a
  dirty state.

- [ ] **Rollback drill** — end-to-end Alembic downgrade against a real DB: run
  `alembic downgrade -1` (or to a named revision), verify the app starts cleanly on the
  older schema, then upgrade back to head and verify again. Confirms that every migration
  has a working `downgrade()` path.

---

## Reference: MR Index (audit-remediation wave, 2026-07-08)

| MR | Branch | Status | Summary |
|----|--------|--------|---------|
| !110 | (multiple scopes) | merged | Chat memory, governance split, InventoryMRAgent wiring, schema-drift full compare |
| !111 | — | merged + verified | Deploy verification |
| !112 | `fix/n8n-healthcheck-localhost` | merged | n8n container healthcheck fix |
| !113 | `feat/ci-migration-parity-chain` | merged | Migration-parity CI exercises real DDL chain |
| !116 | `fix/freshness-skipped-status` | closed (into !124) | Distinguish skipped from broken collectors |
| !117 | `fix/collection-health-finished-at-filter` | closed (into !124) | Health filter: `finished_at` not `started_at` |
| !124 | `feat/dashboard2-full-consolidated` | **merged 2026-07-11** | Dashboard2 consolidation (carries !116/!117 fixes) |
