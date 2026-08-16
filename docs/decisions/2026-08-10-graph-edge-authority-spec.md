All source read and verified. Here is the spec.

---

# Design Spec: Authority & Provenance Model for Knowledge-Graph Identity Edges

**Fixes KG-1 (critical), KG-2 (high), KG-3 (high) as one coherent model.**
**Status: design only — no code, no migration written. Handed to implementation agents as-is.**

---

## 0. The one missing model (read this before the numbered sections)

All three findings are symptoms of the same absence: **decisions about identity are not first-class, authoritative facts in the graph.** Today, decision state is smeared across four carriers that do not consult each other:

1. The free-form `graph_edges.source` string (an emitter name doing double duty as an authority marker — `graph_phase3.py:123-125`),
2. `ProposedAction.status` flips (`action_decisions.py:119`, with no attribution and per-node, not per-pair, scope),
3. An in-memory dict key `identity_ambiguous_sources` that is never persisted and never read (`host_reconcile.py:833-835`),
4. Nothing at all, for "a human said NO to this pair."

The fix: **decisions live in the graph as bitemporal, authority-tagged edges. The review queue is a workflow inbox, never a decision store.** Concretely:

- A new `graph_edges.authority` column (`'auto'` | `'human'`) with a precedence rule enforced at the single write choke point (`upsert_edge`).
- A human YES is an active `SAME_AS` edge with `authority='human'`.
- A human NO is a new **`NOT_SAME_AS`** edge with `authority='human'` — pair-scoped, bitemporal, retractable, and consulted by *every* auto emitter in *both* stores before emission.
- A PENDING question gates auto-emission of the pairs it covers, via one shared predicate both `graph_phase3` and `host_reconcile` call.
- `identity_ambiguous_sources` becomes a real persisted column on `host_identities`, consulted by `_emit_is_same_as_edges` and fed into `_score_candidate` as counter-evidence.

A fix to KG-1 alone (e.g., making `upsert_edge` filter on `source`) would leave KG-2 (host_reconcile still emits over ambiguity/pending reviews) and KG-3 (rejection still node-permanent yet emission-porous) fully live, because neither of those bugs touches `upsert_edge`'s row-selection at all.

---

## 1. Verified code ground truth (trust-the-code notes)

Every finding was re-verified against source. Where the findings' wording and the code differ, the code is stated here and governs.

