/** "Hosts" tab of the consolidated Hosts page — the unified host-identity view.
 *
 *  Backed by GET /api/dashboard/hosts (src/infra_brain/api/routers/hosts.py),
 *  which returns HostsPageOut {items,total,limit,offset}: one row per *reconciled
 *  host identity*, stitched across Rapid7, vSphere, Octopus and the Linux/Windows
 *  collectors. This is the canonical grain — see `Hosts.tsx`'s header for how it
 *  differs from the Resources (per-collector rows) and Fleet (Rapid7-only assets)
 *  tabs that live beside it.
 *
 *  Row click opens the host detail drawer: editable purpose/VLAN/subnet, PCI
 *  posture (certs / security / firewall / shares / local admins), Linux/Windows
 *  OS internals, and the per-host CVE drilldown with its "View full page →"
 *  deep-link to /vulns?host=…. All four sections are ports of the pre-redesign
 *  drawer sections, rebuilt on the Phase 1 primitives.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, AuthRequired, apiGet, apiPut, redirectToLogin, rows } from "../../api";
import type { HostPurposeEntry, HostPurposeWriteResult } from "../../api";
import { DetailDrawer } from "../../components/DetailDrawer";
import { Skeleton } from "../../components/Skeleton";
import { Button } from "../../components/ui/Button";
import { DataTable } from "../../components/ui/DataTable";
import type { ColumnDef } from "../../components/ui/DataTable";
import { EmptyState } from "../../components/ui/EmptyState";
import { Panel } from "../../components/ui/Panel";
import { Badge, SeverityPill } from "../../components/ui/SeverityPill";
import { DomainCell, GaugeCell } from "../../components/ui/cells";
import { Tabs } from "../../components/ui/Tabs";
import { useAutoRefresh, useRefreshTick } from "../../hooks/useAutoRefresh";
import { riskBandFor } from "../../lib/riskBand";
import { BORDER, FAINT, FONT_MONO, MUTED, RAISED, TEXT, normalizeSeverity } from "../../lib/tokens";
import type { Severity } from "../../lib/tokens";
import { ChipFilter, DrawerNote, FilterInput, KvGrid, Pager, fmtBool, qs, relTime } from "./shared";

export type Host = {
  // TRK-348: observed=false means NO collector currently sees this machine
  // (every identity leg is NULL). It must never render as an ordinary host.
  observed?: boolean;
  retired_at?: string | null;
  short_hostname: string;
  fqdn: string;
  ip_addresses: string[];
  os_family: string | null;
  risk_score: number | null;
  vuln_count: number | null;
  patch_status: string | null;
  vsphere_power_state: string | null;
  octopus_machine_status: string | null;
  r7_resource_id: string | null;
  vsphere_resource_id: string | null;
  octopus_resource_id: string | null;
  linux_resource_id: string | null;
  windows_resource_id: string | null;
  last_reconciled: string | null;
};

type HostsPage = { items: Host[]; total: number; limit: number; offset: number };

/** Per-host CVE row returned by GET /api/dashboard/hosts/{hostname}/vulns. */
type HostVuln = {
  cve_id: string | null;
  severity: string | null;
  cvss_v3: number | null;
  sla: string | null;
  kb_id: string | null;
  solution_summary: string | null;
};

type HostVulnHeader = {
  risk_score: number | null;
  vuln_critical: number | null;
  vuln_severe: number | null;
  vuln_moderate: number | null;
};

type HostVulnsResponse = { header: HostVulnHeader | null; items: HostVuln[]; total: number };

type HostCertificateRow = {
  store: string;
  subject: string | null;
  issuer: string | null;
  thumbprint: string;
  not_before: string | null;
  not_after: string | null;
  days_until_expiry: number | null;
  is_expired: boolean;
};
type HostSecurityPostureRow = {
  firewall_enabled: boolean | null;
  firewall_service: string | null;
  av_enabled: boolean | null;
  av_product: string | null;
  av_signature_date: string | null;
  rdp_enabled: boolean | null;
  uac_enabled: boolean | null;
  ssh_password_auth: boolean | null;
  ssh_permit_root_login: boolean | null;
  ssh_pubkey_auth: boolean | null;
  selinux_mode: string | null;
  apparmor_status: string | null;
};
type HostFirewallRuleRow = {
  table_name: string | null;
  chain: string | null;
  rule_text: string;
  action: string | null;
  source: string;
};
type HostShareRow = { share_type: string; name: string; path: string | null; permissions: unknown[] };
type WindowsLocalUserRow = {
  username: string;
  enabled: boolean | null;
  is_admin: boolean;
  last_logon: string | null;
  password_required: boolean | null;
  password_never_expires: boolean | null;
};
type WindowsLocalGroupMemberRow = { group_name: string; member_name: string };

