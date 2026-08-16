# Collector Refactor Round 2 — Implementation Plan (2026-07-15)

Follows the KG-convergence tranche (MR !159). Three independent workstreams,
each its own feature branch + MR. Ordered by ascending risk/scope so the cheap,
self-contained wins land first and the large consolidation lands last.

Current-state audit grounding this plan was gathered by three read-only
`infra-researcher` passes (vuln, iac/cicd, netdiscovery) on 2026-07-15.

Branch base: `master` (do **not** branch off `feat/kg-convergence-nodes-2026-07-15`;
that MR is still open).

---

## Workstream 1 — `vuln` → `rapid7` (rename + data migration)

**Scope:** a domain/agent rename layered on an already-Rapid7-native stack. The
tools (`tools/rapid7.py`), the entire model layer (`db/models/rapid7.py`, all
`R7*`/`r7_*`), and config (`rapid7_*` settings) are *already* Rapid7-branded.
Only four things still say "vuln": the agent module/class, `AgentSpec.domain`,
the `AGENT_REGISTRY` key, and ~3,860 `Resource.domain` rows.

**Risk:** low. No new tables. One forward-only data migration. The only subtlety
is that two internal call sites hard-code the domain string literal.

**Steps:**
1. Rename `agents/vuln.py` → `agents/rapid7.py`, class `VulnAgent` → `Rapid7Agent`
   (keep a thin `VulnAgent = Rapid7Agent` alias only if any import needs it —
   check first; prefer a clean rename).
2. `AgentSpec(domain="vuln")` → `domain="rapid7"` (`vuln.py:86`).
3. Fix the two hard-coded literals: `.filter_by(domain="vuln", ...)` at
   `vuln.py:309` and `:640` → derive from `self.domain` (don't re-hard-code
   `"rapid7"`).
4. `supervisor.py:82` — `AGENT_REGISTRY` key `"vuln"` → `"rapid7"`, module/class
   path updated. Schedule + sweep membership propagate automatically (derived
   from registry/`AgentSpec`).
5. Update downstream domain-string readers that filter `domain=="vuln"`:
   `fleet_health.py:143`, `governance_ops.py:177,259,538`,
   `governance_drift.py:124`, and comments in `eol.py:16` / `graph_maintenance.py:1592`.
6. **Data migration** (new head, template = `0024_zone_canonicalize.py`):
   forward-only, inspector-guarded `UPDATE resources SET domain='rapid7' WHERE
   domain='vuln'`. First grep `db/models/core.py`/`governance.py` for other
   `domain` columns (`collection_runs.domain`, `agent_action_log.domain`) and
   include them in the same migration. `downgrade()` reverts nothing (one-way,
   per the 0024 precedent). Generate via `/migration-create`, not hand-written.
7. Rename `tests/agents/test_vuln.py` → `test_rapid7.py`; update the ~39
   `domain="vuln"` assertions/fixtures.
8. Update `AGENTS.md` roster row (regenerated from `AgentSpec`).
9. Dashboard `index.html` has ~20 `domain:'vuln'` **mock/fixture** literals —
   cosmetic; update in the same MR for consistency but they're not live queries.

**Verification:** full pytest; `alembic check`; CI migration-parity job; confirm
the sweep graph still lists `rapid7` and drops `vuln`.

---

## Workstream 2 — netdiscovery **service-level** IS_SAME_AS (new)

**Scope correction:** host-level IS_SAME_AS is **already shipped** (4cd0dd7 —
hostname path + guarded unique-IP path; `net` already in `_SOURCE_KEYS`). This
workstream is the genuinely-new **service-level** identity linking: matching a
`discovered_service` (`ip:port/proto`) to the same logical service seen by
another source.

**Risk:** medium — new matching logic, new false-merge surface. `HostReconcileAgent`
must remain the **sole writer** of IS_SAME_AS (enforced by
`test_is_same_as_single_writer.py`).

**Decision (2026-07-15):** match target is **Rapid7** — link `discovered_service`
rows to Rapid7-scanned services on the same reconciled host. **Prerequisite:**
first verify `db/models/rapid7.py` actually stores port-level listening services
(R7Software tracks installed *products*, which may not be the same thing). If no
port-level service record exists in the R7 model, fall back to internal-only
dedup and raise the gap with the maintainer before inventing a schema.
Match key: `(merged_host_resource_id, port, proto)` — only link services whose
*host* is already reconciled, so we inherit the host-merge's false-positive
guards rather than inventing a new one.

**Steps (pending the match-target decision):**
1. Add a service-source entry to the reconciliation model analogous to
   `_SOURCE_KEYS`, scoped to services whose host already merged.
2. Extend `_emit_is_same_as_edges` (or a sibling method) to emit directed
   service↔service edges at a confidence that inherits the host edge's
   `match_basis` (hostname=0.95 / ip=0.75) — never higher than the host link
   that anchors it.
3. Guard: emit **only** when the anchoring host has exactly one merged identity
   (reuse the 4cd0dd7 unique-match philosophy) to avoid the TRK-087 false-merge
   class at service granularity.
4. Tests: extend `test_is_same_as_single_writer.py` (still sole writer) and add
   service-merge cases to `test_host_reconcile.py` — success, no-match, ambiguous-host-skip.
5. No migration expected (edges use the existing relationship tables).

