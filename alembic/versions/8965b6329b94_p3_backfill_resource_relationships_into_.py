"""p3 backfill resource_relationships into graph_edges

Revision ID: 8965b6329b94
Revises: adacc8308153
Create Date: 2026-08-12

DATA MIGRATION, no schema change. Phase **P3** of
``docs/decisions/2026-08-11-graph-first-architecture.md`` — copy the genuine
relationships out of the legacy ``resource_relationships`` store and into
``graph_edges``, so the bitemporal store holds the HISTORY the legacy table
structurally cannot express (its ``uq_resource_relationships`` constraint
permits exactly one row per ``(from, to, type)`` triple, forever).

Nothing is deleted here. ``resource_relationships`` keeps every row it has —
readers still serve it until P4 and the table is dropped in P5.

WHAT IS COPIED, AND WHY THAT SET
--------------------------------
An **allow-list**, not a deny-list of containment types. The set is exactly
*the relationship types a collector now DECLARES in the graph contract*
(``AgentSpec.emits_edges``): ``BELONGS_TO``, ``DEFINED_IN``, ``RUNS_IMAGE`` —
all three declared by ``agents/iac.py`` and written live by ``graph_engine``.

The allow-list shape is deliberate and the reason is stronger than "the rest
looks like containment":

* **An edge type nothing declares has no maintainer.** ``graph_engine`` only
  refreshes and (eventually) retires edges it can re-derive from a
  declaration. Backfilling ``MEMBER_OF`` / ``RUNS_EOL`` / ``ISSUED_BY`` /
  ``TRIGGERED_BY`` / ``HAS_SCHEDULE`` / ``HAS_VENDOR`` / ``HAS_CERTIFICATE``
  would mint rows that nothing will ever update, correct or close — permanent
  stale assertions wearing ``authority='auto'``. Those types stay in the
  legacy store until (and unless) a collector declares them.
* **A closed list cannot leak.** A new relationship type appearing in
  ``resource_relationships`` between now and P5 cannot silently enter the
  graph; it lands in ``skipped_type_not_declared`` and is counted.

Explicitly NOT copied, each counted separately in the printed report:

* **Containment / decomposition** (§3.1's modelling rule — facts *about* an
  entity stay attributes, not edges): ``HAS_DRIFT``, ``ON_FIELD``,
  ``HAS_ACCOUNT``, ``EXPOSES_PORT``, ``HAS_MOUNT``, ``HAS_CRON``,
  ``HAS_FIREWALL_RULE``, ``HAS_PENDING_UPDATE``, ``HAS_SOFTWARE``,
  ``TAGGED_AS`` and the rest of the ``HAS_*`` family. ~2,700 of the live
  2,863 rows. Their underlying detail rows are untouched and stay queryable.
* **``ANSIBLE_MANAGES``** (14 live rows) — still hand-written into the legacy
  store, blocked on the edge-OWNERSHIP decision recorded in the design doc's
  2026-08-12 addendum (TRK-354). Deliberately left where it is; migrating it
  now would pick the ownership answer by accident.

DUPLICATE-ACTIVE-EDGE POLICY (the load-bearing decision)
--------------------------------------------------------
``graph_engine`` already writes active ``BELONGS_TO`` / ``DEFINED_IN`` /
``RUNS_IMAGE`` edges. For those pairs the backfill's only value is the
PREHISTORY the engine never saw, so:

  If an ACTIVE engine edge exists for the resolved ``(source_node,
  target_node, edge_type)``, the legacy row is written as a **CLOSED
  historical edge** spanning ``[legacy.created_at, engine_edge.valid_from)``.

Why closed-prehistory rather than skip-or-overwrite:

* The partial unique index ``uq_graph_edges_active`` constrains only rows with
  ``valid_to IS NULL``. A closed row is unconstrained by construction, so this
  needs no exception and creates no duplicate ACTIVE edge.
* It is the honest bitemporal reading. ``valid_from``/``valid_to`` model when
  the relationship was true in the world; the legacy row is evidence it was
  true from ``created_at``, and the engine edge is evidence it is true from
  its own ``valid_from``. Writing the earlier interval is exactly the
  "supersede, don't overwrite" property §2.2 says the legacy table lacks.
* ``recorded_at`` stays ``now()`` — we learned this today, it was true then.
  That true-at / known-at split is the whole reason this store exists.
* Skipping instead would throw the history away, which is the one thing P3 is
  for. Retiring the engine's row and inserting an "active" legacy one would
  make a stale copy authoritative over a live derivation.

If ``legacy.created_at >= engine_edge.valid_from`` the interval is empty or
inverted, so there is no prehistory to preserve: the row is skipped and
counted as ``skipped_no_prehistory`` rather than written as a lie.

Where NO active engine edge exists for the pair, the edge is written with
``valid_from = min(created_at)`` and:

* ``valid_to = last_seen`` when the legacy row is not ``status='active'``;
* ``valid_to = min(endpoint retired_at)`` (clamped to ``>= valid_from``) when
  either endpoint ``resources`` row is retired — this preserves the deliberate
  divergence pinned by P2's tests, where the declarative path excludes retired
  rows. Re-introducing them as ACTIVE edges would silently undo that decision.
* ``valid_to = NULL`` (a genuinely active edge) otherwise.

MERGING: TWO LEGACY ROWS, ONE RELATIONSHIP
------------------------------------------
``cicd``'s L-8b rename created a second ``resources`` row per GitLab project
(130 retired + 138 live, sharing ``metadata.project_id``), so the legacy store
holds TWO ``BELONGS_TO`` rows per file — one to each. Both resolve to the SAME
``GitlabProject`` node, because the engine keys that node on the immutable
numeric id. They are therefore ONE relationship and are merged into one edge
with ``valid_from = MIN(created_at)`` (its true first observation, on the
retired row), ``confidence = MIN(confidence)`` (an edge is only as strong as
its weakest supporting evidence) and every contributing legacy row id recorded
in ``evidence``. That collapse is a demonstration of the point, not a
workaround: the legacy store could not express "same relationship, renamed
endpoint" at all.

NODE RESOLUTION
---------------
Endpoints are ``resources`` rows; ``graph_edges`` endpoints are
``graph_nodes`` rows. Resolution reuses ``graph_engine._emit_nodes``' identity
conventions **verbatim**, copied into ``NODE_CONVENTIONS`` below from the
``NodeSpec`` declarations in ``agents/iac.py`` and ``agents/cicd.py``, so a
resolved node UNIFIES with the engine-written one instead of duplicating it.
An endpoint whose ``(domain, type)`` has no declared convention is counted as
``skipped_unmappable_endpoint`` — never resolved by guesswork.

Node handling is **get-or-create, never update**: an existing node is used
as-is and nothing on it is touched. Unlike ``graph_phase2.upsert_node`` this
migration must not overwrite an engine-owned node's ``resource_id`` with a
RETIRED resource's id just because a legacy edge happened to point there.

SAFETY PROPERTIES
-----------------
* **Idempotent.** A backfilled edge is identified by
  ``source LIKE 'p3_backfill:%'`` on its ``(source_id, target_id, edge_type)``
  triple; a second run finds them all present and writes nothing
  (``skipped_already_backfilled``). Nodes are get-or-create.
* **Additive.** No UPDATE, no DELETE. ``resource_relationships`` is read-only
  here.
* **Bounded and logged.** The whole legacy store is ~2,863 rows; per-type
  disposition counts are printed so the deploy record shows exactly what
  happened. Every legacy row lands in exactly one disposition bucket.
* **Skips cleanly after P5.** If ``resource_relationships`` no longer exists
  (P5 dropped it), ``upgrade()`` is a no-op.
* **Runs under the 4s ``lock_timeout``** set in ``alembic/env.py``, inside the
  migration transaction.

``downgrade()`` is genuinely reversible for the rows this migration inserts —
see its docstring.
"""

