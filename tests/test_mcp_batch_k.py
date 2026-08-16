"""Batch K MCP tools — knowledge/learning read-only query tools (issue #55)."""

import contextlib
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain import mcp_server
from infra_brain.db.models import Document, Observation

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextlib.contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.mcp_server.get_session", _get_session):
        yield engine


def _seed(engine, *objs):
    with Session(engine) as s:
        s.add_all(objs)
        s.commit()


def test_get_documents_success(patched_session):
    _seed(
        patched_session,
        Document(title="Disk runbook", source="confluence", space="OPS", status="current"),
        Document(title="Old page", source="confluence", space="OPS", status="stale"),
    )
    rows = mcp_server.get_documents()
    assert len(rows) == 2
    assert {r["title"] for r in rows} == {"Disk runbook", "Old page"}
    # metadata/freshness columns are surfaced, not chunk text.
    assert "indexed_at" in rows[0] and "status" in rows[0]


def test_get_documents_filters(patched_session):
    _seed(
        patched_session,
        Document(title="Disk runbook", source="confluence", space="OPS", status="current"),
        Document(title="Old page", source="confluence", space="OPS", status="stale"),
        Document(title="Net doc", source="confluence", space="NET", status="current"),
    )
    rows = mcp_server.get_documents(space="OPS", status="current")
    assert [r["title"] for r in rows] == ["Disk runbook"]


def test_get_documents_empty(patched_session):
    assert mcp_server.get_documents() == []


def test_get_observations_success(patched_session):
    _seed(
        patched_session,
        Observation(
            agent="linux", tool="ssh_facts", domain="linux", pattern="port 8080 open", count=5
        ),
        Observation(
            agent="cicd", tool="gitlab_api", domain="cicd", pattern="pipeline flaky", count=2
        ),
    )
    rows = mcp_server.get_observations()
    assert len(rows) == 2
    assert {r["pattern"] for r in rows} == {"port 8080 open", "pipeline flaky"}
    # ordered by count desc — the trending pattern comes first.
    assert rows[0]["count"] == 5


def test_get_observations_domain_filter(patched_session):
    _seed(
        patched_session,
        Observation(
            agent="linux", tool="ssh_facts", domain="linux", pattern="port 8080 open", count=5
        ),
        Observation(
            agent="cicd", tool="gitlab_api", domain="cicd", pattern="pipeline flaky", count=2
        ),
    )
    rows = mcp_server.get_observations(domain="cicd")
    assert [r["pattern"] for r in rows] == ["pipeline flaky"]


def test_get_observations_empty(patched_session):
    assert mcp_server.get_observations() == []
