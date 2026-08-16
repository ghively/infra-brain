import type { ReactNode } from "react";
import { ACCENT, hexA } from "../lib/tokens";
import { Icon } from "../lib/icons";

/** A single header pill: a colored dot (`dot`) followed by label text (`text`),
 *  matching v1's `pills[pg]` shape. */
export type PageHeaderPill = { dot: string; text: string };

/** Shared page header (visual-parity pass): a color-tinted icon tile + 20px
 *  title + description + an optional row of colored-dot status pills. The tile
 *  tint derives from `color` (default ACCENT) via hexA, matching v1.
 *
 *  The tile icon can be supplied two ways: pass an explicit `icon` node, OR
 *  pass an `iconName` (a v1 icon-name string, e.g. "vsphere") to render the
 *  shared inline-SVG `<Icon>` inside the tile. `icon` wins if both are given.
 *  At least one of `icon` / `iconName` should be provided. */
export function PageHeader({
  icon,
  iconName,
  title,
  description,
  color = ACCENT,
  pills,
}: {
  icon?: ReactNode;
  iconName?: string;
  title: string;
  description?: string;
  color?: string;
  pills?: PageHeaderPill[];
}) {
  const tileContent: ReactNode =
    icon ?? (iconName ? <Icon name={iconName} size={22} /> : null);
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 14, marginBottom: 20 }}>
      <div
        style={{
          width: 42,
          height: 42,
          borderRadius: 10,
          background: hexA(color, 0.1),
          border: `1px solid ${hexA(color, 0.22)}`,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 18,
          flexShrink: 0,
        }}
      >
        {tileContent}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 20, fontWeight: 700, color: "#eef2ff", letterSpacing: "-.3px" }}>{title}</div>
        {description && (
          <div style={{ fontSize: 12, color: "#4b6080", marginTop: 3 }}>{description}</div>
        )}
        {pills && pills.length > 0 && (
          <div style={{ display: "flex", gap: 12, marginTop: 8, flexWrap: "wrap", alignItems: "center" }}>
            {pills.map((p) => (
              <span
                key={p.text}
                style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, color: "#6b7a99" }}
              >
                <span
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    background: p.dot,
                    flexShrink: 0,
                  }}
                />
                {p.text}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
