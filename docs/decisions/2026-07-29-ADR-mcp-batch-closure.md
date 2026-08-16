# ADR: audited, predicate-scoped batch closure over MCP

Date: 2026-07-29
Status: accepted
Scope: `src/infra_brain/mcp_server.py` (`resolve_drift_events`,
`close_compliance_violations`), `src/infra_brain/mcp_auth.py`
(`MUTATION_TOOL_NAMES`)

## Context

Before this change nothing in the MCP surface touched `drift_events.status` or
`compliance_violations.status` lifecycle state. `approve_proposal` /
`reject_proposal` act on `proposed_actions`; `record_rootcause_note` can
*annotate* a drift event but not resolve it. Consequently the finding queues
could only ever grow — 63,704 open drift events and 45,020 open violations at
the time of filing, a large share of them artifacts of the write-path defects
in #137 (ComplianceAgent `stale_drift:` retirement rows) and #142
(first-observation `null -> value` events).

Fixing the generating write paths is necessary but not sufficient: the rows
already written had no removal path short of direct DB surgery on the deployed
host, which CI reverts on the next deploy and which has already caused one
outage.

## Decision

Add exactly two mutating MCP tools, both writing **only** to infra-brain's own
Postgres. They cannot reach GitLab/Jira/Confluence, do not touch managed
infrastructure, and trigger no agent execution — the read-only core guarantee
is untouched (see `docs/READONLY-MODEL.md`; this is the same sanctioned
"mutates infra-brain's own DB" category as `approve_proposal`).

Load-bearing constraints:

1. **A narrowing predicate is mandatory.** `_require_predicate` refuses a call
   whose only filter is `from_status`. "Close everything open" is deliberately
   not expressible: an unscoped clear would erase real findings alongside
   artifacts and leave an audit trail showing a clean fleet that was never
   verified. An empty `event_ids` / `violation_ids` list is not a predicate
   either.
1a. **Predicate inputs cannot be widened back to "everything".** Two ways the
   mandatory-predicate check could have been satisfied in form but defeated in
   substance, both closed:
   - `field_prefix` / `rule_prefix` are matched **literally** — `%` and `_` are
     escaped (`_like_prefix`, `escape=_LIKE_ESCAPE`) and a blank prefix is
     refused. Unescaped, `field_prefix="%"` would have expanded to "match
     every row", which is precisely the unscoped bulk close the issue's design
     note forbids.
   - `from_status` is validated against a closed set (`open`, `acknowledged`) —
     it is no longer a pass-through where an empty or unrecognized value would
     fall through to "no status filter" (again: every row, any state). The
     terminal `resolved` is excluded too, since closing the already-closed is a
     no-op flip that would still write an audit row.
2. **`dry_run=True` is the default.** The preview reports `matched_total`, the
   bounded selection, what the cap hid, how many selected drift events already
   carry a `RootCauseNote` (i.e. were already investigated — closing those is
   usually a sign the predicate is too broad), and for compliance the exact
   tombstone rows a flip would DELETE.
3. **A hard per-call cap (`_CLOSURE_BATCH_CAP = 500`) bounds the scope of one
   call, and therefore of one audit row — not cumulative session volume.**
   Over-cap matches are *not* refused (that would make a 63k class permanently
   unclearable); they are truncated deterministically (oldest first) with
   `remaining` reported. An automated caller can still clear an arbitrarily
   large class across ~N/500 successive calls, so the cap is **not** a volume
   limit. What it actually buys is auditability: a large clear is forced to
   decompose into many individually recorded, individually attributable audit
   rows instead of one opaque one. If a true volume bound is ever wanted, it has
   to come from somewhere else (a rate limit or per-key quota), not from this
   constant.
4. **The audit row is written in the same transaction as the status flips.**
   `McpAuditMiddleware` is best-effort by design and HTTP-scoped, so a direct
   in-process invocation bypasses it. `_record_closure_audit` instead
   writes an `AgentActionLog` row (`agent='manual_mcp'`, `domain='mcp'`,
   `tool=<tool name>`, PAN-scrubbed `args_summary` carrying the predicate, the
   resolution reason, the actor and the exact id list) *inside the tool body*
   and inside the same commit. If the audit write fails, the closure rolls
   back. There is no such thing as an unrecorded batch closure, even under
   direct invocation. Read it back with
   `get_agent_activity(agent="manual_mcp")`.
5. **Attribution is server-derived** via `_caller_identity()` — resolved from
   the authenticated bearer token, never from a caller-supplied string. There
   is no `resolved_by` / `closed_by` / `actor` parameter at all, so the
   spoofable-attribution bug class fixed in the MCP write-parity work cannot
   reappear here.

