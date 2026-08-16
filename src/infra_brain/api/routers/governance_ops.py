"""infra_brain.api.routers.governance_ops -- Agents, settings, config, compliance, approvals.

Split from governance.py (refactor/split-governance-router).
Handler bodies are byte-identical to the originals; no logic changes.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request

from infra_brain.action_decisions import (
    ActionDecisionError,
    approve_action,
    reject_action,
)
from infra_brain.api._envelope import paginate
from infra_brain.api._helpers import (
    _DOMAIN_REQUIREMENTS,
    _is_secret,
    _RUN_STATUS,
    _SECRET_KEYS,  # noqa: F401 — re-export: dashboard_api.py and governance.py import it from here
    _humanize_cron,
    _s,
    _setting_row,
)
from infra_brain.api.schemas import (
    ActionApproveBody,
    ActionApproveResult,
    ActionRejectResult,
    AgentConfigOut,
    AgentConfigPageOut,
    AgentConfigRequirement,
    AgentOut,
    AgentRosterPageOut,
    ComplianceOut,
    CompliancePageOut,
    InventoryReconcileOut,
    InventoryReconcilePageOut,
    SettingGroup,
    SettingRow,
    SettingsPageOut,
    UiSettingsPageOut,
)
from infra_brain.dashboard_auth import current_user, require_admin, require_session
from infra_brain.db.models import (
    CollectionRun,
    ComplianceViolation,
    InventoryReconcileEvent,
)
from infra_brain.db.session import get_session
from infra_brain.remediation_graph import resume_remediation_action

governance_ops_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)


@governance_ops_router.get("/inventory_reconcile", response_model=InventoryReconcilePageOut)
def list_inventory_reconcile(status: str | None = None, limit: int = 500, offset: int = 0):
    # M-3: clamp before use so the returned envelope reflects what was
    # actually queried (paginate() also clamps as a backstop).
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(InventoryReconcileEvent)
        if status:
            q = q.filter(InventoryReconcileEvent.status == status)
        q = q.order_by(InventoryReconcileEvent.detected_at.desc())
        items_raw, total = paginate(q, limit=limit, offset=offset)
        items = [
            InventoryReconcileOut(
                host=e.host,
                domain=e.domain,
                target_group=e.target_group,
                status=e.status,
                mr_url=e.mr_url or "—",
                detected_at=e.detected_at,
            )
            for e in items_raw
        ]
        return InventoryReconcilePageOut(items=items, total=total, limit=limit, offset=offset)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Compliance violations  (NEW page)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@governance_ops_router.get("/compliance", response_model=CompliancePageOut)
def list_compliance(status: str | None = None, limit: int = 500, offset: int = 0):
    """List compliance violations, optionally filtered by status.

    FE-7: the default used to be ``status="open"``, but the dashboard's
    Compliance page fetches this endpoint with no ``status`` query param at
    all (``load('/compliance','COMPL')`` — see dashboard/static/index.html)
    and then computes its own "Resolved" stat/tab by filtering the *response*
    client-side for ``status === 'resolved'``. Since the server-side default
    silently dropped every non-open row before the client ever saw it, the
    "Resolved" pill was permanently 0 and the "Resolved" filter tab always
    empty, even though ``ComplianceAgent`` (agents/compliance.py) does write
    ``status="resolved"`` rows. Defaulting to no filter (matches the sibling
    ``/inventory_reconcile`` endpoint's pattern) makes the client's own
    open/resolved counts correct without changing behavior for any caller
    that already passes an explicit ``status``.
    """
    # M-3: clamp before use, see list_inventory_reconcile above for rationale.
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(ComplianceViolation)
        if status:
            q = q.filter(ComplianceViolation.status == status)
        q = q.order_by(ComplianceViolation.detected_at.desc())
        items_raw, total = paginate(q, limit=limit, offset=offset)
        items = [
            ComplianceOut(
                rule=c.rule,
                severity=c.severity,
                host=c.host,
                detail=c.detail,
                status=c.status,
                detected_at=c.detected_at,
            )
            for c in items_raw
        ]
        return CompliancePageOut(items=items, total=total, limit=limit, offset=offset)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Agents roster
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


_SYSTEM_DOMAINS = {"drift", "notification", "drift_learning", "fleet_health", "inventory_reconcile"}

_AGENT_DESC: dict[str, str] = {
    "linux": (
        "Runs ansible -m setup against every Linux host to snapshot OS version, kernel, "
        "installed packages, running services, and network interfaces. Deviations from the "
        "previous snapshot are raised as drift events automatically."
    ),
    # The three entries below describe RETIRED collectors (AgentSpec.retired).
    # They were dropped from this map when the domains were commented out of
    # _AGENT_SPECS; they are back because retirement keeps the domain in
    # AGENT_REGISTRY on purpose, so the roster shows it as off-by-decision
    # rather than not showing it at all — and a roster row with a blank
    # description is exactly the "is this broken?" ambiguity that defeats.
    "windows": (
        "RETIRED (TRK-278 / GitLab #140). Gathered Windows host facts — patch state, "
        "services, local admins, certificates, shares — over WinRM via Ansible. The "
        "Ansible Windows collection was retired by standing decision."
    ),
    "cloud": (
        "RETIRED. Enumerated AWS EC2 instances, VPCs, and security groups. There is no "
        "AWS account behind this home lab; deferred at POC stage and now off by standing "
        "decision."
    ),
    "k8s": (
        "RETIRED. Listed Kubernetes nodes, pods, and deployments via the cluster API. "
        "Nothing in this home lab runs a cluster."
    ),
    "iac": (
        "Clones Ansible inventory files and Terraform state from GitLab repositories and "
        "parses host groups, variable assignments, and provisioned resources. This is the "
        "source-of-truth baseline that other agents reconcile against."
    ),
    "vsphere": (
        "Uses pyVmomi to enumerate vCenter VMs, ESXi host hardware, datastore capacity, "
        "and VM power states. Detects orphaned VMs, datastore pressure, and ESXi "
        "version drift across your on-premises virtualisation layer."
    ),
    "cicd": (
        "Reads GitLab project metadata, last pipeline status, runner tag assignments, and "
        "deployment frequencies. Surfaces failed pipelines, stale branches, and projects "
        "with no recent CI activity that may indicate abandoned workloads."
    ),
    "octopus": (
        "Enumerates Octopus Deploy environments, deployment targets (machines), and recent "
        "deployment history including which package versions are live where. Flags targets "
        "that have gone offline or environments with failed deployments."
    ),
    "vuln": (
        "Pulls the full vulnerability inventory from Rapid7 InsightVM — CVE IDs, CVSS "
        "scores, affected packages, asset exposure, and remediation SLA timers. Feeds the "
        "vuln_triage agent for LLM-powered prioritisation."
    ),
    "eol": (
        "Checks each tracked asset's product version against endoflife.date to determine "
        "current support status. Assigns a PCI risk score based on exposure and flags assets "
        "past or approaching end-of-life as active compliance liabilities."
    ),
    "licensing": (
        "Reconciles operator-seeded software license entitlement rows against the installed "
        "base derived from HAS_SOFTWARE graph edges (Windows/Rapid7/Linux installed "
        "software). Surfaces over- and under-entitlement as compliance violations."
    ),
    "dns": (
        "Collects authoritative DNS zone/record data (A/AAAA/CNAME/MX/TXT/SRV, SOA/zone "
        "serial, delegation NS records) via SOA lookup and best-effort AXFR. Enables drift "
        "detection on DNS itself — stale records, orphaned CNAMEs pointing at decommissioned "
        "hosts. Unconfigured by default (empty dns_zones)."
    ),
    "backup": (
        "Polls backup-system APIs (Veeam, Bacula, cloud snapshot) for last-successful-backup "
        "and last-restore-test timestamps per target resource. Flags hosts/VMs with no "
        "recent successful backup or an overdue DR restore test."
    ),
    "fleet_health": (
        "Aggregates the most recent collection run status, resource counts, and drift totals "
        "across all domains into a single fleet-wide health signal. Surfaces overdue sweeps, "
        "zero-resource runs, and domains with consecutive failures."
    ),
    "discovery": (
        "LLM-powered agent that cross-correlates all domain inventories to find assets "
        "present in one source but absent from others. Produces integration proposals for "
        "untracked resources and new data sources that would improve coverage."
    ),
    "drift": (
        "Diffs the latest snapshot against the previous one for every resource in the "
        "database. Raises a typed drift event (config_drift, state_drift, or new_asset) "
        "whenever a tracked field changes or an asset disappears."
    ),
    "notification": (
        "Reads open drift events and opens Jira tickets or upserts Confluence documentation "
        "pages to reflect the current state. This is the only agent that writes to external "
        "systems — every action is recorded in the immutable audit trail."
    ),
    "drift_learning": (
        "Analyses the drift event history to identify patterns that repeat across runs and "
        "hosts. Promotes high-confidence patterns (â‰¥ 0.70) into Instincts that prime future "
        "agent reasoning, reducing alert noise for expected environmental changes."
    ),
    "inventory_reconcile": (
        "Compares the set of discovered hosts against the Ansible inventory in GitLab. "
        "For each gap it generates an add-only merge request for human review — the system "
        "never deletes or modifies existing inventory entries autonomously."
    ),
    "vuln_triage": (
        "LLM-powered agent that scores each open CVE using CVSS severity, EPSS exploit "
        "probability, the affected asset's EOL status, and its network exposure zone. "
        "Produces a prioritised remediation queue ordered by actual business risk."
    ),
    "compliance": (
        "Evaluates every asset against policy-as-code rules: EOL status, open CVEs past "
        "their remediation SLA, and drift left unresolved beyond the grace period. "
        "Findings are tagged to PCI DSS control identifiers for audit evidence."
    ),
    "rootcause": (
        "Correlates open drift events with recent CI/CD deployments and collection run "
        "timelines to propose likely root causes. Helps distinguish expected change "
        "from unexpected deviation before a Jira ticket is opened."
    ),
    "remediation": (
        "Drafts Ansible playbooks or Terraform patches for approved drift or CVE findings "
        "and generates a GitLab merge request for human review. No infrastructure change "
        "is applied without explicit human approval of the MR."
    ),
    "coverage": (
        "LLM-powered agent that compares AGENT_REGISTRY against active scan "
        "points to find coverage gaps, proposes a collection strategy per gap, "
        "and wires an approved proposal into a live ScanPoint plus seed Instincts."
    ),
    "graph_maintenance": (
        "Recomputes and refreshes knowledge-graph edges across the full "
        "resource inventory in chunk-buffered batches, keeping resource "
        "relationships current as the fleet grows."
    ),
    "host_reconcile": (
        "Reconciles discovered host identities across domains (e.g. the same "
        "physical host seen by both the vSphere and Linux collectors) into a "
        "single canonical resource record."
    ),
    "inventory_mr": (
        "Opens GitLab merge requests that add discovered-but-untracked hosts "
        "to the Ansible inventory. Add-only — never modifies or removes "
        "existing inventory entries."
    ),
    "knowledge": (
        "Indexes GitLab wiki pages, Confluence spaces, and repository docs "
        "into the RAG knowledge base used by the chat agent's search_knowledge "
        "tool."
    ),
    "learning_feedback": (
        "Analyses which drift-learning Instincts have proven useful (reduced "
        "alert noise) versus which were wrong, feeding a confidence-adjustment "
        "signal back into future Instinct promotion."
    ),
    "local_docs": (
        "Indexes local repository documentation (README, docs/, runbooks) "
        "into the same RAG knowledge base as the knowledge agent, for "
        "documentation that lives in this repo rather than GitLab/Confluence."
    ),
    "netdiscovery": (
        "Runs read-only nmap/`ip route` subprocess scans (gated by "
        "_gate_nmap_targets) to discover network-reachable hosts not yet "
        "known to any other domain collector."
    ),
    "personal_wiki": (
        "Indexes a curated subset (entities/, wiki/, raw/) of a separate "
        "personal Hermes-wiki filesystem into the same RAG knowledge base as "
        "knowledge/local_docs — tagged Document.source='personal_wiki' and "
        "FENCED: excluded from search_knowledge by default, requires the "
        "caller to pass include_personal=True. Off by default; requires "
        "rag_enabled AND personal_wiki_ingest_enabled AND personal_wiki_root."
    ),
    "secrets_inventory": (
        "Inventories an external secrets manager (Vault / Bitwarden Secrets "
        "Manager) for secret EXISTENCE, rotation age, and referencing system. "
        "Metadata-only by construction: it holds no code path to any "
        "value-returning endpoint and stores no secret material."
    ),
    "query": (
        "On-demand text-to-SQL agent over the collected infrastructure "
        "database; also runs a weekly scope='health' DB-connectivity "
        "warm-check job."
    ),
    "loadbalancer": (
        "Collects load balancer / reverse proxy / CDN configuration — F5 BIG-IP "
        "(iControl REST), nginx Plus (API), HAProxy (stats page), and Cloudflare "
        "(API) — recording backend-pool membership, health-check config, and "
        "TLS-termination config. Idle (CollectorSkipped) until lb_enabled and at "
        "least one vendor's config is set (GitLab issue #100)."
    ),
    "saas_inventory": (
        "Metadata-only collector for third-party SaaS applications and "
        "their API-key metadata (name/scope/created_at/last_used_at — "
        "never the key value); clean no-op until saas_admin_url is "
        "configured (GitLab #103)."
    ),
    "pki": (
        "Derives tracked root/intermediate Certificate Authorities from "
        "already-collected host_certificates issuer strings (no new external "
        "API); runs a read-only GET health check against each CA's own "
        "CRL/OCSP responder URL when known (GitLab issue #94)."
    ),
    "capacity_forecast": (
        "Deterministic (no-LLM) linear-regression reasoner over existing "
        "vsphere_datastore_metrics history, projecting days-until-threshold "
        "capacity per datastore as Instinct rows. Opt-in via "
        "capacity_forecast_enabled (default off, GitLab #99)."
    ),
    "container_registry": (
        "Registry-agnostic OCI Distribution API v2 collector (Docker Hub, "
        "GitLab Container Registry, Harbor, ECR, GHCR, ACR, ...) — enumerates "
        "images and their attached OCI referrers artifacts (signatures/SBOMs/"
        "scan attestations). Never claims runtime placement (GitLab #101)."
    ),
    "identity": (
        "IdP (Okta) identity/SSO/RBAC audit — reconciles IdP principals and "
        "group memberships against existing per-system access grants via "
        "IS_PRINCIPAL_FOR edges. Clean no-op until okta_url/okta_api_token "
        "are configured (GitLab issue #102)."
    ),
    "homelab_services": (
        "Generic manifest-driven health sweep across the home lab's "
        "self-hosted services (bare GET reachability, up/down + HTTP status) — "
        "the fleet-wide coarse counterpart to the per-service collectors "
        "below. Idle-safe: an entry missing a URL is skipped, never guessed."
    ),
    "prometheus": (
        "Scrape-target health and firing-alert inventory via Prometheus's "
        "HTTP API (/api/v1/targets, /api/v1/alerts). Idle (CollectorSkipped) "
        "until prometheus_url is configured."
    ),
    "grafana": (
        "Grafana liveness, dashboard inventory, and alert rules. The "
        "unauthenticated /api/health check always runs when grafana_url is "
        "set; dashboard/alert collection additionally needs grafana_api_token."
    ),
    "alertmanager": (
        "Alertmanager liveness, active alerts, and active silences via its "
        "HTTP API (/api/v2/status, /api/v2/alerts, /api/v2/silences). Idle "
        "until alertmanager_url is configured."
    ),
    "uptime_kuma": (
        "Per-monitor up/down status via Uptime Kuma's public status-page API. "
        "Idle until uptime_kuma_url is configured; the real status-page slug "
        "may need confirming against uptime_kuma_status_page_slug."
    ),
    "wazuh": (
        "Registered-agent inventory and recent security alerts via the Wazuh "
        "Manager REST API (JWT auth per collect() run). Idle until "
        "wazuh_url/wazuh_username/wazuh_password are configured; an auth "
        "failure (as opposed to being unconfigured) is a real error, not a "
        "self-skip."
    ),
}

_AGENT_TOOLS: dict[str, list[str]] = {
    "linux": ["Ansible facts module", "Ansible ping"],
    # Retired collectors keep their tool list — it is what they WOULD use if
    # revived. See the retired entries in _AGENT_DESC above.
    "windows": ["Ansible win_setup facts (WinRM)", "WinRM client"],
    "cloud": ["AWS EC2 API (GET-only)", "AWS VPC API", "AWS security-groups API"],
    "k8s": ["Kubernetes list API (paginated)"],
    "iac": ["GitLab API (paginated)", "Ansible inventory parser"],
    "vsphere": ["pyVmomi VM list", "pyVmomi host list", "pyVmomi datastore list"],
    "cicd": ["GitLab projects API", "GitLab pipelines API"],
    "octopus": ["Octopus Deploy API (paginated)"],
    "vuln": ["Rapid7 InsightVM assets API"],
    "eol": ["endoflife.date HTTP API"],
    "licensing": ["software_licenses DB query", "HAS_SOFTWARE graph edge query"],
    "dns": ["DNS SOA lookup (GET-equivalent)", "best-effort AXFR zone transfer"],
    "backup": ["Veeam/Bacula/cloud-snapshot API (GET-only)"],
    "fleet_health": ["collection_runs DB query", "resources DB query"],
    "discovery": [
        "Ansible inventory tool",
        "vSphere hosts tool",
        "Context7 docs tool",
    ],
    "drift": ["snapshot diff engine"],
    "notification": ["Jira create ticket", "Confluence upsert page"],
    "drift_learning": ["drift events DB query", "instincts upsert"],
    "inventory_reconcile": [
        "GitLab repository tree",
        "Ansible inventory parser",
        "GitLab file reader",
    ],
    "vuln_triage": ["vuln queue DB query", "EOL registry DB query"],
    "compliance": ["resources DB query", "snapshots DB query"],
    "rootcause": ["drift events DB query", "collection runs DB query"],
    "remediation": ["proposed actions DB query", "GitLab MR creator"],
    "coverage": ["AGENT_REGISTRY gap scan", "scan_points DB query", "instincts upsert"],
    "graph_maintenance": ["resources DB query", "graph edge upsert (chunk-buffered)"],
    "host_reconcile": ["resources DB query", "host identity merge"],
    "inventory_mr": ["GitLab repository tree", "GitLab MR creator"],
    "knowledge": ["GitLab wiki API", "Confluence spaces API", "embeddings upsert"],
    "learning_feedback": ["instincts DB query", "drift events DB query"],
    "local_docs": ["local filesystem doc scan", "embeddings upsert"],
    "personal_wiki": ["local filesystem doc scan (Hermes wiki root)", "embeddings upsert"],
    "netdiscovery": ["nmap subprocess (gated)", "ip route subprocess (gated)"],
    "query": ["text-to-SQL agent", "DB connectivity health check"],
    "loadbalancer": [
        "F5 iControl REST (GET-only)",
        "nginx Plus API (GET-only)",
        "HAProxy stats page CSV export (GET-only)",
        "Cloudflare API (GET-only)",
    ],
    "saas_inventory": [
        "SaaS applications tool (GET-only)",
        "SaaS API-key metadata tool (GET-only, secret-stripped)",
    ],
    "pki": ["CRL/OCSP responder probe (GET-only)"],
    "capacity_forecast": ["vsphere_datastore_metrics DB query"],
    "container_registry": [
        "OCI Distribution API v2 catalog/tags/manifest/referrers (GET-only)",
    ],
    "identity": ["Okta users/groups API (GET-only, paginated)"],
    "secrets_inventory": [
        "Vault KV-v2 metadata API (GET, list/metadata only)",
        "Bitwarden Secrets Manager org listing (GET, no value endpoint)",
    ],
    "homelab_services": ["Bare HTTP GET reachability per manifest entry"],
    "prometheus": ["Prometheus targets API (GET)", "Prometheus alerts API (GET)"],
    "grafana": [
        "Grafana health API (GET, anonymous)",
        "Grafana dashboard search API (GET)",
        "Grafana alerting API (GET)",
    ],
    "alertmanager": [
        "Alertmanager status API (GET)",
        "Alertmanager alerts API (GET)",
        "Alertmanager silences API (GET)",
    ],
    "uptime_kuma": ["Uptime Kuma status-page API (GET)"],
    "wazuh": [
        "Wazuh Manager JWT auth (POST, one-time per run)",
        "Wazuh agents API (GET)",
        "Wazuh alerts API (GET)",
    ],
}


@governance_ops_router.get("/agents", response_model=AgentRosterPageOut)
def list_agents(limit: int = 200, offset: int = 0):
    from infra_brain.etl.spec import retired_domains
    from infra_brain.scheduler import _DEFAULT_SCHEDULES
    from infra_brain.supervisor import AGENT_REGISTRY

    retired = retired_domains()
    out: list[AgentOut] = []
    with get_session() as s:
        for domain, cls in AGENT_REGISTRY.items():
            last = (
                s.query(CollectionRun)
                .filter(CollectionRun.domain == domain)
                .order_by(CollectionRun.started_at.desc())
                .first()
            )
            if getattr(cls, "llm_role", None):
                kind = "llm"
            elif domain in _SYSTEM_DOMAINS:
                kind = "system"
            else:
                kind = "collector"
            status = "idle"
            output = "no runs yet"
            if last:
                # T8: cover every terminal status etl/base.py's BaseAgent.run()
                # can write (see the R3 status mapping + CollectorSkipped
                # branch there) so a partial or skipped run never silently
                # falls through to "idle" — that collapsed real problems
                # (detail-write failures reported as "partial") into the same
                # neutral grey as "never run", and excluded them from both
                # the Healthy and Degraded roster counts. "partial" keeps its
                # own explicit status (a run that reported success but lost
                # some detail rows) rather than collapsing into "healthy" or
                # "degraded" — the frontend folds it into the Degraded tile
                # since partial data is a degraded state, while still
                # labeling it distinctly. "skipped" (dependency unconfigured
                # or collection_disabled_domains) is intentional, not a
                # failure, so it gets its own neutral-but-explicit status
                # rather than reusing "idle" (which means "no runs yet").
                status = {
                    "completed": "healthy",
                    "failed": "degraded",
                    "partial": "partial",
                    "skipped": "skipped",
                }.get(last.status, "idle")
                total_rows = (last.resources_found or 0) + (last.detail_rows_written or 0)
                # A failed run's `output` previously read "N resources"
                # (often "0 resources") the same as a genuinely successful
                # but empty sweep — indistinguishable from a real crash in
                # the roster/drawer UI. Say so plainly instead.
                output = "run failed" if last.status == "failed" else f"{total_rows} resources"
            if domain in retired:
                # Overrides whatever the last (now historical) run said. A
                # retired collector is off by standing decision — its upstream
                # system does not exist here — so reporting it as "skipped"
                # (which reads as "unconfigured, go configure it") or leaving a
                # months-old "healthy" on the row are both misleading. This is
                # the one status an operator should read as "off on purpose".
                status = "retired"
                output = "retired — not collected"
            out.append(
                AgentOut(
                    name=cls.__name__,
                    domain=domain,
                    kind=kind,
                    schedule="retired"
                    if domain in retired
                    else _humanize_cron(_DEFAULT_SCHEDULES.get(domain, "on-demand")),
                    last_run=_s(last.started_at) if last else "—",
                    status=status,
                    output=output,
                    desc=_AGENT_DESC.get(domain, ""),
                    tools=_AGENT_TOOLS.get(domain, []),
                )
            )
    total = len(out)
    page = out[offset : offset + limit]
    return AgentRosterPageOut(items=page, total=total, limit=limit, offset=offset)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Settings & health
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


# ── Settings surface split (TRK-321) ─────────────────────────────────────────
# `GET /settings` returns the ENTIRE Settings model. That is an elevated view —
# every URL, every tunable, every feature flag, every masked-but-enumerable
# secret NAME in the system — yet it used to sit behind only the router-level
# `require_session`, unlike the comparable elevated operations in this same
# file, which are `require_admin`-gated. Any signed-in non-admin could
# enumerate the whole configuration surface.
#
# Simply adding `require_admin` to it was attempted and REVERTED, because the
# route had TWO consumers, not one: the Settings page AND `Intprops.tsx`, which
# fetched the whole dump inside a `Promise.all` purely to read ONE row
# (INTEGRATION_CONFIDENCE_GATE). A 403 rejected the whole `Promise.all` and
# blanked the entire Integrations page — proposals included — for every
# non-admin user. `require_admin` is open in dev mode, so that breakage was
# invisible locally and showed up only in deployments carrying non-admin
# `ui_users` rows.
#
# The fix is to split the surface rather than pick one consumer over the other:
#   * `GET /settings`     — the full dump, `require_admin`.
#   * `GET /settings/ui`  — the narrow non-sensitive subset the UI needs in
#                           order to render, readable by any signed-in session.
#
# _UI_SETTINGS_ALLOWLIST is deliberately an ALLOWLIST of specific field names,
# NOT a "everything except the things that look secret" denylist. A denylist
# silently widens every time a new sensitive Settings field is added whose name
# happens not to match a secret hint — which is exactly how `postgres_url`,
# `postgres_readonly_url` and `redis_url` came to render their
# `user:password@host` DSNs in cleartext (TRK-318). Adding a key here must be a
# deliberate, reviewed act. `tests/test_dashboard_api.py` pins both the
# membership (every entry is a real Settings field) and the non-sensitivity
# (no entry is secret-hinted) of this set.
_UI_SETTINGS_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Intprops.tsx renders the integration-discovery confidence threshold
        # as the cutoff line on the proposal list, and needs it at runtime so
        # the two surfaces can't drift from a hardcoded 0.7. Non-sensitive: a
        # bare float.
        "integration_confidence_gate",
    }
)


@governance_ops_router.get("/settings/ui", response_model=UiSettingsPageOut)
def get_ui_settings_view() -> UiSettingsPageOut:
    """The non-sensitive settings subset any signed-in session may read.

    Serves ONLY the keys in `_UI_SETTINGS_ALLOWLIST` (see the rationale above
    the allowlist). Flat list of rows rather than the admin view's grouped
    shape: this is a deliberately different, narrower contract, not a filtered
    rendering of the same one — so it cannot be quietly swapped back for the
    full dump.

    No DB access and no `await`-able work, hence a plain `def` handler (FastAPI
    runs it in the threadpool); `require_admin` is NOT applied — that is the
    whole point of this route existing.
    """
    from infra_brain.config import get_settings

    fields = get_settings().model_dump()
    items: list[SettingRow] = []
    for key in sorted(_UI_SETTINGS_ALLOWLIST):
        if key not in fields:
            # Allowlisted name no longer exists on Settings (renamed/removed).
            # Skip rather than 500; the test suite fails loudly on this drift.
            continue
        row = _setting_row(key, fields[key])
        if row.type == "secret":
            # Belt-and-braces only: an allowlisted field that renders as a
            # secret is an allowlist mistake, so drop it rather than serve it.
            # A test asserts no allowlist entry is secret-hinted, so this
            # branch is unreachable in practice and must not become the thing
            # actually holding the line.
            continue
        items.append(row)
    return UiSettingsPageOut(items=items, total=len(items), limit=len(items), offset=0)


@governance_ops_router.get("/settings", response_model=SettingsPageOut)
def get_settings_view(limit: int = 50, offset: int = 0, _: None = Depends(require_admin)):
    """Full `Settings.model_dump()` — admin only (TRK-321).

    Non-admin sessions that need the handful of non-sensitive values the UI
    renders must use `GET /settings/ui` instead; see the block comment above.
    """
    from infra_brain.config import get_settings

    s = get_settings()
    fields = s.model_dump()

    def group_for(name: str) -> str:
        n = name.lower()
        if n.startswith(("llm_", "anthropic", "bedrock", "openai")):
            return "LLM"
        if n.startswith(("postgres", "redis")):
            return "Database"
        if n.startswith(("gitlab", "octopus", "rapid7", "vsphere", "ansible", "snmp", "inventory")):
            return "Infrastructure APIs"
        if n.startswith(("jira", "confluence", "n8n")):
            return "Notifications"
        if n.startswith(("webhook",)):
            return "Webhooks"
        if n.startswith(("langsmith",)):
            return "Observability"
        if n in (
            "dlp_fail_closed",
            "scan_readonly_enforce",
            "integration_approval_required",
        ) or n.startswith("scripts"):
            return "Security"
        return "Other"

    grouped: dict[str, list[SettingRow]] = {}
    for key, value in fields.items():
        grouped.setdefault(group_for(key), []).append(_setting_row(key, value))
    order = [
        "LLM",
        "Database",
        "Infrastructure APIs",
        "Notifications",
        "Security",
        "Webhooks",
        "Observability",
        "Other",
    ]
    all_groups = [SettingGroup(group=g, rows=grouped[g]) for g in order if g in grouped]
    total = len(all_groups)
    page = all_groups[offset : offset + limit]
    return SettingsPageOut(items=page, total=total, limit=limit, offset=offset)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Remediation action approval  (session-gated mirror of the webhook routes)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _action_uuid(action_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(action_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="action_id must be a valid UUID")


@governance_ops_router.post(
    "/actions/{action_id}/approve", status_code=200, response_model=ActionApproveResult
)
async def approve_remediation_action(
    action_id: str,
    request: Request,
    body: ActionApproveBody | None = None,
    _: None = Depends(require_admin),
):
    """Approve a human-gated ProposedAction (e.g. a remediation) from the dashboard.

    Session-gated mirror of the webhook `POST /actions/{id}/approve` route, so the
    browser can approve without handling the webhook secret. Approving is an
    elevated operation, so it additionally requires an admin session
    (same gate as agent-config). Approval only flips the action's *status* — it
    never mutates managed infrastructure; the proposing agent executes the
    approved action on its next run. Confidence â‰¥0.7 is required.

    Phase 3 Task 4: async handler — the sync DB work runs in a worker thread
    (asyncio.to_thread; CLAUDE.md #2/#3), then the parked interrupt graph (if
    any) is resumed via the shared, NON-fatal resume helper. The DB flip has
    already committed, so a resume failure only defers execution to the poll.
    """
    user = current_user(request) or {}
    approved_by = (body.approved_by if body else None) or user.get("username") or "dashboard"

    def _approve() -> SimpleNamespace:
        # Guards + field writes live in action_decisions.approve_action, shared
        # verbatim with mcp_server.approve_proposal so the two surfaces cannot
        # drift. Only the transport translation (ActionDecisionError ->
        # HTTPException) is route-specific.
        with get_session() as session:
            try:
                return approve_action(session, _action_uuid(action_id), approved_by)
            except ActionDecisionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    snapshot = await asyncio.to_thread(_approve)
    await resume_remediation_action(snapshot, approved=True)
    return {"approved": True, "action_id": action_id}


@governance_ops_router.post(
    "/actions/{action_id}/reject", status_code=200, response_model=ActionRejectResult
)
async def reject_remediation_action(
    action_id: str,
    _: None = Depends(require_admin),
):
    """Reject a human-gated ProposedAction from the dashboard.

    Session-gated mirror of the webhook `POST /actions/{id}/reject` route. Sets
    the action's status to ``rejected``; never mutates managed infrastructure.
    Async handler — DB work in a worker thread, then the parked interrupt graph
    (if any) is resumed with a rejection so its thread finishes cleanly.
    """

    def _reject() -> SimpleNamespace:
        # Shared with mcp_server.reject_proposal — see _approve above.
        with get_session() as session:
            try:
                return reject_action(session, _action_uuid(action_id))
            except ActionDecisionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    snapshot = await asyncio.to_thread(_reject)
    await resume_remediation_action(snapshot, approved=False)
    return {"rejected": True, "action_id": action_id}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Agent configuration status
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# `_DOMAIN_REQUIREMENTS` / `_SECRET_KEYS` / `_is_secret` MOVED to
# `infra_brain.api._helpers` (imported at the top of this module, so every
# reference below and every `from .governance_ops import _DOMAIN_REQUIREMENTS`
# elsewhere still resolves unchanged). `routers/sources.py` needs the same
# "is this domain configured?" table, and a router importing a private name out
# of a sibling router is the cycle `routers/cloudnet.py`'s header forbids — so
# the one copy lives in the shared helper module both import from.


@governance_ops_router.get("/agent-config", response_model=AgentConfigPageOut)
def list_agent_config(limit: int = 100, offset: int = 0):
    """Return per-domain configuration status: last run, error, and what env vars are needed."""
    from infra_brain.config import get_settings
    from infra_brain.runtime_flags import paused_domains
    from infra_brain.webhooks import KNOWN_DOMAINS

    s = get_settings()
    # One fleet-wide read of the live dispatchable__<domain> overrides.
    paused = paused_domains()

    # Get latest run per domain
    latest_runs: dict[str, CollectionRun] = {}
    with get_session() as session:
        for domain in KNOWN_DOMAINS:
            run = (
                session.query(CollectionRun)
                .filter(CollectionRun.domain == domain)
                .order_by(CollectionRun.started_at.desc())
                .first()
            )
            if run:
                latest_runs[domain] = run

    out = []
    for domain in sorted(KNOWN_DOMAINS):
        reqs_spec = _DOMAIN_REQUIREMENTS.get(domain, [])
        requirements = []
        all_configured = True

        for req in reqs_spec:
            attr = req.get("attr")
            if attr:
                raw = getattr(s, attr, "") or ""
            else:
                import os

                raw = os.environ.get(req["key"], "")

            configured = bool(raw)
            if not configured:
                all_configured = False

            display = None
            if configured and not _is_secret(req["key"]):
                display = raw
            elif configured:
                display = "***"

            requirements.append(
                AgentConfigRequirement(
                    key=req["key"],
                    label=req["label"],
                    configured=configured,
                    current_value=display,
                )
            )

        run = latest_runs.get(domain)
        out.append(
            AgentConfigOut(
                domain=domain,
                last_status=_RUN_STATUS.get(run.status, run.status) if run else None,
                last_error=run.error_message if run else None,
                last_run_at=run.started_at if run else None,
                requirements=requirements,
                ready=all_configured or not reqs_spec,
                paused=domain in paused,
            )
        )

    total = len(out)
    page = out[offset : offset + limit]
    return AgentConfigPageOut(items=page, total=total, limit=limit, offset=offset)
