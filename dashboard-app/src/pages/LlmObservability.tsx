import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { fmtDt } from "../lib/fmtDt";
import {
  Badge,
  DataTable,
  EmptyState,
  PageShell,
  Panel,
  SummaryBlock,
  type ColumnDef,
} from "../components/ui";

/** LLM Observability (T7, rev14).
 *
 *  WHY A NEW PAGE AND NOT AN EXTENSION OF /decisions
 *  -------------------------------------------------
 *  `Decisions.tsx` is a *transcript reader*: it fetches up to 500 raw
 *  `agent_decision_log` rows, groups them by run_id client-side, and renders
 *  each run as a panel of reasoning steps. It has no time window, no cost
 *  figure, no terminal-outcome concept, and no cross-run aggregate — and it
 *  cannot grow one without changing what it is, because its aggregation
 *  substrate is "whatever 500 rows the API happened to return", which is
 *  window-blind and silently partial.
 *
 *  This page answers a different question at a different grain: over a chosen
 *  window, is the LLM being used, by whom, what did it cost, and is anything
 *  looping or failing. Its numbers come from server-side aggregates
 *  (`/api/dashboard/llm/summary`, `/llm/runs`) that classify every run in the
 *  window, not from a client-held slice.
 *
 *  The two are cross-linked rather than merged: the run drill-down here shows
 *  the iteration ladder with the audit facts Decisions lacks (per-call tokens,
 *  tool repeats within a turn, terminal outcome) and links out to
 *  `/decisions?run_id=…` for the full untruncated transcript of the same run.
 *
 *  HONESTY RULES ENCODED HERE (this is an audit surface)
 *  -----------------------------------------------------
 *  1. A step with no reasoning renders an explicit statement of WHICH kind of
 *     absence it is (`reasoning_state` from the API), never a blank line that
 *     reads as "nothing happened".
 *  2. Every token figure carries its unit label. `tokens_billed` is the SUM of
 *     per-call totals; because the whole conversation is re-sent each turn it
 *     is larger than the count of distinct tokens in the transcript, and that
 *     is stated on the page, not just in a comment.
 *  3. Default-off feature flags are listed with the effect of being off, so an
 *     empty section is explained rather than ambiguous.
 *  4. A run that hit the recursion limit is red and labelled, never visually
 *     interchangeable with one that completed.
 */

type Flag = { name: string; enabled: boolean; effect: string };
type ToolUse = { tool: string; calls: number; max_in_one_iteration: number };

type AgentStats = {
  agent: string;
  domain: string;
  runs: number;
  turns: number;
  tokens_billed: number;
  peak_call_tokens: number;
  tool_calls: number;
  narrated_turns: number;
  silent_turns: number;
  completed: number;
  recursion_limit: number;
  truncated: number;
  last_run_at: string | null;
};

type Summary = {
  window_hours: number;
  since: string;
  generated_at: string;
  provider: string;
  model: string;
  runs: number;
  turns: number;
  tokens_billed: number;
  peak_call_tokens: number;
  tool_calls: number;
  narrated_turns: number;
  silent_turns: number;
  outcomes: { completed: number; recursion_limit: number; truncated: number; unknown: number };
  by_agent: AgentStats[];
  top_tools: ToolUse[];
  flags: Flag[];
  token_ceiling_enabled: boolean;
  token_ceiling: number;
  rows_scanned: number;
  truncated_scan: boolean;
  scan_cap: number;
  token_metric: string;
};

type Run = {
  run_id: string;
  agent: string;
  domain: string;
  started_at: string;
  ended_at: string;
  turns: number;
  tokens_billed: number;
  peak_call_tokens: number;
  tool_calls: number;
  distinct_tools: number;
  max_tool_repeat: number;
  narrated_turns: number;
  silent_turns: number;
  outcome: string;
};

type RunPage = { items: Run[]; total: number; limit: number; offset: number };

type Step = {
  iteration: number;
  ts: string;
  call_tokens: number | null;
  tools_chosen: string[];
  tool_repeats: Record<string, number>;
  reasoning_text: string;
  reasoning_state: string;
};

