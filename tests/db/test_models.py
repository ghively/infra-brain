from datetime import datetime, timezone
import uuid
from sqlalchemy import inspect as sa_inspect
from infra_brain.db.models import (
    Resource,
    DriftEvent,
    ScanPoint,
)


def test_resource_create(db):
    r = Resource(
        id=uuid.uuid4(),
        domain="linux",
        type="host",
        name="web01",
        source="LinuxAgent",
        zone="corpor",
        last_seen=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    assert db.get(Resource, r.id).name == "web01"


def test_drift_event_create(db):
    r = Resource(
        id=uuid.uuid4(),
        domain="cloud",
        type="vm",
        name="test",
        source="CloudAgent",
        zone="corpor",
        last_seen=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    d = DriftEvent(
        id=uuid.uuid4(),
        resource_id=r.id,
        drift_type="config_drift",
        field="instance_type",
        old_value={"v": "t3.small"},
        new_value={"v": "t3.medium"},
        detected_at=datetime.now(timezone.utc),
        status="open",
    )
    db.add(d)
    db.flush()
    assert db.get(DriftEvent, d.id).field == "instance_type"


def test_scan_point_create(db):
    sp = ScanPoint(
        id=uuid.uuid4(),
        domain="linux",
        method="ansible",
        endpoint="all",
        schedule="0 */2 * * *",
        status="active",
    )
    db.add(sp)
    db.flush()
    assert db.get(ScanPoint, sp.id).status == "active"


# --- Task 4: Index existence tests ---


def test_resource_has_domain_index(engine):
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("resources")}
    assert "ix_resources_domain" in indexes


def test_resource_has_domain_type_index(engine):
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("resources")}
    assert "ix_resources_domain_type" in indexes


def test_resource_has_name_index(engine):
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("resources")}
    assert "ix_resources_name" in indexes


def test_collection_runs_has_domain_index(engine):
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("collection_runs")}
    assert "ix_collection_runs_domain" in indexes


def test_collection_runs_has_status_index(engine):
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("collection_runs")}
    assert "ix_collection_runs_status" in indexes


def test_collection_runs_has_started_at_index(engine):
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("collection_runs")}
    assert "ix_collection_runs_started_at" in indexes


def test_drift_event_has_status_index(engine):
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("drift_events")}
    assert "ix_drift_events_status" in indexes


def test_drift_event_has_detected_at_index(engine):
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("drift_events")}
    assert "ix_drift_events_detected_at" in indexes


def test_drift_event_has_resource_id_index(engine):
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("drift_events")}
    assert "ix_drift_events_resource_id" in indexes


def test_audit_log_has_ts_index(engine):
    # Table name is "audit_log" (not "audit_logs")
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("audit_log")}
    assert "ix_audit_log_ts" in indexes


def test_audit_log_has_agent_index(engine):
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("audit_log")}
    assert "ix_audit_log_agent" in indexes


def test_observations_agent_tool_index_removed(engine):
    """MR-F / migration 0027 dropped ix_observations_agent_tool as confirmed
    dead (no query reads the (agent, tool) composite). Guard against re-adding it."""
    inspector = sa_inspect(engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("observations")}
    assert "ix_observations_agent_tool" not in indexes


def test_backup_job_collected_at_refreshes_on_reupsert(db):
    """BackupJob.collected_at must advance when an existing job row is re-polled.

    BackupAgent._upsert_backup_job builds its row dict without a
    ``collected_at`` key, and ETLConnector._upsert_detail only writes columns
    present in that dict — so the column needs ``onupdate=_now`` of its own.
    Without it, collected_at freezes at the row's first-ever insert time and
    any freshness/staleness check reading it reports every previously seen job
    as perpetually stale even while BackupAgent runs successfully.
    """
    from infra_brain.db.models import BackupJob
    from infra_brain.etl.base import ETLConnector

    r = Resource(
        id=uuid.uuid4(),
        domain="backup",
        type="backup_job",
        name="veeam/daily-web01",
        source="BackupAgent",
        zone="corpor",
        last_seen=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()

    first_collected = datetime(2020, 1, 1, tzinfo=timezone.utc)
    db.add(
        BackupJob(
            id=uuid.uuid4(),
            resource_id=r.id,
            backend="veeam",
            job_name="daily-web01",
            last_status="success",
            collected_at=first_collected,
        )
    )
    db.flush()

    # Re-poll: exactly the row dict shape BackupAgent._upsert_backup_job
    # passes — note there is no ``collected_at`` key.
    ETLConnector._upsert_detail(
        None,
        db,
        BackupJob,
        {
            "resource_id": r.id,
            "backend": "veeam",
            "job_name": "daily-web01",
            "last_success_at": datetime.now(timezone.utc),
            "last_restore_test_at": None,
            "retention_days": 30,
            "last_status": "success",
        },
        key_fields=["resource_id", "backend", "job_name"],
    )
    db.flush()

    # Expire so we read what actually landed in the row, not the stale
    # in-session attribute — a later staleness reader sees the DB value.
    db.expire_all()
    row = db.query(BackupJob).filter_by(resource_id=r.id).one()
    collected = row.collected_at
    if collected.tzinfo is None:  # sqlite drops tzinfo on round-trip
        collected = collected.replace(tzinfo=timezone.utc)
    assert collected > first_collected


# --- resources.id overlay-table FK cascade parity ---

# Overlay tables that hang off resources.id and are documented as mirroring the
# same pattern. Every one of them must declare ondelete="CASCADE" so a hard
# delete of a Resource row behaves identically across all of them instead of
# failing with an FK violation on whichever one forgot it.
_RESOURCE_OVERLAY_TABLES = (
    "host_certificates",
    "host_security_posture",
    "host_shares",
    "host_firewall_rules",
    "windows_local_users",
    "windows_local_group_members",
    "windows_logon_events",
    "resource_ownership",
)


def test_resource_ownership_resource_fk_cascades():
    """resource_ownership.resource_id must CASCADE like its posture siblings.

    Without ondelete="CASCADE" Postgres defaults to NO ACTION, so deleting a
    Resource would raise an FK violation on this one overlay table while its
    siblings in host_posture.py cascade cleanly.
    """
    from infra_brain.db.models import ResourceOwnership

    fks = list(ResourceOwnership.__table__.c.resource_id.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "resources"
    assert fk.ondelete == "CASCADE"


def test_all_resource_overlay_fks_cascade():
    """No overlay table in the set may silently drop the CASCADE."""
    from infra_brain.db.models._base import Base

    missing = []
    for table_name in _RESOURCE_OVERLAY_TABLES:
        table = Base.metadata.tables[table_name]
        for fk in table.c.resource_id.foreign_keys:
            if fk.column.table.name == "resources" and fk.ondelete != "CASCADE":
                missing.append(table_name)
    assert missing == []
