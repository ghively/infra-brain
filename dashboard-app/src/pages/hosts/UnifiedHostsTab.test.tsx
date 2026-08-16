/** Tests for `UnifiedHostsTab`'s host-vulns drawer panel — in particular the
 *  VULN panel's rail-tone computation (`worst`), which must reflect the
 *  worst *present* severity band including "moderate" (medium), not just
 *  critical/severe/low. Regression coverage for the bug where a host with
 *  only moderate-severity open CVEs rendered the panel rail as "ok" (green)
 *  instead of "warn" (amber), even though the Moderate `SeverityPill` a few
 *  lines below showed a nonzero count.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../../api";
import { UnifiedHostsTab } from "./UnifiedHostsTab";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) never registers — without this, renders from
  // earlier tests accumulate in the jsdom document.
  cleanup();
  vi.restoreAllMocks();
});

const HOST = {
  short_hostname: "web01",
  fqdn: "web01.corp.local",
  ip_addresses: ["10.0.0.5"],
  os_family: "linux",
  risk_score: 1200,
  vuln_count: 5,
  patch_status: "compliant",
  vsphere_power_state: "poweredOn",
  octopus_machine_status: "Online",
  r7_resource_id: "r7-1",
  vsphere_resource_id: null,
  octopus_resource_id: null,
  // Both null so HostPostureDetail/HostOsDetail render null and issue no
  // fetches of their own — keeps this test scoped to the VULN panel.
  linux_resource_id: null,
  windows_resource_id: null,
  last_reconciled: "2026-07-27T00:00:00Z",
};

const HOSTS_PAGE = { items: [HOST], total: 1, limit: 100, offset: 0 };

function renderTab() {
  return render(
    <MemoryRouter initialEntries={["/hosts"]}>
      <UnifiedHostsTab />
    </MemoryRouter>,
  );
}

/** Mocks the host list, the purpose sub-fetch (always issued by the drawer),
 *  and the vulns sub-fetch with the given header counts. */
function mockDrawer(header: { vuln_critical?: number; vuln_severe?: number; vuln_moderate?: number }) {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/api/dashboard/hosts/web01/vulns")) {
      return {
        header: {
          risk_score: 100,
          vuln_critical: header.vuln_critical ?? 0,
          vuln_severe: header.vuln_severe ?? 0,
          vuln_moderate: header.vuln_moderate ?? 0,
        },
        items: [],
        total: (header.vuln_critical ?? 0) + (header.vuln_severe ?? 0) + (header.vuln_moderate ?? 0),
      };
    }
    if (url.includes("/purpose")) {
      return { purpose: null, vlan: null, subnet: null };
    }
    if (url.includes("/api/dashboard/hosts")) return HOSTS_PAGE;
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

async function openDrawer() {
  renderTab();
  await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());
  fireEvent.click(screen.getByText("web01").closest("tr") as HTMLElement);
  await waitFor(() => expect(screen.getByRole("region", { name: "VULN" })).toBeInTheDocument());
}

describe("UnifiedHostsTab — host-vulns drawer VULN panel rail", () => {
  it("renders the 'warn' rail when the host has only moderate-severity open CVEs", async () => {
    mockDrawer({ vuln_moderate: 5 });
    await openDrawer();

    await waitFor(() => expect(screen.getByText("Moderate 5")).toBeInTheDocument());
    const panel = screen.getByRole("region", { name: "VULN" });
    expect(panel).toHaveAttribute("data-rail", "warn");
  });

  it("renders the 'err' rail when the host has critical CVEs", async () => {
    mockDrawer({ vuln_critical: 2, vuln_moderate: 5 });
    await openDrawer();

    await waitFor(() => expect(screen.getByText("Critical 2")).toBeInTheDocument());
    const panel = screen.getByRole("region", { name: "VULN" });
    expect(panel).toHaveAttribute("data-rail", "err");
  });

  it("renders the 'ok' rail when the host has no open CVEs at all", async () => {
    mockDrawer({});
    await openDrawer();

    await waitFor(() => expect(screen.getByText("Moderate 0")).toBeInTheDocument());
    const panel = screen.getByRole("region", { name: "VULN" });
    expect(panel).toHaveAttribute("data-rail", "ok");
  });
});
