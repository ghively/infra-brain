---
name: lc-memory
description: Use when designing or implementing memory for a LangChain or LangGraph application — choosing between conversation buffer, trimming, summarization, checkpointing, vector/entity memory, or the Store API. Use when a user asks how to make a chatbot remember things, persist state across sessions, track user facts, or share knowledge across threads.
---

# lc:memory — LangChain & LangGraph Memory Patterns

## Overview

Memory in LangChain/LangGraph splits into two orthogonal axes:

- **Scope:** within one session (short-term) vs. across sessions (long-term)
- **Shape:** full message history, compressed summary, extracted facts, or shared key-value store

Choosing wrong costs tokens, loses facts, or breaks multi-user isolation. Use the decision tree first, then follow the pattern guide.

---

## Decision Tree

```
Does the system need to remember WITHIN one conversation?
├── Yes → Does context get long (> ~4k tokens)?
│         ├── Yes, and you need exact quotes → TRIMMING  (Pattern 2)
│         ├── Yes, but gist is enough       → SUMMARY   (Pattern 3)
│         └── No (short demo/prototype)    → BUFFER    (Pattern 1)
└── No  → skip to cross-session

Does it need to remember ACROSS sessions (conversations)?
├── Yes → CHECKPOINTING (Pattern 4) — choose backend by env:
│         ├── Dev / prototype  → InMemorySaver
│         ├── Single-process   → SqliteSaver
│         └── Production       → PostgresSaver
└── No  → skip to facts

Does it need to remember SPECIFIC FACTS about users/entities?
├── Structured (profiles, attributes) → ENTITY MEMORY  (Pattern 6)
├── Unstructured (anything, searchable) → VECTOR MEMORY (Pattern 5)
└── Both                               → combine 5 + 6

Does it need SHARED knowledge across ALL users / threads?
└── Yes → STORE API  (Pattern 7)

Is it about long documents?
└── Yes → RAG (not memory — see lc:rag)
```

---

## Token Cost Reference

| Pattern | Input tokens per turn | Notes |
|---|---|---|
| Buffer (all messages) | Grows unbounded | ~750 tok/1k chars; will hit context limit |
| Trimming | Capped (your budget) | Loses oldest messages permanently |
| Summary | Small fixed overhead | ~200-400 tok for summary retrieval + recent msgs |
| Checkpointing | Same as buffer per thread | Persistence is free; context window still applies |
| Vector memory | ~50-200 tok per retrieved fact | Only pay for what you retrieve |
| Entity memory | ~100-500 tok for profiles | Grows with entities, not with turns |
| Store API | Same as vector/entity | Plus serialization overhead |

**Rule of thumb:** For GPT-4o at $2.50/M input tokens, a 10k-token conversation costs ~$0.025. Unbounded buffer over 100 turns ≈ $2.50+. Trimming to 4k ≈ $0.01/turn.

---

## Pattern 1 — Conversation Buffer (Simplest)

Store every message. Use for demos and prototypes only.

```python
# patterns/01_buffer.py
from langgraph.graph import StateGraph, START, MessagesState
from langchain_openai import ChatOpenAI

model = ChatOpenAI(model="gpt-4o-mini")

def call_model(state: MessagesState):
    response = model.invoke(state["messages"])
    return {"messages": [response]}

builder = StateGraph(MessagesState)
builder.add_node("call_model", call_model)
builder.add_edge(START, "call_model")
graph = builder.compile()

# Usage
result = graph.invoke({"messages": [{"role": "user", "content": "Hello!"}]})
```

**MessagesState** provides a `messages` list with the `add_messages` reducer built in — new messages are appended, not replaced.

**When to stop using this:** When your app has users who chat for more than ~20 turns, or when you see context-window errors.

---

## Pattern 2 — Conversation with Trimming

Keep the last N tokens. Messages older than the window are gone forever.

```python
# patterns/02_trimming.py
from langchain_core.messages.utils import trim_messages, count_tokens_approximately
from langgraph.graph import StateGraph, START, MessagesState
from langchain_openai import ChatOpenAI

model = ChatOpenAI(model="gpt-4o-mini")

def call_model(state: MessagesState):
    # Trim before sending to LLM — state still stores full history
    trimmed = trim_messages(
        state["messages"],
        strategy="last",               # keep the MOST RECENT messages
        token_counter=count_tokens_approximately,
        max_tokens=4000,               # tune to your model's context window
        start_on="human",              # never start mid-AI-turn
        end_on=("human", "tool"),      # never end mid-AI-turn
        include_system=True,           # keep system prompt if present
    )
    response = model.invoke(trimmed)
    return {"messages": [response]}

builder = StateGraph(MessagesState)
builder.add_node("call_model", call_model)
builder.add_edge(START, "call_model")
graph = builder.compile()
```

**Key parameters:**
- `strategy="last"` — sliding window from the end
- `strategy="first"` — keep oldest (rare; for logs)
- `max_tokens` — hard cap; set to ~80% of model limit to leave room for response
- `start_on="human"` — prevents broken assistant turns at window boundary

**Trade-off:** Simple and cheap. User cannot reference messages outside the window.

---

## Pattern 3 — Conversation with Summary

When context grows long, summarize old messages and keep only recent ones. The LLM retains the gist even when window rolls over.

```python
# patterns/03_summary.py
from typing import Any
from langgraph.graph import StateGraph, START, END, MessagesState
from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage
from langchain_openai import ChatOpenAI

model = ChatOpenAI(model="gpt-4o-mini")

# Extend MessagesState with a summary field
from typing_extensions import TypedDict
from typing import Annotated
from langgraph.graph.message import add_messages

class SummaryState(TypedDict):
    messages: Annotated[list, add_messages]
    summary: str   # accumulated summary of older messages

SUMMARIZE_AFTER = 10  # summarize when message count exceeds this

def call_model(state: SummaryState):
    summary = state.get("summary", "")
    messages = state["messages"]
    if summary:
        system = SystemMessage(content=f"Previous conversation summary:\n{summary}")
        messages = [system] + messages
    response = model.invoke(messages)
    return {"messages": [response]}

def summarize_conversation(state: SummaryState):
    summary = state.get("summary", "")
    if summary:
        prompt = (
            f"Existing summary:\n{summary}\n\n"
            "Extend the summary to include the new messages above:"
        )
    else:
        prompt = "Create a concise summary of the conversation above:"

    messages = state["messages"] + [HumanMessage(content=prompt)]
    response = model.invoke(messages)

    # Keep only the 2 most recent messages (the current exchange)
    to_delete = [RemoveMessage(id=m.id) for m in state["messages"][:-2]]
    return {"summary": response.content, "messages": to_delete}

def should_summarize(state: SummaryState) -> str:
    if len(state["messages"]) > SUMMARIZE_AFTER:
        return "summarize"
    return END

builder = StateGraph(SummaryState)
builder.add_node("call_model", call_model)
builder.add_node("summarize_conversation", summarize_conversation)
builder.add_edge(START, "call_model")
builder.add_conditional_edges("call_model", should_summarize)
builder.add_edge("summarize_conversation", END)
graph = builder.compile()
```

