"""infra_brain.api.routers.internal_state — plain-REST channel for the
infra-ops plugin's hooks (convergence plan P5.1/P5.2/P5.3).

Hooks are stdlib-only ``urllib`` clients with a hard ~1s, non-retrying,
fail-open timeout (D9). FastMCP's streamable-http transport is stateful (an
``initialize`` handshake + ``Mcp-Session-Id`` header + SSE-framed responses)
and cannot be satisfied inside that budget from a fresh, one-shot process —
see the P5.1 research note in CONVERGENCE-PLAN.md. So the three write paths
(observations, client-state, governance-events — the real state_store.py
writers the P5.4 audit found) bypass MCP entirely and call the same
tools/*.py functions directly over one plain JSON request/response cycle.
GET routes back them: session_debrief.py's per-session tally (which used to
read observe_runner.py's local JSON files directly), the SIEM forwarder's
cursor walk (P5.2), and environment_freshness_check.py's live cross-check
against environment_notes (P5.3) all poll these instead.

Auth: a single shared-secret bearer token (``internal_service_token``), not
dashboard-session cookies and not an MCP API key — a narrower, purpose-built
channel for exactly these writes/reads. Same fail-closed-unless-dev-mode
shape as ``webhooks.py``'s ``_verify_token``.

Import rules (matching state_backend.py's):
  * MAY import from infra_brain.api.schemas, infra_brain.config, infra_brain.tools.*.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from infra_brain.api.schemas import (
    ClientObservationEntryOut,
    ClientObservationIn,
    ClientObservationOut,
    ClientStateEntryOut,
    ClientStateIn,
    ClientStateOut,
    EnvironmentNoteOut,
    GovernanceEventIn,
    GovernanceEventOut,
)
from infra_brain.config import get_settings
from infra_brain.tools.client_state import (
    get_client_state,
    get_observations,
    record_client_state,
    record_observation,
)
from infra_brain.tools.environment_notes import get_environment_notes
from infra_brain.tools.governance_events import get_governance_events, record_governance_event

logger = logging.getLogger(__name__)


def verify_internal_service_token(authorization: str | None = Header(default=None)) -> None:
    """Fail-closed bearer check, same shape as webhooks.py's ``_verify_token``.

    An unconfigured token only passes when ``infra_brain_dev`` is set — this
    is a real write path into the audit ledger and observation tables, not a
    read-only convenience route.
    """
    settings = get_settings()
    expected = settings.internal_service_token
    if not expected:
        if settings.infra_brain_dev:
            logger.warning(
                "[internal-state] no internal_service_token configured "
                "(INFRA_BRAIN_DEV=1) — allowing in dev mode"
            )
            return
        logger.warning("[internal-state] hit with no token configured — rejecting (fail closed)")
        raise HTTPException(status_code=503, detail="internal_service_token not configured")

    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:]
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid internal service token")


internal_state_router = APIRouter(
    prefix="/api/internal/state",
    tags=["internal"],
    dependencies=[Depends(verify_internal_service_token)],
)


@internal_state_router.post("/observations", response_model=ClientObservationOut)
def create_observation(body: ClientObservationIn):
    try:
        return record_observation(
            client_id=body.client_id,
            agent=body.agent,
            tool=body.tool,
            domain=body.domain,
            event_data=body.event_data,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@internal_state_router.post("/client-state", response_model=ClientStateOut)
def create_client_state(body: ClientStateIn):
    try:
        return record_client_state(
            collection=body.collection,
            entry_id=body.entry_id,
            payload=body.payload,
            client_id=body.client_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@internal_state_router.get("/client-state", response_model=list[ClientStateEntryOut])
def list_client_state(collection: str, limit: int = 100):
    """P5.1: session_debrief.py polls this (collection='wave_failures') to
    tally a session's blocked-dispatch count now that there is no local
    wave-failures.json left to read."""
    try:
        return get_client_state(collection=collection, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@internal_state_router.get("/observations", response_model=list[ClientObservationEntryOut])
def list_observations(agent: str | None = None, client_id: str | None = None, limit: int = 100):
    """P5.1: session_debrief.py polls this (agent=session_id) to tally a
    session's topic/tool counts now that there is no local observations.json
    left to read."""
    return get_observations(agent=agent, client_id=client_id, limit=limit)


@internal_state_router.post("/governance-events", response_model=GovernanceEventOut)
def create_governance_event(body: GovernanceEventIn):
    try:
        return record_governance_event(
            client_id=body.client_id,
            event_type=body.event_type,
            payload=body.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@internal_state_router.get("/governance-events", response_model=list[GovernanceEventOut])
def list_governance_events(
    client_id: str | None = None, limit: int = 100, since_id: str | None = None
):
    """P5.2: the SIEM forwarder polls this (cursor = ``since_id``) instead of
    tailing a local JSONL file now that governance_ledger.py writes here."""
    return get_governance_events(client_id=client_id, limit=limit, since_id=since_id)


@internal_state_router.get("/environment-notes", response_model=list[EnvironmentNoteOut])
def list_environment_notes_internal(status: str | None = None, limit: int = 50):
    """P5.3: environment_freshness_check.py's live cross-check -- the
    git-committed knowledge/environment.md stays the team-shared source of
    truth (per infra-ops/CLAUDE.md's documented design; this does NOT
    replace it), but the hook also asks whether newer environment_notes
    exist than the doc's own last_updated field, as a nudge to fold them in."""
    return get_environment_notes(status=status, limit=limit)
