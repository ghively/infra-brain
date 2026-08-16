/** Shared UI primitives for the DR-6.1 dashboard redesign.
 *
 * Import from this barrel (`import { Panel, DataTable } from "../components/ui"`)
 * — the individual modules each import `./ui.css`, so styles arrive with the
 * component no matter which entry point a page uses.
 *
 * See `ui.css`'s header comment for the styling conventions and the palette
 * reconciliation note against `src/lib/tokens.ts`.
 */
export { Panel, PanelFootItem } from "./Panel";
export type { PanelProps, PanelRail } from "./Panel";

export { DataTable, DENSITIES } from "./DataTable";
export type { ColumnDef, DataTableProps, Density, StatusBarInfo, TypeGlyph } from "./DataTable";

export { Badge, SeverityPill, normalizeSeverity } from "./SeverityPill";
export type { BadgeProps, Severity, SeverityPillProps } from "./SeverityPill";

export { DomainCell, FkCell, GaugeCell, NullCell, isNullish } from "./cells";
export type { FkCellProps, GaugeCellProps } from "./cells";

export { Button } from "./Button";
export type { ButtonProps, ButtonSize, ButtonVariant } from "./Button";

export { EmptyState } from "./EmptyState";
export type { EmptyStateAction, EmptyStateKind, EmptyStateProps } from "./EmptyState";

export { PageShell } from "./PageShell";
export type { PageShellPill, PageShellProps } from "./PageShell";

export { StatTile, SummaryBlock } from "./StatTile";
export type {
  HealthSegment,
  StatColor,
  StatTileProps,
  SummaryBlockProps,
  SummaryHero,
  SummaryStat,
} from "./StatTile";

export { Tabs } from "./Tabs";
export type { TabItem, TabsProps } from "./Tabs";

/* Graphic widgets — the hand-rolled instruments from the approved mockup. */
export { DriftTapeInstrument } from "./DriftTapeInstrument";
export type { DriftTapeInstrumentProps, DriftTapeZone } from "./DriftTapeInstrument";

export { TrendBarChart } from "./TrendBarChart";
export type { TrendBarChartProps, TrendBarDatum } from "./TrendBarChart";

export { RelationshipMiniGraph } from "./RelationshipMiniGraph";
export type {
  RelationshipEdge,
  RelationshipMiniGraphProps,
  RelationshipNode,
  RelationshipSatellite,
} from "./RelationshipMiniGraph";
