---
name: lc-compliance
description: >
  Use when building GDPR, HIPAA, or EU AI Act compliant LangChain/LangGraph
  applications. Triggered by: "add GDPR compliance", "HIPAA for my agent",
  "EU AI Act requirements", "data subject rights", "right to erasure",
  "PHI detection", "PII masking", "data residency", "privacy-preserving RAG",
  "compliance checklist", "data governance", "/lc:compliance".
  NOTE: This skill provides TECHNICAL implementation patterns only.
  It is NOT legal advice. Always consult qualified legal counsel.
---

# lc:compliance — GDPR, HIPAA, EU AI Act, and Data Governance

> **LEGAL DISCLAIMER — READ FIRST**
> This skill provides technical implementation patterns for privacy and
> compliance in LangChain/LangGraph applications. It is **NOT legal advice**
> and does NOT constitute a compliance certification of any kind.
> Regulations change, enforcement varies by jurisdiction, and your specific
> facts matter enormously. **Always engage qualified legal counsel** before
> deploying in regulated environments.

---

## Trigger Phrases

- "add GDPR compliance"
- "right to erasure / right to be forgotten"
- "delete user data"
- "data subject rights"
- "HIPAA for my chatbot"
- "PHI detection"
- "PII masking"
- "EU AI Act"
- "high-risk AI system"
- "data residency"
- "LangSmith data leaving EU"
- "privacy-preserving RAG"
- "compliance checklist"
- "data governance"
- `/lc:compliance`

---

## Teaching Section — What Each Regulation TECHNICALLY Requires

Read this before touching any code. Regulations map directly to LangChain
patterns once you understand what they demand technically.

### GDPR — General Data Protection Regulation (EU/EEA)

GDPR governs personal data: any information that identifies or can identify
a natural person — names, emails, IP addresses, conversation content
that reveals identity, health information, etc.

**Technical obligations that affect LangChain apps:**

| GDPR Principle | What It Means Technically |
|---|---|
| **Data minimization** | Only send the minimum personal data needed for the LLM task. Strip irrelevant fields before building the prompt. |
| **Purpose limitation** | Data collected for task A cannot be used for task B. Don't cross-contaminate conversation memory between unrelated sessions. |
| **Storage limitation** | Delete data when its purpose is fulfilled. Conversation memory, checkpointer state, and vector embeddings must all be deletable per user. |
| **Data subject rights** | Users can request: access (export), rectification (update), erasure (delete), portability (JSON export). Your app must honor these. |
| **Lawful basis** | You need a legal reason to process data: consent, contract, legitimate interest, etc. Document which one applies. |
| **Data transfers** | Sending data to US-based LLM providers (OpenAI, Anthropic, LangSmith) from the EU requires a legal mechanism (e.g., SCCs — Standard Contractual Clauses). |

**LangChain pattern mapping:**
- Storage limitation → `checkpointer.delete(thread_id)` + `vectorstore.delete(ids)`
- Data subject rights → `delete_user_data()` function (see Pattern 3)
- Data minimization → preprocessing node strips PII before LLM call
- Data transfers → LangSmith data residency options (see Pattern 2)

### HIPAA — Health Insurance Portability and Accountability Act (US healthcare)

HIPAA applies when you handle PHI (Protected Health Information). PHI is any
health information that identifies a patient: diagnosis, treatment, prescriptions,
insurance, plus 18 identifiers including name, DOB, phone, email, IP address.

**Technical safeguards HIPAA requires:**

| Safeguard | Technical Implementation |
|---|---|
| **Access controls** | Authenticate before any PHI is processed; log every access. |
| **Audit controls** | Record who accessed what PHI, when, and why. |
| **Integrity** | PHI must not be altered improperly — checksums, immutable audit logs. |
| **Transmission security** | Encrypt PHI in transit (HTTPS, TLS). |
| **Minimum necessary** | Only transmit/expose PHI needed for the specific task. |
| **Business Associate Agreement** | Every vendor that handles PHI must sign a BAA. |

**LangChain pattern mapping:**
- PHI detection → Presidio recognizer in graph entry node
- Audit controls → covered by `lc:audit` (use alongside this skill)
- Minimum necessary → PHI masking pipeline before LLM call
- BAA → AWS Bedrock (yes), Azure OpenAI (yes), Anthropic direct API (verify current status with Anthropic)

### EU AI Act (effective August 2026)

