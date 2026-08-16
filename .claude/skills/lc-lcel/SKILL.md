---
name: lc-lcel
description: Use when building, debugging, or optimizing any LangChain Expression Language (LCEL) pipeline. Triggered by questions about chaining components with |, RunnablePassthrough, RunnableParallel, RunnableLambda, RunnableBranch, streaming, retry/fallback, async LCEL, or configurable runnables. This is the foundational skill — invoke it before lc-agent or rag whenever the user is wiring LangChain components together.
---

# lc-lcel — LCEL Masterclass: From Zero to Expert

## Overview

LCEL (LangChain Expression Language) is the foundational pattern for composing LangChain components. Every LangChain object — prompts, models, parsers, retrievers, tools — implements the **Runnable interface**, which means they can all be chained together with the `|` operator, streamed, retried, made async, and wired into arbitrarily complex pipelines without writing orchestration boilerplate.

**Core insight:** LCEL is not a special syntax. `a | b` is Python's bitwise-or operator calling `a.__or__(b)`, which returns a `RunnableSequence`. That sequence is itself a Runnable, so you can chain it further: `(a | b) | c`.

**Default model:** `claude-sonnet-4-6` via `langchain-anthropic`.

---

## Skill Flow

Work through these sections in order. Skip to the relevant section if the user has a specific question.

1. Start with the simplest chain — one prompt, one model
2. Add a parser — always explain why
3. Introduce RunnablePassthrough when context must flow through
4. Add RunnableParallel when work can be done in parallel
5. Add RunnableLambda when Python logic is needed mid-chain
6. Cover branching, error handling, streaming, config, and async as needed
7. Build toward a complete application

---

## Environment Setup

```bash
pip install langchain langchain-anthropic langchain-core
```

```python
# .env
ANTHROPIC_API_KEY="sk-ant-..."
LANGSMITH_API_KEY="ls__..."     # optional but recommended — free at smith.langchain.com
LANGSMITH_TRACING="true"
LANGSMITH_PROJECT="lcel-app"
```

```python
# Always load first
from dotenv import load_dotenv
load_dotenv()
```

---

## Part 1 — The Runnable Interface

### Concept: Everything is a Runnable

Every LangChain component implements `Runnable`. This means every component exposes the same six methods:

| Method | What it does |
|---|---|
| `invoke(input)` | Synchronous single call — returns result |
| `batch(inputs)` | Synchronous call over a list — returns list of results |
| `stream(input)` | Synchronous streaming — returns iterator of chunks |
| `ainvoke(input)` | Async single call |
| `abatch(inputs)` | Async call over a list |
| `astream(input)` | Async streaming — returns async iterator of chunks |

Because every component has the same interface, they compose freely. The `|` operator builds a `RunnableSequence` where the output of the left Runnable is passed as input to the right Runnable.

### The Simplest Possible Chain

```python
"""
lcel_basics.py — The simplest LCEL chain, explained step by step.
"""
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

load_dotenv()

# --- Step 1: Create the components ---
# Each of these is a Runnable on its own.

prompt = ChatPromptTemplate.from_template(
    "Tell me a fun fact about {topic} in one sentence."
)
# prompt.invoke({"topic": "penguins"}) → ChatPromptValue

model = ChatAnthropic(model="claude-sonnet-4-6")
# model.invoke([HumanMessage(...)]) → AIMessage

parser = StrOutputParser()
# parser.invoke(AIMessage(content="...")) → "..."

# --- Step 2: Chain them with | ---
# Output of prompt → input of model → input of parser
chain = prompt | model | parser
# chain is a RunnableSequence — itself a Runnable

# --- Step 3: Call invoke() ---
result = chain.invoke({"topic": "black holes"})
print(result)
# "Black holes can warp time itself — a clock near a black hole runs slower..."

# --- Step 4: batch() — process many inputs at once ---
# LangChain runs these in a thread pool by default.
results = chain.batch([
    {"topic": "penguins"},
    {"topic": "volcanoes"},
    {"topic": "tardigrades"},
])
# Returns a list of 3 strings

# --- Step 5: stream() — get output token by token ---
for chunk in chain.stream({"topic": "origami"}):
    print(chunk, end="", flush=True)
print()  # newline after streaming completes
```

### Why the Parser Matters

Without `StrOutputParser`, `chain.invoke(...)` returns an `AIMessage` object. The parser extracts `.content` so downstream code gets a plain string. Always include a parser unless you explicitly need the full message object.

```python
# Without parser: returns AIMessage
chain_raw = prompt | model
result = chain_raw.invoke({"topic": "cats"})
print(type(result))    # <class 'langchain_core.messages.ai.AIMessage'>
print(result.content)  # "Cats sleep 12-16 hours a day..."

# With parser: returns str
chain = prompt | model | StrOutputParser()
result = chain.invoke({"topic": "cats"})
print(type(result))    # <class 'str'>
print(result)          # "Cats sleep 12-16 hours a day..."
```

---

## Part 2 — Pipe Composition

### How `|` Works

`chain = a | b | c` is equivalent to:

```python
from langchain_core.runnables import RunnableSequence
chain = RunnableSequence(first=a, middle=[], last=c)
# Or equivalently:
chain = a.__or__(b).__or__(c)
```

The data flows left to right:

```
input → a → (a's output) → b → (b's output) → c → result
```

**Type rule:** The output type of each step must be compatible with the input type of the next. LCEL does not enforce this at definition time — you'll find out at `invoke()` time if there's a mismatch.

### Common Type Flows

```
ChatPromptTemplate.invoke(dict) → ChatPromptValue
ChatAnthropic.invoke(ChatPromptValue | list[BaseMessage]) → AIMessage
StrOutputParser.invoke(AIMessage) → str
JsonOutputParser.invoke(AIMessage) → dict
BaseRetriever.invoke(str) → list[Document]
```

### Building Complex Pipelines Step by Step

Start simple and add components one at a time. Test at each step.

```python
"""
pipeline_building.py — Growing a pipeline incrementally.
"""
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser, JsonOutputParser
from pydantic import BaseModel, Field

load_dotenv()

model = ChatAnthropic(model="claude-sonnet-4-6")

# --- Step 1: Test the prompt alone ---
prompt = ChatPromptTemplate.from_template("Summarize {text} in one sentence.")
# prompt.invoke({"text": "hello"}) → ChatPromptValue  ← test this first

# --- Step 2: Add the model ---
chain_v1 = prompt | model
# chain_v1.invoke({"text": "..."}) → AIMessage  ← test this

# --- Step 3: Add the parser ---
chain_v2 = prompt | model | StrOutputParser()
# chain_v2.invoke({"text": "..."}) → str  ← final for this flow

# --- Step 4: Structured output with Pydantic ---
class Summary(BaseModel):
    headline: str = Field(description="One-sentence summary")
    sentiment: str = Field(description="positive, negative, or neutral")
    key_topics: list[str] = Field(description="Up to 3 key topics")

structured_prompt = ChatPromptTemplate.from_template(
    "Analyze this text and return structured JSON.\n\nText: {text}"
)

# with_structured_output wraps the model to always return a parsed Pydantic object
structured_chain = structured_prompt | model.with_structured_output(Summary)

result = structured_chain.invoke({"text": "LangChain makes building LLM apps easier."})
print(result.headline)      # "LangChain simplifies LLM application development."
print(result.sentiment)     # "positive"
print(result.key_topics)    # ["LangChain", "LLM", "development"]
```

