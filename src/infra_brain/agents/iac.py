"""
IaC Agent — reads Ansible playbooks, inventories, CI pipelines, Docker Compose,
Kubernetes manifests, and Terraform/OpenTofu files from GitLab repositories.
Read-only on all repos. Stores typed file metadata for drift comparison.
"""

import logging
import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import yaml

from infra_brain.config import get_settings
from infra_brain.pool_metrics import observe_pool
from infra_brain.db.models import (
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    AnsiblePlaybookPlay,
    CiSchedule,
    ComposeService,
    DriftEvent,
    GitlabProject,
    IacFile,
    K8sManifestResource,
    Resource,
    TerraformResource,
)
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectionResult, CollectOutcome, ETLConnector, ReconcileScope
from infra_brain.etl.spec import (
    AgentSpec,
    ChildSpec,
    EdgeDirection,
    EdgeSpec,
    NodeSpec,
    RowGather,
    Tier,
)
from infra_brain.tools.gitlab import gitlab_get_paginated
from infra_brain.tools.iac_reader import (
    gitlab_file_tool,
    gitlab_repository_tree_tool,
    parse_ansible_inventory_content_tool,
    parse_ansible_playbook_plays_tool,
    parse_compose_services_tool,
    parse_k8s_resources_tool,
    parse_terraform_resources_tool,
)

logger = logging.getLogger(__name__)

# Map the collect()-emitted generic resource ``type`` to the normalized
# ``iac_files.file_type`` column value (model: gitlab_ci | playbook | inventory |
# requirements | compose | k8s_manifest | terraform).
_TYPE_TO_FILE_TYPE = {
    "gitlab_ci_pipeline": "gitlab_ci",
    "ansible_playbook": "playbook",
    "ansible_inventory_file": "inventory",
    "ansible_requirements": "requirements",
    "docker_compose": "compose",
    "k8s_manifest": "k8s_manifest",
    "terraform_file": "terraform",
}

# --- graph contribution (P2 of the graph-first architecture) ----------------
#
# ``resources.type`` -> ``graph_nodes.node_type`` for every classification
# ``_classify_yaml``/collect() can produce.
#
# ONE CONCEPT, SEVEN NODE TYPES — and that is the contract's doing, not a
# modelling choice. "An IaC file in a GitLab repo" is a single kind of entity
# with a single relationship (it belongs to its project), but
# ``NodeSpec.resource_type`` takes exactly one ``resources.type`` string, so the
# concept has to be spelled once per classification and the BELONGS_TO edge
# once per node type. Building both tuples from this one mapping keeps the
# declaration honest about that: add a classification above and it is declared
# automatically, rather than being silently absent from the graph until someone
# notices. (``tests/agents/test_iac_belongs_to_graph.py`` asserts the two stay
# in step.) The alternative — letting NodeSpec accept several resource types —
# is a contract change and deliberately not made here.
_IAC_FILE_NODE_TYPES = {
    "gitlab_ci_pipeline": "GitlabCiFile",
    "ansible_playbook": "AnsiblePlaybook",
    "ansible_inventory_file": "AnsibleInventoryFile",
    "ansible_requirements": "AnsibleRequirements",
    "docker_compose": "DockerComposeFile",
    "k8s_manifest": "K8sManifest",
    "terraform_file": "TerraformFile",
}

#: The one classification whose parsed detail rows carry a graph fact. Named
#: rather than spelled inline so the three declarations below that must agree
#: about it cannot drift apart.
_COMPOSE_RESOURCE_TYPE = "docker_compose"
_COMPOSE_FILE_NODE = _IAC_FILE_NODE_TYPES[_COMPOSE_RESOURCE_TYPE]

#: ``resources.id <- iac_files.resource_id`` then ``iac_files.id <-
#: compose_services.iac_file_id``. The images a compose file runs live at the
#: end of that descent and NOWHERE ELSE: ``_extract_yaml_metadata`` puts
#: ``services`` (the service NAMES) and ``service_count`` into the resource's
#: metadata, never the images. That is precisely the gap ``ChildSpec`` exists
#: to close, and why RUNS_IMAGE stayed hand-written through P2.
_COMPOSE_SERVICE_PATH = (("iac_files", "resource_id"), ("compose_services", "iac_file_id"))

#: The same column read two ways, because the relationship needs it at both
#: ends: gathered onto the FILE node as the list of keys it joins on, and used
#: as the IDENTITY of the image node those keys resolve to.
_COMPOSE_IMAGES = ChildSpec(key="images", path=_COMPOSE_SERVICE_PATH, column="image")
_COMPOSE_IMAGE = ChildSpec(key="image", path=_COMPOSE_SERVICE_PATH, column="image")

#: The inventory classification, named for the same reason ``_COMPOSE_RESOURCE_TYPE``
#: is: three declarations below have to agree about it.
_INVENTORY_RESOURCE_TYPE = "ansible_inventory_file"
_INVENTORY_FILE_NODE = _IAC_FILE_NODE_TYPES[_INVENTORY_RESOURCE_TYPE]

#: ``resources.id <- iac_files.resource_id``, ``iac_files.id <-
#: ansible_inventory_groups.iac_file_id``, ``ansible_inventory_groups.id <-
#: ansible_inventory_hosts.group_id``. Three hops, every one of them a strict
#: parent-to-child descent by single-column primary key — the only shape
#: ``ChildSpec`` walks. The hostnames an inventory manages live at the end of it
#: and NOWHERE ELSE: ``_extract_yaml_metadata`` writes no inventory summary at
#: all (the inventory branch of ``_write_child_rows`` is where the parse lands),
#: so this is the same reach gap ``RUNS_IMAGE`` needed ``ChildSpec`` for.
_INVENTORY_HOST_PATH = (
    ("iac_files", "resource_id"),
    ("ansible_inventory_groups", "iac_file_id"),
    ("ansible_inventory_hosts", "group_id"),
)

#: Gathered onto the inventory-file node as the (distinct, sorted) list of host
#: names it manages — the many-valued join key ``ANSIBLE_MANAGES`` fans out over.
_INVENTORY_MANAGED_HOSTS = ChildSpec(key="managed_hosts", path=_INVENTORY_HOST_PATH, column="name")

#: ``resources.type`` -> the child rows that classification gathers. Only two of
#: the seven have any; the rest carry their whole graph contribution in
#: ``metadata``. A map rather than an inline conditional so adding a third
#: cannot quietly turn into a chain of ``if``s.
_IAC_FILE_GATHERS: dict[str, tuple[ChildSpec, ...]] = {
    _COMPOSE_RESOURCE_TYPE: (_COMPOSE_IMAGES,),
    _INVENTORY_RESOURCE_TYPE: (_INVENTORY_MANAGED_HOSTS,),
}

_IAC_FILE_NODES = tuple(
    NodeSpec(
        type=node_type,
        resource_type=resource_type,
        # ``resources.name`` is "<project>/<file_path>" (see _upsert_resource) —
        # the stable natural key for a file.
        natural_key="name",
        # ``project_id`` is the join key and MUST ride onto the node: the engine
        # matches graph-side, so a value left behind in resources.metadata is
        # invisible to the EdgeSpec below. ``file_path``/``ref`` come along
        # because they are how a reader identifies the file without a second
        # lookup.
        attributes=("project_id", "file_path", "ref"),
        gathers=_IAC_FILE_GATHERS.get(resource_type, ()),
    )
    for resource_type, node_type in _IAC_FILE_NODE_TYPES.items()
)

# ``ContainerImage`` — a SHARED VALUE node, one per distinct image string.
#
# WHY IAC OWNS IT. The hand-written deriver minted ``container_image``
# ``resources`` rows itself, inside ``graph_maintenance``, which meant no
# collector owned the entity: delete the block and the nodes stop existing with
# nothing declaring they should. The architecture's answer is that the SOURCE
# ASSERTING A THING EXISTS OWNS IT, and the assertion "this image exists" is
# made by a compose file in a repo iac read. ``container_registry`` is not that
# owner even though it also writes ``container_image`` resources: it inventories
# images a REGISTRY holds, which is a different (and overlapping, not identical)
# population — an image a compose file references may never have been pushed to
# the registry this environment scans, and the graph should still record that
# the file runs it. Cross-source identity between the two is the reconciler's
# job, not a reason to give one of them the other's entity.
#
# NOT resource-backed, and it cannot be: ``nginx:1.25`` appears in six compose
# files here, so there is no single owning ``resources`` row. That sharing is
# the entire value of the node — "which files run this image" is one hop.
_CONTAINER_IMAGE_NODE = NodeSpec(
    type="ContainerImage",
    resource_type=_COMPOSE_RESOURCE_TYPE,
    natural_key="rows.image",
    name="rows.image",
    resource_backed=False,
    from_rows=_COMPOSE_IMAGE,
)

