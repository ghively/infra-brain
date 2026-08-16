# ADR 0001 — infra-brain's `lc-*` skills are a deliberate fork of langchain-lab

- **Status:** Accepted
- **Date:** 2026-06-22
- **Deciders:** A. Operator (youruser@example.com)
- **Context tags:** platform consolidation (4 repos → 2)

## Context

This project's plugin ecosystem originally spanned four repositories. Two of them carry LangChain/LangGraph
authoring skills:

- **`langchain-lab-plugin`** — a standalone, general-purpose Claude Code plugin teaching
  LangChain/LangGraph development (the upstream / origin of the patterns).
- **`infra-brain/.claude/skills/lc-*`** — a set of LangChain skills used when developing
  *this* backend service.

An audit (2026-06-22) compared the overlapping skills and found infra-brain's copies are
**50–130% larger and specialized** for this project, not drifted copies of the upstream:

| Skill | langchain-lab (bytes) | infra-brain (bytes) | Specialization added in infra-brain |
|-------|----------------------:|--------------------:|-------------------------------------|
| `lc-agent` | 46,791 | 106,332 | Event-driven agents (webhook/cron/queue), supervisor nesting, `/agent-register --schedule` references |
| `lc-test`  | 45,089 | 78,867  | RAG evaluators, LangSmith eval-gate CI, A/B workflow |
| `lc-tools` | 39,095 | 72,786  | MCP integration, advanced async/retry |
| `lc-deploy`| 43,617 | 84,872  | Postgres-checkpointing StatefulSet, Helm for the scheduler |
| `lc-lcel`  | 45,627 | 62,503  | FastAPI streaming, connection pooling |

The specializations reference infra-brain-specific machinery (the `BaseAgent`/`LLMAgent`
contract, `supervisor.py` AGENT_REGISTRY, `scheduler.py` `_DEFAULT_SCHEDULES`, the Postgres
layer, the FastAPI dashboard).

As part of consolidating to two repos (the `infra-ops` plugin + this backend),
`langchain-lab-plugin` is being **archived read-only**. This ADR records *why* and how to
treat the relationship so a future maintainer does not mistake the fork for drift and try to
"reconcile" the two — which would strip the specialization this project depends on.

## Decision

1. Treat `infra-brain/.claude/skills/lc-*` as a **deliberate, owned fork**, specialized for
   this backend. It is the **canonical** copy for any work in this repo.
2. **Do not** auto-sync the fork to `langchain-lab-plugin`, and do not "reconcile" the two to a
   single source. They have diverged on purpose.
3. If a genuinely generic improvement appears upstream later, **hand cherry-pick** the delta
   into the fork — never a wholesale sync.
4. `langchain-lab-plugin` is archived read-only. Its upstream paths (for reference) are:
   `langchain-lab-plugin/skills/{lc-agent,lc-graph,lc-lcel,lc-tools,lc-test,lc-deploy,...}` and
   `langchain-lab-plugin/agents/{lc-architect,lc-coder,lc-reviewer,lc-docs-agent}`.

## Consequences

- **Positive:** No third copy to keep in sync; the fork stays free to specialize; the
  authoring skills travel with this repo (so the `infra-ops` plugin's backend-development
  workflow gets them automatically when it materializes this repo — see the
  consolidation plan, Workstream B).
- **Negative / watch-outs:** Upstream bug fixes do not flow in automatically — they require a
  manual cherry-pick. Acceptable given how specialized the fork is.

## References

- Consolidation & Integration Plan (internal, not included in this copy), §4 Workstream A and §2A audit.
