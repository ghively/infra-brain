"""p4 backfill the last declared relationships into graph_edges

Revision ID: c7a41f0b93de
Revises: 8965b6329b94
Create Date: 2026-08-12

DATA MIGRATION, no schema change. The second and final backfill wave of
``docs/decisions/2026-08-11-graph-first-architecture.md`` — it carries the two
relationship types that became DECLARED after ``8965b6329b94`` (P3) ran and
therefore missed its allow-list:

* ``ANSIBLE_MANAGES`` (14 live rows) — P3 counted these as
  ``skipped_ansible_manages_deferred`` because the edge was still hand-written
  and blocked on the ownership question TRK-354 records. That question is now
  answered (Option A: re-anchor onto the ``AnsibleInventoryFile`` iac owns), so
  the rows have a maintainer and can move.
* ``ISSUED_BY`` (3 live rows) — P3 counted these as
  ``skipped_type_not_declared``. ``agents/pki.py`` now declares the edge, so the
  same reasoning applies.

Nothing is deleted here. ``resource_relationships`` keeps every row it has;
P5 is what drops the table.

THE ONE THING THIS MIGRATION DOES THAT P3 DID NOT: RE-ANCHOR
------------------------------------------------------------
P3 could copy a legacy row's endpoints straight across, because every type it
carried was declared with the SAME endpoints the deriver used. ``ANSIBLE_MANAGES``
is not: the deriver stored ``gitlab_project → linux_host``, and the declaration
stores ``ansible_inventory_file → linux_host``. Copying the legacy shape would
mint ``authority='auto'`` edges in a shape NOTHING declares — precisely the
"edge type with no maintainer" failure P3's allow-list exists to prevent, only
worse, because it would masquerade as a migrated relationship.

So each legacy ``ANSIBLE_MANAGES`` row is RE-ANCHORED before it is written,
using the live child tables and the identical join the declaration runs:

    legacy.from_resource  → gitlab_projects.gitlab_project_id
                          → iac_files (that project, with a resources row)
                          → ansible_inventory_groups → ansible_inventory_hosts
                          → whose name matches legacy.to_resource.name under the
                            SAME host-key fold the EdgeSpec declares

The first hop resolves through ``gitlab_projects.resource_id`` where it can and
falls back to ``resources.metadata['project_id']`` where it cannot — see
``project_id_for``. That fallback is not defensive padding: without it the
RETIRED pre-rename project row, which carries the earlier ``created_at`` this
whole backfill exists to recover, resolves to nothing, and 7 of the live 14 rows
vanish while the survivors get dated from the rename.

A legacy row with no such witness is counted ``skipped_no_inventory_witness``
and NOT written — the honest outcome for an assertion the re-anchored edge
cannot make (in practice: a project→host pair that came only from the
playbook-plays half and names a host no inventory group lists). On the live
shape there are none: all 7 distinct hosts are inventory members, which is the
same measurement ``tests/agents/test_iac_ansible_manages_graph.py`` pins.

``ISSUED_BY`` needs none of this — pki writes both endpoints itself and the
declaration keys them exactly as the deriver did — so it takes the plain P3
path.

THE RENAME DOUBLING DISAPPEARS, ON PURPOSE
------------------------------------------
cicd's L-8b rename left two ``resources`` rows per GitLab project, so the legacy
store holds 14 ``ANSIBLE_MANAGES`` rows for 7 real assertions. Both rows of each
pair re-anchor onto the SAME inventory file (the file's identity never changed),
so they merge into one edge with ``valid_from = MIN(created_at)`` — recovering
the true first observation from the retired row, exactly as P3's ``BELONGS_TO``
merge did. That collapse is the point, not a side effect: the legacy store could
not express "same relationship, renamed endpoint" at all.

EVERYTHING ELSE IS P3's PATTERN, DELIBERATELY UNCHANGED
--------------------------------------------------------
Closed-prehistory policy against an existing ACTIVE engine edge; allow-list
scope; per-type counted disposition buckets with every legacy row landing in
exactly one; get-or-create nodes that never update an existing row; idempotence
via ``source LIKE 'p4_backfill:%'``; informational prints; a no-op when
``resource_relationships`` is already gone. Read
``8965b6329b94``'s docstring for the reasoning behind each — it is not repeated
here, and where this file diverges it says so.

``downgrade()`` is reversible for the rows this migration still owns; see its
docstring for the one case it deliberately leaves alone.
"""

