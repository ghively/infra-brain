import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from langchain_core.tools import ToolException

from infra_brain.db.models import (
    DriftEvent,
    HostCertificate,
    HostFirewallRule,
    HostShare,
    LinuxCron,
    LinuxHost,
    LinuxMount,
    LinuxNic,
    LinuxPackage,
    LinuxPendingUpdate,
    LinuxPort,
    LinuxService,
    LinuxUser,
    Resource,
)
from infra_brain.db.session import get_session
from infra_brain.etl.base import (
    CollectOutcome,
    CollectorSkipped,
    ETLConnector,
    ReconcileScope,
)
from infra_brain.etl.spec import AgentSpec, NodeSpec, Tier
from infra_brain.tools.ansible import (
    ALL_HOSTS_UNREACHABLE_MARKER,
    INVENTORY_UNAVAILABLE_MARKER,
    ZERO_HOST_INVENTORY_MARKER,
    ansible_certificates_tool,
    ansible_crontab_slurp_tool,
    ansible_facts_tool,
    ansible_firewall_rules_tool,
    ansible_getent_tool,
    ansible_listen_ports_tool,
    ansible_pending_updates_tool,
    ansible_shares_tool,
)

logger = logging.getLogger(__name__)


class LinuxAgent(ETLConnector):
    spec = AgentSpec(
        domain="linux",
        tier=Tier.COLLECTOR,
        schedule="0 */6 * * *",
        max_staleness=timedelta(hours=8),
        # P1 of docs/decisions/2026-08-11-graph-first-architecture.md.
        # homelab_services declares `Service ─RUNS_ON→ Host`, and an edge needs
        # a target: the HOST is linux's entity, not the service collector's.
        # homelab_services must not mint host nodes of its own — the manifest
        # only knows a hostname STRING, not the machine — so the linux
        # collector declares them here and the two declarations meet in the
        # graph. That meeting is the whole point of a graph "across" sources,
        # and this is the first place in this codebase it happens declaratively.
        #
        # Deliberately only the entity, no attributes and no edges. Ports,
        # packages, crons, mounts and firewall rules are FACTS ABOUT this host,
        # not relationships between independently-existing things (design doc
        # §3.1's modelling rule), so they stay in their detail tables, fully
        # queryable and reachable via this node. Migrating linux's containment
        # edges out of graph_maintenance is P2+ and explicitly not started here.
        emits_nodes=(
            NodeSpec(
                type="LinuxHost",
                resource_type="linux_host",
                natural_key="name",
                # P5: this IS the machine another source may know under its own
                # name, so it belongs in the identity resolver's candidate
                # population (graph_phase3.HOST_NODE_TYPES). Declared here rather
                # than only in that tuple so the two cannot drift again — which
                # is precisely what happened between P1 and P5: this node type
                # was declared, materialised on the live estate, and still never
                # loaded by resolve_entities.
                is_host_identity=True,
            ),
        ),
        # Ansible inventory hostname. The 4 concrete legs host_reconcile
        # hardcodes for sources this homelab does not run (r7_/vsphere_/
        # octopus_/k8s_/cloud_/windows_resource_id, all zero rows) are not
        # replaced here — that is the P2 resolver switch. This is the
        # declaration half only.
        identity_keys=("name",),
    )

    # H-5: fact key(s) each destructive detail-write category reads, paired with
    # the enrichment probe label(s) that supply them. ``_may_reconcile`` consults
    # this to decide whether "no rows in the facts" is a measurement or a
    # swallowed probe failure. Categories fed by the primary ``-m setup`` gather
    # (packages, services, mounts, NICs) are deliberately absent: that gather is
    # never swallowed — it raises and fails the run — so its absence of a key is
    # always a real measurement.
    _USERS_FACT_KEYS = ("ansible_users", "ansible_getent_passwd")
    # Users need BOTH getent probes: passwd supplies the rows and group supplies
    # the sudo flag, so reinserting on a half-failed pair would silently rewrite
    # every user as non-sudo.
    _USERS_PROBES = ("getent_passwd", "getent_group")

    def collect(self, scope: str = "all") -> CollectOutcome:
        # H-5: reset per run — a stale scope from a previous run() on the same
        # instance must never authorize this run's deletes.
        self._enrichment_scope: ReconcileScope | None = None
        inventory = self.settings.ansible_inventory_path
        try:
            raw = ansible_facts_tool.invoke(
                {"target": scope, "inventory": inventory},
                config={"callbacks": self.callbacks},
            )
        except ToolException as exc:
            # GitLab #143: a zero-parsed-host inventory (misconfigured
            # ANSIBLE_INVENTORY_PATH, no genuine connectivity problem) is
            # distinguishable from every other ansible_facts_tool failure by
            # the ZERO_HOST_INVENTORY_MARKER prefix tools/ansible.py attaches.
            # Convert it into CollectorSkipped — the same distinct, clearly-
            # labeled outcome WindowsAgent.collect() uses for its own
            # unconfigured-credentials self-skip gate — instead of a generic
            # status="failed" that reads identically to a real SSH/network
            # outage in collection_runs.
            message = str(exc)
            if ZERO_HOST_INVENTORY_MARKER in message:
                reason = message.replace(f"{ZERO_HOST_INVENTORY_MARKER} ", "", 1)
                raise CollectorSkipped(reason) from exc
            if INVENTORY_UNAVAILABLE_MARKER in message:
                # There is no inventory to read at all — ANSIBLE_INVENTORY_PATH
                # unset, or naming a path absent on this host (usual cause: the
                # inventory volume was never mounted). Same class as the
                # zero-host case above, so same CollectorSkipped outcome.
                #
                # This check MUST live here, in the ToolException handler.
                # tools/ansible.py raises RuntimeError, but @tool wraps it into
                # ToolException before it reaches this frame — so the previous
                # attempt, which put this branch in an `except RuntimeError`
                # below, never fired: the marker leaked into the error text
                # while the status stayed "failed", 153 escalating runs' worth.
                # Verified against the live tool, not assumed.
                reason = message.replace(f"{INVENTORY_UNAVAILABLE_MARKER} ", "", 1)
                raise CollectorSkipped(reason) from exc
            if ALL_HOSTS_UNREACHABLE_MARKER in message:
                # GitLab #160: hosts matched the inventory but every one was
                # unreachable — a real SSH/network/credential outage, not a
                # config problem. This IS a genuine failure (unlike the
                # zero-host case above) and must still surface as
                # status="failed", but relabeled so a human triaging
                # collection_runs.error_message doesn't have to guess which
                # failure mode a 47-second run represents.
                reason = message.replace(f"{ALL_HOSTS_UNREACHABLE_MARKER} ", "", 1)
                raise RuntimeError(f"[linux-fleet-unreachable] {reason}") from exc
            raise
        except RuntimeError as exc:
            # Defensive twin of the INVENTORY_UNAVAILABLE_MARKER branch above.
            # In production the @tool wrapper always converts tools/ansible.py's
            # RuntimeError into ToolException, so the handler above is the one
            # that fires; this covers a direct (unwrapped) call to the tool
            # function, e.g. from a test or a future non-@tool call site.
            message = str(exc)
            if INVENTORY_UNAVAILABLE_MARKER in message:
                reason = message.replace(f"{INVENTORY_UNAVAILABLE_MARKER} ", "", 1)
                raise CollectorSkipped(reason) from exc
            raise
        # If the tool raised (non-zero exit, timeout, total unreachability) the
        # exception propagates to BaseAgent.run() which records status="failed"
        # with error_message.  A successful call that returns {} (legitimate
        # empty inventory) reaches here and produces an empty list — that is NOT
        # an error and stays status="completed"/resources_found=0.
        #
        # scope defaults to "all", which sweeps every inventory group, not just
        # a "linux" one -- deliberately, since real Linux hosts also live in
        # other groups (e.g. a Synology NAS). That means hostnames which are
        # only reachable aliases -- DNS entries/CNAMEs that resolve back to a
        # collector host itself rather than a distinct machine (commonly an
        # inventory group like "adopted_dns") -- answer -m setup too and get
        # recorded as real linux_host resources with facts identical to
        # whatever they route back to. Drop configured non-host hostnames here,
        # before enrichment probes below run against them too.
        if raw and self.settings.linux_exclude_hosts:
            excluded = {
                h.strip() for h in self.settings.linux_exclude_hosts.split(",") if h.strip()
            }
            dropped = sorted(h for h in raw if h in excluded)
            if dropped:
                logger.info("LinuxAgent: excluding configured non-host aliases: %s", dropped)
                raw = {h: v for h, v in raw.items() if h not in excluded}
        #
        # MR-J (INV-1): enrich the plain -m setup gather with the fact keys
        # LinuxUser/LinuxCron/LinuxPort's writers below have been "wired and
        # ready" for but never received (docs/audit/
        # FLEET_INVENTORY_AND_VSPHERE_AUDIT_2026-07-06.md). Each probe is
        # independently best-effort: a failure in ONE (e.g. the target
        # environment lacks community.general, or a host has no crontab) must
        # never fail the whole Linux collection, since the core -m setup
        # gather above already succeeded and is the primary signal.
        if raw:
            self._enrichment_scope = self._merge_enrichment_facts(raw, scope, inventory)
        self._last_raw = raw  # cache for _write_linux_details
        items = []
        for hostname, host_data in raw.items():
            facts = host_data.get("ansible_facts", {})
            items.append(
                {
                    "name": hostname,
                    "type": "linux_host",
                    "data": {
                        "distro": facts.get("ansible_distribution", ""),
                        "version": facts.get("ansible_distribution_version", ""),
                        "kernel": facts.get("ansible_kernel", ""),
                        "arch": facts.get("ansible_architecture", ""),
                        "packages": [
                            {"name": pkg_name, "version": versions[0].get("version", "")}
                            for pkg_name, versions in facts.get("ansible_packages", {}).items()
                            if versions
                        ],
                        "services": [
                            {
                                "name": svc_name,
                                "state": svc_data.get("state", ""),
                                "enabled": svc_data.get("status", "") == "enabled",
                            }
                            for svc_name, svc_data in facts.get("ansible_services", {}).items()
                        ],
                    },
                }
            )
        # H-5: a swallowed enrichment-probe failure must not be reported as a
        # clean run. Feeding the scope's errors into CollectOutcome routes them
        # through run()'s existing R3 status mapping (errors + items ->
        # "partial"); there is deliberately no separate signalling channel.
        return CollectOutcome(
            items=items,
            errors=self._enrichment_scope.errors if self._enrichment_scope else [],
        )

    def _merge_enrichment_facts(self, raw: dict, scope: str, inventory: str) -> ReconcileScope:
        """MR-J (INV-1): merge getent/crontab/listen_ports enrichment into *raw*.

        Each of the three probes below is an independent, best-effort
        ``.invoke()`` call through the same callback chain as the primary
        ``-m setup`` gather (so it is boundary-checked and audit-logged the
        same way). A failure in any ONE probe is logged and treated as "no
        data for this probe" — it must never fail the whole Linux collection,
        since the setup gather above already succeeded and is the primary
        signal this agent reports on.

        H-5: "no data for this probe" is NOT the same claim as "this host has no
        rows", and the delete-then-reinsert detail writers below could not tell
        the two apart — a probe that raised left its fact key absent, exactly
        like a host that genuinely has nothing, so one SSH blip deleted every
        stored port/user/cron/posture row and reinserted nothing. Worse, the
        emptied ``linux_ports`` table made ``_check_port_drift``'s ``prev_ports``
        snapshot empty on every subsequent run, permanently disarming
        new-listening-port detection.

        The returned :class:`ReconcileScope` is what makes the two states
        distinguishable: a probe is ``observed`` only when its call returned, and
        ``_write_linux_details`` reconciles a category only when every probe
        feeding it is in ``safe_scope``. Its ``errors`` are returned from
        ``collect()`` so the run reports "partial" rather than a green run over
        data it never actually looked at.
        """
        probe_scope = ReconcileScope(label="linux enrichment probe")

        def _safe_invoke(tool_, kwargs: dict, label: str) -> dict:
            try:
                result = tool_.invoke(kwargs, config={"callbacks": self.callbacks}) or {}
            except Exception as exc:  # noqa: BLE001 - one probe's failure must not abort the rest
                logger.warning("LinuxAgent: enrichment probe '%s' failed (skipped): %s", label, exc)
                probe_scope.failed(label, exc)
                return {}
            probe_scope.observed(label)
            return result

        passwd = _safe_invoke(
            ansible_getent_tool,
            {"target": scope, "inventory": inventory, "database": "passwd"},
            "getent_passwd",
        )
        groups = _safe_invoke(
            ansible_getent_tool,
            {"target": scope, "inventory": inventory, "database": "group"},
            "getent_group",
        )
        ports = _safe_invoke(
            ansible_listen_ports_tool, {"target": scope, "inventory": inventory}, "listen_ports"
        )
        crontab = _safe_invoke(
            ansible_crontab_slurp_tool, {"target": scope, "inventory": inventory}, "crontab"
        )

        # TRK-031: posture probes — each is an independent, best-effort probe,
        # same as the MR-J trio above. Their run functions return
        # ``{host: {"ansible_facts": {<key>: [rows]}}}``; the merge loop below
        # copies each key straight through into the host's facts.
        updates = _safe_invoke(
            ansible_pending_updates_tool,
            {"target": scope, "inventory": inventory},
            "pending_updates",
        )
        certs = _safe_invoke(
            ansible_certificates_tool, {"target": scope, "inventory": inventory}, "certificates"
        )
        firewall = _safe_invoke(
            ansible_firewall_rules_tool,
            {"target": scope, "inventory": inventory},
            "firewall_rules",
        )
        shares = _safe_invoke(
            ansible_shares_tool, {"target": scope, "inventory": inventory}, "shares"
        )

        for hostname, host_data in raw.items():
            facts = host_data.setdefault("ansible_facts", {})

            passwd_facts = (passwd.get(hostname) or {}).get("ansible_facts", {})
            getent_passwd = passwd_facts.get("getent_passwd")
            if getent_passwd is not None:
                facts.setdefault("ansible_getent_passwd", getent_passwd)

            group_facts = (groups.get(hostname) or {}).get("ansible_facts", {})
            getent_group = group_facts.get("getent_group")
            if getent_group is not None:
                facts.setdefault("ansible_getent_group", getent_group)

            port_facts = (ports.get(hostname) or {}).get("ansible_facts", {})
            listen_ports = port_facts.get("ansible_listen_ports")
            if listen_ports is not None:
                facts.setdefault("ansible_listen_ports", listen_ports)

            cron_facts = (crontab.get(hostname) or {}).get("ansible_facts", {})
            cron_rows = cron_facts.get("ansible_crontab")
            if cron_rows is not None:
                facts.setdefault("ansible_crontab", cron_rows)

            # TRK-031 posture keys (pending_updates / host_certificates /
            # firewall_rules / host_shares).
            for key, src in (
                ("pending_updates", updates),
                ("host_certificates", certs),
                ("firewall_rules", firewall),
                ("host_shares", shares),
            ):
                src_facts = (src.get(hostname) or {}).get("ansible_facts", {})
                value = src_facts.get(key)
                if value is not None:
                    facts.setdefault(key, value)

        return probe_scope

    def _may_reconcile(self, facts: dict, fact_keys: tuple, probes: tuple) -> bool:
        """H-5: may this run DESTROY *facts*' stored rows for this category?

        A delete-then-reinsert pass asserts "these are ALL the rows that exist".
        That assertion is only sound if this run actually looked. Two things
        prove it did:

        * the fact key is PRESENT on this host — data arrived, even if the list
          is empty (present-but-empty is a genuine "nothing here"); or
        * every probe feeding the key completed without raising. The enrichment
          tools omit a host entirely when it produced no rows (see
          ``tools/ansible.py::_merge_host_fact`` and ``run_ansible_listen_ports``,
          which only set their key for a non-empty result), so a successful probe
          plus an absent key genuinely means zero rows.

        If neither holds, the probe raised and was swallowed: we did not look,
        so we must not delete. Skipping one cycle is cheap and self-correcting;
        deleting live data is neither (and for ports it is not even
        self-correcting — see ``_check_port_drift``).

        ``_enrichment_scope`` is None only when no enrichment pass ran for these
        facts (a direct call to this writer from a test or a legacy caller). In
        that case nothing claims a probe failed, so the historical unconditional
        behaviour is kept. ``collect()`` always sets it before
        ``_write_linux_details`` can run with a non-empty ``_last_raw``, so this
        fallback is unreachable in production.
        """
        if any(k in facts for k in fact_keys):
            return True
        scope = getattr(self, "_enrichment_scope", None)
        if scope is None:
            return True
        return all(p in scope.safe_scope for p in probes)

    _recompute_drift_after_details = True

    def _detail_writers(self, scope, result):
        # Write domain-specific rows after the base run upserts resources, via
        # ETLConnector.run()'s _write_details so a detail-write failure is
        # SURFACED on the CollectionRun/result (not silently swallowed).
        # AA-C-2: this phase writes port-drift DriftEvent rows AFTER the base run
        # already computed drift_count, so _recompute_drift_after_details=True
        # makes run() recompute it afterwards (result not left stale).
        return [lambda: self._write_linux_details(scope, result.run_id)]

    def _write_linux_details(self, scope: str, run_id: uuid.UUID | None = None) -> int:
        raw = getattr(self, "_last_raw", None)
        if not raw:
            return 0

        rows_written = 0
        with get_session() as session:
            for hostname, host_data in raw.items():
                facts = host_data.get("ansible_facts", {})
                resource = session.query(Resource).filter_by(domain="linux", name=hostname).first()
                if not resource:
                    continue

                rows_written += 1
                existing = session.query(LinuxHost).filter_by(resource_id=resource.id).first()
                if not existing:
                    host_row = LinuxHost(
                        id=uuid.uuid4(),
                        resource_id=resource.id,
                        distro=facts.get("ansible_distribution", ""),
                        kernel=facts.get("ansible_kernel", ""),
                        arch=facts.get("ansible_architecture", ""),
                    )
                    session.add(host_row)
                    session.flush()
                    host_id = host_row.id
                else:
                    # S-16: previously the existing row's facts were frozen at
                    # first-insert values forever — a host that upgraded its distro
                    # version, kernel, or architecture never reflected that on
                    # subsequent collections. Update in place on every scan, same
                    # as the delete-then-reinsert child tables below.
                    existing.distro = facts.get("ansible_distribution", "")
                    existing.kernel = facts.get("ansible_kernel", "")
                    existing.arch = facts.get("ansible_architecture", "")
                    host_id = existing.id

                # Packages — delete-then-reinsert per host (idempotent).
                session.query(LinuxPackage).filter_by(host_id=host_id).delete()
                for pkg_name, versions in facts.get("ansible_packages", {}).items():
                    if versions:
                        session.add(
                            LinuxPackage(
                                id=uuid.uuid4(),
                                host_id=host_id,
                                name=pkg_name,
                                version=versions[0].get("version", ""),
                                manager="apt" if facts.get("ansible_pkg_mgr") == "apt" else "rpm",
                            )
                        )

                # Services — delete-then-reinsert per host (idempotent).
                session.query(LinuxService).filter_by(host_id=host_id).delete()
                for svc_name, svc_data in facts.get("ansible_services", {}).items():
                    session.add(
                        LinuxService(
                            id=uuid.uuid4(),
                            host_id=host_id,
                            name=svc_name,
                            state=svc_data.get("state", "unknown"),
                            enabled=svc_data.get("status", "") == "enabled",
                        )
                    )

                # Users — delete-then-reinsert per host (idempotent).
                #
                # Source: getent passwd, surfaced as ``ansible_getent_passwd`` when
                # the gather is enriched with ``getent`` (community.general). Plain
                # ``ansible -m setup`` does NOT enumerate local users, so today's
                # blind collectors yield {} here — the writer is wired and ready and
                # populates as soon as the enriched gather supplies the key. ``sudo``
                # is derived from ``ansible_getent_group`` membership in sudo/wheel.
                #
                # H-5: gated — a failed getent probe leaves these keys absent,
                # which must not be read as "this host has no users".
                if self._may_reconcile(facts, self._USERS_FACT_KEYS, self._USERS_PROBES):
                    session.query(LinuxUser).filter_by(host_id=host_id).delete()
                for row in _iter_users(facts):
                    session.add(
                        LinuxUser(
                            id=uuid.uuid4(),
                            host_id=host_id,
                            username=row["username"],
                            shell=row.get("shell", ""),
                            sudo=row.get("sudo", False),
                            last_login=row.get("last_login"),
                        )
                    )

                # Crons — delete-then-reinsert per host (idempotent).
                #
                # Source: an enriched gather key ``ansible_crontab`` (a list of
                # {owner, schedule, command}). Plain ``setup`` does not read
                # crontabs → {} on blind collectors; writer wired and ready.
                #
                # H-5: gated — see _may_reconcile.
                if self._may_reconcile(facts, ("ansible_crontab",), ("crontab",)):
                    session.query(LinuxCron).filter_by(host_id=host_id).delete()
                for row in facts.get("ansible_crontab", []) or []:
                    session.add(
                        LinuxCron(
                            id=uuid.uuid4(),
                            host_id=host_id,
                            owner=row.get("owner", ""),
                            schedule=row.get("schedule", ""),
                            command=row.get("command", ""),
                        )
                    )

                # Ports — delete-then-reinsert per host (idempotent).
                #
                # Source: ``ansible_listen_ports`` — the shape emitted by
                # community.general.listen_ports_facts (a list of {port, protocol,
                # name, ...}). Plain ``setup`` does not gather listening ports → {}
                # on blind collectors; writer wired and ready.
                #
                # H-5: this gate is the single most important one in this file.
                # ``_check_port_drift`` below compares against the rows snapshotted
                # here, so wiping the table on a failed probe does not merely lose
                # one cycle of data — ``prev_ports`` is then empty on EVERY later
                # run, the ``if prev_ports:`` guard short-circuits forever, and
                # new-listening-port detection silently disarms itself permanently
                # for that host. Skip the whole block (there is nothing to insert
                # and nothing trustworthy to compare) rather than reconcile blind.
                if self._may_reconcile(facts, ("ansible_listen_ports",), ("listen_ports",)):
                    # Snapshot BEFORE delete for drift comparison.
                    prev_ports: set[int] = {
                        row.port
                        for row in session.query(LinuxPort).filter_by(host_id=host_id).all()
                    }
                    session.query(LinuxPort).filter_by(host_id=host_id).delete()
                    new_port_entries = facts.get("ansible_listen_ports", []) or []
                    new_ports: list[int] = []
                    for row in new_port_entries:
                        port_num = int(row.get("port", 0) or 0)
                        new_ports.append(port_num)
                        session.add(
                            LinuxPort(
                                id=uuid.uuid4(),
                                host_id=host_id,
                                port=port_num,
                                proto=row.get("protocol") or row.get("proto") or "",
                                process=row.get("name") or row.get("process"),
                                state=row.get("state", "LISTEN"),
                            )
                        )
                    # Emit drift events for newly appearing ports.
                    if prev_ports:
                        self._check_port_drift(session, resource.id, new_ports, prev_ports, run_id)

                # Mounts — delete-then-reinsert per host (idempotent).
                #
                # INV long-tail pick: ``ansible_mounts`` is ALREADY returned by
                # plain ``-m setup`` (zero new gather cost) but was previously
                # dropped after building the shallow Resource.data dict above.
                session.query(LinuxMount).filter_by(host_id=host_id).delete()
                for row in _iter_mounts(facts):
                    session.add(LinuxMount(id=uuid.uuid4(), host_id=host_id, **row))

                # NICs — delete-then-reinsert per host (idempotent).
                #
                # INV long-tail pick: ``ansible_interfaces`` + per-interface
                # ``ansible_<name>`` dicts are ALSO already returned by plain
                # ``-m setup`` — same zero-new-gather-cost rationale as mounts.
                session.query(LinuxNic).filter_by(host_id=host_id).delete()
                for row in _iter_nics(facts):
                    session.add(LinuxNic(id=uuid.uuid4(), host_id=host_id, **row))

                # --- TRK-031 posture rows --------------------------------------
                # Each category is written inside its own SAVEPOINT so a bad row
                # in one posture stream (e.g. a malformed cert on one host) rolls
                # back only that category, never the whole host's detail write.
                self._write_posture_rows(session, facts, resource.id, host_id)

            session.commit()
        return rows_written

    def _write_posture_rows(self, session, facts: dict, resource_id, host_id) -> None:
        """TRK-031: persist pending updates / certs / shares / firewall rules.

        pending updates key off ``host_id`` (linux_hosts, like LinuxPackage);
        certs/shares/firewall rules key off ``resource_id`` (host_posture
        tables, shared with the Windows writer). Each is delete-then-reinsert
        (idempotent) inside a per-category SAVEPOINT (``begin_nested``) — a
        failure in one is logged and rolled back to the savepoint without
        aborting the rest.

        H-5: each category's DELETE is additionally gated on ``_may_reconcile``.
        These four keys all come from swallowed best-effort probes, so an absent
        key can mean either "no rows on this host" or "the probe raised and we
        never looked" — only the former may prune.
        """

        def _savepoint(label: str, fn) -> None:
            try:
                with session.begin_nested():
                    fn()
            except Exception as exc:  # noqa: BLE001 - one category must not abort the rest
                logger.warning(
                    "LinuxAgent: posture write '%s' failed (rolled back to savepoint): %s",
                    label,
                    exc,
                )

        # Pending updates — delete-then-reinsert per host (linux_hosts.id).
        def _write_updates():
            if self._may_reconcile(facts, ("pending_updates",), ("pending_updates",)):
                session.query(LinuxPendingUpdate).filter_by(host_id=host_id).delete()
            for row in facts.get("pending_updates", []) or []:
                package = row.get("package")
                if not package:
                    continue
                session.add(
                    LinuxPendingUpdate(
                        id=uuid.uuid4(),
                        host_id=host_id,
                        package=package,
                        current_version=row.get("current_version"),
                        available_version=row.get("available_version"),
                        security=bool(row.get("security", False)),
                        manager=row.get("manager"),
                    )
                )

        # Certificates — delete-then-reinsert per resource (host_certificates).
        def _write_certs():
            if self._may_reconcile(facts, ("host_certificates",), ("certificates",)):
                session.query(HostCertificate).filter_by(resource_id=resource_id).delete()
            now = datetime.now(UTC)
            for row in facts.get("host_certificates", []) or []:
                thumbprint = str(row.get("thumbprint") or "")
                if not thumbprint:
                    continue
                not_after = row.get("not_after")
                days_left = (not_after - now).days if not_after else None
                session.add(
                    HostCertificate(
                        id=uuid.uuid4(),
                        resource_id=resource_id,
                        store=str(row.get("store") or "linux")[:128],
                        subject=row.get("subject"),
                        issuer=row.get("issuer"),
                        thumbprint=thumbprint[:64],
                        not_before=row.get("not_before"),
                        not_after=not_after,
                        days_until_expiry=days_left,
                        is_expired=bool(days_left is not None and days_left < 0),
                    )
                )

        # Shares — delete-then-reinsert per resource (host_shares). Linux writes
        # nfs+smb; a Windows host is a different resource_id, so deleting all
        # shares for THIS resource_id cannot clobber the Windows writer's rows.
        def _write_shares():
            if self._may_reconcile(facts, ("host_shares",), ("shares",)):
                session.query(HostShare).filter_by(resource_id=resource_id).delete()
            for row in facts.get("host_shares", []) or []:
                name = row.get("name")
                if not name:
                    continue
                session.add(
                    HostShare(
                        id=uuid.uuid4(),
                        resource_id=resource_id,
                        share_type=row.get("share_type") or "nfs",
                        name=str(name)[:256],
                        path=row.get("path"),
                        permissions=row.get("permissions") or [],
                    )
                )

        # Firewall rules — delete-then-reinsert per resource (host_firewall_rules).
        def _write_firewall():
            if self._may_reconcile(facts, ("firewall_rules",), ("firewall_rules",)):
                session.query(HostFirewallRule).filter_by(resource_id=resource_id).delete()
            for row in facts.get("firewall_rules", []) or []:
                rule_text = row.get("rule_text")
                if not rule_text:
                    continue
                session.add(
                    HostFirewallRule(
                        id=uuid.uuid4(),
                        resource_id=resource_id,
                        table_name=row.get("table_name"),
                        chain=row.get("chain"),
                        rule_text=rule_text,
                        action=row.get("action"),
                        source=row.get("source") or "iptables",
                    )
                )

        _savepoint("pending_updates", _write_updates)
        _savepoint("host_certificates", _write_certs)
        _savepoint("host_shares", _write_shares)
        _savepoint("firewall_rules", _write_firewall)

    def _check_port_drift(
        self,
        session,
        resource_id: uuid.UUID,
        new_ports: list[int],
        prev_ports: set[int],
        run_id: uuid.UUID | None = None,
    ) -> None:
        """Emit DriftEvent for any port present now but not in the previous scan."""
        new_port_set = set(new_ports)
        for port in new_port_set:
            if port not in prev_ports:
                session.add(
                    DriftEvent(
                        resource_id=resource_id,
                        collection_run_id=run_id,
                        drift_type="new_listening_port",
                        field="port",
                        old_value=None,
                        new_value={"port": port},
                        detected_at=datetime.now(UTC),
                        status="open",
                    )
                )
                logger.info(
                    "LinuxAgent: new listening port %d detected on resource %s",
                    port,
                    resource_id,
                )


