import { useCallback, useEffect, useState } from "react";
import {
  apiGet,
  apiPost,
  rows,
  ApiError,
  AuthRequired,
  redirectToLogin,
  type McpKey,
  type McpToolCatalog,
  type McpKeyCreateResult,
} from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
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

/** The MCP server is a separate process/port from the dashboard (mcp_server.py,
 *  streamable-http transport, see docs/MCP_SERVER.md) — same host, port 8002,
 *  path /mcp. Derived from the current page's hostname rather than hardcoded so
 *  this stays correct across environments (dev/staging/prod all use the same
 *  host, only the port differs from the dashboard's own). */
function mcpServerUrl(): string {
  return `${window.location.protocol}//${window.location.hostname}:8002/mcp`;
}

/** Claude Code's mcpServers config shape for an HTTP-transport server (see
 *  docs/MCP_SERVER.md "Connecting from Claude Code"). `token` is a placeholder
 *  string like "<your-token>" for the generic reference panel, or a real raw
 *  token for the one-time post-create snippet — exported for Login.test.tsx-style
 *  unit coverage. */
export function mcpClientConfig(url: string, token: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        "infra-brain": {
          type: "streamable-http",
          url,
          headers: { Authorization: `Bearer ${token}` },
        },
      },
    },
    null,
    2,
  );
}

/** `created_at`/`last_used_at` formatting — same truncate-to-second pattern used
 *  across the dashboard (AgentConfig.tsx, CollRuns.tsx, Sweeps.tsx `fmtDt`).
 *  Returns `undefined` (not a placeholder string) for a missing value so
 *  `DataTable`'s NULL contract renders the dim-italic NULL token automatically
 *  — a key that has never been used genuinely has no last-used value yet. */
function fmtDt(s?: string | null): string | undefined {
  if (!s) return undefined;
  return s.length > 19 ? s.slice(0, 10) + " " + s.slice(11, 19) : s;
}

/** Upper bound on the optional expiry, mirroring mcp_auth.MAX_EXPIRES_DAYS.
 *  The server is the enforcer (a bad value is a 422 either way) — this only
 *  exists so the form disables Create instead of round-tripping a rejection. */
const MAX_EXPIRES_DAYS = 3650;

/** Parse the optional "expires in N days" field. Returns:
 *   - `null`  → left blank: no expiry (the default, and back-compatible)
 *   - number  → a valid in-range day count
 *   - `"bad"` → present but not a valid positive in-range integer
 *  Exported for unit coverage — the three outcomes are the whole contract. */
export function parseExpiresDays(raw: string): number | null | "bad" {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (!/^\d+$/.test(trimmed)) return "bad";
  const n = Number(trimmed);
  if (n < 1 || n > MAX_EXPIRES_DAYS) return "bad";
  return n;
}

const CODE_BLOCK_STYLE: React.CSSProperties = {
  display: "block",
  fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
  fontSize: 12,
  color: "var(--ib-text)",
  background: "var(--ib-raised)",
  border: "1px solid var(--ib-border)",
  borderRadius: "var(--ib-radius)",
  padding: "10px 12px",
  whiteSpace: "pre",
  overflowX: "auto",
};

/** Domain-grouped tool picker (replaces the old flat 3-column checkbox grid —
 *  see docs/superpowers/specs/2026-07-30-frontend-audit-report.md Phase 3).
 *  Falls back to two groups ("Read-only tools"/"Mutation tools") if the
 *  backend hasn't been redeployed with `groups` yet, so this degrades
 *  gracefully during a rolling deploy rather than crashing on `undefined`. */
