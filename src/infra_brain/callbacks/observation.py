import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import datetime, timezone

from langchain_core.callbacks import AsyncCallbackHandler, BaseCallbackHandler

from infra_brain.config import get_settings
from infra_brain.db.models import Observation
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)


class ObservationCallbackHandler(BaseCallbackHandler):
    """Sync variant — kept for ``agents/llm_base.py``'s sync ReAct loop."""

    def __init__(self, agent_name: str, domain: str):
        super().__init__()
        self.agent_name = agent_name
        self.domain = domain

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id: uuid.UUID, **kwargs):
        if not get_settings().infra_ops_observe:
            return
        tool = serialized.get("name", "unknown")
        with get_session() as session:
            _upsert_observation_sync(session, self.agent_name, tool, self.domain)
            session.commit()


def _upsert_observation_sync(session, agent_name: str, tool: str, domain: str) -> None:
    bind = session.get_bind()
    dialect = getattr(bind, "dialect", None)
    dialect_name = getattr(dialect, "name", "") if dialect else ""
    if dialect_name == "postgresql":
        # Atomic upsert — no read-modify-write race
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(Observation).values(
            id=uuid.uuid4(),
            agent=agent_name,
            tool=tool,
            domain=domain,
            pattern=f"{agent_name} used {tool}",
            count=1,
            last_seen=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["agent", "tool", "domain"],
            set_={
                "count": Observation.count + 1,
                "last_seen": datetime.now(timezone.utc),
            },
        )
        session.execute(stmt)
    else:
        # SQLite-safe fallback: read-modify-write (acceptable for tests)
        existing = (
            session.query(Observation).filter_by(agent=agent_name, tool=tool, domain=domain).first()
        )
        if existing:
            existing.count += 1
            existing.last_seen = datetime.now(timezone.utc)
        else:
            session.add(
                Observation(
                    id=uuid.uuid4(),
                    agent=agent_name,
                    tool=tool,
                    domain=domain,
                    pattern=f"{agent_name} used {tool}",
                )
            )


# ---------------------------------------------------------------------------
# B2: async, batched writer — same rationale as callbacks/audit.py's
# _AsyncAuditWriter. Observation rows are pure usage-pattern counters (not the
# compliance-critical AuditLog/AgentActionLog pair), so batching here has no
# co-commit constraint to preserve — just accumulate (agent, tool, domain)
# occurrences and flush as one batch of upserts.
# ---------------------------------------------------------------------------

FLUSH_MAX_ITEMS = 50
FLUSH_INTERVAL_SECONDS = 1.0
_MAX_FLUSH_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 0.5


class _AsyncObservationWriter:
    def __init__(self) -> None:
        # See _AsyncAuditWriter's __init__ comment (audit.py) — the queue is
        # built lazily in start() so it always binds to the loop that is
        # actually running, instead of whatever loop happened to be running
        # at import time (or a prior test's now-closed loop).
        self._queue: asyncio.Queue[tuple[str, str, str]] | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._queue = asyncio.Queue()
            self._task = asyncio.create_task(self._run(), name="observation-writer")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        await self._drain_remaining()
        self._queue = None

    async def enqueue(self, item: tuple[str, str, str]) -> None:
        if self._queue is None:
            self.start()
        await self._queue.put(item)

    async def _run(self) -> None:
        buffer: list[tuple[str, str, str]] = []
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
        remaining: list[tuple[str, str, str]] = []
        while not self._queue.empty():
            try:
                remaining.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if remaining:
            await self._flush(remaining)

    async def _flush(self, items: list[tuple[str, str, str]]) -> None:
        for attempt in range(1, _MAX_FLUSH_RETRIES + 1):
            try:
                await asyncio.to_thread(self._write_sync, items)
                return
            except Exception:  # noqa: BLE001
                if attempt == _MAX_FLUSH_RETRIES:
                    logger.critical(
                        "[observation-writer] failed to flush %d observation(s) "
                        "after %d attempts — these are LOST",
                        len(items),
                        attempt,
                        exc_info=True,
                    )
                    return
                logger.warning(
                    "[observation-writer] flush attempt %d/%d failed, retrying",
                    attempt,
                    _MAX_FLUSH_RETRIES,
                    exc_info=True,
                )
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    @staticmethod
    def _write_sync(items: list[tuple[str, str, str]]) -> None:
        with get_session() as session:
            for agent_name, tool, domain in items:
                _upsert_observation_sync(session, agent_name, tool, domain)
            session.commit()


_observation_writer = _AsyncObservationWriter()


def start_observation_writer() -> None:
    _observation_writer.start()


async def stop_observation_writer() -> None:
    await _observation_writer.stop()


class AsyncObservationCallbackHandler(AsyncCallbackHandler):
    """Async, batched counterpart of ``ObservationCallbackHandler`` for the two
    async call sites: ``chat/agent.py`` and ``api/routers/ui.py``'s
    ``custom_view``."""

    def __init__(self, agent_name: str, domain: str):
        super().__init__()
        self.agent_name = agent_name
        self.domain = domain

    async def on_tool_start(self, serialized: dict, input_str: str, *, run_id: uuid.UUID, **kwargs):
        if not get_settings().infra_ops_observe:
            return
        tool = serialized.get("name", "unknown")
        _observation_writer.start()  # idempotent safety net
        await _observation_writer.enqueue((self.agent_name, tool, self.domain))
