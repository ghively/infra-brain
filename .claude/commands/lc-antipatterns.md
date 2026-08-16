---
description: Interactive catalog of the 15 most common LangChain/LangGraph production mistakes. Shows symptom, root cause, why it hurts, and exact before/after fix. Pass a number (1-15) or keyword for direct lookup. Pass "scan <file.py>" to detect antipatterns in a file.
allowed-tools: Read, Glob, Grep
---

You are a senior LangChain/LangGraph engineer presenting an interactive antipattern catalog. Every antipattern entry includes a real symptom, the root cause, the production consequence, and a concrete before/after code fix.

---

## Dispatch Logic

Parse `$ARGUMENTS`:

- **No argument** → print the numbered menu (Step 1), then wait for the user to pick a number or keyword.
- **Number 1–15** → jump directly to that antipattern entry (Step 2).
- **Keyword** (e.g. `memorysaver`, `recursion`, `llmchain`, `pii`) → fuzzy-match to the closest antipattern and display it. If two patterns match equally well, show both titles and ask the user to pick.
- **`scan <file.py>`** → run the file scan (Step 3).
- **`scan`** (no file) → ask: "Which file should I scan?"

---

## Step 1 — Menu (no argument)

Print this exact menu, then wait for input:

```
# LangChain/LangGraph Antipattern Catalog

Pick a number to see the full diagnosis + fix, or type a keyword.
Type `/lc-antipatterns scan <file.py>` to scan a file.

 1. LLMChain in 2024+             — deprecated class, use LCEL pipe instead
 2. AgentExecutor instead of LangGraph — no state, no recovery, no streaming
 3. MemorySaver in production     — all state lost on every restart
 4. Node returns None             — silent crash: InvalidUpdateError
 5. Mutable state in nodes        — shared reference corrupts concurrent runs
 6. Missing recursion_limit       — infinite loops, infinite API spend
 7. Forgetting .compile()         — confusing AttributeError at invoke time
 8. Wrong import paths            — ModuleNotFoundError for classes that exist
 9. Sync tool in async graph      — blocks event loop, kills concurrency
10. ConversationBufferMemory unbounded — OOM or token-limit crash at scale
11. One DB connection per request — connection exhaustion at 5+ users
12. Hardcoded thread_id           — all users share one conversation
13. @tool function called directly — bypasses LangChain tool infrastructure
14. Naive retry without jitter    — thundering herd on rate limits
15. LangSmith always-on with PII  — compliance violation, data leakage

Enter a number (1–15), a keyword, or "all" to page through every entry:
```

If the user types `all`, iterate through all 15 entries in order, printing each full entry and pausing with "Press Enter for next, or type a number to jump:" between entries.

---

## Step 2 — Antipattern Entries

Display the entry in this fixed format:

```
## Antipattern N — <Title>

**Symptom you'll see**
<What the developer observes — error message, wrong behavior, silent failure, or cost spike.>

**Root cause**
<One to two sentences on why this happens technically.>

**Why it hurts in production**
<The real-world consequence: crashes, data loss, cost, security, compliance, performance.>

**Fix**

```python
# BEFORE — <what the broken code looks like>
<before code>

# AFTER — <what the correct code looks like>
<after code>
```

**Related antipatterns:** <list numbers of closely related entries, or "None">
```

After displaying the entry, print:
```
Enter a number (1–15) for another entry, "scan <file.py>" to scan a file, or press Enter to exit:
```
Wait for input. If the user enters a valid number, display that entry. If they press Enter (empty input), stop.

---

### Entry 1 — LLMChain in 2024+

**Symptom you'll see**
`LangChainDeprecationWarning: The class LLMChain was deprecated in LangChain 0.1.17 and will be removed in 1.0.` Your code still runs today but will break on the next major version upgrade. You also miss streaming, async, and batch for free.

**Root cause**
`LLMChain` was a pre-LCEL wrapper that manually orchestrated prompt formatting and LLM invocation. LangChain Expression Language (LCEL) replaced it entirely in 0.1.x. The class is removed in LangChain v1.0.

**Why it hurts in production**
Silent deprecation warnings fill logs. Upgrading to v1.0 breaks all imports with no runtime warning ahead of time. You also lose streaming, `batch()` parallelism, `.with_retry()`, and `.with_fallbacks()` — features that LCEL provides for free.

