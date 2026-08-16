"""infra_brain.api.routers.governance_intelligence -- Instincts, observations, scripts, proposals.

Split from governance.py (refactor/split-governance-router).
Handler bodies are byte-identical to the originals; no logic changes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from infra_brain.api._envelope import paginate
from infra_brain.api._helpers import _s
from infra_brain.api.schemas import (
    GeneratedScriptPageOut,
    InstinctOut,
    InstinctPageOut,
    IntegrationProposalApproveBody,
    IntegrationProposalApproveResult,
    IntegrationProposalPageOut,
    IntegrationProposalRejectResult,
    ObservationOut,
    ObservationPageOut,
    ProposalOut,
    ScriptOut,
)
from infra_brain.dashboard_auth import current_user, require_admin, require_session
from infra_brain.db.models import (
    GeneratedScript,
    Instinct,
    IntegrationProposal,
    Observation,
)
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

governance_intelligence_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)


@governance_intelligence_router.get("/instincts", response_model=InstinctPageOut)
def list_instincts(
    domain: Optional[str] = None,
    zone: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    from infra_brain.config import get_settings

    # M-3: clamp before use so the returned envelope reflects what was
    # actually queried (paginate() also clamps as a backstop).
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(Instinct)
        if domain:
            q = q.filter(Instinct.domain == domain)
        if zone:
            q = q.filter(Instinct.zone == zone)
        q = q.order_by(Instinct.confidence.desc())
        items_raw, total = paginate(q, limit=limit, offset=offset)
        # TRK-182 follow-up: was hardcoded 0.7 — now reads the same
        # Settings field (shared with Intprops.tsx) as fleet.py's
        # get_counts() applied_instincts badge, so the two surfaces
        # can't drift apart.
        confidence_gate = get_settings().integration_confidence_gate
        items = [
            InstinctOut(
                domain=i.domain,
                zone=i.zone,
                pattern=i.pattern,
                confidence=i.confidence,
                promoted_at=_s(i.promoted_at),
                promoted_by=i.promoted_by or "",
                citation=i.citation or "",
                applied=i.confidence >= confidence_gate,
            )
            for i in items_raw
        ]
        return InstinctPageOut(items=items, total=total, limit=limit, offset=offset)


@governance_intelligence_router.get("/observations", response_model=ObservationPageOut)
def list_observations(limit: int = 200, offset: int = 0):
    # M-3: clamp before use, see list_instincts above for rationale.
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(Observation).order_by(Observation.count.desc())
        items_raw, total = paginate(q, limit=limit, offset=offset)
        items = [
            ObservationOut(
                agent=o.agent,
                tool=o.tool,
                domain=o.domain,
                pattern=o.pattern,
                count=o.count,
                last_seen=o.last_seen,
            )
            for o in items_raw
        ]
        return ObservationPageOut(items=items, total=total, limit=limit, offset=offset)


@governance_intelligence_router.get("/generated_scripts", response_model=GeneratedScriptPageOut)
def list_scripts(
    language: Optional[str] = None,
    agent: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    # M-3: clamp before use, see list_instincts above for rationale.
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(GeneratedScript)
        if language:
            q = q.filter(GeneratedScript.language == language)
        if agent:
            q = q.filter(GeneratedScript.created_by_agent == agent)
        q = q.order_by(GeneratedScript.created_at.desc())
        items_raw, total = paginate(q, limit=limit, offset=offset)
        items = [
            ScriptOut(
                name=g.name,
                language=g.language,
                purpose=g.purpose,
                run_count=g.run_count,
                last_returncode=g.last_returncode if g.last_returncode is not None else -1,
                created_by_agent=g.created_by_agent,
                domain=g.domain or "",
                git_path=g.git_path or "",
                last_run_at=g.last_run_at,
                created_at=g.created_at,
            )
            for g in items_raw
        ]
        return GeneratedScriptPageOut(items=items, total=total, limit=limit, offset=offset)


@governance_intelligence_router.get(
    "/integration_proposals", response_model=IntegrationProposalPageOut
)
def list_proposals(status: Optional[str] = None, limit: int = 200, offset: int = 0):
    # M-3: clamp before use, see list_instincts above for rationale.
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(IntegrationProposal)
        if status:
            q = q.filter(IntegrationProposal.status == status)
        q = q.order_by(IntegrationProposal.proposed_at.desc())
        items_raw, total = paginate(q, limit=limit, offset=offset)
        items = [
            ProposalOut(
                id=str(p.id),
                type=p.type,
                endpoint=p.endpoint,
                confidence=p.confidence,
                proposed_at=p.proposed_at,
                status=p.status,
                source=p.source,
            )
            for p in items_raw
        ]
        return IntegrationProposalPageOut(items=items, total=total, limit=limit, offset=offset)


def _proposal_uuid(proposal_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(proposal_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="proposal_id must be a valid UUID")


@governance_intelligence_router.post(
    "/integration_proposals/{proposal_id}/approve",
    status_code=200,
    response_model=IntegrationProposalApproveResult,
)
async def approve_integration_proposal(
    proposal_id: str,
    request: Request,
    body: IntegrationProposalApproveBody | None = None,
    _: None = Depends(require_admin),
):
    """Approve a pending IntegrationProposal and immediately wire() it.

    Unlike ProposedAction's config_fix/vuln_patch flow (execution deferred to
    an async poll — see remediation.py's _execute_approved), CoverageAgent.
    wire() is fast (one LLM call + a handful of DB rows) and safe to run
    inline, so approval and wiring happen in the same request — there is no
    separate poller for IntegrationProposal. (#93: previously there was no
    approve endpoint at all, so wire() — fully built — had zero real callers.)
    """
    user = current_user(request) or {}
    approved_by = (body.approved_by if body else None) or user.get("username") or "dashboard"

    def _approve() -> None:
        with get_session() as session:
            proposal = session.get(IntegrationProposal, _proposal_uuid(proposal_id))
            if not proposal:
                raise HTTPException(status_code=404, detail="Proposal not found")
            if proposal.status != "pending":
                raise HTTPException(
                    status_code=409, detail=f"Proposal is {proposal.status}, not pending"
                )
            proposal.status = "approved"
            proposal.approved_by = approved_by
            proposal.approved_at = datetime.now(timezone.utc)
            session.commit()

    await asyncio.to_thread(_approve)

    from infra_brain.agents.coverage import CoverageAgent  # noqa: PLC0415

    def _revert_to_pending() -> None:
        # A wire() failure (raised exception or a clean False return) must not
        # strand the proposal at status="approved" — that's a dead end: no
        # other code path reads/resets it, and re-approving is blocked by the
        # `!= "pending"` guard above. Revert so the same endpoint can retry.
        with get_session() as session:
            proposal = session.get(IntegrationProposal, _proposal_uuid(proposal_id))
            if proposal and proposal.status == "approved":
                proposal.status = "pending"
                proposal.approved_by = None
                proposal.approved_at = None
                session.commit()

    try:
        wired = await asyncio.to_thread(CoverageAgent().wire, proposal_id)
    except Exception:
        logger.exception("CoverageAgent.wire() raised for proposal %s", proposal_id)
        await asyncio.to_thread(_revert_to_pending)
        raise HTTPException(
            status_code=500,
            detail="Proposal approved but wiring raised an exception — reverted to "
            "pending, retry via this endpoint",
        )
    if not wired:
        await asyncio.to_thread(_revert_to_pending)
        raise HTTPException(
            status_code=500,
            detail="Proposal approved but wiring failed — reverted to pending, "
            "retry via this endpoint",
        )
    return {"approved": True, "wired": True, "proposal_id": proposal_id}


@governance_intelligence_router.post(
    "/integration_proposals/{proposal_id}/reject",
    status_code=200,
    response_model=IntegrationProposalRejectResult,
)
async def reject_integration_proposal(
    proposal_id: str,
    _: None = Depends(require_admin),
):
    """Reject a pending IntegrationProposal — no wiring, status only."""

    def _reject() -> None:
        with get_session() as session:
            proposal = session.get(IntegrationProposal, _proposal_uuid(proposal_id))
            if not proposal:
                raise HTTPException(status_code=404, detail="Proposal not found")
            if proposal.status != "pending":
                raise HTTPException(
                    status_code=409, detail=f"Proposal is {proposal.status}, not pending"
                )
            proposal.status = "rejected"
            session.commit()

    await asyncio.to_thread(_reject)
    return {"rejected": True, "proposal_id": proposal_id}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Audit & activity
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
