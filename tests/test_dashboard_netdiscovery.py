"""Tests for the network-discovery / shadow-IT dashboard endpoint
(UI Batch 8 / GitLab #64).

Mirrors test_dashboard_fleet_software_cve.py's conventions: in-memory
SQLite, ORM schema via Base.metadata.create_all, get_session patched to the
test engine, auth-off via INFRA_BRAIN_DEV=1.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import NetDiscoveryHost, NetDiscoveryService

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def client(engine, monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.netdiscovery import netdiscovery_router

    app = FastAPI()
    app.include_router(netdiscovery_router)
    with patch("infra_brain.api.routers.netdiscovery.get_session", _get_session):
        yield TestClient(app)


def _seed(engine):
    with Session(engine) as s:
        known = NetDiscoveryHost(
            ip="10.0.0.5", mac="aa:bb:cc:00:00:01", hostname="known-printer",
            responded=True, discovery_tier="tier1", is_known=True, is_shadow_it=False,
            threat_level="none", zone="corp",
        )
        shadow = NetDiscoveryHost(
            ip="10.0.0.66", mac="aa:bb:cc:00:00:02", hostname="",
            responded=True, discovery_tier="tier2", is_known=False, is_shadow_it=True,
            threat_level="high", zone="corp",
        )
        s.add_all([known, shadow])
        s.flush()
        s.add(NetDiscoveryService(host_id=shadow.id, port=23, proto="tcp", service="telnet", is_dangerous=True))
        s.add(NetDiscoveryService(host_id=shadow.id, port=80, proto="tcp", service="http", banner="Apache/2.4"))
        s.commit()
        return str(known.id), str(shadow.id)


def test_lists_all_hosts_with_summary(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/netdiscovery/hosts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["summary"]["total_hosts"] == 2
    assert body["summary"]["shadow_it_count"] == 1
    assert body["summary"]["known_count"] == 1
    assert body["summary"]["by_threat_level"] == {"none": 1, "high": 1}


def test_shadow_host_carries_nested_services(client, engine):
    known_id, shadow_id = _seed(engine)
    resp = client.get("/api/dashboard/netdiscovery/hosts")
    body = resp.json()
    shadow_row = next(r for r in body["items"] if r["id"] == shadow_id)
    ports = {s["port"] for s in shadow_row["services"]}
    assert ports == {23, 80}
    telnet = next(s for s in shadow_row["services"] if s["port"] == 23)
    assert telnet["is_dangerous"] is True


def test_known_host_has_empty_services(client, engine):
    known_id, _ = _seed(engine)
    resp = client.get("/api/dashboard/netdiscovery/hosts")
    body = resp.json()
    known_row = next(r for r in body["items"] if r["id"] == known_id)
    assert known_row["services"] == []


def test_filter_by_is_shadow_it(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/netdiscovery/hosts?is_shadow_it=true")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["is_shadow_it"] is True


def test_filter_by_threat_level(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/netdiscovery/hosts?threat_level=high")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["threat_level"] == "high"


def test_threat_level_sort_is_severity_ranked_not_lexicographic(client, engine):
    """Regression for TRK: threat_level.desc() sorted lexicographically, which
    for the {none, low, med, high} string set produces "none > med > low >
    high" — the OPPOSITE of severity order — burying the worst hosts on the
    last page. Seed in scrambled insertion order and assert the response is
    strictly high -> med -> low -> none, with last_seen as tiebreak within a
    threat level (most recent first).
    """
    base_time = datetime(2026, 7, 1, tzinfo=timezone.utc)
    with Session(engine) as s:
        # Insertion order deliberately scrambled and NOT alphabetical/severity
        # order, so a passing test can't be an artifact of insertion order.
        low = NetDiscoveryHost(
            ip="192.0.2.12", threat_level="low", is_known=False, is_shadow_it=True,
            discovery_tier="tier1", last_seen=base_time,
        )
        none_ = NetDiscoveryHost(
            ip="192.0.2.14", threat_level="none", is_known=True, is_shadow_it=False,
            discovery_tier="passive", last_seen=base_time,
        )
        high_older = NetDiscoveryHost(
            ip="192.0.2.15", threat_level="high", is_known=False, is_shadow_it=True,
            discovery_tier="tier2", last_seen=base_time,
        )
        med = NetDiscoveryHost(
            ip="192.0.2.16", threat_level="med", is_known=False, is_shadow_it=True,
            discovery_tier="tier1", last_seen=base_time,
        )
        # Second "high" host, seen more recently, to verify the last_seen
        # tiebreak still applies within a shared threat level.
        high_newer = NetDiscoveryHost(
            ip="192.0.2.17", threat_level="high", is_known=False, is_shadow_it=True,
            discovery_tier="tier2", last_seen=base_time + timedelta(hours=1),
        )
        s.add_all([low, none_, high_older, med, high_newer])
        s.commit()

    resp = client.get("/api/dashboard/netdiscovery/hosts")
    assert resp.status_code == 200
    body = resp.json()
    ips_in_order = [row["ip"] for row in body["items"]]
    assert ips_in_order == [
        "192.0.2.17",  # high, newer last_seen
        "192.0.2.15",  # high, older last_seen
        "192.0.2.16",  # med
        "192.0.2.12",  # low
        "192.0.2.14",  # none
    ]


def test_empty_when_no_hosts_discovered(client, engine):
    resp = client.get("/api/dashboard/netdiscovery/hosts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["summary"]["total_hosts"] == 0
