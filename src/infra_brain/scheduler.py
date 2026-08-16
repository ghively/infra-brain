import logging
import threading
from datetime import UTC, date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from infra_brain.config import get_settings
from infra_brain.dedup import LockBackendUnavailable, release, try_acquire
from infra_brain.etl.spec import retired_domains
from infra_brain.retention import prune_expired
from infra_brain.supervisor import AGENT_REGISTRY, dispatch

log = logging.getLogger(__name__)

# Weekly-or-less-frequent jobs (a specific day_of_week, e.g. drift_learning's
# "0 4 * * 0") get a much longer misfire grace window than the default 300s
# (5 min). Rationale: a missed weekly slot costs a full week's wait for the
# next one, with no catch-up mechanism (unlike the Redis-outage case, which
# _redis_outage_catchup_job below retries same-day) -- confirmed live that
# drift_learning/learning_feedback both silently missed their first-ever
# slot, most plausibly because the scheduler container was mid-recreate at
# the exact trigger instant and didn't come back within 300s. A frequent job
# (every-N-minutes/hourly/daily) doesn't need this: its next slot is at most
# a day away, so the default grace window is fine and a long one would just
# risk an unexpectedly-late run confusing "last run" freshness checks.
_WEEKLY_MISFIRE_GRACE_SECONDS = 6 * 3600  # 6h -- comfortably covers a stuck/recreating container
_DEFAULT_MISFIRE_GRACE_SECONDS = 300


def _misfire_grace_time(day_of_week: str) -> int:
    return (
        _WEEKLY_MISFIRE_GRACE_SECONDS
        if day_of_week != "*"
        else _DEFAULT_MISFIRE_GRACE_SECONDS
    )

# TRK-231 / #109: (domain, scope) -> UTC date it was first skipped this day
# because Redis was unreachable to acquire the dedup lock (LockBackendUnavailable
# — NOT the ordinary "lock already held" skip, which just means another run of
# the same domain is legitimately in progress and needs no catch-up).
# _redis_outage_catchup_job() below re-attempts each entry once Redis is
# healthy again, later the same day, instead of waiting for the domain's next
# regular cron slot (which can be up to a day away for daily/weekly-cadence
# domains). In-memory only — lost on process restart, which is acceptable for
# a best-effort self-healing retry (the domain's normal cron is the ultimate
# backstop). Guarded by _redis_outage_lock since BackgroundScheduler runs jobs
# on a thread pool and two domains' jobs could hit the Redis outage concurrently.
_redis_outage_pending: dict[tuple[str, str], date] = {}
_redis_outage_lock = threading.Lock()

# B6 (Finding 5): default cron schedules are now built from ONE iteration over
# AGENT_REGISTRY, reading each agent class's declarative ``schedule`` class
# attribute (etl/base.py's ETLConnector — the single source of truth), instead
# of being a hand-maintained dict kept in sync with supervisor.py's registry
# by hand. A domain with ``schedule = None`` (drift, notification, inventory_mr
# — hook-only/no-op agents; query — see the scoped add_job_scoped("query",
# "health", ...) call in start() below, since QueryAgent's collect() only does
# real work when scope=="health" and dispatching it through this dict would
# send the default scope="all", a no-op / AA-C-3/AA-D-23 dead dispatch slot)
# is simply absent here, same as before.
#
# Building this dict resolves every agent class in AGENT_REGISTRY (the
# scheduler process legitimately needs the full schedule list at startup —
# unlike supervisor.py's dispatch(), which only needs ONE domain's class per
# call and stays lazy).
_DEFAULT_SCHEDULES: dict[str, str] = {
    domain: cls.schedule
    for domain, cls in AGENT_REGISTRY.items()
    if getattr(cls, "schedule", None) is not None
}

