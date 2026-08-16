# Decision: fate of the committed `.claude/` automation (F-013)

- **Date:** 2026-07 (Wave 5 item 5.6)
- **Status:** DECIDED — **KEEP-AND-HARDEN**
- **Finding:** F-013 — the operator believed the Claude automation had been
  "deleted for a fresh start", but `.claude/` remained fully committed
  (79 tracked files) with the orchestrator-default hook firing on every prompt.
  The deletion never landed on master; the tooling's state did not match the
  operator's stated intent.

## Options considered
| Option | Verdict | Why |
|---|---|---|
| DELETE (fresh start) | rejected | The skills/hooks encode real project knowledge (safety chain, agent wiring, SQL validation) that would be rebuilt from scratch; the observed failures were specific (recursion, unverified claims, token overhead), not systemic. |
| KEEP-AS-IS | rejected | The specific failures were real (F-014/F-015/F-016) and unfixed-as-is. |
| **KEEP-AND-HARDEN** | **accepted** | Keep the automation; fix the three audited failure modes with committed, testable changes. |

## The hardening (landed in Wave 5)
1. **F-014 recursion** — a structural PreToolUse hook
   (`.claude/hooks/block-orchestrator-recursion.py`) denied
   `Agent(subagent_type="orchestrator")` from any subagent context (item 5.2),
   in addition to the prompt-layer guards. **Retired 2026-07-22:** the
   orchestrator-redesign removed the `orchestrator` subagent type entirely
   (`subagent_type="orchestrator"` no longer resolves to anything), so the hook
   became permanently unreachable dead code and was deleted along with its
   `settings.json` registration. There is no orchestrator subagent left to
   recursively spawn; orchestration is now a top-level-only skill-driven mode.
2. **F-015 unverified "done" claims** — orchestrator Step 5 is now
   EVIDENCE-GATED: every completion claim requires a re-runnable artifact the
   orchestrator re-checks, else it is reported UNVERIFIED (item 5.2).
3. **F-016 per-prompt token overhead** — the orchestrator-default reminder is
   gated to substantive prompts and trimmed to 2 lines (item 5.7).

## Intent statement (supersedes the "deleted" belief)
`.claude/` is a deliberate, maintained part of this repository. Changes to it
go through the same MR review as source code. Any future "fresh start" must be
a committed deletion on master, not a local removal.
