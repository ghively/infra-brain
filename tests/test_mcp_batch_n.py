"""Batch N MCP tool — home-lab service status by category.

Covers get_homelab_service_category: unfiltered returns every
domain="homelab_services" resource, category filtering narrows correctly
(including the empty-list-for-unknown-category case, same convention as
every other filtered MCP tool in this module), the metadata_ JSONB fields
(category/url/status/http_status) are flattened onto the returned row for
convenience, limit is respected, and other domains are never leaked in.

The category filter uses ``Resource.metadata_["category"].as_string()`` --
the same cross-dialect JSONB-on-PG/JSON-on-SQLite construct
``_bulk_proposal_selection``/get_manual_writes already use for
``payload["field"]`` (see db/models/_base.py's JSONB type) -- so this test
suite (sqlite) exercises the identical code path production (PostgreSQL)
uses.
"""

import contextlib

import pytest
from sqlalchemy.orm import Session
from unittest.mock import patch

from infra_brain import mcp_server
from infra_brain.db.models import Resource

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


def _seed_service(engine, name, category, url, status, http_status):
    with Session(engine) as s:
        s.add(
            Resource(
                domain="homelab_services",
                type="homelab_service",
                name=name,
                source="HomelabServicesAgent",
                metadata_={
                    "category": category,
                    "url": url,
                    "status": status,
                    "http_status": http_status,
                },
            )
        )
        s.commit()


def test_get_homelab_service_category_empty_table_returns_empty_list(patched_session):
    """No homelab_services resources yet (collector hasn't run) reads as an
    empty list, never an error -- same pattern as get_backup_status."""
    assert mcp_server.get_homelab_service_category() == []


def test_get_homelab_service_category_unfiltered_returns_all(patched_session):
    _seed_service(patched_session, "sonarr", "media-management", "http://x:8989", "up", 200)
    _seed_service(patched_session, "emby", "media-server", "http://x:8096", "up", 200)
    _seed_service(patched_session, "grafana", "observability-visualization", "http://x:3002", "down", None)

    rows = mcp_server.get_homelab_service_category()
    assert len(rows) == 3
    assert {r["name"] for r in rows} == {"sonarr", "emby", "grafana"}


def test_get_homelab_service_category_filters_by_category(patched_session):
    _seed_service(patched_session, "sonarr", "media-management", "http://x:8989", "up", 200)
    _seed_service(patched_session, "radarr", "media-management", "http://x:8310", "up", 200)
    _seed_service(patched_session, "emby", "media-server", "http://x:8096", "up", 200)

    rows = mcp_server.get_homelab_service_category(category="media-management")
    assert {r["name"] for r in rows} == {"sonarr", "radarr"}
    assert all(r["category"] == "media-management" for r in rows)


def test_get_homelab_service_category_unknown_category_returns_empty_list(patched_session):
    """A misspelled/unknown category is an empty result, not an error --
    category is collector-supplied free text, never server-validated."""
    _seed_service(patched_session, "sonarr", "media-management", "http://x:8989", "up", 200)
    assert mcp_server.get_homelab_service_category(category="not-a-real-category") == []


def test_get_homelab_service_category_flattens_metadata_fields(patched_session):
    _seed_service(patched_session, "radarr", "media-management", "http://x:8310", "up", 200)
    rows = mcp_server.get_homelab_service_category(category="media-management")
    assert len(rows) == 1
    row = rows[0]
    assert row["category"] == "media-management"
    assert row["url"] == "http://x:8310"
    assert row["status"] == "up"
    assert row["http_status"] == 200
    # Raw payload still present for callers that want the full blob.
    assert row["metadata_"]["category"] == "media-management"


def test_get_homelab_service_category_excludes_other_domains(patched_session):
    _seed_service(patched_session, "sonarr", "media-management", "http://x:8989", "up", 200)
    with Session(patched_session) as s:
        s.add(Resource(domain="linux", type="host", name="not-homelab", source="LinuxAgent"))
        s.commit()

    rows = mcp_server.get_homelab_service_category()
    assert {r["name"] for r in rows} == {"sonarr"}


def test_get_homelab_service_category_respects_limit(patched_session):
    for i in range(5):
        _seed_service(patched_session, f"svc-{i}", "media-management", f"http://x:{i}", "up", 200)
    rows = mcp_server.get_homelab_service_category(limit=2)
    assert len(rows) == 2
