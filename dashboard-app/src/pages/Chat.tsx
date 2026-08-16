import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, AuthRequired, apiPostRawStream, redirectToLogin } from "../api";
import { Icon } from "../lib/icons";
import { Badge, Button, EmptyState, PageShell, Panel } from "../components/ui";

/** Full-page conversation surface for the read-only Infra Brain assistant
 *  (`POST /api/dashboard/chat` → `api/routers/ui.py::_chat_sse` →
 *  `chat/agent.py`'s LangGraph agent and its twelve read-only tools).
 *
 *  WHY A PAGE, given a chat panel already exists. The sidebar's `SidebarChat`
 *  (components/Sidebar.tsx) is a ~260px-wide always-visible strip. It works,
 *  and it stays — but it is the wrong surface for the three things this page
 *  adds, all of which need width and vertical room:
 *
 *   1. **Starter questions.** The assistant's usefulness is entirely a function
 *      of knowing what it can be asked. Every prompt in `STARTERS` below is
 *      derived from a real bound tool's docstring in `chat/tools.py` /
 *      `chat/agent.py` — not invented — so nothing here advertises a capability
 *      the agent does not have.
 *   2. **Provenance.** The SSE stream now carries `event: tool` and
 *      `event: provenance` frames. Only ONE bound tool
 *      (`query_graph_neighborhood`) returns structured rows, so only it yields
 *      citable nodes/edges; the other eleven return a pre-rendered text table,
 *      and for those this page shows the call and its arguments and stops
 *      there. It never implies a citation that does not exist.
 *   3. **Honest failure.** An `event: error` frame carries a classified code,
 *      fixed copy, and non-secret endpoint facts (provider / model / endpoint),
 *      which render as a checklist. A first-time user whose model gateway is
 *      simply down gets "the model isn't reachable — here's what to check"
 *      instead of an empty bubble.
 *
 *  The sidebar panel deliberately keeps working unchanged: it reads only
 *  `data:` payloads' `token`/`thread_id` keys and ignores `event:` names, and
 *  the error frame carries a `token` key for exactly that reason (see
 *  `_sse()`'s docstring in `api/routers/ui.py`).
 *
 *  STREAMING: yes — this page consumes the SSE stream token-by-token via the
 *  `apiPostRawStream` chokepoint, the same mechanism `SidebarChat` and
 *  `ChatDrawer` use. `EventSource` is structurally incapable of driving this
 *  endpoint (GET-only, no body); see ChatDrawer.tsx's note.
 */

type ToolCall = { tool: string; args: Record<string, unknown> };

type ProvenanceEdge = {
  from?: string;
  to?: string;
  edge_type?: string;
  method?: string;
  confidence?: number;
  source?: string;
};

type ProvenanceNode = { name?: string; type?: string; source?: string };

type Provenance = {
  found: boolean;
  identifier?: string;
  reason?: string;
  root?: { name?: string; type?: string };
  nodes: ProvenanceNode[];
  edges: ProvenanceEdge[];
  node_total?: number;
  edge_total?: number;
  truncated?: boolean;
};

type ChatError = {
  code: string;
  message: string;
  hints?: string[];
  provider?: string;
  model?: string;
  endpoint?: string;
};

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  tools: ToolCall[];
  provenance: Provenance[];
};

/** Example questions, one per bound tool, phrased the way an operator would
 *  actually ask. Each maps to a real tool in `chat/agent.py`'s `TOOLS` list —
 *  the `tool` field is documentation for the next person editing this list,
 *  so a starter can never silently outlive the tool that answers it. */
const STARTERS: { group: string; items: { text: string; tool: string }[] }[] = [
  {
    group: "Fleet & inventory",
    items: [
      { text: "Give me a fleet health summary.", tool: "query_fleet_health" },
      { text: "List the linux hosts you know about.", tool: "query_resources" },
      { text: "What packages are installed on node_a?", tool: "query_linux_detail" },
    ],
  },
  {
    group: "Change & risk",
    items: [
      { text: "What drifted in the last 24 hours?", tool: "query_drift_events" },
      { text: "Show the drift trend over the last 30 days.", tool: "query_drift_trend" },
      { text: "Which hosts have open critical CVEs?", tool: "query_vulnerabilities" },
      { text: "Which hosts are EOL within 90 days?", tool: "query_eol_status" },
      { text: "Which compliance violations are still open?", tool: "query_compliance" },
    ],
  },
  {
    group: "Relationships & docs",
    items: [
      { text: "What's connected to node_a in the knowledge graph?", tool: "query_graph_neighborhood" },
      { text: "Which collection runs failed most recently?", tool: "query_collection_runs" },
      { text: "What do the runbooks say about backups?", tool: "search_knowledge" },
    ],
  },
];

