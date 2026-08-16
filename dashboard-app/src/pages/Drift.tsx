import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { usePageData } from "../hooks/usePageData";
import { DetailDrawer } from "../components/DetailDrawer";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { PageShell } from "../components/ui/PageShell";
import { Panel } from "../components/ui/Panel";
import { DataTable } from "../components/ui/DataTable";
import type { ColumnDef } from "../components/ui/DataTable";
import { Badge } from "../components/ui/SeverityPill";
import { Button } from "../components/ui/Button";
import { EmptyState } from "../components/ui/EmptyState";
import { DriftTapeInstrument } from "../components/ui/DriftTapeInstrument";
import type { DriftTapeZone } from "../components/ui/DriftTapeInstrument";
import { TrendBarChart } from "../components/ui/TrendBarChart";
import type { TrendBarDatum } from "../components/ui/TrendBarChart";
import { fmtDt } from "../lib/fmtDt";

type DriftEvent = {
  id: string;
  domain: string;
  hostname: string;
  field_name: string;
  old_value: string;
  new_value: string;
  detected_at: string;
  status: string;
  jira_ticket: string;
  drift_type: string;
  /** Server-computed plain sentence: WHAT drifted. See DriftOut in
   *  api/schemas.py — derived at read time by drift_taxonomy.summarize_drift.
   *  Optional so a page served against an older backend build degrades to the
   *  raw field/value rendering instead of showing blank cells. */
  summary?: string;
  /** Server-computed one-liner: WHY this was flagged (which collector rule). */
  rule?: string;
  /** old/new with the writer's JSON envelope peeled off ({"v":"node_a"} →
   *  node_a), for the drawer's before/after diff. */
  old_display?: string;
  new_display?: string;
};

const PAGE_SIZE = 100;

/** Honest drift_type labelling.
 *
 *  The pre-readability badge was `drift_type === "state_drift" ? "state" :
 *  "config"` — a two-value split that does not exist in the data. Inventoried
 *  from every `DriftEvent(...)` writer in `src/` (the live table only
 *  exercises 3 of these; the rest are read off their collectors) plus the
 *  legacy type:
 *
 *    config_drift                       agents/drift.py            (963 live rows)
 *    new_listening_port                 agents/linux.py            (26 live rows)
 *    potential_secret_in_iac            agents/iac.py              (1 live row)
 *    new_windows_service                agents/windows.py
 *    service_stopped                    agents/windows.py
 *    threat_escalation                  agents/netdiscovery.py
 *    shadow_it_discovered               agents/netdiscovery.py
 *    dangerous_service_discovered       agents/netdiscovery.py
 *    suspicious_service_discovered      agents/netdiscovery.py
 *    identity_conflict[_suffix_variant|_distinct_object]
 *                                       agents/host_reconcile.py
 *    state_drift                        LEGACY — no writer; bulk-resolved by
 *                                       agents/drift.py::detect_state_drift
 *
 *  `mcp_server.py::seed_drift_event` additionally accepts an arbitrary
 *  caller-chosen drift_type, so an unknown value is genuinely reachable: it
 *  renders VERBATIM below rather than being silently relabelled "config",
 *  which is exactly the mislabelling this map replaces. */
const DRIFT_TYPE_META: Record<string, { label: string; tone: "neutral" | "info" | "ok" | "warn" | "err" }> = {
  config_drift: { label: "config", tone: "info" },
  new_listening_port: { label: "new port", tone: "warn" },
  new_windows_service: { label: "new service", tone: "info" },
  service_stopped: { label: "service stopped", tone: "warn" },
  threat_escalation: { label: "threat ↑", tone: "err" },
  shadow_it_discovered: { label: "shadow IT", tone: "err" },
  dangerous_service_discovered: { label: "dangerous svc", tone: "err" },
  suspicious_service_discovered: { label: "suspicious svc", tone: "warn" },
  potential_secret_in_iac: { label: "possible secret", tone: "err" },
  identity_conflict: { label: "identity", tone: "warn" },
  identity_conflict_suffix_variant: { label: "identity (suffix)", tone: "warn" },
  identity_conflict_distinct_object: { label: "identity (object)", tone: "warn" },
  state_drift: { label: "state (legacy)", tone: "neutral" },
};

