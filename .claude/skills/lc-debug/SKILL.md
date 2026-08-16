---
name: lc-debug
description: Use when a LangChain or LangGraph application throws an error, produces wrong output, loops infinitely, loses state, or behaves unexpectedly. Use when debugging import failures, LLM call errors, output parsing failures, tool/agent errors, LangGraph state violations, memory/checkpoint problems, or async context errors. Use when the user pastes a Python traceback from a LangChain/LangGraph app. Covers LangGraph Studio visual debugging, step-through execution, state injection, interrupt_before breakpoints, and time-travel debugging via get_state_history and update_state.
---

# lc:debug — LangChain & LangGraph Systematic Debugging

## Overview

Most LangChain/LangGraph bugs fall into seven categories. The fastest path to a fix is:
**open Studio → step through nodes → read the error → match the category → check the trace → apply the targeted fix → verify.**

Do not guess. Do not change things randomly. Start with Studio (Phase 0.5) — most bugs are visible before any code changes. Then match the error to the table below and follow the fix pattern.

---

## Debugging Flow

```
0.5 Open LangGraph Studio: langgraph dev → localhost:2024 → step through nodes visually
    (skip to code steps below only if Studio is unavailable or the bug requires it)
1.  Read the full traceback (the LAST line is the error type; the middle is where it broke)
2.  Match error type → Category below
3.  Open LangSmith trace OR enable verbose logging
4.  Isolate the failing component
5.  Apply targeted fix from the category's fix table
6.  Run the minimal repro to verify
    If the run already completed and state is wrong → use Phase 8 Time-Travel Debugging
```

---

## Phase 0.5 — Visual Debugging with LangGraph Studio (Try This First)

**Before touching any code**, open Studio. Most bugs are immediately visible as a highlighted failing node, a wrong state value in the inspector, or a missing edge — no print statements or log parsing required.

### Opening Studio

Requirements: `pip install "langgraph[cli]"` and a `langgraph.json` config file at the project root.

```bash
# Start the dev server — serves your graph at localhost:2024
langgraph dev

# With a custom port or config path:
langgraph dev --port 2024 --config ./langgraph.json
```

Minimal `langgraph.json` (place at project root):

```json
{
  "dependencies": ["."],
  "graphs": {
    "my_agent": "./my_agent.py:graph"
  },
  "env": ".env"
}
```

Open `http://localhost:2024` in a browser. Your compiled graph appears as an interactive node-edge diagram. Hot-reloads on file save — no restart needed.

> No Docker required. `langgraph dev` runs entirely in your local Python environment.

---

### Navigating the Graph

| Panel | What it shows | How to use it |
|---|---|---|
| **Graph canvas** | Nodes as boxes, edges as arrows, conditional edges as diamonds | Click a node to highlight it and open its detail pane |
| **Node list** (left sidebar) | All nodes in execution order | Click to jump to that node's state diff |
| **Edge list** (left sidebar) | All edges including conditional | Verify routing logic is wired as expected |
| **State inspector** (right panel) | Current state dict at the selected step | Expand keys to inspect nested values; spot `None` or wrong types instantly |
| **Thread selector** (top bar) | Saved thread runs | Switch between runs to compare state across invocations |

---

### Step-Through Debugging

Studio lets you click through execution one node at a time without modifying code.

1. Submit input via the **Input** panel (JSON or natural language)
2. Click **Run** — execution pauses at the first node
3. Use **Step** (right arrow) to advance one node at a time
4. Watch the **State inspector** update after each node — spot exactly where a value goes wrong
5. The active node is highlighted in the canvas; failed nodes appear in red with the exception message inline

**Reading the execution timeline** (bottom panel):

```
[START] → generate_topic → write_joke → [END]
              ✓                ✗  ← red = exception raised here
```

Click the red node to see the full Python traceback alongside the input state that was passed to it. No log-scanning needed.

---

### State Injection (Edit State Mid-Execution)

Studio lets you patch state values while paused, then continue — the fastest way to test a hypothesis without restarting.

