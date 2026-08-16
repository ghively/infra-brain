# lc:graph — LangGraph StateGraph Design Skill

## Trigger

Invoke this skill when the user wants to:
- Build a new LangGraph agent or pipeline
- Design a StateGraph from scratch
- Understand any LangGraph primitive (state, nodes, edges, checkpointing, interrupts, streaming, subgraphs, Send)
- Debug or refactor an existing graph
- Learn LangGraph from first principles

## API Version

This skill targets **LangGraph 1.2.x** (Python). All code examples use the current stable API.

> **Recommended starting point (LangGraph 1.2+):** For new projects, prefer the **Functional API** (`@entrypoint` / `@task`) over raw `StateGraph` unless you specifically need complex conditional routing or dynamic edges. See Phase 11 for the full Functional API guide.

---

## Skill Flow

Work through these phases in order. Each phase is interactive — ask, design, confirm, then scaffold.

```
Phase 1:  Understand the goal
Phase 2:  Design state schema
Phase 3:  Design nodes
Phase 4:  Design edges and routing
Phase 5:  Choose checkpointing strategy
Phase 6:  Add interrupts (if needed)
Phase 7:  Scaffold complete graph code
Phase 8:  Show compilation and invocation patterns
Phase 9:  The Send API (dynamic fan-out)
Phase 10: Full scaffold template
Phase 11: Functional API (@entrypoint / @task)   ← NEW — recommended starting point
```

At each phase, **teach the relevant concept**, then apply it to the user's specific graph.

---

## Phase 1: Understand the Goal

Ask the user:

1. What does this graph need to **do**? (e.g., "research assistant that searches the web and synthesizes an answer")
2. What are the rough **steps** or **stages**? (e.g., "query → search → rank → synthesize → output")
3. Does it need **memory** across sessions? (→ checkpointing decision)
4. Does it need **human approval** at any step? (→ interrupt decision)
5. Will it run **in parallel** across multiple items? (→ Send API decision)

Use the answers to drive every subsequent phase.

---

## Phase 2: State Schema Design

### Concept: What is State?

State is the **single shared data structure** that flows through every node in the graph. Nodes read from it and return partial updates to it. LangGraph merges updates using **reducers**.

LangGraph supports two schema styles: `TypedDict` and Pydantic `BaseModel`.

---

### 2.1 TypedDict (most common)

```python
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages

class State(TypedDict):
    messages: Annotated[list, add_messages]  # reducer: append, not replace
    topic: str                                # no reducer: last-write-wins
    iteration: int                            # no reducer: last-write-wins
```

**Rules:**
- Fields with **no annotation reducer** are overwritten on each update (last-write-wins).
- Fields with an **`Annotated` reducer** are merged using that reducer function.
- A node only needs to return the keys it changes.

---

### 2.2 Pydantic BaseModel

```python
from pydantic import BaseModel, Field
from typing import Annotated
from langgraph.graph.message import add_messages

class State(BaseModel):
    messages: Annotated[list, add_messages] = Field(default_factory=list)
    topic: str = ""
    confidence: float = 0.0
```

**When to use Pydantic:**
- You need field validation (e.g., `confidence` must be 0.0–1.0)
- You want default values declared on the schema
- You're exposing state to external APIs that expect Pydantic models

**Trade-off:** Slightly more overhead than TypedDict; not necessary for most graphs.

---

### 2.3 The `add_messages` Reducer

`add_messages` is the most important built-in reducer. It does three things:
1. Appends new messages to the existing list (does not replace).
2. Deduplicates by `id` — if a message with the same `id` arrives, it **replaces** the old one (useful for updating tool call results).
3. Deserializes message dicts into proper `BaseMessage` objects.

```python
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages

class State(TypedDict):
    messages: Annotated[list, add_messages]
```

**MessagesState shorthand** — import the pre-built version instead of writing it yourself:

```python
from langgraph.graph import MessagesState

# Equivalent to:
# class MessagesState(TypedDict):
#     messages: Annotated[list[AnyMessage], add_messages]
```

Use `MessagesState` as your base for any chat/agent graph. Subclass to add more fields:

```python
from langgraph.graph import MessagesState

class AgentState(MessagesState):
    search_results: list[str]   # appended by operator.add
    final_answer: str           # last-write-wins
```

---

### 2.4 Custom Reducers

Use `operator.add` for numeric accumulation or list concatenation:

```python
import operator
from typing import Annotated
from typing_extensions import TypedDict

class State(TypedDict):
    scores: Annotated[list[float], operator.add]   # concatenates lists
    total: Annotated[int, operator.add]             # adds integers
```

Write a **custom reducer function** for any merge logic not covered by builtins:

```python
def merge_dicts(left: dict, right: dict) -> dict:
    """Merge two dicts, with right taking precedence on key conflicts."""
    return {**left, **right}

class State(TypedDict):
    metadata: Annotated[dict, merge_dicts]
```

For **keeping only unique items**:

```python
def union_lists(left: list, right: list) -> list:
    seen = set(left)
    return left + [x for x in right if x not in seen]

class State(TypedDict):
    visited_urls: Annotated[list[str], union_lists]
```

---

### 2.5 Input/Output Schemas (Separate from Internal State)

By default, the graph uses the same schema for input, internal state, and output. For complex graphs, you can separate these:

```python
from typing_extensions import TypedDict
from langgraph.graph import StateGraph

class InputState(TypedDict):
    """What the caller provides."""
    user_query: str

class OutputState(TypedDict):
    """What the caller receives."""
    answer: str
    sources: list[str]

class InternalState(InputState, OutputState):
    """Everything — used inside nodes."""
    search_results: list[dict]
    draft_answer: str
    revision_count: int

graph = StateGraph(InternalState, input=InputState, output=OutputState)
```

This pattern keeps the public API clean while nodes can work with a richer internal state.

---

### 2.6 State Schema Examples for 5 Common Use Cases

**1. Simple chat agent**
```python
from langgraph.graph import MessagesState

class ChatState(MessagesState):
    system_prompt: str
```

**2. Research / RAG agent**
```python
from typing import Annotated
import operator
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages

class ResearchState(TypedDict):
    messages: Annotated[list, add_messages]
    query: str
    search_results: Annotated[list[dict], operator.add]  # accumulate across searches
    sources: Annotated[list[str], operator.add]
    answer: str
    iteration: int
```

**3. Code generation / review loop**
```python
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages

class CodeState(TypedDict):
    messages: Annotated[list, add_messages]
    requirements: str
    generated_code: str
    test_results: str
    approved: bool
    revision_count: int
```

**4. Multi-step data pipeline**
```python
from typing import Annotated
import operator
from typing_extensions import TypedDict

class PipelineState(TypedDict):
    raw_records: list[dict]
    cleaned_records: list[dict]
    errors: Annotated[list[str], operator.add]
    processed_count: int
    report: str
```

**5. Map-reduce (parallel processing)**
```python
from typing import Annotated
import operator
from typing_extensions import TypedDict

class MapReduceState(TypedDict):
    documents: list[str]          # input list to fan out over
    summaries: Annotated[list[str], operator.add]  # collected by reducer
    final_summary: str
```

---

## Phase 3: Node Design

### Concept: What is a Node?

A node is a **Python function** (sync or async) that:
1. Receives the current state as its only argument.
2. Returns a **dict of partial state updates** (only the keys it changes).

Nodes never modify state in-place. They return updates; LangGraph applies them.

---

### 3.1 Basic Node Signature

```python
# Sync
def my_node(state: State) -> dict:
    return {"some_key": "some_value"}

# Async (preferred for I/O-bound work like LLM calls)
async def my_node(state: State) -> dict:
    return {"some_key": "some_value"}
```

**Return only what changed.** If a node only updates `answer`, return `{"answer": "..."}`. Do not return the full state.

---

### 3.2 LLM Calls Inside Nodes

```python
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

llm = ChatOpenAI(model="gpt-4o", temperature=0)

async def call_llm(state: State) -> dict:
    response = await llm.ainvoke(state["messages"])
    return {"messages": [response]}  # add_messages reducer appends this
```