The EU AI Act classifies AI systems by risk level. Technical requirements
apply to **high-risk** systems. Low-risk systems just need transparency
(tell users they're talking to AI).

**Is your system high-risk?** Check Article 6 + Annex III:
- Employment: CV screening, hiring decisions, performance evaluation
- Credit scoring or financial services
- Healthcare: medical devices, clinical decision support
- Law enforcement, border control, justice
- Education: exam scoring, admission decisions
- Critical infrastructure management

**Technical requirements for high-risk systems:**

| Requirement | LangChain/LangGraph Pattern |
|---|---|
| **Human oversight** | `interrupt()` before all consequential decisions; never fully autonomous |
| **Logging & record keeping** | LangSmith tracing + structured audit log |
| **Accuracy testing** | LangSmith evaluation datasets + `lc:test` |
| **Transparency** | System prompt must identify the system as AI |
| **Conformity documentation** | Model cards, data governance records |

---

## Discovery Flow — Ask These 4 Questions First

Ask ALL 4 in a single message before scaffolding anything:

```
1. What region do you operate in?
   [ ] EU/EEA  [ ] UK  [ ] US  [ ] Global  [ ] Other: ___

2. Do users submit personal data that the LLM processes?
   Examples: names, emails, medical history, conversations about themselves.
   [ ] Yes  [ ] No  [ ] Unsure

3. Is this a high-risk AI system? Does it make or influence decisions about:
   [ ] Employment/HR  [ ] Credit/Finance  [ ] Healthcare  [ ] Law enforcement
   [ ] Education  [ ] None of these

4. Where is your LLM provider hosted?
   [ ] Anthropic API (US)  [ ] OpenAI (US)  [ ] AWS Bedrock (choose region)
   [ ] Azure OpenAI (choose region)  [ ] Self-hosted / Ollama
```

**Routing logic from answers:**
- EU/EEA → GDPR patterns mandatory
- US healthcare → HIPAA patterns mandatory
- High-risk category ticked → EU AI Act technical requirements (from Aug 2026)
- US-hosted LLM + EU users → data residency patterns required
- Any personal data processed → data governance patterns apply

---

## Pattern 1 — GDPR Data Minimization Node

**The concept:** Before sending a user's message to the LLM, strip any personal
data that isn't needed for the task. This satisfies GDPR's data minimization
principle and reduces the data you're responsible for under any regulation.

A LangGraph node is the perfect place to intercept data before it flows to
the LLM. Think of it as a firewall at the graph's entry point.

```python
# gdpr_minimization.py
"""
DATA MINIMIZATION NODE — GDPR Article 5(1)(c)

Concept: Personal data must be "adequate, relevant and limited to what is
necessary in relation to the purposes for which they are processed."

We implement this as a LangGraph node that strips PII from user input
before it reaches the LLM. The LLM never sees unnecessary personal data,
which means it can't accidentally store, log, or expose it.
"""

from __future__ import annotations

import os
import re
from typing import TypedDict, Annotated

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, BaseMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

# presidio-analyzer and presidio-anonymizer provide entity-level PII detection
# Install: pip install presidio-analyzer presidio-anonymizer
# Also run: python -m spacy download en_core_web_lg
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

load_dotenv()

# Initialize Presidio engines once at module level — they're expensive to create.
# AnalyzerEngine detects PII entities; AnonymizerEngine replaces them.
_analyzer = AnalyzerEngine()
_anonymizer = AnonymizerEngine()

# Define which PII entity types to strip for your use case.
# Full list: https://microsoft.github.io/presidio/supported_entities/
MINIMIZATION_ENTITIES = [
    "PERSON",           # Names
    "EMAIL_ADDRESS",    # Email addresses
    "PHONE_NUMBER",     # Phone numbers
    "CREDIT_CARD",      # Card numbers
    "US_SSN",           # Social Security Numbers
    "IP_ADDRESS",       # IP addresses
    "LOCATION",         # Addresses and places
    "DATE_TIME",        # Dates that could identify someone
]

class GraphState(TypedDict):
    """State flowing through the compliance graph.

    Concept: TypedDict defines the shape of your LangGraph state.
    Every node reads from and writes to this dict. Fields here are
    the "memory" shared between nodes.
    """
    messages: Annotated[list[BaseMessage], add_messages]
    original_input: str          # Keep original for right-of-access responses
    minimized_input: str         # What actually goes to the LLM
    pii_detected: list[str]      # Which PII types were found (for audit log)
    data_classification: str     # PUBLIC / INTERNAL / CONFIDENTIAL / PHI
    user_id: str                 # For data subject rights operations


def data_minimization_node(state: GraphState) -> dict:
    """Strip unnecessary PII from user input before LLM processing.

    This node is placed at the graph's entry point, before any LLM call.
    It satisfies GDPR Article 5(1)(c) — data minimization.

    Returns a dict with only the keys this node changes — LangGraph
    merges this with the existing state automatically.
    """
    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        None,
    )
    if last_human is None:
        return {"minimized_input": "", "pii_detected": [], "original_input": ""}

    raw_text = last_human.content
    language = "en"  # extend to detect language dynamically if needed

    # Step 1: Detect PII entities in the input text
    results = _analyzer.analyze(
        text=raw_text,
        entities=MINIMIZATION_ENTITIES,
        language=language,
    )

    detected_types = list({r.entity_type for r in results})

    # Step 2: Anonymize — replace detected entities with type placeholders
    # e.g. "John Smith" → "<PERSON>", "john@example.com" → "<EMAIL_ADDRESS>"
    anonymized = _anonymizer.anonymize(
        text=raw_text,
        analyzer_results=results,
        # OperatorConfig("replace") substitutes the entity with its type label.
        # Alternatives: "redact" (removes entirely), "hash", "encrypt"
        operators={
            entity: OperatorConfig("replace", {"new_value": f"<{entity}>"})
            for entity in MINIMIZATION_ENTITIES
        },
    )

    return {
        "original_input": raw_text,         # Kept for right-of-access
        "minimized_input": anonymized.text, # Goes to LLM
        "pii_detected": detected_types,     # Audit log entry
    }
```

---

## Pattern 2 — Data Residency: LangSmith PII Masking Tracer

**The concept:** By default, `LANGSMITH_TRACING=true` sends every LLM input
and output to LangSmith servers in the US. For EU users, this is a GDPR
Chapter V international data transfer — legally permissible but requires
documentation (Standard Contractual Clauses with LangChain/LangSmith Inc.).

**Three options ranked by privacy strength:**

| Option | Privacy | Observability | Effort |
|---|---|---|---|
| A. Self-hosted Langfuse | Highest (EU servers) | Full | Medium |
| B. `LANGSMITH_HIDE_INPUTS=true` | Medium (hides I/O) | Reduced | Minimal |
| C. PIIMaskingTracer | Medium (strips PII before send) | Good | Low |
| D. Full air-gap (Ollama + Phoenix) | Highest | Full locally | High |

### Option B — Environment Variable Approach (Quick)

```bash
# .env additions for GDPR Chapter V mitigation
# These env vars tell LangSmith NOT to send input/output content.
# Metadata and timing are still sent — check your DPA requirements.
LANGSMITH_HIDE_INPUTS=true
LANGSMITH_HIDE_OUTPUTS=true
```

```python
# Verify the masking is active at startup
import os
from dotenv import load_dotenv
load_dotenv()

def verify_langsmith_privacy_config() -> None:
    """Check that LangSmith privacy settings are active.

    Call this at app startup to fail fast if config is wrong.
    Raises RuntimeError if data residency settings aren't applied.
    """
    hide_inputs = os.getenv("LANGSMITH_HIDE_INPUTS", "false").lower() == "true"
    hide_outputs = os.getenv("LANGSMITH_HIDE_OUTPUTS", "false").lower() == "true"
    tracing = os.getenv("LANGSMITH_TRACING", "false").lower() == "true"

    if tracing and not (hide_inputs and hide_outputs):
        raise RuntimeError(
            "LangSmith tracing is enabled but LANGSMITH_HIDE_INPUTS or "
            "LANGSMITH_HIDE_OUTPUTS is not set. "
            "For GDPR Chapter V compliance, set both to 'true' in your .env, "
            "OR ensure you have a valid DPA with LangChain Inc. covering "
            "EU-to-US data transfers. See: https://docs.smith.langchain.com/privacy"
        )
```

### Option C — PIIMaskingTracer (Strip PII Before Sending)

```python
# pii_masking_tracer.py
"""
PII MASKING TRACER

Concept: LangSmith supports custom callbacks. By inserting a callback
that runs BEFORE the trace is sent, we can strip PII from the payload.
This means LangSmith receives anonymized traces — useful for debugging
without sending personal data to US servers.

This is NOT a substitute for a DPA — metadata still leaves the EU.
It does reduce the risk and sensitivity of what's transmitted.
"""

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from typing import Any

_analyzer = AnalyzerEngine()
_anonymizer = AnonymizerEngine()


def _mask_text(text: str) -> str:
    """Replace PII entities in text with type placeholders."""
    if not text or not isinstance(text, str):
        return text
    results = _analyzer.analyze(text=text, language="en")
    if not results:
        return text
    anonymized = _anonymizer.anonymize(
        text=text,
        analyzer_results=results,
        operators={
            r.entity_type: OperatorConfig(
                "replace", {"new_value": f"[{r.entity_type}]"}
            )
            for r in results
        },
    )
    return anonymized.text


class PIIMaskingCallbackHandler(BaseCallbackHandler):
    """Intercept LLM calls and mask PII before traces are recorded.

    Usage:
        from langchain_anthropic import ChatAnthropic
        from pii_masking_tracer import PIIMaskingCallbackHandler

        llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            callbacks=[PIIMaskingCallbackHandler()],
        )

    The callback fires on every LLM call. We mask inputs here so that
    if LangSmith is capturing the run, it receives masked content.
    """

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """Mask PII in prompts before they're logged."""
        for i, prompt in enumerate(prompts):
            prompts[i] = _mask_text(prompt)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list],
        **kwargs: Any,
    ) -> None:
        """Mask PII in chat messages before they're logged."""
        for message_list in messages:
            for message in message_list:
                if hasattr(message, "content") and isinstance(message.content, str):
                    message.content = _mask_text(message.content)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Mask PII in LLM outputs before they're logged."""
        for generation_list in response.generations:
            for generation in generation_list:
                if hasattr(generation, "text"):
                    generation.text = _mask_text(generation.text)
```

### Option A — Self-Hosted Langfuse (EU Data Residency)

```yaml
# docker-compose.langfuse.yml
# Deploy Langfuse in your EU infrastructure for full data residency.
#
# Concept: Langfuse is an open-source LangSmith alternative.
# When self-hosted in the EU, your trace data never leaves EU jurisdiction.
# This satisfies GDPR Chapter V without requiring SCCs with a US vendor.
#
# Prerequisites: Docker, PostgreSQL (or use the bundled one below)
# Production: replace `NEXTAUTH_SECRET` and `SALT` with strong random values.

version: "3.8"

services:
  langfuse-server:
    image: langfuse/langfuse:latest
    restart: unless-stopped
    ports:
      - "3000:3000"
    environment:
      # Generate: openssl rand -base64 32
      NEXTAUTH_SECRET: "REPLACE_WITH_STRONG_RANDOM_SECRET"
      SALT: "REPLACE_WITH_STRONG_RANDOM_SALT"
      NEXTAUTH_URL: "http://localhost:3000"  # Replace with your EU domain
      DATABASE_URL: "postgresql://langfuse:langfuse_pw@langfuse-db:5432/langfuse"
      TELEMETRY_ENABLED: "false"  # Disable telemetry for privacy
    depends_on:
      langfuse-db:
        condition: service_healthy

  langfuse-db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: langfuse
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: langfuse_pw  # Use a strong password in production
    volumes:
      - langfuse_postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  langfuse_postgres_data:
    driver: local
```

```python
# Connect LangChain to self-hosted Langfuse
# .env additions:
# LANGFUSE_HOST=http://your-eu-langfuse-server:3000
# LANGFUSE_PUBLIC_KEY=pk-lf-...
# LANGFUSE_SECRET_KEY=sk-lf-...

from langfuse.callback import CallbackHandler as LangfuseCallbackHandler
from dotenv import load_dotenv
import os

load_dotenv()

# Create the callback handler — it reads LANGFUSE_* env vars automatically
langfuse_handler = LangfuseCallbackHandler()

# Use in any LCEL chain or LangGraph graph:
# chain.invoke({"input": "..."}, config={"callbacks": [langfuse_handler]})
```

---

## Pattern 3 — GDPR Data Subject Rights: `delete_user_data()`

**The concept:** GDPR Article 17 gives users the "right to erasure" (right to
be forgotten). When a user invokes this right, you must delete their personal
data from EVERY place you store it. In a LangChain app, that's typically:

1. The LangGraph checkpointer (conversation state)
2. The vector store (embedded documents, semantic memory)
3. LangSmith traces (if you logged their data)
4. Your own audit log (redact personal fields, keep the event)

This function must be **complete** — partial deletion is a GDPR violation.

```python
# data_subject_rights.py
"""
GDPR DATA SUBJECT RIGHTS IMPLEMENTATION
Articles 15–20 of GDPR

Right of access (Art. 15)   → get_user_data()
Right to rectification (16) → update_user_data()
Right to erasure (17)       → delete_user_data()   ← most critical
Right to portability (20)   → export_user_data()

Concept: Each right maps to a function. In a real system, you'd expose
these through an authenticated admin API endpoint, not the chatbot UI.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from langsmith import Client as LangSmithClient

# These imports depend on your specific stack — adjust to match yours.
# Checkpointer: PostgresSaver, SqliteSaver, MemorySaver, etc.
# Vector store: Chroma, Pinecone, Weaviate, pgvector, etc.
from langgraph.checkpoint.postgres import PostgresSaver
from langchain_chroma import Chroma

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class ErasureResult:
    """Structured result from a right-to-erasure request.

    Concept: Always return a structured result so you can:
    a) Respond to the data subject with what was deleted
    b) Generate an audit record of the deletion event itself
    c) Identify any partial failures that need manual follow-up
    """
    user_id: str
    requested_at: str
    checkpointer_threads_deleted: int
    vector_store_docs_deleted: int
    langsmith_traces_deleted: int
    audit_log_redacted: int
    errors: list[str]

    @property
    def fully_successful(self) -> bool:
        return len(self.errors) == 0


def delete_user_data(
    user_id: str,
    checkpointer: PostgresSaver | None = None,
    vector_store: Chroma | None = None,
    langsmith_project: str | None = None,
    audit_log_redact_fn: callable | None = None,
) -> ErasureResult:
    """Complete implementation of GDPR Article 17 — Right to Erasure.

    Deletes ALL personal data for a user across every storage layer.
    Call this when a verified data subject submits an erasure request.

    IMPORTANT: You must verify the user's identity before calling this.
    This function performs irreversible deletions.

    Args:
        user_id: The application-level identifier for the data subject.
        checkpointer: LangGraph checkpointer instance. Pass None to skip.
        vector_store: LangChain vector store instance. Pass None to skip.
        langsmith_project: LangSmith project name. Pass None to skip.
        audit_log_redact_fn: Callable(user_id) that redacts personal fields
            from your audit log while keeping event records. Pass None to skip.

    Returns:
        ErasureResult with counts of deleted items and any errors.
    """
    result = ErasureResult(
        user_id=user_id,
        requested_at=datetime.now(timezone.utc).isoformat(),
        checkpointer_threads_deleted=0,
        vector_store_docs_deleted=0,
        langsmith_traces_deleted=0,
        audit_log_redacted=0,
        errors=[],
    )

    # ── Step 1: Delete LangGraph checkpointer state ──────────────────────────
    # The checkpointer stores conversation history keyed by thread_id.
    # Convention: thread_ids are prefixed with the user_id, e.g. "user_42:session_1"
    # Adjust the filtering logic to match your thread_id naming scheme.
    if checkpointer is not None:
        try:
            # List all thread IDs belonging to this user
            # PostgresSaver exposes list_threads(); other checkpointers may differ
            user_threads = [
                t for t in checkpointer.list_threads()
                if t.startswith(f"{user_id}:")
            ]
            for thread_id in user_threads:
                checkpointer.delete(thread_id)
                result.checkpointer_threads_deleted += 1
            logger.info(
                "Deleted %d checkpointer threads for user %s",
                result.checkpointer_threads_deleted, user_id
            )
        except Exception as exc:
            error_msg = f"Checkpointer deletion failed: {exc}"
            result.errors.append(error_msg)
            logger.error(error_msg)

    # ── Step 2: Delete from vector store ─────────────────────────────────────
    # Documents in a vector store may contain PII from the user's submissions.
    # Convention: store user_id in the document metadata so you can filter.
    # e.g. vectorstore.add_documents(docs, metadata={"user_id": user_id})
    if vector_store is not None:
        try:
            # Query the vector store for documents tagged with this user_id
            # Most vector stores support metadata filtering
            results = vector_store.get(where={"user_id": user_id})
            doc_ids = results.get("ids", [])
            if doc_ids:
                vector_store.delete(ids=doc_ids)
                result.vector_store_docs_deleted = len(doc_ids)
            logger.info(
                "Deleted %d vector store documents for user %s",
                result.vector_store_docs_deleted, user_id
            )
        except Exception as exc:
            error_msg = f"Vector store deletion failed: {exc}"
            result.errors.append(error_msg)
            logger.error(error_msg)

    # ── Step 3: Handle LangSmith traces ──────────────────────────────────────
    # LangSmith stores every LLM call including inputs and outputs.
    # If you tagged runs with user_id in metadata, you can delete them.
    # Note: LangSmith's deletion API has rate limits and async processing.
    # Verify deletion completed — it may take time to propagate.
    if langsmith_project is not None:
        try:
            ls_client = LangSmithClient()
            # List runs tagged with this user's ID
            # Requires: runs were created with metadata={"user_id": user_id}
            runs = list(ls_client.list_runs(
                project_name=langsmith_project,
                filter=f'has(metadata, \'{{"user_id": "{user_id}"}}\')`,
            ))
            run_ids = [str(r.id) for r in runs]
            if run_ids:
                ls_client.delete_runs(run_ids=run_ids)
                result.langsmith_traces_deleted = len(run_ids)
            logger.info(
                "Deleted %d LangSmith runs for user %s",
                result.langsmith_traces_deleted, user_id
            )
        except Exception as exc:
            error_msg = f"LangSmith trace deletion failed: {exc}"
            result.errors.append(error_msg)
            logger.error(error_msg)

    # ── Step 4: Redact audit log personal fields ──────────────────────────────
    # GDPR requires keeping records of processing activities (Art. 30),
    # but the personal data within those records must be erasable.
    # Solution: redact the personal fields, keep the event metadata.
    # e.g., replace name/email with "[REDACTED - Art.17 erasure YYYY-MM-DD]"
    if audit_log_redact_fn is not None:
        try:
            count = audit_log_redact_fn(user_id)
            result.audit_log_redacted = count
        except Exception as exc:
            error_msg = f"Audit log redaction failed: {exc}"
            result.errors.append(error_msg)
            logger.error(error_msg)

    # ── Step 5: Log the erasure event itself ──────────────────────────────────
    # You need to prove you honored the erasure request.
    # Log the event WITHOUT personal data — just the user_id pseudonym and outcome.
    logger.info(
        "GDPR Art.17 erasure completed | user_id=%s | success=%s | deleted=%s",
        user_id,
        result.fully_successful,
        asdict(result),
    )

    return result


