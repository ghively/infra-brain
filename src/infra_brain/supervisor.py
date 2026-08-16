"""Supervisor — honest typed dispatch for the collection pipeline.

Wave 4 item 4.4 (F-009): the previous LangGraph-based "star router" earned none
of the framework's benefits — no checkpointer, no reducer on the result, no
node RetryPolicy — so it was a veneer, not an adoption. dispatch() is now a plain
typed function. Collectors are deterministic ETL connectors (infra_brain.etl);
durability for the genuinely-agentic layer lives in the LangGraph chat/LLM
agents (items 4.2/4.7), not here.
"""

import importlib
import logging
import uuid
from collections.abc import Iterator, Mapping

from infra_brain.agents.base import CollectionResult, ETLConnector

# DriftDetector and NotificationAgent are used directly by _post_collection_hook
# on EVERY non-skipped dispatch() call (regardless of which domain was
# dispatched), so they are not lazy-registry candidates the way the ~30
# per-domain collectors are — the hook always needs them, so importing them
# eagerly here costs nothing extra beyond what the hook would pay anyway.
from infra_brain.agents.drift import DriftDetector
from infra_brain.agents.notification import NotificationAgent

# is_retired (not retired_domains) so dispatch() resolves ONE agent class per
# call — the fleet-wide helper goes through agent_specs(), which imports every
# agent module and would defeat _LazyAgentRegistry below.
from infra_brain.etl.spec import is_retired

# The dispatchable__<domain> pause lever lives in runtime_flags (not here) so
# the sweep-graph path in graph.py, the MCP roster, and the dashboard
# agent-config endpoint all read the SAME override — the toggle was a no-op
# under sweep_graph_enabled while this lookup was supervisor-private.
from infra_brain.runtime_flags import (
    PAUSED_RUN_STATUS,
    dispatchable_override,
    record_paused_run,
)

logger = logging.getLogger(__name__)