For structured output (tool calling / JSON mode):

```python
from pydantic import BaseModel

class AnswerWithSources(BaseModel):
    answer: str
    sources: list[str]

structured_llm = llm.with_structured_output(AnswerWithSources)

async def extract_answer(state: State) -> dict:
    result = await structured_llm.ainvoke(state["messages"])
    return {"answer": result.answer, "sources": result.sources}
```

---

### 3.3 Returning Command Objects (LangGraph 1.2+)

`Command` lets a node **both update state AND route** to the next node — replacing the need for a separate conditional edge in many cases.

```python
from langgraph.types import Command
from typing_extensions import Literal

def router_node(state: State) -> Command[Literal["node_b", "node_c", "__end__"]]:
    if state["confidence"] > 0.9:
        return Command(update={"status": "done"}, goto="__end__")
    elif state["iteration"] < 3:
        return Command(update={"iteration": state["iteration"] + 1}, goto="node_b")
    else:
        return Command(update={"status": "gave_up"}, goto="node_c")
```

**Type hint `Command[Literal["..."]]`** tells LangGraph which nodes this Command can route to — used for graph validation.

---

### 3.4 Tool Calls Inside Nodes vs ToolNode

**Option A: Manual tool execution inside a node** (full control)

```python
from langchain_core.tools import tool

@tool
def search_web(query: str) -> str:
    """Search the web for a query."""
    # ... actual search implementation
    return f"Results for: {query}"

async def agent_node(state: State) -> dict:
    # LLM decides to call a tool
    response = await llm.bind_tools([search_web]).ainvoke(state["messages"])

    if response.tool_calls:
        # Execute tools manually
        results = []
        for tc in response.tool_calls:
            result = search_web.invoke(tc["args"])
            results.append(ToolMessage(content=result, tool_call_id=tc["id"]))
        return {"messages": [response] + results}
    else:
        return {"messages": [response]}
```

**Option B: ToolNode** (recommended — handles tool dispatch automatically)

```python
from langgraph.prebuilt import ToolNode

tools = [search_web, another_tool]
tool_node = ToolNode(tools)

# ToolNode reads the last AIMessage's tool_calls,
# executes all of them (in parallel if multiple),
# and returns ToolMessages.
```

Wire it into the graph:

```python
from langgraph.prebuilt import ToolNode, tools_condition

builder.add_node("tools", ToolNode(tools))
builder.add_node("agent", call_llm_node)

# tools_condition: routes to "tools" if last message has tool_calls, else "__end__"
builder.add_conditional_edges("agent", tools_condition)
builder.add_edge("tools", "agent")  # loop back after tool execution
```

**ToolNode features:**
- Executes all tool calls in the last `AIMessage` in parallel.
- Returns one `ToolMessage` per tool call.
- Handles errors by returning an error `ToolMessage` (configurable).
- Works with any LangChain tool (`@tool`, `BaseTool` subclasses).

---

### 3.5 Error Handling in Nodes

```python
async def risky_node(state: State) -> dict:
    try:
        result = await external_api_call(state["query"])
        return {"result": result, "error": None}
    except TimeoutError:
        return {"error": "timeout", "result": None}
    except Exception as e:
        return {"error": str(e), "result": None}
```

For retry logic, use a counter in state:

```python
async def fetch_with_retry(state: State) -> dict:
    if state.get("retry_count", 0) >= 3:
        return {"error": "max retries exceeded"}
    try:
        result = await fetch(state["url"])
        return {"result": result, "retry_count": 0}
    except Exception as e:
        return {"error": str(e), "retry_count": state.get("retry_count", 0) + 1}
```

---

## Phase 4: Edge Design

### Concept: What are Edges?

Edges define **control flow** — which node runs next after a given node finishes. There are three types:

1. **Direct edges** — always go from A to B.
2. **Conditional edges** — a function inspects state and returns the next node name.
3. **Command-based routing** — the node itself returns a `Command(goto=...)` (covered in Phase 3).

---

### 4.1 Direct Edges

```python
from langgraph.graph import START, END

builder.add_edge(START, "first_node")      # entry point
builder.add_edge("first_node", "second_node")
builder.add_edge("second_node", END)       # terminal
```

`START` and `END` are string constants: `"__start__"` and `"__end__"`. Always use the imported names.

---

### 4.2 Conditional Edges

```python
def route_after_agent(state: State) -> str:
    """Return the name of the next node based on state."""
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "__end__"

builder.add_conditional_edges(
    "agent",              # source node
    route_after_agent,    # router function → returns node name
    {                     # optional: explicit mapping (for validation/clarity)
        "tools": "tools",
        "__end__": "__end__",
    }
)
```

The mapping dict is optional but recommended — it makes the possible routes explicit and allows graph visualization to show all branches.

---

### 4.3 Router Function Patterns

**Simple string return:**
```python
def simple_router(state: State) -> str:
    if state["score"] > 0.8:
        return "high_confidence"
    elif state["score"] > 0.5:
        return "medium_confidence"
    else:
        return "low_confidence"
```

**Literal type annotation (recommended for validation):**
```python
from typing_extensions import Literal

def typed_router(state: State) -> Literal["tools", "review", "__end__"]:
    if state["needs_tool"]:
        return "tools"
    elif state["needs_review"]:
        return "review"
    return "__end__"
```

**`tools_condition` prebuilt router** (for agent loops):
```python
from langgraph.prebuilt import tools_condition
# Returns "tools" if last message has tool_calls, else "__end__"
builder.add_conditional_edges("agent", tools_condition)
```

---

### 4.4 Multiple Entry Points

```python
# Two entry points: "fast_path" and "slow_path"
builder.add_edge(START, "fast_path")
builder.add_edge(START, "slow_path")
# Both run in parallel on the first step
```

---

### 4.5 Complete ReAct Agent Edge Pattern

```python
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", tools_condition)  # → "tools" or END
builder.add_edge("tools", "agent")                       # loop back
```

---

## Phase 5: Checkpointing

### Concept: Why Checkpoints?

A **checkpointer** saves graph state after every node execution. This enables:
- **Persistence** — resume a graph after a process restart.
- **Human-in-the-loop** — pause graph execution, wait for human input, resume.
- **Time-travel** — replay from any past checkpoint.
- **Multi-user sessions** — each `thread_id` is an isolated conversation.

Without a checkpointer, graph state is lost when the Python process ends.

---

### 5.1 MemorySaver (Development Only)

```python
from langgraph.checkpoint.memory import MemorySaver

checkpointer = MemorySaver()
graph = builder.compile(checkpointer=checkpointer)
```

- Stores state in a Python dict in memory.
- Lost on process restart.
- Zero setup, great for prototyping and tests.
- **Do not use in production.**

**Note:** The docs also show `InMemorySaver` as an alias — both work identically.

---

### 5.2 SqliteSaver (Development / Light Production)

```python
from langgraph.checkpoint.sqlite import SqliteSaver

# File-based: persists across restarts
with SqliteSaver.from_conn_string("checkpoints.db") as checkpointer:
    graph = builder.compile(checkpointer=checkpointer)
    result = graph.invoke({"messages": [HumanMessage("hello")]},
                          config={"configurable": {"thread_id": "user-1"}})
```

Async version:
```python
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

async with AsyncSqliteSaver.from_conn_string("checkpoints.db") as checkpointer:
    graph = builder.compile(checkpointer=checkpointer)
    result = await graph.ainvoke(...)
```

- Survives process restarts.
- Single file = easy backup.
- Not suitable for high-concurrency production workloads.

---

### 5.3 PostgresSaver (Production)

```python
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection

DB_URI = "postgresql://user:password@localhost:5432/langgraph_checkpoints"

with Connection.connect(DB_URI, autocommit=True) as conn:
    checkpointer = PostgresSaver(conn)
    checkpointer.setup()  # run once to create tables
    graph = builder.compile(checkpointer=checkpointer)
```

Async version (recommended for async graphs):
```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

async with AsyncConnectionPool(DB_URI) as pool:
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()
    graph = builder.compile(checkpointer=checkpointer)
```

