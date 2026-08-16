"""NetDiscoveryAgent — active network, shadow-IT and threat discovery.

Continuously discovers every device and service reachable on the network,
tracks them over time, and surfaces shadow IT and unauthorized / potentially
malicious presence.

Design decisions (per spec §3–§11):
- Deterministic (NOT an LLMAgent) — propose-only, never alters targets.
- Non-disruptive by default: aggressiveness=polite maps to -T3 (rate-throttled
  via --max-rate) + top-100 TCP (TRK-280/TRK-281 bumped polite off -T2).
- Fail-closed exclusion guard: _should_probe() → False on any parse error.
- Fragile-device protection overrides the aggressiveness profile.
- Per-tier try/except: failure → run FAILED + error_message (not silent empty).
- nmap absence → clean skipped, not a crash.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import shutil
import socket
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Any

from infra_brain.agents.base import BaseAgent, CollectionResult
from infra_brain.db.models import (
    AgentActionLog,
    CollectionRun,
    DriftEvent,
    HostIdentity,
    NetDiscoveryHost,
    NetDiscoveryService,
    OctopusMachine,
    R7Asset,
    VsphereVm,
)
from infra_brain.db.session import get_session
from infra_brain.etl.base import ReconcileScope, ScrubbedErrorList
from infra_brain.etl.spec import AgentSpec, NodeSpec, Tier

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Aggressiveness profile → nmap flag sets
# ---------------------------------------------------------------------------

# Each profile is a tuple of nmap flags added to the base invocation.
# Base invocation always includes: -oX - (XML to stdout), target list, rate/delay
# overrides, and --max-retries 1 to keep scans bounded.
_PROFILE_FLAGS: dict[str, list[str]] = {
    "passive": [],  # Tier 0 only — no active probes at all
    "polite": [
        "-T2",
        "--max-rate",
        "50",
        "--scan-delay",
        "10ms",
        "-p",
        "21,22,23,25,53,80,110,111,135,139,143,443,445,993,995,"
        "1723,3306,3389,5900,5901,8080,8443,8888",
        "-sV",
        "--version-intensity",
        "0",
        "--max-retries",
        "1",
    ],
    "normal": [
        "-T3",
        "-p",
        "1-1000",
        "-sV",
        "--max-retries",
        "1",
    ],
    "aggressive": [
        "-T4",
        "-p",
        "1-65535",
        "-sV",
        "--version-intensity",
        "5",
        "-O",
        "--max-retries",
        "2",
    ],
}

# Fragile-device profile: ICMP echo only — ignores the configured aggressiveness.
_FRAGILE_FLAGS: list[str] = ["-T2", "--max-retries", "1"]

# Dangerous ports that trigger is_dangerous=True on the service row.
_DEFAULT_DANGEROUS_PORTS: frozenset[int] = frozenset([23, 445, 3389, 5900, 2323])

# ---------------------------------------------------------------------------
# MAC OUI vendor prefixes known to be associated with fragile/embedded devices.
# Comparison uses the first 3 octets (OUI) of the MAC, upper-cased, WITH colons
# (e.g. "00:18:8B") -- matches the ":".join(...) below. A dash-separated or
# colon-free MAC would need normalizing before this comparison would match it.
# ---------------------------------------------------------------------------
_FRAGILE_OUI_PREFIXES: frozenset[str] = frozenset(
    [
        # iDRAC / Dell BMC
        "00:18:8B",
        "F4:CE:46",
        "B0:83:FE",
        # HP iLO
        "00:17:A4",
        "00:1A:4B",
        # Raritan KVM
        "00:0D:5D",
    ]
)

# Port / banner signatures that commonly indicate printers or embedded devices.
_FRAGILE_PORTS: frozenset[int] = frozenset(
    [
        515,  # LPD (printer)
        9100,  # JetDirect (HP printer raw)
        161,  # SNMP (embedded devices)
        623,  # IPMI / BMC
    ]
)

# ---------------------------------------------------------------------------
# NetDiscoveryAgent
# ---------------------------------------------------------------------------


class NetDiscoveryAgent(BaseAgent):
    """Discover and classify every reachable device and service on the network.

    Domain: ``netdiscovery``.  Deterministic collector — does NOT use an LLM.

    Tiers:
      Tier 0 (passive, always on): harvest known IPs from DB + reverse DNS.
      Tier 1 (active host sweep):  ping/ARP sweep; respects aggressiveness.
      Tier 2 (active service scan): port/service/OS scan; respects aggressiveness.

    Override ``run()`` to manage CollectionRun lifecycle directly so we can mark
    individual tier failures accurately (spec §11 anti-pattern guard).
    """

    spec = AgentSpec(
        domain="netdiscovery",
        tier=Tier.COLLECTOR,
        schedule="*/15 * * * *",
        max_staleness=timedelta(hours=1),
        skip_hook=True,
        # P5 (resolver coverage for host_reconcile's "net" leg). This collector
        # is the ONLY live source behind that leg, so retiring host_reconcile's
        # IS_SAME_AS writer without a node here would leave the leg with no
        # identity representation at all.
        #
        # KEYED ON THE HOSTNAME, NEVER ON THE IP — and that is the whole
        # decision. ``resources.name`` for a discovered host is its IP address,
        # which is exactly the value this codebase has spent TRK-062/TRK-087/
        # KG-9 establishing is NOT an identity: DHCP churn, NAT and VM address
        # reassignment mean the same key names a different machine next week,
        # and a node keyed on it would silently rewrite one machine's identity
        # into another's. A NodeSpec whose natural_key resolves empty is SKIPPED
        # (graph_engine._emit_nodes), so keying on ``metadata.hostname``
        # materialises exactly the hostname-BEARING subset and leaves the
        # bare-IP probes with no node — deliberate absence, which is strictly
        # better than an unstable node, and it mirrors host_reconcile's own
        # ``net`` leg, which merges only on ``normalize_host(nd.hostname)`` and
        # routes hostname-less probes to a separate IP-attach pass.
        #
        # The bare-IP subset therefore stays UNCOVERED by design; see
        # tests/test_p5_issameas_resolver_coverage.py, which records that
        # verdict as an assertion rather than a comment.
        emits_nodes=(
            NodeSpec(
                type="NetDiscoveredHost",
                resource_type="discovered_host",
                natural_key="metadata.hostname",
                name="metadata.hostname",
                # ``mac`` is a HARD identifier to graph_phase3._score_candidate
                # (_HARD_IDENTIFIER_FIELDS) and ``ip`` drives its shared-IP
                # signal, so both must ride onto the node: the resolver matches
                # graph-side and cannot see a value the NodeSpec did not carry.
                attributes=("hostname", "ip", "mac", "mac_vendor"),
                is_host_identity=True,
            ),
        ),
        identity_keys=("hostname", "mac"),
    )

    # ------------------------------------------------------------------
    # Mandatory abstract method — called by run() via super() in the
    # standard BaseAgent.run() flow.  For netdiscovery we override run()
    # entirely so collect() must still be defined but returns [] (unused).
    # ------------------------------------------------------------------

    def collect(self, scope: str = "all") -> list[dict]:  # type: ignore[override]
        # collect() is unused in this agent — run() drives the tiers directly.
        return []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(  # type: ignore[override]
        self,
        trigger_type: str = "scheduled",
        scope: str = "all",
        sweep_id: uuid.UUID | None = None,
    ) -> CollectionResult:
        """Drive all discovery tiers and write results to the DB.

        Mirrors HostReconcileAgent.run() — manages CollectionRun lifecycle
        directly so per-tier failures can mark the run FAILED with a clear
        error_message (spec §11).
        """
        run_id = uuid.uuid4()

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
            # #178: a monotonically increasing counter (this domain's total
            # CollectionRun count, this run included) used to rotate Tier 1/
            # Tier 2 processing order across runs — see _rotate_for_run.
            # Without this, both tiers walked their target/chunk lists in the
            # exact same stable order every run, so whichever subnet/chunk
            # landed last was the one the per-phase budget always cut, run
            # after run (Tier-1/Tier-2 budget exhaustion, not a watchdog
            # reap). Reusing the persisted collection_runs table means no new
            # schema is needed to carry this counter between runs.
            run_ordinal = session.query(CollectionRun).filter_by(domain=self.domain).count()

        # M-1/TRK-106: the base run()'s finalize-in-a-finally does not apply to
        # an override, so the tier body runs inside the shared guard — a
        # BaseException (KeyboardInterrupt/SystemExit on scheduler shutdown)
        # would otherwise strand this row at status="in_progress" forever. The
        # body lives in _run_tiers() rather than under an inline `with` purely
        # so this hardening did not have to reindent ~230 lines of tier logic.
        with self.run_row_guard(run_id):
            return self._run_tiers(run_id, run_ordinal, scope)

    def _run_tiers(
        self,
        run_id: uuid.UUID,
        run_ordinal: int,
        scope: str,
    ) -> CollectionResult:
        """The Tier 0-3 body of :meth:`run`, with the CollectionRun row already open.

        Split out of ``run()`` for M-1 (see the ``run_row_guard`` call there);
        behaviour is unchanged. ``errors`` is a
        :class:`~infra_brain.etl.base.ScrubbedErrorList` so every one of the
        nine scattered append/extend sites below — and any tenth added later —
        is DSN-scrubbed (SEC-2) before it can reach
        ``CollectionRun.error_message`` or ``CollectionResult.errors``.
        """
        errors: list[str] = ScrubbedErrorList()
        hosts_found = 0
        services_found = 0
        # M-2 (F-007): reset before _persist runs — see its docstring for why
        # per-item skip failures travel via this instance attribute instead of
        # widening _persist's (hosts_written, services_written) return shape.
        self._persist_skip_errors: list[str] = []

        # AA-R-11/12: honour collection_disabled_domains BEFORE doing any
        # work — every ETLConnector.run()-standard collector respects this
        # maintenance-pause knob (F-022); a run()-override must not silently
        # skip it.
        _disabled = {
            d.strip()
            for d in (self.settings.collection_disabled_domains or "").split(",")
            if d.strip()
        }
        if self.domain in _disabled:
            reason = f"domain '{self.domain}' is in collection_disabled_domains"
            logger.info("NetDiscoveryAgent.run skipped reason=%r", reason)
            self._mark_run_skipped(run_id, reason)
            return CollectionResult(
                run_id=run_id,
                domain=self.domain,
                resources_found=0,
                drift_count=0,
                status="skipped",
                errors=errors,
            )

        # ----- Tier 0: passive (always on) --------------------------------
        try:
            passive_hosts = self._call_with_timeout(self._tier0_passive)
            logger.info("NetDiscoveryAgent: Tier 0 passive: %d candidate IP(s)", len(passive_hosts))
        except Exception as exc:
            msg = f"Tier 0 (passive) failed: {exc}"
            logger.exception("NetDiscoveryAgent: %s", msg)
            errors.append(msg)
            self._mark_run_failed(run_id, msg)
            return CollectionResult(
                run_id=run_id,
                domain=self.domain,
                resources_found=0,
                drift_count=0,
                status="failed",
                errors=errors,
            )

        # passive_hosts is a dict[str, str | None] in production (ip -> best-effort
        # hostname from _tier0_passive); tests sometimes mock it with a plain set of
        # IPs, which has no hostnames to offer.
        live_hosts: dict[str, dict[str, Any]] = {
            ip: {
                "ip": ip,
                "hostname": passive_hosts.get(ip) if isinstance(passive_hosts, dict) else None,
                "discovery_tier": "passive",
            }
            for ip in passive_hosts
        }

        # ----- Tier 1: active host sweep ----------------------------------
        aggressiveness = self.settings.netdiscovery_aggressiveness.strip().lower()
        if aggressiveness == "passive":
            logger.info("NetDiscoveryAgent: aggressiveness=passive — Tier 1/2 skipped")
        elif self.settings.netdiscovery_enable_ping_sweep:
            nmap_path = shutil.which("nmap")
            if nmap_path is None:
                logger.warning("NetDiscoveryAgent: nmap not found — Tier 1 skipped (nmap absent)")
            else:
                try:
                    subnets = self._reachable_subnets()
                    sweep_results, sweep_errors = self._call_with_timeout(
                        self._tier1_host_sweep,
                        subnets,
                        nmap_path,
                        aggressiveness,
                        run_ordinal=run_ordinal,
                    )
                    # F-007: a subnet that was skipped, timed out or errored is
                    # recorded and reported — it does NOT sink the whole run.
                    errors.extend(sweep_errors)
                    for ip, info in sweep_results.items():
                        if ip not in live_hosts:
                            live_hosts[ip] = {"ip": ip}
                        # Tier 0 (DB/PTR) hostname is more informative than a
                        # Tier 1 miss — don't let a blank nmap hostname clobber
                        # a real one already found passively.
                        prior_hostname = live_hosts[ip].get("hostname")
                        live_hosts[ip].update(info)
                        if not live_hosts[ip].get("hostname"):
                            live_hosts[ip]["hostname"] = prior_hostname
                        live_hosts[ip]["responded"] = True
                        # This host was actively ping-swept by nmap (not merely
                        # inferred from a DB/DNS record) — record the tier that
                        # actually confirmed it live.
                        live_hosts[ip]["discovery_tier"] = "tier1"
                    logger.info(
                        "NetDiscoveryAgent: Tier 1 sweep: %d live host(s)", len(sweep_results)
                    )
                except Exception as exc:
                    msg = f"Tier 1 (host sweep) failed: {exc}"
                    logger.exception("NetDiscoveryAgent: %s", msg)
                    errors.append(msg)
                    self._mark_run_failed(run_id, msg)
                    return CollectionResult(
                        run_id=run_id,
                        domain=self.domain,
                        resources_found=0,
                        drift_count=0,
                        status="failed",
                        errors=errors,
                    )
        else:
            logger.info("NetDiscoveryAgent: Tier 1 disabled (NETDISCOVERY_ENABLE_PING_SWEEP=false)")

        # ----- Tier 2: active service scan --------------------------------
        service_rows: list[dict[str, Any]] = []
        if aggressiveness != "passive" and self.settings.netdiscovery_enable_port_scan:
            nmap_path = shutil.which("nmap")
            if nmap_path is None:
                logger.warning("NetDiscoveryAgent: nmap not found — Tier 2 skipped (nmap absent)")
            else:
                probeable_hosts = {
                    ip: info
                    for ip, info in live_hosts.items()
                    if info.get("responded") and self._should_probe(ip)
                }
                if probeable_hosts:
                    try:
                        service_rows, service_errors = self._call_with_timeout(
                            self._tier2_service_scan,
                            probeable_hosts,
                            nmap_path,
                            aggressiveness,
                            run_ordinal=run_ordinal,
                        )
                        # F-007: a scan group that timed out or errored is
                        # recorded and reported — it does NOT sink the whole
                        # run (mirrors Tier 1's per-subnet resilience, TRK-239).
                        errors.extend(service_errors)
                        # Merge OS guess back into host info if Tier 2 produced it,
                        # and record that this host was actively service-scanned
                        # (not just ping-swept) — the discovery_tier persisted on
                        # NetDiscoveryHost must reflect the actual highest tier
                        # that touched the host, not just Tier 0/1.
                        for svc in service_rows:
                            ip = svc.get("ip", "")
                            if ip not in live_hosts:
                                continue
                            if svc.get("os_guess"):
                                live_hosts[ip].setdefault("os_guess", svc["os_guess"])
                            live_hosts[ip]["discovery_tier"] = "tier2"
                        logger.info(
                            "NetDiscoveryAgent: Tier 2 scan: %d service(s)", len(service_rows)
                        )
                    except Exception as exc:
                        # Should be rare now that _tier2_service_scan catches
                        # per-group errors internally — kept as a defense-in-
                        # depth net, but no longer the primary failure path.
                        msg = f"Tier 2 (service scan) failed: {exc}"
                        logger.exception("NetDiscoveryAgent: %s", msg)
                        errors.append(msg)
        elif aggressiveness != "passive":
            logger.info("NetDiscoveryAgent: Tier 2 disabled (NETDISCOVERY_ENABLE_PORT_SCAN=false)")

        # ----- Classify, persist, emit edges ------------------------------
        try:
            known_ips = self._load_known_ips()
            classified = self._classify_hosts(live_hosts, known_ips)
            dangerous_ports = self._parse_dangerous_ports()
            suspicious_sigs = self._parse_suspicious_signatures()
            hosts_found, services_found = self._call_with_timeout(
                self._persist, classified, service_rows, run_id, dangerous_ports, suspicious_sigs
            )
            # M-2 (F-007): _persist's per-item SAVEPOINT loops (below) log+skip
            # a bad host/service item (TRK-132) rather than raising, so one bad
            # row can't roll back its siblings' writes — but that per-item
            # resilience must not be invisible. _persist stashes every skip
            # here; fold them into the SAME errors list the R3 status mapping
            # below already uses, so a run that dropped items never reads
            # "completed" with an empty errors list.
            errors.extend(getattr(self, "_persist_skip_errors", []))
        except Exception as exc:
            msg = f"Persist/classify phase failed: {exc}"
            logger.exception("NetDiscoveryAgent: %s", msg)
            errors.append(msg)
            self._mark_run_failed(run_id, msg)
            return CollectionResult(
                run_id=run_id,
                domain=self.domain,
                resources_found=0,
                drift_count=0,
                status="failed",
                errors=errors,
            )

        # ----- Finalise run record ----------------------------------------
        # R3 status mapping (etl.base.CollectOutcome): no errors -> completed;
        # errors but data collected -> partial; errors and nothing collected ->
        # failed. TRK-239: a skipped/timed-out Tier 1 subnet used to be fatal,
        # discarding every host Tier 0 had already found.
        resources_found = hosts_found + services_found
        if not errors:
            status = "completed"
        elif resources_found > 0:
            status = "partial"
        else:
            status = "failed"

        # GitLab #148: _persist writes one NetDiscoveryHost detail row per host
        # and one NetDiscoveryService detail row per service, alongside the
        # generic Resource rows. Because this run() override bypasses
        # ETLConnector._write_details, the counter was never populated — detail
        # rows landed while detail_rows_written stayed 0 on every run.
        #
        # M-1/SEC-2: _finalize_run scrubs error_message, so a DSN-bearing tier
        # failure cannot persist live credentials into the dashboard-readable
        # column (belt-and-braces with ScrubbedErrorList above).
        self._finalize_run(
            run_id,
            status=status,
            error_message="; ".join(errors)[:4000] if errors else None,
            resources_found=resources_found,
            detail_rows_written=hosts_found + services_found,
        )

        with get_session() as session:
            from infra_brain.agents.base import count_drift_events_for_run

            drift_count = count_drift_events_for_run(run_id, session)

        logger.info(
            "NetDiscoveryAgent: finished — hosts=%d services=%d drift=%d status=%s",
            hosts_found,
            services_found,
            drift_count,
            status,
        )
        return CollectionResult(
            run_id=run_id,
            domain=self.domain,
            resources_found=resources_found,
            drift_count=drift_count,
            status=status,
            errors=errors,
            detail_rows_written=hosts_found + services_found,
        )

    # ------------------------------------------------------------------
    # Exclusion guard (fail-closed)
    # ------------------------------------------------------------------

    def _should_probe(self, ip: str) -> bool:
        """Return True only if ``ip`` is safe to probe.

        Fail-closed: any parse error or ambiguity returns False so we never
        accidentally probe an excluded range.  Exhaustively unit-tested.
        """
        if not ip or not ip.strip():
            return False
        ip = ip.strip()
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            logger.debug("NetDiscoveryAgent: _should_probe: cannot parse %r — denying", ip)
            return False

        for raw in self._get_exclude_cidrs():
            raw = raw.strip()
            if not raw:
                continue
            try:
                net = ipaddress.ip_network(raw, strict=False)
                if addr in net:
                    logger.debug("NetDiscoveryAgent: %s excluded by CIDR %s — skipping", ip, raw)
                    return False
            except ValueError:
                logger.warning(
                    "NetDiscoveryAgent: malformed exclude CIDR %r — denying probe for safety",
                    raw,
                )
                return False

        return True

    def _get_exclude_cidrs(self) -> list[str]:
        raw = getattr(self.settings, "netdiscovery_exclude_cidrs", "") or ""
        return [c.strip() for c in raw.split(",") if c.strip()]

    # ------------------------------------------------------------------
    # Fragile-device detection
    # ------------------------------------------------------------------

    def _is_fragile(self, ip: str, mac: str | None = None) -> bool:
        """Return True if this host should be treated as fragile.

        Fragile hosts are scanned with the reduced `_FRAGILE_FLAGS` set (gentle
        timing, single retry, no OS/version probing) — regardless of the
        configured aggressiveness profile (spec §6).

        Detection signals (any one triggers fragile classification):
          1. IP or CIDR in NETDISCOVERY_FRAGILE_CIDRS / NETDISCOVERY_FRAGILE_HOSTS.
          2. MAC OUI matches a known BMC/printer/embedded OUI prefix.
        """
        # Explicit CIDR/host override lists.
        fragile_hosts_raw = getattr(self.settings, "netdiscovery_fragile_hosts", "") or ""
        fragile_hosts = {h.strip().lower() for h in fragile_hosts_raw.split(",") if h.strip()}
        if ip.strip() in fragile_hosts:
            return True

        fragile_cidrs_raw = getattr(self.settings, "netdiscovery_fragile_cidrs", "") or ""
        for raw_cidr in fragile_cidrs_raw.split(","):
            raw_cidr = raw_cidr.strip()
            if not raw_cidr:
                continue
            try:
                if ipaddress.ip_address(ip) in ipaddress.ip_network(raw_cidr, strict=False):
                    return True
            except ValueError:
                pass

        # MAC OUI match.
        if mac:
            oui = ":".join(mac.upper().split(":")[:3])
            if oui in _FRAGILE_OUI_PREFIXES:
                return True

        return False

    # ------------------------------------------------------------------
    # Tier 0 — Passive: harvest known IPs from DB + DNS
    # ------------------------------------------------------------------

    def _tier0_passive(self) -> dict[str, str | None]:
        """Return known IPs harvested from sanctioned DB sources, mapped to a
        best-effort hostname sourced from the same rows (no extra query).

        Sources: r7_assets (``hostname``), vsphere_vms (``guest_hostname`` or
        ``name``), host_identities (``ip_addresses`` + ``fqdn``/``short_hostname``).
        Also performs a PTR sweep via the configured DNS resolver if reachable.

        A hostname already known for an IP is never overwritten with a blank
        one from a later, less-informative source.
        """
        known: dict[str, str | None] = {}

        def _remember(raw_ip: str | None, raw_hostname: str | None) -> None:
            ip = (raw_ip or "").strip()
            if not ip:
                return
            hostname = (raw_hostname or "").strip() or None
            if hostname and not known.get(ip):
                known[ip] = hostname
            else:
                known.setdefault(ip, hostname)

        with get_session() as session:
            for asset in session.query(R7Asset).all():
                if asset.ip:
                    _remember(asset.ip, asset.hostname)

            for vm in session.query(VsphereVm).all():
                if vm.ip_address:
                    _remember(vm.ip_address, vm.guest_hostname or vm.name)

            for hi in session.query(HostIdentity).all():
                ips = hi.ip_addresses or []
                hostname = hi.fqdn or hi.short_hostname
                for ip in ips:
                    if ip and ip.strip():
                        _remember(ip, hostname)

        # PTR-sweep the directly-connected subnets (passive — no probe, just DNS).
        resolver = getattr(self.settings, "netdiscovery_dns_resolver", "") or ""
        for subnet in self._reachable_subnets():
            for ip, hostname in self._ptr_sweep(subnet, resolver).items():
                _remember(ip, hostname)

        # Tailscale mesh peers: individually /32-routed nodes scattered across
        # the whole 100.64.0.0/10 CGNAT block (Tailscale allocates
        # pseudo-randomly within it, not contiguously) — a CIDR sweep large
        # enough to cover them would be ~4M addresses and get dropped whole by
        # netdiscovery_max_subnet_hosts before ever reaching nmap. Tailscale's
        # own daemon already knows every authorized peer's identity and live
        # state, which a ping/port sweep could never produce as reliably
        # (there's nothing to guess: no fingerprinting, no MAC-vendor lookup —
        # the tailnet coordination server already told this host who's who).
        # So peers are harvested directly instead of swept, same "passive,
        # known-source" treatment as the r7_assets/vsphere_vms/host_identities
        # sources above.
        for ip, hostname in self._tailscale_peers().items():
            _remember(ip, hostname)

        return {ip: hostname for ip, hostname in known.items() if self._should_probe(ip)}

    def _tailscale_peers(self) -> dict[str, str | None]:
        """Harvest ``{ip: hostname}`` for every Tailscale peer this host's
        tailscaled daemon knows about, via ``tailscale status --json``.

        Read-only: queries the local daemon's already-known peer state, makes
        no network call itself, mutates nothing (Item 1.5 — audited the same
        way as ``_reachable_subnets``'s ``ip route`` call). Degrades to an
        empty dict (never raises) when the ``tailscale`` binary is absent,
        the daemon isn't running, or the output doesn't parse — a missing
        Tailscale mesh is a normal deployment shape, not a collection error.
        Only the first IPv4 address per peer is kept (this agent's CIDR/
        IP-parsing throughout assumes IPv4); a peer with only an IPv6
        Tailscale address is skipped.
        """
        tailscale_path = shutil.which("tailscale")
        if tailscale_path is None:
            return {}

        try:
            self._audit_subprocess(
                tool="tailscale_status", args_summary="tailscale status --json", verdict="allow"
            )
            proc = subprocess.run(
                [tailscale_path, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                logger.debug(
                    "NetDiscoveryAgent: tailscale status exited %d — skipping", proc.returncode
                )
                return {}
            data = json.loads(proc.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            logger.debug("NetDiscoveryAgent: tailscale status unavailable: %s", exc)
            return {}

        peers: dict[str, str | None] = {}

        def _add_peer(node: dict) -> None:
            ipv4 = next((addr for addr in (node.get("TailscaleIPs") or []) if "." in addr), None)
            if not ipv4:
                return
            hostname = node.get("HostName") or (node.get("DNSName") or "").rstrip(".") or None
            peers[ipv4] = hostname

        self_node = data.get("Self")
        if isinstance(self_node, dict):
            _add_peer(self_node)
        for node in (data.get("Peer") or {}).values():
            if isinstance(node, dict):
                _add_peer(node)

        return peers

    def _ptr_sweep(self, subnet: str, resolver: str) -> dict[str, str]:
        """Perform a reverse-DNS PTR lookup sweep over a /24-or-smaller subnet.

        Uses stdlib socket — no external deps.  Only sweeps subnets up to /22
        (1022 addresses) to keep Tier 0 lightweight.  Returns ``{ip: hostname}``
        for every address that reverse-resolves (the resolved hostname is kept,
        not discarded — it feeds straight into the Tier 0 resource record).
        """
        try:
            net = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            return {}

        # Skip large subnets to stay passive-lightweight.
        if net.num_addresses > 1022:
            return {}

        resolved: dict[str, str] = {}
        for addr in net.hosts():
            ip = str(addr)
            if not self._should_probe(ip):
                continue
            try:
                hostname, _aliases, _addrs = socket.gethostbyaddr(ip)
                resolved[ip] = hostname
            except (socket.herror, socket.gaierror, OSError):
                pass
        return resolved

    # ------------------------------------------------------------------
    # Tier 1 — Active host sweep
    # ------------------------------------------------------------------

    def _sweep_targets(self, subnets: list[str]) -> tuple[list[str], list[str]]:
        """Narrow routed subnets down to the ones actually worth sweeping.

        Returns ``(targets, errors)``.

        TRK-239: ``ip route`` inside the app container reports the Docker bridge
        network (172.x.0.0/16 — 65534 addresses) alongside any real LAN route.
        Handing that to a single ``nmap -sn`` at ``--max-rate 50`` cannot finish
        inside ``COLLECT_TIMEOUT_SECONDS``, which is exactly what started failing
        the moment TRK-232 installed iproute2 and ``_reachable_subnets()`` began
        returning routes at all.  Two filters apply, in order:

        1. ``netdiscovery_include_cidrs`` (optional allowlist) — a route outside
           it is out of scope; dropped silently, not reported as an error. When a
           discovered route overlaps one or more allowlist entries, the actual
           sweep target(s) are the address-range *intersection* of the route and
           the matching entries — not the original (possibly much wider) route —
           so a narrow allowlist entry genuinely narrows scope instead of merely
           gating pass/fail on any overlap.
        2. ``netdiscovery_max_subnet_hosts`` — an oversized target is logged at
           WARNING so the operator sees it (and can allowlist a narrower slice),
           but the run still proceeds with whatever else is in scope. This check
           applies to the post-narrowing target, not the raw discovered route.
           It is deliberately NOT an entry in ``errors``: the cap refusing a
           65k-address sweep is this agent working as designed, and BaseAgent
           maps errors-with-no-data to status="failed". When every route is
           oversized the run therefore completes having found nothing, which
           for a ZERO_OK_DOMAINS collector is the honest outcome and clears
           itself the moment real scope exists — rather than a permanent
           "failed" that no action can resolve.
        """
        include_nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        raw_include = getattr(self.settings, "netdiscovery_include_cidrs", "") or ""
        for raw in raw_include.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                include_nets.append(ipaddress.ip_network(raw, strict=False))
            except ValueError:
                logger.warning(
                    "NetDiscoveryAgent: malformed NETDISCOVERY_INCLUDE_CIDRS entry %r — ignored",
                    raw,
                )

        max_hosts = int(getattr(self.settings, "netdiscovery_max_subnet_hosts", 1024) or 1024)

        targets: list[str] = []
        errors: list[str] = []
        oversized: list[str] = []
        for raw in subnets:
            raw = (raw or "").strip()
            if not raw:
                continue
            try:
                net = ipaddress.ip_network(raw, strict=False)
            except ValueError:
                errors.append(f"subnet {raw!r} is not a valid network — skipped")
                continue

            if include_nets:
                narrowed = self._narrow_to_allowlist(net, include_nets)
                if not narrowed:
                    logger.debug(
                        "NetDiscoveryAgent: subnet %s outside NETDISCOVERY_INCLUDE_CIDRS — skipped",
                        raw,
                    )
                    continue
            else:
                narrowed = [net]

            for candidate in narrowed:
                if candidate.num_addresses > max_hosts:
                    msg = (
                        f"subnet {candidate} skipped: {candidate.num_addresses} addresses exceeds "
                        f"NETDISCOVERY_MAX_SUBNET_HOSTS={max_hosts} "
                        f"(set NETDISCOVERY_INCLUDE_CIDRS to sweep a narrower slice)"
                    )
                    logger.warning("NetDiscoveryAgent: %s", msg)
                    oversized.append(msg)
                    continue

                targets.append(str(candidate))

        # Declining to sweep an oversized subnet is this agent's safety cap doing
        # its job — a policy decision, not a fault — so it stays out of
        # ``errors``. BaseAgent maps errors-with-no-data to status="failed", and
        # on a host whose only routes are the Docker bridges plus a wide LAN
        # route EVERY target is oversized: the run then carried errors, zero
        # data, and recorded "failed" on all 644 runs in a week, with nothing
        # short of an env change able to clear it. netdiscovery is in
        # ZERO_OK_DOMAINS, so the honest outcome — "completed, found nothing,
        # here is why" — raises no false alarm, and the WARNING logged per
        # refused subnet above keeps it visible.
        return targets, errors

    @staticmethod
    def _narrow_to_allowlist(
        net: ipaddress.IPv4Network | ipaddress.IPv6Network,
        include_nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    ) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """Return the sweep target(s) for ``net`` narrowed to ``include_nets``.

        For each allowlist entry that overlaps ``net`` at all, compute the
        address-range *intersection* rather than just gating on ``overlaps()``
        — a discovered route that is wider than the allowlist entry must be
        narrowed down to that entry's range, not passed through unmodified.
        If ``net`` is already narrower than (or equal to) the matching entry,
        the intersection is ``net`` itself (nothing to narrow).

        Multiple allowlist entries can each overlap the same discovered route
        (e.g. two narrow entries inside one wide supernet); the results are
        collapsed to merge duplicate/adjacent/overlapping ranges so callers
        never see redundant sweep targets.
        """
        pieces: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for inc in include_nets:
            if not net.overlaps(inc):
                continue
            if inc.version != net.version:
                continue
            # Address-range intersection via first/last host integers. Using
            # summarize_address_range (rather than a bare subnet_of/supernet_of
            # check) also handles the rare case of overlapping-but-unaligned
            # ranges, where neither network is a clean subnet of the other.
            start = max(int(net.network_address), int(inc.network_address))
            end = min(int(net.broadcast_address), int(inc.broadcast_address))
            if start > end:
                continue  # overlaps() said yes but the precise ranges don't intersect
            addr_cls = ipaddress.IPv4Address if net.version == 4 else ipaddress.IPv6Address
            pieces.extend(ipaddress.summarize_address_range(addr_cls(start), addr_cls(end)))

        if not pieces:
            return []
        return list(ipaddress.collapse_addresses(pieces))

    @staticmethod
    def _rotate_for_run(items: list[str], run_ordinal: int) -> list[str]:
        """Rotate ``items`` so consecutive runs start from a different position.

        #178: Tier 1 (subnet targets) and Tier 2 (per-group host chunks) each
        walk a list in a fixed, stable order and stop as soon as the
        per-phase time/item budget runs out. With no rotation that means the
        SAME tail slice — whatever happened to sort last — is the one that
        never gets swept/scanned, on every single run forever (chronic
        "partial" status, same resource count run after run). Rotating the
        starting index by ``run_ordinal % len(items)`` round-robins which
        entries sit at the front (and therefore which sit at the back): every
        entry reaches the front — and gets its shot at the budget — at least
        once every ``len(items)`` runs, so no fixed subset is permanently
        starved. ``run_ordinal=0`` (the default used by direct/unit-test
        callers) is a no-op rotation, preserving the original stable order.
        """
        if not items:
            return list(items)
        offset = run_ordinal % len(items)
        return items[offset:] + items[:offset]

    def _tier1_host_sweep(
        self,
        subnets: list[str],
        nmap_path: str,
        aggressiveness: str,
        run_ordinal: int = 0,
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """Ping sweep the reachable subnets one at a time.

        Returns ``({ip: {mac, mac_vendor, hostname}}, errors)``.

        Uses ``nmap -sn`` (host discovery only — no port scan).  Each target IP
        goes through ``_should_probe()`` before being passed to nmap, and fragile
        hosts get the fragile flag set without being port-scanned later.

        TRK-239: this used to be **one** nmap invocation across every reachable
        subnet, bounded only by the outer 600s ``_call_with_timeout`` guard — so
        one oversized or unreachable subnet produced an all-or-nothing failed run
        with ``resources_found=0``, discarding even the Tier 0 hosts already
        collected.  Now each subnet is its own bounded invocation, results
        accumulate incrementally, and a per-subnet failure is recorded in
        ``errors`` (F-007) instead of raised.  A whole-phase budget
        (``netdiscovery_tier1_budget_seconds``) stops the loop before the outer
        guard can fire, so partial results always survive to the persist phase.
        """
        targets, errors = self._sweep_targets(subnets)
        results: dict[str, dict[str, Any]] = {}
        if not targets:
            return results, errors

        # #178: rotate the stable subnet order so a budget exhaustion mid-loop
        # cuts a different tail each run instead of the same one forever.
        targets = self._rotate_for_run(targets, run_ordinal)

        base_cmd = [nmap_path, "-sn", "-oX", "-", "--max-retries", "1"]
        # Timing — TRK-280/TRK-281: polite moved -T2 -> -T3. Oversized routes
        # (Docker bridges, e.g. 172.x.0.0/16) never reach this call at all —
        # they're filtered out by netdiscovery_max_subnet_hosts in
        # _sweep_targets() before any nmap invocation — so every target here
        # is already a real, in-scope subnet (e.g. the 192.0.2.0/24 corporate
        # LAN exposed by MR !264's network_mode: host change). polite keeps
        # its --max-rate throttle (still rate-limited, unlike normal/
        # aggressive) but no longer pays -T2's extra per-probe delay on top
        # of it. normal/aggressive stay at -T3, unthrottled by --max-rate.
        if aggressiveness == "polite":
            base_cmd += ["-T3", "--max-rate", str(self.settings.netdiscovery_max_rate)]
        elif aggressiveness in ("normal", "aggressive"):
            base_cmd += ["-T3"]

        per_subnet_timeout = int(
            getattr(self.settings, "netdiscovery_subnet_timeout_seconds", 120) or 120
        )
        budget = int(getattr(self.settings, "netdiscovery_tier1_budget_seconds", 480) or 480)
        started = time.monotonic()

        for target_idx, target in enumerate(targets):
            elapsed = time.monotonic() - started
            remaining = budget - elapsed
            if remaining <= 0:
                skipped = targets[target_idx:]
                msg = f"Tier 1 budget of {budget}s exhausted — not swept: {', '.join(skipped)}"
                logger.warning("NetDiscoveryAgent: %s", msg)
                errors.append(msg)
                break

            try:
                xml_output = self._run_nmap(
                    base_cmd + [target],
                    timeout=max(1, int(min(per_subnet_timeout, remaining))),
                )
            except Exception as exc:
                msg = f"subnet {target} sweep failed: {exc}"
                logger.warning("NetDiscoveryAgent: %s", msg)
                errors.append(msg)
                continue

            results.update(self._parse_sn_xml(xml_output))

        return results, errors

    # ------------------------------------------------------------------
    # Tier 2 — Active service / OS scan
    # ------------------------------------------------------------------

    def _tier2_service_scan(
        self,
        live_hosts: dict[str, dict[str, Any]],
        nmap_path: str,
        aggressiveness: str,
        run_ordinal: int = 0,
    ) -> list[dict[str, Any]]:
        """Service/version scan the live hosts; return list of service dicts.

        Each dict: {ip, port, proto, service, banner, fingerprint, os_guess}.
        Fragile hosts (per ``_is_fragile``, using the IP/MAC known from Tier 1)
        are scanned separately with ``_FRAGILE_FLAGS`` — the reduced,
        non-disruptive flag set — instead of the configured aggressiveness
        profile.

        TRK-280/TRK-281 follow-up: this used to call ``_run_nmap`` twice with
        no explicit timeout (implicit 600s default each), bounded only by the
        outer 600s ``_call_with_timeout`` guard at the ``run()`` level — so
        once a real, previously-unreachable LAN (e.g. ``192.0.2.0/24`` via MR
        !264) started producing live hosts for Tier 1, Tier 2 had far more to
        probe than before and started blowing the outer guard, which discards
        the ENTIRE result (Tier 0/1's already-collected hosts included, not
        just Tier 2's). First fix: bounded per-group timeout, mirroring Tier
        1's own ``netdiscovery_tier1_budget_seconds`` pattern (TRK-239) — that
        stopped the catastrophic all-or-nothing failure, but a live
        re-verification against ~109 real hosts (109 IPs x 23 ports x -sV in
        ONE nmap invocation) still exceeded the whole-group budget in a single
        shot, so the "normal" group still contributed zero rows even though
        the run itself now degrades gracefully. Second fix (this pass): each
        group is now chunked (``netdiscovery_tier2_chunk_size`` hosts per nmap
        invocation, incremental like Tier 1's per-subnet loop) so a slow batch
        costs only its own chunk's rows, not the whole group's — the budget is
        still the single source of truth for when to stop, just checked more
        often. Each chunk's failure is recorded in the returned errors list
        (F-007) instead of raised.
        """
        if not live_hosts:
            return [], []

        fragile_ips = [
            ip for ip, info in live_hosts.items() if self._is_fragile(ip, info.get("mac"))
        ]
        normal_ips = [ip for ip in live_hosts if ip not in fragile_ips]

        # #178: rotate each group's host order so the chunk that gets cut by
        # a budget exhaustion isn't the same tail slice on every run.
        fragile_ips = self._rotate_for_run(fragile_ips, run_ordinal)
        normal_ips = self._rotate_for_run(normal_ips, run_ordinal)

        rows: list[dict[str, Any]] = []
        errors: list[str] = []

        budget = int(getattr(self.settings, "netdiscovery_tier2_budget_seconds", 480) or 480)
        chunk_size = max(1, int(getattr(self.settings, "netdiscovery_tier2_chunk_size", 20) or 20))
        started = time.monotonic()

        groups: list[tuple[str, list[str], tuple[str, ...] | list[str]]] = []
        if fragile_ips:
            groups.append(("fragile", fragile_ips, _FRAGILE_FLAGS))
        if normal_ips:
            profile_flags = _PROFILE_FLAGS.get(aggressiveness, _PROFILE_FLAGS["polite"])
            groups.append(("normal", normal_ips, profile_flags))

        for label, ips, flags in groups:
            chunks = [ips[i : i + chunk_size] for i in range(0, len(ips), chunk_size)]
            for chunk_idx, chunk in enumerate(chunks):
                elapsed = time.monotonic() - started
                remaining = budget - elapsed
                if remaining <= 0:
                    skipped = sum(len(c) for c in chunks[chunk_idx:])
                    msg = (
                        f"Tier 2 budget of {budget}s exhausted — not scanned: "
                        f"{label} ({skipped} host(s) remaining)"
                    )
                    logger.warning("NetDiscoveryAgent: %s", msg)
                    errors.append(msg)
                    break

                cmd = [nmap_path, "-oX", "-"] + list(flags) + chunk
                try:
                    xml_output = self._run_nmap(cmd, timeout=max(1, int(remaining)))
                    rows.extend(self._parse_service_xml(xml_output, aggressiveness))
                except Exception as exc:
                    msg = f"Tier 2 {label} chunk {chunk_idx + 1}/{len(chunks)} scan failed: {exc}"
                    logger.warning("NetDiscoveryAgent: %s", msg)
                    errors.append(msg)
                    continue

        return rows, errors

    # ------------------------------------------------------------------
    # Subprocess boundary gate + audit (Wave 1 item 1.5 / F-004)
    # ------------------------------------------------------------------

    def _audit_subprocess(
        self,
        tool: str,
        args_summary: str,
        verdict: str,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        """Write one AgentActionLog row per subprocess invocation (best-effort).

        The audit row must never break a sweep — DB failure is logged, not raised.
        The gate's PermissionError (below) is raised by the CALLER regardless.
        """
        try:
            with get_session() as session:
                session.add(
                    AgentActionLog(
                        agent=type(self).__name__,
                        domain=self.domain,
                        tool=tool,
                        args_summary=args_summary[:2000],
                        verdict=verdict,
                        status=status,
                        error=error,
                    )
                )
                session.commit()
        except Exception:
            logger.exception(
                "NetDiscoveryAgent: audit row write failed (tool=%s verdict=%s)",
                tool,
                verdict,
            )

    def _gate_nmap_targets(self, cmd: list[str]) -> None:
        """R2 boundary gate: raise PermissionError BEFORE subprocess.run when any
        nmap target fails the exclusion gate. Fail-closed, mirroring _should_probe:
        a malformed exclude CIDR denies everything.

        Target tokens are the cmd elements that parse as an IP address or an IP
        network; flag values (port lists, rates, delays) do not parse and are
        skipped deterministically.
        """
        exclude_nets = []
        for raw in self._get_exclude_cidrs():
            try:
                exclude_nets.append(ipaddress.ip_network(raw, strict=False))
            except ValueError:
                reason = f"malformed exclude CIDR {raw!r} — denying all probes (fail closed)"
                self._audit_subprocess(
                    tool="nmap",
                    args_summary=" ".join(cmd),
                    verdict="deny",
                    status="error",
                    error=reason,
                )
                raise PermissionError(f"[netdiscovery-gate] {reason}")
        for token in cmd[1:]:
            if token.startswith("-"):
                continue
            try:
                ipaddress.ip_address(token)
                is_single_ip = True
            except ValueError:
                is_single_ip = False
            if is_single_ip:
                if not self._should_probe(token):
                    reason = f"target {token} fails the exclusion gate"
                    self._audit_subprocess(
                        tool="nmap",
                        args_summary=" ".join(cmd),
                        verdict="deny",
                        status="error",
                        error=reason,
                    )
                    raise PermissionError(f"[netdiscovery-gate] {reason}")
                continue
            try:
                net = ipaddress.ip_network(token, strict=False)
            except ValueError:
                continue  # flag value (port list, rate, delay) — not a target
            for excl in exclude_nets:
                if net.overlaps(excl):
                    reason = f"target network {token} overlaps excluded CIDR {excl}"
                    self._audit_subprocess(
                        tool="nmap",
                        args_summary=" ".join(cmd),
                        verdict="deny",
                        status="error",
                        error=reason,
                    )
                    raise PermissionError(f"[netdiscovery-gate] {reason}")

    # ------------------------------------------------------------------
    # nmap subprocess wrapper
    # ------------------------------------------------------------------

    def _run_nmap(self, cmd: list[str], timeout: int = 600) -> str:
        """Run nmap and return stdout as a string.

        GATED (item 1.5): every invocation passes _gate_nmap_targets() first —
        a gate-failing target raises PermissionError BEFORE subprocess.run —
        and every allowed invocation writes an AgentActionLog audit row.

        ``timeout`` is the subprocess wall-clock cap in seconds.  Tier 1 passes
        its per-subnet budget (TRK-239) so one slow subnet cannot consume the
        whole phase; callers that omit it keep the previous 600s behaviour.

        Raises RuntimeError with the stderr/returncode on failure so the
        per-tier except in run() can mark the run FAILED accurately.
        """
        self._gate_nmap_targets(cmd)
        self._audit_subprocess(tool="nmap", args_summary=" ".join(cmd), verdict="allow")
        logger.debug("NetDiscoveryAgent: nmap cmd: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"nmap timed out: {exc}") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"nmap binary not found: {exc}") from exc

        if proc.returncode not in (0, 1):  # nmap returns 1 on partial failures
            raise RuntimeError(f"nmap exited with code {proc.returncode}: {proc.stderr[:500]}")
        return proc.stdout

    # ------------------------------------------------------------------
    # nmap XML parsers
    # ------------------------------------------------------------------

    def _parse_sn_xml(self, xml: str) -> dict[str, dict[str, Any]]:
        """Parse ``nmap -sn`` XML output into {ip: {responded, mac, mac_vendor, hostname}}.

        Returns an empty dict on empty or malformed XML — never raises.
        """
        results: dict[str, dict[str, Any]] = {}
        if not xml or not xml.strip():
            return results
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            logger.warning("NetDiscoveryAgent: _parse_sn_xml: XML parse error: %s", exc)
            return results

        for host_el in root.findall("host"):
            state_el = host_el.find("status")
            if state_el is None or state_el.get("state") != "up":
                continue

            ip: str | None = None
            mac: str | None = None
            mac_vendor: str | None = None

            for addr_el in host_el.findall("address"):
                addrtype = addr_el.get("addrtype", "")
                if addrtype == "ipv4":
                    ip = addr_el.get("addr")
                elif addrtype == "mac":
                    mac = addr_el.get("addr")
                    mac_vendor = addr_el.get("vendor") or None

            if not ip:
                continue
            if not self._should_probe(ip):
                continue

            hostname: str | None = None
            hostnames_el = host_el.find("hostnames")
            if hostnames_el is not None:
                hn_el = hostnames_el.find("hostname")
                if hn_el is not None:
                    hostname = hn_el.get("name") or None

            results[ip] = {
                "responded": True,
                "mac": mac,
                "mac_vendor": mac_vendor,
                "hostname": hostname,
                "is_fragile": self._is_fragile(ip, mac),
            }

        return results

    def _parse_service_xml(self, xml: str, aggressiveness: str) -> list[dict[str, Any]]:
        """Parse ``nmap`` service-scan XML into a list of service dicts.

        Each dict: {ip, port, proto, service, banner, fingerprint, os_guess}.
        Returns empty list on empty/malformed XML.
        """
        rows: list[dict[str, Any]] = []
        if not xml or not xml.strip():
            return rows
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            logger.warning("NetDiscoveryAgent: _parse_service_xml: XML parse error: %s", exc)
            return rows

        for host_el in root.findall("host"):
            state_el = host_el.find("status")
            if state_el is None or state_el.get("state") != "up":
                continue

            ip: str | None = None
            for addr_el in host_el.findall("address"):
                if addr_el.get("addrtype") == "ipv4":
                    ip = addr_el.get("addr")
                    break
            if not ip or not self._should_probe(ip):
                continue

            # OS guess (only available at normal/aggressive).
            os_guess: str | None = None
            osmatch_el = host_el.find(".//osmatch")
            if osmatch_el is not None:
                os_guess = osmatch_el.get("name") or None

            ports_el = host_el.find("ports")
            if ports_el is None:
                continue
            for port_el in ports_el.findall("port"):
                state_el2 = port_el.find("state")
                if state_el2 is None or state_el2.get("state") != "open":
                    continue
                port = int(port_el.get("portid", 0))
                proto = port_el.get("protocol", "tcp")
                svc_el = port_el.find("service")
                svc_name: str | None = None
                banner: str | None = None
                fingerprint: str | None = None
                if svc_el is not None:
                    svc_name = svc_el.get("name") or None
                    # nmap puts the version string in "product" / "version" / "extrainfo"
                    parts = [
                        svc_el.get("product") or "",
                        svc_el.get("version") or "",
                        svc_el.get("extrainfo") or "",
                    ]
                    banner_str = " ".join(p for p in parts if p).strip()
                    banner = banner_str or None
                    fingerprint = svc_el.get("ostype") or None

                rows.append(
                    {
                        "ip": ip,
                        "port": port,
                        "proto": proto,
                        "service": svc_name,
                        "banner": banner,
                        "fingerprint": fingerprint,
                        "os_guess": os_guess,
                    }
                )

        return rows

    # ------------------------------------------------------------------
    # Reachable subnet discovery
    # ------------------------------------------------------------------

    def _reachable_subnets(self) -> list[str]:
        """Enumerate directly-connected subnets from the host's network interfaces.

        Uses ``ip route`` on Linux (the container's OS) to list reachable
        networks.  Falls back to an empty list if the command fails or is
        unavailable.
        """
        subnets: list[str] = []
        try:
            # Item 1.5: audited subprocess — one AgentActionLog row per invocation.
            self._audit_subprocess(tool="ip_route", args_summary="ip route", verdict="allow")
            proc = subprocess.run(
                ["ip", "route"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                return []
            for line in proc.stdout.splitlines():
                parts = line.split()
                if not parts:
                    continue
                # Lines like "10.0.0.0/24 dev eth0 proto kernel ..." or
                # "default via 10.0.0.1 dev eth0" — take the first token.
                candidate = parts[0]
                if candidate == "default":
                    continue
                # Skip the container's own Docker/Podman/CNI bridge routes —
                # these are container-internal plumbing, not real LAN
                # segments, and their auto-assigned pools are typically /16s
                # (65534 hosts) that will always exceed
                # NETDISCOVERY_MAX_SUBNET_HOSTS. The interface name (not the
                # CIDR) is the reliable signal: bridge pools are configurable
                # and a hardcoded CIDR list would also risk silently
                # blinding the sweep to a real LAN that happens to use the
                # same RFC1918 range.
                if "dev" in parts:
                    iface = parts[parts.index("dev") + 1]
                    if iface == "docker0" or iface.startswith(("br-", "veth", "podman", "cni")):
                        continue
                try:
                    net = ipaddress.ip_network(candidate, strict=False)
                    # Skip link-local and loopback
                    if net.is_loopback or net.is_link_local:
                        continue
                    subnets.append(str(net))
                except ValueError:
                    pass
        except FileNotFoundError:
            # The `ip` binary is missing from the image (iproute2 not installed).
            # This is NOT the same as "genuinely no routes": it silently disables
            # all active discovery, so it must be loud in the logs.
            logger.warning(
                "NetDiscoveryAgent: `ip` binary not found (iproute2 not installed) — "
                "cannot enumerate reachable subnets. Net discovery will silently collect "
                "nothing beyond passive Tier 0 sources: no Tier 1 host sweep and no Tier 2 "
                "service scan will run. Install iproute2 in the app image to restore collection."
            )
        except Exception:
            logger.debug("NetDiscoveryAgent: _reachable_subnets: ip route failed", exc_info=True)
        return subnets

    # ------------------------------------------------------------------
    # Classification helpers
    # ------------------------------------------------------------------

    def _load_known_ips(self) -> set[str]:
        """Load all IPs that are sanctioned (in the approved inventory sources)."""
        known: set[str] = set()
        with get_session() as session:
            for asset in session.query(R7Asset).all():
                if asset.ip:
                    known.add(asset.ip.strip())
            for vm in session.query(VsphereVm).all():
                if vm.ip_address:
                    known.add(vm.ip_address.strip())
            for machine in session.query(OctopusMachine).all():
                # OctopusMachine stores a URI; extract hostname/IP best-effort.
                if machine.uri:
                    host = machine.uri.split("//")[-1].split(":")[0].split("/")[0]
                    if host:
                        known.add(host.strip())
            for hi in session.query(HostIdentity).all():
                for ip in hi.ip_addresses or []:
                    if ip:
                        known.add(ip.strip())
        # Tailscale peers are authorized-by-construction (the tailnet's own
        # coordination server vouches for them) — sanctioned the same as the
        # inventory-backed sources above, not shadow IT.
        known.update(self._tailscale_peers().keys())
        return known

    def _classify_hosts(
        self,
        live_hosts: dict[str, dict[str, Any]],
        known_ips: set[str],
    ) -> dict[str, dict[str, Any]]:
        """Classify each discovered host as known / shadow-IT / threat."""
        for ip, info in live_hosts.items():
            info.setdefault("responded", False)
            info.setdefault("is_fragile", self._is_fragile(ip, info.get("mac")))

            is_known = ip in known_ips
            info["is_known"] = is_known
            info["is_shadow_it"] = info.get("responded", False) and not is_known

            threat_level = "none"
            # Bump threat for shadow IT — unaccounted live device.
            if info["is_shadow_it"]:
                threat_level = "low"
            # Unknown MAC OUI raises threat.
            if not is_known and not info.get("mac_vendor"):
                if threat_level == "low":
                    threat_level = "med"
            info["threat_level"] = threat_level

        return live_hosts

    # ------------------------------------------------------------------
    # Watchlist helpers
    # ------------------------------------------------------------------

    def _parse_dangerous_ports(self) -> frozenset[int]:
        raw = getattr(self.settings, "netdiscovery_dangerous_ports", "") or ""
        ports: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ports.add(int(part))
        return frozenset(ports) if ports else _DEFAULT_DANGEROUS_PORTS

    def _parse_suspicious_signatures(self) -> list[str]:
        raw = getattr(self.settings, "netdiscovery_suspicious_signatures", "") or ""
        return [s.strip().lower() for s in raw.split(",") if s.strip()]

    def _classify_service(
        self,
        port: int,
        service: str | None,
        banner: str | None,
        dangerous_ports: frozenset[int],
        suspicious_sigs: list[str],
    ) -> tuple[bool, bool]:
        """Return (is_dangerous, is_suspicious) for a discovered service."""
        is_dangerous = port in dangerous_ports

        combined = " ".join(part.lower() for part in [service or "", banner or ""] if part)
        is_suspicious = any(sig in combined for sig in suspicious_sigs)
        return is_dangerous, is_suspicious

    # ------------------------------------------------------------------
    # Persist phase
    # ------------------------------------------------------------------

    def _persist(
        self,
        classified: dict[str, dict[str, Any]],
        service_rows: list[dict[str, Any]],
        run_id: uuid.UUID,
        dangerous_ports: frozenset[int],
        suspicious_sigs: list[str],
    ) -> tuple[int, int]:
        """Upsert host and service rows; emit DriftEvents.

        Cross-source IS_SAME_AS identity linking is NOT emitted here —
        HostReconcileAgent is the sole writer of IS_SAME_AS edges (Task 4.6).

        Each host item and each service item is written inside its own
        SAVEPOINT (``session.begin_nested()``), matching the established
        per-item isolation pattern in ``net.py``/``k8s.py``. Previously the
        whole batch shared a single commit at the end of the run: one bad
        row raising partway through poisoned the transaction
        (``InFailedSqlTransaction``) and silently lost every other host/
        service write for that run (TRK-132). Now a single bad item is
        logged and skipped — its siblings in the same run still persist —
        and the final commit at the end of the run is unchanged.

        Returns (hosts_written, services_written). M-2 (F-007): per-item
        failures are ALSO stashed on ``self._persist_skip_errors`` (a list of
        scrubbed error strings) for ``run()``/``_run_tiers()`` to fold into
        the run's R3 ``errors`` list — the (hosts_written, services_written)
        return shape predates that fix and many call sites (including tests)
        rely on it, so the extra signal travels via this instance attribute
        rather than widening the return tuple.
        """
        hosts_written = 0
        services_written = 0
        now = datetime.now(UTC)
        skip_scope = ReconcileScope(label="netdiscovery item")

        # Group service rows by IP for lookup.
        services_by_ip: dict[str, list[dict[str, Any]]] = {}
        for svc in service_rows:
            services_by_ip.setdefault(svc["ip"], []).append(svc)

        with get_session() as session:
            from infra_brain.api._seeding import upsert_resource

            for ip, info in classified.items():
                try:
                    with session.begin_nested():
                        resource_id, host_id, is_new_host = self._upsert_host_item(
                            session, ip, info, run_id, now, upsert_resource
                        )
                except Exception as exc:
                    logger.warning("NetDiscoveryAgent: skipping bad host item %r: %s", ip, exc)
                    skip_scope.failed(f"host {ip}", exc)
                    continue

                hosts_written += 1

                # --- Upsert services (each in its own SAVEPOINT) ---
                for svc in services_by_ip.get(ip, []):
                    try:
                        with session.begin_nested():
                            self._upsert_service_item(
                                session,
                                resource_id,
                                host_id,
                                ip,
                                svc,
                                run_id,
                                dangerous_ports,
                                suspicious_sigs,
                                now,
                                upsert_resource,
                            )
                    except Exception as exc:
                        logger.warning(
                            "NetDiscoveryAgent: skipping bad service item %r on %s: %s",
                            svc.get("port"),
                            ip,
                            exc,
                        )
                        skip_scope.failed(f"service {svc.get('port')} on {ip}", exc)
                        continue

                    services_written += 1

            session.commit()

        self._persist_skip_errors = skip_scope.errors
        return hosts_written, services_written

    def _upsert_host_item(
        self,
        session,
        ip: str,
        info: dict[str, Any],
        run_id: uuid.UUID,
        now: datetime,
        upsert_resource,
    ) -> tuple[uuid.UUID, uuid.UUID, bool]:
        """Upsert one Resource + NetDiscoveryHost row and its DriftEvents.

        Runs inside the caller's SAVEPOINT — any exception here rolls back
        only this host's writes, not the rest of the batch.

        Returns (resource_id, host_id, is_new_host).
        """
        # --- Upsert generic Resource row (insert+update+merge, single path) ---
        resource = upsert_resource(
            session,
            name=ip,
            domain=self.domain,
            resource_type="discovered_host",
            source="NetDiscoveryAgent",
            zone=self.settings.default_zone,
            last_seen=now,
            metadata={
                "hostname": info.get("hostname"),
                # P5: the IP is the resource's NAME, which a NodeSpec's
                # ``attributes`` (metadata keys only) cannot reach. The identity
                # resolver's shared-IP signal reads ``attributes["ip"]``, so it
                # is carried here too — one duplicated scalar, versus the leg
                # having no correlatable address at all.
                "ip": ip,
                "mac": info.get("mac"),
                "mac_vendor": info.get("mac_vendor"),
            },
        )

        resource_id = resource.id

        # --- Upsert NetDiscoveryHost row ---
        existing_host = session.query(NetDiscoveryHost).filter_by(ip=ip).first()
        is_new_host = existing_host is None
        if is_new_host:
            host_row = NetDiscoveryHost(
                id=uuid.uuid4(),
                ip=ip,
                mac=info.get("mac"),
                mac_vendor=info.get("mac_vendor"),
                hostname=info.get("hostname"),
                first_seen=now,
                last_seen=now,
                responded=info.get("responded", False),
                discovery_tier=info.get("discovery_tier", "passive"),
                is_fragile=info.get("is_fragile", False),
                is_known=info.get("is_known", False),
                is_shadow_it=info.get("is_shadow_it", False),
                threat_level=info.get("threat_level", "none"),
                resource_id=resource_id,
                zone=self.settings.default_zone,
            )
            session.add(host_row)
            session.flush()
            host_id = host_row.id
        else:
            # Update fields that change between sweeps.
            existing_host.last_seen = now
            existing_host.responded = info.get("responded", existing_host.responded)
            existing_host.hostname = info.get("hostname") or existing_host.hostname
            existing_host.mac = info.get("mac") or existing_host.mac
            existing_host.mac_vendor = info.get("mac_vendor") or existing_host.mac_vendor
            existing_host.is_known = info.get("is_known", existing_host.is_known)
            existing_host.is_shadow_it = info.get("is_shadow_it", existing_host.is_shadow_it)
            existing_host.resource_id = resource_id

            old_threat = existing_host.threat_level
            new_threat = info.get("threat_level", "none")
            existing_host.threat_level = new_threat
            existing_host.is_fragile = info.get("is_fragile", existing_host.is_fragile)
            host_id = existing_host.id

            # Emit DriftEvent on threat escalation.
            if old_threat != new_threat and _threat_rank(new_threat) > _threat_rank(old_threat):
                session.add(
                    DriftEvent(
                        resource_id=resource_id,
                        collection_run_id=run_id,
                        drift_type="threat_escalation",
                        field="threat_level",
                        old_value={"threat_level": old_threat},
                        new_value={"threat_level": new_threat},
                        detected_at=now,
                        status="open",
                    )
                )

        # --- DriftEvent: new shadow-IT host ---
        if is_new_host and info.get("is_shadow_it"):
            session.add(
                DriftEvent(
                    resource_id=resource_id,
                    collection_run_id=run_id,
                    drift_type="shadow_it_discovered",
                    field="ip",
                    old_value=None,
                    new_value={"ip": ip, "hostname": info.get("hostname")},
                    detected_at=now,
                    status="open",
                )
            )

        return resource_id, host_id, is_new_host

    def _upsert_service_item(
        self,
        session,
        resource_id: uuid.UUID,
        host_id: uuid.UUID,
        ip: str,
        svc: dict[str, Any],
        run_id: uuid.UUID,
        dangerous_ports: frozenset[int],
        suspicious_sigs: list[str],
        now: datetime,
        upsert_resource,
    ) -> None:
        """Upsert one NetDiscoveryService row + its Resource row and DriftEvent.

        Runs inside the caller's SAVEPOINT — any exception here rolls back
        only this service's writes, not the rest of the host's services or
        the batch.
        """
        port = svc["port"]
        proto = svc.get("proto", "tcp")
        service_name = svc.get("service")
        banner = svc.get("banner")
        fingerprint = svc.get("fingerprint")

        is_dangerous, is_suspicious = self._classify_service(
            port, service_name, banner, dangerous_ports, suspicious_sigs
        )

        existing_svc = (
            session.query(NetDiscoveryService)
            .filter_by(host_id=host_id, port=port, proto=proto)
            .first()
        )
        if existing_svc is None:
            svc_row = NetDiscoveryService(
                id=uuid.uuid4(),
                host_id=host_id,
                port=port,
                proto=proto,
                service=service_name,
                banner=banner,
                fingerprint=fingerprint,
                is_dangerous=is_dangerous,
                is_suspicious=is_suspicious,
                last_seen=now,
            )
            session.add(svc_row)

            # Emit DriftEvent for newly discovered dangerous/suspicious service.
            if is_dangerous or is_suspicious:
                event_type = (
                    "dangerous_service_discovered"
                    if is_dangerous
                    else "suspicious_service_discovered"
                )
                session.add(
                    DriftEvent(
                        resource_id=resource_id,
                        collection_run_id=run_id,
                        drift_type=event_type,
                        field=f"port/{proto}/{port}",
                        old_value=None,
                        new_value={
                            "port": port,
                            "proto": proto,
                            "service": service_name,
                            "banner": banner,
                        },
                        detected_at=now,
                        status="open",
                    )
                )
        else:
            existing_svc.last_seen = now
            existing_svc.service = service_name or existing_svc.service
            existing_svc.banner = banner or existing_svc.banner
            existing_svc.fingerprint = fingerprint or existing_svc.fingerprint
            existing_svc.is_dangerous = is_dangerous
            existing_svc.is_suspicious = is_suspicious

        # Generic Resource row for the service (insert+update+merge, single path).
        svc_resource_name = f"{ip}:{port}/{proto}"
        upsert_resource(
            session,
            name=svc_resource_name,
            domain=self.domain,
            resource_type="discovered_service",
            source="NetDiscoveryAgent",
            zone=self.settings.default_zone,
            last_seen=now,
            metadata={
                "port": port,
                "proto": proto,
                "service": service_name,
                "banner": banner,
            },
        )

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------

    def _mark_run_failed(self, run_id: uuid.UUID, msg: str) -> None:
        """Mark the CollectionRun as failed with the given error message.

        M-1: delegates to the shared ``ETLConnector._finalize_run``, which
        applies the SEC-2 ``scrub_dsn`` before persisting — this used to write
        ``msg`` verbatim, so a DB-layer exception carrying a DSN (e.g. a
        malformed ``POSTGRES_URL`` in ``sqlalchemy.exc.ArgumentError``) landed
        live credentials in the dashboard-readable ``error_message``. It also
        keeps the never-raise + log behaviour this helper already had.
        """
        self._finalize_run(run_id, status="failed", error_message=msg)

    def _mark_run_skipped(self, run_id: uuid.UUID, reason: str) -> None:
        """Mark the CollectionRun as skipped (F-022 collection_disabled_domains).

        M-1: see ``_mark_run_failed`` — same delegation, same scrub.
        """
        self._finalize_run(run_id, status="skipped", error_message=reason)


# ---------------------------------------------------------------------------
# Module-level utility
# ---------------------------------------------------------------------------

_THREAT_RANKS: dict[str, int] = {"none": 0, "low": 1, "med": 2, "high": 3}


def _threat_rank(level: str) -> int:
    return _THREAT_RANKS.get(level, 0)
