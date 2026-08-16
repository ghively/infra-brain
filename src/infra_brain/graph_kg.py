"""Read-side helpers for the ``graph_nodes`` / ``graph_edges`` store.

WHY THIS MODULE EXISTS
----------------------
The iterative BFS behind ``GET /api/graph/kg/{node_id}`` was written inside
``graph_api.py`` because that route was its only caller. It no longer is:
graph-first P5 re-points the chat tools off the legacy ``resource_relationships``
recursive CTE and onto this store, and a chat tool must not have to import a
FastAPI router module (with its auth dependencies and Pydantic response models)
to walk a graph.

So the traversal moved here verbatim and ``graph_api`` imports it back. There is
exactly ONE walk implementation over ``graph_edges`` — the route and the chat
tool cannot drift into answering the same question two different ways, which is
the failure mode the two-store era produced in the first place.

WHAT THIS STORE DOES AND DOES NOT HOLD
--------------------------------------
It holds RELATIONSHIPS BETWEEN ENTITIES, each with recorded provenance
(``method``/``confidence``) and a validity interval: ``RUNS_ON`` (service→host),
``BELONGS_TO`` / ``DEFINED_IN`` (iac file↔project), ``RUNS_IMAGE``, and the
identity edges.

It does NOT hold CONTAINMENT FACTS. A host's packages, systemd units, listening
ports, users and crons are rows in the per-domain detail tables
(``linux_packages``, ``linux_services``, …) keyed by a foreign key — they were
never entities with their own identity, and the declarative contract
(``etl.spec``) deliberately does not manufacture one for them. Callers that want
"what runs on host X" need the RUNS_ON edges from here AND the detail-table
facts; ``linux_service_facts`` below answers the cheap half of that so a
consumer does not have to know the schema. Anything richer belongs in the tools
(``query_linux_detail``), not here.

Every function is READ-ONLY: SELECTs only, no session.commit(), no writes.
"""

from __future__ import annotations

import json as _json
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select

from infra_brain.db.models import GraphEdge as GraphEdgeRow
from infra_brain.db.models import GraphEdgeType
from infra_brain.db.models import GraphNode as GraphNodeRow

# ---------------------------------------------------------------------------
# Caps. These are the values the /kg/{node_id} route has always enforced; they
# live here now so the chat tool inherits the identical ceilings rather than
# inventing its own.
# ---------------------------------------------------------------------------

#: Hop ceiling. Callers that accept a user-supplied depth should REJECT above
#: this rather than silently clamp — a caller who asked for depth=99 and got 4
#: without being told is the same class of quiet lie as an unannounced
#: truncation. (The chat tool clamps instead, and says so in its output, because
#: an LLM tool call has no 422 channel.)
MAX_DEPTH = 4

#: Default and maximum node cap for one neighbourhood response.
DEFAULT_MAX_NODES = 200
MAX_MAX_NODES = 2000

#: Defensive whole-walk ceiling, independent of ``max_nodes``. ``max_nodes``
#: bounds what is RETURNED; this bounds what is VISITED, so a depth-4 walk from
#: a high-fan-in node (a CVE shared by every asset, say) cannot pull the whole
#: store into memory just to compute a total the client will only see as "of M".
#: Hitting it is reported (``walk_ceiling_hit``) rather than silently capping
#: the total.
WALK_CEILING = 5000

#: SQLite caps a statement at 999 bound parameters; chunk every IN () list well
#: under that so a wide BFS frontier doesn't blow up on the sqlite test suite
#: (and stays a sane statement size on PostgreSQL).
IN_CHUNK = 400


def chunks(values: list, size: int = IN_CHUNK):
    for i in range(0, len(values), size):
        yield values[i : i + size]


def iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def blob(value: Any) -> dict[str, Any]:
    """Coerce a JSONB column to a dict (SQLite may hand back a JSON string)."""
    if isinstance(value, str):
        try:
            value = _json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