# NOTE: deliberately NO ``from __future__ import annotations`` here. Alembic
# loads a version file by path WITHOUT registering it in ``sys.modules``
# (``alembic.util.pyfiles.load_module_py``), and ``@dataclass`` resolves STRING
# annotations through ``sys.modules[cls.__module__].__dict__`` — which is
# ``None`` for such a module, so the future import makes every dataclass below
# raise ``AttributeError`` at ``alembic upgrade`` time. Evaluated annotations
# avoid that lookup entirely. ``tests/test_p3_backfill.py`` loads this module
# the same way alembic does, so the regression cannot come back silently.
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8965b6329b94"
down_revision: Union[str, Sequence[str], None] = "adacc8308153"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Vocabulary — what is copied, what is refused, and why
# ---------------------------------------------------------------------------

#: Prefix stamped onto ``graph_edges.source`` so every row this migration
#: writes is identifiable (and so ``downgrade()`` can remove exactly them).
BACKFILL_SOURCE_PREFIX = "p3_backfill:"

#: The allow-list: relationship types a collector DECLARES in the graph
#: contract today, and which therefore have a live emitter that will keep
#: maintaining them. Adding a type here without a matching declaration would
#: mint edges nothing can ever refresh or retire.
MIGRATABLE_TYPES = frozenset({"BELONGS_TO", "DEFINED_IN", "RUNS_IMAGE"})

