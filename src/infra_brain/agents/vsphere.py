"""
VsphereAgent — reads VMware vSphere infrastructure via pyVmomi.
Opens ONE connection per vCenter and collects 7 resource types.
Gracefully degrades to empty list when pyVmomi is unavailable or vSphere is unconfigured.

Stage 2 (relational model): ``collect()`` still emits the generic ``Resource``
items the base ``run()`` upserts. A ``run()`` override then writes the normalized
relational tables — slow-changing INVENTORY tables are UPSERTed by the natural
key ``(vcenter, moref)``; append-only METRICS tables get one new row per sample.
The detail-write is wrapped in ``BaseAgent._write_details`` so a structural
failure marks the CollectionRun ``failed`` instead of silently leaving the rich
tables empty. Per-entity writes are guarded with a savepoint so one malformed
object logs+skips without aborting the rest.

EPITAPH — the vSphere topology edges (P5, rev11-T5-B). ``_write_inventory``
used to close by emitting three relationship types into
``resource_relationships``, the store P5 removes:

  * ``RUNS_ON``        vsphere_vm -> vsphere_host
  * ``MEMBER_OF``      vsphere_host -> vsphere_cluster, and vsphere_vm ->
                       vsphere_cluster indirectly via its ESXi host
  * ``IN_DATACENTER``  vsphere_cluster -> vsphere_datacenter

All three are GENUINE topology, not containment — which is exactly why they are
worth recording here rather than dismissing. They are deleted anyway because
this is a RETIRED domain: no vCenter credentials are configured, ``collect()``
raises ``CollectorSkipped``, and a declaration cannot be proven equivalent to a
deriver that produces nothing on this estate.

WHERE EVERY FACT LIVES NOW — each was already a column on the row the edge
started from, which is what made the edge a restatement in practice even though
it passes §3.1 in principle:
  * RUNS_ON       -> ``vsphere_vms.esxi_host``
  * MEMBER_OF     -> ``vsphere_hosts.cluster_or_parent`` (the VM->cluster leg
                     was itself derived transitively from that column plus
                     ``vsphere_vms.esxi_host`` — a two-column join, not a
                     collected fact)
  * IN_DATACENTER -> ``vsphere_clusters.datacenter_name``

THE PATH ON REVIVAL: declare all three on this agent's ``AgentSpec`` — every
endpoint is a ``resources`` row this collector owns, and each join is functional
from the many side, so they are ordinary FORWARD ``EdgeSpec``s. Do that rather
than restoring the deleted index; ``graph_edges`` can record that a VM MOVED
host, which the old ``UNIQUE(from, to, type)`` store structurally could not.
"""

import logging
from datetime import UTC, datetime, timedelta

