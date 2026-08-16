"""infra_brain.api.routers.llm_observability -- LLM audit surface (T7, rev14).

Answers, for an operator who has never run an agentic system:
  * Is the LLM being used at all, by which agents, and how recently?
  * What did it cost, in tokens, and what exactly does that number measure?
  * Is anything looping (same tool hammered inside one turn), hitting the
    recursion limit, or getting cut off?
  * What did it actually *decide* — the per-iteration ladder with tokens,
    tools, and reasoning.

Source of truth is ``agent_decision_log``, written by
``LLMAgent._log_decisions`` / ``_log_recursion_limit_hit``
(``agents/llm_base.py``). Nothing here assumes Langfuse or any external tracing
backend: ``langfuse_enabled`` is off and the stack is not deployed, so this page
has to stand on the database alone.

Grain, stated once and reused everywhere (see ``api/schemas.py``'s block
comment for the full definitions):

  run       one ``LLMAgent.reason()`` call. ``AgentDecisionLog.run_id`` is a
            uuid4 minted inside ``reason()`` — it is NOT a ``CollectionRun.id``
            and does not join to one.
  turn      one model call (one ``AIMessage``), ``iteration >= 0``.
  marker    ``iteration == -1`` + ``decision_summary == RECURSION_LIMIT_MARKER``
            — not a turn; it records that the ReAct loop ran out of steps.

Outcome classification (derived, never stored):
  recursion_limit  a marker row exists for the run.
  completed        the last turn made no tool calls -> the model produced a
                   final answer and the loop exited normally.
  truncated        the last turn DID make tool calls and no marker exists ->
                   the loop stopped without answering (deadline preemption,
                   transport error, or a partial checkpoint recovery).
  unknown          no real turns at all (marker only, or an empty run).

Boundedness: ``tools_chosen`` is a JSON column, not a native array, so per-tool
frequency cannot be aggregated in portable SQL (sqlite in the suite, PostgreSQL
in production). Everything that needs it is therefore folded in Python over a
**capped** row scan — never an unbounded dump — and the response states
``rows_scanned`` / ``truncated_scan`` so a partial answer is visibly partial.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func

from infra_brain.agents.llm_base import RECURSION_LIMIT_MARKER
from infra_brain.api.schemas import (
    TOKEN_METRIC,
    LLMAgentStatsOut,
    LLMFlagOut,
    LLMOutcomeCountsOut,
    LLMRunDetailOut,
    LLMRunOut,
    LLMRunPageOut,
    LLMStepOut,
    LLMSummaryOut,
    LLMToolUseOut,
)
from infra_brain.config import get_settings
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import AgentDecisionLog
from infra_brain.db.session import get_session

llm_observability_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)

# How many decision-log rows a single summary request may read. The whole live
# table is ~230 rows today; the cap exists so this endpoint stays bounded if the
# table grows by three orders of magnitude, not because it is close to binding.
SUMMARY_SCAN_CAP = 20_000

# Default lookback for the summary. A week covers the daily collection cadence
# with room for a weekend gap, so "0 runs" over this window is a real signal.
DEFAULT_WINDOW_HOURS = 168
MAX_WINDOW_HOURS = 24 * 90

OUTCOME_COMPLETED = "completed"
OUTCOME_RECURSION = "recursion_limit"
OUTCOME_TRUNCATED = "truncated"
OUTCOME_UNKNOWN = "unknown"

_OUTCOME_REASONS = {
    OUTCOME_COMPLETED: (
        "The final model turn made no tool calls — the ReAct loop exited with an answer."
    ),
    OUTCOME_RECURSION: (
        "The loop hit its recursion limit (2 * max_iters + 1 graph steps) before "
        "converging. reason() returned the last mid-exploration text, which is not "
        "the answer the caller asked for."
    ),
    OUTCOME_TRUNCATED: (
        "The last recorded turn was still calling tools and no recursion-limit marker "
        "was written — the loop stopped without answering (wall-clock deadline "
        "preemption, a transport error, or a partial checkpoint recovery)."
    ),
    OUTCOME_UNKNOWN: "No model turns were recorded for this run.",
}

# Reasoning-absence states. The column is NOT NULL, so "no narration" and
# "capture failed" would otherwise be the same empty string on the wire. These
# let the UI say which one it is instead of rendering an unexplained blank.
REASONING_PRESENT = "present"
REASONING_ABSENT_TOOL_TURN = "absent_tool_call_turn"
REASONING_ABSENT_NO_NARRATION = "absent_no_narration"


def _flags(settings) -> list[LLMFlagOut]:
    """The default-off switches that make a section of this page legitimately
    empty. Reported so "nothing here" is never mistaken for "nothing happened"."""
    return [
        LLMFlagOut(
            name="rootcause_llm_enabled",
            enabled=bool(getattr(settings, "rootcause_llm_enabled", False)),
            effect="Off: root-cause analysis is deterministic — no LLM runs are produced by it.",
        ),
        LLMFlagOut(
            name="compliance_gap_finder_enabled",
            enabled=bool(getattr(settings, "compliance_gap_finder_enabled", False)),
            effect="Off: the compliance gap finder never calls the model, so it logs no runs.",
        ),
        LLMFlagOut(
            name="remediation_interrupt_enabled",
            enabled=bool(getattr(settings, "remediation_interrupt_enabled", False)),
            effect=(
                "Off: remediation never pauses for human approval mid-run, so no "
                "interrupt-resumed runs appear."
            ),
        ),
        LLMFlagOut(
            name="langfuse_enabled",
            enabled=bool(getattr(settings, "langfuse_enabled", False)),
            effect=(
                "Off: no external trace backend. This page is built entirely from "
                "agent_decision_log rows in the local database."
            ),
        ),
    ]


def _tool_names(raw) -> list[str]:
    """``tools_chosen`` as a clean list of names. The column is JSON and is
    written as a list of strings, but a hand-seeded or legacy row could hold
    anything — coerce defensively rather than 500 on one bad row."""
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, str)]


def _reasoning_state(reasoning_len: int, tools: list[str]) -> str:
    if reasoning_len > 0:
        return REASONING_PRESENT
    return REASONING_ABSENT_TOOL_TURN if tools else REASONING_ABSENT_NO_NARRATION


class _RunFold:
    """Accumulator for one run's rows. Rows arrive in ascending ``iteration``."""

    __slots__ = (
        "run_id",
        "agent",
        "domain",
        "started_at",
        "ended_at",
        "turns",
        "tokens_billed",
        "peak_call_tokens",
        "tool_calls",
        "tool_counter",
        "max_tool_repeat",
        "narrated_turns",
        "silent_turns",
        "has_marker",
        "last_turn_had_tools",
    )

    def __init__(self, run_id: str, agent: str, domain: str, ts: datetime):
        self.run_id = run_id
        self.agent = agent
        self.domain = domain
        self.started_at = ts
        self.ended_at = ts
        self.turns = 0
        self.tokens_billed = 0
        self.peak_call_tokens = 0
        self.tool_calls = 0
        self.tool_counter: Counter[str] = Counter()
        self.max_tool_repeat = 0
        self.narrated_turns = 0
        self.silent_turns = 0
        self.has_marker = False
        self.last_turn_had_tools = False

    def add(self, iteration, ts, token_count, tools, reasoning_len, summary) -> None:
        if ts < self.started_at:
            self.started_at = ts
        if ts > self.ended_at:
            self.ended_at = ts
        if iteration < 0:
            # Marker row: not a turn. Only its outcome signal counts.
            if summary == RECURSION_LIMIT_MARKER:
                self.has_marker = True
            return
        self.turns += 1
        tokens = token_count or 0
        self.tokens_billed += tokens
        self.peak_call_tokens = max(self.peak_call_tokens, tokens)
        self.tool_calls += len(tools)
        self.tool_counter.update(tools)
        if tools:
            self.max_tool_repeat = max(self.max_tool_repeat, max(Counter(tools).values()))
        if reasoning_len > 0:
            self.narrated_turns += 1
        else:
            self.silent_turns += 1
        self.last_turn_had_tools = bool(tools)

    @property
    def outcome(self) -> str:
        if self.has_marker:
            return OUTCOME_RECURSION
        if self.turns == 0:
            return OUTCOME_UNKNOWN
        return OUTCOME_TRUNCATED if self.last_turn_had_tools else OUTCOME_COMPLETED

    def to_run_out(self) -> LLMRunOut:
        return LLMRunOut(
            run_id=self.run_id,
            agent=self.agent,
            domain=self.domain,
            started_at=self.started_at,
            ended_at=self.ended_at,
            turns=self.turns,
            tokens_billed=self.tokens_billed,
            peak_call_tokens=self.peak_call_tokens,
            tool_calls=self.tool_calls,
            distinct_tools=len(self.tool_counter),
            max_tool_repeat=self.max_tool_repeat,
            narrated_turns=self.narrated_turns,
            silent_turns=self.silent_turns,
            outcome=self.outcome,
        )


