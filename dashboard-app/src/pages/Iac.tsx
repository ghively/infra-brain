import { useCallback, useEffect, useState } from "react";
import { apiGet, AuthRequired, redirectToLogin } from "../api";
import { Icon } from "../lib/icons";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { Skeleton } from "../components/Skeleton";
import { fmtDt } from "../lib/fmtDt";
import {
  PageShell,
  Panel,
  DataTable,
  Tabs,
  Badge,
  EmptyState,
  FkCell,
  type ColumnDef,
} from "../components/ui";
import { statusColor } from "../lib/tokens";

type IacPipelineRun = {
  pipeline_id: number;
  ref: string;
  status: string;
  source: string;
  created_at: string | null;
  duration: number | null;
  web_url: string;
};

type IacProject = {
  gitlab_project_id: number;
  name: string;
  path_with_namespace: string;
  default_branch: string;
  archived: boolean;
  last_pipeline_status: string;
  last_activity_at: string | null;
  file_count: number;
  files_by_type: Record<string, number>;
  recent_pipelines: IacPipelineRun[];
};

type IacOverview = {
  projects: IacProject[];
  summary: { project_count: number; file_count: number; pipeline_run_count: number };
};

type IacView = "overview" | "compose" | "k8s" | "terraform" | "ansible" | "ci-schedules";

const VIEW_TABS = [
  { key: "overview", label: "Overview" },
  { key: "compose", label: "Compose Services" },
  { key: "k8s", label: "K8s Resources" },
  { key: "terraform", label: "Terraform Resources" },
  { key: "ansible", label: "Ansible" },
  { key: "ci-schedules", label: "CI Schedules" },
];

type ComposeServiceRow = { service_name: string; image: string; ports: unknown[]; project_name: string; file_path: string };
type ComposeServicesResponse = { items: ComposeServiceRow[]; total: number; limit: number; offset: number };

type K8sResourceRow = { kind: string; name: string; namespace: string; api_version: string; project_name: string; file_path: string };
type K8sResourcesResponse = { items: K8sResourceRow[]; total: number; limit: number; offset: number };

type TerraformResourceRow = { resource_type: string; resource_name: string; project_name: string; file_path: string };
type TerraformResourcesResponse = { items: TerraformResourceRow[]; total: number; limit: number; offset: number };

type AnsibleGroupRow = { name: string; project_name: string; file_path: string; hosts: { name: string }[] };
type AnsiblePlayRow = { name: string; play_index: number; hosts: unknown[] | string; project_name: string; file_path: string };

/** Playbook `hosts:` is a loose YAML field — usually a string pattern
 *  ("all", "webservers:!phoenix") or a list of group/host names, occasionally
 *  a list of objects. Render each shape as a readable chip list instead of a
 *  raw JSON string dump (TRK-198 fix, preserved here). */
function fmtPlayHosts(hosts: unknown[] | string | null | undefined): string[] {
  if (hosts == null) return [];
  if (typeof hosts === "string") return [hosts];
  if (!Array.isArray(hosts)) return [String(hosts)];
  return hosts.map((h) => {
    if (typeof h === "string") return h;
    if (h && typeof h === "object" && "name" in h) return String((h as { name: unknown }).name);
    return JSON.stringify(h);
  });
}
type AnsibleStructureResponse = { groups: AnsibleGroupRow[]; plays: AnsiblePlayRow[] };

type CiScheduleRow = { project_id: number; schedule_id: number; description: string; ref: string; cron: string; active: boolean | null; created_at: string | null };
type CiSchedulesResponse = { items: CiScheduleRow[]; total: number; limit: number; offset: number };

/** GitLab/CI pipeline status → Badge tone. This is a run-outcome axis
 *  (`Status`, per DESIGN.md §3), not a severity — success/passed is `ok`,
 *  failed/canceled is `err`, the in-flight states are `warn`, anything else
 *  (including a genuinely missing status) is `neutral`. */
