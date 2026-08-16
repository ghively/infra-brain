"""Tests for the OS-internals extensions to GET /resources/{id}/linux and
GET /resources/{id}/windows (UI-2/#58, corrected scope: mounts/nics/
pending-updates + WindowsService only — ports/users/crons are already
covered by the pre-existing linux detail route, see the plan header)."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    LinuxHost,
    LinuxMount,
    LinuxNic,
    LinuxPendingUpdate,
    Resource,
    WindowsPatchState,
    WindowsService,
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


def test_linux_detail_includes_mounts_nics_pending_updates(client, engine):
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="lin-01", source="LinuxAgent")
        s.add(r)
        s.flush()
        lh = LinuxHost(resource_id=r.id, distro="Ubuntu 22.04", kernel="5.15.0", arch="x86_64")
        s.add(lh)
        s.flush()
        s.add(LinuxMount(host_id=lh.id, mount="/", device="/dev/sda1", fstype="ext4", size_total_gb=100.0, size_available_gb=40.0))
        s.add(LinuxNic(host_id=lh.id, name="eth0", mac="aa:bb:cc:dd:ee:ff", ipv4="10.0.0.5"))
        s.add(LinuxPendingUpdate(host_id=lh.id, package="openssl", current_version="1.1.1", available_version="1.1.2", security=True))
        s.commit()
        rid = r.id

    resp = client.get(f"/api/dashboard/resources/{rid}/linux")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["mounts"]) == 1
    assert data["mounts"][0]["mount"] == "/"
    assert len(data["nics"]) == 1
    assert data["nics"][0]["ipv4"] == "10.0.0.5"
    assert len(data["pending_updates"]) == 1
    assert data["pending_updates"][0]["package"] == "openssl"
    assert data["pending_updates"][0]["security"] is True


def test_linux_detail_empty_mounts_nics_pending_updates(client, engine):
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="lin-02", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(LinuxHost(resource_id=r.id, distro="Ubuntu 22.04", kernel="5.15.0", arch="x86_64"))
        s.commit()
        rid = r.id

    resp = client.get(f"/api/dashboard/resources/{rid}/linux")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mounts"] == []
    assert data["nics"] == []
    assert data["pending_updates"] == []


def test_windows_detail_includes_services(client, engine):
    with Session(engine) as s:
        r = Resource(domain="windows", type="host", name="win-01", source="WindowsAgent")
        s.add(r)
        s.flush()
        s.add(WindowsPatchState(resource_id=r.id, hostname="win-01", pending_count=2, winrm_status="ok"))
        s.add(WindowsService(resource_id=r.id, name="Spooler", state="Running", start_type="Automatic"))
        s.commit()
        rid = r.id

    resp = client.get(f"/api/dashboard/resources/{rid}/windows")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["services"]) == 1
    assert data["services"][0]["name"] == "Spooler"
    assert data["services"][0]["state"] == "Running"


def test_windows_detail_empty_services(client, engine):
    with Session(engine) as s:
        r = Resource(domain="windows", type="host", name="win-02", source="WindowsAgent")
        s.add(r)
        s.flush()
        s.add(WindowsPatchState(resource_id=r.id, hostname="win-02", pending_count=0, winrm_status="ok"))
        s.commit()
        rid = r.id

    resp = client.get(f"/api/dashboard/resources/{rid}/windows")
    assert resp.status_code == 200
    assert resp.json()["services"] == []


def test_linux_detail_404_still_works_for_missing_linux_host(client, engine):
    """Regression guard: extending the response model must not change the
    existing 404-when-no-LinuxHost-row behavior."""
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="lin-03", source="LinuxAgent")
        s.add(r)
        s.commit()
        rid = r.id
    resp = client.get(f"/api/dashboard/resources/{rid}/linux")
    assert resp.status_code == 404