---

## Part 3 — Core Runnables

### RunnablePassthrough — Preserve Input While Transforming It

`RunnablePassthrough` passes its input through unchanged. Its primary use is in RAG and multi-step chains where you need to carry the original input forward alongside computed values.

```python
"""
runnable_passthrough.py — Pass input through while adding computed context.
"""
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

load_dotenv()

model = ChatAnthropic(model="claude-sonnet-4-6")

# --- Problem: The prompt needs both {question} AND {context} ---
# If chain input is just a string (the question), how does {context} get filled?

rag_prompt = ChatPromptTemplate.from_template(
    "Answer the question using only the context.\n\nContext: {context}\n\nQuestion: {question}"
)

# Fake retriever — in real life this queries a vector store
def retrieve_docs(question: str) -> str:
    return f"[Retrieved documents relevant to: {question}]"

# --- Solution: RunnablePassthrough.assign() ---
# assign(key=runnable) adds a new key to the input dict.
# The runnable receives the FULL input dict (or the raw input if it's not a dict).

rag_chain = (
    RunnablePassthrough.assign(context=lambda x: retrieve_docs(x["question"]))
    | rag_prompt
    | model
    | StrOutputParser()
)

# Input: {"question": "What is LCEL?"}
# After assign: {"question": "What is LCEL?", "context": "[Retrieved docs...]"}
# Then prompt fills both {question} and {context}

result = rag_chain.invoke({"question": "What is LCEL?"})
print(result)

# --- RunnablePassthrough alone (no assign) ---
# Useful when you want a chain branch that always returns the input.

from langchain_core.runnables import RunnableParallel

# This chain returns the original question alongside the answer
full_chain = RunnableParallel(
    question=RunnablePassthrough(),   # passes input unchanged
    answer=rag_chain,                 # computes the answer
)

result = full_chain.invoke({"question": "What is LCEL?"})
print(result["question"])   # {"question": "What is LCEL?"}
print(result["answer"])     # "LCEL stands for LangChain Expression Language..."
```

### RunnableLambda — Wrap Any Python Function

`RunnableLambda` turns any Python function into a Runnable. Use it for data transformation, business logic, or any step that isn't a built-in LangChain component.

```python
"""
runnable_lambda.py — Python functions as first-class chain components.
"""
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda

load_dotenv()

model = ChatAnthropic(model="claude-sonnet-4-6")

# --- Basic RunnableLambda ---
def clean_input(text: str) -> str:
    """Strip whitespace and normalize to lowercase."""
    return text.strip().lower()

def add_context(text: str) -> dict:
    """Transform a string into the dict the prompt expects."""
    return {"topic": text, "style": "formal"}

cleaner = RunnableLambda(clean_input)
contextualizer = RunnableLambda(add_context)

prompt = ChatPromptTemplate.from_template(
    "Write a {style} one-paragraph overview of {topic}."
)

chain = cleaner | contextualizer | prompt | model | StrOutputParser()

result = chain.invoke("  QUANTUM COMPUTING  ")
# "   QUANTUM COMPUTING  " → "quantum computing" → {"topic": "quantum computing", "style": "formal"} → ...

# --- Using RunnableLambda inline with pipe ---
# You can also pass a plain function directly — LCEL wraps it automatically

chain_implicit = (
    (lambda text: text.strip().lower())   # auto-wrapped as RunnableLambda
    | (lambda text: {"topic": text, "style": "casual"})
    | prompt
    | model
    | StrOutputParser()
)

# --- Async RunnableLambda ---
import asyncio

async def async_fetch_user_data(user_id: str) -> dict:
    """Simulate async database lookup."""
    await asyncio.sleep(0.01)  # simulate I/O
    return {"user_id": user_id, "name": "Alice", "preferences": "concise answers"}

async_chain = (
    RunnableLambda(async_fetch_user_data)
    | (lambda data: {"question": "How do I get started?", "user_name": data["name"]})
    | ChatPromptTemplate.from_template("Hi {user_name}! {question}")
    | model
    | StrOutputParser()
)
```

### RunnableParallel — Run Multiple Chains Simultaneously

`RunnableParallel` runs several runnables with the **same input** and returns a dict of results. All branches run concurrently in a thread pool.

```python
"""
runnable_parallel.py — Fan out to multiple chains, collect all results.
"""
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableParallel, RunnablePassthrough
from operator import itemgetter

load_dotenv()

model = ChatAnthropic(model="claude-sonnet-4-6")
parser = StrOutputParser()

# --- Generate multiple perspectives on the same input simultaneously ---

pros_chain = (
    ChatPromptTemplate.from_template("List 3 pros of {topic} in bullet points.")
    | model | parser
)

cons_chain = (
    ChatPromptTemplate.from_template("List 3 cons of {topic} in bullet points.")
    | model | parser
)

summary_chain = (
    ChatPromptTemplate.from_template("Write a neutral one-paragraph overview of {topic}.")
    | model | parser
)

# All three chains receive {"topic": "..."}  at the same time
analysis = RunnableParallel(
    pros=pros_chain,
    cons=cons_chain,
    summary=summary_chain,
)

result = analysis.invoke({"topic": "remote work"})
print(result["pros"])     # "- Flexibility in schedule\n- No commute..."
print(result["cons"])     # "- Isolation\n- Blurred work-life boundaries..."
print(result["summary"])  # "Remote work is a working arrangement..."

# --- Dict shorthand: same as RunnableParallel ---
# LCEL auto-wraps a dict of runnables into RunnableParallel

analysis_shorthand = {
    "pros": pros_chain,
    "cons": cons_chain,
    "summary": summary_chain,
}
# This works anywhere a Runnable is expected:
full_chain = analysis_shorthand | (
    ChatPromptTemplate.from_template(
        "Given these perspectives on {topic}:\n\nPros: {pros}\n\nCons: {cons}\n\n"
        "Summary: {summary}\n\nWrite a balanced recommendation."
    )
    | model | parser
)

# --- itemgetter: extract a specific key from a dict ---
# operator.itemgetter("key") is shorthand for lambda d: d["key"]
# LCEL wraps it as a RunnableLambda automatically

from operator import itemgetter

extract_chain = (
    RunnableParallel(
        question=RunnablePassthrough(),
        rephrased=(
            ChatPromptTemplate.from_template("Rephrase this question more precisely: {question}")
            | model | parser
        ),
    )
    | itemgetter("rephrased")   # extract just the rephrased question
    | ChatPromptTemplate.from_template("Answer this: {question}")
    | model | parser
)
# Note: itemgetter("rephrased") returns a str, but the next prompt expects {"question": str}
# Fix: use a lambda to rebuild the dict
extract_chain_fixed = (
    RunnableParallel(
        question=RunnablePassthrough(),
        rephrased=(
            ChatPromptTemplate.from_template("Rephrase this question more precisely: {question}")
            | model | parser
        ),
    )
    | (lambda d: {"question": d["rephrased"]})
    | ChatPromptTemplate.from_template("Answer this: {question}")
    | model | parser
)
```