function driftTypeMeta(driftType: string) {
  return DRIFT_TYPE_META[driftType] ?? { label: driftType || "unknown", tone: "neutral" as const };
}

/** The sentence to lead the row with.
 *
 *  `summary` is always present from a current backend. The fallback reproduces
 *  the old raw rendering rather than showing an empty primary cell — a page
 *  served from a stale bundle/backend pairing degrades, it does not blank. */
function driftSentence(d: DriftEvent): string {
  if (d.summary && d.summary.trim()) return d.summary;
  const from = d.old_value || "(none)";
  const to = d.new_value || "(none)";
  return `${d.field_name} changed from ${from} to ${to} on ${d.hostname}`;
}

// ACCURACY (T2 audit): fleet.py's CountsOut exposes a real fleet-wide
// open_drift aggregate (a plain COUNT, unaffected by this page's limit/
// offset/filters) — see src/infra_brain/api/schemas.py::CountsOut. There is
// no acknowledged/resolved equivalent, so those two pills stay sourced from
// the currently-loaded page/filter and are labeled honestly rather than
// presented as fleet-wide totals (same "(page)" convention as Compl.tsx /
// Vulns.tsx).
type Counts = { open_drift: number };

const STATUS_TABS: [string, string][] = [
  ["open", "Open"],
  ["acknowledged", "Acknowledged"],
  ["resolved", "Resolved"],
  ["(all)", "All"],
];

const WINDOW_TABS: [string, string][] = [
  ["0", "All time"],
  ["24", "24h"],
  ["48", "48h"],
  ["168", "7d"],
  ["720", "30d"],
];

const filterLabelStyle: React.CSSProperties = {
  fontSize: 9,
  fontWeight: 700,
  color: "var(--ib-faint)",
  textTransform: "uppercase",
  letterSpacing: ".08em",
  marginRight: 2,
};

/** Port of legacy Drift page (dashboard/src/pages/drift.dc.html:8-100).
 *  Backed by GET /api/dashboard/drift_events (governance_drift.py) —
 *  {items,total,...}. Status + window filtering is pushed server-side via
 *  query params (`status`, `hours`) rather than the legacy's client-side
 *  filter over a full-domain fetch. Domain list comes from
 *  /drift_events/domains (bare array).
 *
 *  Phase 2 design-foundation rebuild (dashboard-app ground-up redesign,
 *  see /home/youruser/.claude/plans/tidy-inventing-duckling.md): migrated
 *  from `PageHeader`/hand-rolled `<table>` onto the Phase 1 primitives
 *  (`PageShell`, `Panel`, `DataTable`) and features the two new headline
 *  widgets, `DriftTapeInstrument` (current fleet-wide open-drift reading)
 *  and `TrendBarChart` (drift volume over recent days) — both wired to real
 *  data, see the widget section below for the exact sourcing and the one
 *  documented gap (no second real series for the bar chart's `subset`).
 *
 *  Every filter/interaction from the pre-rebuild page is preserved: status/
 *  domain/window tabs, the telemetry-suppression toggle, server-side
 *  limit/offset pagination with Prev/Next, row-click → DetailDrawer, and the
 *  T0.2 cross-page deep-link handler.
 *
 *  TRK-191 note (verified against docs/TRACKER.md before this rebuild, since
 *  a prior task brief for this session claimed it was already fixed and
 *  live — it is NOT): TRK-191 ("Drift event queue polluted by
 *  `graph_maintenance` self-telemetry") is still listed as **NOT YET
 *  INVESTIGATED** in the tracker; there is no `include_graph_maintenance`
 *  backend param anywhere in `governance_drift.py`. The only existing
 *  telemetry filter is `suppress_telemetry` (the pre-existing "Telemetry
 *  hidden" toggle below), which excludes high-volume `vuln`-domain property
 *  fields — unrelated to `graph_maintenance` noise. This rebuild preserves
 *  that toggle exactly as it already behaved; it does not invent a
 *  graph_maintenance exclusion that doesn't exist server-side. */