_IAC_BELONGS_TO_EDGES = tuple(
    EdgeSpec(
        type="BELONGS_TO",
        from_node=node_type,
        to_node="GitlabProject",
        from_key="attributes.project_id",
        to_key="attributes.project_id",
        # FK-strength, not a name match: GitLab's numeric project id is an
        # immutable identifier on both sides (iac reads it off the same API
        # response cicd does), so unlike a hostname join this cannot mis-resolve
        # on a rename and may honestly claim 1.000 — which is the confidence
        # the hand-written emitter this replaces used.
        method="declared",
        confidence=Decimal("1.000"),
    )
    for node_type in _IAC_FILE_NODE_TYPES.values()
)

# ``<GitlabProject> ─DEFINED_IN→ <IaC file>`` — the EXACT INVERSE of the edge
# above: same relational join (the file's ``project_id``), opposite arrow.
#
# It was left hand-written by P2 for one reason only — direction. One project
# defines MANY files, so storing the arrow in join order was impossible: the
# fan-out end has to be the edge's SOURCE, and an ``EdgeSpec`` could only store
# ``from_node → to_node``. ``EdgeDirection.INVERSE`` closes exactly that gap
# without touching the join: the engine still resolves each FILE to its ONE
# project and still refuses to guess if two projects claim a key — it just
# writes the resulting edge project→file. Hence the join keys below are
# identical to BELONGS_TO's, character for character; only ``direction`` and
# ``type`` differ. If they ever diverge, one of the two is wrong.
_IAC_DEFINED_IN_EDGES = tuple(
    EdgeSpec(
        type="DEFINED_IN",
        from_node=node_type,
        to_node="GitlabProject",
        from_key="attributes.project_id",
        to_key="attributes.project_id",
        direction=EdgeDirection.INVERSE,
        # Same FK-strength join as BELONGS_TO, so the same honest 1.000 — which
        # is also what the hand-written deriver in graph_maintenance emitted.
        method="declared",
        confidence=Decimal("1.000"),
    )
    for node_type in _IAC_FILE_NODE_TYPES.values()
)

# ``<compose file> ─RUNS_IMAGE→ <ContainerImage>``.
#
# ONE FILE RUNS MANY IMAGES AND ONE IMAGE RUNS IN MANY FILES — a genuine
# many-to-many, and it is declarable here WITHOUT the ambiguity rule moving,
# because the image node's key is its identity. ``from_key_multi`` says the
# source holds several keys; each of them still resolves through the same
# key→ONE-target index, so a contradiction is still refused rather than guessed.
# The boundary that remains unexpressible is unchanged and still pinned by
# ``tests/test_graph_engine.py::test_a_many_to_many_relationship_is_still_not_expressible``:
# a join where the TARGET side is ambiguous has no direction and no cardinality
# that rescues it.
#
# ``method="declared"`` / 1.000 — the honest reading, and what the deriver
# claimed. Both sides of this join are the SAME ``compose_services.image``
# string: the file's gathered key and the image node's identity come from one
# column, so unlike a hostname match there is no mutable display name in the
# middle that could mis-resolve.
_IAC_RUNS_IMAGE_EDGES = (
    EdgeSpec(
        type="RUNS_IMAGE",
        from_node=_COMPOSE_FILE_NODE,
        to_node="ContainerImage",
        from_key="attributes.images",
        to_key="natural_key",
        from_key_multi=True,
        method="declared",
        confidence=Decimal("1.000"),
    ),
)

# ``<AnsibleInventoryFile> ─ANSIBLE_MANAGES→ <LinuxHost>`` — the LAST relationship
# to leave the hand-written path, and the one that needed a DECISION rather than
# a mechanism.
#
# OPTION A (re-anchor), per TRK-354. The hand-written deriver anchored this edge
# on the inventory file's GitLab PROJECT — its own comment called that the "best
# available anchor" only because ``ansible_inventory_group`` had no ``resources``
# row to hang it on. That made the stored edge run cicd's entity → linux's
# entity, with iac owning only the join rows, and ``AgentSpec`` requires
# ``EdgeSpec.from_node`` to be a node the declaring spec emits. Not a mechanism
# gap: no direction, no cardinality and no normaliser moves an edge whose BOTH
# ends are foreign into a shape iac may declare, and neither owner can reach the
# join (cicd's ``GitlabProject`` reaches ``iac_files`` only through the non-PK
# business key ``gitlab_project_id``, which ``ChildSpec`` deliberately cannot
# express; linux has no path into iac's tables at all).
#
# So the anchor moves to the ``AnsibleInventoryFile`` iac DOES own — the design
# doc's own argument, exercised rather than deferred. The file is the thing that
# actually asserts "these hosts are managed": a project is a repo, and a repo may
# hold several inventories or none.
#
# DECISION PROVENANCE, recorded here because it is a product decision and not a
# refactor: chosen by the integrator under the maintainer's direction to finish
# the graph plan, per TRK-354's Option A. **Vetoable by the maintainer** — Option
# B (a junction/third-party declaration form that widens the ownership rule) is
# still the alternative, and was deliberately not taken to unblock one edge.
#
# THE TYPE NAME DOES NOT CHANGE. ``ANSIBLE_MANAGES`` is kept rather than minted
# as a new ``MANAGES``: the ANCHOR changed, the MEANING did not. Blast-radius and
# "what manages this host" queries mean the same thing, the 14 legacy rows stay
# comparable, and a rename would have made a continuity break out of what is
# actually a better answer to the same question.
#
# BECAUSE THE ANCHOR CHANGED, THE EQUIVALENCE ORACLE CANNOT APPLY. Every other
# migration here proved "same edges, byte for byte" before deleting its deriver.
# This one produces DIFFERENT edges by construction, so the discipline is
# replaced with a MAPPING claim, machine-checked in
# ``tests/agents/test_iac_ansible_manages_graph.py``: every (project → host) pair
# the old deriver produced corresponds to ≥1 (inventory file → host) pair here
# whose file ``BELONGS_TO`` that project. Nothing the old edge asserted is lost;
# it is asserted more precisely.
#
# ``from_key_multi`` — one inventory names many hosts, and each of those names
# still resolves through the UNCHANGED key→one-target index, so a hostname two
# ``LinuxHost`` nodes claimed would be refused rather than guessed.
#
# ``key_normalizer="host"`` — the same fold ``RUNS_ON`` uses, and needed for the
# same reason: this homelab spells the same machines with underscores in the
# inventory and hyphens elsewhere. Live shape today happens to agree on
# underscores (inventory ``node_a``; ``linux_host`` resource ``node_a``), so the
# fold is currently a no-op — declared anyway, because the day one side is
# rewritten with hyphens is not the day to discover the join was exact-match.
#
# ``deterministic_match`` / 0.900 — a hostname STRING matched against a
# separately-collected host entity, exactly what the deriver claimed for its
# inventory half (0.9) and exactly what the honesty rule forbids calling 1.000.
#
# WINDOWS. The deriver matched ``domain in ('linux','windows')``. Only
# ``LinuxHost`` is targeted here because it is the only host node type any spec
# declares; the live DB holds zero ``windows`` resources, so today's coverage is
# identical. A second edge to ``WindowsHost`` is a one-line addition the day
# ``agents/windows.py`` declares that node.
_IAC_ANSIBLE_MANAGES_EDGES = (
    EdgeSpec(
        type="ANSIBLE_MANAGES",
        from_node=_INVENTORY_FILE_NODE,
        to_node="LinuxHost",
        from_key=f"attributes.{_INVENTORY_MANAGED_HOSTS.key}",
        to_key="name",
        from_key_multi=True,
        key_normalizer="host",
        method="deterministic_match",
        confidence=Decimal("0.900"),
    ),
)

