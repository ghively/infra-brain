"""Tests for tools/client_state.py (P4.2e/P4.2f, convergence plan).

Mirrors test_documents.py's conventions: in-memory SQLite via StaticPool, ORM
schema built with Base.metadata.create_all, get_session patched to a session
factory bound to that engine.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import ClientObservation, ClientStateEntry
from infra_brain.tools.client_state import (
    get_client_state,
    get_observations,
    record_client_state,
    record_observation,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture(autouse=True)
def _patch_get_session(engine, monkeypatch):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr("infra_brain.tools.client_state.get_session", _get_session)
    return _get_session


def _count(engine, model) -> int:
    with Session(engine) as s:
        return s.query(model).count()


# ---------------------------------------------------------------------------
# record_client_state
# ---------------------------------------------------------------------------


def test_record_client_state_wave_failures_success(engine):
    result = record_client_state(
        collection="wave_failures",
        entry_id="wave_failure-1234-abcd",
        payload={
            "sessionId": "sess-1",
            "agentType": "iac-author",
            "needs": ["fact:foo"],
            "outputSnippet": '{"status": "blocked"}',
        },
        client_id="machine-a",
    )

    assert result["collection"] == "wave_failures"
    assert result["entry_id"] == "wave_failure-1234-abcd"
    assert result["client_id"] == "machine-a"
    assert result["id"]
    assert result["created_at"]
    assert _count(engine, ClientStateEntry) == 1

    with Session(engine) as s:
        row = s.query(ClientStateEntry).one()
        assert row.payload["agentType"] == "iac-author"
        assert row.payload["needs"] == ["fact:foo"]


def test_record_client_state_session_debrief_empty_payload(engine):
    """Edge case: session_debrief-shaped write with a minimal/empty payload."""
    result = record_client_state(
        collection="session_debrief",
        entry_id="sess-2-20260731T000000Z",
        payload={},
        client_id="machine-b",
    )

    assert result["collection"] == "session_debrief"
    assert _count(engine, ClientStateEntry) == 1
    with Session(engine) as s:
        row = s.query(ClientStateEntry).one()
        assert row.payload == {}


def test_record_client_state_rejects_unscoped_collection(engine):
    """The convergence-plan scope decision: only the two real use cases are
    accepted, not a speculative generic collection name."""
    with pytest.raises(ValueError, match="not one of the scoped collections"):
        record_client_state(
            collection="anything_goes",
            entry_id="x",
            payload={},
            client_id="machine-a",
        )
    assert _count(engine, ClientStateEntry) == 0


def test_record_client_state_rejects_blank_client_id(engine):
    """Attribution must be a real, non-empty value — never a blindly-trusted
    free-form empty/whitespace string."""
    with pytest.raises(ValueError, match="client_id"):
        record_client_state(
            collection="wave_failures",
            entry_id="wave_failure-1",
            payload={"sessionId": "sess-1"},
            client_id="   ",
        )
    assert _count(engine, ClientStateEntry) == 0


def test_record_client_state_rejects_non_dict_payload(engine):
    with pytest.raises(ValueError, match="payload must be a dict"):
        record_client_state(
            collection="wave_failures",
            entry_id="wave_failure-1",
            payload="not-a-dict",  # type: ignore[arg-type]
            client_id="machine-a",
        )
    assert _count(engine, ClientStateEntry) == 0


# ---------------------------------------------------------------------------
# record_observation
# ---------------------------------------------------------------------------


def test_record_observation_success_with_event_data(engine):
    result = record_observation(
        client_id="machine-a",
        agent="claude-code",
        tool="bash",
        domain="ansible-playbook",
        event_data={"fingerprint": "abc123", "commandName": "ansible-playbook"},
    )

    assert result["client_id"] == "machine-a"
    assert result["agent"] == "claude-code"
    assert result["tool"] == "bash"
    assert result["domain"] == "ansible-playbook"
    assert result["id"]
    assert _count(engine, ClientObservation) == 1

    with Session(engine) as s:
        row = s.query(ClientObservation).one()
        assert row.event_data["fingerprint"] == "abc123"


def test_record_observation_success_without_event_data(engine):
    """Edge case: event_data omitted entirely (defaults to None / SQL NULL)."""
    result = record_observation(
        client_id="machine-b",
        agent="claude-code",
        tool="skill",
        domain="skill-invocation",
    )

    assert result["tool"] == "skill"
    assert _count(engine, ClientObservation) == 1
    with Session(engine) as s:
        row = s.query(ClientObservation).one()
        assert row.event_data is None


def test_record_observation_two_calls_are_two_distinct_rows(engine):
    """ClientObservation is per-event, NOT deduped/aggregated like core.Observation."""
    record_observation(client_id="machine-a", agent="a", tool="bash", domain="general")
    record_observation(client_id="machine-a", agent="a", tool="bash", domain="general")
    assert _count(engine, ClientObservation) == 2


def test_record_observation_rejects_empty_tool(engine):
    with pytest.raises(ValueError, match="tool"):
        record_observation(client_id="machine-a", agent="a", tool="", domain="general")
    assert _count(engine, ClientObservation) == 0


def test_record_observation_rejects_non_dict_event_data(engine):
    with pytest.raises(ValueError, match="event_data must be a dict"):
        record_observation(
            client_id="machine-a",
            agent="a",
            tool="bash",
            domain="general",
            event_data="oops",  # type: ignore[arg-type]
        )
    assert _count(engine, ClientObservation) == 0


# ---------------------------------------------------------------------------
# get_client_state (P5.1 — backs session_debrief.py's wave_failures tally)
# ---------------------------------------------------------------------------


def test_get_client_state_returns_newest_first(engine):
    record_client_state("wave_failures", "wave-1", {"n": 1}, "machine-a")
    record_client_state("wave_failures", "wave-2", {"n": 2}, "machine-a")

    results = get_client_state("wave_failures")

    assert [r["entry_id"] for r in results] == ["wave-2", "wave-1"]


def test_get_client_state_scoped_by_collection(engine):
    record_client_state("wave_failures", "wave-1", {}, "machine-a")
    record_client_state("session_debrief", "sess-1", {}, "machine-a")

    results = get_client_state("wave_failures")

    assert len(results) == 1
    assert results[0]["entry_id"] == "wave-1"


def test_get_client_state_respects_limit(engine):
    for i in range(5):
        record_client_state("wave_failures", f"wave-{i}", {}, "machine-a")

    assert len(get_client_state("wave_failures", limit=2)) == 2


def test_get_client_state_rejects_unscoped_collection(engine):
    with pytest.raises(ValueError, match="not one of the scoped collections"):
        get_client_state("not_allowed")


def test_get_client_state_empty_when_no_entries(engine):
    assert get_client_state("wave_failures") == []


# ---------------------------------------------------------------------------
# get_observations (P5.1 — backs session_debrief.py's topic/tool tally)
# ---------------------------------------------------------------------------


def test_get_observations_filters_by_agent(engine):
    record_observation(client_id="m", agent="sess-a", tool="bash", domain="general")
    record_observation(client_id="m", agent="sess-b", tool="edit", domain="ansible-playbook")
    record_observation(client_id="m", agent="sess-a", tool="edit", domain="ansible-playbook")

    results = get_observations(agent="sess-a")

    assert len(results) == 2
    assert all(r["agent"] == "sess-a" for r in results)


def test_get_observations_filters_by_client_id(engine):
    record_observation(client_id="host-a", agent="s", tool="bash", domain="general")
    record_observation(client_id="host-b", agent="s", tool="bash", domain="general")

    results = get_observations(client_id="host-a")

    assert len(results) == 1
    assert results[0]["client_id"] == "host-a"


def test_get_observations_none_filters_returns_all(engine):
    record_observation(client_id="m", agent="a", tool="bash", domain="general")
    record_observation(client_id="m", agent="b", tool="edit", domain="general")

    assert len(get_observations()) == 2


def test_get_observations_respects_limit(engine):
    for i in range(5):
        record_observation(client_id="m", agent=f"a{i}", tool="bash", domain="general")

    assert len(get_observations(limit=2)) == 2


def test_get_observations_includes_event_data(engine):
    record_observation(
        client_id="m", agent="a", tool="bash", domain="general",
        event_data={"fingerprint": "abc"},
    )
    results = get_observations(agent="a")
    assert results[0]["event_data"] == {"fingerprint": "abc"}
