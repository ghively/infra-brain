"""Tests for GET /api/dashboard/resources/{resource_id}/posture (UI-1/#57).

Mirrors the existing dashboard_api test conventions: in-memory SQLite, the ORM
schema via Base.metadata.create_all, get_session patched to the test engine —
same shape as tests/test_dashboard_fleet_software_cve.py.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    HostCertificate,
    HostFirewallRule,
    HostSecurityPosture,
    HostShare,
    Resource,
    WindowsLocalGroupMember,
    WindowsLocalUser,
)

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

    from infra_brain.api.routers.hosts import resources_router

    app = FastAPI()
    app.include_router(resources_router)
    with patch("infra_brain.api.routers.hosts.get_session", _get_session):
        yield TestClient(app)


def _seed_resource(engine) -> uuid.UUID:
    with Session(engine) as s:
        r = Resource(domain="windows", type="host", name="win-app-01", source="WindowsAgent")
        s.add(r)
        s.commit()
        return r.id


def test_posture_returns_all_five_categories(client, engine):
    rid = _seed_resource(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        s.add(
            HostCertificate(
                resource_id=rid,
                store="LocalMachine/My",
                subject="CN=win-app-01",
                issuer="CN=Internal CA",
                thumbprint="ABC123",
                not_after=now,
                days_until_expiry=10,
                is_expired=False,
            )
        )
        s.add(
            HostSecurityPosture(
                resource_id=rid,
                firewall_enabled=True,
                av_enabled=True,
                rdp_enabled=False,
                uac_enabled=True,
            )
        )
        s.add(
            HostFirewallRule(
                resource_id=rid,
                chain="INPUT",
                rule_text="-A INPUT -p tcp --dport 22 -j ACCEPT",
                action="ACCEPT",
                source="iptables",
            )
        )
        s.add(HostShare(resource_id=rid, share_type="smb", name="C$", path="C:\\"))
        s.add(WindowsLocalUser(resource_id=rid, username="Administrator", is_admin=True))
        s.add(WindowsLocalGroupMember(resource_id=rid, group_name="Administrators", member_name="Administrator"))
        s.commit()

    resp = client.get(f"/api/dashboard/resources/{rid}/posture")
    assert resp.status_code == 200
    data = resp.json()
    assert data["hostname"] == "win-app-01"
    assert len(data["certificates"]) == 1
    assert data["certificates"][0]["thumbprint"] == "ABC123"
    assert data["security_posture"]["firewall_enabled"] is True
    assert len(data["firewall_rules"]) == 1
    assert len(data["shares"]) == 1
    assert data["shares"][0]["share_type"] == "smb"
    assert len(data["local_users"]) == 1
    assert data["local_users"][0]["is_admin"] is True
    assert len(data["local_group_members"]) == 1


def test_posture_empty_when_no_posture_rows(client, engine):
    """A resource that exists but has zero posture rows in any of the five
    tables returns 200 with empty lists / null summary, NOT a 404 — unlike
    get_linux_detail (which 404s on a missing LinuxHost root row), posture
    data has no single required root row."""
    rid = _seed_resource(engine)
    resp = client.get(f"/api/dashboard/resources/{rid}/posture")
    assert resp.status_code == 200
    data = resp.json()
    assert data["certificates"] == []
    assert data["security_posture"] is None
    assert data["firewall_rules"] == []
    assert data["shares"] == []
    assert data["local_users"] == []
    assert data["local_group_members"] == []


def test_posture_404_for_unknown_resource(client):
    resp = client.get(f"/api/dashboard/resources/{uuid.uuid4()}/posture")
    assert resp.status_code == 404


def test_posture_404_for_malformed_resource_id(client):
    resp = client.get("/api/dashboard/resources/not-a-uuid/posture")
    assert resp.status_code == 404