# NOTE: deliberately NO ``from __future__ import annotations`` here — same
# reason as ``8965b6329b94``: alembic loads a version file by path without
# registering it in ``sys.modules``, and ``@dataclass`` resolves STRING
# annotations through ``sys.modules[cls.__module__].__dict__``, which is None
# for such a module. ``tests/test_p4_backfill.py`` loads this module the way
# alembic does so the regression cannot come back silently.
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7a41f0b93de"
down_revision: Union[str, Sequence[str], None] = "8965b6329b94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

BACKFILL_SOURCE_PREFIX = "p4_backfill:"

#: P3's marker, recognised so a P3-written row is never counted as missing.
P3_SOURCE_PREFIX = "p3_backfill:"

#: The allow-list for THIS wave: types a collector declares now and did not
#: when P3 ran.
MIGRATABLE_TYPES = frozenset({"ANSIBLE_MANAGES", "ISSUED_BY"})

#: Types whose legacy rows P3 already carried. Counted separately so the report
#: shows the two waves partitioning the declared population rather than one
#: wave appearing to have missed rows.
P3_TYPES = frozenset({"BELONGS_TO", "DEFINED_IN", "RUNS_IMAGE"})

#: Types whose stored endpoints are NOT the endpoints the declaration uses, and
#: which therefore go through ``_reanchor`` instead of being copied across.
REANCHORED_TYPES = frozenset({"ANSIBLE_MANAGES"})

#: Containment / decomposition per §3.1. Copied verbatim from ``8965b6329b94``
#: — a migration must keep meaning what it meant, so this is frozen rather than
#: imported from the earlier file.
CONTAINMENT_TYPES = frozenset(
    {
        "HAS_DRIFT",
        "ON_FIELD",
        "HAS_ACCOUNT",
        "EXPOSES_PORT",
        "HAS_MOUNT",
        "HAS_CRON",
        "HAS_FIREWALL_RULE",
        "HAS_PENDING_UPDATE",
        "HAS_SOFTWARE",
        "TAGGED_AS",
        "HAS_CERTIFICATE",
        "HAS_SHARE",
        "HAS_SNAPSHOT",
        "HAS_VARIABLE",
        "HAS_DISK",
        "HAS_NIC",
        "HAS_HBA",
        "HAS_PHYSICAL_DISK",
        "HAS_SECURITY_POSTURE",
        "HAS_VIOLATION",
        "HAS_ROLE",
        "HAS_VENDOR",
        "HAS_SCHEDULE",
        "HAS_BIOS",
        "HAS_CPU_MODEL",
        "HAS_HARDWARE_MODEL",
        "HAS_HARDWARE_VENDOR",
        "HAS_ACCESS",
        "HAS_VULNERABILITY_SCAN",
    }
)

# Dispositions. Every legacy row lands in exactly one.
MIGRATED_HISTORY = "migrated_as_history"
MIGRATED_ACTIVE = "migrated_as_active"
MIGRATED_CLOSED = "migrated_as_closed"
MERGED = "merged_into_earlier_row"
SKIPPED_ALREADY = "skipped_already_backfilled"
SKIPPED_NO_PREHISTORY = "skipped_no_prehistory"
SKIPPED_CONTAINMENT = "skipped_containment"
SKIPPED_P3 = "skipped_carried_by_p3"
SKIPPED_UNDECLARED = "skipped_type_not_declared"
SKIPPED_UNMAPPABLE = "skipped_unmappable_endpoint"
SKIPPED_NO_WITNESS = "skipped_no_inventory_witness"


# ---------------------------------------------------------------------------
# Node identity conventions — copied from the NodeSpec declarations, frozen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeConvention:
    """How ``graph_engine`` names the node for one ``(domain, resource_type)``.

    Same class as ``8965b6329b94``'s, for the same reason: frozen here rather
    than imported so a future ``NodeSpec`` edit cannot change what this
    migration did the day it ran.
    """

    node_type: str
    #: ``"name"`` = ``resources.name``; ``"metadata:<k>"`` = ``metadata[k]``.
    key_from: str
    #: ``graph_nodes.source`` — the DOMAIN that DECLARES the node, which is not
    #: always the endpoint resource's own domain (``Certificate`` rows live in
    #: ``security`` but are declared and owned by ``pki``).
    source: str
    resource_backed: bool
    attribute_keys: tuple = ()