/** GET /api/dashboard/resources/{resource_id}/posture — schemas.py HostPostureOut. */
type HostPosture = {
  resource_id: string;
  hostname: string;
  certificates: HostCertificateRow[];
  security_posture: HostSecurityPostureRow | null;
  firewall_rules: HostFirewallRuleRow[];
  shares: HostShareRow[];
  local_users: WindowsLocalUserRow[];
  local_group_members: WindowsLocalGroupMemberRow[];
};

type LinuxPackage = { name: string; version: string; manager: string; installed_at: string | null };
type LinuxServiceRow = { name: string; state: string; enabled: boolean; last_checked: string };
type LinuxUserRow = { username: string; shell: string; sudo: boolean; last_login: string | null };
type LinuxPortRow = { port: number; proto: string; process: string | null; state: string };
type LinuxCronRow = { owner: string; schedule: string; command: string };
type LinuxMountRow = {
  mount: string;
  device: string | null;
  fstype: string | null;
  size_total_gb: number | null;
  size_available_gb: number | null;
};
type LinuxNicRow = { name: string; mac: string | null; ipv4: string | null; ipv6: string | null; speed_mbps: number | null };
type LinuxPendingUpdateRow = {
  package: string;
  current_version: string | null;
  available_version: string | null;
  security: boolean;
  manager: string | null;
};

/** GET /api/dashboard/resources/{resource_id}/linux — schemas.py LinuxDetailOut. */
type LinuxDetail = {
  resource_id: string;
  hostname: string;
  distro: string | null;
  kernel: string | null;
  arch: string | null;
  packages: LinuxPackage[];
  services: LinuxServiceRow[];
  users: LinuxUserRow[];
  ports: LinuxPortRow[];
  crons: LinuxCronRow[];
  mounts: LinuxMountRow[];
  nics: LinuxNicRow[];
  pending_updates: LinuxPendingUpdateRow[];
};

/** GET /api/dashboard/resources/{resource_id}/windows — schemas.py WindowsPatchOut. */
type WindowsPatch = {
  hostname: string;
  kb_list: string[];
  pending_count: number;
  last_patched: string | null;
  winrm_status: string;
};

const PAGE_SIZE = 100;

const OS_OPTS = [
  { value: "(all)", label: "All OS" },
  { value: "linux", label: "Linux" },
  { value: "windows", label: "Windows" },
];

const PATCH_OPTS = [
  { value: "(all)", label: "All patch" },
  { value: "compliant", label: "Compliant" },
  { value: "pending", label: "Pending" },
  { value: "overdue", label: "Overdue" },
];

/** Rapid7 names its severity bands Critical / Severe / Moderate, which are not
 *  the four design-system levels. Map them onto the contract rather than letting
 *  `normalizeSeverity` fail open to LOW styling on "Severe" (DESIGN.md §3). */
export function r7Severity(raw: string | null | undefined): Severity | null {
  const k = (raw ?? "").trim().toLowerCase();
  if (k === "severe") return "high";
  return normalizeSeverity(raw);
}

/** patch_status → badge tone. The backend emits compliant/pending/overdue;
 *  anything else is genuinely unknown and renders neutral, never green. */
function patchTone(status: string): "ok" | "warn" | "err" | "neutral" {
  const s = status.toLowerCase();
  if (s === "compliant") return "ok";
  if (s === "pending") return "warn";
  if (s === "overdue") return "err";
  return "neutral";
}

function SourceDot({ present, title }: { present: boolean; title: string }) {
  return <span title={title} aria-label={title} className={`source-dot ${present ? "present" : "absent"}`} />;
}

