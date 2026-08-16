"""The generic graph engine — P0 of the graph-first architecture.

Reads the ``emits_nodes`` / ``emits_edges`` declarations registered on
``AgentSpec`` and materialises them into ``graph_nodes`` / ``graph_edges``.

See docs/decisions/2026-08-11-graph-first-architecture.md (§4, §5 P0) and its
dependency docs/decisions/2026-08-10-graph-edge-authority-spec.md.

THE ONE RULE THIS MODULE MUST KEEP
-----------------------------------
**Zero domain knowledge.** Nothing here imports ``LinuxHost``,
``HomelabService``, ``VsphereVm`` or any other domain model, and nothing here
branches on a domain name, a node type or an edge type. If a source's data
cannot be expressed as a declaration, the fix is the CONTRACT
(``etl/spec.py``), never a special case here — a special case here is how
``graph_maintenance.py`` reached 6,367 lines, and re-growing one in a new file
would be a rename, not a fix. ``tests/test_graph_engine.py`` asserts this
mechanically — it parses this module's AST and fails on an import of any
collector, on a reference to any domain model, and on a string literal equal
to a registered domain — and drives the engine with fictional domains, so an
accidental coupling fails a test rather than having to be caught in review.

WHAT IT READS
-------------
The generic ``resources`` table, filtered by ``(domain, type)``, with the
payload in ``resources.metadata``. That is a deliberate divergence from the
design doc's ``source_table=<ORM class>`` sketch; see ``NodeSpec``'s docstring
for why (short version: ``resources`` is the one table every collector already
writes, and reading it is what lets this module import no domain model at all).

Plus, where a declaration asks for it, the CHILD ROWS of those resources — a
``ChildSpec``'s ``(table name, FK column)`` path. Those tables are looked up BY
NAME in the shared SQLAlchemy ``MetaData`` and walked by primary key, so this
module still imports no domain model and still knows nothing about what any of
them mean. It exists because the join keys of real relationships live there:
a compose file's images are rows of a detail table, not a key in its metadata.

WHAT IT WRITES
--------------
``graph_nodes`` / ``graph_edges`` — the bitemporal store — through
``graph_phase2.upsert_node`` / ``upsert_edge``, never directly. NOT
``resource_relationships``: that table's ``UNIQUE(from, to, type)`` permits one
row per relationship forever, so it structurally cannot record history.

``upsert_edge`` is the single write choke point where the authority model is
enforced, so an engine edge (always ``authority='auto'``) can never overwrite a
human-authored one — rule W4 declines and logs rather than downgrading the row.

WHAT IT DOES NOT DO YET
-----------------------
Retire edges whose backing evidence disappeared. ``graph_phase2.retire_edges``
exists for it, but retirement needs a "this pass observed the full population"
signal to distinguish "the relationship ended" from "the collector was skipped
this cycle" — and getting that wrong silently deletes true history, which is
worse than a stale edge. Deferred deliberately; ``graph_maintenance``'s
existing decay/prune passes continue to run alongside.

This runs ALONGSIDE ``graph_maintenance``'s existing derivation. Nothing there
is removed; that is P2+.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

from sqlalchemy import select

from infra_brain import graph_phase2
from infra_brain.db.models import GraphNode, Resource
from infra_brain.etl.keys import KEY_NORMALIZERS
from infra_brain.etl.spec import AgentSpec, ChildSpec, EdgeSpec, NodeSpec, agent_specs

logger = logging.getLogger(__name__)

#: Written to ``graph_nodes.source`` / ``graph_edges.source`` — the CODE PATH
#: audit trail, never the authority discriminator. The declaring domain is
#: appended so a reader can see which collector's declaration produced a row.
EMITTER = "graph_engine"


# ---------------------------------------------------------------------------
# Field-reference resolution (the only "schema knowledge" the engine has, and
# it is entirely structural — column names come from the declaration)
# ---------------------------------------------------------------------------


def _resolve(obj: Any, ref: str, blob_attr: str) -> Optional[str]:
    """Resolve a declared field reference against ``obj``.

    ``"name"`` -> a column; ``"metadata.host"`` / ``"attributes.host"`` -> a
    key inside the JSONB blob. Returns ``None`` when the value is absent, null
    or blank — the engine reads that as "no key", which means "emit nothing",
    never "match anything".

    Values are stringified because both sides of a join must be comparable and
    both target columns are ``String``. A non-scalar (dict/list) is refused
    rather than stringified into a key nobody could have intended.
    """
    prefix, _, key = ref.partition(".")
    if key and prefix in ("metadata", "attributes"):
        blob = getattr(obj, blob_attr, None) or {}
        value = blob.get(key)
    else:
        value = getattr(obj, ref, None)
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _resolve_all(obj: Any, ref: str, blob_attr: str) -> list[str]:
    """Every value a MANY-VALUED source key holds, in declaration order.

    Used ONLY for an ``EdgeSpec`` whose ``from_key_multi`` is set, and only on
    the source side. ``_resolve`` above is untouched and still refuses a
    non-scalar outright, which is what keeps every existing declaration meaning
    what it meant: a list arriving where a single key was declared still
    produces no edge rather than several.

    A scalar is treated as a one-element list so a gathered attribute that
    happens to hold one value behaves identically to a plain one. Nested
    containers inside the list are dropped for the same reason ``_resolve``
    drops them: there is no key a reader could have intended.
    """
    prefix, _, key = ref.partition(".")
    if key and prefix in ("metadata", "attributes"):
        blob = getattr(obj, blob_attr, None) or {}
        value = blob.get(key)
    else:
        value = getattr(obj, ref, None)
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        if item is None or isinstance(item, (dict, list, tuple, set)):
            continue
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# Child-row reach (ChildSpec). Resolved through the shared SQLAlchemy MetaData
# by TABLE NAME, never through an imported model class — which is the only
# reason a declaration can reach a detail table without the engine learning
# what that table is.
# ---------------------------------------------------------------------------


def _single_pk(table: Any) -> Any:
    """The one primary-key column a child path descends from."""
    pks = list(table.primary_key.columns)
    if len(pks) != 1:
        raise ValueError(
            f"child path table {table.name!r} has {len(pks)} primary-key columns — a child "
            "path descends by single-column primary key"
        )
    return pks[0]


def _descend(joined: Any, start_table: Any, hops: tuple[tuple[str, str], ...]) -> tuple[Any, Any]:
    """Extend ``joined`` down ``hops``, starting from ``start_table``'s PK.

    Every hop joins ``table[fk_column]`` to the PREVIOUS table's primary key.
    A name that does not resolve raises here, inside the caller's
    per-declaration SAVEPOINT, so one bad declaration is reported and the rest
    of the pass survives. Returns ``(extended join clause, last table)`` —
    the last table being ``start_table`` itself when ``hops`` is empty.
    """
    tables = Resource.__table__.metadata.tables
    parent_pk = _single_pk(start_table)
    last: Any = start_table
    for table_name, fk_column in hops:
        table = tables.get(table_name)
        if table is None:
            raise ValueError(f"child path names unknown table {table_name!r}")
        if fk_column not in table.c:
            raise ValueError(f"child path table {table_name!r} has no column {fk_column!r}")
        joined = joined.join(table, table.c[fk_column] == parent_pk)
        parent_pk = _single_pk(table)
        last = table
    return joined, last


def _child_chain(child: ChildSpec) -> tuple[Any, Any]:
    """``(join clause, last table)`` for a declared child path.

    A strict descent from ``resources`` — see ``_descend``. ``ChildSpec``
    guarantees the path is non-empty, so ``last`` is always a real child table.
    """
    joined, last = _descend(Resource.__table__, Resource.__table__, child.path)
    if child.column not in last.c:
        raise ValueError(f"child table {last.name!r} has no column {child.column!r}")
    return joined, last


def _child_values(
    session: Any, domain: str, node_spec: NodeSpec, child: ChildSpec
) -> dict[UUID, list[str]]:
    """Parent resource id -> its child rows' distinct values, sorted.

    The parent set is exactly the one ``_emit_nodes`` reads — same domain, same
    resource type, retired rows excluded — so a value can never arrive from a
    resource the node population does not contain.

    A non-scalar column value is SKIPPED, matching ``_resolve``'s refusal: a
    JSON-list column is a real shape in this schema and flattening it silently
    would invent a fan-out the declaration never asked for.
    """
    joined, last = _child_chain(child)
    rows = session.execute(
        select(Resource.id, last.c[child.column])
        .select_from(joined)
        .where(
            Resource.domain == (node_spec.domain or domain),
            Resource.type == node_spec.resource_type,
            Resource.retired_at.is_(None),
        )
    ).all()
    out: dict[UUID, list[str]] = {}
    for resource_id, value in rows:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value).strip()
        if not text:
            continue
        bucket = out.setdefault(resource_id, [])
        if text not in bucket:
            bucket.append(text)
    for bucket in out.values():
        bucket.sort()
    return out


def _node_attributes(resource: Resource, node_spec: NodeSpec) -> Optional[dict[str, Any]]:
    """Copy the declared ``metadata`` keys onto the node.

    Keys whose value is absent are omitted rather than stored as null, so a
    downstream reader can tell "the source did not say" from "the source said
    nothing runs here" only by the key's absence — which is the same signal
    ``resources.metadata`` itself carries.
    """
    if not node_spec.attributes:
        return None
    blob = resource.metadata_ or {}
    attrs = {k: blob[k] for k in node_spec.attributes if blob.get(k) is not None}
    return attrs or None


# ---------------------------------------------------------------------------
# Node materialisation
# ---------------------------------------------------------------------------


def _row_value(ref: str, row: Mapping[str, Any]) -> Optional[str]:
    """Resolve a ``rows.<key>`` reference against one child row's values."""
    _, _, key = ref.partition(".")
    value = row.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _scalar_text(value: Any) -> Optional[str]:
    """One place for the child-value refusal rules: a non-scalar (a JSON list,
    a dict) is SKIPPED rather than stringified or flattened, and blank is
    "no value" — the same rules ``_resolve`` and ``_child_values`` apply."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _row_gathered_values(
    session: Any, domain: str, node_spec: NodeSpec
) -> dict[str, dict[str, list[str]]]:
    """``gather key -> junction value -> gathered values`` for a junction node.

    Grouped by the junction VALUE, never by parent resource — that grouping is
    the whole of the junction grammar (TRK-359): a value recurring under many
    anchors accumulates the deterministic, sorted UNION of what all of them
    say, where a per-parent list would have meant "whichever parent was
    materialised last".

    Two shapes per ``RowGather`` (see its docstring): a NON-EMPTY ``path``
    descends further below the junction table by the same PK walk every child
    path uses; an EMPTY ``path`` reads a field of the ANCHORING ``resources``
    row itself. Either way the anchor population is exactly the one the node's
    own values are read from — same domain, same resource type, retired
    excluded.
    """
    child = node_spec.from_rows
    out: dict[str, dict[str, list[str]]] = {}
    for gather in node_spec.row_gathers:
        joined, junction = _child_chain(child)
        anchor_filter = (
            Resource.domain == (node_spec.domain or domain),
            Resource.type == node_spec.resource_type,
            Resource.retired_at.is_(None),
        )
        buckets: dict[str, list[str]] = {}
        if gather.path:
            joined, deepest = _descend(joined, junction, gather.path)
            if gather.column not in deepest.c:
                raise ValueError(
                    f"row gather table {deepest.name!r} has no column {gather.column!r}"
                )
            rows = session.execute(
                select(junction.c[child.column], deepest.c[gather.column])
                .select_from(joined)
                .where(*anchor_filter)
            ).all()
            pairs = ((_scalar_text(jval), _scalar_text(gval)) for jval, gval in rows)
        else:
            rows = session.execute(
                select(junction.c[child.column], Resource).select_from(joined).where(*anchor_filter)
            ).all()
            pairs = (
                (_scalar_text(jval), _resolve(resource, gather.column, "metadata_"))
                for jval, resource in rows
            )
        for junction_value, gathered in pairs:
            if not junction_value or not gathered:
                continue
            bucket = buckets.setdefault(junction_value, [])
            if gathered not in bucket:
                bucket.append(gathered)
        for bucket in buckets.values():
            bucket.sort()
        out[gather.key] = buckets
    return out


def _emit_child_row_nodes(session: Any, domain: str, node_spec: NodeSpec) -> int:
    """One node per DISTINCT child value — a shared value node.

    ``nginx:1.25`` in six compose files is ONE ``ContainerImage``, which is what
    makes "who runs this image" answerable. That sharing is also why such a
    node carries no ``resource_id``: it has six owning resources, so claiming
    one would be a lie and the last pass would decide which.

    Declared ``row_gathers`` ride onto the node grouped by the junction value
    (``_row_gathered_values``). A value with nothing gathered has the key
    ABSENT, not empty — the same signal ``gathers`` gives a parent with no
    child rows, and load-bearing the same way: an edge over an absent key
    writes nothing, which is the correct answer for a group with zero members.
    """
    child = node_spec.from_rows
    gathered = _row_gathered_values(session, domain, node_spec)
    written = 0
    seen: set[str] = set()
    for values in _child_values(session, domain, node_spec, child).values():
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            row = {child.key: value}
            natural_key = _row_value(node_spec.natural_key, row)
            if not natural_key:
                continue
            attributes = dict(row)
            for key, buckets in gathered.items():
                bucket = buckets.get(value)
                if bucket:
                    attributes[key] = bucket
            graph_phase2.upsert_node(
                session,
                node_type=node_spec.type,
                natural_key=natural_key[:512],
                name=(_row_value(node_spec.name, row) or natural_key)[:256],
                source=domain,
                resource_id=None,
                attributes=attributes,
            )
            written += 1
    return written


def _emit_nodes(session: Any, domain: str, node_spec: NodeSpec) -> int:
    if node_spec.from_rows is not None:
        return _emit_child_row_nodes(session, domain, node_spec)
    # Gathered once per declaration, not once per row: the query is the same
    # join for every resource in the population.
    gathered = {
        child.key: _child_values(session, domain, node_spec, child) for child in node_spec.gathers
    }
    rows = (
        session.execute(
            select(Resource).where(
                Resource.domain == (node_spec.domain or domain),
                Resource.type == node_spec.resource_type,
                Resource.retired_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    written = 0
    for resource in rows:
        natural_key = _resolve(resource, node_spec.natural_key, "metadata_")
        if not natural_key:
            # No stable identity -> no node. Materialising under a blank key
            # would collapse every such row onto one node.
            logger.debug(
                "graph_engine: %s resource %s has no %s — skipped",
                node_spec.type,
                resource.id,
                node_spec.natural_key,
            )
            continue
        attributes = _node_attributes(resource, node_spec)
        for key, by_resource in gathered.items():
            values = by_resource.get(resource.id)
            if not values:
                # No child rows -> the key is ABSENT, not empty. Same signal
                # ``_node_attributes`` gives for a metadata key the source did
                # not set, and it matters: an edge over an absent key writes
                # nothing, which is the correct answer for a compose file whose
                # services were never parsed.
                continue
            attributes = dict(attributes or {})
            attributes[key] = values
        graph_phase2.upsert_node(
            session,
            node_type=node_spec.type,
            natural_key=natural_key[:512],
            name=(_resolve(resource, node_spec.name, "metadata_") or natural_key)[:256],
            source=domain,
            resource_id=resource.id if node_spec.resource_backed else None,
            attributes=attributes,
        )
        written += 1
    return written


# ---------------------------------------------------------------------------
# Edge materialisation
# ---------------------------------------------------------------------------


def _target_index(
    session: Any, edge_spec: EdgeSpec, normalize: Any
) -> tuple[dict[str, UUID], set[str]]:
    """Map normalised target key -> node id, plus the set of AMBIGUOUS keys.

    A key claimed by two target nodes is a contradiction in the source data,
    not a coin flip. Such keys are reported and skipped: guessing would mint a
    confident-looking edge that is wrong half the time, and this store's whole
    point is that provenance is honest.

    THIS RULE IS DIRECTION-INDEPENDENT AND STAYS THAT WAY. ``EdgeSpec`` grew an
    ``EdgeDirection.INVERSE`` so a one-to-many relationship can be declared, and
    it deliberately changes nothing here: an inverse edge runs the identical
    functional join and gets the identical refusal, then stores the arrow the
    other way. If a future declaration seems to need "a key may have several
    targets", the answer is INVERSE on the other side of the join — never a
    softer rule here.
    """
    index: dict[str, UUID] = {}
    ambiguous: set[str] = set()
    targets = (
        session.execute(select(GraphNode).where(GraphNode.node_type == edge_spec.to_node))
        .scalars()
        .all()
    )
    for node in targets:
        raw = _resolve(node, edge_spec.to_key, "attributes")
        key = normalize(raw or "")
        if not key:
            continue
        if key in index and index[key] != node.id:
            ambiguous.add(key)
            continue
        index[key] = node.id
    for key in ambiguous:
        index.pop(key, None)
    return index, ambiguous


def _emit_edges(session: Any, domain: str, edge_spec: EdgeSpec, errors: list[str]) -> int:
    normalize = KEY_NORMALIZERS[edge_spec.key_normalizer]
    index, ambiguous = _target_index(session, edge_spec, normalize)
    if ambiguous:
        errors.append(
            f"{domain}/{edge_spec.type}: ambiguous {edge_spec.to_node} target key(s) "
            f"{sorted(ambiguous)} — more than one node claims each; no edge written"
        )

    written = 0
    # .all() -- fully drained BEFORE the loop. upsert_edge SELECTs and flushes
    # per call, and issuing those against a half-consumed result set is how you
    # get a silently truncated pass on some drivers.
    sources = (
        session.execute(
            select(GraphNode).where(
                GraphNode.node_type == edge_spec.from_node, GraphNode.source == domain
            )
        )
        .scalars()
        .all()
    )
    for node in sources:
        # A MANY-VALUED source key makes several assertions out of one node;
        # the default single-valued path is byte-for-byte what it was. Either
        # way each value below is resolved through the SAME index, so the
        # ambiguity guarantee is identical for one key and for twenty.
        if edge_spec.from_key_multi:
            raw_values = _resolve_all(node, edge_spec.from_key, "attributes")
        else:
            single = _resolve(node, edge_spec.from_key, "attributes")
            raw_values = [single] if single else []
        for raw in raw_values:
            key = normalize(raw or "")
            if not key:
                # The source did not say (e.g. a manifest entry with host: null).
                # No edge is the correct answer, not a broken one.
                continue
            target_id = index.get(key)
            if target_id is None:
                # Nothing we collect answers to that key. The engine never
                # invents a target node for a name it was merely told about.
                logger.debug(
                    "graph_engine: %s %s -> no %s matching %r",
                    edge_spec.type,
                    node.natural_key,
                    edge_spec.to_node,
                    key,
                )
                continue
            # The JOIN always runs from this node to its single resolved target.
            # ``direction`` decides only which way the resulting edge is STORED,
            # so an INVERSE declaration fans N edges OUT of the one target
            # without the index above having relaxed anything. This is the whole
            # of the one-to-many support, and it is contract knowledge, not
            # domain knowledge — the engine still cannot name a domain, a node
            # type or an edge type.
            source_id, edge_target_id = (
                (target_id, node.id) if edge_spec.is_inverse else (node.id, target_id)
            )
            graph_phase2.upsert_edge(
                session,
                source_id=source_id,
                target_id=edge_target_id,
                edge_type=edge_spec.type,
                method=edge_spec.method,
                confidence=edge_spec.confidence,
                source=f"{EMITTER}.{domain}",
                evidence={
                    "declared_by": domain,
                    "from_key": edge_spec.from_key,
                    "to_key": edge_spec.to_key,
                    "key_normalizer": edge_spec.key_normalizer,
                    "raw_value": raw,
                    "match_key": key,
                    # Recorded because the arrow alone no longer tells a reader
                    # which end carried the join key — the ambiguity guarantee
                    # applies to the JOIN's target, which under INVERSE is the
                    # edge's SOURCE.
                    "direction": edge_spec.direction.value,
                },
            )
            written += 1
    return written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def emit_all(
    session: Any, specs: Optional[Mapping[str, AgentSpec]] = None
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Materialise every registered declaration. Returns ``(counts, errors)``.

    Same shape as ``graph_phase2.emit_all`` so ``graph_maintenance`` treats it
    identically — including the "accumulate errors, do not raise" contract,
    which matters because this runs inside a per-block SAVEPOINT where a raise
    would abort the whole maintenance pass over one bad declaration.

    NODES ARE MATERIALISED FOR EVERY SPEC BEFORE ANY EDGE IS. An edge routinely
    targets another collector's entity — that is the entire point of a graph
    "across" sources — so a single interleaved pass would make correctness
    depend on registry iteration order.

    ``specs`` is injectable for tests; production passes None and gets the live
    registry.
    """
    registry = agent_specs() if specs is None else specs
    counts: dict[str, dict[str, int]] = {"nodes": {}, "edges": {}}
    errors: list[str] = []

    contributing: list[tuple[str, AgentSpec]] = [
        (domain, spec)
        for domain, spec in sorted(registry.items())
        if spec.emits_nodes or spec.emits_edges
    ]
    if not contributing:
        return counts, errors

    for domain, spec in contributing:
        for node_spec in spec.emits_nodes:
            # PER-DECLARATION SAVEPOINT (the repo's F-023 pattern). Catching the
            # exception is not on its own enough to contain it: on real
            # PostgreSQL a failed flush leaves the connection in
            # InFailedSqlTransaction, so without the ROLLBACK TO SAVEPOINT that
            # exiting begin_nested() performs, one bad declaration would make
            # every LATER declaration fail too — turning "one spec is broken"
            # into "the whole pass is broken", which is exactly the claim this
            # try/except is here to make false. sqlite tolerates the sloppy
            # version, which is why it has to be deliberate rather than
            # discovered in production.
            try:
                with session.begin_nested():
                    written = _emit_nodes(session, domain, node_spec)
            except Exception as exc:  # noqa: BLE001 - one bad spec must not sink the pass
                errors.append(f"{domain}/{node_spec.type} nodes: {exc}")
                continue
            counts["nodes"][node_spec.type] = counts["nodes"].get(node_spec.type, 0) + written

    for domain, spec in contributing:
        for edge_spec in spec.emits_edges:
            try:
                with session.begin_nested():
                    written = _emit_edges(session, domain, edge_spec, errors)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{domain}/{edge_spec.type} edges: {exc}")
                continue
            counts["edges"][edge_spec.type] = counts["edges"].get(edge_spec.type, 0) + written

    logger.info(
        "graph_engine: nodes=%s edges=%s errors=%d", counts["nodes"], counts["edges"], len(errors)
    )
    return counts, errors


def _render_edge(edge_spec: EdgeSpec) -> str:
    """``"<stored source>-<TYPE>-><stored target>"`` for introspection."""
    src, dst = edge_spec.written_as()
    return f"{src}-{edge_spec.type}->{dst}"


def declared_contributions() -> dict[str, dict[str, Iterable[str]]]:
    """Introspection helper: what each domain currently declares.

    Exists so "what does the graph know how to build" is answerable without
    reading source — the question the 6,367-line deriver made unanswerable.

    Edges are rendered in the direction they are STORED (``EdgeSpec.written_as``),
    not in join order: a reader asking what the graph looks like wants the
    arrow, and an INVERSE declaration's arrow is the reverse of its join.
    """
    return {
        domain: {
            "nodes": [n.type for n in spec.emits_nodes],
            "edges": [_render_edge(e) for e in spec.emits_edges],
            "identity_keys": list(spec.identity_keys),
        }
        for domain, spec in sorted(agent_specs().items())
        if spec.emits_nodes or spec.emits_edges
    }


__all__ = ["EMITTER", "declared_contributions", "emit_all"]
