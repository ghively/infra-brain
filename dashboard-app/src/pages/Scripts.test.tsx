import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Scripts } from "./Scripts";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx/ScanSchedule.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const SCRIPTS = [
  {
    name: "check_stale_hosts",
    language: "python",
    purpose: "Flags hosts with no successful sweep in 7+ days.",
    run_count: 4,
    last_returncode: 0,
    created_by_agent: "linux",
    domain: "linux",
    git_path: "scripts/check_stale_hosts.py",
    last_run_at: "2026-07-27T12:00:00Z",
    created_at: "2026-07-01T00:00:00Z",
  },
  {
    name: "vuln_summary",
    language: "python",
    purpose: "Summarizes open critical CVEs.",
    run_count: 2,
    last_returncode: 1,
    created_by_agent: "vulns",
    domain: "vulns",
    git_path: "scripts/vuln_summary.py",
    last_run_at: "2026-07-26T09:00:00Z",
    created_at: "2026-06-15T00:00:00Z",
  },
  {
    name: "unused_draft",
    language: "bash",
    purpose: "Draft script, never executed.",
    run_count: 0,
    last_returncode: -1,
    created_by_agent: "linux",
    domain: "linux",
    git_path: "scripts/unused_draft.sh",
    last_run_at: null,
    created_at: "2026-07-20T00:00:00Z",
  },
];

function mockLoadSuccess() {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/api/dashboard/generated_scripts")) {
      return { items: SCRIPTS, total: SCRIPTS.length };
    }
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function renderScripts() {
  return render(<Scripts />);
}

function findRowCell(text: string): HTMLElement {
  const match = screen.getAllByText(text).find((el) => el.closest("tr"));
  if (!match) throw new Error(`no table-row match for "${text}"`);
  return match;
}

describe("Scripts page — Phase 3 design-system rebuild", () => {
  it("renders generated scripts in a DataTable with name, language, and run outcome", async () => {
    mockLoadSuccess();
    renderScripts();

    await waitFor(() => expect(screen.getAllByText("check_stale_hosts").length).toBeGreaterThan(0));
    expect(screen.getByText("read-only · no mutations possible from this view")).toBeInTheDocument();
    const row = findRowCell("check_stale_hosts").closest("tr");
    expect(row?.textContent).toContain("exit 0");
  });

  it("renders 'never run' (not a false failure badge) for a script with zero runs, despite last_returncode being the -1 sentinel", async () => {
    mockLoadSuccess();
    renderScripts();

    await waitFor(() => expect(screen.getAllByText("unused_draft").length).toBeGreaterThan(0));
    const row = findRowCell("unused_draft").closest("tr");
    expect(row?.textContent).toContain("never run");
    expect(row?.textContent).not.toContain("exit -1");
  });

  it("renders a non-zero exit code badge for a failing script", async () => {
    mockLoadSuccess();
    renderScripts();

    await waitFor(() => expect(screen.getAllByText("vuln_summary").length).toBeGreaterThan(0));
    const row = findRowCell("vuln_summary").closest("tr");
    expect(row?.textContent).toContain("exit 1");
  });

  it("computes Success Rate excluding never-run scripts from the denominator", async () => {
    mockLoadSuccess();
    renderScripts();

    // 2 ever-run scripts (check_stale_hosts ok, vuln_summary failing) -> 50%,
    // unused_draft (run_count 0) must not count against the rate.
    await waitFor(() => expect(screen.getByText("Success Rate")).toBeInTheDocument());
    expect(screen.getByText("50%")).toBeInTheDocument();
  });

  it("shows an error EmptyState (not a bare alert div) when the fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async () => {
      throw new Error("db unreachable");
    });

    renderScripts();
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert").textContent).toMatch(/failed to load/i);
  });

  it("shows a none-yet EmptyState when there are zero generated scripts", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async () => ({ items: [], total: 0 }));

    renderScripts();
    await waitFor(() => expect(screen.getByText("No generated scripts yet")).toBeInTheDocument());
  });
});
