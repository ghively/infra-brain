---
name: lc-audit
description: Use when adding compliance-grade immutable audit logging to a LangChain or LangGraph application — SOC2, HIPAA, PCI, EU AI Act, or internal audit requirements. Covers tamper-evident append-only PostgreSQL schemas, hash-chained entries, user attribution, decision traceability, retention policies, and self-hosted LangSmith alternatives. Triggered by requests for audit logs, compliance logging, tamper-evident traces, HIPAA/SOC2/PCI evidence, user attribution in AI calls, or replacing LangSmith for regulated environments.
---

# lc:audit — Immutable Audit Logging for LangChain / LangGraph

## Why This Skill Exists

LangSmith is excellent for development observability. It is **not** a compliance audit log. This skill builds the layer that LangSmith cannot replace: a cryptographically tamper-evident, append-only record of every LLM call, tool use, and agent decision — with user attribution, retention enforcement, and a verifiable chain of custody.

---

## Teaching Section — Why LangSmith Is Not Enough for Compliance

Before writing a single line of code, you need to understand the gap. Read this section carefully — it will shape every decision in your audit architecture.

### What LangSmith Actually Is

LangSmith is a **development and operations observability platform**. It records traces so you can debug agents, evaluate quality, and track latency. It does those jobs well. But its design goals are completely different from compliance audit logging.

### The Five Problems with LangSmith as Your Compliance Record

**Problem 1: Traces are mutable and deletable.**
Any team member with the right LangSmith role can delete a trace. A compliance audit log must be append-only — once written, no row can be modified or deleted by the application. LangSmith has no append-only enforcement at the storage layer.

**Problem 2: You do not control the storage.**
LangSmith stores your data on Langchain Inc.'s servers (AWS us-east-1 by default). HIPAA requires you to control — or have a signed BAA covering — every system that touches PHI. GDPR Article 28 requires a Data Processing Agreement with every sub-processor. LangSmith can provide a BAA and DPA, but it adds a contractual dependency and US data residency unless you use their self-hosted option.

**Problem 3: There is no cryptographic integrity guarantee.**
A compliance auditor needs to prove that the log has not been tampered with after the fact. LangSmith does not hash-chain its entries. If a row is silently modified at the database level (by a rogue admin, a bug, or a breach), there is no way to detect it from the data alone.

**Problem 4: User attribution is optional and application-level.**
LangSmith traces can include metadata, but there is no enforced schema that requires `user_id` or `tenant_id` on every record. A compliance framework like SOC2 CC6.1 (logical access) requires that every privileged action be attributed to a specific user. If your application forgets to set the metadata, LangSmith records an anonymous trace — and you have no audit trail.

**Problem 5: Retention policy is coarse-grained.**
HIPAA requires 6-year audit log retention. PCI DSS requires 1 year online, 3 years archived. LangSmith's retention is plan-dependent and not partitioned for regulatory retention schedules. You cannot say "delete all data for user X after 7 years and archive it to cold storage first."

### What a Compliance Audit Log Must Provide

| Property | Why It Matters |
|---|---|
| **Append-only writes** | Prevents retrospective deletion of evidence |
| **Tamper detection** | Cryptographic proof the log has not been modified |
| **User attribution** | Every LLM call linked to a specific human identity |
| **Immutable timestamps** | Server-side `NOW()`, never client-supplied |
| **Structured retention** | Time-partitioned for efficient policy enforcement |
| **Access segregation** | App role has INSERT/SELECT only — never DELETE |
| **Decision rationale** | For high-stakes routing: why did the agent decide X? |
| **Data residency control** | You choose the country and cloud region |

### The Architecture We Will Build

```
FastAPI / Django / Flask
        │
        ├── extract user_id from JWT  ←── user attribution
        │
        ▼
LangGraph graph.invoke(config={"configurable": {"user_id": uid}})
        │
        ├── ImmutableAuditCallback  ←── fires on every LLM call, tool call, chain end
        │         │
        │         └── asyncpg.pool.execute(INSERT ...)  ←── append-only, non-blocking
        │
        ▼
PostgreSQL audit table  ←── app role: INSERT + SELECT only, no DELETE, no UPDATE
        │
        ├── hash-chained rows  ←── tamper detection
        ├── monthly partitions  ←── retention policy
        └── pg_cron archival job  ←── S3/GCS before deletion
```

LangSmith can still run in parallel for development debugging — it just stops being your *compliance record*.

---

## Discovery Flow

Ask all three questions in one message before scaffolding any code:

```
Before I build your audit logging setup, I have three quick questions:

1. What compliance framework are you targeting?
   (a) SOC2 Type II
   (b) HIPAA
   (c) PCI DSS
   (d) EU AI Act
   (e) Internal policy only
   (f) None — I just want tamper-evident logs

2. Do you need user attribution (linking every LLM call to a specific human)?
   (a) Yes — we are a multi-tenant SaaS (user_id + tenant_id required)
   (b) Yes — single-tenant but we need to know which employee made each call
   (c) No — this is a batch pipeline with no human users

3. What is your retention requirement?
   (a) 30 days (development / short-term ops)
   (b) 1 year (PCI DSS minimum)
   (c) 6 years (HIPAA)
   (d) 7 years (SOC2 / financial)
   (e) Indefinite (keep forever, no deletion)
```

**What the answers change:**

- SOC2/HIPAA/PCI → include all seven patterns below; add compliance-specific SQL views
- EU AI Act → add `model_version` and `rationale_json` as mandatory fields; include decision traceability
- No compliance framework → patterns 1, 2, 3 are sufficient
- User attribution required → pattern 4 is mandatory; every `graph.invoke()` needs the extraction snippet
- Retention > 30 days → pattern 7 is mandatory (partitioned table + archival)

---

## Pattern 1 — Append-Only PostgreSQL Audit Table

**What you are learning:** The database schema is the foundation. The key insight is that security is enforced at the *database privilege* layer, not in application code. Even if a bug, a compromised dependency, or a rogue developer tries to delete a row, the database will refuse because the application role has no DELETE privilege.

### Full DDL