function pipeTone(status: string): "ok" | "err" | "warn" | "neutral" {
  const v = String(status || "").toLowerCase();
  if (v === "success" || v === "passed") return "ok";
  if (v === "failed" || v === "error" || v === "canceled" || v === "cancelled") return "err";
  if (["running", "pending", "created", "preparing", "waiting_for_resource", "scheduled", "manual"].includes(v)) return "warn";
  return "neutral";
}

/** GitLab group = the namespace prefix of path_with_namespace (everything before
 *  the final "/"). Ports the legacy `p.group` column. */
function groupOf(pathWithNamespace: string): string {
  const i = pathWithNamespace.lastIndexOf("/");
  return i > 0 ? pathWithNamespace.slice(0, i) : "—";
}

/** Compact date-time for the Last Activity column (legacy `p.last_activity_at`).
 *  Delegates to the shared `fmtDt` helper (second-level truncation, `undefined`
 *  for missing) — this column has no reason to be minute-only, so it's aligned
 *  with the rest of the dashboard's datetime convention. */
function fmtActivity(iso: string | null): string | undefined {
  return fmtDt(iso);
}

function fmtDur(secs: number | null): string | undefined {
  if (secs === null || secs === undefined) return undefined;
  const n = Number(secs);
  if (!isFinite(n)) return undefined;
  if (n < 60) return `${n}s`;
  const m = Math.floor(n / 60);
  const s = n % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

/** "active" is a deliberately nullable boolean (unknown collection state, not
 *  "known inactive") — render null as its own em-dash rather than folding it
 *  into "no", which would make "unknown" read as explicitly disabled. */
function activeLabel(active: boolean | null): string | undefined {
  if (active === null) return undefined;
  return active ? "yes" : "no";
}

function ComposePanel() {
  const [data, setData] = useState<ComposeServicesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    apiGet<ComposeServicesResponse>("/api/iac/compose-services")
      .then((d) => live && setData(d))
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
    return () => {
      live = false;
    };
  }, []);

  if (error) return <EmptyState kind="error" title="Compose services failed to load" hint={error} />;
  if (data === null) {
    return (
      <Panel mnemonic="COMPOSE" description="compose_services" rail="info">
        <Skeleton count={3} height={32} />
      </Panel>
    );
  }

  const columns: ColumnDef<ComposeServiceRow>[] = [
    { key: "service_name", header: "Service", typeGlyph: "pk" },
    { key: "image", header: "Image", typeGlyph: "text" },
    { key: "project_name", header: "Project", typeGlyph: "text" },
    { key: "file_path", header: "File", typeGlyph: "text" },
  ];

  return (
    <Panel mnemonic="COMPOSE" description="compose_services" rail="info">
      {data.items.length === 0 ? (
        <EmptyState kind="none-yet" title="No Docker Compose services parsed yet" hint="Services appear here after the next IaC collection run." />
      ) : (
        <DataTable<ComposeServiceRow>
          columns={columns}
          rows={data.items}
          rowKey={(r, i) => `${r.project_name}-${r.service_name}-${i}`}
          toolbarName="compose_services"
          caption="Docker Compose services: service, image, project, and file"
          statusBar={{ shown: data.items.length, total: data.total, note: "read-only · no mutations possible from this view" }}
        />
      )}
    </Panel>
  );
}

