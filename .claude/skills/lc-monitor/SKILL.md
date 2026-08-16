---
name: lc-monitor
description: Use when setting up LangSmith observability for a LangChain or LangGraph project — tracing LLM calls, chain runs, agent steps, and tool calls; building evaluation datasets; running quality evaluations; tracking cost and latency in production; or configuring monitoring dashboards and alerts. Triggered by requests to add tracing, monitor production LLM quality, set up LangSmith, evaluate outputs, or debug what an agent did.
---

# lc:monitor — LangSmith Monitoring, Tracing & Evaluation

## Overview

LangSmith is Anthropic-agnostic observability for LangChain and LangGraph. It records every LLM call, chain step, agent decision, and tool invocation as a **trace** — a tree of nested runs. From traces you build evaluation datasets, run automated quality checks, and watch dashboards for cost/latency/error regressions.

**Core model:** Every run has a root span (one chain or agent invocation) and child spans (each LLM call, tool use, retrieval step). Traces flow to LangSmith automatically once `LANGSMITH_TRACING=true` is set.

---

## Skill Flow

1. Do you have LangSmith set up? → If not, complete Setup (Section 1) first
2. What do you want to monitor? → Quality / Cost / Latency / Errors / All
3. Scaffold tracing for the current project (Section 2–3)
4. Create initial evaluation dataset from existing code (Section 5)
5. Set up dashboards and alerts (Section 8)

---

## 1. Initial Setup

### Install

```bash
pip install langsmith
```

### Environment Variables

```bash
# .env (never commit)
LANGSMITH_API_KEY=ls__...          # from app.smith.langchain.com → Settings → API Keys
LANGSMITH_TRACING=true             # enables auto-tracing for all LangChain runs
LANGSMITH_PROJECT=my-project       # project bucket in LangSmith UI
```

Load in Python:

```python
# at top of main entry point
from dotenv import load_dotenv
load_dotenv()

# verify tracing is active
import os
assert os.environ.get("LANGSMITH_TRACING") == "true", "LangSmith tracing not enabled"
```

### Create a Project in the UI

1. Go to `app.smith.langchain.com`
2. Click **Projects** → **New Project**
3. Name it `my-project` (match `LANGSMITH_PROJECT`)
4. Run any LangChain call → first trace appears within seconds

### Verify First Trace

```python
from langchain_openai import ChatOpenAI  # or langchain_anthropic

llm = ChatOpenAI(model="gpt-4o-mini")
result = llm.invoke("Hello, world!")
print(result.content)
# Open LangSmith → Projects → my-project → you should see one trace
```

---

## 2. Automatic Tracing

Once `LANGSMITH_TRACING=true` is set, the following are traced with **zero code changes**:

| Component | What is recorded |
|---|---|
| LLM calls | Input messages, output, model, latency, token counts, cost |
| Chain calls | Input/output of each LCEL chain step |
| Agent steps | Thought, action, tool input/output, observation loop |
| Tool calls | Tool name, input args, output, errors |
| Retriever calls | Query, returned documents, similarity scores |
| Prompt templates | Rendered prompt text (not just template) |

### Trace Structure

```
root run  ← one invoke() / ainvoke() call
  ├─ LLM call        (gpt-4o-mini, 350 tokens, $0.0004)
  ├─ tool: search    (query="...", results=[...])
  ├─ LLM call        (gpt-4o-mini, 420 tokens, $0.0005)
  └─ tool: calculator(expr="...", result=42)
```

Each run has: `run_id`, `name`, `inputs`, `outputs`, `start_time`, `end_time`, `error`, `tags`, `metadata`.

### Adding Tags and Metadata at Call Time

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4o-mini")

# Tags appear as filterable labels in LangSmith UI
result = llm.invoke(
    "Summarize this document",
    config={
        "tags": ["summarization", "v2"],
        "metadata": {
            "user_id": "alice",
            "feature": "doc-summary",
            "environment": "production",
        },
        "run_name": "SummarizeDocument",   # custom display name in UI
    }
)
```

---

## 3. Manual Tracing

Use manual tracing when you want to trace non-LangChain functions (plain Python, external APIs, preprocessing steps).

### @traceable Decorator

```python
from langsmith import traceable

@traceable(name="PreprocessDocument", tags=["preprocessing"])
def preprocess(raw_text: str) -> str:
    """Wrap any Python function — its inputs and outputs are recorded."""
    text = raw_text.strip().lower()
    return text

@traceable(name="FetchUserProfile", metadata={"source": "db"})
def fetch_user(user_id: str) -> dict:
    # database call — appears as a child span in the parent trace
    return {"user_id": user_id, "tier": "pro"}

# These now appear as child spans when called inside a LangChain chain
```

### run_tree Context Manager (Custom Spans)

```python
from langsmith.run_trees import RunTree

def complex_pipeline(question: str) -> str:
    # Create a root run manually
    root = RunTree(
        name="ComplexPipeline",
        run_type="chain",
        inputs={"question": question},
    )

    # Child span for retrieval
    retrieval_run = root.create_child(
        name="Retrieval",
        run_type="retriever",
        inputs={"query": question},
    )
    docs = ["doc1 content", "doc2 content"]  # your retrieval
    retrieval_run.end(outputs={"documents": docs})
    retrieval_run.post()

    # Child span for LLM synthesis
    llm_run = root.create_child(
        name="Synthesis",
        run_type="llm",
        inputs={"prompt": f"Answer based on: {docs}\nQ: {question}"},
    )
    answer = "synthesized answer"   # your LLM call
    llm_run.end(outputs={"answer": answer})
    llm_run.post()

    root.end(outputs={"answer": answer})
    root.post()
    return answer
```

### Propagating Trace Context Across Services

```python
# Service A — pass the trace context header to Service B
from langsmith import get_current_run_tree

@traceable
def service_a_handler(request: dict) -> dict:
    run = get_current_run_tree()
    headers = {}
    if run:
        headers["langsmith-trace"] = run.to_headers()["langsmith-trace"]
    # Pass headers in your HTTP call to service B
    response = call_service_b(request, headers=headers)
    return response

# Service B — pick up the parent trace
from langsmith.run_helpers import traceable_with_parent_header

@traceable
def service_b_handler(request: dict) -> dict:
    # LangSmith automatically reads langsmith-trace header from request context
    return {"result": "processed"}
```

---

## 4. Production Tracing Patterns

### Project Naming by Environment

```python
import os

ENVIRONMENT = os.environ.get("APP_ENV", "dev")    # dev / staging / production
os.environ["LANGSMITH_PROJECT"] = f"my-app-{ENVIRONMENT}"
# Traces for each environment stay in separate projects
```

### User ID + Session ID Tracking

```python
from langchain_anthropic import ChatAnthropic

llm = ChatAnthropic(model="claude-sonnet-4-6")

def chat(user_id: str, session_id: str, message: str) -> str:
    return llm.invoke(
        message,
        config={
            "metadata": {
                "user_id": user_id,
                "session_id": session_id,
            },
            "tags": [f"user:{user_id}"],
        }
    ).content
```

### Cost Attribution Per User / Feature

```python
# metadata.user_id + metadata.feature let you group cost in LangSmith dashboards
config = {
    "metadata": {
        "user_id": "alice",
        "feature": "chat",           # or "doc-summary", "search", etc.
        "plan": "pro",
        "tenant_id": "acme-corp",
    }
}
```

### Sampling for High-Volume Production

```python
import random
import os

def should_trace(sampling_rate: float = 0.1) -> bool:
    """Sample 10% of requests — reduces LangSmith cost at scale."""
    return random.random() < sampling_rate

def llm_call(prompt: str, user_id: str) -> str:
    if should_trace():
        os.environ["LANGSMITH_TRACING"] = "true"
    else:
        os.environ["LANGSMITH_TRACING"] = "false"

    from langchain_anthropic import ChatAnthropic
    llm = ChatAnthropic(model="claude-sonnet-4-6")
    result = llm.invoke(prompt)
    os.environ["LANGSMITH_TRACING"] = "true"   # reset
    return result.content
```

---

## 5. Datasets and Evaluation

### Creating a Dataset from Existing Traces (Golden Set)

```
In LangSmith UI:
1. Projects → your-project → click any trace
2. Click "Add to Dataset" button (top right of trace view)
3. Select or create a dataset name
4. Confirm input/output fields to capture
```

### Adding Examples Programmatically

```python
from langsmith import Client

client = Client()

# Create dataset
dataset = client.create_dataset(
    "qa-golden-set",
    description="Question-answering golden examples",
)

# Add examples
examples = [
    {
        "inputs":  {"question": "What is the return policy?"},
        "outputs": {"answer": "30 days, no questions asked."},
    },
    {
        "inputs":  {"question": "How do I reset my password?"},
        "outputs": {"answer": "Click 'Forgot Password' on the login page."},
    },
]
client.create_examples(
    inputs=[e["inputs"] for e in examples],
    outputs=[e["outputs"] for e in examples],
    dataset_id=dataset.id,
)
```

### Running Evaluations with aevaluate()

```python
import asyncio
from langsmith.evaluation import aevaluate
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

# 1. The function under test
async def my_app(inputs: dict) -> dict:
    llm = ChatAnthropic(model="claude-sonnet-4-6")
    response = await llm.ainvoke(inputs["question"])
    return {"answer": response.content}

# 2. Evaluator using LLM-as-judge
def correctness_evaluator(run, example) -> dict:
    """Score whether the answer is correct vs the reference."""
    judge = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = f"""Score whether the AI answer is correct.
Reference: {example.outputs['answer']}
AI Answer: {run.outputs['answer']}
Return JSON: {{"score": 0.0-1.0, "reasoning": "..."}}"""
    import json
    result = json.loads(judge.invoke(prompt).content)
    return {"key": "correctness", "score": result["score"], "comment": result["reasoning"]}

# 3. Run evaluation
async def run_eval():
    results = await aevaluate(
        my_app,
        data="qa-golden-set",               # dataset name or ID
        evaluators=[correctness_evaluator],
        experiment_prefix="claude-sonnet-v1",
        max_concurrency=4,
    )
    print(results)

asyncio.run(run_eval())
```

### Regression Detection

```
In LangSmith UI:
1. Experiments tab → select two experiment runs
2. Click "Compare" → side-by-side score comparison
3. Filter by score drops: "correctness < 0.8"
4. Identify which examples regressed and why
```

---

## 6. Online Evaluation (Production Quality Gates)

### Evaluating Production Runs Automatically

```python
from langsmith import Client
from langchain_openai import ChatOpenAI
import json

client = Client()

def judge_production_run(run_id: str):
    """Pull a completed run and evaluate it as an LLM judge."""
    run = client.read_run(run_id)

    judge = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = f"""Rate the quality of this AI response on a scale of 0-1.
User question: {run.inputs.get('question', run.inputs)}
AI response: {run.outputs.get('answer', run.outputs)}
Return JSON: {{"score": 0.0-1.0, "issues": ["..."]}}"""

    result = json.loads(judge.invoke(prompt).content)

    # Post score back to the run
    client.create_feedback(
        run_id=run_id,
        key="quality",
        score=result["score"],
        comment=str(result.get("issues", [])),
    )
    return result