# ``AnsibleInventoryGroup`` + ``<LinuxHost> ─MEMBER_OF→ <AnsibleInventoryGroup>``
# — the FIRST junction declaration (TRK-359), restoring the first of P5's two
# accepted losses.
#
# THE GAP THIS NEEDED. The group is a value in ``ansible_inventory_groups``
# with no ``resources`` row of its own — a ``from_rows`` node, like
# ``ContainerImage`` — but unlike an image it has to CARRY something: the
# member hosts an edge fans out over, which live one table further down
# (``ansible_inventory_hosts``). A ``from_rows`` node may not ``gathers``
# (per-parent lists on a shared identity would mean last-writer-wins), which is
# exactly why P5 recorded MEMBER_OF as an accepted loss. ``RowGather`` closes
# it with a different grouping, not a softer rule: members are keyed by the
# GROUP NAME, so a group name appearing in two inventory files is ONE node
# managing the deterministic union of both files' hosts.
#
# The junction descent is ``_INVENTORY_GROUP_PATH`` — the same walk
# ``_INVENTORY_HOST_PATH`` takes, stopped one table early — and the gather's
# one extra hop is the table that walk would have ended at. The EDGE itself
# needed no new vocabulary at all: ``from_key_multi`` (one group, many
# members) + ``EdgeDirection.INVERSE`` (the join starts at the group, the
# stored arrow at the host: host MEMBER_OF group, matching the taxonomy's
# read-as-``from`` *verb* ``to``).
#
# ``key_normalizer="host"`` / ``deterministic_match`` / 0.900 — identical to
# ANSIBLE_MANAGES, for identical reasons: the member is a hostname STRING
# matched against a separately-collected host entity, hyphen/underscore
# spellings folded. The deleted deriver claimed 1.0 into the legacy store
# (which had no honesty gate) and matched by bare ``.lower()``; this both
# folds more (node-a == node_a) and claims less, which is the honest pairing.
#
# WINDOWS: only ``LinuxHost`` is targeted because it is the only host node
# type any spec declares (same note as ANSIBLE_MANAGES above); the live DB
# holds zero ``windows`` resources, so coverage is identical to the deleted
# deriver's ``domain in ('linux','windows')`` match.
#
# PART_OF is deliberately NOT declared alongside this: it names the identical
# fact (host → inventory group), and one fact under two labels is a
# contradiction waiting to drift. See db/relationships.py's DEFERRED entry.
_INVENTORY_GROUP_PATH = (
    ("iac_files", "resource_id"),
    ("ansible_inventory_groups", "iac_file_id"),
)
_INVENTORY_GROUP = ChildSpec(key="group", path=_INVENTORY_GROUP_PATH, column="name")
_INVENTORY_GROUP_MEMBERS = RowGather(
    key="members", path=(("ansible_inventory_hosts", "group_id"),), column="name"
)
_INVENTORY_GROUP_NODE = NodeSpec(
    type="AnsibleInventoryGroup",
    resource_type=_INVENTORY_RESOURCE_TYPE,
    natural_key="rows.group",
    name="rows.group",
    resource_backed=False,
    from_rows=_INVENTORY_GROUP,
    row_gathers=(_INVENTORY_GROUP_MEMBERS,),
)
_IAC_MEMBER_OF_EDGES = (
    EdgeSpec(
        type="MEMBER_OF",
        from_node="AnsibleInventoryGroup",
        to_node="LinuxHost",
        from_key=f"attributes.{_INVENTORY_GROUP_MEMBERS.key}",
        to_key="name",
        from_key_multi=True,
        direction=EdgeDirection.INVERSE,
        key_normalizer="host",
        method="deterministic_match",
        confidence=Decimal("0.900"),
    ),
)

# TRK-311: bounded worker count for per-file GitLab content fetches within a
# project (see collect()'s ThreadPoolExecutor fan-out). Matches CICDAgent's
# _PIPELINE_WORKERS precedent for the same "many independent read-only GitLab
# API calls" shape.
_IAC_FILE_WORKERS = 10

_IAC_EXTENSIONS = (".yml", ".yaml", ".tf", ".tofu", ".tfvars")
_TF_EXTENSIONS = (".tf", ".tofu", ".tfvars")
_INVENTORY_PATHS = ("inventories/", "inventory/", "hosts.yml", "hosts.yaml", "hosts.ini")

# Patterns used to detect potential secrets embedded in IaC files.
# We detect on the PATTERN TYPE only — the matched value is NEVER stored.
#
# GitLab #164 defect 2: these were bare `password\s*[=:]\s*\S+` matchers with no
# value inspection at all, so `POSTGRES_PASSWORD: ${PG_PASS}` — a *reference* to
# a secret held elsewhere, i.e. exactly the correct practice — scored identically
# to a hard-coded literal. Every pattern now carries a capture group on the
# VALUE, and the value is classified into a confidence tier (see
# _classify_secret_value) so references and placeholders can be suppressed and
# genuine literals can be ranked. The third tuple element is the BASE tier used
# when the value is an ordinary literal that no stronger signal upgrades.
_SECRET_PATTERNS = [
    (re.compile(r"password\s*[=:]\s*(\S+)", re.IGNORECASE), "password", "literal"),
    (re.compile(r"api[_-]?key\s*[=:]\s*(\S+)", re.IGNORECASE), "api_key", "literal"),
    (re.compile(r"token\s*[=:]\s*(\S+)", re.IGNORECASE), "token", "literal"),
    (re.compile(r"secret\s*[=:]\s*(\S+)", re.IGNORECASE), "secret", "literal"),
    (re.compile(r"private[_-]?key\s*[=:]\s*(\S+)", re.IGNORECASE), "private_key", "literal"),
]

# --- Confidence tiers (GitLab #164 defect 2) -------------------------------
# Ordered most- to least-alarming. Only LITERAL_HIGH and LITERAL are emitted;
# REFERENCE and PLACEHOLDER are suppressed, because a finding that fires on
# `${VAR}` trains operators to ignore the whole signal.
TIER_LITERAL_HIGH = "literal_high"
TIER_LITERAL = "literal"
TIER_REFERENCE = "reference"
TIER_PLACEHOLDER = "placeholder"
_SUPPRESSED_TIERS = frozenset({TIER_REFERENCE, TIER_PLACEHOLDER})

# A value that is plainly an indirection to a secret stored elsewhere:
#   ${DB_PASSWORD} / $DB_PASSWORD  — shell / compose interpolation
#   {{ vault_db_password }}        — Ansible vault-backed variable
#   !vault |                       — inline ansible-vault ciphertext
_REFERENCE_VALUE_RE = re.compile(
    r"^(?:\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\{\{\s*vault_[^}]*\}\}|!vault\b)",
)
# Markers that make the whole LINE a reference rather than a value, regardless of
# what the capture group grabbed — the Kubernetes/compose indirection vocabulary.
_REFERENCE_LINE_MARKERS = ("secretkeyref", "valuefrom", "envfrom", "secretref", "!vault")
# Values that are obviously not secrets — scaffolding left for a human to fill.
_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        '""',
        "''",
        "changeme",
        "change_me",
        "change-me",
        "todo",
        "tbd",
        "<redacted>",
        "redacted",
        "none",
        "null",
        "~",
        "example",
        "placeholder",
    }
)
_PLACEHOLDER_RE = re.compile(r"^x+$|^<[^>]*>$", re.IGNORECASE)
# Prefixes that are self-identifying credential formats — no entropy check needed.
_HIGH_CONFIDENCE_PREFIXES = (
    "ghp_",
    "gho_",
    "ghs_",
    "glpat-",
    "glrt-",
    "AKIA",
    "ASIA",
    "-----BEGIN",
)
_MIN_ENTROPY_LEN = 20
_MIN_ENTROPY_BITS = 3.5

# Root-level files/extensions that signal a repo carries IaC.
_IAC_MARKERS = (
    ".gitlab-ci.yml",
    ".tf",
    "ansible.cfg",
    "requirements.yml",
    "site.yml",
    "site.yaml",
    "playbook.yml",
    "playbook.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
)

# Paths within a repo to skip entirely — non-IaC content that would otherwise
# be ingested as false positives (e.g. InfraOps plugin internal YAML files).
_EXCLUDED_PATH_PREFIXES = (
    "knowledge/",
    "rules/",
    ".claude/",
    "docs/",
    "tests/",
    "skills/",
    "agents/",
    "commands/",
    "scripts/hooks/",
    # GitLab #164 defect 2: sample/fixture trees are where placeholder
    # credentials legitimately live. Scanning them manufactures findings that
    # can never be "fixed", which is the fastest way to get a security signal
    # ignored wholesale.
    "fixtures/",
    "testdata/",
    "test_data/",
    "examples/",
)

