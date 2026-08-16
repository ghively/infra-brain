"""Client-local state ingestion (P4.2e/P4.2f, convergence plan).

Plain functions (not LangChain ``@tool``s — MCP wiring happens in a later
integration pass; see ``db/models/client_state.py`` for the schema and the
reasoning behind scoping this to exactly two real use cases rather than a
generic KV store): the infra-ops plugin's client-local ``state_store.py``
persists per-machine facts as JSON files with no shared backend. The
convergence-plan audit found exactly two real callers worth centralizing:

* ``scripts/hooks/observe_runner.py``'s ``store.wave_failures.add({...})`` —
  a blocked-Agent-dispatch record (``sessionId``, ``agentType``, ``needs``,
  ``outputSnippet``).
* ``scripts/hooks/session_debrief.py``'s raw JSONL append to
  ``sessions.jsonl`` — a per-session summary (``sessionId``, ``timestamp``,
  ``observation_count``, ``topics``, ``tools_used``, ``top_topic``,
  ``failures_this_session``, ``needs_gap_review``).

Neither of these functions performs an external network call, so neither goes
through ``callbacks/write_gate.py``'s ``gate_external_write`` (that gate is
for outbound GitLab/Jira/Confluence calls specifically — see its docstring);
these two write only to infra-brain's own Postgres DB. Attribution
(``client_id``) is still validated rather than trusted as an arbitrary
free-form string — see ``_require_str`` below.
"""

from __future__ import annotations

import logging
from typing import Any

from infra_brain.db.models import ClientObservation, ClientStateEntry
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

# The convergence-plan audit's real-caller scope (see module docstring). Not a
# generic KV store — a write naming any other collection is rejected rather
# than silently accepted, so a future speculative third caller is a deliberate
# schema decision (widen this set), not a silent scope-creep.
ALLOWED_COLLECTIONS = frozenset({"wave_failures", "session_debrief"})

_MAX_SHORT_FIELD = 128


def _require_str(value: Any, field_name: str, *, max_len: int = _MAX_SHORT_FIELD) -> str:
    """Validate *value* is a non-empty, length-bounded string; return it stripped.

    Centralizes the "accept/derive attribution, don't blindly trust a
    free-form caller string" rule for every identity-bearing field these
    functions take (``client_id``, ``agent``, ``tool``, ``domain``,
    ``entry_id``, ``collection``) — raises ``ValueError`` fail-closed rather
    than silently truncating or coercing a bad value, matching this repo's
    write-path convention of never swallowing a caller error into a
    successful-looking write.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string, got {type(value).__name__}")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    if len(cleaned) > max_len:
        raise ValueError(f"{field_name} must be at most {max_len} characters (got {len(cleaned)})")
    return cleaned


def _require_payload(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a dict, got {type(value).__name__}")
    return value


def record_client_state(
    collection: str,
    entry_id: str,
    payload: dict[str, Any],
    client_id: str,
) -> dict[str, Any]:
    """Persist one client-local state entry.

    Args:
        collection: which state_store collection this entry belongs to — must
            be one of ``ALLOWED_COLLECTIONS`` (``"wave_failures"`` or
            ``"session_debrief"``); any other value raises ``ValueError``.
        entry_id: caller-supplied id for this entry (for ``wave_failures``,
            the state_store-generated id already on the JSON entry;
            ``session_debrief`` has none natively, so the caller derives one —
            e.g. ``sessionId`` + timestamp — see
            ``db/models/client_state.py``'s module docstring for why no
            uniqueness is asserted across repeated writes).
        payload: the entry's data, stored as-is (JSONB). May be ``{}``.
        client_id: attribution — which client/machine wrote this entry.

    Returns:
        ``{"id": str, "collection": str, "entry_id": str, "client_id": str,
        "created_at": str}``.

    Raises:
        ValueError: invalid collection/entry_id/client_id, or a non-dict payload.
    """
    collection = _require_str(collection, "collection", max_len=64)
    if collection not in ALLOWED_COLLECTIONS:
        raise ValueError(
            f"collection {collection!r} is not one of the scoped collections "
            f"{sorted(ALLOWED_COLLECTIONS)}"
        )
    entry_id = _require_str(entry_id, "entry_id")
    client_id = _require_str(client_id, "client_id")
    payload = _require_payload(payload, "payload")

    row = ClientStateEntry(
        collection=collection,
        entry_id=entry_id,
        payload=payload,
        client_id=client_id,
    )
    with get_session() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        result = {
            "id": str(row.id),
            "collection": row.collection,
            "entry_id": row.entry_id,
            "client_id": row.client_id,
            "created_at": row.created_at.isoformat(),
        }
    logger.info(
        "client_state: recorded %s entry %s for client %s", collection, entry_id, client_id
    )
    return result


def record_observation(
    client_id: str,
    agent: str,
    tool: str,
    domain: str,
    event_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one per-event client tool-usage observation (never deduped/aggregated).

    Args:
        client_id: attribution — which client/machine reported this event.
        agent: reporting agent/session identifier (e.g. subagent type).
        tool: the tool name observed (e.g. ``"bash"``, ``"edit"``, ``"agent"``).
        domain: classified work topic (e.g. ``"ansible-playbook"``, ``"general"``).
        event_data: optional additional detail (JSONB), e.g. a bash fingerprint
            or file path. Defaults to ``None`` (stored as SQL NULL).

    Returns:
        ``{"id": str, "client_id": str, "agent": str, "tool": str,
        "domain": str, "created_at": str}``.

    Raises:
        ValueError: any required string field is missing/empty/oversized, or
            ``event_data`` is provided but not a dict.
    """
    client_id = _require_str(client_id, "client_id")
    agent = _require_str(agent, "agent", max_len=64)
    tool = _require_str(tool, "tool")
    domain = _require_str(domain, "domain", max_len=64)
    if event_data is not None:
        event_data = _require_payload(event_data, "event_data")

    row = ClientObservation(
        client_id=client_id,
        agent=agent,
        tool=tool,
        domain=domain,
        event_data=event_data,
    )
    with get_session() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        result = {
            "id": str(row.id),
            "client_id": row.client_id,
            "agent": row.agent,
            "tool": row.tool,
            "domain": row.domain,
            "created_at": row.created_at.isoformat(),
        }
    logger.info("client_state: recorded observation %s/%s for client %s", agent, tool, client_id)
    return result


