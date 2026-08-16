import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Button } from "./Button";

afterEach(() => {
  cleanup();
});

describe("Button", () => {
  it("renders children and fires onClick", () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Save</Button>);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("applies the variant and size classes", () => {
    render(
      <Button variant="danger" size="sm">
        Delete
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "Delete" });
    expect(btn.className).toContain("ui-btn-danger");
    expect(btn.className).toContain("ui-btn-sm");
  });

  it("defaults to secondary/md variant", () => {
    render(<Button>Default</Button>);
    const btn = screen.getByRole("button", { name: "Default" });
    expect(btn.className).toContain("ui-btn-secondary");
    expect(btn.className).toContain("ui-btn-md");
  });

  it("is disabled and does not fire onClick when disabled", () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Nope
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "Nope" });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("shows a spinner and disables the button while loading", () => {
    render(<Button loading>Submitting</Button>);
    const btn = screen.getByRole("button", { name: /Submitting/ });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
  });

  it("has a visible focus ring style hook via CSS class (not inline style)", () => {
    render(<Button>Focusable</Button>);
    const btn = screen.getByRole("button", { name: "Focusable" });
    expect(btn.className).toContain("ui-btn");
  });
});
