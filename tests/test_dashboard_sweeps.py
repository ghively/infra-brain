"""Tests for GET /api/dashboard/sweeps and /api/dashboard/sweeps/{sweep_id}
(Phase 4 Task 4 — sweep view + logging enhancement).

Mirrors test_dashboard_fleet_software_cve.py's conventions: in-memory
SQLite, ORM schema via Base.metadata.create_all, get_session patched to the
test engine, dev-mode auth-off client.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.agents.llm_base import RECURSION_LIMIT_MARKER
from infra_brain.db.models import AgentDecisionLog, CollectionRun

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

    from infra_brain.api.routers.fleet import fleet_router

    app = FastAPI()
    app.include_router(fleet_router)
    with patch("infra_brain.api.routers.fleet.get_session", _get_session):
        yield TestClient(app)


def _now():
    return datetime.now(timezone.utc)


def test_list_sweeps_empty_db_returns_empty_list(client):
    resp = client.get("/api/dashboard/sweeps")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "total": 0, "limit": 20}


def test_list_sweeps_groups_by_sweep_id_and_counts_statuses(engine, client):
    sweep_id = uuid.uuid4()
    t0 = _now()
    with Session(engine) as s:
        s.add_all(
            [
                CollectionRun(
                    domain="linux",
                    trigger_type="scheduled",
                    started_at=t0,
                    finished_at=t0 + timedelta(seconds=30),
                    resources_found=10,
                    status="completed",
                    sweep_id=sweep_id,
                ),
                CollectionRun(
                    domain="windows",
                    trigger_type="scheduled",
                    started_at=t0,
                    finished_at=t0 + timedelta(seconds=60),
                    resources_found=5,
                    status="failed",
                    sweep_id=sweep_id,
                ),
                # Not part of any sweep — must not leak into the grouping.
                CollectionRun(
                    domain="vsphere",
                    trigger_type="manual",
                    started_at=t0,
                    status="completed",
                    sweep_id=None,
                ),
            ]
        )
        s.commit()

    resp = client.get("/api/dashboard/sweeps")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["sweep_id"] == str(sweep_id)
    assert item["domain_count"] == 2
    assert item["status_counts"]["completed"] == 1
    assert item["status_counts"]["failed"] == 1
    assert item["status_counts"]["retry_exhausted"] == 0
    assert item["duration_seconds"] == 60


def test_list_sweeps_respects_limit_param(engine, client):
    with Session(engine) as s:
        for i in range(3):
            sid = uuid.uuid4()
            s.add(
                CollectionRun(
                    domain="linux",
                    trigger_type="scheduled",
                    started_at=_now() - timedelta(hours=i),
                    finished_at=_now() - timedelta(hours=i) + timedelta(seconds=10),
                    status="completed",
                    sweep_id=sid,
                )
            )
        s.commit()

    resp = client.get("/api/dashboard/sweeps?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2


def test_sweep_detail_returns_per_domain_rows_and_unknown_tier_fallback(engine, client):
    sweep_id = uuid.uuid4()
    t0 = _now()
    with Session(engine) as s:
        run1 = CollectionRun(
            domain="linux",
            trigger_type="scheduled",
            started_at=t0,
            finished_at=t0 + timedelta(seconds=20),
            resources_found=7,
            status="completed",
            sweep_id=sweep_id,
        )
        # "totally_made_up_domain" has no AgentSpec entry -> tier "unknown".
        run2 = CollectionRun(
            domain="totally_made_up_domain",
            trigger_type="scheduled",
            started_at=t0,
            status="in_progress",
            sweep_id=sweep_id,
        )
        s.add_all([run1, run2])
        s.flush()
        # A recursion-limit hit on run1, which should surface as max_iters_hits.
        s.add(
            AgentDecisionLog(
                run_id=run1.id,
                agent="linux",
                domain="linux",
                decision_summary=RECURSION_LIMIT_MARKER,
            )
        )
        s.commit()

    resp = client.get(f"/api/dashboard/sweeps/{sweep_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sweep_id"] == str(sweep_id)
    by_domain = {d["domain"]: d for d in body["domains"]}
    assert by_domain["linux"]["tier"] == "collector"
    assert by_domain["linux"]["max_iters_hits"] == 1
    assert by_domain["totally_made_up_domain"]["tier"] == "unknown"
    assert body["max_iters_hit_count"] == 1
    assert body["status_counts"]["in_progress"] == 1
    assert body["status_counts"]["completed"] == 1


def test_sweep_detail_unknown_sweep_id_returns_404(client):
    resp = client.get(f"/api/dashboard/sweeps/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_sweep_detail_malformed_sweep_id_returns_404(client):
    resp = client.get("/api/dashboard/sweeps/not-a-uuid")
    assert resp.status_code == 404