```sql
-- audit_schema.sql
-- Run once as a superuser / migration owner (not as the application role)

-- ── Schema ────────────────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS audit;

-- ── Immutable audit table ─────────────────────────────────────────────────────
-- Partitioned by month so retention policy can drop entire partitions cheaply
-- instead of running expensive DELETE WHERE created_at < '...' scans.

CREATE TABLE audit.llm_calls (
    id              BIGSERIAL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- server-assigned, never client

    -- Identity
    user_id         TEXT        NOT NULL,                -- required; never NULL
    tenant_id       TEXT,                                -- NULL for single-tenant apps
    session_id      TEXT,                                -- optional conversation grouping

    -- Model
    model           TEXT        NOT NULL,                -- e.g. "claude-sonnet-4-6"
    model_version   TEXT,                                -- e.g. "20241022"
    provider        TEXT        NOT NULL DEFAULT 'anthropic',

    -- Token accounting
    input_tokens    INT         NOT NULL DEFAULT 0,
    output_tokens   INT         NOT NULL DEFAULT 0,
    total_tokens    INT GENERATED ALWAYS AS (input_tokens + output_tokens) STORED,

    -- Content hashes (SHA-256 hex of the raw prompt / completion text)
    -- Store hashes, not raw text, to avoid PII in the audit table itself.
    -- You can verify a specific prompt by re-hashing it and comparing.
    prompt_hash     TEXT        NOT NULL,
    completion_hash TEXT        NOT NULL,

    -- Tool calls (JSON array of {name, input_hash, output_hash, error})
    tool_calls      JSONB       NOT NULL DEFAULT '[]',

    -- Performance
    latency_ms      INT         NOT NULL DEFAULT 0,

    -- LangSmith / LangGraph run correlation
    run_id          TEXT,                                -- LangGraph run_id for cross-referencing
    parent_run_id   TEXT,                                -- for nested chain correlation

    -- Tamper detection (set by application, verified by AuditChainVerifier)
    prev_hash       TEXT,                                -- SHA-256 of previous row
    row_hash        TEXT,                                -- SHA-256 of this row's fields

    -- Decision traceability (EU AI Act, high-stakes routing)
    rationale_json  JSONB,                               -- chain-of-thought for routing decisions

    -- Compliance metadata
    framework       TEXT,                                -- "SOC2", "HIPAA", "PCI", etc.
    data_region     TEXT        NOT NULL DEFAULT 'eu-west-1',

    PRIMARY KEY (id, created_at)                         -- composite PK required for partitioning
)
PARTITION BY RANGE (created_at);

-- ── Monthly partitions — create one month ahead with a cron job ───────────────
-- Pattern: audit.llm_calls_YYYY_MM

CREATE TABLE audit.llm_calls_2025_01
    PARTITION OF audit.llm_calls
    FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');

CREATE TABLE audit.llm_calls_2025_02
    PARTITION OF audit.llm_calls
    FOR VALUES FROM ('2025-02-01') TO ('2025-03-01');

-- Add future months here, or use the pg_cron job in Pattern 7.

-- ── Indexes ───────────────────────────────────────────────────────────────────

CREATE INDEX ON audit.llm_calls (user_id, created_at DESC);
CREATE INDEX ON audit.llm_calls (tenant_id, created_at DESC);
CREATE INDEX ON audit.llm_calls (run_id);
CREATE INDEX ON audit.llm_calls (created_at DESC);  -- for retention queries

-- ── Application role — INSERT and SELECT ONLY ─────────────────────────────────
-- This is the account your FastAPI / LangGraph app connects as.
-- It can never modify or delete records.

CREATE ROLE audit_app_role;

GRANT USAGE ON SCHEMA audit TO audit_app_role;
GRANT INSERT, SELECT ON audit.llm_calls TO audit_app_role;
GRANT USAGE, SELECT ON SEQUENCE audit.llm_calls_id_seq TO audit_app_role;

-- Explicitly revoke dangerous privileges — belt and braces.
-- These REVOKE statements document intent and prevent accidental grants.
REVOKE UPDATE ON audit.llm_calls FROM audit_app_role;
REVOKE DELETE ON audit.llm_calls FROM audit_app_role;
REVOKE TRUNCATE ON audit.llm_calls FROM audit_app_role;
REVOKE DROP ON SCHEMA audit FROM audit_app_role;

-- ── Separate read-only role for compliance reports ────────────────────────────
CREATE ROLE audit_reader_role;
GRANT USAGE ON SCHEMA audit TO audit_reader_role;
GRANT SELECT ON audit.llm_calls TO audit_reader_role;
REVOKE INSERT ON audit.llm_calls FROM audit_reader_role;

-- ── Assign roles to database users ───────────────────────────────────────────
-- Replace 'myapp_db_user' with your actual application database user.
GRANT audit_app_role TO myapp_db_user;
-- GRANT audit_reader_role TO compliance_team_user;
```

**Why the role separation matters:** Even if your application is fully compromised, the attacker cannot use your application's database credentials to delete audit rows. They would need to escalate to the superuser or migration role, which should be a separate credential stored differently.

---

## Pattern 2 — ImmutableAuditCallback

**What you are learning:** LangChain's `BaseCallbackHandler` is an event hook system. Every LLM call, tool execution, and chain completion fires callbacks. `ImmutableAuditCallback` intercepts these events and writes to the audit table using a non-blocking async insert — it never makes the user wait for the audit write to complete.

