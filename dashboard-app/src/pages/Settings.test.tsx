import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import type { SettingCatalogEntry } from "../api";
import { Settings } from "./Settings";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx/ScanSchedule.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const HEALTH = [
  { name: "Postgres", detail: "5433", status: "ok" },
  { name: "Redis", detail: "6379", status: "down" },
  { name: "LangSmith", detail: "tracing", status: "degraded" },
];

const SELFCHECK = {
  overall: "ok",
  checks: [
    { name: "agent_registry", status: "ok", message: "36 agents, 32 scheduled, 16 skip-hook" },
    { name: "schedule_collisions", status: "ok", message: "No collisions across 38 domains" },
    { name: "callbacks_wiring", status: "ok", message: "All agents inherit BaseAgent" },
  ],
};

function entry(over: Partial<SettingCatalogEntry> & { key: string }): SettingCatalogEntry {
  return {
    env_var: over.key.toUpperCase(),
    group: "Other",
    type: "str",
    description: "",
    value: null,
    default: null,
    source: "default",
    shadowed_value: null,
    secret: false,
    secret_state: null,
    secret_reason: null,
    managed_in: null,
    editable: true,
    locked_reason: null,
    db_row: false,
    override_ignored_reason: null,
    degraded: false,
    ...over,
  };
}

const POOL = entry({
  key: "db_pool_size",
  env_var: "DB_POOL_SIZE",
  group: "Database & cache",
  type: "int",
  description: "SQLAlchemy pool sizing for the shared primary engine.",
  value: "5",
  default: "5",
  source: "default",
});

const OVERRIDDEN = entry({
  key: "netdiscovery_max_rate",
  group: "Collector — network discovery & DNS",
  type: "int",
  value: "500",
  default: "100",
  source: "db-override",
  shadowed_value: "250",
  db_row: true,
});

const SECRET = entry({
  key: "gitlab_token",
  group: "Collector — GitLab, IaC & remediation",
  value: null,
  default: null,
  source: "env",
  secret: true,
  secret_state: "set",
  secret_reason: "name-hint",
  managed_in: "Bitwarden Secrets Manager, injected into `GITLAB_TOKEN` at startup by secrets.py.",
  editable: false,
  locked_reason: "Secret — set it where it belongs, never from this UI.",
});

const LOCKED = entry({
  key: "scan_readonly_enforce",
  group: "Security & read-only boundary",
  type: "bool",
  value: "true",
  default: "true",
  source: "default",
  editable: false,
  locked_reason: "Non-overridable: this field gates a read-only/auth guarantee.",
});

const CATALOG = {
  items: [POOL, OVERRIDDEN, SECRET, LOCKED],
  total: 4,
  groups: [
    "Database & cache",
    "Security & read-only boundary",
    "Collector — GitLab, IaC & remediation",
    "Collector — network discovery & DNS",
  ],
};

/** The catalog URL is a PREFIX of nothing else, but /api/dashboard/settings is
 *  a prefix of it — so the catalog branch must be checked first in every mock
 *  here, or a catalog request silently gets the wrong payload. */
function mockLoadSuccess(runtimeConfig: unknown[] = [], catalog: unknown = CATALOG) {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/api/dashboard/settings-catalog")) return catalog;
    if (url.includes("/api/dashboard/system_health")) return { items: HEALTH, total: HEALTH.length };
    if (url.includes("/api/dashboard/runtime-config")) return { items: runtimeConfig };
    if (url.includes("/api/dashboard/selfcheck")) return SELFCHECK;
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function renderSettings() {
  return render(<Settings />);
}

/** Open a collapsed catalog group by clicking its header button.
 *  Matched on full textContent — several group names share a "Collector — "
 *  prefix, so a truncated regex matches more than one button. */
async function openGroup(name: string) {
  const find = () =>
    screen.getAllByRole("button").find((b) => (b.textContent ?? "").includes(name));
  await waitFor(() => expect(find()).toBeTruthy());
  fireEvent.click(find() as HTMLElement);
}

/** The row element for one catalog setting (see CatalogRow's data-testid). */
function settingRow(key: string): HTMLElement {
  return screen.getByTestId(`setting-${key}`);
}

describe("Settings page — health and selfcheck", () => {
  it("renders system health tiles with status-derived color/label", async () => {
    mockLoadSuccess();
    renderSettings();

    await waitFor(() => expect(screen.getByText("Postgres")).toBeInTheDocument());
    expect(screen.getByText("operational")).toBeInTheDocument();
    expect(screen.getByText("down")).toBeInTheDocument();
    expect(screen.getByText("degraded")).toBeInTheDocument();
  });

  it("renders the read-only-mode pill from the real scan_readonly_enforce catalog entry", async () => {
    mockLoadSuccess();
    renderSettings();

    await waitFor(() => expect(screen.getByText("read-only mode")).toBeInTheDocument());
  });

  it("shows the read-only-mode-disabled pill when the server reports it off", async () => {
    mockLoadSuccess([], {
      ...CATALOG,
      items: [{ ...LOCKED, value: "false" }],
      total: 1,
      groups: ["Security & read-only boundary"],
    });
    renderSettings();

    await waitFor(() => expect(screen.getByText("read-only mode disabled")).toBeInTheDocument());
  });

  it("shows an error EmptyState (not a bare alert div) when the health fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async () => {
      throw new Error("db unreachable");
    });

    renderSettings();
    await waitFor(() => expect(screen.getAllByRole("alert").length).toBeGreaterThan(0));
    expect(
      screen.getAllByRole("alert").some((a) => /failed to load/i.test(a.textContent ?? "")),
    ).toBe(true);
  });
});

