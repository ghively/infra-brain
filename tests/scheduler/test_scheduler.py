"""Tests for SchedulerService (APScheduler-backed collection scheduler)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from infra_brain.scheduler import SchedulerService, _DEFAULT_SCHEDULES
from infra_brain.supervisor import AGENT_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_result(domain: str):
    r = MagicMock()
    r.status = "completed"
    r.domain = domain
    return r


# ---------------------------------------------------------------------------
# Test: job runs dispatch when lock is acquired
# ---------------------------------------------------------------------------


@patch("infra_brain.scheduler.release")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value="mock-token-abc")
def test_job_runs_dispatch(mock_acquire, mock_dispatch, mock_release):
    """When try_acquire returns a token the job must call dispatch then release with that token."""
    mock_dispatch.return_value = _make_mock_result("linux")

    svc = SchedulerService()
    # Grab the internal callable directly without starting the scheduler
    svc.add_job("linux", "0 */6 * * *")
    jobs = svc._scheduler.get_jobs()
    assert len(jobs) == 1, "Expected exactly 1 registered job"

    # Invoke the job function directly
    job_func = jobs[0].func
    job_func()

    mock_acquire.assert_called_once_with("linux", "all")
    mock_dispatch.assert_called_once_with("linux", trigger_type="scheduled", scope="all")
    mock_release.assert_called_once_with("linux", "all", token="mock-token-abc")


# ---------------------------------------------------------------------------
# Test: job skips dispatch when lock is already held
# ---------------------------------------------------------------------------


@patch("infra_brain.scheduler.release")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=False)
def test_job_skips_when_locked(mock_acquire, mock_dispatch, mock_release):
    """When try_acquire returns False the job must not call dispatch or release."""
    svc = SchedulerService()
    svc.add_job("windows", "0 */6 * * *")
    job_func = svc._scheduler.get_jobs()[0].func
    job_func()

    mock_acquire.assert_called_once_with("windows", "all")
    mock_dispatch.assert_not_called()
    mock_release.assert_not_called()


# ---------------------------------------------------------------------------
# Test: scheduler starts and stops cleanly
# ---------------------------------------------------------------------------


@patch("infra_brain.scheduler.get_settings")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=True)
@patch("infra_brain.scheduler.release")
def test_scheduler_starts_and_stops(mock_release, mock_acquire, mock_dispatch, mock_settings):
    """start() registers default jobs and the scheduler can be stopped.

    With the full-inventory flag ENABLED, every default schedule entry is
    registered plus the extra scoped vSphere "pulse" job (15-min quickStats
    refresh between the 6h full inventory scans), the scoped query/"health"
    job (AA-C-3/AA-D-23 — "query" is deliberately NOT in _DEFAULT_SCHEDULES),
    the hourly collection-health monitor, and the retention prune job (F-030,
    registered directly on self._scheduler — not via _DEFAULT_SCHEDULES,
    since that dict is parsed by registry-sync as domains).
    """
    mock_settings.return_value.vsphere_full_inventory_enabled = True
    mock_settings.return_value.sweep_graph_enabled = False
    mock_settings.return_value.headless_runner_mcp_token = ""
    svc = SchedulerService()
    svc.start()

    try:
        jobs = svc._scheduler.get_jobs()
        from infra_brain.scheduler import _DEFAULT_SCHEDULES

        from infra_brain.etl.spec import retired_domains

        # Retired domains (AgentSpec.retired) keep their cron string in
        # _DEFAULT_SCHEDULES — that is the cadence they would resume on — but
        # register no job, so they must come off the expected count.
        retired = retired_domains() & set(_DEFAULT_SCHEDULES)
        # +1 for the query/health scoped job, +1 for the hourly collection-health
        # monitor (2.5), +1 for the retention prune job (F-030), +1 for the
        # daily full-fleet drift catch-up job (B3), +1 for the hourly
        # Redis-outage catch-up job (#109), +1 for the hourly
        # webhook-subscription retry job (#112). The vsphere/pulse scoped job is
        # NOT counted while vsphere is retired — see
        # test_retirement_dominates_the_full_inventory_flag below.
        expected = len(_DEFAULT_SCHEDULES) - len(retired) + 6
        assert len(jobs) == expected, f"Expected {expected} jobs, got {len(jobs)}"
        assert svc._scheduler.running
    finally:
        svc.stop()

    assert not svc._scheduler.running


# ---------------------------------------------------------------------------
# Test: pulse-first hold — the VSPHERE_FULL_INVENTORY_ENABLED flag gates ONLY
# the scheduled 6h full vsphere job; the 15-min pulse always registers.
# ---------------------------------------------------------------------------


def _job_ids(svc) -> set[str]:
    return {j.id for j in svc._scheduler.get_jobs()}


@patch("infra_brain.scheduler.get_settings")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=True)
@patch("infra_brain.scheduler.release")
def test_full_inventory_held_when_flag_false(
    mock_release, mock_acquire, mock_dispatch, mock_settings
):
    """Flag false → pulse job registered, but NOT the 6h full vsphere job.

    vsphere is retired in this environment, which switches off the pulse and the
    full job alike, so this test revives it — the flag's own logic is what is
    under test here, and it still has to work for whoever turns vSphere back on.
    Retirement-beats-flag is asserted separately, below.
    """
    mock_settings.return_value.vsphere_full_inventory_enabled = False
    mock_settings.return_value.sweep_graph_enabled = False
    mock_settings.return_value.headless_runner_mcp_token = ""
    svc = SchedulerService()
    with patch("infra_brain.scheduler.retired_domains", return_value=set()):
        svc.start()

    try:
        ids = _job_ids(svc)
        # Pulse always present
        assert "infra_brain_vsphere_pulse" in ids
        # Full vsphere is HELD
        assert "infra_brain_vsphere" not in ids
        # Unrelated full jobs are unaffected
        assert "infra_brain_linux" in ids
        # Held one full job → defaults minus 1, plus 1 vsphere pulse, plus 1
        # query/health scoped job, plus 1 collection-health monitor (2.5),
        # plus 1 retention prune (F-030), plus 1 daily full-fleet drift
        # catch-up job (B3), plus 1 hourly Redis-outage catch-up job (#109),
        # plus 1 hourly webhook-subscription retry job (#112)
        assert (
            len(svc._scheduler.get_jobs())
            == len(_DEFAULT_SCHEDULES) - 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1
        )
    finally:
        svc.stop()


@patch("infra_brain.scheduler.get_settings")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=True)
@patch("infra_brain.scheduler.release")
def test_full_inventory_registered_when_flag_true(
    mock_release, mock_acquire, mock_dispatch, mock_settings
):
    """Flag true → both the pulse job AND the 6h full vsphere job registered.

    Revives vsphere for the same reason as the held-flag test above.
    """
    mock_settings.return_value.vsphere_full_inventory_enabled = True
    mock_settings.return_value.sweep_graph_enabled = False
    mock_settings.return_value.headless_runner_mcp_token = ""
    svc = SchedulerService()
    with patch("infra_brain.scheduler.retired_domains", return_value=set()):
        svc.start()

    try:
        ids = _job_ids(svc)
        assert "infra_brain_vsphere_pulse" in ids
        assert "infra_brain_vsphere" in ids
    finally:
        svc.stop()


@patch("infra_brain.scheduler.get_settings")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=True)
@patch("infra_brain.scheduler.release")
def test_retirement_dominates_the_full_inventory_flag(
    mock_release, mock_acquire, mock_dispatch, mock_settings
):
    """A retired vsphere registers NEITHER job, even with the full-inventory
    flag on.

    The 15-minute pulse is the case that matters: it is added by a hardcoded
    add_job_scoped() call that bypasses _DEFAULT_SCHEDULES entirely, so the
    retired filter in the schedule loop cannot reach it. At 4x/hour against a
    vCenter that does not exist, it was the single largest producer of
    `skipped` collection_runs on the box — if retirement missed it, retiring
    the domain would barely dent the noise it exists to remove.
    """
    mock_settings.return_value.vsphere_full_inventory_enabled = True
    mock_settings.return_value.sweep_graph_enabled = False
    mock_settings.return_value.headless_runner_mcp_token = ""
    svc = SchedulerService()
    svc.start()

    try:
        ids = _job_ids(svc)
        assert "infra_brain_vsphere_pulse" not in ids
        assert "infra_brain_vsphere" not in ids
    finally:
        svc.stop()


# ---------------------------------------------------------------------------
# Test: dispatch error does NOT crash the scheduler (job absorbs exceptions)
# ---------------------------------------------------------------------------


@patch("infra_brain.scheduler.release")
@patch("infra_brain.scheduler.dispatch", side_effect=RuntimeError("boom"))
@patch("infra_brain.scheduler.try_acquire", return_value="mock-token-cloud")
def test_job_survives_dispatch_error(mock_acquire, mock_dispatch, mock_release):
    """A failing dispatch must not propagate — the scheduler must stay alive."""
    svc = SchedulerService()
    svc.add_job("cloud", "0 2 * * *")
    job_func = svc._scheduler.get_jobs()[0].func

    # Must not raise
    job_func()

    mock_release.assert_called_once_with("cloud", "all", token="mock-token-cloud")


# ---------------------------------------------------------------------------
# Test: start_scheduler() entry point
# ---------------------------------------------------------------------------


def test_start_scheduler_returns_running_service():
    """start_scheduler() must return a running SchedulerService."""
    from infra_brain.scheduler import start_scheduler

    svc = start_scheduler()
    try:
        assert svc._scheduler.running
    finally:
        svc.stop()


def test_onprem_has_no_schedule():
    """F-006: onprem was removed from AGENT_REGISTRY; it must not be scheduled."""
    assert "onprem" not in _DEFAULT_SCHEDULES


def test_all_registry_domains_are_scheduled_or_exempt():
    """Every AGENT_REGISTRY domain must be scheduled or intentionally exempt."""
    EXEMPT = {
        # Legacy key (IntegrationAgent deleted): kept to avoid false positives if old code paths persist
        "integration",
        # Post-collection hook agents — run after every collection, not on their own cadence
        "drift",
        "notification",
        # On-demand knowledge-graph agents — invoked directly, not by the scheduler
        "coverage",
        "query",
        # Event-driven / on-demand agents — dispatched via propose_inventory_fix(), not scheduled
        "inventory_mr",
    }
    unscheduled = set(AGENT_REGISTRY.keys()) - set(_DEFAULT_SCHEDULES.keys()) - EXEMPT
    assert not unscheduled, (
        f"Domains in AGENT_REGISTRY with no schedule and not exempt: {sorted(unscheduled)}. "
        "Add to _DEFAULT_SCHEDULES or to EXEMPT."
    )


# ---------------------------------------------------------------------------
# Test: no two scheduled domains share the exact (hour, minute) cron slot
# ---------------------------------------------------------------------------


def test_no_exact_schedule_collisions():
    """No two scheduled domains/ad-hoc jobs may share the exact minute+hour
    cron slot. Extended (#75) to also cover the 5 ad-hoc jobs SchedulerService.
    start() registers directly (bypassing _DEFAULT_SCHEDULES) — previously
    invisible here, which let vsphere:pulse collide with netdiscovery
    undetected.

    Mirrors .claude/scripts/dev_status.py::check_schedule_collisions, which keys
    each 5-field cron string on its literal (hour, minute) fields. Two domains
    landing on the identical slot contend for the same Redis lock and run
    simultaneously — each domain has its own dedup lock, so neither is
    silently skipped; the real cost is correlated resource load. Regression
    guard for the 2026-07-21 stagger of coverage/discovery (was 03:00) and
    learning_feedback/remediation (was 05:30).
    """
    import collections

    from infra_brain.etl.spec import retired_domains
    from infra_brain.scheduler import _ADHOC_SCHEDULES

    # Retired domains keep their cron string but register no job, so they
    # cannot contend for anything. Counting them would force a live domain to
    # be staggered around a slot nothing occupies.
    retired = retired_domains()

    by_slot: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for domain, cron in {**_DEFAULT_SCHEDULES, **_ADHOC_SCHEDULES}.items():
        if domain in retired:
            continue
        parts = cron.split()
        assert len(parts) == 5, f"{domain}: expected a 5-field cron, got {cron!r}"
        minute, hour = parts[0], parts[1]
        by_slot[(hour, minute)].append(domain)

    collisions = {slot: sorted(d) for slot, d in by_slot.items() if len(d) > 1}
    assert not collisions, (
        "Schedule collisions (identical hour+minute cron fields) — stagger by "
        f">=30 min: {collisions}"
    )


def test_make_job_redis_unavailable_writes_failed_collection_run_row():
    from infra_brain.scheduler import _make_job
    from infra_brain.dedup import LockBackendUnavailable

    job = _make_job("linux", scope="all")
    mock_session = MagicMock()
    with (
        patch("infra_brain.scheduler.try_acquire", side_effect=LockBackendUnavailable()),
        patch("infra_brain.redis_outage.get_session") as mock_get_session,
    ):
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        job()

    mock_session.add.assert_called_once()
    row = mock_session.add.call_args[0][0]
    assert row.domain == "linux"
    assert row.status == "failed"


# ---------------------------------------------------------------------------
# Tests: #109 — hourly Redis-outage catch-up job
# ---------------------------------------------------------------------------


def _clear_redis_outage_pending():
    import infra_brain.scheduler as sched_mod

    with sched_mod._redis_outage_lock:
        sched_mod._redis_outage_pending.clear()


def test_make_job_records_pending_on_redis_outage():
    """A LockBackendUnavailable skip must be recorded for the catch-up job to retry."""
    import infra_brain.scheduler as sched_mod
    from infra_brain.dedup import LockBackendUnavailable

    _clear_redis_outage_pending()
    job = sched_mod._make_job("linux", scope="all")
    with (
        patch("infra_brain.scheduler.try_acquire", side_effect=LockBackendUnavailable()),
        patch("infra_brain.redis_outage.record_redis_outage_run"),
    ):
        job()

    assert ("linux", "all") in sched_mod._redis_outage_pending
    _clear_redis_outage_pending()


def test_job_skip_when_locked_does_not_record_pending():
    """An ordinary 'lock already held' skip (another run in progress) must NOT
    be queued for catch-up — only a genuine Redis-unavailable skip should be."""
    import infra_brain.scheduler as sched_mod

    _clear_redis_outage_pending()
    with (
        patch("infra_brain.scheduler.try_acquire", return_value=False),
        patch("infra_brain.scheduler.dispatch"),
        patch("infra_brain.scheduler.release"),
    ):
        sched_mod._make_job("windows", scope="all")()

    assert sched_mod._redis_outage_pending == {}


def test_redis_outage_catchup_retries_pending_domain_same_day():
    """A domain skipped earlier today for a Redis outage must be re-dispatched
    once the catch-up job runs and Redis has recovered."""
    import infra_brain.scheduler as sched_mod

    _clear_redis_outage_pending()
    today = datetime.now(timezone.utc).date()
    sched_mod._redis_outage_pending[("linux", "all")] = today

    with (
        patch("infra_brain.scheduler.try_acquire", return_value="tok-1") as mock_acquire,
        patch("infra_brain.scheduler.dispatch") as mock_dispatch,
        patch("infra_brain.scheduler.release") as mock_release,
    ):
        mock_dispatch.return_value = _make_mock_result("linux")
        sched_mod._redis_outage_catchup_job()

    mock_acquire.assert_called_once_with("linux", "all")
    mock_dispatch.assert_called_once_with("linux", trigger_type="scheduled", scope="all")
    mock_release.assert_called_once_with("linux", "all", token="tok-1")
    # Retried entry must be cleared, not left to accumulate.
    assert sched_mod._redis_outage_pending == {}


def test_redis_outage_catchup_drops_stale_entries_without_retrying():
    """An entry recorded on an earlier UTC day must be dropped, not retried —
    the domain's regular cron has already moved past it."""
    import infra_brain.scheduler as sched_mod

    _clear_redis_outage_pending()
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    sched_mod._redis_outage_pending[("windows", "all")] = yesterday

    with (
        patch("infra_brain.scheduler.try_acquire") as mock_acquire,
        patch("infra_brain.scheduler.dispatch") as mock_dispatch,
    ):
        sched_mod._redis_outage_catchup_job()

    mock_acquire.assert_not_called()
    mock_dispatch.assert_not_called()
    assert sched_mod._redis_outage_pending == {}


def test_redis_outage_catchup_still_locked_out_does_not_infinite_loop():
    """If the retry itself hits LockBackendUnavailable again, the catch-up job
    must complete (re-queuing the entry for the next hourly pass) instead of
    looping or raising."""
    import infra_brain.scheduler as sched_mod
    from infra_brain.dedup import LockBackendUnavailable

    _clear_redis_outage_pending()
    today = datetime.now(timezone.utc).date()
    sched_mod._redis_outage_pending[("cloud", "all")] = today

    with (
        patch("infra_brain.scheduler.try_acquire", side_effect=LockBackendUnavailable()),
        patch("infra_brain.redis_outage.record_redis_outage_run"),
    ):
        sched_mod._redis_outage_catchup_job()  # must return, not hang/raise

    # Re-queued for the next hourly pass, exactly one entry — no duplication.
    assert sched_mod._redis_outage_pending == {("cloud", "all"): today}
    _clear_redis_outage_pending()


# ---------------------------------------------------------------------------
# P6.1/P6.4: headless-runner standing tasks
# ---------------------------------------------------------------------------


@patch("infra_brain.scheduler.get_settings")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=True)
@patch("infra_brain.scheduler.release")
def test_agent_task_jobs_not_registered_when_token_unset(
    mock_release, mock_acquire, mock_dispatch, mock_settings
):
    mock_settings.return_value.vsphere_full_inventory_enabled = True
    mock_settings.return_value.sweep_graph_enabled = False
    mock_settings.return_value.headless_runner_mcp_token = ""
    svc = SchedulerService()
    svc.start()
    try:
        ids = {j.id for j in svc._scheduler.get_jobs()}
        assert not any(i.startswith("infra_brain_agent_task_") for i in ids)
    finally:
        svc.stop()


