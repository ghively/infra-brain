import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Tabs } from "./Tabs";

afterEach(() => {
  cleanup();
});

const TABS = [
  { key: "sites", label: "Sites" },
  { key: "tags", label: "Tags" },
  { key: "assets", label: "Assets" },
];

describe("Tabs", () => {
  it("renders a tablist with proper roles and aria-selected", () => {
    render(<Tabs tabs={TABS} active="sites" onChange={vi.fn()} />);
    expect(screen.getByRole("tablist")).toBeInTheDocument();
    const tabEls = screen.getAllByRole("tab");
    expect(tabEls).toHaveLength(3);
    expect(screen.getByRole("tab", { name: "Sites" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Tags" })).toHaveAttribute("aria-selected", "false");
  });

  it("calls onChange when a tab is clicked", () => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} active="sites" onChange={onChange} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tags" }));
    expect(onChange).toHaveBeenCalledWith("tags");
  });

  it("moves selection right with ArrowRight", () => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} active="sites" onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("tab", { name: "Sites" }), { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith("tags");
  });

  it("wraps around with ArrowLeft from the first tab", () => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} active="sites" onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("tab", { name: "Sites" }), { key: "ArrowLeft" });
    expect(onChange).toHaveBeenCalledWith("assets");
  });

  it("jumps to the last tab with End and first with Home", () => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} active="tags" onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("tab", { name: "Tags" }), { key: "End" });
    expect(onChange).toHaveBeenCalledWith("assets");
    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole("tab", { name: "Tags" }), { key: "Home" });
    expect(onChange).toHaveBeenCalledWith("sites");
  });

  it("skips disabled tabs during keyboard navigation", () => {
    const onChange = vi.fn();
    const tabsWithDisabled = [
      { key: "sites", label: "Sites" },
      { key: "tags", label: "Tags", disabled: true },
      { key: "assets", label: "Assets" },
    ];
    render(<Tabs tabs={tabsWithDisabled} active="sites" onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("tab", { name: "Sites" }), { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith("assets");
  });
});
