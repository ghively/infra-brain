"""Pydantic response-model classes for the infra-brain dashboard API.

Extracted from dashboard_api.py (Task 2 — Phase 3 god-file split).
Class bodies are byte-identical to the originals; no logic changes.

Import rules:
  * MAY import from standard library and pydantic.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * infra_brain.dashboard_api imports FROM this module via `from ... import *`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from infra_brain.api._envelope import PageEnvelope
from infra_brain.chat.limits import CHAT_MAX_MESSAGE_CHARS

__all__ = [
    # Envelope base (Task 5.2)
    "PageEnvelope",
    # Resources
    "KV",
    "ResourceOut",
    "ResourcePageOut",
    "SnapshotOut",
    "SeedResourceBody",
    "BulkSeedBody",
    # Linux host detail
    "LinuxPackageOut",
    "LinuxServiceOut",
    "LinuxUserOut",
    "LinuxPortOut",
    "LinuxCronOut",
    "LinuxMountOut",
    "LinuxNicOut",
    "LinuxPendingUpdateOut",
    "LinuxDetailOut",
    # Windows
    "WindowsServiceOut",
    "WindowsPatchOut",
    # Host Purpose Map
    "HostPurposeEntry",
    "HostPurposeUpdate",
    "HostPurposeWriteResult",
    # Resource Ownership (issue #116)
    "ResourceOwnershipOut",
    "ResourceOwnershipUpdate",
    # Drift
    "DriftTrendPoint",
    "DriftTrendOut",
    "DriftOut",
    "DriftPageOut",
    # Vulnerabilities
    "VulnOut",
    "VulnPageOut",
    # Counts
    "CountsOut",
    # EOL
    "EolOut",
    "EolMigrationUpdate",
    # Collection Runs
    "RunOut",
    "CollectionRunPageOut",
    # Sweeps
    "SweepStatusCountsOut",
    "SweepDomainOut",
    "SweepSummaryOut",
    "SweepListOut",
    "SweepDetailOut",
    # Scan Points
    "ScanOut",
    "ScanPointPageOut",
    # System health
    "SystemHealthPageOut",
    # Notifications
    "NotificationOut",
    "NotificationPageOut",
    # Intelligence
    "InstinctOut",
    "InstinctPageOut",
    "ObservationOut",
    "ObservationPageOut",
    "ScriptOut",
    "GeneratedScriptPageOut",
    "ProposalOut",
    "IntegrationProposalPageOut",
    # Activity / Audit
    "ActivityOut",
    "ActivityPageOut",
    "DecisionOut",
    "DecisionPageOut",
    "AuditOut",
    "AuditPageOut",
    # Inventory Reconcile
    "InventoryReconcileOut",
    "InventoryReconcilePageOut",
    # Remediation
    "RemediationOut",
    "RemediationPageOut",
    "RemediationRollupOut",
    "RemediationRollupRowOut",
    # Compliance
    "ComplianceOut",
    "CompliancePageOut",
    # Agents
    "AgentOut",
    "AgentRosterPageOut",
    # Settings / Health
    "SettingRow",
    "SettingGroup",
    "SettingsPageOut",
    "UiSettingsPageOut",
    "HealthItem",
    # Chat
    "ChatRequest",
    # Custom View (streaming + CRUD)
    "CustomViewRequest",
    "CustomViewCreate",
    "CustomViewUpdate",
    "CustomViewOut",
    # Actions
    "ActionApproveBody",
    "ActionApproveResult",
    "ActionRejectResult",
    "IncidentAckBody",
    "IncidentAckResult",
    # In-app ops-alert receiver (TRK-135 / TRK-329)
    "OpsAlertIn",
    "OpsAlertReceivedOut",
    # Agent config
    "AgentConfigRequirement",
    "AgentConfigOut",
    "AgentConfigPageOut",
    # IaC
    "IacPipelineRunOut",
    "IacProjectOut",
    "IacSummaryOut",
    "IacOverviewOut",
    # vSphere
    "VsphereDatacenterOut",
    "VsphereClusterOut",
    "VsphereHostOut",
    "VsphereVmOut",
    "VsphereDatastoreOut",
    "VsphereSummaryOut",
    "VsphereOverviewOut",
    # Fleet
    "FleetAssetOut",
    "FleetOsCountOut",
    "FleetRiskBandOut",
    "FleetSummaryOut",
    "FleetAssetsOut",
    # Software
    "SoftwareAggRowOut",
    "SoftwareDetailRowOut",
    "SoftwareSummaryOut",
    "SoftwareInventoryOut",
    # CVE
    "CveSolutionOut",
    "CveListItemOut",
    "CveListOut",
    "CveAffectedHostOut",
    "CveDetailOut",
    # Hosts
    "HostsPageOut",
    "SnapshotPageOut",
    "EolPageOut",
    "HostVulnHeaderOut",
    "HostVulnItemOut",
    "HostVulnsOut",
    # Custom Views
    "CustomViewPageOut",
]

# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


class KV(BaseModel):
    k: str
    v: str


class ResourceOut(BaseModel):
    id: str
    hostname: str
    domain: str
    resource_type: str
    zone: str
    status: str
    last_seen_at: datetime
    drift_count: int = 0
    meta: list[KV] = []


class ResourcePageOut(PageEnvelope):
    items: list[ResourceOut]
    total: int
    limit: int
    offset: int


class SnapshotOut(BaseModel):
    ts: datetime
    label: str


class SnapshotPageOut(PageEnvelope):
    items: list[SnapshotOut]


class SeedResourceBody(BaseModel):
    hostname: str
    domain: str
    resource_type: str = "host"
    ip_address: str | None = None
    os_name: str | None = None
    environment: str | None = None
    tags: list | None = None
    metadata: dict | None = None
    source: str = "manual"


class BulkSeedBody(BaseModel):
    resources_yaml: str | None = None
    resources: list | None = None


# ---------------------------------------------------------------------------
# Linux host detail
# ---------------------------------------------------------------------------


class LinuxPackageOut(BaseModel):
    name: str
    version: str
    manager: str
    installed_at: datetime | None = None


class LinuxServiceOut(BaseModel):
    name: str
    state: str
    enabled: bool
    last_checked: datetime


class LinuxUserOut(BaseModel):
    username: str
    shell: str
    sudo: bool
    last_login: datetime | None = None


class LinuxPortOut(BaseModel):
    port: int
    proto: str
    process: str | None = None
    state: str


class LinuxCronOut(BaseModel):
    owner: str
    schedule: str
    command: str


class LinuxMountOut(BaseModel):
    mount: str
    device: str | None = None
    fstype: str | None = None
    size_total_gb: float | None = None
    size_available_gb: float | None = None


class LinuxNicOut(BaseModel):
    name: str
    mac: str | None = None
    ipv4: str | None = None
    ipv6: str | None = None
    speed_mbps: int | None = None


class LinuxPendingUpdateOut(BaseModel):
    package: str
    current_version: str | None = None
    available_version: str | None = None
    security: bool = False
    manager: str | None = None


class LinuxDetailOut(BaseModel):
    resource_id: str
    hostname: str
    distro: str | None = None
    kernel: str | None = None
    arch: str | None = None
    packages: list[LinuxPackageOut] = []
    services: list[LinuxServiceOut] = []
    users: list[LinuxUserOut] = []
    ports: list[LinuxPortOut] = []
    crons: list[LinuxCronOut] = []
    # UI-2 (#58, corrected scope): mounts/nics/pending-updates. Defaults keep
    # this change backward-compatible for any existing consumer.
    mounts: list[LinuxMountOut] = []
    nics: list[LinuxNicOut] = []
    pending_updates: list[LinuxPendingUpdateOut] = []


# ---------------------------------------------------------------------------
# Windows Patch State
# ---------------------------------------------------------------------------


class WindowsServiceOut(BaseModel):
    name: str
    state: str | None = None
    start_type: str | None = None
    path: str | None = None


class WindowsPatchOut(BaseModel):
    hostname: str
    kb_list: list[str]
    pending_count: int
    last_patched: datetime | None = None
    winrm_status: str
    # UI-2 (#58, corrected scope): Windows services. Default keeps this
    # change backward-compatible for any existing consumer.
    services: list[WindowsServiceOut] = []


# ---------------------------------------------------------------------------
# Host Purpose Map (MR-J item 3)
# ---------------------------------------------------------------------------


class HostPurposeMapOut(BaseModel):
    hostname: str
    purpose: str | None = None
    vlan: str | None = None
    subnet: str | None = None
    source: str
    updated_at: datetime


class HostPurposeEntry(BaseModel):
    """Read model for a single host's purpose/VLAN/subnet entry.

    ``provenance`` is derived server-side from ``source``: unset when source is
    empty, ``ui`` for human dashboard edits (``ui:<username>``), else ``repo``
    (repo-sync rows carry a ``<project_id>:<file>`` source).
    """

    hostname: str
    purpose: str | None = None
    vlan: str | None = None
    subnet: str | None = None
    source: str | None = None
    updated_at: datetime | None = None
    provenance: Literal["repo", "ui", "unset"]


class HostPurposeUpdate(BaseModel):
    """Request body for a human dashboard edit of a host's purpose/VLAN/subnet."""

    purpose: str | None = None
    vlan: str | None = None
    subnet: str | None = None


