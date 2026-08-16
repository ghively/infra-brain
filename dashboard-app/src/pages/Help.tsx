/** Help / Getting Started — T4-onboarding.
 *
 *  Written for the exact gap this page exists to close: the owner logging
 *  into this dashboard for the first time and saying (verbatim) "I have
 *  never gotten LLMs attached to this before so I dont exactly know how it
 *  works or how to operate this board." Nothing here is aspirational —
 *  every claim below is traceable to a source file read while writing this
 *  page (cited inline in the section comments) or to `docs/READONLY-MODEL.md`.
 *  Where a fact could not be verified from code, it was left out rather than
 *  guessed (see the section comments for the specific omissions).
 *
 *  This is a static content page — no API calls, so no loading/error state.
 *  It reuses the same `PageShell`/`Panel` primitives every other page uses
 *  rather than inventing a docs-page layout, per DESIGN.md's "one visual
 *  system" rule.
 *
 *  NOT wired into `nav.ts` or `App.tsx`'s route table — those are owned by a
 *  sibling task in this same work session (T4-onboarding's HARD RULES).
 *  Needed nav entry (for whoever does own nav.ts): an `Overview` or `System`
 *  section item, e.g. `{ label: "Help", path: "/help" }`, and a route
 *  `<Route path="/help" element={<Help />} />` in App.tsx importing
 *  `{ Help } from "./pages/Help"`.
 */
import { Link } from "react-router-dom";
import { Panel } from "../components/ui/Panel";
import { PageShell } from "../components/ui/PageShell";
import { Badge } from "../components/ui/SeverityPill";

const kv: React.CSSProperties = {
  display: "flex",
  gap: 10,
  padding: "9px 0",
  borderBottom: "1px solid var(--ib-border)",
  fontSize: 12.5,
  lineHeight: 1.55,
};
const kvLabel: React.CSSProperties = {
  flex: "0 0 200px",
  color: "var(--ib-text)",
  fontWeight: 600,
};
const kvValue: React.CSSProperties = { color: "var(--ib-muted)", flex: 1 };
const p: React.CSSProperties = { fontSize: 13, lineHeight: 1.65, color: "var(--ib-muted)", margin: "0 0 12px" };
const h3: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 700,
  color: "var(--ib-text)",
  textTransform: "uppercase",
  letterSpacing: ".06em",
  margin: "18px 0 8px",
};
const linkStyle: React.CSSProperties = { color: "var(--ib-blue)", fontWeight: 600 };

