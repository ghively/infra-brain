---
name: lc-providers
description: Use when the user needs to connect to a non-default LLM provider (Azure OpenAI, AWS Bedrock, Google Gemini, Ollama), swap providers at runtime, add provider fallback for resilience, or understand which provider to choose for their use case. Also use when a user says "I don't have an Anthropic key", "we use Azure at work", "I need this to run offline", or "what if the API goes down".
---

# lc:providers — LLM Provider Ecosystem Guide

## Overview

The plugin defaults to `claude-sonnet-4-6` via Anthropic's API. That covers
most cases — but enterprise teams use Azure OpenAI, AWS Bedrock, or Google
Gemini for policy or cost reasons. Developers without API keys need Ollama
locally. Production systems need fallback chains so one provider going down
does not take the whole application down.

This skill teaches you to:
1. Configure any major provider correctly (including the auth traps each one
   has)
2. Write a provider-agnostic factory so you can swap providers by changing
   one environment variable
3. Build fallback chains so the LLM tier degrades gracefully under load or
   outage

**Core insight:** Every LangChain chat model exposes the same interface —
`.invoke()`, `.stream()`, `.bind_tools()`, `.with_structured_output()`. Once
you know how to instantiate a provider, the rest of your code does not change.
The factory pattern below exploits this.

---

## Discovery Questions (ask all three before scaffolding)

```
1. Which providers do you need?
   [ ] Anthropic (Claude) — already the default
   [ ] OpenAI (GPT-4o, o1, o3)
   [ ] Azure OpenAI — OpenAI models deployed inside your Azure tenant
   [ ] AWS Bedrock — Claude, Llama, Titan via IAM auth
   [ ] Google Gemini — Gemini 2.0 Flash, 1.5 Pro
   [ ] Ollama — fully local, no API key, runs on your machine
   [ ] Multiple with routing — cost-tiered or fallback-chain

2. Is this for production (managed cloud API) or local development
   (offline / free / private data)?

3. Do you need provider fallback if one goes down or rate-limits?
   (yes → generates a .with_fallbacks() chain)
```

Use the answers to jump to the relevant pattern sections below.

---

## Provider Comparison Table

Use this before picking a provider. Latency and cost are approximate as of
mid-2025 — always check current pricing pages.

| Provider | Best models | Context window | Vision | Tool calling | Streaming | Embeddings | Cost tier | Latency |
|---|---|---|---|---|---|---|---|---|
| Anthropic | claude-sonnet-4-6, Opus 4 | 200k | Yes | Yes | Yes | No (use Voyage) | Mid | Mid |
| OpenAI | GPT-4o, o3 | 128k | Yes | Yes | Yes | Yes (3-small, 3-large) | Mid | Fast |
| Azure OpenAI | Same as OpenAI | Same as OpenAI | Yes | Yes | Yes | Yes | Mid + Azure overhead | Mid |
| AWS Bedrock | Claude, Llama 3, Titan | Varies by model | Yes (Claude) | Yes (Claude) | Yes | Yes (Titan, Cohere) | Variable | Mid |
| Google Gemini | Gemini 2.0 Flash, 1.5 Pro | 1M (1.5 Pro) | Yes | Yes | Yes | Yes (text-embedding-004) | Low-Mid | Fast |
| Ollama | Llama 3.2, Mistral, Phi-3 | 8k-128k | Some (llava) | Limited | Yes | Yes (nomic-embed) | Free | GPU-dependent |

**Decision guide:**
- **Privacy / no external calls:** Ollama
- **Largest context window:** Gemini 1.5 Pro (1M tokens)
- **Best reasoning:** Claude Opus 4, o1/o3
- **Cheapest at scale:** Gemini Flash, GPT-4o-mini, Haiku
- **Corporate Azure tenant required:** AzureChatOpenAI
- **Already on AWS with IAM:** Bedrock
- **Multimodal vision pipeline:** GPT-4o or Claude claude-sonnet-4-6 (both excellent)

---

## Environment Setup

Every provider needs credentials. Use a single `.env` file and load it once at
program startup. Never hardcode credentials.

```bash
# .env — one file for all providers; check in .env.example, not .env

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...

# ── OpenAI ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY=sk-...

# ── Azure OpenAI ──────────────────────────────────────────────────────────────
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
AZURE_OPENAI_API_VERSION=2024-08-01-preview
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o          # the deployment name YOU chose

# ── AWS Bedrock ───────────────────────────────────────────────────────────────
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
# OR use IAM roles / instance profiles — then no key vars needed

# ── Google Gemini (AI Studio) ─────────────────────────────────────────────────
GOOGLE_API_KEY=...
# Vertex AI uses ADC instead — see Pattern 4

# ── Ollama (local — no key needed) ───────────────────────────────────────────
OLLAMA_BASE_URL=http://localhost:11434       # default; only set if non-standard

# ── Factory selector ──────────────────────────────────────────────────────────
LLM_PROVIDER=anthropic                       # anthropic | openai | azure | bedrock | gemini | ollama
LLM_TIER=standard                            # fast | standard | powerful

# ── LangSmith (optional but strongly recommended) ────────────────────────────
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=my-project
```

