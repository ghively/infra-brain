"""Tests for the shared checkpointer factory (Wave 4 item 4.2).

Includes the critical-bug regression coverage added when API review found that
chat/agent.py compiled its graph with the SYNC PostgresSaver but drove it via
astream_events()/ainvoke() (async): sync PostgresSaver does not implement
aget_tuple/aput/alist and raises NotImplementedError on first real use. The
fix splits the factory into get_checkpointer() (sync, for llm_base.py's
.invoke() loop) and get_async_checkpointer() (async, for the chat graph).
"""

import asyncio
import sys
import types


def test_get_checkpointer_uses_pooled_connection_pool(monkeypatch):
    """B4: get_checkpointer() must construct a psycopg_pool.ConnectionPool
    (not a single psycopg.Connection) and hand it directly to PostgresSaver —
    no more hand-rolled _LockingPostgresSaver RLock wrapper (a pool removes
    the single-connection contention that wrapper existed to serialize).
    The pool's connection kwargs must carry autocommit=True/row_factory=
    dict_row (PostgresSaver breaks without them), and no `pipe=` may be
    passed alongside the pool.
    """
    import infra_brain.checkpointer as cp
    from infra_brain.config import get_settings

    pool_calls = []

    class FakePostgresSaver:
        """Stand-in for langgraph.checkpoint.postgres.PostgresSaver."""

        def __init__(self, conn):
            self.conn = conn

        def setup(self):
            pass

    class FakeConnectionPool:
        def __init__(self, conninfo, **kwargs):
            pool_calls.append((conninfo, kwargs))
            self.conninfo = conninfo
            self.kwargs = kwargs

    fake_postgres_pkg = types.ModuleType("langgraph.checkpoint.postgres")
    fake_postgres_pkg.PostgresSaver = FakePostgresSaver

    fake_psycopg_rows_mod = types.ModuleType("psycopg.rows")
    fake_psycopg_rows_mod.dict_row = object()
    fake_psycopg_mod = types.ModuleType("psycopg")
    fake_psycopg_mod.rows = fake_psycopg_rows_mod

    fake_psycopg_pool_mod = types.ModuleType("psycopg_pool")
    fake_psycopg_pool_mod.ConnectionPool = FakeConnectionPool

    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", fake_postgres_pkg)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg_mod)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_psycopg_rows_mod)
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_psycopg_pool_mod)

    settings = get_settings()
    monkeypatch.setattr(settings, "postgres_url", "postgresql://user:pass@localhost/db")
    monkeypatch.setattr(cp, "_checkpointer", None)

    saver = cp.get_checkpointer()

    assert isinstance(saver, FakePostgresSaver)
    assert type(saver).__name__ == "FakePostgresSaver", (
        "get_checkpointer() must return the plain saver wrapping a pool — no "
        f"more _LockingPostgresSaver wrapper class, got {type(saver).__name__!r}"
    )
    assert isinstance(saver.conn, FakeConnectionPool), (
        "PostgresSaver must be constructed with a ConnectionPool, not a bare Connection"
    )
    assert pool_calls, "ConnectionPool was never constructed"
    _, kwargs = pool_calls[0]
    assert "pipe" not in kwargs, "must not pass pipe= alongside a pool"
    conn_kwargs = kwargs.get("kwargs") or {}
    assert conn_kwargs.get("autocommit") is True
    assert conn_kwargs.get("row_factory") is fake_psycopg_rows_mod.dict_row
    monkeypatch.setattr(cp, "_checkpointer", None)


def test_get_checkpointer_memory_fallback_on_sqlite(monkeypatch):
    from langgraph.checkpoint.memory import MemorySaver

    import infra_brain.checkpointer as cp

    monkeypatch.setattr(cp, "_checkpointer", None)
    saver = cp.get_checkpointer()
    # The suite runs with POSTGRES_URL=sqlite:///:memory: (conftest) — must
    # degrade to MemorySaver, loudly, instead of crashing.
    assert isinstance(saver, MemorySaver)
    monkeypatch.setattr(cp, "_checkpointer", None)


