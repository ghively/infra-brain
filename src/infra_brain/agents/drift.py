import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from infra_brain.db.models import Resource

from deepdiff import DeepDiff
from deepdiff.path import parse_path
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from infra_brain.agents.base import (
    BaseAgent,
    CollectionResult,
    CollectorSkipped,
    count_drift_events_for_run,
)
from infra_brain.callbacks.dlp import redact_pans_preserving_uuids
from infra_brain.db.models import AgentActionLog, DriftEvent, Resource, Snapshot
from infra_brain.db.session import get_session
from infra_brain.etl.base import ScrubbedErrorList, scrub_dsn
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)

_MISSING = object()

# GitLab #158-follow-up / final-branch-review fix for TRK-256: hard per-call
# cap on how many open DriftEvent rows one _detect_for_resources() batch may
# auto-resolve. Mirrors resolve_drift_events' _CLOSURE_BATCH_CAP (500) in
# mcp_server.py — same "bound the blast radius of one bulk write" rationale.
# Not imported from mcp_server directly: that module pulls in fastmcp/uvicorn
# and a large, unrelated import surface; duplicating one int here avoids
# coupling a core agent module to the MCP server's import graph. If the two
# ever need to move in lockstep, promote this to a small shared constants
# module instead of importing mcp_server from drift.py.
_AUTO_RESOLVE_BATCH_CAP = 500


def _field_from_path(path: str) -> str:
    """Render a DeepDiff path string as a dotted/bracketed field name.

    AA-C-6: the previous implementation did naive string replacement
    (``path.replace("root['", "").replace("']", "")``) which only handled a
    single top-level dict key. Any nested path — ``root['a']['b'][0]['c']``,
    a list index, a mix of dict/list levels — produced a mangled field name
    (stray brackets/quotes left in, or indices swallowed). ``parse_path``
    (DeepDiff's own path parser) returns the real ordered list of keys/
    indices, which we then render as ``a.b[0].c``.
    """
    parts = parse_path(path)
    rendered = ""
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif rendered:
            rendered += f".{part}"
        else:
            rendered = str(part)
    return rendered or path


def _get_by_path(data: Any, path: str) -> Any:
    """Look up the value at a DeepDiff path inside *data*; ``_MISSING`` if absent."""
    current = data
    for part in parse_path(path):
        try:
            if isinstance(part, int):
                current = current[part]
            else:
                current = current[part]
        except (KeyError, IndexError, TypeError):
            return _MISSING
    return current


_FIELD_TOKEN_RE = re.compile(r"\[(\d+)\]|([^.\[\]]+)")


def _get_by_rendered_field(data: Any, field: str) -> Any:
    """Look up the value at a ``_field_from_path``-rendered field name (e.g.
    ``"tags.env"`` or ``"nested.list[1].a"``) inside *data*; ``_MISSING`` if
    absent.

    NOT the same as ``_get_by_path``: that function consumes DeepDiff's own
    ``root['a']['b']`` path syntax via ``parse_path``, which assumes a
    ``root``-prefixed input and silently drops the first token of a bare
    dotted path like ``"tags.env"`` (``parse_path`` treats the first segment
    as the path's root element, not as data). This function instead tokenizes
    the *rendered* field-name grammar that ``_field_from_path`` itself
    produces, so it round-trips correctly.
    """
    current = data
    for idx_str, name in _FIELD_TOKEN_RE.findall(field):
        try:
            current = current[int(idx_str)] if idx_str else current[name]
        except (KeyError, IndexError, TypeError):
            return _MISSING
    return current


def diff_snapshots(old: dict, new: dict) -> list[dict]:
    """Returns list of {field, old_value, new_value, drift_type} for each change."""
    result = []
    diff = DeepDiff(old, new, ignore_order=True)

    for path, change in diff.get("values_changed", {}).items():
        result.append(
            {
                "field": _field_from_path(path),
                "old_value": change["old_value"],
                "new_value": change["new_value"],
                "drift_type": "config_drift",
            }
        )

    for path in diff.get("dictionary_item_added", set()):
        path = str(path)
        new_value = _get_by_path(new, path)
        result.append(
            {
                "field": _field_from_path(path),
                "old_value": None,
                "new_value": None if new_value is _MISSING else new_value,
                "drift_type": "config_drift",
            }
        )

    for path in diff.get("dictionary_item_removed", set()):
        path = str(path)
        old_value = _get_by_path(old, path)
        result.append(
            {
                "field": _field_from_path(path),
                "old_value": None if old_value is _MISSING else old_value,
                "new_value": None,
                "drift_type": "config_drift",
            }
        )

    return result