#: (resources.domain, resources.type) -> convention.
#:
#: AnsibleInventoryFile: ``agents/iac.py::_IAC_FILE_NODES`` — natural_key="name",
#: attributes=(project_id, file_path, ref). The ``managed_hosts`` gathered list
#: is deliberately NOT reproduced: it is derived from child rows the engine
#: re-reads every pass, and a migration that froze one pass's copy would leave a
#: stale list on any node it happened to mint.
#: LinuxHost: ``agents/linux.py`` — the entity only, no attributes.
#: Certificate / CertificateAuthority: ``agents/pki.py``.
NODE_CONVENTIONS = {
    ("iac", "ansible_inventory_file"): NodeConvention(
        "AnsibleInventoryFile", "name", "iac", True, ("project_id", "file_path", "ref")
    ),
    ("linux", "linux_host"): NodeConvention("LinuxHost", "name", "linux", True, ()),
    ("security", "certificate"): NodeConvention(
        "Certificate", "name", "pki", True, ("thumbprint", "subject", "issuer")
    ),
    ("pki", "certificate_authority"): NodeConvention(
        "CertificateAuthority", "name", "pki", True, ("ca_type",)
    ),
}


# ---------------------------------------------------------------------------
# The host-key fold, frozen
# ---------------------------------------------------------------------------

_IPV4_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$")

#: ``tools/hostmatch._GENERIC_HOSTNAMES``, frozen.
_GENERIC_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "unknown",
        "none",
        "null",
        "n/a",
        "na",
        "host",
        "hostname",
        "default",
        "server",
        "vm",
        "template",
        "new virtual machine",
    }
)


def host_match_key(name):
    """``tools/hostmatch.graph_match_key``, frozen for this migration.

    The EdgeSpec declares ``key_normalizer="host"``, so the re-anchor must fold
    hostnames the SAME way or it would resolve a different set of witnesses than
    the live edge does. Copied rather than imported for the reason every other
    frozen block here is: a migration must keep doing what it did the day it
    ran, and ``graph_match_key``'s docstring already anticipates being widened
    (the ``normalize_host`` backfill it defers).
    """
    if not name:
        return ""
    low = str(name).strip().lower().rstrip(".")
    if not low:
        return ""
    if _IPV4_RE.match(low) or ":" in low:
        return low.replace("_", "-")
    short = low.split(".")[0]
    if short in _GENERIC_HOSTNAMES:
        return ""
    return short.replace("_", "-")


# ---------------------------------------------------------------------------
# Pure planning layer (no database access — see tests/test_p4_backfill.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceRow:
    """One endpoint, as read from ``resources``."""

    id: str
    domain: str
    type: str
    name: str
    retired_at: Optional[datetime]
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LegacyRow:
    """One ``resource_relationships`` row plus both resolved endpoints."""

    id: str
    relationship_type: str
    source: str
    confidence: float
    status: str
    created_at: datetime
    last_seen: datetime
    from_resource: ResourceRow
    to_resource: ResourceRow


@dataclass(frozen=True)
class PlannedNode:
    node_type: str
    natural_key: str
    name: str
    source: str
    resource_id: Optional[str]
    attributes: Optional[dict]

    @property
    def identity(self):
        return (self.node_type, self.natural_key)


@dataclass(frozen=True)
class PlannedEdge:
    source_identity: tuple
    target_identity: tuple
    edge_type: str
    method: str
    confidence: Decimal
    source: str
    evidence: dict
    valid_from: datetime
    valid_to: Optional[datetime]
    disposition: str


@dataclass
class BackfillPlan:
    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    #: relationship_type -> disposition -> count
    counts: dict = field(default_factory=dict)

    def count(self, relationship_type, disposition, n=1):
        self.counts.setdefault(relationship_type, {}).setdefault(disposition, 0)
        self.counts[relationship_type][disposition] += n

    def total(self):
        return sum(sum(d.values()) for d in self.counts.values())


def classify(relationship_type):
    """Bucket a legacy relationship type. Allow-list first, refusals after."""
    if relationship_type in MIGRATABLE_TYPES:
        return "migrate"
    if relationship_type in P3_TYPES:
        return SKIPPED_P3
    if relationship_type in CONTAINMENT_TYPES:
        return SKIPPED_CONTAINMENT
    return SKIPPED_UNDECLARED