# #75: mirrors the literal cron strings registered directly in
# SchedulerService.start() (bypassing _DEFAULT_SCHEDULES entirely) so both
# collision guards (tests/scheduler/test_scheduler.py::
# test_no_exact_schedule_collisions and .claude/scripts/dev_status.py::
# check_schedule_collisions) can see them too. Keep every value here in
# lockstep with its add_job/add_job_scoped call in start() below.
_ADHOC_SCHEDULES: dict[str, str] = {
    # Only actually registered when sweep_graph_enabled=True (default off,
    # CLAUDE.md constraint #10) — included here as the configured default so
    # the guard sees it too, on the same best-effort basis _DEFAULT_SCHEDULES
    # already applies to every other domain's schedule.
    "sweep_graph": "0 */4 * * *",
    # Registered only while vsphere is NOT retired (AgentSpec.retired) — which
    # it currently is, so this job does not exist at runtime today. Kept here,
    # like the retired domains' own cron strings in _DEFAULT_SCHEDULES, as the
    # cadence it resumes on if vsphere is revived; both collision guards skip
    # retired domains so it costs nothing while off.
    "vsphere:pulse": "5,20,35,50 * * * *",
    "query:health": "30 2 * * 0",
    "collection_health": "20 * * * *",
    "retention_prune": "30 4 * * *",
    "drift_full_fleet_catchup": "0 1 * * *",
    "redis_outage_catchup": "40 * * * *",
    "webhook_retry": "10 * * * *",
    # P6.1/P6.4: headless-runner standing tasks — only actually registered
    # when headless_runner_mcp_token is configured (default empty = off,
    # same "empty = clean no-op" convention as every other optional
    # integration in this repo).
    "agent_task:drift-triage": "0 2 * * *",
    "agent_task:self-review": "5 3 * * 0",
    "agent_task:eol-report": "5 4 * * 1",
}


def _make_job(domain: str, scope: str = "all"):
    """Return a callable that runs the scheduled collection for *domain* at *scope*."""

    def _job():
        token = None
        try:
            token = try_acquire(domain, scope)
            if not token:
                log.info(
                    "[scheduler] Skipping %s/%s — lock already held",
                    domain,
                    scope,
                    extra={"domain": domain},
                )
                return
            dispatch(domain, trigger_type="scheduled", scope=scope)
        except LockBackendUnavailable:
            # F-029: Redis-down must be loud and unmistakable — every scheduled
            # sweep is being skipped until Redis returns.
            log.error(
                "[scheduler] Redis unavailable — %s/%s sweep NOT run this cycle",
                domain,
                scope,
                exc_info=True,
                extra={"domain": domain},
            )
            from infra_brain.redis_outage import record_redis_outage_run

            record_redis_outage_run(domain)
            # #109: remember this skip so the hourly catch-up job can retry it
            # later today once Redis is healthy again, rather than leaving the
            # domain to silently wait for its next regular cron slot.
            with _redis_outage_lock:
                _redis_outage_pending[(domain, scope)] = datetime.now(UTC).date()
        except Exception:
            log.warning(
                "[scheduler] Job for domain %s scope %s raised an exception",
                domain,
                scope,
                exc_info=True,
                extra={"domain": domain},
            )
        finally:
            if token:
                release(domain, scope, token=token)

    _job.__name__ = f"_job_{domain}_{scope}"
    return _job


def _deliver_ops_alerts(category: str, alerts: list[str]) -> None:
    """Best-effort delivery of *alerts* through NotificationAgent (OB-1).

    Every alert passed here is already logged by the caller before this runs
    (actionable ones at ERROR, informational ones at INFO), so a delivery
    failure here is never the only signal an alert happened — it just means
    the webhook side-channel didn't also fire. Callers are expected to filter
    informational entries out first (see is_informational_alert); this webhook
    is for things a human should act on.
    """
    if not alerts:
        return
    try:
        from infra_brain.agents.notification import NotificationAgent

        NotificationAgent().notify_ops_alerts(category, alerts)
    except Exception:
        log.exception("[%s] failed to deliver ops-webhook alert", category)


def _webhook_retry_job() -> None:
    """Retry due outbound webhook subscription deliveries (issue #112).

    Best-effort: a failure here means one or more subscribers get their
    retry attempt(s) delayed to the next hourly slot rather than losing the
    delivery outright — WebhookDelivery rows persist in the DB regardless
    of whether this job itself raises.
    """
    try:
        from infra_brain.tools.webhook_publish import retry_pending_deliveries

        result = retry_pending_deliveries()
        if result["attempted"]:
            log.info("[webhook-retry] %s", result)
    except Exception:
        log.exception("[webhook-retry] retry_pending_deliveries raised")


