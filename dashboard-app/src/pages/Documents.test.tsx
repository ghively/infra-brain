import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Documents } from "./Documents";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Security.test.tsx/Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

function doc(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: "d1",
    title: "Runbook: vSphere host maintenance",
    sensitivity: "internal",
    source: "confluence",
    status: "current",
    space: "OPS",
    url: "https://example.test/doc",
    indexed_at: "2026-07-20T00:00:00Z",
    last_updated: "2026-07-24T00:00:00Z",
    chunk_count: 12,
    ...overrides,
  };
}

function mockLoadSuccess(opts?: { items?: unknown[]; total?: number; staleTotal?: number }) {
  const items = opts?.items ?? [doc()];
  const total = opts?.total ?? items.length;
  const staleTotal = opts?.staleTotal ?? 0;
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("status=stale&limit=1")) {
      return { items: [], total: staleTotal, limit: 1, offset: 0 };
    }
    return { items, total, limit: 100, offset: 0 };
  });
}

function renderDocuments() {
  return render(
    <MemoryRouter>
      <Documents />
    </MemoryRouter>,
  );
}

describe("Documents page — Phase 3 design-system rebuild", () => {
  it("renders the documents table with sensitivity and status badges", async () => {
    mockLoadSuccess({ items: [doc({ sensitivity: "restricted", status: "stale" })] });

    renderDocuments();

    await waitFor(() => expect(screen.getByText("Runbook: vSphere host maintenance")).toBeInTheDocument());
    expect(screen.getByText("restricted")).toBeInTheDocument();
    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.getByText(/read-only/)).toBeInTheDocument();
  });

  it("'documents' and 'stale' pills reflect the true fleet-wide total, not the current page", async () => {
    mockLoadSuccess({ items: [doc()], total: 4321, staleTotal: 17 });

    renderDocuments();

    await waitFor(() => expect(screen.getByText("4321 documents")).toBeInTheDocument());
    expect(screen.getByText("17 stale")).toBeInTheDocument();
  });

  it("the Stale tab filters the query and re-fetches with status=stale", async () => {
    mockLoadSuccess({ items: [doc()] });

    renderDocuments();

    await waitFor(() => expect(screen.getByText("Runbook: vSphere host maintenance")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Stale" }));

    await waitFor(() =>
      expect(api.apiGet).toHaveBeenCalledWith(expect.stringContaining("status=stale")),
    );
  });

  it("clicking a row opens the detail drawer and fetches document detail", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/documents/d1")) {
        return {
          ...doc(),
          external_id: "conf-123",
          source_version: 3,
          content_hash: "abcdef123456",
          chunks: [{ chunk_index: 0, text_preview: "Step one...", token_count: 128 }],
        };
      }
      if (url.includes("status=stale&limit=1")) return { items: [], total: 0, limit: 1, offset: 0 };
      return { items: [doc()], total: 1, limit: 100, offset: 0 };
    });

    renderDocuments();

    await waitFor(() => expect(screen.getByText("Runbook: vSphere host maintenance")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Runbook: vSphere host maintenance"));

    await waitFor(() => expect(screen.getByText("Chunks (12)")).toBeInTheDocument());
    expect(screen.getByText("Step one...")).toBeInTheDocument();
  });

  it("shows a filter-zero empty state (not a false 'none yet' message) when a search matches nothing", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("status=stale&limit=1")) return { items: [], total: 3, limit: 1, offset: 0 };
      if (url.includes("q=zzz")) return { items: [], total: 0, limit: 100, offset: 0 };
      return { items: [doc()], total: 3, limit: 100, offset: 0 };
    });

    renderDocuments();

    await waitFor(() => expect(screen.getByText("Runbook: vSphere host maintenance")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText("Search title…"), { target: { value: "zzz" } });

    expect(await screen.findByText("No documents match the current filters")).toBeInTheDocument();
  });

  it("shows a 'none-yet' empty state, distinct from filter-zero, when nothing is indexed at all", async () => {
    mockLoadSuccess({ items: [], total: 0, staleTotal: 0 });

    renderDocuments();

    await waitFor(() => expect(screen.getByText("No documents indexed yet")).toBeInTheDocument());
  });

  it("shows a full-page error state on load failure", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderDocuments();

    await waitFor(() => expect(screen.getByText("Documents failed to load")).toBeInTheDocument());
  });
});