# Path SUBSTRINGS and SUFFIXES that mark sample/fixture content wherever it sits
# in the tree — the prefix tuple above only catches top-level placement, but
# `roles/db/tests/fixtures/vars.yml` and `docker-compose.yml.example` are just as
# certainly not real deployed configuration.
_EXCLUDED_PATH_MARKERS = ("/fixtures/", "/testdata/", "/test_data/", "/examples/")
_EXCLUDED_PATH_SUFFIXES = (".example", ".template", ".sample", ".dist")


def _is_excluded_path(fpath: str) -> bool:
    """True when a repo-relative path is sample/fixture/plugin-internal content.

    Shared by the collect-time file filter and by ``_scan_for_secrets`` — the
    scanner re-checks rather than trusting the caller, because a file can reach
    the scanner through a code path that never consulted the collect filter.
    """
    low = (fpath or "").lower()
    if any(low.startswith(p) for p in _EXCLUDED_PATH_PREFIXES):
        return True
    if any(m in low for m in _EXCLUDED_PATH_MARKERS):
        return True
    # Tolerate a trailing extension after the marker: "compose.yml.example" and
    # "vars.example.yml" are both sample files.
    return any(s in low for s in _EXCLUDED_PATH_SUFFIXES)


# Reserved top-level keys in .gitlab-ci.yml (not job names)
_CI_RESERVED_KEYS = {
    "stages",
    "variables",
    "default",
    "workflow",
    "include",
    "image",
    "services",
    "cache",
    "before_script",
    "after_script",
}


def _shannon_entropy(value: str) -> float:
    """Shannon entropy in bits/char — the standard "does this look random" test."""
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _classify_secret_value(raw_value: str, line: str = "") -> str:
    """Classify a matched secret VALUE into a confidence tier (GitLab #164).

    ``line`` is the full source line the match came from; some indirections
    (``valueFrom:`` / ``secretKeyRef:`` / ``envFrom:``) are properties of the
    line's structure rather than of the captured token.

    Order is deliberate:

    1. Line-level indirection markers win outright — inside a ``secretKeyRef``
       block nothing on the line is a literal, whatever it looks like.
    2. Self-identifying credential prefixes (``ghp_``, ``glpat-``, ``AKIA``,
       PEM headers) are checked BEFORE the placeholder test, so a real-format
       token that happens to be redacted-looking (``glpat-XXXXXXXX``) is still
       treated as the highest-confidence finding rather than dismissed.
    3. Reference syntax (``${VAR}`` / ``$VAR`` / ``{{ vault_* }}`` / ``!vault``).
    4. Placeholders (empty, CHANGEME, xxx, TODO, <redacted>).
    5. High-entropy quoted strings.
    6. Otherwise an ordinary literal.

    The value itself is NEVER returned or stored — only the tier.
    """
    if any(marker in (line or "").lower() for marker in _REFERENCE_LINE_MARKERS):
        return TIER_REFERENCE

    value = (raw_value or "").strip().strip(",;")
    unquoted = value.strip("\"'").strip()

    if any(unquoted.startswith(p) for p in _HIGH_CONFIDENCE_PREFIXES):
        return TIER_LITERAL_HIGH
    if _REFERENCE_VALUE_RE.match(unquoted):
        return TIER_REFERENCE
    if unquoted.lower() in _PLACEHOLDER_VALUES or _PLACEHOLDER_RE.match(unquoted):
        return TIER_PLACEHOLDER
    if (
        len(unquoted) >= _MIN_ENTROPY_LEN
        and _shannon_entropy(unquoted) >= _MIN_ENTROPY_BITS
        and value != unquoted  # quoted in source — a deliberate literal string
    ):
        return TIER_LITERAL_HIGH
    return TIER_LITERAL


def _k8s_secret_manifest_findings(content: str) -> bool:
    """True when ``content`` holds a k8s Secret manifest with a POPULATED body.

    GitLab #164 defect 3 (reframed): ``parse_k8s_resources_tool`` was never the
    problem — it is kind-agnostic and parses ``kind: Secret`` fine. The gap was
    that the secret SCANNER had no kind-awareness at all, so a literal
    ``stringData: password: hunter2`` inside a genuine Secret manifest scored the
    same as a ``${VAR}`` reference in a compose file, when it is the single
    highest-confidence finding this system can produce.

    An empty ``data: {}`` / ``stringData:`` block is not a finding — a Secret
    whose values are injected at deploy time is correct practice.
    """
    try:
        docs = list(yaml.safe_load_all(content))
    except yaml.YAMLError:
        return False
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Secret":
            continue
        for block in ("data", "stringData"):
            body = doc.get(block)
            if isinstance(body, dict) and body:
                return True
    return False


def _classify_yaml(fpath: str, fname: str, content: str) -> str | None:
    """
    Return the IaC resource type for a YAML file, or None to skip it.
    Priority: filename-exact -> path-prefix -> content inspection.
    Returns None for unclassifiable YAML (caller skips the file).
    """
    # 1. Exact filename matches — path-independent, unambiguous
    if fname == ".gitlab-ci.yml":
        return "gitlab_ci_pipeline"

    # 2. Docker Compose — filename pattern
    if fname.startswith("docker-compose") or fname in ("compose.yml", "compose.yaml"):
        return "docker_compose"

    # 3. K8s manifests — path prefix is definitive (no need to parse content)
    path_lower = fpath.lower()
    if any(
        path_lower.startswith(p) for p in ("k8s/", "kubernetes/", "manifests/", "helm/", "charts/")
    ):
        return "k8s_manifest"

    # 4. Content-based classification
    try:
        data = yaml.safe_load(content)
    except Exception:
        return None  # unparseable YAML — skip

    if isinstance(data, dict):
        # K8s: apiVersion + kind (catches manifests in non-standard paths)
        if "apiVersion" in data and "kind" in data:
            return "k8s_manifest"

        # Ansible requirements: collections or roles list
        if fname in ("requirements.yml", "requirements.yaml"):
            if "collections" in data or "roles" in data:
                return "ansible_requirements"

        # Docker Compose fallback: services dict
        if "services" in data and isinstance(data["services"], dict):
            return "docker_compose"

    if isinstance(data, list) and data:
        # Ansible playbook: list of plays, first item must have a 'hosts' key
        if isinstance(data[0], dict) and "hosts" in data[0]:
            return "ansible_playbook"

    # Unclassifiable — not recognisable IaC, skip it
    return None


def _extract_yaml_metadata(
    iac_type: str,
    fpath: str,
    fname: str,
    content: str,
    project: str,
    project_id: int,
    ref: str,
) -> dict:
    """Return a metadata dict appropriate for the given IaC type."""
    base = {
        "project": project,
        "project_id": project_id,
        "file_path": fpath,
        "ref": ref,
        "size_bytes": len(content.encode()),
    }

    try:
        data = yaml.safe_load(content)
    except Exception:
        return base

    if iac_type == "gitlab_ci_pipeline" and isinstance(data, dict):
        stages = data.get("stages", [])
        jobs = [k for k, v in data.items() if k not in _CI_RESERVED_KEYS and isinstance(v, dict)]
        return {**base, "stage_count": len(stages), "stages": stages, "job_count": len(jobs)}

    if iac_type == "docker_compose" and isinstance(data, dict):
        services = data.get("services", {}) or {}
        svc_names = list(services.keys()) if isinstance(services, dict) else []
        return {**base, "service_count": len(svc_names), "services": svc_names}

    if iac_type == "k8s_manifest" and isinstance(data, dict):
        meta = data.get("metadata") or {}
        return {
            **base,
            "kind": data.get("kind"),
            "api_version": data.get("apiVersion"),
            "resource_name": meta.get("name") if isinstance(meta, dict) else None,
            "namespace": meta.get("namespace") if isinstance(meta, dict) else None,
        }

    if iac_type == "ansible_requirements" and isinstance(data, dict):
        collections = data.get("collections") or []
        roles = data.get("roles") or []
        return {
            **base,
            "collection_count": len(collections),
            "role_count": len(roles),
        }

    if iac_type == "ansible_playbook" and isinstance(data, list):
        hosts = [play.get("hosts") for play in data if isinstance(play, dict) and "hosts" in play]
        return {**base, "play_count": len(data), "hosts": hosts}

    return base


