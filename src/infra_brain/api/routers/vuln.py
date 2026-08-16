"""infra_brain.api.routers.vuln — Vulnerability queue + remediation API router.

Extracted from dashboard_api.py (Task 7 — Phase 3 god-file split).
Handler bodies are byte-identical to the originals; no logic changes.

Import rules:
  * MAY import from infra_brain.api.schemas, infra_brain.api._helpers,
    infra_brain.db.models, infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from infra_brain.api._envelope import paginate
from infra_brain.api._helpers import _best_vuln_by_cve, _now, _sla_string
from infra_brain.api.schemas import (
    RemediationOut,
    RemediationPageOut,
    RemediationRollupOut,
    RemediationRollupRowOut,
    VulnOut,
    VulnPageOut,
)
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import (
    EolRegistry,
    ProposedAction,
    Resource,
    VulnQueueItem,
)
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

# M-4: has_exploit/pci_only filtering happens in Python below (the
# enrichment values aren't known at the SQL layer -- see the comment on
# `rows` in list_vulns), so `total`/priority-sort must be computed over the
# full SQL-matching candidate set, not a naive SQL-level offset/limit page.
# Without SOME upper bound, an unfiltered (or lightly filtered) request
# against a large host x CVE vuln_queue can materialize the ENTIRE table
# into Python in one request -- the worst-case scenario this constant
# guards against. If the true candidate set exceeds this ceiling, `total`/
# ranking reflect only the earliest-SLA-due _VULN_CANDIDATE_CEILING rows
# rather than the true full set: an accepted degradation for a
# pathological unfiltered request, traded off against unbounded memory/
# latency risk. A caller that wants the true total for a narrower slice
# should pass severity/status/host/sla_overdue -- already pushed to SQL
# above -- which keeps the candidate set well under this ceiling in
# practice.
_VULN_CANDIDATE_CEILING = 20_000

# Vulnerability queue + remediation — same session gate and prefix as the main
# dashboard router.
# Routes: GET /api/dashboard/vulnerabilities, GET /api/dashboard/remediation
vuln_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)


@vuln_router.get("/vulnerabilities", response_model=VulnPageOut)
def list_vulns(
    severity: str | None = None,
    status: str | None = None,
    host: str = "",
    sla_overdue: bool = False,
    has_exploit: bool = False,
    pci_only: bool = False,
    limit: int = 200,
    offset: int = 0,
):
    """Paged, filtered vulnerability queue with a real CVSS join.

    Read-only. Pagination + filters are pushed to SQL where possible
    (severity/status/host/sla_overdue); ``cvss``/``pci_fail``/``exploits`` come
    from the richest backing R7Vulnerability resolved through the r7_vuln_cves
    bridge. ``has_exploit``/``pci_only`` filter on those enriched values for the
    returned page. Sorted by triage priority (descending).
    """
    from infra_brain.triage import compute_vuln_priority

    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        pci = {
            rid: risk
            for rid, risk in s.query(EolRegistry.resource_id, EolRegistry.pci_risk_score).all()
            if risk is not None
        }
        q = s.query(VulnQueueItem, Resource).outerjoin(
            Resource, Resource.id == VulnQueueItem.resource_id
        )
        if severity:
            # vuln_queue stores canonical lowercase bands; normalize the incoming
            # filter so a capitalized param ("Critical") can't silently match zero.
            from infra_brain.db.severity import normalize_severity

            q = q.filter(VulnQueueItem.severity == (normalize_severity(severity) or severity))
        if status:
            q = q.filter(VulnQueueItem.status == status)
        if host:
            q = q.filter(Resource.name.ilike(f"%{host}%"))
        if sla_overdue:
            q = q.filter(VulnQueueItem.sla_due.isnot(None)).filter(VulnQueueItem.sla_due < _now())

        # has_exploit/pci_only filter on values resolved per-CVE via _best_vuln_by_cve,
        # which isn't known at the SQL layer -- so total/pagination must be computed over
        # the full enriched+filtered candidate set, not a SQL-level offset/limit page, or
        # total and items go out of sync (they silently did before this fix: total counted
        # every SQL-matching row while items only counted the ones surviving the
        # enrichment filter on that one page).
        rows = (
            q.order_by(VulnQueueItem.sla_due.asc().nullslast())
            .limit(_VULN_CANDIDATE_CEILING)
            .all()
        )

        best = _best_vuln_by_cve(s, [v.cve_id for v, _ in rows])
        candidates: list[VulnOut] = []
        for v, r in rows:
            rv = best.get(v.cve_id)
            cvss = float(rv.cvss_v3_score or 0.0) if rv else 0.0
            exploits = int(rv.exploits or 0) if rv else 0
            pci_fail = bool(rv.pci_fail) if rv else False
            if has_exploit and exploits <= 0:
                continue
            if pci_only and not pci_fail:
                continue
            candidates.append(
                VulnOut(
                    cve=v.cve_id,
                    host=r.name if r else "—",
                    pkg=v.kb_id or "—",
                    severity=v.severity,
                    cvss=cvss,
                    sla=_sla_string(v.sla_due),
                    status=v.status,
                    priority=compute_vuln_priority(v.severity, v.sla_due, pci.get(v.resource_id)),
                    pci_fail=pci_fail,
                    exploits=exploits,
                )
            )
        candidates.sort(key=lambda x: x.priority, reverse=True)
        total = len(candidates)
        out = candidates[offset : offset + limit]
        return VulnPageOut(items=out, total=total, limit=limit, offset=offset)


@vuln_router.get(
    "/remediation", response_model=RemediationPageOut | RemediationRollupOut
)
def list_remediation(
    status: str | None = None,
    view: str = "detail",
    limit: int = 500,
    offset: int = 0,
):
    """Pending ``ProposedAction`` queue, or a dedup/rollup aggregate over it.

    ``view="detail"`` (default) is the original unaggregated list.
    ``view="rollup"`` (TRK-255) groups pending rows by
    ``(action_type, payload->>'field')`` — ``field`` is a stable string set by
    the drafting agent, unlike ``payload->>'plan'`` which is LLM-drafted free
    text that can vary between rows expressing the same underlying
    recommendation (see the design note in ``api/schemas.py``). Follows the
    ``view=detail|aggregated`` toggle pattern in ``fleet.py``'s
    ``list_software``.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        if view == "rollup":
            from sqlalchemy import func

            # ``payload->>'field'`` indexing follows the established repo
            # convention (see mcp_server.py's ``payload["authored_by"]``
            # usage) rather than the ``cast(..., String)`` form — this is a
            # cross-dialect JSON column (JSONB on postgres, JSON/TEXT on
            # sqlite; see db/models/_base.py), and ``.as_string()`` is the
            # form already proven to work on both dialects in this repo.
            #
            # Sample targets are fetched per-group (bounded by the page size,
            # not the underlying row count) rather than via ``array_agg`` --
            # array_agg is postgres-only and this endpoint must also run
            # against the sqlite test suite.
            field_expr = ProposedAction.payload["field"].as_string()
            grp = (
                s.query(
                    ProposedAction.action_type,
                    field_expr.label("field"),
                    func.count().label("count"),
                )
                .filter(ProposedAction.status == "pending")
                .group_by(ProposedAction.action_type, field_expr)
                .order_by(func.count().desc())
            )
            all_rows = grp.all()
            total = len(all_rows)
            rows = all_rows[offset : offset + limit]
            items = []
            for r in rows:
                sample_q = s.query(ProposedAction.target).filter(
                    ProposedAction.status == "pending",
                    ProposedAction.action_type == r.action_type,
                    field_expr == r.field,
                )
                sample_targets = [t for (t,) in sample_q.limit(5).all()]
                items.append(
                    RemediationRollupRowOut(
                        action_type=r.action_type,
                        field=r.field,
                        count=int(r.count),
                        sample_targets=sample_targets,
                    )
                )
            return RemediationRollupOut(items=items, total=total, limit=limit, offset=offset)

        q = s.query(ProposedAction)
        if status:
            q = q.filter(ProposedAction.status == status)
        q = q.order_by(ProposedAction.created_at.desc())
        items_raw, total = paginate(q, limit=limit, offset=offset)
        items = [
            RemediationOut(
                id=str(a.id),
                agent=a.agent,
                action_type=a.action_type,
                target=a.target,
                confidence=a.confidence,
                status=a.status,
                approved_by=a.approved_by or "—",
                result_url=a.result_url or "—",
                created_at=a.created_at,
            )
            for a in items_raw
        ]
        return RemediationPageOut(items=items, total=total, limit=limit, offset=offset)
