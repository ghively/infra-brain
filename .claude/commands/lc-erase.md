---
description: >
  Generate a GDPR Article 17 right-to-erasure implementation for a specific
  user. Scans the project for active data stores (PostgresSaver, vector stores,
  LangSmith), emits erase_user.py with a complete delete_user_data() function,
  generates the erasure_audit DDL (immutable proof erasure occurred), and
  shows a 30-day compliance deadline reminder.
argument-hint: <user_id>
allowed-tools: Read, Glob, Grep, Write
---

You are a GDPR compliance engineer. Execute every step below in order.
Do not skip or reorder steps.

> **LEGAL DISCLAIMER**
> This command generates technical implementation code only. It is NOT legal
> advice. Always engage qualified legal counsel before deploying erasure
> workflows in production. GDPR enforcement varies by jurisdiction and fact
> pattern.

---

## Step 1 — Parse the User ID

Extract `user_id` from `$ARGUMENTS`. If blank, ask:

```
Which user_id should be erased? (e.g. "user_42", "usr_abc123")
```

Do not proceed until you have a non-empty user_id string. Store it as
`TARGET_USER_ID` for the rest of this command.

---

## Step 2 — Confirmation Gate

Print exactly this block (substituting the real `TARGET_USER_ID`):

```
⚠️  GDPR Article 17 — Right to Erasure

This will generate code to delete ALL data for user: {TARGET_USER_ID}

Data stores that will be targeted (detected in Step 3):
  • PostgresSaver checkpoints
  • Vector store documents (filtered by user_id metadata)
  • LangSmith runs (filtered by user_id metadata)
  • Audit log: erasure EVENT will be INSERTED (personal data rows are NOT deleted)

The generated code is IRREVERSIBLE when run. The erasure audit record
itself is retained permanently as required by law.

Proceed? [y/N]
```

Wait for explicit confirmation. If the user types anything other than `y`
or `yes` (case-insensitive), print "Erasure cancelled." and stop.

---

## Step 3 — Detect Data Stores

Scan the project to discover which stores are in use. Run ALL of the
following Grep searches in parallel. Record which ones return hits.

| Signal to detect | Grep pattern | Store flag |
|---|---|---|
| PostgresSaver checkpointer | `PostgresSaver\|AsyncPostgresSaver` | `HAS_POSTGRES_SAVER` |
| Chroma vector store | `from langchain_chroma\|Chroma(` | `HAS_CHROMA` |
| Pinecone vector store | `from langchain_pinecone\|PineconeVectorStore` | `HAS_PINECONE` |
| Qdrant vector store | `from langchain_qdrant\|QdrantVectorStore\|Qdrant(` | `HAS_QDRANT` |
| pgvector store | `PGVector\|from langchain_postgres` | `HAS_PGVECTOR` |
| LangSmith tracing | `LANGSMITH_TRACING\|langsmith` | `HAS_LANGSMITH` |
| Any vector store (fallback) | `vectorstore\|vector_store\|VectorStore` | `HAS_VECTOR_GENERIC` |

After scanning, print a detection summary:

```
Detected data stores:
  [✓ or ✗] PostgresSaver checkpoints
  [✓ or ✗] Chroma vector store
  [✓ or ✗] Pinecone vector store
  [✓ or ✗] Qdrant vector store
  [✓ or ✗] pgvector store
  [✓ or ✗] LangSmith tracing
```

**If nothing is detected:** warn the user that no LangChain data stores
were found and ask whether to generate a template anyway. Generate it if
they confirm — use placeholder comments for each section.

---

## Step 4 — Generate erase_user.py

Write the file to `compliance/erase_user.py`.

The file must contain:

1. A module docstring citing GDPR Article 17
2. All imports
3. `load_dotenv()` at module top
4. The `ErasureResult` Pydantic v2 model
5. The `delete_user_data()` async function covering every detected store
6. A `__main__` block for CLI invocation