class DriftDetector(BaseAgent):
    spec = AgentSpec(
        domain="drift",
        tier=Tier.REASONER,
        schedule=None,
        max_staleness=None,
        skip_hook=True,
    )

    # Resource types that are EVENTS, not assets. See detect_all() for the
    # measurement that motivated this (91% of a week's drift feed was
    # wazuh_alert churn). Module-local for the same reason as
    # _DURABLE_RESOURCE_TYPES below: two AgentSpec contract changes are in
    # flight in parallel worktrees; promote to a per-collector declaration
    # (e.g. NodeSpec-style `event_shaped=True`) once they land.
    _EVENT_SHAPED_TYPES: frozenset = frozenset({"wazuh_alert"})
    _EVENT_SHAPED_HEAL_CAP = 2000

    def collect(self, scope: str = "all") -> list[dict]:
        return []

    def run(
        self,
        trigger_type: str = "scheduled",
        scope: str = "all",
        sweep_id: uuid.UUID | None = None,
    ) -> "CollectionResult":
        """Override base run() to execute real drift detection methods."""
        from infra_brain.db.models import CollectionRun

        run_id = uuid.uuid4()
        # M-1/SEC-2: DSN-scrubs on append, so no future append site can leak
        # credentials into CollectionResult.errors / CollectionRun.error_message.
        errors: list[str] = ScrubbedErrorList()

        with get_session() as session:
            run = CollectionRun(
                id=run_id,
                domain=self.domain,
                trigger_type=trigger_type,
                trigger_source=scope,
                status="in_progress",
                sweep_id=sweep_id,
            )
            session.add(run)
            session.commit()

        # M-1/TRK-106: the base run()'s finalize-in-a-finally does not apply to
        # an override, so wrap the whole body in the shared guard — a
        # BaseException (KeyboardInterrupt/SystemExit on scheduler shutdown)
        # would otherwise strand this row at status="in_progress" forever.
        with self.run_row_guard(run_id):
            try:
                # AA-R-11/12: honour collection_disabled_domains BEFORE doing any
                # work — every ETLConnector.run()-standard collector respects this
                # maintenance-pause knob (F-022); a run()-override must not
                # silently skip it.
                _disabled = {
                    d.strip()
                    for d in (self.settings.collection_disabled_domains or "").split(",")
                    if d.strip()
                }
                if self.domain in _disabled:
                    raise CollectorSkipped(
                        f"domain '{self.domain}' is in collection_disabled_domains"
                    )

                # F-004.4: run detection under the shared collect timeout guard.
                self._call_with_timeout(self.detect_all, run_id)
                self._call_with_timeout(self.detect_state_drift, run_id)

                self._finalize_run(run_id, status="completed", resources_found=0)

            except CollectorSkipped as exc:
                # Same "skipped" contract as ETLConnector.run() (F-022): distinct
                # from "completed" (ran, found nothing) and "failed" (runtime error).
                reason = str(exc) or "unconfigured"
                # M-1/SEC-2: _finalize_run scrubs the message before it is
                # persisted, so a DSN-bearing exception cannot leak credentials
                # into the dashboard-readable error_message.
                self._finalize_run(run_id, status="skipped", error_message=reason)
                return CollectionResult(
                    run_id=run_id,
                    domain=self.domain,
                    resources_found=0,
                    drift_count=0,
                    status="skipped",
                    errors=errors,
                )
            except Exception as exc:
                # M-1/SEC-2: scrub the returned CollectionResult.errors entry too
                # — it is surfaced by dispatch() and the sweep summary, not just
                # persisted.
                scrubbed = scrub_dsn(str(exc))
                errors.append(scrubbed)
                self._finalize_run(run_id, status="failed", error_message=scrubbed)

            # TRK-026: derive drift_count from the DB on BOTH the success and the
            # failure path — detect_all() commits DriftEvents before
            # detect_state_drift() runs, so a raise in the state phase must not
            # discard the config-drift events already persisted. Mirrors
            # host_reconcile.py / netdiscovery.py's post-run count.
            with get_session() as session:
                drift_count = count_drift_events_for_run(run_id, session)

            return CollectionResult(
                run_id=run_id,
                domain=self.domain,
                resources_found=0,
                drift_count=drift_count,
                status="completed" if not errors else "failed",
                errors=errors,
            )

    def detect_all(self, run_id: uuid.UUID | None = None, scoped: bool = False) -> list[DriftEvent]:
        """Detect config drift for resources touched by snapshots.

        B3 (revised after adversarial review): ``scoped=False`` (the default,
        and the ONLY mode used by anything before this change) queries every
        distinct ``resource_id`` in the ``snapshots`` table — a full-fleet
        scan. This is intentionally still the default because it is the
        system's only full-fleet drift catch-up: ``detect_all()`` used to be
        called exclusively from ``_post_collection_hook`` with no scoping,
        and several ``SKIP_HOOK`` domains (netdiscovery, host_reconcile,
        inventory_reconcile, ...) write snapshots but never trigger the hook
        themselves — their resources previously got caught only by some
        OTHER domain's full-fleet sweep eventually re-scanning everyone.

        ``scoped=True`` (used by ``supervisor.py``'s routine per-collection
        hook call, and ONLY there) narrows the resource-selection query to
        just the resources touched by *run_id* — cheap enough to run after
        every 15-minute pulse. The daily full-fleet catch-up job added in
        scheduler.py calls this with the default ``scoped=False`` so
        ``SKIP_HOOK`` domains' resources keep getting checked.
        """
        events = []
        with get_session() as session:
            # Event-shaped resource types never legitimately DRIFT — an alert
            # is an occurrence, not an asset; a field changing on one is
            # ingestion bookkeeping, not infrastructure change. Measured on
            # the live DB 2026-08-12: 846 of ~931 drift events in the prior 7
            # days (91%) were wazuh_alert churn — the noise floor behind the
            # maintainer's "I don't understand what drifted or why" complaint.
            # Filtered at GENERATION, at the SQL level, so the feed stays
            # honest instead of being hidden per-page in the UI. The resources
            # themselves still snapshot/retire normally — only drift is
            # suppressed.
            # Plain Select, NOT .scalar_subquery(): wrapping a scalar subquery
            # back into select()/IN produced `IN (SELECT (SELECT ...))`, which
            # PostgreSQL evaluates as a per-row scalar expression and kills
            # with CardinalityViolation the moment resources has two matching
            # rows. sqlite tolerated it, so 5,215 green tests never saw it --
            # it failed on the FIRST live run after deploy. The hard MR gates
            # check raw dashboard SQL on real Postgres but not agent ORM
            # queries; this comment is the warning in lieu of that gate.
            event_typed = select(Resource.id).where(
                Resource.type.in_(self._EVENT_SHAPED_TYPES)
            )
            query = (
                session.query(Snapshot.resource_id)
                .filter(Snapshot.resource_id.notin_(event_typed))
                .distinct()
            )
            if scoped:
                if run_id is None:
                    raise ValueError("detect_all(scoped=True) requires a run_id")
                query = query.filter(Snapshot.run_id == run_id)

            # Self-heal the historical pollution every pass (the exact
            # precedent of detect_state_drift's rule 3 for legacy presence
            # rows): still-open drift rows on event-shaped resources are
            # bulk-resolved, bounded per pass so one call can't lock the
            # table for the whole backlog.
            healed = (
                session.query(DriftEvent)
                .filter(
                    DriftEvent.status == "open",
                    DriftEvent.resource_id.in_(event_typed),
                )
                .limit(self._EVENT_SHAPED_HEAL_CAP)
                .all()
            )
            for row in healed:
                row.status = "resolved"
            if healed:
                logger.info(
                    "drift: self-healed %d open drift row(s) on event-shaped resources",
                    len(healed),
                )
            resource_ids = [
                row[0]
                for row in query.all()
                # F-005 run-scoped discovery checkpoints have resource_id=NULL;
                # DriftEvent.resource_id is NOT NULL, so skip them here (same
                # None-guard as the checkpoint writer itself).
                if row[0] is not None
            ]
            events = self._detect_for_resources(session, resource_ids, run_id)
            session.commit()
        return events

    def _detect_for_resources(
        self, session, resource_ids: list[uuid.UUID], run_id: uuid.UUID | None = None
    ) -> list[DriftEvent]:
        """Batched replacement for the old per-resource "last 2 snapshots" N+1
        query: one windowed query across every resource_id in scope, then diff
        in Python. Still scoped to the (already narrowed, when scoped=True)
        resource_ids passed in, so this stays cheap even for a full-fleet pass.
        """
        if not resource_ids:
            return []

        # TRK-239: the "last 2 snapshots per resource" window is computed
        # SERVER-SIDE with row_number(), not by hydrating every snapshot row for
        # every resource and slicing in Python. The previous form
        # (`session.query(Snapshot).filter(Snapshot.resource_id.in_(...))` with
        # no per-resource limit) pulled the whole retained snapshot history —
        # ~1.14M ORM objects on the live fleet, growing ~85k rows/day — which
        # OOM-killed the nightly 01:00 full-fleet catch-up job inside its 2 GiB
        # container (TRK-149) and silently zeroed out drift-event output.
        # Semantics are identical: the two most recent snapshots per resource,
        # most-recent-first, ~2 rows per resource off the wire instead of all.
        rank_col = (
            func.row_number()
            .over(
                partition_by=Snapshot.resource_id,
                order_by=Snapshot.collected_at.desc(),
            )
            .label("rn")
        )
        ranked = select(Snapshot, rank_col).where(Snapshot.resource_id.in_(resource_ids)).subquery()
        ranked_snapshot = aliased(Snapshot, ranked)
        rows = (
            session.query(ranked_snapshot)
            .filter(ranked.c.rn <= 2)
            .order_by(ranked.c.resource_id, ranked.c.collected_at.desc())
            .all()
        )
        by_resource: dict[uuid.UUID, list] = {}
        for row in rows:
            bucket = by_resource.setdefault(row.resource_id, [])
            if len(bucket) < 2:
                bucket.append(row)

        # Idempotency check ("already open for this field") batched into one
        # query per call instead of one query per diff. The (resource_id,
        # field, old_value) triple is read — old_value is needed by the
        # TRK-256 auto-resolve check below, so this hydrates one extra JSON
        # column, not full DriftEvent ORM objects — same row count (bounded
        # by currently-open events), still a fraction of the memory the old
        # per-diff filter_by/first() N+1 query paid.
        open_rows = (
            session.query(DriftEvent.resource_id, DriftEvent.field, DriftEvent.old_value)
            .filter(
                DriftEvent.resource_id.in_(resource_ids),
                DriftEvent.status == "open",
            )
            .all()
        )
        already_open: set[tuple[uuid.UUID, str]] = {(rid, field) for rid, field, _ in open_rows}
        # TRK-256: per-resource list of (field, recorded pre-drift baseline)
        # for every currently-open event, used by the auto-resolve check
        # below. Captured BEFORE this pass mutates `already_open`.
        #
        # Final-branch-review fix (post-TRK-256): the open-events query above
        # filters ONLY on resource_id + status=="open" — it has no way to
        # tell which agent wrote a given DriftEvent row, because DriftEvent
        # has no domain/agent/producer column (checked db/models/core.py; the
        # only per-agent context on the row is the drift_type/field values
        # each writer happens to choose, which are not disambiguating either
        # — e.g. host_reconcile.py also writes drift_type="identity_conflict"
        # rows). Other domain agents (linux.py, windows.py, iac.py,
        # netdiscovery.py, host_reconcile.py) all write their own DriftEvent
        # rows with a DIFFERENT old_value shape than DriftDetector's own
        # ({"v": <value>}): they use old_value=None, or their own dict shapes
        # such as {"state": ...}, {"threat_level": ...}, {"ip": ..., "source":
        # ...}. Without a schema-level identity column, the only reliable
        # signal available without a migration is old_value's shape itself:
        # ONLY a dict with exactly the key "v" is DriftDetector's own
        # signature (verified no other writer uses that key). Any open row
        # whose old_value isn't in that shape is skipped here entirely — it
        # is never added to open_events_by_resource, so it can never be
        # auto-resolved by this pass. This catches every currently known
        # foreign-writer shape (None and the dict shapes above); it would
        # NOT catch a hypothetical future writer that also happened to use
        # {"v": ...} — that residual risk is accepted here in exchange for
        # not requiring a schema migration, but should be revisited (a real
        # producer/domain column) if another agent ever adopts that shape.
        open_events_by_resource: dict[uuid.UUID, list[tuple[str, Any]]] = {}
        for resource_id, field, old_value in open_rows:
            if not (isinstance(old_value, dict) and "v" in old_value and len(old_value) == 1):
                continue
            baseline = old_value["v"]
            open_events_by_resource.setdefault(resource_id, []).append((field, baseline))

        events: list[DriftEvent] = []
        # (resource_id, field) pairs auto-resolved this pass, for the audit row.
        auto_resolved: list[tuple[uuid.UUID, str]] = []
        for resource_id, snapshots in by_resource.items():
            if len(snapshots) < 2:
                continue
            new_snap, old_snap = snapshots[0], snapshots[1]
            diffs = diff_snapshots(old_snap.snapshot, new_snap.snapshot)
            for d in diffs:
                key = (resource_id, d["field"])
                if key in already_open:
                    continue
                event = DriftEvent(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    collection_run_id=run_id,
                    drift_type=d["drift_type"],
                    field=d["field"],
                    old_value={"v": d["old_value"]},
                    new_value={"v": d["new_value"]},
                    detected_at=datetime.now(UTC),
                    status="open",
                )
                session.add(event)
                events.append(event)
                already_open.add(key)  # a diff within THIS batch also counts

            # TRK-256: for each field with a currently-open event on this
            # resource, resolve it iff the ACTUAL value in the newest
            # snapshot matches the event's own recorded pre-drift baseline
            # (`old_value`) — i.e. the field is back in spec. This is
            # deliberately NOT "field absent from this pass's diff": that
            # would be wrong on both sides — a field whose value hasn't
            # moved AGAIN since the last pass (still sitting on the drifted
            # value, never reverted) would also be absent from this pass's
            # diff and get wrongly auto-resolved, while a field that reverts
            # to baseline exactly during this pass DOES show up in the diff
            # and would be wrongly left open. Comparing the newest snapshot's
            # real value against the event's own baseline is correct in both
            # cases and doesn't depend on this pass's diff at all. A snapshot
            # where the field is now absent entirely is treated as matching a
            # `None` baseline (`_get_by_rendered_field` returns `_MISSING`,
            # normalized to `None` below) — that's the same state
            # `diff_snapshots` records for a field that didn't exist before.
            for field, baseline in open_events_by_resource.get(resource_id, []):
                current_value = _get_by_rendered_field(new_snap.snapshot, field)
                if current_value is _MISSING:
                    current_value = None
                if current_value == baseline:
                    auto_resolved.append((resource_id, field))

        # Settings-flag decision (final-branch-review, considered per CLAUDE.md's
        # "behavior-changing automated writes get an off-by-default flag"
        # pattern used for rootcause_llm_enabled / compliance_gap_finder_enabled
        # / remediation_interrupt_enabled): decided AGAINST adding one here.
        # Those three flags gate brand-new, non-deterministic LLM-reasoner
        # behavior that needs a real-model smoke run before trusting it with
        # live data — the flag exists to let that validation happen opt-in.
        # This auto-resolve path is deterministic (no LLM), was already
        # shipped and running by default (TRK-256/GitLab #148), and the bug
        # found here is a scope-correctness bug in existing behavior, not a
        # request to gate brand-new speculative behavior. The fix above
        # (schema-shape scope restriction + hard cap) is what makes the
        # existing default-on behavior safe; adding an off-by-default flag on
        # top would silently regress a feature already relied upon, with no
        # analogous "needs a live smoke test first" justification. If a
        # future incident shows even the scoped/capped version needs a kill
        # switch, add `drift_auto_resolve_enabled: bool = True` (default ON,
        # since this preserves existing behavior rather than introducing new
        # behavior) to config.py at that point.
        # Final-branch-review fix (post-TRK-256): resolve_drift_events (the
        # manual MCP tool this auto-resolve path otherwise mirrors) has a
        # mandatory narrowing predicate, a dry_run=True default, AND a hard
        # per-call row cap (_CLOSURE_BATCH_CAP) — this automated path had none
        # of the three. The scope restriction above is the narrowing
        # predicate; auto-resolve is inherently "preview then apply" in one
        # step by design (it only fires on a verified value match, not a
        # broad filter), so dry_run doesn't apply the same way; but the cap
        # was genuinely missing. Truncate deterministically (oldest-detected
        # resource iteration order, first N pairs) rather than silently
        # resolving an unbounded number of rows in one transaction — any
        # events past the cap simply stay open and get picked up on the next
        # detection pass, same "N bounded calls" pattern as the manual tool.
        if len(auto_resolved) > _AUTO_RESOLVE_BATCH_CAP:
            auto_resolved = auto_resolved[:_AUTO_RESOLVE_BATCH_CAP]

        # Bulk-resolve everything found across all resources in one UPDATE —
        # bounded by the total currently-open events for this batch's
        # resource_ids, same shape as resolve_drift_events' bulk UPDATE
        # (mcp_server.py) which this mirrors.
        if auto_resolved:
            for resource_id in {rid for rid, _ in auto_resolved}:
                fields = [field for rid, field in auto_resolved if rid == resource_id]
                session.query(DriftEvent).filter(
                    DriftEvent.resource_id == resource_id,
                    DriftEvent.field.in_(fields),
                    DriftEvent.status == "open",
                ).update({DriftEvent.status: "resolved"}, synchronize_session=False)

        if auto_resolved:
            # Mirrors resolve_drift_events' audit pattern (mcp_server.py): one
            # AgentActionLog row per batch, written in the SAME transaction as
            # the status flips so a rollback here rolls the resolutions back
            # too — no such thing as an unrecorded auto-resolve batch.
            #
            # Final-branch-review fix (post-TRK-256): `len(auto_resolved)` in
            # the summary text is the real, accurate count of every event
            # resolved this batch (already capped above). The pairs listed
            # after it are a SAMPLE ONLY (first 20, truncated for readability
            # inside a Text column), not the exhaustive list — the wording
            # below says so explicitly so a reader doesn't mistake "20 shown"
            # for "20 total" when the count is higher. AgentActionLog has no
            # JSON/structured field (only `args_summary: Text`, checked
            # governance.py) that could hold the full list instead; nothing
            # downstream parses this row to reconstruct exact resolved IDs
            # (checked: no other module reads `auto_resolve` args_summary),
            # so a truncated-but-labeled text sample is sufficient here.
            sample = ", ".join(f"{rid}:{field}" for rid, field in auto_resolved[:20])
            sample_note = (
                "sample of first 20 shown"
                if len(auto_resolved) > 20
                else "full list shown"
            )
            summary = (
                f"auto-resolved {len(auto_resolved)} drift event(s) "
                f"back in spec on re-detection ({sample_note}): {sample}"
            )
            session.add(
                AgentActionLog(
                    id=uuid.uuid4(),
                    agent=type(self).__name__,
                    domain=self.domain,
                    tool="_detect_for_resources.auto_resolve",
                    args_summary=redact_pans_preserving_uuids(summary)[:2000],
                    verdict="allow",
                    status="ok",
                    ts=datetime.now(UTC),
                )
            )
        return events

    def _detect_for_resource(
        self, session, resource_id: uuid.UUID, run_id: uuid.UUID | None = None
    ) -> list[DriftEvent]:
        """Single-resource entry point — kept for callers/tests that still
        target one resource_id; delegates to the batched implementation."""
        return self._detect_for_resources(session, [resource_id], run_id)

    def detect_state_drift(
        self,
        run_id: uuid.UUID | None = None,
        succeeded_domains: set[str] | None = None,
    ) -> list["Resource"]:
        """Retire resources not seen in their domain's last collection run
        (disappeared assets). Returns the resources newly retired this pass.

        Compares each resource against the most recent completed run for its own domain,
        not a single global last run — prevents cross-domain false positives where a
        completed IAC run would mark all Octopus resources as absent.

        GitLab #137: disappearance is entity-retirement bookkeeping, NOT drift on a
        live resource — it is recorded by stamping ``Resource.retired_at`` (the
        TRK-102 retirement mechanism), never by inserting a ``DriftEvent``. The old
        behavior (a ``state_drift``/``presence`` DriftEvent per disappeared resource,
        with a NULL ``collection_run_id`` because it was never a collection
        observation) flooded the open-drift feed with 60k+ bookkeeping rows and fed a
        runaway loop with ComplianceAgent's stale_drift rule and graph_maintenance's
        violation shadow nodes (the ``stale_drift:stale_drift:*`` explosion). Three
        rules now govern this pass:

        1. A resource that has NEVER been snapshotted is skipped — it was never a
           collection observation (e.g. graph_maintenance's synthetic compliance
           violation-shadow nodes), so "absent from the last run" is meaningless.
        2. A previously-observed resource missing from its domain's last completed
           run gets ``retired_at`` stamped (idempotent — already-retired rows skip).
           A retired resource that reappears in the last run gets ``retired_at``
           cleared.
        3. Any still-open legacy ``state_drift``/``presence`` DriftEvent rows are
           bulk-resolved every pass, so the historical pollution self-heals without
           a data migration.

        ``run_id`` is retained for signature compatibility but unused — retirement
        rows no longer reference a collection run (they never legitimately did).

        Task 3 (partial-sweep drift suppression): when ``succeeded_domains`` is given,
        the disappearance scan is restricted at the SQL level to resources whose domain
        completed THIS sweep (``Resource.domain.in_(succeeded_domains)``) — a domain
        that failed/retry-exhausted this sweep never had its resources re-collected, so
        their absence from the (stale) last-run snapshot is not evidence they vanished.
        ``None`` (all existing callers: the supervisor hook, the daily catch-up job)
        preserves prior unscoped behavior — every domain is scanned. Only this
        disappearance logic is scoped; creation/change drift (detect_all) is untouched.
        """
        del run_id  # GitLab #137: retirement bookkeeping never references a run.
        from infra_brain.db.models import CollectionRun, Resource

        retired: list[Resource] = []
        with get_session() as session:
            # GitLab #137 self-heal: retirement bookkeeping rows written by the
            # pre-fix code (this method was their only writer) are resolved out of
            # the open feed on every pass. Bulk UPDATE — 60k+ rows existed.
            session.query(DriftEvent).filter(
                DriftEvent.drift_type == "state_drift",
                DriftEvent.field == "presence",
                DriftEvent.status == "open",
            ).update({DriftEvent.status: "resolved"}, synchronize_session=False)

            # Build per-domain map: domain → most recent completed CollectionRun
            #
            # M-4: this used to be `.filter_by(status="completed")
            # .order_by(finished_at.desc()).all()` -- EVERY completed
            # CollectionRun ever, across every domain, fully materialized
            # into Python just to keep the single most-recent one per
            # domain. collection_runs grows without bound (every sweep of
            # every domain, forever), so the "latest per domain" selection
            # is pushed into SQL via a ROW_NUMBER() window instead: at most
            # one row per domain ever crosses into Python.
            domain_last_run: dict = {}
            rn = (
                func.row_number()
                .over(
                    partition_by=CollectionRun.domain,
                    order_by=CollectionRun.finished_at.desc(),
                )
                .label("rn")
            )
            ranked_subq = (
                session.query(CollectionRun, rn).filter_by(status="completed").subquery()
            )
            RankedRun = aliased(CollectionRun, ranked_subq)
            completed_runs = (
                session.query(RankedRun).filter(ranked_subq.c.rn == 1).all()
            )
            for run in completed_runs:
                if run.domain not in domain_last_run:
                    domain_last_run[run.domain] = run

            if not domain_last_run:
                session.commit()  # the self-heal above must still land
                return []

            # Set-based presence lookups (replaces the per-resource N+1 queries):
            # (resource_id, run_id) pairs present in any domain's last run, and the
            # set of resource_ids that have EVER been snapshotted (rule 1 above).
            last_run_ids = [r.id for r in domain_last_run.values()]
            in_last_run: set[tuple] = {
                (rid, rrun)
                for rid, rrun in session.query(Snapshot.resource_id, Snapshot.run_id)
                .filter(Snapshot.run_id.in_(last_run_ids))
                .distinct()
            }
            ever_observed: set = {rid for (rid,) in session.query(Snapshot.resource_id).distinct()}

            now = datetime.now(UTC)
            resource_query = session.query(Resource)
            if succeeded_domains is not None:
                resource_query = resource_query.filter(Resource.domain.in_(succeeded_domains))
            for resource in resource_query.all():
                # Use the last run for this resource's own domain; skip if no run yet
                last_run = domain_last_run.get(resource.domain)
                if not last_run:
                    continue

                if (resource.id, last_run.id) in in_last_run:
                    # Present in the latest run — a reappearance clears retirement.
                    if resource.retired_at is not None:
                        resource.retired_at = None
                    continue
                if resource.id not in ever_observed:
                    # Never a collection observation (synthetic bookkeeping node,
                    # e.g. a compliance violation shadow) — cannot "disappear".
                    continue
                if resource.retired_at is not None:
                    continue  # already retired — idempotent
                resource.retired_at = now
                retired.append(resource)
            session.commit()
            # INSIDE the session scope on purpose: the alert reads
            # r.type/r.name/r.domain, and after this block exits those
            # instances are detached — the read would raise
            # DetachedInstanceError, the defensive except below would swallow
            # it, and the alert would silently never fire. Found by this
            # change's own fail-first test before it ever shipped.
            self._alert_durable_retirements(retired)

        return retired

    # Resource types whose DISAPPEARANCE is operationally meaningful. On
    # 2026-08-11 two real machines (media_host, storage_node) dropped off the fleet
    # and this system produced ZERO signal — no drift event, no alert, only a
    # silent ``retired_at`` stamp that both callers of detect_state_drift()
    # discard. That silence is the over-correction of GitLab #137, which
    # rightly killed per-resource presence DriftEvents after a 60k-row
    # bookkeeping flood — but went from "60k noise rows" to "nothing at all".
    #
    # This set is what makes alerting here flood-proof where #137's design was
    # not: (a) ``retired`` only ever contains NEWLY-retired resources (the
    # idempotence guard above), so a dead host alerts exactly once, and
    # (b) only these curated durable types alert — the ephemeral types whose
    # churn caused #137 (wazuh_alert and friends) stay silent forever.
    #
    # Deliberately a module-local set for now, NOT an AgentSpec field: two
    # concurrent contract changes are already in flight on etl/spec.py, and a
    # third would guarantee a conflict for one line of data. Promote it to a
    # per-collector declaration (``durable_resource_types``) once those land.
    _DURABLE_RESOURCE_TYPES: frozenset = frozenset(
        {
            "linux_host",
            "windows_host",
            "net_device",
            "gitlab_project",
        }
    )
    _RETIREMENT_ALERT_CAP = 25

    def _alert_durable_retirements(self, retired: list["Resource"]) -> None:
        """Make the disappearance of a durable entity LOUD (bounded).

        Routed through ``send_ops_alert`` — with a webhook configured it pages;
        unconfigured (TRK-329's current state) it persists to the
        dashboard-queryable AuditLog instead of vanishing into a container log.
        Never raises: an alerting failure must not fail the drift pass that
        detected the disappearance (same rule as tools/ops_webhook's own
        persistence fallback).
        """
        durable = [r for r in retired if r.type in self._DURABLE_RESOURCE_TYPES]
        if not durable:
            return
        try:
            from infra_brain.tools.ops_webhook import send_ops_alert  # noqa: PLC0415

            messages = [
                f"{r.type} '{r.name}' (domain {r.domain}) disappeared from its "
                f"domain's latest collection run and was retired"
                for r in durable[: self._RETIREMENT_ALERT_CAP]
            ]
            if len(durable) > self._RETIREMENT_ALERT_CAP:
                messages.append(
                    f"...and {len(durable) - self._RETIREMENT_ALERT_CAP} more durable "
                    f"resource(s) retired in the same pass ({len(durable)} total)"
                )
            send_ops_alert(
                "entity_retired",
                messages,
                agent_name="DriftAgent",
                dedup_key=f"entity_retired:{','.join(sorted(r.name for r in durable)[:5])}",
            )
        except Exception:  # noqa: BLE001 — see docstring: never fail the pass
            logger.exception("retirement alert failed; retirements themselves are committed")