1. Pause execution at any node using **Step** mode or a breakpoint (see below)
2. In the **State inspector**, click the pencil icon next to any field
3. Type the corrected value and press **Apply**
4. Click **Continue** — the graph resumes with the patched state

Use this to:
- Replace a bad tool result with a known-good value to verify downstream behavior
- Set a counter or flag to force a specific branch
- Inject a mock LLM response to test error-handling paths

---

### Breakpoints (interrupt_before / interrupt_after)

Add breakpoints to your compiled graph to pause automatically before or after a specific node. Remove them after debugging — they are not meant for production.

```python
from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()

# Pause BEFORE "validate_output" runs — inspect incoming state
graph = builder.compile(
    checkpointer=checkpointer,
    interrupt_before=["validate_output"],
)

# Pause AFTER "call_llm" runs — inspect what the LLM returned
graph = builder.compile(
    checkpointer=checkpointer,
    interrupt_after=["call_llm"],
)

# Pause at multiple nodes
graph = builder.compile(
    checkpointer=checkpointer,
    interrupt_before=["validate_output", "write_result"],
)
```

After `invoke()` or `stream()` returns at the breakpoint, resume with:

```python
from langgraph.types import Command

# Option A: continue with no state change
graph.invoke(None, config)

# Option B: inject a state patch then continue
graph.update_state(config, {"field": "corrected_value"})
graph.invoke(None, config)
```

Studio also lets you set breakpoints interactively via the node right-click menu, without editing code.

---

### Terminal Graph Rendering (No Browser Required)

For SSH sessions, CI, or environments without a browser, render the graph structure in the terminal or a Jupyter cell.

```python
# ASCII art — works in any terminal, zero dependencies
print(graph.get_graph().draw_ascii())

# Example output:
#        +-----------+
#        | __start__ |
#        +-----------+
#               |
#        +------v------+
#        | fetch_data  |
#        +------+------+
#               |
#        +------v------+
#        | call_llm    |
#        +------+------+
#               |
#        +------v------+
#        | __end__     |
#        +-------------+

# Mermaid source — paste at mermaid.live or embed in docs
print(graph.get_graph().draw_mermaid())

# PNG (Jupyter / VS Code notebooks — requires IPython)
from IPython.display import Image, display
display(Image(graph.get_graph().draw_mermaid_png()))

# Subgraph support — pass xray=True to expand nested subgraphs
print(graph.get_graph(xray=True).draw_ascii())
```

`draw_ascii()` is the zero-dependency fallback. Use it first when debugging in a remote environment, then switch to Studio for interactive inspection.

---

## Step 0 — Enable Visibility (Do This First)

Before reading the trace, turn on instrumentation so you can see what's happening.

```python
# Option A: LangSmith (best — full token-level trace in UI)
import os
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"] = "ls-..."          # get at smith.langchain.com
os.environ["LANGCHAIN_PROJECT"] = "my-debug-project"

# Option B: verbose console logging (no account needed)
from langchain.globals import set_verbose, set_debug
set_verbose(True)   # log chain inputs/outputs
set_debug(True)     # log every LLM call, including raw prompts

# Option C: LangGraph state inspection (after any invoke/stream call)
state = graph.get_state(config)
print(state.values)          # current state dict
print(state.next)            # which node runs next

# Option D: Time-travel (inspect state at every checkpoint)
for snapshot in graph.get_state_history(config):
    print(snapshot.config["configurable"]["checkpoint_id"], snapshot.values)
```

---

## Category 1 — Setup / Import Errors

**Symptoms:** `ModuleNotFoundError`, `ImportError`, `ValidationError` on init, `AuthenticationError` before any LLM call.