**When to use over trimming:** When users need the AI to remember the thread of a long discussion, not just the last few messages.

---

## Pattern 4 — LangGraph Checkpointing (Cross-Session)

Persist full graph state to a durable backend. Each `thread_id` is an isolated conversation that survives restarts.

### Choose Your Backend

```python
# patterns/04_checkpointing.py

# --- DEV: In-memory (lost on restart) ---
from langgraph.checkpoint.memory import InMemorySaver
checkpointer = InMemorySaver()

# --- SINGLE-PROCESS: SQLite (survives restarts) ---
# pip install langgraph-checkpoint-sqlite
from langgraph.checkpoint.sqlite import SqliteSaver
checkpointer = SqliteSaver.from_conn_string("./checkpoints.db")

# --- PRODUCTION: PostgreSQL (multi-instance safe) ---
# pip install langgraph-checkpoint-postgres
from langgraph.checkpoint.postgres import PostgresSaver
DB_URI = "postgresql://user:pass@host:5432/dbname"
# Run once to create tables: checkpointer.setup()
checkpointer = PostgresSaver.from_conn_string(DB_URI)
```

### Wire It In and Use thread_id for User Isolation

```python
from langgraph.graph import StateGraph, START, MessagesState
from langchain_openai import ChatOpenAI

model = ChatOpenAI(model="gpt-4o-mini")

def call_model(state: MessagesState):
    return {"messages": [model.invoke(state["messages"])]}

builder = StateGraph(MessagesState)
builder.add_node("call_model", call_model)
builder.add_edge(START, "call_model")

# Attach whichever checkpointer above
graph = builder.compile(checkpointer=checkpointer)

# Each user / conversation gets its own thread_id
USER_THREAD = "user-alice-session-42"

config = {"configurable": {"thread_id": USER_THREAD}}

# Turn 1
graph.invoke(
    {"messages": [{"role": "user", "content": "My name is Alice."}]},
    config,
)

# Turn 2 — graph automatically loads Alice's prior state
graph.invoke(
    {"messages": [{"role": "user", "content": "What is my name?"}]},
    config,
)

# Inspect what's stored
state = graph.get_state(config)
print(state.values["messages"])
```

**thread_id conventions:**
- Single user, single session: `"user-{user_id}"`
- Multiple sessions per user: `"user-{user_id}-session-{session_id}"`
- Never reuse thread_ids across users

**PostgresSaver setup (run once):**
```python
with PostgresSaver.from_conn_string(DB_URI) as cp:
    cp.setup()  # creates checkpoint tables
```

---

## Pattern 5 — Semantic / Vector Memory

Store facts as embeddings; retrieve by similarity at query time. Unlike checkpointing, this survives context-window limits and retrieves only what's relevant.

```python
# patterns/05_vector_memory.py
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_core.vectorstores import InMemoryVectorStore
# Production: swap InMemoryVectorStore for Chroma, Qdrant, Pinecone, etc.

model = ChatOpenAI(model="gpt-4o-mini")
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
vector_store = InMemoryVectorStore(embeddings)

# --- Store a fact ---
def store_memory(fact: str):
    vector_store.add_texts([fact])

# --- Retrieve relevant facts ---
def retrieve_memories(query: str, k: int = 3) -> list[str]:
    docs = vector_store.similarity_search(query, k=k)
    return [d.page_content for d in docs]

# --- Use as a tool in an agent ---
from langchain_core.tools import tool

@tool
def remember_fact(fact: str) -> str:
    """Store a fact for later retrieval."""
    store_memory(fact)
    return f"Stored: {fact}"

@tool
def recall_facts(query: str) -> str:
    """Retrieve facts relevant to the query."""
    facts = retrieve_memories(query)
    return "\n".join(facts) if facts else "No relevant memories found."

# Bind tools to the model
model_with_tools = model.bind_tools([remember_fact, recall_facts])
```

**Production vector stores (drop-in replacements):**
```python
# Chroma (local file persistence)
from langchain_chroma import Chroma
vector_store = Chroma(embedding_function=embeddings, persist_directory="./chroma_db")

# Qdrant (Docker / cloud)
from langchain_qdrant import QdrantVectorStore
vector_store = QdrantVectorStore.from_existing_collection(
    embedding=embeddings, collection_name="memories", url="http://localhost:6333"
)
```

---

## Pattern 6 — Entity Memory

Track structured profiles for named entities (people, places, concepts). The LLM extracts and updates JSON profiles each turn.

```python
# patterns/06_entity_memory.py
import json
from langgraph.graph import StateGraph, START, END, MessagesState
from langchain_openai import ChatOpenAI
from typing_extensions import TypedDict
from typing import Annotated
from langgraph.graph.message import add_messages

model = ChatOpenAI(model="gpt-4o-mini")

class EntityState(TypedDict):
    messages: Annotated[list, add_messages]
    entities: dict  # {"Alice": {"name": "Alice", "job": "engineer", ...}}

ENTITY_EXTRACTION_PROMPT = """
Extract or update entity information from the conversation.
Return ONLY a JSON object of shape: {{"EntityName": {{"attribute": "value"}}}}
Merge with existing data — do not lose prior attributes.
If no entities found, return {{}}.

Existing entities:
{existing}

Latest message:
{message}
"""

def extract_entities(state: EntityState):
    last_msg = state["messages"][-1].content
    existing = json.dumps(state.get("entities", {}), indent=2)
    prompt = ENTITY_EXTRACTION_PROMPT.format(existing=existing, message=last_msg)
    response = model.invoke([{"role": "user", "content": prompt}])
    try:
        updates = json.loads(response.content)
        merged = {**state.get("entities", {}), **updates}
    except json.JSONDecodeError:
        merged = state.get("entities", {})
    return {"entities": merged}

def call_model(state: EntityState):
    entities_ctx = json.dumps(state.get("entities", {}), indent=2)
    system = f"Known entities:\n{entities_ctx}\n\nUse this context when answering."
    messages = [{"role": "system", "content": system}] + list(state["messages"])
    response = model.invoke(messages)
    return {"messages": [response]}

builder = StateGraph(EntityState)
builder.add_node("extract_entities", extract_entities)
builder.add_node("call_model", call_model)
builder.add_edge(START, "extract_entities")
builder.add_edge("extract_entities", "call_model")
builder.add_edge("call_model", END)
graph = builder.compile()
```

