import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Eol } from "./Eol";

/** Eol.tsx calls `useNavigate()` (F-1: the Host cell drills into Hosts via
 *  onClick+navigate, not a raw `href`), which requires a Router ancestor —
 *  every render here needs at least a bare `<MemoryRouter>`, even tests that
 *  never touch navigation. */
function renderEol() {
  return render(
    <MemoryRouter>
      <Eol />
    </MemoryRouter>,
  );
}

/** Renders a stand-in for the Hosts page at the navigation target so the
 *  Host-cell drill-down test can assert where onClick+navigate actually
 *  lands, not just that a click handler fired. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="landed">{location.pathname + location.search}</div>;
}

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see McpKeys.test.tsx / Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

/** Five EOL registry rows exercising all four proximity bands hosts.py's
 *  list_eol can emit (Bug 1, 2026-07-24 UX audit T6):
 *    - overdue-asset:     past eol_date
 *    - approaching-asset: within the approaching window, risk scored
 *    - tracked-asset:     eol_date far in the future, UNscored (risk null)
 *    - unknown-asset:     no eol_date at all, UNscored (risk null)
 *    - tracked-scored:    eol_date far in the future, risk scored
 *  total=7 > items.length=5 simulates the ?limit=200 page cap (Bug 2).
 *
 *  pci_risk_score values (80/60/40) are real points on EOLAgent's actual 0-100
 *  scale (TRK-275/GitLab #146+#153) — not placeholders on the old, wrong 0-10
 *  assumption. */
const FIVE_OF_SEVEN = {
  items: [
    { id: "1", asset: "overdue-asset", host: "h1", eol: "2020-01-01", pci_risk_score: 80, migration: "—", status: "overdue" },
    { id: "2", asset: "approaching-asset", host: "h2", eol: "2026-08-15", pci_risk_score: 60, migration: "—", status: "approaching" },
    { id: "3", asset: "tracked-asset", host: "h3", eol: "2036-01-01", pci_risk_score: null, migration: "—", status: "tracked" },
    { id: "4", asset: "unknown-asset", host: "h4", eol: "—", pci_risk_score: null, migration: "—", status: "unknown" },
    { id: "5", asset: "tracked-scored", host: "h5", eol: "2036-06-01", pci_risk_score: 40, migration: "—", status: "tracked" },
  ],
  total: 7,
  limit: 200,
  offset: 0,
};

function mockLoadSuccess() {
  vi.spyOn(api, "apiGet").mockResolvedValue(FIVE_OF_SEVEN);
}