export function Drift() {
  const [status, setStatus] = useState("open");
  const [domain, setDomain] = useState("(all)");
  const [windowHrs, setWindowHrs] = useState("0");
  const [suppress, setSuppress] = useState(true);
  const [offset, setOffset] = useState(0);
  const [domains, setDomains] = useState<string[]>([]);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [selected, setSelected] = useState<DriftEvent | null>(null);
  const triggerRef = useRef<HTMLElement>(null);
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    apiGet<unknown>("/api/dashboard/drift_events/domains")
      .then((d) => setDomains(rows<string>(d)))
      .catch(() => void 0);
  }, []);

  const loadCounts = useCallback(async () => {
    await apiGet<Counts>("/api/dashboard/counts")
      .then((d) => setCounts(d))
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        // Otherwise leave counts as-is — a failed counts fetch shouldn't
        // blank out the rest of the page (matches Vulns.tsx's convention).
      });
  }, []);

  useEffect(() => {
    void loadCounts();
  }, [loadCounts]);

  const fetchDrift = useCallback(async () => {
    const params = new URLSearchParams();
    if (status !== "(all)") params.set("status", status);
    if (domain !== "(all)") params.set("domain", domain);
    if (windowHrs !== "0") params.set("hours", windowHrs);
    if (suppress) params.set("suppress_telemetry", "true");
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(offset));
    return apiGet<{ items: DriftEvent[]; total: number }>(`/api/dashboard/drift_events?${params}`);
  }, [status, domain, windowHrs, suppress, offset]);

  const { data, error, loading, reload } = usePageData(fetchDrift, [status, domain, windowHrs, suppress, offset]);
  const refreshAll = useCallback(async () => {
    await reload();
    await loadCounts();
  }, [reload, loadCounts]);
  useAutoRefresh(refreshAll);

  // T0.2 cross-page deep-link (unchanged)
  useEffect(() => {
    const st = location.state as { detailOpen?: { kind: string; data: DriftEvent } } | null;
    if (st?.detailOpen?.kind === "drift" && st.detailOpen.data) {
      setSelected(st.detailOpen.data);
      navigate(location.pathname, { replace: true, state: null });
    }
  }, [location, navigate]);

  const events = data?.items ?? null;
  const total = data?.total ?? 0;

  const h = headerFor("drift");
  const ev = events ?? [];
  // ACCURACY (T2 audit, preserved): these pills used to be computed from `ev`
  // (the current page, capped and — with the default "Open" status tab —
  // also filtered), presented as if they were global totals. "open" sources
  // the real fleet-wide /api/dashboard/counts.open_drift aggregate (falling
  // back to the page count while counts is still loading); acknowledged/
  // resolved have no such aggregate, so they're labeled "(page)" honestly.
  const pills = [
    { dot: "var(--ib-blue)", text: `${counts?.open_drift ?? ev.filter((d) => d.status === "open").length} open` },
    { dot: "var(--ib-muted)", text: `${ev.filter((d) => d.status === "acknowledged").length} acknowledged (page)` },
    { dot: "var(--ib-green)", text: `${ev.filter((d) => d.status === "resolved").length} resolved (page)` },
  ];

  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + ev.length, total);
  const hasPrev = offset > 0;
  const hasNext = offset + ev.length < total;

  const columns: ColumnDef<DriftEvent>[] = [
    {
      key: "drift_type",
      header: "Type",
      typeGlyph: "enum",
      width: 130,
      render: (d) => {
        const meta = driftTypeMeta(d.drift_type);
        // `title` keeps the exact drift_type reachable on hover even when the
        // badge shows the friendlier label.
        return (
          <Badge tone={meta.tone} title={d.drift_type}>
            {meta.label}
          </Badge>
        );
      },
    },
    {
      key: "hostname",
      header: "Resource",
      typeGlyph: "fk",
      width: 180,
      render: (d) => (
        <span className="ib-fk" title={d.domain}>
          {d.hostname}
        </span>
      ),
    },
    {
      // The primary cell. Replaces the old `field_name` + `change` pair, which
      // between them rendered `listen_ports` next to two stringified JSONB
      // blobs — the exact "I don't understand what drifted" complaint. Both
      // are still reachable: `field_name` and the full before/after values are
      // in the drawer (row click).
      key: "summary",
      header: "What changed",
      typeGlyph: "text",
      accessor: (d) => driftSentence(d),
      render: (d) => (
        <span className="cell-primary" title={`${d.field_name} · ${d.drift_type}`}>
          {driftSentence(d)}
        </span>
      ),
    },
    { key: "detected_at", header: "Detected", typeGlyph: "text", width: 150, render: (d) => fmtDt(d.detected_at) },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (d) => (
        <Badge tone={d.status === "open" ? "warn" : d.status === "resolved" ? "ok" : "neutral"}>{d.status}</Badge>
      ),
    },
  ];

  return (
    <div>
      <PageShell icon="⚡" title={h.title} description={h.desc} pills={pills} />

      {error && (
        <Panel rail="err" mnemonic="ERROR" description="Could not refresh — showing last known results.">
          <div style={{ padding: "6px 2px", fontSize: 12, color: "var(--ib-red)" }}>{error}</div>
        </Panel>
      )}

      <DriftWidgets domain={domain} counts={counts} openPageCount={ev.filter((d) => d.status === "open").length} />

      <div style={{ marginBottom: 16 }}>
      <Panel
        mnemonic="DRIFT"
        description="filters"
        headerRight={
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
            <span style={filterLabelStyle}>Status</span>
            {STATUS_TABS.map(([v, l]) => (
              <Button
                key={v}
                size="sm"
                variant={status === v ? "primary" : "secondary"}
                aria-pressed={status === v}
                onClick={() => {
                  setStatus(v);
                  setOffset(0);
                }}
              >
                {l}
              </Button>
            ))}
            <div style={{ width: 1, height: 16, background: "var(--ib-border)", margin: "0 4px" }} />
            <span style={filterLabelStyle}>Domain</span>
            {["(all)", ...domains].map((d) => (
              <Button
                key={d}
                size="sm"
                variant={domain === d ? "primary" : "secondary"}
                aria-pressed={domain === d}
                onClick={() => {
                  setDomain(d);
                  setOffset(0);
                }}
              >
                {d === "(all)" ? "All" : d}
              </Button>
            ))}
            <div style={{ width: 1, height: 16, background: "var(--ib-border)", margin: "0 4px" }} />
            <span style={filterLabelStyle}>Window</span>
            {WINDOW_TABS.map(([v, l]) => (
              <Button
                key={v}
                size="sm"
                variant={windowHrs === v ? "primary" : "secondary"}
                aria-pressed={windowHrs === v}
                onClick={() => {
                  setWindowHrs(v);
                  setOffset(0);
                }}
              >
                {l}
              </Button>
            ))}
            <Button
              size="sm"
              variant={suppress ? "primary" : "secondary"}
              aria-pressed={suppress}
              onClick={() => {
                setSuppress((s) => !s);
                setOffset(0);
              }}
            >
              {suppress ? "Telemetry hidden" : "Show all"}
            </Button>
          </div>
        }
      >
        {loading ? (
          <div style={{ padding: 16 }}>
            <Skeleton count={6} height={28} />
          </div>
        ) : ev.length === 0 ? (
          <EmptyState
            kind="filter-zero"
            title="No drift events match the current filters."
            hint="Try widening the window, clearing the domain filter, or turning off telemetry suppression."
          />
        ) : (
          <DataTable<DriftEvent>
            columns={columns}
            rows={ev}
            rowKey={(d) => d.id}
            sortable
            onRowClick={(d) => setSelected(d)}
            caption="Drift events"
            statusBar={{
              shown: ev.length,
              total,
              note: `${pageStart}–${pageEnd} of ${total} · read-only`,
            }}
            toolbarRight={
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                {hasPrev && (
                  <Button size="sm" variant="secondary" onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>
                    Prev
                  </Button>
                )}
                {hasNext && (
                  <Button size="sm" variant="secondary" onClick={() => setOffset((o) => o + PAGE_SIZE)}>
                    Next
                  </Button>
                )}
              </div>
            }
          />
        )}
      </Panel>
      </div>

      {/* Detail drawer — slide-in panel on row click */}
      <DetailDrawer open={selected !== null} onClose={() => setSelected(null)} title="Drift event detail" triggerRef={triggerRef}>
        {selected && (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {/* WHAT drifted — the same sentence as the row's primary cell, so
                opening the drawer never loses the reading you clicked on. */}
            <div style={{ fontSize: 14, lineHeight: 1.45, color: "var(--ib-text)" }}>
              {driftSentence(selected)}
            </div>

            {/* WHY it's flagged — the other half of the complaint. Sourced from
                drift_taxonomy.DRIFT_RULE_EXPLANATIONS via DriftOut.rule; the
                block is omitted entirely rather than showing an empty heading
                when an older backend doesn't supply it. */}
            {selected.rule && (
              <div
                style={{
                  borderLeft: "2px solid var(--ib-border)",
                  paddingLeft: 10,
                }}
              >
                <DrawerLabel>Why is this flagged?</DrawerLabel>
                <div style={{ fontSize: 12, lineHeight: 1.5, color: "var(--ib-muted)" }}>
                  {selected.rule}
                </div>
              </div>
            )}

            <BeforeAfter event={selected} />

            {(
              [
                ["Field", selected.field_name],
                ["Resource", selected.hostname],
                ["Domain", selected.domain],
                ["Drift type", selected.drift_type],
                ["Status", selected.status],
                ["Detected", fmtDt(selected.detected_at) ?? "—"],
                ["Jira", selected.jira_ticket || "—"],
                ["ID", selected.id],
              ] as [string, string][]
            ).map(([label, value]) => (
              <div key={label}>
                <DrawerLabel>{label}</DrawerLabel>
                <div className="ib-mono cell-primary" style={{ wordBreak: "break-all" }}>
                  {value}
                </div>
              </div>
            ))}
          </div>
        )}
      </DetailDrawer>
    </div>
  );
}

