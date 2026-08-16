import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost, apiPut, apiDelete, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { PageShell, StatTile, Panel, Button, EmptyState, Badge, type PanelRail } from "../components/ui";
import { fmtDt } from "../lib/fmtDt";

// Port of dashboard/src/pages/agentconfig.dc.html + the agentConfigRows/
// agentConfigStats derivation in the legacy renderVals() (static/index.html:6237-6283).
// Backed by GET /api/dashboard/agent-config (AgentConfigPageOut envelope,
// src/infra_brain/api/routers/governance.py:941). Rebuilt on the Phase 1
// design system (see dashboard-app/DESIGN.md) as a Phase 3 mechanical
// conversion — see Security.tsx/NetDiscovery.tsx for the established pattern.
//
// Each row is one Panel rather than a DataTable row: a domain's card mixes a
// heterogeneous set of facts (status badge, ready badge, last run, an
// optional error block, and an *expandable* requirements sub-list) that don't
// share a flat column shape — DESIGN.md §8's Panel/card guidance, not the
// table default.
//
// Read-only: this page shows each domain's requirement status and a Trigger Sweep
// button. The former inline-edit form (POST /api/dashboard/agent-config) was removed
// (#83) — that write-only sink persisted to an AgentConfigSetting KV table nothing
// reads back, so config is set via .env / Bitwarden, not this page.

type Requirement = {
  key: string;
  label: string;
  configured: boolean;
  current_value: string | null;
};

type AgentConfigRow = {
  domain: string;
  last_status: string | null;
  last_error: string | null;
  last_run_at: string | null;
  requirements: Requirement[];
  ready: boolean;
  // Live `dispatchable__<domain>` override state from the backend. Optional so
  // a stale/older API response (or a test fixture predating the field) simply
  // reads as "not paused" rather than rendering `undefined`.
  paused?: boolean;
};

/** Status → Badge tone. `null` means the domain has never run yet — that's a
 *  neutral "no data" state, not a failure, and must not fall through to the
 *  red "failure" styling below (a fresh environment would otherwise show a
 *  wall of red badges implying everything is broken). It shares the same
 *  neutral styling as other non-success, non-failure statuses (e.g. "running"). */
function statusTone(s: string | null): "ok" | "err" | "neutral" {
  if (s === "success" || s === "healthy") return "ok";
  if (s === "failure") return "err";
  return "neutral";
}

