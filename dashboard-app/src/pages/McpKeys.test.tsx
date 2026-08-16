import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { McpKeys, mcpClientConfig, parseExpiresDays } from "./McpKeys";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const CATALOG = { readonly: ["read_hosts"], mutation: ["create_key"] };

const ONE_KEY = [
  {
    id: "k1",
    name: "infra-ops-prod-subnet-a",
    created_at: "2026-07-24T00:00:00Z",
    created_by: "operator",
    last_used_at: null,
    revoked: false,
    allowed_tools_count: 1,
  },
];

function mockLoadSuccess() {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/mcp-keys/tools")) return CATALOG;
    if (url.includes("/mcp-keys")) return ONE_KEY;
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function renderMcpKeys() {
  return render(
    <MemoryRouter>
      <McpKeys />
    </MemoryRouter>,
  );
}

describe("McpKeys page error handling", () => {
  it("shows the full-page error and hides everything else on a load failure", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/mcp-keys/tools")) throw new Error("network unreachable");
      return ONE_KEY;
    });

    renderMcpKeys();

    await waitFor(() =>
      expect(screen.getByText(/MCP keys failed to load/)).toBeInTheDocument(),
    );
    expect(screen.queryByPlaceholderText(/Key name/)).not.toBeInTheDocument();
    expect(screen.queryByText("infra-ops-prod-subnet-a")).not.toBeInTheDocument();
  });

  it("renders the page shell (not a full-page error) when the key list 403s for a non-admin", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/mcp-keys/tools")) return CATALOG;
      if (url.includes("/mcp-keys")) throw new api.ApiError(403, url);
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderMcpKeys();

    // The connection-instructions panel needs no admin privilege — it must
    // still render, and the page must not blank to a full-page error string.
    await waitFor(() => expect(screen.getByText(/Connecting a client/)).toBeInTheDocument());
    expect(screen.queryByText(/MCP keys failed to load/)).not.toBeInTheDocument();

    // The create form (admin-only) and the key table (admin-only data) must
    // both be gated off rather than rendered broken/empty.
    expect(screen.queryByPlaceholderText(/Key name/)).not.toBeInTheDocument();
    expect(screen.getByText(/admin access/i)).toBeInTheDocument();
    expect(screen.getByText(/only visible to admins/i)).toBeInTheDocument();
  });

  it("a failed revoke shows an inline error without hiding the key list or create form", async () => {
    mockLoadSuccess();
    vi.spyOn(api, "apiPost").mockRejectedValue(new Error("403 Forbidden"));

    renderMcpKeys();

    await waitFor(() => expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument());

    screen.getByText("Revoke").click();

    await waitFor(() => expect(screen.getByText(/403 Forbidden/)).toBeInTheDocument());

    // The list and create form must still be present — a mutation failure
    // must not blank out an already-successfully-loaded page.
    expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/Key name/)).toBeInTheDocument();
    expect(screen.queryByText(/MCP keys failed to load/)).not.toBeInTheDocument();
  });

  it("clears a prior mutation error once a subsequent revoke succeeds", async () => {
    mockLoadSuccess();
    const apiPost = vi.spyOn(api, "apiPost");
    apiPost.mockRejectedValueOnce(new Error("network blip"));

    renderMcpKeys();
    await waitFor(() => expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument());

    screen.getByText("Revoke").click();
    await waitFor(() => expect(screen.getByText(/network blip/)).toBeInTheDocument());

    apiPost.mockResolvedValueOnce(undefined);
    screen.getByText("Revoke").click();

    await waitFor(() => expect(screen.queryByText(/network blip/)).not.toBeInTheDocument());
    // Page must still be intact.
    expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument();
  });

  it("does not surface a stale mutation error banner on a normal successful load", async () => {
    mockLoadSuccess();

    renderMcpKeys();
    await waitFor(() => expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument());

    expect(screen.queryByText(/MCP keys failed to load/)).not.toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getByText("infra-ops-prod-subnet-a")).toBeInTheDocument();
  });

  it("renders the Status column as a tone-classed Badge (ok for active, err for revoked) — Phase 3 design-system rebuild", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/mcp-keys/tools")) return CATALOG;
      if (url.includes("/mcp-keys")) {
        return [
          ONE_KEY[0],
          { ...ONE_KEY[0], id: "k2", name: "revoked-key", revoked: true },
        ];
      }
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderMcpKeys();
    await waitFor(() => expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument());

    const activeBadge = screen.getByText("active");
    const revokedBadge = screen.getByText("revoked");
    expect(activeBadge.className).toContain("ib-sev-ok");
    expect(revokedBadge.className).toContain("ib-sev-high");
  });

  it("shows a persistent client-config reference panel even before any key is created", async () => {
    mockLoadSuccess();

    renderMcpKeys();
    await waitFor(() => expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument());

    expect(screen.getByText(/Connecting a client/)).toBeInTheDocument();
    expect(screen.getByText(/<your-token>/)).toBeInTheDocument();
    expect(screen.getAllByText(/streamable-http/).length).toBeGreaterThan(0);
  });

  it("shows a ready-to-paste client config with the real token right after creating a key", async () => {
    mockLoadSuccess();
    vi.spyOn(api, "apiPost").mockResolvedValue({
      id: "k2",
      name: "new-key",
      token: "ibmcp_the-real-raw-token",
      allowed_tools: ["read_hosts"],
    });

    renderMcpKeys();
    await waitFor(() => expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument());

    const nameInput = screen.getByPlaceholderText(/Key name/);
    fireEvent.change(nameInput, { target: { value: "new-key" } });
    fireEvent.click(screen.getByText("Select all read-only"));
    await waitFor(() => expect(screen.getByText("Create key")).not.toBeDisabled());
    fireEvent.click(screen.getByText("Create key"));

    await waitFor(() => expect(screen.getByText(/Copy this token now/)).toBeInTheDocument());

    // The one-time token AND a config snippet containing that same token —
    // not just a bare token with nothing telling you how to use it.
    expect(screen.getByText("ibmcp_the-real-raw-token")).toBeInTheDocument();
    expect(screen.getByText(/Add this to your MCP client's config/)).toBeInTheDocument();
    const configBlocks = screen.getAllByText(/ibmcp_the-real-raw-token/);
    expect(configBlocks.length).toBeGreaterThan(1); // one in the bare-token <code>, one in the config snippet
  });
});