def _full_fleet_drift_catchup_job() -> None:
    """Daily unscoped drift catch-up (B3).

    supervisor.py's _post_collection_hook now scopes its per-collection drift
    scan to the resources the triggering run actually touched — cheap enough
    to run after a 15-minute vSphere pulse. Scoping removed the system's only
    full-fleet catch-up as a side effect: detect_all() used to be called
    exclusively (and unscoped) from that hook. Several SKIP_HOOK domains
    (netdiscovery, host_reconcile, inventory_reconcile, ...) write snapshots
    but never trigger the hook themselves, so without this job their
    resources — and anything touched by a run other than the one that most
    recently changed it — would never be drift-checked again.

    Runs off-peak (01:00 UTC, ahead of the 6h/nightly collection groups) and
    is deliberately unscoped: no run_id filter, full resource_id sweep.
    """
    from infra_brain.agents.drift import DriftDetector

    try:
        detector = DriftDetector()
        events = detector.detect_all()  # scoped=False (default) — full fleet
        retired = detector.detect_state_drift()
        log.info(
            "[drift-catchup] full-fleet daily scan complete: %d config-drift "
            "event(s), %d resource retirement(s)",
            len(events),
            len(retired),
        )
    except Exception:
        log.exception("[drift-catchup] full-fleet daily scan failed")


def _redis_outage_catchup_job() -> None:
    """Hourly self-healing catch-up (#109).

    _make_job()'s except-LockBackendUnavailable branch records every
    (domain, scope) skipped this cycle because Redis itself was unreachable
    (distinct from the ordinary "lock already held" skip, which just means a
    prior run of that same domain is legitimately still in flight and needs
    no catch-up). Without this job that domain would simply wait for its next
    regular cron slot — up to a day away for daily/weekly-cadence domains —
    even though Redis may come back within minutes.

    Mirrors _full_fleet_drift_catchup_job's shape (a scheduled follow-up that
    re-runs an already-read-only step), but targets missed COLLECTIONS instead
    of re-scanning drift over data already in the DB: each pending entry is
    re-run through the exact same _make_job() closure used by the regular
    schedule, so it gets the identical lock-acquire/dispatch/release/error
    handling as a normal scheduled fire.

    Bounded retry, no infinite loop: every entry is popped before being
    retried, so at most one attempt happens per entry per hourly cycle. If
    Redis is still down, that retry's own LockBackendUnavailable handling
    re-adds the entry for the next hourly pass — it does not recurse or spin
    within this call. Entries first skipped on an earlier UTC day are dropped
    without a retry (stale — the domain's regular cron already moved on;
    retrying a multi-day-old miss is noise, not useful catch-up).
    """
    today = datetime.now(UTC).date()
    with _redis_outage_lock:
        pending = list(_redis_outage_pending.items())
        for domain_scope_key, _ in pending:
            _redis_outage_pending.pop(domain_scope_key, None)

    for (domain, scope), skipped_on in pending:
        if skipped_on != today:
            log.info(
                "[redis-outage-catchup] dropping stale skip for %s/%s "
                "(skipped %s, not retried)",
                domain,
                scope,
                skipped_on,
            )
            continue
        log.info(
            "[redis-outage-catchup] re-attempting %s/%s after earlier "
            "Redis-lock-unavailable skip",
            domain,
            scope,
        )
        try:
            _make_job(domain, scope)()
        except Exception:
            # _make_job()'s own _job() closure already absorbs every
            # exception it can anticipate (including re-queuing on a repeat
            # LockBackendUnavailable) — this is an extra backstop so a truly
            # unexpected error here still can't take down the hourly job for
            # every other pending domain.
            log.exception(
                "[redis-outage-catchup] unexpected error re-attempting %s/%s",
                domain,
                scope,
            )


