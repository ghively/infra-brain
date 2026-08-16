import { useCallback, useEffect, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { Icon } from "../lib/icons";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { fmtDt } from "../lib/fmtDt";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  EmptyState,
  Badge,
  type ColumnDef,
} from "../components/ui";

type GeneratedScript = {
  name: string;
  language: string;
  purpose: string;
  run_count: number;
  last_returncode: number;
  created_by_agent: string;
  domain: string;
  git_path: string;
  last_run_at: string | null;
  created_at: string;
};

/** Port of the legacy Scripts page (dashboard/src/pages/scripts.dc.html),
 *  rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md) as a
 *  Phase 3 mechanical DataTable/Panel conversion.
 *  Backed by GET /api/dashboard/generated_scripts ({items,total,...}
 *  envelope, GeneratedScriptPageOut — src/infra_brain/api/routers/governance.py:309).
 *  The endpoint supports server-side `language`/`agent` filters; this port
 *  re-fetches with those as query params (rather than the legacy's fetch-all
 *  + client-side filter) so pagination-correctness holds if the script list
 *  grows past one page in the future.
 */
export function Scripts() {
  const [scripts, setScripts] = useState<GeneratedScript[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lang, setLang] = useState("(all)");
  const [agent, setAgent] = useState("(all)");
  const [langOpts, setLangOpts] = useState<string[]>(["(all)"]);
  const [agentOpts, setAgentOpts] = useState<string[]>(["(all)"]);

  // Load the unfiltered set once to derive the filter option lists.
  useEffect(() => {
    apiGet<unknown>("/api/dashboard/generated_scripts")
      .then((d) => {
        const items = rows<GeneratedScript>(d);
        setLangOpts(["(all)", ...[...new Set(items.map((s) => s.language))].sort()]);
        setAgentOpts(["(all)", ...[...new Set(items.map((s) => s.created_by_agent))].sort()]);
      })
      .catch(() => {
        /* option lists are cosmetic; filtered fetch below still runs and reports errors */
      });
  }, []);

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    if (lang !== "(all)") params.set("language", lang);
    if (agent !== "(all)") params.set("agent", agent);
    await apiGet<unknown>(`/api/dashboard/generated_scripts?${params.toString()}`)
      .then((d) => setScripts(rows<GeneratedScript>(d)))
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, [lang, agent]);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const h = headerFor("scripts");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Scripts failed to load" hint={error} />
      </div>
    );
  }

  if (scripts === null) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const runsTotal = scripts.reduce((a, s) => a + s.run_count, 0);
  // A never-run script's last_returncode is NULL backend-side, mapped to -1
  // by governance_intelligence.py — treated as "non-zero" it reads as a
  // failure before the script has ever executed. Gate on run_count so
  // never-run scripts are excluded from both the failure badge and this
  // denominator, matching the column rendering below.
  const everRun = scripts.filter((s) => s.run_count > 0);
  const successPct = everRun.length
    ? Math.round((everRun.filter((s) => s.last_returncode === 0).length / everRun.length) * 100)
    : 0;
  const stats: Array<{ label: string; value: string; color: "red" | "yellow" | "green" | "blue" | undefined }> = [
    { label: "Scripts", value: String(scripts.length), color: undefined },
    { label: "Total Runs", value: String(runsTotal), color: "blue" },
    { label: "Success Rate", value: `${successPct}%`, color: "green" },
    { label: "Languages", value: String(new Set(scripts.map((s) => s.language)).size), color: undefined },
  ];

  const empty = scripts.length === 0;
  const hasAnyFilter = lang !== "(all)" || agent !== "(all)";

  const columns: ColumnDef<GeneratedScript>[] = [
    {
      key: "name",
      header: "Name",
      typeGlyph: "pk",
      render: (s) => <span style={{ fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>{s.name}</span>,
    },
    {
      key: "language",
      header: "Language",
      typeGlyph: "enum",
      render: (s) => <Badge tone="warn">{s.language}</Badge>,
    },
    {
      key: "last_returncode",
      header: "Last Run",
      typeGlyph: "enum",
      render: (s) => {
        const neverRun = s.run_count === 0;
        const ok = s.last_returncode === 0;
        const tone = neverRun ? "neutral" : ok ? "ok" : "err";
        const label = neverRun ? "never run" : ok ? "exit 0" : `exit ${s.last_returncode}`;
        return <Badge tone={tone}>{label}</Badge>;
      },
    },
    { key: "run_count", header: "Runs", typeGlyph: "num" },
    {
      key: "last_run_at",
      header: "Last Run At",
      typeGlyph: "text",
      render: (s) => fmtDt(s.last_run_at),
    },
    { key: "purpose", header: "Purpose", typeGlyph: "text" },
    {
      key: "created_by_agent",
      header: "Written By",
      typeGlyph: "fk",
      render: (s) => <Badge tone="info">{s.created_by_agent} agent</Badge>,
    },
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

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
        <select
          value={lang}
          onChange={(e) => setLang(e.target.value)}
          style={{
            background: "var(--ib-raised)",
            border: "1px solid var(--ib-border)",
            borderRadius: "var(--ib-radius-sm)",
            padding: "6px 12px",
            color: "var(--ib-text)",
            fontSize: 13,
            outline: "none",
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          {langOpts.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
        <select
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          style={{
            background: "var(--ib-raised)",
            border: "1px solid var(--ib-border)",
            borderRadius: "var(--ib-radius-sm)",
            padding: "6px 12px",
            color: "var(--ib-text)",
            fontSize: 13,
            outline: "none",
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          {agentOpts.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      </div>

      <Panel mnemonic="SCRIPTS" description="generated_scripts — self-improvement loop output" live>
        {empty ? (
          hasAnyFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No scripts match your filters"
              hint="Adjust the filters above to see results."
              action={{
                label: "Clear filters",
                onClick: () => {
                  setLang("(all)");
                  setAgent("(all)");
                },
              }}
            />
          ) : (
            <EmptyState kind="none-yet" title="No generated scripts yet" hint="Scripts appear here once an agent writes one during the self-improvement loop." />
          )
        ) : (
          <DataTable<GeneratedScript>
            columns={columns}
            rows={scripts}
            rowKey={(s, i) => `${s.name}-${i}`}
            sortable
            initialSortKey="name"
            toolbarName="generated_scripts"
            caption="Generated scripts: name, language, last run outcome, run count, last run time, purpose, and authoring agent"
            statusBar={{ shown: scripts.length, note: "read-only · no mutations possible from this view" }}
          />
        )}
      </Panel>
    </div>
  );
}