export function UnifiedHostsTab() {
  const navigate = useNavigate();
  // `?q=` seeds the hostname search, so a cross-tab link from the Resources tab
  // ("view this resource's unified host") lands here already filtered.
  const [searchParams] = useSearchParams();
  const urlQuery = searchParams.get("q") ?? "";
  const [page, setPage] = useState<HostsPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState(urlQuery);
  const [os, setOs] = useState("(all)");
  const [patch, setPatch] = useState("(all)");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Host | null>(null);
  const triggerRef = useRef<HTMLElement>(null);
  useRefreshTick(); // re-render the relative "Xm ago" stamps each minute

  const load = useCallback(async () => {
    await apiGet<unknown>(
      "/api/dashboard/hosts" +
        qs({
          q: query,
          os_family: os === "(all)" ? "" : os,
          patch_status: patch === "(all)" ? "" : patch,
          limit: PAGE_SIZE,
          offset,
        }),
    )
      .then((d) => {
        const items = rows<Host>(d);
        const envelope = d as HostsPage;
        setPage({
          items,
          total: envelope?.total ?? items.length,
          limit: envelope?.limit ?? PAGE_SIZE,
          offset: envelope?.offset ?? offset,
        });
      })
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, [query, os, patch, offset]);

  // Follow the URL when it changes under us (a fresh cross-tab link, or
  // back/forward), without fighting the user's own typing.
  useEffect(() => {
    setQuery(urlQuery);
    setOffset(0);
  }, [urlQuery]);

  useEffect(() => {
    const t = setTimeout(() => {
      void load();
    }, 300);
    return () => clearTimeout(t);
  }, [load]);
  useAutoRefresh(load);

  if (error) {
    return (
      <EmptyState
        kind="error"
        title="Hosts failed to load"
        hint={error}
        action={{ label: "Retry", onClick: () => { setError(null); void load(); } }}
      />
    );
  }
  if (page === null) return <Skeleton count={6} height={26} />;

  const items = page.items;
  // Distinguishes "genuinely zero hosts exist" from "current filter matches zero".
  const filtersActive = query.trim() !== "" || os !== "(all)" || patch !== "(all)";
  // Risk bars are scaled to the largest score on the current page, so the
  // column stays readable whatever absolute range Rapid7 returns. The numeric
  // score beside each bar is the real, unscaled value.
  const maxRisk = Math.max(1, ...items.map((h) => h.risk_score || 0));
  // "with vulns" is page-scoped: hosts.py's list endpoint exposes no global
  // vuln aggregate, and presenting a page-scoped count beside a global total as
  // if they shared scope undercounted by up to 60% past 100 hosts (2026-07-24
  // audit finding). The label says so explicitly.
  const withVulns = items.filter((x) => (x.vuln_count || 0) > 0).length;

  const columns: ColumnDef<Host>[] = [
    {
      key: "short_hostname",
      header: "Hostname",
      typeGlyph: "pk",
      render: (h) =>
        h.short_hostname || h.fqdn ? (
          <span>
            <span
              style={{
                color: h.observed === false ? MUTED : TEXT,
                fontWeight: 500,
                textDecoration: h.observed === false ? "line-through" : undefined,
              }}
            >
              {h.short_hostname || h.fqdn}
            </span>
            {h.observed === false && (
              <span
                title={
                  h.retired_at
                    ? `No collector currently observes this host (retired ${relTime(h.retired_at)})`
                    : "No collector currently observes this host"
                }
                style={{
                  marginLeft: 8,
                  fontSize: 10,
                  letterSpacing: "0.06em",
                  textTransform: "uppercase" as const,
                  color: "#b3592e",
                  border: "1px solid #b3592e55",
                  borderRadius: 3,
                  padding: "1px 6px",
                }}
              >
                unobserved{h.retired_at ? ` · ${relTime(h.retired_at)}` : ""}
              </span>
            )}
            {h.fqdn && h.fqdn !== h.short_hostname && (
              <span style={{ color: FAINT, fontSize: 10.5, marginLeft: 6 }}>{h.fqdn}</span>
            )}
          </span>
        ) : null,
    },
    {
      key: "ip_addresses",
      header: "IP addresses",
      typeGlyph: "text",
      accessor: (h) => (h.ip_addresses || []).join(", "),
      render: (h) => (h.ip_addresses || []).join(", ") || null,
    },
    {
      key: "os_family",
      header: "OS family",
      typeGlyph: "enum",
      render: (h) => (h.os_family ? <DomainCell name={h.os_family} /> : null),
    },
    {
      key: "risk_score",
      header: "Risk",
      typeGlyph: "num",
      render: (h) =>
        h.risk_score === null || h.risk_score === undefined ? null : (
          <GaugeCell
            value={((h.risk_score || 0) / maxRisk) * 100}
            severity={riskBandFor(h.risk_score)}
            display={Math.round(h.risk_score)}
            label={`Rapid7 risk score for ${h.short_hostname || h.fqdn}`}
          />
        ),
    },
    { key: "vuln_count", header: "Vulns", typeGlyph: "num", render: (h) => h.vuln_count ?? 0 },
    {
      key: "vsphere_power_state",
      header: "vSphere",
      typeGlyph: "enum",
      render: (h) =>
        h.vsphere_power_state ? (
          <Badge tone={h.vsphere_power_state === "poweredOn" ? "ok" : h.vsphere_power_state === "poweredOff" ? "err" : "neutral"}>
            {h.vsphere_power_state.replace("powered", "")}
          </Badge>
        ) : null,
    },
    {
      key: "octopus_machine_status",
      header: "Octopus",
      typeGlyph: "enum",
      render: (h) =>
        h.octopus_machine_status ? (
          <Badge tone={h.octopus_machine_status === "Online" ? "ok" : "warn"}>{h.octopus_machine_status}</Badge>
        ) : null,
    },
    {
      key: "patch_status",
      header: "Patch",
      typeGlyph: "enum",
      render: (h) => (h.patch_status ? <Badge tone={patchTone(h.patch_status)}>{h.patch_status}</Badge> : null),
    },
    {
      key: "sources",
      header: "Sources",
      typeGlyph: "text",
      sortable: false,
      accessor: () => null,
      render: (h) => (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
          <SourceDot present={!!h.r7_resource_id} title="Rapid7" />
          <SourceDot present={!!h.vsphere_resource_id} title="vSphere" />
          <SourceDot present={!!h.octopus_resource_id} title="Octopus" />
          <SourceDot present={!!(h.linux_resource_id || h.windows_resource_id)} title="Linux/Windows" />
        </span>
      ),
    },
    {
      key: "last_reconciled",
      header: "Last reconciled",
      typeGlyph: "text",
      render: (h) => (h.last_reconciled ? relTime(h.last_reconciled) : null),
    },
  ];

  const chips = [
    `${page.total} hosts`,
    `${withVulns} with vulns (this page)`,
    ...(page.total > items.length ? [`sort + risk scale: this page of ${items.length} only`] : []),
  ];

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12, flexWrap: "wrap" }}>
        <FilterInput
          label="Search hostname or FQDN"
          placeholder="Search hostname / fqdn…"
          value={query}
          onChange={(v) => {
            setQuery(v);
            setOffset(0);
          }}
        />
        <ChipFilter
          label="Filter by OS family"
          options={OS_OPTS}
          active={os}
          onChange={(v) => {
            setOs(v);
            setOffset(0);
          }}
        />
        <ChipFilter
          label="Filter by patch status"
          options={PATCH_OPTS}
          active={patch}
          onChange={(v) => {
            setPatch(v);
            setOffset(0);
          }}
        />
      </div>

      <Panel
        rail="info"
        mnemonic="HOSTS"
        description="unified host identity ⨝ rapid7 · vsphere · octopus · linux/windows"
        live
      >
        {items.length === 0 ? (
          filtersActive ? (
            <EmptyState
              kind="filter-zero"
              title="No hosts match your filters"
              hint="Try clearing the search box or the OS / patch filters above."
              action={{
                label: "Clear filters",
                onClick: () => {
                  setQuery("");
                  setOs("(all)");
                  setPatch("(all)");
                  setOffset(0);
                },
              }}
            />
          ) : (
            <EmptyState
              kind="none-yet"
              title="No unified host records yet"
              hint="Hosts appear here after the first reconciliation run across all collectors."
            />
          )
        ) : (
          <DataTable<Host>
            columns={columns}
            rows={items}
            rowKey={(h) => h.short_hostname + h.fqdn}
            toolbarName="hosts"
            chips={chips}
            sortable
            caption="Unified host identity index"
            statusBar={{ shown: items.length, total: page.total }}
            onRowClick={(h) => setSelected(h)}
          />
        )}
      </Panel>

      <Pager offset={page.offset} shown={items.length} total={page.total} pageSize={PAGE_SIZE} noun="hosts" onOffset={setOffset} />

      <DetailDrawer
        title={selected ? `${selected.short_hostname || selected.fqdn} — host detail` : "Host detail"}
        open={selected !== null}
        onClose={() => setSelected(null)}
        triggerRef={triggerRef}
      >
        {selected && (
          <>
            <HostPurposeSection hostname={selected.short_hostname || selected.fqdn} />
            <HostPostureDetail resourceId={selected.windows_resource_id || selected.linux_resource_id || null} />
            <HostOsDetail linuxResourceId={selected.linux_resource_id} windowsResourceId={selected.windows_resource_id} />
            <HostVulnsDetail
              hostname={selected.short_hostname || selected.fqdn}
              onViewFullPage={() =>
                navigate(`/vulns?host=${encodeURIComponent(selected.short_hostname || selected.fqdn)}`)
              }
            />
          </>
        )}
      </DetailDrawer>
    </div>
  );
}

