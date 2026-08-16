"""Outbound Slack replies for the chatops bot (GitLab #111).

Plain functions (not LangChain ``@tool``s — mirrors tools/jira.py,
tools/confluence.py, tools/ops_webhook.py) — gated by ``gate_external_write``
(F-004.3 / R2 layer 2) before any ``httpx.post`` leaves the process, same as
every other sanctioned external write.

Two outbound shapes, because Slack's two inbound surfaces reply differently:

* Events API (``app_mention`` / DM) — no synchronous reply channel; the bot
  must call ``chat.postMessage`` back with the bot token.
* Slash commands — must ack within Slack's 3s window, so the real answer is
  posted asynchronously to the per-invocation ``response_url`` Slack hands
  back in the command payload.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from infra_brain.callbacks.write_gate import gate_external_write
from infra_brain.config import get_settings

logger = logging.getLogger(__name__)

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def send_slack_message(
    channel: str,
    text: str,
    thread_ts: str | None = None,
    agent_name: str = "chatops",
) -> bool:
    """POST a reply into *channel* via ``chat.postMessage``.

    Returns True when delivered OR when the bot token is unconfigured (a
    clean no-op — matches the Jira/Confluence/ops-webhook "unconfigured"
    pattern). Returns False only when a delivery attempt was made and failed.
    """
    settings = get_settings()
    token = (settings.slack_bot_token or "").strip()
    if not token:
        logger.info(
            "[chatops:slack] SLACK_BOT_TOKEN not configured; skipping reply delivery to channel=%s",
            channel,
        )
        return True

    # F-004.3: pre-write DLP + audit gate — refuses BEFORE any httpx.post.
    gate_external_write(
        system="slack",
        operation="chat.postMessage",
        payload=f"channel={channel} text={text}",
        agent_name=agent_name,
    )

    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    try:
        resp = httpx.post(
            SLACK_POST_MESSAGE_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=settings.api_timeout_seconds,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            logger.error("[chatops:slack] chat.postMessage rejected: %s", body.get("error"))
            return False
        return True
    except Exception:
        logger.exception("[chatops:slack] chat.postMessage delivery failed for channel=%s", channel)
        return False


def send_slack_response_url(response_url: str, text: str, agent_name: str = "chatops") -> bool:
    """POST the deferred answer to a slash command's per-invocation ``response_url``.

    Returns True on success, False on any delivery failure. There is no
    "unconfigured" no-op case here — ``response_url`` always comes from the
    inbound Slack payload itself, never from settings.

    Pinned to Slack's own hostname (lc-safety-reviewer finding): only
    reachable today by someone who can produce a valid v0 signature, but a
    leaked/rotated-late signing secret would otherwise upgrade "can query
    the bot" to "can make infra-brain POST arbitrary JSON to any URL" --
    cheap defense-in-depth given Slack always sends
    https://hooks.slack.com/commands/... here.
    """
    parsed = urlparse(response_url)
    if parsed.scheme != "https" or parsed.hostname != "hooks.slack.com":
        logger.error(
            "[chatops:slack] refusing response_url delivery to unexpected host: %s",
            response_url,
        )
        return False

    settings = get_settings()

    # F-004.3: pre-write DLP + audit gate — refuses BEFORE any httpx.post.
    gate_external_write(
        system="slack",
        operation="response_url",
        payload=f"response_url reply text={text}",
        agent_name=agent_name,
    )

    try:
        resp = httpx.post(
            response_url,
            json={"response_type": "in_channel", "text": text},
            timeout=settings.api_timeout_seconds,
        )
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("[chatops:slack] response_url delivery failed")
        return False