# B6 (Finding 5, declarative agent registry): previously, wiring a new
# scheduled agent required 3 separate hand-edits — this module's
# AGENT_REGISTRY dict, its SKIP_HOOK set, and scheduler.py's
# _DEFAULT_SCHEDULES dict — with exception categories (SKIP_HOOK membership,
# no-op registrations like inventory_mr) explained only in comments. That
# split bookkeeping was a documented source of a real bug (the "query"
# dead-dispatch slot). Each agent's schedule/skip_hook/dispatchable is now a
# class attribute (etl/base.py's ETLConnector) — the single source of truth —
# and AGENT_REGISTRY/SKIP_HOOK below are both built from ONE iteration over
# _AGENT_SPECS instead of hand-maintained in parallel.
#
# _AGENT_SPECS intentionally stores (module path, class name) STRINGS, not
# imported classes: AGENT_REGISTRY lazily resolves a domain's class on first
# access (via dispatch(), the agents roster endpoint, etc.) rather than
# importing all ~30 agent modules — several 1000+ line files — at
# supervisor.py's own import time. A process that only needs dispatch()
# reachable (not necessarily every collector loaded at boot) no longer pays
# that import cost just by importing this module.
#
#   • drift, notification — NOT scheduled (schedule=None); they are run BY
#     _post_collection_hook after every (non-skipped) collection, not on
#     their own cadence. skip_hook=True on both prevents recursion.
#   • coverage, graph_maintenance, query — on-demand/optional-dependency
#     agents. Absence of the module degrades that ONE domain (KeyError on
#     lookup, logged), not the whole registry — same fail-open behavviour the
#     old defensive try/except imports gave, just resolved lazily instead of
#     at supervisor.py import time.
#   • inventory_mr — schedule=None, skip_hook=True. collect() always returns
#     []; a dispatch() call is a no-op run. Real MR creation requires calling
#     propose_inventory_fix() directly on an InventoryMRAgent instance (e.g.
#     from NotificationAgent after drift detection). Registered so the domain
#     is visible in the agents roster endpoint.
#   • onprem — REMOVED (F-006): the alias inherited domain="vsphere" so a
#     domain="onprem" collection run was structurally impossible; callers
#     must use "vsphere".
_AGENT_SPECS: dict[str, tuple[str, str]] = {
    # cloud / k8s / windows: RETIRED, not deleted (2026-08-12). These were
    # previously switched off by commenting the entry out here AND setting
    # dispatchable=False on the spec — which worked, but removed the domain
    # from AGENT_REGISTRY entirely, so an operator saw nothing at all and
    # turning one back on meant two edits to critical files. They now declare
    # ``retired=True`` on their AgentSpec (etl/spec.py), which switches off
    # exactly the same things (no schedule, no sweep-graph node, no freshness
    # monitoring, no dispatch) while keeping the domain registered, importable,
    # testable, and visible in the roster as deliberately off. Re-enable with
    # COLLECTION_REVIVED_DOMAINS — no code change.
    "cloud": ("infra_brain.agents.cloud", "CloudAgent"),
    "k8s": ("infra_brain.agents.k8s", "K8sAgent"),
    "windows": ("infra_brain.agents.windows", "WindowsAgent"),
    "linux": ("infra_brain.agents.linux", "LinuxAgent"),
    "dns": ("infra_brain.agents.dns", "DnsAgent"),
    # POC (2026-07-15): net (SNMP system-group collector) disabled — netdiscovery
    # already covers network-device reachability/inventory, and net only adds the
    # shallow SNMPv2 system group (sysName/sysDescr/location/contact). Re-enable by
    # uncommenting and configuring snmp_targets + snmp_community.
    # "net": ("infra_brain.agents.net", "NetAgent"),
    "cicd": ("infra_brain.agents.cicd", "CICDAgent"),
    "octopus": ("infra_brain.agents.octopus", "OctopusAgent"),
    "knowledge": ("infra_brain.agents.knowledge", "KnowledgeAgent"),
    "local_docs": ("infra_brain.agents.local_docs", "LocalDocsAgent"),
    # Fenced personal-domain content (Hermes wiki) — see agents/personal_wiki.py
    # module docstring and embeddings.py's _PERSONAL_DOMAIN_SOURCES. Off by
    # default: gated behind rag_enabled AND its own
    # personal_wiki_ingest_enabled flag AND personal_wiki_root.
    "personal_wiki": ("infra_brain.agents.personal_wiki", "PersonalWikiAgent"),
    "vuln": ("infra_brain.agents.vuln", "VulnAgent"),
    "eol": ("infra_brain.agents.eol", "EOLAgent"),
    "backup": ("infra_brain.agents.backup", "BackupAgent"),
    "licensing": ("infra_brain.agents.licensing", "LicensingAgent"),
    "fleet_health": ("infra_brain.agents.fleet_health", "FleetHealthReporter"),
    "iac": ("infra_brain.agents.iac", "IaCAgent"),
    "vsphere": ("infra_brain.agents.vsphere", "VsphereAgent"),
    "container_registry": ("infra_brain.agents.container_registry", "ContainerRegistryAgent"),
    "drift": ("infra_brain.agents.drift", "DriftDetector"),
    "notification": ("infra_brain.agents.notification", "NotificationAgent"),
    "discovery": ("infra_brain.agents.discovery", "DiscoveryAgent"),
    "drift_learning": ("infra_brain.agents.drift_learning", "DriftLearningAgent"),
    "inventory_reconcile": (
        "infra_brain.agents.inventory_reconcile",
        "InventoryReconcileAgent",
    ),
    "remediation": ("infra_brain.agents.remediation", "RemediationAgent"),
    "vuln_triage": ("infra_brain.agents.vuln_triage", "VulnTriageAgent"),
    "compliance": ("infra_brain.agents.compliance", "ComplianceAgent"),
    "rootcause": ("infra_brain.agents.rootcause", "RootCauseAgent"),
    "learning_feedback": ("infra_brain.agents.learning_feedback", "LearningFeedbackAgent"),
    "host_reconcile": ("infra_brain.agents.host_reconcile", "HostReconcileAgent"),
    "netdiscovery": ("infra_brain.agents.netdiscovery", "NetDiscoveryAgent"),
    "inventory_mr": ("infra_brain.agents.inventory_mr", "InventoryMRAgent"),
    # GitLab issue #94: PKI/CA chain monitoring — derives tracked CAs from
    # already-collected host_certificates issuer strings.
    "pki": ("infra_brain.agents.pki", "PKIAgent"),
    # GitLab issue #102: IdP (Okta) identity/SSO/RBAC audit — reconciles IdP
    # principals against existing per-system access grants (vSphere/Octopus/
    # etc convergence nodes) via IS_PRINCIPAL_FOR edges.
    "identity": ("infra_brain.agents.identity", "IdentityAgent"),
    "secrets_inventory": ("infra_brain.agents.secrets_inventory", "SecretsInventoryAgent"),
    # knowledge-graph branch agents — resolved lazily like everything else
    # here; a missing/broken module degrades just this one domain (see
    # _LazyAgentRegistry.__contains__/_resolve below), replacing the old
    # module-level try/except import guards with the same fail-open behavior.
    "coverage": ("infra_brain.agents.coverage", "CoverageAgent"),
    "graph_maintenance": ("infra_brain.agents.graph_maintenance", "GraphMaintenanceAgent"),
    "query": ("infra_brain.agents.query", "QueryAgent"),
    # GitLab #99: reasoner over vsphere_datastore_metrics history, gated by
    # capacity_forecast_enabled (default False) — see agents/capacity_forecast.py.
    "capacity_forecast": (
        "infra_brain.agents.capacity_forecast",
        "CapacityForecastAgent",
    ),
    # GitLab issue #100: LB/reverse-proxy/CDN collector (F5/nginx Plus/
    # HAProxy/Cloudflare). dispatchable=True (scheduled like every other
    # collector); idle-safe via CollectorSkipped when lb_enabled=False
    # (the config.py default, since no vendor credentials are configured
    # yet) — see agents/loadbalancer.py.
    "loadbalancer": ("infra_brain.agents.loadbalancer", "LoadBalancerAgent"),
    # SaaS / API-key inventory (GitLab #103) — metadata-only collector, clean
    # no-op until saas_admin_url is configured.
    "saas_inventory": ("infra_brain.agents.saas_inventory", "SaaSInventoryAgent"),
    # Home-lab monitoring/observability collectors, added directly on operator
    # request ("develop integrations... building new collectors"). Every one
    # is idle-safe (CollectorSkipped) until its *_url setting is configured.
    "homelab_services": ("infra_brain.agents.homelab_services", "HomelabServicesAgent"),
    "prometheus": ("infra_brain.agents.prometheus", "PrometheusAgent"),
    "grafana": ("infra_brain.agents.grafana", "GrafanaAgent"),
    "alertmanager": ("infra_brain.agents.alertmanager", "AlertmanagerAgent"),
    "uptime_kuma": ("infra_brain.agents.uptime_kuma", "UptimeKumaAgent"),
    "wazuh": ("infra_brain.agents.wazuh", "WazuhAgent"),
}