---

## Part 4 — Branching and Routing

### RunnableBranch — Conditional Routing

`RunnableBranch` picks which chain to run based on the input. It takes a series of `(condition, runnable)` pairs and a default runnable.

```python
"""
runnable_branch.py — Route to different chains based on input content.
"""
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableBranch, RunnableLambda

load_dotenv()

model = ChatAnthropic(model="claude-sonnet-4-6")
parser = StrOutputParser()

# --- Three specialized chains ---

technical_chain = (
    ChatPromptTemplate.from_template(
        "You are a technical expert. Answer this precisely with code examples if relevant: {question}"
    ) | model | parser
)

casual_chain = (
    ChatPromptTemplate.from_template(
        "You are a friendly explainer. Answer this in plain English with analogies: {question}"
    ) | model | parser
)

default_chain = (
    ChatPromptTemplate.from_template("Answer this clearly: {question}")
    | model | parser
)

# --- RunnableBranch ---
# Each (condition, runnable) pair: condition is a callable that receives the input.
# First condition that returns True wins. Last argument is the default (no condition).

router = RunnableBranch(
    (
        lambda x: any(word in x["question"].lower()
                      for word in ["code", "function", "algorithm", "implement", "debug"]),
        technical_chain,
    ),
    (
        lambda x: any(word in x["question"].lower()
                      for word in ["explain", "what is", "how does", "simple", "beginner"]),
        casual_chain,
    ),
    default_chain,   # fallback — no condition needed
)

# Usage:
result = router.invoke({"question": "Implement a binary search algorithm"})
# → routes to technical_chain (contains "implement")

result = router.invoke({"question": "What is recursion in simple terms?"})
# → routes to casual_chain (contains "what is" and "simple")

result = router.invoke({"question": "What time is it in Tokyo?"})
# → routes to default_chain (no keyword match)

# --- RunnableLambda for dynamic routing ---
# When the routing logic is complex, use a function that returns a chain.

def route_by_language(input_dict: dict):
    """Detect language and return the appropriate chain."""
    question = input_dict["question"]
    # Real implementation: use langdetect or a classifier LLM
    if any(c > "" for c in question):  # rough non-ASCII heuristic
        return (
            ChatPromptTemplate.from_template("Respond in the same language: {question}")
            | model | parser
        )
    return default_chain

dynamic_router = RunnableLambda(route_by_language)
# dynamic_router.invoke({"question": "Bonjour!"}) → calls the multilingual chain
```

---

## Part 5 — Error Handling in LCEL

### Retry, Fallback, and Lifecycle Hooks

Every Runnable supports `.with_retry()`, `.with_fallbacks()`, and `.with_listeners()`. These compose naturally with `|`.

```python
"""
error_handling.py — Retry, fallback, and lifecycle hooks in LCEL.
"""
import time
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda
from tenacity import stop_after_attempt, wait_exponential

load_dotenv()

model = ChatAnthropic(model="claude-sonnet-4-6")
parser = StrOutputParser()
prompt = ChatPromptTemplate.from_template("Answer: {question}")

# --- .with_retry() ---
# Automatically retries on any exception. Uses tenacity under the hood.

resilient_model = model.with_retry(
    stop=stop_after_attempt(3),              # stop after 3 total attempts
    wait=wait_exponential(multiplier=1, min=1, max=10),  # exponential backoff
    retry_on_exception=lambda e: True,       # retry on any exception (default)
)

chain_with_retry = prompt | resilient_model | parser

# --- .with_fallbacks() ---
# If the primary chain fails after all retries, try the fallback chain.

fallback_model = ChatAnthropic(model="claude-haiku-4-5")  # cheaper fallback
fallback_chain = prompt | fallback_model | parser

primary_chain = prompt | resilient_model | parser

# On any unrecovered exception, falls through to fallback_chain
robust_chain = primary_chain.with_fallbacks([fallback_chain])

result = robust_chain.invoke({"question": "What is LCEL?"})

# --- Fallback to a static response ---
# If all models fail, return a safe default message.

def static_fallback(input_dict: dict) -> str:
    return "I'm temporarily unavailable. Please try again shortly."

ultimate_chain = (
    primary_chain
    .with_fallbacks([
        fallback_chain,                              # try cheaper model first
        RunnableLambda(static_fallback),             # then static response
    ])
)

# --- .with_listeners() — lifecycle hooks ---
# on_start: called before the Runnable runs (receives serialized input)
# on_end: called after success (receives Run object with inputs/outputs)
# on_error: called on exception (receives Run object with error)

def on_chain_start(serialized: dict, **kwargs) -> None:
    print(f"[START] Chain starting at {time.time():.2f}")

def on_chain_end(run, **kwargs) -> None:
    print(f"[END] Chain completed. Output: {str(run.outputs)[:100]}")

def on_chain_error(run, **kwargs) -> None:
    print(f"[ERROR] Chain failed: {run.error}")

instrumented_chain = (prompt | model | parser).with_listeners(
    on_start=on_chain_start,
    on_end=on_chain_end,
    on_error=on_chain_error,
)

result = instrumented_chain.invoke({"question": "What is 2+2?"})
# [START] Chain starting at 1718700000.00
# [END] Chain completed. Output: {'output': '4'}
```

---

## Part 6 — Streaming Patterns

### Token-Level and Event-Level Streaming

