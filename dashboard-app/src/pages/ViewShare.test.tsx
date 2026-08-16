import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { ApiError } from "../api";
import { ViewShare } from "./ViewShare";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderAt(token: string) {
  return render(
    <MemoryRouter initialEntries={[`/views/${token}`]}>
      <Routes>
        <Route path="/views/:token" element={<ViewShare />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ViewShare page — Phase 3 design-system rebuild", () => {
  it("renders the shared view's title, sharing note, prompt, and OpenUI output", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      id: "v1",
      title: "Fleet Overview",
      prompt: "show me the fleet",
      openui_lang: "text: hello",
      is_public: true,
      created_at: "2026-07-01T00:00:00Z",
    });

    renderAt("tok123");

    await waitFor(() => expect(screen.getByText("Fleet Overview")).toBeInTheDocument());
    expect(screen.getByText(/Shared publicly/)).toBeInTheDocument();
    expect(screen.getByText("“show me the fleet”")).toBeInTheDocument();
  });

  it("preserves the 403-forbidden case (TRK-198) as an EmptyState, not a generic error", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new ApiError(403, "forbidden"));

    renderAt("tok-private");

    await waitFor(() =>
      expect(screen.getByText("You do not have access to this view")).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("shows a distinct error EmptyState on a non-403 load failure", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderAt("tok-broken");

    await waitFor(() => expect(screen.getByText("Shared view failed to load")).toBeInTheDocument());
  });
});