**When to prefer over vector memory:** When you need structured, updatable profiles rather than free-text facts. Combine both when you need both.

---

## Pattern 7 — Cross-Thread Shared Memory (LangGraph Store API)

Share facts across all threads and users. Use for global knowledge, user preferences shared across sessions, or a shared knowledge base.

```python
# patterns/07_store_api.py
import uuid
from langgraph.graph import StateGraph, START, MessagesState
from langgraph.store.memory import InMemoryStore
from langgraph.checkpoint.memory import InMemorySaver
from langchain_openai import ChatOpenAI
from langgraph.config import get_store
from langchain_core.runnables import RunnableConfig

# Production: PostgresStore.from_conn_string(DB_URI)
store = InMemoryStore()
checkpointer = InMemorySaver()

model = ChatOpenAI(model="gpt-4o-mini")

def call_model(state: MessagesState, config: RunnableConfig):
    runtime_store = get_store()          # injected by LangGraph at runtime
    user_id = config["configurable"]["user_id"]
    namespace = ("memories", user_id)

    # Retrieve existing memories for this user
    memories = runtime_store.search(namespace, query=state["messages"][-1].content)
    facts = "\n".join(m.value["data"] for m in memories) if memories else "none"

    system_msg = f"User facts:\n{facts}"
    response = model.invoke(
        [{"role": "system", "content": system_msg}] + list(state["messages"])
    )

    # Persist new memory if user says "remember"
    if "remember" in state["messages"][-1].content.lower():
        runtime_store.put(namespace, str(uuid.uuid4()), {"data": state["messages"][-1].content})

    return {"messages": [response]}

builder = StateGraph(MessagesState)
builder.add_node("call_model", call_model)
builder.add_edge(START, "call_model")
graph = builder.compile(checkpointer=checkpointer, store=store)

# thread_id isolates the conversation; user_id scopes the store namespace
config_t1 = {"configurable": {"thread_id": "thread-1", "user_id": "alice"}}
config_t2 = {"configurable": {"thread_id": "thread-2", "user_id": "alice"}}

graph.invoke(
    {"messages": [{"role": "user", "content": "Remember: I prefer dark mode."}]},
    config_t1,
)
# Alice's preference is now available in thread-2
graph.invoke(
    {"messages": [{"role": "user", "content": "What are my preferences?"}]},
    config_t2,
)
```

**Store API summary:**
```python
store.put(namespace, key, {"data": value})   # write
store.get(namespace, key)                    # read exact key
store.search(namespace, query=text)          # semantic search (needs embeddings configured)
store.delete(namespace, key)                 # remove
```

**Namespace conventions:**
- `("memories", user_id)` — per-user memories
- `("preferences", user_id)` — per-user settings
- `("knowledge", "global")` — shared across all users

---

## Combining Patterns

Most production systems combine patterns:

| Use case | Combine |
|---|---|
| Multi-session chatbot | Pattern 4 (checkpointing) + Pattern 2 (trimming) |
| Personal assistant | Pattern 4 + Pattern 6 (entity) + Pattern 7 (store) |
| Knowledge-base agent | Pattern 5 (vector) + Pattern 4 |
| Enterprise multi-tenant | Pattern 4 (PostgresSaver) + Pattern 7 (PostgresStore) |

---

## Common Mistakes

| Mistake | Fix |
|---|---|
| Using `InMemorySaver` in production | Switch to `PostgresSaver`; `InMemorySaver` is lost on restart |
| Reusing `thread_id` across users | Each user needs a unique `thread_id`; never share |
| Storing embeddings in `InMemoryVectorStore` in production | Use Chroma, Qdrant, or Pinecone with persistence |
| Forgetting `checkpointer.setup()` for Postgres | Call once on deploy; idempotent after that |
| Trimming state directly (mutating `state["messages"]`) | Trim only the slice passed to the LLM — let state grow, trim the send |
| Using RAG when you need memory | RAG retrieves from a document corpus; memory tracks conversation state — different problems |
| Entity extraction on every message | Gate on message length or keyword presence to avoid unnecessary LLM calls |

---

## Quick Install Reference

```bash
# Core (already in langchain-core)
pip install langchain-core langchain-openai langgraph

# SQLite checkpointing
pip install langgraph-checkpoint-sqlite

# PostgreSQL checkpointing + store
pip install langgraph-checkpoint-postgres

# Chroma vector store
pip install langchain-chroma chromadb

# Qdrant vector store
pip install langchain-qdrant qdrant-client
```

---

## See Also

- `lc:rag` — retrieval-augmented generation over documents (not memory)
- `lc:agents` — tool-calling agents that use memory tools
- `lc:state` — LangGraph state design patterns

---

## Section 8 — Multi-Tenant Isolation

### The Problem: thread_id Is Not a Security Boundary

LangGraph's default `thread_id` is caller-supplied and opaque to the checkpointer. Two consequences:

1. **Guessable:** `"user-alice"` is trivially enumerated. Tenant B can read Tenant A's checkpoints by passing `thread_id="user-alice"` if your API does not enforce ownership.
2. **No scope:** `PostgresSaver` stores all threads in the same table with no tenant column. A compromised service account or a missing `WHERE` clause leaks every user's conversation history.

These are not hypothetical: any multi-tenant SaaS shipping LangGraph without the mitigations below has a data-isolation vulnerability.

---

### 8.1 Thread ID Convention

Encode tenant, user, and session into every thread ID so ownership is derivable without an extra lookup:

```
tenant:{tenant_id}:user:{user_id}:session:{session_id}
```

Example:

```python
import secrets

def make_thread_id(tenant_id: str, user_id: str) -> str:
    """Generate a scoped, unguessable thread ID."""
    session_id = secrets.token_urlsafe(16)   # 128-bit random, URL-safe
    return f"tenant:{tenant_id}:user:{user_id}:session:{session_id}"

def parse_thread_id(thread_id: str) -> dict:
    """Extract components from a scoped thread ID. Raises ValueError if malformed."""
    parts = thread_id.split(":")
    if len(parts) != 6 or parts[0] != "tenant" or parts[2] != "user" or parts[4] != "session":
        raise ValueError(f"Invalid thread_id format: {thread_id!r}")
    return {
        "tenant_id": parts[1],
        "user_id": parts[3],
        "session_id": parts[5],
    }
```

Rules:
- `session_id` must be cryptographically random — never sequential integers.
- The calling service is responsible for storing the mapping `(user_id -> [thread_ids])` so users can resume prior sessions.
- Never expose raw thread IDs to end-users in URLs or API responses without first validating the caller owns the tenant embedded in the ID.

---

### 8.2 TenantIsolatedCheckpointer

A drop-in wrapper around any `BaseCheckpointSaver` that validates ownership on every read and write. If the `tenant_id` in the thread ID does not match the `tenant_id` in the config, it raises `PermissionError` before touching storage.

```python
# patterns/08_tenant_checkpointer.py
from __future__ import annotations

import re
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

_THREAD_RE = re.compile(
    r"^tenant:(?P<tenant_id>[^:]+):user:(?P<user_id>[^:]+):session:(?P<session_id>[^:]+)$"
)


def _extract_tenant(thread_id: str) -> str:
    """Return the tenant_id embedded in the thread_id, or raise."""
    m = _THREAD_RE.match(thread_id)
    if not m:
        raise ValueError(
            f"thread_id {thread_id!r} does not follow the "
            "'tenant:T:user:U:session:S' convention."
        )
    return m.group("tenant_id")


def _assert_ownership(config: RunnableConfig) -> None:
    """
    Compare the tenant_id in the thread_id against the tenant_id supplied
    in config['configurable']. Raises PermissionError on mismatch.
    """
    configurable = config.get("configurable", {})
    thread_id: str = configurable.get("thread_id", "")
    caller_tenant: str = configurable.get("tenant_id", "")

    if not caller_tenant:
        raise PermissionError(
            "config['configurable']['tenant_id'] is required for multi-tenant graphs."
        )

    embedded_tenant = _extract_tenant(thread_id)
    if embedded_tenant != caller_tenant:
        raise PermissionError(
            f"Access denied: thread belongs to tenant '{embedded_tenant}', "
            f"but caller presented tenant '{caller_tenant}'."
        )


class TenantIsolatedCheckpointer(BaseCheckpointSaver):
    """
    Wraps any BaseCheckpointSaver and enforces tenant ownership on every
    get/put/list operation.

    Usage:
        inner = PostgresSaver.from_conn_string(DB_URI)
        checkpointer = TenantIsolatedCheckpointer(inner)
        graph = builder.compile(checkpointer=checkpointer)

    Every invoke/stream call must include:
        config = {
            "configurable": {
                "thread_id": "tenant:acme:user:alice:session:abc123",
                "tenant_id": "acme",   # <-- must match thread_id
            }
        }
    """

    def __init__(self, inner: BaseCheckpointSaver) -> None:
        # Do NOT call super().__init__() with a serde — delegate everything to inner.
        object.__setattr__(self, "_inner", inner)

    # ------------------------------------------------------------------
    # Synchronous interface
    # ------------------------------------------------------------------

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        _assert_ownership(config)
        return self._inner.get_tuple(config)

    def list(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        if config is not None:
            _assert_ownership(config)
        yield from self._inner.list(config, filter=filter, before=before, limit=limit)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict,
    ) -> RunnableConfig:
        _assert_ownership(config)
        return self._inner.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        _assert_ownership(config)
        self._inner.put_writes(config, writes, task_id)

    # ------------------------------------------------------------------
    # Asynchronous interface
    # ------------------------------------------------------------------

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        _assert_ownership(config)
        return await self._inner.aget_tuple(config)

    async def alist(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is not None:
            _assert_ownership(config)
        async for item in self._inner.alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict,
    ) -> RunnableConfig:
        _assert_ownership(config)
        return await self._inner.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        _assert_ownership(config)
        await self._inner.aput_writes(config, writes, task_id)

    # Delegate serializer access so inner saver's serde is used transparently
    @property
    def serde(self):
        return self._inner.serde
```

**Wire it up:**

```python
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import StateGraph, START, MessagesState
from langchain_openai import ChatOpenAI

inner_cp = PostgresSaver.from_conn_string(DB_URI)
checkpointer = TenantIsolatedCheckpointer(inner_cp)

model = ChatOpenAI(model="gpt-4o-mini")

def call_model(state: MessagesState):
    return {"messages": [model.invoke(state["messages"])]}

builder = StateGraph(MessagesState)
builder.add_node("call_model", call_model)
builder.add_edge(START, "call_model")
graph = builder.compile(checkpointer=checkpointer)

# Correct usage — tenant_id matches the one embedded in thread_id
config = {
    "configurable": {
        "thread_id": "tenant:acme:user:alice:session:xK3mP9qR",
        "tenant_id": "acme",
    }
}
graph.invoke({"messages": [{"role": "user", "content": "Hello"}]}, config)

# Attempt to access another tenant's thread — raises PermissionError
bad_config = {
    "configurable": {
        "thread_id": "tenant:acme:user:alice:session:xK3mP9qR",
        "tenant_id": "evil-corp",   # mismatch!
    }
}
# graph.invoke(..., bad_config)  # -> PermissionError
```

---

### 8.3 PostgreSQL Row-Level Security

`TenantIsolatedCheckpointer` enforces isolation at the application layer. Add PostgreSQL RLS as a second independent layer so that even a direct DB connection (e.g. a compromised read replica credential) cannot cross tenant boundaries.

