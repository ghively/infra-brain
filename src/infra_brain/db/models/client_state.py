"""Client-local state persistence for infra-ops (P4.2e/P4.2f, convergence plan).

The infra-ops plugin's ``scripts/lib/state_store.py`` persists client-local
state as per-collection JSON files under ``.infra-ops/state-store/`` (one file
per client machine, no shared backend). The convergence-plan audit found the
ONLY real caller of its generic "add to a collection" API is
``scripts/hooks/observe_runner.py`` writing to a ``wave_failures`` collection
(``store.wave_failures.add({...})``), plus ``scripts/hooks/session_debrief.py``'s
own separate raw-JSONL append to ``sessions.jsonl`` (bypassing the Collection
API entirely). Both are per-client-machine facts a shared backend can usefully
centralize — but there is no third real caller, so this table is deliberately
scoped to those two use cases (``collection`` values ``"wave_failures"`` and
``"session_debrief"``) rather than built as a fully generic KV store on
speculation. See ``tools/client_state.py`` for the writer functions and their
collection allowlist.

Neither real caller has a caller-supplied natural dedup key: state_store's
``Collection.add()`` auto-generates a fresh ``id`` (timestamp + random hex) on
every call for ``wave_failures``, and ``session_debrief``'s JSONL append has no
id field at all (each Stop-hook firing is its own line, never updated). So
there is no stable ``(collection, entry_id, client_id)`` a real caller would
ever re-submit for a genuine idempotent-upsert — a unique constraint on that
tuple would just be inert. Accordingly this table indexes (collection,
client_id) for the real read pattern ("this client's entries in this
collection") and (collection, entry_id) for point lookups, without asserting
a uniqueness guarantee the data doesn't actually have.

``ClientObservation`` is a companion, deliberately-NOT-deduped per-event table
for ``observe_runner.py``'s tool-usage observations (mirrors the shape of its
``record_observation()`` — agent/tool/domain plus a JSON event blob). This is
NOT the same as the existing ``Observation`` model in ``core.py``, which is a
counter keyed by a ``UniqueConstraint("agent", "tool", "domain")`` — every
``ClientObservation`` call is its own row so per-event detail (timestamps,
topic classification, filenames) survives instead of collapsing into a count.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ._base import JSONB, Base, _now, _uuid


class ClientStateEntry(Base):
    """One entry written by a client machine into a scoped state collection.

    ``collection`` is one of the infra-ops ``state_store.py`` collection names
    this table actually backs (currently ``"wave_failures"`` and
    ``"session_debrief"`` — see ``tools/client_state.py``'s allowlist, which is
    the actual enforcement point; this column is a plain ``String`` so the
    schema itself doesn't need a migration to add a future third collection).
    ``entry_id`` is caller-supplied (for ``wave_failures`` this is the
    state_store-generated id already present on the JSON entry;
    ``session_debrief`` has no natural id so the caller derives one — see the
    module docstring on why no uniqueness is asserted here). ``payload`` holds
    the entry's data as it was passed by the caller.
    """

    __tablename__ = "client_state_entries"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    collection: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Which client/machine wrote this entry (attribution — see
    # tools/client_state.py for validation; never a blindly-trusted free-form
    # string).
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        # Real read pattern: "this client's entries in this collection".
        Index("ix_client_state_entries_collection_client", "collection", "client_id"),
        # Point lookup: "the entry with this id in this collection".
        Index("ix_client_state_entries_collection_entry", "collection", "entry_id"),
    )


class ClientObservation(Base):
    """A single tool-usage observation event reported by a client machine.

    Deliberately per-event (NOT deduped/aggregated like ``core.Observation``,
    which is a counter keyed by ``UniqueConstraint(agent, tool, domain)``) —
    every ``record_observation()`` call is its own row, mirroring
    ``observe_runner.py``'s ``store.observations.add({...})`` shape
    (sessionId/timestamp/tool/topic/... folded into ``event_data``).
    """

    __tablename__ = "client_observations"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    event_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_client_observations_client_id", "client_id"),
        Index("ix_client_observations_created_at", "created_at"),
    )


__all__ = ["ClientObservation", "ClientStateEntry"]
