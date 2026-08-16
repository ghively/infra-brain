import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PageShell } from "./PageShell";

afterEach(() => {
  cleanup();
});

describe("PageShell", () => {
  it("renders title and description", () => {
    render(<PageShell title="Fleet" description="Rapid7 asset inventory" />);
    expect(screen.getByText("Fleet")).toBeInTheDocument();
    expect(screen.getByText("Rapid7 asset inventory")).toBeInTheDocument();
  });

  it("renders an icon tile when icon is provided", () => {
    render(<PageShell title="Fleet" icon={<span data-testid="icon">F</span>} />);
    expect(screen.getByTestId("icon")).toBeInTheDocument();
  });

  it("renders status pills", () => {
    render(<PageShell title="Fleet" pills={[{ dot: "#4CBB6C", text: "Live" }]} />);
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("renders the actions slot", () => {
    render(<PageShell title="Fleet" actions={<button>Refresh</button>} />);
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });

  it("omits pills and actions blocks when not provided", () => {
    const { container } = render(<PageShell title="Fleet" />);
    expect(container.querySelector(".ui-page-shell-actions")).not.toBeInTheDocument();
  });
});