```

### Setting Up Evaluator Rules in UI

```
In LangSmith UI → Projects → your-project → Automations:
1. Click "New Automation"
2. Trigger: On run completion
3. Filter: tags contains "production"
4. Action: Run evaluator → select your LLM judge
5. Save → all matching runs are evaluated automatically
```

### Alerting on Quality Drops

```
In LangSmith UI → Alerts:
1. New Alert → Metric: feedback[quality].avg
2. Condition: drops below 0.7 over last 1 hour
3. Action: Slack webhook OR email
4. Message template: "Quality score dropped to {{value}} in {{project}}"
```

---

## 7. Custom Metrics and Feedback

### Logging Custom Scores to Runs

```python
from langsmith import Client, traceable

client = Client()

@traceable
def answer_question(question: str) -> str:
    from langchain_anthropic import ChatAnthropic
    llm = ChatAnthropic(model="claude-sonnet-4-6")
    answer = llm.invoke(question).content
    return answer

def answer_with_feedback(question: str, user_id: str) -> str:
    answer = answer_question(question)

    # Post business metric alongside LLM metric
    from langsmith.run_helpers import get_current_run_tree
    run = get_current_run_tree()
    if run:
        client.create_feedback(
            run_id=str(run.id),
            key="response_length",
            score=len(answer.split()),    # word count as a metric
            source_info={"user_id": user_id},
        )
    return answer
```

### Feedback API — Thumbs Up/Down from Users

```python
from langsmith import Client

client = Client()

def record_user_feedback(run_id: str, thumbs_up: bool, user_id: str):
    """Call this when user clicks thumbs up/down in your UI."""
    client.create_feedback(
        run_id=run_id,
        key="user_feedback",
        score=1.0 if thumbs_up else 0.0,
        source_info={"user_id": user_id},
        feedback_source_type="app",
    )
```

### Getting run_id to Surface to Your Application

```python
from langsmith.run_helpers import get_current_run_tree
from langchain_anthropic import ChatAnthropic

@traceable
def handle_request(question: str) -> dict:
    llm = ChatAnthropic(model="claude-sonnet-4-6")
    answer = llm.invoke(question).content

    run = get_current_run_tree()
    run_id = str(run.id) if run else None

    # Return run_id to frontend so it can post feedback later
    return {"answer": answer, "run_id": run_id}
```

---

## 8. Monitoring Dashboards

### Built-In Metrics (Available Immediately)

| Metric | Description |
|---|---|
| Latency P50 / P99 | Median and tail latency per chain/model |
| Requests per second | Volume over time |
| Error rate | % runs that ended with an error |
| Token usage | Input + output tokens, by model |
| Cost | Estimated USD cost, by model / project |
| Token breakdown | Tokens by model, grouped by tag/metadata |

### Setting Up Custom Charts

```
In LangSmith UI → Projects → your-project → Dashboard:
1. Add Widget → Chart type: Line / Bar / Number
2. Metric: feedback[correctness].avg   (or latency, token_count, cost)
3. Group by: metadata.feature          (shows cost per feature)
4. Filter: tags = "production"
5. Save Dashboard
```

### Alert Configuration

```
In LangSmith UI → Alerts → New Alert:

Cost alert:
  Metric:    cost.sum
  Condition: > $50 per day
  Action:    Email owner@example.com

Latency alert:
  Metric:    latency.p99
  Condition: > 5000ms over last 15 minutes
  Action:    Slack webhook

Error rate alert:
  Metric:    error_rate
  Condition: > 5% over last 30 minutes
  Action:    Slack webhook
```

### Token Usage Breakdown by Model

```python
# Tag runs with model name for dashboard grouping
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

def llm_for(task: str):
    """Route to different models by task; tag accordingly."""
    if task in ("summarize", "classify"):
        llm = ChatAnthropic(model="claude-haiku-4-5")
        tags = ["model:claude-haiku", f"task:{task}"]
    else:
        llm = ChatAnthropic(model="claude-sonnet-4-6")
        tags = ["model:claude-sonnet", f"task:{task}"]
    return llm, tags
```

---

## 9. Prompt Hub

### Pushing a Prompt

```python
from langsmith import Client
from langchain_core.prompts import ChatPromptTemplate

client = Client()

prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful assistant specializing in {domain}."),
    ("human", "{question}"),
])

# Push to hub — creates version 1 (or increments version)
client.push_prompt("my-assistant-prompt", object=prompt)
# → accessible at: https://smith.langchain.com/hub/your-org/my-assistant-prompt
```

### Pulling in Production (Version Pinned)

```python
from langsmith import Client

client = Client()

# Pin to specific version for production stability
prompt = client.pull_prompt("my-assistant-prompt:3")

# Or pull latest (risky in production — prompt changes affect behavior)
prompt_latest = client.pull_prompt("my-assistant-prompt:latest")

# Use like any LangChain prompt
chain = prompt | llm
result = chain.invoke({"domain": "finance", "question": "What is NAV?"})
```

### A/B Testing Prompt Versions

```python
import random
from langsmith import Client

client = Client()

def get_prompt_for_ab_test(user_id: str) -> tuple:
    """50/50 split between prompt versions 3 and 4."""
    # Deterministic by user_id for consistent experience
    variant = "v3" if hash(user_id) % 2 == 0 else "v4"
    version = "3" if variant == "v3" else "4"
    prompt = client.pull_prompt(f"my-assistant-prompt:{version}")
    return prompt, variant

from langchain_anthropic import ChatAnthropic

def answer_with_ab(question: str, user_id: str) -> str:
    llm = ChatAnthropic(model="claude-sonnet-4-6")
    prompt, variant = get_prompt_for_ab_test(user_id)
    chain = prompt | llm

    result = chain.invoke(
        {"domain": "general", "question": question},
        config={
            "metadata": {"ab_variant": variant, "user_id": user_id},
            "tags": [f"ab:{variant}"],
        }
    )
    return result.content

# In LangSmith: filter by tag "ab:v3" vs "ab:v4" to compare quality/cost/latency
```

---

## Quick Reference

### Environment Variables

```bash
LANGSMITH_API_KEY=ls__...        # required
LANGSMITH_TRACING=true           # required to enable
LANGSMITH_PROJECT=my-project     # optional; default is "default"
LANGSMITH_ENDPOINT=https://api.smith.langchain.com  # default; override for self-hosted
```

### Key SDK Objects

```python
from langsmith import Client, traceable
from langsmith.run_trees import RunTree
from langsmith.run_helpers import get_current_run_tree
from langsmith.evaluation import aevaluate, evaluate
```

### Common Operations

| Task | Code |
|---|---|
| Trace a function | `@traceable` decorator |
| Add metadata to a run | `config={"metadata": {...}}` |
| Post user feedback | `client.create_feedback(run_id, key, score)` |
| Create dataset | `client.create_dataset(name)` |
| Add examples | `client.create_examples(inputs, outputs, dataset_id)` |
| Run evaluation | `await aevaluate(fn, data="dataset-name", evaluators=[...])` |
| Push prompt | `client.push_prompt(name, object=prompt)` |
| Pull prompt | `client.pull_prompt("name:version")` |

---

## Common Mistakes

| Mistake | Fix |
|---|---|
| `LANGSMITH_TRACING_V2=true` (old env var) | Use `LANGSMITH_TRACING=true` — v2 is deprecated |
| Tracing enabled in tests, polluting the project | Set `LANGSMITH_PROJECT=my-project-test` in test env |
| Not pinning prompt versions in production | Always use `pull_prompt("name:N")` not `:latest` in prod |
| Sampling by toggling env var (not thread-safe) | Use `with tracing_context(enabled=False):` or `@traceable(enabled=False)` for per-call control |
| Forgetting to return `run_id` to frontend | Return it from the traceable function; needed for user feedback |
| Large metadata values bloating traces | Keep metadata values to strings/numbers; store large payloads in your own DB and reference by ID |
| Comparing experiment runs without enough examples | Use at least 50 examples per dataset for statistically meaningful comparisons |

---

## Install Reference

```bash
pip install langsmith                          # core
pip install langchain-anthropic                # Claude models
pip install langchain-openai                   # OpenAI models (for LLM-as-judge)
pip install python-dotenv                      # .env loading
```

---

## See Also

- `lc:rag` — RAG pipelines (tracing section covers eval integration)
- `lc-agent` — Agent patterns (tracing is built into all agent scaffolds)
- `lc-memory` — Memory patterns (checkpointing and Store API)
- `lc-lcel` — LCEL pipelines (Section 10 covers semantic caching for cost reduction)

---

## 12. Cost Management

### Overview

LLM API costs can grow quickly in production. This section covers four layers of cost control: real-time per-call cost tracking via a callback, per-tenant metering in Redis, hard budget gates at graph entry, and model routing to match model capability to task complexity. Combine all four for full cost observability and control.

---

### Pricing Reference Table

| Model | Input (per M tokens) | Output (per M tokens) |
|---|---|---|
| `claude-sonnet-4-6` | $3.00 | $15.00 |
| `claude-haiku-4-5` | $0.25 | $1.25 |
| `gpt-4o` | $5.00 | $15.00 |

Add new models to the `COST_TABLE` dict below. Prices are USD; check the Anthropic Console and OpenAI pricing pages for current rates — these figures were correct as of mid-2025.

---

### CostTrackingCallback

`CostTrackingCallback` hooks into `on_llm_end` to read `usage_metadata` from the LLM response, compute dollar cost, and write it to Redis. It also emits a structured log line for your log aggregator and tags the active LangSmith run with the computed cost.

```python
"""
cost_tracking_callback.py

A LangChain BaseCallbackHandler that:
  1. Reads usage_metadata from every LLM response (input_tokens, output_tokens, model_name)
  2. Looks up per-million-token prices from COST_TABLE
  3. Writes cost to Redis with INCRBYFLOAT "cost:{tenant_id}:{YYYY-MM-DD}"
  4. Tags the LangSmith run with the cost for dashboard grouping
  5. Emits a structured log line for external log aggregators

Environment variables:
  REDIS_URL         Redis connection URL (default: redis://localhost:6379/0)
  COST_LOG_LEVEL    Logging level for cost lines (default: INFO)
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

import redis
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from langsmith import Client as LangSmithClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing table — input and output costs per million tokens, USD
# ---------------------------------------------------------------------------
COST_TABLE: Dict[str, Dict[str, float]] = {
    # Anthropic
    "claude-sonnet-4-6":           {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5":            {"input": 0.25,  "output": 1.25},
    "claude-opus-4-5":             {"input": 15.00, "output": 75.00},
    # OpenAI
    "gpt-4o":                      {"input": 5.00,  "output": 15.00},
    "gpt-4o-mini":                 {"input": 0.15,  "output": 0.60},
    "gpt-4-turbo":                 {"input": 10.00, "output": 30.00},
    # Add new models here — changes take effect without restarting
}

# Fallback for unknown models — logs a warning and uses this rate
_UNKNOWN_MODEL_COST = {"input": 10.00, "output": 30.00}

# Redis key TTL — keep daily counters for 90 days
_REDIS_TTL_SECONDS = 90 * 24 * 3600


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Compute USD cost for one LLM call.

    Args:
        model:         Model name string (e.g., "claude-sonnet-4-6").
        input_tokens:  Number of input/prompt tokens consumed.
        output_tokens: Number of output/completion tokens generated.

    Returns:
        Cost in USD as a float (e.g., 0.000312).
    """
    rates = COST_TABLE.get(model)
    if rates is None:
        logger.warning(
            "CostTrackingCallback: unknown model %r — using fallback rate $%.2f/$%.2f per M",
            model,
            _UNKNOWN_MODEL_COST["input"],
            _UNKNOWN_MODEL_COST["output"],
        )
        rates = _UNKNOWN_MODEL_COST

    cost = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000
    return cost


