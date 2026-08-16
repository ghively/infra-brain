"""R3 completeness monitor: stale / failed-partial / never-ran / silently-empty
domains raise alerts. Fails pre-2.5: check_collection_health did not exist.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from infra_brain.callbacks import freshness
from infra_brain.db.models import CollectionRun

from tests.support.pg import make_engine


def _engine():
    eng = make_engine()
    return eng


@contextmanager
def _ctx(eng):
    with Session(eng) as s:
        yield s


def _patched(eng):
    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    return patch.object(freshness, "get_session", _get_session)


def _add_run(eng, domain, status, minutes_ago, resources_found=1, detail_rows=0, err=None):
    now = datetime.now(timezone.utc)
    with Session(eng) as s:
        s.add(
            CollectionRun(
                id=uuid.uuid4(),
                domain=domain,
                trigger_type="scheduled",
                status=status,
                started_at=now - timedelta(minutes=minutes_ago + 1),
                finished_at=now - timedelta(minutes=minutes_ago),
                resources_found=resources_found,
                detail_rows_written=detail_rows,
                error_message=err,
            )
        )
        s.commit()


def test_never_started_domain_alerts():
    eng = _engine()
    with _patched(eng):
        alerts = freshness.check_collection_health()
    # every monitored domain alerts on an empty DB — incl. discovery/coverage
    joined = "\n".join(alerts)
    assert "discovery: no collection run has ever started" in joined
    assert "coverage: no collection run has ever started" in joined


def test_partial_latest_run_alerts_with_error_message():
    eng = _engine()
    _add_run(eng, "cicd", "partial", minutes_ago=5, err="project X: 403 Forbidden")
    with _patched(eng):
        alerts = freshness.check_collection_health()
    assert any(a.startswith("cicd: last run partial") and "403" in a for a in alerts)


def test_stale_domain_alerts():
    eng = _engine()
    _add_run(eng, "linux", "completed", minutes_ago=9 * 60)  # window is 8h
    with _patched(eng):
        alerts = freshness.check_collection_health()
    assert any(a.startswith("linux: stale") for a in alerts)


def test_silently_empty_completed_run_alerts():
    # `dns`, not `vuln`: this needs a domain that is actually freshness-
    # monitored, and vuln is retired (AgentSpec.retired) so it is deliberately
    # absent from DOMAIN_EXPECTED_MAX_AGE and can raise no alert at all.
    eng = _engine()
    _add_run(eng, "dns", "completed", minutes_ago=5, resources_found=0, detail_rows=0)
    with _patched(eng):
        alerts = freshness.check_collection_health()
    assert any("dns: last completed run wrote zero rows" in a for a in alerts)


def test_zero_ok_domain_does_not_alert_on_empty():
    eng = _engine()
    _add_run(eng, "compliance", "completed", minutes_ago=5, resources_found=0)
    with _patched(eng):
        alerts = freshness.check_collection_health()
    assert not any("compliance" in a for a in alerts)


def test_healthy_domain_no_alert():
    eng = _engine()
    _add_run(eng, "linux", "completed", minutes_ago=5, resources_found=10)
    with _patched(eng):
        alerts = freshness.check_collection_health()
    assert not any(a.startswith("linux:") for a in alerts)


# ---------------------------------------------------------------------------
# Phase 2 sweep-graph run-state constants (etl/base.py): retry_exhausted is
# failed-equivalent; interrupt_pending is in-progress-equivalent.
# ---------------------------------------------------------------------------


def test_retry_exhausted_latest_run_alerts_like_failed():
    eng = _engine()
    _add_run(eng, "cicd", "retry_exhausted", minutes_ago=5, err="connection refused x3")
    with _patched(eng):
        alerts = freshness.check_collection_health()
    assert any(
        a.startswith("cicd: last run retry_exhausted") and "connection refused" in a for a in alerts
    )


def test_interrupt_pending_latest_run_does_not_alert_as_failed():
    eng = _engine()
    _add_run(eng, "linux", "interrupt_pending", minutes_ago=5)
    with _patched(eng):
        alerts = freshness.check_collection_health()
    # No "last run X" failure-style alert for an in-progress-equivalent status.
    assert not any(a.startswith("linux: last run") for a in alerts)


# ---------------------------------------------------------------------------
# Skipped-status handling: intentionally-unconfigured collectors must not be
# reported as "has never completed a collection run".
# ---------------------------------------------------------------------------


def test_skipped_domain_excluded_from_stale_list():
    """check_freshness: a domain whose only run is 'skipped' is NOT stale."""
    eng = _engine()
    _add_run(eng, "vsphere", "skipped", minutes_ago=10, resources_found=0)
    with _patched(eng):
        stale = freshness.check_freshness()
    assert "vsphere" not in stale


def test_skipped_domain_health_alert_is_informational_not_alarming():
    """check_collection_health: skipped domain gets 'unconfigured (skipped)' message.

    Uses `wazuh` rather than the former `vsphere`: this asserts the treatment of
    a collector that is monitored but currently unconfigured, and vsphere is now
    retired (AgentSpec.retired), which removes it from monitoring entirely.
    Those are different states on purpose — "not set up yet" vs "not coming
    back" — so the test needs a domain still in the first one.
    """
    eng = _engine()
    _add_run(
        eng,
        "wazuh",
        "skipped",
        minutes_ago=10,
        resources_found=0,
        err="WAZUH_URL not configured",
    )
    with _patched(eng):
        alerts = freshness.check_collection_health()
    wazuh_alerts = [a for a in alerts if a.startswith("wazuh:")]
    assert len(wazuh_alerts) == 1
    assert "unconfigured (skipped)" in wazuh_alerts[0]
    assert "WAZUH_URL not configured" in wazuh_alerts[0]
    # Must NOT contain the alarming "has never completed" text
    assert "has never completed" not in wazuh_alerts[0]


def test_skipped_domain_with_no_error_message_still_informational():
    """check_collection_health: skipped run with no error_message shows fallback text."""
    # `dns` is a freshness-monitored collector. It replaced `octopus` here when
    # octopus was retired (AgentSpec.retired) — a retired domain is not
    # monitored at all, so it can produce no alert to assert on.
    eng = _engine()
    _add_run(eng, "dns", "skipped", minutes_ago=5, resources_found=0, err=None)
    with _patched(eng):
        alerts = freshness.check_collection_health()
    dns_alerts = [a for a in alerts if a.startswith("dns:")]
    assert len(dns_alerts) == 1
    assert "unconfigured (skipped)" in dns_alerts[0]
    assert "no reason recorded" in dns_alerts[0]


def test_old_skip_row_does_not_mask_newer_failures_in_stale_list():
    """check_freshness: a collector configured *after* an old 'skipped' run must
    not stay excluded from the stale list once it starts failing for real.

    Regression: the skip lookup used to find the most recent *skipped* run
    rather than the most recent run, so wazuh's 2026-08-02 skip row kept
    masking 262 consecutive 401 auth failures as "intentionally unconfigured".
    """
    eng = _engine()
    _add_run(
        eng,
        "wazuh",
        "skipped",
        minutes_ago=7 * 24 * 60,
        resources_found=0,
        err="wazuh_url/wazuh_username/wazuh_password not configured",
    )
    _add_run(
        eng,
        "wazuh",
        "failed",
        minutes_ago=5,
        resources_found=0,
        err="Wazuh authentication failed: 401 Unauthorized",
    )
    with _patched(eng):
        stale = freshness.check_freshness()
    assert "wazuh" in stale


def test_old_skip_row_does_not_mask_newer_failures_in_health_alerts():
    """check_collection_health: the same stale-skip-row case must surface the
    real failure instead of the benign 'unconfigured (skipped)' message."""
    eng = _engine()
    _add_run(
        eng,
        "wazuh",
        "skipped",
        minutes_ago=7 * 24 * 60,
        resources_found=0,
        err="wazuh_url/wazuh_username/wazuh_password not configured",
    )
    _add_run(
        eng,
        "wazuh",
        "failed",
        minutes_ago=5,
        resources_found=0,
        err="Wazuh authentication failed: 401 Unauthorized",
    )
    with _patched(eng):
        alerts = freshness.check_collection_health()
    wazuh_alerts = [a for a in alerts if "wazuh" in a]
    joined = "\n".join(wazuh_alerts)
    assert "401 Unauthorized" in joined
    assert "unconfigured (skipped)" not in joined


def test_truly_broken_domain_still_alerts_has_never_completed():
    """check_collection_health: a failed-only domain keeps the alarming message.

    Uses ``linux`` because DOMAIN_EXPECTED_MAX_AGE is registry-derived and only
    covers dispatchable domains — ``windows`` was retired (TRK-278 / GitLab #140)
    and is therefore no longer monitored at all.
    """
    eng = _engine()
    _add_run(eng, "linux", "failed", minutes_ago=5, resources_found=0, err="connection refused")
    with _patched(eng):
        alerts = freshness.check_collection_health()
    linux_alerts = [a for a in alerts if "linux" in a]
    # Both the failed-run alert AND the never-completed alert should fire
    assert any("last run failed" in a for a in linux_alerts)
    assert any("has never completed" in a for a in linux_alerts)


# ---------------------------------------------------------------------------
# OB-1: scheduler dead-man switch — record_scheduler_heartbeat() /
# check_scheduler_deadman()
# ---------------------------------------------------------------------------


def test_deadman_alerts_when_no_heartbeat_ever_recorded():
    eng = _engine()
    with _patched(eng):
        alert = freshness.check_scheduler_deadman()
    assert alert is not None
    assert "no heartbeat has ever been recorded" in alert


def test_deadman_no_alert_right_after_heartbeat_recorded():
    eng = _engine()
    with _patched(eng):
        freshness.record_scheduler_heartbeat()
        alert = freshness.check_scheduler_deadman(max_age=timedelta(hours=2))
    assert alert is None


def test_deadman_alerts_when_heartbeat_stale():
    eng = _engine()
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    with Session(eng) as s:
        from infra_brain.db.models import AgentConfigSetting

        s.add(AgentConfigSetting(key="scheduler_heartbeat_last_run", value=stale_ts))
        s.commit()

    with _patched(eng):
        alert = freshness.check_scheduler_deadman(max_age=timedelta(hours=2))
    assert alert is not None
    assert "stale" in alert


def test_record_scheduler_heartbeat_updates_in_place_not_duplicated():
    eng = _engine()
    with _patched(eng):
        freshness.record_scheduler_heartbeat()
        freshness.record_scheduler_heartbeat()

    with Session(eng) as s:
        from infra_brain.db.models import AgentConfigSetting

        rows = s.query(AgentConfigSetting).filter_by(key="scheduler_heartbeat_last_run").all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# TRK-120: daily LLM token aggregate (cost visibility, no alerting pipeline)
# ---------------------------------------------------------------------------


def _add_decision_row(eng, token_count, hours_ago=0):
    from infra_brain.db.models import AgentDecisionLog

    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    with Session(eng) as s:
        s.add(
            AgentDecisionLog(
                run_id=uuid.uuid4(),
                agent="RootCauseAgent",
                domain="rootcause",
                iteration=0,
                reasoning_text="",
                tools_chosen=[],
                decision_summary="",
                token_count=token_count,
                ts=ts,
            )
        )
        s.commit()


def test_sum_llm_tokens_today_sums_only_current_day():
    eng = _engine()
    # Both "today" rows at now (hours_ago=0) to avoid a midnight-boundary race.
    _add_decision_row(eng, 100, hours_ago=0)
    _add_decision_row(eng, 50, hours_ago=0)
    _add_decision_row(eng, 999, hours_ago=48)  # two days ago — always excluded
    _add_decision_row(eng, None, hours_ago=0)  # null token_count — coalesced to 0
    with _patched(eng):
        total = freshness.sum_llm_tokens_today()
    # 100 + 50 from today; the 48h-ago row and the NULL row do not add.
    assert total == 150


def test_sum_llm_tokens_today_empty_is_zero():
    eng = _engine()
    with _patched(eng):
        assert freshness.sum_llm_tokens_today() == 0


# ---------------------------------------------------------------------------
# TRK-241 (GitLab #110): consecutive-failure streak tracking + escalation.
# Alert-only — asserts no retry/remediation side effect is introduced.
# ---------------------------------------------------------------------------


def test_streak_below_threshold_does_not_escalate():
    """Two unhealthy passes in a row (default threshold 3) — no ESCALATED marker."""
    eng = _engine()
    _add_run(eng, "cicd", "failed", minutes_ago=5, err="boom")
    with _patched(eng):
        freshness.check_collection_health()
        alerts = freshness.check_collection_health()
    cicd_alerts = [a for a in alerts if "cicd" in a]
    assert cicd_alerts
    assert not any(a.startswith("ESCALATED") for a in cicd_alerts)


def test_streak_crossing_threshold_escalates():
    """Three consecutive unhealthy passes (default threshold) escalates the alert."""
    eng = _engine()
    _add_run(eng, "cicd", "failed", minutes_ago=5, err="boom")
    with _patched(eng):
        freshness.check_collection_health()
        freshness.check_collection_health()
        alerts = freshness.check_collection_health()
    cicd_alerts = [a for a in alerts if "cicd" in a]
    assert any(
        a.startswith("ESCALATED (3x in a row)") and "cicd: last run failed" in a
        for a in cicd_alerts
    )


def test_streak_keeps_escalating_past_threshold():
    """A 4th consecutive unhealthy pass still escalates, with an updated count."""
    eng = _engine()
    _add_run(eng, "cicd", "failed", minutes_ago=5, err="boom")
    with _patched(eng):
        for _ in range(3):
            freshness.check_collection_health()
        alerts = freshness.check_collection_health()
    cicd_alerts = [a for a in alerts if "cicd" in a]
    assert any(a.startswith("ESCALATED (4x in a row)") for a in cicd_alerts)


def test_streak_resets_when_domain_recovers():
    """A healthy pass breaks the streak; a subsequent failure starts back at 1."""
    eng = _engine()
    with _patched(eng):
        # 3 consecutive failing passes would normally escalate...
        for _ in range(3):
            with Session(eng) as s:
                s.query(CollectionRun).filter_by(domain="cicd").delete()
                s.commit()
            _add_run(eng, "cicd", "failed", minutes_ago=5, err="boom")
            freshness.check_collection_health()

        # ...but a healthy pass in between resets the streak.
        with Session(eng) as s:
            s.query(CollectionRun).filter_by(domain="cicd").delete()
            s.commit()
        _add_run(eng, "cicd", "completed", minutes_ago=5, resources_found=10)
        healthy_alerts = freshness.check_collection_health()
        assert not any("cicd" in a for a in healthy_alerts)

        # Next failure starts the streak over at 1, not escalated.
        with Session(eng) as s:
            s.query(CollectionRun).filter_by(domain="cicd").delete()
            s.commit()
        _add_run(eng, "cicd", "failed", minutes_ago=5, err="boom again")
        alerts = freshness.check_collection_health()
    cicd_alerts = [a for a in alerts if "cicd" in a]
    assert cicd_alerts
    assert not any(a.startswith("ESCALATED") for a in cicd_alerts)


def test_skipped_domain_never_counts_toward_streak():
    """An intentionally-skipped (unconfigured) domain never escalates, no matter
    how many passes go by — it's informational, not a failure signal."""
    # `wazuh`, not the former `vsphere` — see
    # test_skipped_domain_health_alert_is_informational_not_alarming.
    eng = _engine()
    _add_run(eng, "wazuh", "skipped", minutes_ago=10, err="WAZUH_URL not configured")
    with _patched(eng):
        for _ in range(5):
            alerts = freshness.check_collection_health()
    wazuh_alerts = [a for a in alerts if "wazuh" in a]
    assert wazuh_alerts
    assert not any(a.startswith("ESCALATED") for a in wazuh_alerts)


# ---------------------------------------------------------------------------
# check_freshness tz-naive normalization: SQLite returns naive datetimes even
# for DateTime(timezone=True) columns, so the staleness subtraction against a
# tz-aware `now` must normalize first. Pre-fix these raised
# "TypeError: can't subtract offset-naive and offset-aware datetimes".
# ---------------------------------------------------------------------------


def test_check_freshness_stale_domain_with_naive_finished_at():
    """A genuinely stale domain is reported, not lost to a TypeError."""
    eng = _engine()
    # linux's max_staleness is measured in hours; 30 days is unambiguously stale.
    _add_run(eng, "linux", "completed", minutes_ago=60 * 24 * 30, resources_found=5)
    with Session(eng) as s:
        stored = s.query(CollectionRun).filter_by(domain="linux").one()
        # Guard the premise: this backend really does hand back a naive
        # datetime, which is what made the unnormalized subtraction raise.
        # Only SQLite does — PostgreSQL honours DateTime(timezone=True) and
        # returns an aware value, so under PG_GATE_DSN this file instead
        # proves the normalization is a no-op on already-aware timestamps.
        if eng.dialect.name == "sqlite":
            assert stored.finished_at.tzinfo is None

    with _patched(eng):
        stale = freshness.check_freshness()

    assert "linux" in stale


def test_check_freshness_fresh_domain_with_naive_finished_at_not_stale():
    """A just-collected domain is not stale — normalization must not skew the
    comparison (e.g. by shifting a naive timestamp into a stale window)."""
    eng = _engine()
    _add_run(eng, "linux", "completed", minutes_ago=1, resources_found=5)
    with _patched(eng):
        stale = freshness.check_freshness()
    assert "linux" not in stale


def test_currently_skipped_domain_with_older_success_is_not_stale():
    """A switched-off collector must not age into a stale alert.

    Regression: the skip check only ran when NO successful run existed. A
    domain that used to work and now skips (its dependency was removed) still
    had old `completed` rows, so the staleness branch kept firing forever —
    unclearable except by configuring an integration nobody asked for. Observed
    the moment saas_inventory began skipping correctly instead of returning an
    empty success: the "silently empty?" noise simply became "stale" noise.
    """
    eng = _engine()
    # Succeeded two days ago, then started skipping.
    _add_run(eng, "saas_inventory", "completed", minutes_ago=2 * 24 * 60, resources_found=5)
    _add_run(
        eng,
        "saas_inventory",
        "skipped",
        minutes_ago=10,
        resources_found=0,
        err="saas_admin_url not configured",
    )

    with _patched(eng):
        alerts = freshness.check_collection_health()
        stale = freshness.check_freshness()

    assert "saas_inventory" not in stale
    mine = [a for a in alerts if "saas_inventory" in a]
    joined = " ".join(mine)
    assert "stale" not in joined, f"a skipping collector must not be stale: {mine}"
    assert "unconfigured (skipped)" in joined
    # Informational only — must not reach the ops webhook.
    assert all(freshness.is_informational_alert(a) for a in mine)


def test_still_stale_when_domain_is_not_currently_skipping():
    """The staleness alert itself must survive — only the skip case is exempt."""
    eng = _engine()
    _add_run(eng, "saas_inventory", "completed", minutes_ago=2 * 24 * 60, resources_found=5)

    with _patched(eng):
        stale = freshness.check_freshness()

    assert "saas_inventory" in stale
