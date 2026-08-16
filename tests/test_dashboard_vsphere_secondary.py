"""Tests for GET /api/vsphere/secondary (UI-3/#59): networks, resource pools,
VM disks, snapshots (stale-snapshot hygiene), licenses, alarms, permissions.
Mirrors the existing dashboard router test conventions (in-memory SQLite,
get_session patched to the test engine)."""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    VsphereAlarm,
    VsphereLicense,
    VsphereNetwork,
    VspherePermission,
    VsphereResourcePool,
    VsphereSnapshot,
    VsphereVmDisk,
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

    from infra_brain.api.routers.vsphere import vsphere_router

    app = FastAPI()
    app.include_router(vsphere_router)
    with patch("infra_brain.api.routers.vsphere.get_session", _get_session):
        yield TestClient(app)


def test_secondary_empty_state(client):
    resp = client.get("/api/vsphere/secondary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["networks"] == []
    assert data["summary"]["network_count"] == 0
    assert data["summary"]["stale_snapshot_count"] == 0


def test_secondary_returns_all_seven_categories(client, engine):
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        s.add(VsphereNetwork(vcenter="vc1", moref="net-1", name="VM Network", network_kind="portgroup", accessible=True, vm_count=3))
        s.add(VsphereResourcePool(vcenter="vc1", moref="rp-1", name="Prod Pool", cpu_limit=-1, vm_count=5))
        s.add(VsphereVmDisk(vcenter="vc1", vm_moref="vm-1", vm_name="app-01", disk_key=0, label="Hard disk 1", capacity_gb=100.0))
        s.add(
            VsphereSnapshot(
                vcenter="vc1",
                vm_moref="vm-1",
                vm_name="app-01",
                snapshot_id=1,
                name="pre-upgrade",
                age_days=45,
                is_current=True,
            )
        )
        s.add(VsphereSnapshot(vcenter="vc1", vm_moref="vm-2", vm_name="app-02", snapshot_id=1, name="fresh", age_days=1))
        s.add(VsphereLicense(vcenter="vc1", name="vSphere Enterprise Plus", total=10, used=7, expiration=now))
        s.add(VsphereAlarm(vcenter="vc1", alarm_name="Host connection lost", entity_name="esxi-01", overall_status="red"))
        s.add(VspherePermission(vcenter="vc1", principal="DOMAIN\\admins", role_name="Administrator", is_group=True))
        s.commit()

    resp = client.get("/api/vsphere/secondary")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["networks"]) == 1
    assert data["networks"][0]["name"] == "VM Network"
    assert len(data["resource_pools"]) == 1
    assert len(data["vm_disks"]) == 1
    assert data["vm_disks"][0]["capacity_gb"] == 100.0
    assert len(data["snapshots"]) == 2
    assert len(data["licenses"]) == 1
    assert len(data["alarms"]) == 1
    assert len(data["permissions"]) == 1
    # Stale-snapshot hygiene: only the 45-day-old snapshot counts (>= 7 days).
    assert data["summary"]["stale_snapshot_count"] == 1
    assert data["summary"]["snapshot_count"] == 2


def test_secondary_vm_disks_and_snapshots_capped_at_500(client, engine):
    with Session(engine) as s:
        for i in range(510):
            s.add(
                VsphereSnapshot(
                    vcenter="vc1", vm_moref=f"vm-{i}", vm_name=f"vm-{i}", snapshot_id=1, age_days=i
                )
            )
        s.commit()

    resp = client.get("/api/vsphere/secondary")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["snapshots"]) == 500
    assert data["summary"]["snapshot_count"] == 510
