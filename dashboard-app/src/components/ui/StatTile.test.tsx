import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { StatTile, SummaryBlock } from "./StatTile";

afterEach(() => {
  cleanup();
});

describe("StatTile", () => {
  it("renders label, value, unit and sub", () => {
    render(<StatTile label="Hosts" value={42} unit="total" sub="up 3 today" color="green" />);
    expect(screen.getByText("Hosts")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("total")).toBeInTheDocument();
    expect(screen.getByText("up 3 today")).toBeInTheDocument();
  });

  it("applies the semantic color class", () => {
    render(<StatTile label="Critical" value={5} color="red" />);
    const val = screen.getByText("5");
    expect(val.className).toContain("c-red");
  });
});

describe("SummaryBlock", () => {
  const hero = { label: "Risk Score", value: 78, unit: "/100", sub: "trending up" };
  const stats = [
    { label: "Critical", value: 3, color: "red" as const },
    { label: "High", value: 12, color: "yellow" as const },
    { label: "OK", value: 200, color: "green" as const },
    { label: "Info", value: 9, color: "blue" as const },
  ];

  it("renders the hero column and all stat columns", () => {
    render(<SummaryBlock hero={hero} stats={stats} />);
    expect(screen.getByText("Risk Score")).toBeInTheDocument();
    expect(screen.getByText("78")).toBeInTheDocument();
    expect(screen.getByText("/100")).toBeInTheDocument();
    expect(screen.getByText("trending up")).toBeInTheDocument();
    for (const s of stats) {
      expect(screen.getByText(s.label)).toBeInTheDocument();
      expect(screen.getByText(String(s.value))).toBeInTheDocument();
    }
  });

  it("renders a health bar when segments are provided", () => {
    render(
      <SummaryBlock
        hero={hero}
        stats={stats}
        health={[
          { color: "green", fraction: 0.7 },
          { color: "red", fraction: 0.3 },
        ]}
      />,
    );
    expect(screen.getByRole("img", { name: "Health breakdown" })).toBeInTheDocument();
  });

  it("omits the health bar when not provided", () => {
    render(<SummaryBlock hero={hero} stats={stats} />);
    expect(screen.queryByRole("img", { name: "Health breakdown" })).not.toBeInTheDocument();
  });
});
