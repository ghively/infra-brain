import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { RefreshProvider } from "../hooks/useAutoRefresh";
import { Sidebar } from "./Sidebar";

let originalLocation: Location;

beforeEach(() => {
  // window.location.assign is non-configurable in jsdom by default — swap
  // the whole `location` object out, same pattern as api.test.ts's
  // redirectToLogin coverage.
  originalLocation = window.location;
  // jsdom doesn't implement scrollIntoView; the embedded SidebarChat panel
  // calls it on mount/update, so it must be stubbed for Sidebar to render.
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) never fires — see Home.test.tsx for the
  // same pattern.
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  Object.defineProperty(window, "location", {
    value: originalLocation,
    writable: true,
    configurable: true,
  });
});

function mockLocationAssign(): ReturnType<typeof vi.fn> {
  const assignSpy = vi.fn();
  Object.defineProperty(window, "location", {
    value: { ...originalLocation, assign: assignSpy },
    writable: true,
    configurable: true,
  });
  return assignSpy;
}

/** Minimal happy-path stub for the identity/version/counts calls Sidebar fires
 *  on mount, so the sign-out button renders without unrelated errors. */
function mockIdentity() {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/counts")) return {};
    if (url.includes("/sources")) return { items: [] };
    if (url.includes("/me")) return { authenticated: true, dev_mode: false, username: "operator", name: "the maintainer", role: "admin" };
    if (url.includes("/version")) return { version: "1.0.0", environment: "dev" };
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

/** Stub the chrome fetches and serve a specific source inventory, so a test can
 *  say "with these collectors retired, the nav should look like this". */
function mockSources(items: { domain: string; visibility: string }[]) {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/sources")) return { items };
    if (url.includes("/counts")) return {};
    if (url.includes("/me")) return { authenticated: true, dev_mode: false, username: "operator", name: "the maintainer", role: "admin" };
    if (url.includes("/version")) return { version: "1.0.0", environment: "dev" };
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function renderSidebar() {
  return render(
    <MemoryRouter>
      <Sidebar />
    </MemoryRouter>,
  );
}

describe("Sidebar sign-out", () => {
  it("POSTs /api/dashboard/logout and navigates to the login page on success", async () => {
    mockIdentity();
    const postSpy = vi.spyOn(api, "apiPost").mockResolvedValue({ authenticated: false });
    const assignSpy = mockLocationAssign();

    renderSidebar();
    const btn = await screen.findByRole("button", { name: "Sign out" });
    fireEvent.click(btn);

    await waitFor(() => expect(postSpy).toHaveBeenCalledWith("/api/dashboard/logout"));
    await waitFor(() => expect(assignSpy).toHaveBeenCalledWith("/dashboard2/login"));
  });

  it("still navigates to the login page when the logout request fails", async () => {
    mockIdentity();
    const postSpy = vi.spyOn(api, "apiPost").mockRejectedValue(new Error("network unreachable"));
    const assignSpy = mockLocationAssign();

    renderSidebar();
    const btn = await screen.findByRole("button", { name: "Sign out" });
    fireEvent.click(btn);

    await waitFor(() => expect(postSpy).toHaveBeenCalledWith("/api/dashboard/logout"));
    await waitFor(() => expect(assignSpy).toHaveBeenCalledWith("/dashboard2/login"));
  });
});

describe("Sidebar counts — quiet background auto-refresh (F-7)", () => {
  // F-7: sidebar counts fetched once on mount and never refreshed, so a nav
  // badge count goes stale for the rest of the session. Sidebar persists for
  // the whole session (unlike a page component that remounts on navigation),
  // so this is the same staleness bug as the R7Tabs fix but for chrome.
  it("re-fetches counts on the 5th 60s tick via useAutoRefresh, not before", async () => {
    const apiGet = vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/counts")) return {};
      if (url.includes("/me")) return { authenticated: true, dev_mode: false, username: "operator", name: "the maintainer", role: "admin" };
      if (url.includes("/version")) return { version: "1.0.0", environment: "dev" };
      throw new Error(`unexpected apiGet: ${url}`);
    });
    vi.useFakeTimers();

    render(
      <RefreshProvider>
        <MemoryRouter>
          <Sidebar />
        </MemoryRouter>
      </RefreshProvider>,
    );

    await vi.waitFor(() =>
      expect(apiGet.mock.calls.filter(([u]) => (u as string).includes("/counts")).length).toBe(1),
    );

    // 4 ticks (4 minutes) is not enough.
    await vi.advanceTimersByTimeAsync(4 * 60_000);
    expect(apiGet.mock.calls.filter(([u]) => (u as string).includes("/counts")).length).toBe(1);

    // The 5th tick (5 minutes total) must trigger a background re-fetch of
    // counts. Before the fix, Sidebar's counts effect never re-ran, so this
    // second call would never happen and nav badges would go stale forever.
    await vi.advanceTimersByTimeAsync(60_000);
    await vi.waitFor(() =>
      expect(apiGet.mock.calls.filter(([u]) => (u as string).includes("/counts")).length).toBe(2),
    );
  });
});