- Full ACID durability.
- Supports connection pooling.
- Run `checkpointer.setup()` once to create the required tables.

---

### 5.4 thread_id: Session Isolation

Every invocation must pass a `thread_id` via config. All checkpoints for the same `thread_id` form one conversation/session.

```python
config = {"configurable": {"thread_id": "user-abc-session-1"}}

# First turn
result = graph.invoke({"messages": [HumanMessage("What is LangGraph?")]}, config=config)

# Second turn — LangGraph loads the checkpoint and continues from where it left off
result = graph.invoke({"messages": [HumanMessage("Can you give an example?")]}, config=config)
```

Use a **stable, unique string** for `thread_id`:
- User ID + session ID: `"user_123_session_456"`
- UUID: `str(uuid.uuid4())`
- Anything that uniquely identifies this conversation thread.

---

### 5.5 Time-Travel: get_state_history()

```python
config = {"configurable": {"thread_id": "my-thread"}}

# Get all past checkpoints for this thread
history = list(graph.get_state_history(config))

for snapshot in history:
    print(snapshot.config)   # checkpoint config (includes checkpoint_id)
    print(snapshot.values)   # state at that point
    print(snapshot.next)     # which nodes were about to run

# Jump back to a specific checkpoint
past_config = history[2].config  # e.g., 3 steps ago
result = graph.invoke(None, config=past_config)  # replay from there
```

Get current state without re-running:
```python
current_state = graph.get_state(config)
print(current_state.values)
```

Update state between runs (useful during HITL or debugging):
```python
graph.update_state(config, {"answer": "corrected answer"})
```

---

## Phase 6: Interrupts and Human-in-the-Loop

### Concept: Why Interrupts?

Interrupts pause graph execution at a specific point and wait for external input. LangGraph supports two flavors:

1. **Static interrupts** — declared at compile time: `interrupt_before=["node_name"]`
2. **Dynamic interrupts** — triggered at runtime inside a node: `interrupt("payload")`

Both require a **checkpointer** to work (state must be saved while waiting).

---

### 6.1 Static Interrupts: interrupt_before

```python
graph = builder.compile(
    checkpointer=MemorySaver(),
    interrupt_before=["human_review"],  # pause BEFORE this node runs
    # interrupt_after=["risky_action"],  # pause AFTER this node runs
)
```

After the graph pauses, inspect state and resume:

```python
config = {"configurable": {"thread_id": "t1"}}

# Run — pauses before "human_review"
graph.invoke({"messages": [HumanMessage("Do something risky")]}, config=config)

# Inspect what the graph was about to do
state = graph.get_state(config)
print(state.values)

# Optionally edit state before resuming
graph.update_state(config, {"approved": True})

# Resume — pass None as input to continue from checkpoint
result = graph.invoke(None, config=config)
```

---

### 6.2 Dynamic Interrupts: interrupt() Inside a Node

The `interrupt()` function is the LangGraph 1.2+ way to pause mid-node and request input. Its argument is the payload shown to the human.

```python
from langgraph.types import interrupt

def human_review_node(state: State) -> dict:
    # Pause and send this payload to the caller
    human_decision = interrupt({
        "question": "Do you approve this action?",
        "draft": state["draft_answer"],
        "tool_calls": state["messages"][-1].tool_calls,
    })
    # human_decision is whatever the caller passes to Command(resume=...)
    if human_decision["approved"]:
        return {"approved": True, "final_answer": human_decision.get("edited_answer", state["draft_answer"])}
    else:
        return {"approved": False, "final_answer": None}
```

The `interrupt()` call raises a special exception internally; the node is **re-executed from the top** when resumed. Design nodes to be idempotent up to the `interrupt()` call.

---

### 6.3 Resuming with Command(resume=...)

```python
from langgraph.types import Command

config = {"configurable": {"thread_id": "review-session-1"}}

# Initial run — graph pauses at interrupt()
stream = graph.stream_events({"messages": [HumanMessage("Generate a report")]},
                              config=config, version="v3")
_ = stream.output  # drive the stream; pauses at interrupt

# Check what the interrupt payload was
print(stream.interrupts)
# > (Interrupt(value={'question': 'Do you approve this action?', ...}),)

# Human reviews and provides input
human_response = Command(resume={"approved": True, "edited_answer": "Final approved text"})

# Resume
resumed = graph.stream_events(human_response, config=config, version="v3")
final_state = resumed.output
```

---

### 6.4 Multi-Turn Interrupt Loop

For UIs that need to stream tokens AND handle interrupts in a loop:

```python
from langgraph.types import Command

stream_input = {"messages": [HumanMessage(content=user_input)]}
config = {"configurable": {"thread_id": thread_id}}

while True:
    stream = graph.stream_events(stream_input, config=config, version="v3")

    # Stream tokens as they arrive
    for message in stream.messages:
        for token in message.text:
            print(token, end="", flush=True)

    # Check if paused for human input
    if not stream.interrupted:
        final_state = stream.output
        break  # graph completed

    # Handle interrupt
    interrupt_payload = stream.interrupts[0].value
    user_response = input(f"\nHuman input needed: {interrupt_payload}\n> ")
    stream_input = Command(resume=user_response)
```

---

### 6.5 State Editing During Interrupt

Use `graph.update_state()` between the pause and resume to modify state:

```python
# Graph paused — edit state before resuming
graph.update_state(
    config,
    {"draft_answer": "Edited by human reviewer"},
    as_node="human_review_node"  # attribute the update to this node
)

# Resume from the edited state
result = graph.invoke(None, config=config)
```

---

### 6.6 Common HITL Use Cases

| Use Case | Pattern |
|---|---|
| Approve/reject an action | `interrupt({"action": ..., "question": "Approve?"})` → resume with `True/False` |
| Edit LLM output | `interrupt({"draft": state["draft"]})` → resume with corrected text |
| Fact-check a claim | `interrupt({"claim": ..., "sources": ...})` → resume with `{"verified": True}` |
| Request missing info | `interrupt({"missing": "customer ID"})` → resume with the value |
| Step-by-step debugging | `interrupt_before=["every_node"]` during development |

---

## Phase 7: Streaming

### Concept: Why Streaming?

Streaming lets you observe graph execution **as it happens** — token by token, node by node — rather than waiting for the full result. Essential for responsive UIs.

---

### 7.1 stream_mode Options

LangGraph supports four stream modes (can be combined):

| Mode | What you get |
|---|---|
| `"values"` | Full state snapshot after each node |
| `"updates"` | Only the changed keys from each node |
| `"messages"` | LLM token chunks with metadata |
| `"custom"` | Arbitrary data emitted by nodes/tools |

---

### 7.2 stream_mode="values" — Full State Snapshots

```python
for chunk in graph.stream(
    {"messages": [HumanMessage("Tell me a joke")]},
    stream_mode="values",
):
    # chunk is the full state after each node completes
    print(chunk["messages"][-1].content)
```

---

### 7.3 stream_mode="updates" — Node Diffs

```python
for chunk in graph.stream(
    {"messages": [HumanMessage("Tell me a joke")]},
    stream_mode="updates",
):
    # chunk is {node_name: {changed_keys: changed_values}}
    for node_name, state_update in chunk.items():
        print(f"Node '{node_name}' updated: {state_update}")
```

---

### 7.4 stream_mode="messages" — LLM Token Streaming

```python
for chunk in graph.stream(
    {"topic": "ice cream"},
    stream_mode="messages",
    version="v2",
):
    if chunk["type"] == "messages":
        message_chunk, metadata = chunk["data"]
        if message_chunk.content:
            print(message_chunk.content, end="", flush=True)
```

---

### 7.5 Combining Stream Modes

```python
for part in graph.stream(
    {"messages": [HumanMessage("Analyze this data")]},
    stream_mode=["updates", "messages", "custom"],
    version="v2",
):
    if part["type"] == "updates":
        for node, update in part["data"].items():
            print(f"[{node}] updated")
    elif part["type"] == "messages":
        msg, meta = part["data"]
        print(msg.content, end="", flush=True)
    elif part["type"] == "custom":
        print(f"\nProgress: {part['data']}")
```

