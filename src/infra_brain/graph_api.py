"""Graph traversal API for the Infra Brain knowledge graph.

ONE STORE (graph-first P5)
--------------------------
Every route here now reads the ``graph_nodes`` / ``graph_edges`` bitemporal
store (keyed by ``graph_nodes.id``) written by ``graph_engine`` from the
declarative ``etl.spec`` contracts, plus the hand-written Phase 2/3 emitters.

The four routes that read the ORIGINAL ``resource_relationships`` table —
``/relationships``, ``/search``, ``/{resource_id}``, and ``/stats``' legacy
body — are GONE. P4 froze that table and labelled it in the response; P5's job
is to leave it with zero readers so the migration can drop it. The one thing
kept is the ``/stats`` PATH: see its handler for why it aliases rather than
410s.

``graph_nodes.resource_id`` remains the explicit bridge to ``resources.id``
when a caller wants to cross into the resource id space, and it is exposed on
every ``/kg/*`` node so the client can follow it. The two id spaces are still
deliberately NOT unioned into one response shape — both are UUIDs and would
silently interleave into an unresolvable node set.

Read-only GET endpoints, plus three admin-gated writes (TRK-227, TRK-228):

  GET /api/graph/stats
      Edge counts grouped by type, descending — the same QUESTION the legacy
      route answered, now answered from graph_edges. Alias of
      /api/graph/kg/stats' edge_types, in the original PageEnvelope shape.

  GET /api/graph/kg/stats?active_only=true
      Node counts by node_type and edge counts by edge_type over the
      graph_nodes/graph_edges store. Active edges (valid_to IS NULL) only by
      default — a retired edge is history, not current state.

  GET /api/graph/kg/search?q=<fragment>&type=LinuxHost&limit=25
      Fuzzy match over graph_nodes.name AND graph_nodes.natural_key, returning
      candidates the caller follows up with GET /api/graph/kg/{node_id}.

  GET /api/graph/kg/edges?type=RUNS_ON&limit=200&after_id=<edge id>
      Flat, name-resolved edge list over graph_edges — the kg counterpart of
      the removed /relationships, for callers that want an edge census rather
      than a neighbourhood walk (the dashboard's home-page GRAPH panel).

  GET /api/graph/kg/{node_id}?depth=2&max_nodes=200&active_only=true
      The subgraph around one graph node, as nodes + edges the dashboard's
      canvas draws directly. Capped, and reports node_total/edge_total/
      truncated so a capped answer is never silently truncated.

  GET /api/graph/blast-radius/{node_id}?max_hops=2&min_confidence=1.0&top_n=25
      "What else is affected if this entity breaks?" — capped, ranked
      traversal. Delegates to graph_phase3.blast_radius, the same read-only
      function backing the get_blast_radius MCP tool (mcp_server.py). node_id
      is a graph_nodes.id UUID.

  GET /api/graph/entity-resolution/queue?domain=vsphere
      The cross-source entity-resolution review queue: one row per
      ambiguous identity question graph_phase3.resolve_entities queued for
      a human instead of auto-emitting. Thin wrapper over
      graph_phase3.get_reconciliation_state.

  POST /api/graph/entity-resolution/{action_id}/confirm  (admin-gated WRITE)
      {target_node_id}: human confirmation that action_id's source node and
      target_node_id (one of that row's own candidate_matches — no other
      node_id is accepted) are the same entity. Unlike every other
      ProposedAction type, there is no agent that ever executes an approved
      entity_resolution_same_as row, so this endpoint calls
      graph_phase3.confirm_same_as directly and commits in the same
      request — approving IS executing here, not deferred to a later run.
      Writes a permanent declared/1.000 SAME_AS edge pair. The generic
      POST /api/dashboard/actions/{id}/approve (governance_ops.py) refuses
      this action_type with 409, pointing here, so there is exactly one
      path that can resolve a review-queue row as approved.

  POST /api/graph/entity-resolution/{action_id}/reject  (admin-gated WRITE)
      {target_node_id?, reason?}: the mirror of /confirm. A rejection is a
      named human's assertion that a specific PAIR is NOT the same entity,
      so it writes an attributed, pair-scoped, retractable NOT_SAME_AS veto
      that every automatic identity emitter consults before emitting -- NOT
      the bare status flip the generic /api/dashboard/actions/{id}/reject
      performs, which is unattributed, bundle-scoped, invisible to the
      emitters, and used to silence the source node forever (KG-3). That
      generic route now 409s this action_type and points here, exactly as
      the generic approve route already did.

  POST /api/graph/entity-resolution/{action_id}/retract  (admin-gated WRITE)
      {reason?}: undo a mistaken confirmation. action_id must be the SAME
      row the confirm route resolved (status='approved') — the pairing to
      undo is read from that row's own source_node/confirmed_target_node_id
      stamp, never from caller input, so a caller cannot retract an
      arbitrary edge by supplying its own node ids. Closes the validity
      interval on both directed SAME_AS edges (never DELETE — the
      historical record survives in each edge's evidence) and reopens the
      review-queue row to 'pending'.

This router no longer has a single-segment wildcard route (the legacy
``/{resource_id}`` ego-network was removed with the store it read), so the
"named routes must come first" ordering hazard that used to govern this file is
gone. New routes are still added under an explicit path segment rather than a
wildcard, so it cannot come back.

Every route requires a session (require_session); the two write routes above
additionally require an admin session (require_admin). Every OTHER route in
this module is strictly read-only.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from infra_brain import graph_kg
from infra_brain.api._envelope import PageEnvelope
from infra_brain.dashboard_auth import current_user, require_admin, require_session
from infra_brain.db.models import GraphEdge as GraphEdgeRow
from infra_brain.db.models import GraphNode as GraphNodeRow
from infra_brain.db.models import ProposedAction
from infra_brain.db.session import get_session
from infra_brain.graph_phase3 import MAX_HOPS, MAX_TOP_N, REVIEW_ACTION_TYPE
from infra_brain.graph_phase3 import blast_radius as _blast_radius_walk
from infra_brain.graph_phase3 import confirm_same_as as _confirm_same_as
from infra_brain.graph_phase3 import get_reconciliation_state as _get_reconciliation_state
from infra_brain.graph_phase3 import reject_same_as as _reject_same_as
from infra_brain.graph_phase3 import retract_same_as as _retract_same_as

graph_router = APIRouter(
    prefix="/api/graph",
    tags=["graph"],
    dependencies=[Depends(require_session)],
)

# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class StatRow(BaseModel):
    type: str
    count: int


class GraphStatsPageOut(PageEnvelope):
    """``/api/graph/stats``' envelope, unchanged in SHAPE across the P5 cut.

    P4 added ``frozen``/``deprecated``/``frozen_reason`` so a client could
    label the legacy store without hard-coding the migration policy. P5 removes
    them, because the sentence they carried ("this store is retained until it
    is dropped") is no longer true of the data behind this route — it is
    ``graph_edges`` now, which is not frozen and is not going anywhere. Leaving
    a `frozen: true` flag on live data would be worse than never having had it.
    """

    items: list[StatRow]


class BlastRadiusRootOut(BaseModel):
    node_id: str
    node_type: str
    name: str
    resource_id: str | None = None


class BlastRadiusNodeOut(BaseModel):
    node_id: str
    node_type: str
    name: str
    natural_key: str | None = None
    source: str
    resource_id: str | None = None


class BlastRadiusResourceOut(BaseModel):
    resource_id: str
    name: str
    domain: str
    type: str


class BlastRadiusNeighborOut(BaseModel):
    # Exactly one of node/resource is populated depending on which edge store
    # (graph_edges vs. the older resource_relationships) contributed the
    # shortest path to this neighbour — mirrors graph_phase3.blast_radius'
    # raw dict shape, kept as separate optional fields rather than a union so
    # RelationshipMiniGraph (dashboard-app) can read a stable node/resource
    # shape either way.
    node: BlastRadiusNodeOut | None = None
    resource: BlastRadiusResourceOut | None = None
    edge_type: str
    hop_distance: int
    confidence: float
    why: str


class BlastRadiusOut(BaseModel):
    root: BlastRadiusRootOut
    hops: int
    min_confidence: float
    count: int
    total_found: int
    truncated: bool
    neighbors: list[BlastRadiusNeighborOut]


class ReconciliationSourceNodeOut(BaseModel):
    node_id: str
    node_type: str
    natural_key: str
    name: str
    source: str


class ReconciliationCandidateOut(BaseModel):
    node_id: str
    node_type: str
    natural_key: str
    name: str
    source: str
    score: float
    reason: str
    # GitLab issue #168: hard-identifier corroboration + conflict signals a
    # reviewer needs to correctly weigh candidate strength -- a single queued
    # candidate is NOT itself high confidence (see confidence_band).
    evidence: dict[str, Any] = {}
    counter_evidence: list[str] = []
    confidence_band: str = "fuzzy_review"
    # Other pending-review action targets proposing the SAME node as their
    # candidate -- confirming one implicitly rules those out.
    mutually_exclusive: list[str] = []


class RetractionHistoryEntryOut(BaseModel):
    confirmed_target_node_id: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    retracted_by: str
    retracted_at: str
    reason: str | None = None


class ReconciliationRowOut(BaseModel):
    action_id: str
    source_node: ReconciliationSourceNodeOut
    candidate_matches: list[ReconciliationCandidateOut]
    # GitLab issue #168: explicit replacement for reading candidate COUNT as a
    # confidence signal -- "1 candidate" means the weaker "nothing else
    # fuzzy-matched" case, not high confidence. Pair with confidence_band.
    candidates_to_disambiguate: int = 0
    confidence_band: str | None = None
    status: str
    best_score: float
    approved_by: str | None = None
    created_at: str | None = None
    retraction_history: list[RetractionHistoryEntryOut] = []


class ReconciliationQueueOut(BaseModel):
    items: list[ReconciliationRowOut]
    total: int


class ConfirmSameAsBody(BaseModel):
    target_node_id: str


class ConfirmSameAsNodeOut(BaseModel):
    node_id: str
    node_type: str
    name: str


class ConfirmSameAsOut(BaseModel):
    confirmed: bool
    edge_ids: list[str]
    source_node: ConfirmSameAsNodeOut
    target_node: ConfirmSameAsNodeOut
    method: str
    confidence: float
    approver: str
    review_resolved: bool
    # True when this confirmation reversed a colleague's active rejection.
    veto_overridden: bool = False


class RejectSameAsBody(BaseModel):
    # Omit to reject EVERY candidate this row lists (each gets its own
    # attributed veto). Supply one candidate's node_id to reject just that
    # pair and leave the question open on the rest.
    target_node_id: str | None = None
    reason: str | None = None


class RejectedPairOut(BaseModel):
    node_id: str
    natural_key: str
    name: str
    rejected_evidence_class: str


class RejectSameAsOut(BaseModel):
    rejected: bool
    action_id: str
    status: str
    rejector: str
    vetoed: list[RejectedPairOut]
    candidates_remaining: int


class RetractSameAsBody(BaseModel):
    reason: str | None = None


class RetractSameAsOut(BaseModel):
    retracted: bool
    edge_ids: list[str]
    retractor: str
    review_reopened: bool


# --- graph_nodes / graph_edges store (the /kg/* routes) ---------------------


class KgNodeOut(BaseModel):
    """One ``graph_nodes`` row.

    ``id`` is a ``graph_nodes.id``, NOT a ``resources.id`` — see the module
    docstring's two-stores note. ``resource_id`` is the explicit bridge back
    into the legacy id space (nullable: shared value nodes such as a CVE have
    no single owning resource).
    """

    id: str
    #: ``graph_nodes.node_type`` — a free per-source string by contract
    #: ("LinuxHost", "HomelabService", "GitlabProject", …), deliberately not a
    #: closed enum (see etl.spec.NodeSpec).
    type: str
    name: str
    natural_key: str
    #: Which collector/domain contributed the node.
    source: str
    resource_id: str | None = None
    attributes: dict[str, Any] = {}
    first_seen: str | None = None
    last_seen: str | None = None


class KgEdgeOut(BaseModel):
    """One ``graph_edges`` row, in the direction it is STORED.

    ``valid_to`` is surfaced rather than filtered away silently: a client that
    asked for ``active_only=false`` needs to be able to tell a live edge from a
    retired one, and a client that did not is entitled to see the field is null.
    """

    id: str
    source_id: str
    target_id: str
    edge_type: str
    method: str
    confidence: float
    authority: str
    source: str
    evidence: dict[str, Any] = {}
    valid_from: str | None = None
    #: NULL = active. Non-null = the edge was retired at this instant.
    valid_to: str | None = None


class KgStatRow(BaseModel):
    type: str
    count: int


class KgStatsOut(BaseModel):
    node_types: list[KgStatRow]
    edge_types: list[KgStatRow]
    total_nodes: int
    total_edges: int
    active_only: bool


class KgSearchOut(BaseModel):
    candidates: list[KgNodeOut]
    #: Full match count BEFORE ``limit`` — so a UI can honestly say
    #: "showing 25 of 312" rather than implying it showed everything.
    total: int
    limit: int


class KgEdgeRow(KgEdgeOut):
    """A flat edge row with both endpoints NAMED as well as identified.

    The removed ``/relationships`` route joined its two endpoint ids back to
    ``resources.name`` for exactly this reason: an edge list whose rows are two
    UUIDs is unreadable, and a client that has to fetch every node separately to
    render a list is doing N+1 over HTTP. Names are carried ALONGSIDE the ids,
    never instead of them — an id stays the addressable thing, a name stays a
    label (DESIGN.md §5).
    """

    source_name: str = ""
    target_name: str = ""


class KgEdgeListOut(PageEnvelope):
    items: list[KgEdgeRow]
    active_only: bool = True


class KgGraphOut(BaseModel):
    root_id: str
    nodes: list[KgNodeOut]
    edges: list[KgEdgeOut]
    #: Nodes/edges the walk actually FOUND, before ``max_nodes`` was applied.
    #: ``len(nodes) < node_total`` is the whole point: the client renders
    #: "showing N of M" instead of quietly drawing a partial graph.
    node_total: int
    edge_total: int
    truncated: bool
    depth: int
    active_only: bool
    #: True when the walk stopped early at the defensive whole-store ceiling
    #: (``_KG_WALK_CEILING``) rather than because it ran out of graph. Then
    #: ``node_total`` is itself a floor, not the true reachable count, and the
    #: client must not present it as one.
    walk_ceiling_hit: bool = False


# ---------------------------------------------------------------------------
# graph_nodes / graph_edges helpers (the /kg/* routes)
#
# The traversal, the caps and the JSON coercion all live in infra_brain.graph_kg
# now, because the chat tools walk the same store and must not have to import a
# FastAPI router (with its auth dependencies and Pydantic models) to do it.
# There is exactly ONE walk over graph_edges; these are the HTTP-shaped
# projections of it.
# ---------------------------------------------------------------------------

_KG_MAX_DEPTH = graph_kg.MAX_DEPTH
_KG_DEFAULT_MAX_NODES = graph_kg.DEFAULT_MAX_NODES
_KG_MAX_MAX_NODES = graph_kg.MAX_MAX_NODES
_KG_WALK_CEILING = graph_kg.WALK_CEILING
_chunks = graph_kg.chunks
_kg_iso = graph_kg.iso
_kg_blob = graph_kg.blob
_kg_walk = graph_kg.walk


def _kg_node_out(row: GraphNodeRow) -> KgNodeOut:
    return KgNodeOut(
        id=str(row.id),
        type=row.node_type,
        name=row.name,
        natural_key=row.natural_key,
        source=row.source,
        resource_id=str(row.resource_id) if row.resource_id else None,
        attributes=_kg_blob(row.attributes),
        first_seen=_kg_iso(row.first_seen),
        last_seen=_kg_iso(row.last_seen),
    )


def _kg_edge_out(row: GraphEdgeRow) -> KgEdgeOut:
    return KgEdgeOut(
        id=str(row.id),
        source_id=str(row.source_id),
        target_id=str(row.target_id),
        edge_type=row.edge_type,
        method=row.method,
        confidence=float(row.confidence),
        authority=row.authority,
        source=row.source,
        evidence=_kg_blob(row.evidence),
        valid_from=_kg_iso(row.valid_from),
        valid_to=_kg_iso(row.valid_to),
    )


# Endpoints
# ---------------------------------------------------------------------------


@graph_router.get("/stats", response_model=GraphStatsPageOut)
def graph_stats():
    """Edge counts grouped by type, descending — from ``graph_edges``.

    WHY THIS PATH SURVIVED THE P5 CUT (and its three siblings did not)

    ``/relationships``, ``/search`` and ``/{resource_id}`` were shaped by the
    legacy store: their response bodies name ``from_resource_id`` /
    ``to_resource_id`` / ``relationship_type`` and are keyed in the
    ``resources.id`` space. Re-pointing them at ``graph_edges`` would have kept
    the URL while changing the id space underneath it — the worst of both, a
    silent lie to any client that stored an id. They were removed, and
    ``/kg/{search,edges,<node_id>}`` are their honestly-renamed replacements.

    ``/stats`` is different: its contract is a QUESTION ("how many edges of
    each type does this system know?"), and its response body — ``{type,
    count}`` — carries no id space at all. The question outlives the table, so
    the path does too, now answered from ``graph_edges``. That is why this
    aliases rather than 410s: a 410 would delete the only stable path for a
    question that is still perfectly well-posed, and force every caller to
    learn the ``/kg/*`` vocabulary to ask it. A 307 to ``/kg/stats`` was
    rejected for the opposite reason — that route returns ``KgStatsOut`` (node
    types AND edge types, no PageEnvelope), so a redirect would hand followers
    a different response SHAPE while claiming to be the same resource.

    Active edges only (``valid_to IS NULL``): a retired edge is history, and
    counting it here would inflate "what does the graph know" with things the
    graph has explicitly stopped asserting. ``/kg/stats?active_only=false`` is
    where the full bitemporal census lives.
    """
    with get_session() as s:
        rows = s.execute(
            select(GraphEdgeRow.edge_type, func.count())
            .where(GraphEdgeRow.valid_to.is_(None))
            .group_by(GraphEdgeRow.edge_type)
            .order_by(func.count().desc(), GraphEdgeRow.edge_type)
        ).all()
    items = [StatRow(type=r[0], count=r[1]) for r in rows]
    total = len(items)
    return GraphStatsPageOut(items=items, total=total, limit=total, offset=0)


@graph_router.get("/blast-radius/{node_id}", response_model=BlastRadiusOut)
def blast_radius(
    node_id: str,
    max_hops: int = Query(
        2, ge=1, le=MAX_HOPS, description=f"Hop cap (clamped server-side to {MAX_HOPS})"
    ),
    min_confidence: float = Query(
        1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Declared edges only by default (1.0). Lower to 0.99 for deterministic "
            "name matches, 0.8 for fuzzy identity links — a blast-radius answer "
            "decides what to touch during an incident, so weaker links must be "
            "opted into rather than assumed."
        ),
    ),
    top_n: int = Query(
        25, ge=1, le=MAX_TOP_N, description=f"Result cap (clamped server-side to {MAX_TOP_N})"
    ),
):
    """ "What else is affected if this entity breaks?" — read-only.

    ``node_id`` is a ``graph_nodes.id`` UUID (Phase 2/3 store), NOT a
    ``resources.id`` — see the module docstring. Thin wrapper over
    ``graph_phase3.blast_radius``, the same function backing the
    ``get_blast_radius`` MCP tool: walks both edge stores (``graph_edges``
    from the node, and the older ``resource_relationships`` from its
    ``resource_id`` when it has one), dedupes neighbours to the shortest hop
    distance, and ranks by (hop asc, confidence desc, name).

    Returns 404 for an unknown node_id, 422 for a malformed UUID.
    """
    try:
        nid = uuid.UUID(node_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="node_id must be a valid UUID")

    with get_session() as s:
        result = _blast_radius_walk(
            s, nid, max_hops=max_hops, min_confidence=min_confidence, top_n=top_n
        )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@graph_router.get("/entity-resolution/queue", response_model=ReconciliationQueueOut)
def entity_resolution_queue(
    domain: str | None = Query(
        None,
        description=(
            "Filter on the SOURCE node's source field (vsphere/rapid7/ansible/octopus). "
            "Omit for all domains."
        ),
    ),
):
    """The cross-source entity-resolution review queue (TRK-226).

    Thin read-only wrapper over ``graph_phase3.get_reconciliation_state`` — one
    row per unresolved identity question Phase 3's fuzzy-matching pass queued
    for a human instead of auto-emitting (ambiguous key, or a fuzzy score in
    the [0.75, 0.90) review band). Pending rows first, then most recent.
    """
    with get_session() as s:
        rows = _get_reconciliation_state(s, domain=domain)
    return ReconciliationQueueOut(items=rows, total=len(rows))


@graph_router.post(
    "/entity-resolution/{action_id}/confirm",
    response_model=ConfirmSameAsOut,
    dependencies=[Depends(require_admin)],
)
def confirm_entity_resolution(
    action_id: str,
    body: ConfirmSameAsBody,
    request: Request,
):
    """Confirm one candidate match from the review queue as the SAME entity.

    Elevated write (admin session required, mirroring the existing
    ``/api/dashboard/actions/{id}/approve`` gate): unlike a ProposedAction's
    generic approve/reject flow (status flip only, execution deferred to an
    agent's next run), there is no agent that ever re-checks
    ``entity_resolution_same_as`` rows, so this endpoint calls
    ``graph_phase3.confirm_same_as`` directly and commits — approving IS
    executing here. Writes a confirmed ``SAME_AS`` edge pair at the
    ``declared``/1.000 confidence reserved for an accountable human
    assertion, and resolves the matching review-queue row if one exists.

    ``action_id`` must be a pending row's UUID from
    ``GET /entity-resolution/queue`` — used only to look up the row's
    ``source_node_id``; the actual match is ``source_node_id`` (from that
    row) confirmed against ``body.target_node_id`` (the human-picked
    candidate). Returns 404 if the action is unknown, 409 if it is not
    pending, 422 for a malformed target_node_id or a refused pairing
    (unknown node, same node, same source type — see
    ``confirm_same_as``'s own validation).
    """
    try:
        act_id = uuid.UUID(action_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="action_id must be a valid UUID")
    try:
        target_id = uuid.UUID(body.target_node_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="target_node_id must be a valid UUID")

    user = current_user(request) or {}
    approver = user.get("username") or "dashboard"

    with get_session() as s:
        # with_for_update=True: without a row lock here, two concurrent confirms
        # of the same action_id (double-click, two tabs) can both read
        # status="pending" before either commits. If they carry different
        # target_node_id values, both could succeed and write two contradictory
        # SAME_AS edges from the same source node -- undermining the
        # "accountable human assertion" invariant confirm_same_as documents.
        action = s.get(ProposedAction, act_id, with_for_update=True)
        if action is None or action.action_type != REVIEW_ACTION_TYPE:
            raise HTTPException(status_code=404, detail="review-queue action not found")
        if action.status != "pending":
            raise HTTPException(status_code=409, detail=f"action is {action.status}, not pending")
        source_node_id_raw = (action.payload or {}).get("source_node", {}).get("node_id")
        if not source_node_id_raw:
            raise HTTPException(status_code=422, detail="action has no source_node.node_id")
        try:
            source_node_uuid = uuid.UUID(source_node_id_raw)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422, detail="action has a malformed source_node.node_id"
            )

        # The human may only confirm ONE of the candidates Phase 3 itself
        # ranked and queued for this question — never an arbitrary node.
        # Without this, action_id degrades to a mere pending-state gate and
        # a caller (or a UI bug picking the wrong row) could write a
        # permanent declared/1.000 SAME_AS edge between two entities the
        # resolver never proposed as a match.
        candidate_ids = {
            c.get("node_id") for c in (action.payload or {}).get("candidate_matches", [])
        }
        if body.target_node_id not in candidate_ids:
            raise HTTPException(
                status_code=422,
                detail="target_node_id is not one of this action's candidate_matches",
            )

        result = _confirm_same_as(s, source_node_uuid, target_id, approver)
        if "error" in result:
            raise HTTPException(status_code=422, detail=result["error"])
        s.commit()
    return result


@graph_router.post(
    "/entity-resolution/{action_id}/reject",
    response_model=RejectSameAsOut,
    dependencies=[Depends(require_admin)],
)
def reject_entity_resolution(
    action_id: str,
    body: RejectSameAsBody,
    request: Request,
):
    """Reject one candidate — or every listed candidate — as NOT the same entity.

    Elevated write (admin session required, the same gate and the same bar as
    confirm). The exact mirror of ``confirm_entity_resolution``, and it exists
    for the same reason: approving IS executing for this action_type, and so is
    rejecting. A rejection here writes an attributed, pair-scoped, retractable
    ``NOT_SAME_AS`` veto that every automatic identity emitter consults before
    it emits — not the bare ProposedAction status flip the generic
    ``POST /api/dashboard/actions/{id}/reject`` performs, which is
    unattributed, bundle-scoped, invisible to the emitters, and silenced the
    source node forever (KG-3). That generic route now 409s this action_type
    and points here, mirroring what the generic approve route already did.

    ``target_node_id`` (optional) must be one of this row's own
    ``candidate_matches``. Supplying it rejects just that pair and leaves the
    row ``pending`` while other candidates remain; omitting it rejects every
    listed candidate — each with its own attributed veto, because the operator
    did look at all of them — and flips the row to ``rejected``.

    Returns 404 if the action is unknown, 409 if it is not pending, 422 for a
    malformed target_node_id or a refused rejection (see ``reject_same_as``'s
    own validation).
    """
    try:
        act_id = uuid.UUID(action_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="action_id must be a valid UUID")
    target_id: uuid.UUID | None = None
    if body.target_node_id is not None:
        try:
            target_id = uuid.UUID(body.target_node_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="target_node_id must be a valid UUID")

    user = current_user(request) or {}
    rejector = user.get("username") or "dashboard"

    with get_session() as s:
        # with_for_update: same race-safety reason as confirm/retract -- two
        # concurrent rejections (or a rejection racing a confirmation) must not
        # both act on a stale read of this row's candidate list.
        action = s.get(ProposedAction, act_id, with_for_update=True)
        if action is None or action.action_type != REVIEW_ACTION_TYPE:
            raise HTTPException(status_code=404, detail="review-queue action not found")
        if action.status != "pending":
            raise HTTPException(status_code=409, detail=f"action is {action.status}, not pending")

        result = _reject_same_as(s, act_id, rejector, target_node_id=target_id, reason=body.reason)
        if "error" in result:
            raise HTTPException(status_code=422, detail=result["error"])
        s.commit()
    return result


@graph_router.post(
    "/entity-resolution/{action_id}/retract",
    response_model=RetractSameAsOut,
    dependencies=[Depends(require_admin)],
)
def retract_entity_resolution(
    action_id: str,
    body: RetractSameAsBody,
    request: Request,
):
    """Undo a mistaken confirmation (see ``confirm_entity_resolution``).

    Elevated write (admin session required, same gate as confirm). Closes the
    validity interval on both directed ``SAME_AS`` edges — never DELETE, the
    historical record (who confirmed it, who retracted it, when, and why) is
    preserved in each edge's evidence — and reopens the review-queue row to
    ``pending`` so the identity question can be re-decided.

    ``action_id`` must be the SAME row `confirm_entity_resolution` resolved
    (``status='approved'``, ``entity_resolution_same_as``) — the pairing to
    undo is read from that row's own ``source_node.node_id`` and
    ``confirmed_target_node_id`` (stamped by the confirm step; nothing else
    records which candidate was picked once a row leaves ``pending``), never
    from caller input. Returns 404 if the action is unknown, 409 if it is not
    an already-approved review row, 422 if the confirmation stamp is missing
    or malformed, or the underlying edge/state has already changed (e.g. no
    active edge remains — see ``retract_same_as``'s own validation).
    """
    try:
        act_id = uuid.UUID(action_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="action_id must be a valid UUID")

    user = current_user(request) or {}
    retractor = user.get("username") or "dashboard"

    with get_session() as s:
        # with_for_update=True: same race-safety reason as the confirm route --
        # a concurrent retract (or a retract racing a fresh confirm on the same
        # row) must not both act on a stale read of this action's state.
        action = s.get(ProposedAction, act_id, with_for_update=True)
        if action is None or action.action_type != REVIEW_ACTION_TYPE:
            raise HTTPException(status_code=404, detail="review-queue action not found")
        if action.status != "approved":
            raise HTTPException(
                status_code=409,
                detail=f"action is {action.status}, not approved -- nothing to retract",
            )
        payload = action.payload or {}
        source_node_id_raw = payload.get("source_node", {}).get("node_id")
        target_node_id_raw = payload.get("confirmed_target_node_id")
        if not source_node_id_raw or not target_node_id_raw:
            raise HTTPException(
                status_code=422,
                detail="action has no confirmed_target_node_id -- was it confirmed through "
                "this same API rather than the MCP tool directly, or before this field existed?",
            )
        try:
            source_node_uuid = uuid.UUID(source_node_id_raw)
            target_node_uuid = uuid.UUID(target_node_id_raw)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="action has a malformed confirmed pairing")

        result = _retract_same_as(
            s, source_node_uuid, target_node_uuid, retractor, reason=body.reason
        )
        if "error" in result:
            raise HTTPException(status_code=422, detail=result["error"])
        s.commit()
    return result


# ---------------------------------------------------------------------------
# graph_nodes / graph_edges store — /api/graph/kg/*
#
# These sit before the /{resource_id} wildcard for the same reason every other
# named route does. (They are two-segment paths so the single-segment wildcard
# could not swallow them anyway, but the ordering rule in this module is a
# stated invariant, not an accident to be relied on.)
# ---------------------------------------------------------------------------


@graph_router.get("/kg/stats", response_model=KgStatsOut)
def kg_stats(
    active_only: bool = Query(
        True,
        description=(
            "Count only edges with valid_to IS NULL (the current state of the "
            "estate). Set false to include retired edges, i.e. the full "
            "bitemporal history."
        ),
    ),
):
    """Node counts by node_type and edge counts by edge_type, descending.

    The graph-store counterpart of ``/api/graph/stats``. Kept as a SEPARATE
    route rather than a mode on that one: the two read different tables in
    different id spaces, and a shared route with a `?store=` switch would make
    every caller's response shape conditional on a query param.
    """
    with get_session() as s:
        node_rows = s.execute(
            select(GraphNodeRow.node_type, func.count())
            .group_by(GraphNodeRow.node_type)
            .order_by(func.count().desc(), GraphNodeRow.node_type)
        ).all()
        edge_stmt = select(GraphEdgeRow.edge_type, func.count())
        if active_only:
            edge_stmt = edge_stmt.where(GraphEdgeRow.valid_to.is_(None))
        edge_rows = s.execute(
            edge_stmt.group_by(GraphEdgeRow.edge_type).order_by(
                func.count().desc(), GraphEdgeRow.edge_type
            )
        ).all()

    node_types = [KgStatRow(type=r[0], count=r[1]) for r in node_rows]
    edge_types = [KgStatRow(type=r[0], count=r[1]) for r in edge_rows]
    return KgStatsOut(
        node_types=node_types,
        edge_types=edge_types,
        total_nodes=sum(r.count for r in node_types),
        total_edges=sum(r.count for r in edge_types),
        active_only=active_only,
    )


@graph_router.get("/kg/search", response_model=KgSearchOut)
def kg_search(
    q: str = Query("", description="Case-insensitive fragment of name OR natural_key"),
    type: str | None = Query(None, description="Filter by node_type, e.g. LinuxHost"),
    limit: int = Query(25, ge=1, le=200),
):
    """Find graph nodes to start a traversal from.

    Matches ``name`` OR ``natural_key``, because they routinely differ: a
    ``HomelabService``'s name is the bare service ("litellm") while its
    natural_key is "<host>/<service>", so searching for the HOST would miss
    every service running on it if only names were matched.

    Unlike ``/api/graph/search``, this never auto-expands a single match into a
    neighbourhood — it always returns candidates. The one-match shortcut on the
    legacy route means a caller cannot tell "I found exactly one" from "I found
    a neighbourhood", and the client here always needs the node id anyway in
    order to call ``/kg/{node_id}``.

    ``total`` is the FULL match count before ``limit``, so the caller can say
    how many it is not showing.
    """
    pattern = f"%{q.lower().strip()}%"
    with get_session() as s:
        base = select(GraphNodeRow).where(
            or_(
                func.lower(GraphNodeRow.name).like(pattern),
                func.lower(GraphNodeRow.natural_key).like(pattern),
            )
        )
        if type:
            base = base.where(GraphNodeRow.node_type == type)
        total = int(s.execute(select(func.count()).select_from(base.subquery())).scalar() or 0)
        rows = (
            s.execute(base.order_by(GraphNodeRow.name, GraphNodeRow.natural_key).limit(limit))
            .scalars()
            .all()
        )
        candidates = [_kg_node_out(r) for r in rows]
    return KgSearchOut(candidates=candidates, total=total, limit=limit)


@graph_router.get("/kg/edges", response_model=KgEdgeListOut)
def kg_edges(
    type: str | None = Query(None, description="Filter by edge_type, e.g. RUNS_ON"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    after_id: str | None = Query(
        None,
        description=(
            "Keyset cursor (edge id from the last item of the previous page). "
            "When set, this takes over from `offset` and paginates via "
            "`id > after_id` instead of COUNT+OFFSET — use this for deep "
            "pagination, where OFFSET degrades linearly with page depth."
        ),
    ),
    min_confidence: float = Query(
        0.0,
        ge=0.0,
        le=1.0,
        description="Only return edges with confidence >= this threshold.",
    ),
    active_only: bool = Query(
        True, description="Only edges with valid_to IS NULL (the current state)."
    ),
):
    """Flat edge census over ``graph_edges``, both endpoints named.

    The KG counterpart of the removed ``/api/graph/relationships``. It exists
    because "list me edges" and "walk me a neighbourhood" are genuinely
    different questions: ``/kg/{node_id}`` needs a root, and a caller that has
    no root yet — the dashboard home page's GRAPH panel picks its own hub by
    degree from an unfiltered page — cannot ask it. Removing ``/relationships``
    without this would have left that panel permanently empty rather than
    re-pointed, which is the failure mode the P5 consumer pass exists to avoid.

    Both pagination modes of the route it replaces are preserved: COUNT+OFFSET
    by default, keyset via ``after_id``. They are mutually exclusive, not
    interchangeable mid-run: keyset mode orders by ``id`` (that is what makes
    ``id > after_id`` a valid page boundary) while offset mode orders by
    ``(edge_type, source name, id)`` for readability. Switching to keyset from
    an offset page hands it a cursor that is not the ordering key and silently
    skips rows — pick one and use it from the first page.

    Deliberately NOT preserved: the
    ``rr.from_resource_id <> rr.to_resource_id`` self-loop filter that route
    carried — ``graph_edges`` rejects self-edges at write time, so filtering
    here would be dead code implying a hazard that no longer exists.
    """
    src = aliased(GraphNodeRow)
    dst = aliased(GraphNodeRow)

    def _scoped(stmt):
        if type:
            stmt = stmt.where(GraphEdgeRow.edge_type == type)
        if active_only:
            stmt = stmt.where(GraphEdgeRow.valid_to.is_(None))
        if min_confidence > 0:
            # Decimal, not float: `confidence` is NUMERIC(4,3), and binding a
            # float against it leans on an implicit cast that differs between
            # PostgreSQL and the sqlite test dialect. Same coercion graph_kg's
            # walk uses, for the same reason.
            stmt = stmt.where(GraphEdgeRow.confidence >= Decimal(str(min_confidence)))
        return stmt

    with get_session() as s:
        total = int(
            s.execute(_scoped(select(func.count()).select_from(GraphEdgeRow))).scalar() or 0
        )
        base = _scoped(
            select(GraphEdgeRow, src.name, dst.name)
            .join(src, src.id == GraphEdgeRow.source_id)
            .join(dst, dst.id == GraphEdgeRow.target_id)
        )
        if after_id is not None:
            try:
                cursor = uuid.UUID(after_id)
            except ValueError:
                raise HTTPException(status_code=422, detail="after_id must be a valid UUID")
            base = base.where(GraphEdgeRow.id > cursor).order_by(GraphEdgeRow.id).limit(limit)
        else:
            base = (
                base.order_by(GraphEdgeRow.edge_type, src.name, GraphEdgeRow.id)
                .limit(limit)
                .offset(offset)
            )
        rows = s.execute(base).all()

    items = [
        KgEdgeRow(
            **_kg_edge_out(edge).model_dump(), source_name=sname or "", target_name=tname or ""
        )
        for edge, sname, tname in rows
    ]
    return KgEdgeListOut(
        items=items, total=total, limit=limit, offset=offset, active_only=active_only
    )


@graph_router.get("/kg/{node_id}", response_model=KgGraphOut)
def kg_neighborhood(
    node_id: str,
    depth: int = Query(
        2,
        ge=1,
        le=_KG_MAX_DEPTH,
        description=(
            f"Hops to walk (max {_KG_MAX_DEPTH}). REJECTED with 422 above the "
            "max rather than silently clamped, unlike the legacy "
            "/api/graph/{resource_id} route — a caller who asked for more "
            "should be told it isn't happening."
        ),
    ),
    max_nodes: int = Query(
        _KG_DEFAULT_MAX_NODES,
        ge=1,
        le=_KG_MAX_MAX_NODES,
        description=(
            "Cap on RETURNED nodes. The response always reports node_total / "
            "edge_total / truncated, so a capped answer is visibly capped."
        ),
    ),
    active_only: bool = Query(
        True, description="Only edges with valid_to IS NULL (the current state)."
    ),
    min_confidence: float = Query(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Drop edges (and any node reached only through one) below this "
            "confidence. 1.0 = declared edges only; 0.99 also admits "
            "deterministic name matches; 0.8 also admits fuzzy identity links."
        ),
    ),
):
    """The subgraph around one graph node — what the dashboard canvas draws.

    ``node_id`` is a ``graph_nodes.id`` (get one from ``/kg/search``), NOT a
    ``resources.id``. 404 for an unknown node, 422 for a malformed UUID.

    Nodes come back in BFS order (root, then hop 1, then hop 2), so the
    ``max_nodes`` cap keeps the NEAREST nodes to what was asked about and can
    never drop the root itself. Edges are then confined to the returned node
    set — an edge whose other end was capped away would render as an arrow to
    nowhere.
    """
    try:
        nid = uuid.UUID(node_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="node_id must be a valid UUID")

    with get_session() as s:
        root = s.get(GraphNodeRow, nid)
        if root is None:
            raise HTTPException(status_code=404, detail=f"graph node {node_id} not found")

        order, edge_map, ceiling_hit = _kg_walk(s, nid, depth, active_only, min_confidence)
        kept_ids = order[:max_nodes]
        kept = set(kept_ids)

        rows: dict[uuid.UUID, GraphNodeRow] = {}
        for chunk in _chunks(kept_ids):
            for row in s.execute(select(GraphNodeRow).where(GraphNodeRow.id.in_(chunk))).scalars():
                rows[row.id] = row
        # Preserve BFS order; a node id with no row is a referential
        # impossibility (FK CASCADE) but skipping beats a KeyError in a route.
        nodes = [_kg_node_out(rows[i]) for i in kept_ids if i in rows]
        edges = [
            _kg_edge_out(e)
            for e in edge_map.values()
            if e.source_id in kept and e.target_id in kept
        ]

    return KgGraphOut(
        root_id=str(nid),
        nodes=nodes,
        edges=edges,
        node_total=len(order),
        edge_total=len(edge_map),
        truncated=len(order) > len(kept_ids),
        depth=depth,
        active_only=active_only,
        walk_ceiling_hit=ceiling_hit,
    )