```python
"""
streaming_patterns.py — Four streaming approaches in LCEL.
"""
import asyncio
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda

load_dotenv()

model = ChatAnthropic(model="claude-sonnet-4-6")
parser = StrOutputParser()
prompt = ChatPromptTemplate.from_template("Write a short story about {topic}.")
chain = prompt | model | parser

# --- Pattern 1: stream() — synchronous token streaming ---
# Each chunk is a string fragment. Print as they arrive.

print("=== stream() ===")
for chunk in chain.stream({"topic": "a robot learning to paint"}):
    print(chunk, end="", flush=True)
print()

# --- Pattern 2: astream() — async token streaming ---
# Use in async contexts (FastAPI, Jupyter, etc.)

async def async_stream_example():
    print("\n=== astream() ===")
    async for chunk in chain.astream({"topic": "a lighthouse keeper"}):
        print(chunk, end="", flush=True)
    print()

asyncio.run(async_stream_example())

# --- Pattern 3: astream_events() — detailed event stream ---
# Yields events for every step: on_chain_start, on_llm_stream, on_chain_end, etc.
# Use for: building real-time UIs, debugging pipelines, progress indicators.

async def stream_events_example():
    print("\n=== astream_events() ===")
    async for event in chain.astream_events(
        {"topic": "a time-traveling historian"},
        version="v2",   # always specify version="v2"
    ):
        kind = event["event"]

        if kind == "on_chain_start":
            print(f"[Chain started: {event['name']}]")

        elif kind == "on_chat_model_stream":
            # Token-level chunks from the LLM
            chunk = event["data"]["chunk"]
            if chunk.content:
                print(chunk.content, end="", flush=True)

        elif kind == "on_chain_end":
            print(f"\n[Chain ended: {event['name']}]")

asyncio.run(stream_events_example())

# --- Pattern 4: Streaming through custom transforms ---
# A generator function that processes streamed chunks mid-pipeline.
# Must use yield to preserve streaming — do NOT accumulate then return.

def uppercase_stream(input_iter):
    """Transform each chunk to uppercase as it streams through."""
    for chunk in input_iter:
        yield chunk.upper()

# RunnableLambda with a generator becomes a streaming transform
streaming_chain = prompt | model | parser | RunnableLambda(uppercase_stream)

print("\n=== Custom streaming transform ===")
for chunk in streaming_chain.stream({"topic": "ocean currents"}):
    print(chunk, end="", flush=True)
print()

# Async generator version
async def async_uppercase(input_iter):
    async for chunk in input_iter:
        yield chunk.upper()

async_streaming_chain = prompt | model | parser | RunnableLambda(async_uppercase)
```

---

## Part 7 — Configuration

### RunnableConfig, bind(), configurable_fields()

Configuration lets you fix arguments at definition time or make them configurable at invoke time.

```python
"""
configuration.py — Bind arguments, pass metadata, and make fields configurable.
"""
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableConfig, ConfigurableField

load_dotenv()

# --- .bind() — Fix arguments at definition time ---
# bind() pre-fills keyword arguments so they don't need to be passed at invoke time.

model = ChatAnthropic(model="claude-sonnet-4-6")

# Fix stop sequences and temperature — no need to pass these at invoke time
model_with_stops = model.bind(
    stop=["\n\nHuman:", "END"],   # stop generating at these tokens
    temperature=0,                # override default temperature
    max_tokens=500,               # cap response length
)

prompt = ChatPromptTemplate.from_template("Answer: {question}")
chain = prompt | model_with_stops | StrOutputParser()

# --- RunnableConfig — Pass metadata and callbacks at invoke time ---
# RunnableConfig is a TypedDict. Pass it as the second argument to invoke().

config: RunnableConfig = {
    "tags": ["prod", "customer-support"],           # for filtering in LangSmith
    "metadata": {"user_id": "u123", "session": "s456"},  # arbitrary metadata
    "run_name": "support-query",                    # display name in traces
    "max_concurrency": 5,                           # for .batch() calls
    "recursion_limit": 10,                          # for nested chains
}

result = chain.invoke({"question": "What is LCEL?"}, config=config)

# --- .configurable_fields() — Make fields configurable at runtime ---
# Expose specific model parameters as named configurable fields.
# The caller can override them per-invoke without changing the chain definition.

configurable_model = ChatAnthropic(
    model="claude-sonnet-4-6",
    temperature=0,
).configurable_fields(
    temperature=ConfigurableField(
        id="temperature",               # key used in config["configurable"]
        name="LLM Temperature",
        description="0=deterministic, 1=creative",
    ),
    max_tokens=ConfigurableField(
        id="max_tokens",
        name="Max Tokens",
        description="Maximum tokens in the response",
    ),
)

configurable_chain = prompt | configurable_model | StrOutputParser()

# Override temperature at invoke time — the chain definition doesn't change
creative_result = configurable_chain.invoke(
    {"question": "Write me a creative story hook."},
    config={"configurable": {"temperature": 0.9, "max_tokens": 200}},
)

precise_result = configurable_chain.invoke(
    {"question": "What is 2 + 2?"},
    config={"configurable": {"temperature": 0.0, "max_tokens": 10}},
)

# --- .configurable_alternatives() — Swap entire components at runtime ---
# Define alternative implementations and choose between them at invoke time.

fast_model = ChatAnthropic(model="claude-haiku-4-5")
smart_model = ChatAnthropic(model="claude-opus-4-5")

switchable_chain = (
    prompt
    | smart_model.configurable_alternatives(
        ConfigurableField(id="model_choice"),
        default_key="smart",
        fast=fast_model,      # invoke with config={"configurable": {"model_choice": "fast"}}
    )
    | StrOutputParser()
)

fast_result = switchable_chain.invoke(
    {"question": "Quick answer: what year was Python created?"},
    config={"configurable": {"model_choice": "fast"}},
)
```

---

## Part 8 — Async Patterns

### When and How to Use Async LCEL

Use async patterns when:
- Serving multiple users concurrently (FastAPI, web servers)
- Running many LLM calls in parallel with `abatch()`
- Streaming tokens to a browser or SSE endpoint

**Key rule:** In an async context, always use `ainvoke`, `abatch`, `astream`. Mixing sync `invoke()` into async code can block the event loop.

