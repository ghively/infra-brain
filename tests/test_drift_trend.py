"""Tests for GET /api/dashboard/drift_events/trend."""

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def auth_client(engine, monkeypatch):
    """Client with require_session bypassed via explicit dev-mode flag.

    Item 1.5b (F-027/F-031) made dev-mode explicit: it used to be implied by
    "no UI_COOKIE_SECRET configured", now it requires INFRA_BRAIN_DEV=1.
    """
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from unittest.mock import patch

    from infra_brain.api.routers.governance import governance_drift_router
    from infra_brain.dashboard_api import router

    app = FastAPI()
    app.include_router(router)
    app.include_router(governance_drift_router)

    with (
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.governance_drift.get_session", _get_session),
    ):
        yield TestClient(app)


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


def test_drift_trend_requires_auth(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from unittest.mock import patch

    from infra_brain.api.routers.governance import governance_drift_router
    from infra_brain.dashboard_api import router
    from infra_brain.dashboard_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(governance_drift_router)

    with (
        patch.dict(os.environ, {"UI_COOKIE_SECRET": "unit-test-secret"}),
        patch("infra_brain.dashboard_auth.get_session", _get_session),
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.governance_drift.get_session", _get_session),
    ):
        tc = TestClient(app, raise_server_exceptions=True)
        resp = tc.get("/api/dashboard/drift_events/trend")
        assert resp.status_code == 401


def test_drift_trend_returns_points(auth_client, db_session):
    from infra_brain.db.models import DriftEvent, Resource

    r = Resource(
        id=uuid.uuid4(),
        domain="linux",
        type="host",
        name="lnx-01",
        source="LinuxAgent",
    )
    db_session.add(r)
    db_session.flush()
    ev = DriftEvent(
        id=uuid.uuid4(),
        resource_id=r.id,
        drift_type="config_drift",
        field="status",
        old_value={"v": "running"},
        new_value={"v": "stopped"},
        detected_at=datetime.now(timezone.utc),
        status="open",
    )
    db_session.add(ev)
    db_session.commit()
    resp = auth_client.get("/api/dashboard/drift_events/trend?days=7")
    assert resp.status_code == 200
    data = resp.json()
    assert "points" in data
    assert data["total"] >= 1
    assert data["days"] == 7


def test_drift_trend_domain_filter(auth_client, db_session):
    from infra_brain.db.models import DriftEvent, Resource

    r_linux = Resource(
        id=uuid.uuid4(),
        domain="linux",
        type="host",
        name="lnx-02",
        source="LinuxAgent",
    )
    db_session.add(r_linux)
    db_session.flush()
    db_session.add(
        DriftEvent(
            id=uuid.uuid4(),
            resource_id=r_linux.id,
            drift_type="config_drift",
            field="kernel",
            old_value={"v": "a"},
            new_value={"v": "b"},
            detected_at=datetime.now(timezone.utc),
            status="open",
        )
    )

    r_win = Resource(
        id=uuid.uuid4(),
        domain="windows",
        type="host",
        name="win-01",
        source="WindowsAgent",
    )
    db_session.add(r_win)
    db_session.flush()
    db_session.add(
        DriftEvent(
            id=uuid.uuid4(),
            resource_id=r_win.id,
            drift_type="config_drift",
            field="patch_state",
            old_value={"v": "patched"},
            new_value={"v": "pending"},
            detected_at=datetime.now(timezone.utc),
            status="open",
        )
    )
    db_session.commit()

    resp = auth_client.get("/api/dashboard/drift_events/trend?domain=linux")
    assert resp.status_code == 200
    data = resp.json()
    # windows events should not appear under linux filter
    assert all(p["domain"] == "linux" for p in data["points"])


def test_drift_trend_empty_domain(auth_client):
    resp = auth_client.get("/api/dashboard/drift_events/trend?domain=nonexistent_xyz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["points"] == []
    assert data["total"] == 0


def test_drift_trend_days_param(auth_client):
    resp = auth_client.get("/api/dashboard/drift_events/trend?days=90")
    assert resp.status_code == 200
    data = resp.json()
    assert data["days"] == 90