---

### 7.6 Custom Events from Nodes and Tools

Nodes and tools can emit arbitrary data via `get_stream_writer()`:

```python
from langgraph.config import get_stream_writer

async def research_node(state: State) -> dict:
    writer = get_stream_writer()

    writer({"status": "searching", "query": state["query"]})
    results = await search(state["query"])

    writer({"status": "found", "count": len(results)})
    return {"search_results": results}
```

Consume on the caller side with `stream_mode="custom"`.

---

### 7.7 Async Streaming

```python
async for chunk in graph.astream(
    {"messages": [HumanMessage("Hello")]},
    stream_mode="updates",
):
    for node, update in chunk.items():
        print(f"[{node}]: {update}")
```

---

### 7.8 astream_events — Fine-Grained Event Stream

`astream_events` gives you every internal LangChain event (LLM starts, ends, tool starts, ends, chain steps):

```python
async for event in graph.astream_events(
    {"messages": [HumanMessage("Hello")]},
    version="v2",
):
    kind = event["event"]
    if kind == "on_chat_model_stream":
        chunk = event["data"]["chunk"]
        print(chunk.content, end="", flush=True)
    elif kind == "on_tool_start":
        print(f"\n[Tool] Calling: {event['name']}")
    elif kind == "on_tool_end":
        print(f"[Tool] Result: {event['data']['output']}")
```

Use `astream_events` when you need sub-node granularity (e.g., stream tokens from an LLM inside a tool).

---

## Phase 8: Subgraphs

### Concept: When to Use Subgraphs

Subgraphs encapsulate a complete `StateGraph` inside a parent graph as a single node. Use them when:
- You want to **reuse** a graph component across multiple parent graphs.
- A sub-task is complex enough to deserve its own state, nodes, and edges.
- You want to enforce **isolation** — the subgraph has its own state schema.
- You're building **multi-agent systems** where each agent is a graph.

---

### 8.1 Defining and Wiring a Subgraph

```python
from langgraph.graph import StateGraph, START, END
from typing_extensions import TypedDict
from typing import Annotated
import operator

# --- Subgraph ---
class SubgraphState(TypedDict):
    query: str
    results: Annotated[list[str], operator.add]

async def search_node(state: SubgraphState) -> dict:
    results = await do_search(state["query"])
    return {"results": results}

sub_builder = StateGraph(SubgraphState)
sub_builder.add_node("search", search_node)
sub_builder.add_edge(START, "search")
sub_builder.add_edge("search", END)
subgraph = sub_builder.compile()  # compile without checkpointer (parent handles it)

# --- Parent Graph ---
class ParentState(TypedDict):
    query: str
    results: Annotated[list[str], operator.add]  # shared channel name
    final_answer: str

parent_builder = StateGraph(ParentState)
parent_builder.add_node("research", subgraph)   # subgraph used as a node
parent_builder.add_node("synthesize", synthesize_node)
parent_builder.add_edge(START, "research")
parent_builder.add_edge("research", "synthesize")
parent_builder.add_edge("synthesize", END)

graph = parent_builder.compile(checkpointer=MemorySaver())
```

**State channel mapping:** LangGraph automatically maps channels with the same name between parent and subgraph. Here `query` and `results` exist in both — they are shared automatically.

---

### 8.2 Subgraph Routing to Parent with Command.PARENT

A subgraph node can route to a **parent graph node** using `graph=Command.PARENT`:

```python
from langgraph.types import Command
import operator
from typing import Annotated
from typing_extensions import TypedDict

class SharedState(TypedDict):
    foo: Annotated[str, operator.add]

def subgraph_node_a(state: SharedState) -> Command:
    return Command(
        update={"foo": "from_subgraph"},
        goto="parent_node_b",
        graph=Command.PARENT,  # route to parent graph, not subgraph
    )
```

---

### 8.3 Interrupt Propagation from Subgraph

Interrupts inside a subgraph bubble up to the parent caller automatically. The `thread_id` config must be set at the top-level graph — the checkpointer is shared across the parent/subgraph boundary.

```python
# An interrupt() inside a subgraph node
# is surfaced just like any other interrupt to the caller
stream = graph.stream_events(input, config=config, version="v3")
if stream.interrupted:
    # This interrupt may have come from deep inside a subgraph
    print(stream.interrupts[0].value)
```

---

## Phase 9: The Send API (Dynamic Fan-Out)

### Concept: When to Use Send

The `Send` API enables **dynamic parallelism**: generate N work items at runtime and process them in parallel, then reduce the results. Use `Send` when:
- You don't know the number of parallel tasks at graph-build time.
- You want map-reduce: fan out over a list, collect results.
- Each item needs its own isolated state.

---

### 9.1 Basic Send Fan-Out

`Send(node_name, state_dict)` schedules a node invocation with a specific input state. Return a list of `Send` objects from a conditional edge function.

```python
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from typing import Annotated
import operator
from typing_extensions import TypedDict

class OverallState(TypedDict):
    subjects: list[str]
    jokes: Annotated[list[str], operator.add]  # reducer collects all results
    best_joke: str

class JokeState(TypedDict):
    subject: str

def generate_topics(state: OverallState) -> dict:
    return {"subjects": ["cats", "dogs", "penguins"]}

def generate_joke(state: JokeState) -> dict:
    # This runs in parallel for each subject
    joke = call_llm(f"Write a joke about {state['subject']}")
    return {"jokes": [joke]}

def fan_out_to_jokes(state: OverallState) -> list[Send]:
    # Return one Send per subject — all run in parallel
    return [Send("generate_joke", {"subject": s}) for s in state["subjects"]]

def pick_best_joke(state: OverallState) -> dict:
    best = max(state["jokes"], key=len)  # simplistic selector
    return {"best_joke": best}

builder = StateGraph(OverallState)
builder.add_node("generate_topics", generate_topics)
builder.add_node("generate_joke", generate_joke)   # will run N times in parallel
builder.add_node("pick_best", pick_best_joke)

builder.add_edge(START, "generate_topics")
builder.add_conditional_edges("generate_topics", fan_out_to_jokes, ["generate_joke"])
builder.add_edge("generate_joke", "pick_best")
builder.add_edge("pick_best", END)

graph = builder.compile()
```

**Key points:**
- `generate_joke` receives a `JokeState` (just `{"subject": "cats"}`), not the full `OverallState`.
- Each parallel invocation returns `{"jokes": ["..."]}`.
- The `operator.add` reducer on `jokes` **concatenates** all results into one list.
- `pick_best` runs only after **all** parallel joke nodes complete.

---

### 9.2 Send vs Parallel Nodes

| | Send | Parallel nodes (same step) |
|---|---|---|
| Number of instances | Dynamic (determined at runtime) | Fixed (set at graph-build time) |
| State per instance | Isolated (custom dict per Send) | Shared state |
| When to use | Map-reduce over variable-length lists | Fixed parallel branches |

---

### 9.3 Mixed Conditional Edge: Send + String

A conditional edge function can return **either** a list of `Send` objects **or** a string node name:

```python
def smart_router(state: OverallState) -> str | list[Send]:
    if len(state["subjects"]) == 0:
        return "__end__"  # nothing to process
    elif len(state["subjects"]) == 1:
        return "generate_joke"  # single item: normal edge
    else:
        return [Send("generate_joke", {"subject": s}) for s in state["subjects"]]
```

---

## Phase 10: Full Scaffold Template

This is the complete boilerplate for a production-ready LangGraph agent. Fill in the `# TODO` sections.

