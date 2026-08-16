"""PrometheusAgent — scrape-target health and firing-alert collection.

New home-lab monitoring/observability domain (operator-requested, additive
beyond the convergence plan — not one of the roadmap's tracked GitLab
issues). Collects real structured data from the operator's actual Prometheus
deployment (see the Hermes wiki's `entities/services/prometheus.md`:
`monitoring-prometheus` container on node_a, port 9090, scraping Grafana
Alloy / blackbox-exporter / cAdvisor / node exporters across the fleet) via
Prometheus's stable HTTP API
(https://prometheus.io/docs/prometheus/latest/querying/api/):

  * ``GET /api/v1/targets`` — one Resource item per *active* scrape target
    (``type="prometheus_target"``), health up/down/unknown, last scrape
    time, and which scrape pool/job it belongs to. Dropped targets
    (relabeled away before scraping) are not emitted — they carry no
    ``health`` field and are not part of "scrape target health".
  * ``GET /api/v1/alerts`` — one Resource item per currently firing/pending
    alert (``type="prometheus_alert"``). An empty list is a normal healthy
    response, not an error.

Read-only via ``tools/prometheus_tool.py``, which only ever issues GET
requests (structural read-only, ``ReadOnlyClient`` via
``tools/http_readonly.py`` — see ``docs/READONLY-MODEL.md``). Every existing
collector in this codebase writes into the shared ``Resource`` table via
``ETLConnector.run()`` — no new DB model is needed here, so this agent
declares no ``_detail_writers`` override (the base no-op is correct).

Self-skips cleanly (``CollectorSkipped``) when ``prometheus_url`` is
unconfigured — mirrors ``WindowsAgent``'s unconfigured-credential gate and
``tools/ansible.py``'s ``ZERO_HOST_INVENTORY_MARKER`` self-skip convention.
Each of the two API calls is independent and tolerant of the other failing
(e.g. ``/api/v1/alerts`` down while ``/api/v1/targets`` is up still returns
whatever succeeded) — a single failed call is logged as a warning and
recorded in ``CollectOutcome.errors``, never raised, so it cannot abort the
whole ``collect()``.
"""

import logging
from datetime import timedelta

from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.prometheus_tool import (
    prometheus_alerts_tool,
    prometheus_targets_tool,
)

logger = logging.getLogger(__name__)


class PrometheusAgent(ETLConnector):
    """Collects Prometheus scrape-target health and currently-firing alerts."""

    spec = AgentSpec(
        domain="prometheus",
        tier=Tier.COLLECTOR,
        # Every 10 minutes — monitoring/alert data goes stale fast (unlike the
        # once-daily inventory-style collectors elsewhere in this codebase);
        # 10 min keeps target/alert freshness reasonable without hammering a
        # single home-lab Prometheus instance. max_staleness gives it one
        # missed cycle of slack before being flagged stale.
        schedule="3,13,23,33,43,53 * * * *",
        max_staleness=timedelta(minutes=30),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        base_url = (self.settings.prometheus_url or "").strip()
        if not base_url:
            raise CollectorSkipped(
                "prometheus_url not configured; set PROMETHEUS_URL to enable "
                "Prometheus collection"
            )

        _cb = {"callbacks": self.callbacks}
        items: list[dict] = []
        errors: list[str] = []

        try:
            targets_resp = prometheus_targets_tool.invoke({}, config=_cb)
            for target in self._extract_active_targets(targets_resp):
                items.append(self._target_to_item(target))
        except Exception as exc:
            logger.warning("PrometheusAgent: /api/v1/targets fetch failed: %s", exc)
            errors.append(f"targets fetch failed: {exc}")

        try:
            alerts_resp = prometheus_alerts_tool.invoke({}, config=_cb)
            for alert in self._extract_alerts(alerts_resp):
                items.append(self._alert_to_item(alert))
        except Exception as exc:
            logger.warning("PrometheusAgent: /api/v1/alerts fetch failed: %s", exc)
            errors.append(f"alerts fetch failed: {exc}")

        return CollectOutcome(items=items, errors=errors)

    @staticmethod
    def _extract_active_targets(resp) -> list:
        """Pull ``data.activeTargets`` out of the /api/v1/targets envelope.

        Tolerant of a malformed/unexpected response shape (missing keys,
        non-dict) — returns an empty list rather than raising, since this is
        called after the tool invocation already succeeded (a shape surprise
        here is not the same failure mode as the HTTP call itself failing).
        """
        if not isinstance(resp, dict):
            return []
        data = resp.get("data") or {}
        if not isinstance(data, dict):
            return []
        return data.get("activeTargets") or []

    @staticmethod
    def _extract_alerts(resp) -> list:
        """Pull ``data.alerts`` out of the /api/v1/alerts envelope (see ``_extract_active_targets``)."""
        if not isinstance(resp, dict):
            return []
        data = resp.get("data") or {}
        if not isinstance(data, dict):
            return []
        return data.get("alerts") or []

    @staticmethod
    def _target_to_item(target: dict) -> dict:
        """One Resource item per active scrape target.

        ``labels`` (job/instance/...) sits one level down in the real
        Prometheus response; ``scrapePool``/``lastScrape``/``health`` are
        top-level. All fields are optional per-target in practice (a target
        can appear with an empty ``lastScrape`` before its first successful
        scrape) — every read below tolerates a missing key.
        """
        labels = target.get("labels") or {}
        job = labels.get("job", "")
        instance = labels.get("instance", "")
        name = f"{job}/{instance}" if (job or instance) else (target.get("scrapePool") or "unknown")
        return {
            "name": name,
            "type": "prometheus_target",
            "data": {
                "job": job,
                "instance": instance,
                "health": target.get("health", "unknown"),
                "last_scrape": target.get("lastScrape", ""),
                "scrape_pool": target.get("scrapePool", ""),
            },
        }

    @staticmethod
    def _alert_to_item(alert: dict) -> dict:
        """One Resource item per currently firing/pending alert.

        ``alertname`` is a label, not a top-level field, per the Prometheus
        alerts API.
        """
        labels = alert.get("labels") or {}
        alertname = labels.get("alertname") or "unknown_alert"
        return {
            "name": alertname,
            "type": "prometheus_alert",
            "data": {
                "state": alert.get("state", "unknown"),
                "labels": labels,
                "annotations": alert.get("annotations") or {},
                "active_since": alert.get("activeAt", ""),
            },
        }
