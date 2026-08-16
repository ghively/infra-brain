import { useEffect, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { DataTable, EmptyState, Badge, NullCell, type ColumnDef } from "../components/ui";

export type RemediationRollupRow = {
  action_type: string;
  field: string | null;
  count: number;
  sample_targets: string[];
};

const ACTION_TYPE_TONE: Record<string, "info" | "ok" | "warn" | "err"> = {
  restart_service: "info",
  patch: "ok",
  scale: "warn",
  rollback: "err",
};

function actionTypeTone(actionType: string): "info" | "ok" | "warn" | "err" | "neutral" {
  return ACTION_TYPE_TONE[actionType] ?? "neutral";
}

/** Dedup/rollup view over pending ProposedAction rows (TRK-255) — groups by
 *  (action_type, payload->>"field") server-side (vuln.py's
 *  `list_remediation(view="rollup")`); this view is inherently small
 *  (post-aggregation), so unlike Remed.tsx's flat 500-row-capped fetch, a
 *  plain unpaginated fetch is fine here.
 *
 *  Clicking a group row drills into the parent Detail tab, filtered to that
 *  group's action_type (the closest available filter — RemediationOut has no
 *  `field` column, only ProposedAction.payload does, which the detail
 *  endpoint doesn't expose; see the design note in api/schemas.py). */
export function RemedRollup({
  onDrillToActionType,
}: {
  onDrillToActionType?: (actionType: string) => void;
}) {
  const [data, setData] = useState<RemediationRollupRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiGet<unknown>("/api/dashboard/remediation?view=rollup")
      .then((d) => {
        if (!cancelled) setData(rows<RemediationRollupRow>(d));
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return <EmptyState kind="error" title="Rollup failed to load" hint={error} />;
  }

  if (data === null) {
    return (
      <div style={{ padding: 16 }}>
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  if (data.length === 0) {
    return (
      <EmptyState
        kind="none-yet"
        title="No pending remediation actions to roll up"
        hint="Groups appear here once pending config_fix (or similar) actions accumulate."
      />
    );
  }

  const columns: ColumnDef<RemediationRollupRow>[] = [
    {
      key: "action_type",
      header: "Action",
      typeGlyph: "enum",
      render: (r) => <Badge tone={actionTypeTone(r.action_type)}>{r.action_type}</Badge>,
    },
    { key: "field", header: "Field", typeGlyph: "text", render: (r) => (r.field ? r.field : <NullCell />) },
    { key: "count", header: "Count", typeGlyph: "num", accessor: (r) => r.count },
    {
      key: "sample_targets",
      header: "Sample targets",
      typeGlyph: "text",
      render: (r) =>
        r.sample_targets.length > 0 ? (
          <span title={r.sample_targets.join(", ")}>{r.sample_targets.join(", ")}</span>
        ) : (
          <NullCell />
        ),
    },
  ];

  return (
    <DataTable<RemediationRollupRow>
      columns={columns}
      rows={data}
      rowKey={(r) => `${r.action_type}::${r.field ?? ""}`}
      sortable
      toolbarName="remediation_rollup"
      caption="Dedup rollup of pending remediation actions, grouped by action type and field"
      onRowClick={onDrillToActionType ? (r) => onDrillToActionType(r.action_type) : undefined}
      statusBar={{ shown: data.length, note: "groups — click a row to filter the Detail tab" }}
    />
  );
}
