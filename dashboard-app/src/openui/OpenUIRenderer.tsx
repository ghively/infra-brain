/**
 * OpenUIRenderer — React port of the bespoke OpenUI rendering bridge from the
 * legacy DC dashboard (dashboard/src/shell.dc.html lines 6044-6318 and
 * src/infra_brain/dashboard/static/openui/library.js).
 *
 * The legacy bridge was a MutationObserver watching a hidden #openui-relay div,
 * driving a ReactDOM.createRoot on #openui-root.  In the React app we don't need
 * the DOM bridge — we take `output` and `isStreaming` directly as props.
 *
 * Rendering logic (ported verbatim from mountOpenUI/parseAndRender):
 *   1. While streaming: show a "Generating…" spinner placeholder.
 *   2. After streaming: parse self-closing JSX-like tags in the output string,
 *      e.g. `<FleetStatCard title="Online hosts" value={42} color="green" />`.
 *      Render each matched tag as the corresponding React component.
 *   3. If no tags match: fall back to a styled <pre> block (raw text).
 *   4. Unknown component names render a fallback card showing the props.
 *
 * Component library (ported from static/openui/library.js, then wired to real
 * data — audit session 2026-07-27, TRK pending):
 *   - FleetStatCard — renders the title/value/color the LLM embedded directly
 *     in the tag (a point-in-time KPI number; no live fetch needed).
 *   - All 13 others — each fetches live from the endpoint documented for its
 *     tag in `src/infra_brain/openui/prompt.py` (the system prompt that tells
 *     the LLM which props each component accepts), using the same
 *     apiGet/AuthRequired/Skeleton conventions as the rest of dashboard-app
 *     (see e.g. pages/Vulns.tsx, pages/Home.tsx).
 */

import React, { useEffect, useState } from "react";

import { apiGet, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";

// ── Helpers ──────────────────────────────────────────────────────────────────

const COLOR_MAP: Record<string, string> = {
  green: "#34d399",
  red: "#f87171",
  amber: "#fbbf24",
  blue: "#60a5fa",
};

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        background: "#0b0f1c",
        border: "1px solid rgba(255,255,255,.06)",
        borderRadius: 14,
        padding: "16px 18px",
        marginBottom: 12,
        color: "#d4dcf0",
        fontFamily: "'DM Sans',-apple-system,sans-serif",
      }}
    >
      {children}
    </div>
  );
}

function CardTitle({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 11,
        color: "#8896b3",
        textTransform: "uppercase",
        letterSpacing: ".06em",
        marginBottom: 10,
        fontWeight: 600,
      }}
    >
      {children}
    </div>
  );
}

/** Loading placeholder for a data-fetching component — matches the Skeleton
 *  convention used app-wide (e.g. Sidebar.tsx, Home.tsx) instead of a bespoke
 *  spinner. */
function LoadingCard({ title }: { title: string }) {
  return (
    <Card>
      <CardTitle>{title}</CardTitle>
      <Skeleton count={3} height={12} />
    </Card>
  );
}

/** Inline error fallback. The app-wide fetch-error banners (ErrorBanners.tsx,
 *  fed automatically by apiGet's fetchErrors side-channel) already surface the
 *  underlying failing endpoint globally; this is the small in-card
 *  acknowledgement so an embedded chat widget doesn't look silently broken. */
function ErrorCard({ title, message }: { title: string; message: string }) {
  return (
    <Card>
      <CardTitle>{title}</CardTitle>
      <div style={{ fontSize: 12, color: "#f87171" }}>{`Unable to load live data — ${message}`}</div>
    </Card>
  );
}

function EmptyCard({ title, message }: { title: string; message: string }) {
  return (
    <Card>
      <CardTitle>{title}</CardTitle>
      <div style={{ fontSize: 12, color: "#8896b3" }}>{message}</div>
    </Card>
  );
}