class HostPurposeWriteResult(BaseModel):
    """Result of a human-authoritative purpose edit: the persisted row plus the
    trailing-MR outcome. ``mr_url`` is set on MR success; ``mr_error`` carries the
    failure reason when the trailing persistence MR fails (the DB edit is
    authoritative and is NEVER rolled back on MR failure)."""

    hostname: str
    purpose: str | None = None
    vlan: str | None = None
    subnet: str | None = None
    source: str | None = None
    updated_at: datetime | None = None
    provenance: Literal["repo", "ui", "unset"]
    mr_url: str | None = None
    mr_error: str | None = None


# ---------------------------------------------------------------------------
# Resource Ownership (issue #116) — owner_team/on_call_rotation/criticality_tier
# ---------------------------------------------------------------------------


class ResourceOwnershipOut(BaseModel):
    """Read model for a single resource's ownership/on-call/criticality entry.

    A missing row returns 200 with all-null fields rather than 404, matching
    the HostPurposeMap ``/purpose`` GET contract, so the dashboard can render
    an empty editable form."""

    resource_id: str
    owner_team: str | None = None
    on_call_rotation: str | None = None
    criticality_tier: str | None = None
    source: str | None = None
    updated_at: datetime | None = None


class ResourceOwnershipUpdate(BaseModel):
    """Request body for a human dashboard edit of a resource's ownership."""

    owner_team: str | None = None
    on_call_rotation: str | None = None
    criticality_tier: str | None = None


# ---------------------------------------------------------------------------
# MCP scoped API keys (auth overhaul)
# ---------------------------------------------------------------------------


class McpKeyOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    created_by: str
    last_used_at: datetime | None = None
    revoked: bool
    allowed_tools_count: int
    # TRK-160. `expires_at is None` = never expires. `expired` is a DERIVED
    # convenience the server computes so the UI never re-implements the
    # timezone-sensitive comparison (see mcp_auth._as_utc) client-side and
    # never disagrees with what auth actually enforces. Deliberately distinct
    # from `revoked`: both deny, but only one is somebody's deliberate act.
    expires_at: datetime | None = None
    expired: bool = False


class McpKeyPageOut(BaseModel):
    items: list[McpKeyOut]
    total: int


class McpToolCatalogOut(BaseModel):
    readonly: list[str]
    mutation: list[str]
    groups: dict[str, list[str]] = {}


def _validate_known_tool_names(v: list[str]) -> list[str]:
    """Shared allowed_tools check for both create and amend bodies — unknown
    tool names 422 rather than silently persisting. Not re-implemented per
    body: both McpKeyCreateBody and McpKeyUpdateBody call this same helper."""
    from infra_brain import mcp_auth

    unknown = [t for t in v if t not in mcp_auth.ALL_TOOL_NAMES]
    if unknown:
        raise ValueError(f"unknown tool name(s): {unknown}")
    return v


class McpKeyCreateBody(BaseModel):
    # DB column is String(128) (McpApiKey.name, db/models/core.py) — bound the
    # length here so an overlong name is a clean 422, not an unhandled DataError.
    name: str = Field(min_length=1, max_length=128)
    allowed_tools: list[str]
    # TRK-160: optional expiry, omitted/null = never expires (the historical
    # behavior, so no existing client breaks).
    expires_days: int | None = None

    @field_validator("allowed_tools")
    @classmethod
    def allowed_tools_known(cls, v: list[str]) -> list[str]:
        return _validate_known_tool_names(v)

    @field_validator("expires_days")
    @classmethod
    def expires_days_in_range(cls, v: int | None) -> int | None:
        """Validate against mcp_auth's own rule rather than restating the
        bounds as pydantic ``ge``/``le`` here — one source of truth for
        "1..MAX_EXPIRES_DAYS", so the route, the CLI and this body can never
        drift apart. The lazy import matches ``_validate_known_tool_names``
        above and this module's stated import discipline. Pydantic converts the
        raised ValueError into a 422.
        """
        from infra_brain import mcp_auth

        mcp_auth.expiry_from_days(v)
        return v


class McpKeyCreateResult(BaseModel):
    id: str
    name: str
    # The raw token, returned exactly once on creation and never persisted raw.
    token: str
    allowed_tools: list[str]
    # Echoes the COMPUTED absolute instant (not the requested day count) so the
    # operator can see exactly when the key dies without recomputing it, and
    # None when it never expires.
    expires_at: datetime | None = None


class McpKeyRevokeResult(BaseModel):
    id: str
    revoked: bool


class McpKeyUpdateBody(BaseModel):
    """PATCH body for amending an existing key's tool scope. No name/token
    fields — this route only ever touches allowed_tools."""

    allowed_tools: list[str]

    @field_validator("allowed_tools")
    @classmethod
    def allowed_tools_known(cls, v: list[str]) -> list[str]:
        return _validate_known_tool_names(v)


class McpKeyUpdateResult(BaseModel):
    id: str
    name: str
    # Echoes the full new scope so the caller/UI can confirm exactly what was
    # granted, per the misuse-analysis mitigation in the implementation plan.
    allowed_tools: list[str]


# ---------------------------------------------------------------------------
# Drift Trend
# ---------------------------------------------------------------------------


class DriftTrendPoint(BaseModel):
    date: str
    count: int
    domain: str


class DriftTrendOut(BaseModel):
    points: list[DriftTrendPoint]
    total: int
    domain_filter: str
    days: int


# ---------------------------------------------------------------------------
# Drift Events
# ---------------------------------------------------------------------------


class DriftOut(BaseModel):
    id: str
    domain: str
    hostname: str
    field_name: str
    old_value: str
    new_value: str
    detected_at: datetime
    status: str
    jira_ticket: str = "—"
    drift_type: str
    root_cause: str = ""
    # Drift readability. All three are DERIVED at read time by
    # infra_brain.drift_taxonomy from the four columns above — no schema
    # change, no migration, and no staleness when the phrasing improves.
    #
    # summary  — one plain sentence: WHAT drifted ("node_a started listening
    #            on port 8443 (was not listening)"). The Drift table's primary
    #            cell; `field_name`/`old_value`/`new_value` alone required
    #            archaeology to read.
    # rule     — one line: WHY this was flagged (which collector rule raised
    #            it). The other half of the same complaint.
    # *_display — old/new with the writer-specific JSON envelope peeled off
    #            ({"v": "node_a"} → node_a), for the drawer's before/after
    #            diff. `old_value`/`new_value` keep their exact prior
    #            stringification so no existing consumer shifts under foot.
    #
    # Defaulted so any consumer built against the pre-readability contract
    # keeps validating.
    summary: str = ""
    rule: str = ""
    old_display: str = ""
    new_display: str = ""


class DriftPageOut(PageEnvelope):
    items: list[DriftOut]


# ---------------------------------------------------------------------------
# Vulnerabilities
# ---------------------------------------------------------------------------


class VulnOut(BaseModel):
    cve: str
    host: str
    pkg: str
    severity: str
    cvss: float = 0.0
    sla: str
    status: str
    priority: int = 0
    pci_fail: bool = False
    exploits: int = 0