#: Hand-written into the legacy store; blocked on the edge-OWNERSHIP decision
#: (design-doc addendum / TRK-354). Counted, never copied.
DEFERRED_TYPES = frozenset({"ANSIBLE_MANAGES"})

#: Containment / decomposition per §3.1 — a fact ABOUT an entity, not a
#: relationship BETWEEN two independently-referrable entities. Enumerated
#: rather than pattern-matched on ``HAS_`` so the refusal is a decision on
#: record instead of a naming coincidence.
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

# Dispositions. Every legacy row lands in exactly one of these.
MIGRATED_HISTORY = "migrated_as_history"
MIGRATED_ACTIVE = "migrated_as_active"
MIGRATED_CLOSED = "migrated_as_closed"
MERGED = "merged_into_earlier_row"
SKIPPED_ALREADY = "skipped_already_backfilled"
SKIPPED_NO_PREHISTORY = "skipped_no_prehistory"
SKIPPED_CONTAINMENT = "skipped_containment"
SKIPPED_DEFERRED = "skipped_ansible_manages_deferred"
SKIPPED_UNDECLARED = "skipped_type_not_declared"
SKIPPED_UNMAPPABLE = "skipped_unmappable_endpoint"


# ---------------------------------------------------------------------------
# Node identity conventions — copied verbatim from the NodeSpec declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeConvention:
    """How ``graph_engine`` names the node for one ``(domain, resource_type)``.

    Mirrors ``NodeSpec``: ``natural_key`` / ``name`` / ``attributes`` /
    ``resource_backed``. Frozen here rather than imported so this migration
    cannot change meaning when a future ``NodeSpec`` edit lands — a migration
    must keep doing what it did the day it ran.
    """

    node_type: str
    #: ``"name"`` = ``resources.name``; ``"metadata:<k>"`` = ``metadata[k]``.
    key_from: str
    #: ``graph_nodes.source`` — the DOMAIN that declares the node, which is not
    #: always the domain of the endpoint resource (``ContainerImage`` rows live
    #: in the ``container`` domain but are declared and owned by ``iac``).
    source: str
    resource_backed: bool
    #: ``resources.metadata`` keys copied onto the node, per ``NodeSpec``.
    attribute_keys: tuple[str, ...] = ()


#: (resources.domain, resources.type) -> convention.
#:
#: iac file classifications: ``agents/iac.py::_IAC_FILE_NODE_TYPES`` +
#: ``_IAC_FILE_NODES`` (natural_key="name", attributes=(project_id, file_path,
#: ref)).
#: GitlabProject: ``agents/cicd.py`` (natural_key="metadata.project_id",
#: name="name", attributes=(project_id, default_branch,
#: last_pipeline_status)).
#: ContainerImage: ``agents/iac.py::_CONTAINER_IMAGE_NODE`` — a SHARED VALUE
#: node keyed on the image string with ``resource_backed=False``, so the legacy
#: ``container/container_image`` resource (whose ``name`` IS that string) maps
#: onto it by value and carries no ``resource_id``.
NODE_CONVENTIONS: dict[tuple[str, str], NodeConvention] = {
    ("iac", "gitlab_ci_pipeline"): NodeConvention(
        "GitlabCiFile", "name", "iac", True, ("project_id", "file_path", "ref")
    ),
    ("iac", "ansible_playbook"): NodeConvention(
        "AnsiblePlaybook", "name", "iac", True, ("project_id", "file_path", "ref")
    ),
    ("iac", "ansible_inventory_file"): NodeConvention(
        "AnsibleInventoryFile", "name", "iac", True, ("project_id", "file_path", "ref")
    ),
    ("iac", "ansible_requirements"): NodeConvention(
        "AnsibleRequirements", "name", "iac", True, ("project_id", "file_path", "ref")
    ),
    ("iac", "docker_compose"): NodeConvention(
        "DockerComposeFile", "name", "iac", True, ("project_id", "file_path", "ref")
    ),
    ("iac", "k8s_manifest"): NodeConvention(
        "K8sManifest", "name", "iac", True, ("project_id", "file_path", "ref")
    ),
    ("iac", "terraform_file"): NodeConvention(
        "TerraformFile", "name", "iac", True, ("project_id", "file_path", "ref")
    ),
    ("cicd", "gitlab_project"): NodeConvention(
        "GitlabProject",
        "metadata:project_id",
        "cicd",
        True,
        ("project_id", "default_branch", "last_pipeline_status"),
    ),
    ("container", "container_image"): NodeConvention("ContainerImage", "name", "iac", False, ()),
}