def test_get_checkpointer_is_singleton(monkeypatch):
    import infra_brain.checkpointer as cp

    monkeypatch.setattr(cp, "_checkpointer", None)
    a = cp.get_checkpointer()
    b = cp.get_checkpointer()
    assert a is b
    monkeypatch.setattr(cp, "_checkpointer", None)


def test_get_checkpointer_falls_back_when_postgres_saver_import_fails(monkeypatch):
    """Import-guard: if langgraph-checkpoint-postgres is not installed (or its
    import otherwise fails), get_checkpointer() must degrade to MemorySaver
    instead of raising."""
    from langgraph.checkpoint.memory import MemorySaver

    import infra_brain.checkpointer as cp
    from infra_brain.config import get_settings

    monkeypatch.setattr(cp, "_checkpointer", None)
    settings = get_settings()
    monkeypatch.setattr(settings, "postgres_url", "postgresql://user:pass@localhost/db")
    # Force the import failure regardless of what's installed in this
    # environment: a None entry in sys.modules makes the import raise
    # ImportError. Without this, an environment WITH the package installed
    # (e.g. the CI runner) takes the real ConnectionPool path and tries to
    # connect to a Postgres that doesn't exist.
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", None)
    saver = cp.get_checkpointer()
    assert isinstance(saver, MemorySaver)
    monkeypatch.setattr(cp, "_checkpointer", None)


def test_get_async_checkpointer_memory_fallback_on_sqlite(monkeypatch):
    """Async twin of the sync fallback test: no Postgres DSN -> MemorySaver,
    which is safe because MemorySaver implements the async checkpoint API too."""
    from langgraph.checkpoint.memory import MemorySaver

    import infra_brain.checkpointer as cp

    monkeypatch.setattr(cp, "_async_checkpointer", None)
    saver = asyncio.run(cp.get_async_checkpointer())
    assert isinstance(saver, MemorySaver)
    monkeypatch.setattr(cp, "_async_checkpointer", None)


def test_get_async_checkpointer_falls_back_when_postgres_saver_import_fails(monkeypatch):
    """Import-guard on the async path: if langgraph-checkpoint-postgres (or its
    .aio submodule) is not installed, get_async_checkpointer() must degrade to
    MemorySaver instead of raising."""
    from langgraph.checkpoint.memory import MemorySaver

    import infra_brain.checkpointer as cp
    from infra_brain.config import get_settings

    monkeypatch.setattr(cp, "_async_checkpointer", None)
    settings = get_settings()
    monkeypatch.setattr(settings, "postgres_url", "postgresql://user:pass@localhost/db")
    # Force the import failure regardless of what's installed (see the sync
    # twin above). This test hung the CI test job for its full 1h timeout on
    # 2026-07-14 (pipeline 1214): with langgraph-checkpoint-postgres installed
    # on the runner, it opened a real AsyncConnectionPool against a
    # nonexistent localhost Postgres.
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", None)
    saver = asyncio.run(cp.get_async_checkpointer())
    assert isinstance(saver, MemorySaver)
    monkeypatch.setattr(cp, "_async_checkpointer", None)


