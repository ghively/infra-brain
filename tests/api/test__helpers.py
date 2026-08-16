"""Unit tests for infra_brain.api._helpers.

Covers _sla_string's day-counting arithmetic, which drives the SLA badge
rendered by both vuln.py::list_vulns and hosts.py::get_host_vulns.
"""

from datetime import datetime, timedelta, timezone

from infra_brain.api._helpers import _sla_string


def test_sla_string_none_is_placeholder():
    assert _sla_string(None) == "—"


def test_sla_string_overdue_by_one_hour_rounds_down_to_zero_days():
    # Regression test: floor division on the negative timedelta used to
    # compute `-3600 // 86400 == -1`, misreporting "overdue 1d" for a vuln
    # that is only 1 hour past its SLA due date.
    now = datetime.now(timezone.utc)
    due = now - timedelta(hours=1)
    assert _sla_string(due) == "overdue 0d"


def test_sla_string_overdue_by_twenty_five_hours_is_one_day():
    # 25 hours overdue used to floor to -2 ("overdue 2d") instead of the
    # correct 1 full day elapsed.
    now = datetime.now(timezone.utc)
    due = now - timedelta(hours=25)
    assert _sla_string(due) == "overdue 1d"


def test_sla_string_overdue_by_exactly_one_day():
    now = datetime.now(timezone.utc)
    due = now - timedelta(days=1)
    assert _sla_string(due) == "overdue 1d"


def test_sla_string_due_in_future_truncates_partial_day():
    now = datetime.now(timezone.utc)
    due = now + timedelta(hours=25)
    assert _sla_string(due) == "due in 1d"


def test_sla_string_naive_datetime_treated_as_utc():
    # due.tzinfo is None branch: should still compute correctly rather than
    # raising on tz-aware/naive subtraction.
    now = datetime.now(timezone.utc)
    due_naive = (now - timedelta(hours=1)).replace(tzinfo=None)
    assert _sla_string(due_naive) == "overdue 0d"