# Domains whose import failure is expected/tolerated (optional dependency or
# incremental knowledge-graph branch work) — degrade just that domain rather
# than raising. Any OTHER domain's import failure must be loud (F-009: a
# silently-vanishing whole domain was a real regression before).
_OPTIONAL_DOMAINS = {"coverage", "graph_maintenance", "query"}


class _LazyAgentRegistry(Mapping):
    """Dict-like view over ``_AGENT_SPECS`` that imports an agent's module
    only on first access to that domain, caching the resolved class.

    Supports the read-only dict operations the rest of the codebase already
    relies on (``in``, ``[...]``, ``.keys()``, ``.items()``, ``len()``,
    iteration) via ``collections.abc.Mapping``, so this is a drop-in
    replacement for the old plain ``dict``.
    """

    def __init__(self, specs: dict[str, tuple[str, str]]):
        self._specs = specs
        self._resolved: dict[str, type[ETLConnector]] = {}
        self._unavailable: set[str] = set()

    def _resolve(self, domain: str) -> type[ETLConnector]:
        if domain in self._resolved:
            return self._resolved[domain]
        if domain not in self._specs or domain in self._unavailable:
            raise KeyError(domain)
        module_path, class_name = self._specs[domain]
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
        except ImportError:
            if domain in _OPTIONAL_DOMAINS:
                logger.warning(
                    "optional agent domain %r failed to import — domain unavailable",
                    domain,
                    exc_info=True,
                )
                self._unavailable.add(domain)
                raise KeyError(domain) from None
            # A non-optional domain failing to import must be LOUD (F-009) —
            # never silently vanish a whole domain.
            raise
        self._resolved[domain] = cls
        return cls

    def __getitem__(self, domain: str) -> type[ETLConnector]:
        return self._resolve(domain)

    def __contains__(self, domain: object) -> bool:
        if not isinstance(domain, str) or domain not in self._specs:
            return False
        try:
            self._resolve(domain)
            return True
        except KeyError:
            return False

    def __iter__(self) -> Iterator[str]:
        for domain in self._specs:
            if domain in self:
                yield domain

    def __len__(self) -> int:
        return sum(1 for _ in self)