| Error | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'langchain_openai'` | Package not installed | `pip install langchain-openai` |
| `ModuleNotFoundError: No module named 'langchain_community'` | Community tools in separate package | `pip install langchain-community` |
| `ImportError: cannot import name 'ChatOpenAI' from 'langchain'` | Importing from old top-level `langchain` | Change to `from langchain_openai import ChatOpenAI` |
| `ImportError: cannot import name 'ConversationBufferMemory'` | Legacy memory removed in v0.3 | Migrate to LangGraph checkpointing (see `lc:memory`) |
| `ValidationError: field required: openai_api_key` | API key not set | `export OPENAI_API_KEY=sk-...` or pass `api_key=` arg |
| `openai.AuthenticationError: Incorrect API key` | Wrong/expired key | Check key at platform.openai.com; regenerate if needed |

**Package map for common classes:**

```
ChatOpenAI, OpenAIEmbeddings    → pip install langchain-openai
ChatAnthropic                   → pip install langchain-anthropic
ChatGoogleGenerativeAI          → pip install langchain-google-genai
ChatOllama                      → pip install langchain-ollama
Chroma                          → pip install langchain-chroma chromadb
Document loaders (PDF, web...)  → pip install langchain-community
LangGraph                       → pip install langgraph
LangGraph SQLite checkpoint     → pip install langgraph-checkpoint-sqlite
LangGraph Postgres checkpoint   → pip install langgraph-checkpoint-postgres
```

---

## Category 2 — LLM Call Errors

**Symptoms:** Error raised during `.invoke()`, `.stream()`, or `.batch()` on a model or chain.

| Error | Cause | Fix |
|---|---|---|
| `openai.RateLimitError: 429` | Too many requests / quota | Use `.with_retry()` (see below); add `time.sleep` between batches |
| `openai.AuthenticationError` | Key missing or wrong at call time | Verify `OPENAI_API_KEY` in env; use `os.getenv` to debug |
| `openai.BadRequestError: maximum context length` | Prompt too long | Use `trim_messages()` (see `lc:memory` Pattern 2) |
| `openai.BadRequestError: Invalid value for 'content'` | Malformed message (common with tools) | Check message list for `None` content; tool messages need `tool_call_id` |
| `openai.APITimeoutError` | Network or slow model | Use `.with_retry(stop_after_attempt=3)` + increase timeout |
| `anthropic.BadRequestError: prompt must start with human turn` | Missing human message | Ensure first message is `HumanMessage`; add a system message before |

### Retry Pattern

```python
from langchain_openai import ChatOpenAI
from langchain_core.rate_limiters import InMemoryRateLimiter

# Option A: built-in retry with exponential backoff
model = ChatOpenAI(model="gpt-4o").with_retry(
    stop_after_attempt=4,
    wait_exponential_jitter=True,   # adds jitter to avoid thundering herd
)

# Option B: rate limiter (prevents hitting limits in the first place)
rate_limiter = InMemoryRateLimiter(
    requests_per_second=0.5,        # 1 request every 2 seconds
    max_bucket_size=10,
)
model = ChatOpenAI(model="gpt-4o", rate_limiter=rate_limiter)

# Option C: both together
model = ChatOpenAI(model="gpt-4o", rate_limiter=rate_limiter).with_retry(
    stop_after_attempt=3,
)
```

### Diagnosing Malformed Messages

```python
# Print the exact message list before sending to LLM
for i, msg in enumerate(messages):
    print(f"[{i}] {type(msg).__name__}: role={getattr(msg, 'role', '?')} "
          f"content_type={type(msg.content)} tool_call_id={getattr(msg, 'tool_call_id', 'N/A')}")
```

---

## Category 3 — Output Parsing Errors

**Symptoms:** `OutputParserException`, `PydanticValidationError`, `JSONDecodeError`, structured output returns `None`.

| Error | Cause | Fix |
|---|---|---|
| `OutputParserException: Got invalid JSON` | LLM added prose around JSON | Use `OutputFixingParser` wrapper (see below) |
| `PydanticValidationError: field required` | LLM omitted a required field | Add field to prompt with example; make field `Optional` with default |
| `JSONDecodeError: Expecting value` | LLM returned markdown code fences around JSON | Strip fences or use `JsonOutputParser` which handles them |
| `ValidationError: value is not a valid ...` | LLM returned wrong type (string where int expected) | Add type coercion or use `Optional[str]` then parse |
| `KeyError: 'answer'` | LLM returned different key name | Use `.with_structured_output()` instead of manual parsing |

### Fix Patterns

```python
# Pattern A: .with_structured_output() — most robust (handles JSON extraction automatically)
from pydantic import BaseModel
from langchain_openai import ChatOpenAI

