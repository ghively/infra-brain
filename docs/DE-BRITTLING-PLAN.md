# Infra-Brain De-Brittling Plan

> **Status as of 2026-07-01: most of Wave 1–3 has shipped, and the full-bugcheck
> regressions below were fixed the same day.** This document is left in its original
> all-pending form for historical reference (it's still useful as a root-cause explainer),
> but do NOT treat it as reflecting current code state. See `CHANGELOG.md`'s "De-Brittling
> Waves" entry for the full history. Fixed same-day: the chat safety-chain bypass
> (`chat/agent.py` now wires `build_callbacks()` into the graph invocation config), the
> `list_vulns` total/items pagination mismatch, `create_admin.py`'s reintroduced
> `create_all()`, the unauthenticated `/scan_points` route, and — closing V1/V2 below — a
> `metadata_`-driven fallback topology builder in `graph_maintenance.py` that now emits
> edges for `seed_resource`-originated vsphere data (previously only live-collector data
> produced edges).

*Synthesis of a 5-analyst fragility investigation. Reviewed before any code is written. Bias: simplification and resilience over new features. Propose-only / MR discipline throughout.*

---

## 1. Executive Summary

Infra-brain cascade-breaks on every change because **three structural facts have never been fixed, only patched reactively at the symptom site**:

1. **No layer enforces that what the code declares matches what actually exists.** The ORM model declares columns the migrated DB may not have (`r7_vulnerabilities.resource_id`), the frontend assumes array shapes the API may not return (`{items,total}` wrapper), and the graph code expects field names the seed path never writes. Every gate checks *structure* (revision graph, table names, build idempotency, process liveness) but never *applied state* — so drift passes CI green and only surfaces as a production 500.
2. **God-files couple unrelated domains into one blast radius.** A 4017-LOC `dashboard_api.py` (47 routes, 5 routers, ~50 models), a 1768-LOC `models.py` (93 tables, 8+ domains), and a single 1450-line `renderVals()` that eagerly computes every page mean one bad row, column, or fetch anywhere takes down everything.
3. **Resilience is applied one-off, never systemically.** DB-error guards exist on 2 of 47 routes; an array normalizer exists for 1 of 20 frontend props; `create_all()` masks the very drift migrations are meant to catch. Each incident gets a local bandage, so the *next* unguarded site repeats it.

The fix is not new features — it is **convergence gates** (model↔DB, API↔frontend), **single choke points** (one fetch layer, one error boundary, one schema-builder), and **decomposition** to shrink blast radius.

---

## 2. The Smoking Gun: Model ↔ DB Schema Drift

### Causal chain for the `resource_id` 500

1. The ORM model declares the column: `R7Vulnerability.resource_id` (nullable FK to `resources.id`) — `models.py:1518-1527`.
2. Migration `0018_r7_vuln_solution_resource_ids.py:38-65` that adds it **exists and is reachable from head** (chain `0001 → … → c3d4e5f6a7b8` is intact). So this is an **apply/masking** problem, not a broken chain.
3. On the live DB, migration 0018 **never applied** — either because:
   - `alembic upgrade head` ran against the **wrong/empty DB**. The migrate service (`docker-compose.yml:37-48`) sets `POSTGRES_URL=${DATABASE_URL}` but carries **NO `env_file`** (unlike app/scheduler/mcp). If `DATABASE_URL` is empty at compose-interpolation time, `POSTGRES_URL` is empty and `get_settings()` falls back to the `config.py:55` default `postgresql://infra:infra@localhost:5432/infra_brain` — alembic "upgrades" a throwaway localhost DB, exits 0, and the live volume stays unmigrated. (`alembic/env.py:14-31` documents this exact historical failure.)
4. The seed step then runs `create_tables()` = `Base.metadata.create_all()` (`seed_db.py:137-139` → `session.py:22-23`). **`create_all()` only issues CREATE TABLE for missing tables; it NEVER issues ALTER on an existing table.** `r7_vulnerabilities` already exists without `resource_id`, so create_all does nothing, raises nothing, and **seed exits 0 — the deploy reads "schema fine."**
5. The deploy health gate polls only `/healthz` (the CI pipeline config), which is zero-I/O and always returns `{status:ok}` (`webhooks.py:133-136`). Even `/health` (`webhooks.py:139-162`) only does `SELECT 1` — it never SELECTs a drifted column. **Broken schema deploys green.**
6. Every `SELECT` on `R7Vulnerability` emits `r7_vulnerabilities.resource_id` → Postgres returns "column does not exist" → unhandled exception → **blanket 500**, surfacing only when a dashboard/MCP query hits the table in production.

**`create_all()` is the landmine: it turns "migration didn't apply" into "deploy looks healthy but schema is wrong."**

### The durable fix (two layers: always-apply + always-detect)

**A. Make migrations always reach the live DB and fail loud otherwise.**
- Give the migrate service `env_file: ../.env` (parity with app/scheduler/mcp) so `DATABASE_URL` is always present — `docker-compose.yml:37-48`.
- Remove the `config.py:55` localhost default for `postgres_url`; make it **required** (raise if unset) so an unresolved DSN aborts migrate instead of silently targeting an empty localhost DB.
- **Delete `create_tables()`/`create_all()` from the production seed path** (`seed_db.py:137-139`). Alembic becomes the sole schema authority in prod; keep `create_all` only in test fixtures.

**B. Make drift impossible to ship undetected — a startup assertion + a CI gate.**
- **Startup schema-drift assertion** (the keystone): in the FastAPI lifespan (`main.py:24-35`) or a dedicated entrypoint check, assert `current_rev == ScriptDirectory head` AND inspect `r7_vulnerabilities`/`r7_solutions` for `resource_id` (or run full `alembic compare_metadata`). On drift, log the offending table/column and **exit non-zero** so the health gate goes red. This catches BOTH failure modes (migrate-ran-against-wrong-DB and create_all-masked-drift) regardless of how the DSN resolved — at the exact layer where the 500 originates.
- **CI column-parity gate**: a job that spins up an ephemeral Postgres service, runs `alembic upgrade head`, then runs `compare_metadata` against `Base.metadata` and **fails on ANY difference** (missing column, type mismatch, missing index/FK) — not just missing tables. This replaces/supplements `test_migration.py:31-45`, which checks only table NAMES and **skips in CI** (no Postgres → `_db_reachable()` returns False). This is the single gate that would have caught `resource_id` before it shipped.
- **Deploy gate on applied state**: after `compose up -d`, explicitly assert `infra-brain-migrate-1` exited 0 AND `SELECT version_num FROM alembic_version` equals repo head before declaring success (the CI pipeline config currently only polls the app and dumps migrate logs on failure).

---

## 3. Root-Cause Inventory (grouped, with severity + cascade)

### Deploy / migration path
| # | Root cause | Sev | Cascade it triggers |
|---|---|---|---|
| D1 | `create_all()` in prod seed path masks column drift, reports false success (`seed_db.py:137-139`, `session.py:22-23`) | critical | Drift survives deploy looking green → prod 500 |
| D2 | `alembic upgrade head` can succeed against wrong/empty DB; `DATABASE_URL→POSTGRES_URL` indirection has no `env_file` backstop, falls back to localhost default (`docker-compose.yml:37-48`, `config.py:55`, `env.py:14-31`) | critical | Live DB stays unmigrated while CI/deploy report success |
| D3 | No gate compares model COLUMNS to live DB; only test skips in CI (`test_migration.py:31-45`); alembic-chain-check only walks revision graph (the CI pipeline config) | high | Model can declare a column with no applied migration; every gate passes |
| D4 | Health gate proves liveness not schema; `/healthz` zero-I/O, `/health` only `SELECT 1` (`webhooks.py:133-162`, the CI pipeline config) | high | Schema-broken app deploys green |
| D5 | Deploy never asserts migrate container exit code or applied revision (the CI pipeline config) | medium | Migrate-against-wrong-DB sails through |

### Backend
| # | Root cause | Sev | Cascade |
|---|---|---|---|
| B1 | No global exception handler; DB-error guard on only 2 of 47 routes (`main.py:39-59`; import at `dashboard_api.py:27` used only at L3459, L3671) | critical | One bad row/column 500s the whole endpoint (`list_vulns:719`, `_best_vuln_by_cve:697`, `counts:811`, `fleet:3095`, `software:3308`, `eol:863`, `compliance:1449`, `drift_events:628` all unguarded) |
| B2 | 4017-LOC `dashboard_api.py` (47 routes, 5 routers) + 1768-LOC `models.py` (93 tables, 8+ domains) god-files | high | A CVE-schema change can break the Octopus view's import path; any edit retests whole surface |
| B3 | Cross-domain joins by unconstrained String(128) slug, no FK; load-all-in-Python (`models.py:1518-1614`; 65 `.all()` calls; `list_cves:3653` paginates AFTER full load) | high | Duplicated drift-prone join logic; O(rows) memory; silent dropped rows |
| B4 | CI suite runs on SQLite via `create_all()`, structurally cannot catch model↔Postgres drift (`conftest.py:27-32`, `agents/conftest.py:44-49`, `test_e2e_smoke.py:7,31-37`) | critical | The exact outage class is invisible to tests by construction |
| B5 | Prod = Postgres+JSONB+Alembic; test = SQLite+JSON+create_all; no enforced convergence | high | New model column needs a hand-written ALTER nothing verifies was written/applied |

### Frontend
| # | Root cause | Sev | Cascade |
|---|---|---|---|
| F1 | Monolithic `renderVals()` (`index.html:4265-5713`, ~1450 lines) computes every page eagerly; pills map (`4416-4431`) runs `AUDIT/AGENTS/NOTIFS/EOLREG/INVREC.filter()` unconditionally even on Home (`4429`) | critical | One non-array prop throws → error boundary (`support.js:772`) blanks the ENTIRE dashboard (the reported `this.AUDIT.filter is not a function`) |
| F2 | `load()` assigns fetch results with no array guard: `if(data!=null) this[prop]=data;` (`index.html:2285`); only VULNS normalized (`4275`), other ~19 props undefended | critical | A 200-but-wrong-shape body silently poisons state |
| F3 | dc.html→build→index.html byte-identity fragile; pre-commit regenerates but does NOT `git add` index.html; whitespace mismatch (artifact double-spaced, sources not) | high | Stale artifact committed → CI fails after the fact; hand-edits tempting |
| F4 | Hard CDN dependency on unpkg.com for React/ReactDOM/Babel/openui, loaded redundantly from two paths (`index.html:19-21`, `support.js:1424-1452`) | high | CDN/CSP/proxy block blanks whole UI; documented "React twice → white screen" race |
| F5 | Triplicate frontend trees (`src/infra_brain/dashboard/`, `dashboard/src/`, `dashboard/staging/app/`) synced by hand | medium | Multiplies stale-artifact surface |
| F6 | OpenUI library: 14 components, all placeholder text stubs, on a CDN bundle + React-timing race (`openui/library.js:43-125`) | medium | Dead weight: CDN dep, race, cognitive load, zero value |

### API ↔ Frontend contract
| # | Root cause | Sev | Cascade |
|---|---|---|---|
| C1 | No shared contract; shape restated by hand in 3 unlinked places (Pydantic models, test asserts, 30 .dc.html pages); no OpenAPI consumed, no typed client, zero `*.ts` | critical | Backend shape change passes all backend tests while silently breaking frontend — the structural reason every change cascades |
| C2 | The documented bare-list→`{items,total}` break is STILL LIVE for VULNS: `resources.dc.html:701` inits bare array, `_f('/vulnerabilities','VULNS'):306` assigns the wrapper, consumed as array at `1020/1072/1104/1257/1261`; RESOURCES (`1054`) and CVES (`1622`) migrated, VULNS+AUDIT not | critical | On first 5-min auto-refresh, `this.VULNS.filter` throws → home/vulns/security cards crash |
| C3 | Raw-assign fetch helpers `_f` (`304`) and `_pagedGet` have no shape validation, no safe default; ~36 call sites | high | Any changed payload flows straight into shape-assuming render |
| C4 | Backend field names hard-coded across 30 pages (`d.field_name/d.detected_at/d.drift_type` at `1130-1140`) | high | Routine field rename silently breaks rendering with zero test failure |
| C5 | Only frontend CI job is `design-sync-check` (build idempotency, the CI pipeline config); backend tests pin server side only (`test_dashboard_api.py:114,127`) | high | Green CI actively masks the break vector |

### VMware knowledge-graph wiring
| # | Root cause | Sev | Cascade |
|---|---|---|---|
| V1 | Two edge-builders, neither reads the 1057 seeded resources: live `VsphereAgent._write_inventory` (`vsphere.py:282-369`) hard-gated off when vCenter paused (`183`); `GraphMaintenanceAgent._populate_typed_relationships` (`graph_maintenance.py:348-616`) has zero vsphere branches; seed writes only a Resource row, topology in `metadata_`, no edges (`mcp_server.py:248-301`) | critical | 1057 nodes, 0 vmware topology edges, no path turns metadata into edges |
| V2 | Field-name/location mismatch: seed exposes `metadata.host/cluster/datacenter/datastore_names/network_names`; collector keys off `data.esxi_host/cluster_or_parent/datacenter_name` (`vsphere.py:262,266,273`) | high | Even forcing the collector against the DB wouldn't find seeded fields |
| V3 | `trigger_collection('vsphere')` re-collects from live (paused) vCenter, returns `[]` (`mcp_server.py:232-242`, `vsphere.py:154-156,183`) | high | The obvious operator instinct does nothing for seeded data |
| V4 | No `STORED_ON/ATTACHED_TO/IN_DATACENTER`/resource_pool edge types emitted by anyone; enum lacks them (`relationships.py:58-91`); datastore/network collected as columns never edged (`vsphere.py:565-566,624,694`) | medium | Graph structurally incomplete for vmware even when collector runs |

---

## 4. De-Brittling Roadmap (P0 / P1 / P2)

### P0 — Stop the bleeding (quick wins, defensive defaults, deploy correctness)

**P0-1 — Frontend array-shape guard at the one chokepoint.**
Change `load()`'s `if(data!=null) this[prop]=data;` (`index.html:2285`) to coerce list props: `this[prop]=Array.isArray(data)?data:(data&&Array.isArray(data.items)?data.items:[]);`. Files: `dashboard/src/pages/*` source + rebuilt `index.html`. Effort: quick-win. **Blast radius:** eliminates the entire `this.X.filter is not a function` class for all 20 props at the single point where every list enters state.

**P0-2 — Fix the live VULNS/AUDIT wrapper bug.**
In `resources.dc.html`, either unwrap at fetch (`_f('/vulnerabilities', d=>d.items)`) or read `this.VULNS.items` at the ~6 consumers (`1020/1072/1104/1257/1261`), matching RESOURCES/CVES. Confirm AUDIT (`1074/1271/1274`) never gets the raw `AuditPageOut` wrapper. Files: `dashboard/src/pages/resources.dc.html` + rebuild. Effort: quick-win. **Blast radius:** stops the home/vulns/security crash on first auto-refresh.

**P0-3 — Global FastAPI exception handler + degrade-don't-crash.**
Add to `create_app` (`main.py:39-59`) a handler catching `OperationalError`/`ProgrammingError`/`Exception` that returns a clean 503 (or degraded payload) for that endpoint only, logging the column/table at fault. Files: `main.py`. Effort: quick-win. **Blast radius:** missing column degrades from "endpoint dead / whole-UI 500" to "one endpoint degraded."

**P0-4 — Remove `create_all()` from prod seed path.**
Delete the `create_tables()` call at `seed_db.py:137-139`; alembic becomes sole prod schema authority. Files: `scripts/seed_db.py`. Effort: quick-win. **Blast radius:** a missing column can no longer be silently tolerated by seed.

**P0-5 — Make the migrate service fail loud on a bad DSN.**
Add `env_file: ../.env` to the migrate service (`docker-compose.yml:37-48`); make `postgres_url` required in `config.py:55` (raise if unset). Files: `docker/docker-compose.yml`, `src/infra_brain/config.py`. Effort: quick-win. **Blast radius:** eliminates the silent-localhost-fallback divergent-DB failure.

**P0-6 — Startup schema-drift assertion (keystone).**
In `main.py:24-35` lifespan: assert `current_rev == head` and inspect `r7_vulnerabilities`/`r7_solutions` for `resource_id`; exit non-zero on drift. Files: `src/infra_brain/main.py` (+ small helper). Effort: medium. **Blast radius:** converts silent column drift into a loud, deploy-blocking failure regardless of DSN resolution — catches both D1 and D2.

**P0-7 — CI column-parity gate.**
New CI job: ephemeral Postgres service → `alembic upgrade head` → `compare_metadata` vs `Base.metadata`, fail on any difference. Files: the CI pipeline config, new `tests/test_migration_parity.py` (replaces table-only `test_migration.py:31-45`). Effort: medium. **Blast radius:** model/migration drift becomes a red pipeline, not a prod 500.

### P1 — Structural (decomposition, contract mechanism, build safety, fault isolation)

**P1-1 — Uniform DB-error guard on every DB-backed endpoint via shared decorator.**
Apply the degradation guard to `counts:811`, `list_vulns:719`, `fleet:3095`, `software:3308`, `eol:863`, `compliance:1449`, `drift_events:628`, etc. as a decorator, not copy-paste. Files: `dashboard_api.py`. Effort: medium. **Blast radius:** resilience becomes the default; removes the reactive 2-of-47 inconsistency.

**P1-2 — Deploy gate on actual migration state.**
After `compose up -d`, assert `infra-brain-migrate-1` exited 0 AND `alembic_version.version_num == head` before success (the CI pipeline config). Files: the CI pipeline config. Effort: medium. **Blast radius:** "migration didn't reach live DB" becomes a deploy failure.

**P1-3 — One thin typed fetch layer (frontend contract — see §5).**
Single `apiGet(path,{shape})` choke point handling 401/!ok/network once + runtime shape guard returning safe defaults; route all `_load*`/`_f`/`_pagedGet` through it; tag each endpoint `list|page` in one table. Files: shared dc.html script block + per-page call sites. Effort: medium. **Blast radius:** a future shape change degrades to empty-but-rendering + logged warning, never a white screen.

**P1-4 — Stop eager all-pages render / isolate per page.**
Gate per-page sections of `renderVals()` behind the active page (only build `pills[pg]` for the active page), OR split page bodies into child DC components so the existing per-component error boundary (`support.js:772`) isolates a fault to one panel. Also wrap unguarded pills reads with `arr(x)=>Array.isArray(x)?x:[]` (`index.html:4416-4431`). Files: `index.html` source pages. Effort: large. **Blast radius:** whole-dashboard outage → single-panel error.

**P1-5 — Self-host React/ReactDOM/openui; remove redundant load path.**
Vendor as static assets under `/dashboard`; drop either `index.html:19-21` tags or `support.js:1424-1452 loadReactUmd` (not both); pin versions. Files: `dashboard/static/*`, `index.html`. Effort: medium. **Blast radius:** removes CDN/CSP SPOF + permanently closes the "React twice" race.

**P1-6 — Self-enforcing build pipeline.**
Pre-commit `design-sync-build` auto-stages rebuilt `index.html` (or fails if dirty after build); normalize whitespace/line-endings so sources and artifact share one canonical form (kill double-spacing); keep CI idempotency check as backstop. Files: `.pre-commit-config.yaml`, `scripts/design_sync/build.py`. Effort: quick-win. **Blast radius:** closes the stale-artifact commit gap.

**P1-7 — Decompose the god-files.**
Split `dashboard_api.py` into `api/routers/{vuln,cve,octopus,vsphere,iac,hosts,fleet,governance}.py` (each owns routes + Pydantic schemas); split `models.py` into `db/models/{core,rapid7,octopus,vsphere,k8s_cloud_net,ansible,os_inventory,governance}.py` re-exported from a package `__init__`. Mount routers in `main.py` unchanged. Files: large module split, no behavior change. Effort: large. **Blast radius:** any edit's reach shrinks from "all 5 routers / whole schema" to one domain; CVE-schema change stops touching Octopus import graph.

**P1-8 — Centralize the Rapid7 CVE slug-walk + push selection to SQL.**
One resolver module reused by `list_vulns`/`list_cves`/`get_cve_detail`; max-CVSS-per-CVE via Postgres `DISTINCT ON`/window function instead of load-all-in-Python; add DB indexes (+ referential-integrity check job) over the slug bridges. Files: new resolver module, `dashboard_api.py`. Effort: large. **Blast radius:** removes duplicated drift-prone join logic + the O(rows) memory pattern; one place for the error boundary.

### P2 — Simplification (trim dead weight, model sprawl, convergence)

**P2-1 — Delete the unused OpenUI library + consolidate frontend trees.**
Remove `openui/library.js` (14 text-stub components), the `@openuidev/browser-bundle` CDN script (`index.html:5995`), the `library.js` include (`5997`); collapse the 3 dashboard trees to one served + one build-source, delete the `dashboard/staging` duplicate. Effort: medium. **Blast radius:** cheapest large reduction in fragility surface.

**P2-2 — Endpoint tests against migrated Postgres with sparse/null rows.**
TestClient tests on the CI Postgres service (from P0-7), seeded with deliberately sparse enrichment, asserting list endpoints return 200-degraded not 500. Effort: medium. **Blast radius:** closes the loop SQLite create_all and the static SQL parser cannot.

**P2-3 — Standardize test fixtures to apply alembic (kill create_all/alembic split-brain).**
Replace the 27 independent `Base.metadata.create_all` fixtures with a shared migration-applying builder so tests exercise the deploy schema path. Effort: large. **Blast radius:** tests stop validating models against themselves.

**P2-4 — Uniform pagination contract.**
Every list endpoint returns `{items,total,limit,offset}` (no bare lists); migrate remaining bare-list endpoints (`drift_events`, `eol`, `activity`) behind the fetch layer so the frontend unwraps exactly one shape. Effort: large. **Blast radius:** removes the list-vs-wrapper ambiguity that is the root of the recurring break.

---

## 5. API ↔ Frontend Contract — the low-ceremony mechanism

Do **not** introduce a typed-client/TS build. Three additive layers, in priority order:

1. **One frontend fetch choke point (P1-3).** A single `apiGet(path,{shape})` that does fetch + 401/!ok/network handling once, then a runtime shape guard: for `page` endpoints guarantee `{items:[],total:0,…}`, for `list` endpoints guarantee an array, returning a safe default and `console.error` on mismatch. Route all `_load*`/`_f`/`_pagedGet` through it. This is the single thing the codebase lacks — resilience becomes addable in one place instead of ~36 call sites.

2. **One shared endpoint→shape map, consumed by BOTH sides.** A small constant (`{path: 'list'|'page'}`) imported by the frontend fetch layer for its list-vs-page tagging AND by a single pytest that hits each dashboard endpoint via TestClient (reusing the `test_dashboard_api.py` harness) and asserts the top-level kind. One source of truth; drift becomes a red CI build, not a prod crash.

3. **OpenAPI snapshot diff in CI.** FastAPI already emits a complete OpenAPI doc for free from the `response_model`s — currently unused. Snapshot it as a CI artifact and fail the build on an unreviewed diff. This catches every field rename/shape change at review time, killing the hard-coded-field-name fragility (C4) without a typed-client rewrite.

This trio converts C1–C5 from "invisible until the browser crashes" to "caught at CI review."

---

## 6. VMware Knowledge-Graph Wiring (1057 seeded resources)

**Critical framing — flagged code-needed vs trigger-only:** `trigger_collection('vsphere')` is **trigger-only and WRONG here** — it re-collects from the paused live vCenter (`mcp_server.py:232-242` → `vsphere.py:154-156,183`) and produces zero edges for seeded data. The seeded snapshot and the live-collector edge logic are two ships passing in the night. **New code is required.**

**Step 1 (code) — Add a metadata-driven builder to GraphMaintenance.**
New `_populate_vsphere_topology` called from `_populate_typed_relationships` (`graph_maintenance.py:348`). Read straight from the DB: load all `Resource` rows where `domain='vsphere'` into name→id maps per type (vm/esxi_host/cluster/datastore/network/datacenter); for each VM read `metadata_.host/cluster/datastore_names/network_names` and emit via the idempotent `emit_edges_batch` (`relationships.py:243-278`). This derives edges from durable `Resource.metadata_`, independent of any live vCenter — so it works against the paused/seeded state today. GraphMaintenance already runs `0 */2` (`scheduler.py:52`) and builds the other 4 typed edge families the same way.

**Step 2 (code) — Define the edge set + field mapping (resolve by `Resource.name` within `domain='vsphere'`):**
- VM `RUNS_ON` esxi_host ← `metadata.host`
- esxi_host `MEMBER_OF` cluster ← host `metadata.cluster`
- cluster `MEMBER_OF`/`IN_DATACENTER` datacenter ← cluster `metadata.datacenter`
- esxi_host `MEMBER_OF` datacenter ← host `metadata.datacenter`
- VM `STORED_ON` datastore ← `metadata.datastore_names[]` *(NEW enum member)*
- VM `ATTACHED_TO` network ← `metadata.network_names[]` *(NEW enum member)*

Add `STORED_ON` and `ATTACHED_TO` to `RelationshipType` (`relationships.py:58-91`); reuse `MEMBER_OF` for cluster→datacenter or add `IN_DATACENTER` for clarity. These two edge types are genuinely missing from the taxonomy — the live collector never emitted them either (datastore/network sit in columns at `vsphere.py:565-566,624,694`).

**Step 3 (trigger, after merge) — Populate the graph.**
Invoke `GraphMaintenanceAgent().collect(scope='all')` via `dispatch('graph_maintenance', scope='all')` (`supervisor.py:101-102`) — **NOT** `trigger_collection('vsphere')`. Verify with `GET /api/graph/stats` (`graph_api.py:174`) showing non-zero `RUNS_ON/MEMBER_OF/STORED_ON/ATTACHED_TO`.

**Step 4 (code, P1) — Unify the metadata→edge resolver.**
Refactor `VsphereAgent._write_inventory` topology (`vsphere.py:282-362`) to write the same `metadata_` keys the seed path uses, then route BOTH live and seeded data through the single GraphMaintenance builder; remove the duplicate in-memory topology emission. Collapses 3 field vocabularies + 2 builders into one — so the next ingestion path can't desync edges again.

**Step 5 (code, P2) — Observability.**
Have `_populate_typed_relationships` (already returns per-type counts, `graph_maintenance.py:355`) assert vsphere edge count > 0 when `domain='vsphere'` resource count > 0, surfaced via `get_collection_health`. Turns "seeded but unwired" from an INFO-log zero into a visible health signal.

---

## 7. Sequencing

**Constraints:** workspace serialization (one writer at a time), propose-only (push, never merge), every change through git→push→pipeline (no live edits), design-sync rebuild before any index.html commit, infra-brain backend has no local clone (materialize via `~/.infra-ops/workspaces/agents-infra-brain`).

### Wave 1 — Deploy correctness + stop-the-bleeding (one MR, backend)
Batch the deploy-path P0s that are all small and tightly coupled: **P0-3, P0-4, P0-5, P0-6, P0-7**. These belong in one MR because the startup assertion (P0-6), removing create_all (P0-4), and the CI parity gate (P0-7) only make sense together — the assertion + gate are what make removing create_all safe. P1-2 (deploy-state gate) can ride along since it edits the same the CI pipeline config.
*Self-check: this is the MR that fixes the actual `resource_id` 500.*

### Wave 2 — Frontend crash fixes (one MR, frontend; serialized after Wave 1)
Batch **P0-1, P0-2** (and the pills `arr()` wrap from P1-4's quick subset). All touch `dashboard/src/pages/*` and require a single design-sync rebuild + staged `index.html`. Pair with **P1-6** (self-enforcing build) so the rebuild discipline lands with the change that exercises it.
*Cannot batch with Wave 1: different repo subtree + frontend build artifact; keep diffs reviewable.*

### Wave 3 — VMware graph wiring (one MR, backend)
**§6 Steps 1+2** in one MR (builder + enum members are inseparable). Step 3 is a **post-merge trigger action**, not code. Defer Steps 4–5 to a later MR (they're P1/P2 refactor + observability).

### Wave 4 — Contract mechanism (one MR, full-stack but additive)
**P1-3 + §5 layers 2–3 (P2-2 partial).** The fetch layer, shared endpoint-shape map, and OpenAPI snapshot are additive and reinforce each other; ship together so the map has both consumers from day one.

### Wave 5+ — Structural decomposition (separate large MRs, one domain each)
**P1-7** (god-file split) and **P1-8** (slug-walk resolver) are large pure-refactors — keep each in its own MR, ideally one domain split per MR to stay reviewable. **P1-1** (uniform decorator) lands cleanly after/with P1-7. **P1-5** (self-host React) is independent and can slot anywhere after Wave 2.

### Wave 6 — Simplification cleanup (low-risk MRs)
**P2-1** (delete OpenUI + consolidate trees), **P2-3** (test fixtures → alembic), **P2-4** (uniform pagination). Sequence P2-4 after Wave 4 so the fetch layer already unwraps one shape.

**Rule of thumb for batching:** group by repo subtree + whether they share a gate/artifact (CI yaml, design-sync rebuild, an enum). Never batch a frontend-build MR with a backend MR. Each large refactor (P1-7, P1-8, P2-3) is its own MR.
