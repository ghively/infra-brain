/** Hosts — the canonical host page, and the consolidation of what used to be
 *  three separate pages showing "a host" three different ways.
 *
 *  ## Why one page with tabs
 *
 *  Before this rebuild the app had `Hosts.tsx` (1348 lines), `Resources.tsx`
 *  (662) and `Fleet.tsx` (487), each with its own header, filter bar, table
 *  style and drawer, and no signal to the operator which was authoritative.
 *  Investigating the three data sources showed they are *not* the same rows
 *  rendered three ways — they are three genuinely different grains over the same
 *  fleet:
 *
 *   | Tab       | Endpoint                     | Grain                                          |
 *   |-----------|------------------------------|------------------------------------------------|
 *   | Hosts     | GET /api/dashboard/hosts     | one row per reconciled host identity (canonical)|
 *   | Resources | GET /api/dashboard/resources | one row per collector-owned resource (host × domain) |
 *
 *  Because the grains differ (a host appears once in Hosts and N times in
 *  Resources), merging them into one filtered table would have to either drop
 *  columns or silently duplicate rows. So they stay distinct *views*, unified by
 *  one page, one header, one filter idiom and one table primitive — which is the
 *  actual defect the audit found.
 *
 *  The "Rapid7 Fleet", "R7 Sites" and "R7 Tags" tabs are gone (with
 *  `hosts/FleetTab.tsx` and `hosts/R7Tabs.tsx`). All three read Rapid7's own
 *  tables — `r7_assets`, `r7_asset_sites`, `r7_asset_tags` — which are empty and
 *  stay empty: the `vuln` collector that fills them is `retired=True` and there
 *  is no InsightVM console in this environment. See `RETIRED_SOURCES` in nav.ts.
 *
 *  ## Routing
 *
 *  `/hosts` is canonical. `/resources` and `/fleet` remain **real routes
 *  rendering this same component** with a different `defaultTab`, rather than
 *  redirects to `/hosts?tab=…`, because:
 *
 *   - `components/Topbar.tsx`'s global search navigates to `/resources` carrying
 *     react-router *state* (`{detailOpen:{kind:'resource',data}}`) to open a
 *     drawer on arrival. A `<Navigate>` redirect does not carry that state
 *     through, so a redirect would silently break global search for resources.
 *   - Existing bookmarks/links to `/fleet` and `/resources` keep working with
 *     zero redirect hop.
 *   - `nav.ts`'s `SECONDARY_ROUTES` mechanism already exists to describe exactly
 *     this: routes reachable but deliberately absent from the sidebar. Keeping
 *     the paths keeps that list accurate and unchanged rather than fighting it.
 *
 *  The active tab is additionally reflected in `?tab=` so any tab is
 *  deep-linkable and survives a reload; `?tab=` on `/hosts` wins over the
 *  route's default when present.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { Icon } from "../lib/icons";
import { headerFor } from "../lib/headers";
import { GREEN, BLUE, YELLOW } from "../lib/tokens";
import { PageShell } from "../components/ui/PageShell";
import { Tabs } from "../components/ui/Tabs";
import { UnifiedHostsTab } from "./hosts/UnifiedHostsTab";
import { ResourcesTab } from "./hosts/ResourcesTab";
import "./hosts/hosts.css";

/** "fleet" | "sites" | "tags" were removed with their Rapid7-only tabs (see the
 *  file header). `isHostsTab` now rejects them, so a stale `?tab=fleet` bookmark
 *  falls back to the route's default tab instead of rendering nothing. */
export type HostsTab = "hosts" | "resources";

const TABS: { key: HostsTab; label: string }[] = [
  { key: "hosts", label: "Hosts" },
  { key: "resources", label: "Resources" },
];

const TAB_KEYS = TABS.map((t) => t.key);

const DESCRIPTIONS: Record<HostsTab, string> = {
  hosts:
    "Unified host identity — one row per host, reconciled across every live collector.",
  resources:
    "Raw collector resources — one row per host per collector domain, each with its own snapshot timeline and drift history.",
};

function isHostsTab(v: string | null): v is HostsTab {
  return v !== null && (TAB_KEYS as string[]).includes(v);
}

export function Hosts({ defaultTab = "hosts" }: { defaultTab?: HostsTab }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const paramTab = searchParams.get("tab");
  const [tab, setTab] = useState<HostsTab>(isHostsTab(paramTab) ? paramTab : defaultTab);

  // Keep the tab in step with the URL when the user edits `?tab=` or uses
  // back/forward, and with `defaultTab` when the route itself changes
  // (/resources → /fleet without unmounting).
  useEffect(() => {
    if (isHostsTab(paramTab)) setTab(paramTab);
    else setTab(defaultTab);
  }, [paramTab, defaultTab]);

  const onTabChange = useCallback(
    (key: string) => {
      if (!isHostsTab(key)) return;
      setTab(key);
      const next = new URLSearchParams(searchParams);
      next.set("tab", key);
      // `replace` so tab-flipping doesn't bury the previous page in history.
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const hdr = headerFor("resources");

  // Router state carrying a resource deep-link (Topbar global search) implies
  // the Resources tab regardless of which path it landed on.
  const forcedResourceTab = (location.state as { detailOpen?: { kind?: string } } | null)?.detailOpen?.kind === "resource";
  const activeTab: HostsTab = forcedResourceTab && !isHostsTab(paramTab) ? "resources" : tab;

  const pills = useMemo(
    () => [
      { dot: GREEN, text: "reconciled identity" },
      { dot: BLUE, text: "per-collector resources" },
      { dot: YELLOW, text: "rapid7 risk" },
    ],
    [],
  );

  return (
    <div>
      <PageShell
        icon={<Icon name={hdr.icon} />}
        title="Hosts"
        description={DESCRIPTIONS[activeTab]}
        pills={pills}
      />

      <Tabs tabs={TABS} active={activeTab} onChange={onTabChange} label="Host views" className="ib-hosts-tabs" />

      <div
        id={`tabpanel-${activeTab}`}
        role="tabpanel"
        aria-labelledby={`tab-${activeTab}`}
        style={{ marginTop: 14 }}
      >
        {activeTab === "hosts" && <UnifiedHostsTab />}
        {activeTab === "resources" && <ResourcesTab />}
      </div>
    </div>
  );
}

/** `/resources` renders the consolidated page with the Resources tab active —
 *  see the routing note in this file's header for why it is a real route and not
 *  a redirect. */
export function Resources() {
  return <Hosts defaultTab="resources" />;
}

/** `/fleet` used to open the Rapid7 Fleet tab, which no longer exists (Rapid7 is
 *  retired and `r7_assets` is empty — see the file header). The route is kept and
 *  now lands on the canonical Hosts view rather than 404-ing an old bookmark. */
export function Fleet() {
  return <Hosts defaultTab="hosts" />;
}
