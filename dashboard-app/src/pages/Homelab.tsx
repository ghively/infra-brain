/** Homelab Services — the dashboard-side piece the "Batch N: home-lab service
 *  status by category" backend work (see mcp_server.py's
 *  get_homelab_service_category and src/infra_brain/homelab_services_manifest.json)
 *  deliberately deferred: nothing in dashboard-app/ grouped
 *  `Resource(domain="homelab_services")` rows by their `category` metadata field.
 *
 *  No new backend route was needed — GET /api/dashboard/resources already
 *  accepts `domain=homelab_services` (used today only as one chip among many
 *  on the generic Hosts → Resources tab, see pages/hosts/ResourcesTab.tsx) and
 *  its `list_resources` handler (api/routers/hosts.py) already flattens the
 *  full JSONB `metadata_` payload — including `category`/`url`/`status`/
 *  `http_status`, the same fields HomelabServicesAgent writes
 *  (agents/homelab_services.py) — into each row's `meta: KV[]` list. That data
 *  was simply never rendered: ResourcesTab's own `Resource` type doesn't even
 *  declare a `meta` field. This page is the first consumer that reads it.
 *
 *  UI pattern: the stacked-per-group `Panel` + `DataTable` layout from
 *  Cloud.tsx's `DomainPanel` helper is the closest existing precedent in this
 *  codebase for "one page, several titled sections" (surveyed against
 *  Compl.tsx/Vulns.tsx/Software.tsx, which all use `Tabs` as a single-select
 *  status/severity *filter* rather than rendering every group at once — not
 *  what "group by category" asked for here). Unlike Cloud.tsx's five
 *  compile-time-fixed panels, the categories here are derived at runtime from
 *  whatever the manifest + collector actually produced, sorted alphabetically,
 *  each with its own service count in the description.
 */
import { useCallback, useEffect, useState } from "react";
import { apiGet, AuthRequired, redirectToLogin, rows } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { Icon } from "../lib/icons";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { PageShell, Panel, DataTable, EmptyState, Badge, type ColumnDef } from "../components/ui";

type MetaKV = { k: string; v: string };

/** Shape of one row from GET /api/dashboard/resources?domain=homelab_services
 *  (ResourceOut, api/schemas.py) — `meta` carries the flattened metadata_
 *  dict, which is where category/url/status/http_status actually live for
 *  this domain (see the file header). `status` at the top level is the
 *  generic EOL/healthy flag computed by list_resources — deliberately NOT the
 *  same thing as the homelab probe's up/down status buried in `meta`. */
type Resource = {
  id: string;
  hostname: string;
  domain: string;
  resource_type: string;
  zone: string;
  status: string;
  last_seen_at: string;
  drift_count: number;
  meta?: MetaKV[];
};

/** One flattened row ready for the per-category table: the resource's own
 *  fields plus category/url/probe-status/http_status pulled out of `meta`. */
type ServiceRow = {
  id: string;
  name: string;
  host: string;
  category: string;
  url: string;
  probeStatus: string;
  httpStatus: string;
  lastSeenAt: string;
};

const UNCATEGORIZED = "(uncategorized)";

/** The manifest lets a service entry omit `category`; HomelabServicesAgent
 *  still probes and persists it in that case (see homelab_services.py:138,
 *  `entry.get("category", "")`), so an empty-string category is expected
 *  input here, not a bug — bucket it under one explicit group rather than
 *  silently dropping those rows or scattering them under `""`. */
function metaValue(meta: MetaKV[] | undefined, key: string): string {
  return meta?.find((kv) => kv.k === key)?.v ?? "";
}

function toServiceRow(r: Resource): ServiceRow {
  return {
    id: r.id,
    name: r.hostname,
    host: metaValue(r.meta, "host") || r.zone,
    category: metaValue(r.meta, "category") || UNCATEGORIZED,
    url: metaValue(r.meta, "url"),
    probeStatus: metaValue(r.meta, "status"),
    httpStatus: metaValue(r.meta, "http_status"),
    lastSeenAt: r.last_seen_at,
  };
}

/** Homelab probe status ("up"/"down", set by HomelabServicesAgent.collect())
 *  → badge tone. Anything else (empty string — not yet probed, or an
 *  unrecognized future value) reads neutral, never green, same "unknown is
 *  not healthy" convention as ResourcesTab's `statusTone`. */
