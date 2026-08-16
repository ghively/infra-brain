"""infra_brain.api.routers.governance_audit -- Audit log, agent activity and decision routes.

Split from governance.py (refactor/split-governance-router).
Handler bodies are byte-identical to the originals; no logic changes.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import String, func, or_

from infra_brain.api._envelope import paginate
from infra_brain.api._helpers import (
    _AUDIT_CATEGORY_KEYWORDS,
    _audit_category,
    _s,
)
from infra_brain.api.schemas import (
    ActivityOut,
    ActivityPageOut,
    AuditOut,
    AuditPageOut,
    DecisionOut,
    DecisionPageOut,
)
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import (
    AgentActionLog,
    AgentDecisionLog,
    AuditLog,
)
from infra_brain.db.session import get_session

governance_audit_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)


@governance_audit_router.get("/activity", response_model=ActivityPageOut)
def list_activity(
    agent: Optional[str] = None,
    verdict: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
):
    # M-3: clamp before use — matches sibling list_audit's clamp below and
    # keeps the echoed response limit/offset consistent with what was
    # actually queried (paginate() also clamps as a backstop).
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(AgentActionLog)
        if agent:
            q = q.filter(AgentActionLog.agent == agent)
        if verdict:
            q = q.filter(AgentActionLog.verdict == verdict)
        q = q.order_by(AgentActionLog.ts.desc())
        items_raw, total = paginate(q, limit=limit, offset=offset)
        items = [
            ActivityOut(
                ts=a.ts,
                agent=a.agent,
                domain=a.domain or "",
                tool=a.tool,
                verdict=a.verdict,
                status=a.status or "",
                latency_ms=a.latency_ms or 0.0,
                args_summary=a.args_summary or "",
                error=a.error or "",
            )
            for a in items_raw
        ]
        return ActivityPageOut(items=items, total=total, limit=limit, offset=offset)


@governance_audit_router.get("/decisions", response_model=DecisionPageOut)
def list_decisions(
    agent: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
):
    # M-3: clamp before use, see list_activity above for rationale.
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(AgentDecisionLog)
        if agent:
            q = q.filter(AgentDecisionLog.agent == agent)
        if run_id:
            # A full UUID matches exactly (backend-agnostic); a fragment falls
            # back to a substring match on the text form, mirroring the legacy
            # CAST(run_id AS TEXT) filter.
            try:
                q = q.filter(AgentDecisionLog.run_id == uuid.UUID(run_id))
            except ValueError:
                q = q.filter(func.cast(AgentDecisionLog.run_id, String).contains(run_id))
        q = q.order_by(AgentDecisionLog.ts.desc())
        items_raw, total = paginate(q, limit=limit, offset=offset)
        items = [
            DecisionOut(
                agent=d.agent,
                domain=d.domain,
                run_id=_s(d.run_id),
                iteration=d.iteration,
                decision_summary=d.decision_summary,
                reasoning_text=d.reasoning_text,
                tools_chosen=list(d.tools_chosen or []),
                ts=d.ts,
            )
            for d in items_raw
        ]
        return DecisionPageOut(items=items, total=total, limit=limit, offset=offset)


@governance_audit_router.get("/audit_log", response_model=AuditPageOut)
def list_audit(
    category: Optional[str] = None,
    allowed: Optional[bool] = None,
    limit: int = 200,
    offset: int = 0,
):
    """Paged audit log with a SQL-side category pre-filter.

    Read-only. ``category`` (dlp/allowlist/readonly) is pushed to SQL via a
    keyword pre-filter on ``denial_reason`` so pagination totals reflect the
    filtered set. Ordered by ``ts`` descending.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(AuditLog)
        if allowed is not None:
            q = q.filter(AuditLog.allowed == allowed)
        if category in _AUDIT_CATEGORY_KEYWORDS:
            kws = _AUDIT_CATEGORY_KEYWORDS[category]
            q = q.filter(or_(*[func.lower(AuditLog.denial_reason).contains(kw) for kw in kws]))
        elif category == "readonly":
            # readonly == NOT (dlp âˆª allowlist): null/empty reason or no keyword hit.
            all_kws = _AUDIT_CATEGORY_KEYWORDS["dlp"] + _AUDIT_CATEGORY_KEYWORDS["allowlist"]
            denial_low = func.lower(func.coalesce(AuditLog.denial_reason, ""))
            for kw in all_kws:
                q = q.filter(~denial_low.contains(kw))

        total = q.count()
        rows = q.order_by(AuditLog.ts.desc()).offset(offset).limit(limit).all()
        items = [
            AuditOut(
                ts=a.ts,
                agent=a.agent,
                tool=a.tool,
                category=_audit_category(a.denial_reason or "passed"),
                allowed=a.allowed,
                reason=a.denial_reason or "passed",
                ihash=a.input_hash,
                ohash=a.output_hash or "—",
            )
            for a in rows
        ]
        return AuditPageOut(items=items, total=total, limit=limit, offset=offset)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Inventory reconciliation  (NEW page)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
