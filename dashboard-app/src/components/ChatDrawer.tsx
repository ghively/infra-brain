import { useRef, useState } from "react";
import { AuthRequired, apiPostRawStream, redirectToLogin } from "../api";
import { useFocusTrap } from "../hooks/useFocusTrap";

/** Chat drawer — persistent shell element (mounted once in App.tsx outside
 * <Routes>, not a routed page), ported from the legacy chat panel (DR-6.1 / B3).
 *
 * MR-I a11y fix (residual gap noted in FRONTEND_UX_AUDIT_2026-07-06: "the
 * React port still lacks drawer focus management" even after the DC shell's
 * own a11y defects, FE-14, were retired): opening the drawer now traps
 * keyboard focus inside it (Tab/Shift+Tab cycle within the panel, Escape
 * closes it) and closing it — by Escape or the × button — returns focus to
 * the floating toggle button, via `useFocusTrap`.
 *
 * Legacy source: src/infra_brain/dashboard/static/index.html:5715-5773
 * (`sendChat`) for the streaming client, and
 * src/infra_brain/api/routers/ui.py:76-111 (`_chat_sse` / `POST /api/dashboard/chat`)
 * for the backend contract.
 *
 * DEVIATION FROM THE TASK'S SUGGESTED MECHANISM (documented deliberately,
 * not a guess): the task asked for the browser's native `EventSource` API.
 * `EventSource` can only issue GET requests with no body and no custom
 * headers. The actual backend endpoint is `POST /api/dashboard/chat` with a
 * JSON body (`{message, thread_id}`) — EventSource is structurally incapable
 * of driving it. The legacy dashboard itself does NOT use EventSource either;
 * it uses `fetch()` + a manual `ReadableStream` reader that parses
 * `data: {...}\n\n` frames out of the POST response body (index.html:5731-5763).
 * This component ports that exact mechanism instead, via the `apiPostRawStream`
 * chokepoint helper in `../api` (no raw `fetch()` here) — this component only
 * does its own SSE frame-splitting on top of the decoded text chunks it yields.
 *
 * SIMPLIFICATION: the legacy chat keeps a running `chatMessages` array with
 * a seeded assistant greeting and preserves the full conversation across
 * page navigations (component-level state on the single-page-app root). This
 * port keeps full message history for the lifetime of the mounted drawer
 * (matches the persistent-across-routes requirement) but does not seed a
 * greeting message and does not persist across a full page reload — the
 * legacy app didn't persist across reloads either (state is in-memory only
 * in both versions), so behavior is equivalent for the common case.
 */

type ChatMessage = { role: "user" | "assistant"; content: string };

