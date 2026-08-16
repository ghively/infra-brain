"""Core domain models: generic infrastructure records, knowledge/instincts,
UI users, and shared operational tables.

Bucket: Resource, CollectionRun, Snapshot, ResourceConfig, DriftEvent,
        ScanPoint, AuditLog, Instinct, Observation, Document, JiraTicket,
        ConfluencePage, IntegrationProposal, ProposedAction, RootCauseNote,
        ImportedSkill, PolicyRegistry, Workspace, UIUser, CustomView,
        HostIdentity, GeneratedScript.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import JSONB, Base, _now, _uuid

# Embedding vector dimension for the RAG knowledge store. MUST equal both
# ``Settings.embedding_dim`` (its default) and the ``document_chunks.embedding``
# pgvector column dimension. A test cross-checks these three stay in sync, and
# an alembic migration imports this via ``from infra_brain.db.models import
# _EMBED_DIM`` to build the HNSW index at the right width.
_EMBED_DIM = 1024

# Canonical zone slugs. ZONE_CORPORATE is the single source of truth for the
# "corpor" literal — every reader/writer of Resource.zone / Instinct.zone /
# CloudResource-family zone columns must import this constant rather than
# hardcoding "corpor" or (worse) the stale "corporate" spelling, which
# silently breaks the learning loop's write/read round-trip (Phase 4).
ZONE_CORPORATE = "corpor"

# Repair migration 0024_zone_canonicalize sets a real DB-level
# ``ALTER COLUMN zone SET DEFAULT 'corpor'`` on resources/instincts/
# net_discovery_hosts, but the ORM never declared a matching server_default --
# only the Python-side default= above. That mismatch is real, pre-existing
# schema drift the migration-parity gate's compare_server_default check (added
# for TRK-270) would otherwise flag on every future MR. This constant lets all
# three model declarations state the truth once.
# Plain string literal, no dialect-specific cast: an explicit
# ``::character varying`` (this repo's first attempt) is valid Postgres DDL
# but invalid SQLite DDL ("unrecognized token"), and the full local suite
# runs on sqlite via Base.metadata.create_all() -- a portable literal default
# works identically on both and is all a VARCHAR column needs.
_ZONE_SERVER_DEFAULT = text(f"'{ZONE_CORPORATE}'")


class Resource(Base):
    __tablename__ = "resources"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    domain: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(64))
    zone: Mapped[str] = mapped_column(
        String(32), default=ZONE_CORPORATE, server_default=_ZONE_SERVER_DEFAULT
    )
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    snapshots: Mapped[list["Snapshot"]] = relationship(back_populates="resource")
    drift_events: Mapped[list["DriftEvent"]] = relationship(back_populates="resource")


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    domain: Mapped[str] = mapped_column(String(64))
    trigger_type: Mapped[str] = mapped_column(String(32))
    trigger_source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resources_found: Mapped[int] = mapped_column(Integer, default=0)
    detail_rows_written: Mapped[int] = mapped_column(Integer, default=0)
    drift_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="in_progress")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 2 Task 1 (orchestration v2.1 groundwork): correlates every
    # CollectionRun spawned by one sweep-graph invocation. Nullable — legacy/
    # ad-hoc runs (manual dispatch, on-demand agents) never set it. Indexed so
    # the (not-yet-built) sweep graph can cheaply fetch "all runs for sweep X".
    sweep_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    snapshots: Mapped[list["Snapshot"]] = relationship(back_populates="run")


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    # Nullable by design: discovery partial-progress checkpoints (F-005) are
    # run-scoped snapshots with no owning resource. Ordinary snapshots always
    # set resource_id; run_id stays NOT NULL for both kinds.
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collection_runs.id"))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resource: Mapped["Resource"] = relationship(back_populates="snapshots")
    run: Mapped["CollectionRun"] = relationship(back_populates="snapshots")


class ResourceConfig(Base):
    __tablename__ = "resource_configs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collection_runs.id"))
    key: Mapped[str] = mapped_column(String(256))
    value: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DriftEvent(Base):
    __tablename__ = "drift_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"))
    collection_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("collection_runs.id"), nullable=True
    )
    # GitLab #163: widened 32 -> 64. The identity-conflict split introduced
    # sub-classed drift types ("identity_conflict_distinct_object" is 33 chars),
    # which VARCHAR(32) would silently truncate on sqlite and hard-reject on
    # PostgreSQL. Widening is additive and needs no data backfill.
    drift_type: Mapped[str] = mapped_column(String(64))
    field: Mapped[str] = mapped_column(String(256))
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # GitLab #163/#164: FIRST-observation timestamp semantics for detected_at are
    # now enforced by the writers — ``detected_at`` stays immutable for as long as
    # the finding's data is unchanged, and ``last_seen_at`` carries "this open
    # finding was re-observed by this sweep". Nullable because rows written before
    # this column existed have no re-observation history; every recency-sorted
    # reader must therefore use ``coalesce(last_seen_at, detected_at)`` while
    # age/staleness scoring keeps using ``detected_at`` alone (that is precisely
    # what makes staleness scoring meaningful once detected_at stops being bumped).
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # GitLab #164: queryable, indexable dedup identity for findings whose natural
    # key lives inside the ``new_value`` JSONB blob (e.g. the IaC secret scanner's
    # (secret_type, file_path) pair). Nullable — writers that already dedup on
    # real columns (resource_id/drift_type/field) leave it unset.
    dedup_key: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="open")
    jira_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # TRK-242 (GitLab #113): incident-management correlation key. Set when an
    # ops-alert for this event is sent with a dedup_key (tools/ops_webhook.py
    # send_ops_alert), so a PagerDuty/Opsgenie-style ack callback
    # (POST /webhooks/incident/ack) can find its way back to this row without
    # a new outbound mechanism — mirrors jira_key's existing pattern above.
    incident_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    resource: Mapped["Resource"] = relationship(back_populates="drift_events")


def drift_recency() -> Any:
    """The ordering key for any "most recently observed drift first" listing.

    ``coalesce(last_seen_at, detected_at)`` — GitLab #163/#164. Since writers
    stopped bumping ``detected_at`` on every re-observation, ordering by
    ``detected_at`` alone would bury a finding that is still actively re-observed
    beneath a newer one that fired once and went quiet. ``last_seen_at`` is
    NULL for rows written before it existed, hence the coalesce.

    Deliberately NOT for age/staleness scoring — that must keep using
    ``detected_at`` alone (see ``agents/compliance_rules.py::_rule_staleness``),
    which is precisely what the immutable-``detected_at`` change makes correct.
    """
    return func.coalesce(DriftEvent.last_seen_at, DriftEvent.detected_at)


class ScanPoint(Base):
    __tablename__ = "scan_points"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    domain: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(512))
    schedule: Mapped[str] = mapped_column(String(64))
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    agent: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(128))
    input_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    denial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Instinct(Base):
    __tablename__ = "instincts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    zone: Mapped[str] = mapped_column(
        String(32), default=ZONE_CORPORATE, server_default=_ZONE_SERVER_DEFAULT
    )
    domain: Mapped[str] = mapped_column(String(64))
    pattern: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    promoted_by: Mapped[str] = mapped_column(String(128))
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    citation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # P4.2b (convergence plan): governed-instinct extension columns. All
    # nullable or server-defaulted so the 200+ existing rows are unaffected —
    # this is a live table, additive-only. `source` records where the
    # evidence for THIS instinct came from (e.g. "drift_learning",
    # "manual_review"), distinct from `promoted_by` (who promoted it) and
    # `proposed_by` (who proposed it before promotion, if the propose/promote
    # split from P4.2d/P4.3b is in play).
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    status: Mapped[str] = mapped_column(
        String(32), default="active", server_default=text("'active'")
    )
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    proposed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    agent: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(128))
    domain: Mapped[str] = mapped_column(String(64))
    pattern: Mapped[str] = mapped_column(Text)
    count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("agent", "tool", "domain", name="uq_observation_agent_tool_domain"),
    )


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(512))
    # Vestigial per D7 (convergence plan): the four-class sensitivity
    # taxonomy (public/internal/chd-adjacent/key-material) and its local-only
    # lane are dropped. Column is kept, not removed, to avoid a breaking
    # schema change / data-migration decision outside this task's scope.
    sensitivity: Mapped[str] = mapped_column(String(32), default="internal")
    source: Mapped[str] = mapped_column(String(512))
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Provenance + incremental re-index columns (TRK-067 RAG revival). All
    # nullable so existing rows are unaffected; used to skip-unchanged pages,
    # track Confluence source versions, and mark rows "stale" (never delete)
    # when a source page disappears.
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    space: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_updated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="current")
    # P4.2h (convergence plan, D7): client-origin provenance columns, added
    # in place of the dropped sensitivity taxonomy. Both nullable — existing
    # rows (and any writer that hasn't been updated yet) are unaffected.
    client_origin: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ingested_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class DocumentChunk(Base):
    """A single embedded text chunk of a :class:`Document` (RAG knowledge store).

    The ``embedding`` column is dialect-aware: a pgvector ``Vector`` on
    PostgreSQL (production) and plain JSON on SQLite (tests), via the
    ``with_variant`` idiom. The HNSW index is declared here so migration-parity
    sees ORM and migration agree; the ``postgresql_*`` kwargs are ignored on
    SQLite (where it degrades to a plain index over the JSON column).
    """

    __tablename__ = "document_chunks"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(_EMBED_DIM).with_variant(JSON(), "sqlite"), nullable=True
    )
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# ``create_all`` paths (tests, fresh dev DBs) must have the pgvector extension
# before ``document_chunks`` is emitted; the alembic migration covers the
# production upgrade path separately. Postgres-only via ``execute_if``.
event.listen(
    Base.metadata,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS vector").execute_if(dialect="postgresql"),
)


# ---------------------------------------------------------------------------
# TRK-297 R6 — hybrid retrieval's PostgreSQL-only full-text side
# ---------------------------------------------------------------------------
# ``document_chunks.text_tsv`` is a DELIBERATELY UNMODELED column: it exists on
# PostgreSQL only and is NOT declared as a ``mapped_column`` on
# :class:`DocumentChunk`.
#
# Why not model it. Its real definition is
# ``GENERATED ALWAYS AS (to_tsvector('english', ...)) STORED``, and neither half
# of that survives the ORM:
#   * SQLite has no ``to_tsvector``. It *does* support generated columns, and it
#     validates the expression at ``CREATE TABLE`` time — so a ``Computed(...)``
#     on the model breaks ``Base.metadata.create_all()`` on SQLite, i.e. the
#     entire test suite and ``/pg-gate-check``. This is the exact blocker that
#     kept R6 deferred.
#   * Declaring it *without* ``Computed`` (a plain nullable column, generated
#     only in the migration) type-checks on both dialects but is worse: alembic's
#     ``_compare_computed_default`` then emits
#     ``Computed default on document_chunks.text_tsv cannot be modified`` on
#     EVERY ``compare_metadata`` run — the migration-parity gate, ``alembic
#     check``, and the runtime ``assert_schema_current`` deploy gate — permanently
#     normalising a warning class that is supposed to mean something. Worse
#     still, a future ``--autogenerate`` would see a plain column and happily
#     propose recreating it *without* the GENERATED clause.
#
# So the column (and its GIN index) live outside ``Base.metadata`` entirely,
# exactly like ``ix_resources_name_trgm`` — see ``_UNMODELED_INDEXES`` /
# ``_UNMODELED_COLUMNS`` in ``db/schema_check.py`` and the mirror entries in
# ``alembic/env.py``. All four comparison configs must stay aligned.
#
# The DDL is emitted from two places, which MUST stay identical (a static test,
# ``tests/test_rag_hybrid_ddl_parity.py``, asserts the migration file carries
# these exact strings):
#   1. the ``after_create`` listener below — the ``create_all`` path (gate-4
#      ORM tests on real PostgreSQL, fresh dev DBs), mirroring how the pgvector
#      extension is handled above; and
#   2. alembic migration ``2a7f1c9e4d30`` — the production upgrade path.
# Both are ``IF NOT EXISTS``-guarded so either may run second harmlessly.
#
# ``to_tsvector('english', ...)`` with a *literal* regconfig is IMMUTABLE, which
# is what makes it legal in a generated column; the one-argument form (which
# reads ``default_text_search_config``) is not and would be rejected.
DOCUMENT_CHUNKS_TSV_COLUMN = "text_tsv"
DOCUMENT_CHUNKS_TSV_INDEX = "ix_document_chunks_text_tsv"
DOCUMENT_CHUNKS_TSV_EXPR = "to_tsvector('english', coalesce(text, ''))"

DOCUMENT_CHUNKS_TSV_ADD_COLUMN_DDL = (
    "ALTER TABLE document_chunks "
    f"ADD COLUMN IF NOT EXISTS {DOCUMENT_CHUNKS_TSV_COLUMN} tsvector "
    f"GENERATED ALWAYS AS ({DOCUMENT_CHUNKS_TSV_EXPR}) STORED"
)
# Plain (non-CONCURRENT) CREATE INDEX is correct here: the compose ``migration``
# service runs the chain with the app stopped, so nothing is reading or writing
# document_chunks while the index builds, and CONCURRENTLY would additionally
# force the migration out of alembic's transaction (autocommit block) for a
# table holding thousands of rows at most. See the migration's danger review.
DOCUMENT_CHUNKS_TSV_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {DOCUMENT_CHUNKS_TSV_INDEX} "
    f"ON document_chunks USING gin ({DOCUMENT_CHUNKS_TSV_COLUMN})"
)

for _tsv_ddl in (DOCUMENT_CHUNKS_TSV_ADD_COLUMN_DDL, DOCUMENT_CHUNKS_TSV_INDEX_DDL):
    event.listen(
        DocumentChunk.__table__,
        "after_create",
        DDL(_tsv_ddl).execute_if(dialect="postgresql"),
    )
del _tsv_ddl


class JiraTicket(Base):
    __tablename__ = "jira_tickets"
    drift_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("drift_events.id"), primary_key=True
    )
    jira_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ConfluencePage(Base):
    __tablename__ = "confluence_pages"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    domain: Mapped[str] = mapped_column(String(64))
    page_id: Mapped[str] = mapped_column(String(64))
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("domain", "page_id"),)


class IntegrationProposal(Base):
    __tablename__ = "integration_proposals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(256))
    type: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(512))
    confidence: Mapped[float] = mapped_column(Float)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProposedAction(Base):
    """A human-gated action proposed by an agent (e.g. a drift remediation).
    Generic approval primitive — never auto-applied; status drives the flow."""

    __tablename__ = "proposed_actions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    agent: Mapped[str] = mapped_column(String(64))
    action_type: Mapped[str] = mapped_column(String(64))  # e.g. "config_fix"
    target: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    status: Mapped[str] = mapped_column(
        String(32), default="pending"
    )  # pending|approved|rejected|executed
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Phase 3 Task 1: interrupt-backed remediation groundwork. All three are
    # nullable-only — chosen over a payload-JSONB blob because Task 5's
    # retention protection query needs an INDEXED thread_id to efficiently
    # find every ProposedAction tied to a live LangGraph thread before it is
    # pruned. interrupt_id/graph_version are plain metadata (which interrupt()
    # call produced this proposal, and which graph version handled it) with
    # no query-shape requirement, so no index for those two.
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    interrupt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    graph_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # idempotency: at most one open proposal per (action_type, target)
    __table_args__ = (
        UniqueConstraint(
            "action_type", "target", "status", name="uq_proposed_action_target_status"
        ),
        Index("ix_proposed_actions_thread_id", "thread_id"),
    )


class RootCauseNote(Base):
    """A correlation explanation attached to a drift event by RootCauseAgent."""

    __tablename__ = "root_cause_notes"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    drift_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("drift_events.id"))
    explanation: Mapped[str] = mapped_column(Text, default="")
    correlated: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("drift_event_id", name="uq_rootcause_drift"),)


class ImportedSkill(Base):
    __tablename__ = "imported_skills"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    source_repo: Mapped[str] = mapped_column(String(512))
    skill_name: Mapped[str] = mapped_column(String(128))
    converted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    tool_path: Mapped[str] = mapped_column(String(512))
    active: Mapped[bool] = mapped_column(Boolean, default=False)


class PolicyRegistry(Base):
    __tablename__ = "policy_registry"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    rule_file: Mapped[str] = mapped_column(String(512), unique=True)
    delivery_layers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    pci_control_mapping: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    mr_id: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(512))
    branch: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    gc_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    last_accessed: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UIUser(Base):
    """Dashboard user with a bcrypt password hash for streamlit-authenticator."""

    __tablename__ = "ui_users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="viewer")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class McpApiKey(Base):
    """DB-backed, per-key scoped MCP API key (replaces the single global
    INFRA_BRAIN_MCP_TOKEN). The raw token is NEVER stored — only its sha256
    hex digest. ``allowed_tools`` scopes the key to a set of MCP tool names;
    a key is ACTIVE when ``revoked_at IS NULL`` **and** it has not passed
    ``expires_at``. ``last_used_at`` is a best-effort, non-blocking touch
    updated on each authenticated call."""

    __tablename__ = "mcp_api_keys"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    allowed_tools: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_by: Mapped[str] = mapped_column(String(128), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # TRK-160: optional expiry. NULL = never expires, which is deliberately the
    # default so every key minted before this column existed keeps working
    # unchanged — this is an opt-in bound on a leaked key's useful life, not a
    # retroactive policy. Enforced in mcp_auth.lookup_active_key alongside
    # revoked_at (NOT here — a column cannot enforce anything), so an expired
    # key and a revoked key are the same 401 to the caller.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomView(Base):
    """User-authored OpenUI view: stores the prompt, rendered lang, and share token."""

    __tablename__ = "custom_views"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ui_users.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    openui_lang: Mapped[str] = mapped_column(Text, nullable=False)
    share_token: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        # NOTE: ``share_token`` is ``unique=True`` above, so Postgres already
        # maintains a unique index on it — a second plain btree index here was
        # pure redundancy (MR-F / migration 0027 dropped it).
        Index("ix_custom_views_user_id", "user_id"),
    )


class HostIdentity(Base):
    """Cross-source canonical host record — links the same physical/virtual
    machine across Rapid7, vSphere, Octopus, Linux/Windows collectors.

    Populated and maintained by HostReconcileAgent (every 30 min). One row per
    unique short hostname; the five nullable resource_id FKs point to the
    canonical Resource row for each source domain. Denormalized display fields
    are refreshed on each reconcile run to avoid join-heavy dashboard queries.
    """

    __tablename__ = "host_identities"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    # Normalized lowercase short hostname — the canonical merge key.
    short_hostname: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    fqdn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_addresses: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # Links to domain tables via the canonical Resource row.
    # KG-9 (2026-08-10): every source leg below is a FOREIGN KEY that was
    # unindexed. Postgres does NOT create an index for a FK automatically (only
    # for the PK / UNIQUE side), and these nine columns are precisely the join
    # path identity reconciliation walks — `host_reconcile` looks a host up by
    # each source's resource_id on every pass, and `blast_radius`/`get_host_
    # profile` traverse them per request. Unindexed, each is a sequential scan.
    # Cheap now (7 rows); the point is that it stops being cheap silently.
    #
    # SCOPE NOTE: a repo-wide audit found 55 unindexed FK columns, not nine.
    # The other 46 are filed as TRK-330 rather than swept in here — indexing
    # large detail tables (linux_packages.host_id, vuln_queue.resource_id …)
    # needs CREATE INDEX CONCURRENTLY and its own risk assessment, which is a
    # different change from closing the knowledge-graph identity path.
    r7_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    vsphere_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    octopus_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    linux_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    windows_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    # KG-6: identity reconciliation extended to netdiscovery/net, cloud, k8s.
    net_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    cloud_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    k8s_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    # KG-4: NetDevice was the only cloud/net-ish domain table omitted from
    # identity reconciliation, confirmed during the 2026-07-23 KG audit.
    netdevice_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id"), nullable=True, index=True
    )
    # Denormalized fields for quick display (no join needed from dashboard).
    os_family: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vuln_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    patch_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vsphere_power_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    octopus_machine_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_reconciled: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: KG-2: source labels (``r7``, ``vsphere``, …) whose leg for this
    #: ``short_hostname`` had a SAME-SOURCE identity collision in the MOST
    #: RECENT reconcile run — two rows from one source table normalizing to this
    #: hostname, where first-write-wins kept a coin-flip survivor (see
    #: ``HostReconcileAgent._note_identity_collision``).
    #:
    #: RECOMPUTED every run, never accumulated: a leg that no longer collides is
    #: cleared, so a resolved ambiguity self-heals within the 30-minute reconcile
    #: cadence. An ambiguity flag that outlived its own resolution would be both
    #: a permanent false alarm and — because the emitters refuse to link an
    #: unsettled leg — a permanent hole in the graph.
    #:
    #: Consumed by ``graph_phase3._score_candidate`` (TRK-341, via
    #: ``ambiguous_leg_index``), where an unsettled leg is counter-evidence
    #: that lowers the pair's score and diverts it to review instead of
    #: auto-emitting a SAME_AS, and by dashboard/``get_host_profile`` readers.
    #: (host_reconcile's own ``_emit_is_same_as_edges`` /
    #: ``_emit_cross_hostname_ip_edges``, which declined to assert IS_SAME_AS
    #: over an unsettled leg, were deleted in the P5 resolver switch — the
    #: refusal now lives in the resolver's scoring.) Before this column
    #: existed the flag
    #: lived only on the in-memory merged dict and died when ``run()`` returned,
    #: so the docstring promising downstream visibility was false as written.
    identity_ambiguous_sources: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    __table_args__ = (UniqueConstraint("short_hostname", name="uq_host_identities_short_hostname"),)


class GeneratedScript(Base):
    """Persisted record of every script generated and/or run by the agent (never deleted)."""

    __tablename__ = "generated_scripts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(256))
    language: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_by_agent: Mapped[str] = mapped_column(String(128))
    domain: Mapped[str] = mapped_column(String(64))
    git_repo: Mapped[str] = mapped_column(String(512), default="")
    git_path: Mapped[str] = mapped_column(String(512), default="")
    git_commit_sha: Mapped[str] = mapped_column(String(64), default="")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    last_stdout: Mapped[str] = mapped_column(Text, default="")
    last_returncode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reusable: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# Hot-path indexes for core tables

# resources: frequent filter by domain, domain+type, and name lookups
Index("ix_resources_domain", Resource.domain)
Index("ix_resources_domain_type", Resource.domain, Resource.type)
Index("ix_resources_name", Resource.name)

# collection_runs: filter by domain, status, and time-range queries
Index("ix_collection_runs_domain", CollectionRun.domain)
Index("ix_collection_runs_status", CollectionRun.status)
Index("ix_collection_runs_started_at", CollectionRun.started_at)

# snapshots / resource_configs: retention pruning by age + run cascade (F-030)
Index("ix_snapshots_collected_at", Snapshot.collected_at)
Index("ix_snapshots_run_id", Snapshot.run_id)
Index("ix_resource_configs_collected_at", ResourceConfig.collected_at)
# resource_configs.run_id backs the retention run-cascade DELETE
# (retention.prune_expired: delete(ResourceConfig).where(run_id.in_(...))) — MR-F.
Index("ix_resource_configs_run_id", ResourceConfig.run_id)

# drift_events: dashboards filter by status; time-range and resource_id joins
Index("ix_drift_events_status", DriftEvent.status)
Index("ix_drift_events_detected_at", DriftEvent.detected_at)
Index("ix_drift_events_resource_id", DriftEvent.resource_id)
# collection_run_id backs the retention run-cascade UPDATE that detaches
# drift_events from expired runs (update(DriftEvent).where(collection_run_id.in_(...))) — MR-F.
Index("ix_drift_events_collection_run_id", DriftEvent.collection_run_id)

# audit_log: time-ordered queries and per-agent filtering
Index("ix_audit_log_ts", AuditLog.ts)
Index("ix_audit_log_agent", AuditLog.agent)

# observations: the (agent, tool) composite index was confirmed unused by any
# query (no aggregation reads it — pattern rollups scan by time) and dropped in
# MR-F / migration 0027.

# generated_scripts: dedup by hash, filter by language and agent (Task F1)
Index("ix_generated_scripts_content_sha256", GeneratedScript.content_sha256)
Index("ix_generated_scripts_language", GeneratedScript.language)
Index("ix_generated_scripts_created_by_agent", GeneratedScript.created_by_agent)


__all__ = [
    "_EMBED_DIM",
    "AuditLog",
    "CollectionRun",
    "ConfluencePage",
    "CustomView",
    "Document",
    "DocumentChunk",
    "DriftEvent",
    "GeneratedScript",
    "HostIdentity",
    "ImportedSkill",
    "Instinct",
    "IntegrationProposal",
    "JiraTicket",
    "McpApiKey",
    "Observation",
    "PolicyRegistry",
    "ProposedAction",
    "Resource",
    "ResourceConfig",
    "RootCauseNote",
    "ScanPoint",
    "Snapshot",
    "UIUser",
    "Workspace",
]
