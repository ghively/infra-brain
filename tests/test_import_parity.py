"""Task 1 regression guard: assert infra_brain.db.models is a package and
that all 92 model classes plus shared objects are importable from it.

Step 1 (TDD): the ``__path__`` assertion fails today (models.py is a module).
After the move it must pass. All other assertions act as import-parity guards
that survive the refactor.
"""

import importlib

import pytest


# ---------------------------------------------------------------------------
# All names that must be importable from infra_brain.db.models
# ---------------------------------------------------------------------------

_MODEL_NAMES = [
    # core
    "Resource",
    "CollectionRun",
    "Snapshot",
    "ResourceConfig",
    "DriftEvent",
    "ScanPoint",
    "AuditLog",
    "Instinct",
    "Observation",
    "Document",
    "JiraTicket",
    "ConfluencePage",
    "IntegrationProposal",
    "ProposedAction",
    "RootCauseNote",
    "ImportedSkill",
    "PolicyRegistry",
    "Workspace",
    "UIUser",
    "CustomView",
    "HostIdentity",
    "GeneratedScript",
    # rapid7
    "VulnQueueItem",
    "R7Asset",
    "R7Site",
    "R7Tag",
    "R7Software",
    "R7AssetConfig",
    "R7AssetUser",
    "R7AssetAddress",
    "R7Vulnerability",
    "R7Solution",
    "R7VulnSolution",
    "R7VulnCve",
    # octopus (20)
    "OctopusProjectGroup",
    "OctopusLifecycle",
    "OctopusProject",
    "OctopusEnvironment",
    "OctopusMachine",
    "OctopusChannel",
    "OctopusRelease",
    "OctopusDeployment",
    "OctopusDeploymentStep",
    "OctopusVariable",
    "OctopusLibraryVariableSet",
    "OctopusActionTemplate",
    "OctopusMachineRole",
    "OctopusFeed",
    "OctopusInterruption",
    "OctopusTask",
    "OctopusAccount",
    "OctopusTeam",
    "OctopusUser",
    "OctopusEvent",
    # vsphere (9)
    "VsphereDatacenter",
    "VsphereCluster",
    "VsphereHost",
    "VsphereVm",
    "VsphereDatastore",
    "VsphereNetwork",
    "VsphereResourcePool",
    "VsphereVmMetric",
    "VsphereHostMetric",
    # cloud_k8s_net (7)
    "CloudResource",
    "K8sNode",
    "K8sPod",
    "K8sDeployment",
    "NetDevice",
    "NetDiscoveryHost",
    "NetDiscoveryService",
    # ansible (10)
    "GitlabProject",
    "IacFile",
    "CiPipelineRun",
    "ComposeService",
    "K8sManifestResource",
    "TerraformResource",
    "AnsibleInventoryGroup",
    "AnsibleInventoryHost",
    "AnsiblePlaybookPlay",
    "CiSchedule",
    # os_inventory (10)
    "LinuxHost",
    "LinuxPackage",
    "LinuxService",
    "LinuxUser",
    "LinuxCron",
    "LinuxPort",
    "WindowsPatchState",
    "WindowsService",
    "WindowsSoftware",
    "EolRegistry",
    # governance (5)
    "InventoryReconcileEvent",
    "ComplianceViolation",
    "AgentActionLog",
    "AgentDecisionLog",
    "AgentConfigSetting",
    # shared objects
    "Base",
    "JSONB",
    # ResourceRelationship is deliberately NOT here: its re-export was deleted
    # at the P5 integration with the table drop (see db/models/__init__.py's
    # module docstring).
]

_SHARED_CALLABLES = [
    "_now",
    "_uuid",
]


def test_models_is_package() -> None:
    """infra_brain.db.models must be a package (has __path__).

    This assertion fails before Task 1 is implemented (it is a module today).
    After the move it must pass.
    """
    import infra_brain.db.models as m

    assert hasattr(m, "__path__"), (
        "infra_brain.db.models is still a module (db/models.py). "
        "Task 1 requires converting it to a package (db/models/)."
    )


@pytest.mark.parametrize("name", _MODEL_NAMES)
def test_model_importable(name: str) -> None:
    """Every model class must be importable from infra_brain.db.models."""
    mod = importlib.import_module("infra_brain.db.models")
    assert hasattr(mod, name), (
        f"infra_brain.db.models has no attribute '{name}'. "
        "Either it was not added to __all__ or the bucket import is missing."
    )
    obj = getattr(mod, name)
    assert obj is not None, f"'{name}' is None in infra_brain.db.models"


@pytest.mark.parametrize("name", _SHARED_CALLABLES)
def test_shared_callable_importable(name: str) -> None:
    """_now and _uuid must remain importable from infra_brain.db.models."""
    mod = importlib.import_module("infra_brain.db.models")
    assert hasattr(mod, name), (
        f"infra_brain.db.models has no attribute '{name}'. "
        "Ensure _base.py defines it and __init__.py re-exports it."
    )
    assert callable(getattr(mod, name)), f"'{name}' must be callable"