Both tools are additionally gated by `_mutations_enabled()`
(`INFRA_BRAIN_MCP_ENABLE_MUTATIONS`) in their own bodies, and both are listed
in `mcp_auth.MUTATION_TOOL_NAMES` so per-key `allowed_tools` scoping applies
(a name absent from the catalog is 403'd for every key, including bootstrap).

### Closed status vocabulary

Both tools write the **existing** terminal value `status="resolved"` rather
than a new one, so every reader keeps working unchanged: `retention.py`'s
`status != "open"`, the `digest.py`/`fleet.py` open counters, and the
governance routes' status filter. No reader has to learn a new state to be
correct.

### Reason vocabulary

`resolution` is required and closed to four values — `fixed`, `never_valid`,
`wont_fix`, `superseded`. The issue's requirement is that "resolved because
fixed" and "closed as never-valid data" be *distinguishable* in the audit
trail; a closed enum is how, and keeps the trail aggregatable instead of
accumulating free-text synonyms. `note` is where per-batch prose goes (capped
and PAN-scrubbed via `_check_free_text`).

### Why the reason lives in the audit row, not on the finding row

Neither `drift_events` nor `compliance_violations` has a resolution-reason
column, and adding one is a `db/models/` schema change (migration +
`/pg-gate-check` + `lc-migration-reviewer`) that this change deliberately does
not make. The audit location is also strictly more durable: retention reaps
non-open `drift_events` at `retention_drift_events_days` (180) while
`agent_action_log` is kept `retention_agent_action_log_days` (400), so the
record of *why* a row was closed outlives the row itself.

**Accepted trade-off:** you cannot SQL-filter the finding tables by reason,
only reconstruct a batch from its audit row's recorded id list. A
`resolution_reason` column on both finding tables is **deferred**, not
rejected — revisit if reason-based reporting over live rows is ever needed.

### Why `close_compliance_violations` deletes rows

`uq_compliance_rule_host_status` permits one row per `(rule, host, status)`, so
flipping an open violation to `resolved` collides with any stale `resolved`
tombstone left by an earlier resolve -> reopen -> resolve cycle. ComplianceAgent
already resolves this by dropping the stale tombstone first (latest resolved
row wins; history is a single tombstone by design — `agents/compliance.py`).
This tool reproduces that *outcome* rather than inventing a second, divergent
one — but **not** compliance.py's ordering discipline, and that divergence is
deliberate. `agents/compliance.py:408-424` performs every tombstone DELETE and
flushes before mutating any status, specifically so autoflush cannot push a
flipped row into a collision with a tombstone not yet deleted. This tool
interleaves instead: delete-then-flip, per row, inside that row's SAVEPOINT.
That is safe here because `uq_compliance_rule_host_status` is
`(rule, host, status)` and every selected row shares one `from_status`, so the
constraint itself guarantees at most one selected row per `(rule, host)` — there
is no second selected row for autoflush to collide with. The per-row SAVEPOINT
is doing the work compliance.py's ordering does, and buys something that
ordering cannot: a concurrent writer's collision costs one row, not the pass.
Every deletion is previewed in dry-run as
`tombstones_to_delete` and recorded in the audit row as `tombstones_deleted`.
Each row is flipped inside its own SAVEPOINT, so one row colliding
concurrently reports an `error` entry for that row alone instead of losing the
batch — per-row outcomes are always explicit, never silent.

## Alternatives rejected

- **An unrestricted `clear_open_drift`.** Rejected per the issue's own design
  note: it would erase real findings alongside artifacts.
- **A `resolution_reason` schema column now.** Deferred (above) to keep this
  change out of the migration/`pg-gate-check` path.
- **A new terminal status (e.g. `closed_never_valid`).** Rejected: every
  existing open-count reader would silently miscount until updated.
- **Relying on `McpAuditMiddleware` for the audit record.** Rejected: it is
  best-effort and bypassable by direct in-process invocation.

## Verification

`tests/test_mcp_batch_closure.py` (20 tests) covers the mutation gate, catalog
registration, unscoped-call refusal (including "`from_status` alone is not a
predicate" and "unscoped call changes nothing"), the closed reason vocabulary,
id/timestamp/note validation, dry-run-by-default, the cap's oldest-first
truncation with `remaining`, the in-transaction audit record's contents, the
tombstone collision in both preview and execute, per-row savepoint isolation,
and that attribution is server-derived and has no caller-settable parameter.
Two tests specifically pin the widening defences: a `%`/`_` prefix selects only
rows whose text literally starts with it (and a blank prefix is refused), and an
empty/unknown/terminal `from_status` is refused without touching a row.

## Known residual, accepted

`_require_predicate` enforces that *a* predicate is present, not that it is
*selective*. Three ways a formally valid predicate can match nearly the whole
queue, all left permitted, all bounded only by the per-call cap and the audit
row's record of the exact predicate used:

- **`detected_after` with an ancient timestamp** (e.g. `1970-01-01`). Unlike a
  stray `%` this cannot happen by accident — nobody types 1970 while meaning
  "recent".
- **`detected_before` with a current timestamp** (e.g. `detected_before=<now>`),
  which matches ~100% of the queue. This is the **more dangerous of the two**
  and was not called out in the original draft: "close everything detected
  before now" is an intuitive, easy-to-reach-for phrasing that a caller could
  plausibly write while intending "close the old stuff", where the 1970 case
  requires deliberately choosing an absurd bound. Treat a `detected_before` near
  the present as the shape most likely to produce an accidental near-total
  clear, and rely on `dry_run=True` (the default) surfacing `matched_total`
  before it happens.
- **`unlinked_only=True` (drift only), whose selectivity decays over time.**
  Today it is tight — a null `collection_run_id` overwhelmingly means a #137
  retirement row. But `retention.py:166-171` nulls `collection_run_id` on any
  drift event whose collection run has aged past
  `retention_collection_runs_days`, so legitimate, correctly-generated events
  progressively join the "unlinked" set as they age. The predicate that means
  "artifact" today means "artifact or simply old" later, and nothing warns when
  that crossover happens.

Re-evaluate all three if a "predicate selectivity" check (e.g. refuse when the
selection exceeds some fraction of `matched_total` for the whole table, or when
`matched_total` exceeds a threshold without an explicit acknowledgement) is ever
wanted. That check is the missing piece, not more predicate plumbing.