/** The single non-read-only capability, called out explicitly so nobody has to
 *  discover it by accident. It opens a GitLab MR for a human — it changes no
 *  live data (see `propose_host_purpose_update` in chat/tools.py). */
const PROPOSE_NOTE =
  "The assistant is read-only with exactly one exception: it can PROPOSE a correction " +
  "to a host's purpose, VLAN or subnet, which opens a GitLab merge request for a human " +
  "to review. It never changes live infrastructure.";

function newMessage(role: ChatMessage["role"], content: string): ChatMessage {
  return { role, content, tools: [], provenance: [] };
}

/** Split an SSE byte stream into `{event, data}` frames. Frames are separated
 *  by a blank line; a frame without a parsable `data:` line is skipped rather
 *  than throwing, so one malformed frame cannot kill a live answer. */
function drainFrames(buffer: string): { frames: { event: string; data: Record<string, unknown> }[]; rest: string } {
  const frames: { event: string; data: Record<string, unknown> }[] = [];
  let buf = buffer;
  let idx: number;
  while ((idx = buf.indexOf("\n\n")) >= 0) {
    const raw = buf.slice(0, idx);
    buf = buf.slice(idx + 2);
    const lines = raw.split("\n");
    const eventLine = lines.find((l) => l.startsWith("event:"));
    const dataLine = lines.find((l) => l.startsWith("data:"));
    if (!dataLine) continue;
    try {
      frames.push({
        event: eventLine ? eventLine.slice(6).trim() : "message",
        data: JSON.parse(dataLine.slice(5).trim()) as Record<string, unknown>,
      });
    } catch {
      /* malformed frame — skip it, keep the stream alive */
    }
  }
  return { frames, rest: buf };
}

function ToolStrip({ tools }: { tools: ToolCall[] }) {
  if (tools.length === 0) return null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
      <span style={{ fontSize: 11, color: "var(--ib-muted)", fontWeight: 600 }}>Consulted</span>
      {tools.map((t, i) => (
        <Badge key={`${t.tool}-${i}`} tone="info">
          {t.tool}
          {Object.keys(t.args || {}).length > 0
            ? `(${Object.entries(t.args)
                .map(([k, v]) => `${k}=${String(v)}`)
                .join(", ")})`
            : "()"}
        </Badge>
      ))}
    </div>
  );
}

