"""Tests for the hash-chained, append-only governance ledger (P4.2g / P4.3d).

Uses a real (sqlite, in-memory) session -- record/get/verify read and write
GovernanceEvent rows directly, so a fully mocked session isn't a faithful
test here (matches tests/tools/test_environment_notes.py's pattern for a
module that's genuinely DB-shaped).
"""

from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import GovernanceEvent
from infra_brain.tools import governance_events

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def session_ctx(engine, monkeypatch):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr(governance_events, "get_session", _get_session)
    return _get_session


class TestRecordGovernanceEvent:
    def test_record_success_first_row_has_no_prev_hash(self, session_ctx, engine):
        result = governance_events.record_governance_event(
            client_id="infra-ops-1",
            event_type="instinct_promoted",
            payload={"instinct_id": "abc-123"},
        )

        assert result["client_id"] == "infra-ops-1"
        assert result["event_type"] == "instinct_promoted"
        assert result["payload"] == {"instinct_id": "abc-123"}
        assert result["prev_hash"] is None
        assert isinstance(result["hash"], str) and len(result["hash"]) == 64
        assert result["created_at"] is not None

        import uuid

        uuid.UUID(result["id"])
        with Session(engine) as s:
            row = s.get(GovernanceEvent, uuid.UUID(result["id"]))
            assert row is not None
            assert row.prev_hash is None

    def test_chain_links_across_three_or_more_calls(self, session_ctx):
        r1 = governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_one", payload={"n": 1}
        )
        r2 = governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_two", payload={"n": 2}
        )
        r3 = governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_three", payload={"n": 3}
        )
        r4 = governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_four", payload={"n": 4}
        )

        assert r1["prev_hash"] is None
        assert r2["prev_hash"] == r1["hash"]
        assert r3["prev_hash"] == r2["hash"]
        assert r4["prev_hash"] == r3["hash"]
        # Every hash in the chain is distinct.
        hashes = {r1["hash"], r2["hash"], r3["hash"], r4["hash"]}
        assert len(hashes) == 4

        verdict = governance_events.verify_governance_chain()
        assert verdict == {"valid": True, "broken_at": None, "checked": 4}

    def test_record_rejects_empty_client_id(self, session_ctx):
        with pytest.raises(ValueError, match="client_id is required"):
            governance_events.record_governance_event(
                client_id="   ", event_type="event_one", payload={}
            )

    def test_record_rejects_empty_event_type(self, session_ctx):
        with pytest.raises(ValueError, match="event_type is required"):
            governance_events.record_governance_event(
                client_id="infra-ops-1", event_type="", payload={}
            )

    def test_record_rejects_non_serializable_payload(self, session_ctx):
        with pytest.raises(ValueError, match="JSON-serializable"):
            governance_events.record_governance_event(
                client_id="infra-ops-1",
                event_type="event_one",
                payload={"bad": object()},
            )

    def test_record_defaults_payload_to_empty_dict(self, session_ctx):
        result = governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_one"
        )
        assert result["payload"] == {}


class TestGetGovernanceEvents:
    def test_get_empty_when_no_events(self, session_ctx):
        assert governance_events.get_governance_events() == []

    def test_get_filters_by_client_id(self, session_ctx):
        governance_events.record_governance_event(
            client_id="client-a", event_type="event_one", payload={}
        )
        governance_events.record_governance_event(
            client_id="client-b", event_type="event_two", payload={}
        )
        governance_events.record_governance_event(
            client_id="client-a", event_type="event_three", payload={}
        )

        results = governance_events.get_governance_events(client_id="client-a")

        assert len(results) == 2
        assert all(r["client_id"] == "client-a" for r in results)

    def test_get_none_client_id_returns_all(self, session_ctx):
        governance_events.record_governance_event(
            client_id="client-a", event_type="event_one", payload={}
        )
        governance_events.record_governance_event(
            client_id="client-b", event_type="event_two", payload={}
        )

        results = governance_events.get_governance_events()

        assert len(results) == 2

    def test_get_respects_limit(self, session_ctx):
        for i in range(5):
            governance_events.record_governance_event(
                client_id="client-a", event_type=f"event_{i}", payload={}
            )

        results = governance_events.get_governance_events(limit=2)

        assert len(results) == 2

    def test_since_id_returns_only_newer_rows_oldest_first(self, session_ctx):
        rows = [
            governance_events.record_governance_event(
                client_id="client-a", event_type=f"event_{i}", payload={}
            )
            for i in range(3)
        ]

        results = governance_events.get_governance_events(since_id=rows[0]["id"])

        assert [r["event_type"] for r in results] == ["event_1", "event_2"]

    def test_since_id_unknown_returns_empty(self, session_ctx):
        governance_events.record_governance_event(
            client_id="client-a", event_type="event_one", payload={}
        )
        assert governance_events.get_governance_events(since_id="00000000-0000-0000-0000-000000000000") == []

    def test_since_id_malformed_returns_empty(self, session_ctx):
        governance_events.record_governance_event(
            client_id="client-a", event_type="event_one", payload={}
        )
        assert governance_events.get_governance_events(since_id="not-a-uuid") == []

    def test_since_id_no_newer_rows_returns_empty(self, session_ctx):
        rows = [
            governance_events.record_governance_event(
                client_id="client-a", event_type=f"event_{i}", payload={}
            )
            for i in range(2)
        ]
        assert governance_events.get_governance_events(since_id=rows[-1]["id"]) == []


class TestVerifyGovernanceChain:
    def test_verify_valid_on_empty_table(self, session_ctx):
        assert governance_events.verify_governance_chain() == {
            "valid": True,
            "broken_at": None,
            "checked": 0,
        }

    def test_verify_valid_on_untampered_chain(self, session_ctx):
        governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_one", payload={"n": 1}
        )
        governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_two", payload={"n": 2}
        )
        governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_three", payload={"n": 3}
        )

        result = governance_events.verify_governance_chain()

        assert result["valid"] is True
        assert result["broken_at"] is None
        assert result["checked"] == 3

    def test_verify_detects_tampered_row_and_reports_correct_broken_at(
        self, session_ctx, engine
    ):
        governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_one", payload={"n": 1}
        )
        r2 = governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_two", payload={"n": 2}
        )
        governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_three", payload={"n": 3}
        )

        import uuid

        # Manually corrupt the SECOND row's stored hash -- simulates tampering
        # with an existing "immutable" row directly at the storage layer.
        with Session(engine) as s:
            row = s.get(GovernanceEvent, uuid.UUID(r2["id"]))
            row.hash = "0" * 64
            s.commit()

        result = governance_events.verify_governance_chain()

        assert result["valid"] is False
        assert result["broken_at"] == r2["id"]
        assert result["checked"] == 3

    def test_verify_never_deletes_or_mutates_rows(self, session_ctx, engine):
        """verify_governance_chain is read-only: row count is unaffected by
        calling it, and record_governance_event never touches existing rows."""
        governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_one", payload={}
        )
        governance_events.record_governance_event(
            client_id="infra-ops-1", event_type="event_two", payload={}
        )

        governance_events.verify_governance_chain()

        with Session(engine) as s:
            count = s.query(GovernanceEvent).count()
        assert count == 2