function probeStatusTone(status: string): "ok" | "err" | "neutral" {
  const s = status.toLowerCase();
  if (s === "up") return "ok";
  if (s === "down") return "err";
  return "neutral";
}

function groupByCategory(items: ServiceRow[]): [string, ServiceRow[]][] {
  const groups = new Map<string, ServiceRow[]>();
  for (const row of items) {
    const bucket = groups.get(row.category);
    if (bucket) bucket.push(row);
    else groups.set(row.category, [row]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([category, rowsInCategory]) => [
      category,
      [...rowsInCategory].sort((a, b) => a.name.localeCompare(b.name)),
    ]);
}

const columns: ColumnDef<ServiceRow>[] = [
  { key: "name", header: "Service", typeGlyph: "pk" },
  { key: "host", header: "Host", typeGlyph: "text", render: (r) => r.host || undefined },
  {
    key: "url",
    header: "URL",
    typeGlyph: "text",
    render: (r) =>
      r.url ? (
        <a href={r.url} target="_blank" rel="noreferrer" style={{ color: "var(--ib-blue)" }}>
          {r.url}
        </a>
      ) : undefined,
  },
  {
    key: "probeStatus",
    header: "Status",
    typeGlyph: "enum",
    render: (r) => (r.probeStatus ? <Badge tone={probeStatusTone(r.probeStatus)}>{r.probeStatus}</Badge> : undefined),
  },
  { key: "httpStatus", header: "HTTP", typeGlyph: "num", render: (r) => r.httpStatus || undefined },
];

/** Per-category section: a `Panel` header (category name + service count)
 *  wrapping a `DataTable` of that category's services — mirrors Cloud.tsx's
 *  `DomainPanel` helper (see file header for why that's the chosen
 *  precedent), specialized to a runtime-derived category instead of a
 *  compile-time domain. */
function CategoryPanel({ category, items }: { category: string; items: ServiceRow[] }) {
  const upCount = items.filter((r) => r.probeStatus.toLowerCase() === "up").length;
  return (
    <div style={{ marginBottom: 20 }}>
      <Panel
        rail={upCount === items.length ? "ok" : upCount === 0 ? "err" : "warn"}
        mnemonic={category.toUpperCase()}
        description={`${items.length} service${items.length === 1 ? "" : "s"} · ${upCount} up`}
      >
        <DataTable<ServiceRow>
          columns={columns}
          rows={items}
          rowKey={(r) => r.id}
          toolbarName={`homelab_${category}`}
          caption={`Homelab services in the "${category}" category: name, host, url, and probe status`}
          statusBar={{ shown: items.length, note: "read-only · no mutations possible from this view" }}
        />
      </Panel>
    </div>
  );
}

/** Homelab Services page — GET /api/dashboard/resources?domain=homelab_services,
 *  grouped client-side by the `category` field already present in each row's
 *  metadata (see file header). No server-side grouping endpoint was added:
 *  the existing generic resources list already returns every field needed,
 *  and the category set is small/derived, not something worth a bespoke
 *  aggregate route for a single page. */
export function Homelab() {
  const [items, setItems] = useState<ServiceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ domain: "homelab_services", limit: "200" });
    const data = await apiGet<unknown>(`/api/dashboard/resources?${params.toString()}`);
    const resources = rows<Resource>(data);
    setItems(resources.map(toServiceRow));
  }, []);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  const h = headerFor("homelab");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Homelab services failed to load" hint={error} />
      </div>
    );
  }
  if (items === null) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const upCount = items.filter((r) => r.probeStatus.toLowerCase() === "up").length;
  const downCount = items.filter((r) => r.probeStatus.toLowerCase() === "down").length;
  const groups = groupByCategory(items);

  const pills = [
    { dot: "var(--ib-blue)", text: `${items.length} services` },
    { dot: "var(--ib-green)", text: `${upCount} up` },
    { dot: "var(--ib-red, #f87171)", text: `${downCount} down` },
    { dot: "var(--ib-muted)", text: `${groups.length} categories` },
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} pills={pills} />

      {groups.length === 0 ? (
        <EmptyState
          kind="none-yet"
          title="No homelab services collected"
          hint="HomelabServicesAgent probes every URL-bearing entry in homelab_services_manifest.json — data appears here once it has run a sweep."
        />
      ) : (
        groups.map(([category, categoryItems]) => (
          <CategoryPanel key={category} category={category} items={categoryItems} />
        ))
      )}
    </div>
  );
}
