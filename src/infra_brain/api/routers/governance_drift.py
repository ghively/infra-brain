"""infra_brain.api.routers.governance_drift -- Drift-event and notification routes.

Split from governance.py (refactor/split-governance-router).
Handler bodies are byte-identical to the originals; no logic changes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func

from infra_brain.api._helpers import _now, _s
from infra_brain.api.schemas import (
    DriftOut,
    DriftPageOut,
    DriftTrendOut,
    DriftTrendPoint,
    NotificationOut,
    NotificationPageOut,
)
from infra_brain.config import get_settings
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import (
    ConfluencePage,
    DriftEvent,
    JiraTicket,
    Resource,
    RootCauseNote,
    drift_recency,
)
from infra_brain.db.session import get_session
from infra_brain.drift_taxonomy import (
    describe_drift_rule,
    render_drift_value,
    summarize_drift,
)

governance_drift_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)

_TELEMETRY_FIELDS = frozenset({"ip", "risk_score", "vulnerabilities", "risk_factors", "last_seen"})


@governance_drift_router.get("/drift_events/trend", response_model=DriftTrendOut)
def get_drift_trend(
    days: int = 30,
    domain: str = "",
):
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(days=days)
    with get_session() as s:
        q = (
            s.query(
                func.date(DriftEvent.detected_at).label("date"),
                Resource.domain.label("domain"),
                func.count().label("count"),
            )
            .join(Resource, Resource.id == DriftEvent.resource_id)
            .filter(DriftEvent.detected_at >= cutoff)
        )
        if domain:
            q = q.filter(Resource.domain == domain)
        q = q.group_by(
            func.date(DriftEvent.detected_at),
            Resource.domain,
        ).order_by(func.date(DriftEvent.detected_at))
        rows = q.all()
    points = [
        DriftTrendPoint(date=str(row.date), count=row.count, domain=row.domain) for row in rows
    ]
    return DriftTrendOut(
        points=points,
        total=sum(p.count for p in points),
        domain_filter=domain,
        days=days,
    )


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Drift Events
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


_TELEMETRY_FIELDS = frozenset({"ip", "risk_score", "vulnerabilities", "risk_factors", "last_seen"})


@governance_drift_router.get("/drift_events/domains", response_model=list[str])
def list_drift_domains():
    """Return the distinct resource domains that have drift events recorded."""
    with get_session() as s:
        rows = (
            s.query(Resource.domain)
            .join(DriftEvent, DriftEvent.resource_id == Resource.id)
            .distinct()
            .order_by(Resource.domain)
            .all()
        )
        return [r.domain for r in rows]


@governance_drift_router.get("/drift_events", response_model=DriftPageOut)
def list_drift(
    status: str | None = None,
    domain: str | None = None,
    hours: int | None = None,
    suppress_telemetry: bool = False,
    limit: int = 500,
    offset: int = 0,
):
    # Clamp pagination BEFORE materializing rows — matches the sibling handlers
    # in this same file (list_notifications, below) and elsewhere in this layer
    # (fleet.py list_r7_assets:521-522, hosts.py:448-449/491-492). Without this,
    # a large ?limit or deep ?offset would pull unbounded rows into Python
    # (memory/DoS) — drift_events backs on a table that grows without bound.
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(DriftEvent, Resource).join(Resource, Resource.id == DriftEvent.resource_id)
        if status:
            q = q.filter(DriftEvent.status == status)
        if domain:
            q = q.filter(Resource.domain == domain)
        if hours:
            from datetime import timedelta

            q = q.filter(DriftEvent.detected_at >= _now() - timedelta(hours=hours))
        if suppress_telemetry:
            # Exclude routine R7 property-change noise: vuln-domain events where
            # the field is one of the high-volume telemetry fields.
            q = q.filter(
                ~((Resource.domain == "vuln") & (DriftEvent.field.in_(list(_TELEMETRY_FIELDS))))
            )
        # GitLab #163/#164: order by coalesce(last_seen_at, detected_at) —
        # detected_at is now the immutable FIRST-observation stamp, so it alone
        # would sort a still-recurring finding below a stale one-off.
        q = q.order_by(drift_recency().desc())
        total = q.count()
        rows = q.offset(offset).limit(limit).all()
        # M-4: scope both lookups to the drift_event_ids actually on this
        # page, not the whole jira_tickets/root_cause_notes table — only
        # these <= `limit` rows can ever be looked up in the dicts below.
        page_ids = [de.id for de, _ in rows]
        jira = {
            jt.drift_event_id: jt.jira_key
            for jt in s.query(JiraTicket).filter(JiraTicket.drift_event_id.in_(page_ids))
        }
        notes = {
            n.drift_event_id: n.explanation
            for n in s.query(RootCauseNote).filter(RootCauseNote.drift_event_id.in_(page_ids))
        }
        items = [
            DriftOut(
                id=str(de.id),
                domain=r.domain,
                hostname=r.name,
                field_name=de.field,
                old_value=_s(de.old_value),
                new_value=_s(de.new_value),
                detected_at=de.detected_at,
                status=de.status,
                jira_ticket=jira.get(de.id) or de.jira_key or "—",
                drift_type=de.drift_type,
                root_cause=notes.get(de.id, ""),
                # Drift readability: WHAT drifted + WHY it was flagged, both
                # derived here from the row's own columns. Pure functions over
                # (drift_type, field, old_value, new_value, hostname) — no
                # extra query, no schema change, and `summarize_drift` is
                # total (a malformed JSONB payload on one row can never blank
                # the page). `render_drift_value` peels the writer-specific
                # envelope ({"v": ...} / {"value": ...}) for the drawer's
                # before/after diff; old_value/new_value above are left at
                # their exact prior stringification.
                summary=summarize_drift(
                    de.drift_type, de.field, de.old_value, de.new_value, r.name
                ),
                rule=describe_drift_rule(de.drift_type),
                old_display=render_drift_value(de.old_value),
                new_display=render_drift_value(de.new_value),
            )
            for de, r in rows
        ]
        return DriftPageOut(items=items, total=total, limit=limit, offset=offset)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Notifications
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@governance_drift_router.get("/notifications", response_model=NotificationPageOut)
def list_notifications(type: str | None = None, limit: int = 500, offset: int = 0):
    # Clamp pagination BEFORE materializing rows — matches the sibling handlers
    # (fleet.py list_r7_assets:521-522, hosts.py:448-449/491-492). Without this,
    # `fetch = offset + limit` below would let a large ?limit or deep ?offset
    # pull unbounded JiraTicket/ConfluencePage rows into Python (memory/DoS).
    # The true-COUNT total (below) is computed independently and is unaffected.
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    out: list[NotificationOut] = []
    # FE-6: report the true DB-wide total via COUNT (respecting the type filter),
    # not len(out) — the per-type .limit() capped materialization, so a
    # page-capped number was being presented as a grand total. Materialize
    # offset+limit rows per source so the post-merge page slice stays correct.
    fetch = offset + limit
    total = 0
    # TRK-178 follow-up (GitLab #158): deep-link URLs for Jira/Confluence rows.
    # Base URLs only — never tokens/credentials. Empty-string-safe: an unset
    # jira_url/confluence_url leaves the field None rather than producing a
    # malformed URL with an empty prefix (e.g. "/browse/JIRA-1").
    settings = get_settings()
    jira_base = (settings.jira_url or "").strip()
    confluence_base = (settings.confluence_url or "").strip()
    with get_session() as s:
        if type in (None, "jira"):
            total += s.query(JiraTicket).count()
            for jt in s.query(JiraTicket).order_by(JiraTicket.created_at.desc()).limit(fetch):
                out.append(
                    NotificationOut(
                        type="jira",
                        target=jt.jira_key,
                        title=jt.jira_key,
                        domain="—",
                        created=jt.created_at,
                        status="open",
                        jira_url=f"{jira_base}/browse/{jt.jira_key}" if jira_base else None,
                    )
                )
        if type in (None, "confluence"):
            total += s.query(ConfluencePage).count()
            for cp in (
                s.query(ConfluencePage).order_by(ConfluencePage.last_updated.desc()).limit(fetch)
            ):
                out.append(
                    NotificationOut(
                        type="confluence",
                        target=cp.page_id,
                        title=f"{cp.domain} page",
                        domain=cp.domain,
                        created=cp.last_updated,
                        status="synced",
                        confluence_url=(
                            f"{confluence_base}/pages/viewpage.action?pageId={cp.page_id}"
                            if confluence_base
                            else None
                        ),
                    )
                )
    out.sort(key=lambda n: n.created, reverse=True)
    page = out[offset : offset + limit]
    return NotificationPageOut(items=page, total=total, limit=limit, offset=offset)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Intelligence loop
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