```python
"""
Graph: <name>
Purpose: <one-line description>
"""

import operator
from typing import Annotated
from typing_extensions import TypedDict, Literal

from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt


# ─── 1. STATE SCHEMA ──────────────────────────────────────────────────────────

class State(MessagesState):
    # Add fields beyond messages here
    # Example:
    # search_results: Annotated[list[str], operator.add]
    # final_answer: str
    pass


# ─── 2. TOOLS ────────────────────────────────────────────────────────────────

@tool
def search(query: str) -> str:
    """Search the web for information."""
    # TODO: implement
    return f"Search results for: {query}"

tools = [search]
llm = ChatOpenAI(model="gpt-4o", temperature=0)
llm_with_tools = llm.bind_tools(tools)


# ─── 3. NODES ────────────────────────────────────────────────────────────────

async def agent(state: State) -> dict:
    """Main agent node: calls LLM with tools."""
    response = await llm_with_tools.ainvoke(state["messages"])
    return {"messages": [response]}


# Optional: human review node
def human_review(state: State) -> dict:
    """Pause for human approval of the last tool call."""
    last_message = state["messages"][-1]
    decision = interrupt({
        "question": "Approve this action?",
        "tool_calls": last_message.tool_calls if hasattr(last_message, "tool_calls") else [],
    })
    if not decision.get("approved", False):
        # Route back to agent with rejection feedback
        return {"messages": [HumanMessage(content="Action rejected. Please try a different approach.")]}
    return {}  # approved — continue to tool execution


# ─── 4. GRAPH BUILDER ────────────────────────────────────────────────────────

builder = StateGraph(State)

builder.add_node("agent", agent)
builder.add_node("tools", ToolNode(tools))
# builder.add_node("human_review", human_review)  # uncomment for HITL

# Edges
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", tools_condition)  # → "tools" or END
builder.add_edge("tools", "agent")  # loop back after tool execution


# ─── 5. COMPILATION ──────────────────────────────────────────────────────────

# Dev: in-memory checkpointer
checkpointer = MemorySaver()

# Prod: use PostgresSaver (see Phase 5.3)
# from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
# checkpointer = AsyncPostgresSaver(pool)

graph = builder.compile(
    checkpointer=checkpointer,
    # interrupt_before=["human_review"],  # static HITL
)


# ─── 6. INVOCATION PATTERNS ──────────────────────────────────────────────────

import asyncio

async def run_sync():
    """Single invocation, full result."""
    config = {"configurable": {"thread_id": "session-1"}}
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="What is the capital of France?")]},
        config=config,
    )
    print(result["messages"][-1].content)


async def run_streaming():
    """Stream token-by-token."""
    config = {"configurable": {"thread_id": "session-2"}}
    async for chunk in graph.astream(
        {"messages": [HumanMessage(content="Search for LangGraph tutorials")]},
        config=config,
        stream_mode="updates",
    ):
        for node, update in chunk.items():
            if "messages" in update:
                last = update["messages"][-1]
                print(f"[{node}] {last.content}")


async def run_with_hitl():
    """Human-in-the-loop: pause, review, resume."""
    from langgraph.types import Command

    config = {"configurable": {"thread_id": "session-hitl-1"}}
    initial_input = {"messages": [HumanMessage(content="Delete all temporary files")]}

    # First run — may pause at interrupt()
    stream = graph.stream_events(initial_input, config=config, version="v3")
    _ = stream.output

    if stream.interrupted:
        payload = stream.interrupts[0].value
        print(f"Interrupt: {payload}")
        user_says = input("Approve? (y/n): ")
        approved = user_says.lower() == "y"

        resumed = graph.stream_events(
            Command(resume={"approved": approved}),
            config=config,
            version="v3",
        )
        final = resumed.output
        print(final["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(run_streaming())
```

---

## Quick Reference

### Imports Cheatsheet

```python
# Core graph
from langgraph.graph import StateGraph, START, END, MessagesState

# State
from langgraph.graph.message import add_messages
from typing import Annotated
from typing_extensions import TypedDict
import operator

# Nodes / routing
from langgraph.types import Command, interrupt, Send
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.config import get_stream_writer

# Checkpointing
from langgraph.checkpoint.memory import MemorySaver           # dev
from langgraph.checkpoint.sqlite import SqliteSaver            # dev/light prod
from langgraph.checkpoint.postgres import PostgresSaver        # prod
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # async prod

# Tools
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
```

### Decision Tree

```
Need persistence across restarts?
  No  → MemorySaver
  Yes → SqliteSaver (single-server) or PostgresSaver (distributed/prod)

Need human approval mid-graph?
  At a specific node boundary → interrupt_before=["node_name"]
  Inside a node with custom payload → interrupt("message")
  Resume with → Command(resume=value)

Need parallel processing?
  Fixed branches → multiple edges from START, or parallel nodes
  Variable-length list → Send API (fan-out) + operator.add reducer (reduce)

Node needs to route AND update state?
  → Return Command(update={...}, goto="node_name")

Need to stream tokens?
  → stream_mode="messages" or astream_events(version="v2")

Need fine-grained internal events (tool starts, sub-LLM calls)?
  → astream_events(version="v2")

Which API to use for a new graph?
  Function-shaped workflow (sequential + parallel steps, no dynamic routing) → Functional API (@entrypoint / @task)
  Complex conditional routing, dynamic edges, Send fan-out             → StateGraph
  Need both (e.g., an agent inside a pipeline)                         → call compiled StateGraph as a task inside @entrypoint

Want parallelism in Functional API?
  Fixed set of tasks → asyncio.gather(*[t(x) for x in items])
  Gather futures returned by @task → [f.result() for f in futures]

Want HITL in Functional API?
  → interrupt() inside an @task body — identical semantics to StateGraph

Migrating StateGraph → Functional API?
  Each node → @task
  Graph edges → sequential await / asyncio.gather in @entrypoint
  State schema → plain function arguments and return values
  Checkpointer → passed to @entrypoint(checkpointer=...)
```

---

## Phase 11: Functional API (@entrypoint / @task)

### Concept: What is the Functional API?

LangGraph 1.2 introduced a second way to build graphs alongside `StateGraph`: the **Functional API**. Instead of declaring state, nodes, and edges explicitly, you write **ordinary Python functions** and decorate them.

Two decorators are the entire surface area:

| Decorator | Purpose |
|---|---|
| `@task` | Marks a function as a **checkpointed unit of work**. Its result is saved automatically. |
| `@entrypoint` | Marks an async (or sync) function as the **graph entry point**. Wires checkpointing, streaming, HITL, and invocation to it. |

The runtime behaviour is **identical** to `StateGraph`: same checkpointing semantics, same streaming API (`stream`, `astream`, `astream_events`), same HITL via `interrupt()`, same `thread_id`-based session isolation.

---

### 11.1 @task — Checkpointed Units of Work

`@task` wraps a function so that:

1. Its **return value is checkpointed** after it completes.
2. On replay/resume, if the checkpoint already contains the result, the function body is **skipped** — the saved value is returned directly.
3. It returns a **Future-like object** immediately; call `.result()` to get the value.

```python
from langgraph.func import task

@task
def fetch_page(url: str) -> str:
    """Fetch a URL. Result is checkpointed — on replay this is skipped."""
    import requests
    return requests.get(url).text[:500]
```

**Calling a `@task`:**

```python
future = fetch_page("https://example.com")  # starts task, returns future
text = future.result()                        # blocks until complete
```

**Async `@task`:**

```python
from langgraph.func import task

@task
async def call_llm(prompt: str) -> str:
    response = await model.ainvoke([{"role": "user", "content": prompt}])
    return response.content
```

**Key rules:**
- `@task` functions can be sync or async.
- They must be **called from inside an `@entrypoint`** (or from another `@task`).
- They must accept and return **JSON-serialisable** values (checkpointer stores them as JSON).
- They are **idempotent by design** — if the checkpoint exists the body never re-runs.

---

### 11.2 @entrypoint — The Graph Entry Point

`@entrypoint` declares the top-level function that replaces the entire `StateGraph` build/compile pipeline.

```python
from langgraph.func import entrypoint
from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()

@entrypoint(checkpointer=checkpointer)
async def pipeline(inputs: dict) -> str:
    """This function IS the graph."""
    result = await call_llm(inputs["prompt"])  # @task call
    return result.result()
```

