"""Bounds for the interactive chat lane — one definition, three consumers.

``api/schemas.py`` (request validation), ``api/routers/ui.py`` (the SSE route)
and ``chat/agent.py`` (the graph driver) all need the same numbers, and this
module has no heavy imports so any of them can pull it without dragging
LangGraph or Pydantic into a place that does not already have it.

These are DENIAL-OF-WALLET / runaway-context bounds, deliberately separate from
the per-user rate limit and daily token budget in ``ratelimit.py``
(TRK-090/TRK-091). Those cap how OFTEN and how MUCH a user may spend across
requests; these cap how big a SINGLE request and a SINGLE conversation thread
can get, which is what actually determines the per-call context size the
provider is billed for.
"""

from __future__ import annotations

#: Longest single user message accepted by ``POST /api/dashboard/chat``.
#: Rejected at the Pydantic layer (422) rather than truncated — silently
#: dropping half of someone's paste is worse than telling them it was too long.
CHAT_MAX_MESSAGE_CHARS = 4000

#: Most user turns allowed on one checkpointer thread before the caller must
#: start a new conversation. The LangGraph checkpointer replays the WHOLE thread
#: into every subsequent call, so an unbounded thread grows the prompt (and the
#: bill) without limit. The UI surfaces this as a "start a new conversation"
#: error rather than silently truncating history, so an operator is never shown
#: an answer that quietly forgot the earlier context it was reasoning from.
CHAT_MAX_TURNS = 40

#: Cap on how many graph nodes/edges are echoed back to the browser as
#: provenance for one tool call (the tool itself already caps and REPORTS its
#: own truncation; this is a second cap on the wire payload).
CHAT_MAX_PROVENANCE_ITEMS = 25
