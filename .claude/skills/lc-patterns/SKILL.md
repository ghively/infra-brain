---
name: lc-patterns
description: Use when a user wants to know which LangChain/LangGraph pattern to use for their use case. Takes a natural-language description and returns: the right pattern with rationale, an ASCII architecture diagram, key components, complexity estimate (1-5), and the next skill to invoke. Handles ambiguous descriptions with clarifying questions, multi-pattern compositions, and over-engineered requests with simpler alternatives. Triggered by /lc-patterns or phrases like "what pattern should I use", "which LangGraph approach", "how do I build X with LangChain".
---

# lc-patterns — LangChain/LangGraph Pattern Recommender

## Overview

This skill analyzes a plain-English use-case description and maps it to the
correct LangChain/LangGraph architecture pattern. It teaches **why** a pattern
fits before showing **what** to build. The output is always actionable: it
ends with the exact skill to invoke next.

**Usage:** `/lc-patterns [description]`

**Example:**
```
/lc-patterns "I want to build an assistant that answers questions about my
PDF documents and remembers past conversations"
```

---

## Skill Flow

```
1. Receive description
       │
       ▼
2. Check for ambiguity  ──(ambiguous)──► Ask 1-3 clarifying questions → restart
       │
   (clear enough)
       ▼
3. Run signal detection (see Decision Matrix)
       │
       ▼
4. Resolve pattern(s) — single or composition
       │
       ▼
5. Check complexity vs. description — simplify if over-engineered
       │
       ▼
6. Output structured recommendation
```

Always complete the full flow. Never output partial recommendations.

---

## Step 1 — Ambiguity Check

Before running the decision matrix, check if the description gives enough
signal to route confidently. If ANY of the following are true, ask the
clarifying question(s) before proceeding:

| Ambiguity | Clarifying question |
|-----------|---------------------|
| No mention of data/docs/tools/tasks | "What should the assistant be able to _do_? (e.g., answer questions from documents, call APIs, write and run code, do research)" |
| Unclear if memory is needed | "Should it remember previous conversations, or is each session independent?" |
| Unclear output type | "What does a successful response look like — a short answer, a long document, a series of actions, or something else?" |
| Very broad (e.g., "build an AI") | "Can you describe a concrete example of someone using it — what do they type in, what comes back?" |

Ask at most 3 clarifying questions in a single message. Do not ask more.
Format them as a numbered list. Do not scaffold anything until you have
answers.

**Exception:** If the description is rich enough that you can make a
high-confidence recommendation, proceed and note any assumptions you made.

---

## Step 2 — Signal Detection

Scan the description for these signals. Each signal votes for one or more
patterns. Tally the votes.

| Signal phrase / concept | Pattern vote(s) |
|-------------------------|-----------------|
| documents, PDFs, files, knowledge base, corpus, index | RAG |
| remember, history, past conversations, context, sessions | Checkpointing |
| multi-step, plan, research and write, complex task | Plan-and-Execute |
| multiple agents, specialists, roles (researcher + writer) | Supervisor |
| approve, review, human in the loop, confirm before | Human-in-the-loop (interrupt) |
| many items, batch, parallel, simultaneously, 100 X | Parallel (Send API) |
| improve quality, self-critique, revise, iterate, refine | Reflection |
| simple question, quick answer, one-shot, no memory | LCEL Chain |
| tool, API, search, calculator, code execution | ReAct |
| real-time, stream, token-by-token, typewriter | Streaming |
| long-running, background, fire and forget | Plan-and-Execute |

**Combination rules:**
- RAG + Checkpointing → "Conversational RAG" (RAG inside a LangGraph agent with thread memory)
- RAG + ReAct → "Agentic RAG" (agent decides when to retrieve vs. answer from memory)
- Supervisor + RAG → each specialist can have its own retriever
- Any pattern + Streaming → add `astream()` to whatever is built
- Any pattern + Human-in-the-loop → add `interrupt_before=` to compiled graph

---

## Step 3 — Pattern Resolution Table

Use the vote tallies from Step 2 to select the primary pattern and any
secondary modifiers.

### Primary Patterns

