# Orchestration Redesign Plan — infra-brain Claude Code workflow

**Builds on:** `docs/decisions/2026-07-22-dev-tooling-audit.md` (finding 1). **Status:**
implemented and merged — see "Addendum 2026-07-22" below for the adversarial-audit
closeout.
**Grounding evidence used:** `.claude/agents/orchestrator.md`, `.claude/skills/orchestrator/SKILL.md`,
`.claude/agents/review-coordinator.md`, CLAUDE.md "How Work Gets Done", `.claude/hooks/`
listing, git history around merge `42ebc63` (the merge request that consolidated this
work), file sizes of the shared-file hotspots (the open-findings tracker file 170KB,
`config.py` 37KB, `.env.example` 31KB, `AGENTS.md` 2KB/generated).

---

## 0. The one-sentence model

Orchestration becomes a **top-level mode**: the session loads the orchestrator *skill*
for decomposition and policy, then does the fan-out itself — via plain `Agent()`
batches by default, or via the `Workflow` tool when the maintainer has explicitly opted in — with
**worktree isolation as the default for every code-writing dispatch**, a **rolling
integrate-as-they-complete** consolidation procedure, and a **per-dispatch
model-routing rubric**. The spawnable `orchestrator` subagent is retired.

---

## 1. Verdict: Workflow vs. hand-rolled top-level orchestration

**Verdict: Workflow is the right execution substrate when available, and it natively
covers roughly 60% of the maintainer's two asks — but two load-bearing pieces are policy, not
mechanism, and remain the skill's job under either substrate.** Honest breakdown:

| Need | Workflow provides | Still the skill's job |
|---|---|---|
| Parallel dispatch, concurrency limits | Yes — `parallel()`, `pipeline()`, auto-capped concurrency. Strictly better than hand-batched `Agent()` calls (pipelining without stage barriers is something manual batching cannot express — a manual "batch" is always a barrier). | Deciding the graph shape at all (decomposition). |
| Worktree-per-agent (ask 1, first half) | Yes — `agent(prompt, {isolation:'worktree'})` is first-class; auto-removes clean worktrees; returns path + branch. | The *policy* of when to isolate (§2.1 — we deliberately go stricter than Workflow's own "only when they'd conflict" guidance). |
| **Branch consolidation** (ask 1, second half — "resolve work trees when the agent is finished") | **No.** Workflow hands back N worktree paths and N branches. Merging them into one coherent, testable, pushable result is entirely outside the tool. This is the biggest gap and the core of today's manual-consolidation pain. | The full integration procedure in §2.3 — needed identically in both modes. |
| Per-agent model (ask 2, mechanism) | Yes — `{model, effort}` per call. | The *rubric* for choosing (§3). Workflow's own guidance is "only set model when you're highly confident a different tier fits" — the rubric is precisely the definition of "highly confident." |
| Cost awareness | Yes — `budget` object. | When to consult it (dispatch-count scaling). |
| Evidence-gated synthesis, shared-file conventions, routing to project skills/reviewers | No. | All skill. |

**Gating reality:** the maintainer has not said "ultracode"/"workflow"/"multi-agent
orchestration" in his own words; he described capabilities. So the design is
**dual-substrate with one policy layer**:

- **Default path (no opt-in):** top-level session loads the orchestrator skill,
  decomposes, then dispatches `Agent()` batches directly, setting
  `isolation: "worktree"` and `model:` per call according to §2/§3, and runs the §2.3
  integration procedure itself between/after batches.
- **Opt-in path:** if the maintainer explicitly opts in (the skill should say exactly this:
  *"Workflow-scale orchestration requires you to ask for it — say 'use a workflow' /
  'ultracode'"*), the same decomposition compiles to a Workflow script using
  `pipeline()` where the graph is multi-stage, with the same isolation and model
  parameters. Integration (§2.3) still runs in the top-level session afterward,
  consuming the branch names Workflow returns.
- **Recommendation to the maintainer:** worth opting in for any task graph with ≥4 code-writing
  tasks or any multi-stage per-item shape (e.g., "for each of 5 agents: implement →
  test → review" — pipeline overlap is a real wall-clock win manual batching can't
  get). For 2–3 tasks, plain `Agent()` is fine and avoids the script-authoring overhead.

**Consequence for the skill's scope:** its job shrinks to (a) decomposition + routing,
(b) the isolation/integration policy, (c) the model rubric, (d) evidence-gated
synthesis. That's a healthy shrink — the current agent file's dispatch-mechanics prose
gets replaced by the substrate.

**Confidence:** High on the gap analysis (capabilities were provided verbatim). One
open verification item carried from the audit: before finalizing, run the 2-minute
probe (spawn a trivial subagent whose only task is "list your available tools") to
re-confirm subagents lack `Agent` — the audit itself flags its historical inference as
unconfirmed.

---

## 2. Worktree lifecycle design

### 2.1 When to isolate — default-on for code-writers, and why that beats Workflow's selective heuristic here

Workflow's documented philosophy is "isolate only when agents mutate files in parallel
and would otherwise conflict." **For this repo, adopt the stricter policy:
`isolation: 'worktree'` is mandatory for every dispatch that may write git-tracked
files, with a single explicit override token for exceptions.** Reasoning:

1. **The costs are asymmetric.** Over-isolation costs ~200–500ms + disk + a
   usually-trivial fast-forward at integration. Under-isolation cost today was branch
   hijacking, file cross-contamination, and a manual `git rebase --onto` +
   `merge-base --is-ancestor` + hand-resolved tracker-file conflict session — and per
   audit finding 2 this was a **repeat** of an already-documented incident class. Two
   occurrences of the expensive failure vs. a sub-second premium.
2. **The selective heuristic requires predicting each agent's file-touch set in
   advance, and that prediction is exactly what failed.** infra-brain tasks that look
   file-disjoint on paper almost all converge on the same shared files in practice —
   the open-findings tracker file (every task logs a row), `AGENTS.md`, `config.py`/`.env.example`
   (any new flag). In this repo, "would otherwise conflict" evaluates to *true* for
   essentially every pair of code-writing tasks, so the selective heuristic and
   default-on converge — default-on is just the version that doesn't depend on a
   fallible prediction.
3. **It's mechanically enforceable.** Audit item 2's proposed `PreToolUse` hook (block
   code-writing `Agent` dispatches lacking `isolation:"worktree"` unless an explicit
   override token is present, modeled on the existing
   `block-orchestrator-recursion.py`) should ship as part of this redesign, not
   separately. Policy that lives only in prose regressed once already.

**Exemptions (no worktree):** read-only dispatches — `infra-researcher`, all
`lc-*-reviewer` agents, `sweep-health`, `drift-analyst` — and the degenerate case of a
batch with exactly one code-writing agent *and* no concurrent top-level edits (still
fine to isolate; the exemption exists so the hook override has a legitimate use).

**Preconditions before any fan-out (new hard gate):** the base working tree must be
**clean** (`git status --porcelain` empty — commit or stash first), and the session
records `BASE=$(git rev-parse HEAD)`. Today's incident began with three agents sharing
one dirty tree; a clean recorded base is what makes everything in §2.3 mechanical.

### 2.2 Per-agent contract

Every code-writing dispatch's prompt must include (skill provides the template):

- **Branch naming:** `wt/<batch-slug>/<task-id>-<short-name>` (e.g.
  `wt/findings-117/T4-phase1-n1`). In Workflow mode, record the branch name it returns
  instead.
- **Rules:** branch from BASE only; commit locally, small and focused (per existing
  merge policy); **never push, never switch branches, never rebase, never touch files
  outside the worktree**.
- **Shared-file rule (§2.4):** do NOT edit the open-findings tracker file or `AGENTS.md`;
  report intended tracker row content in the result instead. `config.py`/`.env.example`
  edits must be additive-only.
- **Result contract (proof, not prose):** branch name, tip SHA,
  `git diff --name-only BASE..HEAD` output, and test-command + result line. (This
  extends the existing F-015 evidence rule to worktree outputs.)

### 2.3 Integration: rolling, integrate-as-they-complete

**Do not wait for all N and then reconcile** — that is exactly the end-state-tangle
that produced today's manual merge-request surgery. Instead, fold each branch in as its agent
finishes, while slower agents are still running (integration work hides inside the
parallel window). Each conflict is then pairwise, small, and fresh — never N-way.

**Procedure** (top-level session runs this; `integration` starts as a new branch at
BASE):

**Per completing agent T_i:**

1. **Verify the branch is sane** (guards against exactly today's hijacking class):
   ```bash
   git merge-base --is-ancestor $BASE wt/<slug>/Ti        # branch really descends from BASE
   git log --oneline $BASE..wt/<slug>/Ti                   # commits exist and are only Ti's
   git diff --name-only $BASE..wt/<slug>/Ti                # matches the agent's reported file list
   ```
   Any mismatch → quarantine the branch (do not fold), report as UNVERIFIED per F-015.
2. **Predict conflicts:** intersect Ti's file list with the union of files already
   folded into `integration`. Empty intersection → step 3 will be clean; non-empty →
   you know the conflict files before touching git.
3. **Fold:** `git rebase --onto integration $BASE wt/<slug>/Ti && git branch -f integration wt/<slug>/Ti`
   (equivalently: cherry-pick the range onto `integration`). First finisher is a
   trivial fast-forward.
4. **Resolve conflicts now,** using the per-file playbook in §2.4.
5. **Verify semantically, not just textually:** run the *fast tier* on `integration`
   after every fold — `ruff check` + the focused pytest subset for the touched areas +
   the relevant guard hooks' checks (`env-parity`, `agent-registry-sync` invariants).
   This catches the conflict class git can't see: two branches that merge cleanly but,
   e.g., both add a Settings field or claim the same cron slot (today's actual
   schedule-collision bug class — `f385ebf`).
6. **Clean up immediately:** once
   `git merge-base --is-ancestor <Ti-tip> integration` confirms the commits are in,
   ```bash
   git worktree remove <path> && git branch -d wt/<slug>/Ti
   ```
   (Workflow auto-removes *clean* worktrees only; dirty-with-changes worktrees are
   returned to you and this step applies identically.) End-of-session:
   `git worktree list` must show only the main tree; `git worktree prune` as backstop.
   Manual-mode worktrees live in a dedicated git-ignored location outside the repo tree
   (e.g. `../infra-brain.wt/<branch>`), never nested in the repo.

**After the last fold:** full test suite on `integration`, then tracker-file/AGENTS.md
updates (§2.4), then rename/merge `integration` into the session's feature branch →
one MR-gated push per the real merge policy. If any branch touched `db/models/`,
dialect types, or raw SQL: `/pg-gate-check` before push (existing rule, unchanged).

**Worked example — today's realistic case:** T1 (graph_maintenance perf), T2 (RAG
hardening), T3 (LLM hardening) all branch from BASE; T1 and T3 would both have edited
the tracker file. T2 finishes first → `integration` fast-forwards to T2. T1 finishes →
file-intersection with {T2's files} is empty → clean rebase, fold, fast tests. T3
finishes → intersection with folded set = `{tracker file}` under the old behavior —
but under the §2.4 rule neither T1 nor T3 edited the tracker file at all; each *reported* its
row, and the session writes both rows once at the end. The conflict that took manual
surgery today simply never exists. Under the old behavior it would have been a
pairwise append/append conflict resolved by keeping both hunks — annoying but 30
seconds; under the new convention it's zero.

### 2.4 Shared-file conflict playbook (convention layer — works with or without any tooling)

| File | Rule for parallel subagents | Integration handling |
|---|---|---|
| Open-findings tracker file | **Never edited by subagents.** Each reports its row(s)/status-changes as structured text in its result. | Session writes all rows in one commit after the last fold. Removes the single most frequent conflict source (every task writes here; 170KB file, a planned split will help too). |
| `AGENTS.md` | Never hand-edited by subagents (it is generated from AgentSpec anyway). | Regenerate once on `integration` after all folds. |
| `src/infra_brain/config.py` / `.env.example` | **Additive-only** in subagents: append new Settings fields / env entries in a clearly-delimited own block; never reorder, rename, or refactor existing entries. Any config *refactor* is a singleton task — never scheduled parallel with anything else touching config. | Append/append conflicts at the same location resolve as keep-both-hunks; then the existing `env-parity-guard` check + `high-blast-radius-test` subset run in the step-5 fast tier catch semantic duplication. |
| `supervisor.py` AGENT_REGISTRY / `scheduler.py` schedules | Additive-only; each task's decomposition row must declare the registry/schedule entries it will add, so cron-slot collisions are caught **at decomposition time**, not at integration (today's `f385ebf` collision class). | `agent-registry-sync` invariants in the fast tier. |
| Everything else | Decomposition must declare expected file touches per task. Two tasks declaring the same non-shared file → serialize them or re-cut the boundary. Undeclared overlap discovered at step 2 → resolve pairwise, note it, and tighten the next decomposition. | Standard pairwise resolution. |

Deliberately rejected: `.gitattributes merge=union` on the tracker file — union-merge
silently keeps both sides of *edits to existing lines* (status changes corrupt into
duplicates). The report-and-centralize rule is strictly safer and also simpler.

**Confidence:** the git mechanics are exactly the commands that resolved today's
merge-request consolidation, systematized — high confidence. The "subagents never
touch the tracker file" rule is new convention; it costs a small prompt-contract
addition and eliminates the dominant conflict, but depends on agents complying — the
step-1 file-list verification is the enforcement backstop (a branch touching the
tracker file when it declared it wouldn't fails verification).

---

## 3. Model-routing rubric

Four tiers: **Fable** (deep judgment), **Opus** (`claude-opus-4-8`, high-stakes
implementation), **Sonnet** (scoped implementation/review), **Haiku** (mechanical).
Calibration evidence from today: Fable5 did the graph_maintenance root-cause, the RAG
architecture review, the LiteLLM/Langfuse evaluation, and the dev-tooling audit — all
deep/ambiguous, all excellent and evidence-grounded. Opus-tier subagents did well-scoped
implement-test-commit work — solid, but at Opus cost for work that was largely
mechanical once scoped.

**Decision procedure — apply the first rule that fires** (this is the concrete
algorithm for writing an `Agent()`/`agent()` call's `model:` param):

| # | If the subtask is… | Tier | Effort | Rationale / evidence |
|---|---|---|---|---|
| 1 | Investigation, root-cause, audit, architecture/design review, any task where the *success criterion itself* must be discovered; output is a diagnosis/decision/report, not a diff | **Fable** | high | Directly evidence-backed: all four of today's Fable assignments. **High confidence.** |
| 2 | Writes to Critical-Files-table paths or the safety chain (`callbacks/`, `supervisor.py`, `db/models/`, `config.py`, `db/session.py`, `graph.py`), or generates a migration | **Opus** | default | Blast-radius-if-wrong dominates cost; today's Opus implementation work fits here. **High confidence.** |
| 3 | Code diff with a crisp spec in leaf/isolated scope: one domain agent module, one tool + test, one dashboard `.tsx` component, test-writing against a defined behavior | **Sonnet** | default | This is the "solid but Opus was overkill" bucket the maintainer observed. **Medium confidence — extrapolated, not yet A/B'd; see calibration note.** |
| 4 | Mechanical, near-zero judgment: run a test/lint command and report, grep/registry/wiring checks, apply a precisely-specified diff, extract exact signatures/strings from named files, draft a tracker row from a template | **Haiku** | default | Output is deterministic given the instructions. **Medium confidence.** |
| 5 | Read-only research fan-out (`infra-researcher`) | **Sonnet** default; **Haiku** when the ask is pure extraction (rule 4 shape) | default | Pattern-summarization needs some judgment; verbatim fetch doesn't. |
| 6 | None of the above fires cleanly | **Omit `model:`** (inherit session model) | — | Matches Workflow's own guidance: only set it when confident. The rubric *is* the confidence test; no clean match = no override. |

**Standing modifiers:**
- **Specialist reviewers keep their own pinned models** (each `.claude/agents/lc-*.md`
  pins one; `review-coordinator.md` is sonnet). Do not override reviewer models
  per-dispatch — their pins were set to their checklists' cognitive load, and
  `lc-safety-reviewer` in particular must never be down-tiered.
- **Escalate on stuckness, never loop:** a Sonnet/Haiku agent reporting uncertainty, or
  failing verification twice, gets re-dispatched one tier up with the failure context —
  cheaper than N retry loops at the low tier.
- **Ambiguity beats mechanics:** a task that *looks* mechanical but sits inside an
  unresolved design question routes by rule 1, not rule 4 (decompose better instead, if
  possible).
- **Budget interplay (Workflow mode):** if `budget.remaining()` is under ~2× the
  estimate for remaining dispatches, prefer the lower tier wherever rules 3/4 plausibly
  apply; never budget-downgrade rules 1–2.
- **Decomposition itself** happens in the top-level session (whatever it runs on) — it
  is no longer a reason to spawn anything, which by itself deletes the largest standing
  Opus cost (an Opus orchestrator subagent wrapping every task).

**Calibration note for the maintainer:** rows 1–2 are evidence-backed today; rows 3–4 are
reasoned extrapolation. Suggest running rows 3–4 as the default for ~2 weeks, noting
any tier-mismatch (escalations fired, or Opus used where Sonnet output would clearly
have sufficed) in day-close session notes, then hardening or adjusting the table.
Cheap, reversible, produces its own evidence.

---

## 4. Revised `.claude/skills/orchestrator/SKILL.md` outline

Frontmatter: keep name `orchestrator`; rewrite `description` to "Top-level
orchestration mode for any substantive task: decompose into a task graph, dispatch
specialist agents in parallel *from this session*, isolate code-writers in worktrees,
integrate results incrementally, synthesize with evidence. Never spawnable as a
subagent."

Sections (with the load-bearing content each must contain):

1. **You are the orchestrator — there is no orchestrator agent.** The inversion,
   stated once, hard: this skill loads into the top-level session, which does the
   `Agent()`/`Workflow` fan-out itself. `Agent(subagent_type="orchestrator")` must
   never appear; the `.claude/agents/orchestrator.md` definition is retired. Subagents
   that need review dispatch report the need upward; they never fan out.
2. **Classify the request.** Carry the SIMPLE/COMPOUND/PARALLEL/COMPLEX table
   verbatim; SIMPLE routes directly with no ceremony (unchanged).
3. **Decompose into a task graph.** Carry the current Step-2 discipline (IDs, type
   `research|implement|validate|verify`, deps, outputs; type-based parallelizability
   rules) **plus two new required per-task columns:** `writes-files?` (drives
   isolation) and `declared file touches, shared files flagged` (drives §2.4
   serialization/lifting decisions and cron-collision checks at plan time).
4. **Choose the execution substrate.** Default: direct `Agent()` batches, all
   independent calls of a batch in ONE response. Workflow path: only when the maintainer has
   explicitly opted in (list the exact trigger phrases); recommended at ≥4
   code-writing tasks or per-item multi-stage shape (use `pipeline()`); same isolation
   + model params; integration still runs here afterward.
5. **Model routing.** The §3 table + decision procedure + modifiers, verbatim.
6. **Worktree lifecycle.** Preconditions (clean tree, record BASE); default-on
   isolation policy + override token (matching the enforcement hook); branch naming;
   the per-agent prompt contract from §2.2 as a copy-paste template.
7. **Rolling integration.** The §2.3 six-step per-completion procedure with the actual
   git commands; the §2.4 shared-file playbook table; fast-tier-per-fold /
   full-suite-at-end; worktree removal + prune hygiene; quarantine rule for branches
   failing verification.
8. **Evidence-gated synthesis.** Carry the F-015 hard rule and the 6-part synthesis
   format from the current agent file verbatim, extended with the §2.2 branch/SHA/file-
   list artifacts.
9. **Routing tables.** Carry the skill's existing 20-scenario tree and the agent file's
   task→skill/agent tables (merged, deduplicated). **Fold `review-coordinator`'s
   content in here as a "Review batches" subsection** — its file-pattern→reviewer
   matrix and four prompt templates are good and survive intact; only its broken
   coordination shell (a subagent that can't spawn) is deleted. Reviewer dispatches are
   just another parallel validate batch from the top level.
10. **Merge policy (corrected).** Replaces the current Critical-Rule 10: *"Feature
    branch → small local commits → integration branch per §7 → ONE push + GitLab MR
    when the unit of work is done (confirm with the user first) → MR gates
    (`migration-parity`, `sql-execution-check`) → merge. Never push directly to
    master."* The same correction must hit the two workflow blocks that currently say
    `git push origin master → CI deploys` (Scenario-10 step and the "Add a new Windows
    Ansible sweep agent" walkthrough) — otherwise the wrong instruction survives in
    examples even after rule 10 is fixed.
11. **Dependency-aware sweep sequencing.** Carry verbatim (WARN-not-BLOCK,
    `agent-dependencies.yaml`).
12. **Creating new capabilities.** Carry the 3-condition gate verbatim, minus the
    "never another orchestrator" clause (moot once the agent is gone).

**Companion changes the outline implies (list them here so nothing is orphaned):**
- Delete `.claude/agents/orchestrator.md` and `.claude/agents/review-coordinator.md`
  (content absorbed per above).
- Rewrite CLAUDE.md "How Work Gets Done": "spawn the orchestrator agent" → "enter
  orchestration mode: invoke the `/orchestrator` skill, then fan out from the top
  level"; delete the recursion warning block (no longer applicable); update the
  Available Subagents table.
- Update `.claude/hooks/orchestrator-default.py` to inject the new routing reminder
  (load the skill, don't spawn); keep `block-orchestrator-recursion.py` temporarily as
  belt-and-braces against stale habits, retire later.
- Add the audit-item-2 isolation-enforcement `PreToolUse` hook as part of this change,
  not separately.
- First implementation step: the audit's 2-minute tool-list probe, so the redesign's
  premise is re-confirmed on the record.

---

## 5. What NOT to change

Explicitly carried forward unchanged — these are good and redesigning them would be
motion, not progress:

1. **The decomposition discipline** in `.claude/agents/orchestrator.md` Steps 1–2
   (request classification; task IDs/types/deps/outputs; the type-based
   parallelizability rules). This is the part of the current design that was never
   broken — only the *executor* of the plan was.
2. **The batch-dispatch hard rule** ("all independent calls of a batch in a single
   response"). It is exactly how top-level parallelism has actually worked all session
   — the audit confirms top-level fan-out is the thing that functions today.
3. **The F-015 evidence-gated synthesis rule**, verbatim. It was written from a real
   incident (a subagent reporting an unlanded commit) and slots perfectly into the
   worktree contract (branch/SHA verification is just F-015 with better artifacts).
4. **The error-handling rules** (continue the batch on single failure; report failures
   first; assess downstream blockage) — they map one-to-one onto the quarantine rule in
   §2.3.
5. **The specialist reviewer agents themselves** (`lc-safety-reviewer`,
   `lc-migration-reviewer`, `lc-api-reviewer`, `lc-agent-completeness`) and their
   pinned models — they work correctly as leaf subagents; the problem was only ever the
   coordination layer above them.
6. **Dependency-aware sweep sequencing** (WARN-not-BLOCK) and the **new-capability
   creation gate** (3 conditions).
7. **Per the audit's own "recommended against" list:** no task-board machinery
   (worktree-by-default removes the shared-mutable-tree class that motivated it), no
   CI-gate reinstatement (standing decision, reaffirmed 2026-07-22), no skill
   consolidation beyond folding review-coordinator in.

---

## Confidence summary

- **High:** the Workflow gap analysis (§1); the integration git mechanics (§2.3 —
  systematization of commands actually run today for the manual-consolidation incident);
  default-on isolation (§2.1
  — asymmetric-cost argument plus a documented repeat incident); rubric rows 1–2 (§3 —
  directly evidence-backed); everything in §5.
- **Medium:** rubric rows 3–4 (extrapolated — 2-week calibration proposed); the
  "subagents never edit the tracker file" convention (eliminates the dominant conflict but
  relies on prompt compliance, backstopped by file-list verification).
- **Open verification item before implementation:** the audit's tool-list probe
  re-confirming subagents lack `Agent` (2 minutes); and note the fast-tier test
  selection in §2.3 step 5 assumes the focused pytest subsets in CLAUDE.md remain
  representative — they are today.

---

## Addendum 2026-07-22 — adversarial audit + remediation, closed out

Implementation landed as commit `784102d`. A 4-way adversarial audit (4 independent
agents, one per angle: hook edge-cases, repo-wide dangling refs, cross-doc consistency,
SKILL.md + tool-capability verification) then reviewed it and found real bugs, all
fixed via 4 parallel Opus remediation agents plus manual integration:

- `require-worktree-isolation.py`: fixed write-verb false negatives (word-boundary
  regex), crash-safety on malformed `tool_input`/`prompt`/`description` (the hook must
  never raise — always a deliberate exit 0/2), and an override-token substring bypass
  (`NO_WORKTREE_OK` now word-boundary matched). Added `tests/hooks/
  test_require_worktree_isolation.py` (60 cases) — the "tested against N cases" claim
  now has a re-runnable artifact.
- Deleted `.claude/hooks/block-orchestrator-recursion.py` (permanently unreachable
  dead code — its target subagent type no longer exists) and its `settings.json`
  registration; 3 independent audits had converged on this same file as confusing.
- Fixed stale `review-coordinator`/`/orchestrate` references in
  `.claude/skills/onboard/SKILL.md`.
- Fixed `.claude/skills/orchestrator/SKILL.md`: broken cross-refs (3, not just the
  1 originally flagged), restored "pass context between batches" guidance, aligned
  the worktree-exemption prose with the hook's exact `READ_ONLY_SUBAGENTS` set, fixed
  stale present-tense "silently degrades" wording, added a hook-table row for
  `orchestrator-default`, and added a note that `Agent`'s `isolation`/`model` params
  and `Workflow`'s capabilities were **directly verified against the live tool
  schemas by the top-level session** (the only context that can see them) — this
  resolves, rather than merely asserts past, the audit's one CRITICAL-severity
  finding (whether those parameters exist at all), which no subagent can check for
  itself since subagents have no visibility into the `Agent`/`Workflow` tool schemas.

**New finding from remediation itself, worth carrying forward:** 3 of the 4
remediation agents' `isolation:"worktree"` dispatches were, empirically, branched
from stale `master` rather than from this branch's actual tip — even though all 4
were dispatched identically from the same session in the same state. One agent
(the 4th) independently noticed its own mis-basing and self-corrected with
`git reset --hard <intended-base>` before editing; the other 3 did not notice and
proceeded, silently reverting every file in the redesign except the ones they
themselves touched.

This is real, concrete evidence *for* a check the skill already specifies — §6.1
step 1 (`git merge-base --is-ancestor $BASE wt/<slug>/Ti`, quarantine on mismatch) —
not a gap in the documented procedure. The gap was in execution: the dispatching
session didn't run that check until after manually reviewing file-level diffs looked
suspicious, rather than immediately per completed agent as §6.1 already prescribes.
Corrected here by extracting each agent's intended file-level changes surgically
(rather than merging the mis-based branches wholesale) once the mismatch was found.
No skill-doc change needed — the existing §6.1 step 1 was correct all along; this is
a reminder to actually run it first, every time, not just when something looks off.

Full suite green after integration: 2850 passed, 22 skipped, 0 failed.
