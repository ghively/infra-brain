"""infra_brain.api.routers.cve — CVE Detail API router.

Extracted from dashboard_api.py (Task 6 — Phase 3 god-file split).
Handler bodies are byte-identical to the originals; no logic changes.

Import rules:
  * MAY import from infra_brain.api.schemas, infra_brain.api._helpers,
    infra_brain.db.models, infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import OperationalError, ProgrammingError

from infra_brain.api._helpers import _now, _sla_string
from infra_brain.api.schemas import (
    CveAffectedHostOut,
    CveDetailOut,
    CveListItemOut,
    CveListOut,
    CveSolutionOut,
)
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import (
    R7Solution,
    R7Vulnerability,
    R7VulnCve,
    R7VulnSolution,
    Resource,
    VulnQueueItem,
)
from infra_brain.db.session import get_session
from infra_brain.db.vuln_status import OPEN_VULN_STATUSES

import logging

logger = logging.getLogger(__name__)

# CVE Detail view — same session gate and prefix as the main dashboard router.
# Routes: GET /api/dashboard/cves, GET /api/dashboard/cves/{cve_id}
cve_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)


def _vuln_for_cve(s, cve_id: str) -> Optional[R7Vulnerability]:
    """Resolve the richest R7Vulnerability backing a canonical CVE id via the
    r7_vuln_cves bridge (pick the highest CVSS when several slugs map to it)."""
    slugs = [r[0] for r in s.query(R7VulnCve.r7_vuln_id).filter(R7VulnCve.cve_id == cve_id).all()]
    if not slugs:
        return None
    return (
        s.query(R7Vulnerability)
        .filter(R7Vulnerability.r7_vuln_id.in_(slugs))
        .order_by(R7Vulnerability.cvss_v3_score.desc().nullslast())
        .first()
    )


@cve_router.get("/cves", response_model=CveListOut)
def list_cves(
    severity: Optional[str] = None,
    q: Optional[str] = None,
    has_exploit: bool = False,
    pci_fail_filter: bool = False,
    cvss_min: float = 0.0,
    cvss_max: float = 10.0,
    host: str = "",
    sla_overdue: bool = False,
    limit: int = 50,
    offset: int = 0,
):
    """Paged CVE list for the CVE Detail view, joined from the Rapid7 vuln defs.

    Read-only (SELECT only). One row per canonical CVE id (from the r7_vuln_cves
    bridge), decorated with the backing R7Vulnerability's severity/CVSS and the
    affected-host count derived from ``vuln_queue``. Paged + filterable by
    ``severity``; ``q`` is a substring over the CVE id. ``by_severity`` counts
    the filtered set.

    Filters:
      * ``severity`` — exact match against the R7Vulnerability severity band.
      * ``q`` — case-insensitive substring over the CVE id.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    try:
        with get_session() as s:
            # When filtering by host / SLA, restrict the CVE universe to the
            # canonical CVE ids present in the matching vuln_queue rows first.
            queue_cve_ids: Optional[set[str]] = None
            if host or sla_overdue:
                vq = s.query(VulnQueueItem.cve_id).outerjoin(
                    Resource, Resource.id == VulnQueueItem.resource_id
                )
                if host:
                    vq = vq.filter(Resource.name.ilike(f"%{host}%"))
                if sla_overdue:
                    vq = vq.filter(VulnQueueItem.sla_due.isnot(None)).filter(
                        VulnQueueItem.sla_due < _now()
                    )
                queue_cve_ids = {r[0] for r in vq.distinct().all()}
                if not queue_cve_ids:
                    return CveListOut(items=[], total=0, limit=limit, offset=offset, by_severity={})

            # TRK-113: default the /vulns CVE universe to CVEs backed by at
            # least one OPEN vuln_queue row, matching the /counts open_cves
            # badge (fleet.py::get_counts). The r7_vuln_cves bridge is NOT
            # pruned when a queue row auto-closes, so an unfiltered distinct-
            # count over the bridge over-reports vs the badge. Reuse the exact
            # canonical open-status vocabulary the badge uses — do not redefine
            # it — so both surfaces agree on what "open" means.
            open_cve_sq = (
                s.query(func.distinct(VulnQueueItem.cve_id))
                .filter(VulnQueueItem.status.in_(OPEN_VULN_STATUSES))
                .subquery()
            )

            # Distinct canonical CVE ids from the bridge (optionally filtered by id),
            # restricted to the open-backed universe.
            cve_q = s.query(func.distinct(R7VulnCve.cve_id).label("cve_id")).filter(
                R7VulnCve.cve_id.in_(s.query(open_cve_sq))
            )
            if q:
                cve_q = cve_q.filter(R7VulnCve.cve_id.ilike(f"%{q}%"))
            if queue_cve_ids is not None:
                cve_q = cve_q.filter(R7VulnCve.cve_id.in_(queue_cve_ids))
            cve_sq = cve_q.subquery()

            # Best backing vuln per CVE: join bridge → vuln, then dedup in Python on
            # the page slice only (page is small). Severity filter applies to the vuln.
            # Build the per-CVE → best-vuln map for the filtered universe first.
            bridge_rows = (
                s.query(R7VulnCve.cve_id, R7Vulnerability)
                .join(R7Vulnerability, R7Vulnerability.r7_vuln_id == R7VulnCve.r7_vuln_id)
                .filter(R7VulnCve.cve_id.in_(s.query(cve_sq.c.cve_id)))
            )
            if severity:
                bridge_rows = bridge_rows.filter(R7Vulnerability.severity == severity)

            best: dict[str, R7Vulnerability] = {}
            for cid, vuln in bridge_rows.all():
                cur = best.get(cid)
                if cur is None or (vuln.cvss_v3_score or 0) > (cur.cvss_v3_score or 0):
                    best[cid] = vuln

            # Enrichment filters applied to the resolved best-vuln per CVE.
            if has_exploit or pci_fail_filter or cvss_min > 0.0 or cvss_max < 10.0:
                best = {
                    cid: vuln
                    for cid, vuln in best.items()
                    if (not has_exploit or (vuln.exploits or 0) > 0)
                    and (not pci_fail_filter or bool(vuln.pci_fail))
                    and (cvss_min <= float(vuln.cvss_v3_score or 0.0) <= cvss_max)
                }

            # Affected-host counts per CVE from the canonical vuln_queue.
            host_counts: dict[str, int] = {}
            if best:
                for cid, n in (
                    s.query(
                        VulnQueueItem.cve_id, func.count(func.distinct(VulnQueueItem.resource_id))
                    )
                    .filter(VulnQueueItem.cve_id.in_(list(best.keys())))
                    .group_by(VulnQueueItem.cve_id)
                    .all()
                ):
                    host_counts[cid] = n

            by_severity: dict[str, int] = {}
            for vuln in best.values():
                sev = vuln.severity or "unknown"
                by_severity[sev] = by_severity.get(sev, 0) + 1

            ordered = sorted(
                best.items(),
                key=lambda kv: (kv[1].cvss_v3_score or 0.0, kv[0]),
                reverse=True,
            )
            total = len(ordered)
            page = ordered[offset : offset + limit]
            items = [
                CveListItemOut(
                    cve_id=cid,
                    severity=vuln.severity or "",
                    cvss=float(vuln.cvss_v3_score or 0.0),
                    title=vuln.title or "",
                    affected_hosts=host_counts.get(cid, 0),
                    exploits=vuln.exploits,
                    fix_available=bool(vuln.fix_available),
                    pci_fail=bool(vuln.pci_fail),
                    risk_score=float(vuln.risk_score or 0.0),
                )
                for cid, vuln in page
            ]
            return CveListOut(
                items=items, total=total, limit=limit, offset=offset, by_severity=by_severity
            )
    except (OperationalError, ProgrammingError) as exc:
        logger.error("cves endpoint db error (check migrations): %s", exc)
        return CveListOut(items=[], total=0, limit=limit, offset=offset, by_severity={})