**What `@entrypoint` does:**
- Compiles the function into a runnable graph object.
- Attaches the checkpointer.
- Exposes `.invoke()`, `.ainvoke()`, `.stream()`, `.astream()`, `.astream_events()` — exactly the same interface as a compiled `StateGraph`.
- Handles `thread_id`-based session isolation automatically.

**Invocation is identical to StateGraph:**

```python
config = {"configurable": {"thread_id": "session-1"}}

# Sync
result = pipeline.invoke({"prompt": "Hello"}, config=config)

# Async
result = await pipeline.ainvoke({"prompt": "Hello"}, config=config)

# Streaming
async for chunk in pipeline.astream({"prompt": "Hello"}, config=config, stream_mode="updates"):
    print(chunk)
```

---

### 11.3 Parallel Tasks with asyncio.gather()

Because `@task` returns a future, you can fan out in parallel using `asyncio.gather()` — or simply by collecting futures and calling `.result()` on each.

**Pattern A — asyncio.gather (true async parallelism):**

```python
import asyncio
from langgraph.func import entrypoint, task
from langgraph.checkpoint.memory import InMemorySaver
from langchain.chat_models import init_chat_model

model = init_chat_model("gpt-4o")

@task
async def research_topic(topic: str) -> str:
    """Research a single topic with an LLM."""
    response = await model.ainvoke([
        {"role": "system", "content": "You are a research assistant. Be concise."},
        {"role": "user", "content": f"Summarise key facts about: {topic}"},
    ])
    return response.content

@task
async def write_section(topic: str, research: str) -> str:
    """Write a report section from research notes."""
    response = await model.ainvoke([
        {"role": "system", "content": "You are a technical writer."},
        {"role": "user", "content": f"Write a report section on '{topic}'.\n\nResearch notes:\n{research}"},
    ])
    return response.content

checkpointer = InMemorySaver()

@entrypoint(checkpointer=checkpointer)
async def research_pipeline(inputs: dict) -> dict:
    """
    Fan out research tasks in parallel, then write sections in parallel.
    inputs = {"topics": ["quantum computing", "photonics", "neuromorphic chips"]}
    """
    topics: list[str] = inputs["topics"]

    # Phase 1: research all topics in parallel
    research_futures = await asyncio.gather(
        *[research_topic(t) for t in topics]
    )
    research_results = [f.result() for f in research_futures]

    # Phase 2: write sections in parallel (each section depends only on its own research)
    write_futures = await asyncio.gather(
        *[write_section(topic, research)
          for topic, research in zip(topics, research_results)]
    )
    sections = [f.result() for f in write_futures]

    return {
        "topics": topics,
        "sections": sections,
        "report": "\n\n".join(
            f"## {topic}\n\n{section}"
            for topic, section in zip(topics, sections)
        ),
    }
```

**Pattern B — sequential futures (simpler, no asyncio.gather overhead):**

```python
@entrypoint(checkpointer=checkpointer)
async def sequential_pipeline(inputs: dict) -> str:
    # Each .result() call awaits the task before starting the next
    research = research_topic(inputs["topic"]).result()
    article = write_section(inputs["topic"], research).result()
    return article
```

**Pattern C — fan-out over a list (map-reduce equivalent):**

```python
@entrypoint(checkpointer=checkpointer)
async def summarise_many(inputs: dict) -> list[str]:
    futures = [summarise(doc) for doc in inputs["documents"]]  # all start immediately
    return [f.result() for f in futures]                        # wait for all
```

**Checkpoint behaviour across all patterns:**
- Each `@task` result is checkpointed individually.
- On resume after failure, only the tasks that did not yet complete are re-run.
- Tasks that already completed return their cached value instantly.

---

### 11.4 Runtime Equivalence to StateGraph

The Functional API and StateGraph are two **syntaxes over the same runtime**. They share:

| Feature | StateGraph | Functional API |
|---|---|---|
| Checkpointing | `compile(checkpointer=...)` | `@entrypoint(checkpointer=...)` |
| Session isolation | `thread_id` in config | same |
| Streaming | `.stream()` / `.astream()` / `.astream_events()` | same |
| Human-in-the-loop | `interrupt()` inside a node | `interrupt()` inside a `@task` |
| Resume | `Command(resume=value)` | same |
| Time travel | `get_state_history()` | same |
| State update | `update_state()` | same |
| LangSmith tracing | automatic | automatic |

You can swap a compiled `StateGraph` with a Functional API `@entrypoint` and the caller code does not change at all.

---

### 11.5 When Functional API > StateGraph

Prefer the Functional API when your workflow is **naturally function-shaped**:

- Linear pipelines: A → B → C with no dynamic routing.
- Parallel fan-out where all branches are known at call time (use `asyncio.gather`).
- Workflows where the "graph" is clearest written as sequential Python.
- Rapid prototyping — no boilerplate `TypedDict`, `add_edge`, `compile`.
- When you want the checkpointing / HITL / streaming benefits of LangGraph with minimal LangGraph-specific syntax.

```python
# Functional API: the logic is obvious at a glance
@entrypoint(checkpointer=checkpointer)
async def pipeline(inputs: dict) -> str:
    data   = fetch_data(inputs["url"]).result()
    parsed = parse(data).result()
    answer = generate_answer(parsed, inputs["question"]).result()
    return answer
```

vs the equivalent StateGraph which requires defining State, three nodes, three edges, compilation.

---

### 11.6 When StateGraph > Functional API

Prefer `StateGraph` when:

- You need **complex conditional routing**: multiple branches determined by state at runtime (many `add_conditional_edges` calls with `Literal` type annotations that feed graph visualisation).
- You need the **Send API** for truly dynamic fan-out (unknown number of parallel items, each with isolated state, reducing back into shared state via reducers).
- You have **complex state merge logic** using custom reducers (`Annotated[list, operator.add]` etc.) that would be awkward to manage as plain return values.
- You want **graph visualisation** (`graph.get_graph().draw_mermaid()`) showing every branch — Functional API graphs render as a single node.
- You need **interrupt_before / interrupt_after** compile-time static interrupts on specific nodes.
- You are integrating with LangGraph Studio or LangGraph Cloud tooling that inspects the graph schema.

```
Decision rule:
  "Does my routing logic look like Python if/elif/else?"  → Functional API
  "Does my routing logic need graph edges + visualisation?" → StateGraph
```

---

### 11.7 Mixing Both: Call a StateGraph as a Task

You can call a **compiled StateGraph as a `@task`** inside an `@entrypoint`. This lets you compose Functional API pipelines that delegate complex sub-tasks to StateGraph agents.

```python
import asyncio
from langgraph.func import entrypoint, task
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain.chat_models import init_chat_model

# ── 1. Build a StateGraph sub-agent ──────────────────────────────────────────

model = init_chat_model("gpt-4o")

@tool
def web_search(query: str) -> str:
    """Search the web."""
    return f"[Search results for: {query}]"

tools = [web_search]

async def agent_node(state: MessagesState) -> dict:
    response = await model.bind_tools(tools).ainvoke(state["messages"])
    return {"messages": [response]}

sub_builder = StateGraph(MessagesState)
sub_builder.add_node("agent", agent_node)
sub_builder.add_node("tools", ToolNode(tools))
sub_builder.add_edge(START, "agent")
sub_builder.add_conditional_edges("agent", tools_condition)
sub_builder.add_edge("tools", "agent")
# Note: compile the subgraph WITHOUT a checkpointer — parent handles persistence
research_agent = sub_builder.compile()

# ── 2. Wrap the compiled graph as a @task ────────────────────────────────────

@task
async def run_research_agent(question: str) -> str:
    """Invoke the StateGraph sub-agent and return its final answer."""
    result = await research_agent.ainvoke(
        {"messages": [HumanMessage(content=question)]}
    )
    return result["messages"][-1].content

@task
async def write_report(topic: str, research_a: str, research_b: str) -> str:
    """Synthesise two research results into a final report."""
    response = await model.ainvoke([
        {"role": "system", "content": "You are a technical writer. Synthesise the research into a concise report."},
        {"role": "user", "content": f"Topic: {topic}\n\nResearch A:\n{research_a}\n\nResearch B:\n{research_b}"},
    ])
    return response.content

# ── 3. Compose everything in @entrypoint ────────────────────────────────────

checkpointer = InMemorySaver()

@entrypoint(checkpointer=checkpointer)
async def hybrid_pipeline(inputs: dict) -> str:
    """
    Functional API outer pipeline + two StateGraph sub-agents running in parallel.
    inputs = {"topic": "...", "question_a": "...", "question_b": "..."}
    """
    # Fan out: two research agents run in parallel
    future_a, future_b = await asyncio.gather(
        run_research_agent(inputs["question_a"]),
        run_research_agent(inputs["question_b"]),
    )
    research_a = future_a.result()
    research_b = future_b.result()

    # Reduce: synthesise into a single report
    report = write_report(inputs["topic"], research_a, research_b).result()
    return report

# ── 4. Invoke ────────────────────────────────────────────────────────────────

async def main():
    config = {"configurable": {"thread_id": "hybrid-1"}}
    result = await hybrid_pipeline.ainvoke(
        {
            "topic": "The future of photonic computing",
            "question_a": "What are the latest advances in silicon photonics?",
            "question_b": "What are the main barriers to photonic CPU adoption?",
        },
        config=config,
    )
    print(result)
```

