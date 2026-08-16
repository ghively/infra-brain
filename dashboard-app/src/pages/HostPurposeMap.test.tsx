import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { HostPurposeMap } from "./HostPurposeMap";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Security.test.tsx/Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const ENTRIES = [
  {
    hostname: "web01.example.test",
    purpose: "Web frontend",
    vlan: "110",
    subnet: "10.0.10.0/24",
    source: "manual",
    updated_at: "2026-07-20T00:00:00Z",
  },
  {
    hostname: "db01.example.test",
    purpose: null,
    vlan: null,
    subnet: null,
    source: null,
    updated_at: null,
  },
];

function mockLoadSuccess(items = ENTRIES) {
  vi.spyOn(api, "apiGet").mockResolvedValue({ items, total: items.length, limit: 1000, offset: 0 });
}

function renderPage() {
  return render(
    <MemoryRouter>
      <HostPurposeMap />
    </MemoryRouter>,
  );
}

describe("HostPurposeMap page — Phase 3 design-system rebuild", () => {
  it("renders the host purpose map table with a NULL cell for missing fields", async () => {
    mockLoadSuccess();

    renderPage();

    await waitFor(() => expect(screen.getByText("web01.example.test")).toBeInTheDocument());
    expect(screen.getByText("Web frontend")).toBeInTheDocument();
    expect(screen.getByText("db01.example.test")).toBeInTheDocument();
    // Missing purpose/vlan/subnet/source/updated_at on the second row must
    // render the dim-italic NULL token, never a blank cell (DESIGN.md §4).
    expect(screen.getAllByText("NULL").length).toBeGreaterThan(0);
    expect(screen.getByText(/read-only/)).toBeInTheDocument();
  });

  it("shows the true hosts-mapped count in the header pill", async () => {
    mockLoadSuccess();

    renderPage();

    await waitFor(() => expect(screen.getByText("2 hosts mapped")).toBeInTheDocument());
  });

  it("client-side search filters by hostname, purpose, or VLAN", async () => {
    mockLoadSuccess();

    renderPage();

    await waitFor(() => expect(screen.getByText("web01.example.test")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText("Search hostname / purpose / VLAN…"), {
      target: { value: "frontend" },
    });

    expect(screen.getByText("web01.example.test")).toBeInTheDocument();
    expect(screen.queryByText("db01.example.test")).not.toBeInTheDocument();
  });

  it("shows a filter-zero empty state when the search matches nothing", async () => {
    mockLoadSuccess();

    renderPage();

    await waitFor(() => expect(screen.getByText("web01.example.test")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText("Search hostname / purpose / VLAN…"), {
      target: { value: "no-such-host" },
    });

    expect(await screen.findByText("No hosts match the current search")).toBeInTheDocument();
  });

  it("shows a 'none-yet' empty state when there are no entries at all", async () => {
    mockLoadSuccess([]);

    renderPage();

    await waitFor(() => expect(screen.getByText("No host purpose entries yet")).toBeInTheDocument());
  });

  it("shows a full-page error state on load failure", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderPage();

    await waitFor(() => expect(screen.getByText("Host purpose map failed to load")).toBeInTheDocument());
  });
});