```python
# audit/callback.py
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Any

import asyncpg
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_core.tools import ToolException

logger = logging.getLogger(__name__)


def _sha256(text: str) -> str:
    """Hash a string with SHA-256. Used for prompt/completion fingerprinting.

    Why hash instead of storing raw text?
    - Keeps PII out of the audit table itself
    - A compliance auditor can verify: re-hash the prompt and compare
    - Dramatically reduces storage cost for large prompts
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_text(data: Any) -> str:
    """Best-effort extraction of string content from LangChain output types."""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        # AIMessage content, ChatGeneration text, etc.
        return data.get("content") or data.get("text") or json.dumps(data)
    if hasattr(data, "content"):
        return str(data.content)
    return str(data)


class ImmutableAuditCallback(AsyncCallbackHandler):
    """Append-only audit log writer for every LLM call, tool use, and chain end.

    Usage
    -----
    Pass an instance to graph.invoke() or any runnable via the callbacks list:

        callback = ImmutableAuditCallback(pool=pool, user_id="user-123", tenant_id="acme")
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=query)]},
            config=RunnableConfig(callbacks=[callback]),
        )

    The callback fires three events:
    - on_llm_end: captures model, tokens, latency, prompt/completion hashes
    - on_tool_end: captures tool name, input/output hashes
    - on_chain_end: marks the top-level run as complete

    All writes are fire-and-forget (asyncio.create_task) so they never
    add latency to the user-facing response.
    """

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        user_id: str,
        tenant_id: str | None = None,
        session_id: str | None = None,
        framework: str | None = None,
        data_region: str = "eu-west-1",
    ) -> None:
        super().__init__()
        self.pool = pool
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.framework = framework
        self.data_region = data_region

        # Track per-run start times for latency calculation
        # Key: run_id (str), Value: float (time.monotonic() at start)
        self._start_times: dict[str, float] = {}

    # ── LLM lifecycle ─────────────────────────────────────────────────────────

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Record the start time so we can compute latency in on_llm_end."""
        self._start_times[str(run_id)] = time.monotonic()

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Fires after every LLM call — this is the main audit event.

        LLMResult.llm_output contains token counts (usage_metadata).
        LLMResult.generations is a list of lists of Generation objects;
        we take [0][0] as the primary completion.
        """
        run_id_str = str(run_id)
        start = self._start_times.pop(run_id_str, None)
        latency_ms = int((time.monotonic() - start) * 1000) if start else 0

        # Extract token counts from usage metadata
        usage = response.llm_output or {}
        token_usage = usage.get("token_usage") or usage.get("usage") or {}
        input_tokens = int(
            token_usage.get("input_tokens")
            or token_usage.get("prompt_tokens")
            or 0
        )
        output_tokens = int(
            token_usage.get("output_tokens")
            or token_usage.get("completion_tokens")
            or 0
        )

        # Extract model name from serialized metadata or llm_output
        model = (
            usage.get("model_name")
            or kwargs.get("invocation_params", {}).get("model")
            or "unknown"
        )

        # Hash the prompt (from kwargs — passed by LangChain when available)
        invocation_params = kwargs.get("invocation_params", {})
        raw_prompt = json.dumps(invocation_params.get("messages", []))
        prompt_hash = _sha256(raw_prompt)

        # Hash the completion
        try:
            completion_text = _extract_text(response.generations[0][0])
        except (IndexError, AttributeError):
            completion_text = ""
        completion_hash = _sha256(completion_text)

        # Fire-and-forget: never block the main execution path
        asyncio.create_task(
            self._insert_llm_call(
                run_id=run_id_str,
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                prompt_hash=prompt_hash,
                completion_hash=completion_hash,
                latency_ms=latency_ms,
            )
        )

    # ── Tool lifecycle ────────────────────────────────────────────────────────

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        self._start_times[str(run_id)] = time.monotonic()

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Fires after every tool call — records tool name, input hash, output hash."""
        run_id_str = str(run_id)
        start = self._start_times.pop(run_id_str, None)
        latency_ms = int((time.monotonic() - start) * 1000) if start else 0

        tool_name = kwargs.get("name") or "unknown_tool"
        input_str = kwargs.get("input_str") or ""

        output_text = _extract_text(output)
        is_error = isinstance(output, ToolException) or kwargs.get("error")

        tool_call_record = {
            "name": tool_name,
            "input_hash": _sha256(input_str),
            "output_hash": _sha256(output_text),
            "latency_ms": latency_ms,
            "error": is_error,
        }

        # Tool calls are stored inside the parent LLM call row.
        # Here we write a standalone tool-call row for detailed audit trails.
        asyncio.create_task(
            self._insert_tool_call(
                run_id=run_id_str,
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                tool_call_record=tool_call_record,
            )
        )

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Log failed tool calls — failures are audit evidence too."""
        run_id_str = str(run_id)
        self._start_times.pop(run_id_str, None)

        tool_name = kwargs.get("name") or "unknown_tool"
        error_record = {
            "name": tool_name,
            "input_hash": _sha256(kwargs.get("input_str") or ""),
            "output_hash": _sha256(str(error)),
            "latency_ms": 0,
            "error": True,
            "error_type": type(error).__name__,
        }
        asyncio.create_task(
            self._insert_tool_call(
                run_id=run_id_str,
                parent_run_id=None,
                tool_call_record=error_record,
            )
        )

    # ── Private insert helpers ────────────────────────────────────────────────

    async def _insert_llm_call(
        self,
        *,
        run_id: str,
        parent_run_id: str | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        prompt_hash: str,
        completion_hash: str,
        latency_ms: int,
    ) -> None:
        """Insert one row into audit.llm_calls. Called as a background task."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO audit.llm_calls (
                        user_id, tenant_id, session_id,
                        model, provider,
                        input_tokens, output_tokens,
                        prompt_hash, completion_hash,
                        latency_ms,
                        run_id, parent_run_id,
                        framework, data_region
                    ) VALUES (
                        $1, $2, $3,
                        $4, $5,
                        $6, $7,
                        $8, $9,
                        $10,
                        $11, $12,
                        $13, $14
                    )
                    """,
                    self.user_id,
                    self.tenant_id,
                    self.session_id,
                    model,
                    "anthropic",
                    input_tokens,
                    output_tokens,
                    prompt_hash,
                    completion_hash,
                    latency_ms,
                    run_id,
                    parent_run_id,
                    self.framework,
                    self.data_region,
                )
        except Exception:
            # NEVER let an audit failure crash the main application.
            # Log the error and continue — losing one audit row is
            # better than losing the user's response.
            logger.exception(
                "AUDIT WRITE FAILED for run_id=%s user_id=%s — "
                "audit log has a gap",
                run_id,
                self.user_id,
            )

    async def _insert_tool_call(
        self,
        *,
        run_id: str,
        parent_run_id: str | None,
        tool_call_record: dict,
    ) -> None:
        """Insert a tool call row. Tool calls get their own rows for granular audit."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO audit.llm_calls (
                        user_id, tenant_id, session_id,
                        model, provider,
                        input_tokens, output_tokens,
                        prompt_hash, completion_hash,
                        tool_calls,
                        latency_ms, run_id, parent_run_id,
                        framework, data_region
                    ) VALUES (
                        $1, $2, $3,
                        'tool-call', 'internal',
                        0, 0,
                        $4, $5,
                        $6::jsonb,
                        $7, $8, $9,
                        $10, $11
                    )
                    """,
                    self.user_id,
                    self.tenant_id,
                    self.session_id,
                    tool_call_record["input_hash"],
                    tool_call_record["output_hash"],
                    json.dumps([tool_call_record]),
                    tool_call_record.get("latency_ms", 0),
                    run_id,
                    parent_run_id,
                    self.framework,
                    self.data_region,
                )
        except Exception:
            logger.exception(
                "AUDIT TOOL WRITE FAILED for run_id=%s user_id=%s",
                run_id,
                self.user_id,
            )
```

---

## Pattern 3 — Hash-Chained Entries (Tamper Detection)

**What you are learning:** A blockchain-style hash chain means each row includes a hash of the *previous* row. If anyone modifies row 500, row 501's `prev_hash` will no longer match — the entire chain fails to verify from row 500 onward. This is your cryptographic proof that the log is intact.

