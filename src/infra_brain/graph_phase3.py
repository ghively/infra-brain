"""Relationship Graph Phase 3 — entity resolution (SAME_AS) + traversal reads.

GitLab issue #127. Builds directly on Phase 2 (issue #126,
:mod:`infra_brain.graph_phase2` / :mod:`infra_brain.db.models.graph`) and does
NOT touch its three emitters (``HOSTED_ON`` / ``MOUNTS_DATASTORE`` /
``AFFECTED_BY_CVE``).

Two halves:

1. **Entity resolution** — link the per-source host nodes that denote the same
   physical machine with an explicit ``SAME_AS`` edge, in the order the issue
   prescribes: deterministic rules first, fuzzy scoring second, and an
   explicit human-review queue for the ambiguous middle. Nothing here ever
   silently force-merges: a candidate that is not clearly right becomes a
   queued question, never an edge.
2. **Traversal reads** — bounded, ranked, server-side-summarised blast-radius
   and root-cause queries over BOTH edge stores (see below), so the LLM-facing
   MCP tools never hand back a raw subgraph dump.

Why the resolution population is not hopeless
---------------------------------------------
The issue's own live-data finding: one CVE sample matched 0/55 hosts (a
workstation-scoped, genuinely disjoint population — no amount of cleverness
invents a match there) while another matched 68/358 once short-name
normalization was applied (server-class, same fleet). So: normalize properly,
be deterministic where the data supports it, and be explicit rather than
optimistic everywhere else.

Deliberate reuse (do not reinvent these)
----------------------------------------
* :func:`infra_brain.tools.hostmatch.normalize_host` — THE canonical
  normalizer (TRK-189). No second normalizer is defined in this module.
* :func:`infra_brain.tools.hostmatch.hosts_domain_conflict` — TRK-087's
  pairwise cross-domain false-merge guard, already used by
  ``host_reconcile``'s IS_SAME_AS passes. Applied here to every candidate
  pair, deterministic and fuzzy alike, BEFORE anything is emitted or queued.
* :func:`infra_brain.graph_phase2.upsert_node` /
  :func:`infra_brain.graph_phase2.upsert_edge` — the only write paths for
  graph nodes/edges. ``upsert_edge`` also enforces the confidence-honesty
  rule (1.000 only for ``method='declared'``), so this module does not
  re-check it.
* ``ProposedAction`` — the review queue. See "Review queue" below.

Relationship to ``resource_relationships`` (TRK-196 — CLOSED by P5)
-------------------------------------------------------------------
Phase 2 deferred "should the two edge stores converge?"; the graph-first
plan answered it in stages and P5 finished the job: the legacy store is
DROPPED (``docs/decisions/2026-08-11-graph-first-architecture.md``).
Traversal walks exactly one store, ``graph_edges``, and ``IS_SAME_AS``
lives there as ``SAME_AS`` under the authority model. Containment facts
(``HAS_MOUNT``, ``EXPOSES_PORT``, drift, …) were deliberately never
migrated as edges — consumers read the detail tables for them. See the
epitaph above :func:`_why` for what the two-store walk looked like and
why its identity re-entry machinery went with it.
* SAME_AS is written ONLY to ``graph_edges``, and — since P5's resolver
  switch — ONLY by this module's resolver. ``host_reconcile``'s ``IS_SAME_AS``
  emitters were deleted with the store they wrote to; it still builds
  ``HostIdentity`` merge records and ambiguity DriftEvents, but the graph's
  identity claims all carry recorded method/evidence and bitemporal validity
  from here.

Review queue
------------
No new table. ``ProposedAction`` is documented in the schema as the *generic*
human-gated approval primitive ("never auto-applied; status drives the flow"),
and is already reused for a non-remediation queue by ComplianceAgent's
``compliance_rule_gap`` rows. Phase 3 adds ``action_type=
"entity_resolution_same_as"``. This is safe against the remediation executor,
which only picks up ``action_type in ("config_fix", "vuln_patch")``, so a
queued identity question can never be executed as a remediation.

Read-only guarantee: this module reads infra-brain's own Postgres tables and
writes only ``graph_nodes`` / ``graph_edges`` / ``proposed_actions``. No
external system is contacted — no HTTP client, no pyvmomi, no subprocess.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import String, case, cast, func, literal, or_, select

from infra_brain.db.models import (
    CONFIDENCE_DECLARED,
    CONFIDENCE_DETERMINISTIC_NAME,
    CONFIDENCE_PROBABILISTIC_NAME,
    AnsibleInventoryHost,
    DriftEvent,
    GraphEdge,
    GraphEdgeAuthority,
    GraphEdgeMethod,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    HostIdentity,
    OctopusMachine,
    ProposedAction,
    R7Asset,
    Resource,
    VsphereVm,
)
from infra_brain.graph_phase2 import retire_edges, upsert_edge, upsert_node, vsphere_key
from infra_brain.tools.hostmatch import host_domain, hosts_domain_conflict, normalize_host

logger = logging.getLogger(__name__)

#: Written to ``graph_edges.source`` by the automated resolver.
EMITTER_SAME_AS = "graph_phase3.same_as"
#: Written to ``graph_edges.source`` by an operator confirmation.
EMITTER_SAME_AS_CONFIRMED = "graph_phase3.confirm_same_as"
#: Written to ``graph_edges.source`` by an operator rejection (the veto).
EMITTER_NOT_SAME_AS = "graph_phase3.reject_same_as"

#: ``ProposedAction.action_type`` for a queued identity question.
REVIEW_ACTION_TYPE = "entity_resolution_same_as"
#: ``ProposedAction.agent`` attribution for the same.
REVIEW_AGENT = "graph_phase3"
#: Statuses that count as "this question is already OPEN".
#:
#: KG-3: this used to include 'approved' and 'rejected', which made a single
#: answer silence the SOURCE NODE forever — a node rejected against source B
#: could never be asked about source C, and a node confirmed against B could
#: never be asked about C either. The real invariant is "at most one PENDING
#: question per node"; a decided pair is now excluded per-CANDIDATE instead
#: (see :func:`_filter_decided_candidates`), which is pair-scoped rather than
#: node-scoped. ComplianceAgent's ``_GAP_PROPOSAL_LIVE_STATUSES`` keeps its
#: own semantics — only this constant changes.
REVIEW_LIVE_STATUSES = ("pending",)

#: Ordered evidence-class scale for a rejection (§ the re-ask ladder). A
#: vetoed pair may be RE-ASKED — never auto-emitted — only when a later pass
#: produces evidence of a STRICTLY STRONGER class than the one the human
#: judged. ``hard_identifier`` is the top of the ladder, so a pair rejected
#: there is never re-asked automatically; only an explicit retraction or a
#: later confirmation reopens it.
EVIDENCE_CLASS_FUZZY = "fuzzy"
EVIDENCE_CLASS_EXACT_NAME = "exact_name"
EVIDENCE_CLASS_HARD_IDENTIFIER = "hard_identifier"
_EVIDENCE_CLASS_RANK: dict[str, int] = {
    EVIDENCE_CLASS_FUZZY: 0,
    EVIDENCE_CLASS_EXACT_NAME: 1,
    EVIDENCE_CLASS_HARD_IDENTIFIER: 2,
}

# ---------------------------------------------------------------------------
# Scoring thresholds
# ---------------------------------------------------------------------------
# The fuzzy score (see _score_pair) is only ever consulted for pairs the
# deterministic pass could NOT resolve, i.e. pairs whose normalized short
# names are already known to differ.
#
#: >= this score → auto-emit a probabilistic SAME_AS edge.
#: 0.90 on the blended metric means the two names differ by roughly one short
#: token or a couple of characters out of a typical 8-14 character hostname
#: ("web01a" vs "web-01a", "sqlprod2" vs "sql-prod2") — the near-miss class
#: the issue explicitly wants the fuzzy pass to catch.
FUZZY_AUTO_EMIT_MIN = 0.90
#: [FUZZY_REVIEW_MIN, FUZZY_AUTO_EMIT_MIN) → REVIEW QUEUE, never an edge.
#: 0.75 is the "plausibly the same box, plausibly two boxes in one naming
#: series" band ("web01" vs "web02" scores here) — precisely the case where a
#: silent merge would be a data-integrity incident, so a human decides.
FUZZY_REVIEW_MIN = 0.75
#: Below FUZZY_REVIEW_MIN the pair is discarded outright: not emitted, not
#: queued. Queueing every low-similarity pair would make the queue O(n^2)
#: noise and guarantee nobody reads it.

#: Cap on queued candidates per source node, worst-case fan-out protection.
MAX_REVIEW_CANDIDATES = 10

#: Hard ceiling on traversal hops, independent of caller input. Mirrors the
#: bounded-walk discipline of ``db.relationships._MAX_ALLOWED_DEPTH`` (6) but
#: tighter: the issue specs 2-3 hop traversal, and every extra hop multiplies
#: fan-out through high-degree nodes such as a shared ``Cve``.
MAX_HOPS = 3
#: Hard ceiling on rows any traversal returns to an LLM, whatever top_n says.
MAX_TOP_N = 100

#: Node types that denote a host-shaped machine and are therefore SAME_AS
#: candidates. Value nodes (``Cve``) and non-machine vSphere objects
#: (``VsphereDatastore``) are excluded by construction.
#:
#: THIS TUPLE IS THE RESOLVER'S ENTIRE FIELD OF VIEW. ``resolve_entities`` loads
#: nothing else, so a host-shaped node type missing from here is not "uncertain,
#: therefore queued" — it is INVISIBLE: no node loaded, no candidate scored, no
#: review question asked. That failure is silent by construction, and it
#: happened: ``agents/linux.py`` declared ``LinuxHost``, the live estate
#: materialised seven of them, and every linux↔anything pair stayed unseen
#: because nothing connected the declaration to this tuple (P5 / T1's GAP 1).
#:
#: WHY IT IS STILL AN EXPLICIT TUPLE AND NOT DERIVED. Deriving it from
#: ``etl.spec.declared_host_identity_node_types()`` would mean calling
#: ``agent_specs()``, which resolves the whole ``AGENT_REGISTRY`` and imports
#: every collector module — wrong inside anything request-path (an objection
#: first recorded at the late ``GRAPH_SERVED_EDGE_TYPES``, which P5 removed
#: with the legacy walk), and worse here because this constant is read
#: at import time by ``materialize_host_nodes`` and exported in ``__all__``.
#: Instead the DECLARATION exists (``NodeSpec.is_host_identity``) and the two
#: sides are compared by a test: ``tests/test_p5_issameas_resolver_coverage.py``
#: fails if a live collector declares a host node this tuple has not been
#: widened for. Explicit set + mechanical guard, so widening stays a decision on
#: the record while forgetting to widen is no longer possible.
HOST_NODE_TYPES: tuple[str, ...] = (
    # Hand-written Phase 2 emitters (``materialize_host_nodes``, below).
    GraphNodeType.VSPHERE_VM.value,
    GraphNodeType.R7_ASSET.value,
    GraphNodeType.ANSIBLE_MANAGED_HOST.value,
    GraphNodeType.OCTOPUS_MACHINE.value,
    # Declarative NodeSpecs (``AgentSpec.emits_nodes``, materialised by
    # ``graph_engine``). Free strings, not ``GraphNodeType`` members, because
    # that enum is a closed vocabulary for the hand-written emitters — see
    # ``NodeSpec.type``. Each is declared ``is_host_identity=True`` by its
    # collector; the guard test holds these two lists together.
    "LinuxHost",
    "NetDiscoveredHost",
)

#: The subset of :data:`HOST_NODE_TYPES` that :func:`materialize_host_nodes`
#: itself writes. The declarative types are materialised by ``graph_engine``
#: from their ``NodeSpec``s, so reporting them in this function's per-type counts
#: would claim a zero it never even looked for.
_PHASE2_MATERIALIZED_HOST_TYPES: tuple[str, ...] = (
    GraphNodeType.VSPHERE_VM.value,
    GraphNodeType.R7_ASSET.value,
    GraphNodeType.ANSIBLE_MANAGED_HOST.value,
    GraphNodeType.OCTOPUS_MACHINE.value,
)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Node materialisation for the two declared-only Phase 2 node types
# ---------------------------------------------------------------------------


def materialize_host_nodes(session: Any) -> dict[str, int]:
    """Ensure a ``graph_nodes`` row exists for every host-shaped source row.

    Phase 2's emitters only ever created ``VsphereVM`` and ``R7Asset`` nodes
    (its three edge types have vSphere/Rapid7 endpoints), leaving
    ``AnsibleManagedHost`` and ``OctopusMachine`` declared in the vocabulary
    but unwritten. Entity resolution across all four sources needs all four
    materialised, so this fills the gap — additively, via Phase 2's own
    ``upsert_node``, touching none of its emitters.

    Returns per-node-type counts of rows upserted.

    Natural keys (stable per-source identifiers, never display names, per the
    ``GraphNode`` contract):

    * ``VsphereVM``          ``"<vcenter>:<moref>"`` (same as Phase 2)
    * ``R7Asset``            ``str(r7_assets.r7_asset_id)`` (same as Phase 2)
    * ``OctopusMachine``     ``octopus_machines.octopus_id``
    * ``AnsibleManagedHost`` the lowercased inventory host name. Ansible
      inventory has no per-host id of its own — its uniqueness is
      ``(group_id, name)`` — and one host legitimately appears in many groups,
      so keying on the group would mint N nodes for one machine. The name IS
      the identity in Ansible inventory, so the name (lowercased, trailing
      dot stripped) is the key. Placeholder names that ``normalize_host``
      rejects are skipped rather than given a node.
    """
    counts = {t: 0 for t in _PHASE2_MATERIALIZED_HOST_TYPES}

    for vm in session.execute(select(VsphereVm)).scalars():
        if vm.is_template:
            continue
        upsert_node(
            session,
            node_type=GraphNodeType.VSPHERE_VM,
            natural_key=vsphere_key(vm.vcenter, vm.moref),
            name=vm.name,
            source="vsphere",
            resource_id=vm.resource_id,
            attributes={
                "vcenter": vm.vcenter,
                "moref": vm.moref,
                "power_state": vm.power_state,
                # GitLab issue #168: hard identifiers, already on the model,
                # just never copied into graph_nodes.attributes before now --
                # this is what lets _score_candidate corroborate a match
                # independent of name similarity.
                "uuid": vm.uuid,
                "instance_uuid": vm.instance_uuid,
                # KG-5: vSphere's own hard identifiers (uuid/instance_uuid)
                # and Rapid7's (mac) never shared a populated field on ANY
                # real cross-source pair -- no other node type ever set
                # uuid/instance_uuid, and vSphere never set mac -- so
                # _first_matching_identifier's intersection was empty for
                # every real vSphere<->Rapid7 pair and this corroboration
                # path was dead code despite being its documented flagship
                # case. The collector already fetches guest.net (used until
                # now only for all_ips) and rides its NIC MACs along in
                # ``details["mac_addresses"]`` (no schema change -- existing
                # JSONB overflow); the first one is the primary-NIC MAC, the
                # same "one scalar representative value" precedent as
                # ``ip``/``ip_address`` beside the full ``all_ips`` list.
                "mac": ((vm.details or {}).get("mac_addresses") or [None])[0],
                "ip": vm.ip_address,
                "ip_address": vm.ip_address,
                "all_ips": vm.all_ips or [],
                "guest_hostname": vm.guest_hostname,
                # GitLab issue #168's "differing OS" counter-evidence check in
                # _score_candidate reads attrs["os"] on BOTH sides of a pair —
                # without this, R7Asset was the only host-shaped node type
                # that ever populated "os", so os_a and os_b could never both
                # be truthy for any real cross-source candidate pair and the
                # check silently never fired. guest_full_name is vSphere's
                # OS string (e.g. "Ubuntu Linux (64-bit)"), the VM-side
                # equivalent of R7Asset.os.
                "os": vm.guest_full_name,
            },
        )
        counts[GraphNodeType.VSPHERE_VM.value] += 1

    for asset in session.execute(select(R7Asset)).scalars():
        upsert_node(
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
                # Hard identifier (issue #168) -- Rapid7 is the only source
                # here that carries a NIC MAC.
                "mac": asset.mac,
            },
        )
        counts[GraphNodeType.R7_ASSET.value] += 1

    for machine in session.execute(select(OctopusMachine)).scalars():
        upsert_node(
            session,
            node_type=GraphNodeType.OCTOPUS_MACHINE,
            natural_key=machine.octopus_id,
            name=machine.name,
            source="octopus",
            resource_id=machine.resource_id,
            attributes={
                "octopus_id": machine.octopus_id,
                "status": machine.status,
                "is_disabled": machine.is_disabled,
                "roles": machine.roles,
            },
        )
        counts[GraphNodeType.OCTOPUS_MACHINE.value] += 1

    seen_ansible: set[str] = set()
    for host in session.execute(select(AnsibleInventoryHost)).scalars():
        raw = (host.name or "").strip().lower().rstrip(".")
        if not raw or raw in seen_ansible:
            continue
        if not normalize_host(raw):
            # Placeholder / generic name (localhost, template, …) — never a
            # stable identity, so never a node (KG-6 rationale).
            continue
        seen_ansible.add(raw)
        upsert_node(
            session,
            node_type=GraphNodeType.ANSIBLE_MANAGED_HOST,
            natural_key=raw,
            name=host.name,
            source="ansible",
            attributes={"inventory_name": host.name},
        )
        counts[GraphNodeType.ANSIBLE_MANAGED_HOST.value] += 1

    logger.info("graph_phase3: materialized host nodes %s", counts)
    return counts


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _tokens(name: str) -> set[str]:
    """Split a hostname into comparable tokens.

    Separators AND digit/letter boundaries both split, so ``web01`` and
    ``web-01`` tokenize identically (``{"web", "01"}``) — the single most
    common real spelling difference between two inventories of the same
    fleet.
    """
    out: list[str] = []
    current = ""
    kind: str | None = None
    for ch in name:
        if ch.isalnum():
            ch_kind = "d" if ch.isdigit() else "a"
            if kind is not None and ch_kind != kind:
                out.append(current)
                current = ""
            current += ch
            kind = ch_kind
        else:
            if current:
                out.append(current)
            current = ""
            kind = None
    if current:
        out.append(current)
    return {t for t in out if t}


def _score_pair(name_a: str, name_b: str) -> float:
    """Blended similarity in [0, 1] for two hostnames. Higher = more alike.

    ``0.6 * SequenceMatcher.ratio + 0.4 * token Jaccard``, computed on the
    normalized short names.

    Why blended, and why this weighting: edit-distance alone rates ``web01``
    vs ``web02`` very highly (one character in six) even though a numeric
    suffix is exactly what distinguishes two *different* machines in a naming
    series; token overlap alone rates ``web01`` vs ``web-01`` as identical
    (good) but is blind to typos and cannot rank near-misses at all. Mixing
    them, character similarity dominant, gives a metric where punctuation and
    zero-padding differences score high while a changed numeric token drags
    the score down into the review band instead of the auto-emit band — which
    is the discrimination the auto-emit/review threshold actually needs. The
    weights are a deliberate, documented choice, not a tuned parameter: no
    labelled match corpus exists in this repo to tune against, so they are
    held simple and explainable, and every emitted edge records its score in
    ``evidence`` so a later pass can retune against real outcomes.

    Returns 0.0 if either side normalizes to empty (placeholder names).
    """
    a = normalize_host(name_a)
    b = normalize_host(name_b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = _tokens(a), _tokens(b)
    jaccard = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return round(0.6 * seq + 0.4 * jaccard, 4)


# ---------------------------------------------------------------------------
# Candidate scoring — hard-identifier corroboration (GitLab issue #168)
# ---------------------------------------------------------------------------
#
# Root cause of #168: the resolver used to score candidate pairs on name
# string similarity ALONE (the old ``_score_pair(name_a, name_b)`` took two
# bare strings and could never see an identifier even when one existed).
# That made the fuzzy-review population backwards: a SINGLE queued candidate
# meant "nothing else fuzzy-matched this name" -- the WEAKER signal -- not
# "this one is confirmed," while genuinely tied multi-candidate rows (from an
# ambiguous normalized-name key) sat at a hardcoded 1.0 with no way to tell
# them apart. ``_score_candidate`` fixes this by taking the actual
# ``GraphNode`` objects (not strings) so it can read the hard identifiers
# ``materialize_host_nodes`` already puts on ``GraphNode.attributes`` --
# vSphere ``uuid``/``instance_uuid``, Rapid7 ``mac`` -- and score based on
# those FIRST, falling back to name similarity only when no identifier is
# available on either side.

#: attrs keys treated as identity-grade: an exact match on any ONE of these
#: is corroboration strong enough to float a pair into the auto-emit band
#: independent of how different the two node names look. Order is
#: significance order for ``evidence["corroborating_identifier"]`` when more
#: than one happens to match: a vSphere ``uuid``/``instance_uuid`` is
#: globally unique and assigned once, so it outranks a MAC (NIC-scoped, but
#: can be reused across a network-card swap) and a serial (chassis-scoped,
#: meaningless for most VMs).
_HARD_IDENTIFIER_FIELDS: tuple[str, ...] = ("uuid", "instance_uuid", "mac", "serial")

#: Fields checked for a MISMATCH (not a match) between two nodes' attributes
#: -- present-but-different is counter-evidence even though absent-on-one-side
#: is not.
_MISMATCH_ONLY_FIELDS: tuple[str, ...] = ("moref",)

#: A last_seen gap wider than this between two candidate nodes is
#: counter-evidence: one side may no longer even be the same live machine.
_LAST_SEEN_GAP_COUNTER_EVIDENCE = timedelta(days=90)


# ---------------------------------------------------------------------------
# host_reconcile -> graph_phase3: unsettled identity legs as counter-evidence
# ---------------------------------------------------------------------------
#
# TRK-341, design spec 2026-08-10 §4.4 — the RETURN direction of the
# cross-module contract. ``host_reconcile`` persists, per short hostname, the
# source labels whose leg was the coin-flip survivor of a SAME-SOURCE identity
# collision (``host_identities.identity_ambiguous_sources``, recomputed every
# run). Its own emitters already decline to assert a 0.95 ``IS_SAME_AS`` over
# such a leg. Until now nothing carried that fact back the other way: this
# module's ``_score_candidate`` scored every pair as if both legs were settled,
# so persisted ambiguity SUPPRESSED emission in one store while leaving the
# score that decides "does this even look like a match" untouched in the other.
#
# Two things happen when a leg is unsettled, and they are deliberately
# different in kind:
#
#   * counter-evidence (unconditional) — a string a reviewer reads, and the
#     signal the existing machinery already routes on: non-empty
#     ``counter_evidence`` diverts a pair to the review queue instead of
#     auto-emitting it, in BOTH passes. This alone is what §4.4 specifies and
#     it is what actually stops the edge.
#   * a bounded score penalty — the machine's own belief, lowered. The
#     endpoint on an unsettled leg may not even be the entity we think it is,
#     so "how much does this look like a match" is genuinely lower. The
#     penalty is FLOORED at :data:`FUZZY_REVIEW_MIN` for any pair that scored
#     at or above it, because being MORE cautious about asserting a match must
#     not turn into asking the human FEWER questions — a pair that silently
#     dropped below the review floor would take the ambiguity out of sight,
#     which is the failure mode KG-3 exists to prevent.


#: ``host_identities`` leg column -> ``host_reconcile`` source label, derived
#: from the schema rather than duplicating that agent's ``_SOURCE_KEYS`` (which
#: cannot be imported here — ``host_reconcile`` imports FROM this module).
_HOST_IDENTITY_LEG_COLUMNS: dict[str, str] = {
    col.name: col.name[: -len("_resource_id")]
    for col in HostIdentity.__table__.columns
    if col.name.endswith("_resource_id")
}

#: How much an unsettled leg costs a candidate's score: exactly the width of
#: the review band, so a would-be auto-emit lands at the BOTTOM of the review
#: band rather than anywhere below it. Expressed from the thresholds, not as a
#: loose constant, so retuning either threshold keeps that property.
_AMBIGUOUS_LEG_SCORE_PENALTY = round(FUZZY_AUTO_EMIT_MIN - FUZZY_REVIEW_MIN, 4)


class AmbiguousLegIndex:
    """Which graph nodes sit on an unsettled ``host_identities`` leg.

    Built ONCE per resolution pass (:func:`ambiguous_leg_index`) and handed to
    every :func:`_score_candidate` call, rather than queried per pair: asked
    per pair this would be a round trip for every candidate on the fleet.

    A node is matched by ``resource_id``, not by its ``source`` string. The two
    vocabularies genuinely differ (this module's nodes say ``rapid7``,
    ``host_identities`` says ``r7``; ``AnsibleManagedHost`` has no leg at all),
    and the leg column that actually holds a node's ``resource_id`` answers
    "which source label is this node, in host_reconcile's terms" exactly,
    without inventing a translation table that could drift.
    """

    __slots__ = ("_by_resource", "hits")

    def __init__(self, by_resource: dict[str, str]) -> None:
        self._by_resource = by_resource
        #: How many times a leg was reported unsettled, for the pass counters.
        self.hits = 0

    def unsettled_label(self, node: GraphNode) -> str | None:
        """The source label of ``node``'s leg if that leg is unsettled.

        ``None`` means settled *or* not reconciled at all — a node with no
        ``resource_id`` (``AnsibleManagedHost``) contributes no signal, because
        the absence of a leg is not evidence of an unsettled one.
        """
        rid = getattr(node, "resource_id", None)
        if rid is None:
            return None
        label = self._by_resource.get(str(rid))
        if label is not None:
            self.hits += 1
        return label


def ambiguous_leg_index(session: Any) -> AmbiguousLegIndex:
    """Load every unsettled identity leg into one lookup.

    FAIL CLOSED: this deliberately does NOT catch load errors. If ambiguity
    state cannot be read, the caller's whole pass aborts before a single edge
    is written, and the next scheduled pass retries. Swallowing the error into
    an empty index would read as "nothing is ambiguous, emit freely" — the one
    interpretation that can assert a match over a coin flip, and (via
    ``upsert_edge``) over a human decision.
    """
    by_resource: dict[str, str] = {}
    rows = (
        session.execute(
            select(HostIdentity).where(HostIdentity.identity_ambiguous_sources.is_not(None))
        )
        .scalars()
        .all()
    )
    for row in rows:
        flagged = {str(s) for s in (row.identity_ambiguous_sources or [])}
        if not flagged:
            continue
        for column, label in _HOST_IDENTITY_LEG_COLUMNS.items():
            if label not in flagged:
                continue
            rid = getattr(row, column, None)
            if rid is not None:
                by_resource[str(rid)] = label
    return AmbiguousLegIndex(by_resource)


def _first_matching_identifier(
    attrs_a: dict[str, Any], attrs_b: dict[str, Any]
) -> tuple[str, Any] | None:
    """Return ``(field, value)`` for the first hard identifier both sides
    share (case-insensitive, blank values never count), or ``None``."""
    for field in _HARD_IDENTIFIER_FIELDS:
        va, vb = attrs_a.get(field), attrs_b.get(field)
        if va and vb and str(va).strip().lower() == str(vb).strip().lower():
            return field, va
    return None


# ---------------------------------------------------------------------------
# Shared-IP correlation guards (P5 GAP 2 — inherited from TRK-062, not invented)
# ---------------------------------------------------------------------------
#
# The retired ``host_reconcile._emit_cross_hostname_ip_edges`` carried two guards
# that were properties of that pass, not of the IP signal, so they would have
# died with it. They are ported here instead, because the pair class did not stop
# being dangerous when it moved from an assertion to a question — a review queue
# full of noise is not a safer failure than a wrong edge, it is a queue nobody
# reads, which is the same outcome with better paperwork.
#
#   1. NON-ROUTABLE ADDRESSES ARE NOT IDENTITIES. Every host reports its own
#      127.0.0.1; link-local and unspecified addresses are not routable at all.
#      Correlating on one would pair every machine in the estate with every
#      other. (Was ``_is_correlatable_ip``.)
#   2. AN IP THREE OR MORE HOSTS CLAIM IS NAT / DHCP REUSE, not a coincidence
#      worth asking about. The old pass refused such an IP outright; here the
#      REVIEW FLOOR is withheld, so the pair falls back to what its names alone
#      justify. Promoting it would fan out C(n,2) questions from one shared
#      address.


def _correlatable_ips(attrs: dict[str, Any]) -> set[str]:
    """The addresses on a node that are stable enough to correlate identity on.

    Loopback / unspecified / link-local / multicast and anything unparseable are
    dropped. Reads the same three attribute shapes ``_score_candidate`` does:
    the scalar ``ip``, its alias ``ip_address``, and the ``all_ips`` list.
    """
    raw: list[Any] = [attrs.get("ip"), attrs.get("ip_address")]
    raw.extend(attrs.get("all_ips") or [])
    out: set[str] = set()
    for value in raw:
        if not value:
            continue
        text = str(value).strip().lower()
        try:
            addr = ipaddress.ip_address(text)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_unspecified or addr.is_link_local or addr.is_multicast:
            continue
        out.add(text)
    return out


class SharedIpIndex:
    """How many host nodes claim each correlatable IP, for ONE resolution pass.

    Built once (:func:`shared_ip_index`) and handed to every scored pair, for the
    same reason :class:`AmbiguousLegIndex` is: asked per pair, "how many nodes
    hold this address" is a round trip per candidate.

    ``None`` in place of an index means "no reason to believe any address is
    widely shared" — the conservative default for callers whose result does not
    become an edge, matching how ``ambiguity=None`` scores every leg as settled.
    """

    __slots__ = ("_counts",)

    #: At or above this many claimants an address is infrastructure (NAT gateway,
    #: DHCP lease churn, a shared VIP), not an identity. Two is the only count
    #: that means "these two might be one machine".
    AMBIGUOUS_CLAIMANTS = 3

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def any_uniquely_shared(self, shared: set[str]) -> bool:
        """Is any of these shared addresses claimed by no THIRD node?

        Unknown addresses count as uniquely shared (``.get(ip, 2)``): an index
        that has never seen an address has no evidence it is widely claimed, and
        inventing that evidence would silently suppress correlation.
        """
        return any(self._counts.get(ip, 2) < self.AMBIGUOUS_CLAIMANTS for ip in shared)


def shared_ip_index(nodes: Sequence[GraphNode]) -> SharedIpIndex:
    """Count correlatable-IP claimants across the pass's whole node population.

    Counts NODES, not appearances: a node listing one address twice (as ``ip``
    and inside ``all_ips``) claims it once, which is why
    :func:`_correlatable_ips` returns a set.
    """
    counts: dict[str, int] = {}
    for node in nodes:
        for ip in _correlatable_ips(node.attributes or {}):
            counts[ip] = counts.get(ip, 0) + 1
    return SharedIpIndex(counts)


def _score_candidate(
    node_a: GraphNode,
    node_b: GraphNode,
    *,
    ambiguity: AmbiguousLegIndex | None = None,
    shared_ips: SharedIpIndex | None = None,
) -> tuple[float, dict[str, Any], list[str]]:
    """Score one candidate SAME_AS pair. Returns ``(score, evidence,
    counter_evidence)``.

    * ``score`` -- in [0, 1]. A matched hard identifier
      (:data:`_HARD_IDENTIFIER_FIELDS`) floors the score at
      :data:`FUZZY_AUTO_EMIT_MIN` regardless of name similarity -- a renamed
      VM with the same vSphere ``instanceUuid`` is still the same VM. A
      shared IP (either the primary address or an overlap in ``all_ips``) is
      weaker evidence (DHCP churn / NAT / reassignment) so it nudges the
      name-similarity score up, capped strictly BELOW
      :data:`FUZZY_AUTO_EMIT_MIN` — it can never auto-emit — while also
      floored at exactly :data:`FUZZY_REVIEW_MIN` so the pair reaches the
      REVIEW QUEUE rather than being dropped unasked (P5 GAP 2 / TRK-062; see
      the inline note at the branch). ``evidence["ip_floor_applied"]`` marks a
      score that came from that floor rather than from name similarity.
      Otherwise falls back entirely to :func:`_score_pair`.
    * ``evidence`` -- which identifier (if any) corroborated the match, so a
      reviewer -- or a future retune -- sees WHY, not just a number.
    * ``counter_evidence`` -- human-readable conflict strings a reviewer must
      weigh: conflicting IP, differing OS, cross-domain hostname (reusing
      :func:`infra_brain.tools.hostmatch.hosts_domain_conflict`), a mismatched
      hard identifier that ALSO happened to match on another field, a
      disabled/decommissioned flag on either side, or a large last-seen gap.
      Non-empty ``counter_evidence`` means: do not auto-merge even if the
      score alone says auto-emit -- callers must route the pair to the review
      queue instead.

    ``ambiguity`` (TRK-341, spec §4.4) carries host_reconcile's persisted
    same-source-collision flags. Passing it is what lets an UNSETTLED identity
    leg act as counter-evidence and lower the score; omitting it scores the
    pair as if every leg were settled, which is correct only where no decision
    rides on the result (:func:`_evidence_class_for_pair` reads ``evidence``
    alone). :func:`resolve_entities` -- the one caller whose result becomes an
    edge -- always passes it.

    ``shared_ips`` (P5) carries the pass-wide count of claimants per correlatable
    address, and withholds the review floor for an address three or more hosts
    claim -- NAT, a DHCP lease reused, a shared VIP. Omitting it means "no reason
    to think any address is widely shared", the same conservative default
    ``ambiguity=None`` takes; :func:`resolve_entities` always passes it.
    """
    attrs_a = node_a.attributes or {}
    attrs_b = node_b.attributes or {}
    evidence: dict[str, Any] = {}
    counter_evidence: list[str] = []

    name_score = _score_pair(node_a.name, node_b.name)
    score = name_score

    ip_a = attrs_a.get("ip") or attrs_a.get("ip_address")
    ip_b = attrs_b.get("ip") or attrs_b.get("ip_address")
    # P5: the address sets are filtered to CORRELATABLE addresses before they are
    # compared at all — loopback / unspecified / link-local / multicast are not
    # identities in either direction. Every host reports its own 127.0.0.1, so
    # counting that as an overlap would raise the score of every pair in the
    # estate; counting it as a CONFLICT (the else branch below) would be just as
    # wrong in the other direction. Previously both happened.
    corr_a = _correlatable_ips(attrs_a)
    corr_b = _correlatable_ips(attrs_b)
    shared_addresses = corr_a & corr_b
    ip_overlap = bool(shared_addresses)
    # ...and an address three or more hosts claim is a NAT gateway, a reused
    # DHCP lease or a shared VIP: real, and evidence of nothing. It is reported
    # (``ip_overlap``, so a reviewer sees it) but never scored.
    ip_is_evidence = ip_overlap and (
        shared_ips.any_uniquely_shared(shared_addresses) if shared_ips is not None else True
    )

    matched = _first_matching_identifier(attrs_a, attrs_b)
    if matched is not None:
        field, value = matched
        score = max(name_score, FUZZY_AUTO_EMIT_MIN)
        evidence["corroborating_identifier"] = field
        evidence[field] = value
    elif ip_is_evidence:
        # TRK-062 / P5 GAP 2. A shared correlatable IP is a NUDGE, capped below
        # FUZZY_AUTO_EMIT_MIN, because bare-IP equality is spoofable and reused
        # (DHCP churn / NAT / VM reassignment) — that cap is unchanged and is
        # what keeps this class out of auto-emit forever.
        #
        # What changed: the nudge is now also FLOORED at FUZZY_REVIEW_MIN. Two
        # sources that name a machine differently but agree on one routable
        # address are exactly the pair class host_reconcile's retired
        # ``_emit_cross_hostname_ip_edges`` asserted at 0.70. Before this floor
        # the resolver did not merely decline to assert it — the pair scored
        # below the review floor and was DROPPED, so the question was never put
        # to a human at all, in either store. Deleting the writer without this
        # would have removed a capability rather than relocated it.
        #
        # The floor deliberately lands at EXACTLY FUZZY_REVIEW_MIN, the bottom
        # of the review band: the weakest thing the system will ask about, and
        # still the weakest thing it will never assert. It is a REVIEW
        # PROMOTION, not a confidence claim; ``ip_floor_applied`` records that
        # the number came from this rule rather than from name similarity, so a
        # reviewer reading 0.75 is not misled into thinking the names matched.
        #
        # Both guards are applied EARLIER, at ``ip_is_evidence`` — a
        # non-correlatable or widely-claimed address does not reach this branch
        # at all, so it moves neither the nudge nor the floor. They are inherited
        # from the retired pass rather than invented; see _correlatable_ips and
        # SharedIpIndex.
        nudged = min(FUZZY_AUTO_EMIT_MIN - 0.01, name_score + 0.15)
        score = max(name_score, nudged)
        if score < FUZZY_REVIEW_MIN:
            score = FUZZY_REVIEW_MIN
            evidence["ip_floor_applied"] = True
    if ip_overlap:
        # Reported whether or not it was scored, because a reviewer looking at a
        # pair should see the shared address either way — and, when it was NOT
        # scored, why.
        evidence["ip_overlap"] = True
        if not ip_is_evidence:
            evidence["ip_overlap_ambiguous"] = True

    # --- counter-evidence: conflicts a reviewer must see regardless of score
    # Only ROUTABLE addresses can conflict: two hosts each reporting 127.0.0.1
    # do not disagree about anything, and a host whose only address is a loopback
    # has made no claim to contradict.
    if corr_a and corr_b and not shared_addresses:
        counter_evidence.append(f"conflicting IP ({ip_a} vs {ip_b})")

    os_a, os_b = attrs_a.get("os"), attrs_b.get("os")
    if os_a and os_b and str(os_a).strip().lower() != str(os_b).strip().lower():
        counter_evidence.append(f"differing OS ({os_a} vs {os_b})")

    if hosts_domain_conflict(node_a.name, node_b.name):
        counter_evidence.append(
            f"cross-domain hostname ({host_domain(node_a.name)} vs {host_domain(node_b.name)})"
        )

    for field in (*_HARD_IDENTIFIER_FIELDS, *_MISMATCH_ONLY_FIELDS):
        va, vb = attrs_a.get(field), attrs_b.get(field)
        if va and vb and str(va).strip().lower() != str(vb).strip().lower():
            counter_evidence.append(f"{field} mismatch ({va} vs {vb})")

    if attrs_a.get("is_disabled") or attrs_b.get("is_disabled"):
        counter_evidence.append("one side is disabled/decommissioned (is_disabled=True)")

    last_a, last_b = getattr(node_a, "last_seen", None), getattr(node_b, "last_seen", None)
    if last_a is not None and last_b is not None:
        # sqlite (the test suite's dialect) does not actually enforce
        # DateTime(timezone=True) -- a value written tz-aware can come back
        # naive. Normalize to naive-UTC for the subtraction only; this is a
        # display/threshold comparison, not a stored value, so precision loss
        # from dropping tzinfo is immaterial.
        na = last_a.replace(tzinfo=None) if last_a.tzinfo else last_a
        nb = last_b.replace(tzinfo=None) if last_b.tzinfo else last_b
        gap = abs(na - nb)
        if gap > _LAST_SEEN_GAP_COUNTER_EVIDENCE:
            counter_evidence.append(f"last-seen gap of {gap.days}d")

    # --- TRK-341: host_reconcile's unsettled legs, as counter-evidence -----
    # Applied LAST so it composes with the hard-identifier floor above: a
    # shared uuid that floored the score into the auto-emit band still gets
    # demoted here, because a matching identifier on a row that was itself a
    # coin flip corroborates the wrong thing.
    if ambiguity is not None:
        unsettled = [
            label
            for label in (ambiguity.unsettled_label(node_a), ambiguity.unsettled_label(node_b))
            if label is not None
        ]
        if unsettled:
            for label in unsettled:
                counter_evidence.append(
                    f"unsettled identity leg for {label} (host_reconcile same-source collision)"
                )
            # One penalty however many legs are unsettled: two unsettled legs
            # are not twice as uncertain as one, they are the same
            # "do not assert this" verdict, and the floor below would absorb
            # the difference anyway.
            floor = FUZZY_REVIEW_MIN if score >= FUZZY_REVIEW_MIN else 0.0
            score = round(max(floor, score - _AMBIGUOUS_LEG_SCORE_PENALTY), 4)

    return score, evidence, counter_evidence


# ---------------------------------------------------------------------------
# Review queue (ProposedAction-backed)
# ---------------------------------------------------------------------------


def _review_target(node: GraphNode) -> str:
    """Stable ``ProposedAction.target`` for one source node's question."""
    return f"same-as:{node.node_type}:{node.natural_key}"[:512]


