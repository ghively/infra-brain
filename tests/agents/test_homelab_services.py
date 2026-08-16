"""Tests for HomelabServicesAgent (additive home-lab collectors scope).

Covers: success with a mocked manifest + mocked httpx returning a mix of
up/down services, an empty manifest raising CollectorSkipped (and a missing
manifest file doing the same), and one unreachable service not aborting
collection of the rest.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from infra_brain.agents.homelab_services import HomelabServicesAgent
from infra_brain.etl.base import CollectorSkipped, CollectOutcome

MODULE = "infra_brain.agents.homelab_services"


def _write_manifest(tmp_path, services):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"services": services}))
    return str(path)


@pytest.fixture
def agent(make_agent):
    return make_agent(HomelabServicesAgent)


def test_collect_success_mixed_up_down(agent, tmp_path):
    """Two configured services: one reachable (200), one that raises a
    connection error. Both must appear in the result with the right status."""
    manifest_path = _write_manifest(
        tmp_path,
        [
            {"name": "grafana", "url": "http://203.0.113.15:3002", "category": "monitoring"},
            {"name": "ntfy", "url": "http://203.0.113.15:8080", "category": "notifications"},
        ],
    )
    ok_resp = MagicMock(status_code=200)

    def _fake_get(url, timeout):
        if "3002" in url:
            return ok_resp
        raise httpx.ConnectError("refused")

    with patch(f"{MODULE}.readonly_get", side_effect=_fake_get):
        outcome = agent.collect(manifest_path=manifest_path)

    assert isinstance(outcome, CollectOutcome)
    assert outcome.errors == []
    assert len(outcome.items) == 2

    by_name = {item["name"]: item for item in outcome.items}
    assert by_name["grafana"]["type"] == "homelab_service"
    assert by_name["grafana"]["data"]["status"] == "up"
    assert by_name["grafana"]["data"]["http_status"] == 200
    assert by_name["grafana"]["data"]["category"] == "monitoring"
    assert by_name["grafana"]["data"]["url"] == "http://203.0.113.15:3002"

    assert by_name["ntfy"]["data"]["status"] == "down"
    assert by_name["ntfy"]["data"]["http_status"] is None


def test_collect_treats_non_5xx_as_up(agent, tmp_path):
    """A bare-root 404 (very common for these services, per the wiki) still
    counts as reachable -- only a connection failure or 5xx means down."""
    manifest_path = _write_manifest(
        tmp_path,
        [{"name": "hermes-mcp-bridge", "url": "http://203.0.113.19:8766", "category": "ai"}],
    )
    resp_404 = MagicMock(status_code=404)

    with patch(f"{MODULE}.readonly_get", return_value=resp_404):
        outcome = agent.collect(manifest_path=manifest_path)

    assert len(outcome.items) == 1
    assert outcome.items[0]["data"]["status"] == "up"
    assert outcome.items[0]["data"]["http_status"] == 404


def test_collect_empty_manifest_raises_collector_skipped(agent, tmp_path):
    manifest_path = _write_manifest(tmp_path, [])
    with pytest.raises(CollectorSkipped):
        agent.collect(manifest_path=manifest_path)


def test_collect_missing_manifest_file_raises_collector_skipped(agent, tmp_path):
    missing_path = str(tmp_path / "does-not-exist.json")
    with pytest.raises(CollectorSkipped):
        agent.collect(manifest_path=missing_path)


def test_collect_invalid_json_manifest_raises_collector_skipped(agent, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    with pytest.raises(CollectorSkipped):
        agent.collect(manifest_path=str(path))


def test_unreachable_service_does_not_abort_the_rest(agent, tmp_path):
    """A failure probing the middle service of three must not prevent the
    first and third from being collected (per-service try/except tolerance,
    mirroring ContainerRegistryAgent / tools/ansible.py's per-host pattern)."""
    manifest_path = _write_manifest(
        tmp_path,
        [
            {"name": "svc-a", "url": "http://10.0.0.1:1111", "category": "test"},
            {"name": "svc-b", "url": "http://10.0.0.2:2222", "category": "test"},
            {"name": "svc-c", "url": "http://10.0.0.3:3333", "category": "test"},
        ],
    )
    ok_resp = MagicMock(status_code=200)

    def _fake_get(url, timeout):
        if "10.0.0.2" in url:
            raise httpx.TimeoutException("timed out")
        return ok_resp

    with patch(f"{MODULE}.readonly_get", side_effect=_fake_get):
        outcome = agent.collect(manifest_path=manifest_path)

    assert len(outcome.items) == 3
    by_name = {item["name"]: item["data"]["status"] for item in outcome.items}
    assert by_name == {"svc-a": "up", "svc-b": "down", "svc-c": "up"}


def test_non_httperror_exception_does_not_abort_the_rest(agent, tmp_path):
    """A malformed-but-present URL (e.g. an embedded control character or
    stray newline, a plausible copy/paste artifact from the wiki source this
    manifest is hand-built from) makes real httpx.get() raise
    httpx.InvalidURL -- which is NOT an httpx.HTTPError subclass. Before the
    fix, `_probe_one`'s `except httpx.HTTPError` let that propagate straight
    out of `collect()`'s unguarded per-entry loop, silently dropping every
    manifest entry after it for that whole run. Simulated here with a bare
    ValueError (any non-HTTPError exception exercises the same code path)
    raised for the middle of three services; the first and third must still
    be collected."""
    manifest_path = _write_manifest(
        tmp_path,
        [
            {"name": "svc-a", "url": "http://10.0.0.1:1111", "category": "test"},
            {"name": "svc-b", "url": "http://10.0.0.2:2222\n", "category": "test"},
            {"name": "svc-c", "url": "http://10.0.0.3:3333", "category": "test"},
        ],
    )
    ok_resp = MagicMock(status_code=200)

    def _fake_get(url, timeout):
        if "10.0.0.2" in url:
            # httpx.InvalidURL subclasses Exception directly, NOT HTTPError --
            # this is the real exception type a trailing-newline URL raises.
            raise httpx.InvalidURL("Invalid non-printable ASCII character in URL")
        return ok_resp

    with patch(f"{MODULE}.readonly_get", side_effect=_fake_get):
        outcome = agent.collect(manifest_path=manifest_path)

    assert len(outcome.items) == 3
    by_name = {item["name"]: item["data"]["status"] for item in outcome.items}
    assert by_name == {"svc-a": "up", "svc-b": "down", "svc-c": "up"}


def test_entry_missing_url_is_recorded_not_applicable_not_an_error(agent, tmp_path):
    """An entry with a name but no url (url:null) is the manifest's own
    documented convention for "known service, no confirmed pollable
    endpoint" -- it must be recorded as a real item (status=not_applicable)
    so it's visible in resources/snapshots, NOT dropped into .errors like a
    parsing failure (that was the pre-fix behavior, and it also inflated
    status="partial" on every single run since ~1/3 of the real manifest
    intentionally has no url)."""
    manifest_path = _write_manifest(
        tmp_path,
        [
            {"name": "no-url-here", "category": "broken", "note": "internal only"},
            {"name": "good-service", "url": "http://10.0.0.9:9999", "category": "test"},
        ],
    )
    ok_resp = MagicMock(status_code=200)

    with patch(f"{MODULE}.readonly_get", return_value=ok_resp):
        outcome = agent.collect(manifest_path=manifest_path)

    assert len(outcome.items) == 2
    assert outcome.errors == []
    no_url_item = next(i for i in outcome.items if i["name"] == "no-url-here")
    assert no_url_item["data"]["status"] == "not_applicable"
    assert no_url_item["data"]["url"] is None
    assert no_url_item["data"]["reason"] == "internal only"
    good_item = next(i for i in outcome.items if i["name"] == "good-service")
    assert good_item["data"]["status"] == "up"


def test_entry_missing_name_is_a_real_error_and_skipped(agent, tmp_path):
    """Unlike a missing url, a missing name is a genuine schema violation --
    there's nothing to label the item with -- so it stays an error, not a
    recorded item."""
    manifest_path = _write_manifest(
        tmp_path,
        [
            {"url": "http://10.0.0.9:9999", "category": "broken"},
            {"name": "good-service", "url": "http://10.0.0.9:9999", "category": "test"},
        ],
    )
    ok_resp = MagicMock(status_code=200)

    with patch(f"{MODULE}.readonly_get", return_value=ok_resp):
        outcome = agent.collect(manifest_path=manifest_path)

    assert len(outcome.items) == 1
    assert outcome.items[0]["name"] == "good-service"
    assert len(outcome.errors) == 1
    assert "missing name" in outcome.errors[0]


def test_probe_routes_through_readonly_get_not_bare_httpx(agent, tmp_path):
    """L-8a: the probe must go through tools/http_readonly.readonly_get (the
    GET-only client shape every other collector uses), not a bare
    module-level httpx.get -- a bare call bypasses the audit chain the rest
    of the read-only model relies on. Patching the real ``readonly_get``
    symbol in this module (rather than ``httpx.get``) exercises the actual
    call path, not just its presence."""
    manifest_path = _write_manifest(
        tmp_path,
        [{"name": "grafana", "url": "http://203.0.113.15:3002", "category": "monitoring"}],
    )
    ok_resp = MagicMock(status_code=200)

    with patch(f"{MODULE}.readonly_get", return_value=ok_resp) as mock_readonly_get:
        outcome = agent.collect(manifest_path=manifest_path)

    mock_readonly_get.assert_called_once()
    called_url = mock_readonly_get.call_args.args[0] if mock_readonly_get.call_args.args else mock_readonly_get.call_args.kwargs["url"]
    assert called_url == "http://203.0.113.15:3002"
    assert outcome.items[0]["data"]["status"] == "up"
    assert outcome.items[0]["data"]["http_status"] == 200


def test_default_manifest_path_is_the_bundled_json_file():
    """Sanity check that DEFAULT_MANIFEST_PATH points at the real bundled
    manifest shipped alongside this module (not an accidental typo'd path)."""
    from pathlib import Path

    from infra_brain.agents.homelab_services import DEFAULT_MANIFEST_PATH

    assert Path(DEFAULT_MANIFEST_PATH).name == "homelab_services_manifest.json"
    assert Path(DEFAULT_MANIFEST_PATH).exists()