```python
# audit/chain_verifier.py
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


def _compute_row_hash(row: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hash of a row's content fields.

    We hash a canonical JSON representation of the fields that matter
    for integrity. We deliberately exclude `prev_hash` and `row_hash`
    themselves (they are part of the chain structure, not the content).
    """
    content = {
        "id": row["id"],
        "created_at": row["created_at"].isoformat(),
        "user_id": row["user_id"],
        "model": row["model"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "prompt_hash": row["prompt_hash"],
        "completion_hash": row["completion_hash"],
        "run_id": row["run_id"],
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ChainVerificationResult:
    total_rows: int
    verified_rows: int
    first_failure_id: int | None
    failure_reason: str | None
    is_valid: bool

    def __str__(self) -> str:
        if self.is_valid:
            return f"Chain VALID: {self.verified_rows}/{self.total_rows} rows verified"
        return (
            f"Chain INVALID at row {self.first_failure_id}: "
            f"{self.failure_reason} "
            f"({self.verified_rows} rows verified before failure)"
        )


class AuditChainVerifier:
    """Reads audit rows in order and verifies the hash chain is unbroken.

    Run this periodically (daily cron job) or on demand before a compliance
    audit. If any row has been modified, deleted, or inserted out of order,
    this verifier will detect it.

    Usage
    -----
        verifier = AuditChainVerifier(pool)
        result = await verifier.verify_chain(
            start_date="2025-01-01",
            end_date="2025-02-01",
        )
        if not result.is_valid:
            alert_security_team(result)
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def verify_chain(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        tenant_id: str | None = None,
    ) -> ChainVerificationResult:
        """Verify the hash chain for all rows in the given date window.

        The chain is verified by reading rows in ascending `id` order and
        checking that each row's `prev_hash` equals the computed hash of
        the previous row.
        """
        where_clauses = ["row_hash IS NOT NULL"]
        params: list[Any] = []

        if start_date:
            params.append(start_date)
            where_clauses.append(f"created_at >= ${len(params)}")
        if end_date:
            params.append(end_date)
            where_clauses.append(f"created_at < ${len(params)}")
        if tenant_id:
            params.append(tenant_id)
            where_clauses.append(f"tenant_id = ${len(params)}")

        where_sql = " AND ".join(where_clauses)
        query = f"""
            SELECT id, created_at, user_id, model, input_tokens, output_tokens,
                   prompt_hash, completion_hash, run_id, prev_hash, row_hash
            FROM audit.llm_calls
            WHERE {where_sql}
            ORDER BY id ASC
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        if not rows:
            return ChainVerificationResult(
                total_rows=0,
                verified_rows=0,
                first_failure_id=None,
                failure_reason=None,
                is_valid=True,
            )

        previous_hash: str | None = None
        verified = 0

        for row in rows:
            row_dict = dict(row)
            expected_prev = previous_hash

            # Check 1: prev_hash must match previous row's hash
            if expected_prev is not None and row_dict["prev_hash"] != expected_prev:
                logger.error(
                    "AUDIT CHAIN BROKEN at row id=%d: "
                    "prev_hash=%r expected=%r",
                    row_dict["id"],
                    row_dict["prev_hash"],
                    expected_prev,
                )
                return ChainVerificationResult(
                    total_rows=len(rows),
                    verified_rows=verified,
                    first_failure_id=row_dict["id"],
                    failure_reason=(
                        f"prev_hash mismatch: stored={row_dict['prev_hash']!r} "
                        f"expected={expected_prev!r}"
                    ),
                    is_valid=False,
                )

            # Check 2: stored row_hash must match recomputed hash
            expected_row_hash = _compute_row_hash(row_dict)
            if row_dict["row_hash"] != expected_row_hash:
                logger.error(
                    "AUDIT ROW MODIFIED at id=%d: "
                    "stored_hash=%r recomputed=%r",
                    row_dict["id"],
                    row_dict["row_hash"],
                    expected_row_hash,
                )
                return ChainVerificationResult(
                    total_rows=len(rows),
                    verified_rows=verified,
                    first_failure_id=row_dict["id"],
                    failure_reason=(
                        f"row_hash mismatch: stored={row_dict['row_hash']!r} "
                        f"recomputed={expected_row_hash!r}"
                    ),
                    is_valid=False,
                )

            previous_hash = row_dict["row_hash"]
            verified += 1

        logger.info("Audit chain verified: %d/%d rows valid", verified, len(rows))
        return ChainVerificationResult(
            total_rows=len(rows),
            verified_rows=verified,
            first_failure_id=None,
            failure_reason=None,
            is_valid=True,
        )


# ── Updating ImmutableAuditCallback to write row_hash and prev_hash ──────────
# Add this helper to the callback's _insert_llm_call method.
# It fetches the last row's hash, then inserts with chain fields populated.

async def insert_with_chain(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    tenant_id: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    prompt_hash: str,
    completion_hash: str,
    run_id: str,
    **kwargs: Any,
) -> None:
    """Insert a row with hash chain fields. Must run in a SERIALIZABLE transaction
    to prevent two concurrent inserts from using the same prev_hash.
    """
    async with conn.transaction(isolation="serializable"):
        # Fetch the hash of the most recently inserted row
        last_row = await conn.fetchrow(
            "SELECT row_hash FROM audit.llm_calls ORDER BY id DESC LIMIT 1"
        )
        prev_hash = last_row["row_hash"] if last_row else None

        # Compute this row's hash (without prev_hash included — it is metadata)
        row_content = {
            "user_id": user_id,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "prompt_hash": prompt_hash,
            "completion_hash": completion_hash,
            "run_id": run_id,
        }
        canonical = json.dumps(row_content, sort_keys=True, separators=(",", ":"))
        row_hash = hashlib.sha256(canonical.encode()).hexdigest()

        await conn.execute(
            """
            INSERT INTO audit.llm_calls (
                user_id, tenant_id, model, provider,
                input_tokens, output_tokens,
                prompt_hash, completion_hash,
                run_id, prev_hash, row_hash
            ) VALUES ($1, $2, $3, 'anthropic', $4, $5, $6, $7, $8, $9, $10)
            """,
            user_id, tenant_id, model,
            input_tokens, output_tokens,
            prompt_hash, completion_hash,
            run_id, prev_hash, row_hash,
        )
```

**Important note on serializable transactions:** The hash chain requires that inserts happen one at a time in sequence. Under high concurrency, use a PostgreSQL advisory lock or queue the writes through a single async worker to avoid transaction conflicts. For most compliance workloads (hundreds of calls/second, not millions), serializable transactions with `asyncpg` are fast enough.

---

## Pattern 4 — User Attribution

**What you are learning:** User attribution means every row in the audit table is linked to a specific human identity. This does not happen automatically — you must extract the user identity from your authentication layer and inject it into every LangGraph call. This pattern shows you where that extraction happens and how to ensure it is never forgotten.

```python
# api/routes/chat.py
from __future__ import annotations

import jwt
from fastapi import FastAPI, Header, HTTPException, Depends
from pydantic import BaseModel
import asyncpg
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph

from audit.callback import ImmutableAuditCallback

app = FastAPI()


# ── JWT extraction dependency ─────────────────────────────────────────────────

async def get_current_user(
    authorization: str = Header(..., description="Bearer JWT token"),
) -> dict:
    """Extract user identity from the Authorization header.

    This is the single point where user identity enters your system.
    Everything downstream receives user_id already extracted — it never
    needs to deal with JWTs or session cookies directly.

    Why do it here instead of inside the LangGraph node?
    - Separation of concerns: auth is a web layer concern, not an AI concern
    - Fail fast: if the token is invalid, reject before touching the LLM
    - Consistency: every route shares the same extraction logic
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization[len("Bearer "):]
    try:
        payload = jwt.decode(
            token,
            key="your-jwt-secret",     # load from environment in production
            algorithms=["HS256"],
        )
        return {
            "user_id": payload["sub"],          # standard JWT subject claim
            "tenant_id": payload.get("tid"),    # optional tenant claim
            "email": payload.get("email"),
        }
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


# ── Request / response models ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    run_id: str | None = None


# ── Route with mandatory user attribution ────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    user: dict = Depends(get_current_user),     # always required — no anonymous calls
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    graph: StateGraph = Depends(get_graph),
) -> ChatResponse:
    """Every LLM call goes through this route.

    The audit callback is instantiated per-request with the user's identity.
    This guarantees that every audit row has user_id and tenant_id set —
    the callback constructor requires them as non-optional parameters.
    """
    audit_callback = ImmutableAuditCallback(
        pool=db_pool,
        user_id=user["user_id"],        # extracted from JWT — never from request body
        tenant_id=user["tenant_id"],    # None for single-tenant apps
        session_id=request.session_id,
        framework="SOC2",               # set from your app config
    )

    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=request.message)]},
        config={
            "callbacks": [audit_callback],
            "configurable": {
                # Pass user_id into graph state so nodes can access it if needed
                "user_id": user["user_id"],
                "tenant_id": user["tenant_id"],
            },
            "recursion_limit": 25,      # always set to prevent infinite loops
        },
    )

    messages = result.get("messages", [])
    response_text = messages[-1].content if messages else ""

    return ChatResponse(
        response=response_text,
        run_id=result.get("run_id"),
    )


# ── lc-coder checklist: every graph.invoke() in a web handler MUST ───────────
#
#   [ ] audit_callback instantiated with user_id (not None, not "anonymous")
#   [ ] ImmutableAuditCallback in config["callbacks"]
#   [ ] recursion_limit set in config
#   [ ] user_id sourced from verified auth token, never from request body
#   [ ] tenant_id extracted if multi-tenant
#   [ ] session_id propagated for conversation grouping
```

