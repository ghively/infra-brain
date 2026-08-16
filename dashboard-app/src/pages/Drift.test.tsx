import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Drift } from "./Drift";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration (which detects a global
  // `afterEach`) never fires — without this, renders from earlier tests in
  // this file accumulate in the jsdom document and cause "Found multiple
  // elements" failures (e.g. two "Open"/"web01" matches across tests).
  cleanup();
  vi.restoreAllMocks();
});

function renderDrift() {
  return render(
    <MemoryRouter>
      <Drift />
    </MemoryRouter>,
  );
}

describe("Drift page error handling (#91)", () => {
  it("renders the events table on a successful load", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/domains")) return [];
      return {
        items: [
          {
            id: "1",
            domain: "linux",
            hostname: "web01",
            field_name: "os_version",
            old_value: "22.04",
            new_value: "24.04",
            detected_at: "2026-07-23T00:00:00Z",
            status: "open",
            jira_ticket: "",
            drift_type: "state_drift",
          },
        ],
        total: 1,
      };
    });

    renderDrift();
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());
  });

  it("does NOT unmount the page on a fetch error — filters and header stay visible", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/domains")) return [];
      throw new Error("network unreachable");
    });

    renderDrift();

    // The old behavior: `if (error) return <div role="alert">Drift failed to
    // load: {error}</div>` — which means NOTHING else renders, including the
    // status-filter tabs. The new behavior must keep the filter tabs mounted.
    await waitFor(() => expect(screen.getByText("Open")).toBeInTheDocument());
    expect(screen.getByText("Acknowledged")).toBeInTheDocument();
    expect(screen.getByText("All time")).toBeInTheDocument();
  });

  it("shows a non-blocking inline notice on error instead of the bare role=alert div", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/domains")) return [];
      throw new Error("network unreachable");
    });

    renderDrift();
    // Phase 2 rebuild: the main-table error banner is a `Panel` whose
    // description reads "Could not refresh — showing last known results.";
    // "showing last known results" is unique to it (unlike "failed to load",
    // which the independent TrendBarChart/DriftTapeInstrument widget sidecar
    // also renders on its own fetch failure — both are legitimately separate,
    // non-blocking notices, matching this test's own intent that a fetch
    // error must not blank the page).
    await waitFor(() => expect(screen.getByText(/showing last known results/i)).toBeInTheDocument());
  });
});

describe("Drift page 'open events — fleet-wide' widget (TRK-238 item 3)", () => {
  function mockZeroOpenDrift() {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/domains")) return [];
      if (url.includes("/api/dashboard/counts")) return { open_drift: 0 };
      if (url.includes("/drift_events/trend")) return { points: [], total: 0, domain_filter: "", days: 30 };
      if (url.includes("/drift_events")) return { items: [], total: 0 };
      throw new Error(`unexpected apiGet: ${url}`);
    });
  }

  it("shows an EmptyState instead of the tape gauge when there are zero open events", async () => {
    mockZeroOpenDrift();

    renderDrift();

    // Previously a DriftTapeInstrument gauge (a narrow 74x236 vertical tape
    // with tick labels) rendered even at zero, reading as a clipped/squished
    // chart with nothing meaningful to show — this must now be a real
    // EmptyState instead.
    expect(await screen.findByText("No open drift events")).toBeInTheDocument();
    expect(screen.getByText(/nothing is currently drifting/i)).toBeInTheDocument();
  });

  it("still renders the tape gauge when there are open events", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/domains")) return [];
      if (url.includes("/api/dashboard/counts")) return { open_drift: 5 };
      if (url.includes("/drift_events/trend")) return { points: [], total: 0, domain_filter: "", days: 30 };
      if (url.includes("/drift_events")) return { items: [], total: 0 };
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderDrift();

    await waitFor(() => expect(screen.getByRole("img", { name: /Open drift events/ })).toBeInTheDocument());
    expect(screen.queryByText("No open drift events")).not.toBeInTheDocument();
  });
});

