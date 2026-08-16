import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Chat } from "./Chat";

beforeEach(() => {
  // jsdom doesn't implement scrollIntoView; Chat scrolls to the last message.
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) never fires — see Home.test.tsx for the
  // same pattern.
  cleanup();
  vi.restoreAllMocks();
});

function renderChat() {
  return render(
    <MemoryRouter>
      <Chat />
    </MemoryRouter>,
  );
}

/** Mock the SSE chokepoint with a canned list of raw chunks. Frames are
 *  deliberately handed over split across chunk boundaries in one test to pin
 *  the buffering behaviour. */
function mockStream(chunks: string[]) {
  return vi.spyOn(api, "apiPostRawStream").mockImplementation(async function* () {
    for (const c of chunks) yield c;
  });
}

const META = 'event: meta\ndata: {"thread_id": "t-1"}\n\n';
const DONE = "event: done\ndata: {}\n\n";

describe("Chat page — empty state", () => {
  it("shows starter questions derived from the agent's real tools", () => {
    renderChat();
    expect(screen.getByText(/what can I ask\?/i)).toBeInTheDocument();
    expect(screen.getByText("Give me a fleet health summary.")).toBeInTheDocument();
    expect(screen.getByText("What drifted in the last 24 hours?")).toBeInTheDocument();
    expect(
      screen.getByText("What's connected to node_a in the knowledge graph?"),
    ).toBeInTheDocument();
  });

  it("states the one non-read-only capability instead of leaving it to be discovered", () => {
    renderChat();
    expect(screen.getByText(/opens a GitLab merge request/i)).toBeInTheDocument();
  });

  it("clicking a starter sends it as the question", async () => {
    mockStream([META, 'data: {"token": "all green"}\n\n', DONE]);
    renderChat();
    fireEvent.click(screen.getByText("Give me a fleet health summary."));
    await waitFor(() =>
      expect(screen.getByText(/^Give me a fleet health summary\.$/)).toBeInTheDocument(),
    );
    await waitFor(() => expect(screen.getByText("all green")).toBeInTheDocument());
  });
});

describe("Chat page — in-flight state", () => {
  it("shows the button in its loading state until the stream finishes", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    vi.spyOn(api, "apiPostRawStream").mockImplementation(async function* () {
      yield META;
      await gate;
      yield DONE;
    });

    renderChat();
    fireEvent.change(screen.getByLabelText("Ask a question"), {
      target: { value: "how is the fleet?" },
    });
    fireEvent.click(screen.getByText("Send"));

    await waitFor(() => expect(screen.getByText("Thinking")).toBeInTheDocument());
    release();
    await waitFor(() => expect(screen.getByText("Send")).toBeInTheDocument());
  });
});

describe("Chat page — error state", () => {
  it("renders the backend's structured error as a what-to-check checklist", async () => {
    mockStream([
      META,
      'event: error\ndata: {"code": "llm_unreachable", "message": "Could not reach the language-model endpoint.", ' +
        '"hints": ["Provider: openai \\u00b7 model: auto/best-reasoning \\u00b7 endpoint: https://gw.internal:4000/v1", ' +
        '"Check the endpoint is up and routable from this container"], "token": "\\n\\n[error: chat request failed]"}\n\n',
      DONE,
    ]);

    renderChat();
    fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "hello" } });
    fireEvent.click(screen.getByText("Send"));

    await waitFor(() =>
      expect(screen.getByText("Could not reach the language-model endpoint.")).toBeInTheDocument(),
    );
    expect(screen.getByText(/code: llm_unreachable/)).toBeInTheDocument();
    expect(screen.getByText(/endpoint: https:\/\/gw.internal:4000\/v1/)).toBeInTheDocument();
  });

  it("renders a transport failure (API itself unreachable) without blanking the page", async () => {
    vi.spyOn(api, "apiPostRawStream").mockImplementation(async function* () {
      throw new api.ApiError(0, "/api/dashboard/chat");
      // eslint-disable-next-line no-unreachable
      yield "";
    });

    renderChat();
    fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "hello" } });
    fireEvent.click(screen.getByText("Send"));

    await waitFor(() =>
      expect(screen.getByText("Could not reach the Infra Brain API.")).toBeInTheDocument(),
    );
  });
});

describe("Chat page — provenance", () => {
  it("shows which graph entities and edges an answer was built from", async () => {
    const prov = {
      found: true,
      identifier: "node_a",
      root: { name: "node_a", type: "host" },
      nodes: [{ name: "litellm", type: "service", source: "homelab" }],
      edges: [
        {
          from: "litellm",
          to: "node_a",
          edge_type: "RUNS_ON",
          method: "compose_file",
          confidence: 0.95,
          source: "homelab",
        },
      ],
      node_total: 2,
      edge_total: 1,
      truncated: false,
    };
    mockStream([
      META,
      'event: tool\ndata: {"tool": "query_graph_neighborhood", "args": {"resource_id": "node_a"}}\n\n',
      `event: provenance\ndata: ${JSON.stringify({ tool: "query_graph_neighborhood", provenance: prov })}\n\n`,
      'data: {"token": "litellm runs on node_a."}\n\n',
      DONE,
    ]);

    renderChat();
    fireEvent.click(screen.getByText("What's connected to node_a in the knowledge graph?"));

    await waitFor(() => expect(screen.getByText("RUNS_ON")).toBeInTheDocument());
    expect(screen.getByText(/Knowledge-graph evidence/)).toBeInTheDocument();
    expect(screen.getByText("compose_file")).toBeInTheDocument();
    expect(screen.getByText("0.95")).toBeInTheDocument();
    // The tool call itself is surfaced too, so a text-tool answer (which has no
    // structured citations) still shows what was consulted.
    expect(
      screen.getByText(/query_graph_neighborhood\(resource_id=node_a\)/),
    ).toBeInTheDocument();
  });

  it("says a graph lookup found nothing rather than silently omitting it", async () => {
    const prov = {
      found: false,
      identifier: "nope",
      reason: "'nope' not found in the knowledge graph (graph_nodes).",
      nodes: [],
      edges: [],
    };
    mockStream([
      META,
      `event: provenance\ndata: ${JSON.stringify({ tool: "query_graph_neighborhood", provenance: prov })}\n\n`,
      'data: {"token": "I could not find that host."}\n\n',
      DONE,
    ]);

    renderChat();
    fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "what is nope?" } });
    fireEvent.click(screen.getByText("Send"));

    await waitFor(() => expect(screen.getByText(/was not found/)).toBeInTheDocument());
  });
});

describe("Chat page — streaming mechanics", () => {
  it("reassembles SSE frames split across chunk boundaries", async () => {
    mockStream([META, 'data: {"token": "hel', 'lo"}\n\ndata: {"token": " there"}\n\n', DONE]);
    renderChat();
    fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "hi" } });
    fireEvent.click(screen.getByText("Send"));
    await waitFor(() => expect(screen.getByText("hello there")).toBeInTheDocument());
  });

  it("New conversation clears the transcript and drops the thread", async () => {
    mockStream([META, 'data: {"token": "ok"}\n\n', DONE]);
    renderChat();
    fireEvent.click(screen.getByText("Give me a fleet health summary."));
    await waitFor(() => expect(screen.getByText("ok")).toBeInTheDocument());

    fireEvent.click(screen.getByText("New conversation"));
    await waitFor(() => expect(screen.queryByText("ok")).not.toBeInTheDocument());
    expect(screen.getByText(/what can I ask\?/i)).toBeInTheDocument();
  });
});