| Claim | Code | Verdict |
|---|---|---|
| `upsert_edge` selects active edge with no `source` filter, then overwrites `method`/`confidence`/`evidence`/`source` unconditionally | `graph_phase2.py:166-173` (select: `source_id, target_id, edge_type, valid_to IS NULL` — note it *does* filter to the active edge, which the finding's "(source_id, target_id, edge_type)" phrasing omits but which changes nothing about the bug) and `graph_phase2.py:190-194` (unconditional overwrite incl. `edge.source = source`) | **CONFIRMED** |
| `retract_same_as` filters `GraphEdge.source == EMITTER_SAME_AS_CONFIRMED` and errors otherwise | `graph_phase3.py:878`, error at `graph_phase3.py:891-898` | **CONFIRMED** |
| The 2-hourly `graph_maintenance` pass re-runs `resolve_entities` unconditionally | `graph_maintenance.py:463-471`; cadence per comment `graph_maintenance.py:82` ("every 2h"). Pass 1 emits at `graph_phase3.py:1189-1196` via `_emit_same_as` → `upsert_edge`, with **no check for an existing human edge or for a rejected/approved review row**. So a confirmed edge (`declared`/1.000/`graph_phase3.confirm_same_as`, written at `graph_phase3.py:769-781`) is rewritten in place to `deterministic_match`/0.990/`graph_phase3.same_as` on the next pass, destroying approver attribution, after which retraction is permanently impossible. No data change required. | **CONFIRMED** |
| `_emit_is_same_as_edges` consults neither `identity_ambiguous_sources` nor the review queue before emitting | `host_reconcile.py:1200-1323`. Its only pre-emission guards are the domain conflict (`:1268`) and the open-IP-conflict set (`:1281`). It emits at `_CONF_HOSTNAME = 0.95` (`:123`, used `:1295`) on the first-write-wins survivor of a collision (`:805-807` — "first-write-wins is kept for WHICH candidate is stored"). The review-queue check that *does* exist (`_same_as_review_exists`, `:977-1010`) is used only to suppress duplicate **DriftEvents** (`:916-925`), never to gate edge emission. | **CONFIRMED** |
| `identity_ambiguous_sources` is set on an in-memory dict, has no column, has zero consumers | Setter: `host_reconcile.py:833-835`. `HostIdentity` (`db/models/core.py:493-550`) has no such column. Repo-wide grep finds only the setter, two docstrings (`:305`, `:810`), and tests. The `:810-811` docstring's claim that "downstream consumers (e.g. get_host_profile) can tell this source's leg is unsettled" is **false as written** — the flag dies with the dict when `run()` returns. | **CONFIRMED** |
| Rejection permanently blocks re-asking, but not re-emission | `REVIEW_LIVE_STATUSES = ("pending", "approved", "rejected")` (`graph_phase3.py:134`) is checked by `queue_for_review` (`:589-601`), so one rejected row silences **all future questions anchored on that source node forever** — note the scope is even worse than "that pair": the row bundles up to `MAX_REVIEW_CANDIDATES = 10` candidates (`:159`) and the generic reject (`action_decisions.py:99-123`) flips the whole row with **no attribution** (no rejected_by field exists) and no per-candidate granularity. Meanwhile nothing in `resolve_entities` (either pass) reads ProposedAction status before emitting, so a data shift can auto-emit the exact rejected pair. Also note: `status='approved'` has the same node-forever-silenced shape (a node confirmed against source B can never be asked about source C), which this design also fixes. | **CONFIRMED** |

Store clarification for §5: the live DB's 2,427 sound edges cannot be `graph_edges` rows (only 7 `graph_nodes` exist); they are `resource_relationships` rows from the deterministic convergence emitters. Both stores are covered below; neither has its existing rows rewritten.

---

## 2. Q1 — The authority model

### 2.1 The field

Add to `graph_edges` (see §5 for exact DDL shape):

- **`authority`** — `'auto'` | `'human'`. New app-level enum `GraphEdgeAuthority` in `db/models/graph.py` alongside `GraphEdgeMethod`.

**Why `source` alone is the wrong discriminator** (the question asked): `source` is a `String(128)` free-form *emitter name* (`db/models/graph.py:267`). Using it as the authority marker is exactly what broke: (a) `upsert_edge` cannot know that `graph_phase3.confirm_same_as` outranks `graph_phase3.same_as` without a hardcoded emitter-precedence registry that every future emitter must remember to join; (b) `retract_same_as`'s equality filter on one emitter string (`:878`) silently breaks the moment a second human-confirmation path exists (an MCP bulk-confirm, a future import tool) — the exact brittleness class that produced KG-1. `method` cannot discriminate either: `AFFECTED_BY_CVE` is `declared`/1.000 *and* automatic (`graph_phase2.py:467-482`), so `method='declared'` ≠ human. Authority is a genuinely distinct axis — *on whose accountability does this assertion rest* — and it deserves its own column. Keep `source` for what it is good at: audit trail of which code path wrote the row.

### 2.2 Who may write, who may overwrite

All writes still flow through the single choke point `upsert_edge` (`graph_phase2.py:133-196`), which gains a keyword-only `authority: str = "auto"` parameter (default `'auto'` keeps all Phase-2 emitter call sites unchanged). Rules, enforced *inside* `upsert_edge` exactly as the confidence-honesty rule already is (`:158-164`):

| # | Situation | Behavior |
|---|---|---|
| W1 | No active edge for the triple | Insert with the writer's authority. Any writer may create. |
| W2 | Active edge, **same authority** as writer | Refresh in place (today's semantics, `graph_phase2.py:190-194`): update `method`/`confidence`/`evidence`/`source`/`recorded_at`, preserve `valid_from`. This keeps the 2-hourly re-observation of the Phase-2 emitters cheap and history-compact, unchanged. |
| W3 | Active edge `authority='auto'`, writer `'human'` (**escalation**) | **Retire the auto row** (`valid_to = now`, reusing `retire_edges` semantics, `graph_phase2.py:199-214`) **and insert a new row** with `authority='human'`, `valid_from = now`. Do NOT mutate in place. Rationale: an authority change is a *different assertion*, and the bitemporal design's whole point is that history shows "machine asserted 0.990 from T1–T2; operator declared 1.000 from T2–". Today's in-place mutation by `confirm_same_as` over a pre-existing auto edge destroys that record in the human→auto direction's mirror image of KG-1. |
| W4 | Active edge `authority='human'`, writer `'auto'` (**de-escalation attempt**) | **No-op.** Return the existing edge unchanged; log at WARNING (`"upsert_edge: auto writer %s declined to overwrite human-authority edge %s"`). This is a boundary guard in the DLP/readonly spirit — prevent and record, never raise (a raise inside an emitter's SAVEPOINT block, e.g. `graph_maintenance.py:465`, would abort the whole pass for a condition that is expected and benign). Emitters MUST also pre-filter (rule E1, §4.3) so hitting this guard is rare and indicates an emitter bug. |
| W5 | `edge_type == NOT_SAME_AS` with `authority != 'human'` | Raise `ValueError`, same style as the confidence rule at `:158-162`. Negative identity assertions are human-only in this design (machine negative knowledge stays what it is today: *suppression*, which asserts nothing). |

**Retraction fix (KG-1's second half):** `retract_same_as` replaces the filter `GraphEdge.source == EMITTER_SAME_AS_CONFIRMED` (`graph_phase3.py:878`) with `GraphEdge.authority == 'human'`. The error message at `:891-898` updates accordingly. Everything else about retract (with_for_update at `:886`, review-row reopen with retraction_history at `:911-955`) is sound and stays.

`confirm_same_as` passes `authority='human'` in its two `upsert_edge` calls (`:769-781`); combined with W3, confirmation over an auto edge now retires-and-inserts rather than mutating.

### 2.3 What happens when an automatic emitter meets a human edge

Layered, so no single miss is fatal:

1. **Pre-filter (primary):** at the start of `resolve_entities`, load all active `SAME_AS` and `NOT_SAME_AS` edges with `authority='human'` among the host nodes; seed the confirmed pairs into `matched_pairs` (so both passes skip them via the existing checks at `:1216-1218`) and the vetoed pairs into a new `vetoed_pairs` set checked before *any* emit or queue.
2. **Choke-point guard (backstop):** rule W4 in `upsert_edge`.
3. **Observability:** `resolve_entities` counts gain `human_confirmed_skipped`, `human_vetoed_skipped`, `pending_gated` so the graph-health stats (`graph_maintenance.py` stamped stats) show the gates working rather than silently doing nothing.

---

## 3. Q2 — What a rejection means, precisely

### 3.1 Definition

> **A rejection is a named human's assertion that a specific PAIR of nodes are not the same entity, judged against the evidence presented at rejection time.**

Not "stop asking about this node." Not "this whole 10-candidate bundle is wrong." Pair-scoped, attributed, and evidence-anchored.

### 3.2 State carrier

An active **`NOT_SAME_AS`** edge pair in `graph_edges` (both directions, mirroring `_emit_same_as`'s symmetric-storage choice at `graph_phase3.py:988-1004`):

- `edge_type = 'NOT_SAME_AS'` (new `GraphEdgeType` member; the "HARD SCOPE BOUNDARY: only these four" comment at `db/models/graph.py:118-122` is deliberately amended by this design — that boundary guarded against *speculative* edge types like `DEFINED_IN_IAC`, and this is neither speculative nor a guess-dressed-as-fact: it is the missing negative half of the SAME_AS vocabulary)
- `authority = 'human'`, `method = 'declared'`, `confidence = 1.000` (consistent with the honesty rule: a named human's declaration, exactly the `confirm_same_as` rationale at `graph_phase3.py:688-694`)
- `evidence = {"basis": "human_rejection", "rejector": <name>, "rejected_at": <iso>, "reason": <optional>, "rejected_evidence_class": <see 3.3>, "rejected_candidate_snapshot": <the candidate dict as presented>}`

Why an edge and not a new table or a ProposedAction status: it inherits, for free and with zero new machinery, everything the model already guarantees — bitemporality (undo a rejection = retire the edge, never DELETE), the one-active-row partial unique index (`db/models/graph.py:292-300`), symmetric storage, provenance, and — critically — **one queryable place both `graph_phase3` and `host_reconcile` can consult** (§4). A rejection recorded as a ProposedAction status is invisible to `host_reconcile` today; that is KG-2's root shape repeating.

### 3.3 Duration and the re-ask ladder (the symmetric design)

A `NOT_SAME_AS` edge stays active until one of:

1. **Explicit human retraction** — extend `retract_same_as` (or add a sibling `retract_not_same_as` sharing its body) to retire an active human `NOT_SAME_AS` pair, same attribution rules.
2. **Human reversal** — a later `confirm_same_as` on the same pair MUST first retire the active `NOT_SAME_AS` pair (stamping who overrode whom into both rows' evidence), then write the human `SAME_AS`. A pair can never simultaneously carry an active SAME_AS and NOT_SAME_AS; `confirm_same_as` enforces this ordering.

While active, the veto:

- **Blocks auto-emission of the pair, in both stores** (this is the half KG-3 says is missing today): `resolve_entities` pass 1 and pass 2 check `vetoed_pairs` before `_emit_same_as`; `host_reconcile._emit_is_same_as_edges` checks the shared predicate (§4) before appending an edge for the pair. If the data shifts and the machine's score crosses any threshold, the outcome is at most a *re-ask* (below), never an edge.
- **Excludes the vetoed candidate from future review-queue candidate lists**, with one exception —

**The evidence-class ladder.** Define an ordered evidence-class scale, recorded on the veto as `rejected_evidence_class`:

`fuzzy` (name-similarity score) **<** `exact_name` (normalized-name equality) **<** `hard_identifier` (shared vSphere `uuid`/`instance_uuid`/`mac` per `_HARD_IDENTIFIER_FIELDS`, `graph_phase3.py:409`)

If a later pass produces evidence for a vetoed pair whose class is **strictly stronger** than `rejected_evidence_class`, the pair may be **re-queued** — as a review question flagged `"previously_rejected": {<the veto's evidence dict>}` so the reviewer sees they are being asked to overrule a colleague — but still never auto-emitted. The veto edge stays active until the human answers (confirm → 3.3.2; re-reject → refresh the veto's evidence in place with the new, stronger `rejected_evidence_class`, a same-authority W2 refresh). A veto rejected at `hard_identifier` class is never re-asked automatically (nothing outranks it); only 3.3.1/3.3.2 reopen it.

This is the symmetry the finding demands: rejection now blocks emission (it didn't), and no longer permanently silences the node (it did).

### 3.4 Route changes

- **New:** `POST /api/graph/entity-resolution/{action_id}/reject` (admin-gated, mirroring the confirm route at `graph_api.py:32-45`), body `{target_node_id?, rejector, reason?}`:
  - With `target_node_id` (must be in the row's own `candidate_matches`, same constraint discipline as confirm): write the `NOT_SAME_AS` pair for that one candidate; remove it from the row's `candidate_matches`; the row **stays `pending`** if candidates remain, else flips to `rejected`.
  - Without `target_node_id`: reject every listed candidate (each gets its own attributed `NOT_SAME_AS` pair — the operator did look at all of them; that is an honest per-pair assertion), row flips to `rejected`.
  - Blank `rejector` refused — identical stance to `confirm_same_as` (`graph_phase3.py:715-716`).
- The generic `POST /api/dashboard/actions/{id}/reject` (`governance_ops.py:631-655`) gains a 409 for `REVIEW_ACTION_TYPE`, pointing at the new route — the exact mirror of what the generic *approve* route already does (`graph_api.py:40-43`). The comment in `action_decisions.py:105-112` claiming a bare status flip suffices for this action_type is superseded by this design (a bare flip is precisely the unattributed, pair-less rejection that caused KG-3).

### 3.5 `queue_for_review` idempotency change

`REVIEW_LIVE_STATUSES` (`graph_phase3.py:134`) shrinks to **`("pending",)`** for the already-asked check: the open-question invariant is "at most one *pending* question per node." `approved`/`rejected` no longer block new questions — instead, per-candidate filtering does the real work: a candidate is dropped from any new question if the pair already has an active human `SAME_AS`, an active `NOT_SAME_AS` at ≥ the current evidence class, or an active auto `SAME_AS`. A question with zero surviving candidates is not queued. (This also fixes the adjacent `approved` variant of KG-3 noted in §1: node A confirmed = B can later be asked about C.) The `_GAP_PROPOSAL_LIVE_STATUSES` analogy the comment cites is not disturbed — ComplianceAgent keeps its own semantics; only the entity-resolution constant changes.

This requires the ProposedAction constraint change in §5.3 (multiple historical `rejected`/`approved` rows per target become possible).

---

## 4. Q3 — How a PENDING review gates emission (the cross-module contract)

### 4.1 The shared predicate

One function, defined in `graph_phase3` (host_reconcile already imports from there — `host_reconcile.py:42`), is the **only** implementation of "is this pair decided or under question":

```
pair_gate(session, node_a: GraphNode, node_b: GraphNode) -> str | None
```

Returns the blocking reason or `None` (emit freely):

| Return | Condition | Meaning for an auto emitter |
|---|---|---|
| `'human_confirmed'` | Active `SAME_AS` with `authority='human'` between the pair (either direction) | Skip — the fact is already asserted at higher authority. |
| `'human_veto'` | Active `NOT_SAME_AS` between the pair | Skip — a human said no. |
| `'pending_review'` | A `pending` `REVIEW_ACTION_TYPE` row whose `target` is either node's `_review_target` **and** whose `candidate_matches` includes the other node | Skip — the machine already asked; it must not answer its own open question by emitting. |

Plus a resource-space adapter for host_reconcile:

```
resource_pair_gate(session, resource_id_a, resource_id_b) -> str | None
```

which maps each `resources.id` to its `graph_nodes` rows via `graph_nodes.resource_id` (the reconciliation join Phase 2 deliberately left available — `db/models/graph.py:54-58`) and returns the strongest blocking reason across all node-pair combinations. A resource with no graph node contributes no block (fail-open on the *gate*, matching `_same_as_review_exists`'s explicit fail-open direction at `host_reconcile.py:987-989` — the worst case is today's behavior, never a silently dropped positive signal... note the asymmetry is deliberate: gates fail open, emissions of *disputed* pairs are what we're closing).

### 4.2 host_reconcile's obligations (fixes KG-2's emission half)

In `_emit_is_same_as_edges` (`host_reconcile.py:1200-1323`), immediately after the two existing suppression guards (`:1268`, `:1281`) and before appending edges for a pair (`:1300`):

1. **Ambiguous-leg gate:** if either endpoint's source label is in the merged record's `identity_ambiguous_sources` (now also persisted — §5.2), skip every pair involving that leg. The coin-flip winner of a same-source collision is exactly the row first-write-wins kept (`:805-807`); asserting it at 0.95 while a human question about it may be open is KG-2's core defect.
2. **Cross-store gate:** `resource_pair_gate(...)` — skip on any non-None reason, log at INFO with the reason (mirroring the existing suppression logging style at `:1269-1277`).

The same two gates apply to `_emit_cross_hostname_ip_edges` (`host_reconcile.py:1407+`), which is an even lower-confidence emitter and must not outrun a human either.

`host_reconcile` remains the **sole** `IS_SAME_AS` writer in `resource_relationships` (the Task 4.6 contract restated at `graph_phase3.py:65-70`) — the gate changes what it declines to write, never who writes.

### 4.3 graph_phase3's obligations

- **E1:** `resolve_entities` pass 1 (`:1189`) and pass 2 (`:1251`) call `pair_gate` (or equivalently consult the pre-loaded sets from §2.3) before `_emit_same_as` *and* before adding a candidate to `pending_review`.
- **E2 (machine withdraws a claim it no longer stands behind):** when a pass diverts a pair to review (counter-evidence at `:1160-1179`, ambiguous key at `:1105-1142`, or the pass-2 review band at `:1264-1286`) **and** an active `authority='auto'` `SAME_AS` edge exists for that same pair, retire that auto edge pair (`retire_edges`), stamping `evidence["retired_reason"] = "diverted_to_review"`. Without this, the store can simultaneously assert "same" and ask "same?", which is the incoherence class this whole design exists to remove.

### 4.4 host_reconcile → graph_phase3 direction (completes "do not communicate in either direction")

`_score_candidate` (`graph_phase3.py:433-523`) gains one counter-evidence source: if either node's `resource_id` maps to a `host_identities` row whose (now persisted) `identity_ambiguous_sources` contains that node's source label, append `"unsettled identity leg for <source> (host_reconcile same-source collision)"` to `counter_evidence`. Existing machinery then does the right thing automatically — counter-evidence diverts to review instead of auto-emit (`:1160`, `:1241`).

The existing DriftEvent-suppression direction (`_same_as_review_exists`, `:977-1010`) is sound and unchanged.

---

## 5. Q4 — Schema changes (exact; no migration written here)

### 5.1 `graph_edges` — new column

| Table | Column | Type | Constraints |
|---|---|---|---|
| `graph_edges` | `authority` | `VARCHAR(16)` (`String(16)` in the model) | `NOT NULL`, `server_default='auto'`; app-level enum `GraphEdgeAuthority = {'auto','human'}` in `db/models/graph.py`; optional `CheckConstraint("authority IN ('auto','human')", name="ck_graph_edges_authority")` — include it; this table is small and the vocabulary is load-bearing |

No new index required: authority is only ever queried in combination with the already-indexed `(source_id, target_id, edge_type)` active-edge lookups.

`edge_type` is `String(64)` with an app-level vocabulary (`db/models/graph.py:258-259`), so adding `NOT_SAME_AS = "NOT_SAME_AS"` to `GraphEdgeType` needs **no DB change**.

### 5.2 `host_identities` — yes, `identity_ambiguous_sources` needs a real column

The docstring at `host_reconcile.py:810-811` promises downstream visibility that does not exist; either the promise or the gap must go, and §4 needs the data. Add:

| Table | Column | Type | Constraints |
|---|---|---|---|
| `host_identities` | `identity_ambiguous_sources` | `JSONB` (the project's `_base.JSONB`, dialect-portable) | `nullable=True`, default `None` |

Semantics: a JSON list of source labels (values from `_SOURCE_KEYS` labels, `host_reconcile.py:96-106`) whose leg for this `short_hostname` had a same-source collision in the **most recent** reconcile run. Written by `_upsert_identities` from the merged dict on every run — **recomputed each run, not accumulated**, and cleared (set to `None`/`[]`) when the collision is no longer observed, so a resolved collision self-heals within the 30-minute cadence (`spec.schedule`, `host_reconcile.py:86`). Consumers: `_emit_is_same_as_edges` (§4.2), `_score_candidate` (§4.4), `get_host_profile`/dashboard (making the `:810` docstring true at last).

### 5.3 `proposed_actions` — constraint reshape

`uq_proposed_action_target_status` (`db/models/core.py:389-392`) is `UNIQUE(action_type, target, status)`. Once `rejected`/`approved` rows stop blocking re-asks (§3.5), a second historical `rejected` row for the same target violates it. The comment above it (`core.py:388`) states the *actual* invariant: "at most one **open** proposal per (action_type, target)". Enforce exactly that:

- **Drop** `uq_proposed_action_target_status`.
- **Add** partial unique index `uq_proposed_action_open` on `(action_type, target)` with `postgresql_where=text("status = 'pending'")` and `sqlite_where=text("status = 'pending'")` (the codebase already uses this exact dual-dialect partial-index pattern at `db/models/graph.py:292-300`, so the sqlite test suite keeps enforcing it).

**Blast-radius caution for implementers:** `proposed_actions` is shared with remediation and compliance flows. Both do their own live-status dedup in application code (e.g. ComplianceAgent's `_GAP_PROPOSAL_LIVE_STATUSES`), and the executor picks up rows by `action_type in ("config_fix","vuln_patch")` — this reshape widens nothing they rely on (a target can still have only one pending row), but the change touches `db/models/` and therefore **requires `/pg-gate-check` before push** (CLAUDE.md constraint 5) and a generated — never hand-written — migration via `/migration-create`.

### 5.4 Not changed

- `resource_relationships`: **no schema change.** Authority gating for that store is behavioral (§4.2) — the store's provenance limitations are documented and accepted (`db/models/graph.py:47-49`), and rewriting a live, widely-read table was already ruled out (`graph_phase3.py:55-59`).
- `ProposedAction` gets no `rejected_by` column: rejection attribution now lives on the `NOT_SAME_AS` edge evidence, which is the system of record; duplicating it on the inbox row is optional payload (`payload["rejections"]` append is permitted, not required).

---

## 6. Q5 — Migration and compatibility with existing data

Live state (given): 2,427 sound edges from the deterministic convergence emitters (necessarily `resource_relationships` rows — only 7 `graph_nodes` exist, see §1); 0 entity-resolution `graph_edges`, 0 `IS_SAME_AS` in the graph store, 0 pending questions.

1. **The 2,427 edges: untouched, by construction.** They live in `resource_relationships`, which this design does not alter schematically and whose existing rows no code path rewrites. Any `graph_edges` rows that do exist (Phase-2 `HOSTED_ON`/`MOUNTS_DATASTORE`/`AFFECTED_BY_CVE` among the 7 nodes) receive `authority='auto'` via `server_default` — which is *correct*, not a compromise: every current `graph_edges` writer except `confirm_same_as` is an automatic emitter, and the live DB has zero human-confirmed edges. No row is retired, re-valued, or re-attributed.
2. **Defensive backfill (no-op on live data, required for dev/test parity):** one `UPDATE graph_edges SET authority='human' WHERE source = 'graph_phase3.confirm_same_as'` in the migration, so any environment that *does* hold confirmed edges (test fixtures, dev DBs) is coherent the moment the code deploys. Idempotent, 0 rows in prod.
3. **Existing `rejected` ProposedAction rows are NOT converted into `NOT_SAME_AS` vetoes.** A historical rejection was a bundle-level, unattributed status flip; fabricating up to 10 per-pair human declarations from it would be dishonest provenance. They remain as history; because `REVIEW_LIVE_STATUSES` shrinks, those nodes become re-askable — which is the intended KG-3 fix, and costs nothing on a DB with 0 such rows.
4. **Ordering:** the `graph_edges.authority` and `host_identities.identity_ambiguous_sources` columns must land **before or with** the code that reads them; `server_default` + nullable respectively make both deploy-order-safe against old code (old code never writes the columns; new code tolerates NULL ambiguous-sources). The `proposed_actions` index swap is safe with 0 live entity-resolution rows; on Postgres the new partial index should be created with the project's standard `CONCURRENTLY` discipline (the `/migration-create` danger-pattern review will check this — do not hand-write).
5. **All three hard MR gates** (`lock-freshness`, `migration-parity`, `sql-execution-check`) must be replicated locally via `/pg-gate-check` before push — this change touches `db/models/` and is exactly the class the sqlite suite cannot validate (CLAUDE.md constraint 5).
6. **Derivation-version stamp:** `graph_maintenance` stamps a derivation-logic version into its graph-health stats (`graph_maintenance.py:488-489`); bump it, so the first post-deploy pass is distinguishable in the health history.
7. **Traversal:** `NOT_SAME_AS` is a negative assertion, not connectivity. `blast_radius` / `root_cause_candidates` (`graph_phase3.py:1318+`) MUST exclude `edge_type='NOT_SAME_AS'` from the walk (add to the walk's edge-type filters); `get_reconciliation_state` (`:628-677`) should surface active vetoes on each row's candidates so reviewers see them.

---

## 7. Q6 — Explicit DO-NOT-CHANGE list (review-verified sound; this design builds on, never bypasses, each)

1. **Bitemporal retire-not-delete** (`db/models/graph.py:26-29`, `retire_edges` `graph_phase2.py:199-214`, the active-edge partial unique index `:292-300`). This design *extends* it — W3 escalation and E2 divert-retirement are retire-and-insert precisely because in-place mutation across an authority boundary is what destroyed the record in KG-1.
2. **The confidence-honesty rule** (`graph_phase2.py:158-164`; 1.000 only for `method='declared'`). Unchanged; `NOT_SAME_AS` at declared/1.000 conforms to it. Do not weaken, relocate, or duplicate it.
3. **Decay never touching structural or confirmed edges** (graph_maintenance's decay over `resource_relationships`). Untouched. New human-authority edges live in `graph_edges`, which decay does not process; do not "helpfully" extend decay there.
4. **Bounded traversals** — `MAX_HOPS = 3`, `MAX_TOP_N = 100` (`graph_phase3.py:165-167`) and the summarised, never-raw-dump read contract. Untouched.
5. **Race-safe confirm/retract** — `with_for_update` on the contended edge rows (`graph_phase3.py:869-887`) and on the review row (`:917-927`); the confirm route's candidate-list constraint and blank-approver refusal (`:715-758`); the approve-route 409 single-path discipline (`graph_api.py:40-43`). The new reject route must *match* this bar, not relax it.
6. **The existing domain-conflict suppression and the IP-conflict guard** in both `host_reconcile` passes and both `graph_phase3` passes. The new gates are additional, after these, never replacements.
7. **The three Phase-2 emitters' join logic and evidence payloads** (`graph_phase2.py:271-485`) — the deterministic convergence emitters the review verified sound. They change only in that `upsert_edge` grows a defaulted keyword they don't pass.
8. **host_reconcile as sole `IS_SAME_AS` writer in `resource_relationships`** (`graph_phase3.py:65-70`); `graph_phase3` as sole `SAME_AS`/`NOT_SAME_AS` writer in `graph_edges`.
9. **The existing DriftEvent detection and review-queue dedup** (`_note_identity_collision`, `_emit_identity_conflict_events`, `_same_as_review_exists`) — visibility layer stays exactly as is; this design adds the enforcement layer it lacked.
10. **`retraction_history` preservation on reopened rows** (`graph_phase3.py:932-955`) — extend the same pattern to rejections (`previously_rejected` flag, §3.3), never replace it.

---

## 8. Implementation map (per file, for dispatch)

| File | Changes |
|---|---|
| `db/models/graph.py` | `GraphEdgeAuthority` enum; `GraphEdge.authority` column + check constraint; `GraphEdgeType.NOT_SAME_AS`; amend the `:118` scope-boundary comment to name this design as the sanctioned extension |
| `db/models/core.py` | `HostIdentity.identity_ambiguous_sources` (JSONB, nullable); replace `uq_proposed_action_target_status` with partial unique `uq_proposed_action_open` (pending-only) |
| `src/infra_brain/graph_phase2.py` | `upsert_edge(..., authority="auto")` with rules W1–W5 (§2.2); no emitter call-site changes |
| `src/infra_brain/graph_phase3.py` | `confirm_same_as` passes `authority='human'` + retires active `NOT_SAME_AS` first (§3.3.2); `retract_same_as` filters on `authority='human'` + handles `NOT_SAME_AS` retraction; new `reject_same_as(session, action_id, target_node_id|None, rejector, reason)` core function (routes call it, per the every-caller-holds principle of `:708-710`); `pair_gate` / `resource_pair_gate`; `resolve_entities` pre-loads confirmed/vetoed pairs, applies E1/E2, new counters; `REVIEW_LIVE_STATUSES → ("pending",)` + per-candidate decided-pair filtering + evidence-class re-ask ladder; `_score_candidate` reads persisted ambiguous legs (§4.4); traversal excludes `NOT_SAME_AS`; `get_reconciliation_state` surfaces vetoes |
| `src/infra_brain/agents/host_reconcile.py` | `_upsert_identities` persists/clears `identity_ambiguous_sources`; `_emit_is_same_as_edges` + `_emit_cross_hostname_ip_edges` apply the ambiguous-leg gate and `resource_pair_gate`; fix the `:810` docstring to describe the now-true behavior |
| `src/infra_brain/graph_api.py` | New reject route (§3.4); retract route extended for veto retraction |
| `src/infra_brain/api/routers/governance_ops.py` + `action_decisions.py` | Generic reject 409s `REVIEW_ACTION_TYPE` (mirror of approve); update the `action_decisions.py:105-112` comment |
| `src/infra_brain/mcp_server.py` | Mirror reject/confirm tool parity with the routes; `bulk_reject_proposals` continues excluding `REVIEW_ACTION_TYPE` |
| Alembic | One generated migration via `/migration-create` covering §5.1–5.3 (backfill UPDATE from §6.2 included); `/pg-gate-check` mandatory before push |

## 9. Test requirements (minimum bar)

1. **KG-1 regression:** confirm A↔B (`authority='human'`), run `resolve_entities` twice; assert the human edge is byte-identical (method/confidence/evidence/approver intact), the auto pass counted `human_confirmed_skipped`, and `retract_same_as` still succeeds afterward.
2. **W3 escalation:** auto edge exists → confirm → assert old row retired (`valid_to` set), new row `authority='human'`, history shows both.
3. **W4/W5 guards:** auto writer vs human edge → no-op + warning; `NOT_SAME_AS` with `authority='auto'` → `ValueError`.
4. **KG-3 symmetry:** reject pair → shift data so the pair would deterministically match → assert no edge emitted, `human_vetoed_skipped` incremented; then present hard-identifier evidence → assert re-queued with `previously_rejected`, still no edge; confirm from the re-queued row → veto retired, human SAME_AS written. Separately: rejected node with a *different* new candidate → new question queued (node not silenced).
5. **KG-2 gates:** same-source collision → `identity_ambiguous_sources` persisted on `host_identities`; `_emit_is_same_as_edges` skips the ambiguous leg's pairs and skips a pair with a pending review row covering it; `_score_candidate` produces the unsettled-leg counter-evidence.
6. **Constraint:** two sequential rejected rows for one target coexist; two pending rows for one target refused (both dialects).
7. **Traversal:** `NOT_SAME_AS` never appears in `blast_radius` output.

---

*Ground-truth line references were verified 2026-08-10 against branch `fix/deploy-pipeline-master-to-main-rename` (clean tree, HEAD d3d0603): `src/infra_brain/graph_phase2.py`, `src/infra_brain/graph_phase3.py`, `src/infra_brain/agents/host_reconcile.py`, `src/infra_brain/agents/graph_maintenance.py`, `src/infra_brain/db/models/graph.py`, `src/infra_brain/db/models/core.py`, `src/infra_brain/graph_api.py`, `src/infra_brain/action_decisions.py`, `src/infra_brain/api/routers/governance_ops.py`.*