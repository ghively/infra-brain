"""AlertmanagerAgent -- monitoring/alerting collector for the home-lab Alertmanager.

New collector domain, additive scope beyond the convergence plan (operator
request): real structured data from Alertmanager's HTTP v2 API rather than a
bare health ping -- server status, active alerts, and active silences.

See ~/.hermes/wiki/entities/services/alertmanager.md for this environment's
deployment specifics: ``monitoring-alertmanager`` container on [[node_a]],
port 9093 (127.0.0.1 only), fed by [[prometheus]], routing to [[ntfy]].

Modeled on ``agents/container_registry.py`` (the simpler of the two reference
templates -- no rich per-domain detail tables are needed here, only the
generic ``Resource`` table that every collector's ``collect()`` writes into
via ``etl/base.py``'s ``run()``). Three read-only GETs, each independently
error-tolerant so one failing sub-call never aborts the others:

  * GET /api/v2/status   -> one Resource item (name "alertmanager",
                             type "alertmanager_status")
  * GET /api/v2/alerts   -> one Resource item per active alert
                             (type "alertmanager_alert"); an empty list is
                             success, not an error
  * GET /api/v2/silences -> one Resource item per active silence
                             (type "alertmanager_silence")

Self-skips cleanly via ``CollectorSkipped`` when ``alertmanager_url`` is
unconfigured -- this integration is not yet wired with real credentials in
this home lab, and that is expected (see windows.py / tools/ansible.py's
``ZERO_HOST_INVENTORY_MARKER`` for the same self-skip convention).
"""

import logging
from datetime import timedelta

from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.alertmanager_tool import (
    alertmanager_alerts_tool,
    alertmanager_silences_tool,
    alertmanager_status_tool,
)

logger = logging.getLogger(__name__)


class AlertmanagerAgent(ETLConnector):
    """Collects Alertmanager server status, active alerts, and active silences.

    All three sub-calls are read-only GETs (``tools/alertmanager_tool.py`` ->
    ``readonly_get``). Every sub-call is wrapped independently so a single
    failing endpoint (e.g. silences unreachable) still lets the other two
    succeed -- the collector reports ``CollectOutcome`` with the swallowed
    failures recorded in ``.errors`` rather than aborting the whole collect.
    """

    spec = AgentSpec(
        domain="alertmanager",
        tier=Tier.COLLECTOR,
        # Alerts are near-real-time signal (unlike e.g. Octopus's daily
        # estate sweep) -- a 5-minute cadence keeps active-alert/silence
        # state reasonably fresh without hammering a local single-node
        # Alertmanager.
        schedule="*/5 * * * *",
        max_staleness=timedelta(minutes=20),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        base_url = (self.settings.alertmanager_url or "").strip()
        if not base_url:
            raise CollectorSkipped(
                "alertmanager_url not configured; set ALERTMANAGER_URL to enable "
                "Alertmanager collection"
            )

        _cb = {"callbacks": self.callbacks}
        items: list[dict] = []
        errors: list[str] = []

        items.extend(self._collect_status(_cb, errors))
        items.extend(self._collect_alerts(_cb, errors))
        items.extend(self._collect_silences(_cb, errors))

        return CollectOutcome(items=items, errors=errors)

    # --- per-endpoint collectors (each independently error-tolerant) -------

    def _collect_status(self, _cb: dict, errors: list[str]) -> list[dict]:
        try:
            status = alertmanager_status_tool.invoke({}, config=_cb) or {}
        except Exception as exc:
            logger.warning("AlertmanagerAgent: status fetch failed: %s", exc)
            errors.append(f"status fetch failed: {exc}")
            return []

        version_info = status.get("versionInfo") or {}
        cluster = status.get("cluster") or {}
        return [
            {
                "name": "alertmanager",
                "type": "alertmanager_status",
                "data": {
                    "version": version_info.get("version", ""),
                    "revision": version_info.get("revision", ""),
                    "uptime": status.get("uptime", ""),
                    "cluster_status": cluster.get("status", ""),
                    "cluster_name": cluster.get("name", ""),
                    "peer_count": len(cluster.get("peers") or []),
                },
            }
        ]

    def _collect_alerts(self, _cb: dict, errors: list[str]) -> list[dict]:
        try:
            alerts = alertmanager_alerts_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            logger.warning("AlertmanagerAgent: alerts fetch failed: %s", exc)
            errors.append(f"alerts fetch failed: {exc}")
            return []

        items: list[dict] = []
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            labels = alert.get("labels") or {}
            alertname = labels.get("alertname") or f"alert-{alert.get('fingerprint', 'unknown')}"
            alert_status = alert.get("status") or {}
            items.append(
                {
                    "name": alertname,
                    "type": "alertmanager_alert",
                    "data": {
                        "status": {
                            "state": alert_status.get("state", ""),
                            "silenced_by": alert_status.get("silencedBy") or [],
                            "inhibited_by": alert_status.get("inhibitedBy") or [],
                        },
                        "labels": labels,
                        "annotations": alert.get("annotations") or {},
                        "startsAt": alert.get("startsAt", ""),
                        "endsAt": alert.get("endsAt", ""),
                        "fingerprint": alert.get("fingerprint", ""),
                    },
                }
            )
        return items

    def _collect_silences(self, _cb: dict, errors: list[str]) -> list[dict]:
        try:
            silences = alertmanager_silences_tool.invoke({}, config=_cb) or []
        except Exception as exc:
            logger.warning("AlertmanagerAgent: silences fetch failed: %s", exc)
            errors.append(f"silences fetch failed: {exc}")
            return []

        items: list[dict] = []
        for silence in silences:
            if not isinstance(silence, dict):
                continue
            silence_status = silence.get("status") or {}
            items.append(
                {
                    "name": silence.get("id", ""),
                    "type": "alertmanager_silence",
                    "data": {
                        "matchers": silence.get("matchers") or [],
                        "startsAt": silence.get("startsAt", ""),
                        "endsAt": silence.get("endsAt", ""),
                        "createdBy": silence.get("createdBy", ""),
                        "comment": silence.get("comment", ""),
                        "state": silence_status.get("state", ""),
                    },
                }
            )
        return items
