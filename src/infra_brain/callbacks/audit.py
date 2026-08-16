import asyncio
import hashlib
import logging
import time
import uuid
from contextlib import suppress
from typing import Any, Union

from langchain_core.callbacks import AsyncCallbackHandler, BaseCallbackHandler

from infra_brain.db.models import AgentActionLog, AuditLog
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)


class AuditCallbackHandler(BaseCallbackHandler):
    """Sync variant — kept for ``agents/llm_base.py``'s sync ReAct loop (``.invoke()``,
    not async), which is not on the FastAPI event loop and so does not hit the
    CLAUDE.md constraint #3 concern B2 addresses. Do not use this on an async
    call site — see ``AsyncAuditCallbackHandler`` below."""

    def __init__(self, agent_name: str, domain: str = ""):
        super().__init__()
        self.agent_name = agent_name
        self.domain = domain
        self._pending: dict[str, dict] = {}

    def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        self._pending[str(run_id)] = {
            "tool": serialized.get("name", "unknown"),
            "input_hash": hashlib.sha256(input_str.encode()).hexdigest(),
            "args_summary": input_str[:256],
            "t0": time.monotonic(),
        }

    def on_tool_end(
        self,
        output: str,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        pending = self._pending.pop(str(run_id), {})
        output_hash = hashlib.sha256(str(output).encode()).hexdigest()
        t0 = pending.get("t0")
        latency_ms = (time.monotonic() - t0) * 1000.0 if t0 is not None else None

        with get_session() as session:
            session.add(
                AuditLog(
                    id=uuid.uuid4(),
                    agent=self.agent_name,
                    tool=pending.get("tool", "unknown"),
                    input_hash=pending.get("input_hash", ""),
                    output_hash=output_hash,
                    allowed=True,
                )
            )
            session.add(
                AgentActionLog(
                    id=uuid.uuid4(),
                    run_id=str(run_id),
                    agent=self.agent_name,
                    domain=self.domain,
                    tool=pending.get("tool", "unknown"),
                    args_summary=pending.get("args_summary"),
                    verdict="allow",
                    status="ok",
                    error=None,
                    latency_ms=latency_ms,
                )
            )
            session.commit()

    def on_tool_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        pending = self._pending.pop(str(run_id), {})
        t0 = pending.get("t0")
        latency_ms = (time.monotonic() - t0) * 1000.0 if t0 is not None else None

        with get_session() as session:
            session.add(
                AuditLog(
                    id=uuid.uuid4(),
                    agent=self.agent_name,
                    tool=pending.get("tool", "unknown"),
                    input_hash="",
                    allowed=False,
                    denial_reason=str(error),
                )
            )
            session.add(
                AgentActionLog(
                    id=uuid.uuid4(),
                    run_id=str(run_id),
                    agent=self.agent_name,
                    domain=self.domain,
                    tool=pending.get("tool", "unknown"),
                    args_summary=pending.get("args_summary"),
                    verdict="deny",
                    status="error",
                    error=str(error),
                    latency_ms=latency_ms,
                )
            )
            session.commit()


# ---------------------------------------------------------------------------
# B2: async, batched writer for the two async call sites (chat/agent.py and
# api/routers/ui.py's custom_view). Per CLAUDE.md constraint #3, a sync DB
# commit per tool event must not run on the FastAPI event loop thread.
#
# Durability tradeoff (explicit, per plan): today's sync design commits once
# per event, so a hard crash (SIGKILL/OOM/pod eviction) loses at most the one
# in-flight event. This batched design loses up to FLUSH_MAX_ITEMS events / up
# to FLUSH_INTERVAL_SECONDS of audit history on a hard kill; a graceful
# shutdown (see main.py's lifespan) still flushes everything queued. This is
# an accepted, bounded-loss tradeoff for a compliance-relevant log — call it
# out in the MR description, do not gloss over it.
#
# AuditLog + AgentActionLog for the SAME tool event must stay co-committed
# (they were both written inside one sync commit() before). Each queued item
# below already carries both rows for one complete event, so a flush boundary
# (size- or time-triggered) can only ever land between two DIFFERENT events,
# never split a single event's pair across two commits.
# ---------------------------------------------------------------------------

FLUSH_MAX_ITEMS = 50
FLUSH_INTERVAL_SECONDS = 1.0
_MAX_FLUSH_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 0.5


class _AuditEvent:
    __slots__ = ("audit_log_kwargs", "action_log_kwargs")

    def __init__(self, audit_log_kwargs: dict, action_log_kwargs: dict):
        self.audit_log_kwargs = audit_log_kwargs
        self.action_log_kwargs = action_log_kwargs


class _AsyncAuditWriter:
    """One background asyncio task per process draining a queue of complete
    audit events, batching writes instead of committing once per event."""

    def __init__(self) -> None:
        # Not created here: asyncio.Queue binds to whichever event loop is
        # running the first time it's used. Since this object is a
        # module-level singleton, creating the queue eagerly at import time
        # (or reusing one across process-lifetime app instances, as happens
        # once per test when each test's TestClient spins up its own loop)
        # would bind it to a loop that later gets closed, raising "Queue is
        # bound to a different event loop". Build it lazily in start(),
        # which always runs inside the current (running) loop.
        self._queue: asyncio.Queue[_AuditEvent] | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._queue = asyncio.Queue()
            self._task = asyncio.create_task(self._run(), name="audit-writer")

    async def stop(self) -> None:
        """Cancel the loop but flush everything already queued first — never
        drop pending audit rows on shutdown."""
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        await self._drain_remaining()
        self._queue = None

    async def enqueue(self, event: _AuditEvent) -> None:
        if self._queue is None:
            # Writer not started (e.g. lifespan not wired in this process) —
            # start it now rather than silently dropping the event.
            self.start()
        await self._queue.put(event)

    async def _run(self) -> None:
        buffer: list[_AuditEvent] = []
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=FLUSH_INTERVAL_SECONDS)
                    buffer.append(item)
                    while len(buffer) < FLUSH_MAX_ITEMS:
                        try:
                            buffer.append(self._queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                except asyncio.TimeoutError:
                    pass
                if buffer:
                    await self._flush(buffer)
                    buffer = []
        except asyncio.CancelledError:
            if buffer:
                await self._flush(buffer)
            raise

    async def _drain_remaining(self) -> None:
        remaining: list[_AuditEvent] = []
        while not self._queue.empty():
            try:
                remaining.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if remaining:
            await self._flush(remaining)

    async def _flush(self, events: list[_AuditEvent]) -> None:
        # Retry-on-writer-failure: a transient DB error must not silently drop
        # this batch. Retry with backoff; if it still fails, log at CRITICAL
        # (never at debug/warning) so the durability loss is visible in alerts,
        # then move on rather than blocking the writer loop forever.
        for attempt in range(1, _MAX_FLUSH_RETRIES + 1):
            try:
                await asyncio.to_thread(self._write_sync, events)
                return
            except Exception:  # noqa: BLE001
                if attempt == _MAX_FLUSH_RETRIES:
                    logger.critical(
                        "[audit-writer] failed to flush %d audit event(s) after "
                        "%d attempts — these rows are LOST",
                        len(events),
                        attempt,
                        exc_info=True,
                    )
                    return
                logger.warning(
                    "[audit-writer] flush attempt %d/%d failed, retrying",
                    attempt,
                    _MAX_FLUSH_RETRIES,
                    exc_info=True,
                )
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    @staticmethod
    def _write_sync(events: list[_AuditEvent]) -> None:
        with get_session() as session:
            for event in events:
                session.add(AuditLog(**event.audit_log_kwargs))
                session.add(AgentActionLog(**event.action_log_kwargs))
            session.commit()


_audit_writer = _AsyncAuditWriter()


def start_audit_writer() -> None:
    """Call once from the FastAPI lifespan's startup phase."""
    _audit_writer.start()


async def stop_audit_writer() -> None:
    """Call once from the FastAPI lifespan's shutdown phase — flushes any
    queued audit rows before the process exits."""
    await _audit_writer.stop()


class AsyncAuditCallbackHandler(AsyncCallbackHandler):
    """Async, batched counterpart of ``AuditCallbackHandler`` for the two
    async call sites: ``chat/agent.py`` and ``api/routers/ui.py``'s
    ``custom_view``. Never commits synchronously on the event loop thread."""

    def __init__(self, agent_name: str, domain: str = ""):
        super().__init__()
        self.agent_name = agent_name
        self.domain = domain
        self._pending: dict[str, dict] = {}

    async def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        self._pending[str(run_id)] = {
            "tool": serialized.get("name", "unknown"),
            "input_hash": hashlib.sha256(input_str.encode()).hexdigest(),
            "args_summary": input_str[:256],
            "t0": time.monotonic(),
        }

    async def on_tool_end(
        self,
        output: str,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        pending = self._pending.pop(str(run_id), {})
        output_hash = hashlib.sha256(str(output).encode()).hexdigest()
        t0 = pending.get("t0")
        latency_ms = (time.monotonic() - t0) * 1000.0 if t0 is not None else None
        event = _AuditEvent(
            audit_log_kwargs=dict(
                id=uuid.uuid4(),
                agent=self.agent_name,
                tool=pending.get("tool", "unknown"),
                input_hash=pending.get("input_hash", ""),
                output_hash=output_hash,
                allowed=True,
            ),
            action_log_kwargs=dict(
                id=uuid.uuid4(),
                run_id=str(run_id),
                agent=self.agent_name,
                domain=self.domain,
                tool=pending.get("tool", "unknown"),
                args_summary=pending.get("args_summary"),
                verdict="allow",
                status="ok",
                error=None,
                latency_ms=latency_ms,
            ),
        )
        _audit_writer.start()  # idempotent safety net if lifespan didn't start it
        await _audit_writer.enqueue(event)

    async def on_tool_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        pending = self._pending.pop(str(run_id), {})
        t0 = pending.get("t0")
        latency_ms = (time.monotonic() - t0) * 1000.0 if t0 is not None else None
        event = _AuditEvent(
            audit_log_kwargs=dict(
                id=uuid.uuid4(),
                agent=self.agent_name,
                tool=pending.get("tool", "unknown"),
                input_hash="",
                allowed=False,
                denial_reason=str(error),
            ),
            action_log_kwargs=dict(
                id=uuid.uuid4(),
                run_id=str(run_id),
                agent=self.agent_name,
                domain=self.domain,
                tool=pending.get("tool", "unknown"),
                args_summary=pending.get("args_summary"),
                verdict="deny",
                status="error",
                error=str(error),
                latency_ms=latency_ms,
            ),
        )
        _audit_writer.start()
        await _audit_writer.enqueue(event)