| Pattern | Select when | Complexity |
|---------|-------------|------------|
| LCEL Chain | No tools, no memory, simple transform or Q&A, 1-2 steps | 1 |
| ReAct Agent | Tools needed, single domain, no upfront plan required | 2 |
| Conversational RAG | Documents + memory, standard chat-over-docs | 3 |
| Agentic RAG | Documents + tools + multi-step reasoning | 3 |
| Reflection | Output quality critical, iterative refinement loop | 3 |
| Plan-and-Execute | Multi-step, long-running, needs upfront planning | 4 |
| Parallel (Send API) | Batch processing N independent items | 3 |
| Supervisor | Multiple domains, 3+ specialized roles | 4 |
| Supervisor + RAG | Multiple domains each with their own knowledge base | 5 |

### Secondary Modifiers (add to any primary pattern)

| Modifier | Add when |
|----------|----------|
| Checkpointing | "remember across sessions" signal present |
| Streaming | "real-time output" or "stream" signal present |
| Human-in-the-loop | "approve" / "human review" signal present |

### Simplification Rule

If the description maps to a complexity-4+ pattern but the use case is
simple (1-2 example interactions, single user, prototype/demo), recommend
the simpler pattern and explain why. State explicitly:

> "Your description could be built with [complex pattern], but [simpler
> pattern] covers your use case and is far easier to maintain. I recommend
> starting with [simpler] — you can always add [complexity] later."

Examples:
- "chat over one PDF" → LCEL Chain, not Supervisor
- "answer questions from a database with memory" → Conversational RAG, not Plan-and-Execute
- "summarize 3 documents" → LCEL Chain with RunnableParallel, not full Parallel Send API

---

## Step 4 — Output Format

Always produce all six sections in order. Do not skip sections.

---

### Section 1 — Recommended Pattern

```
## Recommended Pattern: [Pattern Name]

[2-3 sentence plain-English explanation of what this pattern IS and why
it exists. No code. No jargon without definition.]
```

If multiple patterns compose:
```
## Recommended Pattern: [Primary] + [Modifier]

[Explain each component in 1 sentence, then explain how they combine.]
```

---

### Section 2 — Why This Pattern Fits

```
## Why This Pattern Fits Your Use Case

[Map each detected signal from the description to the pattern component
that handles it. Use a bullet list:]

- "answers questions about PDF documents" → RAG component retrieves relevant
  chunks before the LLM answers
- "remembers past conversations" → LangGraph checkpointer stores message
  history keyed by thread_id
- [any other signals]

[If you made assumptions because the description was ambiguous, state them:]
Assumptions made:
- Assumed single user (not multi-tenant) → MemorySaver is sufficient
- Assumed English documents → no special tokenization needed
```

---

### Section 3 — Architecture Overview

Produce an ASCII diagram. Tailor it to the specific pattern(s) recommended.
Use these canonical diagram templates as starting points, then adapt.

**LCEL Chain:**
```
User Input
    │
    ▼
[Prompt Template]
    │
    ▼
[ChatAnthropic]
    │
    ▼
[Output Parser]
    │
    ▼
Response
```

**ReAct Agent:**
```
User Input
    │
    ▼
[agent node] ──tool_calls──► [ToolNode]
    ▲                              │
    └──────────────────────────────┘
    │
    ▼ (no tool_calls)
Response
```

**Conversational RAG:**
```
User Input + thread_id
    │
    ▼
[History-Aware Retriever] ── reformulates query using history
    │
    ▼
[Vector Store] ── retrieves relevant chunks
    │
    ▼
[Prompt: context + history + question]
    │
    ▼
[ChatAnthropic]
    │
    ▼
[Checkpointer saves to memory/DB]
    │
    ▼
Response
```

**Agentic RAG:**
```
User Input
    │
    ▼
[ReAct Agent]
    │
    ├──retrieve──► [Retriever Tool] ──► Vector Store
    ├──search───► [Web Search Tool]
    └──calculate► [Code Tool]
    │
    ▼ (answer found)
Response
```

**Reflection:**
```
User Input
    │
    ▼
[generate node] ──► Draft
    │
    ▼
[critic node] ──► Score + Critique
    │
    ├── score < 8 AND iter < MAX ──► [revise node] ──┐
    │                                                  │
    └── score >= 8 OR iter == MAX ◄────────────────────┘
    │
    ▼
[finalize node]
    │
    ▼
Final Output
```