def node_for(resource):
    """Resolve a ``resources`` row to its ``graph_nodes`` identity + payload.

    ``None`` when the endpoint's ``(domain, type)`` has no declared convention —
    the caller counts and skips, never guesses a key.
    """
    convention = NODE_CONVENTIONS.get((resource.domain, resource.type))
    if convention is None:
        return None
    if convention.key_from == "name":
        raw = resource.name
    else:
        _, _, key = convention.key_from.partition(":")
        raw = (resource.metadata or {}).get(key)
    natural_key = "" if raw is None else str(raw).strip()
    if not natural_key:
        return None
    blob = resource.metadata or {}
    attributes = {k: blob[k] for k in convention.attribute_keys if blob.get(k) is not None}
    return PlannedNode(
        node_type=convention.node_type,
        natural_key=natural_key[:512],
        name=(resource.name or natural_key)[:256],
        source=convention.source,
        resource_id=resource.id if convention.resource_backed else None,
        attributes=attributes or None,
    )


def _method_for(confidence):
    """Derive ``method``. Frozen from P3 — 1.000 is legal only for ``declared``."""
    return "declared" if Decimal(str(confidence)) >= Decimal("1.000") else "deterministic_match"


def _quantize(confidence):
    value = Decimal(str(confidence)).quantize(Decimal("0.001"))
    if value < Decimal("0"):
        return Decimal("0.000")
    if value > Decimal("1.000"):
        return Decimal("1.000")
    return value


