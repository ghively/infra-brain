/** Saved Views page — React port of legacy savedviews.dc.html.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md) as a
 *  Phase 3 mechanical DataTable/Panel conversion — PageHeader → PageShell,
 *  hand-rolled `.ib-card` rows → a `DataTable` with a per-row Actions column,
 *  ad-hoc confirm modal markup restyled onto design-system tokens.
 *
 *  Preserved behavior (do not regress):
 *  - Field-name fix: API returns `title` (CustomViewOut.title), not `name`.
 *  - created_at date display.
 *  - Open action: navigates to /customviews with state { output, prompt } so
 *    CustomViews pre-loads the saved output (mirrors onOpenSavedViewClick in
 *    shell.dc.html:4206-4222).
 *  - Delete action with a confirmation modal (calls DELETE
 *    /api/dashboard/views/{id}, ui.py:333, and refreshes the list on success).
 *  - Public/private badge chip.
 *  - Copy-link button: `share_url` from the backend already includes the
 *    `/dashboard2` prefix — do NOT prepend it again, or the copied link
 *    double-prefixes and 404s (no route matches).
 *  - Inline rename/visibility edit (title + Public checkbox) via PATCH
 *    /api/dashboard/views/{id}.
 */

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiGet, apiPatch, apiDelete, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { Icon } from "../lib/icons";
import { fmtDt } from "../lib/fmtDt";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  Panel,
  DataTable,
  Button,
  EmptyState,
  Badge,
  type ColumnDef,
} from "../components/ui";

type SavedView = {
  id: string;
  /** API field is `title`, not `name` — matches CustomViewOut (ui.py). */
  title: string;
  prompt: string;
  openui_lang: string | null;
  is_public: boolean;
  share_url: string;
  created_at: string;
};

