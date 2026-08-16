> **For a human reader landing here first:** this whole `.claude/` directory is the AI
> coding-agent tooling used to build infra-brain with [Claude Code](https://claude.com/claude-code) —
> specialist subagents, reusable skills, and hooks. It splits into two layers:
> `.claude/agents/` (7 files) and roughly a dozen skills under `.claude/skills/` are
> infra-brain-specific (they understand this project's domain — safety callbacks,
> drift detection, the sweep graph); everything else under `.claude/skills/` (~30
> files) is a bundled, reusable, generic LangChain/LangGraph authoring toolkit
> ("langchain-lab") that isn't specific to this project — see `.claude/PLUGIN_SPEC.md`
> for its own design doc. The rest of this file is written for Claude Code itself, not
> a human reader — it's the operational reference the agent sessions load.

> Claude Code subagent/tooling reference (moved from repo-root AGENTS.md on 2026-07-11
> so "agents" stops meaning two things). Repo-root AGENTS.md now documents infra-brain
> DOMAIN agents.

# infra-brain — Agent Registry

Quick reference for all Claude Code subagents in this repository.
Full definitions: `.claude/agents/`. Full skill list: `.claude/plugin.json`.

## Orchestration is a skill-driven MODE, not a subagent (redesigned 2026-07-22)

There is no `orchestrator` subagent anymore — see
`docs/decisions/2026-07-22-orchestrator-redesign-plan.md`. Subagents in this harness
have no `Agent`/`Task` tool available to themselves (confirmed empirically), so a
spawned "orchestrator" could never actually fan out to specialists as documented —
it silently did everything serially instead, skipping every reviewer dispatch it
claimed to make. `Agent(subagent_type="orchestrator", ...)` no longer resolves to
anything; **never call it.**

Instead: **invoke `Skill(skill="orchestrator")`**, which loads
`.claude/skills/orchestrator/SKILL.md` into whichever session is doing the work
(almost always the top level). That session then decomposes the task itself and
dispatches specialist `Agent()` calls directly, in parallel batches, with
`isolation:"worktree"` mandatory for anything code-writing (mechanically enforced by
the `require-worktree-isolation` `PreToolUse` hook) and a rolling branch-integration
procedure once those dispatches finish. Full detail lives in the skill file — this
README just points at it.

**Invoke the skill when:** building/modifying agents, tools, callbacks, routes,
schemas; debugging; migrations; deployment.
**Skip for:** frontend edits in `dashboard-app/` (the `/dashboard2` React SPA), simple
questions, status checks, git operations.

---

## Research

| Agent | Purpose |
|---|---|
| `infra-researcher` | Read-only context gathering. Dispatched in parallel during orchestration-mode decomposition, before implementation. Reads source files, registries, test structure, git history. Never writes. |

---

## Reviewers (always run in parallel with each other)

| Agent | Trigger | Checks |
|---|---|---|
| `lc-safety-reviewer` | After touching callbacks/, supervisor.py, tools/ | Callback wiring, DLP bypass paths, read-only enforcement, async/sync mismatches |
| `lc-migration-reviewer` | After generating an Alembic migration | NOT NULL without default, DROP TABLE, missing CONCURRENTLY, missing lock_timeout |
| `lc-api-reviewer` | After adding/modifying FastAPI routes | async/sync correctness, missing auth, sync DB calls, missing response_model |
| `lc-agent-completeness` | After adding a new domain agent file | 4 wiring points: agent file, test file, AGENT_REGISTRY, _DEFAULT_SCHEDULES |

**Multi-file change touching ≥2 reviewer domains:** there's no `review-coordinator`
agent anymore (retired alongside `orchestrator` for the same spawn-capability reason)
— classify the changed files against the matrix above yourself and dispatch the
applicable reviewers directly, in one parallel batch. See the orchestrator skill's
"Review batches" section for the exact routing matrix + prompt templates.

**Note:** `lc-architect`, `lc-coder`, and `lc-reviewer` are also specialist agents but are
defined as skills (`.claude/skills/<name>/AGENT.md`), not under `.claude/agents/` — see the
Agent Files section below.

| Agent | Trigger | Checks |
|---|---|---|
| `lc-architect` | When architectural design exceeds a single skill's scope | Full architecture document: state schema, graph topology, RAG pipeline, memory, risks |
| `lc-coder` | When a task needs a complete, production-quality LangChain/LangGraph file, not a snippet | Produces the file directly |
| `lc-reviewer` | Invoked by `/lc-review` or as a pre-merge quality gate | Seven-dimension review, high-confidence findings only, file:line + fix |

---

## DB-Backed Analysis (require postgres-infra MCP)

| Agent | Purpose |
|---|---|
| `sweep-health` | Cross-domain collection_runs status. Reports per-domain last-run, status, resource count, drift count. Flags overdue/failed. |
| `drift-analyst` | Per-domain drift delta between the last 2 completed runs. Reports new/resolved/persistent events and resource count delta. |

---

## Work Classification

| Work type | Use |
|---|---|
| New agent end-to-end | `/agent-register` (orchestration-mode decomposition) |
| New FastAPI tool | `/tool-register` (orchestration-mode decomposition) |
| Database schema change | `/migration-create` (orchestration-mode decomposition) |
| Multi-file feature | orchestration mode → parallel specialists, worktree-isolated |
| Frontend edit (`dashboard-app/`, the `/dashboard2` React SPA) | Edit/Write directly (no orchestration mode needed) |
| Sweep failure | `/sweep-debug` directly |
| Pre-deploy | `/deploy-check` directly |
| Health check | `/dev-status` directly |
| LangChain code review | `lc-reviewer` agent or `/lc-review` |

---

## Agent Files

```
.claude/agents/
  infra-researcher.md      ← read-only context gatherer
  lc-safety-reviewer.md    ← callback + safety checks
  lc-migration-reviewer.md ← Alembic migration safety
  lc-api-reviewer.md       ← FastAPI route correctness
  lc-agent-completeness.md ← 4-point wiring validation
  sweep-health.md          ← cross-domain sweep status
  drift-analyst.md         ← per-domain drift comparison

.claude/skills/orchestrator/SKILL.md ← orchestration MODE: decomposition, model
                                        routing, worktree lifecycle, routing tree
                                        (replaces the retired orchestrator +
                                        review-coordinator agents, 2026-07-22)
.claude/skills/lc-architect/AGENT.md ← deep LangChain/LangGraph architecture
.claude/skills/lc-coder/AGENT.md     ← LangChain/LangGraph code generation
.claude/skills/lc-reviewer/AGENT.md  ← LangChain/LangGraph code review
```
