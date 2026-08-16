"""LoadBalancerAgent — read-only load balancer / reverse proxy / CDN collector.

GitLab issue #100: "Currently a total blank in the topology graph — nginx/
haproxy/F5/Cloudflare are invisible even though ``resource_relationships``
already models multi-hop infra topology." Collects LB backend-pool
membership, health-check config, and TLS-termination config.

Four vendors, each independently optional (empty config -> that vendor is
skipped cleanly, never fabricated): F5 BIG-IP (iControl REST), nginx Plus
(API), HAProxy (stats page CSV), Cloudflare (API). All four are naturally
GET-only, per ``infra_brain.tools.loadbalancer`` — fits the structural
read-only model cleanly, exactly as the issue notes.

Data shape (the issue's own design sketch): ``LbInstance``, ``LbPool``,
``LbPoolMember``, ``LbVirtualServer`` (``db/models/loadbalancer.py``).

EPITAPH — the three edges (P5, rev11-T5-B). The issue asked for ``ROUTES_TO``
(lb_virtual_server -> lb_pool), ``MEMBER_OF_POOL`` (host -> lb_pool) and
``TERMINATES_TLS_FOR`` (lb_instance -> certificate), and this agent wrote all
three into ``resource_relationships``. P5 drops that store, so all three are
deleted here. This is a RETIRED-IN-EFFECT domain — ``lb_enabled`` is False, so
``collect()`` never runs and the live table holds no rows from it — but the
verdict would be the same either way: all three are GENUINE two-entity
relationships, none is containment, and each would be re-established by a
COLLECTOR DECLARATION over the four detail tables, never by a re-derivation.

WHERE EVERY FACT LIVES NOW (all four tables are still written, unchanged):
  * ROUTES_TO         -> ``lb_virtual_servers.pool_id`` (a real FK)
  * MEMBER_OF_POOL    -> ``lb_pool_members.pool_id`` + ``.resource_id`` — the
    resolved host id is STILL stamped on the member row; only the edge that
    restated it, and the heuristic 0.8 confidence that rode on the edge, are
    gone.
  * TERMINATES_TLS_FOR-> ``lb_virtual_servers.tls_enabled`` plus the vendor's
    ``tls_certificate_thumbprint`` in ``details``; the ``security/certificate``
    row it pointed at is pki's and is untouched.
THE DECLARATION PATH if this domain is ever switched on: all three joins are
functional from the many side (a VS has one pool, a member one pool, a VS one
cert) and both endpoints of each are rows this collector owns, so each is an
ordinary ``EdgeSpec`` — the ownership question that blocked ANSIBLE_MANAGES
does not arise here.
"""

import logging
from datetime import timedelta
from functools import partial
from typing import Any

from infra_brain.db.models import (
    LbInstance,
    LbPool,
    LbPoolMember,
    LbVirtualServer,
    Resource,
)
from infra_brain.db.session import get_session
from infra_brain.etl.base import (
    CollectionResult,
    CollectOutcome,
    CollectorSkipped,
    ETLConnector,
    ReconcileScope,
)
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.loadbalancer import (
    cloudflare_load_balancers_tool,
    cloudflare_pools_tool,
    cloudflare_zones_tool,
    f5_ltm_pools_tool,
    f5_ltm_virtual_servers_tool,
    haproxy_stats_tool,
    nginx_plus_upstreams_tool,
)

logger = logging.getLogger(__name__)

# data fields mapped to a typed column -> excluded from the details overflow.
_POOL_MAPPED_FIELDS = {"vendor", "instance", "monitor_type", "members"}
_VS_MAPPED_FIELDS = {"vendor", "instance", "pool", "address", "port", "protocol", "tls_enabled"}

# Host-producing domains a pool member's address might resolve against —
# best-effort cross-domain name/IP match (heuristic, so confidence < 1.0).
_HOST_DOMAINS = ("linux", "windows", "vsphere", "netdiscovery", "vuln")


