import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { SavedViews } from "./SavedViews";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const SHARE_URL = "/dashboard2/views/abc123";

const ONE_VIEW = [
  {
    id: "v1",
    title: "Fleet overview",
    prompt: "show me the fleet",
    openui_lang: null,
    is_public: true,
    share_url: SHARE_URL,
    created_at: "2026-07-24T00:00:00Z",
  },
];

function renderSavedViews() {
  return render(
    <MemoryRouter>
      <SavedViews />
    </MemoryRouter>,
  );
}

describe("SavedViews copy link (T0 share-link audit)", () => {
  it("copies the absolute URL with no double /dashboard2 prefix — share_url from the backend already includes it", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(ONE_VIEW);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    renderSavedViews();
    await waitFor(() => expect(screen.getByText("Fleet overview")).toBeInTheDocument());

    screen.getByText("Copy link").click();

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));

    const expected = `${window.location.origin}${SHARE_URL}`;
    expect(writeText).toHaveBeenCalledWith(expected);

    // Regression guard: the exact double-prefix bug this test covers.
    expect(writeText.mock.calls[0][0]).not.toContain("/dashboard2/dashboard2");
  });
});
