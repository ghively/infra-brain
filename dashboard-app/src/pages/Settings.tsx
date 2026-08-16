import { useCallback, useEffect, useMemo, useState } from "react";
import {
  apiGet,
  apiPut,
  apiDelete,
  apiDeleteJson,
  rows,
  ApiError,
  AuthRequired,
  redirectToLogin,
  type RuntimeConfigItem,
  type SettingCatalogEntry,
  type SettingsCatalog,
} from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { Icon } from "../lib/icons";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { PageShell, Panel, StatTile, EmptyState, Badge, Button } from "../components/ui";

type HealthItem = { name: string; detail: string; status: string };
type SelfcheckItem = { name: string; status: string; message: string };
type Selfcheck = { overall: string; checks: SelfcheckItem[] };

const CATALOG_URL = "/api/dashboard/settings-catalog";

const ROW_STYLE: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: 12,
  padding: "11px 18px",
  borderTop: "1px solid var(--ib-border)",
};

const MONO: React.CSSProperties = {
  fontSize: 11,
  fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
};

/** Source badge. This is the single most operationally useful thing on the
 *  page: `db-override` means a `runtime_config` DB row is winning over
 *  whatever the environment says, which is exactly the condition that made
 *  TRK-314 invisible (a DB override silently masking a wrong `.env` value).
 *  It is deliberately the loudest tone of the three. */
function SourceBadge({ entry }: { entry: SettingCatalogEntry }) {
  if (entry.source === "db-override") {
    return (
      <Badge
        tone="warn"
        title={
          entry.shadowed_value === null
            ? "A runtime_config DB row is overriding the environment/default value."
            : `A runtime_config DB row is overriding the environment/default value (${entry.shadowed_value}).`
        }
      >
        db-override
      </Badge>
    );
  }
  if (entry.source === "env") {
    return (
      <Badge tone="info" title={`Set in the environment / .env as ${entry.env_var}.`}>
        env
      </Badge>
    );
  }
  if (entry.source === "unknown") {
    return (
      <Badge tone="err" title="This entry could not be rendered; its value is withheld.">
        unavailable
      </Badge>
    );
  }
  return (
    <Badge tone="neutral" title="No override anywhere — this is the value declared in config.py.">
      default
    </Badge>
  );
}

/** One catalog row: identity + description on the left, state + controls right. */
function CatalogRow({
  entry,
  draft,
  onDraft,
  onSave,
  onRevert,
  busy,
  error,
}: {
  entry: SettingCatalogEntry;
  draft: string | undefined;
  onDraft: (key: string, value: string) => void;
  onSave: (key: string) => void;
  onRevert: (key: string) => void;
  busy: boolean;
  error: string | undefined;
}) {
  const value = draft ?? entry.value ?? "";
  return (
    <div style={ROW_STYLE} data-testid={`setting-${entry.key}`}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ ...MONO, color: "var(--ib-text)" }}>{entry.key}</span>
          <span style={{ ...MONO, color: "var(--ib-faint)" }}>{entry.type}</span>
          <SourceBadge entry={entry} />
          {entry.override_ignored_reason && (
            <Badge tone="err" title={entry.override_ignored_reason}>
              override ignored
            </Badge>
          )}
        </div>
        {entry.description && (
          <div style={{ fontSize: 11, color: "var(--ib-muted)", lineHeight: 1.5, marginTop: 4 }}>
            {entry.description}
          </div>
        )}
        {entry.secret && entry.managed_in && (
          <div style={{ fontSize: 11, color: "var(--ib-blue)", lineHeight: 1.5, marginTop: 4 }}>
            {entry.managed_in}
          </div>
        )}
        {entry.source === "db-override" && entry.shadowed_value !== null && (
          <div style={{ ...MONO, color: "var(--ib-yellow)", marginTop: 4 }}>
            masking environment value: {entry.shadowed_value || "(empty)"}
          </div>
        )}
        {error && (
          <div style={{ fontSize: 11, color: "var(--ib-red)", marginTop: 4 }} role="alert">
            {error}
          </div>
        )}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
        {entry.secret ? (
          /* Secrets are a CHIP, never an input. The server sends no value at
           * all for these — not even a masked one — so there is nothing here
           * to reveal, and `disabled` is a courtesy on top of an already-empty
           * payload rather than the thing holding the line. */
          <Badge
            tone={entry.secret_state === "set" ? "ok" : "neutral"}
            title={entry.managed_in ?? undefined}
          >
            {entry.secret_state ?? "not set"}
          </Badge>
        ) : entry.editable ? (
          <>
            <input
              type="text"
              autoComplete="off"
              aria-label={`${entry.key} value`}
              value={value}
              onChange={(e) => onDraft(entry.key, e.target.value)}
              style={{ ...MONO, width: 150 }}
            />
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              loading={busy}
              onClick={() => onSave(entry.key)}
            >
              Save
            </Button>
            {entry.db_row && (
              <Button
                size="sm"
                variant="secondary"
                disabled={busy}
                onClick={() => onRevert(entry.key)}
                title="Delete the runtime_config row so the environment/default value takes over again"
              >
                Revert to default
              </Button>
            )}
          </>
        ) : (
          <>
            <span style={{ ...MONO, color: "var(--ib-muted)", maxWidth: 200, textAlign: "right" }}>
              {entry.value || <span style={{ color: "var(--ib-faint)" }}>(empty)</span>}
            </span>
            <Badge tone="neutral" title={entry.locked_reason ?? undefined}>
              locked
            </Badge>
          </>
        )}
      </div>
    </div>
  );
}