function ProvenanceBlock({ prov }: { prov: Provenance }) {
  if (!prov.found) {
    return (
      <div style={{ marginTop: 8, fontSize: 11, color: "var(--ib-muted)" }}>
        Knowledge graph: <strong>{prov.identifier}</strong> was not found — {prov.reason}
      </div>
    );
  }
  return (
    <div
      style={{
        marginTop: 10,
        padding: "10px 12px",
        border: "1px solid var(--ib-border)",
        borderRadius: "var(--ib-radius-sm)",
        background: "var(--ib-raised)",
      }}
    >
      <div style={{ fontSize: 11, fontWeight: 600, color: "var(--ib-muted)", marginBottom: 6 }}>
        Knowledge-graph evidence · centred on {prov.root?.name ?? prov.identifier}
        {prov.root?.type ? ` (${prov.root.type})` : ""}
      </div>
      {prov.edges.length === 0 ? (
        <div style={{ fontSize: 11, color: "var(--ib-faint)" }}>
          No relationships recorded within the walked depth. That means no EDGE exists — not that
          the entity is unused; containment facts (packages, ports, users, crons) live in detail
          tables, not the graph.
        </div>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
          <thead>
            <tr style={{ color: "var(--ib-muted)", textAlign: "left" }}>
              <th style={{ padding: "2px 6px 4px 0" }}>From</th>
              <th style={{ padding: "2px 6px 4px 0" }}>Relationship</th>
              <th style={{ padding: "2px 6px 4px 0" }}>To</th>
              <th style={{ padding: "2px 6px 4px 0" }}>Derived by</th>
              <th style={{ padding: "2px 0 4px 0" }}>Confidence</th>
            </tr>
          </thead>
          <tbody>
            {prov.edges.map((e, i) => (
              <tr key={i} style={{ color: "var(--ib-text)" }}>
                <td style={{ padding: "2px 6px 2px 0" }}>{e.from}</td>
                <td style={{ padding: "2px 6px 2px 0", fontFamily: "var(--ib-mono, monospace)" }}>
                  {e.edge_type}
                </td>
                <td style={{ padding: "2px 6px 2px 0" }}>{e.to}</td>
                <td style={{ padding: "2px 6px 2px 0", color: "var(--ib-muted)" }}>{e.method}</td>
                <td style={{ padding: "2px 0" }}>
                  {typeof e.confidence === "number" ? e.confidence.toFixed(2) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div style={{ fontSize: 10, color: "var(--ib-faint)", marginTop: 6 }}>
        {prov.node_total ?? prov.nodes.length} node(s), {prov.edge_total ?? prov.edges.length}{" "}
        edge(s) found{prov.truncated ? " — list truncated for display" : ""}.
      </div>
    </div>
  );
}

function Bubble({ msg, streaming }: { msg: ChatMessage; streaming: boolean }) {
  const isUser = msg.role === "user";
  return (
    <div
      style={{
        alignSelf: isUser ? "flex-end" : "flex-start",
        maxWidth: isUser ? "70%" : "92%",
        width: isUser ? undefined : "92%",
        background: isUser ? "rgba(99,102,241,.14)" : "var(--ib-surface, rgba(255,255,255,.03))",
        border: `1px solid ${isUser ? "rgba(99,102,241,.28)" : "var(--ib-border)"}`,
        borderRadius: "var(--ib-radius-sm)",
        padding: "10px 13px",
        fontSize: 13,
        color: "var(--ib-text)",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}
      data-role={msg.role}
    >
      {msg.content || (streaming ? "…" : "")}
      {!isUser && <ToolStrip tools={msg.tools} />}
      {!isUser &&
        msg.provenance.map((p, i) => <ProvenanceBlock key={i} prov={p} />)}
    </div>
  );
}

function ErrorPanel({ err, onDismiss }: { err: ChatError; onDismiss: () => void }) {
  return (
    <EmptyState
      kind="error"
      title={err.message}
      action={{ label: "Dismiss", onClick: onDismiss }}
      hint={
        <div style={{ textAlign: "left" }}>
          <div style={{ fontSize: 11, color: "var(--ib-faint)", marginBottom: 6 }}>
            code: {err.code}
          </div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {(err.hints ?? []).map((h, i) => (
              <li key={i} style={{ marginBottom: 4 }}>
                {h}
              </li>
            ))}
          </ul>
        </div>
      }
    />
  );
}

export function Chat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<ChatError | null>(null);
  const threadIdRef = useRef<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streaming]);

  const send = useCallback(
    async (text: string) => {
      const question = text.trim();
      if (!question || streaming) return;
      setError(null);
      setInput("");
      setMessages((m) => [...m, newMessage("user", question), newMessage("assistant", "")]);
      setStreaming(true);

      /** Mutate only the trailing (assistant) message. */
      const patchLast = (fn: (m: ChatMessage) => ChatMessage) =>
        setMessages((m) => {
          const copy = m.slice();
          copy[copy.length - 1] = fn(copy[copy.length - 1]);
          return copy;
        });

      try {
        let buf = "";
        for await (const chunk of apiPostRawStream("/api/dashboard/chat", {
          message: question,
          thread_id: threadIdRef.current,
        })) {
          buf += chunk;
          const { frames, rest } = drainFrames(buf);
          buf = rest;
          for (const f of frames) {
            if (f.event === "meta" && typeof f.data.thread_id === "string") {
              threadIdRef.current = f.data.thread_id;
            } else if (f.event === "error") {
              setError(f.data as unknown as ChatError);
            } else if (f.event === "tool") {
              const call = { tool: String(f.data.tool), args: (f.data.args ?? {}) as Record<string, unknown> };
              patchLast((m) => ({ ...m, tools: [...m.tools, call] }));
            } else if (f.event === "provenance") {
              const prov = f.data.provenance as Provenance | undefined;
              if (prov) patchLast((m) => ({ ...m, provenance: [...m.provenance, prov] }));
            } else if (typeof f.data.token === "string") {
              const tok = f.data.token;
              patchLast((m) => ({ ...m, content: m.content + tok }));
            }
          }
        }
      } catch (e) {
        if (e instanceof AuthRequired) {
          redirectToLogin();
          return;
        }
        // The transport failed before any SSE frame arrived (the backend
        // classifies everything that happens *inside* the stream and sends an
        // `error` frame). ApiError(0) is our own "fetch itself threw" code.
        const status = e instanceof ApiError ? e.status : -1;
        setError({
          code: status === 0 ? "dashboard_unreachable" : "chat_request_failed",
          message:
            status === 0
              ? "Could not reach the Infra Brain API."
              : "The chat request failed before the assistant could answer.",
          hints: [
            status === 0
              ? "The dashboard could not open the request at all — check the API container is up."
              : `The API returned HTTP ${status}. Check the API container logs.`,
          ],
        });
      } finally {
        setStreaming(false);
      }
    },
    [streaming],
  );

  const reset = useCallback(() => {
    threadIdRef.current = null;
    setMessages([]);
    setError(null);
  }, []);

  return (
    <>
      <PageShell
        title="Ask Infra Brain"
        description="A read-only assistant over everything Infra Brain has collected — hosts, drift, CVEs, EOL, compliance, collection runs, the knowledge graph, and the internal documentation store. It answers by querying the database, not from memory."
        icon={<Icon name="brain" size={22} />}
        actions={
          <Button variant="secondary" size="sm" onClick={reset} disabled={messages.length === 0}>
            New conversation
          </Button>
        }
      />

      {error && <ErrorPanel err={error} onDismiss={() => setError(null)} />}

      {messages.length === 0 && !error && (
        <Panel
          rail="info"
          mnemonic="START"
          description="what can I ask?"
          ariaLabel="Starter questions"
        >
          <div style={{ fontSize: 12, color: "var(--ib-muted)", margin: "4px 0 14px" }}>
            Every example below maps to a query tool the assistant actually has. Click one to send
            it.
          </div>
          {STARTERS.map((g) => (
            <div key={g.group} style={{ marginBottom: 14 }}>
              <div
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  color: "var(--ib-muted)",
                  marginBottom: 6,
                  textTransform: "uppercase",
                  letterSpacing: ".04em",
                }}
              >
                {g.group}
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {g.items.map((s) => (
                  <Button key={s.text} variant="secondary" size="sm" onClick={() => send(s.text)}>
                    {s.text}
                  </Button>
                ))}
              </div>
            </div>
          ))}
          <div style={{ fontSize: 11, color: "var(--ib-faint)", marginTop: 4 }}>{PROPOSE_NOTE}</div>
        </Panel>
      )}

      {messages.length > 0 && (
        <Panel rail="info" mnemonic="CHAT" description="read-only assistant" ariaLabel="Conversation">
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 12,
              maxHeight: "58vh",
              overflowY: "auto",
              padding: "4px 2px",
            }}
          >
            {messages.map((m, i) => (
              <Bubble key={i} msg={m} streaming={streaming && i === messages.length - 1} />
            ))}
            <div ref={endRef} />
          </div>
        </Panel>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 14, alignItems: "flex-end" }}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(input);
            }
          }}
          rows={2}
          aria-label="Ask a question"
          placeholder="Ask about hosts, drift, CVEs, EOL, compliance, or the knowledge graph…  (Enter to send, Shift+Enter for a new line)"
          style={{
            flex: 1,
            background: "var(--ib-raised)",
            border: "1px solid var(--ib-border)",
            borderRadius: "var(--ib-radius-sm)",
            padding: "10px 12px",
            color: "var(--ib-text)",
            fontSize: 13,
            fontFamily: "inherit",
            resize: "vertical",
            outline: "none",
          }}
        />
        <Button variant="primary" onClick={() => send(input)} loading={streaming}>
          {streaming ? "Thinking" : "Send"}
        </Button>
      </div>
    </>
  );
}
