"""Hash-chained, append-only governance ledger — plain functions (P4.2g /
part of P4.3d). MCP tool wiring happens in a later integration pass; these
are deliberately plain functions, not ``@tool``-decorated.

This is a NEW property versus the existing governance surfaces in this repo:

* ``AgentActionLog`` (``db/models/governance.py``) — durable per-tool-call
  rows written by ``AuditCallbackHandler``. Independent rows, no chaining.
* ``agent/scripts/hooks/governance_ledger.py`` — the plugin-side JSONL hook
  ledger. Fingerprints the command with a *truncated* sha256 for privacy;
  records are independent lines, NOT hash-chained.

``GovernanceEvent`` (``db/models/governance_events_ext.py``) adds real
hash-chaining: each row's ``hash`` covers its own payload PLUS the previous
row's ``hash``, so tampering with any row breaks the chain from that point
forward. ``verify_governance_chain`` below is the tamper-detection capability
that fact buys — the whole point of this table over the existing ledger.

No external network call is made anywhere in this module, so
``callbacks.write_gate.gate_external_write`` does not apply here (it exists
for outbound GitLab/Jira/Confluence writes specifically). Mutation-safety
here instead means: ``record_governance_event`` requires real attribution
(a non-empty ``client_id`` — callers must resolve a real caller identity
before calling this, not pass through arbitrary free text) and NEVER updates
or deletes an existing row; it only ever inserts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from infra_brain.db.models import GovernanceEvent, _now
from infra_brain.db.session import get_session


def _iso(created_at: datetime) -> str:
    """Normalize ``created_at`` to a UTC-aware ISO string before hashing.

    SQLite's ``DateTime(timezone=True)`` column does not actually preserve
    tzinfo across a write/read round-trip (it comes back naive) even though
    PostgreSQL does -- see ``db/models/_base.py``'s cross-dialect DateTime
    usage. ``_now()`` always produces a UTC instant regardless of backend, so
    a naive value read back is always implicitly UTC; without this
    normalization, ``.isoformat()`` differs between the in-memory value used
    at write time (tz-aware, "+00:00" suffix) and the value re-read from
    SQLite during ``verify_governance_chain`` (naive, no suffix), which would
    make every freshly-written row fail its own hash check on SQLite. Both
    call sites of ``_row_hash`` route through this helper so the material is
    identical regardless of dialect.
    """
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.isoformat()


def _row_hash(prev_hash: Optional[str], event_type: str, payload: dict, created_at: datetime) -> str:
    """sha256 hex of prev_hash-or-empty-string + event_type +
    json.dumps(payload, sort_keys=True) + created_at.isoformat().

    This exact formula is used both when writing a new row
    (``record_governance_event``) and when re-deriving each row's expected
    hash during verification (``verify_governance_chain``) — the two MUST
    stay in lockstep or every existing chain looks tampered.
    """
    payload_json = json.dumps(payload, sort_keys=True)
    material = (prev_hash or "") + event_type + payload_json + _iso(created_at)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _serialize(row: GovernanceEvent) -> dict:
    return {
        "id": str(row.id),
        "client_id": row.client_id,
        "event_type": row.event_type,
        "payload": row.payload,
        "prev_hash": row.prev_hash,
        "hash": row.hash,
        "created_at": row.created_at.isoformat(),
    }


def record_governance_event(client_id: str, event_type: str, payload: Optional[dict] = None) -> dict:
    """Append one hash-chained row to the governance_events ledger.

    Attribution: ``client_id`` is REQUIRED and must be a non-empty string —
    this function does not accept a blank/missing caller identity and trust
    it implicitly; the caller (the later MCP tool-wiring pass) is responsible
    for deriving ``client_id`` from a real authenticated identity rather than
    letting free-form user input flow straight through.

    Chaining: reads the most recent row (by ``created_at``, descending) to
    get ``prev_hash`` (``None`` if the table is empty — this is the first row
    ever), computes this row's ``hash`` per ``_row_hash``, and inserts. This
    function ONLY EVER INSERTS — it never updates or deletes an existing row.

    Args:
        client_id: attribution for this event. Non-empty, max 128 chars
            (DB column limit — longer values raise ``ValueError`` here rather
            than failing at flush time).
        event_type: short machine-readable event category, e.g.
            "instinct_promoted", "rollback_approved". Non-empty, max 64
            chars.
        payload: JSON-serializable event detail. Defaults to ``{}``.

    Returns:
        A dict with ``id``, ``client_id``, ``event_type``, ``payload``,
        ``prev_hash``, ``hash``, ``created_at`` (all JSON-safe scalars).

    Raises:
        ValueError: ``client_id``/``event_type`` missing or too long, or
            ``payload`` is not a JSON-serializable dict.
    """
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id is required and must be a non-empty string")
    client_id = client_id.strip()
    if len(client_id) > 128:
        raise ValueError("client_id must be at most 128 characters")

    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("event_type is required and must be a non-empty string")
    event_type = event_type.strip()
    if len(event_type) > 64:
        raise ValueError("event_type must be at most 64 characters")

    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    try:
        json.dumps(payload, sort_keys=True)
    except TypeError as exc:
        raise ValueError(f"payload must be JSON-serializable: {exc}") from exc

    with get_session() as session:
        # P5.2 (convergence plan): governance_ledger.py's per-tool-call audit
        # hook redirects here, raising write volume well past the single
        # learning_promotion_gate.py caller this originally had. Two
        # concurrent inserts that both read "latest" before either commits
        # would both compute the same prev_hash and silently fork the
        # chain -- with_for_update() serializes readers of the current tail
        # row against Postgres (a harmless no-op on SQLite's single-writer
        # engine, used by the test suite). Doesn't cover the one-time empty-
        # table race (no row yet to lock); that residual window is accepted
        # as a home-lab-scale edge case.
        latest = (
            session.query(GovernanceEvent)
            .order_by(GovernanceEvent.created_at.desc())
            .with_for_update()
            .limit(1)
            .first()
        )
        prev_hash = latest.hash if latest is not None else None

        created_at = _now()
        row = GovernanceEvent(
            client_id=client_id,
            event_type=event_type,
            payload=payload,
            prev_hash=prev_hash,
            hash=_row_hash(prev_hash, event_type, payload, created_at),
            created_at=created_at,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row)


def get_governance_events(
    client_id: Optional[str] = None,
    limit: int = 100,
    since_id: Optional[str] = None,
) -> list[dict]:
    """Return governance_events rows, optionally filtered/paginated.

    Args:
        client_id: when given, only rows with this exact ``client_id`` are
            returned. ``None`` (default) returns rows across all clients.
        limit: maximum rows to return (default 100).
        since_id: cursor for incremental forwarding (P5.2's SIEM forwarder,
            which polls this endpoint instead of tailing a local file now
            that governance_ledger.py writes here). When given, returns only
            rows strictly newer than the row with this id, oldest-first (so
            a forwarder can walk forward and re-arm its cursor to the last
            row's id). Unknown/already-consumed id -> empty list, not an
            error (matches state_store.py's old "nothing new" behavior).
            When omitted, ordering is newest-first (the original, unpaginated
            "recent activity" shape everything else here still uses).

    Returns:
        A list of dicts (see ``_serialize``). Empty list if nothing matches
        (never raises for "no rows").
    """
    with get_session() as session:
        if since_id is not None:
            try:
                parsed_since_id = uuid.UUID(str(since_id))
            except (ValueError, TypeError, AttributeError):
                return []
            cursor_row = session.get(GovernanceEvent, parsed_since_id)
            if cursor_row is None:
                return []
            query = session.query(GovernanceEvent).filter(
                GovernanceEvent.created_at > cursor_row.created_at
            )
            if client_id:
                query = query.filter(GovernanceEvent.client_id == client_id)
            rows = query.order_by(GovernanceEvent.created_at.asc()).limit(limit).all()
            return [_serialize(row) for row in rows]

        query = session.query(GovernanceEvent)
        if client_id:
            query = query.filter(GovernanceEvent.client_id == client_id)
        rows = query.order_by(GovernanceEvent.created_at.desc()).limit(limit).all()
        return [_serialize(row) for row in rows]


def verify_governance_chain() -> dict:
    """Walk the entire governance_events table and verify the hash chain.

    For every row, in ``created_at`` ascending order, recomputes the expected
    hash from that row's own payload PLUS the hash ACTUALLY produced by the
    previous row in this walk (not merely the previous row's stored
    ``prev_hash`` claim — a tampered ``prev_hash`` field is caught the same
    way a tampered ``hash``/``payload`` field is: the recomputed hash will not
    match what is stored). The first mismatch stops the walk.

    Returns:
        ``{"valid": bool, "broken_at": str | None, "checked": int}`` —
        ``broken_at`` is the ``id`` (str) of the first row whose stored hash
        does not match its recomputed hash, or ``None`` if the whole chain is
        intact. ``checked`` is the number of rows examined.
    """
    with get_session() as session:
        rows = session.query(GovernanceEvent).order_by(GovernanceEvent.created_at.asc()).all()

    prev_hash: Optional[str] = None
    for row in rows:
        expected_hash = _row_hash(prev_hash, row.event_type, row.payload, row.created_at)
        if row.prev_hash != prev_hash or row.hash != expected_hash:
            return {"valid": False, "broken_at": str(row.id), "checked": len(rows)}
        prev_hash = row.hash

    return {"valid": True, "broken_at": None, "checked": len(rows)}
