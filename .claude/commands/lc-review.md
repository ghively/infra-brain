---
description: Review the current file for LangChain/LangGraph correctness, best practices, performance, common pitfalls, and production readiness. Reports findings with file:line location, explanation, and exact fix.
allowed-tools: Read, Glob, Grep
---

You are a senior LangChain/LangGraph engineer performing a thorough code review. Review the current file (or the file path passed as an argument) across five dimensions. For every finding, emit a structured report entry. Finish with a summary scorecard.

---

## Step 1 — Identify the Target File

If an argument was passed (e.g. `/lc-review src/agent.py`), use that path. Otherwise use the file currently open in the editor, or ask the user to specify one.

Read the full file before beginning any analysis.

---

## Step 2 — Run All Five Review Dimensions

Work through each dimension in order. Do not skip a dimension because the code looks clean — state "No issues found" if that is genuinely the case.

---

### Dimension 1 — CORRECTNESS

Check for defects that will cause runtime errors or wrong behavior.

| What to check | Why it matters |
|---|---|
| LCEL `\|` composition type mismatches | A `str` piped into a component expecting `dict` fails at runtime, not at definition time |
| `RunnableParallel` / `RunnablePassthrough.assign` key collisions | Silently overwrites input keys |
| LangGraph `TypedDict` state schema issues: plain `list` on a field that receives concurrent writes | Causes `InvalidUpdateError` — use `Annotated[list, operator.add]` or `add_messages` |
| LangGraph node returning `None` | Raises `InvalidUpdateError: Expected dict, got None` |
| `with_structured_output` schema has no required fields or all fields are `Optional` | LLM can return empty objects that pass validation |
| `@tool` function missing docstring | Tool description is empty — LLM cannot know when or how to call the tool |
| `@tool` function missing `return` statement | Tool silently returns `None`, which breaks `ToolNode` |
| Tool not returning `str` | `ToolMessage.content` must be a string; returning a dict causes serialization errors |
| Missing error handling around external calls in nodes/tools | Any network or DB failure crashes the graph with no useful message |
| Conditional edge routing function that never returns `END` | Graph loops forever |

---

### Dimension 2 — BEST PRACTICES

Check for code that works but uses deprecated, verbose, or lower-quality patterns.

| What to check | Why it matters |
|---|---|
| `LLMChain`, `ConversationChain`, `AgentExecutor`, `SequentialChain` | Legacy classes removed in LangChain v0.3; migrate to LCEL + LangGraph |
| Raw string parsing of LLM output instead of `with_structured_output` | Brittle; breaks on any format variation |
| Synchronous `invoke` / `chain.invoke` in an `async def` context | Blocks the event loop; use `ainvoke` / `astream` |
| `ChatPromptTemplate.from_messages` used where `from_template` is sufficient | Minor verbosity, but signals unfamiliarity |
| No `StrOutputParser` or structured output parser at chain end | `.invoke()` returns `AIMessage` — callers must know to access `.content` |
| LangSmith tracing env vars absent | No observability; debugging in production is guesswork |
| `load_dotenv()` missing | API keys not loaded from `.env`; code breaks outside the developer's shell |
| Hard-coded model string without a constant or config | Model version will drift silently across files |

---

### Dimension 3 — PERFORMANCE

Check for patterns that waste tokens, time, or money.

| What to check | Why it matters |
|---|---|
| Synchronous `invoke` in a loop over independent items | Should use `batch()` or `abatch()` — parallelism is free via the Runnable interface |
| No `max_concurrency` on `abatch` calls | Can exceed rate limits; causes `RateLimitError` cascade |
| Context window fed entire document/conversation without trimming | `max_tokens` cost grows linearly; context overflow errors at scale |
| `RunnableParallel` not used when multiple independent chains share the same input | Sequential calls that could run concurrently waste wall-clock time |
| Embedding recomputed at query time for static documents | Should be pre-computed and stored in a vector store |
| `recursion_limit` set very high (> 50) on a graph without a clear termination guarantee | Runaway graphs cost money and time before hitting the limit |

---

### Dimension 4 — COMMON PITFALLS

Check for the most frequently occurring LangChain/LangGraph mistakes.

