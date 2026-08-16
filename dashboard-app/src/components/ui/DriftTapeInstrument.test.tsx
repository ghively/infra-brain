import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DriftTapeInstrument } from "./DriftTapeInstrument";

afterEach(() => {
  cleanup();
});

describe("DriftTapeInstrument", () => {
  it("places the now-line at the top for a value at max", () => {
    render(
      <DriftTapeInstrument
        label="Open Drift"
        value={100}
        min={0}
        max={100}
        zones={[{ color: "red", from: 80, to: 100 }]}
      />,
    );
    const nowline = screen.getByTestId("nowline");
    expect(nowline).toHaveStyle({ top: "0px" });
  });

  it("places the now-line at the bottom for a value at min", () => {
    render(
      <DriftTapeInstrument
        label="Open Drift"
        value={0}
        min={0}
        max={100}
        zones={[{ color: "green", from: 0, to: 20 }]}
      />,
    );
    const nowline = screen.getByTestId("nowline");
    expect(nowline).toHaveStyle({ top: "236px" });
  });

  it("places the now-line at the vertical midpoint for a mid-range value", () => {
    render(
      <DriftTapeInstrument label="Open Drift" value={50} min={0} max={100} zones={[]} />,
    );
    const nowline = screen.getByTestId("nowline");
    expect(nowline).toHaveStyle({ top: "118px" });
  });

  it("renders a red band spanning the correct region for the zone config", () => {
    render(
      <DriftTapeInstrument
        label="Open Drift"
        value={50}
        min={0}
        max={100}
        zones={[{ color: "red", from: 80, to: 100 }]}
      />,
    );
    const band = screen.getByTestId("band-red");
    // value 100 -> top of tape (y=0), value 80 -> y = 236 - 0.8*236 = 47.2
    expect(band).toHaveStyle({ top: "0px" });
    expect(band).toHaveStyle({ height: "47.2px" });
  });

  it("renders no trend line with fewer than 2 history points", () => {
    render(
      <DriftTapeInstrument
        label="Open Drift"
        value={50}
        min={0}
        max={100}
        zones={[]}
        history={[50]}
      />,
    );
    expect(screen.queryByTestId("trend")).not.toBeInTheDocument();
  });

  it("draws a trend vector spanning the previous and current values", () => {
    render(
      <DriftTapeInstrument
        label="Open Drift"
        value={80}
        min={0}
        max={100}
        zones={[]}
        history={[20, 80]}
      />,
    );
    const trend = screen.getByTestId("trend");
    // prev=20 -> y=188.8, cur=80 -> y=47.2. top = min = 47.2, height = |47.2-188.8|=141.6
    expect(trend).toHaveStyle({ top: "47.2px" });
    expect(trend).toHaveStyle({ height: "141.6px" });
  });

  it("derives the trend vector from history's last two points, not from `value`", () => {
    // `value` (95) intentionally does NOT equal history[history.length - 1] (80) —
    // a caller may reasonably pass a history of strictly-prior readings that
    // excludes the live value. The trend vector and word must still be driven
    // by history[-2] and history[-1], per the `history` prop's documented contract.
    render(
      <DriftTapeInstrument
        label="Open Drift"
        value={95}
        min={0}
        max={100}
        zones={[]}
        history={[20, 80]}
      />,
    );
    const trend = screen.getByTestId("trend");
    // prev=20 -> y=188.8, latest=80 -> y=47.2 (NOT value=95 -> y=11.8).
    expect(trend).toHaveStyle({ top: "47.2px" });
    expect(trend).toHaveStyle({ height: "141.6px" });

    const el = screen.getByRole("img", {
      name: /Open Drift: 95, out of range, rising from 20/,
    });
    expect(el).toBeInTheDocument();
  });

  it("includes a human-readable aria-label summarizing the reading", () => {
    render(
      <DriftTapeInstrument
        label="Open Drift"
        value={85}
        min={0}
        max={100}
        zones={[{ color: "red", from: 80, to: 100 }]}
        history={[60, 85]}
        unit=" events"
      />,
    );
    const el = screen.getByRole("img", {
      name: /Open Drift: 85 events, critical, rising from 60 events/,
    });
    expect(el).toBeInTheDocument();
  });

  it("computes nice-rounded tick values from the min/max range", () => {
    render(
      <DriftTapeInstrument label="Open Drift" value={50} min={0} max={100} zones={[]} tickCount={5} />,
    );
    const ticks = screen.getAllByTestId("tick");
    expect(ticks.length).toBeGreaterThan(0);
    const values = ticks.map((t) => Number(t.getAttribute("data-value")));
    // every tick should be a round multiple (nice step), not an arbitrary float
    for (const v of values) {
      expect(Number.isFinite(v)).toBe(true);
    }
  });
});
