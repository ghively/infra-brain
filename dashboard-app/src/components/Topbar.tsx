import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { hexA } from "../lib/tokens";
import { NAV_ITEMS, NAV_GROUPS, BREADCRUMB_OVERRIDES } from "../nav";
import { useAutoRefresh } from "../hooks/useAutoRefresh";

/** Sticky top bar (Tier-0 parity port, T0.2 / T0.5 / T0.6). Shows a breadcrumb
 *  derived from NAV_ITEMS (`group › page`), a global cross-page search
 *  dropdown, a DYNAMIC system-status pill sourced from
 *  GET /api/dashboard/system_health, and a live DM Mono clock.
 *
 *  Beneath itself it renders the "Sweep Health" strip: one solid colored bar
 *  per domain, computed from the latest run per domain across
 *  GET /api/dashboard/collection_runs unioned with the domain set from
 *  GET /api/dashboard/scan_points — a faithful port of v1 (index.html
 *  L392-406 markup, L6128-6144 computation). */

// ---- shared shapes (mirror the owning pages' response types) --------------
type HealthItem = { name: string; detail: string; status: string };
type RunRow = { domain: string; status: string; started_at: string };
type ScanPoint = { domain: string; status: string };
type Resource = { hostname: string; domain: string; resource_type: string };
type DriftEvent = { field_name: string; hostname: string; domain: string };
type GeneratedScript = { name: string; purpose: string; created_by_agent: string };
type Instinct = { pattern: string; domain: string; confidence: number };
type Proposal = { endpoint: string; type: string };

type SearchResult = {
  key: string;
  dot: string;
  label: string;
  sub: string;
  type: string;
  onClick: () => void;
};

/** Walks the FULL `NAV_ITEMS` registry, not the visibility-filtered sections the
 *  sidebar renders. Deliberate: a breadcrumb describes where you are, and a page
 *  reached by a direct link is somewhere even when it has no sidebar entry. If
 *  this filtered by visibility, a historical source's page would show the wrong
 *  crumb the moment its collector was retired. */
function findBreadcrumb(pathname: string): { group: string; label: string } {
  for (const item of NAV_ITEMS) {
    const active = item.path === "/" ? pathname === "/" : pathname.startsWith(item.path);
    if (active) {
      const heading = NAV_GROUPS.find((g) => g.id === item.group)?.heading ?? "Overview";
      return { group: heading, label: item.label };
    }
  }
  // NAV_ITEMS deliberately omits SECONDARY_ROUTES (/resources, /fleet) and the
  // retired routes (/vsphere, /cloud, …) — all real routes with real content
  // but not sibling sidebar entries (see nav.ts). Without this they fell
  // through to the fallback below, showing a wrong "Overview › Dashboard"
  // breadcrumb on pages that are neither (TRK-237).
  for (const [path, crumb] of Object.entries(BREADCRUMB_OVERRIDES)) {
    if (pathname.startsWith(path)) return crumb;
  }
  return { group: "Overview", label: "Dashboard" };
}

function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

