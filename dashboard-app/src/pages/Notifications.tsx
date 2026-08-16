import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  Button,
  EmptyState,
  Badge,
  type ColumnDef,
} from "../components/ui";
import { fmtDt } from "../lib/fmtDt";

type Notification = {
  type: string;
  target: string;
  title: string;
  domain: string;
  created: string;
  status: string;
  jira_url?: string | null;
  confluence_url?: string | null;
};

// ACCURACY (T2 audit, preserved from the pre-Phase-3 page): src/infra_brain/
// api/routers/governance_drift.py's list_notifications() hardcodes
// status="open" on every Jira row and status="synced" on every Confluence
// row (no real ticket/page state is tracked — see the two NotificationOut(...)
// constructions there). "synced" for Confluence is at least approximately
// true by construction (the row only exists because a page write succeeded),
// so its badge stays. "open" for Jira is not — the ticket may since have been
// closed/resolved and this backend never learns that, so the badge would
// misrepresent it as live operational state. The status column below is
// therefore rendered only for Confluence rows, same as before this rebuild.
function statusTone(status: string): "ok" | "neutral" {
  return status === "synced" || status === "resolved" ? "ok" : "neutral";
}

const TABS: [string, string][] = [
  ["(all)", "All"],
  ["jira", "Jira"],
  ["confluence", "Confluence"],
];

/** Rebuild of Notifications.tsx on the Phase 1 design system (see
 *  dashboard-app/DESIGN.md) — Phase 3 mechanical DataTable/Panel conversion.
 *
 *  Backed by GET /api/dashboard/notifications (governance.py:216) — returns
 *  NotificationPageOut {items,total,limit,offset}. Filtering by type is done
 *  client-side against the full (unfiltered) fetch, matching legacy behavior
 *  where all notifications are loaded once and tabs just slice the array.
 *
 *  Preserves two prior UX-audit fixes that must not regress:
 *  - The fabricated "Open" stat tile was removed (it was mathematically
 *    identical to "Jira Tickets" since every Jira row is hardcoded
 *    status="open" — see statusTone's docstring above).
 *  - Timestamps go through fmtDt(), never raw ISO strings.
 *
 *  Row-level Jira/Confluence deep links (TRK-178 follow-up, GitLab #158):
 *  list_notifications() now serves jira_url/confluence_url base URLs from
 *  settings, so `target` renders as a link when the corresponding backend
 *  is configured, and as plain text (no implied click-through) otherwise. */
export function Notifications() {
  const [notifs, setNotifs] = useState<Notification[] | null>(null);
  // FE-6: true DB-wide total from the {items,total,...} envelope; the
  // per-type breakdowns below stay over the loaded page.
  const [totalNotifs, setTotalNotifs] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("(all)");

  const load = useCallback(async () => {
    await apiGet<unknown>("/api/dashboard/notifications?limit=500")
      .then((d) => {
        setNotifs(rows<Notification>(d));
        setTotalNotifs((d as { total?: number }).total ?? null);
        setError(null);
      })
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const filtered = useMemo(() => {
    if (!notifs) return [];
    if (filter === "(all)") return notifs;
    return notifs.filter((n) => n.type === filter);
  }, [notifs, filter]);

  // ACCURACY (T2 audit): no "Open" tile — see statusTone's docstring above for
  // why it would have been a fabricated duplicate of "Jira Tickets".
  const stats = useMemo(() => {
    const all = notifs || [];
    return [
      { label: "Total", value: totalNotifs ?? all.length },
      { label: "Jira Tickets", value: all.filter((n) => n.type === "jira").length },
      { label: "Confluence", value: all.filter((n) => n.type === "confluence").length },
    ];
  }, [notifs, totalNotifs]);

  const hdr = headerFor("notifications");

  if (error) {
    return (
      <div>
        <PageShell title={hdr.title} description={hdr.desc} />
        <EmptyState kind="error" title="Notifications failed to load" hint={error} />
      </div>
    );
  }

  if (notifs === null) {
    return (
      <div>
        <PageShell title={hdr.title} description={hdr.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const empty = filtered.length === 0;

  const columns: ColumnDef<Notification>[] = [
    {
      key: "type",
      header: "Type",
      typeGlyph: "enum",
      render: (n) => (
        <Badge tone={n.type === "jira" ? "warn" : "info"}>{n.type === "jira" ? "Jira" : "Confluence"}</Badge>
      ),
    },
    { key: "title", header: "Title", typeGlyph: "text" },
    {
      key: "target",
      header: "Target",
      typeGlyph: "text",
      render: (n) => {
        const url = n.type === "jira" ? n.jira_url : n.confluence_url;
        return url ? (
          <a href={url} target="_blank" rel="noreferrer">
            {n.target}
          </a>
        ) : (
          <span>{n.target}</span>
        );
      },
    },
    { key: "domain", header: "Domain", typeGlyph: "enum" },
    {
      key: "created",
      header: "Created",
      typeGlyph: "text",
      render: (n) => <span>{fmtDt(n.created)}</span>,
    },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      sortable: false,
      // Suppressed for Jira rows — see statusTone's docstring: the backend
      // hardcodes status="open" for every Jira row and never learns the
      // ticket's real state, so showing it as a badge would misrepresent
      // stale data as live. Rendered as an em-dash rather than left `null` —
      // DataTable's null contract (DESIGN.md §4) would otherwise show this
      // as the `NULL` token, which is wrong here: the status isn't missing,
      // it's deliberately not surfaced because the backend can't track it.
      render: (n) =>
        n.type === "jira" ? (
          <span style={{ color: "var(--ib-faint)" }}>—</span>
        ) : (
          <Badge tone={statusTone(n.status)}>{n.status}</Badge>
        ),
    },
  ];

  return (
    <div>
      <PageShell title={hdr.title} description={hdr.desc} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} />
        ))}
      </div>

      <div style={{ display: "flex", gap: 6, marginBottom: 16, flexWrap: "wrap" }}>
        {TABS.map(([v, label]) => (
          <Button key={v} variant={filter === v ? "primary" : "ghost"} size="sm" onClick={() => setFilter(v)}>
            {label}
          </Button>
        ))}
      </div>

      <Panel rail="info" mnemonic="NOTIFY" description="jira_notifications ⨝ confluence_notifications" live>
        {empty ? (
          <EmptyState
            kind={filter === "(all)" ? "none-yet" : "filter-zero"}
            title={
              filter === "(all)"
                ? "No notifications recorded yet"
                : "No notifications match the current filter"
            }
            hint={
              filter === "(all)"
                ? "Notifications appear here after the next Jira/Confluence write-back."
                : "Switch tabs to see other notifications."
            }
            action={
              filter !== "(all)"
                ? { label: "Clear filter", onClick: () => setFilter("(all)") }
                : undefined
            }
          />
        ) : (
          <DataTable<Notification>
            columns={columns}
            rows={filtered}
            rowKey={(n, i) => `${n.target}-${i}`}
            sortable
            initialSortKey="created"
            initialSortDir="desc"
            toolbarName="notifications"
            caption="Notifications: type, title, target, domain, created time, and status"
            statusBar={{ shown: filtered.length, total: totalNotifs ?? filtered.length, note: "read-only · no mutations possible from this view" }}
          />
        )}
      </Panel>
    </div>
  );
}
