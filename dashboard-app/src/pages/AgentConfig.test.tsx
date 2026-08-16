import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { AgentConfig } from "./AgentConfig";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx / McpKeys.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

function agentConfigRow(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    domain: "linux",
    last_status: "success",
    last_error: null,
    last_run_at: "2026-07-24T00:00:00Z",
    ready: true,
    requirements: [{ key: "LINUX_HOST", label: "SSH host", configured: true, current_value: "10.0.0.1" }],
    ...overrides,
  };
}

function mockLoadSuccess(items: Record<string, unknown>[]) {
  vi.spyOn(api, "apiGet").mockImplementation(async () => ({ items, total: items.length }));
}

function renderAgentConfig() {
  return render(
    <MemoryRouter>
      <AgentConfig />
    </MemoryRouter>,
  );
}

describe("AgentConfig page — Phase 3 design-system rebuild", () => {
  it("renders domain cards with status and ready badges", async () => {
    mockLoadSuccess([agentConfigRow({ domain: "linux", ready: true })]);

    renderAgentConfig();

    await waitFor(() => expect(screen.getByText("LINUX")).toBeInTheDocument());
    expect(screen.getByText("success")).toBeInTheDocument();
    expect(screen.getByText("ready")).toBeInTheDocument();
  });

  it("counts a domain's last_error in the stat tile even when the domain is ready (T-fix preserved)", async () => {
    mockLoadSuccess([
      agentConfigRow({ domain: "linux", ready: true, last_error: "collector timeout" }),
      agentConfigRow({ domain: "windows", ready: true, last_error: null }),
    ]);

    renderAgentConfig();

    await waitFor(() => expect(screen.getByText("LINUX")).toBeInTheDocument());

    const lastErrorsTile = screen.getByText("Last Errors").parentElement as HTMLElement;
    expect(lastErrorsTile.textContent).toContain("1");
    // The error block itself must render even though the domain is `ready`.
    expect(screen.getByText("collector timeout")).toBeInTheDocument();
  });

  it("collapses requirements by default only when all are configured, and expands on click", async () => {
    mockLoadSuccess([
      agentConfigRow({
        domain: "linux",
        requirements: [{ key: "LINUX_HOST", label: "SSH host", configured: true, current_value: "10.0.0.1" }],
      }),
    ]);

    renderAgentConfig();

    await waitFor(() => expect(screen.getByText("LINUX")).toBeInTheDocument());
    expect(screen.queryByText("LINUX_HOST")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("Requirements"));
    expect(screen.getByText("LINUX_HOST")).toBeInTheDocument();
  });

  it("shows requirements expanded by default when not all are configured", async () => {
    mockLoadSuccess([
      agentConfigRow({
        domain: "vsphere",
        ready: false,
        requirements: [
          { key: "VSPHERE_HOST", label: "vCenter host", configured: false, current_value: null },
        ],
      }),
    ]);

    renderAgentConfig();

    await waitFor(() => expect(screen.getByText("VSPHERE")).toBeInTheDocument());
    expect(screen.getByText("VSPHERE_HOST")).toBeInTheDocument();
    expect(screen.getByText("not ready")).toBeInTheDocument();
  });

  it("triggers a sweep and shows a flash message", async () => {
    mockLoadSuccess([agentConfigRow({ domain: "linux" })]);
    const postSpy = vi.spyOn(api, "apiPost").mockResolvedValue({});

    renderAgentConfig();

    await waitFor(() => expect(screen.getByText("LINUX")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Trigger Sweep" }));

    expect(postSpy).toHaveBeenCalledWith("/api/dashboard/sweeps/linux");
    expect(await screen.findByText(/Sweep triggered for linux/)).toBeInTheDocument();
  });

  it("disables a collector via PUT to the dispatchable runtime-config key, then flips to re-enable via DELETE", async () => {
    mockLoadSuccess([agentConfigRow({ domain: "linux" })]);
    const putSpy = vi.spyOn(api, "apiPut").mockResolvedValue({});
    const deleteSpy = vi.spyOn(api, "apiDelete").mockResolvedValue(undefined);

    renderAgentConfig();

    await waitFor(() => expect(screen.getByText("LINUX")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Disable Collector" }));

    expect(putSpy).toHaveBeenCalledWith("/api/dashboard/runtime-config/dispatchable__linux", {
      value: "false",
      value_type: "bool",
      category: "tuning",
    });
    expect(await screen.findByText(/Collector disabled for linux/)).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Enable Collector" }));

    expect(deleteSpy).toHaveBeenCalledWith("/api/dashboard/runtime-config/dispatchable__linux");
    expect(await screen.findByText(/Collector re-enabled for linux/)).toBeInTheDocument();
  });

  it("initializes the collector toggle from the server's `paused` field on load", async () => {
    // Before the backend echoed `paused` back, this state was click-only: a
    // domain paused in an earlier session rendered "Disable Collector", i.e.
    // as if its collector were still running.
    mockLoadSuccess([
      agentConfigRow({ domain: "linux", paused: true }),
      agentConfigRow({ domain: "vsphere", paused: false }),
    ]);

    renderAgentConfig();

    await waitFor(() => expect(screen.getByText("LINUX")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Enable Collector" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disable Collector" })).toBeInTheDocument();
  });

  it("treats a response with no `paused` field as not paused", async () => {
    mockLoadSuccess([agentConfigRow({ domain: "linux" })]);

    renderAgentConfig();

    await waitFor(() => expect(screen.getByText("LINUX")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Disable Collector" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable Collector" })).not.toBeInTheDocument();
  });

  it("re-syncs the toggle from the server when a refetch reports a different pause state", async () => {
    // The optimistic click-state must not win over a later server read (e.g.
    // another admin re-enabled the collector, or the PUT was rolled back).
    let paused = true;
    vi.spyOn(api, "apiGet").mockImplementation(async () => ({
      items: [agentConfigRow({ domain: "linux", paused })],
      total: 1,
    }));

    renderAgentConfig();
    await waitFor(() => expect(screen.getByRole("button", { name: "Enable Collector" })).toBeInTheDocument());

    // A later load (auto-refresh, or a remount) must pick up the server's view.
    paused = false;
    cleanup();
    renderAgentConfig();

    await waitFor(() => expect(screen.getByRole("button", { name: "Disable Collector" })).toBeInTheDocument());
  });

  it("shows a 'none-yet' empty state when no domains are configured", async () => {
    mockLoadSuccess([]);

    renderAgentConfig();

    await waitFor(() => expect(screen.getByText("No agent configuration loaded")).toBeInTheDocument());
  });

  it("shows a full-page error on load failure", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderAgentConfig();

    await waitFor(() => expect(screen.getByText("Agent config failed to load")).toBeInTheDocument());
  });
});
