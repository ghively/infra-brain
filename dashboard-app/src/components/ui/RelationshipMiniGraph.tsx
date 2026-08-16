import "./graphicTokens.css";

export type RelationshipNode = { id: string; label: string };

export type RelationshipSatellite = RelationshipNode & { flagged?: boolean };

export type RelationshipEdge = {
  fromId: string;
  toId: string;
  style: "normal" | "flagged";
  /** e.g. "HOSTED_ON", "AFFECTED_BY_CVE" — a `get_blast_radius`-style edge_type. */
  edgeType: string;
};

export type RelationshipMiniGraphProps = {
  hub: RelationshipNode;
  satellites: RelationshipSatellite[];
  edges: RelationshipEdge[];
  width?: number;
  height?: number;
};

const VIEW_W = 288;
const VIEW_H = 190;
const HUB_R = 11;
const SAT_R = 4;
const SAT_R_FLAGGED = 5.5;
const RING_RADIUS = 68;

type LaidOutSatellite = RelationshipSatellite & { x: number; y: number };

/** Evenly space `n` satellites on a circle of `radius` around (cx,cy),
 *  starting at 12 o'clock and going clockwise. Works for any N >= 0 — the
 *  mockup hand-placed exactly 10 positions; this computes them for any count. */
function layoutSatellites(
  satellites: RelationshipSatellite[],
  cx: number,
  cy: number,
  radius: number,
): LaidOutSatellite[] {
  const n = satellites.length;
  if (n === 0) return [];
  return satellites.map((s, i) => {
    const angle = -Math.PI / 2 + (i * (2 * Math.PI)) / n;
    return { ...s, x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) };
  });
}

/** Text-anchor + dy chosen from a satellite's position relative to the hub so
 *  labels read outward (left-side satellites right-align away from the hub,
 *  top satellites' labels sit above the node, etc.) rather than overlapping it. */
function labelPlacement(x: number, y: number, cx: number, cy: number) {
  const dx = x - cx;
  const dy = y - cy;
  let anchor: "start" | "middle" | "end" = "middle";
  if (Math.abs(dx) > 6) anchor = dx > 0 ? "start" : "end";
  const yOffset = dy >= 0 ? 16 : -10;
  return { anchor, yOffset };
}

/** Small SVG hub-and-satellite graph — reads as a mini blast-radius view.
 *  `satellites` auto-layout in a circle around `hub`; `edges` connect them,
 *  drawn normal (solid gray) or flagged (dashed red per `get_blast_radius`
 *  high-risk edges like AFFECTED_BY_CVE). */
export function RelationshipMiniGraph({
  hub,
  satellites,
  edges,
  width = VIEW_W,
  height = VIEW_H,
}: RelationshipMiniGraphProps) {
  const cx = VIEW_W / 2;
  const cy = VIEW_H / 2;
  const laidOut = layoutSatellites(satellites, cx, cy, RING_RADIUS);
  const posById = new Map(laidOut.map((s) => [s.id, s]));

  const flaggedCount = satellites.filter((s) => s.flagged).length;
  const flaggedEdgeCount = edges.filter((e) => e.style === "flagged").length;
  const ariaLabel =
    `Relationship graph centered on ${hub.label}, with ${satellites.length} related ` +
    `resource${satellites.length === 1 ? "" : "s"}` +
    (flaggedCount > 0 ? `, ${flaggedCount} flagged` : "") +
    (flaggedEdgeCount > 0
      ? `, ${flaggedEdgeCount} flagged edge${flaggedEdgeCount === 1 ? "" : "s"}`
      : "") +
    `: ` +
    (satellites.length === 0
      ? "no related resources"
      : satellites
          .map((s) => `${s.label}${s.flagged ? " (flagged)" : ""}`)
          .join(", "));

  const normalEdges = edges.filter((e) => e.style === "normal");
  const flaggedEdges = edges.filter((e) => e.style === "flagged");

  const fontFamily = '"DM Mono", ui-monospace, monospace';

  return (
    <div className="gw-scope">
      <svg
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        width={width}
        height={height}
        role="img"
        aria-label={ariaLabel}
      >
        <g stroke="#3a3f4a" strokeWidth={1}>
          {normalEdges.map((e, i) => {
            const from = e.fromId === hub.id ? { x: cx, y: cy } : posById.get(e.fromId);
            const to = e.toId === hub.id ? { x: cx, y: cy } : posById.get(e.toId);
            if (!from || !to) return null;
            return (
              <line
                key={`edge-normal-${i}`}
                data-testid="edge-normal"
                x1={from.x}
                y1={from.y}
                x2={to.x}
                y2={to.y}
              >
                <title>{e.edgeType}</title>
              </line>
            );
          })}
        </g>
        <g stroke="var(--gw-red)" strokeWidth={1.5} strokeDasharray="4 3">
          {flaggedEdges.map((e, i) => {
            const from = e.fromId === hub.id ? { x: cx, y: cy } : posById.get(e.fromId);
            const to = e.toId === hub.id ? { x: cx, y: cy } : posById.get(e.toId);
            if (!from || !to) return null;
            return (
              <line
                key={`edge-flagged-${i}`}
                data-testid="edge-flagged"
                x1={from.x}
                y1={from.y}
                x2={to.x}
                y2={to.y}
              >
                <title>{e.edgeType}</title>
              </line>
            );
          })}
        </g>

        <g fill="var(--gw-muted)">
          {laidOut
            .filter((s) => !s.flagged)
            .map((s) => (
              <circle key={s.id} data-testid="satellite-normal" cx={s.x} cy={s.y} r={SAT_R} />
            ))}
        </g>
        {laidOut
          .filter((s) => s.flagged)
          .map((s) => (
            <circle
              key={s.id}
              data-testid="satellite-flagged"
              cx={s.x}
              cy={s.y}
              r={SAT_R_FLAGGED}
              fill="var(--gw-red)"
            />
          ))}

        {laidOut.map((s) => {
          const { anchor, yOffset } = labelPlacement(s.x, s.y, cx, cy);
          return (
            <text
              key={`label-${s.id}`}
              x={s.x}
              y={s.y + yOffset}
              textAnchor={anchor}
              fontFamily={fontFamily}
              fontSize={8.5}
              fill="var(--gw-muted)"
            >
              {s.label}
            </text>
          );
        })}

        <circle cx={cx} cy={cy} r={HUB_R} fill="var(--gw-panel)" stroke="var(--gw-yellow)" strokeWidth={2} />
        <text
          x={cx}
          y={cy - HUB_R - 6}
          textAnchor="middle"
          fontFamily={fontFamily}
          fontSize={9.5}
          fill="var(--gw-text)"
        >
          {hub.label}
        </text>
      </svg>
    </div>
  );
}