describe("Drift page 'open events — fleet-wide' gauge headroom ceiling (audit finding)", () => {
  // Regression test for the "headroom ceiling" bug: `ceiling` used to be
  // `Math.max(currentValue * 1.5, 10)`, a pure multiple of `currentValue`
  // itself. That pins `currentValue / ceiling` to the constant ~0.667 for
  // any currentValue above the floor, which is *past* the 0.66 red-zone
  // threshold — so the gauge rendered "critical" no matter how the current
  // reading compared to its own recent history, defeating the documented
  // green/yellow/red scheme. Here the current reading (40) sits well below
  // a recent 14-day peak (60), so a correctly graduated gauge must NOT
  // render it as critical/red.
  it("does not render the gauge as critical when the current reading is well below its recent peak", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/domains")) return [];
      if (url.includes("/api/dashboard/counts")) return { open_drift: 40 };
      if (url.includes("/drift_events/trend")) {
        return {
          points: [{ date: "2026-07-20", count: 60, domain: "linux" }],
          total: 60,
          domain_filter: "",
          days: 14,
        };
      }
      if (url.includes("/drift_events")) return { items: [], total: 0 };
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderDrift();

    const gauge = await screen.findByRole("img", { name: /Open drift events/ });
    // Old buggy formula: ceiling = max(40 * 1.5, 10) = 60 -> 40/60 ≈ 0.667 ->
    // "critical" (red), regardless of the 60-count history peak above it.
    // Fixed formula anchors the ceiling to that history peak (60 * 1.5 =
    // 90), so 40/90 ≈ 0.444 lands in the yellow "warning" band instead.
    expect(gauge.getAttribute("aria-label")).not.toMatch(/critical/);
    expect(gauge.getAttribute("aria-label")).toMatch(/warning/);

    const panel = gauge.closest("[data-rail]");
    expect(panel).not.toBeNull();
    expect(panel?.getAttribute("data-rail")).not.toBe("err");
  });
});

describe("Drift page 'open events — fleet-wide' widget trend (flow vs. stock)", () => {
  it("does not fabricate a rising/falling trend by comparing the daily-detected series to the open-count snapshot", async () => {
    // Regression test: `history` used to be the daily "drift events detected"
    // series (a flow metric from /drift_events/trend) with `counts.open_drift`
    // (a stock metric) spliced onto its end, then fed to DriftTapeInstrument
    // to derive a "rising"/"falling" trend word. Those two series measure
    // different things, so the comparison was meaningless. Here the most
    // recent daily-detected count (5) is far below the open-count snapshot
    // (20) — under the old buggy splice this would render "rising from 5" in
    // the instrument's aria-label. The fix must render no trend claim at all.
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/domains")) return [];
      if (url.includes("/api/dashboard/counts")) return { open_drift: 20 };
      if (url.includes("/drift_events/trend")) {
        return {
          points: [
            { date: "2026-07-20", count: 100, domain: "linux" },
            { date: "2026-07-21", count: 5, domain: "linux" },
          ],
          total: 105,
          domain_filter: "",
          days: 14,
        };
      }
      if (url.includes("/drift_events")) return { items: [], total: 0 };
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderDrift();

    const tape = await screen.findByRole("img", { name: /Open drift events: 20/ });
    expect(tape.getAttribute("aria-label")).not.toMatch(/rising|falling/i);
    expect(screen.queryByTestId("trend")).not.toBeInTheDocument();
  });
});