**Fix**

```python
# BEFORE — LLMChain wraps prompt + LLM manually
from langchain.chains import LLMChain
from langchain_core.prompts import PromptTemplate
from langchain_anthropic import ChatAnthropic

llm = ChatAnthropic(model="claude-sonnet-4-6")
prompt = PromptTemplate.from_template("Summarize: {text}")
chain = LLMChain(llm=llm, prompt=prompt)
result = chain.run(text="LangChain is a framework...")
# result is a dict: {"text": "...", "output": "..."}

# AFTER — LCEL pipe: prompt | llm | parser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_anthropic import ChatAnthropic

llm = ChatAnthropic(model="claude-sonnet-4-6")
prompt = ChatPromptTemplate.from_template("Summarize: {text}")
chain = prompt | llm | StrOutputParser()
result = chain.invoke({"text": "LangChain is a framework..."})
# result is a plain str — no dict unwrapping needed
# streaming, batch, async all work automatically:
# for chunk in chain.stream({"text": "..."}): print(chunk, end="")
```

**Related antipatterns:** 2, 10

---

### Entry 2 — AgentExecutor instead of LangGraph

**Symptom you'll see**
Agent gets stuck in a loop with no way to recover. Tool errors cause the whole run to fail with no retry. You cannot inspect intermediate state, stream token-by-token from tool results, or add a human approval step without rewriting the agent.

**Root cause**
`AgentExecutor` is a monolithic runner with a fixed loop: think → act → observe. It has no checkpointing, no state graph, and no conditional routing. It cannot be extended without subclassing.

**Why it hurts in production**
No recovery from transient tool failures. No per-user conversation isolation. Cannot add human-in-the-loop without major refactor. Debugging requires reading raw verbose logs rather than LangSmith's graph trace. Removed in LangChain v0.3.

**Fix**

```python
# BEFORE — AgentExecutor wraps an agent function
from langchain.agents import AgentExecutor, create_react_agent
from langchain import hub

prompt = hub.pull("hwchase17/react")
agent_fn = create_react_agent(llm, tools, prompt)
executor = AgentExecutor(agent=agent_fn, tools=tools, max_iterations=10, verbose=True)
result = executor.invoke({"input": "What is the weather in Paris?"})

# AFTER — LangGraph create_react_agent (IS the executor)
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

agent = create_react_agent(
    llm,
    tools,
    checkpointer=MemorySaver(),          # swap PostgresSaver in prod
    state_modifier="You are a helpful assistant.",
)
config = {"configurable": {"thread_id": "user-123"}, "recursion_limit": 25}
result = agent.invoke(
    {"messages": [{"role": "user", "content": "What is the weather in Paris?"}]},
    config=config,
)
print(result["messages"][-1].content)
# Add interrupt_before=["tools"] to compile() for human-in-the-loop
```

**Related antipatterns:** 1, 3, 6, 12

---

### Entry 3 — MemorySaver in production

**Symptom you'll see**
Every time the server restarts (deploy, crash, scale-down), all conversation history vanishes. Users restart from scratch with no warning. In multi-process deployments, two workers have separate memory — the same `thread_id` returns different history depending on which worker handles the request.

**Root cause**
`MemorySaver` stores state in a Python dict in the current process's heap. It has no persistence layer. It is intentionally designed for development and testing only.

**Why it hurts in production**
Complete state loss on every restart. No multi-process safety. No recovery from crashes. The `lc:start` journey and `lc-coder` agent both leave a `# swap PostgresSaver in prod` comment — ignoring it is this antipattern.

**Fix**

```python
# BEFORE — MemorySaver in a production API handler
from langgraph.checkpoint.memory import MemorySaver

checkpointer = MemorySaver()          # lost on every restart
agent = create_react_agent(llm, tools, checkpointer=checkpointer)

# AFTER — AsyncPostgresSaver with a connection pool
import os
from contextlib import asynccontextmanager
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

DB_URI = os.environ["DATABASE_URL"]   # e.g. postgresql://user:pw@host/db
pool: AsyncConnectionPool | None = None

@asynccontextmanager
async def lifespan(app):
    global pool
    pool = AsyncConnectionPool(conninfo=DB_URI, max_size=20, open=False)
    await pool.open()
    async with pool.connection() as conn:
        await AsyncPostgresSaver(conn).setup()   # creates tables once
    yield
    await pool.close()

async def get_agent():
    async with pool.connection() as conn:
        checkpointer = AsyncPostgresSaver(conn)
        return create_react_agent(llm, tools, checkpointer=checkpointer)
```

