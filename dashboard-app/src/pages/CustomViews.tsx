/** Custom Views page — React port of legacy customviews.dc.html.
 *
 *  Wave 1 regression fix: port the OpenUI live-rendering bridge.
 *
 *  Legacy bridge (customviews.dc.html:77-84 + shell.dc.html:6044-6318):
 *  A MutationObserver watched a hidden #openui-relay div whose data-output /
 *  data-streaming attributes were updated by the DC framework whenever component
 *  state changed.  The observer called parseAndRender(), which scanned the LLM
 *  output for self-closing JSX-like tags such as
 *    <FleetStatCard title="Online hosts" value={42} color="green" />
 *  and rendered them as real React components from a registry loaded by
 *  static/openui/library.js.  If no tags matched, the raw text was shown in a
 *  styled <pre> block.
 *
 *  This port replaces the MutationObserver DOM bridge with a direct React prop
 *  flow: <OpenUIRenderer output={output} isStreaming={loading} />.  The component
 *  registry (FleetStatCard + 13 preview stubs) is ported verbatim from library.js
 *  into dashboard-app/src/openui/OpenUIRenderer.tsx.
 *
 *  The original React port (pre-Wave-1) rendered the streamed output as a static
 *  <pre> block only, losing the interactive component rendering entirely.
 *
 *  Additionally: accepts React Router navigation state { output, prompt } from
 *  the SavedViews "Open" action (mirrors onOpenSavedViewClick in shell.dc.html
 *  :4206-4222) to pre-populate the generator with a saved view's output and prompt.
 *
 *  Generation: POST /api/dashboard/custom-view (ui.py:146) streams NDJSON
 *  ({"token":"..."} lines, terminated by {"done":true}) — consumed via
 *  apiPostStream (api.ts), the streaming-aware sibling of apiGet/apiPost.
 *  Save/list: POST/GET /api/dashboard/views (ui.py:174/209) — same envelope
 *  {items,total,...} as the ported SavedViews page.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md) as a
 *  Phase 3 mechanical conversion. This page is a generator/form, not a
 *  collection of homogeneous records, so per DESIGN.md §8 it stays a
 *  Panel-based layout rather than a DataTable — the prompt input, live
 *  output, save modal, and saved-views link-out are all heterogeneous
 *  single-purpose surfaces. */

