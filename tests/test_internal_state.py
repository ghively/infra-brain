"""Tests for the infra-ops hook write path (P5.1/P5.2,
api/routers/internal_state.py). Mirrors test_dashboard_state_backend.py's
conventions: in-memory SQLite, ORM schema via Base.metadata.create_all --
but this router is bearer-token-gated (not session-gated), so auth
scenarios are exercised directly rather than assumed off.
"""

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from unittest.mock import patch

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


def _make_client(engine, monkeypatch, *, token="", dev=False):
    from infra_brain.config import get_settings

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", token)
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1" if dev else "0")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.internal_state import internal_state_router

    app = FastAPI()
    app.include_router(internal_state_router)
    ctx = (
        patch("infra_brain.tools.client_state.get_session", _get_session),
        patch("infra_brain.tools.governance_events.get_session", _get_session),
        patch("infra_brain.tools.environment_notes.get_session", _get_session),
    )
    for c in ctx:
        c.__enter__()
    return TestClient(app)


@pytest.fixture
def client(engine, monkeypatch):
    yield _make_client(engine, monkeypatch, token="s3cret")


@pytest.fixture
def dev_client(engine, monkeypatch):
    yield _make_client(engine, monkeypatch, token="", dev=True)


@pytest.fixture
def unconfigured_client(engine, monkeypatch):
    yield _make_client(engine, monkeypatch, token="", dev=False)


AUTH = {"Authorization": "Bearer s3cret"}


def test_missing_token_rejected(client):
    resp = client.post(
        "/api/internal/state/observations",
        json={"client_id": "hook:x", "agent": "a", "tool": "bash", "domain": "general"},
    )
    assert resp.status_code == 401


def test_wrong_token_rejected(client):
    resp = client.post(
        "/api/internal/state/observations",
        json={"client_id": "hook:x", "agent": "a", "tool": "bash", "domain": "general"},
        headers={"Authorization": "Bearer nope"},
    )
    assert resp.status_code == 401


def test_unconfigured_token_fails_closed(unconfigured_client):
    resp = unconfigured_client.post(
        "/api/internal/state/observations",
        json={"client_id": "hook:x", "agent": "a", "tool": "bash", "domain": "general"},
    )
    assert resp.status_code == 503


def test_unconfigured_token_allowed_in_dev_mode(dev_client):
    resp = dev_client.post(
        "/api/internal/state/observations",
        json={"client_id": "hook:x", "agent": "a", "tool": "bash", "domain": "general"},
    )
    assert resp.status_code == 200


def test_create_observation(client):
    resp = client.post(
        "/api/internal/state/observations",
        json={
            "client_id": "hook:home-lab-01",
            "agent": "observe_runner",
            "tool": "bash",
            "domain": "ansible-playbook",
            "event_data": {"fingerprint": "abc123"},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == "hook:home-lab-01"
    assert body["tool"] == "bash"


def test_create_observation_rejects_blank_field(client):
    resp = client.post(
        "/api/internal/state/observations",
        json={"client_id": "", "agent": "a", "tool": "bash", "domain": "general"},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_create_client_state(client):
    resp = client.post(
        "/api/internal/state/client-state",
        json={
            "collection": "wave_failures",
            "entry_id": "wave-1",
            "payload": {"agentType": "iac-author", "needs": ["a", "b"]},
            "client_id": "hook:home-lab-01",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["collection"] == "wave_failures"
    assert body["entry_id"] == "wave-1"


def test_create_client_state_rejects_unscoped_collection(client):
    resp = client.post(
        "/api/internal/state/client-state",
        json={
            "collection": "not_a_real_collection",
            "entry_id": "x",
            "payload": {},
            "client_id": "hook:home-lab-01",
        },
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_create_governance_event(client):
    resp = client.post(
        "/api/internal/state/governance-events",
        json={
            "client_id": "hook:home-lab-01",
            "event_type": "learning-promotion-gate",
            "payload": {"result": "allowed"},
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["event_type"] == "learning-promotion-gate"
    assert body["hash"]
    assert body["prev_hash"] is None


def test_create_governance_event_chains(client):
    first = client.post(
        "/api/internal/state/governance-events",
        json={"client_id": "hook:x", "event_type": "e1", "payload": {}},
        headers=AUTH,
    ).json()
    second = client.post(
        "/api/internal/state/governance-events",
        json={"client_id": "hook:x", "event_type": "e2", "payload": {}},
        headers=AUTH,
    ).json()
    assert second["prev_hash"] == first["hash"]


def test_create_governance_event_rejects_blank_client_id(client):
    resp = client.post(
        "/api/internal/state/governance-events",
        json={"client_id": "", "event_type": "e1", "payload": {}},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_list_governance_events_newest_first(client):
    for i in range(3):
        client.post(
            "/api/internal/state/governance-events",
            json={"client_id": "hook:x", "event_type": f"e{i}", "payload": {}},
            headers=AUTH,
        )
    resp = client.get("/api/internal/state/governance-events", headers=AUTH)
    assert resp.status_code == 200
    types = [row["event_type"] for row in resp.json()]
    assert types == ["e2", "e1", "e0"]


def test_list_governance_events_since_id_cursor(client):
    ids = []
    for i in range(3):
        row = client.post(
            "/api/internal/state/governance-events",
            json={"client_id": "hook:x", "event_type": f"e{i}", "payload": {}},
            headers=AUTH,
        ).json()
        ids.append(row["id"])

    resp = client.get(
        "/api/internal/state/governance-events",
        params={"since_id": ids[0]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    types = [row["event_type"] for row in resp.json()]
    assert types == ["e1", "e2"]  # oldest-first past the cursor


def test_list_governance_events_since_id_unknown_returns_empty(client):
    resp = client.get(
        "/api/internal/state/governance-events",
        params={"since_id": "00000000-0000-0000-0000-000000000000"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_client_state(client):
    client.post(
        "/api/internal/state/client-state",
        json={"collection": "wave_failures", "entry_id": "w-1", "payload": {}, "client_id": "hook:x"},
        headers=AUTH,
    )
    resp = client.get(
        "/api/internal/state/client-state", params={"collection": "wave_failures"}, headers=AUTH
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["entry_id"] == "w-1"


def test_list_client_state_rejects_unscoped_collection(client):
    resp = client.get(
        "/api/internal/state/client-state", params={"collection": "nope"}, headers=AUTH
    )
    assert resp.status_code == 400


def test_list_environment_notes(client):
    from infra_brain.tools.environment_notes import record_environment_note

    record_environment_note(note="staging DB scheduled for decommission", author="hook:x")
    resp = client.get("/api/internal/state/environment-notes", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["note"] == "staging DB scheduled for decommission"
    assert body[0]["status"] == "open"


def test_list_environment_notes_filters_by_status(client):
    from infra_brain.tools.environment_notes import record_environment_note

    record_environment_note(note="x", author="hook:x")
    resp = client.get(
        "/api/internal/state/environment-notes", params={"status": "resolved"}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_observations(client):
    client.post(
        "/api/internal/state/observations",
        json={"client_id": "hook:x", "agent": "sess-1", "tool": "bash", "domain": "general"},
        headers=AUTH,
    )
    resp = client.get(
        "/api/internal/state/observations", params={"agent": "sess-1"}, headers=AUTH
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["agent"] == "sess-1"