function K8sResourcesPanel() {
  const [data, setData] = useState<K8sResourcesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    apiGet<K8sResourcesResponse>("/api/iac/k8s-resources")
      .then((d) => live && setData(d))
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
    return () => {
      live = false;
    };
  }, []);

  if (error) return <EmptyState kind="error" title="Kubernetes resources failed to load" hint={error} />;
  if (data === null) {
    return (
      <Panel mnemonic="K8S" description="k8s_resources" rail="info">
        <Skeleton count={3} height={32} />
      </Panel>
    );
  }

  const columns: ColumnDef<K8sResourceRow>[] = [
    { key: "kind", header: "Kind", typeGlyph: "enum" },
    { key: "name", header: "Name", typeGlyph: "pk" },
    { key: "namespace", header: "Namespace", typeGlyph: "text" },
    { key: "api_version", header: "API Version", typeGlyph: "text" },
    { key: "project_name", header: "Project", typeGlyph: "text" },
  ];

  return (
    <Panel mnemonic="K8S" description="k8s_resources" rail="info">
      {data.items.length === 0 ? (
        <EmptyState kind="none-yet" title="No Kubernetes manifest resources parsed yet" hint="Resources appear here after the next IaC collection run." />
      ) : (
        <DataTable<K8sResourceRow>
          columns={columns}
          rows={data.items}
          rowKey={(r, i) => `${r.project_name}-${r.kind}-${r.name}-${i}`}
          toolbarName="k8s_resources"
          caption="Kubernetes manifest resources: kind, name, namespace, API version, and project"
          statusBar={{ shown: data.items.length, total: data.total, note: "read-only · no mutations possible from this view" }}
        />
      )}
    </Panel>
  );
}

function TerraformPanel() {
  const [data, setData] = useState<TerraformResourcesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    apiGet<TerraformResourcesResponse>("/api/iac/terraform-resources")
      .then((d) => live && setData(d))
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
    return () => {
      live = false;
    };
  }, []);

  if (error) return <EmptyState kind="error" title="Terraform resources failed to load" hint={error} />;
  if (data === null) {
    return (
      <Panel mnemonic="TF" description="terraform_resources" rail="info">
        <Skeleton count={3} height={32} />
      </Panel>
    );
  }

  const columns: ColumnDef<TerraformResourceRow>[] = [
    { key: "resource_type", header: "Resource Type", typeGlyph: "enum" },
    { key: "resource_name", header: "Resource Name", typeGlyph: "pk" },
    { key: "project_name", header: "Project", typeGlyph: "text" },
  ];

  return (
    <Panel mnemonic="TF" description="terraform_resources" rail="info">
      {data.items.length === 0 ? (
        <EmptyState kind="none-yet" title="No Terraform resources parsed yet" hint="Resources appear here after the next IaC collection run." />
      ) : (
        <DataTable<TerraformResourceRow>
          columns={columns}
          rows={data.items}
          rowKey={(r, i) => `${r.project_name}-${r.resource_type}-${r.resource_name}-${i}`}
          toolbarName="terraform_resources"
          caption="Terraform resources: resource type, resource name, and project"
          statusBar={{ shown: data.items.length, total: data.total, note: "read-only · no mutations possible from this view" }}
        />
      )}
    </Panel>
  );
}

