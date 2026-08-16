"""infra_brain.db.models package.

Re-exports every model class, Base, JSONB, _now, and _uuid so that all
existing ``from infra_brain.db.models import <X>`` call sites continue to
work without any modification.

Import order follows the original models.py: shared objects first, then
domain buckets in dependency order (core → os_inventory/rapid7/octopus/
vsphere/cloud_k8s_net/ansible → governance).

``ResourceRelationship`` is no longer re-exported here. It existed for ONE
reason — so Alembic autogenerate would see the edge model when ``env.py``
imported this package — and P5 dropped the table it mapped. The transitional
re-export of the (non-mapped, instantiation-refusing) shim was deleted at the
P5 integration once its last importer, ``graph_phase3``'s legacy walk, was
removed; anything that needs the frozen legacy schema imports
``LEGACY_RESOURCE_RELATIONSHIPS`` from ``db.relationships`` explicitly.
"""

from ._base import JSONB, Base, _now, _uuid
from .ansible import (
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    AnsiblePlaybookPlay,
    CiPipelineRun,
    CiSchedule,
    ComposeService,
    GitlabProject,
    IacFile,
    K8sManifestResource,
    TerraformResource,
)
from .backup import BackupJob
from .cloud_k8s_net import (
    CloudResource,
    K8sDeployment,
    K8sNode,
    K8sPod,
    NetDevice,
    NetDiscoveryHost,
    NetDiscoveryService,
)
from .container_registry import ContainerImage
from .core import (
    _EMBED_DIM,
    DOCUMENT_CHUNKS_TSV_ADD_COLUMN_DDL,
    DOCUMENT_CHUNKS_TSV_COLUMN,
    DOCUMENT_CHUNKS_TSV_EXPR,
    DOCUMENT_CHUNKS_TSV_INDEX,
    DOCUMENT_CHUNKS_TSV_INDEX_DDL,
    ZONE_CORPORATE,
    AuditLog,
    CollectionRun,
    ConfluencePage,
    CustomView,
    Document,
    DocumentChunk,
    DriftEvent,
    GeneratedScript,
    HostIdentity,
    ImportedSkill,
    Instinct,
    IntegrationProposal,
    JiraTicket,
    McpApiKey,
    Observation,
    PolicyRegistry,
    ProposedAction,
    Resource,
    ResourceConfig,
    RootCauseNote,
    ScanPoint,
    Snapshot,
    UIUser,
    Workspace,
    drift_recency,
)
from .dns import DnsRecord, DnsZone
from .environment_notes import EnvironmentNote
from .governance import (
    AgentActionLog,
    AgentConfigSetting,
    AgentDecisionLog,
    ComplianceViolation,
    InventoryReconcileEvent,
    RuntimeConfig,
)
from .graph import (
    CONFIDENCE_DECLARED,
    CONFIDENCE_DETERMINISTIC_NAME,
    CONFIDENCE_PROBABILISTIC_NAME,
    GraphEdge,
    GraphEdgeAuthority,
    GraphEdgeMethod,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
)
from .host_posture import (
    HostCertificate,
    HostFirewallRule,
    HostSecurityPosture,
    HostShare,
    WindowsLocalGroupMember,
    WindowsLocalUser,
    WindowsLogonEvent,
)
from .host_purpose import HostPurposeMap
from .identity import IdentityGroupMembership, IdentityPrincipal
from .instinct_governance import (
    InstinctApproval,
    InstinctProposal,
    InstinctVersion,
)
from .licensing import SoftwareLicense
from .loadbalancer import (
    LbInstance,
    LbPool,
    LbPoolMember,
    LbVirtualServer,
)
from .octopus import (
    OctopusAccount,
    OctopusActionTemplate,
    OctopusChannel,
    OctopusDeployment,
    OctopusDeploymentStep,
    OctopusEnvironment,
    OctopusEvent,
    OctopusFeed,
    OctopusInterruption,
    OctopusLibraryVariableSet,
    OctopusLifecycle,
    OctopusMachine,
    OctopusMachineRole,
    OctopusProject,
    OctopusProjectGroup,
    OctopusRelease,
    OctopusTask,
    OctopusTeam,
    OctopusUser,
    OctopusVariable,
)
from .os_inventory import (
    EolRegistry,
    LinuxCron,
    LinuxHost,
    LinuxMount,
    LinuxNic,
    LinuxPackage,
    LinuxPendingUpdate,
    LinuxPort,
    LinuxService,
    LinuxUser,
    WindowsPatchState,
    WindowsService,
    WindowsSoftware,
)
from .ownership import ResourceOwnership
from .pki import CertificateAuthority
from .rapid7 import (
    R7Asset,
    R7AssetAddress,
    R7AssetConfig,
    R7AssetSite,
    R7AssetUser,
    R7Site,
    R7Software,
    R7Solution,
    R7Tag,
    R7VulnCve,
    R7Vulnerability,
    R7VulnSolution,
    VulnQueueItem,
)
from .saas_inventory import (
    SaaSApiKeyMetadata,
    SaaSApplication,
)
from .secrets_inventory import (
    BACKEND_AWS,
    BACKEND_BITWARDEN,
    BACKEND_VAULT,
    SECRET_RECORD_COLUMNS,
    SecretRecord,
)
from .vsphere import (
    VsphereAlarm,
    VsphereCluster,
    VsphereDatacenter,
    VsphereDatastore,
    VsphereDatastoreMetric,
    VsphereHost,
    VsphereHostMetric,
    VsphereLicense,
    VsphereNetwork,
    VspherePermission,
    VsphereResourcePool,
    VsphereSession,
    VsphereSnapshot,
    VsphereVm,
    VsphereVmDisk,
    VsphereVmMetric,
)
from .webhooks import (
    DEFAULT_MAX_ATTEMPTS,
    DELIVERY_DELIVERED,
    DELIVERY_EXHAUSTED,
    DELIVERY_PENDING,
    DELIVERY_RETRYING,
    RETRY_BACKOFF_MINUTES,
    WebhookDelivery,
    WebhookSubscription,
)
from .governance_events_ext import GovernanceEvent
from .client_state import ClientObservation, ClientStateEntry
from .agent_task_runs import AgentTaskRun

