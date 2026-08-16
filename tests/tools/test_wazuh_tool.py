"""Tests for tools/wazuh_tool.py — the Wazuh Manager API + Indexer tools."""

from unittest.mock import MagicMock, patch

import pytest

from infra_brain.tools.wazuh_tool import wazuh_agents_tool, wazuh_indexer_alerts_tool

MODULE = "infra_brain.tools.wazuh_tool"


def _capturing_get(captured: dict):
    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"_shards": {"total": 1}, "hits": {"hits": []}}
        return resp

    return _fake_get


def test_indexer_tool_still_authenticates_from_settings():
    """H-3 moved the indexer credentials out of the tool signature. They must
    still REACH the HTTP layer — dropping them would silently turn every
    indexer search into an unauthenticated 401, which is the obvious way this
    fix could regress."""
    captured: dict = {}
    fake_settings = MagicMock()
    fake_settings.wazuh_indexer_url = "https://indexer.test:9200"
    fake_settings.wazuh_indexer_username = "  indexer-admin  "  # whitespace is stripped
    fake_settings.wazuh_indexer_password = "IndexerPw!42"

    with (
        patch(f"{MODULE}.readonly_get", side_effect=_capturing_get(captured)),
        patch(f"{MODULE}.get_settings", return_value=fake_settings),
    ):
        wazuh_indexer_alerts_tool.func(
            indexer_url="https://indexer.test:9200",
            ssl_verify=False,
            timeout=30,
            since_iso="2026-08-09T00:00:00",
        )

    assert captured["kwargs"]["auth"] == ("indexer-admin", "IndexerPw!42")


def test_manager_tool_still_sends_a_bearer_token_resolved_in_process():
    """The Manager tools no longer take ``token``; they must still send the
    Authorization header, sourced from the token published by
    ``get_wazuh_token`` (or, failing that, a fresh authentication)."""
    captured: dict = {}
    fake_settings = MagicMock()
    fake_settings.wazuh_url = "https://wazuh.test:55000"

    with (
        patch(f"{MODULE}.readonly_get", side_effect=_capturing_get(captured)),
        patch(f"{MODULE}._active_manager_token", return_value="resolved.jwt.value"),
        patch(f"{MODULE}.get_settings", return_value=fake_settings),
    ):
        wazuh_agents_tool.func(base_url="https://wazuh.test:55000", ssl_verify=False, timeout=30)

    assert captured["kwargs"]["headers"] == {"Authorization": "Bearer resolved.jwt.value"}


# --- MEDIUM-1 — credential/host binding -------------------------------------
#
# _auth_headers() (Manager) and the indexer auth block used to attach the
# resolved credential to WHATEVER base_url/indexer_url the caller passed, with
# no relationship enforced between the credential and the target host. These
# tools are only closed off from LLM misuse by convention (they are not in any
# TOOLS list), and ReadOnlyToolValidator inspects `inp["url"]`, not
# `inp["base_url"]`/`inp["indexer_url"]`, so it cannot backstop this either.
# Each tool must now refuse (loudly) to attach its credential when the passed
# URL doesn't match the configured setting for that credential.


def test_manager_tool_refuses_mismatched_base_url():
    """A base_url that does not match settings.wazuh_url must NEVER get the
    Manager JWT attached — and the refusal must be loud, not a silent skip."""
    captured: dict = {}
    fake_settings = MagicMock()
    fake_settings.wazuh_url = "https://wazuh.test:55000"

    with (
        patch(f"{MODULE}.readonly_get", side_effect=_capturing_get(captured)),
        patch(f"{MODULE}._active_manager_token", return_value="resolved.jwt.value"),
        patch(f"{MODULE}.get_settings", return_value=fake_settings),
    ):
        with pytest.raises(Exception, match="wazuh_url"):
            wazuh_agents_tool.func(
                base_url="https://attacker.example/wazuh", ssl_verify=False, timeout=30
            )

    # The credential must never have reached the HTTP layer at all.
    assert captured == {}


