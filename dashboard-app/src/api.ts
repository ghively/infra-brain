/** The single fetch chokepoint. Every network call in dashboard-app goes through
 *  apiGet/apiPost. Rules (Wave 6.1 / F-023): no raw fetch() anywhere else; no
 *  catch-and-ignore; envelope vs array is normalized HERE, never at call sites. */

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly url: string,
  ) {
    super(`${status} on ${url}`);
  }
}

export class AuthRequired extends Error {}

/** Observable per-endpoint fetch-error store (Wave 6.1 / F-023 — port of v1
 *  index.html `_fetchErrs` Map + `_noteFetchError`/`_clearFetchError`,
 *  L3919-3938). The request() chokepoint below NOTES a failure keyed by path on
 *  a non-401 error and CLEARS it on the next success to the same path — this is
 *  what makes the bottom "failing endpoints" banner self-healing. It is a pure
 *  side-channel for observation: it never swallows or alters the thrown
 *  ApiError/AuthRequired. 401/AuthRequired is deliberately NOT noted (an
 *  unauthenticated session is handled by the login redirect, not an error
 *  banner). Snapshots are cached so the store can back React's
 *  useSyncExternalStore without tripping its "getSnapshot must be stable" guard. */
class FetchErrorStore {
  private map = new Map<string, string>();
  private listeners = new Set<() => void>();
  private snapshot: ReadonlyArray<readonly [string, string]> = [];

  /** Record that `path` is currently failing with `msg`. */
  note = (path: string, msg: string): void => {
    if (this.map.get(path) === msg) return;
    this.map.set(path, msg);
    this.commit();
  };

  /** Clear a previously-noted failure (called on the next success to `path`). */
  clear = (path: string): void => {
    if (this.map.delete(path)) this.commit();
  };

  /** Cached snapshot of the currently-failing endpoints. Stable between changes. */
  getAll = (): ReadonlyArray<readonly [string, string]> => this.snapshot;

  /** Subscribe to change notifications; returns an unsubscribe fn. */
  subscribe = (cb: () => void): (() => void) => {
    this.listeners.add(cb);
    return () => {
      this.listeners.delete(cb);
    };
  };

  private commit(): void {
    this.snapshot = [...this.map.entries()];
    for (const l of this.listeners) l();
  }
}

export const fetchErrors = new FetchErrorStore();

/** Ambient "quiet" depth. While > 0, request() treats every call as quiet: it
 *  neither NOTES nor CLEARS the fetchErrors store, so a transient background
 *  refresh cannot flash (or clear) the error banners. useAutoRefresh wraps its
 *  loader in runQuiet() so a page's ordinary apiGet calls become quiet during a
 *  5-minute background refresh without the page changing anything. Throw
 *  semantics are UNCHANGED — ApiError/AuthRequired still propagate. */
let quietDepth = 0;

/** Run `fn` with ambient quiet mode enabled (see quietDepth). Rethrows anything
 *  `fn` throws — this only suppresses the fetch-error side-channel, never errors. */
export function runQuiet<T>(fn: () => Promise<T>): Promise<T> {
  quietDepth++;
  const release = (): void => {
    quietDepth--;
  };
  return fn().then(
    (v) => {
      release();
      return v;
    },
    (e) => {
      release();
      throw e;
    },
  );
}

async function request<T>(url: string, init?: RequestInit, quiet = false): Promise<T> {
  const silent = quiet || quietDepth > 0;
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    if (!silent) fetchErrors.note(url, "network unreachable");
    throw new ApiError(0, url); // network failure — always surfaced, never swallowed
  }
  if (res.status === 401) throw new AuthRequired(); // auth: never noted (login redirect handles it)
  if (!res.ok) {
    if (!silent) fetchErrors.note(url, `HTTP ${res.status}`);
    throw new ApiError(res.status, url);
  }
  if (!silent) fetchErrors.clear(url); // success → self-heal any prior failure for this path
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Options for a GET. `quiet: true` opts this single call out of the fetch-error
 *  store (no note on failure, no clear on success) — the same effect runQuiet()
 *  applies ambiently. Existing callers pass no options and are unaffected. */
export type ApiGetOptions = { quiet?: boolean };

export function apiGet<T>(url: string, opts?: ApiGetOptions): Promise<T> {
  return request<T>(url, undefined, opts?.quiet ?? false);
}

