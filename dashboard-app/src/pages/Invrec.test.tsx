import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Invrec } from "./Invrec";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const RESPONSE = {
  items: [
    { host: "host-proposed", domain: "linux", target_group: "g1", status: "proposed", mr_url: "—", detected_at: "t" },
    { host: "host-merged", domain: "linux", target_group: "g1", status: "merged", mr_url: "https://gitlab.example/mr/1", detected_at: "t" },
    { host: "host-rejected", domain: "linux", target_group: "g1", status: "rejected", mr_url: "—", detected_at: "t" },
  ],
  total: 3,
};

function renderInvrec() {
  return render(
    <MemoryRouter>
      <Invrec />
    </MemoryRouter>,
  );
}

/** Given a stat-tile label (e.g. "Proposed"), find the tile's value <div>
 *  (a sibling of the label <div>) and return its `c-{color}` class suffix.
 *  `StatTile` colors its value via a CSS class, not an inline style, so
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

/** Row `Badge` tone → the `ib-sev-*` class it renders, read back from a
 *  status pill's own classList. */
function badgeToneClass(status: string): string | undefined {
  const el = screen.getByText(status);
  return Array.from(el.classList).find((c) => c.startsWith("ib-sev-"));
}

const TONE_CLASS: Record<string, string> = {
  info: "ib-sev-info",
  ok: "ib-sev-ok",
  err: "ib-sev-high",
};
const EXPECTED: Record<string, { label: string; tone: string; color: string }> = {
  proposed: { label: "Proposed", tone: "info", color: "blue" },
  merged: { label: "Merged (page)", tone: "ok", color: "green" },
  rejected: { label: "Rejected (page)", tone: "err", color: "red" },
};

describe("Invrec page — stat tile colors match row badge tones", () => {
  it.each(Object.entries(EXPECTED))("%s: tile color matches the row badge tone", async (status, expected) => {
    vi.spyOn(api, "apiGet").mockImplementation((url: string) =>
      url.includes("/counts") ? Promise.resolve({}) : Promise.resolve(RESPONSE),
    );
    renderInvrec();
    await waitFor(() => expect(screen.getByText(`host-${status}`)).toBeInTheDocument());

    expect(tileColorClass(expected.label)).toBe(expected.color);
    expect(badgeToneClass(status)).toBe(TONE_CLASS[expected.tone]);
  });

  it("Proposed and Merged tiles are colored distinctly (no longer inverted)", async () => {
    vi.spyOn(api, "apiGet").mockImplementation((url: string) =>
      url.includes("/counts") ? Promise.resolve({}) : Promise.resolve(RESPONSE),
    );
    renderInvrec();
    await waitFor(() => expect(screen.getByText("host-proposed")).toBeInTheDocument());

    expect(tileColorClass("Proposed")).not.toBe(tileColorClass("Merged (page)"));
  });
});

describe("Invrec page — mr_url link-affordance consistency (table row vs. drawer)", () => {
  it("renders mr_url as a real link in the table row, not dead monospace text", async () => {
    vi.spyOn(api, "apiGet").mockImplementation((url: string) =>
      url.includes("/counts") ? Promise.resolve({}) : Promise.resolve(RESPONSE),
    );
    renderInvrec();
    await waitFor(() => expect(screen.getByText("host-merged")).toBeInTheDocument());

    const link = screen.getByRole("link", { name: "View MR" });
    expect(link).toHaveAttribute("href", "https://gitlab.example/mr/1");
  });

  it("renders NULL (not a dead link) in the table row when mr_url is absent", async () => {
    vi.spyOn(api, "apiGet").mockImplementation((url: string) =>
      url.includes("/counts") ? Promise.resolve({}) : Promise.resolve(RESPONSE),
    );
    renderInvrec();
    await waitFor(() => expect(screen.getByText("host-proposed")).toBeInTheDocument());

    const proposedRow = screen.getByText("host-proposed").closest("tr")!;
    expect(proposedRow.textContent).toContain("NULL");
  });

  it("opens the drawer with a live 'View merge request' link when mr_url is present", async () => {
    vi.spyOn(api, "apiGet").mockImplementation((url: string) =>
      url.includes("/counts") ? Promise.resolve({}) : Promise.resolve(RESPONSE),
    );
    renderInvrec();
    await waitFor(() => expect(screen.getByText("host-merged")).toBeInTheDocument());

    fireEvent.click(screen.getByText("host-merged").closest("tr")!);
    const drawerLink = await screen.findByRole("link", { name: /View merge request/ });
    expect(drawerLink).toHaveAttribute("href", "https://gitlab.example/mr/1");
  });
});
