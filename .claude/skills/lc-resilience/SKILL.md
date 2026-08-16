---
name: lc-resilience
description: Use when adding production reliability to a LangChain or LangGraph application — connection pooling, timeouts, circuit breakers, exponential backoff with jitter, bulkhead isolation, graceful degradation, retry budgets, or dead letter queues. Triggered by requests to "make my app production-ready", "handle failures gracefully", "my app hangs under load", "retries are causing a storm", "I keep hitting rate limits", "requests time out", or any mention of reliability, resilience, or production hardening.
---

# lc:resilience — Production Reliability Patterns

## Overview

This skill transforms a working prototype into a production-grade LangChain/LangGraph application. Every pattern here solves a specific failure mode that will occur in production. The skill teaches each concept inline before showing you the code.

**The four failure modes this skill eliminates:**

| Failure Mode | Symptom | Pattern Applied |
|---|---|---|
| Connection exhaustion | App crashes under 10+ concurrent users | PostgreSQL Connection Pooling |
| Hanging requests | Requests never complete, users wait forever | Three-Level Timeouts |
| Cascade failures | One bad API call takes down everything | Circuit Breakers |
| Retry storms | Retries amplify load during an outage | Exponential Backoff with Jitter |

Plus three advanced patterns: Bulkhead Isolation, Graceful Degradation, and Dead Letter Queues.

---

## Trigger Phrases

- "make my app production-ready"
- "my app hangs under load"
- "retries are causing a storm"
- "I keep hitting rate limits"
- "connection pool exhausted"
- "requests time out"
- "circuit breaker"
- "graceful degradation"
- `/lc:resilience`

---

## Discovery Flow

Ask all three questions in one message before generating any code.

```
Before I scaffold resilience patterns for your app, I need to understand your load profile:

1. CONCURRENT USERS
   What is your expected peak concurrent user count?
   (This determines connection pool sizing. Rule of thumb: pool max = concurrent_users × 2)
   (a) < 10 users  → min_size=2,  max_size=10
   (b) 10–50 users → min_size=5,  max_size=20
   (c) 50–200 users → min_size=10, max_size=50
   (d) 200+ users  → use PgBouncer in front of your pool

2. EXTERNAL API CALLS
   Do your LangChain tools call external APIs? (Anthropic, OpenAI, web search, databases, etc.)
   (a) Yes — I need circuit breakers per provider
   (b) No — skip circuit breakers, focus on timeouts and retries

3. MAX ACCEPTABLE LATENCY
   What is the maximum time a user should wait for a response before seeing an error?
   (a) 30 seconds  → tool_timeout=10s, llm_timeout=25s, graph_timeout=30s
   (b) 60 seconds  → tool_timeout=15s, llm_timeout=45s, graph_timeout=60s
   (c) 120 seconds → tool_timeout=30s, llm_timeout=90s, graph_timeout=120s
   (d) No limit    → still set timeouts — "no limit" means "hang forever" in production
```

After answers are provided, proceed to scaffold all applicable patterns.

---

## Pattern 1 — PostgreSQL Connection Pooling (CRITICAL)

**Why the default pattern breaks under concurrency**

The naive pattern creates a new database connection per request:

```python
# BAD — do not use in production
conn = psycopg.connect(DATABASE_URL)
checkpointer = PostgresSaver(conn)
```

PostgreSQL has a hard connection limit (default: 100). Each request holds a connection for the duration of the LLM call (seconds to minutes). Under 10+ concurrent users, you exhaust the connection limit and every new request crashes with `connection refused`. The connections also don't clean up on error, so the pool leaks over time.

**The fix: asyncpg connection pool**

A connection pool maintains a fixed set of long-lived database connections and lends them to requests on demand. Requests wait in a queue if all connections are busy, rather than creating a new connection (and crashing).

```python
# resilience/db_pool.py
import asyncpg
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# These values are set based on your answer to Question 1 above.
# min_size: connections kept alive even when idle (reduces connection setup latency)
# max_size: hard ceiling — requests queue if all connections are busy
# command_timeout: how long a single SQL query may run before being killed
POOL_MIN_SIZE = 5
POOL_MAX_SIZE = 20
COMMAND_TIMEOUT = 30  # seconds

_pool: Optional[asyncpg.Pool] = None


async def get_pool(database_url: str) -> asyncpg.Pool:
    """Return the singleton connection pool, creating it on first call.
    
    This is a singleton so the pool is shared across all requests in the process.
    Creating a new pool per request defeats the entire purpose of pooling.
    """
    global _pool
    if _pool is None:
        logger.info("Creating asyncpg connection pool (min=%d, max=%d)", POOL_MIN_SIZE, POOL_MAX_SIZE)
        _pool = await asyncpg.create_pool(
            dsn=database_url,
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            command_timeout=COMMAND_TIMEOUT,
            # Automatically reconnect if a connection goes stale
            max_inactive_connection_lifetime=300,  # 5 minutes
        )
        # Health check: verify the pool is working before accepting traffic
        await health_check(_pool)
    return _pool


async def health_check(pool: asyncpg.Pool) -> None:
    """Run SELECT 1 to verify the database is reachable. Fails fast on startup
    rather than failing slowly on the first user request."""
    async with pool.acquire() as conn:
        result = await conn.fetchval("SELECT 1")
        assert result == 1, "Database health check failed"
        logger.info("Database health check passed")


async def log_pool_stats(pool: asyncpg.Pool) -> None:
    """Log pool utilization. Call this periodically from a background task
    to detect pool exhaustion before it becomes an incident."""
    logger.info(
        "Pool stats — size: %d, idle: %d, waiting: %d",
        pool.get_size(),
        pool.get_idle_size(),
        # get_size() - get_idle_size() = active connections
        pool.get_size() - pool.get_idle_size(),
    )


async def close_pool() -> None:
    """Gracefully close the pool on application shutdown. This lets in-flight
    queries complete before the process exits."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Connection pool closed")
```

```python
# resilience/checkpointer.py
import os
from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from resilience.db_pool import get_pool

load_dotenv()


async def create_checkpointer() -> AsyncPostgresSaver:
    """Create an AsyncPostgresSaver backed by the shared connection pool.
    
    AsyncPostgresSaver is the LangGraph persistence layer — it saves graph state
    between steps so you can resume after failures and support human-in-the-loop.
    Using the shared pool means all graph runs share connections rather than
    each creating their own.
    """
    pool = await get_pool(os.environ["DATABASE_URL"])
    checkpointer = AsyncPostgresSaver(pool)
    # Create the checkpoint tables if they don't exist (idempotent)
    await checkpointer.setup()
    return checkpointer
```

