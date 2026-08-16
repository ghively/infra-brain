import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Notifications } from "./Notifications";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Vulns.test.tsx/Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const ITEMS = [
  { type: "jira", target: "INFRA-101", title: "Drift detected on web01", domain: "linux", created: "2026-07-20T10:15:30Z", status: "open" },
  { type: "confluence", target: "Runbook: web01", title: "Runbook updated", domain: "linux", created: "2026-07-21T09:00:00Z", status: "synced" },
  { type: "jira", target: "INFRA-102", title: "Compliance gap on db02", domain: "compliance", created: "2026-07-19T08:30:00Z", status: "open" },
];

function mockLoadSuccess(total = 3) {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/api/dashboard/notifications")) {
      return { items: ITEMS, total, limit: 500, offset: 0 };
    }
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

describe("Notifications page — Phase 3 DataTable/Panel rebuild", () => {
  it("renders notifications inside a DataTable with a read-only status-bar footer", async () => {
    mockLoadSuccess();
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("Drift detected on web01")).toBeInTheDocument());

    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
    expect(screen.getAllByText("Jira").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Confluence").length).toBeGreaterThan(0);
  });

  it("uses the DB-wide total from the envelope for the Total stat tile, not just the loaded page length", async () => {
    mockLoadSuccess(250);
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("Drift detected on web01")).toBeInTheDocument());

    const totalTileLabel = screen.getByText("Total");
    const totalTile = totalTileLabel.parentElement;
    expect(totalTile?.textContent).toContain("250");
  });

  it("does NOT render a fabricated 'Open' stat tile (T2 audit regression guard)", async () => {
    mockLoadSuccess();
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("Drift detected on web01")).toBeInTheDocument());

    expect(screen.queryByText("Open")).not.toBeInTheDocument();
    expect(screen.getByText("Jira Tickets")).toBeInTheDocument();
    expect(screen.getAllByText("Confluence").length).toBeGreaterThan(0);
  });

  it("formats timestamps via fmtDt (space-separated date/time), never as a raw ISO string", async () => {
    mockLoadSuccess();
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("Drift detected on web01")).toBeInTheDocument());

    expect(screen.getByText("2026-07-20 10:15:30")).toBeInTheDocument();
    expect(screen.queryByText("2026-07-20T10:15:30Z")).not.toBeInTheDocument();
  });

  it("suppresses the status badge for Jira rows (backend hardcodes status=open, not real ticket state)", async () => {
    mockLoadSuccess();
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("Drift detected on web01")).toBeInTheDocument());

    const jiraRow = screen.getByText("Drift detected on web01").closest("tr");
    expect(jiraRow?.textContent).not.toMatch(/open/i);

    const confluenceRow = screen.getByText("Runbook updated").closest("tr");
    expect(confluenceRow?.textContent).toMatch(/synced/i);
  });

  it("filters by type via the tab buttons", async () => {
    mockLoadSuccess();
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("Drift detected on web01")).toBeInTheDocument());

    screen.getByRole("button", { name: "Confluence" }).click();
    await waitFor(() => expect(screen.queryByText("Drift detected on web01")).not.toBeInTheDocument());
    expect(screen.getByText("Runbook updated")).toBeInTheDocument();
  });

  it("shows the none-yet EmptyState when there are no notifications at all", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/notifications")) return { items: [], total: 0, limit: 500, offset: 0 };
      throw new Error(`unexpected apiGet: ${url}`);
    });

    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("No notifications recorded yet")).toBeInTheDocument());
  });

  it("shows the filter-zero EmptyState (with a clear-filter action) when a tab excludes all rows", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/notifications")) {
        return {
          items: [{ type: "jira", target: "INFRA-1", title: "only jira", domain: "linux", created: "2026-07-20T10:15:30Z", status: "open" }],
          total: 1,
          limit: 500,
          offset: 0,
        };
      }
      throw new Error(`unexpected apiGet: ${url}`);
    });

    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("only jira")).toBeInTheDocument());

    screen.getByRole("button", { name: "Confluence" }).click();
    await waitFor(() =>
      expect(screen.getByText("No notifications match the current filter")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /clear filter/i })).toBeInTheDocument();
  });

  it("renders the target as a deep link when jira_url/confluence_url are present (TRK-178 follow-up)", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/notifications")) {
        return {
          items: [
            {
              type: "jira",
              target: "INFRA-101",
              title: "Drift detected on web01",
              domain: "linux",
              created: "2026-07-20T10:15:30Z",
              status: "open",
              jira_url: "https://jira.example.com/browse/INFRA-101",
              confluence_url: null,
            },
            {
              type: "confluence",
              target: "page-9",
              title: "Runbook updated",
              domain: "linux",
              created: "2026-07-21T09:00:00Z",
              status: "synced",
              jira_url: null,
              confluence_url: "https://confluence.example.com/pages/viewpage.action?pageId=page-9",
            },
          ],
          total: 2,
          limit: 500,
          offset: 0,
        };
      }
      throw new Error(`unexpected apiGet: ${url}`);
    });

    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("INFRA-101")).toBeInTheDocument());

    const jiraLink = screen.getByText("INFRA-101").closest("a");
    expect(jiraLink).toHaveAttribute("href", "https://jira.example.com/browse/INFRA-101");
    expect(jiraLink).toHaveAttribute("target", "_blank");
    expect(jiraLink).toHaveAttribute("rel", "noreferrer");

    const confluenceLink = screen.getByText("page-9").closest("a");
    expect(confluenceLink).toHaveAttribute(
      "href",
      "https://confluence.example.com/pages/viewpage.action?pageId=page-9",
    );
  });

  it("renders the target as plain text (no link) when the corresponding deep-link URL is absent", async () => {
    mockLoadSuccess();
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("Drift detected on web01")).toBeInTheDocument());

    const target = screen.getByText("INFRA-101");
    expect(target.closest("a")).toBeNull();
  });

  it("shows an error EmptyState (not a bare alert div) when the notifications fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/notifications")) throw new Error("db unreachable");
      throw new Error(`unexpected apiGet: ${url}`);
    });

    render(<Notifications />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert").textContent).toMatch(/failed to load/i);
  });
});