**Why `load_dotenv()` must come first:** LangChain reads `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, etc. at import time when the client is constructed. If you
import the provider class before calling `load_dotenv()`, the env var will not
be set and you will get an auth error that looks like a missing key — even
though the key is in your `.env` file.

```python
# CORRECT
from dotenv import load_dotenv
load_dotenv()                          # must be before any LangChain imports

from langchain_anthropic import ChatAnthropic  # reads ANTHROPIC_API_KEY here

# WRONG — common mistake
from langchain_anthropic import ChatAnthropic  # ANTHROPIC_API_KEY not set yet
from dotenv import load_dotenv
load_dotenv()                          # too late
```

---

## Pattern 1 — Anthropic (Claude)

The default. Document the correct way to configure it — there are several
common mistakes around model naming.

**Install:**
```bash
pip install langchain-anthropic
```

**Model selection guide:**

| Model ID | Nickname | When to use |
|---|---|---|
| `claude-opus-4-5` | Opus | Most complex reasoning, coding, long documents |
| `claude-sonnet-4-6` | Sonnet | Balanced — good for most production work |
| `claude-haiku-3-5` | Haiku | High-volume classification, routing, cheap tasks |

**Rule:** Always use dated model IDs (e.g., `claude-sonnet-4-6`), never
floating aliases (e.g., `claude-sonnet-latest`). Floating aliases silently
change behaviour when Anthropic releases a new version — your tests pass
today, your production app behaves differently next month.

```python
# patterns/01_anthropic.py
from dotenv import load_dotenv
load_dotenv()  # always first — reads ANTHROPIC_API_KEY from .env

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

# ── Basic setup ───────────────────────────────────────────────────────────────
llm = ChatAnthropic(
    model="claude-sonnet-4-6",   # pinned, dated ID — never use "claude-sonnet-latest"
    max_tokens=4096,             # Anthropic requires you to set this; no default
    temperature=0,               # 0 = deterministic, good for structured tasks
    timeout=60,                  # seconds before raising a timeout error
    max_retries=3,               # built-in exponential back-off on 429/529
)

# ── Simple invoke ─────────────────────────────────────────────────────────────
response = llm.invoke([
    SystemMessage(content="You are a concise assistant."),
    HumanMessage(content="What is LCEL?"),
])
print(response.content)

# ── Streaming — yields tokens as they arrive ───────────────────────────────────
for chunk in llm.stream([HumanMessage(content="Count to five.")]):
    print(chunk.content, end="", flush=True)

# ── Extended thinking — forces deep step-by-step reasoning ───────────────────
# Use for: math, logic puzzles, multi-step planning, debugging complex systems
# Adds latency and tokens; do not use for simple Q&A
thinking_llm = ChatAnthropic(
    model="claude-sonnet-4-6",
    max_tokens=16000,            # thinking uses tokens internally; budget generously
    thinking={"type": "enabled", "budget_tokens": 8000},
)
result = thinking_llm.invoke([HumanMessage(content="Prove that sqrt(2) is irrational.")])
# result.content is the final answer; thinking blocks are in result.additional_kwargs

# ── Vision — pass image URLs or base64 directly ───────────────────────────────
vision_llm = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=1024)
vision_response = vision_llm.invoke([
    HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": "https://example.com/chart.png"}},
        {"type": "text", "text": "Describe the trend in this chart."},
    ])
])
```

**Rate limits and retry setup:** `max_retries=3` in the constructor enables
automatic exponential back-off on HTTP 429 (rate limit) and 529 (overloaded)
errors. For heavier workloads, wrap with `tenacity` or use `.with_fallbacks()`
(see Pattern 7).

---

## Pattern 2 — OpenAI

**Install:**
```bash
pip install langchain-openai
```

**Model selection guide:**

| Model | When to use |
|---|---|
| `gpt-4o` | Conversation, vision, tool calling, most tasks |
| `gpt-4o-mini` | High-volume, cost-sensitive tasks (same API, cheaper) |
| `o1` | Math, scientific reasoning, coding — slow but accurate |
| `o3` | Best reasoning available; expensive |

**o1/o3 vs gpt-4o decision:** The o-series models do internal chain-of-thought
before responding. Use them when accuracy matters more than speed — competition
math, security analysis, complex debugging. Use `gpt-4o` for everything
conversational.

```python
# patterns/02_openai.py
from dotenv import load_dotenv
load_dotenv()

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.messages import HumanMessage

# ── Standard model ────────────────────────────────────────────────────────────
llm = ChatOpenAI(
    model="gpt-4o",
    temperature=0,
    max_tokens=4096,
    max_retries=3,    # same auto-retry as Anthropic
)

# ── Cheap model for high-volume tasks ─────────────────────────────────────────
cheap_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

# ── Reasoning model — note: temperature must be 1 for o1/o3 ──────────────────
reasoning_llm = ChatOpenAI(
    model="o1",
    temperature=1,   # o1 ignores temperature, but API requires it to be 1
    # max_tokens is "max_completion_tokens" for o1 — LangChain handles this
)

response = llm.invoke([HumanMessage(content="Explain LCEL in one sentence.")])
print(response.content)

# ── Embeddings ────────────────────────────────────────────────────────────────
# text-embedding-3-small: 1536 dims, cheap, good quality — use for most RAG
# text-embedding-3-large: 3072 dims, best quality, ~6x more expensive
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