```python
# main.py — application lifespan management
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from resilience.db_pool import get_pool, close_pool, log_pool_stats

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler — runs setup on startup, teardown on shutdown.
    
    Using lifespan (rather than @app.on_event) is the modern FastAPI pattern
    and ensures cleanup runs even if startup raises an exception.
    """
    # STARTUP
    import os
    pool = await get_pool(os.environ["DATABASE_URL"])
    
    # Start background pool stats logging every 60 seconds
    async def pool_monitor():
        while True:
            await asyncio.sleep(60)
            await log_pool_stats(pool)
    
    monitor_task = asyncio.create_task(pool_monitor())
    
    yield  # Application runs here
    
    # SHUTDOWN
    monitor_task.cancel()
    await close_pool()


app = FastAPI(lifespan=lifespan)
```

---

## Pattern 2 — Timeout Enforcement at Three Levels

**Why you need timeouts at every layer**

A single LLM call with no timeout can hold a connection open for minutes if the provider is slow. Under concurrency, this exhausts your connection pool. Worse, if the provider is completely down, requests hang forever — your app appears frozen to users and health checks fail.

The rule: **every async call that touches the network must have a timeout.**

There are three distinct layers where calls can hang, and each needs its own timeout:

```
User request
  └── Graph invocation (Level 3 — longest timeout, outer bound)
        └── LLM call (Level 2 — provider-specific timeout)
              └── Tool call (Level 1 — shortest timeout, fastest operations)
```

```python
# resilience/timeouts.py
import asyncio
import httpx
import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Timeout constants — adjust based on your answer to Question 3 above
TOOL_TIMEOUT = 10.0      # Level 1: individual tool execution
LLM_TIMEOUT = 30.0       # Level 2: single LLM API call
GRAPH_TIMEOUT = 120.0    # Level 3: full graph invocation (outer bound)

# Slow nodes (e.g., code execution, file processing) get more time
SLOW_TOOL_TIMEOUT = 30.0
SLOW_NODE_TIMEOUT = 60.0


async def with_timeout(
    coro: Any,
    timeout: float,
    operation_name: str = "operation",
) -> Any:
    """Wrap any coroutine with a timeout. On timeout, logs the event and raises
    a structured TimeoutError with the operation name.
    
    Usage:
        result = await with_timeout(my_tool.ainvoke(input), timeout=10.0, operation_name="web_search")
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.error("Timeout after %.1fs — operation: %s", timeout, operation_name)
        raise TimeoutError(
            f"Operation '{operation_name}' timed out after {timeout:.0f}s. "
            "The service may be slow or unavailable. Please try again."
        )


def create_llm_with_timeout(timeout_seconds: float = LLM_TIMEOUT):
    """Create a ChatAnthropic instance with a custom HTTP client that enforces
    a timeout on every API call.
    
    Why httpx.AsyncClient? LangChain's Anthropic integration uses httpx under
    the hood. By injecting our own client, we control the timeout at the HTTP
    level — this catches hangs in the TCP connection, TLS handshake, and
    response streaming, not just the Python-level await.
    """
    from langchain_anthropic import ChatAnthropic
    import os
    
    # httpx.Timeout has four components:
    # - connect: time to establish the TCP+TLS connection
    # - read: time between receiving bytes (not total response time)
    # - write: time to send the request body
    # - pool: time to acquire a connection from the httpx pool
    http_timeout = httpx.Timeout(
        connect=5.0,
        read=timeout_seconds,
        write=10.0,
        pool=5.0,
    )
    
    return ChatAnthropic(
        model="claude-sonnet-4-6",
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        http_client=httpx.AsyncClient(timeout=http_timeout),
        # max_retries=0 here — retries are handled by tenacity (Pattern 4)
        # so the LLM client should not retry on its own
        max_retries=0,
    )


# Level 1 — wrap a tool with a timeout
class TimeoutTool:
    """Wraps any LangChain tool to enforce a maximum execution time.
    
    Concept: tools that call external services (web search, databases, APIs)
    can hang indefinitely if the service is slow. This wrapper applies
    asyncio.wait_for() transparently so your tool node code stays clean.
    """
    
    def __init__(self, tool, timeout: float = TOOL_TIMEOUT):
        self.tool = tool
        self.timeout = timeout
    
    async def ainvoke(self, input: Any) -> Any:
        return await with_timeout(
            self.tool.ainvoke(input),
            timeout=self.timeout,
            operation_name=getattr(self.tool, "name", str(self.tool)),
        )


# Level 3 — wrap a graph invocation with a timeout
async def invoke_graph_with_timeout(
    graph,
    input: dict,
    config: dict,
    timeout: float = GRAPH_TIMEOUT,
) -> dict:
    """Invoke a LangGraph graph with an outer timeout.
    
    This is the last line of defense — even if individual nodes don't have
    timeouts (they should!), the graph as a whole will not run beyond this limit.
    The user gets a clear error message rather than waiting forever.
    """
    try:
        return await with_timeout(
            graph.ainvoke(input, config),
            timeout=timeout,
            operation_name="graph_invocation",
        )
    except TimeoutError as e:
        # Return a structured error response that your API layer can serialize
        return {
            "error": str(e),
            "error_type": "timeout",
            "messages": [{"role": "assistant", "content": str(e)}],
        }
```

```python
# Example: using timeouts in a LangGraph node
from resilience.timeouts import with_timeout, TOOL_TIMEOUT, SLOW_TOOL_TIMEOUT

async def search_node(state: AgentState) -> AgentState:
    """Node that calls a web search tool with a timeout.
    
    Pattern: always name the operation — the name appears in logs and LangSmith
    traces, making it easy to identify which tool is slow in production.
    """
    query = state["messages"][-1].content
    
    try:
        result = await with_timeout(
            web_search_tool.ainvoke({"query": query}),
            timeout=TOOL_TIMEOUT,
            operation_name="web_search",
        )
        return {"messages": [result], "last_tool": "web_search"}
    except TimeoutError as e:
        # Don't crash the graph — return an error message in state
        # The next node can decide whether to retry or degrade gracefully
        return {
            "messages": [{"role": "tool", "content": f"Search timed out: {e}"}],
            "error": str(e),
        }
```