class Answer(BaseModel):
    reasoning: str
    answer: str
    confidence: float

model = ChatOpenAI(model="gpt-4o")
structured = model.with_structured_output(Answer)
result = structured.invoke("What is 2+2? Explain your reasoning.")
# result is an Answer instance — guaranteed schema compliance

# Pattern B: OutputFixingParser — wraps any parser, re-prompts LLM to fix bad output
from langchain.output_parsers import OutputFixingParser
from langchain_core.output_parsers import JsonOutputParser

base_parser = JsonOutputParser(pydantic_object=Answer)
fixing_parser = OutputFixingParser.from_llm(parser=base_parser, llm=model)
# fixing_parser.parse(bad_output) will automatically retry with the LLM

# Pattern C: add examples to system prompt to steer format
SYSTEM = """You MUST respond with valid JSON only. No prose. No code fences.
Example output:
{"reasoning": "...", "answer": "...", "confidence": 0.9}"""
```

---

## Category 4 — Tool / Agent Errors

**Symptoms:** Agent stops unexpectedly, `ToolException`, `ValueError` in tool, agent loops without terminating, `StopIteration: max iterations reached`.

| Error | Cause | Fix |
|---|---|---|
| `ToolException: ...` (unhandled) | Tool raised but agent didn't catch it | Set `handle_tool_errors=True` on `AgentExecutor` or use `ToolNode` error handler |
| `ValueError: Tool input validation error` | Agent passed wrong type to tool | Add type annotations to tool `@tool` function; add input description |
| `StopIteration: Reached max iterations` | Agent in loop — can't decide when to stop | Add `max_iterations=` cap; check if tool outputs are clear enough |
| `Tool call JSON parse failure` | LLM returned malformed tool call JSON | Use a more capable model; add system prompt examples of tool calls |
| Tool returns `None` | Tool function missing `return` | Add explicit `return` statement to every tool |
| Tool output causes next error | Tool returns non-string | Ensure tools return `str`; stringify with `json.dumps()` if needed |

### Fix Patterns

```python
# Pattern A: handle tool errors in LangGraph ToolNode
from langgraph.prebuilt import ToolNode

def handle_tool_error(state):
    error = state.get("error")
    tool_calls = state["messages"][-1].tool_calls
    return {
        "messages": [
            ToolMessage(
                content=f"Error: {repr(error)}\nPlease fix your inputs and retry.",
                tool_call_id=tc["id"],
            )
            for tc in tool_calls
        ]
    }

tool_node = ToolNode(tools).with_fallbacks(
    [RunnableLambda(handle_tool_error)],
    exception_key="error",
)

# Pattern B: cap agent iterations and detect loops
from langgraph.prebuilt import create_react_agent

agent = create_react_agent(
    model=model,
    tools=tools,
    # LangGraph recursion limit applies globally; set per-run:
)
config = {"recursion_limit": 25}  # default is 25; raise if legitimate deep chains
result = agent.invoke({"messages": [...]}, config)

# Pattern C: well-annotated tool prevents bad inputs
from langchain_core.tools import tool

@tool
def search_documents(query: str, max_results: int = 5) -> str:
    """Search the document store.

    Args:
        query: Natural language search query (e.g. "revenue in Q3 2024")
        max_results: Number of results to return, 1-20. Default 5.

    Returns:
        JSON string with list of matching document snippets.
    """
    results = doc_store.search(query, k=max_results)
    return json.dumps([r.page_content for r in results])
