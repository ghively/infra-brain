import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, AuthRequired, redirectToLogin } from "../api";
import { DetailDrawer } from "../components/DetailDrawer";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { Icon } from "../lib/icons";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  Panel,
  DataTable,
  Button,
  EmptyState,
  Badge,
  NullCell,
  type ColumnDef,
  type PanelRail,
} from "../components/ui";
import { fmtDt } from "../lib/fmtDt";

type DocumentItem = {
  id: string;
  title: string;
  sensitivity: string;
  source: string;
  status: string;
  space: string;
  url: string;
  indexed_at: string | null;
  last_updated: string | null;
  chunk_count: number;
};

type DocumentsResponse = { items: DocumentItem[]; total: number; limit: number; offset: number };

type DocumentChunkPreview = { chunk_index: number; text_preview: string; token_count: number | null };

type DocumentDetail = DocumentItem & {
  external_id: string;
  source_version: number | null;
  content_hash: string;
  chunks: DocumentChunkPreview[];
};

const PAGE_SIZE = 100;

const STATUS_TABS: [string, string][] = [
  ["(all)", "All"],
  ["current", "Current"],
  ["stale", "Stale"],
];

/** Sensitivity classification → Badge tone. Restricted/confidential documents
 *  are the ones an operator most needs to notice, so they read `err` (red);
 *  internal reads `info` (blue); anything else (public, unclassified, unknown)
 *  reads `neutral`. This is a data-sensitivity axis, not a severity finding,
 *  so it deliberately does not go through `severityStyle()` (DESIGN.md §3's
 *  "Status is a separate axis" note applies here too — sensitivity is a third
 *  axis, distinct from both severity and run-status). */
function sensitivityTone(level: string): "err" | "info" | "neutral" {
  const v = (level || "").toLowerCase();
  if (v === "restricted" || v === "confidential") return "err";
  if (v === "internal") return "info";
  return "neutral";
}

/** Document freshness → Badge tone. `stale` reads warn (yellow); `current`
 *  (or anything else) reads ok (green) — same "unknown reads as a real state,
 *  not a guess" posture as the rest of the app, just collapsed to two buckets
 *  since the backend only ever emits these two values today. */
function statusTone(status: string): "warn" | "ok" {
  return (status || "").toLowerCase() === "stale" ? "warn" : "ok";
}