# ---------------------------------------------------------------------------
# Pure planning layer (no database access — see tests/test_p3_backfill.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceRow:
    """One endpoint of a legacy edge, as read from ``resources``."""

    id: str
    domain: str
    type: str
    name: str
    retired_at: Optional[datetime]
    metadata: dict[str, Any] = field(default_factory=dict)


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
    attributes: Optional[dict[str, Any]]

    @property
    def identity(self) -> tuple[str, str]:
        return (self.node_type, self.natural_key)


@dataclass(frozen=True)
class PlannedEdge:
    source_identity: tuple[str, str]
    target_identity: tuple[str, str]
    edge_type: str
    method: str
    confidence: Decimal
    source: str
    evidence: dict[str, Any]
    valid_from: datetime
    valid_to: Optional[datetime]
    disposition: str


@dataclass
class BackfillPlan:
    nodes: list[PlannedNode] = field(default_factory=list)
    edges: list[PlannedEdge] = field(default_factory=list)
    #: relationship_type -> disposition -> count
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def count(self, relationship_type: str, disposition: str, n: int = 1) -> None:
        self.counts.setdefault(relationship_type, {}).setdefault(disposition, 0)
        self.counts[relationship_type][disposition] += n

    def total(self) -> int:
        return sum(sum(d.values()) for d in self.counts.values())


def classify(relationship_type: str) -> str:
    """Bucket a legacy relationship type. Allow-list first, refusals after."""
    if relationship_type in MIGRATABLE_TYPES:
        return "migrate"
    if relationship_type in DEFERRED_TYPES:
        return SKIPPED_DEFERRED
    if relationship_type in CONTAINMENT_TYPES:
        return SKIPPED_CONTAINMENT
    return SKIPPED_UNDECLARED