class CostTrackingCallback(BaseCallbackHandler):
    """
    LangChain callback handler for per-call and per-tenant cost tracking.

    Usage:
        # Attach to a single chain call
        tracker = CostTrackingCallback(tenant_id="acme-corp")
        result = chain.invoke(inputs, config={"callbacks": [tracker]})
        print(f"This call cost ${tracker.session_cost:.6f}")

        # Or attach at model construction for all calls from that model
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            callbacks=[CostTrackingCallback(tenant_id="acme-corp")],
        )
    """

    def __init__(
        self,
        tenant_id: str,
        redis_url: Optional[str] = None,
        langsmith_client: Optional[LangSmithClient] = None,
    ):
        super().__init__()
        self.tenant_id = tenant_id
        self.session_cost: float = 0.0   # accumulated cost for this callback instance

        # Redis connection — lazy, single connection per callback instance
        _redis_url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self._redis: redis.Redis = redis.from_url(_redis_url, decode_responses=True)

        # LangSmith client for tagging runs with cost
        self._ls_client: Optional[LangSmithClient] = langsmith_client

    # ------------------------------------------------------------------
    # Callback hook
    # ------------------------------------------------------------------

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Called after every LLM call completes. Compute and record cost."""
        # --- 1. Extract usage_metadata ---
        # LangChain populates response.llm_output["usage_metadata"] for supported models.
        # Structure varies slightly by provider; we normalise here.
        usage = self._extract_usage(response)
        if usage is None:
            logger.debug("CostTrackingCallback: no usage_metadata in response — skipping")
            return

        model       = usage.get("model_name", "unknown")
        input_tok   = int(usage.get("input_tokens", 0))
        output_tok  = int(usage.get("output_tokens", 0))

        # --- 2. Compute cost ---
        cost = compute_cost(model, input_tok, output_tok)
        self.session_cost += cost

        # --- 3. Persist to Redis ---
        today = date.today().isoformat()           # "2026-06-18"
        redis_key = f"cost:{self.tenant_id}:{today}"
        new_total = self._redis.incrbyfloat(redis_key, cost)
        # Set TTL on first write (INCRBYFLOAT creates the key if absent)
        self._redis.expire(redis_key, _REDIS_TTL_SECONDS)

        # --- 4. Emit structured log line ---
        logger.info(
            "llm_cost model=%s tenant=%s input_tokens=%d output_tokens=%d "
            "call_cost_usd=%.6f daily_total_usd=%.4f date=%s run_id=%s",
            model,
            self.tenant_id,
            input_tok,
            output_tok,
            cost,
            new_total,
            today,
            str(run_id),
        )

        # --- 5. Tag the LangSmith run with cost (optional) ---
        if self._ls_client is not None:
            try:
                self._ls_client.create_feedback(
                    run_id=str(run_id),
                    key="call_cost_usd",
                    score=cost,
                    comment=f"model={model} input={input_tok} output={output_tok}",
                )
            except Exception:
                logger.debug("CostTrackingCallback: failed to tag LangSmith run", exc_info=True)

    # ------------------------------------------------------------------
    # Helper: normalise usage_metadata across providers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_usage(response: LLMResult) -> Optional[Dict[str, Any]]:
        """
        Extract token usage from LLMResult.llm_output.

        LangChain stores usage differently per provider:
          - Anthropic: response.llm_output["usage"] = {"input_tokens": N, "output_tokens": N}
          - OpenAI:    response.llm_output["token_usage"] = {"prompt_tokens": N, "completion_tokens": N}
        We normalise both into {"input_tokens": N, "output_tokens": N, "model_name": "..."}.
        """
        if not response.llm_output:
            return None

        out = response.llm_output

        # Anthropic format
        if "usage" in out:
            u = out["usage"]
            return {
                "input_tokens":  u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "model_name":    out.get("model", "unknown"),
            }

        # OpenAI format
        if "token_usage" in out:
            u = out["token_usage"]
            return {
                "input_tokens":  u.get("prompt_tokens", 0),
                "output_tokens": u.get("completion_tokens", 0),
                "model_name":    out.get("model_name", "unknown"),
            }

        # LangChain ≥0.2 unified format
        if "usage_metadata" in out:
            u = out["usage_metadata"]
            return {
                "input_tokens":  u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "model_name":    out.get("model_name", "unknown"),
            }

        return None
```

---

### Per-Tenant Cost Metering

Redis stores one floating-point counter per tenant per day. The key format is `cost:{tenant_id}:{YYYY-MM-DD}`.

```python
"""
cost_metering.py — Query and display per-tenant cost from Redis.
"""

import os
from datetime import date, timedelta
from typing import Optional
import redis

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def get_daily_cost(tenant_id: str, for_date: Optional[date] = None) -> float:
    """Return accumulated USD cost for a tenant on a given date (default: today)."""
    r = redis.from_url(_REDIS_URL, decode_responses=True)
    d = (for_date or date.today()).isoformat()
    value = r.get(f"cost:{tenant_id}:{d}")
    return float(value) if value else 0.0


def get_mtd_cost(tenant_id: str) -> float:
    """Return month-to-date cost for a tenant by summing all daily keys this month."""
    r = redis.from_url(_REDIS_URL, decode_responses=True)
    today = date.today()
    total = 0.0
    for day_offset in range(today.day):
        d = (today - timedelta(days=day_offset)).isoformat()
        value = r.get(f"cost:{tenant_id}:{d}")
        if value:
            total += float(value)
    return total


def reset_daily_cost(tenant_id: str, for_date: Optional[date] = None) -> None:
    """Zero out a tenant's daily counter (useful for testing or billing resets)."""
    r = redis.from_url(_REDIS_URL, decode_responses=True)
    d = (for_date or date.today()).isoformat()
    r.delete(f"cost:{tenant_id}:{d}")


# Example: print a simple cost report
if __name__ == "__main__":
    for tenant in ["acme-corp", "beta-inc", "free-tier-user"]:
        today_cost = get_daily_cost(tenant)
        mtd_cost   = get_mtd_cost(tenant)
        print(f"{tenant:30s}  today=${today_cost:.4f}  MTD=${mtd_cost:.4f}")
```

---

### Budget Alerts: CostBudgetExceeded Gate

Check the Redis cost counter at the entry point of a LangGraph graph or before any expensive chain. Raise `CostBudgetExceeded` if the tenant has exceeded their daily budget.

```python
"""
budget_gate.py

Raise CostBudgetExceeded before executing an LLM call if the tenant has
exceeded their configured daily spend limit.

Typical placement: as the first node in a LangGraph graph, or as a
RunnableLambda at the start of an LCEL chain.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Optional
import redis


class CostBudgetExceeded(Exception):
    """Raised when a tenant's daily spend limit has been reached."""
    def __init__(self, tenant_id: str, current: float, limit: float):
        self.tenant_id = tenant_id
        self.current   = current
        self.limit     = limit
        super().__init__(
            f"Tenant '{tenant_id}' has exceeded daily budget: "
            f"${current:.4f} spent of ${limit:.2f} limit"
        )


# Default daily budgets per tenant tier (USD)
# Override by passing budgets= to check_budget(), or store limits in Redis/DB
DEFAULT_BUDGETS: dict[str, float] = {
    "free":       1.00,    # $1/day for free tier
    "pro":        20.00,   # $20/day for pro tier
    "enterprise": 500.00,  # $500/day for enterprise
}


def check_budget(
    tenant_id: str,
    budget_usd: Optional[float] = None,
    redis_url: Optional[str] = None,
) -> None:
    """
    Read the current daily cost counter from Redis and raise CostBudgetExceeded
    if it exceeds the configured limit.

    Args:
        tenant_id:   The tenant to check.
        budget_usd:  Daily budget in USD. If None, uses DEFAULT_BUDGETS["pro"].
        redis_url:   Redis URL. Defaults to REDIS_URL env var.

    Raises:
        CostBudgetExceeded: if the tenant has exceeded their daily budget.
    """
    limit = budget_usd or DEFAULT_BUDGETS.get("pro", 20.00)
    _redis_url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    r = redis.from_url(_redis_url, decode_responses=True)
    today = date.today().isoformat()
    raw = r.get(f"cost:{tenant_id}:{today}")
    current = float(raw) if raw else 0.0

    if current >= limit:
        raise CostBudgetExceeded(tenant_id, current, limit)


# --- LangGraph integration ---
# Place check_budget_node as the first node in your StateGraph.

from typing import TypedDict

class AgentState(TypedDict):
    question: str
    tenant_id: str
    answer: str

def check_budget_node(state: AgentState) -> AgentState:
    """
    LangGraph node: checks budget before any LLM work.
    Raises CostBudgetExceeded if over limit — LangGraph surfaces this as an error.
    """
    check_budget(
        tenant_id=state["tenant_id"],
        budget_usd=DEFAULT_BUDGETS.get("pro", 20.00),
    )
    return state   # pass through unchanged if budget is OK


# --- LCEL integration ---
# Wrap check_budget as a RunnableLambda at the start of a chain.

from langchain_core.runnables import RunnableLambda

def make_budget_gate(budget_usd: float):
    """
    Returns a RunnableLambda that checks budget before passing input downstream.

    Usage:
        chain = make_budget_gate(budget_usd=5.00) | rag_chain
        # Raises CostBudgetExceeded before touching the LLM if over budget
    """
    def _gate(inputs: dict) -> dict:
        check_budget(
            tenant_id=inputs["tenant_id"],
            budget_usd=budget_usd,
        )
        return inputs

    return RunnableLambda(_gate)


# --- FastAPI integration ---
# Catch CostBudgetExceeded in a middleware or exception handler.

"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from budget_gate import CostBudgetExceeded

