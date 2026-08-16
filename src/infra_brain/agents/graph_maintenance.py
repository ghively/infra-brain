"""GraphMaintenanceAgent — knowledge-graph edge health and gap-filling.

Runs as a post-collection maintenance pass (domain: "graph_maintenance"), every
two hours.

WHICH STORE THIS FILE TOUCHES (read this before adding anything)
----------------------------------------------------------------
Two edge stores exist. ``graph_nodes`` / ``graph_edges`` is the one the graph
lives in: bitemporal, provenance-carrying, authority-tagged, able to relate a
node that is not an inventory row. ``resource_relationships`` is the legacy
store — FK-constrained to ``resources`` and ``UNIQUE(from, to, type)``, so it
structurally cannot record that a relationship ENDED.

**This file writes ONLY ``graph_edges``.** rev10/T3 froze the legacy store
here by retiring the containment derivations; P5 (rev11-T5-B) finished the job
by deleting the rest — the typed-relationship pass, the convergence-node pass
and the vSphere-topology pass are gone in full, with the per-type dispositions
recorded in the "3b." epitaph inside the class. This module now contains no
``emit_edges_batch`` call, no ``RelationshipType`` reference and no import from
``db.relationships`` at all; that is a load-bearing property, pinned by
``tests/agents/test_graph_maintenance_legacy_writers_gone.py``. Historical rows
in the legacy table are NOT deleted from here — dropping the table is T3's
migration, and a maintenance pass quietly deleting rows first would destroy the
data every reader and the P3 backfill still depend on until it lands.

Sub-tasks per run:

1. _prune_stale_edges           — RETIRE (never delete) active ``graph_edges``
                                   whose endpoint node lost its backing
                                   ``resources`` row
2. _decay_confidence            — decay stale, re-observation-backed
                                   ``graph_edges``; retire below 0.500.
                                   NEVER touches an ``authority='human'`` or a
                                   ``method='declared'`` (structural) edge
3. _reconcile_contradictory_edges — retire an auto ``SAME_AS`` contradicted by
                                   an active human ``NOT_SAME_AS``
4. _fill_cross_domain_gaps      — no-op stats placeholder; identity gap-filling
                                   is HostReconcileAgent's job (Task 4.6)
5. graph_phase2 / role_tagging / graph_engine / graph_phase3 — the declarative
                                   and Phase-2/3 emitters, all writing
                                   ``graph_edges``. THIS is where the graph now
                                   comes from; skipped in scope="quick"
6. _link_new_resources          — no-op stats placeholder (Task 4.6)

``graph_phase3`` is the sole writer of ``SAME_AS``/``NOT_SAME_AS`` in
``graph_edges``. This agent never emits an identity edge — it only maintains
what that pass wrote.

scope="all"   runs every task (scheduled / manual use)
scope="full"  accepted and treated as "all"; it used to mean "ignore freshness
              gating", and the gate went with the derivation it gated
scope="quick" runs only prune + link_new (post-collection fast pass)
"""

import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_

from infra_brain import graph_engine, graph_phase2, graph_phase3, graph_role_tagging
from infra_brain.agents.base import BaseAgent, CollectOutcome
from infra_brain.db.models import (
    GraphEdge,
    GraphEdgeAuthority,
    GraphEdgeMethod,
    GraphEdgeType,
    GraphNode,
)
from infra_brain.db.session import get_session
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)


def _NOW() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# TRK-117 Phase 2: incremental freshness gating for the typed-relationship pass
#
# The full pass re-derives its entire typed-edge set from scratch every run.
# Most passes change nothing because the source collectors (windows/linux every
# 6h, vuln daily, …) run far less often than graph_maintenance (every 2h). Each
# cost-dominant edge-derivation block is tagged with the set of SOURCE DOMAINS
# it derives from; on a scope="all" pass, a block whose sources have produced no
# fresher data than the last successful full pass is SKIPPED (its prior edges
# are left in place — prune/decay are NOT gated) instead of re-derived.
#
# Row-level "updated_at" signals cannot be used: collectors delete-and-reinsert
# their detail rows every sweep, so nothing at the row level reliably indicates
# "unchanged". collection_runs (a completed run finished after the baseline) is
# the only trustworthy "did anything change" signal.
#
# _DERIVATION_VERSION: bump this WHENEVER a gated block's derivation LOGIC
# changes (not just data). A bump forces exactly one full backfill pass (the
# next run sees a version mismatch vs the version stamped in the last pass's
# stats) so a code change can't silently gate against stale logic forever.
# ---------------------------------------------------------------------------
# v2 (KG-1/KG-3): resolve_entities now gates every emission on the identity
# authority model -- a human-confirmed pair is never re-asserted automatically,
# a human-vetoed pair is never emitted, and an open question is never answered
# by the emitter that asked it. That changes what this pass derives, so the
# first post-deploy run must be distinguishable in the health history.
#
# v3 (TRK-341): resolve_entities now feeds host_reconcile's persisted unsettled
# identity legs (host_identities.identity_ambiguous_sources) into
# _score_candidate as counter-evidence, which lowers the pair's score and
# diverts pairs that previously auto-emitted. Same reason as v2: what this pass
# derives changed, so the first post-deploy run must be distinguishable.
#
# v4 (rev10/T3): the containment derivations are RETIRED — this pass no longer
# writes resource_relationships at all — and prune/decay/contradiction-reconcile
# moved onto graph_edges. This is the largest single change to what the pass
# derives since gating was added, so it must force one full backfill: the
# gate's baseline was computed against a derivation that emitted ~25 edge types
# this one does not, and gating against that baseline would be gating against
# logic that no longer exists.
# v5 (P5, rev11-T5-B): the typed-relationship / convergence-node /
# vSphere-topology derivation is DELETED IN FULL — this pass writes nothing to
# ``resource_relationships`` and mints no convergence ``resources`` row. That is
# a larger change to what the pass derives than v4 was, so the version bumps
# once more and the first post-deploy run is again distinguishable in the health
# history.
#
# WHY THIS CONSTANT SURVIVES ITS ONLY READER. The freshness gate that read it
# back (``_last_stamped_derivation_version``) went with the derivation it gated,
# so nothing consumes this value any more — it is now WRITE-ONLY, stamped into
# the graph-health report's ``metadata_``. It is kept deliberately: the stamp is
# how an operator (and the health history) tells a run of the new pass from a
# run of the old one while both are still present in ``collection_runs``, and it
# is the marker any future gate would key off. Bump it if what this pass derives
# ever changes again.
_DERIVATION_VERSION = 5


