"""VulnTriageAgent — prioritize the CVE queue and propose patches (human-gated).

Rapid7 dumps raw CVEs; this ranks them deterministically by severity × SLA
pressure × PCI exposure (EolRegistry.pci_risk_score), marks the worst ones
``triage``, and drafts a ``ProposedAction`` (``vuln_patch``) for each so a human
can approve patching. No external writes — DB only.
"""

from __future__ import annotations

import logging
from datetime import timedelta
import uuid

from infra_brain.agents.base import BaseAgent, CollectOutcome
from infra_brain.db.models import (
    EolRegistry,
    ProposedAction,
    Resource,
    Snapshot,
    VulnQueueItem,
)
from infra_brain.db.session import get_session
from infra_brain.triage import compute_vuln_priority, is_high_priority
from infra_brain.etl.base import ReconcileScope
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)

# TRK-057: this agent used to compute its own ad hoc `base_confidence * weight`
# confidence score (severity-only base, plus a decay-by-age multiplier) —
# a formula that disagreed with `compute_vuln_priority()`, the one actually
# persisted and shown by the dashboard (api/routers/vuln.py). Confidence is
# now derived from that single canonical priority score so the agent and the
# UI always agree on a CVE's rank. `_CONFIDENCE_PRIORITY_CEILING` is the
# priority score (see `triage._SEVERITY_WEIGHT` + overdue/PCI bonuses) that
# maps to confidence=0.99; scores at or above it saturate.
_CONFIDENCE_PRIORITY_CEILING = 150.0


class VulnTriageAgent(BaseAgent):
    spec = AgentSpec(
        domain="vuln_triage",
        tier=Tier.REASONER,
        schedule="0 6 * * *",
        max_staleness=timedelta(hours=26),
        skip_hook=True,
        # DELIBERATELY NOT retired (2026-08-12), unlike the `vuln` collector it
        # was originally built to follow. This agent reasons over the
        # `vuln_queue` table, not over Rapid7 — and VulnAgent is not that
        # table's only writer: `mcp_server.seed_vulnerability` writes
        # VulnQueueItem rows too, so an operator can seed a CVE by hand and
        # this agent will triage it. Retiring it would switch off a working
        # reasoner because its most-used upstream happens to be gone, which is
        # the "looks enterprise but runs on local data" trap. It is also in
        # freshness.ZERO_OK_DOMAINS, so an empty run raises no alert and costs
        # nothing while the queue is empty.
    )

    def collect(self, scope: str = "all") -> list[dict]:
        proposed = 0
        # M-2 (F-007): the per-item SAVEPOINT below logs+skips a bad
        # VulnQueueItem (TRK-132) so its siblings still commit — but that
        # resilience must not make a run that dropped every item look
        # identical to one that dropped nothing. Route the failures through
        # CollectOutcome.errors so the existing status contract (ok/partial/
        # failed) reflects them, instead of a run silently reporting "ok".
        skip_scope = ReconcileScope(label="vuln_queue_item")
        with get_session() as session:
            pci_by_resource = self._pci_map(session)
            rows = (
                session.query(VulnQueueItem, Resource)
                .outerjoin(Resource, Resource.id == VulnQueueItem.resource_id)
                .filter(VulnQueueItem.status.in_(("open", "triage")))
                .all()
            )
            for v, r in rows:
                if not is_high_priority(v.severity, v.sla_due):
                    continue
                # TRK-132: one bad VulnQueueItem/Resource pair must not abort the
                # whole run's writes (InFailedSqlTransaction cascade on Postgres).
                # Per-item SAVEPOINT — same precedent as net.py::_write_net_details
                # and k8s.py::_write_k8s_details — logs+skips the bad row instead
                # of losing every other item's ProposedAction/status write.
                key = getattr(v, "cve_id", None) or "unknown"
                try:
                    with session.begin_nested():
                        if self._triage_one(session, v, r, pci_by_resource):
                            proposed += 1
                    skip_scope.observed(key)
                except Exception as exc:
                    logger.warning(
                        "VulnTriageAgent: skipping bad vuln queue item %r: %s",
                        getattr(v, "cve_id", None),
                        exc,
                    )
                    skip_scope.failed(key, exc)

            # TRK-064/FBS-B5: the triage summary used to only be logged — the
            # F-008 fix removed a broken write to a nonexistent CollectionRun
            # metadata column, but never replaced it with a real persistence
            # path, so the dashboard had no way to read it back. Snapshot is
            # the existing run-scoped detail-row mechanism (see
            # `etl/base.py::_write_snapshot` and the "discovery partial-progress
            # checkpoints" case in the Snapshot model docstring: resource_id is
            # nullable by design for exactly this kind of run-scoped, no-owning-
            # resource row) — no new table or column needed. `_active_run_id`
            # is set by `ETLConnector.run()` before `collect()` runs; direct
            # `collect()` calls (as in the unit tests) skip the write.
            run_id = getattr(self, "_active_run_id", None)
            if run_id is not None:
                session.add(
                    Snapshot(
                        id=uuid.uuid4(),
                        resource_id=None,
                        run_id=run_id,
                        snapshot={
                            "type": "triage_summary",
                            "newly_proposed": proposed,
                            "total_scored": len(rows),
                        },
                    )
                )
            session.commit()
        if proposed:
            logger.info("VulnTriageAgent proposed %d patch action(s)", proposed)

        logger.info(
            "VulnTriageAgent triage_summary: newly_proposed=%d total_scored=%d%s",
            proposed,
            len(rows),
            f" ({skip_scope.failed_count} item(s) skipped)" if skip_scope.has_failures else "",
        )
        return CollectOutcome(items=[], errors=skip_scope.errors, count_override=proposed)

    @staticmethod
    def _triage_one(session, v: VulnQueueItem, r: Resource | None, pci_by_resource: dict) -> bool:
        """Score+propose a single VulnQueueItem/Resource pair.

        Runs inside the caller's per-item SAVEPOINT (see `collect()`) so any
        exception raised here — a flush-time constraint violation, a malformed
        row, etc. — rolls back only this item's writes (including the
        ``v.status = "triage"`` flip) rather than the whole batch.

        Returns True iff a new ProposedAction was proposed (for the caller's
        running ``proposed`` counter).
        """
        host = r.name if r else "unknown"
        if v.status == "open":
            v.status = "triage"
        target = f"cve:{v.cve_id}:{host}"
        exists = (
            session.query(ProposedAction)
            .filter(
                ProposedAction.target == target,
                ProposedAction.status.in_(("pending", "approved", "executed")),
            )
            .first()
        )
        if exists:
            return False
        pci_risk = pci_by_resource.get(v.resource_id, 0)
        priority = compute_vuln_priority(v.severity, v.sla_due, pci_risk)
        confidence = round(min(0.99, priority / _CONFIDENCE_PRIORITY_CEILING), 3)
        session.add(
            ProposedAction(
                agent="VulnTriageAgent",
                action_type="vuln_patch",
                target=target,
                payload={
                    "cve": v.cve_id,
                    "host": host,
                    "severity": v.severity,
                    "pci_risk": pci_risk,
                    "guidance": f"Patch {v.cve_id} on {host} ({v.severity}).",
                    "priority": priority,
                },
                confidence=confidence,
                status="pending",
            )
        )
        return True

    @staticmethod
    def _pci_map(session) -> dict:
        """resource_id → max pci_risk_score from the EOL registry."""
        out: dict = {}
        for rid, risk in session.query(EolRegistry.resource_id, EolRegistry.pci_risk_score).all():
            if risk is not None and risk > out.get(rid, 0):
                out[rid] = risk
        return out
