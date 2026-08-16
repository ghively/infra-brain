---
name: lc-trace
description: Use when adding LangSmith tracing to an existing Python file that uses LangChain or LangGraph. Triggered by requests to add tracing, instrument a file, make code observable, hook up LangSmith, add @traceable, or trace a chain/agent/function. Reads the current file, detects what LangChain/LangGraph components are present, and applies the correct tracing pattern for each — LCEL chains, @traceable for custom functions, RunnableConfig for LangGraph nodes, and context-manager wrapping for standalone functions.
---

# lc-trace — Add LangSmith Tracing to a Python File

## Overview

This command instruments an existing Python file with LangSmith tracing. It detects what LangChain/LangGraph components are present and applies the right tracing technique for each — no manual pattern-matching needed.

**Core model:** LangSmith tracing is layered. LCEL chains and LangGraph graphs trace automatically when the env vars are set. Custom Python functions need explicit decoration. The goal of this command is to ensure every meaningful unit of work in the file is visible in the LangSmith UI.

---

## Command Flow

Execute these steps in order without pausing for input:

1. Read the target file
2. Check `.env` for `LANGSMITH_API_KEY`
3. Analyze what LangChain/LangGraph components are present
4. Apply tracing transformations (see Detection and Transformation rules below)
5. Add missing `langsmith` imports
6. Show a before/after diff and summary of changes made

---

## Step 1 — Read the File

Read the file the user has open or the path they specified. If no path is given, ask: "Which file should I add tracing to?"

Parse the file to identify components (see Detection section).

---

## Step 2 — .env Setup Check

Search for a `.env` file in the project root (same directory as the Python file, or walk up to find one).

```python
# What to look for in .env:
LANGSMITH_API_KEY=...
LANGSMITH_TRACING=...
LANGSMITH_PROJECT=...
```

### If LANGSMITH_API_KEY is missing or not in .env:

Append to `.env` (create it if it does not exist):

```bash
# LangSmith tracing — get your free API key at smith.langchain.com
LANGSMITH_API_KEY=ls__YOUR_KEY_HERE
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=langchain-lab
```

Report: "Added LangSmith placeholder to `.env`. Replace `ls__YOUR_KEY_HERE` with your key from smith.langchain.com."

### If LANGSMITH_API_KEY is present but LANGSMITH_TRACING is missing:

Add:
```bash
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=langchain-lab
```

### If all three vars are present:

Report: "LangSmith env vars already configured." — no change to `.env`.

---

## Step 3 — Component Detection

Scan the file for these patterns. Build a detection report before transforming anything.

### Detection Table

| Component Type | Detection Signals | Tracing Method |
|---|---|---|
| LCEL chain | `\|` operator between LangChain objects, `RunnableSequence`, `.pipe()` calls | Env var only (auto-traced) + `run_name`/`tags` in config |
| LangGraph graph | `StateGraph`, `CompiledGraph`, `.compile()`, `add_node`, `add_edge` | Env var only (auto-traced) + `run_name` in invoke config |
| LangGraph node function | Function passed to `graph.add_node(...)` | Add `config: RunnableConfig` parameter + `run_name` via metadata |
| `@tool` decorated function | `@tool` decorator from `langchain_core.tools` | `@traceable` wrapping (see note on ordering) |
| Plain Python function called inside a chain or node | Function is called inside a node or chain, no LangChain decoration | `@traceable` decorator |
| Standalone function (not inside graph/chain) | Top-level function, no LangChain context | `with tracing_context(...)` wrapper in `__main__` block or `@traceable` |
| `load_dotenv()` call | Already present | No change needed |
| Missing `load_dotenv()` | `dotenv` not imported, no `load_dotenv()` | Add import and call at top of file |

---

## Step 4 — Transformation Rules

Apply these transformations. Each rule is independent — apply all that match.

---

### Rule A: LCEL Chains — Ensure Auto-Tracing is Active

