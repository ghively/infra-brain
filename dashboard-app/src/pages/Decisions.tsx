import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { Icon } from "../lib/icons";
import { fmtDt } from "../lib/fmtDt";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { PageShell, Panel, Badge, EmptyState, type PanelRail } from "../components/ui";

type Decision = {
  agent: string;
  domain: string;
  run_id: string;
  iteration: number;
  decision_summary: string;
  reasoning_text: string;
  tools_chosen: string[];
  ts: string;
};

type Session = {
  run_id: string;
  agent: string;
  domain: string;
  ts: string;
  iterations: Decision[];
};

/** Port of legacy Decisions page (dashboard/src/pages/decisions.dc.html:8-53),
 *  rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md).
 *  Backed by GET /api/dashboard/decisions (governance.py:402) — {items,total,...}.
 *  Simplification/improvement (preserved from the pre-rebuild page): the legacy
 *  page fetches the whole decision log once and filters agent/run_id
 *  client-side (index.html:6187). Here the filters are passed straight to the
 *  API's existing `agent`/`run_id` query params (debounced 300ms), so
 *  filtering happens server-side instead of over a client-held copy of the
 *  full log. Session grouping (by run_id, sorted by iteration) still happens
 *  client-side, same as the legacy page.
 *
 *  Each session is heterogeneous (a header plus a variable-length ordered list
 *  of reasoning steps) rather than a set of homogeneous rows, so per
 *  DESIGN.md §8 this stays a Panel-per-session layout, not a DataTable —
 *  converting it to a flat table would destroy the iteration ordering/timeline
 *  that is the entire point of this view. */
/** T7 (rev14): plain-language explanation for an EMPTY `reasoning_text`.
 *
 *  The column is NOT NULL, so "the model narrated nothing" and "we failed to
 *  capture the narration" arrive on the wire as the same empty string. This
 *  page used to render that as a bare blank line, which reads as "nothing
 *  happened" — the single most misleading thing an audit surface can do.
 *
 *  Empty + tool calls is the common, legitimate case: an OpenAI-compatible
 *  assistant message carrying `tool_calls` has `content == ""`. The model acted
 *  without commentary. Say that, rather than leaving a hole. */
function reasoningNote(d: Decision): string {
  return d.tools_chosen.length > 0
    ? "Tool-call turn — the model called tools without writing any reasoning. Nothing was lost."
    : "No reasoning text was recorded for this step.";
}