export function apiPost<T>(url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function apiPatch<T>(url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method: "PATCH",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function apiPut<T>(url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method: "PUT",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function apiDelete(url: string): Promise<void> {
  return request<void>(url, { method: "DELETE" });
}

/** DELETE that returns a body. Used by the settings catalog's "revert to
 *  default", which answers with the RECOMPUTED entry — the point of the button
 *  is to show you the value (and source) you fell back to, so discarding the
 *  response and re-fetching the whole catalog would be both slower and racier. */
export function apiDeleteJson<T>(url: string): Promise<T> {
  return request<T>(url, { method: "DELETE" });
}

/** Host purpose/VLAN/subnet metadata. GET
 *  /api/dashboard/hosts/{hostname}/purpose returns this shape. `provenance`
 *  distinguishes a repo-sourced value ("repo"), a dashboard edit ("ui", whose
 *  editor is encoded in `source` as `ui:<username>`), or an absent record
 *  ("unset"). */
export type HostPurposeEntry = {
  hostname: string;
  purpose: string | null;
  vlan: string | null;
  subnet: string | null;
  source: string | null;
  updated_at: string | null;
  provenance: "repo" | "ui" | "unset";
};

/** Result of PUT /api/dashboard/hosts/{hostname}/purpose. The DB row is already
 *  saved on a 200; `mr_url` is the persistence MR just opened, or `mr_error`
 *  explains why the MR could not be opened (the save still succeeded either
 *  way). Extends HostPurposeEntry with those two write-only fields. */
export type HostPurposeWriteResult = HostPurposeEntry & {
  mr_url: string | null;
  mr_error: string | null;
};

/** One row from GET /api/dashboard/mcp-keys (list view — never carries the raw
 *  token, which is shown once at create time only). */
export type McpKey = {
  id: string;
  name: string;
  created_at: string;
  created_by: string;
  last_used_at: string | null;
  revoked: boolean;
  allowed_tools_count: number;
  // TRK-160. `expires_at: null` = never expires. `expired` is computed
  // SERVER-side with the same predicate auth enforces (mcp_auth.is_expired) —
  // never recompute it here from expires_at, or the UI can disagree with what
  // the MCP server actually accepts. Optional so a dashboard built against a
  // backend that predates TRK-160 still type-checks during a rolling deploy.
  expires_at?: string | null;
  expired?: boolean;
};

/** GET /api/dashboard/mcp-keys/tools — the grouped tool catalog the create
 *  form's multi-select is built from. */
export type McpToolCatalog = { readonly: string[]; mutation: string[]; groups?: Record<string, string[]> };

/** POST /api/dashboard/mcp-keys result. `token` is the raw key, returned exactly
 *  ONCE — it is never stored raw and can never be retrieved again. */
export type McpKeyCreateResult = {
  id: string;
  name: string;
  token: string;
  allowed_tools: string[];
  /** Computed absolute expiry (TRK-160), or null when the key never expires. */
  expires_at?: string | null;
};

/** Result of POST /api/dashboard/login (schemas.py LoginOut). */
export type LoginResult = { authenticated: boolean; dev_mode: boolean; username?: string | null };

/** Authenticate against the dashboard session endpoint. On bad credentials the
 *  backend returns 401 (→ AuthRequired) and on lockout 429 (→ ApiError 429);
 *  the Login page catches both and shows a message rather than redirecting. */
export function login(username: string, password: string): Promise<LoginResult> {
  return apiPost<LoginResult>("/api/dashboard/login", { username, password });
}

/** Send the user to the login page, preserving where they were so the Login
 *  page can return them there afterward (?next=<current path>). Every page's
 *  AuthRequired handler routes through here (F14).
 *
 *  No-ops if already on the login page. Sidebar/Topbar render on every route
 *  (including /login, App.tsx) and each fire their own protected API calls on
 *  mount; without this guard, an unauthenticated visit to /login triggers
 *  their calls too, each 401 recomputes `next=` from the CURRENT (already
 *  /login?next=...) URL, and the resulting redirect nests another layer of
 *  encoding on top of the last — an infinite reload loop that grows the URL
 *  exponentially until the server rejects it with 400. */
export function redirectToLogin(): void {
  if (window.location.pathname.startsWith("/dashboard2/login")) return;
  const here = window.location.pathname + window.location.search + window.location.hash;
  window.location.assign(`/dashboard2/login?next=${encodeURIComponent(here)}`);
}

/** Streaming variant of the chokepoint for NDJSON endpoints (e.g. /custom-view),
 *  which return one JSON object per line rather than a single JSON body — the
 *  plain request()/apiPost() path (res.json()) cannot parse that shape. This is
 *  one of two streaming chokepoints (see apiPostRawStream below); pages still
 *  must not call fetch() directly. */
export async function* apiPostStream(url: string, body: unknown): AsyncGenerator<string> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(0, url);
  }
  if (res.status === 401) throw new AuthRequired();
  if (!res.ok) throw new ApiError(res.status, url);
  const reader = res.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx);
      buf = buf.slice(idx + 1);
      if (line.trim()) yield line;
    }
  }
  if (buf.trim()) yield buf;
}

