# infra-brain Architecture (living document)

> **Header rule:** if updating this doc requires updating a second doc, the
> second doc should be generated or deleted. This file is the only
> hand-maintained architecture description; anything it references that can
> drift (rosters, schedules, counts) must live in generated artifacts
> (`AGENTS.md`) or in code (`etl/spec.py`) — never be duplicated here.

## The AgentSpec contract

Every domain agent declares exactly one piece of metadata: a frozen
`AgentSpec` (`src/infra_brain/etl/spec.py`) set as a class attribute:

```python
class OctopusAgent(ETLConnector):
    spec = AgentSpec(
        domain="octopus",
        tier=Tier.COLLECTOR,
        schedule="10 2 * * *",          # 5-field cron, or None
        max_staleness=timedelta(hours=26),  # cadence + slack, or None
        skip_hook=False,                # default
        dispatchable=True,              # default
        retired=False,                  # default
    )
```

`ETLConnector.__init_subclass__` derives the legacy class attributes
(`domain`, `schedule`, `skip_hook`, `dispatchable`) from the spec, so
`supervisor.py` (AGENT_REGISTRY / SKIP_HOOK) and `scheduler.py`
(_DEFAULT_SCHEDULES) work unmodified. Everything else derives too:

- `callbacks/freshness.py` — `DOMAIN_EXPECTED_MAX_AGE` is a lazy view over
  `spec.max_staleness`.
- `agents/fleet_health.py` — the `_domain_staleness` window is
  `spec.max_staleness` per domain.
- `agents/coverage.py` — expected cadence for a wired domain comes from
  `spec.schedule` via `etl.spec.schedule_by_domain()`.
- `graph.py` — the whole sweep topology comes from `etl.spec.sweep_members()`.
- `AGENTS.md` — generated from the specs by `scripts/gen_agents_md.py`.

Before Phase 1 Task 5 (TRK-047) this metadata lived in four hand-maintained
tables that had already drifted (octopus: 24h in fleet_health vs 26h in
freshness). There are now zero shadow copies; a value that appears in two
places is a bug.

### Turning a collector off (`retired`)

This system monitors a home lab, and several collectors are remnants of an
enterprise estate that will never be configured here. `retired=True` is the
first-class OFF switch for exactly that case. A retired domain is **not
scheduled, not a sweep member, not freshness-monitored, not a coverage gap, and
not dispatchable** — but it stays in `AGENT_REGISTRY`, keeps every other field
of its spec (including the cron it *would* run on), stays importable and
testable, and reports as `retired` in both roster surfaces. It is a switch, not
a deletion. Retired today: `cloud`, `identity`, `k8s`, `octopus`, `vsphere`,
`vuln`, `windows`.

There are four ways to stop a collector; they are not interchangeable:

| Lever | Scope | Visible as | Writes a run row? | Reversed by |
|---|---|---|---|---|
| `retired=True` | permanent, by decision | `retired` | **no** | `COLLECTION_REVIVED_DOMAINS` |
| `dispatchable=False` | permanent, structural | *nothing* — absent from the registry | no | code edit |
| `dispatchable__<domain>` RuntimeConfig row | live, temporary pause | `paused` | yes, `skipped` each cycle | dashboard toggle |
| `collection_disabled_domains` | static skip | `skipped` | yes | settings |

`retired` deliberately writes **no** `collection_runs` row. The pause lever
writes one on purpose (a temporary pause must stay visible); a permanently-off
collector doing that produces a daily stream of `skipped` rows that read as
gaps — which is what this field exists to remove.

**The override only ever turns a collector ON.** `collection_revived_domains`
un-retires a domain with no code change; nothing in it can retire a live one
(that is what the two rows below it in the table are for). Retirement is the one
direction that previously required editing a critical file, so that is the
direction the override covers. If the settings read fails, a retired domain
**stays retired** — fail-safe, so a DB blip cannot silently start dispatching a
collector against a system that does not exist. See
`etl.spec.retired_domains()`.

### Tiers

| Tier | Meaning |
|---|---|
| `COLLECTOR` | Deterministic collector of an external system's state (GET-only clients or read-only-by-convention). |
| `RECONCILER` | Cross-domain identity/graph reconciliation over already-collected rows. |
| `REASONER` | Analysis over the DB: drift, compliance, triage, root cause, remediation drafting. |
| `REPORTER` | Read-only reporting/rollups outside the sweep graph. |
| `ON_DEMAND` | Dispatched explicitly (operator/hook/scoped job), never part of the scheduled sweep graph. |

Tier labels describe the orchestration-v2.1 *target* architecture (see
`docs/superpowers/specs/2026-07-11-agent-orchestration-v2.1-design.md`). The
Phase 2 sweep graph has landed but is strictly opt-in via `sweep_graph_enabled`
(default `False`, see the "Sweep graph" section below); with it off — the
default runtime path — all domains still dispatch flat via
`supervisor.dispatch()`. The current roster and each domain's tier live in
the generated `AGENTS.md`.

### Adding agent #29

One file plus one test: create `src/infra_brain/agents/<name>.py` with a
class subclassing `ETLConnector` (or `LLMAgent`) that declares its
`spec = AgentSpec(...)`, add the `(module, class)` entry to
`supervisor._AGENT_SPECS`, and add `tests/agents/test_<name>.py` (success,
empty, exception cases — `/agent-scaffold` generates both). Nothing else:
schedule, hook behavior, freshness monitoring, and the roster all derive
from the spec. Then regenerate the roster
(`.venv/bin/python scripts/gen_agents_md.py`) — the completeness
tests in `tests/etl/test_agent_spec.py` fail CI if the spec is missing,
malformed, mismatched with its registry key, or if `AGENTS.md` is stale.

## Sweep graph (Phase 2, `src/infra_brain/graph.py`)

Orchestration v2.1's replacement for `supervisor.dispatch()`-in-a-loop: one
LangGraph `StateGraph` runs a full sweep across every registry-derived
collector/reconciler/reasoner in a single graph invocation. It is strictly
**opt-in** (`sweep_graph_enabled`, default `False`) — see the cutover runbook
below. `supervisor.dispatch()` itself is untouched; the graph only calls
the same `AGENT_REGISTRY[domain]().run(...)` path it always used.

