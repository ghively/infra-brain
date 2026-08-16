import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, AuthRequired, redirectToLogin } from "../api";
import { DetailDrawer } from "../components/DetailDrawer";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { fmtDt } from "../lib/fmtDt";
import { Icon } from "../lib/icons";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  Panel,
  DataTable,
  Button,
  EmptyState,
  Badge,
  NullCell,
  type ColumnDef,
} from "../components/ui";

type NetDiscoveryServiceItem = {
  port: number;
  proto: string;
  service: string;
  banner: string;
  fingerprint: string;
  is_dangerous: boolean;
  is_suspicious: boolean;
  last_seen: string | null;
};

type NetDiscoveryHostItem = {
  id: string;
  ip: string;
  mac: string;
  mac_vendor: string;
  hostname: string;
  first_seen: string | null;
  last_seen: string | null;
  responded: boolean;
  discovery_tier: string;
  is_fragile: boolean;
  is_known: boolean;
  is_shadow_it: boolean;
  threat_level: string;
  zone: string;
  resource_id: string | null;
  services: NetDiscoveryServiceItem[];
};

type NetDiscoverySummary = {
  total_hosts: number;
  shadow_it_count: number;
  known_count: number;
  by_threat_level: Record<string, number>;
};

type NetDiscoveryResponse = {
  items: NetDiscoveryHostItem[];
  total: number;
  limit: number;
  offset: number;
  summary: NetDiscoverySummary;
};

const PAGE_SIZE = 100;

const THREAT_TABS: [string, string][] = [
  ["(all)", "All"],
  ["high", "High"],
  ["med", "Medium"],
  ["low", "Low"],
  ["none", "None"],
];

/** Threat level → Badge tone. Server-side `threat_level` ranking (TRK-169)
 *  means this page never re-derives ordering client-side — only display tone
 *  is decided here. Unknown/malformed levels map to `neutral` (muted gray),
 *  deliberately distinct from the `ok` (green) tone used for a confirmed-safe
 *  "none" — the specific bug this rebuild must not regress (TRK-176 Wave 2:
 *  "NetDiscovery's unknown-threat default rendered identically to
 *  explicitly-safe 'none'"). */
function threatTone(level: string): "err" | "warn" | "info" | "ok" | "neutral" {
  const v = (level || "").toLowerCase();
  if (v === "high") return "err";
  if (v === "med") return "warn";
  if (v === "low") return "info";
  if (v === "none") return "ok";
  return "neutral";
}

/** Detail content for a discovered host's service list, shown inside
 *  DetailDrawer. Renders banner/fingerprint as plain text only — never
 *  interpreted as HTML (this is untrusted scanned-service data). */
