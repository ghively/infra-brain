# ADR 2026-07-29 — Prioritized Implementation Plan (post-audit synthesis)

**Status:** PROPOSED — planning deliverable only; no code changed by this document.
**Author:** synthesis agent, 2026-07-29 (branch `wt/2026-07-29-implementation-plan/T1-synthesis`).
**Inputs:** `docs/TRACKER.md` (consolidated through TRK-259, the 2026-07-28/29 full-system-audit
batch TRK-247..259), `docs/HANDOFF.md` (2026-07-28 day close), and direct code verification of
`src/infra_brain/mcp_server.py`, `src/infra_brain/mcp_auth.py`, and
`src/infra_brain/api/routers/mcp_keys.py` at master `8bc0ab1`.

---

## 1. Context and settled premises (do not re-litigate)

1. **The MCP direct-invocation audit gap (TRK-247) is not structurally fixable.**
   `@mcp.tool`-decorated functions are plain Python callables; any in-process caller
   (`docker exec … python -c`) bypasses both the ASGI `_ApiKeyAuthMiddleware` and
   `McpAuditMiddleware` because both are HTTP-request-scoped. This is a framework property,
   not a codebase bug. The 928 backfilled root-cause notes attributed to
   `mcp:unauthenticated` with zero `agent_action_log` rows are the concrete evidence.
   **The mitigation strategy is settled:** (a) visibility — make direct-invocation-shaped
   writes findable after the fact; (b) a legitimate bulk path so operators never need direct
   invocation; (c) documentation of the boundary (partially done in
   `skills/infra-brain-mcp-operations/SKILL.md`, "Operating the system vs. building it").
   This plan sequences the mitigations. It does not revisit the verdict.

2. **Code-verified gaps** (all confirmed absent at `8bc0ab1`, so nothing below proposes a
   tool that already exists):
   - No read tool lists `manual_mcp`-provenance writes. The provenance markers exist
     (`RootCauseNote.correlated["source"] == "manual_mcp"`,
     `ProposedAction.agent == "manual_mcp"`) but nothing queries by them.
   - `get_drift_events` (`mcp_server.py:524`) has `status/hours/domain/limit/
     include_graph_maintenance` params only — no way to filter to events lacking a
     `RootCauseNote`.
   - The only note-writing path is single-row `record_rootcause_note` (`mcp_server.py:3399`),
     one call per event.
   - `api/routers/mcp_keys.py` exposes exactly `GET /tools`, `GET ""`, `POST ""`,
     `POST /{key_id}/revoke`. Amending an existing key's `allowed_tools` requires minting a
     new key and revoking the old one.

3. **The tool-name catalog in `mcp_auth.py` is load-bearing for enforcement** (TRK-231
   lesson): a tool registered in `mcp_server.py` but absent from
   `READONLY_TOOL_NAMES`/`MUTATION_TOOL_NAMES` is unreachable by *every* key. Every new tool
   below must land in both places in the same commit;
   `tests/test_mcp_auth_helpers.py::test_catalog_matches_registered_mcp_tools` enforces this.

---

## 2. Blocked, not sequenced (operator/external-gated — surface, don't plan through)

These are deliberately **excluded** from the phased build order. None can be unblocked by an
agent; each has a named external dependency.

