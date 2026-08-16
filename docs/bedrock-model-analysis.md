# AWS Bedrock Model Analysis — Infra Brain LLM Selection

**Date:** 2026-06-19
**Purpose:** Pick the right Bedrock model(s) for Infra Brain's LLM workloads — minimize cost without losing quality.
**Method:** Fanned out 15 Haiku research subagents (one per provider group), each scoring its models against our three real workloads, then synthesized here.

> ⚠️ **Verification note.** Model availability, context windows, tool-use support, and especially **pricing** change fast and are region/account-specific. Treat every figure below as a research estimate. Before committing a model, confirm with:
> `aws bedrock list-inference-profiles` (your region) + the [AWS Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/), and enable model access in the console first. Newer/preview entries (Fable 5, Mythos 5, GPT‑5.x, GLM 5, MiniMax M2.5, Qwen3 Coder Next) carry the most uncertainty.

## The three workloads every model is scored against

| ID | Workload | Where in code | What it demands |
|----|----------|---------------|-----------------|
| **A** | Agentic tool-calling loop, ≤10 iterations, must emit **strict JSON** | `DiscoveryAgent.collect()` → `reason()` | Reliable multi-step tool use + valid JSON. Hardest. Scheduled (latency irrelevant). |
| **B** | Natural-language → **SQL** generation (read-only) | `IntegrationAgent.query()` → `create_sql_agent` | Correct SQL / strong code reasoning. Bounded. Errors non-destructive. |
| **C** | Interactive **streaming chat** with read-only DB tools | `chat/agent.py` | Fast + cheap + good-enough tool routing. User-facing → latency matters. |

**Reminder on scope:** only **3 agents** (`DiscoveryAgent`, `CoverageAgent`, `QueryAgent` —
the `LLMAgent` subclasses) ever call an LLM via the domain-agent path; the rest of
`AGENT_REGISTRY` (28 entries as of this writing) are deterministic collectors (no tokens).
The streaming chat feature (`chat/agent.py`, workload C above) is a separate, non-agent
FastAPI code path that also calls an LLM — as of 2026-07-01 it does so **without** the
project's safety callbacks wired in (see the bugcheck note in
`docs/DE-BRITTLING-PLAN.md`), which is a gap independent of the model-selection analysis
below.

---

# Part 1 — Cross-provider comparison (the decision view)

Prices = Bedrock on-demand $/M tokens (input / output). "≈/unv." = unverified estimate.

## Cost ladder (text-gen, tool-capable models — cheapest first)

| Model | $/M in / out | Tool calling | Notes |
|---|---|---|---|
| Nova Micro | 0.035 / 0.14 | Yes (weak agentic) | text-only, ultra-budget |
| Gemma 4 E2B | ≈low / unv. | Yes | tiny, low-latency |
| Nova Lite | 0.06 / 0.24 | Yes | AWS-native, fast |
| Llama 4 Scout | 0.17 / 0.17 | Yes | 10M context, flat pricing |
| Jamba 1.5 Mini | 0.20 / 0.40 | Yes | 256K context, efficient |
| Llama 4 Maverick | 0.20 / 0.80 | Yes (strong) | MoE, fast, strong FC |
| Qwen3 Next 80B A3B | ≈low / unv. | Yes | MoE 3B active, agentic-tuned |
| Qwen3 Coder 30B A3B | ≈low / unv. | Yes | code-specialized |
| GLM 4.7 / 4.7 Flash | ≈low / unv. | Yes | 203K ctx, Flash = low-latency |
| Kimi K2 Thinking | 0.60 / 2.50 | Yes | reasoning variant |
| Kimi K2.5 | 0.60 / 3.00 | Yes | agentic + vision |
| DeepSeek V3.2 | 0.62 / 1.85 | Yes (strict JSON) | strongest open agentic/JSON |
| Nova Pro | 0.80 / 3.20 | Yes | AWS-native, balanced |
| Claude Haiku 4.5 | 1.00 / 5.00 | Yes (very good) | proprietary, reliable |
| MiniMax M2.5 | ≈med / unv. | Yes (BFCL 76.9%) | agent-native frontier |
| GLM 5 | ≈med / unv. | Yes | frontier agentic coding |
| Mistral Large 3 | ≈high / unv. | Yes | 256K, proven FC |
| Mistral Large | 2.00 / 6.00 | Yes | older |
| Jamba 1.5 Large | 2.00 / 8.00 | Yes | 256K, hybrid SSM |
| Claude Sonnet 4.6 | 3.00 / 15.00 | Yes (excellent) | quality workhorse |
| GPT‑5.4 | ≈med-high / unv. | Yes | GA Jun 2026 |
| GPT‑5.5 | ≈high / unv. | Yes | top agentic |
| Claude Opus 4.8 | premium / unv. | Yes (excellent) | max reasoning |

