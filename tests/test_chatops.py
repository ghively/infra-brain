"""Tests for the inbound Slack/Teams chatops bot router (GitLab #111).

Router-level: signature verification (Slack HMAC v0 / Teams outgoing-webhook
HMAC), the fail-closed feature flag, the Slack Events API subscription
challenge, and that inbound messages route through the shared chat agent and
back out through the sanctioned Slack/Teams reply helpers (mocked here —
these tests never make a real network call).
"""

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from infra_brain.main import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _slack_headers(body: bytes, signing_secret: str, timestamp: str | None = None) -> dict:
    ts = timestamp or str(int(time.time()))
    basestring = f"v0:{ts}:{body.decode()}"
    sig = "v0=" + hmac.new(signing_secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def _teams_headers(body: bytes, secret_b64: str) -> dict:
    key = base64.b64decode(secret_b64)
    mac = hmac.new(key, body, hashlib.sha256).digest()
    return {"Authorization": "HMAC " + base64.b64encode(mac).decode()}


# "T1" / "TEST-TENANT" match the team_id / channelData.tenant.id every
# payload below uses -- the allowlist is deny-by-default (empty string here
# would deny everything, per _require_allowed_scope), so tests exercising
# the happy path must be explicitly on it.
CHATOPS_SETTINGS = MagicMock(
    chatops_enabled=True,
    slack_signing_secret="slack-signing-secret",
    slack_bot_token="xoxb-test",
    teams_outgoing_webhook_secret=base64.b64encode(b"teams-secret-key").decode(),
    chat_rate_limit_per_hour=30,
    chatops_allowed_slack_team_ids="T1",
    chatops_allowed_teams_tenant_ids="TEST-TENANT",
)


def _patched_settings(**overrides):
    settings = MagicMock(
        chatops_enabled=True,
        slack_signing_secret="slack-signing-secret",
        slack_bot_token="xoxb-test",
        teams_outgoing_webhook_secret=base64.b64encode(b"teams-secret-key").decode(),
        chat_rate_limit_per_hour=30,
        chatops_allowed_slack_team_ids="T1",
        chatops_allowed_teams_tenant_ids="TEST-TENANT",
    )
    for k, v in overrides.items():
        setattr(settings, k, v)
    return settings


# ─────────────────────────────────────────────────────────────────────────────
# Feature flag / auth
# ─────────────────────────────────────────────────────────────────────────────


def test_chatops_disabled_by_default_403(client):
    """chatops_enabled defaults False — every route must fail closed."""
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    with patch(
        "infra_brain.api.routers.chatops.get_settings",
        return_value=_patched_settings(chatops_enabled=False),
    ):
        resp = client.post(
            "/webhooks/chatops/slack/events",
            data=body,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 403


def test_slack_events_invalid_signature_403(client):
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    with patch("infra_brain.api.routers.chatops.get_settings", return_value=_patched_settings()):
        resp = client.post(
            "/webhooks/chatops/slack/events",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": "v0=deadbeef",
            },
        )
    assert resp.status_code == 403


def test_slack_events_stale_timestamp_rejected(client):
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    stale_ts = str(int(time.time()) - 60 * 30)  # 30 minutes old
    headers = _slack_headers(body, "slack-signing-secret", timestamp=stale_ts)
    with patch("infra_brain.api.routers.chatops.get_settings", return_value=_patched_settings()):
        resp = client.post(
            "/webhooks/chatops/slack/events",
            data=body,
            headers={"Content-Type": "application/json", **headers},
        )
    assert resp.status_code == 403


def test_teams_invalid_hmac_403(client):
    body = json.dumps({"text": "hi", "from": {"id": "u1"}, "conversation": {"id": "c1"}}).encode()
    with patch("infra_brain.api.routers.chatops.get_settings", return_value=_patched_settings()):
        resp = client.post(
            "/webhooks/chatops/teams",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "HMAC bm90dmFsaWQ="},
        )
    assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Slack Events API
# ─────────────────────────────────────────────────────────────────────────────


def test_slack_url_verification_challenge_echoed(client):
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    headers = _slack_headers(body, "slack-signing-secret")
    with patch("infra_brain.api.routers.chatops.get_settings", return_value=_patched_settings()):
        resp = client.post(
            "/webhooks/chatops/slack/events",
            data=body,
            headers={"Content-Type": "application/json", **headers},
        )
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc123"}


