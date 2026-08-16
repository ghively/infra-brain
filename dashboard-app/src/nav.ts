/** Navigation registry for the dashboard shell — one flat list, grouped and
 *  filtered at render time.
 *
 *  ## What changed and why
 *
 *  This file used to be `NAV_SECTIONS`: a hand-maintained nested array of
 *  sections, each with its own inline item list. That shape had two problems
 *  the owner hit on first login.
 *
 *  1. **It advertised sources this deployment does not have.** The sidebar
 *     listed vSphere, Cloud/K8s, Rapid7 fleet views and a Rapid7 software
 *     inventory. Those are remnants of the enterprise deployment this codebase
 *     grew out of; this is a homelab and those upstream systems will never be
 *     configured here. The backend has said so for a while —
 *     `AgentSpec.retired=True` — but nothing connected that declaration to the
 *     UI, so the nav kept offering them.
 *  2. **Adding an entry meant editing structure.** Finding the right section,
 *     matching the nesting, and not disturbing its siblings. A nav entry should
 *     be one line of data.
 *
 *  Both are fixed by inverting the shape: `NAV_ITEMS` is a FLAT array of
 *  self-describing entries (each naming its own group), and `buildNavSections()`
 *  groups and filters them at render time against the live source inventory
 *  from `GET /api/dashboard/sources`. See `api/source_visibility.py` for the
 *  rule that endpoint applies.
 *
 *  ## Adding a page
 *
 *  Append one object to `NAV_ITEMS`. That is the whole change — group ordering,
 *  section headings and visibility all fall out of it:
 *
 *      { label: "Chat", path: "/chat", group: "overview" }
 *
 *  Add `domains: ["<collector domain>"]` only if the page is a *view of a
 *  collector's data*, so it can disappear when that collector is retired and
 *  empty. Pages that are features of the app itself (Chat, Settings, Help,
 *  Agents) name no domain and are always shown.
 */

/** Verdict strings from `GET /api/dashboard/sources` (`api/source_visibility.py`). */
export type SourceVisibility = "visible" | "historical" | "hidden";

/** `domain -> verdict`, as consumed by `buildNavSections`. */
export type VisibilityMap = Record<string, SourceVisibility>;

/** Group ids, in the order their sections render. Six groups, ordered the way a
 *  homelab operator actually works: what's happening now, then the machines,
 *  then whether they're safe, then the work being done to them, then what the
 *  system knows, then the app's own knobs. */
export type NavGroupId =
  | "overview"
  | "infrastructure"
  | "security"
  | "operations"
  | "knowledge"
  | "system";

export const NAV_GROUPS: { id: NavGroupId; heading: string }[] = [
  { id: "overview", heading: "Overview" },
  { id: "infrastructure", heading: "Infrastructure" },
  { id: "security", heading: "Security & Compliance" },
  { id: "operations", heading: "Operations" },
  { id: "knowledge", heading: "Knowledge & Agents" },
  { id: "system", heading: "System" },
];

export type NavItem = {
  label: string;
  path: string;
  group: NavGroupId;
  /** Optional key into the `/api/dashboard/counts` response, rendered as a
   *  badge by `Sidebar.tsx`. */
  countKey?: string;
  /** Collector domains this page is a view of. Omit for pages that are features
   *  of the app rather than views of collected data — those are always shown.
   *  An item is shown if ANY listed domain is `visible`, marked historical if
   *  ALL of them are `historical`, and hidden only if every one is `hidden`.
   *  "Any visible wins" is deliberate: a page fed by several collectors (Security
   *  reads wazuh + pki + secrets_inventory) must not vanish because one of its
   *  sources was retired. */
  domains?: string[];
};

/** THE list. Flat, ordered within each group, grouped at render time.
 *
 *  Deleted from the pre-2026-08 version of this file, all for the same reason —
 *  retired collector, zero rows in the database, so `source_visibility()` returns
 *  `hidden` and there is nothing for the page to render (see `RETIRED_SOURCES`
 *  below for the per-path epitaphs and the verified row counts):
 *    - "vSphere" (/vsphere)  - "Cloud / K8s / Net" (/cloud)
 *    - the Rapid7 CVE detail view (/r7detail)
 *  ("Software Inventory" (/software) was in this list and was RESTORED by owner
 *   decision 2026-08-13: software inventory is wanted, it just has no live
 *   collector yet — a missing feed, not a source this deployment will never
 *   run. It renders the real page with an explanatory empty state.)
 *  Their routes still resolve — to a "not configured here" page, never a 404. */