**Related antipatterns:** 11, 12

---

### Entry 4 — Node returns None instead of dict

**Symptom you'll see**
`langgraph.errors.InvalidUpdateError: Expected dict, got None` — raised inside the graph runtime with a confusing traceback that points to LangGraph internals, not your node function.

**Root cause**
Every LangGraph node must return a dict (or `Command`) containing the state keys it wants to update. A function that returns `None` implicitly (no `return` statement, or `return` with no value) causes the runtime to crash when it tries to merge the update into state.

**Why it hurts in production**
Crashes the entire graph run. The error message points to LangGraph internals, making it hard to find the offending node. Particularly common when adding print/debug statements that shadow a return, or when an early `return` path is added without a value.

**Fix**

```python
# BEFORE — node returns None implicitly
def process_document(state: MyState):
    docs = retrieve(state["query"])
    if not docs:
        print("No docs found")
        return            # ← returns None — crashes the graph
    state["context"] = "\n".join(d.page_content for d in docs)
    # ← forgot return at end — also returns None

# AFTER — every code path returns a partial state dict
def process_document(state: MyState) -> dict:
    docs = retrieve(state["query"])
    if not docs:
        return {"context": "", "retrieval_error": "No documents matched the query"}
    return {"context": "\n".join(d.page_content for d in docs)}
# Return only the keys you are updating — LangGraph merges them into state.
# You do NOT need to return a full copy of state.
```

**Related antipatterns:** 5

---

### Entry 5 — Mutable state in nodes

**Symptom you'll see**
State values silently change between nodes in ways that are hard to reproduce. Two concurrent graph runs using the same thread corrupt each other's state. `add_messages` reducer produces duplicate messages. Tests pass individually but fail when run together.

**Root cause**
Python `list` and `dict` objects are passed by reference. If a node appends to `state["messages"]` directly (e.g. `state["messages"].append(msg)`) instead of returning a new value, it mutates the shared state object, bypassing the reducer system entirely.

**Why it hurts in production**
Non-deterministic behavior in concurrent workloads. Reducer invariants broken — `add_messages` deduplication stops working. State snapshots in LangSmith show incorrect intermediate values. Extremely difficult to debug because the mutation happens silently.

**Fix**

```python
# BEFORE — mutating state in place
from langchain_core.messages import AIMessage

def call_llm(state: MessagesState):
    response = llm.invoke(state["messages"])
    state["messages"].append(response)    # ← direct mutation, bypasses reducer
    state["call_count"] += 1              # ← also wrong if using operator.add reducer
    # returns None implicitly (see antipattern 4)

# AFTER — return a partial dict; let the reducer handle merging
def call_llm(state: MessagesState) -> dict:
    response = llm.invoke(state["messages"])
    return {
        "messages": [response],           # add_messages reducer appends this
        "call_count": 1,                  # operator.add reducer adds this to the total
    }
# The reducer declared in the TypedDict controls how values are merged.
# Your node only needs to return what changed.
```

**Related antipatterns:** 4

---

### Entry 6 — Missing recursion_limit

**Symptom you'll see**
`GraphRecursionError: Recursion limit of 25 reached` — but only on certain inputs. For other inputs the graph loops indefinitely, burning API credits until you kill the process. The default limit of 25 is hit unexpectedly on long but valid reasoning chains.

**Root cause**
All `StateGraph` compilations have a default `recursion_limit=25`. If your graph has a cycle (which every ReAct loop does), and the termination condition never triggers (LLM keeps calling tools, routing function always returns a non-END node), the graph loops forever — limited only by the recursion_limit or your wallet.

**Why it hurts in production**
Infinite API spend on stuck inputs. `GraphRecursionError` surfaces as a 500 to users with no graceful degradation. The default of 25 may be too low for multi-hop research agents or too high for simple chatbots — it must be set explicitly to signal intent.

**Fix**