```sql
-- 08_rls.sql
-- Run once during database setup, after PostgresSaver.setup() has created the tables.

-- Step 1: Add a tenant_id column to the checkpoints table.
-- PostgresSaver stores thread_id in the 'thread_id' column. We extract the
-- tenant segment and materialise it for fast indexed filtering.
ALTER TABLE checkpoints
    ADD COLUMN IF NOT EXISTS tenant_id TEXT
        GENERATED ALWAYS AS (
            -- Extract 'acme' from 'tenant:acme:user:alice:session:xyz'
            split_part(thread_id, ':', 2)
        ) STORED;

ALTER TABLE checkpoint_writes
    ADD COLUMN IF NOT EXISTS tenant_id TEXT
        GENERATED ALWAYS AS (
            split_part(thread_id, ':', 2)
        ) STORED;

-- Step 2: Index the generated column for O(log n) per-tenant scans.
CREATE INDEX IF NOT EXISTS idx_checkpoints_tenant_id
    ON checkpoints (tenant_id);

CREATE INDEX IF NOT EXISTS idx_checkpoint_writes_tenant_id
    ON checkpoint_writes (tenant_id);

-- Step 3: Enable RLS on both tables.
ALTER TABLE checkpoints        ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoint_writes  ENABLE ROW LEVEL SECURITY;

-- Step 4: Create policies. The application must SET app.current_tenant_id
--         at the start of every connection/transaction.
--
--         In Python (psycopg2 / asyncpg), do:
--           await conn.execute("SET app.current_tenant_id = $1", tenant_id)
--         before any LangGraph checkpoint operation.

CREATE POLICY tenant_isolation_checkpoints
    ON checkpoints
    USING (tenant_id = current_setting('app.current_tenant_id', true));

CREATE POLICY tenant_isolation_checkpoint_writes
    ON checkpoint_writes
    USING (tenant_id = current_setting('app.current_tenant_id', true));

-- Step 5: The application DB user must NOT be a superuser (superusers bypass RLS).
-- Verify with: SELECT rolsuper FROM pg_roles WHERE rolname = 'your_app_user';
-- It must return 'f'.

-- Step 6: Grant only necessary privileges (no BYPASSRLS).
-- GRANT SELECT, INSERT, UPDATE, DELETE ON checkpoints TO app_user;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON checkpoint_writes TO app_user;
```

**Setting the tenant context in Python:**

```python
# patterns/08_rls_connection.py
import asyncpg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

async def get_tenant_checkpointer(tenant_id: str) -> AsyncPostgresSaver:
    """
    Return a checkpointer whose underlying connection is scoped to tenant_id
    via PostgreSQL session-level GUC. Wrap with TenantIsolatedCheckpointer
    for defence-in-depth.
    """
    conn = await asyncpg.connect(DB_URI)
    # Set the GUC that the RLS policy reads
    await conn.execute("SET app.current_tenant_id = $1", tenant_id)
    inner = AsyncPostgresSaver(conn)
    return TenantIsolatedCheckpointer(inner)
```

**Defence-in-depth summary:**

| Layer | What it catches |
|---|---|
| Thread ID convention | Human errors: wrong thread used by accident |
| TenantIsolatedCheckpointer | Application-layer bugs: service passes wrong tenant_id |
| PostgreSQL RLS | Infrastructure-layer breaches: direct DB access, SQL injection |

All three layers are cheap to add and independent. Any one of them failing is tolerable if the other two hold.

---

### 8.4 Vector Store Isolation: Collection-per-Tenant vs. Metadata Filter

Vector memory (Pattern 5) needs its own isolation strategy because checkpointer tables and vector stores are separate systems.

#### Decision Rule

| Factor | Collection-per-tenant | Metadata filter |
|---|---|---|
| Number of tenants | **< 1,000** | > 1,000 |
| Delete all tenant data | **Clean, one call** | Requires filtered delete loop |
| Query performance | **No cross-tenant overhead** | Small overhead from filter |
| Operational complexity | More collections to manage | Simpler — one collection |
| Shared knowledge base | Requires explicit cross-collection query | **Natural — filter = None** |

**Rule:** Use collection-per-tenant when you have fewer than 1,000 tenants, need hard delete (GDPR §9 below), or have SLA-sensitive latency. Use metadata filter above 1,000 tenants or when tenants share a common knowledge base.

#### Collection-per-Tenant (Qdrant example)

```python
# patterns/08_vector_collection.py
from langchain_qdrant import QdrantVectorStore
from langchain_openai import OpenAIEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
client = QdrantClient(url="http://localhost:6333")

def get_tenant_vector_store(tenant_id: str) -> QdrantVectorStore:
    """Return the vector store for a specific tenant, creating it if needed."""
    collection_name = f"memories_{tenant_id}"   # e.g. "memories_acme"

    existing = {c.name for c in client.get_collections().collections}
    if collection_name not in existing:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
        )

    return QdrantVectorStore(
        client=client,
        collection_name=collection_name,
        embedding=embeddings,
    )

def delete_tenant_collection(tenant_id: str) -> None:
    """Hard-delete all vectors for a tenant in one call (used by erasure, Section 9)."""
    client.delete_collection(f"memories_{tenant_id}")
```

#### Metadata-Filter Approach (Chroma example)

```python
# patterns/08_vector_metadata.py
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain_core.documents import Document

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

# Single shared collection
shared_store = Chroma(
    collection_name="all_memories",
    embedding_function=embeddings,
    persist_directory="./chroma_shared",
)

def store_tenant_memory(tenant_id: str, user_id: str, text: str) -> None:
    doc = Document(
        page_content=text,
        metadata={"tenant_id": tenant_id, "user_id": user_id},
    )
    shared_store.add_documents([doc])

def retrieve_tenant_memories(tenant_id: str, user_id: str, query: str, k: int = 4):
    """Retrieve memories scoped to this tenant+user via metadata filter."""
    return shared_store.similarity_search(
        query,
        k=k,
        filter={"$and": [{"tenant_id": tenant_id}, {"user_id": user_id}]},
    )

def delete_tenant_memories(tenant_id: str) -> None:
    """
    Filtered delete — less clean than dropping a collection.
    Chroma does not yet support filter-only delete via the LangChain wrapper;
    use the underlying client directly.
    """
    ids = shared_store._collection.get(
        where={"tenant_id": tenant_id}
    )["ids"]
    if ids:
        shared_store._collection.delete(ids=ids)
```

---

### 8.5 Store API Namespace Convention

The LangGraph Store API uses hierarchical namespaces. For multi-tenant systems, always encode tenant and user into the namespace tuple so that `store.search` and `store.list` are automatically scoped:

```python
# Namespace structure: (tenant_id, user_id, feature_name)
#
# Examples:
#   ("acme", "alice", "memories")        — Alice's memories at Acme
#   ("acme", "alice", "preferences")     — Alice's preferences at Acme
#   ("acme", "_shared", "knowledge")     — Acme-wide shared knowledge base
#   ("_global", "_all", "knowledge")     — Cross-tenant global knowledge (rare)

def user_namespace(tenant_id: str, user_id: str, feature: str) -> tuple:
    return (tenant_id, user_id, feature)

def tenant_namespace(tenant_id: str, feature: str) -> tuple:
    return (tenant_id, "_shared", feature)
```