# Test: embed a string, get a list of floats
vector = embeddings.embed_query("What is LangGraph?")
print(f"Embedding dimension: {len(vector)}")   # 1536
```

### Pattern 2b — Azure OpenAI

Azure OpenAI runs the same OpenAI models inside your Azure tenant. The API is
identical to OpenAI's except for auth and endpoint — which causes one very
common mistake.

**The deployment_name vs model_name trap:**
- In Azure, you create a *deployment* and give it a name (e.g., `my-gpt4o`).
- The *model* is the underlying model (e.g., `gpt-4o`).
- You must pass `azure_deployment` (your deployment name) to LangChain.
- If you pass `model` instead, the call silently fails with a 404.

```python
# patterns/02b_azure_openai.py
from dotenv import load_dotenv
load_dotenv()

from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings

# ── Azure Chat ────────────────────────────────────────────────────────────────
# Reads from env: AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT,
#                 AZURE_OPENAI_API_VERSION
azure_llm = AzureChatOpenAI(
    azure_deployment="my-gpt4o",   # the NAME you chose when deploying in Azure portal
    # NOT model="gpt-4o" — that is the OpenAI constructor, not Azure
    temperature=0,
    max_tokens=4096,
    max_retries=3,
)

# ── Azure Embeddings ───────────────────────────────────────────────────────────
azure_embeddings = AzureOpenAIEmbeddings(
    azure_deployment="my-text-embedding-3-small",  # your embedding deployment name
)

# The interface is identical to ChatOpenAI after construction
from langchain_core.messages import HumanMessage
response = azure_llm.invoke([HumanMessage(content="Hello")])
print(response.content)
```

**Common mistake:** Using `AZURE_OPENAI_DEPLOYMENT_NAME` as the env var and
expecting LangChain to pick it up automatically. LangChain does NOT read this
variable — you must pass `azure_deployment=` explicitly in the constructor
(or set `AZURE_OPENAI_CHAT_DEPLOYMENT_NAME` which some versions do read, but
explicit is always safer).

---

## Pattern 3 — AWS Bedrock

Bedrock hosts multiple models (Claude, Llama, Titan, Mistral) inside AWS.
Auth is via IAM, not API keys — which is very different from the other
providers.

**Install:**
```bash
pip install langchain-aws boto3
```

**ALWAYS use `ChatBedrockConverse`, NOT `ChatBedrock`.**
`ChatBedrock` is the legacy class using the older `InvokeModel` API. It does
not support tool calling correctly and has inconsistent streaming. Bedrock
added the `Converse` API specifically to fix these issues. LangChain's
`ChatBedrockConverse` uses the new API.

**Auth options:**

| Method | When to use |
|---|---|
| Environment vars (`AWS_ACCESS_KEY_ID` etc.) | Local dev, CI/CD |
| IAM role (EC2/ECS/Lambda) | Production — no credentials to manage |
| AWS SSO / `aws configure sso` | Developer machines on SSO-enabled orgs |

```python
# patterns/03_bedrock.py
from dotenv import load_dotenv
load_dotenv()

import boto3
from langchain_aws import ChatBedrockConverse, BedrockEmbeddings
from langchain_core.messages import HumanMessage

# ── Option A: Use environment credentials (dev) ────────────────────────────────
# boto3 automatically reads AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
# AWS_DEFAULT_REGION from environment — load_dotenv() sets these
llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20251001-v1:0",  # Bedrock model ID format
    # Note: Bedrock model IDs differ from Anthropic's direct IDs
    region_name="us-east-1",   # must match where you enabled the model
    temperature=0,
    max_tokens=4096,
)

# ── Option B: Explicit boto3 session (production / multi-account) ─────────────
# Use this when you need to assume a role or use a specific profile
session = boto3.Session(
    region_name="us-east-1",
    # profile_name="my-aws-profile",  # uncomment if using named profile
)
llm_with_session = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20251001-v1:0",
    client=session.client("bedrock-runtime"),  # pass pre-built client
    temperature=0,
    max_tokens=4096,
)

# ── Option C: Cross-region inference ─────────────────────────────────────────
# Bedrock can route to multiple regions for higher throughput
# Use the "us." prefix model ID — this triggers cross-region routing
cross_region_llm = ChatBedrockConverse(
    model="us.anthropic.claude-sonnet-4-5-20251001-v1:0",  # "us." prefix = cross-region
    region_name="us-east-1",
    temperature=0,
    max_tokens=4096,
)

# ── Available Bedrock models (as of mid-2025) ─────────────────────────────────
# Claude claude-sonnet-4-6:  us.anthropic.claude-sonnet-4-5-20251001-v1:0
# Claude Haiku 3.5:  us.anthropic.claude-haiku-3-5-20241022-v1:0
# Llama 3.1 70B:     us.meta.llama3-1-70b-instruct-v1:0
# Titan Text G1:     amazon.titan-text-express-v1

# ── Embeddings via Titan ───────────────────────────────────────────────────────
embeddings = BedrockEmbeddings(
    model_id="amazon.titan-embed-text-v2:0",
    region_name="us-east-1",
)

