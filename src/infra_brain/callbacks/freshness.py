import logging
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone, timedelta
from infra_brain.db.models import AgentConfigSetting, CollectionRun
from infra_brain.db.session import get_session
from infra_brain.etl.base import RUN_STATUS_INTERRUPT_PENDING, RUN_STATUS_RETRY_EXHAUSTED

log = logging.getLogger(__name__)

# OB-1 scheduler dead-man switch: key in the generic AgentConfigSetting
# key-value store (no migration needed — it already exists for dashboard-set
# agent config). record_scheduler_heartbeat() is called from the hourly
# _collection_health_job in scheduler.py; check_scheduler_deadman() is called
# from an INDEPENDENT execution path (an asyncio task in main.py's FastAPI
# lifespan, not an APScheduler job) so a fully-stopped scheduler can still be
# detected and alerted on instead of needing to self-report.
_SCHEDULER_HEARTBEAT_KEY = "scheduler_heartbeat_last_run"

# Freshness windows are cron cadence + slack. Phase 1 Task 5 (TRK-047): this
# used to be a hand-maintained literal dict (one of FOUR shadow cadence
# tables); it is now DERIVED from each agent's ``spec.max_staleness``
# (etl/spec.py) — the single source of truth. The module-level name is kept
# for callers; it is a lazy read-only Mapping so importing this module does
# not force-resolve every agent module in the lazy AGENT_REGISTRY (only the
# first freshness check does, and those run in processes that need the whole
# roster anyway).


class _SpecDerivedMaxAge(Mapping):
    """Lazy domain -> max_staleness view over the AgentSpec registry."""

    def __init__(self) -> None:
        self._cache: dict[str, timedelta] | None = None

    def _data(self) -> dict[str, timedelta]:
        if self._cache is None:
            from infra_brain.etl.spec import max_staleness_by_domain  # noqa: PLC0415

            self._cache = max_staleness_by_domain()
        return self._cache

    def __getitem__(self, domain: str) -> timedelta:
        return self._data()[domain]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())


DOMAIN_EXPECTED_MAX_AGE: Mapping[str, timedelta] = _SpecDerivedMaxAge()

# Domains where a zero-row completed run is a NORMAL outcome (analysis and
# propose-only agents) — excluded from the "silently empty" alert only.
ZERO_OK_DOMAINS = {
    "discovery",
    "coverage",
    "compliance",
    "vuln_triage",
    "rootcause",
    "drift_learning",
    "remediation",
    "learning_feedback",
    "query",
    "netdiscovery",
    "graph_maintenance",
    "inventory_reconcile",
    # Tier.REASONER, and by its own module docstring "purely a regression over
    # numbers already in Postgres" — it emits forecasts only where there is
    # enough history to regress over, so a zero-row run on a young or sparse
    # dataset is the correct outcome, not a fault. Its omission from this set
    # meant the only run it has ever had was flagged "silently empty?" and
    # escalated 168x in a row.
    "capacity_forecast",
}


_STREAK_KEY_PREFIX = "collection_health_streak_"

# Wording marker for the "intentionally unconfigured collector" alert.
# check_collection_health() already classifies these as informational (they set
# skip_only, are excluded from the unhealthy streak, and never escalate), but it
# returns a FLAT list of strings, so that classification was invisible to callers
# — scheduler.py logged every entry at ERROR and shipped it to the ops webhook.
# With 11 unconfigured collectors on this fleet that buried the one genuinely
# failing domain under ~11x its own volume of "vsphere not configured" noise
# every single health pass. Centralised here so callers classify via
# is_informational_alert() instead of re-deriving the wording.
UNCONFIGURED_ALERT_MARKER = "unconfigured (skipped)"


def is_informational_alert(alert: str) -> bool:
    """True when *alert* only reports an intentionally-unconfigured collector.

    Such an alert is a statement of configuration, not a fault: nothing is
    broken and no action clears it short of configuring the integration.
    """
    return UNCONFIGURED_ALERT_MARKER in alert


def _get_streak(session, domain: str) -> int:
    """Read the current consecutive-unhealthy-pass streak for *domain*.

    Persisted via the existing generic AgentConfigSetting key-value store
    (same mechanism as the OB-1 scheduler heartbeat) so the streak survives
    scheduler/process restarts without a new migration.
    """
    row = session.query(AgentConfigSetting).filter_by(key=_STREAK_KEY_PREFIX + domain).first()
    if row is None or not row.value:
        return 0
    try:
        return int(row.value)
    except ValueError:
        return 0


