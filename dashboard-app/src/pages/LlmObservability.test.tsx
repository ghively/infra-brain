import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { LlmObservability } from "./LlmObservability";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const TOKEN_METRIC =
  "Tokens are per-CALL totals (prompt + completion) summed over the run. The full conversation is re-sent on every turn, so a run's total is what the provider billed — necessarily larger than the number of distinct tokens in the transcript.";

const FLAGS = [
  { name: "rootcause_llm_enabled", enabled: false, effect: "Off: root-cause analysis is deterministic." },
  { name: "compliance_gap_finder_enabled", enabled: false, effect: "Off: the compliance gap finder never calls the model." },
  { name: "remediation_interrupt_enabled", enabled: false, effect: "Off: remediation never pauses for approval." },
  { name: "langfuse_enabled", enabled: false, effect: "Off: no external trace backend." },
];

const SUMMARY = {
  window_hours: 168,
  since: "2026-08-06T00:00:00Z",
  generated_at: "2026-08-13T00:00:00Z",
  provider: "openai",
  model: "glm-4.6",
  runs: 2,
  turns: 3,
  tokens_billed: 460,
  peak_call_tokens: 250,
  tool_calls: 5,
  narrated_turns: 2,
  silent_turns: 1,
  outcomes: { completed: 1, recursion_limit: 1, truncated: 0, unknown: 0 },
  by_agent: [
    {
      agent: "DiscoveryAgent",
      domain: "discovery",
      runs: 2,
      turns: 3,
      tokens_billed: 460,
      peak_call_tokens: 250,
      tool_calls: 5,
      narrated_turns: 2,
      silent_turns: 1,
      completed: 1,
      recursion_limit: 1,
      truncated: 0,
      last_run_at: "2026-08-13T00:00:00Z",
    },
  ],
  top_tools: [
    { tool: "gitlab_file_tool", calls: 9, max_in_one_iteration: 6 },
    { tool: "gitlab_projects_tool", calls: 1, max_in_one_iteration: 1 },
  ],
  flags: FLAGS,
  token_ceiling_enabled: false,
  token_ceiling: 100000,
  rows_scanned: 4,
  truncated_scan: false,
  scan_cap: 20000,
  token_metric: TOKEN_METRIC,
};

const RUN_RECURSION = {
  run_id: "aaaaaaaa-0000-4000-8000-000000000001",
  agent: "DiscoveryAgent",
  domain: "discovery",
  started_at: "2026-08-13T00:00:00Z",
  ended_at: "2026-08-13T00:01:00Z",
  turns: 2,
  tokens_billed: 350,
  peak_call_tokens: 250,
  tool_calls: 4,
  distinct_tools: 2,
  max_tool_repeat: 6,
  narrated_turns: 1,
  silent_turns: 1,
  outcome: "recursion_limit",
};

const RUN_COMPLETED = {
  ...RUN_RECURSION,
  run_id: "bbbbbbbb-0000-4000-8000-000000000002",
  tokens_billed: 110,
  max_tool_repeat: 1,
  outcome: "completed",
};

const DETAIL = {
  ...RUN_RECURSION,
  outcome_reason:
    "The loop hit its recursion limit (2 * max_iters + 1 graph steps) before converging.",
  token_metric: TOKEN_METRIC,
  steps: [
    {
      iteration: 0,
      ts: "2026-08-13T00:00:00Z",
      call_tokens: 100,
      tools_chosen: ["gitlab_file_tool", "gitlab_file_tool"],
      tool_repeats: { gitlab_file_tool: 2 },
      reasoning_text: "",
      reasoning_state: "absent_tool_call_turn",
    },
    {
      iteration: 1,
      ts: "2026-08-13T00:00:30Z",
      call_tokens: 250,
      tools_chosen: [],
      tool_repeats: {},
      reasoning_text: "I now understand the repo layout.",
      reasoning_state: "present",
    },
  ],
};

function mockApi(
  summary: unknown = SUMMARY,
  runs: unknown = { items: [RUN_RECURSION, RUN_COMPLETED], total: 2, limit: 50, offset: 0, token_metric: TOKEN_METRIC },
  detail: unknown = DETAIL,
) {
  return vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/llm/summary")) return summary;
    if (/\/llm\/runs\/[^?]/.test(url)) return detail;
    return runs;
  });
}

function renderPage() {
  return render(
    <MemoryRouter>
      <LlmObservability />
    </MemoryRouter>,
  );
}