def test_slack_app_mention_answers_and_posts_reply(client):
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "app_mention",
            "text": "<@BOT> is db01 drifting?",
            "channel": "C123",
            "user": "U1",
            "ts": "111.222",
        },
    }
    body = json.dumps(payload).encode()
    headers = _slack_headers(body, "slack-signing-secret")

    mock_graph = object()
    with (
        patch("infra_brain.api.routers.chatops.get_settings", return_value=_patched_settings()),
        patch("infra_brain.chat.agent.build_chat_agent", new=AsyncMock(return_value=mock_graph)),
        patch(
            "infra_brain.chat.agent.answer_message",
            new=AsyncMock(return_value="db01 has 0 open drift events"),
        ),
        patch("infra_brain.tools.slack.send_slack_message", return_value=True) as mock_send,
        patch("infra_brain.ratelimit.rate_limit_exceeded", return_value=False),
        patch("infra_brain.ratelimit.token_budget_exceeded", return_value=False),
    ):
        resp = client.post(
            "/webhooks/chatops/slack/events",
            data=body,
            headers={"Content-Type": "application/json", **headers},
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_send.assert_called_once()
    assert mock_send.call_args.args[0] == "C123"
    assert "0 open drift events" in mock_send.call_args.args[1]


def test_slack_bot_message_ignored(client):
    """Must not reply to its own bot messages — avoids an infinite reply loop."""
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event": {"type": "message", "text": "hi", "channel": "C123", "bot_id": "B1"},
    }
    body = json.dumps(payload).encode()
    headers = _slack_headers(body, "slack-signing-secret")

    with (
        patch("infra_brain.api.routers.chatops.get_settings", return_value=_patched_settings()),
        patch("infra_brain.tools.slack.send_slack_message") as mock_send,
    ):
        resp = client.post(
            "/webhooks/chatops/slack/events",
            data=body,
            headers={"Content-Type": "application/json", **headers},
        )

    assert resp.status_code == 200
    mock_send.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Slack slash command
# ─────────────────────────────────────────────────────────────────────────────


def test_slack_command_acks_immediately_and_posts_to_response_url(client):
    form = {
        "text": "what is drifting on db01?",
        "response_url": "https://hooks.slack.com/commands/1/2/3",
        "user_id": "U1",
        "team_id": "T1",
        "channel_id": "C1",
    }
    body = "&".join(f"{k}={v}" for k, v in form.items()).encode()
    # form-encode properly
    from urllib.parse import urlencode

    body = urlencode(form).encode()
    headers = _slack_headers(body, "slack-signing-secret")

    mock_graph = object()
    with (
        patch("infra_brain.api.routers.chatops.get_settings", return_value=_patched_settings()),
        patch("infra_brain.chat.agent.build_chat_agent", new=AsyncMock(return_value=mock_graph)),
        patch(
            "infra_brain.chat.agent.answer_message",
            new=AsyncMock(return_value="answer text"),
        ),
        patch("infra_brain.tools.slack.send_slack_response_url", return_value=True) as mock_send,
        patch("infra_brain.ratelimit.rate_limit_exceeded", return_value=False),
        patch("infra_brain.ratelimit.token_budget_exceeded", return_value=False),
    ):
        resp = client.post(
            "/webhooks/chatops/slack/command",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", **headers},
        )

    assert resp.status_code == 200
    assert resp.json()["response_type"] == "ephemeral"
    mock_send.assert_called_once()
    assert mock_send.call_args.args[0] == "https://hooks.slack.com/commands/1/2/3"
    assert mock_send.call_args.args[1] == "answer text"


# ─────────────────────────────────────────────────────────────────────────────
# Teams outgoing webhook
# ─────────────────────────────────────────────────────────────────────────────


