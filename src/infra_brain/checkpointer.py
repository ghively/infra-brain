"""Shared LangGraph checkpointer factory (Wave 4 item 4.2 — F-026, F-005/F-009).

Two process-wide singletons, because the chat graph is driven ASYNC
(``astream_events``/``ainvoke``) while the background domain-agent ReAct loop
in ``agents/llm_base.py`` is driven SYNC (``invoke``) — a single checkpointer
type cannot serve both correctly:

- ``get_checkpointer()`` — SYNC ``PostgresSaver`` (langgraph-checkpoint-postgres)
  when ``POSTGRES_URL`` points at a real PostgreSQL database, ``MemorySaver``
  otherwise. Used by ``agents/llm_base.py``'s sync ``.invoke()`` path only.
- ``get_async_checkpointer()`` — ASYNC ``AsyncPostgresSaver`` (same package,
  ``.aio`` submodule) when Postgres is configured, ``MemorySaver`` otherwise
  (``MemorySaver`` implements both the sync and async checkpoint APIs). Used
  by ``chat/agent.py``'s async graph (``astream_events``) — the sync
  ``PostgresSaver`` does NOT implement ``aget_tuple``/``aput``/``alist`` (they
  inherit ``BaseCheckpointSaver`` stubs that raise ``NotImplementedError``),
  so driving the chat graph with the sync saver crashes the first real
  request once a real ``POSTGRES_URL`` + the package are both present.

This replaces chat/agent.py's dead ``langchain_postgres.PostgresSaver`` import:
langchain-postgres 0.0.17 never exported that name, so the old code ALWAYS
fell back to MemorySaver while its docstring claimed persistence (F-026).

NOTE: the ``langgraph.checkpoint.postgres`` / ``langgraph.checkpoint.postgres.aio``
imports below are intentionally import-guarded — ``langgraph-checkpoint-postgres``
may not be installed in every environment (e.g. offline dev boxes without
network access to add the dependency yet). Import failure degrades to
``MemorySaver`` with a warning rather than crashing graph compilation.
"""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

_checkpointer = None
_lock = threading.Lock()

_async_checkpointer = None
_async_lock = asyncio.Lock()


def _postgres_conn_str() -> str | None:
    """Return a plain psycopg3 DSN if POSTGRES_URL is a real Postgres URL, else None."""
    from infra_brain.config import get_settings

    url = get_settings().postgres_url or ""
    # PostgresSaver needs a plain psycopg3 DSN (no SQLAlchemy driver suffix).
    conn_str = (
        url.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
        .replace("postgresql+psycopg://", "postgresql://")
    )
    if not conn_str.startswith("postgresql://"):
        return None
    return conn_str


# B4: pool sizing convention matches db/session.py's existing pool (~5-10
# connections) rather than the single shared connection this used to hold.
_POOL_MIN_SIZE = 2
_POOL_MAX_SIZE = 10