def get_client_state(collection: str, limit: int = 100) -> list[dict[str, Any]]:
    """Return client-state entries for one collection, newest first.

    P5.1 (convergence plan): session_debrief.py used to tally a session's
    wave_failures by reading the local state_store.py JSON file directly;
    now that observe_runner.py's real writer is a network call, there is no
    local file left to read, so this backs that same tally server-side.

    Args:
        collection: must be one of ``ALLOWED_COLLECTIONS``; any other value
            raises ``ValueError`` (same scoping rule as ``record_client_state``).
        limit: maximum rows to return (default 100).

    Returns:
        A list of dicts: ``{"id", "collection", "entry_id", "payload",
        "client_id", "created_at"}``, most recent first. Empty list if
        nothing matches (never raises for "no rows").
    """
    collection = _require_str(collection, "collection", max_len=64)
    if collection not in ALLOWED_COLLECTIONS:
        raise ValueError(
            f"collection {collection!r} is not one of the scoped collections "
            f"{sorted(ALLOWED_COLLECTIONS)}"
        )
    with get_session() as session:
        rows = (
            session.query(ClientStateEntry)
            .filter(ClientStateEntry.collection == collection)
            .order_by(ClientStateEntry.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": str(row.id),
                "collection": row.collection,
                "entry_id": row.entry_id,
                "payload": row.payload,
                "client_id": row.client_id,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]


def get_observations(
    agent: str | None = None, client_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Return per-event client observations, newest first, optionally filtered.

    P5.1: backs session_debrief.py's per-session tally the same way
    ``get_client_state`` backs its wave_failures tally (see that function's
    docstring) — ``agent`` is the session id (observe_runner.py's
    ``record_observation(agent=session_id, ...)`` convention).

    Args:
        agent: when given, only rows with this exact ``agent`` are returned.
        client_id: when given, only rows with this exact ``client_id`` are
            returned. Both filters may be combined.
        limit: maximum rows to return (default 100).

    Returns:
        A list of dicts (see ``record_observation``'s return shape), most
        recent first. Empty list if nothing matches (never raises for "no
        rows").
    """
    with get_session() as session:
        query = session.query(ClientObservation)
        if agent:
            query = query.filter(ClientObservation.agent == agent)
        if client_id:
            query = query.filter(ClientObservation.client_id == client_id)
        rows = query.order_by(ClientObservation.created_at.desc()).limit(limit).all()
        return [
            {
                "id": str(row.id),
                "client_id": row.client_id,
                "agent": row.agent,
                "tool": row.tool,
                "domain": row.domain,
                "event_data": row.event_data,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]


__all__ = [
    "ALLOWED_COLLECTIONS",
    "record_client_state",
    "record_observation",
    "get_client_state",
    "get_observations",
]