def reap_stale_collection_runs() -> int:
    """F20: mark orphaned in_progress collection_runs as failed.

    A process death (OOM / SIGKILL) leaves a CollectionRun row stuck in
    status="in_progress" forever — fleet_health then shows it "running" for
    days. Any in_progress run whose started_at is older than
    settings.collection_run_stale_after_hours is assumed orphaned and reaped to
    status="failed" (finished_at set, error_message annotated). Fresh
    in_progress runs and terminal runs (completed/failed/skipped) are untouched.

    Returns the number of rows reaped. Best-effort: never raises — a reaper
    failure must not abort the rest of the hourly health job (it is logged via
    log.exception below, not silently swallowed). TRK-106 note: this reaper is
    the backstop for the residual case where ETLConnector.run()'s finally-block
    finalize itself fails to commit; it is not the primary fix.
    """
    from sqlalchemy import update

    from infra_brain.db.models import CollectionRun
    from infra_brain.db.session import get_session

    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(hours=settings.collection_run_stale_after_hours)
    note = "reaped: exceeded max runtime (process likely died)"
    try:
        with get_session() as session:
            result = session.execute(
                update(CollectionRun)
                .where(
                    CollectionRun.status == "in_progress",
                    CollectionRun.started_at < cutoff,
                )
                .values(
                    status="failed",
                    finished_at=datetime.now(UTC),
                    error_message=note,
                )
                .execution_options(synchronize_session=False)
            )
            session.commit()
            reaped = result.rowcount or 0
    except Exception:
        log.exception("[reaper] failed to reap stale in_progress collection_runs")
        return 0
    if reaped:
        log.warning(
            "[reaper] reaped %d stale in_progress collection_run(s) older than %dh",
            reaped,
            settings.collection_run_stale_after_hours,
        )
    return reaped


def _collection_health_job() -> None:
    """Hourly completeness monitor (R3 / F-007) + agent-anomaly monitor (item 9).

    Also records the scheduler heartbeat (OB-1 dead-man switch — see
    callbacks/freshness.py record_scheduler_heartbeat() /
    check_scheduler_deadman()): this job running at all, on its hourly cron,
    is itself the liveness signal an independent checker (main.py's lifespan
    asyncio task) watches for.

    F20: also reaps orphaned in_progress collection_runs (see
    reap_stale_collection_runs()) — this hourly job on the dedicated scheduler
    is the natural home for it.
    """
    from infra_brain.callbacks.anomaly import check_agent_anomalies
    from infra_brain.callbacks.freshness import (
        check_collection_health,
        is_informational_alert,
        record_scheduler_heartbeat,
    )

    try:
        record_scheduler_heartbeat()
    except Exception:
        log.exception("[collection-health] failed to record scheduler heartbeat")

    reap_stale_collection_runs()

    try:
        alerts = check_collection_health()
    except Exception:
        log.exception("[collection-health] monitor itself failed to run")
        alerts = []
    # Split by severity. check_collection_health() already treats an
    # "unconfigured (skipped)" domain as informational (skip_only -> never
    # unhealthy, never escalates), but this loop used to flatten every entry to
    # ERROR and hand the whole list to the ops webhook — so 11 unconfigured
    # collectors drowned the one domain that was actually failing, every pass.
    # Informational entries stay visible at INFO; only actionable ones alert.
    actionable = [a for a in alerts if not is_informational_alert(a)]
    for alert in alerts:
        if is_informational_alert(alert):
            log.info("[collection-health] %s", alert)
        else:
            log.error("[collection-health] ALERT %s", alert)
    if not alerts:
        log.info("[collection-health] all monitored domains healthy")
    _deliver_ops_alerts("collection_health", actionable)

    try:
        anomalies = check_agent_anomalies()
    except Exception:
        log.exception("[agent-anomaly] monitor itself failed to run")
        anomalies = []
    for anomaly_alert in anomalies:
        log.error("[agent-anomaly] ALERT %s", anomaly_alert)
    _deliver_ops_alerts("agent_anomaly", anomalies)

    # TRK-120: lightweight LLM cost visibility — log today's cumulative token
    # usage (sum of AgentDecisionLog.token_count). No alerting pipeline by
    # design; this is just an operator-visible number once real paid usage
    # starts. Best-effort: never let it break the health job.
    try:
        from infra_brain.callbacks.freshness import sum_llm_tokens_today

        log.info(
            "[llm-cost] AgentDecisionLog token_count sum today (UTC): %d",
            sum_llm_tokens_today(),
        )
    except Exception:
        log.debug("[llm-cost] daily token aggregate failed", exc_info=True)