export function Help() {
  return (
    <main>
      <PageShell
        icon="🧭"
        title="Help / Getting Started"
        description="What infra-brain actually does today, in plain language — grounded in the code, not a sales pitch. Written for someone operating this for the first time."
      />

      {/* ── 1. What is this, and the read-only guarantee ─────────────────── */}
      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="WHAT IS THIS" description="the one-paragraph version" rail="info">
          <p style={p}>
            infra-brain watches your homelab and tells you what changed, what&apos;s stale, and what
            needs attention — it never touches your infrastructure. A set of scheduled collectors
            reads your systems on a cadence, stores what they find, and a few downstream passes
            (drift detection, EOL tracking, compliance rules) turn that raw data into the findings
            you see on this dashboard.
          </p>
          <h3 style={h3}>The read-only guarantee</h3>
          <p style={p}>
            infra-brain can never change your infrastructure. This isn&apos;t a policy promise — it&apos;s
            enforced in three separate layers (see <code>docs/READONLY-MODEL.md</code>), so a bug in
            any one layer doesn&apos;t remove the guarantee:
          </p>
          <div style={kv}>
            <div style={kvLabel}>1. Structural</div>
            <div style={kvValue}>
              Most collectors hold HTTP clients that can only issue GET requests — a non-GET call
              raises an error before it leaves the process. The natural-language query agent runs
              against a database role that can only SELECT, backed by a query-shape guard on top.
            </div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>2. Boundary gate</div>
            <div style={kvValue}>
              Every tool call an LLM agent wants to make is checked <em>before</em> it runs; a
              disallowed call is blocked, not just logged after the fact. The only writes infra-brain
              is ever allowed to make are to its own database, or — as a sanctioned exception — a
              GitLab merge request, a Jira ticket, or a Confluence page, each scanned for sensitive
              data before it goes out.
            </div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>3. Audit</div>
            <div style={kvValue}>
              Every tool call, allowed or denied, is recorded. See the{" "}
              <Link to="/activity" style={linkStyle}>
                Agent Activity
              </Link>{" "}
              page for the live feed and the{" "}
              <Link to="/security" style={linkStyle}>
                Security &amp; Audit
              </Link>{" "}
              page for denials specifically.
            </div>
          </div>
          <p style={{ ...p, marginTop: 12, marginBottom: 0 }}>
            A few sources (vSphere, Kubernetes, and the network-discovery scanner) can&apos;t be made
            GET-only at the protocol level, so they&apos;re read-only <em>by convention</em> instead —
            code review plus an audited call log, rather than a structural block. This is documented
            honestly, not hidden: see the &quot;Read-only by CONVENTION&quot; table in{" "}
            <code>docs/READONLY-MODEL.md</code> for exactly which sources and why.
          </p>
        </Panel>
      </div>

      {/* ── 2. How collection works ───────────────────────────────────────── */}
      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="COLLECTION" description="schedule → Postgres → derived findings" rail="info">
          <p style={p}>
            Each data source (Linux hosts, GitLab, DNS, EOL registries, and so on) has its own
            collector agent with its own cron schedule. On its turn, a collector reads its source
            read-only, writes what it found into Postgres as a snapshot, and finishes. Nothing on
            this dashboard is computed live from your infrastructure at page-load time — every
            number you see comes from the most recent snapshot in the database.
          </p>
          <p style={p}>
            Three things are derived from those snapshots after the fact, not collected directly:
          </p>
          <div style={kv}>
            <div style={kvLabel}>Drift</div>
            <div style={kvValue}>
              After a collector finishes, its new snapshot is diffed against the previous one. A
              changed field is a drift event. See{" "}
              <Link to="/drift" style={linkStyle}>
                Drift Events
              </Link>
              .
            </div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>EOL</div>
            <div style={kvValue}>
              Collected OS/software versions are checked against endoflife.date. See{" "}
              <Link to="/eol" style={linkStyle}>
                EOL Registry
              </Link>
              .
            </div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>Compliance</div>
            <div style={kvValue}>
              A small set of deterministic rules run over already-collected inventory — overdue EOL
              assets, vulnerabilities past their SLA, and drift left open too long. Nothing here is
              an LLM guessing; it&apos;s fixed rules over real rows. See{" "}
              <Link to="/compl" style={linkStyle}>
                Compliance
              </Link>
              .
            </div>
          </div>

          <h3 style={h3}>Where to see a sweep run, and what a failed one looks like</h3>
          <p style={p}>
            <Link to="/collruns" style={linkStyle}>
              Collection Runs
            </Link>{" "}
            lists every collector run, one row per domain per run, each with a status. A run that
            says <Badge tone="err">failure</Badge> or <Badge tone="err">retry_exhausted</Badge> means
            that collector hit an error and gave up after retrying — check the run&apos;s error message
            there first.{" "}
            <Link to="/scanschedule" style={linkStyle}>
              Scan Schedule
            </Link>{" "}
            shows each domain&apos;s configured cron and lets you trigger a manual run on demand.
          </p>
          <p style={p}>
            <Link to="/sweeps" style={linkStyle}>
              Sweeps
            </Link>{" "}
            groups runs by a shared <code>sweep_id</code> for a per-tier (collector / reconciler /
            reasoner) view of one coordinated pass — but that grouping only exists when the
            experimental sweep-graph orchestrator is turned on
            (<code>sweep_graph_enabled</code>, off by default in this codebase). On a stock install
            like this one, collection instead runs through the classic per-domain dispatch path, so
            the Sweeps page will read empty even though collection itself is working fine —{" "}
            <strong>Collection Runs is the page that always has data</strong>; Sweeps is the
            richer view for when that flag is on.
          </p>
        </Panel>
      </div>

      {/* ── 3. Knowledge graph ────────────────────────────────────────────── */}
      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="KNOWLEDGE GRAPH" description="why it's the core, not a side feature" rail="info">
          <p style={p}>
            Every other view on this dashboard is one collector&apos;s slice of the world — Linux hosts
            from the Linux collector, GitLab projects from the GitLab collector, and so on. The
            knowledge graph is where those slices are stitched into one picture: <strong>nodes</strong>{" "}
            are entities (a host, a service, a GitLab project, a file), and <strong>edges</strong> are
            typed relationships between them, each carrying who/what asserted it and how confident
            that assertion is.
          </p>
          <p style={p}>
            The edge type that matters most for a homelab with several collectors watching the same
            boxes is <code>SAME_AS</code> — it&apos;s how infra-brain knows that the host your Linux
            collector calls <code>node_a</code> and the host your GitLab inventory calls something
            slightly different are the <em>same physical machine</em>, rather than two unrelated
            entries. Without that stitching, the same host would silently show up as several
            disconnected records instead of one.
          </p>
          <p style={p}>
            The graph is populated by a dedicated maintenance pass that runs on its own schedule
            (every two hours) independent of any other collector — see the{" "}
            <Link to="/graph" style={linkStyle}>
              Knowledge Graph
            </Link>{" "}
            page&apos;s Explorer tab to search for a host and see its neighborhood, or the Review Queue
            tab for identity matches that need a human decision.
          </p>
        </Panel>
      </div>

      {/* ── 4. How the LLM fits ───────────────────────────────────────────── */}
      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="LLM" description="which flags are on, which are off, and why" rail="info">
          <p style={p}>
            infra-brain uses an LLM in a few specific, opt-in places — it is not an LLM freely poking
            at your infrastructure. Every LLM tool call still goes through the same read-only
            boundary gate and audit chain described above. Here is the actual on/off state of every
            LLM-gated feature flag in this codebase (from <code>config.py</code> — this is the ground
            truth, not a claim):
          </p>
          <div style={kv}>
            <div style={kvLabel}>
              <code>rag_enabled</code> <Badge tone="neutral">off</Badge>
            </div>
            <div style={kvValue}>
              Retrieval over ingested documents (Confluence pages, etc.) via the{" "}
              <Link to="/documents" style={linkStyle}>
                Knowledge Store
              </Link>
              . Off by default in both <code>config.py</code> and <code>.env.example</code> — with it
              off, the document-ingest agent and the search tool are both fully inert (no-op), not
              partially working.
            </div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>
              <code>rootcause_llm_enabled</code> <Badge tone="neutral">off</Badge>
            </div>
            <div style={kvValue}>
              Would let an LLM reason over unexplained drift events to suggest a root cause. Off by
              default; the codebase notes this needs a real-model smoke test before enabling against
              live data.
            </div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>
              <code>compliance_gap_finder_enabled</code> <Badge tone="neutral">off</Badge>
            </div>
            <div style={kvValue}>
              Would let an LLM propose <em>new</em> compliance rules beyond the four fixed ones
              (EOL overdue, vuln SLA breach, stale drift, unaddressed critical CVE) that already run
              deterministically today regardless of this flag. Off by default.
            </div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>
              <code>remediation_interrupt_enabled</code> <Badge tone="neutral">off</Badge>
            </div>
            <div style={kvValue}>
              Would add a human-in-the-loop step to the remediation flow. Off by default; the
              codebase notes it additionally needs a real (non-in-memory) checkpointer before it&apos;s
              safe to turn on.
            </div>
          </div>
          <p style={{ ...p, marginTop: 12 }}>
            A Chat page is planned as the interactive front door for asking this data questions in
            natural language — it does not exist in this build yet, so there is nothing to link to
            here today. Until it lands, every LLM-touched feature above is reached through its own
            page (Knowledge Store, Drift, Compliance, Remediation), not a chat box.
          </p>
        </Panel>
      </div>

      {/* ── 5. Agent roster ───────────────────────────────────────────────── */}
      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="AGENTS" description="the full roster, and what 'retired' means" rail="info">
          <p style={p}>
            The <Link to="/agents" style={linkStyle}>
              Agents
            </Link>{" "}
            page lists every agent infra-brain knows about, with its kind (collector, LLM, or system
            agent), its last run status, and a drill-in for its recent tool calls and decisions.
            Status reads as one of a small honest vocabulary:
          </p>
          <div style={kv}>
            <div style={kvLabel}>
              <Badge tone="ok">healthy</Badge>
            </div>
            <div style={kvValue}>Ran recently and succeeded.</div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>
              <Badge tone="warn">degraded / partial</Badge>
            </div>
            <div style={kvValue}>Ran, but something about the run wasn&apos;t fully clean.</div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>
              <Badge tone="info">skipped</Badge>
            </div>
            <div style={kvValue}>
              A required setting (credentials, URL) was never configured — go configure it if you
              want this source. This is the status you&apos;ll see for anything this homelab genuinely
              doesn&apos;t have set up.
            </div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>
              <Badge tone="neutral">idle</Badge>
            </div>
            <div style={kvValue}>Configured and scheduled, but hasn&apos;t run yet.</div>
          </div>
          <div style={kv}>
            <div style={kvLabel}>
              <Badge tone="neutral">retired</Badge>
            </div>
            <div style={kvValue}>
              Switched off by a deliberate, standing decision — not a missing config, not an error.
              <code> cloud</code>, <code>k8s</code>, and <code>windows</code> carry this status in the
              current codebase (retired 2026-08-12). A retired domain stays visible in the roster
              rather than disappearing, so &quot;we chose not to run this&quot; reads differently from
              &quot;this is broken&quot; — re-enabling one needs a config change, not a code change.
            </div>
          </div>
          <p style={{ ...p, marginTop: 12 }}>
            Several other collectors in the roster — Rapid7, vSphere, Octopus — aren&apos;t retired,
            but their required URL/credential settings default to empty in this codebase, so they
            read <Badge tone="info">skipped</Badge> until configured. That&apos;s why the{" "}
            <Link to="/vulns" style={linkStyle}>
              Vulnerabilities
            </Link>
            , <code>Fleet</code>, and <code>Software Inventory</code> views (all Rapid7-backed) and
            the <Link to="/vsphere" style={linkStyle}>
              vSphere
            </Link>{" "}
            page are likely to show &quot;not configured&quot; empty states on a fresh homelab install
            rather than data — that is expected, not a bug.
          </p>
        </Panel>
      </div>

      {/* ── 6. Secrets ─────────────────────────────────────────────────────── */}
      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="SECRETS" description="where they live, and why the UI won't show them" rail="info">
          <p style={p}>
            Almost every secret infra-brain uses — API keys, passwords, tokens for the sources it
            collects from — lives in Bitwarden Secrets Manager and is pulled into the process at
            startup, never stored in this app&apos;s own database or shown unmasked in the UI. The one
            exception is the Bitwarden access token itself, which has to live somewhere outside
            Bitwarden to bootstrap that pull — it&apos;s set as a Kubernetes secret / Docker environment
            variable instead. Local development secrets go in a git-ignored <code>.env</code> file;
            CI-only credentials live as GitLab CI/CD variables. The{" "}
            <Link to="/settings" style={linkStyle}>
              Settings
            </Link>{" "}
            page shows the resulting runtime configuration with secret values masked — you can see{" "}
            <em>that</em> something is configured, never the value.
          </p>
        </Panel>
      </div>
    </main>
  );
}