def walk_multi(
    session: Any,
    root_ids: list[uuid.UUID],
    depth: int,
    active_only: bool = True,
    min_confidence: float = 0.0,
) -> tuple[list[uuid.UUID], dict[uuid.UUID, GraphEdgeRow], bool, dict[uuid.UUID, uuid.UUID]]:
    """Breadth-first walk over ``graph_edges`` from ONE OR MORE roots.

    Returns ``(node_ids, edges, ceiling_hit, edge_origin)``.

    ``node_ids`` is in BFS order — every root first, then hop 1, then hop 2 —
    which is what makes ``max_nodes`` truncation meaningful downstream: the
    nodes kept are the ones NEAREST what the user asked about, and no root can
    be truncated away.

    MULTI-ROOT, not single-root, because an identity cluster is ONE logical
    entity (see :func:`identity_cluster`). Seeding every member of the cluster
    at hop 0 is what makes the identity hop free: the caller's ``depth`` is
    spent on real relationships, never on stepping between two rows that both
    denote the same physical machine.

    ``edge_origin`` maps each edge id to the ROOT whose BFS branch first
    reached it. That is the provenance an operator needs after a merge: it is
    the difference between "the Ansible collector recorded this" and "the Linux
    collector recorded this", and losing it would make a merged answer
    unauditable.

    An ITERATIVE walk (one query per hop) rather than a recursive CTE: the
    legacy ``get_neighborhood`` used a PostgreSQL-only recursive CTE and was
    stubbed out in the test suite as a result, which meant its traversal was
    never actually exercised. With ``depth <= 4`` this is at most four queries,
    and it runs identically on SQLite and PostgreSQL so the behaviour under
    test is the behaviour in production.

    Edges are collected UNDIRECTED (an edge is followed from either end) —
    "what runs on this host" and "what does this service run on" are the same
    question asked from opposite ends, and a directed-only walk would answer
    only one of them. The stored direction is preserved in the response, so the
    arrow on the canvas stays honest.

    ``NOT_SAME_AS`` is EXCLUDED outright, matching
    ``graph_phase3._graph_edge_walk``: it is a negative assertion ("a human
    says these are definitely different machines"), not connectivity. Walking
    it would draw a relationship between exactly the two entities a human took
    the trouble to say are unrelated.
    """
    seen_order: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    origin: dict[uuid.UUID, uuid.UUID] = {}
    for rid in root_ids:
        if rid in seen:
            continue
        seen.add(rid)
        seen_order.append(rid)
        origin[rid] = rid
    frontier: list[uuid.UUID] = list(seen_order)
    edges: dict[uuid.UUID, GraphEdgeRow] = {}
    edge_origin: dict[uuid.UUID, uuid.UUID] = {}
    ceiling_hit = False

    for _ in range(depth):
        if not frontier or ceiling_hit:
            break
        rows: list[GraphEdgeRow] = []
        for chunk in chunks(frontier):
            stmt = select(GraphEdgeRow).where(
                or_(GraphEdgeRow.source_id.in_(chunk), GraphEdgeRow.target_id.in_(chunk)),
                GraphEdgeRow.edge_type != GraphEdgeType.NOT_SAME_AS.value,
            )
            if active_only:
                stmt = stmt.where(GraphEdgeRow.valid_to.is_(None))
            if min_confidence > 0:
                stmt = stmt.where(GraphEdgeRow.confidence >= Decimal(str(min_confidence)))
            rows.extend(session.execute(stmt).scalars().all())

        next_frontier: list[uuid.UUID] = []
        for edge in rows:
            edges[edge.id] = edge
            # Whichever endpoint we arrived FROM already carries an origin; the
            # far endpoint inherits it. First writer wins, so an edge reachable
            # from two members is credited to the one that found it first —
            # which is also the one BFS order lists first.
            known = edge.source_id if edge.source_id in origin else edge.target_id
            edge_origin.setdefault(edge.id, origin.get(known, root_ids[0]))
            for nid in (edge.source_id, edge.target_id):
                if nid in seen:
                    continue
                if len(seen) >= WALK_CEILING:
                    ceiling_hit = True
                    continue
                seen.add(nid)
                seen_order.append(nid)
                origin.setdefault(nid, origin.get(known, root_ids[0]))
                next_frontier.append(nid)
        frontier = next_frontier

    # An edge discovered on the final hop can point at a node the ceiling
    # refused to admit. Drop it rather than return an edge to a node that is
    # not in the graph at any cap.
    edges = {eid: e for eid, e in edges.items() if e.source_id in seen and e.target_id in seen}
    edge_origin = {eid: o for eid, o in edge_origin.items() if eid in edges}
    return seen_order, edges, ceiling_hit, edge_origin