### State contract

`SweepState` (a `TypedDict`) carries **metadata only** — no collected payload
ever passes through graph state; every collector/reconciler/reasoner writes
its own rows straight to Postgres exactly as it does under `dispatch()`. The
fields:

| Field | Type | Meaning |
|---|---|---|
| `sweep_id` | `str` | Minted once per `run_sweep()` call; doubles as the LangGraph `thread_id` |
| `domains` | `list[str]` | Requested collectors (`[]` = every sweep collector) |
| `run_reasoners` | `bool` | Single-domain webhook policy switch — gates the whole reasoner tier |
| `trigger_type` | `str` | Stamped onto each `CollectionRun` (e.g. `"sweep"`, `"webhook"`) |
| `runs` | `Annotated[dict[str, str], _merge_dicts]` | domain → `run_id` |
| `statuses` | `Annotated[dict[str, str], _merge_dicts]` | domain → terminal status |

`runs`/`statuses` use a custom reducer (`_merge_dicts`, shallow-merges two
dicts) because parallel collector branches (`Send`) each return their own
single-domain entry — LangGraph's default "last writer wins" reducer would
drop every branch but one.

### Tier topology

Nodes are **registry-derived**: they come from `etl.spec.sweep_members()`
(itself reading `supervisor.AGENT_REGISTRY`), so adding agent #29 with a
`COLLECTOR`/`RECONCILER`/`REASONER` tier automatically adds its node — no
edit to `graph.py` required. `remediation` and `drift_learning` are excluded
via `_PHASE3_DEFERRED` and keep their standalone crons until Phase 3 adds the
interrupt/approval gate.

```
START
  │
  ▼
dispatch  (filters collection_disabled_domains + unknown domains → "skipped",
           never Sent — R-11, TRK-026)
  │
  ├─Send─▶ collect_<domain>  (one node per COLLECTOR-tier member, parallel,
  │         per-domain RetryPolicy, Redis dedup try_acquire(domain,"all"))
  │         [vsphere, octopus, cloud, iac, cicd, vuln, linux, windows,
  │          k8s, net, eol, netdiscovery — see AGENTS.md for the live roster]
  │
  └─(nothing to Send)────────────┐
                                 ▼
                    join  (defer=True — fires once every dispatched branch
                            reaches a terminal status, in ANY mix:
                            completed/partial/failed/skipped/retry_exhausted;
                            never hangs on one domain — spec §2.3)
                                 │
                                 ▼
                    host_reconcile → inventory_reconcile → graph_maintenance
                       (RECONCILER tier, sequential, registry-derived order
                        with the above as the preferred order — unlisted
                        RECONCILER members are appended alphabetically)
                                 │
                          run_reasoners? ──false──▶ END
                                 │ true
                                 ▼
                    drift  (full-sweep: detect_all(scoped=False) +
                             detect_state_drift(succeeded_domains=...))
                                 │
                                 ▼
                    notification  (notify_all())
                                 │
                                 ▼
                    {rootcause, vuln_triage, compliance, ...}  (REASONER tier,
                       parallel — everything not in the ordered head, minus
                       _PHASE3_DEFERRED)
                                 │
                                 ▼
                                END
```

Per-domain `RetryPolicy` (spec §2.6) exists so one collector class doesn't
force a shared `retry_on` on every other domain: vsphere retries
pyVmomi/`OSError` connection classes, the HTTP-backed domains (`octopus`,
`vuln`, `cicd`, `iac`, `cloud`) retry `httpx`/`requests` transport errors plus
`OSError`, everything else retries plain `OSError` only. This is one retry
layer for connection-setup/auth-expiry failures — tenacity still owns
transient in-request HTTP retries inside the collectors themselves.

### Partial-sweep semantics

- **`succeeded_domains` (drift suppression, TRK-074):** the `drift` node
  computes `succeeded = {d for d, s in statuses.items() if s in
  ("completed", "partial")}` and passes it to
  `DriftDetector.detect_state_drift(succeeded_domains=succeeded)`, which
  restricts the "resource disappeared" scan to those domains at the SQL
  level. A collector that failed or exhausted its retries this sweep can
  never manufacture a false "disappeared asset" event from stale data.
  `"partial"` is included alongside `"completed"` harmlessly:
  `detect_state_drift`'s domain-last-run map only ever considers
  `CollectionRun` rows with `status="completed"`, so a `"partial"` entry
  here only avoids wrongly *excluding* that domain — it grants no baseline
  it wouldn't already have.
- **Empty-set dispatch:** if every requested domain is disabled or unknown,
  `_fan_out` returns `[_JOIN]` directly (no `Send` at all) — the graph still
  reaches `join` and runs the full reconciler/reasoner tiers on zero fresh
  collector data rather than deadlocking or raising.
- **`join`'s `defer=True`** (belt-and-braces today, load-bearing once Phase 3
  adds branches of unequal depth): the barrier fires once every dispatched
  collector branch reaches a terminal status, whatever the mix — a single
  stuck/failed domain never blocks the rest of the sweep from continuing
  into reconcilers and reasoners.

### Run-state machine

Per-domain terminal statuses that land in `SweepState["statuses"]`:

| Status | Set by | Meaning |
|---|---|---|
| `completed` | `ETLConnector.run()` (`etl/base.py`) | Collector/reconciler/reasoner finished cleanly |
| `partial` | `ETLConnector.run()` | Some writes succeeded, at least one failed mid-run |
| `failed` | `ETLConnector.run()`, or the graph's collector/reconciler/reasoner wrapper on a non-retryable exception | Run aborted |
| `skipped` | graph `_dispatch` (disabled/unknown domain), or the collector wrapper (Redis lock already held / Redis unavailable) | Domain never actually ran this sweep |
| `retry_exhausted` (`RUN_STATUS_RETRY_EXHAUSTED`, `etl/base.py`) | the graph's collector wrapper only, on the `RetryPolicy`'s final attempt | A retryable exception class kept recurring past `max_attempts`; the wrapper swallows the final re-raise so the graph never aborts, and records this status instead |
| `interrupt_pending` (`RUN_STATUS_INTERRUPT_PENDING`, `etl/base.py`) | defined now (TRK-072), **not yet written by any Phase 2 code path** | Reserved for Phase 3's remediation interrupt/approval gate; `callbacks/freshness.py::check_collection_health()` and `agents/fleet_health.py` already classify it as in-progress-equivalent (no alert) so dashboards don't need a Phase 3 change to render it correctly once it starts appearing |

