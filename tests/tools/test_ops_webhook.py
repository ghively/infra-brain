"""Tests for the generic outbound ops-alert webhook (OB-1)."""

from unittest.mock import MagicMock, patch

import pytest

from infra_brain.tools.ops_webhook import send_ops_alert


def _settings(**overrides):
    from types import SimpleNamespace

    defaults = dict(
        ops_webhook_url="",
        ops_webhook_token="",
        api_timeout_seconds=30,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_send_ops_alert_noop_when_unconfigured():
    """Empty OPS_WEBHOOK_URL must be a clean no-op that returns True (not an error)."""
    with (
        patch("infra_brain.tools.ops_webhook.get_settings", return_value=_settings()),
        patch("infra_brain.tools.ops_webhook.httpx.post") as mock_post,
    ):
        result = send_ops_alert("collection_health", ["linux: stale"])

    assert result is True
    mock_post.assert_not_called()


def test_send_ops_alert_noop_when_no_messages():
    with patch("infra_brain.tools.ops_webhook.httpx.post") as mock_post:
        result = send_ops_alert("collection_health", [])

    assert result is True
    mock_post.assert_not_called()


def test_send_ops_alert_posts_when_configured():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_session = MagicMock()

    with (
        patch(
            "infra_brain.tools.ops_webhook.get_settings",
            return_value=_settings(ops_webhook_url="https://hooks.example.com/alert"),
        ),
        patch("infra_brain.tools.ops_webhook.httpx.post", return_value=mock_response) as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        result = send_ops_alert("collection_health", ["linux: stale — last data 9h ago"])

    assert result is True
    mock_post.assert_called_once()
    call = mock_post.call_args
    assert call.args[0] == "https://hooks.example.com/alert"
    assert call.kwargs["json"]["category"] == "collection_health"
    assert call.kwargs["json"]["messages"] == ["linux: stale — last data 9h ago"]


def test_send_ops_alert_adds_bearer_token_when_configured():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_session = MagicMock()

    with (
        patch(
            "infra_brain.tools.ops_webhook.get_settings",
            return_value=_settings(
                ops_webhook_url="https://hooks.example.com/alert",
                ops_webhook_token="secret-tok",
            ),
        ),
        patch("infra_brain.tools.ops_webhook.httpx.post", return_value=mock_response) as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        send_ops_alert("agent_anomaly", ["QueryAgent: 12 denied tool calls"])

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer secret-tok"


def test_send_ops_alert_returns_false_on_http_failure():
    mock_session = MagicMock()

    with (
        patch(
            "infra_brain.tools.ops_webhook.get_settings",
            return_value=_settings(ops_webhook_url="https://hooks.example.com/alert"),
        ),
        patch(
            "infra_brain.tools.ops_webhook.httpx.post",
            side_effect=RuntimeError("connection refused"),
        ),
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        result = send_ops_alert("collection_health", ["linux: stale"])

    assert result is False


def test_send_ops_alert_includes_dedup_key_when_provided():
    """TRK-242 (GitLab #113): dedup_key correlates repeat alerts into one
    incident on PagerDuty/Opsgenie's side — must be included in the payload
    verbatim when the caller provides it."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_session = MagicMock()

    with (
        patch(
            "infra_brain.tools.ops_webhook.get_settings",
            return_value=_settings(ops_webhook_url="https://hooks.example.com/alert"),
        ),
        patch("infra_brain.tools.ops_webhook.httpx.post", return_value=mock_response) as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        result = send_ops_alert(
            "drift_incident",
            ["cloud/web01: config_drift on 'instance_type'"],
            dedup_key="drift:web01:instance_type",
        )

    assert result is True
    assert mock_post.call_args.kwargs["json"]["dedup_key"] == "drift:web01:instance_type"


def test_send_ops_alert_omits_dedup_key_when_not_provided():
    """No dedup_key given (existing callers: collection_health/scheduler_deadman/
    agent_anomaly) must not send the field at all — no shape change for them."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_session = MagicMock()

    with (
        patch(
            "infra_brain.tools.ops_webhook.get_settings",
            return_value=_settings(ops_webhook_url="https://hooks.example.com/alert"),
        ),
        patch("infra_brain.tools.ops_webhook.httpx.post", return_value=mock_response) as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        send_ops_alert("collection_health", ["linux: stale"])

    assert "dedup_key" not in mock_post.call_args.kwargs["json"]


def test_send_ops_alert_blocked_by_write_gate_on_pan_in_payload():
    """DLP layer (write_gate) must still apply to this outbound write path."""
    mock_session = MagicMock()

    with (
        patch(
            "infra_brain.tools.ops_webhook.get_settings",
            return_value=_settings(ops_webhook_url="https://hooks.example.com/alert"),
        ),
        patch("infra_brain.tools.ops_webhook.httpx.post") as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(PermissionError):
            # Luhn-valid PAN
            send_ops_alert("collection_health", ["card 4111111111111111 leaked"])

    mock_post.assert_not_called()