```python
"""
async_patterns.py — Complete async LCEL patterns including FastAPI integration.
"""
import asyncio
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

load_dotenv()

model = ChatAnthropic(model="claude-sonnet-4-6")
parser = StrOutputParser()
prompt = ChatPromptTemplate.from_template("Answer briefly: {question}")
chain = prompt | model | parser

# --- Pattern 1: Basic async invoke ---

async def async_single():
    result = await chain.ainvoke({"question": "What is async/await?"})
    print(result)

# --- Pattern 2: Concurrent requests with abatch() ---
# max_concurrency limits how many LLM calls run simultaneously.
# Without it, abatch() fires all requests at once (may hit rate limits).

async def async_batch():
    questions = [
        {"question": "What is Python?"},
        {"question": "What is async programming?"},
        {"question": "What is LangChain?"},
        {"question": "What is LCEL?"},
        {"question": "What is a vector database?"},
    ]

    results = await chain.abatch(
        questions,
        config={"max_concurrency": 3},   # at most 3 concurrent LLM calls
    )

    for q, r in zip(questions, results):
        print(f"Q: {q['question']}\nA: {r}\n")

# --- Pattern 3: Async streaming with astream() ---

async def async_stream():
    async for chunk in chain.astream({"question": "Explain async programming"}):
        print(chunk, end="", flush=True)
    print()

# --- Pattern 4: Gather multiple chains concurrently ---
# When chains are independent, run them with asyncio.gather() for true parallelism.

async def concurrent_chains():
    summarize = (
        ChatPromptTemplate.from_template("Summarize: {text}") | model | parser
    )
    translate = (
        ChatPromptTemplate.from_template("Translate to Spanish: {text}") | model | parser
    )
    critique = (
        ChatPromptTemplate.from_template("Critique this text: {text}") | model | parser
    )

    text = "LangChain is a framework for building LLM-powered applications."

    # All three run concurrently — total time ≈ max(individual times), not sum
    summary, translation, feedback = await asyncio.gather(
        summarize.ainvoke({"text": text}),
        translate.ainvoke({"text": text}),
        critique.ainvoke({"text": text}),
    )

    print(f"Summary: {summary}")
    print(f"Translation: {translation}")
    print(f"Critique: {feedback}")

# --- Pattern 5: FastAPI + streaming SSE endpoint ---
# This is the canonical production pattern for streaming LCEL responses.

"""
To run this FastAPI example:
    pip install fastapi uvicorn sse-starlette
    uvicorn async_patterns:app --reload
"""

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()

class QuestionRequest(BaseModel):
    question: str

@app.post("/ask")
async def ask_question(request: QuestionRequest):
    """Non-streaming endpoint — returns full response."""
    result = await chain.ainvoke({"question": request.question})
    return {"answer": result}

@app.post("/ask/stream")
async def ask_question_stream(request: QuestionRequest):
    """
    Streaming endpoint — yields tokens as server-sent events.
    The client receives tokens in real time instead of waiting for the full response.
    """
    async def generate():
        async for chunk in chain.astream({"question": request.question}):
            # SSE format: each event starts with "data: " and ends with double newline
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # disable nginx buffering for SSE
        },
    )

@app.post("/analyze")
async def analyze_concurrently(request: QuestionRequest):
    """
    Run multiple analysis chains concurrently and return all results.
    Total latency ≈ slowest single chain, not sum of all chains.
    """
    pros_chain = (
        ChatPromptTemplate.from_template("List 3 pros of: {question}") | model | parser
    )
    cons_chain = (
        ChatPromptTemplate.from_template("List 3 cons of: {question}") | model | parser
    )

    pros, cons = await asyncio.gather(
        pros_chain.ainvoke({"question": request.question}),
        cons_chain.ainvoke({"question": request.question}),
    )

    return {"pros": pros, "cons": cons}


# --- Run all async examples ---
if __name__ == "__main__":
    asyncio.run(async_single())
    asyncio.run(async_batch())
    asyncio.run(async_stream())
    asyncio.run(concurrent_chains())
    # For FastAPI: uvicorn async_patterns:app --reload
```

---

## Part 9 — Complete Application: RAG Pipeline

Putting all LCEL concepts together in a realistic retrieval-augmented generation pipeline.

```python
"""
rag_pipeline.py — Complete RAG pipeline using all major LCEL concepts.

Demonstrates:
  - RunnablePassthrough.assign() to thread context through the chain
  - RunnableParallel to retrieve and keep the question simultaneously
  - itemgetter to extract specific fields
  - .with_retry() for resilience
  - .configurable_fields() for runtime configuration
  - astream() for streaming responses
  - RunnableConfig for tracing metadata
"""
import asyncio
from operator import itemgetter
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import (
    RunnablePassthrough,
    RunnableParallel,
    RunnableLambda,
    ConfigurableField,
)
from tenacity import stop_after_attempt, wait_exponential

load_dotenv()

# --- Mock retriever (replace with Chroma, Pinecone, pgvector, etc.) ---

KNOWLEDGE_BASE = {
    "lcel": "LCEL is LangChain Expression Language. It uses | to compose Runnables.",
    "runnable": "A Runnable implements invoke, batch, stream and their async variants.",
    "rag": "RAG combines retrieval of relevant documents with LLM generation.",
    "langchain": "LangChain is a framework for building applications powered by LLMs.",
}

def retrieve(question: str) -> str:
    """Retrieve relevant context for a question."""
    question_lower = question.lower()
    relevant = [v for k, v in KNOWLEDGE_BASE.items() if k in question_lower]
    if relevant:
        return "\n".join(relevant)
    return "No specific context found. Answer from general knowledge."

# --- Components ---

model = ChatAnthropic(
    model="claude-sonnet-4-6",
    temperature=0,
).with_retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
).configurable_fields(
    temperature=ConfigurableField(
        id="temperature",
        name="Response Temperature",
        description="Higher values produce more creative responses",
    )
)

rag_prompt = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a helpful assistant. Answer the question using the provided context. "
        "If the context doesn't contain relevant information, say so and answer from "
        "general knowledge.\n\nContext:\n{context}"
    )),
    ("human", "{question}"),
])

# --- The RAG chain ---
#
# Data flow:
# {"question": "What is LCEL?"} (input dict)
#   → RunnablePassthrough.assign adds "context" key
#   → {"question": "What is LCEL?", "context": "LCEL is..."}
#   → rag_prompt fills both template variables
#   → model generates response
#   → parser extracts string
#
rag_chain = (
    RunnablePassthrough.assign(
        context=RunnableLambda(lambda x: retrieve(x["question"]))
    )
    | rag_prompt
    | model
    | StrOutputParser()
)

# --- Extended chain with source attribution ---
#
# Returns both the answer AND the retrieved context.

rag_with_sources = (
    RunnableParallel(
        question=itemgetter("question"),
        context=RunnableLambda(lambda x: retrieve(x["question"])),
    )
    | RunnableParallel(
        answer=(
            rag_prompt
            | model
            | StrOutputParser()
        ),
        context=itemgetter("context"),
        question=itemgetter("question"),
    )
)

# --- Usage ---

async def main():
    # Standard invoke
    result = rag_chain.invoke({"question": "What is LCEL?"})
    print(f"Answer: {result}\n")

    # With tracing metadata
    result = rag_chain.invoke(
        {"question": "How does RAG work?"},
        config={
            "run_name": "rag-query",
            "metadata": {"user_id": "u789"},
            "tags": ["rag", "prod"],
        },
    )
    print(f"Answer: {result}\n")

    # With runtime temperature override
    creative_result = rag_chain.invoke(
        {"question": "Explain LCEL creatively."},
        config={"configurable": {"temperature": 0.8}},
    )
    print(f"Creative answer: {creative_result}\n")

    # Streaming
    print("Streaming: ", end="")
    async for chunk in rag_chain.astream({"question": "What is a Runnable?"}):
        print(chunk, end="", flush=True)
    print("\n")

    # With sources
    result_with_sources = rag_with_sources.invoke({"question": "What is LangChain?"})
    print(f"Q: {result_with_sources['question']}")
    print(f"Context used: {result_with_sources['context']}")
    print(f"Answer: {result_with_sources['answer']}")

    # Batch processing
    questions = [
        {"question": "What is LCEL?"},
        {"question": "What is a Runnable?"},
        {"question": "What is RAG?"},
    ]
    answers = await rag_chain.abatch(questions, config={"max_concurrency": 3})
    for q, a in zip(questions, answers):
        print(f"Q: {q['question']}\nA: {a}\n")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Common Mistakes

| Mistake | What happens | Fix |
|---|---|---|
| Forgetting `StrOutputParser()` | `invoke()` returns `AIMessage`, not `str` | Always add a parser at the end of chains |
| `chain.stream()` returns nothing | Parser or transform broke streaming | Use generator functions (`yield`), not return — see Part 6 |
| Type mismatch between steps | `ValidationError` or `AttributeError` at invoke time | Test each step individually: `prompt.invoke({...})` then `model.invoke(...)` |
| `RunnableParallel` dict not recognized | Chain doesn't run in parallel | Wrap explicitly: `RunnableParallel(a=chain_a, b=chain_b)` or use dict shorthand only at the top level |
| `assign()` overwrites a key | Input key is silently replaced | Use different key names in `assign()` than in the input dict |
| Blocking `invoke()` inside async code | Event loop blocked, timeouts under load | Use `ainvoke()` in async contexts |
| `abatch()` hits rate limits | `RateLimitError` for large batches | Set `config={"max_concurrency": N}` where N fits your API tier |
| `with_retry()` retries forever | Chain hangs on persistent errors | Always set `stop=stop_after_attempt(N)` |
| `configurable_fields()` not passed at invoke | Default value used silently | Pass `config={"configurable": {"field_id": value}}` |
| `astream_events()` without `version="v2"` | Warning or wrong event format | Always pass `version="v2"` |

---

## Quick Reference

```python
# Imports cheat sheet
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser, JsonOutputParser
from langchain_core.runnables import (
    RunnablePassthrough,
    RunnableParallel,
    RunnableLambda,
    RunnableBranch,
    RunnableConfig,
    ConfigurableField,
)
from operator import itemgetter

