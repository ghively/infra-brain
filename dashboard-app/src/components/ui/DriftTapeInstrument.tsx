import "./graphicTokens.css";

/** One colored threshold band on the tape, e.g. {color:"red", from:80, to:100}
 *  meaning "values from 80 up to 100 render as the red band". Bands may
 *  overlap the visible [min,max] range partially or not at all — anything
 *  outside [min,max] is clipped. */
export type DriftTapeZone = {
  color: "red" | "yellow" | "green" | "blue";
  from: number;
  to: number;
};

export type DriftTapeInstrumentProps = {
  /** Instrument label, e.g. "Open Drift Events". */
  label: string;
  /** Current reading — where the "now" line sits. */
  value: number;
  /** Visible scale bounds. `min` renders at the bottom, `max` at the top. */
  min: number;
  max: number;
  /** Threshold bands (red/yellow/green/blue) drawn behind the ticks. */
  zones: DriftTapeZone[];
  /** Sweep history, oldest → newest. The last two points (if present) drive
   *  the trend vector; a single point (or none) renders no trend line. */
  history?: number[];
  /** Optional unit suffix for tick/aria text, e.g. "%" or " events". */
  unit?: string;
  /** Roughly how many gridline ticks to draw. Actual count is "nice-rounded". */
  tickCount?: number;
};

const TAPE_HEIGHT = 236;

/** Round to 2 decimal places — avoids binary floating-point artifacts (e.g.
 *  141.60000000000002) leaking into rendered pixel values. */
function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

/** Pick a "nice" step (1/2/5 × a power of 10) so ~targetCount ticks span
 *  [min,max] on human-readable boundaries, mirroring the classic d3-style
 *  nice-tick algorithm without pulling in a dependency. */
function niceStep(min: number, max: number, targetCount: number): number {
  const span = Math.max(max - min, 1e-9);
  const rough = span / Math.max(targetCount, 1);
  const pow10 = 10 ** Math.floor(Math.log10(rough));
  const candidates = [1, 2, 5, 10];
  let best = candidates[0] * pow10;
  for (const c of candidates) {
    const step = c * pow10;
    if (Math.abs(span / step - targetCount) < Math.abs(span / best - targetCount)) {
      best = step;
    }
  }
  return best;
}

/** Tick values covering [min,max], snapped to the computed nice step. */
function computeTicks(min: number, max: number, targetCount: number): number[] {
  if (max <= min) return [min];
  const step = niceStep(min, max, targetCount);
  const first = Math.ceil(min / step) * step;
  const ticks: number[] = [];
  for (let v = first; v <= max + step * 1e-6; v += step) {
    ticks.push(Math.round(v / step) * step);
  }
  if (ticks.length === 0) ticks.push(min);
  return ticks;
}

/** Map a data value to a y-offset (px from the top of the tape). Clamped to
 *  the visible [0, TAPE_HEIGHT] range — values outside [min,max] pin to an edge. */
function valueToY(v: number, min: number, max: number): number {
  const span = max - min;
  if (span <= 0) return TAPE_HEIGHT / 2;
  const frac = (v - min) / span;
  const clamped = Math.min(1, Math.max(0, frac));
  return round2(TAPE_HEIGHT - clamped * TAPE_HEIGHT);
}

const ZONE_BG: Record<DriftTapeZone["color"], string> = {
  red: "rgba(240,101,78,.16)",
  yellow: "rgba(217,166,46,.13)",
  green: "rgba(76,187,108,.11)",
  blue: "rgba(91,157,232,.09)",
};

const ZONE_LINE: Record<DriftTapeZone["color"], string> = {
  red: "var(--gw-red)",
  yellow: "var(--gw-yellow)",
  green: "var(--gw-green)",
  blue: "var(--gw-blue)",
};

const ZONE_WORD: Record<DriftTapeZone["color"], string> = {
  red: "critical",
  yellow: "warning",
  green: "healthy",
  blue: "informational",
};

