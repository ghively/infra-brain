"""Tests for the environment_notes narrative layer (P4.2a / P4.3a).

Uses a real (sqlite, in-memory) session -- record/get/resolve read and write
EnvironmentNote rows directly, so a fully mocked session isn't a faithful
test here (matches tests/tools/test_webhook_publish.py's pattern for a
module that's genuinely DB-shaped).
"""

from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import EnvironmentNote
from infra_brain.tools import environment_notes

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

    monkeypatch.setattr(environment_notes, "get_session", _get_session)
    return _get_session


class TestRecordEnvironmentNote:
    def test_record_success(self, session_ctx, engine):
        result = environment_notes.record_environment_note(
            note="staging DB host is scheduled for decommission next quarter",
            author="exampleuser",
        )

        assert result["note"] == (
            "staging DB host is scheduled for decommission next quarter"
        )
        assert result["author"] == "exampleuser"
        assert result["status"] == "open"
        assert result["resolved_by"] is None
        assert result["resolved_at"] is None
        assert result["created_at"] is not None
        # id is a real UUID string
        import uuid

        uuid.UUID(result["id"])

        with Session(engine) as s:
            row = s.get(EnvironmentNote, uuid.UUID(result["id"]))
            assert row is not None
            assert row.status == "open"

    def test_record_strips_whitespace_and_truncates_author(self, session_ctx):
        long_author = "x" * 200
        result = environment_notes.record_environment_note(
            note="  padded note text  ",
            author=long_author,
        )
        assert result["note"] == "padded note text"
        assert len(result["author"]) == 128
        assert result["author"] == "x" * 128

    def test_record_rejects_empty_note(self, session_ctx):
        with pytest.raises(ValueError, match="note must not be empty"):
            environment_notes.record_environment_note(note="   ", author="exampleuser")

    def test_record_rejects_oversized_note(self, session_ctx):
        with pytest.raises(ValueError, match="8000-character limit"):
            environment_notes.record_environment_note(note="x" * 8001, author="exampleuser")

    def test_record_accepts_exactly_max_length_note(self, session_ctx):
        result = environment_notes.record_environment_note(note="x" * 8000, author="exampleuser")
        assert len(result["note"]) == 8000

    def test_record_rejects_empty_author(self, session_ctx):
        with pytest.raises(ValueError, match="author must be a non-empty"):
            environment_notes.record_environment_note(note="a real note", author="   ")


class TestGetEnvironmentNotes:
    def test_get_empty_when_no_notes(self, session_ctx):
        assert environment_notes.get_environment_notes() == []

    def test_get_default_status_filters_to_open(self, session_ctx):
        opened = environment_notes.record_environment_note(note="open note", author="a")
        recorded = environment_notes.record_environment_note(note="to resolve", author="a")
        environment_notes.resolve_environment_note(recorded["id"], resolved_by="b")

        results = environment_notes.get_environment_notes()

        assert [r["id"] for r in results] == [opened["id"]]
        assert results[0]["status"] == "open"

    def test_get_status_none_returns_all(self, session_ctx):
        environment_notes.record_environment_note(note="one", author="a")
        second = environment_notes.record_environment_note(note="two", author="a")
        environment_notes.resolve_environment_note(second["id"], resolved_by="b")

        results = environment_notes.get_environment_notes(status=None)

        assert len(results) == 2

    def test_get_resolved_filter(self, session_ctx):
        environment_notes.record_environment_note(note="stays open", author="a")
        recorded = environment_notes.record_environment_note(note="gets resolved", author="a")
        environment_notes.resolve_environment_note(recorded["id"], resolved_by="b")

        results = environment_notes.get_environment_notes(status="resolved")

        assert len(results) == 1
        assert results[0]["note"] == "gets resolved"
        assert results[0]["resolved_by"] == "b"
        assert results[0]["resolved_at"] is not None

    def test_get_respects_limit(self, session_ctx):
        for i in range(5):
            environment_notes.record_environment_note(note=f"note {i}", author="a")

        results = environment_notes.get_environment_notes(limit=2)

        assert len(results) == 2

    def test_get_zero_or_negative_limit_returns_empty(self, session_ctx):
        environment_notes.record_environment_note(note="note", author="a")

        assert environment_notes.get_environment_notes(limit=0) == []
        assert environment_notes.get_environment_notes(limit=-5) == []

    def test_get_most_recent_first(self, session_ctx):
        first = environment_notes.record_environment_note(note="first", author="a")
        second = environment_notes.record_environment_note(note="second", author="a")

        results = environment_notes.get_environment_notes(status=None)

        ids_in_order = [r["id"] for r in results]
        assert ids_in_order.index(second["id"]) < ids_in_order.index(first["id"])


class TestResolveEnvironmentNote:
    def test_resolve_success(self, session_ctx):
        recorded = environment_notes.record_environment_note(note="fix me", author="a")

        result = environment_notes.resolve_environment_note(
            recorded["id"], resolved_by="exampleuser"
        )

        assert result["status"] == "resolved"
        assert result["resolved_by"] == "exampleuser"
        assert result["resolved_at"] is not None
        assert result["id"] == recorded["id"]

    def test_resolve_truncates_resolved_by(self, session_ctx):
        recorded = environment_notes.record_environment_note(note="fix me", author="a")
        long_resolver = "y" * 200

        result = environment_notes.resolve_environment_note(recorded["id"], long_resolver)

        assert len(result["resolved_by"]) == 128

    def test_resolve_rejects_invalid_uuid(self, session_ctx):
        with pytest.raises(ValueError, match="not a valid UUID"):
            environment_notes.resolve_environment_note("not-a-uuid", resolved_by="a")

    def test_resolve_rejects_missing_note(self, session_ctx):
        missing_id = "00000000-0000-0000-0000-000000000000"
        with pytest.raises(ValueError, match="no environment note found"):
            environment_notes.resolve_environment_note(missing_id, resolved_by="a")

    def test_resolve_rejects_empty_resolved_by(self, session_ctx):
        recorded = environment_notes.record_environment_note(note="fix me", author="a")
        with pytest.raises(ValueError, match="resolved_by must be a non-empty"):
            environment_notes.resolve_environment_note(recorded["id"], resolved_by="")