| Item | What it is | Blocked on |
|---|---|---|
| TRK-135 / TRK-008 residual | Alert delivery: `OPS_WEBHOOK_URL`/`OPS_WEBHOOK_TOKEN` unset — P0/CRITICAL; every silent failure in TRK-248/249/253/254 compounds because of this | the maintainer provisions a sink (ntfy/Slack/Teams/Gotify) + Bitwarden entry |
| TRK-077 smoke run; `rootcause_llm_enabled` / `compliance_gap_finder_enabled` / `remediation_interrupt_enabled` validation; all model-quality LLM work | Reasoner-tier real-model validation | AWS Bedrock keys (standing hold, see memory `bedrock-access-blocks-llm-work`) |
| RAG/Confluence ingestion smoke test | Code-ready; `RAG_ENABLED` already live-configured | `CONFLUENCE_URL`/`TOKEN`/`USER_EMAIL` hand-copied to Bitwarden + `INFRA_BRAIN_ENV` by the maintainer (sandbox classifier blocks automated `.env` reads) |
| TRK-099, and downstream TRK-043 (Windows creds), IN_VLAN work, the fleet-unreachable half of TRK-254 | Fleet SSH/WinRM connectivity, `fleet-ansible` inventory/DNS | Provisioned service-account key + DNS/inventory work outside this repo |
| TRK-249 | `cicd` 403 spread to 5 projects — token scope/expiry/role inspection | the maintainer checks the live GitLab token (operator credential action; only becomes code work if the token proves healthy) |
| TRK-146 ops half / TRK-248 timeout half | Ollama hosting capacity (600s LLM timeouts) | the maintainer's call on a dedicated Ollama instance (GitLab #40) |
| TRK-149 residual | Scheduler memory trend | Needs multiple days of uninterrupted uptime — a calendar, not a task |
| TRK-145 | Instinct-sync wiring decision | Parked behind the possible GitLab-instance migration question |
| GitLab #132-134 | One-time seed candidates | Explicitly held for a mutation-capable MCP key + operator go-ahead |
| `fix/6.1-mig-b6-flip` remote branch | Delete/keep decision | the maintainer's confirmation (do not delete unconfirmed) |
| TRK-259 | Root-disk trend (89%/28GB) | Standing watch item by its own row's decision — escalate at ~95% or if TRK-246 Phase 1 lands on this host; no work now |

**Needs-a-decision-from-the maintainer before code (sequenced as decision items, with build work staged
behind them):** TRK-247 direction (runtime guard yes/no), TRK-255 (what "review" means at
20k pending proposals), TRK-256 (drift-event lifecycle: permanent log vs. real omission),
TRK-246 (llm-stack integration host choice + 3 other open risks).

---

## 3. Prioritized build order

Ordering rationale: (1) visibility first — the audit's through-line is that every serious
finding (TRK-248, 253, 254, 257) stayed invisible because delivery is dead and there is no
read path over manual writes; (2) small, fully-scoped fixes next; (3) investigations that
convert unknowns into scoped work; (4) decision-gated builds; (5) roadmap.

### Phase 0 — immediate, zero/near-zero risk (start now, parallelizable)