describe("LLM Observability page", () => {
  it("shows the headline usage figures with an explicit statement of what tokens measure", async () => {
    mockApi();
    renderPage();

    await waitFor(() => expect(screen.getByText("LLM Runs")).toBeInTheDocument());
    expect(screen.getByText("Tokens Billed")).toBeInTheDocument();
    expect(screen.getByText("sum of per-call totals")).toBeInTheDocument();
    // The label must be on the page, not just in a code comment.
    expect(screen.getByText(/per-CALL totals \(prompt \+ completion\)/)).toBeInTheDocument();
    expect(screen.getByText(/is one reasoning loop/)).toBeInTheDocument();
  });

  it("states how many model calls narrated vs. called tools silently", async () => {
    mockApi();
    renderPage();
    await waitFor(() =>
      expect(screen.getByText(/called tools\s*without narrating, which is the model/)).toBeInTheDocument(),
    );
  });

  it("makes a recursion-limit run visibly distinct from a completed one", async () => {
    mockApi();
    renderPage();

    await waitFor(() => expect(screen.getByText("recursion limit")).toBeInTheDocument());
    expect(screen.getByText("completed")).toBeInTheDocument();
    // And the summary strip calls it out as its own stat, not folded into a
    // generic "errors" bucket.
    expect(screen.getByText("Hit Recursion Limit")).toBeInTheDocument();
    expect(screen.getByText("ran out of steps, no answer")).toBeInTheDocument();
  });

  it("flags a tool hammered inside a single turn as a possible loop", async () => {
    mockApi();
    renderPage();
    await waitFor(() => expect(screen.getAllByText("×6 in one turn").length).toBeGreaterThan(0));
  });

  it("explains an absent reasoning step instead of rendering a blank", async () => {
    mockApi();
    renderPage();

    await waitFor(() => expect(screen.getByText(/aaaaaaaa/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/aaaaaaaa/));

    await waitFor(() =>
      expect(
        screen.getByText(/the model called tools without narrating\. Nothing was lost/),
      ).toBeInTheDocument(),
    );
    // The narrated step still shows its real text.
    expect(screen.getByText("I now understand the repo layout.")).toBeInTheDocument();
    // And per-call tokens are labelled as per-call, not as a run total.
    expect(screen.getByText("100 tokens this call")).toBeInTheDocument();
  });

  it("explains how a run ended and links to the full transcript", async () => {
    mockApi();
    renderPage();
    await waitFor(() => expect(screen.getByText(/aaaaaaaa/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/aaaaaaaa/));

    await waitFor(() =>
      expect(screen.getByText(/hit its recursion limit/)).toBeInTheDocument(),
    );
    const link = screen.getByRole("link", { name: /full transcript/i });
    expect(link).toHaveAttribute("href", `/decisions?run_id=${RUN_RECURSION.run_id}`);
  });

  it("says WHY a section may be empty when the LLM feature flags are off", async () => {
    mockApi();
    renderPage();

    await waitFor(() => expect(screen.getByText("rootcause_llm_enabled")).toBeInTheDocument());
    expect(screen.getByText("langfuse_enabled")).toBeInTheDocument();
    expect(screen.getByText(/All optional LLM features are off/)).toBeInTheDocument();
    // The cost ceiling's OFF state is stated as a risk, not silently omitted.
    expect(screen.getByText(/Off: nothing stops a run from spending/)).toBeInTheDocument();
  });

  it("does not imply a loading failure when there genuinely were no runs", async () => {
    mockApi(
      { ...SUMMARY, runs: 0, turns: 0, tokens_billed: 0, by_agent: [], top_tools: [], outcomes: { completed: 0, recursion_limit: 0, truncated: 0, unknown: 0 } },
      { items: [], total: 0, limit: 50, offset: 0, token_metric: TOKEN_METRIC },
    );
    renderPage();

    await waitFor(() => expect(screen.getByText("No LLM runs in this window")).toBeInTheDocument());
    expect(screen.getByText(/That is the whole story — not a loading failure/)).toBeInTheDocument();
    expect(screen.getByText("No agent used the LLM in this window")).toBeInTheDocument();
  });

  it("warns when the summary only covers part of the window", async () => {
    mockApi({ ...SUMMARY, truncated_scan: true, rows_scanned: 20000, scan_cap: 20000 });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText(/cover only the most recent 20,000/)).toBeInTheDocument(),
    );
  });

  it("refetches with a different window when a window button is clicked", async () => {
    const spy = mockApi();
    renderPage();
    await waitFor(() => expect(screen.getByText("LLM Runs")).toBeInTheDocument());

    fireEvent.click(screen.getByText("24h"));
    await waitFor(() =>
      expect(spy.mock.calls.some(([u]) => String(u).includes("window_hours=24"))).toBe(true),
    );
  });

  it("does not blank the page on a refresh error", async () => {
    const spy = mockApi();
    renderPage();
    await waitFor(() => expect(screen.getByText("LLM Runs")).toBeInTheDocument());

    spy.mockRejectedValue(new Error("boom"));
    fireEvent.click(screen.getByText("30d"));
    await waitFor(() => expect(screen.getByText("STALE")).toBeInTheDocument());
    // Previously-loaded data survives.
    expect(screen.getByText("LLM Runs")).toBeInTheDocument();
  });

  it("shows an error state when the very first load fails", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("nope"));
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("LLM observability failed to load")).toBeInTheDocument(),
    );
  });
});