**Why this matters:** `store.search(("acme",))` would return ALL Acme data. Scoping to `("acme", "alice", "memories")` prevents one user from reading another's memories within the same tenant without any extra filtering logic.

```python
# patterns/08_store_namespace.py
import uuid
from langgraph.store.postgres import PostgresStore
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_store

store = PostgresStore.from_conn_string(DB_URI)

def save_user_memory(tenant_id: str, user_id: str, text: str) -> str:
    key = str(uuid.uuid4())
    ns = user_namespace(tenant_id, user_id, "memories")
    store.put(ns, key, {"data": text, "tenant_id": tenant_id, "user_id": user_id})
    return key

def search_user_memories(tenant_id: str, user_id: str, query: str) -> list[str]:
    ns = user_namespace(tenant_id, user_id, "memories")
    results = store.search(ns, query=query, limit=5)
    return [r.value["data"] for r in results]
```

---

### 8.6 Per-Tenant Token Quota

Use a Redis counter incremented by a LangChain callback to enforce token budgets per tenant. The counter is a sliding window (TTL-based) or a monthly bucket.

```python
# patterns/08_token_quota.py
from __future__ import annotations

import time
from typing import Any

import redis.asyncio as aioredis
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

QUOTA_PREFIX = "token_quota"
DEFAULT_MONTHLY_LIMIT = 1_000_000  # 1M tokens/month per tenant


class CostTrackingCallback(AsyncCallbackHandler):
    """
    Async LangChain callback that increments a per-tenant Redis token counter
    after every LLM call. Raises RuntimeError if the tenant has exceeded quota.

    Usage:
        cb = CostTrackingCallback(redis_client, tenant_id="acme")
        await model.ainvoke(messages, config={"callbacks": [cb]})
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        tenant_id: str,
        monthly_limit: int = DEFAULT_MONTHLY_LIMIT,
    ) -> None:
        self.redis = redis
        self.tenant_id = tenant_id
        self.monthly_limit = monthly_limit

    def _quota_key(self) -> str:
        # Bucket by calendar month: "token_quota:acme:2025-07"
        month = time.strftime("%Y-%m")
        return f"{QUOTA_PREFIX}:{self.tenant_id}:{month}"

    async def _get_current_usage(self) -> int:
        val = await self.redis.get(self._quota_key())
        return int(val) if val else 0

    async def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any
    ) -> None:
        """Check quota BEFORE the LLM call to fail fast."""
        usage = await self._get_current_usage()
        if usage >= self.monthly_limit:
            raise RuntimeError(
                f"Tenant '{self.tenant_id}' has exceeded its monthly token quota "
                f"({usage:,} / {self.monthly_limit:,}). "
                "Upgrade plan or wait until next billing cycle."
            )

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Increment counter AFTER the LLM call with actual usage."""
        total_tokens = 0
        for generations in response.generations:
            for gen in generations:
                if hasattr(gen, "generation_info") and gen.generation_info:
                    usage = gen.generation_info.get("token_usage", {})
                    total_tokens += usage.get("total_tokens", 0)

        # Fall back to llm_output if generation_info is absent
        if total_tokens == 0 and response.llm_output:
            usage = response.llm_output.get("token_usage", {})
            total_tokens = usage.get("total_tokens", 0)

        if total_tokens > 0:
            key = self._quota_key()
            pipe = self.redis.pipeline()
            pipe.incrby(key, total_tokens)
            # Expire the key 35 days after first write (covers month boundary)
            pipe.expire(key, 60 * 60 * 24 * 35, nx=True)
            await pipe.execute()

    async def get_usage_report(self) -> dict:
        usage = await self._get_current_usage()
        return {
            "tenant_id": self.tenant_id,
            "month": time.strftime("%Y-%m"),
            "tokens_used": usage,
            "tokens_limit": self.monthly_limit,
            "pct_used": round(usage / self.monthly_limit * 100, 1),
        }


# --- Integration with a LangGraph node ---

async def call_model_with_quota(state, config, *, redis_client: aioredis.Redis):
    from langchain_openai import ChatOpenAI

    tenant_id = config["configurable"]["tenant_id"]
    cb = CostTrackingCallback(redis_client, tenant_id=tenant_id)
    model = ChatOpenAI(model="gpt-4o-mini", callbacks=[cb])
    response = await model.ainvoke(state["messages"])
    return {"messages": [response]}
```

**Redis data model:**

```
key:   token_quota:{tenant_id}:{YYYY-MM}
type:  string (integer)
TTL:   35 days (set once, nx=True)
value: cumulative token count for the month
```

**Quota enforcement flow:**
1. `on_llm_start` — read counter; reject if >= limit (fail-fast, no wasted API call)
2. `on_llm_end` — atomically increment counter by actual token usage

---

## Section 9 — Right to Erasure (GDPR Art. 17)

### Legal Context

GDPR Article 17 grants data subjects the right to have their personal data erased without undue delay. Controllers must comply **within 30 days** of a valid request. Failure carries fines up to €20M or 4% of global annual turnover.

For a LangChain/LangGraph application, "personal data" lives in:

| Storage | Contains | Must erase? |
|---|---|---|
| PostgreSQL checkpoints | Full conversation history | Yes |
| Vector store | Embedded user utterances / facts | Yes |
| Audit log | Who did what and when | **No** — insert erasure record instead |
| Redis quota counters | Token counts (no PII) | No (or yes if tenant_id is PII in your model) |
| Backups | Snapshot of all of the above | Yes — covered by backup retention policy |

**The audit trail must not be deleted.** It is evidence of compliance. Deleting it can itself be a violation. Instead, insert an immutable erasure record that proves the deletion happened and when.

---

### 9.1 Audit Table Schema

```sql
-- 09_audit_schema.sql
-- Run once during database setup.

CREATE TABLE IF NOT EXISTS user_data_erasure_log (
    erasure_record_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             TEXT        NOT NULL,
    tenant_id           TEXT        NOT NULL,
    requested_at        TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    checkpoint_rows     INTEGER     NOT NULL DEFAULT 0,
    vector_docs         INTEGER     NOT NULL DEFAULT 0,
    store_items         INTEGER     NOT NULL DEFAULT 0,
    requested_by        TEXT,       -- operator who triggered the erasure
    notes               TEXT
);

-- Index for compliance reporting: "show all erasures for tenant X in 2025"
CREATE INDEX IF NOT EXISTS idx_erasure_log_tenant_user
    ON user_data_erasure_log (tenant_id, user_id, completed_at DESC);

-- Prevent any UPDATE or DELETE on this table (immutable audit trail).
-- Enforce via application permissions: the app DB user gets INSERT only.
-- REVOKE UPDATE, DELETE ON user_data_erasure_log FROM app_user;
-- GRANT  INSERT, SELECT  ON user_data_erasure_log TO app_user;
```

