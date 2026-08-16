"""Tests for scripts/mint_headless_runner_key.py (P6.1/P6.2, D10).

Mirrors test_create_admin.py's conventions: in-memory SQLite, get_session
patched to a session factory bound to that engine.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import McpApiKey

from tests.support.pg import make_engine
import scripts.mint_headless_runner_key as mint_script


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

    monkeypatch.setattr("infra_brain.db.session.get_session", _get_session)
    return _get_session


def _count(engine) -> int:
    with Session(engine) as s:
        return s.query(McpApiKey).count()


def test_mints_key_with_correct_scope(engine, capsys):
    rc = mint_script.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RAW TOKEN" in out
    assert "ibmcp_" in out
    assert _count(engine) == 1
    with Session(engine) as s:
        row = s.query(McpApiKey).one()
        assert row.name == "headless-runner"
        assert set(row.allowed_tools) == {
            "create_gitlab_issue",
            "record_governance_event",
            "record_client_state",
            "propose_instinct",
        }
        assert row.revoked_at is None


def test_refuses_second_mint_without_rotate(engine, capsys):
    mint_script.main([])
    rc = mint_script.main([])
    assert rc == 1
    assert _count(engine) == 1
    assert "already exists" in capsys.readouterr().out


def test_rotate_revokes_old_and_mints_new(engine, capsys):
    mint_script.main([])
    with Session(engine) as s:
        old_id = s.query(McpApiKey).one().id

    rc = mint_script.main(["--rotate"])
    assert rc == 0
    assert _count(engine) == 2
    with Session(engine) as s:
        old_row = s.get(McpApiKey, old_id)
        assert old_row.revoked_at is not None
        active = s.query(McpApiKey).filter(McpApiKey.revoked_at.is_(None)).one()
        assert active.name == "headless-runner"


def test_rotate_with_no_existing_key_just_mints(engine, capsys):
    rc = mint_script.main(["--rotate"])
    assert rc == 0
    assert _count(engine) == 1