**Key points:**
- The sub-`StateGraph` is compiled **without** a checkpointer — the parent `@entrypoint`'s checkpointer handles all persistence.
- `@task` wrapping the subgraph invocation means the sub-agent result is checkpointed. On replay, the sub-agent is not re-invoked.
- The outer `@entrypoint` still supports full streaming, HITL, and time-travel.

---

### 11.8 Migration: StateGraph → Functional API

When a StateGraph is straightforward enough to migrate, the mapping is mechanical:

| StateGraph concept | Functional API equivalent |
|---|---|
| `TypedDict` state class | Function arguments + return values |
| Node function | `@task` decorated function |
| Direct edge A → B | Sequential: `b(a(x).result())` |
| Parallel edges A → B, A → C | `asyncio.gather(b(x), c(x))` |
| `compile(checkpointer=...)` | `@entrypoint(checkpointer=...)` |
| `graph.invoke(input, config)` | `workflow.invoke(input, config)` |
| Custom reducer on state field | Explicit merge logic in `@entrypoint` body |
| `interrupt_before=["node"]` | `interrupt()` call at the top of the `@task` |

**Before (StateGraph):**

```python
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

class State(TypedDict):
    query: str
    research: str
    report: str

async def research_node(state: State) -> dict:
    result = await do_research(state["query"])
    return {"research": result}

async def write_node(state: State) -> dict:
    result = await write_report(state["query"], state["research"])
    return {"report": result}

builder = StateGraph(State)
builder.add_node("research", research_node)
builder.add_node("write", write_node)
builder.add_edge(START, "research")
builder.add_edge("research", "write")
builder.add_edge("write", END)

graph = builder.compile(checkpointer=InMemorySaver())
result = graph.invoke({"query": "photonics"}, config={"configurable": {"thread_id": "1"}})
```

**After (Functional API — same runtime, less boilerplate):**

```python
from langgraph.func import entrypoint, task
from langgraph.checkpoint.memory import InMemorySaver

@task
async def research(query: str) -> str:
    return await do_research(query)

@task
async def write(query: str, research_notes: str) -> str:
    return await write_report(query, research_notes)

@entrypoint(checkpointer=InMemorySaver())
async def pipeline(inputs: dict) -> str:
    notes  = research(inputs["query"]).result()
    report = write(inputs["query"], notes).result()
    return report

result = pipeline.invoke({"query": "photonics"}, config={"configurable": {"thread_id": "1"}})
```

**What did not change:**
- The config shape (`thread_id`).
- The invocation methods.
- Checkpointing, streaming, HITL semantics.
- LangSmith tracing.

**What changed:**
- Removed `TypedDict` state class.
- Removed `StateGraph`, `add_node`, `add_edge`, `compile`.
- Each node became a `@task`.
- Data flows explicitly as function arguments, not through a shared state dict.

---

### 11.9 interrupt() Inside @task for HITL

`interrupt()` works **identically** inside a `@task` body as inside a StateGraph node. The same rules apply:

- Requires a checkpointer on the parent `@entrypoint`.
- The task body is **re-executed from the top** on resume — design it to be idempotent before the `interrupt()` call.
- Resume with `Command(resume=value)` passed to the same invocation method.

```python
from langgraph.func import entrypoint, task
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import InMemorySaver
from langchain.chat_models import init_chat_model

model = init_chat_model("gpt-4o")

@task
async def draft_email(context: str) -> str:
    """Draft an email with an LLM."""
    response = await model.ainvoke([
        {"role": "system", "content": "Draft a professional email reply."},
        {"role": "user", "content": context},
    ])
    return response.content

@task
def human_review(draft: str) -> str:
    """
    Pause for human review. The task is re-executed on resume;
    everything before interrupt() is idempotent (no side effects here).
    """
    decision = interrupt({
        "question": "Approve this email draft?",
        "draft": draft,
    })
    # decision is whatever was passed to Command(resume=...)
    if decision.get("approved"):
        return decision.get("edited_draft", draft)  # human may have edited
    else:
        return ""  # signal rejection

@task
async def send_email(recipient: str, body: str) -> str:
    """Send the approved email."""
    # ... actual send logic ...
    return f"Email sent to {recipient}"

checkpointer = InMemorySaver()

@entrypoint(checkpointer=checkpointer)
async def email_workflow(inputs: dict) -> str:
    draft    = draft_email(inputs["context"]).result()
    approved = human_review(draft).result()          # pauses here for HITL

    if not approved:
        return "Email cancelled by reviewer."

    confirmation = send_email(inputs["recipient"], approved).result()
    return confirmation
```

**Invocation with HITL:**

```python
import asyncio

config = {"configurable": {"thread_id": "email-session-1"}}
inputs = {
    "context": "Customer complained about double billing on invoice #4421.",
    "recipient": "customer@example.com",
}

async def run():
    # First run — pauses at interrupt() inside human_review
    stream = email_workflow.stream_events(inputs, config=config, version="v3")
    _ = stream.output

    if stream.interrupted:
        payload = stream.interrupts[0].value
        print(f"Draft to review:\n{payload['draft']}")

        # Human edits and approves
        human_response = Command(resume={
            "approved": True,
            "edited_draft": "Dear customer, we sincerely apologise for the duplicate charge...",
        })

        resumed = email_workflow.stream_events(human_response, config=config, version="v3")
        result = resumed.output
        print(result)

asyncio.run(run())
```

**interrupt() rules summary for Functional API:**

| Rule | Detail |
|---|---|
| Where to call | Inside any `@task` body (not in `@entrypoint` directly) |
| Checkpointer required | Yes — on `@entrypoint(checkpointer=...)` |
| Task re-execution on resume | Yes — entire task body re-runs; code before `interrupt()` must be side-effect-free or idempotent |
| Resume mechanism | `Command(resume=value)` passed to any invocation method |
| Multiple interrupts | Supported — each `interrupt()` call in sequence creates one pause per invocation |

---

### 11.10 Complete Working Example: Research Pipeline

A fully runnable research pipeline demonstrating:
- `@task` for modular work units
- Parallel research with `asyncio.gather`
- Sequential write step
- HITL approval before final publish
- Checkpointing and resume