def get_user_data(user_id: str, vector_store: Chroma | None = None) -> dict:
    """GDPR Article 15 — Right of Access.

    Returns all data held about the user as a JSON-serializable dict.
    Use this to respond to Subject Access Requests (SARs).
    """
    data: dict[str, Any] = {
        "user_id": user_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vector_store_documents": [],
    }

    if vector_store is not None:
        results = vector_store.get(where={"user_id": user_id})
        data["vector_store_documents"] = [
            {"id": doc_id, "content": doc, "metadata": meta}
            for doc_id, doc, meta in zip(
                results.get("ids", []),
                results.get("documents", []),
                results.get("metadatas", []),
            )
        ]

    return data


def export_user_data(user_id: str, vector_store: Chroma | None = None) -> str:
    """GDPR Article 20 — Right to Data Portability.

    Returns all user data as a formatted JSON string.
    Send this to the data subject in a machine-readable format.
    """
    data = get_user_data(user_id, vector_store)
    return json.dumps(data, indent=2, default=str)
```

---

## Pattern 4 — HIPAA PHI Detection and Masking Pipeline

**The concept:** In a healthcare application, Protected Health Information
(PHI) must never reach an LLM provider that hasn't signed a Business Associate
Agreement (BAA). Presidio has medical entity recognizers that detect PHI.

We implement a `PHIGuardNode` — a mandatory gateway node in the LangGraph
that checks every input before it reaches the LLM node.

```python
# phi_guard.py
"""
HIPAA PHI GUARD NODE

Concept: PHI detection is not just about PII — it's specifically about
health-related information. Presidio's medical recognizers detect:
- Medical conditions and diagnoses
- Drug names and dosages
- Medical record numbers
- Dates associated with healthcare events
- Plus all standard PII (name, DOB, SSN, phone, address)

The PHIGuardNode sits at the graph entry and has two modes:
  BLOCK mode: Reject inputs containing PHI entirely (strictest)
  MASK mode:  Anonymize PHI before forwarding to LLM (balanced)

For HIPAA, BLOCK mode is safer unless you have a BAA with your LLM provider.
"""