/** The guided configuration surface.
 *
 *  Replaces what used to be here: a read-only `Settings.model_dump()` grid
 *  plus a raw key/value editor that required you to already know a field name
 *  to type into it. Everything shown is derived server-side from the live
 *  `Settings` model (`GET /api/dashboard/settings-catalog`), so a field added
 *  to config.py shows up here with its type, default, and doc comment without
 *  anyone editing this file.
 *
 *  The raw editor is NOT removed — it moves to the "Advanced" section below,
 *  because it is still the only way to reach a non-`Settings` runtime_config
 *  key. */
function ConfigurationCatalog({ onLoaded }: { onLoaded: (c: SettingsCatalog | null) => void }) {
  const [data, setData] = useState<SettingsCatalog | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [overridesOnly, setOverridesOnly] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});

  // Fetched exactly once, here — deliberately NOT part of the page's
  // auto-refreshing `load()`. A 5-minute background re-fetch of ~270 entries
  // would both be wasteful and clobber a half-typed edit. `onLoaded` hands the
  // result up so the page header can render the read-only-mode pill from a
  // real setting value without a second identical request.
  useEffect(() => {
    apiGet<SettingsCatalog>(CATALOG_URL)
      .then((c) => {
        setData(c);
        onLoaded(c);
      })
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        // TRK-321 precedent: the catalog enumerates every configuration key,
        // so it is admin-only. A non-admin gets an honest empty state here
        // instead of the whole page dying on a raw "ApiError: 403".
        else if (e instanceof ApiError && e.status === 403) setForbidden(true);
        else setError(String(e));
        onLoaded(null);
      });
  }, [onLoaded]);

  function replaceEntry(updated: SettingCatalogEntry) {
    setData((prev) =>
      prev === null
        ? prev
        : { ...prev, items: prev.items.map((it) => (it.key === updated.key ? updated : it)) },
    );
  }

  async function mutate(key: string, run: () => Promise<SettingCatalogEntry>) {
    setBusyKey(key);
    setRowErrors((prev) => {
      const { [key]: _drop, ...rest } = prev;
      return rest;
    });
    try {
      replaceEntry(await run());
      setDrafts((prev) => {
        const { [key]: _drop, ...rest } = prev;
        return rest;
      });
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setRowErrors((prev) => ({ ...prev, [key]: String(e) }));
    } finally {
      setBusyKey(null);
    }
  }

  const save = (key: string) =>
    mutate(key, () =>
      apiPut<SettingCatalogEntry>(`${CATALOG_URL}/${encodeURIComponent(key)}`, {
        value: drafts[key] ?? "",
      }),
    );

  const revert = (key: string) =>
    mutate(key, () =>
      apiDeleteJson<SettingCatalogEntry>(`${CATALOG_URL}/${encodeURIComponent(key)}`),
    );

  const filtered = useMemo(() => {
    if (data === null) return [];
    const q = query.trim().toLowerCase();
    return data.items.filter((it) => {
      if (overridesOnly && it.source !== "db-override" && !it.db_row) return false;
      if (!q) return true;
      return (
        it.key.includes(q) ||
        it.env_var.toLowerCase().includes(q) ||
        it.group.toLowerCase().includes(q) ||
        it.description.toLowerCase().includes(q)
      );
    });
  }, [data, query, overridesOnly]);

  const byGroup = useMemo(() => {
    const map = new Map<string, SettingCatalogEntry[]>();
    for (const it of filtered) {
      const list = map.get(it.group);
      if (list) list.push(it);
      else map.set(it.group, [it]);
    }
    return map;
  }, [filtered]);

  if (forbidden) {
    return (
      <Panel mnemonic="CONFIG" description="settings-catalog — every config.py setting">
        <EmptyState
          kind="none-yet"
          title="Configuration is admin-only"
          hint="Your account does not have the admin role, so the configuration catalog is not shown. Service health and selfcheck above are unaffected."
        />
      </Panel>
    );
  }

  if (data === null) {
    return (
      <Panel mnemonic="CONFIG" description="settings-catalog — every config.py setting">
        {error ? (
          <EmptyState kind="error" title="Configuration failed to load" hint={error} />
        ) : (
          <Skeleton count={4} height={32} />
        )}
      </Panel>
    );
  }

  // While searching, every matching group opens — otherwise a hit inside a
  // collapsed group is invisible and the search reads as broken.
  const searching = query.trim().length > 0 || overridesOnly;
  const groups = (data.groups ?? []).filter((g) => byGroup.has(g));

  return (
    <Panel
      mnemonic="CONFIG"
      description={`settings-catalog — ${data.total} settings derived from config.py`}
      headerRight={
        <span style={{ ...MONO, color: "var(--ib-muted)" }}>
          {filtered.length} shown
        </span>
      }
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "11px 18px" }}>
        <input
          type="search"
          placeholder="Search settings by name, group, or description…"
          aria-label="Search settings"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ ...MONO, flex: 1, minWidth: 0 }}
        />
        <label style={{ ...MONO, display: "flex", alignItems: "center", gap: 6, color: "var(--ib-muted)" }}>
          <input
            type="checkbox"
            checked={overridesOnly}
            onChange={(e) => setOverridesOnly(e.target.checked)}
          />
          Only DB overrides
        </label>
      </div>

      {groups.length === 0 ? (
        <EmptyState kind="none-yet" title="No settings match this search" />
      ) : (
        groups.map((group) => {
          const entries = byGroup.get(group) ?? [];
          const open = searching || expanded[group] === true;
          return (
            <div key={group} style={{ borderTop: "1px solid var(--ib-border)" }}>
              <button
                type="button"
                aria-expanded={open}
                onClick={() => setExpanded((prev) => ({ ...prev, [group]: !open }))}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  width: "100%",
                  padding: "11px 18px",
                  background: "none",
                  border: "none",
                  cursor: "pointer",
                  textAlign: "left",
                  color: "var(--ib-text)",
                  fontSize: 12,
                }}
              >
                <span style={{ color: "var(--ib-faint)" }}>{open ? "▾" : "▸"}</span>
                <span style={{ flex: 1 }}>{group}</span>
                <span style={{ ...MONO, color: "var(--ib-muted)" }}>{entries.length}</span>
              </button>
              {open &&
                entries.map((entry) => (
                  <CatalogRow
                    key={entry.key}
                    entry={entry}
                    draft={drafts[entry.key]}
                    onDraft={(k, v) => setDrafts((prev) => ({ ...prev, [k]: v }))}
                    onSave={save}
                    onRevert={revert}
                    busy={busyKey === entry.key}
                    error={rowErrors[entry.key]}
                  />
                ))}
            </div>
          );
        })
      )}
    </Panel>
  );
}