**Plan-and-Execute:**
```
User Input
    │
    ▼
[planner node] ──► Plan: [step1, step2, step3, ...]
    │
    ▼
[executor node] ──► Executes step1 using tools
    │
    ▼
[replanner node]
    │
    ├── more steps needed ──► revised plan ──► [executor node]
    │
    └── task complete ──► Response
```

**Parallel (Send API):**
```
Input: [item1, item2, ..., itemN]
    │
    ▼
[fan-out node] ── Send("worker", item1) ──► [worker node]
               ── Send("worker", item2) ──► [worker node]  (parallel)
               ── Send("worker", itemN) ──► [worker node]
    │
    ▼ (all workers complete, results aggregated via operator.add)
[aggregate node]
    │
    ▼
Final Result
```

**Supervisor:**
```
User Input
    │
    ▼
[Supervisor LLM]
    │
    ├── transfer_to_research_agent ──► [Research Agent] ──► result
    ├── transfer_to_analyst_agent  ──► [Analyst Agent]  ──► result
    └── transfer_to_writer_agent   ──► [Writer Agent]   ──► result
    │
    ▼ (all subtasks done)
[Supervisor synthesizes]
    │
    ▼
Response
```

Add secondary modifiers to the diagram:
- Checkpointing: add `[Checkpointer: MemorySaver/PostgresSaver]` as a side
  node connected to the graph with a dashed line labeled "saves state"
- Streaming: add `astream()` annotation on the final output arrow
- Human-in-the-loop: add `[interrupt()]` node before any tool/action node
  with `[Human Approval]` branching to Resume or Cancel

---

### Section 4 — Key Components Needed

List only the components actually needed for this specific recommendation.
Group by install source.

```
## Key Components

### Python packages
pip install [exact packages needed — no extras]

### LangChain / LangGraph components
- [ClassName] from [module] — [one-line purpose]
- ...

### External services (if needed)
- [Service name]: [purpose] — [where to get credentials]

### Environment variables
ANTHROPIC_API_KEY=        # required — model calls
LANGSMITH_API_KEY=        # recommended — tracing (free at smith.langchain.com)
LANGSMITH_TRACING=true
[any pattern-specific vars]
```

---

### Section 5 — Estimated Complexity

```
## Complexity: [N]/5

[One sentence stating N and what it means for this project]

Breakdown:
- Setup: [Easy/Medium/Hard] — [reason]
- Core logic: [Easy/Medium/Hard] — [reason]  
- Testing: [Easy/Medium/Hard] — [reason]
- Production readiness: [Easy/Medium/Hard] — [reason]

[If N >= 4, add:]
To reduce complexity, consider: [specific simplification]
```

Complexity scale:
- 1: Single LCEL chain, 1-2 files, no external services
- 2: ReAct agent with 2-4 tools, basic memory
- 3: RAG pipeline OR multi-step graph, vector store required
- 4: Multiple agents OR Plan-and-Execute, persistent storage required
- 5: Supervisor + RAG, multi-tenant, production deployment

---

### Section 6 — Next Step

```
## Next Step

Run: /[skill-name]

[One sentence on what that skill will do from this point.]
```

Skill routing table:

| Primary pattern | Next skill |
|----------------|------------|
| LCEL Chain | `/lc-lcel` |
| ReAct Agent (no docs) | `/lc-agent` |
| Conversational RAG | `/rag` |
| Agentic RAG | `/rag` then `/lc-agent` |
| Reflection | `/lc-agent` |
| Plan-and-Execute | `/lc-agent` |
| Parallel (Send API) | `/lc-agent` |
| Supervisor | `/lc-agent` |
| Any pattern needing memory design | `/lc-memory` |
| Any pattern needing tool design | `/lc-tools` |
| Any pattern needing testing | `/lc-test` |

If the next step is a sequence (e.g., `/rag` then `/lc-agent`), list both
in order with a brief note on what each covers.

---

## Step 5 — Composition Guidance

When two or more primary patterns are needed, explain how they compose
**before** the architecture diagram. Use this template:

```
## Pattern Composition

This use case requires two patterns working together:

1. **[Pattern A]** handles [specific concern from description]
2. **[Pattern B]** handles [specific concern from description]

They compose by: [one sentence on the integration point — e.g., "the RAG
retriever runs inside a LangGraph node, so the agent can choose when to
retrieve vs. answer from conversation history"].

The architecture diagram below shows the combined system.
```

Common valid compositions and their integration points:

| Composition | Integration point |
|-------------|-------------------|
| RAG + Checkpointing | Retriever runs inside a LangGraph node; checkpointer saves message history |
| RAG + ReAct | Retriever is a tool in the agent's tool list |
| Supervisor + RAG | Each specialist agent has its own retriever tool |
| Any + Streaming | `astream(stream_mode="messages")` on the compiled graph |
| Any + Human-in-the-loop | `interrupt_before=["node_name"]` on `graph.compile()` |
| Reflection + RAG | Critic can retrieve reference material to ground its critique |

---

## Step 6 — Anti-Pattern Detection

Before finalizing the recommendation, check for these over-engineering traps.
If one matches, recommend the simpler approach and explain the trade-off.

| Over-engineered request | Simpler recommendation |
|------------------------|------------------------|
| Supervisor for a single domain | ReAct with multiple tools — supervisors add latency and complexity for no benefit with one domain |
| Plan-and-Execute for 1-2 step tasks | ReAct — plan-and-execute shines for 5+ interdependent steps |
| Parallel Send API for 2-3 items | `RunnableParallel` in LCEL — Send API overhead not worth it below ~10 items |
| Reflection loop for factual Q&A | Single ReAct pass — reflection helps with writing quality, not factual lookup |
| Full Agentic RAG for static FAQ | Naive RAG LCEL chain — if documents don't change and queries are simple, skip the agent overhead |
| PostgresSaver for a prototype | MemorySaver — add PostgresSaver when you actually need cross-session persistence |
| Supervisor + RAG for a single PDF | LCEL chain with one retriever — the simplest thing that works |

When an anti-pattern is detected, output:

```
## Simplification Opportunity

Your description maps to [complex pattern], but [simpler pattern] handles
your use case with significantly less complexity.

[Complex pattern] is the right choice when: [1-2 specific conditions].
Your use case [does/does not] meet those conditions because [reason].

I recommend [simpler pattern]. You can always migrate to [complex pattern]
later if you outgrow it — the skills follow the same LangGraph primitives.

[Continue with recommendation for the simpler pattern]
```

---

## Full Example Walkthrough

**Input:**
```
/lc-patterns "I want to build an assistant that answers questions about my
PDF documents and remembers past conversations"
```

**Signal detection:**
- "answers questions about my PDF documents" → RAG vote
- "remembers past conversations" → Checkpointing vote
- No tools mentioned beyond retrieval → no ReAct vote
- No multi-step planning → no Plan-and-Execute vote

**Pattern resolution:** Conversational RAG (RAG + Checkpointing), complexity 3.

**Anti-pattern check:** No over-engineering detected — both signals are
legitimate requirements.

**Full output:**

---

## Recommended Pattern: Conversational RAG

Conversational RAG combines a vector retrieval system with LangGraph's
checkpointing to produce a document-grounded chatbot that remembers context
across turns. The retriever reformulates each new question using prior
conversation history before searching, so "tell me more about that" works
correctly even though the vector store has no concept of "that."

---

## Why This Pattern Fits Your Use Case

- "answers questions about PDF documents" → RAG component: PDFs are chunked,
  embedded, and stored in a vector store; at query time the most relevant
  chunks are retrieved and injected into the LLM prompt as context
- "remembers past conversations" → LangGraph checkpointer: every message
  exchange is saved under a `thread_id`; on the next turn the full history
  is loaded and used to reformulate the retrieval query

Assumptions made:
- Single user or low-traffic (MemorySaver sufficient for dev; swap to
  PostgresSaver before going to production with multiple users)
- PDFs are pre-loaded, not uploaded at runtime

---

## Architecture Overview

```
User Input + thread_id
    │
    ▼
[History-Aware Retriever]
    │  rewrites query using conversation history
    │  e.g., "tell me more" → "tell me more about [topic from history]"
    ▼
[Chroma / FAISS Vector Store]
    │  returns top-k relevant chunks from PDFs
    ▼
[Prompt: system + retrieved context + message history + question]
    │
    ▼
[ChatAnthropic claude-sonnet-4-6]
    │
    ▼
[Checkpointer] ──saves──► MemorySaver (dev) / PostgresSaver (prod)
    │
    ▼
Response streamed to user
```

---

## Key Components

### Python packages
```
pip install langgraph langchain-anthropic langchain-community \
            chromadb pypdf langchain-text-splitters
```