```python
# BEFORE — compile with no recursion_limit (relies on default of 25)
graph = builder.compile(checkpointer=checkpointer)
result = graph.invoke({"messages": [...]})   # default limit, no visibility

# AFTER — set recursion_limit explicitly at compile AND ensure termination
graph = builder.compile(
    checkpointer=checkpointer,
    # recursion_limit is passed at invoke time, not compile time in LangGraph 1.2+
)
config = {
    "configurable": {"thread_id": "user-123"},
    "recursion_limit": 25,                  # explicit — document why this number
}
result = graph.invoke({"messages": [...]}, config=config)

# Also ensure your routing function always has a path to END:
from langgraph.graph import END

def should_continue(state: MessagesState) -> str:
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return END                          # ← must exist or graph loops forever
    return "tools"
```

**Related antipatterns:** 2, 4

---

### Entry 7 — Forgetting .compile()

**Symptom you'll see**
`AttributeError: 'StateGraph' object has no attribute 'invoke'` — or `'stream'`, or `'get_state'`. The error appears far from where the graph was defined, making it hard to trace back to the missing `.compile()`.

**Root cause**
`StateGraph` is a builder object. It accumulates nodes and edges but cannot be executed. `.compile()` validates the graph structure, wires up reducers, and returns a `CompiledGraph` — the only object that supports `invoke()`, `stream()`, and `astream()`.

**Why it hurts in production**
Immediate crash on first invocation. The error message (`'StateGraph' object has no attribute 'invoke'`) does not mention `.compile()`, so developers spend time searching for a wrong method name instead of the missing call. Particularly common when a graph is defined in one module and imported into another.

**Fix**

```python
# BEFORE — builder returned directly, not compiled
from langgraph.graph import StateGraph, START, END, MessagesState

def build_graph(checkpointer):
    builder = StateGraph(MessagesState)
    builder.add_node("chat", call_llm)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    return builder                          # ← returns StateGraph, not CompiledGraph

app = build_graph(MemorySaver())
app.invoke({"messages": [...]})            # ← AttributeError here

# AFTER — always return builder.compile()
def build_graph(checkpointer):
    builder = StateGraph(MessagesState)
    builder.add_node("chat", call_llm)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    return builder.compile(checkpointer=checkpointer)   # ← returns CompiledGraph

app = build_graph(MemorySaver())
app.invoke({"messages": [...]})            # ← works
```

**Related antipatterns:** 6

---

### Entry 8 — Wrong import paths

**Symptom you'll see**
`ModuleNotFoundError: No module named 'langchain.chat_models'` — or `ImportError: cannot import name 'ChatAnthropic' from 'langchain'`. The class exists and is installed, but the import path moved between packages or versions.

**Root cause**
LangChain split its monolithic package into `langchain-core` (stable interfaces), `langchain` (orchestration), `langchain-community` (third-party integrations), and provider packages like `langchain-anthropic`. Imports from the old monolithic `langchain.*` paths were removed in v0.3.

**Why it hurts in production**
Hard import failure on startup — the entire application refuses to start. Errors are confusing because the package is installed but the path is wrong. Community package docs and LLM training data often cite the old paths.

**Fix**

```python
# BEFORE — old monolithic paths (fail in v0.3+)
from langchain.chat_models import ChatAnthropic         # removed
from langchain.embeddings import OpenAIEmbeddings        # removed
from langchain.vectorstores import Chroma                # removed
from langchain.schema import HumanMessage                # removed

# AFTER — correct package paths for LangChain 0.3.x / LangGraph 1.2.x
from langchain_anthropic import ChatAnthropic            # pip install langchain-anthropic
from langchain_openai import OpenAIEmbeddings            # pip install langchain-openai
from langchain_chroma import Chroma                      # pip install langchain-chroma
from langchain_core.messages import HumanMessage         # in langchain-core (always installed)

# Quick reference — where things live now:
# langchain_core   → messages, prompts, runnables, output_parsers, tools (base classes)
# langchain        → chains (LCEL patterns), agents (prebuilt), retrievers
# langchain_community → third-party loaders, vectorstores, tools, LLMs
# langchain_anthropic → ChatAnthropic
# langchain_openai    → ChatOpenAI, OpenAIEmbeddings
# langgraph           → StateGraph, create_react_agent, MemorySaver, ToolNode
```

**Related antipatterns:** 1, 2

---

### Entry 9 — Sync tool in async graph

