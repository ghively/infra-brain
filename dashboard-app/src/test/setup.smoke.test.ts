import { describe, expect, it } from "vitest";

describe("vitest harness smoke test", () => {
  it("can run a trivial assertion", () => {
    expect(1 + 1).toBe(2);
  });
});