@patch("infra_brain.scheduler.get_settings")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=True)
@patch("infra_brain.scheduler.release")
def test_agent_task_jobs_registered_when_token_configured(
    mock_release, mock_acquire, mock_dispatch, mock_settings
):
    mock_settings.return_value.vsphere_full_inventory_enabled = True
    mock_settings.return_value.sweep_graph_enabled = False
    mock_settings.return_value.headless_runner_mcp_token = "ibmcp_test"
    svc = SchedulerService()
    svc.start()
    try:
        ids = {j.id for j in svc._scheduler.get_jobs()}
        assert "infra_brain_agent_task_drift-triage" in ids
        assert "infra_brain_agent_task_self-review" in ids
        assert "infra_brain_agent_task_eol-report" in ids
    finally:
        svc.stop()


def test_make_agent_task_job_logs_success(monkeypatch, caplog):
    import logging

    import infra_brain.scheduler as sched_mod

    monkeypatch.setattr(
        "infra_brain.runner.agent_task.run_agent_task_sync",
        lambda task_name, prompt: {"status": "completed", "run_id": "r1"},
    )
    job = sched_mod._make_agent_task_job("drift-triage", "prompt text")
    with caplog.at_level(logging.INFO):
        job()  # must not raise
    assert any("drift-triage" in r.message for r in caplog.records)