def test_indexer_tool_refuses_mismatched_indexer_url():
    """An indexer_url that does not match settings.wazuh_indexer_url must
    NEVER get the indexer basic-auth credential attached."""
    captured: dict = {}
    fake_settings = MagicMock()
    fake_settings.wazuh_indexer_url = "https://indexer.test:9200"
    fake_settings.wazuh_indexer_username = "indexer-admin"
    fake_settings.wazuh_indexer_password = "IndexerPw!42"

    with (
        patch(f"{MODULE}.readonly_get", side_effect=_capturing_get(captured)),
        patch(f"{MODULE}.get_settings", return_value=fake_settings),
    ):
        with pytest.raises(Exception, match="wazuh_indexer_url"):
            wazuh_indexer_alerts_tool.func(
                indexer_url="https://attacker.example/indexer",
                ssl_verify=False,
                timeout=30,
                since_iso="2026-08-09T00:00:00",
            )

    assert captured == {}


def test_get_wazuh_token_publishes_the_token_for_the_tools():
    """The in-process handoff that replaces the token argument: a successful
    ``get_wazuh_token`` must make that exact JWT the one the tools then use."""
    from infra_brain.tools import wazuh_tool

    settings = MagicMock()
    settings.wazuh_url = "https://wazuh.test:55000"
    settings.wazuh_username = "wazuh-wui"
    settings.wazuh_password = "s3cret"
    settings.wazuh_ssl_verify = False
    settings.api_timeout_seconds = 30

    with patch(f"{MODULE}._wazuh_authenticate", return_value="fresh.jwt.value") as mock_auth:
        returned = wazuh_tool.get_wazuh_token(settings)
        # The tools must now resolve the SAME token without re-authenticating.
        resolved = wazuh_tool._active_manager_token()

    assert returned == "fresh.jwt.value"
    assert resolved == "fresh.jwt.value"
    mock_auth.assert_called_once()


def test_indexer_alerts_tool_requests_only_the_fields_the_agent_uses():
    """The query must _source-filter, not pull whole alert documents.

    WazuhAgent reads exactly rule.description, rule.level, agent.name,
    timestamp (and id as a name fallback). Everything else — most importantly
    full_log — was fetched, scanned and discarded.

    That surplus blocked ingestion outright. full_log carries raw command
    lines, and dlp.py fail-closes on any Luhn-valid 13-19 digit run in tool
    output. An Ansible temp dir (ansible-tmp-<epoch>.<frac>-<pid>-<13 digits>)
    produces pid+suffix = 16 digits starting "65", a real Discover IIN, so it
    passed both Luhn and the _matches_card_network corroboration and pinned
    wazuh at "partial" with zero alerts. Not fetching the field is the fix;
    dlp.py is deliberately left untouched.
    """
    import json as _json
    from urllib.parse import parse_qs, urlparse

    captured = {}

    def _fake_get(url, **kwargs):
        captured["url"] = url
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"_shards": {"total": 1}, "hits": {"hits": []}}
        return resp

    fake_settings = MagicMock()
    fake_settings.wazuh_indexer_url = "https://indexer.test:9200"
    fake_settings.wazuh_indexer_username = "admin"
    fake_settings.wazuh_indexer_password = "pw"

    with (
        patch(f"{MODULE}.readonly_get", side_effect=_fake_get),
        # H-3: indexer credentials are resolved from settings inside the tool,
        # never taken as arguments — see the module docstring.
        patch(f"{MODULE}.get_settings", return_value=fake_settings),
    ):
        wazuh_indexer_alerts_tool.func(
            indexer_url="https://indexer.test:9200",
            ssl_verify=False,
            timeout=30,
            since_iso="2026-08-09T00:00:00",
        )

    source = parse_qs(urlparse(captured["url"]).query)["source"][0]
    body = _json.loads(source)
    assert "_source" in body, "query must restrict returned fields"
    fields = set(body["_source"])
    assert fields == {"timestamp", "id", "rule.level", "rule.description", "agent.name"}
    # The field that carried the false positive must not be requested.
    assert "full_log" not in fields