---

## Pattern 5 — Decision Traceability

**What you are learning:** For high-stakes AI applications (medical triage, loan decisions, legal routing, content moderation), it is not enough to log *what* the model output was — you must log *why*. The EU AI Act Article 13 (transparency) and Article 14 (human oversight) require that high-risk AI systems explain their outputs. This pattern captures the chain-of-thought rationale before the final routing decision.

```python
# audit/rationale.py
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ── Rationale schema ──────────────────────────────────────────────────────────

class DecisionRationale(BaseModel):
    """Structured rationale for a high-stakes routing decision.

    This schema is stored in audit.llm_calls.rationale_json and provides
    the evidence trail required by EU AI Act Article 13.
    """

    decision: str = Field(description="The routing decision made, e.g. 'escalate_to_human'")
    confidence: float = Field(ge=0.0, le=1.0, description="Model confidence 0.0-1.0")
    primary_reason: str = Field(description="One-sentence explanation of the primary factor")
    supporting_factors: list[str] = Field(description="Additional factors considered")
    risk_level: str = Field(description="'low', 'medium', 'high', or 'critical'")
    human_review_required: bool = Field(description="Whether this decision requires human review")
    data_used: list[str] = Field(
        description="List of data fields that influenced this decision (for GDPR data minimization audit)"
    )


# ── Rationale capture prompt ──────────────────────────────────────────────────

RATIONALE_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are an AI decision auditor. Your job is to document the reasoning
behind an AI routing decision in a structured, auditable format.

Be precise and factual. Do not hedge or be vague. This record will be used
as evidence in compliance audits — clarity is required.

Respond with valid JSON matching the schema exactly.""",
    ),
    (
        "human",
        """Document the reasoning for this routing decision:

Context: {context}
Decision made: {decision}
Input that triggered the decision: {input_summary}

Provide a structured rationale JSON.""",
    ),
])


def build_rationale_chain(model_name: str = "claude-sonnet-4-6"):
    """Build an LCEL chain that produces a DecisionRationale.

    This chain is called BEFORE the final routing node executes,
    so the rationale is captured in the same audit row as the decision.
    """
    llm = ChatAnthropic(model=model_name, temperature=0)
    parser = JsonOutputParser(pydantic_object=DecisionRationale)

    # LCEL pipe syntax: prompt | llm | parser
    # Each | connects a Runnable — the output of the left becomes the input of the right
    return RATIONALE_PROMPT | llm | parser


# ── Graph node that captures rationale ────────────────────────────────────────

async def routing_node_with_rationale(
    state: dict[str, Any],
    rationale_chain=None,
) -> dict[str, Any]:
    """A LangGraph node that makes a high-stakes routing decision and captures rationale.

    Pattern:
    1. Call the rationale chain to document the reasoning
    2. Make the actual routing decision
    3. Store the rationale in state so it flows to the audit callback
    4. Return the decision in state

    The audit callback's on_chain_end will pick up rationale_json from state.
    """
    if rationale_chain is None:
        rationale_chain = build_rationale_chain()

    user_message = state["messages"][-1].content if state.get("messages") else ""

    try:
        rationale = await rationale_chain.ainvoke({
            "context": "Medical triage system — routing patient queries to appropriate care level",
            "decision": "Determining urgency level (emergency / urgent / routine / self-care)",
            "input_summary": user_message[:500],  # truncate for prompt efficiency
        })
    except Exception:
        logger.exception("Rationale capture failed — proceeding without rationale")
        rationale = {"decision": "unknown", "primary_reason": "rationale capture error"}

    # Store rationale in state — the audit callback reads it from here
    return {
        **state,
        "rationale_json": rationale,
        # The actual routing decision goes here too
        "routing_decision": rationale.get("decision", "route_to_human"),
    }


# ── Helper: extract rationale from state into audit row ──────────────────────

def capture_rationale(state: dict[str, Any]) -> str | None:
    """Extract the rationale JSON string from graph state for audit storage.

    Call this from the audit callback's on_chain_end to write rationale_json
    into the audit row.

    Example usage in a custom chain end callback:
        rationale_str = capture_rationale(state)
        if rationale_str:
            await conn.execute(
                "UPDATE audit.llm_calls SET rationale_json = $1::jsonb WHERE run_id = $2",
                rationale_str, run_id
            )
    """
    rationale = state.get("rationale_json")
    if rationale is None:
        return None
    if isinstance(rationale, dict):
        return json.dumps(rationale)
    return str(rationale)
```

---

## Pattern 6 — Audit Log Query Patterns

**What you are learning:** The audit log is only useful if you can query it. Compliance frameworks have specific reporting requirements. These SQL views and queries cover the most common audit requests — from "all calls by user X" to GDPR erasure evidence.

