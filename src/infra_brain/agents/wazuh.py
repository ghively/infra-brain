"""WazuhAgent — Wazuh SIEM security-monitoring collector (home-lab, node_a).

New collector domain, additive beyond the convergence plan, requested
directly by the operator: real Wazuh SIEM integration (agent inventory +
recent alerts), not a bare health ping. Deployment specifics — 3-container
Docker stack, manager at 203.0.113.15, Wazuh 4.14.5 — are documented in
``~/.hermes/wiki/entities/services/wazuh.md``.

Collects via the Wazuh Manager REST API (``tools/wazuh_tool.py``):

  * GET /agents  -> one Resource item per registered Wazuh agent
                    (type "wazuh_agent", data: status/os/version/last_keep_alive)
  * GET /alerts  -> one Resource item per recent alert in the configured
                    lookback window (type "wazuh_alert", data: rule_level/
                    agent_name/timestamp/description)

ENDPOINT CHOICE for "recent alerts" (documented per task instructions, since
the Wazuh API offers no single obviously-correct answer here): Wazuh 4.x's
Manager REST API (port 55000) is primarily agent/rule/config inventory —
full alert *search* moved to the Wazuh Indexer (OpenSearch, ``wazuh-alerts-*``
index, port 9200 in this deployment) with the Elastic/OpenSearch-stack
architecture Wazuh adopted from 4.0 onward.

RESOLVED 2026-08-09 — this is that prescribed follow-up. The Manager-API bet
did not pay off: against the real 4.14.5 install it 404'd on EVERY run, 526
consecutive times, because port 55000 has no ``/alerts`` route at all. When
``wazuh_indexer_url`` is set the collector now queries
``{indexer_url}/{wazuh_indexer_alerts_index}/_search`` (see
``tools/wazuh_tool.wazuh_indexer_alerts_tool``). The Manager-API path is kept
as the unconfigured fallback, since a deployment MAY front the indexer with a
proxy that exposes ``/alerts``.

An index pattern matching zero indices is treated as an ERROR, not as an empty
result. On this fleet that is exactly the live state: the indexer holds only
``wazuh-monitoring-*``/``wazuh-statistics-*``, so the manager is shipping no
alerts anywhere and the SIEM is recording no detections. Reporting that as a
clean "completed, 0 alerts" would be indistinguishable from a genuinely quiet
estate — the precise blind spot a security collector must never manufacture.

Auth: Wazuh requires a JWT via ``POST /security/user/authenticate`` (Basic
auth) before any other call. ``get_wazuh_token()`` (in ``tools/wazuh_tool.py``)
authenticates ONCE per ``collect()`` call — Wazuh tokens are typically valid
~15 minutes, and every run re-authenticates rather than reusing a previous
run's token — and that same token is reused for both the ``/agents`` and
``/alerts`` fetches in that run.

H-3: the JWT (and the Indexer's username/password) are NEVER passed as tool
ARGUMENTS. ``callbacks/audit.py`` persists ``args_summary = input_str[:256]``
verbatim into ``AgentActionLog``/``AuditLog`` — both queryable from the
dashboard — and DLP only scans for PANs, so anything handed to ``tool.invoke``
is a permanent cleartext copy of that credential. The tools resolve their own
credentials from settings instead (the pattern ``tools/prometheus_tool.py`` and
``tools/alertmanager_tool.py`` already use, and the one
``agents/secrets_inventory.py`` documents). ``get_wazuh_token`` is still called
here, up-front and outside the try blocks, so a bad credential still fails the
whole run rather than being downgraded to a partial.

Self-skip vs. real failure:
  * wazuh_url / wazuh_username / wazuh_password unconfigured -> CollectorSkipped
    (clean no-op, the standard WindowsAgent/ansible.py convention).
  * Authentication itself failing (bad credentials, manager unreachable) is
    NOT a self-skip -- it propagates as RuntimeError so BaseAgent.run()
    records status="failed". A silent auth failure on a security-monitoring
    collector is exactly the kind of gap Wazuh exists to catch elsewhere.
  * A failure fetching /agents or /alerts AFTER a successful auth is reported
    in CollectOutcome.errors (partial-failure tolerance) rather than aborting
    the other sub-fetch.
"""

import logging
from datetime import datetime, timedelta, timezone

from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.wazuh_tool import (
    get_wazuh_token,
    wazuh_agents_tool,
    wazuh_alerts_tool,
    wazuh_indexer_alerts_tool,
)

log = logging.getLogger(__name__)