## Workload A — agentic tool-loop + strict JSON (ranked best-fit)

| Tier | Models | Why |
|---|---|---|
| **Max quality** | Claude Opus 4.8 · GPT‑5.5 · Claude Sonnet 4.6 · *Fable 5*\* | Best multi-step tool reliability + JSON; pricey |
| **Strong / mid cost** | **MiniMax M2.5** · **GLM 5** · Kimi K2.5 · Claude Haiku 4.5 · Nova Pro · Writer Palmyra X4 · GPT‑5.4 · Mistral Large 3 | Purpose-built agentic models; great quality-per-dollar |
| **Cheap / good** | **DeepSeek V3.2** · Llama 4 Maverick · Qwen3 Next 80B A3B · GLM 4.7 · Nova 2 Lite · Ministral 8B · Jamba Mini | Solid FC + JSON; big savings |
| **Budget / verify JSON** | Llama 4 Scout · Nova Lite · gpt‑oss‑20b · Gemma 4 E2B | Works, but higher JSON-flake risk in a 10-iter loop |

\* *Fable 5 / Mythos 5 reported as access-restricted (per subagent, unverified) — treat Opus 4.8 as the practical proprietary ceiling.*

## Workload B — NL→SQL (code reasoning, bounded)

| Tier | Models | Why |
|---|---|---|
| **Best value cheap** | **DeepSeek V3.2** · **Qwen3 Coder 30B A3B** | Code-specialized, cheap, strong SQL |
| **Strong** | Qwen3 Coder 480B A35B · GLM 5 · Llama 4 Scout (huge ctx for big schemas) · Ministral 14B | Heavier code models / schema-friendly context |
| **Premium correctness** | Claude Sonnet 4.6 · Claude Haiku 4.5 · GPT‑5.4 · Mistral Large 3 | When SQL accuracy is paramount |

## Workload C — interactive chat (fast + cheap)

| Tier | Models | Why |
|---|---|---|
| **Best balance** | **Llama 4 Maverick** · **GLM 4.7 Flash** | Fast, cheap, strong tool routing |
| **Ultra-budget** | Nova Lite · Nova Micro · Jamba Mini · Gemma 4 E2B | Rock-bottom cost, low latency, weaker answers |
| **Premium UX** | Claude Haiku 4.5 · Kimi K2.5 · Nova Pro · Claude Sonnet 4.6 | Polished answers, slightly pricier |

## Headline recommendation (cheaper, non-Anthropic, quality-aware)

| Agent | Recommended default | Cheaper alt | Premium alt |
|---|---|---|---|
| **DiscoveryAgent (A)** | **DeepSeek V3.2** (strict JSON, cheap) or **MiniMax M2.5** (agent-native) | Llama 4 Maverick | Claude Sonnet 4.6 / GPT‑5.4 |
| **IntegrationAgent NL→SQL (B)** | **DeepSeek V3.2** or **Qwen3 Coder 30B** | Llama 4 Maverick | Claude Sonnet 4.6 |
| **Chat UI (C)** | **Llama 4 Maverick** or **GLM 4.7 Flash** | Nova Lite | Claude Haiku 4.5 |

Plus: switch DiscoveryAgent to `with_structured_output` so even a weaker model can't emit broken JSON; and make every model choice a **per-agent env var** so you can retune without code changes.

---

# Part 2 — Full catalog by provider

### AI21 Labs