AGENT_REGISTRY: Mapping[str, type[ETLConnector]] = _LazyAgentRegistry(_AGENT_SPECS)


class _LazySkipHookSet:
    """Set-like view of "domains whose class declares ``skip_hook = True``".

    Deliberately NOT a plain ``set`` built by eagerly resolving every domain
    at import time — that would force-import all ~30 agent modules just to
    populate this, defeating AGENT_REGISTRY's laziness. Membership tests
    (``domain in SKIP_HOOK`` — the only thing dispatch()/tests actually do)
    resolve only the one domain being asked about.
    """

    def __init__(self, registry: "_LazyAgentRegistry"):
        self._registry = registry

    def __contains__(self, domain: object) -> bool:
        if not isinstance(domain, str) or domain not in self._registry:
            return False
        return bool(getattr(self._registry[domain], "skip_hook", False))

    def __iter__(self) -> Iterator[str]:
        for domain in self._registry:
            if domain in self:
                yield domain

    def __len__(self) -> int:
        return sum(1 for _ in self)


# Domains that must NOT trigger the post-collection hook: they are run BY the
# hook (drift, notification — no recursion) or are on-demand analysis agents.
# Computed from each domain's declarative skip_hook class attribute (single
# source of truth — see etl/base.py's ETLConnector) instead of a
# hand-maintained literal kept in sync by hand.
SKIP_HOOK = _LazySkipHookSet(AGENT_REGISTRY)