describe("McpKeys optional expiry (TRK-160)", () => {
  it("parses the expiry field: blank means never, valid ints pass, junk is rejected", () => {
    expect(parseExpiresDays("")).toBeNull();
    expect(parseExpiresDays("   ")).toBeNull();
    expect(parseExpiresDays("30")).toBe(30);
    expect(parseExpiresDays("1")).toBe(1);
    expect(parseExpiresDays("3650")).toBe(3650);
    expect(parseExpiresDays("0")).toBe("bad");
    expect(parseExpiresDays("-1")).toBe("bad");
    expect(parseExpiresDays("3651")).toBe("bad");
    expect(parseExpiresDays("1.5")).toBe("bad");
    expect(parseExpiresDays("abc")).toBe("bad");
  });

  it("shows an expiry column with 'never' for a null expires_at and a date otherwise", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/mcp-keys/tools")) return CATALOG;
      if (url.includes("/mcp-keys")) {
        return [
          ONE_KEY[0], // expires_at absent → never
          {
            ...ONE_KEY[0],
            id: "k2",
            name: "expiring-key",
            expires_at: "2027-01-15T09:30:00Z",
            expired: false,
          },
        ];
      }
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderMcpKeys();
    await waitFor(() => expect(screen.getByText("expiring-key")).toBeInTheDocument());

    const table = screen.getByRole("table");
    expect(within(table).getByText("never")).toBeInTheDocument();
    expect(within(table).getByText("2027-01-15 09:30:00")).toBeInTheDocument();
  });

  it("renders 'expired' as a distinct warn Badge, not as 'revoked'", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/mcp-keys/tools")) return CATALOG;
      if (url.includes("/mcp-keys")) {
        return [
          {
            ...ONE_KEY[0],
            id: "k3",
            name: "stale-key",
            expires_at: "2020-01-01T00:00:00Z",
            expired: true,
            revoked: false,
          },
          { ...ONE_KEY[0], id: "k4", name: "killed-key", revoked: true, expired: true },
        ];
      }
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderMcpKeys();
    await waitFor(() => expect(screen.getByText("stale-key")).toBeInTheDocument());

    // Ran out on its own → "expired" (warn), never conflated with "revoked".
    const expiredBadge = screen.getByText("expired");
    expect(expiredBadge.className).toContain("ib-sev-");
    expect(screen.queryByText("active")).not.toBeInTheDocument();

    // Deliberately revoked AND past its expiry → "revoked" wins, because the
    // deliberate act is the fact an operator needs.
    expect(screen.getByText("revoked")).toBeInTheDocument();
  });

  it("omits expires_days from the create request when the field is left blank", async () => {
    mockLoadSuccess();
    const apiPost = vi.spyOn(api, "apiPost").mockResolvedValue({
      id: "k9",
      name: "no-expiry",
      token: "ibmcp_tok",
      allowed_tools: ["read_hosts"],
      expires_at: null,
    });

    renderMcpKeys();
    await waitFor(() => expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText(/Key name/), { target: { value: "no-expiry" } });
    fireEvent.click(screen.getByText("Select all read-only"));
    await waitFor(() => expect(screen.getByText("Create key")).not.toBeDisabled());
    fireEvent.click(screen.getByText("Create key"));

    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    const body = apiPost.mock.calls[0][1] as Record<string, unknown>;
    expect("expires_days" in body).toBe(false);

    // And the one-time token panel says so in words. Matched on the whole
    // "Expires: never" line rather than a bare "never" — the table row for the
    // existing no-expiry key legitimately renders "never" too.
    await waitFor(() => expect(screen.getByText(/Copy this token now/)).toBeInTheDocument());
    expect(
      screen.getByText((_, el) => el?.textContent === "Expires: never" && el.tagName === "DIV"),
    ).toBeInTheDocument();
  });

  it("sends expires_days and echoes the computed expires_at when a value is entered", async () => {
    mockLoadSuccess();
    const apiPost = vi.spyOn(api, "apiPost").mockResolvedValue({
      id: "k10",
      name: "expiring",
      token: "ibmcp_tok",
      allowed_tools: ["read_hosts"],
      expires_at: "2026-09-12T10:00:00Z",
    });

    renderMcpKeys();
    await waitFor(() => expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText(/Key name/), { target: { value: "expiring" } });
    fireEvent.change(screen.getByLabelText(/Expires in days/), { target: { value: "30" } });
    fireEvent.click(screen.getByText("Select all read-only"));
    await waitFor(() => expect(screen.getByText("Create key")).not.toBeDisabled());
    fireEvent.click(screen.getByText("Create key"));

    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    expect(apiPost.mock.calls[0][1]).toMatchObject({ expires_days: 30 });

    await waitFor(() => expect(screen.getByText(/Copy this token now/)).toBeInTheDocument());
    expect(screen.getByText("2026-09-12 10:00:00")).toBeInTheDocument();
  });

  it("blocks Create and explains why when the expiry value is out of range", async () => {
    mockLoadSuccess();
    const apiPost = vi.spyOn(api, "apiPost");

    renderMcpKeys();
    await waitFor(() => expect(screen.getByText("infra-ops-prod-subnet-a")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText(/Key name/), { target: { value: "bad-expiry" } });
    fireEvent.click(screen.getByText("Select all read-only"));
    fireEvent.change(screen.getByLabelText(/Expires in days/), { target: { value: "99999" } });

    await waitFor(() => expect(screen.getByText("Create key")).toBeDisabled());
    expect(screen.getByText(/between 1 and 3650/)).toBeInTheDocument();
    expect(apiPost).not.toHaveBeenCalled();
  });
});

describe("mcpClientConfig", () => {
  it("produces the Claude Code mcpServers shape (docs/MCP_SERVER.md) with the token embedded", () => {
    const json = mcpClientConfig("http://192.0.2.13:8002/mcp", "ibmcp_abc123");
    const parsed = JSON.parse(json);

    expect(parsed).toEqual({
      mcpServers: {
        "infra-brain": {
          type: "streamable-http",
          url: "http://192.0.2.13:8002/mcp",
          headers: { Authorization: "Bearer ibmcp_abc123" },
        },
      },
    });
  });

  it("is valid, pretty-printed JSON (copy-pasteable as-is)", () => {
    const json = mcpClientConfig("http://example:8002/mcp", "tok");
    expect(() => JSON.parse(json)).not.toThrow();
    expect(json).toContain("\n"); // pretty-printed, not a single line
  });
});