from __future__ import annotations

import logging
from typing import TypedDict, Literal
from dotenv import load_dotenv

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, START, END

load_dotenv()
logger = logging.getLogger(__name__)

# ── PHI Entity Types ──────────────────────────────────────────────────────────
# Standard PII entities that become PHI in healthcare context
STANDARD_PHI_ENTITIES = [
    "PERSON",            # Patient names
    "DATE_TIME",         # Dates of birth, admission, treatment
    "PHONE_NUMBER",      # Contact numbers
    "EMAIL_ADDRESS",     # Email addresses
    "US_SSN",            # Social Security Numbers
    "US_DRIVER_LICENSE", # Driver's license numbers
    "US_PASSPORT",       # Passport numbers
    "LOCATION",          # Addresses, zip codes
    "IP_ADDRESS",        # IP addresses (HIPAA identifier)
    "MEDICAL_LICENSE",   # Medical license numbers
    "URL",               # URLs that might contain identifiers
    "US_BANK_NUMBER",    # Financial account numbers
    "CREDIT_CARD",       # Credit card numbers
    "IBAN_CODE",         # Bank account numbers
]


def _build_phi_analyzer() -> AnalyzerEngine:
    """Build a Presidio analyzer with medical entity support.

    Concept: The default AnalyzerEngine handles standard PII.
    For HIPAA, we additionally need to detect medical conditions, drug names,
    and healthcare-specific patterns. The NLP engine (spaCy) powers this.
    """
    # Configure the NLP engine — en_core_web_lg is more accurate than _sm
    configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
    }
    provider = NlpEngineProvider(nlp_configuration=configuration)
    nlp_engine = provider.create_engine()

    registry = RecognizerRegistry()
    registry.load_predefined_recognizers(nlp_engine=nlp_engine)

    return AnalyzerEngine(nlp_engine=nlp_engine, registry=registry)


# Build once at module load — thread safe after initialization
_phi_analyzer = _build_phi_analyzer()
_anonymizer = AnonymizerEngine()


class HealthcareGraphState(TypedDict):
    """State for a HIPAA-compliant healthcare LangGraph application."""
    messages: list
    phi_detected: bool
    phi_entities: list[str]
    phi_masked_input: str
    original_input: str
    phi_guard_mode: Literal["block", "mask"]
    user_id: str
    access_reason: str  # Why this user is accessing PHI — for audit log


def phi_guard_node(state: HealthcareGraphState) -> dict:
    """HIPAA Safeguard: Detect PHI before any LLM processing.

    BLOCK mode: Reject the request entirely if PHI is detected.
                Use this when no BAA is in place with the LLM provider.
    MASK  mode: Anonymize PHI and continue with masked input.
                Use this when a BAA is in place but you want defense-in-depth.

    Either way, log every PHI detection event for HIPAA audit trail.
    """
    mode = state.get("phi_guard_mode", "mask")

    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        None,
    )
    if last_human is None:
        return {"phi_detected": False, "phi_entities": [], "phi_masked_input": ""}

    raw_text = last_human.content

    # Detect PHI entities
    results = _phi_analyzer.analyze(
        text=raw_text,
        entities=STANDARD_PHI_ENTITIES,
        language="en",
    )

    detected_entities = [r.entity_type for r in results]
    phi_detected = len(results) > 0

    if phi_detected:
        # HIPAA audit log entry — log THAT PHI was detected, not WHAT it was
        logger.warning(
            "PHI detected | user_id=%s | entities=%s | mode=%s | "
            "access_reason=%s",
            state.get("user_id", "unknown"),
            detected_entities,
            mode,
            state.get("access_reason", "not_specified"),
        )

    if phi_detected and mode == "block":
        # BLOCK mode: do not forward to LLM
        # Return an error message indicating PHI was detected
        block_message = AIMessage(
            content=(
                "I cannot process this request because it contains protected "
                "health information (PHI). Please remove identifying information "
                "before continuing. If you need to discuss a specific patient case, "
                "please use the secure PHI workflow."
            )
        )
        return {
            "messages": [block_message],
            "phi_detected": True,
            "phi_entities": detected_entities,
            "phi_masked_input": "",
            "original_input": raw_text,
        }

    # MASK mode: anonymize and continue
    if phi_detected:
        anonymized = _anonymizer.anonymize(
            text=raw_text,
            analyzer_results=results,
            operators={
                entity: OperatorConfig(
                    "replace", {"new_value": f"[PHI:{entity}]"}
                )
                for entity in {r.entity_type for r in results}
            },
        )
        masked_text = anonymized.text
    else:
        masked_text = raw_text

    return {
        "phi_detected": phi_detected,
        "phi_entities": detected_entities,
        "phi_masked_input": masked_text,
        "original_input": raw_text,
    }