describe("Drift page pagination (#73)", () => {
  it("includes limit/offset in the query and shows a count label, no Prev on page 1", async () => {
    const seen: string[] = [];
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      seen.push(url);
      if (url.includes("/domains")) return [];
      return { items: [{ id: "1", domain: "linux", hostname: "web01", field_name: "f", old_value: "a", new_value: "b", detected_at: "t", status: "open", jira_ticket: "", drift_type: "state_drift" }], total: 250 };
    });

    renderDrift();
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());

    const driftCall = seen.find((u) => u.includes("/drift_events?"));
    expect(driftCall).toContain("limit=100");
    expect(driftCall).toContain("offset=0");
    // Phase 2 rebuild: the pagination label now lives inside DataTable's
    // status-bar `note` slot alongside a "· read-only" suffix, so it's a
    // separate text node from a plain string match — match by function
    // instead of an exact string.
    expect(screen.getByText("1–1 of 250 · read-only")).toBeInTheDocument();
    expect(screen.queryByText("Prev")).not.toBeInTheDocument();
    expect(screen.getByText("Next")).toBeInTheDocument();
  });

  it("Next advances offset by PAGE_SIZE and refetches; Prev goes back", async () => {
    const seen: string[] = [];
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      seen.push(url);
      if (url.includes("/domains")) return [];
      return { items: [{ id: "1", domain: "linux", hostname: "web01", field_name: "f", old_value: "a", new_value: "b", detected_at: "t", status: "open", jira_ticket: "", drift_type: "state_drift" }], total: 250 };
    });

    renderDrift();
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());

    screen.getByText("Next").click();

    await waitFor(() =>
      expect(seen.find((u) => u.includes("/drift_events?") && u.includes("offset=100"))).toBeTruthy(),
    );
    expect(screen.getByText("101–101 of 250 · read-only")).toBeInTheDocument();
    expect(screen.getByText("Prev")).toBeInTheDocument();

    screen.getByText("Prev").click();

    await waitFor(() =>
      expect(
        seen.filter((u) => u.includes("/drift_events?") && u.includes("offset=0")).length,
      ).toBeGreaterThan(1),
    );
  });

  it("resets offset to 0 when a filter (status) changes", async () => {
    const seen: string[] = [];
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      seen.push(url);
      if (url.includes("/domains")) return [];
      return { items: [{ id: "1", domain: "linux", hostname: "web01", field_name: "f", old_value: "a", new_value: "b", detected_at: "t", status: "open", jira_ticket: "", drift_type: "state_drift" }], total: 250 };
    });

    renderDrift();
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());

    screen.getByText("Next").click();
    await waitFor(() =>
      expect(seen.find((u) => u.includes("/drift_events?") && u.includes("offset=100"))).toBeTruthy(),
    );

    const callsBeforeClick = seen.filter((u) => u.includes("/drift_events?")).length;
    // "Resolved" is unambiguous (unlike "All", which renders twice — once as
    // the status "(all)" tab, once as the default domain "(all)" tab).
    screen.getByText("Resolved").click();

    await waitFor(() =>
      expect(seen.filter((u) => u.includes("/drift_events?")).length).toBeGreaterThan(callsBeforeClick),
    );
    const lastCall = seen.filter((u) => u.includes("/drift_events?")).at(-1);
    expect(lastCall).toContain("status=resolved");
    expect(lastCall).toContain("offset=0");
  });
});

// ---------------------------------------------------------------------------
// Drift readability. Maintainer, verbatim: "I don't understand what drifted or
// why they are flagged as drift." The page used to render drift_type / field /
// old_value / new_value raw, so a row read
// `state_drift | listen_ports | {'v': [...]} | {'v': [...]}`.
// ---------------------------------------------------------------------------

function mockDrift(overrides: Record<string, unknown> = {}) {
  return vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/domains")) return [];
    if (url.includes("/trend")) return { points: [], total: 0, domain_filter: "", days: 14 };
    if (url.includes("/counts")) return { open_drift: 1 };
    return {
      items: [
        {
          id: "1",
          domain: "linux",
          hostname: "node_a",
          field_name: "port",
          old_value: "",
          new_value: "{'port': 8443}",
          detected_at: "2026-08-07T00:00:00Z",
          status: "open",
          jira_ticket: "",
          drift_type: "new_listening_port",
          summary: "node_a started listening on port 8443 (was not listening)",
          rule: "Port drift — the Linux collector saw this TCP/UDP port listening in the current scan of this host, and it was not listening in the previous scan.",
          old_display: "",
          new_display: '{"port": 8443}',
          ...overrides,
        },
      ],
      total: 1,
    };
  });
}

describe("Drift readability — leading with the sentence", () => {
  it("renders the server-computed summary as the row's primary cell", async () => {
    mockDrift();
    renderDrift();
    await waitFor(() =>
      expect(
        screen.getByText("node_a started listening on port 8443 (was not listening)"),
      ).toBeInTheDocument(),
    );
  });

  it("does not render raw JSONB envelopes in the table body", async () => {
    mockDrift();
    renderDrift();
    await waitFor(() => expect(screen.getByText("node_a")).toBeInTheDocument());
    // The old "Change" column put the stringified payload straight in the row.
    expect(screen.queryByText("{'port': 8443}")).not.toBeInTheDocument();
    expect(screen.queryByText("Change")).not.toBeInTheDocument();
  });

  it("falls back to a raw-value sentence when the backend omits `summary`", async () => {
    // A stale bundle/backend pairing must degrade, not render a blank cell.
    mockDrift({ summary: undefined, field_name: "kernel", old_value: "5.15", new_value: "6.8" });
    renderDrift();
    await waitFor(() =>
      expect(screen.getByText("kernel changed from 5.15 to 6.8 on node_a")).toBeInTheDocument(),
    );
  });
});