def node_for(resource: ResourceRow) -> Optional[PlannedNode]:
    """Resolve a ``resources`` row to its ``graph_nodes`` identity + payload.

    Returns ``None`` when the endpoint's ``(domain, type)`` has no declared
    convention — the caller must count and skip, never guess a key.
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
        # ``_emit_nodes`` skips a row with no stable identity rather than
        # collapsing every such row onto one blank-keyed node. Same here.
        return None
    blob = resource.metadata or {}
    attributes: Optional[dict[str, Any]]
    if convention.resource_backed:
        attributes = {k: blob[k] for k in convention.attribute_keys if blob.get(k) is not None}
        attributes = attributes or None
    else:
        # Shared value node: ``_emit_child_row_nodes`` stores the child row it
        # was built from, i.e. {"image": "<value>"} for ContainerImage.
        attributes = {"image": natural_key} if convention.node_type == "ContainerImage" else None
    return PlannedNode(
        node_type=convention.node_type,
        natural_key=natural_key[:512],
        name=(resource.name or natural_key)[:256],
        source=convention.source,
        resource_id=resource.id if convention.resource_backed else None,
        attributes=attributes,
    )


def _method_for(confidence: float) -> str:
    """Derive ``method`` from the legacy row's confidence.

    ``resource_relationships`` has no ``method`` column (§2.2 — that missing
    provenance is one of the reasons it is being retired), so it has to be
    derived, and the derivation is pinned by ``graph_phase2.upsert_edge``'s
    honesty rule: 1.000 is legal ONLY for ``declared``. A legacy row claiming
    1.000 is therefore recorded as ``declared``; anything below it as
    ``deterministic_match``, the weakest claim that cannot be contradicted by
    the confidence it carries.
    """
    return "declared" if Decimal(str(confidence)) >= Decimal("1.000") else "deterministic_match"


def _quantize(confidence: float) -> Decimal:
    """Clamp to [0, 1.000] at ``Numeric(4, 3)`` precision."""
    value = Decimal(str(confidence)).quantize(Decimal("0.001"))
    if value < Decimal("0"):
        return Decimal("0.000")
    if value > Decimal("1.000"):
        return Decimal("1.000")
    return value


def plan_backfill(
    rows: Sequence[LegacyRow],
    *,
    active_edges: dict[tuple[tuple[str, str], tuple[str, str], str], datetime],
    existing_backfill: set[tuple[tuple[str, str], tuple[str, str], str]],
    existing_nodes: set[tuple[str, str]],
) -> BackfillPlan:
    """Decide, without touching a database, what P3 should write.

    ``active_edges`` maps a resolved ``(source_identity, target_identity,
    edge_type)`` triple to the ACTIVE engine edge's ``valid_from``.
    ``existing_backfill`` is the same triple for edges this migration has
    already written (idempotence). ``existing_nodes`` is every
    ``(node_type, natural_key)`` already in ``graph_nodes``.
    """
    plan = BackfillPlan()
    groups: dict[
        tuple[tuple[str, str], tuple[str, str], str],
        list[tuple[LegacyRow, PlannedNode, PlannedNode]],
    ] = {}

    for row in rows:
        verdict = classify(row.relationship_type)
        if verdict != "migrate":
            plan.count(row.relationship_type, verdict)
            continue
        src = node_for(row.from_resource)
        tgt = node_for(row.to_resource)
        if src is None or tgt is None:
            plan.count(row.relationship_type, SKIPPED_UNMAPPABLE)
            continue
        triple = (src.identity, tgt.identity, row.relationship_type)
        if triple in existing_backfill:
            plan.count(row.relationship_type, SKIPPED_ALREADY)
            continue
        groups.setdefault(triple, []).append((row, src, tgt))

    planned_node_identities: set[tuple[str, str]] = set()

    for triple in sorted(groups, key=lambda t: (t[2], t[0], t[1])):
        members = sorted(groups[triple], key=lambda m: (m[0].created_at, m[0].id))
        rel_type = triple[2]
        primary = members[0][0]
        valid_from = min(m[0].created_at for m in members)
        engine_valid_from = active_edges.get(triple)

        if engine_valid_from is not None:
            if valid_from >= engine_valid_from:
                # No interval to preserve — the engine already covers it.
                plan.count(rel_type, SKIPPED_NO_PREHISTORY, len(members))
                continue
            valid_to: Optional[datetime] = engine_valid_from
            disposition = MIGRATED_HISTORY
        else:
            valid_to = _closing_time(members, valid_from)
            disposition = MIGRATED_CLOSED if valid_to is not None else MIGRATED_ACTIVE

        confidence = min(_quantize(m[0].confidence) for m in members)
        sources = sorted({m[0].source for m in members})
        legacy_sources = "+".join(sources)
        evidence: dict[str, Any] = {
            "backfill": "p3",
            "legacy_table": "resource_relationships",
            "legacy_relationship_ids": [m[0].id for m in members],
            "legacy_sources": sources,
            "legacy_status": sorted({m[0].status for m in members}),
            "legacy_created_at": [m[0].created_at.isoformat() for m in members],
            "legacy_last_seen": max(m[0].last_seen for m in members).isoformat(),
            "legacy_from_resource_id": primary.from_resource.id,
            "legacy_to_resource_id": primary.to_resource.id,
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
                source=(BACKFILL_SOURCE_PREFIX + legacy_sources)[:128],
                evidence=evidence,
                valid_from=valid_from,
                valid_to=valid_to,
                disposition=disposition,
            )
        )
        plan.count(rel_type, disposition)
        if len(members) > 1:
            plan.count(rel_type, MERGED, len(members) - 1)

        # Both endpoints must exist as nodes before the edge can be inserted.
        for candidate in _endpoint_nodes(members):
            if candidate.identity in existing_nodes:
                continue
            if candidate.identity in planned_node_identities:
                continue
            planned_node_identities.add(candidate.identity)
            plan.nodes.append(candidate)

    return plan


def _endpoint_nodes(
    members: Sequence[tuple[LegacyRow, PlannedNode, PlannedNode]],
) -> list[PlannedNode]:
    """Pick one node payload per endpoint identity across the merged rows.

    Prefers the member whose backing ``resources`` row is NOT retired, so a
    minted node's ``resource_id`` points at the live row rather than the
    renamed-away one.
    """
    chosen: dict[tuple[str, str], tuple[int, PlannedNode]] = {}
    for row, src, tgt in members:
        for node, resource in ((src, row.from_resource), (tgt, row.to_resource)):
            rank = 1 if resource.retired_at is None else 0
            current = chosen.get(node.identity)
            if current is None or rank > current[0]:
                chosen[node.identity] = (rank, node)
    return [node for _, node in chosen.values()]


def _closing_time(
    members: Sequence[tuple[LegacyRow, PlannedNode, PlannedNode]],
    valid_from: datetime,
) -> Optional[datetime]:
    """When did this relationship stop being true, if it did?

    ``None`` means it is still true and the edge is written ACTIVE.
    """
    candidates: list[datetime] = []
    for row, _src, _tgt in members:
        if row.status != "active":
            candidates.append(row.last_seen)
        for endpoint in (row.from_resource, row.to_resource):
            if endpoint.retired_at is not None:
                candidates.append(endpoint.retired_at)
    if not candidates:
        return None
    # A relationship ends when its FIRST endpoint goes away, and can never end
    # before it started.
    return max(min(candidates), valid_from)


def format_report(counts: dict[str, dict[str, int]]) -> list[str]:
    """One stable, greppable line per (relationship_type, disposition)."""
    lines: list[str] = []
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


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_param(bind: Any, name: str) -> str:
    """Bind a JSON parameter, cast on PostgreSQL where the column is JSONB."""
    if bind.dialect.name == "postgresql":
        return f"CAST(:{name} AS JSONB)"
    return f":{name}"


def upgrade() -> None:
    """Copy the declared genuine relationships into ``graph_edges``."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "resource_relationships" not in tables:
        # P5 already dropped the legacy store; there is nothing to backfill.
        print(f"[{revision}] resource_relationships absent — nothing to backfill")
        return
    if not {"graph_nodes", "graph_edges"} <= tables:
        print(f"[{revision}] graph tables absent — nothing to backfill")
        return

    rows: list[LegacyRow] = []
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

    node_id_by_identity: dict[tuple[str, str], str] = {}
    for n in bind.execute(_SELECT_NODES):
        node_id_by_identity[(n[1], n[2])] = n[0]

    active_edges: dict[tuple[tuple[str, str], tuple[str, str], str], datetime] = {}
    existing_backfill: set[tuple[tuple[str, str], tuple[str, str], str]] = set()
    for e in bind.execute(_SELECT_EDGES).mappings():
        triple = ((e["s_type"], e["s_key"]), (e["t_type"], e["t_key"]), e["edge_type"])
        if e["valid_to"] is None:
            active_edges[triple] = e["valid_from"]
        if str(e["source"] or "").startswith(BACKFILL_SOURCE_PREFIX):
            existing_backfill.add(triple)

    plan = plan_backfill(
        rows,
        active_edges=active_edges,
        existing_backfill=existing_backfill,
        existing_nodes=set(node_id_by_identity),
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
        f"[{revision}] P3 backfill: read {plan.total()} resource_relationships row(s); "
        f"wrote {len(plan.nodes)} graph_nodes and {len(plan.edges)} graph_edges"
    )
    for line in format_report(plan.counts):
        print(f"[{revision}]{line}")


