"""Instinct governance models: approval/version history + a proper proposal
queue (P4.2c / P4.2d / P4.3b of the convergence plan).

Bucket: InstinctApproval, InstinctVersion, InstinctProposal.

``InstinctApproval`` and ``InstinctVersion`` are append-only audit trails
keyed off ``instincts.id`` — every promotion, rollback, or future edit of an
``Instinct`` row should leave a matching version snapshot and/or approval
decision here rather than silently overwriting history in place.

``InstinctProposal`` is a separate pending-review queue: a proposed pattern
lives here (status="pending") until a human approves it, at which point (out
of scope for this module — see ``tools/instinct_governance.py``'s
``propose_instinct`` docstring) an ``Instinct`` row gets created and this
row's status flips to "approved"/"rejected". Proposals never touch the
``instincts`` table directly.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, _now, _uuid


class InstinctApproval(Base):
    """One human approval/rejection decision against an ``Instinct`` row.

    Written on promotion (decision="approved"), on rollback
    (decision="rejected"), and by any future dual-control approval flow.
    Append-only — never updated or deleted.
    """

    __tablename__ = "instinct_approvals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    instinct_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instincts.id"), index=True)
    approved_by: Mapped[str] = mapped_column(String(128))
    decision: Mapped[str] = mapped_column(String(16))  # "approved" | "rejected"
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class InstinctVersion(Base):
    """A point-in-time snapshot of an ``Instinct`` row's pattern/confidence.

    Written on every change to an instinct's content (initial promotion is
    version=1; later edits would append version=2, 3, ...). Append-only —
    never updated or deleted; this is the audit trail that makes
    ``get_instinct_history`` possible.
    """

    __tablename__ = "instinct_versions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    instinct_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instincts.id"), index=True)
    version: Mapped[int] = mapped_column()
    pattern: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    changed_by: Mapped[str] = mapped_column(String(128))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InstinctProposal(Base):
    """A proposed pattern awaiting human approval before it becomes an
    ``Instinct``. Distinct from ``IntegrationProposal`` (core.py) — this is
    specifically the instinct-promotion review queue (P4.3b)."""

    __tablename__ = "instinct_proposals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    zone: Mapped[str] = mapped_column(String(32))
    domain: Mapped[str] = mapped_column(String(64))
    pattern: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    citation: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_by: Mapped[str] = mapped_column(String(128))
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|approved|rejected
