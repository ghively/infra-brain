"""Relationship Graph Phase 2 schema — ``graph_nodes`` / ``graph_edges``.

This is the storage layer for GitLab issue #126 (Relationship Graph Phase 2).
It is DELIBERATELY a separate pair of tables from the pre-existing
``resource_relationships`` edge table (``infra_brain/db/relationships.py``);
see the "Relationship to resource_relationships" note below before adding
anything here.

Design decisions carried verbatim from the issue
------------------------------------------------
* **Plain Postgres, not Neo4j / Apache AGE.** Recursive-CTE traversal is
  sufficient at this scale, and a second datastore would mean duplicating
  audit/backup/DR controls under PCI scope. AGE is revisited only if CTEs
  prove insufficient — not now.
* **Node types are per-source, never a unified ``Host``.** ``VsphereVM``,
  ``AnsibleManagedHost``, ``R7Asset``, ``OctopusMachine`` are distinct node
  types even when they describe the same physical box. Cross-source entity
  resolution (an ``IS_SAME_AS``-style link between per-source nodes) is
  explicitly separate, later work — nothing in this module merges identities.
* **Every edge records HOW it was derived.** ``method`` is one of
  ``declared`` / ``deterministic_match`` / ``probabilistic_match`` and
  ``confidence`` must honestly reflect it: ``1.000`` is reserved for facts
  the upstream source itself declares (an FK-strength join in our own
  schema), and anything we *derived* — including an exact string match —
  scores below 1.000.
* **Edges are bitemporal.** ``valid_from`` / ``valid_to`` model when the
  relationship was true in the world (``valid_to IS NULL`` = currently
  active); ``recorded_at`` is when we wrote the row. Edges are retired by
  stamping ``valid_to``, never by DELETE.

Relationship to ``resource_relationships`` (read before extending)
------------------------------------------------------------------
infra-brain already has a mature edge store: ``resource_relationships``,
written by ``graph_maintenance.py`` and several collectors, with ~70 edge
types including ``RUNS_ON`` (vm → esxi_host), ``STORED_ON`` (vm → datastore)
and ``VULNERABLE_TO`` (asset → vuln) — semantic overlaps of this phase's
``HOSTED_ON`` / ``MOUNTS_DATASTORE`` / ``AFFECTED_BY_CVE``. That table is
NOT superseded here and is not modified by this module.

The two differ in ways that could not be retrofitted onto
``resource_relationships`` without changing a live, widely-read table:

  1. Its endpoints are ``resources.id`` rows. This phase's endpoints are
     ``graph_nodes`` rows with an explicit per-source ``node_type``, so the
     "never a unified Host" rule is enforced by the schema rather than by
     convention in each emitter.
  2. It has no ``method`` column — provenance of an edge (declared vs.
     matched) is not recorded, so a name-matched edge and an FK-backed edge
     are indistinguishable to a reader.
  3. It is single-temporal (``created_at`` / ``last_seen`` / ``status``);
     this phase is bitemporal, which the Phase 3 traversal API needs in
     order to answer "what did the graph look like at time T".

``graph_nodes.resource_id`` deliberately points at the SAME ``resources.id``
UUID space the existing table uses, so the two stores are reconcilable
rather than divergent — a Phase 3 reader can join a ``graph_nodes`` row to
its ``resource_relationships`` neighbourhood. Whether the two eventually
converge is a Phase 3 (issue #127) decision, not one made here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONB, _uuid


# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------


class GraphNodeType(str, Enum):
    """Per-source node types. There is intentionally no unified ``Host``.

    Two node types describing the same physical machine (``VsphereVM`` and
    ``R7Asset``, say) stay separate rows forever unless a *future* entity-
    resolution phase links them with an explicit edge. Nothing in Phase 2
    merges them.
    """

    # --- Phase 2 emitters populate these -----------------------------------
    VSPHERE_VM = "VsphereVM"
    VSPHERE_HOST = "VsphereHost"  # ESXi host (hypervisor)
    VSPHERE_DATASTORE = "VsphereDatastore"
    R7_ASSET = "R7Asset"
    CVE = "Cve"  # shared, high-fan-in vulnerability node

    # --- Declared for the taxonomy; NO Phase 2 emitter ---------------------
    # Named in the issue's node-type list, but every Phase 2 edge type
    # (HOSTED_ON / MOUNTS_DATASTORE / AFFECTED_BY_CVE) has vSphere or Rapid7
    # endpoints, so nothing writes these yet. They are declared now so the
    # vocabulary is stable for whoever adds the next emitter.
    ANSIBLE_MANAGED_HOST = "AnsibleManagedHost"
    OCTOPUS_MACHINE = "OctopusMachine"


class GraphEdgeType(str, Enum):
    """Phase 2 + Phase 3 edge types.

    HARD SCOPE BOUNDARY: only these five. In particular ``DEFINED_IN_IAC``
    is deliberately absent and must NOT be added speculatively — Ansible
    playbook ``hosts:`` fields are unresolved raw strings / Jinja defaults
    with no structured link to inventory groups, so any such edge would be
    a guess dressed as a fact.

    ``NOT_SAME_AS`` is the ONE sanctioned amendment to that boundary (the
    KG-1/KG-3 authority-and-rejection design). It is neither speculative nor
    a guess dressed as a fact: it is the missing NEGATIVE half of the
    ``SAME_AS`` vocabulary, writable only by an accountable human (see
    ``GraphEdgeAuthority`` and ``graph_phase2.upsert_edge``'s rule W5).
    """

    HOSTED_ON = "HOSTED_ON"  # VsphereVM --HOSTED_ON--> VsphereHost
    MOUNTS_DATASTORE = "MOUNTS_DATASTORE"  # VsphereVM|VsphereHost --> VsphereDatastore
    AFFECTED_BY_CVE = "AFFECTED_BY_CVE"  # R7Asset --AFFECTED_BY_CVE--> Cve
    # Phase 3 (issue #127) entity resolution: two PER-SOURCE host nodes that
    # denote the same physical machine. This does NOT merge the nodes — the
    # "never a unified Host" rule from Phase 2 still holds; SAME_AS is an
    # explicit, provenance-carrying link BETWEEN the per-source rows, exactly
    # as the Phase 2 docstring above anticipated ("Cross-source entity
    # resolution ... is explicitly separate, later work").
    SAME_AS = "SAME_AS"  # <SourceA>Host <--SAME_AS--> <SourceB>Host
    # A named human's assertion that this PAIR is NOT the same entity, judged
    # against the evidence presented at rejection time. Pair-scoped (never
    # node-scoped), bitemporal, retractable, and consulted by every automatic
    # identity emitter before it emits. Machine "negative knowledge" stays what
    # it has always been — suppression (TRK-087), which asserts nothing — so
    # nothing automatic may write this type.
    NOT_SAME_AS = "NOT_SAME_AS"  # <SourceA>Host <--NOT_SAME_AS--> <SourceB>Host


class GraphEdgeAuthority(str, Enum):
    """On whose accountability an edge's assertion rests.

    A genuinely distinct axis from ``source`` and ``method``, and the reason
    both were wrong as authority discriminators:

    * ``source`` is a free-form ``String(128)`` EMITTER NAME. Using it as the
      authority marker means every write path needs a hardcoded
      emitter-precedence registry that each future emitter must remember to
      join, and any equality filter on one emitter string (as
      ``retract_same_as`` used to carry) silently breaks the moment a second
      human-confirmation path exists.
    * ``method`` cannot discriminate either: ``AFFECTED_BY_CVE`` is
      ``declared``/1.000 and fully automatic, so ``declared`` != human.

    Precedence is enforced in exactly one place —
    ``graph_phase2.upsert_edge`` — so no emitter can forget it.
    """

    AUTO = "auto"
    HUMAN = "human"


class GraphEdgeMethod(str, Enum):
    """How an edge was derived. Constrains what ``confidence`` may be.

    DECLARED
        The upstream source states the relationship and we resolved it
        through an FK-strength join inside our own schema (no string
        matching, no inference). Only this method may carry 1.000.
    DETERMINISTIC_MATCH
        Resolved by an exact, scoped equality on a non-key attribute (e.g.
        matching ``vsphere_vms.esxi_host`` against ``vsphere_hosts.name``
        within one vCenter). Reproducible and non-fuzzy, but the join key is
        a display name, so a rename or a name collision can produce a wrong
        edge. Must be < 1.000.
    PROBABILISTIC_MATCH
        Fuzzy / heuristic / scored matching. Must be well below 1.000.
        Unused in Phase 2 — no emitter here does fuzzy matching. Phase 3
        (issue #127) is the first producer, for fuzzy SAME_AS candidates
        that clear its auto-emit band.
    """

    DECLARED = "declared"
    DETERMINISTIC_MATCH = "deterministic_match"
    PROBABILISTIC_MATCH = "probabilistic_match"


#: Confidence for a ``declared`` edge — the only value that may be 1.000.
CONFIDENCE_DECLARED = Decimal("1.000")
#: Confidence for an exact-name deterministic match inside one scope.
#: Not 1.000: the join key is a mutable display name, so a rename or a
#: cross-vCenter name collision can mis-resolve it.
CONFIDENCE_DETERMINISTIC_NAME = Decimal("0.990")
#: Phase 3: confidence for a FUZZY (scored) cross-source SAME_AS match.
#: Justification for 0.800 specifically — it is pinned relative to the two
#: neighbouring tiers that already exist in this codebase rather than picked
#: for feel:
#:   * strictly below 0.990 (exact normalized-name equality) because a fuzzy
#:     match is, by construction, a name the normalizer could NOT reconcile;
#:   * strictly below host_reconcile's 0.95 hostname-convergence tier, which
#:     is backed by an exact normalized-key match plus a reconciler-verified
#:     HostIdentity row — strictly more evidence than a similarity score;
#:   * above host_reconcile's 0.70 cross-hostname bare-IP tier, because two
#:     hostname-BEARING sources whose names are >=0.90 similar is stronger
#:     evidence than a shared IP between sources that DISAGREE on hostname
#:     (DHCP churn / NAT / VM IP reassignment make bare-IP equality weak).
#: The gap to 0.990 is deliberately wide so a consumer's default
#: ``min_confidence`` filter excludes fuzzy identity claims by accident-proof
#: margin rather than by a hair.
CONFIDENCE_PROBABILISTIC_NAME = Decimal("0.800")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class GraphNode(Base):
    """One per-source entity in the relationship graph.

    Identity is ``(node_type, natural_key)``, where ``natural_key`` is the
    stable per-source identifier — NOT a display name:

      * ``VsphereVM`` / ``VsphereHost`` / ``VsphereDatastore``
        → ``"<vcenter>:<moref>"`` (the ``uq_vsphere_*_natkey`` pair)
      * ``R7Asset``        → ``str(r7_assets.r7_asset_id)``
      * ``Cve``            → the uppercased CVE id

    ``resource_id`` links back to the existing ``resources.id`` UUID space
    that every domain table (``vsphere_vms``, ``r7_assets``, …) already
    carries, so a node resolves to its originating domain resource without
    inventing a new join key. Nullable: shared value nodes such as ``Cve``
    may have no single owning resource.
    """

    __tablename__ = "graph_nodes"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    node_type: Mapped[str] = mapped_column(String(64), nullable=False)
    natural_key: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    #: Which collector/domain the node came from ("vsphere", "rapid7", …).
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    attributes: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # SET NULL, not CASCADE: losing the domain resource row must not
        # silently delete graph history (and would orphan its edges).
        ForeignKeyConstraint(
            ["resource_id"],
            ["resources.id"],
            ondelete="SET NULL",
            name="fk_graph_nodes_resource",
        ),
        UniqueConstraint("node_type", "natural_key", name="uq_graph_nodes_identity"),
        Index("ix_graph_nodes_type", "node_type"),
        Index("ix_graph_nodes_resource", "resource_id"),
    )


class GraphEdge(Base):
    """A directed, bitemporal, provenance-carrying edge between two nodes.

    Read as ``source`` *edge_type* ``target``: ``vm HOSTED_ON esxi_host``.

    Active edges are those with ``valid_to IS NULL``; a partial unique index
    enforces at most ONE active edge per
    ``(source_id, target_id, edge_type)`` while still allowing the full
    history of previously-retired instances of the same edge to coexist.
    """

    __tablename__ = "graph_edges"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    #: GraphEdgeType value, e.g. 'HOSTED_ON'.
    edge_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: GraphEdgeMethod value: 'declared' | 'deterministic_match' | 'probabilistic_match'.
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    #: [0, 1] with millesimal precision. 1.000 only for method='declared'.
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    #: What justifies this edge — the matched values, join path, severities.
    evidence: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    #: Which emitter wrote the edge (e.g. "graph_phase2.hosted_on"). Audit
    #: trail of the CODE PATH — never the authority discriminator, see
    #: ``GraphEdgeAuthority``.
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    #: GraphEdgeAuthority value: 'auto' | 'human'. server_default keeps every
    #: pre-existing row (all written by automatic emitters) correct without a
    #: backfill, and makes the column deploy-order-safe against old code.
    authority: Mapped[str] = mapped_column(
        String(16), nullable=False, default="auto", server_default="auto"
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: NULL = currently active. Retirement stamps this; edges are never deleted.
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The vocabulary is load-bearing (it gates who may overwrite whom) and
        # this table is small, so it is enforced in the DB, not only in Python.
        CheckConstraint(
            "authority IN ('auto', 'human')", name="ck_graph_edges_authority"
        ),
        ForeignKeyConstraint(
            ["source_id"],
            ["graph_nodes.id"],
            ondelete="CASCADE",
            name="fk_graph_edges_source",
        ),
        ForeignKeyConstraint(
            ["target_id"],
            ["graph_nodes.id"],
            ondelete="CASCADE",
            name="fk_graph_edges_target",
        ),
        # One ACTIVE edge per (source, target, type); retired duplicates
        # (valid_to NOT NULL) are unconstrained so history accumulates.
        Index(
            "uq_graph_edges_active",
            "source_id",
            "target_id",
            "edge_type",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
            sqlite_where=text("valid_to IS NULL"),
        ),
        Index("ix_graph_edges_source", "source_id"),
        Index("ix_graph_edges_target", "target_id"),
        Index("ix_graph_edges_type", "edge_type"),
    )


__all__ = [
    "CONFIDENCE_DECLARED",
    "CONFIDENCE_DETERMINISTIC_NAME",
    "CONFIDENCE_PROBABILISTIC_NAME",
    "GraphEdge",
    "GraphEdgeAuthority",
    "GraphEdgeMethod",
    "GraphEdgeType",
    "GraphNode",
    "GraphNodeType",
]