LCEL chains (`prompt | llm | parser`, `.pipe()`) are auto-traced when `LANGSMITH_TRACING=true`. No code change is needed for tracing itself.

**But add `run_name` and `tags` to every `.invoke()` / `.ainvoke()` / `.stream()` call that does not already have them:**

Before:
```python
result = chain.invoke({"question": user_input})
```

After:
```python
result = chain.invoke(
    {"question": user_input},
    config={
        "run_name": "QuestionAnswerChain",
        "tags": ["lcel", "qa"],
        "metadata": {"file": __file__},
    },
)
```

**Derive `run_name` from:**
1. The variable name the chain is assigned to (e.g., `qa_chain` → `"QaChain"`)
2. If the chain is anonymous (inline), use `"LCELChain"`

**If the file has no `.invoke()` call but defines chains for export:** Add a comment above the chain definition:

```python
# LangSmith auto-traces this chain when LANGSMITH_TRACING=true.
# Pass run_name and tags in config when calling .invoke():
#   chain.invoke(input, config={"run_name": "MyChain", "tags": ["v1"]})
```

---

### Rule B: LangGraph Graphs — Ensure Auto-Tracing + run_name

LangGraph graphs are auto-traced. Add `run_name` to every `app.invoke()` / `app.ainvoke()` / `app.astream()` call:

Before:
```python
result = app.invoke({"messages": [...]}, config)
```

After:
```python
result = app.invoke(
    {"messages": [...]},
    config={
        **config,                          # preserve existing config (thread_id etc.)
        "run_name": "MyAgentRun",
        "tags": ["langgraph", "agent"],
        "metadata": {"file": __file__},
    },
)
```

If `config` is already a dict with `"configurable"`, merge carefully:

```python
config = {
    "configurable": {"thread_id": "user-123"},
    "run_name": "MyAgentRun",
    "tags": ["langgraph"],
    "metadata": {"file": __file__},
}
```

**Derive `run_name`** from the graph variable name or the file name (e.g., `react_agent.py` → `"ReactAgent"`).

---

### Rule C: LangGraph Node Functions — Add RunnableConfig

Node functions that perform meaningful work (LLM calls, tool calls, retrieval) should accept `RunnableConfig` so they can log custom metadata to their span.

**Detection:** A function is a node function if it is passed to `graph.add_node(name, fn)`.

Before:
```python
def agent_node(state: MessagesState) -> dict:
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}
```

After:
```python
from langchain_core.runnables import RunnableConfig

def agent_node(state: MessagesState, config: RunnableConfig = None) -> dict:
    # LangSmith: this node appears as a child span in the graph trace.
    # config carries run_id, tags, and metadata from the parent invoke.
    response = llm_with_tools.invoke(
        state["messages"],
        config=config,   # propagates trace context into the LLM call
    )
    return {"messages": [response]}
```

**Rules for config propagation:**
- Pass `config=config` to every `llm.invoke()` / `llm.ainvoke()` call inside the node
- Pass `config=config` to every sub-chain call inside the node
- Do NOT pass config to `ToolNode` — it handles its own tracing

---

### Rule D: Plain Python Functions — Add @traceable

Any function that is:
- Called inside a LangGraph node OR an LCEL chain step (via `RunnableLambda`)
- Performs work worth observing (API calls, data processing, retrieval, scoring)
- Is not already decorated with `@traceable` or `@tool`

Add `@traceable`:

Before:
```python
def fetch_user_context(user_id: str) -> dict:
    # database call
    return db.query(f"SELECT * FROM users WHERE id = {user_id!r}")
```

After:
```python
from langsmith import traceable

@traceable(
    name="FetchUserContext",
    tags=["db", "context"],
    metadata={"source": "users_table"},
)
def fetch_user_context(user_id: str) -> dict:
    # database call — inputs and output now visible in LangSmith
    return db.query(f"SELECT * FROM users WHERE id = {user_id!r}")
```