_DELETE_BACKFILL = sa.text("DELETE FROM graph_edges WHERE source LIKE :prefix")


def downgrade() -> None:
    """Delete exactly the edges ``upgrade()`` inserted — genuinely reversible.

    Every backfilled row carries ``source = 'p3_backfill:<legacy source>'``, a
    marker no emitter writes, so the DELETE is precise: it cannot touch an
    engine-written edge and it restores ``graph_edges`` to its pre-migration
    contents. ``resource_relationships`` was never modified, so the data itself
    is not lost by downgrading — it is still in the legacy store.

    Nodes are deliberately NOT deleted. Two reasons, both about damage:

    1. A backfilled node is INDISTINGUISHABLE from an engine-written one by
       design — unification is the requirement, so there is no safe marker to
       select on without polluting the node payload.
    2. ``graph_edges``' FKs are ``ON DELETE CASCADE``. If the engine has since
       written edges against a node this migration minted (which is the
       expected outcome — the engine adopts it on its next pass), deleting the
       node would silently destroy engine-written edges. A leftover node is an
       entity assertion that is either true or harmless; a cascaded edge delete
       is neither.

    On the live database this is moot: every endpoint already resolves to an
    engine-written node, so ``upgrade()`` mints zero nodes there.
    """
    result = op.get_bind().execute(_DELETE_BACKFILL, {"prefix": f"{BACKFILL_SOURCE_PREFIX}%"})
    print(f"[{revision}] removed {result.rowcount} p3-backfilled graph_edges row(s)")
