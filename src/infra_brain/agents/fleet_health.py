import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, text

from infra_brain.db.models import (
    CollectionRun,
    DriftEvent,
    EolRegistry,
    Resource,
    VulnQueueItem,
)
from infra_brain.db.session import get_session
from infra_brain.db.severity import HIGH_AND_CRITICAL
from infra_brain.db.vuln_status import OPEN_VULN_STATUSES
from infra_brain.etl.base import (
    RUN_STATUS_INTERRUPT_PENDING,
    RUN_STATUS_RETRY_EXHAUSTED,
    CollectOutcome,
    ETLConnector,
)
from infra_brain.etl.spec import AgentSpec, Tier

log = logging.getLogger(__name__)

# TRK-047 (FBS-B1/AA-D-27): the former hand-maintained ``_DOMAIN_MAX_AGE``
# seconds table (a third shadow copy of the cadence truth, already drifted
# for octopus: 24h here vs 26h in freshness.py) is gone. Staleness windows
# are derived per call from each agent's ``spec.max_staleness`` (etl/spec.py)
# — the single source of truth shared with callbacks/freshness.py.


class FleetHealthReporter(ETLConnector):
    spec = AgentSpec(
        domain="fleet_health",
        tier=Tier.REPORTER,
        schedule="25 2 * * *",
        max_staleness=timedelta(hours=26),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        """Compute the fleet-health snapshot and persist it as a Resource.

        F-008: like graph_maintenance/rootcause/vuln_triage/compliance, this
        writes its own summary Resource row directly (via ``upsert_resource``)
        instead of returning the snapshot as a generic ``items=[...]`` dict.
        Returning it as an item would make ``ETLConnector.run()`` write a
        fresh Snapshot every run, and DriftAgent — domain-agnostic, it diffs
        whatever changed between a resource's last two snapshots — would then
        treat this agent's own ever-changing internal counters (open drift
        counts, trend data, timings, ...) as real fleet drift: an
        indefinitely-growing, non-fleet noise source in drift_events that
        poisons remediation input (TRK-268 / GitLab #155). No Snapshot is
        written here, so this resource never feeds DriftAgent's snapshot
        diffing, and ``CollectOutcome(items=[], count_override=1)`` reports a
        truthful resources_found without a generic Resource/Snapshot pair.
        """
        self._errors: list[str] = []
        with get_session() as session:
            data = {
                **self._summary_counts(session),
                **self._trend_data(session),
                **self._per_domain_breakdown(session),
                "top_drifted": self._top_drifted(session),
                "top_vulnerable": self._top_vulnerable(session),
                **self._domain_staleness(session),
                **self._failing_and_running_domains(session),
            }

            from infra_brain.api._seeding import upsert_resource

            upsert_resource(
                session,
                name="fleet-health",
                domain=self.domain,
                resource_type="health_snapshot",
                metadata=data,
                zone=self.settings.default_zone,
                source=type(self).__name__,
            )
            session.commit()

        return CollectOutcome(items=[], count_override=1, errors=self._errors)

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _summary_counts(self, session) -> dict:
        """Core fleet counts: total resources, open drift events, EOL hosts."""
        try:
            total_resources = session.query(Resource).count()
            open_drift_events = (
                session.query(DriftEvent).filter(DriftEvent.status == "open").count()
            )
            eol_hosts = session.query(EolRegistry).count()
            open_vulnerabilities = (
                session.query(VulnQueueItem)
                .filter(VulnQueueItem.status.in_(OPEN_VULN_STATUSES))
                .count()
            )
            return {
                "total_resources": total_resources,
                "open_drift_events": open_drift_events,
                "eol_assets": eol_hosts,
                "open_vulnerabilities": open_vulnerabilities,
            }
        except Exception as exc:
            log.warning("fleet_health._summary_counts failed", exc_info=True)
            self._errors.append(f"_summary_counts failed: {exc}")
            return {}

    def _trend_data(self, session) -> dict:
        """Compare current counts to the previous 7 health_snapshot runs.

        A metric is improving if the latest value < 7-snapshot average.
        A metric is degrading if it is > 10% above the 7-snapshot average.
        Otherwise it is stable.
        """
        try:
            # Pull the 8 most-recent fleet_health snapshots (current + 7 history)
            rows = (
                session.query(CollectionRun)
                .filter(
                    CollectionRun.domain == "fleet_health",
                    CollectionRun.status == "completed",
                )
                .order_by(CollectionRun.started_at.desc())
                .limit(8)
                .all()
            )

            if len(rows) < 2:
                return {
                    "drift_trend": "unknown",
                    "vuln_trend": "unknown",
                    "eol_trend": "unknown",
                }

            # NOTE: drift is NOT trended off fleet_health's own CollectionRun
            # rows (``rows`` above — kept only for the length gate). Fleet-
            # HealthReporter is a pure reporter — its own collect() never
            # creates DriftEvent rows tied to its own run_id
            # (count_drift_events_for_run(run_id, ...) counts DriftEvent rows
            # by collection_run_id), so CollectionRun.drift_count is always 0
            # on every fleet_health run. Trending that would make drift_trend
            # hardcoded to "stable" regardless of real fleet drift. Instead,
            # mirror the vuln_trend/eol_trend pattern below: a live current
            # count plus a history proxy from the domain that actually
            # produces the metric (the "drift" DriftDetector agent).

            def _trend(current_val: int, history_vals: list[int]) -> str:
                if not history_vals:
                    return "unknown"
                avg = sum(history_vals) / len(history_vals)
                if avg == 0:
                    return "stable" if current_val == 0 else "degrading"
                if current_val < avg:
                    return "improving"
                if current_val > avg * 1.10:
                    return "degrading"
                return "stable"

            drift_current = (
                session.query(DriftEvent).filter(DriftEvent.status == "open").count()
            )
            drift_history_runs = (
                session.query(CollectionRun)
                .filter(
                    CollectionRun.domain == "drift",
                    CollectionRun.status == "completed",
                )
                .order_by(CollectionRun.started_at.desc())
                .limit(7)
                .all()
            )
            drift_vals = [r.drift_count for r in drift_history_runs]
            vuln_current = (
                session.query(VulnQueueItem)
                .filter(VulnQueueItem.status.in_(OPEN_VULN_STATUSES))
                .count()
            )
            # Approximate historical vuln counts from resources_found on vuln runs
            vuln_history_runs = (
                session.query(CollectionRun)
                .filter(
                    CollectionRun.domain == "vuln",
                    CollectionRun.status == "completed",
                )
                .order_by(CollectionRun.started_at.desc())
                .limit(7)
                .all()
            )
            vuln_vals = [r.resources_found for r in vuln_history_runs]

            eol_current = session.query(EolRegistry).count()
            eol_history_runs = (
                session.query(CollectionRun)
                .filter(
                    CollectionRun.domain == "eol",
                    CollectionRun.status == "completed",
                )
                .order_by(CollectionRun.started_at.desc())
                .limit(7)
                .all()
            )
            eol_vals = [r.resources_found for r in eol_history_runs]

            return {
                "drift_trend": _trend(drift_current, drift_vals),
                "vuln_trend": _trend(vuln_current, vuln_vals),
                "eol_trend": _trend(eol_current, eol_vals),
            }
        except Exception as exc:
            log.warning("fleet_health._trend_data failed", exc_info=True)
            self._errors.append(f"_trend_data failed: {exc}")
            return {
                "drift_trend": "unknown",
                "vuln_trend": "unknown",
                "eol_trend": "unknown",
            }

    def _per_domain_breakdown(self, session) -> dict:
        """Open drift events and critical/high vulns (VulnQueueItem) per domain."""
        try:
            # Open drift events per domain via join to resources
            drift_rows = (
                session.query(Resource.domain, func.count(DriftEvent.id).label("cnt"))
                .join(DriftEvent, DriftEvent.resource_id == Resource.id)
                .filter(DriftEvent.status == "open")
                .group_by(Resource.domain)
                .all()
            )

            # Critical/High vulns per domain via vuln_queue → resources
            vuln_rows = (
                session.query(Resource.domain, func.count(VulnQueueItem.id).label("cnt"))
                .join(VulnQueueItem, VulnQueueItem.resource_id == Resource.id)
                .filter(
                    VulnQueueItem.severity.in_(HIGH_AND_CRITICAL),
                    VulnQueueItem.status.in_(OPEN_VULN_STATUSES),
                )
                .group_by(Resource.domain)
                .all()
            )

            domains: dict[str, dict] = {}
            for dom, cnt in drift_rows:
                domains.setdefault(dom, {"drift": 0, "vulns": 0})["drift"] = cnt
            for dom, cnt in vuln_rows:
                domains.setdefault(dom, {"drift": 0, "vulns": 0})["vulns"] = cnt

            return {"domains": domains}
        except Exception as exc:
            log.warning("fleet_health._per_domain_breakdown failed", exc_info=True)
            self._errors.append(f"_per_domain_breakdown failed: {exc}")
            return {"domains": {}}

    def _top_drifted(self, session) -> list:
        """Top 5 resources by open drift event count."""
        try:
            rows = (
                session.query(
                    DriftEvent.resource_id,
                    Resource.name.label("hostname"),
                    func.count(DriftEvent.id).label("drift_count"),
                )
                .join(Resource, Resource.id == DriftEvent.resource_id)
                .filter(DriftEvent.status == "open")
                .group_by(DriftEvent.resource_id, Resource.name)
                .order_by(text("drift_count DESC"))
                .limit(5)
                .all()
            )
            return [
                {
                    "resource_id": str(r.resource_id),
                    "hostname": r.hostname,
                    "open_drift_count": r.drift_count,
                }
                for r in rows
            ]
        except Exception as exc:
            log.warning("fleet_health._top_drifted failed", exc_info=True)
            self._errors.append(f"_top_drifted failed: {exc}")
            return []

    def _top_vulnerable(self, session) -> list:
        """Top 5 resources by open Critical/High vuln count (VulnQueueItem)."""
        try:
            rows = (
                session.query(
                    VulnQueueItem.resource_id,
                    Resource.name.label("hostname"),
                    func.count(VulnQueueItem.id).label("vuln_count"),
                )
                .join(Resource, Resource.id == VulnQueueItem.resource_id)
                .filter(
                    VulnQueueItem.severity.in_(HIGH_AND_CRITICAL),
                    VulnQueueItem.status.in_(OPEN_VULN_STATUSES),
                )
                .group_by(VulnQueueItem.resource_id, Resource.name)
                .order_by(text("vuln_count DESC"))
                .limit(5)
                .all()
            )
            return [
                {
                    "resource_id": str(r.resource_id),
                    "hostname": r.hostname,
                    "critical_high_vuln_count": r.vuln_count,
                }
                for r in rows
            ]
        except Exception as exc:
            log.warning("fleet_health._top_vulnerable failed", exc_info=True)
            self._errors.append(f"_top_vulnerable failed: {exc}")
            return []

    def _domain_staleness(self, session) -> dict:
        """Flag domains whose last successful collection is overdue."""
        try:
            from infra_brain.etl.spec import max_staleness_by_domain

            max_age_by_domain = {
                domain: int(age.total_seconds())
                for domain, age in max_staleness_by_domain().items()
            }
            rows = (
                session.query(
                    CollectionRun.domain,
                    func.max(CollectionRun.finished_at).label("last_success"),
                )
                .filter(CollectionRun.status == "completed")
                .group_by(CollectionRun.domain)
                .all()
            )

            now = datetime.now(UTC)
            stale_domains: list[str] = []
            domain_freshness: dict[str, dict] = {}

            for dom, last_success in rows:
                max_age = max_age_by_domain.get(dom)
                if max_age is None:
                    continue  # domain not in freshness spec — skip
                if last_success is None:
                    stale_domains.append(dom)
                    domain_freshness[dom] = {"last_success": None, "stale": True}
                    continue

                # Ensure timezone-aware comparison
                if last_success.tzinfo is None:
                    last_success = last_success.replace(tzinfo=UTC)

                age_seconds = (now - last_success).total_seconds()
                is_stale = age_seconds > max_age
                if is_stale:
                    stale_domains.append(dom)
                domain_freshness[dom] = {
                    "last_success": last_success.isoformat(),
                    "age_seconds": int(age_seconds),
                    "max_age_seconds": max_age,
                    "stale": is_stale,
                }

            return {
                "stale_domains": stale_domains,
                "domain_freshness": domain_freshness,
            }
        except Exception as exc:
            log.warning("fleet_health._domain_staleness failed", exc_info=True)
            self._errors.append(f"_domain_staleness failed: {exc}")
            return {"stale_domains": [], "domain_freshness": {}}

    def _failing_and_running_domains(self, session) -> dict:
        """Classify each monitored domain's most recent run (Phase 2 sweep
        graph run-state constants, etl/base.py):

        * ``RUN_STATUS_RETRY_EXHAUSTED`` is failed-equivalent — alongside the
          plain ``"failed"`` status — the collector's RetryPolicy exhausted
          every attempt and the wrapper swallowed the final exception rather
          than crashing the sweep (spec §2.6).
        * ``RUN_STATUS_INTERRUPT_PENDING`` is in-progress-equivalent —
          alongside plain ``"in_progress"`` — a sweep paused awaiting
          human-in-the-loop approval (Phase 3 groundwork) is not a broken
          collector, it just hasn't finished yet.
        """
        try:
            from infra_brain.etl.spec import max_staleness_by_domain

            failed_domains: list[str] = []
            in_progress_domains: list[str] = []
            for domain in max_staleness_by_domain():
                latest = (
                    session.query(CollectionRun)
                    .filter(CollectionRun.domain == domain)
                    .order_by(CollectionRun.started_at.desc())
                    .first()
                )
                if latest is None:
                    continue
                if latest.status in ("failed", RUN_STATUS_RETRY_EXHAUSTED):
                    failed_domains.append(domain)
                elif latest.status in ("in_progress", RUN_STATUS_INTERRUPT_PENDING):
                    in_progress_domains.append(domain)
            return {
                "failed_domains": sorted(failed_domains),
                "in_progress_domains": sorted(in_progress_domains),
            }
        except Exception as exc:
            log.warning("fleet_health._failing_and_running_domains failed", exc_info=True)
            self._errors.append(f"_failing_and_running_domains failed: {exc}")
            return {"failed_domains": [], "in_progress_domains": []}
