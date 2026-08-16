import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { RemedRollup } from "./RemedRollup";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — see Remed.test.tsx.
  cleanup();
  vi.restoreAllMocks();
});

const GROUPS = [
  {
    action_type: "config_fix",
    field: "max_staleness",
    count: 3,
    sample_targets: ["host-a", "host-b", "host-c"],
  },
  {
    action_type: "config_fix",
    field: "retention_days",
    count: 1,
    sample_targets: ["host-d"],
  },
];

describe("RemedRollup", () => {
  it("fetches the rollup view and renders one row per group", async () => {
    const getSpy = vi.spyOn(api, "apiGet").mockResolvedValue(GROUPS);
    render(<RemedRollup />);

    await waitFor(() => expect(screen.getByText("max_staleness")).toBeInTheDocument());
    expect(screen.getByText("retention_days")).toBeInTheDocument();
    expect(getSpy).toHaveBeenCalledWith("/api/dashboard/remediation?view=rollup");
  });

  it("shows an empty state when there are no pending groups", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue([]);
    render(<RemedRollup />);

    await waitFor(() =>
      expect(screen.getByText("No pending remediation actions to roll up")).toBeInTheDocument(),
    );
  });

  it("shows an error state when the fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("boom"));
    render(<RemedRollup />);

    await waitFor(() => expect(screen.getByText("Rollup failed to load")).toBeInTheDocument());
  });

  it("calls onDrillToActionType with the row's action_type when clicked", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(GROUPS);
    const onDrill = vi.fn();
    render(<RemedRollup onDrillToActionType={onDrill} />);

    await waitFor(() => expect(screen.getByText("max_staleness")).toBeInTheDocument());
    fireEvent.click(screen.getByText("max_staleness").closest("tr")!);

    expect(onDrill).toHaveBeenCalledWith("config_fix");
  });
});