/** Per-host CVE drilldown. Fetches the host's open vulns on open
 *  (GET /api/dashboard/hosts/{hostname}/vulns?limit=50) and renders the
 *  aggregate risk/band header plus the CVE table. `onViewFullPage` keeps the
 *  legacy /vulns?host= deep-link available. */
function HostVulnsDetail({ hostname, onViewFullPage }: { hostname: string; onViewFullPage: () => void }) {
  const [data, setData] = useState<HostVulnsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setData(null);
    setError(null);
    apiGet<HostVulnsResponse>(`/api/dashboard/hosts/${encodeURIComponent(hostname)}/vulns?limit=50`)
      .then((d) => {
        if (live) setData({ header: d?.header ?? null, items: d?.items ?? [], total: d?.total ?? 0 });
      })
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
    return () => {
      live = false;
    };
  }, [hostname]);

  const hdr = data?.header ?? null;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  const columns: ColumnDef<HostVuln>[] = [
    { key: "cve_id", header: "CVE", typeGlyph: "pk" },
    {
      key: "severity",
      header: "Sev",
      typeGlyph: "enum",
      render: (v) => (v.severity ? <SeverityPill severity={r7Severity(v.severity) ?? v.severity} label={v.severity} /> : null),
    },
    {
      key: "cvss_v3",
      header: "CVSS",
      typeGlyph: "num",
      render: (v) =>
        v.cvss_v3 != null && v.cvss_v3 > 0 ? (
          <GaugeCell
            value={Number(v.cvss_v3) * 10}
            severity={r7Severity(v.severity) ?? "low"}
            display={Number(v.cvss_v3).toFixed(1)}
            label={`CVSS v3 for ${v.cve_id ?? "this CVE"}`}
          />
        ) : null,
    },
    {
      key: "sla",
      header: "SLA",
      typeGlyph: "enum",
      render: (v) => {
        if (!v.sla) return null;
        const s = v.sla.toLowerCase();
        return <Badge tone={s.includes("overdue") ? "err" : s.includes("due") ? "warn" : "neutral"}>{v.sla}</Badge>;
      },
    },
    {
      key: "patch",
      header: "Patch / KB",
      typeGlyph: "text",
      accessor: (v) => v.kb_id || v.solution_summary,
      render: (v) => {
        const patch = v.kb_id || v.solution_summary;
        if (!patch) return null;
        return (
          <span
            title={v.solution_summary || v.kb_id || ""}
            style={{ display: "inline-block", maxWidth: 190, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          >
            {patch}
          </span>
        );
      },
    },
  ];

  const worst = r7Severity(
    hdr && (hdr.vuln_critical || 0) > 0
      ? "critical"
      : (hdr?.vuln_severe || 0) > 0
        ? "severe"
        : (hdr?.vuln_moderate || 0) > 0
          ? "moderate"
          : "low"
  );

  return (
    <Panel
      className="ib-hosts-drawer-panel"
      rail={worst === "critical" || worst === "high" ? "err" : worst === "medium" ? "warn" : "ok"}
      mnemonic="VULN"
      description={`${total} open CVEs`}
      headerRight={
        <Button size="sm" onClick={onViewFullPage}>
          View full page →
        </Button>
      }
    >
      {hdr && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
          <SeverityPill severity="critical" label={`Critical ${hdr.vuln_critical || 0}`} />
          <SeverityPill severity="high" label={`Severe ${hdr.vuln_severe || 0}`} />
          <SeverityPill severity="medium" label={`Moderate ${hdr.vuln_moderate || 0}`} />
          <Badge tone="info">Risk {hdr.risk_score != null ? String(Math.round(hdr.risk_score)) : "0"}</Badge>
        </div>
      )}

      {error && <EmptyState kind="error" title="Host vulnerabilities failed to load" hint={error} />}
      {!error && data === null && <Skeleton count={3} height={20} />}
      {!error && data !== null && items.length === 0 && (
        <EmptyState kind="none-yet" title="No open vulnerabilities for this host." />
      )}
      {!error && items.length > 0 && (
        <DataTable<HostVuln>
          columns={columns}
          rows={items}
          rowKey={(v, i) => (v.cve_id || "") + i}
          showDensityToggle={false}
          statusBar={false}
          minWidth={0}
          caption={`Open CVEs for ${hostname}`}
        />
      )}
    </Panel>
  );
}