---

## Pattern 3 — Circuit Breaker

**What is a circuit breaker and why do you need one?**

Imagine your LLM provider has an outage. Without a circuit breaker:
1. Request 1 fails after 30s timeout
2. Request 2 fails after 30s timeout  
3. 100 concurrent requests all pile up waiting 30s each
4. Your thread pool, connection pool, and memory are all exhausted
5. Your entire application is now down — not because of your code, but because of a dependency

A circuit breaker is an automatic switch with three states:
- **CLOSED** (normal): requests flow through, failures are counted
- **OPEN** (failing): requests are rejected immediately (no waiting!) — fast failure
- **HALF_OPEN** (recovering): one test request is allowed through to check recovery

When failures exceed `fail_max` in a time window, the breaker opens. After `reset_timeout` seconds, it moves to HALF_OPEN and tests recovery.

```python
# resilience/circuit_breakers.py
import pybreaker
import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


# IMPORTANT: one breaker per external dependency.
# This prevents an Anthropic outage from affecting your database calls,
# and a database outage from affecting your Anthropic calls.

def make_breaker(name: str, fail_max: int = 5, reset_timeout: int = 60) -> pybreaker.CircuitBreaker:
    """Create a named circuit breaker with logging listeners.
    
    fail_max: how many consecutive failures before opening the circuit
    reset_timeout: how many seconds to wait before testing recovery (HALF_OPEN)
    
    Production tuning:
    - Critical, fast APIs (database): fail_max=3, reset_timeout=30
    - External APIs (LLM providers): fail_max=5, reset_timeout=60
    - Slow/unreliable third-party services: fail_max=3, reset_timeout=120
    """
    
    class LoggingListener(pybreaker.CircuitBreakerListener):
        def state_change(self, cb, old_state, new_state):
            logger.warning(
                "Circuit breaker '%s' changed: %s → %s",
                name, old_state.name, new_state.name
            )
        
        def failure(self, cb, exc):
            logger.warning(
                "Circuit breaker '%s' recorded failure (%d/%d): %s",
                name, cb.fail_counter, cb.fail_max, exc
            )
        
        def success(self, cb):
            if cb.current_state == "half-open":
                logger.info("Circuit breaker '%s' recovery probe succeeded", name)
    
    return pybreaker.CircuitBreaker(
        fail_max=fail_max,
        reset_timeout=reset_timeout,
        listeners=[LoggingListener()],
        name=name,
    )


# One breaker per external dependency
anthropic_breaker = make_breaker("anthropic", fail_max=5, reset_timeout=60)
web_search_breaker = make_breaker("web_search", fail_max=3, reset_timeout=30)
database_breaker = make_breaker("database", fail_max=3, reset_timeout=30)
# Add more breakers for each external API your tools call


async def call_with_breaker(
    breaker: pybreaker.CircuitBreaker,
    coro_factory: Callable[[], Awaitable[Any]],
    fallback_value: Any = None,
    operation_name: str = "operation",
) -> Any:
    """Call an async function protected by a circuit breaker.
    
    Why coro_factory (a callable) instead of a coroutine?
    Because pybreaker wraps the call in a try/except. If you pass a coroutine
    directly, it has already started executing. We pass a factory so pybreaker
    can decide whether to even start the call (OPEN state rejects immediately).
    
    fallback_value: what to return if the circuit is open.
    Set to None to let the exception propagate to the caller.
    """
    try:
        # pybreaker wraps the function call — if OPEN, raises CircuitBreakerError
        # immediately without calling the function
        return await breaker.call_async(coro_factory)
    except pybreaker.CircuitBreakerError as e:
        logger.error(
            "Circuit open for '%s' — rejecting request immediately. "
            "Will test recovery in %ds",
            operation_name, breaker.reset_timeout,
        )
        if fallback_value is not None:
            return fallback_value
        raise
```

```python
# Example: wrapping LLM calls with a circuit breaker
# resilience/protected_llm.py
import os
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage
from resilience.circuit_breakers import anthropic_breaker, call_with_breaker
from resilience.timeouts import create_llm_with_timeout

load_dotenv()

llm = create_llm_with_timeout(timeout_seconds=30.0)

FALLBACK_MESSAGE = (
    "I'm temporarily unable to process your request due to a service issue. "
    "Please try again in a moment."
)


async def invoke_llm_protected(messages: list[BaseMessage]) -> str:
    """Invoke the LLM with circuit breaker protection.
    
    If Anthropic is having an outage, the circuit opens after 5 failures
    and subsequent requests are rejected immediately (< 1ms) instead of
    waiting 30s for a timeout. This protects your server from overload.
    """
    result = await call_with_breaker(
        breaker=anthropic_breaker,
        # Lambda wraps the coroutine in a factory
        coro_factory=lambda: llm.ainvoke(messages),
        fallback_value=FALLBACK_MESSAGE,
        operation_name="anthropic_llm",
    )
    
    if isinstance(result, str):
        # Returned the fallback — circuit is open
        return result
    
    return result.content
```

---

## Pattern 4 — Exponential Backoff with Jitter

**Why naive retries cause retry storms**

Suppose your LLM provider rate-limits you. You have 100 concurrent requests, all failing at the same time. If every request retries after exactly 1 second, you send 100 simultaneous requests again — which also all fail. This pattern (retry storm) can overwhelm a recovering service and keep it down.

The solution: **jitter** — randomize the retry delay so retries are spread out over time.

**Exponential backoff with jitter:**
- First retry: wait 1–3 seconds (not exactly 1 second)
- Second retry: wait 2–7 seconds
- Third retry: wait 4–15 seconds

The random component prevents all retries from firing simultaneously.