def walk(
    session: Any,
    root_id: uuid.UUID,
    depth: int,
    active_only: bool = True,
    min_confidence: float = 0.0,
) -> tuple[list[uuid.UUID], dict[uuid.UUID, GraphEdgeRow], bool]:
    """Single-root :func:`walk_multi`, minus the origin map.

    Kept as the shape ``graph_api._kg_walk`` has always called, so the HTTP
    ``/kg/{node_id}`` route (which is node-id addressed — the caller already
    picked exactly which row they meant) is unchanged apart from inheriting the
    ``NOT_SAME_AS`` exclusion, which was always the intended semantic.
    """
    order, edges, ceiling_hit, _origin = walk_multi(
        session, [root_id], depth, active_only, min_confidence
    )
    return order, edges, ceiling_hit


# ---------------------------------------------------------------------------
# Identity: turning several per-source rows back into ONE machine
# ---------------------------------------------------------------------------


def _pair_edges(
    session: Any, left: list[uuid.UUID], right: list[uuid.UUID], edge_type: str
) -> list[GraphEdgeRow]:
    """ACTIVE edges of ``edge_type`` between the two id sets, either direction."""
    if not left or not right:
        return []
    rows: list[GraphEdgeRow] = []
    for lchunk in chunks(left):
        for rchunk in chunks(right):
            stmt = select(GraphEdgeRow).where(
                GraphEdgeRow.edge_type == edge_type,
                GraphEdgeRow.valid_to.is_(None),
                or_(
                    and_(
                        GraphEdgeRow.source_id.in_(lchunk),
                        GraphEdgeRow.target_id.in_(rchunk),
                    ),
                    and_(
                        GraphEdgeRow.source_id.in_(rchunk),
                        GraphEdgeRow.target_id.in_(lchunk),
                    ),
                ),
            )
            rows.extend(session.execute(stmt).scalars().all())
    return rows