class WazuhAgent(ETLConnector):
    """Collects Wazuh SIEM agent inventory + recent security alerts."""

    spec = AgentSpec(
        domain="wazuh",
        tier=Tier.COLLECTOR,
        schedule="12,27,42,57 * * * *",
        max_staleness=timedelta(hours=1),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        base_url = (self.settings.wazuh_url or "").strip()
        username = (self.settings.wazuh_username or "").strip()
        password = self.settings.wazuh_password or ""
        if not base_url or not username or not password:
            raise CollectorSkipped(
                "wazuh_url/wazuh_username/wazuh_password not configured; set all three to "
                "enable Wazuh SIEM collection"
            )

        ssl_verify = getattr(self.settings, "wazuh_ssl_verify", True)
        timeout = getattr(self.settings, "api_timeout_seconds", 30)
        _cb = {"callbacks": self.callbacks}

        # Auth failure is a real failure, not a self-skip — see module docstring.
        # Deliberately NOT caught here: it must propagate out of collect() so
        # BaseAgent.run() records status="failed".
        #
        # H-3: the returned JWT is intentionally NOT passed on to any tool. This
        # call both validates the credentials (so a failure still fails the run,
        # loudly, here) and publishes the token in-process for the Manager tools
        # to pick up — see tools/wazuh_tool.py's `_active_manager_token`. Tool
        # arguments are persisted verbatim by callbacks/audit.py into the
        # dashboard-queryable audit trail, and DLP only scans for PANs, so a
        # credential passed as a tool argument leaks in cleartext on every run.
        get_wazuh_token(self.settings)

        items: list[dict] = []
        errors: list[str] = []

        try:
            agents_payload = wazuh_agents_tool.invoke(
                {
                    "base_url": base_url,
                    "ssl_verify": ssl_verify,
                    "timeout": timeout,
                    "limit": getattr(self.settings, "wazuh_agents_cap", 500),
                },
                config=_cb,
            )
            for a in self._extract_items(agents_payload):
                os_info = a.get("os") or {}
                items.append(
                    {
                        "name": a.get("name") or a.get("id") or "unknown-agent",
                        "type": "wazuh_agent",
                        "data": {
                            "status": a.get("status", ""),
                            "os": os_info.get("name") or os_info.get("platform") or "",
                            "version": a.get("version", ""),
                            "last_keep_alive": a.get("lastKeepAlive"),
                        },
                    }
                )
        except Exception as exc:
            log.warning("Wazuh agents fetch failed: %s", exc)
            errors.append(f"agents fetch failed: {exc}")

        indexer_url = (getattr(self.settings, "wazuh_indexer_url", "") or "").strip()
        try:
            hours = getattr(self.settings, "wazuh_alerts_window_hours", 24)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            since_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
            limit = getattr(self.settings, "wazuh_alerts_cap", 200)

            if indexer_url:
                # Wazuh 4.x: alert SEARCH lives in the Indexer, not the Manager
                # API. See wazuh_indexer_alerts_tool.
                index_pattern = (
                    getattr(self.settings, "wazuh_indexer_alerts_index", "") or "wazuh-alerts-*"
                )
                alerts_payload = wazuh_indexer_alerts_tool.invoke(
                    {
                        # H-3: indexer username/password are NOT passed here —
                        # the tool reads them from settings itself, so they never
                        # land in the audit trail. See tools/wazuh_tool.py.
                        "indexer_url": indexer_url,
                        "ssl_verify": getattr(self.settings, "wazuh_indexer_ssl_verify", True),
                        "timeout": timeout,
                        "since_iso": since_iso,
                        "index_pattern": index_pattern,
                        "limit": limit,
                    },
                    config=_cb,
                )
                # An index pattern matching NOTHING is a live finding, not an
                # empty-but-healthy result: it means the manager is not shipping
                # alerts into the indexer at all, so this SIEM is recording no
                # detections. Reported as an error so the run stays "partial"
                # and stays visible — a silent "completed, 0 alerts" here would
                # look identical to a genuinely quiet estate, which is the exact
                # blind spot a security collector must never create.
                shards_total = ((alerts_payload or {}).get("_shards") or {}).get("total")
                if shards_total == 0:
                    msg = (
                        f"alerts index pattern {index_pattern!r} matches NO indices on "
                        f"{indexer_url} — the Wazuh manager is not shipping alerts to the "
                        "indexer, so no detections are being recorded anywhere. Agent "
                        "inventory below is unaffected."
                    )
                    log.error("Wazuh: %s", msg)
                    errors.append(msg)
            else:
                # No indexer configured — fall back to the Manager API. Retained
                # only because some deployments front the indexer with a proxy
                # that does expose /alerts; on a stock 4.x install this 404s.
                alerts_payload = wazuh_alerts_tool.invoke(
                    {
                        "base_url": base_url,
                        "ssl_verify": ssl_verify,
                        "timeout": timeout,
                        "since_iso": since_iso,
                        "limit": limit,
                    },
                    config=_cb,
                )

            for al in self._extract_items(alerts_payload):
                rule = al.get("rule") or {}
                agent = al.get("agent") or {}
                description = rule.get("description") or ""
                name = description or al.get("id") or "wazuh-alert"
                items.append(
                    {
                        "name": name,
                        "type": "wazuh_alert",
                        "data": {
                            "rule_level": rule.get("level"),
                            "agent_name": agent.get("name", ""),
                            "timestamp": al.get("timestamp"),
                            "description": description,
                        },
                    }
                )
        except Exception as exc:
            log.warning("Wazuh alerts fetch failed: %s", exc)
            errors.append(f"alerts fetch failed: {exc}")

        return CollectOutcome(items=items, errors=errors)

    @staticmethod
    def _extract_items(payload) -> list[dict]:
        """Normalize a Wazuh 4.x ``{"data": {"affected_items": [...]}}`` response
        envelope (or a bare list, defensively) into a plain list of item dicts.

        Also handles the OpenSearch ``{"hits": {"hits": [{"_source": {...}}]}}``
        envelope the Indexer returns for alert search — a different shape from
        the Manager API's, and the per-alert fields the caller reads
        (``rule``/``agent``/``timestamp``) live inside ``_source``, so without
        this branch every indexer alert would flatten to an empty item.
        """
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            hits = payload.get("hits")
            if isinstance(hits, dict) and isinstance(hits.get("hits"), list):
                return [
                    (h.get("_source") or {}) for h in hits["hits"] if isinstance(h, dict)
                ]
            data = payload.get("data")
            if isinstance(data, dict):
                return data.get("affected_items", []) or []
            if isinstance(data, list):
                return data
        return []
