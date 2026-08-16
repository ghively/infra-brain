# Dev tooling / automation ecosystem audit — 2026-07-22

- **Status:** Findings recorded. Items 2-7 not yet implemented (queued). Item 1
  (orchestrator model) is being taken further by a follow-up planning pass — see
  `docs/decisions/2026-07-22-orchestrator-redesign-plan.md` once that lands.
- **Author:** Fable5 subagent audit, synthesized by Claude Code session.
- **Scope:** Claude Code skills/subagents/hooks, CI/CD, scripts, testing practices,
  tracking docs, and the orchestration/parallelism model. Read-only audit, no files
  changed by the audit itself.

## Headline finding — the documented orchestrator model is structurally broken for background dispatch

`.claude/agents/orchestrator.md` is built entirely around "spawn other agents, never
write code yourself" — but when spawned as a background subagent (exactly how
`CLAUDE.md`'s "How Work Gets Done" section instructs), it has **no `Agent`/`Task` tool
available to itself** and cannot fan out. Confirmed twice independently: (1) today's
three implementation subagents each reported this verbatim mid-task, and (2) the
auditing Fable5 subagent itself checked its own tool list and found the same gap. It
silently degrades to doing everything serially and skipping specialist review steps
(`lc-safety-reviewer` etc.) it's supposed to invoke. `review-coordinator` is broken in
**every** invocation for the identical reason — it is always a subagent, and its whole
job description is "spawns reviewers in parallel via Agent tool calls."

What still works: the **top-level** session has the `Agent` tool, and parallel
specialist fan-out from there is real (this is what has actually been happening all
session whenever real parallelism occurred).

**Recommendation:** invert the model — orchestration should be a top-level *mode*
(invoke the orchestrator skill for decomposition, then fan out via `Agent()` from the
top level directly) rather than a spawnable subagent. Convert `review-coordinator` into
a skill for the same reason. Pure quality gain, not a speed/quality tradeoff — specialist
reviews would actually run instead of being silently skipped. **Deferred to the
follow-up planning pass** rather than implemented immediately, since the maintainer wants this
redesign to also cover per-subagent worktree lifecycle and dynamic model routing —
see the forthcoming plan doc.

## Other findings (ranked, not yet implemented — queued for a later pass)

| # | Finding | Fix | Effort |
|---|---|---|---|
| 2 | Today's worktree-collision class was a **repeat** — already documented once, happened again | `PreToolUse` hook blocking code-writing `Agent` dispatches lacking `isolation:"worktree"` (explicit override token); same pattern as the existing recursion-blocking hook | Small |
| 3 | The SQL validator (one of 2 hard MR gates) doesn't scan `api/routers/`, where real route handlers with raw SQL now live — the skill itself warns about this | Add that directory to the scanned file list | Small |
| 4 | Every doc/skill (`.venv/Scripts/python.exe`, 30+ occurrences) references a Windows path absent on this Linux host | Repo-wide fix to `.venv/bin/python` | Trivial |
| 5 | Today's schedule-collision and env-parity bugs were both catchable by tests that already exist — nothing runs them on combined-branch state pre-merge | One fast no-DB CI job running just those specific tests (~20 lines); explicitly NOT reinstating the pruned lint/coverage/k8s-lint gates | Small |
| 6 | `TRACKER.md` is ~73K tokens — even the auditing Fable5 agent couldn't read it in one pass | Split into open/recent + a verbatim archive, same never-delete discipline | Small |
| 7 | Orchestrator skill itself contains a stale, actively wrong instruction ("merge directly to master, no PRs") contradicting real MR-gated policy | One-line fix | Trivial |

**Explicitly recommended against** (so nothing gets over-built): adopting the
task-board-coordination machinery (today's collisions are the "shared mutable working
tree" class, which worktree-by-default removes entirely — board machinery solves a
different, fleet-scale problem not present here); reinstating lint/coverage/k8s-lint
gates (no outage evidence supports them, hooks cover them locally); consolidating or
retiring any current project skill; changes to the migration workflow or testing
conventions beyond item 7 (traced incident history — every past failure class already
has a tested countermeasure).

## Confidence notes

Findings 1 (present-day behavior), 2, 3, 4, 6, and 7 are directly verified by reading/
measuring the cited files. Finding 1's *history* (whether nested dispatch ever worked,
per the F-014 "orchestrator recursion" incident record) is inferred, not confirmed —
worth a 2-minute probe (spawn a trivial subagent whose only task is "list your available
tools") before finalizing any redesign. Finding 5's claim that existing tests would have
caught today's bugs is verified to "the test files exist and target those invariants" —
not re-run against the pre-fix commits.