**Note on `net_discovery_svcs`:** phantom identifier — does not exist. If a real
services-table symptom surfaces later, triage it as a separate bug via
`systematic-debugging`; it is **not** part of this workstream.

---

## Workstream 3 — `iac` + `cicd` → single `gitlab` domain + 5 new entities

**Scope:** the large one. Both agents already share GitLab transport
(`tools/gitlab.py`) and a model bucket (`db/models/ansible.py`). Consolidate into
one `gitlab` collector and add the 5 net-new entity types.

**Existing (4/9):** projects (`gitlab_projects`), IaC files (`iac_files` + 5 parse
children), pipelines (`ci_pipeline_runs`), pipeline schedules (`ci_schedules`).
**Net-new (5/9):** merge requests, releases, environments, members, CI variables —
each needs a model + migration.

**Risk:** high. Domain merge + 5 new tables + agent consolidation. Recommend
splitting into **two MRs** (3a then 3b) to keep review tractable.

### 3a — Consolidate iac+cicd into one `gitlab` domain (no new entities yet)
1. New `agents/gitlab.py` / `GitlabAgent` (`domain="gitlab"`, COLLECTOR) that
   subsumes both `collect()` flows. Retire `cicd.py`/`iac.py` (or keep as
   internal modules the new agent composes).
2. `supervisor.py` — replace the two registry keys with one `"gitlab"`.
   Update `graph.py:130` retry-policy set and `rootcause.py:57`
   `_CORRELATE_DOMAINS` (currently `("cicd","octopus")`).
3. `webhooks.py:189` — `_trigger_domain("cicd",...)` → `"gitlab"`.
4. **Name-collision guard (flagged by research):** cicd names resources by
   project, iac by file path — currently disjoint. Verify no collision once both
   live under `domain="gitlab"`; adapt the `_upsert_resource`/`_retype_existing_by_name`
   dedupe (`iac.py:424-432,922-936`) to the merged namespace.
5. **KG emitters are safe** — RUNS_IMAGE and the project/pipeline edges anchor on
   `IacFile.resource_id`/`GitlabProject.resource_id`, not the domain string
   (confirmed). Just ensure the merged agent still creates a `Resource` per file
   and populates `resource_id`.
6. Data migration (template 0024): `UPDATE resources SET domain='gitlab' WHERE
   domain IN ('iac','cicd')`.
7. Tests: merge `test_iac.py`/`test_cicd.py`/`test_iac_retype.py`; update the
   cross-cutting tests listed in the research (sweep-graph, collection-health,
   convergence-nodes, webhooks, fleet-health, etc.).
8. Dashboard: fold the `@page:iac` block + nav into a `gitlab` page.

### 3b — Add the 5 net-new entity types
For each of merge requests / releases / environments / members / CI variables:
1. New model + table in `db/models/ansible.py` (or a new `gitlab.py` model module),
   keyed on `(gitlab_project_id, <natural id>)`, with `resource_id` FK.
2. GitLab API fetch (endpoints already reachable via `gitlab_get_paginated`;
   add `@tool` wrappers in `tools/gitlab.py` where missing).
3. Generated Alembic migration per batch (`/migration-create`).
4. Optional KG edges (e.g. MR→project, release→project) — additive, no migration.
5. Tests per entity: success, empty, exception.

**Per-entity depth/scope decisions (2026-07-15):**
- **Merge requests + pipelines:** **recent window** — open MRs plus recently-merged
  (last 30–90 days; tune to the collector timeout budget), last N pipelines
  (keep the current last-5 behavior). Bounded API cost, avoids the 600s timeout.
- **CI/CD variables:** **metadata only, NEVER values.** Store `key`, `scope`,
  `environment_scope`, `masked`, `protected` flags only. Do not fetch or persist
  the variable value — even GitLab-"masked" values can leak, and the DLP callback
  would flag them. This is a hard read-only/DLP constraint, not a preference.
- **Members:** **direct + group-inherited**, each with access level, for a complete
  effective-access picture.
- **Releases / environments:** current state (all releases, all environments per
  project) — low cardinality, no windowing needed.

---

## Sequencing & MRs

| Order | Workstream | MR | Migration? | Rationale |
|---|---|---|---|---|
| 1 | vuln → rapid7 | 1 | data-only | lowest risk, self-contained, unblocks nothing else |
| 2 | netdiscovery service IS_SAME_AS | 1 | none | self-contained; pending match-target decision |
| 3a | gitlab consolidation | 1 | data-only | high risk; land the merge before adding entities |
| 3b | gitlab 5 new entities | 1 (or 5) | schema | largest; builds on 3a |

All work branches off `master`. Each MR runs the CI contract + migration-parity
gates. `/deploy-check` before any release.

## Decisions — all settled (2026-07-15)
- **WS2 match target:** Rapid7 (verify R7 port-level service data exists first;
  else fall back to internal-only dedup and flag the gap).
- **WS3b MR/pipeline depth:** recent window (open + last 30–90 days; last-5 pipelines).
- **WS3b CI variables:** metadata only, never values (hard DLP constraint).
- **WS3b members:** direct + group-inherited, with access level.
- **WS3b releases/environments:** full current state.

No open decisions remain. WS1 is fully specified and unblocked; WS2 has one
prerequisite verification (R7 port-service model) that is part of its first step;
WS3 is fully specified.
