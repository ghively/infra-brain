# DR: embedding provider on the dev deployment — Ollama gateway + mxbai-embed-large @ 1024

**Date:** 2026-07-22 · **Status:** accepted · **Relates to:**
`docs/ARCHITECTURE.md` "RAG knowledge store"

## Context

The RAG store embeds Confluence chunks through
`embeddings.py::get_embeddings()`, a provider factory supporting `bedrock` and
`openai`. The `document_chunks.embedding` column is `Vector(_EMBED_DIM)` with
`_EMBED_DIM = 1024` **hardcoded** in `db/models/core.py` (the migration and HNSW
index are built at that width; changing it is a schema migration + full re-embed).

During activation prep on `deploy-host-01` (2026-07-22) we found:

- The host's `llm_provider=openai` is not OpenAI: `openai_base_url` points at
  **Ollama on the host** (`http://host.docker.internal:11434/v1`, Ollama 0.21.0).
  There are no AWS/Bedrock credentials and no real OpenAI account in this lab.
- Probing Ollama's OpenAI-compat `/v1/embeddings` empirically:
  - plain-string input → works;
  - tiktoken token-ID-array input (what `langchain_openai.OpenAIEmbeddings` sends
    by default) → HTTP 400 `invalid input type`;
  - the `dimensions` request param → **silently ignored**, native width returned.
- Models already on the host: `nomic-embed-text` (768-dim) — mismatches the
  1024 column; inserts would be rejected by pgvector.

## Decision

1. **Embed via the existing Ollama, treated as an OpenAI-compatible gateway** —
   no new cloud credentials, embedding traffic stays on-host (consistent with the
   lab's LLM posture).
2. **Model: `mxbai-embed-large`** (pulled 2026-07-22, probed at exactly **1024**
   native dims — matches `Vector(_EMBED_DIM)` with no `EMBEDDING_DIM` override,
   keeping dev schema identical to CI/test schema). Set
   `EMBEDDING_OPENAI_MODEL=mxbai-embed-large` in `INFRA_BRAIN_ENV`.
3. **Code fix:** `get_embeddings()` defaults
   `check_embedding_ctx_length=False` whenever `openai_base_url` is set, so raw
   strings are sent instead of token arrays. `setdefault` keeps caller override
   possible. The `dimensions=` pin stays (honored by real OpenAI/OpenRouter,
   harmlessly ignored by Ollama).

## Consequences

- Because gateways ignore `dimensions`, the pin is **not** a safety net on this
  path: the chosen gateway model MUST natively emit 1024-wide vectors. A
  wrong-width model fails loudly at insert time (pgvector rejects the width), not
  at configuration time.
- Embedding quality/latency is bounded by a ~334M-param local model on CPU —
  acceptable for a wiki-citation store; revisit if retrieval quality disappoints.
- Switching to Bedrock Titan or real OpenAI later is a config-only change
  (`EMBEDDING_PROVIDER`/keys) **as long as the replacement emits 1024 dims**;
  anything else re-triggers the migration + re-embed cost.

## Alternatives rejected

- **`nomic-embed-text` (already present):** 768-dim; would require either a schema
  migration to `Vector(768)` (diverges dev from CI, one-way churn) or Matryoshka
  truncation Ollama's OpenAI-compat endpoint can't express (`dimensions` ignored).
- **Bedrock Titan (the factory's default fallback):** no AWS credentials in this
  lab; introducing cloud creds for a dev-only feature is unjustified.
- **Real OpenAI `text-embedding-3-small`:** no OpenAI account; the configured
  `OPENAI_API_KEY` is an Ollama placeholder.
- **Patching `EMBEDDING_DIM=768` via env:** dead on arrival — `_EMBED_DIM` is
  hardcoded in the ORM, not env-driven; the env knob only affects the request-side
  `dimensions` param, which the gateway ignores anyway.