def _fold_rows(rows) -> dict[str, _RunFold]:
    """Group already-ordered decision rows into per-run accumulators.

    ``rows`` must be tuples of
    ``(run_id, agent, domain, iteration, ts, token_count, tools_chosen,
    decision_summary, reasoning_len)`` sorted by ``(run_id, iteration)`` so
    ``last_turn_had_tools`` genuinely reflects the LAST turn.
    """
    folds: dict[str, _RunFold] = {}
    for run_id, agent, domain, iteration, ts, token_count, tools_raw, summary, rlen in rows:
        key = str(run_id)
        fold = folds.get(key)
        if fold is None:
            fold = _RunFold(key, agent, domain, ts)
            folds[key] = fold
        fold.add(iteration, ts, token_count, _tool_names(tools_raw), rlen or 0, summary)
    return folds


def _summary_columns():
    """Column list for the scan queries. Deliberately excludes
    ``reasoning_text`` — it is the widest column in the table and only its
    LENGTH is needed to tell a narrated turn from a silent one."""
    return (
        AgentDecisionLog.run_id,
        AgentDecisionLog.agent,
        AgentDecisionLog.domain,
        AgentDecisionLog.iteration,
        AgentDecisionLog.ts,
        AgentDecisionLog.token_count,
        AgentDecisionLog.tools_chosen,
        AgentDecisionLog.decision_summary,
        func.length(func.coalesce(AgentDecisionLog.reasoning_text, "")).label("reasoning_len"),
    )