`retry_exhausted` is classified as failed-equivalent (alerts) by
`check_collection_health()`/`fleet_health.py`; `interrupt_pending` is
classified as in-progress-equivalent (no alert) — both extended into
`api/_helpers.py::_RUN_STATUS` and `chat/tools.py`'s status `CASE` (TRK-075).

### Checkpointing

`run_sweep()` resolves the process-wide async checkpointer
(`checkpointer.get_async_checkpointer()` — `AsyncPostgresSaver` when
`POSTGRES_URL` is real Postgres, `MemorySaver` otherwise) once, then invokes
the compiled graph with:

- `config={"configurable": {"thread_id": str(sweep_id)}}` — each sweep gets
  its own checkpoint thread, so concurrent sweeps never collide.
- `durability="exit"` — checkpoint state is only persisted at graph exit,
  not after every superstep. This is deliberately NOT resumable
  mid-sweep; a crash mid-run loses in-flight progress and the next sweep
  starts fresh. Full step-by-step durability (`durability="sync"`) is
  reserved for the Phase 3 interrupt/approval graphs, where a pending
  remediation approval genuinely must survive a restart —
  `require_postgres_checkpointer()` hard-fails a `MemorySaver` for exactly
  that future path (`run_sweep()` itself only warns, since plain sweeps can
  tolerate losing an exited checkpoint).
- **Retention** (Phase 3 Task 5, closes **TRK-069**): `retention.py`'s daily
  `prune_expired()` now also calls `prune_checkpoints(session)`, deleting
  stale rows from `checkpoints`/`checkpoint_writes`/`checkpoint_blobs`. See
  "Checkpoint retention" below for the policy and verified schema.

### The `run_sweep_sync` event-loop rule

`run_sweep_sync()` (the sync wrapper used by APScheduler jobs and the sync
webhook handlers) does **not** call `asyncio.run(run_sweep(...))`. Instead it
routes every call through one module-singleton daemon-thread event loop
(`_get_sync_loop()` + `asyncio.run_coroutine_threadsafe`). Reason: the async
checkpointer is a process-wide `AsyncPostgresSaver` wrapping one psycopg
async connection pool, and that pool binds to whichever event loop first
awaits it. `asyncio.run()` spins up and tears down a fresh loop per call —
a scheduler thread and a webhook thread each calling `asyncio.run(run_sweep(...))`
would each get their own loop, and the second caller would hand the
pool coroutines scheduled on a different (closed) loop, corrupting it.
**Never call `asyncio.run(run_sweep(...))` anywhere in this codebase — always
go through `run_sweep_sync()`.**

### Webhook routing policy

`webhooks._trigger_domain(domain, trigger_type, scope, background_tasks)` is
the single routing point for every single-domain trigger (cicd, octopus,
cloud, generic webhooks, `ansible` host-scope, `manual_sweep`):

- **`sweep_graph_enabled` AND `scope == "all"`** → routes through
  `_dispatch_sweep_bg()` → `run_sweep_sync(domains=[domain],
  run_reasoners=False, trigger_type="webhook")`. **No route-level Redis lock
  is acquired** — the sweep graph's own collector node already owns
  `domain:all` for the run's duration; taking the route-level lock too would
  double-acquire and either deadlock the legacy release/acquire dance or
  cause the webhook to skip a sweep that's already safely holding the lock.
  `run_reasoners=False` matches spec §2.4 ("reconcile yes, reasoners no" for
  single-domain webhook triggers) — reconcilers still run (they sit before
  the reasoner gate), only the reasoner tier is skipped.
- **Any scoped trigger** (e.g. per-host `ansible` dispatch) **always** stays
  on the legacy `try_acquire()` + `_dispatch_bg()` path, in both modes — the
  graph has no scope channel, and a host-scoped trigger must never silently
  widen into a full-domain sweep collection.
- **`sweep_graph_enabled == False`**: every trigger uses the legacy path,
  byte-identical to pre-Phase-2 behavior.

### Cutover runbook

Executed by the operator, after this merge — **not** part of this
program's Definition of Done. `sweep_graph_enabled` defaults to `False`, so
merging Phase 2 does not itself activate the graph.

1. **Enable the flag in staging.** Set `SWEEP_GRAPH_ENABLED=true` (and
   optionally tune `SWEEP_GRAPH_SCHEDULE`, default `"0 */4 * * *"`) in the
   staging environment only.
2. **Single-domain soak.** Trigger one domain's webhook (or
   `manual_sweep` scoped to one domain) repeatedly against staging; confirm
   `collection_runs` rows, drift suppression, and dashboard status
   rendering all look correct for that domain before widening scope.
3. **Full-sweep soak, ≥1 week.** Let the scheduled `infra_brain_sweep_graph`
   cron run unattended in staging for at least a full week, across every
   collector/reconciler/reasoner combination the real cadence produces.
   Watch for: `retry_exhausted` rates per domain, Redis lock contention
   between the sweep and any still-running per-domain crons, and
   checkpoint-table growth rate (the daily `prune_checkpoints` job — see
   "Checkpoint retention" — bounds this, but a soak still validates the
   default 30-day window is generous enough for real interrupt lifetimes).
4. **Flip the default.** Only after the staging soak is clean, change
   `sweep_graph_enabled`'s default (or set `SWEEP_GRAPH_ENABLED=true` in
   deployed config) to activate the graph on the deployed stack.
