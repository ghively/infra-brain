import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { apiGet, apiPost, AuthRequired, apiPostRawStream, redirectToLogin } from "../api";
import { buildNavSections } from "../nav";
import type { VisibilityMap } from "../nav";
import { Skeleton } from "./Skeleton";
import { Icon } from "../lib/icons";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import type { Me } from "../AuthGate";

type Counts = Record<string, number | undefined>;
type ChatMsg = { role: "user" | "assistant"; content: string };

/** GET /api/dashboard/version — build/env metadata. */
type Version = { version: string; environment: string };

/** Derive the avatar initial(s) client-side (no `initial` field on /me):
 *  first letter of each of the first two words of `name`, uppercased; else
 *  the first letter of `username`; else 'U'. */
function initialsFrom(name: string | null, username: string | null): string {
  const n = name?.trim();
  if (n) {
    const initials = n
      .split(/\s+/)
      .slice(0, 2)
      .map((w) => w[0])
      .join("");
    if (initials) return initials.toUpperCase();
  }
  const u = username?.trim();
  if (u) return u[0].toUpperCase();
  return "U";
}

/** Nav route → v1 icon name (see `ICON_PATHS` in `lib/icons.tsx`). Every route
 *  now renders the SAME inline-SVG icon as the v1 legacy dashboard's `ICONS`
 *  map (index.html L5302-5362), rather than the older DC-sprite copies — full
 *  parity, offline/CSP-safe, no CDN, no emoji. `/graph` (Knowledge Graph) is
 *  rendered by the shared `Icon` via its `graph` special-case. `/sweeps` is a
 *  v2-only route with no v1 `ICONS` entry, so it keeps a small local inline
 *  chart SVG (see `NavIcon`). */
const NAV_ICON_NAMES: Record<string, string> = {
  "/":              "home",
  "/vulns":         "vulns",
  "/eol":           "eolcal",
  "/compl":         "compl",
  "/security":      "security",
  "/netdiscovery":  "netscan",
  "/hosts":         "resources",
  "/host-purpose-map": "tag",
  // "/software" ("box"), "/vsphere" ("vsphere") and "/cloud" ("cloud") were
  // removed with their pages — retired collectors, zero rows (see
  // RETIRED_SOURCES in nav.ts). The `Icon` glyphs themselves are untouched.
  "/homelab":       "resources",
  "/graph":         "graph",
  "/documents":     "docs",
  "/drift":         "drift",
  "/remed":         "remed",
  "/invrec":        "invrec",
  "/collruns":      "collruns",
  "/scanschedule":  "scan",
  "/notifications": "notify",
  "/iac":           "iac",
  "/agents":        "agents",
  "/activity":      "activity",
  "/decisions":     "decisions",
  "/instincts":     "instincts",
  "/observations":  "observations",
  "/scripts":       "scripts",
  "/intprops":      "intprops",
  "/settings":      "settings",
  "/agentconfig":   "settings",
  "/customviews":   "sparkles",
  "/savedviews":    "sparkles",
};

/** Sign out: revoke the session server-side (POST /api/dashboard/logout —
 *  clears the jti in Redis via revoke_session, see dashboard_auth.py) then send
 *  the user to the login page. Navigation happens unconditionally in `finally`
 *  — even if the network call fails, the user must not be left stuck looking
 *  logged-in; a plain `window.location.assign` is used rather than
 *  `redirectToLogin()` since that helper appends `?next=<current path>` (meant
 *  for an auth-expiry redirect a user should return from), which makes no
 *  sense for a deliberate sign-out. */
async function signOut() {
  try {
    await apiPost("/api/dashboard/logout");
  } catch {
    // Best-effort: the client-side cookie/redirect below is what actually
    // gets the user "logged out" from their perspective even if this fails.
  } finally {
    window.location.assign("/dashboard2/login");
  }
}

function NavIcon({ path, active }: { path: string; active: boolean }) {
  const color = active ? "var(--blue)" : "var(--faint)";
  // /sweeps is a v2-only route with no v1 ICONS entry — keep its local chart glyph.
  if (path === "/sweeps") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.8"
           strokeLinecap="round" strokeLinejoin="round"
           style={{ width: 14, height: 14, flexShrink: 0 }}>
        <path d="M3 3v18h18 M7 15l3-4 3 3 5-7" />
      </svg>
    );
  }
  const name = NAV_ICON_NAMES[path];
  if (!name) return null;
  // Icon renders stroke="currentColor"; drive active/inactive tint via `color`.
  return (
    <span style={{ color, display: "inline-flex", flexShrink: 0 }}>
      <Icon name={name} size={14} />
    </span>
  );
}

