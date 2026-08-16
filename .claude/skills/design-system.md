# lc:design-system — LangChain System Design Interview

## Purpose

This is a JOURNEY LAYER skill. It conducts a structured technical interview with the user, selects the best LangChain/LangGraph architectural pattern for their use case, and writes a complete spec document to `docs/specs/YYYY-MM-DD-<system-name>.md`. When the spec is finished it routes to the `writing-plans` skill.

Do not write any code. Do not scaffold any files. The sole output of this skill is a spec document and a transition to planning.

---

## Phase 1 — Open the Interview

Greet the user and set expectations:

> "Let's design your LangChain system before writing a single line of code. I'll ask you eight focused questions. Your answers determine the architecture, the LangGraph pattern, the memory strategy, and the deployment target. At the end I'll write a complete spec document you can hand to an engineer (or to the next skill)."

Then ask **Question 1** and wait for the answer before asking the next question. Do not batch all questions into a single message.

---

## Phase 2 — The Eight Questions

Work through these questions one at a time. After each answer, reflect it back in one sentence to confirm understanding, then proceed to the next question.

### Q1 — Core Purpose
> "In one sentence, what does this system do? Describe the user-facing outcome, not the technology."

Capture: `system_description`

### Q2 — Data Sources
> "What data does the system work with? List all sources: uploaded documents, live APIs, databases, web scraping, user input only, or something else."

Capture: `data_sources[]` — classify each as: `documents`, `structured_db`, `live_api`, `web`, `user_input_only`

### Q3 — Cross-Session Memory
> "Does the system need to remember things between separate conversations or user sessions? For example: user preferences, prior decisions, ongoing task state."

Capture: `needs_persistence` (boolean). If yes, capture: `memory_scope` — `user_level`, `session_level`, or `task_level`.

### Q4 — External Tool Calls
> "Does the system need to call external tools or APIs during a run? For example: run a search, execute code, call a REST endpoint, send an email, query a database."

Capture: `needs_tools` (boolean). If yes, list: `tools[]` with name and a one-line description of each.

### Q5 — Document / Knowledge Base
> "Does the system need to retrieve information from a document collection or knowledge base to answer questions or complete tasks?"

Capture: `needs_rag` (boolean). If yes, capture: `rag_corpus_description`, `approximate_doc_count`, `update_frequency` (static/periodic/real-time).

### Q6 — Human Approval Gates
> "Are there steps where a human must review or approve the system's output before it continues? For example: approve a drafted email, confirm a purchase, review a generated report."

Capture: `needs_hitl` (boolean). If yes, list: `approval_points[]` — describe each checkpoint.

### Q7 — Scale
> "How many concurrent users or sessions do you expect? Give a rough order of magnitude: single user (1), small team (2–20), departmental (20–200), or public-facing (200+)."

Capture: `scale_tier` — `single`, `team`, `departmental`, `public`.

### Q8 — LLM Provider
> "Which LLM provider will you use? Options: Anthropic (Claude), OpenAI (GPT), a local/open-source model (Ollama, vLLM, etc.), or undecided."

Capture: `llm_provider` — `anthropic`, `openai`, `local`, `undecided`.

---

## Phase 3 — Pattern Selection Decision Tree

After collecting all answers, select the primary architectural pattern using this decision tree. Apply it top-to-bottom; the first matching branch wins.