function ToolPicker({
  catalog,
  selected,
  toggle,
}: {
  catalog: McpToolCatalog;
  selected: Set<string>;
  toggle: (t: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());

  const groupEntries: [string, string[]][] = catalog.groups
    ? Object.entries(catalog.groups)
    : [
        ["Read-only tools", catalog.readonly],
        ["Mutation tools", catalog.mutation],
      ];

  const mutationSet = new Set(catalog.mutation);
  const q = filter.trim().toLowerCase();
  const searching = q.length > 0;

  const toggleGroup = (g: string) =>
    setOpenGroups((prev) => {
      const next = new Set(prev);
      next.has(g) ? next.delete(g) : next.add(g);
      return next;
    });

  return (
    <div>
      <input
        placeholder="Search tools…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        style={{
          width: "100%",
          boxSizing: "border-box",
          background: "var(--ib-raised)",
          border: "1px solid var(--ib-border)",
          borderRadius: "var(--ib-radius-sm)",
          padding: "8px 12px",
          fontSize: 12,
          color: "var(--ib-text)",
          fontFamily: "inherit",
          marginBottom: 12,
        }}
      />

      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {groupEntries.map(([group, allNames]) => {
          const names = searching ? allNames.filter((n) => n.toLowerCase().includes(q)) : allNames;
          if (searching && names.length === 0) return null;

          const isMutation = allNames.some((n) => mutationSet.has(n));
          const selectedCount = allNames.filter((n) => selected.has(n)).length;
          const isOpen = searching || openGroups.has(group);

          return (
            <div
              key={group}
              style={{
                border: "1px solid var(--ib-border)",
                borderRadius: "var(--ib-radius-sm)",
                overflow: "hidden",
              }}
            >
              <button
                type="button"
                onClick={() => toggleGroup(group)}
                style={{
                  width: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 8,
                  padding: "7px 10px",
                  background: "var(--ib-raised)",
                  border: "none",
                  cursor: "pointer",
                  fontFamily: "inherit",
                  textAlign: "left",
                }}
              >
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: isMutation ? "var(--ib-yellow)" : "var(--ib-text)",
                  }}
                >
                  {isOpen ? "▾" : "▸"} {group}
                </span>
                <span style={{ fontSize: 10, color: "var(--ib-muted)" }}>
                  {selectedCount > 0 ? `${selectedCount}/${allNames.length} selected` : allNames.length}
                </span>
              </button>

              {isOpen && (
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
                    gap: 6,
                    padding: "8px 10px",
                  }}
                >
                  {names.map((t) => (
                    <label
                      key={t}
                      style={{
                        fontSize: 12,
                        color: isMutation ? "var(--ib-yellow)" : "var(--ib-muted)",
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                      }}
                    >
                      <input type="checkbox" checked={selected.has(t)} onChange={() => toggle(t)} /> {t}
                    </label>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** MCP scoped-API-key management (spec Part 1 §Key management UI). Create keys
 *  scoped to a tool set, list existing keys (name/created/last-used/revoked/
 *  tool-count), and revoke. The raw token is shown exactly once after create
 *  with a copy button and a "you will not see this again" warning; only its
 *  hash is stored server-side. Session-gated (create/revoke require admin).
 *
 *  Also shows how to actually USE a key: a ready-to-paste client config next
 *  to the freshly created token, plus a collapsed "Connecting a client" note
 *  next to the create form for connecting later or from another client — kept
 *  subordinate to (and, once a real token exists, hidden in favor of) the
 *  create form and the freshly-created-token panel, which are the primary
 *  content on this page.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md) — Phase 3
 *  mechanical PageShell/Panel/DataTable conversion. All prior feature/bugfix
 *  behavior is preserved: the 403-vs-error distinction for the admin-only key
 *  list (`keysForbidden`), the mutation-scoped error state that must not blank
 *  the already-loaded page on a failed revoke, and the fresh-token-vs-generic
 *  "Connecting a client" note subordination. */
export function McpKeys() {
  const [keys, setKeys] = useState<McpKey[] | null>(null);
  const [catalog, setCatalog] = useState<McpToolCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Set when the key LIST fetch (GET /api/dashboard/mcp-keys, admin-only)
  // specifically fails with 403 — an expected state for a non-admin session,
  // not a page-load failure. Kept separate from `error` (below) so a non-admin
  // still gets the page shell (connection instructions, gated create form)
  // instead of the whole page blanking to an error string. `error` is reserved
  // for a genuine failure to load — the tools catalog (required for the page
  // shell) failing, or the key list failing with anything other than 403.
  const [keysForbidden, setKeysForbidden] = useState(false);
  // Mutation-scoped error (create/revoke), distinct from the page-load `error`
  // above. A failed revoke (403 for a non-admin, 404 on a double-click, a
  // transient network blip) must not blank out a successfully-loaded page —
  // see the bug this fixes: revoke() used to reuse `error`, which gates the
  // entire render below.
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [name, setName] = useState("");
  // Optional expiry (TRK-160). Kept as the raw string so the field can be
  // legitimately empty — "" means "never expires", which is the default and
  // must stay effortless to choose.
  const [expiresDays, setExpiresDays] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [freshToken, setFreshToken] = useState<McpKeyCreateResult | null>(null);

  const load = useCallback(async () => {
    // Tools catalog is unauthenticated-admin (session-only) and required for
    // the page shell itself (create form, connection instructions) — any
    // failure here is a genuine full-page load failure.
    try {
      setCatalog(await apiGet<McpToolCatalog>("/api/dashboard/mcp-keys/tools"));
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
      return;
    }

    // Key list is admin-only. A 403 here is a normal non-admin session, not a
    // load failure — render the shell without the key table / with creation
    // gated, rather than blanking the whole page.
    try {
      const d = await apiGet<unknown>("/api/dashboard/mcp-keys");
      setKeys(rows<McpKey>(d));
      setKeysForbidden(false);
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else if (e instanceof ApiError && e.status === 403) setKeysForbidden(true);
      else setError(String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const toggle = (t: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(t) ? next.delete(t) : next.add(t);
      return next;
    });

  const selectGroup = (names: string[]) =>
    setSelected((prev) => new Set([...prev, ...names]));

  const create = async () => {
    setMutationError(null);
    const days = parseExpiresDays(expiresDays);
    if (days === "bad") {
      setMutationError(`Expiry must be a whole number of days between 1 and ${MAX_EXPIRES_DAYS}.`);
      return;
    }
    try {
      const res = await apiPost<McpKeyCreateResult>("/api/dashboard/mcp-keys", {
        name,
        allowed_tools: [...selected],
        // Omit the field entirely when blank rather than sending null — the
        // request body then looks exactly like a pre-TRK-160 one.
        ...(days === null ? {} : { expires_days: days }),
      });
      setFreshToken(res);
      setName("");
      setExpiresDays("");
      setSelected(new Set());
      await load();
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setMutationError(String(e));
    }
  };

  const revoke = async (id: string) => {
    setMutationError(null);
    try {
      await apiPost(`/api/dashboard/mcp-keys/${id}/revoke`);
      await load();
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setMutationError(String(e));
    }
  };

  const h = headerFor("mcpkeys");

  if (error) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <EmptyState kind="error" title="MCP keys failed to load" hint={error} />
      </div>
    );
  }
  // Only the catalog gates the whole page — it's required for the shell
  // (create form + connection instructions) that every session, admin or not,
  // should see. `keys` legitimately stays null for a non-admin (keysForbidden)
  // or briefly while its own fetch is still in flight; neither blocks the shell.
  if (catalog === null) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const expiryParsed = parseExpiresDays(expiresDays);
  const canCreate = !keysForbidden && !!name && selected.size > 0 && expiryParsed !== "bad";

  const columns: ColumnDef<McpKey>[] = [
    { key: "name", header: "Name", typeGlyph: "pk" },
    { key: "created_at", header: "Created", typeGlyph: "text", render: (k) => fmtDt(k.created_at) },
    { key: "created_by", header: "By", typeGlyph: "text" },
    { key: "last_used_at", header: "Last used", typeGlyph: "text", render: (k) => fmtDt(k.last_used_at) },
    { key: "allowed_tools_count", header: "Tools", typeGlyph: "num" },
    {
      // Deliberately renders the literal word "never" instead of leaving the
      // value undefined for DataTable's dim-italic NULL token (as last_used_at
      // does above). A null last_used_at is genuinely absent data; a null
      // expires_at is a POLICY — this key never expires — and reading that as
      // "unknown" would be the opposite of the truth.
      key: "expires_at",
      header: "Expires",
      typeGlyph: "text",
      render: (k) => fmtDt(k.expires_at) ?? "never",
    },
    {
      key: "revoked",
      header: "Status",
      typeGlyph: "enum",
      // Three states, not two. Revoked outranks expired: if someone
      // deliberately killed the key, that is the fact worth surfacing, even
      // if the clock also ran out afterwards. `expired` comes from the server
      // (computed with the predicate auth enforces), never recomputed here.
      render: (k) =>
        k.revoked ? (
          <Badge tone="err">revoked</Badge>
        ) : k.expired ? (
          <Badge tone="warn">expired</Badge>
        ) : (
          <Badge tone="ok">active</Badge>
        ),
    },
    {
      key: "actions",
      header: "",
      typeGlyph: "text",
      sortable: false,
      render: (k) =>
        !k.revoked ? (
          <Button variant="danger" size="sm" onClick={() => void revoke(k.id)}>
            Revoke
          </Button>
        ) : null,
    },
  ];

  return (
    <div>
      <PageShell title={h.title} description={h.desc} />

      {mutationError && (
        <div style={{ marginBottom: 16 }}>
          <EmptyState kind="error" title="Mutation failed" hint={mutationError} />
        </div>
      )}

      {/* Primary content: the create form comes first — it's the cause, not
       *  the aftermath. The "Connecting a client" note lives inside it,
       *  collapsed by default, as a subordinate reference rather than a
       *  peer-weight panel. */}
      <div style={{ marginBottom: 20 }}>
        <Panel mnemonic="MCP" description="Create a new scoped API key">
          <div style={{ padding: "16px" }}>
            {keysForbidden ? (
              <div
                style={{
                  fontSize: 12,
                  color: "var(--ib-blue)",
                  background: "var(--ib-raised)",
                  border: "1px solid var(--ib-border)",
                  borderRadius: "var(--ib-radius)",
                  padding: "10px 14px",
                }}
              >
                Creating and managing MCP keys requires admin access. Ask an admin to create a
                key for you, or see the connection instructions below to use one you already
                have.
              </div>
            ) : (
              <>
                <input
                  placeholder="Key name (e.g. infra-ops-prod-subnet-a)"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  style={{
                    width: "100%",
                    boxSizing: "border-box",
                    background: "var(--ib-raised)",
                    border: "1px solid var(--ib-border)",
                    borderRadius: "var(--ib-radius-sm)",
                    padding: "9px 12px",
                    fontSize: 13,
                    color: "var(--ib-text)",
                    outline: "none",
                    fontFamily: "inherit",
                    marginBottom: 14,
                  }}
                />

                {/* Optional expiry (TRK-160). Blank is a first-class choice and
                 *  the default — a key with no expiry behaves exactly as every
                 *  key did before this field existed. */}
                <div style={{ marginBottom: 14 }}>
                  <input
                    type="number"
                    min={1}
                    max={MAX_EXPIRES_DAYS}
                    placeholder={`Expires in N days (optional — blank means never)`}
                    aria-label="Expires in days"
                    value={expiresDays}
                    onChange={(e) => setExpiresDays(e.target.value)}
                    style={{
                      width: "100%",
                      boxSizing: "border-box",
                      background: "var(--ib-raised)",
                      border: `1px solid ${expiryParsed === "bad" ? "var(--ib-red)" : "var(--ib-border)"}`,
                      borderRadius: "var(--ib-radius-sm)",
                      padding: "9px 12px",
                      fontSize: 13,
                      color: "var(--ib-text)",
                      outline: "none",
                      fontFamily: "inherit",
                    }}
                  />
                  <div style={{ fontSize: 11, color: expiryParsed === "bad" ? "var(--ib-red)" : "var(--ib-muted)", marginTop: 5 }}>
                    {expiryParsed === "bad"
                      ? `Enter a whole number of days between 1 and ${MAX_EXPIRES_DAYS}, or leave blank.`
                      : "After this many days the key stops authenticating on its own — a leaked key that nobody notices still dies."}
                  </div>
                </div>

                <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
                  <Button variant="secondary" size="sm" onClick={() => selectGroup(catalog.readonly)}>
                    Select all read-only
                  </Button>
                  <Button variant="secondary" size="sm" onClick={() => selectGroup(catalog.mutation)}>
                    Select all mutation
                  </Button>
                </div>

                <ToolPicker catalog={catalog} selected={selected} toggle={toggle} />

                <div style={{ marginTop: 16 }}>
                  <Button variant="primary" disabled={!canCreate} onClick={() => void create()}>
                    Create key
                  </Button>
                </div>
              </>
            )}

            {/* Once a key was just created, the panel below already shows this
             *  exact config with a real token — showing the generic placeholder
             *  version too would be a redundant near-duplicate, so it steps aside
             *  until freshToken is dismissed. */}
            {!freshToken && (
              <details style={{ marginTop: 16, paddingTop: 14, borderTop: "1px solid var(--ib-border)" }}>
                <summary style={{ cursor: "pointer", fontSize: 11, fontWeight: 600, color: "var(--ib-muted)" }}>
                  Connecting a client
                </summary>
                <div style={{ marginTop: 10 }}>
                  <div style={{ fontSize: 12, color: "var(--ib-muted)", marginBottom: 10 }}>
                    The MCP server is a separate process on port 8002 (streamable-http transport,
                    see docs/MCP_SERVER.md). Create a key above to get a token, then use it in a
                    config shaped like this — the format Claude Code and other MCP clients expect:
                  </div>
                  <code style={CODE_BLOCK_STYLE}>{mcpClientConfig(mcpServerUrl(), "<your-token>")}</code>
                </div>
              </details>
            )}
          </div>
        </Panel>
      </div>

      {/* Result of a successful create — appears here, right after its cause. */}
      {freshToken && (
        <div style={{ marginBottom: 20 }}>
          <Panel rail="warn" mnemonic="TOKEN" description="Copy this token now — you will not see it again">
            <div style={{ padding: "0 16px 16px" }}>
              <code style={{ fontFamily: "var(--ib-mono, 'DM Mono', monospace)", color: "var(--ib-text)", wordBreak: "break-all" }}>
                {freshToken.token}
              </code>
              {/* The computed absolute expiry, echoed back by the server — the
               *  operator should see WHEN this dies, not the day count they
               *  typed, and "never" spelled out when they left it blank. */}
              <div style={{ marginTop: 8, fontSize: 12, color: "var(--ib-muted)" }}>
                Expires: <span style={{ color: "var(--ib-text)" }}>{fmtDt(freshToken.expires_at) ?? "never"}</span>
              </div>
              <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
                <Button variant="secondary" size="sm" onClick={() => void navigator.clipboard.writeText(freshToken.token)}>
                  Copy token
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setFreshToken(null)}>
                  Dismiss
                </Button>
              </div>

              <div style={{ marginTop: 14, fontWeight: 700, color: "var(--ib-yellow)", marginBottom: 6, fontSize: 12 }}>
                Add this to your MCP client's config
              </div>
              <code style={CODE_BLOCK_STYLE}>{mcpClientConfig(mcpServerUrl(), freshToken.token)}</code>
              <div style={{ marginTop: 8 }}>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => void navigator.clipboard.writeText(mcpClientConfig(mcpServerUrl(), freshToken.token))}
                >
                  Copy config
                </Button>
              </div>
            </div>
          </Panel>
        </div>
      )}

      {/* Key list/table is admin-only data (GET /api/dashboard/mcp-keys 403s for
       *  non-admin) — hidden rather than rendered empty when keysForbidden,
       *  distinct from the "still loading" case (keys === null, !keysForbidden)
       *  which gets a skeleton instead of silently showing nothing. */}
      {keysForbidden ? (
        <EmptyState kind="none-yet" title="Existing keys are only visible to admins" />
      ) : keys === null ? (
        <Skeleton count={3} height={32} />
      ) : (
        <Panel mnemonic="MCP_KEYS" description="mcp_keys" live>
          {keys.length === 0 ? (
            <EmptyState kind="none-yet" title="No MCP keys created yet" hint="Create one above to get started." />
          ) : (
            <DataTable<McpKey>
              columns={columns}
              rows={keys}
              rowKey={(k) => k.id}
              sortable
              initialSortKey="name"
              toolbarName="mcp_keys"
              caption="MCP API keys: name, created, creator, last used, tool count, expiry, and status"
              statusBar={{ shown: keys.length, note: "read-only · no mutations possible from this view" }}
            />
          )}
        </Panel>
      )}
    </div>
  );
}
