import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Security } from "./Security";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx / McpKeys.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

function auditEntry(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    ts: "2026-07-24T00:00:00Z",
    agent: "linux",
    tool: "run_command",
    category: "readonly",
    allowed: true,
    reason: "passed",
    ihash: "abc123",
    ohash: "def456",
    ...overrides,
  };
}

/** 200 allowed/readonly rows — the size of the backend's default window —
 *  standing in for a log that is actually much larger overall (simulated via
 *  the envelope `total` below being far greater than `items.length`). */
const WINDOW_ITEMS = Array.from({ length: 200 }, () => auditEntry());

function mockLoadSuccess(opts?: { total?: number; denied?: number; dlp?: number }) {
  const total = opts?.total ?? 4321;
  const denied = opts?.denied ?? 17;
  const dlp = opts?.dlp ?? 5;
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("allowed=false")) {
      return { items: [], total: denied, limit: 1, offset: 0 };
    }
    if (url.includes("category=dlp")) {
      return { items: [], total: dlp, limit: 1, offset: 0 };
    }
    // Unfiltered fetch — the 200-row window with a much larger true total.
    return { items: WINDOW_ITEMS, total, limit: 200, offset: 0 };
  });
}

function renderSecurity() {
  return render(
    <MemoryRouter>
      <Security />
    </MemoryRouter>,
  );
}

describe("Security page — audit totals accuracy", () => {
  it("'Audited Calls' reflects the true envelope total, not the 200-row window length", async () => {
    mockLoadSuccess({ total: 4321 });

    renderSecurity();

    await waitFor(() => expect(screen.getByText("Audited Calls")).toBeInTheDocument());

    // Must show the true total (4321), never the capped window size (200).
    expect(screen.getByText("4321")).toBeInTheDocument();
    expect(screen.queryByText("200")).not.toBeInTheDocument();
  });

  it("'Denied' and 'DLP Blocks' tiles reflect true filtered totals, not window counts", async () => {
    mockLoadSuccess({ total: 4321, denied: 17, dlp: 5 });

    renderSecurity();

    await waitFor(() => expect(screen.getByText("Audited Calls")).toBeInTheDocument());

    // The window itself contains zero denied/dlp rows (all allowed/readonly),
    // so if the tiles were still window-derived they'd read "0" — assert the
    // true filtered totals instead.
    expect(screen.getByText("17")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
  });

  it("'Allowed' tile is derived as total - denied, not the window's allowed count", async () => {
    mockLoadSuccess({ total: 4321, denied: 17, dlp: 5 });

    renderSecurity();

    await waitFor(() => expect(screen.getByText("Audited Calls")).toBeInTheDocument());

    // 4321 - 17 = 4304, not the window's 200 allowed rows.
    expect(screen.getByText("4304")).toBeInTheDocument();
  });

  it("falls back to the window-derived count if the true total is missing from the envelope", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("allowed=false")) return { items: [], total: 0, limit: 1, offset: 0 };
      if (url.includes("category=dlp")) return { items: [], total: 0, limit: 1, offset: 0 };
      // Envelope missing `total` entirely — degrade gracefully to items.length.
      return { items: WINDOW_ITEMS };
    });

    renderSecurity();

    await waitFor(() => expect(screen.getByText("Audited Calls")).toBeInTheDocument());

    // Both "Audited Calls" and "Allowed" degrade to the window count (200)
    // here since denied/dlp also came back 0 — assert at least one "200" is
    // present rather than pinning down which tile renders first.
    expect(screen.getAllByText("200").length).toBeGreaterThan(0);
  });

  it("shows a full-page error on load failure", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderSecurity();

    await waitFor(() =>
      expect(screen.getByText(/Security audit log failed to load/)).toBeInTheDocument(),
    );
  });
});

describe("Security page — Phase 3 design-system rebuild", () => {
  it("renders the audit_log table with verdict and layer badges", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("allowed=false")) return { items: [], total: 1, limit: 1, offset: 0 };
      if (url.includes("category=dlp")) return { items: [], total: 1, limit: 1, offset: 0 };
      return {
        items: [
          auditEntry({ tool: "run_command", category: "readonly", allowed: true }),
          auditEntry({ tool: "get_secret", category: "dlp", allowed: false, reason: "PII detected" }),
        ],
        total: 2,
        limit: 200,
        offset: 0,
      };
    });

    renderSecurity();

    await waitFor(() => expect(screen.getByText("allowed")).toBeInTheDocument());
    expect(screen.getByText("denied")).toBeInTheDocument();
    expect(screen.getByText("DLP")).toBeInTheDocument();
    expect(screen.getByText("read-only")).toBeInTheDocument();
  });

  it("the Denied tab filters the table to only denied entries", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("allowed=false")) return { items: [], total: 1, limit: 1, offset: 0 };
      if (url.includes("category=dlp")) return { items: [], total: 0, limit: 1, offset: 0 };
      return {
        items: [
          auditEntry({ tool: "run_command", allowed: true }),
          auditEntry({ tool: "delete_thing", allowed: false, reason: "write blocked" }),
        ],
        total: 2,
        limit: 200,
        offset: 0,
      };
    });

    renderSecurity();

    await waitFor(() => expect(screen.getByText("run_command")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Denied" }));

    expect(screen.queryByText("run_command")).not.toBeInTheDocument();
    expect(screen.getByText("delete_thing")).toBeInTheDocument();
  });

  it("shows a filter-zero empty state (not a false 'no data' message) when a filter matches nothing", async () => {
    mockLoadSuccess({ total: 200, denied: 0, dlp: 0 });

    renderSecurity();

    await waitFor(() => expect(screen.getByText("Audited Calls")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Denied" }));

    expect(await screen.findByText("No audit entries match the current filter")).toBeInTheDocument();
  });

  it("shows a 'none-yet' empty state, distinct from filter-zero, when the log is genuinely empty", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("allowed=false")) return { items: [], total: 0, limit: 1, offset: 0 };
      if (url.includes("category=dlp")) return { items: [], total: 0, limit: 1, offset: 0 };
      return { items: [], total: 0, limit: 200, offset: 0 };
    });

    renderSecurity();

    await waitFor(() => expect(screen.getByText("No audit log entries")).toBeInTheDocument());
    expect(
      screen.getByText("Entries will appear here once agent tool calls are audited."),
    ).toBeInTheDocument();
  });
});