Use the exact template below. For each `# CONDITIONAL BLOCK` section,
include the block if the corresponding store flag was set in Step 3,
comment it out with a `# NOT DETECTED IN PROJECT — enable if needed`
header if it was not detected.

```python
"""
compliance/erase_user.py
GDPR Article 17 — Right to Erasure (Right to Be Forgotten)

Deletes all personal data for a specific user_id across every data store
used by this application. Call this function when a verified data subject
submits an erasure request through your admin API.

LEGAL NOTE: This code is technical scaffolding only. Consult qualified
legal counsel before deploying in production. Identity verification of
the requestor is YOUR responsibility before calling this function.

Deadline: GDPR requires erasure to be completed within 30 days of the
verified request. See the 30-day reminder printed at the end of this module.

References:
  GDPR Article 17: https://gdpr-info.eu/art-17-gdpr/
  GDPR Article 5(1)(e): Storage limitation principle
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
logger = logging.getLogger(__name__)


# ── Result model ─────────────────────────────────────────────────────────────

class ErasureResult(BaseModel):
    """Structured result returned after an Article 17 erasure operation.

    Every field is required. Return this to your admin API so the caller
    can confirm what was deleted and store the erasure_record_id for
    the 30-day compliance window.
    """

    status: str = Field(
        description="'completed', 'partial', or 'failed'",
    )
    user_id: str = Field(
        description="The user_id that was erased.",
    )
    records_deleted: dict[str, int] = Field(
        description=(
            "Count of records deleted per store. "
            "Keys: 'checkpoints', 'vector_docs', 'langsmith_runs', "
            "'audit_log_rows_redacted'."
        ),
        default_factory=dict,
    )
    timestamp: str = Field(
        description="ISO-8601 UTC timestamp when erasure was initiated.",
    )
    erasure_record_id: str = Field(
        description=(
            "UUID of the immutable erasure audit record. "
            "This record MUST be retained — it is legal proof that erasure occurred."
        ),
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal errors encountered. Populated if status == 'partial'.",
    )

    @property
    def fully_successful(self) -> bool:
        return self.status == "completed" and len(self.errors) == 0


# ── Main erasure function ─────────────────────────────────────────────────────

async def delete_user_data(user_id: str) -> ErasureResult:
    """GDPR Article 17 — delete all personal data for user_id.

    IMPORTANT: Verify the requestor's identity BEFORE calling this function.
    This operation is irreversible. The function does NOT perform identity
    verification — that is the caller's responsibility.

    Thread naming convention assumed:
        Checkpointer thread IDs follow the pattern:
            tenant:<tenant_id>:user:<user_id>:<anything>
        Adjust THREAD_ID_PATTERN below to match your application's scheme.

    Args:
        user_id: The application-level identifier for the data subject.

    Returns:
        ErasureResult with counts, timestamp, and erasure_record_id.
    """
    erasure_record_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    records_deleted: dict[str, int] = {
        "checkpoints": 0,
        "vector_docs": 0,
        "langsmith_runs": 0,
        "audit_log_rows_redacted": 0,
    }
    errors: list[str] = []

    logger.info(
        "GDPR Art.17 erasure STARTED | user_id=%s | erasure_record_id=%s",
        user_id,
        erasure_record_id,
    )

    # ── CONDITIONAL BLOCK: PostgresSaver checkpoints ──────────────────────────
    # Deletes all LangGraph conversation state for this user.
    # Thread IDs matching tenant:*:user:{user_id}:* are deleted via raw SQL.
    # Adjust THREAD_ID_PATTERN to match your naming scheme.
    #
    # HAS_POSTGRES_SAVER = {HAS_POSTGRES_SAVER}
    try:
        import asyncpg  # pip install asyncpg

        THREAD_ID_PATTERN = f"tenant:%:user:{user_id}:%"
        DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")

        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL environment variable is not set. "
                "Set it to your PostgreSQL connection string."
            )

        conn = await asyncpg.connect(DATABASE_URL)
        try:
            # checkpoints table: thread_id identifies the conversation
            result = await conn.execute(
                "DELETE FROM checkpoints WHERE thread_id LIKE $1",
                THREAD_ID_PATTERN,
            )
            # writes table (LangGraph >= 1.x stores writes separately)
            await conn.execute(
                "DELETE FROM checkpoint_writes WHERE thread_id LIKE $1",
                THREAD_ID_PATTERN,
            )
            # checkpoint_migrations table is schema-only, skip
            deleted_count = int(result.split()[-1]) if result else 0
            records_deleted["checkpoints"] = deleted_count
            logger.info(
                "Deleted %d checkpoint rows for user_id=%s (pattern=%s)",
                deleted_count,
                user_id,
                THREAD_ID_PATTERN,
            )
        finally:
            await conn.close()

    except Exception as exc:
        msg = f"Checkpoint deletion failed: {exc}"
        errors.append(msg)
        logger.error(msg)
    # ── END CONDITIONAL BLOCK: PostgresSaver ─────────────────────────────────

    # ── CONDITIONAL BLOCK: Vector store ──────────────────────────────────────
    # Deletes all documents whose metadata contains user_id == this user.
    # Documents must have been ingested with metadata={"user_id": user_id}.
    # Adapt the vector store client import to match your project.
    #
    # HAS_VECTOR_STORE = {HAS_CHROMA or HAS_PINECONE or HAS_QDRANT or HAS_PGVECTOR or HAS_VECTOR_GENERIC}
    try:
        # ── Chroma ────────────────────────────────────────────────────────────
        # Uncomment the block that matches your vector store.
        # Only one block should be active at a time.

        from langchain_chroma import Chroma  # noqa: F401
        from langchain_huggingface import HuggingFaceEmbeddings  # noqa: F401

        CHROMA_PERSIST_DIR = os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db")
        CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "default")

        embeddings = HuggingFaceEmbeddings(
            model_name="sentence-transformers/all-MiniLM-L6-v2"
        )
        vector_store = Chroma(
            collection_name=CHROMA_COLLECTION,
            embedding_function=embeddings,
            persist_directory=CHROMA_PERSIST_DIR,
        )
        results = vector_store.get(where={"user_id": user_id})
        doc_ids: list[str] = results.get("ids", [])
        if doc_ids:
            vector_store.delete(ids=doc_ids)
        records_deleted["vector_docs"] = len(doc_ids)
        logger.info(
            "Deleted %d vector documents for user_id=%s",
            len(doc_ids),
            user_id,
        )

        # ── Pinecone (alternative) ────────────────────────────────────────────
        # from langchain_pinecone import PineconeVectorStore
        # from pinecone import Pinecone
        #
        # pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        # index = pc.Index(os.environ["PINECONE_INDEX_NAME"])
        # # Pinecone supports metadata filter deletes
        # index.delete(filter={"user_id": {"$eq": user_id}})
        # # Count is not returned by Pinecone delete — use describe_index_stats
        # records_deleted["vector_docs"] = -1  # unknown; use -1 as sentinel

        # ── Qdrant (alternative) ──────────────────────────────────────────────
        # from qdrant_client import QdrantClient
        # from qdrant_client.models import Filter, FieldCondition, MatchValue
        #
        # client = QdrantClient(url=os.environ.get("QDRANT_URL", "http://localhost:6333"))
        # collection = os.environ.get("QDRANT_COLLECTION", "default")
        # result = client.delete(
        #     collection_name=collection,
        #     points_selector=Filter(
        #         must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
        #     ),
        # )
        # records_deleted["vector_docs"] = result.deleted or 0

        # ── pgvector (alternative) ────────────────────────────────────────────
        # import asyncpg
        # conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        # result = await conn.execute(
        #     "DELETE FROM langchain_pg_embedding WHERE cmetadata->>'user_id' = $1",
        #     user_id,
        # )
        # records_deleted["vector_docs"] = int(result.split()[-1])
        # await conn.close()

    except Exception as exc:
        msg = f"Vector store deletion failed: {exc}"
        errors.append(msg)
        logger.error(msg)
    # ── END CONDITIONAL BLOCK: Vector store ──────────────────────────────────

    # ── CONDITIONAL BLOCK: LangSmith traces ──────────────────────────────────
    # Deletes LangSmith runs tagged with metadata.user_id == this user.
    # Runs must have been created with metadata={"user_id": user_id}.
    #
    # IMPORTANT: LangSmith deletion is asynchronous — propagation may take
    # minutes. Verify deletion in the LangSmith UI after running this function.
    # Rate limits apply; large user histories may require batching.
    #
    # HAS_LANGSMITH = {HAS_LANGSMITH}
    try:
        from langsmith import Client as LangSmithClient  # pip install langsmith

        LANGSMITH_PROJECT = os.environ.get("LANGSMITH_PROJECT", "langchain-lab")

        ls_client = LangSmithClient()
        runs = list(
            ls_client.list_runs(
                project_name=LANGSMITH_PROJECT,
                filter=f'has(metadata, \'{{"user_id": "{user_id}"}}\' )',
            )
        )
        run_ids = [str(r.id) for r in runs]

        if run_ids:
            # Batch deletes: LangSmith recommends batches of 100
            BATCH_SIZE = 100
            for i in range(0, len(run_ids), BATCH_SIZE):
                batch = run_ids[i : i + BATCH_SIZE]
                ls_client.delete_runs(run_ids=batch)

        records_deleted["langsmith_runs"] = len(run_ids)
        logger.info(
            "Deleted %d LangSmith runs for user_id=%s (project=%s)",
            len(run_ids),
            user_id,
            LANGSMITH_PROJECT,
        )

    except Exception as exc:
        msg = f"LangSmith trace deletion failed: {exc}"
        errors.append(msg)
        logger.error(msg)
    # ── END CONDITIONAL BLOCK: LangSmith ─────────────────────────────────────

    # ── Audit log: INSERT erasure event (NEVER DELETE audit rows) ────────────
    # GDPR requires that the FACT of erasure is permanently retained.
    # This INSERT records THAT erasure occurred — it does NOT contain
    # the user's personal data, only the pseudonymous user_id and outcome.
    #
    # The erasure_audit table schema is defined in compliance/erasure_audit.sql.
    # Personal data rows in audit.llm_calls are NOT deleted; instead, their
    # personal fields are redacted (see redact_audit_personal_fields below).
    try:
        import asyncpg  # pip install asyncpg

        DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")

        if DATABASE_URL:
            conn = await asyncpg.connect(DATABASE_URL)
            try:
                await conn.execute(
                    """
                    INSERT INTO compliance.erasure_audit (
                        erasure_record_id,
                        user_id,
                        requested_at,
                        completed_at,
                        checkpoints_deleted,
                        vector_docs_deleted,
                        langsmith_runs_deleted,
                        errors_json,
                        status
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7,
                        $8::jsonb,
                        $9
                    )
                    """,
                    erasure_record_id,
                    user_id,
                    timestamp,
                    datetime.now(timezone.utc).isoformat(),
                    records_deleted["checkpoints"],
                    records_deleted["vector_docs"],
                    records_deleted["langsmith_runs"],
                    __import__("json").dumps(errors),
                    "partial" if errors else "completed",
                )
                records_deleted["audit_log_rows_redacted"] = 1
                logger.info(
                    "Erasure audit record written | erasure_record_id=%s",
                    erasure_record_id,
                )
            finally:
                await conn.close()

    except Exception as exc:
        # Audit write failure is serious — log loudly but do not crash
        logger.error(
            "CRITICAL: Erasure audit INSERT failed for user_id=%s: %s. "
            "The erasure_audit record may be missing. "
            "Insert it manually using erasure_record_id=%s.",
            user_id,
            exc,
            erasure_record_id,
        )
        errors.append(f"Erasure audit INSERT failed: {exc}")
    # ── END Audit log block ───────────────────────────────────────────────────

    # ── Redact personal fields from existing audit.llm_calls rows ─────────────
    # The event rows in audit.llm_calls are NOT deleted (audit rows are immutable).
    # Instead, personal identifiers in the user-visible fields are overwritten
    # with a GDPR redaction marker. The row_hash and prev_hash chain fields are
    # NOT updated — preserve them so chain verification still passes for
    # surrounding rows. Add a separate `redacted_at` column to track this.
    try:
        import asyncpg

        DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")

        if DATABASE_URL:
            conn = await asyncpg.connect(DATABASE_URL)
            try:
                redact_marker = f"[GDPR Art.17 erased {timestamp[:10]}]"
                result = await conn.execute(
                    """
                    UPDATE audit.llm_calls
                    SET
                        session_id   = $2,
                        redacted_at  = NOW()
                    WHERE user_id = $1
                      AND redacted_at IS NULL
                    """,
                    user_id,
                    redact_marker,
                )
                redacted_count = int(result.split()[-1]) if result else 0
                records_deleted["audit_log_rows_redacted"] = redacted_count
                logger.info(
                    "Redacted %d audit.llm_calls rows for user_id=%s",
                    redacted_count,
                    user_id,
                )
            finally:
                await conn.close()

    except Exception as exc:
        msg = f"Audit log redaction failed: {exc}"
        errors.append(msg)
        logger.error(msg)
    # ── END Audit log redaction block ─────────────────────────────────────────

    final_status = "failed" if not any(records_deleted.values()) and errors else (
        "partial" if errors else "completed"
    )

    result = ErasureResult(
        status=final_status,
        user_id=user_id,
        records_deleted=records_deleted,
        timestamp=timestamp,
        erasure_record_id=erasure_record_id,
        errors=errors,
    )

    logger.info(
        "GDPR Art.17 erasure FINISHED | user_id=%s | status=%s | "
        "records_deleted=%s | erasure_record_id=%s",
        user_id,
        result.status,
        result.records_deleted,
        erasure_record_id,
    )

    return result


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="GDPR Article 17 erasure — delete all data for a user_id."
    )
    parser.add_argument("user_id", help="The user_id to erase.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without making any changes.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print(f"[DRY RUN] Would erase all data for user_id={args.user_id!r}")
        print("  • PostgresSaver: DELETE WHERE thread_id LIKE 'tenant:%:user:{user_id}:%'")
        print("  • Vector store:  delete(where={'user_id': user_id})")
        print("  • LangSmith:     delete_runs(filter='has(metadata, {\"user_id\": ...})')")
        print("  • Audit log:     INSERT INTO compliance.erasure_audit ...")
        print("  • Audit rows:    UPDATE audit.llm_calls SET session_id='[GDPR erased]'")
        sys.exit(0)

    result = asyncio.run(delete_user_data(args.user_id))
    print(json.dumps(result.model_dump(), indent=2))

    if not result.fully_successful:
        print(
            f"\n[WARN] Erasure was {result.status}. "
            f"{len(result.errors)} error(s) occurred.",
            file=sys.stderr,
        )
        sys.exit(1)
```

