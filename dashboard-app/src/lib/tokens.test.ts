/** Tokens are the design system's contract (see dashboard-app/DESIGN.md).
 * These tests pin the exact values and the severity/status/rail mappings so a
 * later "small tweak" to a hue or a threshold can't silently change what
 * CRITICAL looks like across every page at once. */
import { describe, it, expect } from "vitest";
import * as T from "./tokens";
import { riskBandFor, type RiskBand } from "./riskBand";

describe("tokens — palette values", () => {
  it("exports the approved surface + text + hue values exactly", () => {
    expect({
      BG: T.BG,
      PANEL: T.PANEL,
      RAISED: T.RAISED,
      BORDER: T.BORDER,
      GRID: T.GRID,
      TEXT: T.TEXT,
      MUTED: T.MUTED,
      FAINT: T.FAINT,
      RED: T.RED,
      YELLOW: T.YELLOW,
      GREEN: T.GREEN,
      BLUE: T.BLUE,
      RED_DEEP: T.RED_DEEP,
      RED_MID: T.RED_MID,
    }).toEqual({
      BG: "#1A1C21",
      PANEL: "#22252C",
      RAISED: "#262A32",
      BORDER: "#32363F",
      GRID: "#2C3038",
      TEXT: "#D8DBE2",
      MUTED: "#8A8FA0",
      FAINT: "#6A6F7D",
      RED: "#F0654E",
      YELLOW: "#D9A62E",
      GREEN: "#4CBB6C",
      BLUE: "#5B9DE8",
      RED_DEEP: "#8B2615",
      RED_MID: "#C0341D",
    });
  });

  it("uses radius 8 for panels (superseding the old 14) and 3 for small chrome", () => {
    expect(T.RADIUS).toBe(8);
    expect(T.RADIUS_SM).toBe(3);
    expect(T.RAIL_WIDTH).toBe(4);
  });

  it("keeps deprecated aliases pointing at new-system values", () => {
    // Unmigrated callers (Topbar/PageHeader/Home) still import these.
    expect(T.ACCENT).toBe(T.BLUE);
    expect(T.CARD_BG).toBe(T.PANEL);
    expect(T.CARD_BORDER).toBe(T.BORDER);
  });

  it("has no fifth brand accent — the retired indigo appears nowhere", () => {
    const values = (Object.values(T) as unknown[]).filter(
      (v): v is string => typeof v === "string",
    );
    expect(values.map((v) => v.toLowerCase())).not.toContain("#6366f1");
  });

  it("references the vendored @font-face families, not a system-ui-only stack", () => {
    expect(T.FONT_MONO).toContain('"DM Mono"');
    expect(T.FONT_SANS).toContain('"DM Sans"');
  });
});

describe("tokens — hexA", () => {
  it("converts hex + alpha to rgba", () => {
    expect(T.hexA("#000000", 0.5)).toBe("rgba(0,0,0,0.5)");
    expect(T.hexA("#ffffff", 1)).toBe("rgba(255,255,255,1)");
    expect(T.hexA(T.BLUE, 0.12)).toBe("rgba(91,157,232,0.12)");
  });
});

describe("tokens — severity", () => {
  it("covers all four levels, worst-first", () => {
    expect(T.SEVERITY_ORDER).toEqual(["critical", "high", "medium", "low"]);
    expect(Object.keys(T.SEMANTIC).sort()).toEqual(["critical", "high", "low", "medium"]);
  });

  it("fills CRITICAL with red-deep and white text (the only filled level)", () => {
    expect(T.severityStyle("critical")).toEqual({
      fg: "#FFFFFF",
      bg: T.RED_DEEP,
      border: T.RED_DEEP,
    });
  });

  it("outlines HIGH with red-mid border and red text, never a fifth hue", () => {
    const s = T.severityStyle("high");
    expect(s.bg).toBe("transparent");
    expect(s.border).toBe(T.RED_MID);
    expect(s.fg).toBe(T.RED);
  });

  it("steps MEDIUM down to yellow and LOW down to neutral, both unfilled", () => {
    expect(T.severityStyle("medium").fg).toBe(T.YELLOW);
    expect(T.severityStyle("medium").bg).toBe("transparent");
    expect(T.severityStyle("low").fg).toBe(T.MUTED);
    expect(T.severityStyle("low").bg).toBe("transparent");
  });

  it("severityColor gives a usable accent hue for value coloring", () => {
    // critical's pill fg is white, which would be invisible as a value color —
    // severityColor deliberately returns the red hue instead.
    expect(T.severityColor("critical")).toBe(T.RED);
    expect(T.severityColor("high")).toBe(T.RED);
    expect(T.severityColor("medium")).toBe(T.YELLOW);
    expect(T.severityColor("low")).toBe(T.MUTED);
  });
});