def test_async_checkpointer_drives_compiled_graph_without_notimplementederror(monkeypatch):
    """CRITICAL regression (API review finding): compile a real LangGraph graph
    with the checkpointer returned for the chat path and drive it via
    ainvoke()/astream_events() across two turns on the same thread. Locally this
    exercises the MemorySaver fallback -- which DOES implement aget_tuple/aput/
    alist -- proving the async contract holds for whatever get_async_checkpointer()
    returns. The bug this guards against: a SYNC PostgresSaver plugged into an
    async-driven graph raises NotImplementedError from BaseCheckpointSaver's
    async stubs on the very first checkpoint read/write.
    """
    from langgraph.graph import END, START, MessagesState, StateGraph

    import infra_brain.checkpointer as cp

    monkeypatch.setattr(cp, "_async_checkpointer", None)

    async def run():
        checkpointer = await cp.get_async_checkpointer()

        async def echo_node(state):
            return {"messages": [{"role": "ai", "content": "ok"}]}

        graph = (
            StateGraph(MessagesState)
            .add_node("echo", echo_node)
            .add_edge(START, "echo")
            .add_edge("echo", END)
            .compile(checkpointer=checkpointer)
        )

        config = {"configurable": {"thread_id": "regression-thread"}}
        # First turn: exercises aput/aput_writes (checkpoint write path).
        result1 = await graph.ainvoke(
            {"messages": [{"role": "human", "content": "hi"}]}, config=config
        )
        # Second turn on the SAME thread: exercises aget_tuple (checkpoint read
        # path) to resume from the prior checkpoint -- this is exactly the call
        # that raised NotImplementedError against a sync PostgresSaver.
        result2 = await graph.ainvoke(
            {"messages": [{"role": "human", "content": "again"}]}, config=config
        )

        events_seen = False
        async for event in graph.astream_events(
            {"messages": [{"role": "human", "content": "stream"}]}, config=config, version="v2"
        ):
            events_seen = True
        return result1, result2, events_seen

    result1, result2, events_seen = asyncio.run(run())
    assert result1["messages"][-1].content == "ok"
    assert result2["messages"][-1].content == "ok"
    assert events_seen
    monkeypatch.setattr(cp, "_async_checkpointer", None)


def test_chat_checkpointer_selects_async_saver_when_postgres_configured(monkeypatch):
    """CRITICAL regression (API review finding): when a Postgres DSN is
    configured AND the async saver is importable, the CHAT factory must select
    AsyncPostgresSaver -- never the sync PostgresSaver, which lacks async
    checkpoint methods. Simulates the package being installed by injecting fake
    langgraph.checkpoint.postgres(.aio) / psycopg modules into sys.modules,
    since the real package is not installed in this offline dev environment.
    """
    import infra_brain.checkpointer as cp
    from infra_brain.config import get_settings

    setup_calls = []

    class FakeAsyncPostgresSaver:
        def __init__(self, conn):
            self.conn = conn

        async def setup(self):
            setup_calls.append(self.conn)

    class FakeAsyncConnectionPool:
        def __init__(self, conninfo, **kwargs):
            self.conninfo = conninfo
            self.kwargs = kwargs
            self.opened = False

        async def open(self):
            self.opened = True

    fake_postgres_pkg = types.ModuleType("langgraph.checkpoint.postgres")
    fake_aio_mod = types.ModuleType("langgraph.checkpoint.postgres.aio")
    fake_aio_mod.AsyncPostgresSaver = FakeAsyncPostgresSaver
    fake_postgres_pkg.aio = fake_aio_mod

    fake_psycopg_rows_mod = types.ModuleType("psycopg.rows")
    fake_psycopg_rows_mod.dict_row = object()
    fake_psycopg_mod = types.ModuleType("psycopg")
    fake_psycopg_mod.rows = fake_psycopg_rows_mod

    fake_psycopg_pool_mod = types.ModuleType("psycopg_pool")
    fake_psycopg_pool_mod.AsyncConnectionPool = FakeAsyncConnectionPool

    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", fake_postgres_pkg)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", fake_aio_mod)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg_mod)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_psycopg_rows_mod)
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_psycopg_pool_mod)

    settings = get_settings()
    monkeypatch.setattr(settings, "postgres_url", "postgresql://user:pass@localhost/db")
    monkeypatch.setattr(cp, "_async_checkpointer", None)

    saver = asyncio.run(cp.get_async_checkpointer())

    assert isinstance(saver, FakeAsyncPostgresSaver), (
        f"chat path must select the ASYNC saver when Postgres is configured and "
        f"importable, got {type(saver)!r}"
    )
    assert isinstance(saver.conn, FakeAsyncConnectionPool), (
        "AsyncPostgresSaver must be constructed with an AsyncConnectionPool"
    )
    assert saver.conn.opened, "the pool must be opened (awaited) before use"
    assert setup_calls, "AsyncPostgresSaver.setup() must be awaited during init"
    monkeypatch.setattr(cp, "_async_checkpointer", None)


