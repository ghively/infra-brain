import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost, rows, AuthRequired, ApiError, redirectToLogin } from "../api";
import { groupDomains } from "../domainGroups";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { PageShell, StatTile, Panel, DataTable, Button, EmptyState, Badge, type ColumnDef } from "../components/ui";
import { normalizeStatus, statusColor, statusStyle } from "../lib/tokens";

type ScanPoint = {
  domain: string;
  method: string;
  endpoint: string;
  schedule: string;
  status: string;
  next_run: string;
  last_run: string | null;
  last_success: string | null;
};

/** Status → Badge tone. `active`/`degraded` are the two statuses the legacy
 *  page actually saw, but `normalizeStatus` covers any backend string
 *  honestly (an unrecognized status renders neutral, never a guessed hue). */
function statusBadgeTone(status: string): "neutral" | "info" | "ok" | "warn" | "err" {
  const s = normalizeStatus(status);
  if (s === "ok") return "ok";
  if (s === "error") return "err";
  if (s === "warn") return "warn";
  if (s === "info") return "info";
  return "neutral";
}

function fmtDt(s: string | null | undefined): string | undefined {
  // Returning undefined (rather than a placeholder string) lets DataTable's
  // NULL contract render the dim-italic NULL token automatically instead of
  // a page-local "—" — a never-run scan point genuinely has no last-run
  // value yet, distinct from the FK-rule's "not applicable" em-dash.
  if (!s) return undefined;
  const d = String(s);
  return d.length > 19 ? `${d.slice(0, 10)} ${d.slice(11, 19)}` : d;
}

/** Port of the legacy Scan Schedule page (dashboard/src/pages/scanschedule.dc.html),
 *  rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md).
 *  Scan points come from GET /api/dashboard/scan_points ({items,total,...}
 *  envelope, ScanPointPageOut — src/infra_brain/api/routers/fleet.py:174).
 *  "Trigger Sweep" POSTs to /api/dashboard/sweeps/{domain}
 *  (fleet.py:262) which queues a read-only collection run; 202 means
 *  accepted, 409 means a collection for that domain is already in flight.
 *
 *  ACCURACY (T2 audit, preserved from the pre-rebuild page): last_run/
 *  last_success were already fetched (ScanPoint includes them, and the
 *  backend joins collection_runs specifically to provide this — see
 *  fleet.py:174/list_scans) but never rendered, so a collector silently
 *  broken for weeks still showed "active" with a fresh-looking Next Run and
 *  no staleness signal. The Last Run column below must not regress back to
 *  fetched-but-unrendered.
 *
 *  The sweep-result message box below must stay outcome-accurate — colored
 *  red for failure/409-already-in-progress, green only on a genuine 202
 *  Accepted — never hardcoded to a single color regardless of outcome. */
