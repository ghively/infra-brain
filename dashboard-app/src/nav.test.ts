/** The nav registry's grouping/visibility logic.
 *
 *  Uses INVENTED nav items and INVENTED domain names throughout. `nav.ts`'s real
 *  `NAV_ITEMS` is data that will change every time a page is added; the thing
 *  worth locking in is the rule that turns items + inventory into sections. Two
 *  tests at the end do assert facts about the real list, but only structural
 *  ones (no dangling group, no duplicate path) that must stay true forever.
 */
import { describe, expect, it } from "vitest";

import {
  NAV_GROUPS,
  NAV_ITEMS,
  RETIRED_SOURCES,
  buildNavSections,
  resolveNavItem,
} from "./nav";
import type { NavItem, VisibilityMap } from "./nav";

const item = (over: Partial<NavItem> = {}): NavItem => ({
  label: "Test Page",
  path: "/test",
  group: "operations",
  ...over,
});

describe("resolveNavItem — one item against the source inventory", () => {
  it("always shows an item that names no domain (app features, not data views)", () => {
    const resolved = resolveNavItem(item(), { anything: "hidden" });
    expect(resolved).not.toBeNull();
    expect(resolved!.historical).toBe(false);
  });

  it("shows an item whose domain is visible", () => {
    expect(resolveNavItem(item({ domains: ["flux"] }), { flux: "visible" })).not.toBeNull();
  });

  it("marks an item historical when its only domain is historical", () => {
    const resolved = resolveNavItem(item({ domains: ["flux"] }), { flux: "historical" });
    expect(resolved).not.toBeNull();
    expect(resolved!.historical).toBe(true);
  });

  it("hides an item whose only domain is hidden", () => {
    expect(resolveNavItem(item({ domains: ["flux"] }), { flux: "hidden" })).toBeNull();
  });

  it("keeps a multi-domain item when ANY of its domains is still visible", () => {
    // The real case: Security reads wazuh + pki + secrets_inventory. Retiring
    // one of those must not remove the page.
    const resolved = resolveNavItem(item({ domains: ["dead", "alive"] }), {
      dead: "hidden",
      alive: "visible",
    });
    expect(resolved).not.toBeNull();
    expect(resolved!.historical).toBe(false);
  });

  it("hides a multi-domain item only when EVERY domain is hidden", () => {
    expect(
      resolveNavItem(item({ domains: ["a", "b"] }), { a: "hidden", b: "hidden" }),
    ).toBeNull();
  });

  it("prefers historical over hidden when domains disagree", () => {
    const resolved = resolveNavItem(item({ domains: ["a", "b"] }), {
      a: "hidden",
      b: "historical",
    });
    expect(resolved!.historical).toBe(true);
  });

  it("treats an unreported domain as visible, never as hidden", () => {
    // A brand-new collector, or the window before the /sources fetch resolves.
    // Defaulting the other way would make pages blink out of the nav.
    expect(resolveNavItem(item({ domains: ["never_heard_of_it"] }), {})).not.toBeNull();
  });
});

describe("buildNavSections — grouping", () => {
  const items: NavItem[] = [
    item({ label: "A", path: "/a", group: "overview" }),
    item({ label: "B", path: "/b", group: "system" }),
    item({ label: "C", path: "/c", group: "overview" }),
  ];

  it("groups items under their group's heading", () => {
    const sections = buildNavSections(items, {});
    expect(sections.map((s) => s.heading)).toEqual(["Overview", "System"]);
    expect(sections[0].items.map((i) => i.label)).toEqual(["A", "C"]);
  });

  it("orders sections by NAV_GROUPS, not by item order", () => {
    const reversed = [items[1], items[0]];
    expect(buildNavSections(reversed, {}).map((s) => s.id)).toEqual(["overview", "system"]);
  });

  it("preserves item order within a group", () => {
    expect(buildNavSections(items, {})[0].items.map((i) => i.label)).toEqual(["A", "C"]);
  });

  it("drops a group entirely once all its items are hidden", () => {
    const withDead = [
      item({ label: "A", path: "/a", group: "overview" }),
      item({ label: "Dead", path: "/dead", group: "system", domains: ["gone"] }),
    ];
    const sections = buildNavSections(withDead, { gone: "hidden" });
    expect(sections.map((s) => s.id)).toEqual(["overview"]);
  });

  it("renders the full nav when the inventory is empty (failed or pending fetch)", () => {
    const withDomains = [item({ label: "X", path: "/x", domains: ["unknown"] })];
    expect(buildNavSections(withDomains, {})[0].items).toHaveLength(1);
  });

  it("defaults to the real NAV_ITEMS when called with no arguments", () => {
    expect(buildNavSections().length).toBeGreaterThan(0);
  });
});

describe("NAV_ITEMS — structural invariants of the real registry", () => {
  it("has no duplicate paths", () => {
    const paths = NAV_ITEMS.map((i) => i.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it("assigns every item to a group that NAV_GROUPS declares", () => {
    const known = new Set(NAV_GROUPS.map((g) => g.id));
    for (const i of NAV_ITEMS) expect(known.has(i.group)).toBe(true);
  });

  it("offers no nav entry for any retired source", () => {
    // The owner's actual complaint: "there are still old data sources in the
    // UI... I want to remove rapid 7". This is the guard against them coming
    // back via a copy-paste.
    const paths = new Set(NAV_ITEMS.map((i) => i.path));
    for (const retired of Object.keys(RETIRED_SOURCES)) {
      expect(paths.has(retired)).toBe(false);
    }
  });

  it("names no retired collector domain in any nav item", () => {
    const retiredDomains = new Set(
      Object.values(RETIRED_SOURCES).flatMap((entry) => entry.domains),
    );
    for (const i of NAV_ITEMS) {
      for (const d of i.domains ?? []) expect(retiredDomains.has(d)).toBe(false);
    }
  });

  it("hides the Vulnerabilities entry if vuln_triage is ever retired and emptied", () => {
    // Proves the real registry is genuinely wired to the inventory, not just
    // the synthetic items above.
    const visibility: VisibilityMap = { vuln_triage: "hidden" };
    const paths = buildNavSections(NAV_ITEMS, visibility)
      .flatMap((s) => s.items)
      .map((i) => i.path);
    expect(paths).not.toContain("/vulns");
    expect(paths).toContain("/hosts"); // unrelated entries unaffected
  });
});
