"""Shared Redis-outage CollectionRun recorder (#89).

Both scheduler.py's _make_job and graph.py's sweep collector node skip a
domain entirely on dedup.LockBackendUnavailable — logged loudly (F-029) but
historically with no CollectionRun row written, so
callbacks.freshness.check_collection_health()'s hourly alert batch could
only catch the outage once the domain's own staleness window eventually
elapsed (up to a day+ for daily/weekly-cadence domains), even though the log
line itself was immediate. record_redis_outage_run() closes that gap: it
writes a CollectionRun(status="failed") row immediately, which
check_collection_health() catches on its very next hourly pass via its
existing "latest run failed/partial" condition — no new alerting mechanism,
just making the existing one able to see this failure mode at all.
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from datetime import datetime, timezone

from infra_brain.db.models import CollectionRun
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

_OUTAGE_MESSAGE = "redis lock backend unavailable"


def record_redis_outage_run(domain: str, sweep_id: str | None = None) -> None:
    """Best-effort: write one failed CollectionRun row for this Redis-outage
    skip. Never raises — this already runs on the degraded Redis-down path;
    a DB blip while recording it must not compound the failure. The hourly
    stale-run reaper and the domain's next successful run remain the
    backstops if even this write fails."""
    try:
        now = datetime.now(timezone.utc)
        with get_session() as session:
            session.add(
                CollectionRun(
                    id=_uuid_mod.uuid4(),
                    domain=domain,
                    trigger_type="scheduled",
                    trigger_source="redis_outage",
                    status="failed",
                    error_message=_OUTAGE_MESSAGE,
                    started_at=now,
                    finished_at=now,
                    sweep_id=_uuid_mod.UUID(sweep_id) if sweep_id else None,
                )
            )
            session.commit()
    except Exception:
        logger.exception(
            "redis_outage: failed to record outage CollectionRun for domain=%s",
            domain,
            extra={"domain": domain},
        )