/** The drawer's small uppercase field caption — was repeated inline eight
 *  times as an identical style object. */
function DrawerLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 10,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: ".08em",
        color: "var(--ib-faint)",
        marginBottom: 4,
      }}
    >
      {children}
    </div>
  );
}

/** Before/after diff for one drift event.
 *
 *  Replaces the pre-readability drawer's `{old_value} → {new_value}` on one
 *  line, which for the dominant live shape rendered as
 *  `{'v': 'vmi-example-deploy'} → {'v': 'node_a'}` — two stringified JSONB envelopes
 *  side by side.
 *
 *  Two changes make it a diff rather than a dump:
 *   1. It reads `old_display`/`new_display` (server-side, envelope peeled —
 *      see drift_taxonomy.render_drift_value), falling back to the raw
 *      `old_value`/`new_value` only when an older backend omits them.
 *   2. Before and after are stacked and separately labelled with +/− gutters,
 *      so a long value wraps and stays comparable instead of being squeezed
 *      onto one nowrap line.
 *
 *  Many real drift types have only one side: `new_listening_port`,
 *  `shadow_it_discovered`, `potential_secret_in_iac` and every other
 *  "something appeared" type write `old_value=None`. Presenting those as
 *  "(empty) → value" invites the reading that a value was erased, so they are
 *  labelled as an addition (or removal) explicitly. */