const POSTURE_TABS = [
  { key: "certs", label: "Certificates" },
  { key: "security", label: "Security" },
  { key: "firewall", label: "Firewall" },
  { key: "shares", label: "Shares" },
  { key: "admins", label: "Local admins" },
];

/** PCI-relevant host-posture detail — certificates, firewall/AV/RDP/UAC/SSH/
 *  SELinux summary, firewall rules, shares, Windows local admin/group
 *  membership. One fetch (GET /resources/{resourceId}/posture) backs all five
 *  tabs. Renders nothing when the host has neither a linux nor windows
 *  resource id. */
function HostPostureDetail({ resourceId }: { resourceId: string | null }) {
  const [tab, setTab] = useState("certs");
  const [data, setData] = useState<HostPosture | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!resourceId) return;
    let live = true;
    setData(null);
    setError(null);
    apiGet<HostPosture>(`/api/dashboard/resources/${encodeURIComponent(resourceId)}/posture`)
      .then((d) => {
        if (live) setData(d);
      })
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) {
          redirectToLogin();
          return;
        }
        setError(String(e));
      });
    return () => {
      live = false;
    };
  }, [resourceId]);

  if (!resourceId) return null;

  return (
    <Panel className="ib-hosts-drawer-panel" rail="info" mnemonic="POSTURE" description="host security posture">
      <Tabs tabs={POSTURE_TABS} active={tab} onChange={setTab} label="Host posture sections" />
      <div id={`tabpanel-${tab}`} role="tabpanel" aria-labelledby={`tab-${tab}`} style={{ marginTop: 12 }}>
        {error && <EmptyState kind="error" title="Host posture failed to load" hint={error} />}
        {!error && data === null && <Skeleton count={3} height={20} />}

        {!error && data !== null && tab === "certs" && (
          <MiniTable
            caption="Certificates"
            emptyTitle="No certificates collected for this host."
            columns={[
              { key: "store", header: "Store", typeGlyph: "text" },
              { key: "subject", header: "Subject", typeGlyph: "text" },
              {
                key: "not_after",
                header: "Expires",
                typeGlyph: "text",
                render: (c: HostCertificateRow) => (c.not_after ? new Date(c.not_after).toLocaleDateString() : null),
              },
              {
                key: "days_until_expiry",
                header: "Days left",
                typeGlyph: "num",
                render: (c: HostCertificateRow) =>
                  c.is_expired ? <Badge tone="err">Expired</Badge> : c.days_until_expiry,
              },
            ]}
            rows={data.certificates}
          />
        )}

        {!error && data !== null && tab === "security" &&
          (data.security_posture === null ? (
            <EmptyState kind="none-yet" title="No security posture summary collected for this host." />
          ) : (
            <KvGrid
              fields={[
                { label: "Firewall", value: fmtBool(data.security_posture.firewall_enabled) },
                { label: "AV", value: fmtBool(data.security_posture.av_enabled) },
                { label: "RDP", value: fmtBool(data.security_posture.rdp_enabled) },
                { label: "UAC", value: fmtBool(data.security_posture.uac_enabled) },
                { label: "SSH password auth", value: fmtBool(data.security_posture.ssh_password_auth) },
                { label: "SSH root login", value: fmtBool(data.security_posture.ssh_permit_root_login) },
                { label: "SELinux", value: data.security_posture.selinux_mode || "—" },
                { label: "AppArmor", value: data.security_posture.apparmor_status || "—" },
              ]}
            />
          ))}

        {!error && data !== null && tab === "firewall" && (
          <MiniTable
            caption="Firewall rules"
            emptyTitle="No firewall rules collected for this host."
            columns={[
              { key: "source", header: "Source", typeGlyph: "enum" },
              { key: "chain", header: "Chain", typeGlyph: "text" },
              { key: "rule_text", header: "Rule", typeGlyph: "text" },
              { key: "action", header: "Action", typeGlyph: "enum" },
            ]}
            rows={data.firewall_rules}
          />
        )}

        {!error && data !== null && tab === "shares" && (
          <MiniTable
            caption="Shares"
            emptyTitle="No shares collected for this host."
            columns={[
              { key: "share_type", header: "Type", typeGlyph: "enum" },
              { key: "name", header: "Name", typeGlyph: "text" },
              { key: "path", header: "Path", typeGlyph: "text" },
            ]}
            rows={data.shares}
          />
        )}

        {!error && data !== null && tab === "admins" &&
          (data.local_users.length === 0 && data.local_group_members.length === 0 ? (
            <EmptyState kind="none-yet" title="No local users/groups collected for this host." />
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {data.local_users.length > 0 && (
                <MiniTable
                  caption="Local users"
                  emptyTitle="No local users collected for this host."
                  columns={[
                    { key: "username", header: "User", typeGlyph: "pk" },
                    {
                      key: "is_admin",
                      header: "Admin",
                      typeGlyph: "enum",
                      render: (u: WindowsLocalUserRow) => fmtBool(u.is_admin),
                    },
                    {
                      key: "enabled",
                      header: "Enabled",
                      typeGlyph: "enum",
                      render: (u: WindowsLocalUserRow) => fmtBool(u.enabled),
                    },
                    {
                      key: "password_never_expires",
                      header: "Password never expires",
                      typeGlyph: "enum",
                      render: (u: WindowsLocalUserRow) => fmtBool(u.password_never_expires),
                    },
                  ]}
                  rows={data.local_users}
                />
              )}
              {data.local_group_members.length > 0 && (
                <MiniTable
                  caption="Local group members"
                  emptyTitle="No local group members collected for this host."
                  columns={[
                    { key: "group_name", header: "Group", typeGlyph: "fk" },
                    { key: "member_name", header: "Member", typeGlyph: "text" },
                  ]}
                  rows={data.local_group_members}
                />
              )}
            </div>
          ))}
      </div>
    </Panel>
  );
}