# Minimal chain
chain = ChatPromptTemplate.from_template("{q}") | ChatAnthropic(model="claude-sonnet-4-6") | StrOutputParser()

# Six Runnable methods
chain.invoke({"q": "hello"})                         # sync single
chain.batch([{"q": "a"}, {"q": "b"}])               # sync batch
for chunk in chain.stream({"q": "hello"}): ...       # sync stream
await chain.ainvoke({"q": "hello"})                  # async single
await chain.abatch([...], config={"max_concurrency": 5})  # async batch
async for chunk in chain.astream({"q": "hello"}): ...  # async stream

# Add computed keys to dict
RunnablePassthrough.assign(context=lambda x: retrieve(x["question"]))

# Wrap a Python function
RunnableLambda(my_function)

# Run in parallel, merge results
RunnableParallel(a=chain_a, b=chain_b)   # or {"a": chain_a, "b": chain_b}

# Conditional routing
RunnableBranch((condition_fn, chain_a), (condition_fn_2, chain_b), default_chain)

# Resilience
chain.with_retry(stop=stop_after_attempt(3))
chain.with_fallbacks([fallback_chain])

# Fix arguments
model.bind(temperature=0, stop=["END"])

# Configurable at runtime
model.configurable_fields(temperature=ConfigurableField(id="temp"))
chain.invoke(input, config={"configurable": {"temp": 0.9}})

# Tracing
chain.invoke(input, config={"run_name": "my-run", "tags": ["prod"], "metadata": {"user": "u1"}})
```

---

## Part 10 — Semantic Caching

### What Semantic Caching Does

Semantic caching intercepts LLM calls at the `langchain_core` layer. Before sending a prompt to the model, LangChain checks the cache. On a **cache hit** — where a previous prompt is semantically similar enough to the current one — LangChain returns the stored response immediately, skipping the LLM call entirely. On a **cache miss** the call proceeds normally and the response is stored.

**Effect on LCEL chains:** Semantic caching is transparent to your chain code. You set the cache once globally via `set_llm_cache()` and every LLM call in every chain in the same process benefits automatically. You do not need to modify individual chains.

**Cost savings estimate:** 40-80% cost reduction on Q&A-heavy or FAQ-style applications, where users ask semantically equivalent questions repeatedly. For applications with highly varied or personalized queries the savings are much lower.

---

### Cache Types

| Cache | Match type | Storage | When to use |
|---|---|---|---|
| `InMemoryCache` | Exact string match | Python dict (in-process) | Development, unit tests, demos |
| `RedisCache` | Exact string match | Redis | Production, exact deduplication |
| `RedisSemanticCache` | Embedding similarity | Redis + vector index | Production, natural language Q&A |
| `GPTCache` | Semantic (multiple backends) | Pluggable | When you need advanced eviction policies |

---

### Option 1: InMemoryCache (Exact Match, Dev Only)

`InMemoryCache` caches by exact prompt string. Two prompts must be character-for-character identical to hit the cache. It lives in process memory — it is lost on restart and is not shared between workers.

```python
"""
lcel_inmemory_cache.py — Exact-match in-memory LLM cache for development.

When to use:
  - Local development and unit tests
  - Demos where you want fast repeated calls without hitting the API
  - CI pipelines to avoid flaky tests that depend on LLM responses

When NOT to use:
  - Production (cache lost on restart, not shared across workers)
  - Personalized responses (same exact prompt produces wrong answer for different users)
  - Real-time data queries (cache will return stale information)
  - Creative tasks (every call should produce a different result)
"""

from dotenv import load_dotenv
from langchain_core.globals import set_llm_cache
from langchain_core.caches import InMemoryCache
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
import time

load_dotenv()

# --- Set the global cache ---
# Call this once at application startup before any LLM calls.
# All LLM calls in this process will use this cache automatically.
set_llm_cache(InMemoryCache())

# --- Build a normal LCEL chain — no changes needed ---
model = ChatAnthropic(model="claude-sonnet-4-6")
prompt = ChatPromptTemplate.from_template("Answer briefly: {question}")
chain = prompt | model | StrOutputParser()

# --- First call: cache miss → LLM is invoked ---
t0 = time.time()
result1 = chain.invoke({"question": "What is the capital of France?"})
t1 = time.time()
print(f"Miss: {result1!r}  ({t1-t0:.2f}s)")
# → "Paris is the capital of France."  (1.2s — real LLM call)

# --- Second call: EXACT same prompt string → cache hit ---
t0 = time.time()
result2 = chain.invoke({"question": "What is the capital of France?"})
t1 = time.time()
print(f"Hit:  {result2!r}  ({t1-t0:.4f}s)")
# → "Paris is the capital of France."  (0.0003s — from cache)

# --- Different question: cache miss (exact match only) ---
t0 = time.time()
result3 = chain.invoke({"question": "What's the capital city of France?"})
t1 = time.time()
print(f"Miss: {result3!r}  ({t1-t0:.2f}s)")
# → "The capital city of France is Paris."  (1.1s — different string = cache miss)

# --- Disable cache for a specific call ---
# Pass cache=False in the config to bypass the global cache for one call.
result_nocache = chain.invoke(
    {"question": "What is the capital of France?"},
    config={"cache": False},   # bypass cache even though this prompt is cached
)