```

---

## Category 5 — LangGraph-Specific Errors

**Symptoms:** `InvalidUpdateError`, node returns `None`, graph hits recursion limit, `GraphRecursionError`, checkpoint deserialization fails, subgraph state mapping fails.

| Error | Cause | Fix |
|---|---|---|
| `InvalidUpdateError: Expected dict, got None` | Node returned `None` instead of state dict | Add `return {}` or `return {"field": value}` to every node |
| `GraphRecursionError: Recursion limit of N reached` | Infinite loop in graph | Add base case to conditional edges; inspect with `get_state_history` |
| `InvalidUpdateError: Channel X received unexpected type` | Node returned wrong type for state field | Check `TypedDict` schema; use `Annotated[list, add_messages]` for message fields |
| Checkpoint deserialization error | State contains non-serializable object | Use only JSON-serializable types in state; serialize custom objects |
| `ValueError: Interrupt not resumed` | `interrupt()` called but resume not provided | Pass `Command(resume=value)` when invoking after interrupt |
| Subgraph state not flowing to parent | Subgraph channel names don't match parent | Add explicit input/output channel mapping in `add_node(..., input=..., output=...)` |

### Fix Patterns

```python
# Pattern A: every node MUST return a dict
def my_node(state: MyState) -> dict:           # annotate return type
    result = do_work(state["input"])
    return {"output": result}                  # NEVER return None

# Pattern B: debug infinite loop with state history
config = {"configurable": {"thread_id": "debug-1"}}
graph.invoke(input_data, config)

for i, snapshot in enumerate(graph.get_state_history(config)):
    print(f"Step {i}: next={snapshot.next} values={snapshot.values}")
    if i > 30:
        break  # safety

# Pattern C: correct state schema for messages
from typing import Annotated
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

class State(TypedDict):
    messages: Annotated[list, add_messages]    # reducer handles append+dedup
    status: str                                # plain field — gets replaced on update

# Pattern D: resume after interrupt
from langgraph.types import Command

# First call — hits interrupt
graph.invoke({"messages": [...]}, config)

# Inspect what it's waiting for
state = graph.get_state(config)
print(state.next, state.tasks)

# Resume
graph.invoke(Command(resume="user approved"), config)

# Pattern E: subgraph with explicit channel mapping
builder.add_node(
    "subgraph",
    subgraph_compiled,
    input={"sub_input": "parent_field"},   # parent_field → sub_input
    output={"parent_field": "sub_output"}, # sub_output → parent_field
)
```

---

## Category 6 — Memory / State Errors

**Symptoms:** Bot forgets previous messages, state resets on every call, `OperationalError` on checkpoint DB, state mutation causes unexpected behavior.

| Error | Cause | Fix |
|---|---|---|
| Bot forgets everything between turns | `thread_id` not set in config | Always pass `{"configurable": {"thread_id": "..."}}` |
| Bot forgets everything on restart | Using `InMemorySaver` in production | Switch to `SqliteSaver` or `PostgresSaver` (see `lc:memory` Pattern 4) |
| `OperationalError: no such table: checkpoints` | Postgres/SQLite tables not created | Call `checkpointer.setup()` once before first use |
| `sqlite3.OperationalError: database is locked` | Multiple threads sharing one SQLite connection | Use `async with SqliteSaver.from_conn_string(":memory:") as cp:` or Postgres |
| State field overwritten instead of appended | Using plain `list` instead of `Annotated[list, add_messages]` | Change messages field to `Annotated[list, add_messages]` |
| State mutated in node (side effect) | `state["messages"].append(...)` | Never mutate state; return new values: `return {"messages": [new_msg]}` |
| Memory grows unbounded → context overflow | No trimming on checkpointed graph | Add `trim_messages()` in node before LLM call (trim send, not state) |

```python
# WRONG — mutates state
def bad_node(state):
    state["messages"].append(HumanMessage("oops"))  # DO NOT DO THIS
    return state

# CORRECT — returns new values, reducer handles merge
def good_node(state):
    return {"messages": [HumanMessage("correct")]}  # add_messages reducer appends

# CORRECT — checkpoint config
config = {"configurable": {"thread_id": "user-alice-42"}}
result = graph.invoke(input_data, config)      # pass config on EVERY call
```

---

## Category 7 — Async Errors

**Symptoms:** `RuntimeError: This event loop is already running`, `RuntimeError: cannot be called from a running event loop`, sync tool blocks async graph.

| Error | Cause | Fix |
|---|---|---|
| `RuntimeError: This event loop is already running` | `asyncio.run()` inside Jupyter or FastAPI | Use `await graph.ainvoke(...)` directly; or `nest_asyncio.apply()` for Jupyter |
| `RuntimeError: cannot be called from a running event loop` | Same as above, different surface | Replace `asyncio.run(coro())` with `await coro()` |
| Sync tool blocks async graph | Calling blocking IO in async node | Wrap with `asyncio.to_thread(sync_fn, args)` |
| `SynchronousCallbackManagerForChainRun` warning | Sync chain inside async chain | Use async variants: `.ainvoke()`, `.astream()`, `.abatch()` |

```python
# Pattern A: async graph invocation (preferred)
import asyncio
from langgraph.graph import StateGraph, START, MessagesState