def route_after_phi_guard(state: HealthcareGraphState) -> str:
    """Route after PHI guard: skip LLM if blocked, continue if masked.

    Concept: Conditional edges in LangGraph are routing functions.
    They return a string key that maps to the next node name.
    """
    if state.get("phi_detected") and state.get("phi_guard_mode") == "block":
        # PHI was detected and blocked — go directly to END
        # The block_message was already added by phi_guard_node
        return "blocked"
    return "proceed"


# ── Build the HIPAA-compliant graph ───────────────────────────────────────────

def build_hipaa_graph(llm_node_fn: callable) -> any:
    """Assemble a LangGraph with mandatory PHI guard at entry.

    Args:
        llm_node_fn: Your existing LLM node function. The PHI guard
                     wraps it — your node doesn't need to change.

    Concept: recursion_limit prevents infinite loops in graphs.
    Set it conservatively — 10–25 is typical for most use cases.
    """
    builder = StateGraph(HealthcareGraphState)

    builder.add_node("phi_guard", phi_guard_node)
    builder.add_node("llm_call", llm_node_fn)

    builder.add_edge(START, "phi_guard")
    builder.add_conditional_edges(
        "phi_guard",
        route_after_phi_guard,
        {"blocked": END, "proceed": "llm_call"},
    )
    builder.add_edge("llm_call", END)

    return builder.compile(
        # recursion_limit: maximum number of node executions before the graph
        # raises RecursionError. Prevents runaway loops.
        # For a linear graph like this, 5 is more than enough.
        # For agents with tool loops, use 25–50.
    )
```

---

## Pattern 5 — EU AI Act: Human Oversight Configuration

**The concept:** EU AI Act Article 14 requires high-risk AI systems to allow
human operators to "effectively oversee" the system. In LangGraph, this maps
directly to `interrupt()` — a mechanism that pauses the graph execution and
waits for a human to review and approve before continuing.

Every consequential decision in a high-risk system must pass through an
interrupt checkpoint.

```python
# eu_ai_act_oversight.py
"""
EU AI ACT — HUMAN OVERSIGHT (Article 14)

Concept: interrupt() in LangGraph pauses graph execution at a node.
The graph's state is saved in the checkpointer, and execution resumes
only when you call graph.invoke() again with the same thread_id and
a Command object that carries the human's decision.

This is the technical implementation of "human in the loop" oversight
required for high-risk AI systems under EU AI Act Article 14.

Timeline note: EU AI Act full application August 2026.
High-risk system requirements apply from that date.
"""

from __future__ import annotations

from typing import TypedDict, Literal, Any
from dotenv import load_dotenv

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command

load_dotenv()


class HighRiskAIState(TypedDict):
    """State for an EU AI Act high-risk compliant system.

    'decision_log' tracks all decisions for Art. 12 record keeping.
    'human_approved' tracks whether oversight was obtained.
    """
    messages: list[BaseMessage]
    proposed_decision: str          # The AI's proposed action
    decision_confidence: float      # AI's confidence score
    human_approved: bool            # Was human oversight obtained?
    human_reviewer_id: str          # Who approved (for audit)
    decision_log: list[dict]        # Art. 12: record keeping


def ai_reasoning_node(state: HighRiskAIState) -> dict:
    """Node that generates a proposed decision — but does NOT act on it yet.

    Concept: In a high-risk system, the AI proposes; the human disposes.
    The AI generates a recommendation with reasoning, which flows to
    the human oversight node before any action is taken.
    """
    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        temperature=0,  # Deterministic for high-stakes decisions
    )

    # Transparency requirement: AI must identify itself and explain reasoning
    system = SystemMessage(content=(
        "You are an AI assistant making a recommendation that will be reviewed "
        "by a human before any action is taken. You must:\n"
        "1. State your recommendation clearly\n"
        "2. Explain your reasoning step by step\n"
        "3. State your confidence level (0.0–1.0)\n"
        "4. Identify any uncertainty or edge cases\n\n"
        "Format: RECOMMENDATION: [action]\nCONFIDENCE: [0.0-1.0]\n"
        "REASONING: [explanation]\nUNCERTAINTY: [caveats]"
    ))

    response = llm.invoke([system] + state["messages"])

    # Parse confidence from response (simplified — use structured output in prod)
    import re
    confidence_match = re.search(r"CONFIDENCE:\s*([\d.]+)", response.content)
    confidence = float(confidence_match.group(1)) if confidence_match else 0.5

    return {
        "messages": [response],
        "proposed_decision": response.content,
        "decision_confidence": confidence,
        "human_approved": False,
    }


def human_oversight_node(state: HighRiskAIState) -> dict:
    """Mandatory human oversight checkpoint — EU AI Act Article 14.

    Concept: interrupt() pauses execution HERE. The graph serializes its
    state to the checkpointer and waits. Your application can then:
    1. Display the AI's proposed decision to a human reviewer
    2. Collect their approval/rejection via your UI
    3. Resume with: graph.invoke(Command(resume=decision), config=config)

    The human's response comes back as the return value of interrupt().
    """
    # interrupt() takes a value to show the human reviewer.
    # It returns whatever value was passed in the Command(resume=...) call.
    human_response = interrupt({
        "proposed_decision": state["proposed_decision"],
        "confidence": state["decision_confidence"],
        "instruction": (
            "Please review this AI recommendation. "
            "Respond with 'approve', 'reject', or 'modify: [your modification]'"
        ),
    })

    # Log the oversight event for EU AI Act Article 12 record keeping
    oversight_record = {
        "proposed": state["proposed_decision"],
        "confidence": state["decision_confidence"],
        "human_response": human_response,
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
    }

    approved = str(human_response).lower().startswith("approve")

    return {
        "human_approved": approved,
        "human_reviewer_id": "reviewer_id_from_auth_context",  # Replace with real auth
        "decision_log": state.get("decision_log", []) + [oversight_record],
    }


def execute_or_reject_node(state: HighRiskAIState) -> dict:
    """Execute the approved decision or explain rejection."""
    if state["human_approved"]:
        # Proceed with the approved action
        return {
            "messages": [AIMessage(
                content=f"Action approved and executed: {state['proposed_decision']}"
            )]
        }
    else:
        return {
            "messages": [AIMessage(
                content="The proposed action was rejected by the human reviewer. "
                        "No action has been taken."
            )]
        }


def build_high_risk_ai_graph() -> any:
    """Build EU AI Act Article 14 compliant high-risk AI graph.

    The graph ALWAYS pauses for human review before executing decisions.
    This is non-negotiable for high-risk AI systems from August 2026.
    """
    from langchain_core.messages import SystemMessage  # local import for clarity
    from langgraph.checkpoint.memory import MemorySaver

    builder = StateGraph(HighRiskAIState)

    builder.add_node("ai_reasoning", ai_reasoning_node)
    builder.add_node("human_oversight", human_oversight_node)
    builder.add_node("execute_or_reject", execute_or_reject_node)

    builder.add_edge(START, "ai_reasoning")
    builder.add_edge("ai_reasoning", "human_oversight")
    builder.add_edge("human_oversight", "execute_or_reject")
    builder.add_edge("execute_or_reject", END)

    # MemorySaver checkpointer is required for interrupt() to work.
    # In production, use PostgresSaver or SqliteSaver for persistence.
    checkpointer = MemorySaver()

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_oversight"],  # Explicit interrupt declaration
    )


