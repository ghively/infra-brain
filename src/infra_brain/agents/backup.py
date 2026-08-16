"""BackupAgent — backup verification / freshness poller (GitLab issue #96).

Polls a configured backup system's API (Veeam / Bacula / cloud snapshot —
selected via ``backup_backend``, see tools/backup.py) for each protected
resource's last-successful-backup timestamp and last DR/chaos restore-test
date, and flags jobs with no recent successful backup or an overdue restore
test — the same freshness/staleness SHAPE already built for collection
health (callbacks/freshness.py), applied here to backup jobs rather than
infra-brain's own collection runs. "Overdue" is a point-in-time derived
fact recomputed by collect() each run (not a stored boolean column) —
config.py's ``backup_overdue_days`` / ``backup_restore_test_overdue_days``
are the thresholds.

No live Veeam/Bacula/cloud-snapshot target is configured in this dev
environment. Mirrors eol.py/net.py/vuln.py's degrade-gracefully convention:
with ``backup_backend``/``backup_api_url`` unset, ``collect()`` raises
``CollectorSkipped`` and the run is recorded status="skipped" — distinct
from a working-empty "completed" run.

Chaos/DR-test tracking folds into the same ``backup_jobs`` table
(``last_restore_test_at``) rather than a separate agent/table, per the
issue #96 scope decision — a restore test is a property of a backup job,
not an independent entity.
"""

import logging
from datetime import UTC, datetime, timedelta

from infra_brain.agents.base import BaseAgent
from infra_brain.db.models import BackupJob
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectorSkipped, CollectOutcome
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.backup import backup_jobs_tool

logger = logging.getLogger(__name__)


def _parse_dt(value) -> datetime | None:
    """Best-effort ISO-8601 parse; returns None for missing/unparseable values.

    Normalizes a naive parsed result to UTC (mirrors eol.py's _parse_eol) so
    every comparison against ``datetime.now(UTC)`` in collect() is safe.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class BackupAgent(BaseAgent):
    """Read-only backup-verification collector (Veeam / Bacula / cloud snapshot)."""

    spec = AgentSpec(
        domain="backup",
        tier=Tier.COLLECTOR,
        schedule="50 2 * * *",
        max_staleness=timedelta(hours=26),
    )

    def collect(self, scope: str = "all") -> "list[dict] | CollectOutcome":
        if not self.settings.backup_backend or not self.settings.backup_api_url:
            raise CollectorSkipped(
                "no backup backend/API configured (backup_backend/backup_api_url)"
            )

        _cb = {"callbacks": self.callbacks}
        try:
            jobs = backup_jobs_tool.invoke(
                {
                    "base_url": self.settings.backup_api_url,
                    "backend": self.settings.backup_backend,
                    "ssl_verify": self.settings.backup_ssl_verify,
                },
                config=_cb,
            )
        except Exception as exc:
            msg = f"backup API poll failed: {exc}"
            logger.warning("BackupAgent: %s", msg)
            self._last_jobs = []
            return CollectOutcome(items=[], errors=[msg])

        jobs = jobs or []
        now = datetime.now(UTC)
        overdue_window = timedelta(days=self.settings.backup_overdue_days)
        restore_test_window = timedelta(days=self.settings.backup_restore_test_overdue_days)

        items: list[dict] = []
        for job in jobs:
            job_name = (job.get("job_name") or "").strip()
            resource_name = (job.get("resource_name") or "").strip() or job_name
            if not job_name or not resource_name:
                logger.info("BackupAgent: skipping job with no job_name/resource_name: %r", job)
                continue

            last_success = _parse_dt(job.get("last_success_at"))
            last_restore_test = _parse_dt(job.get("last_restore_test_at"))
            backup_overdue = last_success is None or (now - last_success) > overdue_window
            restore_test_overdue = (
                last_restore_test is None or (now - last_restore_test) > restore_test_window
            )

            items.append(
                {
                    "name": f"{resource_name}:{job_name}",
                    "type": "backup_job",
                    "data": {
                        "backend": self.settings.backup_backend,
                        "job_name": job_name,
                        "resource_name": resource_name,
                        "last_success_at": job.get("last_success_at"),
                        "last_restore_test_at": job.get("last_restore_test_at"),
                        "retention_days": job.get("retention_days"),
                        "status": job.get("status"),
                        "backup_overdue": backup_overdue,
                        "restore_test_overdue": restore_test_overdue,
                    },
                }
            )

        self._last_jobs = items
        overdue_count = sum(1 for i in items if i["data"]["backup_overdue"])
        restore_overdue_count = sum(1 for i in items if i["data"]["restore_test_overdue"])
        logger.info(
            "BackupAgent: collected %d backup job(s) — %d overdue backup, %d overdue restore test",
            len(items),
            overdue_count,
            restore_overdue_count,
        )
        return CollectOutcome(items=items)

    # --- relational detail-write phase --------------------------------

    def _detail_writers(self, scope, result):
        # Surface a detail-write failure on the CollectionRun (no silent
        # data loss) via ETLConnector.run()'s _write_details.
        return [self._write_backup_details]

    def _write_backup_details(self) -> int:
        """Populate backup_jobs from the items collect() emitted.

        Idle-safety first: with no items (unconfigured, or a poll that
        legitimately found nothing) there is nothing to write.
        """
        items = getattr(self, "_last_jobs", None) or []
        if not items:
            return 0

        written = 0
        with get_session() as session:
            for item in items:
                try:
                    with session.begin_nested():
                        self._upsert_backup_job(session, item)
                    written += 1
                except Exception as exc:
                    logger.warning(
                        "BackupAgent: skipping bad backup_job item %r: %s",
                        item.get("name"),
                        exc,
                    )
            session.commit()
        logger.info("BackupAgent: wrote %d backup_jobs row(s)", written)
        return written

    def _upsert_backup_job(self, session, item: dict) -> None:
        data = item.get("data", {}) or {}
        job_name = data.get("job_name")
        backend = data.get("backend")
        if not job_name or not backend:
            raise ValueError(f"missing job_name/backend on {item.get('name')!r}")

        # The generic Resource/Snapshot upsert (ETLConnector.run()'s own
        # collect-phase loop) already created/updated the domain="backup",
        # type="backup_job" Resource for this item BEFORE detail writers
        # run — resolve it via the shared _resource_id helper rather than
        # re-implementing the upsert (k8s/octopus/vsphere pattern).
        res_id = self._resource_id(session, "backup_job", item["name"])
        if res_id is None:
            raise ValueError(f"no resource_id resolved for {item['name']!r}")

        row = {
            "resource_id": res_id,
            "backend": backend,
            "job_name": job_name,
            "last_success_at": _parse_dt(data.get("last_success_at")),
            "last_restore_test_at": _parse_dt(data.get("last_restore_test_at")),
            "retention_days": data.get("retention_days"),
            "last_status": data.get("status"),
            # Bumped on every poll (not just the first insert) — mirrors
            # pki.py's `existing.collected_at = now` convention — so this
            # column tracks "last confirmed by a poll", not just "first seen".
            "collected_at": datetime.now(UTC),
        }
        self._upsert_detail(session, BackupJob, row, key_fields=["resource_id", "backend", "job_name"])