---

### 9.2 delete_user_data — Complete Implementation

```python
# patterns/09_erasure.py
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

@dataclass
class ErasureReceipt:
    """
    Returned to the caller (and ideally stored / emailed as proof of erasure).
    """
    user_id: str
    tenant_id: str
    timestamp: str                  # ISO-8601 UTC
    records_deleted: dict           # {"checkpoints": N, "vector_docs": N, "store_items": N}
    erasure_record_id: str          # UUID inserted into user_data_erasure_log
    requested_by: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "timestamp": self.timestamp,
            "records_deleted": self.records_deleted,
            "erasure_record_id": self.erasure_record_id,
            "requested_by": self.requested_by,
        }


# ---------------------------------------------------------------------------
# Vector store protocol
# ---------------------------------------------------------------------------

class DeletableVectorStore:
    """
    Minimal protocol expected by delete_user_data.
    Implement this interface on whichever vector store you use.
    """
    async def adelete_by_metadata(self, filter: dict) -> int:
        """Delete all documents matching filter. Return count deleted."""
        raise NotImplementedError


class QdrantTenantVectorStore(DeletableVectorStore):
    """
    Collection-per-tenant Qdrant implementation.
    Drops the entire collection for the tenant (clean, atomic).
    Falls back to point-level delete if only a single user within the tenant
    must be erased while other users' data is retained.
    """

    def __init__(self, qdrant_client, collection_prefix: str = "memories"):
        self.client = qdrant_client
        self.collection_prefix = collection_prefix

    async def adelete_by_metadata(self, filter: dict) -> int:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        tenant_id = filter.get("tenant_id")
        user_id = filter.get("user_id")
        collection_name = f"{self.collection_prefix}_{tenant_id}"

        existing = {c.name for c in self.client.get_collections().collections}
        if collection_name not in existing:
            return 0

        # If deleting entire tenant: drop collection
        if not user_id:
            info = self.client.get_collection(collection_name)
            count = info.points_count or 0
            self.client.delete_collection(collection_name)
            return count

        # Deleting a single user within a shared/multi-user collection
        qdrant_filter = Filter(
            must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
        )
        # scroll to count before delete
        results, _ = self.client.scroll(
            collection_name=collection_name,
            scroll_filter=qdrant_filter,
            limit=10_000,
            with_payload=False,
        )
        point_ids = [r.id for r in results]
        if point_ids:
            self.client.delete(
                collection_name=collection_name,
                points_selector=point_ids,
            )
        return len(point_ids)


# ---------------------------------------------------------------------------
# Store API items (LangGraph PostgresStore)
# ---------------------------------------------------------------------------

async def _delete_store_items(
    pool: asyncpg.Pool,
    tenant_id: str,
    user_id: str,
) -> int:
    """
    Delete all LangGraph Store API items whose namespace starts with
    (tenant_id, user_id, ...).

    PostgresStore stores namespace as a text array column 'prefix'.
    The exact schema depends on the langgraph-checkpoint-postgres version;
    adjust the column name if your migration differs.
    """
    result = await pool.execute(
        """
        DELETE FROM store
        WHERE prefix[1] = $1
          AND prefix[2] = $2
        """,
        tenant_id,
        user_id,
    )
    # asyncpg returns "DELETE N"
    return int(result.split()[-1])


# ---------------------------------------------------------------------------
# Main erasure function
# ---------------------------------------------------------------------------

async def delete_user_data(
    user_id: str,
    tenant_id: str,
    pool: asyncpg.Pool,
    vector_store: DeletableVectorStore,
    *,
    requested_by: Optional[str] = None,
    requested_at: Optional[datetime] = None,
    dry_run: bool = False,
) -> ErasureReceipt:
    """
    Erase all personal data for a user across checkpoints, vector store,
    and the LangGraph Store API. Insert an immutable audit record.

    Args:
        user_id:       The user whose data must be erased.
        tenant_id:     The tenant the user belongs to.
        pool:          asyncpg connection pool to the application database.
        vector_store:  A DeletableVectorStore implementation for this tenant.
        requested_by:  Operator / system that triggered the request (for audit).
        requested_at:  When the erasure request was received (defaults to now).
        dry_run:       If True, count rows but do NOT delete or insert audit record.

    Returns:
        ErasureReceipt with counts and erasure_record_id.

    Raises:
        asyncpg.PostgresError: on database failure.
        RuntimeError: if the audit record insertion fails (data already deleted).
    """
    if requested_at is None:
        requested_at = datetime.now(timezone.utc)

    completed_at = datetime.now(timezone.utc)
    counts: dict[str, int] = {"checkpoints": 0, "vector_docs": 0, "store_items": 0}

    async with pool.acquire() as conn:
        async with conn.transaction():

            # ----------------------------------------------------------
            # 1. Delete checkpoint rows
            #    thread_id pattern: "tenant:{tenant_id}:user:{user_id}:*"
            # ----------------------------------------------------------
            if not dry_run:
                cp_result = await conn.execute(
                    """
                    DELETE FROM checkpoints
                    WHERE thread_id LIKE $1
                    """,
                    f"tenant:{tenant_id}:user:{user_id}:%",
                )
                counts["checkpoints"] += int(cp_result.split()[-1])

                cw_result = await conn.execute(
                    """
                    DELETE FROM checkpoint_writes
                    WHERE thread_id LIKE $1
                    """,
                    f"tenant:{tenant_id}:user:{user_id}:%",
                )
                counts["checkpoints"] += int(cw_result.split()[-1])

                # Also delete from checkpoint_blobs if your version creates it
                try:
                    cb_result = await conn.execute(
                        """
                        DELETE FROM checkpoint_blobs
                        WHERE thread_id LIKE $1
                        """,
                        f"tenant:{tenant_id}:user:{user_id}:%",
                    )
                    counts["checkpoints"] += int(cb_result.split()[-1])
                except asyncpg.UndefinedTableError:
                    pass  # table does not exist in this version — safe to ignore

            else:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) FROM checkpoints WHERE thread_id LIKE $1",
                    f"tenant:{tenant_id}:user:{user_id}:%",
                )
                counts["checkpoints"] = row[0]

            # ----------------------------------------------------------
            # 2. Delete LangGraph Store API items
            # ----------------------------------------------------------
            if not dry_run:
                counts["store_items"] = await _delete_store_items(pool, tenant_id, user_id)
            else:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) FROM store WHERE prefix[1] = $1 AND prefix[2] = $2",
                    tenant_id, user_id,
                )
                counts["store_items"] = row[0]

            # ----------------------------------------------------------
            # 3. Insert immutable audit record  (NEVER skip this step)
            # ----------------------------------------------------------
            erasure_record_id = str(uuid.uuid4())
            if not dry_run:
                await conn.execute(
                    """
                    INSERT INTO user_data_erasure_log
                        (erasure_record_id, user_id, tenant_id,
                         requested_at, completed_at,
                         checkpoint_rows, vector_docs, store_items,
                         requested_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    erasure_record_id,
                    user_id,
                    tenant_id,
                    requested_at,
                    completed_at,
                    counts["checkpoints"],
                    counts["vector_docs"],   # filled in below after vector delete
                    counts["store_items"],
                    requested_by,
                )

    # ------------------------------------------------------------------
    # 4. Delete vector store documents
    #    Done OUTSIDE the Postgres transaction because the vector store
    #    is a separate system. If this fails after the DB commit, the
    #    audit record is still present — re-run the erasure to clean up.
    # ------------------------------------------------------------------
    if not dry_run:
        try:
            counts["vector_docs"] = await vector_store.adelete_by_metadata(
                {"tenant_id": tenant_id, "user_id": user_id}
            )
        except Exception as exc:
            logger.error(
                "Vector store deletion failed for user=%s tenant=%s: %s",
                user_id, tenant_id, exc, exc_info=True,
            )
            # Update the audit record with the failure note
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE user_data_erasure_log
                    SET notes = $1
                    WHERE erasure_record_id = $2
                    """,
                    f"Vector store deletion failed: {exc}",
                    erasure_record_id,
                )
            raise

        # Update audit record with accurate vector_docs count
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE user_data_erasure_log
                SET vector_docs = $1
                WHERE erasure_record_id = $2
                """,
                counts["vector_docs"],
                erasure_record_id,
            )
    else:
        counts["vector_docs"] = await vector_store.adelete_by_metadata(
            {"tenant_id": tenant_id, "user_id": user_id, "_dry_run": True}
        )

    receipt = ErasureReceipt(
        user_id=user_id,
        tenant_id=tenant_id,
        timestamp=completed_at.isoformat(),
        records_deleted=counts,
        erasure_record_id=erasure_record_id if not dry_run else "(dry-run — no record inserted)",
        requested_by=requested_by,
    )

    logger.info(
        "Erasure %s for user=%s tenant=%s: %s",
        "DRY-RUN" if dry_run else "COMPLETE",
        user_id, tenant_id, counts,
    )

    return receipt
```

