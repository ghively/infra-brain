"""``get_utilization_forecast`` MCP tool — GitLab #131's forecast/lead-time
detection half (the correlation half shipped separately as
``get_recent_changes``, see ``tests/test_mcp_batch_j.py``).

Reuses the per-sweep ``snapshots`` table (append-only via ``collected_at``,
written every collection run by ``etl/base.py``'s ``_write_snapshot``) rather
than a new table — these tests seed that existing table directly.
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain import mcp_server
from infra_brain.db.models import CollectionRun, Resource, Snapshot

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextlib.contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.mcp_server.get_session", _get_session):
        yield engine


def _seed_resource(engine, name: str, domain: str = "vsphere") -> Resource:
    with Session(engine) as s:
        r = Resource(domain=domain, type="vm", name=name, source="VsphereAgent")
        s.add(r)
        s.commit()
        s.refresh(r)
        return r


def _seed_snapshots(engine, resource_id: uuid.UUID, series: list[tuple[datetime, dict]]):
    with Session(engine) as s:
        run = CollectionRun(domain="vsphere", trigger_type="scheduled")
        s.add(run)
        s.flush()
        for ts, data in series:
            s.add(
                Snapshot(
                    resource_id=resource_id,
                    run_id=run.id,
                    snapshot=data,
                    collected_at=ts,
                )
            )
        s.commit()


def test_forecast_rising_trend_projects_crossing(patched_session):
    r = _seed_resource(patched_session, "esxi-vm-01")
    now = datetime.now(UTC)
    series = [
        (now - timedelta(hours=72), {"memory_usage_mb": 1000}),
        (now - timedelta(hours=48), {"memory_usage_mb": 2000}),
        (now - timedelta(hours=24), {"memory_usage_mb": 3000}),
        (now, {"memory_usage_mb": 4000}),
    ]
    _seed_snapshots(patched_session, r.id, series)

    result = mcp_server.get_utilization_forecast(
        "esxi-vm-01", "memory_usage_mb", threshold=8000.0, hours=200
    )

    assert result["status"] == "forecast"
    assert result["resource"] == "esxi-vm-01"
    assert result["samples_used"] == 4
    # slope is ~1000 MB / 24h -> ~24h/1000MB-per-hour; crossing 8000 from 4000
    # at ~+1000/24h needs ~96h more.
    assert result["hours_until_threshold"] == pytest.approx(96, rel=0.05)
    assert result["forecast_at"] is not None


def test_forecast_insufficient_data(patched_session):
    r = _seed_resource(patched_session, "quiet-vm")
    now = datetime.now(UTC)
    _seed_snapshots(patched_session, r.id, [(now, {"memory_usage_mb": 1000})])

    result = mcp_server.get_utilization_forecast("quiet-vm", "memory_usage_mb", threshold=8000.0)

    assert result["status"] == "insufficient_data"
    assert result["samples_found"] == 1


def test_forecast_flat_trend_no_crossing(patched_session):
    r = _seed_resource(patched_session, "steady-vm")
    now = datetime.now(UTC)
    series = [
        (now - timedelta(hours=48), {"memory_usage_mb": 2000}),
        (now - timedelta(hours=24), {"memory_usage_mb": 2000}),
        (now, {"memory_usage_mb": 2000}),
    ]
    _seed_snapshots(patched_session, r.id, series)

    result = mcp_server.get_utilization_forecast(
        "steady-vm", "memory_usage_mb", threshold=8000.0, hours=200
    )

    assert result["status"] == "flat_or_diverging"
    assert "forecast_at" not in result


def test_forecast_declining_trend_moving_away_from_threshold(patched_session):
    """Memory usage is dropping, threshold is an upper bound above current
    value moving further away — must not fabricate a forward crossing."""
    r = _seed_resource(patched_session, "shrinking-vm")
    now = datetime.now(UTC)
    series = [
        (now - timedelta(hours=48), {"memory_usage_mb": 4000}),
        (now - timedelta(hours=24), {"memory_usage_mb": 3000}),
        (now, {"memory_usage_mb": 2000}),
    ]
    _seed_snapshots(patched_session, r.id, series)

    result = mcp_server.get_utilization_forecast(
        "shrinking-vm", "memory_usage_mb", threshold=8000.0, hours=200
    )

    assert result["status"] == "flat_or_diverging"


def test_forecast_not_found(patched_session):
    result = mcp_server.get_utilization_forecast(
        "does-not-exist", "memory_usage_mb", threshold=8000.0
    )
    assert result["status"] == "not_found"


def test_forecast_ambiguous_match(patched_session):
    _seed_resource(patched_session, "app-vm-01")
    _seed_resource(patched_session, "app-vm-02")

    result = mcp_server.get_utilization_forecast("app-vm", "memory_usage_mb", threshold=8000.0)

    assert result["status"] == "ambiguous"
    assert set(result["candidates"]) == {"app-vm-01", "app-vm-02"}


def test_forecast_exact_match_short_circuits_decoy_substring(patched_session):
    """TRK-195/#179: a decoy resource whose name merely CONTAINS the query
    (e.g. a "stale_drift:<host>" row) must not shadow the real exact-match
    host behind an unresolvable "ambiguous" result."""
    real = _seed_resource(patched_session, "web-01")
    _seed_resource(patched_session, "stale_drift:web-01")
    now = datetime.now(UTC)
    _seed_snapshots(
        patched_session,
        real.id,
        [
            (now - timedelta(hours=24), {"cpu_usage_mhz": 1000}),
            (now, {"cpu_usage_mhz": 2000}),
        ],
    )

    result = mcp_server.get_utilization_forecast(
        "web-01", "cpu_usage_mhz", threshold=5000.0, hours=200, min_points=2
    )

    assert result["status"] == "forecast"


def test_forecast_domain_filter_disambiguates(patched_session):
    v = _seed_resource(patched_session, "shared-name", domain="vsphere")
    _seed_resource(patched_session, "shared-name", domain="cloud")
    now = datetime.now(UTC)
    series = [
        (now - timedelta(hours=24), {"cpu_usage_mhz": 1000}),
        (now, {"cpu_usage_mhz": 2000}),
    ]
    _seed_snapshots(patched_session, v.id, series)

    result = mcp_server.get_utilization_forecast(
        "shared-name",
        "cpu_usage_mhz",
        threshold=5000.0,
        hours=200,
        domain="vsphere",
        min_points=2,
    )

    assert result["status"] == "forecast"


def test_forecast_ignores_non_numeric_and_missing_metric_values(patched_session):
    r = _seed_resource(patched_session, "mixed-vm")
    now = datetime.now(UTC)
    series = [
        (now - timedelta(hours=48), {"memory_usage_mb": 1000}),
        (now - timedelta(hours=24), {"memory_usage_mb": None}),
        (now - timedelta(hours=12), {"other_field": "irrelevant"}),
        (now, {"memory_usage_mb": 2000}),
    ]
    _seed_snapshots(patched_session, r.id, series)

    result = mcp_server.get_utilization_forecast(
        "mixed-vm", "memory_usage_mb", threshold=8000.0, hours=200, min_points=2
    )

    assert result["status"] == "forecast"
    assert result["samples_used"] == 2