5. **Collector crons auto-skip — no further action needed.** `scheduler.py`
   already makes this conditional: once the flag is on, `start()` skips
   registering the per-domain cron for every `sweep_members()[Tier.COLLECTOR]`
   domain (they're now collected inside the sweep graph's dispatch/fan-out)
   and registers exactly one `infra_brain_sweep_graph` job instead. Reporter,
   reasoner, and on-demand crons (fleet_health, coverage, discovery, drift,
   compliance, the vsphere pulse, query/health) are registered unconditionally
   in both modes and need no operator action. `remediation`/`drift_learning`
   keep their own crons in both modes until Phase 3.

## Reasoner-tier LLM features (Phase 3)

Three features are strictly opt-in — every default is `False`, and each
default-off config keeps its agent's prior deterministic behavior
byte-identical. All three are `config.py` / `.env.example` settings:

| Flag | Default | Agent / graph | Enable prerequisite |
|---|---|---|---|
| `rootcause_llm_enabled` | `False` | `RootCauseAgent` (`agents/rootcause.py`) | A real-model structured-output smoke run (TRK-077) — `with_structured_output(RootCauseFinding)` has only been exercised against a stub (`FakeListChatModel`) in CI; verify the finalization call against the real provider before flipping this on anywhere real drift is scored. |
| `compliance_gap_finder_enabled` | `False` | `ComplianceAgent` (`agents/compliance.py`) | A real-model smoke run of the LLM-assisted gap-proposal method — same rationale as above (no structured-output/tool-call has been exercised against a live model in CI). Also confirm `_stable_gap_hash` wording consistency per TRK-079 before relying on idempotent re-runs on a deployed stack. |
| `remediation_interrupt_enabled` | `False` | `remediation_graph.py` | Requires a real (non-`MemorySaver`) Postgres checkpointer — `require_postgres_checkpointer()` hard-fails otherwise. Confirm the dedicated sync-loop thread (`graph._get_sync_loop`) is healthy in the target environment (APScheduler + FastAPI both drive the graph through it) before enabling on a deployed stack. |

**Policy hold (2026-07-22):** flipping any of these three flags on live data is on
explicit hold pending AWS Bedrock account access (per operator directive) — the current
lab environment's Ollama gateway is a throwaway plumbing-test workaround, not the target
provider; see docs/HANDOFF.md's reasoner-tier hold note.

### RootCause tool loop + finalization audit

Flag OFF: `RootCauseAgent._collect_deterministic()` — the original per-event
timeline-correlation logic, edge confidence pinned at `0.85`. Flag ON:
`_collect_llm()` runs a 3-phase flow per unnoted drift event (capped at
`rootcause_llm_max_events_per_run`, default 20): (1) a session gathers
per-event context + the deterministic baseline, then closes — no LLM call
ever happens inside an open `get_session()` (TRK-068); (2) a `reason()` tool
loop (`correlate_deploys`, `query_drift_events`, `query_compliance`,
`query_resource_neighborhood`) followed by a separate
`get_chat_model(role="rootcause").with_structured_output(RootCauseFinding)`
finalization call, audited via an explicit `AgentDecisionLog` row
(`iteration=999`, the `FINALIZATION_ITERATION` sentinel, PAN-scrubbed); (3) a
fresh session persists the `RootCauseNote` and — only when the finding
resolves to a concrete `Resource` by exact name (KG-2, both modes) — the
`TRIGGERED_BY` edge at `clamp(finding.confidence, 0.05, 0.99)`. A per-event
LLM exception falls back to the deterministic result for that event and
marks the run `partial`; events beyond the cap silently fall back with no
run-status change. Both `_REASON_TASK_TEMPLATE` and `_FINALIZE_PROMPT_TEMPLATE`
fence interpolated infra data as "untrusted" (TRK-077) since a compromised
collector-fed source (resource/pipeline names, config values) could otherwise
attempt to steer the model toward a false high-confidence edge.

### Remediation graph lifecycle

`remediation_graph.py` compiles a per-`ProposedAction` 3-node `StateGraph`
(`GRAPH_VERSION = "remediation-v1"`, thread_id `remediation:{action_id}`,
DEFAULT durability — not `"exit"`, since an interrupt is only resumable if
every step, including the pre-interrupt one, was actually persisted):

- **draft** — stamps `thread_id`/`graph_version` onto the row and builds the
  approval summary. No external side effects (the row itself already exists,
  created idempotently before the graph starts).
- **wait_approval** — parks on `interrupt({summary})` until a human decision
  arrives via `Command(resume={"approved": bool})`.
- **execute** — re-reads the row from Postgres and proceeds ONLY if
  `status == "approved"` — the DB row is the source of truth, so a stale
  checkpoint resumed against an unapproved row never executes. Runs the same
  `RemediationAgent._execute_one` MR path as the poll, still behind
  `INFRA_BRAIN_MR_ENABLED` + the write gate.

**Resume surfaces:** `governance_ops.py`'s approve/reject handlers and the
`webhooks.py` mirrors both call the shared `resume_remediation_action(action,
approved)` helper (all four converted to `async def`, sync DB work moved into
`asyncio.to_thread`). The resume is bounded by a 10s timeout via
`asyncio.wait_for` — a slow resume is treated as a non-fatal failure (the
graph keeps running to completion on the dedicated loop in the background;
the DB row is already updated).

**Poll fallback:** `_execute_approved()` stays registered as a daily
safety net in BOTH flag modes — resume failures (flag off, foreign
`graph_version`, timeout, or any exception) are logged and non-fatal, since
the DB row was already flipped by the caller. No approval is ever stranded
(spec §2.7).

**`GRAPH_VERSION` sweep semantics:** any `ProposedAction` whose stored
`graph_version` differs from the module's current `GRAPH_VERSION` is never
resumed by `resume_remediation_action` — it is left entirely to the poll.
This means a `GRAPH_VERSION` bump (e.g. a future graph-shape change) is safe
to ship without a data migration: old threads simply age out through the
poll instead of being resumed against a graph shape they were never
compiled for.

### Checkpoint retention (TRK-069, closed)

`retention.py:prune_checkpoints(session)`, wired into the daily
`prune_expired()` job, deletes stale rows from LangGraph's own
`checkpoints`/`checkpoint_writes`/`checkpoint_blobs` tables (never
ORM-mapped — raw SQL only).