# --- Clear the cache ---
from langchain_core.globals import get_llm_cache
get_llm_cache().clear()
```

---

### Option 2: RedisSemanticCache (Production)

`RedisSemanticCache` embeds the incoming prompt with an embedding model, searches for similar prompts stored in Redis, and returns the cached response if cosine similarity exceeds the threshold. Two questions that ask the same thing in different words will hit the same cache entry.

```python
"""
lcel_redis_semantic_cache.py — Semantic LLM cache backed by Redis for production.

Architecture:
  1. User query arrives
  2. Embedding model converts query to a vector
  3. Redis vector search finds the nearest stored query
  4. If nearest_distance <= (1 - score_threshold): cache hit → return stored response
  5. If no close match: cache miss → call LLM → embed + store the new query+response

Requirements:
  pip install langchain-redis redis langchain-openai   # or langchain-anthropic for embeddings

Redis requirements:
  - Redis Stack (includes RediSearch module for vector similarity search)
  - OR use Redis Cloud with Search enabled
  - Docker: docker run -p 6379:6379 redis/redis-stack:latest

Environment variables:
  REDIS_URL           Redis connection URL (default: redis://localhost:6379/0)
  OPENAI_API_KEY      Required if using OpenAI embeddings
  ANTHROPIC_API_KEY   Required if using Anthropic models

When NOT to use semantic caching:
  - Personalized responses: "What are MY account details?" must not return another user's data
  - Real-time data: "What is the current Bitcoin price?" must not return a stale cached price
  - Creative or generative tasks: "Write me a poem about autumn" — every call should differ
  - Low-volume apps: embedding overhead is ~5-10ms; for <10 RPS the savings don't justify complexity
"""

import os
from dotenv import load_dotenv
from langchain_core.globals import set_llm_cache
from langchain_openai import OpenAIEmbeddings      # embeddings model for semantic matching
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
import time

load_dotenv()

# --- 1. Configure the semantic cache ---
#
# RedisSemanticCache stores (embedding_vector, prompt, response) in Redis.
# score_threshold: cosine similarity cutoff for a cache hit.
#   - 0.0 = always hit (dangerous: returns cache for anything)
#   - 1.0 = exact match only (same as InMemoryCache)
#   - 0.9 = recommended starting point for English Q&A
#   - 0.8 = more aggressive caching; may return answers to subtly different questions
#
# Tune score_threshold in your staging environment:
# - Too high (0.99): cache rarely hits, savings minimal
# - Too low  (0.75): cache returns wrong answers to different questions (cache drift)
# - Recommended: start at 0.9, monitor cache_hit rate and answer quality

try:
    from langchain_redis import RedisSemanticCache
    _redis_available = True
except ImportError:
    _redis_available = False
    print("langchain-redis not installed — falling back to InMemoryCache")

if _redis_available:
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    # text-embedding-3-small: 1536 dimensions, $0.02/M tokens — cheap for cache keys

    semantic_cache = RedisSemanticCache(
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        embedding=embeddings,
        score_threshold=0.9,    # cosine similarity threshold — tune per application
        ttl=3600,               # optional: expire cache entries after 1 hour (seconds)
                                # set ttl=None for no expiration
    )
    set_llm_cache(semantic_cache)
else:
    from langchain_core.caches import InMemoryCache
    set_llm_cache(InMemoryCache())

# --- 2. Build a normal chain — no caching-specific code ---
model = ChatAnthropic(model="claude-sonnet-4-6")
prompt = ChatPromptTemplate.from_template("Answer concisely: {question}")
chain = prompt | model | StrOutputParser()

# --- 3. First call: cache miss → LLM invoked, result stored ---
t0 = time.time()
r1 = chain.invoke({"question": "What is the capital of France?"})
print(f"Miss: {r1!r}  ({time.time()-t0:.2f}s)")
# → "Paris."  (1.2s — LLM call + embedding write to Redis)

# --- 4. Semantically similar question: cache HIT ---
t0 = time.time()
r2 = chain.invoke({"question": "Which city is the capital of France?"})
print(f"Hit:  {r2!r}  ({time.time()-t0:.3f}s)")
# → "Paris."  (0.015s — embedding lookup returned above threshold)

# --- 5. Different topic: cache miss ---
t0 = time.time()
r3 = chain.invoke({"question": "What is the capital of Germany?"})
print(f"Miss: {r3!r}  ({time.time()-t0:.2f}s)")
# → "Berlin."  (1.1s — different question, no close match in Redis)

# --- 6. Bypass cache for real-time or personalized calls ---
r4 = chain.invoke(
    {"question": "What is the current exchange rate for EUR/USD?"},
    config={"cache": False},   # always call LLM — never return stale rate
)
```

---

### Cache Key Mechanics

Understanding how the cache key is computed helps you reason about what will and will not hit the cache.

```
Query: "What is the capital of France?"
   ↓
Embedding model: text-embedding-3-small
   ↓
Vector: [0.021, -0.003, 0.147, ... ]  (1536 dimensions)
   ↓
Redis HNSW vector search: find nearest stored vector
   ↓
nearest_distance = 0.08   (cosine distance; 0 = identical, 1 = orthogonal)
similarity = 1 - distance = 0.92
   ↓
score_threshold = 0.9
0.92 >= 0.9 → CACHE HIT → return stored response
```

**What affects similarity:**
- Synonymous questions score 0.85-0.98: "capital of France" ≈ "French capital city"
- Different topics score 0.0-0.5: "capital of France" vs "GDP of France"
- Same topic, different intent may score 0.7-0.85: "capital of France" vs "largest city in France" — these should NOT hit the same cache entry; use `score_threshold >= 0.88` to avoid this

**Cache entry structure in Redis:**
```
Key:   llmcache:{hash_of_namespace}
Fields:
  prompt_vector:   <1536-dim float32 array>
  prompt:          "Answer concisely: What is the capital of France?"
  response:        "Paris."
  timestamp:       1718700000
```

---

### Complete Production Setup

```python
"""
cache_setup.py — Production-ready semantic cache module.

Drop this into your application and call configure_cache() at startup.
The cache type and parameters are controlled by environment variables.
"""

from __future__ import annotations

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def configure_cache(
    cache_type: Optional[str] = None,
    score_threshold: float = 0.90,
    ttl_seconds: Optional[int] = 3600,
) -> None:
    """
    Configure the global LangChain LLM cache.

    Args:
        cache_type:      "redis_semantic" | "redis_exact" | "memory" | "none"
                         Defaults to LANGCHAIN_CACHE_TYPE env var, then "memory".
        score_threshold: Cosine similarity threshold for semantic cache hits (0.0-1.0).
                         Only applies to redis_semantic.
        ttl_seconds:     Cache entry TTL in seconds. None = no expiration.

    Call once at application startup before any LLM calls.
    """
    _type = cache_type or os.environ.get("LANGCHAIN_CACHE_TYPE", "memory")

    if _type == "redis_semantic":
        _configure_redis_semantic(score_threshold, ttl_seconds)

    elif _type == "redis_exact":
        _configure_redis_exact(ttl_seconds)

    elif _type == "memory":
        from langchain_core.globals import set_llm_cache
        from langchain_core.caches import InMemoryCache
        set_llm_cache(InMemoryCache())
        logger.info("LLM cache: InMemoryCache (exact match, dev only)")

    elif _type == "none":
        logger.info("LLM cache: disabled")

    else:
        raise ValueError(f"Unknown LANGCHAIN_CACHE_TYPE: {_type!r}. Use: redis_semantic|redis_exact|memory|none")


