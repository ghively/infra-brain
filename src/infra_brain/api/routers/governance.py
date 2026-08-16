"""infra_brain.api.routers.governance -- Re-export shim.

Split into four focused sub-routers (refactor/split-governance-router):
  governance_drift.py        -- drift events + notifications
  governance_audit.py        -- audit log, activity, decisions
  governance_intelligence.py -- instincts, observations, scripts, proposals
  governance_ops.py          -- compliance, inventory reconcile, agents,
                                settings, agent-config, action approvals

All public symbols re-exported here for backwards compatibility.
"""

from infra_brain.api.routers.governance_audit import (  # noqa: F401
    governance_audit_router,
    list_activity,
    list_audit,
    list_decisions,
)
from infra_brain.api.routers.governance_drift import (  # noqa: F401
    _TELEMETRY_FIELDS,
    get_drift_trend,
    governance_drift_router,
    list_drift,
    list_drift_domains,
    list_notifications,
)
from infra_brain.api.routers.governance_intelligence import (  # noqa: F401
    governance_intelligence_router,
    list_instincts,
    list_observations,
    list_proposals,
    list_scripts,
)
from infra_brain.api.routers.governance_ops import (  # noqa: F401
    _AGENT_DESC,
    _AGENT_TOOLS,
    _DOMAIN_REQUIREMENTS,
    _SECRET_KEYS,
    _SYSTEM_DOMAINS,
    _action_uuid,
    _is_secret,
    approve_remediation_action,
    get_settings_view,
    governance_ops_router,
    list_agent_config,
    list_agents,
    list_compliance,
    list_inventory_reconcile,
    reject_remediation_action,
)

# Backwards-compatible alias.
governance_router = governance_ops_router
