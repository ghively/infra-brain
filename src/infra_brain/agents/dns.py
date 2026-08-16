"""DnsAgent — collect authoritative DNS zone/record state (GitLab issue #95).

DNS was previously only checked as a side effect during host reconciliation
(hostname resolution failures were logged, never audited as their own
resource). This agent makes DNS a first-class collected domain: for each
configured zone it records SOA/serial metadata and, where the authoritative
server permits it, the full record set (A/AAAA/CNAME/MX/TXT/SRV/NS) via zone
transfer — so DriftDetector can diff DnsRecord snapshots the same way it
diffs any other domain, surfacing stale records pointing at decommissioned
hosts and orphaned CNAMEs.

Read-only model: this is a raw DNS-protocol collector, not HTTP, so it
cannot hold a structurally GET-only client the way ``tools/http_readonly``
collectors do (R2 layer 1). Like pyVmomi/k8s-client/nmap, it is read-only
**by convention** — see ``tools/dns.py``'s module docstring for exactly which
dnspython calls are used (resolver lookups + AXFR reads only, never the DDNS
update API) — and every query is audited via ``_audit_dns_query`` (an
AgentActionLog row per call), mirroring ``NetDiscoveryAgent._audit_subprocess``,
so the DLP/audit callback chain still has a record of DNS activity even
though it doesn't ride LangChain's @tool boundary.

Unconfigured (no ``dns_zones``) or dnspython unavailable both degrade to
CollectorSkipped — a graceful no-op run, not an exception — mirroring how
VulnAgent/VsphereAgent handle a missing dependency (see ``_unconfigured()``
in tools/rapid7.py and ``_PYVMOMI_AVAILABLE`` in tools/vsphere.py).
"""

import logging
import uuid
from datetime import timedelta

from infra_brain.callbacks.dlp import DLPCallbackHandler
from infra_brain.db.models import AgentActionLog, DnsRecord, DnsZone, Resource, _now
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.dns import _DNSPYTHON_AVAILABLE, query_axfr, query_soa

logger = logging.getLogger(__name__)