```
START
│
├─ needs_hitl == true
│   └─ → PATTERN: LangGraph with interrupt()
│       (add whichever sub-pattern below also applies, as a base layer)
│
├─ scale_tier == "public" AND (needs_tools OR needs_rag)
│   └─ → PATTERN: Plan-and-Execute + Checkpointing
│
├─ multiple specialized sub-tasks detected in system_description
│   (keywords: "route", "delegate", "specialized", "different roles")
│   └─ → PATTERN: Supervisor Multi-Agent
│
├─ needs_tools == true AND needs_rag == true
│   └─ → PATTERN: Agentic RAG
│       (ReAct agent with retrieval tool + other tools in LangGraph)
│
├─ needs_tools == true AND needs_rag == false
│   └─ → PATTERN: ReAct Agent (LangGraph StateGraph)
│
├─ needs_tools == false AND needs_rag == true
│   └─ → PATTERN: RAG Chain (LCEL retrieval chain)
│       upgrade to Agentic RAG if multi-hop retrieval likely
│
├─ needs_tools == false AND needs_rag == false AND needs_persistence == true
│   └─ → PATTERN: LCEL Chain + LangGraph Checkpointer
│
└─ needs_tools == false AND needs_rag == false AND needs_persistence == false
    └─ → PATTERN: Simple LCEL Chain
        (prompt | model | output_parser)
```

### Pattern Summaries

| Pattern | Key LangChain primitives |
|---|---|
| Simple LCEL Chain | `ChatPromptTemplate`, `BaseChatModel`, `StrOutputParser` / Pydantic parser |
| RAG Chain | `RecursiveCharacterTextSplitter`, `VectorStore`, `create_retrieval_chain` |
| ReAct Agent | `StateGraph`, `ToolNode`, `create_react_agent` |
| Agentic RAG | `StateGraph`, `ToolNode`, retriever-as-tool, `create_react_agent` |
| Supervisor Multi-Agent | `StateGraph`, `Command`, supervisor node routing to worker subgraphs |
| Plan-and-Execute + Checkpointing | `StateGraph`, planner node, executor node, `MemorySaver` / Postgres checkpointer |
| LangGraph with interrupt() | `interrupt()`, `Command(resume=...)`, any of the above as base |

---

## Phase 4 — Memory Architecture Selection

Select memory approach based on `needs_persistence`, `scale_tier`, and the chosen pattern.

```
needs_persistence == false
  → No checkpointer needed. Stateless invocations.

needs_persistence == true AND scale_tier IN ("single", "team")
  → MemorySaver (in-process, dev/low-scale)
  → thread_id = session or user identifier

needs_persistence == true AND scale_tier IN ("departmental", "public")
  → AsyncPostgresSaver (LangGraph Platform) or RedisSaver
  → thread_id = user_id + session_id composite key
  → Consider namespace isolation per user

needs_rag == true
  → Short-term: message history in graph State
  → Long-term knowledge: VectorStore (Chroma for local, Pinecone/pgvector for prod)
  → Semantic cache for repeated queries (GPTCache / LangChain cache layer)
```

---

## Phase 5 — Deployment Target Selection

```
scale_tier == "single"
  → Local CLI script or Jupyter notebook
  → No server needed

scale_tier == "team" AND needs_hitl == false
  → LangServe (FastAPI wrapper around LCEL/graph)
  → Single container deployment

scale_tier == "team" AND needs_hitl == true
  → LangGraph Platform (Studio + API server)
  → Supports interrupt/resume lifecycle

scale_tier IN ("departmental", "public")
  → LangGraph Platform (managed) or self-hosted LangGraph Server
  → Postgres checkpointer, horizontal scaling
  → Add LangSmith tracing for observability
```

---

## Phase 6 — Write the Spec Document

### File naming

```
docs/specs/YYYY-MM-DD-<kebab-case-system-name>.md
```

Use today's date. Derive `<system-name>` from `system_description` (3–5 word slug).

Create the `docs/specs/` directory if it does not exist.

### Spec Template

Fill in every section. Do not leave placeholders. If a section is not applicable (e.g., no RAG), write "N/A — not required for this system" and explain why.

---

```markdown
# System Spec: <System Name>

**Date:** YYYY-MM-DD
**Status:** Draft
**Pattern:** <selected pattern>
**LLM Provider:** <provider>

---

## 1. System Overview

<2–4 sentences. What the system does, who uses it, what problem it solves.>

**Core user journey:**
1. <step 1>
2. <step 2>
3. <step 3>

---

## 2. Architecture Diagram (ASCII)

```
<ASCII diagram showing nodes, edges, data stores, and external services.>

