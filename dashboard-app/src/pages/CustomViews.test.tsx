import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { CustomViews } from "./CustomViews";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx / McpKeys.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const SHARE_URL = "/dashboard2/views/abc123";

function renderCustomViews() {
  // Seed navigation state with a non-empty `output` so the "Save & Share"
  // button (which only renders once `output` is truthy) is present without
  // having to drive the streaming /custom-view generator.
  return render(
    <MemoryRouter
      initialEntries={[
        { pathname: "/customviews", state: { output: "<FleetStatCard />", prompt: "show fleet" } },
      ]}
    >
      <CustomViews />
    </MemoryRouter>,
  );
}

describe("CustomViews share link copy (T0 share-link audit)", () => {
  it("copies an absolute URL (origin + share_url), not the bare relative path", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue([]); // initial saved-views list load
    vi.spyOn(api, "apiPost").mockResolvedValue({
      id: "v1",
      title: "Fleet overview",
      prompt: "show fleet",
      is_public: true,
      share_url: SHARE_URL,
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    renderCustomViews();

    screen.getByText("Save & Share").click();
    await waitFor(() => expect(screen.getByText("Save Custom View")).toBeInTheDocument());

    screen.getByText("Save").click();
    await waitFor(() => expect(screen.getByText("Copy")).toBeInTheDocument());

    screen.getByText("Copy").click();

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));

    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toBe(`${window.location.origin}${SHARE_URL}`);
    expect(copied.startsWith("http")).toBe(true);
    // Regression guard against the double-prefix bug fixed alongside this one.
    expect(copied).not.toContain("/dashboard2/dashboard2");
  });
});