```python
# resilience/retry.py
import logging
import functools
from typing import Any, Callable, Awaitable

import tenacity
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
    before_sleep_log,
)
from anthropic import RateLimitError, APIConnectionError, APIStatusError

logger = logging.getLogger(__name__)

# Errors that are worth retrying — transient, not caused by our code
RETRYABLE_EXCEPTIONS = (
    RateLimitError,        # 429 — provider is throttling us
    APIConnectionError,    # Network failure, DNS resolution failure
)

# Errors we should NEVER retry — retrying wastes quota and hides bugs
NON_RETRYABLE_EXCEPTIONS = (
    # 401 — invalid API key (retrying will never fix this)
    # 400 — malformed request (retrying won't fix our bug)
    # These are excluded by using retry_if_exception_type above
)


def is_retryable(exc: Exception) -> bool:
    """Determine if an exception is worth retrying.
    
    Key principle: only retry transient failures. Permanent failures
    (authentication, invalid input) should fail fast.
    
    For 5xx errors: retry because the server is temporarily unavailable.
    For 4xx errors: do NOT retry — we sent a bad request and the server is
    correctly rejecting it.
    """
    if isinstance(exc, RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, APIStatusError):
        # 5xx = server error = retryable
        # 4xx = client error = NOT retryable (except 429 which is RateLimitError)
        return exc.status_code >= 500
    return False


def log_retry_to_langsmith(retry_state: tenacity.RetryCallState) -> None:
    """Callback called before each retry sleep. Logs to both Python logger
    and LangSmith (via metadata on the next invocation).
    
    LangSmith tracing picks up the retry count automatically if you add it
    to the run metadata. See: https://docs.smith.langchain.com/
    """
    logger.warning(
        "Retry #%d for '%s' after %.2fs — error: %s",
        retry_state.attempt_number,
        retry_state.fn.__name__ if retry_state.fn else "unknown",
        retry_state.outcome_timestamp - retry_state.start_time if retry_state.outcome_timestamp else 0,
        retry_state.outcome.exception() if retry_state.outcome else "unknown",
    )


# USER_FACING_RETRY: short retry budget — user is waiting
USER_FACING_RETRY = retry(
    retry=tenacity.retry_if_exception(is_retryable),
    stop=stop_after_attempt(3),          # Max 3 attempts (1 original + 2 retries)
    wait=wait_exponential_jitter(
        initial=1,    # First retry after ~1s (with jitter: 0.5s–2s)
        max=30,       # Cap at ~30s (with jitter: 15s–45s)
        jitter=2,     # Add up to 2s of random jitter on top of exponential
    ),
    before_sleep=log_retry_to_langsmith,
    reraise=True,    # After all retries exhausted, raise the original exception
)

# BACKGROUND_RETRY: longer retry budget — no user waiting, maximize success rate
BACKGROUND_RETRY = retry(
    retry=tenacity.retry_if_exception(is_retryable),
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(
        initial=2,
        max=60,
        jitter=5,
    ),
    before_sleep=log_retry_to_langsmith,
    reraise=True,
)


# Usage as a decorator:
@USER_FACING_RETRY
async def call_llm_with_retry(llm, messages):
    """Call the LLM with automatic retry on transient failures.
    
    Decorating with @USER_FACING_RETRY means: if the call raises a
    RateLimitError or APIConnectionError, wait with jitter and retry
    up to 3 total attempts before giving up.
    """
    return await llm.ainvoke(messages)


# Usage as a wrapper (useful when you can't use decorators):
async def call_tool_with_retry(tool, input: Any) -> Any:
    """Wrap a tool call with retry logic at call-time.
    
    tenacity.AsyncRetrying is the programmatic version of the @retry decorator.
    Use this when you need to retry a coroutine that you're constructing
    dynamically (e.g., different tools in a loop).
    """
    async for attempt in tenacity.AsyncRetrying(
        retry=tenacity.retry_if_exception(is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=30, jitter=2),
        before_sleep=log_retry_to_langsmith,
        reraise=True,
    ):
        with attempt:
            return await tool.ainvoke(input)
```

---

## Pattern 5 — Bulkhead Isolation (Multi-Agent)

**What is a bulkhead?**

Named after ship bulkheads (watertight compartments), bulkhead isolation ensures that a slow or failing specialist agent cannot consume all concurrency slots in a supervisor pattern.

Without bulkheads: if the "code_execution" specialist is slow (running user code takes 60s), it can consume all your asyncio concurrency, leaving the "web_search" specialist (which takes 2s) unable to run at all.

With bulkheads: each specialist gets its own concurrency limit via `asyncio.Semaphore`. The slow specialist can only hold 3 slots; the other specialists always have slots available.

```python
# resilience/bulkhead.py
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Each specialist gets its own semaphore — its "bulkhead"
# The number reflects how many concurrent calls you expect to be reasonable
# for that specialist's resource usage.
BULKHEADS = {
    "web_search":      asyncio.Semaphore(5),   # Fast, I/O bound — allow more
    "code_execution":  asyncio.Semaphore(2),   # Slow, CPU bound — limit tightly
    "database_query":  asyncio.Semaphore(5),   # Backed by connection pool
    "file_processing": asyncio.Semaphore(3),   # Memory-intensive
}


async def call_with_bulkhead(
    specialist_name: str,
    coro,
    timeout: float = 30.0,
) -> Any:
    """Acquire the bulkhead semaphore for a specialist before calling it.
    
    If all slots are taken, this waits — but only up to `timeout` seconds.
    This prevents one slow specialist from blocking forever.
    
    asyncio.Semaphore(n) allows n concurrent holders. The (n+1)th caller
    waits until one of the n releases.
    """
    semaphore = BULKHEADS.get(specialist_name)
    if semaphore is None:
        # If no bulkhead configured, use a generous default
        logger.warning("No bulkhead configured for specialist '%s' — using default", specialist_name)
        semaphore = asyncio.Semaphore(5)
    
    try:
        # acquire() blocks until a slot is free
        async with asyncio.timeout(timeout):
            async with semaphore:
                logger.debug(
                    "Bulkhead '%s' acquired (slots in use: ~%d)",
                    specialist_name,
                    # Semaphore doesn't expose current count directly, but
                    # _value gives remaining slots (internal API, use carefully)
                    getattr(semaphore, "_value", "?"),
                )
                return await coro
    except asyncio.TimeoutError:
        logger.error(
            "Bulkhead '%s' timed out after %.1fs waiting for a slot",
            specialist_name, timeout
        )
        raise TimeoutError(
            f"Specialist '{specialist_name}' is overloaded. "
            "All concurrency slots are busy. Please try again."
        )
```

