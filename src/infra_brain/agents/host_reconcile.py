"""HostReconcileAgent — cross-source canonical host identity reconciliation.

Runs every 30 minutes (scheduler domain: "host_reconcile"). Queries each
source domain table (Rapid7, vSphere, Octopus, Linux, Windows), normalizes
hostnames to their short form, and upserts a HostIdentity row per unique
short hostname with denormalized display fields so the dashboard can render
per-host posture without join-heavy queries.

It does NOT write identity EDGES. As of P5 (2026-08-12) the two IS_SAME_AS
emission passes are deleted and ``graph_phase3.resolve_entities`` is the sole
identity writer; see ``HostReconcileAgent``'s docstring for what replaced each
confidence tier and why deletion beat re-anchoring onto ``graph_edges``.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from infra_brain.agents.base import BaseAgent, CollectionResult
from infra_brain.db.models import (
    AnsibleInventoryHost,
    CloudResource,
    CollectionRun,
    DriftEvent,
    HostIdentity,
    HostPurposeMap,
    K8sNode,
    NetDiscoveryHost,
    OctopusMachine,
    ProposedAction,
    R7Asset,
    Resource,
    VsphereVm,
    WindowsPatchState,
)
from infra_brain.db.models.graph import GraphNodeType
from infra_brain.db.session import get_session
from infra_brain.etl.base import (
    CollectorSkipped,
    ReconcileScope,
    ScrubbedErrorList,
    count_drift_events_for_run,
    scrub_dsn,
)
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.graph_phase2 import vsphere_key
from infra_brain.graph_phase3 import (
    REVIEW_ACTION_TYPE,
    REVIEW_LIVE_STATUSES,
)
from infra_brain.tools.hostmatch import (
    host_domain,
    normalize_host,
)

logger = logging.getLogger(__name__)

# GitLab #163 defect 3: the flat ``identity_conflict`` drift_type conflated two
# structurally different situations, which made them impossible to triage (or
# bulk-resolve) separately. They are now distinct drift types:
#
#   suffix_variant  — the two colliding rows carry DIFFERENT hostnames whose DNS
#                     domain suffixes differ (``web01`` vs ``web01.corp.example.com``,
#                     or ``.corp`` vs ``.dmz``). Almost always one machine spelled
#                     two ways; resolvable by picking a canonical spelling.
#   distinct_object — the two rows agree on the guest hostname but are genuinely
#                     DIFFERENT source objects (different vCenter moref/uuid):
#                     clones, DR replicas, snapshot restores. Resolvable only by
#                     deciding which object is authoritative.
#
# Anything that fits neither shape keeps the original flat ``identity_conflict``
# value — an unclassified collision is not silently forced into a bucket, and
# every pre-existing open row keeps matching its own drift_type.
_CONFLICT_CLASS_SUFFIX_VARIANT = "suffix_variant"
_CONFLICT_CLASS_DISTINCT_OBJECT = "distinct_object"
_CONFLICT_CLASS_UNCLASSIFIED = "unclassified"
_CONFLICT_CLASS_DRIFT_TYPES = {
    _CONFLICT_CLASS_SUFFIX_VARIANT: "identity_conflict_suffix_variant",
    _CONFLICT_CLASS_DISTINCT_OBJECT: "identity_conflict_distinct_object",
    _CONFLICT_CLASS_UNCLASSIFIED: "identity_conflict",
}


def _NOW() -> datetime:
    return datetime.now(UTC)


class HostReconcileAgent(BaseAgent):
    """Reconcile cross-source host data into the host_identities table.

    NOT AN IDENTITY WRITER (P5, 2026-08-12). This agent used to emit
    ``IS_SAME_AS`` into ``resource_relationships`` from two passes —
    ``_emit_is_same_as_edges`` (0.95 hostname convergence / 0.75 guarded bare-IP
    netdiscovery attach) and ``_emit_cross_hostname_ip_edges`` (0.70 TRK-062
    cross-hostname IP correlation). Both are DELETED, and deliberately not
    re-anchored onto ``graph_edges``: re-anchoring would have made a second
    writer of the resolver-owned ``SAME_AS`` type, with different thresholds,
    different confidence tiers and a different authority model — the
    two-writers-one-truth problem the graph-first architecture exists to remove.
    ``graph_phase3.resolve_entities`` is the sole identity writer.

    Deleting was only safe once the resolver asserted-or-QUEUED every pair this
    agent would have asserted, on every leg that can carry data here. That is
    P5's GAP (a) (``LinuxHost`` and ``NetDiscoveredHost`` added to
    ``graph_phase3.HOST_NODE_TYPES``, declared via ``NodeSpec.is_host_identity``)
    and GAP (b) (a shared IP with no name support is floored into the REVIEW
    BAND rather than dropped, which is where the 0.70 tier's capability went —
    to a question, not a quieter assertion). The proof is
    ``tests/test_p5_issameas_resolver_coverage.py``. Live legacy ``IS_SAME_AS``
    row count at deletion: ZERO — single-source estate, the passes never fired —
    so no data migration was needed.

    WHAT IT STILL DOES, all of which FEEDS the resolver rather than competing
    with it: builds ``HostIdentity`` rows (the cross-source merge itself),
    persists ``identity_ambiguous_sources`` (same-source collisions, which
    ``graph_phase3.ambiguous_leg_index`` reads back as counter-evidence), and
    emits IP/identity conflict ``DriftEvent``s. It reads the resolver's review
    queue (``_same_as_review_exists``) so it does not raise a conflict a human
    is already being asked about.
    """

    spec = AgentSpec(
        domain="host_reconcile",
        tier=Tier.RECONCILER,
        schedule="*/30 * * * *",
        max_staleness=timedelta(hours=2),
        skip_hook=True,
    )

    # Per-source resource-id columns on the merged-host dict, in descending
    # trust/priority order. This is the LEG VOCABULARY: what counts as a source
    # for a merged host, and which one is the representative "anchor" resource.
    # Both IS_SAME_AS emitters that used to share it are deleted (P5); the order
    # is still load-bearing for ``agents/eol.py``'s anchor choice, and the set is
    # what ``graph_phase3._HOST_IDENTITY_LEG_COLUMNS`` derives itself from and
    # what tests/test_p5_issameas_resolver_coverage.py measures resolver
    # coverage against, one leg at a time.
    _SOURCE_KEYS = (
        ("r7_resource_id", "r7"),
        ("vsphere_resource_id", "vsphere"),
        ("octopus_resource_id", "octopus"),
        ("linux_resource_id", "linux"),
        ("windows_resource_id", "windows"),
        ("net_resource_id", "net"),
        ("cloud_resource_id", "cloud"),
        ("k8s_resource_id", "k8s"),
        ("netdevice_resource_id", "netdevice"),
    )

    # P5 (2026-08-12): the three IS_SAME_AS confidence tiers that lived here
    # (_CONF_HOSTNAME 0.95 / _CONF_NET_IP_ATTACH 0.75 / _CONF_CROSS_HOSTNAME_IP
    # 0.70) are DELETED with their emitters. Their successors are the resolver's
    # own, in graph_phase3, and the mapping is deliberate rather than a rename:
    #
    #   0.95 hostname convergence -> CONFIDENCE_DETERMINISTIC_NAME (0.990),
    #        emitted by the deterministic pass on the same normalize_host key.
    #   0.70 cross-hostname shared IP -> NOT an edge at any confidence. It is
    #        floored into the REVIEW BAND (FUZZY_REVIEW_MIN) and becomes a
    #        question for a human. Relocating an assertion to a question is the
    #        whole point: a spoofable, reused address never justified asserting
    #        identity, only asking about it.
    #   0.75 bare-IP netdiscovery attach -> covered only where the netdiscovery
    #        row carries a hostname (NetDiscoveredHost is keyed on it). A
    #        hostname-less probe has an address and no identity, so it gets no
    #        node — see agents/netdiscovery.py's NodeSpec for why absence beats
    #        a churn-following pseudo-entity.

    # HostReconcileAgent does not collect generic Resources — it reads existing
    # domain tables and writes HostIdentity rows. collect() is defined here to
    # satisfy the ABC but is not called; run() is fully overridden.
    def collect(self, scope: str = "all") -> list[dict]:  # type: ignore[override]
        return []

    def run(  # type: ignore[override]
        self,
        trigger_type: str = "scheduled",
        scope: str = "all",
        sweep_id: uuid.UUID | None = None,
    ) -> CollectionResult:
        run_id = uuid.uuid4()
        # M-1/SEC-2: DSN-scrubs on append, so no future append site can leak
        # credentials into CollectionResult.errors / CollectionRun.error_message.
        errors: list[str] = ScrubbedErrorList()
        n_new = 0
        n_updated = 0
        # M-2 (F-007): _upsert_identities / _emit_ip_conflict_events /
        # _emit_identity_conflict_events each isolate a per-item write in its
        # own SAVEPOINT (TRK-132) and, on failure, log + skip rather than
        # raise — one bad host/conflict must not roll back every sibling
        # write for the run. That per-item resilience used to be invisible:
        # the swallowed exception never reached this method, so a run that
        # dropped EVERY item still finalized status="completed" (below) and
        # was never revisited. Each helper stashes its own per-item failures
        # here; see the block after the try/except for how they are folded
        # into the run's error-reporting path.
        self._identity_write_errors: list[str] = []
        self._ip_conflict_write_errors: list[str] = []
        self._identity_conflict_write_errors: list[str] = []

        # Open a CollectionRun record — mirrors the BaseAgent.run() pattern.
        with get_session() as session:
            run = CollectionRun(
                id=run_id,
                domain=self.domain,
                trigger_type=trigger_type,
                trigger_source=scope,
                status="in_progress",
                sweep_id=sweep_id,
            )
            session.add(run)
            session.commit()

        # M-1/TRK-106: the base run()'s finalize-in-a-finally does not apply to
        # an override, so wrap the whole body in the shared guard — a
        # BaseException (KeyboardInterrupt/SystemExit on scheduler shutdown)
        # would otherwise strand this row at status="in_progress" forever.
        with self.run_row_guard(run_id):
            try:
                # AA-R-11/12: honour collection_disabled_domains BEFORE doing any
                # work — every ETLConnector.run()-standard collector respects this
                # maintenance-pause knob (F-022); a run()-override must not
                # silently skip it.
                _disabled = {
                    d.strip()
                    for d in (self.settings.collection_disabled_domains or "").split(",")
                    if d.strip()
                }
                if self.domain in _disabled:
                    raise CollectorSkipped(
                        f"domain '{self.domain}' is in collection_disabled_domains"
                    )

                # F-004.4: every phase runs under the shared collect timeout guard —
                # a hang becomes status="failed" instead of wedging the scheduler.
                merged, ip_conflicts, identity_conflicts = self._call_with_timeout(
                    self._build_merged_hosts
                )
                n_new, n_updated = self._call_with_timeout(self._upsert_identities, merged)
                # GitLab #148/#149: this count is HostIdentity upserts into
                # host_identities — real, addressable detail rows, but NOT
                # generic Resource rows (query_resources(domain=
                # "host_reconcile") is empty by design; this agent is a
                # reconciler, not a Resource collector). Report it as
                # detail_rows_written too so the count traces to a queryable
                # table instead of being a provenance dead-end.
                # resources_found is kept for backward compatibility with
                # existing dashboards/trends.
                self._finalize_run(
                    run_id,
                    status="completed",
                    resources_found=n_new + n_updated,
                    detail_rows_written=n_new + n_updated,
                )
                # P5 (2026-08-12): the two IS_SAME_AS emission passes that used
                # to run here are GONE. This agent is no longer an identity
                # WRITER; graph_phase3.resolve_entities is the sole one. What it
                # still does — build HostIdentity rows, persist ambiguity, emit
                # conflict DriftEvents — all FEEDS that resolver. See the class
                # docstring for why deleting beat re-anchoring.
                self._call_with_timeout(self._emit_ip_conflict_events, ip_conflicts, run_id)
                # TRK-304 / GitLab #158: surface same-source identity collisions
                # (ambiguous candidates) instead of letting first-write-wins
                # silently settle them.
                self._call_with_timeout(
                    self._emit_identity_conflict_events, identity_conflicts, run_id
                )
            except CollectorSkipped as exc:
                # Same "skipped" contract as ETLConnector.run() (F-022): distinct
                # from "completed" (ran, found nothing) and "failed" (runtime error).
                reason = str(exc) or "unconfigured"
                logger.info("HostReconcileAgent.run skipped reason=%r", reason)
                # M-1/SEC-2: _finalize_run scrubs the message before it is
                # persisted, so a DSN-bearing exception cannot leak credentials
                # into the dashboard-readable error_message.
                self._finalize_run(run_id, status="skipped", error_message=reason)
                return CollectionResult(
                    run_id=run_id,
                    domain=self.domain,
                    resources_found=0,
                    drift_count=0,
                    status="skipped",
                    errors=errors,
                )
            except Exception as exc:
                logger.exception("HostReconcileAgent.run failed")
                # M-1/SEC-2: scrub the returned CollectionResult.errors entry too
                # — it is surfaced by dispatch() and the sweep summary, not just
                # persisted.
                scrubbed = scrub_dsn(str(exc))
                errors.append(scrubbed)
                self._finalize_run(run_id, status="failed", error_message=scrubbed)

            # AA-C-2: _emit_ip_conflict_events now stamps collection_run_id on every
            # DriftEvent it writes, so drift_count can reflect real IP-conflict
            # events instead of being hardcoded to 0.
            #
            # GitLab #163 defect 2: this count used to be computed here and returned
            # ONLY in the in-memory CollectionResult — it was never written back to
            # the CollectionRun row, which defaults to drift_count=0. Every
            # DB-backed consumer (health monitoring, the dashboard's per-domain drift
            # column, sweep-health) therefore read 0 for host_reconcile forever, no
            # matter how much drift the run actually wrote. The recompute-and-persist
            # happens HERE rather than inside the try-block because every DriftEvent
            # writer (_emit_ip_conflict_events / _emit_identity_conflict_events) has
            # finished by now on both the success AND the exception path, so a failed
            # run still reports the drift it managed to record. Mirrors the
            # recompute-after-detail-writes pattern in etl/base.py.
            with get_session() as session:
                drift_count = count_drift_events_for_run(run_id, session)
                run = session.get(CollectionRun, run_id)
                if run is not None:
                    run.drift_count = drift_count
                session.commit()

            result = CollectionResult(
                run_id=run_id,
                domain=self.domain,
                resources_found=n_new + n_updated,
                drift_count=drift_count,
                status="failed" if errors else "completed",
                errors=list(errors),
                detail_rows_written=n_new + n_updated,
            )
            # M-2 (F-007): fold every per-item SAVEPOINT skip from
            # _upsert_identities / _emit_ip_conflict_events /
            # _emit_identity_conflict_events into the SAME completed->partial
            # downgrade path collect()-phase collectors use
            # (ETLConnector._record_partial_errors) — never a second, bespoke
            # signalling channel. When the top-level try already failed
            # (result.status == "failed" above), this only appends the extra
            # detail to result.errors/CollectionRun.error_message; it never
            # un-fails a run (the helper only ever downgrades "completed").
            skip_errors = [
                *self._identity_write_errors,
                *self._ip_conflict_write_errors,
                *self._identity_conflict_write_errors,
            ]
            self._record_partial_errors(result, skip_errors)

            logger.info(
                "HostReconcileAgent: reconciled=%d new=%d updated=%d drift=%d status=%s",
                n_new + n_updated,
                n_new,
                n_updated,
                drift_count,
                result.status,
            )
            return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_merged_hosts(self) -> tuple[dict[str, dict], list[dict], list[dict]]:
        """Query every source table and return a merged host dict, IP conflicts,
        and identity (same-source) conflicts.

        Returns:
            merged: dict keyed by short_hostname with fields for HostIdentity.
            ip_conflicts: list of conflict dicts describing IP address disagreements
                          between sources for the same hostname.
            identity_conflicts: list of conflict dicts describing TWO ROWS FROM
                          THE SAME SOURCE TABLE normalizing to the same
                          short_hostname (TRK-304 / GitLab #158). Cross-source
                          convergence via normalize_host() is intentional and
                          NOT a conflict — only a same-source collision is.

        Sources are merged left-to-right; later sources add missing fields
        without overwriting already-populated ones.  When a new source reports
        a different primary IP than the first source, an IP conflict is recorded.
        When a SECOND row from the same source maps to a short_hostname that
        already has a *_resource_id from that same source, the field value is
        left as first-write-wins (no guessing which candidate is "right") but
        the collision is recorded into identity_conflicts and the record is
        flagged via ``identity_ambiguous_sources`` instead of being silently
        dropped — see _note_identity_collision.
        """
        hosts: dict[str, dict] = {}
        ip_conflicts: list[dict] = []
        identity_conflicts: list[dict] = []

        with get_session() as session:
            # --- Rapid7 assets ---
            for asset in session.query(R7Asset).all():
                short = normalize_host(asset.hostname or "")
                if not short:
                    continue
                hosts.setdefault(
                    short,
                    {
                        "short_hostname": short,
                        "fqdn": asset.hostname,
                        "ip_addresses": [asset.ip] if asset.ip else [],
                        "primary_ip": asset.ip,
                        "primary_ip_source": "r7",
                        "r7_resource_id": asset.resource_id,
                        "os_family": asset.os_family,
                        "risk_score": int(asset.risk_score)
                        if asset.risk_score is not None
                        else None,
                        "vuln_count": asset.vuln_total,
                    },
                )
                rec = hosts[short]
                # TRK-087: keep the original (pre-normalize) name so the edge
                # step can compare domains and block cross-domain false merges.
                rec.setdefault("r7_hostname", asset.hostname or "")
                if rec.get("r7_resource_id") is None:
                    rec["r7_resource_id"] = asset.resource_id
                elif rec["r7_resource_id"] != asset.resource_id:
                    self._note_identity_collision(
                        identity_conflicts,
                        rec,
                        "r7",
                        "r7_resource_id",
                        asset.resource_id,
                        rec.get("r7_hostname", ""),
                        asset.hostname or "",
                    )
                if rec.get("os_family") is None:
                    rec["os_family"] = asset.os_family
                if rec.get("risk_score") is None and asset.risk_score is not None:
                    rec["risk_score"] = int(asset.risk_score)
                if rec.get("vuln_count") is None:
                    rec["vuln_count"] = asset.vuln_total
                if asset.ip and asset.ip not in rec["ip_addresses"]:
                    rec["ip_addresses"].append(asset.ip)

            # --- vSphere VMs ---
            for vm in session.query(VsphereVm).all():
                short = normalize_host(vm.guest_hostname or vm.name or "")
                if not short:
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("vsphere_hostname", vm.guest_hostname or vm.name or "")
                # GitLab #163 defect 3: remember WHICH vCenter object won the
                # first-write-wins race, not just its resource_id. Two VMs can
                # report the same guest hostname while being genuinely distinct
                # objects (clone / DR replica / snapshot restore) — only
                # (vcenter, moref) and uuid can tell those apart, and they are
                # also the natural keys graph_phase3's review queue is keyed on.
                vm_object_ids = {"vcenter": vm.vcenter, "moref": vm.moref, "uuid": vm.uuid}
                if rec.get("vsphere_resource_id") is None:
                    rec["vsphere_resource_id"] = vm.resource_id
                    rec["vsphere_object_ids"] = vm_object_ids
                elif rec["vsphere_resource_id"] != vm.resource_id:
                    self._note_identity_collision(
                        identity_conflicts,
                        rec,
                        "vsphere",
                        "vsphere_resource_id",
                        vm.resource_id,
                        rec.get("vsphere_hostname", ""),
                        vm.guest_hostname or vm.name or "",
                        kept_object_ids=rec.get("vsphere_object_ids"),
                        dropped_object_ids=vm_object_ids,
                    )
                if rec.get("vsphere_power_state") is None:
                    rec["vsphere_power_state"] = vm.power_state
                if rec.get("fqdn") is None and vm.guest_hostname:
                    rec["fqdn"] = vm.guest_hostname
                if vm.ip_address:
                    existing_primary = rec.get("primary_ip")
                    if (
                        existing_primary
                        and vm.ip_address != existing_primary
                        and vm.ip_address not in rec["ip_addresses"]
                    ):
                        # New source reports a primary IP that differs from the
                        # first-seen primary IP — record the conflict.
                        ip_conflicts.append(
                            {
                                "short_hostname": short,
                                "resource_id": rec.get("r7_resource_id")
                                or rec.get("vsphere_resource_id"),
                                "existing_ip": existing_primary,
                                "new_ip": vm.ip_address,
                                "existing_source": rec.get("primary_ip_source", "unknown"),
                                "new_source": "vsphere",
                            }
                        )
                        logger.warning(
                            "IP conflict for %s: %s says %s, vsphere says %s",
                            short,
                            rec.get("primary_ip_source", "unknown"),
                            existing_primary,
                            vm.ip_address,
                        )
                        # TRK-138: vSphere wins IP-precedence disputes. The
                        # guest-tools-reported IP is live/current; Rapid7 (or
                        # any earlier source)'s primary_ip is scan-time-stale
                        # by the time a conflict is observed. The conflict
                        # itself is still recorded/logged above unchanged —
                        # this only decides which IP becomes canonical.
                        # (Default could be revisited if a source proves more
                        # current than vSphere in practice; not configurable.)
                        rec["primary_ip"] = vm.ip_address
                        rec["primary_ip_source"] = "vsphere"
                    if vm.ip_address not in rec["ip_addresses"]:
                        rec["ip_addresses"].append(vm.ip_address)

            # --- Octopus machines ---
            for machine in session.query(OctopusMachine).all():
                short = normalize_host(machine.name or "")
                if not short:
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("octopus_hostname", machine.name or "")
                if rec.get("octopus_resource_id") is None:
                    rec["octopus_resource_id"] = machine.resource_id
                elif rec["octopus_resource_id"] != machine.resource_id:
                    self._note_identity_collision(
                        identity_conflicts,
                        rec,
                        "octopus",
                        "octopus_resource_id",
                        machine.resource_id,
                        rec.get("octopus_hostname", ""),
                        machine.name or "",
                    )
                if rec.get("octopus_machine_status") is None:
                    rec["octopus_machine_status"] = machine.status

            # --- Linux hosts ---
            # TRK-187: source the linux leg from the canonical Resource rows
            # (domain="linux"), NOT from the LinuxHost detail table. The detail
            # row is only written for hosts that answered the most recent fact
            # gather AND whose detail-write phase succeeded — a linux Resource
            # without its LinuxHost row previously never populated
            # linux_resource_id at all (get_host_profile's advertised linux leg
            # was structurally null). LinuxHost adds nothing the leg needs
            # (both carry the same resource_id); retired Resources are excluded
            # so a decommissioned host's leg is not resurrected.
            for res in (
                session.query(Resource)
                .filter(Resource.domain == "linux", Resource.retired_at.is_(None))
                .all()
            ):
                short = normalize_host(res.name or "")
                if not short:
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("linux_hostname", res.name or "")
                if rec.get("linux_resource_id") is None:
                    rec["linux_resource_id"] = res.id
                elif rec["linux_resource_id"] != res.id:
                    self._note_identity_collision(
                        identity_conflicts,
                        rec,
                        "linux",
                        "linux_resource_id",
                        res.id,
                        rec.get("linux_hostname", ""),
                        res.name or "",
                    )

            # --- Windows hosts ---
            # TRK-187: same fix as linux above — the windows leg comes from the
            # canonical Resource rows (domain="windows"), so a windows Resource
            # is represented even when its WindowsPatchState detail row is
            # missing (WinRM skipped/unreachable, detail-write failure). The
            # WindowsPatchState pass below is kept for patch_status enrichment.
            for res in (
                session.query(Resource)
                .filter(Resource.domain == "windows", Resource.retired_at.is_(None))
                .all()
            ):
                short = normalize_host(res.name or "")
                if not short:
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("windows_hostname", res.name or "")
                if rec.get("windows_resource_id") is None:
                    rec["windows_resource_id"] = res.id
                elif rec["windows_resource_id"] != res.id:
                    self._note_identity_collision(
                        identity_conflicts,
                        rec,
                        "windows",
                        "windows_resource_id",
                        res.id,
                        rec.get("windows_hostname", ""),
                        res.name or "",
                    )

            # --- Windows patch state (enrichment; leg fallback) ---
            # NOTE (TRK-304): this loop deliberately does NOT run same-source
            # collision detection on windows_resource_id — it is a FALLBACK
            # enrichment pass filling the leg only when the windows Resource
            # loop above left it unset (WinRM-unreachable hosts that still
            # have a WindowsPatchState row). A collision here would conflate
            # "two different tables disagree" with "the same table reported
            # two rows", which is a different (and already out-of-scope)
            # signal. If two WindowsPatchState rows themselves collide (rare:
            # both mapping to the same short_hostname with no Resource(windows)
            # leg at all), first-write-wins still applies exactly as before —
            # this fallback path is not part of the 2026-07-30 fix's covered
            # source set.
            for wps in session.query(WindowsPatchState).all():
                short = normalize_host(wps.hostname or "")
                if not short:
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("windows_hostname", wps.hostname or "")
                if rec.get("windows_resource_id") is None:
                    rec["windows_resource_id"] = wps.resource_id
                if rec.get("patch_status") is None:
                    # Derive a simple patch_status label from pending_count + winrm_status
                    if wps.winrm_status not in ("ok", "unknown"):
                        rec["patch_status"] = f"winrm_{wps.winrm_status}"
                    elif wps.pending_count > 0:
                        rec["patch_status"] = "pending"
                    else:
                        rec["patch_status"] = "current"

            # --- NetDiscovery hosts (KG-6) ---
            # NetDiscoveryHost carries a (possibly-null) hostname and an ip. We
            # merge on the normalized hostname when present; a bare-IP host that
            # never resolved a name is NOT merged here on the hostname key.
            # KG-9 / TRK-102: bare-IP netdiscovery hosts (the majority of the
            # ~2,699 rows) are handled in a *separate*, deliberately conservative
            # IP-attach pass below — see _attach_bare_ip_netdiscovery.
            bare_ip_nds: list[tuple[uuid.UUID | None, str]] = []
            for nd in session.query(NetDiscoveryHost).all():
                short = normalize_host(nd.hostname or "")
                if not short:
                    # KG-9 / TRK-102: defer hostname-less (bare-IP) hosts to the
                    # guarded IP-attach pass instead of dropping them entirely.
                    if nd.ip:
                        bare_ip_nds.append((nd.resource_id, nd.ip))
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("net_hostname", nd.hostname or "")
                if rec.get("net_resource_id") is None:
                    rec["net_resource_id"] = nd.resource_id
                    # This net source was matched by hostname (the safe path).
                    rec["net_match_basis"] = "hostname"
                elif rec["net_resource_id"] != nd.resource_id:
                    self._note_identity_collision(
                        identity_conflicts,
                        rec,
                        "net",
                        "net_resource_id",
                        nd.resource_id,
                        rec.get("net_hostname", ""),
                        nd.hostname or "",
                    )
                if nd.ip and nd.ip not in rec["ip_addresses"]:
                    rec["ip_addresses"].append(nd.ip)

            # --- Cloud resources (KG-6) ---
            for cr in session.query(CloudResource).all():
                short = normalize_host(cr.name or "")
                if not short:
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("cloud_hostname", cr.name or "")
                if rec.get("cloud_resource_id") is None:
                    rec["cloud_resource_id"] = cr.resource_id
                elif rec["cloud_resource_id"] != cr.resource_id:
                    self._note_identity_collision(
                        identity_conflicts,
                        rec,
                        "cloud",
                        "cloud_resource_id",
                        cr.resource_id,
                        rec.get("cloud_hostname", ""),
                        cr.name or "",
                    )

            # --- Kubernetes nodes (KG-6) ---
            for kn in session.query(K8sNode).all():
                short = normalize_host(kn.name or "")
                if not short:
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("k8s_hostname", kn.name or "")
                if rec.get("k8s_resource_id") is None:
                    rec["k8s_resource_id"] = kn.resource_id
                elif rec["k8s_resource_id"] != kn.resource_id:
                    self._note_identity_collision(
                        identity_conflicts,
                        rec,
                        "k8s",
                        "k8s_resource_id",
                        kn.resource_id,
                        rec.get("k8s_hostname", ""),
                        kn.name or "",
                    )

            # --- Net devices (KG-4) ---
            # NetDevice is SNMP-discovered switch/router/network-gear
            # inventory (net_devices table) — distinct from NetDiscoveryHost
            # (nmap-scanned endpoint hosts, already merged above). Confirmed
            # during the 2026-07-23 KG audit as the only cloud/net-ish
            # domain table omitted from this reconciliation.
            from infra_brain.db.models import NetDevice

            for ndv in session.query(NetDevice).all():
                short = normalize_host(ndv.sysname or ndv.name or "")
                if not short:
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("netdevice_hostname", ndv.sysname or ndv.name or "")
                if rec.get("netdevice_resource_id") is None:
                    rec["netdevice_resource_id"] = ndv.resource_id
                elif rec["netdevice_resource_id"] != ndv.resource_id:
                    self._note_identity_collision(
                        identity_conflicts,
                        rec,
                        "netdevice",
                        "netdevice_resource_id",
                        ndv.resource_id,
                        rec.get("netdevice_hostname", ""),
                        ndv.sysname or ndv.name or "",
                    )
                if ndv.ip and ndv.ip not in rec["ip_addresses"]:
                    rec["ip_addresses"].append(ndv.ip)

            # --- Ansible inventory + host_purpose_map seeding (TRK-187) ---
            # Neither table carries a Resource FK, so they contribute NO
            # *_resource_id leg — but a host present in the curated Ansible
            # inventory or the host_purpose_map is a real, known machine and
            # must have a HostIdentity row even when no collector has reached
            # it yet (the SITEB-SRV-02 case: present in BOTH tables, zero
            # host_identities row, get_host_profile → "not found"). Seeding
            # here guarantees the row exists; collector legs merge onto it via
            # the shared normalize_host() key as they appear. Seed-only rows
            # never emit IS_SAME_AS edges (they carry <2 source legs) and never
            # enter IP correlation (no ip_addresses).
            for inv in session.query(AnsibleInventoryHost).all():
                short = normalize_host(inv.name or "")
                if not short:
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("ansible_inventory_hostname", inv.name or "")
                if rec.get("fqdn") is None and "." in (inv.name or ""):
                    rec["fqdn"] = inv.name

            for hp in session.query(HostPurposeMap).all():
                short = normalize_host(hp.hostname or "")
                if not short:
                    continue
                rec = hosts.setdefault(short, {"short_hostname": short, "ip_addresses": []})
                rec.setdefault("purpose_map_hostname", hp.hostname or "")

            # --- Guarded bare-IP netdiscovery attach (KG-9 / TRK-102) ---
            # Close the netdiscovery gap: the majority of NetDiscoveryHost rows
            # are hostname-less ping-sweep discoveries that the hostname key can
            # never unify. We attach each such host to an EXISTING merged host
            # ONLY when its IP UNIQUELY and UNAMBIGUOUSLY matches exactly one
            # merged host's known authoritative IPs. Zero or >1 matches → skip.
            # This is the explicit guard against the TRK-087 false-IP-merge class
            # (1,676 legacy bad edges): we never merge on an ambiguous IP.
            self._attach_bare_ip_netdiscovery(hosts, bare_ip_nds)

        return hosts, ip_conflicts, identity_conflicts

    @staticmethod
    def _coalesce_resource_id(data: dict, existing: "HostIdentity", field: str):
        """Return data[field], or None if it is absent/None/empty -- clearing
        the leg on any falsy value rather than falling back to `existing`.

        KG-8: ``data`` is this run's full, current merge (``_build_merged_hosts``
        queries every source table fresh every call; `run()`'s `_call_with_timeout`
        guard means `_upsert_identities` is only ever reached after that merge
        completed in full -- a partial/timed-out merge aborts the whole run before
        this point, so an absent field here is never a partial-scan artifact). A
        field missing/None/empty means the current merge found no matching row for
        that source RIGHT NOW -- e.g. the vSphere VM was deleted, or the source
        Resource was retired (`_build_merged_hosts` deliberately excludes retired
        Resources: "so a decommissioned host's leg is not resurrected"). Previously
        this fell back to `existing.field`, silently re-adding the exact stale FK
        that exclusion was written to prevent -- a leg, once set, could never clear
        again. Recomputing the leg fully from the current merge every run (instead
        of accumulating stale state) is the same "trust this run's full scan, don't
        carry forward what it didn't find" principle the retired-Resource exclusion
        above already applies -- this just closes the gap where the update path
        silently undid it.
        """
        return data.get(field) or None

    @staticmethod
    def _attach_bare_ip_netdiscovery(
        hosts: dict[str, dict],
        bare_ip_nds: list[tuple[uuid.UUID | None, str]],
    ) -> None:
        """Attach hostname-less netdiscovery hosts to merged hosts by unique IP.

        KG-9 / TRK-102. Builds an ip -> [short_hostname, ...] index from the
        authoritative IPs already collected on each merged host (R7Asset.ip,
        VsphereVm.ip_address, and hostname-matched NetDiscoveryHost.ip — the
        IPs accumulated in ``rec['ip_addresses']``). A bare-IP netdiscovery
        host is attached ONLY when its IP maps to exactly one merged host that
        does not already carry a net_resource_id. Ambiguous IPs (mapping to two
        or more merged hosts) are skipped so no false merge is ever created.
        """
        if not bare_ip_nds:
            return

        # Build ip -> set(short_hostname) index from merged-host authoritative IPs.
        ip_index: dict[str, set[str]] = {}
        for short, rec in hosts.items():
            for ip in rec.get("ip_addresses", []) or []:
                if ip:
                    ip_index.setdefault(ip, set()).add(short)

        for net_rid, ip in bare_ip_nds:
            matches = ip_index.get(ip)
            # Guard: only a UNIQUE (exactly one) match is safe to merge.
            if not matches or len(matches) != 1:
                continue  # zero or ambiguous (>1) → never merge (TRK-087 guard)
            (short,) = tuple(matches)
            rec = hosts[short]
            # Do not overwrite a net source already matched by hostname.
            if rec.get("net_resource_id") is not None:
                continue
            rec["net_resource_id"] = net_rid
            # Flag the lower-trust basis so edge emission + future decay/audit
            # can distinguish IP-derived links from hostname-derived ones.
            rec["net_match_basis"] = "ip"

    @staticmethod
    def _classify_identity_collision(
        kept_hostname: str,
        dropped_hostname: str,
        kept_object_ids: dict | None,
        dropped_object_ids: dict | None,
    ) -> str:
        """Classify a same-source identity collision (GitLab #163 defect 3).

        Order matters. A DNS-suffix variant is checked FIRST because it is the
        cheaper, source-agnostic signal and is decidable from the hostnames
        alone: the two spellings genuinely differ AND their domain suffixes
        differ (``host_domain`` returns "" for an unqualified name, so the
        short<->FQDN pair ``web01`` / ``web01.corp.example.com`` classifies here
        too — that IS the suffix-variant case).

        Only if the hostnames do NOT distinguish the rows do we consult the
        per-source object identity (vCenter ``moref`` / ``uuid``). Two rows that
        agree on the guest hostname but disagree on moref/uuid are genuinely
        different vCenter objects — a clone or DR replica — not a spelling
        variant of one machine.

        Returns ``_CONFLICT_CLASS_UNCLASSIFIED`` when neither test fires (e.g. a
        source that carries no object identity and two identical hostnames);
        that keeps the original flat ``identity_conflict`` drift_type rather than
        guessing a bucket.
        """
        kept_norm = (kept_hostname or "").strip().lower().rstrip(".")
        dropped_norm = (dropped_hostname or "").strip().lower().rstrip(".")
        if (
            kept_norm
            and dropped_norm
            and kept_norm != dropped_norm
            and host_domain(kept_norm) != host_domain(dropped_norm)
        ):
            return _CONFLICT_CLASS_SUFFIX_VARIANT

        kept_ids = kept_object_ids or {}
        dropped_ids = dropped_object_ids or {}
        for key in ("moref", "uuid"):
            kept_val, dropped_val = kept_ids.get(key), dropped_ids.get(key)
            if kept_val and dropped_val and kept_val != dropped_val:
                return _CONFLICT_CLASS_DISTINCT_OBJECT

        return _CONFLICT_CLASS_UNCLASSIFIED

    @staticmethod
    def _note_identity_collision(
        identity_conflicts: list[dict],
        rec: dict,
        source: str,
        field: str,
        dropped_resource_id,
        kept_hostname: str,
        dropped_hostname: str,
        kept_object_ids: dict | None = None,
        dropped_object_ids: dict | None = None,
    ) -> None:
        """Record a same-source identity collision (TRK-304 / GitLab #158).

        Called when a SECOND row from the SAME source table normalizes to a
        short_hostname that already has ``field`` populated FROM THAT SAME
        SOURCE. Cross-source convergence (two different sources landing on
        the same short_hostname) is intentional and handled elsewhere — this
        is only for genuine same-source ambiguity, e.g. a live VM and a
        stale snapshot/clone both reporting the same guest hostname.

        The field value itself is left untouched (first-write-wins is kept
        for WHICH candidate is stored — this function does not try to guess
        a "better" one, mirroring the IP-conflict precedent). Instead the
        collision is appended to ``identity_conflicts`` for later DriftEvent
        persistence (see _emit_identity_conflict_events) and the record is
        flagged via ``identity_ambiguous_sources``.

        KG-2: that flag is now load-bearing rather than decorative. It is
        PERSISTED to ``host_identities.identity_ambiguous_sources`` by
        ``_upsert_identity_item`` (recomputed every run, cleared once the
        collision stops being observed), so downstream consumers — dashboard /
        ``get_host_profile`` readers — can genuinely tell this source's leg is
        unsettled, and it is read back by this agent's own emitters, which
        refuse to assert a 0.95 ``IS_SAME_AS`` over the coin-flip survivor of a
        collision. Before that, the flag lived only on the in-memory merged dict
        and died when ``run()`` returned, so this docstring's promise of
        downstream visibility was false as written.
        """
        conflict_class = HostReconcileAgent._classify_identity_collision(
            kept_hostname, dropped_hostname, kept_object_ids, dropped_object_ids
        )
        identity_conflicts.append(
            {
                "short_hostname": rec["short_hostname"],
                "source": source,
                "field": field,
                "kept_resource_id": rec.get(field),
                "dropped_resource_id": dropped_resource_id,
                "kept_hostname": kept_hostname,
                "dropped_hostname": dropped_hostname,
                # GitLab #163 defect 3: carried through to the DriftEvent so the
                # two structurally different collision shapes get distinct
                # drift_type values (and are therefore separately triageable).
                "conflict_class": conflict_class,
                "kept_object_ids": kept_object_ids or {},
                "dropped_object_ids": dropped_object_ids or {},
            }
        )
        ambiguous = rec.setdefault("identity_ambiguous_sources", [])
        if source not in ambiguous:
            ambiguous.append(source)
        logger.warning(
            "TRK-304: same-source identity collision for %r — source=%s class=%s "
            "kept_resource_id=%s dropped_resource_id=%s",
            rec["short_hostname"],
            source,
            conflict_class,
            rec.get(field),
            dropped_resource_id,
        )

    def _emit_identity_conflict_events(
        self, identity_conflicts: list[dict], run_id: uuid.UUID | None = None
    ) -> None:
        """Write a DriftEvent for each same-source identity collision detected
        during reconciliation (TRK-304 / GitLab #158). Mirrors
        _emit_ip_conflict_events exactly, including the per-item SAVEPOINT
        isolation (TRK-132) so one bad conflict dict can't roll back every
        other conflict's DriftEvent in the same run.
        """
        if not identity_conflicts:
            return
        # M-2 (F-007): the per-item SAVEPOINT below logs+skips a bad conflict
        # dict (TRK-132) so it can't roll back its siblings — but a skip must
        # not be invisible. Stash the failures on self._identity_conflict_write_errors
        # so run() can fold them into the run's errors/status (see run()'s use
        # of ETLConnector._record_partial_errors).
        scope = ReconcileScope(label="identity_conflict")
        with get_session() as session:
            for conflict in identity_conflicts:
                key = conflict.get("short_hostname") or "unknown"
                try:
                    with session.begin_nested():
                        self._upsert_identity_conflict_event(session, conflict, run_id)
                    scope.observed(key)
                except Exception as exc:
                    logger.warning(
                        "HostReconcileAgent: skipping bad identity_conflict item %r: %s",
                        conflict.get("short_hostname"),
                        exc,
                    )
                    scope.failed(key, exc)
            session.commit()
        self._identity_conflict_write_errors = scope.errors
        logger.info(
            "HostReconcileAgent: recorded %d identity conflict event(s)%s",
            scope.observed_count,
            f" ({scope.failed_count} skipped)" if scope.has_failures else "",
        )

    def _upsert_identity_conflict_event(
        self, session, conflict: dict, run_id: uuid.UUID | None
    ) -> None:
        """Write (or refresh) a single identity_conflict/<source>_resource_id
        DriftEvent.

        Anchored on the KEPT resource_id (the one still stored on the merged
        record's field — i.e. the first-write-wins survivor), so the conflict
        is discoverable from that resource just like an IP conflict is
        anchored on its host's resource_id. Split out of
        _emit_identity_conflict_events so it can be wrapped in a per-item
        SAVEPOINT (TRK-132), mirroring _upsert_ip_conflict_event.

        GitLab #163 defect 1: the refresh branch used to stamp
        ``detected_at = now`` on EVERY sweep, unconditionally, and never touched
        ``collection_run_id``. That made ``detected_at`` mean "last sweep that
        saw this" rather than "when this was first detected" — which silently
        broke every age/staleness computation downstream (a conflict open for
        six months always looked thirty minutes old) while ALSO leaving
        ``collection_run_id`` pointing at the long-finished run that first wrote
        the row. Now:

          * ``last_seen_at`` is bumped on every observation — that is the field
            that legitimately means "still here as of this sweep";
          * ``detected_at`` and ``collection_run_id`` advance together, and ONLY
            when the finding's data actually changed (a different (kept, dropped)
            pair, or a changed hostname/class) — i.e. when this really is a NEW
            observation rather than the same one re-seen.
        """
        resource_id = conflict.get("kept_resource_id")
        if not resource_id:
            return
        field = conflict["field"]
        conflict_class = conflict.get("conflict_class", _CONFLICT_CLASS_UNCLASSIFIED)
        drift_type = _CONFLICT_CLASS_DRIFT_TYPES.get(
            conflict_class, _CONFLICT_CLASS_DRIFT_TYPES[_CONFLICT_CLASS_UNCLASSIFIED]
        )

        # GitLab #163 defect 3: a "distinct object" collision is exactly the
        # question graph_phase3 already queues for a human via an
        # entity_resolution_same_as ProposedAction. Emitting a DriftEvent for a
        # pair that already has a live review row duplicates one signal across
        # two inboxes with no added information — suppress instead.
        if conflict_class == _CONFLICT_CLASS_DISTINCT_OBJECT and self._same_as_review_exists(
            session, conflict
        ):
            logger.debug(
                "HostReconcileAgent: suppressing %s DriftEvent for %r — an open "
                "entity_resolution_same_as review already covers this pair",
                drift_type,
                conflict.get("short_hostname"),
            )
            return

        # Check for a pre-existing open event of THIS drift_type on this
        # resource+field so repeat runs REFRESH the same ongoing collision
        # instead of piling up a new DriftEvent every 30 minutes (open/refresh
        # semantics, same idempotency approach as _upsert_ip_conflict_event).
        existing = (
            session.query(DriftEvent)
            .filter_by(
                resource_id=resource_id,
                drift_type=drift_type,
                field=field,
                status="open",
            )
            .first()
        )
        new_value = {
            "short_hostname": conflict.get("short_hostname"),
            "source": conflict.get("source"),
            "dropped_resource_id": str(conflict["dropped_resource_id"]),
            "dropped_hostname": conflict.get("dropped_hostname"),
            "conflict_class": conflict_class,
        }
        now = _NOW()
        if existing:
            # Always: this open finding was re-observed by this sweep.
            existing.last_seen_at = now
            # Only when the finding's DATA changed is this a new detection.
            if existing.new_value != new_value:
                existing.new_value = new_value
                existing.detected_at = now
                existing.collection_run_id = run_id
            return
        session.add(
            DriftEvent(
                resource_id=resource_id,
                collection_run_id=run_id,
                drift_type=drift_type,
                field=field,
                old_value={
                    "short_hostname": conflict.get("short_hostname"),
                    "source": conflict.get("source"),
                    "kept_resource_id": str(resource_id),
                    "kept_hostname": conflict.get("kept_hostname"),
                },
                new_value=new_value,
                detected_at=now,
                last_seen_at=now,
                status="open",
            )
        )

    @staticmethod
    def _same_as_review_exists(session, conflict: dict) -> bool:
        """True when graph_phase3 already has a live same-as review for this pair.

        The review queue is keyed per SOURCE NODE (``_review_target`` builds
        ``same-as:<node_type>:<natural_key>``), so a pair is considered covered
        when EITHER side has a live row — the operator answering that row settles
        the pairing in both directions.

        Only vCenter objects can be checked: ``(vcenter, moref)`` is the vSphere
        GraphNode natural key. A conflict carrying no vCenter identity returns
        False (never suppressed), which is the fail-open direction — worst case a
        DriftEvent that duplicates a review row, never a silently dropped signal.
        """
        targets = []
        for side in ("kept_object_ids", "dropped_object_ids"):
            ids = conflict.get(side) or {}
            vcenter, moref = ids.get("vcenter"), ids.get("moref")
            if vcenter and moref:
                targets.append(
                    f"same-as:{GraphNodeType.VSPHERE_VM.value}:{vsphere_key(vcenter, moref)}"[:512]
                )
        if not targets:
            return False
        return (
            session.query(ProposedAction)
            .filter(
                ProposedAction.action_type == REVIEW_ACTION_TYPE,
                ProposedAction.target.in_(targets),
                ProposedAction.status.in_(REVIEW_LIVE_STATUSES),
            )
            .first()
            is not None
        )

    def _emit_ip_conflict_events(
        self, ip_conflicts: list[dict], run_id: uuid.UUID | None = None
    ) -> None:
        """Write a DriftEvent for each IP address conflict detected during reconciliation."""
        if not ip_conflicts:
            return
        # TRK-132: isolate each conflict's write in its own SAVEPOINT (mirrors
        # net.py::_write_net_details / k8s.py::_write_k8s_details) so one bad
        # conflict dict can't roll back every other conflict's DriftEvent in
        # the same run.
        #
        # M-2 (F-007): that per-item isolation must not make a skip
        # invisible — stash the failures on self._ip_conflict_write_errors so
        # run() can fold them into the run's errors/status.
        scope = ReconcileScope(label="ip_conflict")
        with get_session() as session:
            for conflict in ip_conflicts:
                key = conflict.get("short_hostname") or "unknown"
                try:
                    with session.begin_nested():
                        self._upsert_ip_conflict_event(session, conflict, run_id)
                    scope.observed(key)
                except Exception as exc:
                    logger.warning(
                        "HostReconcileAgent: skipping bad ip_conflict item %r: %s",
                        conflict.get("short_hostname"),
                        exc,
                    )
                    scope.failed(key, exc)
            session.commit()
        self._ip_conflict_write_errors = scope.errors
        logger.info(
            "HostReconcileAgent: recorded %d IP conflict event(s)%s",
            scope.observed_count,
            f" ({scope.failed_count} skipped)" if scope.has_failures else "",
        )

    def _upsert_ip_conflict_event(self, session, conflict: dict, run_id: uuid.UUID | None) -> None:
        """Write (or refresh) a single identity_conflict/ip_address DriftEvent.

        Split out of _emit_ip_conflict_events so it can be wrapped in a
        per-item SAVEPOINT (TRK-132).

        GitLab #163 defect 1 (mirrors _upsert_identity_conflict_event above):
        the refresh branch used to stamp ``detected_at = now`` on EVERY sweep,
        unconditionally, which broke every age/staleness computation
        downstream (a conflict open for six months always looked freshly
        detected). Now ``last_seen_at`` is bumped on every observation — that
        is the field that legitimately means "still here as of this sweep" —
        and ``detected_at`` only advances when the finding's data actually
        changed (a different ip/source pair), i.e. when this really is a NEW
        observation rather than the same one re-seen.
        """
        resource_id = conflict.get("resource_id")
        if not resource_id:
            return
        # Check for a pre-existing open identity_conflict on this resource
        existing = (
            session.query(DriftEvent)
            .filter_by(
                resource_id=resource_id,
                drift_type="identity_conflict",
                field="ip_address",
                status="open",
            )
            .first()
        )
        new_value = {"ip": conflict["new_ip"], "source": conflict["new_source"]}
        now = _NOW()
        if existing:
            # Always: this open finding was re-observed by this sweep.
            existing.last_seen_at = now
            # Only when the finding's DATA changed is this a new detection.
            if existing.new_value != new_value:
                existing.new_value = new_value
                existing.detected_at = now
                existing.collection_run_id = run_id
            return
        session.add(
            DriftEvent(
                resource_id=resource_id,
                collection_run_id=run_id,
                drift_type="identity_conflict",
                field="ip_address",
                old_value={
                    "ip": conflict["existing_ip"],
                    "source": conflict["existing_source"],
                },
                new_value=new_value,
                detected_at=now,
                last_seen_at=now,
                status="open",
            )
        )

    def _upsert_identities(self, merged: dict[str, dict]) -> tuple[int, int]:
        """Upsert HostIdentity rows from the merged host dict.

        Returns (n_new, n_updated). M-2 (F-007): per-item failures are ALSO
        stashed on ``self._identity_write_errors`` (a list of scrubbed error
        strings) for ``run()`` to fold into the run's errors/status — the
        (n_new, n_updated) return shape predates that fix and several call
        sites (including tests) rely on it, so the extra signal travels via
        this instance attribute rather than widening the return tuple.
        """
        n_new = 0
        n_updated = 0

        # TRK-132: isolate each host's upsert in its own SAVEPOINT (mirrors
        # net.py::_write_net_details / k8s.py::_write_k8s_details) so one bad
        # row can't roll back every other host's write for this run — without
        # this, a single mid-loop exception used to take down the whole
        # phase's commit (InFailedSqlTransaction cascade).
        scope = ReconcileScope(label="host_identity")
        with get_session() as session:
            for short, data in merged.items():
                try:
                    with session.begin_nested():
                        is_new = self._upsert_identity_item(session, short, data)
                    scope.observed(short)
                except Exception as exc:
                    logger.warning(
                        "HostReconcileAgent: skipping bad host_identity item %r: %s",
                        short,
                        exc,
                    )
                    scope.failed(short, exc)
                    continue
                if is_new:
                    n_new += 1
                else:
                    n_updated += 1

            session.commit()

        self._identity_write_errors = scope.errors
        return n_new, n_updated

    def _upsert_identity_item(self, session, short: str, data: dict) -> bool:
        """Upsert a single HostIdentity row keyed by ``short``.

        Returns True if a new row was created, False if an existing row was
        updated. Split out of _upsert_identities so it can be wrapped in a
        per-item SAVEPOINT (TRK-132).
        """
        existing = session.query(HostIdentity).filter_by(short_hostname=short).first()
        now = _NOW()
        if existing is None:
            row = HostIdentity(
                id=uuid.uuid4(),
                short_hostname=short,
                fqdn=data.get("fqdn"),
                ip_addresses=data.get("ip_addresses", []),
                r7_resource_id=data.get("r7_resource_id"),
                vsphere_resource_id=data.get("vsphere_resource_id"),
                octopus_resource_id=data.get("octopus_resource_id"),
                linux_resource_id=data.get("linux_resource_id"),
                windows_resource_id=data.get("windows_resource_id"),
                net_resource_id=data.get("net_resource_id"),
                cloud_resource_id=data.get("cloud_resource_id"),
                k8s_resource_id=data.get("k8s_resource_id"),
                netdevice_resource_id=data.get("netdevice_resource_id"),
                os_family=data.get("os_family"),
                risk_score=data.get("risk_score"),
                vuln_count=data.get("vuln_count"),
                patch_status=data.get("patch_status"),
                vsphere_power_state=data.get("vsphere_power_state"),
                octopus_machine_status=data.get("octopus_machine_status"),
                last_reconciled=now,
                identity_ambiguous_sources=self._ambiguous_sources(data),
            )
            session.add(row)
            return True

        # Refresh all denormalized fields on every reconcile run.
        existing.fqdn = data.get("fqdn") or existing.fqdn
        existing.ip_addresses = data.get("ip_addresses", existing.ip_addresses)
        existing.r7_resource_id = self._coalesce_resource_id(data, existing, "r7_resource_id")
        existing.vsphere_resource_id = self._coalesce_resource_id(
            data, existing, "vsphere_resource_id"
        )
        existing.octopus_resource_id = self._coalesce_resource_id(
            data, existing, "octopus_resource_id"
        )
        existing.linux_resource_id = self._coalesce_resource_id(data, existing, "linux_resource_id")
        existing.windows_resource_id = self._coalesce_resource_id(
            data, existing, "windows_resource_id"
        )
        existing.net_resource_id = self._coalesce_resource_id(data, existing, "net_resource_id")
        existing.cloud_resource_id = self._coalesce_resource_id(data, existing, "cloud_resource_id")
        existing.k8s_resource_id = self._coalesce_resource_id(data, existing, "k8s_resource_id")
        existing.netdevice_resource_id = self._coalesce_resource_id(
            data, existing, "netdevice_resource_id"
        )
        existing.os_family = data.get("os_family") or existing.os_family
        existing.risk_score = (
            data.get("risk_score") if data.get("risk_score") is not None else existing.risk_score
        )
        existing.vuln_count = (
            data.get("vuln_count") if data.get("vuln_count") is not None else existing.vuln_count
        )
        existing.patch_status = data.get("patch_status") or existing.patch_status
        existing.vsphere_power_state = (
            data.get("vsphere_power_state") or existing.vsphere_power_state
        )
        existing.octopus_machine_status = (
            data.get("octopus_machine_status") or existing.octopus_machine_status
        )
        existing.last_reconciled = now
        # KG-2: RECOMPUTED, never coalesced with the previous value. Unlike the
        # denormalized display fields above (which fall back to the stored value
        # when a source is momentarily missing), an ambiguity flag that survived
        # its own resolution would be a permanent false alarm — and, because the
        # emitters below refuse to link an ambiguous leg, a permanent hole in the
        # graph. A leg that no longer collides is cleared, so a resolved
        # collision self-heals within the 30-minute reconcile cadence.
        existing.identity_ambiguous_sources = self._ambiguous_sources(data)
        return False

    @staticmethod
    def _ambiguous_sources(data: dict) -> list[str] | None:
        """Normalized ``identity_ambiguous_sources`` value for one merged host.

        Returns None (not ``[]``) for the overwhelmingly common no-collision
        case, so the column stays NULL rather than filling with empty lists.
        """
        sources = data.get("identity_ambiguous_sources") or []
        return sorted({str(s) for s in sources}) or None