def identity_cluster(
    session: Any,
    seed_ids: list[uuid.UUID],
    min_confidence: float = 0.0,
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Transitive closure over active ``SAME_AS`` — ONE machine's rows.

    Returns ``(members, vetoed)``. ``members`` starts with ``seed_ids`` in the
    order given (so the caller's chosen root stays first and can never be
    truncated away) followed by every node reachable through active ``SAME_AS``
    edges. ``vetoed`` are the nodes a ``SAME_AS`` edge pointed at that were
    REFUSED admission by a human veto — reported rather than silently dropped,
    because "we could have merged this and a human said no" is information an
    operator wants.

    WHY THIS EXISTS
    ---------------
    ``SAME_AS`` is the whole point of the identity model: a fact collected by
    one source has to be reachable when you ask about the same machine as seen
    by another. Per-source node rows are deliberately never merged in the store
    (Phase 2's "never a unified Host" rule), so the merge has to happen at READ
    time, here, once — not in each consumer, and not in a prompt.

    TWO RULES THAT ARE NOT NEGOTIABLE
    ---------------------------------
    1. **``NOT_SAME_AS`` is never merged across.** The closure is transitive,
       so ``A --SAME_AS-- B --SAME_AS-- C`` would otherwise merge ``A`` and
       ``C`` even when a human has recorded an explicit ``A``/``C`` veto —
       laundering a human "no" through two machine "yes"es. A candidate is
       admitted only if it carries no ACTIVE ``NOT_SAME_AS`` against ANY node
       already in the cluster. (``NOT_SAME_AS`` is also excluded from the
       neighbourhood walk itself; see :func:`walk_multi`.)
    2. **Only ACTIVE ``SAME_AS`` merges**, regardless of any caller's
       ``active_only=False`` request for history. A retired identity edge is a
       claim we have since withdrawn; re-merging on it would resurrect a
       decision the bitemporal store exists to record as over. Likewise the
       veto lookup is always active-only — a retracted veto is not a veto.

    ``min_confidence`` applies to the identity edges too, so a caller who wants
    declared-only identity (the discipline ``graph_phase3.blast_radius``
    defaults to) can ask for it. The default 0.0 merges on any active
    ``SAME_AS``, which is what "the resolver asserted these are the same
    machine" means.
    """
    members: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for sid in seed_ids:
        if sid not in seen:
            seen.add(sid)
            members.append(sid)
    vetoed: list[uuid.UUID] = []
    vetoed_seen: set[uuid.UUID] = set()

    frontier = list(members)
    while frontier:
        rows: list[GraphEdgeRow] = []
        for chunk in chunks(frontier):
            stmt = select(GraphEdgeRow).where(
                GraphEdgeRow.edge_type == GraphEdgeType.SAME_AS.value,
                GraphEdgeRow.valid_to.is_(None),
                or_(GraphEdgeRow.source_id.in_(chunk), GraphEdgeRow.target_id.in_(chunk)),
            )
            if min_confidence > 0:
                stmt = stmt.where(GraphEdgeRow.confidence >= Decimal(str(min_confidence)))
            rows.extend(session.execute(stmt).scalars().all())

        candidates: list[uuid.UUID] = []
        cand_seen: set[uuid.UUID] = set()
        for edge in rows:
            for nid in (edge.source_id, edge.target_id):
                if nid in seen or nid in cand_seen:
                    continue
                cand_seen.add(nid)
                candidates.append(nid)
        if not candidates:
            break

        # One query for the whole candidate batch: any active human veto
        # between a candidate and an existing member disqualifies it.
        blocked = {
            e.source_id if e.source_id in cand_seen else e.target_id
            for e in _pair_edges(session, members, candidates, GraphEdgeType.NOT_SAME_AS.value)
        }

        next_frontier: list[uuid.UUID] = []
        for nid in candidates:
            if nid in blocked:
                if nid not in vetoed_seen:
                    vetoed_seen.add(nid)
                    vetoed.append(nid)
                continue
            seen.add(nid)
            members.append(nid)
            next_frontier.append(nid)
        frontier = next_frontier

    return members, vetoed


def group_by_identity(
    session: Any, nodes: list[GraphNodeRow], min_confidence: float = 0.0
) -> list[list[GraphNodeRow]]:
    """Partition resolved candidates into identity clusters, order preserved.

    This is what turns "``node_a`` matched 2 rows" into the right question.
    Rows joined by ``SAME_AS`` are ONE entity and there is nothing to
    disambiguate; rows that are NOT joined are a genuine name collision the
    caller has to choose between. Collapsing both cases into "walked the first"
    is what produced the wrong answer this module now prevents.

    The first group contains ``nodes[0]``, so the caller's primary root is
    stable.
    """
    groups: list[list[GraphNodeRow]] = []
    assigned: set[uuid.UUID] = set()
    for node in nodes:
        if node.id in assigned:
            continue
        members, _vetoed = identity_cluster(session, [node.id], min_confidence=min_confidence)
        member_set = set(members)
        group = [n for n in nodes if n.id in member_set and n.id not in assigned]
        assigned.update(n.id for n in group)
        groups.append(group)
    return groups


# ---------------------------------------------------------------------------
# Identifier resolution
# ---------------------------------------------------------------------------


def resolve_roots(session: Any, ident: str, limit: int = 10) -> list[GraphNodeRow]:
    """Resolve a caller-supplied identifier to candidate ``graph_nodes`` rows.

    Accepts, in order of specificity:

    1. a ``graph_nodes.id`` UUID — the store's own id space;
    2. a ``resources.id`` UUID — resolved through ``graph_nodes.resource_id``,
       the documented bridge between the two id spaces. One resources row can
       back several nodes (per-source node types are deliberately NOT merged),
       so this can legitimately return more than one;
    3. an exact (case-insensitive) ``name`` or ``natural_key``;
    4. failing all of the above, a substring match on either.

    The name forms matter more than they look: an LLM never holds a UUID —
    ``query_resources`` returns hostnames — so a graph tool that only accepted
    ids was unreachable from chat in practice.

    Returns ``[]`` for no match. The caller decides whether one-of-many is an
    answer or an ambiguity to report.
    """
    ident = (ident or "").strip()
    if not ident:
        return []

    try:
        as_uuid = uuid.UUID(ident)
    except (ValueError, AttributeError, TypeError):
        as_uuid = None

    if as_uuid is not None:
        node = session.get(GraphNodeRow, as_uuid)
        if node is not None:
            return [node]
        bridged = (
            session.execute(
                select(GraphNodeRow)
                .where(GraphNodeRow.resource_id == as_uuid)
                .order_by(GraphNodeRow.node_type, GraphNodeRow.natural_key)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return list(bridged)

    lowered = ident.lower()
    exact = (
        session.execute(
            select(GraphNodeRow)
            .where(
                or_(
                    func.lower(GraphNodeRow.name) == lowered,
                    func.lower(GraphNodeRow.natural_key) == lowered,
                )
            )
            .order_by(GraphNodeRow.node_type, GraphNodeRow.natural_key)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    if exact:
        return list(exact)

    pattern = f"%{lowered}%"
    fuzzy = (
        session.execute(
            select(GraphNodeRow)
            .where(
                or_(
                    func.lower(GraphNodeRow.name).like(pattern),
                    func.lower(GraphNodeRow.natural_key).like(pattern),
                )
            )
            .order_by(GraphNodeRow.name, GraphNodeRow.natural_key)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(fuzzy)


# ---------------------------------------------------------------------------
# Plain-dict neighbourhood (the shape non-HTTP consumers want)
# ---------------------------------------------------------------------------


def node_dict(row: GraphNodeRow) -> dict[str, Any]:
    return {
        "node_id": str(row.id),
        "type": row.node_type,
        "name": row.name,
        "natural_key": row.natural_key,
        "source": row.source,
        "resource_id": str(row.resource_id) if row.resource_id else None,
    }


def member_label(row: GraphNodeRow) -> str:
    """``name (NodeType)`` — how one member of an identity cluster is named.

    The type is not decoration: after a merge, two members share a name by
    construction, so the type is the only thing that tells an operator whether
    a fact came in from Ansible's row or the Linux collector's.
    """
    return f"{row.name or row.natural_key} ({row.node_type})"


def edge_dict(
    row: GraphEdgeRow, names: dict[uuid.UUID, str], via: str | None = None
) -> dict[str, Any]:
    """One edge, with both endpoints named.

    Names are carried alongside the ids rather than instead of them: an id is
    never presented as if it were a name, and a name is never presented as if
    it were addressable.

    ``via`` — present only on a merged (identity-cluster) answer — names the
    cluster member this edge was reached through, so provenance survives the
    merge. Omitted entirely on a single-node answer, where it would say nothing
    the root already says.
    """
    out = {
        "from": names.get(row.source_id, str(row.source_id)),
        "to": names.get(row.target_id, str(row.target_id)),
        "edge_type": row.edge_type,
        "method": row.method,
        "confidence": float(row.confidence),
        "source": row.source,
        "retired": row.valid_to is not None,
    }
    if via is not None:
        out["via"] = via
    return out


def neighborhood(
    session: Any,
    root_id: uuid.UUID,
    depth: int = 2,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = 1000,
    active_only: bool = True,
    min_confidence: float = 0.0,
    identity_merge: bool = True,
) -> dict[str, Any]:
    """The subgraph around ``root_id`` as plain dicts (no Pydantic, no HTTP).

    Truncation is REPORTED, never silent: ``node_total``/``edge_total`` are what
    the walk found, ``nodes``/``edges`` are what fits under the caps, and
    ``truncated`` says whether those differ.

    IDENTITY (default ON). ``root_id`` is expanded to its full identity cluster
    (:func:`identity_cluster`) and the walk is seeded with every member, so the
    answer is the UNION of the machine's edges no matter which per-source row
    the caller happened to name. Reported as:

    ``identity_members``
        every row that makes up this logical entity, root first
    ``identity_merged``
        whether more than one row was involved (i.e. whether ``via`` is
        meaningful)
    ``identity_vetoed``
        rows a ``SAME_AS`` edge pointed at but a human veto refused

    DEPTH ACCOUNTING — THE IDENTITY HOP IS FREE, DELIBERATELY. ``depth`` is
    spent only on real relationships. Traversing to "the same machine as seen
    by another collector" is not a step AWAY from the machine, and charging a
    hop for it is exactly why the live ``depth=1`` question returned one
    self-referential ``SAME_AS`` link and nothing else. Concretely: at
    ``depth=1`` from the Ansible row you get the Linux row's own hop-1
    neighbours, and NOT their hop-2 neighbours — free does not mean unbounded.
    (This differs from ``graph_phase3.blast_radius``, where ``SAME_AS`` is an
    ordinary edge inside the hop budget, and that difference is intentional:
    blast radius answers "how far does damage spread", a question in which
    every hop is a real step; this answers "what is connected to X", a question
    about one entity.)
    """
    depth = max(1, min(int(depth), MAX_DEPTH))
    if identity_merge:
        members, vetoed = identity_cluster(session, [root_id], min_confidence=min_confidence)
    else:
        members, vetoed = [root_id], []

    order, edge_map, ceiling_hit, edge_origin = walk_multi(
        session, members, depth, active_only, min_confidence
    )

    kept_ids = order[:max_nodes]
    kept = set(kept_ids)

    # Member rows are always fetched, even if the node cap truncated one away,
    # so ``identity_members`` can never under-report which rows were merged.
    fetch_ids = list(dict.fromkeys(kept_ids + members + vetoed))
    rows: dict[uuid.UUID, GraphNodeRow] = {}
    for chunk in chunks(fetch_ids):
        for row in session.execute(
            select(GraphNodeRow).where(GraphNodeRow.id.in_(chunk))
        ).scalars():
            rows[row.id] = row

    names = {nid: (r.name or r.natural_key or str(nid)) for nid, r in rows.items()}
    merged = len(members) > 1
    labels = {mid: member_label(rows[mid]) for mid in members if mid in rows}
    if merged:
        # Cluster members share a name by construction, so an edge BETWEEN two
        # of them renders as "node_a -> node_a" — read by a reader (human or
        # model) as "an identity link between two representations of itself",
        # which is the useless answer this whole change exists to kill. Qualify
        # member endpoints with their node type so a cross-source link is
        # visibly a cross-source link. The SAME_AS rows are KEPT, not
        # suppressed: they are the evidence for the merge, and hiding the
        # evidence for a merge is how you get an answer nobody can audit.
        names.update(labels)
    # Preserve BFS order; a node id with no row is a referential impossibility
    # (FK CASCADE) but skipping beats a KeyError.
    nodes = [node_dict(rows[i]) for i in kept_ids if i in rows]
    kept_edges = [e for e in edge_map.values() if e.source_id in kept and e.target_id in kept]
    edges = [
        edge_dict(e, names, via=labels.get(edge_origin.get(e.id)) if merged else None)
        for e in kept_edges[:max_edges]
    ]

    return {
        "root_id": str(root_id),
        "nodes": nodes,
        "edges": edges,
        "node_total": len(order),
        "edge_total": len(edge_map),
        "truncated": len(nodes) < len(order) or len(edges) < len(kept_edges),
        "depth": depth,
        "active_only": active_only,
        "walk_ceiling_hit": ceiling_hit,
        "identity_members": [node_dict(rows[m]) for m in members if m in rows],
        "identity_merged": merged,
        "identity_vetoed": [node_dict(v) for v in (rows[x] for x in vetoed if x in rows)],
    }


# ---------------------------------------------------------------------------
# Containment facts — the half of the answer the KG store structurally cannot
# give (see the module docstring).
# ---------------------------------------------------------------------------


def linux_service_facts(
    session: Any, resource_id: uuid.UUID | None, limit: int = 50
) -> list[dict[str, Any]]:
    """systemd units recorded for a Linux host, from ``linux_services``.

    These are FACTS from a detail table, not graph edges, and callers must
    label them as such. Returns ``[]`` when the node is not resource-backed or
    is not a Linux host — absence of facts, not an error.
    """
    if resource_id is None:
        return []
    from infra_brain.db.models import LinuxHost, LinuxService

    rows = session.execute(
        select(LinuxService.name, LinuxService.state, LinuxService.enabled)
        .join(LinuxHost, LinuxHost.id == LinuxService.host_id)
        .where(LinuxHost.resource_id == resource_id)
        .order_by(LinuxService.name)
        .limit(limit)
    ).all()
    return [{"service": r[0], "state": r[1], "enabled": bool(r[2])} for r in rows]