# ── Invoke (same interface as every other provider) ───────────────────────────
response = llm.invoke([HumanMessage(content="What is AWS Bedrock?")])
print(response.content)
```

**Common mistakes:**
- Using `ChatBedrock` instead of `ChatBedrockConverse` — tool calling breaks
- Not enabling the model in the AWS Bedrock console before calling it — you
  get a `ModelNotReadyException` even though the model ID is correct
- Wrong region — models are enabled per-region; `us-east-1` has the broadest
  coverage

---

## Pattern 4 — Google Gemini

Two auth paths: Google AI Studio (personal API key) vs Vertex AI (enterprise
GCP). The classes are different.

**Install:**
```bash
pip install langchain-google-genai          # AI Studio (API key)
pip install langchain-google-vertexai       # Vertex AI (GCP ADC)
```

**Model selection:**

| Model | Context | When to use |
|---|---|---|
| `gemini-2.0-flash-exp` | 1M tokens | Fast, cheap, large context tasks |
| `gemini-1.5-pro` | 1M tokens | Quality + large context, more expensive |
| `gemini-1.5-flash` | 1M tokens | Cheap multimodal tasks |

Gemini's 1M-token context window is genuinely useful for entire codebase
analysis, long document Q&A, or multi-session context without summarization.

```python
# patterns/04_gemini.py
from dotenv import load_dotenv
load_dotenv()

# ── Option A: Google AI Studio (personal dev — API key) ───────────────────────
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

# Reads GOOGLE_API_KEY from environment
llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash-exp",
    temperature=0,
    max_tokens=4096,
)

# Embeddings — text-embedding-004 is the current best
embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")

from langchain_core.messages import HumanMessage
response = llm.invoke([HumanMessage(content="What is LangGraph?")])
print(response.content)


# ── Option B: Vertex AI (enterprise — Application Default Credentials) ────────
# ADC setup (run once in terminal, not in Python):
#   gcloud auth application-default login
#   gcloud config set project YOUR_GCP_PROJECT_ID
#
# No API key needed — ADC handles auth transparently
from langchain_google_vertexai import ChatVertexAI, VertexAIEmbeddings

vertex_llm = ChatVertexAI(
    model_name="gemini-1.5-pro",
    project="your-gcp-project-id",   # or set GOOGLE_CLOUD_PROJECT env var
    location="us-central1",
    temperature=0,
    max_output_tokens=4096,
)

vertex_embeddings = VertexAIEmbeddings(
    model_name="text-embedding-004",
    project="your-gcp-project-id",
)
```

**AI Studio vs Vertex AI decision:**
- **AI Studio:** API key, personal quota, no GCP account needed — use for dev
- **Vertex AI:** IAM auth, enterprise SLAs, data residency controls,
  higher quota — use for production on GCP

---

## Pattern 5 — Ollama (Local)

Ollama runs open-source models on your own hardware. No API key, no external
calls, no per-token cost. The trade-off: speed depends on your GPU (or CPU,
which is slow).

**When to use Ollama:**
- Privacy requirements — data cannot leave the building
- Offline / air-gapped environments
- Cost-free iteration during development
- Testing provider-agnostic code without spending money

**Install Ollama:**
```bash
# macOS
brew install ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Windows: download installer from https://ollama.com/download
```

**Pull models (run in terminal, not Python):**
```bash
ollama pull llama3.2          # Meta Llama 3.2 3B — fast, good general model
ollama pull llama3.2:70b      # Llama 3.2 70B — slower, better quality, needs ~40GB VRAM
ollama pull mistral           # Mistral 7B — fast, good for coding
ollama pull codellama         # Code Llama — specialised for code generation
ollama pull phi3              # Microsoft Phi-3 Mini — tiny, fast
ollama pull nomic-embed-text  # Embeddings model for fully local RAG
ollama pull llava             # Llava — vision model (multimodal)
ollama list                   # see what you have pulled
```

**Install Python package:**
```bash
pip install langchain-ollama
```

```python
# patterns/05_ollama.py
# Note: no load_dotenv() needed — Ollama needs no API key
# But keep load_dotenv() if you're mixing Ollama with other providers

from dotenv import load_dotenv
load_dotenv()

from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_core.messages import HumanMessage, SystemMessage

# ── Chat model ────────────────────────────────────────────────────────────────
llm = ChatOllama(
    model="llama3.2",            # must match a model you've pulled with `ollama pull`
    temperature=0,
    base_url="http://localhost:11434",  # default; change if Ollama is on another host
)

# ── Streaming — important for Ollama because local models can be slow ──────────
# Streaming lets you see tokens as they arrive rather than waiting for the full
# response, which feels much faster to the user
print("Streaming response:")
for chunk in llm.stream([HumanMessage(content="What is Python?")]):
    print(chunk.content, end="", flush=True)
print()

# ── Embeddings — enables fully local RAG with no external API calls ───────────
# OllamaEmbeddings + a local vector store (e.g. ChromaDB) = fully air-gapped RAG
embeddings = OllamaEmbeddings(
    model="nomic-embed-text",    # pull first: `ollama pull nomic-embed-text`
    base_url="http://localhost:11434",
)
vector = embeddings.embed_query("LangGraph state machine")
print(f"Embedding dimension: {len(vector)}")   # 768 for nomic-embed-text