describe("Eol page — proximity bands, total count, and risk average", () => {
  it("renders four distinct badge states, not a binary overdue/approaching flag", async () => {
    mockLoadSuccess();
    renderEol();

    await waitFor(() => expect(screen.getByText("overdue-asset")).toBeInTheDocument());

    // Each row's badge text must reflect its own real band.
    const overdueRow = screen.getByText("overdue-asset").closest("tr") as HTMLElement;
    expect(within(overdueRow).getByText("overdue")).toBeInTheDocument();

    const approachingRow = screen.getByText("approaching-asset").closest("tr") as HTMLElement;
    expect(within(approachingRow).getByText("approaching")).toBeInTheDocument();

    const trackedRow = screen.getByText("tracked-asset").closest("tr") as HTMLElement;
    expect(within(trackedRow).getByText("tracked")).toBeInTheDocument();

    const unknownRow = screen.getByText("unknown-asset").closest("tr") as HTMLElement;
    expect(within(unknownRow).getByText("unknown")).toBeInTheDocument();
  });

  it("counts only the real 'approaching' band in the Approaching tile — a decade-out or undated asset must not inflate it", async () => {
    mockLoadSuccess();
    renderEol();

    await waitFor(() => expect(screen.getByText("overdue-asset")).toBeInTheDocument());

    // Exactly one asset (approaching-asset) is genuinely within the window;
    // tracked-asset, tracked-scored, and unknown-asset must be excluded.
    const approachingTile = screen.getByText("Approaching").parentElement as HTMLElement;
    expect(within(approachingTile).getByText("1")).toBeInTheDocument();

    const overdueTile = screen.getByText("Overdue").parentElement as HTMLElement;
    expect(within(overdueTile).getByText("1")).toBeInTheDocument();
  });

  it("shows the true registry total in the Tracked tile, not the capped page length", async () => {
    mockLoadSuccess();
    renderEol();

    await waitFor(() => expect(screen.getByText("overdue-asset")).toBeInTheDocument());

    const trackedTile = screen.getByText("Tracked").parentElement as HTMLElement;
    // 7 (total), not 5 (items.length) — Bug 2.
    expect(within(trackedTile).getByText("7")).toBeInTheDocument();
  });

  it("shows a 'showing X of Y' note when the fetched page is smaller than the total", async () => {
    mockLoadSuccess();
    renderEol();

    await waitFor(() => expect(screen.getByText("overdue-asset")).toBeInTheDocument());

    expect(screen.getByText(/showing 5 of 7 EOL assets/)).toBeInTheDocument();
  });

  it("does not show a capped note when the page is not truncated", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: FIVE_OF_SEVEN.items.slice(0, 2),
      total: 2,
      limit: 200,
      offset: 0,
    });
    renderEol();

    await waitFor(() => expect(screen.getByText("overdue-asset")).toBeInTheDocument());
    expect(screen.queryByText(/showing .* of .* EOL assets/)).not.toBeInTheDocument();
  });

  it("excludes unscored (null pci_risk_score) assets from the Avg PCI Risk denominator", async () => {
    mockLoadSuccess();
    renderEol();

    await waitFor(() => expect(screen.getByText("overdue-asset")).toBeInTheDocument());

    // Scored assets are 80, 60, 40 → avg 60.0. Including the two null-risk
    // assets as 0 would drag this down — the pre-fix bug (Bug 3).
    const avgTile = screen.getByText("Avg PCI Risk").parentElement as HTMLElement;
    expect(within(avgTile).getByText("60.0")).toBeInTheDocument();
  });

  it("renders the per-row PCI Risk Score cell as NULL for an unscored asset rather than 0/100", async () => {
    mockLoadSuccess();
    renderEol();

    await waitFor(() => expect(screen.getByText("tracked-asset")).toBeInTheDocument());

    const trackedRow = screen.getByText("tracked-asset").closest("tr") as HTMLElement;
    expect(within(trackedRow).getByText("NULL")).toBeInTheDocument();
    expect(within(trackedRow).queryByText("0/100")).not.toBeInTheDocument();
  });

  it("scales the PCI Risk Score gauge/severity on the real 0-100 range, not a 0-10 one (TRK-275)", async () => {
    mockLoadSuccess();
    renderEol();

    await waitFor(() => expect(screen.getByText("overdue-asset")).toBeInTheDocument());

    // overdue-asset (80) renders its real value/100, not ×10 (which would be
    // "800/100" under the pre-fix scaling bug).
    const overdueRow = screen.getByText("overdue-asset").closest("tr") as HTMLElement;
    expect(within(overdueRow).getByText("80/100")).toBeInTheDocument();

    // tracked-scored (40) must land in the "medium" severity band, not
    // "critical" — under the pre-fix 0-10 thresholds (>=8 → critical), every
    // real score (10/40/70/90) was >=8 and always rendered critical.
    const trackedScoredRow = screen.getByText("tracked-scored").closest("tr") as HTMLElement;
    const gauge = within(trackedScoredRow).getByText("40/100").closest(".ib-gauge") as HTMLElement;
    expect(gauge.className).toContain("ib-g-med");
    expect(gauge.className).not.toContain("ib-g-crit");
  });

  it("renders the Host cell as a click-through FK button, not a raw label or a raw <a href>", async () => {
    mockLoadSuccess();
    renderEol();

    await waitFor(() => expect(screen.getByText("overdue-asset")).toBeInTheDocument());

    const overdueRow = screen.getByText("overdue-asset").closest("tr") as HTMLElement;
    // F-1: a raw `<a href>` escapes the router's `basename="/dashboard2"`
    // (App.tsx) into a hard-navigation 404 against the backend, which serves
    // the SPA only at /dashboard2. FkCell's `onClick` prop renders a real
    // `<button>` instead (cells.tsx) — there must be no `<a>` here at all.
    expect(within(overdueRow).queryByText("h1")?.closest("a")).toBeNull();
    expect(within(overdueRow).getByText("h1").closest("button")).not.toBeNull();
  });

  it("F-1: clicking the Host cell navigates to the Hosts-page query shape it actually consumes (?tab=hosts&q=..., not the dead ?host=)", async () => {
    // Regression guard: Hosts.tsx only reads `?tab=`, and its hosts-list
    // child UnifiedHostsTab.tsx only reads `?q=` — neither reads `?host=`.
    // A link built with `?host=` silently lands on Hosts unfiltered.
    mockLoadSuccess();
    render(
      <MemoryRouter initialEntries={["/eol"]}>
        <Routes>
          <Route path="/eol" element={<Eol />} />
          <Route path="/hosts" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText("overdue-asset")).toBeInTheDocument());
    const overdueRow = screen.getByText("overdue-asset").closest("tr") as HTMLElement;
    fireEvent.click(within(overdueRow).getByText("h1"));

    await waitFor(() => expect(screen.getByTestId("landed")).toHaveTextContent("/hosts?tab=hosts&q=h1"));
  });
});