from infra_brain.config import get_settings
from infra_brain.db.models import (
    Resource,
    VsphereAlarm,
    VsphereCluster,
    VsphereDatacenter,
    VsphereDatastore,
    VsphereDatastoreMetric,
    VsphereHost,
    VsphereHostMetric,
    VsphereLicense,
    VsphereNetwork,
    VspherePermission,
    VsphereResourcePool,
    VsphereSession,
    VsphereSnapshot,
    VsphereVm,
    VsphereVmDisk,
    VsphereVmMetric,
)
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class VsphereAgent(ETLConnector):
    spec = AgentSpec(
        domain="vsphere",
        tier=Tier.COLLECTOR,
        schedule="25 */6 * * *",
        max_staleness=timedelta(hours=2),
        # 2026-08-12: retired. This system monitors a home lab; vCenter is an
        # enterprise remnant that is deliberately unused here and will never be
        # configured (docs/TRACKER.md TRK-343: "vSphere is deliberately unused
        # in this environment and never will be configured"; TRK-248 says the
        # same). Between the 6-hourly full inventory and the 15-minute pulse
        # this was the single largest producer of `skipped` collection_runs on
        # the box. Re-enable with COLLECTION_REVIVED_DOMAINS=vsphere — no code
        # change needed; the schedule above is what it resumes on.
        #
        # NOTE for whoever revisits discovery: DiscoveryAgent self-skips only
        # when vSphere AND the Ansible inventory are BOTH absent
        # (discovery.py's has_vsphere/has_inventory check). It is NOT retired
        # and keeps working off the inventory alone.
        retired=True,
    )

    def _get_vcenter_hosts(self, settings) -> list[str]:
        """Return list of vCenter hostnames to scan. vsphere_hosts takes precedence."""
        hosts_str = settings.vsphere_hosts or settings.vsphere_host
        return [h.strip() for h in hosts_str.split(",") if h.strip()]

    def _collect_vcenter(
        self, host: str, settings, scope: str = "all", errors: list | None = None
    ) -> list[dict]:
        """Open ONE connection to a vCenter, collect based on scope, disconnect.

        scope="pulse"  — fast path: power state + quickStats for VMs and ESXi only.
        scope="all"    — full 7-type inventory; optionally enriched with QueryPerf history.
        """
        if errors is None:
            errors = []
        from infra_brain.tools.vsphere import (
            _PYVMOMI_AVAILABLE,
            Disconnect,
            _connect,
            collect_clusters,
            collect_datacenters,
            collect_datastores,
            collect_esxi_hosts,
            collect_esxi_pulse,
            collect_networks,
            collect_resource_pools,
            collect_vm_perf_history,
            collect_vm_pulse,
            collect_vms,
        )

        if not _PYVMOMI_AVAILABLE:
            raise CollectorSkipped("pyVmomi not installed")

        try:
            si = _connect(
                host,
                settings.vsphere_user,
                settings.vsphere_password,
                settings.vsphere_ssl_verify,
                settings.vsphere_connect_timeout,
            )
        except Exception as exc:
            msg = f"cannot connect to {host}: {exc}"
            logger.warning("VsphereAgent: %s", msg)
            errors.append(msg)
            return []

        # AA-C-4 / S-9: everything between connect and disconnect runs inside a
        # try/finally so ``Disconnect(si)`` ALWAYS runs — even if
        # ``RetrieveContent()`` (or anything else) raises. The pre-MR-B code
        # called RetrieveContent() outside any guard and Disconnect() as a
        # trailing statement, so a failure in between leaked the vCenter session.
        items = []
        try:
            content = si.RetrieveContent()

            if scope == "pulse":
                for label, fn in [
                    ("VM pulse", collect_vm_pulse),
                    ("ESXi pulse", collect_esxi_pulse),
                ]:
                    try:
                        results = fn(content, host)
                        items.extend(results)
                        logger.info(
                            "VsphereAgent: %s -> %d items from %s", label, len(results), host
                        )
                    except Exception as exc:
                        logger.warning("VsphereAgent: %s failed for %s: %s", label, host, exc)
                        errors.append(f"{label} failed for {host}: {exc}")
            else:
                collectors = [
                    ("VMs", collect_vms),
                    ("ESXi hosts", collect_esxi_hosts),
                    ("datastores", collect_datastores),
                    ("clusters", collect_clusters),
                    ("datacenters", collect_datacenters),
                    ("networks", collect_networks),
                    ("resource pools", collect_resource_pools),
                ]
                for label, fn in collectors:
                    try:
                        results = fn(content, host)
                        items.extend(results)
                        logger.info(
                            "VsphereAgent: %s -> %d items from %s", label, len(results), host
                        )
                    except Exception as exc:
                        logger.warning(
                            "VsphereAgent: %s collection failed for %s: %s", label, host, exc
                        )
                        errors.append(f"{label} collection failed for {host}: {exc}")

                # Phase F: vCenter-scoped lists (licenses/alarms/permissions/
                # sessions). Best-effort per kind — a privilege gap on one must
                # not fail the sweep or lose the others.
                from infra_brain.tools.vsphere import (
                    collect_licenses,
                    collect_permissions,
                    collect_sessions,
                    collect_triggered_alarms,
                )

                scoped = {"vcenter": host}
                for kind, sfn in [
                    ("licenses", collect_licenses),
                    ("alarms", collect_triggered_alarms),
                    ("permissions", collect_permissions),
                    ("sessions", collect_sessions),
                ]:
                    try:
                        scoped[kind] = sfn(content, host)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("VsphereAgent: %s fetch failed for %s: %s", kind, host, exc)
                        scoped[kind] = None  # None = not collected (leave existing rows)
                        # M-7: preserving existing rows on a scoped-fetch
                        # failure (the line above) is deliberate and correct
                        # — a transient/permission blip must not blow away
                        # last-known-good licenses/alarms/permissions/
                        # sessions. But the failure itself was previously
                        # ONLY logged, so a PERMANENT privilege gap (e.g. a
                        # service account missing System.Read on one vCenter)
                        # was invisible in collection_runs forever, and
                        # grant/revoke history for that kind silently stopped
                        # being emitted (_scoped_history_items only turns
                        # non-None lists into Resource items) with no signal
                        # anywhere that it had stopped. Surface it exactly
                        # like every other collector-level failure in this
                        # method (errors.append) so the run downgrades to
                        # "partial" via the existing CollectOutcome status
                        # mapping — without touching the None-preserves-rows
                        # behavior above.
                        errors.append(f"{kind} fetch failed for {host}: {exc}")
                if not hasattr(self, "_vcenter_scoped"):
                    self._vcenter_scoped = []
                self._vcenter_scoped.append(scoped)

                if getattr(settings, "vsphere_collect_perf_history", False):
                    try:
                        vm_perf = collect_vm_perf_history(
                            content,
                            host,
                            interval_id=getattr(settings, "vsphere_perf_interval_id", 300),
                            max_samples=getattr(settings, "vsphere_perf_max_samples", 12),
                            batch_size=getattr(settings, "vsphere_perf_batch_size", 25),
                        )
                        if vm_perf:
                            for item in items:
                                if item["type"] == "vsphere_vm" and item["name"] in vm_perf:
                                    item["data"].update(vm_perf[item["name"]])
                            logger.info(
                                "VsphereAgent: perf history enriched %d VMs from %s",
                                len(vm_perf),
                                host,
                            )
                    except Exception as exc:
                        logger.warning("VsphereAgent: perf history failed for %s: %s", host, exc)
                        errors.append(f"perf history failed for {host}: {exc}")
        except Exception as exc:
            # RetrieveContent() or an unexpected error — record and move on; the
            # finally block still releases the session.
            logger.warning("VsphereAgent: inventory retrieval failed for %s: %s", host, exc)
            errors.append(f"inventory retrieval failed for {host}: {exc}")
        finally:
            try:
                Disconnect(si)
            except Exception as exc:
                logger.debug("VsphereAgent: Disconnect(si) failed for %s: %s", host, exc)
        return items

    def collect(self, scope: str = "all") -> CollectOutcome:
        settings = get_settings()
        vcenter_hosts = self._get_vcenter_hosts(settings)
        if not vcenter_hosts:
            raise CollectorSkipped("no vCenter hosts configured (set VSPHERE_HOSTS)")

        items = []
        errors: list[str] = []
        self._vcenter_scoped = []  # Phase F: per-vcenter license/alarm/perm/session lists
        for host in vcenter_hosts:
            pre_err_count = len(errors)
            items.extend(self._collect_vcenter(host, settings, scope, errors=errors))
            # Phase I: emit the vCenter server itself as a first-class Resource
            # (TRK-171 follow-up). Previously no node existed for the vCenter
            # server — every collected entity embeds it only as a "(vcenter)"
            # name suffix — so ASSIGNED_TO (vsphere_license -> vcenter) could
            # never resolve a target and always emitted 0 edges even with live
            # license data present. Only emitted when this cycle's connection
            # actually succeeded (a connect failure leaves no new "cannot
            # connect to {host}" error), so an unreachable/misconfigured host
            # doesn't get a ghost vcenter node.
            connect_failed = any(f"cannot connect to {host}" in e for e in errors[pre_err_count:])
            if scope == "all" and not connect_failed:
                items.append(
                    {
                        "name": host,
                        "type": "vsphere_vcenter",
                        "data": {"vcenter": host, "moref": f"vcenter:{host}"},
                    }
                )
        # Phase H: also emit permissions/licenses/alarms as Resource items so they
        # flow through the base snapshot + DeepDiff drift pipeline — giving grant/
        # revoke/role-change and alarm fire/clear HISTORY for free. Sessions are
        # deliberately excluded (ephemeral — churn would be pure drift noise).
        if scope == "all":
            items.extend(self._scoped_history_items())
        # Cache for the relational detail-write phase so we don't re-connect.
        self._last_items = items
        return CollectOutcome(items=items, errors=errors)

    def _scoped_history_items(self) -> list[dict]:
        """Turn the vCenter-scoped lists (permissions/licenses/alarms) into
        Resource items with a stable natural-key name so the base pipeline
        snapshots them (append-only history) and DriftDetector diffs them.
        A synthetic ``moref`` (the natural key) lets the inventory-writer skip
        them cleanly without a 'no moref' warning; they have no relational
        table there (they live in the Phase F current-state tables)."""
        out: list[dict] = []
        for entry in getattr(self, "_vcenter_scoped", None) or []:
            vc = entry.get("vcenter")
            for perm in entry.get("permissions") or []:
                ent = perm.get("entity") or "root"
                name = f"{perm.get('principal')}@{ent}"
                out.append(
                    {
                        "name": name,
                        "type": "vsphere_permission",
                        "data": {**perm, "vcenter": vc, "moref": f"perm:{name}"},
                    }
                )
            for lic in entry.get("licenses") or []:
                name = lic.get("name") or lic.get("edition_key") or "license"
                out.append(
                    {
                        "name": name,
                        "type": "vsphere_license",
                        "data": {**lic, "vcenter": vc, "moref": f"lic:{name}"},
                    }
                )
            for alarm in entry.get("alarms") or []:
                name = f"{alarm.get('alarm_name')}@{alarm.get('entity_name') or 'root'}"
                out.append(
                    {
                        "name": name,
                        "type": "vsphere_alarm",
                        "data": {**alarm, "vcenter": vc, "moref": f"alarm:{name}"},
                    }
                )
        return out

    # ------------------------------------------------------------------
    # Relational detail-write phase (Stage 2)
    # ------------------------------------------------------------------

    def _detail_writers(self, scope, result):
        # Surface a detail-write failure on the CollectionRun (no silent data
        # loss) via ETLConnector.run()'s _write_details.
        return [lambda: self._write_vsphere_details(scope)]

    def _write_vsphere_details(self, scope: str) -> int:
        """Populate the relational vSphere tables from the items collect() emitted.

        Idle-safety FIRST: when no vCenter host is configured the connector is
        paused — collect() returned [] and never connected — so there is nothing
        to write and we must not touch the DB or vCenter.

        Returns the number of detail rows written so ``_write_details`` can
        persist it to ``CollectionRun.detail_rows_written`` (GitLab #148: this
        writer used to return None, so even though the relational tables were
        populated on every run, the counter stayed 0 forever — indistinguishable
        from the write path being dead).
        """
        settings = getattr(self, "settings", None) or get_settings()
        if not (settings.vsphere_host or settings.vsphere_hosts):
            logger.info("VsphereAgent: host unset — relational write skipped (idle)")
            return 0

        items = getattr(self, "_last_items", None) or []
        if not items and scope != "pulse":
            # collect() yields nothing when paused/unreachable; nothing to upsert.
            return 0

        if scope == "pulse":
            rows_written = self._write_pulse_metrics(items)
        else:
            rows_written = self._write_inventory(items)
            rows_written += self._write_vcenter_scoped()

        self._prune_metrics(settings)
        return rows_written

    def _write_vcenter_scoped(self) -> int:
        """Phase F: write per-vCenter license/alarm/permission/session snapshots.
        Delete-reinsert per (vcenter, kind); a kind whose fetch returned None
        (privilege gap) is left untouched. One session per vcenter.
        Returns the number of rows inserted (GitLab #148)."""
        scoped = getattr(self, "_vcenter_scoped", None) or []
        if not scoped:
            return 0
        model_by_kind = {
            "licenses": VsphereLicense,
            "alarms": VsphereAlarm,
            "permissions": VspherePermission,
            "sessions": VsphereSession,
        }
        dt_fields = {"expiration", "triggered_at", "login_time", "last_active"}
        rows_written = 0
        with get_session() as session:
            for entry in scoped:
                vcenter = entry.get("vcenter")
                for kind, model in model_by_kind.items():
                    rows = entry.get(kind)
                    if rows is None:  # not collected — preserve existing rows
                        continue
                    session.query(model).filter_by(vcenter=vcenter).delete(
                        synchronize_session=False
                    )
                    for r in rows:
                        vals = {"vcenter": vcenter}
                        for k, v in r.items():
                            if k in dt_fields and isinstance(v, str):
                                try:
                                    v = datetime.fromisoformat(v)
                                except ValueError:
                                    v = None
                            vals[k] = v
                        session.add(model(**vals))
                        rows_written += 1
            session.commit()
        return rows_written

    # --- scope="all": upsert the 7 inventory tables ---------------------

    def _write_inventory(self, items: list[dict]) -> int:
        """Upsert the seven vSphere relational inventory tables.

        Returns the number of relational detail rows upserted (GitLab #148).

        P5 (rev11-T5-B): this method used to ALSO build a per-vCenter
        moref→resource_id index and emit RUNS_ON / MEMBER_OF / IN_DATACENTER
        topology edges into ``resource_relationships``. That store is being
        dropped and the edges go with it — see the module docstring's EPITAPH.
        The detail upserts are untouched, deliberately: vSphere is a RETIRED
        domain (no live vCenter credentials), and if it is ever revived these
        seven tables must still be written from the first sweep. Only the index
        and the edge build, which existed for nothing else, are gone.
        """
        rows_written = 0
        with get_session() as session:
            for item in items:
                # Each entity is isolated in a savepoint so one malformed object
                # logs+skips without aborting the rest of the inventory.
                try:
                    with session.begin_nested():
                        wrote = self._upsert_inventory_item(session, item)
                except Exception as exc:
                    logger.warning(
                        "VsphereAgent: skipping bad %s item %r: %s",
                        item.get("type"),
                        item.get("name"),
                        exc,
                    )
                    continue
                if wrote:
                    rows_written += 1

                # P5 (rev11-T5-B): the per-vCenter (vcenter, moref) ->
                # resource_id index and the RUNS_ON / MEMBER_OF /
                # IN_DATACENTER topology-edge build that followed it were
                # DELETED here. They read nothing this loop needs and wrote
                # nothing but ``resource_relationships`` rows; see the module
                # docstring's EPITAPH for where each fact lives now.

            session.commit()
        return rows_written

    def _upsert_inventory_item(self, session, item: dict) -> bool:
        """Upsert one item into its relational table.

        Returns True when a detail row was written, False when the item's type
        has no relational table (so _write_inventory's row count only counts
        real writes — GitLab #148).
        """
        item_type = item.get("type", "")
        data = item.get("data", {}) or {}
        name = item.get("name", "")
        vcenter = data.get("vcenter") or ""
        moref = data.get("moref") or ""
        if not moref:
            raise ValueError(f"no moref on {item_type} {name!r} — cannot key relational row")

        resource_id = self._resource_id(
            session,
            item_type,
            name,
            qualify=lambda n: self._qualified_name(session, n, vcenter, item_type, moref),
        )

        if item_type in ("vsphere_vm", "vsphere_template"):
            self._upsert_vm(session, data, name, vcenter, moref, resource_id, item_type)
        elif item_type == "vsphere_host":
            self._upsert_host(session, data, name, vcenter, moref, resource_id)
        elif item_type == "vsphere_datastore":
            self._upsert_datastore(session, data, name, vcenter, moref, resource_id)
        elif item_type == "vsphere_cluster":
            self._upsert_cluster(session, data, name, vcenter, moref, resource_id)
        elif item_type == "vsphere_datacenter":
            self._upsert_datacenter(session, data, name, vcenter, moref, resource_id)
        elif item_type in ("vsphere_portgroup", "vsphere_dvswitch", "vsphere_dvportgroup"):
            self._upsert_network(session, data, name, vcenter, moref, resource_id, item_type)
        elif item_type == "vsphere_resource_pool":
            self._upsert_resource_pool(session, data, name, vcenter, moref, resource_id)
        else:
            logger.debug("VsphereAgent: no relational table for type %s", item_type)
            return False
        return True

    def _qualified_name(
        self, session, name: str, vcenter: str, item_type: str = "", moref: str = ""
    ) -> str:
        """vCenter-scoped canonical Resource name (S-9), moref-safe (#184).

        Two vCenters can host resources with identical names/morefs; the shared
        Resource natural key is (domain, type, name), so without the vCenter in
        the name they collapse into one Resource row. Qualifying the name with
        the vCenter host keeps them distinct. Empty ``vcenter`` (e.g. legacy
        rows) leaves the name unchanged.

        The vCenter server's own node (``item_type="vsphere_vcenter"``) is
        exempt: qualifying it against itself would produce ``"host (host)"``,
        but its bare hostname IS its identity — other emitters (ASSIGNED_TO in
        graph_maintenance.py) require an exact-name match against
        ``vsphere_licenses.vcenter``, which stores the bare host string.

        #184: vCenter-qualification alone doesn't handle two genuinely
        DIFFERENT objects sharing a display name WITHIN one vCenter (template
        clones, same-named VMs in different clusters/folders) — the vCenter
        qualifier is identical for both, so they still collapsed into one
        Resource row, with the second upsert silently overwriting the first's
        data. Only disambiguate on an actually-detected collision (an existing
        row at the vCenter-qualified name whose stored moref differs from this
        item's) so the overwhelming common case — no collision — stays
        byte-identical to before; a real collision gets a ``[moref]`` suffix
        instead of a data clobber.
        """
        if item_type == "vsphere_vcenter":
            return name
        base = f"{name} ({vcenter})" if vcenter else name
        if not moref:
            return base
        existing = (
            session.query(Resource).filter_by(domain=self.domain, type=item_type, name=base).first()
        )
        if existing is not None:
            existing_moref = (existing.metadata_ or {}).get("moref")
            if existing_moref and existing_moref != moref:
                return f"{base} [{moref}]"
        return base

    def _upsert_resource(self, session, item: dict):
        """Override the base Resource upsert to scope vSphere identity by vCenter.

        The base upsert keys on (domain, type, name); we qualify ``name`` with
        the item's vCenter host so resources from different vCenters never
        collapse into a single Resource row (S-9). Everything else mirrors the
        base implementation.
        """
        from infra_brain.api._seeding import upsert_resource

        data = item.get("data", {}) or {}
        vcenter = data.get("vcenter") or ""
        item_type = item.get("type", "unknown")
        moref = data.get("moref") or ""
        return upsert_resource(
            session,
            name=self._qualified_name(session, item["name"], vcenter, item_type, moref),
            domain=self.domain,
            resource_type=item_type,
            metadata=data,
            zone=self.settings.default_zone,
            source=type(self).__name__,
        )

    # _resource_id migrated to ETLConnector._resource_id (Task 4,
    # phase1/shared-helpers); call sites now pass qualify=lambda n:
    # self._qualified_name(n, vcenter) since the canonical Resource row is
    # vCenter-qualified.

    @staticmethod
    def _details(data: dict, mapped_keys: set[str]) -> dict | None:
        """Leftover data fields (not mapped to a typed column) → details JSONB."""
        skip = mapped_keys | {"vcenter", "moref", "pulse"}
        extras = {k: v for k, v in data.items() if k not in skip}
        return extras or None

    def _upsert_vm(self, session, data, name, vcenter, moref, resource_id, item_type) -> None:
        mapped = {
            "uuid",
            "instance_uuid",
            "guest_full_name",
            "guest_id",
            "num_cpu",
            "memory_mb",
            "cores_per_socket",
            "hw_version",
            "power_state",
            "boot_time",
            "esxi_host",
            "guest_hostname",
            "ip_address",
            "all_ips",
            "tools_status",
            "tools_version",
            "datastore_names",
            "network_names",
            "snapshot_count",
            "annotation",
            "overall_status",
            "cpu_reservation_mhz",
            "cpu_limit_mhz",
            "mem_reservation_mb",
            "mem_limit_mb",
            "firmware",
            "secure_boot",
            "encrypted",
            "resource_pool_moref",
            # child records handled separately — keep out of details JSON
            "disks",
            "snapshots",
        }
        row = {
            "resource_id": resource_id,
            "vcenter": vcenter,
            "moref": moref,
            "name": name,
            "overall_status": data.get("overall_status"),
            "uuid": data.get("uuid"),
            "instance_uuid": data.get("instance_uuid"),
            "is_template": item_type == "vsphere_template",
            "guest_full_name": data.get("guest_full_name"),
            "guest_id": data.get("guest_id"),
            "num_cpu": data.get("num_cpu"),
            "memory_mb": data.get("memory_mb"),
            "cores_per_socket": data.get("cores_per_socket"),
            "hw_version": data.get("hw_version"),
            "power_state": data.get("power_state"),
            "boot_time": data.get("boot_time"),
            "esxi_host": data.get("esxi_host"),
            "guest_hostname": data.get("guest_hostname"),
            "ip_address": data.get("ip_address"),
            "all_ips": data.get("all_ips") or [],
            "tools_status": data.get("tools_status"),
            "tools_version": data.get("tools_version"),
            "datastore_names": data.get("datastore_names") or [],
            "network_names": data.get("network_names") or [],
            "snapshot_count": data.get("snapshot_count"),
            "annotation": data.get("annotation"),
            "cpu_reservation_mhz": data.get("cpu_reservation_mhz"),
            "cpu_limit_mhz": data.get("cpu_limit_mhz"),
            "mem_reservation_mb": data.get("mem_reservation_mb"),
            "mem_limit_mb": data.get("mem_limit_mb"),
            "firmware": data.get("firmware"),
            "secure_boot": data.get("secure_boot"),
            "encrypted": data.get("encrypted"),
            "resource_pool_moref": data.get("resource_pool_moref"),
            "details": self._details(data, mapped | _PERF_FIELDS),
        }
        self._upsert_detail(session, VsphereVm, row, ["vcenter", "moref"])
        self._upsert_vm_children(session, vcenter, moref, name, data)

        # If perf-history fields rode along on this VM, ALSO append a metrics row.
        if any(k in data for k in _PERF_FIELDS):
            self._append_vm_metric(session, data, name, vcenter, moref, source="perf")

    def _upsert_vm_children(self, session, vcenter, moref, vm_name, data) -> None:
        """Delete-reinsert this VM's disks + snapshots (child tables keyed by
        (vcenter, vm_moref, key)). Delete-reinsert keeps the set in sync as
        disks/snapshots are added/removed, mirroring octopus's per-owner pattern."""
        disks = data.get("disks") or []
        snaps = data.get("snapshots") or []
        session.query(VsphereVmDisk).filter_by(vcenter=vcenter, vm_moref=moref).delete(
            synchronize_session=False
        )
        for d in disks:
            if d.get("disk_key") is None:
                continue
            session.add(
                VsphereVmDisk(
                    vcenter=vcenter,
                    vm_moref=moref,
                    vm_name=vm_name,
                    disk_key=d["disk_key"],
                    label=d.get("label"),
                    capacity_gb=d.get("capacity_gb"),
                    thin_provisioned=d.get("thin_provisioned"),
                    backing_type=d.get("backing_type"),
                    datastore_name=d.get("datastore_name"),
                    file_path=d.get("file_path"),
                )
            )
        session.query(VsphereSnapshot).filter_by(vcenter=vcenter, vm_moref=moref).delete(
            synchronize_session=False
        )
        for sp in snaps:
            if sp.get("snapshot_id") is None:
                continue
            created = sp.get("created_at")
            if isinstance(created, str):
                try:
                    created = datetime.fromisoformat(created)
                except ValueError:
                    created = None
            session.add(
                VsphereSnapshot(
                    vcenter=vcenter,
                    vm_moref=moref,
                    vm_name=vm_name,
                    snapshot_id=sp["snapshot_id"],
                    name=sp.get("name"),
                    description=sp.get("description"),
                    created_at=created,
                    age_days=sp.get("age_days"),
                    is_current=sp.get("is_current"),
                    state=sp.get("state"),
                )
            )

    def _upsert_host(self, session, data, name, vcenter, moref, resource_id) -> None:
        mapped = {
            "overall_status",
            "hardware_uuid",
            "vendor",
            "model",
            "cpu_model",
            "num_cpu_cores",
            "num_cpu_threads",
            "cpu_mhz",
            "memory_gb",
            "num_nics",
            "num_hbas",
            "version",
            "build",
            "connection_state",
            "power_state",
            "in_maintenance_mode",
            "dns_hostname",
            "vm_count",
            "datastore_names",
            "cluster_or_parent",
            "ntp_servers",
            "syslog_host",
            "lockdown_mode",
            "ssh_enabled",
            "esxi_shell_enabled",
            "shell_timeout_sec",
            "account_lock_failures",
            "bios_version",
            "bios_date",
            "hw_health",
            "hw_health_issues",
        }
        bios_date = data.get("bios_date")
        if isinstance(bios_date, str):
            try:
                bios_date = datetime.fromisoformat(bios_date)
            except ValueError:
                bios_date = None
        row = {
            "resource_id": resource_id,
            "vcenter": vcenter,
            "moref": moref,
            "name": name,
            "overall_status": data.get("overall_status"),
            "hardware_uuid": data.get("hardware_uuid"),
            "cluster_name": data.get("cluster_or_parent"),
            "ntp_servers": data.get("ntp_servers") or [],
            "syslog_host": data.get("syslog_host"),
            "lockdown_mode": data.get("lockdown_mode"),
            "ssh_enabled": data.get("ssh_enabled"),
            "esxi_shell_enabled": data.get("esxi_shell_enabled"),
            "shell_timeout_sec": data.get("shell_timeout_sec"),
            "account_lock_failures": data.get("account_lock_failures"),
            "bios_version": data.get("bios_version"),
            "bios_date": bios_date,
            "hw_health": data.get("hw_health"),
            "hw_health_issues": data.get("hw_health_issues"),
            "vendor": data.get("vendor"),
            "model": data.get("model"),
            "cpu_model": data.get("cpu_model"),
            "num_cpu_cores": data.get("num_cpu_cores"),
            "num_cpu_threads": data.get("num_cpu_threads"),
            "cpu_mhz": data.get("cpu_mhz"),
            "memory_gb": data.get("memory_gb"),
            "num_nics": data.get("num_nics"),
            "num_hbas": data.get("num_hbas"),
            "version": data.get("version"),
            "build": data.get("build"),
            "connection_state": data.get("connection_state"),
            "power_state": data.get("power_state"),
            "in_maintenance_mode": data.get("in_maintenance_mode"),
            "dns_hostname": data.get("dns_hostname"),
            "vm_count": data.get("vm_count"),
            "datastore_names": data.get("datastore_names") or [],
            "details": self._details(data, mapped),
        }
        self._upsert_detail(session, VsphereHost, row, ["vcenter", "moref"])

    def _upsert_datastore(self, session, data, name, vcenter, moref, resource_id) -> None:
        mapped = {
            "overall_status",
            "datastore_type",
            "capacity_gb",
            "free_gb",
            "used_gb",
            "used_pct",
            "accessible",
            "maintenance_mode",
            "host_count",
            "vm_count",
            "url",
            "sioc_enabled",
            "vmfs_version",
            "ssd",
            "remote_host",
            "remote_path",
        }
        row = {
            "resource_id": resource_id,
            "vcenter": vcenter,
            "moref": moref,
            "name": name,
            "overall_status": data.get("overall_status"),
            "datastore_type": data.get("datastore_type"),
            "capacity_gb": data.get("capacity_gb"),
            "free_gb": data.get("free_gb"),
            "used_gb": data.get("used_gb"),
            "used_pct": data.get("used_pct"),
            "accessible": data.get("accessible"),
            "maintenance_mode": _coerce_bool(data.get("maintenance_mode")),
            "host_count": data.get("host_count"),
            "vm_count": data.get("vm_count"),
            "url": data.get("url"),
            "sioc_enabled": _coerce_bool(data.get("sioc_enabled")),
            "vmfs_version": data.get("vmfs_version"),
            "ssd": _coerce_bool(data.get("ssd")),
            "remote_host": data.get("remote_host"),
            "remote_path": data.get("remote_path"),
            "details": self._details(data, mapped),
        }
        self._upsert_detail(session, VsphereDatastore, row, ["vcenter", "moref"])
        # Phase E — append a capacity/free sample for storage-growth trend.
        session.add(
            VsphereDatastoreMetric(
                vcenter=vcenter,
                moref=moref,
                name=name,
                capacity_gb=data.get("capacity_gb"),
                free_gb=data.get("free_gb"),
                used_gb=data.get("used_gb"),
                used_pct=data.get("used_pct"),
            )
        )

    def _upsert_cluster(self, session, data, name, vcenter, moref, resource_id) -> None:
        mapped = {
            "overall_status",
            "datacenter_name",
            "ha_enabled",
            "drs_enabled",
            "drs_default_behavior",
            "num_hosts",
            "num_effective_hosts",
            "total_cpu_mhz",
            "total_memory_gb",
            "num_cpu_cores",
            "host_names",
            "datastore_names",
            "evc_mode",
            "ha_admission_control",
            "drs_rules",
        }
        row = {
            "resource_id": resource_id,
            "vcenter": vcenter,
            "moref": moref,
            "name": name,
            "overall_status": data.get("overall_status"),
            "datacenter_name": data.get("datacenter_name"),
            "evc_mode": data.get("evc_mode"),
            "ha_admission_control": data.get("ha_admission_control"),
            "drs_rules": data.get("drs_rules") or [],
            "ha_enabled": data.get("ha_enabled"),
            "drs_enabled": data.get("drs_enabled"),
            "drs_default_behavior": data.get("drs_default_behavior"),
            "num_hosts": data.get("num_hosts"),
            "num_effective_hosts": data.get("num_effective_hosts"),
            "total_cpu_mhz": data.get("total_cpu_mhz"),
            "total_memory_gb": data.get("total_memory_gb"),
            "num_cpu_cores": data.get("num_cpu_cores"),
            "host_names": data.get("host_names") or [],
            "datastore_names": data.get("datastore_names") or [],
            "details": self._details(data, mapped),
        }
        self._upsert_detail(session, VsphereCluster, row, ["vcenter", "moref"])

    def _upsert_datacenter(self, session, data, name, vcenter, moref, resource_id) -> None:
        mapped = {"overall_status", "parent"}
        row = {
            "resource_id": resource_id,
            "vcenter": vcenter,
            "moref": moref,
            "name": name,
            "overall_status": data.get("overall_status"),
            "parent": data.get("parent"),
            "details": self._details(data, mapped),
        }
        self._upsert_detail(session, VsphereDatacenter, row, ["vcenter", "moref"])

    def _upsert_network(self, session, data, name, vcenter, moref, resource_id, item_type) -> None:
        kind = {
            "vsphere_portgroup": "portgroup",
            "vsphere_dvswitch": "dvswitch",
            "vsphere_dvportgroup": "dvportgroup",
        }[item_type]
        mapped = {
            "overall_status",
            "accessible",
            "num_ports",
            "uuid",
            "version",
            "host_count",
            "vm_count",
        }
        row = {
            "resource_id": resource_id,
            "vcenter": vcenter,
            "moref": moref,
            "name": name,
            "overall_status": data.get("overall_status"),
            "network_kind": kind,
            "accessible": data.get("accessible"),
            "num_ports": data.get("num_ports"),
            "dvs_uuid": data.get("uuid"),
            "version": data.get("version"),
            "host_count": data.get("host_count"),
            "vm_count": data.get("vm_count"),
            "details": self._details(data, mapped),
        }
        self._upsert_detail(session, VsphereNetwork, row, ["vcenter", "moref"])

    def _upsert_resource_pool(self, session, data, name, vcenter, moref, resource_id) -> None:
        mapped = {
            "overall_status",
            "cpu_limit",
            "cpu_reservation",
            "memory_limit",
            "memory_reservation",
            "cpu_usage_mhz",
            "memory_usage_mb",
            "vm_count",
        }
        row = {
            "resource_id": resource_id,
            "vcenter": vcenter,
            "moref": moref,
            "name": name,
            "overall_status": data.get("overall_status"),
            "cpu_limit": data.get("cpu_limit"),
            "cpu_reservation": data.get("cpu_reservation"),
            "memory_limit": data.get("memory_limit"),
            "memory_reservation": data.get("memory_reservation"),
            "cpu_usage_mhz": data.get("cpu_usage_mhz"),
            "memory_usage_mb": data.get("memory_usage_mb"),
            "vm_count": data.get("vm_count"),
            "details": self._details(data, mapped),
        }
        self._upsert_detail(session, VsphereResourcePool, row, ["vcenter", "moref"])

    # --- scope="pulse": append time-series metrics ----------------------

    def _write_pulse_metrics(self, items: list[dict]) -> int:
        # Returns the number of metric rows appended (GitLab #148).
        rows_written = 0
        with get_session() as session:
            for item in items:
                try:
                    with session.begin_nested():
                        wrote = self._append_pulse_item(session, item)
                except Exception as exc:
                    logger.warning(
                        "VsphereAgent: skipping bad pulse %s %r: %s",
                        item.get("type"),
                        item.get("name"),
                        exc,
                    )
                    continue
                if wrote:
                    rows_written += 1
            session.commit()
        return rows_written

    def _append_pulse_item(self, session, item: dict) -> bool:
        """Append one pulse metric row; returns True when a row was written."""
        data = item.get("data", {}) or {}
        name = item.get("name", "")
        vcenter = data.get("vcenter") or ""
        moref = data.get("moref") or ""
        if not moref:
            raise ValueError(f"no moref on pulse {item.get('type')} {name!r}")
        if item.get("type") == "vsphere_vm":
            self._append_vm_metric(session, data, name, vcenter, moref, source="pulse")
        elif item.get("type") == "vsphere_host":
            self._append_host_metric(session, data, name, vcenter, moref, source="pulse")
        else:
            return False
        return True

    def _append_vm_metric(self, session, data, name, vcenter, moref, source: str) -> None:
        session.add(
            VsphereVmMetric(
                vcenter=vcenter,
                moref=moref,
                name=name,
                collected_at=_now(),
                source=source,
                power_state=data.get("power_state"),
                cpu_usage_mhz=data.get("cpu_usage_mhz"),
                memory_usage_mb=data.get("memory_usage_mb"),
                uptime_seconds=data.get("uptime_seconds"),
                ballooned_memory_mb=data.get("ballooned_memory_mb"),
                overall_status=data.get("overall_status"),
                ip_address=data.get("ip_address"),
                perf_cpu_usage_pct_avg=data.get("perf_cpu_usage_pct_avg"),
                perf_cpu_ready_ms_sum=data.get("perf_cpu_ready_ms_sum"),
                perf_mem_usage_pct_avg=data.get("perf_mem_usage_pct_avg"),
                perf_disk_read_kbps_avg=data.get("perf_disk_read_kbps_avg"),
                perf_disk_write_kbps_avg=data.get("perf_disk_write_kbps_avg"),
                perf_net_rx_kbps_avg=data.get("perf_net_rx_kbps_avg"),
                perf_net_tx_kbps_avg=data.get("perf_net_tx_kbps_avg"),
                perf_samples=data.get("perf_samples"),
                perf_interval_id=data.get("perf_interval_id"),
            )
        )

    def _append_host_metric(self, session, data, name, vcenter, moref, source: str) -> None:
        session.add(
            VsphereHostMetric(
                vcenter=vcenter,
                moref=moref,
                name=name,
                collected_at=_now(),
                source=source,
                connection_state=data.get("connection_state"),
                in_maintenance_mode=data.get("in_maintenance_mode"),
                power_state=data.get("power_state"),
                cpu_usage_mhz=data.get("cpu_usage_mhz"),
                memory_usage_mb=data.get("memory_usage_mb"),
                uptime_seconds=data.get("uptime_seconds"),
                overall_status=data.get("overall_status"),
            )
        )

    # --- retention ------------------------------------------------------

    def _prune_metrics(self, settings) -> None:
        """Delete time-series rows older than the retention window. Bounded + simple."""
        days = getattr(settings, "vsphere_metrics_retention_days", 30)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 30
        if days <= 0:
            return
        cutoff = _now() - timedelta(days=days)
        with get_session() as session:
            for model in (VsphereVmMetric, VsphereHostMetric):
                session.query(model).filter(model.collected_at < cutoff).delete(
                    synchronize_session=False
                )
            session.commit()


# Perf-history field names that ride on VM items when QueryPerf is enabled.
_PERF_FIELDS = {
    "perf_cpu_usage_pct_avg",
    "perf_cpu_ready_ms_sum",
    "perf_mem_usage_pct_avg",
    "perf_disk_read_kbps_avg",
    "perf_disk_write_kbps_avg",
    "perf_net_rx_kbps_avg",
    "perf_net_tx_kbps_avg",
    "perf_samples",
    "perf_interval_id",
}


def _coerce_bool(val):
    """vCenter maintenanceMode is the string "normal"/"inMaintenance"; coerce to bool."""
    if isinstance(val, bool) or val is None:
        return val
    if isinstance(val, str):
        return val.strip().lower() not in ("", "normal", "false", "no", "0")
    return bool(val)