def _set_streak(session, domain: str, value: int) -> None:
    """Persist *value* for *domain*'s streak, deleting the row once it's 0
    (a healthy/reset domain has no lingering streak row to accumulate)."""
    key = _STREAK_KEY_PREFIX + domain
    row = session.query(AgentConfigSetting).filter_by(key=key).first()
    if value <= 0:
        if row is not None:
            session.delete(row)
        return
    if row is not None:
        row.value = str(value)
    else:
        session.add(AgentConfigSetting(key=key, value=str(value)))


def _most_recent_skipped(session, domain: str) -> CollectionRun | None:
    """Return *domain*'s latest run when it is a "skipped" one, else None.

    The lookup is deliberately anchored to the domain's most recent run rather
    than to the most recent *skipped* run. A collector that was once
    unconfigured keeps that old ``skipped`` row forever, so an unanchored
    lookup kept reporting "intentionally unconfigured" long after the collector
    had been configured and started failing for real — the caller treats a
    skip as non-alarming, so every later failure was suppressed.

    Observed on wazuh: last ``skipped`` run 2026-08-02, then 262 consecutive
    401 auth failures through 2026-08-07, every one of them reported as a
    benign "unconfigured (skipped)" and excluded from the stale list.

    Anchoring here restores the meaning both callers (and check_freshness's
    own docstring) already assume: the collector is *currently* skipped.
    """
    latest = (
        session.query(CollectionRun)
        .filter(CollectionRun.domain == domain)
        .order_by(CollectionRun.started_at.desc())
        .first()
    )
    if latest is None or latest.status != "skipped":
        return None
    return latest


def check_freshness() -> list[str]:
    """Returns list of stale domain names.

    Domains whose most-recent run is "skipped" (intentionally unconfigured
    collectors) are excluded from the stale list — they are logged at INFO
    level with the skip reason so operators can see them without a false alarm.
    """
    stale = []
    now = datetime.now(timezone.utc)
    with get_session() as session:
        for domain, max_age in DOMAIN_EXPECTED_MAX_AGE.items():
            last_run = (
                session.query(CollectionRun)
                .filter(
                    CollectionRun.domain == domain,
                    CollectionRun.status.in_(["completed", "partial"]),
                )
                .order_by(CollectionRun.finished_at.desc())
                .first()
            )
            # Checked BEFORE staleness, and regardless of whether a successful
            # run exists: a collector that is skipping right now is switched
            # off, and something switched off cannot be "stale". Anchoring this
            # only to the never-succeeded case meant a domain with older
            # `completed` rows kept ageing into a stale alert forever after its
            # dependency was removed — unclearable without configuring an
            # integration nobody asked for.
            skipped = _most_recent_skipped(session, domain)
            if skipped is not None:
                reason = skipped.error_message or "no reason recorded"
                log.info(
                    "[freshness] %s is unconfigured (skipped) — %s",
                    domain,
                    reason,
                )
                continue  # not stale — intentionally skipped
            if not last_run or not last_run.finished_at:
                stale.append(domain)
                log.warning("[freshness] %s has never completed a collection run", domain)
                continue
            finished_at = last_run.finished_at
            # SQLite (used in tests) returns naive datetimes even for
            # DateTime(timezone=True) columns; Postgres (production) always
            # returns tz-aware. Normalize so the subtraction below is safe
            # under both backends (same guard as check_collection_health).
            if finished_at.tzinfo is None:
                finished_at = finished_at.replace(tzinfo=timezone.utc)
            if now - finished_at > max_age:
                stale.append(domain)
                log.warning(
                    "[freshness] %s last collected %s ago (max %s)",
                    domain,
                    now - finished_at,
                    max_age,
                )
    return stale