describe("Settings page — configuration catalog", () => {
  it("groups settings and keeps groups collapsed until opened", async () => {
    mockLoadSuccess();
    renderSettings();

    await waitFor(() => expect(screen.getByText("Database & cache")).toBeInTheDocument());
    // Every group from the envelope is offered...
    expect(screen.getByText("Collector — network discovery & DNS")).toBeInTheDocument();
    // ...but the rows inside are not rendered until the group is opened.
    expect(screen.queryByText("db_pool_size")).not.toBeInTheDocument();

    await openGroup("Database & cache");
    expect(screen.getByText("db_pool_size")).toBeInTheDocument();
    expect(
      screen.getByText(/SQLAlchemy pool sizing for the shared primary engine/i),
    ).toBeInTheDocument();
  });

  it("searching expands matching groups and hides the rest", async () => {
    mockLoadSuccess();
    renderSettings();

    await waitFor(() => expect(screen.getByLabelText("Search settings")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Search settings"), {
      target: { value: "pool" },
    });

    // Auto-expanded: a hit inside a collapsed group would read as "search broken".
    await waitFor(() => expect(screen.getByText("db_pool_size")).toBeInTheDocument());
    expect(screen.queryByText("netdiscovery_max_rate")).not.toBeInTheDocument();
    expect(screen.queryByText("gitlab_token")).not.toBeInTheDocument();
  });

  it("searching matches the description text, not only the key", async () => {
    mockLoadSuccess();
    renderSettings();

    await waitFor(() => expect(screen.getByLabelText("Search settings")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Search settings"), {
      target: { value: "sqlalchemy" },
    });
    await waitFor(() => expect(screen.getByText("db_pool_size")).toBeInTheDocument());
  });

  it("shows a source badge per setting, and what a db-override is masking (TRK-314)", async () => {
    mockLoadSuccess();
    renderSettings();

    await openGroup("Collector — network discovery & DNS");

    const row = settingRow("netdiscovery_max_rate");
    expect(within(row).getByText("db-override")).toBeInTheDocument();
    // The env value the DB row is currently masking is stated outright — the
    // thing that made TRK-314 invisible.
    expect(screen.getByText(/masking environment value: 250/i)).toBeInTheDocument();
  });

  it("renders a secret as a disabled set/not-set chip with the Bitwarden pointer, never an input", async () => {
    mockLoadSuccess();
    renderSettings();

    await openGroup("Collector — GitLab, IaC & remediation");

    const row = settingRow("gitlab_token");
    // No input anywhere on a secret row — nothing to type into, nothing to reveal.
    expect(row.querySelector("input")).toBeNull();
    expect(within(row).getByText("set")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
    // Scoped to the row: the page-level explainer also mentions Bitwarden.
    expect(within(row).getByText(/Bitwarden Secrets Manager/)).toBeInTheDocument();
  });

  it("renders a non-secret but non-editable setting as a locked readout", async () => {
    mockLoadSuccess();
    renderSettings();

    await openGroup("Security & read-only boundary");

    const row = settingRow("scan_readonly_enforce");
    expect(row.querySelector("input")).toBeNull();
    expect(within(row).getByText("locked")).toBeInTheDocument();
    expect(within(row).getByText("true")).toBeInTheDocument();
  });

  it("edits an editable setting: PUTs the value and re-renders from the response", async () => {
    mockLoadSuccess();
    const putSpy = vi.spyOn(api, "apiPut").mockResolvedValue(
      entry({
        ...POOL,
        value: "23",
        source: "db-override",
        db_row: true,
        shadowed_value: "5",
      }),
    );

    renderSettings();
    await openGroup("Database & cache");

    fireEvent.change(screen.getByLabelText("db_pool_size value"), { target: { value: "23" } });
    fireEvent.click(within(settingRow("db_pool_size")).getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith("/api/dashboard/settings-catalog/db_pool_size", {
        value: "23",
      }),
    );
    // The response drives the re-render — the source badge flips to db-override.
    // Scoped to the row: the page-level explainer also names the three sources.
    await waitFor(() =>
      expect(within(settingRow("db_pool_size")).getByText("db-override")).toBeInTheDocument(),
    );
    expect(screen.getByDisplayValue("23")).toBeInTheDocument();
  });

  it("surfaces a rejected edit on the row itself without breaking the page", async () => {
    mockLoadSuccess();
    vi.spyOn(api, "apiPut").mockRejectedValue(new api.ApiError(400, "/x"));

    renderSettings();
    await openGroup("Database & cache");

    fireEvent.change(screen.getByLabelText("db_pool_size value"), { target: { value: "nope" } });
    fireEvent.click(within(settingRow("db_pool_size")).getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(screen.getAllByRole("alert").some((a) => /400/.test(a.textContent ?? ""))).toBe(true),
    );
    expect(screen.getByText("Postgres")).toBeInTheDocument();
  });

  it("reverts to default via DELETE and shows the value that took over", async () => {
    mockLoadSuccess();
    const delSpy = vi.spyOn(api, "apiDeleteJson").mockResolvedValue(
      entry({ ...OVERRIDDEN, value: "100", source: "default", db_row: false, shadowed_value: null }),
    );

    renderSettings();
    await openGroup("Collector — network discovery & DNS");

    fireEvent.click(
      within(settingRow("netdiscovery_max_rate")).getByRole("button", { name: /revert to default/i }),
    );

    await waitFor(() =>
      expect(delSpy).toHaveBeenCalledWith(
        "/api/dashboard/settings-catalog/netdiscovery_max_rate",
      ),
    );
    await waitFor(() => expect(screen.getByDisplayValue("100")).toBeInTheDocument());
    // The revert button is gone with the row it belonged to.
    expect(screen.queryByRole("button", { name: /revert to default/i })).not.toBeInTheDocument();
  });

  it("offers no revert button for a setting with no DB override", async () => {
    mockLoadSuccess();
    renderSettings();
    await openGroup("Database & cache");

    expect(
      within(settingRow("db_pool_size")).queryByRole("button", { name: /revert to default/i }),
    ).not.toBeInTheDocument();
  });

  it("flags a runtime_config row that exists but was not applied", async () => {
    mockLoadSuccess([], {
      ...CATALOG,
      items: [
        {
          ...POOL,
          db_row: true,
          source: "default",
          override_ignored_reason: "A runtime_config row exists but is IGNORED.",
        },
      ],
      total: 1,
      groups: ["Database & cache"],
    });
    renderSettings();
    await openGroup("Database & cache");

    expect(screen.getByText("override ignored")).toBeInTheDocument();
  });

  it("degrades to an admin-only notice when the catalog 403s, keeping health and selfcheck (TRK-321)", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/settings-catalog")) throw new api.ApiError(403, url);
      if (url.includes("/api/dashboard/system_health")) return { items: HEALTH, total: HEALTH.length };
      if (url.includes("/api/dashboard/runtime-config")) return { items: [] };
      if (url.includes("/api/dashboard/selfcheck")) return SELFCHECK;
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderSettings();

    await waitFor(() =>
      expect(screen.getByText("Configuration is admin-only")).toBeInTheDocument(),
    );
    expect(screen.queryByText("Settings failed to load")).not.toBeInTheDocument();
    expect(screen.getByText("Postgres")).toBeInTheDocument();
    expect(screen.getByText("agent_registry")).toBeInTheDocument();
    // ...and no config keys leak into the DOM.
    expect(screen.queryByText("gitlab_token")).not.toBeInTheDocument();
  });

  it("points at the pages that own collector enable/disable and cadence", async () => {
    mockLoadSuccess();
    renderSettings();

    await waitFor(() => expect(screen.getByText("Agents")).toBeInTheDocument());
    expect(screen.getByText("Agents").getAttribute("href")).toBe("/dashboard2/agents");
    expect(screen.getByText("Scan Schedule").getAttribute("href")).toBe(
      "/dashboard2/scanschedule",
    );
  });
});

