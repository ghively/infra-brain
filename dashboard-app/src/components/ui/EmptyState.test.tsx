import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EmptyState } from "./EmptyState";

afterEach(() => {
  cleanup();
});

describe("EmptyState", () => {
  it("renders none-yet kind with status role", () => {
    render(<EmptyState kind="none-yet" title="No Rapid7 sites collected yet" />);
    const el = screen.getByRole("status");
    expect(el).toHaveAttribute("data-empty-kind", "none-yet");
    expect(screen.getByText("No Rapid7 sites collected yet")).toBeInTheDocument();
  });

  it("renders filter-zero kind distinctly from none-yet", () => {
    render(<EmptyState kind="filter-zero" title="No assets match the current filters" />);
    const el = screen.getByRole("status");
    expect(el).toHaveAttribute("data-empty-kind", "filter-zero");
    expect(el.className).not.toContain("ui-empty-error");
  });

  it("renders error kind with alert role and error styling", () => {
    render(<EmptyState kind="error" title="Sites failed to load" />);
    const el = screen.getByRole("alert");
    expect(el).toHaveAttribute("data-empty-kind", "error");
    expect(el.className).toContain("ui-empty-error");
  });

  it("renders an optional hint and action button", () => {
    const onClick = vi.fn();
    render(
      <EmptyState
        kind="error"
        title="Sites failed to load"
        hint="The Rapid7 API returned a 500."
        action={{ label: "Retry", onClick }}
      />,
    );
    expect(screen.getByText("The Rapid7 API returned a 500.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("uses distinct default icons per kind", () => {
    const { container: c1 } = render(<EmptyState kind="none-yet" title="A" />);
    const { container: c2 } = render(<EmptyState kind="filter-zero" title="B" />);
    const { container: c3 } = render(<EmptyState kind="error" title="C" />);
    const { container: c4 } = render(<EmptyState kind="not-configured" title="D" />);
    const { container: c5 } = render(<EmptyState kind="clean" title="E" />);
    const icon1 = c1.querySelector(".ui-empty-icon")?.textContent;
    const icon2 = c2.querySelector(".ui-empty-icon")?.textContent;
    const icon3 = c3.querySelector(".ui-empty-icon")?.textContent;
    const icon4 = c4.querySelector(".ui-empty-icon")?.textContent;
    const icon5 = c5.querySelector(".ui-empty-icon")?.textContent;
    expect(new Set([icon1, icon2, icon3, icon4, icon5]).size).toBe(5);
  });

  it("renders not-configured kind with status role and its own styling hook", () => {
    render(
      <EmptyState
        kind="not-configured"
        title="Vulnerabilities are not configured"
        hint="Set RAPID7_URL / RAPID7_API_KEY to enable this collector."
      />,
    );
    const el = screen.getByRole("status");
    expect(el).toHaveAttribute("data-empty-kind", "not-configured");
    expect(el.className).toContain("ui-empty-not-configured");
    expect(el.className).not.toContain("ui-empty-error");
    expect(screen.getByText("Vulnerabilities are not configured")).toBeInTheDocument();
  });

  it("renders clean kind distinctly — good news, not an error or a gap", () => {
    render(<EmptyState kind="clean" title="No open drift events" hint="Fleet-wide — nothing is currently drifting." />);
    const el = screen.getByRole("status");
    expect(el).toHaveAttribute("data-empty-kind", "clean");
    expect(el.className).toContain("ui-empty-clean");
    expect(el.className).not.toContain("ui-empty-error");
  });
});