export const NAV_ITEMS: NavItem[] = [
  // ── Overview ────────────────────────────────────────────────────────────
  { label: "Dashboard", path: "/", group: "overview" },
  // Chat (rev14/T2) and Help (rev14/T4) carry no `domains`: they are app
  // features, not data sources, so the source-visibility filter never hides
  // them. Chat sits in Overview deliberately — for an operator who has never
  // had an LLM attached, asking a question is the first useful thing to find,
  // not the eleventh.
  { label: "Ask Infra Brain", path: "/chat", group: "overview" },
  { label: "Help & Getting Started", path: "/help", group: "overview" },

  // ── Infrastructure ──────────────────────────────────────────────────────
  { label: "Hosts", path: "/hosts", group: "infrastructure" },
  { label: "Host Purpose Map", path: "/host-purpose-map", group: "infrastructure" },
  // Deliberately carries NO `domains` filter. Its only historical feed (Rapid7)
  // is retired and linux_packages is empty, so a domain-filtered entry would
  // hide it — but the owner wants software inventory collected here, so the
  // page stays visible and its empty state explains the real state of play
  // rather than the page silently not existing.
  { label: "Software Inventory", path: "/software", group: "infrastructure" },
  { label: "Homelab Services", path: "/homelab", group: "infrastructure", domains: ["homelab_services"] },
  { label: "Network Discovery", path: "/netdiscovery", group: "infrastructure", domains: ["netdiscovery", "discovery"] },
  { label: "IaC / Pipelines", path: "/iac", group: "infrastructure", domains: ["iac", "cicd"] },

  // ── Security & Compliance ───────────────────────────────────────────────
  { label: "Vulnerabilities", path: "/vulns", group: "security", countKey: "open_cves", domains: ["vuln_triage"] },
  { label: "Security & Audit", path: "/security", group: "security", domains: ["wazuh", "pki", "secrets_inventory"] },
  { label: "Compliance", path: "/compl", group: "security", domains: ["compliance"] },
  { label: "EOL Registry", path: "/eol", group: "security", countKey: "eol_overdue", domains: ["eol"] },

  // ── Operations ──────────────────────────────────────────────────────────
  { label: "Drift Events", path: "/drift", group: "operations", countKey: "open_drift", domains: ["drift"] },
  { label: "Remediation", path: "/remed", group: "operations", domains: ["remediation"] },
  { label: "Inventory Reconcile", path: "/invrec", group: "operations", countKey: "invrec_proposed", domains: ["inventory_reconcile"] },
  { label: "Collection Runs", path: "/collruns", group: "operations" },
  { label: "Sweeps", path: "/sweeps", group: "operations" },
  { label: "Scan Schedule", path: "/scanschedule", group: "operations" },
  { label: "Notifications", path: "/notifications", group: "operations" },

  // ── Knowledge & Agents ──────────────────────────────────────────────────
  { label: "Knowledge Graph", path: "/graph", group: "knowledge" },
  { label: "Knowledge Store", path: "/documents", group: "knowledge" },
  { label: "Agents", path: "/agents", group: "knowledge" },
  { label: "Agent Activity", path: "/activity", group: "knowledge" },
  { label: "Agent Decisions", path: "/decisions", group: "knowledge" },
  // rev14/T7: the LLM audit surface — window-scoped cost/outcome aggregates and
  // an iteration ladder. No `domains`: it reports on the LLM lane itself, which
  // is not a collected data source, so source-visibility must never hide it.
  { label: "LLM Observability", path: "/llm", group: "knowledge" },
  { label: "Instincts", path: "/instincts", group: "knowledge" },
  { label: "Observations", path: "/observations", group: "knowledge" },
  { label: "Generated Scripts", path: "/scripts", group: "knowledge" },
  { label: "Integration Proposals", path: "/intprops", group: "knowledge" },

  // ── System ──────────────────────────────────────────────────────────────
  { label: "Settings", path: "/settings", group: "system" },
  { label: "Agent Config", path: "/agentconfig", group: "system" },
  { label: "MCP Keys", path: "/mcpkeys", group: "system" },
  { label: "Custom Views", path: "/customviews", group: "system" },
  { label: "Saved Views", path: "/savedviews", group: "system" },
];

/** A nav item resolved against the live source inventory. */
export type ResolvedNavItem = NavItem & {
  /** True when every domain this page reads is retired-but-still-holding-data.
   *  The sidebar marks these so a frozen snapshot is never mistaken for a live
   *  feed — `identity` is the real example: retired collector, 53 real rows. */
  historical: boolean;
};

export type NavSection = {
  id: NavGroupId;
  heading: string;
  items: ResolvedNavItem[];
};

/** Resolve ONE item against the inventory. Exported for testing.
 *
 *  Unknown domains count as visible, not hidden. A domain the endpoint has not
 *  reported yet — a brand-new collector, or the window before the fetch resolves
 *  — must not cause its page to blink out of the nav. Hiding is only ever done
 *  on a positive `hidden` verdict for every domain the page reads.
 */