| Model | Modality | Tool Calling | Context | Cost | Strengths | Best for | Fit |
|---|---|---|---|---|---|---|---|
| Jamba 1.5 Large | Text | Yes | 256K | High (2/8) | Hybrid SSM-Transformer, long-context, JSON FC | Long-doc reasoning, agentic | A,B,C |
| Jamba 1.5 Mini | Text | Yes | 256K | Low (0.20/0.40) | Efficient, fast, long context | Cost-sensitive chat/SQL | B,C |

**Standout:** Jamba 1.5 Mini (cheap, long-context, solid tools) for B/C.

### Amazon (Nova / Titan)

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| Nova 2 Lite | Text+img/video | Yes | 1M | Low | Fast reasoning, +60% tool-call efficiency | A,B,C |
| Nova 2 Sonic | Speech+text | Yes | 300K | Low | Speech-to-speech | C (voice) |
| Nova Lite | Text+img/video | Yes | 300K | Low | Multimodal, cheap, fast | A,B,C |
| Nova Micro | Text | Yes | 128K | Very low | Lowest latency/cost | A,B,C (simple) |
| Nova Premier | Text+img/video | Yes | 1M | High | Most capable Nova | A,B,C |
| Nova Pro | Text+img/video | Yes | 300K | Med | Best accuracy/cost balance (~33% < Sonnet) | A,B,C |
| Nova Canvas / Reel | Image / Video | No | — | Med/High | Image / video generation | N/A |
| Titan Text Express/Lite | Text | Yes (unv.) | 8K/4K | Very low | Legacy, small context, weak code | A,B (weak) |
| Titan Image / Multimodal / Text Embeddings | Image/Embedding | No | — | Low-Med | Generation / embeddings | N/A |

**Standout:** Nova 2 Lite (1M ctx, strong tools, ultra-cheap) for A/C; Nova Pro for B. Avoid Titan for agents.

### Anthropic (Claude)

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| Claude Fable 5 | Text+img | Yes | 1M | High | Mythos-class, top tool use (restricted, unv.) | A,B,C |
| Claude Mythos 5 (preview) | Text+img | Yes | 1M | High | No safety classifiers, restricted | A,B,C |
| Opus 4.8 / 4.7 / 4.6 | Text+img | Yes | 1M | High/premium | Best reasoning + tools | A,B,C |
| Sonnet 4.6 | Text+img | Yes | 1M | Med (3/15) | Balanced workhorse | A,B,C |
| Sonnet 4.5 / 4 | Text+img | Yes | 200K | Med | Prior gen | A,B,C |
| Haiku 4.5 | Text+img | Yes | 200K | Low (1/5) | Fast, near-frontier, reliable | A,C |
| Claude 3.5 Haiku | Text+img | Yes | 200K | Low | Older fast | C |
| Claude 3 Haiku | Text+img | Yes | 200K | Low | Deprecated, old cutoff | — |

**Standout:** Opus 4.8 / Sonnet 4.6 for A/B (max reliability); Haiku 4.5 for C. (Fable/Mythos restricted.)

### Cohere

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| Command R+ | Text | Yes | unv. | Med | Multi-step tool use, RAG (**Legacy** — migrate) | A,B |
| Command R | Text | Yes | 128K | Low | RAG, tools (**EOL ~Aug 2026**) | A,B,C |
| Rerank 3.5 / Embed (Eng/Multi/v4) | Rerank/Embed | N/A | — | Low | Retrieval | N/A |

**Standout:** none recommended — Command R/R+ are Legacy; don't build new on them.

### DeepSeek

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| V3.2 | Text | Yes (strict JSON) | 164K | Med (0.62/1.85) | Best open agentic + JSON + code | A,B,C |
| V3.1 | Text | unv. | 128K | Med | Strong reasoning/code | A,B |
| R1 | Text | unv. | 128K | High | Deep CoT; 5–50× token blowup | Not for loops |

**Standout:** **DeepSeek V3.2** — best cheap option for A and B. R1's reasoning overhead hurts a 10-iter loop.