def _as_project_id(value):
    """``gitlab_projects.gitlab_project_id`` is an integer column; JSON is not."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def project_id_for(resource, project_id_by_resource):
    """The immutable GitLab numeric id behind a ``gitlab_project`` resource row.

    TWO SOURCES, AND THE FALLBACK IS THE LOAD-BEARING ONE.

    ``gitlab_projects.resource_id`` is the direct route, but cicd's L-8b rename
    REPOINTED that column at the new ``resources`` row — so the RETIRED
    pre-rename row, which is the one carrying the earlier ``created_at`` this
    backfill exists to recover, has no ``gitlab_projects`` row of its own.
    Resolving only through the table would silently drop exactly half the
    ``ANSIBLE_MANAGES`` population (measured: 7 of 14 on the live shape) and
    date every surviving edge from the rename instead of from first
    observation, which is the opposite of the point.

    So the fallback reads ``resources.metadata['project_id']`` — the SAME
    immutable numeric key ``agents/iac.py``'s BELONGS_TO declaration joins on,
    chosen there precisely because a rename cannot move it. It is a fallback
    rather than the primary because the table is the authority when it has an
    answer; they agree whenever both exist.
    """
    direct = project_id_by_resource.get(resource.id)
    if direct is not None:
        return _as_project_id(direct)
    return _as_project_id((resource.metadata or {}).get("project_id"))


def reanchor(row, *, project_id_by_resource, inventory_witnesses):
    """The re-anchored endpoints for one legacy ``ANSIBLE_MANAGES`` row.

    Returns a list of ``(source ResourceRow, target ResourceRow)`` — normally
    one, more only if several inventory FILES in that project name the same
    host, in which case each is a true assertion and each gets its edge (the
    declaration would write exactly the same set).

    Empty means no witness: the project has no inventory file whose descent
    names that host. The caller counts ``skipped_no_inventory_witness`` and
    writes nothing, which is the honest answer — the re-anchored edge simply
    cannot make that assertion.
    """
    project_id = project_id_for(row.from_resource, project_id_by_resource)
    if project_id is None:
        return []
    key = host_match_key(row.to_resource.name)
    if not key:
        return []
    files = inventory_witnesses.get((project_id, key)) or []
    return [(iac_file, row.to_resource) for iac_file in files]


def _resolved_endpoints(row, *, project_id_by_resource, inventory_witnesses):
    """``(list of endpoint pairs, disposition-or-None)`` for one legacy row."""
    if row.relationship_type not in REANCHORED_TYPES:
        return [(row.from_resource, row.to_resource)], None
    pairs = reanchor(
        row,
        project_id_by_resource=project_id_by_resource,
        inventory_witnesses=inventory_witnesses,
    )
    if not pairs:
        return [], SKIPPED_NO_WITNESS
    return pairs, None


def plan_backfill(
    rows,
    *,
    active_edges,
    existing_backfill,
    existing_nodes,
    project_id_by_resource=None,
    inventory_witnesses=None,
):
    """Decide, without touching a database, what P4 should write.

    ``active_edges`` maps a resolved ``(source_identity, target_identity,
    edge_type)`` triple to the ACTIVE engine edge's ``valid_from``.
    ``existing_backfill`` is the same triple for edges ANY backfill wave has
    already written. ``existing_nodes`` is every ``(node_type, natural_key)``
    already in ``graph_nodes``. The last two arguments feed the re-anchor and
    are only consulted for ``REANCHORED_TYPES``.
    """
    project_id_by_resource = project_id_by_resource or {}
    inventory_witnesses = inventory_witnesses or {}
    plan = BackfillPlan()
    groups = {}

    for row in rows:
        verdict = classify(row.relationship_type)
        if verdict != "migrate":
            plan.count(row.relationship_type, verdict)
            continue
        pairs, blocked = _resolved_endpoints(
            row,
            project_id_by_resource=project_id_by_resource,
            inventory_witnesses=inventory_witnesses,
        )
        if blocked is not None:
            plan.count(row.relationship_type, blocked)
            continue
        planned = []
        for from_resource, to_resource in pairs:
            src = node_for(from_resource)
            tgt = node_for(to_resource)
            if src is None or tgt is None:
                planned = None
                break
            planned.append((src, tgt, from_resource, to_resource))
        if not planned:
            plan.count(row.relationship_type, SKIPPED_UNMAPPABLE)
            continue
        # A re-anchored row can legitimately produce several edges; the row
        # still lands in ONE bucket, decided by its FIRST resolved triple, and
        # any further triples are recorded as their own group. That keeps
        # "every legacy row lands in exactly one disposition" true.
        counted = False
        for src, tgt, from_resource, to_resource in planned:
            triple = (src.identity, tgt.identity, row.relationship_type)
            if triple in existing_backfill:
                if not counted:
                    plan.count(row.relationship_type, SKIPPED_ALREADY)
                    counted = True
                continue
            groups.setdefault(triple, []).append((row, src, tgt, from_resource, to_resource))
            counted = True

    planned_node_identities = set()

    for triple in sorted(groups, key=lambda t: (t[2], t[0], t[1])):
        members = sorted(groups[triple], key=lambda m: (m[0].created_at, m[0].id))
        rel_type = triple[2]
        primary = members[0]
        valid_from = min(m[0].created_at for m in members)
        engine_valid_from = active_edges.get(triple)

        if engine_valid_from is not None:
            if valid_from >= engine_valid_from:
                plan.count(rel_type, SKIPPED_NO_PREHISTORY, len(members))
                continue
            valid_to = engine_valid_from
            disposition = MIGRATED_HISTORY
        else:
            valid_to = _closing_time(members, valid_from)
            disposition = MIGRATED_CLOSED if valid_to is not None else MIGRATED_ACTIVE

        confidence = min(_quantize(m[0].confidence) for m in members)
        sources = sorted({m[0].source for m in members})
        evidence = {
            "backfill": "p4",
            "legacy_table": "resource_relationships",
            "legacy_relationship_ids": [m[0].id for m in members],
            "legacy_sources": sources,
            "legacy_status": sorted({m[0].status for m in members}),
            "legacy_created_at": [m[0].created_at.isoformat() for m in members],
            "legacy_last_seen": max(m[0].last_seen for m in members).isoformat(),
            "legacy_from_resource_id": primary[0].from_resource.id,
            "legacy_to_resource_id": primary[0].to_resource.id,
        }
        if rel_type in REANCHORED_TYPES:
            # The stored edge does NOT run between the endpoints the legacy row
            # named. Say so on the row itself, so a reader who finds this edge
            # and then greps the legacy id is not left wondering why the ends
            # disagree.
            evidence["reanchored"] = {
                "reason": "TRK-354 option A: edge re-anchored onto the "
                "AnsibleInventoryFile iac owns",
                "legacy_from_resource_id": primary[0].from_resource.id,
                "legacy_from_name": primary[0].from_resource.name,
                "resolved_from_resource_id": primary[3].id,
                "resolved_from_name": primary[3].name,
            }
        if disposition == MIGRATED_HISTORY:
            evidence["superseded_by_active_edge_valid_from"] = engine_valid_from.isoformat()
        if len(members) > 1:
            evidence["merged_legacy_rows"] = len(members)

        plan.edges.append(
            PlannedEdge(
                source_identity=triple[0],
                target_identity=triple[1],
                edge_type=rel_type,
                method=_method_for(float(confidence)),
                confidence=confidence,
                source=(BACKFILL_SOURCE_PREFIX + "+".join(sources))[:128],
                evidence=evidence,
                valid_from=valid_from,
                valid_to=valid_to,
                disposition=disposition,
            )
        )
        plan.count(rel_type, disposition)
        if len(members) > 1:
            plan.count(rel_type, MERGED, len(members) - 1)

        for candidate in _endpoint_nodes(members):
            if candidate.identity in existing_nodes:
                continue
            if candidate.identity in planned_node_identities:
                continue
            planned_node_identities.add(candidate.identity)
            plan.nodes.append(candidate)

    return plan


def _endpoint_nodes(members):
    """One node payload per endpoint identity, preferring a live backing row."""
    chosen = {}
    for _row, src, tgt, from_resource, to_resource in members:
        for node, resource in ((src, from_resource), (tgt, to_resource)):
            rank = 1 if resource.retired_at is None else 0
            current = chosen.get(node.identity)
            if current is None or rank > current[0]:
                chosen[node.identity] = (rank, node)
    return [node for _, node in chosen.values()]


def _closing_time(members, valid_from):
    """When did this relationship stop being true, if it did?

    DIVERGES FROM P3 IN ONE WAY, and only for a re-anchored type: the endpoints
    consulted are the RESOLVED ones, not the legacy row's. That matters
    precisely because of the rename this backfill collapses — the retired
    ``gitlab_project`` row is not an endpoint of the edge being written, and
    closing the edge on its ``retired_at`` would record that Ansible stopped
    managing a host on the day a repo was renamed. Which is false.
    """
    candidates = []
    for row, _src, _tgt, from_resource, to_resource in members:
        if row.status != "active":
            candidates.append(row.last_seen)
        for endpoint in (from_resource, to_resource):
            if endpoint.retired_at is not None:
                candidates.append(endpoint.retired_at)
    if not candidates:
        return None
    return max(min(candidates), valid_from)


def format_report(counts):
    """One stable, greppable line per (relationship_type, disposition)."""
    lines = []
    for rel_type in sorted(counts):
        for disposition in sorted(counts[rel_type]):
            lines.append(f"  {rel_type:<20} {disposition:<34} {counts[rel_type][disposition]}")
    return lines


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

_SELECT_LEGACY = sa.text(
    """
    SELECT rr.id::text                AS id,
           rr.relationship_type       AS relationship_type,
           rr.source                  AS source,
           rr.confidence              AS confidence,
           rr.status                  AS status,
           rr.created_at              AS created_at,
           rr.last_seen               AS last_seen,
           fr.id::text                AS from_id,
           fr.domain                  AS from_domain,
           fr.type                    AS from_type,
           fr.name                    AS from_name,
           fr.retired_at              AS from_retired_at,
           fr.metadata                AS from_metadata,
           tr.id::text                AS to_id,
           tr.domain                  AS to_domain,
           tr.type                    AS to_type,
           tr.name                    AS to_name,
           tr.retired_at              AS to_retired_at,
           tr.metadata                AS to_metadata
      FROM resource_relationships rr
      JOIN resources fr ON fr.id = rr.from_resource_id
      JOIN resources tr ON tr.id = rr.to_resource_id
    """
)

_SELECT_NODES = sa.text("SELECT id::text, node_type, natural_key FROM graph_nodes")

_SELECT_EDGES = sa.text(
    """
    SELECT ge.edge_type, ge.source, ge.valid_from, ge.valid_to,
           sn.node_type AS s_type, sn.natural_key AS s_key,
           tn.node_type AS t_type, tn.natural_key AS t_key
      FROM graph_edges ge
      JOIN graph_nodes sn ON sn.id = ge.source_id
      JOIN graph_nodes tn ON tn.id = ge.target_id
    """
)

_SELECT_PROJECT_RESOURCES = sa.text(
    """
    SELECT resource_id::text AS resource_id, gitlab_project_id
      FROM gitlab_projects
     WHERE resource_id IS NOT NULL
    """
)

#: The re-anchor witness query — the SAME descent the ``ChildSpec`` walks
#: (``resources → iac_files → ansible_inventory_groups →
#: ansible_inventory_hosts``), plus the ``gitlab_project_id`` needed to tie a
#: legacy project endpoint to it.
_SELECT_INVENTORY_WITNESSES = sa.text(
    """
    SELECT f.gitlab_project_id       AS gitlab_project_id,
           h.name                    AS host_name,
           r.id::text                AS resource_id,
           r.domain                  AS domain,
           r.type                    AS type,
           r.name                    AS name,
           r.retired_at              AS retired_at,
           r.metadata                AS metadata
      FROM iac_files f
      JOIN resources r ON r.id = f.resource_id
      JOIN ansible_inventory_groups g ON g.iac_file_id = f.id
      JOIN ansible_inventory_hosts h ON h.group_id = g.id
     WHERE r.retired_at IS NULL
    """
)


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_param(bind, name):
    """Bind a JSON parameter, cast on PostgreSQL where the column is JSONB."""
    if bind.dialect.name == "postgresql":
        return f"CAST(:{name} AS JSONB)"
    return f":{name}"


def _load_inventory_witnesses(bind):
    """``(gitlab_project_id, host_match_key) -> [inventory-file ResourceRow]``."""
    witnesses = {}
    seen = set()
    for r in bind.execute(_SELECT_INVENTORY_WITNESSES).mappings():
        key = host_match_key(r["host_name"])
        if not key:
            continue
        pair = (_as_project_id(r["gitlab_project_id"]), key)
        if (pair, r["resource_id"]) in seen:
            continue
        seen.add((pair, r["resource_id"]))
        witnesses.setdefault(pair, []).append(
            ResourceRow(
                id=r["resource_id"],
                domain=r["domain"],
                type=r["type"],
                name=r["name"],
                retired_at=r["retired_at"],
                metadata=_as_dict(r["metadata"]),
            )
        )
    for bucket in witnesses.values():
        bucket.sort(key=lambda row: row.name)
    return witnesses


def upgrade() -> None:
    """Copy the two newly-declared relationship types into ``graph_edges``."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "resource_relationships" not in tables:
        print(f"[{revision}] resource_relationships absent — nothing to backfill")
        return
    if not {"graph_nodes", "graph_edges"} <= tables:
        print(f"[{revision}] graph tables absent — nothing to backfill")
        return

    rows = []
    for r in bind.execute(_SELECT_LEGACY).mappings():
        rows.append(
            LegacyRow(
                id=r["id"],
                relationship_type=r["relationship_type"],
                source=r["source"],
                confidence=float(r["confidence"]),
                status=r["status"],
                created_at=r["created_at"],
                last_seen=r["last_seen"],
                from_resource=ResourceRow(
                    id=r["from_id"],
                    domain=r["from_domain"],
                    type=r["from_type"],
                    name=r["from_name"],
                    retired_at=r["from_retired_at"],
                    metadata=_as_dict(r["from_metadata"]),
                ),
                to_resource=ResourceRow(
                    id=r["to_id"],
                    domain=r["to_domain"],
                    type=r["to_type"],
                    name=r["to_name"],
                    retired_at=r["to_retired_at"],
                    metadata=_as_dict(r["to_metadata"]),
                ),
            )
        )

    node_id_by_identity = {}
    for n in bind.execute(_SELECT_NODES):
        node_id_by_identity[(n[1], n[2])] = n[0]

    active_edges = {}
    existing_backfill = set()
    for e in bind.execute(_SELECT_EDGES).mappings():
        triple = ((e["s_type"], e["s_key"]), (e["t_type"], e["t_key"]), e["edge_type"])
        if e["valid_to"] is None:
            active_edges[triple] = e["valid_from"]
        source = str(e["source"] or "")
        if source.startswith(BACKFILL_SOURCE_PREFIX) or source.startswith(P3_SOURCE_PREFIX):
            existing_backfill.add(triple)

    project_id_by_resource = {}
    if "gitlab_projects" in tables:
        for r in bind.execute(_SELECT_PROJECT_RESOURCES).mappings():
            project_id_by_resource[r["resource_id"]] = r["gitlab_project_id"]

    inventory_witnesses = {}
    if {"iac_files", "ansible_inventory_groups", "ansible_inventory_hosts"} <= tables:
        inventory_witnesses = _load_inventory_witnesses(bind)

    plan = plan_backfill(
        rows,
        active_edges=active_edges,
        existing_backfill=existing_backfill,
        existing_nodes=set(node_id_by_identity),
        project_id_by_resource=project_id_by_resource,
        inventory_witnesses=inventory_witnesses,
    )

    node_sql = sa.text(
        "INSERT INTO graph_nodes "
        "(id, node_type, natural_key, name, source, resource_id, attributes, first_seen, last_seen) "
        "VALUES (:id, :node_type, :natural_key, :name, :source, :resource_id, "
        f"{_json_param(bind, 'attributes')}, :now, :now)"
    )
    edge_sql = sa.text(
        "INSERT INTO graph_edges "
        "(id, source_id, target_id, edge_type, method, confidence, evidence, source, "
        " authority, valid_from, valid_to, recorded_at) "
        "VALUES (:id, :source_id, :target_id, :edge_type, :method, :confidence, "
        f"{_json_param(bind, 'evidence')}, :source, 'auto', :valid_from, :valid_to, :now)"
    )

    now = datetime.now().astimezone()
    for node in plan.nodes:
        new_id = str(uuid.uuid4())
        bind.execute(
            node_sql,
            {
                "id": new_id,
                "node_type": node.node_type,
                "natural_key": node.natural_key,
                "name": node.name,
                "source": node.source,
                "resource_id": node.resource_id,
                "attributes": json.dumps(node.attributes) if node.attributes else None,
                "now": now,
            },
        )
        node_id_by_identity[node.identity] = new_id

    for edge in plan.edges:
        bind.execute(
            edge_sql,
            {
                "id": str(uuid.uuid4()),
                "source_id": node_id_by_identity[edge.source_identity],
                "target_id": node_id_by_identity[edge.target_identity],
                "edge_type": edge.edge_type,
                "method": edge.method,
                "confidence": edge.confidence,
                "evidence": json.dumps(edge.evidence),
                "source": edge.source,
                "valid_from": edge.valid_from,
                "valid_to": edge.valid_to,
                "now": now,
            },
        )

    print(
        f"[{revision}] P4 backfill: read {plan.total()} resource_relationships row(s); "
        f"wrote {len(plan.nodes)} graph_nodes and {len(plan.edges)} graph_edges"
    )
    for line in format_report(plan.counts):
        print(f"[{revision}]{line}")