export function resolveNavItem(item: NavItem, visibility: VisibilityMap): ResolvedNavItem | null {
  if (!item.domains || item.domains.length === 0) {
    return { ...item, historical: false };
  }
  const verdicts = item.domains.map((d) => visibility[d] ?? "visible");
  if (verdicts.some((v) => v === "visible")) return { ...item, historical: false };
  if (verdicts.some((v) => v === "historical")) return { ...item, historical: true };
  return null; // every domain hidden
}

/** Group `items` into ordered sections, dropping hidden ones and empty groups.
 *
 *  Pure and synchronous: `Sidebar.tsx` calls it on every render with whatever
 *  inventory it currently holds, so an empty map (first paint, or a failed
 *  fetch) yields the full nav rather than a blank sidebar.
 */
export function buildNavSections(
  items: NavItem[] = NAV_ITEMS,
  visibility: VisibilityMap = {},
): NavSection[] {
  return NAV_GROUPS.map(({ id, heading }) => ({
    id,
    heading,
    items: items
      .filter((i) => i.group === id)
      .map((i) => resolveNavItem(i, visibility))
      .filter((i): i is ResolvedNavItem => i !== null),
  })).filter((section) => section.items.length > 0);
}

/** Routes that exist and are reachable but are deliberately NOT sidebar items,
 *  kept listed so it is obvious to anyone auditing nav coverage against
 *  `App.tsx` that these are choices, not oversights.
 *
 *  `/resources` and `/fleet` are aliases of the canonical Hosts page (see
 *  `pages/Hosts.tsx`'s routing note) — real routes rather than redirects because
 *  Topbar's global search navigates to `/resources` carrying react-router state
 *  a redirect would drop. `/fleet` now lands on the Hosts page rather than a
 *  Rapid7 tab, since that tab is gone (see `RETIRED_SOURCES`). */
export const SECONDARY_ROUTES = ["/resources", "/fleet"];

/** Epitaphs. Every retired UI surface gets a row here, and this table is also
 *  the copy `pages/SourceUnavailable.tsx` renders — so the explanation an
 *  operator sees when they follow an old bookmark is the same text a developer
 *  finds when they go looking for the deleted page. One source, not two.
 *
 *  Each of these was verified to hold ZERO rows before deletion, and each is a
 *  `retired=True` collector, i.e. `source_visibility()` returns `hidden`. The
 *  backend collectors and their (empty) tables are untouched — retirement and
 *  revival are `AgentSpec.retired` + `COLLECTION_REVIVED_DOMAINS`, and this is
 *  only the UI reflecting that state. Set the revival flag and re-add a
 *  `NAV_ITEMS` line and the domain is back. */
export const RETIRED_SOURCES: Record<
  string,
  { title: string; domains: string[]; reason: string }
> = {
  "/vsphere": {
    title: "vSphere / Virtualization",
    domains: ["vsphere"],
    reason:
      "The vSphere collector is retired and every vsphere_* table is empty — there is no vCenter in this environment. pages/Vsphere.tsx was deleted along with this route's content.",
  },
  "/cloud": {
    title: "Cloud / Kubernetes / Network devices",
    domains: ["cloud", "k8s"],
    reason:
      "The cloud and k8s collectors are retired, and cloud_resources, k8s_* and net_devices are all empty. pages/Cloud.tsx was deleted. Live network data lives on Network Discovery instead.",
  },
  "/r7detail": {
    title: "Rapid7 CVE Detail",
    domains: ["vuln"],
    reason:
      "A Rapid7-specific CVE browser over r7_vulnerabilities, which is empty. pages/R7detail.tsx was deleted; Vulnerabilities (/vulns) is the CVE surface that remains.",
  },
};

/** Breadcrumb entries (`Topbar.tsx`'s `findBreadcrumb()`) for routes that render
 *  real content but have no sibling entry in `NAV_ITEMS`. Without this they fall
 *  through to the "Overview / Dashboard" fallback and show a wrong breadcrumb
 *  (TRK-237).
 *
 *  Retired routes get one too, so following an old bookmark lands on a page that
 *  is correctly labelled rather than pretending to be the Dashboard. */
export const BREADCRUMB_OVERRIDES: Record<string, { group: string; label: string }> = {
  "/resources": { group: "Infrastructure", label: "Hosts" },
  "/fleet": { group: "Infrastructure", label: "Hosts" },
  "/vsphere": { group: "Infrastructure", label: "vSphere (not configured)" },
  "/cloud": { group: "Infrastructure", label: "Cloud / K8s (not configured)" },
  "/r7detail": { group: "Security & Compliance", label: "CVE Detail (not configured)" },
};