def _make_agent_task_job(task_name: str, prompt: str):
    """Job factory for one headless-runner standing task (P6.1/P6.4).

    Same bespoke-callable shape as ``_full_fleet_drift_catchup_job``/
    ``_webhook_retry_job`` above — a zero-argument closure registered
    directly via ``self._scheduler.add_job(...)``, not ``_make_job()``
    (that helper is ETL-collector-specific: dedup locks, ``CollectionRun``
    semantics, ``AGENT_REGISTRY`` class dispatch — none of which applies to
    a prompt-driven agent turn). ``run_agent_task_sync`` already records the
    outcome on an ``AgentTaskRun`` row and never raises, so this wrapper's
    ``try/except`` is a pure belt-and-suspenders match for every other
    bespoke job here, not the primary error-recording path.
    """

    def _job() -> None:
        from infra_brain.runner.agent_task import run_agent_task_sync

        try:
            result = run_agent_task_sync(task_name, prompt)
            log.info("[agent-task:%s] %s", task_name, result.get("status"))
        except Exception:
            log.exception("[agent-task:%s] run_agent_task_sync raised", task_name)

    return _job


def _translate_cron_dow(field: str) -> str:
    """Translate a standard-cron day-of-week field to APScheduler's numbering.

    TRK-310: standard cron numbers weekdays 0=Sunday..6=Saturday (7 is also
    accepted as Sunday); APScheduler's ``CronTrigger(day_of_week=...)`` numbers
    them 0=Monday..6=Sunday (ISO weekday order, matching ``date.weekday()``).
    Passing a cron field straight through — which every schedule in this
    codebase did until this fix — silently shifts every schedule with a
    specific (non-``*``) numeric day-of-week by one day. Confirmed live:
    ``day_of_week="0"`` (intended Sunday) actually fired on Monday.

    Only numeric tokens are ambiguous between the two conventions; day names
    (``mon``, ``tue``, ...), ``*``, and step expressions (``*/N``) mean the
    same thing in both and pass through unchanged. Handles the field grammar
    actually used by cron: comma-separated tokens, each a single digit or a
    digit range (``a-b``), with an optional trailing ``/step``.
    """

    def _translate_token(token: str) -> str:
        base, _, step = token.partition("/")
        step_suffix = f"/{step}" if step else ""

        def _translate_num(n: str) -> str:
            return str((int(n) - 1) % 7)

        if base == "*" or not base:
            return token
        if "-" in base:
            lo, hi = base.split("-", 1)
            if lo.isdigit() and hi.isdigit():
                return f"{_translate_num(lo)}-{_translate_num(hi)}{step_suffix}"
            return token
        if base.isdigit():
            return f"{_translate_num(base)}{step_suffix}"
        return token

    return ",".join(_translate_token(t) for t in field.split(","))