describe("Settings page — advanced raw runtime-config escape hatch", () => {
  it("is still present and still saves a raw override", async () => {
    mockLoadSuccess([]);
    const putSpy = vi.spyOn(api, "apiPut").mockResolvedValue({
      key: "some_future_key",
      value_type: "str",
      value: "500",
      is_secret: false,
      category: "tuning",
      updated_by: "operator",
      updated_at: "2026-07-30T00:00:00Z",
    });

    renderSettings();
    await waitFor(() => expect(screen.getByLabelText("New override key")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("New override key"), {
      target: { value: "some_future_key" },
    });
    fireEvent.change(screen.getByLabelText("New override value"), { target: { value: "500" } });
    const advanced = screen.getByLabelText("New override key").closest("div") as HTMLElement;
    fireEvent.click(within(advanced).getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith(
        "/api/dashboard/runtime-config/some_future_key",
        expect.objectContaining({ value: "500" }),
      ),
    );
    await waitFor(() => expect(screen.getByText("some_future_key")).toBeInTheDocument());
  });

  it("locks out editing for an is_secret raw row: no input, no Save", async () => {
    mockLoadSuccess([
      {
        key: "GITLAB_TOKEN",
        value_type: "str",
        value: null,
        is_secret: true,
        category: "secret",
        updated_by: "operator",
        updated_at: "2026-07-30T00:00:00Z",
      },
    ]);

    renderSettings();
    await waitFor(() => expect(screen.getByText("GITLAB_TOKEN")).toBeInTheDocument());

    const row = screen.getByText("GITLAB_TOKEN").closest("div") as HTMLElement;
    expect(row.querySelector("input")).toBeNull();
    expect(within(row).getByText(/secret — not editable here/i)).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /reset to default/i })).toBeInTheDocument();
  });

  it("hides dispatchable__<domain> collector-pause rows from the raw panel", async () => {
    mockLoadSuccess([
      {
        key: "dispatchable__vsphere",
        value_type: "str",
        value: "false",
        is_secret: false,
        category: "tuning",
        updated_by: "operator",
        updated_at: "2026-07-30T00:00:00Z",
      },
      {
        key: "some_future_key",
        value_type: "str",
        value: "500",
        is_secret: false,
        category: "tuning",
        updated_by: "operator",
        updated_at: "2026-07-30T00:00:00Z",
      },
    ]);

    renderSettings();
    await waitFor(() => expect(screen.getByText("some_future_key")).toBeInTheDocument());
    expect(screen.queryByText("dispatchable__vsphere")).not.toBeInTheDocument();
  });

  it("resets a raw override via DELETE and it disappears", async () => {
    mockLoadSuccess([
      {
        key: "some_future_key",
        value_type: "str",
        value: "500",
        is_secret: false,
        category: "tuning",
        updated_by: "operator",
        updated_at: "2026-07-30T00:00:00Z",
      },
    ]);
    const deleteSpy = vi.spyOn(api, "apiDelete").mockResolvedValue(undefined);

    renderSettings();
    await waitFor(() => expect(screen.getByText("some_future_key")).toBeInTheDocument());

    const row = screen.getByText("some_future_key").closest("div") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /reset to default/i }));

    await waitFor(() =>
      expect(deleteSpy).toHaveBeenCalledWith("/api/dashboard/runtime-config/some_future_key"),
    );
    await waitFor(() => expect(screen.queryByText("some_future_key")).not.toBeInTheDocument());
  });

  it("F-8: shows a reachable error, not a perpetual skeleton, when GET /runtime-config fails", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/settings-catalog")) return CATALOG;
      if (url.includes("/api/dashboard/system_health")) return { items: HEALTH, total: HEALTH.length };
      if (url.includes("/api/dashboard/runtime-config")) throw new Error("db unreachable");
      if (url.includes("/api/dashboard/selfcheck")) return SELFCHECK;
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderSettings();

    await waitFor(() => expect(screen.getByText("Postgres")).toBeInTheDocument());
    await waitFor(() => {
      const alerts = screen.getAllByRole("alert");
      expect(alerts.some((a) => /db unreachable/i.test(a.textContent ?? ""))).toBe(true);
    });
  });
});