**Substitution rules when writing the file:**
- Replace `{HAS_POSTGRES_SAVER}` with `True` or `False`
- Replace `{HAS_CHROMA or ...}` with the actual boolean expression result
- Replace `{HAS_LANGSMITH}` with `True` or `False`
- For stores NOT detected: wrap their block in a `# NOT DETECTED IN PROJECT — enable if needed` comment and comment out all the code in that block
- For stores detected: include the code uncommented

---

## Step 5 — Generate erasure_audit Table DDL

Write the file to `compliance/erasure_audit.sql`.

This table is the **immutable legal record that erasure occurred**. It must
never have rows deleted from it. It also adds the `redacted_at` column to
`audit.llm_calls` needed by the redaction step in `erase_user.py`.

```sql
-- compliance/erasure_audit.sql
-- GDPR Article 17 — Erasure Audit Table
--
-- PURPOSE: Retain permanent, immutable proof that an erasure request
-- was received and honored. This table MUST be kept forever — it is
-- legal evidence of GDPR compliance, not personal data.
--
-- The user_id column contains a pseudonymous identifier only.
-- No name, email, or other personal data is stored here.
--
-- Run this migration once as a superuser / migration role.
-- The application role (audit_app_role) needs INSERT + SELECT only.

-- ── Schema ────────────────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS compliance;

-- ── Erasure audit table ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS compliance.erasure_audit (
    erasure_record_id    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              TEXT        NOT NULL,
    requested_at         TIMESTAMPTZ NOT NULL,
    completed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checkpoints_deleted  INT         NOT NULL DEFAULT 0,
    vector_docs_deleted  INT         NOT NULL DEFAULT 0,
    langsmith_runs_deleted INT       NOT NULL DEFAULT 0,
    errors_json          JSONB       NOT NULL DEFAULT '[]',
    status               TEXT        NOT NULL
                             CHECK (status IN ('completed', 'partial', 'failed')),

    -- Metadata
    erased_by            TEXT,       -- operator identity (who triggered erasure)
    request_reference    TEXT,       -- ticket/case number from your DSAR process
    notes                TEXT        -- free-text for edge cases
);

-- Indexes for compliance reporting
CREATE INDEX IF NOT EXISTS idx_erasure_audit_user_id
    ON compliance.erasure_audit (user_id);

CREATE INDEX IF NOT EXISTS idx_erasure_audit_completed_at
    ON compliance.erasure_audit (completed_at DESC);

-- ── Application role: INSERT + SELECT only ────────────────────────────────────
-- The app can write new erasure records and query them for reporting.
-- It can NEVER delete or modify them.

GRANT USAGE ON SCHEMA compliance TO audit_app_role;
GRANT INSERT, SELECT ON compliance.erasure_audit TO audit_app_role;

REVOKE UPDATE  ON compliance.erasure_audit FROM audit_app_role;
REVOKE DELETE  ON compliance.erasure_audit FROM audit_app_role;
REVOKE TRUNCATE ON compliance.erasure_audit FROM audit_app_role;

-- ── Add redacted_at column to audit.llm_calls (if it exists) ─────────────────
-- This column tracks which rows have had their personal fields overwritten
-- as part of an Article 17 erasure. The row itself is NOT deleted — only
-- the personal fields are replaced with a GDPR redaction marker.
-- The hash chain (row_hash, prev_hash) is preserved intact.

ALTER TABLE audit.llm_calls
    ADD COLUMN IF NOT EXISTS redacted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_llm_calls_redacted_at
    ON audit.llm_calls (redacted_at)
    WHERE redacted_at IS NOT NULL;

-- ── Compliance view: all erasure events ──────────────────────────────────────

CREATE OR REPLACE VIEW compliance.erasure_summary AS
SELECT
    erasure_record_id,
    user_id,
    requested_at,
    completed_at,
    EXTRACT(EPOCH FROM (completed_at - requested_at))::int AS processing_seconds,
    checkpoints_deleted + vector_docs_deleted + langsmith_runs_deleted
        AS total_records_deleted,
    status,
    jsonb_array_length(errors_json) AS error_count,
    erased_by,
    request_reference
FROM compliance.erasure_audit
ORDER BY completed_at DESC;

-- ── GDPR 30-day compliance check ─────────────────────────────────────────────
-- Query to find any erasure requests that are at risk of breaching
-- the 30-day deadline. Run this daily.

CREATE OR REPLACE VIEW compliance.erasure_deadline_risk AS
SELECT
    erasure_record_id,
    user_id,
    requested_at,
    requested_at + INTERVAL '30 days' AS deadline,
    requested_at + INTERVAL '30 days' - NOW() AS time_remaining,
    status
FROM compliance.erasure_audit
WHERE
    status != 'completed'
    AND requested_at > NOW() - INTERVAL '30 days'
ORDER BY deadline ASC;

-- ── Usage notes ───────────────────────────────────────────────────────────────
--
-- To verify an erasure occurred for user_id 'user_42':
--   SELECT * FROM compliance.erasure_audit WHERE user_id = 'user_42';
--
-- To check for at-risk requests:
--   SELECT * FROM compliance.erasure_deadline_risk;
--
-- To audit that audit.llm_calls rows were redacted:
--   SELECT COUNT(*) FROM audit.llm_calls
--   WHERE user_id = 'user_42' AND redacted_at IS NOT NULL;
--
-- IMPORTANT: Never run DELETE on compliance.erasure_audit.
-- IMPORTANT: Never run DELETE on audit.llm_calls (use redaction only).
```

