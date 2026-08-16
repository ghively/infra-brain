"""Task 3: Resource + DriftEvent types round-trip seeded rows, and Task 5's
DataLoader batches the Resource -> DriftEvent N+1 into one query."""

import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from infra_brain.db.models import DriftEvent, Resource

# ``infra_brain.api.graphql.__init__`` re-exports the module-level ``schema``
# Schema instance under the name ``schema``, which shadows the ``schema``
# *submodule* as an attribute of the ``infra_brain.api.graphql`` package.
# Both ``from infra_brain.api.graphql import schema`` and
# ``import infra_brain.api.graphql.schema as x`` therefore resolve to the
# Schema instance, not the module — go through sys.modules to get the real
# submodule (which is what ``get_session``/``_query_resources`` live on).
import infra_brain.api.graphql.schema  # noqa: F401  (ensures it's imported)

from tests.support.pg import make_engine


schema_module = sys.modules["infra_brain.api.graphql.schema"]


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

    with (
        patch("infra_brain.api.graphql.schema.get_session", _get_session),
    ):
        yield TestClient(app)


def _seed(engine):
    with Session(engine) as s:
        r1 = Resource(domain="vsphere", type="vm", name="vm-web-01", source="vSphereAgent")
        r2 = Resource(domain="linux", type="host", name="prod-web-01", source="LinuxAgent")
        s.add_all([r1, r2])
        s.flush()
        s.add(
            DriftEvent(
                resource_id=r1.id,
                drift_type="config",
                field="cpu_count",
                old_value={"v": 2},
                new_value={"v": 4},
                status="open",
            )
        )
        s.commit()
        return r1.id, r2.id


def test_resources_query_returns_seeded_rows_with_nested_drift(client, engine):
    _seed(engine)
    resp = client.post(
        "/api/graphql",
        json={"query": '{ resources(domain: "vsphere") { name driftEvents { field status } } }'},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("errors") is None
    resources = body["data"]["resources"]
    assert len(resources) == 1
    assert resources[0]["name"] == "vm-web-01"
    assert resources[0]["driftEvents"] == [{"field": "cpu_count", "status": "open"}]


def test_resources_query_limit_and_no_domain_filter(client, engine):
    _seed(engine)
    resp = client.post("/api/graphql", json={"query": "{ resources { name } }"})
    assert resp.status_code == 200
    names = {r["name"] for r in resp.json()["data"]["resources"]}
    assert names == {"vm-web-01", "prod-web-01"}


def test_query_resources_clamps_negative_limit_to_positive_one():
    """A negative ``limit`` (e.g. GraphQL ``resources(limit: -1)``) must be
    clamped to at least 1 before it reaches the ORM's ``.limit()`` — passing
    a negative value straight through would hit PostgreSQL's LIMIT clause,
    which rejects negative values with a 500 from inside the resolver."""
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = []

    mock_session = MagicMock()
    mock_session.query.return_value = mock_query

    @contextmanager
    def _fake_get_session():
        yield mock_session

    with patch.object(schema_module, "get_session", _fake_get_session):
        result = schema_module._query_resources(None, -1)

    mock_query.limit.assert_called_once_with(1)
    assert result == []


def test_query_resources_clamps_zero_limit_to_positive_one():
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = []

    mock_session = MagicMock()
    mock_session.query.return_value = mock_query

    @contextmanager
    def _fake_get_session():
        yield mock_session

    with patch.object(schema_module, "get_session", _fake_get_session):
        schema_module._query_resources(None, 0)

    mock_query.limit.assert_called_once_with(1)


def test_query_resources_still_caps_excessive_limit():
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = []

    mock_session = MagicMock()
    mock_session.query.return_value = mock_query

    @contextmanager
    def _fake_get_session():
        yield mock_session

    with patch.object(schema_module, "get_session", _fake_get_session):
        schema_module._query_resources(None, 10_000)

    mock_query.limit.assert_called_once_with(schema_module._MAX_LIMIT)


def test_resources_query_with_negative_limit_does_not_error(client, engine):
    """End-to-end smoke test: the GraphQL resolver must not surface a 500 or
    a GraphQL error for a negative ``limit`` argument."""
    _seed(engine)
    resp = client.post(
        "/api/graphql",
        json={"query": "{ resources(limit: -1) { name } }"},
    )
    assert resp.status_code == 200
    assert resp.json().get("errors") is None


def test_drift_events_batched_into_one_query(client, engine):
    """5 resources each with drift; the DriftEvent lookup must be ONE batched
    query (DataLoader), not five."""
    with Session(engine) as s:
        resources = [
            Resource(domain="vsphere", type="vm", name=f"vm-{i}", source="vSphereAgent")
            for i in range(5)
        ]
        s.add_all(resources)
        s.flush()
        for r in resources:
            s.add(
                DriftEvent(
                    resource_id=r.id,
                    drift_type="config",
                    field="cpu_count",
                    status="open",
                )
            )
        s.commit()

    query_count = {"n": 0}

    def _count(*args, **kwargs):
        query_count["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        resp = client.post(
            "/api/graphql",
            json={"query": "{ resources { name driftEvents { field } } }"},
        )
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert resp.status_code == 200
    assert resp.json().get("errors") is None
    drift_query_count_before = query_count["n"]
    assert drift_query_count_before > 0
    # One resources SELECT + one batched drift_events SELECT == 2 total
    # (not 1 + 5). This is the N+1 regression guard.
    assert drift_query_count_before <= 2
