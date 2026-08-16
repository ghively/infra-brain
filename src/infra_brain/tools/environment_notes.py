"""Environment notes -- human-written narrative layer (P4.2a / P4.3a).

A small (~25-entries-in-practice) set of free-text operator notes about the
environment that supplement the structured resource/drift/eol queries --
things a human knows and wants recorded ("the staging DB host is scheduled
for decommission next quarter", "ignore drift on netdev-07, it's mid-RMA")
that don't map onto any existing structured column.

Plain functions (not the MCP ``@mcp.tool`` decorator -- that wiring is a
later integration pass; see mcp_server.py's ``record_rootcause_note`` for
the eventual shape). None of these functions make an external network call,
so none of them go through ``callbacks/write_gate.py``'s
``gate_external_write`` -- that gate is reserved for outbound HTTP writes
(GitLab/Jira/Confluence/webhooks); this module only ever writes to
infra-brain's own ``environment_notes`` table.

Attribution (``author`` / ``resolved_by``) is a SERVER-DERIVED string, never
raw free-form caller text -- same contract as ``authored_by`` in
``mcp_server.record_rootcause_note``. Deriving it from the authenticated
caller identity (``_caller_identity()`` in mcp_server.py) is out of scope
here (mcp_server.py is one of the shared files this task must not touch);
the later integration pass is responsible for calling
``record_environment_note`` / ``resolve_environment_note`` with an already-
derived attribution string. What this module DOES do internally is refuse
to trust that string blindly: it must be non-empty and is bounded to the
``EnvironmentNote.author`` / ``.resolved_by`` column width (128 chars)
before ever reaching the DB.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from infra_brain.db.models import EnvironmentNote
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

# Mirrors the ``_FREE_TEXT_MAX_LEN`` bound mcp_server.py applies to other
# manual-write tools (e.g. ``record_rootcause_note``'s ``explanation``).
_NOTE_MAX_LEN = 8000

# Matches EnvironmentNote.author / .resolved_by column width (String(128)).
_ATTRIBUTION_MAX_LEN = 128

_STATUS_OPEN = "open"
_STATUS_RESOLVED = "resolved"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _clean_attribution(value: str, field_name: str) -> str:
    """Bound + validate a server-derived attribution string.

    Raises ``ValueError`` if empty/whitespace-only; truncates to the DB
    column width otherwise so an oversized value can never reach the DB
    (and never silently corrupts a different column via a DB-level error).
    """
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} must be a non-empty attribution string")
    return cleaned[:_ATTRIBUTION_MAX_LEN]


def _note_to_dict(note: EnvironmentNote) -> dict:
    return {
        "id": str(note.id),
        "note": note.note,
        "author": note.author,
        "created_at": note.created_at.isoformat() if note.created_at else None,
        "status": note.status,
        "resolved_by": note.resolved_by,
        "resolved_at": note.resolved_at.isoformat() if note.resolved_at else None,
    }


def record_environment_note(note: str, author: str) -> dict:
    """Record one human-written environment note. Returns the new row as a dict.

    Args:
        note: the note text. Must be non-empty (after stripping) and at
            most 8000 characters.
        author: server-derived attribution string for who wrote this note.
            NOT free-form caller text -- see module docstring. Must be
            non-empty; truncated to 128 characters if longer.

    Raises:
        ValueError: ``note`` is empty/whitespace-only, ``note`` exceeds
            8000 characters, or ``author`` is empty/whitespace-only.
    """
    cleaned_note = (note or "").strip()
    if not cleaned_note:
        raise ValueError("note must not be empty")
    if len(cleaned_note) > _NOTE_MAX_LEN:
        raise ValueError(f"note exceeds the {_NOTE_MAX_LEN}-character limit")
    cleaned_author = _clean_attribution(author, "author")

    with get_session() as session:
        row = EnvironmentNote(note=cleaned_note, author=cleaned_author, status=_STATUS_OPEN)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _note_to_dict(row)


def get_environment_notes(status: str | None = "open", limit: int = 50) -> list[dict]:
    """Return environment notes, most recently created first.

    Args:
        status: filter to this exact status ("open"/"resolved"). Pass
            ``None`` to return notes of any status. Default "open".
        limit: maximum number of rows to return. Default 50. Values <= 0
            return an empty list.

    Returns:
        A list of dicts (possibly empty), newest ``created_at`` first.
    """
    capped_limit = max(0, limit)
    if capped_limit == 0:
        return []

    with get_session() as session:
        query = session.query(EnvironmentNote)
        if status is not None:
            query = query.filter(EnvironmentNote.status == status)
        rows = query.order_by(EnvironmentNote.created_at.desc()).limit(capped_limit).all()
        return [_note_to_dict(row) for row in rows]


def resolve_environment_note(note_id: str, resolved_by: str) -> dict:
    """Mark one environment note resolved. Returns the updated row as a dict.

    Sets ``status="resolved"``, ``resolved_by``, and ``resolved_at`` (now,
    UTC).

    Args:
        note_id: the note's UUID (string form).
        resolved_by: server-derived attribution string for who resolved
            this note. NOT free-form caller text -- see module docstring.
            Must be non-empty; truncated to 128 characters if longer.

    Raises:
        ValueError: ``note_id`` is not a valid UUID, no note exists with
            that id, or ``resolved_by`` is empty/whitespace-only.
    """
    try:
        parsed_id = uuid.UUID(str(note_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"note_id {note_id!r} is not a valid UUID") from exc

    cleaned_resolver = _clean_attribution(resolved_by, "resolved_by")

    with get_session() as session:
        row = session.get(EnvironmentNote, parsed_id)
        if row is None:
            raise ValueError(f"no environment note found with id {note_id}")
        row.status = _STATUS_RESOLVED
        row.resolved_by = cleaned_resolver
        row.resolved_at = _now_utc()
        session.commit()
        session.refresh(row)
        return _note_to_dict(row)


__all__ = [
    "record_environment_note",
    "get_environment_notes",
    "resolve_environment_note",
]