function BeforeAfter({ event }: { event: DriftEvent }) {
  const before = event.old_display ?? event.old_value ?? "";
  const after = event.new_display ?? event.new_value ?? "";
  const hasBefore = before.trim() !== "";
  const hasAfter = after.trim() !== "";

  let caption = "Before → after";
  if (!hasBefore && hasAfter) caption = "Added — no previous value";
  else if (hasBefore && !hasAfter) caption = "Removed — no current value";

  const row = (sign: string, label: string, value: string, tone: string) => (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-start", padding: "4px 0" }}>
      <span aria-hidden="true" style={{ color: tone, fontWeight: 700, width: 10, flexShrink: 0 }}>
        {sign}
      </span>
      <span style={{ width: 46, flexShrink: 0, fontSize: 10, color: "var(--ib-faint)", paddingTop: 2 }}>
        {label}
      </span>
      <span className="ib-mono" style={{ color: tone, wordBreak: "break-word", whiteSpace: "pre-wrap" }}>
        {value}
      </span>
    </div>
  );

  return (
    <div>
      <DrawerLabel>{caption}</DrawerLabel>
      {!hasBefore && !hasAfter ? (
        <div style={{ fontSize: 12, color: "var(--ib-faint)" }}>
          This event recorded no before/after values.
        </div>
      ) : (
        <div
          style={{
            border: "1px solid var(--ib-border)",
            borderRadius: 3,
            padding: "6px 8px",
          }}
        >
          {hasBefore && row("−", "before", before, "var(--ib-muted)")}
          {hasAfter && row("+", "after", after, "var(--ib-text)")}
        </div>
      )}
    </div>
  );
}