/** Streaming chat panel embedded in the sidebar — replicates legacy DC shell's
 *  "Ask Infra Brain" panel (shell.dc.html lines 247-284). Preserves the same
 *  POST /api/dashboard/chat streaming contract as ChatDrawer.tsx; the floating
 *  ChatDrawer is removed from App.tsx and replaced by this always-visible embed. */
function SidebarChat() {
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput]       = useState("");
  const [typing, setTyping]     = useState(false);
  const threadIdRef = useRef<string | null>(null);
  const msgsEndRef  = useRef<HTMLDivElement>(null);

  // Auto-scroll to newest message
  useEffect(() => {
    msgsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, typing]);

  async function send() {
    const inp = input.trim();
    if (!inp || typing) return;
    setMessages((m) => [...m, { role: "user", content: inp }, { role: "assistant", content: "" }]);
    setInput("");
    setTyping(true);
    const appendToken = (tok: string) =>
      setMessages((m) => {
        const copy = m.slice();
        const last = copy.length - 1;
        copy[last] = { ...copy[last], content: copy[last].content + tok };
        return copy;
      });
    try {
      let buf = "";
      for await (const chunk of apiPostRawStream("/api/dashboard/chat", {
        message: inp,
        thread_id: threadIdRef.current,
      })) {
        buf += chunk;
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
          if (!dataLine) continue;
          let payload: { thread_id?: string; token?: string };
          try { payload = JSON.parse(dataLine.slice(5).trim()); } catch { continue; }
          if (payload.thread_id) threadIdRef.current = payload.thread_id;
          if (payload.token) appendToken(payload.token);
        }
      }
      setMessages((m) => {
        const copy = m.slice();
        const last = copy.length - 1;
        if (!copy[last].content) copy[last] = { ...copy[last], content: "(no reply)" };
        return copy;
      });
    } catch (e) {
      if (e instanceof AuthRequired) { redirectToLogin(); return; }
      setMessages((m) => {
        const copy = m.slice();
        const last = copy.length - 1;
        copy[last] = { ...copy[last], content: "Sorry — unavailable." };
        return copy;
      });
    } finally {
      setTyping(false);
    }
  }

  return (
    <div className="ib-sidebar-chat">
      <div className="ib-sidebar-chat-header">
        <div className="ib-sidebar-chat-dot" />
        Ask Infra Brain
      </div>
      <div className="ib-sidebar-chat-msgs">
        {messages.length === 0 && (
          <div style={{ fontSize: 11, color: "var(--faint)", fontStyle: "italic" }}>
            Ask about your infra…
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} style={{
            fontSize: 11,
            lineHeight: 1.5,
            padding: "6px 9px",
            borderRadius: 8,
            background:  m.role === "user" ? "rgba(91,157,232,.14)" : "var(--raised)",
            color:        m.role === "user" ? "var(--text)" : "var(--muted)",
            border:      `1px solid ${m.role === "user" ? "rgba(91,157,232,.28)" : "var(--border)"}`,
            wordBreak: "break-word",
            alignSelf:   m.role === "user" ? "flex-end" : "flex-start",
            maxWidth: "92%",
          }}>
            {m.content || (typing && i === messages.length - 1 ? "…" : "")}
          </div>
        ))}
        {typing && messages.length === 0 && (
          <div style={{ fontSize: 11, color: "var(--faint)", fontStyle: "italic" }}>Thinking…</div>
        )}
        <div ref={msgsEndRef} />
      </div>
      <div className="ib-sidebar-chat-input-row">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") send(); }}
          placeholder="Ask about your infra…"
          style={{
            flex: 1,
            minWidth: 0,
            background: "var(--raised)",
            border: "1px solid var(--border)",
            borderRadius: 7,
            padding: "6px 9px",
            color: "var(--text)",
            fontSize: 12,
            outline: "none",
            fontFamily: "inherit",
          }}
        />
        <button
          onClick={send}
          disabled={typing}
          style={{
            width: 32,
            height: 32,
            borderRadius: 7,
            background: "var(--blue)",
            border: "none",
            cursor: typing ? "default" : "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
            opacity: typing ? 0.6 : 1,
            boxShadow: "0 2px 8px rgba(91,157,232,.35)",
          }}
          aria-label="Send"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round"
               style={{ width: 11, height: 11, marginLeft: 1 }}>
            <path d="M22 2L11 13 M22 2l-7 20-4-9-9-4 20-7z" />
          </svg>
        </button>
      </div>
    </div>
  );
}

