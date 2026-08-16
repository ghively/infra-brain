import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Panel, PanelFootItem } from "./Panel";

// This repo runs Vitest with `globals: false`, so React Testing Library cannot
// auto-register its own afterEach cleanup — without this each test's render
// leaks into the next and `screen` queries match duplicated elements.
afterEach(cleanup);

describe("Panel", () => {
  it("renders the mnemonic, description and children", () => {
    render(
      <Panel mnemonic="VULN" description="vulnerabilities ⨝ resources">
        <p>body content</p>
      </Panel>,
    );
    expect(screen.getByText("VULN")).toHaveClass("ib-mn");
    expect(screen.getByText("vulnerabilities ⨝ resources")).toHaveClass("ib-pd");
    expect(screen.getByText("body content")).toBeInTheDocument();
  });

  it("names the landmark from the mnemonic so it is reachable by role", () => {
    render(<Panel mnemonic="DRIFT">x</Panel>);
    expect(screen.getByRole("region", { name: "DRIFT" })).toBeInTheDocument();
  });

  it("applies the requested rail hue class", () => {
    const { container } = render(<Panel rail="err" mnemonic="VULN" />);
    expect(container.querySelector(".ib-rail")).toHaveClass("ib-rail-err");
    expect(container.querySelector(".ib-panel")).toHaveAttribute("data-rail", "err");
  });

  it("defaults the rail to info", () => {
    const { container } = render(<Panel mnemonic="TREND" />);
    expect(container.querySelector(".ib-rail")).toHaveClass("ib-rail-info");
  });

  it("renders the LIVE marker only when live is set", () => {
    const { container, unmount } = render(<Panel mnemonic="VULN" />);
    expect(container.querySelector(".ib-live")).toBeNull();
    unmount();
    const live = render(<Panel mnemonic="VULN" live />);
    expect(live.container.querySelector(".ib-live")).toHaveTextContent("LIVE");
  });

  it("omits the header entirely when nothing header-ish is supplied", () => {
    const { container } = render(<Panel>bare</Panel>);
    expect(container.querySelector(".ib-panel-hd")).toBeNull();
  });

  it("renders footer items with their tone class", () => {
    const { container } = render(
      <Panel
        mnemonic="VULN"
        footer={
          <>
            <PanelFootItem>exit 0</PanelFootItem>
            <PanelFootItem tone="warn">3 stale</PanelFootItem>
            <PanelFootItem tone="err">1 failed</PanelFootItem>
          </>
        }
      />,
    );
    expect(container.querySelector(".ib-panel-ft")).toBeInTheDocument();
    expect(screen.getByText("3 stale")).toHaveClass("ib-exit-warn");
    expect(screen.getByText("1 failed")).toHaveClass("ib-exit-err");
    expect(screen.getByText("exit 0").className).toBe("");
  });

  it("renders headerRight content", () => {
    render(<Panel mnemonic="VULN" headerRight={<button type="button">Refresh</button>} />);
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });
});