/** A drawer-scale DataTable: no density toggle, no status bar, still enforcing
 *  the NULL-token and type-glyph contracts. */
function MiniTable<T>({
  columns,
  rows: data,
  caption,
  emptyTitle,
}: {
  columns: ColumnDef<T>[];
  rows: T[];
  caption: string;
  emptyTitle: string;
}) {
  if (data.length === 0) return <EmptyState kind="none-yet" title={emptyTitle} />;
  return (
    <DataTable<T>
      columns={columns}
      rows={data}
      caption={caption}
      showDensityToggle={false}
      statusBar={false}
      minWidth={0}
    />
  );
}

/** Per-host Linux/Windows OS-internals summary, consuming the existing
 *  GET /resources/{id}/linux and GET /resources/{id}/windows endpoints. Renders
 *  nothing when the host has neither resource id. A 404 from either endpoint is
 *  "no data collected", not an error — both 404 by design when the collector has
 *  never written a row for that resource. */
function HostOsDetail({
  linuxResourceId,
  windowsResourceId,
}: {
  linuxResourceId: string | null;
  windowsResourceId: string | null;
}) {
  const available: ("linux" | "windows")[] = [
    ...(linuxResourceId ? (["linux"] as const) : []),
    ...(windowsResourceId ? (["windows"] as const) : []),
  ];
  const [tab, setTab] = useState<"linux" | "windows" | null>(available[0] ?? null);
  const [linux, setLinux] = useState<LinuxDetail | null>(null);
  const [windows, setWindows] = useState<WindowsPatch | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (tab === null) return;
    let live = true;
    setNotFound(false);
    setError(null);
    const rid = tab === "linux" ? linuxResourceId : windowsResourceId;
    if (!rid) return;
    const path =
      tab === "linux"
        ? `/api/dashboard/resources/${encodeURIComponent(rid)}/linux`
        : `/api/dashboard/resources/${encodeURIComponent(rid)}/windows`;
    apiGet<LinuxDetail | WindowsPatch>(path)
      .then((d) => {
        if (!live) return;
        if (tab === "linux") setLinux(d as LinuxDetail);
        else setWindows(d as WindowsPatch);
      })
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) {
          redirectToLogin();
          return;
        }
        if (e instanceof ApiError && e.status === 404) {
          setNotFound(true);
          return;
        }
        setError(String(e));
      });
    return () => {
      live = false;
    };
  }, [tab, linuxResourceId, windowsResourceId]);

  if (available.length === 0 || tab === null) return null;

  const securityUpdates = linux ? linux.pending_updates.filter((u) => u.security).length : 0;

  return (
    <Panel className="ib-hosts-drawer-panel" rail="info" mnemonic="OS" description="operating-system internals">
      <Tabs
        tabs={available.map((t) => ({ key: t, label: t === "linux" ? "Linux" : "Windows" }))}
        active={tab}
        onChange={(k) => setTab(k as "linux" | "windows")}
        label="OS detail sections"
      />
      <div id={`tabpanel-${tab}`} role="tabpanel" aria-labelledby={`tab-${tab}`} style={{ marginTop: 12 }}>
        {error && <EmptyState kind="error" title="OS detail failed to load" hint={error} />}
        {notFound && !error && <EmptyState kind="none-yet" title={`No ${tab} record collected for this host yet.`} />}
        {!error && !notFound && tab === "linux" && linux === null && <Skeleton count={3} height={20} />}
        {!error && !notFound && tab === "linux" && linux !== null && (
          <KvGrid
            fields={[
              { label: "Distro", value: linux.distro || "—" },
              { label: "Kernel", value: linux.kernel || "—" },
              { label: "Arch", value: linux.arch || "—" },
              { label: "Packages", value: linux.packages.length },
              { label: "Services", value: linux.services.length },
              { label: "Ports", value: linux.ports.length },
              { label: "Users", value: linux.users.length },
              { label: "Cron jobs", value: linux.crons.length },
              { label: "Mounts", value: linux.mounts.length },
              { label: "NICs", value: linux.nics.length },
              {
                label: "Pending updates",
                value:
                  securityUpdates > 0
                    ? `${linux.pending_updates.length} (${securityUpdates} security)`
                    : linux.pending_updates.length,
              },
            ]}
          />
        )}
        {!error && !notFound && tab === "windows" && windows === null && <Skeleton count={3} height={20} />}
        {!error && !notFound && tab === "windows" && windows !== null && (
          <KvGrid
            fields={[
              { label: "Pending KBs", value: windows.pending_count },
              { label: "WinRM status", value: windows.winrm_status },
              {
                label: "Last patched",
                value: windows.last_patched ? new Date(windows.last_patched).toLocaleDateString() : "—",
              },
            ]}
          />
        )}
      </div>
    </Panel>
  );
}