| # | Item | TRK | Shape |
|---|---|---|---|
| 0.1 | `ANALYZE proposed_actions` on live DB (one-off ops action; prerequisite for any TRK-255 query work — planner stats are 11x stale) | TRK-258(2) | ops, no code |
| 0.2 | MCP container healthcheck: stop probing auth-gated `GET /mcp` (972+ junk 401s masking real 401s like TRK-250's) — point at an unauthenticated liveness path or add one | TRK-258(1) | compose/one-file |
| 0.3 | `compliance` runtime follow-up observation: next scheduled run back in 24-27s band → close as cold-start; still ~360s → promote to a real investigation | TRK-252 | observation only |
| 0.4 | TRK-247 mitigation (c) completion: extend the operate-vs-build boundary doc with the explicit rule "direct in-process invocation is unaudited and unattributed; use the HTTP path or the Phase-2 bulk tool", cross-referenced from `docs/READONLY-MODEL.md` | TRK-247(c) | docs only |

### Phase 1 — small scoped code fixes (independent, worktree-isolated, parallel)

| # | Item | TRK | Notes / review tier |
|---|---|---|---|
| 1.1 | EOL vmware-esxi 301: fix URL (preferred) or enable redirect-following at the call site. **Touches `tools/http_readonly.py` territory — a structural read-only layer — so even a one-line fix takes the `lc-safety-reviewer` path** and must not weaken the GET-only guarantee. Do not suppress the error without restoring the data | TRK-257 | safety review; Sonnet build, Fable/Opus review |
| 1.2 | `discovery` structured-output conversion: `DiscoveryAgent` free-text-parse → structured output following `rootcause.py`'s shape. Fixes the parse-failure half only; the 600s-timeout half stays blocked (Ollama capacity, §2). Must keep flowing through `build_callbacks()` | TRK-248 | agent change + tests (constraint #6); does not touch Critical-Files paths |
| 1.3 | `coverage` erratic-runs triage: classify the 5 failure modes (real-empty vs. swallowed error — the TRK-232 shape — vs. TRK-146 LLM-reachability). Investigation only; resist scoping code first, per the row itself | TRK-253 | Fable-tier investigation |
| 1.4 | `remediation` timeout headroom: add per-phase timing inside the run to discriminate contention vs. legitimately-grown work, then (and only then) the one-line `collect_timeout_seconds` decision. **If the config value moves, that edit lands in `config.py` — Critical Files: full pytest after** | TRK-251 | investigate → possible config change |
| 1.5 | `linux`/`windows` 29-day-data investigation, bounded: determine whether current runs error per-host while reporting aggregate success (fixable here) or genuinely can't reach the fleet (→ TRK-099, blocked, stop). Deliverable is the determination, not a fix | TRK-254 | Fable-tier; may terminate at "blocked" |
| 1.6 | infra-ops MCP client checks: issue a fresh scoped key, confirm 401→200; confirm client sends `Accept: application/json, text/event-stream`. Likely an infra-ops-side fix; close only on one demonstrated end-to-end tool call | TRK-250 | mostly outside this repo; cheap |

### Phase 2 — MCP visibility + bulk-path batch (the TRK-247 mitigation core)

Four additions, blast-radius analysis in §4. 2.1 and 2.2 are pure reads and can ship
first/together; 2.3 is the mutation and should follow 2.1 (so its output is immediately
inspectable); 2.4 is dashboard-API-only and independent.

| # | Item | Mitigates |
|---|---|---|
| 2.1 | `get_manual_writes()` read-only MCP tool | TRK-247(a)-visibility — highest-value single gap |
| 2.2 | `has_note: Optional[bool]` filter on `get_drift_events` | Manual-reasoning workflow (find un-noted events without pulling everything); also serves TRK-256 reading (a) |
| 2.3 | `record_rootcause_notes_bulk()` mutation tool | TRK-247(b) — the legitimate bulk path that removes the reason to reach for `docker exec` |
| 2.4 | `PATCH /api/dashboard/mcp-keys/{key_id}` — amend an existing key's `allowed_tools` | Key-scope lifecycle (today: mint-new + revoke is the only path); reduces long-lived over-scoped keys |

Every tool in 2.1-2.3 must be added to the `mcp_auth.py` catalog in the same commit
(§1.3). The catalog is not in the Critical Files table but is enforcement-load-bearing;
treat it with the same care and rely on the existing parity test.

### Phase 3 — decision-gated builds (dispatch the decisions to the maintainer now; build after)

| # | Item | TRK | Gate |
|---|---|---|---|
| 3.1 | TRK-247 direction (b)-detection: a runtime guard in the mutating tool bodies that notices an absent HTTP request context and, at minimum, logs loudly + stamps the write (e.g. `authored_by="direct:unattributed"`). Complementary to Phase 2, not replaced by it | TRK-247 | the maintainer picks (a)/(b)/both; (a) is Phase 2.3 regardless |
| 3.2 | Proposed-actions review at scale: rollup/dedup (18,408 config_fix rows → distinct recommendations), auto-expiry, and/or class+confidence bulk review. **Do not build a bulk-approve button before the decision** — a wrong-grouping bulk mutation over 20k rows is exactly what the write-gate discipline exists to prevent. Run 0.1 (`ANALYZE`) before any large query | TRK-255 | the maintainer decides what "review" means |
| 3.3 | Drift-event lifecycle: either document "permanent log, handled = out-of-band note" (then 2.2's `has_note` is the operational surface, and dashboards/`check_collection_health` should say "un-noted" not "open"), or build resolution transitions (re-detection closes events; the TRK-242 ack path already proves the write works) | TRK-256 | the maintainer picks (a) document vs. (b) build |
| 3.4 | llm-stack integration Phases 1-3 per `docs/decisions/2026-07-28-llm-stack-integration-plan.md` — noteworthy because its Phase 3 (LiteLLM local models as a real structured-output smoke path) would partially de-blockade the Bedrock-held reasoner-tier validation | TRK-246 | 4 open risks in that ADR, esp. host choice vs. TRK-259 disk trend |

### Phase 4 — roadmap (Tier 4, after the above or when parallel capacity exists)

- **roadmap-agents #94-105** (12 issues, 10 with full agent-design plans; #104/#105
  scoping-only). Each new agent: `/agent-scaffold` or `/agent-register`, test file with
  success/empty/exception cases (constraint #6), wiring through `callbacks/registry.py`,
  and **`supervisor.py` + `scheduler.py` registration — `supervisor.py` is Critical Files:
  routing tests required, `lc-safety-reviewer` + `lc-agent-completeness` on every one.**
- **roadmap-automation #108** (last one standing in its batch).
- **roadmap-integrations #111/#112/#114/#118** (#115/#117 scoping-only; #117 explicitly
  "do not build without a real need" — honor that).
- **#125-131** unplanned batch — #129 ("turn on dormant collectors") should be triaged
  against the `k8s`/`net`/`cloud` 13-day staleness noted in HANDOFF before any build.
- **TRK-258(3)** `resources` stale-duplicate retirement pass — scope deliberately, not as a
  side effect.

Not re-sequenced from history: long-tail partially-fixed rows (TRK-030's dead emitter at
`graph_maintenance.py:1255`, TRK-102's ghost-node sliver, TRK-104's schema-blocked edges,
TRK-038's runner/host separation) remain valid backlog but none outranks the phases above;
pull them in opportunistically when touching adjacent code.

---

## 4. Blast-radius and scoping analysis — Phase 2 additions

Common properties of all three MCP tools (2.1-2.3): they are FastMCP tools, not LLM-agent
tools — they do **not** flow through `build_callbacks()`/`ReadOnlyToolValidator` (that chain
gates LLM tool-calls inside agents). Their safety chain is: `_ApiKeyAuthMiddleware`
(per-key `allowed_tools` 403) → `McpAuditMiddleware` (per-call audit row) → in-body gates
(`_mutations_enabled()` for mutations, size caps, `redact_pans`, server-derived attribution
via `_caller_identity()`). All writes stay inside infra-brain's own Postgres — no path to
GitLab/Jira/Confluence, no managed-infrastructure reach, so the three-layer read-only model
over *infrastructure* is untouched by every item below.

### 4.1 `get_manual_writes()` — read-only

- **Reads:** `root_cause_notes` where `correlated->>'source' = 'manual_mcp'`, and
  `proposed_actions` where `agent = 'manual_mcp'`. Optional params: `kind`
  (`rootcause|compliance_gap|all`), `authored_by` substring, `since` (ISO timestamp),
  `limit` (default 100, hard cap ~500). Nothing else; no joins beyond the ones the existing
  read tools already make.
- **Writes:** none.
- **Safety chain:** ASGI auth (add to `READONLY_TOOL_NAMES`) + audit middleware. No
  `_mutations_enabled()` gate needed.
- **Misuse analysis:** worst case is reading note/proposal content a key was scoped to read
  anyway — the same rows are already reachable via `get_remediation_suggestions`/
  `get_drift_events`-adjacent surfaces one at a time; content was PAN-scrubbed at write
  time. A hostile caller cannot use it to *hide* anything (read-only) or to enumerate
  secrets (the tables carry operator prose + drift metadata, no credentials by
  construction).
- **Why the scope is bounded correctly:** it filters on the server-generated provenance
  markers that `record_rootcause_note`/`record_compliance_gap` write *last* (caller data
  can never mask them), so its result set is exactly "everything that entered via the
  manual path" — including the 928 `mcp:unauthenticated` backfill rows, which is the whole
  point: direct-invocation writes become findable even though the invocation itself was
  never audited. This is the single highest-leverage TRK-247 mitigation because it works
  retroactively.
- **Critical-files exposure:** none (new tool body in `mcp_server.py` + catalog line +
  tests). No schema change, no migration, no `/pg-gate-check` trigger *provided* the
  JSONB `->>` filter is written via SQLAlchemy's portable JSON accessor (sqlite tests) —
  if any raw dialect-specific SQL is used instead, `/pg-gate-check` before push.

### 4.2 `has_note` filter on `get_drift_events` — read-only, additive

- **Change:** one optional param `has_note: Optional[bool] = None`; `False` → anti-join
  (`~exists()` on `RootCauseNote.drift_event_id`), `True` → semi-join. Default `None`
  preserves byte-identical existing behavior for every current caller.
- **Writes:** none. **Schema:** none — `uq_rootcause_drift` already gives a unique index on
  `root_cause_notes.drift_event_id`, so the anti-join is index-backed against the 63,809-row
  `drift_events` table.
- **Misuse analysis:** none beyond existing tool; it strictly narrows an existing result
  set. The one operational risk is *performance* if written as a correlated subquery
  without the index — use `exists()` and keep the existing `limit`.
- **Why bounded correctly:** it composes with the existing `status/hours/domain/limit`
  params rather than adding a new surface; a reasoning client can now ask "open, last 24h,
  un-noted" and get the actual work queue instead of pulling 63k rows and diffing locally.
  Also becomes the operational surface for TRK-256 reading (a) if the maintainer chooses "document,
  don't build".
- **Critical-files exposure:** none.

### 4.3 `record_rootcause_notes_bulk()` — mutation, the (b) path

- **Signature:** `notes: list[NoteItem]` where `NoteItem = {drift_event_id: str,
  explanation: str, author_label?: str, correlated?: dict}` (typed via Pydantic so FastMCP
  publishes a real schema); `dry_run: bool = True`. **Hard cap 100 items per call** —
  an oversized list is refused whole, before any DB work.
- **Behavior:** `dry_run=True` (the default) validates every item — UUID shape, event
  exists, no existing note, free-text/`correlated` size caps — and returns the per-item
  verdict without writing. `dry_run=False` executes with **one savepoint per item**
  (`session.begin_nested()`): item N failing (e.g. a concurrent note landing between
  dry-run and execute) rolls back item N only; the response reports per-item
  `written|skipped|error` so partial success is explicit, never silent. Reuses
  `record_rootcause_note`'s exact validation/marker/banner/scrub code path per item —
  factored into a shared helper, not duplicated — so provenance is identical:
  `correlated["source"]="manual_mcp"`, `authored_by` derived from the authenticated key,
  markers written last, `redact_pans` on all caller text, one-note-per-event enforced by
  `uq_rootcause_drift`.
- **What it can write:** rows in `root_cause_notes` only. It cannot touch `drift_events`
  status, cannot create proposals, cannot reach any external system, and cannot execute
  anything — `RootCauseNote` has no execution path at all (documented invariant in
  `mcp_server.py`).
- **Safety chain:** ASGI auth (add to `MUTATION_TOOL_NAMES` — so a key must be explicitly
  granted it; existing keys gain nothing implicitly) + audit middleware (one audit row per
  HTTP call, carrying item count) + `_mutations_enabled()` env gate, same as every other
  mutating tool.
- **Misuse analysis:** worst case with a granted, mutation-enabled key is 100 junk-but-
  size-capped, PAN-scrubbed notes per call, each irreversibly attributed to that key's
  real name and each surfaced by 4.1 — i.e. vandalism that is bounded, attributable, and
  discoverable, on a table whose rows annotate rather than drive behavior. The
  one-note-per-event constraint prevents overwrite of agent-authored notes. A caller
  cannot forge agent authorship (markers server-generated, written last). Rate: 100
  items/call is deliberately sized so the 928-note backfill becomes ~10 audited calls
  instead of one unaudited `docker exec`.
- **Why bounded correctly:** it is exactly the existing single-note tool times N with the
  N capped, previewed by default, and transactionally isolated per item. It adds no new
  write *kind*, only a new write *cadence* — which is precisely the affordance whose
  absence caused the TRK-247 incident.
- **Critical-files exposure:** none (no schema change, no callbacks/ change). New tests
  required: dry-run default, cap refusal, per-item savepoint isolation, duplicate-note
  skip, attribution, mutation-gate off. Note the MagicMock/`sqlalchemy.inspect()` gotcha —
  use a real in-memory sqlite Session fixture (HANDOFF carried-forward lesson).

### 4.4 `PATCH /api/dashboard/mcp-keys/{key_id}` — dashboard API, not an MCP tool

- **Change:** admin-gated (`Depends(require_admin)`, same as the existing three routes)
  route accepting `{allowed_tools: list[str]}`, validated by the existing
  `allowed_tools_known` field validator in `api/schemas.py` (unknown names 422 — reuse
  `McpKeyCreateBody`'s validator, do not re-implement). Refuses to amend a revoked key.
  Updates `McpApiKey.allowed_tools` in place; token hash untouched (no re-issuance).
- **What it can write:** one JSONB column on one `mcp_api_keys` row. It cannot mint
  tokens, cannot un-revoke, cannot touch any other table.
- **Misuse analysis:** the real risk is **scope escalation** — an admin (or a stolen admin
  session) widening a long-lived key from read-only to mutation tools without the
  visibility that minting a new key provides today. Mitigations, in scope for v1:
  (i) admin auth is already session-cookie + Redis-revocation backed (TRK-095/096
  hardening); (ii) log the amendment at WARNING with key id/name, before/after tool
  counts, and the acting dashboard user; (iii) the response echoes the full new scope so
  the UI shows exactly what was granted. **Deliberately deferred:** a persisted
  amendment-history column — that is a `db/models/` schema change (Critical Files:
  migration + `/pg-gate-check` + `lc-migration-reviewer`) and should only be added if
  the maintainer wants durable scope-change audit beyond logs; ship v1 without it and note the
  trade-off in the MR.
- **Why bounded correctly:** it manipulates authorization *metadata*, never
  authentication material; the blast radius of a bad amendment is capped by the tool
  catalog itself (a key can never be granted a name outside `ALL_TOOL_NAMES`) and by the
  `_mutations_enabled()` env gate sitting behind every mutation tool regardless of key
  scope. Narrowing a key — the common case, and the reason this exists — becomes a
  one-call operation instead of a mint-and-rotate.
- **Review tier:** new FastAPI route → `lc-api-reviewer` (async correctness, auth,
  `response_model`); frontend follow-up on the McpKeys page is optional and separable
  (live-patch-eligible as a small scoped frontend tweak once the route exists).

---

## 5. Critical-Files-table exposure summary (heavier review/model tier per orchestrator rules)

| Planned item | Critical path touched | Required rigor |
|---|---|---|
| 1.1 (TRK-257) | `tools/http_readonly.py` — structural read-only layer (not in the table, but named in READONLY-MODEL layer 1; treat equivalently) | `lc-safety-reviewer`; never weaken the GET-only guard; callback tests |
| 1.4 (TRK-251) | `config.py` if `collect_timeout_seconds` default moves | Full pytest after; Opus-tier if anything beyond the one literal changes |
| 3.2 (TRK-255 build) | Likely `db/models/` (expiry/rollup columns) + possibly `callbacks/write_gate.py`-adjacent review semantics | Migration via `/migration-create`, `/pg-gate-check` before push, `lc-migration-reviewer` + `lc-safety-reviewer`; Opus-tier implementation |
| 3.3 (TRK-256 build path) | `graph.py`/drift-detector surroundings if re-detection closes events | Tests in `tests/test_sweep_graph.py` territory; Opus-tier |
| 4.4 deferred history column | `db/models/` | Only if the maintainer asks; full migration discipline |
| Phase 4 agents | `supervisor.py` (every registration), `callbacks/registry.py` wiring | `lc-safety-reviewer` + `lc-agent-completeness` per agent; routing tests |
| Phase 2.1-2.3 | none — but `mcp_auth.py` catalog is enforcement-load-bearing (TRK-231) | Same-commit catalog update; parity test must pass |

Everything in Phases 0-2 not listed above touches no Critical-Files path and routes as
Sonnet-tier scoped implementation with standard review.

---

## 6. Dependency graph / what starts when

```
NOW (parallel):  0.1  0.2  0.3  0.4  1.1  1.2  1.3  1.4  1.5  1.6  2.1  2.2  2.4
THEN:            2.3  (after 2.1 merges — bulk writes should be inspectable on day one)
ON THE MAINTAINER: 3.1 (TRK-247 dir)   3.2 (TRK-255)   3.3 (TRK-256)   3.4 (TRK-246 risks)
                 └─ decisions can be requested immediately; builds staged behind them
AFTER/PARALLEL:  Phase 4 roadmap batches (independent of everything above)
BLOCKED-OUT:     §2 list — re-surface to the maintainer at session start; do not plan through
```

Sequencing notes:
- 2.1+2.2 can ship as one MR (two read tools, shared tests); 2.3 as its own MR (mutation —
  different review profile); 2.4 as its own MR (different subsystem, `lc-api-reviewer`).
- 1.5 (TRK-254) may terminate at "blocked on TRK-099" — that outcome is a valid deliverable
  and moves the item to §2, not to a fix.
- If TRK-252's follow-up observation (0.3) shows sustained ~360s runs, promote it into
  Phase 1 as a real regression investigation alongside 1.4.
- All code-writing dispatches: `isolation:"worktree"` (mechanically enforced); commit
  locally early and often; push/MR only batched and only with the maintainer's confirmation
  (standing merge policy — never autonomous).

## 7. Decision

Adopt the phase ordering above. The single highest-priority code item is **2.1
`get_manual_writes()`** (retroactive visibility over the already-existing 928 unattributed
writes plus all future ones), followed by 2.3 as the standing prevention. The single
highest-priority non-code item remains provisioning `OPS_WEBHOOK_URL` — four of the
audit's findings went unnoticed precisely because alert delivery is dead, and no amount of
sequenced code work substitutes for it.