describe("tokens — normalizeSeverity", () => {
  it("accepts canonical values, any case, with whitespace", () => {
    expect(T.normalizeSeverity("CRITICAL")).toBe("critical");
    expect(T.normalizeSeverity("  High ")).toBe("high");
    expect(T.normalizeSeverity("medium")).toBe("medium");
    expect(T.normalizeSeverity("Low")).toBe("low");
  });

  it("maps the aliases the API layer emits", () => {
    expect(T.normalizeSeverity("crit")).toBe("critical");
    expect(T.normalizeSeverity("moderate")).toBe("medium");
    expect(T.normalizeSeverity("warning")).toBe("medium");
    expect(T.normalizeSeverity("info")).toBe("low");
    expect(T.normalizeSeverity("none")).toBe("low");
  });

  it("returns null — not a reassuring 'low' — for missing/unknown input", () => {
    expect(T.normalizeSeverity(null)).toBeNull();
    expect(T.normalizeSeverity(undefined)).toBeNull();
    expect(T.normalizeSeverity("")).toBeNull();
    expect(T.normalizeSeverity("   ")).toBeNull();
    expect(T.normalizeSeverity("banana")).toBeNull();
  });
});

describe("tokens — worstSeverity", () => {
  it("picks the worst recognizable level regardless of order", () => {
    expect(T.worstSeverity(["low", "critical", "medium"])).toBe("critical");
    expect(T.worstSeverity(["low", "medium"])).toBe("medium");
    expect(T.worstSeverity(["high", "high"])).toBe("high");
  });

  it("ignores unrecognizable entries but still uses the recognizable ones", () => {
    expect(T.worstSeverity([null, "banana", "medium", undefined])).toBe("medium");
  });

  it("returns null for empty or wholly unrecognizable input", () => {
    expect(T.worstSeverity([])).toBeNull();
    expect(T.worstSeverity([null, undefined, ""])).toBeNull();
  });
});

describe("tokens — status", () => {
  it("maps the five statuses onto hues, with neutral off-hue", () => {
    expect(T.STATUS).toEqual({
      ok: T.GREEN,
      warn: T.YELLOW,
      error: T.RED,
      info: T.BLUE,
      neutral: T.MUTED,
    });
  });

  it("normalizeStatus never returns null — unknown is genuinely neutral", () => {
    expect(T.normalizeStatus("succeeded")).toBe("ok");
    expect(T.normalizeStatus("DEGRADED")).toBe("warn");
    expect(T.normalizeStatus("failed")).toBe("error");
    expect(T.normalizeStatus("running")).toBe("info");
    expect(T.normalizeStatus("banana")).toBe("neutral");
    expect(T.normalizeStatus(null)).toBe("neutral");
    expect(T.normalizeStatus(undefined)).toBe("neutral");
  });

  it("statusColor falls back to neutral, statusStyle tints the hue", () => {
    expect(T.statusColor("ok")).toBe(T.GREEN);
    expect(T.statusColor(null)).toBe(T.MUTED);
    expect(T.statusStyle("error")).toEqual({
      fg: T.RED,
      bg: T.hexA(T.RED, 0.12),
      border: T.hexA(T.RED, 0.32),
    });
  });
});

describe("tokens — panel rail", () => {
  it("exposes exactly the four rail tones", () => {
    expect(Object.keys(T.RAIL).sort()).toEqual(["blue", "green", "red", "yellow"]);
    expect(T.railColor("yellow")).toBe(T.YELLOW);
    expect(T.railColor(null)).toBe(T.BLUE);
  });

  it("collapses critical+high to red; low and no-findings both read green", () => {
    expect(T.severityRail("critical")).toBe("red");
    expect(T.severityRail("high")).toBe("red");
    expect(T.severityRail("medium")).toBe("yellow");
    expect(T.severityRail("low")).toBe("green");
    expect(T.severityRail(null)).toBe("green");
  });

  it("maps health-keyed panels through statusRail, unknown → blue", () => {
    expect(T.statusRail("ok")).toBe("green");
    expect(T.statusRail("warn")).toBe("yellow");
    expect(T.statusRail("error")).toBe("red");
    expect(T.statusRail("info")).toBe("blue");
    expect(T.statusRail(null)).toBe("blue");
  });
});

describe("tokens — DataTable contracts", () => {
  it("defines the three densities in toolbar order, defaulting to cozy", () => {
    expect(T.DENSITIES).toEqual(["compact", "cozy", "tall"]);
    expect(T.DEFAULT_DENSITY).toBe("cozy");
    expect(T.DENSITY_PADDING.compact).toBeLessThan(T.DENSITY_PADDING.cozy);
    expect(T.DENSITY_PADDING.cozy).toBeLessThan(T.DENSITY_PADDING.tall);
  });

  it("defines the full type-glyph legend", () => {
    expect(T.TYPE_GLYPH).toEqual({ pk: "PK", text: "T", num: "#", enum: "◆", fk: "⇥" });
  });

  it("renders missing values as a NULL token, never as a blank", () => {
    expect(T.NULL_TOKEN).toBe("NULL");
    expect(T.NULL_TOKEN).not.toBe("");
  });
});

describe("tokens — interop with riskBand", () => {
  it("accepts a RiskBand directly as a Severity (identical unions)", () => {
    const band: RiskBand = riskBandFor(30000);
    const sev: T.Severity = band;
    expect(sev).toBe("critical");
    expect(T.severityStyle(band).bg).toBe(T.RED_DEEP);
    expect(T.severityRail(riskBandFor(0))).toBe("green");
  });
});