def _make_checkpointer():
    from langgraph.checkpoint.memory import MemorySaver

    conn_str = _postgres_conn_str()
    if conn_str is None:
        logger.warning(
            "POSTGRES_URL is not a PostgreSQL DSN — using in-process MemorySaver "
            "(threads/checkpoints will not persist across restarts)"
        )
        return MemorySaver()
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # B4: every concurrent chat session's (well, agents/llm_base.py's sync
        # ReAct loop's) checkpoint reads/writes used to serialize on ONE shared
        # psycopg.Connection, which needed a hand-rolled _LockingPostgresSaver
        # RLock wrapper just to avoid two threads corrupting each other's
        # in-flight cursor state. A pool removes both the contention and the
        # need for that wrapper — PostgresSaver accepts a ConnectionPool
        # directly (verified against the pinned langgraph-checkpoint-postgres
        # 3.1.0). autocommit=True/row_factory=dict_row must be set on the
        # POOL's connection kwargs (not passed to PostgresSaver itself) —
        # without them the saver breaks. Do not pass `pipe=` alongside a pool;
        # the constructor raises if both are set.
        pool = ConnectionPool(
            conn_str,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
        try:
            saver = PostgresSaver(pool)
            saver.setup()  # creates the checkpoint tables if they don't exist
        except Exception:
            # Close the pool before falling back — an abandoned pool keeps
            # background workers retrying the connection forever.
            pool.close()
            raise
        logger.info(
            "PostgresSaver checkpointer initialised (pooled, min=%d max=%d)",
            _POOL_MIN_SIZE,
            _POOL_MAX_SIZE,
        )
        return saver
    except ImportError:
        logger.warning(
            "langgraph-checkpoint-postgres not installed — using in-process "
            "MemorySaver (threads/checkpoints will not persist across restarts). "
            "Install the package and re-run for durable Postgres-backed checkpointing."
        )
        return MemorySaver()
    except Exception:
        logger.warning(
            "Postgres checkpointer unavailable — falling back to MemorySaver",
            exc_info=True,
        )
        return MemorySaver()


def get_checkpointer():
    """Return the shared SYNC checkpointer singleton (thread-safe lazy init).

    For ``agents/llm_base.py``'s sync ``.invoke()`` ReAct loop only. Do NOT use
    this for the chat graph, which is driven async — use
    ``get_async_checkpointer()`` there instead.
    """
    global _checkpointer
    if _checkpointer is None:
        with _lock:
            if _checkpointer is None:
                _checkpointer = _make_checkpointer()
    return _checkpointer


async def _make_async_checkpointer():
    """Build the ASYNC checkpointer used to drive the chat graph.

    ``AsyncPostgresSaver`` when Postgres is configured and the package is
    importable, ``MemorySaver`` otherwise. Unlike the sync ``PostgresSaver``,
    ``MemorySaver`` implements the async checkpoint API (``aget_tuple``,
    ``aput``, ``alist``), so it is a safe fallback for the async path too.

    All connect/setup work happens here, inside a coroutine — callers must
    ``await`` this (directly or via ``get_async_checkpointer()``) rather than
    running it synchronously, so the blocking DDL ``setup()`` call never runs
    on the event loop thread (CLAUDE.md constraint #2).
    """
    from langgraph.checkpoint.memory import MemorySaver

    conn_str = _postgres_conn_str()
    if conn_str is None:
        logger.warning(
            "POSTGRES_URL is not a PostgreSQL DSN — using in-process MemorySaver "
            "for the chat graph (threads/checkpoints will not persist across restarts)"
        )
        return MemorySaver()
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        # B4: same rationale as the sync _make_checkpointer() above — every
        # concurrent chat session used to serialize on one shared
        # AsyncConnection. AsyncPostgresSaver accepts an AsyncConnectionPool
        # directly. Same two constraints apply: autocommit=True/dict_row go on
        # the pool's connection kwargs, and no `pipe=` alongside a pool.
        pool = AsyncConnectionPool(
            conn_str,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=False,
        )
        try:
            await pool.open()
            saver = AsyncPostgresSaver(pool)
            await saver.setup()  # creates the checkpoint tables if they don't exist
        except Exception:
            # Close the pool before falling back — an abandoned async pool's
            # background workers keep retrying the connection and can stall
            # event-loop shutdown indefinitely (this is what hung the CI test
            # job for its full 1h timeout on 2026-07-14).
            await pool.close()
            raise
        logger.info(
            "AsyncPostgresSaver checkpointer initialised (pooled, min=%d max=%d)",
            _POOL_MIN_SIZE,
            _POOL_MAX_SIZE,
        )
        return saver
    except ImportError:
        logger.warning(
            "langgraph-checkpoint-postgres not installed — using in-process "
            "MemorySaver for the chat graph (threads/checkpoints will not persist "
            "across restarts). Install the package and re-run for durable "
            "Postgres-backed checkpointing."
        )
        return MemorySaver()
    except Exception:
        logger.warning(
            "Async Postgres checkpointer unavailable — falling back to MemorySaver "
            "for the chat graph",
            exc_info=True,
        )
        return MemorySaver()


async def get_async_checkpointer():
    """Return the shared ASYNC checkpointer singleton (asyncio-lock lazy init).

    For ``chat/agent.py``'s async graph (``astream_events``/``ainvoke``) only.
    Never blocks the event loop: connect + DDL setup happen inside the
    awaited coroutine, not via a sync call from an async context.
    """
    global _async_checkpointer
    if _async_checkpointer is None:
        async with _async_lock:
            if _async_checkpointer is None:
                _async_checkpointer = await _make_async_checkpointer()
    return _async_checkpointer