function DocumentDetailPanel({ documentId }: { documentId: string }) {
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let live = true;
    setDetail(null);
    setErr(false);
    apiGet<DocumentDetail>(`/api/dashboard/documents/${encodeURIComponent(documentId)}`)
      .then((d) => live && setDetail(d))
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setErr(true);
      });
    return () => {
      live = false;
    };
  }, [documentId]);

  if (err) return <EmptyState kind="error" title="Could not load document detail" />;
  if (detail === null) return <Skeleton count={3} height={20} />;

  const row = (label: string, value: React.ReactNode) => (
    <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
      <span style={{ fontSize: 11, color: "var(--ib-muted)", fontWeight: 600, minWidth: 110, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 12, color: "var(--ib-text)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)", wordBreak: "break-all" }}>
        {value}
      </span>
    </div>
  );

  return (
    <div style={{ padding: "4px 0" }}>
      <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
        <Badge tone={sensitivityTone(detail.sensitivity)}>{detail.sensitivity}</Badge>
        <Badge tone={statusTone(detail.status)}>{detail.status}</Badge>
      </div>

      {row("Source", detail.source || <NullCell />)}
      {row("Space", detail.space || <NullCell />)}
      {row(
        "URL",
        detail.url ? (
          <a href={detail.url} target="_blank" rel="noopener" style={{ color: "var(--ib-blue)" }}>
            {detail.url}
          </a>
        ) : (
          <NullCell />
        ),
      )}
      {row("Indexed", fmtDt(detail.indexed_at))}
      {row("Last Updated", fmtDt(detail.last_updated))}
      {row("External ID", detail.external_id || <NullCell />)}
      {row("Content Hash", detail.content_hash || <NullCell />)}

      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          color: "var(--ib-muted)",
          textTransform: "uppercase",
          letterSpacing: ".1em",
          margin: "20px 0 10px",
        }}
      >
        Chunks ({detail.chunk_count})
      </div>
      {detail.chunks.length === 0 ? (
        <div style={{ fontSize: 12, color: "var(--ib-faint)" }}>
          Not yet embedded — no chunks indexed for this document.
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {detail.chunks.map((c) => (
            <div
              key={c.chunk_index}
              style={{ background: "var(--ib-raised)", border: "1px solid var(--ib-border)", borderRadius: 9, padding: "10px 12px" }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: "var(--ib-blue)" }}>Chunk {c.chunk_index}</span>
                {c.token_count != null && (
                  <span style={{ fontSize: 10, color: "var(--ib-faint)" }}>{c.token_count} tokens</span>
                )}
              </div>
              <div style={{ fontSize: 12, color: "var(--ib-text)", wordBreak: "break-word" }}>{c.text_preview}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** RAG documents / knowledge-store browser (UI Batch 9 / GitLab #65), backed
 *  by GET /api/dashboard/documents + GET /api/dashboard/documents/{id}
 *  (src/infra_brain/api/routers/documents.py). This is a health-browse view
 *  ("is my knowledge base healthy") — freshness, sensitivity, source, chunk
 *  count — distinct from the existing semantic-search chat surface.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md) as a
 *  Phase 3 mechanical DataTable/Panel conversion — see this file's git
 *  history / TRACKER for the itemized before/after. */
export function Documents() {
  const [data, setData] = useState<DocumentsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("(all)");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<DocumentItem | null>(null);
  const triggerRef = useRef<HTMLElement>(null);
  // True fleet-wide stale count, independent of the current page/filters —
  // fetched via a cheap `limit=1` filtered request against the backend's
  // exact-match `status` filter (documents.py), same pattern used to fix
  // Security.tsx's Denied/DLP pill counts. Falls back to counting the
  // current page only if this sibling request hasn't resolved yet (or came
  // back without a `total`), rather than showing nothing.
  const [staleTotal, setStaleTotal] = useState<number | null>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (status !== "(all)") params.set("status", status);
    if (query) params.set("q", query);
    await Promise.all([
      apiGet<DocumentsResponse>(`/api/dashboard/documents?${params}`).then((d) => setData(d)),
      apiGet<unknown>("/api/dashboard/documents?status=stale&limit=1").then((d) => {
        setStaleTotal((d as { total?: number }).total ?? null);
      }),
    ]);
  }, [status, query, offset]);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  const h = headerFor("documents");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Documents failed to load" hint={error} />
      </div>
    );
  }
  if (data === null) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const staleCount = staleTotal ?? data.items.filter((d) => d.status === "stale").length;
  const pills = [
    { dot: "var(--ib-blue)", text: `${data.total} documents` },
    { dot: "var(--ib-yellow)", text: `${staleCount} stale` },
  ];

  const pageStart = data.total === 0 ? 0 : data.offset + 1;
  const pageEnd = Math.min(data.offset + data.items.length, data.total);
  const hasPrev = data.offset > 0;
  const hasNext = data.offset + data.items.length < data.total;
  const hasAnyFilter = status !== "(all)" || query.trim() !== "";
  const empty = data.items.length === 0;

  // Panel rail matches the worst-of (any stale document visible) → yellow,
  // else green (DESIGN.md §6's "rail matches worst contained finding", applied
  // to freshness instead of a severity field).
  const rail: PanelRail = data.items.some((d) => d.status === "stale") ? "warn" : "ok";

  const columns: ColumnDef<DocumentItem>[] = [
    {
      key: "title",
      header: "Title",
      typeGlyph: "text",
      render: (doc) => (
        <span
          style={{
            display: "inline-block",
            maxWidth: 320,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            verticalAlign: "bottom",
            fontWeight: 600,
            color: "var(--ib-text)",
          }}
          title={doc.title}
        >
          {doc.title}
        </span>
      ),
    },
    { key: "source", header: "Source", typeGlyph: "text" },
    { key: "space", header: "Space", typeGlyph: "text" },
    {
      key: "sensitivity",
      header: "Sensitivity",
      typeGlyph: "enum",
      render: (doc) => <Badge tone={sensitivityTone(doc.sensitivity)}>{doc.sensitivity}</Badge>,
    },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (doc) => <Badge tone={statusTone(doc.status)}>{doc.status}</Badge>,
    },
    { key: "chunk_count", header: "Chunks", typeGlyph: "num" },
    {
      key: "last_updated",
      header: "Last Updated",
      typeGlyph: "text",
      render: (doc) => fmtDt(doc.last_updated),
    },
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} pills={pills} />

      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
        <input
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOffset(0);
          }}
          placeholder="Search title…"
          style={{
            background: "var(--ib-raised)",
            border: "1px solid var(--ib-border)",
            borderRadius: "var(--ib-radius-sm)",
            padding: "5px 11px",
            color: "var(--ib-text)",
            fontSize: 12,
            outline: "none",
            fontFamily: "inherit",
            minWidth: 190,
          }}
        />
        <div style={{ width: 1, height: 16, background: "var(--ib-border)", margin: "0 4px" }} />
        {STATUS_TABS.map(([v, label]) => (
          <Button
            key={v}
            variant={status === v ? "primary" : "ghost"}
            size="sm"
            onClick={() => {
              setStatus(v);
              setOffset(0);
            }}
          >
            {label}
          </Button>
        ))}
      </div>

      <Panel rail={rail} mnemonic="DOCS" description="documents — RAG knowledge store" live>
        {empty ? (
          hasAnyFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No documents match the current filters"
              hint="Try a different search term, or clear back to All."
              action={{
                label: "Clear filters",
                onClick: () => {
                  setStatus("(all)");
                  setQuery("");
                  setOffset(0);
                },
              }}
            />
          ) : (
            <EmptyState
              kind="none-yet"
              title="No documents indexed yet"
              hint="Documents will appear here once the knowledge store ingests a source."
            />
          )
        ) : (
          <DataTable<DocumentItem>
            columns={columns}
            rows={data.items}
            rowKey={(doc) => doc.id}
            onRowClick={(doc) => setSelected(doc)}
            toolbarName="documents"
            caption="Documents: title, source, space, sensitivity, status, chunk count, and last updated"
            statusBar={{
              shown: data.items.length,
              total: data.total,
              note: "read-only · no mutations possible from this view",
            }}
          />
        )}
      </Panel>

      {!empty && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
          <span style={{ fontSize: 11, color: "var(--ib-faint)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)", marginRight: 6 }}>
            {pageStart}–{pageEnd} of {data.total}
          </span>
          {hasPrev && (
            <Button size="sm" onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>
              Prev
            </Button>
          )}
          {hasNext && (
            <Button size="sm" onClick={() => setOffset((o) => o + PAGE_SIZE)}>
              Next
            </Button>
          )}
        </div>
      )}

      <DetailDrawer
        title={selected ? selected.title : "Document detail"}
        open={selected !== null}
        onClose={() => setSelected(null)}
        triggerRef={triggerRef}
      >
        {selected && <DocumentDetailPanel documentId={selected.id} />}
      </DetailDrawer>
    </div>
  );
}
