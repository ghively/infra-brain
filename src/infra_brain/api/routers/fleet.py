"""infra_brain.api.routers.fleet — Fleet / health / version dashboard routes.

Extracted from dashboard_api.py (Task 10 — Phase 3 god-file split).
Handler bodies are byte-identical to the originals; no logic changes.

Import rules:
  * MAY import from infra_brain.api.schemas, infra_brain.api._helpers,
    infra_brain.db.models, infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, or_
from sqlalchemy.exc import OperationalError, ProgrammingError

from infra_brain.agents.llm_base import RECURSION_LIMIT_MARKER
from infra_brain.api._envelope import paginate
from infra_brain.api._helpers import _RUN_STATUS, _humanize_cron, _next_run_for, _now
from infra_brain.api.schemas import (
    CollectionRunPageOut,
    CountsOut,
    FleetAssetOut,
    FleetAssetsOut,
    FleetOsCountOut,
    FleetRiskBandOut,
    FleetSummaryOut,
    HealthItem,
    R7AssetAddressOut,
    R7AssetConfigOut,
    R7AssetDetailOut,
    R7AssetUserOut,
    R7SiteOut,
    R7SitesPageOut,
    R7TagOut,
    R7TagsPageOut,
    RunOut,
    ScanOut,
    ScanPointPageOut,
    SelfcheckOut,
    SoftwareAggRowOut,
    SoftwareDetailRowOut,
    SoftwareInventoryOut,
    SoftwareSummaryOut,
    SweepAcceptedOut,
    SweepDetailOut,
    SweepDomainOut,
    SweepListOut,
    SweepStatusCountsOut,
    SweepSummaryOut,
    SystemHealthPageOut,
    VersionOut,
)
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import (
    AgentDecisionLog,
    CollectionRun,
    ComplianceViolation,
    DriftEvent,
    EolRegistry,
    Instinct,
    InventoryReconcileEvent,
    R7Asset,
    R7AssetAddress,
    R7AssetConfig,
    R7AssetUser,
    R7Site,
    R7Software,
    R7Tag,
    Resource,
    ScanPoint,
    VulnQueueItem,
)
from infra_brain.db.session import get_session
from infra_brain.db.vuln_status import OPEN_VULN_STATUSES
from infra_brain.etl.spec import agent_specs

# All real CollectionRun.status values the sweep view tracks (spec §2.11 —
# pending interrupts must stay visible, not get folded into a generic
# "failed" bucket). Any status outside this set is simply not counted —
# there are none today, but this stays a fixed, known set rather than
# growing dynamically off whatever happens to be in the DB.
_SWEEP_STATUSES = (
    "completed",
    "partial",
    "failed",
    "retry_exhausted",
    "interrupt_pending",
    "in_progress",
    "skipped",
)

logger = logging.getLogger(__name__)

# Fleet / health / version routes — same session gate and prefix as the main
# dashboard router.
fleet_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard summary counts — server-side COUNT queries for the home page cards.
# These bypass the .limit(500) caps on the list endpoints and return the real
# totals. The frontend should prefer these over len(array) for any metric shown
# on the home page or nav badges.
# ─────────────────────────────────────────────────────────────────────────────


@fleet_router.get("/counts", response_model=CountsOut)
def get_counts():
    """Server-side aggregate counts for home page health cards and nav badges.

    All values come from COUNT queries — never affected by the .limit(N) caps
    on the corresponding list endpoints.
    """
    with get_session() as s:
        open_drift = s.query(DriftEvent).filter(DriftEvent.status == "open").count()
        # FE-9: was `status != "resolved"`. Nothing ever wrote "resolved" (or
        # any other closed status) onto a vuln_queue row once Rapid7 stopped
        # reporting a CVE for an asset — VulnAgent's reconciliation pass (see
        # agents/vuln.py::_write_vuln_queue) now closes those out, and the
        # canonical "still open" vocabulary lives in db/vuln_status.py so this
        # reader and every other one (fleet_health.py, remediation.py, ...)
        # agree on what "open" means instead of each inventing its own
        # ==/!= predicate against a different status literal.
        #
        # FE-9 (label integrity): "Open CVEs" must count CVEs, not raw
        # vuln_queue rows. A vuln_queue row is a (host, CVE) instance, so the
        # old row COUNT reported a CVE affecting N hosts as N. Count distinct
        # cve_id among OPEN rows so the number reflects distinct open CVEs on
        # the fleet, matching its label.
        #
        # NOTE: /vulns (cve.py list_cves) restricts its CVE universe to the
        # same open-backed population as this badge, reusing the same
        # OPEN_VULN_STATUSES vocabulary (TRK-113, fixed@5d36f8f) — the two
        # surfaces agree by construction.
        open_cves = (
            s.query(func.count(func.distinct(VulnQueueItem.cve_id)))
            .filter(VulnQueueItem.status.in_(OPEN_VULN_STATUSES))
            .scalar()
        ) or 0
        # TRK-166 follow-up: fleet-wide distinct-CVE counts by severity, same
        # OPEN_VULN_STATUSES vocabulary as open_cves above, so pages that
        # paginate the vuln list (e.g. Vulns.tsx) can show real severity
        # totals instead of page-limited client-side counts.
        critical_cves = (
            s.query(func.count(func.distinct(VulnQueueItem.cve_id)))
            .filter(VulnQueueItem.status.in_(OPEN_VULN_STATUSES))
            .filter(VulnQueueItem.severity == "critical")
            .scalar()
        ) or 0
        severe_cves = (
            s.query(func.count(func.distinct(VulnQueueItem.cve_id)))
            .filter(VulnQueueItem.status.in_(OPEN_VULN_STATUSES))
            .filter(VulnQueueItem.severity.in_(("severe", "high")))
            .scalar()
        ) or 0
        # TRK-167 follow-up: fleet-wide ComplianceViolation open/resolved
        # counts, same pattern as critical_cves/severe_cves above, so pages
        # that paginate the compliance list (Compl.tsx) can show real fleet
        # totals instead of page-limited client-side counts.
        compliance_open = (
            s.query(ComplianceViolation).filter(ComplianceViolation.status == "open").count()
        )
        compliance_resolved = (
            s.query(ComplianceViolation)
            .filter(ComplianceViolation.status == "resolved")
            .count()
        )
        eol_overdue = (
            s.query(EolRegistry)
            .filter(EolRegistry.eol_date.isnot(None))
            .filter(EolRegistry.eol_date < _now())
            .count()
        )
        eol_total = s.query(EolRegistry).count()
        invrec_proposed = (
            s.query(InventoryReconcileEvent)
            .filter(InventoryReconcileEvent.status == "proposed")
            .count()
        )
        invrec_total = s.query(InventoryReconcileEvent).count()
        total_resources = s.query(Resource).count()
        # TRK-182 follow-up: was hardcoded 0.7 — now a Settings field shared
        # with Intprops.tsx so the two surfaces can't drift apart.
        from infra_brain.config import get_settings

        applied_instincts = (
            s.query(Instinct)
            .filter(Instinct.confidence >= get_settings().integration_confidence_gate)
            .count()
        )
        return CountsOut(
            open_drift=open_drift,
            open_cves=open_cves,
            critical_cves=critical_cves,
            severe_cves=severe_cves,
            compliance_open=compliance_open,
            compliance_resolved=compliance_resolved,
            eol_overdue=eol_overdue,
            eol_total=eol_total,
            invrec_proposed=invrec_proposed,
            invrec_total=invrec_total,
            total_resources=total_resources,
            applied_instincts=applied_instincts,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Collection Runs
# ─────────────────────────────────────────────────────────────────────────────


@fleet_router.get("/collection_runs", response_model=CollectionRunPageOut)
def list_runs(
    status: str | None = None,
    domain: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    # M-3: clamp before use so the paged response echoes what was actually
    # queried (paginate() also clamps as a backstop, but the local clamp
    # keeps the returned limit/offset consistent — matches the sibling
    # pattern used by list_r7_assets/list_sites/list_tags below).
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(CollectionRun)
        if domain:
            q = q.filter(CollectionRun.domain == domain)
        if status:
            # Build reverse mapping: display value → set of raw DB status values.
            # See api/_helpers.py::_RUN_STATUS for the full raw->display map (incl. the
            # Phase-2 retry_exhausted/interrupt_pending states).
            # Multiple raw values could theoretically map to the same display value, so
            # collect all of them and use .in_() to keep total/items from the same universe.
            _reverse: dict[str, list[str]] = {}
            for raw_val, display_val in _RUN_STATUS.items():
                _reverse.setdefault(display_val, []).append(raw_val)
            matching_raw = _reverse.get(status, [])
            if matching_raw:
                q = q.filter(CollectionRun.status.in_(matching_raw))
            else:
                # Requested display status has no mapping — return empty result.
                return CollectionRunPageOut(items=[], total=0, limit=limit, offset=offset)
        q = q.order_by(CollectionRun.started_at.desc())
        items_raw, total = paginate(q, limit=limit, offset=offset)
        out: list[RunOut] = []
        for run in items_raw:
            mapped = _RUN_STATUS.get(run.status, run.status)
            dur = 0
            if run.finished_at and run.started_at:
                dur = int((run.finished_at - run.started_at).total_seconds())
            out.append(
                RunOut(
                    domain=run.domain,
                    trigger_type=run.trigger_type,
                    status=mapped,
                    records_collected=run.resources_found,
                    detail_rows_written=run.detail_rows_written or 0,
                    started_at=run.started_at,
                    duration_seconds=dur,
                    error_message=run.error_message,
                )
            )
        return CollectionRunPageOut(items=out, total=total, limit=limit, offset=offset)


# ─────────────────────────────────────────────────────────────────────────────
# Sweeps (Phase 4 Task 4) — one sweep-graph invocation grouped by sweep_id
# ─────────────────────────────────────────────────────────────────────────────


def _recursion_counts_by_run(s, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """run_id -> count of AgentDecisionLog rows that hit the recursion limit.

    Mirrors callbacks/anomaly.py's own query against
    ``AgentDecisionLog.decision_summary == RECURSION_LIMIT_MARKER`` — reused
    rather than reimplemented, just grouped by run instead of by agent/window.
    """
    if not run_ids:
        return {}
    rows = (
        s.query(AgentDecisionLog.run_id, func.count(AgentDecisionLog.id))
        .filter(
            AgentDecisionLog.run_id.in_(run_ids),
            AgentDecisionLog.decision_summary == RECURSION_LIMIT_MARKER,
        )
        .group_by(AgentDecisionLog.run_id)
        .all()
    )
    return {rid: int(cnt) for rid, cnt in rows if rid is not None}


def _sweep_status_counts(runs: list[CollectionRun]) -> SweepStatusCountsOut:
    counts = dict.fromkeys(_SWEEP_STATUSES, 0)
    for r in runs:
        if r.status in counts:
            counts[r.status] += 1
    return SweepStatusCountsOut(**counts)


def _sweep_window(runs: list[CollectionRun]) -> tuple[Any, Any, int | None]:
    started = min((r.started_at for r in runs if r.started_at), default=None)
    finished_at_vals = [r.finished_at for r in runs if r.finished_at]
    finished = max(finished_at_vals) if len(finished_at_vals) == len(runs) else None
    duration = int((finished - started).total_seconds()) if (started and finished) else None
    return started, finished, duration


def _domain_tier(domain: str, specs: dict) -> str:
    spec = specs.get(domain)
    return spec.tier.value if spec is not None else "unknown"


@fleet_router.get("/sweeps", response_model=SweepListOut)
def list_sweeps(limit: int = 20):
    """Last N sweeps (grouped ``CollectionRun.sweep_id``), newest first.

    Each entry is a per-sweep summary: start/finish window, duration, counts
    over every real run status (spec §2.11 — pending interrupts stay
    visible), and the max_iters/recursion-limit hit count for the sweep.
    """
    limit = max(1, min(limit, 200))
    with get_session() as s:
        sweep_id_rows = (
            s.query(CollectionRun.sweep_id, func.min(CollectionRun.started_at).label("min_started"))
            .filter(CollectionRun.sweep_id.isnot(None))
            .group_by(CollectionRun.sweep_id)
            .order_by(func.min(CollectionRun.started_at).desc())
            .limit(limit)
            .all()
        )
        sweep_ids = [row[0] for row in sweep_id_rows]
        if not sweep_ids:
            return SweepListOut(items=[], total=0, limit=limit)

        all_runs = s.query(CollectionRun).filter(CollectionRun.sweep_id.in_(sweep_ids)).all()
        by_sweep: dict[uuid.UUID, list[CollectionRun]] = {}
        for r in all_runs:
            by_sweep.setdefault(r.sweep_id, []).append(r)
        recursion_counts = _recursion_counts_by_run(s, [r.id for r in all_runs])

        items: list[SweepSummaryOut] = []
        for sid in sweep_ids:
            runs = by_sweep.get(sid, [])
            started, finished, duration = _sweep_window(runs)
            max_iters_hit_count = sum(recursion_counts.get(r.id, 0) for r in runs)
            items.append(
                SweepSummaryOut(
                    sweep_id=str(sid),
                    started_at=started,
                    finished_at=finished,
                    duration_seconds=duration,
                    domain_count=len(runs),
                    status_counts=_sweep_status_counts(runs),
                    max_iters_hit_count=max_iters_hit_count,
                )
            )
        return SweepListOut(items=items, total=len(sweep_ids), limit=limit)


@fleet_router.get("/sweeps/{sweep_id}", response_model=SweepDetailOut)
def get_sweep(sweep_id: str):
    """Per-domain detail for a single sweep_id."""
    try:
        sid = uuid.UUID(sweep_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown sweep: {sweep_id}")

    with get_session() as s:
        runs = (
            s.query(CollectionRun)
            .filter(CollectionRun.sweep_id == sid)
            .order_by(CollectionRun.domain)
            .all()
        )
        if not runs:
            raise HTTPException(status_code=404, detail=f"Unknown sweep: {sweep_id}")

        specs = agent_specs()
        recursion_counts = _recursion_counts_by_run(s, [r.id for r in runs])
        started, finished, duration = _sweep_window(runs)

        domains: list[SweepDomainOut] = []
        for r in runs:
            r_dur = None
            if r.started_at and r.finished_at:
                r_dur = int((r.finished_at - r.started_at).total_seconds())
            domains.append(
                SweepDomainOut(
                    domain=r.domain,
                    tier=_domain_tier(r.domain, specs),
                    status=r.status,
                    started_at=r.started_at,
                    finished_at=r.finished_at,
                    duration_seconds=r_dur,
                    records_collected=r.resources_found,
                    error_message=r.error_message,
                    max_iters_hits=recursion_counts.get(r.id, 0),
                )
            )
        return SweepDetailOut(
            sweep_id=str(sid),
            started_at=started,
            finished_at=finished,
            duration_seconds=duration,
            status_counts=_sweep_status_counts(runs),
            max_iters_hit_count=sum(d.max_iters_hits for d in domains),
            domains=domains,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Scan Schedule
# ─────────────────────────────────────────────────────────────────────────────


@fleet_router.get("/scan_points", response_model=ScanPointPageOut)
def list_scans(limit: int = 100, offset: int = 0):
    from infra_brain.scheduler import _DEFAULT_SCHEDULES

    # M-3: clamp before use, see list_runs above for rationale.
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(ScanPoint)
        scan_rows, total = paginate(q, limit=limit, offset=offset)
        # Per-domain last run + last successful run, mirroring the legacy
        # Streamlit scan-schedule join against collection_runs.
        last_run = dict(
            s.query(CollectionRun.domain, func.max(CollectionRun.started_at))
            .group_by(CollectionRun.domain)
            .all()
        )
        last_success = dict(
            s.query(CollectionRun.domain, func.max(CollectionRun.started_at))
            .filter(CollectionRun.status == "completed")
            .group_by(CollectionRun.domain)
            .all()
        )
    out = [
        ScanOut(
            domain=sp.domain,
            method=sp.method,
            endpoint=sp.endpoint,
            schedule=_humanize_cron(_DEFAULT_SCHEDULES.get(sp.domain, sp.schedule)),
            status="active" if sp.status == "active" else "degraded",
            # FE-16: was a hardcoded "—" placeholder even though the cron
            # string needed to compute it (_DEFAULT_SCHEDULES) was already
            # in scope — see _next_run_for.
            next_run=_next_run_for(_DEFAULT_SCHEDULES.get(sp.domain, sp.schedule)),
            last_run=last_run.get(sp.domain),
            last_success=last_success.get(sp.domain),
        )
        for sp in scan_rows
    ]
    return ScanPointPageOut(items=out, total=total, limit=limit, offset=offset)


# ─────────────────────────────────────────────────────────────────────────────
# Settings & health
# ─────────────────────────────────────────────────────────────────────────────


@fleet_router.get("/system_health", response_model=SystemHealthPageOut)
def system_health():
    items: list[HealthItem] = []
    # Postgres
    try:
        from sqlalchemy import text

        with get_session() as s:
            s.execute(text("SELECT 1"))
        items.append(HealthItem(name="PostgreSQL", detail="connected", status="ok"))
    except Exception as exc:  # noqa: BLE001
        items.append(HealthItem(name="PostgreSQL", detail=str(exc)[:80], status="down"))
    # Redis
    try:
        import redis

        from infra_brain.config import get_settings

        client = redis.from_url(get_settings().redis_url, socket_connect_timeout=2)
        client.ping()
        items.append(HealthItem(name="Redis", detail="connected", status="ok"))
    except Exception as exc:  # noqa: BLE001
        items.append(HealthItem(name="Redis", detail=str(exc)[:80], status="degraded"))
    total = len(items)
    return SystemHealthPageOut(items=items, total=total, limit=total, offset=0)


# ─────────────────────────────────────────────────────────────────────────────
# Version / build info
# ─────────────────────────────────────────────────────────────────────────────

APP_VERSION = "0.1.0"


@fleet_router.get("/version", response_model=VersionOut)
def get_version():
    import os

    env = os.environ.get("INFRA_BRAIN_ENV", "development")
    return {"version": APP_VERSION, "environment": env}


# ─────────────────────────────────────────────────────────────────────────────
# Self-check (P3.1) — the subset of .claude/scripts/dev_status.py's 10 checks
# that only need already-imported live objects or the source tree already
# baked into the image (no git, no dev venv, no k8s manifests it can't see).
# See selfcheck.py's module docstring for exactly what's covered and why the
# rest stays dev-only.
# ─────────────────────────────────────────────────────────────────────────────


@fleet_router.get("/selfcheck", response_model=SelfcheckOut)
def get_selfcheck():
    from infra_brain.selfcheck import run_selfcheck

    return run_selfcheck()


# ─────────────────────────────────────────────────────────────────────────────
# Manual sweep  (authenticated; triggers a READ-ONLY collection run)
# ─────────────────────────────────────────────────────────────────────────────


@fleet_router.post("/sweeps/{domain}", status_code=202, response_model=SweepAcceptedOut)
def trigger_sweep(domain: str, background_tasks: BackgroundTasks):
    """Queue an immediate read-only collection sweep for *domain*.

    Mirrors the webhook `/sweeps/{domain}` route, but is gated by the dashboard
    session (see dashboard_auth) instead of the X-Infra-Token header, so the
    browser can trigger it without handling webhook secrets. Collection is
    read-only against infrastructure — this never mutates managed systems.

    M-5: plain `def`, not `async def` — the body performs zero awaits and one
    SYNC, blocking Redis call (``dedup.try_acquire``, bounded at up to 5s by
    ``dedup.get_redis``'s socket timeout). An `async def` handler runs
    directly on the event loop, so that blocking call would stall every
    other in-flight request in the process for up to 5s whenever Redis is
    partitioned. A plain `def` handler runs in FastAPI's threadpool instead,
    so the wait only ties up one worker thread.
    """
    from infra_brain.dedup import try_acquire
    from infra_brain.webhooks import KNOWN_DOMAINS, _dispatch_bg

    if domain not in KNOWN_DOMAINS:
        raise HTTPException(status_code=404, detail=f"Unknown domain: {domain}")
    token = try_acquire(domain, "all")
    if not token:
        raise HTTPException(status_code=409, detail=f"{domain} collection already in progress")
    background_tasks.add_task(_dispatch_bg, domain, "manual", "all", token)
    return {"accepted": True, "domain": domain}


# ─────────────────────────────────────────────────────────────────────────────
# Fleet Assets — dedicated view (Rapid7 asset inventory)
# ─────────────────────────────────────────────────────────────────────────────
#
# Surfaces the Rapid7 asset estate (r7_assets) decorated with a small slice of
# its hardware/OS config rollup (r7_asset_config). Rapid7 uses the vulnerability
# bands critical / severe / moderate — there is NO "high" band. The page reads
# this rather than the generic Resource contract because it carries the risk
# scores and per-asset vuln-band counts the Fleet Assets view is built around.


def _risk_band(score: float | None) -> str:
    """Map a Rapid7 risk score to a coarse band for the fleet view."""
    s = score or 0.0
    if s >= 25000:
        return "critical"
    if s >= 10000:
        return "high"
    if s >= 1000:
        return "medium"
    return "low"


@fleet_router.get("/fleet", response_model=FleetAssetsOut)
def list_fleet(
    q: str | None = None,
    os: str | None = None,
    risk_band: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Paged Rapid7 asset inventory for the Fleet Assets view (2,435 assets).

    Read-only (SELECT only). Returns a page of assets plus a fleet-wide
    ``summary`` (totals, counts by OS product, counts by risk band). The list
    is server-side paged/filtered; the summary aggregates the whole estate
    (after the ``q``/``os`` filters, ignoring paging) via SQL ``GROUP BY`` — no
    full-table load into Python.

    Filters:
      * ``q`` — case-insensitive substring over hostname / ip / os.
      * ``os`` — case-insensitive substring over os_product.
      * ``risk_band`` — ``low`` | ``medium`` | ``high`` | ``critical``.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        base = s.query(R7Asset)
        if q:
            like = f"%{q}%"
            base = base.filter(
                or_(
                    R7Asset.hostname.ilike(like),
                    R7Asset.ip.ilike(like),
                    R7Asset.os.ilike(like),
                )
            )
        if os:
            base = base.filter(R7Asset.os_product.ilike(f"%{os}%"))

        # Risk band maps to a raw_risk/risk_score range; filter in SQL.
        band_bounds = {
            "low": (0.0, 1000.0),
            "medium": (1000.0, 10000.0),
            "high": (10000.0, 25000.0),
            "critical": (25000.0, None),
        }
        if risk_band in band_bounds:
            lo, hi = band_bounds[risk_band]
            score = func.coalesce(R7Asset.risk_score, 0.0)
            base = base.filter(score >= lo)
            if hi is not None:
                base = base.filter(score < hi)

        rows = (
            base.order_by(R7Asset.risk_score.desc().nullslast(), R7Asset.hostname)
            .offset(offset)
            .limit(limit)
            .all()
        )

        # Config-count rollup for just the assets on this page (one grouped query).
        page_ids = [r.id for r in rows]
        cfg_counts: dict[Any, int] = {}
        if page_ids:
            for asset_id, n in (
                s.query(R7AssetConfig.asset_id, func.count(R7AssetConfig.id))
                .filter(R7AssetConfig.asset_id.in_(page_ids))
                .group_by(R7AssetConfig.asset_id)
                .all()
            ):
                cfg_counts[asset_id] = n

        items = [
            FleetAssetOut(
                id=str(r.id),
                r7_asset_id=r.r7_asset_id,
                hostname=r.hostname or "—",
                ip=r.ip or "",
                os=r.os or "",
                os_product=r.os_product or "",
                os_version=r.os_version or "",
                os_vendor=r.os_vendor or "",
                asset_type=r.asset_type or "",
                risk_score=float(r.risk_score or 0.0),
                risk_band=_risk_band(r.risk_score),
                vuln_critical=r.vuln_critical,
                vuln_severe=r.vuln_severe,
                vuln_moderate=r.vuln_moderate,
                vuln_total=r.vuln_total,
                vuln_exploits=r.vuln_exploits,
                assessed=r.assessed_for_vulnerabilities,
                config_count=cfg_counts.get(r.id, 0),
            )
            for r in rows
        ]

        # Summary: aggregate the whole (filtered) estate, ignoring paging.
        summ = s.query(R7Asset)
        if q:
            like = f"%{q}%"
            summ = summ.filter(
                or_(
                    R7Asset.hostname.ilike(like),
                    R7Asset.ip.ilike(like),
                    R7Asset.os.ilike(like),
                )
            )
        if os:
            summ = summ.filter(R7Asset.os_product.ilike(f"%{os}%"))
        summ_sq = summ.subquery()

        total_assets = s.query(func.count()).select_from(summ_sq).scalar() or 0
        assessed_assets = (
            s.query(func.count())
            .select_from(summ_sq)
            .filter(summ_sq.c.assessed_for_vulnerabilities.is_(True))
            .scalar()
            or 0
        )
        total_critical = int(
            s.query(func.coalesce(func.sum(summ_sq.c.vuln_critical), 0))
            .select_from(summ_sq)
            .scalar()
            or 0
        )
        total_severe = int(
            s.query(func.coalesce(func.sum(summ_sq.c.vuln_severe), 0)).select_from(summ_sq).scalar()
            or 0
        )
        by_os = [
            FleetOsCountOut(os_product=name or "(unknown)", count=cnt)
            for name, cnt in (
                s.query(summ_sq.c.os_product, func.count())
                .select_from(summ_sq)
                .group_by(summ_sq.c.os_product)
                .order_by(func.count().desc())
                .limit(15)
                .all()
            )
        ]
        # Risk-band counts: bucket in SQL via a CASE-equivalent expressed with
        # comparisons, then count. Done with a small set of scalar counts to keep
        # the ORM portable across sqlite (tests) and postgres (prod).
        score = func.coalesce(summ_sq.c.risk_score, 0.0)
        band_defs = [
            ("critical", 25000.0, None),
            ("high", 10000.0, 25000.0),
            ("medium", 1000.0, 10000.0),
            ("low", 0.0, 1000.0),
        ]
        by_risk_band: list[FleetRiskBandOut] = []
        for band, lo, hi in band_defs:
            bq = s.query(func.count()).select_from(summ_sq).filter(score >= lo)
            if hi is not None:
                bq = bq.filter(score < hi)
            by_risk_band.append(FleetRiskBandOut(band=band, count=int(bq.scalar() or 0)))

        summary = FleetSummaryOut(
            total_assets=int(total_assets),
            assessed_assets=int(assessed_assets),
            total_critical=total_critical,
            total_severe=total_severe,
            by_os=by_os,
            by_risk_band=by_risk_band,
        )
        return FleetAssetsOut(
            items=items, total=int(total_assets), limit=limit, offset=offset, summary=summary
        )


@fleet_router.get("/fleet/{asset_id}/detail", response_model=R7AssetDetailOut)
def get_fleet_asset_detail(asset_id: str):
    """Per-asset system/hardware config, local users, and IP/MAC addresses
    for the Fleet Assets detail drawer (UI Batch 5 / GitLab #61).

    Read-only (SELECT only). 404 when the asset id does not resolve to an
    R7Asset row. Children default to empty lists when Rapid7 has not
    collected that sub-table for the asset yet — never a 500.
    """
    try:
        aid = uuid.UUID(asset_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Asset not found")
    with get_session() as s:
        asset = s.get(R7Asset, aid)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        configs = s.query(R7AssetConfig).filter(R7AssetConfig.asset_id == aid).order_by(
            R7AssetConfig.name
        ).all()
        users = s.query(R7AssetUser).filter(R7AssetUser.asset_id == aid).order_by(
            R7AssetUser.username
        ).all()
        addresses = s.query(R7AssetAddress).filter(R7AssetAddress.asset_id == aid).order_by(
            R7AssetAddress.ip
        ).all()
        return R7AssetDetailOut(
            id=str(asset.id),
            hostname=asset.hostname or "",
            configs=[R7AssetConfigOut(name=c.name, value=c.value or "") for c in configs],
            users=[
                R7AssetUserOut(username=u.username, full_name=u.full_name or "") for u in users
            ],
            addresses=[
                R7AssetAddressOut(ip=a.ip, mac=a.mac or "") for a in addresses
            ],
        )


@fleet_router.get("/sites", response_model=R7SitesPageOut)
def list_sites(limit: int = 100, offset: int = 0):
    """Paged Rapid7 site browser (UI Batch 5 / GitLab #61).

    Read-only (SELECT only). Empty items/total=0 when no sites have been
    collected yet — never a 500.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(R7Site).order_by(R7Site.name)
        items, total = paginate(q, limit=limit, offset=offset)
        return R7SitesPageOut(
            items=[
                R7SiteOut(
                    r7_site_id=r.r7_site_id,
                    name=r.name,
                    asset_count=r.asset_count or 0,
                    risk_score=float(r.risk_score or 0.0),
                    importance=r.importance or "",
                    site_type=r.site_type or "",
                    last_scan_time=r.last_scan_time,
                )
                for r in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@fleet_router.get("/tags", response_model=R7TagsPageOut)
def list_tags(limit: int = 100, offset: int = 0):
    """Paged Rapid7 tag browser (UI Batch 5 / GitLab #61).

    Read-only (SELECT only). Empty items/total=0 when no tags have been
    collected yet — never a 500.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(R7Tag).order_by(R7Tag.name)
        items, total = paginate(q, limit=limit, offset=offset)
        return R7TagsPageOut(
            items=[
                R7TagOut(
                    r7_tag_id=r.r7_tag_id,
                    name=r.name,
                    tag_type=r.tag_type or "",
                    color=r.color or "",
                    source=r.source or "",
                )
                for r in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Software Inventory — dedicated view (Rapid7 installed software)
# ─────────────────────────────────────────────────────────────────────────────
#
# r7_software holds ~335k rows (one per installed product per asset), so this
# view NEVER returns the whole table. It offers two shapes:
#   * aggregated (default)  — distinct product × version with a host count,
#     server-side GROUP BY, paged + searchable, ordered by host count desc.
#   * detail (?view=detail) — drill-down rows (host × product × version), paged,
#     filterable by product/vendor/host and free-text search.
#
# NOTE: /software/vendors MUST be registered before /software to prevent
# FastAPI from treating "vendors" as a path parameter value for a hypothetical
# /software/{param} route. Both are exact GET paths — register the more-specific
# static sub-path first.


@fleet_router.get("/software/vendors", response_model=list[str])
def list_software_vendors() -> list[str]:
    """Return a sorted list of distinct non-null vendor values from r7_software.

    Used by the dashboard frontend to populate the vendor filter chip bar.
    Returns an empty list if the table does not yet exist (first-run safety).
    """
    try:
        with get_session() as s:
            rows = (
                s.query(R7Software.vendor)
                .filter(R7Software.vendor.isnot(None))
                .filter(R7Software.vendor != "")
                .distinct()
                .order_by(R7Software.vendor)
                .all()
            )
            return [r.vendor for r in rows if r.vendor]
    except (OperationalError, ProgrammingError):
        return []


@fleet_router.get("/software", response_model=SoftwareInventoryOut)
def list_software(
    view: str = "aggregated",
    q: str | None = None,
    product: str | None = None,
    vendor: str | None = None,
    r7_asset_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Server-side paged Rapid7 software inventory (~335k rows — never bulk-loaded).

    Read-only (SELECT only). Two modes:
      * ``view=aggregated`` (default) — distinct (product, version, vendor) with a
        host count, via SQL ``GROUP BY``; ``total`` is the distinct-group count.
        Ordered by host count desc.
      * ``view=detail`` — host × product rows; ``total`` is the matching row count.
        Filter by ``product``/``vendor``/``r7_asset_id`` and free-text ``q``.

    ``q`` is a case-insensitive substring over product / vendor. The ``summary``
    block (total records, unique products, hosts covered) reflects the filtered
    set, computed with aggregate queries — no full-table materialisation.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with get_session() as s:

        def _apply_filters(query):
            if q:
                like = f"%{q}%"
                query = query.filter(
                    or_(R7Software.product.ilike(like), R7Software.vendor.ilike(like))
                )
            if product:
                query = query.filter(R7Software.product.ilike(f"%{product}%"))
            if vendor:
                query = query.filter(R7Software.vendor.ilike(f"%{vendor}%"))
            if r7_asset_id is not None:
                query = query.filter(R7Software.r7_asset_id == r7_asset_id)
            return query

        # --- summary (filtered, aggregate-only) -------------------------------
        filtered = _apply_filters(s.query(R7Software.id))
        total_records = filtered.with_entities(func.count(R7Software.id)).scalar() or 0
        unique_products = (
            _apply_filters(s.query(R7Software.product))
            .with_entities(func.count(func.distinct(R7Software.product)))
            .scalar()
            or 0
        )
        hosts_covered = (
            _apply_filters(s.query(R7Software.r7_asset_id))
            .with_entities(func.count(func.distinct(R7Software.r7_asset_id)))
            .scalar()
            or 0
        )
        summary = SoftwareSummaryOut(
            total_records=int(total_records),
            unique_products=int(unique_products),
            hosts_covered=int(hosts_covered),
        )

        if view == "detail":
            base = _apply_filters(
                s.query(R7Software, R7Asset.hostname).outerjoin(
                    R7Asset, R7Asset.id == R7Software.asset_id
                )
            )
            total = int(total_records)
            rows = (
                base.order_by(R7Software.product, R7Software.version)
                .offset(offset)
                .limit(limit)
                .all()
            )
            detail = [
                SoftwareDetailRowOut(
                    id=str(sw.id),
                    r7_asset_id=sw.r7_asset_id,
                    hostname=hostname or "—",
                    product=sw.product,
                    version=sw.version or "",
                    vendor=sw.vendor or "",
                    software_type=sw.software_type or "",
                )
                for sw, hostname in rows
            ]
            return SoftwareInventoryOut(
                view="detail",
                items=detail,
                total=total,
                limit=limit,
                offset=offset,
                summary=summary,
            )

        # --- aggregated (default) ---------------------------------------------
        grp = _apply_filters(
            s.query(
                R7Software.product,
                R7Software.version,
                func.max(R7Software.vendor).label("vendor"),
                func.count(func.distinct(R7Software.r7_asset_id)).label("host_count"),
            )
        ).group_by(R7Software.product, R7Software.version)

        # Total distinct groups (for paging) — count rows of the grouped subquery.
        total = s.query(func.count()).select_from(grp.order_by(None).subquery()).scalar() or 0
        rows = (
            grp.order_by(func.count(func.distinct(R7Software.r7_asset_id)).desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        aggregated = [
            SoftwareAggRowOut(
                product=row.product,
                version=row.version or "",
                vendor=row.vendor or "",
                host_count=int(row.host_count),
            )
            for row in rows
        ]
        return SoftwareInventoryOut(
            view="aggregated",
            items=aggregated,
            total=int(total),
            limit=limit,
            offset=offset,
            summary=summary,
        )