### Google (Gemma)

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| Gemma 4 31B | Text+img/video | Yes | 256K | High | Strong reasoning/code | A,B |
| Gemma 4 26B-A4B | Text+img/video | Yes | 256K | Med | MoE, balanced | A,B |
| Gemma 4 E2B | Text+img/audio/video | Yes | 128K | Low | Compact, low-latency | C |
| Gemma 3 4B/12B IT | Text+img | No (unv.) | 128K | Low | Small; 12B OK for chat | C (12B) |
| Gemma 3 27B PT | Text+img | No | 128K | Med | Pretrained, not chat-ready | N/A |

**Standout:** Gemma 4 E2B for C; Gemma 4 26B/31B for A. Avoid Gemma 3 for tool use.

### Meta (Llama)

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| Llama 4 Maverick 17B | Text+img | Yes | 1M | ~0.20/0.80 | MoE, parallel tool calls, fast | A,C |
| Llama 4 Scout 17B | Text+img | Yes | 10M | ~0.17/0.17 | Massive context, cheap | B,C |
| Llama 3.3 70B | Text | Yes (inconsistent JSON) | 128K | Med (0.72/0.72) | Needs JSON validation/retry | A (guarded) |
| Llama 3.1 405B | Text | Yes | 128K | High | SOTA 3.1 tool use | A |
| Llama 3.1 70B / 8B | Text | Yes | 128K | Med/Low | Balanced / lightweight | A,B |
| Llama 3.2 1B/3B/11B/90B | Text/+img | unv. | 128K | Low-Med | Tiny→weak; 90B for docs | C (mid) |
| Llama 3 8B/70B | Text | No/weak | 8K | Low/Med | Pre-tool era | N/A |

**Standout:** Llama 4 Maverick (A/C, cheap+strong FC); Llama 4 Scout (B/C, 10M ctx). Avoid Llama 3 original.

### MiniMax

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| MiniMax M2 | Text | Yes | 1M | Low | Long context, coding | A,C |
| MiniMax M2.1 | Text | Yes | 196K | Low-Med | Improved reasoning | A,B,C |
| MiniMax M2.5 | Text | Yes (BFCL 76.9%) | 196K | Med | Agent-native frontier, token-efficient | A,B |

**Standout:** **MiniMax M2.5** — purpose-built agentic, strong BFCL, great for A.

### Mistral AI

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| Mistral Large 3 | Text | Yes | 256K | High | MoE, strong JSON/tools | A,B |
| Mistral Large | Text | Yes | 32K | High (2/6) | Older, small ctx | A |
| Mistral Small | Text | Yes | 128K | Med | Cost-efficient | A (limited) |
| Ministral 8B (3.0) | Text/vision | Yes | 128K | Low-Med | Native tools, JSON, vision | A,B,C |
| Ministral 14B (3.0) | Text/vision | Yes | 128K | Med | Strongest OSS reasoning | B |
| Ministral 3B | Text/vision | Yes | 128K | Low | Edge | B,C |
| Magistral Small 2509 | Text/vision | unv. | 128K | Med | CoT reasoning | N/A |
| Mixtral 8x7B | Text | No (unv.) | 32K | Med | No reliable tools | N/A |
| Pixtral Large / Voxtral | Vision / Audio | varies | 128K/32K | High/Low | Vision / speech | N/A |

**Standout:** Mistral Large 3 (A); Ministral 14B (B); Ministral 8B (C).

### Moonshot AI (Kimi)

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| Kimi K2.5 | Text+img | Yes | 256K | Med (0.60/3.00) | Multimodal agentic | A,C |
| Kimi K2 Thinking | Text | Yes | 256K | Med (0.60/2.50) | CoT reasoning catches errors early | A,B |

**Standout:** Kimi K2 Thinking for A/B (reasoning improves SQL + tool decisions). *Note: Bedrock Converse occasionally leaks internal reasoning tokens — test serialization.*

### NVIDIA (Nemotron)

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| Nemotron 3 Super 120B | Text | Yes | 256K | Med-High | Agent/tool-tuned, high throughput | A,B,C |
| Nemotron Nano 3 30B | Text | unv. | 256K | Med | MoE, fast | A,B,C |
| Nemotron Nano 9B v2 | Text | unv. | 128K | Low | Efficient | C |
| Nemotron Nano 12B v2 VL | Text+img | unv. | 128K | Low | Vision-language | N/A |

**Standout:** Nemotron 3 Super 120B (only confident tool-calling fit) for A.