**Verified schema** (langgraph-checkpoint-postgres 3.1.0, read directly from
`.venv/Lib/site-packages/langgraph/checkpoint/postgres/base.py`'s
`MIGRATIONS` list — do not assume without checking again on a version bump):

- `checkpoints(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
  type, checkpoint JSONB, metadata JSONB)` — PK `(thread_id, checkpoint_ns,
  checkpoint_id)`. **No `created_at` column** — the checkpoint's own
  timestamp lives inside the JSONB payload at `checkpoint->>'ts'` (ISO 8601
  string; see `langgraph.checkpoint.base.Checkpoint.ts`).
- `checkpoint_blobs(thread_id, checkpoint_ns, channel, version, type, blob
  BYTEA)` — PK `(thread_id, checkpoint_ns, channel, version)`.
- `checkpoint_writes(thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
  channel, type, blob BYTEA, task_path)` — PK `(thread_id, checkpoint_ns,
  checkpoint_id, task_id, idx)`.

**Policy:** a `thread_id` is eligible for deletion only when both hold: its
newest checkpoint's `checkpoint->>'ts'` is older than
`retention_checkpoints_days` (default 30); and it is NOT referenced by any
`ProposedAction` with `status IN ('pending', 'approved')` — the cheap,
indexed protection query enabled by Task 1's `proposed_actions.thread_id`
column (retention never scans checkpoint blobs to reconstruct interrupt
state). Guard: the three tables may not exist at all (fresh env, or a
deployment that only ever used `MemorySaver`) — `prune_checkpoints` inspects
the schema first and skips cleanly, returning 0.

## Observability (Phase 4)

### Tracing chain