function fmtClock(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** T0.5 — dynamic status pill. Ports index.html L5807-5819: optimistic green
 *  when health is empty or all-ok, red when any item is down, amber otherwise.
 *
 *  F-7 fix (two defects):
 *   1. This used to fetch once on mount with a bare useEffect. Topbar is
 *      sticky chrome that persists for the whole session, so "All systems
 *      operational" reflected the moment of login indefinitely — a backend
 *      that went degraded an hour later still showed green. Now wired into
 *      useAutoRefresh for a quiet 5-min background refresh, same as pages.
 *   2. A failed fetch left `health` as `[]`, which was indistinguishable from
 *      a successful-but-empty response and rendered the same optimistic
 *      green pill — a monitoring UI showing green when it cannot reach the
 *      backend is worse than one showing nothing. `unreachable` now tracks
 *      fetch failure explicitly and renders a visually distinct "unknown"
 *      state (grey, hollow ring instead of a solid glowing dot) rather than
 *      reusing the healthy-green treatment. */
function StatusPill() {
  const [health, setHealth] = useState<HealthItem[]>([]);
  const [unreachable, setUnreachable] = useState(false);

  const load = useCallback(async () => {
    try {
      const d = await apiGet<unknown>("/api/dashboard/system_health");
      setHealth(rows<HealthItem>(d));
      setUnreachable(false);
    } catch (e) {
      if (e instanceof AuthRequired) {
        redirectToLogin();
        return;
      }
      setUnreachable(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const healthOk = health.length === 0 || health.every((h) => h.status === "ok");
  const healthDown = health.some((h) => h.status === "down");
  const text = unreachable
    ? "Status unknown — cannot reach backend"
    : healthOk
      ? "All systems operational"
      : healthDown
        ? "Systems down"
        : "Degraded";
  const color = unreachable ? "#8A8FA0" : healthOk ? "#4CBB6C" : healthDown ? "#F0654E" : "#D9A62E";
  const statusKey = unreachable ? "unknown" : healthOk ? "ok" : healthDown ? "down" : "degraded";

  return (
    <span
      data-health-status={statusKey}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        fontSize: 11,
        fontWeight: 600,
        color,
        background: hexA(color, 0.08),
        border: `1px solid ${hexA(color, 0.18)}`,
        borderRadius: 999,
        padding: "4px 10px",
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          // Unreachable: hollow ring, no glow — visually distinct from every
          // real status (which are all solid, glowing dots) so it can never
          // be mistaken for "healthy" at a glance.
          background: unreachable ? "transparent" : color,
          border: unreachable ? `1.5px solid ${color}` : "none",
          boxShadow: unreachable ? "none" : `0 0 7px ${hexA(color, 0.6)}`,
        }}
      />
      {text}
    </span>
  );
}

/** T0.6 — sweep-health strip. Ports index.html L6128-6144: latest run per
 *  domain (max started_at), colored solid bars, `active/total · last …` meta. */
function SweepHealthStrip() {
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [scans, setScans] = useState<ScanPoint[]>([]);

  // F-7: previously a bare mount-once useEffect, so the sweep bars reflected
  // the moment of login for the entire session. Wired into useAutoRefresh
  // for a quiet 5-min background refresh, matching every page loader.
  const load = useCallback(async () => {
    const runErr = (e: unknown) => {
      if (e instanceof AuthRequired) redirectToLogin();
    };
    await Promise.all([
      apiGet<unknown>("/api/dashboard/collection_runs?limit=100").then((d) => setRuns(rows<RunRow>(d)), runErr),
      apiGet<unknown>("/api/dashboard/scan_points").then((d) => setScans(rows<ScanPoint>(d)), runErr),
    ]);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const latest = new Map<string, RunRow>();
  for (const r of runs) {
    const cur = latest.get(r.domain);
    if (!cur || String(r.started_at) > String(cur.started_at)) latest.set(r.domain, r);
  }

  const domains = [...new Set([...scans.map((s) => s.domain), ...latest.keys()])].sort();

  const NO_RUN_COLOR = "var(--border)";
  const bars = domains.map((domain) => {
    const r = latest.get(domain);
    const bg = !r
      ? NO_RUN_COLOR
      : r.status === "success" || r.status === "completed"
        ? "var(--green)"
        : r.status === "failure" || r.status === "failed"
          ? "var(--red)"
          : "var(--yellow)";
    return { domain, bg };
  });

  const active = bars.filter((b) => b.bg !== NO_RUN_COLOR).length;
  const lastArr = runs
    .map((r) => String(r.started_at || ""))
    .filter(Boolean)
    .sort();
  const lastAt = lastArr.length ? lastArr[lastArr.length - 1] : "";
  const lastLabel = lastAt ? lastAt.replace("T", " ").slice(0, 16) : "—";
  const meta = domains.length > 0 ? `${active}/${domains.length} active · last ${lastLabel}` : "no sweep data";

  return (
    <div className="ib-sweep-strip">
      <span
        style={{
          fontSize: 9,
          fontWeight: 700,
          color: "var(--faint)",
          textTransform: "uppercase",
          letterSpacing: ".12em",
          marginRight: 8,
          whiteSpace: "nowrap",
        }}
      >
        Sweep Health
      </span>
      {bars.map((b) => (
        <div
          key={b.domain}
          title={b.domain}
          style={{
            height: 10,
            width: 16,
            borderRadius: 2,
            marginRight: 2,
            background: b.bg,
            flexShrink: 0,
          }}
        />
      ))}
      <div style={{ flex: 1 }} />
      <span
        style={{
          fontSize: 10,
          color: "var(--faint)",
          fontFamily: "'DM Mono', ui-monospace, monospace",
          whiteSpace: "nowrap",
        }}
      >
        {meta}
      </span>
    </div>
  );
}

/** New trust-signal pill (approved mockup's `.hd .role-badge` pattern) —
 *  surfaces infra-brain's core read-only guarantee (docs/READONLY-MODEL.md) in
 *  the chrome, alongside the existing system-health `<StatusPill>` (which
 *  reports operational status, a different signal from "this session can only
 *  read"). Static text: the specific DB role name isn't exposed to the
 *  dashboard API today (visual-restyle pass only, no backend change), so this
 *  states the guarantee generically rather than fabricating a role identifier. */
function RoleBadge() {
  return <span className="role-badge">read-only session · SELECT-only DB access</span>;
}

/** T0.2 — global cross-page search. Ports index.html L330-368 (markup) and
 *  L6202-6222 (filter + per-type caps). Datasets are fetched lazily on first
 *  focus and cached in state (never refetched per keystroke). Clicking a result
 *  deep-links via router state `detailOpen: {kind, data}`, which the Resources
 *  and Drift pages read on mount to open their DetailDrawer. */
function GlobalSearch() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [loaded, setLoaded] = useState(false);
  const loadingRef = useRef(false);
  // F-9: result buttons this render pass produced, in display order, so
  // ArrowDown/ArrowUp can move real DOM focus between them (same
  // ref-array + focus() pattern as components/ui/Tabs.tsx's roving
  // tab-strip navigation).
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const searchInputId = useId();
  const listboxId = useId();

  const [resources, setResources] = useState<Resource[]>([]);
  const [drift, setDrift] = useState<DriftEvent[]>([]);
  const [scripts, setScripts] = useState<GeneratedScript[]>([]);
  const [instincts, setInstincts] = useState<Instinct[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);

  const loadData = useCallback(() => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    const onErr = (e: unknown) => {
      if (e instanceof AuthRequired) redirectToLogin();
    };
    Promise.allSettled([
      apiGet<unknown>("/api/dashboard/resources?limit=500").then((d) => setResources(rows<Resource>(d)), onErr),
      apiGet<unknown>("/api/dashboard/drift_events?limit=500").then((d) => setDrift(rows<DriftEvent>(d)), onErr),
      apiGet<unknown>("/api/dashboard/generated_scripts").then((d) => setScripts(rows<GeneratedScript>(d)), onErr),
      apiGet<unknown>("/api/dashboard/instincts").then((d) => setInstincts(rows<Instinct>(d)), onErr),
      apiGet<unknown>("/api/dashboard/integration_proposals?limit=500").then((d) => setProposals(rows<Proposal>(d)), onErr),
    ]).finally(() => setLoaded(true));
  }, []);

  const clear = useCallback(() => setQuery(""), []);

  const q = query.trim().toLowerCase();
  const results: SearchResult[] = [];
  if (q) {
    resources
      .filter(
        (r) =>
          r.hostname.toLowerCase().includes(q) ||
          r.domain.toLowerCase().includes(q) ||
          r.resource_type.toLowerCase().includes(q),
      )
      .slice(0, 5)
      .forEach((r, i) =>
        results.push({
          key: `res-${i}`,
          type: "Resource",
          dot: "#4CBB6C",
          label: r.hostname,
          sub: `${r.domain} · ${r.resource_type}`,
          onClick: () => {
            navigate("/resources", { state: { detailOpen: { kind: "resource", data: r } } });
            clear();
          },
        }),
      );
    drift
      .filter(
        (d) =>
          d.field_name.toLowerCase().includes(q) ||
          d.hostname.toLowerCase().includes(q) ||
          d.domain.toLowerCase().includes(q),
      )
      .slice(0, 5)
      .forEach((d, i) =>
        results.push({
          key: `drift-${i}`,
          type: "Drift",
          dot: "#D9A62E",
          label: d.field_name,
          sub: d.hostname,
          onClick: () => {
            navigate("/drift", { state: { detailOpen: { kind: "drift", data: d } } });
            clear();
          },
        }),
      );
    scripts
      .filter((s) => s.name.toLowerCase().includes(q) || s.purpose.toLowerCase().includes(q))
      .slice(0, 4)
      .forEach((s, i) =>
        results.push({
          key: `script-${i}`,
          type: "Script",
          dot: "#5B9DE8",
          label: s.name,
          sub: `${s.created_by_agent} agent`,
          onClick: () => {
            navigate("/scripts");
            clear();
          },
        }),
      );
    instincts
      .filter((it) => it.pattern.toLowerCase().includes(q) || it.domain.toLowerCase().includes(q))
      .slice(0, 3)
      .forEach((it, i) =>
        results.push({
          key: `inst-${i}`,
          type: "Instinct",
          dot: "#8A8FA0",
          label: it.pattern,
          sub: `${it.domain} · ${it.confidence.toFixed(2)}`,
          onClick: () => {
            navigate("/instincts");
            clear();
          },
        }),
      );
    proposals
      .filter((p) => p.endpoint.toLowerCase().includes(q) || p.type.toLowerCase().includes(q))
      .slice(0, 3)
      .forEach((p, i) =>
        results.push({
          key: `int-${i}`,
          type: "Integration",
          dot: "#5B9DE8",
          label: p.endpoint,
          sub: p.type,
          onClick: () => {
            navigate("/intprops");
            clear();
          },
        }),
      );
  }

  const open = q.length > 0;
  const noResults = open && loaded && results.length === 0;

  // F-9: keyboard nav between the input and its result list. Real focus
  // moves onto the option buttons (they're natively focusable/activatable —
  // same "use a real interactive element" precedent as `FkCell`'s <button>),
  // so Tab already reaches every result; this just adds the conventional
  // listbox ArrowDown/ArrowUp/Escape shortcuts on top.
  const focusOption = (index: number) => {
    const el = optionRefs.current[index];
    el?.focus();
  };

  const handleInputKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" && results.length > 0) {
      e.preventDefault();
      focusOption(0);
    } else if (e.key === "Escape") {
      clear();
    }
  };

  const handleOptionKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focusOption((index + 1) % results.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (index === 0) {
        (document.getElementById(searchInputId) as HTMLInputElement | null)?.focus();
      } else {
        focusOption(index - 1);
      }
    } else if (e.key === "Escape") {
      clear();
      (document.getElementById(searchInputId) as HTMLInputElement | null)?.focus();
    }
  };

  return (
    <div style={{ flex: 1, maxWidth: 440, position: "relative", zIndex: 40 }}>
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="var(--faint)"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{
          width: 14,
          height: 14,
          position: "absolute",
          left: 11,
          top: "50%",
          transform: "translateY(-50%)",
          pointerEvents: "none",
          zIndex: 2,
        }}
      >
        <path d="M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.35-4.35" />
      </svg>
      <input
        id={searchInputId}
        value={query}
        onFocus={loadData}
        onChange={(e) => {
          if (!loaded) loadData();
          setQuery(e.target.value);
        }}
        onKeyDown={handleInputKeyDown}
        placeholder="Search resources, drift, scripts, instincts…"
        role="combobox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-autocomplete="list"
        autoComplete="off"
        style={{
          position: "relative",
          zIndex: 2,
          width: "100%",
          background: "var(--raised)",
          border: "1px solid var(--border)",
          borderRadius: 9,
          padding: "7px 12px 7px 33px",
          color: "var(--text)",
          fontSize: 12.5,
          outline: "none",
          fontFamily: "inherit",
        }}
      />
      {open && (
        <>
          <div onClick={clear} style={{ position: "fixed", inset: 0, zIndex: 1 }} />
          <div
            id={listboxId}
            role="listbox"
            aria-label="Search results"
            style={{
              position: "absolute",
              top: "calc(100% + 8px)",
              left: 0,
              right: 0,
              zIndex: 3,
              background: "var(--panel)",
              border: "1px solid var(--border)",
              borderRadius: 11,
              boxShadow: "0 16px 44px rgba(0,0,0,.55)",
              overflow: "hidden",
              maxHeight: "64vh",
              overflowY: "auto",
            }}
          >
            {results.map((r, i) => (
              <button
                key={r.key}
                type="button"
                role="option"
                aria-selected={false}
                ref={(el) => {
                  optionRefs.current[i] = el;
                }}
                onClick={r.onClick}
                onKeyDown={(e) => handleOptionKeyDown(e, i)}
                className="hd-search-option"
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 11,
                  width: "100%",
                  padding: "10px 14px",
                  cursor: "pointer",
                  borderBottom: "1px solid var(--border)",
                  border: "none",
                  borderLeft: "none",
                  borderRight: "none",
                  borderTop: "none",
                  background: "transparent",
                  font: "inherit",
                  textAlign: "left",
                  color: "inherit",
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(91,157,232,.08)")}
                onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
              >
                <div style={{ width: 7, height: 7, borderRadius: "50%", background: r.dot, flexShrink: 0 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 13,
                      color: "var(--text)",
                      fontWeight: 500,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {r.label}
                  </div>
                  <div
                    style={{
                      fontSize: 11,
                      color: "var(--faint)",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {r.sub}
                  </div>
                </div>
                <span
                  style={{
                    fontSize: 10,
                    fontWeight: 600,
                    color: "var(--muted)",
                    background: "var(--raised)",
                    border: "1px solid var(--border)",
                    borderRadius: 5,
                    padding: "2px 7px",
                    flexShrink: 0,
                  }}
                >
                  {r.type}
                </span>
              </button>
            ))}
            {noResults && (
              <div style={{ padding: 22, textAlign: "center", fontSize: 12, color: "var(--faint)" }}>No matches found</div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export function Topbar() {
  const location = useLocation();
  const clock = useClock();
  const { group, label } = findBreadcrumb(location.pathname);

  return (
    <div style={{ position: "sticky", top: 0, zIndex: 50 }}>
      <div
        className="hd"
        style={{
          background: "rgba(34,37,44,.82)",
          backdropFilter: "blur(10px)",
          WebkitBackdropFilter: "blur(10px)",
        }}
      >
        {/* `.hd .crumb` — approved mockup's header treatment (group › **page**) */}
        <span className="crumb" style={{ flexShrink: 0 }}>
          {group} <span style={{ color: "var(--border)", margin: "0 4px" }}>›</span>
          <b>{label}</b>
        </span>
        <GlobalSearch />
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexShrink: 0, marginLeft: "auto" }}>
          {/* `.hd .role-badge` — read-only trust signal (docs/READONLY-MODEL.md),
           *  shown alongside (not replacing) the system-health pill: the two
           *  report different things — this session's access level vs. overall
           *  system health. */}
          <RoleBadge />
          <StatusPill />
          <span style={{ fontSize: 12, color: "var(--faint)", fontFamily: "'DM Mono', ui-monospace, monospace" }}>
            {fmtClock(clock)}
          </span>
        </div>
      </div>
      <SweepHealthStrip />
    </div>
  );
}