__all__ = [
    # shared objects
    "Base",
    "JSONB",
    "_now",
    "_uuid",
    # core
    "AuditLog",
    "CollectionRun",
    "ConfluencePage",
    "CustomView",
    "Document",
    "DocumentChunk",
    "_EMBED_DIM",
    # TRK-297 R6 — the deliberately unmodeled PostgreSQL FTS column on
    # document_chunks (see db/models/core.py for why it is not a mapped_column).
    "DOCUMENT_CHUNKS_TSV_COLUMN",
    "DOCUMENT_CHUNKS_TSV_INDEX",
    "DOCUMENT_CHUNKS_TSV_EXPR",
    "DOCUMENT_CHUNKS_TSV_ADD_COLUMN_DDL",
    "DOCUMENT_CHUNKS_TSV_INDEX_DDL",
    "DriftEvent",
    "drift_recency",
    "GeneratedScript",
    "HostIdentity",
    "ImportedSkill",
    "Instinct",
    "IntegrationProposal",
    "JiraTicket",
    "McpApiKey",
    "Observation",
    "PolicyRegistry",
    "ProposedAction",
    "Resource",
    "ResourceConfig",
    "RootCauseNote",
    "ScanPoint",
    "Snapshot",
    "UIUser",
    "Workspace",
    "ZONE_CORPORATE",
    # os_inventory
    "EolRegistry",
    "LinuxCron",
    "LinuxHost",
    "LinuxMount",
    "LinuxNic",
    "LinuxPackage",
    "LinuxPendingUpdate",
    "LinuxPort",
    "LinuxService",
    "LinuxUser",
    "WindowsPatchState",
    "WindowsService",
    "WindowsSoftware",
    # host_posture (MR-J / INV-4; TRK-031 adds HostFirewallRule)
    "HostCertificate",
    "HostFirewallRule",
    "HostSecurityPosture",
    "HostShare",
    "WindowsLocalGroupMember",
    "WindowsLocalUser",
    "WindowsLogonEvent",
    # host_purpose (MR-J item 3)
    "HostPurposeMap",
    # environment_notes (P4.2a/P4.3a — human-written narrative layer)
    "EnvironmentNote",
    # instinct_governance (P4.2c/P4.2d/P4.3b — approval + version history + proposal queue)
    "InstinctApproval",
    "InstinctProposal",
    "InstinctVersion",
    # loadbalancer (issue #100)
    "LbInstance",
    "LbPool",
    "LbPoolMember",
    "LbVirtualServer",
    # identity (issue #102)
    "IdentityPrincipal",
    "IdentityGroupMembership",
    # licensing (issue #97)
    "SoftwareLicense",
    # ownership (issue #116)
    "ResourceOwnership",
    # pki (issue #94)
    "CertificateAuthority",
    # container_registry (issue #101)
    "ContainerImage",
    # dns (issue #95)
    "DnsRecord",
    "DnsZone",
    # backup (issue #96)
    "BackupJob",
    # secrets-manager inventory, METADATA ONLY (issue #98)
    "BACKEND_AWS",
    "BACKEND_BITWARDEN",
    "BACKEND_VAULT",
    "SECRET_RECORD_COLUMNS",
    "SecretRecord",
    # rapid7
    "R7Asset",
    "R7AssetAddress",
    "R7AssetConfig",
    "R7AssetUser",
    "R7Site",
    "R7Software",
    "R7Solution",
    "R7Tag",
    "R7Vulnerability",
    "R7VulnCve",
    "R7AssetSite",
    "R7VulnSolution",
    "VulnQueueItem",
    # octopus
    "OctopusAccount",
    "OctopusActionTemplate",
    "OctopusChannel",
    "OctopusDeployment",
    "OctopusDeploymentStep",
    "OctopusEnvironment",
    "OctopusEvent",
    "OctopusFeed",
    "OctopusInterruption",
    "OctopusLibraryVariableSet",
    "OctopusLifecycle",
    "OctopusMachine",
    "OctopusMachineRole",
    "OctopusProject",
    "OctopusProjectGroup",
    "OctopusRelease",
    "OctopusTask",
    "OctopusTeam",
    "OctopusUser",
    "OctopusVariable",
    # vsphere
    "VsphereAlarm",
    "VsphereCluster",
    "VsphereDatacenter",
    "VsphereDatastore",
    "VsphereDatastoreMetric",
    "VsphereHost",
    "VsphereHostMetric",
    "VsphereNetwork",
    "VsphereResourcePool",
    "VsphereSnapshot",
    "VsphereVm",
    "VsphereVmDisk",
    "VsphereVmMetric",
    "VsphereLicense",
    "VspherePermission",
    "VsphereSession",
    # cloud_k8s_net
    "CloudResource",
    "K8sDeployment",
    "K8sNode",
    "K8sPod",
    "NetDevice",
    "NetDiscoveryHost",
    "NetDiscoveryService",
    # ansible
    "AnsibleInventoryGroup",
    "AnsibleInventoryHost",
    "AnsiblePlaybookPlay",
    "CiPipelineRun",
    "CiSchedule",
    "ComposeService",
    "GitlabProject",
    "IacFile",
    "K8sManifestResource",
    "TerraformResource",
    # relationship graph phase 2 (issue #126)
    "CONFIDENCE_DECLARED",
    "CONFIDENCE_DETERMINISTIC_NAME",
    "CONFIDENCE_PROBABILISTIC_NAME",
    "GraphEdge",
    "GraphEdgeAuthority",
    "GraphEdgeMethod",
    "GraphEdgeType",
    "GraphNode",
    "GraphNodeType",
    # governance
    "AgentActionLog",
    "AgentConfigSetting",
    "AgentDecisionLog",
    "ComplianceViolation",
    "InventoryReconcileEvent",
    "RuntimeConfig",
    # saas_inventory (GitLab #103)
    "SaaSApiKeyMetadata",
    "SaaSApplication",
    # webhooks (outbound subscription/event-publish system, issue #112)
    "DEFAULT_MAX_ATTEMPTS",
    "DELIVERY_DELIVERED",
    "DELIVERY_EXHAUSTED",
    "DELIVERY_PENDING",
    "DELIVERY_RETRYING",
    "RETRY_BACKOFF_MINUTES",
    "WebhookDelivery",
    "WebhookSubscription",
    # relationships — transitional reader shim, not a model. See the module
    # docstring; goes with the sibling branch that retires the legacy readers.
    # governance_events_ext (P4.2g / P4.3d — hash-chained append-only ledger)
    "GovernanceEvent",
    # client_state (P4.2e/P4.2f convergence plan — infra-ops client-local
    # state persistence, scoped to the wave_failures + session_debrief use cases)
    "ClientObservation",
    "ClientStateEntry",
    # agent_task_runs (P6.1 convergence plan Phase 6 — headless-runner
    # standing-task execution records)
    "AgentTaskRun",
]
