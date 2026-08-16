import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { useParams } from "react-router-dom";
import { apiGet, AuthRequired, ApiError, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { PageShell, EmptyState } from "../components/ui";
import { fmtDt } from "../lib/fmtDt";
import { OpenUIRenderer } from "../openui/OpenUIRenderer";

/** Public/owner view-share landing page (UI-0). Backed by the already-existing
 *  GET /api/dashboard/views/{share_token} (ui.py:316-353), which is public for
 *  a view with is_public=true and owner-gated (401 unauthenticated / 403
 *  non-owner) otherwise. Reached via the `share_url` a Saved Views card's
 *  "Copy link" button copies.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md) as a
 *  Phase 3 mechanical PageShell/EmptyState conversion — the 403-forbidden
 *  case (TRK-198: this file, not the deleted ViewShared.tsx duplicate, is
 *  canonical for the `/views/:token` route) is preserved exactly, now
 *  rendered as an `EmptyState` instead of a bare `role="alert"` div.
 *
 *  F-2: this route mounts OUTSIDE AuthenticatedLayout (see App.tsx), so it
 *  gets none of the shell's `padding: 26px 32px` content wrapper or its
 *  Sidebar/Topbar chrome — `PAGE_PADDING` below reproduces just the padding
 *  inline so an anonymous viewer sees a coherent, breathing-room page rather
 *  than content flush against the viewport edge. */
const PAGE_PADDING: CSSProperties = { padding: "26px 32px" };
type SharedView = {
  id: string;
  title: string;
  prompt: string;
  openui_lang: string;
  is_public: boolean;
  created_at: string;
};

export function ViewShare() {
  const { token } = useParams<{ token: string }>();
  const [view, setView] = useState<SharedView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  useEffect(() => {
    if (!token) return;
    let live = true;
    apiGet<SharedView>(`/api/dashboard/views/${encodeURIComponent(token)}`)
      .then((d) => {
        if (live) setView(d);
      })
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) {
          redirectToLogin();
          return;
        }
        if (e instanceof ApiError && e.status === 403) {
          setForbidden(true);
          return;
        }
        setError(String(e));
      });
    return () => {
      live = false;
    };
  }, [token]);

  if (forbidden) {
    return (
      <div style={PAGE_PADDING}>
        <EmptyState
          kind="error"
          title="You do not have access to this view"
          hint="This view is private and owned by someone else. Ask the owner to make it public or share it with you directly."
        />
      </div>
    );
  }
  if (error) {
    return (
      <div style={PAGE_PADDING}>
        <EmptyState kind="error" title="Shared view failed to load" hint={error} />
      </div>
    );
  }
  if (view === null)
    return (
      <div style={PAGE_PADDING}>
        <Skeleton count={3} height={32} />
      </div>
    );

  return (
    <div style={PAGE_PADDING}>
      <PageShell
        icon="🔗"
        title={view.title}
        description={`Shared ${view.is_public ? "publicly" : "privately"} · ${fmtDt(view.created_at) ?? "unknown"}`}
      />
      <div style={{ fontSize: 12, color: "var(--ib-faint)", marginBottom: 16, fontStyle: "italic" }}>
        “{view.prompt}”
      </div>
      <OpenUIRenderer output={view.openui_lang} isStreaming={false} />
    </div>
  );
}