```python
# Complete supervisor graph with bulkheads
# graphs/supervisor_with_bulkheads.py
import asyncio
import operator
import os
from typing import Annotated, Any, Sequence
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command
from pydantic import BaseModel, Field
from resilience.bulkhead import call_with_bulkhead
from resilience.timeouts import TOOL_TIMEOUT, LLM_TIMEOUT

load_dotenv()

# ── State ──────────────────────────────────────────────────────────────────
class SupervisorState(BaseModel):
    messages: Annotated[list[BaseMessage], operator.add] = Field(default_factory=list)
    next_specialist: str = "FINISH"
    specialist_results: dict[str, Any] = Field(default_factory=dict)
    # Retry budget — see Pattern 7
    retry_count: int = 0

# ── Specialists ────────────────────────────────────────────────────────────
llm = ChatAnthropic(model="claude-sonnet-4-6")

async def web_search_specialist(state: SupervisorState) -> SupervisorState:
    """Specialist runs inside its bulkhead — max 5 concurrent calls."""
    query = state.messages[-1].content
    
    result = await call_with_bulkhead(
        specialist_name="web_search",
        coro=web_search_tool.ainvoke({"query": query}),
        timeout=TOOL_TIMEOUT,
    )
    return {"specialist_results": {"web_search": result}}


async def code_execution_specialist(state: SupervisorState) -> SupervisorState:
    """Code execution is CPU-intensive — bulkhead limits to 2 concurrent."""
    code = state.messages[-1].content
    
    result = await call_with_bulkhead(
        specialist_name="code_execution",
        coro=code_executor_tool.ainvoke({"code": code}),
        timeout=60.0,  # Code execution gets more time
    )
    return {"specialist_results": {"code_execution": result}}


async def supervisor_node(state: SupervisorState) -> Command:
    """Supervisor decides which specialist to call next, or FINISH.
    
    Command is the LangGraph way to dynamically route to the next node.
    The supervisor LLM reads the conversation and decides what to do next.
    """
    response = await llm.ainvoke(
        state.messages,
        # Structured output ensures the LLM always returns a valid specialist name
    )
    
    next_node = response.content.strip()
    if next_node not in ("web_search", "code_execution", "FINISH"):
        next_node = "FINISH"
    
    return Command(goto=next_node, update={"next_specialist": next_node})


# ── Graph ──────────────────────────────────────────────────────────────────
def build_supervisor_graph() -> StateGraph:
    builder = StateGraph(SupervisorState)
    
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("web_search", web_search_specialist)
    builder.add_node("code_execution", code_execution_specialist)
    
    builder.add_edge(START, "supervisor")
    builder.add_edge("web_search", "supervisor")
    builder.add_edge("code_execution", "supervisor")
    builder.add_edge("supervisor", END)  # reached when next_specialist == "FINISH"
    
    return builder.compile(
        # recursion_limit: maximum number of node executions before giving up
        # Prevents infinite loops in the supervisor (e.g., LLM always says "web_search")
        # Set this to (max_specialist_calls × 2) + 1 to be safe
        recursion_limit=25,
    )
```

---

## Pattern 6 — Graceful Degradation

**The degradation chain**

When your primary service is unavailable, instead of returning an error, work down a chain of fallbacks:

```
1. Try primary service (LLM + tools)
2. If circuit open → check Redis exact-match cache
3. If cache miss → check semantic cache (similar past queries)
4. If no semantic match → check static fallback table
5. If no fallback → return structured error (last resort)
```

Each level is cheaper and faster than the one above it. Users get a response even during outages.

```python
# resilience/cache.py
import hashlib
import json
import logging
import os
from typing import Optional

import redis.asyncio as redis
from langchain.cache import InMemoryCache
from langchain.globals import set_llm_cache
from langchain_anthropic import ChatAnthropic
from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)

# L1 Cache: Redis exact-match cache (fastest — O(1) lookup)
# Key: SHA-256 of the normalized query string
# Value: the serialized LLM response
# TTL: 1 hour (adjust based on how often your answers go stale)

CACHE_TTL_SECONDS = 3600


class ResponseCache:
    """Exact-match response cache backed by Redis.
    
    Exact-match means the query must be byte-for-byte identical to a cached
    query. This is a good L1 cache for repeated identical queries (e.g., FAQ
    bots where users ask the same questions repeatedly).
    
    For near-duplicate queries ("What is X?" vs "Can you explain X?"),
    use the semantic cache instead.
    """
    
    def __init__(self, redis_url: str):
        self.client = redis.from_url(redis_url, decode_responses=True)
    
    def _cache_key(self, query: str) -> str:
        """Normalize and hash the query to a stable cache key."""
        normalized = query.strip().lower()
        return f"llm_cache:{hashlib.sha256(normalized.encode()).hexdigest()}"
    
    async def get(self, query: str) -> Optional[str]:
        """Return cached response if available, else None."""
        key = self._cache_key(query)
        cached = await self.client.get(key)
        if cached:
            logger.info("Cache HIT for query (key: %s...)", key[:16])
        return cached
    
    async def set(self, query: str, response: str) -> None:
        """Cache a response with TTL."""
        key = self._cache_key(query)
        await self.client.setex(key, CACHE_TTL_SECONDS, response)
        logger.debug("Cached response for query (key: %s...)", key[:16])
    
    async def close(self) -> None:
        await self.client.aclose()


# L2 Cache: LangChain InMemoryCache (semantic — fuzzy match)
# LangChain's built-in cache intercepts identical LLM calls automatically.
# Set it globally so all ChatAnthropic calls benefit.
set_llm_cache(InMemoryCache())
# For production with Redis: use RedisSemanticCache from langchain_community


# Static fallback table: known query patterns with static answers
# Use for high-confidence answers that rarely change (status pages, policies, etc.)
STATIC_FALLBACKS = {
    "what is your refund policy": "Our refund policy is available at example.com/refund",
    "are you available": "Our service status page is at status.example.com",
    "contact support": "Contact support at support@example.com or call 1-800-EXAMPLE",
}


def get_static_fallback(query: str) -> Optional[str]:
    """Check the static fallback table for a matching query.
    
    Uses simple substring matching — for production, consider using
    fuzzy matching (rapidfuzz) or a keyword classifier.
    """
    query_lower = query.strip().lower()
    for pattern, response in STATIC_FALLBACKS.items():
        if pattern in query_lower:
            logger.info("Serving static fallback for query matching pattern '%s'", pattern)
            return response
    return None


# CacheBackedEmbeddings: cache embedding computations to reduce API costs
# Embedding the same text twice costs money. This caches by text hash.
# Particularly valuable if you're embedding many similar documents.
def create_cached_embeddings(base_embeddings: Embeddings) -> Embeddings:
    """Wrap an embeddings model with a local cache.
    
    Without caching: embedding "Hello world" 1000 times = 1000 API calls
    With caching: embedding "Hello world" 1000 times = 1 API call + 999 cache hits
    
    Requires: pip install langchain-community
    """
    from langchain.embeddings import CacheBackedEmbeddings
    from langchain.storage import LocalFileStore
    
    store = LocalFileStore("./.embedding_cache")
    return CacheBackedEmbeddings.from_bytes_store(
        underlying_embeddings=base_embeddings,
        document_embedding_cache=store,
        namespace=base_embeddings.model,
    )
```

