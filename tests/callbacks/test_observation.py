"""Direct functional tests for callbacks/observation.py (#84).

Covers: sync handler write path, sync upsert increment-on-conflict behavior,
infra_ops_observe gating, and the async batched writer's enqueue/flush cycle
(async test pattern per CLAUDE.md constraint #3 — AsyncCallbackHandler).
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infra_brain.callbacks.observation import (
    AsyncObservationCallbackHandler,
    ObservationCallbackHandler,
    _AsyncObservationWriter,
    _upsert_observation_sync,
)


def _settings(observe: bool = True):
    s = MagicMock()
    s.infra_ops_observe = observe
    return s


def test_sync_handler_writes_observation_row():
    mock_session = MagicMock()
    mock_session.get_bind.return_value = None  # non-postgres path -> read-modify-write branch
    mock_session.query.return_value.filter_by.return_value.first.return_value = None
    handler = ObservationCallbackHandler(agent_name="LinuxAgent", domain="linux")
    run_id = uuid.uuid4()

    with (
        patch("infra_brain.callbacks.observation.get_settings", return_value=_settings(True)),
        patch("infra_brain.callbacks.observation.get_session") as mock_get_session,
    ):
        mock_get_session.return_value.__enter__ = lambda s: mock_session
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)
        handler.on_tool_start({"name": "AnsibleTool"}, '{"host": "web01"}', run_id=run_id)

    mock_session.add.assert_called_once()
    added = mock_session.add.call_args[0][0]
    assert added.agent == "LinuxAgent"
    assert added.tool == "AnsibleTool"
    assert added.domain == "linux"
    mock_session.commit.assert_called_once()


def test_sync_handler_noop_when_infra_ops_observe_disabled():
    handler = ObservationCallbackHandler(agent_name="LinuxAgent", domain="linux")
    with (
        patch("infra_brain.callbacks.observation.get_settings", return_value=_settings(False)),
        patch("infra_brain.callbacks.observation.get_session") as mock_get_session,
    ):
        handler.on_tool_start({"name": "AnsibleTool"}, "{}", run_id=uuid.uuid4())
    mock_get_session.assert_not_called()


def test_upsert_observation_sync_increments_existing_row_on_sqlite_path():
    existing = MagicMock(count=3)
    mock_session = MagicMock()
    mock_session.get_bind.return_value = None
    mock_session.query.return_value.filter_by.return_value.first.return_value = existing
    _upsert_observation_sync(mock_session, "WinAgent", "AdTool", "windows")
    assert existing.count == 4
    mock_session.add.assert_not_called()  # updated in place, not re-added


def test_upsert_observation_sync_creates_new_row_when_absent():
    mock_session = MagicMock()
    mock_session.get_bind.return_value = None
    mock_session.query.return_value.filter_by.return_value.first.return_value = None
    _upsert_observation_sync(mock_session, "WinAgent", "AdTool", "windows")
    mock_session.add.assert_called_once()
    added = mock_session.add.call_args[0][0]
    assert added.agent == "WinAgent"
    assert added.tool == "AdTool"
    assert added.domain == "windows"
    assert added.pattern == "WinAgent used AdTool"


@pytest.mark.asyncio
async def test_async_writer_flushes_enqueued_items():
    writer = _AsyncObservationWriter()
    with patch.object(_AsyncObservationWriter, "_write_sync") as mock_write:
        writer.start()
        await writer.enqueue(("ChatAgent", "query_resources", "chat"))
        # FLUSH_INTERVAL_SECONDS is 1.0 — give the writer loop one tick to flush.
        await asyncio.sleep(1.2)
        await writer.stop()
    mock_write.assert_called()
    flushed_batches = [call.args[0] for call in mock_write.call_args_list]
    assert any(("ChatAgent", "query_resources", "chat") in batch for batch in flushed_batches)


@pytest.mark.asyncio
async def test_async_writer_drains_remaining_items_on_stop():
    writer = _AsyncObservationWriter()
    with patch.object(_AsyncObservationWriter, "_write_sync") as mock_write:
        writer.start()
        await writer.enqueue(("ChatAgent", "query_nl", "chat"))
        await writer.stop()  # must drain immediately, not wait for the 1s tick
    mock_write.assert_called()


@pytest.mark.asyncio
async def test_async_handler_enqueues_when_observe_enabled():
    handler = AsyncObservationCallbackHandler(agent_name="ChatAgent", domain="chat")
    with (
        patch("infra_brain.callbacks.observation.get_settings", return_value=_settings(True)),
        patch("infra_brain.callbacks.observation._observation_writer") as mock_writer,
    ):
        mock_writer.enqueue = AsyncMock()
        await handler.on_tool_start({"name": "query_resources"}, "{}", run_id=uuid.uuid4())
    mock_writer.start.assert_called_once()
    mock_writer.enqueue.assert_awaited_once_with(("ChatAgent", "query_resources", "chat"))


@pytest.mark.asyncio
async def test_async_handler_noop_when_infra_ops_observe_disabled():
    handler = AsyncObservationCallbackHandler(agent_name="ChatAgent", domain="chat")
    with (
        patch("infra_brain.callbacks.observation.get_settings", return_value=_settings(False)),
        patch("infra_brain.callbacks.observation._observation_writer") as mock_writer,
    ):
        await handler.on_tool_start({"name": "query_resources"}, "{}", run_id=uuid.uuid4())
    mock_writer.start.assert_not_called()