# ── Usage example ─────────────────────────────────────────────────────────────
"""
graph = build_high_risk_ai_graph()
config = {"configurable": {"thread_id": "case-001"}, "recursion_limit": 10}

# Step 1: Run graph — it will pause at human_oversight
result = graph.invoke(
    {"messages": [HumanMessage(content="Should we approve this loan application?")]},
    config=config,
)
# result["__interrupt__"] contains the data shown to the reviewer

# Step 2: Resume after human reviews
final = graph.invoke(
    Command(resume="approve"),
    config=config,
)
"""
```

---

## Pattern 6 — Data Classification Node

**The concept:** Before data flows anywhere in your system, classify it.
Classification determines which processing path it takes — encrypted storage,
plain storage, or rejection. This implements the data governance principle of
"know what you have before you process it."

```python
# data_classification.py
"""
DATA CLASSIFICATION NODE

Four classification levels (adapt to your organization's policy):
  PUBLIC       — No restrictions. Can go anywhere.
  INTERNAL     — Internal use only. Not for external sharing.
  CONFIDENTIAL — Business sensitive. Restricted access, encrypted storage.
  PHI          — Protected Health Information. HIPAA controls apply.

Concept: Classification happens at graph entry as a routing decision.
Different downstream paths enforce different security controls.
This is "data governance at the architecture level" — the graph structure
itself enforces policy, not just application code.
"""

from __future__ import annotations

import re
import logging
from typing import TypedDict, Literal, Annotated
from dotenv import load_dotenv

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

load_dotenv()
logger = logging.getLogger(__name__)

DataClassification = Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "PHI"]


class DataGovernanceState(TypedDict):
    """State for a data-governance-aware LangGraph application."""
    messages: Annotated[list[BaseMessage], add_messages]
    classification: DataClassification
    classification_confidence: float
    classification_reason: str
    user_id: str
    source_doc_ids: list[str]    # For RAG data lineage tracking


# Regex patterns for rule-based classification (fast, no LLM cost)
# These run BEFORE the LLM classifier — catch obvious cases instantly.
_RULE_BASED_PATTERNS: dict[DataClassification, list[re.Pattern]] = {
    "PHI": [
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                   # SSN pattern
        re.compile(r"\bMRN[:\s#]*\d+\b", re.IGNORECASE),         # Medical record number
        re.compile(r"\bdiagnos(?:is|ed)\b", re.IGNORECASE),       # Diagnosis language
        re.compile(r"\bprescri(?:be|ption|bed)\b", re.IGNORECASE), # Prescription language
        re.compile(r"\bpatient\s+(?:name|id|number)\b", re.IGNORECASE),
    ],
    "CONFIDENTIAL": [
        re.compile(r"\bpassword\b", re.IGNORECASE),
        re.compile(r"\bapi[_\s]?key\b", re.IGNORECASE),
        re.compile(r"\bsalary\b|\bcompensation\b", re.IGNORECASE),
        re.compile(r"\bconfidential\b|\bproprietary\b", re.IGNORECASE),
    ],
}


def classify_data_node(state: DataGovernanceState) -> dict:
    """Classify incoming data before processing.

    Two-stage classification:
    1. Rule-based regex patterns (fast, no LLM cost) for obvious cases
    2. LLM-based classification for ambiguous cases

    Concept: Use cheap rules first, expensive LLMs only when needed.
    This is the "filter-then-classify" pattern for cost efficiency.
    """
    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        None,
    )
    if last_human is None:
        return {"classification": "PUBLIC", "classification_confidence": 1.0,
                "classification_reason": "No input to classify"}

    text = last_human.content

    # Stage 1: Rule-based classification (O(n) regex, very fast)
    for classification, patterns in _RULE_BASED_PATTERNS.items():
        for pattern in patterns:
            if pattern.search(text):
                logger.info(
                    "Rule-based classification: %s | user_id=%s | pattern=%s",
                    classification, state.get("user_id"), pattern.pattern
                )
                return {
                    "classification": classification,
                    "classification_confidence": 0.95,
                    "classification_reason": f"Matched rule pattern: {pattern.pattern}",
                }

    # Stage 2: LLM-based classification for ambiguous inputs
    # Use a cheap/fast model for classification — it's a simple task
    classifier_llm = ChatAnthropic(model="claude-haiku-4-5", temperature=0)

    classification_prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a data classification expert. Classify the input text
into exactly one of these categories and respond with ONLY the category name:

PUBLIC: General information, no personal or sensitive data
INTERNAL: Internal business information, not for external sharing
CONFIDENTIAL: Business sensitive, personal data not health-related
PHI: Protected Health Information — any health/medical data about a person

Respond with ONLY the category name, nothing else."""),
        ("human", "{text}"),
    ])

    # LCEL pipe syntax: prompt | llm | parser
    # Each | chains the output of the left into the input of the right.
    classification_chain = classification_prompt | classifier_llm | StrOutputParser()

    try:
        raw_result = classification_chain.invoke({"text": text[:500]})  # Limit input size
        category = raw_result.strip().upper()

        if category not in ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "PHI"):
            category = "INTERNAL"  # Safe default if LLM returns unexpected value

        return {
            "classification": category,
            "classification_confidence": 0.85,
            "classification_reason": "LLM classification",
        }
    except Exception as exc:
        logger.error("Classification LLM failed: %s — defaulting to CONFIDENTIAL", exc)
        # Fail-safe: default to CONFIDENTIAL if classification fails
        # Never default to PUBLIC on error — that would be a security failure
        return {
            "classification": "CONFIDENTIAL",
            "classification_confidence": 0.0,
            "classification_reason": f"Classification error — safe default applied: {exc}",
        }


def route_by_classification(state: DataGovernanceState) -> str:
    """Route to different processing paths based on data classification.

    Concept: Conditional edges in LangGraph are just Python functions
    returning the name of the next node. Your routing logic can be
    as simple or complex as needed.
    """
    classification = state.get("classification", "CONFIDENTIAL")
    routing_map = {
        "PUBLIC": "standard_processing",
        "INTERNAL": "standard_processing",
        "CONFIDENTIAL": "encrypted_processing",
        "PHI": "phi_processing",
    }
    return routing_map.get(classification, "encrypted_processing")
```

---

## Pattern 7 — Privacy-Preserving RAG

**The concept:** When users query a RAG system, their query and the retrieved
documents may contain PII. Privacy-preserving RAG has three layers:

1. **Ingest-time**: Strip PII from documents before embedding
2. **Query-time**: Isolate queries by user namespace, audit retrievals
3. **Response-time**: Strip PII from retrieved context before LLM sees it

```python
# privacy_rag.py
"""
PRIVACY-PRESERVING RAG

Concept: A RAG (Retrieval Augmented Generation) pipeline has multiple
data flows, each of which can leak PII:

INGEST PATH: Document → Embed → Vector Store
  Risk: PII embedded into vectors, reconstructible via similarity search

QUERY PATH: User query → Search → Retrieved chunks → LLM
  Risk: One user's query retrieving another user's private documents
  Risk: User query containing PII sent to external embedding API

