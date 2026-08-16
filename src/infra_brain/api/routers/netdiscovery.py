"""infra_brain.api.routers.netdiscovery — Network Discovery / Shadow-IT API
router (UI Batch 8 / GitLab #64).

Gives infra-brain's active netdiscovery agent (nmap-based passive/tier1/
tier2 scanning, read-only by convention per docs/READONLY-MODEL.md and
gated by _gate_nmap_targets) its first dashboard surface. Previously
NetDiscoveryHost/NetDiscoveryService had zero UI representation despite
being a distinctive shadow-IT detection capability.

Import rules:
  * MAY import from infra_brain.api.schemas, infra_brain.api._envelope,
    infra_brain.db.models, infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import case

from infra_brain.api._envelope import paginate
from infra_brain.api.schemas import (
    NetDiscoveryHostOut,
    NetDiscoveryHostsPageOut,
    NetDiscoveryServiceOut,
    NetDiscoverySummaryOut,
)
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import NetDiscoveryHost, NetDiscoveryService
from infra_brain.db.session import get_session

# Same prefix/gate as the shared dashboard router — a dedicated sub-path
# (/netdiscovery/hosts) rather than a top-level prefix, since this is a
# single-purpose browse view alongside the rest of /api/dashboard/*.
netdiscovery_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)

# threat_level is a free-text column (``none`` | ``low`` | ``med`` | ``high`` — see
# NetDiscoveryAgent._THREAT_RANKS in agents/netdiscovery.py, the canonical source of
# these strings). A plain ``.desc()`` on that column sorts lexicographically, which for
# this exact string set produces "none > med > low > high" — the reverse of severity
# order — pushing the worst hosts onto the LAST page. Rank explicitly instead.
_THREAT_LEVEL_RANK = case(
    (NetDiscoveryHost.threat_level == "high", 3),
    (NetDiscoveryHost.threat_level == "med", 2),
    (NetDiscoveryHost.threat_level == "low", 1),
    else_=0,
)


@netdiscovery_router.get("/netdiscovery/hosts", response_model=NetDiscoveryHostsPageOut)
def list_netdiscovery_hosts(
    is_shadow_it: Optional[bool] = None,
    is_known: Optional[bool] = None,
    threat_level: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    """Paged network-discovery host list for the shadow-IT view, each host
    decorated with its discovered services.

    Read-only (SELECT only). ``summary`` aggregates the full filtered-out
    universe (before paging) so the header stats reflect the whole estate,
    not just the current page — matching the ``fleet``/``iac_overview``
    convention. Empty items/total=0/summary all-zero when nothing has been
    discovered yet — never a 500.

    Filters:
      * ``is_shadow_it`` — exact match.
      * ``is_known`` — exact match.
      * ``threat_level`` — exact match (``none`` | ``low`` | ``med`` | ``high``).
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        base = s.query(NetDiscoveryHost)
        if is_shadow_it is not None:
            base = base.filter(NetDiscoveryHost.is_shadow_it.is_(is_shadow_it))
        if is_known is not None:
            base = base.filter(NetDiscoveryHost.is_known.is_(is_known))
        if threat_level:
            base = base.filter(NetDiscoveryHost.threat_level == threat_level)

        # Summary aggregates over the same filtered universe, before paging.
        filtered_all = base.all()
        shadow_it_count = sum(1 for h in filtered_all if h.is_shadow_it)
        known_count = sum(1 for h in filtered_all if h.is_known)
        by_threat: dict[str, int] = {}
        for h in filtered_all:
            lvl = h.threat_level or "none"
            by_threat[lvl] = by_threat.get(lvl, 0) + 1

        q = base.order_by(_THREAT_LEVEL_RANK.desc(), NetDiscoveryHost.last_seen.desc())
        items, total = paginate(q, limit=limit, offset=offset)

        host_ids = [h.id for h in items]
        services_by_host: dict = {}
        if host_ids:
            for svc in (
                s.query(NetDiscoveryService)
                .filter(NetDiscoveryService.host_id.in_(host_ids))
                .order_by(NetDiscoveryService.port)
                .all()
            ):
                services_by_host.setdefault(svc.host_id, []).append(svc)

        return NetDiscoveryHostsPageOut(
            items=[
                NetDiscoveryHostOut(
                    id=str(h.id),
                    ip=h.ip,
                    mac=h.mac or "",
                    mac_vendor=h.mac_vendor or "",
                    hostname=h.hostname or "",
                    first_seen=h.first_seen,
                    last_seen=h.last_seen,
                    responded=h.responded,
                    discovery_tier=h.discovery_tier,
                    is_fragile=h.is_fragile,
                    is_known=h.is_known,
                    is_shadow_it=h.is_shadow_it,
                    threat_level=h.threat_level,
                    zone=h.zone,
                    resource_id=str(h.resource_id) if h.resource_id else None,
                    services=[
                        NetDiscoveryServiceOut(
                            port=svc.port,
                            proto=svc.proto,
                            service=svc.service or "",
                            banner=svc.banner or "",
                            fingerprint=svc.fingerprint or "",
                            is_dangerous=svc.is_dangerous,
                            is_suspicious=svc.is_suspicious,
                            last_seen=svc.last_seen,
                        )
                        for svc in services_by_host.get(h.id, [])
                    ],
                )
                for h in items
            ],
            total=total,
            limit=limit,
            offset=offset,
            summary=NetDiscoverySummaryOut(
                total_hosts=len(filtered_all),
                shadow_it_count=shadow_it_count,
                known_count=known_count,
                by_threat_level=by_threat,
            ),
        )
