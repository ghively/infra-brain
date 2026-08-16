"""Task 4: HostIdentity, ComplianceViolation, EolRegistry catalog queries —
the Backstage-style catalog surface."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    ComplianceViolation,
    EolRegistry,
    HostIdentity,
    Resource,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    from infra_brain.config import get_settings

    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.graphql.router import graphql_router

    app = FastAPI()
    app.include_router(graphql_router)

    with patch("infra_brain.api.graphql.schema.get_session", _get_session):
        yield TestClient(app)


def test_hosts_query_filters_by_name(client, engine):
    with Session(engine) as s:
        s.add_all(
            [
                HostIdentity(short_hostname="prod-web-01", fqdn="prod-web-01.example.com"),
                HostIdentity(short_hostname="prod-db-01", fqdn="prod-db-01.example.com"),
            ]
        )
        s.commit()

    resp = client.post("/api/graphql", json={"query": '{ hosts(q: "web") { shortHostname } }'})
    assert resp.status_code == 200
    assert resp.json()["data"]["hosts"] == [{"shortHostname": "prod-web-01"}]


def test_compliance_violations_query_filters_by_status(client, engine):
    with Session(engine) as s:
        s.add_all(
            [
                ComplianceViolation(rule="r1", host="h1", status="open"),
                ComplianceViolation(rule="r2", host="h2", status="resolved"),
            ]
        )
        s.commit()

    resp = client.post(
        "/api/graphql",
        json={"query": '{ complianceViolations(status: "open") { rule status } }'},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]["complianceViolations"]
    assert data == [{"rule": "r1", "status": "open"}]


def test_eol_query_overdue_only(client, engine):
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        r1 = Resource(domain="linux", type="host", name="old-box", source="LinuxAgent")
        r2 = Resource(domain="linux", type="host", name="new-box", source="LinuxAgent")
        s.add_all([r1, r2])
        s.flush()
        s.add_all(
            [
                EolRegistry(
                    resource_id=r1.id,
                    asset_name="old-box",
                    eol_date=now - timedelta(days=30),
                ),
                EolRegistry(
                    resource_id=r2.id,
                    asset_name="new-box",
                    eol_date=now + timedelta(days=365),
                ),
            ]
        )
        s.commit()

    resp = client.post(
        "/api/graphql",
        json={"query": "{ eol(overdueOnly: true) { assetName } }"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["eol"] == [{"assetName": "old-box"}]