def _post_collection_hook(result: CollectionResult | None = None) -> None:
    """Run drift detection + notifications after every collection.

    Also triggers a lightweight GraphMaintenanceAgent pass (scope="quick") when
    the module is available, so the knowledge graph stays fresh after each sweep.

    B3 (revised after adversarial review): the config-drift scan is scoped to
    just *result*'s own run_id here — full-fleet drift detection on every one
    of the ~96/day 15-minute-pulse-driven hook calls was the actual N+1 cost.
    Scoping removes the system's only full-fleet catch-up as a side effect
    (detect_all() was previously called ONLY from here, unscoped), so a new
    daily unscoped detect_all() job is registered in scheduler.py to cover
    SKIP_HOOK domains and anything touched by a run other than the one that
    most recently changed it.

    detect_state_drift(), notify_all(), and the GraphMaintenanceAgent quick
    pass below are deliberately left fleet-wide/unscoped in this pass —
    scoping those is out of scope for B3; this is a known remaining cost, not
    an oversight.
    """
    detector = DriftDetector()
    # vSphere's 15-minute pulse writes snapshots for essentially the whole
    # VM/ESXi fleet (etl/base.py's run()/`_write_snapshot` path is the same
    # for a pulse as a full sweep), so run_id-scoping the drift query alone
    # barely shrinks a pulse-triggered scan — it would still be
    # full-fleet-equivalent every 15 minutes. Skip the scoped drift scan
    # entirely for a pulse; the daily full-fleet catch-up job still covers it.
    if result is not None and getattr(result, "scope", None) == "pulse":
        logger.debug(
            "[post-collection-hook] scope=pulse (domain=%s) — skipping the "
            "per-collection drift scan; covered by the daily full-fleet job",
            getattr(result, "domain", "?"),
        )
    elif result is not None:
        detector.detect_all(run_id=result.run_id, scoped=True)
    else:
        # No result to scope to (defensive — dispatch() always passes one).
        # Fall back to the old unscoped behaviour rather than silently
        # skipping drift detection.
        detector.detect_all()
    detector.detect_state_drift()
    notifier = NotificationAgent()
    notifier.notify_all()
    # TRK-242 (GitLab #113): incident-management correlation. Best-effort like
    # notify_all() above — a failure here must not break the post-collection
    # hook, and notify_incidents() itself is a clean no-op when the ops
    # webhook is unconfigured.
    try:
        notifier.notify_incidents()
    except Exception:
        logger.error("[post-collection-hook] notify_incidents() raised", exc_info=True)
    # graph_maintenance is an optional-dependency domain resolved lazily via
    # AGENT_REGISTRY (see _LazyAgentRegistry/_OPTIONAL_DOMAINS above) — absence
    # degrades just this quick pass, not the whole hook.
    graph_maintenance_cls = AGENT_REGISTRY.get("graph_maintenance")
    if graph_maintenance_cls is not None:
        try:
            graph_maintenance_cls().collect(scope="quick")
        except Exception:
            logger.warning("GraphMaintenanceAgent quick pass failed", exc_info=True)


def dispatch(domain: str, trigger_type: str = "scheduled", scope: str = "all") -> CollectionResult:
    """Instantiate the registered collector for *domain*, run it, run the post-hook.

    Plain, honest, synchronous — the caller (scheduler cron / webhook background
    task) owns concurrency and dedup. Raises ValueError on an unknown domain.

    Before dispatching, consults a live ``dispatchable__<domain>`` RuntimeConfig
    override (TRK-303 part 5/7); when it resolves to False, the run is skipped
    without instantiating the agent or running the post-collection hook.

    A ``retired`` domain (``AgentSpec.retired``) raises ValueError rather than
    recording a skip. Retirement is a standing decision that the upstream
    system does not exist here, so asking to collect it is a caller mistake
    worth surfacing — and, unlike the pause lever below, it deliberately writes
    NO ``collection_runs`` row: a permanently-off collector reporting "skipped"
    on every cycle is precisely the noise the flag exists to remove.
    """
    if domain not in AGENT_REGISTRY:
        raise ValueError(f"Unknown domain: {domain}. Valid: {list(AGENT_REGISTRY)}")
    if is_retired(domain):
        raise ValueError(
            f"Domain {domain!r} is retired (AgentSpec.retired=True) and is not collected. "
            f"Set COLLECTION_REVIVED_DOMAINS={domain} to turn it back on."
        )
    if dispatchable_override(domain) is False:
        logger.info(
            "dispatch(%s) skipped — dispatchable__%s runtime override is false", domain, domain
        )
        # Persist a real (minimal) collection_runs row rather than returning a
        # purely in-memory result with a fabricated run_id: the MCP roster and
        # the dashboard both read last_run from collection_runs, so an
        # unpersisted skip makes a paused domain look dormant/unwired instead
        # of paused. Fail-open — if the write fails we still skip the run,
        # just with a local-only id.
        run_id = record_paused_run(domain, trigger_type=trigger_type) or uuid.uuid4()
        return CollectionResult(
            run_id=run_id,
            domain=domain,
            resources_found=0,
            drift_count=0,
            status=PAUSED_RUN_STATUS,
            scope=scope,
        )
    agent = AGENT_REGISTRY[domain]()
    result = agent.run(trigger_type=trigger_type, scope=scope)
    if domain not in SKIP_HOOK:
        _post_collection_hook(result)
    return result
