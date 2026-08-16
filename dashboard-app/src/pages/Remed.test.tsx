import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Remed } from "./Remed";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const ITEMS = [
  { id: "1", agent: "a", action_type: "restart_service", target: "host-pending", confidence: 0.9, status: "pending", approved_by: "—", result_url: "—", created_at: "t" },
  { id: "2", agent: "a", action_type: "restart_service", target: "host-approved", confidence: 0.9, status: "approved", approved_by: "operator", result_url: "—", created_at: "t" },
  { id: "3", agent: "a", action_type: "restart_service", target: "host-executed", confidence: 0.9, status: "executed", approved_by: "operator", result_url: "—", created_at: "t" },
  { id: "4", agent: "a", action_type: "restart_service", target: "host-rejected", confidence: 0.9, status: "rejected", approved_by: "—", result_url: "—", created_at: "t" },
];

function renderRemed() {
  return render(
    <MemoryRouter>
      <Remed />
    </MemoryRouter>,
  );
}

/** Given a stat-tile label (e.g. "Pending (page)"), find the tile's value
 *  <div> (a sibling of the label <div>) and return its `c-{color}` class
 *  suffix, or undefined if it has none. The Phase 1 `StatTile` primitive
 *  colors its value via a `c-{color}` CSS class, not an inline style, so
 *  class-suffix comparison replaces the pre-rebuild inline-`style.color`
 *  comparison. */
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
 *  SeverityPill.tsx's `TONE_CLASS`), read back from a status pill's own
 *  classList. */
function badgeToneClass(status: string): string | undefined {
  const el = screen.getByText(status);
  return Array.from(el.classList).find((c) => c.startsWith("ib-sev-"));
}

// The page maps each remediation status to both a Badge tone (row/drawer
// pill) and a StatTile `color` (stat tile) independently — this table
// mirrors that mapping so the tests can assert the two stay in lockstep
// without depending on inline styles neither primitive uses.
const TONE_CLASS: Record<string, string> = {
  info: "ib-sev-info",
  ok: "ib-sev-ok",
  warn: "ib-sev-med",
  err: "ib-sev-high",
};
const EXPECTED: Record<string, { tone: string; color: string }> = {
  pending: { tone: "info", color: "blue" },
  approved: { tone: "warn", color: "yellow" },
  executed: { tone: "ok", color: "green" },
  rejected: { tone: "err", color: "red" },
};

describe("Remed page — stat tile colors match row badge tones", () => {
  it.each(Object.entries(EXPECTED))("%s: tile color matches the row badge tone", async (status, expected) => {
    vi.spyOn(api, "apiGet").mockResolvedValue(ITEMS);
    renderRemed();
    await waitFor(() => expect(screen.getByText(`host-${status}`)).toBeInTheDocument());

    const label = `${status[0].toUpperCase()}${status.slice(1)} (page)`;
    expect(tileColorClass(label)).toBe(expected.color);
    expect(badgeToneClass(status)).toBe(TONE_CLASS[expected.tone]);
  });

  it("approved and executed are visually distinct statuses (no longer share one color)", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(ITEMS);
    renderRemed();
    await waitFor(() => expect(screen.getByText("host-approved")).toBeInTheDocument());

    expect(badgeToneClass("approved")).not.toBe(badgeToneClass("executed"));
    expect(tileColorClass("Approved (page)")).not.toBe(tileColorClass("Executed (page)"));
  });
});

describe("Remed page — approve/reject state-changing actions", () => {
  it("approves a pending action, optimistically updates status, and POSTs the approve endpoint", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(ITEMS);
    const postSpy = vi.spyOn(api, "apiPost").mockResolvedValue({});
    renderRemed();
    await waitFor(() => expect(screen.getByText("host-pending")).toBeInTheDocument());

    fireEvent.click(screen.getByText("host-pending").closest("tr")!);
    const approveBtn = await screen.findByRole("button", { name: "Approve" });
    fireEvent.click(approveBtn);

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith("/api/dashboard/actions/1/approve", {}),
    );
    await waitFor(() => expect(screen.getByText(/Approved\./)).toBeInTheDocument());
  });

  it("rejects a pending action after a confirm click, and POSTs the reject endpoint", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(ITEMS);
    const postSpy = vi.spyOn(api, "apiPost").mockResolvedValue({});
    renderRemed();
    await waitFor(() => expect(screen.getByText("host-pending")).toBeInTheDocument());

    fireEvent.click(screen.getByText("host-pending").closest("tr")!);
    const rejectBtn = await screen.findByRole("button", { name: "Reject" });
    fireEvent.click(rejectBtn);
    const confirmBtn = await screen.findByRole("button", { name: "Click again to confirm" });
    fireEvent.click(confirmBtn);

    await waitFor(() => expect(postSpy).toHaveBeenCalledWith("/api/dashboard/actions/1/reject"));
    await waitFor(() => expect(screen.getByText("Rejected.")).toBeInTheDocument());
  });

  it("shows an error message and does not change status when the approve call fails", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(ITEMS);
    vi.spyOn(api, "apiPost").mockRejectedValue(new Error("boom"));
    renderRemed();
    await waitFor(() => expect(screen.getByText("host-pending")).toBeInTheDocument());

    fireEvent.click(screen.getByText("host-pending").closest("tr")!);
    const approveBtn = await screen.findByRole("button", { name: "Approve" });
    fireEvent.click(approveBtn);

    await waitFor(() => expect(screen.getByText(/Approve failed: boom/)).toBeInTheDocument());
  });
});