```python
# resilience/degradation.py — the full degradation chain
import logging
from typing import Optional

import pybreaker

from resilience.cache import ResponseCache, get_static_fallback
from resilience.circuit_breakers import anthropic_breaker, call_with_breaker
from resilience.retry import USER_FACING_RETRY

logger = logging.getLogger(__name__)

LAST_RESORT_ERROR = (
    "I'm temporarily unavailable due to a service issue. "
    "Your request has been logged and we'll process it shortly."
)


async def answer_with_degradation(
    query: str,
    graph,
    config: dict,
    cache: ResponseCache,
) -> str:
    """Answer a query using the full degradation chain.
    
    Level 1: Try the graph (LLM + tools) — full capability
    Level 2: Redis exact-match cache — instant, stale-tolerant
    Level 3: Static fallback — for known query patterns
    Level 4: Structured error — last resort with actionable message
    """
    
    # Level 1: Try the primary path
    try:
        result = await call_with_breaker(
            breaker=anthropic_breaker,
            coro_factory=lambda: graph.ainvoke(
                {"messages": [{"role": "user", "content": query}]},
                config,
            ),
            fallback_value=None,  # None means "let me try fallbacks"
            operation_name="graph_primary",
        )
        
        if result:
            response = result["messages"][-1].content
            # Cache the successful response for future degraded service
            await cache.set(query, response)
            return response
    
    except Exception as e:
        logger.warning("Primary path failed: %s — trying degradation chain", e)
    
    # Level 2: Redis exact-match cache
    cached = await cache.get(query)
    if cached:
        logger.info("Serving cached response (degraded mode)")
        return f"[Cached] {cached}"
    
    # Level 3: Static fallback
    fallback = get_static_fallback(query)
    if fallback:
        return fallback
    
    # Level 4: Last resort error
    logger.error("All degradation levels exhausted for query: %s", query[:100])
    return LAST_RESORT_ERROR
```

---

## Pattern 7 — Retry Budget State Field

**The nested retry amplification problem**

Suppose your graph has 3 nodes, each retrying up to 3 times. A single graph run with a persistent failure will attempt: 3 × 3 × 3 = 27 total calls. At scale, this multiplies your API costs and load dramatically.

The retry budget is a state field that tracks total retries across all nodes. Each node checks this budget before retrying, and the graph stops when the budget is exhausted.

```python
# resilience/retry_budget.py
import operator
from typing import Annotated
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END

# Maximum retries shared across all nodes in a single graph run.
# This is the "circuit breaker" at the graph level.
MAX_GRAPH_RETRIES = 5


class ResilientState(BaseModel):
    """Base state class with retry budget built in.
    
    Every agent that needs retries should extend this class.
    The retry_count field is incremented each time any node retries,
    and nodes check it before attempting work.
    
    Why Annotated[int, operator.add]? LangGraph uses this annotation to know
    HOW to merge state updates. operator.add means returned values are ADDED
    to the existing count (not replaced). This correctly accumulates retries
    across nodes.
    """
    messages: Annotated[list, operator.add] = Field(default_factory=list)
    retry_count: Annotated[int, operator.add] = 0
    error: str = ""
    result: str = ""


async def node_with_retry_budget(state: ResilientState) -> dict:
    """A node that respects the shared retry budget.
    
    Before doing any work, check the budget. If exhausted, return an error
    immediately. This prevents nested retries from amplifying API calls.
    """
    # Guard: check budget before attempting work
    if state.retry_count >= MAX_GRAPH_RETRIES:
        return {
            "error": f"Retry budget exhausted ({MAX_GRAPH_RETRIES} retries used). "
                     "Manual intervention required.",
            "result": "",
        }
    
    try:
        result = await some_external_call()
        return {"result": result, "retry_count": 0}  # 0 means no retry needed
    
    except RetryableError as e:
        # Increment the budget by 1 for this retry
        # operator.add annotation means this 1 is ADDED to existing count
        return {
            "retry_count": 1,
            "error": str(e),
        }
    
    except PermanentError as e:
        # Don't increment budget for permanent errors — they won't benefit from retrying
        return {"error": str(e), "result": ""}


def should_retry(state: ResilientState) -> str:
    """Conditional edge: retry the node or give up based on budget."""
    if state.error and state.retry_count < MAX_GRAPH_RETRIES:
        return "retry"
    elif state.error:
        return "dead_letter"  # Budget exhausted — route to DLQ (Pattern 8)
    return "next_node"


def build_resilient_graph():
    builder = StateGraph(ResilientState)
    
    builder.add_node("work", node_with_retry_budget)
    builder.add_node("dead_letter", dead_letter_node)  # See Pattern 8
    builder.add_node("next_node", next_node)
    
    builder.add_edge(START, "work")
    builder.add_conditional_edges("work", should_retry, {
        "retry": "work",           # Loop back and try again
        "dead_letter": "dead_letter",
        "next_node": "next_node",
    })
    builder.add_edge("next_node", END)
    builder.add_edge("dead_letter", END)
    
    # recursion_limit is the absolute cap on node visits.
    # Must be > MAX_GRAPH_RETRIES to allow retries to execute.
    return builder.compile(recursion_limit=MAX_GRAPH_RETRIES + 10)
```

---

## Pattern 8 — Dead Letter Queue

**What is a dead letter queue (DLQ)?**

A DLQ is a storage location for messages (graph runs) that have failed all retry attempts. Instead of losing the work, you capture it for:
1. Offline retry with manual investigation
2. Human-in-the-loop recovery
3. Alerting and SLA tracking