describe("Sidebar nav icons (TRK-238 item 1)", () => {
  it("renders an icon svg for every nav item, including Host Purpose Map", async () => {
    mockIdentity();

    renderSidebar();
    const link = await screen.findByRole("link", { name: /Host Purpose Map/ });

    // Host Purpose Map was previously the one nav item with no NAV_ICON_NAMES
    // entry (NavIcon returned null, so the link rendered with no icon at
    // all). It's now mapped to the new "tag" glyph — assert an <svg> is
    // actually present inside the link, not just that the label renders.
    const icon = link.querySelector("svg");
    expect(icon).not.toBeNull();
  });
});

describe("Sidebar navigation is derived from GET /api/dashboard/sources", () => {
  // The sidebar used to be a hardcoded page list, which is why a homelab
  // dashboard was still advertising Rapid7, vSphere and Cloud integrations that
  // will never be configured here. It is now built from the backend's own
  // retirement state — see nav.ts and api/source_visibility.py.

  it("renders the six-group homelab layout", async () => {
    mockSources([]);
    renderSidebar();

    for (const heading of [
      "Overview",
      "Infrastructure",
      "Security & Compliance",
      "Operations",
      "Knowledge & Agents",
      "System",
    ]) {
      expect(await screen.findByText(heading)).toBeInTheDocument();
    }
  });

  it("drops a nav entry whose collector the endpoint reports as hidden", async () => {
    mockSources([{ domain: "compliance", visibility: "hidden" }]);
    renderSidebar();

    // A sibling entry proves the nav rendered at all.
    expect(await screen.findByText("Drift Events")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Compliance")).toBeNull());
  });

  it("keeps a retired-but-populated source reachable, marked historical", async () => {
    // The `identity` case: retired collector, 53 real rows. Hiding it would
    // make the UI assert that data which exists does not.
    mockSources([{ domain: "compliance", visibility: "historical" }]);
    renderSidebar();

    const label = await screen.findByText("Compliance");
    const anchor = label.closest("a") as HTMLElement;
    expect(anchor).toHaveAttribute("href", "/compl");
    expect(anchor.getAttribute("title")).toMatch(/historical/i);
  });

  it("shows the whole nav when the sources fetch fails, never a blank sidebar", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/sources")) throw new Error("500");
      if (url.includes("/counts")) return {};
      if (url.includes("/me")) {
        return { authenticated: true, dev_mode: false, username: "operator", name: "the maintainer", role: "admin" };
      }
      if (url.includes("/version")) return { version: "1.0.0", environment: "dev" };
      throw new Error(`unexpected apiGet: ${url}`);
    });
    renderSidebar();

    expect(await screen.findByText("Compliance")).toBeInTheDocument();
    expect(await screen.findByText("Drift Events")).toBeInTheDocument();
  });

  it("offers no Rapid7, vSphere or Cloud entry under any inventory", async () => {
    mockSources([]);
    renderSidebar();

    await screen.findByText("Hosts");
    for (const gone of [/rapid7/i, /vsphere/i, /^cloud/i]) {
      expect(screen.queryByText(gone)).toBeNull();
    }
  });

  it("keeps Software Inventory even with zero live sources — a missing collector is not a retired source", async () => {
    // Owner decision 2026-08-13: software inventory is wanted here and will get
    // a collector; it carries no `domains` filter for exactly that reason, so
    // it must survive an empty /sources payload rather than vanishing.
    mockSources([]);
    renderSidebar();

    expect(await screen.findByText("Software Inventory")).toBeInTheDocument();
  });
});
