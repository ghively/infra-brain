"""Relationship Graph Phase 2 — node/edge store helpers + the three emitters.

Issue #126. Populates ``graph_nodes`` / ``graph_edges`` (see
``infra_brain/db/models/graph.py`` for the schema and for why these tables are
separate from the pre-existing ``resource_relationships`` store).

Scope — exactly three edge types, all derived from already-populated tables:

  1. ``HOSTED_ON``        VsphereVM → VsphereHost
     ``vsphere_vms.esxi_host`` (an ESXi display name declared by vCenter)
     matched to ``vsphere_hosts.name`` within the SAME ``vcenter``.
     method=deterministic_match, confidence=0.990 — the join key is a mutable
     display name, so this is not an FK-strength fact.
  2. ``MOUNTS_DATASTORE`` VsphereVM → VsphereDatastore and
                          VsphereHost → VsphereDatastore
     ``vsphere_vms.datastore_names`` / ``vsphere_hosts.datastore_names`` (JSONB
     name lists) matched to ``vsphere_datastores.name`` within the same
     ``vcenter``. Same method/confidence rationale as HOSTED_ON.
  3. ``AFFECTED_BY_CVE``  R7Asset → Cve
     ``vuln_queue.resource_id`` → ``r7_assets.resource_id`` (a real FK join in
     our own schema) with ``vuln_queue.cve_id`` as the CVE node key.
     method=declared, confidence=1.000 — Rapid7 asserts the finding and we
     resolve it without any string matching.

NOT built, deliberately: ``DEFINED_IN_IAC``. Ansible playbook ``hosts:`` fields
are unresolved raw strings / Jinja defaults with no structured link to inventory
groups, so such an edge would be a guess. Do not stub it half-working.

Read-only guarantee: every function here READS infra-brain's own Postgres
tables and WRITES only ``graph_nodes`` / ``graph_edges``. No external
infrastructure is contacted, mutated, or even opened — there is no HTTP client,
no pyvmomi, no subprocess in this module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import select

from infra_brain.db.models import (
    CONFIDENCE_DECLARED,
    CONFIDENCE_DETERMINISTIC_NAME,
    GraphEdge,
    GraphEdgeAuthority,
    GraphEdgeMethod,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    R7Asset,
    VsphereDatastore,
    VsphereHost,
    VsphereVm,
    VulnQueueItem,
)

logger = logging.getLogger(__name__)

#: Emitter identity written to ``graph_edges.source`` / ``graph_nodes.source``.
EMITTER_HOSTED_ON = "graph_phase2.hosted_on"
EMITTER_MOUNTS_DATASTORE = "graph_phase2.mounts_datastore"
EMITTER_AFFECTED_BY_CVE = "graph_phase2.affected_by_cve"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def vsphere_key(vcenter: str, moref: str) -> str:
    """Natural key for any vSphere node: the ``(vcenter, moref)`` unique pair."""
    return f"{vcenter}:{moref}"


# ---------------------------------------------------------------------------
# Store helpers (pure ORM — dialect-portable, so the same code path runs
# against Postgres in production and sqlite in tests)
# ---------------------------------------------------------------------------


def upsert_node(
    session: Any,
    *,
    node_type: GraphNodeType | str,
    natural_key: str,
    name: str,
    source: str,
    resource_id: Optional[UUID] = None,
    attributes: Optional[dict[str, Any]] = None,
) -> GraphNode:
    """Get-or-create the node identified by ``(node_type, natural_key)``.

    Idempotent: a second call with the same identity refreshes ``last_seen``,
    ``name``, ``resource_id`` and ``attributes`` on the existing row rather
    than inserting a duplicate (the ``uq_graph_nodes_identity`` constraint
    would reject one anyway).
    """
    ntype = node_type.value if isinstance(node_type, GraphNodeType) else node_type
    node = session.execute(
        select(GraphNode).where(
            GraphNode.node_type == ntype,
            GraphNode.natural_key == natural_key,
        )
    ).scalar_one_or_none()
    now = _now()
    if node is None:
        node = GraphNode(
            node_type=ntype,
            natural_key=natural_key,
            name=name,
            source=source,
            resource_id=resource_id,
            attributes=attributes,
            first_seen=now,
            last_seen=now,
        )
        session.add(node)
        session.flush()
        return node
    node.name = name
    node.source = source
    node.last_seen = now
    if resource_id is not None:
        node.resource_id = resource_id
    if attributes is not None:
        node.attributes = attributes
    session.flush()
    return node


def upsert_edge(
    session: Any,
    *,
    source_id: UUID,
    target_id: UUID,
    edge_type: GraphEdgeType | str,
    method: GraphEdgeMethod | str,
    confidence: Decimal,
    source: str,
    evidence: Optional[dict[str, Any]] = None,
    authority: GraphEdgeAuthority | str = GraphEdgeAuthority.AUTO.value,
) -> GraphEdge:
    """Insert, or refresh in place, the ACTIVE edge for this triple.

    "Active" means ``valid_to IS NULL``; the partial unique index allows at
    most one such row per ``(source_id, target_id, edge_type)``.

    THE SINGLE WRITE CHOKE POINT for ``graph_edges``, and therefore the one
    place the authority-precedence rules are enforced — no emitter can forget
    them, and no future emitter has to remember to join an emitter-precedence
    registry (which is exactly the brittleness that let an automatic pass
    silently overwrite a human-confirmed identity edge, KG-1):

    ==  ==========================================  ==========================
    W1  no active edge                              insert with the writer's
                                                    authority
    W2  active edge, SAME authority as the writer   refresh in place (today's
                                                    semantics; ``valid_from``
                                                    keeps its true start, so
                                                    2-hourly re-observation
                                                    stays cheap)
    W3  active ``auto`` edge, ``human`` writer      ESCALATION — retire the
                                                    auto row and INSERT a new
                                                    human row. An authority
                                                    change is a DIFFERENT
                                                    assertion; mutating in
                                                    place would destroy the
                                                    bitemporal record of
                                                    "machine asserted 0.990
                                                    T1–T2; operator declared
                                                    1.000 from T2–".
    W4  active ``human`` edge, ``auto`` writer      DE-ESCALATION — no-op.
                                                    Returns the existing edge
                                                    unchanged and logs at
                                                    WARNING. Deliberately not
                                                    a raise: this runs inside
                                                    emitters' per-item
                                                    SAVEPOINTs, where raising
                                                    would abort a whole pass
                                                    for an expected, benign
                                                    condition. Emitters MUST
                                                    still pre-filter (see
                                                    ``graph_phase3.pair_gate``)
                                                    — reaching this guard
                                                    means an emitter bug.
    W5  ``NOT_SAME_AS`` with non-human authority    ``ValueError``.
    ==  ==========================================  ==========================

    Raises ``ValueError`` if a caller tries to claim confidence 1.000 for
    anything other than ``method='declared'`` — the honesty rule from the
    issue is enforced here rather than left to each emitter.
    """
    etype = edge_type.value if isinstance(edge_type, GraphEdgeType) else edge_type
    emethod = method.value if isinstance(method, GraphEdgeMethod) else method
    eauth = authority.value if isinstance(authority, GraphEdgeAuthority) else authority
    conf = Decimal(confidence)
    if eauth not in (GraphEdgeAuthority.AUTO.value, GraphEdgeAuthority.HUMAN.value):
        raise ValueError(f"authority must be 'auto' or 'human', got {eauth!r}")
    if conf >= Decimal("1.000") and emethod != GraphEdgeMethod.DECLARED.value:
        raise ValueError(
            f"confidence {conf} is only permitted for method='declared', got {emethod!r} "
            f"({etype} edge from {source})"
        )
    if conf < Decimal("0") or conf > Decimal("1.000"):
        raise ValueError(f"confidence must be within [0, 1.000], got {conf}")
    # W5. Machine "negative knowledge" already has a representation —
    # suppression (TRK-087), which asserts nothing and writes nothing. A
    # NOT_SAME_AS row is an accountable human's positive claim that two things
    # differ, so an automatic writer may never mint one.
    if etype == GraphEdgeType.NOT_SAME_AS.value and eauth != GraphEdgeAuthority.HUMAN.value:
        raise ValueError(
            f"NOT_SAME_AS is a human-only assertion; got authority={eauth!r} from {source!r}"
        )

    edge = session.execute(
        select(GraphEdge).where(
            GraphEdge.source_id == source_id,
            GraphEdge.target_id == target_id,
            GraphEdge.edge_type == etype,
            GraphEdge.valid_to.is_(None),
        )
    ).scalar_one_or_none()
    now = _now()
    if edge is not None and edge.authority != eauth:
        if eauth == GraphEdgeAuthority.AUTO.value:
            # W4 — prevent and record, never raise.
            logger.warning(
                "upsert_edge: auto writer %s declined to overwrite human-authority %s edge %s",
                source,
                etype,
                edge.id,
            )
            return edge
        # W3 — retire-and-insert, never mutate across an authority boundary.
        # retire_edges flushes, so the partial unique index sees the old row
        # closed before the replacement is inserted.
        retired_evidence = dict(edge.evidence or {})
        retired_evidence["superseded_by_authority"] = eauth
        retired_evidence["superseded_at"] = now.isoformat()
        edge.evidence = retired_evidence
        retire_edges(session, [edge])
        edge = None

    if edge is None:
        edge = GraphEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=etype,
            method=emethod,
            confidence=conf,
            evidence=evidence,
            source=source,
            authority=eauth,
            valid_from=now,
            recorded_at=now,
        )
        session.add(edge)
        session.flush()
        return edge
    # W2 — same authority, refresh in place.
    edge.method = emethod
    edge.confidence = conf
    edge.evidence = evidence
    edge.source = source
    edge.recorded_at = now
    session.flush()
    return edge


def retire_edges(session: Any, edges: Iterable[GraphEdge]) -> int:
    """Close the validity interval of each edge (never DELETE).

    Provided so a future reconciler can retire edges whose backing evidence
    disappeared without losing the historical record. No Phase 2 emitter
    calls this — the emitters only assert what they currently observe.
    """
    now = _now()
    count = 0
    for edge in edges:
        if edge.valid_to is None:
            edge.valid_to = now
            count += 1
    if count:
        session.flush()
    return count


# ---------------------------------------------------------------------------
# Node resolution helpers
# ---------------------------------------------------------------------------


def _vsphere_vm_node(session: Any, vm: VsphereVm) -> GraphNode:
    return upsert_node(
        session,
        node_type=GraphNodeType.VSPHERE_VM,
        natural_key=vsphere_key(vm.vcenter, vm.moref),
        name=vm.name,
        source="vsphere",
        resource_id=vm.resource_id,
        attributes={"vcenter": vm.vcenter, "moref": vm.moref, "power_state": vm.power_state},
    )


def _vsphere_host_node(session: Any, host: VsphereHost) -> GraphNode:
    return upsert_node(
        session,
        node_type=GraphNodeType.VSPHERE_HOST,
        natural_key=vsphere_key(host.vcenter, host.moref),
        name=host.name,
        source="vsphere",
        resource_id=host.resource_id,
        attributes={
            "vcenter": host.vcenter,
            "moref": host.moref,
            "cluster_name": host.cluster_name,
        },
    )


def _vsphere_datastore_node(session: Any, ds: VsphereDatastore) -> GraphNode:
    return upsert_node(
        session,
        node_type=GraphNodeType.VSPHERE_DATASTORE,
        natural_key=vsphere_key(ds.vcenter, ds.moref),
        name=ds.name,
        source="vsphere",
        resource_id=ds.resource_id,
        attributes={
            "vcenter": ds.vcenter,
            "moref": ds.moref,
            "datastore_type": ds.datastore_type,
        },
    )


# ---------------------------------------------------------------------------
# Emitter 1 — HOSTED_ON (VsphereVM → VsphereHost)
# ---------------------------------------------------------------------------


def emit_hosted_on(session: Any) -> int:
    """Emit ``VsphereVM --HOSTED_ON--> VsphereHost``; return edges written.

    Resolution: ``vsphere_vms.esxi_host`` holds the ESXi host's display name as
    vCenter reported it. It is matched, exactly and case-sensitively, against
    ``vsphere_hosts.name`` scoped to the same ``vcenter`` — cross-vCenter name
    collisions are therefore impossible, but an in-vCenter rename between the
    VM and host collections can still mis-resolve, which is why this is
    ``deterministic_match`` at 0.990 rather than ``declared`` at 1.000.

    VM templates are skipped: a template is not a running workload and its
    ``esxi_host`` is a storage artifact, not a placement fact.
    """
    hosts_by_key: dict[tuple[str, str], VsphereHost] = {}
    for host in session.execute(select(VsphereHost)).scalars():
        hosts_by_key[(host.vcenter, host.name)] = host

    written = 0
    for vm in session.execute(select(VsphereVm)).scalars():
        if vm.is_template:
            continue
        host_name = (vm.esxi_host or "").strip()
        if not host_name:
            continue
        host = hosts_by_key.get((vm.vcenter, host_name))
        if host is None:
            logger.debug(
                "graph_phase2: HOSTED_ON unresolved esxi_host %r for vm %s in %s",
                host_name,
                vm.moref,
                vm.vcenter,
            )
            continue
        vm_node = _vsphere_vm_node(session, vm)
        host_node = _vsphere_host_node(session, host)
        upsert_edge(
            session,
            source_id=vm_node.id,
            target_id=host_node.id,
            edge_type=GraphEdgeType.HOSTED_ON,
            method=GraphEdgeMethod.DETERMINISTIC_MATCH,
            confidence=CONFIDENCE_DETERMINISTIC_NAME,
            source=EMITTER_HOSTED_ON,
            evidence={
                "join": "vsphere_vms.esxi_host == vsphere_hosts.name, scoped by vcenter",
                "vcenter": vm.vcenter,
                "esxi_host_name": host_name,
                "vm_moref": vm.moref,
                "host_moref": host.moref,
            },
        )
        written += 1
    logger.info("graph_phase2: HOSTED_ON emitted %d edges", written)
    return written


# ---------------------------------------------------------------------------
# Emitter 2 — MOUNTS_DATASTORE (VsphereVM|VsphereHost → VsphereDatastore)
# ---------------------------------------------------------------------------


def emit_mounts_datastore(session: Any) -> int:
    """Emit ``VsphereVM|VsphereHost --MOUNTS_DATASTORE--> VsphereDatastore``.

    Resolution: both ``vsphere_vms.datastore_names`` and
    ``vsphere_hosts.datastore_names`` are JSONB lists of datastore DISPLAY
    names, matched to ``vsphere_datastores.name`` within the same ``vcenter``.
    Same ``deterministic_match`` / 0.990 rationale as HOSTED_ON: an exact,
    scoped, reproducible string equality, but on a mutable name.

    Duplicate names inside one list collapse to a single edge (the active-edge
    upsert would do so anyway). Returns the number of edges written.
    """
    ds_by_key: dict[tuple[str, str], VsphereDatastore] = {}
    for ds in session.execute(select(VsphereDatastore)).scalars():
        ds_by_key[(ds.vcenter, ds.name)] = ds

    written = 0

    def _link(node: GraphNode, vcenter: str, names: Any, owner_kind: str, owner_moref: str) -> int:
        n = 0
        for raw in names or []:
            ds_name = (raw or "").strip() if isinstance(raw, str) else ""
            if not ds_name:
                continue
            ds = ds_by_key.get((vcenter, ds_name))
            if ds is None:
                logger.debug(
                    "graph_phase2: MOUNTS_DATASTORE unresolved datastore %r in %s",
                    ds_name,
                    vcenter,
                )
                continue
            ds_node = _vsphere_datastore_node(session, ds)
            upsert_edge(
                session,
                source_id=node.id,
                target_id=ds_node.id,
                edge_type=GraphEdgeType.MOUNTS_DATASTORE,
                method=GraphEdgeMethod.DETERMINISTIC_MATCH,
                confidence=CONFIDENCE_DETERMINISTIC_NAME,
                source=EMITTER_MOUNTS_DATASTORE,
                evidence={
                    "join": (
                        f"vsphere_{owner_kind}s.datastore_names[] == "
                        "vsphere_datastores.name, scoped by vcenter"
                    ),
                    "vcenter": vcenter,
                    "datastore_name": ds_name,
                    "owner_kind": owner_kind,
                    "owner_moref": owner_moref,
                    "datastore_moref": ds.moref,
                },
            )
            n += 1
        return n

    for vm in session.execute(select(VsphereVm)).scalars():
        if vm.is_template:
            continue
        if not vm.datastore_names:
            continue
        written += _link(
            _vsphere_vm_node(session, vm), vm.vcenter, vm.datastore_names, "vm", vm.moref
        )

    for host in session.execute(select(VsphereHost)).scalars():
        if not host.datastore_names:
            continue
        written += _link(
            _vsphere_host_node(session, host),
            host.vcenter,
            host.datastore_names,
            "host",
            host.moref,
        )

    logger.info("graph_phase2: MOUNTS_DATASTORE emitted %d edges", written)
    return written


# ---------------------------------------------------------------------------
# Emitter 3 — AFFECTED_BY_CVE (R7Asset → Cve)
# ---------------------------------------------------------------------------


def emit_affected_by_cve(session: Any) -> int:
    """Emit ``R7Asset --AFFECTED_BY_CVE--> Cve``; return edges written.

    Resolution: ``vuln_queue.resource_id`` and ``r7_assets.resource_id`` are
    both FKs into the one shared ``resources.id`` UUID space, so this is a
    direct FK join with no identity or matching work — hence ``declared`` at
    confidence 1.000. Rapid7 itself asserts the asset/CVE finding; we only
    resolve which of our rows it refers to.

    The ``Cve`` node is a shared high-fan-in node keyed by the uppercased CVE
    id with no owning resource (``resource_id`` NULL), so N assets affected by
    one CVE converge on one node instead of creating N private CVE nodes.
    """
    assets_by_resource: dict[UUID, R7Asset] = {}
    for asset in session.execute(select(R7Asset).where(R7Asset.resource_id.is_not(None))).scalars():
        assets_by_resource[asset.resource_id] = asset
    if not assets_by_resource:
        logger.info("graph_phase2: AFFECTED_BY_CVE — no r7_assets with resource_id; skipping")
        return 0

    written = 0
    for item in session.execute(select(VulnQueueItem)).scalars():
        asset = assets_by_resource.get(item.resource_id)
        if asset is None:
            continue
        cve_id = (item.cve_id or "").strip().upper()
        if not cve_id:
            continue
        asset_node = upsert_node(
            session,
            node_type=GraphNodeType.R7_ASSET,
            natural_key=str(asset.r7_asset_id),
            name=asset.hostname or asset.ip or str(asset.r7_asset_id),
            source="rapid7",
            resource_id=asset.resource_id,
            attributes={
                "r7_asset_id": asset.r7_asset_id,
                "ip": asset.ip,
                "hostname": asset.hostname,
                "os": asset.os,
            },
        )
        cve_node = upsert_node(
            session,
            node_type=GraphNodeType.CVE,
            natural_key=cve_id,
            name=cve_id,
            source="rapid7",
            attributes={"cve_id": cve_id},
        )
        upsert_edge(
            session,
            source_id=asset_node.id,
            target_id=cve_node.id,
            edge_type=GraphEdgeType.AFFECTED_BY_CVE,
            method=GraphEdgeMethod.DECLARED,
            confidence=CONFIDENCE_DECLARED,
            source=EMITTER_AFFECTED_BY_CVE,
            evidence={
                "join": "vuln_queue.resource_id == r7_assets.resource_id (shared resources.id FK)",
                "cve_id": cve_id,
                "severity": item.severity,
                "vuln_queue_status": item.status,
                "r7_asset_id": asset.r7_asset_id,
            },
        )
        written += 1
    logger.info("graph_phase2: AFFECTED_BY_CVE emitted %d edges", written)
    return written


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def emit_all(session: Any) -> tuple[dict[str, int], list[str]]:
    """Run all three Phase 2 emitters; return ``(counts, errors)``.

    Each emitter is independently guarded so one failing domain does not abort
    the other two — mirroring the per-block error isolation in
    ``graph_maintenance``. A failure is NOT reported as a successful zero: it
    is logged AND surfaced in the returned ``errors`` list, so a caller can
    distinguish "nothing to emit" from "the emitter blew up".

    Each emitter also runs inside its own ``session.begin_nested()`` SAVEPOINT:
    on real PostgreSQL (unlike sqlite) a failed ``session.flush()`` leaves the
    connection in ``InFailedSqlTransaction`` until a ``ROLLBACK TO SAVEPOINT``
    recovers it — without this, "one failing domain does not abort the other
    two" above would be false the first time this ran against Postgres.
    """
    counts: dict[str, int] = {}
    errors: list[str] = []
    for edge_type, fn in (
        (GraphEdgeType.HOSTED_ON, emit_hosted_on),
        (GraphEdgeType.MOUNTS_DATASTORE, emit_mounts_datastore),
        (GraphEdgeType.AFFECTED_BY_CVE, emit_affected_by_cve),
    ):
        try:
            with session.begin_nested():
                counts[edge_type.value] = fn(session)
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph_phase2: %s emitter failed: %s", edge_type.value, exc)
            counts[edge_type.value] = 0
            errors.append(f"{edge_type.value}: {exc}")
    return counts, errors


__all__ = [
    "EMITTER_AFFECTED_BY_CVE",
    "EMITTER_HOSTED_ON",
    "EMITTER_MOUNTS_DATASTORE",
    "emit_affected_by_cve",
    "emit_all",
    "emit_hosted_on",
    "emit_mounts_datastore",
    "retire_edges",
    "upsert_edge",
    "upsert_node",
    "vsphere_key",
]