export function AgentConfig() {
  const [rowsData, setRowsData] = useState<AgentConfigRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [expandedDomains, setExpandedDomains] = useState<Record<string, boolean>>({});
  // runtime_flags.dispatchable_override reads a live `dispatchable__<domain>`
  // RuntimeConfig row to skip a domain's collection (both supervisor.dispatch()
  // and the sweep graph honor it). The list endpoint now echoes that state back
  // as `paused`, so this is seeded from the server on every load/auto-refresh
  // and only updated optimistically on a toggle click — previously it was
  // click-state only, so a domain paused in an earlier session rendered as
  // "Disable Collector" (i.e. as if it were running).
  const [disabledDomains, setDisabledDomains] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    const d = await apiGet<unknown>("/api/dashboard/agent-config?limit=100");
    const next = rows<AgentConfigRow>(d);
    setRowsData(next);
    setDisabledDomains(Object.fromEntries(next.map((r) => [r.domain, !!r.paused])));
  }, []);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  function flash(msg: string) {
    setSaveMsg(msg);
    setTimeout(() => setSaveMsg(null), 3500);
  }

  function triggerSweep(domain: string) {
    apiPost(`/api/dashboard/sweeps/${encodeURIComponent(domain)}`)
      .then(() => flash(`Sweep triggered for ${domain} — queued.`))
      .catch((e) => flash(`Sweep trigger failed for ${domain}: ${String(e)}`));
  }

  function toggleDispatchable(domain: string) {
    const key = `dispatchable__${domain}`;
    if (disabledDomains[domain]) {
      apiDelete(`/api/dashboard/runtime-config/${key}`)
        .then(() => {
          setDisabledDomains((prev) => ({ ...prev, [domain]: false }));
          flash(`Collector re-enabled for ${domain}.`);
        })
        .catch((e) => flash(`Failed to re-enable collector for ${domain}: ${String(e)}`));
    } else {
      apiPut(`/api/dashboard/runtime-config/${key}`, {
        value: "false",
        value_type: "bool",
        category: "tuning",
      })
        .then(() => {
          setDisabledDomains((prev) => ({ ...prev, [domain]: true }));
          flash(`Collector disabled for ${domain}.`);
        })
        .catch((e) => flash(`Failed to disable collector for ${domain}: ${String(e)}`));
    }
  }

  function toggleExpanded(domain: string) {
    setExpandedDomains((prev) => ({ ...prev, [domain]: !prev[domain] }));
  }

  const h = headerFor("agentConfig");

  if (error) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Agent config failed to load" hint={error} />
      </div>
    );
  }

  if (rowsData === null) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const readyCount = rowsData.filter((a) => a.ready).length;
  const notReadyCount = rowsData.filter((a) => !a.ready).length;
  const lastErrorCount = rowsData.filter((a) => !!a.last_error).length;

  const stats: Array<{ label: string; value: number; color: "red" | "yellow" | "green" | "blue" | undefined }> = [
    { label: "Domains", value: rowsData.length, color: undefined },
    { label: "Ready", value: readyCount, color: "green" },
    { label: "Not Ready", value: notReadyCount, color: "red" },
    { label: "Last Errors", value: lastErrorCount, color: "yellow" },
  ];

  const empty = rowsData.length === 0;

  return (
    <div>
      <PageShell title={h.title} description={h.desc} icon="⚙️" />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

      {empty ? (
        <EmptyState
          kind="none-yet"
          title="No agent configuration loaded"
          hint="Agent configuration appears here once domains are registered in the agent registry."
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {rowsData.map((ac) => {
            const requirements = ac.requirements || [];
            const configuredCount = requirements.filter((r) => r.configured).length;
            const allConfigured = requirements.length > 0 && configuredCount === requirements.length;
            const expanded = !allConfigured || !!expandedDomains[ac.domain];
            // The "Last Errors" stat tile above counts every domain with a
            // last_error string set, regardless of `ready` — a domain can be
            // `ready` (all requirements configured) while its last actual run
            // still failed. Match this card's error-block render condition to
            // that count (no `!ac.ready &&` gate) rather than shrinking the
            // count to match the box: the error message is the more useful
            // side to keep.
            const hasError = !!ac.last_error;
            const rail: PanelRail = hasError ? "err" : ac.ready ? "ok" : "warn";

            return (
              <Panel
                key={ac.domain}
                rail={rail}
                mnemonic={ac.domain.toUpperCase()}
                description={`Last run: ${fmtDt(ac.last_run_at) ?? "—"}`}
                headerRight={
                  <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <Badge tone={statusTone(ac.last_status)}>{ac.last_status ?? "—"}</Badge>
                    <Badge tone={ac.ready ? "ok" : "err"}>{ac.ready ? "ready" : "not ready"}</Badge>
                    <Button variant="primary" size="sm" onClick={() => triggerSweep(ac.domain)}>
                      Trigger Sweep
                    </Button>
                    <Button
                      variant={disabledDomains[ac.domain] ? "secondary" : "danger"}
                      size="sm"
                      onClick={() => toggleDispatchable(ac.domain)}
                    >
                      {disabledDomains[ac.domain] ? "Enable Collector" : "Disable Collector"}
                    </Button>
                  </div>
                }
              >
                {hasError && (
                  <div
                    style={{
                      margin: "0 0 14px",
                      background: "rgba(240,101,78,.07)",
                      border: "1px solid var(--ib-red)",
                      borderRadius: 9,
                      padding: "12px 14px",
                    }}
                  >
                    <div
                      style={{
                        fontSize: 10,
                        fontWeight: 700,
                        color: "var(--ib-red)",
                        textTransform: "uppercase",
                        letterSpacing: ".07em",
                        marginBottom: 5,
                        fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                      }}
                    >
                      Last Error
                    </div>
                    <div
                      style={{
                        fontSize: 11,
                        color: "var(--ib-red)",
                        fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                        lineHeight: 1.5,
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                      }}
                    >
                      {ac.last_error}
                    </div>
                  </div>
                )}

                {allConfigured ? (
                  <button
                    onClick={() => toggleExpanded(ac.domain)}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      width: "100%",
                      background: "none",
                      border: "none",
                      padding: 0,
                      cursor: "pointer",
                      fontFamily: "inherit",
                      textAlign: "left",
                    }}
                    aria-expanded={expanded}
                  >
                    <span
                      style={{
                        fontSize: 10,
                        fontWeight: 700,
                        color: "var(--ib-muted)",
                        textTransform: "uppercase",
                        letterSpacing: ".08em",
                        fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                      }}
                    >
                      Requirements
                    </span>
                    <span
                      style={{
                        fontSize: 11,
                        color: "var(--ib-green)",
                        fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                      }}
                    >
                      {configuredCount}/{requirements.length} configured
                    </span>
                    <span
                      style={{
                        marginLeft: "auto",
                        fontSize: 10,
                        color: "var(--ib-faint)",
                        transform: expanded ? "rotate(90deg)" : "none",
                        transition: "transform .12s",
                      }}
                    >
                      ▸
                    </span>
                  </button>
                ) : (
                  <div
                    style={{
                      fontSize: 10,
                      fontWeight: 700,
                      color: "var(--ib-muted)",
                      textTransform: "uppercase",
                      letterSpacing: ".08em",
                      marginBottom: 10,
                      fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                    }}
                  >
                    Requirements
                  </div>
                )}

                {expanded && (
                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: 6,
                      marginTop: allConfigured ? 12 : 0,
                    }}
                  >
                    {requirements.map((req) => (
                      <div
                        key={req.key}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 10,
                          padding: "9px 12px",
                          background: "var(--ib-raised)",
                          borderRadius: 8,
                          border: "1px solid var(--ib-border)",
                        }}
                      >
                        <Badge tone={req.configured ? "ok" : "err"}>{req.configured ? "✓" : "✗"}</Badge>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div
                            style={{
                              fontSize: 12,
                              fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                              color: "var(--ib-text)",
                            }}
                          >
                            {req.key}
                          </div>
                          <div style={{ fontSize: 11, color: "var(--ib-muted)", marginTop: 1 }}>{req.label}</div>
                        </div>
                        <span
                          style={{
                            fontSize: 11,
                            fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                            color: req.configured ? "var(--ib-green)" : "var(--ib-red)",
                            maxWidth: 160,
                            whiteSpace: "nowrap",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            flexShrink: 0,
                          }}
                        >
                          {req.configured ? req.current_value || "(set)" : "(not set)"}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </Panel>
            );
          })}
        </div>
      )}

      {saveMsg && (
        <div
          role="status"
          style={{
            marginTop: 14,
            padding: "10px 14px",
            borderRadius: "var(--ib-radius-sm)",
            fontSize: 12,
            fontWeight: 500,
            background: "rgba(76,187,108,.08)",
            color: "var(--ib-green)",
            border: "1px solid var(--ib-green)",
          }}
        >
          {saveMsg}
        </div>
      )}
    </div>
  );
}
