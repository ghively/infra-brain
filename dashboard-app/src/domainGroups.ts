/** Heuristic grouping for the sweep-domain picker (MR-I usability nit).
 *
 * ScanSchedule.tsx's "Manual Sweep" domain <select> lists all ~23 collection
 * domains flat with no grouping, and the domain set is backend-defined
 * (returned by GET /api/dashboard/scan_points, not hardcoded here), so this
 * is a pure display-layer classifier applied to whatever domain strings come
 * back — not a hardcoded domain list. Categories mirror the Security /
 * Inventory / Deploy / Operations / Agents groupings used in nav.ts so the
 * picker reads consistently with the sidebar.
 */
export function domainGroup(domain: string): string {
  const d = domain.toLowerCase();
  if (/(vuln|cve|compliance|eol|security)/.test(d)) return "Security";
  if (/(linux|windows|vsphere|net|cloud|discovery|host|inventory)/.test(d)) return "Inventory";
  if (/(octopus|cicd|iac|deploy)/.test(d)) return "Deploy";
  if (/(drift|remediation|rootcause|fleet|health|retention|pulse|query)/.test(d)) return "Operations";
  if (/(learning|triage|instinct|observation)/.test(d)) return "Agents & Intelligence";
  return "Other";
}

/** Groups a flat domain list into `{ group: [domains...] }`, preserving each
 * group's first-seen order and sorting domains alphabetically within it. */
export function groupDomains(domains: string[]): Array<[string, string[]]> {
  const byGroup = new Map<string, string[]>();
  for (const d of domains) {
    const g = domainGroup(d);
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g)!.push(d);
  }
  for (const list of byGroup.values()) list.sort();
  return Array.from(byGroup.entries());
}
