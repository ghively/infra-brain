/**
 * Page header metadata, ported VERBATIM from the v1 index.html HEADERS map
 * (title / color / icon / desc). The `icon` field is the v1 icon-name string
 * (see `ICON_PATHS` in `lib/icons.tsx`); pages can derive their header icon
 * from it via `<Icon name={headerFor(key).icon} />`.
 */

export type HeaderEntry = { title: string; color: string; icon: string; desc: string };

export const HEADERS = {
  home: {
    title: "Dashboard",
    color: "#818cf8",
    icon: "home",
    desc: "A real-time view of your infrastructure-intelligence pipeline — from scheduled collection, through drift detection, to autonomous learning. Everything Infra Brain does is strictly read-only.",
  },
  resources: {
    title: "Hosts / Resources",
    color: "#34d399",
    icon: "resources",
    desc: "Every host and asset Infra Brain has discovered across your fleet. Each resource is collected read-only by a domain agent and snapshotted on every sweep. Use domain chips to filter by collector type; paginate with Prev/Next. Click any row to inspect its facts, snapshot history, and drift.",
  },
  drift: {
    title: "Drift Events",
    color: "#c084fc",
    icon: "drift",
    desc: "Changes detected by diffing consecutive snapshots. A config-drift means a field changed; a state-drift means a tracked asset disappeared. Drift can open a Jira ticket automatically.",
  },
  collruns: {
    title: "Collection Runs",
    color: "#60a5fa",
    icon: "collruns",
    desc: "Every sweep executed by a domain agent — scheduled by cron or triggered by webhook. Each run snapshots resources and feeds the drift detector downstream.",
  },
  scan: {
    title: "Scan Schedule",
    color: "#fbbf24",
    icon: "scan",
    desc: "The configured scan points that drive collection. Each domain has a cron schedule and a read-only endpoint. Trigger a manual sweep below to collect on demand.",
  },
  instincts: {
    title: "Instincts",
    color: "#a78bfa",
    icon: "instincts",
    desc: "Operational patterns the system has learned from its own drift history. Instincts at confidence ≥ 0.70 are automatically applied to prime future agent reasoning — this is the memory of the self-improvement loop.",
  },
  scripts: {
    title: "Generated Scripts",
    color: "#818cf8",
    icon: "scripts",
    desc: "Automation the agents wrote for themselves during the self-improvement loop. Reusable scripts are read back to accelerate future tasks. Nothing here was hand-written.",
  },
  intprops: {
    title: "Integration Proposals",
    color: "#60a5fa",
    icon: "intprops",
    desc: "New integrations the Discovery agent found by cross-referencing your sources. Proposals at confidence ≥ 0.70 can be approved to wire them in; anything lower stays in review.",
  },
  activity: {
    title: "Agent Activity",
    color: "#34d399",
    icon: "activity",
    desc: "Every tool call made by every agent, with the ReadOnlyToolValidator’s verdict. Denied rows show the safety layer actively blocking a mutating or sensitive call — the read-only guarantee in action.",
  },
  decisions: {
    title: "Agent Decisions",
    color: "#c084fc",
    icon: "decisions",
    desc: "The reasoning behind autonomous behavior, grouped into sessions. Each session is one agent run; each step is an iteration of its LLM tool-calling loop — what it reasoned, what it decided, and which tools it chose.",
  },
  observations: {
    title: "Observations",
    color: "#60a5fa",
    icon: "observations",
    desc: "Tool-usage patterns recorded by the ObservationCallbackHandler on every call. Together with Instincts and Generated Scripts, these prime future agent reasoning — the raw signal feeding the self-improvement loop.",
  },
  settings: {
    title: "Settings",
    color: "#94a3b8",
    icon: "settings",
    desc: "Runtime configuration read by config.py from environment variables. Secrets are injected at startup from Bitwarden Secrets Manager and shown masked. This view is read-only except where noted.",
  },
  agents: {
    title: "Agents",
    color: "#818cf8",
    icon: "agents",
    desc: "The full roster of agents. Deterministic collectors gather read-only data per domain; LLM agents reason over read-only tools; system agents run drift detection, notification, and the learning loop. Each is dispatched by the LangGraph supervisor.",
  },
  notifications: {
    title: "Notifications",
    color: "#fbbf24",
    icon: "notify",
    desc: "Outputs of the Notify stage. When drift is detected, the NotificationAgent opens Jira tickets and updates Confluence documentation pages — the only systems Infra Brain writes to besides its own database and GitLab MRs.",
  },
  vulns: {
    title: "Vulnerabilities",
    color: "#f87171",
    icon: "vulns",
    desc: "The CVE queue assembled by the vuln_triage agent from whichever vulnerability sources are live in this environment. Each finding is scored by CVSS and tracked against a remediation SLA; critical and overdue items are surfaced first. (Previously described as sourced from Rapid7 InsightVM — that collector is retired here and holds no data.)",
  },
  eol: {
    title: "EOL Registry",
    color: "#fbbf24",
    icon: "eolcal",
    desc: "Assets past or approaching end-of-life, tracked by the EOL agent against endoflife.date. Each carries a PCI risk score and a migration path. Overdue items are a compliance liability.",
  },
  security: {
    title: "Security & Audit",
    color: "#34d399",
    icon: "security",
    desc: "The immutable audit trail written by the safety callback layer. The ReadOnlyToolValidator denies any mutating call, the DLP handler suppresses output containing card data, and every tool call is hashed and logged. Denials and DLP blocks are surfaced first.",
  },
  mcpkeys: {
    title: "MCP API Keys",
    color: "#818cf8",
    icon: "security",
    desc: "Scoped API keys for the MCP server — create, review, revoke. Each key is scoped to a set of MCP tool names; the raw token is shown exactly once at creation and only its hash is stored. Revoked keys are kept for the audit trail.",
  },
  netdiscovery: {
    title: "Network Discovery",
    color: "#f87171",
    icon: "netscan",
    desc: "Hosts and services discovered by active network scanning (nmap-based, read-only per docs/READONLY-MODEL.md), independent of any agent-reported inventory. Flags shadow-IT (devices with no known source of truth) and threat level per discovered service. This is infra-brain's unmanaged-device detection capability.",
  },
  invrec: {
    title: "Inventory Reconcile",
    color: "#34d399",
    icon: "invrec",
    desc: "Hosts discovered in real infrastructure but missing from the GitLab Ansible inventory — the source of truth. Each is proposed as an add-only merge request; humans merge. The system never deletes inventory entries.",
  },
  remed: {
    title: "Remediation",
    color: "#f87171",
    icon: "remed",
    desc: "Fixes proposed for open configuration drift. Nothing is applied automatically — each proposal needs human approval; on approval a remediation merge request is opened. Propose, never dispose.",
  },
  compl: {
    title: "Compliance",
    color: "#fbbf24",
    icon: "compl",
    desc: "Policy-as-code violations: assets past end-of-life, vulnerabilities past their remediation SLA, and drift left unresolved too long. Especially relevant to PCI scope.",
  },
  agentConfig: {
    title: "Agent Configuration",
    color: "#94a3b8",
    icon: "settings",
    desc: "Configure domain agents and view last run status. Set required environment variables per domain and trigger manual collection sweeps.",
  },
  iac: {
    title: "IaC / Pipelines",
    color: "#818cf8",
    icon: "iac",
    desc: "The GitLab projects Infra Brain has collected, with their parsed infrastructure-as-code files and recent CI/CD pipeline runs — all read-only. Each project shows its default branch, last-pipeline status, and a breakdown of the IaC files (CI definitions, playbooks, inventories, compose, manifests, terraform) found inside it.",
  },
  homelab: {
    title: "Homelab Services",
    color: "#34d399",
    icon: "box",
    desc: "Home-lab services from homelab_services_manifest.json, probed read-only by HomelabServicesAgent and grouped by category — media stack, AI inference, observability, security/SIEM, storage, and more. Each row is a single GET reachability check; services without a confirmed URL in the manifest are intentionally excluded, not missing data.",
  },
  /* REMOVED header entries: `vsphere`, `cloud`, `fleet`, `software`, `r7detail`.
   * Their pages were deleted — each read only a retired collector's tables, all
   * of which are empty (see RETIRED_SOURCES in src/nav.ts). Nothing calls
   * headerFor() with these keys any more; the routes now render
   * pages/SourceUnavailable.tsx, which builds its own header from nav.ts. */
  customviews: {
    title: "Custom Views",
    color: "#a78bfa",
    icon: "sparkles",
    desc: "Create on-demand dashboard views using natural language. Infra Brain generates a live component from your prompt, powered by your real infrastructure data.",
  },
  savedviews: {
    title: "Saved Views",
    color: "#818cf8",
    icon: "sparkles",
    desc: "Your saved custom views. Share a link or revisit any view you have built.",
  },
  graph: {
    title: "Knowledge Graph",
    color: "#6366f1",
    icon: "graph",
    desc: "The infrastructure knowledge graph — resource nodes and the typed edges connecting them across every collector domain. Search by host name to visualize its neighborhood; click relationship types to explore edges.",
  },
  documents: {
    title: "Knowledge Store",
    color: "#a78bfa",
    icon: "docs",
    desc: "The RAG knowledge store's health — every indexed document with its source, sensitivity classification, freshness, and chunk count. This is a browse/inspect view, not semantic search; use the chat panel for that. Select a document to see its full metadata and chunk previews.",
  },
} as const satisfies Record<string, HeaderEntry>;

export type PageKey = keyof typeof HEADERS;

/**
 * Look up a header entry by page-key, falling back to `home` when the key is
 * unknown. Mirrors v1's `HEADERS[pg]||HEADERS.home`.
 */
export function headerFor(key: string): HeaderEntry {
  return (HEADERS as Record<string, HeaderEntry>)[key] ?? HEADERS.home;
}
