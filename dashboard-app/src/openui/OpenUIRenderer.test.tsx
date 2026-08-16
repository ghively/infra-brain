import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { OpenUIRenderer } from "./OpenUIRenderer";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("OpenUIRenderer — streaming and parsing", () => {
  it("shows the generating spinner while streaming", () => {
    render(<OpenUIRenderer output="<FleetStatCard />" isStreaming={true} />);
    expect(screen.getByText("Generating…")).toBeInTheDocument();
  });

  it("renders nothing for empty output", () => {
    const { container } = render(<OpenUIRenderer output="" isStreaming={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("falls back to raw text when no tags match", () => {
    render(<OpenUIRenderer output="just some prose, no tags here" isStreaming={false} />);
    expect(screen.getByText("just some prose, no tags here")).toBeInTheDocument();
  });

  it("renders an unknown component name as a fallback prop-dump card", () => {
    render(<OpenUIRenderer output={'<TotallyMadeUp foo="bar" />'} isStreaming={false} />);
    expect(screen.getByText("TotallyMadeUp")).toBeInTheDocument();
    expect(screen.getByText(/"foo": "bar"/)).toBeInTheDocument();
  });

  it("does not drop a tag whose quoted prop value contains a literal slash", () => {
    // Regression test: TAG_RE used to exclude '/' from the whole attribute-list
    // capture, so a prop value like domain="docker/compose" made the regex
    // engine unable to ever reach the tag's real `/>` — the tag silently
    // disappeared instead of rendering.
    render(
      <OpenUIRenderer
        output='<TotallyMadeUp domain="docker/compose" limit={20} />'
        isStreaming={false}
      />,
    );
    expect(screen.getByText("TotallyMadeUp")).toBeInTheDocument();
    expect(screen.getByText(/"domain": "docker\/compose"/)).toBeInTheDocument();
    expect(screen.getByText(/"limit": 20/)).toBeInTheDocument();
  });

  it("still parses multiple sibling tags after one with a slash in a prop value", () => {
    render(
      <OpenUIRenderer
        output='<TotallyMadeUp domain="docker/compose" /><FleetStatCard title="Open Drift Events" value={18} color="red" />'
        isStreaming={false}
      />,
    );
    expect(screen.getByText("TotallyMadeUp")).toBeInTheDocument();
    expect(screen.getByText("Open Drift Events")).toBeInTheDocument();
    expect(screen.getByText("18")).toBeInTheDocument();
  });
});

describe("OpenUIRenderer — FleetStatCard (props-only, no fetch)", () => {
  it("renders the title/value/color straight from the parsed tag", () => {
    render(
      <OpenUIRenderer
        output='<FleetStatCard title="Open Drift Events" value={18} color="red" />'
        isStreaming={false}
      />,
    );
    expect(screen.getByText("Open Drift Events")).toBeInTheDocument();
    expect(screen.getByText("18")).toBeInTheDocument();
  });
});

describe("OpenUIRenderer — wired data components", () => {
  it("DriftEventTable fetches /api/dashboard/drift_events with the parsed filters and renders rows", async () => {
    const spy = vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [
        {
          id: "1",
          domain: "linux",
          hostname: "lnx-web-01",
          field_name: "kernel",
          old_value: "5.14",
          new_value: "5.15",
          detected_at: "2026-07-20T00:00:00Z",
          status: "open",
        },
      ],
    });

    render(<OpenUIRenderer output='<DriftEventTable domain="linux" status="open" hours={48} />' isStreaming={false} />);

    await waitFor(() => expect(screen.getByText("lnx-web-01")).toBeInTheDocument());
    expect(spy).toHaveBeenCalledWith(
      expect.stringMatching(/^\/api\/dashboard\/drift_events\?.*domain=linux.*/),
    );
    const calledUrl = spy.mock.calls[0][0] as string;
    expect(calledUrl).toContain("status=open");
    expect(calledUrl).toContain("hours=48");
  });

  it("VulnSummary shows a loading skeleton, then renders CVE rows", async () => {
    let resolve!: (v: unknown) => void;
    vi.spyOn(api, "apiGet").mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );

    render(<OpenUIRenderer output='<VulnSummary severity="critical" />' isStreaming={false} />);
    expect(screen.getByText("Vulnerabilities")).toBeInTheDocument();

    resolve({
      items: [{ cve: "CVE-2026-1234", host: "web-01", pkg: "openssl", severity: "critical", cvss: 9.8, status: "open" }],
    });

    await waitFor(() => expect(screen.getByText("CVE-2026-1234")).toBeInTheDocument());
  });

  it("shows an inline error card when the fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("HTTP 500"));

    render(<OpenUIRenderer output="<ResourceList />" isStreaming={false} />);

    await waitFor(() => expect(screen.getByText(/Unable to load live data/)).toBeInTheDocument());
  });

  it("shows an empty-state message when the endpoint returns no rows", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({ items: [] });

    render(<OpenUIRenderer output="<AgentActivityFeed />" isStreaming={false} />);

    await waitFor(() =>
      expect(screen.getByText("No agent activity matches these filters.")).toBeInTheDocument(),
    );
  });

  it("LinuxPackageTable resolves hostname to resource_id, then fetches the linux detail endpoint", async () => {
    const spy = vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.startsWith("/api/dashboard/resources?")) {
        return { items: [{ id: "res-1", hostname: "lnx-web-01" }] };
      }
      if (url === "/api/dashboard/resources/res-1/linux") {
        return { packages: [{ name: "openssl", version: "3.0.2", manager: "apt" }] };
      }
      throw new Error(`unexpected url ${url}`);
    });

    render(<OpenUIRenderer output='<LinuxPackageTable hostname="lnx-web-01" />' isStreaming={false} />);

    await waitFor(() => expect(screen.getByText("openssl")).toBeInTheDocument());
    expect(spy).toHaveBeenCalledWith("/api/dashboard/resources/res-1/linux");
  });

  it("LinuxServiceStatus shows a not-found empty state when the hostname has no matching resource", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({ items: [{ id: "res-9", hostname: "other-host" }] });

    render(<OpenUIRenderer output='<LinuxServiceStatus hostname="lnx-missing-01" />' isStreaming={false} />);

    await waitFor(() =>
      expect(screen.getByText('No resource found for host "lnx-missing-01".')).toBeInTheDocument(),
    );
  });

  it("requires hostname for the host-scoped Linux/Windows components", () => {
    render(<OpenUIRenderer output="<WindowsPatchState />" isStreaming={false} />);
    expect(screen.getByText(/hostname prop is required/)).toBeInTheDocument();
  });

  it("DomainHealthGrid composes collection_runs + drift_events into a per-domain grid", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.startsWith("/api/dashboard/collection_runs")) {
        return {
          items: [{ domain: "linux", trigger_type: "scheduled", status: "success", started_at: "2026-07-27T00:00:00Z", duration_seconds: 12 }],
        };
      }
      if (url.startsWith("/api/dashboard/drift_events")) {
        return { items: [{ domain: "linux" }, { domain: "linux" }] };
      }
      throw new Error(`unexpected url ${url}`);
    });

    render(<OpenUIRenderer output="<DomainHealthGrid />" isStreaming={false} />);

    await waitFor(() => expect(screen.getByText("linux")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("2 open drift")).toBeInTheDocument());
  });

});