type DriftTrendPointT = { date: string; count: number; domain: string };
type DriftTrendResponse = { points: DriftTrendPointT[]; total: number; domain_filter: string; days: number };

const TREND_DAYS = 14;

/** The two headline widgets, wired to real data:
 *
 *  - `DriftTapeInstrument` reads the current fleet-wide open-drift count
 *    (`/api/dashboard/counts.open_drift`, the same real aggregate the pills
 *    above use — falls back to the current page's open count while `counts`
 *    is still loading). The zone thresholds (red/yellow/green bands) are a
 *    **documented judgment call**: there is no backend-configured SLA/target
 *    for "how much open drift is acceptable" anywhere in the schema, so the
 *    bands are scaled relative to the reading's own recent history (0–33%
 *    green, 33–66% yellow, 66–100%+ red of a headroom ceiling set to 1.5×
 *    the peak of the last `TREND_DAYS` days, or 1.5× today's reading if it's
 *    itself the peak) rather than invented absolute numbers presented as if
 *    they were real limits. `history` comes from the same per-day series as
 *    the trend chart below, summed across domains for the selected filter,
 *    and anchors the ceiling. **It is NOT passed to `DriftTapeInstrument` as
 *    a trend vector**: that series is a *flow* metric (drift events
 *    *detected* that day, including ones later resolved/acknowledged),
 *    while `currentValue` is a *stock* metric (`open_drift`, a live snapshot
 *    of events still `status="open"` right now) — splicing `currentValue`
 *    onto the end of the daily-detection series to derive a rising/falling
 *    trend word compares two different quantities and can label drift as
 *    rising/falling based on nothing meaningful. There is no backend series
 *    of open-drift-count-over-time to legitimately drive a trend line here,
 *    so — same policy as `subset` below — this stays real-omitted rather
 *    than fabricating a comparison.
 *
 *  - `TrendBarChart` reads `GET /api/dashboard/drift_events/trend` (existing
 *    route, `governance_drift.py::get_drift_trend` — real, not fabricated),
 *    grouped server-side by (date, domain); this component sums per date the
 *    same way the previous inline bar chart did. **Documented gap:** the
 *    `subset` (highlighted) series is always 0 here. The trend endpoint only
 *    returns an undifferentiated per-day count (`date`, `domain`, `count`) —
 *    there is no second real dimension (e.g. by `drift_type` or severity) at
 *    day granularity to legitimately highlight. Per this task's instruction
 *    not to fabricate widget data, `subset` stays real-zero rather than
 *    inventing a split; the bar chart still shows genuine daily volume via
 *    `total`. A future backend change to `get_drift_trend` (grouping by
 *    `drift_type` too) would be the correct way to light up `subset` — out
 *    of scope for this frontend-only rebuild.
 *
 *  Like the it replaced, this is a non-fatal sidecar: a fetch failure here
 *  renders `EmptyState` for just this widget row, never the whole page. */