@cve_router.get("/cves/{cve_id}", response_model=CveDetailOut)
def get_cve_detail(cve_id: str):
    """Detail for one canonical CVE id: the backing Rapid7 vuln definition plus
    the affected-host list derived from ``vuln_queue``.

    Read-only (SELECT only). 404 when the CVE id is not present in the Rapid7
    bridge (``r7_vuln_cves``). On a DB/migration error (``OperationalError`` /
    ``ProgrammingError``), degrades to an empty-but-valid ``CveDetailOut``
    instead of a 500 — matching the ``list_cves`` convention above.
    """
    try:
        with get_session() as s:
            slugs = [
                r[0]
                for r in s.query(R7VulnCve.r7_vuln_id).filter(R7VulnCve.cve_id == cve_id).all()
            ]
            if not slugs:
                raise HTTPException(status_code=404, detail=f"CVE {cve_id} not found")
            vuln = _vuln_for_cve(s, cve_id)

            # Affected hosts from the canonical vuln_queue (CVE → resource).
            host_rows = (
                s.query(VulnQueueItem, Resource)
                .outerjoin(Resource, Resource.id == VulnQueueItem.resource_id)
                .filter(VulnQueueItem.cve_id == cve_id)
                .all()
            )
            affected = [
                CveAffectedHostOut(
                    hostname=r.name if r else "—",
                    resource_id=str(v.resource_id) if v.resource_id else "",
                    status=v.status or "",
                    sla=_sla_string(v.sla_due),
                    kb_id=v.kb_id or "",
                    last_updated=v.last_updated,
                )
                for v, r in host_rows
            ]

            # Remediation solutions: walk r7_vuln_solutions → r7_solutions by slug.
            vuln_sol_rows = (
                s.query(R7VulnSolution).filter(R7VulnSolution.r7_vuln_id.in_(slugs)).all()
                if slugs
                else []
            )
            sol_ids = list({vs.r7_solution_id for vs in vuln_sol_rows})
            sol_rows = (
                s.query(R7Solution).filter(R7Solution.r7_solution_id.in_(sol_ids)).all()
                if sol_ids
                else []
            )
            solutions = [
                CveSolutionOut(
                    summary=sol.summary or "",
                    steps=sol.steps or "",
                    solution_type=sol.solution_type or "",
                    estimate=sol.estimate or "",
                )
                for sol in sol_rows
            ]

            # SLA stats across the affected vuln_queue rows. Normalize naive
            # datetimes to UTC before comparing (fixtures/SQLite may store naive).
            def _aware(dt: datetime | None) -> datetime | None:
                if dt is None:
                    return None
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

            now = _now()
            vuln_queue_rows = [v for v, _ in host_rows]
            sla_dues = [_aware(h.sla_due) for h in vuln_queue_rows if h.sla_due]
            sla_deadline = min(sla_dues, default=None)
            sla_overdue_count = sum(1 for d in sla_dues if d < now)

            return CveDetailOut(
                cve_id=cve_id,
                severity=(vuln.severity if vuln else "") or "",
                cvss=float((vuln.cvss_v3_score if vuln else 0.0) or 0.0),
                cvss_vector=(vuln.cvss_v3_vector if vuln else "") or "",
                cvss_v2=float((vuln.cvss_v2_score if vuln else 0.0) or 0.0),
                risk_score=float((vuln.risk_score if vuln else 0.0) or 0.0),
                title=(vuln.title if vuln else "") or "",
                exploits=vuln.exploits if vuln else 0,
                malware_kits=vuln.malware_kits if vuln else 0,
                fix_available=bool(vuln.fix_available) if vuln else False,
                pci_status=(vuln.pci_status if vuln else "") or "",
                pci_fail=bool(vuln.pci_fail) if vuln else False,
                published=vuln.published if vuln else None,
                denial_of_service=bool(vuln.denial_of_service) if vuln else False,
                categories=list(vuln.categories or []) if vuln else [],
                r7_vuln_ids=slugs,
                affected_hosts=affected,
                affected_host_count=len(affected),
                solutions=solutions,
                sla_deadline=sla_deadline,
                sla_overdue_count=sla_overdue_count,
            )
    except (OperationalError, ProgrammingError) as exc:
        logger.error("cve detail endpoint db error (check migrations): %s", exc)
        return CveDetailOut(cve_id=cve_id)