class VulnPageOut(PageEnvelope):
    items: list[VulnOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Dashboard summary counts
# ---------------------------------------------------------------------------


class CountsOut(BaseModel):
    open_drift: int = 0
    open_cves: int = 0
    critical_cves: int = 0
    severe_cves: int = 0
    compliance_open: int = 0
    compliance_resolved: int = 0
    eol_overdue: int = 0
    eol_total: int = 0
    invrec_proposed: int = 0
    invrec_total: int = 0
    total_resources: int = 0
    applied_instincts: int = 0


# ---------------------------------------------------------------------------
# EOL Registry
# ---------------------------------------------------------------------------


class EolOut(BaseModel):
    id: str
    asset: str
    host: str
    eol: str
    # Named `pci_risk_score` (not the bare `risk` used before TRK-275/GitLab
    # #146+#153), and NOT to be confused with Rapid7's `risk_score` field
    # (R7Asset/HostIdentity — an unbounded exposure score, typically in the
    # hundreds/thousands) which lives on a completely different scale. This
    # field is EolRegistry.pci_risk_score verbatim: a 0-100 score computed
    # purely from EOL-date proximity by EOLAgent._pci_risk_score (past-EOL=90,
    # <90 days=70, <1yr=40, else=10) — never derived from or comparable to
    # Rapid7's field. Nullable: pci_risk_score is nullable in the DB
    # (os_inventory.py), and the frontend must be able to distinguish
    # "unscored" from "scored 0" when averaging risk. Do not default this to 0
    # here or in the router.
    pci_risk_score: int | None = None
    migration: str
    status: str


class EolPageOut(PageEnvelope):
    items: list[EolOut]


class EolMigrationUpdate(BaseModel):
    migration_path: str


# ---------------------------------------------------------------------------
# Collection Runs
# ---------------------------------------------------------------------------


class RunOut(BaseModel):
    domain: str
    trigger_type: str
    status: str
    records_collected: int
    detail_rows_written: int = 0
    started_at: datetime
    duration_seconds: int
    error_message: str | None = None


class CollectionRunPageOut(PageEnvelope):
    items: list[RunOut]


# ---------------------------------------------------------------------------
# Sweeps (Phase 4 Task 4 — sweep view)
# ---------------------------------------------------------------------------


class SweepStatusCountsOut(BaseModel):
    """Counts over ALL real CollectionRun.status values (spec §2.11 — pending
    interrupts must stay visible, not be folded into a generic "failed")."""

    completed: int = 0
    partial: int = 0
    failed: int = 0
    retry_exhausted: int = 0
    interrupt_pending: int = 0
    in_progress: int = 0
    skipped: int = 0


class SweepDomainOut(BaseModel):
    domain: str
    tier: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: int | None = None
    records_collected: int = 0
    error_message: str | None = None
    max_iters_hits: int = 0


class SweepSummaryOut(BaseModel):
    sweep_id: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: int | None = None
    domain_count: int
    status_counts: SweepStatusCountsOut
    max_iters_hit_count: int = 0


class SweepListOut(BaseModel):
    items: list[SweepSummaryOut]
    total: int
    limit: int


class SweepDetailOut(BaseModel):
    sweep_id: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: int | None = None
    status_counts: SweepStatusCountsOut
    max_iters_hit_count: int = 0
    domains: list[SweepDomainOut]


# ---------------------------------------------------------------------------
# Scan Schedule
# ---------------------------------------------------------------------------


class ScanOut(BaseModel):
    domain: str
    method: str
    endpoint: str
    schedule: str
    status: str
    next_run: str
    last_run: datetime | None = None
    last_success: datetime | None = None


class ScanPointPageOut(PageEnvelope):
    items: list[ScanOut]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class NotificationOut(BaseModel):
    type: str
    target: str
    title: str
    domain: str
    created: datetime
    status: str
    jira_url: str | None = None
    confluence_url: str | None = None


class NotificationPageOut(PageEnvelope):
    items: list[NotificationOut]


# ---------------------------------------------------------------------------
# Intelligence loop
# ---------------------------------------------------------------------------


class InstinctOut(BaseModel):
    domain: str
    zone: str
    pattern: str
    confidence: float
    promoted_at: str
    promoted_by: str = ""
    citation: str = ""
    applied: bool = False


class InstinctPageOut(PageEnvelope):
    items: list[InstinctOut]


class ObservationOut(BaseModel):
    agent: str
    tool: str
    domain: str
    pattern: str
    count: int
    last_seen: datetime


class ObservationPageOut(PageEnvelope):
    items: list[ObservationOut]


class ScriptOut(BaseModel):
    name: str
    language: str
    purpose: str
    run_count: int
    last_returncode: int
    created_by_agent: str
    domain: str = ""
    git_path: str = ""
    last_run_at: datetime | None
    created_at: datetime | None = None


class GeneratedScriptPageOut(PageEnvelope):
    items: list[ScriptOut]


class ProposalOut(BaseModel):
    id: str
    type: str
    endpoint: str
    confidence: float
    proposed_at: datetime
    status: str
    source: str


class IntegrationProposalPageOut(PageEnvelope):
    items: list[ProposalOut]


# ---------------------------------------------------------------------------
# Audit & activity
# ---------------------------------------------------------------------------


class ActivityOut(BaseModel):
    ts: datetime
    agent: str
    domain: str = ""
    tool: str
    verdict: str
    status: str = ""
    latency_ms: float
    args_summary: str
    error: str = ""


class ActivityPageOut(PageEnvelope):
    items: list[ActivityOut]


class DecisionOut(BaseModel):
    agent: str
    domain: str
    run_id: str
    iteration: int
    decision_summary: str
    reasoning_text: str
    tools_chosen: list[str]
    ts: datetime


class DecisionPageOut(PageEnvelope):
    items: list[DecisionOut]


class AuditOut(BaseModel):
    ts: datetime
    agent: str
    tool: str
    category: str
    allowed: bool
    reason: str
    ihash: str
    ohash: str


class AuditPageOut(PageEnvelope):
    items: list[AuditOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Inventory reconciliation
# ---------------------------------------------------------------------------


class InventoryReconcileOut(BaseModel):
    host: str
    domain: str
    target_group: str
    status: str
    mr_url: str
    detected_at: datetime


class InventoryReconcilePageOut(PageEnvelope):
    items: list[InventoryReconcileOut]


# ---------------------------------------------------------------------------
# Remediation proposals
# ---------------------------------------------------------------------------


class RemediationOut(BaseModel):
    id: str
    agent: str
    action_type: str
    target: str
    confidence: float
    status: str
    approved_by: str
    result_url: str
    created_at: datetime


class RemediationPageOut(PageEnvelope):
    items: list[RemediationOut]


class RemediationRollupRowOut(BaseModel):
    """One dedup group: same ``action_type`` + stable ``payload->>'field'``.

    See vuln.py's ``list_remediation(view="rollup")`` for why grouping uses
    ``field`` (a stable string) rather than the LLM-drafted ``payload->>'plan'``
    text (TRK-255).
    """

    action_type: str
    field: str | None
    count: int
    sample_targets: list[str]


class RemediationRollupOut(PageEnvelope):
    items: list[RemediationRollupRowOut]


# ---------------------------------------------------------------------------
# Compliance violations
# ---------------------------------------------------------------------------


class ComplianceOut(BaseModel):
    rule: str
    severity: str
    host: str
    detail: str
    status: str
    detected_at: datetime


class CompliancePageOut(PageEnvelope):
    items: list[ComplianceOut]


# ---------------------------------------------------------------------------
# Agents roster
# ---------------------------------------------------------------------------


class AgentOut(BaseModel):
    name: str
    domain: str
    kind: str
    schedule: str
    last_run: str
    status: str
    output: str
    desc: str = ""
    tools: list[str] = []


class AgentRosterPageOut(PageEnvelope):
    items: list[AgentOut]


# ---------------------------------------------------------------------------
# Settings & health
# ---------------------------------------------------------------------------


class SettingRow(BaseModel):
    k: str
    type: str
    v: str | None = None
    on: bool | None = None


class SettingGroup(BaseModel):
    group: str
    rows: list[SettingRow]


class HealthItem(BaseModel):
    name: str
    detail: str
    status: str


class SystemHealthPageOut(PageEnvelope):
    items: list[HealthItem]


class SettingsPageOut(PageEnvelope):
    """Settings grouped by category; each item is a SettingGroup."""

    items: list[SettingGroup]


class UiSettingsPageOut(PageEnvelope):
    """TRK-321: the narrow, non-sensitive settings subset any signed-in session
    may read, as a FLAT list of rows.

    Deliberately not `SettingsPageOut`: the grouped shape belongs to the
    admin-only full `model_dump()` view, and giving this endpoint its own
    response model keeps the two contracts visibly distinct so the narrow one
    can't be quietly widened back into the full one. Membership is governed by
    `governance_ops._UI_SETTINGS_ALLOWLIST`.
    """

    items: list[SettingRow]


# ---------------------------------------------------------------------------
# Chat (read-only streaming assistant)
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    # Bounded at the schema layer so an oversized paste is refused with a 422
    # BEFORE it is ever sent to the provider (denial-of-wallet: the message is
    # replayed into every subsequent turn of the thread by the checkpointer, so
    # one huge message is not a one-off cost). Rejected, not truncated — quietly
    # dropping half of what someone pasted and answering the rest is worse than
    # saying it was too long. See infra_brain.chat.limits.
    message: str = Field(..., max_length=CHAT_MAX_MESSAGE_CHARS)
    thread_id: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# Custom View (AI-generated OpenUI component tree, NDJSON stream)
# ---------------------------------------------------------------------------


class CustomViewRequest(BaseModel):
    prompt: str
    thread_id: str | None = None


# ---------------------------------------------------------------------------
# Custom Views CRUD (save/share/list/delete named OpenUI views)
# ---------------------------------------------------------------------------


class CustomViewCreate(BaseModel):
    title: str
    prompt: str
    openui_lang: str
    is_public: bool = False


class CustomViewUpdate(BaseModel):
    title: str | None = None
    is_public: bool | None = None


class CustomViewOut(BaseModel):
    id: str
    title: str
    prompt: str
    openui_lang: str
    share_token: str
    is_public: bool
    created_at: datetime
    share_url: str


class CustomViewPageOut(PageEnvelope):
    items: list[CustomViewOut]


# ---------------------------------------------------------------------------
# Remediation action approval
# ---------------------------------------------------------------------------


class ActionApproveBody(BaseModel):
    approved_by: str | None = None


class ActionApproveResult(BaseModel):
    approved: bool
    action_id: str


class ActionRejectResult(BaseModel):
    rejected: bool
    action_id: str


# ---------------------------------------------------------------------------
# Incident-management ack-in webhook (TRK-242 / GitLab #113)
# ---------------------------------------------------------------------------


class IncidentAckBody(BaseModel):
    """Inbound ack/resolve callback from an external incident-management tool
    (PagerDuty, Opsgenie, ...). ``incident_key`` is the dedup_key that was set
    on the originating DriftEvent/ComplianceViolation row(s) when the alert
    was sent (tools/ops_webhook.py send_ops_alert's dedup_key param)."""

    incident_key: str
    action: Literal["acknowledge", "resolve"] = "acknowledge"


class IncidentAckResult(BaseModel):
    acknowledged: bool
    incident_key: str
    updated: int


# ---------------------------------------------------------------------------
# In-app ops-alert receiver (TRK-135 / TRK-329)
# ---------------------------------------------------------------------------


class OpsAlertIn(BaseModel):
    """Inbound ops alert for POST /webhooks/ops-alert.

    This is NOT a new schema — it is the union of what the two EXISTING
    producers already POST, and nothing more:

      * ``tools/ops_webhook.py::send_ops_alert`` sends
        ``{"category": str, "messages": [str], "dedup_key"?: str}``;
      * ``docker/deadman-prober/prober.py::build_alert_payload`` sends
        ``{"category": str, "messages": [str]}`` (no dedup_key).

    Liberal in what it accepts, on purpose: a rejected alert is a LOST alert,
    which is the exact failure mode this endpoint exists to end. So every field
    has a default, unknown keys are ignored (pydantic's default), and a bare
    string in ``messages`` is coerced to a one-element list rather than 422'd.
    """

    category: str = "unspecified"
    messages: list[str] = Field(default_factory=list)
    dedup_key: str | None = None

    @field_validator("messages", mode="before")
    @classmethod
    def _coerce_messages(cls, v):
        """Accept a bare string (or None) where a list of strings is expected."""
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [m if isinstance(m, str) else str(m) for m in v]
        return [str(v)]


class OpsAlertReceivedOut(BaseModel):
    received: bool
    category: str
    messages: int


# ---------------------------------------------------------------------------
# Integration proposal approval (CoverageAgent.wire())
# ---------------------------------------------------------------------------


class IntegrationProposalApproveBody(BaseModel):
    approved_by: str | None = None


class IntegrationProposalApproveResult(BaseModel):
    # Named distinctly from webhooks.py's pre-existing ProposalApproveResult
    # (approved/proposal_id only, no wired field) — that class is still used
    # by webhooks.py's separate /integrations/{id}/approve route and must not
    # be redefined here (F811 collision found in code review of #93).
    approved: bool
    wired: bool
    proposal_id: str


class IntegrationProposalRejectResult(BaseModel):
    rejected: bool
    proposal_id: str


# ---------------------------------------------------------------------------
# Agent configuration status
# ---------------------------------------------------------------------------


class AgentConfigRequirement(BaseModel):
    key: str
    label: str
    configured: bool
    current_value: str | None = None  # masked for secrets


class AgentConfigOut(BaseModel):
    domain: str
    last_status: str | None = None
    last_error: str | None = None
    last_run_at: datetime | None = None
    requirements: list[AgentConfigRequirement]
    ready: bool
    # Live dispatchable__<domain>=false override (runtime_flags.paused_domains).
    # Echoed back so the UI can render the CURRENT pause state on load instead
    # of only knowing about a pause the user performed in this browser session.
    paused: bool = False


class AgentConfigPageOut(PageEnvelope):
    items: list[AgentConfigOut]


# ---------------------------------------------------------------------------
# Source inventory — what the UI should show, derived not declared
# ---------------------------------------------------------------------------


class SourceOut(BaseModel):
    """One data source (= one collector domain) and the UI's verdict on it.

    Every field is derived at request time from ``etl.spec.agent_specs()`` plus
    the database — there is no hardcoded source list anywhere behind this — so a
    collector added tomorrow appears here, and in the sidebar, with no edit to
    this schema or to the frontend.
    """

    domain: str
    #: Switched off by standing decision right now (folds in COLLECTION_REVIVED_DOMAINS).
    retired: bool
    #: Convenience inverse of `retired`, so the client never re-derives the polarity.
    live: bool
    #: Every env var the domain declares has a value. True when it declares none.
    configured: bool
    #: Declared-but-unset env var KEYS (never values) — safe to render verbatim.
    missing_config: list[str] = []
    #: Rows this domain owns in `resources`. The input to the visibility rule.
    resource_count: int = 0
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    #: "visible" | "historical" | "hidden" — see api/source_visibility.py.
    visibility: str
    visibility_reason: str


class SourceInventoryPageOut(PageEnvelope):
    items: list[SourceOut]
    #: Counts by verdict, e.g. {"visible": 39, "historical": 1, "hidden": 6}. A
    #: sidecar with a default so the degraded envelope still deserializes.
    by_visibility: dict[str, int] = {}


# ---------------------------------------------------------------------------
# IaC / Pipelines — dedicated view
# ---------------------------------------------------------------------------


class IacPipelineRunOut(BaseModel):
    pipeline_id: int
    ref: str = ""
    status: str = ""
    source: str = ""
    created_at: datetime | None = None
    duration: int | None = None
    web_url: str = ""


class IacProjectOut(BaseModel):
    gitlab_project_id: int
    name: str
    path_with_namespace: str = ""
    default_branch: str = ""
    visibility: str = ""
    archived: bool = False
    last_pipeline_status: str = ""
    last_pipeline_ref: str = ""
    last_activity_at: datetime | None = None
    file_count: int = 0
    files_by_type: dict[str, int] = {}
    recent_pipelines: list[IacPipelineRunOut] = []


class IacSummaryOut(BaseModel):
    project_count: int = 0
    file_count: int = 0
    files_by_type: dict[str, int] = {}
    pipeline_run_count: int = 0


class IacOverviewOut(BaseModel):
    projects: list[IacProjectOut] = []
    summary: IacSummaryOut = IacSummaryOut()


# ---------------------------------------------------------------------------
# IaC deep views (UI Batch 6 / GitLab #62) — Compose/K8s/Terraform resource
# listings, Ansible inventory/playbook structure, CI schedules.
# ---------------------------------------------------------------------------


class ComposeServiceOut(BaseModel):
    service_name: str
    image: str = ""
    ports: list = []
    project_name: str = ""
    file_path: str = ""


class ComposeServicesPageOut(PageEnvelope):
    items: list[ComposeServiceOut] = []
    total: int = 0
    limit: int = 200
    offset: int = 0


class K8sManifestResourceOut(BaseModel):
    kind: str
    name: str
    namespace: str = ""
    api_version: str = ""
    project_name: str = ""
    file_path: str = ""


class K8sManifestResourcesPageOut(PageEnvelope):
    items: list[K8sManifestResourceOut] = []
    total: int = 0
    limit: int = 200
    offset: int = 0


class TerraformResourceOut(BaseModel):
    resource_type: str
    resource_name: str
    project_name: str = ""
    file_path: str = ""


class TerraformResourcesPageOut(PageEnvelope):
    items: list[TerraformResourceOut] = []
    total: int = 0
    limit: int = 200
    offset: int = 0


class AnsibleInventoryHostOut(BaseModel):
    name: str


class AnsibleInventoryGroupOut(BaseModel):
    name: str
    project_name: str = ""
    file_path: str = ""
    hosts: list[AnsibleInventoryHostOut] = []


class AnsiblePlaybookPlayOut(BaseModel):
    name: str = ""
    play_index: int = 0
    hosts: list = []
    project_name: str = ""
    file_path: str = ""


class AnsibleStructureOut(BaseModel):
    groups: list[AnsibleInventoryGroupOut] = []
    plays: list[AnsiblePlaybookPlayOut] = []


class CiScheduleOut(BaseModel):
    project_id: int
    schedule_id: int
    description: str = ""
    ref: str = ""
    cron: str = ""
    active: bool | None = None
    created_at: datetime | None = None


class CiSchedulesPageOut(PageEnvelope):
    items: list[CiScheduleOut] = []
    total: int = 0
    limit: int = 200
    offset: int = 0


# ---------------------------------------------------------------------------
# vSphere / Virtualization — dedicated view
# ---------------------------------------------------------------------------


class VsphereDatacenterOut(BaseModel):
    name: str
    cluster_count: int = 0
    host_count: int = 0
    vm_count: int = 0


class VsphereClusterOut(BaseModel):
    name: str
    datacenter_name: str = ""
    num_hosts: int | None = None
    drs_enabled: bool | None = None
    ha_enabled: bool | None = None
    total_memory_gb: float | None = None
    overall_status: str = ""


class VsphereHostOut(BaseModel):
    name: str
    cluster_name: str = ""
    vendor: str = ""
    model: str = ""
    version: str = ""
    connection_state: str = ""
    power_state: str = ""
    in_maintenance_mode: bool | None = None
    vm_count: int | None = None
    memory_gb: float | None = None
    overall_status: str = ""


class VsphereVmOut(BaseModel):
    name: str
    esxi_host: str = ""
    power_state: str = ""
    guest_full_name: str = ""
    num_cpu: int | None = None
    memory_mb: int | None = None
    ip_address: str = ""
    tools_status: str = ""
    overall_status: str = ""


class VsphereDatastoreOut(BaseModel):
    name: str
    datastore_type: str = ""
    capacity_gb: float | None = None
    free_gb: float | None = None
    used_pct: float | None = None
    accessible: bool | None = None


class VsphereSummaryOut(BaseModel):
    datacenter_count: int = 0
    cluster_count: int = 0
    host_count: int = 0
    vm_count: int = 0
    template_count: int = 0
    datastore_count: int = 0
    network_count: int = 0
    total_capacity_gb: float = 0.0
    total_free_gb: float = 0.0
    latest_metric_at: datetime | None = None


class VsphereOverviewOut(BaseModel):
    datacenters: list[VsphereDatacenterOut] = []
    clusters: list[VsphereClusterOut] = []
    hosts: list[VsphereHostOut] = []
    vms: list[VsphereVmOut] = []
    datastores: list[VsphereDatastoreOut] = []
    summary: VsphereSummaryOut = VsphereSummaryOut()


# ---------------------------------------------------------------------------
# vSphere secondary views (UI-3 / #59) — networks (full rows, superseding the
# count-only usage in VsphereOverviewOut), resource pools, VM disks, snapshots
# (with a stale-snapshot hygiene flag), licenses, alarms, permissions.
# Sessions and the two metric time-series tables are explicitly out of scope
# for this batch — see the plan's Global Constraints.
# ---------------------------------------------------------------------------


class VsphereNetworkOut(BaseModel):
    name: str
    network_kind: str
    accessible: bool | None = None
    num_ports: int | None = None
    host_count: int | None = None
    vm_count: int | None = None


class VsphereResourcePoolOut(BaseModel):
    name: str
    cpu_limit: int | None = None
    cpu_reservation: int | None = None
    memory_limit: int | None = None
    memory_reservation: int | None = None
    vm_count: int | None = None


class VsphereVmDiskOut(BaseModel):
    vm_name: str = ""
    label: str | None = None
    capacity_gb: float | None = None
    thin_provisioned: bool | None = None
    backing_type: str | None = None
    datastore_name: str | None = None


class VsphereSnapshotOut(BaseModel):
    vm_name: str = ""
    name: str | None = None
    created_at: datetime | None = None
    age_days: int | None = None
    is_current: bool | None = None
    state: str | None = None


class VsphereLicenseOut(BaseModel):
    name: str | None = None
    edition_key: str | None = None
    total: int | None = None
    used: int | None = None
    expiration: datetime | None = None


class VsphereAlarmOut(BaseModel):
    alarm_name: str | None = None
    entity_name: str | None = None
    entity_type: str | None = None
    overall_status: str | None = None
    acknowledged: bool | None = None
    triggered_at: datetime | None = None


class VspherePermissionOut(BaseModel):
    principal: str | None = None
    role_name: str | None = None
    is_group: bool | None = None
    propagate: bool | None = None
    entity: str | None = None


class VsphereSecondarySummaryOut(BaseModel):
    network_count: int = 0
    resource_pool_count: int = 0
    vm_disk_count: int = 0
    snapshot_count: int = 0
    stale_snapshot_count: int = 0
    license_count: int = 0
    alarm_count: int = 0
    permission_count: int = 0


class VsphereSecondaryOut(BaseModel):
    networks: list[VsphereNetworkOut] = []
    resource_pools: list[VsphereResourcePoolOut] = []
    vm_disks: list[VsphereVmDiskOut] = []
    snapshots: list[VsphereSnapshotOut] = []
    licenses: list[VsphereLicenseOut] = []
    alarms: list[VsphereAlarmOut] = []
    permissions: list[VspherePermissionOut] = []
    summary: VsphereSecondarySummaryOut = VsphereSecondarySummaryOut()


__all__ += [
    "VsphereAlarmOut",
    "VsphereLicenseOut",
    "VsphereNetworkOut",
    "VspherePermissionOut",
    "VsphereResourcePoolOut",
    "VsphereSecondaryOut",
    "VsphereSecondarySummaryOut",
    "VsphereSnapshotOut",
    "VsphereVmDiskOut",
]


# ---------------------------------------------------------------------------
# Cloud / K8s / Net typed views (UI Batch 7 / GitLab #63) — POC-disabled
# domains (TRK-041); plumbing built ahead of enablement, per design spec.
# ---------------------------------------------------------------------------


class CloudResourceOut(BaseModel):
    provider: str
    cloud_type: str
    cloud_id: str
    name: str
    region: str = ""
    state: str = ""


class K8sNodeOut(BaseModel):
    cluster: str = ""
    name: str
    status: str = ""
    roles: list = []
    kubelet_version: str = ""
    arch: str = ""


class K8sPodOut(BaseModel):
    cluster: str = ""
    namespace: str
    name: str
    phase: str = ""
    node_name: str = ""


class K8sDeploymentOut(BaseModel):
    cluster: str = ""
    namespace: str
    name: str
    replicas: int | None = None
    ready: int | None = None
    available: int | None = None


class NetDeviceOut(BaseModel):
    ip: str
    name: str
    sysname: str = ""
    contact: str = ""
    location: str = ""


class CloudNetSummaryOut(BaseModel):
    cloud_resource_count: int = 0
    k8s_node_count: int = 0
    k8s_pod_count: int = 0
    k8s_deployment_count: int = 0
    net_device_count: int = 0


class CloudNetOverviewOut(BaseModel):
    cloud_resources: list[CloudResourceOut] = []
    k8s_nodes: list[K8sNodeOut] = []
    k8s_pods: list[K8sPodOut] = []
    k8s_deployments: list[K8sDeploymentOut] = []
    net_devices: list[NetDeviceOut] = []
    summary: CloudNetSummaryOut = CloudNetSummaryOut()



# ---------------------------------------------------------------------------
# Fleet Assets — dedicated view (Rapid7 asset inventory)
# ---------------------------------------------------------------------------


class FleetAssetOut(BaseModel):
    id: str
    r7_asset_id: int
    hostname: str
    ip: str = ""
    os: str = ""
    os_product: str = ""
    os_version: str = ""
    os_vendor: str = ""
    asset_type: str = ""
    risk_score: float = 0.0
    risk_band: str = "low"
    vuln_critical: int = 0
    vuln_severe: int = 0
    vuln_moderate: int = 0
    vuln_total: int = 0
    vuln_exploits: int = 0
    assessed: bool = False
    config_count: int = 0


class FleetOsCountOut(BaseModel):
    os_product: str
    count: int


class FleetRiskBandOut(BaseModel):
    band: str
    count: int


class FleetSummaryOut(BaseModel):
    total_assets: int = 0
    assessed_assets: int = 0
    total_critical: int = 0
    total_severe: int = 0
    by_os: list[FleetOsCountOut] = []
    by_risk_band: list[FleetRiskBandOut] = []


class FleetAssetsOut(PageEnvelope):
    items: list[FleetAssetOut] = []
    total: int = 0
    limit: int = 100
    offset: int = 0
    # summary has a default so the degraded body {items:[],total:0,...} validates
    # without needing special-case degraded body construction (Task 5.2 decision).
    summary: FleetSummaryOut = FleetSummaryOut()


# ---------------------------------------------------------------------------
# Software Inventory — dedicated view (Rapid7 installed software)
# ---------------------------------------------------------------------------


class SoftwareAggRowOut(BaseModel):
    product: str
    version: str = ""
    vendor: str = ""
    host_count: int = 0


class SoftwareDetailRowOut(BaseModel):
    id: str
    r7_asset_id: int
    hostname: str = ""
    product: str
    version: str = ""
    vendor: str = ""
    software_type: str = ""


class SoftwareSummaryOut(BaseModel):
    total_records: int = 0
    unique_products: int = 0
    hosts_covered: int = 0


class SoftwareInventoryOut(PageEnvelope):
    """Paged software inventory response.

    Task 5.4 — Phase 5 contract rework.

    ``items`` holds either ``SoftwareAggRowOut`` or ``SoftwareDetailRowOut``
    depending on the requested ``view`` (``"aggregated"`` | ``"detail"``).

    The union type ``SoftwareAggRowOut | SoftwareDetailRowOut`` is safe in
    practice because both call sites in ``fleet.py`` pass pre-built typed
    instances (``SoftwareAggRowOut(...)`` / ``SoftwareDetailRowOut(...)``),
    not raw dicts.  Pydantic v2's exact-instance short-circuit means no
    left-to-right field-matching occurs and there is no discrimination
    ambiguity.  If a future refactor passes raw dicts instead of instances,
    the discrimination risk becomes real (aggregated rows carry ``host_count``
    which detail rows lack; detail rows carry ``id`` + ``r7_asset_id`` which
    aggregated rows lack) and should be re-evaluated with an explicit
    discriminator field at that time.

    Sidecar fields beyond the PageEnvelope base four:
      - ``view``   — echoes the requested view parameter.
      - ``summary`` — aggregate stats for the filtered set; always present.
    """

    items: list[SoftwareAggRowOut | SoftwareDetailRowOut] = []
    total: int = 0
    limit: int = 50
    offset: int = 0
    view: str = "aggregated"
    summary: SoftwareSummaryOut = SoftwareSummaryOut()


# ---------------------------------------------------------------------------
# CVE Detail — dedicated view (Rapid7 vulnerability definitions × CVE bridge)
# ---------------------------------------------------------------------------


class CveSolutionOut(BaseModel):
    summary: str = ""
    steps: str = ""
    solution_type: str = ""
    estimate: str = ""


class CveListItemOut(BaseModel):
    cve_id: str
    severity: str = ""
    cvss: float = 0.0
    title: str = ""
    affected_hosts: int = 0
    exploits: int = 0
    fix_available: bool = False
    pci_fail: bool = False
    risk_score: float = 0.0


class CveListOut(PageEnvelope):
    items: list[CveListItemOut] = []
    total: int = 0
    limit: int = 50
    offset: int = 0
    # by_severity has a default so the degraded body {items:[],total:0,...} validates
    # without needing special-case degraded body construction (Task 5.2 decision).
    by_severity: dict[str, int] = {}


class CveAffectedHostOut(BaseModel):
    hostname: str = ""
    resource_id: str = ""
    status: str = ""
    sla: str = ""
    kb_id: str = ""
    last_updated: datetime | None = None


class CveDetailOut(BaseModel):
    cve_id: str
    severity: str = ""
    cvss: float = 0.0
    cvss_vector: str = ""
    cvss_v2: float = 0.0
    risk_score: float = 0.0
    title: str = ""
    exploits: int = 0
    malware_kits: int = 0
    fix_available: bool = False
    pci_status: str = ""
    pci_fail: bool = False
    published: datetime | None = None
    denial_of_service: bool = False
    categories: list = []
    r7_vuln_ids: list[str] = []
    affected_hosts: list[CveAffectedHostOut] = []
    affected_host_count: int = 0
    solutions: list[CveSolutionOut] = []
    sla_deadline: datetime | None = None
    sla_overdue_count: int = 0


# ---------------------------------------------------------------------------
# Unified host identity view
# ---------------------------------------------------------------------------


class HostsPageOut(PageEnvelope):
    # items was untyped (list) in the original; kept untyped here because
    # list_hosts() populates it with plain dicts (not a Pydantic model).
    # Proper typing is a Task 5.3 concern once the host dict shape is stabilised.
    items: list
    total: int
    limit: int
    offset: int


class HostVulnHeaderOut(BaseModel):
    hostname: str
    risk_score: float = 0.0
    vuln_critical: int = 0
    vuln_severe: int = 0
    vuln_moderate: int = 0


class HostVulnItemOut(BaseModel):
    cve_id: str
    kb_id: str = ""
    severity: str = ""
    cvss_v3: float = 0.0
    title: str = ""
    exploits: int = 0
    fix_available: bool = False
    pci_fail: bool = False
    sla: str = ""
    sla_due: datetime | None = None
    status: str = "open"
    last_updated: datetime | None = None
    r7_vuln_id: str = ""
    solution_summary: str = ""


class HostVulnsOut(PageEnvelope):
    # header is given a default so the degraded body {items:[],total:0,...}
    # validates without FastAPI raising a validation error.  The field is still
    # required in normal (non-degraded) responses because callers always supply it
    # explicitly.  Giving it a default here does NOT make it optional in practice —
    # any route that constructs HostVulnsOut sets header explicitly (Task 5.2 decision).
    header: HostVulnHeaderOut = HostVulnHeaderOut(hostname="")
    items: list[HostVulnItemOut] = []
    total: int = 0
    limit: int = 0
    offset: int = 0


# ---------------------------------------------------------------------------
# Host posture (UI-1 / #57) — certificates, security posture summary,
# firewall rules, shares, Windows local admin/group membership. One combined
# GET route (matching the resources/{id}/linux pattern) rather than five
# separate endpoints.
# ---------------------------------------------------------------------------


class HostCertificateOut(BaseModel):
    store: str
    subject: str | None = None
    issuer: str | None = None
    thumbprint: str = ""
    not_before: datetime | None = None
    not_after: datetime | None = None
    days_until_expiry: int | None = None
    is_expired: bool = False


class HostSecurityPostureOut(BaseModel):
    firewall_enabled: bool | None = None
    firewall_service: str | None = None
    av_enabled: bool | None = None
    av_product: str | None = None
    av_signature_date: datetime | None = None
    rdp_enabled: bool | None = None
    uac_enabled: bool | None = None
    ssh_password_auth: bool | None = None
    ssh_permit_root_login: bool | None = None
    ssh_pubkey_auth: bool | None = None
    selinux_mode: str | None = None
    apparmor_status: str | None = None


class HostFirewallRuleOut(BaseModel):
    table_name: str | None = None
    chain: str | None = None
    rule_text: str
    action: str | None = None
    source: str


class HostShareOut(BaseModel):
    share_type: str
    name: str
    path: str | None = None
    permissions: list[dict] = []


class WindowsLocalUserOut(BaseModel):
    username: str
    enabled: bool | None = None
    is_admin: bool = False
    last_logon: datetime | None = None
    password_required: bool | None = None
    password_never_expires: bool | None = None


class WindowsLocalGroupMemberOut(BaseModel):
    group_name: str
    member_name: str


class HostPostureOut(BaseModel):
    resource_id: str
    hostname: str
    certificates: list[HostCertificateOut] = []
    security_posture: HostSecurityPostureOut | None = None
    firewall_rules: list[HostFirewallRuleOut] = []
    shares: list[HostShareOut] = []
    local_users: list[WindowsLocalUserOut] = []
    local_group_members: list[WindowsLocalGroupMemberOut] = []


__all__ += [
    "HostCertificateOut",
    "HostFirewallRuleOut",
    "HostPostureOut",
    "HostSecurityPostureOut",
    "HostShareOut",
    "WindowsLocalGroupMemberOut",
    "WindowsLocalUserOut",
]


# ---------------------------------------------------------------------------
# Wave 4 item 4.6 — response models for previously-shapeless routes
# (F-020 / F-039c). Field names are byte-identical to the dicts the handlers
# already return; nothing here changes a client-visible shape.
# ---------------------------------------------------------------------------


class VersionOut(BaseModel):
    version: str
    environment: str


class SelfcheckItemOut(BaseModel):
    name: str
    status: str
    message: str


class SelfcheckOut(BaseModel):
    overall: str
    checks: list[SelfcheckItemOut]


# ---------------------------------------------------------------------------
# Phase 4 state backend REST counterparts (P4.3e)
# ---------------------------------------------------------------------------


class EnvironmentNoteIn(BaseModel):
    note: str


class EnvironmentNoteOut(BaseModel):
    id: str
    note: str
    author: str
    created_at: str
    status: str
    resolved_by: str | None = None
    resolved_at: str | None = None


class InstinctProposalIn(BaseModel):
    zone: str
    domain: str
    pattern: str
    confidence: float
    evidence: str | None = None
    citation: str | None = None


class InstinctProposalOut(BaseModel):
    proposed: str
    domain: str
    status: str


class InstinctHistoryOut(BaseModel):
    instinct: dict
    versions: list[dict]
    approvals: list[dict]


class DocumentIngestIn(BaseModel):
    title: str
    content_hash: str
    source: str
    url: str | None = None
    external_id: str | None = None


class DocumentIngestOut(BaseModel):
    id: str
    title: str
    sensitivity: str
    source: str
    status: str | None = None
    url: str | None = None
    external_id: str | None = None
    content_hash: str
    client_origin: str | None = None
    ingested_by: str | None = None
    indexed_at: str


class ClientObservationIn(BaseModel):
    client_id: str
    agent: str
    tool: str
    domain: str
    event_data: dict | None = None


class ClientObservationOut(BaseModel):
    id: str
    client_id: str
    agent: str
    tool: str
    domain: str
    created_at: str


class ClientStateIn(BaseModel):
    collection: str
    entry_id: str
    payload: dict
    client_id: str


class ClientStateOut(BaseModel):
    id: str
    collection: str
    entry_id: str
    client_id: str
    created_at: str


class ClientStateEntryOut(BaseModel):
    id: str
    collection: str
    entry_id: str
    payload: dict
    client_id: str
    created_at: str


class ClientObservationEntryOut(BaseModel):
    id: str
    client_id: str
    agent: str
    tool: str
    domain: str
    event_data: dict | None = None
    created_at: str


class GovernanceEventIn(BaseModel):
    client_id: str
    event_type: str
    payload: dict | None = None


class GovernanceEventOut(BaseModel):
    id: str
    client_id: str
    event_type: str
    payload: dict
    prev_hash: str | None = None
    hash: str
    created_at: str


class SweepAcceptedOut(BaseModel):
    accepted: bool
    domain: str


class WebhookCloudAcceptedOut(BaseModel):
    accepted: bool
    domain: str
    provider: str


class SeedResourceOut(BaseModel):
    resource_id: str
    created: bool
    hostname: str


class BulkSeedErrorOut(BaseModel):
    index: int
    hostname: str | None = None
    error: str


class BulkSeedOut(BaseModel):
    created: int
    updated: int
    errors: list[BulkSeedErrorOut] = []


class EolMigrationOut(BaseModel):
    id: str
    migration_path: str


class HostIdentityOut(BaseModel):
    # TRK-348: computed at read time. observed=False means EVERY source leg is
    # NULL -- no collector currently sees this machine; retired_at (when
    # resolvable) is when its host Resource was retired. Defaults keep every
    # existing consumer of the contract valid.
    observed: bool = True
    retired_at: str | None = None
    id: str
    short_hostname: str
    fqdn: str | None = None
    ip_addresses: list[str] = []
    r7_resource_id: str | None = None
    vsphere_resource_id: str | None = None
    octopus_resource_id: str | None = None
    linux_resource_id: str | None = None
    windows_resource_id: str | None = None
    os_family: str | None = None
    risk_score: float | None = None
    vuln_count: int | None = None
    patch_status: str | None = None
    vsphere_power_state: str | None = None
    octopus_machine_status: str | None = None
    last_reconciled: str | None = None


class HealthzOut(BaseModel):
    status: str


class HealthOut(BaseModel):
    status: str
    postgres: str | None = None
    redis: str | None = None


class HeartbeatOut(BaseModel):
    """OB-1 dead-man switch surface for GET /api/ops/heartbeat.

    heartbeat_age_seconds is None when the scheduler has never recorded a
    heartbeat (record_scheduler_heartbeat runs hourly from the scheduler's
    _collection_health_job) — that state is itself reported as stale=True.
    """

    heartbeat_age_seconds: float | None = None
    max_age_seconds: float
    stale: bool


class ScanPointItemOut(BaseModel):
    id: str
    domain: str
    method: str | None = None
    schedule: str | None = None
    last_run: str | None = None


class ProposalApproveResult(BaseModel):
    approved: bool
    proposal_id: str


class LoginOut(BaseModel):
    authenticated: bool
    dev_mode: bool
    username: str | None = None


class LogoutOut(BaseModel):
    authenticated: bool


class MeOut(BaseModel):
    authenticated: bool
    dev_mode: bool
    username: str | None = None
    name: str | None = None
    role: str | None = None


# ---------------------------------------------------------------------------
# Rapid7 secondary views (UI Batch 5 / GitLab #61) — asset config/users/
# addresses detail, plus site/tag browsers.
# ---------------------------------------------------------------------------


class R7AssetConfigOut(BaseModel):
    name: str
    value: str = ""


class R7AssetUserOut(BaseModel):
    username: str
    full_name: str = ""


class R7AssetAddressOut(BaseModel):
    ip: str
    mac: str = ""


class R7AssetDetailOut(BaseModel):
    id: str
    hostname: str = ""
    configs: list[R7AssetConfigOut] = []
    users: list[R7AssetUserOut] = []
    addresses: list[R7AssetAddressOut] = []


class R7SiteOut(BaseModel):
    r7_site_id: int
    name: str
    asset_count: int = 0
    risk_score: float = 0.0
    importance: str = ""
    site_type: str = ""
    last_scan_time: datetime | None = None


class R7SitesPageOut(PageEnvelope):
    items: list[R7SiteOut] = []
    total: int = 0
    limit: int = 100
    offset: int = 0


class R7TagOut(BaseModel):
    r7_tag_id: int
    name: str
    tag_type: str = ""
    color: str = ""
    source: str = ""


class R7TagsPageOut(PageEnvelope):
    items: list[R7TagOut] = []
    total: int = 0
    limit: int = 100
    offset: int = 0


__all__ += [
    "BulkSeedErrorOut",
    "BulkSeedOut",
    "EolMigrationOut",
    "HealthOut",
    "HealthzOut",
    "HostIdentityOut",
    "LoginOut",
    "LogoutOut",
    "McpKeyCreateBody",
    "McpKeyCreateResult",
    "McpKeyOut",
    "McpKeyPageOut",
    "McpKeyRevokeResult",
    "McpKeyUpdateBody",
    "McpKeyUpdateResult",
    "McpToolCatalogOut",
    "MeOut",
    "ProposalApproveResult",
    "ScanPointItemOut",
    "SeedResourceOut",
    "SweepAcceptedOut",
    "VersionOut",
    "WebhookCloudAcceptedOut",
]

__all__ += [
    "R7AssetAddressOut",
    "R7AssetConfigOut",
    "R7AssetDetailOut",
    "R7AssetUserOut",
    "R7SiteOut",
    "R7SitesPageOut",
    "R7TagOut",
    "R7TagsPageOut",
]

__all__ += [
    "AnsibleInventoryGroupOut",
    "AnsibleInventoryHostOut",
    "AnsiblePlaybookPlayOut",
    "AnsibleStructureOut",
    "CiScheduleOut",
    "CiSchedulesPageOut",
    "ComposeServiceOut",
    "ComposeServicesPageOut",
    "K8sManifestResourceOut",
    "K8sManifestResourcesPageOut",
    "TerraformResourceOut",
    "TerraformResourcesPageOut",
]

__all__ += [
    "CloudNetOverviewOut",
    "CloudNetSummaryOut",
    "CloudResourceOut",
    "K8sDeploymentOut",
    "K8sNodeOut",
    "K8sPodOut",
    "NetDeviceOut",
]


# ---------------------------------------------------------------------------
# Network discovery / shadow-IT view (UI Batch 8 / GitLab #64).
# ---------------------------------------------------------------------------


class NetDiscoveryServiceOut(BaseModel):
    port: int
    proto: str
    service: str = ""
    banner: str = ""
    fingerprint: str = ""
    is_dangerous: bool = False
    is_suspicious: bool = False
    last_seen: datetime | None = None


class NetDiscoveryHostOut(BaseModel):
    id: str
    ip: str
    mac: str = ""
    mac_vendor: str = ""
    hostname: str = ""
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    responded: bool = False
    discovery_tier: str = "passive"
    is_fragile: bool = False
    is_known: bool = False
    is_shadow_it: bool = False
    threat_level: str = "none"
    zone: str = ""
    resource_id: str | None = None
    services: list[NetDiscoveryServiceOut] = []


class NetDiscoverySummaryOut(BaseModel):
    total_hosts: int = 0
    shadow_it_count: int = 0
    known_count: int = 0
    by_threat_level: dict[str, int] = {}


class NetDiscoveryHostsPageOut(PageEnvelope):
    items: list[NetDiscoveryHostOut] = []
    total: int = 0
    limit: int = 100
    offset: int = 0
    summary: NetDiscoverySummaryOut = NetDiscoverySummaryOut()


__all__ += [
    "NetDiscoveryHostOut",
    "NetDiscoveryHostsPageOut",
    "NetDiscoveryServiceOut",
    "NetDiscoverySummaryOut",
]


# ---------------------------------------------------------------------------
# RAG documents / knowledge-store browser (UI Batch 9 / GitLab #65).
# DocumentChunkPreviewOut deliberately excludes the `embedding` vector column
# — irrelevant to a health-browse view and unnecessary to ship to the client.
# ---------------------------------------------------------------------------


class DocumentOut(BaseModel):
    id: str
    title: str
    sensitivity: str = "internal"
    source: str = ""
    status: str = "current"
    space: str = ""
    url: str = ""
    indexed_at: datetime | None = None
    last_updated: datetime | None = None
    chunk_count: int = 0


class DocumentsPageOut(PageEnvelope):
    items: list[DocumentOut] = []
    total: int = 0
    limit: int = 100
    offset: int = 0


class DocumentChunkPreviewOut(BaseModel):
    chunk_index: int
    text_preview: str = ""
    token_count: int | None = None


class DocumentDetailOut(BaseModel):
    id: str
    title: str
    sensitivity: str = "internal"
    source: str = ""
    status: str = "current"
    space: str = ""
    url: str = ""
    external_id: str = ""
    source_version: int | None = None
    content_hash: str = ""
    indexed_at: datetime | None = None
    last_updated: datetime | None = None
    chunks: list[DocumentChunkPreviewOut] = []
    chunk_count: int = 0


__all__ += [
    "DocumentChunkPreviewOut",
    "DocumentDetailOut",
    "DocumentOut",
    "DocumentsPageOut",
]


# ---------------------------------------------------------------------------
# Webhook subscriptions (outbound event-publish system, GitLab #112)
# ---------------------------------------------------------------------------


class WebhookSubscriptionOut(BaseModel):
    id: str
    name: str
    target_url: str
    has_secret: bool
    event_pattern: str
    domain_filter: str | None = None
    active: bool
    description: str = ""
    created_by: str = ""
    created_at: datetime
    updated_at: datetime


class WebhookSubscriptionPageOut(BaseModel):
    items: list[WebhookSubscriptionOut]
    total: int


class WebhookSubscriptionCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    target_url: str = Field(min_length=1, max_length=1024)
    secret_token: str | None = Field(default=None, max_length=256)
    event_pattern: str = Field(default="*", max_length=128)
    domain_filter: str | None = Field(default=None, max_length=64)
    description: str = Field(default="", max_length=4000)


class WebhookSubscriptionUpdateBody(BaseModel):
    """PATCH body — every field optional, only supplied fields are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    target_url: str | None = Field(default=None, min_length=1, max_length=1024)
    secret_token: str | None = Field(default=None, max_length=256)
    event_pattern: str | None = Field(default=None, max_length=128)
    domain_filter: str | None = Field(default=None, max_length=64)
    active: bool | None = None
    description: str | None = Field(default=None, max_length=4000)


class WebhookSubscriptionDeleteResult(BaseModel):
    id: str
    deleted: bool


class WebhookTestDeliveryBody(BaseModel):
    category: str = Field(default="test", max_length=128)
    message: str = Field(default="Test delivery from infra-brain", max_length=2000)


class WebhookTestDeliveryResult(BaseModel):
    id: str
    delivered: bool
    error: str | None = None


class WebhookDeliveryOut(BaseModel):
    id: str
    subscription_id: str
    category: str
    domain: str | None = None
    dedup_key: str | None = None
    status: str
    attempt_count: int
    max_attempts: int
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    created_at: datetime
    delivered_at: datetime | None = None


class WebhookDeliveryPageOut(BaseModel):
    items: list[WebhookDeliveryOut]
    total: int


__all__ += [
    "WebhookDeliveryOut",
    "WebhookDeliveryPageOut",
    "WebhookSubscriptionCreateBody",
    "WebhookSubscriptionDeleteResult",
    "WebhookSubscriptionOut",
    "WebhookSubscriptionPageOut",
    "WebhookSubscriptionUpdateBody",
    "WebhookTestDeliveryBody",
    "WebhookTestDeliveryResult",
]


# ─────────────────────────────────────────────────────────────────────────────
# RuntimeConfig — admin-editable operational tuning overrides (TRK-303 part 3/7)
# ─────────────────────────────────────────────────────────────────────────────


class RuntimeConfigOut(BaseModel):
    key: str
    value_type: str
    value: str | None  # None for secrets — never serialize encrypted_value out
    is_secret: bool
    category: str
    updated_by: str
    updated_at: datetime


class RuntimeConfigPageOut(BaseModel):
    items: list[RuntimeConfigOut]


class RuntimeConfigWriteBody(BaseModel):
    value: str = Field(min_length=1, max_length=4096)
    value_type: str = Field(pattern="^(str|int|float|bool)$")
    category: str = "tuning"


__all__ += [
    "RuntimeConfigOut",
    "RuntimeConfigPageOut",
    "RuntimeConfigWriteBody",
]


# ─────────────────────────────────────────────────────────────────────────────
# Settings catalog — the operator-facing configuration surface
#
# Derived at request time from the `Settings` pydantic model itself (see
# api/routers/settings_catalog.py). Deliberately NOT a hand-maintained mirror
# of config.py: a duplicated list rots the moment someone adds a field.
#
# SECRET CONTRACT (the hard constraint, pinned by
# tests/test_settings_catalog_secrets.py): for any entry with `secret=True`,
# BOTH `value` and `default` are always None and `secret_state` carries
# "set"/"not set" instead. There is no field on this model that can hold a
# secret's value — not even a masked one. `_helpers.mask_secret` preserves the
# last four characters and is deliberately NOT used on this surface.
# ─────────────────────────────────────────────────────────────────────────────


class SettingCatalogEntry(BaseModel):
    key: str
    """Lowercase `Settings` field name — also the runtime_config row key."""

    env_var: str
    """Uppercased field name; the env/.env variable pydantic-settings reads."""

    group: str
    """Domain/subsystem bucket for UI grouping."""

    type: str
    """"bool" | "int" | "float" | "str" | "other" — from the field annotation."""

    description: str
    """Derived from the `#` comment block above the field in config.py."""

    value: str | None = None
    """Effective (env + DB-override layered) value, DSN-scrubbed. Always None
    when `secret` is True."""

    default: str | None = None
    """The field's declared default. Always None when `secret` is True."""

    source: str
    """Where the effective value came from: "db-override" | "env" | "default".
    The single most useful field for an operator (TRK-314)."""

    shadowed_value: str | None = None
    """The env/default value an APPLIED db-override is currently masking —
    the thing that made TRK-314 invisible. None when `secret` is True."""

    secret: bool = False
    secret_state: str | None = None
    """"set" | "not set" — the ONLY thing ever reported about a secret's value."""

    secret_reason: str | None = None
    """"name-hint" (key contains key/token/password/secret) or
    "embedded-credential" (the value carries a `scheme://user:pass@` DSN)."""

    managed_in: str | None = None
    """Where a secret actually belongs (Bitwarden / env). Set only for secrets."""

    editable: bool = False
    locked_reason: str | None = None
    """Why `editable` is False — shown verbatim in the UI."""

    db_row: bool = False
    """A `runtime_config` row exists for this key (applied or not)."""

    override_ignored_reason: str | None = None
    """Set when `db_row` is True but the override was NOT applied (denylisted,
    failed validation, undecryptable). Silent-no-op detection."""

    degraded: bool = False
    """Rendering this entry raised; the row is metadata-only. No value is ever
    substituted from the exception."""


class SettingsCatalogPageOut(BaseModel):
    items: list[SettingCatalogEntry]
    total: int
    groups: list[str]
    """Group names in display order."""


class SettingsCatalogWriteBody(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


__all__ += [
    "SettingCatalogEntry",
    "SettingsCatalogPageOut",
    "SettingsCatalogWriteBody",
]


# LLM observability (T7, rev14) — api/routers/llm_observability.py
#
# EVERY NUMBER BELOW IS LABELLED, because this is an audit surface and a
# mislabelled cost figure is worse than no cost figure.
#
#   * "LLM run"   = ONE ``LLMAgent.reason()`` call (one ReAct loop).
#     ``AgentDecisionLog.run_id`` is a fresh uuid4 minted inside ``reason()``
#     (agents/llm_base.py) — it is NOT a ``CollectionRun.id``, and the two
#     never join. A single collection sweep may contain several LLM runs, or
#     none.
#   * "iteration" = one model turn (one ``AIMessage``) inside that loop.
#     ``iteration == -1`` is NOT a turn: it is the recursion-limit marker row
#     (``RECURSION_LIMIT_MARKER``) and is excluded from every turn count.
#   * ``token_count`` on a row = ``usage_metadata.total_tokens`` for that ONE
#     model call (prompt + completion). The whole conversation is re-sent each
#     turn, so this GROWS across iterations by construction — it is not a
#     running total anyone accumulated.
#   * ``tokens_billed`` on a run = the SUM of those per-call totals. Each API
#     call is billed independently, so the sum is what the run actually cost;
#     it is necessarily larger than the number of distinct tokens in the
#     transcript. This is the same quantity ``_accumulate_run_tokens`` feeds to
#     the TRK-120 per-run ceiling.
#   * ``peak_call_tokens`` = the largest single call in the run — the
#     context-window-pressure signal, not a cost signal.
# ─────────────────────────────────────────────────────────────────────────────


TOKEN_METRIC = (
    "Tokens are per-CALL totals (prompt + completion) summed over the run. "
    "The full conversation is re-sent on every turn, so a run's total is what "
    "the provider billed — necessarily larger than the number of distinct "
    "tokens in the transcript."
)
"""Canonical label for every token figure this API returns.

It lives on the schema (with the field defaulting to it) rather than only in
the router so a *degraded* envelope — the ``{"items": [], "total": 0, ...}``
body ``_degraded_body_for`` synthesises when a query fails — still carries the
unit statement. A degraded page showing unlabelled zeros would be exactly the
mislabelled-cost-data failure this surface exists to avoid.
"""


class LLMFlagOut(BaseModel):
    """One default-off LLM feature flag and what its being off means."""

    name: str
    enabled: bool
    effect: str


class LLMToolUseOut(BaseModel):
    tool: str
    calls: int
    """Total invocations across every iteration in scope."""
    max_in_one_iteration: int
    """Highest number of calls to this tool inside a SINGLE model turn — the
    'is it hammering the same tool' signal."""


class LLMAgentStatsOut(BaseModel):
    agent: str
    domain: str
    runs: int
    turns: int
    """Model turns (iteration >= 0). Excludes recursion-limit marker rows."""
    tokens_billed: int
    peak_call_tokens: int
    tool_calls: int
    narrated_turns: int
    silent_turns: int
    """Turns where the model emitted no prose. Not a capture failure — see
    ``reasoning_state`` on the step rows."""
    completed: int
    recursion_limit: int
    truncated: int
    last_run_at: datetime | None = None


class LLMOutcomeCountsOut(BaseModel):
    completed: int = 0
    recursion_limit: int = 0
    truncated: int = 0
    unknown: int = 0


class LLMSummaryOut(BaseModel):
    window_hours: int
    since: datetime
    generated_at: datetime
    provider: str
    model: str
    runs: int
    turns: int
    tokens_billed: int
    peak_call_tokens: int
    tool_calls: int
    narrated_turns: int
    silent_turns: int
    outcomes: LLMOutcomeCountsOut
    by_agent: list[LLMAgentStatsOut]
    top_tools: list[LLMToolUseOut]
    flags: list[LLMFlagOut]
    token_ceiling_enabled: bool
    token_ceiling: int
    rows_scanned: int
    """How many decision-log rows this summary actually read."""
    truncated_scan: bool
    """True when the window held more rows than ``scan_cap`` and the numbers
    below cover only the most recent ``rows_scanned`` of them."""
    scan_cap: int
    token_metric: str = TOKEN_METRIC
    """Human-readable statement of what the token figures measure."""


class LLMRunOut(BaseModel):
    run_id: str
    agent: str
    domain: str
    started_at: datetime
    ended_at: datetime
    turns: int
    tokens_billed: int
    peak_call_tokens: int
    tool_calls: int
    distinct_tools: int
    max_tool_repeat: int
    """Highest count of one tool inside a single turn, across the run."""
    narrated_turns: int
    silent_turns: int
    outcome: str
    """completed | recursion_limit | truncated | unknown — see
    ``LLMRunDetailOut.outcome_reason``."""


class LLMRunPageOut(PageEnvelope):
    items: list[LLMRunOut]
    token_metric: str = TOKEN_METRIC


class LLMStepOut(BaseModel):
    iteration: int
    ts: datetime
    call_tokens: int | None = None
    """Tokens for THIS call only (prompt + completion). Null when the provider
    returned no usage metadata."""
    tools_chosen: list[str]
    tool_repeats: dict[str, int]
    """Only tools called more than once in THIS turn, with their count."""
    reasoning_text: str
    reasoning_state: str
    """present | absent_tool_call_turn | absent_no_narration — never let the UI
    render an unexplained blank."""


class LLMRunDetailOut(BaseModel):
    run_id: str
    agent: str
    domain: str
    started_at: datetime
    ended_at: datetime
    turns: int
    tokens_billed: int
    peak_call_tokens: int
    tool_calls: int
    distinct_tools: int
    max_tool_repeat: int
    narrated_turns: int
    silent_turns: int
    outcome: str
    outcome_reason: str
    steps: list[LLMStepOut]
    token_metric: str = TOKEN_METRIC


__all__ += [
    "LLMAgentStatsOut",
    "LLMFlagOut",
    "LLMOutcomeCountsOut",
    "LLMRunDetailOut",
    "LLMRunOut",
    "LLMRunPageOut",
    "LLMStepOut",
    "LLMSummaryOut",
    "LLMToolUseOut",
    "TOKEN_METRIC",
]