function DriftWidgets({
  domain,
  counts,
  openPageCount,
}: {
  domain: string;
  counts: Counts | null;
  openPageCount: number;
}) {
  const [data, setData] = useState<DriftTrendResponse | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    setData(null);
    setFailed(false);
    const params = new URLSearchParams({ days: String(TREND_DAYS) });
    if (domain !== "(all)") params.set("domain", domain);
    apiGet<DriftTrendResponse>(`/api/dashboard/drift_events/trend?${params}`)
      .then((d) => {
        if (!live) return;
        setData({
          points: Array.isArray(d?.points) ? d.points : [],
          total: d?.total ?? 0,
          domain_filter: d?.domain_filter ?? "",
          days: d?.days ?? TREND_DAYS,
        });
      })
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setFailed(true);
      });
    return () => {
      live = false;
    };
  }, [domain]);

  const byDate = useMemo(() => {
    const m = new Map<string, number>();
    for (const p of data?.points ?? []) m.set(p.date, (m.get(p.date) || 0) + p.count);
    return m;
  }, [data]);

  const dates = useMemo(() => [...byDate.keys()].sort(), [byDate]);
  const history = useMemo(() => dates.map((d) => byDate.get(d) ?? 0), [dates, byDate]);

  const bars: TrendBarDatum[] = dates.map((d) => ({
    label: d.slice(5), // MM-DD — the full ISO date is redundant column-width noise
    total: byDate.get(d) ?? 0,
    subset: 0, // see docstring above — no legitimate second series exists yet
  }));

  const currentValue = counts?.open_drift ?? openPageCount;
  // BUGFIX (audit finding, dashboard-app-src-pages-Drift-tsx): `ceiling` used
  // to be `Math.max(currentValue * 1.5, 10)` — purely a multiple of
  // `currentValue` itself. That makes `currentValue / ceiling` a CONSTANT
  // (1 / 1.5 ≈ 0.667) for every reading once `currentValue * 1.5 > 10`
  // (i.e. currentValue ≳ 6.67), which is just past the 0.66 red-zone
  // threshold below — so the gauge rendered "critical" for nearly any
  // nontrivial open-drift count and could never show "warning" again,
  // regardless of how that count compares to its own recent history.
  // Anchoring the ceiling to the recent trend (`history`, the same per-day
  // series feeding the sparkline) instead of to `currentValue` alone
  // restores the graduated 0–33/33–66/66–100% scheme documented above:
  // today's reading now lands red only when it's near/above its own recent
  // peak, not merely because it's a moderately large number.
  const historyPeak = history.length > 0 ? Math.max(...history, currentValue) : currentValue;
  const ceiling = Math.max(historyPeak * 1.5, 10);
  const zones: DriftTapeZone[] = [
    { color: "green", from: 0, to: ceiling * 0.33 },
    { color: "yellow", from: ceiling * 0.33, to: ceiling * 0.66 },
    { color: "red", from: ceiling * 0.66, to: ceiling },
  ];

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(220px, 320px) 1fr",
        gap: 16,
        marginBottom: 16,
      }}
    >
      <Panel
        mnemonic="DRIFT"
        description="open events — fleet-wide"
        rail={currentValue === 0 ? "ok" : currentValue > ceiling * 0.66 ? "err" : "warn"}
        live
      >
        {counts === null ? (
          <div style={{ padding: 16 }}>
            <Skeleton height={236} />
          </div>
        ) : currentValue === 0 ? (
          // TRK-238 item 3: with zero open events, `ceiling` degenerates to
          // its floor (max(0 * 1.5, 10) = 10) and the tape instrument still
          // rendered — a narrow, mostly-empty gauge strip with tick labels
          // packed into a near-zero-signal scale, reading as a rendering
          // glitch rather than "there is genuinely nothing open right now".
          // A real EmptyState communicates that unambiguously instead.
          <EmptyState kind="clean" title="No open drift events" hint="Fleet-wide — nothing is currently drifting. This is the healthy state." />
        ) : (
          <div style={{ display: "flex", justifyContent: "center", padding: "12px 0" }}>
            <DriftTapeInstrument
              label="Open drift events"
              value={currentValue}
              min={0}
              max={ceiling}
              zones={zones}
              // No `history` — see the docstring above (flow vs. stock
              // mismatch): the daily-detected-events series and the
              // currently-open snapshot aren't comparable, so no trend
              // vector is fabricated here. `history` (the same series) is
              // still used above to anchor `ceiling`, just not passed
              // through as a trend vector.
              unit=""
            />
          </div>
        )}
      </Panel>

      <Panel mnemonic="TREND" description={`drift volume — last ${TREND_DAYS} days${domain !== "(all)" ? ` · ${domain}` : ""}`}>
        {failed ? (
          <EmptyState kind="error" title="Drift trend failed to load" hint="The table above is unaffected." />
        ) : data === null ? (
          <div style={{ padding: 16 }}>
            <Skeleton height={120} />
          </div>
        ) : bars.length === 0 ? (
          <EmptyState kind="filter-zero" title="No drift events in this window." />
        ) : (
          <div style={{ padding: "12px 4px" }}>
            <TrendBarChart
              data={bars}
              totalColor="var(--ib-border)"
              subsetColor="var(--ib-blue)"
              legend={{ totalLabel: "Drift events", subsetLabel: "no secondary breakdown available" }}
              height={120}
            />
            <div style={{ marginTop: 8, fontSize: 10, color: "var(--ib-faint)" }}>{data.total} events total</div>
          </div>
        )}
      </Panel>
    </div>
  );
}