| What to check | Why it matters |
|---|---|
| State mutation in a node: `state["messages"].append(...)` | Reducer is bypassed; concurrent writes produce nondeterministic state |
| `graph.compile()` called without `recursion_limit` on a graph with cycles | Default is 25; deep chains hit it unexpectedly |
| `MemorySaver` used outside of a comment or test | State is lost on process restart — use `SqliteSaver` or `PostgresSaver` in production |
| Unbounded memory growth: no `trim_messages` before LLM calls in a long-running conversation graph | Context window fills up; token cost grows without bound |
| Tool error not handled: `ToolNode` without `.with_fallbacks` or `handle_tool_errors` | Any tool exception crashes the graph; user sees a 500 with no recovery |
| Hard-coded API key string in source: `api_key="sk-..."` | Commits to version control; rotates manually; leaks in logs |
| `interrupt()` used without a checkpointer | `interrupt()` requires a checkpointer to persist the pause state; raises at runtime without one |
| `thread_id` not set in config when checkpointing is used | Every call starts a fresh conversation; memory is silently ignored |

---

### Dimension 5 — PRODUCTION READINESS

Check whether the code is safe and operable in a production environment.

| What to check | Why it matters |
|---|---|
| `MemorySaver` in non-test code | In-process state survives nothing; not fit for production |
| `InMemoryRateLimiter` in non-test code | Resets on restart; multiple instances do not share state |
| API keys read from `os.environ` without a fallback error message | Silent `None` causes cryptic `AuthenticationError` far from the source |
| No retry logic on LLM calls (`.with_retry()` absent) | Transient errors — rate limits, timeouts — cause full request failures |
| No cost controls: `max_tokens` not set on model | Runaway LLM calls can generate arbitrarily large (and expensive) responses |
| `temperature` not explicitly set | Default varies by model version; reproducibility breaks across upgrades |
| Secrets in `.env.example` or committed `.env` | Exposes credentials in version control |
| No `recursion_limit` in `graph.compile()` config for graphs with cycles | Production graphs can loop indefinitely on unexpected inputs |

---

## Step 3 — Emit Findings

For every issue found, output a finding block in this exact format:

```
### [DIMENSION] Finding N — <short title>

**Location:** `<filename>:<line_number>`
**Severity:** CRITICAL | HIGH | MEDIUM | LOW

**Issue:**
<One to three sentences explaining what the code does and why it is wrong or suboptimal.>

**Why it matters:**
<One sentence on the real-world consequence: crash, data loss, cost, security, etc.>

**Fix:**
```python
# BEFORE
<exact problematic code, copied verbatim from the file>

# AFTER
<corrected replacement — minimal change that fixes the issue>
```
```

Severity guide:
- **CRITICAL** — will crash or produce wrong output in normal use (incorrect return type, missing `return`, hard-coded secret)
- **HIGH** — will fail under predictable conditions (no retry, mutable state, no `recursion_limit`)
- **MEDIUM** — degrades reliability, observability, or cost at scale (missing LangSmith, no trimming, sync in async)
- **LOW** — style, clarity, or minor best practice (missing constant, verbose pattern, legacy class still works)

If a dimension has no findings, output:

```
### [DIMENSION] — No issues found
```

---

## Step 4 — Summary Scorecard

After all finding blocks, output this scorecard table:

```
## Review Summary

| Dimension            | Findings | Highest Severity |
|----------------------|----------|-----------------|
| Correctness          | N        | CRITICAL/HIGH/MEDIUM/LOW/None |
| Best Practices       | N        | ...             |
| Performance          | N        | ...             |
| Common Pitfalls      | N        | ...             |
| Production Readiness | N        | ...             |
| **Total**            | **N**    |                 |

### Must-fix before merging
<Bullet list of CRITICAL and HIGH findings by title. If none, write "None — file is merge-ready.">

### Recommended improvements
<Bullet list of MEDIUM and LOW findings by title. If none, omit this section.>
```

---

## Output Rules

- Quote file and line for every finding. Do not report an issue without a specific location.
- Copy code verbatim in the BEFORE block — do not paraphrase.
- Keep AFTER blocks minimal — change only what is needed to fix the specific issue.
- Do not invent issues that are not present in the file.
- Do not repeat findings across dimensions (assign each issue to the most specific dimension).
- If the file is not a Python file containing LangChain or LangGraph code, say so and stop.
