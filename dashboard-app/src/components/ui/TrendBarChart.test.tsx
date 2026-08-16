import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TrendBarChart } from "./TrendBarChart";

afterEach(() => {
  cleanup();
});

describe("TrendBarChart", () => {
  const data = [
    { label: "Mon", total: 10, subset: 2 },
    { label: "Tue", total: 20, subset: 5 },
    { label: "Wed", total: 5, subset: 5 },
  ];

  it("scales the tallest bar's total segment height to the full plot height", () => {
    render(
      <TrendBarChart
        data={data}
        totalColor="#5b9de8"
        subsetColor="#f0654e"
        legend={{ totalLabel: "Total", subsetLabel: "Critical" }}
        height={120}
      />,
    );
    const bars = screen.getAllByTestId("bar");
    // Tue has the max total (20) -> scale = (120-4)/20 = 5.8
    // Tue: total px = round(20*5.8)=116, subset px=round(5*5.8)=29, rest=87
    const tueSubset = bars[1].querySelector('[data-testid="seg-subset"]') as HTMLElement;
    const tueTotal = bars[1].querySelector('[data-testid="seg-total"]') as HTMLElement;
    expect(tueSubset).toHaveStyle({ height: "29px" });
    expect(tueTotal).toHaveStyle({ height: "87px" });
  });

  it("renders proportionally shorter bars for smaller totals", () => {
    render(
      <TrendBarChart
        data={data}
        totalColor="#5b9de8"
        subsetColor="#f0654e"
        legend={{ totalLabel: "Total", subsetLabel: "Critical" }}
        height={120}
      />,
    );
    const bars = screen.getAllByTestId("bar");
    // Mon total=10 -> px = round(10*5.8)=58, subset=round(2*5.8)=12, rest=46
    const monSubset = bars[0].querySelector('[data-testid="seg-subset"]') as HTMLElement;
    const monTotal = bars[0].querySelector('[data-testid="seg-total"]') as HTMLElement;
    expect(monSubset).toHaveStyle({ height: "12px" });
    expect(monTotal).toHaveStyle({ height: "46px" });
  });

  it("outlines the current bar's subset segment (defaults to last period)", () => {
    render(
      <TrendBarChart
        data={data}
        totalColor="#5b9de8"
        subsetColor="#f0654e"
        legend={{ totalLabel: "Total", subsetLabel: "Critical" }}
      />,
    );
    const bars = screen.getAllByTestId("bar");
    expect(bars[2].className).toContain("cur");
    expect(bars[0].className).not.toContain("cur");
  });

  it("respects an explicit currentIndex override", () => {
    render(
      <TrendBarChart
        data={data}
        totalColor="#5b9de8"
        subsetColor="#f0654e"
        legend={{ totalLabel: "Total", subsetLabel: "Critical" }}
        currentIndex={0}
      />,
    );
    const bars = screen.getAllByTestId("bar");
    expect(bars[0].className).toContain("cur");
    expect(bars[2].className).not.toContain("cur");
  });

  it("renders the x-axis period labels", () => {
    render(
      <TrendBarChart
        data={data}
        totalColor="#5b9de8"
        subsetColor="#f0654e"
        legend={{ totalLabel: "Total", subsetLabel: "Critical" }}
      />,
    );
    expect(screen.getByText("Mon")).toBeInTheDocument();
    expect(screen.getByText("Tue")).toBeInTheDocument();
    expect(screen.getByText("Wed")).toBeInTheDocument();
  });

  it("includes a human-readable aria-label describing the whole series", () => {
    render(
      <TrendBarChart
        data={data}
        totalColor="#5b9de8"
        subsetColor="#f0654e"
        legend={{ totalLabel: "Total", subsetLabel: "Critical" }}
      />,
    );
    const el = screen.getByRole("img", {
      name: /Total trend over 3 periods.*Mon: 10 total, 2 critical.*Tue: 20 total, 5 critical.*Wed: 5 total, 5 critical/,
    });
    expect(el).toBeInTheDocument();
  });

  it("handles an empty data array without throwing", () => {
    render(
      <TrendBarChart
        data={[]}
        totalColor="#5b9de8"
        subsetColor="#f0654e"
        legend={{ totalLabel: "Total", subsetLabel: "Critical" }}
      />,
    );
    expect(screen.getByRole("img", { name: /no data/ })).toBeInTheDocument();
  });
});