**@traceable naming convention:**
- Convert `snake_case` to `TitleCase` for the `name` parameter
- `fetch_user_context` → `"FetchUserContext"`
- `run_retrieval` → `"RunRetrieval"`

**Do NOT add @traceable to:**
- Functions that only do arithmetic or pure string formatting
- `__init__` methods
- Functions already decorated with `@tool` (they are already traced via ToolNode)
- The `main()` / `if __name__ == "__main__"` block (use `with_tracing_context` there instead)

---

### Rule E: @tool Functions — Preserve @tool, Note Tracing

`@tool` functions are already traced by LangChain's ToolNode. Do not add `@traceable` on top.

If the tool has no docstring (which means poor LangSmith trace names), add one:

Before:
```python
@tool
def calculate(expression: str) -> str:
    return str(eval(expression))
```

After:
```python
@tool
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression and return the result as a string."""
    return str(eval(expression))
```

Add this comment above the tool if there is none:
```python
# @tool functions are auto-traced by LangChain's ToolNode.
# The docstring appears as the run description in LangSmith.
```

---

### Rule F: Standalone / Entry-Point Functions — Wrap with tracing_context

Functions in an `if __name__ == "__main__"` block or at module entry level that call chains or graphs directly should have a named tracing context so all their child spans are grouped under one root run.

Before:
```python
if __name__ == "__main__":
    result = chain.invoke({"question": "What is LCEL?"})
    print(result)
```

After:
```python
from langsmith import trace

if __name__ == "__main__":
    with trace(
        name="ManualRun",
        project_name=os.environ.get("LANGSMITH_PROJECT", "langchain-lab"),
        tags=["manual", "dev"],
    ):
        result = chain.invoke({"question": "What is LCEL?"})
        print(result)
```

If `os` is not already imported, add `import os` to the import block.

---

### Rule G: load_dotenv — Ensure It Is Called

If `load_dotenv()` is not present anywhere in the file:

Add at the top of the file, after standard library imports and before any LangChain imports:

```python
from dotenv import load_dotenv
load_dotenv()
```

If `load_dotenv()` is already called, do nothing.

---

## Step 5 — Import Consolidation

After all transformations, consolidate imports at the top of the file. Add only what is actually used by the transformations applied.

```python
# Add only the imports that were actually needed by the transformations applied:

from dotenv import load_dotenv                          # Rule G
from langsmith import traceable, trace                  # Rule D, Rule F
from langchain_core.runnables import RunnableConfig     # Rule C
import os                                               # Rule F (for os.environ)
```

**Placement rule:** Insert new imports after the last existing `from langchain` or `from langgraph` import line. Do not move existing imports.

**Deduplication:** If the import already exists (even partially), do not add a duplicate. Merge into the existing import line if possible:

```python
# Before: from langsmith import traceable
# After adding trace: from langsmith import traceable, trace
```

---

## Step 6 — Diff and Summary Report

After all transformations are applied, output:

### Summary

```
/lc-trace results for: <filename>

Components detected:
  - 1 LCEL chain (chain)           → added run_name to .invoke() call
  - 2 LangGraph node functions     → added RunnableConfig parameter + config propagation
  - 1 LangGraph graph compile      → added run_name to app.invoke() call
  - 3 plain Python functions       → added @traceable decorator
  - 0 @tool functions              → (none found)
  - load_dotenv                    → already present

.env status:
  LANGSMITH_API_KEY  ✓ (already present)
  LANGSMITH_TRACING  ✓ (already present)
  LANGSMITH_PROJECT  added (langchain-lab)

Imports added:
  from langsmith import traceable, trace
  from langchain_core.runnables import RunnableConfig

Next step: Replace ls__YOUR_KEY_HERE in .env, then run your file.
Traces will appear at smith.langchain.com under project "langchain-lab".
```

### Diff

Show a unified diff of every change made. Use this format:

```diff
--- original
+++ traced

@@ -1,5 +1,8 @@
+from dotenv import load_dotenv
+from langsmith import traceable
+from langchain_core.runnables import RunnableConfig
+load_dotenv()
 from langchain_anthropic import ChatAnthropic
 ...

@@ -12,6 +15,13 @@
-def fetch_context(user_id: str) -> dict:
+@traceable(name="FetchContext", tags=["retrieval"])
+def fetch_context(user_id: str) -> dict:
 ...
```

---

## Transformation Priority and Conflict Resolution

When a function matches multiple rules, apply them in this order:

1. **Rule G** (load_dotenv) — always first
2. **Rule E** (@tool) — check before Rule D to avoid double-decorating
3. **Rule C** (node RunnableConfig) — before Rule D since node functions should use config propagation, not just @traceable
4. **Rule D** (@traceable) — for non-node, non-tool functions
5. **Rule A / B** (LCEL / LangGraph invoke config) — last, after function-level tracing is set up
6. **Rule F** (entry-point context manager) — very last, wraps everything

---

## Complete Transformation Example

### Input file: `rag_pipeline.py`

```python
import os
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

llm = ChatAnthropic(model="claude-sonnet-4-6")

def retrieve_docs(query: str) -> list[str]:
    # Simulated retrieval
    return ["doc1 content", "doc2 content"]

def format_context(docs: list[str]) -> str:
    return "\n".join(docs)

prompt = ChatPromptTemplate.from_messages([
    ("system", "Answer using this context: {context}"),
    ("human", "{question}"),
])

rag_chain = (
    {"context": lambda q: format_context(retrieve_docs(q)), "question": lambda q: q}
    | prompt
    | llm
    | StrOutputParser()
)

if __name__ == "__main__":
    answer = rag_chain.invoke("What is RAG?")
    print(answer)
```

### Output file: `rag_pipeline.py` (after /lc-trace)

```python
import os
from dotenv import load_dotenv
from langsmith import traceable, trace
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

llm = ChatAnthropic(model="claude-sonnet-4-6")

@traceable(name="RetrieveDocs", tags=["retrieval"])
def retrieve_docs(query: str) -> list[str]:
    # Simulated retrieval
    return ["doc1 content", "doc2 content"]

@traceable(name="FormatContext", tags=["preprocessing"])
def format_context(docs: list[str]) -> str:
    return "\n".join(docs)

prompt = ChatPromptTemplate.from_messages([
    ("system", "Answer using this context: {context}"),
    ("human", "{question}"),
])

# LangSmith auto-traces this chain when LANGSMITH_TRACING=true.
rag_chain = (
    {"context": lambda q: format_context(retrieve_docs(q)), "question": lambda q: q}
    | prompt
    | llm
    | StrOutputParser()
)

if __name__ == "__main__":
    with trace(
        name="RAGPipelineRun",
        project_name=os.environ.get("LANGSMITH_PROJECT", "langchain-lab"),
        tags=["manual", "dev"],
    ):
        answer = rag_chain.invoke(
            "What is RAG?",
            config={
                "run_name": "RagChain",
                "tags": ["lcel", "rag"],
                "metadata": {"file": __file__},
            },
        )
        print(answer)
```

---

## Complete Transformation Example 2 — LangGraph Agent

### Input file: `agent.py`

```python
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

llm = ChatAnthropic(model="claude-sonnet-4-6")

@tool
def search(query: str) -> str:
    return f"Results for {query}"

tools = [search]
llm_with_tools = llm.bind_tools(tools)
tool_node = ToolNode(tools)

def agent_node(state: MessagesState) -> dict:
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}

def should_continue(state):
    if state["messages"][-1].tool_calls:
        return "tools"
    return "end"

graph = StateGraph(MessagesState)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
graph.add_edge("tools", "agent")
app = graph.compile(checkpointer=MemorySaver())

if __name__ == "__main__":
    config = {"configurable": {"thread_id": "1"}}
    result = app.invoke({"messages": [{"role": "user", "content": "Search for Python news"}]}, config)
    print(result["messages"][-1].content)
```