/** Minimal dark-themed data table shared by every list/table OpenUI component. */
function MiniTable({
  columns,
  rows,
}: {
  columns: Array<{ key: string; label: string }>;
  rows: Array<Record<string, React.ReactNode>>;
}) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                style={{
                  textAlign: "left",
                  padding: "4px 8px",
                  color: "#8896b3",
                  fontWeight: 600,
                  borderBottom: "1px solid rgba(255,255,255,.08)",
                  whiteSpace: "nowrap",
                }}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td
                  key={c.key}
                  style={{
                    padding: "4px 8px",
                    borderBottom: "1px solid rgba(255,255,255,.04)",
                    whiteSpace: "nowrap",
                  }}
                >
                  {r[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Builds a query string from a props object, dropping undefined/null/"" so
 *  optional OpenUI props (per prompt.py — "omit optional props if not
 *  needed") don't get forwarded to the API as literal empty-string filters. */
function qs(params: Record<string, unknown>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

function str(v: unknown): string {
  return v === undefined || v === null ? "" : String(v);
}

function num(v: unknown, fallback: number): number {
  return typeof v === "number" && !Number.isNaN(v) ? v : fallback;
}

function fmtDate(v: string | undefined | null): string {
  if (!v) return "—";
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? v : d.toLocaleString();
}

const SEV_COLOR: Record<string, string> = {
  critical: "#f87171",
  high: "#fb923c",
  medium: "#fbbf24",
  low: "#60a5fa",
};

function sevColor(sev: string): string {
  return SEV_COLOR[sev.toLowerCase()] ?? "#8896b3";
}

function healthColor(status: string | undefined): string {
  const s = (status ?? "").toLowerCase();
  if (!s) return "#374569";
  if (/(fail|error|denied)/.test(s)) return "#f87171";
  if (/(running|pending|retry|interrupt)/.test(s)) return "#fbbf24";
  return "#34d399";
}

/** Generic data-fetch hook shared by every wired OpenUI component. Handles the
 *  loading/error/AuthRequired states the same way every other dashboard-app
 *  page does via apiGet (pages/Vulns.tsx, pages/Home.tsx): AuthRequired
 *  redirects to /login; any other failure is surfaced inline (apiGet has
 *  already noted it into the global fetchErrors side-channel for the
 *  app-wide banners). Passing `url: null` skips the fetch entirely (used to
 *  gate a second, dependent fetch behind the first one's result). */
function useOpenUIFetch<T>(url: string | null): {
  data: T | null;
  loading: boolean;
  error: string | null;
} {
  const [state, setState] = useState<{ data: T | null; loading: boolean; error: string | null }>({
    data: null,
    loading: url !== null,
    error: null,
  });

  useEffect(() => {
    if (url === null) {
      setState({ data: null, loading: false, error: null });
      return;
    }
    let cancelled = false;
    setState({ data: null, loading: true, error: null });
    apiGet<T>(url).then(
      (data) => {
        if (!cancelled) setState({ data, loading: false, error: null });
      },
      (e: unknown) => {
        if (cancelled) return;
        if (e instanceof AuthRequired) {
          redirectToLogin();
          return;
        }
        setState({ data: null, loading: false, error: e instanceof Error ? e.message : "request failed" });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [url]);

  return state;
}

/** Resolves a hostname (the prop LinuxPackageTable/LinuxServiceStatus/
 *  LinuxPortScan/WindowsPatchState are prompted with) to the resource_id the
 *  real detail endpoints are keyed by (`/api/dashboard/resources/{id}/linux`
 *  and `.../windows` — hosts.py). There is no hostname-keyed lookup route, so
 *  this fetches the resource list and matches client-side, same as the
 *  hostname→resource drill-in Hosts.tsx already does when linking into
 *  Vulns.tsx (see Vulns.tsx's `?host=` handling for the sibling pattern). */
function useHostResourceId(hostname: string): {
  resourceId: string | null;
  loading: boolean;
  error: string | null;
} {
  const { data, loading, error } = useOpenUIFetch<{ items: Array<{ id: string; hostname: string }> }>(
    hostname ? "/api/dashboard/resources?limit=1000" : null,
  );
  if (loading || error) return { resourceId: null, loading, error };
  const match = data?.items.find((r) => r.hostname.toLowerCase() === hostname.toLowerCase());
  return { resourceId: match ? match.id : null, loading: false, error: null };
}

// ── Component library (mirrors static/openui/library.js) ─────────────────────

function FleetStatCard(props: Record<string, unknown>) {
  const color = COLOR_MAP[String(props.color ?? "")] ?? "#60a5fa";
  return (
    <Card>
      <div
        style={{
          fontSize: 11,
          color: "#374569",
          textTransform: "uppercase",
          letterSpacing: ".08em",
          marginBottom: 4,
        }}
      >
        {String(props.title ?? "")}
      </div>
      <div
        style={{
          fontSize: 28,
          fontWeight: 700,
          color,
          letterSpacing: "-.5px",
          fontFamily: "'DM Mono',monospace",
        }}
      >
        {String(props.value ?? "")}
      </div>
    </Card>
  );
}

interface DriftEventRow {
  id: string;
  domain: string;
  hostname: string;
  field_name: string;
  old_value: string;
  new_value: string;
  detected_at: string;
  status: string;
}

function DriftEventTable(props: Record<string, unknown>) {
  const domain = str(props.domain);
  const status = str(props.status);
  const hours = props.hours !== undefined ? num(props.hours, 24) : undefined;
  const limit = num(props.limit, 20);
  const url = `/api/dashboard/drift_events${qs({ domain, status, hours, limit })}`;
  const { data, loading, error } = useOpenUIFetch<{ items: DriftEventRow[] }>(url);

  if (loading) return <LoadingCard title="Drift Events" />;
  if (error) return <ErrorCard title="Drift Events" message={error} />;
  const items = data?.items ?? [];
  if (items.length === 0) return <EmptyCard title="Drift Events" message="No drift events match these filters." />;
  return (
    <Card>
      <CardTitle>Drift Events</CardTitle>
      <MiniTable
        columns={[
          { key: "hostname", label: "Host" },
          { key: "domain", label: "Domain" },
          { key: "field_name", label: "Field" },
          { key: "change", label: "Change" },
          { key: "detected_at", label: "Detected" },
          { key: "status", label: "Status" },
        ]}
        rows={items.map((r) => ({
          hostname: r.hostname,
          domain: r.domain,
          field_name: r.field_name,
          change: `${r.old_value} → ${r.new_value}`,
          detected_at: fmtDate(r.detected_at),
          status: <span style={{ color: healthColor(r.status) }}>{r.status}</span>,
        }))}
      />
    </Card>
  );
}

interface RunRow {
  domain: string;
  trigger_type: string;
  status: string;
  started_at: string;
  duration_seconds: number;
}

function SweepTimeline(props: Record<string, unknown>) {
  const domain = str(props.domain);
  const days = num(props.days, 7);
  const url = `/api/dashboard/collection_runs${qs({ domain, limit: 200 })}`;
  const { data, loading, error } = useOpenUIFetch<{ items: RunRow[] }>(url);

  if (loading) return <LoadingCard title="Sweep Timeline" />;
  if (error) return <ErrorCard title="Sweep Timeline" message={error} />;
  const cutoff = Date.now() - days * 86400000;
  const items = (data?.items ?? []).filter((r) => new Date(r.started_at).getTime() >= cutoff);
  if (items.length === 0)
    return <EmptyCard title="Sweep Timeline" message={`No collection runs in the last ${days} day(s).`} />;
  return (
    <Card>
      <CardTitle>{`Sweep Timeline (last ${days}d)`}</CardTitle>
      <MiniTable
        columns={[
          { key: "domain", label: "Domain" },
          { key: "trigger_type", label: "Trigger" },
          { key: "status", label: "Status" },
          { key: "started_at", label: "Started" },
          { key: "duration_seconds", label: "Duration (s)" },
        ]}
        rows={items.map((r) => ({
          domain: r.domain,
          trigger_type: r.trigger_type,
          status: <span style={{ color: healthColor(r.status) }}>{r.status}</span>,
          started_at: fmtDate(r.started_at),
          duration_seconds: r.duration_seconds,
        }))}
      />
    </Card>
  );
}

interface ResourceRow {
  id: string;
  hostname: string;
  domain: string;
  resource_type: string;
  zone: string;
  status: string;
  last_seen_at: string;
}

function ResourceList(props: Record<string, unknown>) {
  const domain = str(props.domain);
  const resourceType = str(props.resource_type);
  const zone = str(props.zone);
  const limit = num(props.limit, 20);
  const url = `/api/dashboard/resources${qs({ domain, resource_type: resourceType, zone, limit })}`;
  const { data, loading, error } = useOpenUIFetch<{ items: ResourceRow[] }>(url);

  if (loading) return <LoadingCard title="Resources" />;
  if (error) return <ErrorCard title="Resources" message={error} />;
  const items = data?.items ?? [];
  if (items.length === 0) return <EmptyCard title="Resources" message="No resources match these filters." />;
  return (
    <Card>
      <CardTitle>Resources</CardTitle>
      <MiniTable
        columns={[
          { key: "hostname", label: "Host" },
          { key: "domain", label: "Domain" },
          { key: "resource_type", label: "Type" },
          { key: "zone", label: "Zone" },
          { key: "status", label: "Status" },
          { key: "last_seen_at", label: "Last Seen" },
        ]}
        rows={items.map((r) => ({
          hostname: r.hostname,
          domain: r.domain,
          resource_type: r.resource_type,
          zone: r.zone,
          status: <span style={{ color: healthColor(r.status === "healthy" ? "success" : r.status) }}>{r.status}</span>,
          last_seen_at: fmtDate(r.last_seen_at),
        }))}
      />
    </Card>
  );
}

interface VulnRow {
  cve: string;
  host: string;
  pkg: string;
  severity: string;
  cvss: number;
  status: string;
}

function VulnSummary(props: Record<string, unknown>) {
  const severity = str(props.severity);
  const status = str(props.status);
  const limit = num(props.limit, 20);
  const url = `/api/dashboard/vulnerabilities${qs({ severity, status, limit })}`;
  const { data, loading, error } = useOpenUIFetch<{ items: VulnRow[] }>(url);

  if (loading) return <LoadingCard title="Vulnerabilities" />;
  if (error) return <ErrorCard title="Vulnerabilities" message={error} />;
  const items = data?.items ?? [];
  if (items.length === 0) return <EmptyCard title="Vulnerabilities" message="No vulnerabilities match these filters." />;
  return (
    <Card>
      <CardTitle>Vulnerabilities</CardTitle>
      <MiniTable
        columns={[
          { key: "cve", label: "CVE" },
          { key: "host", label: "Host" },
          { key: "pkg", label: "Package" },
          { key: "severity", label: "Severity" },
          { key: "cvss", label: "CVSS" },
          { key: "status", label: "Status" },
        ]}
        rows={items.map((r) => ({
          cve: r.cve,
          host: r.host,
          pkg: r.pkg,
          severity: <span style={{ color: sevColor(r.severity) }}>{r.severity}</span>,
          cvss: r.cvss,
          status: r.status,
        }))}
      />
    </Card>
  );
}

interface ComplianceRow {
  rule: string;
  severity: string;
  host: string;
  detail: string;
  status: string;
  detected_at: string;
}

function ComplianceBoard(props: Record<string, unknown>) {
  const severity = str(props.severity);
  const status = props.status !== undefined ? str(props.status) : "open";
  const url = `/api/dashboard/compliance${qs({ status, limit: 500 })}`;
  const { data, loading, error } = useOpenUIFetch<{ items: ComplianceRow[] }>(url);

  if (loading) return <LoadingCard title="Compliance" />;
  if (error) return <ErrorCard title="Compliance" message={error} />;
  // The /compliance endpoint has no server-side severity filter (governance_ops.py
  // only accepts status/limit/offset) — filter client-side on the returned page.
  let items = data?.items ?? [];
  if (severity) items = items.filter((i) => i.severity.toLowerCase() === severity.toLowerCase());
  if (items.length === 0) return <EmptyCard title="Compliance" message="No compliance findings match these filters." />;

  const groups = new Map<string, ComplianceRow[]>();
  for (const i of items) {
    const key = i.severity || "unknown";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(i);
  }
  const order = ["critical", "high", "medium", "low"];
  const keys = Array.from(groups.keys()).sort((a, b) => {
    const ai = order.indexOf(a.toLowerCase());
    const bi = order.indexOf(b.toLowerCase());
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  return (
    <Card>
      <CardTitle>Compliance Findings</CardTitle>
      {keys.map((k) => (
        <div key={k} style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: sevColor(k), marginBottom: 4 }}>
            {`${k} (${groups.get(k)!.length})`}
          </div>
          <MiniTable
            columns={[
              { key: "rule", label: "Rule" },
              { key: "host", label: "Host" },
              { key: "detail", label: "Detail" },
              { key: "detected_at", label: "Detected" },
            ]}
            rows={groups.get(k)!.map((r) => ({
              rule: r.rule,
              host: r.host,
              detail: r.detail,
              detected_at: fmtDate(r.detected_at),
            }))}
          />
        </div>
      ))}
    </Card>
  );
}

interface ActivityRow {
  ts: string;
  agent: string;
  domain: string;
  tool: string;
  verdict: string;
  latency_ms: number;
}

function AgentActivityFeed(props: Record<string, unknown>) {
  const agent = str(props.agent);
  const verdict = str(props.verdict);
  const limit = num(props.limit, 20);
  const url = `/api/dashboard/activity${qs({ agent, verdict, limit })}`;
  const { data, loading, error } = useOpenUIFetch<{ items: ActivityRow[] }>(url);

  if (loading) return <LoadingCard title="Agent Activity" />;
  if (error) return <ErrorCard title="Agent Activity" message={error} />;
  const items = data?.items ?? [];
  if (items.length === 0) return <EmptyCard title="Agent Activity" message="No agent activity matches these filters." />;
  return (
    <Card>
      <CardTitle>Agent Activity</CardTitle>
      <MiniTable
        columns={[
          { key: "ts", label: "Time" },
          { key: "agent", label: "Agent" },
          { key: "tool", label: "Tool" },
          { key: "verdict", label: "Verdict" },
          { key: "latency_ms", label: "Latency (ms)" },
        ]}
        rows={items.map((r) => ({
          ts: fmtDate(r.ts),
          agent: r.agent,
          tool: r.tool,
          verdict: <span style={{ color: r.verdict === "deny" ? "#f87171" : "#34d399" }}>{r.verdict}</span>,
          latency_ms: Math.round(r.latency_ms),
        }))}
      />
    </Card>
  );
}

function DomainHealthGrid(props: Record<string, unknown>) {
  const domainsFilter =
    typeof props.domains === "string" && props.domains.trim()
      ? props.domains
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
      : null;
  const showDrift = props.show_drift_count !== false;

  const runs = useOpenUIFetch<{ items: RunRow[] }>("/api/dashboard/collection_runs?limit=500");
  const drift = useOpenUIFetch<{ items: { domain: string }[] }>(
    showDrift ? "/api/dashboard/drift_events?status=open&limit=1000" : null,
  );

  if (runs.loading) return <LoadingCard title="Domain Health" />;
  if (runs.error) return <ErrorCard title="Domain Health" message={runs.error} />;

  const latestByDomain = new Map<string, RunRow>();
  for (const r of runs.data?.items ?? []) {
    const cur = latestByDomain.get(r.domain);
    if (!cur || new Date(r.started_at) > new Date(cur.started_at)) latestByDomain.set(r.domain, r);
  }
  const domains = domainsFilter ?? Array.from(latestByDomain.keys()).sort();
  if (domains.length === 0) return <EmptyCard title="Domain Health" message="No collection runs recorded yet." />;

  const driftCounts = new Map<string, number>();
  for (const d of drift.data?.items ?? []) driftCounts.set(d.domain, (driftCounts.get(d.domain) ?? 0) + 1);

  return (
    <Card>
      <CardTitle>Domain Health</CardTitle>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(140px,1fr))", gap: 10 }}>
        {domains.map((d) => {
          const info = latestByDomain.get(d);
          return (
            <div
              key={d}
              style={{
                background: "rgba(255,255,255,.03)",
                border: "1px solid rgba(255,255,255,.06)",
                borderRadius: 10,
                padding: "10px 12px",
              }}
            >
              <div style={{ fontSize: 11, color: "#8896b3", textTransform: "uppercase", marginBottom: 4 }}>{d}</div>
              <div style={{ fontSize: 13, fontWeight: 600, color: healthColor(info?.status) }}>
                {info?.status ?? "no runs"}
              </div>
              {showDrift && (
                <div style={{ fontSize: 11, color: "#8896b3", marginTop: 4 }}>
                  {drift.loading ? "…" : `${driftCounts.get(d) ?? 0} open drift`}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

interface LinuxPackage {
  name: string;
  version: string;
  manager: string;
}

function LinuxPackageTable(props: Record<string, unknown>) {
  const hostname = str(props.hostname);
  const manager = str(props.manager);
  const nameFilter = str(props.name_filter);
  const host = useHostResourceId(hostname);
  const detail = useOpenUIFetch<{ packages: LinuxPackage[] }>(
    host.resourceId ? `/api/dashboard/resources/${host.resourceId}/linux` : null,
  );

  if (!hostname) return <ErrorCard title="Linux Packages" message="hostname prop is required" />;
  if (host.loading) return <LoadingCard title="Linux Packages" />;
  if (host.error) return <ErrorCard title="Linux Packages" message={host.error} />;
  if (!host.resourceId) return <EmptyCard title="Linux Packages" message={`No resource found for host "${hostname}".`} />;
  if (detail.loading) return <LoadingCard title="Linux Packages" />;
  if (detail.error) return <ErrorCard title="Linux Packages" message={detail.error} />;

  let pkgs = detail.data?.packages ?? [];
  if (manager) pkgs = pkgs.filter((p) => p.manager === manager);
  if (nameFilter) pkgs = pkgs.filter((p) => p.name.includes(nameFilter));
  if (pkgs.length === 0) return <EmptyCard title="Linux Packages" message={`No packages match on "${hostname}".`} />;

  return (
    <Card>
      <CardTitle>{`Linux Packages — ${hostname}`}</CardTitle>
      <MiniTable
        columns={[
          { key: "name", label: "Package" },
          { key: "version", label: "Version" },
          { key: "manager", label: "Manager" },
        ]}
        rows={pkgs.map((p) => ({ name: p.name, version: p.version, manager: p.manager }))}
      />
    </Card>
  );
}

interface LinuxService {
  name: string;
  state: string;
  enabled: boolean;
}

function LinuxServiceStatus(props: Record<string, unknown>) {
  const hostname = str(props.hostname);
  const state = str(props.state);
  const host = useHostResourceId(hostname);
  const detail = useOpenUIFetch<{ services: LinuxService[] }>(
    host.resourceId ? `/api/dashboard/resources/${host.resourceId}/linux` : null,
  );

  if (!hostname) return <ErrorCard title="Linux Services" message="hostname prop is required" />;
  if (host.loading) return <LoadingCard title="Linux Services" />;
  if (host.error) return <ErrorCard title="Linux Services" message={host.error} />;
  if (!host.resourceId) return <EmptyCard title="Linux Services" message={`No resource found for host "${hostname}".`} />;
  if (detail.loading) return <LoadingCard title="Linux Services" />;
  if (detail.error) return <ErrorCard title="Linux Services" message={detail.error} />;

  let svcs = detail.data?.services ?? [];
  if (state) svcs = svcs.filter((s) => s.state === state);
  if (svcs.length === 0) return <EmptyCard title="Linux Services" message={`No services match on "${hostname}".`} />;

  return (
    <Card>
      <CardTitle>{`Linux Services — ${hostname}`}</CardTitle>
      <MiniTable
        columns={[
          { key: "name", label: "Service" },
          { key: "state", label: "State" },
          { key: "enabled", label: "Enabled" },
        ]}
        rows={svcs.map((s) => ({
          name: s.name,
          state: <span style={{ color: s.state === "running" ? "#34d399" : "#f87171" }}>{s.state}</span>,
          enabled: s.enabled ? "yes" : "no",
        }))}
      />
    </Card>
  );
}

interface LinuxPort {
  port: number;
  proto: string;
  process?: string | null;
  state: string;
}

function LinuxPortScan(props: Record<string, unknown>) {
  const hostname = str(props.hostname);
  const state = str(props.state);
  const proto = str(props.proto);
  const host = useHostResourceId(hostname);
  const detail = useOpenUIFetch<{ ports: LinuxPort[] }>(
    host.resourceId ? `/api/dashboard/resources/${host.resourceId}/linux` : null,
  );

  if (!hostname) return <ErrorCard title="Linux Open Ports" message="hostname prop is required" />;
  if (host.loading) return <LoadingCard title="Linux Open Ports" />;
  if (host.error) return <ErrorCard title="Linux Open Ports" message={host.error} />;
  if (!host.resourceId) return <EmptyCard title="Linux Open Ports" message={`No resource found for host "${hostname}".`} />;
  if (detail.loading) return <LoadingCard title="Linux Open Ports" />;
  if (detail.error) return <ErrorCard title="Linux Open Ports" message={detail.error} />;

  let ports = detail.data?.ports ?? [];
  if (state) ports = ports.filter((p) => p.state === state);
  if (proto) ports = ports.filter((p) => p.proto === proto);
  if (ports.length === 0) return <EmptyCard title="Linux Open Ports" message={`No ports match on "${hostname}".`} />;

  return (
    <Card>
      <CardTitle>{`Open Ports — ${hostname}`}</CardTitle>
      <MiniTable
        columns={[
          { key: "port", label: "Port" },
          { key: "proto", label: "Proto" },
          { key: "state", label: "State" },
          { key: "process", label: "Process" },
        ]}
        rows={ports.map((p) => ({
          port: p.port,
          proto: p.proto,
          state: p.state,
          process: p.process ?? "—",
        }))}
      />
    </Card>
  );
}

function DriftTrendChart(props: Record<string, unknown>) {
  const domain = str(props.domain);
  const days = num(props.days, 30);
  const url = `/api/dashboard/drift_events/trend${qs({ domain, days })}`;
  const { data, loading, error } = useOpenUIFetch<{ points: Array<{ date: string; count: number; domain: string }> }>(
    url,
  );

  if (loading) return <LoadingCard title="Drift Trend" />;
  if (error) return <ErrorCard title="Drift Trend" message={error} />;
  const points = data?.points ?? [];
  if (points.length === 0) return <EmptyCard title="Drift Trend" message="No drift events in this window." />;

  const byDate = new Map<string, number>();
  for (const p of points) byDate.set(p.date, (byDate.get(p.date) ?? 0) + p.count);
  const entries = Array.from(byDate.entries()).sort(([a], [b]) => a.localeCompare(b));
  const max = Math.max(1, ...entries.map(([, c]) => c));

  return (
    <Card>
      <CardTitle>{`Drift Trend${domain ? ` — ${domain}` : ""} (${days}d)`}</CardTitle>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {entries.map(([date, count]) => (
          <div key={date} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11 }}>
            <div style={{ width: 76, color: "#8896b3", flexShrink: 0 }}>{date}</div>
            <div style={{ flex: 1, background: "rgba(255,255,255,.04)", borderRadius: 4, height: 10 }}>
              <div
                style={{
                  width: `${(count / max) * 100}%`,
                  height: "100%",
                  background: "#6366f1",
                  borderRadius: 4,
                }}
              />
            </div>
            <div style={{ width: 24, textAlign: "right", color: "#d4dcf0", flexShrink: 0 }}>{count}</div>
          </div>
        ))}
      </div>
    </Card>
  );
}

interface WindowsPatch {
  hostname: string;
  kb_list: string[];
  pending_count: number;
  last_patched?: string | null;
  winrm_status: string;
}

function WindowsPatchState(props: Record<string, unknown>) {
  const hostname = str(props.hostname);
  const showKb = props.show_kb_list === true;
  const host = useHostResourceId(hostname);
  const detail = useOpenUIFetch<WindowsPatch>(
    host.resourceId ? `/api/dashboard/resources/${host.resourceId}/windows` : null,
  );

  if (!hostname) return <ErrorCard title="Windows Patch State" message="hostname prop is required" />;
  if (host.loading) return <LoadingCard title="Windows Patch State" />;
  if (host.error) return <ErrorCard title="Windows Patch State" message={host.error} />;
  if (!host.resourceId)
    return <EmptyCard title="Windows Patch State" message={`No resource found for host "${hostname}".`} />;
  if (detail.loading) return <LoadingCard title="Windows Patch State" />;
  if (detail.error) return <ErrorCard title="Windows Patch State" message={detail.error} />;
  const d = detail.data;
  if (!d) return <EmptyCard title="Windows Patch State" message="No patch data available." />;

  return (
    <Card>
      <CardTitle>{`Windows Patch State — ${hostname}`}</CardTitle>
      <div style={{ fontSize: 12, display: "flex", flexDirection: "column", gap: 4 }}>
        <div>
          WinRM: <strong style={{ color: d.winrm_status === "ok" ? "#34d399" : "#f87171" }}>{d.winrm_status}</strong>
        </div>
        <div>
          Pending updates:{" "}
          <strong style={{ color: d.pending_count > 0 ? "#fbbf24" : "#34d399" }}>{d.pending_count}</strong>
        </div>
        <div>Last patched: {fmtDate(d.last_patched)}</div>
      </div>
      {showKb && d.kb_list.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ fontSize: 11, color: "#8896b3", marginBottom: 6 }}>Pending KBs</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {d.kb_list.map((kb) => (
              <span
                key={kb}
                style={{
                  fontSize: 11,
                  background: "rgba(255,255,255,.05)",
                  border: "1px solid rgba(255,255,255,.08)",
                  borderRadius: 4,
                  padding: "2px 6px",
                }}
              >
                {kb}
              </span>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

const COMPONENT_REGISTRY: Record<
  string,
  React.ComponentType<Record<string, unknown>>
> = {
  FleetStatCard,
  DriftEventTable,
  SweepTimeline,
  ResourceList,
  VulnSummary,
  ComplianceBoard,
  AgentActivityFeed,
  DomainHealthGrid,
  LinuxPackageTable,
  LinuxServiceStatus,
  LinuxPortScan,
  DriftTrendChart,
  WindowsPatchState,
};

// ── Parser (ported from mountOpenUI/parseAndRender in shell.dc.html) ─────────

function parseAndRender(output: string): React.ReactElement {
  // Attribute list may be a bare run of non-`/`/`>` chars, or a quoted string /
  // `{...}` expression that is allowed to contain a literal `/` (e.g. a prop
  // value like domain="docker/compose") — those alternatives are tried first so
  // an embedded slash never gets mistaken for the tag's self-closing `/>`.
  const TAG_RE = /<([A-Z][A-Za-z]+)((?:"[^"]*"|'[^']*'|\{[^}]*\}|[^/>])*)\/>/g;
  const PROP_RE = /([a-zA-Z_]+)=(?:"([^"]*)"|{([^}]*)})/g;

  const elements: React.ReactElement[] = [];
  let m: RegExpExecArray | null;
  let i = 0;

  while ((m = TAG_RE.exec(output)) !== null) {
    const compName = m[1];
    const propsStr = m[2];
    const props: Record<string, unknown> = {};
    let pm: RegExpExecArray | null;

    while ((pm = PROP_RE.exec(propsStr)) !== null) {
      if (pm[2] !== undefined) {
        props[pm[1]] = pm[2];
      } else {
        const raw = pm[3];
        if (raw === "true") props[pm[1]] = true;
        else if (raw === "false") props[pm[1]] = false;
        else {
          const n = Number(raw);
          props[pm[1]] = isNaN(n) ? raw : n;
        }
      }
    }

    const Comp = COMPONENT_REGISTRY[compName];
    if (Comp) {
      elements.push(<Comp key={i++} {...props} />);
    } else {
      // Unknown component — styled fallback card with prop dump
      elements.push(
        <div
          key={i++}
          style={{
            background: "#0d1117",
            border: "1px solid rgba(255,255,255,.08)",
            borderRadius: 12,
            padding: "14px 16px",
            marginBottom: 10,
            fontFamily: "'DM Mono',monospace",
          }}
        >
          <strong style={{ color: "#6366f1", fontSize: 13 }}>{compName}</strong>
          <pre
            style={{
              margin: "8px 0 0",
              fontSize: 11,
              color: "#374569",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {JSON.stringify(props, null, 2)}
          </pre>
        </div>,
      );
    }
  }

  if (elements.length === 0 && output.trim()) {
    // No components found — render as raw text
    return (
      <pre
        style={{
          fontFamily: "'DM Mono',monospace",
          fontSize: 12,
          color: "#d4dcf0",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          lineHeight: 1.6,
          background: "#0b0f1c",
          border: "1px solid rgba(255,255,255,.06)",
          borderRadius: 14,
          padding: 18,
          margin: 0,
        }}
      >
        {output}
      </pre>
    );
  }

  return <>{elements}</>;
}

// ── Public component ──────────────────────────────────────────────────────────

export interface OpenUIRendererProps {
  /** The LLM-generated output string (may contain self-closing JSX-like tags). */
  output: string;
  /** True while the stream is still arriving — shows a spinner instead of parsing. */
  isStreaming: boolean;
}

export function OpenUIRenderer({ output, isStreaming }: OpenUIRendererProps) {
  if (!output) return null;

  if (isStreaming) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          color: "#6366f1",
          fontFamily: "'DM Sans',system-ui,sans-serif",
          fontSize: 13,
          fontStyle: "italic",
          padding: "16px 0",
        }}
      >
        <span style={{ fontSize: 16 }}>&#x27F3;</span>
        <span>Generating…</span>
      </div>
    );
  }

  return parseAndRender(output);
}
