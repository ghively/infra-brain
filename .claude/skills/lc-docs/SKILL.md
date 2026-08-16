---
name: lc-docs
description: Use when the user asks about a LangChain or LangGraph concept, API, class, pattern, or feature by name. Triggered by /lc-docs followed by a topic such as StateGraph, checkpointing, interrupt, RAG, LangSmith tracing, LCEL, ToolNode, MessagesState, or any other LangChain/LangGraph term. Use to fetch live documentation and synthesize an actionable explanation with code.
---

# lc-docs — Live LangChain/LangGraph Documentation Lookup

## Overview

Fetches current LangChain and LangGraph documentation via Context7 MCP and synthesizes a 200-500 word actionable answer: what the topic is, when to use it, how to use it (with code), common mistakes, and a source link.

Always fetches live docs. Never answer from training-data memory alone — the libraries change frequently.

---

## Usage

```
/lc-docs [topic]
```

**Examples:**
```
/lc-docs StateGraph
/lc-docs checkpointing
/lc-docs interrupt
/lc-docs RAG
/lc-docs LangSmith tracing
/lc-docs LCEL
/lc-docs ToolNode
/lc-docs MessagesState
/lc-docs send API
/lc-docs with_structured_output
```

---

## Execution Flow

Run these steps in order for every `/lc-docs` invocation.

### Step 1 — Parse Topic

Extract the topic from `$ARGUMENTS`. If no topic was provided, ask:

```
What LangChain or LangGraph topic would you like docs for?
Examples: StateGraph, checkpointing, interrupt, LCEL, ToolNode, MessagesState
```

Store the raw topic string as TOPIC.

---

### Step 2 — Resolve Library

Determine which library (or both) the topic belongs to. Use this routing table:

| Topic signals | Library to resolve |
|---|---|
| StateGraph, node, edge, checkpointing, interrupt, Send, Command, MemorySaver, PostgresSaver, subgraph, human-in-the-loop, stream_mode | LangGraph |
| LCEL, pipe operator, Runnable, RunnablePassthrough, RunnableParallel, chain, StrOutputParser, invoke, batch | LangChain Core |
| RAG, retriever, vector store, embeddings, document loader, text splitter, Chroma, Pinecone | LangChain (community + core) |
| LangSmith, tracing, evaluation, dataset, runs | LangSmith |
| @tool, ToolNode, tool_calls, bind_tools, create_react_agent | Both LangChain + LangGraph |
| Ambiguous | Resolve both, surface whichever has more relevant results |

Call `mcp__plugin_context7_context7__resolve-library-id` for the determined library.

**For LangGraph topics:**
```
libraryName: "langgraph"
```

**For LangChain topics:**
```
libraryName: "langchain"
```

**For topics spanning both** (e.g., tools, agents, RAG): resolve both libraries in parallel.

If `resolve-library-id` returns multiple candidates, pick the one whose description best matches the topic. Prefer the official `langchain-ai/langgraph` and `langchain-ai/langchain` repositories.

---

### Step 3 — Fetch Documentation

Call `mcp__plugin_context7_context7__query-docs` with:

```
context7CompatibleLibraryId: [ID from Step 2]
topic: [TOPIC]
tokens: 4000
```

If the topic spans both LangChain and LangGraph, make two parallel calls — one per library — and merge the results.

If the query returns no results or sparse results (fewer than 200 tokens), try one fallback query with a broader phrasing:

| Original | Fallback |
|---|---|
| `interrupt` | `human-in-the-loop interrupt LangGraph` |
| `send API` | `Send parallel fan-out LangGraph` |
| `MessagesState` | `messages state LangGraph graph` |
| `with_structured_output` | `structured output Pydantic LangChain` |
| `LCEL` | `LangChain Expression Language Runnable pipe` |

---

### Step 4 — Detect Ambiguity

Before synthesizing, check for ambiguity:

**Ambiguous topic signals:**
- Topic exists in both LangChain and LangGraph with different meanings (e.g., "memory" means `ConversationBufferMemory` in LangChain but checkpointing in LangGraph)
- Topic is a generic term (e.g., "streaming", "state", "tools")
- Docs from both libraries are equally relevant

**If ambiguous**, ask one clarifying question:

```
[TOPIC] applies to both LangChain and LangGraph. Which context are you asking about?

  1. LangChain — [brief description of LangChain meaning]
  2. LangGraph — [brief description of LangGraph meaning]
  3. Both — show me how they differ

Enter 1, 2, or 3:
```

Wait for the response before proceeding to Step 5.

**Known ambiguous topics and their meanings:**

| Topic | LangChain meaning | LangGraph meaning |
|---|---|---|
| memory | `ConversationBufferMemory` (deprecated), chat history | Checkpointing — state persisted per thread_id |
| streaming | `.stream()` on LCEL chains, token streaming | `stream_mode` on graphs — values/updates/messages/debug |
| state | Chain state, RunnableConfig | Typed `TypedDict` passed through graph nodes |
| tools | `@tool` decorator, tool definitions | `ToolNode` — prebuilt tool executor node |
| graph | Abstract: chain as DAG | Concrete: `StateGraph`, `add_node`, `add_edge` |
| checkpointing | N/A (does not exist) | `MemorySaver` / `PostgresSaver` — persistent state |

---

### Step 5 — Handle Unknown Topics