export function SavedViews() {
  const navigate = useNavigate();
  const [views, setViews] = useState<SavedView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<SavedView | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editPublic, setEditPublic] = useState(true);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    const d = await apiGet<unknown>("/api/dashboard/views");
    setViews(rows<SavedView>(d));
  }, []);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  async function onOpen(v: SavedView) {
    // Navigate to Custom Views pre-loading this view's output and prompt,
    // mirroring onOpenSavedViewClick (shell.dc.html:4206).
    navigate("/customviews", {
      state: { output: v.openui_lang ?? "", prompt: v.prompt },
    });
  }

  function onDeleteClick(v: SavedView) {
    setConfirmDelete(v);
  }

  async function onDeleteConfirmed() {
    const v = confirmDelete;
    if (!v) return;
    setConfirmDelete(null);
    setDeletingId(v.id);
    try {
      await apiDelete(`/api/dashboard/views/${v.id}`);
      setViews((prev) => (prev ?? []).filter((x) => x.id !== v.id));
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    } finally {
      setDeletingId(null);
    }
  }

  function startEdit(v: SavedView) {
    setEditingId(v.id);
    setEditTitle(v.title);
    setEditPublic(v.is_public);
  }

  async function saveEdit(v: SavedView) {
    try {
      const updated = await apiPatch<SavedView>(`/api/dashboard/views/${v.id}`, {
        title: editTitle.trim(),
        is_public: editPublic,
      });
      setViews((prev) => (prev ?? []).map((x) => (x.id === v.id ? updated : x)));
      setEditingId(null);
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    }
  }

  function copyLink(v: SavedView) {
    // share_url from the backend (ui.py) already includes the /dashboard2
    // prefix (e.g. "/dashboard2/views/abc123") — do NOT prepend it again
    // here, or the copied link double-prefixes to
    // "origin/dashboard2/dashboard2/views/abc123" and 404s (no route matches).
    const abs = `${window.location.origin}${v.share_url}`;
    void navigator.clipboard?.writeText(abs).then(() => {
      setCopiedId(v.id);
      setTimeout(() => setCopiedId(null), 1500);
    });
  }

  const h = headerFor("savedviews");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Saved views failed to load" hint={error} />
      </div>
    );
  }

  if (views === null) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const pills = [{ dot: "var(--ib-blue)", text: "shared views" }];
  const empty = views.length === 0;

  const columns: ColumnDef<SavedView>[] = [
    {
      key: "title",
      header: "Title",
      typeGlyph: "text",
      render: (v) =>
        editingId === v.id ? (
          <input
            value={editTitle}
            onChange={(e) => setEditTitle(e.target.value)}
            style={{
              background: "var(--ib-raised)",
              border: "1px solid var(--ib-border)",
              borderRadius: "var(--ib-radius-sm)",
              color: "var(--ib-text)",
              fontSize: 12,
              padding: "4px 9px",
              outline: "none",
              fontFamily: "inherit",
              width: "100%",
            }}
          />
        ) : (
          v.title
        ),
    },
    {
      key: "created_at",
      header: "Created",
      typeGlyph: "text",
      render: (v) => {
        const t = fmtDt(v.created_at);
        return t ? <span style={{ whiteSpace: "nowrap" }}>{t}</span> : undefined;
      },
    },
    {
      key: "is_public",
      header: "Visibility",
      typeGlyph: "enum",
      render: (v) =>
        editingId === v.id ? (
          <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: "var(--ib-muted)" }}>
            <input type="checkbox" checked={editPublic} onChange={(e) => setEditPublic(e.target.checked)} />
            Public
          </label>
        ) : (
          <Badge tone={v.is_public ? "ok" : "warn"}>{v.is_public ? "Public" : "Private"}</Badge>
        ),
    },
    {
      key: "actions",
      header: "Actions",
      typeGlyph: "text",
      sortable: false,
      render: (v) => {
        const isDeleting = deletingId === v.id;
        if (editingId === v.id) {
          return (
            <div style={{ display: "flex", gap: 8 }}>
              <Button variant="primary" size="sm" onClick={() => void saveEdit(v)}>
                Save
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setEditingId(null)}>
                Cancel
              </Button>
            </div>
          );
        }
        return (
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Button variant="ghost" size="sm" onClick={() => copyLink(v)}>
              {copiedId === v.id ? "Copied ✓" : "Copy link"}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => startEdit(v)} aria-label="Edit" title="Edit">
              Edit
            </Button>
            <Button
              variant="danger"
              size="sm"
              onClick={() => onDeleteClick(v)}
              disabled={isDeleting}
              aria-label={isDeleting ? "Deleting" : "Delete"}
              title="Delete"
            >
              {isDeleting ? "Deleting…" : "Delete"}
            </Button>
            <Button variant="primary" size="sm" onClick={() => void onOpen(v)}>
              Open
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} pills={pills} />

      <Panel mnemonic="VIEWS" description="views — saved &amp; shared custom views" live>
        {empty ? (
          <EmptyState
            kind="none-yet"
            title="No saved views yet"
            hint="Go to Custom Views and generate a view, then click Save &amp; Share."
          />
        ) : (
          <DataTable<SavedView>
            columns={columns}
            rows={views}
            rowKey={(v) => v.id}
            sortable
            initialSortKey="created_at"
            initialSortDir="desc"
            toolbarName="views"
            caption="Saved views: title, created date, visibility, and actions"
            statusBar={{ shown: views.length, note: "read-only · no mutations possible from this view" }}
          />
        )}
      </Panel>

      {confirmDelete && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,.6)",
            zIndex: 999,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            style={{
              background: "var(--ib-panel)",
              border: "1px solid var(--ib-border)",
              borderRadius: "var(--ib-radius)",
              padding: 24,
              width: 360,
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 600, color: "var(--ib-text)", marginBottom: 16 }}>
              Delete "{confirmDelete.title}"?
            </div>
            <div style={{ fontSize: 13, color: "var(--ib-muted)", marginBottom: 16 }}>This cannot be undone.</div>
            <div style={{ display: "flex", gap: 8 }}>
              <Button variant="danger" onClick={() => void onDeleteConfirmed()} style={{ flex: 1 }}>
                Delete
              </Button>
              <Button variant="ghost" onClick={() => setConfirmDelete(null)} style={{ flex: 1 }}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