def test_make_agent_task_job_survives_exception(monkeypatch, caplog):
    import logging

    import infra_brain.scheduler as sched_mod

    def _boom(task_name, prompt):
        raise RuntimeError("LLM unreachable")

    monkeypatch.setattr("infra_brain.runner.agent_task.run_agent_task_sync", _boom)
    job = sched_mod._make_agent_task_job("drift-triage", "prompt text")
    with caplog.at_level(logging.ERROR):
        job()  # must not raise even though run_agent_task_sync did
    assert any("raised" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test: TRK-310 -- cron day-of-week must be translated for APScheduler
# ---------------------------------------------------------------------------
# Standard cron numbers weekdays 0=Sunday..6=Saturday (7 also accepted as
# Sunday); APScheduler's CronTrigger(day_of_week=...) numbers them
# 0=Monday..6=Sunday. A schedule string is authored in cron convention
# (AGENT_REGISTRY / _DEFAULT_SCHEDULES, docs, the operator's mental model) and
# must fire on the cron-intended weekday, not APScheduler's raw interpretation
# of the same digit.


def test_translate_cron_dow_single_digits():
    from infra_brain.scheduler import _translate_cron_dow

    # cron: 0=Sun 1=Mon 2=Tue 3=Wed 4=Thu 5=Fri 6=Sat 7=Sun(alt)
    # APScheduler: 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun
    assert _translate_cron_dow("0") == "6"  # Sun -> APS Sun
    assert _translate_cron_dow("1") == "0"  # Mon -> APS Mon
    assert _translate_cron_dow("2") == "1"
    assert _translate_cron_dow("6") == "5"  # Sat -> APS Sat
    assert _translate_cron_dow("7") == "6"  # Sun(alt) -> APS Sun


def test_translate_cron_dow_passthrough_forms():
    from infra_brain.scheduler import _translate_cron_dow

    assert _translate_cron_dow("*") == "*"
    assert _translate_cron_dow("*/2") == "*/2"
    assert _translate_cron_dow("mon,wed,fri") == "mon,wed,fri"


def test_translate_cron_dow_lists_and_ranges():
    from infra_brain.scheduler import _translate_cron_dow

    assert _translate_cron_dow("0,1") == "6,0"  # Sun,Mon -> APS Sun,Mon
    assert _translate_cron_dow("1-5") == "0-4"  # Mon-Fri -> APS Mon-Fri


def test_add_job_sunday_schedule_fires_on_sunday_not_monday():
    """Live-reproduces the TRK-310 bug: before the fix, day_of_week="0" (cron
    Sunday) actually fired on Monday. Asserts the real next-fire-time weekday,
    not just the translated field string, so this fails if the fix regresses
    even via an unrelated refactor of add_job."""
    svc = SchedulerService()
    svc.add_job("discovery", "0 3 * * 0")  # intended: Sunday 03:00 UTC
    job = svc._scheduler.get_job("infra_brain_discovery")
    assert job is not None

    # 2026-08-03 is a Monday -- if the bug were present, the very next
    # candidate would incorrectly be this same day instead of the following Sunday.
    monday = datetime(2026, 8, 3, 0, 0, 0, tzinfo=timezone.utc)
    next_fire = job.trigger.get_next_fire_time(None, monday)
    assert next_fire.strftime("%A") == "Sunday"
    assert next_fire.hour == 3 and next_fire.minute == 0


def test_add_job_scoped_also_translates_dow():
    svc = SchedulerService()
    svc.add_job_scoped("discovery", "custom-scope", "0 3 * * 0")
    job = svc._scheduler.get_job("infra_brain_discovery_custom-scope")
    monday = datetime(2026, 8, 3, 0, 0, 0, tzinfo=timezone.utc)
    next_fire = job.trigger.get_next_fire_time(None, monday)
    assert next_fire.strftime("%A") == "Sunday"


def test_weekly_job_gets_long_misfire_grace_time():
    """A weekly (specific day_of_week) job must get the long grace window --
    drift_learning/learning_feedback both silently missed their first-ever
    slot with the old flat 300s, most plausibly because the scheduler
    container was mid-recreate at the exact trigger instant."""
    svc = SchedulerService()
    svc.add_job("drift_learning", "0 4 * * 0")
    job = svc._scheduler.get_job("infra_brain_drift_learning")
    assert job.misfire_grace_time == 6 * 3600


def test_frequent_job_keeps_short_misfire_grace_time():
    """An every-N-minutes/hourly/daily job (day_of_week="*") must keep the
    short default -- its next slot is at most a day away, so a long grace
    window would just risk an unexpectedly-late run."""
    svc = SchedulerService()
    svc.add_job("netdiscovery", "*/15 * * * *")
    job = svc._scheduler.get_job("infra_brain_netdiscovery")
    assert job.misfire_grace_time == 300


def test_add_job_scoped_weekly_also_gets_long_misfire_grace_time():
    svc = SchedulerService()
    svc.add_job_scoped("query", "health", "30 2 * * 0")
    job = svc._scheduler.get_job("infra_brain_query_health")
    assert job.misfire_grace_time == 6 * 3600


@patch("infra_brain.scheduler.get_settings")
@patch("infra_brain.scheduler.dispatch")
@patch("infra_brain.scheduler.try_acquire", return_value=True)
@patch("infra_brain.scheduler.release")
def test_sweep_graph_schedule_translates_dow(
    mock_release, mock_acquire, mock_dispatch, mock_settings
):
    """Regression guard for the sweep-graph registration reintroducing
    TRK-310: start() must run the sweep_graph_schedule's day_of_week through
    _translate_cron_dow() the same as every other CronTrigger built here, not
    pass the raw cron-convention digit straight to APScheduler."""
    mock_settings.return_value.vsphere_full_inventory_enabled = True
    mock_settings.return_value.sweep_graph_enabled = True
    # Intended: Sunday 04:00 UTC (cron day_of_week="0" == Sunday).
    mock_settings.return_value.sweep_graph_schedule = "0 4 * * 0"
    mock_settings.return_value.headless_runner_mcp_token = ""
    svc = SchedulerService()
    svc.start()

    try:
        job = svc._scheduler.get_job("infra_brain_sweep_graph")
        assert job is not None

        # 2026-08-03 is a Monday -- if the bug were present (raw day_of_week
        # passed straight to CronTrigger), the very next candidate would
        # incorrectly be this same day instead of the following Sunday.
        monday = datetime(2026, 8, 3, 0, 0, 0, tzinfo=timezone.utc)
        next_fire = job.trigger.get_next_fire_time(None, monday)
        assert next_fire.strftime("%A") == "Sunday"
        assert next_fire.hour == 4 and next_fire.minute == 0
    finally:
        svc.stop()