_DELETE_BACKFILL = sa.text("DELETE FROM graph_edges WHERE source LIKE :prefix")


def downgrade() -> None:
    """Delete exactly the edges ``upgrade()`` still owns.

    Every row written here carries ``source = 'p4_backfill:<legacy source>'``, a
    marker no emitter writes, so the DELETE cannot touch an engine-written edge.
    ``resource_relationships`` was never modified, so downgrading loses no data
    — the rows are still in the legacy store.

    THE ONE ROW THIS DELIBERATELY LEAVES BEHIND, and why. Unlike P3 — which on
    the live database wrote only CLOSED historical edges, in a shape no emitter
    refreshes — this wave writes ACTIVE edges for relationships whose declaration
    had not yet run when the migration deployed. ``graph_engine``'s next pass
    adopts such a row: ``upsert_edge``'s W2 rule refreshes it IN PLACE (keeping
    ``valid_from``, which is the whole value of the backfill) and overwrites
    ``source`` with the engine's own. That row is then an engine-owned live
    derivation that merely happens to have been seeded here, and downgrading
    must NOT delete it — removing a live relationship because a superseded
    migration once wrote it would be strictly worse than leaving it.

    Re-running ``upgrade()`` after such a downgrade stays correct: it finds the
    active engine edge, computes an empty prehistory interval and counts
    ``skipped_no_prehistory`` rather than writing a duplicate.

    Nodes are deliberately NOT deleted, for the two reasons ``8965b6329b94``'s
    downgrade gives: a backfilled node is indistinguishable from an
    engine-written one by design, and ``graph_edges``' FKs are
    ``ON DELETE CASCADE``, so dropping a node the engine has since built on
    would silently destroy engine-written edges.
    """
    result = op.get_bind().execute(_DELETE_BACKFILL, {"prefix": f"{BACKFILL_SOURCE_PREFIX}%"})
    print(f"[{revision}] removed {result.rowcount} p4-backfilled graph_edges row(s)")