/** The dashboard-app navigation shell (MR-I / FE-1, FE-10).
 *
 * Design-foundation update (feat/dashboard2-design-foundation):
 *  - Added per-item SVG icons matching legacy shell.dc.html symbol defs.
 *  - Restored gradient brand logo box with box-shadow (legacy line 200).
 *  - Embedded the "Ask Infra Brain" chat panel at the bottom of the sidebar
 *    (replacing the hidden floating ChatDrawer — legacy lines 247-284).
 */
/** `initialMe` (TRK-236): the caller (App.tsx's AuthenticatedLayout) already
 *  resolved GET /api/dashboard/me via AuthGate before Sidebar ever mounted —
 *  passing it in here means Sidebar does not re-fetch /me itself, so an
 *  authenticated page load makes the same number of /me calls as before (one
 *  — just moved earlier), not one extra. When the prop is omitted (e.g. in
 *  tests that render Sidebar standalone, without AuthGate), Sidebar falls
 *  back to fetching /me itself exactly as it always has. */
export function Sidebar({ initialMe }: { initialMe?: Me | null } = {}) {
  const location = useLocation();
  const [counts, setCounts] = useState<Counts>({});
  const [me, setMe] = useState<Me | null>(initialMe ?? null);
  const [version, setVersion] = useState<Version | null>(null);
  const [identityLoading, setIdentityLoading] = useState(true);
  const [visibility, setVisibility] = useState<VisibilityMap>({});

  // F-7: this used to be a bare mount-once useEffect — Sidebar persists for
  // the entire session, so nav badge counts reflected the moment of login
  // indefinitely. Wired into useAutoRefresh (quiet 5-min background refresh,
  // same as every page loader) so the badges stay live.
  const loadCounts = useCallback(async () => {
    const c = await apiGet<Counts>("/api/dashboard/counts");
    setCounts(c);
  }, []);

  useEffect(() => {
    loadCounts().catch(() => void 0);
  }, [loadCounts]);
  useAutoRefresh(loadCounts);

  // Which data sources this deployment actually has. The sidebar used to be a
  // hardcoded page list that advertised vSphere/Rapid7/Cloud integrations this
  // homelab will never configure; it is now derived from the backend's own
  // retirement state (see nav.ts and api/source_visibility.py).
  //
  // Fetched once per mount, not on the auto-refresh tick: a collector being
  // retired or revived is a config change, not live telemetry, and having nav
  // items appear and disappear under the cursor every five minutes would be
  // worse than being one reload stale.
  //
  // A failure leaves `visibility` empty, and `buildNavSections({}, …)` treats
  // an unreported domain as visible — so a flaky request degrades to the full
  // nav, never to a blank sidebar.
  useEffect(() => {
    let cancelled = false;
    apiGet<{ items: { domain: string; visibility: string }[] }>("/api/dashboard/sources")
      .then((res) => {
        if (cancelled) return;
        const map: VisibilityMap = {};
        for (const item of res.items ?? []) {
          map[item.domain] = item.visibility as VisibilityMap[string];
        }
        setVisibility(map);
      })
      .catch(() => void 0);
    return () => {
      cancelled = true;
    };
  }, []);

  const navSections = buildNavSections(undefined, visibility);

  useEffect(() => {
    let cancelled = false;
    const meFetch: Promise<Me | null> =
      initialMe !== undefined ? Promise.resolve(initialMe) : apiGet<Me>("/api/dashboard/me");
    Promise.all([meFetch, apiGet<Version>("/api/dashboard/version")])
      .then(([meRes, verRes]) => {
        if (cancelled) return;
        setMe(meRes);
        setVersion(verRes);
      })
      .catch((e) => {
        if (e instanceof AuthRequired) {
          redirectToLogin();
          return;
        }
        // Non-auth failure: degrade to neutral placeholders (never crash,
        // never fall back to a hardcoded identity).
        if (!cancelled) {
          setMe(null);
          setVersion(null);
        }
      })
      .finally(() => {
        if (!cancelled) setIdentityLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- initialMe is a
    // one-shot value from the parent's already-resolved auth check; it is
    // not expected to change identity across re-renders of the same mount.
  }, []);

  const displayName = me?.name?.trim() || me?.username?.trim() || "User";
  const avatarInitial = initialsFrom(me?.name ?? null, me?.username ?? null);
  const versionLine = version ? `v${version.version} · ${version.environment}` : null;
  const role = me?.role?.trim() || null;
  // Sub line: role and/or version·env, whichever are available.
  const subLine = [role, versionLine].filter(Boolean).join(" · ") || null;

  return (
    <nav className="ib-sidebar" aria-label="Primary">
      {/* Brand logo — flat dark-token chip, no gradient/glow (new design system) */}
      <div className="ib-sidebar-brand">
        <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
          <div style={{
            width: 34, height: 34,
            borderRadius: 8,
            background: "var(--raised)",
            border: "1px solid var(--border)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 17, flexShrink: 0,
          }}>
            🧠
          </div>
          <div>
            <div className="wordmark">
              Infra Brain
            </div>
            <div style={{ fontSize: 10, fontWeight: 500, color: "var(--faint)", textTransform: "uppercase", letterSpacing: ".1em", marginTop: 1 }}>
              dashboard
            </div>
          </div>
        </div>
      </div>

      {/* Scrollable nav */}
      <div className="ib-sidebar-nav">
        {navSections.map((section) => (
          <div className="ib-nav-section" key={section.id}>
            <div className="ib-nav-heading">{section.heading}</div>
            {section.items.map((item) => {
              const active =
                item.path === "/"
                  ? location.pathname === "/"
                  : location.pathname.startsWith(item.path);
              const badge = item.countKey ? counts[item.countKey] : undefined;
              return (
                <Link
                  key={item.path}
                  to={item.path}
                  className={`ib-nav-link${active ? " active" : ""}`}
                  aria-current={active ? "page" : undefined}
                  title={
                    item.historical
                      ? "Retired collector — the data shown is historical and no longer updating"
                      : undefined
                  }
                >
                  <NavIcon path={item.path} active={active} />
                  <span style={{ flex: 1, whiteSpace: "nowrap" }}>{item.label}</span>
                  {/* Retired-but-still-holding-data sources stay reachable (the
                    * rows are real) but must never read as a live feed — see the
                    * `historical` branch of api/source_visibility.py. */}
                  {item.historical && (
                    <span className="ib-nav-badge" data-historical="true" title="historical data">
                      hist
                    </span>
                  )}
                  {typeof badge === "number" && badge > 0 && (
                    <span className="ib-nav-badge">{badge > 999 ? "999+" : badge}</span>
                  )}
                </Link>
              );
            })}
          </div>
        ))}
      </div>

      {/* User card + version/env subtitle (T0.4). Identity and version now come
       *  from live endpoints (/api/dashboard/me, /api/dashboard/version); the
       *  avatar initial is derived client-side. The identity block sits inside a
       *  rounded pill container per spec. */}
      <div
        style={{
          padding: "10px 12px",
          borderTop: "1px solid var(--border)",
          flexShrink: 0,
        }}
      >
       <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 9,
          background: "var(--raised)",
          borderRadius: 9,
          padding: "8px 10px",
        }}
      >
        <div
          style={{
            width: 26,
            height: 26,
            borderRadius: "50%",
            background: "var(--blue)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 11,
            fontWeight: 700,
            color: "#fff",
            flexShrink: 0,
          }}
        >
          {identityLoading ? "" : avatarInitial}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          {identityLoading ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              <Skeleton width={90} height={10} radius={4} />
              <Skeleton width={64} height={8} radius={4} />
            </div>
          ) : (
            <>
              <div
                style={{
                  fontSize: 12,
                  fontWeight: 600,
                  color: "var(--text)",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {displayName}
              </div>
              {subLine && (
                <div
                  style={{
                    fontSize: 10,
                    color: "var(--faint)",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {subLine}
                </div>
              )}
            </>
          )}
        </div>
        <button
          type="button"
          onClick={() => void signOut()}
          title="Sign out"
          aria-label="Sign out"
          style={{
            color: "var(--faint)",
            display: "flex",
            alignItems: "center",
            background: "none",
            border: "none",
            padding: 0,
            cursor: "pointer",
          }}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
               strokeLinecap="round" strokeLinejoin="round" style={{ width: 14, height: 14 }}>
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4 M16 17l5-5-5-5 M21 12H9" />
          </svg>
        </button>
       </div>
      </div>

      {/* Always-visible embedded chat panel */}
      <SidebarChat />
    </nav>
  );
}
