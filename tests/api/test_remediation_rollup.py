"""Tests for the ``view=rollup`` dedup/aggregate mode on
``GET /api/dashboard/remediation`` (TRK-255).

Mirrors the existing dashboard test conventions: in-memory SQLite, the ORM
schema via ``Base.metadata.create_all``, and ``get_session`` patched to the
test engine (see tests/test_dashboard_fleet_software_cve.py).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.api.routers.vuln import vuln_router
from infra_brain.db.models import ProposedAction

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

    from unittest.mock import patch

    app = FastAPI()
    app.include_router(vuln_router)
    with patch("infra_brain.api.routers.vuln.get_session", _get_session):
        yield TestClient(app)


@pytest.fixture
def seeded_proposed_actions(engine):
    """Several pending ``config_fix`` rows sharing ``payload["field"]``, plus
    one distinct field and one non-pending row (must be excluded from rollup).
    """
    with Session(engine) as s:
        rows = [
            ProposedAction(
                agent="remediation",
                action_type="config_fix",
                target="host-a.example.com",
                payload={"field": "max_staleness", "plan": "bump staleness threshold on host-a"},
                status="pending",
            ),
            ProposedAction(
                agent="remediation",
                action_type="config_fix",
                target="host-b.example.com",
                payload={"field": "max_staleness", "plan": "raise staleness limit for host-b"},
                status="pending",
            ),
            ProposedAction(
                agent="remediation",
                action_type="config_fix",
                target="host-c.example.com",
                payload={"field": "max_staleness", "plan": "adjust staleness setting"},
                status="pending",
            ),
            ProposedAction(
                agent="remediation",
                action_type="config_fix",
                target="host-d.example.com",
                payload={"field": "retention_days", "plan": "extend retention"},
                status="pending",
            ),
            ProposedAction(
                agent="remediation",
                action_type="config_fix",
                target="host-e.example.com",
                payload={"field": "max_staleness", "plan": "already handled"},
                status="approved",
            ),
        ]
        s.add_all(rows)
        s.commit()
    return engine


def test_rollup_groups_config_fix_by_field(client, seeded_proposed_actions):
    resp = client.get("/api/dashboard/remediation?view=rollup")
    assert resp.status_code == 200
    body = resp.json()
    group = next(g for g in body["items"] if g["field"] == "max_staleness")
    assert group["count"] >= 2
    assert group["action_type"] == "config_fix"
    assert "sample_targets" in group
    assert len(group["sample_targets"]) == group["count"]


def test_rollup_excludes_non_pending_rows(client, seeded_proposed_actions):
    resp = client.get("/api/dashboard/remediation?view=rollup")
    body = resp.json()
    group = next(g for g in body["items"] if g["field"] == "max_staleness")
    # 3 pending max_staleness rows seeded; the 4th (approved) must not count.
    assert group["count"] == 3
    assert "host-e.example.com" not in group["sample_targets"]


def test_rollup_distinct_field_is_its_own_group(client, seeded_proposed_actions):
    resp = client.get("/api/dashboard/remediation?view=rollup")
    body = resp.json()
    fields = {g["field"] for g in body["items"]}
    assert "retention_days" in fields
    retention_group = next(g for g in body["items"] if g["field"] == "retention_days")
    assert retention_group["count"] == 1


def test_rollup_empty_when_no_pending_actions(client, engine):
    resp = client.get("/api/dashboard/remediation?view=rollup")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_detail_view_is_unaffected_by_rollup(client, seeded_proposed_actions):
    """The default (detail) view keeps returning flat ProposedAction rows."""
    resp = client.get("/api/dashboard/remediation")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 5
    assert "field" not in body["items"][0]