# ---------------------------------------------------------------------------
# Authority model — decided pairs are facts in the graph, not queue statuses
# ---------------------------------------------------------------------------
#
# The whole KG-1/KG-3 design in one paragraph: decisions about identity are
# first-class, authority-tagged, bitemporal EDGES. A human YES is an active
# ``SAME_AS`` with ``authority='human'``; a human NO is an active
# ``NOT_SAME_AS`` with ``authority='human'``. The review queue is a workflow
# INBOX, never the decision store — which is why both ``graph_phase3`` and
# ``host_reconcile`` can consult one shared predicate (:func:`pair_gate` /
# :func:`resource_pair_gate`) instead of each re-deriving decision state from
# a ProposedAction status they may not even be able to see.


def _evidence_class_for_pair(node_a: GraphNode, node_b: GraphNode) -> str:
    """Classify the CURRENT evidence for a pair on the re-ask ladder.

    Computed the same way at rejection time and at re-ask time, from live node
    state, so "is this strictly stronger than what the human judged?" is a
    comparison of like with like rather than of a stored label against a
    freshly-invented one.

    Deliberately scores WITHOUT the unsettled-leg counter-evidence (TRK-341):
    the class measures POSITIVE evidence strength, and it must stay comparable
    against a label stored at rejection time. An unsettled leg is transient —
    recomputed every 30-minute reconcile and cleared once the collision
    resolves — so letting it move a pair up or down this ladder would make the
    re-ask threshold depend on which reconcile run happened to run last. What
    an unsettled leg does is block emission and demote the score, which happens
    in :func:`_score_candidate` for the callers that emit.
    """
    _score, evidence, _counter = _score_candidate(node_a, node_b)
    if evidence.get("corroborating_identifier"):
        return EVIDENCE_CLASS_HARD_IDENTIFIER
    key_a, key_b = normalize_host(node_a.name), normalize_host(node_b.name)
    if key_a and key_a == key_b:
        return EVIDENCE_CLASS_EXACT_NAME
    return EVIDENCE_CLASS_FUZZY