class SchedulerService:
    """APScheduler-backed service that runs infra collection on cron schedules."""

    def __init__(self):
        self._scheduler = BackgroundScheduler()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_job(self, domain: str, cron_str: str) -> None:
        """Register *domain* (scope='all') to run on *cron_str* (5-field cron expression)."""
        minute, hour, day, month, day_of_week = cron_str.split()
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=_translate_cron_dow(day_of_week),
        )
        self._scheduler.add_job(
            _make_job(domain),
            trigger=trigger,
            id=f"infra_brain_{domain}",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=_misfire_grace_time(day_of_week),
        )
        log.info(
            "[scheduler] Registered job for domain=%s cron=%s",
            domain,
            cron_str,
            extra={"domain": domain},
        )

    def add_job_scoped(self, domain: str, scope: str, cron_str: str) -> None:
        """Register a scoped job (non-default scope) on *cron_str*."""
        minute, hour, day, month, day_of_week = cron_str.split()
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=_translate_cron_dow(day_of_week),
        )
        self._scheduler.add_job(
            _make_job(domain, scope),
            trigger=trigger,
            id=f"infra_brain_{domain}_{scope}",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=_misfire_grace_time(day_of_week),
        )
        log.info(
            "[scheduler] Registered scoped job domain=%s scope=%s cron=%s",
            domain,
            scope,
            cron_str,
        )

    def start(self) -> None:
        """Register default schedules and start the background scheduler."""
        settings = get_settings()
        # Pulse-first hold: gate the scheduled 6h FULL vSphere inventory behind a
        # flag. "vsphere" is a full-scope job; skip registering it when the
        # flag is off so only the 15-min pulse runs automatically. A manual
        # dispatch("vsphere", scope="all") still works on demand, and flipping
        # the flag re-enables fulls (no code change). The scoped pulse job
        # below is registered unconditionally.
        full_vsphere_enabled = settings.vsphere_full_inventory_enabled
        _FULL_VSPHERE_DOMAINS = {"vsphere"}

        # Retired domains (AgentSpec.retired — etl/spec.py) get NO cron at all.
        # Resolved once here rather than at _DEFAULT_SCHEDULES construction
        # time so that dict stays a pure, import-safe "what cron would this
        # domain have" view for its display consumers (the dashboard roster and
        # scan-schedule endpoints), and so the collection_revived_domains
        # override is read from live settings at start() rather than frozen at
        # module import.
        retired = retired_domains()
        if retired:
            log.info(
                "[scheduler] retired domains — no jobs registered: %s "
                "(set COLLECTION_REVIVED_DOMAINS to turn one back on)",
                sorted(retired),
            )

        # Phase 2 Task 4: strictly opt-in sweep graph. When enabled, ONE
        # scheduled sweep job (run_sweep_sync()) replaces the per-domain
        # collector crons for sweep-member COLLECTOR-tier domains (see
        # etl/spec.py sweep_members()) — those domains are now collected
        # inside the sweep graph's dispatch→collector node fan-out instead of
        # each having its own standalone cron. Reporters/on-demand/reasoner
        # crons (fleet_health, coverage, discovery, compliance, ...) are
        # untouched — reasoner/reporter/on-demand domains keep their crons
        # (compliance's 30 6 * * * included); drift/notification have
        # schedule=None and were never in _DEFAULT_SCHEDULES.
        # remediation/drift_learning are Phase-3-deferred sweep non-members
        # (etl/spec.py's _PHASE3_DEFERRED) and keep their own crons too.
        # Disabled (default) is byte-identical to pre-Task-4 behavior: this
        # whole block is skipped and graph.py is never imported at scheduler
        # startup.
        sweep_collector_domains: set[str] = set()
        if settings.sweep_graph_enabled:
            from infra_brain.etl.spec import Tier, sweep_members
            from infra_brain.graph import run_sweep_sync

            sweep_collector_domains = set(sweep_members()[Tier.COLLECTOR])

            def _sweep_job() -> None:
                try:
                    run_sweep_sync()
                except Exception:
                    log.exception("[scheduler] sweep graph run failed")

            minute, hour, day, month, day_of_week = settings.sweep_graph_schedule.split()
            self._scheduler.add_job(
                _sweep_job,
                trigger=CronTrigger(
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=_translate_cron_dow(day_of_week),
                ),
                id="infra_brain_sweep_graph",
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=300,
            )
            log.info(
                "[scheduler] Registered sweep graph job (cron=%s) — skipping "
                "per-domain collector crons for sweep members: %s",
                settings.sweep_graph_schedule,
                sorted(sweep_collector_domains),
            )

        for domain, cron_str in _DEFAULT_SCHEDULES.items():
            if domain in retired:
                # Switched off by standing decision — not a schedule to hold,
                # skip, or catch up on. Logged once above, not per domain.
                continue
            if domain in sweep_collector_domains:
                # Now collected inside the sweep graph's dispatch node.
                continue
            if domain in _FULL_VSPHERE_DOMAINS and not full_vsphere_enabled:
                log.info(
                    "[scheduler] Holding scheduled full inventory for domain=%s "
                    "(VSPHERE_FULL_INVENTORY_ENABLED is false) — pulse-only",
                    domain,
                )
                continue
            self.add_job(domain, cron_str)
        # 15-minute vSphere pulse: refresh power state + quickStats between full 6h inventory scans.
        # Independent of the full-inventory hold above — but NOT of retirement.
        # This call bypasses _DEFAULT_SCHEDULES entirely, so the loop's retired
        # check above cannot reach it; at 4x/hour it is on its own the largest
        # single source of `skipped` collection_runs when vSphere is off, which
        # is exactly what retiring the domain is meant to stop.
        if "vsphere" not in retired:
            self.add_job_scoped("vsphere", "pulse", "5,20,35,50 * * * *")
        # AA-C-3/AA-D-23: QueryAgent's weekly warm/health-check job must dispatch
        # with scope="health" — collect() only runs the DB-connectivity check
        # for that exact scope string (query.py QueryAgent.collect()). Dispatching
        # with the default scope="all" (via add_job) would be a dead no-op slot;
        # this does NOT plug a monitoring gap — the independent hourly
        # _collection_health_job below already covers collection-health alerting.
        self.add_job_scoped("query", "health", "30 2 * * 0")
        # Hourly collection-health monitor (R3): stale/partial/empty alerting.
        self._scheduler.add_job(
            _collection_health_job,
            trigger=CronTrigger(minute="20"),
            id="infra_brain_collection_health",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=300,
        )
        # Retention prune (F-030): daily 04:30 UTC. Registered directly (NOT via
        # _DEFAULT_SCHEDULES — that dict is parsed by registry-sync as domains).
        self._scheduler.add_job(
            prune_expired,
            trigger=CronTrigger(minute="30", hour="4"),
            id="infra_brain_retention_prune",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        log.info("[scheduler] Registered retention prune job (daily 04:30 UTC)")
        # B3: daily full-fleet drift catch-up (01:00 UTC — ahead of the 6h/
        # nightly collection groups). See _full_fleet_drift_catchup_job()'s
        # docstring for why this exists now that the per-collection hook
        # scopes its scan to just the triggering run's resources.
        self._scheduler.add_job(
            _full_fleet_drift_catchup_job,
            trigger=CronTrigger(minute="0", hour="1"),
            id="infra_brain_drift_full_fleet_catchup",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        log.info("[scheduler] Registered daily full-fleet drift catch-up job (01:00 UTC)")
        # #109: hourly self-healing catch-up for domains skipped this same day
        # because Redis itself was unreachable (LockBackendUnavailable), not
        # just an ordinary "another run of this domain is in progress" skip.
        self._scheduler.add_job(
            _redis_outage_catchup_job,
            trigger=CronTrigger(minute="40"),
            id="infra_brain_redis_outage_catchup",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=300,
        )
        log.info("[scheduler] Registered hourly Redis-outage catch-up job (#109)")
        # Issue #112: hourly retry sweep for failed outbound webhook
        # subscription deliveries (exponential backoff is enforced by
        # WebhookDelivery.next_attempt_at itself — this job just picks up
        # whatever is due each time it runs).
        self._scheduler.add_job(
            _webhook_retry_job,
            trigger=CronTrigger(minute="10"),
            id="infra_brain_webhook_retry",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=300,
        )
        log.info("[scheduler] Registered hourly webhook-subscription retry job (#112)")
        # P6.1/P6.4: headless-runner standing tasks (drift-triage, self-review,
        # eol-report) — off by default (empty token = clean no-op, same
        # convention as every other optional integration in this repo). A
        # human mints HEADLESS_RUNNER_MCP_TOKEN via
        # scripts/mint_headless_runner_key.py to turn these on.
        if settings.headless_runner_mcp_token:
            from infra_brain.runner.tasks import STANDING_TASKS

            cron_by_task = {
                name.split(":", 1)[1]: cron
                for name, cron in _ADHOC_SCHEDULES.items()
                if name.startswith("agent_task:")
            }
            for task_name, prompt in STANDING_TASKS:
                cron_str = cron_by_task.get(task_name)
                if not cron_str:
                    log.warning(
                        "[scheduler] no cron configured for standing task %r — skipping",
                        task_name,
                    )
                    continue
                minute, hour, day, month, day_of_week = cron_str.split()
                self._scheduler.add_job(
                    _make_agent_task_job(task_name, prompt),
                    trigger=CronTrigger(
                        minute=minute,
                        hour=hour,
                        day=day,
                        month=month,
                        day_of_week=_translate_cron_dow(day_of_week),
                    ),
                    id=f"infra_brain_agent_task_{task_name}",
                    replace_existing=True,
                    coalesce=True,
                    misfire_grace_time=max(3600, _misfire_grace_time(day_of_week)),
                )
            log.info(
                "[scheduler] Registered %d headless-runner standing task(s): %s",
                len(STANDING_TASKS),
                [name for name, _ in STANDING_TASKS],
            )
        else:
            log.info(
                "[scheduler] Headless runner standing tasks disabled "
                "(HEADLESS_RUNNER_MCP_TOKEN not configured)"
            )
        self._scheduler.start()
        log.info("[scheduler] Started with %d jobs", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        """Shut down the background scheduler gracefully."""
        # wait=True drains in-flight jobs before returning.
        # Do NOT pass timeout= — that parameter was never implemented in APScheduler 3.x
        # and raises TypeError. terminationGracePeriodSeconds in k8s/scheduler.yaml
        # provides the wall-clock limit instead.
        self._scheduler.shutdown(wait=True)
        log.info("[scheduler] Stopped")


def start_scheduler() -> "SchedulerService":
    """Create and start a SchedulerService. Used by container entry points."""
    svc = SchedulerService()
    svc.start()
    return svc


def _install_sigterm_handler() -> None:
    """F19: translate SIGTERM into SystemExit so the drain path runs.

    k8s / docker stop send SIGTERM (not KeyboardInterrupt) on shutdown. Python's
    default SIGTERM disposition terminates the process immediately WITHOUT
    raising anything, so run_scheduler_forever()'s ``except SystemExit`` drain
    (svc.stop(), which waits for in-flight jobs) never ran despite
    terminationGracePeriodSeconds=120. Raising SystemExit from the handler makes
    the existing drain path fire.

    Windows note: signal.SIGTERM exists on Windows but handler delivery
    semantics differ and signal.signal() can raise; guard so import/dev on
    Windows never breaks.
    """
    import signal

    def _on_sigterm(signum, frame):
        log.info("[scheduler] SIGTERM received — draining and shutting down")
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError, AttributeError, RuntimeError) as exc:
        # Not in the main thread, or platform doesn't support it — fall back to
        # the default disposition (KeyboardInterrupt/SystemExit still drains).
        log.warning("[scheduler] could not install SIGTERM handler: %s", exc)


def run_scheduler_forever() -> None:
    """Blocking entry point for the scheduler container (console script
    ``infra-brain-scheduler``)."""
    import time

    # F-002 / OB-4: shared JSON-envelope config so every container's `docker logs`
    # output parses the same way — see logging_config.configure_logging().
    from infra_brain.logging_config import configure_logging
    from infra_brain.observability import init_tracing
    from infra_brain.secrets import load_secrets_into_env

    configure_logging()
    load_secrets_into_env()
    init_tracing()
    # Schema-drift keystone (mirrors main.py's lifespan): refuse to start firing
    # cron sweeps against a DB whose schema does not match the migrated/declared
    # schema. No-op on SQLite (test), so this only enforces against the real
    # Postgres deploy — on drift it raises and the process exits non-zero rather
    # than silently running sweeps against a drifted/un-migrated DB.
    try:
        from infra_brain.db.schema_check import assert_schema_current
        from infra_brain.db.session import get_engine

        assert_schema_current(get_engine())
    except Exception:
        # Any drift (or inability to verify against a real DB) must abort startup
        # loudly — do NOT swallow. Re-raise so the scheduler process exits
        # non-zero instead of running sweeps against a drifted schema.
        log.exception("[schema-check] scheduler startup schema verification failed — aborting")
        raise
    # F19: drain on SIGTERM (k8s/docker stop), not just KeyboardInterrupt.
    _install_sigterm_handler()
    svc = start_scheduler()
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        svc.stop()
