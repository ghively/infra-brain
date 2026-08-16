import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Activity } from "./Activity";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx / McpKeys.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

function activityRow(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    ts: "2026-07-24T00:00:00Z",
    agent: "linux",
    domain: "linux",
    tool: "run_command",
    verdict: "allow",
    status: "ok",
    latency_ms: 42,
    args_summary: "cmd=uptime",
    error: "",
    ...overrides,
  };
}

// F-5: the real backend now receives `agent=`/`verdict=` query params and
// filters server-side (governance_audit.py's list_activity) — this mock
// mirrors that so tests exercise the actual client<->server contract instead
// of asserting against a client-side filter the component no longer applies.
function mockLoadSuccess(items: Record<string, unknown>[], total?: number) {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/api/dashboard/agents")) {
      return { items: [{ domain: "linux" }, { domain: "windows" }], total: 2 };
    }
    const params = new URLSearchParams(url.split("?")[1] ?? "");
    const agentParam = params.get("agent");
    const verdictParam = params.get("verdict");
    const matched = items.filter(
      (i) => (!agentParam || i.agent === agentParam) && (!verdictParam || i.verdict === verdictParam),
    );
    return { items: matched, total: total ?? matched.length };
  });
}

function renderActivity() {
  return render(
    <MemoryRouter>
      <Activity />
    </MemoryRouter>,
  );
}

describe("Activity page — Phase 3 design-system rebuild", () => {
  it("shows the true envelope total on the Tool Calls tile, not the fetched window length", async () => {
    mockLoadSuccess([activityRow()], 999);

    renderActivity();

    await waitFor(() => expect(screen.getByText("Tool Calls")).toBeInTheDocument());
    expect(screen.getByText("999")).toBeInTheDocument();
  });

  it("renders the activity table with a verdict badge", async () => {
    mockLoadSuccess([
      activityRow({ tool: "run_command", verdict: "allow" }),
      activityRow({ tool: "delete_thing", verdict: "deny", error: "write blocked" }),
    ]);

    renderActivity();

    await waitFor(() => expect(screen.getByText("run_command")).toBeInTheDocument());
    expect(screen.getByText("allow")).toBeInTheDocument();
    expect(screen.getByText("deny")).toBeInTheDocument();
    expect(screen.getByText("read-only · no mutations possible from this view")).toBeInTheDocument();
  });

  it("the Denied tab filters the table to only denied entries", async () => {
    mockLoadSuccess([
      activityRow({ tool: "run_command", verdict: "allow" }),
      activityRow({ tool: "delete_thing", verdict: "deny", error: "write blocked" }),
    ]);

    renderActivity();

    await waitFor(() => expect(screen.getByText("run_command")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Denied" }));

    // F-5: this now round-trips through a real (mocked) server fetch rather
    // than an in-memory filter, so the update is asynchronous.
    await waitFor(() => expect(screen.queryByText("run_command")).not.toBeInTheDocument());
    expect(screen.getByText("delete_thing")).toBeInTheDocument();
  });

  it("the agent dropdown filters by domain and is populated from the live roster", async () => {
    mockLoadSuccess([
      activityRow({ tool: "linux_tool", agent: "linux" }),
      activityRow({ tool: "windows_tool", agent: "windows" }),
    ]);

    renderActivity();

    await waitFor(() => expect(screen.getByText("linux_tool")).toBeInTheDocument());

    const select = await screen.findByDisplayValue("(all)");
    fireEvent.change(select, { target: { value: "windows" } });

    // F-5: async server round-trip, not a synchronous in-memory filter.
    await waitFor(() => expect(screen.queryByText("linux_tool")).not.toBeInTheDocument());
    expect(screen.getByText("windows_tool")).toBeInTheDocument();
  });

  it("F-5: pushes the agent filter server-side so a match outside the fetched window is not falsely reported as no matches", async () => {
    // Simulate the exact failure mode: the unfiltered 500-row window is all
    // "linux" tool calls (the agent's last calls scrolled outside the window
    // for "windows"), but the backend DOES have matching "windows" rows and
    // returns them once the filter is actually sent as a query param.
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/agents")) {
        return { items: [{ domain: "linux" }, { domain: "windows" }], total: 2 };
      }
      if (url.includes("agent=windows")) {
        return { items: [activityRow({ tool: "windows_only_tool", agent: "windows" })], total: 1 };
      }
      // Unfiltered fetch: only "linux" rows in the truncated window.
      return { items: [activityRow({ tool: "linux_tool", agent: "linux" })], total: 1 };
    });

    renderActivity();

    await waitFor(() => expect(screen.getByText("linux_tool")).toBeInTheDocument());

    const select = await screen.findByDisplayValue("(all)");
    fireEvent.change(select, { target: { value: "windows" } });

    // A real server-side-filtered result must appear -- not a false "no
    // matches" derived from client-filtering the stale unfiltered window.
    await waitFor(() => expect(screen.getByText("windows_only_tool")).toBeInTheDocument());
    expect(screen.queryByText("No activity entries match the current filters")).not.toBeInTheDocument();
  });

  it("shows a filter-zero empty state (not a false 'no data' message) when a filter matches nothing", async () => {
    mockLoadSuccess([activityRow({ verdict: "allow" })]);

    renderActivity();

    await waitFor(() => expect(screen.getByText("Tool Calls")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Denied" }));

    expect(await screen.findByText("No activity entries match the current filters")).toBeInTheDocument();
  });

  it("shows a 'none-yet' empty state, distinct from filter-zero, when there is no activity at all", async () => {
    mockLoadSuccess([]);

    renderActivity();

    await waitFor(() => expect(screen.getByText("No activity recorded")).toBeInTheDocument());
  });

  it("shows a full-page error on load failure", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderActivity();

    await waitFor(() => expect(screen.getByText("Activity failed to load")).toBeInTheDocument());
  });
});
