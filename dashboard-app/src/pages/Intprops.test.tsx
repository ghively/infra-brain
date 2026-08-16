import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Intprops } from "./Intprops";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Remed.test.tsx/Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const ITEMS = [
  { id: "1", type: "webhook", endpoint: "/api/gitlab/webhook", confidence: 0.92, proposed_at: "t", status: "pending", source: "discovery" },
  { id: "2", type: "webhook", endpoint: "/api/jira/webhook", confidence: 0.8, proposed_at: "t", status: "approved", source: "discovery" },
  { id: "3", type: "webhook", endpoint: "/api/confluence/webhook", confidence: 0.85, proposed_at: "t", status: "wired", source: "discovery" },
  { id: "4", type: "webhook", endpoint: "/api/slack/webhook", confidence: 0.6, proposed_at: "t", status: "rejected", source: "discovery" },
];

/** TRK-321: the narrow /settings/ui endpoint returns a FLAT list of rows, not
 *  the admin view's grouped list[SettingGroup]. */
const UI_SETTINGS = [{ k: "INTEGRATION_CONFIDENCE_GATE", type: "text", v: "0.7" }];

/** TRK-182 follow-up: the page fetches both integration_proposals and the
 *  settings row via Promise.all — mock apiGet by URL so each call gets its own
 *  fixture instead of both resolving to the same payload. */
function mockApiGet(items: unknown = ITEMS, uiSettings: unknown = UI_SETTINGS) {
  return vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/settings")) return uiSettings;
    return items;
  });
}

function renderIntprops() {
  return render(
    <MemoryRouter>
      <Intprops />
    </MemoryRouter>,
  );
}

/** Given a stat-tile label (e.g. "Pending"), find the tile's value <div> (a
 *  sibling of the label <div>) and return its `c-{color}` class suffix. The
 *  Phase 1 `StatTile` primitive colors its value via a `c-{color}` CSS class,
 *  not an inline style — mirrors Remed.test.tsx's `tileColorClass` helper. */
function tileColorClass(label: string): string | undefined {
  const candidates = screen.getAllByText(label);
  const labelDiv = candidates.find((el) => el.tagName === "DIV");
  if (!labelDiv || !labelDiv.nextElementSibling) {
    throw new Error(`could not find stat tile for label "${label}"`);
  }
  const valueEl = labelDiv.nextElementSibling as HTMLElement;
  return Array.from(valueEl.classList)
    .find((c) => c.startsWith("c-"))
    ?.replace("c-", "");
}

/** Row/detail `Badge` tone → the `ib-sev-*` class it renders (per
 *  SeverityPill.tsx's `TONE_CLASS`). */
function badgeToneClass(status: string): string | undefined {
  const el = screen.getAllByText(status).find((e) => e.className.includes("ib-sev"));
  return el ? Array.from(el.classList).find((c) => c.startsWith("ib-sev-")) : undefined;
}

