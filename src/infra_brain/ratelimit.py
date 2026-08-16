"""Per-user LLM cost guardrails for the dashboard chat/custom-view endpoints.

TRK-090 (rate limit) + TRK-091 (token budget). Both are Redis-backed so they are
process-/pod-count-invariant (unlike a per-worker in-memory counter, which would
multiply the effective limit by the replica count). The Redis client is the shared
singleton from :mod:`infra_brain.dedup` — do NOT construct a new client here.

Design notes
------------
* **Rate limit** — a fixed-window counter keyed by user + hour bucket. First hit in
  a window sets a 1h TTL; the key auto-expires, so no sweeper is needed.
* **Token budget** — a cumulative per-user, per-calendar-day counter. The chat and
  custom-view streaming paths call :func:`record_token_usage` after the stream
  finishes, incrementing the day's counter by an APPROXIMATE token count
  (~4 chars/token over prompt + streamed output). Exact usage_metadata is not
  reliably surfaced by the streaming LLM calls, and ``AgentDecisionLog.token_count``
  (the DB field TRK-091 references) has no per-user column to SUM — so a Redis
  counter keyed by the requesting user is the only enforceable option. Same token
  numbers, just stored where they can be filtered by user.
* **Failure mode — FAIL OPEN.** On any ``redis.RedisError`` the guardrails allow the
  request (and log loudly). These are cost guardrails, not a security boundary;
  denying every dashboard chat during a Redis blip is worse than briefly ungated
  spend. NOTE ``dashboard_auth._is_revoked`` deliberately fails the OTHER way —
  CLOSED (TRK-096) — because failing open on revocation would defeat the logout
  boundary; here it is only a cost guardrail. (Contrast: ``dedup`` fails LOUD
  because a silent skip there would break sweeps.)
"""

from __future__ import annotations

import logging
import time

import redis

from infra_brain.config import get_settings
from infra_brain.dedup import get_redis

log = logging.getLogger(__name__)

_RATE_WINDOW_SECONDS = 3600  # rolling hour (fixed-window buckets)
_BUDGET_TTL_SECONDS = 26 * 3600  # a bit over a day so the daily key self-expires


def _rate_key(user_key: str) -> str:
    bucket = int(time.time() // _RATE_WINDOW_SECONDS)
    return f"infra_brain:ratelimit:chat:{user_key}:{bucket}"


def _budget_key(user_key: str) -> str:
    day = time.strftime("%Y%m%d", time.gmtime())
    return f"infra_brain:token_budget:{user_key}:{day}"


def estimate_tokens(*texts: str) -> int:
    """Rough token estimate (~4 chars/token). Approximate by design — see module docstring."""
    return sum(len(t) for t in texts if t) // 4


def rate_limit_exceeded(user_key: str) -> bool:
    """Increment this user's hourly request counter; True if it now exceeds the limit.

    Fixed-window: the first request in an hour bucket sets a 1h TTL. Fails OPEN
    (returns False) if Redis is unavailable.
    """
    limit = get_settings().chat_rate_limit_per_hour
    key = _rate_key(user_key)
    try:
        client = get_redis()
        count = client.incr(key)
        if count == 1:
            client.expire(key, _RATE_WINDOW_SECONDS)
        return count > limit
    except redis.RedisError:
        log.exception(
            "Redis unavailable for rate-limit check (user=%s); failing open (request allowed)",
            user_key,
        )
        return False


def token_budget_exceeded(user_key: str) -> bool:
    """True if this user has already crossed the daily token budget.

    Read-only check run BEFORE serving. Fails OPEN (returns False) on Redis error.
    """
    budget = get_settings().daily_token_budget_per_user
    key = _budget_key(user_key)
    try:
        raw = get_redis().get(key)
    except redis.RedisError:
        log.exception(
            "Redis unavailable for token-budget check (user=%s); failing open (request allowed)",
            user_key,
        )
        return False
    if raw is None:
        return False
    try:
        used = int(raw)
    except (TypeError, ValueError):
        return False
    return used >= budget


def record_token_usage(user_key: str, tokens: int) -> None:
    """Add ``tokens`` to this user's daily counter (best-effort; TTL ~26h).

    Called after a chat/custom-view stream completes. Never raises — a Redis
    outage just means this increment is lost (fail open, logged).
    """
    if tokens <= 0:
        return
    key = _budget_key(user_key)
    try:
        client = get_redis()
        total = client.incrby(key, tokens)
        if total == tokens:  # first write of the day → set expiry
            client.expire(key, _BUDGET_TTL_SECONDS)
    except redis.RedisError:
        log.exception(
            "Redis unavailable recording token usage (user=%s, tokens=%d); usage not counted",
            user_key,
            tokens,
        )