async def main():
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "hello"}]},
        {"configurable": {"thread_id": "async-1"}},
    )
    return result

# In a script:
asyncio.run(main())

# In Jupyter (event loop already running):
import nest_asyncio
nest_asyncio.apply()          # one-time; allows nested asyncio.run()
result = await graph.ainvoke(...)

# Pattern B: wrap blocking tool for async context
import asyncio
from langchain_core.tools import tool

@tool
async def slow_database_lookup(query: str) -> str:
    """Look up records from legacy sync database."""
    def _sync():
        return legacy_db.query(query)            # blocking call
    return await asyncio.to_thread(_sync)        # runs in thread pool

# Pattern C: async node with async tool
async def call_tools(state: MessagesState):
    # use ainvoke, not invoke
    response = await model_with_tools.ainvoke(state["messages"])
    return {"messages": [response]}
```

---

## Phase 8 — Time-Travel Debugging

Use time-travel when a graph run has already completed (or crashed) and you need to replay it from a specific point, patch bad state, or branch from a known-good checkpoint — without re-running earlier, expensive steps.

**Requires:** graph compiled with a checkpointer. `InMemorySaver` is fine for local debugging; `SqliteSaver` / `PostgresSaver` for persistence across restarts.

---

### How Checkpoints Work

Every time a node completes, LangGraph saves a `StateSnapshot` to the checkpointer. `get_state_history(config)` returns all snapshots for a thread in **reverse chronological order** (newest first).

```python
StateSnapshot(
    values={...},          # full state dict at this point in time
    next=("node_name",),   # tuple of nodes that will run next
                           # () = graph finished or was interrupted
    config={               # use this config to target this exact checkpoint
        "configurable": {
            "thread_id": "...",
            "checkpoint_id": "1ef663ba-28fe-6528-8002-5a559208592c",
        }
    },
    metadata={
        "step": 3,         # execution step number (0-indexed)
        "source": "loop",  # "input" | "loop" | "update"
        "writes": {...},   # what the node wrote to state
    },
    created_at="2025-...",
    parent_config={...},   # config of the preceding checkpoint
    tasks=(...),           # PregelTask objects — inspect for errors
    interrupts=(),
)
```

---

### Core API

```python
# 1. Get full history — returns an iterator of StateSnapshot objects
history = list(graph.get_state_history(config))
# history[0]  = most recent checkpoint (after last node)
# history[-1] = oldest checkpoint (before first node)

# 2. Get state at a specific checkpoint by ID
target_config = {
    "configurable": {
        "thread_id": "my-thread",
        "checkpoint_id": "1ef663ba-28fe-6528-8002-5a559208592c",
    }
}
snapshot = graph.get_state(target_config)

# 3. Patch state at a checkpoint — returns a new config pointing to the patched state
new_config = graph.update_state(
    target_config,                        # which checkpoint to patch
    {"field": "corrected_value"},         # partial state update (merged, not replaced)
)

# 4. Resume from the patched checkpoint
result = graph.invoke(None, new_config)   # None = no new input; continue from checkpoint
# async variant:
result = await graph.ainvoke(None, new_config)
```

---

### Step-by-Step: Find the Failure, Patch, and Replay

```python
import os
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END
from typing_extensions import TypedDict

# --- Setup (your existing graph, compiled with a checkpointer) ---
checkpointer = InMemorySaver()
graph = builder.compile(checkpointer=checkpointer)

config = {"configurable": {"thread_id": "debug-session-1"}}

