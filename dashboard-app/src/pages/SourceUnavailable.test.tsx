/** A hidden source's route must render a clean explanation — not a crash, not a
 *  blank screen, not a spinner that never resolves. */
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { RETIRED_SOURCES } from "../nav";
import { SourceUnavailable } from "./SourceUnavailable";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderAt(pathname: string) {
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <SourceUnavailable />
    </MemoryRouter>,
  );
}

describe("SourceUnavailable — the page a hidden source's route lands on", () => {
  it("names the source and says plainly that it is not configured here", () => {
    renderAt("/vsphere");
    expect(screen.getByText(RETIRED_SOURCES["/vsphere"].title)).toBeInTheDocument();
    expect(screen.getByText(/isn't configured here/i)).toBeInTheDocument();
  });

  it("explains why, using the epitaph from nav.ts rather than its own copy", () => {
    renderAt("/vsphere");
    expect(screen.getByText(RETIRED_SOURCES["/vsphere"].reason)).toBeInTheDocument();
  });

  it("names the retired collector domains so the reason is checkable", () => {
    renderAt("/cloud");
    expect(screen.getByText(/cloud, k8s/)).toBeInTheDocument();
  });

  it("renders every retired route without crashing", () => {
    for (const path of Object.keys(RETIRED_SOURCES)) {
      renderAt(path);
      expect(screen.getByText(RETIRED_SOURCES[path].reason)).toBeInTheDocument();
      cleanup();
    }
  });

  it("falls back to a generic explanation for an unknown path, never a blank page", () => {
    renderAt("/some-route-nobody-listed");
    expect(screen.getByText(/not configured in this environment/i)).toBeInTheDocument();
  });

  it("fetches nothing — a retired source has no endpoint worth calling", () => {
    const apiGet = vi.spyOn(api, "apiGet");
    renderAt("/vsphere");
    expect(apiGet).not.toHaveBeenCalled();
  });

  it("tells the operator how to bring the source back", () => {
    // Was /software until 2026-08-13; that route was restored to the real page
    // by owner decision (software inventory is wanted, only its collector is
    // missing). /cloud is the same shape of claim over a source this
    // deployment genuinely will not run.
    renderAt("/cloud");
    expect(screen.getByText(/COLLECTION_REVIVED_DOMAINS/)).toBeInTheDocument();
  });
});