def check_collection_health() -> list[str]:
    """Completeness monitor (R3): alert strings for unhealthy domains.

    Conditions per monitored domain (DOMAIN_EXPECTED_MAX_AGE):
      1. never started;
      2. latest run failed/partial (carries error_message);
      3. stale — no completed/partial run inside the max-age window;
      4. silently empty — last success wrote zero rows (non-ZERO_OK domains).
    Returns [] when everything is healthy.

    TRK-241 (GitLab #110): each pass also updates a per-domain consecutive-
    unhealthy-pass streak (persisted in AgentConfigSetting, keyed
    ``collection_health_streak_<domain>``). Once a domain's streak reaches
    ``settings.collection_health_escalation_streak``, its alert message(s)
    are prefixed with an "ESCALATED" marker carrying the streak count — a
    stronger signal than a single-pass alert, since the same condition has
    now re-fired hourly N times in a row without anyone/anything clearing it.
    A domain whose "unconfigured (skipped)" informational message is its
    only alert is NOT treated as unhealthy for streak purposes (matches the
    existing non-alarming treatment of intentionally-skipped collectors
    elsewhere in this function). This is alert-only: escalating severity
    does not trigger any retry, remediation, or other automated action.
    """
    from infra_brain.config import get_settings  # noqa: PLC0415

    escalation_streak = get_settings().collection_health_escalation_streak
    alerts: list[str] = []
    now = datetime.now(timezone.utc)
    with get_session() as session:
        for domain, max_age in DOMAIN_EXPECTED_MAX_AGE.items():
            domain_alerts: list[str] = []
            latest = (
                session.query(CollectionRun)
                .filter(CollectionRun.domain == domain)
                .order_by(CollectionRun.started_at.desc())
                .first()
            )
            skip_only = False
            if latest is None:
                domain_alerts.append(f"{domain}: no collection run has ever started")
            else:
                if latest.status in ("failed", "partial", RUN_STATUS_RETRY_EXHAUSTED):
                    # RUN_STATUS_RETRY_EXHAUSTED (Phase 2 sweep graph): the
                    # collector wrapper's RetryPolicy exhausted all attempts and
                    # swallowed the final exception rather than crashing the
                    # sweep — failed-equivalent for alerting purposes.
                    domain_alerts.append(
                        f"{domain}: last run {latest.status}"
                        f" ({(latest.error_message or 'no error_message')[:200]})"
                    )
                elif latest.status == RUN_STATUS_INTERRUPT_PENDING:
                    # Phase 3 (groundwork only): a sweep paused awaiting
                    # human-in-the-loop approval is in-progress-equivalent, not
                    # failed/stuck — same treatment as a plain "in_progress" run
                    # (no alert here; staleness is still checked below via the
                    # last completed/partial run).
                    pass
                last_ok = (
                    session.query(CollectionRun)
                    .filter(
                        CollectionRun.domain == domain,
                        CollectionRun.status.in_(["completed", "partial"]),
                        CollectionRun.finished_at.isnot(None),
                    )
                    .order_by(CollectionRun.finished_at.desc())
                    .first()
                )
                currently_skipped = _most_recent_skipped(session, domain)
                if currently_skipped is not None:
                    # The collector is skipping RIGHT NOW — its dependency is
                    # unconfigured, so it is deliberately not running. Checked
                    # before staleness, because a domain that is switched off
                    # cannot meaningfully be "stale": its last successful run
                    # recedes forever and the alert can never be cleared by any
                    # action except configuring an integration nobody asked for.
                    #
                    # This surfaced the moment saas_inventory started skipping
                    # correctly instead of returning an empty success — it still
                    # had older `completed` rows, so the staleness branch below
                    # kept firing and the "silently empty?" noise simply became
                    # "stale" noise.
                    reason = currently_skipped.error_message or "no reason recorded"
                    domain_alerts.append(f"{domain}: {UNCONFIGURED_ALERT_MARKER} — {reason}")
                    skip_only = len(domain_alerts) == 1
                elif last_ok is None:
                    # Genuinely broken: never completed, and not currently
                    # skipping either (the branch above already claimed that
                    # case, so re-checking _most_recent_skipped here would be
                    # dead code).
                    domain_alerts.append(f"{domain}: has never completed a collection run")
                else:
                    finished_at = last_ok.finished_at
                    # SQLite (used in tests) returns naive datetimes even for
                    # DateTime(timezone=True) columns; Postgres (production)
                    # always returns tz-aware. Normalize so the subtraction
                    # below is safe under both backends.
                    if finished_at.tzinfo is None:
                        finished_at = finished_at.replace(tzinfo=timezone.utc)
                    if now - finished_at > max_age:
                        domain_alerts.append(
                            f"{domain}: stale — last data {now - finished_at} ago (max {max_age})"
                        )
                    elif (
                        last_ok.status == "completed"
                        and (last_ok.resources_found or 0) == 0
                        and (last_ok.detail_rows_written or 0) == 0
                        and domain not in ZERO_OK_DOMAINS
                    ):
                        domain_alerts.append(
                            f"{domain}: last completed run wrote zero rows (silently empty?)"
                        )

            # TRK-241: an "unconfigured (skipped)" domain with no other alert
            # is informational, not unhealthy — matches the pre-existing
            # treatment elsewhere in this function. Everything else with at
            # least one alert this pass counts toward the streak.
            unhealthy = bool(domain_alerts) and not skip_only
            streak = _get_streak(session, domain)
            if unhealthy:
                streak += 1
            else:
                streak = 0
            _set_streak(session, domain, streak)

            if unhealthy and streak >= escalation_streak:
                domain_alerts = [
                    f"ESCALATED ({streak}x in a row) — {msg}" for msg in domain_alerts
                ]

            alerts.extend(domain_alerts)
        session.commit()
    return alerts