app = FastAPI()

@app.exception_handler(CostBudgetExceeded)
async def budget_exceeded_handler(request: Request, exc: CostBudgetExceeded):
    return JSONResponse(
        status_code=402,   # Payment Required
        content={
            "error":      "budget_exceeded",
            "tenant_id":  exc.tenant_id,
            "spent_usd":  round(exc.current, 4),
            "limit_usd":  exc.limit,
            "message":    "Daily spending limit reached. Upgrade your plan or wait until midnight UTC.",
        },
    )
"""
```

---

### Model Routing by Complexity

Route cheap classification tasks to Haiku and expensive reasoning tasks to Sonnet. This alone typically cuts costs by 60-80% on mixed-workload applications without measurable quality loss on simple tasks.

```python
"""
model_routing.py

Route LLM calls to the cheapest model that can handle the task.

Decision logic:
  - classification / sentiment / extraction → claude-haiku-4-5  ($0.25/$1.25 per M)
  - summarization / QA / chat → claude-sonnet-4-6               ($3/$15 per M)
  - complex reasoning / code / long-context → claude-opus-4-5   ($15/$75 per M)
"""

from __future__ import annotations

from enum import Enum
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser


class TaskComplexity(str, Enum):
    SIMPLE    = "simple"     # classification, extraction, sentiment, yes/no
    MODERATE  = "moderate"   # summarization, QA, structured output, chat
    COMPLEX   = "complex"    # multi-step reasoning, code generation, long documents


# Map complexity level to the cheapest appropriate model
_MODEL_MAP: dict[TaskComplexity, str] = {
    TaskComplexity.SIMPLE:   "claude-haiku-4-5",
    TaskComplexity.MODERATE: "claude-sonnet-4-6",
    TaskComplexity.COMPLEX:  "claude-opus-4-5",
}

# Estimated cost ratio (Haiku = 1x baseline)
_COST_RATIO: dict[TaskComplexity, str] = {
    TaskComplexity.SIMPLE:   "1x  (~$0.25/$1.25 per M)",
    TaskComplexity.MODERATE: "12x (~$3/$15 per M)",
    TaskComplexity.COMPLEX:  "60x (~$15/$75 per M)",
}


def route_by_complexity(complexity: TaskComplexity) -> ChatAnthropic:
    """
    Return a ChatAnthropic instance pre-configured for the given complexity.

    Args:
        complexity: TaskComplexity enum value describing the task.

    Returns:
        ChatAnthropic configured with the cheapest appropriate model.

    Example:
        llm = route_by_complexity(TaskComplexity.SIMPLE)
        result = llm.invoke("Is this review positive or negative? 'Great product!'")
    """
    model_name = _MODEL_MAP[complexity]
    return ChatAnthropic(model=model_name, temperature=0)


# --- Auto-classify task complexity with a cheap Haiku call ---

_CLASSIFY_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a task classifier. Given a user request, output exactly one word: "
        "SIMPLE, MODERATE, or COMPLEX.\n\n"
        "SIMPLE:   classification, sentiment, yes/no, entity extraction, short translation\n"
        "MODERATE: summarization, factual QA, structured JSON output, general chat\n"
        "COMPLEX:  multi-step reasoning, code generation, documents >4000 tokens, "
        "          math proofs, adversarial robustness"
    )),
    ("human", "{task_description}"),
])

_classify_chain = (
    _CLASSIFY_PROMPT
    | ChatAnthropic(model="claude-haiku-4-5", temperature=0, max_tokens=5)
    | StrOutputParser()
)


def auto_route(task_description: str) -> ChatAnthropic:
    """
    Classify a task description and return the appropriate model.

    The classification itself uses Haiku (cheapest), so the overhead is minimal.
    Total cost of the classification call: ~0.000005 USD.

    Args:
        task_description: Plain-English description of the task (not the full prompt).

    Returns:
        ChatAnthropic configured with the cheapest model for the classified complexity.

    Example:
        llm = auto_route("Classify customer feedback as positive/negative")
        # → returns ChatAnthropic(model="claude-haiku-4-5")

        llm = auto_route("Write a Python function that implements a red-black tree")
        # → returns ChatAnthropic(model="claude-opus-4-5")
    """
    raw = _classify_chain.invoke({"task_description": task_description}).strip().upper()
    try:
        complexity = TaskComplexity(raw.lower())
    except ValueError:
        # If classification returns something unexpected, default to moderate
        complexity = TaskComplexity.MODERATE

    model = _MODEL_MAP[complexity]
    print(f"[route_by_complexity] task={task_description[:60]!r} → {complexity.value} → {model}")
    return ChatAnthropic(model=model, temperature=0)


# --- Example usage ---

if __name__ == "__main__":
    # Explicit routing
    simple_llm   = route_by_complexity(TaskComplexity.SIMPLE)
    moderate_llm = route_by_complexity(TaskComplexity.MODERATE)
    complex_llm  = route_by_complexity(TaskComplexity.COMPLEX)

    # Classification task → haiku
    result = simple_llm.invoke("Is this sentiment positive or negative? 'The product broke on day one.'")
    print(result.content)

    # Summarization task → sonnet
    result = moderate_llm.invoke("Summarize the key concepts of retrieval-augmented generation.")
    print(result.content)

    # Auto-routing
    llm = auto_route("Extract all dates and amounts from this invoice text")
    print(f"Auto-routed to: {llm.model}")
    # → claude-haiku-4-5 (extraction = SIMPLE)

    llm = auto_route("Write unit tests for a distributed rate limiter in Go")
    print(f"Auto-routed to: {llm.model}")
    # → claude-opus-4-5 (code + complexity = COMPLEX)
```

---

### Anthropic Console Hard Spend Cap

Set a hard spend cap in the Anthropic Console to prevent runaway charges regardless of application bugs or attacks:

```
Anthropic Console → https://console.anthropic.com/settings/billing
→ Billing → Spend Limits
→ Set "Monthly spend limit" to your maximum acceptable monthly charge
→ Optionally set "Email alert at" to 80% of the limit for early warning
```

The Console hard cap is enforced server-side — it cannot be bypassed by application code. Set it to 110% of your expected monthly spend to allow headroom while protecting against runaway costs. The `CostBudgetExceeded` gate above operates at the per-tenant application layer; the Console cap is the final backstop for your entire Anthropic account.

---

### LangSmith Cost Dashboard: Run Tagging for Attribution

Tag every run with `user_id`, `feature`, and `env` so LangSmith can group costs by any dimension in dashboards.

```python
"""
cost_tagging.py — Tag every run for LangSmith cost attribution.
"""

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from cost_tracking_callback import CostTrackingCallback

model = ChatAnthropic(model="claude-sonnet-4-6")
prompt = ChatPromptTemplate.from_template("Answer: {question}")
chain = prompt | model | StrOutputParser()


def run_with_cost_attribution(
    question: str,
    user_id: str,
    tenant_id: str,
    feature: str,
    env: str = "production",
) -> dict:
    """
    Invoke the chain with full cost attribution metadata.

    In LangSmith dashboards you can then:
      - Group by metadata.feature → see cost per product feature
      - Group by metadata.user_id → identify high-cost users
      - Filter by metadata.env   → separate prod vs staging costs
      - Filter by tags            → isolate experiments or A/B variants
    """
    tracker = CostTrackingCallback(tenant_id=tenant_id)

    result = chain.invoke(
        {"question": question},
        config={
            "run_name": f"{feature}-query",
            "tags": [
                f"user:{user_id}",
                f"feature:{feature}",
                f"env:{env}",
                f"tenant:{tenant_id}",
            ],
            "metadata": {
                "user_id":   user_id,
                "tenant_id": tenant_id,
                "feature":   feature,
                "env":       env,
            },
            "callbacks": [tracker],
        },
    )

    return {
        "answer":      result,
        "call_cost":   tracker.session_cost,
    }


# LangSmith Dashboard setup:
# Projects → your-project → Dashboard → Add Widget:
#   Chart:  Bar / Line
#   Metric: cost.sum  (or feedback[call_cost_usd].sum)
#   Group by: metadata.feature    → cost by feature
#   Group by: metadata.tenant_id  → cost by tenant
#   Group by: metadata.user_id    → cost by user
#   Filter:  metadata.env = "production"
```

---

### Shadow Model Comparison

Run both an expensive model and a cheap model on the same input, log both outputs to LangSmith, and compare quality before committing to the cheaper model in production.

```python
"""
shadow_comparison.py

Shadow model comparison: run the expensive model (primary) and a cheap model
(shadow) on every request. Log both outputs to LangSmith with quality scores.
After N requests, review the comparison in LangSmith to decide whether to
promote the cheap model.

