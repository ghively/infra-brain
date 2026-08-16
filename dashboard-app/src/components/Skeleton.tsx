/** Shared pulsing loading placeholder (visual-parity pass, A4). Swapped in
 *  wherever pages previously rendered literal "Loading…" text, for a
 *  consistent loading treatment app-wide. */
export function Skeleton({
  width = "100%",
  height = 14,
  count = 1,
  radius = 6,
}: {
  width?: number | string;
  height?: number | string;
  count?: number;
  radius?: number;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          style={{
            width,
            height,
            borderRadius: radius,
            background:
              "linear-gradient(90deg, rgba(255,255,255,.04) 25%, rgba(255,255,255,.09) 37%, rgba(255,255,255,.04) 63%)",
            backgroundSize: "400% 100%",
            animation: "ib-skeleton-pulse 1.4s ease infinite",
          }}
        />
      ))}
    </div>
  );
}
