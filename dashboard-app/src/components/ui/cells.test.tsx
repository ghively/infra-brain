import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { DomainCell, FkCell, GaugeCell, NullCell, isNullish } from "./cells";
import { Badge, SeverityPill, normalizeSeverity } from "./SeverityPill";

// This repo runs Vitest with `globals: false`, so React Testing Library cannot
// auto-register its own afterEach cleanup — without this each test's render
// leaks into the next and `screen` queries match duplicated elements.
afterEach(cleanup);

describe("NullCell", () => {
  it("renders the NULL token, not a blank", () => {
    render(<NullCell />);
    const el = screen.getByText("NULL");
    expect(el).toHaveClass("ib-null");
  });
});

describe("isNullish", () => {
  it("treats null, undefined and blank strings as missing", () => {
    expect(isNullish(null)).toBe(true);
    expect(isNullish(undefined)).toBe(true);
    expect(isNullish("   ")).toBe(true);
  });

  it("does not treat 0 or false as missing", () => {
    expect(isNullish(0)).toBe(false);
    expect(isNullish(false)).toBe(false);
  });
});

describe("SeverityPill", () => {
  const cases: [string, string][] = [
    ["critical", "ib-sev-crit"],
    ["high", "ib-sev-high"],
    ["medium", "ib-sev-med"],
    ["low", "ib-sev-low"],
  ];

  it.each(cases)("renders %s with class %s", (severity, cls) => {
    const { container } = render(<SeverityPill severity={severity} />);
    const pill = container.querySelector(".ib-sev");
    expect(pill).toHaveClass(cls);
  });

  it("normalizes messy backend spellings", () => {
    expect(normalizeSeverity("CRITICAL")).toBe("critical");
    expect(normalizeSeverity(" Crit ")).toBe("critical");
    expect(normalizeSeverity("moderate")).toBe("medium");
    expect(normalizeSeverity("sev2")).toBe("high");
    expect(normalizeSeverity("bogus")).toBeNull();
    expect(normalizeSeverity(null)).toBeNull();
  });

  it("keeps an unrecognized label's own text instead of relabeling it Low", () => {
    render(<SeverityPill severity="Whatever" />);
    const pill = screen.getByText("Whatever");
    expect(pill).toHaveClass("ib-sev-low");
  });

  it("renders canonical title-case labels", () => {
    render(<SeverityPill severity="CRITICAL" />);
    expect(screen.getByText("Critical")).toBeInTheDocument();
  });
});

describe("Badge", () => {
  it("maps tones onto pill classes", () => {
    const { container } = render(<Badge tone="warn">stale</Badge>);
    expect(container.querySelector(".ib-sev")).toHaveClass("ib-sev-med");
  });
});

describe("FkCell", () => {
  it("renders a link with a human label and a dim id", () => {
    render(<FkCell label="deploy-host-01" id="a1b2c3" href="/hosts/a1b2c3" />);
    const link = screen.getByRole("link", { name: /deploy-host-01/ });
    expect(link).toHaveClass("ib-fk");
    expect(link).toHaveAttribute("href", "/hosts/a1b2c3");
    expect(screen.getByText("a1b2c3")).toHaveClass("ib-fkid");
  });

  it("fires its handler when clicked", async () => {
    const onClick = vi.fn();
    render(<FkCell label="deploy-host-01" onClick={onClick} />);
    const btn = screen.getByRole("button", { name: /deploy-host-01/ });
    fireEvent.click(btn);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("omits the id span when there is no id", () => {
    const { container } = render(<FkCell label="orphan" />);
    expect(container.querySelector(".ib-fkid")).toBeNull();
  });
});

describe("GaugeCell", () => {
  it("clamps the bar width and colors it by severity", () => {
    const { container } = render(<GaugeCell value={140} severity="critical" display="9.8" />);
    const gauge = container.querySelector(".ib-gauge");
    expect(gauge).toHaveClass("ib-g-crit");
    expect(container.querySelector(".ib-bar i")).toHaveStyle({ width: "100%" });
    expect(screen.getByText("9.8")).toBeInTheDocument();
  });

  it("floors negative and non-finite values at 0", () => {
    const { container } = render(<GaugeCell value={Number.NaN} severity="low" />);
    expect(container.querySelector(".ib-bar i")).toHaveStyle({ width: "0%" });
  });
});

describe("DomainCell", () => {
  it("renders the domain token", () => {
    render(<DomainCell name="windows" />);
    expect(screen.getByText("windows")).toHaveClass("ib-dom");
  });
});
