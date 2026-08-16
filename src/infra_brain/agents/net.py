import logging
from datetime import timedelta

from infra_brain.db.models import NetDevice, Resource
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectOutcome, CollectorSkipped, ETLConnector
from infra_brain.tools.snmp import net_device_facts_tool
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)

# data fields mapped to a typed column → excluded from the details overflow.
_MAPPED_FIELDS = {
    "host",
    "sys_name",
    "sys_descr",
    "sys_object_id",
    "sys_uptime",
    "sys_contact",
    "sys_location",
}


class NetAgent(ETLConnector):
    """Read-only network-device collector via SNMP.

    Walks the SNMPv2 system group (sysName/sysDescr/sysObjectID/sysUpTime/…) for
    each configured target using a read-only community string. Returns one
    resource dict per reachable device.

    Configuration (see config.Settings):
      - ``snmp_targets``   — comma-separated host/IP list (empty → collector idle)
      - ``snmp_community`` — read-only community string
      - ``snmp_port``      — UDP port (default 161)

    When no targets/community are configured, or pysnmp (the ``net`` extra) is not
    installed, collect() returns [] cleanly rather than fabricating data.
    """

    spec = AgentSpec(
        domain="net",
        tier=Tier.COLLECTOR,
        schedule="10 */6 * * *",
        max_staleness=timedelta(hours=8),
        # POC (2026-07-15): disabled — netdiscovery covers network-device
        # inventory; net adds only the shallow SNMPv2 system group. Re-enable by
        # flipping dispatchable back to True (and configuring snmp_targets/community).
        #
        # P5 REVIVAL OBLIGATION: this collector feeds ``net_devices``, which is
        # host_reconcile's "netdevice" leg (NOT its "net" leg — that one is
        # netdiscovery). ``dispatchable=False`` removes the class from
        # AGENT_REGISTRY entirely, so the leg has no rows and no spec the
        # resolver-coverage guard can even see. Re-enabling it means adding a
        # NodeSpec(..., is_host_identity=True) here — keyed on ``sys_name``, the
        # SNMP system name, which IS stable, never on the IP — AND the matching
        # entry in graph_phase3.HOST_NODE_TYPES. Without both, every
        # netdevice↔anything identity pair is invisible to the resolver: not
        # queued for a human, silently absent. No speculative NodeSpec is
        # declared now — zero rows, ever, and this collector is not even
        # registered.
        dispatchable=False,
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        targets = [t.strip() for t in (self.settings.snmp_targets or "").split(",") if t.strip()]
        community = self.settings.snmp_community

        if not targets or not community:
            raise CollectorSkipped("no SNMP targets/community configured")

        _cb = {"callbacks": self.callbacks}
        items: list[dict] = []
        errors: list[str] = []
        for host in targets:
            try:
                result = net_device_facts_tool.invoke(
                    {
                        "host": host,
                        "community": community,
                        "port": self.settings.snmp_port,
                        "timeout": self.settings.snmp_timeout_seconds,
                        "retries": self.settings.snmp_retries,
                    },
                    config=_cb,
                )
            except Exception as exc:
                msg = f"SNMP tool raised for {host}: {exc}"
                logger.warning("NetAgent: %s", msg)
                errors.append(msg)
                continue
            if result is None:
                msg = f"{host} unreachable or SNMP disabled"
                logger.warning("NetAgent: %s — skipping", msg)
                errors.append(msg)
                continue
            items.append(result)

        logger.info("NetAgent: collected %d/%d device(s)", len(items), len(targets))
        # Cache for the relational detail-write phase.
        self._last_items = items
        return CollectOutcome(items=items, errors=errors)

    # ------------------------------------------------------------------
    # Relational detail-write phase (MR5)
    # ------------------------------------------------------------------

    def _detail_writers(self, scope, result):
        # Surface a detail-write failure on the CollectionRun (no silent data
        # loss) via ETLConnector.run()'s _write_details.
        return [self._write_net_details]

    def _write_net_details(self) -> None:
        """Populate net_devices from the items collect() emitted.

        Idle-safety FIRST: with no SNMP targets/community the collector never ran,
        so there is nothing to write and we must not touch the DB.
        """
        targets = [t.strip() for t in (self.settings.snmp_targets or "").split(",") if t.strip()]
        if not targets or not self.settings.snmp_community:
            logger.info("NetAgent: no SNMP targets/community — relational write skipped (idle)")
            return

        items = getattr(self, "_last_items", None) or []
        if not items:
            return

        with get_session() as session:
            for item in items:
                try:
                    with session.begin_nested():
                        self._upsert_net_item(session, item)
                except Exception as exc:
                    logger.warning(
                        "NetAgent: skipping bad net_device item %r: %s",
                        item.get("name"),
                        exc,
                    )
            session.commit()

    def _upsert_net_item(self, session, item: dict) -> None:
        data = item.get("data", {}) or {}
        name = item.get("name", "")
        ip = data.get("host")
        if not ip:
            raise ValueError(f"no host (snmp target) on net_device {name!r} — cannot key row")

        res = session.query(Resource).filter_by(domain="net", type="net_device", name=name).first()
        details = {k: v for k, v in data.items() if k not in _MAPPED_FIELDS}
        row = {
            "resource_id": res.id if res else None,
            "ip": ip,
            "name": name,
            "sysname": data.get("sys_name") or None,
            "descr": data.get("sys_descr") or None,
            "object_id": data.get("sys_object_id") or None,
            "uptime": data.get("sys_uptime") or None,
            "contact": data.get("sys_contact") or None,
            "location": data.get("sys_location") or None,
            "details": details or None,
        }
        self._upsert_detail(session, NetDevice, row, ["ip"])
