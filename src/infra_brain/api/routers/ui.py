"""infra_brain.api.routers.ui — Chat & Custom Views router.

Extracted from dashboard_api.py (Task 8 — Phase 3 god-file split).
Handler bodies are byte-identical to the originals; no logic changes.

Import rules:
  * MAY import from infra_brain.api.schemas, infra_brain.api._helpers,
    infra_brain.db.models, infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.

Auth note (mixed-auth reproduction):
  All 7 routes live on the big ``router`` in dashboard_api.py, which carries
  ``dependencies=[Depends(require_session)]`` at the router level.  That means
  every route — including GET /views/{share_token} — is gated by require_session.
  The GET /views/{share_token} handler ALSO declares
  ``_session: Optional[dict] = Depends(require_session_optional)`` as a function
  parameter, but that is an ADDITIONAL dependency to retrieve the optional user
  inside the handler body; it does not remove the router-level gate.
  Proof: test_get_private_view_no_auth_401 expects HTTP 401 from the anon client,
  which is only possible because require_session (router-level) still fires.

  The ui_router here reproduces the SAME pattern:
    * router-level ``dependencies=[Depends(require_session)]`` → all routes gated
    * each handler's function signature is byte-identical, preserving the explicit
      per-route ``_: None = Depends(require_session)`` and the
      ``_session: Optional[dict] = Depends(require_session_optional)`` as-is.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from infra_brain.api._envelope import paginate
from infra_brain.api._helpers import require_session_optional
from infra_brain.api.schemas import (
    ChatRequest,
    CustomViewCreate,
    CustomViewOut,
    CustomViewPageOut,
    CustomViewRequest,
    CustomViewUpdate,
)
from infra_brain.config import get_settings
from infra_brain.dashboard_auth import current_user, require_session
from infra_brain.db.models import CustomView, UIUser
from infra_brain.db.session import get_session
from infra_brain.ratelimit import (
    estimate_tokens,
    rate_limit_exceeded,
    record_token_usage,
    token_budget_exceeded,
)

logger = logging.getLogger(__name__)


def _resolve_user_key(request: Request) -> str:
    """Stable per-user key for cost guardrails — same value used to scope chat threads."""
    user = current_user(request)
    return (user or {}).get("username") or "dev"


def _enforce_llm_cost_limits(user_key: str) -> None:
    """Gate the two LLM-invoking dashboard endpoints (TRK-090 / TRK-091).

    Raises HTTP 429 with a distinct ``code`` per case so the frontend can tell the
    rate-limit rejection apart from the token-budget rejection. Rate limit is
    checked first (cheap INCR) and increments the counter; the budget check is a
    read-only lookup, so ordering does not double-charge the budget.
    """
    if rate_limit_exceeded(user_key):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limit_exceeded",
                "message": (
                    f"Rate limit exceeded: max {get_settings().chat_rate_limit_per_hour} "
                    "requests/hour. Please slow down and try again shortly."
                ),
            },
        )
    if token_budget_exceeded(user_key):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "token_budget_exceeded",
                "message": (
                    "Daily LLM token budget exhausted for your account. "
                    "It resets at 00:00 UTC."
                ),
            },
        )

# Chat & Custom Views — same session gate and prefix as the main dashboard router.
# Routes: POST /api/dashboard/chat, POST /api/dashboard/custom-view,
#         POST/GET /api/dashboard/views, GET/PATCH/DELETE /api/dashboard/views/{...}
ui_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)

# TRK-306: deliberately NO router-level auth dependency. Exactly one route lives
# here — GET /views/{share_token} — because a public share link must be openable
# by a logged-out recipient, which is the entire point of a share link. Its
# handler still enforces auth itself for non-public views.
#
# Do NOT add routes to this router. Anything else belongs on ``ui_router`` above,
# where the router-level ``require_session`` gate applies. If a second genuinely
# public route ever appears, give it the same explicit in-handler treatment and
# say so here — an unguarded router is only safe while every route on it is
# audited, and "one route, stated why" is what keeps that true.
ui_public_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Chat  (read-only streaming assistant)
# ─────────────────────────────────────────────────────────────────────────────


def _scoped_thread_id(user_key: str, client_thread_id: str) -> str:
    """Bind the checkpointer thread to the caller (F-028). A client-supplied
    thread_id is resolved ONLY inside the caller's own namespace, so user A
    supplying user B's thread_id gets a fresh thread, never B's history."""
    return f"{user_key}::{client_thread_id}"


def _sse(event: str | None, payload: dict) -> str:
    """One SSE frame. ``event=None`` emits a bare ``data:`` frame.

    Every non-default frame type added here (``tool``, ``provenance``,
    ``error``) is BACKWARD-COMPATIBLE with the older sidebar chat client
    (`dashboard-app/src/components/Sidebar.tsx`'s `SidebarChat`), which ignores
    the ``event:`` line entirely and reads only ``data:`` payloads' ``token``
    and ``thread_id`` keys: the new frames carry neither except on ``error``,
    which deliberately DOES carry a ``token`` so the old client still shows the
    failure instead of an empty bubble. The new Chat page keys off the
    ``event:`` name and renders the structured fields.
    """
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(payload)}\n\n"


async def _chat_sse(message: str, thread_id: str, user_key: str):
    """Server-Sent-Events generator for one chat turn.

    Frames, in order: ``meta`` (client thread_id), then any mix of ``tool`` /
    ``provenance`` / bare token frames, then ``done``. A failure emits an
    ``error`` frame — structured, renderable, and derived from a fixed copy
    table rather than the exception (N-3) — never a 500 mid-stream and never a
    stack trace on the wire. The checkpointer is keyed on the user-scoped
    thread id.
    """
    from infra_brain.chat.agent import (
        build_chat_agent,
        classify_chat_error,
        count_user_turns,
        stream_chat_events,
    )
    from infra_brain.chat.limits import CHAT_MAX_TURNS

    scoped = _scoped_thread_id(user_key, thread_id)
    yield _sse("meta", {"thread_id": thread_id})
    out_chars = 0
    try:
        graph = await build_chat_agent()
        # Bound the conversation, not just the single message: the checkpointer
        # replays the whole thread into every turn, so an unbounded thread is an
        # unbounded per-call prompt. Refuse with a renderable error rather than
        # silently dropping history the operator still thinks is in context.
        if await count_user_turns(graph, scoped) >= CHAT_MAX_TURNS:
            yield _sse(
                "error",
                {
                    "code": "conversation_too_long",
                    "message": (
                        f"This conversation has reached its {CHAT_MAX_TURNS}-turn limit."
                    ),
                    "hints": ["Start a new conversation to keep going."],
                    "token": "\n\n[error: conversation turn limit reached]",
                },
            )
        else:
            async for event in stream_chat_events(graph, scoped, message):
                if event["type"] == "token":
                    out_chars += len(event["text"])
                    yield _sse(None, {"token": event["text"]})
                elif event["type"] == "tool":
                    yield _sse("tool", {"tool": event["name"], "args": event["args"]})
                elif event["type"] == "provenance":
                    yield _sse(
                        "provenance",
                        {"tool": event["tool"], "provenance": event["provenance"]},
                    )
    except Exception as exc:  # noqa: BLE001
        # N-3: log the real error server-side; never leak raw exception text
        # (which can carry internal detail) into the client stream. What DOES
        # go out is a classified code + fixed copy + non-secret endpoint facts,
        # so the UI can say "the model isn't reachable — here's what to check".
        logger.exception("chat SSE stream failed")
        detail = classify_chat_error(exc)
        yield _sse("error", {**detail, "token": "\n\n[error: chat request failed]"})
    finally:
        # TRK-091: charge the user's daily token budget by the approximate tokens
        # consumed (prompt + streamed output). Best-effort; never blocks the stream.
        record_token_usage(user_key, estimate_tokens(message) + (out_chars // 4))
    yield "event: done\ndata: {}\n\n"


@ui_router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    """Stream a read-only assistant reply token-by-token (text/event-stream).

    Drives the same LangGraph agent the legacy Streamlit sidebar used — four
    read-only query tools, streaming, and per-thread memory via a USER-SCOPED
    thread id (F-028: client thread_ids are namespaced by the session user).
    """
    if not req.message.strip():
        raise HTTPException(status_code=422, detail="empty message")
    user_key = _resolve_user_key(request)
    _enforce_llm_cost_limits(user_key)
    thread_id = req.thread_id or uuid.uuid4().hex
    return StreamingResponse(
        _chat_sse(req.message, thread_id, user_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Custom View  (AI-generated OpenUI component tree, NDJSON stream)
# ─────────────────────────────────────────────────────────────────────────────


async def _custom_view_ndjson(prompt: str, user_key: str):
    """NDJSON stream: token chunks followed by a done sentinel."""
    from infra_brain.callbacks.dlp import redact_pans_preserving_uuids  # noqa: PLC0415
    from infra_brain.callbacks.registry import build_async_callbacks  # noqa: PLC0415
    from infra_brain.llm import get_chat_model  # noqa: PLC0415
    from infra_brain.openui.prompt import get_system_prompt  # noqa: PLC0415
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

    # B2: this generator runs on the FastAPI event loop (astream over the LLM),
    # so use the async/batched Audit + Observation handlers — the sync variants
    # would commit to Postgres once per tool event on the event loop thread.
    callbacks = build_async_callbacks(agent_name="custom_view", domain="custom_view")
    llm = get_chat_model(
        model="claude-haiku-4-5-20251001",
        callbacks=callbacks,
        streaming=True,
    )
    messages = [
        SystemMessage(content=get_system_prompt()),
        HumanMessage(content=prompt),
    ]
    out_chars = 0
    try:
        async for chunk in llm.astream(messages):
            text = chunk.content if hasattr(chunk, "content") else str(chunk)
            if text:
                out_chars += len(text)
                # N-3: scrub Luhn-valid PANs out of streamed model tokens before
                # they reach the client — parity with the chat SSE stream, which
                # already applies redact_pans().
                yield json.dumps({"token": redact_pans_preserving_uuids(text)}) + "\n"
    except Exception:  # noqa: BLE001
        # N-3: do NOT leak raw exception text (stack/internal detail) to the
        # client. Log the real error server-side; return a generic message.
        logger.exception("custom-view stream failed")
        yield json.dumps({"token": "[error: failed to generate custom view]"}) + "\n"
    finally:
        # TRK-091: charge the user's daily token budget (prompt + streamed output).
        record_token_usage(user_key, estimate_tokens(prompt) + (out_chars // 4))
    yield json.dumps({"done": True}) + "\n"


@ui_router.post("/custom-view")
async def custom_view(body: CustomViewRequest, request: Request) -> StreamingResponse:
    """Stream an OpenUI component tree from a natural-language prompt.

    Uses the Haiku model for speed. The router is already session-gated.
    Returns NDJSON: one JSON object per line, each with a "token" key,
    terminated by {"done": true}.
    """
    if not body.prompt.strip():
        raise HTTPException(status_code=422, detail="prompt is required")
    user_key = _resolve_user_key(request)
    _enforce_llm_cost_limits(user_key)
    return StreamingResponse(
        _custom_view_ndjson(body.prompt, user_key),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Custom Views CRUD  (save/share/list/delete named OpenUI views)
# ─────────────────────────────────────────────────────────────────────────────


def _get_user_id(username: str, db) -> Optional[uuid.UUID]:
    """Look up UIUser.id from username. Returns None if not found."""
    user_row = db.query(UIUser).filter(UIUser.username == username).one_or_none()
    return user_row.id if user_row else None


@ui_router.post("/views", response_model=CustomViewOut, status_code=201)
def create_custom_view(
    body: CustomViewCreate,
    request: Request,
    _: None = Depends(require_session),
):
    """Create a new named custom view, optionally shared publicly."""
    user = current_user(request)
    share_token = secrets.token_urlsafe(10)[:10]
    with get_session() as db:
        user_id = _get_user_id(user["username"], db) if user else None
        view = CustomView(
            id=uuid.uuid4(),
            user_id=user_id,
            title=body.title,
            prompt=body.prompt,
            openui_lang=body.openui_lang,
            share_token=share_token,
            is_public=body.is_public,
        )
        db.add(view)
        db.commit()
        db.refresh(view)
        return CustomViewOut(
            id=str(view.id),
            title=view.title,
            prompt=view.prompt,
            openui_lang=view.openui_lang,
            share_token=view.share_token,
            is_public=view.is_public,
            created_at=view.created_at,
            share_url=f"/dashboard2/views/{view.share_token}",
        )


@ui_router.get("/views", response_model=CustomViewPageOut)
def list_custom_views(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    _: None = Depends(require_session),
):
    """List all custom views owned by the current user."""
    # M-3: clamp before use so the returned envelope reflects what was
    # actually queried (paginate() also clamps as a backstop).
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    user = current_user(request)
    with get_session() as db:
        user_id = _get_user_id(user["username"], db) if user else None
        q = (
            db.query(CustomView)
            .filter(CustomView.user_id == user_id)
            .order_by(CustomView.created_at.desc())
        )
        rows, total = paginate(q, limit=limit, offset=offset)
        items = [
            CustomViewOut(
                id=str(v.id),
                title=v.title,
                prompt=v.prompt,
                openui_lang=v.openui_lang,
                share_token=v.share_token,
                is_public=v.is_public,
                created_at=v.created_at,
                share_url=f"/dashboard2/views/{v.share_token}",
            )
            for v in rows
        ]
        return CustomViewPageOut(items=items, total=total, limit=limit, offset=offset)


@ui_public_router.get("/views/{share_token}", response_model=CustomViewOut)
def get_custom_view(
    share_token: str,
    request: Request,
    _session: Optional[dict] = Depends(require_session_optional),
):
    """Fetch a custom view by share token. Public views are open; private ones require ownership.

    TRK-306: this route lives on ``ui_public_router`` — the ONLY route that does —
    specifically because ``ui_router`` carries a router-level
    ``dependencies=[Depends(require_session)]`` that fires before any handler body
    runs. This handler was always written to serve anonymous viewers (it takes
    ``require_session_optional``, branches on ``is_public``, and only demands a
    session for private views), but the router-level gate overrode all of that and
    returned 401 to every anonymous request. The feature the backend builds a
    ``share_url`` for has therefore never worked end-to-end.

    Splitting the route out is the narrowest possible fix: removing the
    router-level dependency would un-gate all seven routes, and FastAPI offers no
    per-route opt-out of a router-level dependency.

    The private-view path is UNCHANGED and still 401s an anonymous caller — that
    check now lives in the handler body (where it always was) instead of being
    enforced one layer up. ``test_get_private_view_no_auth_401`` pins it, and a
    new test pins the public case that was previously impossible.
    """
    with get_session() as db:
        view = db.query(CustomView).filter(CustomView.share_token == share_token).one_or_none()
        if view is None:
            raise HTTPException(status_code=404, detail="View not found")
        if view.is_public:
            return CustomViewOut(
                id=str(view.id),
                title=view.title,
                prompt=view.prompt,
                openui_lang=view.openui_lang,
                share_token=view.share_token,
                is_public=view.is_public,
                created_at=view.created_at,
                share_url=f"/dashboard2/views/{view.share_token}",
            )
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_id = _get_user_id(user["username"], db)
        if view.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        return CustomViewOut(
            id=str(view.id),
            title=view.title,
            prompt=view.prompt,
            openui_lang=view.openui_lang,
            share_token=view.share_token,
            is_public=view.is_public,
            created_at=view.created_at,
            share_url=f"/dashboard2/views/{view.share_token}",
        )


@ui_router.patch("/views/{view_id}", response_model=CustomViewOut)
def patch_custom_view(
    view_id: str,
    body: CustomViewUpdate,
    request: Request,
    _: None = Depends(require_session),
):
    """Update title or is_public on a custom view the caller owns."""
    try:
        vid = uuid.UUID(view_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="View not found")
    user = current_user(request)
    with get_session() as db:
        view = db.get(CustomView, vid)
        if view is None:
            raise HTTPException(status_code=404, detail="View not found")
        user_id = _get_user_id(user["username"], db) if user else None
        # L-5: NULL == NULL in Python, so a caller resolving to user_id=None
        # would otherwise "own" every NULL-owner view. But there are two very
        # different ways to reach user_id=None, and only one is dangerous:
        #   1. No session user at all (dev mode / unauthenticated deployment).
        #      Every view is legitimately NULL-owned there, and rejecting would
        #      make each view uneditable by the person who just created it.
        #   2. A live signed session whose UIUser row has since been deleted
        #      (the cookie stays valid up to a day). THAT caller must not
        #      inherit ownership of unowned views — this is the actual hole.
        # Reject case 2 explicitly, then compare normally.
        if user is not None and user_id is None:
            raise HTTPException(status_code=403, detail="Forbidden")
        if view.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        if body.title is not None:
            view.title = body.title
        if body.is_public is not None:
            view.is_public = body.is_public
        db.commit()
        db.refresh(view)
        return CustomViewOut(
            id=str(view.id),
            title=view.title,
            prompt=view.prompt,
            openui_lang=view.openui_lang,
            share_token=view.share_token,
            is_public=view.is_public,
            created_at=view.created_at,
            share_url=f"/dashboard2/views/{view.share_token}",
        )


@ui_router.delete("/views/{view_id}", status_code=204)
def delete_custom_view(
    view_id: str,
    request: Request,
    _: None = Depends(require_session),
):
    """Delete a custom view the caller owns."""
    try:
        vid = uuid.UUID(view_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="View not found")
    user = current_user(request)
    with get_session() as db:
        view = db.get(CustomView, vid)
        if view is None:
            raise HTTPException(status_code=404, detail="View not found")
        user_id = _get_user_id(user["username"], db) if user else None
        # L-5: see the matching comment in patch_custom_view — a session whose
        # UIUser row was deleted must not inherit unowned views, but a
        # deployment with no session users at all legitimately owns its own
        # NULL-owner views.
        if user is not None and user_id is None:
            raise HTTPException(status_code=403, detail="Forbidden")
        if view.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        db.delete(view)
        db.commit()
    return None
