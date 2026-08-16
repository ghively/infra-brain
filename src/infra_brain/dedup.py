import logging
import uuid as _uuid_mod

import redis

from infra_brain.config import get_settings

log = logging.getLogger(__name__)

_client: redis.Redis | None = None

# Lua compare-and-delete: only deletes the key if its value matches ARGV[1].
# This prevents a worker from deleting another worker's lock after TTL expiry.
_RELEASE_LUA = (
    "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
)

# Margin added to collect_timeout_seconds for the default lock TTL (F-029):
# covers CollectionRun bookkeeping + detail writes after the guarded collect.
_TTL_MARGIN_SECONDS = 120


class LockBackendUnavailable(RuntimeError):
    """Raised when Redis cannot be reached to acquire a sweep lock.

    F-029: a Redis outage must be LOUD (a distinct error the caller handles),
    never a silent skip of every scheduled sweep.
    """


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        # TRK-090/091: bound socket blocking time. This shared client is called
        # SYNCHRONOUSLY from async request paths (session-revocation check, and now
        # the per-user rate-limit / token-budget guardrails), so an unbounded socket
        # wait on a partitioned Redis would stall the whole event loop until the OS
        # TCP timeout. 5s connect/read caps that; a RedisError surfaces instead,
        # which every caller already handles (fail-loud for sweeps, fail-open for
        # the auth/cost guardrails).
        _client = redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
    return _client


def _key(domain: str, scope: str) -> str:
    return f"infra_brain:lock:{domain}:{scope}"


def default_ttl_seconds(domain: str | None = None) -> int:
    """Lock TTL that always outlives the timeout-bounded collect phase (F-029).

    TRK-117 (gotcha #2): when *domain* is given, the TTL is derived from that
    domain's EFFECTIVE collect timeout — i.e. respecting any per-domain
    ``AgentSpec.collect_timeout_seconds`` override (graph_maintenance: 1200s),
    not the global default. Both the collect() guard
    (``BaseAgent._call_with_timeout``) and this TTL resolve the timeout through
    the same ``collect_timeout_for_domain`` helper, so the lock can never expire
    while a per-domain-extended collect() is still running — which would
    otherwise admit a duplicate overlapping dispatch of the same domain. With
    *domain* omitted the global default is used (backward-compatible).
    """
    base_timeout = get_settings().collect_timeout_seconds
    if domain is not None:
        from infra_brain.etl.spec import collect_timeout_override_for_domain  # noqa: PLC0415

        override = collect_timeout_override_for_domain(domain)
        if override is not None:
            base_timeout = override
    return base_timeout + _TTL_MARGIN_SECONDS


def try_acquire(domain: str, scope: str, ttl_seconds: int | None = None) -> str | None:
    """Attempt to acquire the distributed lock.

    Returns a unique token string on success (truthy), or None if the lock is
    already held by another worker (falsy).  Callers must pass the returned token
    to ``release()`` so only the lock owner can delete it.

    TRK-258 (4), re-verified: contention on the ``SET ... NX`` call below is NOT
    a source of Redis error replies, and never has been -- ``NX`` is precisely
    the "try, and tell me whether I got it" primitive: a losing attempt (lock
    already held) returns a plain null reply (success, falsy), not an error.
    ``redis-cli INFO commandstats`` on the live instance confirms this
    empirically -- ``cmdstat_set`` shows ``failed_calls=0`` even while
    ``INFO stats``' ``total_error_replies`` was nonzero (110 over a ~4 day
    uptime window: ``errorstat_ERR:108`` + ``errorstat_WRONGTYPE:2``, against
    65,061 total commands processed -- ~0.17%). None of that volume correlated
    with any command this module (or ``ratelimit.py`` / ``dashboard_auth.py``'s
    login-lockout and session-revocation, the redis client's only other
    callers) actually issues: every one of their per-command ``failed_calls``
    counters was 0 in the same snapshot, and the app/scheduler/mcp containers'
    full retained logs had zero ``redis.RedisError`` exceptions logged in that
    window. If a future audit re-finds a nonzero ``total_error_replies`` and
    reaches for "dedup lock contention" as the explanation, that mechanism is
    specifically wrong -- re-check ``cmdstat_set``'s own ``failed_calls`` before
    attributing it here. At this volume (well under 0.2% of traffic, no
    RedisError observed, and every ``except redis.RedisError`` site in this
    module and its siblings already has a deliberate documented fail-open/
    fail-closed policy) it remains WONTFIX regardless of the exact source.

    ttl_seconds defaults to the domain's effective collect timeout + 120s (was
    a hardcoded 60s that expired mid-sweep and allowed concurrent duplicates —
    F-029). TRK-117: the default is now resolved PER DOMAIN, so a domain with a
    longer per-domain collect timeout (graph_maintenance) gets a proportionally
    longer lock TTL instead of the global default that would expire mid-run.

    Raises LockBackendUnavailable if Redis is unreachable.
    """
    if ttl_seconds is None:
        ttl_seconds = default_ttl_seconds(domain)
    token = _uuid_mod.uuid4().hex
    try:
        result = get_redis().set(_key(domain, scope), token, ex=ttl_seconds, nx=True)
    except redis.RedisError as exc:
        raise LockBackendUnavailable(
            f"Redis unavailable acquiring lock {domain}/{scope}: {exc}"
        ) from exc
    return token if result is True else None


def release(domain: str, scope: str, token: str | None = None) -> None:
    """Release the lock for (domain, scope).

    When *token* is supplied the release is compare-and-delete via a Lua script,
    ensuring a worker can never delete another worker's lock after TTL expiry.
    When *token* is omitted the key is deleted unconditionally (backward-compat
    path used by callers that haven't been updated yet).

    A Redis error here is logged, not raised: the lock carries a TTL, so a
    missed release only delays the next run — failing a finished sweep over
    lock cleanup would be worse (F-029).
    """
    try:
        if token is not None:
            get_redis().eval(_RELEASE_LUA, 1, _key(domain, scope), token)
        else:
            get_redis().delete(_key(domain, scope))
    except redis.RedisError:
        log.exception("Redis unavailable releasing lock %s/%s (TTL will expire it)", domain, scope)