```sql
-- audit_queries.sql

-- ── View: all calls by a specific user (for HR / security investigations) ─────
CREATE VIEW audit.calls_by_user AS
SELECT
    id,
    created_at,
    user_id,
    tenant_id,
    model,
    input_tokens,
    output_tokens,
    latency_ms,
    run_id,
    tool_calls,
    rationale_json
FROM audit.llm_calls
ORDER BY user_id, created_at DESC;


-- ── View: high-cost calls (token spend monitoring) ───────────────────────────
CREATE VIEW audit.high_cost_calls AS
SELECT
    id,
    created_at,
    user_id,
    tenant_id,
    model,
    input_tokens,
    output_tokens,
    (input_tokens + output_tokens) AS total_tokens,
    latency_ms,
    run_id
FROM audit.llm_calls
WHERE (input_tokens + output_tokens) > 10000   -- adjust threshold
ORDER BY (input_tokens + output_tokens) DESC;


-- ── View: failed tool calls (error rate monitoring) ──────────────────────────
CREATE VIEW audit.failed_tool_calls AS
SELECT
    id,
    created_at,
    user_id,
    run_id,
    tool_calls
FROM audit.llm_calls
WHERE tool_calls @> '[{"error": true}]'   -- JSONB containment check
ORDER BY created_at DESC;


-- ── View: daily usage summary (cost reporting) ───────────────────────────────
CREATE VIEW audit.daily_usage_summary AS
SELECT
    DATE_TRUNC('day', created_at)::date AS day,
    tenant_id,
    model,
    COUNT(*)                       AS call_count,
    SUM(input_tokens)              AS total_input_tokens,
    SUM(output_tokens)             AS total_output_tokens,
    AVG(latency_ms)::int           AS avg_latency_ms,
    PERCENTILE_CONT(0.95)
        WITHIN GROUP (ORDER BY latency_ms)::int AS p95_latency_ms
FROM audit.llm_calls
WHERE model != 'tool-call'    -- exclude tool rows
GROUP BY 1, 2, 3
ORDER BY 1 DESC, 2, 3;


-- ── SOC2 CC6.1: all privileged AI actions in a date range ────────────────────
-- "Who accessed what data, when, using which model?"
SELECT
    created_at,
    user_id,
    tenant_id,
    model,
    input_tokens + output_tokens AS tokens_used,
    latency_ms,
    run_id
FROM audit.llm_calls
WHERE
    created_at BETWEEN '2025-01-01' AND '2025-02-01'
    AND model != 'tool-call'
ORDER BY user_id, created_at;


-- ── HIPAA: all AI interactions involving a specific patient data window ───────
-- HIPAA requires you to provide an accounting of disclosures on request.
-- "All LLM calls that processed data for user X in the last 6 years."
SELECT
    created_at,
    user_id,
    model,
    input_tokens,
    output_tokens,
    run_id,
    prompt_hash,        -- auditor can verify: re-hash the original prompt
    completion_hash
FROM audit.llm_calls
WHERE
    user_id = 'patient-user-id-here'
ORDER BY created_at ASC;


-- ── GDPR Article 15: data subject access request ────────────────────────────
-- "Show me all AI processing records for this user."
-- Note: we return hashes, not raw prompts, to avoid re-disclosing other PII.
SELECT
    id,
    created_at,
    model,
    provider,
    input_tokens,
    output_tokens,
    prompt_hash,
    completion_hash,
    latency_ms,
    run_id
FROM audit.llm_calls
WHERE user_id = $1    -- parameterized to prevent injection
ORDER BY created_at ASC;


-- ── EU AI Act: all routing decisions with rationale ──────────────────────────
SELECT
    id,
    created_at,
    user_id,
    model,
    rationale_json->>'decision'         AS decision,
    rationale_json->>'risk_level'       AS risk_level,
    rationale_json->>'primary_reason'   AS primary_reason,
    (rationale_json->>'human_review_required')::boolean AS human_review,
    run_id
FROM audit.llm_calls
WHERE rationale_json IS NOT NULL
ORDER BY created_at DESC;


-- ── PCI DSS Req 10.2: all access to cardholder data systems ─────────────────
-- Tag calls that touch payment data with framework='PCI'
SELECT
    created_at,
    user_id,
    model,
    input_tokens + output_tokens AS tokens,
    run_id
FROM audit.llm_calls
WHERE
    framework = 'PCI'
    AND created_at >= NOW() - INTERVAL '1 year'
ORDER BY created_at DESC;
```

---

## Pattern 7 — Retention Policy Implementation

**What you are learning:** A retention policy means keeping data for exactly as long as required — no longer (GDPR data minimization), no shorter (regulatory obligation). Time-partitioned tables make this cheap: dropping a partition is instant, while `DELETE WHERE created_at < x` on a 500M-row table can run for hours.

```sql
-- audit_retention.sql

-- ── Automatic monthly partition creation (run monthly before month starts) ───
-- Add this to a pg_cron job or your migration pipeline.

CREATE OR REPLACE FUNCTION audit.create_next_month_partition()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    next_month     DATE := DATE_TRUNC('month', NOW() + INTERVAL '1 month');
    partition_end  DATE := next_month + INTERVAL '1 month';
    partition_name TEXT := 'llm_calls_' || TO_CHAR(next_month, 'YYYY_MM');
BEGIN
    EXECUTE FORMAT(
        'CREATE TABLE IF NOT EXISTS audit.%I PARTITION OF audit.llm_calls '
        'FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        next_month,
        partition_end
    );
    RAISE NOTICE 'Created partition audit.%', partition_name;
END;
$$;


-- ── Archival before deletion ──────────────────────────────────────────────────
-- Call this before dropping a partition. It exports the partition to S3/GCS
-- using postgres_fdw or pg_dump. Shown here as a notification function —
-- actual S3 export happens in your application layer or a Lambda/Cloud Function.

CREATE OR REPLACE FUNCTION audit.archive_partition_before_delete(
    partition_name TEXT
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    -- Signal your archival system to export this partition.
    -- In production: call pg_notify to trigger a Python archival worker,
    -- or use aws_s3.query_export_to_s3 if on RDS.
    PERFORM pg_notify(
        'audit_archive_requested',
        JSON_BUILD_OBJECT(
            'partition', partition_name,
            'requested_at', NOW()
        )::text
    );
    RAISE NOTICE 'Archive requested for partition: %', partition_name;
END;
$$;


-- ── delete_before: drop partitions older than a given date ───────────────────

CREATE OR REPLACE FUNCTION audit.delete_before(cutoff_date DATE)
RETURNS int
LANGUAGE plpgsql
AS $$
DECLARE
    partition_name TEXT;
    dropped_count  INT := 0;
    partition_date DATE;
BEGIN
    FOR partition_name IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'audit'
          AND tablename LIKE 'llm_calls_%'
        ORDER BY tablename
    LOOP
        -- Extract YYYY_MM from partition name like llm_calls_2023_06
        BEGIN
            partition_date := TO_DATE(
                SUBSTRING(partition_name FROM 'llm_calls_(\d{4}_\d{2})'),
                'YYYY_MM'
            );
        EXCEPTION WHEN OTHERS THEN
            CONTINUE;  -- skip partitions with unexpected names
        END;

        IF partition_date < DATE_TRUNC('month', cutoff_date) THEN
            -- Archive before dropping
            PERFORM audit.archive_partition_before_delete(partition_name);

            -- Drop the partition (instant — no row-level locking)
            EXECUTE FORMAT('DROP TABLE IF EXISTS audit.%I', partition_name);
            dropped_count := dropped_count + 1;
            RAISE NOTICE 'Dropped expired partition: audit.%', partition_name;
        END IF;
    END LOOP;

    RETURN dropped_count;
END;
$$;


-- ── pg_cron jobs (requires pg_cron extension) ────────────────────────────────
-- Install: CREATE EXTENSION IF NOT EXISTS pg_cron;
-- Note: pg_cron is available on AWS RDS, Google Cloud SQL, and Supabase.

-- Create next month's partition on the 25th of each month
SELECT cron.schedule(
    'create-monthly-audit-partition',
    '0 2 25 * *',    -- 02:00 UTC on the 25th
    'SELECT audit.create_next_month_partition()'
);

-- Enforce 7-year retention: drop partitions older than 7 years (SOC2/financial)
SELECT cron.schedule(
    'enforce-7year-retention',
    '0 3 1 * *',     -- 03:00 UTC on the 1st of each month
    $$SELECT audit.delete_before(NOW()::date - INTERVAL '7 years')$$
);

-- For HIPAA (6-year retention), change the interval:
-- SELECT audit.delete_before(NOW()::date - INTERVAL '6 years')

-- For PCI DSS (1-year online, archive older):
-- SELECT audit.delete_before(NOW()::date - INTERVAL '1 year')
```

