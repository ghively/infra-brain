import "./graphicTokens.css";

/** One period's worth of data: `total` is the full bar height, `subset` is a
 *  highlighted portion of it (e.g. critical events out of all drift events).
 *  `subset` must be <= `total` — values above `total` are clamped when drawn. */
export type TrendBarDatum = {
  label: string;
  total: number;
  subset: number;
};

export type TrendBarChartProps = {
  /** N periods, oldest → newest. */
  data: TrendBarDatum[];
  /** Fill color for the non-subset (remainder) portion of each bar. */
  totalColor: string;
  /** Fill color for the subset (highlighted) portion of each bar. */
  subsetColor: string;
  /** Legend/aria copy — kept generic so this is reusable beyond drift/critical. */
  legend: { totalLabel: string; subsetLabel: string };
  /** Index of the "current" period, outlined per the mockup's `.b.cur`.
   *  Defaults to the last item (most recent period). */
  currentIndex?: number;
  /** Pixel height of the plot area, excluding the x-axis label row. */
  height?: number;
};

/** Generic stacked bar chart: N periods, 2 stacked series per bar. Heights are
 *  computed proportionally from `data` (never hardcoded), scaled so the
 *  tallest `total` in the series nearly fills `height`. */
export function TrendBarChart({
  data,
  totalColor,
  subsetColor,
  legend,
  currentIndex,
  height = 120,
}: TrendBarChartProps) {
  const curIdx = currentIndex ?? (data.length > 0 ? data.length - 1 : -1);
  const maxTotal = Math.max(1, ...data.map((d) => d.total));
  const scale = (height - 4) / maxTotal;

  const ariaLabel =
    data.length === 0
      ? `${legend.totalLabel} trend: no data`
      : `${legend.totalLabel} trend over ${data.length} periods, with ${legend.subsetLabel} highlighted: ` +
        data
          .map(
            (d) =>
              `${d.label}: ${d.total} ${legend.totalLabel.toLowerCase()}, ${d.subset} ${legend.subsetLabel.toLowerCase()}`,
          )
          .join("; ");

  return (
    <div className="gw-scope">
      <div role="img" aria-label={ariaLabel}>
        <div
          className="bars"
          style={{
            display: "flex",
            alignItems: "flex-end",
            gap: 6,
            height,
            borderBottom: "1px solid var(--gw-border)",
          }}
        >
          {data.map((d, i) => {
            const totalPx = Math.round(Math.max(0, d.total) * scale);
            const subsetPx = Math.min(totalPx, Math.round(Math.max(0, d.subset) * scale));
            const restPx = Math.max(0, totalPx - subsetPx);
            const isCurrent = i === curIdx;
            return (
              <div
                key={d.label}
                data-testid="bar"
                className={`b${isCurrent ? " cur" : ""}`}
                style={{
                  flex: 1,
                  display: "flex",
                  flexDirection: "column-reverse",
                  gap: 1,
                  minWidth: 0,
                }}
              >
                <i
                  data-testid="seg-subset"
                  className="seg-subset"
                  style={{
                    display: "block",
                    height: subsetPx,
                    background: subsetColor,
                    borderRadius: "1.5px 1.5px 0 0",
                    outline: isCurrent ? "1px solid var(--gw-text)" : undefined,
                  }}
                />
                <i
                  data-testid="seg-total"
                  className="seg-total"
                  style={{
                    display: "block",
                    height: restPx,
                    background: totalColor,
                    borderRadius: "1.5px 1.5px 0 0",
                  }}
                />
              </div>
            );
          })}
        </div>
        <div
          className="bar-x"
          style={{ display: "flex", gap: 6, fontSize: 8.5, color: "var(--gw-faint)", marginTop: 5 }}
        >
          {data.map((d) => (
            <span key={d.label} style={{ flex: 1, textAlign: "center", minWidth: 0 }}>
              {d.label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
