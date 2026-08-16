"""Hash-chained, append-only governance ledger (P4.2g / part of P4.3d).

This is a NEW property versus the existing governance surfaces in this repo:

* ``AgentActionLog`` (``db/models/governance.py``) — durable per-tool-call rows
  written by ``AuditCallbackHandler``. Independent rows, no chaining.
* ``agent/scripts/hooks/governance_ledger.py`` — the plugin-side JSONL hook
  ledger. Fingerprints the command with a *truncated* sha256 (16 hex chars) for
  privacy; records are independent lines, NOT hash-chained.

``GovernanceEvent`` adds real hash-chaining on top of those: every row's
``hash`` covers its own payload PLUS the immediately preceding row's ``hash``,
so tampering with any row (or deleting/reordering one) breaks the chain from
that point forward — this is verified by
``infra_brain.tools.governance_events.verify_governance_chain``, not just
asserted here.

Table name: ``governance_events``.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ._base import JSONB, Base, _now, _uuid


class GovernanceEvent(Base):
    """One append-only, hash-chained governance ledger row.

    ``hash`` is computed by ``record_governance_event`` (the only writer) as
    ``sha256(prev_hash_or_empty_string + event_type +
    json.dumps(payload, sort_keys=True) + created_at.isoformat())``, hex
    digest. ``prev_hash`` is the ``hash`` of the row that was most recent
    (by ``created_at``) at write time, or ``None`` for the very first row
    ever written to this table.

    This model intentionally exposes no update/delete path — rows are
    inserted only, by ``infra_brain.tools.governance_events`` functions, which
    never call ``session.delete`` or mutate an existing row's columns.
    """

    __tablename__ = "governance_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    client_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Null only for the first row ever inserted into this table.
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# governance_events: filter by client_id, and walk the chain in created_at order.
Index("ix_governance_events_client_id", GovernanceEvent.client_id)
Index("ix_governance_events_created_at", GovernanceEvent.created_at)


__all__ = ["GovernanceEvent"]
