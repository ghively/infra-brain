import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Homelab } from "./Homelab";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Cloud.test.tsx / Security.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

function resourcePage(items: unknown[]) {
  return { items, total: items.length, limit: 200, offset: 0 };
}

function serviceResource(
  name: string,
  category: string,
  status: string,
  overrides: Record<string, string> = {},
) {
  return {
    id: `${name}-id`,
    hostname: name,
    domain: "homelab_services",
    resource_type: "homelab_service",
    zone: "ai_node",
    status: "healthy",
    last_seen_at: "2026-07-30T00:00:00Z",
    drift_count: 0,
    meta: [
      { k: "category", v: category },
      { k: "url", v: overrides.url ?? `http://127.0.0.1:1234` },
      { k: "status", v: status },
      { k: "http_status", v: overrides.http_status ?? "200" },
    ],
  };
}

function renderHomelab() {
  return render(
    <MemoryRouter>
      <Homelab />
    </MemoryRouter>,
  );
}

describe("Homelab Services page", () => {
  it("shows a none-yet empty state when no homelab_services resources exist", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(resourcePage([]));

    renderHomelab();

    await waitFor(() => expect(screen.getByText("Homelab Services")).toBeInTheDocument());
    expect(screen.getByText("No homelab services collected")).toBeInTheDocument();
  });

  it("groups services into one panel per category, sorted alphabetically", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(
      resourcePage([
        serviceResource("sonarr", "media-management", "up"),
        serviceResource("radarr", "media-management", "down"),
        serviceResource("searxng", "search", "up"),
      ]),
    );

    renderHomelab();

    await waitFor(() => expect(screen.getByText("sonarr")).toBeInTheDocument());
    expect(screen.getByText("radarr")).toBeInTheDocument();
    expect(screen.getByText("searxng")).toBeInTheDocument();

    // Category mnemonics render uppercased.
    expect(screen.getByText("MEDIA-MANAGEMENT")).toBeInTheDocument();
    expect(screen.getByText("SEARCH")).toBeInTheDocument();

    // Per-category counts in the panel description.
    expect(screen.getByText("2 services · 1 up")).toBeInTheDocument();
    expect(screen.getByText("1 service · 1 up")).toBeInTheDocument();

    // Overall pill counts.
    expect(screen.getByText("3 services")).toBeInTheDocument();
    expect(screen.getByText("2 up")).toBeInTheDocument();
    expect(screen.getByText("1 down")).toBeInTheDocument();
    expect(screen.getByText("2 categories")).toBeInTheDocument();
  });

  it("buckets services missing a category under an explicit uncategorized group", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(
      resourcePage([serviceResource("mystery-svc", "", "up")]),
    );

    renderHomelab();

    await waitFor(() => expect(screen.getByText("mystery-svc")).toBeInTheDocument());
    expect(screen.getByText("(UNCATEGORIZED)")).toBeInTheDocument();
  });

  it("shows a full-page error state on load failure", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderHomelab();

    await waitFor(() =>
      expect(screen.getByText("Homelab services failed to load")).toBeInTheDocument(),
    );
  });
});
