"""Batch I MCP tools — governance/compliance read-only query tools (issue #53)."""

import contextlib
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain import mcp_server
from infra_brain.db.models import (
    CollectionRun,
    ComplianceViolation,
    ConfluencePage,
    DriftEvent,
    JiraTicket,
    Resource,
    ScanPoint,
)
from infra_brain.etl.spec import AgentSpec, Tier

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


def _seed(engine, *objs):
    with Session(engine) as s:
        s.add_all(objs)
        s.commit()


def test_get_compliance_violations_success(patched_session):
    _seed(
        patched_session,
        ComplianceViolation(rule="PCI-2.2", severity="high", host="db-1", status="open"),
        ComplianceViolation(rule="PCI-8.1", severity="medium", host="web-1", status="open"),
    )
    rows = mcp_server.get_compliance_violations()
    assert len(rows) == 2
    assert {r["rule"] for r in rows} == {"PCI-2.2", "PCI-8.1"}
    assert {r["host"] for r in rows} == {"db-1", "web-1"}


def test_get_compliance_violations_filters(patched_session):
    _seed(
        patched_session,
        ComplianceViolation(rule="PCI-2.2", severity="high", host="db-1", status="open"),
        ComplianceViolation(rule="PCI-2.2", severity="high", host="db-2", status="resolved"),
        ComplianceViolation(rule="PCI-8.1", severity="medium", host="web-1", status="open"),
    )
    rows = mcp_server.get_compliance_violations(status="open", severity="high", rule="PCI-2.2")
    assert [r["host"] for r in rows] == ["db-1"]


def test_get_compliance_violations_empty(patched_session):
    assert mcp_server.get_compliance_violations() == []