def _iter_users(facts: dict):
    """Yield normalized user rows from getent-enriched facts.

    Accepts either ``ansible_getent_passwd`` (the getent module's
    ``{username: [passwd-fields...]}`` shape) or a pre-normalized
    ``ansible_users`` list of dicts. Returns nothing for plain ``setup`` facts.
    """
    users = facts.get("ansible_users")
    if isinstance(users, list):
        for u in users:
            if u.get("username") or u.get("name"):
                yield {
                    "username": u.get("username") or u.get("name"),
                    "shell": u.get("shell", ""),
                    "sudo": bool(u.get("sudo", False)),
                    "last_login": u.get("last_login"),
                }
        return

    passwd = facts.get("ansible_getent_passwd")
    if isinstance(passwd, dict):
        sudo_members = _sudo_members(facts)
        for username, fields in passwd.items():
            # getent passwd fields: [password, uid, gid, gecos, home, shell]
            shell = fields[5] if isinstance(fields, list) and len(fields) > 5 else ""
            yield {
                "username": username,
                "shell": shell,
                "sudo": username in sudo_members,
                "last_login": None,
            }


def _sudo_members(facts: dict) -> set[str]:
    """Members of sudo/wheel groups, from ansible_getent_group when present."""
    members: set[str] = set()
    groups = facts.get("ansible_getent_group")
    if isinstance(groups, dict):
        for gname in ("sudo", "wheel"):
            entry = groups.get(gname)
            # getent group fields: [password, gid, members-csv]
            if isinstance(entry, list) and len(entry) > 2 and entry[2]:
                members.update(m for m in entry[2].split(",") if m)
    return members