type RunDetail = Run & { outcome_reason: string; steps: Step[]; token_metric: string };

const WINDOWS: [number, string][] = [
  [24, "24h"],
  [168, "7d"],
  [720, "30d"],
  [2160, "90d"],
];

/** Terminal outcome -> visual tone. `recursion_limit` and `truncated` are
 *  deliberately NOT the same hue as `completed`: a run that ran out of
 *  reasoning steps produced no answer, and must be impossible to mistake for
 *  one that did. */
function outcomeTone(outcome: string): "ok" | "warn" | "err" | "neutral" {
  switch (outcome) {
    case "completed":
      return "ok";
    case "recursion_limit":
      return "err";
    case "truncated":
      return "warn";
    default:
      return "neutral";
  }
}

const OUTCOME_LABEL: Record<string, string> = {
  completed: "completed",
  recursion_limit: "recursion limit",
  truncated: "cut short",
  unknown: "unknown",
};

/** Plain-language explanation of an empty reasoning cell. Never render "" —
 *  a blank implies the model did nothing, which is false in both cases. */
const REASONING_ABSENCE: Record<string, string> = {
  absent_tool_call_turn:
    "Tool-call turn — the model called tools without narrating. Nothing was lost; it wrote no prose this turn.",
  absent_no_narration: "No narration recorded for this turn.",
};

function fmtInt(n: number): string {
  return n.toLocaleString();
}

