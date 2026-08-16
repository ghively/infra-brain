"""Tests for the outbound Slack reply helpers (GitLab #111 chatops bot)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from infra_brain.tools.slack import send_slack_message, send_slack_response_url


def _settings(**overrides):
    defaults = dict(slack_bot_token="", api_timeout_seconds=30)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _gate_patch():
    mock_session = MagicMock()
    ctx = patch("infra_brain.callbacks.write_gate.get_session")
    return ctx, mock_session


def test_send_slack_message_noop_when_unconfigured():
    """Empty SLACK_BOT_TOKEN must be a clean no-op that returns True (not an error)."""
    with (
        patch("infra_brain.tools.slack.get_settings", return_value=_settings()),
        patch("infra_brain.tools.slack.httpx.post") as mock_post,
    ):
        result = send_slack_message("C123", "hello")

    assert result is True
    mock_post.assert_not_called()


def test_send_slack_message_posts_when_configured():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"ok": True}
    mock_session = MagicMock()

    with (
        patch(
            "infra_brain.tools.slack.get_settings",
            return_value=_settings(slack_bot_token="xoxb-test"),
        ),
        patch("infra_brain.tools.slack.httpx.post", return_value=mock_response) as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        result = send_slack_message("C123", "drift detected on db01", thread_ts="123.456")

    assert result is True
    mock_post.assert_called_once()
    call = mock_post.call_args
    assert call.args[0] == "https://slack.com/api/chat.postMessage"
    assert call.kwargs["json"]["channel"] == "C123"
    assert call.kwargs["json"]["text"] == "drift detected on db01"
    assert call.kwargs["json"]["thread_ts"] == "123.456"
    assert call.kwargs["headers"]["Authorization"] == "Bearer xoxb-test"


def test_send_slack_message_returns_false_when_slack_rejects():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"ok": False, "error": "channel_not_found"}
    mock_session = MagicMock()

    with (
        patch(
            "infra_brain.tools.slack.get_settings",
            return_value=_settings(slack_bot_token="xoxb-test"),
        ),
        patch("infra_brain.tools.slack.httpx.post", return_value=mock_response),
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        result = send_slack_message("C123", "hello")

    assert result is False


def test_send_slack_message_returns_false_on_http_failure():
    mock_session = MagicMock()

    with (
        patch(
            "infra_brain.tools.slack.get_settings",
            return_value=_settings(slack_bot_token="xoxb-test"),
        ),
        patch("infra_brain.tools.slack.httpx.post", side_effect=RuntimeError("connection refused")),
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        result = send_slack_message("C123", "hello")

    assert result is False


def test_send_slack_message_blocked_by_write_gate_on_pan_in_payload():
    mock_session = MagicMock()

    with (
        patch(
            "infra_brain.tools.slack.get_settings",
            return_value=_settings(slack_bot_token="xoxb-test"),
        ),
        patch("infra_brain.tools.slack.httpx.post") as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(PermissionError):
            send_slack_message("C123", "card 4111111111111111 leaked")

    mock_post.assert_not_called()


def test_send_slack_response_url_posts():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_session = MagicMock()

    with (
        patch("infra_brain.tools.slack.get_settings", return_value=_settings()),
        patch("infra_brain.tools.slack.httpx.post", return_value=mock_response) as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        result = send_slack_response_url("https://hooks.slack.com/commands/abc", "the answer")

    assert result is True
    mock_post.assert_called_once()
    assert mock_post.call_args.args[0] == "https://hooks.slack.com/commands/abc"
    assert mock_post.call_args.kwargs["json"]["text"] == "the answer"


def test_send_slack_response_url_blocked_by_write_gate_on_pan():
    mock_session = MagicMock()

    with (
        patch("infra_brain.tools.slack.get_settings", return_value=_settings()),
        patch("infra_brain.tools.slack.httpx.post") as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(PermissionError):
            send_slack_response_url("https://hooks.slack.com/commands/abc", "4111111111111111")

    mock_post.assert_not_called()


def test_send_slack_response_url_refuses_non_slack_host():
    """A leaked/rotated-late signing secret would otherwise let a caller
    make infra-brain POST arbitrary JSON to any URL -- pinned to Slack's
    own hostname as defense-in-depth."""
    with (
        patch("infra_brain.tools.slack.get_settings", return_value=_settings()),
        patch("infra_brain.tools.slack.httpx.post") as mock_post,
    ):
        result = send_slack_response_url("https://evil.example.com/steal", "the answer")

    assert result is False
    mock_post.assert_not_called()