# Step 1: Run the graph — it crashes or produces wrong output
try:
    result = graph.invoke({"query": "..."}, config)
except Exception as e:
    print(f"Graph failed: {e}")

# Step 2: Walk the history to find where it went wrong
print("\n--- Execution history (newest first) ---")
history = list(graph.get_state_history(config))

for i, snapshot in enumerate(history):
    checkpoint_id = snapshot.config["configurable"]["checkpoint_id"]
    step = snapshot.metadata.get("step", "?")
    writes = snapshot.metadata.get("writes")
    print(f"[{i}] step={step:>3}  next={snapshot.next}  checkpoint={checkpoint_id[:16]}...")
    if writes:
        print(f"       writes: {list(writes.keys())}")

# Step 3: Identify the last GOOD checkpoint — the one BEFORE the failing node
# Example: if "validate_output" raised an error, find the checkpoint where
# next=("validate_output",), meaning it hasn't run yet.
failing_node = "validate_output"
target = next(
    (s for s in history if failing_node in s.next),
    None,
)

if target is None:
    print(f"Node '{failing_node}' not found in history. Check the node name.")
else:
    checkpoint_id = target.config["configurable"]["checkpoint_id"]
    print(f"\nFound checkpoint before '{failing_node}': {checkpoint_id}")
    print(f"State at that point: {target.values}")

# Step 4: Patch the state — fix whatever caused the failure
# Common patches:
#   - Replace a bad tool result
#   - Fix a malformed field
#   - Reset a counter to escape an infinite loop
patched_config = graph.update_state(
    target.config,
    {"tool_output": "corrected result"},   # only the fields you want to change
)

# Step 5: Resume from the patched checkpoint
# The graph replays from `failing_node` onward with the corrected state.
# Nodes BEFORE the checkpoint are NOT re-executed (no wasted LLM calls).
result = graph.invoke(None, patched_config)
print(f"\nResult after patch: {result}")
```

---

### Common Time-Travel Use Cases

| Scenario | What to patch | How |
|---|---|---|
| Tool returned garbage | `tool_output` or `messages` | Replace the bad `ToolMessage` content |
| LLM chose wrong branch | Routing field (e.g. `next_step`) | Set it to the correct branch value |
| Infinite loop — counter stuck | Loop counter or `iteration` field | Set counter to exit condition |
| Malformed state from subgraph | The field the subgraph wrote | Overwrite with a valid value |
| Re-run from a known-good point | Any field | `update_state` with no changes — just get a new config from a prior checkpoint |

---

### Async Time-Travel

```python
# All time-travel APIs have async equivalents
async def debug_async(graph, config):
    history = []
    async for snapshot in graph.aget_state_history(config):
        history.append(snapshot)

    target = next(s for s in history if "bad_node" in s.next)

    patched_config = await graph.aupdate_state(
        target.config,
        {"field": "fixed_value"},
    )

    result = await graph.ainvoke(None, patched_config)
    return result