RESPONSE PATH: Retrieved chunks → Prompt → LLM → Response
  Risk: LLM response containing PII from retrieved chunks

We address all three with namespace isolation, PII stripping, and
audit logging of all retrievals.
"""

from __future__ import annotations

import logging
from typing import TypedDict, Annotated
from dotenv import load_dotenv

from langchain_anthropic import ChatAnthropic
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings  # Local embeddings — no API call
from langchain_core.documents import Document
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

load_dotenv()
logger = logging.getLogger(__name__)

_analyzer = AnalyzerEngine()
_anonymizer = AnonymizerEngine()

# LOCAL EMBEDDINGS: Using HuggingFace sentence transformers means
# documents are embedded locally — no text leaves your infrastructure.
# This satisfies GDPR data minimization for the embedding step.
# Trade-off: slightly lower quality than OpenAI/Anthropic embeddings.
_local_embeddings = HuggingFaceEmbeddings(
    model_name="sentence-transformers/all-MiniLM-L6-v2"
)


def strip_pii_from_document(doc: Document) -> Document:
    """Remove PII from a document before embedding.

    Call during the ingest pipeline, before adding to vector store.
    Preserves document structure and metadata while anonymizing content.

    IMPORTANT: Store the original document securely elsewhere if you
    need to honor Right of Access requests. The stripped version in
    the vector store cannot be 'un-stripped'.
    """
    results = _analyzer.analyze(text=doc.page_content, language="en")
    if not results:
        return doc

    anonymized = _anonymizer.anonymize(
        text=doc.page_content,
        analyzer_results=results,
        operators={
            r.entity_type: OperatorConfig(
                "replace", {"new_value": f"[{r.entity_type}]"}
            )
            for r in results
        },
    )
    return Document(
        page_content=anonymized.text,
        metadata={
            **doc.metadata,
            "pii_stripped": True,
            "original_entity_types": list({r.entity_type for r in results}),
        }
    )


def ingest_documents_with_pii_stripping(
    documents: list[Document],
    vector_store: Chroma,
    user_id: str,
) -> list[str]:
    """Ingest documents into the vector store with PII stripped.

    Tags each document with:
    - user_id: for namespace isolation and right-to-erasure
    - source_doc_id: for data lineage in RAG citations
    """
    stripped_docs = []
    for doc in documents:
        stripped = strip_pii_from_document(doc)
        # Add namespace metadata for per-user isolation
        stripped.metadata.update({
            "user_id": user_id,
            "source_doc_id": doc.metadata.get("source_doc_id", doc.metadata.get("source", "unknown")),
        })
        stripped_docs.append(stripped)

    doc_ids = vector_store.add_documents(stripped_docs)
    logger.info("Ingested %d documents for user %s", len(doc_ids), user_id)
    return doc_ids


class PrivacyRAGState(TypedDict):
    """State for privacy-preserving RAG graph."""
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    retrieved_docs: list[dict]      # Retrieved chunks with lineage
    source_doc_ids: list[str]       # For data lineage attribution


def privacy_preserving_retrieval_node(
    vector_store: Chroma,
) -> callable:
    """Factory function that creates a retrieval node with user namespace isolation.

    Concept: We use a factory because the retrieval node needs access to the
    vector_store object, but LangGraph nodes only receive `state`. Factories
    close over the dependencies — a common LangGraph pattern.
    """
    def retrieval_node(state: PrivacyRAGState) -> dict:
        """Retrieve documents filtered to this user's namespace only.

        User A cannot retrieve User B's documents, even if semantically similar.
        This is per-user namespace isolation — critical for multi-tenant RAG.
        """
        last_human = next(
            (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
            None,
        )
        if last_human is None:
            return {"retrieved_docs": [], "source_doc_ids": []}

        query = last_human.content
        user_id = state["user_id"]

        # Filter retrieval to this user's namespace
        # where clause varies by vector store — this is Chroma syntax
        results = vector_store.similarity_search(
            query=query,
            k=4,
            filter={"user_id": user_id},  # NAMESPACE ISOLATION: only this user's docs
        )

        # Audit every retrieval: WHO queried WHAT documents and WHEN
        source_ids = [doc.metadata.get("source_doc_id", "unknown") for doc in results]
        logger.info(
            "RAG retrieval | user_id=%s | query_length=%d | docs_retrieved=%d | sources=%s",
            user_id, len(query), len(results), source_ids
        )

        retrieved = [
            {
                "content": doc.page_content,
                "source_doc_id": doc.metadata.get("source_doc_id", "unknown"),
                "metadata": doc.metadata,
            }
            for doc in results
        ]

        return {
            "retrieved_docs": retrieved,
            "source_doc_ids": source_ids,
        }

    return retrieval_node


def privacy_rag_generation_node(state: PrivacyRAGState) -> dict:
    """Generate response using retrieved context, citing sources for data lineage.

    Data lineage: every RAG response must cite which source documents
    contributed to the answer. This satisfies:
    - GDPR: users can request deletion of source documents
    - Audit: you can trace which data influenced which output
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

    # Build context string with source attribution
    context_parts = []
    for doc in state.get("retrieved_docs", []):
        context_parts.append(
            f"[Source: {doc['source_doc_id']}]\n{doc['content']}"
        )
    context = "\n\n---\n\n".join(context_parts)

    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        None,
    )
    query = last_human.content if last_human else ""

    prompt = ChatPromptTemplate.from_messages([
        ("system", """Answer the user's question using the provided context.
Always cite your sources using the [Source: ...] references.
If the context doesn't contain relevant information, say so rather than guessing.
Never fabricate citations."""),
        ("human", "Context:\n{context}\n\nQuestion: {question}"),
    ])

    chain = prompt | llm
    response = chain.invoke({"context": context, "question": query})

    return {"messages": [response]}
```

---

## Compliance Checklist Generator

After the discovery questions, generate a tailored `COMPLIANCE_CHECKLIST.md`.
The checklist below is the template — Claude fills in checked/unchecked items
based on the user's answers to the 4 discovery questions.

**Instructions to Claude:** When generating the checklist, use `- [x]` for
items already addressed by patterns scaffolded in this session, `- [ ]` for
items still needed, and `- [N/A]` for items that don't apply given the user's
answers.

```markdown
# Compliance Checklist

> NOTE: This checklist covers technical implementation only.
> It is NOT a legal compliance certification.
> Consult qualified legal counsel before deploying in regulated environments.

Generated: [DATE]
Region: [FROM ANSWER 1]
Regulations: [FROM ANSWERS 1-4]

---

## GDPR (applies if: EU/EEA users or processing EU personal data)

### Data Processing Principles (Article 5)
- [ ] Data minimization node implemented (PIIMaskingNode at graph entry)
- [ ] Purpose limitation documented — data used only for stated purpose
- [ ] Storage limitation: conversation memory has TTL or per-user deletion
- [ ] Vector store documents tagged with user_id for deletability
- [ ] Lawful basis documented (consent / contract / legitimate interest / other)

### Data Subject Rights (Articles 15–22)
- [ ] Right of access: get_user_data() implemented
- [ ] Right to rectification: mechanism to update incorrect stored data
- [ ] Right to erasure: delete_user_data() implemented and tested
- [ ] Right to portability: export_user_data() returns JSON
- [ ] Rights request process documented (how users submit requests)
- [ ] Response time SLA established (GDPR requires 1 month)

