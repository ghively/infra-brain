---
name: orchestrator
description: >
  Top-level orchestration MODE for infra-brain — not a spawnable subagent. Loads into
  whichever session is doing the work (almost always the top-level session) and
  provides: request classification, task-graph decomposition, execution-substrate
  choice (direct Agent() batches vs. the Workflow tool), a model-routing rubric,
  mandatory worktree isolation for code-writing dispatches with a rolling
  integration procedure, and evidence-gated synthesis. Invoke this skill directly;
  never call Agent(subagent_type="orchestrator") — that agent no longer exists.
disable-model-invocation: false
---

# infra-brain Orchestrator

> **Claude**: invoke this skill at the start of every non-trivial task to determine
> the correct routing before writing a single line of code or running any command.
> **You do the dispatching yourself** — this skill is decomposition + policy, not a
> hand-off to another agent.

---

## 0. You ARE the orchestrator — there is no orchestrator agent

**As of 2026-07-22, `.claude/agents/orchestrator.md` and `.claude/agents/review-coordinator.md`
are retired.** Both were subagent definitions built entirely around "spawn other agents
in parallel" — but subagents in this harness have **no `Agent`/`Task` tool available to
themselves** (confirmed empirically: a probe subagent's own tool list came back as
`Artifact, Bash, Edit, Read, ReportFindings, SendUserFile, Skill, ToolSearch, Write` —
no `Agent`, no `Task`). Spawning `Agent(subagent_type="orchestrator")` and expecting it to
fan out to specialists never worked: while that agent definition still existed, the call
silently degraded to serial self-work with every specialist-review step skipped (observed
twice on 2026-07-22). **Now that the definition is deleted, the call no longer degrades to
anything — it fails outright as an unrecognized subagent type.** See
`docs/decisions/2026-07-22-dev-tooling-audit.md` finding 1 and
`docs/decisions/2026-07-22-orchestrator-redesign-plan.md` for the full analysis.

**What this means in practice:**
- Orchestration happens **in the session that has the `Agent` tool** — almost always
  the top-level session. If you are reading this skill as a subagent, you cannot
  fan out further; do your assigned task directly and report results upward
  (`SendMessage`/your final answer) instead of trying to dispatch.
- The old `Agent(subagent_type="orchestrator", prompt=...)` pattern documented
  historically in `CLAUDE.md` is gone. **Never call it** — the subagent type no longer
  exists, and even if it did, the tool-access gap above makes it structurally unable
  to do its documented job.
- Everything the old orchestrator agent and review-coordinator agent did well —
  decomposition discipline, the reviewer-routing matrix, evidence-gated synthesis —
  is preserved below, just executed by you directly instead of delegated one level down.

---

## What is infra-brain? (Read this first if you're new)

infra-brain is a **read-only infrastructure audit agent** built on LangChain/LangGraph.
It continuously sweeps your infrastructure (Linux hosts, Windows servers, vSphere,
GitLab, Ansible IaC, Kubernetes, cloud accounts, network devices, vulnerability scanners)
and stores findings in PostgreSQL. It never mutates infrastructure — every tool call
passes through a safety callback chain that enforces this.

**The stack:**
- `src/infra_brain/agents/` — 25+ domain sweep agents (one per infrastructure domain)
- `src/infra_brain/supervisor.py` — routes requests to the right agent
- `src/infra_brain/scheduler.py` — runs scheduled sweeps
- `src/infra_brain/tools/` — read-only connectors (GitLab, Ansible, vSphere, SNMP…)
- `src/infra_brain/callbacks/` — safety boundary (read-only, DLP, audit — never weaken these)
- `src/infra_brain/db/` — PostgreSQL schema + Alembic migrations
- `dashboard-app/` — Vite+React SPA (the `/dashboard2` dashboard, the sole UI), built to `src/infra_brain/dashboard/static2/`
- `docker/` — Docker Compose stack (app + scheduler + postgres + redis + n8n)
- `k8s/` — Kubernetes manifests (migration path from Docker already written)

**Critical guarantee:** infra-brain never writes to infrastructure. Every tool must flow
through `build_callbacks()` from `callbacks/registry.py`. Never bypass this.

---

## Quick Reference (experienced contributors)

| I want to… | Run this | Also triggers |
|---|---|---|
| Add a new infrastructure domain agent | `/agent-register <name>` | `lc-agent-completeness` subagent |
| Add a new tool to an existing agent | `/tool-register <name> <agent>` | `lc-safety-reviewer` subagent |
| Debug a sweep returning nothing | `/sweep-debug [domain]` | — |
| Add/change a database column or table | `/migration-create <message>` | `lc-migration-reviewer` subagent |
| Add or modify a FastAPI route | Write it, then… | `lc-api-reviewer` subagent dispatched |
| Touch callbacks/, supervisor.py, tools/ | Write it, then… | `lc-safety-reviewer` subagent dispatched |
| Check everything is wired correctly | `/dev-status` | — |
| Pre-deploy validation | `/deploy-check` | — |
| Debug a failing CI pipeline | `/ci-debug [pipeline_id]` | — |
| Check sweep health across all domains | `sweep-health` agent | — |
| Compare drift between two sweep runs | `drift-analyst` agent | — |
| Multi-file change touching ≥2 review areas | "Review batches" (Step 8 below) | dispatch applicable reviewers yourself, in parallel |
| Getting started / new to this repo | `/onboard` | `dev-status` auto-run |
| Debug a LangChain error | `/lc-debug` | — |
| Write tests for an agent | `/lc-test` | — |
| Understand a LangChain concept | `/lc-explain <concept>` | — |
| Pick the right LangChain pattern | `/lc-patterns [description]` | — |
| Add retry / fallback / circuit breaker | `/lc-resilience` | — |
| Add conversation memory | `/lc-memory` | — |
| Add guardrails / PII protection | `/lc-guardrails` | — |
| Add LangSmith tracing to a file | `/lc-trace <file.py>` | — |
| Switch or add an LLM provider | `/lc-providers [provider]` | — |
| Review LangChain code for issues | `/lc-review [file.py]` | — |
| Fetch live library documentation | `/lc-docs <topic>` | — |
| Audit for security vulnerabilities | `/lc-guard [path]` | — |
| GDPR right-to-erasure workflow | `/lc-erase <user_id>` | — |
| Scaffold project/agent/graph/RAG file | `/lc-scaffold [type]` | — |
| Validate UI SQL column references | `/validate-sql` | — |
| Build an entirely new LangChain project | `/lc-start` | — |

---

## Step 1 — Classify the Request

| Type | Description | Examples |
|---|---|---|
| **SIMPLE** | Single-step, single-agent task | "What does the linux agent collect?", "explain StateGraph" |
| **COMPOUND** | Multi-step, sequential dependencies | "Add a migration, review it, apply it" |
| **PARALLEL** | Multiple independent tasks | "Review this agent for safety AND completeness" |
| **COMPLEX** | Mix of parallel + sequential phases | "Build a new Windows Ansible sweep agent end-to-end" |

For SIMPLE requests: route directly to the right skill/agent. No decomposition needed,
no worktree ceremony, no model-routing table — just do it or dispatch the one agent.
For everything else: proceed to Step 2.

---

## Step 2 — Decompose into a Task Graph

Break the request into atomic tasks. For each task, identify:

- **ID** — T1, T2, T3…
- **Type** — `research` | `implement` | `validate` | `verify`
- **Agent/Skill** — which specialist handles it
- **Depends on** — which task IDs must complete first (empty = can run immediately)
- **Output** — what this task produces for downstream tasks
- **Writes files?** — yes/no. Drives the Step 5 worktree-isolation decision.
- **Declared file touches** — the specific files/dirs this task expects to write, with
  any of the known shared files (a findings tracker doc if one exists in this
  repo, `AGENTS.md`, `config.py`, `.env.example`, `supervisor.py` AGENT_REGISTRY,
  `scheduler.py` schedules) flagged
  explicitly. Two `implement` tasks declaring the same **non-shared** file → serialize
  them or re-cut the task boundary; overlap on a **shared** file is fine — Step 6's
  playbook handles it by convention, not by serialization.

**Task type rules:**
- `research` tasks are ALWAYS parallelizable — they are read-only and independent
- `validate` tasks (subagents: lc-safety-reviewer, lc-agent-completeness, etc.) are ALWAYS parallelizable with each other
- `implement` tasks: parallelizable if isolated in their own worktree (Step 5) —
  file-overlap is no longer a blocker for parallelism, only for *serialization at
  integration time* if it's an undeclared, non-shared-file collision
- `verify` tasks (tests, lint, SQL check): parallelizable with each other, depend on implement

**Dependency rule:** If task B needs output from task A, B depends on A.
If tasks A and B are both independent, put them in the same parallel batch.

### Example decomposition: "Add a new Windows Ansible sweep agent"

```
T1 [research, no deps, writes: none] — infra-researcher: read windows.py, linux.py, ansible.py for patterns
T2 [research, no deps, writes: none] — infra-researcher: check supervisor.py AGENT_REGISTRY format
T3 [research, no deps, writes: none] — infra-researcher: check scheduler.py _DEFAULT_SCHEDULES format
                          ↓ (T1, T2, T3 complete)
T4 [implement, deps: T1,T2,T3, writes: agents/windows_ansible.py, tests/agents/test_windows_ansible.py, supervisor.py(shared), scheduler.py(shared)]
   — skill: /agent-register windows_ansible --schedule "0 3 * * *"
                          ↓ (T4 complete)
T5 [validate, deps: T4, writes: none] — agent: lc-agent-completeness
T6 [validate, deps: T4, writes: none] — agent: lc-safety-reviewer
                          ↓ (T5, T6 complete)
T7 [verify, deps: T4, writes: none]   — Bash: pytest tests/agents/test_windows_ansible.py -v
T8 [verify, deps: T4, writes: none]   — skill: /dev-status
```

Parallel batches: [T1, T2, T3] → [T4] → [T5, T6] → [T7, T8]

---

## Step 3 — Choose Your Execution Substrate

Two ways to actually run the batches from Step 2 — pick per task graph, not globally:

### Default: direct `Agent()` batches (no opt-in required)

**CRITICAL RULE: when dispatching a batch with multiple independent tasks, send ALL
`Agent`/`Skill` tool calls for that batch in a SINGLE response.** This is the difference
between parallel and sequential execution — sending T1, wait, T2, wait, T3 is three
times slower than sending all three in one response.

Use this for anything with ≤3 code-writing tasks and no multi-stage per-item shape.
It's simpler, needs no script authoring, and covers the vast majority of real requests.

**Pass context between batches.** When you dispatch a second (or later) batch after an
earlier one completes, carry the earlier batch's relevant results forward into the new
batch's prompts — a file path that `/agent-register` just created, a branch name/tip SHA,
a research finding, a validator verdict. Include that output verbatim in the downstream
task's prompt; do not start each batch cold and do not assume a downstream agent can
rediscover the earlier output on its own (it runs in a fresh context and generally
cannot). This applies to plain `Agent()` batches; in `Workflow` mode `pipeline()` threads
stage outputs for you.

### Opt-in: the `Workflow` tool

`Workflow` gives you `parallel()`/`pipeline()` primitives with built-in concurrency
capping, per-agent `isolation:'worktree'` and `model:`/`effort:` overrides, and (if a
token budget was set) cost-aware scaling via `budget`. It is genuinely better than
hand-batched `Agent()` calls for **pipelined multi-stage work** (e.g. "for each of 5
independent items: implement → test → review" — `pipeline()` overlaps stages across
items with no barrier, which manual batching cannot express at all).

**It is gated: you may only invoke `Workflow` when the user has explicitly opted in** —
they said "ultracode", have ultracode on for the session, explicitly asked for "a
workflow" or "multi-agent orchestration" in their own words, or invoked a
workflow-authoring skill/named workflow. Describing the *capabilities* he wants
(parallelism, worktrees, model routing) is NOT the same as saying the trigger phrase —
don't assume opt-in from that alone.