/** Raw `runtime_config` key/value editor — now the ADVANCED escape hatch
 *  rather than the only way to configure anything (TRK-303 part 6/7).
 *
 *  Kept, not removed: the catalog above covers `Settings` fields only, and a
 *  `runtime_config` row is allowed to be any key. This panel is what reaches
 *  the rest — and what an operator needs when a key does not (yet) exist as a
 *  `Settings` field.
 *
 *  `dispatchable__<domain>` rows are filtered out here exactly as before: they
 *  are collector pause levers managed from the Agents page, and "Reset to
 *  default" from a panel that does not show what they do would silently
 *  un-pause a collector. */
function RuntimeConfigPanel() {
  const [items, setItems] = useState<RuntimeConfigItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const load = useCallback(async () => {
    const d = await apiGet<unknown>("/api/dashboard/runtime-config");
    const all = rows<RuntimeConfigItem>(d);
    setItems(all.filter((it) => !it.key.startsWith("dispatchable__")));
  }, []);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  async function save(key: string, value: string) {
    if (!key.trim() || !value.trim()) return;
    setBusyKey(key);
    setError(null);
    try {
      const updated = await apiPut<RuntimeConfigItem>(
        `/api/dashboard/runtime-config/${encodeURIComponent(key)}`,
        { value, value_type: "str", category: "tuning" },
      );
      setItems((prev) => {
        const rest = (prev ?? []).filter((x) => x.key !== updated.key);
        return [...rest, updated].sort((a, b) => a.key.localeCompare(b.key));
      });
      setEdits((prev) => {
        const { [key]: _drop, ...rest } = prev;
        return rest;
      });
      setNewKey("");
      setNewValue("");
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    } finally {
      setBusyKey(null);
    }
  }

  async function reset(key: string) {
    setBusyKey(key);
    setError(null);
    try {
      await apiDelete(`/api/dashboard/runtime-config/${encodeURIComponent(key)}`);
      setItems((prev) => (prev ?? []).filter((x) => x.key !== key));
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    } finally {
      setBusyKey(null);
    }
  }

  if (items === null) {
    return (
      <Panel mnemonic="ADVANCED" description="runtime-config — raw key/value overrides">
        {error ? (
          <EmptyState kind="error" title="Failed to load tuning overrides" hint={error} />
        ) : (
          <Skeleton count={2} height={32} />
        )}
      </Panel>
    );
  }

  return (
    <Panel mnemonic="ADVANCED" description="runtime-config — raw key/value overrides">
      <div style={{ padding: "11px 18px", fontSize: 12, color: "var(--ib-muted)", lineHeight: 1.5 }}>
        Escape hatch for keys the catalog above does not cover. Prefer the catalog
        — it validates the value against the setting's real type and refuses
        secrets and safety-critical fields. Anything written here is a raw string.
      </div>
      {error && (
        <div style={{ padding: "8px 18px", fontSize: 12, color: "var(--ib-red)" }} role="alert">
          {error}
        </div>
      )}
      {items.length === 0 ? (
        <EmptyState kind="none-yet" title="No raw overrides set" />
      ) : (
        <div>
          {items.map((it) => (
            <div key={it.key} style={{ ...ROW_STYLE, alignItems: "center" }}>
              <span
                style={{
                  ...MONO,
                  flex: 1,
                  minWidth: 0,
                  color: "var(--ib-muted)",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {it.key}
              </span>
              {it.is_secret ? (
                <span style={{ width: 140, display: "flex" }}>
                  <Badge tone="neutral" title="Secrets cannot be edited from this panel">
                    •••••• (secret — not editable here)
                  </Badge>
                </span>
              ) : (
                <input
                  type="text"
                  autoComplete="off"
                  aria-label={`${it.key} raw value`}
                  value={edits[it.key] ?? it.value ?? ""}
                  onChange={(e) => setEdits((prev) => ({ ...prev, [it.key]: e.target.value }))}
                  style={{ ...MONO, width: 140 }}
                />
              )}
              {!it.is_secret && (
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={busyKey === it.key}
                  loading={busyKey === it.key}
                  onClick={() => save(it.key, edits[it.key] ?? it.value ?? "")}
                >
                  Save
                </Button>
              )}
              <Button
                size="sm"
                variant="secondary"
                disabled={busyKey === it.key}
                onClick={() => reset(it.key)}
              >
                Reset to default
              </Button>
            </div>
          ))}
        </div>
      )}
      <div style={{ ...ROW_STYLE, alignItems: "center" }}>
        <input
          type="text"
          placeholder="key (e.g. dispatchable__linux)"
          aria-label="New override key"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          style={{ ...MONO, flex: 1, minWidth: 0 }}
        />
        <input
          type="text"
          placeholder="value"
          aria-label="New override value"
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          style={{ ...MONO, width: 140 }}
        />
        <Button
          size="sm"
          variant="primary"
          disabled={busyKey !== null || !newKey.trim() || !newValue.trim()}
          loading={busyKey === newKey}
          onClick={() => save(newKey, newValue)}
        >
          Save
        </Button>
      </div>
    </Panel>
  );
}

/** Settings — service health, self-checks, and the configuration surface.
 *
 *  TRK-198 history, still binding: this page must never render a toggle that
 *  only writes to client state. Everything interactive here persists through a
 *  real endpoint, and everything that cannot be changed is rendered as an
 *  explicitly locked, non-interactive control with the server's own reason
 *  attached — not as a control that looks live and silently does nothing.
 */
export function Settings() {
  const [health, setHealth] = useState<HealthItem[] | null>(null);
  const [selfcheck, setSelfcheck] = useState<Selfcheck | null>(null);
  const [readonlyOn, setReadonlyOn] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Health and selfcheck only. The configuration catalog is admin-only and is
  // fetched by <ConfigurationCatalog> itself — keeping it OUT of this
  // Promise.all is deliberate: a non-admin's 403 there must not reject this
  // batch and blank health/selfcheck too. That coupling is the exact failure
  // mode that got the first attempt at TRK-321 reverted (on Intprops.tsx).
  const load = useCallback(async () => {
    await Promise.all([
      apiGet<unknown>("/api/dashboard/system_health"),
      apiGet<Selfcheck>("/api/dashboard/selfcheck"),
    ])
      .then(([h, s]) => {
        setHealth(rows<HealthItem>(h));
        setSelfcheck(s);
      })
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, []);

  const onCatalogLoaded = useCallback((c: SettingsCatalog | null) => {
    const row = c?.items.find((it) => it.key === "scan_readonly_enforce");
    setReadonlyOn(row ? row.value === "true" : null);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const h = headerFor("settings");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Settings failed to load" hint={error} />
      </div>
    );
  }

  if (health === null || selfcheck === null) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const healthy = health.filter((x) => x.status === "ok").length;
  const down = health.filter((x) => x.status === "down").length;
  const degraded = health.length - healthy - down;
  const pills = [
    { dot: "var(--ib-green)", text: `${healthy} services healthy` },
    { dot: "var(--ib-red)", text: `${down} down` },
    { dot: "var(--ib-yellow)", text: `${degraded} degraded` },
    ...(readonlyOn === null
      ? []
      : [
          {
            dot: readonlyOn ? "var(--ib-blue)" : "var(--ib-red)",
            text: readonlyOn ? "read-only mode" : "read-only mode disabled",
          },
        ]),
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} pills={pills} />

      <Panel
        rail={down > 0 ? "err" : degraded > 0 ? "warn" : "ok"}
        mnemonic="HEALTH"
        description="system_health — live status from every backing service"
        live
      >
        {health.length === 0 ? (
          <EmptyState kind="none-yet" title="No health checks reported yet" />
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12, padding: 16 }}>
            {health.map((item, i) => (
              <StatTile
                key={`${item.name}-${i}`}
                label={item.name}
                value={item.status === "ok" ? "operational" : item.status === "down" ? "down" : "degraded"}
                color={item.status === "ok" ? "green" : item.status === "down" ? "red" : "yellow"}
                sub={item.detail}
              />
            ))}
          </div>
        )}
      </Panel>

      <Panel
        rail={selfcheck.overall === "error" ? "err" : selfcheck.overall === "warn" ? "warn" : "ok"}
        mnemonic="SELFCHECK"
        description="selfcheck — runtime-viable subset of dev_status.py's 10 checks (P3.1)"
        live
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12, padding: 16 }}>
          {selfcheck.checks.map((c) => (
            <StatTile
              key={c.name}
              label={c.name}
              value={c.status}
              color={c.status === "ok" ? "green" : c.status === "error" ? "red" : "yellow"}
              sub={c.message}
            />
          ))}
        </div>
      </Panel>

      <div
        style={{
          background: "var(--ib-panel)",
          border: "1px solid var(--ib-border)",
          borderRadius: "var(--ib-radius)",
          padding: "11px 16px",
          margin: "16px 0",
          fontSize: 12,
          color: "var(--ib-blue)",
          lineHeight: 1.6,
        }}
      >
        <div>
          Each setting shows where its <b>effective value</b> came from: <b>env</b> (the
          environment or <code>.env</code>), <b>db-override</b> (a live
          <code> runtime_config </code> row, which survives deploys and wins over
          the environment), or <b>default</b> (the value declared in
          <code> config.py</code>). Editing here writes a db-override; "Revert to
          default" deletes it.
        </div>
        <div style={{ marginTop: 6 }}>
          Secrets are injected at startup from <b>Bitwarden Secrets Manager</b> and are
          shown only as <b>set</b> / <b>not set</b> — their values are never sent to this
          page and are never editable from it.
        </div>
        <div style={{ marginTop: 6 }}>
          Not here: enabling or pausing a collector per domain lives on the{" "}
          <a href="/dashboard2/agents">Agents</a> page, and collection cadence lives on{" "}
          <a href="/dashboard2/scanschedule">Scan Schedule</a>.
        </div>
      </div>

      <ConfigurationCatalog onLoaded={onCatalogLoaded} />
      <RuntimeConfigPanel />
    </div>
  );
}