### OpenAI

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| GPT‑5.5 | Text+img | Yes | ~1M | High | Top agentic, error recovery | A,C |
| GPT‑5.4 | Text+img | Yes | ~1M | Med | Best price/perf of GPT‑5.x | A,B,C |
| gpt‑oss‑120b / 20b | Text | Yes | 128K | Low (OSS) | Strong tools, open-weight | A,B |
| GPT OSS Safeguard 120B/20B | Text | unv. | 128K | Low | Safety-classification only | N/A |

**Standout:** GPT‑5.4 (covers A/B/C, balanced) if a proprietary non-Anthropic is wanted; gpt‑oss‑120b for open-weight. (GPT‑5.x GA on Bedrock Jun 2026, per subagent.)

### Qwen

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| Qwen3 Next 80B A3B | Text | Yes | 256K | Low | MoE 3B active, agentic-tuned | A |
| Qwen3 235B A22B 2507 | Text | Yes | 256K | Med | Balanced reasoning | A |
| Qwen3 32B | Text | Yes | 32K | Low | Lightweight | A |
| Qwen3 VL 235B A22B | Text+vision | Yes | 256K | Med | Vision/OCR | A (vision) |
| Qwen3 Coder Next | Text | Yes | 256K | Med | Multi-turn tool calling | A |
| Qwen3 Coder 480B A35B | Text | Yes | 128K | High | SOTA code; NL→SQL (unv.) | A,B |
| Qwen3 Coder 30B A3B | Text | Yes | 256K | Low | Efficient code model | A,B |

**Standout:** Qwen3 Next 80B A3B (A, cheap agentic); Qwen3 Coder 30B/480B (B, code/SQL).

### Writer & Z.AI (GLM)

| Model | Modality | Tool Calling | Context | Cost | Notes | Fit |
|---|---|---|---|---|---|---|
| Writer Palmyra X4 | Text | Yes | 128K | Med | Industry-high tool-call accuracy (~78.8%) | A,B |
| Writer Palmyra X5 | Text | Yes | 128K–1M (unv.) | Low-Med | Long ctx, fast multi-turn FC | A,B,C |
| Writer Palmyra Vision 7B | Text+vision | unv. | 4K | Low | Visual docs (tiny ctx) | N/A |
| GLM 4.7 | Text | Yes | 203K | Low | Strong reasoning, native tools | A,B,C |
| GLM 4.7 Flash | Text | Yes | 203K | Low | Ultra-low latency | C |
| GLM 5 | Text | Yes (unv.) | 200K | Med | Frontier agentic coding, 128K output | A |

**Standout:** GLM 5 (A); GLM 4.7 Flash (C); Palmyra X5 (B).

### Stability AI & TwelveLabs (media — out of scope)

| Model | Modality | Purpose | Fit |
|---|---|---|---|
| Stable Image suite (upscale/inpaint/outpaint/bg-removal/control/search/style) | Image | Image gen & editing | N/A |
| Marengo Embed 3.0 / v2.7 | Video | Video embedding | N/A |
| Pegasus v1.2 | Video | Video understanding | N/A |

**Standout:** none — all image/video/embedding, not text generation.

---

# Part 3 — Data-quality caveats

- **Pricing** is the least reliable field — several models returned "unverified." Confirm against the AWS pricing page before committing.
- **Legacy/EOL:** Cohere Command R (EOL ~Aug 2026) and R+ (Legacy) — don't build new work on them.
- **Restricted:** Claude Fable 5 / Mythos 5 reported access-restricted (unverified) — plan around Opus 4.8 / Sonnet 4.6.
- **JSON reliability:** Llama 3.3 70B has documented markdown-instead-of-JSON issues; small/PT models (Gemma 3, tiny Llama, Titan) are weak for strict-JSON tool loops. Mitigate with `with_structured_output` + retry.
- **Reasoning models** (DeepSeek R1, Kimi K2 Thinking, Magistral): can blow up token usage and may leak reasoning tokens through Bedrock Converse — test serialization before production.
- **BFCL gap:** open-weights still trail top proprietary models by ~8–15 pts on function calling; the gap concentrates in multi-step loops (our Workload A).