/** Streaming variant of the chokepoint for chunked responses that need their own
 *  framing rather than NDJSON lines (e.g. SSE "data: {...}\n\n" frames) — yields
 *  raw decoded text chunks and lets the caller do its own buffering/splitting.
 *  The other legitimate fetch() call site alongside apiPostStream above. */
export async function* apiPostRawStream(url: string, body: unknown): AsyncGenerator<string> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(0, url);
  }
  if (res.status === 401) throw new AuthRequired();
  if (!res.ok || !res.body) throw new ApiError(res.status, url);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    yield decoder.decode(value, { stream: true });
  }
}

/** Normalize the two API collection shapes: bare array, or {items,total,...} envelope.
 *  Port of rows() (legacy index.html:3568) — kept semantics-identical on purpose. */
export function rows<T>(x: unknown): T[] {
  if (Array.isArray(x)) return x as T[];
  if (x && Array.isArray((x as { items?: unknown[] }).items)) {
    return (x as { items: T[] }).items;
  }
  return [];
}

/** A single live-runtime-config override row (TRK-303 part 6/7). GET
 *  /api/dashboard/runtime-config returns {items: RuntimeConfigItem[]}
 *  (RuntimeConfigPageOut — api/routers/runtime_config.py). `value` is always
 *  `null` for `is_secret` rows — no read path ever serializes a secret's
 *  plaintext or ciphertext back out, so a secret row can only be
 *  written/reset, never displayed. Write with
 *  `apiPut<RuntimeConfigItem>(`/api/dashboard/runtime-config/${encodeURIComponent(key)}`, {value, value_type, category})`
 *  and clear an override with
 *  `apiDelete(`/api/dashboard/runtime-config/${encodeURIComponent(key)}`)`. */
export type RuntimeConfigItem = {
  key: string;
  value_type: string;
  value: string | null;
  is_secret: boolean;
  category: string;
  updated_by: string;
  updated_at: string;
};

/** One entry in the derived settings catalog. GET
 *  /api/dashboard/settings-catalog returns
 *  {items: SettingCatalogEntry[], total, groups} — see
 *  `api/routers/settings_catalog.py`, which derives every field from the live
 *  `Settings` pydantic model rather than any hand-maintained list.
 *
 *  SECRET CONTRACT: when `secret` is true, BOTH `value` and `default` are
 *  ALWAYS null and `secret_state` carries "set"/"not set" instead. The server
 *  never sends a secret's value — not even masked — so the UI has nothing to
 *  accidentally render. `editable` is likewise false for every secret, and the
 *  server re-checks that on write; the disabled control is a courtesy, not the
 *  enforcement.
 *
 *  `source` is the TRK-314 field: which layer the effective value came from.
 *  `shadowed_value` is what an applied `db-override` is currently masking, and
 *  `override_ignored_reason` is set when a runtime_config row EXISTS but did
 *  nothing (denylisted / failed validation) — the silent no-op case. */
export type SettingCatalogEntry = {
  key: string;
  env_var: string;
  group: string;
  type: string;
  description: string;
  value: string | null;
  default: string | null;
  source: "env" | "db-override" | "default" | "unknown";
  shadowed_value: string | null;
  secret: boolean;
  secret_state: "set" | "not set" | null;
  secret_reason: string | null;
  managed_in: string | null;
  editable: boolean;
  locked_reason: string | null;
  db_row: boolean;
  override_ignored_reason: string | null;
  degraded: boolean;
};

export type SettingsCatalog = {
  items: SettingCatalogEntry[];
  total: number;
  groups: string[];
};