```python
# audit/archival.py
# Python archival worker: listens for pg_notify and exports to S3/GCS

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime

import asyncpg
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


async def export_partition_to_s3(
    pool: asyncpg.Pool,
    partition_name: str,
    s3_bucket: str,
    s3_prefix: str = "audit-archive",
) -> None:
    """Export a PostgreSQL partition to S3 as a compressed CSV.

    This function is called before the partition is dropped, ensuring
    the data is preserved in cold storage for the full retention period.

    For production use: consider aws_s3 extension on RDS for direct DB→S3,
    or use COPY TO STDOUT piped to aws s3 cp for self-managed PostgreSQL.
    """
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    s3_key = f"{s3_prefix}/{partition_name}/{timestamp}.csv.gz"

    dsn = os.environ["AUDIT_DATABASE_URL"]

    # pg_dump a single partition to stdout, gzip, upload to S3
    pg_dump_cmd = [
        "pg_dump",
        "--table", f"audit.{partition_name}",
        "--data-only",
        "--format", "csv",
        dsn,
    ]
    s3_upload_cmd = [
        "aws", "s3", "cp",
        "-",                    # stdin
        f"s3://{s3_bucket}/{s3_key}",
        "--content-encoding", "gzip",
    ]

    logger.info("Archiving %s to s3://%s/%s", partition_name, s3_bucket, s3_key)

    pg_proc = await asyncio.create_subprocess_exec(
        *pg_dump_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    s3_proc = await asyncio.create_subprocess_exec(
        *s3_upload_cmd,
        stdin=pg_proc.stdout,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _, s3_err = await s3_proc.communicate()
    if s3_proc.returncode != 0:
        raise RuntimeError(f"S3 upload failed: {s3_err.decode()}")

    logger.info("Archived %s successfully to %s", partition_name, s3_key)


async def listen_for_archival_requests(pool: asyncpg.Pool) -> None:
    """Listen for pg_notify archive requests from the delete_before() function.

    This worker runs as a sidecar process alongside your application.
    It receives notifications when a partition is about to be deleted
    and triggers the S3 export.
    """
    async with pool.acquire() as conn:
        await conn.add_listener(
            "audit_archive_requested",
            lambda conn, pid, channel, payload: asyncio.create_task(
                _handle_archive_request(pool, payload)
            ),
        )
        logger.info("Listening for audit archive requests...")
        while True:
            await asyncio.sleep(60)


async def _handle_archive_request(pool: asyncpg.Pool, payload: str) -> None:
    try:
        data = json.loads(payload)
        partition_name = data["partition"]
        s3_bucket = os.environ["AUDIT_ARCHIVE_S3_BUCKET"]
        await export_partition_to_s3(pool, partition_name, s3_bucket)
    except Exception:
        logger.exception("Archive request failed for payload: %s", payload)
```

---

## Pattern 8 — Self-Hosted Alternatives to LangSmith

**What you are learning:** For regulated environments where you cannot send trace data to a US-based SaaS, three self-hosted alternatives cover the same observability functionality as LangSmith. Choose based on your deployment constraints.

### Comparison Table

| Feature | LangSmith | Langfuse | Phoenix (Arize) | OpenTelemetry |
|---|---|---|---|---|
| Self-hostable | Yes (Enterprise) | Yes (MIT license) | Yes (Apache 2.0) | Yes |
| EU/GDPR data residency | With self-host | With Docker | With Docker | Your infra |
| Air-gapped deployment | No | Yes | Yes | Yes |
| LangChain native | Yes | Yes | Yes | Via OTLP |
| Cost | Paid tiers | Free self-host | Free | Infrastructure only |
| Trace immutability | No | No | No | Depends on backend |
| UI for trace inspection | Yes | Yes | Yes | Grafana/Jaeger |
| Evaluation datasets | Yes | Yes | Yes (Evals) | No |
| Best for | Development | GDPR/HIPAA teams | Air-gapped / ML | Custom SIEM integration |

**Important:** None of these alternatives provide tamper-evident logs or compliance-grade immutability. They are observability tools. Use them alongside the PostgreSQL audit table from Pattern 1, not instead of it.

### Option A: Langfuse (GDPR-Compliant EU Self-Host)

Langfuse is MIT-licensed and the closest feature-equivalent to LangSmith. Deploy it in your EU infrastructure to satisfy GDPR data residency requirements.

```yaml
# docker-compose.langfuse.yml
version: "3.9"

services:
  langfuse-server:
    image: langfuse/langfuse:2
    restart: always
    ports:
      - "3000:3000"
    environment:
      DATABASE_URL: postgresql://langfuse:${POSTGRES_PASSWORD}@langfuse-db:5432/langfuse
      NEXTAUTH_URL: http://localhost:3000
      NEXTAUTH_SECRET: ${NEXTAUTH_SECRET}        # openssl rand -hex 32
      SALT: ${LANGFUSE_SALT}                     # openssl rand -hex 32
      ENCRYPTION_KEY: ${ENCRYPTION_KEY}          # openssl rand -hex 32
      TELEMETRY_ENABLED: "false"                 # disable phone-home
      LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES: "false"
    depends_on:
      langfuse-db:
        condition: service_healthy

  langfuse-db:
    image: postgres:15-alpine
    restart: always
    environment:
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: langfuse
    volumes:
      - langfuse_db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse"]
      interval: 5s
      timeout: 5s
      retries: 10

volumes:
  langfuse_db_data:
```

```bash
# .env for Langfuse
POSTGRES_PASSWORD=change-me-strong-password
NEXTAUTH_SECRET=$(openssl rand -hex 32)
LANGFUSE_SALT=$(openssl rand -hex 32)
ENCRYPTION_KEY=$(openssl rand -hex 32)
```

```python
# Langfuse integration with LangChain
# pip install langfuse

from langfuse.callback import CallbackHandler as LangfuseCallback
from dotenv import load_dotenv
import os

load_dotenv()

# Point to your self-hosted Langfuse instance
langfuse_handler = LangfuseCallback(
    public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
    secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    host="http://localhost:3000",       # your self-hosted URL
    session_id="session-123",
    user_id="user-456",
    tags=["production", "SOC2"],
)

# Use alongside ImmutableAuditCallback — one for observability, one for compliance
result = await graph.ainvoke(
    {"messages": [HumanMessage(content=query)]},
    config={
        "callbacks": [langfuse_handler, audit_callback],
        "recursion_limit": 25,
    },
)
```

### Option B: Phoenix (Arize) for Air-Gapped Deployments

Phoenix runs fully offline — no network calls to any external service. Ideal for government, defense, or clinical environments where internet access is restricted.

