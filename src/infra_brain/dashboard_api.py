"""Re-export shim for infra_brain.dashboard_api (Phase 3 god-file split).

All route handlers and Pydantic schemas have been moved to infra_brain.api.*
sub-packages (Tasks 2–11).  This file now exists solely to:

  1. Preserve every ``from infra_brain.dashboard_api import X`` call site in
     tests and main.py without modification (zero-edit rule).
  2. Keep the ``router`` object alive so ``main.py``'s
     ``app.include_router(dashboard_router)`` remains a valid (empty) no-op
     and tests that build a bare ``FastAPI(); app.include_router(router)`` still
     work.
  3. Re-export ``get_session`` as a *real name in this module's namespace* for
     any legacy import or patch target of the form
     ``patch("infra_brain.dashboard_api.get_session", …)``.
     IMPORTANT: this re-export does NOT intercept handlers that have moved to
     ``infra_brain.api.routers.*``.  Those handlers resolve ``get_session``
     from their OWN module namespace (``infra_brain.api.routers.<bucket>``).
     Tests that target moved handlers must patch
     ``infra_brain.api.routers.<bucket>.get_session`` — which they already do
     (see test_error_handling.py).  Patching this shim alone is insufficient
     for moved handlers.

Do NOT add handler logic here.  Do NOT remove any exported name that has an
active importer (see ``git grep "from infra_brain.dashboard_api import"``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from infra_brain.dashboard_auth import require_session
from infra_brain.db.session import get_session  # noqa: F401 — patched by tests

# ─────────────────────────────────────────────────────────────────────────────
# Empty router — kept so main.py's include_router(dashboard_router) is a valid
# no-op and test fixtures that do app.include_router(router) still import it.
# ─────────────────────────────────────────────────────────────────────────────
router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)

# ─────────────────────────────────────────────────────────────────────────────
# Router re-exports (moved in Tasks 3–11)
# ─────────────────────────────────────────────────────────────────────────────
from infra_brain.api.routers.cve import cve_router  # noqa: E402, F401
from infra_brain.api.routers.governance import (  # noqa: E402, F401
    _AGENT_DESC,
    _AGENT_TOOLS,
    _DOMAIN_REQUIREMENTS,
    _SECRET_KEYS,
    _SYSTEM_DOMAINS,
    _TELEMETRY_FIELDS,
    _action_uuid,
    _is_secret,
    approve_remediation_action,
    get_drift_trend,
    get_settings_view,
    governance_router,
    list_activity,
    list_agent_config,
    list_agents,
    list_audit,
    list_compliance,
    list_decisions,
    list_drift,
    list_drift_domains,
    list_instincts,
    list_inventory_reconcile,
    list_notifications,
    list_observations,
    list_proposals,
    list_scripts,
    reject_remediation_action,
)
from infra_brain.api.routers.hosts import (  # noqa: E402, F401
    _host_identity_dict,
    bulk_create_resources,
    create_resource,
    get_host,
    get_host_vulns,
    get_linux_detail,
    get_windows_detail,
    hosts_router,
    list_eol,
    list_hosts,
    list_resources,
    patch_eol_migration,
    resource_snapshots,
    resources_router,
)
from infra_brain.api.routers.iac import iac_router  # noqa: E402, F401
from infra_brain.api.routers.vsphere import vsphere_router  # noqa: E402, F401

# ─────────────────────────────────────────────────────────────────────────────
# Shared schemas + helpers (moved in Task 2)
# ─────────────────────────────────────────────────────────────────────────────
from infra_brain.api._helpers import *  # noqa: E402, F401, F403
from infra_brain.api.schemas import *  # noqa: E402, F401, F403, F405