def test_get_drift_trend_success(patched_session):
    with Session(patched_session) as s:
        r = Resource(domain="linux", type="host", name="lnx-1", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add_all(
            [
                DriftEvent(
                    resource_id=r.id,
                    drift_type="config_drift",
                    field="pkg",
                    detected_at=datetime.now(UTC),
                    status="open",
                ),
                DriftEvent(
                    resource_id=r.id,
                    drift_type="config_drift",
                    field="svc",
                    detected_at=datetime.now(UTC),
                    status="open",
                ),
            ]
        )
        s.commit()
    out = mcp_server.get_drift_trend(days=7)
    assert out["total"] == 2
    assert out["days"] == 7
    assert out["domain_filter"] == ""
    assert out["points"] and out["points"][0]["domain"] == "linux"
    assert out["points"][0]["count"] == 2


def test_get_drift_trend_domain_filter(patched_session):
    with Session(patched_session) as s:
        r1 = Resource(domain="linux", type="host", name="lnx-1", source="LinuxAgent")
        r2 = Resource(domain="windows", type="host", name="win-1", source="WindowsAgent")
        s.add_all([r1, r2])
        s.flush()
        s.add_all(
            [
                DriftEvent(
                    resource_id=r1.id,
                    drift_type="config_drift",
                    field="pkg",
                    detected_at=datetime.now(UTC),
                    status="open",
                ),
                DriftEvent(
                    resource_id=r2.id,
                    drift_type="config_drift",
                    field="reg",
                    detected_at=datetime.now(UTC),
                    status="open",
                ),
            ]
        )
        s.commit()
    out = mcp_server.get_drift_trend(days=7, domain="windows")
    assert out["total"] == 1
    assert all(p["domain"] == "windows" for p in out["points"])


def test_get_drift_trend_empty(patched_session):
    out = mcp_server.get_drift_trend()
    assert out["total"] == 0
    assert out["points"] == []


def test_get_notifications_merges_sources(patched_session):
    # JiraTicket.drift_event_id is a NOT-NULL FK to drift_events, and DriftEvent
    # requires a resource, so build Resource -> DriftEvent -> JiraTicket in order.
    with Session(patched_session) as s:
        r = Resource(domain="linux", type="host", name="lnx-1", source="LinuxAgent")
        s.add(r)
        s.flush()
        ev = DriftEvent(
            resource_id=r.id,
            drift_type="config_drift",
            field="pkg",
            detected_at=datetime.now(UTC),
            status="open",
        )
        s.add(ev)
        s.flush()
        s.add(JiraTicket(drift_event_id=ev.id, jira_key="INFRA-42"))
        s.add(ConfluencePage(domain="linux", page_id="12345"))
        s.commit()
    rows = mcp_server.get_notifications()
    types = {r["type"] for r in rows}
    assert types == {"jira", "confluence"}
    jira = next(r for r in rows if r["type"] == "jira")
    assert jira["target"] == "INFRA-42"


def test_get_notifications_type_filter(patched_session):
    _seed(patched_session, ConfluencePage(domain="linux", page_id="12345"))
    rows = mcp_server.get_notifications(type="confluence")
    assert [r["type"] for r in rows] == ["confluence"]
    assert rows[0]["target"] == "12345"


def test_get_notifications_empty(patched_session):
    assert mcp_server.get_notifications() == []


def test_get_agent_roster_success(patched_session):
    # Isolate the registry so the test does not import the whole agent package.
    linux_agent = MagicMock()
    linux_agent.__name__ = "LinuxAgent"
    linux_agent.spec = AgentSpec(
        domain="linux",
        tier=Tier.COLLECTOR,
        schedule="0 */6 * * *",  # normally scheduled
        max_staleness=None,
    )
    fake_registry = {"linux": linux_agent}
    with Session(patched_session) as s:
        s.add(
            CollectionRun(
                domain="linux", trigger_type="scheduled", status="completed", resources_found=12
            )
        )
        s.commit()
    with patch("infra_brain.supervisor.AGENT_REGISTRY", fake_registry):
        rows = mcp_server.get_agent_roster()
    assert len(rows) == 1
    assert rows[0]["domain"] == "linux"
    assert rows[0]["agent"] == "LinuxAgent"
    assert rows[0]["last_status"] == "completed"
    assert rows[0]["hook_driven"] is False


def test_get_agent_roster_no_runs(patched_session):
    windows_agent = MagicMock()
    windows_agent.__name__ = "WindowsAgent"
    windows_agent.spec = AgentSpec(
        domain="windows",
        tier=Tier.COLLECTOR,
        schedule="0 */8 * * *",  # normally scheduled
        max_staleness=None,
    )
    fake_registry = {"windows": windows_agent}
    with patch("infra_brain.supervisor.AGENT_REGISTRY", fake_registry):
        rows = mcp_server.get_agent_roster()
    assert rows[0]["domain"] == "windows"
    assert rows[0]["last_status"] is None
    assert rows[0]["last_run"] is None
    assert rows[0]["hook_driven"] is False


def test_get_sweep_status_latest_per_domain(patched_session):
    now = datetime.now(UTC)
    with Session(patched_session) as s:
        s.add_all(
            [
                CollectionRun(
                    domain="linux",
                    trigger_type="scheduled",
                    status="failed",
                    started_at=now - timedelta(hours=2),
                ),
                CollectionRun(
                    domain="linux",
                    trigger_type="scheduled",
                    status="completed",
                    started_at=now - timedelta(minutes=5),
                ),
                CollectionRun(
                    domain="windows",
                    trigger_type="scheduled",
                    status="completed",
                    started_at=now - timedelta(hours=1),
                ),
            ]
        )
        s.commit()
    rows = mcp_server.get_sweep_status()
    by_domain = {r["domain"]: r for r in rows}
    assert set(by_domain) == {"linux", "windows"}
    # linux's LATEST run is the completed one, not the earlier failed one.
    assert by_domain["linux"]["status"] == "completed"


def test_get_sweep_status_empty(patched_session):
    assert mcp_server.get_sweep_status() == []


def test_get_scan_schedule_success(patched_session):
    _seed(
        patched_session,
        ScanPoint(domain="linux", method="ssh", endpoint="10.0.0.1", schedule="0 4 * * *"),
        ScanPoint(domain="cicd", method="api", endpoint="gitlab", schedule="*/30 * * * *"),
    )
    rows = mcp_server.get_scan_schedule()
    assert len(rows) == 2
    assert {r["domain"] for r in rows} == {"linux", "cicd"}


def test_get_scan_schedule_domain_filter(patched_session):
    _seed(
        patched_session,
        ScanPoint(domain="linux", method="ssh", endpoint="10.0.0.1", schedule="0 4 * * *"),
        ScanPoint(domain="cicd", method="api", endpoint="gitlab", schedule="*/30 * * * *"),
    )
    rows = mcp_server.get_scan_schedule(domain="cicd")
    assert [r["domain"] for r in rows] == ["cicd"]


def test_get_scan_schedule_empty(patched_session):
    assert mcp_server.get_scan_schedule() == []