class DnsAgent(ETLConnector):
    spec = AgentSpec(
        domain="dns",
        tier=Tier.COLLECTOR,
        schedule="55 2 * * *",
        max_staleness=timedelta(hours=26),
    )

    def collect(self, scope: str = "all") -> "list[dict] | CollectOutcome":
        """Query SOA (+ best-effort AXFR) for every configured zone.

        Returns one ``dns_zone`` item per configured zone with SOA data in
        ``data``; the fuller per-record list (when AXFR succeeded) is stashed
        on ``self._zone_records`` for the detail-write phase, mirroring
        EOLAgent's ``self._derived`` stash pattern.
        """
        self._zone_records: dict[str, list[dict]] = {}

        zones = [z.strip() for z in (self.settings.dns_zones or "").split(",") if z.strip()]
        if not zones:
            raise CollectorSkipped("dns_zones not configured")
        if not _DNSPYTHON_AVAILABLE:
            raise CollectorSkipped("dnspython not installed")

        resolver_host = self.settings.dns_resolver_host or ""
        timeout = float(self.settings.dns_query_timeout_seconds)

        items: list[dict] = []
        errors: list[str] = []
        for zone_name in zones:
            try:
                self._gate_dns_zone(zone_name)
            except PermissionError as exc:
                msg = f"zone {zone_name!r} denied by dns_allowed_zones gate: {exc}"
                logger.warning("DnsAgent: %s", msg)
                errors.append(msg)
                continue

            self._audit_dns_query(
                tool="dns_soa", zone=zone_name, verdict="allow", resolver_host=resolver_host
            )
            try:
                soa = query_soa(zone_name, resolver_host, timeout)
            except Exception as exc:
                msg = f"SOA query failed for zone {zone_name!r}: {exc}"
                logger.warning("DnsAgent: %s", msg)
                errors.append(msg)
                self._audit_dns_query(
                    tool="dns_soa",
                    zone=zone_name,
                    verdict="allow",
                    resolver_host=resolver_host,
                    status="error",
                    error=str(exc),
                )
                continue

            axfr_succeeded = False
            records: list[dict] = []
            self._audit_dns_query(
                tool="dns_axfr", zone=zone_name, verdict="allow", resolver_host=resolver_host
            )
            try:
                fetched = query_axfr(zone_name, resolver_host, timeout)
                # Residual 2 (GitLab #95): query_axfr is a bare dnspython call,
                # not a LangChain @tool, so nothing in the CallbackManager
                # chain ever sees these values otherwise. Route TXT values
                # through DLPCallbackHandler._check BEFORE they're stashed
                # into self._zone_records/written to DnsRecord — reuses the
                # existing fail-closed/fail-open (dlp_fail_closed) semantics
                # and AuditLog write path already in callbacks/dlp.py. On a
                # fail-closed raise, `records` stays unassigned (still []) so
                # the flagged values never reach the stash below.
                self._check_axfr_txt_dlp(fetched)
                records = fetched
                axfr_succeeded = True
            except Exception as exc:
                # Expected in most environments (server refuses transfer to
                # an arbitrary client) — not an error, just reduced fidelity
                # for this zone this cycle. See tools/dns.py:query_axfr. Also
                # reached when the DLP check above raises (fail-closed).
                logger.info("DnsAgent: AXFR unavailable for zone %s: %s", zone_name, exc)
                self._audit_dns_query(
                    tool="dns_axfr",
                    zone=zone_name,
                    verdict="allow",
                    resolver_host=resolver_host,
                    status="error",
                    error=str(exc),
                )

            self._zone_records[zone_name] = records
            items.append(
                {
                    "name": zone_name,
                    "type": "dns_zone",
                    "data": {
                        **soa,
                        "axfr_succeeded": axfr_succeeded,
                        "record_count": len(records),
                    },
                }
            )

        if not items:
            # Every configured zone failed its SOA query — a data-quality
            # signal worth surfacing (F-007 style), not a silently "completed"
            # empty run. status is a computed property (ok/partial/failed) —
            # errors present + no items/count_override -> "failed" here.
            return CollectOutcome(
                items=[], count_override=0, errors=errors or ["all zone queries failed"]
            )
        return CollectOutcome(items=items, errors=errors)

    def _detail_writers(self, scope, result):
        return [self._write_dns_zones]

    def _write_dns_zones(self) -> int:
        """Upsert DnsZone + DnsRecord rows from the collect-phase stash.

        Uses ``_resource_id``/`_upsert_detail`/`_write_each` (etl/base.py) —
        same helpers EOL/vSphere/etc. use — rather than a hand-rolled upsert.
        """
        zone_records = getattr(self, "_zone_records", None)
        if not zone_records:
            return 0

        written = 0
        with get_session() as session:
            for zone_name, records in zone_records.items():
                resource_id = self._resource_id(session, "dns_zone", zone_name)
                if resource_id is None:
                    # The generic Resource/Snapshot upsert (run() base phase)
                    # runs before _detail_writers, so this should always
                    # resolve for a zone that made it into collect()'s
                    # returned items; guard anyway rather than crashing the
                    # whole detail-write phase on one bad zone.
                    logger.warning("DnsAgent: no Resource found for zone %s — skipping", zone_name)
                    continue

                # The generic Resource row's metadata_ carries the SOA fields
                # collect() returned as `data` — read them back rather than
                # re-threading collect()'s soa dict through the stash too.
                snapshot = session.query(Resource).filter_by(id=resource_id).one()
                soa_data = snapshot.metadata_ or {}
                zone_row_fields = {
                    "resource_id": resource_id,
                    "name": zone_name,
                    "serial": soa_data.get("serial"),
                    "primary_ns": soa_data.get("primary_ns"),
                    "admin_email": soa_data.get("admin_email"),
                    "refresh_seconds": soa_data.get("refresh_seconds"),
                    "retry_seconds": soa_data.get("retry_seconds"),
                    "expire_seconds": soa_data.get("expire_seconds"),
                    "minimum_ttl_seconds": soa_data.get("minimum_ttl_seconds"),
                    "axfr_succeeded": bool(soa_data.get("axfr_succeeded")),
                    "last_collected_at": _now(),
                }
                self._upsert_detail(session, DnsZone, zone_row_fields, key_fields=["resource_id"])
                session.flush()
                zone_id = session.query(DnsZone).filter_by(resource_id=resource_id).one().id
                written += 1

                def _write_record(rec, _zone_id=zone_id):
                    self._upsert_detail(
                        session,
                        DnsRecord,
                        {
                            "zone_id": _zone_id,
                            "name": rec["name"],
                            "type": rec["type"],
                            "value": rec["value"],
                            "ttl": rec.get("ttl"),
                            "priority": rec.get("priority"),
                        },
                        key_fields=["zone_id", "name", "type", "value"],
                    )

                rec_written, rec_skipped = self._write_each(session, records, _write_record)
                if rec_skipped:
                    logger.warning(
                        "DnsAgent: %d/%d records for zone %s failed to write",
                        rec_skipped,
                        rec_written + rec_skipped,
                        zone_name,
                    )
                written += rec_written

                # L-2 (GitLab #95): a record deleted upstream — exactly the
                # stale-record case this agent exists to catch — otherwise
                # persists in DnsRecord forever, since the upsert above only
                # ever adds/updates, never removes. Prune here, but ONLY
                # within the scope this run actually, successfully observed:
                # `axfr_succeeded` (read back from the same Resource metadata
                # the zone row above was built from) is False whenever this
                # zone's AXFR was refused/failed/DLP-blocked this cycle
                # (collect() leaves `records` == [] in that case) — pruning
                # then would wipe every known record on reduced fidelity,
                # not authoritative absence. Mirrors the delete-then-reinsert
                # discipline LinuxAgent/WindowsAgent use for their per-host
                # detail tables, scoped down to just the stale rows since the
                # rest were already upserted above.
                # TODO: migrate to a shared ReconcileScope (etl/base.py) once
                # available (task A1) instead of this inline axfr_succeeded
                # check.
                if zone_row_fields["axfr_succeeded"]:
                    observed_keys = {(r["name"], r["type"], r["value"]) for r in records}
                    stale_rows = [
                        row
                        for row in session.query(DnsRecord).filter_by(zone_id=zone_id).all()
                        if (row.name, row.type, row.value) not in observed_keys
                    ]
                    for row in stale_rows:
                        session.delete(row)
                    if stale_rows:
                        logger.info(
                            "DnsAgent: pruned %d stale record(s) from zone %s",
                            len(stale_rows),
                            zone_name,
                        )
            session.commit()
        return written

    def _audit_dns_query(
        self,
        tool: str,
        zone: str,
        verdict: str,
        resolver_host: str = "",
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        """Write one AgentActionLog row per DNS query (best-effort, never raises).

        Mirrors ``NetDiscoveryAgent._audit_subprocess`` — the raw-protocol
        (non-HTTP, non-@tool) audit trail for a read-only-by-convention call.
        ``args_summary`` records which server was queried (not just the zone)
        so the audit row can answer "who did DnsAgent talk to," matching the
        fidelity of netdiscovery's full-cmd summary.
        """
        summary = f"zone={zone} resolver={resolver_host or 'system-default'}"
        try:
            with get_session() as session:
                session.add(
                    AgentActionLog(
                        agent=type(self).__name__,
                        domain=self.domain,
                        tool=tool,
                        args_summary=summary[:2000],
                        verdict=verdict,
                        status=status,
                        error=error,
                    )
                )
                session.commit()
        except Exception:
            logger.exception(
                "DnsAgent: audit row write failed (tool=%s zone=%s verdict=%s)",
                tool,
                zone,
                verdict,
            )

    def _gate_dns_zone(self, zone: str) -> None:
        """R2 boundary gate: raise PermissionError BEFORE any SOA/AXFR query
        when the zone fails the allow-list gate. Fail-closed, mirroring
        ``NetDiscoveryAgent._gate_nmap_targets`` (netdiscovery.py:807-864):
        writes a ``deny`` audit row first, then raises — the caller (the
        per-zone loop in ``collect()``) catches PermissionError and skips
        only this zone, not the whole run.

        Unlike netdiscovery's exclude-list, this is a positive allow-list —
        DNS zones are a much smaller, more deliberate set than IP ranges.
        Empty ``dns_allowed_zones`` (default) allows every configured zone,
        matching pre-gate behavior.
        """
        allowed = [
            z.strip() for z in (self.settings.dns_allowed_zones or "").split(",") if z.strip()
        ]
        if not allowed:
            return
        if zone not in allowed:
            reason = f"zone {zone!r} not in dns_allowed_zones allow-list"
            self._audit_dns_query(
                tool="dns_gate",
                zone=zone,
                verdict="deny",
                status="error",
                error=reason,
            )
            raise PermissionError(f"[dns-gate] {reason}")

    def _check_axfr_txt_dlp(self, records: list[dict]) -> None:
        """Route AXFR TXT record values through DLPCallbackHandler._check.

        ``query_axfr`` (tools/dns.py) is a bare dnspython call, not a
        LangChain ``@tool``, so nothing in the CallbackManager chain ever
        sees these values — this call is what puts AXFR TXT data back under
        the same DLP boundary every LLM tool call gets. Reuses
        ``callbacks/dlp.py``'s existing fail-closed/fail-open
        (``dlp_fail_closed``) semantics and ``AuditLog`` write path
        unmodified — this only calls it, never reimplements it.
        """
        dlp = DLPCallbackHandler(agent_name=type(self).__name__)
        for rec in records:
            if rec.get("type") == "TXT":
                dlp._check(rec.get("value", ""), uuid.uuid4(), "dns-axfr-txt")