Use case: you are paying for claude-sonnet-4-6 but want to know if
claude-haiku-4-5 produces acceptable quality for this particular task.
"""

import asyncio
from typing import Optional
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langsmith import Client, traceable

client = Client()

_prompt = ChatPromptTemplate.from_template("{question}")
_parser = StrOutputParser()

_primary_chain = _prompt | ChatAnthropic(model="claude-sonnet-4-6", temperature=0) | _parser
_shadow_chain  = _prompt | ChatAnthropic(model="claude-haiku-4-5",  temperature=0) | _parser

_judge_chain = (
    ChatPromptTemplate.from_messages([
        ("system", (
            "You are a quality judge. Compare two AI answers to the same question. "
            "Score the shadow answer relative to the primary: "
            "1.0 = equally good, 0.0 = completely wrong. "
            "Return JSON only: {\"score\": float, \"reason\": str}"
        )),
        ("human", (
            "Question: {question}\n\n"
            "Primary answer (claude-sonnet-4-6):\n{primary}\n\n"
            "Shadow answer (claude-haiku-4-5):\n{shadow}"
        )),
    ])
    | ChatAnthropic(model="claude-haiku-4-5", temperature=0)
    | _parser
)


@traceable(name="ShadowComparison", tags=["shadow-test"])
async def shadow_compare(question: str, tenant_id: str) -> dict:
    """
    Run primary and shadow models concurrently. Score the shadow answer.
    Both runs are recorded as child spans in LangSmith.

    Args:
        question:  The user question to send to both models.
        tenant_id: Used for cost metering metadata.

    Returns:
        dict with keys: primary_answer, shadow_answer, shadow_quality_score, reason
    """
    # Run both models concurrently — total latency ≈ max(primary, shadow)
    primary_answer, shadow_answer = await asyncio.gather(
        _primary_chain.ainvoke(
            {"question": question},
            config={
                "run_name": "primary-sonnet",
                "tags": ["shadow:primary"],
                "metadata": {"tenant_id": tenant_id, "model": "claude-sonnet-4-6"},
            },
        ),
        _shadow_chain.ainvoke(
            {"question": question},
            config={
                "run_name": "shadow-haiku",
                "tags": ["shadow:candidate"],
                "metadata": {"tenant_id": tenant_id, "model": "claude-haiku-4-5"},
            },
        ),
    )

    # Judge the shadow answer quality using a cheap Haiku call
    import json
    raw_judgment = await _judge_chain.ainvoke({
        "question": question,
        "primary":  primary_answer,
        "shadow":   shadow_answer,
    })

    try:
        judgment = json.loads(raw_judgment)
        score  = float(judgment.get("score", 0.5))
        reason = judgment.get("reason", "")
    except (json.JSONDecodeError, KeyError):
        score, reason = 0.5, "parse error"

    # Log shadow quality to LangSmith for dashboard analysis
    from langsmith.run_helpers import get_current_run_tree
    run = get_current_run_tree()
    if run:
        client.create_feedback(
            run_id=str(run.id),
            key="shadow_quality",
            score=score,
            comment=reason,
        )

    return {
        "primary_answer":        primary_answer,
        "shadow_answer":         shadow_answer,
        "shadow_quality_score":  score,   # 1.0 = shadow as good as primary
        "reason":                reason,
    }


# --- Analysis workflow ---
#
# After running shadow_compare() for 100+ real requests:
#
# In LangSmith:
#   Projects → your-project → Filter tag: "shadow-test"
#   Sort by: feedback[shadow_quality].avg
#
# If shadow_quality.avg >= 0.85 across your workload:
#   → Safe to promote claude-haiku-4-5 for this task
#   → Cost reduction: ~12x cheaper per call
#
# If shadow_quality.avg < 0.70:
#   → Keep primary model; haiku is not sufficient for this task
#
# Segment by question type:
#   Filter metadata.feature = "classification" → may be 0.95 (safe to switch)
#   Filter metadata.feature = "reasoning"      → may be 0.65 (keep sonnet)
```

---

### Cost Management Quick Reference

```python
# Install
# pip install redis langchain-anthropic langsmith

# 1. Track cost per call
from cost_tracking_callback import CostTrackingCallback
tracker = CostTrackingCallback(tenant_id="acme-corp")
result = chain.invoke(inputs, config={"callbacks": [tracker]})
print(f"Cost: ${tracker.session_cost:.6f}")

# 2. Check tenant budget before calling
from budget_gate import check_budget, CostBudgetExceeded
try:
    check_budget("acme-corp", budget_usd=5.00)
except CostBudgetExceeded as e:
    return {"error": str(e)}

# 3. Route by complexity
from model_routing import route_by_complexity, auto_route, TaskComplexity
llm = route_by_complexity(TaskComplexity.SIMPLE)    # haiku
llm = route_by_complexity(TaskComplexity.MODERATE)  # sonnet
llm = auto_route("classify this support ticket")    # auto-detected

# 4. Query Redis cost counters
from cost_metering import get_daily_cost, get_mtd_cost
today = get_daily_cost("acme-corp")                 # today's spend
mtd   = get_mtd_cost("acme-corp")                  # month-to-date

# 5. Tag runs for LangSmith cost dashboards
config = {
    "metadata": {"user_id": "u1", "feature": "chat", "env": "prod"},
    "tags": ["feature:chat", "env:prod"],
}
```

---

### Common Mistakes: Section 12

| Mistake | Fix |
|---|---|
| Constructing `CostTrackingCallback` outside a request, reusing it across tenants | Create a fresh instance per request — `session_cost` accumulates across calls on the same instance |
| Checking budget after the LLM call | Always check budget before the call — otherwise you've already spent the money |
| Using `INCRBYFLOAT` without setting TTL | Without `EXPIRE`, cost keys accumulate forever. The callback sets TTL automatically; set it manually if writing cost keys elsewhere |
| Relying solely on the Console hard cap | Console caps apply to your entire account, not per-tenant. The Redis budget gate enforces per-tenant limits |
| Not segmenting shadow results by task type | A shadow model may be great for classification but poor for reasoning — always break down results by feature/task |
| Running shadow comparison in production at 100% traffic | Start shadow testing at 5-10% sampling rate — enough to collect data without doubling your LLM spend |
| Using `auto_route()` for latency-sensitive paths | `auto_route()` makes an extra LLM call for classification (~50-100ms). For latency-critical paths, use explicit `route_by_complexity()` with a pre-classified task type |

---

## 10. Self-Hosted Observability Alternatives

### Mandatory Disclosure: LangSmith Data Residency

**LangSmith sends all LLM inputs and outputs to Langchain Inc. servers located in the United States.** This has direct GDPR implications:

- Every prompt, every LLM response, every tool input/output is transmitted to and stored on US servers operated by a US company.
- Under GDPR Article 46, transferring personal data outside the EEA to a third country requires adequate safeguards (SCCs, adequacy decisions). As of 2025, the EU-US Data Privacy Framework provides a mechanism, but reliance on it carries legal risk (successive Schrems rulings have invalidated prior frameworks).
- For regulated industries (healthcare under HIPAA, finance under MiFID II, public sector under NIS2), storing patient data, financial conversations, or citizen queries in a US SaaS product requires explicit DPA agreements and may be prohibited outright by sector-specific rules.
- LangSmith's enterprise tier offers a self-hosted deployment, but the default SaaS product has no EU data residency option as of 2025.

**Decision rule:** If your users are EU residents, if you handle special categories of personal data (Art. 9 GDPR), or if your legal team has not signed a LangSmith DPA + SCCs, use a self-hosted alternative below.

---

### LANGSMITH_HIDE_INPUTS / LANGSMITH_HIDE_OUTPUTS: Partial Solution

LangSmith provides two environment variables that suppress sending I/O to their servers:

```bash
LANGSMITH_HIDE_INPUTS=true    # inputs are replaced with {"__hidden": true}
LANGSMITH_HIDE_OUTPUTS=true   # outputs are replaced with {"__hidden": true}
```

**What this does:**
- Prevents raw prompts and responses from leaving your infrastructure.
- Traces still appear in LangSmith with all structural metadata: run names, latency, token counts, cost estimates, tags, metadata fields, error messages.
- You keep dashboard functionality for cost, latency, and error monitoring.

**What this does NOT do:**
- Metadata fields you attach via `config={"metadata": {...}}` are still sent. If you store PII in metadata (e.g., `user_id` that maps to a natural person), that is still transmitted.
- Error messages may contain fragments of the input (e.g., "Input too long: 'John Smith asked...'"). Stack traces are still sent.
- Token counts are still sent; a sufficiently short token count may allow inference of the content.
- It does not make LangSmith GDPR-compliant — it reduces exposure but does not eliminate data transfer to the US.

**Verdict:** `LANGSMITH_HIDE_INPUTS/OUTPUTS` is appropriate for reducing data leakage in non-EU deployments or for low-sensitivity internal tools. It is not a substitute for self-hosted infrastructure when data residency is legally required.

---

### Option A — Langfuse (Recommended for GDPR)

Langfuse is an open-source LLM observability platform with a LangChain `CallbackHandler` that is a near drop-in replacement for LangSmith tracing.

**Install:**

```bash
pip install langfuse
```

**Docker Compose — Full Self-Hosted Stack:**

```yaml
# docker-compose.yml
version: "3.8"

services:
  langfuse-server:
    image: langfuse/langfuse:latest
    ports:
      - "3000:3000"
    environment:
      DATABASE_URL: postgresql://langfuse:langfuse@postgres:5432/langfuse
      NEXTAUTH_URL: http://localhost:3000
      NEXTAUTH_SECRET: "replace-with-a-long-random-secret-32chars+"
      SALT: "replace-with-a-different-long-random-secret"
      LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES: "false"
      # Optional: restrict sign-ups to your org
      # AUTH_DISABLE_SIGNUP: "true"
    depends_on:
      postgres:
        condition: service_healthy

  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: langfuse
      POSTGRES_DB: langfuse
    volumes:
      - langfuse_postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse"]
      interval: 5s
      timeout: 5s
      retries: 10

  # Optional: ClickHouse for analytics at scale (>1M traces/month)
  clickhouse:
    image: clickhouse/clickhouse-server:23.8
    ports:
      - "8123:8123"
    volumes:
      - langfuse_clickhouse_data:/var/lib/clickhouse

volumes:
  langfuse_postgres_data:
  langfuse_clickhouse_data:
```

```bash
# Start the stack
docker compose up -d

# Langfuse UI is at http://localhost:3000
# Create an account, then create a project to get your API keys
```

**LangChain Integration:**

```python
import os
from langfuse.callback import CallbackHandler
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate

# Point to your self-hosted Langfuse instance
os.environ["LANGFUSE_HOST"] = "http://localhost:3000"   # or your server's hostname
os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-..."         # from Langfuse UI → Settings
os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-..."         # from Langfuse UI → Settings

# Create the callback handler — this is the only change from LangSmith
langfuse_handler = CallbackHandler(
    trace_name="my-pipeline",
    user_id="alice",                     # optional: per-request user tracking
    session_id="session-xyz",            # optional: conversation grouping
    tags=["production", "v2"],           # optional: filterable labels
    metadata={"feature": "doc-summary"}, # optional: arbitrary key-value
)

# Use exactly like LangSmith tracing — pass as a callback
llm = ChatAnthropic(model="claude-sonnet-4-6")
prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful assistant."),
    ("human", "{question}"),
])
chain = prompt | llm

result = chain.invoke(
    {"question": "What is GDPR?"},
    config={"callbacks": [langfuse_handler]},
)
print(result.content)
# Open http://localhost:3000 → Traces → you should see the run
```

**Per-Request Handler (Production Pattern):**

```python
from langfuse.callback import CallbackHandler

def build_handler(user_id: str, session_id: str, trace_name: str) -> CallbackHandler:
    """Create a fresh handler per request — avoids state bleed between requests."""
    return CallbackHandler(
        trace_name=trace_name,
        user_id=user_id,
        session_id=session_id,
        tags=["production"],
        metadata={"user_id": user_id},
        # Langfuse flushes async; call handler.flush() if you need sync confirmation
    )

async def handle_chat(user_id: str, session_id: str, message: str) -> str:
    handler = build_handler(user_id, session_id, "ChatEndpoint")
    llm = ChatAnthropic(model="claude-sonnet-4-6")
    result = await llm.ainvoke(message, config={"callbacks": [handler]})
    await handler.flush_async()   # ensure trace is written before response returns
    return result.content
```

**Scoring / Feedback (equivalent to LangSmith `create_feedback`):**

```python
from langfuse import Langfuse

langfuse = Langfuse(
    host="http://localhost:3000",
    public_key="pk-lf-...",
    secret_key="sk-lf-...",
)

def record_user_feedback(trace_id: str, thumbs_up: bool):
    """Post user feedback back to the trace."""
    langfuse.score(
        trace_id=trace_id,
        name="user_feedback",
        value=1.0 if thumbs_up else 0.0,
        comment="User rated the response",
    )
```

---

### Option B — Arize Phoenix (Local, Zero-Config)

Arize Phoenix runs as a local server with zero external dependencies. It uses OpenTelemetry (OTEL) under the hood and supports LangChain auto-instrumentation.

**Install:**

```bash
pip install arize-phoenix openinference-instrumentation-langchain
```

**Start the Phoenix server (runs in-process or as a separate server):**

```python
# option 1: in-process server (development / notebooks)
import phoenix as px

session = px.launch_app()   # opens browser at http://localhost:6006
print(session.url)          # http://localhost:6006
```

```bash
# option 2: standalone server (production)
python -m phoenix.server.main serve
# → listening at http://localhost:6006
```

**LangChain Auto-Instrumentation:**

```python
from openinference.instrumentation.langchain import LangChainInstrumentor
from phoenix.otel import register

# Register Phoenix as the OTEL endpoint
tracer_provider = register(
    project_name="my-langchain-app",
    endpoint="http://localhost:6006/v1/traces",  # Phoenix OTEL endpoint
)

# Instrument LangChain — this patches all LangChain internals automatically
LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

# Now ALL LangChain calls are traced with zero further code changes
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate

llm = ChatAnthropic(model="claude-sonnet-4-6")
prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful assistant."),
    ("human", "{question}"),
])
chain = prompt | llm

result = chain.invoke({"question": "What is the capital of France?"})
# Open http://localhost:6006 → Traces tab → see the full span tree
```

**Adding Custom Spans (OTEL-native):**

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

def my_preprocessing(text: str) -> str:
    with tracer.start_as_current_span("Preprocessing") as span:
        span.set_attribute("input.length", len(text))
        result = text.strip().lower()
        span.set_attribute("output.length", len(result))
        return result
```

**Docker for Phoenix (persistent storage):**

```bash
docker run -p 6006:6006 -v phoenix_data:/phoenix arizephoenix/phoenix:latest
```

---

### Option C — OpenTelemetry to Grafana Tempo

If you already operate a Grafana stack (common in enterprise), route LangChain traces directly to Grafana Tempo via OTLP. No new services required.

**Install:**

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp openinference-instrumentation-langchain
```

**Configure OTLP exporter pointing to Tempo:**

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from openinference.instrumentation.langchain import LangChainInstrumentor

# Define service resource (shows in Grafana Explore → Service dropdown)
resource = Resource.create({
    "service.name": "my-langchain-app",
    "service.version": "1.2.0",
    "deployment.environment": "production",
})

# Create provider with OTLP exporter → Grafana Tempo
tracer_provider = TracerProvider(resource=resource)
otlp_exporter = OTLPSpanExporter(
    endpoint="http://tempo:4317",    # Tempo OTLP gRPC endpoint (adjust host)
    # For TLS: credentials=grpc.ssl_channel_credentials(...)
)
tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
trace.set_tracer_provider(tracer_provider)

# Instrument LangChain
LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

# All LangChain calls now emit OTEL spans to Tempo
```

**Grafana Tempo — minimal docker-compose addition:**

```yaml
# Add to your existing docker-compose.yml
services:
  tempo:
    image: grafana/tempo:latest
    command: ["-config.file=/etc/tempo/tempo.yaml"]
    volumes:
      - ./tempo.yaml:/etc/tempo/tempo.yaml
      - tempo_data:/var/tempo
    ports:
      - "4317:4317"   # OTLP gRPC
      - "3200:3200"   # Tempo query API

  # If you don't already have Grafana:
  grafana:
    image: grafana/grafana:latest
    ports:
      - "3001:3000"
    environment:
      GF_SECURITY_ADMIN_PASSWORD: admin
    volumes:
      - grafana_data:/var/lib/grafana

volumes:
  tempo_data:
  grafana_data:
```

```yaml
# tempo.yaml — minimal config
server:
  http_listen_port: 3200

distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: "0.0.0.0:4317"

storage:
  trace:
    backend: local
    local:
      path: /var/tempo/traces
    wal:
      path: /var/tempo/wal
```

**Grafana datasource setup:**
```
Grafana UI → Connections → Data Sources → Add → Tempo
URL: http://tempo:3200
Save & Test
```

**Querying LangChain traces in Grafana:**
```
Explore → Tempo → Service Name: my-langchain-app
TraceQL: { .langchain.run_type = "llm" } | select(duration)
```

---

### Feature Parity Comparison Table

| Feature | LangSmith (SaaS) | Langfuse (self-hosted) | Arize Phoenix | OTEL → Grafana Tempo |
|---|---|---|---|---|
| Auto LangChain tracing | Yes | Yes | Yes | Yes |
| Trace tree UI | Yes | Yes | Yes | Partial (Grafana Explore) |
| Evaluation datasets | Yes | Yes | Yes | No |
| LLM-as-judge evals | Yes | Yes | Yes | No |
| Online evaluator rules | Yes | Yes | No | No |
| Cost tracking | Yes | Yes (model config) | Yes | No (custom spans) |
| Token counting | Yes | Yes | Yes | Attributes only |
| Prompt Hub | Yes | Yes (prompt mgmt) | No | No |
| User feedback API | Yes | Yes | No | No |
| Self-hostable | Enterprise only | Yes, free OSS | Yes, free OSS | Yes (your infra) |
| EU data residency | No (US servers) | Yes | Yes | Yes |
| GDPR-suitable | No (without DPA) | Yes | Yes | Yes |
| Price | $0 dev / usage prod | Free OSS / cloud paid | Free OSS | Infra cost only |
| Persistent storage | LangSmith cloud | PostgreSQL | SQLite / S3 | Tempo backend |
| Existing infra fit | Low | Medium | Low | High (Grafana shops) |

---

### Migration Path: LangSmith → Langfuse

The LangSmith `CallbackHandler` and Langfuse `CallbackHandler` share the same LangChain `BaseCallbackHandler` interface. Migration is a three-step find-and-replace:

**Step 1 — Swap the import:**

```python
# Before (LangSmith)
from langsmith.run_helpers import LangSmithTracer   # or auto-tracing via env var

# After (Langfuse)
from langfuse.callback import CallbackHandler as LangfuseHandler
```

**Step 2 — Swap environment variables:**

```bash
# Remove / disable
LANGSMITH_API_KEY=...
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=...
LANGSMITH_ENDPOINT=...

# Add
LANGFUSE_HOST=http://your-langfuse-server:3000
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

**Step 3 — Replace callback instantiation at call sites:**

```python
# Before: LangSmith auto-tracing (env var approach)
# No explicit callback needed — just LANGSMITH_TRACING=true

# After: Langfuse explicit callback
from langfuse.callback import CallbackHandler

handler = CallbackHandler(
    trace_name="MyChain",
    user_id=user_id,
    session_id=session_id,
    tags=tags,
    metadata=metadata,
)

result = chain.invoke(inputs, config={"callbacks": [handler]})
```

**Migrating `create_feedback` calls:**

```python
# Before (LangSmith)
from langsmith import Client
client = Client()
client.create_feedback(run_id=run_id, key="quality", score=0.9)

# After (Langfuse)
from langfuse import Langfuse
langfuse = Langfuse(host="http://...", public_key="...", secret_key="...")
langfuse.score(trace_id=trace_id, name="quality", value=0.9)
# Note: LangSmith run_id == Langfuse trace_id — both are UUIDs you extract
# from the handler after the run completes:
trace_id = handler.get_trace_id()
```

**Migrating datasets and evals:**

```python
# LangSmith datasets have no direct Langfuse import tool.
# Export from LangSmith: UI → Dataset → Export → JSON
# Import to Langfuse:
from langfuse import Langfuse
import json

langfuse = Langfuse(host="http://...", public_key="...", secret_key="...")
dataset = langfuse.create_dataset(name="qa-golden-set")

with open("langsmith_export.json") as f:
    examples = json.load(f)

for ex in examples:
    langfuse.create_dataset_item(
        dataset_name="qa-golden-set",
        input=ex["inputs"],
        expected_output=ex["outputs"],
    )
```

---

### Complete Langfuse Self-Hosted Docker Compose + Integration

This is a production-ready configuration combining all pieces above.

**docker-compose.yml:**

```yaml
version: "3.8"

services:
  langfuse-server:
    image: langfuse/langfuse:latest
    restart: unless-stopped
    ports:
      - "3000:3000"
    environment:
      DATABASE_URL: postgresql://langfuse:${POSTGRES_PASSWORD}@postgres:5432/langfuse
      NEXTAUTH_URL: ${LANGFUSE_EXTERNAL_URL:-http://localhost:3000}
      NEXTAUTH_SECRET: ${NEXTAUTH_SECRET}
      SALT: ${LANGFUSE_SALT}
      # Security
      AUTH_DISABLE_SIGNUP: "false"   # set "true" after creating your org
      # Performance
      LANGFUSE_ASYNC_INGESTION: "true"
      # Optional S3 for large trace storage
      # LANGFUSE_S3_MEDIA_UPLOAD_ENABLED: "true"
      # LANGFUSE_S3_BUCKET_NAME: my-langfuse-bucket
      # AWS_ACCESS_KEY_ID: ...
      # AWS_SECRET_ACCESS_KEY: ...
      # AWS_REGION: eu-west-1
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:3000/api/public/health"]
      interval: 30s
      timeout: 10s
      retries: 5

  postgres:
    image: postgres:15-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: langfuse
    volumes:
      - langfuse_postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse"]
      interval: 5s
      timeout: 5s
      retries: 10

  # Reverse proxy with TLS — replace with your cert paths or use Caddy
  nginx:
    image: nginx:alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro   # certbot certs
    depends_on:
      - langfuse-server

volumes:
  langfuse_postgres_data:
```

**.env (do not commit):**

```bash
POSTGRES_PASSWORD=change_me_strong_password
NEXTAUTH_SECRET=change_me_32char_random_string_here
LANGFUSE_SALT=change_me_different_32char_random_string
LANGFUSE_EXTERNAL_URL=https://langfuse.yourdomain.com
```

**Python integration module (`observability.py`):**

```python
"""
observability.py — drop-in Langfuse integration for self-hosted deployment.
Replace LangSmith environment variables and callbacks with this module.
"""

import os
from functools import lru_cache
from typing import Optional
from langfuse.callback import CallbackHandler
from langfuse import Langfuse


@lru_cache(maxsize=1)
def _langfuse_client() -> Langfuse:
    """Singleton Langfuse client — reuses HTTP connection pool."""
    return Langfuse(
        host=os.environ["LANGFUSE_HOST"],
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    )


def make_tracer(
    trace_name: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    tags: Optional[list] = None,
    metadata: Optional[dict] = None,
) -> CallbackHandler:
    """
    Create a per-request Langfuse callback handler.

    Usage:
        handler = make_tracer("ChatEndpoint", user_id="alice", session_id="s1")
        result = chain.invoke(inputs, config={"callbacks": [handler]})
        trace_id = handler.get_trace_id()
    """
    return CallbackHandler(
        host=os.environ["LANGFUSE_HOST"],
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        trace_name=trace_name,
        user_id=user_id,
        session_id=session_id,
        tags=tags or [],
        metadata=metadata or {},
    )


def record_feedback(trace_id: str, name: str, value: float, comment: str = ""):
    """Post a score to a completed trace — equivalent to LangSmith create_feedback."""
    _langfuse_client().score(
        trace_id=trace_id,
        name=name,
        value=value,
        comment=comment,
    )


def create_dataset(name: str, description: str = "") -> object:
    """Create a Langfuse dataset — equivalent to LangSmith create_dataset."""
    return _langfuse_client().create_dataset(name=name, description=description)


def add_dataset_example(dataset_name: str, inputs: dict, expected_output: dict):
    """Add one example — equivalent to LangSmith create_examples."""
    _langfuse_client().create_dataset_item(
        dataset_name=dataset_name,
        input=inputs,
        expected_output=expected_output,
    )
```

---

## 11. PII Masking Tracer

### Overview

`PIIMaskingTracer` is a custom LangChain callback handler that intercepts all LLM inputs and outputs before they leave your process, redacts PII using Microsoft Presidio, and then forwards the sanitized data to LangSmith (or any other tracer). This allows teams that use LangSmith SaaS to comply with internal PII policies even though they cannot achieve full GDPR data residency.

**Architecture:**
```
LangChain call
    → PIIMaskingTracer.on_llm_start(prompts)
        → Presidio AnalyzerEngine (async)
        → Presidio AnonymizerEngine (redact/replace)
        → sanitized prompts → LangSmithTracer.on_llm_start(sanitized)
    → LLM call (original prompt — masking is tracer-only, not model input)
    → PIIMaskingTracer.on_llm_end(response)
        → Presidio (redact response)
        → sanitized response → LangSmithTracer.on_llm_end(sanitized)
```

Note: The LLM itself still receives the original prompt. Masking is applied only to the data sent to the observability backend. To mask data sent to the LLM, apply Presidio before building the prompt.

### Install

```bash
pip install \
    presidio-analyzer \
    presidio-anonymizer \
    spacy \
    langsmith

# Download the spaCy English model required by Presidio's NLP engine
python -m spacy download en_core_web_lg

# Optional: for multilingual PII detection (EU languages)
pip install presidio-analyzer[transformers]
python -m spacy download de_core_news_lg   # German
python -m spacy download fr_core_news_lg   # French
python -m spacy download es_core_news_lg   # Spanish
```

### Complete PIIMaskingTracer Implementation

```python
"""
pii_masking_tracer.py