```python
# resilience/dlq.py
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import asyncpg
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ── Dead Letter Queue storage ──────────────────────────────────────────────

CREATE_DLQ_TABLE = """
CREATE TABLE IF NOT EXISTS failed_runs (
    id          SERIAL PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    input       JSONB NOT NULL,
    error       TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    next_retry  TIMESTAMPTZ,
    resolved    BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,
    notes       TEXT           -- For human reviewers
);

CREATE INDEX IF NOT EXISTS failed_runs_next_retry ON failed_runs (next_retry)
    WHERE resolved = FALSE;
"""


async def setup_dlq(pool: asyncpg.Pool) -> None:
    """Create the dead letter queue table. Call during application startup."""
    async with pool.acquire() as conn:
        await conn.execute(CREATE_DLQ_TABLE)
    logger.info("Dead letter queue table ready")


async def send_to_dlq(
    pool: asyncpg.Pool,
    thread_id: str,
    input: dict[str, Any],
    error: str,
    retry_count: int,
) -> int:
    """Write a failed graph run to the dead letter queue.
    
    Returns the DLQ entry ID for reference in logs and alerts.
    
    Why store the full input? So the background worker can replay the
    exact same request without any reconstruction.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO failed_runs (thread_id, input, error, retry_count, next_retry)
            VALUES ($1, $2, $3, $4, NOW() + INTERVAL '5 minutes')
            RETURNING id
            """,
            thread_id,
            json.dumps(input),
            error,
            retry_count,
        )
    dlq_id = row["id"]
    logger.error(
        "Graph run sent to DLQ (id=%d, thread=%s, retries=%d): %s",
        dlq_id, thread_id, retry_count, error[:200]
    )
    return dlq_id


# ── Background DLQ worker ──────────────────────────────────────────────────

async def dlq_worker(pool: asyncpg.Pool, graph) -> None:
    """Background worker that polls the DLQ and retries failed runs.
    
    This runs as a long-lived asyncio task alongside your main application.
    It uses SELECT ... FOR UPDATE SKIP LOCKED to safely handle multiple
    worker replicas without double-processing.
    
    SELECT FOR UPDATE: locks the row so other workers skip it
    SKIP LOCKED: instead of waiting for the lock, skip to the next row
    This is the standard PostgreSQL pattern for building job queues.
    """
    logger.info("DLQ worker started")
    
    while True:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Fetch one overdue, unresolved entry
                    row = await conn.fetchrow(
                        """
                        SELECT id, thread_id, input, retry_count
                        FROM failed_runs
                        WHERE resolved = FALSE
                          AND next_retry <= NOW()
                        ORDER BY next_retry ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """,
                    )
                    
                    if row is None:
                        # Nothing to process — sleep and check again
                        break  # Exit transaction, sleep below
                    
                    dlq_id = row["id"]
                    thread_id = row["thread_id"]
                    input_data = json.loads(row["input"])
                    retry_count = row["retry_count"]
                    
                    logger.info("DLQ worker retrying run (id=%d, thread=%s)", dlq_id, thread_id)
                    
                    try:
                        # Attempt to replay the graph run
                        await graph.ainvoke(
                            input_data,
                            {"configurable": {"thread_id": thread_id}},
                        )
                        
                        # Success — mark as resolved
                        await conn.execute(
                            """
                            UPDATE failed_runs
                            SET resolved = TRUE, resolved_at = NOW()
                            WHERE id = $1
                            """,
                            dlq_id,
                        )
                        logger.info("DLQ run %d resolved successfully", dlq_id)
                    
                    except Exception as e:
                        # Failed again — schedule next retry with exponential backoff
                        new_retry_count = retry_count + 1
                        
                        if new_retry_count >= 10:
                            # Too many retries — escalate to human review
                            await conn.execute(
                                """
                                UPDATE failed_runs
                                SET retry_count = $1, error = $2,
                                    notes = 'Escalated for human review after 10 retries'
                                WHERE id = $3
                                """,
                                new_retry_count, str(e), dlq_id,
                            )
                            logger.critical(
                                "DLQ run %d requires human review after %d retries",
                                dlq_id, new_retry_count,
                            )
                        else:
                            # Exponential backoff: 5m, 10m, 20m, 40m, ...
                            backoff_minutes = 5 * (2 ** retry_count)
                            await conn.execute(
                                """
                                UPDATE failed_runs
                                SET retry_count = $1, error = $2,
                                    next_retry = NOW() + ($3 * INTERVAL '1 minute')
                                WHERE id = $4
                                """,
                                new_retry_count, str(e), backoff_minutes, dlq_id,
                            )
                            logger.warning(
                                "DLQ run %d failed again (attempt %d) — retry in %dm",
                                dlq_id, new_retry_count, backoff_minutes,
                            )
        
        except Exception as e:
            logger.error("DLQ worker error: %s", e)
        
        # Poll interval: check for new items every 30 seconds
        await asyncio.sleep(30)


async def start_dlq_worker(pool: asyncpg.Pool, graph) -> asyncio.Task:
    """Start the DLQ worker as a background asyncio task.
    
    Call this during application startup (e.g., in the FastAPI lifespan handler).
    The task runs for the lifetime of the process.
    """
    return asyncio.create_task(dlq_worker(pool, graph))
```

```python
# The dead_letter_node for use in the LangGraph graph (Pattern 7 integration)
async def dead_letter_node(state: ResilientState) -> dict:
    """LangGraph node that writes the current state to the DLQ.
    
    Called when the retry budget is exhausted. This ensures failed runs are
    preserved for investigation rather than silently dropped.
    """
    from resilience.db_pool import get_pool
    import os
    
    pool = await get_pool(os.environ["DATABASE_URL"])
    
    dlq_id = await send_to_dlq(
        pool=pool,
        thread_id=state.get("thread_id", "unknown"),
        input={"messages": [m.dict() for m in state.messages]},
        error=state.error,
        retry_count=state.retry_count,
    )
    
    return {
        "result": f"Request queued for retry (reference: DLQ-{dlq_id}). "
                  "We'll process it automatically. Sorry for the inconvenience.",
    }
```

---

## Installation