_BYTES_PER_GB = 1024**3


def _iter_mounts(facts: dict):
    """Yield normalized per-filesystem rows from the setup-gathered
    ``ansible_mounts`` fact (list of {mount, device, fstype, size_total,
    size_available, ...}, sizes in bytes)."""
    for m in facts.get("ansible_mounts", []) or []:
        if not isinstance(m, dict):
            continue
        mount = m.get("mount")
        if not mount:
            continue
        yield {
            "mount": mount,
            "device": m.get("device"),
            "fstype": m.get("fstype"),
            "size_total_gb": _bytes_to_gb(m.get("size_total")),
            "size_available_gb": _bytes_to_gb(m.get("size_available")),
        }


def _bytes_to_gb(value) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(value / _BYTES_PER_GB, 2)


def _iter_nics(facts: dict):
    """Yield normalized per-interface rows from setup-gathered facts:
    ``ansible_interfaces`` (a list of interface names) plus the per-interface
    ``ansible_<name>`` dict Ansible emits for each one (fact keys replace
    non-alphanumeric characters in the interface name with ``_``)."""
    for name in facts.get("ansible_interfaces", []) or []:
        if not name:
            continue
        key = "ansible_" + re.sub(r"\W", "_", name)
        nic = facts.get(key)
        if not isinstance(nic, dict):
            continue
        ipv4 = nic.get("ipv4") or {}
        ipv6_entries = nic.get("ipv6") or []
        ipv6 = None
        if ipv6_entries and isinstance(ipv6_entries[0], dict):
            ipv6 = ipv6_entries[0].get("address")
        yield {
            "name": name,
            "mac": nic.get("macaddress"),
            "ipv4": ipv4.get("address") if isinstance(ipv4, dict) else None,
            "ipv6": ipv6,
            "speed_mbps": nic.get("speed") if isinstance(nic.get("speed"), int) else None,
        }