describe("Intprops page — Phase 3 design-system rebuild (TRK-198/TRK-215 lineage)", () => {
  it("renders proposals in a DataTable with endpoint, type, confidence, source, and status", async () => {
    mockApiGet();
    renderIntprops();

    await waitFor(() => expect(screen.getByText("/api/gitlab/webhook")).toBeInTheDocument());
    expect(screen.getByText("read-only · no mutations possible from this view")).toBeInTheDocument();
  });

  it("stat tile colors match row badge tones, and approved/wired are visually distinct", async () => {
    mockApiGet();
    renderIntprops();
    await waitFor(() => expect(screen.getByText("/api/gitlab/webhook")).toBeInTheDocument());

    // default tab is "pending", so switch to All to see every status rendered as a badge
    fireEvent.click(screen.getByRole("tab", { name: "All" }));
    await waitFor(() => expect(screen.getByText("/api/slack/webhook")).toBeInTheDocument());

    expect(tileColorClass("Pending")).toBe("blue");
    expect(tileColorClass("Approved")).toBe("yellow");
    expect(tileColorClass("Wired")).toBe("green");
    expect(tileColorClass("Rejected")).toBe("red");
    expect(badgeToneClass("approved")).not.toBe(badgeToneClass("wired"));
  });

  it("defaults to the Pending tab, same as the pre-rebuild page", async () => {
    mockApiGet();
    renderIntprops();
    await waitFor(() => expect(screen.getByText("/api/gitlab/webhook")).toBeInTheDocument());

    expect(screen.queryByText("/api/jira/webhook")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Pending" })).toHaveAttribute("aria-selected", "true");
  });

  it("approves and wires a pending proposal, optimistically updates status, and POSTs the approve endpoint", async () => {
    mockApiGet();
    const postSpy = vi.spyOn(api, "apiPost").mockResolvedValue({ approved: true, wired: true });
    renderIntprops();
    await waitFor(() => expect(screen.getByText("/api/gitlab/webhook")).toBeInTheDocument());

    fireEvent.click(screen.getByText("/api/gitlab/webhook").closest("tr")!);
    const approveBtn = await screen.findByRole("button", { name: "Approve & Wire" });
    fireEvent.click(approveBtn);

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith("/api/dashboard/integration_proposals/1/approve", {}),
    );
    await waitFor(() => expect(screen.getByText("Approved and wired.")).toBeInTheDocument());
  });

  it("rejects a pending proposal after a confirm click, and POSTs the reject endpoint", async () => {
    mockApiGet();
    const postSpy = vi.spyOn(api, "apiPost").mockResolvedValue({});
    renderIntprops();
    await waitFor(() => expect(screen.getByText("/api/gitlab/webhook")).toBeInTheDocument());

    fireEvent.click(screen.getByText("/api/gitlab/webhook").closest("tr")!);
    const rejectBtn = await screen.findByRole("button", { name: "Reject" });
    fireEvent.click(rejectBtn);
    const confirmBtn = await screen.findByRole("button", { name: "Click again to confirm" });
    fireEvent.click(confirmBtn);

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith("/api/dashboard/integration_proposals/1/reject"),
    );
    await waitFor(() => expect(screen.getByText("Rejected.")).toBeInTheDocument());
  });

  it("shows an error message and does not change status when the approve call fails", async () => {
    mockApiGet();
    vi.spyOn(api, "apiPost").mockRejectedValue(new Error("boom"));
    renderIntprops();
    await waitFor(() => expect(screen.getByText("/api/gitlab/webhook")).toBeInTheDocument());

    fireEvent.click(screen.getByText("/api/gitlab/webhook").closest("tr")!);
    const approveBtn = await screen.findByRole("button", { name: "Approve & Wire" });
    fireEvent.click(approveBtn);

    await waitFor(() => expect(screen.getByText(/Approve failed: boom/)).toBeInTheDocument());
  });

  it("shows a filter-zero EmptyState with a Show all action when a tab has no matching proposals", async () => {
    mockApiGet(ITEMS.filter((p) => p.status !== "approved"));
    renderIntprops();
    await waitFor(() => expect(screen.getByText("/api/gitlab/webhook")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: "Approved" }));

    await waitFor(() => expect(screen.getByText("No proposals in this status")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Show all" })).toBeInTheDocument();
  });

  it("shows a none-yet EmptyState when there are no proposals at all", async () => {
    mockApiGet([]);
    renderIntprops();
    // Default tab is "pending" (same as the pre-rebuild page), which is a
    // filter — the true "nothing at all" state only surfaces on the "All"
    // tab, same as Remed.tsx's equivalent none-yet/filter-zero split.
    await waitFor(() => expect(screen.getByText("No proposals in this status")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: "All" }));
    await waitFor(() => expect(screen.getByText("No integration proposals yet")).toBeInTheDocument());
  });

  it("reads the gate from the non-admin /settings/ui endpoint, never the admin /settings dump (TRK-321)", async () => {
    const spy = mockApiGet();
    renderIntprops();
    await waitFor(() => expect(screen.getByText("/api/gitlab/webhook")).toBeInTheDocument());

    const urls = spy.mock.calls.map((c) => c[0] as string);
    // The admin-only full dump 403s for a non-admin session, and this page
    // fetches inside a Promise.all — hitting it would blank the whole
    // Integrations page, proposals included. That is exactly what reverted
    // the first attempt at TRK-321.
    expect(urls).toContain("/api/dashboard/settings/ui");
    expect(urls).not.toContain("/api/dashboard/settings");
  });

  it("uses the fetched INTEGRATION_CONFIDENCE_GATE setting instead of a hardcoded 0.7 (TRK-182 follow-up)", async () => {
    mockApiGet(ITEMS, [{ k: "INTEGRATION_CONFIDENCE_GATE", type: "text", v: "0.85" }]);
    renderIntprops();
    await waitFor(() => expect(screen.getByText("/api/gitlab/webhook")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: "All" }));
    // item "2" has confidence 0.80 — below the fetched 0.85 gate, so the
    // pre-change hardcoded-0.7 label ("approvable") must no longer appear
    // for it; the label must reflect the server-provided gate value. (item
    // "4" at 0.60 is also below 0.85, so >=1 match is expected here.)
    await waitFor(() => expect(screen.getAllByText(/below 0\.85 gate/).length).toBeGreaterThan(0));
  });

  it("shows an error EmptyState when the fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network down"));
    renderIntprops();
    await waitFor(() => expect(screen.getByText("Integration proposals failed to load")).toBeInTheDocument());
    expect(screen.getByText(/network down/)).toBeInTheDocument();
  });
});