```bash
pip install \
    langchain>=0.3.0 \
    langgraph>=1.2.0 \
    langsmith>=0.2.0 \
    langchain-anthropic \
    langgraph-checkpoint-postgres \
    asyncpg \
    pybreaker \
    tenacity \
    httpx \
    redis[asyncio] \
    pydantic>=2.0 \
    python-dotenv \
    fastapi \
    uvicorn
```

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
LANGSMITH_API_KEY=ls__...
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=my-resilient-agent
DATABASE_URL=postgresql://user:password@localhost:5432/mydb
REDIS_URL=redis://localhost:6379
```

---

## Output: production-checklist.md Generation

After scaffolding, generate a `production-checklist.md` file in the project root. Use this exact content as the template, filling in the user's specific timeout values from their Question 3 answer:

```markdown
# Production Readiness Checklist

Generated by lc:resilience — $(date)

## Connection Management
- [ ] asyncpg.create_pool() used (NOT single-connection psycopg.connect)
- [ ] min_size and max_size tuned to concurrent user count
- [ ] command_timeout=30 set on pool
- [ ] Health check query (SELECT 1) runs on startup
- [ ] Pool closed gracefully in application shutdown handler
- [ ] Pool stats logged every 60 seconds

## Timeouts
- [ ] Tool timeout set: TOOL_TIMEOUT = 10.0s (or your configured value)
- [ ] LLM timeout set: LLM_TIMEOUT = 30.0s via httpx.AsyncClient
- [ ] Graph timeout set: GRAPH_TIMEOUT = 120.0s via asyncio.wait_for
- [ ] TimeoutError returns structured error message (not 500)
- [ ] Slow nodes (code execution, file I/O) have extended timeouts

## Circuit Breakers
- [ ] pybreaker installed
- [ ] Separate breaker per external dependency (Anthropic, search, DB, etc.)
- [ ] fail_max=5, reset_timeout=60 configured (or tuned values)
- [ ] CircuitBreakerError caught and handled (fallback or cache)
- [ ] Breaker state changes logged with WARNING level
- [ ] Breaker state exposed in health endpoint

## Retries
- [ ] tenacity installed
- [ ] wait_exponential_jitter() used (NOT wait_fixed or wait_exponential alone)
- [ ] Retry only on: RateLimitError, APIConnectionError, 5xx errors
- [ ] Never retry on: AuthenticationError, 4xx errors
- [ ] User-facing: max 3 retries
- [ ] Background jobs: max 5 retries
- [ ] Retry events logged with attempt number

## Bulkheads (multi-agent only)
- [ ] asyncio.Semaphore per specialist agent
- [ ] Semaphore sized to specialist resource usage (2-5 typical)
- [ ] Timeout on semaphore acquisition (not just on the call)
- [ ] recursion_limit set on all compiled graphs

## Caching
- [ ] Redis exact-match cache for repeated queries
- [ ] LangChain InMemoryCache set globally (or RedisSemanticCache)
- [ ] CacheBackedEmbeddings used if embedding frequently
- [ ] Cache TTL set based on data staleness tolerance
- [ ] Cache hit/miss logged for observability

## Dead Letter Queue
- [ ] failed_runs table created in PostgreSQL
- [ ] DLQ worker running as background asyncio task
- [ ] Exponential backoff on DLQ retry (5min, 10min, 20min, ...)
- [ ] Human escalation after 10 retries
- [ ] DLQ reference ID returned to user in error message

## Retry Budget
- [ ] retry_count field in LangGraph state (Annotated[int, operator.add])
- [ ] MAX_GRAPH_RETRIES constant defined per graph (recommend: 5)
- [ ] Every retrying node checks retry_count before attempting
- [ ] Budget-exhausted state routes to dead_letter node
- [ ] recursion_limit > MAX_GRAPH_RETRIES on compiled graph

## Observability
- [ ] LANGCHAIN_TRACING_V2=true in .env
- [ ] LANGSMITH_PROJECT set to meaningful name
- [ ] All timeout events logged at ERROR level with duration
- [ ] All circuit breaker state changes logged at WARNING
- [ ] All DLQ entries logged at ERROR with reference ID
- [ ] Retry attempts logged at WARNING with attempt number

## Load Testing
- [ ] Tested with concurrent_users × 2 simultaneous requests
- [ ] Pool exhaustion tested (simulate with max_size=2 temporarily)
- [ ] Circuit breaker tested by blocking a dependency temporarily
- [ ] DLQ tested by inserting a failing run manually
- [ ] Timeout tested by adding artificial delays in staging
```

---

## Concepts Taught (Reference)

| Concept | Pattern | First Appears |
|---|---|---|
| Connection pool | Why single-connection breaks | Pattern 1 |
| Pool sizing (min/max) | Based on concurrent users | Pattern 1 |
| Health check on startup | Fail fast, not on first user | Pattern 1 |
| asyncio.wait_for() | Timeout any coroutine | Pattern 2 |
| httpx.AsyncClient timeout | HTTP-level timeout (4 components) | Pattern 2 |
| Circuit breaker states | CLOSED/OPEN/HALF_OPEN | Pattern 3 |
| Per-dependency breakers | Isolation prevents cascade | Pattern 3 |
| Retry storm | Why naive retries fail | Pattern 4 |
| Jitter | Randomize to spread retries | Pattern 4 |
| Retryable vs permanent errors | RateLimitError yes, AuthError no | Pattern 4 |
| Bulkhead | Semaphore per specialist | Pattern 5 |
| Semaphore semantics | Concurrent holder limit | Pattern 5 |
| Degradation chain | Cache → fallback → error | Pattern 6 |
| Exact-match vs semantic cache | O(1) lookup vs similarity | Pattern 6 |
| CacheBackedEmbeddings | Cost reduction for repeated text | Pattern 6 |
| Annotated[int, operator.add] | LangGraph state accumulation | Pattern 7 |
| Retry budget | Shared counter across nodes | Pattern 7 |
| Dead letter queue | Preserve failed work for replay | Pattern 8 |
| SELECT FOR UPDATE SKIP LOCKED | Safe multi-worker job queue | Pattern 8 |

---

## Transitions

After scaffolding:

1. **If the user is deploying**: suggest `lc:deploy` for containerizing with the pool and DLQ worker.
2. **If the user wants observability**: suggest `lc:monitor` for LangSmith dashboards and alerting.
3. **If the user wants to test the patterns**: suggest `lc:test` for load testing and chaos testing the circuit breakers.
4. **If the user wants to trace the retry/timeout events**: suggest `lc:trace` for custom LangSmith trace annotations.
