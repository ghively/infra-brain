# ADR-0002: Agent orchestration v2.1 — LangGraph re-adoption

**Date:** 2026-07-11 · **Status:** Accepted

## Decision
Re-adopt LangGraph for sweep orchestration as a 3-tier StateGraph (collectors → reconcilers
→ reasoners) built from a declarative AgentSpec registry — reversing Wave 4.4's removal,
which targeted a LangGraph *veneer* (no checkpointer/reducers/retry), not LangGraph itself.
Fixes land first (Phase 1), graph second. Langfuse (self-hosted, compose) over LangSmith.
Full reasoner tier (RootCause LLM, DriftLearning causal, Compliance gap-finder, Remediation
interrupt-as-wait over DB-backed approval).

## Consequences
- supervisor.dispatch() becomes a thin wrapper; SKIP_HOOK and _post_collection_hook retire.
- ProposedAction stays the approval source of truth; checkpoints are versioned and sweepable.
- docs/ARCHITECTURE.md becomes the living doc; five historical decision/evidence sets frozen.

## References
Based on a detailed internal design spec (2026-07-11) and its frozen predecessor design
(2026-06-30). Supporting evidence lives in this repo's frozen historical agent-activity and
audit records (not included in this public excerpt).

## Status/Outcome (2026-07-12)

All four phases landed. Phase 1 fixed the correctness/robustness backlog and collapsed
four shadow cadence tables onto the single `AgentSpec` registry (`etl/spec.py`). Phase 2
rebuilt the sweep as a 3-tier LangGraph `StateGraph` (collectors → reconcilers →
reasoners) with Redis dedup, per-domain retry policies, and partial-sweep-aware drift
suppression. Phase 3 delivered the full reasoner tier (RootCause LLM, DriftLearning
causal clause, Compliance gap-finder, Remediation interrupt-as-wait over a DB-backed
approval) plus LangGraph checkpoint retention. Phase 4 closed observability hygiene
(shared JSON logging, truncation counters, LangSmith cloud-egress default removed),
stood up a self-hosted Langfuse v3 tracing stack, and added a three-layer dead-man
architecture (per-domain freshness, in-process scheduler heartbeat, out-of-process
sidecar prober) plus a sweep dashboard view. See `docs/ARCHITECTURE.md`'s
"Observability (Phase 4)" section for the tracing/dead-man/logging/truncation detail.

**Flag inventory** (all default `False` — every flag flips independently, none changes
another flag's behavior):

| Flag | Component |
|---|---|
| `sweep_graph_enabled` | Phase 2 sweep `StateGraph` (`graph.py`) |
| `rootcause_llm_enabled` | Phase 3 `RootCauseAgent` LLM reasoning |
| `compliance_gap_finder_enabled` | Phase 3 `ComplianceAgent` LLM gap proposals |
| `remediation_interrupt_enabled` | Phase 3 `remediation_graph.py` interrupt-as-wait |
| `langfuse_enabled` | Phase 4 Langfuse tracing (`callbacks/langfuse_handler.py`) |

**Operator prerequisites before enabling any flag in production:**
- `sweep_graph_enabled` — a soak run of the sweep graph against a non-production
  environment, confirming no collector regressions vs. the flat `supervisor.dispatch()`
  path it replaces.
- `rootcause_llm_enabled` / `compliance_gap_finder_enabled` — a real-model
  structured-output/tool-call smoke run; both have only ever been
  exercised against a stub (`FakeListChatModel`) in CI.
- `remediation_interrupt_enabled` — a real (non-`MemorySaver`) Postgres checkpointer and
  a healthy dedicated sync-loop thread (`graph._get_sync_loop`).
- `langfuse_enabled` — the self-hosted Langfuse v3 compose stack (`docker/langfuse/`)
  deployed and reachable (operator-only, never CI; disk/RAM sizing is a hard gate given
  this host's 2026-07-12 100%-disk incident — see `docker/langfuse/README.md`), plus
  `LANGFUSE_HOST`/`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` provisioned into Bitwarden.
- A `retention_checkpoints_days` prune schedule running (default 30 days) before any of
  the above accumulate meaningful checkpoint volume, and continued monitoring of the
  checkpoint/action-log tables for unbounded growth.

**Recorded deviation:** the spec's Phase 4 line "the callback handler is async" shipped
as sync-but-backgrounded instead — `langfuse.langchain.CallbackHandler` joins the
registry's pre-existing documented sync-handler exception (`ReadOnlyToolValidator`/
`DLPCallbackHandler`) rather than introducing a new violation class, since its methods
only enqueue onto Langfuse's own OTel batch processor and never raise into the agent.
See `docs/ARCHITECTURE.md`'s "Observability (Phase 4)" section for the full
verification.