def test_checkpoint_persistence_round_trip(monkeypatch):
    """Verify checkpoint persistence: write a checkpoint via the graph's ainvoke
    path, then read it back directly from the checkpointer to confirm that state
    actually persists within a session (wave 4 item 4.2 acceptance check).

    This exercises MemorySaver (the in-test fallback) but validates the same
    aput->aget_tuple contract that AsyncPostgresSaver satisfies in production
    -- the interface is identical.
    """
    from langgraph.graph import END, START, MessagesState, StateGraph

    import infra_brain.checkpointer as cp

    monkeypatch.setattr(cp, "_async_checkpointer", None)

    async def run():
        checkpointer = await cp.get_async_checkpointer()

        async def echo_node(state):
            return {"messages": [{"role": "ai", "content": "pong"}]}

        graph = (
            StateGraph(MessagesState)
            .add_node("echo", echo_node)
            .add_edge(START, "echo")
            .add_edge("echo", END)
            .compile(checkpointer=checkpointer)
        )

        thread_id = "roundtrip-thread-001"
        config = {"configurable": {"thread_id": thread_id}}

        # First turn -- writes a checkpoint.
        await graph.ainvoke({"messages": [{"role": "human", "content": "ping"}]}, config=config)

        # Read the checkpoint back directly from the saver (not via the graph).
        # aget_tuple returns the most recent CheckpointTuple for the thread.
        ckpt_tuple = await checkpointer.aget_tuple(config)

        assert ckpt_tuple is not None, (
            "Checkpoint not found after ainvoke -- persistence round-trip failed. "
            "aget_tuple returned None, meaning no checkpoint was written."
        )
        # The checkpoint should carry the AI reply that echo_node produced.
        messages = ckpt_tuple.checkpoint.get("channel_values", {}).get("messages", [])
        assert any(getattr(m, "content", None) == "pong" for m in messages), (
            f"Expected 'pong' in persisted messages; got: {messages!r}"
        )

        # Second turn -- aget_tuple must resume from the persisted state, not start fresh.
        result2 = await graph.ainvoke(
            {"messages": [{"role": "human", "content": "again"}]}, config=config
        )
        # The thread now has more than one human turn -- proof resume worked.
        all_human = [m for m in result2["messages"] if getattr(m, "type", None) == "human"]
        assert len(all_human) >= 2, (
            "Second turn should see both human messages if checkpointer replays state; "
            f"found: {[m.content for m in all_human]!r}"
        )

        return ckpt_tuple

    ckpt_tuple = asyncio.run(run())
    assert ckpt_tuple is not None
    monkeypatch.setattr(cp, "_async_checkpointer", None)


def test_lifespan_source_contains_eager_checkpointer_call():
    """Structural guard: confirm that main.py's lifespan body calls
    get_async_checkpointer() so the intent survives future refactors.
    Uses a source-level check because the full FastAPI app cannot be imported
    in the test suite without a live governance_audit router (pre-existing
    import gap); the test is intentionally lightweight and reads the raw source.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).parent.parent / "src" / "infra_brain" / "main.py").read_text(
        encoding="utf-8"
    )

    # Must import get_async_checkpointer at the module level.
    assert "get_async_checkpointer" in src, (
        "main.py does not reference get_async_checkpointer at all -- eager startup init is missing"
    )

    # The call must appear inside the lifespan coroutine body (not just imported).
    # Parse the AST and find the lifespan function; check for an await expression
    # that calls get_async_checkpointer.
    tree = ast.parse(src)
    lifespan_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            lifespan_fn = node
            break

    assert lifespan_fn is not None, "lifespan async function not found in main.py"

    call_found = False
    for node in ast.walk(lifespan_fn):
        if (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "get_async_checkpointer"
        ):
            call_found = True
            break

    assert call_found, (
        "lifespan() in main.py does not await get_async_checkpointer() -- "
        "eager startup init of the chat checkpointer is missing"
    )
