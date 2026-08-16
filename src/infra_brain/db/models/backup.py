"""BackupJob — backup verification / freshness tracking (GitLab issue #96).

Polls backup-system APIs (Veeam, Bacula, cloud snapshot APIs) for the
last-successful-backup timestamp per protected resource, plus the last DR/
chaos-test restore date and configured retention window. One row per
(resource, backend, job_name) — a host/VM can have more than one backup job
(e.g. a daily VM-level snapshot job AND a separate DB-dump job for the same
resource), so the natural key is (resource_id, backend, job_name), not
resource_id alone.

Chaos/DR-test tracking folds into this table as ``last_restore_test_at``
rather than a separate table/agent, per the issue #96 scope decision — a
restore test is a property of a backup job, not an independent entity.

Design notes (mirrors host_posture.py's conventions):
- Keys off ``resource_id`` (FK to ``resources.id``, CASCADE on delete), like
  every table in the host_posture bucket.
- ``backend`` disambiguates which backup system produced the row ("veeam" |
  "bacula" | "cloud_snapshot" | ...) since more than one could in principle
  be configured against the same resource.
- Freshness/overdue is NOT precomputed into a boolean column here — like
  callbacks/freshness.py's staleness checks, "overdue" is a point-in-time
  derived fact computed by the reader (BackupAgent's own collect() emits it
  into the Snapshot data for the run that found it; the dashboard/chat tool
  layer can recompute it live against ``last_success_at`` /
  ``last_restore_test_at`` at query time), not baked into a stale stored
  boolean that itself needs re-collection to update.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, _now, _uuid


class BackupJob(Base):
    """One row per (resource, backend, job) tracked by a backup-system API."""

    __tablename__ = "backup_jobs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Which backup system produced this row: "veeam" | "bacula" |
    # "cloud_snapshot" | ... — config-driven, see BackupAgent/tools/backup.py.
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    # The job/policy name as reported by the backend (e.g. Veeam job name,
    # Bacula job name, cloud snapshot policy id).
    job_name: Mapped[str] = mapped_column(String(256), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Chaos/DR-test tracking (issue #96 scope: folded into this table, not a
    # separate table/agent) — most recent successful restore/DR test for
    # this job, if any has ever been recorded. NULL means "never tested".
    last_restore_test_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Last-observed run status as reported by the backend, for operator
    # context ("success" | "warning" | "failed" | backend-specific string).
    # Informational only — overdue/staleness logic reads last_success_at,
    # not this field.
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # ``onupdate=_now`` (not just ``default=_now``) so a re-poll of an
    # already-known job refreshes this. BackupAgent's ``_upsert_backup_job``
    # does not put ``collected_at`` in its row dict, and ``_upsert_detail``
    # only writes the columns present there — without ``onupdate`` this would
    # freeze at the row's first-ever insert time and make every previously
    # seen job look perpetually stale. Matches container_registry's
    # ContainerImage.last_seen / secrets_inventory's SecretRecord.updated_at.
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (
        UniqueConstraint("resource_id", "backend", "job_name", name="uq_backup_job_natkey"),
    )


__all__ = ["BackupJob"]
