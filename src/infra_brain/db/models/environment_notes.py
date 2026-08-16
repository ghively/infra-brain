"""Environment notes — human-written narrative layer (P4.2a / P4.3a).

A small (~25-entries-in-practice) table of free-text operator notes about the
environment that supplement the structured resource/drift/eol queries —
things a human knows and wants recorded ("the staging DB host is scheduled
for decommission next quarter", "ignore drift on netdev-07, it's mid-RMA")
that don't map onto any existing structured column.

``author``/``resolved_by`` are server-derived attribution strings (see
``tools/environment_notes.py``), never raw caller-supplied free text — same
contract as ``RootCauseNote.correlated["authored_by"]`` and
``IntegrationProposal.approved_by`` elsewhere in this bucket.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, _now, _uuid


class EnvironmentNote(Base):
    """One human-authored note. ``status`` drives the open/resolved lifecycle."""

    __tablename__ = "environment_notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    note: Mapped[str] = mapped_column(Text)
    # Server-derived attribution string — NOT a free-form caller-supplied
    # value. See ``tools/environment_notes.record_environment_note``.
    author: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # open | resolved
    status: Mapped[str] = mapped_column(String(32), default="open")
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["EnvironmentNote"]