export function ScanSchedule() {
  const [scans, setScans] = useState<ScanPoint[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sweepDomain, setSweepDomain] = useState("");
  const [sweepMsg, setSweepMsg] = useState<string | null>(null);
  const [sweepOk, setSweepOk] = useState(true);
  const [sweeping, setSweeping] = useState(false);

  const load = useCallback(async () => {
    await apiGet<unknown>("/api/dashboard/scan_points")
      .then((d) => {
        const items = rows<ScanPoint>(d);
        setScans(items);
        setError(null);
        if (items.length > 0) setSweepDomain((prev) => prev || items[0].domain);
      })
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const h = headerFor("scan");

  if (error) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} icon="⏱️" />
        <EmptyState kind="error" title="Scan schedule failed to load" hint={error} />
      </div>
    );
  }

  if (scans === null) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} icon="⏱️" />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  // 4th tile: "Next Sweep" — the earliest next_run among all scan points.
  // Mirrors the legacy scanStats[3]: stat('Next Sweep', '17m', '#60a5fa').
  // next_run is an ISO datetime string from the API; we compute time-until.
  function fmtRelative(isoStr: string | null | undefined): string {
    if (!isoStr) return "—";
    const ms = new Date(isoStr).getTime() - Date.now();
    if (isNaN(ms)) return "—";
    if (ms <= 0) return "now";
    const totalMin = Math.ceil(ms / 60000);
    const hrs = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    return hrs > 0 ? `${hrs}h ${m}m` : `${m}m`;
  }
  const nextSweepStr = fmtRelative(
    scans.reduce<string | null>((earliest, s) => {
      if (!s.next_run) return earliest;
      return earliest === null || s.next_run < earliest ? s.next_run : earliest;
    }, null)
  );

  const activeCount = scans.filter((s) => s.status === "active").length;
  const degradedCount = scans.filter((s) => s.status === "degraded").length;

  const stats: Array<{ label: string; value: string; color: "red" | "yellow" | "green" | "blue" | undefined }> = [
    { label: "Scan Points", value: String(scans.length), color: undefined },
    { label: "Active", value: String(activeCount), color: "green" },
    { label: "Degraded", value: String(degradedCount), color: degradedCount > 0 ? "yellow" : undefined },
    { label: "Next Sweep", value: nextSweepStr, color: "blue" },
  ];

  async function triggerSweep() {
    if (!sweepDomain) return;
    setSweeping(true);
    setSweepOk(true);
    setSweepMsg(`Triggering sweep for ${sweepDomain}…`);
    try {
      // POST /api/dashboard/sweeps/{domain} returns 202 on success.
      await apiPost(`/api/dashboard/sweeps/${encodeURIComponent(sweepDomain)}`);
      setSweepOk(true);
      setSweepMsg(`Sweep triggered for ${sweepDomain} — 202 Accepted. Collection run queued.`);
    } catch (e) {
      if (e instanceof AuthRequired) {
        redirectToLogin();
      } else if (e instanceof ApiError && e.status === 409) {
        setSweepOk(false);
        setSweepMsg(`${sweepDomain} collection already in progress (409).`);
      } else if (e instanceof ApiError && e.status === 0) {
        setSweepOk(false);
        setSweepMsg("Sweep failed: could not reach the API.");
      } else {
        setSweepOk(false);
        setSweepMsg(`Sweep failed: ${String(e)}`);
      }
    } finally {
      setSweeping(false);
    }
  }

  const pills = [
    { dot: statusColor("ok"), text: `${activeCount} active` },
    { dot: statusColor("warn"), text: `${degradedCount} degraded` },
  ];

  const columns: ColumnDef<ScanPoint>[] = [
    { key: "domain", header: "Domain", typeGlyph: "pk" },
    { key: "method", header: "Method", typeGlyph: "text" },
    { key: "endpoint", header: "Endpoint", typeGlyph: "text" },
    { key: "schedule", header: "Schedule", typeGlyph: "text" },
    { key: "last_run", header: "Last Run", typeGlyph: "text", render: (s) => fmtDt(s.last_run) },
    { key: "next_run", header: "Next Run", typeGlyph: "text", render: (s) => fmtDt(s.next_run) },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (s) => <Badge tone={statusBadgeTone(s.status)}>{s.status}</Badge>,
    },
  ];

  const empty = scans.length === 0;
  const sweepBoxStyle = sweepMsg
    ? statusStyle(sweepOk ? "ok" : "error")
    : null;

  return (
    <div>
      <PageShell title={h.title} description={h.desc} icon="⏱️" pills={pills} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

      <Panel mnemonic="SCHED" description="scan_points" live>
        {empty ? (
          <EmptyState kind="none-yet" title="No scan points configured" hint="Scan points appear once a domain agent registers its collection schedule." />
        ) : (
          <DataTable<ScanPoint>
            columns={columns}
            rows={scans}
            rowKey={(s, i) => `${s.domain}-${i}`}
            sortable
            initialSortKey="domain"
            toolbarName="scan_points"
            caption="Scan schedule: domain, method, endpoint, schedule, last run, next run, and status"
            statusBar={{ shown: scans.length, note: "read-only · no mutations possible from this view" }}
          />
        )}
      </Panel>

      <div style={{ marginTop: 18 }}>
        <Panel mnemonic="SWEEP" description="Trigger an immediate read-only collection run for any domain">
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "16px" }}>
            <select
              value={sweepDomain}
              onChange={(e) => setSweepDomain(e.target.value)}
              style={{
                background: "var(--ib-raised)",
                border: "1px solid var(--ib-border)",
                borderRadius: "var(--ib-radius-sm)",
                padding: "8px 14px",
                color: "var(--ib-text)",
                fontSize: 13,
                outline: "none",
                cursor: "pointer",
                fontFamily: "inherit",
              }}
            >
              {/* MR-I usability nit: the ~23 sweep domains were a single flat
               *  list with no grouping. groupDomains() classifies them into the
               *  same Security/Inventory/Deploy/Operations/Agents categories
               *  the sidebar uses (domainGroups.ts), rendered as <optgroup>s. */}
              {groupDomains(Array.from(new Set(scans.map((s) => s.domain)))).map(([group, domainsInGroup]) => (
                <optgroup key={group} label={group}>
                  {domainsInGroup.map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <Button variant="primary" onClick={() => void triggerSweep()} loading={sweeping} disabled={!sweepDomain}>
              Trigger Sweep
            </Button>
          </div>
          {sweepMsg && sweepBoxStyle && (
            <div
              role={sweepOk ? "status" : "alert"}
              style={{
                margin: "0 16px 16px",
                padding: "10px 14px",
                borderRadius: "var(--ib-radius-sm)",
                fontSize: 12,
                fontWeight: 500,
                background: sweepBoxStyle.bg,
                color: sweepBoxStyle.fg,
                border: `1px solid ${sweepBoxStyle.border}`,
              }}
            >
              {sweepMsg}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