def test_teams_webhook_answers_synchronously(client):
    secret_b64 = base64.b64encode(b"teams-secret-key").decode()
    payload = {
        "text": "is db01 drifting?",
        "from": {"id": "u1"},
        "conversation": {"id": "c1"},
        "channelData": {"tenant": {"id": "TEST-TENANT"}},
    }
    body = json.dumps(payload).encode()
    headers = _teams_headers(body, secret_b64)

    mock_graph = object()
    with (
        patch(
            "infra_brain.api.routers.chatops.get_settings",
            return_value=_patched_settings(teams_outgoing_webhook_secret=secret_b64),
        ),
        patch("infra_brain.chat.agent.build_chat_agent", new=AsyncMock(return_value=mock_graph)),
        patch(
            "infra_brain.chat.agent.answer_message",
            new=AsyncMock(return_value="db01 has 0 open drift events"),
        ),
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
        patch("infra_brain.ratelimit.rate_limit_exceeded", return_value=False),
        patch("infra_brain.ratelimit.token_budget_exceeded", return_value=False),
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: MagicMock()
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        resp = client.post(
            "/webhooks/chatops/teams",
            data=body,
            headers={"Content-Type": "application/json", **headers},
        )

    assert resp.status_code == 200
    assert resp.json() == {"type": "message", "text": "db01 has 0 open drift events"}


def test_chatops_enabled_but_secrets_unconfigured_still_403(client):
    """The single most important fail-closed property of this module: a flag
    flip to True with no real Slack app/Teams bot secrets provisioned must
    still 403 everything, since "merge while dark" is the stated
    justification for landing this feature before real credentials exist."""
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    with patch(
        "infra_brain.api.routers.chatops.get_settings",
        return_value=_patched_settings(
            slack_signing_secret="", teams_outgoing_webhook_secret=""
        ),
    ):
        resp = client.post(
            "/webhooks/chatops/slack/events",
            data=body,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 403


def test_slack_events_rejects_team_not_on_allowlist(client):
    """A valid signature only proves "our Slack app sent this" -- it says
    nothing about which workspace. A team_id absent from the allowlist must
    be rejected even with an otherwise-valid, correctly-signed request."""
    with patch(
        "infra_brain.api.routers.chatops.get_settings",
        return_value=_patched_settings(),
    ):
        # url_verification itself has no team_id and isn't scope-checked --
        # use app_mention instead, which is.
        mention_payload = {
            "type": "event_callback",
            "team_id": "T-NOT-ALLOWED",
            "event": {"type": "app_mention", "channel": "C1", "text": "hi", "ts": "1.1"},
        }
        mention_body = json.dumps(mention_payload).encode()
        mention_headers = _slack_headers(mention_body, "slack-signing-secret")
        resp = client.post(
            "/webhooks/chatops/slack/events",
            data=mention_body,
            headers={"Content-Type": "application/json", **mention_headers},
        )
    assert resp.status_code == 403


def test_teams_rejects_tenant_not_on_allowlist(client):
    secret_b64 = base64.b64encode(b"teams-secret-key").decode()
    payload = {
        "text": "is db01 drifting?",
        "from": {"id": "u1"},
        "conversation": {"id": "c1"},
        "channelData": {"tenant": {"id": "TENANT-NOT-ALLOWED"}},
    }
    body = json.dumps(payload).encode()
    headers = _teams_headers(body, secret_b64)

    with patch(
        "infra_brain.api.routers.chatops.get_settings",
        return_value=_patched_settings(teams_outgoing_webhook_secret=secret_b64),
    ):
        resp = client.post(
            "/webhooks/chatops/teams",
            data=body,
            headers={"Content-Type": "application/json", **headers},
        )
    assert resp.status_code == 403


def test_teams_null_tenant_fails_closed_403_not_500(client):
    """channelData.tenant explicitly null (plausible for personal/1:1 Teams
    conversations) must fail closed with 403, not crash with an unhandled
    AttributeError/500 -- `.get('tenant', {})` only substitutes the default
    when the key is *absent*, not when it is present with value None, so the
    extraction must explicitly guard against that case."""
    secret_b64 = base64.b64encode(b"teams-secret-key").decode()
    payload = {
        "text": "is db01 drifting?",
        "from": {"id": "u1"},
        "conversation": {"id": "c1"},
        "channelData": {"tenant": None},
    }
    body = json.dumps(payload).encode()
    headers = _teams_headers(body, secret_b64)

    with patch(
        "infra_brain.api.routers.chatops.get_settings",
        return_value=_patched_settings(teams_outgoing_webhook_secret=secret_b64),
    ):
        resp = client.post(
            "/webhooks/chatops/teams",
            data=body,
            headers={"Content-Type": "application/json", **headers},
        )
    assert resp.status_code == 403
