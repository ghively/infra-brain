import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Compl } from "./Compl";

/** Renders a stand-in for the Hosts page at the navigation target so the
 *  Host-cell drill-down tests can assert where onClick+navigate actually
 *  lands (F-1), not just that a click handler fired. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="landed">{location.pathname + location.search}</div>;
}

function renderComplWithHostsRoute() {
  return render(
    <MemoryRouter initialEntries={["/compl"]}>
      <Routes>
        <Route path="/compl" element={<Compl />} />
        <Route path="/hosts" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx / McpKeys.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

function renderCompl() {
  return render(
    <MemoryRouter>
      <Compl />
    </MemoryRouter>,
  );
}

const ROW = (overrides: Partial<Record<string, unknown>> = {}) => ({
  rule: "PCI-EOL-01",
  severity: "critical",
  host: "web01",
  detail: "OS past end-of-life",
  status: "open",
  detected_at: "2026-07-23T00:00:00Z",
  ...overrides,
});

describe("Compl page — Open/Resolved tile accuracy (TRK-167 follow-up: fleet-wide aggregate)", () => {
  it("sources Open/Resolved from the fleet-wide /counts aggregate, not the capped list fetch", async () => {
    // 3 rows loaded (all "open" — simulating the newest-first cap hiding
    // older resolved/open rows), but the DB-wide total is far larger, and the
    // /counts aggregate reports the true fleet-wide open/resolved split.
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/counts")) {
        return { compliance_open: 7000, compliance_resolved: 2000 };
      }
      expect(url).toContain("/api/dashboard/compliance");
      expect(url).toContain("limit=500");
      return { items: [ROW(), ROW(), ROW()], total: 9000 };
    });

    renderCompl();

    // "Open"/"Resolved" also label the filter tabs, so scope to the stat
    // tiles (`.lbl` class) rather than a bare text match.
    await waitFor(() => expect(screen.getByText("7000")).toBeInTheDocument());
    const labels = document.querySelectorAll(".lbl");
    const labelText = Array.from(labels).map((el) => el.textContent);
    expect(labelText).toContain("Open");
    expect(labelText).toContain("Resolved");
    expect(labelText).toContain("Total");

    // Open/Resolved reflect the fleet-wide /counts aggregate (7000/2000), not
    // the 3 loaded rows or the 9000 DB-wide total — the accuracy fix.
    expect(screen.getByText("7000")).toBeInTheDocument();
    expect(screen.getByText("2000")).toBeInTheDocument();
    expect(screen.getByText("9000")).toBeInTheDocument();
  });

  it("Total always comes from the response envelope's `total`, never from the loaded row count", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/counts")) {
        return { compliance_open: 2, compliance_resolved: 1 };
      }
      return {
        items: [ROW({ status: "open" }), ROW({ status: "open" }), ROW({ status: "resolved" })],
        total: 42,
      };
    });

    renderCompl();

    await waitFor(() => expect(screen.getByText("42")).toBeInTheDocument());
    // Open=2, Resolved=1 come from the /counts aggregate — well under the
    // true Total (42), proving Total is not derived from the capped array.
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("falls back to 0 for Open/Resolved when the /counts fetch fails, without blanking the rest of the page", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/counts")) {
        throw new Error("counts unreachable");
      }
      return { items: [ROW()], total: 1 };
    });

    renderCompl();

    await waitFor(() => expect(screen.getByText("PCI-EOL-01")).toBeInTheDocument());
    expect(screen.getAllByText("0")).toHaveLength(2);
  });

  it("still renders a plain error message on load failure, matching the other pages' pattern", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderCompl();

    await waitFor(() =>
      expect(screen.getByText(/Compliance failed to load/)).toBeInTheDocument(),
    );
  });

  it("renders the Host cell as a click-through FK button, not a raw label or a raw <a href>", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({ items: [ROW({ host: "db02" })], total: 1 });

    renderCompl();

    await waitFor(() => expect(screen.getByText("db02")).toBeInTheDocument());
    // F-1: a raw `<a href>` escapes the router's `basename="/dashboard2"`
    // (App.tsx) into a hard-navigation 404 against the backend. FkCell's
    // `onClick` prop renders a real `<button>` instead (cells.tsx) — there
    // must be no `<a>` here at all.
    expect(screen.getByText("db02").closest("a")).toBeNull();
    expect(screen.getByText("db02").closest("button")).not.toBeNull();
  });

  it("F-1: clicking the table Host cell navigates to the Hosts-page query shape it actually consumes (?tab=hosts&q=..., not the dead ?host=)", async () => {
    // Regression guard: Hosts.tsx only reads `?tab=`, and its hosts-list
    // child UnifiedHostsTab.tsx only reads `?q=` — neither reads `?host=`.
    vi.spyOn(api, "apiGet").mockResolvedValue({ items: [ROW({ host: "db02" })], total: 1 });

    renderComplWithHostsRoute();

    await waitFor(() => expect(screen.getByText("db02")).toBeInTheDocument());
    fireEvent.click(screen.getByText("db02"));

    await waitFor(() => expect(screen.getByTestId("landed")).toHaveTextContent("/hosts?tab=hosts&q=db02"));
  });

  it("F-1: clicking the detail-drawer Host cell also navigates correctly — the drawer copy of this cell had the identical defect", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({ items: [ROW({ rule: "PCI-EOL-01", host: "db03" })], total: 1 });

    renderComplWithHostsRoute();

    await waitFor(() => expect(screen.getByText("PCI-EOL-01")).toBeInTheDocument());
    fireEvent.click(screen.getByText("PCI-EOL-01"));

    const drawer = await screen.findByRole("dialog");
    const drawerHostCell = within(drawer).getByText("db03");
    expect(drawerHostCell.closest("a")).toBeNull();
    expect(drawerHostCell.closest("button")).not.toBeNull();

    fireEvent.click(drawerHostCell);
    await waitFor(() => expect(screen.getByTestId("landed")).toHaveTextContent("/hosts?tab=hosts&q=db03"));
  });

  it("opens the detail drawer with the full finding on row click", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({ items: [ROW({ detail: "OS past end-of-life" })], total: 1 });

    renderCompl();

    await waitFor(() => expect(screen.getByText("PCI-EOL-01")).toBeInTheDocument());
    fireEvent.click(screen.getByText("PCI-EOL-01"));

    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByText("OS past end-of-life")).toBeInTheDocument();
  });

  it("switches between All/Open/Resolved via the Tabs control", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [ROW({ rule: "OPEN-RULE", status: "open" }), ROW({ rule: "RESOLVED-RULE", status: "resolved" })],
      total: 2,
    });

    renderCompl();

    await waitFor(() => expect(screen.getByText("OPEN-RULE")).toBeInTheDocument());
    expect(screen.getByText("RESOLVED-RULE")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Resolved" }));
    expect(screen.queryByText("OPEN-RULE")).not.toBeInTheDocument();
    expect(screen.getByText("RESOLVED-RULE")).toBeInTheDocument();
  });

  it("shows a none-yet empty state when ComplianceAgent has never produced a finding at all", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({ items: [], total: 0 });

    renderCompl();

    await waitFor(() =>
      expect(screen.getByText("No compliance findings recorded yet")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("No compliance findings recorded yet").closest("[data-empty-kind]"),
    ).toHaveAttribute("data-empty-kind", "none-yet");
  });

  it("shows a clean (good-news) empty state when findings exist but none are currently open", async () => {
    // Findings DO exist (rowsData non-empty via the resolved row below), but
    // switching to the "Open" tab has zero — that's the healthy state, not a
    // gap. The page defaults to the "All" tab, so switch explicitly.
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [ROW({ rule: "RESOLVED-RULE", status: "resolved" })],
      total: 1,
    });

    renderCompl();

    await waitFor(() => expect(screen.getByText("RESOLVED-RULE")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: "Open" }));

    await waitFor(() => expect(screen.getByText("No open compliance violations")).toBeInTheDocument());
    expect(
      screen.getByText("No open compliance violations").closest("[data-empty-kind]"),
    ).toHaveAttribute("data-empty-kind", "clean");
  });

  it("shows a filter-zero empty state when switching to a tab with no matching rows but other data exists", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [ROW({ rule: "OPEN-RULE", status: "open" })],
      total: 1,
    });

    renderCompl();

    await waitFor(() => expect(screen.getByText("OPEN-RULE")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: "Resolved" }));

    await waitFor(() =>
      expect(screen.getByText("No compliance findings in this status")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("No compliance findings in this status").closest("[data-empty-kind]"),
    ).toHaveAttribute("data-empty-kind", "filter-zero");
  });
});
