import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Decisions } from "./Decisions";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Security.test.tsx / Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

function decision(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    agent: "linux",
    domain: "linux",
    run_id: "run-1",
    iteration: 1,
    decision_summary: "Collected host facts",
    reasoning_text: "Chose to run a read-only fact-gathering tool.",
    tools_chosen: ["get_facts"],
    ts: "2026-07-24T00:00:00Z",
    ...overrides,
  };
}

function renderDecisions() {
  return render(
    <MemoryRouter>
      <Decisions />
    </MemoryRouter>,
  );
}

describe("Decisions page — Phase 3 design-system rebuild", () => {
  it("groups decisions into sessions by run_id, sorted by iteration", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [
        decision({ run_id: "run-1", iteration: 2, decision_summary: "Second step" }),
        decision({ run_id: "run-1", iteration: 1, decision_summary: "First step" }),
      ],
      total: 2,
    });

    renderDecisions();

    await waitFor(() => expect(screen.getByText("First step")).toBeInTheDocument());
    expect(screen.getByText("Second step")).toBeInTheDocument();
    expect(screen.getByText("1 sessions")).toBeInTheDocument();
    expect(screen.getByText("2 iterations")).toBeInTheDocument();
  });

  it("renders a tool chip per chosen tool, falling back to '(reasoning only)' when none were used", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [decision({ tools_chosen: [] })],
      total: 1,
    });

    renderDecisions();

    await waitFor(() => expect(screen.getByText("(reasoning only)")).toBeInTheDocument());
  });

  it("filters by agent via the server-side query param (debounced)", async () => {
    const apiGet = vi.spyOn(api, "apiGet").mockResolvedValue({ items: [decision()], total: 1 });

    renderDecisions();
    await waitFor(() => expect(screen.getByText("Collected host facts")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText("Filter by agent…"), { target: { value: "windows" } });

    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith(expect.stringContaining("agent=windows")),
    );
  });

  it("shows a filter-zero empty state distinct from none-yet when a filter matches nothing", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({ items: [], total: 0 });

    renderDecisions();
    await waitFor(() => expect(screen.getByText("No reasoning sessions recorded yet")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText("Filter by run_id…"), { target: { value: "nope" } });

    await waitFor(() =>
      expect(screen.getByText("No reasoning sessions match your filters")).toBeInTheDocument(),
    );
  });

  // T7 (rev14): reasoning_text is NOT NULL in the schema, so "the model wrote
  // no prose this turn" arrives as the same empty string a capture failure
  // would. Rendering that as a blank line implied nothing happened — on ~39
  // live tool-call turns, which is exactly the audit-misleading case.
  it("explains an empty reasoning_text instead of rendering a blank line", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [
        decision({
          iteration: 0,
          decision_summary: "",
          reasoning_text: "",
          tools_chosen: ["gitlab_file_tool"],
        }),
      ],
      total: 1,
    });

    renderDecisions();

    await waitFor(() =>
      expect(
        screen.getByText(/called tools without writing any reasoning\. Nothing was lost\./),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText("No summary")).toBeInTheDocument();
  });

  it("distinguishes a reasoning-only turn with no text from a silent tool-call turn", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [decision({ reasoning_text: "", tools_chosen: [] })],
      total: 1,
    });

    renderDecisions();

    await waitFor(() =>
      expect(screen.getByText("No reasoning text was recorded for this step.")).toBeInTheDocument(),
    );
  });

  it("seeds the run_id filter from the ?run_id= query param for deep links", async () => {
    const apiGet = vi.spyOn(api, "apiGet").mockResolvedValue({ items: [decision()], total: 1 });

    render(
      <MemoryRouter initialEntries={["/decisions?run_id=abc-123"]}>
        <Decisions />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith(expect.stringContaining("run_id=abc-123")),
    );
    expect(screen.getByPlaceholderText("Filter by run_id…")).toHaveValue("abc-123");
  });

  it("shows a full-page error state on load failure", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderDecisions();

    await waitFor(() => expect(screen.getByText("Decisions failed to load")).toBeInTheDocument());
  });
});