Example layout for a ReAct agent:

  User Input
      │
      ▼
  ┌─────────────────┐
  │   Agent Node    │◄──────────────┐
  │  (LLM + Tools)  │               │
  └────────┬────────┘               │
           │ tool call?             │ tool result
           ▼                       │
  ┌─────────────────┐               │
  │   Tool Node     │───────────────┘
  │ (ToolExecutor)  │
  └─────────────────┘
           │ END
           ▼
     Final Response
```

---

## 3. State Schema

```python
from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages

class <SystemName>State(TypedDict):
    messages: Annotated[list, add_messages]
    # Add fields specific to this system:
    # user_id: str
    # retrieved_docs: list[str]
    # plan: list[str]
    # current_step: int
    # approval_status: Literal["pending", "approved", "rejected"]
```

**Field descriptions:**

| Field | Type | Purpose |
|---|---|---|
| messages | list[BaseMessage] | Full conversation history, reduced with add_messages |
| <field> | <type> | <purpose> |

---

## 4. Components

### 4a. Nodes (LangGraph)

| Node Name | Responsibility | Inputs from State | Outputs to State |
|---|---|---|---|
| <node> | <what it does> | <fields read> | <fields written> |

### 4b. Chains (LCEL)

| Chain Name | Template / Prompt | Parser | Called By |
|---|---|---|---|
| <chain> | <prompt summary> | <parser> | <node or user> |

### 4c. Tools

| Tool Name | Type | Description | Returns |
|---|---|---|---|
| <tool> | StructuredTool / retriever / API | <description> | <return type> |

### 4d. Edges

```
<node_a> → <node_b>  [condition: <if applicable>]
<node_b> → END       [condition: no more tool calls]
```

---

## 5. Data Flow

Narrative walk-through of a single successful request from user input to final response.

1. User sends: "<example input>"
2. <describe what happens at each node>
3. <describe any retrieval, tool calls, or approvals>
4. System returns: "<example output>"

---

## 6. Memory Architecture

**Checkpointer:** <MemorySaver | AsyncPostgresSaver | RedisSaver | None>
**Thread ID strategy:** <how thread_id is constructed>
**Namespace strategy:** <how users are isolated, if applicable>
**Vector store:** <Chroma | Pinecone | pgvector | None>
**Embedding model:** <model name and provider>
**Retrieval strategy:** <similarity search | MMR | hybrid BM25+vector>
**Cache layer:** <semantic cache config, or None>

---

## 7. Deployment Target

**Environment:** <local | LangServe | LangGraph Platform | self-hosted LangGraph Server>
**Entry point:** <file path and function, e.g., `src/graph.py::graph.compile()`>
**Config / environment variables required:**

| Variable | Purpose | Example value |
|---|---|---|
| LANGCHAIN_API_KEY | LangSmith tracing | `lsv2_...` |
| <VAR> | <purpose> | <example> |

**LangSmith tracing:** <enabled | disabled>
**Scaling notes:** <concurrency model, horizontal scaling approach>

---

## 8. Testing Strategy

### Unit tests
- Test each node in isolation by passing a mock State dict.
- Test each tool function with mocked external calls.

### Integration tests
- Run the full graph against a fixed thread_id with deterministic inputs.
- Assert final state fields match expected values.

### Evaluation (LangSmith datasets)
- Create an eval dataset of <N> input/output pairs.
- Metrics: correctness (LLM-as-judge), latency (p50/p95), token cost.

### Human-in-the-loop testing
- <If applicable: describe how to exercise the interrupt/resume path in tests.>

---

## 9. Open Questions

List any unresolved decisions that the engineer will need to answer before or during implementation.

- [ ] <Question 1>
- [ ] <Question 2>

---

## 10. Implementation Order (seed for writing-plans)

Suggested order of work to minimize integration risk:

1. Scaffold project structure and install dependencies.
2. Define State schema.
3. Implement and unit-test each node/chain individually.
4. Wire nodes into a graph; test with MemorySaver.
5. Add tools and test tool calls in isolation.
6. <If RAG:> Build ingestion pipeline; test retrieval quality.
7. <If HITL:> Implement interrupt/resume; test approval flow.
8. Swap MemorySaver for production checkpointer; load-test.
9. Add LangSmith tracing and build eval dataset.
10. Deploy to target environment.
```

