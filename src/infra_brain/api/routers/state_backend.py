"""infra_brain.api.routers.state_backend — REST counterparts for the Phase 4
state-backend tools (convergence plan P4.3e), so the dashboard isn't blind to
data only reachable via MCP today.

Scope: read/write endpoints only, thin wrappers over the same tools/*.py
functions the MCP layer calls (see mcp_server.py's "Phase 4 state backend"
section for the MCP side) — no new business logic here. Building the actual
dashboard UI panels that call these (the "UI for the new human-approval
loops" the plan also asks for) is a separate, larger frontend task and is
NOT done in this pass; these endpoints exist and are tested so that work can
follow without a backend blocker.

Import rules (matching documents.py's router):
  * MAY import from infra_brain.api.schemas, infra_brain.dashboard_auth,
    infra_brain.tools.*.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from infra_brain.api.schemas import (
    DocumentIngestIn,
    DocumentIngestOut,
    EnvironmentNoteIn,
    EnvironmentNoteOut,
    InstinctHistoryOut,
    InstinctProposalIn,
    InstinctProposalOut,
)
from infra_brain.dashboard_auth import current_user, require_session
from infra_brain.tools.documents import ingest_document
from infra_brain.tools.environment_notes import (
    get_environment_notes,
    record_environment_note,
    resolve_environment_note,
)
from infra_brain.tools.instinct_governance import get_instinct_history, propose_instinct

state_backend_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)


@state_backend_router.get("/environment-notes", response_model=list[EnvironmentNoteOut])
def list_environment_notes(status: str | None = "open", limit: int = 50):
    return get_environment_notes(status=status, limit=limit)


@state_backend_router.post("/environment-notes", response_model=EnvironmentNoteOut)
def create_environment_note(body: EnvironmentNoteIn, request: Request):
    # Server-derived attribution, same rule as the MCP tool wrapper — never a
    # free-form body field. require_session's dependency already resolved a
    # real dashboard user for this request.
    user = current_user(request)
    author = f"dashboard:{user.get('username', 'unknown')}" if user else "dashboard:unknown"
    try:
        return record_environment_note(note=body.note, author=author)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@state_backend_router.post(
    "/environment-notes/{note_id}/resolve", response_model=EnvironmentNoteOut
)
def resolve_environment_note_route(note_id: str, request: Request):
    user = current_user(request)
    resolved_by = f"dashboard:{user.get('username', 'unknown')}" if user else "dashboard:unknown"
    try:
        return resolve_environment_note(note_id=note_id, resolved_by=resolved_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@state_backend_router.get(
    "/instincts/{instinct_id}/history", response_model=InstinctHistoryOut
)
def get_instinct_history_route(instinct_id: str):
    try:
        return get_instinct_history(instinct_id=instinct_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@state_backend_router.post("/instincts/propose", response_model=InstinctProposalOut)
def propose_instinct_route(body: InstinctProposalIn, request: Request):
    user = current_user(request)
    proposed_by = f"dashboard:{user.get('username', 'unknown')}" if user else "dashboard:unknown"
    return propose_instinct(
        zone=body.zone,
        domain=body.domain,
        pattern=body.pattern,
        confidence=body.confidence,
        proposed_by=proposed_by,
        evidence=body.evidence or "",
        citation=body.citation or "",
    )


@state_backend_router.post("/documents", response_model=DocumentIngestOut)
def ingest_document_route(body: DocumentIngestIn, request: Request):
    user = current_user(request)
    ingested_by = f"dashboard:{user.get('username', 'unknown')}" if user else "dashboard:unknown"
    try:
        return ingest_document(
            title=body.title,
            content_hash=body.content_hash,
            source=body.source,
            client_origin="dashboard",
            ingested_by=ingested_by,
            url=body.url,
            external_id=body.external_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