def record_scheduler_heartbeat() -> None:
    """Record "the scheduler is alive and ran its jobs" (OB-1).

    Called from the hourly _collection_health_job in scheduler.py. Uses the
    existing generic AgentConfigSetting key-value store — no migration
    needed. Best-effort: a failure here must not abort the health job.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_session() as session:
        existing = session.query(AgentConfigSetting).filter_by(key=_SCHEDULER_HEARTBEAT_KEY).first()
        if existing is not None:
            existing.value = now_iso
        else:
            session.add(AgentConfigSetting(key=_SCHEDULER_HEARTBEAT_KEY, value=now_iso))
        session.commit()


def check_scheduler_deadman(max_age: timedelta = timedelta(hours=2)) -> str | None:
    """Return an alert string if the scheduler's heartbeat is missing or stale.

    A fully-stopped scheduler cannot itself run this check (it is the thing
    that stopped) — this is meant to be called from an INDEPENDENT execution
    path (see main.py's lifespan asyncio task), not from an APScheduler job.
    Returns None when the heartbeat is fresh.
    """
    with get_session() as session:
        row = session.query(AgentConfigSetting).filter_by(key=_SCHEDULER_HEARTBEAT_KEY).first()

    if row is None or not row.value:
        return "scheduler: no heartbeat has ever been recorded — scheduler may never have started"

    try:
        last = datetime.fromisoformat(row.value)
    except ValueError:
        return f"scheduler: heartbeat value is unparseable ({row.value!r})"

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    age = now - last
    if age > max_age:
        return f"scheduler: heartbeat stale — last seen {age} ago (max {max_age})"
    return None


def get_scheduler_heartbeat_status(
    max_age: timedelta = timedelta(hours=2),
) -> tuple[float | None, float, bool]:
    """Heartbeat accessor for GET /api/ops/heartbeat (Phase 4 OB-1 dead-man prober).

    Returns ``(heartbeat_age_seconds, max_age_seconds, stale)``. The heartbeat
    is only written HOURLY (record_scheduler_heartbeat runs from the hourly
    _collection_health_job), so ``max_age`` must carry slack beyond one hour —
    the default here matches check_scheduler_deadman's 2-hour window (one
    missed cycle of slack) rather than alerting on mere minutes of staleness.

    heartbeat_age_seconds is None when no heartbeat has ever been recorded;
    that state is reported as stale=True.
    """
    with get_session() as session:
        row = session.query(AgentConfigSetting).filter_by(key=_SCHEDULER_HEARTBEAT_KEY).first()

    max_age_seconds = max_age.total_seconds()

    if row is None or not row.value:
        return None, max_age_seconds, True

    try:
        last = datetime.fromisoformat(row.value)
    except ValueError:
        return None, max_age_seconds, True

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    age = datetime.now(timezone.utc) - last
    age_seconds = age.total_seconds()
    return age_seconds, max_age_seconds, age_seconds > max_age_seconds


def sum_llm_tokens_today() -> int:
    """Best-effort sum of ``AgentDecisionLog.token_count`` for the current UTC
    day — a lightweight LLM cost-visibility aggregate (TRK-120).

    Deliberately minimal: it returns a single number the hourly collection-health
    job logs. There is intentionally NO alerting/notification pipeline attached
    (that would be redundant with the separate LiteLLM-proxy evaluation's likely
    outcome). Returns 0 on any error, or when nothing has been logged today.
    """
    from sqlalchemy import func

    from infra_brain.db.models import AgentDecisionLog

    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        with get_session() as session:
            total = (
                session.query(func.coalesce(func.sum(AgentDecisionLog.token_count), 0))
                .filter(AgentDecisionLog.ts >= day_start)
                .scalar()
            )
        return int(total or 0)
    except Exception:
        log.debug("sum_llm_tokens_today failed", exc_info=True)
        return 0
