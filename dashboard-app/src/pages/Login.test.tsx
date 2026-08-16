import { describe, it, expect } from "vitest";
import { safeNext } from "./Login";

/** Regression coverage: after a successful login, safeNext() decides where to
 *  send the user. A next= left over from before the redirectToLogin() no-op
 *  guard (api.ts) existed can point back at /dashboard2/login (possibly with
 *  further nested next= junk) — following it lands the user back on the
 *  login form with no error, which reads as "login did nothing". */
describe("safeNext", () => {
  it("returns a normal same-origin path unchanged", () => {
    expect(safeNext("/hosts")).toBe("/hosts");
  });

  it("falls back to the dashboard root for a protocol-relative target (open-redirect guard)", () => {
    expect(safeNext("//evil.com/phish")).toBe("/dashboard2/");
  });

  it("falls back to the dashboard root for a non-absolute target", () => {
    expect(safeNext("evil.com")).toBe("/dashboard2/");
  });

  it("falls back to the dashboard root when next= points back to /dashboard2/login", () => {
    expect(safeNext("/dashboard2/login")).toBe("/dashboard2/");
  });

  it("falls back to the dashboard root for a stale nested next= pointing back to login", () => {
    const stale =
      "/dashboard2/login?next=" + encodeURIComponent("/dashboard2/login?next=%2Fdashboard2%2Flogin");
    expect(safeNext(stale)).toBe("/dashboard2/");
  });

  it("falls back to the dashboard root when no next= and no usable referrer", () => {
    expect(safeNext(null)).toBe("/dashboard2/");
  });
});
