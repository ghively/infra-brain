import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  Button,
  EmptyState,
  SeverityPill,
  FkCell,
  GaugeCell,
  Badge,
  type ColumnDef,
} from "../components/ui";
import { normalizeStatus, statusColor, severityColor, worstSeverity } from "../lib/tokens";

/** Port of the Vulns page (legacy dashboard/src/pages/vulns.dc.html:10-1710,
 *  driven by _loadVulns()/renderVals() in
 *  src/infra_brain/dashboard/static/index.html:4137-4155,6323-6373), rebuilt on
 *  the Phase 1 design system (see dashboard-app/DESIGN.md) as Phase 2's DataTable
 *  proving ground.
 *
 *  /api/dashboard/vulnerabilities returns VulnPageOut {items,total,limit,offset}
 *  (src/infra_brain/api/routers/vuln.py:50). Filtering/pagination are server-side;
 *  this page only derives display styling client-side, same as the legacy JS.
 *
 *  MR-I: reads an initial `?host=` query param on mount (set by Hosts.tsx's
 *  row drill-in link) to pre-filter this page to one host.
 *
 *  Sort decision (this rebuild): the old page-local `useSort`/`SortTh` pairing
 *  is replaced by `DataTable`'s own built-in `sortable` prop — its comparator
 *  (`compareValues` in DataTable.tsx) is a deliberate reimplementation of
 *  `useSort`'s exact semantics (nulls last, numeric compare for numbers,
 *  natural/numeric string collation otherwise), so behavior is unchanged; it's
 *  still a page-local, current-page-only sort (no `sort`/`order` query param
 *  exists on this endpoint), just owned by the table primitive instead of a
 *  parallel hook + manual `<SortTh>` per column. `useSort`/`SortTh` are dropped
 *  from this page's imports entirely. */

type VulnOut = {
  cve: string;
  host: string;
  pkg: string;
  severity: string;
  cvss: number;
  sla: string;
  status: string;
  priority: number;
  pci_fail: boolean;
  exploits: number;
};

/** Subset of CountsOut (src/infra_brain/api/schemas.py) this page needs.
 *  `open_cves` is a server-side COUNT(DISTINCT cve_id) over rows whose
 *  status is in OPEN_VULN_STATUSES ("open"/"triage" — see
 *  src/infra_brain/db/vuln_status.py), computed in
 *  fleet.py::get_counts() — NOT capped by the vulnerabilities endpoint's
 *  page size, and already using the canonical open-status definition (not
 *  the old, wrong `status !== "resolved"` predicate). Note this counts
 *  distinct CVEs fleet-wide, same unit as the "Open CVEs" metric shown on
 *  Home.tsx, which differs from this page's table rows (one row per
 *  host+CVE pair) — that's an intentional match to the sanctioned
 *  fleet-wide metric, not a bug. `critical_cves`/`severe_cves` are the same
 *  fleet-wide distinct-CVE aggregate, filtered to severity "critical" and
 *  "severe"/"high" respectively (TRK-166 follow-up). */
type Counts = { open_cves: number; critical_cves: number; severe_cves: number };

const PAGE_SIZE = 50;

const SEV_TABS: Array<[string, string]> = [
  ["(all)", "All"],
  ["critical", "Critical"],
  ["severe", "Severe"],
  ["moderate", "Moderate"],
];

/** Composite priority score → color, with severity dominating: any
 *  critical-severity row is always red regardless of exact score (e.g. a
 *  critical CVE with a distant SLA scoring exactly 100 previously fell into
 *  the amber 70-119 band, making it look mid-urgency instead of critical).
 *  Non-critical severities keep the original score-based tiers. Ported
 *  verbatim from the pre-rebuild page (TRK-176/185) — only the color source
 *  moved from hand-picked hex to `severityColor`/token constants. */
function prioColor(prio: number, severity: string): string {
  if (String(severity).toLowerCase() === "critical" || String(severity).toLowerCase() === "severe") {
    return severityColor(String(severity).toLowerCase() === "critical" ? "critical" : "high");
  }
  if (prio >= 120) return severityColor("critical");
  if (prio >= 70) return severityColor("medium");
  if (prio >= 30) return statusColor("info");
  return statusColor("neutral");
}