function AnsiblePanel() {
  const [data, setData] = useState<AnsibleStructureResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    apiGet<AnsibleStructureResponse>("/api/iac/ansible")
      .then((d) => live && setData(d))
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
    return () => {
      live = false;
    };
  }, []);

  if (error) return <EmptyState kind="error" title="Ansible inventory/playbooks failed to load" hint={error} />;
  if (data === null) {
    return (
      <Panel mnemonic="ANSIBLE" description="ansible_inventory_and_playbooks" rail="info">
        <Skeleton count={3} height={32} />
      </Panel>
    );
  }

  const empty = data.groups.length === 0 && data.plays.length === 0;

  const groupColumns: ColumnDef<AnsibleGroupRow>[] = [
    { key: "name", header: "Group", typeGlyph: "pk" },
    { key: "project_name", header: "Project", typeGlyph: "text" },
    { key: "file_path", header: "File", typeGlyph: "text" },
    {
      key: "hosts",
      header: "Hosts",
      typeGlyph: "text",
      sortable: false,
      render: (g) =>
        g.hosts.length === 0 ? undefined : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {g.hosts.map((h, hi) => (
              <span key={`${h.name}-${hi}`} className="ib-badge" style={{ fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>
                {h.name}
              </span>
            ))}
          </div>
        ),
    },
  ];

  const playColumns: ColumnDef<AnsiblePlayRow>[] = [
    {
      key: "name",
      header: "Play",
      typeGlyph: "pk",
      render: (p) => p.name || `Play #${p.play_index}`,
    },
    { key: "project_name", header: "Project", typeGlyph: "text" },
    { key: "file_path", header: "File", typeGlyph: "text" },
    {
      key: "hosts",
      header: "Hosts",
      typeGlyph: "text",
      sortable: false,
      accessor: (p) => fmtPlayHosts(p.hosts).join(","),
      render: (p) => {
        const hostNames = fmtPlayHosts(p.hosts);
        return hostNames.length === 0 ? undefined : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {hostNames.map((name, hi) => (
              <span key={`${name}-${hi}`} className="ib-badge" style={{ fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>
                {name}
              </span>
            ))}
          </div>
        );
      },
    },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {empty ? (
        <Panel mnemonic="ANSIBLE" description="ansible_inventory_and_playbooks" rail="info">
          <EmptyState kind="none-yet" title="No Ansible inventory or playbooks parsed yet" hint="Inventory and playbooks appear here after the next IaC collection run." />
        </Panel>
      ) : (
        <>
          <Panel mnemonic="INVENTORY" description="ansible_groups" rail="info">
            {data.groups.length === 0 ? (
              <EmptyState kind="none-yet" title="No inventory groups parsed" />
            ) : (
              <DataTable<AnsibleGroupRow>
                columns={groupColumns}
                rows={data.groups}
                rowKey={(g, i) => `${g.project_name}-${g.name}-${i}`}
                toolbarName="ansible_groups"
                caption="Ansible inventory groups: group name, project, file, and member hosts"
                statusBar={{ shown: data.groups.length, note: "read-only · no mutations possible from this view" }}
              />
            )}
          </Panel>
          <Panel mnemonic="PLAYS" description="ansible_plays" rail="info">
            {data.plays.length === 0 ? (
              <EmptyState kind="none-yet" title="No playbook plays parsed" />
            ) : (
              <DataTable<AnsiblePlayRow>
                columns={playColumns}
                rows={data.plays}
                rowKey={(p, i) => `${p.project_name}-${p.play_index}-${i}`}
                toolbarName="ansible_plays"
                caption="Ansible playbook plays: play name, project, file, and target hosts"
                statusBar={{ shown: data.plays.length, note: "read-only · no mutations possible from this view" }}
              />
            )}
          </Panel>
        </>
      )}
    </div>
  );
}

function CiSchedulesPanel() {
  const [data, setData] = useState<CiSchedulesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    apiGet<CiSchedulesResponse>("/api/iac/ci-schedules")
      .then((d) => live && setData(d))
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
    return () => {
      live = false;
    };
  }, []);

  if (error) return <EmptyState kind="error" title="CI pipeline schedules failed to load" hint={error} />;
  if (data === null) {
    return (
      <Panel mnemonic="CISCHED" description="ci_schedules" rail="info">
        <Skeleton count={3} height={32} />
      </Panel>
    );
  }

  const columns: ColumnDef<CiScheduleRow>[] = [
    { key: "project_id", header: "Project", typeGlyph: "num" },
    { key: "schedule_id", header: "Schedule", typeGlyph: "num" },
    { key: "description", header: "Description", typeGlyph: "text" },
    { key: "ref", header: "Ref", typeGlyph: "text" },
    { key: "cron", header: "Cron", typeGlyph: "text" },
    { key: "active", header: "Active", typeGlyph: "enum", render: (r) => activeLabel(r.active) },
  ];

  return (
    <Panel mnemonic="CISCHED" description="ci_schedules" rail="info">
      {data.items.length === 0 ? (
        <EmptyState kind="none-yet" title="No CI pipeline schedules collected yet" hint="Schedules appear here after the next IaC collection run." />
      ) : (
        <DataTable<CiScheduleRow>
          columns={columns}
          rows={data.items}
          rowKey={(r, i) => `${r.project_id}-${r.schedule_id}-${i}`}
          toolbarName="ci_schedules"
          caption="CI pipeline schedules: project, schedule, description, ref, cron, and active state"
          statusBar={{ shown: data.items.length, total: data.total, note: "read-only · no mutations possible from this view" }}
        />
      )}
    </Panel>
  );
}