/** Editable host purpose/VLAN/subnet block. Fetches GET /hosts/{hostname}/purpose
 *  on open and offers an inline edit with three text inputs; Save PUTs the three
 *  fields and reconciles local state from the response. Provenance renders as a
 *  badge ("From repo" / "Edited by <user>" / "Not set"). After a save the
 *  persistence-MR link (mr_url) or a non-blocking "saved, MR not opened" warning
 *  (mr_error) is shown. AuthRequired → login redirect; 403 → revert + denied
 *  notice, no data lost. */
function HostPurposeSection({ hostname }: { hostname: string }) {
  const [entry, setEntry] = useState<HostPurposeEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [denied, setDenied] = useState(false);
  const [draft, setDraft] = useState({ purpose: "", vlan: "", subnet: "" });
  const [flash, setFlash] = useState<{ mr_url: string | null; mr_error: string | null } | null>(null);

  useEffect(() => {
    let live = true;
    setEntry(null);
    setError(null);
    setEditing(false);
    setDenied(false);
    setFlash(null);
    apiGet<HostPurposeEntry>(`/api/dashboard/hosts/${encodeURIComponent(hostname)}/purpose`)
      .then((d) => {
        if (live) setEntry(d);
      })
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
    return () => {
      live = false;
    };
  }, [hostname]);

  const startEdit = useCallback(() => {
    setDenied(false);
    setFlash(null);
    setDraft({ purpose: entry?.purpose ?? "", vlan: entry?.vlan ?? "", subnet: entry?.subnet ?? "" });
    setEditing(true);
  }, [entry]);

  const cancelEdit = useCallback(() => {
    setEditing(false);
    setDraft({ purpose: "", vlan: "", subnet: "" });
  }, []);

  const saveEdit = useCallback(async () => {
    setSaving(true);
    try {
      const res = await apiPut<HostPurposeWriteResult>(`/api/dashboard/hosts/${encodeURIComponent(hostname)}/purpose`, {
        purpose: draft.purpose.trim(),
        vlan: draft.vlan.trim(),
        subnet: draft.subnet.trim(),
      });
      setEntry({
        hostname: res.hostname,
        purpose: res.purpose,
        vlan: res.vlan,
        subnet: res.subnet,
        source: res.source,
        updated_at: res.updated_at,
        provenance: res.provenance,
      });
      setFlash({ mr_url: res.mr_url, mr_error: res.mr_error });
      setEditing(false);
      setDenied(false);
    } catch (err) {
      if (err instanceof AuthRequired) {
        redirectToLogin();
        return;
      }
      if (err instanceof ApiError && err.status === 403) {
        // Authenticated but not permitted: revert to display mode with a
        // non-destructive inline notice. No data is lost or mutated.
        setEditing(false);
        setDenied(true);
        return;
      }
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }, [hostname, draft]);

  const prov = provenanceBadge(entry);

  const input = (label: string, key: "purpose" | "vlan" | "subnet") => (
    <div style={{ minWidth: 0 }}>
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          fontFamily: FONT_MONO,
          color: MUTED,
          textTransform: "uppercase",
          letterSpacing: ".08em",
          marginBottom: 3,
        }}
      >
        {label}
      </div>
      <input
        aria-label={label}
        value={draft[key]}
        onChange={(ev) => setDraft((d) => ({ ...d, [key]: ev.target.value }))}
        onKeyDown={(ev) => {
          if (ev.key === "Enter") void saveEdit();
          else if (ev.key === "Escape") cancelEdit();
        }}
        style={{
          background: RAISED,
          border: `1px solid ${BORDER}`,
          borderRadius: 3,
          color: TEXT,
          fontSize: 12,
          fontFamily: FONT_MONO,
          padding: "3px 7px",
          width: "100%",
          boxSizing: "border-box",
        }}
      />
    </div>
  );

  return (
    <Panel
      className="ib-hosts-drawer-panel"
      rail="info"
      mnemonic="PURPOSE"
      description="host purpose · vlan · subnet"
      headerRight={
        <>
          <Badge tone={prov.tone}>{prov.label}</Badge>
          {!editing && entry !== null && (
            <Button size="sm" variant="ghost" onClick={startEdit} title="Edit purpose / VLAN / subnet">
              ✎ Edit
            </Button>
          )}
        </>
      }
    >
      {error && <EmptyState kind="error" title="Host purpose failed to load" hint={error} />}
      {!error && entry === null && <Skeleton count={2} height={20} />}

      {!error && entry !== null && !editing && (
        <KvGrid
          fields={[
            { label: "Purpose", value: entry.purpose || "—" },
            { label: "VLAN", value: entry.vlan || "—" },
            { label: "Subnet", value: entry.subnet || "—" },
          ]}
        />
      )}

      {!error && entry !== null && editing && (
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3,minmax(0,1fr))", gap: 14, marginBottom: 12 }}>
            {input("Purpose", "purpose")}
            {input("VLAN", "vlan")}
            {input("Subnet", "subnet")}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Button size="sm" variant="primary" loading={saving} onClick={() => void saveEdit()}>
              {saving ? "Saving…" : "Save"}
            </Button>
            <Button size="sm" variant="ghost" disabled={saving} onClick={cancelEdit}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {denied && <DrawerNote tone="warn">You are not permitted to edit host purpose.</DrawerNote>}

      {flash?.mr_url && (
        <DrawerNote>
          Saved.{" "}
          <a href={flash.mr_url} target="_blank" rel="noreferrer" style={{ color: "var(--blue)" }}>
            persistence MR open →
          </a>
        </DrawerNote>
      )}
      {flash && !flash.mr_url && flash.mr_error && (
        <DrawerNote tone="warn">Saved, but the persistence MR could not be opened: {flash.mr_error}</DrawerNote>
      )}
    </Panel>
  );
}

/** Provenance badge next to "Host purpose": repo-sourced values read
 *  "From repo"; UI edits read "Edited by <username>" (parsed from a
 *  `ui:<username>` source); an absent record reads "Not set". */
function provenanceBadge(entry: HostPurposeEntry | null): { label: string; tone: "neutral" | "info" } {
  if (!entry || entry.provenance === "unset") return { label: "Not set", tone: "neutral" };
  if (entry.provenance === "repo") return { label: "From repo", tone: "info" };
  const src = entry.source || "";
  const editor = src.startsWith("ui:") ? src.slice(3) : src || "unknown";
  return { label: `Edited by ${editor}`, tone: "info" };
}