If the docs fetch returns no relevant content AND the topic does not appear in any documentation:

1. Say: `"I could not find documentation for '[TOPIC]'. It may be misspelled or very new."`
2. Suggest up to three closest alternatives based on token similarity:
   ```
   Did you mean one of these?
     - [suggestion 1]
     - [suggestion 2]
     - [suggestion 3]
   ```
3. If the user confirms an alternative, restart from Step 2 with the corrected topic.

---

### Step 6 — Synthesize Answer

Using the fetched documentation, write a structured answer in this exact format. Target 200-500 words total (excluding code). Never pad with filler.

---

**[TOPIC]** — [one-sentence definition]

**Library:** [LangChain / LangGraph / Both]
**Latest version covered:** [version from docs metadata if available]

---

#### What it is

[2-4 sentences. Plain English. Define the concept and its role in the ecosystem. Mention the mental model if one applies — e.g., "a StateGraph is a state machine where nodes are Python functions and edges are transitions."]

#### When to use it

[Bullet list, 3-5 items. Concrete triggering conditions.]

- Use when [specific scenario]
- Use when [specific scenario]
- Do NOT use when [anti-pattern or when an alternative is better]

#### How to use it

[Complete, runnable Python code example. 15-40 lines. Annotate with inline comments explaining the why, not just the what. Use `claude-sonnet-4-6` for any model references. Follow existing plugin conventions: `load_dotenv()` first, type annotations, no hardcoded keys.]

```python
# [topic] — minimal working example
[code]
```

#### Common mistakes

| Mistake | What goes wrong | Fix |
|---|---|---|
| [mistake] | [symptom] | [fix] |
| [mistake] | [symptom] | [fix] |

#### Source

[Direct link to the specific documentation page. Format: `https://langchain-ai.github.io/langgraph/...` or `https://python.langchain.com/docs/...`]

---

### Step 7 — Version Note

If the user's question includes a version qualifier (e.g., "in v0.2", "LangGraph 0.1", "latest"):

- Always fetch docs for the latest version available from Context7.
- If the latest version differs from what the user asked about, add this note after the answer:

```
Note: These docs reflect the current version. [TOPIC] behavior changed significantly
in [version] — [brief description of change if visible in docs].
```

---

## Output Quality Rules

These rules apply to every synthesis. No exceptions.

1. **Code must run.** Every example must be complete and importable. No `...` placeholders inside function bodies.

2. **One excellent example, not many.** Do not show the same concept in multiple languages or frameworks. Pick the most relevant pattern for the context.

3. **Inline comments explain why.** Comments like `# appends rather than replaces` are good. Comments like `# this is a function` are not.

4. **Use plugin conventions.**
   - Default model: `claude-sonnet-4-6` via `langchain-anthropic`
   - Always `load_dotenv()` before LangChain imports
   - LangSmith tracing shown when relevant
   - LCEL pipe syntax for any chain composition

5. **Never answer from memory alone.** If Context7 returns no results, say so and offer to search again. Do not synthesize from training data.

6. **Concise over comprehensive.** If the topic is complex (e.g., "agents"), focus on the single most actionable starting point and link to deeper docs.

---

## Quick Reference: Common Topics

| Topic | Library | Core concept |
|---|---|---|
| `StateGraph` | LangGraph | Graph builder — `add_node`, `add_edge`, `compile()` |
| `MessagesState` | LangGraph | Built-in state with `add_messages` reducer |
| `MemorySaver` | LangGraph | In-process checkpointer for dev; not persistent |
| `PostgresSaver` | LangGraph | Persistent checkpointer for production |
| `interrupt()` | LangGraph | Pause graph execution for human approval |
| `Command` | LangGraph | Resume after interrupt; update state on resume |
| `Send` | LangGraph | Fan-out: spawn N parallel node invocations |
| `ToolNode` | LangGraph | Prebuilt node that executes `tool_calls` |
| `create_react_agent` | LangGraph | Prebuilt ReAct graph — fastest agent scaffold |
| `LCEL` | LangChain | `prompt \| model \| parser` pipe composition |
| `RunnablePassthrough` | LangChain | Pass input unchanged while parallel branches run |
| `with_structured_output` | LangChain | Bind Pydantic schema to model, get typed output |
| `@tool` | LangChain | Decorator turning any function into an LLM-callable tool |
| `ChatPromptTemplate` | LangChain | Reusable message template with variable injection |
| LangSmith tracing | LangSmith | Set `LANGSMITH_TRACING=true` — zero code changes needed |

---

## Error Handling

| Situation | Response |
|---|---|
| Context7 resolve returns no match | Say "Could not find library '[name]' in Context7. Trying 'langchain' as fallback." then retry. |
| Context7 query returns empty | Try fallback query (see Step 3). If still empty, say so and do not hallucinate. |
| Topic is ambiguous | Ask the one-question disambiguation from Step 4. |
| Topic is unknown | Offer three closest alternatives from Step 5. |
| Network/MCP error | Report the error, offer to retry, do not generate from memory. |

---

## See Also

- `lc:agent` — scaffold a complete LangGraph agent
- `lc:rag` — RAG patterns from naive to agentic
- `lc:debug` — debug LangChain/LangGraph errors
- `lc:memory` — checkpointing and memory patterns
- `lc:lcel` — LCEL chain composition patterns