class LoadBalancerAgent(ETLConnector):
    """Read-only collector for LB/reverse-proxy/CDN configuration.

    Configuration (see config.Settings): ``lb_enabled`` (global switch) plus
    per-vendor settings (``f5_*``, ``nginx_plus_url``, ``haproxy_stats_*``,
    ``cloudflare_*``). Any vendor left unconfigured is skipped — collect()
    never fabricates data for an absent source.
    """

    spec = AgentSpec(
        domain="loadbalancer",
        tier=Tier.COLLECTOR,
        schedule="45 2 * * *",
        max_staleness=timedelta(hours=26),
        # dispatchable=True (registered/scheduled like every other collector) —
        # unlike cloud.py/net.py (fully removed from _AGENT_SPECS pending
        # post-POC/re-enable work), this agent is safe to leave wired in with
        # no LB admin credentials configured: collect() raises
        # CollectorSkipped (lb_enabled=False by default in config.py), the
        # same idle-safe contract net.py/cloud.py use internally, so a
        # scheduled run is a clean no-op until an operator sets lb_enabled
        # plus at least one vendor's config.
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        if not getattr(self.settings, "lb_enabled", False):
            raise CollectorSkipped("lb_enabled=False")

        _cb = {"callbacks": self.callbacks}
        items: list[dict[str, Any]] = []
        errors: list[str] = []

        if self.settings.f5_host:
            try:
                items.append(
                    {
                        "name": self.settings.f5_host,
                        "type": "lb_instance",
                        "data": {"vendor": "f5", "instance": self.settings.f5_host},
                    }
                )
                items.extend(
                    f5_ltm_pools_tool.invoke(
                        {"host": self.settings.f5_host, "ssl_verify": self.settings.f5_ssl_verify},
                        config=_cb,
                    )
                )
                items.extend(
                    f5_ltm_virtual_servers_tool.invoke(
                        {"host": self.settings.f5_host, "ssl_verify": self.settings.f5_ssl_verify},
                        config=_cb,
                    )
                )
            except Exception as exc:
                msg = f"F5 collection failed for {self.settings.f5_host}: {exc}"
                logger.warning("LoadBalancerAgent: %s", msg)
                errors.append(msg)

        if self.settings.nginx_plus_url:
            try:
                items.append(
                    {
                        "name": self.settings.nginx_plus_url,
                        "type": "lb_instance",
                        "data": {"vendor": "nginx", "instance": self.settings.nginx_plus_url},
                    }
                )
                items.extend(
                    nginx_plus_upstreams_tool.invoke(
                        {"api_url": self.settings.nginx_plus_url}, config=_cb
                    )
                )
            except Exception as exc:
                msg = f"nginx Plus collection failed for {self.settings.nginx_plus_url}: {exc}"
                logger.warning("LoadBalancerAgent: %s", msg)
                errors.append(msg)

        if self.settings.haproxy_stats_url:
            try:
                items.append(
                    {
                        "name": self.settings.haproxy_stats_url,
                        "type": "lb_instance",
                        "data": {"vendor": "haproxy", "instance": self.settings.haproxy_stats_url},
                    }
                )
                items.extend(
                    haproxy_stats_tool.invoke(
                        {"stats_url": self.settings.haproxy_stats_url},
                        config=_cb,
                    )
                )
            except Exception as exc:
                msg = f"HAProxy collection failed for {self.settings.haproxy_stats_url}: {exc}"
                logger.warning("LoadBalancerAgent: %s", msg)
                errors.append(msg)

        if self.settings.cloudflare_api_token:
            try:
                zone_ids = [
                    z.strip()
                    for z in (self.settings.cloudflare_zone_ids or "").split(",")
                    if z.strip()
                ]
                # zone_id -> the zone's domain name, i.e. the same key
                # cloudflare_zones_tool used for its "lb_instance" item's
                # ``name`` (and _upsert_instance used to create the real,
                # Resource-backed LbInstance row below). cloudflare_load_balancers_tool
                # only knows the zone_id, not the domain name, so its
                # ``data["instance"]`` must be normalized to this mapping
                # before being handed to _upsert_virtual_server — otherwise
                # the (vendor, name) lookup in _get_or_create_instance()
                # never matches the real per-zone instance and fabricates a
                # disconnected orphan (resource_id=None) every cycle,
                # silently defeating the TERMINATES_TLS_FOR edge below.
                zone_names_by_id: dict[str, str] = {}
                if not zone_ids:
                    zones = cloudflare_zones_tool.invoke({}, config=_cb)
                    items.extend(zones)
                    zone_names_by_id = {
                        z["data"]["zone_id"]: z["name"]
                        for z in zones
                        if z.get("data", {}).get("zone_id") and z.get("name")
                    }
                    zone_ids = list(zone_names_by_id.keys())

                # Cloudflare pools are account-scoped, not per-zone, so they
                # have no natural zone-name identity — but the tool
                # nonetheless keys them under the literal "cloudflare"
                # (mismatched with the zone-name-keyed rows above). Emit a
                # matching "lb_instance" seed item for that same literal key
                # so the lookup consistently resolves to one deliberate
                # account-level LbInstance instead of a fresh orphan.
                pool_items = cloudflare_pools_tool.invoke({}, config=_cb)
                if pool_items:
                    items.append(
                        {
                            "name": "cloudflare",
                            "type": "lb_instance",
                            "data": {"vendor": "cloudflare", "instance": "cloudflare"},
                        }
                    )
                items.extend(pool_items)

                for zone_id in zone_ids:
                    lb_items = cloudflare_load_balancers_tool.invoke(
                        {"zone_id": zone_id}, config=_cb
                    )
                    zone_name = zone_names_by_id.get(zone_id)
                    if zone_name:
                        for lb_item in lb_items:
                            lb_item.setdefault("data", {})["instance"] = zone_name
                    items.extend(lb_items)
            except Exception as exc:
                msg = f"Cloudflare collection failed: {exc}"
                logger.warning("LoadBalancerAgent: %s", msg)
                errors.append(msg)

        logger.info("LoadBalancerAgent: collected %d item(s)", len(items))
        self._last_items = items
        return CollectOutcome(items=items, errors=errors)

    # ------------------------------------------------------------------
    # Relational detail-write phase
    # ------------------------------------------------------------------

    def _detail_writers(self, scope, result):
        return [partial(self._write_lb_details, result)]

    def _write_lb_details(self, result: "CollectionResult | None" = None) -> None:
        """Populate lb_instances/lb_pools/lb_pool_members/lb_virtual_servers.

        P5 (rev11-T5-B): this used to also emit ROUTES_TO / MEMBER_OF_POOL /
        TERMINATES_TLS_FOR into ``resource_relationships`` after the per-item
        SAVEPOINT loop. The four detail-table writes — and the per-item
        SAVEPOINT + partial-error reporting around them — are untouched; only
        the ``edges`` accumulator and its batch write are gone. See the module
        docstring's EPITAPH for where each relationship's fact now lives.

        Idle-safety first: with lb_enabled=False, collect() never ran, so
        there is nothing to write and we must not touch the DB.
        """
        if not getattr(self.settings, "lb_enabled", False):
            logger.info("LoadBalancerAgent: lb_enabled=False — relational write skipped (idle)")
            return

        items = getattr(self, "_last_items", None) or []
        if not items:
            return

        # M-2 (F-007): the per-item SAVEPOINT below logs+skips a bad LB item
        # so its siblings still commit — but that resilience must not be
        # invisible. Feed every skip through the shared
        # ETLConnector._record_partial_errors helper (same as vuln.py's
        # detail-write phase) so a run that dropped every item downgrades
        # from "completed" to "partial" instead of reporting clean success.
        skip_scope = ReconcileScope(label="lb item")
        with get_session() as session:
            instances: dict[tuple[str, str], LbInstance] = {}
            pools: dict[tuple[str, str], LbPool] = {}

            for item in items:
                data = item.get("data", {}) or {}
                vendor = data.get("vendor", "")
                item_type = item.get("type", "")
                name = item.get("name", "")
                if not vendor or not name:
                    continue
                key = f"{item_type}:{name}"
                try:
                    with session.begin_nested():
                        if item_type == "lb_instance":
                            self._upsert_instance(session, vendor, name, instances)
                        elif item_type == "lb_pool":
                            self._upsert_pool(session, vendor, name, data, instances, pools)
                        elif item_type == "lb_virtual_server":
                            self._upsert_virtual_server(
                                session, vendor, name, data, instances, pools
                            )
                    skip_scope.observed(key)
                except Exception as exc:
                    logger.warning(
                        "LoadBalancerAgent: skipping bad %s item %r: %s", item_type, name, exc
                    )
                    skip_scope.failed(key, exc)
            session.commit()
        self._record_partial_errors(result, skip_scope.errors)

    def _upsert_instance(self, session, vendor: str, name: str, instances: dict) -> None:
        row = session.query(LbInstance).filter_by(vendor=vendor, name=name).first()
        res = (
            session.query(Resource)
            .filter_by(domain="loadbalancer", type="lb_instance", name=name)
            .first()
        )
        if row is None:
            row = LbInstance(
                vendor=vendor, name=name, management_host=name, resource_id=res.id if res else None
            )
            session.add(row)
            session.flush()
        else:
            row.resource_id = res.id if res else row.resource_id
        instances[(vendor, name)] = row

    def _get_or_create_instance(
        self, session, vendor: str, instance_name: str, instances: dict
    ) -> LbInstance:
        key = (vendor, instance_name)
        if key in instances:
            return instances[key]
        row = session.query(LbInstance).filter_by(vendor=vendor, name=instance_name).first()
        if row is None:
            row = LbInstance(vendor=vendor, name=instance_name, management_host=instance_name)
            session.add(row)
            session.flush()
        instances[key] = row
        return row

    def _upsert_pool(
        self, session, vendor: str, name: str, data: dict, instances: dict, pools: dict
    ) -> None:
        instance_name = data.get("instance", "")
        instance = self._get_or_create_instance(session, vendor, instance_name, instances)
        res = (
            session.query(Resource)
            .filter_by(domain="loadbalancer", type="lb_pool", name=name)
            .first()
        )
        pool_row = session.query(LbPool).filter_by(instance_id=instance.id, name=name).first()
        details = {k: v for k, v in data.items() if k not in _POOL_MAPPED_FIELDS}
        if pool_row is None:
            pool_row = LbPool(
                instance_id=instance.id,
                name=name,
                monitor_type=data.get("monitor_type"),
                resource_id=res.id if res else None,
                details=details or None,
            )
            session.add(pool_row)
            session.flush()
        else:
            pool_row.monitor_type = data.get("monitor_type")
            pool_row.resource_id = res.id if res else pool_row.resource_id
            pool_row.details = details or None
        pools[(vendor, instance_name, name)] = pool_row

        # replace member rows for this pool with the current snapshot
        session.query(LbPoolMember).filter_by(pool_id=pool_row.id).delete()
        for member in data.get("members", []) or []:
            address = member.get("address", "")
            port = member.get("port", 0)
            if not address:
                continue
            # The resolved host id STILL lands on the member row — that write is
            # the fact; the MEMBER_OF_POOL edge that used to restate it is gone
            # (P5). Only the heuristic confidence went with the edge.
            host_resource_id, _confidence = self._resolve_host(session, address)
            member_row = LbPoolMember(
                pool_id=pool_row.id,
                address=address,
                port=port,
                state=member.get("state"),
                weight=member.get("weight"),
                resource_id=host_resource_id,
            )
            session.add(member_row)

    def _upsert_virtual_server(
        self, session, vendor: str, name: str, data: dict, instances: dict, pools: dict
    ) -> None:
        instance_name = data.get("instance", "")
        instance = self._get_or_create_instance(session, vendor, instance_name, instances)
        pool_name = data.get("pool")
        pool_row = pools.get((vendor, instance_name, pool_name)) if pool_name else None
        if pool_row is None and pool_name:
            pool_row = (
                session.query(LbPool).filter_by(instance_id=instance.id, name=pool_name).first()
            )

        res = (
            session.query(Resource)
            .filter_by(domain="loadbalancer", type="lb_virtual_server", name=name)
            .first()
        )
        details = {k: v for k, v in data.items() if k not in _VS_MAPPED_FIELDS}
        vs_row = (
            session.query(LbVirtualServer).filter_by(instance_id=instance.id, name=name).first()
        )
        if vs_row is None:
            vs_row = LbVirtualServer(
                instance_id=instance.id,
                pool_id=pool_row.id if pool_row else None,
                name=name,
                address=data.get("address"),
                port=data.get("port"),
                protocol=data.get("protocol"),
                tls_enabled=bool(data.get("tls_enabled", False)),
                resource_id=res.id if res else None,
                details=details or None,
            )
            session.add(vs_row)
            session.flush()
        else:
            vs_row.pool_id = pool_row.id if pool_row else vs_row.pool_id
            vs_row.address = data.get("address")
            vs_row.port = data.get("port")
            vs_row.protocol = data.get("protocol")
            vs_row.tls_enabled = bool(data.get("tls_enabled", False))
            vs_row.resource_id = res.id if res else vs_row.resource_id
            vs_row.details = details or None

        # The ROUTES_TO edge (vs -> pool) and the TERMINATES_TLS_FOR edge
        # (instance -> security/certificate) were emitted here. Both are DELETED
        # (P5) — ``vs_row.pool_id`` above already IS the routing fact, and
        # ``tls_enabled`` + the vendor's thumbprint in ``details`` already IS the
        # termination fact. Neither built a node, so nothing else went with them.

    def _resolve_host(self, session, address: str) -> tuple[Any, float]:
        """Best-effort match of a pool member's address to a known host Resource.

        Scoped to known host-producing domains (heuristic name/IP match, so
        confidence < 1.0 — a rename or IP reassignment can mis-resolve it,
        same caveat as graph.py's CONFIDENCE_DETERMINISTIC_NAME tier).
        Returns (resource_id | None, confidence).
        """
        res = (
            session.query(Resource)
            .filter(Resource.domain.in_(_HOST_DOMAINS), Resource.name == address)
            .first()
        )
        if res:
            return res.id, 0.8
        return None, 0.0
