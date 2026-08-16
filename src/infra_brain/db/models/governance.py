"""Governance, compliance, and agent observability models.

Bucket: InventoryReconcileEvent, ComplianceViolation, AgentActionLog,
        AgentDecisionLog, AgentConfigSetting, RuntimeConfig.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, _now, _uuid


class InventoryReconcileEvent(Base):
    """A host discovered in real infra but missing from the GitLab Ansible
    inventory (source of truth). Add-only: records the proposed addition + MR."""

    __tablename__ = "inventory_reconcile_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    host: Mapped[str] = mapped_column(String(256))
    domain: Mapped[str] = mapped_column(String(64))
    target_group: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="proposed")  # proposed|merged|failed
    # H-6: "proposed" must mean an MR actually exists (mr_url is set). A row
    # with status="failed" records a host that was detected as missing but
    # for which no MR was ever created (creation failed, or MR creation was
    # disabled) — see agents/inventory_reconcile.py and
    # scripts/repair_poisoned_inventory_reconcile_events.py. "failed" rows are
    # NOT excluded by _filter_already_proposed(), so the host is re-evaluated
    # on the next run.
    mr_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("host", "status", name="uq_inv_reconcile_host_status"),)


class ComplianceViolation(Base):
    """A policy-as-code violation found by ComplianceAgent (deterministic rules).

    Invariant: ``resources`` rows with ``domain="homelab_services"`` (home-lab
    media-server reachability checks — radarr/sonarr/emby/etc, written by
    ``HomelabServicesAgent``) are intentionally excluded from every rule that
    can reach this table — see the ``exclude_resource_domains`` list on the
    ``stale_drift`` rule in ``agents/compliance_rules.py``/
    ``rules/enforcement/compliance.yml``, and the fact that ``eol_overdue``/
    ``vuln_sla_breach``/``unaddressed_critical_cve`` are keyed off
    ``EolRegistry``/``VulnQueueItem``/``R7Asset`` rows that only specific
    fleet collectors (EOL/vuln scanners) ever populate, never
    ``HomelabServicesAgent``. Do not add a query path here (or in
    ``VulnQueueItem``) that joins broadly across ``Resource`` without
    preserving this exclusion — a media-server outage must never read as a
    security/compliance/PCI signal.
    """

    __tablename__ = "compliance_violations"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    rule: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    host: Mapped[str] = mapped_column(String(256), default="")
    # Task 4.5: nullable soft-link FK to the canonical resources row matching
    # ``host`` by name. ``host`` itself is KEPT (display + unmatched rows) and
    # the existing unique constraint below is UNCHANGED — resource_id is purely
    # additive. Mirrors R7Asset.resource_id in db/models/rapid7.py.
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("resources.id", ondelete="SET NULL"), nullable=True
    )
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="open")  # open|resolved
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # TRK-242 (GitLab #113): incident-management correlation key, mirrors
    # DriftEvent.incident_key (db/models/core.py) — set when an ops-alert for
    # this violation is sent with a dedup_key, so the ack-in webhook
    # (POST /webhooks/incident/ack) can update this row's status.
    incident_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    __table_args__ = (
        UniqueConstraint("rule", "host", "status", name="uq_compliance_rule_host_status"),
        Index("ix_compliance_violations_resource_id", "resource_id"),
    )


class AgentActionLog(Base):
    """Durable per-tool-call log written by AuditCallbackHandler (Task 28)."""

    __tablename__ = "agent_action_log"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    agent: Mapped[str] = mapped_column(String(64))
    domain: Mapped[str] = mapped_column(String(64), default="")
    tool: Mapped[str] = mapped_column(String(128))
    args_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(String(16))  # "allow" | "deny"
    status: Mapped[str] = mapped_column(String(16))  # "ok" | "error"
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # #90 design / #80 impl: sha256 hex of the raw MCP bearer token, written by
    # McpAuditMiddleware for MCP tool calls. Nullable String(64) with NO FK by
    # design — trivially joinable by equality to the future McpApiKey.token_hash
    # (#44) with no further migration; None for non-MCP callers / unauthenticated
    # dev-mode. See src/infra_brain/mcp_audit_middleware.py.
    caller_key_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class AgentDecisionLog(Base):
    """Per-iteration LLM reasoning/decision log written by LLMAgent.reason() (Task F3)."""

    __tablename__ = "agent_decision_log"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    agent: Mapped[str] = mapped_column(String(64))
    domain: Mapped[str] = mapped_column(String(64), default="")
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_text: Mapped[str] = mapped_column(Text, default="")
    tools_chosen: Mapped[list] = mapped_column(JSON, default=list)
    decision_summary: Mapped[str] = mapped_column(String(512), default="")
    # OB-6: token usage + parent run linkage for cost/latency queries and
    # LangSmith run-tree correlation. Both nullable — populated only when the
    # framework surfaces usage_metadata / a parent run id (migration 0025).
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    parent_run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AgentConfigSetting(Base):
    """Key-value store for non-secret agent configuration set via the dashboard UI."""

    __tablename__ = "agent_config_settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class RuntimeConfig(Base):
    """Admin-editable overrides for operational tuning knobs, secrets, and
    collector on/off toggles — the read/resolution layer that GitLab #83's
    write-only AgentConfigSetting attempt lacked (TRK-303). Deliberately
    separate from AgentConfigSetting (scheduler heartbeat / streak counters,
    no type/secret/audit metadata) rather than overloading it.
    """

    __tablename__ = "runtime_config"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_type: Mapped[str] = mapped_column(String(16))  # "str" | "int" | "float" | "bool"
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    encrypted_value: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    # "tuning" | "secret" | "collector_toggle"
    category: Mapped[str] = mapped_column(String(32), default="tuning")
    updated_by: Mapped[str] = mapped_column(String(128), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


# agent_action_log: time-ordered, per-agent, per-verdict filtering (Task 28)
Index("ix_agent_action_log_ts", AgentActionLog.ts)
Index("ix_agent_action_log_agent", AgentActionLog.agent)
Index("ix_agent_action_log_verdict", AgentActionLog.verdict)

# agent_decision_log: filter by run_id, agent, and time (Task F3)
Index("ix_agent_decision_log_run_id", AgentDecisionLog.run_id)
Index("ix_agent_decision_log_agent", AgentDecisionLog.agent)
Index("ix_agent_decision_log_ts", AgentDecisionLog.ts)


__all__ = [
    "InventoryReconcileEvent",
    "ComplianceViolation",
    "AgentActionLog",
    "AgentDecisionLog",
    "AgentConfigSetting",
    "RuntimeConfig",
]