```python
"""
Research pipeline — Functional API complete example.
Demonstrates: @task, @entrypoint, asyncio.gather, interrupt(), Command(resume=...)
"""

import asyncio
import uuid

from langchain.chat_models import init_chat_model
from langgraph.func import entrypoint, task
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt, Command

# ── Model ─────────────────────────────────────────────────────────────────────

model = init_chat_model("gpt-4o", temperature=0.3)

# ── Tasks ─────────────────────────────────────────────────────────────────────

@task
async def research(topic: str) -> str:
    """Research a single topic. Result is checkpointed — safe to retry."""
    response = await model.ainvoke([
        {
            "role": "system",
            "content": (
                "You are a research analyst. Provide a structured summary with: "
                "key facts, recent developments, open questions. Be concise (200 words max)."
            ),
        },
        {"role": "user", "content": f"Research topic: {topic}"},
    ])
    return response.content


@task
async def write_report(
    main_topic: str,
    subtopic_results: list[tuple[str, str]],
) -> str:
    """
    Write a cohesive report from parallel research results.
    subtopic_results = [(subtopic, research_text), ...]
    """
    research_block = "\n\n".join(
        f"### {subtopic}\n{text}"
        for subtopic, text in subtopic_results
    )
    response = await model.ainvoke([
        {
            "role": "system",
            "content": (
                "You are a technical writer. Synthesise the research notes below "
                "into a well-structured 400-word report with an executive summary, "
                "key findings, and recommendations."
            ),
        },
        {
            "role": "user",
            "content": f"Main topic: {main_topic}\n\nResearch notes:\n{research_block}",
        },
    ])
    return response.content


@task
def editorial_review(draft: str) -> str:
    """
    Pause for human editorial review before publishing.
    The task is re-executed from the top on resume; no side effects before interrupt().
    """
    decision = interrupt({
        "action": "review_report",
        "question": "Approve this report for publishing? You may edit the draft.",
        "draft": draft,
    })
    if decision.get("approved", False):
        # Human may have supplied an edited version
        return decision.get("final_draft", draft)
    raise ValueError("Report rejected by editor.")


@task
async def publish(report: str, destination: str) -> str:
    """Publish the approved report. In production: write to DB, send email, etc."""
    # Simulated publish
    return f"Published {len(report)} chars to '{destination}'."


# ── Entrypoint ────────────────────────────────────────────────────────────────

checkpointer = InMemorySaver()


@entrypoint(checkpointer=checkpointer)
async def research_pipeline(inputs: dict) -> dict:
    """
    Full research pipeline.

    inputs = {
        "main_topic": "The future of photonic computing",
        "subtopics": [
            "Silicon photonics manufacturing",
            "Optical interconnects vs copper",
            "Neuromorphic photonic chips",
        ],
        "destination": "company-blog/photonics-2025",
    }
    """
    main_topic: str  = inputs["main_topic"]
    subtopics: list  = inputs["subtopics"]
    destination: str = inputs["destination"]

    # ── Phase 1: Parallel research ────────────────────────────────────────────
    # All research tasks start simultaneously; asyncio.gather awaits all of them.
    # Each task's result is checkpointed individually.
    research_futures = await asyncio.gather(
        *[research(subtopic) for subtopic in subtopics]
    )
    research_results = [f.result() for f in research_futures]
    subtopic_data = list(zip(subtopics, research_results))

    # ── Phase 2: Write report (sequential — depends on all research) ──────────
    draft = write_report(main_topic, subtopic_data).result()

    # ── Phase 3: Human editorial review (HITL) ────────────────────────────────
    # Pauses here; resumes when Command(resume=...) is provided.
    approved_draft = editorial_review(draft).result()

    # ── Phase 4: Publish ──────────────────────────────────────────────────────
    confirmation = publish(approved_draft, destination).result()

    return {
        "report": approved_draft,
        "confirmation": confirmation,
        "subtopics_researched": subtopics,
    }


# ── Runner ────────────────────────────────────────────────────────────────────

async def main():
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    inputs = {
        "main_topic": "The future of photonic computing",
        "subtopics": [
            "Silicon photonics manufacturing",
            "Optical interconnects vs copper",
            "Neuromorphic photonic chips",
        ],
        "destination": "company-blog/photonics-2025",
    }

    print("=== Starting research pipeline ===")
    print(f"Thread: {config['configurable']['thread_id']}\n")

    # ── First run: executes until editorial_review pauses ────────────────────
    stream = research_pipeline.stream_events(inputs, config=config, version="v3")

    # Stream token output as it arrives
    async for event in stream:
        if event.get("event") == "on_chat_model_stream":
            chunk = event["data"].get("chunk")
            if chunk and chunk.content:
                print(chunk.content, end="", flush=True)

    print("\n\n=== Pipeline paused for editorial review ===")

    if stream.interrupted:
        payload = stream.interrupts[0].value
        print(f"\nDraft report to review:\n{'-'*60}")
        print(payload["draft"])
        print("-" * 60)

        # Simulate human decision (in production: get from UI/API)
        human_decision = Command(resume={
            "approved": True,
            "final_draft": payload["draft"] + "\n\n[Reviewed and approved by editorial team.]",
        })

        print("\n=== Resuming after approval ===")
        resumed = research_pipeline.stream_events(
            human_decision, config=config, version="v3"
        )
        final = resumed.output
        print(f"\nResult: {final['confirmation']}")
        print(f"Report length: {len(final['report'])} chars")
    else:
        # No interrupt — pipeline completed without HITL
        final = stream.output
        print(f"Result: {final['confirmation']}")


if __name__ == "__main__":
    asyncio.run(main())
```

**What this example demonstrates:**

| Concept | Where |
|---|---|
| `@task` for individual work units | `research`, `write_report`, `editorial_review`, `publish` |
| Async `@task` (LLM calls) | `research`, `write_report`, `publish` |
| Sync `@task` (HITL wrapper) | `editorial_review` |
| Parallel fan-out with `asyncio.gather` | Phase 1 in `research_pipeline` |
| Sequential step after fan-out | Phase 2 (`write_report` waits for all research) |
| `interrupt()` for HITL | Inside `editorial_review` |
| `Command(resume=...)` to resume | In `main()` after editorial pause |
| Checkpointing + replay | `InMemorySaver` on `@entrypoint`; swap for `AsyncPostgresSaver` in production |
| Token streaming during execution | `astream_events` loop in `main()` |

---

### 11.11 Functional API Imports Cheatsheet

```python
# Core decorators
from langgraph.func import entrypoint, task

# Same checkpointers as StateGraph
from langgraph.checkpoint.memory import InMemorySaver          # dev
from langgraph.checkpoint.sqlite import SqliteSaver             # dev/light prod
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # prod

# HITL
from langgraph.types import interrupt, Command

# Streaming (same as StateGraph)
# workflow.stream()  /  workflow.astream()  /  workflow.astream_events()

# Parallelism
import asyncio  # asyncio.gather() for parallel @task fan-out
```

---

## Teaching Notes for Skill Execution

When a user invokes this skill:

1. **Start with Phase 1 questions** — do not design anything until you understand the goal.
2. **Recommend Functional API first** — after Phase 1, ask: "Does this workflow have complex conditional routing or dynamic fan-out over variable-length lists?" If no, suggest `@entrypoint` / `@task` (Phase 11) before reaching for `StateGraph`. The Functional API is now the recommended starting point for new projects.
3. **Design state schema first (StateGraph path)** — if the user needs StateGraph, everything flows from the state schema. Get it right before touching nodes.
4. **Teach each concept as you apply it** — do not dump all concepts upfront. Show the relevant section from this SKILL.md at the moment it's needed.
5. **Scaffold incrementally** — start with a minimal working graph (2 nodes or 2 tasks, 1 edge/call), then add complexity.
6. **Always compile and show invocation** — a graph that can't be run is not done.
7. **Flag checkpointing early** — it affects whether interrupts, time-travel, and multi-turn work at all. For Functional API: `@entrypoint(checkpointer=...)`. For StateGraph: `compile(checkpointer=...)`.
8. **Show the error** — if something common goes wrong (e.g., forgetting `add_messages`, wrong `thread_id` scope, calling `interrupt()` directly in `@entrypoint` instead of inside a `@task`), name the mistake and show the fix.
9. **Mixing both APIs** — if the user has a StateGraph agent and wants to embed it in a pipeline, show Phase 11.7 (compile without checkpointer, wrap as `@task`).
10. **Migration questions** — if a user asks "should I rewrite my StateGraph?", apply the decision matrix from Phases 11.5/11.6. Only recommend migration if the graph is function-shaped and has no complex routing.