### International Data Transfers (Chapter V)
- [ ] LLM provider data residency documented
- [ ] If US-based provider + EU users: SCCs or adequacy decision in place
- [ ] LangSmith data residency option selected:
  - [ ] Option A: Self-hosted Langfuse in EU
  - [ ] Option B: LANGSMITH_HIDE_INPUTS + LANGSMITH_HIDE_OUTPUTS=true
  - [ ] Option C: PIIMaskingCallbackHandler active
  - [ ] Option D: Full air-gap deployment
- [ ] Data Processing Agreement (DPA) signed with LangSmith/LangChain Inc.
- [ ] DPA signed with LLM provider (Anthropic / OpenAI / AWS / Azure)

### Records of Processing Activities (Article 30)
- [ ] Processing activity register maintained
- [ ] Data flows documented (user → app → LLM → storage)
- [ ] Retention periods documented for each data type
- [ ] LangSmith run metadata includes retention_days tag

---

## HIPAA (applies if: US healthcare, PHI processing)

### Technical Safeguards
- [ ] PHIGuardNode implemented at graph entry
- [ ] PHI detection mode configured: block / mask
- [ ] Minimum necessary standard: only relevant PHI passed to LLM
- [ ] Encryption at rest for PHI-containing state (pgcrypto or equivalent)
- [ ] TLS for all PHI in transit (HTTPS endpoints only)
- [ ] Access controls: authentication required before PHI access
- [ ] Session timeout configured

### Audit Controls
- [ ] Audit log captures: user_id, action, PHI entity types detected, timestamp
- [ ] Audit log is append-only and tamper-evident
- [ ] lc:audit skill patterns implemented alongside this skill
- [ ] Audit log retention >= 6 years (HIPAA requirement)

### Business Associate Agreements
- [ ] BAA signed with LLM provider:
  - [ ] AWS Bedrock (BAA available — check AWS HIPAA eligibility)
  - [ ] Azure OpenAI (BAA available — check Azure compliance docs)
  - [ ] Anthropic API (verify current BAA availability with Anthropic)
  - [ ] OpenAI API (verify current BAA availability with OpenAI)
- [ ] BAA signed with LangSmith/LangChain Inc. (if using LangSmith)
- [ ] BAA signed with vector store provider (if cloud-hosted)
- [ ] BAA signed with checkpointer database provider (if cloud-hosted)

---

## EU AI Act (applies if: deploying in EU, high-risk category)

### Risk Classification
- [ ] Risk classification documented (prohibited / high-risk / limited-risk / minimal)
- [ ] If high-risk: Annex III category identified

### High-Risk Technical Requirements (Article 10–15)
- [ ] Human oversight: interrupt() before all consequential decisions
- [ ] Logging: every decision logged with reasoning and human approval
- [ ] Accuracy testing: LangSmith evaluation dataset established
- [ ] lc:test evaluation framework implemented
- [ ] Transparency: system prompt identifies AI to users
- [ ] Model card maintained with:
  - [ ] Intended purpose documented
  - [ ] Known limitations documented
  - [ ] Training data sources documented (for fine-tuned models)
  - [ ] Performance metrics on evaluation dataset

### Record Keeping (Article 12)
- [ ] Decision log maintained for all consequential AI decisions
- [ ] Human reviewer ID recorded for each oversight event
- [ ] Log retention period documented

---

## Data Governance (universal best practices)

### Data Classification
- [ ] classify_data_node implemented at graph entry
- [ ] Classification levels defined: PUBLIC / INTERNAL / CONFIDENTIAL / PHI
- [ ] Routing by classification: encrypted path for CONFIDENTIAL+
- [ ] Classification decisions logged

### Privacy-Preserving RAG
- [ ] PII stripped from documents before embedding (strip_pii_from_document)
- [ ] Per-user namespace isolation in vector store
- [ ] All retrievals audited: user_id + source_doc_ids + timestamp
- [ ] Source citations in RAG responses for data lineage
- [ ] Local embeddings used (or API provider DPA in place)

### Incident Response
- [ ] GDPR breach notification procedure (72 hours to supervisory authority)
- [ ] HIPAA breach notification procedure (60 days to HHS)
- [ ] Internal incident log maintained
- [ ] Contact details for DPO (Data Protection Officer) if required

---

## Next Steps

1. Legal review of this checklist with qualified counsel
2. Privacy Impact Assessment (GDPR Art. 35) if high-risk processing
3. Register as high-risk AI system with EU authority (if applicable from Aug 2026)
4. Staff training on data handling procedures
5. Annual compliance review scheduled
```

---

## Scaffolding Summary — What Gets Created

When this skill scaffolds code, it creates the following files:

```
your_project/
├── compliance/
│   ├── __init__.py
│   ├── gdpr_minimization.py      # Pattern 1 — Data minimization node
│   ├── data_residency.py         # Pattern 2 — LangSmith + Langfuse options
│   ├── data_subject_rights.py    # Pattern 3 — delete/get/export user data
│   ├── phi_guard.py              # Pattern 4 — HIPAA PHI detection
│   ├── eu_ai_act_oversight.py    # Pattern 5 — Human oversight with interrupt()
│   ├── data_classification.py   # Pattern 6 — Data classification node
│   └── privacy_rag.py           # Pattern 7 — Privacy-preserving RAG
├── docker-compose.langfuse.yml   # Pattern 2A — Self-hosted Langfuse
└── COMPLIANCE_CHECKLIST.md       # Generated from discovery answers
```

**Dependencies to add to `requirements.txt`:**
```
presidio-analyzer>=2.2
presidio-anonymizer>=2.2
# Run after pip install: python -m spacy download en_core_web_lg
langfuse>=2.0           # If using Option A (self-hosted Langfuse)
```

---

## Concept Index — What You Learned

| Concept | First appeared in | What it is |
|---|---|---|
| GDPR data minimization | Pattern 1 | Strip PII before LLM call — Art. 5(1)(c) |
| Presidio AnalyzerEngine | Pattern 1 | Detects PII/PHI entity types in text |
| Presidio AnonymizerEngine | Pattern 1 | Replaces detected entities with placeholders |
| LangSmith HIDE_INPUTS | Pattern 2B | Env var to suppress I/O from traces |
| LangSmith callbacks | Pattern 2C | Intercept and modify trace data before sending |
| Self-hosted Langfuse | Pattern 2A | EU-hosted LangSmith alternative |
| delete_user_data() | Pattern 3 | GDPR Art. 17 complete erasure across all stores |
| PHIGuardNode | Pattern 4 | HIPAA gate: detect/block/mask PHI before LLM |
| interrupt() | Pattern 5 | Pause graph execution for human review |
| Command(resume=...) | Pattern 5 | Resume a paused graph with human's decision |
| Conditional edges | Patterns 2,6 | Route to different nodes based on state |
| Factory node pattern | Pattern 7 | Close over dependencies the node can't receive in state |
| Per-user namespace isolation | Pattern 7 | filter by user_id in vector store queries |
| Data lineage | Pattern 7 | source_doc_id in every RAG citation |
| Local embeddings | Pattern 7 | Embed without sending text to external API |
| recursion_limit | Pattern 5 | Max node executions before RecursionError |
| LCEL pipe syntax | Pattern 6 | `prompt | llm | parser` chains outputs left-to-right |

---

## Transitions

After this skill completes, suggest:

- **`lc:audit`** — Implement the full audit log referenced throughout this skill (PHI access log, decision log, erasure events)
- **`lc:test`** — Set up LangSmith evaluation datasets for EU AI Act accuracy documentation
- **`lc:trace`** — Configure LangSmith tracing with the privacy options from Pattern 2
- **`lc:memory`** — Implement storage-limited conversation memory with per-user deletion hooks

---

*This skill is part of the langchain-lab plugin. Technical patterns only — not legal advice.*