# ``_BlockSkip``, ``_BLOCK_SOURCE_DOMAINS``, ``_slug_key``,
# ``_is_bookkeeping_host``, ``_MAC_OUI_VENDOR`` and ``_mac_oui_vendor`` were
# DELETED here (P5, rev11-T5-B). Each supported exactly one part of the
# typed-relationship derivation and nothing else:
#   _BlockSkip / _BLOCK_SOURCE_DOMAINS  the freshness gate's control flow
#   _slug_key                           DEPLOYED_VIA's GitLab<->Octopus name match
#   _is_bookkeeping_host                a guard for HAS_VIOLATION's shadow nodes,
#                                       already dead since rev10/T3
#   _MAC_OUI_VENDOR / _mac_oui_vendor   HAS_VENDOR's LinuxNic lookup, likewise
# The OUI map is not a general utility and is not worth relocating:
# netdiscovery.py resolves vendors from nmap's own ``mac_vendor`` and has its
# own ``_FRAGILE_OUI_PREFIXES`` set for the BMC case.


# Default SAME_AS confidence decay window.  Overridden by
# settings.is_same_as_decay_days if the attribute exists. (Setting name kept:
# it is a live config field and the semantics are unchanged — only the store
# the window is applied to moved from resource_relationships to graph_edges.)
_DEFAULT_DECAY_DAYS = 14

# Decimal, not float: graph_edges.confidence is Numeric(4, 3) and
# graph_phase2.upsert_edge's honesty guard compares Decimals. Multiplying a
# Decimal by a float raises; quantizing to the column's millesimal precision
# keeps a decayed value representable rather than silently rounded on write.
_GRAPH_DECAY_FACTOR = Decimal("0.90")
_GRAPH_CONFIDENCE_QUANTUM = Decimal("0.001")
# Below this an edge is no longer trusted as an active assertion, so it is
# RETIRED (valid_to stamped). There is deliberately NO hard-delete tier below
# it: the legacy store needed one to bound growth of `status='archived'` rows,
# but a retired bitemporal row IS the history graph_edges exists to keep.
_GRAPH_DECAY_FLOOR = Decimal("0.500")

# TRK-089 (ported): per-type decay WINDOWS (in days) for the INFERENCE-backed
# graph_edges types. These decay ONLY when settings.graph_edge_decay_enabled is
# True; SAME_AS decay is unconditional and is NOT in this map (its window comes
# from settings.is_same_as_decay_days). The rationale is unchanged from the
# legacy set: these edges are asserted by re-observing evidence on a sweep
# cadence, so evidence not re-seen within the window has almost certainly gone
# away — but nothing retracts the edge, because edge RETIREMENT still lacks a
# "saw the full population" signal (design-doc addendum, "Still open").
#
#   HAS_ROLE        45 — graph_role_tagging's hostname-pattern / annotation
#                        inference (0.85 / 0.65). A host renamed out of its
#                        pattern keeps its old role forever otherwise.
#   IN_ENVIRONMENT  45 — same emitter, FQDN-suffix inference (0.90).
#
# DELIBERATELY ABSENT — the structural/declared tier: HOSTED_ON,
# MOUNTS_DATASTORE, AFFECTED_BY_CVE, RUNS_ON, BELONGS_TO, DEFINED_IN,
# RUNS_IMAGE. They assert currently-true topology from source-of-truth
# relational data and must NEVER decay. Absence is not the only guard —
# _decay_confidence also filters method != 'declared' — because a free-string
# edge_type vocabulary makes "forgot to exclude it" too cheap a mistake.
# NOT_SAME_AS is absent for a different reason: it is a human-only assertion
# and is additionally covered by the authority exemption.
_GRAPH_EDGE_DECAY_DAYS: dict[str, int] = {
    graph_role_tagging.EDGE_TYPE_HAS_ROLE: 45,
    graph_role_tagging.EDGE_TYPE_IN_ENVIRONMENT: 45,
}


def _declared_resource_backed_node_types() -> set[str]:
    """``graph_nodes.node_type`` values a collector declares as resource-backed.

    Read from ``AgentSpec.emits_nodes`` at call time rather than hardcoded, so
    a collector that declares a new node type is covered without an edit here
    — the whole point of the graph contract. Phase-2's hand-written node types
    are excluded by construction: they never appear in a ``NodeSpec``.

    Returns an empty set (and therefore prunes nothing) if the registry cannot
    be resolved — a maintenance sweep must degrade to a no-op, never to a
    guess about which edges are orphaned.
    """
    try:
        from infra_brain.etl.spec import agent_specs

        return {
            node.type
            for spec in agent_specs().values()
            for node in spec.emits_nodes
            if node.resource_backed
        }
    except Exception:  # noqa: BLE001
        logger.warning(
            "graph_maintenance: could not resolve declared node types; prune is a no-op this pass",
            exc_info=True,
        )
        return set()


# ``_EdgeBuffer`` was DELETED here (P5, rev11-T5-B). TRK-108 added it because
# the convergence run built ~340k edge dicts in memory before emitting and
# OOM-killed the scheduler container every two hours. With no edge emission left
# in this file there is nothing to buffer, and the OOM it existed to prevent
# cannot recur.


