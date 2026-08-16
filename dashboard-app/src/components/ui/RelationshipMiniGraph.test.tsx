import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { RelationshipMiniGraph } from "./RelationshipMiniGraph";

afterEach(() => {
  cleanup();
});

const hub = { id: "hub", label: "web01" };

function satellites(n: number, flaggedIdx: number[] = []) {
  return Array.from({ length: n }, (_, i) => ({
    id: `sat-${i}`,
    label: `sat${i}`,
    flagged: flaggedIdx.includes(i),
  }));
}

describe("RelationshipMiniGraph", () => {
  it("auto-layouts 10 satellites evenly around the hub", () => {
    const sats = satellites(10);
    render(<RelationshipMiniGraph hub={hub} satellites={sats} edges={[]} />);
    const circles = screen.getAllByTestId("satellite-normal");
    expect(circles).toHaveLength(10);
    // Evenly spaced on a ring of radius 68 around (144,95): no two satellites
    // should share the same (x,y).
    const coords = new Set(circles.map((c) => `${c.getAttribute("cx")},${c.getAttribute("cy")}`));
    expect(coords.size).toBe(10);
  });

  it("auto-layouts 3 satellites evenly (120 degrees apart)", () => {
    const sats = satellites(3);
    render(<RelationshipMiniGraph hub={hub} satellites={sats} edges={[]} />);
    const circles = screen.getAllByTestId("satellite-normal");
    expect(circles).toHaveLength(3);
    // First satellite should be straight up from the hub center (144, 95-68=27).
    expect(Number(circles[0].getAttribute("cx"))).toBeCloseTo(144, 1);
    expect(Number(circles[0].getAttribute("cy"))).toBeCloseTo(27, 1);
  });

  it("handles a single satellite (N=1) without dividing by zero", () => {
    const sats = satellites(1);
    render(<RelationshipMiniGraph hub={hub} satellites={sats} edges={[]} />);
    const circles = screen.getAllByTestId("satellite-normal");
    expect(circles).toHaveLength(1);
    expect(Number(circles[0].getAttribute("cx"))).toBeCloseTo(144, 1);
    expect(Number(circles[0].getAttribute("cy"))).toBeCloseTo(27, 1);
  });

  it("handles zero satellites without throwing", () => {
    render(<RelationshipMiniGraph hub={hub} satellites={[]} edges={[]} />);
    expect(screen.queryAllByTestId("satellite-normal")).toHaveLength(0);
    expect(screen.queryAllByTestId("satellite-flagged")).toHaveLength(0);
  });

  it("renders flagged satellites with the flagged styling, separate from normal ones", () => {
    const sats = satellites(4, [1, 3]);
    render(<RelationshipMiniGraph hub={hub} satellites={sats} edges={[]} />);
    expect(screen.getAllByTestId("satellite-flagged")).toHaveLength(2);
    expect(screen.getAllByTestId("satellite-normal")).toHaveLength(2);
  });

  it("renders normal and flagged edges with distinct test ids", () => {
    const sats = satellites(2);
    render(
      <RelationshipMiniGraph
        hub={hub}
        satellites={sats}
        edges={[
          { fromId: "hub", toId: "sat-0", style: "normal", edgeType: "HOSTED_ON" },
          { fromId: "hub", toId: "sat-1", style: "flagged", edgeType: "AFFECTED_BY_CVE" },
        ]}
      />,
    );
    expect(screen.getAllByTestId("edge-normal")).toHaveLength(1);
    expect(screen.getAllByTestId("edge-flagged")).toHaveLength(1);
  });

  it("includes a human-readable aria-label describing the graph", () => {
    const sats = satellites(2, [1]);
    render(
      <RelationshipMiniGraph
        hub={hub}
        satellites={sats}
        edges={[{ fromId: "hub", toId: "sat-1", style: "flagged", edgeType: "AFFECTED_BY_CVE" }]}
      />,
    );
    const el = screen.getByRole("img", {
      name: /web01.*2 related resources, 1 flagged.*1 flagged edge/,
    });
    expect(el).toBeInTheDocument();
  });
});