**Symptom you'll see**
Your async LangGraph application handles only one request at a time even under load. Latency spikes to N × tool_latency instead of max(tool_latencies). With `uvicorn` or `FastAPI`, `asyncio` warns: `Blocking call in async context`. In extreme cases, the event loop hangs and health checks time out.

**Root cause**
If a `@tool` function uses `requests`, `psycopg2`, `sqlite3`, or any other synchronous I/O library and is called from an `async` node, it blocks the single thread running the event loop. All other concurrent requests are frozen until the blocking call returns.

**Why it hurts in production**
Single-threaded throughput even on multi-core servers. Under load, one slow database query or API call freezes every user. The failure mode is silent — no error is raised, just degraded concurrency.

**Fix**

```python
# BEFORE — sync requests call inside a tool used by an async graph
import requests
from langchain_core.tools import tool

@tool
def fetch_weather(city: str) -> str:
    """Get current weather for a city."""
    resp = requests.get(f"https://api.weather.example.com/{city}")  # blocks event loop
    return resp.json()["summary"]

# AFTER — Option A: async tool with httpx
import httpx
from langchain_core.tools import tool

@tool
async def fetch_weather(city: str) -> str:
    """Get current weather for a city."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://api.weather.example.com/{city}")
        return resp.json()["summary"]

# AFTER — Option B: wrap a sync library you cannot replace
import asyncio
import requests
from langchain_core.tools import tool
from functools import partial

@tool
async def fetch_weather(city: str) -> str:
    """Get current weather for a city."""
    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(
        None,                                  # uses default ThreadPoolExecutor
        partial(requests.get, f"https://api.weather.example.com/{city}")
    )
    return resp.json()["summary"]
```

**Related antipatterns:** 14

---

### Entry 10 — ConversationBufferMemory growing unbounded

**Symptom you'll see**
After many turns, LLM calls start failing with `ContextWindowExceededError` or `max_tokens` errors. Token cost per request grows linearly with conversation length. In long-running sessions, memory consumption climbs until the process OOMs.

**Root cause**
`ConversationBufferMemory` (legacy) and naive `MessagesState` both append every message indefinitely. There is no built-in trimming. A conversation that started at 500 tokens can reach 128k tokens after dozens of turns — hitting the model's context limit and costing orders of magnitude more per call.

**Why it hurts in production**
Unbounded token cost. Hard `ContextWindowExceededError` at unpredictable conversation lengths. OOM risk in long-running processes. `ConversationBufferMemory` is also a deprecated class (see antipattern 1).

**Fix**

```python
# BEFORE — unbounded message accumulation in MessagesState
def call_llm(state: MessagesState) -> dict:
    response = llm.invoke(state["messages"])   # sends ALL messages every time
    return {"messages": [response]}

# AFTER — Option A: trim_messages (hard token cap, preserves recent context)
from langchain_core.messages import trim_messages, SystemMessage

def call_llm(state: MessagesState) -> dict:
    trimmed = trim_messages(
        state["messages"],
        strategy="last",
        max_tokens=4096,                    # tune to your model's context window
        token_counter=llm,                  # uses the model's tokenizer
        include_system=True,                # always keep the system message
        allow_partial=False,
    )
    response = llm.invoke(trimmed)
    return {"messages": [response]}

# AFTER — Option B: summarize when > N messages (preserves semantic content)
# See /lc-antipatterns 3B in lc-upgrade for the full summarize_conversation node.
# Key: add a `summary` field to state + a summarize node triggered by message count.
```

**Related antipatterns:** 1, 3

---

### Entry 11 — One DB connection per request to PostgresSaver

**Symptom you'll see**
`psycopg2.OperationalError: connection pool exhausted` or `too many connections` from PostgreSQL. Application works fine with 1-4 users, then starts returning 500 errors at 5+ concurrent users. Database CPU spikes on connection setup/teardown.

**Root cause**
Creating a new `psycopg` / `psycopg2` connection per request, or per graph invocation, exhausts PostgreSQL's `max_connections` limit (default: 100). Each connection costs ~5MB of server RAM and 10-50ms to establish. At 10 req/s with 500ms latency, you need 5 connections minimum — but 50 concurrent users need 50 connections, each held for the full request lifetime.

**Why it hurts in production**
Hard failures at moderate load. High connection setup latency on every request. PostgreSQL server resource exhaustion. The fix is a connection pool created once at application startup.

**Fix**