# ── Vision with Llava ─────────────────────────────────────────────────────────
vision_llm = ChatOllama(
    model="llava",               # pull first: `ollama pull llava`
    temperature=0,
)
vision_response = vision_llm.invoke([
    HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
        {"type": "text", "text": "What is in this image?"},
    ])
])
```

**Performance note:** Without a GPU, Ollama generates ~5-15 tokens/second for
a 7B model. With a modern GPU (RTX 4090, M3 Max), it reaches 50-100+
tokens/second. For most dev tasks, CPU speed is acceptable; for interactive
chat apps, a GPU is strongly recommended.

---

## Pattern 6 — Provider-Agnostic Factory

This is the most important pattern. Instead of hardcoding a provider anywhere
in your application, every module calls `get_llm(tier="standard")` and gets
back a `BaseChatModel`. Swapping providers requires changing one environment
variable — no code changes.

```
LLM_PROVIDER=ollama            # runs local
LLM_PROVIDER=anthropic         # runs on Anthropic API
LLM_PROVIDER=openai            # runs on OpenAI API
```

```python
# provider_factory.py
"""
Provider-agnostic LLM factory for the langchain-lab plugin.

Usage:
    from provider_factory import get_llm, get_embeddings

    llm = get_llm(tier="standard")          # reads LLM_PROVIDER from env
    embeddings = get_embeddings()

    # Works with EVERY provider — same interface
    response = llm.invoke([HumanMessage(content="Hello")])

Environment variables:
    LLM_PROVIDER   = anthropic | openai | azure | bedrock | gemini | ollama
                     (default: anthropic)
    LLM_TIER       = fast | standard | powerful
                     (default: standard)
"""

from __future__ import annotations

import os
from typing import Literal

from dotenv import load_dotenv

load_dotenv()  # must be before any LangChain provider imports

from langchain_core.language_models import BaseChatModel
from langchain_core.embeddings import Embeddings

# ── Tier-to-model mapping per provider ────────────────────────────────────────
# fast      = cheapest / quickest model from that provider
# standard  = balanced quality/cost — good default for most tasks
# powerful  = best quality — use for complex reasoning, expensive to run

PROVIDER_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        "fast":      "claude-haiku-3-5",
        "standard":  "claude-sonnet-4-6",
        "powerful":  "claude-opus-4-5",
    },
    "openai": {
        "fast":      "gpt-4o-mini",
        "standard":  "gpt-4o",
        "powerful":  "o1",
    },
    "azure": {
        # These are YOUR Azure deployment names — edit to match your Azure setup
        "fast":      os.getenv("AZURE_FAST_DEPLOYMENT", "gpt-4o-mini"),
        "standard":  os.getenv("AZURE_STANDARD_DEPLOYMENT", "gpt-4o"),
        "powerful":  os.getenv("AZURE_POWERFUL_DEPLOYMENT", "gpt-4o"),
    },
    "bedrock": {
        "fast":      "us.anthropic.claude-haiku-3-5-20241022-v1:0",
        "standard":  "us.anthropic.claude-sonnet-4-5-20251001-v1:0",
        "powerful":  "us.anthropic.claude-opus-4-5-20251101-v1:0",
    },
    "gemini": {
        "fast":      "gemini-2.0-flash-exp",
        "standard":  "gemini-2.0-flash-exp",
        "powerful":  "gemini-1.5-pro",
    },
    "ollama": {
        "fast":      "phi3",
        "standard":  "llama3.2",
        "powerful":  "llama3.2:70b",
    },
}

EMBEDDING_MODELS: dict[str, str] = {
    "anthropic": "text-embedding-3-small",  # Anthropic has no embeddings; falls back to OpenAI
    "openai":    "text-embedding-3-small",
    "azure":     os.getenv("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"),
    "bedrock":   "amazon.titan-embed-text-v2:0",
    "gemini":    "models/text-embedding-004",
    "ollama":    "nomic-embed-text",
}


def get_llm(
    tier: Literal["fast", "standard", "powerful"] = "standard",
    *,
    provider: str | None = None,
    temperature: float = 0,
    max_tokens: int = 4096,
    **kwargs,
) -> BaseChatModel:
    """
    Return a chat model for the given tier, reading provider from LLM_PROVIDER env var.

    Args:
        tier: Model capability tier — "fast" (cheap), "standard" (balanced),
              "powerful" (best quality).
        provider: Override the LLM_PROVIDER env var. Use in tests to force a
                  specific provider without changing the environment.
        temperature: Sampling temperature. 0 = deterministic (recommended for
                     structured tasks), 1 = creative.
        max_tokens: Max tokens in the response. Anthropic requires this
                    explicitly; others have sensible defaults but we set it
                    everywhere for consistency.
        **kwargs: Passed through to the provider constructor — use for
                  provider-specific options (e.g., thinking=... for Anthropic).

    Returns:
        A BaseChatModel instance. All providers implement the same interface.
    """
    resolved_provider = (provider or os.getenv("LLM_PROVIDER", "anthropic")).lower()

    if resolved_provider not in PROVIDER_MODELS:
        raise ValueError(
            f"Unknown provider '{resolved_provider}'. "
            f"Valid values: {list(PROVIDER_MODELS.keys())}"
        )

    model_id = PROVIDER_MODELS[resolved_provider][tier]

    if resolved_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=3,
            **kwargs,
        )

    elif resolved_provider == "openai":
        from langchain_openai import ChatOpenAI
        # o1/o3 models require temperature=1 and do not support max_tokens directly
        if model_id.startswith("o"):
            return ChatOpenAI(model=model_id, temperature=1, max_retries=3, **kwargs)
        return ChatOpenAI(
            model=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=3,
            **kwargs,
        )

    elif resolved_provider == "azure":
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(
            azure_deployment=model_id,   # deployment name, not model name
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=3,
            **kwargs,
        )

    elif resolved_provider == "bedrock":
        from langchain_aws import ChatBedrockConverse  # always Converse, not legacy Bedrock
        return ChatBedrockConverse(
            model=model_id,
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    elif resolved_provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model_id,
            temperature=temperature,
            max_output_tokens=max_tokens,
            **kwargs,
        )

    elif resolved_provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=model_id,
            temperature=temperature,
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            **kwargs,
        )

    # Should never reach here — validated above
    raise ValueError(f"Unhandled provider: {resolved_provider}")


