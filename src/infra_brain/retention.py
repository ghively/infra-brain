"""Retention pruning for high-volume append-only tables (F-030).

Windows are Settings-driven (RETENTION_* env vars, PCI-safe defaults) and
documented in docs/RETENTION-POLICY.md. Runs daily at 04:30 UTC via the
scheduler (job id ``infra_brain_retention_prune``).

Prune rules:
- audit_log / agent_action_log: hard age cutoff (>= 400 days, PCI DSS 10.7).
- snapshots / resource_configs: age cutoff on collected_at.
- drift_events: age cutoff on detected_at, RESOLVED rows only (status != open);
  open drift is never deleted regardless of age.
- collection_runs: age cutoff on started_at; child snapshots/resource_configs
  of expired runs are deleted first and surviving drift_events are detached
  (collection_run_id -> NULL) so FKs never block the delete.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, delete, inspect, select, text, update

from infra_brain.config import get_settings
from infra_brain.db.models import (
    AgentActionLog,
    AuditLog,
    CollectionRun,
    DriftEvent,
    ResourceConfig,
    Snapshot,
)
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

# LangGraph-owned tables (langgraph-checkpoint-postgres 3.1.0) — never
# ORM-mapped. Verified schema (see
# .venv/Lib/site-packages/langgraph/checkpoint/postgres/base.py MIGRATIONS):
#   checkpoints(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
#     type, checkpoint JSONB, metadata JSONB) — PK (thread_id, checkpoint_ns,
#     checkpoint_id). NO created_at column; the checkpoint's own timestamp is
#     the JSONB field checkpoint->>'ts' (ISO 8601 string).
#   checkpoint_blobs(thread_id, checkpoint_ns, channel, version, type,
#     blob BYTEA) — PK (thread_id, checkpoint_ns, channel, version).
#   checkpoint_writes(thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
#     channel, type, blob BYTEA, task_path) — PK (thread_id, checkpoint_ns,
#     checkpoint_id, task_id, idx).
_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _delete_older_than(session, model, ts_column, cutoff, *extra) -> int:
    """DELETE rows of *model* where *ts_column* < *cutoff* (+extra criteria)."""
    stmt = (
        delete(model).where(ts_column < cutoff, *extra).execution_options(synchronize_session=False)
    )
    return session.execute(stmt).rowcount or 0


def prune_checkpoints(session) -> int:
    """Delete stale, unprotected LangGraph checkpoint rows (TRK-069).

    A thread_id is eligible for deletion only when BOTH hold:
    - its newest checkpoint's ``checkpoint->>'ts'`` is older than
      ``settings.retention_checkpoints_days``; and
    - it is not referenced by any ``ProposedAction`` with
      ``status IN ('pending', 'approved')`` (the cheap, indexed protection
      query enabled by Task 1's ``proposed_actions.thread_id`` column — never
      scans checkpoint blobs for interrupt state).

    Guard: the checkpoint tables may not exist at all (fresh env, or a
    deployment that only ever used MemorySaver) — skip cleanly and return 0.
    Returns the number of deleted ``checkpoints`` rows.
    """
    bind = session.get_bind()
    existing_tables = set(inspect(bind).get_table_names())
    if not set(_CHECKPOINT_TABLES).issubset(existing_tables):
        logger.info("[retention] checkpoint tables absent — skipping checkpoint prune")
        return 0

    settings = get_settings()
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(days=settings.retention_checkpoints_days)
    ).isoformat()

    stale_thread_ids = (
        session.execute(
            text(
                """
                SELECT thread_id
                FROM checkpoints
                GROUP BY thread_id
                HAVING MAX(checkpoint ->> 'ts') < :cutoff
                """
            ),
            {"cutoff": cutoff_iso},
        )
        .scalars()
        .all()
    )
    if not stale_thread_ids:
        return 0

    protected_thread_ids = set(
        session.execute(
            text(
                """
                SELECT DISTINCT thread_id
                FROM proposed_actions
                WHERE status IN ('pending', 'approved') AND thread_id IS NOT NULL
                """
            )
        )
        .scalars()
        .all()
    )

    to_delete = [t for t in stale_thread_ids if t not in protected_thread_ids]
    if not to_delete:
        return 0

    deleted_checkpoints = 0
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        stmt = text(f"DELETE FROM {table} WHERE thread_id IN :thread_ids").bindparams(
            bindparam("thread_ids", expanding=True)
        )
        result = session.execute(stmt, {"thread_ids": to_delete})
        if table == "checkpoints":
            deleted_checkpoints = result.rowcount or 0
    return deleted_checkpoints


def prune_expired() -> dict[str, int]:
    """Apply every retention window once. Returns {table: deleted_count}."""
    settings = get_settings()
    if not settings.retention_prune_enabled:
        logger.info("[retention] pruning disabled (RETENTION_PRUNE_ENABLED=false)")
        return {}
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    with get_session() as session:
        counts["audit_log"] = _delete_older_than(
            session,
            AuditLog,
            AuditLog.ts,
            now - timedelta(days=settings.retention_audit_log_days),
        )
        counts["agent_action_log"] = _delete_older_than(
            session,
            AgentActionLog,
            AgentActionLog.ts,
            now - timedelta(days=settings.retention_agent_action_log_days),
        )
        snap_cutoff = now - timedelta(days=settings.retention_snapshots_days)
        counts["snapshots"] = _delete_older_than(
            session, Snapshot, Snapshot.collected_at, snap_cutoff
        )
        counts["resource_configs"] = _delete_older_than(
            session, ResourceConfig, ResourceConfig.collected_at, snap_cutoff
        )
        counts["drift_events"] = _delete_older_than(
            session,
            DriftEvent,
            DriftEvent.detected_at,
            now - timedelta(days=settings.retention_drift_events_days),
            DriftEvent.status != "open",
        )
        run_cutoff = now - timedelta(days=settings.retention_collection_runs_days)
        old_run_ids = select(CollectionRun.id).where(CollectionRun.started_at < run_cutoff)
        session.execute(
            update(DriftEvent)
            .where(DriftEvent.collection_run_id.in_(old_run_ids))
            .values(collection_run_id=None)
            .execution_options(synchronize_session=False)
        )
        session.execute(
            delete(Snapshot)
            .where(Snapshot.run_id.in_(old_run_ids))
            .execution_options(synchronize_session=False)
        )
        session.execute(
            delete(ResourceConfig)
            .where(ResourceConfig.run_id.in_(old_run_ids))
            .execution_options(synchronize_session=False)
        )
        counts["collection_runs"] = _delete_older_than(
            session, CollectionRun, CollectionRun.started_at, run_cutoff
        )
        counts["checkpoints"] = prune_checkpoints(session)
        session.commit()
    logger.info("[retention] prune complete: %s", counts)
    return counts
