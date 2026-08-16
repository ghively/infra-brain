"""Live, runtime-flippable collector pause levers (``dispatchable__<domain>``).

TRK-303 part 5/7 introduced a ``dispatchable__<domain>`` ``RuntimeConfig`` row
as a pause lever for an agent that stays registered in ``AGENT_REGISTRY`` —
distinct from ``AgentSpec.dispatchable=False``, which removes a domain from the
registry entirely (see ``supervisor.py``'s ``_AGENT_SPECS`` docstring).

That lookup originally lived inside ``supervisor.py`` as a private helper, which
made the toggle a **no-op whenever ``sweep_graph_enabled`` is on** — the sweep
graph (``graph.py``) fans collectors out itself and never calls
``supervisor.dispatch()``. This module is the shared home so every sweep path
(``dispatch()``, the sweep graph's collector planning, the MCP roster, and the
dashboard agent-config endpoint) reads the SAME override, rather than the query
being duplicated or — worse — only wired into one of them.

Everything here is fail-open and never raises: an unmigrated table, an
unreachable DB, or a malformed row all degrade to "no override" (or, for the
bulk read, an empty mapping). A toggle lookup failing must never itself block a
scheduled collection.
"""

import logging
import uuid

from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

# Prefix for the dynamic per-domain RuntimeConfig pause keys. Deliberately NOT
# a Settings field — config.py's resolver knows about this prefix by literal
# (see its ``_NON_SETTINGS_KEY_PREFIXES``) rather than importing it from here,
# because config.py is imported by db/session.py and an import back the other
# way would be circular. Keep the two in sync if this value ever changes.
DISPATCHABLE_KEY_PREFIX = "dispatchable__"

# Status recorded on the synthetic CollectionRun row written when a domain is
# paused. Reuses the existing "skipped" status (etl/base.py already writes it
# for CollectorSkipped) rather than inventing a new one — downstream status
# consumers (api/_helpers.py's _RUN_STATUS, the dashboard) already tolerate it.
PAUSED_RUN_STATUS = "skipped"


def _truthy(raw: str) -> bool:
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


def dispatchable_override(domain: str) -> bool | None:
    """Look up the live ``dispatchable__<domain>`` RuntimeConfig override.

    Returns True/False when a row exists and has a value, else None so the
    caller falls through to its existing registry/settings-based behavior
    unchanged.

    Deliberately queries ``RuntimeConfig`` directly rather than through
    ``get_settings()`` — that resolver only surfaces overrides for known
    ``Settings`` fields (see config.py's ``_load_runtime_overrides``), and
    ``dispatchable__<domain>`` is a dynamic per-domain key, not a Settings
    field.
    """
    try:
        from infra_brain.db.models import RuntimeConfig

        with get_session() as session:
            row = session.get(RuntimeConfig, f"{DISPATCHABLE_KEY_PREFIX}{domain}")
    except Exception:
        logger.debug(
            "%s%s override lookup failed — falling through to the registry default",
            DISPATCHABLE_KEY_PREFIX,
            domain,
            exc_info=True,
        )
        return None
    if row is None or row.value is None:
        return None
    return _truthy(row.value)


def paused_domains() -> set[str]:
    """Every domain whose ``dispatchable__<domain>`` override resolves False.

    One query for the whole fleet — used by the collector-planning and roster
    paths that would otherwise issue a per-domain ``dispatchable_override()``
    call each. Fail-open: an empty set on any error, i.e. nothing is paused.
    """
    try:
        from infra_brain.db.models import RuntimeConfig

        with get_session() as session:
            rows = (
                session.query(RuntimeConfig.key, RuntimeConfig.value)
                .filter(RuntimeConfig.key.like(f"{DISPATCHABLE_KEY_PREFIX}%"))
                .all()
            )
    except Exception:
        logger.debug(
            "%s* bulk override lookup failed — treating no domain as paused",
            DISPATCHABLE_KEY_PREFIX,
            exc_info=True,
        )
        return set()
    return {
        key[len(DISPATCHABLE_KEY_PREFIX) :]
        for key, value in rows
        if value is not None and not _truthy(value)
    }


def record_paused_run(
    domain: str,
    trigger_type: str = "scheduled",
    sweep_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Persist a minimal ``collection_runs`` row for a paused (skipped) domain.

    Without this the pause is invisible downstream: ``get_agent_roster()`` and
    the dashboard both read ``last_run`` from real ``collection_runs`` rows, so
    a paused domain's entry would simply freeze at its last real run and read as
    dormant/unwired — exactly the false signal TRK-271's ``hook_driven`` field
    was added to eliminate.

    Returns the new run's id, or None if the write failed (fail-open: a
    bookkeeping write must never turn a benign skip into an exception on the
    dispatch path).
    """
    try:
        from datetime import UTC, datetime

        from infra_brain.db.models import CollectionRun

        now = datetime.now(UTC)
        run_id = uuid.uuid4()
        with get_session() as session:
            session.add(
                CollectionRun(
                    id=run_id,
                    domain=domain,
                    trigger_type=trigger_type,
                    trigger_source=f"paused:{DISPATCHABLE_KEY_PREFIX}{domain}",
                    started_at=now,
                    finished_at=now,
                    resources_found=0,
                    detail_rows_written=0,
                    drift_count=0,
                    status=PAUSED_RUN_STATUS,
                    sweep_id=sweep_id,
                )
            )
            session.commit()
        return run_id
    except Exception:
        logger.warning(
            "failed to record the paused-collection run row for domain=%r", domain, exc_info=True
        )
        return None