def get_embeddings(*, provider: str | None = None) -> Embeddings:
    """
    Return an embeddings model for the current provider.

    Anthropic does not provide embeddings — it falls back to OpenAI's
    text-embedding-3-small automatically. Set OPENAI_API_KEY if using
    Anthropic as the LLM provider.
    """
    resolved_provider = (provider or os.getenv("LLM_PROVIDER", "anthropic")).lower()
    model_id = EMBEDDING_MODELS.get(resolved_provider, "text-embedding-3-small")

    if resolved_provider in ("anthropic", "openai"):
        # Anthropic has no embeddings; both use OpenAI embeddings
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(model=model_id)

    elif resolved_provider == "azure":
        from langchain_openai import AzureOpenAIEmbeddings
        return AzureOpenAIEmbeddings(azure_deployment=model_id)

    elif resolved_provider == "bedrock":
        from langchain_aws import BedrockEmbeddings
        return BedrockEmbeddings(
            model_id=model_id,
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        )

    elif resolved_provider == "gemini":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        return GoogleGenerativeAIEmbeddings(model=model_id)

    elif resolved_provider == "ollama":
        from langchain_ollama import OllamaEmbeddings
        return OllamaEmbeddings(
            model=model_id,
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        )

    raise ValueError(f"No embedding model configured for provider: {resolved_provider}")
```

**How to use the factory across your entire application:**

```python
# Any module in your project — provider is never mentioned here
from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import HumanMessage
from provider_factory import get_llm, get_embeddings

# All tiers — swap LLM_PROVIDER in .env to run on any backend
classifier = get_llm(tier="fast")        # cheap, for high-volume routing decisions
reasoner   = get_llm(tier="standard")   # balanced, for most tasks
analyst    = get_llm(tier="powerful")   # expensive, for complex reasoning

# Embeddings — same provider as LLM
embeddings = get_embeddings()

# Usage is identical regardless of provider
response = reasoner.invoke([HumanMessage(content="Summarize this document.")])
```

---

## Pattern 7 — Multi-Provider Fallback Chain

When a provider rate-limits or goes down, `.with_fallbacks()` automatically
retries the next provider in the chain. No try/except needed in your code.

**How `.with_fallbacks()` works:**
1. Call the primary model
2. If it raises one of the listed exception types, move to the first fallback
3. If that also fails, move to the second fallback, and so on
4. If all fallbacks fail, raise the last exception

```python
# patterns/07_fallback.py
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage

# ── Simple two-provider fallback ──────────────────────────────────────────────
primary   = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=4096, max_retries=1)
secondary = ChatOpenAI(model="gpt-4o", max_tokens=4096, max_retries=1)

# with_fallbacks wraps primary; secondary is tried if primary raises
resilient_llm = primary.with_fallbacks(
    fallbacks=[secondary],
    exceptions_to_handle=(
        Exception,   # broad catch — fine for a fallback chain
        # Or be specific:
        # anthropic.RateLimitError,
        # anthropic.APIConnectionError,
        # openai.RateLimitError,
    ),
)

# Usage is identical to calling primary directly
response = resilient_llm.invoke([HumanMessage(content="Hello")])
print(response.content)


# ── Three-tier cost-optimized fallback chain ──────────────────────────────────
# Strategy: try cheap first, fall back to better models if cheap fails
# Use case: classification or routing tasks where cheap is usually fine,
#           but you need a backup for rate limits during traffic spikes

haiku   = ChatAnthropic(model="claude-haiku-3-5",   max_tokens=1024, max_retries=1)
sonnet  = ChatAnthropic(model="claude-sonnet-4-6",  max_tokens=4096, max_retries=1)
gpt4o   = ChatOpenAI(model="gpt-4o", max_tokens=4096, max_retries=1)
local   = ChatOllama(model="llama3.2")              # always available — no rate limits

cost_optimized_chain = haiku.with_fallbacks(
    fallbacks=[sonnet, gpt4o, local],
    exceptions_to_handle=(Exception,),
)

# ── Factory integration — add fallback at the factory level ──────────────────
from provider_factory import get_llm

