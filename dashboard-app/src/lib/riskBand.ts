/** Host risk-score → coarse band.
 *
 * KEPT, though its original second caller is gone. This was "shared by Hosts.tsx
 * and Fleet.tsx"; `pages/hosts/FleetTab.tsx` was deleted with the rest of the
 * Rapid7 surface (retired collector, `r7_assets` empty — see RETIRED_SOURCES in
 * src/nav.ts). `pages/hosts/UnifiedHostsTab.tsx` still renders `risk_score` on
 * the canonical Hosts page and still needs this mapping, and `lib/tokens.ts`
 * documents an interop contract with it, so it is a live shared helper rather
 * than Rapid7-only dead code.
 *
 * Source of truth: the backend's `_risk_band` (src/infra_brain/api/routers/
 * fleet.py) — low <1000, medium <10000, high <25000, critical >=25000.
 *
 * Before this mapping existed, Hosts.tsx computed its own risk color
 * independently (green at 0, amber <500, red >=500, with no "high"/"critical"
 * distinction at all), so the identical risk_score could render red on one page
 * and green/"low" on another, one click apart (2026-07-24 overnight audit
 * finding). Anything colouring a risk score derives it from here instead of
 * inventing thresholds a second time.
 */
export type RiskBand = "low" | "medium" | "high" | "critical";

export function riskBandFor(score: number | null | undefined): RiskBand {
  const s = score || 0;
  if (s >= 25000) return "critical";
  if (s >= 10000) return "high";
  if (s >= 1000) return "medium";
  return "low";
}

/* REMOVED: `RISK_BAND_STYLE` and `riskColorFor`.
 *
 * Both were pre-redesign hex literals predating `lib/tokens.ts`, carrying an
 * explicit "@deprecated … delete this export once no caller remains" note since
 * 2026-07-27. Their last caller was `pages/hosts/FleetTab.tsx`, deleted with the
 * rest of the Rapid7 surface (retired collector, `r7_assets` empty — see
 * RETIRED_SOURCES in src/nav.ts), so that condition is now met.
 *
 * The replacement was already named in that note: `RiskBand` is deliberately the
 * same four strings as the design system's `Severity`, so feed a band straight
 * to `<SeverityPill severity={band}>` or `severityColor(band)` from
 * `lib/tokens.ts`. `riskBandFor` above (the threshold logic mirroring the
 * backend's `_risk_band`) is the part worth keeping and is unchanged. */