function Reasoning({ step }: { step: Step }) {
  if (step.reasoning_state === "present") {
    return (
      <div style={{ fontSize: 12, color: "var(--ib-muted)", lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
        {step.reasoning_text}
      </div>
    );
  }
  return (
    <div style={{ fontSize: 12, color: "var(--ib-faint)", lineHeight: 1.6, fontStyle: "italic" }}>
      {REASONING_ABSENCE[step.reasoning_state] ?? "No narration recorded for this turn."}
    </div>
  );
}

function FlagPanel({ flags, ceilingEnabled, ceiling }: { flags: Flag[]; ceilingEnabled: boolean; ceiling: number }) {
  const off = flags.filter((f) => !f.enabled);
  return (
    <Panel
      mnemonic="FLAGS"
      description="why a section here may legitimately be empty"
    >
      <div style={{ padding: "10px 2px 4px", display: "flex", flexDirection: "column", gap: 8 }}>
        {flags.map((f) => (
          <div key={f.name} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
            <Badge tone={f.enabled ? "ok" : "neutral"}>{f.enabled ? "on" : "off"}</Badge>
            <div style={{ minWidth: 0 }}>
              <div className="ib-mono" style={{ fontSize: 12, color: "var(--ib-text)" }}>
                {f.name}
              </div>
              <div style={{ fontSize: 11, color: "var(--ib-muted)" }}>{f.effect}</div>
            </div>
          </div>
        ))}
        <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
          <Badge tone={ceilingEnabled ? "ok" : "neutral"}>{ceilingEnabled ? "on" : "off"}</Badge>
          <div>
            <div className="ib-mono" style={{ fontSize: 12, color: "var(--ib-text)" }}>
              llm_run_token_ceiling_enabled
            </div>
            <div style={{ fontSize: 11, color: "var(--ib-muted)" }}>
              {ceilingEnabled
                ? `On: a run stops making further model calls once it has billed ${fmtInt(ceiling)} tokens.`
                : `Off: nothing stops a run from spending. The configured ceiling (${fmtInt(ceiling)} tokens) is not enforced.`}
            </div>
          </div>
        </div>
        {off.length === flags.length ? (
          <div style={{ fontSize: 11, color: "var(--ib-faint)", marginTop: 4 }}>
            All optional LLM features are off — every run counted on this page comes from the
            always-on reasoning collectors, not from the flag-gated reasoner tier.
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

export function LlmObservability() {
  const [windowHours, setWindowHours] = useState(168);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [runs, setRuns] = useState<RunPage | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([
        apiGet<Summary>(`/api/dashboard/llm/summary?window_hours=${windowHours}`),
        apiGet<RunPage>(`/api/dashboard/llm/runs?window_hours=${windowHours}&limit=50`),
      ]);
      setSummary(s);
      setRuns(r);
      setError(null);
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    }
  }, [windowHours]);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    setDetail(null);
    setDetailError(null);
    apiGet<RunDetail>(`/api/dashboard/llm/runs/${selected}`)
      .then(setDetail)
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setDetailError(String(e));
      });
  }, [selected]);

  const loopSuspects = useMemo(
    () => (runs?.items ?? []).filter((r) => r.max_tool_repeat >= 3).length,
    [runs],
  );

  if (error && summary === null) {
    return (
      <div>
        <PageShell
          icon="🧠"
          title="LLM Observability"
          description="What the language model actually did: which agents used it, what it cost in tokens, whether anything looped or failed, and what it decided step by step."
        />
        <EmptyState kind="error" title="LLM observability failed to load" hint={error} />
      </div>
    );
  }
  if (summary === null || runs === null) return <Skeleton count={4} height={36} />;

  const failed = summary.outcomes.recursion_limit + summary.outcomes.truncated;
  const pills = [
    { dot: "var(--ib-blue)", text: `${fmtInt(summary.runs)} LLM runs` },
    { dot: failed ? "var(--ib-red)" : "var(--ib-green)", text: `${fmtInt(failed)} without an answer` },
  ];

  const runColumns: ColumnDef<Run>[] = [
    {
      key: "run_id",
      header: "Run",
      typeGlyph: "pk",
      render: (r) => (
        <span
          className="ib-mono"
          title={r.run_id}
          style={{
            color: r.run_id === selected ? "var(--ib-blue)" : "var(--ib-text)",
            fontWeight: r.run_id === selected ? 600 : 400,
          }}
        >
          {r.run_id === selected ? "▸ " : ""}
          {r.run_id.slice(0, 8)}
        </span>
      ),
    },
    { key: "agent", header: "Agent", typeGlyph: "text" },
    {
      key: "started_at",
      header: "Started",
      typeGlyph: "text",
      render: (r) => <span className="ib-mono">{fmtDt(r.started_at)}</span>,
    },
    {
      key: "outcome",
      header: "Outcome",
      typeGlyph: "enum",
      render: (r) => <Badge tone={outcomeTone(r.outcome)}>{OUTCOME_LABEL[r.outcome] ?? r.outcome}</Badge>,
    },
    { key: "turns", header: "Turns", typeGlyph: "num" },
    {
      key: "tokens_billed",
      header: "Tokens billed",
      typeGlyph: "num",
      render: (r) => <span className="ib-mono">{fmtInt(r.tokens_billed)}</span>,
    },
    {
      key: "tool_calls",
      header: "Tool calls",
      typeGlyph: "num",
      sortable: false,
      render: (r) => (
        <span>
          {r.tool_calls}
          {r.max_tool_repeat >= 3 ? (
            <>
              {" "}
              <Badge tone="warn" title="One tool was called this many times inside a single turn — a possible loop.">
                ×{r.max_tool_repeat} in one turn
              </Badge>
            </>
          ) : null}
        </span>
      ),
    },
    {
      key: "silent_turns",
      header: "Narrated",
      typeGlyph: "num",
      sortable: false,
      render: (r) => (
        <span style={{ fontSize: 12, color: "var(--ib-muted)" }}>
          {r.narrated_turns}/{r.turns}
        </span>
      ),
    },
  ];

  const agentColumns: ColumnDef<AgentStats>[] = [
    { key: "agent", header: "Agent", typeGlyph: "text" },
    { key: "domain", header: "Domain", typeGlyph: "text" },
    { key: "runs", header: "Runs", typeGlyph: "num" },
    { key: "turns", header: "Model calls", typeGlyph: "num" },
    {
      key: "tokens_billed",
      header: "Tokens billed",
      typeGlyph: "num",
      render: (a) => <span className="ib-mono">{fmtInt(a.tokens_billed)}</span>,
    },
    {
      key: "peak_call_tokens",
      header: "Largest call",
      typeGlyph: "num",
      render: (a) => <span className="ib-mono">{fmtInt(a.peak_call_tokens)}</span>,
    },
    {
      key: "recursion_limit",
      header: "Outcomes",
      typeGlyph: "enum",
      sortable: false,
      render: (a) => (
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {a.completed > 0 ? <Badge tone="ok">done {a.completed}</Badge> : null}
          {a.recursion_limit > 0 ? <Badge tone="err">rec-limit {a.recursion_limit}</Badge> : null}
          {a.truncated > 0 ? <Badge tone="warn">cut short {a.truncated}</Badge> : null}
          {a.completed + a.recursion_limit + a.truncated === 0 ? (
            <span style={{ fontSize: 12, color: "var(--ib-faint)" }}>—</span>
          ) : null}
        </div>
      ),
    },
    {
      key: "last_run_at",
      header: "Last run",
      typeGlyph: "text",
      render: (a) => <span className="ib-mono">{a.last_run_at ? fmtDt(a.last_run_at) : "—"}</span>,
    },
  ];

  return (
    <div>
      <PageShell
        icon="🧠"
        title="LLM Observability"
        description="What the language model actually did: which agents used it, what it cost in tokens, whether anything looped or failed, and what it decided step by step."
        pills={pills}
        actions={
          <div style={{ display: "flex", gap: 6 }}>
            {WINDOWS.map(([h, label]) => (
              <button
                key={h}
                type="button"
                onClick={() => {
                  setWindowHours(h);
                  setSelected(null);
                }}
                style={{
                  background: h === windowHours ? "var(--ib-blue)" : "var(--ib-raised)",
                  color: h === windowHours ? "#fff" : "var(--ib-text)",
                  border: "1px solid var(--ib-border)",
                  borderRadius: "var(--ib-radius-sm)",
                  padding: "4px 10px",
                  fontSize: 12,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                {label}
              </button>
            ))}
          </div>
        }
      />

      {error ? (
        <Panel rail="err" mnemonic="STALE" description="Could not refresh — showing the last successful load.">
          <div style={{ padding: "6px 2px", fontSize: 12, color: "var(--ib-red)" }}>{error}</div>
        </Panel>
      ) : null}

      <div style={{ marginBottom: 16 }}>
        <Panel
          mnemonic="USAGE"
          description={`last ${summary.window_hours}h · provider ${summary.provider || "unset"} · model ${summary.model || "unset"}`}
        >
          <div style={{ padding: "14px 2px 2px" }}>
            <SummaryBlock
              hero={{
                label: "LLM Runs",
                value: fmtInt(summary.runs),
                sub: `${fmtInt(summary.turns)} model calls across ${summary.by_agent.length} agent${summary.by_agent.length === 1 ? "" : "s"}`,
              }}
              stats={[
                {
                  label: "Tokens Billed",
                  value: fmtInt(summary.tokens_billed),
                  color: "blue",
                  sub: "sum of per-call totals",
                },
                {
                  label: "Largest Single Call",
                  value: fmtInt(summary.peak_call_tokens),
                  sub: "context-window pressure",
                },
                {
                  label: "Hit Recursion Limit",
                  value: fmtInt(summary.outcomes.recursion_limit),
                  color: summary.outcomes.recursion_limit ? "red" : undefined,
                  sub: "ran out of steps, no answer",
                },
                {
                  label: "Cut Short",
                  value: fmtInt(summary.outcomes.truncated),
                  color: summary.outcomes.truncated ? "yellow" : undefined,
                  sub: "deadline or error mid-loop",
                },
              ]}
            />
          </div>
          <div
            style={{
              padding: "12px 2px 2px",
              fontSize: 11,
              color: "var(--ib-muted)",
              lineHeight: 1.6,
              borderTop: "1px solid var(--ib-border)",
              marginTop: 12,
            }}
          >
            <strong style={{ color: "var(--ib-text)" }}>What these numbers measure.</strong>{" "}
            {summary.token_metric} A “run” is one reasoning loop
            (<span className="ib-mono">LLMAgent.reason()</span>), not one collection sweep — a sweep may
            contain several runs or none. {fmtInt(summary.narrated_turns)} of {fmtInt(summary.turns)}{" "}
            model calls wrote reasoning prose; the other {fmtInt(summary.silent_turns)} called tools
            without narrating, which is the model’s own behaviour, not missing data.
            {summary.truncated_scan ? (
              <>
                {" "}
                <span style={{ color: "var(--ib-yellow)" }}>
                  This window holds more than {fmtInt(summary.scan_cap)} decision rows; the figures above
                  cover only the most recent {fmtInt(summary.rows_scanned)}.
                </span>
              </>
            ) : null}
          </div>
        </Panel>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 14, marginBottom: 16 }}>
        <Panel mnemonic="AGENTS" description="who is using the model, and what it cost them">
          {summary.by_agent.length === 0 ? (
            <EmptyState
              kind="none-yet"
              title="No agent used the LLM in this window"
              hint="Widen the window, or check the feature flags panel — several LLM features are off by default."
            />
          ) : (
            <DataTable<AgentStats>
              columns={agentColumns}
              rows={summary.by_agent}
              rowKey={(a) => `${a.agent}:${a.domain}`}
              caption="LLM usage by agent"
            />
          )}
        </Panel>
        <FlagPanel
          flags={summary.flags}
          ceilingEnabled={summary.token_ceiling_enabled}
          ceiling={summary.token_ceiling}
        />
      </div>

      <div style={{ marginBottom: 16 }}>
        <Panel
          mnemonic="TOOLS"
          description="how often each tool was called — repeats inside one turn are the looping signal"
        >
          {summary.top_tools.length === 0 ? (
            <EmptyState kind="none-yet" title="No tool calls recorded in this window." />
          ) : (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, padding: "12px 2px 4px" }}>
              {summary.top_tools.map((t) => (
                <div
                  key={t.tool}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "6px 10px",
                    borderRadius: "var(--ib-radius-sm)",
                    background: "var(--ib-raised)",
                    border: "1px solid var(--ib-border)",
                  }}
                >
                  <span className="ib-mono" style={{ fontSize: 12, color: "var(--ib-text)" }}>
                    {t.tool}
                  </span>
                  <Badge tone="info">{fmtInt(t.calls)} calls</Badge>
                  {t.max_in_one_iteration >= 3 ? (
                    <Badge tone="warn" title="Called this many times inside a single model turn.">
                      ×{t.max_in_one_iteration} in one turn
                    </Badge>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>

      <div style={{ marginBottom: 18 }}>
        <Panel
          mnemonic="RUNS"
          description={
            loopSuspects > 0
              ? `click a run to read its step-by-step trace · ${loopSuspects} run${loopSuspects === 1 ? "" : "s"} repeated one tool 3+ times in a single turn`
              : "click a run to read its step-by-step trace"
          }
        >
          {runs.items.length === 0 ? (
            <EmptyState
              kind="none-yet"
              title="No LLM runs in this window"
              hint="Nothing called the language model. That is the whole story — not a loading failure."
            />
          ) : (
            <DataTable<Run>
              columns={runColumns}
              rows={runs.items}
              rowKey={(r) => r.run_id}
              onRowClick={(r) => setSelected(r.run_id)}
              caption="LLM runs"
              statusBar={{ shown: runs.items.length, total: runs.total }}
            />
          )}
        </Panel>
      </div>

      {selected ? (
        <div>
          {detailError ? (
            <EmptyState kind="error" title="Run detail failed to load" hint={detailError} />
          ) : detail === null ? (
            <Skeleton count={3} height={60} />
          ) : (
            <Panel
              rail={outcomeTone(detail.outcome) === "ok" ? "ok" : outcomeTone(detail.outcome) === "err" ? "err" : "warn"}
              mnemonic={detail.agent.toUpperCase()}
              description={
                <>
                  {detail.domain} ·{" "}
                  <span className="ib-mono">{detail.run_id}</span> · {detail.turns} model call
                  {detail.turns === 1 ? "" : "s"} · {fmtInt(detail.tokens_billed)} tokens billed
                </>
              }
              headerRight={<Badge tone={outcomeTone(detail.outcome)}>{OUTCOME_LABEL[detail.outcome] ?? detail.outcome}</Badge>}
            >
              <div style={{ padding: "12px 2px 4px" }}>
                <div
                  style={{
                    fontSize: 12,
                    color: "var(--ib-muted)",
                    lineHeight: 1.6,
                    padding: "8px 10px",
                    borderRadius: "var(--ib-radius-sm)",
                    background: "var(--ib-raised)",
                    border: "1px solid var(--ib-border)",
                    marginBottom: 14,
                  }}
                >
                  <strong style={{ color: "var(--ib-text)" }}>How this run ended:</strong>{" "}
                  {detail.outcome_reason}
                  <div style={{ marginTop: 6 }}>
                    <Link to={`/decisions?run_id=${detail.run_id}`} style={{ color: "var(--ib-blue)" }}>
                      Open the full transcript on Agent Decisions →
                    </Link>
                  </div>
                </div>

                {detail.steps.length === 0 ? (
                  <EmptyState
                    kind="none-yet"
                    title="No model turns were recorded for this run"
                    hint="Only a terminal marker row exists — the loop was cut off before any turn was persisted."
                  />
                ) : (
                  detail.steps.map((step) => (
                    <div key={step.iteration} style={{ display: "flex", gap: 14, paddingTop: 14 }}>
                      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", flexShrink: 0 }}>
                        <div
                          className="ib-mono"
                          style={{
                            width: 26,
                            height: 26,
                            borderRadius: "50%",
                            background: "var(--ib-raised)",
                            border: "1px solid var(--ib-border)",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            fontSize: 11,
                            fontWeight: 700,
                            color: "var(--ib-blue)",
                          }}
                        >
                          {step.iteration}
                        </div>
                        <div style={{ flex: 1, width: 1, background: "var(--ib-border)", marginTop: 4 }} />
                      </div>
                      <div style={{ flex: 1, minWidth: 0, paddingBottom: 6 }}>
                        <div
                          style={{
                            display: "flex",
                            gap: 8,
                            alignItems: "center",
                            flexWrap: "wrap",
                            marginBottom: 6,
                          }}
                        >
                          <span className="ib-mono" style={{ fontSize: 11, color: "var(--ib-faint)" }}>
                            {fmtDt(step.ts)}
                          </span>
                          <Badge tone="info">
                            {step.call_tokens === null ? "tokens not reported" : `${fmtInt(step.call_tokens)} tokens this call`}
                          </Badge>
                          {Object.entries(step.tool_repeats).map(([tool, n]) => (
                            <Badge key={tool} tone={n >= 3 ? "warn" : "neutral"}>
                              {tool} ×{n}
                            </Badge>
                          ))}
                        </div>
                        <Reasoning step={step} />
                        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", marginTop: 8 }}>
                          <span
                            className="ib-mono"
                            style={{
                              fontSize: 10,
                              color: "var(--ib-faint)",
                              textTransform: "uppercase",
                              letterSpacing: ".06em",
                              fontWeight: 700,
                            }}
                          >
                            tools
                          </span>
                          {step.tools_chosen.length === 0 ? (
                            <span style={{ fontSize: 11, color: "var(--ib-faint)" }}>
                              none — this turn only produced text
                            </span>
                          ) : (
                            step.tools_chosen.map((tool, i) => (
                              <Badge key={`${tool}-${i}`} tone="info">
                                {tool}
                              </Badge>
                            ))
                          )}
                        </div>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </Panel>
          )}
        </div>
      ) : null}
    </div>
  );
}