A LangChain CallbackHandler that redacts PII from LLM inputs/outputs
before forwarding traces to LangSmith.

Environment variables:
    PII_ENTITIES        Comma-separated Presidio entity types to redact.
                        Default: PERSON,EMAIL_ADDRESS,PHONE_NUMBER,
                                 CREDIT_CARD,US_SSN,IBAN_CODE,
                                 LOCATION,DATE_TIME,NRP
    PII_REPLACEMENT     Replacement strategy: "replace" (default) | "mask" | "redact" | "hash"
    PII_SCORE_THRESHOLD Minimum Presidio confidence score (0.0-1.0). Default: 0.5
    PII_LANGUAGE        Language for NLP analysis. Default: "en"
    LANGSMITH_API_KEY   Standard LangSmith env var
    LANGSMITH_PROJECT   Standard LangSmith env var
"""

from __future__ import annotations

import asyncio
import os
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Union
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult
from langsmith.run_helpers import LangSmithTracer

from presidio_analyzer import AnalyzerEngine, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default PII entities — covers the most common GDPR-relevant categories
# Override via PII_ENTITIES env var
# ---------------------------------------------------------------------------
DEFAULT_PII_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "US_SSN",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "IBAN_CODE",
    "LOCATION",
    "DATE_TIME",
    "NRP",              # National/Religious/Political group
    "MEDICAL_LICENSE",
    "IP_ADDRESS",
    "URL",              # URLs can contain user IDs, session tokens
]


def _build_analyzer(language: str = "en") -> AnalyzerEngine:
    """Build a Presidio AnalyzerEngine with the spaCy NLP backend."""
    configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": language, "model_name": f"{language}_core_web_lg"}],
    }
    provider = NlpEngineProvider(nlp_configuration=configuration)
    nlp_engine = provider.create_engine()
    return AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=[language])


def _build_anonymizer() -> AnonymizerEngine:
    return AnonymizerEngine()


def _get_operator_config(strategy: str) -> Dict[str, OperatorConfig]:
    """
    Build Presidio operator config from strategy name.

    - replace: replaces with <ENTITY_TYPE> placeholder (default, most readable)
    - mask:    replaces with * characters
    - redact:  removes the text entirely
    - hash:    replaces with SHA-256 hash (reversible lookup possible if original known)
    """
    strategy = strategy.lower()
    if strategy == "replace":
        return {"DEFAULT": OperatorConfig("replace", {"new_value": "<{entity_type}>"})}
    elif strategy == "mask":
        return {"DEFAULT": OperatorConfig("mask", {"masking_char": "*", "chars_to_mask": 100, "from_end": False})}
    elif strategy == "redact":
        return {"DEFAULT": OperatorConfig("redact", {})}
    elif strategy == "hash":
        return {"DEFAULT": OperatorConfig("hash", {"hash_type": "sha256"})}
    else:
        raise ValueError(f"Unknown PII_REPLACEMENT strategy: {strategy!r}. Use: replace|mask|redact|hash")


class PIIMaskingTracer(BaseCallbackHandler):
    """
    LangChain CallbackHandler that masks PII before forwarding to LangSmith.

    Usage:
        tracer = PIIMaskingTracer()
        result = chain.invoke(inputs, config={"callbacks": [tracer]})

    Environment variables control all behaviour — no code changes needed
    to adjust which entities are redacted or at what confidence threshold.
    """

    def __init__(
        self,
        entities: Optional[List[str]] = None,
        replacement: str = "replace",
        score_threshold: float = 0.5,
        language: str = "en",
        langsmith_tracer: Optional[LangSmithTracer] = None,
        max_workers: int = 4,
    ):
        super().__init__()

        # Configuration — env vars take precedence over constructor args
        raw_entities = os.environ.get("PII_ENTITIES", "")
        self.entities: List[str] = (
            [e.strip() for e in raw_entities.split(",") if e.strip()]
            if raw_entities
            else (entities or DEFAULT_PII_ENTITIES)
        )
        self.replacement: str = os.environ.get("PII_REPLACEMENT", replacement)
        self.score_threshold: float = float(
            os.environ.get("PII_SCORE_THRESHOLD", score_threshold)
        )
        self.language: str = os.environ.get("PII_LANGUAGE", language)

        # Presidio engines — built once, reused across all calls
        logger.info("Initialising Presidio AnalyzerEngine (loading spaCy model)...")
        self._analyzer = _build_analyzer(self.language)
        self._anonymizer = _build_anonymizer()
        self._operator_config = _get_operator_config(self.replacement)
        logger.info("Presidio ready. Entities: %s", self.entities)

        # Thread pool for async Presidio calls (Presidio is CPU-bound, not async-native)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="presidio-worker",
        )

        # Downstream tracer — if None, only masking is done (no forwarding)
        self._langsmith_tracer: Optional[LangSmithTracer] = langsmith_tracer

    # ------------------------------------------------------------------
    # Core masking logic
    # ------------------------------------------------------------------

    def _redact_text(self, text: str) -> str:
        """Synchronous redaction — runs inside the thread pool."""
        if not text or not text.strip():
            return text
        try:
            results: List[RecognizerResult] = self._analyzer.analyze(
                text=text,
                entities=self.entities,
                language=self.language,
                score_threshold=self.score_threshold,
            )
            if not results:
                return text
            anonymized = self._anonymizer.anonymize(
                text=text,
                analyzer_results=results,
                operators=self._operator_config,
            )
            return anonymized.text
        except Exception:
            logger.exception("Presidio redaction failed — returning original text")
            return text

    async def _aredact_text(self, text: str) -> str:
        """Async wrapper — offloads CPU work to thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self._redact_text, text)

    async def _aredact_messages(
        self, messages: Sequence[Union[BaseMessage, List[BaseMessage]]]
    ) -> List[List[BaseMessage]]:
        """Redact content in a nested message list (the shape on_llm_start receives)."""
        redacted_outer = []
        for batch in messages:
            if isinstance(batch, list):
                redacted_inner = []
                for msg in batch:
                    if hasattr(msg, "content") and isinstance(msg.content, str):
                        new_content = await self._aredact_text(msg.content)
                        # Create a copy of the message with sanitized content
                        redacted_msg = msg.copy(update={"content": new_content})
                        redacted_inner.append(redacted_msg)
                    else:
                        redacted_inner.append(msg)
                redacted_outer.append(redacted_inner)
            else:
                # Flat string prompt (non-chat models)
                redacted_outer.append(batch)
        return redacted_outer

    async def _aredact_prompts(self, prompts: List[str]) -> List[str]:
        """Redact a flat list of string prompts (completion models)."""
        tasks = [self._aredact_text(p) for p in prompts]
        return list(await asyncio.gather(*tasks))

    # ------------------------------------------------------------------
    # LangChain callback hooks
    # ------------------------------------------------------------------

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Intercept completion-model calls — redact prompt strings."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context — schedule and wait
                future = asyncio.ensure_future(self._aredact_prompts(prompts))
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    sanitized_prompts = ex.submit(
                        asyncio.run, self._aredact_prompts(prompts)
                    ).result()
            else:
                sanitized_prompts = loop.run_until_complete(
                    self._aredact_prompts(prompts)
                )
        except RuntimeError:
            # No event loop — run synchronously
            sanitized_prompts = [self._redact_text(p) for p in prompts]

        if self._langsmith_tracer:
            self._langsmith_tracer.on_llm_start(
                serialized,
                sanitized_prompts,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=metadata,
                **kwargs,
            )

    async def on_llm_start_async(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Async version — preferred code path for async chains."""
        sanitized_prompts = await self._aredact_prompts(prompts)
        if self._langsmith_tracer and hasattr(self._langsmith_tracer, "on_llm_start_async"):
            await self._langsmith_tracer.on_llm_start_async(
                serialized,
                sanitized_prompts,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=metadata,
                **kwargs,
            )
        elif self._langsmith_tracer:
            self._langsmith_tracer.on_llm_start(
                serialized,
                sanitized_prompts,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=metadata,
                **kwargs,
            )

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Intercept chat-model calls — redact message content."""
        try:
            loop = asyncio.get_event_loop()
            sanitized = loop.run_until_complete(self._aredact_messages(messages))
        except RuntimeError:
            sanitized = messages   # fallback: no redaction rather than crash
            logger.warning("PIIMaskingTracer: could not redact chat messages (no event loop)")

        if self._langsmith_tracer:
            self._langsmith_tracer.on_chat_model_start(
                serialized,
                sanitized,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=metadata,
                **kwargs,
            )

    async def on_chat_model_start_async(
        self,
        serialized: Dict[str, Any],
        messages: List[List[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Async version — preferred for async chains."""
        sanitized = await self._aredact_messages(messages)
        if self._langsmith_tracer:
            if hasattr(self._langsmith_tracer, "on_chat_model_start_async"):
                await self._langsmith_tracer.on_chat_model_start_async(
                    serialized,
                    sanitized,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    tags=tags,
                    metadata=metadata,
                    **kwargs,
                )
            else:
                self._langsmith_tracer.on_chat_model_start(
                    serialized,
                    sanitized,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    tags=tags,
                    metadata=metadata,
                    **kwargs,
                )

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Intercept LLM response — redact generated text."""
        sanitized_response = self._sanitize_llm_result(response)
        if self._langsmith_tracer:
            self._langsmith_tracer.on_llm_end(
                sanitized_response,
                run_id=run_id,
                parent_run_id=parent_run_id,
                **kwargs,
            )

    async def on_llm_end_async(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Async version."""
        sanitized_response = await self._asanitize_llm_result(response)
        if self._langsmith_tracer:
            if hasattr(self._langsmith_tracer, "on_llm_end_async"):
                await self._langsmith_tracer.on_llm_end_async(
                    sanitized_response,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    **kwargs,
                )
            else:
                self._langsmith_tracer.on_llm_end(
                    sanitized_response,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    **kwargs,
                )

    def on_llm_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Forward errors — redact error message text."""
        # Error messages may contain PII (e.g., "Input contained 'John Smith'...")
        if isinstance(error, Exception):
            sanitized_msg = self._redact_text(str(error))
            # Reconstruct exception with sanitized message
            sanitized_error = type(error)(sanitized_msg)
        else:
            sanitized_error = error

        if self._langsmith_tracer:
            self._langsmith_tracer.on_llm_error(
                sanitized_error,
                run_id=run_id,
                parent_run_id=parent_run_id,
                **kwargs,
            )

    # ------------------------------------------------------------------
    # Helper: sanitize LLMResult
    # ------------------------------------------------------------------

    def _sanitize_llm_result(self, response: LLMResult) -> LLMResult:
        """Synchronously redact all generation texts in an LLMResult."""
        import copy
        from langchain_core.outputs import Generation, ChatGeneration

        sanitized = copy.deepcopy(response)
        for gen_list in sanitized.generations:
            for gen in gen_list:
                if isinstance(gen, ChatGeneration):
                    if hasattr(gen.message, "content") and isinstance(gen.message.content, str):
                        gen.message.content = self._redact_text(gen.message.content)
                elif isinstance(gen, Generation):
                    if gen.text:
                        gen.text = self._redact_text(gen.text)
        return sanitized

    async def _asanitize_llm_result(self, response: LLMResult) -> LLMResult:
        """Asynchronously redact all generation texts in an LLMResult."""
        import copy
        from langchain_core.outputs import Generation, ChatGeneration

        sanitized = copy.deepcopy(response)
        tasks = []
        targets = []   # (gen, "text" | "content") tuples

        for gen_list in sanitized.generations:
            for gen in gen_list:
                if isinstance(gen, ChatGeneration):
                    if hasattr(gen.message, "content") and isinstance(gen.message.content, str):
                        tasks.append(self._aredact_text(gen.message.content))
                        targets.append((gen, "chat"))
                elif isinstance(gen, Generation):
                    if gen.text:
                        tasks.append(self._aredact_text(gen.text))
                        targets.append((gen, "completion"))

        if tasks:
            results = await asyncio.gather(*tasks)
            for (gen, kind), sanitized_text in zip(targets, results):
                if kind == "chat":
                    gen.message.content = sanitized_text
                else:
                    gen.text = sanitized_text

        return sanitized

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def __del__(self):
        self._executor.shutdown(wait=False)
```

---

### Usage Examples

**Basic usage — drop into any chain:**

```python
import os
from pii_masking_tracer import PIIMaskingTracer
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate

os.environ["LANGSMITH_API_KEY"] = "ls__..."
os.environ["LANGSMITH_TRACING"] = "true"
os.environ["LANGSMITH_PROJECT"] = "my-project"

# PIIMaskingTracer sits between LangChain and LangSmith
tracer = PIIMaskingTracer()

llm = ChatAnthropic(model="claude-sonnet-4-6")
prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a support agent."),
    ("human", "{message}"),
])
chain = prompt | llm

# User message contains PII — will be redacted before reaching LangSmith
result = chain.invoke(
    {"message": "Hi, I'm John Smith. My email is john@example.com and my SSN is 123-45-6789."},
    config={"callbacks": [tracer]},
)
# LangSmith trace shows:
# "Hi, I'm <PERSON>. My email is <EMAIL_ADDRESS> and my SSN is <US_SSN>."
print(result.content)
```

**Environment-variable-driven configuration:**

```bash
# .env — control which entities to redact without code changes
PII_ENTITIES=PERSON,EMAIL_ADDRESS,PHONE_NUMBER,CREDIT_CARD
PII_REPLACEMENT=replace       # replace | mask | redact | hash
PII_SCORE_THRESHOLD=0.7       # higher = fewer false positives
PII_LANGUAGE=en
```

**Async chain usage:**

```python
import asyncio
from pii_masking_tracer import PIIMaskingTracer
from langchain_anthropic import ChatAnthropic

tracer = PIIMaskingTracer()
llm = ChatAnthropic(model="claude-sonnet-4-6")

async def handle_message(user_message: str) -> str:
    result = await llm.ainvoke(
        user_message,
        config={"callbacks": [tracer]},
    )
    return result.content

asyncio.run(handle_message("My name is Alice, call me at 555-0100."))
```

**With Langfuse self-hosted (no LangSmith, full self-hosted stack):**

```python
from pii_masking_tracer import PIIMaskingTracer
from langfuse.callback import CallbackHandler as LangfuseHandler
from langchain_anthropic import ChatAnthropic

# Use Langfuse instead of LangSmith as the downstream
langfuse_handler = LangfuseHandler(
    host="http://langfuse.internal:3000",
    public_key="pk-lf-...",
    secret_key="sk-lf-...",
    trace_name="SupportChat",
)

# PIIMaskingTracer + Langfuse: fully self-hosted, GDPR-compliant, PII-masked
tracer = PIIMaskingTracer(langsmith_tracer=None)   # no LangSmith

llm = ChatAnthropic(model="claude-sonnet-4-6")

result = llm.invoke(
    "Hi, I'm Jane Doe, email jane@corp.com. What are my options?",
    config={"callbacks": [tracer, langfuse_handler]},
)
# Langfuse receives: "Hi, I'm <PERSON>, email <EMAIL_ADDRESS>. What are my options?"
```

**Multilingual PII masking (EU deployments):**

```python
import os
from pii_masking_tracer import PIIMaskingTracer

# German PII masking
os.environ["PII_LANGUAGE"] = "de"   # requires de_core_news_lg
os.environ["PII_ENTITIES"] = "PERSON,EMAIL_ADDRESS,PHONE_NUMBER,LOCATION,DATE_TIME"

tracer_de = PIIMaskingTracer()

# For multi-language apps: create one tracer per language, route by detected language
```

---

### Performance Considerations

| Concern | Details |
|---|---|
| Presidio startup latency | spaCy model loads once at `PIIMaskingTracer()` construction (~2-5s). Construct at app startup, not per-request. |
| Per-call latency overhead | ~5-50ms per 500-token message on CPU (mostly spaCy NER). Use `max_workers=4` (default) for async parallelism. |
| GPU acceleration | Presidio supports transformer-based recognizers (HuggingFace). Set `PII_LANGUAGE=en` + `nlp_engine_name=transformers` for higher accuracy at 3-5x latency cost. |
| High-throughput | For >100 RPS, run Presidio as a separate microservice (presidio-analyzer Docker image) and call it via HTTP. |
| False positives | Raise `PII_SCORE_THRESHOLD` (e.g., 0.85) to reduce over-redaction. Monitor redaction rate in your observability backend. |
| spaCy model size | `en_core_web_lg` (~800 MB RAM). For constrained environments, use `en_core_web_sm` (~50 MB) — lower NER accuracy. |

**Presidio as a microservice (high-throughput alternative):**

```python
"""
presidio_client.py — HTTP client for Presidio microservice deployment.
Replaces in-process Presidio engine for high-throughput scenarios.
"""

import aiohttp
from typing import List

PRESIDIO_ANALYZER_URL = "http://presidio-analyzer:3000/analyze"
PRESIDIO_ANONYMIZER_URL = "http://presidio-anonymizer:3000/anonymize"

async def redact_via_service(
    text: str,
    entities: List[str],
    language: str = "en",
    score_threshold: float = 0.5,
) -> str:
    """Call Presidio microservices over HTTP — suitable for >100 RPS."""
    async with aiohttp.ClientSession() as session:
        # Step 1: analyze
        analyze_payload = {
            "text": text,
            "language": language,
            "entities": entities,
            "score_threshold": score_threshold,
        }
        async with session.post(PRESIDIO_ANALYZER_URL, json=analyze_payload) as resp:
            analysis_results = await resp.json()

        if not analysis_results:
            return text

        # Step 2: anonymize
        anonymize_payload = {
            "text": text,
            "anonymizers": {"DEFAULT": {"type": "replace", "new_value": "<{entity_type}>"}},
            "analyzer_results": analysis_results,
        }
        async with session.post(PRESIDIO_ANONYMIZER_URL, json=anonymize_payload) as resp:
            result = await resp.json()
            return result["text"]
```

---

### Common Mistakes: Sections 10–11

| Mistake | Fix |
|---|---|
| Relying on `LANGSMITH_HIDE_INPUTS=true` as a GDPR solution | This only hides I/O content; metadata, token counts, error messages still leave your infra. Use a self-hosted alternative. |
| Constructing `PIIMaskingTracer()` inside a request handler | The spaCy model loads on construction (~2-5s). Construct once at app startup and reuse. |
| Not setting `PII_SCORE_THRESHOLD` — default 0.5 gives false positives | Start at 0.7 and tune down if you're missing real PII. Monitor redaction rate. |
| Langfuse Docker without persistent volumes | Without a named volume on the Postgres data dir, all traces are lost on container restart. |
| Forgetting `handler.flush_async()` in async FastAPI routes | Langfuse buffers writes. Without flush, traces may be dropped when the process exits. |
| Assuming `PIIMaskingTracer` protects model inputs | It only masks data sent to the observability backend. The LLM still receives the original prompt. |
| Running in-process Presidio at >100 RPS | Use the Presidio Docker microservice and `presidio_client.py` HTTP approach instead. |
| Using `en_core_web_sm` in production | The small model has significantly lower NER accuracy for names and locations. Use `en_core_web_lg` or a transformer model. |