---

## Step 6 — Print 30-Day Deadline Reminder

After writing both files, print this block verbatim:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 GDPR Article 17 — 30-Day Compliance Deadline
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

 GDPR requires erasure to be completed within ONE CALENDAR MONTH
 of the verified erasure request (extendable to 3 months with
 written notice to the data subject).

 Today's date: {TODAY}
 30-day deadline: {TODAY + 30 days}

 Required steps before the deadline:
   [ ] Verify the requestor's identity (not done by this tool)
   [ ] Run the generated erase_user.py against PRODUCTION data
   [ ] Confirm erasure_record_id is written to compliance.erasure_audit
   [ ] Notify the data subject that erasure is complete (required by law)
   [ ] Store the request reference and erasure_record_id in your DSAR log

 COMPLIANCE NOTE: The erasure_audit record itself is PERMANENT.
 The erasure event (that it occurred) MUST be retained — do not
 delete rows from compliance.erasure_audit.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Substitute real dates: today's date is available as the current date.
Compute today + 30 days and print both in YYYY-MM-DD format.

---

## Step 7 — Print Summary

Print a summary of what was generated:

```
Files generated:
  compliance/erase_user.py      — async delete_user_data() function
  compliance/erasure_audit.sql  — DDL for erasure_audit table + redacted_at column

Data stores covered:
  [list detected stores from Step 3, each on its own line]

Next steps:
  1. Run the DDL:   psql $DATABASE_URL -f compliance/erasure_audit.sql
  2. Review the generated code and adapt THREAD_ID_PATTERN to your naming scheme
  3. Test with --dry-run:   python compliance/erase_user.py {TARGET_USER_ID} --dry-run
  4. Run in production:     python compliance/erase_user.py {TARGET_USER_ID}
  5. Notify the data subject of completion (required by GDPR Article 12(3))

See lc:compliance for full GDPR/HIPAA compliance patterns.
See lc:audit for the complete audit logging architecture this builds on.
```

---

## Output Rules

- Write files with the exact paths shown: `compliance/erase_user.py` and
  `compliance/erasure_audit.sql`. Create the `compliance/` directory if needed.
- Code in `erase_user.py` must be syntactically valid Python 3.11+.
- SQL in `erasure_audit.sql` must be valid PostgreSQL.
- Every SQL statement that grants or revokes privileges must be present.
- The `erasure_audit` table must be INSERT-only for the app role — the
  REVOKE DELETE and REVOKE UPDATE statements are mandatory.
- The audit log redaction block (`UPDATE audit.llm_calls`) must never use
  DELETE — redaction only, never deletion of audit rows.
- The `ErasureResult` model uses Pydantic v2 syntax (`BaseModel`, `Field`).
  Never use Pydantic v1 validators.
- `load_dotenv()` must appear before any `os.environ` reads.
- Never hardcode credentials. All connection strings come from environment variables.
- The `__main__` block must include a `--dry-run` flag.