**Recommend it to the user** (don't just silently fall back) when the task graph has
≥4 code-writing tasks or a genuine multi-stage per-item shape: *"this task graph would
benefit from the Workflow tool's pipelining — say 'use a workflow' if you'd like me to
use it, otherwise I'll dispatch it as plain parallel batches."*

**Either substrate, the same policy layers below still apply** — Workflow doesn't
replace Steps 4–7, it just executes them with better primitives. In particular,
`Workflow` does **not** do branch consolidation (Step 6) — it hands back N worktree paths
and branches; folding them into one coherent result is your job either way.

> **Tool-capability note (verified, not assumed):** the `Agent` tool's `isolation:"worktree"`
> and `model:` (enum `sonnet`/`opus`/`haiku`/`fable`) parameters, and the `Workflow` tool's
> `agent()`/`parallel()`/`pipeline()` primitives with per-call `isolation:'worktree'`,
> `model`/`effort` overrides, a `budget` object, and automatic concurrency capping, have all
> been confirmed to exist exactly as documented here — directly verified against the live tool
> schemas by the top-level session (which is the only context that can see those schemas), not
> merely inferred from documentation. Future readers do not need to re-litigate whether these
> parameters are real.

---

## Step 4 — Model Routing

Four tiers: **Fable** (deep judgment), **Opus** (`claude-opus-4-8`, high-stakes
implementation), **Sonnet** (scoped implementation/review), **Haiku** (mechanical).
Apply the **first rule that fires**:

| # | If the subtask is… | Tier | Effort |
|---|---|---|---|
| 1 | Investigation, root-cause, audit, architecture/design review — the success criterion itself must be discovered; output is a diagnosis/decision/report, not a diff | **Fable** | high |
| 2 | Writes to Critical-Files-table paths or the safety chain (`callbacks/`, `supervisor.py`, `db/models/`, `config.py`, `db/session.py`, `graph.py`), or generates a migration | **Opus** | default |
| 3 | Code diff with a crisp spec in leaf/isolated scope: one domain agent module, one tool + test, one dashboard `.tsx` component, test-writing against a defined behavior | **Sonnet** | default |
| 4 | Mechanical, near-zero judgment: run a test/lint command and report, grep/registry/wiring checks, apply a precisely-specified diff, extract exact signatures/strings, draft a TRACKER row from a template | **Haiku** | default |
| 5 | Read-only research fan-out (`infra-researcher`) | **Sonnet** default; **Haiku** for pure verbatim-extraction asks | default |
| 6 | None of the above fires cleanly | **Omit `model:`** (inherit session model) | — |

**Standing modifiers:**
- **Specialist reviewer agents keep their own pinned models** (each `.claude/agents/lc-*.md`
  pins one). Don't override per-dispatch — `lc-safety-reviewer` in particular must
  never be down-tiered.
- **Escalate on stuckness, never loop:** an agent reporting uncertainty, or failing
  verification twice, gets re-dispatched one tier up with the failure context —
  cheaper than retrying at the same tier.
- **Ambiguity beats mechanics:** a task that *looks* mechanical but sits inside an
  unresolved design question routes by rule 1 (decompose better, if possible), not rule 4.
- **Budget interplay (Workflow mode):** if `budget.remaining()` is under ~2× the
  estimate for remaining dispatches, prefer the lower tier wherever rules 3/4 plausibly
  apply; never budget-downgrade rules 1–2.
- **Decomposition itself is your job, in whatever model you're already running on** —
  it is no longer a reason to spawn anything.

**Calibration status (2026-07-22):** rules 1–2 are evidence-backed (observed directly
that day). Rules 3–4 are reasoned extrapolation — treat as the working default, note
any tier-mismatch (an escalation fired, or a higher tier was clearly overkill) in
HANDOFF day-close entries, and harden the table as evidence accumulates.

---

## Step 5 — Worktree Lifecycle

### 5.1 When to isolate

**`isolation: "worktree"` is mandatory for every dispatch that may write git-tracked
files.** This is stricter than the `Workflow` tool's own default guidance ("isolate
only when agents would otherwise conflict") — deliberately so, for this repo
specifically: infra-brain tasks that look file-disjoint on paper converge on the same
shared files in practice (every task logs a row in the findings tracker, where one
exists; most touch `AGENTS.md`; any new flag touches `config.py`/`.env.example`).
"Would otherwise
conflict" evaluates to true for nearly every pair of code-writing tasks here, so
default-on isolation and the selective heuristic converge — default-on is just the
version that doesn't depend on a fallible per-task prediction. It also costs almost
nothing (~200–500ms + disk) against a documented, **repeat** incident (shared-tree
dispatch caused branch hijacking and cross-task file contamination on 2026-07-22 —
see `docs/decisions/2026-07-22-orchestrator-redesign-plan.md` §2.1 — the gotcha had
already been written down once before it happened).

**This is mechanically enforced**, not just documented: a `PreToolUse` hook
(`.claude/hooks/require-worktree-isolation.py`) blocks `Agent`/`Task` calls that look
like code-writing dispatches and lack `isolation:"worktree"`. Pass `NO_WORKTREE_OK`
in the prompt to deliberately override for a single dispatch (e.g., the sole
code-writing task in a batch, nothing else running concurrently).

**Exempt (no worktree needed):** the read-only subagent types in the hook's
`READ_ONLY_SUBAGENTS` set, exactly — `infra-researcher`, `lc-safety-reviewer`,
`lc-migration-reviewer`, `lc-api-reviewer`, `lc-agent-completeness`, `sweep-health`,
`drift-analyst`, plus the built-in `Explore` and `Plan` agents. (Note `lc-agent-completeness`
is exempt too — it is read-only despite not matching an `lc-*-reviewer` shorthand.)

### 5.2 Preconditions before any fan-out

The base working tree must be **clean** (`git status --porcelain` empty — commit or
stash first), and you must record `BASE=$(git rev-parse HEAD)` before dispatching.
A dirty shared base is exactly how the 2026-07-22 incident started; a clean recorded
base is what makes §6's integration procedure mechanical.

### 5.3 Per-agent dispatch contract

Every code-writing dispatch's prompt must include:

- **Branch naming:** `wt/<batch-slug>/<task-id>-<short-name>` (e.g.
  `wt/findings-117/T4-phase1-n1`). In `Workflow` mode, record the branch name it
  returns instead.
- **Rules:** branch from BASE only; commit locally, small and focused; **never push,
  never switch branches, never rebase, never touch files outside the worktree.**
- **Shared-file rule (§6.2):** do NOT edit the findings tracker or `AGENTS.md` —
  report intended tracker row content / registry changes in the result instead.
  `config.py`/`.env.example` edits must be additive-only (append a new field in its
  own clearly-delimited block; never reorder or refactor existing entries).
- **Result contract (proof, not prose — extends F-015 to worktree outputs):** branch
  name, tip SHA, `git diff --name-only BASE..HEAD` output, and the test command +
  result line it ran.

---

## Step 6 — Rolling Integration

**Do not wait for all tasks in a batch to finish and then reconcile at the end** —
that produces an N-way tangle (this is exactly what happened on 2026-07-22 and required
manual `git rebase --onto` + `git merge-base --is-ancestor` surgery to fix before a
single MR could go out). Instead, fold each branch in **as its agent finishes**, while
slower agents are still running. Each conflict is then pairwise, small, and fresh.

### 6.1 Per-completing-agent procedure

`integration` is a branch you create at BASE before the first fold.

1. **Verify the branch is sane** (guards against hijacking/contamination AND against
   `isolation:"worktree"` itself mis-basing the worktree — confirmed empirically on
   2026-07-22: 3 of 4 identically-dispatched code-writing agents were silently branched
   from stale `master` instead of the session's actual current branch tip; one agent
   self-caught it, the other 3 didn't, and their branches reverted every file outside
   their own edits). Run this for every completing agent, immediately, not just when
   something looks off:
   ```bash
   bash .claude/skills/orchestrator/verify-worktree-base.sh $BASE wt/<slug>/T1 wt/<slug>/T2 ...
   ```
   (accepts multiple branches at once — pass every branch from the batch that has
   completed so far). PASS prints the branch's commit range and touched-file list for
   the conflict-prediction step below; FAIL means the branch does not descend from
   BASE and diagnoses why. On FAIL: **quarantine the branch** (do not fold it
   wholesale) — extract that agent's intended changes surgically instead (`git show
   <branch>:<path>` per file the agent's own report says it touched, applied onto
   `integration` directly), and report the mismatch as `UNVERIFIED — claimed but not
   evidenced` per the F-015 rule in Step 7.
2. **Predict conflicts:** intersect Ti's file list with the union of files already
   folded into `integration`. Empty → step 3 is clean; non-empty → you know the
   conflict files before touching git.
3. **Fold:**
   `git rebase --onto integration $BASE wt/<slug>/Ti && git branch -f integration wt/<slug>/Ti`
   (equivalently, cherry-pick the range onto `integration`). First finisher is a
   trivial fast-forward.
4. **Resolve conflicts now**, using the shared-file playbook (§6.2).
5. **Verify semantically, not just textually:** run the fast tier on `integration`
   after every fold — `ruff check` + the focused pytest subset for the touched
   area(s) + the relevant guard-hook checks (env-parity, agent-registry-sync
   invariants). This catches the conflict class git can't see: two branches that
   merge cleanly but both add a Settings field, or both claim the same cron slot
   (the exact `f385ebf` schedule-collision bug class from earlier the same day).
6. **Clean up immediately** once
   `git merge-base --is-ancestor <Ti-tip> integration` confirms the commits landed:
   ```bash
   git worktree remove <path> && git branch -d wt/<slug>/Ti
   ```
   (`Workflow` auto-removes *clean* worktrees only — a worktree with actual changes is
   returned to you, and this step applies identically.) End of session:
   `git worktree list` should show only the main tree; `git worktree prune` as
   backstop. Manual-mode worktrees live in a dedicated git-ignored location outside
   the repo tree (e.g. `../infra-brain.wt/<branch>`), never nested inside it.

**After the last fold:** full test suite on `integration`, then write the batched
TRACKER/AGENTS.md updates (§6.2), then merge `integration` into the session's feature
branch → one MR-gated push per the corrected merge policy (§10). If any folded branch
touched `db/models/`, dialect-specific types, or raw SQL: `/pg-gate-check` before push.

### 6.2 Shared-file conflict playbook

| File | Rule for subagents | Integration handling |
|---|---|---|
| findings tracker (if present) | **Never edited directly.** Each task reports its row(s)/status-changes as structured text in its result. | You write all rows in one commit after the last fold. Eliminates the single most frequent conflict source — every task logs here. |
| `AGENTS.md` | Never hand-edited (it's generated from AgentSpec). | Regenerate once on `integration` after all folds — `python scripts/gen_agents_md.py`. |
| `src/infra_brain/config.py` / `.env.example` | **Additive-only**: append new Settings fields / env entries in their own clearly-delimited block; never reorder, rename, or refactor existing entries. A config *refactor* is a singleton task, never scheduled parallel with anything else touching config. | Append/append conflicts at the same anchor resolve as keep-both-hunks; the fast-tier env-parity check catches semantic duplication. |
| `supervisor.py` AGENT_REGISTRY / `scheduler.py` schedules | Additive-only; each task's decomposition row must declare the registry/schedule entries it will add, so cron-slot collisions are caught **at decomposition time** (Step 2), not at integration. | `agent-registry-sync` invariants in the fast tier. |
| Everything else | Decomposition (Step 2) must declare expected file touches per task. Undeclared overlap discovered at §6.1 step 2 → resolve pairwise, note it, tighten the next decomposition. | Standard pairwise resolution. |

**Deliberately rejected:** `.gitattributes merge=union` on TRACKER.md — union-merge
silently keeps both sides of edits to *existing* lines, so a status change corrupts
into duplicate rows instead of a clean update. The report-and-centralize rule above is
strictly safer and simpler.

---

## Step 7 — Evidence-Gated Synthesis

> **HARD RULE (F-015): no subagent claim of completed work enters the synthesis
> without a re-runnable evidence artifact that YOU re-checked.** An evidence
> artifact is one of:
> - a commit hash you re-verified with `git log --oneline -1 <hash>`
> - the §6.1-step-1 branch-verification trio (`merge-base --is-ancestor`, `log`, `diff --name-only`)
> - a command + output you re-ran yourself (pytest, grep, curl, ls)
> - a file path whose existence/content you re-read (Read/Bash)
>
> A "done" claim with no artifact, or whose artifact fails your re-check, is
> reported as **UNVERIFIED — claimed but not evidenced**, never as done.
> (Audit finding F-015: a subagent reported a commit that had not landed.)

After all batches complete and are integrated, synthesize into a coherent response:

1. **Verify first** — for every "done" claim, re-run the cheapest evidence command
   yourself before reporting it as done (this now includes the branch-sanity trio
   from §6.1 for anything that went through worktree isolation)
2. **Status summary** — which tasks passed/failed, each with its evidence artifact
3. **Findings** — surface all CRITICAL/HIGH issues from validators first
4. **Completed work** — what was actually done (evidence-linked)
5. **Remaining steps** — what the user still needs to do (if anything)
6. **Verification status** — did tests pass on `integration`? lint clean? (paste the
   command + result line)

If any task failed: explain WHY and what to do next.
If a validator found issues: list them by severity (CRITICAL first), include file:line.

---

## Step 8 — Routing Tables

### Research tasks → infra-researcher agent

Use for any read-only context gathering that can run in parallel with other tasks:
reading source files for patterns, checking registry/schedule/wiring state,
verifying current DB schema, inspecting test structure, checking git history.

### Implementation tasks → skills

| Task | Skill | Notes |
|---|---|---|
| New domain agent (full wiring) | `/agent-register` | Use this, not agent-scaffold |
| New domain agent (file only) | `/agent-scaffold` | Only if wiring separately |
| New domain tool | `/tool-register` | Creates tool file + test + wires into agent + safety review |
| Database migration | `/migration-create` | ALWAYS follow with `lc-migration-reviewer` |
| Pre-deploy validation | `/deploy-check` | Run before any push |
| SQL column validation | `/validate-sql` | Run after any change to raw SQL in chat/tools.py or api/routers/* |
| Full health audit | `/dev-status` | Run after major changes |
| Debug sweep | `/sweep-debug` | 5-layer triage |
| (LangChain-pattern skills — `/lc-agent`, `/lc-graph`, `/lc-memory`, `/lc-resilience`, `/lc-guardrails`, `/lc-providers`, `/lc-debug`, `/lc-test`, `/lc-review`, `/lc-guard`, `/lc-docs`, `/lc-explain`, `/lc-scaffold`, `/lc-trace`, `/lc-erase`) | see Quick Reference table above | |

### Review batches → dispatch the applicable reviewer subagents yourself, in parallel

(Absorbed from the retired `review-coordinator` agent — the routing matrix and prompt
templates below are unchanged, only the "spawn a coordinator to spawn reviewers" shell
is gone. You dispatch these directly, same as any other validate batch.)

**Step A — classify changed files against this matrix:**

| File pattern | Reviewer | Why |
|---|---|---|
| `src/infra_brain/callbacks/**` | `lc-safety-reviewer` | Safety callback chain |
| `src/infra_brain/supervisor.py` | `lc-safety-reviewer` | LangGraph router, agent dispatch |
| `src/infra_brain/tools/**` | `lc-safety-reviewer` | Input validation, read-only enforcement |
| `alembic/versions/*.py` | `lc-migration-reviewer` | Schema migration safety |
| `src/infra_brain/api/**` | `lc-api-reviewer` | FastAPI async correctness, auth |
| `src/infra_brain/main.py` | `lc-api-reviewer` | Route registration |
| `src/infra_brain/agents/*.py` (new file — no matching test) | `lc-agent-completeness` | Wiring validation |

If multiple patterns match, all matching reviewers apply — dispatch them **in one
batch, one response**. If none match, no reviewer is needed; say so and move on.

**Step B — prompt templates** (fill in the actual changed-file list, dispatch all in one response):

<details>
<summary>lc-safety-reviewer prompt template</summary>

```
Review the following files for safety violations in infra-brain.
Files changed: [list the matching files]

Focus on:
- Callback chain completeness (every agent must call build_callbacks())
- DLP bypass paths (any code path that skips DLPCallbackHandler)
- Read-only enforcement (ReadOnlyToolValidator must be in chain)
- Input validation on tools (regex guard before external calls)
- Sync/async callback mismatch (sync handlers in async context)

Report all findings with severity (CRITICAL / HIGH / MEDIUM) and file:line.
```
</details>

<details>
<summary>lc-migration-reviewer prompt template</summary>

```
Review the following Alembic migration file(s) for safety in infra-brain.
Files: [list migration files]

Check for:
- NOT NULL column without default (breaks existing rows)
- DROP TABLE or DROP COLUMN (data loss)
- Missing CONCURRENTLY on index creation (table lock)
- Missing lock_timeout (long-running locks)
- Integer overflow on large tables

Report all findings with severity and the exact SQL that would fail.
```
</details>

<details>
<summary>lc-api-reviewer prompt template</summary>

```
Review the following FastAPI route files for correctness in infra-brain.
Files: [list api files]

Check for:
- Sync DB calls inside async route handlers (blocks event loop)
- Missing response_model on routes that return sensitive data
- Missing auth dependency on non-public routes
- Session leak (session opened but not closed on exception path)
- Missing ainvoke()/astream() — sync invoke() in async routes

Report all findings with severity and file:line.
```
</details>

<details>
<summary>lc-agent-completeness prompt template</summary>

```
Check wiring completeness for the following new agent file(s) in infra-brain.
Files: [list agent files]

For each agent, validate all 4 wiring points:
1. Agent file exists at src/infra_brain/agents/<domain>.py
2. Test file exists at tests/agents/test_<domain>.py
3. AGENT_REGISTRY entry in supervisor.py
4. _DEFAULT_SCHEDULES entry in scheduler.py (or documented exemption)

Report: COMPLETE / INCOMPLETE with exact missing items.
```
</details>

**Step C — synthesize** (same shape review-coordinator used to produce, now written by you):

```
## Review Summary
**Files reviewed:** N files across M domains
**Reviewers dispatched:** [list which ones]
---
### CRITICAL Findings (must fix before merge)
### HIGH Findings (strongly recommended to fix)
### MEDIUM/INFO Findings (optional improvements)
---
### Per-Reviewer Verdict
| Reviewer | Status | Key Findings |
|---|---|---|
| lc-safety-reviewer | PASS / NEEDS-FIXES | summary |
| lc-migration-reviewer | PASS / NEEDS-FIXES | summary |
| lc-api-reviewer | PASS / NEEDS-FIXES | summary |
| lc-agent-completeness | COMPLETE / INCOMPLETE | summary |
**Overall verdict:** MERGE-READY / NEEDS-FIXES
```
If a reviewer fails or returns no output: report `REVIEWER-ERROR`, don't block the
overall verdict on it, note the domain wasn't reviewed.

### Analysis tasks → DB-backed agents (need postgres-infra MCP; parallelizable)

| When | Agent | What it produces |
|---|---|---|
| Cross-domain sweep status ("are sweeps healthy / overdue?") | `sweep-health` | Per-domain last-run/status/overdue report across all domains |
| Per-domain drift delta ("what changed for `<domain>` since last run?") | `drift-analyst` | New / resolved / persistent drift events between the last 2 completed runs |

If the `postgres-infra` MCP server is unavailable, skip these and note it in synthesis.

### Verification tasks → Bash (parallelizable with each other)

```bash
python -m pytest tests/agents/ -q --tb=short          # agent tests
python -m pytest tests/callbacks/ -q --tb=short       # safety tests
python -m pytest tests/test_dashboard_sql_columns.py -q --tb=short  # SQL column guard
python -m pytest tests/ -q --tb=short                 # full suite
python -m ruff check src/ tests/                       # lint (parallelizable with tests)
# SQL validation (parallelizable): invoke /validate-sql skill
```

---

## Intent Classification (routing decision tree, 20 scenarios)

Read the user's task. Match it to one of the scenarios below. Follow the routing
instructions exactly — do not skip the subagent dispatches, they catch real bugs.
**Every "spawn X" / "auto-spawn X" instruction below means: you dispatch it yourself,
in the appropriate batch, per Steps 3–7 above** (worktree isolation if it writes files,
correct model tier, rolling integration if run alongside other code-writing tasks).

---

### SCENARIO 1 — First time in the repo / onboarding

**Trigger phrases:** "just joined", "first time", "getting started", "how do I set up",
"new to this project", "what do I do first", "orient me", "new contributor"

**Also trigger if:** this is the user's first message and they haven't stated a specific task.

**Routing:**
1. Invoke `/onboard` — the infra-brain-specific onboarding skill covers: safety model,
   25-domain architecture (see AGENTS.md, generated from AgentSpec), critical constraints, and the task map for common first contributions
2. Then run `/dev-status` — checks 10 tooling invariants in ~30 seconds; surfaces broken wiring
   before the user touches anything
3. If they don't know LangChain → invoke `/lc-start` to teach the fundamentals after onboard
4. Ask what they want to work on → re-route to the appropriate scenario below

**Note for LangChain beginners:** infra-brain uses LangGraph StateGraphs, LangChain tools,
and AsyncCallbackHandlers. You don't need to know all of this upfront — the skills teach
it contextually. Start with `/lc-explain StateGraph` or `/lc-explain tool` if you encounter
unfamiliar terms.

---

### SCENARIO 2 — Adding a new infrastructure domain agent

**Trigger phrases:** "new agent", "add agent", "monitor [X]", "collect data from [X]",
"add [domain] coverage", "track [X]", "sweep [X]", "integrate [X] into infra-brain",
"new domain", creating a file in `src/infra_brain/agents/`

**Routing:**
1. Invoke `/agent-register <name> [description] [--schedule <cron>]`
   - This single command does everything: scaffold agent file + test, wire into
     `supervisor.py` (AGENT_REGISTRY + SKIP_HOOK), wire into `scheduler.py` (_DEFAULT_SCHEDULES)
   - `--skip-hook` if this is an analysis/system agent (not a data-collection agent)
   - Default schedule is `0 2 * * *` (daily 2am UTC); adjust for high-frequency domains
2. After scaffolding: **dispatch `lc-agent-completeness`** to verify all
   four wiring points are in place (file, test, registry, schedule)
3. If the new agent calls external tools (API, SSH, SNMP): **also dispatch `lc-safety-reviewer`**
   to verify tools flow through `build_callbacks()` and are read-only

**If the user is new to LangChain agent patterns:** invoke `/lc-agent` first to explain
ReAct/Supervisor patterns before they write the agent body.

**Never do:** add an agent file without a matching test file in `tests/agents/test_<name>.py`.
The `test-coverage-guard` hook warns locally on edit.

---

### SCENARIO 3 — Debugging a sweep (no data, empty results, failed run)

**Trigger phrases:** "[domain] not collecting", "sweep is empty", "no findings", "sweep failed",
"agent returning nothing", "why is [X] not working", "no data in the DB for [domain]",
"[domain] sweep isn't running", run_id or UUID mentioned alongside an error

**Routing:**
1. Invoke `/sweep-debug [domain] [run_id]`
   - This runs a 5-layer triage: DB collection_runs → audit_log DLP/readonly violations
     → Redis dedup state → agent exception logs → LangSmith trace
   - Omit `[domain]` to check the most recent failed run across all domains
   - Omit `[run_id]` to use the most recent run for that domain
2. If the triage reveals a LangChain error (import, LCEL, async): invoke `/lc-debug`
3. If the triage reveals a callback violation (DLP block, read-only rejection): dispatch
   `lc-safety-reviewer` on the relevant agent file
4. If the triage reveals Redis dedup blocking the agent: check `dedup.py` and clear the key

**Common causes by symptom:**
- `resources_found = 0` on first run → check tool connectivity, credentials in `.env`
- `status = failed` immediately → check agent exception in `audit_log`
- `status = success` but 0 resources → check DLP callback — may have scrubbed all findings
- Sweep runs but data doesn't appear in UI → check `validate-sql` for column name drift

---

### SCENARIO 4 — Database / schema changes

**Trigger phrases:** "add a column", "new table", "change the schema", "add field to model",
"track new data", "alembic", "migration", "database change", editing `db/models/*.py`,
"the ORM needs a new field"

**Routing:**
1. Invoke `/migration-create <message>` — never hand-write Alembic migrations
   - This generates the migration, runs `alembic check` to verify ORM↔DB sync,
     and reviews for dangerous patterns (NOT NULL without default, DROP TABLE, etc.)
2. After the migration file is generated: **dispatch `lc-migration-reviewer`**
   - Checks: NOT NULL without server_default, DROP TABLE/COLUMN, missing CONCURRENTLY
     on index creates, missing lock_timeout, migration conflicts
3. The `alembic-check` PostToolUse hook fires automatically when any `db/models/*.py` module is edited —
   if it reports drift, run `/migration-create` to generate the missing migration

**Never do:** hand-write a migration file. Never run `alembic revision` directly.
The `/migration-create` skill handles the full safe workflow.

---

### SCENARIO 5 — FastAPI route work (API endpoints, webhooks)

**Trigger phrases:** "new endpoint", "add a route", "new webhook", "API handler", "POST route",
"webhook receiver", editing `webhooks.py` or `dashboard_api.py`, "new REST endpoint"

**Routing:**
1. Write the route handler
2. **Dispatch `lc-api-reviewer`** immediately after writing any new or modified route
   - Checks: async/await correctness (no sync `invoke()` in async context),
     authentication present, `response_model` defined, `BackgroundTasks` misuse,
     proper exception handling
3. If the route calls agent tools: **also dispatch `lc-safety-reviewer`** to confirm
   tool calls route through `build_callbacks()`

**Critical rule:** never use sync `invoke()` inside FastAPI route handlers — use `ainvoke()`
or `astream()`. Sync calls block the event loop and cause timeouts under load.

---

### SCENARIO 6 — Safety-critical code (callbacks, supervisor, tools)

**Trigger phrases:** editing `callbacks/readonly.py`, `callbacks/dlp.py`,
`callbacks/registry.py`, `supervisor.py`, any file in `src/infra_brain/tools/`,
"change the safety callback", "modify the DLP", "update the supervisor", "add a tool"

**Routing:**
1. The `safety-guard` PreToolUse hook fires automatically with a warning (exit 1)
2. **Always dispatch `lc-safety-reviewer`** before and after any change to these files
   - Checks: callback registration completeness, sync/async handler mismatches,
     read-only enforcement, DLP bypass paths, audit log coverage
3. **For adding a new tool:** invoke `/tool-register <name> <agent>` — this creates the tool
   file, test file, wires it into the agent, and dispatches `lc-safety-reviewer`
4. For modifying an existing tool: edit it, then **dispatch `lc-safety-reviewer`** on the file

**Never do:**
- Remove a `raise` statement from `callbacks/readonly.py` or `callbacks/dlp.py`
- Add a tool that bypasses `build_callbacks()`
- Register a new agent in `AGENT_REGISTRY` without passing `callbacks=build_callbacks()`
  to its `get_chat_model()` call

---

### SCENARIO 7 — Understanding LangChain/LangGraph concepts

**Trigger phrases:** "what is [X]", "explain [X]", "how does [X] work", "I don't understand",
"what's the difference between", "when should I use [X]", "LangGraph vs LangChain",
"what is a StateGraph", "what is LCEL", "what is a checkpointer", "what is a tool node"

**Routing:**
- **Single concept:** invoke `/lc-explain <concept>`
- **Choosing a pattern:** invoke `/lc-patterns [description of what you're building]`
- **Live documentation:** invoke `/lc-docs <topic>`

**For users new to agentic systems:** start with `/lc-explain ReAct` and
`/lc-explain StateGraph` — everything else builds on these two.

---

### SCENARIO 8 — Debugging LangChain/LangGraph errors

**Trigger phrases:** error message pasted into chat, "not working", "exception", "traceback",
"ImportError", "ValueError", "this is broken", "why is this failing",
"LangGraph error", "LCEL error", "async error", "callback error"

**Routing:**
1. Invoke `/lc-debug` — provides a structured triage for all common LangChain error categories
2. If the error is in a callback: dispatch `lc-safety-reviewer`
3. If the error is in an agent registry lookup: run `/dev-status` to check consistency

**Quick lookup for common infra-brain errors:**
- `get_chat_model() missing callbacks=` → agent not registered through `build_callbacks()`
- `APScheduler: timeout not implemented` → `shutdown(wait=True, timeout=30)` is invalid; use `shutdown(wait=True)`
- `sync invoke() in async context` → replace `chain.invoke()` with `await chain.ainvoke()`
- `alembic check fails` → run `/migration-create` to generate the missing migration

---

### SCENARIO 9 — Writing or fixing tests

**Trigger phrases:** "write tests for", "add tests", "fix tests", "test coverage",
"pytest failing", "test this agent", "unit test", "integration test", "eval"

**Routing:**
- **New agent test:** run `/agent-scaffold <name>` or check `tests/agents/test_<name>.py`
- **LangChain test patterns:** invoke `/lc-test`
- **Running tests:** `pytest tests/ -q` (all), `pytest tests/agents/ -q` (agents only),
  `pytest tests/callbacks/ -q` (safety callbacks), `pytest tests/test_dashboard_sql_columns.py -v` (SQL guard)
- **CI gates:** the MR pipeline runs only two blocking gates — `migration-parity` and
  `sql-execution-check`. There is no coverage floor gate.

**Never do:** ship a new agent file without a test file. The `test-coverage-guard` hook
warns locally on edit.

---

### SCENARIO 10 — Deployment

**Trigger phrases:** "deploy", "push to production", "release", "go live", "ship this",
"Docker deploy", "is it ready to deploy", "pre-deploy", "production checklist"

**Routing:**
1. Invoke `/deploy-check` — runs the full pre-deployment validation checklist
2. Open an MR → the MR pipeline runs the two blocking gates (`migration-parity`,
   `sql-execution-check`). On merge to `master`, the master-only stages run automatically:
   build → deploy → backup → runner-disk-prune → rollback → verify-deployed-commit.
   **Confirm with the user before pushing/opening the MR — never automatic.**
3. Monitor the pipeline at: `https://gitlab.example.internal/agents/infra-brain/-/pipelines`

**Current deploy target:** deploy-host-01 (192.0.2.13) via Docker Compose on the GitLab runner.
Port mapping: app → 8001, UI → 8501, postgres → 5433, n8n → 5679.

---

### SCENARIO 11 — Kubernetes migration

**Trigger phrases:** "migrate to k8s", "kubernetes", "k8s", "move off Docker", "Helm"

**Routing:**
1. The k8s manifests are **already written** in `k8s/` (9 files)
2. Migration = `pg_dump` + restore into the k8s postgres pod + `kubectl apply -f k8s/`
3. Invoke `/lc-deploy` → Kubernetes section for step-by-step guidance
4. Critical: `k8s/scheduler.yaml` MUST stay at `replicas: 1`
5. The `lint-k8s` hook validates all k8s YAML on every edit automatically

---

### SCENARIO 12 — Monitoring and tracing

**Trigger phrases:** "trace this", "why is it slow", "LangSmith", "observability",
"add tracing", "monitor agent behavior", "debug trace", "see what the LLM is doing"

**Routing:**
- **Inject tracing into an existing file:** `/lc-trace <file.py>`
- **Set up LangSmith:** invoke `/lc-monitor` → Setup section
- **Sweep-specific performance:** query `collection_runs.duration_sec` in postgres on port 5433
- **Agent-level tracing:** every agent already passes `callbacks=build_callbacks()` — check
  `audit_log` before LangSmith

---

### SCENARIO 13 — Security audit

**Trigger phrases:** "security audit", "is this secure", "prompt injection", "vulnerability",
"security review", "check for exploits", "CVE", "audit the codebase"

**Routing:**
1. Invoke `/lc-guard [path/to/project]` — audits for 8 security gaps
2. For callback chain integrity: dispatch `lc-safety-reviewer`
3. For infrastructure-level vulnerabilities: the `vuln`/`vuln_triage` agents collect
   this data automatically — check sweep findings rather than auditing manually

---

### SCENARIO 14 — Adding LangChain patterns

| User needs to add… | Invoke |
|---|---|
| Retry logic, fallback chains, circuit breaker | `/lc-resilience` |
| Conversation memory, chat history, checkpointing | `/lc-memory` |
| Guardrails, PII protection, cost circuit breaker, HITL | `/lc-guardrails` |
| Multi-modal inputs (images, PDFs, audio) | `/lc-multimodal` |
| SQL/database query agent, text-to-SQL | `/lc-data` |
| Vector store, embeddings, hybrid search | `/lc-vectorstore` |
| Audit logging, compliance logging, hash chains | `/lc-audit` |
| GDPR, HIPAA, EU AI Act compliance patterns | `/lc-compliance` |
| Streamlit / Chainlit / Gradio UI | `/lc-ui` |
| RAG pipeline (any variant) | `/lc-rag` |
| LCEL pipe composition, Runnables | `/lc-lcel` |
| StateGraph, nodes, edges, interrupts | `/lc-graph` |

---

### SCENARIO 15 — Switching or adding an LLM provider

**Trigger phrases:** "switch to Anthropic", "use Claude", "use OpenAI", "use Ollama",
"use Bedrock", "use Azure", "change the LLM", "different model", "add a provider"

**Routing:**
1. Invoke `/lc-providers [provider-name]`
2. Never hardcode API keys — add to `.env.example`, document in Bitwarden, update `config.py`

---

### SCENARIO 16 — Dashboard / frontend work

**Trigger phrases:** "dashboard", "UI", "add a chart", "new page",
"visualization", "UI component", "display findings", "frontend"

**Routing:**
1. The dashboard is the Vite+React SPA in `dashboard-app/` (served at `/dashboard2`, the
   sole UI). Edit the `.tsx` components under `dashboard-app/src/` directly — this is
   frontend work, no heavy decomposition needed for a single component.
2. If you touch the backend that feeds the dashboard: run `/validate-sql`
3. New/modified FastAPI routes that back the dashboard → dispatch `lc-api-reviewer`

---

### SCENARIO 17 — Compliance and data rights

**Trigger phrases:** "GDPR", "right to erasure", "data deletion", "HIPAA", "privacy",
"compliance", "data subject request", "delete user data", "EU AI Act"

**Routing:**
- **Right-to-erasure (GDPR Article 17):** invoke `/lc-erase <user_id>`
- **Compliance patterns:** invoke `/lc-compliance`
- **Audit trail:** `AuditCallbackHandler` already logs every LLM interaction to `audit_log`
  with cryptographic hash chains

---

### SCENARIO 18 — General health check / "is everything ok?"

**Trigger phrases:** "health check", "is everything wired up", "what's broken",
"status check", "dev status", "everything ok?", "sanity check", before a major refactor

**Routing:**
1. Invoke `/dev-status` — 10-check audit (~30 seconds, no Docker or DB required)

---

### SCENARIO 19 — Code review

**Trigger phrases:** "review this", "is this correct", "check my code", "code review",
"review before I merge", "is this pattern right"

**Routing:**
- **Multi-file change (≥2 file types):** use "Review batches" (Step 8 above) — classify
  the changed files, dispatch the applicable reviewers yourself in one parallel batch
- **LangChain/LangGraph code:** invoke `/lc-review [file.py]`
- **FastAPI routes:** dispatch `lc-api-reviewer` on the file
- **Safety-critical code:** dispatch `lc-safety-reviewer` on the file
- **Alembic migrations:** dispatch `lc-migration-reviewer` on the migration file
- **New agent:** dispatch `lc-agent-completeness` to verify all 4 wiring points

---

### SCENARIO 20 — CI/CD pipeline work

**Trigger phrases:** "CI pipeline", "gitlab-ci", "fix the pipeline", "pipeline failing",
editing `.gitlab-ci.yml`, "add a CI job", "CI broke"

**Routing:**
1. **Pipeline is failing:** invoke `/ci-debug [pipeline_id]`
2. After any edit to `.gitlab-ci.yml`: the `ci-lint` PostToolUse hook validates automatically
3. MR pipelines run the two blocking gates (`migration-parity`, `sql-execution-check`);
   merge to `master` runs: build → deploy → backup → runner-disk-prune → rollback →
   verify-deployed-commit
4. Runner: `deploy-host-01 Ansible runner` (id=1, Docker executor, socket-mounted)
5. Secret: `INFRA_BRAIN_ENV` (GitLab CI File variable, project 42) — contains the full `.env`

**Common CI failures:**
- `yaml_errors` in pipeline details → YAML parse error; validate before push
- `No jobs found` / pipeline fails instantly → runner not assigned to project
- `build fails: metadata-generation-failed` → Dockerfiles need `COPY src/ ./src/` before `pip install`
- Jobs stuck `pending` → runner busy with another project's pipeline (concurrent = 1)

---

## Automatic Hook Behavior (always running, no invocation needed)

These fire automatically on every file edit, agent dispatch, or user prompt — you don't
need to invoke them:

| Hook | Triggers on | What it does | Fail behavior |
|---|---|---|---|
| `orchestrator-default` | every user prompt (UserPromptSubmit) | Injects the orchestrator-mode routing reminder (decompose, then dispatch specialists yourself) | Exit 0 (nudge only) |
| `block-env` | `.env`, `.env.local` | Hard-blocks any edit to live secrets | **Exit 2 (blocked)** |
| `safety-guard` | `callbacks/*.py`, `supervisor.py` | Warns before touching safety boundaries | Exit 1 (warn) |
| `ruff-fix` | Any `.py` in src/, tests/, scripts/ | Auto-runs `ruff check --fix` | Exit 0 (auto-fixed) |
| `alembic-check` | `db/models/*.py` | Detects ORM↔migration drift | Exit 1 (warn) |
| `high-blast-radius-test` | `config.py`, `db/models/*.py`, `db/session.py` | Runs targeted regression tests | Exit 1 (warn) |
| `agent-registry-sync` | `supervisor.py`, `scheduler.py` | 3-way AGENT_REGISTRY↔SKIP_HOOK↔schedule check | Exit 1 (warn) |
| `test-coverage-guard` | `agents/<name>.py` | Warns if agent has no matching test file | Exit 1 (warn) |
| `env-parity-guard` | `config.py` | Warns if new Settings field has no .env.example entry | Exit 1 (warn) |
| `lint-k8s` | `k8s/*.yaml` | Validates k8s manifests (probes, replicas, tags, syntax) | Exit 1 (warn) |
| `ci-lint` | `.gitlab-ci.yml` | POSTs to GitLab CI Lint API — hard-blocks invalid YAML | **Exit 2 (blocked)** |
| `require-worktree-isolation` (NEW, 2026-07-22) | `Agent`/`Task` calls that look code-writing | Blocks dispatch without `isolation:"worktree"` (see Step 5.1) | **Exit 2 (blocked)**, override with `NO_WORKTREE_OK` in the prompt |

---

## Critical Rules (never break these)

These are the invariants that make infra-brain safe and correct. Violating them causes
silent data corruption, security holes, or production outages.

1. **All tool calls through `build_callbacks()`** — never call `get_chat_model()` without
   `callbacks=build_callbacks()`. This is what enforces read-only and DLP.
2. **No sync `invoke()` in async FastAPI routes** — always `await chain.ainvoke()`.
3. **Callback handlers must be async** — subclass `AsyncCallbackHandler`.
4. **Never edit `.env`** — it contains live secrets. Edit `.env.example` instead, add
   the secret to Bitwarden, then update `config.py`.
5. **Never hand-write Alembic migrations** — always use `/migration-create`.
6. **New agent = new test file** — `tests/agents/test_<name>.py` must exist before merging.
7. **APScheduler replicas = 1** — both in `docker/docker-compose.yml` and `k8s/scheduler.yaml`.
8. **`/healthz` is zero-I/O, `/health` checks DB+Redis** — never swap these probes.
9. **`shutdown(wait=True)` only** — `shutdown(wait=True, timeout=30)` raises `TypeError`
   in APScheduler 3.x.
10. **MR-gated merge policy (corrected 2026-07-22 — this rule previously said "merge
    directly to master, no PRs," which was WRONG and contradicted the actual workflow):**
    feature branch → small local commits → `integration` branch per Step 6 →
    **ONE push + GitLab MR when the unit of work is done (confirm with the user first,
    it is not automatic)** → MR gates (`migration-parity`, `sql-execution-check`) →
    merge. **Never push directly to `master`** — the MR-blocking CI gates only run on
    MR pipelines.

---

## Common Multi-Step Workflows

### Add a new Windows Ansible sweep agent
```
1. /agent-register windows_ansible --schedule "0 3 * * *"
2. Write the agent body using existing src/infra_brain/agents/windows.py as reference
3. Dispatch: lc-agent-completeness (verify wiring)
4. Dispatch: lc-safety-reviewer (verify tool safety)
5. pytest tests/agents/test_windows_ansible.py -v
6. /dev-status (confirm all 10 checks pass)
7. Push the branch + open a GitLab MR (confirm with the user first) → MR gates → merge
```

### Debug why the GitLab sweep is returning no CI/CD data
```
1. /sweep-debug cicd
2. If audit_log shows DLP violations → lc-safety-reviewer on src/infra_brain/agents/cicd.py
3. If collection_runs shows exception → check src/infra_brain/tools/gitlab.py for 429 handling
4. If Redis dedup is blocking → check dedup.py cache TTL
5. /lc-debug if the traceback is a LangChain error
```

### Add a new database field to store agent metadata
```
1. Edit src/infra_brain/db/models/core.py (add the column — `CollectionRun` lives there)
2. alembic-check hook fires automatically → tells you migration is needed
3. /migration-create "add metadata field to collection_runs"
4. Dispatch: lc-migration-reviewer (safety review)
5. alembic upgrade head (apply migration)
6. /validate-sql (check UI queries still match column names)
```

### Pre-deploy checklist before pushing a major change
```
1. /deploy-check (full validation — all 10 checks)
2. pytest tests/ -q (all tests pass)
3. /validate-sql (no column name drift in UI)
4. ruff check src/ tests/ (no lint errors)
5. Push the branch + open a GitLab MR (confirm with the user first) → gates
   (migration-parity, sql-execution-check) → merge → build → deploy
6. Monitor: https://gitlab.example.internal/agents/infra-brain/-/pipelines
```

### Onboard a new contributor who doesn't know LangChain
```
1. /onboard (infra-brain-specific: safety model, 25 domains, critical constraints, task map)
2. /dev-status (show them the current tooling health)
3. /lc-start (LangChain fundamentals — they'll see how infra-brain's patterns connect)
4. /lc-explain StateGraph (core architecture of all agents)
5. /lc-explain tool (how infra-brain tools connect to external systems)
6. /agent-register sandbox_test --skip-hook (safe low-stakes agent to practice)
```

### Check sweep health and compare drift before a meeting
```
1. sweep-health agent ("check health of all 25 domains")
2. drift-analyst agent ("analyze drift for linux domain")
3. drift-analyst agent ("analyze drift for k8s domain")
   (steps 1-3 can run in parallel if postgres-infra MCP is connected)
```

### Implement 3 independent pieces of work (the shape that caused the 2026-07-22 incident)
```
1. Decompose (Step 2): T1, T2, T3 — each `implement`, each declares its file touches,
   each flags any shared-file touches (TRACKER.md, config.py, etc.)
2. Precondition (Step 5.2): confirm clean tree, record BASE
3. Dispatch T1, T2, T3 in ONE response, each with isolation:"worktree" and its own
   wt/<slug>/<task-id> branch name in the prompt
4. As each finishes: run the Step 6.1 fold procedure immediately — don't wait for all 3
5. After the last fold: full suite on `integration`, batch-write TRACKER rows,
   regenerate AGENTS.md, then one push + MR
```

---

## Dependency-Aware Sweep Sequencing

Before routing any sweep-run or trigger-collection request, read `.claude/agent-dependencies.yaml`.

If the requested domain appears under `dependencies`:
1. Note the `requires` list and `stale_window_hours`
2. If the `postgres-infra` MCP server is available, dispatch the `sweep-health` agent to check
   the most recent completed run age for each upstream domain
3. If any upstream's last run is older than `stale_window_hours`, warn:

   > ⚠ Dependency warning: `{domain}` depends on `{upstream}` (stale threshold: {N}h).
   > Last `{upstream}` completed run: {age}. Consider running `{upstream}` first for accurate results.

4. Proceed if the user confirms — note the staleness in the synthesis section.

This is a **WARN, not a BLOCK**. Report and let the user decide.
When PostgreSQL MCP is unavailable, skip the check and proceed, noting it was skipped.

---

## Creating New Capabilities (subagents / skills)

Creating a **new capability** is meta-work, not domain work — you're authorized to do
it yourself when a task needs a specialist agent or skill that doesn't yet exist.

**Gate — only create when ALL of these hold:**
1. You have checked the existing agents (`.claude/agents/`) and skills (`.claude/skills/`,
   `.claude/commands/`) and **none** fit the task.
2. The capability is **reusable** — it will plausibly be needed again, not a one-off you
   could just do inline this once.
3. The capability has a **clear, single responsibility** you can describe in one line.

If any of these fail, do the work with the closest existing agent/skill instead.

**How to create:**

- **A new infra-brain domain agent** (collects/analyzes infra state): do NOT hand-write it.
  Invoke `/agent-register <name> '<description>' --schedule '<cron>'`, which scaffolds +
  wires + tests it, then validate with `lc-agent-completeness`.

- **A new specialist subagent** (reviewer, researcher, analyst — a Claude Code subagent):
  Write `.claude/agents/<name>.md` with frontmatter:
  ```yaml
  ---
  name: <kebab-name>
  description: >
    <one-paragraph trigger description — when to invoke, what it returns>
  model: <opus | sonnet | haiku>   # REQUIRED — pin per the Step 4 rubric: opus for
                                    # judgment/safety-critical, sonnet for review/
                                    # analysis, haiku for mechanical checks
  allowed-tools: <only if it should be restricted, e.g. Read, Grep, Glob>
  ---
  ```
  Then a body with the agent's checklist and a strict output format. Keep it consistent
  with the existing reviewer/researcher agents. **Never create another `orchestrator`
  agent** — orchestration is this skill, executed by whichever session has the `Agent`
  tool; a spawnable orchestrator subagent cannot do its own job (§0).

- **A new skill** (a reusable workflow/procedure): create `.claude/skills/<name>/SKILL.md`
  with `name` + `description` frontmatter and step-by-step instructions.

**After creating:** announce what you created and why no existing capability fit, then
dispatch it as a normal task in your batch. New safety-relevant agents/skills should be
reviewed by `lc-safety-reviewer` before they touch the safety chain.

---

## Decision Rules: When NOT to Parallelize

Do NOT parallelize tasks that:
1. **Depend on each other's output** — T2 needs T1's result
2. **Must be reviewed before applied** — always dispatch `lc-migration-reviewer` AFTER
   the migration file is generated, not alongside generation
3. **Are inherently sequential flows** — `alembic upgrade head` must come after the
   migration file is reviewed and approved

(File-overlap is no longer a reason to serialize — see Step 5. Worktree isolation plus
the Step 6 rolling-integration procedure handles concurrent file touches, including
shared files, via the Step 6.2 playbook.)

## Efficiency Rules

- For requests with 3+ independent research tasks: ALWAYS parallelize the research batch
- For requests requiring multiple validators: ALWAYS parallelize the validation batch
- For requests touching 2+ independent domains: parallelize implementation, isolated by worktree
- For simple single-agent tasks: skip decomposition, route directly

## Error Handling

If a parallel task fails:
1. Continue the other tasks in the batch (don't cancel the batch)
2. In synthesis, report the failed task first
3. Determine if the failure blocks downstream tasks:
   - If yes: block the dependent batch, report what's blocked
   - If no: continue with what can proceed
4. Provide the specific error and remediation steps

If a worktree branch fails the Step 6.1 sanity check: **quarantine it** (don't fold),
report `UNVERIFIED — claimed but not evidenced`, and treat the task as failed for
synthesis purposes even if the agent itself reported success.

If a validator finds CRITICAL issues:
1. Report them immediately in the synthesis
2. Block any deployment-related steps
3. Provide exact file:line references and fixes