def _evidence_rank(klass: str | None) -> int:
    return _EVIDENCE_CLASS_RANK.get(klass or EVIDENCE_CLASS_FUZZY, 0)


def _active_pair_edges(
    session: Any,
    node_a_id: uuid.UUID,
    node_b_id: uuid.UUID,
    edge_type: GraphEdgeType,
    *,
    authority: str | None = None,
    for_update: bool = False,
) -> list[GraphEdge]:
    """Active edges of ``edge_type`` between the pair, in EITHER direction."""
    stmt = select(GraphEdge).where(
        GraphEdge.edge_type == edge_type.value,
        GraphEdge.valid_to.is_(None),
        or_(
            (GraphEdge.source_id == node_a_id) & (GraphEdge.target_id == node_b_id),
            (GraphEdge.source_id == node_b_id) & (GraphEdge.target_id == node_a_id),
        ),
    )
    if authority is not None:
        stmt = stmt.where(GraphEdge.authority == authority)
    if for_update:
        stmt = stmt.with_for_update()
    return list(session.execute(stmt).scalars().all())


def active_veto(session: Any, node_a_id: uuid.UUID, node_b_id: uuid.UUID) -> GraphEdge | None:
    """The active human ``NOT_SAME_AS`` veto for this pair, or ``None``."""
    edges = _active_pair_edges(session, node_a_id, node_b_id, GraphEdgeType.NOT_SAME_AS)
    return edges[0] if edges else None


#: The order :func:`pair_gate` decides a SINGLE node pair in. Strongest claim
#: first: an explicit human YES or NO outranks an unanswered question.
_PAIR_REASON_ORDER: tuple[str, ...] = ("human_confirmed", "human_veto", "pending_review")


class PairDecisionIndex:
    """Preloaded :func:`pair_gate` answers over a fixed set of graph nodes.

    A query-shape optimisation, NOT a second decision model. There is exactly
    one implementation of "is this pair decided or under question" — the
    ``_load_pair_decisions`` body below — and :func:`pair_gate` itself goes
    through it, so the batched and single-pair entry points cannot drift apart.

    It exists because ``host_reconcile`` tests O(hosts x sources^2) pairs per
    reconcile run; asking per pair would be two round-trips each.
    """

    __slots__ = ("_reasons",)

    def __init__(self, reasons: dict[frozenset, str]) -> None:
        self._reasons = reasons

    def reason(self, node_a_id: Any, node_b_id: Any) -> str | None:
        if node_a_id is None or node_b_id is None or node_a_id == node_b_id:
            return None
        return self._reasons.get(frozenset((node_a_id, node_b_id)))


def _record_pair_reason(reasons: dict[frozenset, str], key: frozenset, reason: str) -> None:
    """Keep only the strongest reason per unordered node pair."""
    current = reasons.get(key)
    if current is None or _PAIR_REASON_ORDER.index(reason) < _PAIR_REASON_ORDER.index(current):
        reasons[key] = reason