```python
# BEFORE — new connection per invocation
from psycopg import AsyncConnection
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

async def handle_request(user_id: str, message: str):
    conn = await AsyncConnection.connect(os.environ["DATABASE_URL"])   # new conn each call
    checkpointer = AsyncPostgresSaver(conn)
    agent = create_react_agent(llm, tools, checkpointer=checkpointer)
    result = await agent.ainvoke(...)
    await conn.close()
    return result

# AFTER — shared pool, created once at startup
import os
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

DB_URI = os.environ["DATABASE_URL"]
pool = AsyncConnectionPool(conninfo=DB_URI, max_size=20, open=False)

async def startup():
    await pool.open()
    async with pool.connection() as conn:
        await AsyncPostgresSaver(conn).setup()   # create tables once

async def handle_request(user_id: str, message: str):
    async with pool.connection() as conn:        # borrowed from pool, not new
        checkpointer = AsyncPostgresSaver(conn)
        agent = create_react_agent(llm, tools, checkpointer=checkpointer)
        config = {"configurable": {"thread_id": user_id}, "recursion_limit": 25}
        return await agent.ainvoke({"messages": [{"role": "user", "content": message}]}, config)
```

**Related antipatterns:** 3, 12

---

### Entry 12 — Hardcoded thread_id

**Symptom you'll see**
All users see each other's conversation history. Asking "What is my name?" returns the previous user's name. Conversations bleed across sessions. In single-user apps, all sessions are merged into one infinite conversation.

**Root cause**
`thread_id` in the LangGraph config dict scopes all checkpoint reads and writes. Using a literal string (e.g. `"thread_id": "main"` or `"thread_id": "1"`) means every invocation, from every user, reads and writes the same checkpoint slot.

**Why it hurts in production**
Complete loss of conversation isolation. Privacy violation — users see other users' messages. History grows unboundedly as all users share one thread (see antipattern 10). Extremely common in tutorials that hard-code `thread_id: "1"` for simplicity.

**Fix**

```python
# BEFORE — hardcoded thread_id shared by all users
config = {"configurable": {"thread_id": "main"}}   # every user shares this

async def chat(user_message: str):
    return await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config=config,       # ← same config for all callers
    )

# AFTER — thread_id derived from authenticated user + session
import uuid

def make_config(user_id: str, session_id: str | None = None) -> dict:
    """
    user_id   — from auth layer (JWT sub, database PK, etc.)
    session_id — None means "continue latest session for this user"
                 A new UUID means "start a new conversation"
    """
    thread = f"{user_id}:{session_id or 'default'}"
    return {
        "configurable": {"thread_id": thread},
        "recursion_limit": 25,
    }

async def chat(user_id: str, user_message: str, session_id: str | None = None):
    config = make_config(user_id, session_id)
    return await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config=config,
    )

# To start a new session: pass session_id=str(uuid.uuid4())
# To continue last session: pass session_id=None (uses 'default')
```

**Related antipatterns:** 3, 11

---

### Entry 13 — @tool function called directly

**Symptom you'll see**
Tool executes but returns the raw Python return value instead of a `ToolMessage`. LangSmith shows no tool span. Error handling in `ToolNode` does not trigger. The LLM never sees the tool result in message history. Retry logic is bypassed.

**Root cause**
`@tool` decorates a function with a `BaseTool` wrapper that handles schema validation, error wrapping (`ToolException` → `ToolMessage`), tracing, and message formatting. Calling `my_tool("arg")` as a plain function invokes the underlying function directly, bypassing all of that infrastructure.

**Why it hurts in production**
Tool errors are not caught by `handle_tool_errors=True` on `ToolNode` — they raise as bare Python exceptions and crash the node. LangSmith has no tool span. The LLM cannot see tool output because it is not wrapped in a `ToolMessage`. Input schema validation is skipped.

**Fix**

