import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { PageShell, Panel, DataTable, EmptyState, type ColumnDef } from "../components/ui";
import { Icon } from "../lib/icons";
import { fmtDt } from "../lib/fmtDt";

/** Bulk host-purpose-map view (UI-0). GET /api/dashboard/host-purpose-map
 *  (hosts.py:100-119) returns a bare list[HostPurposeMapOut] — the same
 *  per-host purpose/VLAN/subnet record edited one-at-a-time in the Hosts.tsx
 *  drawer's HostPurposeSection, here as a single browsable/searchable table
 *  so an operator can audit the whole curated map at once. Read-only; no
 *  endpoint exists yet for a bulk edit, so this page is list-only.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md) as a
 *  Phase 3 mechanical DataTable/Panel conversion — see this file's git
 *  history / TRACKER for the itemized before/after. */
type HostPurposeMapEntry = {
  hostname: string;
  purpose: string | null;
  vlan: string | null;
  subnet: string | null;
  source: string | null;
  updated_at: string | null;
};

export function HostPurposeMap() {
  const [entries, setEntries] = useState<HostPurposeMapEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    const d = await apiGet<unknown>("/api/dashboard/host-purpose-map");
    setEntries(rows<HostPurposeMapEntry>(d));
  }, []);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  const filtered = useMemo(() => {
    if (!entries) return [];
    const q = query.trim().toLowerCase();
    if (!q) return entries;
    return entries.filter(
      (e) =>
        e.hostname.toLowerCase().includes(q) ||
        (e.purpose || "").toLowerCase().includes(q) ||
        (e.vlan || "").toLowerCase().includes(q),
    );
  }, [entries, query]);

  if (error) {
    return (
      <div>
        <PageShell
          icon={<Icon name="tag" />}
          title="Host Purpose Map"
          description="The curated host purpose/VLAN/subnet map (MR-J item 3), one row per host — edit an individual row from the Hosts drawer."
        />
        <EmptyState kind="error" title="Host purpose map failed to load" hint={error} />
      </div>
    );
  }
  if (entries === null) {
    return (
      <div>
        <PageShell
          icon={<Icon name="tag" />}
          title="Host Purpose Map"
          description="The curated host purpose/VLAN/subnet map (MR-J item 3), one row per host — edit an individual row from the Hosts drawer."
        />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const pills = [{ dot: "var(--ib-green)", text: `${entries.length} hosts mapped` }];
  const hasFilter = query.trim() !== "";
  const empty = filtered.length === 0;

  const columns: ColumnDef<HostPurposeMapEntry>[] = [
    { key: "hostname", header: "Hostname", typeGlyph: "pk" },
    { key: "purpose", header: "Purpose", typeGlyph: "text" },
    { key: "vlan", header: "VLAN", typeGlyph: "text" },
    { key: "subnet", header: "Subnet", typeGlyph: "text" },
    { key: "source", header: "Source", typeGlyph: "text" },
    {
      key: "updated_at",
      header: "Updated",
      typeGlyph: "text",
      render: (e) => fmtDt(e.updated_at),
    },
  ];

  return (
    <div>
      <PageShell
        icon={<Icon name="tag" />}
        title="Host Purpose Map"
        description="The curated host purpose/VLAN/subnet map (MR-J item 3), one row per host — edit an individual row from the Hosts drawer."
        pills={pills}
      />
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search hostname / purpose / VLAN…"
        style={{
          background: "var(--ib-raised)",
          border: "1px solid var(--ib-border)",
          borderRadius: "var(--ib-radius-sm)",
          padding: "5px 11px",
          color: "var(--ib-text)",
          fontSize: 12,
          outline: "none",
          fontFamily: "inherit",
          width: 260,
          marginBottom: 14,
        }}
      />

      <Panel rail="ok" mnemonic="HOSTMAP" description="host_purpose_map">
        {empty ? (
          hasFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No hosts match the current search"
              hint="Try a different hostname, purpose, or VLAN."
              action={{ label: "Clear search", onClick: () => setQuery("") }}
            />
          ) : (
            <EmptyState kind="none-yet" title="No host purpose entries yet" />
          )
        ) : (
          <DataTable<HostPurposeMapEntry>
            columns={columns}
            rows={filtered}
            rowKey={(e) => e.hostname}
            toolbarName="host_purpose_map"
            caption="Host purpose map: hostname, purpose, VLAN, subnet, source, and last updated"
            statusBar={{
              shown: filtered.length,
              total: entries.length,
              note: "read-only · no mutations possible from this view",
            }}
          />
        )}
      </Panel>
    </div>
  );
}