export function Decisions() {
  // T7 (rev14): accept ?run_id=… so the LLM Observability drill-down can deep
  // link to a specific run's full transcript. Read once as the initial value;
  // typing in the filter box afterwards takes over as normal.
  const [searchParams] = useSearchParams();
  const [agent, setAgent] = useState("");
  const [runId, setRunId] = useState(() => searchParams.get("run_id") ?? "");
  const [decisions, setDecisions] = useState<Decision[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: "500" });
    if (agent) params.set("agent", agent);
    if (runId) params.set("run_id", runId);
    await apiGet<unknown>(`/api/dashboard/decisions?${params}`)
      .then((d) => setDecisions(rows<Decision>(d)))
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, [agent, runId]);

  useEffect(() => {
    const t = setTimeout(() => {
      void load();
    }, 300);
    return () => clearTimeout(t);
  }, [load]);
  useAutoRefresh(load);

  const h = headerFor("decisions");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Decisions failed to load" hint={error} />
      </div>
    );
  }
  if (decisions === null) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const byRun = new Map<string, Decision[]>();
  for (const d of decisions) {
    const list = byRun.get(d.run_id) ?? [];
    list.push(d);
    byRun.set(d.run_id, list);
  }
  const sessions: Session[] = Array.from(byRun.entries())
    .map(([rid, its]) => {
      const sorted = [...its].sort((a, b) => a.iteration - b.iteration);
      const first = sorted[0];
      return { run_id: rid, agent: first.agent, domain: first.domain, ts: first.ts, iterations: sorted };
    })
    .sort((a, b) => b.ts.localeCompare(a.ts));

  const pills = [
    { dot: "var(--ib-blue)", text: `${sessions.length} sessions` },
    { dot: "var(--ib-blue)", text: `${decisions.length} iterations` },
  ];

  const hasAnyFilter = agent !== "" || runId !== "";

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} pills={pills} />

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "10px 14px",
          background: "var(--ib-panel)",
          border: "1px solid var(--ib-border)",
          borderRadius: "var(--ib-radius)",
          marginBottom: 16,
          flexWrap: "wrap",
        }}
      >
        <input
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          placeholder="Filter by agent…"
          style={{
            background: "var(--ib-raised)",
            border: "1px solid var(--ib-border)",
            borderRadius: "var(--ib-radius-sm)",
            padding: "6px 12px",
            color: "var(--ib-text)",
            fontSize: 13,
            outline: "none",
            width: 170,
            fontFamily: "inherit",
          }}
        />
        <input
          value={runId}
          onChange={(e) => setRunId(e.target.value)}
          placeholder="Filter by run_id…"
          style={{
            background: "var(--ib-raised)",
            border: "1px solid var(--ib-border)",
            borderRadius: "var(--ib-radius-sm)",
            padding: "6px 12px",
            color: "var(--ib-text)",
            fontSize: 13,
            outline: "none",
            width: 200,
            fontFamily: "inherit",
          }}
        />
        <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--ib-muted)", fontWeight: 500 }}>
          {sessions.length} reasoning sessions
        </span>
      </div>

      {sessions.length === 0 ? (
        hasAnyFilter ? (
          <EmptyState
            kind="filter-zero"
            title="No reasoning sessions match your filters"
            hint="Clear the agent/run_id filter to see all sessions."
            action={{
              label: "Clear filters",
              onClick: () => {
                setAgent("");
                setRunId("");
              },
            }}
          />
        ) : (
          <EmptyState kind="none-yet" title="No reasoning sessions recorded yet" />
        )
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {sessions.map((sess) => {
            const rail: PanelRail = "info";
            return (
              <Panel
                key={sess.run_id}
                rail={rail}
                mnemonic={sess.agent.toUpperCase()}
                description={
                  <>
                    {sess.domain} · <span style={{ fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>{sess.run_id}</span> ·{" "}
                    {sess.iterations.length} iterations
                  </>
                }
                headerRight={
                  <span style={{ fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>{fmtDt(sess.ts)}</span>
                }
              >
                <div style={{ padding: "8px 20px 16px" }}>
                  {sess.iterations.map((it) => (
                    <div key={it.iteration} style={{ display: "flex", gap: 14, paddingTop: 14 }}>
                      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", flexShrink: 0 }}>
                        <div
                          style={{
                            width: 24,
                            height: 24,
                            borderRadius: "50%",
                            background: "var(--ib-raised)",
                            border: "1px solid var(--ib-border)",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            fontSize: 11,
                            fontWeight: 700,
                            color: "var(--ib-blue)",
                            fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                          }}
                        >
                          {it.iteration}
                        </div>
                        <div style={{ flex: 1, width: 1, background: "var(--ib-border)", marginTop: 4 }} />
                      </div>
                      <div style={{ flex: 1, minWidth: 0, paddingBottom: 4 }}>
                        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ib-text)", marginBottom: 5 }}>
                          {it.decision_summary || (
                            <span style={{ color: "var(--ib-faint)", fontWeight: 500, fontStyle: "italic" }}>
                              {it.iteration < 0 ? "Terminal marker (not a model turn)" : "No summary"}
                            </span>
                          )}
                        </div>
                        {it.reasoning_text ? (
                          <div style={{ fontSize: 12, color: "var(--ib-muted)", lineHeight: 1.6, marginBottom: 9, whiteSpace: "pre-wrap" }}>
                            {it.reasoning_text}
                          </div>
                        ) : (
                          <div style={{ fontSize: 12, color: "var(--ib-faint)", lineHeight: 1.6, marginBottom: 9, fontStyle: "italic" }}>
                            {reasoningNote(it)}
                          </div>
                        )}
                        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                          <span
                            style={{
                              fontSize: 10,
                              color: "var(--ib-faint)",
                              textTransform: "uppercase",
                              letterSpacing: ".06em",
                              fontWeight: 700,
                              fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                            }}
                          >
                            tools
                          </span>
                          {(it.tools_chosen.length ? it.tools_chosen : ["(reasoning only)"]).map((tool, i) => (
                            <Badge key={i} tone="info">
                              {tool}
                            </Badge>
                          ))}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </Panel>
            );
          })}
        </div>
      )}
    </div>
  );
}
