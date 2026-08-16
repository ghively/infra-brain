"""Provenance markers for rows that were authored by hand, not by an agent.

Lives in its own dependency-free module (rather than in ``mcp_server``) so
non-MCP consumers — ``agents/drift_learning.py`` promoting a RootCauseNote
into an Instinct, for one — can check the marker structurally without
importing the whole FastMCP server.

``MANUAL_PROVENANCE_SOURCE`` is the value written into the STRUCTURED
provenance slot of a manually authored row:

  * ``RootCauseNote.correlated["source"]``
  * ``ProposedAction.agent`` and ``ProposedAction.payload["source"]``

Downstream code must branch on that structured marker, never on the
human-readable ``[MANUAL/MCP-authored by ...]`` prose banner — the banner is
for humans reading the text and does not survive truncation/reformatting.
"""

from __future__ import annotations

from typing import Any

MANUAL_PROVENANCE_SOURCE = "manual_mcp"


def manual_banner(authored_by: str) -> str:
    """The visible provenance banner prefixed onto manually authored prose."""
    return f"[MANUAL/MCP-authored by {authored_by}] "


def is_manually_authored(correlated: Any) -> bool:
    """True when a JSONB provenance blob carries the manual-write marker."""
    return isinstance(correlated, dict) and correlated.get("source") == MANUAL_PROVENANCE_SOURCE