import { useCallback, useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { apiGet, apiPost, apiPostStream, rows, AuthRequired, redirectToLogin } from "../api";
import { OpenUIRenderer } from "../openui/OpenUIRenderer";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { PageShell, Panel, Button, EmptyState } from "../components/ui";

type SavedCustomView = {
  id: string;
  title: string;
  prompt: string;
  is_public: boolean;
  share_url: string;
};

type NavState = { output?: string; prompt?: string } | null;

export function CustomViews() {
  const location = useLocation();
  const navState = (location.state as NavState) ?? null;

  const [prompt, setPrompt] = useState(navState?.prompt ?? "");
  const [output, setOutput] = useState(navState?.output ?? "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showSaveModal, setShowSaveModal] = useState(false);
  const [saveTitle, setSaveTitle] = useState("");
  const [saveIsPublic, setSaveIsPublic] = useState(true);
  const [shareUrl, setShareUrl] = useState("");
  const [copied, setCopied] = useState(false);
  const [savedViews, setSavedViews] = useState<SavedCustomView[] | null>(null);
  // Distinct from the page-level `error` (which is generator-submission-scoped):
  // tracks a genuine failure to load the saved-views list, so it isn't
  // pixel-identical to "you have no saved views yet" (savedViews === []).
  const [savedViewsError, setSavedViewsError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await apiGet<unknown>("/api/dashboard/views");
      setSavedViews(rows<SavedCustomView>(d));
      setSavedViewsError(null);
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      // Non-auth errors here are non-fatal — the generator still works without the
      // list — but must still be visible rather than silently dropped.
      else setSavedViewsError(String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useAutoRefresh(load);

  async function onSubmit() {
    if (!prompt.trim() || loading) return;
    setLoading(true);
    setError(null);
    setOutput("");
    setShareUrl("");
    try {
      for await (const line of apiPostStream("/api/dashboard/custom-view", { prompt })) {
        const parsed = JSON.parse(line) as { token?: string; done?: boolean };
        if (parsed.token) setOutput((o) => o + parsed.token);
        if (parsed.done) break;
      }
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  async function onSaveSubmit() {
    try {
      const view = await apiPost<SavedCustomView>("/api/dashboard/views", {
        title: saveTitle,
        prompt,
        openui_lang: output,
        is_public: saveIsPublic,
      });
      setShareUrl(view.share_url);
      setShowSaveModal(false);
      setSavedViews((prev) => [view, ...(prev ?? [])]);
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    }
  }

  const h = headerFor("customviews");
  const pills = [{ dot: "var(--ib-blue)", text: "AI-powered" }];

  return (
    <div>
      <PageShell icon="✨" title={h.title} description={h.desc} pills={pills} />

      {/* Prompt input */}
      <div style={{ marginBottom: 18 }}>
        <Panel rail="info" mnemonic="GENERATE" description="Create a Custom View">
          <div style={{ padding: 16 }}>
            <div style={{ fontSize: 11, color: "var(--ib-muted)", marginBottom: 16 }}>
              Describe what you want to see. Infra Brain will generate a live dashboard component from your
              infrastructure data.
            </div>
            <div style={{ display: "flex", gap: 10 }}>
              <input
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="e.g. Show me all Linux hosts with drift events in the last 7 days…"
                style={{
                  flex: 1,
                  minWidth: 0,
                  background: "var(--ib-raised)",
                  border: "1px solid var(--ib-border)",
                  borderRadius: "var(--ib-radius-sm)",
                  padding: "10px 14px",
                  color: "var(--ib-text)",
                  fontSize: 13,
                  outline: "none",
                  fontFamily: "inherit",
                }}
              />
              <Button variant="primary" onClick={() => void onSubmit()} loading={loading} disabled={!prompt.trim()}>
                Generate View
              </Button>
            </div>
            {loading && (
              <div style={{ marginTop: 12, fontSize: 12, color: "var(--ib-blue)", fontStyle: "italic" }}>
                Generating your view…
              </div>
            )}
          </div>
        </Panel>
      </div>

      {error && (
        <div style={{ marginBottom: 18 }}>
          <EmptyState kind="error" title="Custom view generation failed" hint={error} />
        </div>
      )}

      {/* Live output area — OpenUI renderer replaces the static <pre> block */}
      {output && (
        <div style={{ marginBottom: 18 }}>
          {/* During streaming: OpenUIRenderer shows the spinner.
              After streaming: it parses component tags; falls back to raw text. */}
          <OpenUIRenderer output={output} isStreaming={loading} />

          {!loading && (
            <div style={{ marginTop: 12 }}>
              <Button variant="primary" onClick={() => setShowSaveModal(true)}>
                Save &amp; Share
              </Button>
            </div>
          )}
        </div>
      )}

      {/* Save modal */}
      {showSaveModal && (
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
              Save Custom View
            </div>
            <input
              type="text"
              value={saveTitle}
              onChange={(e) => setSaveTitle(e.target.value)}
              placeholder="View title"
              style={{
                width: "100%",
                boxSizing: "border-box",
                padding: "8px 12px",
                background: "var(--ib-raised)",
                border: "1px solid var(--ib-border)",
                borderRadius: "var(--ib-radius-sm)",
                color: "var(--ib-text)",
                fontSize: 13,
                marginBottom: 12,
                fontFamily: "inherit",
              }}
            />
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                fontSize: 13,
                color: "var(--ib-muted)",
                marginBottom: 16,
              }}
            >
              <input type="checkbox" checked={saveIsPublic} onChange={(e) => setSaveIsPublic(e.target.checked)} />
              Make shareable (public link)
            </label>
            <div style={{ display: "flex", gap: 8 }}>
              <Button variant="primary" onClick={() => void onSaveSubmit()} style={{ flex: 1 }}>
                Save
              </Button>
              <Button variant="secondary" onClick={() => setShowSaveModal(false)} style={{ flex: 1 }}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Share URL */}
      {shareUrl && (
        <div style={{ marginBottom: 18 }}>
          <Panel rail="ok" mnemonic="SHARE" description="Share link ready">
            <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 16px" }}>
              <span
                style={{
                  flex: 1,
                  minWidth: 0,
                  fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                  fontSize: 12,
                  color: "var(--ib-muted)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                Share link: <span style={{ color: "var(--ib-blue)" }}>{shareUrl}</span>
              </span>
              <Button
                size="sm"
                onClick={() => {
                  // shareUrl is the raw relative path from the backend (e.g.
                  // "/dashboard2/views/abc123") — prefix with the origin so the
                  // copied text is a real clickable URL (Slack/email render a
                  // bare path as plain text, not a link). Do not also prepend
                  // "/dashboard2" here — the backend's share_url already
                  // includes it.
                  void navigator.clipboard?.writeText(`${window.location.origin}${shareUrl}`).then(() => {
                    setCopied(true);
                    setTimeout(() => setCopied(false), 1500);
                  });
                }}
              >
                {copied ? "Copied ✓" : "Copy"}
              </Button>
            </div>
          </Panel>
        </div>
      )}

      {/* Distinct from the legitimately-empty case (savedViews === []): a genuine
       *  load failure gets a small, visible inline indicator instead of silently
       *  looking identical to "you have no saved views." */}
      {savedViewsError && (
        <div style={{ marginTop: 24 }}>
          <EmptyState kind="error" title="Couldn't load your saved custom views" hint={savedViewsError} />
        </div>
      )}

      {/* Saved views: a single link to the dedicated Saved Views page rather than
       *  a second, weaker copy of the same list (Open/Copy-link/dates already
       *  live there — see SavedViews.tsx). */}
      {savedViews && savedViews.length > 0 && (
        <div style={{ marginTop: 24 }}>
          <Link
            to="/savedviews"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              fontSize: 12,
              fontWeight: 600,
              color: "var(--ib-blue)",
              background: "var(--ib-panel)",
              border: "1px solid var(--ib-border)",
              borderRadius: "var(--ib-radius-sm)",
              padding: "10px 16px",
              textDecoration: "none",
            }}
          >
            View your {savedViews.length} saved custom view{savedViews.length === 1 ? "" : "s"} →
          </Link>
        </div>
      )}
    </div>
  );
}