```

---

### What Time-Travel Cannot Do

- **Cannot un-run side effects** — if a node sent an email or wrote to a database, patching state does not undo that. Guard side-effectful nodes with human-in-the-loop interrupts if rollback matters.
- **Cannot patch across threads** — `update_state` is scoped to a single `thread_id`. To branch into a new thread, copy the state manually and start a new thread.
- **Requires a checkpointer** — graphs compiled without `checkpointer=` have no history to query.

---

## Debugging Tools Reference

| Tool | When to Use | Command |
|---|---|---|
| LangSmith trace | Any error — see full token flow | Set `LANGCHAIN_TRACING_V2=true` |
| `set_debug(True)` | No LangSmith; want raw prompts in console | `from langchain.globals import set_debug; set_debug(True)` |
| `graph.get_state(config)` | LangGraph — what is the current state? | `state = graph.get_state(config); print(state.values)` |
| `graph.get_state_history(config)` | LangGraph — trace node-by-node execution | `for s in graph.get_state_history(config): print(s.next)` |
| `graph.stream(..., stream_mode="debug")` | LangGraph — live event stream per node | `for event in graph.stream(input, config, stream_mode="debug"): print(event)` |
| `breakpoint()` in node | Need Python debugger inside a node | Add `breakpoint()` line in node function; run with `python -m pdb` |
| LangGraph Studio | Visual graph + step-through + state injection | `langgraph dev` — opens Studio at localhost:2024 |
| `graph.get_graph().draw_ascii()` | Terminal graph rendering (no browser) | `print(graph.get_graph().draw_ascii())` |
| `graph.get_graph().draw_mermaid()` | Mermaid source for docs/mermaid.live | `print(graph.get_graph().draw_mermaid())` |
| `graph.get_graph().draw_mermaid_png()` | PNG in Jupyter / VS Code notebooks | `display(Image(graph.get_graph().draw_mermaid_png()))` |
| `graph.get_state_history(config)` | Time-travel: all checkpoints for a thread | `history = list(graph.get_state_history(config))` |
| `graph.update_state(target.config, patch)` | Time-travel: patch state at a checkpoint | `new_cfg = graph.update_state(target.config, {"field": "fix"})` |

---

## Minimal Repro Template

When the fix isn't obvious, reduce to the smallest failing case:

```python
"""
Minimal repro — paste this, run it, share the full traceback.
"""
import os
os.environ["LANGCHAIN_TRACING_V2"] = "true"   # comment out if no LangSmith key

from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END, MessagesState

model = ChatOpenAI(model="gpt-4o-mini")

def my_node(state: MessagesState):
    # ISOLATE: only the failing logic here
    response = model.invoke(state["messages"])
    return {"messages": [response]}

builder = StateGraph(MessagesState)
builder.add_node("my_node", my_node)
builder.add_edge(START, "my_node")
builder.add_edge("my_node", END)
graph = builder.compile()

result = graph.invoke(
    {"messages": [{"role": "user", "content": "hello"}]},
    {"configurable": {"thread_id": "repro-1"}},
)
print(result)
```

---

## Error → Category Quick Lookup

| Error string | Category |
|---|---|
| `ModuleNotFoundError` / `ImportError` | 1 — Setup |
| `ValidationError` on model init | 1 — Setup |
| `AuthenticationError` / `RateLimitError` / `APITimeoutError` | 2 — LLM Call |
| `BadRequestError: maximum context length` | 2 — LLM Call |
| `OutputParserException` / `JSONDecodeError` | 3 — Output Parsing |
| `PydanticValidationError` on LLM output | 3 — Output Parsing |
| `ToolException` / `Tool input validation error` | 4 — Tool/Agent |
| `StopIteration: max iterations` | 4 — Tool/Agent |
| `InvalidUpdateError` / `GraphRecursionError` | 5 — LangGraph |
| Node returns `None` | 5 — LangGraph |
| Bot forgets messages / resets on restart | 6 — Memory/State |
| `OperationalError: no such table: checkpoints` | 6 — Memory/State |
| `RuntimeError: event loop already running` | 7 — Async |
| `cannot be called from a running event loop` | 7 — Async |

---

## Common Mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| `from langchain import ChatOpenAI` | `ImportError` | `from langchain_openai import ChatOpenAI` |
| Node returns `None` | `InvalidUpdateError` | Return `{}` or `{"field": value}` always |
| No `thread_id` in config | Bot forgets everything | `config = {"configurable": {"thread_id": "..."}}` |
| `asyncio.run()` inside Jupyter | `RuntimeError: event loop` | `nest_asyncio.apply()` then `await` directly |
| Mutating state in node | Unpredictable state merging | Return new values; never `.append()` on state list |
| `InMemorySaver` in production | State lost on restart | Use `SqliteSaver` or `PostgresSaver` |
| Missing `return` in tool | Tool silently returns `None` | Add `return str(result)` to all tool functions |
| Forgetting `checkpointer.setup()` | DB table missing error | Call once on deploy: `checkpointer.setup()` |

---

## See Also

- `lc:memory` — checkpointing, trimming, vector/entity memory patterns
- `lc:agents` — tool-calling agents, ReAct, planning patterns
- `lc:rag` — retrieval-augmented generation debugging
- `lc:tools` — tool definition, validation, error handling
