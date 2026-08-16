"""Knowledge-graph edge VOCABULARY for infra-brain.

**The store this module used to define is gone.** P5 of
``docs/decisions/2026-08-11-graph-first-architecture.md`` dropped
``resource_relationships``; ``graph_edges`` (``db/models/graph.py``,
materialised by ``graph_engine`` from collector declarations) is the edge store
now. What is left here is the part that was never about the table:

* :class:`RelationshipType` — the canonical edge-label taxonomy, still the
  reference for what each label MEANS and which way it points. Read an edge as
  ``from`` *verb* ``to``: vm RUNS_ON esxi_host, host MEMBER_OF cluster,
  r7_asset VULNERABLE_TO cve_resource.
* The per-type ``*Props`` dataclasses — the documented property shape of each
  label, and the record of which component derived it.
* :data:`DEFERRED_RELATIONSHIP_TYPES` / :data:`MIGRATED_TO_GRAPH_EDGES` /
  :data:`RETIRED_CONTAINMENT_DERIVATIONS` — the three "why is nothing emitting
  this?" answers, which a reader needs MORE now that the rows are gone, not
  less.

Deliberately NOT here any more: the ``ResourceRelationship`` ORM model and the
``emit_edge`` / ``emit_edges_batch`` write helpers. Every collector that called
them now declares its edges on ``AgentSpec.emits_edges`` (or has had a
derivation retired on record). A no-op stub was considered and rejected: it
would have turned every one of those retirements into a silent success.

Two transitional pieces remain at the bottom of the file — a detached
``Table`` shim and the legacy ``get_neighborhood`` walk — purely so this wave's
sibling reader branch still imports. Both carry their own epitaph.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Index,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# Cross-dialect JSON: JSONB on PostgreSQL, JSON/TEXT on SQLite (tests).
JSONB = JSON().with_variant(_PG_JSONB(), "postgresql")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Relationship taxonomy
# ---------------------------------------------------------------------------


class RelationshipType(str, Enum):
    """Controlled vocabulary for directed edges in the resource graph.

    Grouped by domain; the string value is stored in ``relationship_type``.
    """

    # Infrastructure topology
    RUNS_ON = "RUNS_ON"  # vm RUNS_ON esxi_host
    MEMBER_OF = "MEMBER_OF"  # host MEMBER_OF cluster / group
    MANAGES = "MANAGES"  # octopus_project MANAGES octopus_machine
    HOSTED_BY = "HOSTED_BY"  # container HOSTED_BY vm  [DEFERRED — no container collector]
    IN_DATACENTER = "IN_DATACENTER"  # cluster IN_DATACENTER datacenter
    STORED_ON = "STORED_ON"  # vm STORED_ON datastore
    ATTACHED_TO = "ATTACHED_TO"  # vm ATTACHED_TO network

    # Hardware components (first-class Resource nodes)
    HAS_DISK = "HAS_DISK"  # esxi_host HAS_DISK disk_resource
    HAS_NIC = "HAS_NIC"  # esxi_host HAS_NIC nic_resource
    HAS_HBA = "HAS_HBA"  # esxi_host HAS_HBA hba_resource
    HAS_PHYSICAL_DISK = "HAS_PHYSICAL_DISK"  # esxi_host HAS_PHYSICAL_DISK physical_disk_resource

    # Deployment / CI-CD
    DEPLOYED_TO = "DEPLOYED_TO"  # project DEPLOYED_TO environment
    DEPLOYS_TO = "DEPLOYS_TO"  # machine DEPLOYS_TO environment
    TRIGGERED_BY = "TRIGGERED_BY"  # deployment TRIGGERED_BY ci_pipeline
    DEPENDS_ON = "DEPENDS_ON"  # project DEPENDS_ON library_variable_set
    HAS_SCHEDULE = "HAS_SCHEDULE"  # gitlab_project --HAS_SCHEDULE--> ci_schedule resource (CI pipeline schedule) — TRK-104

    # Vulnerability / patch
    VULNERABLE_TO = "VULNERABLE_TO"  # asset VULNERABLE_TO cve_resource
    PATCHED_BY = "PATCHED_BY"  # asset PATCHED_BY solution
    HAS_SOFTWARE = "HAS_SOFTWARE"  # asset HAS_SOFTWARE software_resource
    TAGGED_AS = "TAGGED_AS"  # asset TAGGED_AS r7_tag

    # Configuration / IaC
    BELONGS_TO = "BELONGS_TO"  # variable BELONGS_TO project
    DEFINED_IN = "DEFINED_IN"  # resource DEFINED_IN iac_file
    PART_OF = "PART_OF"  # host PART_OF inventory_group  [DEFERRED — group not a Resource]

    # Compliance
    HAS_VIOLATION = "HAS_VIOLATION"  # resource HAS_VIOLATION compliance_violation_resource
    RELATED_TO = "RELATED_TO"  # compliance_violation RELATED_TO drift_event / policy (KG-9)

    # Identity resolution
    IS_SAME_AS = "IS_SAME_AS"  # canonical cross-source identity link
    IS_PRINCIPAL_FOR = "IS_PRINCIPAL_FOR"  # identity_principal (IdP identity, issue #102)
    # IS_PRINCIPAL_FOR --> identity/user_account convergence node (per-system
    # account, built by graph_maintenance._populate_convergence_nodes)

    # Ansible
    ANSIBLE_MANAGES = "ANSIBLE_MANAGES"  # inventory_group ANSIBLE_MANAGES host

    # Drift (KG coverage gap 4)
    HAS_DRIFT = "HAS_DRIFT"  # host HAS_DRIFT drift_event_resource

    # End-of-life (KG coverage gap 7)
    RUNS_EOL = "RUNS_EOL"  # host RUNS_EOL eol_product_resource

    # Software vulnerability (KG-5)
    AFFECTED_BY = "AFFECTED_BY"  # software_resource AFFECTED_BY cve/vuln_resource

    # CI → CD bridge (KG-4)
    DEPLOYED_VIA = "DEPLOYED_VIA"  # gitlab_project DEPLOYED_VIA octopus_project

    # ── Convergence nodes (TRK-103) — shared attribute values promoted to
    # first-class nodes so many resources can pivot on them. Emitters live in
    # graph_maintenance._populate_convergence_nodes.
    COVERS = "COVERS"  # r7_vulnerability COVERS cve  (a vuln covers one/more CVEs)
    FIXES = "FIXES"  # r7_solution FIXES cve  (a patch fixes one/more CVEs)
    HAS_ACCOUNT = "HAS_ACCOUNT"  # host HAS_ACCOUNT user_account
    IN_SUBNET = "IN_SUBNET"  # host IN_SUBNET subnet (/24 CIDR node)
    RUNS_OS = "RUNS_OS"  # host RUNS_OS os_version
    INSTANCE_OF = "INSTANCE_OF"  # compliance_violation INSTANCE_OF compliance_rule
    ON_FIELD = "ON_FIELD"  # drift_event ON_FIELD drift_field
    HAS_ROLE = "HAS_ROLE"  # octopus_machine HAS_ROLE octopus_role

    # ── vSphere governance convergence edges (TRK-102) — highest-value
    # governance links per the KG audit. Emitters live in
    # graph_maintenance._populate_convergence_nodes.
    GRANTED_ON = "GRANTED_ON"  # user_account (vsphere_permission principal) GRANTED_ON entity
    RAISED_ON = "RAISED_ON"  # vsphere_alarm RAISED_ON entity
    ASSIGNED_TO = "ASSIGNED_TO"  # vsphere_license ASSIGNED_TO entity (host / vCenter)

    # ── Software vendor convergence (TRK-102 item-9, ~1,006 fan-in) ──────
    MADE_BY = "MADE_BY"  # software_title MADE_BY vendor

    # ── Version / hardware / infra convergence nodes (TRK-103 round 2) ────
    # Shared version/hardware/infra attribute values promoted to first-class
    # nodes so many resources pivot on them. Emitters live in
    # graph_maintenance._populate_convergence_nodes.
    RUNS_TOOLS_VERSION = "RUNS_TOOLS_VERSION"  # vsphere_vm RUNS_TOOLS_VERSION vmware_tools_version
    RUNS_TENTACLE_VERSION = (
        "RUNS_TENTACLE_VERSION"  # octopus_machine RUNS_TENTACLE_VERSION tentacle_version
    )
    RUNS_IMAGE = "RUNS_IMAGE"  # iac_file (compose) RUNS_IMAGE container_image

    # ── Container image registry (GitLab issue #101) ─────────────────────
    # Registry-side only — does NOT activate HOSTED_BY (container->vm/k8s_node
    # runtime placement stays [DEFERRED — no container collector], per the
    # issue's own scope-correction comment: that needs a separate
    # running-container collector, still blocked on TRK-041/K8s).
    PULLED_FROM = "PULLED_FROM"  # container_image PULLED_FROM registry
    HAS_VULNERABILITY_SCAN = (
        "HAS_VULNERABILITY_SCAN"  # container_image HAS_VULNERABILITY_SCAN container_scan
    )
    RUNS_ESXI_BUILD = "RUNS_ESXI_BUILD"  # esxi_host RUNS_ESXI_BUILD esxi_build
    HAS_HARDWARE_MODEL = "HAS_HARDWARE_MODEL"  # esxi_host HAS_HARDWARE_MODEL hardware_model
    HAS_CPU_MODEL = "HAS_CPU_MODEL"  # esxi_host HAS_CPU_MODEL cpu_model
    HAS_HARDWARE_VENDOR = "HAS_HARDWARE_VENDOR"  # esxi_host HAS_HARDWARE_VENDOR hardware_vendor
    HAS_BIOS = "HAS_BIOS"  # esxi_host HAS_BIOS bios_version
    SENDS_SYSLOG_TO = "SENDS_SYSLOG_TO"  # esxi_host SENDS_SYSLOG_TO syslog_target
    SYNCS_TIME_WITH = "SYNCS_TIME_WITH"  # esxi_host SYNCS_TIME_WITH ntp_server
    BACKED_BY_FILER = "BACKED_BY_FILER"  # vsphere_datastore BACKED_BY_FILER nfs_filer

    # ── Convergence-node tranche (TRK-103/104) — buildable edge/node types ──
    # Shared attribute values / child rows promoted to first-class nodes.
    # Emitters live in graph_maintenance._populate_convergence_nodes.
    HAS_SNAPSHOT = "HAS_SNAPSHOT"  # vsphere_vm HAS_SNAPSHOT snapshot
    HAS_VARIABLE = "HAS_VARIABLE"  # octopus_project/library HAS_VARIABLE variable_name
    HAS_ACCESS = "HAS_ACCESS"  # octopus_team HAS_ACCESS project
    IN_POOL = "IN_POOL"  # vsphere_vm IN_POOL vsphere_resource_pool
    IN_SITE = "IN_SITE"  # r7_asset IN_SITE r7_site (many-to-many scan-site membership)

    # ── Host-posture convergence tranche (TRK-104 blocked-tier) — host-agent
    # child-table rows (LinuxPort, HostCertificate, HostShare, WindowsLocalUser/
    # GroupMember) promoted to first-class nodes. Emitters live in
    # graph_maintenance._populate_convergence_nodes.
    EXPOSES_PORT = "EXPOSES_PORT"  # linux_host EXPOSES_PORT listening_port ("port/proto" node)
    HAS_CERTIFICATE = "HAS_CERTIFICATE"  # host HAS_CERTIFICATE certificate (thumbprint node)
    HAS_SHARE = "HAS_SHARE"  # host HAS_SHARE share (storage/share node keyed "type:name")
    IN_LOCAL_GROUP = "IN_LOCAL_GROUP"  # user_account IN_LOCAL_GROUP local_group (per-host group)
    HAS_FIREWALL_RULE = "HAS_FIREWALL_RULE"  # host HAS_FIREWALL_RULE firewall_rule (security/firewall_rule convergence node keyed by rule content)
    HAS_SECURITY_POSTURE = "HAS_SECURITY_POSTURE"  # host HAS_SECURITY_POSTURE posture (security/posture per-host shadow resource — NOT a shared-value convergence node; see HasSecurityPostureProps)

    # ── Wave-3B convergence edges (TRK-104 blocked-tier) — VLAN + MAC-OUI
    # convergence. Shared network attributes (curated VLAN, MAC vendor) promoted
    # to first-class nodes. Emitters live in
    # graph_maintenance._populate_convergence_nodes.
    IN_VLAN = "IN_VLAN"  # host IN_VLAN vlan (curated host_purpose_map VLAN node)
    HAS_VENDOR = "HAS_VENDOR"  # host HAS_VENDOR mac_vendor (OUI / nmap-derived)

    # ── CI/IaC provenance (KG-2) — "walk from IaC to the live thing it
    # produced." Emitters live in graph_maintenance._populate_typed_relationships.
    TRIGGERED_DEPLOYMENT = "TRIGGERED_DEPLOYMENT"  # ci_pipeline_run TRIGGERED_DEPLOYMENT octopus_deployment (or octopus_environment fallback)
    PROVISIONS = "PROVISIONS"  # iac_file PROVISIONS resource (terraform/k8s-manifest declaration resolved to the live resource it produced)

    # ── Kubernetes internal topology (KG-3) — built now regardless of the
    # k8s collector's POC-disabled state (TRK-041), ready for when it's
    # turned on. Emitters live in graph_maintenance._populate_convergence_nodes.
    OWNS = "OWNS"  # k8s_deployment OWNS k8s_pod (ReplicaSet pod-naming-convention heuristic)

    # ── Linux OS-internals (KG-5) — closes the asymmetry with Windows
    # (WindowsLocalUser/WindowsLocalGroupMember got the TRK-104 convergence
    # treatment; LinuxService/LinuxCron/LinuxMount/LinuxPendingUpdate were
    # left out of that campaign). LinuxUser reuses HAS_ACCOUNT (see below;
    # no new type needed for it). Emitters live in
    # graph_maintenance._populate_convergence_nodes.
    RUNS_SERVICE = "RUNS_SERVICE"  # host RUNS_SERVICE service (os/service convergence node keyed by service name)
    HAS_CRON = "HAS_CRON"  # host HAS_CRON cron_job (PER-HOST node, not cross-host converged — see HasCronProps)
    HAS_MOUNT = "HAS_MOUNT"  # host HAS_MOUNT mount_point (storage/mount_point convergence node keyed by mount path)
    HAS_PENDING_UPDATE = "HAS_PENDING_UPDATE"  # host HAS_PENDING_UPDATE pending_update (patch/pending_update convergence node keyed by package:version)

    # ── PKI / Certificate Authority chain monitoring (GitLab issue #94) ────
    # Tracks the CA infrastructure itself (as opposed to HAS_CERTIFICATE,
    # which tracks per-host leaf certs). Emitters live in agents/pki.py.
    ISSUED_BY = (
        "ISSUED_BY"  # host_certificate (security/certificate node) ISSUED_BY certificate_authority
    )
    CHAINS_TO = "CHAINS_TO"  # intermediate_ca CHAINS_TO root_ca (or parent intermediate)
    HAS_CRL = "HAS_CRL"  # certificate_authority HAS_CRL crl_responder (pki/crl_responder node keyed by crl_url)
    HAS_OCSP_RESPONDER = "HAS_OCSP_RESPONDER"  # certificate_authority HAS_OCSP_RESPONDER ocsp_responder (pki/ocsp_responder node keyed by ocsp_url)

    # ── Load balancer / reverse proxy / CDN topology (GitLab issue #100).
    # Emitter: LoadBalancerAgent._write_graph_edges. F5/nginx/HAProxy/
    # Cloudflare are otherwise invisible in the topology graph even though
    # resource_relationships already models multi-hop infra topology.
    ROUTES_TO = "ROUTES_TO"  # lb_virtual_server ROUTES_TO lb_pool
    MEMBER_OF_POOL = "MEMBER_OF_POOL"  # host MEMBER_OF_POOL lb_pool (distinct from MEMBER_OF)
    TERMINATES_TLS_FOR = "TERMINATES_TLS_FOR"  # lb_instance TERMINATES_TLS_FOR certificate

    # SaaS / API-key inventory (GitLab #103)
    USES_SAAS_APP = "USES_SAAS_APP"  # team/project USES_SAAS_APP saas_application


# ---------------------------------------------------------------------------
# Per-RelationshipType property schemas (lightweight documentation layer)
#
# Each dataclass documents which keys callers should pass in the ``properties``
# JSONB bag of an edge of that type.  They are NOT enforced at the DB level —
# infra_brain's edge table stores a free-form JSONB bag.  They serve as the
# canonical reference for what to expect when reading properties from an edge,
# and are used by the declared-vs-emitted CI guard in
# tests/test_declared_vs_emitted_edges.py to enumerate every registered type.
#
# DEFERRED types:
#   HOSTED_BY  — no container/pod collector; would require a new container-domain
#                Resource type.  Deferred until a container collector ships.
#   PART_OF    — the identical fact is declared as MEMBER_OF (TRK-359 junction
#                grammar); declaring PART_OF too would store one fact under two
#                labels. See DEFERRED_RELATIONSHIP_TYPES below.
# ---------------------------------------------------------------------------


@dataclass
class RunsOnProps:
    """vm RUNS_ON esxi_host.  Emitter: vsphere collector.

    Also reused (KG-3) by graph_maintenance._populate_convergence_nodes for
    k8s_pod RUNS_ON k8s_node — the same "workload runs on this host machine"
    semantics apply cleanly to Kubernetes pod placement. ``cluster`` holds
    the vSphere cluster_name for the vm/esxi_host case, or the Kubernetes
    cluster name for the pod/node case (both an exact, FK-strength join —
    confidence 1.0 in both emitters).
    """

    cluster: str = ""  # cluster_name resolved from the host at collection time


@dataclass
class MemberOfProps:
    """host MEMBER_OF cluster / inventory_group.

    NO EMITTER into this (dropped) store — see MIGRATED_TO_GRAPH_EDGES below.
    The live producer is the TRK-359 junction declaration on
    ``IaCAgent.spec``: LinuxHost -> AnsibleInventoryGroup in ``graph_edges``,
    the group node materialised from ``ansible_inventory_groups`` rows and
    carrying its members via ``NodeSpec.row_gathers``. The vSphere host ->
    cluster derivation this class originally documented died with its retired
    domain (its ``datacenter`` prop below is kept for the historical rows);
    reviving it is a vsphere-spec declaration, per the revival obligation in
    ``etl/spec.py``.
    """

    datacenter: str = ""


@dataclass
class ManagesProps:
    """octopus_project MANAGES octopus_machine.  Emitter: octopus collector (live)."""


@dataclass
class HostedByProps:
    """container HOSTED_BY vm.  DEFERRED — no container collector yet."""


@dataclass
class InDatacenterProps:
    """cluster IN_DATACENTER datacenter.  Emitter: graph_maintenance._populate_vsphere_topology."""


@dataclass
class StoredOnProps:
    """vm STORED_ON datastore.  Emitter: graph_maintenance._populate_vsphere_topology."""

    datastore_name: str = ""


@dataclass
class AttachedToProps:
    """vm ATTACHED_TO network.  Emitter: graph_maintenance._populate_vsphere_topology."""

    network_name: str = ""


@dataclass
class HasDiskProps:
    """esxi_host HAS_DISK disk_resource.  Emitter: graph_maintenance._populate_vsphere_topology."""

    slot: str = ""
    capacity_mb: int = 0


@dataclass
class HasNicProps:
    """esxi_host HAS_NIC nic_resource.  Emitter: graph_maintenance._populate_vsphere_topology."""

    mac: str = ""
    driver: str = ""


@dataclass
class HasHbaProps:
    """esxi_host HAS_HBA hba_resource.  Emitter: graph_maintenance._populate_vsphere_topology."""

    driver: str = ""
    pci: str = ""


@dataclass
class HasPhysicalDiskProps:
    """esxi_host HAS_PHYSICAL_DISK physical_disk_resource.

    Emitter: graph_maintenance._populate_vsphere_topology.
    """

    capacity_mb: int = 0
    model: str = ""


@dataclass
class DeployedToProps:
    """project DEPLOYED_TO environment.  Emitter: graph_maintenance._populate_typed_relationships."""


@dataclass
class DeploysToProps:
    """machine DEPLOYS_TO environment.  Emitter: graph_maintenance._populate_typed_relationships."""

    roles: list = field(default_factory=list)


@dataclass
class TriggeredByProps:
    """deployment TRIGGERED_BY ci_pipeline.  Emitter: iac collector."""


@dataclass
class DependsOnProps:
    """compose_service DEPENDS_ON compose_service.  Emitter: iac collector (live)."""


@dataclass
class VulnerableToProps:
    """asset VULNERABLE_TO cve_resource.  Emitters: graph_maintenance._populate_typed_relationships and vuln._write_graph_edges (both emit this 4-key superset)."""

    cve_id: str = ""
    severity: str = ""
    exploits: int = 0
    malware_kits: int = 0


@dataclass
class PatchedByProps:
    """asset PATCHED_BY solution.  Emitter: graph_maintenance._populate_typed_relationships."""

    solution_type: str = ""


@dataclass
class HasSoftwareProps:
    """asset HAS_SOFTWARE software_resource.  Emitter: graph_maintenance._populate_typed_relationships."""

    version: str = ""
    vendor: str = ""
    source: str = ""  # "windows" | "rapid7"


@dataclass
class TaggedAsProps:
    """asset TAGGED_AS tag_resource.  Emitter: graph_maintenance._populate_typed_relationships."""

    tag_type: str = ""
    color: str = ""


@dataclass
class BelongsToProps:
    """variable BELONGS_TO project.  Emitter: iac collector."""


@dataclass
class DefinedInProps:
    """resource DEFINED_IN iac_file.

    NO LONGER EMITTED INTO THIS STORE. Migrated to a declaration on iac's
    AgentSpec (``EdgeDirection.INVERSE``) and materialised by ``graph_engine``
    into ``graph_edges``; ``graph_maintenance._populate_typed_relationships``
    no longer derives it. The type and this props class stay for the historical
    ``resource_relationships`` rows still in the table — P3/P4 of
    docs/decisions/2026-08-11-graph-first-architecture.md is what removes those.
    """

    file_path: str = ""


@dataclass
class PartOfProps:
    """host PART_OF inventory_group.  DEFERRED — the fact is declared as MEMBER_OF.

    The grammar gap that used to block this (no Resource row for the group)
    was closed by TRK-359's junction grammar; what keeps PART_OF undeclared
    now is that the identical fact already flows to ``graph_edges`` as
    MEMBER_OF. See DEFERRED_RELATIONSHIP_TYPES.
    """


@dataclass
class HasViolationProps:
    """resource HAS_VIOLATION compliance_violation_resource.

    Emitter: graph_maintenance._populate_typed_relationships (compliance join).
    """

    rule: str = ""
    severity: str = ""


@dataclass
class IsSameAsProps:
    """canonical cross-source identity link.  Emitter: HostReconcileAgent (sole writer)."""

    confidence: float = 1.0
    method: str = ""


@dataclass
class AnsibleManagesProps:
    """inventory_group ANSIBLE_MANAGES host.

    NO EMITTER — see MIGRATED_TO_GRAPH_EDGES below. Declared on
    ``IaCAgent.spec.emits_edges`` and materialised into ``graph_edges``,
    RE-ANCHORED onto ``AnsibleInventoryFile`` (TRK-354 Option A) because the
    project → host shape this class documents has no end iac owns. The props
    the deriver wrote (``group``/``inventory_host``, ``play_name``/
    ``play_index``) are not reproduced: the declared edge's evidence records
    the join it actually ran, and a group name is a property of the inventory
    file's own child rows rather than of the relationship.
    """


@dataclass
class RelatedToProps:
    """compliance_violation RELATED_TO drift_event / policy.

    Emitter: graph_maintenance._populate_typed_relationships (KG-9). A
    best-effort, heuristic cross-link: there is no FK between
    ComplianceViolation and DriftEvent, so the link is derived from a shared
    host resource_id + a substring match of the drifted field name inside the
    violation detail. Confidence reflects that heuristic (< 1.0).
    """

    kind: str = ""  # "drift" | "policy"
    via: str = ""  # what evidence produced the link (e.g. "shared_host+field")


@dataclass
class HasDriftProps:
    """host HAS_DRIFT drift_event_resource.

    Emitter: graph_maintenance._populate_typed_relationships (KG gap 4). Makes
    a DriftEvent (a relational-only row today) graph-visible by attaching a
    lightweight shadow Resource per drift event to the host it concerns.
    """

    drift_type: str = ""
    field: str = ""
    status: str = ""


@dataclass
class RunsEolProps:
    """host RUNS_EOL eol_product_resource.

    NO EMITTER into this (dropped) store — see MIGRATED_TO_GRAPH_EDGES below.
    The live producer is the TRK-359 junction declaration on
    ``EOLAgent.spec``: LinuxHost -> EolProduct in ``graph_edges``, the product
    node materialised from ``eol_registry`` rows with its hosts gathered off
    the rows' own anchoring resources (``RowGather(path=())``). The props the
    deriver wrote (``product``/``eol_date``, kept below for the historical
    rows) are not reproduced: the declared edge's evidence records the join it
    ran, and the EOL date is a fact about the PRODUCT, queryable on
    ``eol_registry``/the ``eol_cycle`` resource rather than restated per edge.
    """

    product: str = ""
    eol_date: str = ""


@dataclass
class AffectedByProps:
    """software_resource AFFECTED_BY cve/vuln_resource.

    Emitter: graph_maintenance._populate_typed_relationships (KG-5). Connects
    an installed-software Resource to the vulnerability (r7_vulnerability)
    Resource whose title names that product. Heuristic (title substring match
    scoped to the same host), so confidence is < 1.0.
    """

    cve_id: str = ""
    product: str = ""


@dataclass
class DeployedViaProps:
    """gitlab_project DEPLOYED_VIA octopus_project.

    Emitter: graph_maintenance._populate_typed_relationships (KG-4). Bridges
    the CI subgraph (GitLab) to the CD subgraph (Octopus) by matching project
    names, completing the ci_pipeline → project → environment → machine path.
    Name-match heuristic, so confidence is < 1.0.
    """

    match: str = ""  # normalized name used to bridge the two subgraphs


@dataclass
class HasScheduleProps:
    """gitlab_project HAS_SCHEDULE ci_schedule.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-104). Links a
    GitLab project Resource to the CI pipeline-schedule Resources it owns,
    joining ci_schedules.project_id to gitlab_projects.gitlab_project_id. Both
    endpoints are already real Resource rows — no convergence node is created.
    """

    cron: str | None = None
    ref: str | None = None
    active: bool | None = None


@dataclass
class CoversProps:
    """r7_vulnerability COVERS cve.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 CVE node).
    A single Rapid7 vulnerability (slug) may enumerate several CVE ids in its
    ``cves`` JSONB (exploded into r7_vuln_cves); one COVERS edge per (vuln, cve).
    """


@dataclass
class FixesProps:
    """r7_solution FIXES cve.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 CVE node).
    Derived transitively: r7_vuln_solutions (vuln→solution) joined to
    r7_vuln_cves (vuln→cve), so a solution FIXES every CVE its vulnerability
    covers.
    """


@dataclass
class HasAccountProps:
    """host HAS_ACCOUNT user_account.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 UserAccount).
    ``kind`` records whether the username is a domain principal ("domain") or a
    local account ("local"); ``raw`` preserves the un-normalized username.
    """

    kind: str = ""  # "domain" | "local"
    raw: str = ""


@dataclass
class InSubnetProps:
    """host IN_SUBNET subnet.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 Subnet /24).
    ``ip`` is the host address that placed it in this /24; ``source`` names the
    collector table the IP came from (rapid7 | netdiscovery | vsphere).
    """

    ip: str = ""
    source: str = ""


@dataclass
class RunsOsProps:
    """host RUNS_OS os_version.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 OSVersion).
    """

    family: str = ""
    product: str = ""
    version: str = ""
    source: str = ""  # rapid7 | vsphere


@dataclass
class InstanceOfProps:
    """compliance_violation INSTANCE_OF compliance_rule.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103
    ComplianceRule). Collapses many per-host violation shadow Resources onto the
    single rule node they instantiate.
    """


@dataclass
class OnFieldProps:
    """drift_event ON_FIELD drift_field.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 DriftField).
    Collapses noisy per-event drift shadow Resources onto the field they concern.
    """


@dataclass
class HasRoleProps:
    """octopus_machine HAS_ROLE octopus_role.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 OctopusRole).
    """


@dataclass
class GrantedOnProps:
    """user_account GRANTED_ON managed entity.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-102 vSphere
    permission). The ``from`` node is the identity/user_account convergence
    node for the VspherePermission.principal (so permissions converge on the
    same principal node the UserAccount block builds); the ``to`` node is the
    live-typed vSphere Resource named by VspherePermission.entity. Emitted only
    when the entity name resolves to a live ``vsphere_*`` Resource.
    """

    role_name: str = ""  # the vCenter role granted (e.g. "Admin")
    is_group: bool = False  # whether the principal is an AD/vSphere group
    propagate: bool = False  # whether the grant propagates to children


@dataclass
class RaisedOnProps:
    """vsphere_alarm RAISED_ON entity.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-102 vSphere
    alarm). The ``from`` node is a vsphere/alarm convergence node keyed by the
    alarm name; the ``to`` node is the live-typed vSphere Resource named by
    VsphereAlarm.entity_name. Emitted only when the entity name resolves.
    """

    status: str = ""  # overall_status of the alarm at collection time
    acknowledged: bool = False


@dataclass
class AssignedToProps:
    """vsphere_license ASSIGNED_TO entity (host / vCenter).

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-102 vSphere
    license). The ``from`` node is a vsphere/license convergence node; the
    ``to`` node is the live-typed vSphere Resource named by VsphereLicense.vcenter.
    The vsphere_licenses table carries no per-host/entity FK — only the
    ``vcenter`` scope string — so an edge is emitted only when a live vSphere
    Resource is named exactly like that vCenter server.
    """

    edition_key: str = ""
    total: int = 0
    used: int = 0


@dataclass
class MadeByProps:
    """software_title MADE_BY vendor.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-102 Vendor
    node). Promotes the vendor/publisher string already read for each
    SoftwareTitle (R7Software.vendor, WindowsSoftware.publisher,
    LinuxPackage.manager) to a first-class software/vendor node, so the ~1,006
    software titles converge on their shared publishers.
    """

    source: str = ""  # rapid7 | windows | linux


@dataclass
class RunsToolsVersionProps:
    """vsphere_vm RUNS_TOOLS_VERSION vmware_tools_version.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 round 2).
    Promotes VsphereVm.tools_version to a shared node so many VMs converge on
    a VMware Tools build. ``tools_status`` records the running/current status
    of the tools install at collection time.
    """

    tools_status: str = ""


@dataclass
class RunsTentacleVersionProps:
    """octopus_machine RUNS_TENTACLE_VERSION tentacle_version.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 round 2).
    Promotes OctopusMachine.tentacle_version to a shared node.
    """


@dataclass
class RunsImageProps:
    """iac_file (compose) RUNS_IMAGE container_image.

    NO EMITTER — see MIGRATED_TO_GRAPH_EDGES below. Derived by
    graph_maintenance._populate_convergence_nodes until the contract grew
    ChildSpec reach; now DECLARED on IaCAgent.spec and written to graph_edges,
    with the ContainerImage node declared there too rather than minted inline.
    This props class stays for the historical resource_relationships rows.
    """


@dataclass
class RunsEsxiBuildProps:
    """esxi_host RUNS_ESXI_BUILD esxi_build.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 round 2).
    Promotes VsphereHost.build to a shared node; ``version`` records the parent
    ESXi version string.
    """

    version: str = ""


@dataclass
class HasHardwareModelProps:
    """esxi_host HAS_HARDWARE_MODEL hardware_model.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 round 2).
    Promotes VsphereHost.model; ``vendor`` records the hardware vendor string.
    """

    vendor: str = ""


@dataclass
class HasCpuModelProps:
    """esxi_host HAS_CPU_MODEL cpu_model.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 round 2).
    Promotes VsphereHost.cpu_model to a shared node.
    """


@dataclass
class HasHardwareVendorProps:
    """esxi_host HAS_HARDWARE_VENDOR hardware_vendor.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 round 2).
    Promotes VsphereHost.vendor to a shared HARDWARE vendor node
    (domain="hardware") — distinct from the software MADE_BY vendor node
    (domain="software").
    """


@dataclass
class HasBiosProps:
    """esxi_host HAS_BIOS bios_version.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 round 2).
    Promotes VsphereHost.bios_version; ``bios_date`` records the firmware date.
    """

    bios_date: str = ""


@dataclass
class SendsSyslogToProps:
    """esxi_host SENDS_SYSLOG_TO syslog_target.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 round 2).
    Promotes VsphereHost.syslog_host to a shared syslog-target node.
    """


@dataclass
class SyncsTimeWithProps:
    """esxi_host SYNCS_TIME_WITH ntp_server.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 round 2).
    Promotes each element of VsphereHost.ntp_servers (JSONB list) to a shared
    NTP-server node; one edge per (host, ntp_server).
    """


@dataclass
class BackedByFilerProps:
    """vsphere_datastore BACKED_BY_FILER nfs_filer.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103 round 2).
    Promotes VsphereDatastore.remote_host (NFS backing filer) to a shared node;
    ``remote_path`` records the exported path. Emitted for NFS datastores only.
    """

    remote_path: str = ""


@dataclass
class HasSnapshotProps:
    """vsphere_vm HAS_SNAPSHOT snapshot.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103/104). Each
    VsphereSnapshot row (unique per vcenter+vm_moref+snapshot_id) becomes a
    first-class snapshot node linked to its parent VM (resolved by exact
    (vcenter, moref) join). ``age_days``/``state`` mirror the collected row.
    """

    age_days: int = 0
    state: str = ""


@dataclass
class HasVariableProps:
    """octopus_project|library_variable_set HAS_VARIABLE variable_name.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103/104).
    Promotes the METADATA-ONLY octopus_variables.name (no value / no secret) to
    a shared variable_name node so many projects/library sets pivot on a shared
    variable name. ``owner_type`` records project vs library provenance.
    """

    owner_type: str = ""  # "project" | "library"


@dataclass
class HasAccessProps:
    """octopus_team HAS_ACCESS project.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-103/104). Team's
    project_ids JSONB list resolved to OctopusProject resource nodes. The
    complementary team membership edge is ``user_account MEMBER_OF team`` (reuses
    MEMBER_OF).
    """


@dataclass
class InPoolProps:
    """vsphere_vm IN_POOL vsphere_resource_pool.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-104). Exact
    (vcenter, moref) join from VsphereVm.resource_pool_moref to the owning
    VsphereResourcePool.moref; both endpoints are already real Resource rows, so
    no convergence node is created. Confidence 1.0 (exact moref match).
    """

    vcenter: str = ""
    moref: str = ""


@dataclass
class InSiteProps:
    """r7_asset IN_SITE r7_site.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-104). Rapid7
    assets are many-to-many with scan sites; the r7_asset_sites bridge is
    resolved to a shared rapid7/site convergence node (R7Site carries no
    resource_id of its own). Confidence 1.0 (exact upstream-id membership).
    """

    r7_site_id: int = 0


@dataclass
class ExposesPortProps:
    """host EXPOSES_PORT listening_port.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-104 blocked-tier).
    Promotes each host-agent-reported LinuxPort (port, proto) to a shared
    network/listening_port node keyed "port/proto" so many hosts converge on a
    shared listening port. Scoped to LinuxPort only (host-agent-reported),
    DISTINCT from network-scanned net_discovery_services. ``process``/``state``
    mirror the collected row; ``source`` is always "linux".
    """

    process: str = ""
    state: str = ""
    source: str = ""


@dataclass
class HasCertificateProps:
    """host HAS_CERTIFICATE certificate.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-104 blocked-tier).
    Promotes each HostCertificate to a shared security/certificate node keyed by
    thumbprint (a good global dedup key — the same cert deployed to many hosts
    converges on one node); node metadata carries {subject, issuer, not_after,
    thumbprint}. Direct resource_id join (host_certificates.resource_id →
    resources.id; no intermediate host table). Populated by windows.py AND
    linux.py. Rows with an empty thumbprint are skipped (cannot be deduped
    globally). Edge ``properties`` carry per-host context.
    """

    store: str = ""
    is_expired: bool = False
    days_until_expiry: int = 0


@dataclass
class HasShareProps:
    """host HAS_SHARE share.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-104 blocked-tier).
    Promotes each HostShare to a shared storage/share node keyed
    "share_type:name" so many hosts converge on a shared exposed share (enables
    "who exposes share X" queries); node metadata carries {share_type, name}.
    Direct resource_id join (host_shares.resource_id → resources.id; no
    intermediate host table). Populated by windows.py + linux.py. ``path`` is the
    per-host filesystem path backing the share.
    """

    share_type: str = ""  # "smb" | "nfs"
    path: str = ""


@dataclass
class InLocalGroupProps:
    """user_account IN_LOCAL_GROUP local_group.

    Emitter: graph_maintenance._populate_convergence_nodes (TRK-104 blocked-tier).
    From WindowsLocalGroupMember: the member username is fed into the SAME shared
    identity/user_account convergence node the HAS_ACCOUNT block builds, and the
    local group is promoted to a per-host security/local_group node keyed
    "<host_resource_id>/<group_name>" (local groups are host-scoped, so they are
    deliberately NOT converged across hosts). A NEW type rather than reusing
    MEMBER_OF: MEMBER_OF's documented semantics are host-topology (host MEMBER_OF
    cluster/group); this edge's subject is a user_account and its object a local
    security group, so overloading MEMBER_OF would pollute host-topology queries.
    ``raw_member`` preserves the un-normalized member string.
    """

    group_name: str = ""
    raw_member: str = ""


@dataclass
class HasVendorProps:
    """host HAS_VENDOR mac_vendor (OUI).

    Emitter: graph_maintenance._populate_convergence_nodes (Wave-3B, TRK-104
    blocked-tier). Promotes each host's MAC-derived hardware vendor to a shared
    network/mac_vendor node keyed by vendor name so many hosts/NICs converge on
    one vendor. Deliberately DISTINCT from the software MADE_BY (software/vendor)
    and hardware HAS_HARDWARE_VENDOR (hardware/vendor) nodes: a MAC OUI vendor is
    the NIC silicon maker, a different axis, so it gets its own domain/type to
    avoid collision. Two sources:
      * NetDiscoveryHost.mac_vendor  — already resolved by nmap at scan time
        (used verbatim; no lookup).
      * LinuxNic.mac                 — no vendor column; resolved via the
        in-code static OUI starter map (_mac_oui_vendor).
    Both are exact prefix / scan-resolved matches, so confidence is 1.0. ``mac``
    is the NIC address that resolved the vendor; ``source`` is
    "netdiscovery" | "linux".
    """

    mac: str = ""
    source: str = ""


@dataclass
class HasFirewallRuleProps:
    """host HAS_FIREWALL_RULE firewall_rule.

    Emitter: graph_maintenance._populate_convergence_nodes (KG-1). Promotes
    each HostFirewallRule row to a shared security/firewall_rule convergence
    node keyed "<source>:<table_name>:<chain>:<rule_text>" — the same rule
    recurring across many hosts converges on one node (e.g. "who allows
    inbound 22 from 0.0.0.0/0" becomes a single high-fan-in query). Direct
    resource_id join (host_firewall_rules.resource_id -> resources.id; no
    intermediate host table), mirroring HostCertificate/HostShare. First
    writer is the Linux agent (iptables/nftables/firewalld); nothing ties
    the table structurally to Linux.
    """

    table_name: str = ""
    chain: str = ""
    action: str = ""
    source: str = ""  # "iptables" | "nftables" | "firewalld"


@dataclass
class HasSecurityPostureProps:
    """host HAS_SECURITY_POSTURE posture.

    Emitter: graph_maintenance._populate_convergence_nodes (KG-1). Unlike
    HAS_CERTIFICATE/HAS_SHARE/HAS_FIREWALL_RULE, HostSecurityPosture is NOT a
    shared value worth converging — it is a per-host SUMMARY row
    (unique=True on resource_id) — so this makes the row graph-visible via a
    lightweight PER-HOST shadow Resource (domain="security", type="posture",
    keyed by the host's own resource_id so it can never collide across
    hosts), mirroring the HAS_DRIFT/RUNS_EOL shadow-resource precedent
    rather than the convergence-node precedent. Edge properties carry the
    full posture snapshot so the graph is queryable without a join back to
    host_security_posture.
    """

    firewall_enabled: bool | None = None
    av_enabled: bool | None = None
    rdp_enabled: bool | None = None
    uac_enabled: bool | None = None
    ssh_password_auth: bool | None = None
    selinux_mode: str = ""
    apparmor_status: str = ""


@dataclass
class InVlanProps:
    """host IN_VLAN vlan.

    Emitter: graph_maintenance._populate_convergence_nodes (Wave-3B, TRK-104
    blocked-tier). Promotes each curated HostPurposeMap.vlan value to a shared
    network/vlan node keyed by the VLAN string (e.g. "VLAN10") so many hosts
    converge on one VLAN. HostPurposeMap has NO resource_id FK, so the host is
    resolved by matching HostPurposeMap.hostname to a host-type Resource.name
    (case-insensitive, with an FQDN short-name fallback). This is a
    curated-map-derived, string-join edge, so confidence is < 1.0 (0.8). Rows
    with an empty vlan or no matching host Resource are skipped. ``source``
    mirrors HostPurposeMap.source (the "<gitlab_project_id>:<file>" origin).
    """

    source: str = ""


@dataclass
class TriggeredDeploymentProps:
    """ci_pipeline_run TRIGGERED_DEPLOYMENT octopus_deployment (or octopus_environment).

    Emitter: graph_maintenance._populate_typed_relationships (KG-2). No FK
    exists between GitLab CI and Octopus Deploy at the per-run level; this
    reuses the DEPLOYED_VIA project-level bridge (emitted immediately
    before, same pass, same session) to find the pipeline's Octopus
    project, then narrows to the deployment whose ``created`` timestamp
    falls within a 24h window after the pipeline run. Falls back to the
    deployment's OctopusEnvironment resource when the matched deployment
    itself has no resource_id. Heuristic (name-match + time-window), so
    confidence < 1.0.
    """

    deployment_octopus_id: str = ""
    environment_name: str = ""
    match_window_hours: int = 24
    candidate_count: int = 0
    fallback: str = ""  # "deployment" | "environment"


@dataclass
class ProvisionsProps:
    """iac_file PROVISIONS resource (terraform resource block / k8s manifest
    resolved to the live resource it produced).

    Emitter: graph_maintenance._populate_typed_relationships (KG-2). Neither
    TerraformResource nor K8sManifestResource carries its own resource_id,
    so the FROM endpoint is the owning IacFile's resource_id (mirrors the
    RunsImageProps/ComposeService anchor precedent — compose_services has no
    Resource of its own either). The K8sManifestResource match is a
    composite-name convention match against K8sAgent's own resource-naming
    convention (confidence 0.85); the TerraformResource match is a
    resource_type-prefix-scoped exact name match against the terraform
    block's own label — the only signal the current regex HCL parser
    exposes, no attrs/name= body (confidence 0.5, the weakest heuristic in
    this file).
    """

    resource_type: str = ""
    resource_name: str = ""
    kind: str = ""
    name: str = ""
    namespace: str = ""
    match: str = ""


@dataclass
class OwnsProps:
    """k8s_deployment OWNS k8s_pod.

    Emitter: graph_maintenance._populate_convergence_nodes (KG-3). K8sPod
    carries no ownerReference/label data in this schema (K8sAgent captures
    only namespace/phase/node_name/containers), so ownership is inferred
    from the Kubernetes ReplicaSet pod-naming convention: a Deployment's
    pods are named "<deployment-name>-<hash>-<hash>", so a K8sPod whose
    name starts with "<deployment-name>-" in the same (cluster, namespace)
    is treated as owned. Heuristic (name-prefix match, not a real
    ownerReference), so confidence < 1.0.
    """

    match: str = ""  # the deployment-name prefix used to match


@dataclass
class RunsServiceProps:
    """host RUNS_SERVICE service.

    Emitter: graph_maintenance._populate_convergence_nodes (KG-5). Promotes
    each LinuxService.name to a shared os/service convergence node — many
    hosts running the same service name (e.g. "nginx") converge on one node,
    so "who runs nginx" becomes a single high-fan-in query. Closes the
    Linux-side asymmetry with the Windows local-identity convergence work
    (TRK-104) that never covered LinuxService. Joined via
    LinuxHost.resource_id (LinuxService keys off host_id, not resource_id
    directly), mirroring the EXPOSES_PORT (LinuxPort) join pattern.
    """

    state: str = ""
    enabled: bool = False


@dataclass
class HasCronProps:
    """host HAS_CRON cron_job.

    Emitter: graph_maintenance._populate_convergence_nodes (KG-5). Cron
    jobs are host-specific (not a meaningfully shared value across hosts),
    so — unlike RUNS_SERVICE/HAS_MOUNT/HAS_PENDING_UPDATE — the node is
    keyed PER-HOST ("<host_resource_id>/<owner>/<schedule>"), mirroring the
    IN_LOCAL_GROUP per-host local_group precedent rather than a cross-host
    convergence node. Joined via LinuxHost.resource_id.
    """

    owner: str = ""
    schedule: str = ""
    command: str = ""


@dataclass
class HasMountProps:
    """host HAS_MOUNT mount_point.

    Emitter: graph_maintenance._populate_convergence_nodes (KG-5). Promotes
    each LinuxMount.mount path to a shared storage/mount_point convergence
    node — many hosts sharing a mount path (e.g. "/data") converge on one
    node, so "who mounts /data" becomes a single query. Joined via
    LinuxHost.resource_id.
    """

    device: str = ""
    fstype: str = ""
    size_total_gb: float | None = None
    size_available_gb: float | None = None


@dataclass
class HasPendingUpdateProps:
    """host HAS_PENDING_UPDATE pending_update.

    Emitter: graph_maintenance._populate_convergence_nodes (KG-5). Promotes
    each LinuxPendingUpdate (package, available_version) pair to a shared
    patch/pending_update convergence node — many hosts pending the same
    package/version converge on one node, mirroring the CVE convergence
    node's "who is affected by X" query shape. Joined via
    LinuxHost.resource_id.
    """

    current_version: str = ""
    security: bool = False
    manager: str = ""


@dataclass
class IssuedByProps:
    """host_certificate ISSUED_BY certificate_authority.

    NO EMITTER — see MIGRATED_TO_GRAPH_EDGES below. Declared on
    ``PKIAgent.spec.emits_edges``; the description that follows is unchanged
    because the declaration reproduces it exactly (same endpoints, same exact
    string join, same 0.900).

    ``from`` endpoint is the existing security/certificate convergence node
    (keyed by thumbprint — same node graph_maintenance's HAS_CERTIFICATE
    emitter uses, joined here by re-running the same get-or-create natural
    key rather than by hard dependency on run order). Matched by exact
    string equality between ``host_certificates.issuer`` and the tracked
    CA's ``name`` — a deterministic, non-fuzzy match, but a display-name
    join, so confidence is < 1.0.
    """

    thumbprint: str = ""


@dataclass
class ChainsToProps:
    """intermediate_ca CHAINS_TO root_ca (or parent intermediate).  Emitter: agents/pki.py.

    Matched by exact string equality between the intermediate's ``issuer``
    and the parent CA's ``name`` — same deterministic-match caveat as
    IssuedByProps.
    """

    intermediate_name: str = ""
    parent_name: str = ""


@dataclass
class HasCrlProps:
    """certificate_authority HAS_CRL crl_responder.  Emitter: agents/pki.py.

    ``to`` endpoint is a pki/crl_responder convergence node keyed by
    ``crl_url`` (many CAs can share a CRL distribution point). Edge
    properties carry the last read-only GET health check result.
    """

    status: str = ""  # "reachable" | "unreachable"
    checked_at: str = ""


@dataclass
class HasOcspResponderProps:
    """certificate_authority HAS_OCSP_RESPONDER ocsp_responder.  Emitter: agents/pki.py.

    ``to`` endpoint is a pki/ocsp_responder convergence node keyed by
    ``ocsp_url``. Edge properties carry the last read-only GET health check
    result (a simplified reachability probe, not a full OCSP protocol
    exchange — see agents/pki.py docstring).
    """

    status: str = ""  # "reachable" | "unreachable"
    checked_at: str = ""


@dataclass
class RoutesToProps:
    """lb_virtual_server ROUTES_TO lb_pool.

    Emitter: LoadBalancerAgent._write_graph_edges (GitLab issue #100). Both
    endpoints are already real Resource rows (LbVirtualServer.pool_id ->
    LbPool, an exact FK-strength join the collector itself resolved), so
    confidence is 1.0 / method "declared".
    """

    vendor: str = ""  # "f5" | "nginx" | "haproxy" | "cloudflare"


@dataclass
class MemberOfPoolProps:
    """host MEMBER_OF_POOL lb_pool.

    Emitter: LoadBalancerAgent._write_graph_edges (GitLab issue #100). A NEW
    type rather than reusing MEMBER_OF: MEMBER_OF's documented semantics are
    vSphere host-topology (host MEMBER_OF cluster/group); this edge's object
    is a backend pool, a different axis, so overloading MEMBER_OF would
    pollute host-topology queries. The FROM endpoint is the backend host's
    own Resource row, resolved by matching LbPoolMember.address against a
    known host Resource's IP/name (best-effort — not always resolvable, so
    confidence < 1.0 unless the match was an exact IP hit).
    """

    address: str = ""
    port: int = 0
    state: str = ""


@dataclass
class TerminatesTlsForProps:
    """lb_instance TERMINATES_TLS_FOR certificate.

    Emitter: LoadBalancerAgent._write_graph_edges (GitLab issue #100).
    Dovetails with the PKI/HAS_CERTIFICATE convergence node
    (HasCertificateProps) — the TO endpoint is the SAME shared
    security/certificate node keyed by thumbprint, so an LB's TLS
    termination converges on the same certificate identity a host's own
    HAS_CERTIFICATE edge points at. Emitted only when
    LbVirtualServer.tls_certificate_thumbprint is set.
    """

    virtual_server: str = ""


@dataclass
class IsPrincipalForProps:
    """identity_principal IS_PRINCIPAL_FOR user_account convergence node.

    Emitter: agents/identity.py. Matched by normalized login/email equality
    between the Okta principal's profile and an existing per-system account
    convergence node — a deterministic-match join, so ``external_id`` (the
    Okta principal id) is carried in properties for traceability, not as a
    join key.
    """

    external_id: str = ""


@dataclass
class PulledFromProps:
    """container_image PULLED_FROM registry.  Emitter: agents/container_registry.py.

    Both endpoints are Resource rows the collector itself created/resolved
    in the same run (the image from its own catalog listing, the registry
    from ``upsert_resource``), so this is a declared, not inferred, edge.
    """


@dataclass
class HasVulnerabilityScanProps:
    """container_image HAS_VULNERABILITY_SCAN container_scan.  Emitter: agents/container_registry.py.

    ``to`` endpoint is a synthetic ``container_scan`` Resource summarizing
    the OCI Referrers API result for that image (signature/SBOM/scan
    attestation counts); emitted only when the registry's Referrers API is
    supported and returned at least one artifact.
    """


@dataclass
class UsesSaasAppProps:
    """team/project USES_SAAS_APP saas_application (GitLab #103).

    Emitter: agents/saas_inventory.py ``_emit_saas_edges``. Best-effort:
    resolved from ``SaaSApplication.owner_team`` matched against an
    existing team/project Resource by exact name — a deterministic-match
    edge, not a declared one, since the join key is a display name.
    """

    owner_team: str = ""


# Registry: RelationshipType → property dataclass class (for CI guard + docs).
RELATIONSHIP_PROPS: dict[RelationshipType, type] = {
    RelationshipType.RUNS_ON: RunsOnProps,
    RelationshipType.MEMBER_OF: MemberOfProps,
    RelationshipType.MANAGES: ManagesProps,
    RelationshipType.HOSTED_BY: HostedByProps,
    RelationshipType.IN_DATACENTER: InDatacenterProps,
    RelationshipType.STORED_ON: StoredOnProps,
    RelationshipType.ATTACHED_TO: AttachedToProps,
    RelationshipType.HAS_DISK: HasDiskProps,
    RelationshipType.HAS_NIC: HasNicProps,
    RelationshipType.HAS_HBA: HasHbaProps,
    RelationshipType.HAS_PHYSICAL_DISK: HasPhysicalDiskProps,
    RelationshipType.DEPLOYED_TO: DeployedToProps,
    RelationshipType.DEPLOYS_TO: DeploysToProps,
    RelationshipType.TRIGGERED_BY: TriggeredByProps,
    RelationshipType.DEPENDS_ON: DependsOnProps,
    RelationshipType.VULNERABLE_TO: VulnerableToProps,
    RelationshipType.PATCHED_BY: PatchedByProps,
    RelationshipType.HAS_SOFTWARE: HasSoftwareProps,
    RelationshipType.TAGGED_AS: TaggedAsProps,
    RelationshipType.BELONGS_TO: BelongsToProps,
    RelationshipType.DEFINED_IN: DefinedInProps,
    RelationshipType.PART_OF: PartOfProps,
    RelationshipType.HAS_VIOLATION: HasViolationProps,
    RelationshipType.RELATED_TO: RelatedToProps,
    RelationshipType.IS_SAME_AS: IsSameAsProps,
    RelationshipType.ANSIBLE_MANAGES: AnsibleManagesProps,
    RelationshipType.HAS_DRIFT: HasDriftProps,
    RelationshipType.RUNS_EOL: RunsEolProps,
    RelationshipType.AFFECTED_BY: AffectedByProps,
    RelationshipType.DEPLOYED_VIA: DeployedViaProps,
    RelationshipType.HAS_SCHEDULE: HasScheduleProps,
    RelationshipType.COVERS: CoversProps,
    RelationshipType.FIXES: FixesProps,
    RelationshipType.HAS_ACCOUNT: HasAccountProps,
    RelationshipType.IN_SUBNET: InSubnetProps,
    RelationshipType.RUNS_OS: RunsOsProps,
    RelationshipType.INSTANCE_OF: InstanceOfProps,
    RelationshipType.ON_FIELD: OnFieldProps,
    RelationshipType.HAS_ROLE: HasRoleProps,
    RelationshipType.GRANTED_ON: GrantedOnProps,
    RelationshipType.RAISED_ON: RaisedOnProps,
    RelationshipType.ASSIGNED_TO: AssignedToProps,
    RelationshipType.MADE_BY: MadeByProps,
    RelationshipType.RUNS_TOOLS_VERSION: RunsToolsVersionProps,
    RelationshipType.RUNS_TENTACLE_VERSION: RunsTentacleVersionProps,
    RelationshipType.RUNS_IMAGE: RunsImageProps,
    RelationshipType.RUNS_ESXI_BUILD: RunsEsxiBuildProps,
    RelationshipType.HAS_HARDWARE_MODEL: HasHardwareModelProps,
    RelationshipType.HAS_CPU_MODEL: HasCpuModelProps,
    RelationshipType.HAS_HARDWARE_VENDOR: HasHardwareVendorProps,
    RelationshipType.HAS_BIOS: HasBiosProps,
    RelationshipType.SENDS_SYSLOG_TO: SendsSyslogToProps,
    RelationshipType.SYNCS_TIME_WITH: SyncsTimeWithProps,
    RelationshipType.BACKED_BY_FILER: BackedByFilerProps,
    RelationshipType.HAS_SNAPSHOT: HasSnapshotProps,
    RelationshipType.HAS_VARIABLE: HasVariableProps,
    RelationshipType.HAS_ACCESS: HasAccessProps,
    RelationshipType.IN_POOL: InPoolProps,
    RelationshipType.IN_SITE: InSiteProps,
    RelationshipType.EXPOSES_PORT: ExposesPortProps,
    RelationshipType.HAS_CERTIFICATE: HasCertificateProps,
    RelationshipType.HAS_SHARE: HasShareProps,
    RelationshipType.IN_LOCAL_GROUP: InLocalGroupProps,
    RelationshipType.IN_VLAN: InVlanProps,
    RelationshipType.HAS_VENDOR: HasVendorProps,
    RelationshipType.HAS_FIREWALL_RULE: HasFirewallRuleProps,
    RelationshipType.HAS_SECURITY_POSTURE: HasSecurityPostureProps,
    RelationshipType.TRIGGERED_DEPLOYMENT: TriggeredDeploymentProps,
    RelationshipType.PROVISIONS: ProvisionsProps,
    RelationshipType.OWNS: OwnsProps,
    RelationshipType.RUNS_SERVICE: RunsServiceProps,
    RelationshipType.HAS_CRON: HasCronProps,
    RelationshipType.HAS_MOUNT: HasMountProps,
    RelationshipType.HAS_PENDING_UPDATE: HasPendingUpdateProps,
    RelationshipType.ISSUED_BY: IssuedByProps,
    RelationshipType.CHAINS_TO: ChainsToProps,
    RelationshipType.HAS_CRL: HasCrlProps,
    RelationshipType.HAS_OCSP_RESPONDER: HasOcspResponderProps,
    RelationshipType.ROUTES_TO: RoutesToProps,
    RelationshipType.MEMBER_OF_POOL: MemberOfPoolProps,
    RelationshipType.TERMINATES_TLS_FOR: TerminatesTlsForProps,
    RelationshipType.USES_SAAS_APP: UsesSaasAppProps,
    RelationshipType.IS_PRINCIPAL_FOR: IsPrincipalForProps,
    RelationshipType.PULLED_FROM: PulledFromProps,
    RelationshipType.HAS_VULNERABILITY_SCAN: HasVulnerabilityScanProps,
}

# Explicitly documented deferrals: these enum members have no live emitter
# because their prerequisite data source or schema work is deferred to a
# future phase.  The declared-vs-emitted CI test accepts these as intentional
# gaps rather than bugs.
#
# NOT in this set (have live emitters despite being "aspirational" edge types):
#   MANAGES      — octopus.py emits octopus_project MANAGES octopus_machine
#   TRIGGERED_BY — iac.py + rootcause.py emit ci_pipeline TRIGGERED_BY project
#   DEPENDS_ON   — iac.py emits compose_service DEPENDS_ON compose_service
#
DEFERRED_RELATIONSHIP_TYPES: set[RelationshipType] = {
    RelationshipType.HOSTED_BY,  # no container/pod collector; requires new domain
    RelationshipType.PART_OF,  # host -> inventory_group, the IDENTICAL fact
    # MEMBER_OF now carries. The grammar gap that used to block it (a NodeSpec
    # identified from child rows could not carry a member list) is CLOSED —
    # TRK-359's junction grammar (``NodeSpec.row_gathers``) is exactly what a
    # PART_OF declaration would use — but declaring it is now REFUSED for a
    # different reason: iac already declares the fact as MEMBER_OF (see
    # MIGRATED_TO_GRAPH_EDGES below), and one fact stored under two labels is a
    # contradiction waiting to drift. Deferred means "declarable the day the
    # taxonomy decides PART_OF should be the label instead of MEMBER_OF" — a
    # rename decision, not missing machinery.
}

# Types that have MOVED OFF this store: no agent derives them into
# resource_relationships any more, because they are declared on a collector's
# ``AgentSpec.emits_edges`` and materialised into ``graph_edges`` by
# ``graph_engine`` (P2 of docs/decisions/2026-08-11-graph-first-architecture.md).
#
# Deliberately NOT folded into DEFERRED_RELATIONSHIP_TYPES, which means "nothing
# emits this and the prerequisite work is pending". These ARE emitted — just not
# here. Collapsing the two would turn a completed migration into something
# indistinguishable from an unfinished one, and the next reader would either
# re-add a deriver or delete a live relationship.
#
# The enum member and its props class stay for the historical rows still sitting
# in the table; P3/P4 of that design doc is what removes those. A type belongs
# here only once its LAST resource_relationships emitter is gone.
MIGRATED_TO_GRAPH_EDGES: set[RelationshipType] = {
    # iac AgentSpec, EdgeDirection.INVERSE (project -> file).
    RelationshipType.DEFINED_IN,
    # iac AgentSpec, NodeSpec.gathers + EdgeSpec.from_key_multi (compose file ->
    # ContainerImage). iac also declares the ContainerImage node the edge points
    # at — graph_maintenance used to mint those container_image Resource rows
    # inline, so deleting its block without an owner would have left the target
    # produced by nothing.
    RelationshipType.RUNS_IMAGE,
    # iac AgentSpec, RE-ANCHORED (TRK-354 Option A): the declared edge runs
    # AnsibleInventoryFile -> LinuxHost, not the gitlab_project -> host the
    # deleted deriver stored. The type name is deliberately unchanged (the
    # anchor moved, the meaning did not), so the 14 historical rows below stay
    # comparable and every blast-radius query naming the type keeps working.
    # Both halves of the old derivation are gone: the inventory half is
    # re-anchored, and the playbook-plays half is dropped because
    # ansible_playbook_plays.hosts is a JSON-list column ChildSpec refuses --
    # it reached no host the inventory half misses (measured, see
    # tests/agents/test_iac_ansible_manages_graph.py).
    RelationshipType.ANSIBLE_MANAGES,
    # pki AgentSpec. Same anchor, same key, same 0.900 as the deleted deriver —
    # the ownership question ANSIBLE_MANAGES had never arose here, because pki
    # writes BOTH endpoints' resources rows itself. The certificate rows the
    # declared node reads are still written by
    # ``PKIAgent._upsert_certificate_resources``; only the edge half moved.
    RelationshipType.ISSUED_BY,
    # octopus AgentSpec (variable -> project). Joined at P5: octopus.py's
    # ``_emit_graph_edges`` — its last legacy writer — was deleted with the
    # store, and the declaration (already live and P4-backfilled; it was in the
    # late ``GRAPH_SERVED_EDGE_TYPES`` dedup set) is the sole producer now.
    RelationshipType.BELONGS_TO,
    # iac AgentSpec (TRK-359) — the first JUNCTION declaration, and the first
    # of P5's two accepted losses restored. The AnsibleInventoryGroup node is
    # materialised from ``ansible_inventory_groups`` child rows and carries its
    # member hosts via ``NodeSpec.row_gathers`` (grouped by GROUP NAME, so one
    # group in two inventory files manages the union); the edge is stored
    # host -> group via from_key_multi + INVERSE. Matched through the ``host``
    # normaliser at 0.900 where the deleted deriver claimed 1.0 on a bare
    # ``.lower()`` — folds more, claims less.
    RelationshipType.MEMBER_OF,
    # eol AgentSpec (TRK-359) — the second junction declaration, anchor-gather
    # shape: ``eol_registry.resource_id`` IS the representative host, so the
    # EolProduct node gathers its anchors' names (``RowGather(path=())``) and
    # the edge fans host -> product out of that list. Registry rows anchored on
    # minted eol/product resources (no known host) produce no node — the same
    # rows the deleted deriver's self-loop guard skipped.
    RelationshipType.RUNS_EOL,
}

# Types whose DERIVATION was RETIRED rather than migrated: nothing emits them
# any more and nothing is meant to. These are the containment types of §3.1 of
# docs/decisions/2026-08-11-graph-first-architecture.md — "the graph holds
# entities and the relationships between them; facts ABOUT an entity stay as
# attributes or time-series attached to it". A host's ports, crons, mounts,
# packages, accounts, certificates, firewall rules, pending updates, its
# hardware's model/CPU/BIOS, a VM's snapshots and disks, a drift event, a
# compliance violation: each is a fact about ONE entity, and each stays fully
# queryable in the detail table the collector already writes it to. The P3
# migration (alembic/versions/8965b6329b94_*) enumerated exactly this set as
# CONTAINMENT_TYPES and refused to backfill it into graph_edges for the same
# reason; that frozenset is the authority and this set mirrors it.
#
# FOUR SEPARATE SETS, DELIBERATELY NOT MERGED — each answers a different
# question and collapsing any two would mislead the next reader:
#   DEFERRED_RELATIONSHIP_TYPES   "nothing emits this AND the prerequisite work
#                                  is pending"  -> someone should finish it
#   MIGRATED_TO_GRAPH_EDGES       "still emitted, just not into THIS store"
#                                  -> do not re-add a deriver
#   RETIRED_CONTAINMENT_DERIVATIONS
#                                 "deliberately no longer derived anywhere"
#                                  -> do not re-add a deriver, and do not go
#                                     looking for a declaration either
#   RETIRED_WITH_LEGACY_STORE     "a genuine relationship whose only writers
#                                  died with the P5 table drop"
#                                  -> declarable the day someone needs it, as a
#                                     collector declaration, never a deriver
#
# The enum members and their props classes STAY: historical rows are still in
# resource_relationships (2,661 of the 2,865 live rows are these types) and P4/P5
# of the design doc is what removes those. Retiring the enum member now would
# make the surviving rows unreadable.
RETIRED_CONTAINMENT_DERIVATIONS: set[RelationshipType] = {
    # --- the 70%: drift_events decomposed into edges ---
    RelationshipType.HAS_DRIFT,  # -> drift_events (resource_id)
    RelationshipType.ON_FIELD,  # -> drift_events.field
    # --- host OS/config state ---
    RelationshipType.HAS_ACCOUNT,  # -> r7_asset_users/windows_local_users/linux_users
    RelationshipType.EXPOSES_PORT,  # -> linux_ports
    RelationshipType.HAS_CRON,  # -> linux_crons
    RelationshipType.HAS_MOUNT,  # -> linux_mounts
    RelationshipType.HAS_PENDING_UPDATE,  # -> linux_pending_updates
    RelationshipType.HAS_CERTIFICATE,  # -> host_certificates
    RelationshipType.HAS_SHARE,  # -> host_shares
    RelationshipType.HAS_FIREWALL_RULE,  # -> host_firewall_rules
    RelationshipType.HAS_SECURITY_POSTURE,  # -> host_security_posture
    RelationshipType.HAS_VENDOR,  # -> linux_nics.mac / net_discovery_hosts.mac_vendor
    # --- software inventory ---
    RelationshipType.HAS_SOFTWARE,  # -> windows_software/r7_software/linux_packages
    RelationshipType.TAGGED_AS,  # -> r7_tags
    # --- compliance ---
    RelationshipType.HAS_VIOLATION,  # -> compliance_violations (resource_id)
    # --- octopus / cicd ---
    # HAS_ROLE is deliberately NOT here — see the note below. One of its two
    # derivations was containment and is deleted; the other is genuine and lives.
    RelationshipType.HAS_VARIABLE,  # -> octopus_variables
    RelationshipType.HAS_ACCESS,  # -> octopus_teams.project_ids
    RelationshipType.HAS_SCHEDULE,  # -> ci_schedules
    # --- vSphere hardware + VM components ---
    RelationshipType.HAS_HARDWARE_MODEL,  # -> vsphere_hosts.model
    RelationshipType.HAS_CPU_MODEL,  # -> vsphere_hosts.cpu_model
    RelationshipType.HAS_HARDWARE_VENDOR,  # -> vsphere_hosts.vendor
    RelationshipType.HAS_BIOS,  # -> vsphere_hosts.bios_version
    RelationshipType.HAS_SNAPSHOT,  # -> vsphere_snapshots
    # --- container registry (P5) ---
    RelationshipType.HAS_VULNERABILITY_SCAN,  # -> container_images.scan_result_summary
    # (the edge's target was a ``container_scan`` anchor whose metadata was a
    # byte-copy of that column — pure restatement, classified containment by
    # the P3 migration's CONTAINMENT_TYPES and deleted with its emitter)
    RelationshipType.HAS_DISK,  # -> vsphere_vm_disks / vsphere_hosts.details
    RelationshipType.HAS_NIC,  # -> vsphere_hosts.details["nics"]
    RelationshipType.HAS_HBA,  # -> vsphere_hosts.details["hbas"]
    RelationshipType.HAS_PHYSICAL_DISK,  # -> vsphere_hosts.details["physical_disks"]
    # --- GENUINE types deleted because their ONLY input was containment ---
    # These are NOT containment refusals. Each connects two independently-
    # referrable entities, so each would pass §3.1 on its own. They are here
    # because every one of them read a node or an edge that only a containment
    # derivation ever produced, so retiring that derivation left them with
    # nothing to stand on. Re-establishing any of them means a COLLECTOR
    # DECLARATION over the source tables, never a re-derivation over this store.
    RelationshipType.AFFECTED_BY,  # software@version node came from HAS_SOFTWARE
    RelationshipType.INSTANCE_OF,  # FROM node was HAS_VIOLATION's shadow resource
    RelationshipType.RELATED_TO,  # BOTH ends were HAS_VIOLATION/HAS_DRIFT shadows
}

# Genuine relationships whose ONLY writers died with the P5 drop of
# ``resource_relationships``. NOT containment refusals — every member connects
# two independently-referrable entities and would pass §3.1 on its own (the P3
# migration's CONTAINMENT_TYPES frozenset is the recorded authority for that
# split, and none of these is in it). They are absent because their last
# emitter was a hand-written deriver into the dropped store, and no collector
# declaration exists (yet) to replace it. Three sub-groups, with the reason
# each has no declaration:
#
#   * RETIRED-DOMAIN types (vSphere / k8s / Rapid7 / Octopus / SaaS /
#     identity): the whole domain is quarantined (``AgentSpec.retired``) on
#     this estate, so the deriver produced zero live rows and a declaration
#     would have no oracle. If a domain is ever revived
#     (``COLLECTION_REVIVED_DOMAINS``), declaring its edges is part of the
#     documented revival obligation in ``etl/spec.py``.
#   * ARTIFACT-TARGET types (COVERS, FIXES, MADE_BY, IN_SUBNET, RUNS_OS,
#     RUNS_SERVICE, IN_VLAN, IN_SITE, IN_LOCAL_GROUP, PULLED_FROM, …): the
#     edge's target was a convergence Resource that ``graph_maintenance``
#     minted and nothing else ever read (proved by grep before deletion — see
#     tests/agents/test_graph_maintenance_legacy_writers_gone.py). The
#     underlying fact lives in the collector's own detail tables.
#   * DECLARABLE-TODAY types (DEPLOYED_VIA, TRIGGERED_DEPLOYMENT, PROVISIONS,
#     DEPENDS_ON): both endpoints are ordinary inventory rows over live or
#     configured domains. Flagged in the P5 integration as an explicit
#     integrator call: nothing blocks a declaration except nobody needing one.
#
# Unlike RETIRED_CONTAINMENT_DERIVATIONS ("do not go looking for a
# declaration"), the correct revival for any member here IS a declaration on
# the owning collector's ``AgentSpec.emits_edges``. Never a deriver — the
# store derivers wrote to is gone.
RETIRED_WITH_LEGACY_STORE: set[RelationshipType] = {
    # --- retired-domain: vSphere ---
    RelationshipType.RUNS_ON,  # vsphere_vm -> vsphere_host
    RelationshipType.IN_DATACENTER,  # vsphere_cluster -> vsphere_datacenter
    RelationshipType.STORED_ON,  # vsphere_vm -> vsphere_datastore
    RelationshipType.ATTACHED_TO,  # vsphere_vm -> vsphere_network
    RelationshipType.GRANTED_ON,  # vsphere permission principal -> entity
    RelationshipType.RAISED_ON,  # vsphere_alarm -> entity
    RelationshipType.ASSIGNED_TO,  # vsphere_license -> entity
    RelationshipType.RUNS_TOOLS_VERSION,  # vsphere_vm -> vmware_tools_version
    RelationshipType.RUNS_ESXI_BUILD,  # vsphere_host -> esxi_build
    RelationshipType.SENDS_SYSLOG_TO,  # vsphere_host -> syslog_target
    RelationshipType.SYNCS_TIME_WITH,  # vsphere_host -> ntp_server
    RelationshipType.BACKED_BY_FILER,  # vsphere_datastore -> nfs_filer
    RelationshipType.IN_POOL,  # vsphere_vm -> vsphere_resource_pool
    RelationshipType.HAS_ROLE,  # see the NOTE below — the split-verdict type
    # --- retired-domain: k8s / Rapid7 / Octopus / SaaS / identity ---
    RelationshipType.OWNS,  # k8s deployment -> pod
    RelationshipType.VULNERABLE_TO,  # r7_asset -> cve (vuln.py + graph_maintenance,
    # both deleted; Rapid7 was never deployed here — zero live rows ever)
    RelationshipType.PATCHED_BY,  # r7_asset -> r7_solution (same file, same reason)
    RelationshipType.IN_SITE,  # r7_asset -> rapid7/site
    RelationshipType.MANAGES,  # octopus_project -> octopus_machine
    RelationshipType.DEPLOYED_TO,  # octopus_project -> octopus_environment
    RelationshipType.DEPLOYS_TO,  # octopus_machine -> octopus_environment
    RelationshipType.RUNS_TENTACLE_VERSION,  # octopus_machine -> tentacle_version
    RelationshipType.USES_SAAS_APP,  # host -> saas_app (saas_inventory retired)
    RelationshipType.IS_PRINCIPAL_FOR,  # user_account -> service (identity retired;
    # its ``identity/user_account`` convergence nodes had exactly one reader —
    # this emitter — so both halves died together)
    # --- artifact-target: the convergence-node edges ---
    RelationshipType.COVERS,  # r7_vulnerability -> vuln/cve
    RelationshipType.FIXES,  # r7_solution -> vuln/cve
    RelationshipType.MADE_BY,  # software_title -> software/vendor
    RelationshipType.IN_SUBNET,  # host -> network/subnet
    RelationshipType.RUNS_OS,  # host -> os/os_version
    RelationshipType.RUNS_SERVICE,  # host -> os/service
    RelationshipType.IN_VLAN,  # host -> network/vlan
    RelationshipType.IN_LOCAL_GROUP,  # user_account -> security/local_group
    RelationshipType.PULLED_FROM,  # container_image -> registry (fact lives on
    # container_images.registry; the registry Resource itself is KEPT)
    # --- retired-in-effect collectors: lb_enabled=False; pki chains empty ---
    RelationshipType.ROUTES_TO,  # lb_virtual_server -> lb_pool
    RelationshipType.MEMBER_OF_POOL,  # host -> lb_pool
    RelationshipType.TERMINATES_TLS_FOR,  # lb_instance -> certificate
    RelationshipType.CHAINS_TO,  # intermediate_ca -> parent CA (the reinstating
    # EdgeSpec is written out in agents/pki.py's epitaph)
    RelationshipType.HAS_CRL,  # certificate_authority -> crl_responder
    RelationshipType.HAS_OCSP_RESPONDER,  # certificate_authority -> ocsp_responder
    # --- declarable today, nobody has needed it ---
    RelationshipType.DEPLOYED_VIA,  # gitlab_project -> octopus_project
    RelationshipType.TRIGGERED_DEPLOYMENT,  # ci_pipeline_run -> octopus_deployment
    RelationshipType.PROVISIONS,  # iac_file -> the resource it produced
    RelationshipType.DEPENDS_ON,  # compose_service -> compose_service (the
    # deleted iac emitter resolved both ends to the same compose-file Resource —
    # a self-loop the upsert made a no-op — so nothing was ever actually stored;
    # a real declaration needs per-service nodes first)
    RelationshipType.TRIGGERED_BY,  # octopus_deployment -> ci_pipeline (iac +
    # rootcause emitters deleted; the iac one pointed at a pipeline Resource
    # that was never created — a row that by construction FK-failed)
    # --- superseded in the graph store ---
    RelationshipType.IS_SAME_AS,  # host identity. NOT lost and NOT declarable:
    # the claim lives on as ``SAME_AS`` in ``graph_edges``, written solely by
    # ``graph_phase3.resolve_entities`` under the authority model (method,
    # evidence, bitemporal validity, human veto). host_reconcile's two legacy
    # emitters were deleted in the P5 resolver switch. Listed here rather than
    # in MIGRATED_TO_GRAPH_EDGES because that set means "a collector AgentSpec
    # declares it", and identity is resolver-written, not collector-declared.
}

# NOTE on HAS_ROLE — the type whose verdict SPLIT, now fully resolved by P5.
# Three things called "HAS_ROLE" existed:
#
#   1. octopus_machine -> octopus/role   RETIRED in rev10/T3 as containment: a
#      deployment role is a tag in ``octopus_machines.roles`` (a JSONB list on
#      the machine's own row) and the edge restated that column. This is the
#      derivation the P3 migration's CONTAINMENT_TYPES classified.
#   2. user_account -> vsphere/role      DELETED in P5 with the legacy store: a
#      vCenter RBAC role is an independently-referrable object, so this half
#      was genuine — but it read identity/vsphere convergence nodes over a
#      retired domain and its emitter died in the graph_maintenance surgery.
#      This is why HAS_ROLE sits in RETIRED_WITH_LEGACY_STORE above rather
#      than in the containment set: what killed it was the store and the
#      domain, not §3.1.
#   3. host -> Role (``graph_edges``)    LIVE, different store, untouched.
#      Written by ``graph_role_tagging`` with method="inferred_from_*".
#
# An earlier revision of this NOTE argued HAS_ROLE could sit in no set because
# derivation (2) still emitted. P5 deleted (2); the reasoning is obsolete and
# the type is now placed, on the record, in the set that matches how it died.


# ---------------------------------------------------------------------------
# The dropped table (P5) — reader shim only
# ---------------------------------------------------------------------------
#
# ``resource_relationships`` NO LONGER EXISTS. Phase P5 of
# ``docs/decisions/2026-08-11-graph-first-architecture.md`` dropped it (see the
# migration whose docstring carries the full derivability audit). The ORM model
# and the two write helpers (``emit_edge`` / ``emit_edges_batch``) that lived
# here are gone with it: a writer against a dropped table is not a degraded
# writer, it is a crash, and keeping a no-op stub would have turned every
# retired derivation into a silent success.
#
# WHAT SURVIVES HERE, AND WHY IT IS NOT THE MODEL
#
# ``LEGACY_RESOURCE_RELATIONSHIPS`` is a plain SQLAlchemy ``Table`` on a
# PRIVATE ``MetaData()`` — deliberately NOT ``Base.metadata``. That single
# difference is the whole point:
#
#   * ``alembic check`` / ``db/schema_check.py`` compare ``Base.metadata``
#     against the live database. A table absent from ``Base.metadata`` and
#     absent from the database is not drift; a model still registered on
#     ``Base`` would report the drop as drift forever.
#   * ``Base.metadata.create_all()`` (the sqlite test bootstrap) will not
#     re-create it, so no test can accidentally keep exercising a store that
#     production no longer has.
#   * It still answers ``.c.<column>`` and supports SQL construction, which is
#     all the two remaining LEGACY READERS need in order to keep importing and
#     compiling on this branch.
#
# The readers in question — ``graph_api.py``'s legacy-store routes and
# ``graph_phase3.py``'s legacy walk — are owned by a SIBLING branch in this
# same wave, which retires them. This shim exists ONLY so those files still
# import while the three branches are being folded together; once that branch
# lands, this block and ``get_neighborhood`` below should go with it. Executing
# either against a P5-migrated database raises ``UndefinedTable``, which is the
# correct and loud outcome — the store is gone, not empty.

_LEGACY_METADATA = MetaData()

#: The ``resource_relationships`` schema EXACTLY as it stood at the commit that
#: dropped it. Frozen here so ``downgrade()`` in the P5 migration and the two
#: legacy readers agree on one definition of the shape that used to exist.
LEGACY_RESOURCE_RELATIONSHIPS = Table(
    "resource_relationships",
    _LEGACY_METADATA,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("from_resource_id", PG_UUID(as_uuid=True), nullable=False),
    Column("to_resource_id", PG_UUID(as_uuid=True), nullable=False),
    Column("relationship_type", String(64), nullable=False),
    Column("properties", JSONB, nullable=False, server_default="{}"),
    Column("confidence", Float, nullable=False, server_default="1.0"),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("source", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("last_seen", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint(
        "from_resource_id",
        "to_resource_id",
        "relationship_type",
        name="uq_resource_relationships",
    ),
    Index("ix_rr_from", "from_resource_id"),
    Index("ix_rr_to", "to_resource_id"),
    Index("ix_rr_type", "relationship_type"),
    Index("ix_rr_from_type", "from_resource_id", "relationship_type"),
    # The two ``ON DELETE CASCADE`` foreign keys to ``resources.id`` are the one
    # part of the original NOT reproduced: ``resources`` lives in
    # ``Base.metadata`` and this Table lives in a private ``MetaData``, so a
    # ForeignKey here would either dangle or force the two metadatas together —
    # and joining them is exactly what putting the table back on ``Base`` would
    # do. ``uq_resource_relationships`` is kept because it is the constraint
    # that DEFINED this store's central limitation (one row per (from, to,
    # type), forever, hence no history), which is the thing a reader of this
    # shim most needs to see. The P5 migration's ``downgrade()`` carries the
    # full DDL including both FKs.
)


class ResourceRelationship:
    """Import shim for the dropped ``resource_relationships`` ORM model.

    NOT a ``Base`` subclass and not mapped — see the block comment above. It
    exposes ``__table__`` because that is the only attribute the surviving
    legacy readers touch (``graph_phase3._walk_legacy_store`` does
    ``rel = ResourceRelationship.__table__``). Instantiating it, querying it
    through a ``Session``, or expecting ``create_all`` to build its table will
    all fail — correctly, because the table it named no longer exists.
    """

    __table__ = LEGACY_RESOURCE_RELATIONSHIPS
    __tablename__ = "resource_relationships"

    def __init__(self, *_args, **_kwargs):
        raise RuntimeError(
            "resource_relationships was dropped in P5 "
            "(docs/decisions/2026-08-11-graph-first-architecture.md). "
            "Write graph_edges via graph_engine instead of instantiating this shim."
        )


# ---------------------------------------------------------------------------
# The deleted write helpers — a LOUD stub, deliberately not a quiet one
# ---------------------------------------------------------------------------
#
# ``emit_edge`` and ``emit_edges_batch`` used to upsert into
# ``resource_relationships``. The table is gone, so the honest implementation of
# both is "there is nowhere to put this".
#
# THE ONE DECISION WORTH RECORDING: this raises, it does not return silently.
# A no-op stub would have made every remaining caller LOOK healthy while
# writing nothing — a collector reporting a successful pass having derived
# nothing at all is precisely the failure mode this codebase has been burned by
# before (a swallowed error shown as success). Raising means an un-migrated
# caller announces itself on its first pass instead of on the day someone
# notices an empty graph.
#
# The remaining callers are the ones this wave's SIBLING branches own —
# ``agents/host_reconcile.py``'s two ``IS_SAME_AS`` emitters most of all, which
# a sibling is re-pointing at ``graph_edges``. They keep importing and
# compiling against this stub while the branches are folded; the stub itself
# should be deleted, not kept, once the last caller is gone.


def _store_is_gone(_helper: str):
    raise NotImplementedError(
        f"{_helper}() wrote to resource_relationships, which P5 dropped "
        "(docs/decisions/2026-08-11-graph-first-architecture.md). Declare the "
        "edge on the collector's AgentSpec.emits_edges so graph_engine "
        "materialises it into graph_edges instead."
    )


def emit_edge(*_args, **_kwargs) -> None:
    """Removed with the store it wrote to. See the block comment above."""
    _store_is_gone("emit_edge")


def emit_edges_batch(*_args, **_kwargs) -> None:
    """Removed with the store it wrote to. See the block comment above."""
    _store_is_gone("emit_edges_batch")


# ---------------------------------------------------------------------------
# Graph traversal — REMOVED (graph-first P5)
# ---------------------------------------------------------------------------
#
# ``get_neighborhood`` and its recursive CTE (``_WALK_SQL``/``_RESOURCES_SQL``)
# lived here. They walked ``resource_relationships``, the store P5 drops, and
# they were this module's ONLY read path — everything above is the write side
# (the type vocabulary, the props contracts, ``emit_edge``/``emit_edges_batch``)
# and the ORM model.
#
# Their replacement is ``infra_brain.graph_kg.walk`` / ``.neighborhood`` over
# ``graph_nodes``/``graph_edges``. That is not a like-for-like port and should
# not be read as one:
#
#   * different id space — ``graph_nodes.id``, not ``resources.id``
#     (``graph_nodes.resource_id`` is the bridge);
#   * iterative BFS, not a recursive CTE. The CTE was PostgreSQL-only
#     (``ANY()``), so every test that touched it stubbed it out and its
#     traversal was never actually exercised anywhere. The replacement runs
#     identically on SQLite and PostgreSQL, so the behaviour under test is the
#     behaviour in production;
#   * no containment edges. ``STORED_ON``/``HAS_SOFTWARE``/``VULNERABLE_TO``
#     and the rest of the ~70-type vocabulary above were derived FACTS from
#     detail tables, not declared relationships, and the declarative contract
#     does not manufacture entities for them. Consumers that need them read the
#     detail tables directly.
#
# Nothing new should be added below this line: this module is write-side and
# schema only.