export function Vulns() {
  // Read-once initial filter from the URL (e.g. Hosts.tsx's row drill-in link,
  // /vulns?host=foo). Deliberately only seeds initial state — this page does
  // not keep the URL in sync with later filter changes, matching how every
  // other filtered list page here works (component state only).
  const [searchParams] = useSearchParams();
  const initialHost = searchParams.get("host") ?? "";
  const navigate = useNavigate();

  const [items, setItems] = useState<VulnOut[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Fleet-wide "Open" count (TRK: Critical/Severe/Open tiles were computed
  // from the current ~50-row page instead of the true fleet-wide count —
  // /api/dashboard/counts is the sanctioned source for absolute metrics and
  // already exposes a correctly-scoped open_cves; see the Counts type doc
  // above). Independent of this page's filters/pagination, so it's loaded
  // once on mount and refreshed on the normal 5-min auto-refresh cycle.
  const [counts, setCounts] = useState<Counts | null>(null);

  const [sev, setSev] = useState("(all)");
  const [hostInput, setHostInput] = useState(initialHost);
  const [host, setHost] = useState(initialHost);
  const [slaOverdue, setSlaOverdue] = useState(false);
  const [hasExploit, setHasExploit] = useState(false);
  const [pciOnly, setPciOnly] = useState(false);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setOffset(0);
      setHost(hostInput);
    }, 350);
    return () => clearTimeout(debounceRef.current);
  }, [hostInput]);

  useEffect(() => {
    let cancelled = false;
    apiGet<Counts>("/api/dashboard/counts")
      .then((d) => {
        if (!cancelled) setCounts(d);
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof AuthRequired) redirectToLogin();
        // Otherwise leave counts as-is (null → tile falls back to 0) — a
        // failed counts fetch shouldn't blank out the rest of the page.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setLoading(true);
    const qs = new URLSearchParams();
    if (sev !== "(all)") qs.set("severity", sev);
    if (host) qs.set("host", host);
    if (slaOverdue) qs.set("sla_overdue", "true");
    if (hasExploit) qs.set("has_exploit", "true");
    if (pciOnly) qs.set("pci_only", "true");
    qs.set("limit", String(PAGE_SIZE));
    qs.set("offset", String(offset));

    let cancelled = false;
    apiGet<unknown>(`/api/dashboard/vulnerabilities?${qs.toString()}`)
      .then((d) => {
        if (cancelled) return;
        const envelope = d as { total?: number };
        setItems(rows<VulnOut>(d));
        setTotal(typeof envelope?.total === "number" ? envelope.total : 0);
        setError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sev, host, slaOverdue, hasExploit, pciOnly, offset]);

  // Quiet 5-min auto-refresh re-runs the current-filter fetch (no cancellation
  // needed — it fires at most once every 5 min, well outside any filter-change
  // race window). The filter-driven useEffect above owns interactive loads.
  const refresh = useCallback(async () => {
    const qs = new URLSearchParams();
    if (sev !== "(all)") qs.set("severity", sev);
    if (host) qs.set("host", host);
    if (slaOverdue) qs.set("sla_overdue", "true");
    if (hasExploit) qs.set("has_exploit", "true");
    if (pciOnly) qs.set("pci_only", "true");
    qs.set("limit", String(PAGE_SIZE));
    qs.set("offset", String(offset));
    await apiGet<unknown>(`/api/dashboard/vulnerabilities?${qs.toString()}`)
      .then((d) => {
        const envelope = d as { total?: number };
        setItems(rows<VulnOut>(d));
        setTotal(typeof envelope?.total === "number" ? envelope.total : 0);
      })
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
      });
    await apiGet<Counts>("/api/dashboard/counts")
      .then((d) => setCounts(d))
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
      });
  }, [sev, host, slaOverdue, hasExploit, pciOnly, offset]);
  useAutoRefresh(refresh);

  const hdr = headerFor("vulns");

  if (error) {
    return (
      <div>
        <PageShell title={hdr.title} description={hdr.desc} />
        <EmptyState kind="error" title="Vulnerabilities failed to load" hint={error} />
      </div>
    );
  }

  const view = items ?? [];
  // "no data ever collected" (items is still null / never fetched a page with
  // filters cleared and zero total) vs. "filters matched nothing" vs. "loaded
  // and truly empty fleet-wide" are distinguished below via EmptyState's kind,
  // per the prior UX-audit fix this rebuild must not regress.
  const hasAnyFilter = sev !== "(all)" || Boolean(host) || slaOverdue || hasExploit || pciOnly;
  const empty = view.length === 0 && !loading && items !== null;

  const stats: Array<{ label: string; value: number; color: "red" | "yellow" | "green" | "blue" | undefined }> = [
    {
      label: "Critical",
      value: counts?.critical_cves ?? 0,
      color: "red",
    },
    {
      label: "Severe",
      value: counts?.severe_cves ?? 0,
      color: "red",
    },
    { label: "Open", value: counts?.open_cves ?? 0, color: "blue" },
    { label: "Total", value: total || view.length, color: undefined },
  ];

  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + view.length, total);
  const countLabel = total === 0 ? "0 vulns" : `${pageStart}–${pageEnd} of ${total}`;
  const hasPrev = offset > 0;
  const hasNext = offset + view.length < total;

  const rail = worstSeverity(view.map((v) => v.severity));

  const columns: ColumnDef<VulnOut>[] = [
    {
      key: "priority",
      header: "Priority",
      typeGlyph: "num",
      render: (v) => <span style={{ color: prioColor(v.priority, v.severity), fontWeight: 700 }}>{v.priority}</span>,
    },
    {
      key: "cve",
      header: "CVE",
      typeGlyph: "pk",
      render: (v) => (
        <FkCell
          label={v.cve}
          onClick={() => navigate(`/r7detail?cve=${encodeURIComponent(v.cve)}`)}
          title={`Open ${v.cve} in the CVE Detail view`}
        />
      ),
    },
    {
      key: "host",
      header: "Host",
      typeGlyph: "fk",
      // Cross-page navigation to the unified Hosts view, filtered to this
      // host. Hosts.tsx only reads `?tab=`, and its hosts-list child
      // UnifiedHostsTab.tsx only reads `?q=` (a short_hostname/fqdn
      // substring filter forwarded to GET /api/dashboard/hosts) — neither
      // reads `?host=`, so a link built with that shape landed on Hosts
      // unfiltered. Matches the `?tab=hosts&q=...` shape Invrec.tsx already
      // uses for the identical host->Hosts drill-down. Uses onClick+navigate
      // (not a raw `href`) — the app mounts under `basename="/dashboard2"`
      // (App.tsx), which only `<Link>`/`navigate()` honor; a plain `<a href>`
      // hard-navigates the browser to the un-prefixed path and 404s against
      // the backend, which serves the SPA only at /dashboard2 (F-1).
      render: (v) => (
        <FkCell label={v.host} onClick={() => navigate(`/hosts?tab=hosts&q=${encodeURIComponent(v.host)}`)} />
      ),
    },
    { key: "pkg", header: "Package", typeGlyph: "text" },
    {
      key: "severity",
      header: "Severity",
      typeGlyph: "enum",
      render: (v) => <SeverityPill severity={v.severity} />,
    },
    {
      key: "cvss",
      header: "CVSS",
      typeGlyph: "num",
      render: (v) =>
        v.cvss != null ? (
          <GaugeCell value={(v.cvss / 10) * 100} severity={v.severity} display={v.cvss.toFixed(1)} label="CVSS score" />
        ) : null,
    },
    {
      key: "pci_fail",
      header: "PCI",
      typeGlyph: "enum",
      sortable: false,
      // `pci_fail` is a real boolean, not a missing value — "false" must not
      // render as the NULL token (DESIGN.md: "zero is a value, not a NULL",
      // same principle applies to false). An em-dash reads as "not
      // applicable/no PCI failure" while staying visually distinct from NULL.
      render: (v) => (v.pci_fail ? <Badge tone="err">PCI</Badge> : <span style={{ color: "var(--ib-faint)" }}>—</span>),
    },
    {
      key: "sla",
      header: "SLA",
      typeGlyph: "text",
      render: (v) => {
        const overdue = String(v.sla || "").indexOf("overdue") >= 0;
        const tone = overdue ? "err" : v.status === "resolved" ? "ok" : "neutral";
        return <span style={{ color: tone === "err" ? severityColor("critical") : tone === "ok" ? statusColor("ok") : statusColor("neutral") }}>{v.sla}</span>;
      },
    },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (v) => <Badge tone={statusBadgeTone(v.status)}>{v.status}</Badge>,
    },
  ];

  return (
    <div>
      <PageShell title={hdr.title} description={hdr.desc} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 16, flexWrap: "wrap" }}>
        <input
          value={hostInput}
          onChange={(e) => setHostInput(e.target.value)}
          placeholder="Filter by host"
          style={{
            background: "var(--ib-raised)",
            border: "1px solid var(--ib-border)",
            borderRadius: "var(--ib-radius-sm)",
            padding: "5px 11px",
            color: "var(--ib-text)",
            fontSize: 12,
            outline: "none",
            fontFamily: "inherit",
            width: 180,
          }}
        />
        {SEV_TABS.map(([vv, label]) => (
          <Button
            key={vv}
            variant={sev === vv ? "primary" : "ghost"}
            size="sm"
            onClick={() => {
              setSev(vv);
              setOffset(0);
            }}
          >
            {label}
          </Button>
        ))}
        <div style={{ width: 1, height: 16, background: "var(--ib-border)", margin: "0 4px" }} />
        <Button
          variant={slaOverdue ? "primary" : "ghost"}
          size="sm"
          onClick={() => {
            setSlaOverdue((v) => !v);
            setOffset(0);
          }}
        >
          SLA Overdue
        </Button>
        <Button
          variant={hasExploit ? "primary" : "ghost"}
          size="sm"
          onClick={() => {
            setHasExploit((v) => !v);
            setOffset(0);
          }}
        >
          Has Exploit
        </Button>
        <Button
          variant={pciOnly ? "primary" : "ghost"}
          size="sm"
          onClick={() => {
            setPciOnly((v) => !v);
            setOffset(0);
          }}
        >
          PCI Fail
        </Button>
        <div style={{ flex: 1 }} />
        <Link to="/r7detail" style={{ fontSize: 11, fontWeight: 600, color: "var(--ib-blue)", marginRight: 12 }}>
          Open CVE Detail view &rarr;
        </Link>
      </div>

      <Panel
        rail={rail === "critical" || rail === "high" ? "err" : rail === "medium" ? "warn" : rail ? "ok" : "info"}
        mnemonic="VULN"
        description="vuln_queue ⨝ rapid7_vulnerabilities"
        live
      >
        {loading && !items ? (
          <div style={{ padding: 16 }}>
            <Skeleton count={5} height={32} />
          </div>
        ) : empty ? (
          hasAnyFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No vulnerabilities match the current filters"
              hint="Try clearing the severity, host, SLA, exploit, or PCI filters."
              action={{
                label: "Clear filters",
                onClick: () => {
                  setSev("(all)");
                  setHostInput("");
                  setHost("");
                  setSlaOverdue(false);
                  setHasExploit(false);
                  setPciOnly(false);
                  setOffset(0);
                },
              }}
            />
          ) : (
            // This queue is fed by the Rapid7-backed VulnAgent (see
            // agents/vuln.py) — it requires RAPID7_URL / RAPID7_API_KEY,
            // which default empty (config.py: rapid7_url = ""). On a
            // homelab install with no Rapid7 InsightVM instance, an empty
            // queue means the collector is legitimately unconfigured, not
            // "hasn't run yet" — those read very differently to a first-time
            // operator, so distinguish them rather than defaulting to the
            // more hopeful-sounding message.
            <EmptyState
              kind="not-configured"
              title="Vulnerability scanning is not configured"
              hint="This queue is fed by the Rapid7 InsightVM collector, which needs RAPID7_URL and RAPID7_API_KEY set. If you don't run Rapid7 in this environment, this page is expected to stay empty — it's not a sign anything is broken."
            />
          )
        ) : (
          <DataTable<VulnOut>
            columns={columns}
            rows={view}
            rowKey={(v) => v.cve + v.host}
            sortable
            initialSortKey="priority"
            initialSortDir="desc"
            toolbarName="vuln_queue"
            caption="Vulnerability queue: CVE, host, package, severity, CVSS, PCI, SLA, and status"
            statusBar={{ shown: view.length, total, note: "read-only · no mutations possible from this view" }}
          />
        )}
      </Panel>

      {!empty && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 12 }}>
          {hasPrev && (
            <Button size="sm" onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>
              Prev
            </Button>
          )}
          <span style={{ fontSize: 11, color: "var(--ib-faint)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>
            {countLabel}
          </span>
          {hasNext && (
            <Button size="sm" onClick={() => setOffset((o) => o + PAGE_SIZE)}>
              Next
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/** Status → Badge tone. `open` reads informational-purple in the legacy page;
 *  the new Badge tone set has no purple, so `open` maps to `info` (blue) —
 *  the closest semantic read ("this needs attention, not yet resolved") that
 *  still fits within the four-hue system DESIGN.md mandates. */
function statusBadgeTone(status: string): "neutral" | "info" | "ok" | "warn" | "err" {
  const s = normalizeStatus(status);
  if (s === "ok") return "ok";
  if (s === "error") return "err";
  if (s === "warn") return "warn";
  if (s === "info") return "info";
  return status === "open" ? "info" : "neutral";
}