export function ChatDrawer() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [typing, setTyping] = useState(false);
  const threadIdRef = useRef<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const toggleBtnRef = useRef<HTMLButtonElement>(null);
  useFocusTrap(open, panelRef, { restoreFocusRef: toggleBtnRef, onEscape: () => setOpen(false) });

  async function sendChat() {
    const inp = input.trim();
    if (!inp) return;
    setMessages((m) => [...m, { role: "user", content: inp }, { role: "assistant", content: "" }]);
    setInput("");
    setTyping(true);

    const appendToken = (tok: string) =>
      setMessages((m) => {
        const copy = m.slice();
        const last = copy.length - 1;
        copy[last] = { ...copy[last], content: copy[last].content + tok };
        return copy;
      });

    try {
      let buf = "";
      for await (const chunk of apiPostRawStream("/api/dashboard/chat", {
        message: inp,
        thread_id: threadIdRef.current,
      })) {
        buf += chunk;
        // SSE frames are separated by a blank line.
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
          if (!dataLine) continue;
          let payload: { thread_id?: string; token?: string };
          try {
            payload = JSON.parse(dataLine.slice(5).trim());
          } catch {
            continue;
          }
          if (payload.thread_id) threadIdRef.current = payload.thread_id;
          if (payload.token) appendToken(payload.token);
        }
      }
      setMessages((m) => {
        const copy = m.slice();
        const last = copy.length - 1;
        if (!copy[last].content) copy[last] = { ...copy[last], content: "(no reply)" };
        return copy;
      });
    } catch (e) {
      if (e instanceof AuthRequired) {
        redirectToLogin();
        return;
      }
      setMessages((m) => {
        const copy = m.slice();
        const last = copy.length - 1;
        copy[last] = { ...copy[last], content: "Sorry — the assistant is unavailable right now." };
        return copy;
      });
    } finally {
      setTyping(false);
    }
  }

  if (!open) {
    return (
      <button
        ref={toggleBtnRef}
        onClick={() => setOpen(true)}
        style={{
          position: "fixed",
          right: 20,
          bottom: 20,
          zIndex: 1000,
          borderRadius: "50%",
          width: 52,
          height: 52,
          background: "linear-gradient(135deg,#6366f1,#7c3aed)",
          border: "none",
          color: "#fff",
          fontSize: 20,
          cursor: "pointer",
          boxShadow: "0 8px 24px rgba(0,0,0,.35)",
        }}
        aria-label="Open chat"
      >
        💬
      </button>
    );
  }

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-modal="true"
      aria-label="Assistant chat"
      style={{
        position: "fixed",
        right: 20,
        bottom: 20,
        zIndex: 1000,
        width: 340,
        maxHeight: 480,
        display: "flex",
        flexDirection: "column",
        background: "#0b0f1c",
        border: "1px solid rgba(255,255,255,.08)",
        borderRadius: 14,
        boxShadow: "0 10px 40px rgba(0,0,0,.5)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "10px 14px",
          borderBottom: "1px solid rgba(255,255,255,.06)",
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 600, color: "#eef2ff" }}>Ask Infra Brain</span>
        <button
          onClick={() => setOpen(false)}
          style={{ background: "none", border: "none", color: "#94a3b8", cursor: "pointer", fontSize: 16 }}
          aria-label="Close chat"
        >
          ×
        </button>
      </div>
      <div style={{ flex: 1, overflowY: "auto", padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
        {messages.length === 0 && (
          <div style={{ fontSize: 12, color: "#374569" }}>
            Ask me anything about your infrastructure — resources, drift, CVEs, runs, and more.
          </div>
        )}
        {messages.map((m, i) => (
          <div
            key={i}
            style={{
              alignSelf: m.role === "user" ? "flex-end" : "flex-start",
              maxWidth: "85%",
              background: m.role === "user" ? "rgba(99,102,241,.14)" : "rgba(255,255,255,.04)",
              border: `1px solid ${m.role === "user" ? "rgba(99,102,241,.25)" : "rgba(255,255,255,.05)"}`,
              color: m.role === "user" ? "#c7d2fe" : "#94a3b8",
              borderRadius: 10,
              padding: "8px 10px",
              fontSize: 12,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {m.content || (typing && i === messages.length - 1 ? "…" : "")}
          </div>
        ))}
      </div>
      <div style={{ display: "flex", gap: 8, padding: 10, borderTop: "1px solid rgba(255,255,255,.06)" }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !typing) sendChat();
          }}
          placeholder="Ask a question..."
          style={{
            flex: 1,
            background: "#111827",
            border: "1px solid #1e2d47",
            borderRadius: 8,
            padding: "8px 10px",
            color: "#d4dcf0",
            fontSize: 12,
            outline: "none",
          }}
        />
        <button
          onClick={sendChat}
          disabled={typing}
          style={{
            padding: "8px 14px",
            borderRadius: 8,
            background: "#6366f1",
            border: "none",
            color: "#fff",
            fontSize: 12,
            fontWeight: 600,
            cursor: typing ? "default" : "pointer",
            opacity: typing ? 0.6 : 1,
          }}
        >
          Send
        </button>
      </div>
    </div>
  );
}
