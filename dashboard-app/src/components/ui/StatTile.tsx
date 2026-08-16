import type { ReactNode } from "react";
import "./ui.css";

export type StatColor = "red" | "yellow" | "green" | "blue" | undefined;

export interface StatTileProps {
  label: string;
  value: ReactNode;
  unit?: string;
  color?: StatColor;
  sub?: ReactNode;
  /** Renders at the larger "hero" size (42px) used by the first column of a
   *  SummaryBlock; standalone StatTile usage normally omits this. */
  hero?: boolean;
  className?: string;
}

const colorClass = (color: StatColor) => (color ? `c-${color}` : undefined);

/** A single stat cell — reusable standalone (`<StatTile>`) or as a building
 *  block inside `<SummaryBlock>`. */
export function StatTile({ label, value, unit, color, sub, hero, className }: StatTileProps) {
  return (
    <div className={["stat-tile", className].filter(Boolean).join(" ")}>
      <div className="lbl">{label}</div>
      <div className={["val", hero ? "hero-val" : undefined, colorClass(color)].filter(Boolean).join(" ")}>
        {value}
        {unit ? <span className="sub"> {unit}</span> : null}
      </div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  );
}

export interface SummaryHero {
  label: string;
  value: ReactNode;
  unit?: string;
  sub?: ReactNode;
}

export interface SummaryStat {
  label: string;
  value: ReactNode;
  color?: StatColor;
  sub?: ReactNode;
}

export interface HealthSegment {
  color: StatColor;
  /** Fraction of the bar this segment fills, 0–1. Segments are rendered in
   *  array order left-to-right; the caller is responsible for the values
   *  summing to <= 1 (any remainder is left as background `--border`). */
  fraction: number;
  label?: string;
}

export interface SummaryBlockProps {
  hero: SummaryHero;
  stats: SummaryStat[];
  /** Optional segmented health bar rendered under the hero value. */
  health?: HealthSegment[];
  className?: string;
}

/** 5-column hero+4-stat grid (1.6fr hero + 4x1fr stat columns), reflowing to
 *  2-col then 1-col on narrow viewports. Port of the approved round-4 mockup
 *  `.summary-block` pattern. */
export function SummaryBlock({ hero, stats, health, className }: SummaryBlockProps) {
  return (
    <div className={["summary-block", className].filter(Boolean).join(" ")}>
      <div>
        <div className="lbl">{hero.label}</div>
        <div className="val hero-val">
          {hero.value}
          {hero.unit ? <span className="sub"> {hero.unit}</span> : null}
        </div>
        {hero.sub ? <div className="sub">{hero.sub}</div> : null}
        {health && health.length > 0 ? (
          <div className="healthbar" role="img" aria-label="Health breakdown">
            {health.map((seg, i) => (
              <span
                key={i}
                style={{
                  width: `${Math.max(0, Math.min(1, seg.fraction)) * 100}%`,
                  background: seg.color ? `var(--${seg.color})` : "var(--faint)",
                }}
                title={seg.label}
              />
            ))}
          </div>
        ) : null}
      </div>
      {stats.map((s, i) => (
        <div key={i}>
          <div className="lbl">{s.label}</div>
          <div className={["val", colorClass(s.color)].filter(Boolean).join(" ")}>{s.value}</div>
          {s.sub ? <div className="sub">{s.sub}</div> : null}
        </div>
      ))}
    </div>
  );
}
