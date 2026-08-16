"""GrafanaAgent — home-lab observability collector (operator-requested, additive
scope beyond the convergence plan).

Collects real structured data from the home lab's Grafana instance
(monitoring-grafana on node_a, port 3002 — see
~/.hermes/wiki/entities/services/grafana.md): liveness, dashboard inventory,
and active alerts. Every item is a generic ``Resource`` row (name/type/data)
written by ``ETLConnector.run()`` — no new DB model needed, matching every
other collector in this codebase.

Two-tier self-skip (see module docstring on tools/grafana_tool.py for the
endpoint shapes):

  1. ``grafana_url`` unconfigured  -> whole collector is CollectorSkipped
     (there is no base URL to hit at all, not even the anonymous health
     check).
  2. ``grafana_url`` configured but ``grafana_api_token`` is not -> the
     unauthenticated GET /api/health call is STILL attempted (Grafana's
     health endpoint is anonymous-accessible on a standard install) and, if
     it succeeds, is returned as a single-item CollectOutcome with no
     errors. The dashboard-search and alerts calls (which need a token on
     any instance that isn't wide open) are skipped with a logged reason,
     not attempted and not counted as a failure — this collector reports
     partial data with zero credentials configured, rather than either
     fully self-skipping or fully failing.

Within the authenticated tier, dashboards and alerts are fetched
independently — one failing (e.g. this Grafana version's alerting API shape
differs) does not prevent the other, or the health item, from being
reported.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.grafana_tool import (
    grafana_alerts_tool,
    grafana_dashboards_tool,
    grafana_health_tool,
)

logger = logging.getLogger(__name__)


class GrafanaAgent(ETLConnector):
    """Collects Grafana health, dashboards, and active alerts (read-only)."""

    spec = AgentSpec(
        domain="grafana",
        tier=Tier.COLLECTOR,
        schedule="7,22,37,52 * * * *",
        max_staleness=timedelta(hours=1),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        base_url = (self.settings.grafana_url or "").strip()
        if not base_url:
            raise CollectorSkipped("grafana_url not configured")

        # H-3: this token is read ONLY to decide whether the authenticated tier
        # runs. It is deliberately never passed to a tool — the tools resolve it
        # from settings themselves (tools/grafana_tool.py::_auth_headers),
        # because callbacks/audit.py persists tool arguments verbatim into the
        # dashboard-queryable audit trail and DLP does not redact API tokens.
        token = (self.settings.grafana_api_token or "").strip()
        _cb = {"callbacks": self.callbacks}

        items: list[dict] = []
        errors: list[str] = []

        # --- Tier 1: /api/health — anonymous, always attempted. -------------
        try:
            health = grafana_health_tool.invoke({"base_url": base_url}, config=_cb)
            items.append(
                {
                    "name": "grafana",
                    "type": "grafana_health",
                    "data": {
                        "version": health.get("version", ""),
                        "database": health.get("database", ""),
                    },
                }
            )
        except Exception as exc:
            logger.warning("Grafana health check failed: %s", exc)
            errors.append(f"health check failed: {exc}")

        # --- Tier 2: token-gated calls. ---------------------------------
        if not token:
            logger.info(
                "Grafana: grafana_api_token not configured — skipping dashboard/alert "
                "collection; reporting health only"
            )
            return CollectOutcome(items=items, errors=errors)

        try:
            dashboards = grafana_dashboards_tool.invoke({"base_url": base_url}, config=_cb)
            for d in dashboards or []:
                items.append(
                    {
                        "name": d.get("title") or d.get("uid", ""),
                        "type": "grafana_dashboard",
                        "data": {
                            "uid": d.get("uid", ""),
                            "folder": d.get("folderTitle", "") or "",
                            "tags": d.get("tags", []) or [],
                        },
                    }
                )
        except Exception as exc:
            logger.warning("Grafana dashboard search failed: %s", exc)
            errors.append(f"dashboard search failed: {exc}")

        try:
            alerts = grafana_alerts_tool.invoke({"base_url": base_url}, config=_cb)
            for a in alerts or []:
                labels = a.get("labels", {}) or {}
                status = a.get("status", {}) or {}
                items.append(
                    {
                        "name": labels.get("alertname") or a.get("fingerprint", "grafana-alert"),
                        "type": "grafana_alert",
                        "data": {
                            "fingerprint": a.get("fingerprint", ""),
                            "state": status.get("state", ""),
                            "severity": labels.get("severity", ""),
                            "starts_at": a.get("startsAt", ""),
                            "labels": labels,
                            "summary": (a.get("annotations", {}) or {}).get("summary", ""),
                        },
                    }
                )
        except Exception as exc:
            logger.warning("Grafana alerts fetch failed: %s", exc)
            errors.append(f"alerts fetch failed: {exc}")

        return CollectOutcome(items=items, errors=errors)
