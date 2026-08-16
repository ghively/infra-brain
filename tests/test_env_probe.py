from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from infra_brain.env_probe import (
    ProbeResult,
    _probe_ansible_inventory,
    _probe_container_registries,
    _probe_gitlab,
    _probe_llm_base_url,
    probe_environment,
    propose_gaps,
)


def _settings(**over):
    base = dict(
        gitlab_url="",
        gitlab_token="",
        gitlab_ssl_verify=True,
        ansible_inventory_path="",
        llm_provider="openai",
        openai_base_url="",
        openai_api_key="",
        openai_model="gpt-4o",
        anthropic_base_url="",
        anthropic_api_key="",
        llm_model="claude-sonnet-5",
        container_registries="",
        container_registry_token="",
        container_registry_ssl_verify=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_probe_gitlab_returns_none_when_unconfigured():
    with patch("infra_brain.env_probe.get_settings", return_value=_settings()):
        assert _probe_gitlab() is None


def test_probe_gitlab_reachable():
    settings = _settings(gitlab_url="https://gitlab.example", gitlab_token="tok")
    mock_resp = MagicMock(status_code=200)
    with (
        patch("infra_brain.env_probe.get_settings", return_value=settings),
        patch("infra_brain.env_probe.httpx.get", return_value=mock_resp),
    ):
        r = _probe_gitlab()
    assert r.configured and r.reachable


def test_probe_gitlab_unreachable_on_connect_error():
    settings = _settings(gitlab_url="https://gitlab.example", gitlab_token="tok")
    with (
        patch("infra_brain.env_probe.get_settings", return_value=settings),
        patch("infra_brain.env_probe.httpx.get", side_effect=httpx.ConnectError("refused")),
    ):
        r = _probe_gitlab()
    assert r.configured and not r.reachable


def test_probe_gitlab_unreachable_on_bad_status():
    settings = _settings(gitlab_url="https://gitlab.example", gitlab_token="badtok")
    mock_resp = MagicMock(status_code=401)
    with (
        patch("infra_brain.env_probe.get_settings", return_value=settings),
        patch("infra_brain.env_probe.httpx.get", return_value=mock_resp),
    ):
        r = _probe_gitlab()
    assert r.configured and not r.reachable


def test_probe_ansible_inventory_missing_path(tmp_path):
    settings = _settings(ansible_inventory_path=str(tmp_path / "nope"))
    with patch("infra_brain.env_probe.get_settings", return_value=settings):
        r = _probe_ansible_inventory()
    assert r.configured and not r.reachable


def test_probe_ansible_inventory_exists(tmp_path):
    inv = tmp_path / "hosts"
    inv.write_text("[all]\n")
    settings = _settings(ansible_inventory_path=str(inv))
    with patch("infra_brain.env_probe.get_settings", return_value=settings):
        r = _probe_ansible_inventory()
    assert r.configured and r.reachable


def test_probe_llm_base_url_unconfigured_returns_none():
    with patch("infra_brain.env_probe.get_settings", return_value=_settings()):
        assert _probe_llm_base_url() is None


def test_probe_llm_base_url_reachable():
    settings = _settings(
        llm_provider="openai",
        openai_base_url="https://openrouter.ai/api/v1",
        openai_api_key="sk-or-real",
        openai_model="anthropic/claude-sonnet-5",
    )
    mock_resp = MagicMock(status_code=200)
    with (
        patch("infra_brain.env_probe.get_settings", return_value=settings),
        patch("infra_brain.env_probe.httpx.post", return_value=mock_resp) as mock_post,
    ):
        r = _probe_llm_base_url()
    assert r.configured and r.reachable
    assert mock_post.call_args[0][0] == "https://openrouter.ai/api/v1/chat/completions"


def test_probe_llm_base_url_auth_rejected_is_a_gap():
    """The exact real-world case this check exists for: a key that answers a
    bare GET but is rejected by the actual completions endpoint (P3.4's
    stale-OpenRouter-key finding)."""
    settings = _settings(
        llm_provider="openai",
        openai_base_url="https://openrouter.ai/api/v1",
        openai_api_key="sk-or-stale",
        openai_model="anthropic/claude-sonnet-5",
    )
    mock_resp = MagicMock(status_code=401)
    with (
        patch("infra_brain.env_probe.get_settings", return_value=settings),
        patch("infra_brain.env_probe.httpx.post", return_value=mock_resp),
    ):
        r = _probe_llm_base_url()
    assert r.configured and not r.reachable


def test_probe_llm_base_url_anthropic_uses_native_messages_shape():
    """The anthropic-provider probe must send the same wire shape ChatAnthropic
    actually sends (llm.py ~106-107): native Messages API at /v1/messages with
    an x-api-key header, not the OpenAI Chat Completions shape at
    /chat/completions with an Authorization: Bearer header. A gateway that
    only speaks native Anthropic would otherwise be wrongly flagged as a gap
    (and an incidental OpenAI-shim stub could be wrongly reported healthy)."""
    settings = _settings(
        llm_provider="anthropic",
        anthropic_base_url="https://anthropic-gateway.example",
        anthropic_api_key="sk-ant-real",
        llm_model="claude-sonnet-5",
    )
    mock_resp = MagicMock(status_code=200)
    with (
        patch("infra_brain.env_probe.get_settings", return_value=settings),
        patch("infra_brain.env_probe.httpx.post", return_value=mock_resp) as mock_post,
    ):
        r = _probe_llm_base_url()
    assert r.configured and r.reachable
    called_url = mock_post.call_args[0][0]
    called_headers = mock_post.call_args.kwargs["headers"]
    assert called_url == "https://anthropic-gateway.example/v1/messages"
    assert called_headers.get("x-api-key") == "sk-ant-real"
    assert called_headers.get("anthropic-version")
    assert "Authorization" not in called_headers


def test_probe_llm_base_url_anthropic_auth_rejected_is_a_gap():
    settings = _settings(
        llm_provider="anthropic",
        anthropic_base_url="https://anthropic-gateway.example",
        anthropic_api_key="sk-ant-stale",
        llm_model="claude-sonnet-5",
    )
    mock_resp = MagicMock(status_code=401)
    with (
        patch("infra_brain.env_probe.get_settings", return_value=settings),
        patch("infra_brain.env_probe.httpx.post", return_value=mock_resp),
    ):
        r = _probe_llm_base_url()
    assert r.configured and not r.reachable


def test_probe_container_registries_multiple_urls():
    settings = _settings(container_registries="https://reg1.example, https://reg2.example")
    ok_resp = MagicMock(status_code=200)
    down_resp = MagicMock(status_code=503)

    def fake_get(url, **kw):
        return ok_resp if "reg1" in url else down_resp

    with (
        patch("infra_brain.env_probe.get_settings", return_value=settings),
        patch("infra_brain.env_probe.httpx.get", side_effect=fake_get),
    ):
        results = _probe_container_registries()
    by_name = {r.name: r for r in results}
    assert by_name["container_registry:https://reg1.example"].reachable
    assert not by_name["container_registry:https://reg2.example"].reachable


def test_probe_environment_aggregates_configured_only():
    settings = _settings(gitlab_url="https://gitlab.example", gitlab_token="tok")
    mock_resp = MagicMock(status_code=200)
    with (
        patch("infra_brain.env_probe.get_settings", return_value=settings),
        patch("infra_brain.env_probe.httpx.get", return_value=mock_resp),
    ):
        results = probe_environment()
    assert len(results) == 1
    assert results[0].name == "gitlab"


def test_propose_gaps_files_a_proposal_for_each_unreachable_result():
    results = [
        ProbeResult("gitlab", True, False, "connection refused"),
        ProbeResult("ansible_inventory", True, True, "ok"),
    ]
    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = None
    with patch("infra_brain.env_probe.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        created = propose_gaps(results)
    assert created == ["env-probe:gitlab"]
    mock_session.add.assert_called_once()
    added = mock_session.add.call_args[0][0]
    assert added.source == "env-probe:gitlab"
    assert added.type == "connectivity-gap"
    assert added.confidence == 1.0


def test_propose_gaps_skips_when_already_pending():
    results = [ProbeResult("gitlab", True, False, "connection refused")]
    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = object()
    with patch("infra_brain.env_probe.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        created = propose_gaps(results)
    assert created == []
    mock_session.add.assert_not_called()


def test_propose_gaps_no_gaps_when_everything_reachable():
    results = [ProbeResult("gitlab", True, True, "ok")]
    created = propose_gaps(results)
    assert created == []