```bash
# Install and run Phoenix locally — zero configuration
pip install arize-phoenix openinference-instrumentation-langchain

# Start the Phoenix UI server (runs on localhost:6006)
python -m phoenix.server.main serve
```

```python
# Phoenix integration with LangChain
import phoenix as px
from openinference.instrumentation.langchain import LangChainInstrumentor

# Launch Phoenix (or connect to a running instance)
session = px.launch_app()
print(f"Phoenix UI: {session.url}")   # http://localhost:6006

# Instrument LangChain globally — all calls are traced automatically
LangChainInstrumentor().instrument()

# No changes needed to graph.invoke() — Phoenix captures everything via
# OpenTelemetry auto-instrumentation.

# For production (non-interactive), set the collector endpoint:
import os
os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = "http://phoenix-host:6006"
```

### Option C: OpenTelemetry OTLP Export (Custom SIEM / Grafana)

If your organization already has a Grafana/Jaeger/Tempo stack for observability, route LangChain traces into it via OpenTelemetry. This avoids a separate observability database.

```python
# pip install opentelemetry-sdk opentelemetry-exporter-otlp openinference-instrumentation-langchain

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from openinference.instrumentation.langchain import LangChainInstrumentor

# Configure OTLP exporter pointing at your Grafana Tempo / Jaeger collector
exporter = OTLPSpanExporter(
    endpoint="http://your-otel-collector:4317",   # gRPC endpoint
    # headers={"Authorization": f"Bearer {token}"}  # if auth required
)

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

# Instrument LangChain — all calls become OpenTelemetry spans
LangChainInstrumentor().instrument()

# graph.invoke() now emits OTLP spans to your existing observability stack.
# View in Grafana Tempo, Jaeger, or any OTLP-compatible backend.
```

```yaml
# docker-compose.otel-collector.yml — minimal OpenTelemetry Collector config
# for teams that want OTLP → Grafana Tempo

version: "3.9"
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.95.0
    ports:
      - "4317:4317"   # gRPC
      - "4318:4318"   # HTTP
    volumes:
      - ./otel-config.yaml:/etc/otelcol-contrib/config.yaml

  tempo:
    image: grafana/tempo:2.4.0
    ports:
      - "3200:3200"

  grafana:
    image: grafana/grafana:10.3.0
    ports:
      - "3001:3000"
```

---

## Setup Instructions

```bash
# Install dependencies
pip install asyncpg langchain-core langchain-anthropic langgraph langsmith \
            python-dotenv pyjwt langfuse arize-phoenix \
            openinference-instrumentation-langchain \
            opentelemetry-sdk opentelemetry-exporter-otlp
```

```python
# audit/pool.py — shared asyncpg pool for the application

import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

_pool: asyncpg.Pool | None = None


async def get_db_pool() -> asyncpg.Pool:
    """Return the shared asyncpg connection pool.

    Create once at application startup; reuse across all requests.
    The pool uses the audit_app_role credentials — INSERT/SELECT only.
    """
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.environ["AUDIT_DATABASE_URL"],
            # AUDIT_DATABASE_URL=postgresql://myapp_db_user:pass@localhost:5432/mydb
            min_size=2,
            max_size=10,
            command_timeout=5.0,    # audit writes must not block for long
        )
    return _pool


async def close_db_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
```

```bash
# .env (never commit to git)
ANTHROPIC_API_KEY=sk-ant-...
AUDIT_DATABASE_URL=postgresql://myapp_db_user:strongpass@localhost:5432/mydb
AUDIT_ARCHIVE_S3_BUCKET=my-audit-archive-bucket

# LangSmith (optional — for development debugging alongside audit log)
LANGSMITH_API_KEY=ls__...
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=my-project

# Langfuse (if using self-hosted option)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

---

## Compliance Checklist

Use this checklist before submitting audit evidence to a compliance framework.

### SOC2 Type II (CC6.1, CC6.2, CC7.1)

```
[ ] audit.llm_calls table exists with INSERT-only app role
[ ] REVOKE DELETE and REVOKE UPDATE confirmed via pg_roles query
[ ] Hash chain verified clean (AuditChainVerifier.verify_chain() passes)
[ ] user_id is non-null on all rows (verify: SELECT COUNT(*) FROM audit.llm_calls WHERE user_id IS NULL)
[ ] Retention policy pg_cron job scheduled and tested
[ ] Archival worker running and tested with a sample partition
[ ] Monthly partition creation scheduled (cron job)
[ ] LangSmith traces NOT used as primary compliance evidence (use audit table)
[ ] Chain verification runs as a daily scheduled job
[ ] Alert configured for chain verification failures
```

### HIPAA (§164.312(b) Audit Controls)

```
[ ] All 6 fields required by HIPAA audit standard present: user_id, created_at,
    model, run_id, prompt_hash, completion_hash
[ ] BAA signed with any third-party services receiving trace data
[ ] Data residency confirmed (US or EU depending on patient location)
[ ] 6-year retention enforced via pg_cron delete_before() job
[ ] GDPR-style data subject query available (Pattern 6, GDPR query)
[ ] Rationale captured for any AI-assisted clinical decision routing
[ ] PHI never stored in prompt_hash or completion_hash fields — only hashes
```

### PCI DSS Requirement 10

```
[ ] All access to cardholder data environment tagged with framework='PCI'
[ ] 1-year online retention enforced
[ ] 3-year archive in S3/GCS confirmed
[ ] Daily log review query scheduled (high-cost-calls view)
[ ] Failed tool call view reviewed weekly
[ ] Log integrity (hash chain) verified monthly
```

### EU AI Act (High-Risk AI Systems)

```
[ ] rationale_json populated for all routing decisions
[ ] model_version field populated (not just model name)
[ ] human_review_required field present in rationale schema
[ ] Decision traceability query (Pattern 6, EU AI Act query) returns complete records
[ ] User attribution present on 100% of rows
```

---

## Common Mistakes

| Mistake | Fix |
|---|---|
| Using LangSmith traces as compliance evidence | LangSmith is for development. The PostgreSQL audit table is your compliance record. |
| Storing raw prompts in audit table | Store SHA-256 hashes only — raw prompts may contain PII |
| Missing `user_id` on anonymous/batch calls | Use a system identifier like `"batch-job:etl-pipeline"` — never leave NULL |
| Blocking the main thread on audit writes | Always use `asyncio.create_task()` — fire-and-forget |
| Forgetting to revoke DELETE explicitly | Add `REVOKE DELETE ON audit.llm_calls FROM audit_app_role` to every migration |
| Hash chain verification only on-demand | Run it daily via cron — detecting a breach 6 months late defeats the purpose |
| Dropping partitions without archiving | Call `archive_partition_before_delete()` in the same transaction |
| Single-role database credentials | Separate app role (INSERT/SELECT) from reader role (SELECT only) from migration role (DDL) |
| Not partitioning the audit table | A 3-year-old unpartitioned table with 500M rows takes hours to retention-prune |
| Rationale captured after the decision | Capture rationale BEFORE the routing decision executes — after is too late for traceability |