### Output file: `agent.py` (after /lc-trace)

```python
import os
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

llm = ChatAnthropic(model="claude-sonnet-4-6")

# @tool functions are auto-traced by LangChain's ToolNode.
# The docstring appears as the run description in LangSmith.
@tool
def search(query: str) -> str:
    """Search the web and return results for the given query."""
    return f"Results for {query}"

tools = [search]
llm_with_tools = llm.bind_tools(tools)
tool_node = ToolNode(tools)

def agent_node(state: MessagesState, config: RunnableConfig = None) -> dict:
    # LangSmith: this node appears as a child span in the graph trace.
    response = llm_with_tools.invoke(
        state["messages"],
        config=config,  # propagates trace context into the LLM call
    )
    return {"messages": [response]}

def should_continue(state):
    if state["messages"][-1].tool_calls:
        return "tools"
    return "end"

graph = StateGraph(MessagesState)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
graph.add_edge("tools", "agent")
app = graph.compile(checkpointer=MemorySaver())

if __name__ == "__main__":
    config = {
        "configurable": {"thread_id": "1"},
        "run_name": "AgentRun",
        "tags": ["langgraph", "agent"],
        "metadata": {"file": __file__},
    }
    result = app.invoke(
        {"messages": [{"role": "user", "content": "Search for Python news"}]},
        config,
    )
    print(result["messages"][-1].content)
```

---

## Edge Cases and Handling

| Situation | Handling |
|---|---|
| File has no LangChain imports at all | Report: "No LangChain/LangGraph components detected. Add `@traceable` manually to functions you want to trace." Then show Rule D pattern. |
| File already has `@traceable` on some functions | Skip those functions, apply to the rest, report which were skipped. |
| `load_dotenv()` is in a different file (e.g., `config.py`) | Detect `from config import *` or similar. Report: "load_dotenv() found in `config.py` — skipping addition. Ensure `LANGSMITH_TRACING=true` is set before this file runs." |
| Multiple `.env` files (root vs subdirectory) | Use the `.env` closest to the Python file being traced. |
| File uses `async def` for nodes | Apply same `config: RunnableConfig = None` rule. Use `await llm.ainvoke(..., config=config)` instead of `.invoke()`. |
| `app.astream()` call | Add run_name to its config dict the same way as `app.invoke()`. |
| Chain with no variable name (inline) | Use `run_name` derived from file name: `my_file.py` → `"MyFile"`. |
| `LANGSMITH_API_KEY` already in `.env` as empty string | Treat as missing. Replace with placeholder + instructions. |
| File is a test file (`test_*.py`) | Apply tracing but use tag `"test"` instead of `"dev"`. Set `run_name` to `"TestRun_<TestFunctionName>"` for each test function that calls a chain. |

---

## Metadata Standards

When adding metadata, use these keys consistently so LangSmith dashboards can filter by them:

```python
metadata = {
    "file": __file__,               # which source file generated this run
    "function": "my_function",       # which function called invoke()
    "version": "1.0",                # optional: your app version
    "environment": os.environ.get("APP_ENV", "dev"),  # dev/staging/production
}
```

Tags follow this convention:
- Component type: `"lcel"`, `"langgraph"`, `"tool"`, `"retrieval"`
- Environment: `"dev"`, `"staging"`, `"production"`
- Feature area: `"qa"`, `"summarization"`, `"agent"`, `"rag"` (derive from file/function name)

---

## What NOT to Change

- Do not rename any functions, variables, or classes
- Do not change the logic inside any function
- Do not reorder existing code beyond adding imports at the top
- Do not add `@traceable` to `@tool` functions
- Do not add `@traceable` to functions that are trivially small (less than 3 lines, no external calls)
- Do not wrap `__init__`, `__repr__`, or dunder methods
- Do not modify the `checkpointer` setup
- Do not change `MemorySaver` to `PostgresSaver` (that is a different command's concern)