def get_resilient_llm(tier="standard") -> BaseChatModel:
    """Returns the tier model with Ollama as the final local fallback."""
    primary = get_llm(tier=tier)
    local_fallback = get_llm(tier=tier, provider="ollama")  # always available
    return primary.with_fallbacks(
        fallbacks=[local_fallback],
        exceptions_to_handle=(Exception,),
    )
```

**OpenRouter as a meta-provider:**
OpenRouter is a single API that routes to 100+ models (Claude, GPT-4, Llama,
Mistral, etc.) with a single API key. Use `ChatOpenAI` with a custom base URL:

```python
# patterns/07b_openrouter.py
from dotenv import load_dotenv
load_dotenv()

from langchain_openai import ChatOpenAI

# OpenRouter exposes an OpenAI-compatible API — use ChatOpenAI with base_url
openrouter_llm = ChatOpenAI(
    model="anthropic/claude-sonnet-4-6",   # OpenRouter model ID format: provider/model
    openai_api_key=os.getenv("OPENROUTER_API_KEY"),
    openai_api_base="https://openrouter.ai/api/v1",
    temperature=0,
    max_tokens=4096,
    default_headers={
        "HTTP-Referer": "https://your-app.com",   # required by OpenRouter
        "X-Title": "My LangChain App",
    },
)
```

OpenRouter is especially useful for teams that want a single billing account
across multiple models, or for quickly benchmarking different providers without
managing separate API keys.

---

## Generated File: tests/test_provider_factory.py

Every provider pattern should have a test that verifies the factory returns a
working model and the interface is consistent regardless of provider. These
tests skip gracefully when credentials are not configured — safe to run in CI.

```python
# tests/test_provider_factory.py
"""
Provider factory tests.

Each test is marked with the provider it requires. Tests skip automatically
when the relevant environment variables are not set.

Run all:           pytest tests/test_provider_factory.py -v
Run one provider:  pytest tests/test_provider_factory.py -v -k anthropic
"""

from __future__ import annotations

import os
import pytest
from dotenv import load_dotenv

load_dotenv()

from langchain_core.messages import HumanMessage


# ── Fixtures ──────────────────────────────────────────────────────────────────

def skip_if_missing(*env_vars: str):
    """Skip the test if any of the listed env vars are unset."""
    missing = [v for v in env_vars if not os.getenv(v)]
    if missing:
        return pytest.mark.skipif(
            True,
            reason=f"Missing env vars: {', '.join(missing)}"
        )
    return pytest.mark.skipif(False, reason="")


SIMPLE_PROMPT = [HumanMessage(content="Say 'hello' in exactly one word.")]


# ── Helper ────────────────────────────────────────────────────────────────────

def assert_valid_response(llm):
    """Invoke the model with a simple prompt and assert the response is non-empty."""
    from langchain_core.messages import AIMessage

    response = llm.invoke(SIMPLE_PROMPT)

    assert isinstance(response, AIMessage), (
        f"Expected AIMessage, got {type(response)}"
    )
    assert response.content, "Response content was empty"
    assert isinstance(response.content, str), (
        f"Expected str content, got {type(response.content)}"
    )


# ── Factory smoke tests ───────────────────────────────────────────────────────