@llm_observability_router.get("/llm/summary", response_model=LLMSummaryOut)
def get_llm_summary(
    window_hours: int = Query(DEFAULT_WINDOW_HOURS, ge=1, le=MAX_WINDOW_HOURS),
):
    """Fleet-level LLM usage over a rolling window, plus per-agent breakdown.

    Read-only. Bounded by ``SUMMARY_SCAN_CAP`` rows (newest first); when the cap
    binds, ``truncated_scan`` is true and every figure below covers only the
    rows actually read.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    with get_session() as s:
        # Newest-first + cap, then re-sort in Python by (run_id, iteration) so
        # the fold sees each run's turns in order. Sorting by (run_id,
        # iteration) in SQL instead would make the cap slice an arbitrary set of
        # runs rather than the most recent ones.
        rows = (
            s.query(*_summary_columns())
            .filter(AgentDecisionLog.ts >= since)
            .order_by(AgentDecisionLog.ts.desc())
            .limit(SUMMARY_SCAN_CAP + 1)
            .all()
        )

    truncated_scan = len(rows) > SUMMARY_SCAN_CAP
    if truncated_scan:
        rows = rows[:SUMMARY_SCAN_CAP]
    rows_scanned = len(rows)

    ordered = sorted(rows, key=lambda r: (str(r[0]), r[3]))
    folds = _fold_rows(ordered)

    outcomes = LLMOutcomeCountsOut()
    tool_totals: Counter[str] = Counter()
    tool_peaks: Counter[str] = Counter()
    by_agent: dict[tuple[str, str], LLMAgentStatsOut] = {}

    for fold in folds.values():
        outcome = fold.outcome
        setattr(outcomes, outcome, getattr(outcomes, outcome) + 1)
        tool_totals.update(fold.tool_counter)
        if fold.max_tool_repeat:
            for tool in fold.tool_counter:
                tool_peaks[tool] = max(tool_peaks[tool], fold.max_tool_repeat)

        key = (fold.agent, fold.domain)
        stats = by_agent.get(key)
        if stats is None:
            stats = LLMAgentStatsOut(
                agent=fold.agent,
                domain=fold.domain,
                runs=0,
                turns=0,
                tokens_billed=0,
                peak_call_tokens=0,
                tool_calls=0,
                narrated_turns=0,
                silent_turns=0,
                completed=0,
                recursion_limit=0,
                truncated=0,
                last_run_at=None,
            )
            by_agent[key] = stats
        stats.runs += 1
        stats.turns += fold.turns
        stats.tokens_billed += fold.tokens_billed
        stats.peak_call_tokens = max(stats.peak_call_tokens, fold.peak_call_tokens)
        stats.tool_calls += fold.tool_calls
        stats.narrated_turns += fold.narrated_turns
        stats.silent_turns += fold.silent_turns
        if outcome in (OUTCOME_COMPLETED, OUTCOME_RECURSION, OUTCOME_TRUNCATED):
            setattr(stats, outcome, getattr(stats, outcome) + 1)
        if stats.last_run_at is None or fold.ended_at > stats.last_run_at:
            stats.last_run_at = fold.ended_at

    top_tools = [
        LLMToolUseOut(tool=name, calls=calls, max_in_one_iteration=tool_peaks.get(name, 0))
        for name, calls in tool_totals.most_common(12)
    ]

    agent_list = sorted(by_agent.values(), key=lambda a: (-a.tokens_billed, a.agent))
    provider = (getattr(settings, "llm_provider", "") or "anthropic").lower()
    if provider == "openai":
        model = getattr(settings, "openai_model", "") or ""
    elif provider == "bedrock":
        model = getattr(settings, "bedrock_model_id", "") or ""
    else:
        model = getattr(settings, "llm_model", "") or ""

    return LLMSummaryOut(
        window_hours=window_hours,
        since=since,
        generated_at=now,
        provider=provider,
        model=model,
        runs=len(folds),
        turns=sum(f.turns for f in folds.values()),
        tokens_billed=sum(f.tokens_billed for f in folds.values()),
        peak_call_tokens=max((f.peak_call_tokens for f in folds.values()), default=0),
        tool_calls=sum(f.tool_calls for f in folds.values()),
        narrated_turns=sum(f.narrated_turns for f in folds.values()),
        silent_turns=sum(f.silent_turns for f in folds.values()),
        outcomes=outcomes,
        by_agent=agent_list,
        top_tools=top_tools,
        flags=_flags(settings),
        token_ceiling_enabled=bool(getattr(settings, "llm_run_token_ceiling_enabled", False)),
        token_ceiling=int(getattr(settings, "llm_run_token_ceiling", 0) or 0),
        rows_scanned=rows_scanned,
        truncated_scan=truncated_scan,
        scan_cap=SUMMARY_SCAN_CAP,
        token_metric=TOKEN_METRIC,
    )


@llm_observability_router.get("/llm/runs", response_model=LLMRunPageOut)
def list_llm_runs(
    agent: str | None = None,
    outcome: str | None = None,
    window_hours: int = Query(DEFAULT_WINDOW_HOURS, ge=1, le=MAX_WINDOW_HOURS),
    limit: int = 50,
    offset: int = 0,
):
    """Paged list of LLM runs, newest first.

    Read-only. ``limit`` is clamped to 200. ``agent`` filters in SQL;
    ``outcome`` is a DERIVED value (see the module docstring) so it can only be
    applied after folding — the page is therefore selected first and filtered
    second, and ``total`` reports the pre-``outcome`` total with
    ``outcome_filtered`` behaviour documented on the frontend. Callers that need
    exact outcome totals should read ``/llm/summary``, which classifies every
    run in the window.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    with get_session() as s:
        # Page over RUNS, not rows: one group-by pass gets the run ids and their
        # ordering key, and only that page's rows are then materialised.
        run_q = s.query(
            AgentDecisionLog.run_id,
            func.max(AgentDecisionLog.ts).label("last_ts"),
        ).filter(AgentDecisionLog.run_id.isnot(None), AgentDecisionLog.ts >= since)
        if agent:
            run_q = run_q.filter(AgentDecisionLog.agent == agent)
        run_q = run_q.group_by(AgentDecisionLog.run_id).order_by(
            func.max(AgentDecisionLog.ts).desc()
        )

        total = run_q.count()
        page_ids = [r[0] for r in run_q.offset(offset).limit(limit).all()]
        if not page_ids:
            return LLMRunPageOut(
                items=[], total=total, limit=limit, offset=offset, token_metric=TOKEN_METRIC
            )

        rows = (
            s.query(*_summary_columns())
            .filter(AgentDecisionLog.run_id.in_(page_ids))
            .order_by(AgentDecisionLog.run_id, AgentDecisionLog.iteration)
            .all()
        )

    folds = _fold_rows(rows)
    order = {str(rid): i for i, rid in enumerate(page_ids)}
    items = [f.to_run_out() for f in folds.values()]
    items.sort(key=lambda r: order.get(r.run_id, len(order)))
    if outcome:
        items = [r for r in items if r.outcome == outcome]
    return LLMRunPageOut(
        items=items, total=total, limit=limit, offset=offset, token_metric=TOKEN_METRIC
    )