Every LLM tool call flows through one ordered callback chain built by
`callbacks/registry.py`. There are two builders -- `build_callbacks()` (the
default, thread-pool path used by scheduler-driven agent runs) and
`build_async_callbacks()` (the two FastAPI event-loop call sites: `chat/agent.py`'s
`stream_response` and `api/routers/ui.py`'s `custom_view`) -- and both share the
same *shape*, differing only in which audit/observation handlers are sync vs
async:

```
build_callbacks():        AuditCallbackHandler
                        -> ReadOnlyToolValidator
                        -> DLPCallbackHandler
                        -> ObservationCallbackHandler
                        -> [Langfuse CallbackHandler]   (appended last, only if configured)

build_async_callbacks():  AsyncAuditCallbackHandler
                        -> ReadOnlyToolValidator
                        -> DLPCallbackHandler
                        -> AsyncObservationCallbackHandler
                        -> [Langfuse CallbackHandler]   (appended last, only if configured)
```

`maybe_langfuse_handler()` (`callbacks/langfuse_handler.py`) returns `None`
unless `langfuse_enabled=True` AND `langfuse_host`/`langfuse_public_key`/
`langfuse_secret_key` are all set -- in that case both builders return their
original 4-handler chain unchanged (verified in
`tests/callbacks/test_registry.py`/`test_langfuse_handler.py` via a `sys.modules`
assertion: the `langfuse` package is never imported at all when disabled).
When configured, the handler is constructed once per process (`_HANDLER_MEMO`)
and appended **last** on both chains.

**The sync-handler exception, and Langfuse's place in it.** `registry.py`
already documents -- predating this phase -- that `ReadOnlyToolValidator` and
`DLPCallbackHandler` stay synchronous even inside `build_async_callbacks()`:
their `_deny()` path must durably persist a denial record before the
`PermissionError` raises, so they are deliberately not folded into the async
batch writer. Langfuse's `langchain.CallbackHandler` joins this same
documented exception rather than creating a new one: its callback methods only
enqueue data onto Langfuse's own OTel batch span processor (no inline network
I/O on the calling thread), and both construction and callback errors are
swallowed internally by the SDK instead of raising into the agent -- so
appending it to the FastAPI event-loop chain does not reintroduce blocking I/O
(CLAUDE.md constraint #2) or an unguarded sync handler on the async path
(CLAUDE.md constraint #3 as previously applied). This is a deliberate,
verified deviation from the original Phase 4 design intent that "the callback
handler is async" -- see TRK-084 and ADR-0002's outcome note for the durable
record of that deviation.

**Masking.** PAN-shaped content never leaves the process. The Langfuse client
is constructed with `mask_otel_spans=_mask_otel_spans` -- an export-stage
OpenTelemetry span-masking hook (verified against the installed
`langfuse==4.14.0` SDK's `MaskOtelSpansFunction`/`MaskOtelSpansParams`/
`OtelSpanPatch`/`MaskOtelSpansResult` types, not the simpler `mask` hook). It
walks every span's string attributes (and string-containing list/tuple
attributes, since OTel allows `Sequence[str]` values) through the existing
`redact_pans()` from `callbacks/dlp.py` -- reused unmodified, the same PAN scrub
the DLP callback itself performs -- and only emits a patch for spans that
actually changed. This is the entire scrub surface; no other masking layer sits
between LangChain and the exported trace.

### Flag matrix (updated)

| Flag | Default | Component | Enable prerequisite |
|---|---|---|---|
| `sweep_graph_enabled` | `False` | `graph.py` sweep StateGraph | See "Sweep graph" cutover runbook above. |
| `rootcause_llm_enabled` | `False` | `RootCauseAgent` | Real-model structured-output smoke run (TRK-077). |
| `compliance_gap_finder_enabled` | `False` | `ComplianceAgent` | Real-model smoke run of the LLM gap-proposal method (TRK-078/079). |
| `remediation_interrupt_enabled` | `False` | `remediation_graph.py` | Real (non-`MemorySaver`) Postgres checkpointer; healthy dedicated sync-loop thread. |
| `langfuse_enabled` | `False` | `callbacks/langfuse_handler.py` | (1) The self-hosted Langfuse v3 compose stack (`docker/langfuse/`) must be deployed and reachable -- operator-only, manual deploy, never CI (see `docker/langfuse/README.md`); its disk/RAM sizing (~25 GiB / ~11 cpus nominal, ClickHouse's 8 GiB floor) is a hard operator gate given this host's 2026-07-12 100%-disk incident -- check `df -h` before standing it up. (2) `LANGFUSE_HOST`/`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` provisioned into Bitwarden per the repo secrets policy, never hard-coded or committed (`docker/langfuse/.env` is gitignored). Flipping the flag with any of the three unset is a silent no-op -- `maybe_langfuse_handler()` returns `None` and tracing stays off. |

The three reasoner-tier flags are unchanged from Phase 3 (see "Reasoner-tier
LLM features" above); `langfuse_enabled` is Phase 4's addition and follows the
identical opt-in-with-verified-prerequisite pattern -- every default is `False`
and flipping any of these five flags never changes another flag's behavior.

### Dead-man architecture

Detecting "the scheduler stopped running jobs" needs a path that survives the
scheduler itself dying, so it is deliberately split across three
independent layers rather than one component watching itself:

1. **Per-domain freshness, in-process** (`callbacks/freshness.py`). Each
   domain's expected cadence comes from its `AgentSpec.max_staleness` (single
   source of truth, see "The AgentSpec contract" above) -- `check_freshness()`
   and `check_collection_health()` compare each domain's most recent
   completed/partial `CollectionRun` against that window and flag
   never-started / stale / silently-empty domains. This layer detects a
   *domain* going quiet even while the scheduler process itself is healthy.
2. **Scheduler heartbeat, in-process loop** (OB-1). `record_scheduler_heartbeat()`
   runs from the hourly `_collection_health_job` (an APScheduler job) and
   writes a timestamp into the generic `AgentConfigSetting` key-value store
   (no migration needed). `check_scheduler_deadman()` /
   `get_scheduler_heartbeat_status()` read that same key from an INDEPENDENT
   execution path -- an asyncio task in `main.py`'s FastAPI lifespan, not an
   APScheduler job -- so a fully-stopped scheduler (the thing that would
   otherwise have to self-report its own death) can still be detected. The
   2-hour default threshold carries one full missed-cycle of slack over the
   hourly write. This is surfaced at `GET /api/ops/heartbeat` ->
   `{heartbeat_age_seconds, max_age_seconds, stale}`.
   **Deployment note (F18):** the embedded in-app scheduler is OFF by default
   (`embedded_scheduler_enabled=False`) — the API process registers NO
   APScheduler jobs, so the heartbeat is *written* by the dedicated scheduler
   service (`k8s/scheduler.yaml` / compose `scheduler`, replicas:1) and *checked*
   by the API's lifespan asyncio task. This is what keeps the two paths
   independent, and it stops every unlocked job (health alerts, retention prune,
   drift catch-up) from firing once per API replica (CLAUDE.md constraint #9).
   Set `EMBEDDED_SCHEDULER_ENABLED=true` only for a single-process all-in-one
   deployment with no dedicated scheduler.
3. **Sidecar prober, out-of-process** (`docker/deadman-prober/prober.py`).
   A standalone container -- **never imports `infra_brain`**, by design, since
   it runs outside the app's trust boundary and only ever sends synthetic
   health strings (endpoint name, up/down, heartbeat age numbers) to the ops
   webhook, bypassing the write-gate layer for the same reason (that gate
   polices the LLM/agent layer's outbound writes; this process isn't part of
   that layer). Every `PROBE_INTERVAL` (default 120s) it polls `/healthz`,
   `/health`, and `/api/ops/heartbeat`; `/healthz`/`/health` failures alert
   after 3 consecutive misses (no flapping spam on a single blip), and the
   heartbeat endpoint alerts ONLY when the endpoint body itself reports
   `stale=True` -- never merely on the endpoint being briefly unreachable, and
   never on a few minutes of staleness given the hourly write cadence. All
   alerting is edge-triggered with a matching recovery message on clear, no
   repeat spam per loop iteration. Wired into `docker/docker-compose.yml` as
   the `deadman-prober` service (`restart: always`, log-capped,
   resource-limited).

**Deferred (post-program, TRK entry in TRACKER.md):** the sidecar prober only
closes the in-process-container-death gap. If the whole VM/pod -- prober
included -- dies, nothing currently observes that from off-host. That needs an
external monitor (e.g. an n8n cron elsewhere polling the same three
endpoints) and is explicitly out of scope for this program.

### Sweep dashboard view

`GET /api/dashboard/sweeps` and `GET /api/dashboard/sweeps/{sweep_id}`
(`api/routers/fleet.py`) group `CollectionRun` rows by `sweep_id`, reporting
per-sweep start/finish window, duration, and per-tier domain status (tier
resolved via `etl.spec.agent_specs()[domain].tier`, falling back to
`"unknown"` for any domain with no spec) across all real run statuses --
`completed`/`partial`/`failed`/`retry_exhausted`/`interrupt_pending`/
`in_progress`/`skipped`. The detail endpoint returns one row per domain in
that sweep. Max-iters/recursion-limit hit rate is surfaced per-domain and
per-sweep by joining `AgentDecisionLog.run_id` against the sweep's
`CollectionRun` ids and reusing `callbacks/anomaly.py`'s
`RECURSION_LIMIT_MARKER` predicate (not reimplemented -- the same marker
constant, grouped by run instead of by agent/window). The React page
`dashboard-app/src/pages/Sweeps.tsx` (route `/sweeps`, under Operations)
renders a per-tier status grid (collectors/reconcilers/reasoners columns plus
a 4th "other/unknown" column so no domain silently disappears) and a
max-iters-hit-rate stat tile, built the same way as the
rest of `dashboard-app/` (sources -> `npm run build` -> committed
`src/infra_brain/dashboard/static2/` artifact).

### Logging config

`logging_config.py::configure_logging()` is called by every long-running
process (`main.py`'s lifespan, `scheduler.run_scheduler_forever`, and
`mcp_server.py`'s `__main__` entrypoint -- all three now share one code path,
closing the last accidental-consistency gap) so every container emits the
same envelope to stdout, required for `docker logs`-based triage
(`/sweep-debug` layer 4) to parse consistently across containers.

- **Default: JSON envelope** (`JsonFormatter`) -- one line per record:
  `{"timestamp", "level", "logger", "message", ...}` plus `exc_info` when
  present.
- **Extra-field passthrough:** anything passed via
  `logger.info("msg", extra={"key": val})` lands as a plain attribute on the
  `LogRecord`; `JsonFormatter` folds every attribute NOT in the standard
  `LogRecord` attribute set into the envelope (falling back to `repr()` for
  anything that isn't JSON-serializable) instead of silently dropping it --
  callers can attach `domain`/`run_id`/`sweep_id`/etc. and it survives into
  the emitted line.
- **`LOG_FORMAT=text` toggle** (local dev only): swaps in `_TextFormatter`, a
  plain `%(asctime)s %(levelname)s %(name)s: %(message)s` line with the same
  extra-field passthrough appended as `key=value` pairs, so extras are never
  dropped in either mode.
- `configure_logging()` calls `logging.basicConfig(..., force=True)` -- wins
  even if a library already attached a root handler first (the exact bug that
  once left the scheduler container emitting zero `docker logs` output).

### Truncation counters

Two collectors write one `AgentActionLog` row per truncation event, when a
cap is actually hit, mapped onto the *existing* `AgentActionLog` columns (no
migration): `octopus.py`'s `_deep_events` (EVENT_CAP) and `vuln.py`'s
`_bounded_assets` (`rapid7_vuln_asset_cap`). Both populate
`tool="truncation"`, `args_summary=json.dumps({"cap": ..., "dropped_count":
..., "entity": ...})` (`entity` is `"octopus_events"` or
`"rapid7_vuln_asset_cap"`), `verdict="allow"`, `status="ok"`,
`run_id=str(self._active_run_id)`, and `agent`/`domain` from `self`. The
pre-existing `log.warning`/`logger.warning` truncation lines are unchanged and
kept alongside the new row -- this is additive audit trail, not a replacement.
The write is best-effort: a DB failure writing the audit row is logged
(`.exception`) and never raised, since truncation itself must never fail the
collection; no row is written when the count stays under cap.

**Deliberate fresh-session deviation:** both writers use a *fresh*
`get_session()` for the truncation-audit write rather than the caller's
in-flight collection session. This is intentional, not a session-discipline
slip -- the audit row must survive even if the collection transaction rolls
back, so it cannot share a transaction boundary with the data it's reporting
on. Both `octopus.py` and `vuln.py` carry an inline comment saying so at the
write site; see TRK-081 for the full history (including the corrected
`dropped_count` computation, which now excludes cutoff-window-excluded rows
from the cap-truncation count so the two loss reasons are never conflated).

#### MCP server tracing — deferred (2026-07-23, #90)

Metrics (`mcp_metrics.py`) and an audit trail (`McpAuditMiddleware` →
`AgentActionLog`) were added for the MCP server's 19 tools; real distributed
tracing was explicitly deferred. `init_tracing()`'s LangSmith
auto-instrumentation only covers LangChain/LangGraph `Runnable` invocations —
18 of 19 MCP tools are plain functions with no `Runnable`, so tracing them
would require introducing a brand-new manual-tracing pattern (no
`@traceable`/`RunTree` call site exists anywhere else in this codebase) for
a need the audit middleware's per-call `latency_ms`/`status`/`error` rows
already answer. Revisit only if a concrete need for span-level MCP tracing
emerges (see `docs/superpowers/plans/2026-07-23-mcp-observability-design.md`
Task 3 for the full reasoning) — do not build this speculatively.

## RAG knowledge store (TRK-067)

A read-only Confluence retrieval-augmented-generation knowledge store,
dark-launched behind `rag_enabled` (default `False`) so merging it changes
nothing until an operator opts in — the same opt-in-with-verified-prerequisite
pattern as the reasoner-tier flags and `langfuse_enabled` above. Its purpose is
to let the dashboard chat agent (and any MCP client) cite the internal wiki. It
adds **no new write path of any kind**.

### Read-only guarantee

The store never widens the read-only boundary (`docs/READONLY-MODEL.md`).
Confluence pages are fetched through the GET-only structural read-only client
(`tools/http_readonly.py::readonly_get` — layer 1), never raw `httpx`.
Retrieval (`embeddings.search_knowledge()`) is SELECT-only — a hybrid
cosine + full-text top-k over `document_chunks` joined to `documents` (see
"Hybrid retrieval" below) — with no INSERT/UPDATE/DELETE. The only writes anywhere in the feature are the ingest
agent's own upserts into those two knowledge tables, exactly as any collector
writes its own rows; no external system is ever mutated.

### `rag_enabled` dark-launch

`rag_enabled` defaults `False` (`config.py` / `.env.example`), consistent with
`rootcause_llm_enabled`/`compliance_gap_finder_enabled`/`remediation_interrupt_enabled`/
`langfuse_enabled`. When off — or on but with Confluence unconfigured — every
surface is inert: the ingest agent returns an empty `CollectOutcome` with no
HTTP and no DB writes, and both retrieval surfaces return `[]` without touching
the model or the DB. `search_knowledge()` additionally returns `[]` on a blank
query, or when the bound DB is not PostgreSQL (e.g. the sqlite test path), since
pgvector's `<=>` cosine operator is PostgreSQL-only.

### Hybrid retrieval: cosine + Postgres FTS, fused with RRF (TRK-297 R6)

On PostgreSQL, `search_knowledge()` computes **two** rankings over the same
filtered candidate set and fuses them:

1. the pgvector cosine kNN (unchanged, `<=>` against `document_chunks.embedding`);
2. a full-text ranking — `plainto_tsquery('english', …)` matched with `@@`
   against `document_chunks.text_tsv`, ordered by `ts_rank`.

Fusion is standard **Reciprocal Rank Fusion** (`rag/hybrid.py`,
`score(d) = Σ 1/(k + rank)`, `k = 60`). RRF fuses on rank *position*, never on
score, which is the point: cosine similarity (bounded `[-1, 1]`) and `ts_rank`
(unbounded, corpus- and length-dependent) share no scale, and any normalisation
between them would need a corpus-wide calibration that changes whenever the
embedding model or the document mix does. What this buys is the class of query a
dense embedding blurs — an exact error string, a hostname, a `TRK-nnn` id.

`rag_hybrid_enabled` (default `true`, subordinate to `rag_enabled`) is the
operator kill-switch back to pure cosine, so a ranking regression is a config
change rather than a deploy. Degradation is graceful everywhere else too: a
non-PostgreSQL dialect never reaches the FTS path; a query with no lexical terms
compiles to an empty tsquery, and RRF over a single ranking is the identity, so
the result is *exactly* the pre-R6 cosine order; and if the FTS query errors at
all (a database predating the migration) it is logged inside a SAVEPOINT and
treated as an empty ranking. The `rag_min_similarity` floor still applies to the
fused list — at its default `0.0` that is a no-op, but raising it deliberately
narrows what lexical matching can contribute.

**`document_chunks.text_tsv` is deliberately unmodeled.** It is a
`tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(text, ''))) STORED`
column with a GIN index, and it exists on PostgreSQL only. It is **not** a
`mapped_column`: SQLite validates generated-column expressions at `CREATE TABLE`
time and has no `to_tsvector`, so a `Computed()` on the model would break
`Base.metadata.create_all()` — the whole SQLite suite and `/pg-gate-check`. This
was the single blocker that kept R6 deferred. Declaring it *without* `Computed`
was the other candidate and is worse: alembic then warns
`Computed default on document_chunks.text_tsv cannot be modified` on every
`compare_metadata` run (migration-parity, `alembic check`, and the runtime
`assert_schema_current` deploy gate), and a future `--autogenerate` would propose
recreating the column without its GENERATED clause.

So the column and its index sit outside `Base.metadata`, exactly like
`ix_resources_name_trgm`. Four places must stay aligned:
`db/schema_check.py` (`_UNMODELED_COLUMNS` / `_UNMODELED_INDEXES`),
`alembic/env.py` (`_IGNORE_COLUMNS` / `_IGNORE_INDEXES`), the `after_create`
listener in `db/models/core.py` (the `create_all` path — gate-4 ORM tests, fresh
dev DBs), and migration `2a7f1c9e4d30` (the upgrade path). The migration carries
frozen literal copies of the DDL rather than importing the model constants;
`tests/test_rag_hybrid_ddl_parity.py` is what keeps the two copies honest, since
`compare_metadata` is structurally blind to a column it has been told to ignore.

R7 (cross-encoder reranking) remains deferred — see `docs/TRACKER.md` TRK-297.

### Embedding provider auto-resolution and the EMBEDDING_DIM caveat

`embeddings.py` is a provider-agnostic factory mirroring `llm.py`. Anthropic has
no embeddings API, so the provider **auto-resolves**: an explicit
`EMBEDDING_PROVIDER` override wins; otherwise it mirrors `llm_provider` when that
is `bedrock` or `openai`; otherwise it falls back to bedrock (Amazon Titan,
`amazon.titan-embed-text-v2:0`). The OpenAI alternative
(`text-embedding-3-small`) is pinned to `EMBEDDING_DIM` via the `dimensions=`
param so both providers emit the same vector width.

`EMBEDDING_DIM` defaults to `1024` and is a one-way commitment: it must match
both the chosen model and the `document_chunks.embedding` pgvector column width
(`_EMBED_DIM`, exported from `db.models` so the migration and the HNSW index
build at the right width). **Changing it after any documents have been indexed
requires a schema migration and a full re-embed** — the stored vectors and the
index are fixed at the old width.

### pgvector image-swap deploy note

The store needs the pgvector extension, so the stock `postgres:15` image was
swapped for the drop-in `pgvector/pgvector:pg15` (same data volume, same major
version) in `docker/docker-compose.yml`, `.gitlab-ci.yml`, and
`k8s/postgres.yaml`. The operator steps before the store works on a deployed
stack are: run the pgvector-enabled Postgres image, then `alembic upgrade head`.
The migration (`1562b407c9cb`, chained onto head `c534a2dd6870`) is hand-written
(pgvector column types are not autogeneratable), inspector-guarded (safe on both
fresh and existing DBs), runs `CREATE EXTENSION IF NOT EXISTS vector` on
PostgreSQL only, and builds an HNSW cosine index (`m=16`, `ef_construction=64`,
`vector_cosine_ops`). On sqlite the embedding column falls back to JSON (via
`Vector(_EMBED_DIM).with_variant(JSON(), "sqlite")` on the model) and every
vector path is a no-op, so the test suite runs without pgvector installed.

### Ingestion behavior

`agents/knowledge.py` (`KnowledgeAgent`, domain `knowledge`, `Tier.COLLECTOR`,
daily `35 2 * * *`, `max_staleness` 26h) is a deterministic `ETLConnector` —
there is no LLM in the ingest path. It pages the configured Confluence spaces
(`confluence_rag_spaces`, comma-separated; empty falls back to
`confluence_space_key`) via the GET-only client, strips storage-format XHTML to
text, chunks with `RecursiveCharacterTextSplitter` (~1200 chars / 200 overlap),
embeds each chunk, and upserts `Document` + `DocumentChunk` rows. Ingestion is
**incremental** — a page whose `content_hash` is unchanged since the last run is
skipped without re-embedding — and follows a **stale-not-delete** policy: within
a fully-scanned space, a `Document` whose `external_id` no longer appears in
Confluence is marked `status="stale"` (never deleted); a space whose fetch
failed is not swept at all, so a transient outage never manufactures spurious
staleness.

### Retrieval surfaces

Two thin wrappers over the same SELECT-only `embeddings.search_knowledge()`
helper expose retrieval, so the store can be queried from both the operator's
MCP client and the dashboard chat agent:

- an **MCP tool** `search_knowledge` (`mcp_server.py`), and
- a **chat tool** `search_knowledge` (`chat/tools.py`), wired into the chat
  agent's tool list (`chat/agent.py`).

Both return stale-filtered top-k chunks carrying their document title, space, and
URL, so a chat answer can cite the source wiki page.