/** Cockpit-tape-style trend instrument. Renders a vertical scale from `min`
 *  (bottom) to `max` (top), colored threshold bands, computed tick marks, a
 *  "now" marker at `value`, and — when `history` has ≥2 points — a trend
 *  vector line between the previous and current reading. Every visual
 *  element is derived from props; nothing is a hardcoded pixel offset. */
export function DriftTapeInstrument({
  label,
  value,
  min,
  max,
  zones,
  history,
  unit = "",
  tickCount = 5,
}: DriftTapeInstrumentProps) {
  const ticks = computeTicks(min, max, tickCount);
  const nowY = valueToY(value, min, max);

  // Trend vector spans the last two `history` points, per this component's
  // contract — not `value`, which may not equal history[-1] for a caller
  // that passes a strictly-prior-readings history array.
  const hasTrend = history !== undefined && history.length >= 2;
  const previous = hasTrend ? history[history.length - 2] : undefined;
  const latest = hasTrend ? history[history.length - 1] : undefined;
  const prevY = previous !== undefined ? valueToY(previous, min, max) : undefined;
  const latestY = latest !== undefined ? valueToY(latest, min, max) : undefined;

  const activeZone = zones.find((z) => {
    const lo = Math.min(z.from, z.to);
    const hi = Math.max(z.from, z.to);
    return value >= lo && value <= hi;
  });

  let trendWord = "steady";
  if (previous !== undefined && latest !== undefined) {
    if (latest > previous) trendWord = "rising";
    else if (latest < previous) trendWord = "falling";
  }

  const zoneWord = activeZone ? ZONE_WORD[activeZone.color] : "out of range";
  const ariaLabel = `${label}: ${value}${unit}, ${zoneWord}${
    previous !== undefined ? `, ${trendWord} from ${previous}${unit}` : ""
  }`;

  return (
    <div className="gw-scope">
      <div
        className="tape"
        role="img"
        aria-label={ariaLabel}
        style={{
          position: "relative",
          width: 74,
          height: TAPE_HEIGHT,
          flexShrink: 0,
          border: "1px solid var(--gw-border)",
          borderRadius: 4,
          overflow: "hidden",
          background: "#1e2126",
        }}
      >
        {zones.map((z, i) => {
          const lo = Math.min(z.from, z.to);
          const hi = Math.max(z.from, z.to);
          const top = valueToY(hi, min, max);
          const bottom = valueToY(lo, min, max);
          const height = round2(Math.max(0, bottom - top));
          if (height <= 0) return null;
          return (
            <div
              key={`band-${z.color}-${i}`}
              data-testid={`band-${z.color}`}
              className={`band band-${z.color}`}
              style={{
                position: "absolute",
                left: 0,
                right: 0,
                top,
                height,
                background: ZONE_BG[z.color],
              }}
            />
          );
        })}

        {ticks.map((t) => {
          const y = valueToY(t, min, max);
          return (
            <div
              key={`tick-${t}`}
              data-testid="tick"
              data-value={t}
              className="tick"
              style={{
                position: "absolute",
                left: 0,
                right: 0,
                top: y,
                borderTop: "1px solid var(--gw-grid)",
              }}
            >
              <span
                style={{
                  position: "absolute",
                  right: 4,
                  top: -8,
                  fontSize: 9,
                  color: "var(--gw-faint)",
                }}
              >
                {t}
                {unit}
              </span>
            </div>
          );
        })}

        {prevY !== undefined && latestY !== undefined && (
          <div
            data-testid="trend"
            className="trend"
            style={{
              position: "absolute",
              left: "50%",
              top: Math.min(latestY, prevY),
              width: 2,
              height: Math.max(1, round2(Math.abs(latestY - prevY))),
              background:
                activeZone !== undefined ? ZONE_LINE[activeZone.color] : "var(--gw-red)",
            }}
          />
        )}

        <div
          data-testid="nowline"
          className="nowline"
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            top: nowY,
            borderTop: "2px solid var(--gw-text)",
          }}
        >
          <span
            aria-hidden="true"
            style={{ position: "absolute", left: 2, top: -8, fontSize: 9, color: "var(--gw-text)" }}
          >
            ▶
          </span>
        </div>
      </div>
    </div>
  );
}
