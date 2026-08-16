import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { Help } from "./Help";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — see
  // Home.test.tsx / Homelab.test.tsx for the same pattern.
  cleanup();
});

function renderHelp() {
  return render(
    <MemoryRouter>
      <Help />
    </MemoryRouter>,
  );
}

describe("Help / Getting Started page", () => {
  it("renders the page header", () => {
    renderHelp();
    expect(screen.getByText("Help / Getting Started")).toBeInTheDocument();
  });

  it("renders the read-only guarantee section with all three enforcement layers", () => {
    renderHelp();
    expect(screen.getByText("The read-only guarantee")).toBeInTheDocument();
    expect(screen.getByText("1. Structural")).toBeInTheDocument();
    expect(screen.getByText("2. Boundary gate")).toBeInTheDocument();
    expect(screen.getByText("3. Audit")).toBeInTheDocument();
  });

  it("renders the collection section explaining schedule → Postgres → derived findings", () => {
    renderHelp();
    expect(
      screen.getByText("Where to see a sweep run, and what a failed one looks like"),
    ).toBeInTheDocument();
    // Links out to the pages that actually carry this data.
    expect(screen.getByRole("link", { name: "Collection Runs" })).toHaveAttribute("href", "/collruns");
    expect(screen.getByRole("link", { name: "Scan Schedule" })).toHaveAttribute("href", "/scanschedule");
    expect(screen.getByRole("link", { name: "Sweeps" })).toHaveAttribute("href", "/sweeps");
  });

  it("is honest that Sweeps reads empty unless sweep_graph_enabled is on", () => {
    renderHelp();
    expect(screen.getByText(/sweep_graph_enabled/)).toBeInTheDocument();
    expect(screen.getByText(/off by default in this codebase/)).toBeInTheDocument();
  });

  it("renders the knowledge graph section, naming SAME_AS and linking to /graph", () => {
    renderHelp();
    expect(screen.getByText(/SAME_AS/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Knowledge Graph" })).toHaveAttribute("href", "/graph");
  });

  it("renders the LLM section with the real on/off state of every gated flag", () => {
    renderHelp();
    expect(screen.getByText(/rag_enabled/)).toBeInTheDocument();
    expect(screen.getByText(/rootcause_llm_enabled/)).toBeInTheDocument();
    expect(screen.getByText(/compliance_gap_finder_enabled/)).toBeInTheDocument();
    expect(screen.getByText(/remediation_interrupt_enabled/)).toBeInTheDocument();
    // Every one of the four flags described above is off by default — assert
    // there are exactly four "off" badges in that panel's key/value rows, not
    // e.g. one flag accidentally described as on.
    expect(screen.getAllByText("off")).toHaveLength(4);
  });

  it("renders the agent roster section explaining the status vocabulary, including retired", () => {
    renderHelp();
    expect(screen.getByText(/what 'retired' means/)).toBeInTheDocument();
    expect(
      screen.getByText(/Switched off by a deliberate, standing decision/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Agents" })).toHaveAttribute("href", "/agents");
  });

  it("renders the secrets section and points at the masked Settings view", () => {
    renderHelp();
    expect(screen.getByText(/Bitwarden Secrets Manager/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
  });
});