class TestGetLlm:
    """Test that get_llm() returns a working model for each provider."""

    @skip_if_missing("ANTHROPIC_API_KEY")
    def test_anthropic_standard(self):
        from provider_factory import get_llm
        llm = get_llm(tier="standard", provider="anthropic")
        assert_valid_response(llm)

    @skip_if_missing("ANTHROPIC_API_KEY")
    def test_anthropic_fast(self):
        from provider_factory import get_llm
        llm = get_llm(tier="fast", provider="anthropic")
        assert_valid_response(llm)

    @skip_if_missing("OPENAI_API_KEY")
    def test_openai_standard(self):
        from provider_factory import get_llm
        llm = get_llm(tier="standard", provider="openai")
        assert_valid_response(llm)

    @skip_if_missing("OPENAI_API_KEY")
    def test_openai_fast(self):
        from provider_factory import get_llm
        llm = get_llm(tier="fast", provider="openai")
        assert_valid_response(llm)

    @skip_if_missing(
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_STANDARD_DEPLOYMENT",
    )
    def test_azure_standard(self):
        from provider_factory import get_llm
        llm = get_llm(tier="standard", provider="azure")
        assert_valid_response(llm)

    @skip_if_missing("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    def test_bedrock_standard(self):
        from provider_factory import get_llm
        llm = get_llm(tier="standard", provider="bedrock")
        assert_valid_response(llm)

    @skip_if_missing("GOOGLE_API_KEY")
    def test_gemini_standard(self):
        from provider_factory import get_llm
        llm = get_llm(tier="standard", provider="gemini")
        assert_valid_response(llm)

    def test_ollama_standard(self, monkeypatch):
        """Ollama test — requires Ollama running locally with llama3.2 pulled."""
        import httpx
        try:
            # Check Ollama is running before attempting the test
            httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        except Exception:
            pytest.skip("Ollama not running locally")

        from provider_factory import get_llm
        llm = get_llm(tier="standard", provider="ollama")
        assert_valid_response(llm)


# ── Factory interface consistency ─────────────────────────────────────────────

class TestProviderInterface:
    """All providers must expose the same interface after construction."""

    @skip_if_missing("ANTHROPIC_API_KEY")
    def test_streaming_works(self):
        """Streaming should yield at least one chunk."""
        from provider_factory import get_llm
        llm = get_llm(tier="fast", provider="anthropic")

        chunks = list(llm.stream(SIMPLE_PROMPT))
        assert len(chunks) > 0, "Streaming returned no chunks"
        full_text = "".join(c.content for c in chunks if c.content)
        assert full_text.strip(), "Streamed content was empty"

    @skip_if_missing("ANTHROPIC_API_KEY")
    def test_bind_tools_works(self):
        """bind_tools() must not raise — validates provider supports tool calling."""
        from langchain_core.tools import tool
        from provider_factory import get_llm

        @tool
        def dummy_tool(x: int) -> int:
            """A dummy tool that doubles its input."""
            return x * 2

        llm = get_llm(tier="fast", provider="anthropic")
        llm_with_tools = llm.bind_tools([dummy_tool])
        # Just verify bind_tools returns a Runnable without error
        assert llm_with_tools is not None

    @skip_if_missing("OPENAI_API_KEY")
    def test_structured_output_works(self):
        """with_structured_output() must return a Pydantic model."""
        from pydantic import BaseModel
        from provider_factory import get_llm

        class Answer(BaseModel):
            word: str
            language: str

        llm = get_llm(tier="fast", provider="openai")
        structured = llm.with_structured_output(Answer)
        result = structured.invoke([HumanMessage(content="Say 'hello' in English.")])
        assert isinstance(result, Answer)
        assert result.word
        assert result.language


# ── Invalid provider ──────────────────────────────────────────────────────────

class TestFactoryErrors:
    def test_invalid_provider_raises(self):
        from provider_factory import get_llm
        with pytest.raises(ValueError, match="Unknown provider"):
            get_llm(tier="standard", provider="fake-provider")

    def test_invalid_tier_raises(self):
        from provider_factory import get_llm
        with pytest.raises(Exception):   # KeyError or ValueError
            get_llm(tier="ultra-powerful", provider="anthropic")  # type: ignore


# ── Fallback chain ────────────────────────────────────────────────────────────

class TestFallbackChain:
    @skip_if_missing("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    def test_fallback_invokes_secondary_on_primary_failure(self, monkeypatch):
        """When primary always raises, the fallback should respond."""
        from provider_factory import get_llm
        from langchain_core.messages import AIMessage

        primary = get_llm(tier="fast", provider="anthropic")
        secondary = get_llm(tier="fast", provider="openai")

        # Monkeypatch primary.invoke to simulate an outage
        def always_fail(*args, **kwargs):
            raise ConnectionError("Simulated primary failure")

        monkeypatch.setattr(primary, "invoke", always_fail)

        resilient = primary.with_fallbacks(
            fallbacks=[secondary],
            exceptions_to_handle=(ConnectionError,),
        )
        response = resilient.invoke(SIMPLE_PROMPT)
        assert isinstance(response, AIMessage)
        assert response.content
```

---

## Common Mistakes Reference

| Mistake | What goes wrong | Fix |
|---|---|---|
| `load_dotenv()` after provider import | Auth error: key not found even though it's in `.env` | Always call `load_dotenv()` as the very first line before any LangChain import |
| `ChatBedrock` instead of `ChatBedrockConverse` | Tool calling broken, inconsistent streaming | Use `ChatBedrockConverse` — always |
| Azure: `model=` instead of `azure_deployment=` | HTTP 404 from Azure endpoint | Use `azure_deployment=` with your deployment name, not `model=` |
| Floating model aliases (`claude-sonnet-latest`) | Behaviour changes silently on new releases | Pin dated IDs: `claude-sonnet-4-6` |
| Bedrock: model not enabled in console | `ModelNotReadyException` at runtime | Go to AWS Console → Bedrock → Model access, enable the model in your region |
| Gemini Vertex AI without ADC setup | Auth error: credentials not found | Run `gcloud auth application-default login` in terminal first |
| Ollama: model not pulled | `pull model` error at runtime | Run `ollama pull <model>` in terminal before calling the Python code |
| Anthropic: `max_tokens` not set | Validation error on construction | Anthropic requires `max_tokens` — no default; always set it |
| o1/o3 with `temperature=0` | API error: temperature must be 1 for o-series | Hard-code `temperature=1` for o1/o3 in the factory |

---

## Transitions

After completing this skill, natural next steps:

- **`lc:rag`** — build a RAG pipeline using `get_embeddings()` from the factory
- **`lc:agent`** — build a LangGraph agent using `get_llm()` as the backbone
- **`lc:memory`** — add cross-session memory using the provider-agnostic LLM
- **`lc:trace`** — add LangSmith tracing to observe provider latency and cost
- **`lc:test`** — expand the provider tests with evaluation datasets

---

## See Also

- Anthropic model docs: https://docs.anthropic.com/en/docs/about-claude/models
- OpenAI model docs: https://platform.openai.com/docs/models
- AWS Bedrock model IDs: https://docs.aws.amazon.com/bedrock/latest/userguide/model-ids.html
- Google Gemini models: https://ai.google.dev/gemini-api/docs/models/gemini
- Ollama model library: https://ollama.com/library
- LangChain provider integrations: https://python.langchain.com/docs/integrations/chat/
- OpenRouter model list: https://openrouter.ai/models