class GraphMaintenanceAgent(BaseAgent):
    """Maintain the knowledge graph.

    Orchestrates six sub-tasks per scheduled or manual run. See the module
    docstring for WHICH STORE each one writes — the short version is that
    everything here acts on ``graph_edges`` and NOTHING in this file writes
    ``resource_relationships`` any more (P5, rev11-T5-B).

    1. _prune_stale_edges           — retire (never delete) active graph_edges whose
                                      endpoint node lost its backing Resource row
    2. _decay_confidence            — decay stale re-observation-backed graph_edges;
                                      retire below 0.500. Never touches an
                                      authority='human' or method='declared' edge
    3. _reconcile_contradictory_edges — retire an auto SAME_AS contradicted by an
                                      active human NOT_SAME_AS (authority spec §3.3)
    4. _fill_cross_domain_gaps      — no-op stats placeholder (HostReconcileAgent's job)
    5. graph_phase2/role_tagging/graph_engine/phase3 — the `graph_nodes`/`graph_edges`
                                      store (TRK-196/197, GitLab #128): Phase 2's
                                      HOSTED_ON/MOUNTS_DATASTORE/AFFECTED_BY_CVE
                                      emitters, role/environment tagging, the
                                      DECLARATIVE engine, then Phase 3's cross-source
                                      SAME_AS entity resolution (which depends on the
                                      nodes the earlier three wrote); skipped in
                                      scope="quick" like the typed-relationship pass.
    6. _link_new_resources          — no-op stats placeholder (HostReconcileAgent's job)
    """

    spec = AgentSpec(
        domain="graph_maintenance",
        tier=Tier.RECONCILER,
        # TRK-117: moved off the ":00" slot. The old `0 */2 * * *` fired at
        # minute :00 of every even hour, colliding at 00/06/12/18:00 with
        # linux's `0 */6 * * *` (which burns ~45s of SSH-fork work against an
        # unreachable fleet, TRK-099) plus host_reconcile (`*/30`) and
        # netdiscovery (`*/15`) — the four marks whose full pass had started
        # timing out. Minute :50 is used by no other cron and avoids the
        # `*/6h` wave (0,5,10,15,20,25), netdiscovery (0,15,30,45),
        # host_reconcile (0,30) and the 2AM/6AM daily clusters at every even
        # hour it fires. Verified by tests/scheduler/test_scheduler.py::
        # test_no_exact_schedule_collisions and dev_status's collision check.
        schedule="50 */2 * * *",
        max_staleness=timedelta(hours=4),
        # TRK-117: the full (scope="all") pass re-derives the entire typed-edge
        # set (~455k edges) and had grown to ~500-599s, riding the global 600s
        # ceiling and failing at the collision marks. Give it head-room while
        # the incremental freshness-gating (Phase 2) reduces the baseline. The
        # Redis dedup lock TTL tracks this automatically via
        # dedup.default_ttl_seconds(domain) -> collect_timeout_for_domain.
        collect_timeout_seconds=1200,
    )

    def collect(self, scope: str = "all") -> CollectOutcome:  # type: ignore[override]
        """Run all maintenance tasks and persist a summary stats record.

        scope="all"   — full pass: prune + decay + reconcile + gap-fill, then
                        the four graph_edges emitters (phase2, role tagging, the
                        declarative engine, entity resolution), then link-new.
        scope="full"  — identical to "all". It used to force a full recompute of
                        the freshness-GATED typed-edge blocks; both the blocks
                        and the gate are deleted (P5), so there is nothing left
                        for it to force. Kept because callers pass it.
        scope="quick" — fast pass (prune + link-new only; for post-collection hooks)

        F-008: emitter failures are accumulated in self._maint_errors and raised
        AFTER the session commit, so committed edges are preserved but the run
        is recorded status="failed" instead of silently "completed".

        TRK-191: this used to return the summary as a plain ``list[dict]`` item,
        which ETLConnector.run()'s generic pipeline then ``_upsert_resource``'d
        into a Resource AND wrote a fresh Snapshot for every run. DriftAgent is
        intentionally domain-agnostic (diffs whatever changed between a
        resource's last two snapshots), so it treated graph_maintenance's own
        ever-changing internal stats (timings, typed-edge counts, ...) as real
        fleet drift — an indefinitely-growing, non-fleet noise source in
        drift_events. Per F-008 (the same pattern rootcause/vuln_triage/
        compliance already use for detail-writer agents), this now writes its
        own summary Resource row directly — no Snapshot — and returns
        ``CollectOutcome(items=[], count_override=1)`` so no generic
        Resource/Snapshot pair is ever created by run().
        """
        stats: dict = {}
        self._maint_errors: list[str] = []
        # TRK-117: per-phase (and, inside _populate_typed_relationships, per
        # cost-dominant block) wall-clock timings, in seconds. Folded into the
        # returned stats record so future runs surface real cost in
        # collection_runs / logs without needing a live SSH session to profile.
        self._timings: dict[str, float] = {}

        with get_session() as session:
            with self._timed("prune"):
                pruned = self._prune_stale_edges(session)
            stats["pruned"] = pruned["pruned"]

            if scope != "quick":
                with self._timed("decay"):
                    decayed = self._decay_confidence(session)
                stats["decayed"] = decayed["decayed"]
                stats["removed_low_confidence"] = decayed["removed"]

                with self._timed("reconcile"):
                    reconciled = self._reconcile_contradictory_edges(session)
                stats["contradictory_edges_removed"] = reconciled["removed"]

                with self._timed("gaps"):
                    gaps = self._fill_cross_domain_gaps(session)
                stats["gaps_filled"] = gaps["filled"]

                # P5 (rev11-T5-B): the typed-relationship derivation and its
                # freshness gate were called from here. Both are deleted — see
                # the "3b." epitaph below. The three stats keys are KEPT and
                # pinned empty rather than dropped: the graph-health report is a
                # time series, and a key that vanishes reads as "the pass broke"
                # where an empty one reads as "the pass has nothing to derive".
                stats["typed_edges"] = {}
                stats["typed_edges_skipped"] = []
                stats["gating"] = "n/a"

                # Each call gets its OWN session.begin_nested() SAVEPOINT, same as
                # every other write block in this file (F-023): a raw session.flush()
                # failure on real PostgreSQL (unlike sqlite) leaves the connection in
                # InFailedSqlTransaction until a ROLLBACK TO SAVEPOINT recovers it. Without
                # this, one IntegrityError here would poison the session for
                # _link_new_resources AND the final session.commit() below, losing the
                # typed-relationship work the earlier blocks already flushed this pass.
                with self._timed("graph_phase2"):
                    try:
                        with session.begin_nested():
                            p2_counts, p2_errors = graph_phase2.emit_all(session)
                        stats["graph_phase2"] = p2_counts
                        for err in p2_errors:
                            self._maint_errors.append(f"graph_phase2: {err}")
                    except Exception as exc:  # noqa: BLE001
                        stats["graph_phase2"] = {}
                        self._maint_errors.append(f"graph_phase2: {exc}")

                with self._timed("graph_role_tagging"):
                    try:
                        with session.begin_nested():
                            rt_counts, rt_errors = graph_role_tagging.emit_all(session)
                        stats["graph_role_tagging"] = rt_counts
                        for err in rt_errors:
                            self._maint_errors.append(f"graph_role_tagging: {err}")
                    except Exception as exc:  # noqa: BLE001
                        stats["graph_role_tagging"] = {}
                        self._maint_errors.append(f"graph_role_tagging: {exc}")

                # P0/P1/P2 (docs/decisions/2026-08-11-graph-first-architecture.md):
                # the DECLARATIVE path. It runs ALONGSIDE everything above, not
                # instead of it — nothing is switched off until its replacement is
                # proven equivalent. P2 has met that bar for three relationships
                # so far, all iac's: BELONGS_TO (deleted from agents/iac.py), its
                # inverse DEFINED_IN and RUNS_IMAGE (both deleted from the
                # typed-relationship pass this file used to carry, once the
                # contract grew EdgeDirection.INVERSE and ChildSpec reach). As of
                # P5 it no longer runs ALONGSIDE a legacy derivation at all —
                # there is nothing left here to run alongside, so this and the
                # three emitters around it ARE the pass.
                #
                # Placed before entity resolution because a node should exist
                # before the resolver considers it. Its own SAVEPOINT + broad
                # except, exactly like the two blocks above: on real PostgreSQL a
                # flush failure poisons the session until ROLLBACK TO SAVEPOINT,
                # and one bad declaration must not cost the whole pass.
                with self._timed("graph_engine"):
                    try:
                        with session.begin_nested():
                            ge_counts, ge_errors = graph_engine.emit_all(session)
                        stats["graph_engine"] = ge_counts
                        for err in ge_errors:
                            self._maint_errors.append(f"graph_engine: {err}")
                    except Exception as exc:  # noqa: BLE001
                        stats["graph_engine"] = {}
                        self._maint_errors.append(f"graph_engine: {exc}")

                # Entity resolution runs LAST in this group — it depends on the
                # node set Phase 2 (vSphere/R7 CVE nodes) and role-tagging just
                # wrote/refreshed, and its own docstring requires deterministic
                # matches to be settled before the fuzzy pass considers a pair.
                with self._timed("graph_phase3_entity_resolution"):
                    try:
                        with session.begin_nested():
                            stats["graph_phase3_entity_resolution"] = graph_phase3.resolve_entities(
                                session
                            )
                    except Exception as exc:  # noqa: BLE001
                        stats["graph_phase3_entity_resolution"] = {}
                        self._maint_errors.append(f"graph_phase3_entity_resolution: {exc}")
            else:
                stats["decayed"] = 0
                stats["removed_low_confidence"] = 0
                stats["contradictory_edges_removed"] = 0
                stats["gaps_filled"] = 0
                stats["typed_edges"] = {}
                stats["typed_edges_skipped"] = []
                stats["gating"] = "n/a"
                stats["graph_phase2"] = {}
                stats["graph_role_tagging"] = {}
                stats["graph_engine"] = {}
                stats["graph_phase3_entity_resolution"] = {}

            with self._timed("link_new"):
                linked = self._link_new_resources(session)
            stats["new_linked"] = linked["linked"]

            session.commit()

        # TRK-117 Phase 2: stamp the derivation-logic version into the persisted
        # stats (the "graph-health" report Resource's metadata_). The NEXT pass
        # reads it back to decide whether a logic change requires a full backfill.
        stats["_derivation_version"] = _DERIVATION_VERSION

        # Round timings to milliseconds for compact storage/logging.
        stats["timings"] = {k: round(v, 3) for k, v in self._timings.items()}
        if self._timings:
            logger.info(
                "graph_maintenance: phase timings (s) %s",
                stats["timings"],
            )

        logger.info(
            "GraphMaintenanceAgent [scope=%s]: pruned=%d decayed=%d removed=%d "
            "gaps_filled=%d new_linked=%d",
            scope,
            stats["pruned"],
            stats["decayed"],
            stats["removed_low_confidence"],
            stats["gaps_filled"],
            stats["new_linked"],
        )

        if self._maint_errors:
            raise RuntimeError(
                f"{len(self._maint_errors)} graph-maintenance emitter(s) failed: "
                + "; ".join(self._maint_errors)
            )

        # TRK-191: persist stats onto the "graph-health" report Resource's
        # metadata_ directly (needed so _last_stamped_derivation_version can
        # keep reading _derivation_version back) — deliberately NOT via
        # ETLConnector.run()'s generic items-list path, and deliberately no
        # Snapshot write, so this resource never again feeds DriftAgent's
        # snapshot diffing. See the F-008 count_override contract below.
        from infra_brain.api._seeding import upsert_resource

        with get_session() as session:
            upsert_resource(
                session,
                name="graph-health",
                domain=self.domain,
                resource_type="graph_maintenance_report",
                metadata=stats,
                zone=self.settings.default_zone,
                source=type(self).__name__,
            )
            session.commit()

        return CollectOutcome(items=[], count_override=1)

    # ------------------------------------------------------------------
    # Timing instrumentation (TRK-117)
    # ------------------------------------------------------------------

    @contextmanager
    def _timed(self, name: str):
        """Accumulate wall-clock seconds spent in the block into self._timings.

        Additive: repeated entries for the same *name* sum (so a per-block name
        used across a loop, or a phase re-entered, totals correctly). Lazily
        initializes self._timings so direct unit-test calls into instrumented
        helpers (which don't go through collect()) don't AttributeError.
        """
        if not hasattr(self, "_timings"):
            self._timings = {}
        start = time.perf_counter()
        try:
            yield
        finally:
            self._timings[name] = self._timings.get(name, 0.0) + (time.perf_counter() - start)

    def _mark(self, name: str, start: float) -> None:
        """Additive per-block timing for straight-line blocks that can't be
        wrapped in the ``_timed`` context manager without re-indenting a large
        try/except. Call with ``start = time.perf_counter()`` captured at the top
        of the block; records on the success path (a failed block is separately
        logged, so missing its timing is acceptable)."""
        if not hasattr(self, "_timings"):
            self._timings = {}
        self._timings[name] = self._timings.get(name, 0.0) + (time.perf_counter() - start)

    # ------------------------------------------------------------------
    # Incremental freshness gating (TRK-117 Phase 2) — DELETED (P5)
    # ------------------------------------------------------------------
    #
    # ``_should_run_block`` / ``_compute_freshness_gate`` /
    # ``_last_successful_full_pass_started_at`` /
    # ``_last_stamped_derivation_version`` lived here, with
    # ``_BLOCK_SOURCE_DOMAINS`` at module level. Every one of them existed to
    # decide whether a TYPED-RELATIONSHIP block had to re-derive this pass.
    # With the derivation gone there is nothing left to gate: prune, decay,
    # contradiction-reconcile, the Phase-2/3 emitters and the declarative
    # engine were never gated (each has its own incremental logic or is cheap
    # enough not to need one), and ``scope='full'`` now differs from
    # ``scope='all'`` in nothing. The parameter is kept on ``collect()``
    # because callers and the scheduler pass it.
    #
    # ``_DERIVATION_VERSION`` deliberately SURVIVES this deletion — see its
    # comment at module level.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 1. Prune stale edges (graph_edges)
    # ------------------------------------------------------------------

    def _prune_stale_edges(self, session) -> dict:
        """Retire active ``graph_edges`` whose endpoint node lost its resource.

        **Store change (rev10/T3).** This used to run a
        ``DELETE FROM resource_relationships WHERE …NOT IN (SELECT id FROM
        resources)`` — the legacy store's referential-integrity sweep for
        orphans a direct SQL delete or a failed cascade left behind. That
        store is frozen (nothing in this file writes it any more), so the
        pass is re-pointed at the store the graph actually lives in.

        The graph_edges analogue is exact, not approximate.
        ``graph_nodes.resource_id`` carries ``ON DELETE SET NULL`` (chosen so
        losing a domain row never silently deletes graph history), so a
        deleted ``resources`` row leaves its node standing with a NULL
        ``resource_id`` and its edges asserting a relationship to something
        that no longer exists. Those are the orphans here.

        **Why the resource-backed set is read from the declarations rather
        than hardcoded, and why NULL alone is not the signal.** A NULL
        ``resource_id`` is perfectly legitimate for a shared VALUE node — a
        ``Cve``, a ``ContainerImage``, a role-tagging ``Role`` — which never
        had an owning resource. Pruning on NULL alone would retire live
        edges. ``graph_engine._emit_nodes`` writes ``resource_id=resource.id``
        for every ``NodeSpec(resource_backed=True)`` node, always non-NULL by
        construction, so for exactly those node types NULL can only mean the
        SET NULL fired. The set comes from ``AgentSpec.emits_nodes`` at call
        time, so a collector declaring a new node type is covered with no
        edit here — the anti-pluggability this file exists to stop being
        reintroduced. Phase-2's hand-written node types (``VsphereVM``,
        ``R7Asset``, …) are deliberately EXCLUDED: ``graph_phase2`` passes
        through whatever ``resource_id`` the domain row carries, which may
        legitimately be None, so NULL is not a deletion signal for them.

        **Retire, never DELETE** — ``graph_edges`` is bitemporal; the row
        stays with ``valid_to`` stamped, which is the whole reason the store
        was preferred. ``authority='human'`` edges are exempt: a named
        person's assertion is not retired by an automatic integrity sweep
        (edge-authority spec §2.2/§7). Retiring one is a human act via
        ``retract_same_as``.

        Returns {"pruned": N} (the stat name is preserved; N now counts
        RETIRED edges).
        """
        backed_types = _declared_resource_backed_node_types()
        if not backed_types:
            return {"pruned": 0}

        orphan_node_ids = [
            nid
            for (nid,) in session.query(GraphNode.id).filter(
                GraphNode.node_type.in_(sorted(backed_types)),
                GraphNode.resource_id.is_(None),
            )
        ]
        if not orphan_node_ids:
            return {"pruned": 0}

        stale = (
            session.query(GraphEdge)
            .filter(
                GraphEdge.valid_to.is_(None),
                GraphEdge.authority != GraphEdgeAuthority.HUMAN.value,
                or_(
                    GraphEdge.source_id.in_(orphan_node_ids),
                    GraphEdge.target_id.in_(orphan_node_ids),
                ),
            )
            .all()
        )
        pruned = graph_phase2.retire_edges(session, stale)
        if pruned:
            logger.info(
                "GraphMaintenanceAgent: retired %d graph_edges whose endpoint node lost "
                "its backing resource",
                pruned,
            )
        return {"pruned": pruned}

    # ------------------------------------------------------------------
    # 1b. Reconcile contradictory edges (graph_edges)
    # ------------------------------------------------------------------

    def _reconcile_contradictory_edges(self, session) -> dict:
        """Retire auto ``SAME_AS`` edges contradicted by a human ``NOT_SAME_AS``.

        **What this pass used to be, and why that job is finished.** Three
        hardcoded ``DELETE FROM resource_relationships`` statements removed
        rows two collectors had persisted with the INVERSE of the canonical
        direction (``octopus_project DEPLOYS_TO env``,
        ``octopus_machine DEPLOYED_TO env``,
        ``vsphere_cluster MEMBER_OF vsphere_datacenter`` — KG-1 / DL-C-7 /
        S-13 / S-14). It was an idempotent ONE-TIME cleanup behind fixed
        emitters, and it has converged: the live store holds zero rows of any
        of the three shapes. It is deleted rather than ported because the
        bug class it swept up cannot recur in ``graph_edges`` — direction is
        no longer each emitter's private convention but a declared
        ``EdgeSpec.direction`` (``FORWARD``/``INVERSE``) resolved centrally by
        ``graph_engine``, so an inverted edge is a contract error caught at
        declaration time, not a row to delete every two hours.

        **What contradiction means in the bitemporal store.** The
        edge-authority spec (§3.3, ``2026-08-10-graph-edge-authority-spec.md``)
        states the invariant: *a pair can never simultaneously carry an active
        ``SAME_AS`` and an active ``NOT_SAME_AS``*. ``confirm_same_as``
        enforces the ordering on the human path, and ``resolve_entities``
        pre-filters vetoed pairs — but nothing swept for a violation that
        slipped past both (a veto written between an emitter's pre-filter and
        its flush; an edge that predates the veto). That is the genuine
        contradiction this pass now reconciles, and it is the same *shape* of
        work as before: converge the store on the assertion that outranks.

        Direction of the fix is fixed by the authority model, never
        negotiable: the AUTO edge is retired, stamped with why. A human
        ``NOT_SAME_AS`` is never touched — retracting a veto is
        ``retract_same_as``'s job and needs a named human.

        Returns {"removed": N} (stat name preserved; N counts RETIRED edges).
        """
        vetoes = (
            session.query(GraphEdge.source_id, GraphEdge.target_id)
            .filter(
                GraphEdge.edge_type == GraphEdgeType.NOT_SAME_AS.value,
                GraphEdge.valid_to.is_(None),
                GraphEdge.authority == GraphEdgeAuthority.HUMAN.value,
            )
            .all()
        )
        if not vetoes:
            return {"removed": 0}

        # A veto is pair-scoped and stored symmetrically, but read it as
        # unordered anyway — one leg missing must not let the contradiction
        # survive.
        vetoed_pairs = set()
        for src, tgt in vetoes:
            vetoed_pairs.add(frozenset((str(src), str(tgt))))

        contradicting = [
            edge
            for edge in session.query(GraphEdge).filter(
                GraphEdge.edge_type == GraphEdgeType.SAME_AS.value,
                GraphEdge.valid_to.is_(None),
                GraphEdge.authority == GraphEdgeAuthority.AUTO.value,
            )
            if frozenset((str(edge.source_id), str(edge.target_id))) in vetoed_pairs
        ]
        for edge in contradicting:
            evidence = dict(edge.evidence or {})
            evidence["retired_reason"] = "contradicted_by_human_not_same_as"
            edge.evidence = evidence
        removed = graph_phase2.retire_edges(session, contradicting)
        if removed:
            logger.info(
                "GraphMaintenanceAgent: retired %d auto SAME_AS edge(s) contradicted by an "
                "active human NOT_SAME_AS",
                removed,
            )
        return {"removed": removed}

    # ------------------------------------------------------------------
    # 2. Decay IS_SAME_AS confidence
    # ------------------------------------------------------------------

    def _decay_confidence(self, session) -> dict:
        """Decay the confidence of stale, re-observation-backed ``graph_edges``.

        **Store change (rev10/T3).** This used to walk ``ResourceRelationship``
        rows. That store is frozen, so the pass moves to ``graph_edges`` — and
        the move is not a transliteration: the target store is bitemporal and
        authority-tagged, which changes two things on purpose.

        *The three-tier outcome becomes two.* The legacy tiers were
        decay → ``status='archived'`` → HARD DELETE (KG-6 added the archive
        tier precisely because the original hard-deleted a
        possibly-still-valid link with no review step). ``graph_edges`` has no
        ``status`` column and one absolute rule — **edges are retired by
        stamping ``valid_to``, never DELETEd** (``db/models/graph.py``,
        ``retire_edges``). So an edge that falls below ``_DECAY_FLOOR`` is
        RETIRED, and there is no third tier: the hard-delete floor
        (``_HARD_DELETE_FLOOR``) existed only to bound unbounded growth of
        archived rows, and a retired bitemporal row IS the history the store
        is for. Deleting it would destroy exactly what motivated moving here.

        *Two exemptions, both enforced, neither by omission alone.*

        1. ``authority='human'`` — never decayed, never retired. A named
           person's assertion does not erode because a machine stopped
           re-observing it (edge-authority spec §7.3: "decay never touching
           structural or confirmed edges"). Retirement is ``retract_same_as``.
        2. **Structural edges** — an FK-backed ``method='declared'`` edge
           asserts topology from source-of-truth relational data and must
           never decay. The legacy pass got this by OMISSION from
           ``_HEURISTIC_DECAY_DAYS``; here it is BOTH an allow-list
           (``_GRAPH_EDGE_DECAY_DAYS``, so a newly declared edge type is
           never silently swept in) AND an explicit ``method != 'declared'``
           guard, because a free-string ``edge_type`` vocabulary makes
           omission alone too easy to lose.

        *What is in the allow-list, and why.* ``SAME_AS`` decays
        unconditionally on ``settings.is_same_as_decay_days`` — the direct
        successor of the legacy ``IS_SAME_AS`` rule, and the one identity
        claim that genuinely goes stale when the resolver stops re-deriving
        it. ``HAS_ROLE`` / ``IN_ENVIRONMENT`` (``graph_role_tagging``, methods
        ``inferred_from_*``, confidence 0.65-0.90) are hostname/annotation
        INFERENCES re-asserted every pass; when a host is renamed out of its
        pattern nothing retracts the old role, so they decay — but only
        behind the strictly opt-in ``settings.graph_edge_decay_enabled``,
        the same gate TRK-089 put on the legacy heuristic set.
        ``HOSTED_ON`` / ``MOUNTS_DATASTORE`` / ``AFFECTED_BY_CVE`` /
        ``RUNS_ON`` / ``BELONGS_TO`` / ``DEFINED_IN`` / ``RUNS_IMAGE`` /
        ``NOT_SAME_AS`` are all absent, deliberately.

        Freshness is read from ``recorded_at``, which ``upsert_edge`` stamps
        on every W2 same-authority refresh — the ``graph_edges`` equivalent of
        the legacy ``last_seen``. ``valid_from`` is NOT used: it deliberately
        keeps the edge's true start across refreshes.

        Returns {"decayed": N, "removed": N, "retired": N} — ``removed`` is
        kept as an alias of ``retired`` so the historical
        ``removed_low_confidence`` stat keeps its meaning.
        """
        decay_days = int(
            getattr(self.settings, "is_same_as_decay_days", _DEFAULT_DECAY_DAYS)
            or _DEFAULT_DECAY_DAYS
        )

        # SAME_AS always decays; the inference-backed types are added ONLY
        # when the strictly-opt-in flag is on (coerce with bool(...) so a
        # MagicMock settings attr in tests does not read truthy — TRK-089).
        policy: dict[str, int] = {GraphEdgeType.SAME_AS.value: decay_days}
        if bool(getattr(self.settings, "graph_edge_decay_enabled", False)):
            policy.update(_GRAPH_EDGE_DECAY_DAYS)

        now = _NOW()
        # Per-type cutoffs; the coarse query uses the *widest* window so no
        # stale edge is missed, then each edge is re-checked against its own.
        cutoffs = {etype: now - timedelta(days=days) for etype, days in policy.items()}
        max_cutoff = max(cutoffs.values())

        stale = (
            session.query(GraphEdge)
            .filter(
                GraphEdge.edge_type.in_(sorted(policy.keys())),
                GraphEdge.valid_to.is_(None),
                GraphEdge.authority != GraphEdgeAuthority.HUMAN.value,
                GraphEdge.method != GraphEdgeMethod.DECLARED.value,
                GraphEdge.recorded_at < max_cutoff,
            )
            .all()
        )

        decayed = 0
        to_retire: list[GraphEdge] = []

        for edge in stale:
            # Normalize to tz-aware UTC — PostgreSQL returns aware datetimes,
            # SQLite (tests) returns naive ones, and comparing the two raises.
            recorded_at = edge.recorded_at
            if recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=UTC)
            if recorded_at >= cutoffs[edge.edge_type]:
                continue
            new_conf = (Decimal(edge.confidence) * _GRAPH_DECAY_FACTOR).quantize(
                _GRAPH_CONFIDENCE_QUANTUM, rounding=ROUND_HALF_UP
            )
            edge.confidence = new_conf
            if new_conf < _GRAPH_DECAY_FLOOR:
                evidence = dict(edge.evidence or {})
                evidence["retired_reason"] = "decayed_below_floor"
                evidence["retired_confidence"] = str(new_conf)
                edge.evidence = evidence
                to_retire.append(edge)
                logger.debug(
                    "GraphMaintenanceAgent: retiring low-confidence %s edge %s (decayed to %s)",
                    edge.edge_type,
                    edge.id,
                    new_conf,
                )
            else:
                decayed += 1

        retired = graph_phase2.retire_edges(session, to_retire)
        if decayed or retired:
            logger.info(
                "GraphMaintenanceAgent: graph_edges decay — decayed=%d retired=%d "
                "(types=%s, same_as_window=%d days, floor=%s, factor=%s)",
                decayed,
                retired,
                sorted(policy.keys()),
                decay_days,
                _GRAPH_DECAY_FLOOR,
                _GRAPH_DECAY_FACTOR,
            )
        return {"decayed": decayed, "removed": retired, "retired": retired}

    # ------------------------------------------------------------------
    # 3. Fill cross-domain gaps
    # ------------------------------------------------------------------

    def _fill_cross_domain_gaps(self, session) -> dict:
        """No-op retained for the maintenance pipeline's stats shape.

        This pass previously re-derived cross-domain IS_SAME_AS edges that
        collection-order races might have missed. HostReconcileAgent is now
        the sole writer of IS_SAME_AS edges (Task 4.6), so gap-filling
        identity links is no longer this agent's responsibility — the
        reconciler re-derives the full merged-host view (and therefore every
        IS_SAME_AS edge) from the source domain tables on every run, so there
        is no gap left for this pass to fill.

        INVESTIGATED rev10/T3 — no interplay with ``graph_role_tagging``, and
        the stub is deliberately NOT repurposed to create one. Role tagging
        writes only ``graph_edges`` (``upsert_node`` / ``upsert_edge``, node
        types ``Role`` / ``Environment``, edge types ``HAS_ROLE`` /
        ``IN_ENVIRONMENT``, methods ``inferred_from_*``); it touches no
        ``resource_relationships`` row and no identity edge, so it can neither
        leave a gap this pass would fill nor be affected by one. The one place
        the two DO meet is decay: role tagging's inferred edges are now in
        ``_GRAPH_EDGE_DECAY_DAYS``, flag-gated exactly like the legacy
        heuristic set — which is a decay concern, handled there, not a gap-fill
        one.

        The stub survives because ``collect()`` reports ``gaps_filled`` in the
        persisted stats and the health history reads it; removing it would put
        a hole in a time series for no gain.

        Returns {"filled": 0}.
        """
        return {"filled": 0}

    # ------------------------------------------------------------------
    # 3b. Typed-relationship / convergence-node / vSphere-topology derivation
    #     — DELETED IN FULL (P5, rev11-T5-B)
    # ------------------------------------------------------------------
    #
    # Nine methods (~2,900 lines) lived here: ``_populate_typed_relationships``
    # and everything it called — ``_get_or_create_node``,
    # ``_populate_convergence_nodes``, ``_populate_vsphere_fork_bridge``,
    # ``_retire_bridged_ghosts``, ``_vsphere_name_to_id``, ``_as_list``,
    # ``_populate_vsphere_topology`` and its metadata fallback. Every one of
    # them existed to write ``resource_relationships``; P5 drops that table, so
    # the whole derivation goes rather than being ported. What follows is the
    # record, because "we deleted 2,900 lines" is not a disposition.
    #
    # THE FOUR DISPOSITIONS, and which types fell into each
    # -----------------------------------------------------
    # (1) ALREADY DEAD ON ARRIVAL — the block was a comment marking a rev10/T3
    #     containment retirement (HAS_SOFTWARE, TAGGED_AS, HAS_VIOLATION,
    #     AFFECTED_BY, HAS_DRIFT, ON_FIELD, HAS_ACCOUNT, EXPOSES_PORT,
    #     HAS_CERTIFICATE, HAS_SHARE, HAS_CRON, HAS_MOUNT, HAS_PENDING_UPDATE,
    #     HAS_VENDOR, HAS_FIREWALL_RULE, HAS_SECURITY_POSTURE, HAS_VARIABLE,
    #     HAS_SCHEDULE, HAS_SNAPSHOT, the ESXi hardware attributes, RELATED_TO,
    #     INSTANCE_OF, the octopus half of HAS_ROLE). Nothing to delete but the
    #     epitaph itself, which stays in db/relationships.py's
    #     RETIRED_CONTAINMENT_DERIVATIONS where it belongs.
    #
    # (2) RETIRED-DOMAIN DEAD CODE — vSphere has no configured credentials, so
    #     every vsphere_* detail table this derivation read is empty and every
    #     block over it emitted zero rows: the RUNS_ON / MEMBER_OF /
    #     IN_DATACENTER / STORED_ON / ATTACHED_TO topology, the
    #     GRANTED_ON / RAISED_ON / ASSIGNED_TO governance edges,
    #     RUNS_TOOLS_VERSION, the esxi_build / syslog / ntp convergence,
    #     BACKED_BY_FILER, the vmdk HAS_DISK/STORED_ON pair, IN_POOL, and the
    #     ``vsphere_fork`` IS_SAME_AS bridge + the ghost retirement that
    #     consumed it. The bridge's consumer went with it deliberately:
    #     ``_retire_bridged_ghosts`` READ ``resource_relationships`` to find
    #     ghosts to flag, so with the writer gone (and the table going) it could
    #     only ever be a no-op over rows already flagged on every pass since
    #     TRK-102. Its ``Resource.retired_at`` stamps are already applied and
    #     are NOT reverted.
    #
    # (3) ARTIFACT-PRODUCING — the block's OUTPUT was a bookkeeping node this
    #     codebase mints and then only ever reads back through the very edge
    #     being deleted. Every convergence node was in this class: a grep of
    #     ``src/`` finds no reader for ``vuln/cve``, ``software/software_title``,
    #     ``software/vendor``, ``network/subnet``, ``os/os_version``,
    #     ``os/service``, ``network/vlan``, ``network/syslog_target``,
    #     ``network/ntp_server``, ``network/nfs_filer``, ``rapid7/site``,
    #     ``ansible/inventory_group``, ``octopus/team``,
    #     ``octopus/library_variable_set``, ``octopus/tentacle_version``,
    #     ``security/local_group``, or any ``vsphere/*`` convergence type. The
    #     ONE exception was ``identity/user_account``, read by identity.py's
    #     ``_emit_is_principal_for_edges`` — which the sibling rev11-T5-A wave
    #     deletes, because it was itself a legacy-store writer. ``eol/product``
    #     is not an exception either: eol.py and the MCP ``add_eol_product``
    #     path both mint those rows and keep doing so.
    #
    # (4) ACCEPTED LOSS — a GENUINE derivation over LIVE data, deleted with the
    #     store because its declaration path is not yet built. Exactly two:
    #
    #     MEMBER_OF (host -> ansible/inventory_group).  The fact is NOT lost:
    #     ``ansible_inventory_groups`` ⋈ ``ansible_inventory_hosts`` holds every
    #     group and every member verbatim, and the audit ran the reconstruction
    #     query against it. What is lost until TRK-359's declaration path lands
    #     is the DERIVED graph hop from a linux/windows host node to its group.
    #     It could not simply be re-declared here: the group has no ``resources``
    #     row of its own (this deleted code minted the shadow), and a NodeSpec
    #     identified from child rows may not also ``gather`` — the identical
    #     blocker recorded for PART_OF in db/relationships.py's
    #     DEFERRED_RELATIONSHIP_TYPES. TRK-359 is where that grammar gap gets
    #     closed; do not re-derive it here in the meantime.
    #
    #     RUNS_EOL (host -> eol/product).  Same shape. The fact lives in
    #     ``eol_registry`` (asset_name + eol_date + the representative
    #     resource_id) joined to ``linux_hosts``/the host tables, and the audit
    #     proved that reconstruction too. eol.py is the natural owner of the
    #     declaration — it already writes both endpoints' rows — and TRK-359
    #     tracks it. Until then the hop is derived by nothing, on purpose.
    #
    #     Both are recorded as ACCEPTED LOSSES by the integrator, not as
    #     oversights: the alternative was blocking the table drop on two
    #     declaration paths that are not ready.
    #
    # WHAT REPLACED ALL OF THIS: nothing here. ``collect()`` still runs
    # ``graph_phase2.emit_all``, ``graph_role_tagging.emit_all``,
    # ``graph_engine.emit_all`` (the DECLARATIVE path) and
    # ``graph_phase3.resolve_entities``, and still prunes/decays/reconciles
    # ``graph_edges``. That is the whole job now.

    # ------------------------------------------------------------------
    # 4. Link newly-created resources
    # ------------------------------------------------------------------

    def _link_new_resources(self, session) -> dict:
        """No-op retained for the maintenance pipeline's stats shape.

        This pass previously emitted IS_SAME_AS edges for Resource rows
        created in the last 2 hours that had no outgoing edges yet (a
        collection-order race against host_reconcile). HostReconcileAgent is
        now the sole writer of IS_SAME_AS edges (Task 4.6): it re-derives the
        full merged-host view — and every resulting IS_SAME_AS edge — from
        the source domain tables on every scheduled run (every 30 minutes),
        so a newly-created resource is picked up on the next reconcile pass
        without needing a separate "link new resources" probe here.

        Returns {"linked": 0}.
        """
        return {"linked": 0}
