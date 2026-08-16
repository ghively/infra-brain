import type { JSX } from "react";

/**
 * Inline-SVG icon set ported VERBATIM from the v1 legacy dashboard
 * (`src/infra_brain/dashboard/static/index.html`, the `ICONS` map at
 * L5302-5362 — the canonical copy, not the `<symbol>` sprite at L33-95).
 *
 * Every value is the path `d` string exactly as it appears in v1. The `Icon`
 * component renders these with the same conventions v1's `svgIcon()` helper
 * used (L5829): `viewBox="0 0 24 24"`, `fill="none"`, `stroke="currentColor"`,
 * `stroke-width="1.8"`, round linecap/linejoin — so icons look identical.
 *
 * Offline / CSP-safe: React-only, no CDN, no external assets, no emoji.
 */
export const ICON_PATHS: Record<string, string> = {
  home: "M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M9 21V9h6v12",
  resources: "M2 20h20 M5 20V8l7-6 7 6v12 M10 20v-5h4v5",
  drift: "M22 12h-4l-3 9L9 3l-3 9H2",
  collruns: "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0 1 14.85-3.36L23 10 M1 14l4.64 4.36A9 9 0 0 0 20.49 15",
  scan: "M12 2a10 10 0 1 0 0 20A10 10 0 0 0 12 2z M12 6v6l4 2",
  netscan: "M12 2v4 M12 18v4 M4.93 4.93l2.83 2.83 M16.24 16.24l2.83 2.83 M2 12h4 M18 12h4 M4.93 19.07l2.83-2.83 M16.24 7.76l2.83-2.83 M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
  intprops:
    "M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71 M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71",
  instincts: "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
  activity: "M4 17l6-6-6-6 M12 19h8",
  scripts: "M16 18l6-6-6-6 M8 6l-6 6 6 6",
  docs: "M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z M14 2v6h6 M8 13h8 M8 17h8 M8 9h2",
  decisions: "M6 3v12 M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M18 9a9 9 0 0 1-12 8.485",
  bell: "M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9 M13.73 21a2 2 0 0 1-3.46 0",
  brain: "M12 2a10 10 0 1 0 0 20A10 10 0 0 0 12 2z M8 14s1.5 2 4 2 4-2 4-2 M9 9h.01 M15 9h.01",
  box: "M21 8v13H3V8 M1 3h22v5H1z M10 12h4",
  settings:
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z",
  observations: "M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.35-4.35",
  agents: "M9 3H4v5 M20 8V3h-5 M4 16v5h5 M15 21h5v-5 M9 9h6v6H9z",
  notify: "M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9 M13.73 21a2 2 0 0 1-3.46 0",
  vulns: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z M12 8v4 M12 16h.01",
  eolcal: "M8 2v4 M16 2v4 M3 10h18 M5 6h14a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2z M9 16l6-4 M15 16l-6-4",
  security: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z M9 12l2 2 4-4",
  lock: "M5 11h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2z M7 11V7a5 5 0 0 1 10 0v4",
  invrec: "M4 5h16v6H4z M4 13h16v6H4z M8 8h.01 M8 16h.01",
  remed: "M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.1 2.1-2.4-.6-.6-2.4z",
  compl: "M9 4h6a1 1 0 0 1 1 1v0h2a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2 M9 14l2 2 4-4",
  octopus: "M12 2a6 6 0 0 0-6 6v3l-2 3v2h4 M18 11V8a6 6 0 0 0-6-6 M20 14v2h-4 M6 16l-1 4 M10 16v4 M14 16v4 M18 16l1 4",
  iac: "M16 18l6-6-6-6 M8 6l-6 6 6 6 M14 4l-4 16",
  vsphere: "M2 5h20v6H2z M2 13h20v6H2z M6 8h.01 M6 16h.01 M10 8h8 M10 16h8",
  cloud: "M17.5 19a4.5 4.5 0 0 0 0-9 6 6 0 0 0-11.5 2A4 4 0 0 0 6 19h11.5z",
  sparkles:
    "M12 3l1.2 3.6 3.8.4-2.8 2.6.8 3.8L12 11.5l-3 1.9.8-3.8L7 7l3.8-.4z M5 17l.6 1.8 1.9.2-1.4 1.3.4 1.9L5 21.2l-1.5.9.4-1.9L2.5 19l1.9-.2z M20 17l.6 1.8 1.9.2-1.4 1.3.4 1.9L20 21.2l-1.5.9.4-1.9-1.4-1.3 1.9-.2z",
  // TRK-238 item 1: Host Purpose Map (/host-purpose-map) was the only nav
  // route with no icon at all — this is a standard "tag/label" glyph (an
  // outlined price-tag with a small punch-hole dot, same "M...z M<dot>.01"
  // shape convention as `eolcal`/`box` above), thematically apt for a
  // curated host-purpose/ownership label map.
  tag: "M20.59 13.41L11 3.83A2 2 0 0 0 9.59 3H4a1 1 0 0 0-1 1v5.59a2 2 0 0 0 .59 1.41l9.58 9.58a2 2 0 0 0 2.83 0l4.59-4.59a2 2 0 0 0 0-2.83z M7 7h.01",
};

/** Fallback icon name used when a requested `name` is unknown. */
const FALLBACK = "box";

/**
 * Inline SVG icon by v1 name. Matches v1 `svgIcon()` stroke/viewBox conventions
 * so icons render identically to the legacy dashboard.
 *
 * The `graph` (Knowledge Graph) icon is a special case: v1 draws it with
 * circles + lines rather than a single `<path d>`, so it is rendered inline
 * here and is addressable by `name="graph"` just like any path icon.
 *
 * Unknown names fall back to the `box` icon (never throws).
 */
export function Icon({
  name,
  size = 18,
  className,
}: {
  name: string;
  size?: number;
  className?: string;
}): JSX.Element {
  const common = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    className,
    style: { width: size, height: size, flexShrink: 0 },
  };

  if (name === "graph") {
    return (
      <svg {...common}>
        <circle cx="18" cy="5" r="3" />
        <circle cx="6" cy="19" r="3" />
        <circle cx="3" cy="8" r="3" />
        <line x1="15.5" y1="7" x2="8.5" y2="11" />
        <line x1="6" y1="16" x2="4" y2="11" />
        <line x1="15" y1="18" x2="8.5" y2="11" />
      </svg>
    );
  }

  const d = ICON_PATHS[name] ?? ICON_PATHS[FALLBACK];
  return (
    <svg {...common}>
      <path d={d} />
    </svg>
  );
}