/** Port of the canonical legacy IaC view (index.html L1969-2149) — GitLab/IaC
 *  estate overview backed by GET /api/iac/overview
 *  (src/infra_brain/api/routers/iac.py:42), returning IacOverviewOut
 *  {projects,summary}. Projects table carries a files-by-type chip breakdown
 *  and an archived pill; pipeline runs are grouped per project with an outbound
 *  web_url link per run.
 *
 *  UI Batch 6 (GitLab #62) added a sub-view tab bar: Compose Services, K8s
 *  Resources, Terraform Resources, Ansible, and CI Schedules — flat listings
 *  of parsed-IaC-file children. TRK-198 replaced a raw `JSON.stringify`
 *  playbook-hosts dump with `fmtPlayHosts()`; preserved here unchanged.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md):
 *  PageShell/Panel/DataTable/Badge/Tabs/EmptyState replace the hand-rolled
 *  `simpleTable`/inline-hex cards. Every sub-view keeps its own loading/error/
 *  empty handling (independent fetches, same as before); the Ansible
 *  sub-view's host-chip lists are now `DataTable` cells instead of standalone
 *  card stacks, since groups/plays are each a homogeneous row shape. */
export function Iac() {
  const [data, setData] = useState<IacOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<IacView>("overview");

  const load = useCallback(async () => {
    setData(await apiGet<IacOverview>("/api/iac/overview"));
  }, []);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  const h = headerFor("iac");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="IaC overview failed to load" hint={error} />
      </div>
    );
  }

  if (data === null) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Panel mnemonic="IAC" description="iac_overview" rail="info">
          <Skeleton count={3} height={32} />
        </Panel>
      </div>
    );
  }

  const projects = data.projects || [];
  const summary = data.summary || { project_count: 0, file_count: 0, pipeline_run_count: 0 };
  const runGroups = projects.filter((p) => (p.recent_pipelines || []).length > 0);
  const archivedCount = projects.filter((p) => p.archived).length;

  const pills = [
    { dot: "var(--ib-blue)", text: `${summary.project_count || 0} projects` },
    { dot: statusColor("ok"), text: `${summary.file_count || 0} IaC files` },
    { dot: "var(--ib-blue)", text: `${summary.pipeline_run_count || 0} pipeline runs` },
    { dot: "var(--ib-muted)", text: `${archivedCount} archived` },
  ];

  const projectColumns: ColumnDef<IacProject>[] = [
    {
      key: "name",
      header: "Project",
      typeGlyph: "pk",
      render: (p) => (
        <span style={{ opacity: p.archived ? 0.55 : 1, display: "inline-flex", alignItems: "center", gap: 7 }}>
          {p.name}
          {p.archived && <Badge tone="neutral">archived</Badge>}
        </span>
      ),
    },
    { key: "group", header: "Group", typeGlyph: "text", accessor: (p) => groupOf(p.path_with_namespace), sortable: false },
    { key: "path_with_namespace", header: "Path", typeGlyph: "text" },
    { key: "default_branch", header: "Default branch", typeGlyph: "text" },
    {
      key: "last_pipeline_status",
      header: "Last pipeline",
      typeGlyph: "enum",
      render: (p) => <Badge tone={pipeTone(p.last_pipeline_status)}>{p.last_pipeline_status || "none"}</Badge>,
    },
    {
      key: "last_activity_at",
      header: "Last activity",
      typeGlyph: "text",
      accessor: (p) => p.last_activity_at,
      render: (p) => fmtActivity(p.last_activity_at),
    },
    { key: "file_count", header: "Files", typeGlyph: "num" },
    {
      key: "files_by_type",
      header: "IaC files by type",
      typeGlyph: "text",
      sortable: false,
      render: (p) => {
        const chips = Object.entries(p.files_by_type || {}).sort((a, b) => b[1] - a[1]);
        if (chips.length === 0) return <span style={{ color: "var(--ib-faint)", fontStyle: "italic" }}>no files parsed</span>;
        return (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
            {chips.map(([ftype, count]) => (
              <span key={ftype} className="ib-badge" style={{ fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>
                {ftype} {count}
              </span>
            ))}
          </div>
        );
      },
    },
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} pills={pills} />

      <div style={{ marginBottom: 18 }}>
        <Tabs tabs={VIEW_TABS} active={view} onChange={(k) => setView(k as IacView)} label="IaC view" />
      </div>

      {view === "overview" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
          <Panel
            mnemonic="PROJECTS"
            description="GitLab projects collected read-only · last-pipeline status and parsed IaC file-type breakdown per project"
            rail="info"
          >
            {projects.length === 0 ? (
              <EmptyState kind="none-yet" title="No GitLab projects collected yet" hint="Projects and IaC files appear here after the next CI/CD collection run." />
            ) : (
              <DataTable<IacProject>
                columns={projectColumns}
                rows={projects}
                rowKey={(p) => p.gitlab_project_id}
                sortable
                toolbarName="iac_projects"
                caption="GitLab IaC projects: name, group, path, default branch, last pipeline, last activity, file count, and file-type breakdown"
                statusBar={{ shown: projects.length, note: "read-only · no mutations possible from this view" }}
              />
            )}
          </Panel>

          <Panel
            mnemonic="PIPELINES"
            description="Latest CI/CD pipeline runs per project · ref, status, when it ran, and duration"
            rail="info"
          >
            {runGroups.length === 0 ? (
              <EmptyState kind="none-yet" title="No pipeline runs collected" hint="Recent CI/CD pipeline runs appear here after the next collection run." />
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 14, padding: 16 }}>
                {runGroups.map((p) => {
                  const runColumns: ColumnDef<IacPipelineRun>[] = [
                    { key: "ref", header: "Ref", typeGlyph: "text" },
                    { key: "status", header: "Status", typeGlyph: "enum", render: (r) => <Badge tone={pipeTone(r.status)}>{r.status}</Badge> },
                    { key: "source", header: "Source", typeGlyph: "text" },
                    { key: "created_at", header: "Created", typeGlyph: "text", render: (r) => fmtDt(r.created_at) },
                    { key: "duration", header: "Duration", typeGlyph: "text", render: (r) => fmtDur(r.duration) },
                    {
                      key: "web_url",
                      header: "Pipeline",
                      typeGlyph: "fk",
                      sortable: false,
                      render: (r) =>
                        r.web_url ? (
                          <FkCell label={`#${r.pipeline_id}`} href={r.web_url} title={r.web_url} />
                        ) : (
                          <span style={{ color: "var(--ib-muted)" }}>#{r.pipeline_id}</span>
                        ),
                    },
                  ];
                  return (
                    <div key={p.gitlab_project_id}>
                      <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 6, flexWrap: "wrap" }}>
                        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ib-text)" }}>{p.name}</span>
                        <span style={{ fontSize: 11, color: "var(--ib-faint)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>
                          {p.path_with_namespace}
                        </span>
                        <span style={{ fontSize: 11, color: "var(--ib-faint)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>
                          · {p.recent_pipelines.length} runs
                        </span>
                      </div>
                      <DataTable<IacPipelineRun>
                        columns={runColumns}
                        rows={p.recent_pipelines}
                        rowKey={(r) => r.pipeline_id}
                        showDensityToggle={false}
                        toolbarName={undefined}
                        statusBar={false}
                        caption={`Recent pipeline runs for ${p.name}`}
                      />
                    </div>
                  );
                })}
              </div>
            )}
          </Panel>
        </div>
      )}

      {view === "compose" && <ComposePanel />}
      {view === "k8s" && <K8sResourcesPanel />}
      {view === "terraform" && <TerraformPanel />}
      {view === "ansible" && <AnsiblePanel />}
      {view === "ci-schedules" && <CiSchedulesPanel />}
    </div>
  );
}
