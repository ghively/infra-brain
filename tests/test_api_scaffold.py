"""Task 2 regression guard: assert that all 87 Pydantic response-model classes
and the listed shared helpers are importable from their new canonical homes:
  * infra_brain.api.schemas   — response models
  * infra_brain.api._helpers  — shared helper functions / constants

Acts as a permanent import-parity guard for canonical schema locations.
"""

import importlib

import pytest

# ---------------------------------------------------------------------------
# The 87 Pydantic response-model classes that must live in api/schemas.py
# ---------------------------------------------------------------------------

_SCHEMA_NAMES = [
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
    "LinuxDetailOut",
    # Windows
    "WindowsPatchOut",
    # Drift
    "DriftTrendPoint",
    "DriftTrendOut",
    "DriftOut",
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
    # Scan Points
    "ScanOut",
    # Notifications
    "NotificationOut",
    # Intelligence
    "InstinctOut",
    "ObservationOut",
    "ScriptOut",
    "ProposalOut",
    # Activity
    "ActivityOut",
    "DecisionOut",
    "AuditOut",
    "AuditPageOut",
    # Inventory Reconcile
    "InventoryReconcileOut",
    # Remediation
    "RemediationOut",
    # Compliance
    "ComplianceOut",
    # Agents
    "AgentOut",
    # Settings / Health
    "SettingRow",
    "SettingGroup",
    "HealthItem",
    # Chat
    "ChatRequest",
    # Custom View
    "CustomViewRequest",
    "CustomViewCreate",
    "CustomViewUpdate",
    "CustomViewOut",
    # Actions
    "ActionApproveBody",
    "ActionApproveResult",
    "ActionRejectResult",
    # Agent config
    "AgentConfigRequirement",
    "AgentConfigOut",
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
    "HostVulnHeaderOut",
    "HostVulnItemOut",
    "HostVulnsOut",
]

# ---------------------------------------------------------------------------
# Shared helpers that must live in api/_helpers.py
# ---------------------------------------------------------------------------

_HELPER_NAMES = [
    "_SECRET_HINTS",
    "mask_secret",
    "_s",
    "_now",
    "_sla_string",
    "_CRON_HUMAN",
    "_humanize_cron",
    "_setting_row",
    "_audit_category",
    "_AUDIT_CATEGORY_KEYWORDS",
    "_best_vuln_by_cve",
    "_seed_resource_row",
    "require_session_optional",
]


# ---------------------------------------------------------------------------
# Package structure assertions
# ---------------------------------------------------------------------------


def test_api_package_exists() -> None:
    """infra_brain.api must be a package (has __path__)."""
    mod = importlib.import_module("infra_brain.api")
    assert hasattr(mod, "__path__"), (
        "infra_brain.api is not a package. Create src/infra_brain/api/__init__.py."
    )


def test_api_routers_package_exists() -> None:
    """infra_brain.api.routers must be a package (has __path__)."""
    mod = importlib.import_module("infra_brain.api.routers")
    assert hasattr(mod, "__path__"), (
        "infra_brain.api.routers is not a package. Create src/infra_brain/api/routers/__init__.py."
    )


# ---------------------------------------------------------------------------
# Schema assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _SCHEMA_NAMES)
def test_schema_importable(name: str) -> None:
    """Every response-model class must be importable from infra_brain.api.schemas."""
    mod = importlib.import_module("infra_brain.api.schemas")
    assert hasattr(mod, name), (
        f"infra_brain.api.schemas has no attribute '{name}'. "
        "Ensure the class was moved and added to __all__."
    )
    obj = getattr(mod, name)
    assert obj is not None, f"'{name}' is None in infra_brain.api.schemas"


def test_schemas_has_all() -> None:
    """infra_brain.api.schemas must define __all__."""
    mod = importlib.import_module("infra_brain.api.schemas")
    assert hasattr(mod, "__all__"), "infra_brain.api.schemas must define __all__"
    all_list = mod.__all__
    assert len(all_list) >= 89, f"__all__ has only {len(all_list)} entries; expected >= 89"


def test_all_89_schemas_present() -> None:
    """The count of names in _SCHEMA_NAMES must be exactly 78 (was 87 before the
    9 Octopus schemas were removed — P7.1a/D6/D11)."""
    assert len(_SCHEMA_NAMES) == 78, f"Expected 78 schema names, got {len(_SCHEMA_NAMES)}"


# ---------------------------------------------------------------------------
# Helper assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _HELPER_NAMES)
def test_helper_importable(name: str) -> None:
    """Every shared helper must be importable from infra_brain.api._helpers."""
    mod = importlib.import_module("infra_brain.api._helpers")
    assert hasattr(mod, name), (
        f"infra_brain.api._helpers has no attribute '{name}'. "
        "Ensure it was moved and added to __all__."
    )
    obj = getattr(mod, name)
    assert obj is not None, f"'{name}' is None in infra_brain.api._helpers"


def test_helpers_has_all() -> None:
    """infra_brain.api._helpers must define __all__."""
    mod = importlib.import_module("infra_brain.api._helpers")
    assert hasattr(mod, "__all__"), "infra_brain.api._helpers must define __all__"


# ---------------------------------------------------------------------------
# Re-export parity: dashboard_api must still expose every name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _SCHEMA_NAMES)
def test_schema_still_in_dashboard_api(name: str) -> None:
    """Schemas must remain importable from infra_brain.dashboard_api (re-export shim)."""
    mod = importlib.import_module("infra_brain.dashboard_api")
    assert hasattr(mod, name), (
        f"infra_brain.dashboard_api no longer exposes '{name}'. "
        "Add 'from infra_brain.api.schemas import *' to dashboard_api.py."
    )


@pytest.mark.parametrize("name", _HELPER_NAMES)
def test_helper_still_in_dashboard_api(name: str) -> None:
    """Helpers must remain importable from infra_brain.dashboard_api (re-export shim)."""
    mod = importlib.import_module("infra_brain.dashboard_api")
    assert hasattr(mod, name), (
        f"infra_brain.dashboard_api no longer exposes '{name}'. "
        "Add 'from infra_brain.api._helpers import *' to dashboard_api.py."
    )