```python
# BEFORE — calling @tool function directly like a plain function
from langchain_core.tools import tool

@tool
def search_web(query: str) -> str:
    """Search the web for current information."""
    return tavily_client.search(query)["results"][0]["content"]

# Wrong — bypasses all tool infrastructure
result = search_web("LangGraph tutorial")          # plain Python call
print(result)                                       # raw str, no ToolMessage

# AFTER — Option A: let the agent/ToolNode call the tool (correct for graphs)
# Just bind the tool to the LLM and add ToolNode — never call the tool directly.
from langgraph.prebuilt import create_react_agent
agent = create_react_agent(llm, [search_web])      # agent decides when to call it

# AFTER — Option B: call via .invoke() when you need the result outside a graph
result = search_web.invoke({"query": "LangGraph tutorial"})   # returns ToolMessage
print(result)   # ToolMessage with content, tool_call_id, schema-validated input

# AFTER — Option C: test the underlying function directly (valid in unit tests)
result = search_web.func("LangGraph tutorial")     # bypasses tool infra intentionally
# Use only in tests where you want to test the function logic, not the tool wrapper.
```

**Related antipatterns:** 4, 9

---

### Entry 14 — Naive retry without jitter

**Symptom you'll see**
After hitting a `RateLimitError`, all retrying clients fire again at the same instant. The API returns another `RateLimitError`. All clients retry again simultaneously. The pattern repeats, and your rate limit window never clears. Latency spikes to minutes. Other users' requests are frozen.

**Root cause**
Naive retry (`time.sleep(2)`, `@retry(wait=wait_fixed(2))`) uses a fixed backoff. When multiple requests hit a rate limit simultaneously, they all wait the same amount of time and then all hammer the API again at the same instant — a thundering herd.

**Why it hurts in production**
Self-reinforcing rate limit storm: the more clients retry, the more they collide, the more they get rate-limited. Throughput drops to near zero under load. Adding more retry attempts makes the storm worse, not better. The fix requires exponential backoff with randomized jitter.

**Fix**

```python
# BEFORE — fixed-interval retry on LLM calls
import time
from langchain_anthropic import ChatAnthropic

llm = ChatAnthropic(model="claude-sonnet-4-6")

for attempt in range(3):
    try:
        result = llm.invoke(messages)
        break
    except Exception:
        time.sleep(2)       # all callers sleep the same duration → thundering herd

# AFTER — Option A: LCEL .with_retry() with tenacity (recommended)
from langchain_anthropic import ChatAnthropic
from tenacity import stop_after_attempt, wait_exponential_jitter

llm = ChatAnthropic(model="claude-sonnet-4-6")
llm_with_retry = llm.with_retry(
    retry_if_exception_type=(Exception,),
    stop=stop_after_attempt(4),
    wait=wait_exponential_jitter(initial=1, max=60),   # 1s, 2s+jitter, 4s+jitter…
)
result = llm_with_retry.invoke(messages)

# AFTER — Option B: .with_fallbacks() for model fallback after exhausted retries
from langchain_anthropic import ChatAnthropic

primary = ChatAnthropic(model="claude-sonnet-4-6").with_retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=30),
)
fallback = ChatAnthropic(model="claude-haiku-4-5")     # cheaper fallback
llm = primary.with_fallbacks([fallback])
result = llm.invoke(messages)   # tries primary 3x, falls back to haiku on failure
```

**Related antipatterns:** 9

---

### Entry 15 — LangSmith always-on with PII in state

**Symptom you'll see**
LangSmith traces contain full names, email addresses, phone numbers, SSNs, or medical information that users entered in chat. If you operate under GDPR, HIPAA, or CCPA, this data is now in a third-party system you do not control — a reportable data incident. The LangSmith free tier has no data retention controls.

**Root cause**
`LANGSMITH_TRACING=true` sends every LLM input and output, every tool call argument and result, and every state snapshot to `api.smith.langchain.com`. If your state contains PII (user messages, form data, documents), all of it is transmitted automatically.

**Why it hurts in production**
Regulatory violation. GDPR Article 28 (data processor agreements), HIPAA Business Associate Agreement, CCPA data residency requirements — all may be triggered. Data subject access requests become impossible to fulfill. Security audit findings. Potential fines.

**Fix**