### LangChain / LangGraph components
- `PyPDFLoader` from `langchain_community.document_loaders` — load PDF pages
- `RecursiveCharacterTextSplitter` from `langchain_text_splitters` — chunk documents
- `Chroma` from `langchain_community.vectorstores` — local vector store (dev)
- `create_history_aware_retriever` from `langchain.chains` — query reformulation
- `create_retrieval_chain` from `langchain.chains` — RAG chain
- `MemorySaver` from `langgraph.checkpoint.memory` — in-process checkpointer
- `StateGraph`, `MessagesState` from `langgraph.graph` — graph scaffolding
- `ChatAnthropic` from `langchain_anthropic` — LLM

### Environment variables
```
ANTHROPIC_API_KEY=          # required
LANGSMITH_API_KEY=          # recommended (free at smith.langchain.com)
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=pdf-chat
```

---

## Complexity: 3/5

A well-understood pattern with good tooling, but requires setting up a
vector store and understanding the history-aware retrieval step.

Breakdown:
- Setup: Medium — vector store + PDF loading pipeline
- Core logic: Medium — history-aware retriever adds one extra LLM call
- Testing: Easy — deterministic with fixed documents
- Production readiness: Medium — swap MemorySaver for PostgresSaver,
  add a hosted vector store (Pinecone, Weaviate, or pgvector)

---

## Next Step

Run: `/rag`

The `/rag` skill will ask four questions about your document type, query
complexity, accuracy requirements, and deployment target, then scaffold
the complete Conversational RAG implementation with tests and LangSmith
tracing.

---

## Edge Cases and Special Handling

### No description provided

If `/lc-patterns` is invoked with no arguments:

```
To recommend the right LangChain/LangGraph pattern, describe what you want
to build. Include:

- What data or knowledge the system works with (documents, APIs, databases)
- What kinds of questions or tasks users will ask it to do
- Whether it needs to remember past conversations
- Whether humans need to approve actions before they happen

Example: /lc-patterns "A customer support bot that searches our help docs
and escalates to a human agent when it is not confident"
```

### "I don't know what I want" input

If the description is too vague to detect any signals (e.g., "build an AI
assistant" with no further context), ask the four canonical questions from
the ambiguity check in a single message. Do not guess.

### Request that matches no pattern

If the description describes something outside LangChain/LangGraph's scope
(e.g., "train a custom LLM", "build a mobile app"), say so clearly and
redirect:

```
This use case ([description]) is outside the scope of LangChain/LangGraph
patterns, which handle LLM application orchestration rather than [model
training / mobile development / etc.].

For LangChain/LangGraph, the closest relevant starting point would be
[nearest applicable pattern] if you want to [related in-scope task].
```

### Multiple valid patterns at equal confidence

When two patterns tie (same number of signal votes, comparable complexity),
present both as options rather than guessing:

```
## Two Viable Patterns

Your use case fits two patterns equally well. The right choice depends on
one factor: [specific differentiating question].

**Option A: [Pattern]**
Choose this if: [condition]
Trade-off: [what you gain vs. lose]

**Option B: [Pattern]**
Choose this if: [condition]
Trade-off: [what you gain vs. lose]

Which fits your situation?
```

---

## Quick Pattern Reference Card

Output this table at the end of every recommendation as a collapsible
reference (use a horizontal rule separator):

```
---
### Pattern Quick Reference

| Pattern           | Best for                              | Complexity | Next skill    |
|-------------------|---------------------------------------|------------|---------------|
| LCEL Chain        | Simple transform, one-shot Q&A        | 1/5        | /lc-lcel      |
| ReAct             | Tools + single domain                 | 2/5        | /lc-agent     |
| Conversational RAG| Chat over documents with memory       | 3/5        | /rag          |
| Agentic RAG       | Docs + tools + multi-step reasoning   | 3/5        | /rag          |
| Reflection        | Quality-critical output, self-critique| 3/5        | /lc-agent     |
| Parallel Send API | Batch N independent items             | 3/5        | /lc-agent     |
| Plan-and-Execute  | Long multi-step, upfront planning     | 4/5        | /lc-agent     |
| Supervisor        | Multiple specialist domains           | 4/5        | /lc-agent     |
| Supervisor + RAG  | Multiple domains + knowledge bases    | 5/5        | /lc-agent     |
```