def _configure_redis_semantic(score_threshold: float, ttl_seconds: Optional[int]) -> None:
    from langchain_core.globals import set_llm_cache
    from langchain_openai import OpenAIEmbeddings

    try:
        from langchain_redis import RedisSemanticCache
    except ImportError as e:
        raise ImportError("Run: pip install langchain-redis") from e

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    embeddings = OpenAIEmbeddings(
        model=os.environ.get("CACHE_EMBEDDING_MODEL", "text-embedding-3-small")
    )

    cache = RedisSemanticCache(
        redis_url=redis_url,
        embedding=embeddings,
        score_threshold=score_threshold,
        ttl=ttl_seconds,
    )
    set_llm_cache(cache)
    logger.info(
        "LLM cache: RedisSemanticCache url=%s threshold=%.2f ttl=%s",
        redis_url,
        score_threshold,
        ttl_seconds,
    )


def _configure_redis_exact(ttl_seconds: Optional[int]) -> None:
    from langchain_core.globals import set_llm_cache

    try:
        from langchain_redis import RedisCache
    except ImportError as e:
        raise ImportError("Run: pip install langchain-redis") from e

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    cache = RedisCache(redis_url=redis_url, ttl=ttl_seconds)
    set_llm_cache(cache)
    logger.info("LLM cache: RedisCache (exact match) url=%s ttl=%s", redis_url, ttl_seconds)
```

**Environment variables for production:**

```bash
# .env — cache configuration
LANGCHAIN_CACHE_TYPE=redis_semantic   # redis_semantic | redis_exact | memory | none
REDIS_URL=redis://redis:6379/0
CACHE_EMBEDDING_MODEL=text-embedding-3-small
CACHE_SCORE_THRESHOLD=0.90            # read this in your configure_cache() call
CACHE_TTL_SECONDS=3600                # 1 hour — adjust per use case
```

**Startup integration:**

```python
# main.py or app.py — call before any chain usage
from dotenv import load_dotenv
from cache_setup import configure_cache

load_dotenv()

configure_cache(
    cache_type="redis_semantic",
    score_threshold=float(os.environ.get("CACHE_SCORE_THRESHOLD", "0.90")),
    ttl_seconds=int(os.environ.get("CACHE_TTL_SECONDS", "3600")),
)

# All subsequent LLM calls in this process will use the semantic cache
```

---

### Trade-offs and Failure Modes

#### Cache Drift

Cache drift occurs when the semantically stored answer is no longer correct even though the new query is considered similar enough to hit the cache.

**Example:**
```
Stored: Q="What is the latest version of LangChain?" A="0.1.17"  (6 months ago)
New:    Q="What version of LangChain is current?"   similarity=0.93 → CACHE HIT
Result: returns "0.1.17" — wrong, the actual version is 0.3.x
```

**Mitigation:**
- Set `ttl_seconds` to a value appropriate for how often your facts change (e.g., `3600` for hourly data, `86400` for daily, `None` only for truly static facts)
- Raise `score_threshold` to reduce cross-topic bleed
- Never cache real-time data queries — always pass `config={"cache": False}` for those

#### Cache Invalidation

LangChain's built-in caches do not support selective invalidation (delete cache entries for a specific topic). Options:

```python
# Option 1: Clear the entire cache (nuclear option)
from langchain_core.globals import get_llm_cache
get_llm_cache().clear()

# Option 2: Use Redis TTL — entries expire automatically
# Set a short TTL for volatile data, long TTL for stable facts

# Option 3: Namespace your cache per knowledge domain
# Use separate RedisSemanticCache instances with different key prefixes
# for different parts of your application (not natively supported —
# work around by using different embedding namespaces)
```

#### When NOT to Use Semantic Caching

| Scenario | Why caching is wrong | Fix |
|---|---|---|
| Personalized responses | "What are my recent orders?" — different user = different correct answer | Pass `config={"cache": False}` for all user-specific queries |
| Real-time data | "What is the stock price of NVDA?" — cached answer is stale | Pass `config={"cache": False}` for any time-sensitive query |
| Creative/generative tasks | "Write me a poem" — users expect variety, not the same poem every time | Pass `config={"cache": False}` for all creative prompts |
| Low-volume / unique queries | Embedding overhead (~5-10ms) with near-zero hit rate wastes latency | Only enable semantic caching when hit rate > ~15% |
| Confidential data mixing | Cached responses from Tenant A may be returned to Tenant B | Use per-tenant namespaced cache instances, or disable caching for sensitive tenants |

---

### Cost Savings Estimation

For a Q&A application receiving 100,000 requests/day using `claude-sonnet-4-6` ($3/$15 per M):

| Hit rate | LLM calls saved | Approx daily savings |
|---|---|---|
| 40% | 40,000 | ~$4–$8 |
| 60% | 60,000 | ~$6–$12 |
| 80% | 80,000 | ~$8–$16 |

Embedding cost (cache miss path): 100,000 × 200 tokens × $0.02/M ≈ $0.40/day — negligible.

**FAQ applications** (help centers, documentation Q&A) typically achieve 60-80% hit rates because users ask the same questions in different words. **Conversational applications** with unique context per session typically achieve 5-20%.

---

### Common Mistakes: Section 10

| Mistake | Fix |
|---|---|
| `set_llm_cache()` called after the first LLM call | Call `configure_cache()` before any chain usage — at module import time or app startup |
| `score_threshold=0.0` — cache always hits | Start at 0.9 and tune down cautiously. 0.0 means every question returns the first cached answer |
| Not setting `ttl_seconds` for factual data | Without TTL, stale answers are returned forever. Match TTL to your data freshness requirement |
| Caching personalized queries | Use `config={"cache": False}` for any query that includes user-specific context |
| Using `InMemoryCache` in production with multiple workers | Each worker has its own cache — no sharing. Use Redis for multi-worker deployments |
| Not monitoring cache hit rate | Without observability you can't tell if caching is working. Log cache hits in a `RunnableLambda` or use Redis MONITOR to measure hit rate |
| Forgetting `langchain-redis` requires Redis Stack | Standard Redis does not have vector search. Use `redis/redis-stack` Docker image or Redis Cloud with Search enabled |

---

## Diagnostic Checklist

When an LCEL chain misbehaves, test each step in isolation:

```python
# 1. Does the prompt produce the right output?
print(prompt.invoke({"question": "test"}))

# 2. Does the model accept that output?
prompt_output = prompt.invoke({"question": "test"})
print(model.invoke(prompt_output))

# 3. Does the parser extract the right thing?
model_output = model.invoke(prompt_output)
print(parser.invoke(model_output))

# 4. Inspect the full chain's intermediate steps
chain.invoke({"question": "test"}, config={"run_name": "debug"})
# Then view the full trace at smith.langchain.com
```
