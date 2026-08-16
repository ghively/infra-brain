"""infra_brain.api.routers.chatops — inbound Slack/Teams chatops bot (GitLab #111).

Confirmed absent via the 2026-07-23 world-class-tool roadmap pass (grep for
"slack"/"teams webhook" turned up zero hits; the only chat surface was the
dashboard's own chat UI). This module gives the same read-only chat agent a
presence in a team channel: Slack Events API mentions/DMs, Slack slash
commands, and a Teams "Outgoing Webhook" bot all resolve to one call into
``infra_brain.chat.agent`` (the exact graph/tools/safety-callback chain the
dashboard's ``/api/dashboard/chat`` SSE endpoint drives) and one reply back
out through the sanctioned external-write path.

Proactive push (infra-brain posting drift/compliance findings into a channel
unprompted, vs. only answering inbound queries) is explicitly OUT of scope
here — per the issue, that depends on the webhook-out subscription system,
which does not exist yet. This module only answers inbound messages.

CODE-COMPLETE BUT NOT LIVE: every route 403s fail-closed unless
``chatops_enabled`` is True AND the matching secret(s) are configured
(``slack_signing_secret`` + ``slack_bot_token`` for Slack,
``teams_outgoing_webhook_secret`` for Teams) — none of which exist yet in
Bitwarden/`.env` for any real Slack app or Teams bot registration. See
docs/TRACKER.md's GitLab #111 entry.

Auth model (deliberately NOT the dashboard's ``require_session`` — these are
machine-to-machine callbacks from Slack/Teams, not a logged-in browser):

* Slack: HMAC-SHA256 request signing (``X-Slack-Signature`` /
  ``X-Slack-Request-Timestamp``), Slack's documented v0 scheme.
* Teams: HMAC-SHA256 over the raw body with the bot's outgoing-webhook
  security token (``Authorization: HMAC <base64>``), Microsoft's documented
  Outgoing Webhook scheme.

Both verifications run against the RAW request body (before any JSON
parsing) and fail closed (403) when the relevant secret is unconfigured —
same fail-closed convention as ``webhooks.py``'s ``_verify_token``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

import anyio
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from infra_brain.config import get_settings
from infra_brain.ratelimit import (
    estimate_tokens,
    rate_limit_exceeded,
    record_token_usage,
    token_budget_exceeded,
)

logger = logging.getLogger(__name__)

chatops_router = APIRouter(prefix="/webhooks/chatops", tags=["chatops"])

# Slack rejects request-signature checks with a timestamp more than 5 minutes
# old (replay-attack defense) — same window Slack's own docs recommend.
_SLACK_TIMESTAMP_TOLERANCE_SECONDS = 60 * 5


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


def _require_chatops_enabled() -> None:
    """Fail closed (403) when the feature flag is off — same convention as the
    other webhook routes' ``_verify_token``: an unconfigured integration
    rejects rather than silently no-opping, so a misconfigured Slack/Teams app
    pointed at this URL gets an unambiguous signal, not a confusing 200/500.
    """
    if not get_settings().chatops_enabled:
        raise HTTPException(status_code=403, detail="chatops bot is not enabled")


def _require_allowed_scope(kind: str, scope_id: str) -> None:
    """Fail closed (403) unless *scope_id* (a Slack team id / Teams tenant
    id) is on the configured allowlist.

    A valid HMAC signature only proves "this request came from our Slack
    app / Teams bot" — it says nothing about which workspace/tenant, so
    without this check any workspace that installs the same Slack app (or
    any tenant pointed at the same Teams bot registration) gets the full
    read-only agent surface, including a tool that can open a real GitLab
    MR (lc-safety-reviewer finding). The dashboard's equivalent path
    requires a real UIUser session; this is the chatops equivalent of that
    gate. Empty allowlist = deny everything, not allow everything.
    """
    settings = get_settings()
    raw = (
        settings.chatops_allowed_slack_team_ids
        if kind == "slack"
        else settings.chatops_allowed_teams_tenant_ids
    )
    allowed = {t.strip() for t in raw.split(",") if t.strip()}
    if scope_id not in allowed:
        logger.warning(
            "[chatops:%s] rejected: scope_id=%s is not on the configured allowlist",
            kind,
            scope_id,
        )
        raise HTTPException(status_code=403, detail=f"{kind} workspace/tenant not authorized")


def _verify_slack_signature(
    body: bytes, timestamp: str | None, signature: str | None, signing_secret: str
) -> bool:
    """Slack's documented v0 HMAC-SHA256 request-signing scheme.

    See https://api.slack.com/authentication/verifying-requests-from-slack.
    Fails closed (returns False) whenever the secret, header, or timestamp is
    missing/unparsable/stale — callers must treat any False as "reject".
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > _SLACK_TIMESTAMP_TOLERANCE_SECONDS:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    computed = (
        "v0="
        + hmac.new(
            signing_secret.encode("utf-8"), basestring.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    )
    return hmac.compare_digest(computed, signature)


def _verify_teams_hmac(body: bytes, auth_header: str | None, secret_b64: str) -> bool:
    """Microsoft's documented Teams "Outgoing Webhook" HMAC-SHA256 scheme.

    The bot's security token (base64) is the HMAC key; the digest is computed
    over the raw request body and compared, base64-encoded, against the
    ``Authorization: HMAC <base64>`` header. Fails closed on any missing/
    malformed input.
    """
    if not secret_b64 or not auth_header or not auth_header.startswith("HMAC "):
        return False
    provided_b64 = auth_header[len("HMAC ") :].strip()
    try:
        key = base64.b64decode(secret_b64)
    except Exception:
        return False
    mac = hmac.new(key, body, hashlib.sha256).digest()
    expected_b64 = base64.b64encode(mac).decode("ascii")
    return hmac.compare_digest(expected_b64, provided_b64)


async def _to_thread(func, *args, **kwargs):
    """Run a sync, potentially-blocking call (DB session, Redis, httpx.post)
    off the event loop.

    BackgroundTasks awaits coroutine callables directly on the running loop
    (Starlette), unlike webhooks.py's sync callables which get a threadpool
    automatically -- so every blocking call reachable from these async
    handlers must explicitly offload itself, or it stalls the whole uvicorn
    worker for the duration of the call (CLAUDE.md constraint #2).
    """
    import functools

    return await anyio.to_thread.run_sync(functools.partial(func, *args, **kwargs))


def _enforce_cost_limits_soft(user_key: str) -> str | None:
    """Same rate-limit/token-budget guardrails as the dashboard chat endpoint
    (TRK-090/TRK-091), but returns a message instead of raising — a
    Slack/Teams caller gets a friendly inline reply, not an HTTP error the
    bot framework would just show as "something went wrong".
    """
    if rate_limit_exceeded(user_key):
        return (
            f"Rate limit exceeded: max {get_settings().chat_rate_limit_per_hour} "
            "requests/hour. Please slow down and try again shortly."
        )
    if token_budget_exceeded(user_key):
        return "Daily LLM token budget exhausted for chatops. It resets at 00:00 UTC."
    return None


async def _ask_chat_agent(thread_id: str, message: str) -> str:
    """Route *message* through the shared chat agent and return the full reply.

    Identical graph/tools/safety-callback chain as the dashboard's streaming
    ``/api/dashboard/chat`` endpoint (``infra_brain.chat.agent``) — chatops is
    a new front door onto the same read-only agent, not a parallel one.
    """
    from infra_brain.chat.agent import answer_message, build_chat_agent

    graph = await build_chat_agent()
    reply = await answer_message(graph, thread_id, message)
    return reply.strip() or "(no response)"


# ─────────────────────────────────────────────────────────────────────────────
# Slack — Events API (app_mention / message.im)
# ─────────────────────────────────────────────────────────────────────────────


async def _handle_slack_mention(
    channel: str, thread_ts: str | None, user: str, team: str, text: str
) -> None:
    """Background task: answer a Slack mention/DM and post the reply back.

    Runs in the event loop via BackgroundTasks (this coroutine is scheduled
    with ``asyncio.create_task`` from an already-async route, so — unlike
    ``webhooks.py``'s sync BackgroundTasks callables — no separate
    ``asyncio.run()`` is needed here).
    """
    from infra_brain.tools.slack import send_slack_message

    user_key = f"chatops:slack:{team}:{user}"
    limited = await _to_thread(_enforce_cost_limits_soft, user_key)
    if limited:
        await _to_thread(send_slack_message, channel, limited, thread_ts=thread_ts)
        return

    thread_id = f"chatops:slack:{team}:{channel}:{thread_ts or user}"
    reply = "Sorry, something went wrong answering that."
    try:
        reply = await _ask_chat_agent(thread_id, text)
    except PermissionError as exc:
        # gate_external_write / DLP denial on the *reply* payload itself is
        # handled inside send_slack_message; this branch is defense-in-depth
        # for anything else raising PermissionError upstream.
        logger.warning("[chatops:slack] blocked reply: %s", exc)
        reply = "I can't send that reply (blocked by a safety check)."
    except Exception:
        logger.exception("[chatops:slack] chat agent failed for channel=%s", channel)
    finally:
        await _to_thread(
            record_token_usage, user_key, estimate_tokens(text) + estimate_tokens(reply)
        )

    try:
        await _to_thread(send_slack_message, channel, reply, thread_ts=thread_ts)
    except PermissionError as exc:
        logger.warning("[chatops:slack] reply blocked by write-gate: %s", exc)


@chatops_router.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    """Slack Events API endpoint — subscription challenge + app_mention/message.im.

    Must ack within Slack's 3s window, so the actual chat-agent call and
    reply are dispatched as a background task; this handler only verifies
    the signature and returns immediately.
    """
    _require_chatops_enabled()
    settings = get_settings()
    body = await request.body()
    if not _verify_slack_signature(
        body,
        request.headers.get("X-Slack-Request-Timestamp"),
        request.headers.get("X-Slack-Signature"),
        settings.slack_signing_secret,
    ):
        raise HTTPException(status_code=403, detail="invalid Slack request signature")

    payload = await request.json()

    # One-time subscription verification handshake — echo the challenge back,
    # no chat-agent invocation involved, so no scope check needed either.
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    _require_allowed_scope("slack", str(payload.get("team_id", "")))

    event = payload.get("event") or {}
    event_type = event.get("type")
    # Ignore the bot's own messages / edits / anything that isn't a genuine
    # inbound mention or DM — Slack re-delivers events the bot's own reply
    # would otherwise trigger an infinite reply loop on.
    if event.get("bot_id") or event_type not in ("app_mention", "message"):
        return JSONResponse({"ok": True})
    if event_type == "message" and event.get("channel_type") != "im":
        # Only answer plain "message" events in a DM ("im"); channel messages
        # not @-mentioning the bot are not this bot's business.
        return JSONResponse({"ok": True})

    text = (event.get("text") or "").strip()
    channel = event.get("channel", "")
    if text and channel:
        # BackgroundTasks awaits coroutine callables directly on the running
        # event loop after the response is sent (Starlette) — do NOT wrap
        # this in asyncio.run(), which would raise RuntimeError ("cannot be
        # called from a running event loop") in that context.
        background_tasks.add_task(
            _handle_slack_mention,
            channel=channel,
            thread_ts=event.get("thread_ts") or event.get("ts"),
            user=event.get("user", "unknown"),
            team=payload.get("team_id", "unknown"),
            text=text,
        )
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# Slack — slash command (e.g. /infra-brain what is drifting on db01?)
# ─────────────────────────────────────────────────────────────────────────────


async def _handle_slack_command(
    response_url: str, user_id: str, team_id: str, channel_id: str, text: str
) -> None:
    from infra_brain.tools.slack import send_slack_response_url

    user_key = f"chatops:slack:{team_id}:{user_id}"
    limited = await _to_thread(_enforce_cost_limits_soft, user_key)
    if limited:
        await _to_thread(send_slack_response_url, response_url, limited)
        return

    thread_id = f"chatops:slack:{team_id}:{channel_id}:{user_id}"
    reply = "(no response)"
    try:
        reply = await _ask_chat_agent(thread_id, text)
    except Exception:
        logger.exception(
            "[chatops:slack] slash-command chat agent failed for channel=%s", channel_id
        )
        reply = "Sorry, something went wrong answering that."
    finally:
        await _to_thread(
            record_token_usage, user_key, estimate_tokens(text) + estimate_tokens(reply)
        )

    try:
        await _to_thread(send_slack_response_url, response_url, reply)
    except PermissionError as exc:
        logger.warning("[chatops:slack] slash-command reply blocked by write-gate: %s", exc)


@chatops_router.post("/slack/command")
async def slack_command(request: Request, background_tasks: BackgroundTasks):
    """Slack slash-command endpoint (``application/x-www-form-urlencoded``).

    Acks immediately (Slack's 3s window) with an ephemeral "thinking"
    message; the real answer is posted asynchronously to the per-invocation
    ``response_url`` Slack supplies in the form payload.
    """
    _require_chatops_enabled()
    settings = get_settings()
    body = await request.body()
    if not _verify_slack_signature(
        body,
        request.headers.get("X-Slack-Request-Timestamp"),
        request.headers.get("X-Slack-Signature"),
        settings.slack_signing_secret,
    ):
        raise HTTPException(status_code=403, detail="invalid Slack request signature")

    form = await request.form()
    _require_allowed_scope("slack", str(form.get("team_id", "")))
    text = (form.get("text") or "").strip()
    response_url = form.get("response_url")
    if not text or not response_url:
        return JSONResponse(
            {"response_type": "ephemeral", "text": "Usage: /infra-brain <question>"}
        )

    background_tasks.add_task(
        _handle_slack_command,
        response_url=str(response_url),
        user_id=str(form.get("user_id", "unknown")),
        team_id=str(form.get("team_id", "unknown")),
        channel_id=str(form.get("channel_id", "unknown")),
        text=text,
    )
    return JSONResponse({"response_type": "ephemeral", "text": "On it…"})


# ─────────────────────────────────────────────────────────────────────────────
# Microsoft Teams — Outgoing Webhook (synchronous request/response)
# ─────────────────────────────────────────────────────────────────────────────


@chatops_router.post("/teams")
async def teams_outgoing_webhook(request: Request):
    """Teams "Outgoing Webhook" bot — synchronous request/response, no
    separate outbound call: the answer is the HTTP response body itself.
    """
    _require_chatops_enabled()
    settings = get_settings()
    body = await request.body()
    if not _verify_teams_hmac(
        body, request.headers.get("Authorization"), settings.teams_outgoing_webhook_secret
    ):
        raise HTTPException(status_code=403, detail="invalid Teams HMAC signature")

    payload = await request.json()
    # Bot Framework Activity schema: tenant id lives under channelData.tenant.id
    # for Teams-originated activities.
    tenant_id = str(((payload.get("channelData") or {}).get("tenant") or {}).get("id", ""))
    _require_allowed_scope("teams", tenant_id)

    text = (payload.get("text") or "").strip()
    from_prop = payload.get("from") or {}
    user_id = from_prop.get("id", "unknown")
    conversation = payload.get("conversation") or {}
    conversation_id = conversation.get("id", "unknown")

    if not text:
        return JSONResponse(
            {"type": "message", "text": "Ask me something about the infrastructure."}
        )

    user_key = f"chatops:teams:{conversation_id}:{user_id}"
    limited = await _to_thread(_enforce_cost_limits_soft, user_key)
    if limited:
        return JSONResponse({"type": "message", "text": limited})

    thread_id = f"chatops:teams:{conversation_id}:{user_id}"
    reply_text = "Sorry, something went wrong answering that."
    try:
        reply_text = await _ask_chat_agent(thread_id, text)
    except Exception:
        logger.exception("[chatops:teams] chat agent failed for conversation=%s", conversation_id)
    finally:
        await _to_thread(
            record_token_usage, user_key, estimate_tokens(text) + estimate_tokens(reply_text)
        )

    from infra_brain.tools.teams import build_teams_reply

    try:
        reply = build_teams_reply(reply_text)
    except PermissionError as exc:
        logger.warning("[chatops:teams] reply blocked by write-gate: %s", exc)
        reply = {"type": "message", "text": "I can't send that reply (blocked by a safety check)."}
    return JSONResponse(reply)