---

## Phase 7 — Confirm and Route

After writing the spec file, output the following summary to the user:

```
Spec written to: docs/specs/YYYY-MM-DD-<system-name>.md

Summary of decisions:
  Pattern:     <pattern name>
  Memory:      <checkpointer choice>
  Deployment:  <target>
  LLM:         <provider>
  Open items:  <count> question(s) flagged for the engineer

Next step: routing to writing-plans to break the spec into an implementation plan.
```

Then invoke the `writing-plans` skill, passing the spec file path as context:

> "I have a completed spec at `docs/specs/YYYY-MM-DD-<system-name>.md`. Please read it and produce an implementation plan."

---

## Guardrails and Edge Cases

**If the user's answers are ambiguous:** Ask one targeted clarifying question before moving on. Do not guess at scope.

**If the user describes a system that maps to multiple patterns:** Choose the one that covers the most requirements. Add a note in Section 9 (Open Questions) explaining the trade-off with the alternative pattern.

**If the user says "I don't know" to the LLM provider question:** Default to `anthropic` (Claude Sonnet) in the spec. Add an open question noting the choice is provisional.

**If the user asks to skip questions:** Remind them that each question directly controls a major architecture decision. Offer to give a default assumption (state it explicitly) and let them correct it rather than skipping.

**If `needs_hitl == true` and `scale_tier == "single"`:** Note in the spec that LangGraph Studio's built-in interrupt UI is the simplest way to exercise the HITL flow locally, and production deployment should use LangGraph Platform.

**If `needs_rag == true` and `update_frequency == "real-time"`:** Flag in Open Questions that real-time indexing requires an ingestion service separate from the inference graph; this is out of scope for the initial spec and must be designed separately.

---

## Example Completed Spec (Reference)

The following is a condensed example showing what a filled-in spec looks like for a "customer support email triage agent."

```
system_description: "Routes incoming customer emails to the right support team and drafts a reply."
data_sources: [live_api (email inbox), structured_db (ticket system), documents (product FAQ)]
needs_persistence: true (task_level)
needs_tools: true [read_email, create_ticket, search_faq, send_draft]
needs_rag: true (product FAQ corpus, ~500 docs, updated weekly)
needs_hitl: true (human approves draft reply before send)
scale_tier: team
llm_provider: anthropic
```

**Selected pattern:** LangGraph with interrupt() — base pattern: Agentic RAG

**ASCII diagram:**

```
  Email Webhook
       │
       ▼
  ┌──────────────┐
  │  Triage Node │  classifies intent, extracts metadata
  └──────┬───────┘
         │
         ▼
  ┌──────────────┐
  │  Agent Node  │◄─────────────────────┐
  │ (Claude 3.5) │                      │
  └──────┬───────┘                      │
         │ tool call                    │ tool result
         ▼                             │
  ┌──────────────┐                      │
  │  Tool Node   │──────────────────────┘
  │search_faq    │
  │create_ticket │
  └──────┬───────┘
         │ draft ready
         ▼
  ┌──────────────┐
  │  interrupt() │  human reviews draft in LangGraph Studio
  └──────┬───────┘
         │ approved
         ▼
  ┌──────────────┐
  │  Send Node   │  calls send_draft tool
  └──────────────┘
```

**State schema:**

```python
class EmailTriageState(TypedDict):
    messages: Annotated[list, add_messages]
    email_id: str
    intent: str
    ticket_id: str | None
    draft_reply: str | None
    approval_status: Literal["pending", "approved", "rejected"]
```

---

## Invocation

This skill is triggered when the user runs:

```
/lc:design-system
```

or says anything equivalent to: "design a LangChain system", "help me architect an AI workflow", "I want to build an agent", "let's spec out my LangGraph app."
