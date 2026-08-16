import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Iac } from "./Iac";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see ScanSchedule.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const OVERVIEW = {
  projects: [
    {
      gitlab_project_id: 1,
      name: "infra-brain",
      path_with_namespace: "agents/infra-brain",
      default_branch: "master",
      archived: false,
      last_pipeline_status: "success",
      last_activity_at: "2026-07-27T12:00:00Z",
      file_count: 12,
      files_by_type: { terraform: 4, k8s: 8 },
      recent_pipelines: [
        {
          pipeline_id: 501,
          ref: "master",
          status: "success",
          source: "push",
          created_at: "2026-07-27T11:00:00Z",
          duration: 125,
          web_url: "https://gitlab.example.test/agents/infra-brain/-/pipelines/501",
        },
      ],
    },
    {
      gitlab_project_id: 2,
      name: "archived-proj",
      path_with_namespace: "agents/archived-proj",
      default_branch: "main",
      archived: true,
      last_pipeline_status: "failed",
      last_activity_at: null,
      file_count: 0,
      files_by_type: {},
      recent_pipelines: [],
    },
  ],
  summary: { project_count: 2, file_count: 12, pipeline_run_count: 1 },
};

function mockOverview(overview: unknown = OVERVIEW) {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/api/iac/overview")) return overview;
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function renderIac() {
  return render(
    <MemoryRouter>
      <Iac />
    </MemoryRouter>,
  );
}

describe("Iac page — Phase 3 design-system rebuild", () => {
  it("renders the projects overview in a DataTable with file-type chips and last-pipeline badge", async () => {
    mockOverview();
    renderIac();

    await waitFor(() => expect(screen.getAllByText("infra-brain")[0]).toBeInTheDocument());
    expect(screen.getByText("terraform 4")).toBeInTheDocument();
    expect(screen.getByText("k8s 8")).toBeInTheDocument();
    expect(screen.getAllByText("success").length).toBeGreaterThan(0);
    expect(screen.getByText("archived")).toBeInTheDocument();
    expect(screen.getByText("no files parsed")).toBeInTheDocument();
  });

  it("renders recent pipeline runs grouped per project with a working outbound link", async () => {
    mockOverview();
    renderIac();

    await waitFor(() => expect(screen.getByText("#501")).toBeInTheDocument());
    const link = screen.getByText("#501").closest("a");
    expect(link).toHaveAttribute("href", "https://gitlab.example.test/agents/infra-brain/-/pipelines/501");
  });

  it("switches to the Compose Services sub-view and fetches its endpoint", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/iac/overview")) return OVERVIEW;
      if (url.includes("/api/iac/compose-services")) {
        return { items: [{ service_name: "web", image: "nginx:latest", ports: [], project_name: "infra-brain", file_path: "docker-compose.yml" }], total: 1, limit: 200, offset: 0 };
      }
      throw new Error(`unexpected apiGet: ${url}`);
    });
    renderIac();
    await waitFor(() => expect(screen.getAllByText("infra-brain")[0]).toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: "Compose Services" }));

    await waitFor(() => expect(screen.getByText("nginx:latest")).toBeInTheDocument());
  });

  it("renders Ansible playbook hosts as chips, not a raw JSON dump (TRK-198 regression guard)", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/iac/overview")) return OVERVIEW;
      if (url.includes("/api/iac/ansible")) {
        return {
          groups: [{ name: "webservers", project_name: "infra-brain", file_path: "inventory.yml", hosts: [{ name: "web-01" }, { name: "web-02" }] }],
          plays: [{ name: "Deploy web", play_index: 0, hosts: "webservers", project_name: "infra-brain", file_path: "deploy.yml" }],
        };
      }
      throw new Error(`unexpected apiGet: ${url}`);
    });
    renderIac();
    await waitFor(() => expect(screen.getAllByText("infra-brain")[0]).toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: "Ansible" }));

    await waitFor(() => expect(screen.getByText("web-01")).toBeInTheDocument());
    expect(screen.getByText("web-02")).toBeInTheDocument();
    expect(screen.getAllByText("webservers").length).toBeGreaterThan(0);
    // The bug this guards against: hosts rendered as `[object Object]` or a
    // raw JSON.stringify blob instead of readable chip text.
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
  });

  it("shows a none-yet EmptyState for CI Schedules when nothing has been collected", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/iac/overview")) return OVERVIEW;
      if (url.includes("/api/iac/ci-schedules")) return { items: [], total: 0, limit: 200, offset: 0 };
      throw new Error(`unexpected apiGet: ${url}`);
    });
    renderIac();
    await waitFor(() => expect(screen.getAllByText("infra-brain")[0]).toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: "CI Schedules" }));

    await waitFor(() => expect(screen.getByText("No CI pipeline schedules collected yet")).toBeInTheDocument());
  });

  it("shows an error EmptyState when the overview fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network down"));
    renderIac();
    await waitFor(() => expect(screen.getByText("IaC overview failed to load")).toBeInTheDocument());
    expect(screen.getByText(/network down/)).toBeInTheDocument();
  });

  it("shows a none-yet EmptyState when there are no GitLab projects", async () => {
    mockOverview({ projects: [], summary: { project_count: 0, file_count: 0, pipeline_run_count: 0 } });
    renderIac();
    await waitFor(() => expect(screen.getByText("No GitLab projects collected yet")).toBeInTheDocument());
  });
});