def _load_pair_decisions(session: Any, nodes: Sequence[GraphNode]) -> PairDecisionIndex:
    """THE decision logic, evaluated over every pair within ``nodes`` at once.

    Two queries regardless of how many pairs the caller will go on to test.

    NOT wrapped in a try/except. If the decision state cannot be READ, an
    automatic emitter must not proceed as though nothing were decided — that is
    precisely the moment a transient DB error would let a 0.95 machine
    assertion ride over a human's. The error propagates; the caller's pass
    fails loudly and emits nothing. (Contrast the ONE sanctioned open
    direction, below: a resource with no ``graph_nodes`` row genuinely carries
    no decision, which is absence of evidence rather than a failure to look.)
    """
    reasons: dict[frozenset, str] = {}
    node_ids = [n.id for n in nodes if n.id is not None]
    if len(set(node_ids)) < 2:
        return PairDecisionIndex(reasons)

    # (1) Decided pairs. An active human-authority SAME_AS is a YES; ANY active
    # NOT_SAME_AS is a NO (rule W5 makes that edge type human-only at the write
    # choke point, so no authority filter is needed or wanted here).
    edges = (
        session.execute(
            select(GraphEdge).where(
                GraphEdge.source_id.in_(node_ids),
                GraphEdge.target_id.in_(node_ids),
                GraphEdge.edge_type.in_(
                    [GraphEdgeType.SAME_AS.value, GraphEdgeType.NOT_SAME_AS.value]
                ),
                GraphEdge.valid_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for edge in edges:
        key = frozenset((edge.source_id, edge.target_id))
        if len(key) < 2:  # defensive: a self-loop decides nothing
            continue
        if edge.edge_type == GraphEdgeType.NOT_SAME_AS.value:
            _record_pair_reason(reasons, key, "human_veto")
        elif edge.authority == GraphEdgeAuthority.HUMAN.value:
            _record_pair_reason(reasons, key, "human_confirmed")

    # (2) Open questions: a PENDING review row anchored on one node whose
    # candidate_matches names the other. Only 'pending' gates — an answered row
    # is a decision, and decisions are carried by edges (above), never by inbox
    # status. See REVIEW_LIVE_STATUSES.
    by_target = {_review_target(n): n for n in nodes if n.id is not None}
    id_index = {str(n.id): n.id for n in nodes if n.id is not None}
    rows = (
        session.execute(
            select(ProposedAction).where(
                ProposedAction.action_type == REVIEW_ACTION_TYPE,
                ProposedAction.target.in_(list(by_target)),
                ProposedAction.status == "pending",
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        anchor = by_target.get(row.target)
        if anchor is None:
            continue
        for cand in (row.payload or {}).get("candidate_matches") or []:
            other = id_index.get(str(cand.get("node_id")))
            if other is None or other == anchor.id:
                continue
            _record_pair_reason(reasons, frozenset((anchor.id, other)), "pending_review")

    return PairDecisionIndex(reasons)


def pair_gate(session: Any, node_a: GraphNode, node_b: GraphNode) -> str | None:
    """Is this pair already DECIDED, or currently UNDER QUESTION?

    THE shared predicate. Every automatic identity emitter — in either edge
    store — asks this before emitting, and skips on any non-``None`` answer:

    ``'human_confirmed'``
        An active ``SAME_AS`` with ``authority='human'`` already asserts the
        fact at higher authority. Re-asserting it automatically is how KG-1
        destroyed approver attribution.
    ``'human_veto'``
        An active ``NOT_SAME_AS``. A human said no; the machine does not get
        to overrule that by re-observing the same evidence.
    ``'pending_review'``
        The machine already ASKED about this pair. It must not answer its own
        open question by emitting — a store that simultaneously asserts "same"
        and asks "same?" is incoherent.

    Returns ``None`` when the pair is free to emit.
    """
    return _load_pair_decisions(session, (node_a, node_b)).reason(node_a.id, node_b.id)


#: Strongest-first, so :func:`resource_pair_gate` can report the most
#: decisive reason when a resource pair maps to several node pairs.
_GATE_PRECEDENCE: tuple[str, ...] = ("human_veto", "human_confirmed", "pending_review")


class ResourcePairGate:
    """:class:`PairDecisionIndex` lifted into ``resources.id`` space (KG-2).

    ``host_reconcile`` used to write ``IS_SAME_AS`` into the (now dropped)
    ``resource_relationships`` store, keyed by ``resources.id``; the
    decisions live on ``graph_nodes``.
    This wraps the node-space index with the reconciliation join Phase 2
    deliberately left available (``graph_nodes.resource_id``) and reports the
    STRONGEST blocking reason across every node pair the two resources span.

    NO CALLER IN ``src/`` SINCE P5, AND KEPT ON PURPOSE. Its only consumer was
    ``host_reconcile._resource_pair_gate``, deleted with that agent's IS_SAME_AS
    writer — everything that asks the gate now works in node space and uses
    :func:`pair_gate` directly. It stays because it is exported public API, it is
    the ONLY resource-space view of human identity decisions, and the next writer
    that needs one (a revived host-bearing collector, a bulk resolver, an MCP
    tool answering "may I link these two resources") would otherwise re-derive it
    — which is exactly the two-answers-to-one-question shape KG-2 introduced it
    to remove. ``tests/agents/test_host_reconcile_ambiguity_gate.py::
    test_batched_gate_agrees_with_the_single_pair_predicate`` keeps it honest
    against :func:`pair_gate`, so it is unused rather than untested. Deleting it
    is a defensible later call; doing so silently as "dead code" is not.
    """

    __slots__ = ("_decisions", "_nodes_by_resource")

    def __init__(
        self, decisions: PairDecisionIndex, nodes_by_resource: dict[str, list[Any]]
    ) -> None:
        self._decisions = decisions
        self._nodes_by_resource = nodes_by_resource

    def reason(self, resource_id_a: Any, resource_id_b: Any) -> str | None:
        if resource_id_a is None or resource_id_b is None or resource_id_a == resource_id_b:
            return None
        nodes_a = self._nodes_by_resource.get(str(resource_id_a)) or []
        nodes_b = self._nodes_by_resource.get(str(resource_id_b)) or []
        found: set[str] = set()
        for a in nodes_a:
            for b in nodes_b:
                reason = self._decisions.reason(a, b)
                if reason:
                    found.add(reason)
        for candidate in _GATE_PRECEDENCE:
            if candidate in found:
                return candidate
        return None


def resource_pair_gate_index(session: Any, resource_ids: Iterable[Any]) -> ResourcePairGate:
    """Preload :func:`resource_pair_gate` over a whole emission pass.

    Two queries for the decisions plus one to resolve the resource→node join,
    however many pairs the caller then tests — which is why the reconciler
    builds this once per pass instead of calling the single-pair adapter inside
    its O(sources^2) loop.

    A resource with no ``graph_nodes`` row simply contributes no node ids, and
    therefore blocks nothing. That is the ONE open direction this gate has, and
    it is structural: there is no decision to find, as opposed to a decision we
    failed to read. Load errors are NOT swallowed — see ``_load_pair_decisions``.
    """
    ids = {rid for rid in resource_ids if rid}
    if len(ids) < 2:
        return ResourcePairGate(PairDecisionIndex({}), {})
    nodes = (
        session.execute(select(GraphNode).where(GraphNode.resource_id.in_(list(ids))))
        .scalars()
        .all()
    )
    nodes_by_resource: dict[str, list[Any]] = {}
    for node in nodes:
        nodes_by_resource.setdefault(str(node.resource_id), []).append(node.id)
    return ResourcePairGate(_load_pair_decisions(session, list(nodes)), nodes_by_resource)


def resource_pair_gate(session: Any, resource_id_a: Any, resource_id_b: Any) -> str | None:
    """:func:`pair_gate` in ``resources.id`` space — single-pair adapter.

    Convenience entry point for a caller holding exactly one pair; the batching
    version, :func:`resource_pair_gate_index`, is the same logic and is what an
    emission pass should use.
    """
    if resource_id_a is None or resource_id_b is None or resource_id_a == resource_id_b:
        return None
    index = resource_pair_gate_index(session, (resource_id_a, resource_id_b))
    return index.reason(resource_id_a, resource_id_b)


def _retire_vetoes(
    session: Any,
    node_a_id: uuid.UUID,
    node_b_id: uuid.UUID,
    *,
    overridden_by: str,
    reason: str | None = None,
) -> int:
    """Retire an active ``NOT_SAME_AS`` pair, stamping who overrode whom.

    A pair may never simultaneously carry an active ``SAME_AS`` and an active
    ``NOT_SAME_AS``, so :func:`confirm_same_as` calls this FIRST. Retire, never
    delete — the fact that a colleague once said no, and who overrode them, is
    exactly the record a later reviewer needs.
    """
    edges = _active_pair_edges(
        session, node_a_id, node_b_id, GraphEdgeType.NOT_SAME_AS, for_update=True
    )
    if not edges:
        return 0
    now = _now()
    for edge in edges:
        evidence = dict(edge.evidence or {})
        evidence["overridden_by"] = overridden_by
        evidence["overridden_at"] = now.isoformat()
        if reason:
            evidence["override_reason"] = reason
        edge.evidence = evidence
    return retire_edges(session, edges)


def _candidate_payload(
    node: GraphNode,
    score: float,
    reason: str,
    *,
    evidence: dict[str, Any] | None = None,
    counter_evidence: list[str] | None = None,
    confidence_band: str = "fuzzy_review",
) -> dict[str, Any]:
    """One ranked candidate in a review-queue row.

    ``confidence_band`` (GitLab issue #168 fix) is the explicit label a
    reviewer needs so a single queued candidate can no longer be misread as
    "high confidence" -- it is the opposite: a single fuzzy candidate means
    nothing else fuzzy-matched this name, which is WEAKER evidence than a
    corroborated match. Values: ``"exact_ambiguous"`` (tied normalized-name
    match, multiple objects in one source), ``"fuzzy_review"`` (fuzzy score in
    the ambiguous band, or an otherwise-clean match diverted here by
    counter-evidence), ``"corroborated"`` (a hard identifier matched).
    """
    return {
        "node_id": str(node.id),
        "node_type": node.node_type,
        "natural_key": node.natural_key,
        "name": node.name,
        "source": node.source,
        "score": score,
        "reason": reason,
        "evidence": evidence or {},
        "counter_evidence": counter_evidence or [],
        "confidence_band": confidence_band,
    }


def queue_for_review(
    session: Any,
    source_node: GraphNode,
    candidates: list[dict[str, Any]],
) -> ProposedAction | None:
    """Record an ambiguous identity question for a human. Emits NO edge.

    Idempotent per source node while a question is OPEN (see
    ``REVIEW_LIVE_STATUSES``, now ``("pending",)``): a pending question is not
    re-asked and ``None`` is returned, so a caller can count genuinely-new
    questions.

    KG-3: an ANSWERED row (``approved``/``rejected``) no longer silences the
    node. It is REOPENED in place with the new candidate list, preserving the
    prior answer under ``previous_decisions`` — the same
    preserve-don't-erase discipline ``retract_same_as`` already applies with
    ``retraction_history``. Reopening the existing row rather than inserting a
    second one also keeps the ``uq_proposed_action_target_status`` invariant
    ("at most one row per (action_type, target) per status") intact, so the
    remediation bulk-reject flow that depends on that constraint is untouched.

    Candidates are ranked best-first and capped at
    :data:`MAX_REVIEW_CANDIDATES` — a queue entry a human cannot read is not
    a review mechanism.

    KG-4: a still-``pending`` row is a LIVE question, not a decision, and
    ``resolve_entities`` re-runs on a cadence (``graph_maintenance``, every
    2h) — each pass recomputes scores/evidence/``mutually_exclusive`` markers
    from the graph's current state. Without a refresh, a pending row's
    ``candidate_matches`` froze at whatever the FIRST pass happened to see
    and a reviewer could act on stale evidence indefinitely. ``approved`` and
    ``rejected`` rows are human decisions and are deliberately left untouched
    (see the early ``return None`` below) — only ``pending`` refreshes.
    """
    if not candidates:
        return None
    target = _review_target(source_node)
    open_row = (
        session.execute(
            select(ProposedAction).where(
                ProposedAction.action_type == REVIEW_ACTION_TYPE,
                ProposedAction.target == target,
                ProposedAction.status.in_(REVIEW_LIVE_STATUSES),
            )
        )
        .scalars()
        .first()
    )
    ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)[:MAX_REVIEW_CANDIDATES]
    source_node_payload = {
        "node_id": str(source_node.id),
        "node_type": source_node.node_type,
        "natural_key": source_node.natural_key,
        "name": source_node.name,
        "source": source_node.source,
    }
    now = _now()

    # KG-4 (integration merge): a still-``pending`` row is a LIVE question, not
    # a decision, and ``resolve_entities`` re-runs every 2h recomputing scores,
    # evidence and mutually-exclusive markers from current graph state. Without
    # this refresh the row's ``candidate_matches`` froze at whatever the FIRST
    # pass saw and a reviewer could act on stale evidence indefinitely. It still
    # returns None — a refreshed question is not a NEW question, so callers
    # counting genuinely-new questions are unaffected.
    #
    # This branch and the ``answered`` reopen below are the KG-4 and KG-3 fixes
    # respectively; they were written by two agents in parallel against separate
    # copies of this function and had to be reconciled here. They are
    # complementary, not alternatives: pending rows REFRESH, answered rows
    # REOPEN, and only a target with no row at all inserts.
    if open_row is not None:
        open_row.payload = {
            **(open_row.payload or {}),
            "source_node": source_node_payload,
            "candidate_matches": ranked,
        }
        open_row.confidence = float(ranked[0]["score"])
        session.add(open_row)
        session.flush()
        return None

    # KG-3: an ANSWERED row (approved/rejected) no longer silences the node
    # forever. Reopen it in place with the new candidate list, preserving the
    # prior answer under ``previous_decisions`` — the same preserve-don't-erase
    # discipline ``retract_same_as`` applies with ``retraction_history``.
    # Reopening rather than inserting a second row keeps the
    # ``uq_proposed_action_target_status`` invariant intact, so the remediation
    # bulk-reject flow that depends on it is untouched.
    answered = (
        session.execute(
            select(ProposedAction)
            .where(
                ProposedAction.action_type == REVIEW_ACTION_TYPE,
                ProposedAction.target == target,
            )
            .order_by(ProposedAction.created_at.desc())
        )
        .scalars()
        .first()
    )
    if answered is not None:
        payload = dict(answered.payload or {})
        history = list(payload.get("previous_decisions") or [])
        history.append(
            {
                "status": answered.status,
                "approved_by": answered.approved_by,
                "approved_at": answered.approved_at.isoformat() if answered.approved_at else None,
                "confirmed_target_node_id": payload.get("confirmed_target_node_id"),
                "rejections": payload.get("rejections") or [],
                "reopened_at": now.isoformat(),
            }
        )
        payload["previous_decisions"] = history
        payload["source_node"] = source_node_payload
        payload["candidate_matches"] = ranked
        payload.pop("confirmed_target_node_id", None)
        answered.payload = payload
        answered.status = "pending"
        answered.approved_by = None
        answered.approved_at = None
        answered.confidence = float(ranked[0]["score"])
        session.flush()
        return answered

    action = ProposedAction(
        id=uuid.uuid4(),
        agent=REVIEW_AGENT,
        action_type=REVIEW_ACTION_TYPE,
        target=target,
        payload={
            "source_node": source_node_payload,
            "candidate_matches": ranked,
        },
        confidence=float(ranked[0]["score"]),
        status="pending",
        created_at=now,
    )
    session.add(action)
    session.flush()
    return action


def _active_vetoes_for(session: Any, node_id_raw: Any) -> list[dict[str, Any]]:
    """Active human ``NOT_SAME_AS`` vetoes anchored on one source node."""
    try:
        node_id = uuid.UUID(str(node_id_raw))
    except (ValueError, TypeError):
        return []
    edges = (
        session.execute(
            select(GraphEdge).where(
                GraphEdge.edge_type == GraphEdgeType.NOT_SAME_AS.value,
                GraphEdge.valid_to.is_(None),
                GraphEdge.source_id == node_id,
            )
        )
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for edge in edges:
        evidence = edge.evidence or {}
        out.append(
            {
                "node_id": str(edge.target_id),
                "name": evidence.get("target_name"),
                "rejector": evidence.get("rejector"),
                "rejected_at": evidence.get("rejected_at"),
                "reason": evidence.get("reason"),
                "rejected_evidence_class": evidence.get("rejected_evidence_class"),
            }
        )
    return out


def get_reconciliation_state(session: Any, domain: str | None = None) -> list[dict[str, Any]]:
    """Return the review queue: one row per unresolved identity question.

    ``domain`` filters on the SOURCE node's ``source`` field ("vsphere",
    "rapid7", "ansible", "octopus"). Pending rows come first (they are the
    ones needing action), then most recent.
    """
    rows = (
        session.execute(
            select(ProposedAction)
            .where(ProposedAction.action_type == REVIEW_ACTION_TYPE)
            .order_by(ProposedAction.created_at.desc())
        )
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = row.payload or {}
        src = payload.get("source_node") or {}
        if domain and src.get("source") != domain:
            continue
        candidates = payload.get("candidate_matches") or []
        # GitLab issue #168: the misreading this fix targets is a reviewer
        # seeing "1 candidate" and inferring high confidence, when a SINGLE
        # queued candidate actually means the weaker signal -- nothing else
        # fuzzy-matched. `candidates_to_disambiguate` replaces any bare
        # candidate count with an explicitly-named field, and `confidence_band`
        # (the top-ranked candidate's band; candidates are pre-sorted
        # best-first by queue_for_review) makes the actual strength of the
        # evidence explicit instead of implied by count.
        out.append(
            {
                "action_id": str(row.id),
                "source_node": src,
                "candidate_matches": candidates,
                "candidates_to_disambiguate": len(candidates),
                "confidence_band": candidates[0].get("confidence_band") if candidates else None,
                "status": row.status,
                "best_score": row.confidence,
                "approved_by": row.approved_by,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                # Set only once a prior confirmation on this row was undone
                # (see retract_same_as) -- lets a reviewer know a "pending"
                # row isn't untouched, it's a reopened mistake.
                "retraction_history": payload.get("retraction_history") or [],
                # Prior answers this row has already been through (KG-3: an
                # answered row is REOPENED rather than silencing the node
                # forever, so "pending" no longer implies "never decided").
                "previous_decisions": payload.get("previous_decisions") or [],
                # Pair-scoped human NOs recorded against this source node, so
                # a reviewer sees which candidates a colleague already ruled
                # out -- and, on a re-ask, that they are overruling one.
                "active_vetoes": _active_vetoes_for(session, src.get("node_id")),
            }
        )
    out.sort(key=lambda r: (r["status"] != "pending", r["created_at"] or ""), reverse=False)
    return out


def confirm_same_as(
    session: Any,
    source_node_id: uuid.UUID,
    target_node_id: uuid.UUID,
    approver: str,
) -> dict[str, Any]:
    """Human-in-the-loop confirmation: write a confirmed ``SAME_AS`` edge.

    Method is ``declared`` at confidence 1.000. Rationale: ``declared`` means
    "the source states the relationship and we resolved it without inference".
    For a cross-source identity claim, the *authoritative source* is the named
    operator — an accountable human assertion is strictly stronger evidence
    than any string match this module can compute, and it is recorded with
    the approver's name in ``evidence`` so the claim is attributable rather
    than anonymous. Nothing else in Phase 3 may use 1.000.

    Refuses (returns an ``error``, never raises, never partially writes) when
    either node is unknown, the two are the same node, both are from the same
    source node_type (a SAME_AS between two rows of one source is a
    within-source dedup question, out of scope here), ``approver`` is blank
    (an unattributed approval is not an approval, same stance as
    ``promote_instinct``'s TRK-136 fix), or EITHER node has its own pending
    review-queue row whose ``candidate_matches`` does not include the other
    node — a queued question constrains the choice to the candidates Phase 3
    itself ranked, and confirming outside that list defeats the point of
    having asked. A confirmation where NEITHER node has a pending question is
    still honoured with no such constraint (an operator may know something
    the resolver never surfaced) and reported as ``review_resolved: False``.
    This check lives here, not only in callers, so it holds for every caller
    (API route, MCP tool, or any future one) rather than being one route's
    responsibility to re-implement correctly.

    Resolves the matching review-queue row when one exists, stamping
    ``status='approved'`` / ``approved_by`` / ``approved_at``.
    """
    if not approver or not approver.strip():
        return {"error": "approver must be non-empty (whitespace-only is rejected)"}
    if source_node_id == target_node_id:
        return {"error": "source_node_id and target_node_id are the same node"}

    src = session.get(GraphNode, source_node_id)
    tgt = session.get(GraphNode, target_node_id)
    if src is None:
        return {"error": f"graph node {source_node_id} not found"}
    if tgt is None:
        return {"error": f"graph node {target_node_id} not found"}
    if src.node_type == tgt.node_type:
        return {
            "error": (
                f"both nodes are {src.node_type}; SAME_AS links per-source nodes of "
                "DIFFERENT sources, not two rows of one source"
            )
        }

    # A pair carrying an active human veto is, by construction, a pair the
    # resolver DID propose and a human DID judge -- the veto edge records the
    # candidate snapshot it was judged on. Confirming it is an explicit
    # REVERSAL of that colleague's decision, which the design sanctions, so the
    # candidate-list constraint below (whose job is to stop a confirmation of a
    # pairing nobody ever proposed) must not block it: reject_same_as removes a
    # vetoed candidate from the row's list precisely so it stops being offered.
    reversing_a_veto = active_veto(session, src.id, tgt.id) is not None

    for node, other in ((src, tgt), (tgt, src)):
        if reversing_a_veto:
            break
        pending = (
            session.execute(
                select(ProposedAction).where(
                    ProposedAction.action_type == REVIEW_ACTION_TYPE,
                    ProposedAction.target == _review_target(node),
                    ProposedAction.status == "pending",
                )
            )
            .scalars()
            .first()
        )
        if pending is None:
            continue
        candidate_ids = {
            c.get("node_id") for c in (pending.payload or {}).get("candidate_matches", [])
        }
        if str(other.id) not in candidate_ids:
            return {
                "error": (
                    f"{other.natural_key} is not one of the candidates queued for "
                    f"{node.natural_key}'s review question ({node.id} has a pending "
                    "review row whose candidate_matches does not include this target)"
                )
            }

    now = _now()
    # A pair may never carry an active SAME_AS and an active NOT_SAME_AS at
    # once, so a confirmation that reverses a colleague's rejection retires the
    # veto FIRST (stamping who overrode whom into the retired rows) and only
    # then asserts the positive claim. Ordering is enforced here, in the core
    # function every caller holds, not in one route.
    veto_overridden = _retire_vetoes(session, src.id, tgt.id, overridden_by=approver.strip())
    evidence = {
        "basis": "human_confirmation",
        "approver": approver.strip(),
        "approved_at": now.isoformat(),
        "source_name": src.name,
        "target_name": tgt.name,
    }
    if veto_overridden:
        evidence["overrode_prior_rejection"] = True
    edges = []
    for a, b in ((src, tgt), (tgt, src)):
        edges.append(
            upsert_edge(
                session,
                source_id=a.id,
                target_id=b.id,
                edge_type=GraphEdgeType.SAME_AS,
                method=GraphEdgeMethod.DECLARED,
                confidence=CONFIDENCE_DECLARED,
                source=EMITTER_SAME_AS_CONFIRMED,
                evidence=evidence,
                # W3: over an existing AUTO edge this retires that row and
                # inserts a new one, so history keeps both the machine's
                # 0.990 claim and the operator's 1.000 declaration.
                authority=GraphEdgeAuthority.HUMAN,
            )
        )

    review_resolved = False
    for node, other in ((src, tgt), (tgt, src)):
        action = (
            session.execute(
                select(ProposedAction).where(
                    ProposedAction.action_type == REVIEW_ACTION_TYPE,
                    ProposedAction.target == _review_target(node),
                    ProposedAction.status == "pending",
                )
            )
            .scalars()
            .first()
        )
        if action is not None:
            action.status = "approved"
            action.approved_by = approver.strip()
            action.approved_at = now
            # Stamped so a later retract_same_as call can find exactly which
            # pairing this row resolved to -- nothing else records that once
            # the row leaves "pending" (the payload's own candidate_matches is
            # the full ranked list Phase 3 proposed, not which one a human
            # actually picked).
            action.payload = {**(action.payload or {}), "confirmed_target_node_id": str(other.id)}
            review_resolved = True
    session.flush()

    logger.info(
        "graph_phase3: confirm_same_as %s <-> %s by %s (review_resolved=%s)",
        src.natural_key,
        tgt.natural_key,
        approver,
        review_resolved,
    )
    return {
        "confirmed": True,
        "edge_ids": [str(e.id) for e in edges],
        "source_node": {"node_id": str(src.id), "node_type": src.node_type, "name": src.name},
        "target_node": {"node_id": str(tgt.id), "node_type": tgt.node_type, "name": tgt.name},
        "method": GraphEdgeMethod.DECLARED.value,
        "confidence": float(CONFIDENCE_DECLARED),
        "approver": approver.strip(),
        "review_resolved": review_resolved,
        "veto_overridden": bool(veto_overridden),
    }


def retract_same_as(
    session: Any,
    source_node_id: uuid.UUID,
    target_node_id: uuid.UUID,
    retractor: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Undo a confirmed ``SAME_AS`` pairing written by :func:`confirm_same_as`.

    Closes the validity interval on BOTH directed edges — never DELETE, reusing
    ``graph_phase2.retire_edges``'s historical-record-preserving convention
    every other Phase 2/3 write follows, so a mistaken confirmation is
    correctable without losing the fact that it was once asserted (and by
    whom, and when it was undone — both stamped into the retired edges'
    ``evidence``).

    If a matching review-queue row exists (``status='approved'`` and its
    ``payload.confirmed_target_node_id`` matches the OTHER node of this pair —
    see the stamp ``confirm_same_as`` writes), it is reopened to ``'pending'``
    so the identity question can be re-decided, rather than staying
    permanently (and now incorrectly) resolved with no edge behind it.

    Only retracts edges of ``authority='human'`` — a machine-emitted
    ``deterministic_match``/``probabilistic_match`` edge is deliberately out
    of scope, since the resolver's own next pass would silently re-emit it,
    making a "retracted" response for one misleading.

    KG-1: this used to filter on ``source == EMITTER_SAME_AS_CONFIRMED``, a
    free-form emitter STRING. That was brittle in two ways, both of which bit:
    (a) any second human-confirmation path (an MCP bulk-confirm, a future
    import tool) wrote a different string and was silently unretractable; and
    (b) once an automatic pass overwrote ``source`` in place, a genuinely
    human-confirmed edge became permanently unretractable. Authority is the
    real axis, and it is now a column that no automatic writer can clobber.

    Refuses (returns an ``error``, never raises, never partially writes) when
    no ACTIVE, human-confirmed ``SAME_AS`` edge exists between these two
    nodes, the two are the same node, or ``retractor`` is blank — an
    unattributed retraction is not a retraction, mirroring
    ``confirm_same_as``'s own TRK-136 stance.
    """
    if not retractor or not retractor.strip():
        return {"error": "retractor must be non-empty (whitespace-only is rejected)"}
    if source_node_id == target_node_id:
        return {"error": "source_node_id and target_node_id are the same node"}

    # with_for_update: this is the actual contended resource -- locking it
    # here (rather than relying on a caller to separately lock a proxy
    # ProposedAction row, which the MCP tool doesn't do at all) makes
    # retract_same_as safe against concurrent retracts from EVERY caller.
    edges = (
        session.execute(
            select(GraphEdge)
            .where(
                GraphEdge.edge_type == GraphEdgeType.SAME_AS.value,
                GraphEdge.valid_to.is_(None),
                GraphEdge.authority == GraphEdgeAuthority.HUMAN.value,
                or_(
                    (GraphEdge.source_id == source_node_id)
                    & (GraphEdge.target_id == target_node_id),
                    (GraphEdge.source_id == target_node_id)
                    & (GraphEdge.target_id == source_node_id),
                ),
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if not edges:
        return {
            "error": (
                "no active, human-confirmed SAME_AS edge found between these two nodes "
                "(a machine-emitted match is not retractable -- only one written by "
                "confirm_same_as is)"
            )
        }

    now = _now()
    for edge in edges:
        evidence = dict(edge.evidence or {})
        evidence["retracted_by"] = retractor.strip()
        evidence["retracted_at"] = now.isoformat()
        if reason:
            evidence["retraction_reason"] = reason
        edge.evidence = evidence

    retired_count = retire_edges(session, edges)

    reopened = False
    for node_id, other_id in ((source_node_id, target_node_id), (target_node_id, source_node_id)):
        node = session.get(GraphNode, node_id)
        if node is None:
            continue
        action = (
            session.execute(
                select(ProposedAction)
                .where(
                    ProposedAction.action_type == REVIEW_ACTION_TYPE,
                    ProposedAction.target == _review_target(node),
                    ProposedAction.status == "approved",
                )
                .with_for_update()
            )
            .scalars()
            .first()
        )
        if action is not None and (action.payload or {}).get("confirmed_target_node_id") == str(
            other_id
        ):
            payload = dict(action.payload or {})
            # Preserve, don't erase, the confirmation this retraction undoes --
            # a bare status flip back to "pending" is indistinguishable from a
            # question no human has ever touched, so a re-opened row invites
            # someone to re-confirm the exact pairing that was just retracted
            # as a mistake with no warning that this was already tried.
            history = list(payload.get("retraction_history") or [])
            history.append(
                {
                    "confirmed_target_node_id": payload.get("confirmed_target_node_id"),
                    "approved_by": action.approved_by,
                    "approved_at": action.approved_at.isoformat() if action.approved_at else None,
                    "retracted_by": retractor.strip(),
                    "retracted_at": now.isoformat(),
                    "reason": reason,
                }
            )
            payload["retraction_history"] = history
            payload.pop("confirmed_target_node_id", None)
            action.status = "pending"
            action.approved_by = None
            action.approved_at = None
            action.payload = payload
            reopened = True
    session.flush()

    logger.info(
        "graph_phase3: retract_same_as %s <-> %s by %s (edges_retired=%d, review_reopened=%s)",
        source_node_id,
        target_node_id,
        retractor,
        retired_count,
        reopened,
    )
    return {
        "retracted": True,
        "edge_ids": [str(e.id) for e in edges],
        "retractor": retractor.strip(),
        "review_reopened": reopened,
    }


def reject_same_as(
    session: Any,
    action_id: uuid.UUID,
    rejector: str,
    target_node_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Human-in-the-loop rejection: write an attributed ``NOT_SAME_AS`` veto.

    **What a rejection MEANS here, precisely:** *a named human's assertion
    that a specific PAIR of nodes are not the same entity, judged against the
    evidence presented at rejection time.* Not "stop asking about this node",
    not "this whole 10-candidate bundle is wrong".

    That definition is what fixes KG-3's asymmetry. The old behaviour was a
    bare ``ProposedAction.status`` flip: unattributed, bundle-scoped, it
    silenced every future question anchored on the source node forever, and —
    because nothing in :func:`resolve_entities` ever read a ProposedAction
    status before emitting — it did not stop a later pass auto-merging the
    exact pair a human had just rejected. Both halves are now inverted: the
    veto BLOCKS emission (via :func:`pair_gate`) and stops silencing the node
    (via ``REVIEW_LIVE_STATUSES``).

    The veto is stored as an active, symmetric ``NOT_SAME_AS`` edge pair at
    ``authority='human'``/``declared``/1.000 — the same rationale
    :func:`confirm_same_as` documents for 1.000 (an accountable human
    assertion is the authoritative source for a cross-source identity claim).
    Storing it as an EDGE rather than a queue status or a new table inherits
    bitemporality, the one-active-row index, symmetric storage, provenance,
    and — critically — one queryable place BOTH edge stores' emitters can
    consult.

    ``target_node_id``
        When given, must be one of this row's own ``candidate_matches`` (the
        same constraint discipline :func:`confirm_same_as` applies): vetoes
        that one candidate, removes it from the row's candidate list, and
        leaves the row ``pending`` if candidates remain.
        When omitted, every listed candidate is vetoed — each with its own
        attributed pair, because the operator did look at all of them, and
        that is an honest per-pair assertion — and the row flips to
        ``rejected``.

    Refuses (returns an ``error``, never raises, never partially writes) on an
    unknown/non-review/non-pending action, a blank ``rejector`` (identical
    TRK-136 stance to :func:`confirm_same_as`), or a target outside the row's
    candidate list.
    """
    if not rejector or not rejector.strip():
        return {"error": "rejector must be non-empty (whitespace-only is rejected)"}

    action = session.get(ProposedAction, action_id)
    if action is None or action.action_type != REVIEW_ACTION_TYPE:
        return {"error": f"review-queue action {action_id} not found"}
    if action.status != "pending":
        return {"error": f"action is {action.status}, not pending"}

    payload = dict(action.payload or {})
    src_id_raw = (payload.get("source_node") or {}).get("node_id")
    try:
        src_node_id = uuid.UUID(str(src_id_raw))
    except (ValueError, TypeError):
        return {"error": "action has a missing or malformed source_node.node_id"}
    src = session.get(GraphNode, src_node_id)
    if src is None:
        return {"error": f"graph node {src_node_id} not found"}

    candidates = list(payload.get("candidate_matches") or [])
    candidate_ids = {c.get("node_id") for c in candidates}
    if target_node_id is not None and str(target_node_id) not in candidate_ids:
        return {
            "error": (
                "target_node_id is not one of this action's candidate_matches — a "
                "rejection is a judgement on a pair the resolver actually proposed"
            )
        }

    chosen = (
        [c for c in candidates if c.get("node_id") == str(target_node_id)]
        if target_node_id is not None
        else candidates
    )
    if not chosen:
        return {"error": "action has no candidate_matches to reject"}

    now = _now()
    who = rejector.strip()
    vetoed: list[dict[str, Any]] = []
    for cand in chosen:
        try:
            other_id = uuid.UUID(str(cand.get("node_id")))
        except (ValueError, TypeError):
            continue
        other = session.get(GraphNode, other_id)
        if other is None or other.id == src.id:
            continue
        evidence_class = _evidence_class_for_pair(src, other)
        evidence = {
            "basis": "human_rejection",
            "rejector": who,
            "rejected_at": now.isoformat(),
            "reason": reason,
            # The class the human actually judged. A later pass may RE-ASK
            # only on strictly stronger evidence than this — never emit.
            "rejected_evidence_class": evidence_class,
            "rejected_candidate_snapshot": cand,
            "source_name": src.name,
            "target_name": other.name,
        }
        for a, b in ((src, other), (other, src)):
            upsert_edge(
                session,
                source_id=a.id,
                target_id=b.id,
                edge_type=GraphEdgeType.NOT_SAME_AS,
                method=GraphEdgeMethod.DECLARED,
                confidence=CONFIDENCE_DECLARED,
                source=EMITTER_NOT_SAME_AS,
                evidence=evidence,
                authority=GraphEdgeAuthority.HUMAN,
            )
        # A veto and a positive claim about the same pair cannot both stand.
        # An auto SAME_AS could exist here if the pair was emitted before the
        # question was asked; withdraw it rather than leave the store
        # asserting "same" while a human has just said "not same".
        stale = _active_pair_edges(session, src.id, other.id, GraphEdgeType.SAME_AS)
        if stale:
            for edge in stale:
                stale_evidence = dict(edge.evidence or {})
                stale_evidence["retired_reason"] = "human_rejection"
                edge.evidence = stale_evidence
            retire_edges(session, stale)
        vetoed.append(
            {
                "node_id": str(other.id),
                "natural_key": other.natural_key,
                "name": other.name,
                "rejected_evidence_class": evidence_class,
            }
        )

    if not vetoed:
        return {"error": "no resolvable candidate nodes to reject"}

    remaining = [c for c in candidates if c.get("node_id") not in {v["node_id"] for v in vetoed}]
    rejections = list(payload.get("rejections") or [])
    rejections.append(
        {"rejector": who, "rejected_at": now.isoformat(), "reason": reason, "pairs": vetoed}
    )
    payload["rejections"] = rejections
    payload["candidate_matches"] = remaining
    action.payload = payload
    if remaining:
        # Candidates survive, so the QUESTION is still open — a partial answer
        # is not a closed inbox row.
        action.status = "pending"
    else:
        action.status = "rejected"
    session.flush()

    logger.info(
        "graph_phase3: reject_same_as %s vetoed %d candidate(s) by %s (row now %s)",
        src.natural_key,
        len(vetoed),
        who,
        action.status,
    )
    return {
        "rejected": True,
        "action_id": str(action.id),
        "status": action.status,
        "rejector": who,
        "vetoed": vetoed,
        "candidates_remaining": len(remaining),
    }


def retract_not_same_as(
    session: Any,
    source_node_id: uuid.UUID,
    target_node_id: uuid.UUID,
    retractor: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Undo a rejection: retire the active human ``NOT_SAME_AS`` pair.

    The mirror of :func:`retract_same_as`, and the first of the two ways a
    veto ends (the other is a later :func:`confirm_same_as` on the same pair,
    which retires it as part of asserting the positive claim). Retire, never
    delete — a withdrawn rejection is still a thing that happened.
    """
    if not retractor or not retractor.strip():
        return {"error": "retractor must be non-empty (whitespace-only is rejected)"}
    if source_node_id == target_node_id:
        return {"error": "source_node_id and target_node_id are the same node"}
    edges = _active_pair_edges(
        session, source_node_id, target_node_id, GraphEdgeType.NOT_SAME_AS, for_update=True
    )
    if not edges:
        return {"error": "no active NOT_SAME_AS veto found between these two nodes"}
    now = _now()
    for edge in edges:
        evidence = dict(edge.evidence or {})
        evidence["retracted_by"] = retractor.strip()
        evidence["retracted_at"] = now.isoformat()
        if reason:
            evidence["retraction_reason"] = reason
        edge.evidence = evidence
    retired = retire_edges(session, edges)
    session.flush()
    logger.info(
        "graph_phase3: retract_not_same_as %s <-> %s by %s (edges_retired=%d)",
        source_node_id,
        target_node_id,
        retractor,
        retired,
    )
    return {
        "retracted": True,
        "edge_ids": [str(e.id) for e in edges],
        "retractor": retractor.strip(),
    }


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------


def _emit_same_as(
    session: Any,
    a: GraphNode,
    b: GraphNode,
    *,
    method: GraphEdgeMethod,
    confidence: Decimal,
    evidence: dict[str, Any],
) -> None:
    """Write the SAME_AS pair in both directions (identity is symmetric).

    Both directions are stored rather than relying on readers to UNION, which
    is the same choice ``host_reconcile`` made for ``IS_SAME_AS`` — it keeps
    single-direction traversal queries correct without a special case.
    """
    for src, tgt in ((a, b), (b, a)):
        upsert_edge(
            session,
            source_id=src.id,
            target_id=tgt.id,
            edge_type=GraphEdgeType.SAME_AS,
            method=method,
            confidence=confidence,
            source=EMITTER_SAME_AS,
            evidence=evidence,
            # Explicit rather than defaulted: this is the automatic resolver,
            # and rule W4 must make that unmistakable at the choke point.
            authority=GraphEdgeAuthority.AUTO,
        )


def _gate_pair(
    session: Any, a: GraphNode, b: GraphNode, counts: dict[str, int]
) -> tuple[str, dict[str, Any] | None]:
    """Apply :func:`pair_gate` to one candidate pair; return what may happen.

    ``("emit", None)``
        Nobody has decided or questioned this pair — proceed normally.
    ``("skip", None)``
        Decided or under question. Emit nothing, queue nothing.
    ``("requeue", previously_rejected)``
        Vetoed, but a later pass now holds evidence of a STRICTLY STRONGER
        class than the human judged (the re-ask ladder: ``fuzzy`` <
        ``exact_name`` < ``hard_identifier``). The pair may be re-QUEUED,
        flagged so the reviewer knows they are being asked to overrule a
        colleague — and still never auto-emitted. The veto stays active until
        a human answers.

    Every branch bumps a counter, so the graph-health stats show the gates
    working rather than silently doing nothing.
    """
    reason = pair_gate(session, a, b)
    if reason is None:
        return "emit", None
    if reason == "human_confirmed":
        counts["human_confirmed_skipped"] += 1
        return "skip", None
    if reason == "pending_review":
        counts["pending_gated"] += 1
        return "skip", None

    counts["human_vetoed_skipped"] += 1
    veto = active_veto(session, a.id, b.id)
    veto_evidence = (veto.evidence or {}) if veto is not None else {}
    prior_class = veto_evidence.get("rejected_evidence_class")
    current_class = _evidence_class_for_pair(a, b)
    if _evidence_rank(current_class) <= _evidence_rank(prior_class):
        return "skip", None
    counts["veto_requeued"] += 1
    return "requeue", {
        "rejector": veto_evidence.get("rejector"),
        "rejected_at": veto_evidence.get("rejected_at"),
        "reason": veto_evidence.get("reason"),
        "rejected_evidence_class": prior_class,
        "new_evidence_class": current_class,
    }


def _withdraw_auto_same_as(
    session: Any, a: GraphNode, b: GraphNode, counts: dict[str, int]
) -> None:
    """Retire an active AUTO ``SAME_AS`` pair the machine no longer stands behind.

    Called whenever a pass diverts a pair to review. Without it the store can
    simultaneously assert "same" and ask "same?" — the incoherence class this
    whole authority model exists to remove. Human-authority edges are never
    touched here (``authority='auto'`` filter): withdrawing a human claim is a
    human's job, via :func:`retract_same_as`.
    """
    edges = _active_pair_edges(
        session, a.id, b.id, GraphEdgeType.SAME_AS, authority=GraphEdgeAuthority.AUTO.value
    )
    if not edges:
        return
    for edge in edges:
        evidence = dict(edge.evidence or {})
        evidence["retired_reason"] = "diverted_to_review"
        edge.evidence = evidence
    retire_edges(session, edges)
    counts["auto_edges_withdrawn"] += 1


def resolve_entities(session: Any, *, materialize: bool = True) -> dict[str, int]:
    """Run the full entity-resolution pass; return a counts summary.

    Order is the issue's, and it matters — a pair resolved deterministically
    is never re-considered by the fuzzy pass:

    1. **Deterministic**: two nodes of DIFFERENT source types whose names
       reduce to the same ``normalize_host`` key. Emitted as
       ``deterministic_match`` at 0.990 (the Phase 2 precedent for a
       name-derived edge — not 1.000, because a name can still be renamed or
       collide).

       Guards, all of which divert to the review queue or drop the pair
       rather than merging:

       * *Cross-domain conflict* (TRK-087): both sides domain-qualified with
         different domains → different machines sharing a first DNS label.
         Suppressed entirely, not queued — the evidence positively says NO.
       * *Ambiguous key*: a normalized key that matches MORE THAN ONE node
         within a single source type. Two Rapid7 assets both called ``web01``
         cannot both be the vSphere ``web01``; picking one would be a coin
         flip presented as a fact. The whole key goes to review.
       * *Counter-evidence* (GitLab issue #168): even an exact normalized-name
         match is diverted to review, not merged, if
         :func:`_score_candidate` finds a live conflict (differing IP, OS, a
         disabled/decommissioned flag, a mismatched hard identifier, …).

    2. **Probabilistic**: for nodes the deterministic pass left unmatched
       against a given source type, score every cross-source pair
       (:func:`_score_candidate` — GitLab issue #168's fix: this scores the
       actual ``GraphNode`` pair, so a shared vSphere ``uuid``/
       ``instance_uuid`` or Rapid7 ``mac`` floors the score into the
       auto-emit band independent of name similarity, instead of the old
       name-string-only ``_score_pair``). ``>= FUZZY_AUTO_EMIT_MIN`` with no
       counter-evidence → ``probabilistic_match`` at 0.800; ``>=
       FUZZY_REVIEW_MIN`` (or an auto-emit-band score WITH counter-evidence)
       → review queue; below → dropped. The TRK-087 domain guard applies
       here too.

       P5 GAP 2 widened what reaches the queue, never what auto-emits: a pair
       with dissimilar names but one shared correlatable IP is floored at
       exactly ``FUZZY_REVIEW_MIN`` by :func:`_score_candidate`, so it becomes
       a review QUESTION instead of being dropped unasked. That is the class
       ``host_reconcile._emit_cross_hostname_ip_edges`` used to ASSERT at 0.70
       in the legacy store; relocating it to the queue is what let that writer
       be deleted without losing the capability. It can never auto-emit — the
       shared-IP nudge is capped strictly below ``FUZZY_AUTO_EMIT_MIN``.

    Every review-queue candidate carries ``evidence``/``counter_evidence``/
    ``confidence_band`` (``"exact_ambiguous"`` | ``"fuzzy_review"`` |
    ``"corroborated"``) so a reviewer cannot misread a single queued
    candidate as high confidence — see :func:`_candidate_payload`. After both
    passes, any candidate node claimed by two or more different source
    questions is stamped ``mutually_exclusive`` on each affected candidate
    (GitLab issue #168 FIX item 5) before queuing.

    Counts returned: ``deterministic_edges``, ``probabilistic_edges``,
    ``review_queued``, ``domain_conflicts_suppressed``,
    ``ambiguous_keys``, ``nodes_considered``, the authority-model gate counters
    (``human_confirmed_skipped``, ``human_vetoed_skipped``, ``pending_gated``,
    ``veto_requeued``, ``auto_edges_withdrawn``) and ``unsettled_legs_flagged``
    (TRK-341). Edge counts are *pairs*, not rows (each pair writes two directed
    rows).
    """
    counts = {
        "deterministic_edges": 0,
        "probabilistic_edges": 0,
        "review_queued": 0,
        "domain_conflicts_suppressed": 0,
        "ambiguous_keys": 0,
        "nodes_considered": 0,
        # Authority-model gates (KG-1/KG-3). Counted, not silent: a gate that
        # reports nothing is indistinguishable from a gate that is not wired
        # in, which is how the pre-fix code could overwrite human decisions
        # for months without anything looking wrong in the health stats.
        "human_confirmed_skipped": 0,
        "human_vetoed_skipped": 0,
        "pending_gated": 0,
        "veto_requeued": 0,
        "auto_edges_withdrawn": 0,
        # TRK-341: times a scored pair had an endpoint on an unsettled
        # host_reconcile identity leg (counted per leg per scored pair, so it
        # is a volume signal, not a pair count).
        "unsettled_legs_flagged": 0,
    }
    # TRK-341 / spec §4.4: loaded ONCE, and BEFORE anything can be emitted.
    # Fail-closed by construction — ambiguous_leg_index does not swallow load
    # errors, so an unreadable ambiguity state aborts the pass here rather
    # than letting it run to completion believing nothing is ambiguous.
    ambiguity = ambiguous_leg_index(session)

    if materialize:
        materialize_host_nodes(session)

    nodes = (
        session.execute(select(GraphNode).where(GraphNode.node_type.in_(HOST_NODE_TYPES)))
        .scalars()
        .all()
    )
    # KG-7: row order from an ORDER BY-less SELECT is unspecified (Postgres
    # gives no guarantee at all). Every downstream choice keyed off this list
    # -- most importantly the ambiguous-key branch's `anchor = group[0]`,
    # which determines the review-queue `target` string -- must be stable
    # run-to-run for the same underlying data, or the same ambiguous group
    # can queue under a different target on different runs and defeat
    # queue_for_review's idempotency (duplicate questions for one group).
    # Sorting on (node_type, natural_key) is fetch-order-independent and
    # requires no new index (both columns already exist).
    nodes = sorted(nodes, key=lambda n: (n.node_type, n.natural_key))
    counts["nodes_considered"] = len(nodes)
    if len(nodes) < 2:
        return counts

    # P5: counted once over the whole population, for the same reason the
    # ambiguity index is — "how many hosts claim this address" is a property of
    # the pass, not of a pair, and asking per pair would be a round trip per
    # candidate. Built AFTER the < 2 guard because a single node shares nothing.
    shared_ips = shared_ip_index(nodes)

    # --- pass 1: deterministic, keyed on the canonical normalized name ----
    by_key: dict[str, list[GraphNode]] = {}
    for node in nodes:
        key = normalize_host(node.name)
        if not key:
            continue
        by_key.setdefault(key, []).append(node)

    matched_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    #: nodes that got at least one deterministic match, per (node, other type)
    resolved_against: set[tuple[uuid.UUID, str]] = set()
    # GitLab issue #168: one shared queue, populated by BOTH passes below, so
    # the mutual-exclusivity inversion (after pass 2) and the single queuing
    # loop at the end see every diverted pair regardless of which pass
    # diverted it (pass 1's counter-evidence guard, or pass 2's ambiguous
    # band / counter-evidence guard).
    pending_review: dict[uuid.UUID, tuple[GraphNode, list[dict[str, Any]]]] = {}

    for key, group in by_key.items():
        per_type: dict[str, list[GraphNode]] = {}
        for node in group:
            per_type.setdefault(node.node_type, []).append(node)
        if len(per_type) < 2:
            continue  # only one source knows this name — nothing to link

        # Ambiguous: some source has >1 node under this key. Never guess.
        if any(len(v) > 1 for v in per_type.values()):
            counts["ambiguous_keys"] += 1
            anchor = group[0]
            _, cands = pending_review.setdefault(anchor.id, (anchor, []))
            for other in group:
                if other.id == anchor.id:
                    continue
                if other.node_type == anchor.node_type:
                    # confirm_same_as unconditionally refuses a pairing where
                    # both nodes share a node_type ("SAME_AS links per-source
                    # nodes of DIFFERENT sources, not two rows of one
                    # source"). Offering such a node as a review-queue
                    # candidate here would queue a question that can never
                    # actually be confirmed as presented — a within-source
                    # dedup question is out of scope for this queue, not a
                    # candidate for it.
                    continue
                # Authority gate: a pair a human has already decided (either
                # way), or one the machine has an open question about, is not
                # offered again — unless the re-ask ladder says the evidence
                # is now strictly stronger than what the rejecter judged.
                decision, prior_rejection = _gate_pair(session, anchor, other, counts)
                if decision == "skip":
                    continue
                # Corroboration (issue #168 FIX item 2) still applies INSIDE
                # an ambiguous group: the group as a whole can never
                # auto-merge (multiple real objects share the name), but a
                # candidate that also shares a hard identifier with the
                # anchor is a fundamentally stronger lead than one that only
                # shares the tied name — worth telling the reviewer.
                score, evidence, counter_evidence = _score_candidate(
                    anchor, other, ambiguity=ambiguity, shared_ips=shared_ips
                )
                band = (
                    "corroborated"
                    if evidence.get("corroborating_identifier")
                    else "exact_ambiguous"
                )
                payload = _candidate_payload(
                    other,
                    score,
                    f"exact normalized-name match on {key!r}, but the key is ambiguous "
                    f"({', '.join(f'{t}x{len(v)}' for t, v in sorted(per_type.items()))}) "
                    "— a human must pick",
                    evidence=evidence,
                    counter_evidence=counter_evidence,
                    confidence_band=band,
                )
                if prior_rejection is not None:
                    payload["previously_rejected"] = prior_rejection
                cands.append(payload)
            continue

        flat = [v[0] for v in per_type.values()]
        for i, a in enumerate(flat):
            for b in flat[i + 1 :]:
                if hosts_domain_conflict(a.name, b.name):
                    counts["domain_conflicts_suppressed"] += 1
                    logger.info(
                        "graph_phase3: TRK-087 suppressed SAME_AS %r (%s domain=%r vs %s "
                        "domain=%r)",
                        key,
                        a.node_type,
                        host_domain(a.name),
                        b.node_type,
                        host_domain(b.name),
                    )
                    continue
                # Authority gate (KG-1/KG-3), applied AFTER TRK-087's
                # domain-conflict suppression, never as a replacement for it.
                decision, prior_rejection = _gate_pair(session, a, b, counts)
                if decision == "skip":
                    continue
                score, evidence, counter_evidence = _score_candidate(
                    a, b, ambiguity=ambiguity, shared_ips=shared_ips
                )
                if counter_evidence or decision == "requeue":
                    # GitLab issue #168 FIX item 3: an exact normalized-name
                    # match is no longer an unconditional auto-merge if a hard
                    # conflict survives the corroboration check (e.g. same
                    # short hostname, two different live IPs, or a
                    # disabled/decommissioned flag). Never silently merge —
                    # divert to the same review queue pass 2 uses.
                    if counter_evidence:
                        reason = (
                            f"exact normalized-name match on {key!r}, but counter-evidence "
                            f"conflicts ({'; '.join(counter_evidence)}) — a human must decide"
                        )
                    else:
                        reason = (
                            f"exact normalized-name match on {key!r} with evidence stronger "
                            "than the class this pair was rejected at — re-asking, never "
                            "auto-merging over a human NO"
                        )
                    _withdraw_auto_same_as(session, a, b, counts)
                    _, cands = pending_review.setdefault(a.id, (a, []))
                    payload = _candidate_payload(
                        b,
                        score,
                        reason,
                        evidence=evidence,
                        counter_evidence=counter_evidence,
                        confidence_band="fuzzy_review",
                    )
                    if prior_rejection is not None:
                        payload["previously_rejected"] = prior_rejection
                    cands.append(payload)
                    continue
                edge_evidence = {
                    "basis": "normalized_hostname_exact",
                    "normalizer": "tools.hostmatch.normalize_host",
                    "normalized_key": key,
                    "source_name": a.name,
                    "target_name": b.name,
                    "score": 1.0,
                    **evidence,
                }
                _emit_same_as(
                    session,
                    a,
                    b,
                    method=GraphEdgeMethod.DETERMINISTIC_MATCH,
                    confidence=CONFIDENCE_DETERMINISTIC_NAME,
                    evidence=edge_evidence,
                )
                counts["deterministic_edges"] += 1
                matched_pairs.add(tuple(sorted((a.id, b.id), key=str)))  # type: ignore[arg-type]
                resolved_against.add((a.id, b.node_type))
                resolved_against.add((b.id, a.node_type))

    # --- pass 2: probabilistic, only for what pass 1 could not resolve ----
    by_type: dict[str, list[GraphNode]] = {}
    for node in nodes:
        by_type.setdefault(node.node_type, []).append(node)

    types = sorted(by_type)
    for i, type_a in enumerate(types):
        for type_b in types[i + 1 :]:
            for a in by_type[type_a]:
                if (a.id, type_b) in resolved_against:
                    continue
                for b in by_type[type_b]:
                    if (b.id, type_a) in resolved_against:
                        continue
                    pair = tuple(sorted((a.id, b.id), key=str))
                    if pair in matched_pairs:
                        continue
                    # Exact-key pairs belong to pass 1's jurisdiction ONLY.
                    # Either it emitted the edge, or it deliberately refused
                    # (ambiguous key / domain conflict / placeholder name /
                    # counter-evidence) — and a refusal must not be quietly
                    # overturned here by a fuzzy score of 1.000 on the same
                    # two names. Without this guard the "never silently
                    # force-merge" rule leaks: an ambiguous key queued for
                    # review would ALSO get an auto-emitted probabilistic edge.
                    key_a, key_b = normalize_host(a.name), normalize_host(b.name)
                    if key_a and key_a == key_b:
                        continue
                    # GitLab issue #168 FIX item 2: score on the actual nodes,
                    # not bare name strings, so a shared hard identifier
                    # (vSphere uuid/instance_uuid, Rapid7 mac) floors the score
                    # into the auto-emit band independent of name similarity —
                    # test (a)'s "different names, same uuid" case.
                    score, evidence, counter_evidence = _score_candidate(
                        a, b, ambiguity=ambiguity, shared_ips=shared_ips
                    )
                    if score < FUZZY_REVIEW_MIN:
                        continue
                    if hosts_domain_conflict(a.name, b.name):
                        counts["domain_conflicts_suppressed"] += 1
                        continue
                    # Authority gate (KG-1/KG-3), after TRK-087 as in pass 1.
                    decision, prior_rejection = _gate_pair(session, a, b, counts)
                    if decision == "skip":
                        continue
                    if (
                        score >= FUZZY_AUTO_EMIT_MIN
                        and not counter_evidence
                        and decision != "requeue"
                    ):
                        edge_evidence = {
                            "basis": "fuzzy_hostname_similarity",
                            "metric": "0.6*SequenceMatcher + 0.4*token_jaccard",
                            "score": score,
                            "threshold": FUZZY_AUTO_EMIT_MIN,
                            "source_name": a.name,
                            "target_name": b.name,
                            **evidence,
                        }
                        _emit_same_as(
                            session,
                            a,
                            b,
                            method=GraphEdgeMethod.PROBABILISTIC_MATCH,
                            confidence=CONFIDENCE_PROBABILISTIC_NAME,
                            evidence=edge_evidence,
                        )
                        counts["probabilistic_edges"] += 1
                        matched_pairs.add(pair)  # type: ignore[arg-type]
                        continue
                    # Ambiguous band, OR a score that would have auto-emitted
                    # except counter-evidence conflicts (issue #168 FIX item
                    # 3, test (b)) → queue, never emit.
                    band = (
                        "corroborated"
                        if evidence.get("corroborating_identifier")
                        else "fuzzy_review"
                    )
                    if decision == "requeue":
                        reason = (
                            f"score {score} with evidence stronger than the class this pair "
                            "was rejected at — re-asking, never auto-merging over a human NO"
                        )
                    elif score >= FUZZY_AUTO_EMIT_MIN:
                        reason = (
                            f"score {score} would auto-emit, but counter-evidence conflicts "
                            f"({'; '.join(counter_evidence)}) — a human must decide"
                        )
                    elif evidence.get("ip_floor_applied"):
                        # P5 GAP 2: say plainly that the names did NOT match and
                        # that 0.75 is a floor, not a similarity. A reviewer who
                        # reads this number as "close names" would approve a
                        # merge on nothing but a shared address.
                        reason = (
                            "dissimilar names but one shared IP — promoted to the review "
                            f"floor ({FUZZY_REVIEW_MIN}); name similarity alone was "
                            f"{_score_pair(a.name, b.name)}. A shared address is spoofable "
                            "and reused (DHCP/NAT/reassignment), so this is a question, "
                            "never an assertion"
                        )
                    else:
                        reason = (
                            f"fuzzy name similarity {score} in the ambiguous band "
                            f"[{FUZZY_REVIEW_MIN}, {FUZZY_AUTO_EMIT_MIN}) — not auto-merged"
                        )
                    _withdraw_auto_same_as(session, a, b, counts)
                    _, cands = pending_review.setdefault(a.id, (a, []))
                    payload = _candidate_payload(
                        b,
                        score,
                        reason,
                        evidence=evidence,
                        counter_evidence=counter_evidence,
                        confidence_band=band,
                    )
                    if prior_rejection is not None:
                        payload["previously_rejected"] = prior_rejection
                    cands.append(payload)

    # GitLab issue #168 FIX item 5: invert pending_review into a
    # target(candidate)->claiming-sources map. If two DIFFERENT source
    # records both propose the SAME target node as a candidate match,
    # confirming one implicitly rules out the other — a reviewer needs to
    # know that BEFORE approving either, not discover it after the fact.
    target_to_sources: dict[str, list[GraphNode]] = {}
    for src_node, cands in pending_review.values():
        for cand in cands:
            target_to_sources.setdefault(cand["node_id"], []).append(src_node)

    for src_node, cands in pending_review.values():
        for cand in cands:
            claimants = target_to_sources.get(cand["node_id"], [])
            others = [s for s in claimants if s.id != src_node.id]
            if others:
                cand["mutually_exclusive"] = sorted({_review_target(o) for o in others})

    for node, cands in pending_review.values():
        if queue_for_review(session, node, cands) is not None:
            counts["review_queued"] += 1

    counts["unsettled_legs_flagged"] = ambiguity.hits
    logger.info("graph_phase3: entity resolution %s", counts)
    return counts


# ---------------------------------------------------------------------------
# Traversal — bounded, ranked, summarised
# ---------------------------------------------------------------------------


def _walk(
    session: Any,
    *,
    table: Any,
    from_col: Any,
    to_col: Any,
    type_col: Any,
    conf_col: Any,
    meta_col: Any,
    method_col: Any,
    extra_filters: list[Any],
    root_id: uuid.UUID,
    max_hops: int,
    min_confidence: float,
    row_limit: int,
    store: str,
) -> list[dict[str, Any]]:
    """Bounded, cycle-safe recursive-CTE walk of ONE directed edge table.

    Built with SQLAlchemy Core's recursive CTE rather than hand-written SQL
    text, so the identical query runs on PostgreSQL in production and on
    SQLite in the test suite. (``db.relationships._WALK_SQL`` — the older
    walker — uses ``= ANY(:array)``, which is Postgres-only and therefore
    cannot be exercised end-to-end by this repo's sqlite suite. Reusing it
    would have meant shipping traversal whose only tests are mock-session
    call-count assertions.)

    Direction: the walk is UNDIRECTED for reachability (an edge is followed
    from either endpoint) while every returned row keeps the edge's true
    stored ``from``/``to``. ``neighbour_id`` is the endpoint that is NOT the
    node we arrived from, which is what a blast-radius reader actually wants.

    Bounded three ways, mirroring the older walker's discipline:

    * ``max_hops`` — caller-supplied, already clamped to :data:`MAX_HOPS`.
    * ``visited`` — a ``|``-delimited path string; a node already on this
      path is never re-stepped-onto. Essential here, not decorative:
      ``SAME_AS`` is stored in both directions, so an unguarded walk loops
      immediately.
    * ``row_limit`` — a hard ``LIMIT`` on the final select.

    ``min_confidence`` (KG-6): honored TRANSITIVELY, not just as a filter on
    the rows finally returned. A sub-threshold edge cannot be traversed AT
    ALL — its far endpoint never becomes a frontier node for the next hop —
    so a node reachable only via a path that dips below the confidence floor
    can never appear, even if some LATER edge on that path individually
    meets the threshold. (An earlier version applied this only to the final
    row list, after the CTE had already expanded through low-confidence
    edges to find higher-confidence ones further out — that let
    ``blast_radius(min_confidence=1.0)``, "declared edges only", report
    neighbours actually reachable only through a fuzzy identity guess,
    overstating certainty to an operator using this to judge impact.)
    ``confidence`` is coalesced to 0 before comparing so a NULL-confidence
    row keeps matching the historical default ``min_confidence=0.0``, exactly
    like the post-walk Python filter's ``confidence is None -> 0.0`` already
    did.
    """
    root_txt = str(root_id)
    conf_floor = func.coalesce(conf_col, 0) >= min_confidence

    # Frontier bookkeeping: ``node_id`` is the far endpoint of the edge we
    # just traversed, i.e. where the next hop starts from.
    anchor_neighbour = case((from_col == root_id, to_col), else_=from_col)
    anchor = select(
        from_col.label("from_id"),
        to_col.label("to_id"),
        type_col.label("edge_type"),
        method_col.label("method"),
        conf_col.label("confidence"),
        meta_col.label("meta"),
        literal(1).label("hop"),
        anchor_neighbour.label("node_id"),
        (
            literal("|")
            + literal(root_txt)
            + literal("|")
            + cast(anchor_neighbour, String)
            + literal("|")
        ).label("visited"),
    ).where(
        or_(from_col == root_id, to_col == root_id),
        from_col != to_col,
        conf_floor,
        *extra_filters,
    )

    walk = anchor.cte("walk", recursive=True)

    step_neighbour = case((from_col == walk.c.node_id, to_col), else_=from_col)
    step = select(
        from_col.label("from_id"),
        to_col.label("to_id"),
        type_col.label("edge_type"),
        method_col.label("method"),
        conf_col.label("confidence"),
        meta_col.label("meta"),
        (walk.c.hop + 1).label("hop"),
        step_neighbour.label("node_id"),
        (walk.c.visited + cast(step_neighbour, String) + literal("|")).label("visited"),
    ).where(
        or_(from_col == walk.c.node_id, to_col == walk.c.node_id),
        from_col != to_col,
        walk.c.hop < max_hops,
        walk.c.visited.notlike(literal("%|") + cast(step_neighbour, String) + literal("|%")),
        conf_floor,
        *extra_filters,
    )

    walk = walk.union_all(step)

    rows = session.execute(
        select(
            walk.c.from_id,
            walk.c.to_id,
            walk.c.edge_type,
            walk.c.method,
            walk.c.confidence,
            walk.c.meta,
            walk.c.hop,
            walk.c.node_id,
        )
        .order_by(walk.c.hop)
        .limit(row_limit)
    ).all()

    out: list[dict[str, Any]] = []
    for from_id, to_id, edge_type, method, confidence, meta, hop, neighbour_id in rows:
        conf = float(confidence) if confidence is not None else 0.0
        if conf < min_confidence:
            continue
        out.append(
            {
                "store": store,
                "from_id": str(from_id),
                "to_id": str(to_id),
                "neighbour_id": str(neighbour_id),
                "edge_type": edge_type,
                "method": method,
                "confidence": conf,
                "evidence": meta,
                "hop": int(hop),
            }
        )
    return out


def _graph_edge_walk(
    session: Any,
    root_id: uuid.UUID,
    max_hops: int,
    min_confidence: float,
    row_limit: int,
) -> list[dict[str, Any]]:
    """Walk Phase 2's ``graph_edges`` store from a ``graph_nodes`` root.

    Only ACTIVE edges (``valid_to IS NULL``) participate — the bitemporal
    store's retired history is deliberately invisible to a "what is connected
    NOW" question. Indexes used: ``ix_graph_edges_source`` /
    ``ix_graph_edges_target``.

    ``NOT_SAME_AS`` is excluded outright: it is a NEGATIVE assertion, not
    connectivity. Walking it would make "these two are definitely different
    machines" propagate blast radius between them — the precise opposite of
    what the edge means.
    """
    edge = GraphEdge.__table__
    return _walk(
        session,
        table=edge,
        from_col=edge.c.source_id,
        to_col=edge.c.target_id,
        type_col=edge.c.edge_type,
        conf_col=edge.c.confidence,
        meta_col=edge.c.evidence,
        method_col=edge.c.method,
        extra_filters=[
            edge.c.valid_to.is_(None),
            edge.c.edge_type != GraphEdgeType.NOT_SAME_AS.value,
        ],
        root_id=root_id,
        max_hops=max_hops,
        min_confidence=min_confidence,
        row_limit=row_limit,
        store="graph_edges",
    )


# ---------------------------------------------------------------------------
# The legacy half of this walk — REMOVED (graph-first P5)
# ---------------------------------------------------------------------------
#
# Four pieces stood here until the P5 drop of ``resource_relationships``:
#
#   * ``GRAPH_SERVED_EDGE_TYPES`` — the explicit dedup set naming which types
#     ``graph_edges`` already served, so the legacy walk would not double-count
#     them. With one store there is nothing to dedup against.
#   * ``_resource_edge_walk`` — the second walk, over the legacy store.
#   * ``_identity_sibling_seeds`` / ``_seeded_legacy_rows`` — the P5-GAP-3
#     re-entry that imported a SAME_AS sibling's legacy facts into blast
#     radius. Built in this same wave, deleted in this same wave, on purpose:
#     it existed to bridge the months where identity lived in ``graph_edges``
#     while containment facts still lived in the legacy store. Post-drop there
#     are no legacy facts to re-enter, and ``_graph_edge_walk`` traverses
#     ``SAME_AS`` natively — a sibling and everything attached to it are
#     ordinary graph neighbours now.
#
# The one semantic from the seeding worth keeping is already kept elsewhere:
# a human ``NOT_SAME_AS`` stays non-traversable (``_graph_edge_walk`` filters
# the type out of the walk entirely), so a chain of machine ``SAME_AS`` hops
# still cannot outvote a recorded human "no".
#
# Containment facts (``HAS_MOUNT``, ``EXPOSES_PORT``, drift, …) were never
# migrated as edges — by decision, not omission (see the P3 migration's
# containment refusals). Consumers read the detail tables for them.


def _why(row: dict[str, Any]) -> str:
    """One-line, human-readable justification for an edge. Never a JSON dump."""
    ev = row.get("evidence") or {}
    method = row.get("method")
    bits = [f"{row['edge_type']} @ hop {row['hop']}"]
    if method:
        bits.append(f"method={method}")
    if isinstance(ev, dict):
        basis = ev.get("basis") or ev.get("join") or ev.get("match_basis")
        if basis:
            bits.append(str(basis)[:120])
        if ev.get("approver"):
            bits.append(f"confirmed by {ev['approver']}")
        if ev.get("score") is not None:
            bits.append(f"score={ev['score']}")
    bits.append(f"confidence={row['confidence']:.3f}")
    bits.append(f"via {row['store']}")
    return "; ".join(bits)


def _node_summaries(session: Any, node_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = [uuid.UUID(i) for i in {str(x) for x in node_ids}]
    if not ids:
        return {}
    rows = session.execute(select(GraphNode).where(GraphNode.id.in_(ids))).scalars().all()
    return {
        str(n.id): {
            "node_id": str(n.id),
            "node_type": n.node_type,
            "name": n.name,
            "natural_key": n.natural_key,
            "source": n.source,
            "resource_id": str(n.resource_id) if n.resource_id else None,
        }
        for n in rows
    }


def _resource_summaries(session: Any, resource_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = [uuid.UUID(i) for i in {str(x) for x in resource_ids}]
    if not ids:
        return {}
    rows = session.execute(select(Resource).where(Resource.id.in_(ids))).scalars().all()
    return {
        str(r.id): {
            "resource_id": str(r.id),
            "name": r.name,
            "domain": r.domain,
            "type": r.type,
        }
        for r in rows
    }


def blast_radius(
    session: Any,
    node_id: uuid.UUID,
    max_hops: int = 2,
    min_confidence: float = 1.0,
    top_n: int = 25,
) -> dict[str, Any]:
    """ "What else is affected if this entity breaks?" — capped and ranked.

    Walks ``graph_edges`` from ``node_id`` (the only edge store since the P5
    drop of ``resource_relationships``). ``SAME_AS`` is traversed natively, so
    an identity sibling and everything attached to it are ordinary neighbours;
    a human ``NOT_SAME_AS`` is never traversed. Results are deduplicated per
    neighbour keeping the SHORTEST hop distance, ranked by (hop asc,
    confidence desc, name) and capped at ``top_n``.

    ``min_confidence`` defaults to 1.0, i.e. **declared edges only**. That is
    a deliberate default, not an accident: a blast-radius answer is used to
    decide what to touch during an incident, so derived/fuzzy identity claims
    must be opted into rather than silently assumed. Lower it (0.99 to include
    deterministic name matches, 0.8 to include fuzzy ones) when exploring.

    Returns a summary dict — never a raw subgraph:
    ``{root, hops, min_confidence, truncated, count, neighbors: [...]}`` where
    each neighbour carries ``node``/``resource``, ``edge_type``,
    ``hop_distance``, ``confidence`` and a one-line ``why``.
    """
    hops = max(1, min(int(max_hops), MAX_HOPS))
    limit = max(1, min(int(top_n), MAX_TOP_N))
    # Over-fetch so post-walk confidence filtering + dedup still has enough
    # rows to fill ``top_n``, but stay bounded.
    row_limit = min(limit * 20, MAX_TOP_N * 20)

    root = session.get(GraphNode, node_id)
    if root is None:
        return {"error": f"graph node {node_id} not found"}

    rows = _graph_edge_walk(session, node_id, hops, min_confidence, row_limit)

    node_meta = _node_summaries(session, [r["neighbour_id"] for r in rows] + [str(node_id)])

    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        nid = row["neighbour_id"]
        if nid == str(node_id):
            continue
        entry = {
            "node": node_meta.get(nid),
            # Always None since P5: every neighbour is a graph node now (the
            # legacy resource-keyed walk is gone). Key kept so the response
            # shape is stable for existing consumers.
            "resource": None,
            "edge_type": row["edge_type"],
            "hop_distance": row["hop"],
            "confidence": row["confidence"],
            "why": _why(row),
        }
        prior = best.get(nid)
        if prior is None or (entry["hop_distance"], -entry["confidence"]) < (
            prior["hop_distance"],
            -prior["confidence"],
        ):
            best[nid] = entry

    ordered = sorted(
        best.values(),
        key=lambda e: (
            e["hop_distance"],
            -e["confidence"],
            ((e["node"] or e["resource"] or {}).get("name") or ""),
        ),
    )
    return {
        "root": {
            "node_id": str(root.id),
            "node_type": root.node_type,
            "name": root.name,
            "resource_id": str(root.resource_id) if root.resource_id else None,
        },
        "hops": hops,
        "min_confidence": min_confidence,
        "count": min(len(ordered), limit),
        "total_found": len(ordered),
        "truncated": len(ordered) > limit,
        "neighbors": ordered[:limit],
    }


def root_cause_candidates(
    session: Any,
    node_id: uuid.UUID,
    since: datetime,
    top_n: int = 10,
) -> dict[str, Any]:
    """ "What changed nearby, recently, that could explain this?"

    Walks out to 2 hops (the issue's stated root-cause radius) over
    ``graph_edges`` at ``min_confidence=0.0`` — unlike blast radius, a root-cause
    search WANTS the weaker links: a merely-probable identity link is a
    perfectly good lead for a human investigating an incident, and each
    candidate reports the confidence and hop distance it came from so the
    reader can discount it. Blast radius decides what to touch; this decides
    what to look at, so the defaults differ on purpose.

    Neighbours are mapped to ``resources.id`` via ``graph_nodes.resource_id``
    (the root's own resource is included at hop zero), then
    ``drift_events`` at or after ``since`` on those resources are returned,
    ranked by (hop asc, detected_at desc) and capped at ``top_n``.
    ``graph_maintenance``'s own self-telemetry domain is excluded (TRK-191)
    so the graph agent's internal bookkeeping can't masquerade as infra
    change. ``delta`` is a one-line ``field: old -> new`` summary, not the raw
    JSONB blobs.
    """
    limit = max(1, min(int(top_n), MAX_TOP_N))
    hops = 2
    row_limit = min(limit * 40, MAX_TOP_N * 20)

    root = session.get(GraphNode, node_id)
    if root is None:
        return {"error": f"graph node {node_id} not found"}

    rows = _graph_edge_walk(session, node_id, hops, 0.0, row_limit)

    # resource_id -> (hop, edge_type, why) keeping the closest hop.
    reach: dict[str, tuple[int, str, str]] = {}

    def _note(rid: str | None, row: dict[str, Any]) -> None:
        if not rid:
            return
        cur = reach.get(rid)
        if cur is None or row["hop"] < cur[0]:
            reach[rid] = (row["hop"], row["edge_type"], _why(row))

    if root.resource_id is not None:
        reach[str(root.resource_id)] = (0, "SELF", "the queried entity itself")

    node_meta = _node_summaries(session, [r["neighbour_id"] for r in rows])
    for row in rows:
        meta = node_meta.get(row["neighbour_id"])
        _note(meta.get("resource_id") if meta else None, row)

    if not reach:
        return {
            "root": {"node_id": str(root.id), "node_type": root.node_type, "name": root.name},
            "since": since.isoformat(),
            "count": 0,
            "total_found": 0,
            "truncated": False,
            "candidates": [],
        }

    rids = [uuid.UUID(r) for r in reach]
    events = session.execute(
        select(DriftEvent, Resource)
        .join(Resource, Resource.id == DriftEvent.resource_id)
        .where(
            DriftEvent.resource_id.in_(rids),
            DriftEvent.detected_at >= since,
            Resource.domain != "graph_maintenance",
        )
        .order_by(DriftEvent.detected_at.desc())
        .limit(row_limit)
    ).all()

    candidates: list[dict[str, Any]] = []
    for event, resource in events:
        hop, edge_type, why = reach[str(event.resource_id)]
        candidates.append(
            {
                "change_event": {
                    "drift_event_id": str(event.id),
                    "drift_type": event.drift_type,
                    "field": event.field,
                    "status": event.status,
                    "detected_at": event.detected_at.isoformat() if event.detected_at else None,
                    "resource_name": resource.name,
                    "resource_domain": resource.domain,
                },
                "edge_type": edge_type,
                "hop_distance": hop,
                "delta": f"{event.field}: {_short(event.old_value)} -> {_short(event.new_value)}",
                "why": why,
            }
        )

    # Two-pass STABLE sort, least-significant key first: sort by detected_at
    # descending (most-recent-first, matching the docstring's promised order
    # and the DESC order the DB query above already fetched), then
    # stable-sort by hop_distance ascending. Because Python's sort is stable,
    # the second pass preserves the detected_at-descending order within each
    # hop tier rather than destroying it -- doing this in the opposite order
    # (or without `reverse=True` on the first pass) silently reverses the
    # within-tier ordering to oldest-first, which is the bug this fixes.
    candidates.sort(key=lambda c: c["change_event"]["detected_at"] or "", reverse=True)
    candidates.sort(key=lambda c: c["hop_distance"])
    return {
        "root": {"node_id": str(root.id), "node_type": root.node_type, "name": root.name},
        "since": since.isoformat(),
        "count": min(len(candidates), limit),
        "total_found": len(candidates),
        "truncated": len(candidates) > limit,
        "candidates": candidates[:limit],
    }


def _short(value: Any, width: int = 60) -> str:
    """Render a drift old/new JSONB value as a short, single-line string."""
    if value is None:
        return "None"
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


__all__ = [
    "CONFIDENCE_PROBABILISTIC_NAME",
    "EMITTER_SAME_AS",
    "EMITTER_SAME_AS_CONFIRMED",
    "FUZZY_AUTO_EMIT_MIN",
    "FUZZY_REVIEW_MIN",
    "HOST_NODE_TYPES",
    "MAX_HOPS",
    "MAX_TOP_N",
    "REVIEW_ACTION_TYPE",
    "AmbiguousLegIndex",
    "PairDecisionIndex",
    "ResourcePairGate",
    "SharedIpIndex",
    "ambiguous_leg_index",
    "blast_radius",
    "confirm_same_as",
    "get_reconciliation_state",
    "materialize_host_nodes",
    "pair_gate",
    "queue_for_review",
    "resolve_entities",
    "resource_pair_gate",
    "resource_pair_gate_index",
    "retract_same_as",
    "root_cause_candidates",
    "shared_ip_index",
]