```python
# BEFORE — tracing always on, PII flows to LangSmith
# .env
# LANGSMITH_TRACING=true        ← sends all inputs/outputs including PII
# LANGSMITH_API_KEY=ls__...

# AFTER — Option A: environment-gated tracing (trace only in non-prod)
import os

# .env.development
# LANGSMITH_TRACING=true

# .env.production
# LANGSMITH_TRACING=false        ← tracing disabled in prod

# AFTER — Option B: redact PII before LLM call using a filter node
import re
from langchain_core.messages import HumanMessage

PII_PATTERNS = [
    (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]"),          # SSN
    (r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", "[EMAIL]"), # email
    (r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b", "[PHONE]"),# phone
]

def redact_pii(text: str) -> str:
    for pattern, replacement in PII_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text

def redact_node(state: MessagesState) -> dict:
    """Redact PII from the last human message before it reaches the LLM."""
    last = state["messages"][-1]
    if isinstance(last, HumanMessage):
        return {"messages": [HumanMessage(content=redact_pii(last.content), id=last.id)]}
    return {}

# AFTER — Option C: self-hosted LangSmith (data never leaves your VPC)
# See: https://docs.smith.langchain.com/self_hosting
# LANGSMITH_ENDPOINT=https://your-internal-langsmith.company.com
```

**Related antipatterns:** 3, 12

---

## Step 3 — File Scan (`scan <file.py>`)

Read the target file completely. Then check for each of the 15 antipatterns using the detection signals below. Report every match found.

### Detection Signals

| # | Antipattern | Grep for |
|---|---|---|
| 1 | LLMChain | `LLMChain`, `from langchain.chains import LLMChain` |
| 2 | AgentExecutor | `AgentExecutor`, `from langchain.agents import AgentExecutor` |
| 3 | MemorySaver in prod | `MemorySaver()` outside a test file or `if __name__ == "__main__"` block |
| 4 | Node returns None | function annotated as a graph node with no `return` or `return` with no value |
| 5 | Mutable state | `state["messages"].append`, `state[`, `].append`, direct index assignment on state |
| 6 | Missing recursion_limit | `.compile(` without `recursion_limit` nearby, `graph.invoke(` without `recursion_limit` in config |
| 7 | Missing .compile() | `StateGraph(` assigned to a variable that is later `.invoke(`d without `.compile()` in between |
| 8 | Wrong import paths | `from langchain.chat_models`, `from langchain.embeddings`, `from langchain.vectorstores`, `from langchain.schema` |
| 9 | Sync tool in async graph | `@tool` + `def ` (not `async def`) + `requests.get`, `psycopg2`, `sqlite3` |
| 10 | Unbounded memory | `add_messages` reducer present but no `trim_messages` or summary node in the file |
| 11 | Connection per request | `AsyncConnection.connect(` or `psycopg2.connect(` inside an `async def` handler (not startup) |
| 12 | Hardcoded thread_id | `"thread_id": "` followed by a string literal (not a variable) |
| 13 | @tool called directly | `@tool`-decorated function name called as `function_name(` without `.invoke(` |
| 14 | Naive retry | `time.sleep(` inside an except block, `wait=wait_fixed(` in tenacity |
| 15 | PII in tracing | `LANGSMITH_TRACING=true` in the file AND any of: `email`, `ssn`, `phone`, `address`, `medical` in state field names |

### Scan Output Format

For every match found, output a finding block:

```
### Antipattern N — <Title>
**Location:** `<filename>:<line_number>`
**Detected:** <verbatim matched line>
**Risk:** <one sentence on the production consequence>
**Fix:** See `/lc-antipatterns N` for the full before/after.
```

After all findings, output a summary:

```
## Scan Summary — <filename>

| Antipattern | Found | Severity |
|---|---|---|
| 1 — LLMChain | yes/no | BREAKING/HIGH/MEDIUM/LOW |
...

**Total issues found:** N
**Recommended action:** [list the top 3 by severity]
```

Severity for each antipattern in a scan context:
- BREAKING (1, 2, 8): will fail to import or run in LangChain v0.3+
- HIGH (3, 4, 6, 7, 12, 15): crashes, data loss, or compliance risk in production
- MEDIUM (5, 9, 10, 11, 13, 14): degrades reliability, concurrency, or cost at scale
- LOW: style or minor best practice issue

If no antipatterns are detected, output:
```
No antipatterns detected in <filename>. The file uses current LangChain/LangGraph patterns.
```

---

## Output Rules

- Display the full entry format for every requested antipattern. Do not summarize or abbreviate.
- Copy the BEFORE code exactly as shown — do not paraphrase.
- In scan mode: report only findings with a confirmed line number. Do not guess.
- Never invent findings that are not present in the file.
- The command is read-only: it never writes, edits, or modifies files.
- After displaying any entry, always prompt for the next action (number, keyword, scan, or exit).