function NetDiscoveryHostDetail({ item }: { item: NetDiscoveryHostItem }) {
  const row = (label: string, value: React.ReactNode) => (
    <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
      <span style={{ fontSize: 11, color: "var(--ib-muted)", fontWeight: 600, minWidth: 110, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 12, color: "var(--ib-text)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)", wordBreak: "break-all" }}>
        {value}
      </span>
    </div>
  );
  return (
    <div style={{ padding: "4px 0" }}>
      {row("IP", item.ip)}
      {row("MAC", item.mac || <NullCell />)}
      {row("MAC Vendor", item.mac_vendor || <NullCell />)}
      {row("Hostname", item.hostname || <NullCell />)}
      {row("Discovery Tier", item.discovery_tier)}
      {row("Zone", item.zone || <NullCell />)}
      {row("Threat Level", <Badge tone={threatTone(item.threat_level)}>{item.threat_level}</Badge>)}
      {row("Shadow IT", item.is_shadow_it ? "yes" : "no")}
      {row("Known", item.is_known ? "yes" : "no")}
      {row("First Seen", fmtDt(item.first_seen))}
      {row("Last Seen", fmtDt(item.last_seen))}

      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          color: "var(--ib-muted)",
          textTransform: "uppercase",
          letterSpacing: ".1em",
          margin: "20px 0 10px",
        }}
      >
        Discovered Services ({item.services.length})
      </div>
      {item.services.length === 0 ? (
        <div style={{ fontSize: 12, color: "var(--ib-faint)" }}>No open services discovered on this host.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {item.services.map((svc, i) => (
            <div
              key={`${svc.port}-${svc.proto}-${i}`}
              style={{ background: "var(--ib-raised)", border: "1px solid var(--ib-border)", borderRadius: 9, padding: "10px 12px" }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ib-text)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>
                  {svc.port}/{svc.proto}
                </span>
                <span style={{ display: "flex", gap: 6 }}>
                  {svc.is_dangerous && <Badge tone="err">dangerous</Badge>}
                  {svc.is_suspicious && <Badge tone="warn">suspicious</Badge>}
                </span>
              </div>
              <div style={{ fontSize: 11, color: "var(--ib-muted)" }}>{svc.service || "unknown service"}</div>
              {svc.banner && (
                <div style={{ fontSize: 11, color: "var(--ib-faint)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)", marginTop: 4, wordBreak: "break-all" }}>
                  {svc.banner}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Standalone page for infra-brain's network-discovery / shadow-IT detection
 *  capability (UI Batch 8 / GitLab #64), backed by
 *  GET /api/dashboard/netdiscovery/hosts
 *  (src/infra_brain/api/routers/netdiscovery.py). Rebuilt on the Phase 1
 *  design system (see dashboard-app/DESIGN.md) as a Phase 3 mechanical
 *  DataTable/Panel conversion.
 *
 *  Sort decision (this rebuild): the endpoint itself already returns rows
 *  ordered by a SQLAlchemy `case()`-based severity rank (TRK-169 — the prior
 *  bug was a backend `.desc()` over the raw `threat_level` text column,
 *  fixed server-side, not a client-side sort this rebuild needs to
 *  reimplement). This page therefore does not enable `DataTable`'s
 *  client-side `sortable` prop for the threat column — doing so would let a
 *  user's in-page sort silently fight the correct server-side severity
 *  order. Other columns also stay unsorted client-side since this is a
 *  paginated, filtered, server-driven list (same convention as the legacy
 *  page, which had no client sort at all). */
export function NetDiscovery() {
  const [data, setData] = useState<NetDiscoveryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [threatLevel, setThreatLevel] = useState("(all)");
  const [shadowOnly, setShadowOnly] = useState(false);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<NetDiscoveryHostItem | null>(null);
  const triggerRef = useRef<HTMLElement>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (threatLevel !== "(all)") params.set("threat_level", threatLevel);
    if (shadowOnly) params.set("is_shadow_it", "true");
    setData(await apiGet<NetDiscoveryResponse>(`/api/dashboard/netdiscovery/hosts?${params}`));
  }, [threatLevel, shadowOnly, offset]);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  const h = headerFor("netdiscovery");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Network discovery failed to load" hint={error} />
      </div>
    );
  }
  if (data === null) return <Skeleton count={3} height={32} />;

  const summary = data.summary;
  const pills = [
    { dot: "var(--ib-blue)", text: `${summary.total_hosts} discovered hosts` },
    { dot: "var(--ib-red)", text: `${summary.shadow_it_count} shadow IT` },
    { dot: "var(--ib-green)", text: `${summary.known_count} known` },
  ];

  const pageStart = data.total === 0 ? 0 : data.offset + 1;
  const pageEnd = Math.min(data.offset + data.items.length, data.total);
  const hasPrev = data.offset > 0;
  const hasNext = data.offset + data.items.length < data.total;
  const hasAnyFilter = threatLevel !== "(all)" || shadowOnly;
  const empty = data.items.length === 0;

  const columns: ColumnDef<NetDiscoveryHostItem>[] = [
    { key: "ip", header: "IP", typeGlyph: "pk" },
    {
      key: "hostname",
      header: "Hostname",
      typeGlyph: "text",
      render: (host) => host.hostname || null,
    },
    {
      key: "mac",
      header: "MAC",
      typeGlyph: "text",
      render: (host) => (host.mac ? <span style={{ fontFamily: "var(--ib-mono)" }}>{host.mac}</span> : null),
    },
    { key: "discovery_tier", header: "Tier", typeGlyph: "enum" },
    {
      key: "threat_level",
      header: "Threat",
      typeGlyph: "enum",
      sortable: false,
      render: (host) => <Badge tone={threatTone(host.threat_level)}>{host.threat_level}</Badge>,
    },
    {
      key: "is_shadow_it",
      header: "Shadow IT",
      typeGlyph: "enum",
      render: (host) =>
        host.is_shadow_it ? (
          <Badge tone="err">yes</Badge>
        ) : (
          <span style={{ color: "var(--ib-faint)" }}>no</span>
        ),
    },
    {
      key: "last_seen",
      header: "Last Seen",
      typeGlyph: "text",
      render: (host) => fmtDt(host.last_seen),
    },
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} pills={pills} />

      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
        {THREAT_TABS.map(([v, label]) => (
          <Button
            key={v}
            variant={threatLevel === v ? "primary" : "ghost"}
            size="sm"
            onClick={() => {
              setThreatLevel(v);
              setOffset(0);
            }}
          >
            {label}
          </Button>
        ))}
        <div style={{ width: 1, height: 16, background: "var(--ib-border)", margin: "0 4px" }} />
        <Button
          variant={shadowOnly ? "primary" : "ghost"}
          size="sm"
          onClick={() => {
            setShadowOnly((v) => !v);
            setOffset(0);
          }}
        >
          Shadow IT only
        </Button>
      </div>

      <Panel
        rail={summary.shadow_it_count > 0 ? "err" : "info"}
        mnemonic="NETSCAN"
        description="netdiscovery_hosts ⨝ netdiscovery_services"
        live
      >
        {empty ? (
          hasAnyFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No discovered hosts match the current filters"
              hint="Try clearing the threat-level or Shadow IT filters."
              action={{
                label: "Clear filters",
                onClick: () => {
                  setThreatLevel("(all)");
                  setShadowOnly(false);
                  setOffset(0);
                },
              }}
            />
          ) : (
            <EmptyState
              kind="none-yet"
              title="No hosts discovered yet"
              hint="Discovered hosts will appear here after the next network-discovery sweep."
            />
          )
        ) : (
          <DataTable<NetDiscoveryHostItem>
            columns={columns}
            rows={data.items}
            rowKey={(host) => host.id}
            onRowClick={(host) => setSelected(host)}
            toolbarName="netdiscovery_hosts"
            caption="Discovered hosts: IP, hostname, MAC, discovery tier, threat level, Shadow IT flag, and last seen"
            statusBar={{
              shown: data.items.length,
              total: data.total,
              note: "read-only · no mutations possible from this view",
            }}
          />
        )}
      </Panel>

      {!empty && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
          <span style={{ fontSize: 11, color: "var(--ib-faint)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)", marginRight: 6 }}>
            {pageStart}–{pageEnd} of {data.total}
          </span>
          {hasPrev && (
            <Button size="sm" onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>
              Prev
            </Button>
          )}
          {hasNext && (
            <Button size="sm" onClick={() => setOffset((o) => o + PAGE_SIZE)}>
              Next
            </Button>
          )}
        </div>
      )}

      <DetailDrawer
        title={selected ? `${selected.ip} — discovered host` : "Host detail"}
        open={selected !== null}
        onClose={() => setSelected(null)}
        triggerRef={triggerRef}
      >
        {selected && <NetDiscoveryHostDetail item={selected} />}
      </DetailDrawer>
    </div>
  );
}
