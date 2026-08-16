import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Instincts } from "./Instincts";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see ScanSchedule.test.tsx/Remed.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const ITEMS = [
  { domain: "linux", zone: "corpor", pattern: "Restart nginx after OOM-kill", confidence: 0.95, promoted_at: "2026-07-01", applied: true },
  { domain: "cloud", zone: "corpor", pattern: "Scale up on sustained CPU > 85%", confidence: 0.72, promoted_at: "2026-07-02", applied: false },
  { domain: "k8s", zone: "corpor", pattern: "Never restart during backup window", confidence: 0.4, promoted_at: "2026-07-03", applied: false },
];

function renderInstincts() {
  return render(
    <MemoryRouter>
      <Instincts />
    </MemoryRouter>,
  );
}

describe("Instincts page — Phase 3 design-system rebuild", () => {
  it("renders instincts in a DataTable with domain, zone, confidence, and applied state", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(ITEMS);
    renderInstincts();

    await waitFor(() => expect(screen.getByText("Restart nginx after OOM-kill")).toBeInTheDocument());
    expect(screen.getAllByText("linux").length).toBeGreaterThan(0);
    expect(screen.getByText("0.95")).toBeInTheDocument();
    expect(screen.getByText("applied to reasoning")).toBeInTheDocument();
    expect(screen.getByText("read-only · no mutations possible from this view")).toBeInTheDocument();
  });

  it("shows the Applied stat tile using the backend's authoritative `applied` field, not a recomputed threshold", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(ITEMS);
    renderInstincts();
    await waitFor(() => expect(screen.getByText("Restart nginx after OOM-kill")).toBeInTheDocument());

    // Only one item has applied:true even though a second item (0.72) clears
    // the legacy >= 0.7 threshold — the tile must reflect the server flag,
    // not a client recomputation of that threshold. "Applied" also appears as
    // a sortable DataTable column header, so pick the stat-tile label (a
    // <div class="lbl">), not the column header <button>.
    const appliedLabel = screen.getAllByText("Applied").find((el) => el.tagName === "DIV" && el.className === "lbl");
    if (!appliedLabel) throw new Error('could not find the "Applied" stat tile label');
    const appliedValue = appliedLabel.nextElementSibling as HTMLElement;
    expect(appliedValue.textContent).toContain("1");
  });

  it("re-fetches with domain/zone as server-side query params when filters change", async () => {
    const getSpy = vi.spyOn(api, "apiGet").mockResolvedValue(ITEMS);
    renderInstincts();
    await waitFor(() => expect(screen.getByText("Restart nginx after OOM-kill")).toBeInTheDocument());

    fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "linux" } });

    await waitFor(() =>
      expect(getSpy).toHaveBeenCalledWith(expect.stringContaining("domain=linux")),
    );
  });

  it("shows a filter-zero EmptyState with a Clear filters action when a filter matches nothing", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValueOnce(ITEMS).mockResolvedValue([]);
    renderInstincts();
    await waitFor(() => expect(screen.getByText("Restart nginx after OOM-kill")).toBeInTheDocument());

    fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "windows" } });

    await waitFor(() => expect(screen.getByText("No instincts match your filters")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("shows a none-yet EmptyState when there are no instincts at all", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue([]);
    renderInstincts();
    await waitFor(() => expect(screen.getByText("No instincts learned yet")).toBeInTheDocument());
  });

  it("shows an error EmptyState when the fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network down"));
    renderInstincts();
    await waitFor(() => expect(screen.getByText("Instincts failed to load")).toBeInTheDocument());
    expect(screen.getByText(/network down/)).toBeInTheDocument();
  });
});