---

### 9.3 Usage Examples

```python
# patterns/09_erasure_usage.py
import asyncio
import asyncpg
from qdrant_client import QdrantClient

async def handle_erasure_request(user_id: str, tenant_id: str):
    pool = await asyncpg.create_pool(DB_URI, min_size=2, max_size=10)
    qdrant = QdrantClient(url="http://localhost:6333")
    vector_store = QdrantTenantVectorStore(qdrant, collection_prefix="memories")

    # --- Dry run first: see what would be deleted ---
    dry_receipt = await delete_user_data(
        user_id=user_id,
        tenant_id=tenant_id,
        pool=pool,
        vector_store=vector_store,
        requested_by="gdpr_portal",
        dry_run=True,
    )
    print("Dry run:", dry_receipt.as_dict())
    # {'user_id': 'alice', 'tenant_id': 'acme', ...,
    #  'records_deleted': {'checkpoints': 47, 'vector_docs': 12, 'store_items': 5},
    #  'erasure_record_id': '(dry-run — no record inserted)'}

    # --- Confirm and execute ---
    receipt = await delete_user_data(
        user_id=user_id,
        tenant_id=tenant_id,
        pool=pool,
        vector_store=vector_store,
        requested_by="gdpr_portal",
    )
    print("Erasure complete:", receipt.as_dict())
    # {'user_id': 'alice', 'tenant_id': 'acme',
    #  'timestamp': '2025-07-14T10:23:45.123456+00:00',
    #  'records_deleted': {'checkpoints': 47, 'vector_docs': 12, 'store_items': 5},
    #  'erasure_record_id': 'f47ac10b-58cc-4372-a567-0e02b2c3d479',
    #  'requested_by': 'gdpr_portal'}

    await pool.close()
```

---

### 9.4 30-Day Compliance Checklist

| Step | Implementation |
|---|---|
| Receive request | GDPR portal / support ticket → record `requested_at` |
| Verify identity | Out-of-band; LangGraph has no auth — your API layer handles this |
| Dry run | Call `delete_user_data(..., dry_run=True)` and log the counts |
| Execute erasure | Call `delete_user_data(...)` within 30 days of `requested_at` |
| Send confirmation | Email the `ErasureReceipt.as_dict()` to the data subject |
| Retain audit record | Row in `user_data_erasure_log` — never delete it |
| Handle backups | Ensure backup retention policy expires within 30 days, or document the exception |
| Re-erasure requests | Query `user_data_erasure_log` first; if a recent record exists, respond with the prior receipt |

---

### 9.5 What NOT to Delete

```
DO erase:
  - checkpoints.*         (conversation history)
  - checkpoint_writes.*   (pending node writes)
  - checkpoint_blobs.*    (serialized state blobs)
  - store.*               (LangGraph Store API items)
  - vector collection     (embedded utterances / facts)

DO NOT erase:
  - user_data_erasure_log  (proof of compliance — immutable)
  - Billing / payment records (separate legal basis — contract)
  - Aggregated analytics (no PII; cannot be re-identified)
  - Redis token quota counters (contains no message content)
```

---

### Quick Install for Section 8 & 9

```bash
# Redis (for token quotas)
pip install redis[asyncio]

# asyncpg (for async Postgres in erasure)
pip install asyncpg

# Qdrant (for collection-per-tenant vector store)
pip install qdrant-client langchain-qdrant
```