@llm_observability_router.get("/llm/runs/{run_id}", response_model=LLMRunDetailOut)
def get_llm_run(run_id: str, limit: int = 500):
    """One run's iteration ladder — the trace an operator reads when something
    looks wrong.

    Read-only. ``limit`` clamps the number of steps returned (max 500); a run
    cannot legitimately exceed ``2 * max_iters + 1`` steps, so this is a
    backstop against a pathological row set, not normal paging.
    """
    limit = max(1, min(limit, 500))
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown LLM run: {run_id}") from None

    with get_session() as s:
        rows = (
            s.query(AgentDecisionLog)
            .filter(AgentDecisionLog.run_id == rid)
            .order_by(AgentDecisionLog.iteration)
            .limit(limit)
            .all()
        )
        if not rows:
            raise HTTPException(status_code=404, detail=f"Unknown LLM run: {run_id}")

        folded = _fold_rows(
            [
                (
                    r.run_id,
                    r.agent,
                    r.domain,
                    r.iteration,
                    r.ts,
                    r.token_count,
                    r.tools_chosen,
                    r.decision_summary,
                    len(r.reasoning_text or ""),
                )
                for r in rows
            ]
        )
        fold = folded[str(rid)]

        steps: list[LLMStepOut] = []
        for r in rows:
            if r.iteration < 0:
                # The marker row is an outcome fact, not a model turn — it is
                # reported via `outcome`, not as a fake iteration in the ladder.
                continue
            tools = _tool_names(r.tools_chosen)
            counts = Counter(tools)
            text = r.reasoning_text or ""
            steps.append(
                LLMStepOut(
                    iteration=r.iteration,
                    ts=r.ts,
                    call_tokens=r.token_count,
                    tools_chosen=tools,
                    tool_repeats={name: n for name, n in counts.items() if n > 1},
                    reasoning_text=text,
                    reasoning_state=_reasoning_state(len(text), tools),
                )
            )

    base = fold.to_run_out()
    return LLMRunDetailOut(
        **base.model_dump(),
        outcome_reason=_OUTCOME_REASONS[base.outcome],
        steps=steps,
        token_metric=TOKEN_METRIC,
    )