describe("Drift readability — honest drift_type badge", () => {
  it("labels a non-config type as itself, not as 'config'", async () => {
    // The pre-fix badge was `drift_type === "state_drift" ? "state" : "config"`,
    // which mislabelled all 9 other real writer types as "config".
    mockDrift();
    renderDrift();
    await waitFor(() => expect(screen.getByText("new port")).toBeInTheDocument());
    expect(screen.queryByText("config")).not.toBeInTheDocument();
  });

  it.each([
    ["potential_secret_in_iac", "possible secret"],
    ["shadow_it_discovered", "shadow IT"],
    ["threat_escalation", "threat ↑"],
    ["service_stopped", "service stopped"],
    ["identity_conflict_distinct_object", "identity (object)"],
    ["config_drift", "config"],
    ["state_drift", "state (legacy)"],
  ])("renders a distinct badge for %s", async (drift_type, label) => {
    mockDrift({ drift_type });
    renderDrift();
    await waitFor(() => expect(screen.getByText(label)).toBeInTheDocument());
  });

  it("renders an unknown drift_type verbatim rather than mislabelling it", async () => {
    // mcp_server.py::seed_drift_event accepts any caller-chosen drift_type.
    mockDrift({ drift_type: "some_future_type" });
    renderDrift();
    await waitFor(() => expect(screen.getByText("some_future_type")).toBeInTheDocument());
    expect(screen.queryByText("config")).not.toBeInTheDocument();
  });
});

describe("Drift readability — the drawer answers 'why is this flagged'", () => {
  it("shows the rule explanation when a row is opened", async () => {
    mockDrift();
    renderDrift();
    await waitFor(() => expect(screen.getByText("node_a")).toBeInTheDocument());

    screen.getByText("node_a").click();

    await waitFor(() => expect(screen.getByText("Why is this flagged?")).toBeInTheDocument());
    expect(screen.getByText(/the Linux collector saw this TCP\/UDP port listening/)).toBeInTheDocument();
  });

  it("renders a before/after diff, labelled as an addition when there is no previous value", async () => {
    mockDrift();
    renderDrift();
    await waitFor(() => expect(screen.getByText("node_a")).toBeInTheDocument());
    screen.getByText("node_a").click();

    await waitFor(() => expect(screen.getByText("Added — no previous value")).toBeInTheDocument());
    expect(screen.getByText("after")).toBeInTheDocument();
    // ...and no misleading empty "before" row implying a value was erased.
    expect(screen.queryByText("before")).not.toBeInTheDocument();
  });

  it("renders both sides for a two-sided change, using the envelope-peeled values", async () => {
    mockDrift({
      drift_type: "config_drift",
      field_name: "agent_name",
      summary: "agent_name changed from vmi-example-deploy to node_b on node_a",
      old_value: "{'v': 'vmi-example-deploy'}",
      new_value: "{'v': 'node_b'}",
      old_display: "vmi-example-deploy",
      new_display: "node_b",
    });
    renderDrift();
    await waitFor(() => expect(screen.getByText("node_a")).toBeInTheDocument());
    screen.getByText("node_a").click();

    await waitFor(() => expect(screen.getByText("Before → after")).toBeInTheDocument());
    // The peeled values, not the {'v': ...} envelopes.
    expect(screen.getByText("vmi-example-deploy")).toBeInTheDocument();
    expect(screen.getByText("node_b")).toBeInTheDocument();
    expect(screen.queryByText("{'v': 'vmi-example-deploy'}")).not.toBeInTheDocument();
    expect(screen.queryByText("{'v': 'node_b'}")).not.toBeInTheDocument();
  });

  it("says so plainly when an event recorded no values at all", async () => {
    mockDrift({
      drift_type: "state_drift",
      summary: "node_a was absent from its domain's last collection run",
      old_value: "",
      new_value: "",
      old_display: "",
      new_display: "",
    });
    renderDrift();
    await waitFor(() => expect(screen.getByText("node_a")).toBeInTheDocument());
    screen.getByText("node_a").click();

    await waitFor(() =>
      expect(screen.getByText("This event recorded no before/after values.")).toBeInTheDocument(),
    );
  });
});