class IaCAgent(ETLConnector):
    spec = AgentSpec(
        domain="iac",
        tier=Tier.COLLECTOR,
        schedule="20 */6 * * *",
        max_staleness=timedelta(hours=8),
        # P2: BELONGS_TO moved here from _emit_iac_edges' hand-written join.
        # The target node (GitlabProject) is declared by cicd — see
        # _IAC_FILE_NODE_TYPES above for why one concept costs seven of these.
        #
        # DEFINED_IN followed once the contract grew EdgeDirection.INVERSE
        # (the one-to-many gap P2 recorded and deferred). Its join is the same
        # one BELONGS_TO uses; only the stored arrow differs.
        #
        # RUNS_IMAGE followed once ``NodeSpec`` grew ``ChildSpec`` reach — its
        # join key lives in ``compose_services``, a child table with no
        # ``resources`` row, which is where a NodeSpec's world used to end. The
        # ``ContainerImage`` end is declared here too, closing the other half of
        # that gap: the entity had no owning collector while graph_maintenance
        # minted it inline.
        #
        # ANSIBLE_MANAGES followed last, RE-ANCHORED onto the
        # ``AnsibleInventoryFile`` iac owns instead of the GitLab project it
        # used to run out of (Option A of TRK-354 — see
        # _IAC_ANSIBLE_MANAGES_EDGES above for the decision and its provenance).
        # With it, _emit_iac_edges derived no relationship the graph contract
        # can express — and P5 has since deleted that method outright, along
        # with the two degenerate relationships it still produced. This tuple
        # is now the WHOLE of iac's edge output; there is no hand-written
        # deriver left in this file. See the _emit_iac_edges epitaph below.
        # MEMBER_OF joined at TRK-359 — the first junction declaration
        # (_INVENTORY_GROUP_NODE carries its member hosts via RowGather), and
        # the first of P5's two accepted losses restored. See the block comment
        # on _IAC_MEMBER_OF_EDGES for the decision record.
        emits_nodes=_IAC_FILE_NODES + (_CONTAINER_IMAGE_NODE, _INVENTORY_GROUP_NODE),
        emits_edges=(
            _IAC_BELONGS_TO_EDGES
            + _IAC_DEFINED_IN_EDGES
            + _IAC_RUNS_IMAGE_EDGES
            + _IAC_ANSIBLE_MANAGES_EDGES
            + _IAC_MEMBER_OF_EDGES
        ),
    )

    def _get_projects(self, settings) -> list[dict]:
        """
        Return the list of GitLab projects to scan.

        When iac_group_ids is configured, enumerate those groups precisely —
        this reaches repos the token can see but isn't a direct member of
        (e.g. playbooks/fleet-ansible lives in the 'playbooks' group).
        Falls back to all visible projects when no groups are configured.
        """
        if settings.iac_group_ids:
            seen: set[int] = set()
            projects: list[dict] = []
            group_errors: list[str] = []
            for gid in (g.strip() for g in settings.iac_group_ids.split(",") if g.strip()):
                try:
                    group_projects = gitlab_get_paginated(
                        f"/api/v4/groups/{gid}/projects?include_subgroups=false&simple=true"
                    )
                    for p in group_projects:
                        if p.get("id") not in seen:
                            seen.add(p["id"])
                            projects.append(p)
                    logger.info(
                        "IaCAgent: group %s → %d projects (total so far: %d)",
                        gid,
                        len(group_projects),
                        len(projects),
                    )
                except Exception as exc:
                    logger.warning("IaCAgent: failed to list group %s projects: %s", gid, exc)
                    group_errors.append(f"group {gid}: {exc}")
            if not projects and group_errors:
                raise RuntimeError(
                    "IaCAgent: every configured IaC group failed to list: "
                    + "; ".join(group_errors)
                )
            return projects

        # Fallback: all projects visible to the token (no membership restriction).
        # A listing failure is a TOTAL collection failure -> raise (F-007); the
        # old `return []` reported status="completed" with zero resources.
        try:
            return gitlab_get_paginated("/api/v4/projects?simple=true")
        except Exception as exc:
            raise RuntimeError(f"IaCAgent: failed to list visible projects: {exc}") from exc

    def collect(self, scope: str = "all") -> CollectOutcome:
        items: list[dict] = []
        # Per-project failures (root-tree fetch, full-tree fetch, per-file read)
        # are logged AND appended here so a run where most/all projects fail to
        # fetch surfaces as CollectionRun.status="partial" instead of a clean
        # "completed" (F-007: never silent-drop projects) -- matches every
        # sibling collector in this layer (cicd.py, cloud.py, etc.).
        errors: list[str] = []
        # Cached for run()'s detail-write phase (avoids re-fetching file content):
        #   _last_files    — per-file {item, content} for child-table parsing
        #   _last_projects — per-project context for the gitlab_projects upsert
        self._last_files: list[dict] = []
        self._last_projects: dict[int, dict] = {}
        settings = get_settings()
        all_projects = self._get_projects(settings)
        if not all_projects:
            return CollectOutcome(items=[], errors=[])

        projects = all_projects[: settings.iac_max_projects]
        logger.info(
            "IaCAgent: scanning %d of %d projects (cap=%d, groups=%r)",
            len(projects),
            len(all_projects),
            settings.iac_max_projects,
            settings.iac_group_ids or "all-visible",
        )

        for project in projects:
            pid = project.get("id")
            name = project.get("name", "unknown")
            if pid is None:
                logger.warning("IaCAgent: project missing id, skipping")
                continue
            ref = project.get("default_branch", "main")
            # Record project context so run() can upsert gitlab_projects even for
            # projects with no IaC files (it still merges name/branch). Keyed on id.
            self._last_projects[pid] = project

            # Quick IaC-presence check: fetch root tree (non-recursive) and skip
            # projects that have none of the known IaC marker files/extensions.
            try:
                root_tree = gitlab_repository_tree_tool.invoke(
                    {"project_id": pid, "path": "", "ref": ref, "recursive": False},
                    config={"callbacks": self.callbacks},
                )
            except Exception as exc:
                msg = f"root tree fetch failed for project {name}: {exc}"
                logger.warning("IaCAgent: %s", msg)
                errors.append(msg)
                continue

            root_names = {entry["name"] for entry in (root_tree or [])}
            has_iac = any(
                any(n == marker or n.endswith(marker) for n in root_names)
                for marker in _IAC_MARKERS
            )
            if not has_iac:
                logger.debug("IaCAgent: skipping %s — no IaC markers in root", name)
                continue

            try:
                tree = gitlab_repository_tree_tool.invoke(
                    {"project_id": pid, "path": "", "ref": ref, "recursive": True},
                    config={"callbacks": self.callbacks},
                )
            except Exception as exc:
                msg = f"tree fetch failed for project {name}: {exc}"
                logger.warning("IaCAgent: %s", msg)
                errors.append(msg)
                continue

            if not tree:
                continue

            candidates = []
            for file_info in tree:
                if file_info["type"] != "blob":
                    continue
                fpath = file_info["path"]
                fname = file_info["name"]
                if not any(fname.endswith(ext) for ext in _IAC_EXTENSIONS):
                    continue
                if _is_excluded_path(fpath):
                    logger.debug("IaCAgent: skipping excluded path %s/%s", name, fpath)
                    continue
                candidates.append((fpath, fname))

            # TRK-311: each candidate needs its own GitLab file-content fetch --
            # doing these one at a time was the actual bottleneck (every run
            # timed out at the global 300s ceiling despite writing real data
            # each time, confirmed via the 2026-08-02 domain-coverage audit).
            # Fetches are independent, read-only GETs, so fan them out the same
            # bounded-worker way CICDAgent already does for its own per-project
            # pipeline fetches. Workers return a result (or None) rather than
            # mutating `items`/`self._last_files` directly from a worker thread
            # -- appends happen back on the main thread via as_completed, same
            # as CICDAgent's pattern, so there's no reliance on list.append's
            # GIL atomicity being "good enough".
            if candidates:
                observe_pool("iac", f"{name}/files", len(candidates), _IAC_FILE_WORKERS)
                with ThreadPoolExecutor(max_workers=_IAC_FILE_WORKERS) as pool:
                    futures = {
                        pool.submit(self._process_iac_file, pid, name, fpath, fname, ref): (
                            fpath,
                            fname,
                        )
                        for fpath, fname in candidates
                    }
                    for future in as_completed(futures):
                        fpath, fname = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            msg = f"file read failed {name}/{fpath}: {exc}"
                            logger.warning("IaCAgent: %s", msg)
                            errors.append(msg)
                            continue
                        if result is None:
                            continue
                        items.append(result["item"])
                        self._last_files.append(result)

        return CollectOutcome(items=items, errors=errors)

    def _process_iac_file(self, pid, name: str, fpath: str, fname: str, ref: str) -> dict | None:
        """Fetch and classify one candidate IaC file. Runs on a worker thread
        (see the ThreadPoolExecutor fan-out in collect()) -- must not touch
        `items`/`self._last_files` directly. Returns ``{"item": ..., "content":
        ...}`` or ``None`` if the file turned out not to be classifiable
        (mirrors the exact classification order collect() used sequentially
        before TRK-311's parallelization)."""
        content = gitlab_file_tool.invoke(
            {"project_id": pid, "file_path": fpath, "ref": ref},
            config={"callbacks": self.callbacks},
        )

        # --- Terraform / OpenTofu ---
        if any(fname.endswith(ext) for ext in _TF_EXTENSIONS):
            resources = parse_terraform_resources_tool.invoke(
                {"content": content},
                config={"callbacks": self.callbacks},
            )
            item = {
                "name": f"{name}/{fpath}",
                "type": "terraform_file",
                "data": {
                    "project": name,
                    "project_id": pid,
                    "file_path": fpath,
                    "ref": ref,
                    "resource_count": len(resources),
                    "resources": resources,
                },
            }
            return {"item": item, "content": content}

        # --- Ansible inventory ---
        if any(fpath.startswith(p) or fname == p for p in _INVENTORY_PATHS):
            parsed = parse_ansible_inventory_content_tool.invoke(
                {"content": content},
                config={"callbacks": self.callbacks},
            )
            item = {
                "name": f"{name}/{fpath}",
                "type": "ansible_inventory_file",
                "data": {
                    "project": name,
                    "project_id": pid,
                    "file_path": fpath,
                    "ref": ref,
                    "group_count": len(parsed.get("groups", {})),
                    "host_count": len(parsed.get("hosts", {})),
                    "groups": list(parsed.get("groups", {}).keys()),
                },
            }
            return {"item": item, "content": content}

        # --- Content-classified YAML ---
        iac_type = _classify_yaml(fpath, fname, content)
        if iac_type is None:
            logger.debug("IaCAgent: skipping unclassifiable YAML %s/%s", name, fpath)
            return None

        item = {
            "name": f"{name}/{fpath}",
            "type": iac_type,
            "data": _extract_yaml_metadata(iac_type, fpath, fname, content, name, pid, ref),
        }
        return {"item": item, "content": content}

    # --- de-dupe guard (F-018) -------------------------------------------
    def _upsert_resource(self, session, item: dict) -> Resource:
        """iac resource identity is ``(domain, name)`` — the name embeds
        project + file path, which IS the stable natural key. The shared
        upsert keys on ``(domain, type, name)``, so a file re-classified
        between runs (e.g. yaml classification drift) inserted a same-name
        duplicate. Re-type the existing row first so the upsert finds it.
        """
        _retype_existing_by_name(session, self.domain, item)
        return super()._upsert_resource(session, item)

    # --- TRK-258 (3): repair pre-F-018 orphans ----------------------------
    def _reconcile_stale_typed_duplicates(self, session) -> int:
        """Retire LIVE ``iac`` resource rows that are stale, same-name,
        different-type duplicates of a fresher row.

        F-018 (``_retype_existing_by_name`` above) prevents this from
        happening on any reclassification GOING FORWARD: it retypes the
        existing row in place, so there is only ever one row per (domain,
        name) after that guard runs. It does nothing for rows that were
        already duplicated BEFORE the guard existed — e.g. a June
        ``ansible_playbook`` row for ``k8s/scheduler.yaml`` that was never
        retired when a later run reclassified the same path as
        ``k8s_manifest`` and (pre-guard) inserted a second row instead of
        retyping the first.

        This is a repair pass, not a migration (this wave's migration slot
        is held by another agent): idempotent (a domain with no orphans left
        is a no-op every subsequent run), scoped to ``self.domain`` only, and
        it RETIRES (stamps ``retired_at``) rather than deletes — same
        convention as ``drift.py``/``graph_maintenance.py``. Called once per
        run from ``_write_iac_details``, in the same transaction as every
        other detail write.

        For each ``name`` with more than one live (``retired_at IS NULL``)
        row in this domain, keeps the most-recently-``last_seen`` row and
        retires the rest.
        """
        live = (
            session.query(Resource)
            .filter(Resource.domain == self.domain, Resource.retired_at.is_(None))
            .order_by(Resource.name, Resource.last_seen.desc())
            .all()
        )
        by_name: dict[str, list[Resource]] = {}
        for r in live:
            by_name.setdefault(r.name, []).append(r)
        now = datetime.now(UTC)
        retired = 0
        for rows in by_name.values():
            if len(rows) <= 1:
                continue
            # rows[0] is the most-recently-seen row for this name (the query
            # above is already ordered last_seen DESC within each name group).
            for stale in rows[1:]:
                stale.retired_at = now
                retired += 1
        return retired

    # --- run: populate gitlab_projects + iac_files + child detail tables ------

    def _detail_writers(self, scope, result):
        # Surface any structural detail-write failure on the run (never silent)
        # via ETLConnector.run()'s _write_details. Per-file parse errors are
        # caught inside _write_iac_details so one bad file doesn't abort the rest.
        return [lambda: self._write_iac_details(result.run_id, result)]

    def _write_iac_details(self, run_id=None, result: "CollectionResult | None" = None) -> int:
        files = getattr(self, "_last_files", None) or []
        projects = getattr(self, "_last_projects", None) or {}
        rows_written = 0

        with get_session() as session:
            # --- gitlab_projects (merge; don't clobber cicd's richer fields) ---
            # Only set the columns we actually have (name + default_branch),
            # leaving cicd-populated columns (visibility, archived, ...) intact.
            for pid, project in projects.items():
                pname = project.get("name", "")
                existing = session.query(GitlabProject).filter_by(gitlab_project_id=pid).first()
                if existing is not None:
                    # Merge: only fill name/default_branch (don't overwrite with None).
                    if pname:
                        existing.name = pname
                    if project.get("default_branch"):
                        existing.default_branch = project.get("default_branch")
                else:
                    session.add(
                        GitlabProject(
                            gitlab_project_id=pid,
                            name=pname,
                            default_branch=project.get("default_branch"),
                        )
                    )
            session.flush()

            # --- iac_files + per-file child tables ---
            # Pre-filter: a file with no project_id can't be keyed relationally
            # (matches the original `if pid is None: continue` — never counted
            # as an attempted write, so it's excluded before _write_each runs).
            keyable_files = [
                r for r in files if r["item"].get("data", {}).get("project_id") is not None
            ]

            def _write_one_file(record: dict) -> None:
                item = record["item"]
                content = record.get("content", "")
                data = item.get("data", {})
                gen_type = item.get("type")
                file_type = _TYPE_TO_FILE_TYPE.get(gen_type, gen_type)
                pid = data.get("project_id")
                pname = data.get("project")
                path = data.get("file_path", "")
                ref = data.get("ref", "")

                resource = (
                    session.query(Resource)
                    .filter_by(domain=self.domain, name=item.get("name"))
                    .first()
                )
                iac_row = {
                    "gitlab_project_id": pid,
                    "project_name": pname,
                    "path": path,
                    "file_type": file_type,
                    "ref": ref,
                    "size_bytes": data.get("size_bytes", 0) or 0,
                    "details": {
                        k: v
                        for k, v in data.items()
                        if k not in ("project", "project_id", "file_path", "ref", "size_bytes")
                    },
                }
                if resource is not None:
                    iac_row["resource_id"] = resource.id
                self._upsert_detail(
                    session,
                    IacFile,
                    iac_row,
                    ["gitlab_project_id", "path", "ref", "file_type"],
                )
                # Re-fetch to get the (possibly newly-inserted) row id.
                iac_file = (
                    session.query(IacFile)
                    .filter_by(gitlab_project_id=pid, path=path, ref=ref, file_type=file_type)
                    .first()
                )
                if iac_file is not None:
                    self._write_child_rows(session, iac_file, file_type, content)
                # Secret detection: scan every IaC file content.
                # resource_id may be None for newly-inserted files that
                # haven't been linked yet; _scan_for_secrets guards on that.
                resource_id = iac_row.get("resource_id")
                # file_type is already normalized above; the scanner needs it for
                # the k8s Secret-kind awareness (GitLab #164 defect 3).
                self._scan_for_secrets(
                    session, resource_id, path, content, run_id, file_type=file_type
                )

            # Per-file guard: a bad parse on one file logs + skips that file,
            # never aborting the whole detail-write (which would surface as a
            # run failure even though most files parsed fine).
            written, skipped = self._write_each(
                session,
                keyable_files,
                _write_one_file,
                label_fn=lambda record, exc: (
                    "IaCAgent: detail-write for "
                    f"{record['item'].get('name')} failed, skipping file: {exc}"
                ),
            )
            rows_written += written
            self._iac_files_skipped = skipped

            # --- CI schedules: one fetch per project -------------------------
            # Use the GitlabProject's resource_id as the anchor (non-nullable
            # on CiSchedule). Projects without a Resource row are skipped.
            #
            # M-2 (priority site): _collect_ci_schedules deletes a project's
            # existing CiSchedule rows then rebuilds them from the freshly
            # fetched list — a genuine reconciliation pass (C-1 shape). A
            # failure that raised AFTER the delete used to commit the wipe
            # with only a DEBUG log line. ci_schedule_scope records every
            # fetch outcome so the failure is reported through
            # ETLConnector._record_partial_errors below (the delete itself is
            # now wrapped in the SAME per-project SAVEPOINT as the rebuild —
            # see _collect_ci_schedules — so a failure rolls back the delete
            # too, never just the reinsert).
            ci_schedule_scope = ReconcileScope(label="ci_schedule project")
            for pid, project in projects.items():
                gp = session.query(GitlabProject).filter_by(gitlab_project_id=pid).first()
                if gp is not None and gp.resource_id is not None:
                    self._collect_ci_schedules(session, gp.resource_id, pid, ci_schedule_scope)

            # No knowledge-graph edges are written here. ``_emit_iac_edges``
            # stood at this point until P5 deleted it — see its epitaph below.
            # Every relationship iac owns is now DECLARED on ``spec.emits_edges``
            # and materialised into ``graph_edges`` by ``graph_engine``.

            # Stale-typed-duplicate repair (TRK-258 item 3) — see
            # _reconcile_stale_typed_duplicates for why this is a repair pass
            # here rather than a migration.
            self._reconcile_stale_typed_duplicates(session)

            session.commit()
        self._record_partial_errors(result, ci_schedule_scope.errors)
        return rows_written

    def _scan_for_secrets(
        self,
        session,
        resource_id,
        file_path: str,
        content: str,
        run_id=None,
        file_type: str | None = None,
    ) -> None:
        """Emit a DriftEvent per distinct potential-secret finding in an IaC file.

        SECURITY: the matched value is NEVER stored — only the file path, the
        pattern type (e.g. "password", "api_key") and the confidence tier.

        DL-C-8 established the query-before-insert idempotency here. GitLab #164
        fixes three things that idempotency check got wrong or left undone:

        * **Dedup was under-keyed and unindexable** (defect 1). It queried on
          ``(resource_id, drift_type, status)`` and then filtered the result set
          in PYTHON on ``new_value["secret_type"]`` — a JSONB blob field — with
          ``file_path`` absent from the key entirely. Two different files under
          the same resource collided on the coarse key, and the client-side
          filter could not use an index. Now every event carries an explicit
          indexed ``dedup_key`` of ``secret:<secret_type>:<file_path>`` and the
          lookup is a single ``filter_by`` on real columns.
        * **Only the first pattern per file was ever tracked** — the trailing
          ``break`` ("one event per file is enough") meant a file containing both
          a hard-coded password and a hard-coded API key reported one finding,
          and fixing that one made the other appear as if it were new. Each
          (file, secret_type) condition is now tracked independently.
        * **A re-observation re-fired or was silently dropped.** An already-open
          finding now bumps ``last_seen_at`` (and refreshes the tier if the
          classification changed) instead of either duplicating or no-oping.
        """
        if resource_id is None:
            return
        # Sample/fixture trees are re-checked here, not just at collect time —
        # see _is_excluded_path.
        if _is_excluded_path(file_path):
            return

        findings: dict[str, str] = {}

        # GitLab #164 defect 3: kind-awareness. A populated data:/stringData: in
        # a genuine k8s Secret manifest is the highest-confidence finding there
        # is — and, because `data:` is base64, it is invisible to every regex
        # below, so it needs its own detection.
        in_k8s_secret = file_type == "k8s_manifest" and _k8s_secret_manifest_findings(content)
        if in_k8s_secret:
            findings["k8s_secret_data"] = TIER_LITERAL_HIGH

        for pattern, secret_type, base_tier in _SECRET_PATTERNS:
            for match in pattern.finditer(content):
                line_start = content.rfind("\n", 0, match.start()) + 1
                line_end = content.find("\n", match.end())
                line = content[line_start : line_end if line_end != -1 else len(content)]
                tier = _classify_secret_value(match.group(1), line)
                if tier == TIER_LITERAL:
                    tier = base_tier
                    # A literal inside a real Secret manifest is not an ordinary
                    # literal — it is a credential checked into the manifest that
                    # deploys it.
                    if in_k8s_secret:
                        tier = TIER_LITERAL_HIGH
                if tier in _SUPPRESSED_TIERS:
                    continue
                # Keep the STRONGEST tier seen for this secret_type in this file.
                if findings.get(secret_type) != TIER_LITERAL_HIGH:
                    findings[secret_type] = tier

        now = datetime.now(UTC)
        for secret_type, tier in findings.items():
            dedup_key = f"secret:{secret_type}:{file_path}"[:256]
            new_value = {
                "file_path": file_path,
                "secret_type": secret_type,
                "confidence_tier": tier,
            }
            existing = (
                session.query(DriftEvent)
                .filter_by(
                    resource_id=resource_id,
                    drift_type="potential_secret_in_iac",
                    dedup_key=dedup_key,
                    status="open",
                )
                .first()
            )
            if existing is not None:
                # Same rule as host_reconcile (GitLab #163): a re-observation
                # bumps last_seen_at; detected_at/collection_run_id only advance
                # when the finding's data actually changed.
                existing.last_seen_at = now
                if existing.new_value != new_value:
                    existing.new_value = new_value
                    existing.detected_at = now
                    existing.collection_run_id = run_id
                continue
            session.add(
                DriftEvent(
                    resource_id=resource_id,
                    collection_run_id=run_id,
                    drift_type="potential_secret_in_iac",
                    field="file_path",
                    old_value=None,
                    new_value=new_value,
                    dedup_key=dedup_key,
                    detected_at=now,
                    last_seen_at=now,
                    status="open",
                )
            )
            logger.warning(
                "IaCAgent: potential %s (%s confidence) found in IaC file: %s (value not recorded)",
                secret_type,
                tier,
                file_path,
            )

    def _collect_ci_schedules(
        self, session, resource_id, project_id: int, scope: ReconcileScope
    ) -> None:
        """Fetch GitLab pipeline schedules for a project and store them in ci_schedules.

        ``resource_id`` should be the GitlabProject's Resource row id (the only
        reliable non-nullable anchor at collect time).

        M-2 (priority site / C-1 shape): this is delete-then-reinsert
        reconciliation — every existing ``CiSchedule`` row for the project is
        replaced by what GitLab reports NOW. That is only sound if the
        rebuild actually succeeds. Before this fix, the delete executed as
        soon as the fetch succeeded, and if row-building then raised (e.g. a
        malformed schedule dict missing ``"id"``), the delete was ALREADY
        staged in this shared session and got committed by the caller's
        end-of-phase ``session.commit()`` regardless — silently wiping the
        project's schedules, logged only at DEBUG. The delete and the
        rebuild now share ONE SAVEPOINT so a failure rolls back both
        together (existing rows survive), and the failure is recorded on
        ``scope`` so the caller can surface it via
        ``ETLConnector._record_partial_errors`` instead of the run reporting
        clean success over lost data.
        """
        if resource_id is None:
            return
        try:
            schedules = gitlab_get_paginated(f"/api/v4/projects/{project_id}/pipeline_schedules")
            with session.begin_nested():
                session.query(CiSchedule).filter_by(project_id=project_id).delete()
                for sched in schedules or []:
                    created_raw = sched.get("created_at")
                    try:
                        created_at = (
                            datetime.fromisoformat(created_raw.rstrip("Z")) if created_raw else None
                        )
                    except (ValueError, AttributeError):
                        created_at = None
                    session.add(
                        CiSchedule(
                            resource_id=resource_id,
                            project_id=project_id,
                            schedule_id=sched["id"],
                            description=sched.get("description"),
                            ref=sched.get("ref"),
                            cron=sched.get("cron"),
                            active=sched.get("active", True),
                            created_at=created_at,
                        )
                    )
                session.flush()
            scope.observed(project_id)
        except Exception as e:
            logger.warning(
                "IaCAgent: CI schedule collection failed for project %d — existing "
                "schedules preserved (delete/rebuild rolled back together): %s",
                project_id,
                e,
            )
            scope.failed(project_id, e)

    # ── _emit_iac_edges — DELETED (P5). The last hand-written edge deriver
    #    in this collector, and the last two relationships it still produced. ─
    #
    # WHAT IT WROTE, at the end: exactly two edge families, both into
    # ``resource_relationships`` via one ``emit_edges_batch`` call, and nothing
    # else — it READ ``gitlab_projects``, ``ci_pipeline_runs``,
    # ``compose_services``, ``iac_files`` and ``resources`` and wrote no detail
    # row and no ``resources`` row of its own.
    #
    #   * ``ci_pipeline_run TRIGGERED_BY gitlab_project``
    #   * ``compose_service DEPENDS_ON compose_service`` (from ``depends_on``)
    #
    # Everything else this method used to derive had already left, each under
    # the two-commit equivalence discipline, each now DECLARED on
    # ``spec.emits_edges`` and materialised into ``graph_edges``: BELONGS_TO
    # (P2), its inverse DEFINED_IN, RUNS_IMAGE, and finally ANSIBLE_MANAGES
    # re-anchored onto ``AnsibleInventoryFile`` (TRK-354 Option A). Their
    # verbatim oracles keep running in tests/agents/test_iac_belongs_to_graph.py,
    # test_iac_defined_in_graph.py, test_iac_runs_image_graph.py and
    # test_iac_ansible_manages_graph.py.
    #
    # WHY THE LAST TWO ARE GONE: the store is being dropped (P5 of
    # docs/decisions/2026-08-11-graph-first-architecture.md), and neither
    # survived that drop on its own merits. Per-type verdict:
    #
    #   TRIGGERED_BY — **rename artifact**. ``ci_pipeline_runs`` has no
    #     ``resources`` row of its own; its ``resource_id`` IS its project's
    #     row. So this edge resolved project -> project, and produced a
    #     non-self-loop at all ONLY because cicd's L-8b rename left two
    #     ``resources`` rows sharing one ``gitlab_project_id``. Every row it
    #     ever wrote is an artifact of that duplication, not a recorded fact —
    #     which is exactly what the T3 drop audit condemned. The relationship
    #     the type NAMES (a RUN was triggered by a project) needs the run to be
    #     an entity first; that prerequisite is recorded in docs/TRACKER.md
    #     rather than being silently satisfied by a duplicate row.
    #
    #   DEPENDS_ON — **writes nothing on this estate**. ``ComposeService`` has
    #     no ``resources`` row either, so ``svc_rid_map`` resolved BOTH ends of
    #     every candidate edge to the parent compose FILE's resource. The
    #     ``to_rid != from_rid`` guard and ``emit_edges_batch``'s own self-loop
    #     drop (F-022) between them discarded 100% of the output. Zero live
    #     rows, measured, not asserted.
    #
    # NEITHER IS A §3.1 CONTAINMENT REFUSAL — pipeline-run -> project and
    # service -> service are both genuine relationships between things that
    # would be independently referrable IF they were entities. That "if" is the
    # whole problem: neither endpoint has a ``resources`` row, which is why
    # both degenerated. Re-establishing either one starts by giving
    # ``ci_pipeline_runs`` / ``compose_services`` real nodes, and then comes
    # back as a COLLECTOR DECLARATION over those tables — never as a
    # re-derivation into the dropped store.
    #
    # WHERE THE FACTS LIVE MEANWHILE: ``ci_pipeline_runs.gitlab_project_id`` is
    # an indexed column on the run row itself, and ``compose_services.details``
    # carries the parsed ``depends_on`` list per service. Both questions were
    # always one column read on a table this collector writes; the edges were a
    # lossy second copy.

    def _write_child_rows(self, session, iac_file, file_type: str, content: str):
        """Populate the child detail table for one iac_file.

        Child rows are handled delete-then-reinsert (the LinuxAgent pattern):
        children are wholly derived from the current file content, so wiping and
        re-inserting cleanly drops services/resources/plays that the file no
        longer declares — simpler and more correct than trying to diff+upsert+prune.
        """
        cb = {"callbacks": self.callbacks}
        fid = iac_file.id

        if file_type == "compose":
            session.query(ComposeService).filter_by(iac_file_id=fid).delete()
            for svc in parse_compose_services_tool.invoke({"content": content}, config=cb):
                session.add(
                    ComposeService(
                        iac_file_id=fid,
                        service_name=svc["service_name"],
                        image=svc.get("image"),
                        ports=svc.get("ports", []),
                        details=svc.get("config") or None,
                    )
                )

        elif file_type == "k8s_manifest":
            session.query(K8sManifestResource).filter_by(iac_file_id=fid).delete()
            for res in parse_k8s_resources_tool.invoke({"content": content}, config=cb):
                session.add(
                    K8sManifestResource(
                        iac_file_id=fid,
                        kind=res["kind"],
                        api_version=res.get("api_version"),
                        name=res.get("name", ""),
                        namespace=res.get("namespace"),
                        details={
                            "labels": res.get("labels") or {},
                            "annotations": res.get("annotations") or {},
                        },
                    )
                )

        elif file_type == "terraform":
            session.query(TerraformResource).filter_by(iac_file_id=fid).delete()
            for res in parse_terraform_resources_tool.invoke({"content": content}, config=cb):
                session.add(
                    TerraformResource(
                        iac_file_id=fid,
                        resource_type=res["resource_type"],
                        resource_name=res["resource_name"],
                        # The regex HCL parser exposes only type+name, not the
                        # block body, so attrs stays empty until a fuller parser.
                        details=None,
                    )
                )

        elif file_type == "inventory":
            parsed = parse_ansible_inventory_content_tool.invoke({"content": content}, config=cb)
            groups = parsed.get("groups", {}) or {}
            hosts = parsed.get("hosts", {}) or {}
            # Delete existing groups (cascade-by-hand: delete their hosts first).
            existing_groups = session.query(AnsibleInventoryGroup).filter_by(iac_file_id=fid).all()
            for g in existing_groups:
                session.query(AnsibleInventoryHost).filter_by(group_id=g.id).delete()
            session.query(AnsibleInventoryGroup).filter_by(iac_file_id=fid).delete()
            session.flush()
            for gname, ghosts in groups.items():
                group_row = AnsibleInventoryGroup(iac_file_id=fid, name=gname)
                session.add(group_row)
                session.flush()
                for hostname in ghosts or []:
                    session.add(
                        AnsibleInventoryHost(
                            group_id=group_row.id,
                            name=hostname,
                            vars=hosts.get(hostname) or None,
                        )
                    )

        elif file_type == "playbook":
            session.query(AnsiblePlaybookPlay).filter_by(iac_file_id=fid).delete()
            for play in parse_ansible_playbook_plays_tool.invoke({"content": content}, config=cb):
                session.add(
                    AnsiblePlaybookPlay(
                        iac_file_id=fid,
                        play_index=play["play_index"],
                        name=play.get("name"),
                        hosts=play.get("hosts", []),
                    )
                )
        # gitlab_ci / requirements: no child table — summary lives in iac_files.details


def _retype_existing_by_name(session, domain: str, item: dict) -> None:
    """F-018 helper: if a row with this (domain, name) exists under a different
    ``type``, update its type in place so the (domain, type, name) upsert
    matches it instead of inserting a duplicate. Testable without an agent."""
    new_type = item.get("type", "unknown")
    existing = (
        session.query(Resource)
        .filter_by(domain=domain, name=item["name"])
        .order_by(Resource.last_seen.desc())
        .first()
    )
    if existing is not None and existing.type != new_type:
        existing.type = new_type
        session.flush()
