import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { login, AuthRequired, ApiError } from "../api";
import { Panel, Button } from "../components/ui";

/** Login page for the Wave 6.1 Vite+React dashboard (F14). Every page's
 *  AuthRequired (401) handler redirects here via redirectToLogin() with a
 *  ?next=<original path> query param; on successful sign-in we return the
 *  user to that path. Without this route the ~30 AuthRequired redirects hit a
 *  dead end (no /login route existed in App.tsx).
 *
 *  Targets POST /api/dashboard/login (dashboard_auth.py:207): body
 *  {username,password}, response LoginOut {authenticated,dev_mode,username}.
 *  Bad credentials → 401 (AuthRequired); lockout → 429 (ApiError). Both are
 *  shown inline rather than triggering a further redirect. Mirrors the legacy
 *  DC-shell login overlay (dashboard/static/index.html:3876-3888). */

/** Only allow returning to a same-origin absolute path — never a
 *  protocol-relative ("//evil.com") or absolute URL — to avoid open redirect.
 *  Also rejects a target that is itself /dashboard2/login (exported for
 *  Login.test.tsx): a next= pointing back to login is a dead-end redirect on
 *  successful sign-in, not a loop — the browser lands back on the login form
 *  with no further error, indistinguishable from "nothing happened". This can
 *  happen with a stale next= left over from before the redirectToLogin()
 *  no-op guard (api.ts) existed. */
export function safeNext(raw: string | null): string {
  const fallback = "/dashboard2/";
  if (!raw) {
    // No ?next — try the referrer if it is same-origin, else the dashboard root.
    try {
      if (document.referrer) {
        const ref = new URL(document.referrer);
        if (ref.origin === window.location.origin && !ref.pathname.startsWith("/dashboard2/login")) {
          return ref.pathname + ref.search + ref.hash;
        }
      }
    } catch {
      return fallback;
    }
    return fallback;
  }
  if (raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/dashboard2/login")) {
    return raw;
  }
  return fallback;
}

export function Login() {
  const [params] = useSearchParams();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function onSubmit(ev: React.FormEvent) {
    ev.preventDefault();
    setError(null);
    setBusy(true);
    login(username, password)
      .then(() => {
        window.location.assign(safeNext(params.get("next")));
      })
      .catch((e) => {
        if (e instanceof AuthRequired) setError("Invalid username or password.");
        else if (e instanceof ApiError && e.status === 429)
          setError("Too many failed attempts — please try again later.");
        else if (e instanceof ApiError && e.status === 0) setError("Could not reach the server.");
        else setError(String(e));
        setBusy(false);
      });
  }

  return (
    <div
      style={{
        minHeight: "70vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div style={{ width: "100%", maxWidth: 380 }}>
        <Panel rail="info" mnemonic="AUTH" description="infra-brain dashboard">
          <form onSubmit={onSubmit} style={{ padding: 16 }}>
            <label
              htmlFor="login-username"
              style={{ display: "block", fontSize: 11, color: "var(--ib-muted)", marginBottom: 6 }}
            >
              Username
            </label>
            <input
              id="login-username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              autoComplete="username"
              style={{
                width: "100%",
                boxSizing: "border-box",
                background: "var(--ib-raised)",
                border: "1px solid var(--ib-border)",
                borderRadius: "var(--ib-radius-sm)",
                padding: "10px 12px",
                fontSize: 13,
                color: "var(--ib-text)",
                marginBottom: 16,
                fontFamily: "inherit",
              }}
            />

            <label
              htmlFor="login-password"
              style={{ display: "block", fontSize: 11, color: "var(--ib-muted)", marginBottom: 6 }}
            >
              Password
            </label>
            <input
              id="login-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              style={{
                width: "100%",
                boxSizing: "border-box",
                background: "var(--ib-raised)",
                border: "1px solid var(--ib-border)",
                borderRadius: "var(--ib-radius-sm)",
                padding: "10px 12px",
                fontSize: 13,
                color: "var(--ib-text)",
                marginBottom: 20,
                fontFamily: "inherit",
              }}
            />

            {error && (
              <div
                role="alert"
                style={{
                  fontSize: 12,
                  color: "var(--ib-red)",
                  background: "rgba(240,101,78,.08)",
                  border: "1px solid rgba(240,101,78,.22)",
                  borderRadius: "var(--ib-radius-sm)",
                  padding: "9px 12px",
                  marginBottom: 16,
                }}
              >
                {error}
              </div>
            )}

            <Button type="submit" variant="primary" loading={busy} style={{ width: "100%" }}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </Panel>
      </div>
    </div>
  );
}
