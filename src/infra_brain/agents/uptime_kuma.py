"""UptimeKumaAgent — home-lab uptime-monitor status collector (operator-
requested, additive scope beyond the convergence plan — see task #9,
"Monitoring collectors (Prometheus/Grafana/Alertmanager/UptimeKuma/Wazuh)").

Collects real structured data from the home lab's Uptime Kuma instance
(monitoring-uptime-kuma container on node_a, port 3001 — see
~/.hermes/wiki/entities/services/uptime-kuma.md): one Resource item per
monitored service, carrying its current up/down/pending state, rolling 24h
uptime percentage, and last-heartbeat timestamp. Every item is a generic
``Resource`` row (name/type/data) written by ``ETLConnector.run()`` — no new
DB model needed, matching every other collector in this codebase.

API choice: Uptime Kuma's primary interface is a Socket.IO push feed (the
dashboard's live websocket) — not something a read-only, GET-only collector
should hold open. The wiki entry for this instance documents no metrics
endpoint and no specific status-page slug, so this agent uses the
lightweight public status-page REST endpoints instead (module docstring on
``tools/uptime_kuma_tool.py`` has the full real-API response shapes):

  * ``GET /api/status-page/<slug>``           — monitor list + grouping
  * ``GET /api/status-page/heartbeat/<slug>`` — heartbeat history + uptime

**Slug note (needs operator confirmation):** no status-page slug is
documented in the wiki entry for this instance. Uptime Kuma ships a
"default" status page out of the box, so ``uptime_kuma_status_page_slug``
defaults to ``"default"`` here — the integration pass / operator should
confirm the real slug (visible in the Uptime Kuma admin UI under
Settings -> Status Pages, or the status page's own URL path) and override it
via settings if it differs. An unconfigured/wrong slug returns a 404 from
Uptime Kuma, surfaced as a collection error, not a silent empty result.

Self-skips cleanly (``CollectorSkipped``) when ``uptime_kuma_url`` is
unconfigured — mirrors ``WindowsAgent``'s unconfigured-credential gate and
``tools/ansible.py``'s ``ZERO_HOST_INVENTORY_MARKER`` self-skip convention.

The status-page monitor list and the heartbeat/uptime feed are two
independent API calls: if the heartbeat call fails (e.g. this Uptime Kuma
version's heartbeat route differs, or a transient 5xx), the monitor list
from the status-page call is still reported — each monitor item is emitted
with ``status="pending"`` (unknown) and ``uptime_percent``/
``last_heartbeat_time`` left ``None`` — rather than losing the whole
collection to one failed sub-resource. Only a failure of the status-page
call itself (which is where the monitor list/names come from) aborts
collection, since there is nothing to report without it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from infra_brain.etl.base import CollectorSkipped, CollectOutcome, ETLConnector
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.uptime_kuma_tool import (
    uptime_kuma_heartbeat_tool,
    uptime_kuma_status_page_tool,
)

logger = logging.getLogger(__name__)

# Uptime Kuma heartbeat ``status`` integer codes (server/uptime-kuma-server.js
# UP/DOWN/PENDING/MAINTENANCE constants). MAINTENANCE is folded into
# "pending" here — this collector's data contract (per the task spec) only
# has three states (up/down/pending), and "under planned maintenance" is
# closer to "don't treat as a real outage" than to either up or down.
_STATUS_UP = 1
_STATUS_DOWN = 0
_STATUS_PENDING = 2
_STATUS_MAINTENANCE = 3

_STATUS_MAP = {
    _STATUS_UP: "up",
    _STATUS_DOWN: "down",
    _STATUS_PENDING: "pending",
    _STATUS_MAINTENANCE: "pending",
}

_DEFAULT_STATUS_PAGE_SLUG = "default"


class UptimeKumaAgent(ETLConnector):
    """Collects Uptime Kuma monitor status via the public status-page API."""

    spec = AgentSpec(
        domain="uptime_kuma",
        tier=Tier.COLLECTOR,
        # Every 10 minutes — uptime/monitor state goes stale fast (unlike the
        # once-daily inventory-style collectors elsewhere in this codebase);
        # matches PrometheusAgent's cadence for the same reason. max_staleness
        # gives it one missed cycle of slack before being flagged stale.
        schedule="8,18,28,38,48,58 * * * *",
        max_staleness=timedelta(minutes=30),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        base_url = (self.settings.uptime_kuma_url or "").strip()
        if not base_url:
            raise CollectorSkipped(
                "uptime_kuma_url not configured; set UPTIME_KUMA_URL to enable "
                "Uptime Kuma collection"
            )

        slug = (getattr(self.settings, "uptime_kuma_status_page_slug", "") or "").strip()
        slug = slug or _DEFAULT_STATUS_PAGE_SLUG

        _cb = {"callbacks": self.callbacks}

        # --- Monitor list + grouping — required; a failure here aborts the
        # whole collection since there is nothing to report without it. ------
        try:
            status_page = uptime_kuma_status_page_tool.invoke(
                {"base_url": base_url, "slug": slug}, config=_cb
            )
        except Exception as exc:
            logger.warning("UptimeKumaAgent: status page '%s' fetch failed: %s", slug, exc)
            return CollectOutcome(items=[], errors=[f"status page '{slug}' fetch failed: {exc}"])

        monitors = self._extract_monitors(status_page)
        if not monitors:
            return CollectOutcome(items=[], errors=[])

        # --- Heartbeat history + uptime — best-effort; one failure here
        # degrades monitor items to status="pending" rather than losing the
        # whole collection. ---------------------------------------------------
        errors: list[str] = []
        heartbeat_list: dict = {}
        uptime_list: dict = {}
        try:
            heartbeat_resp = uptime_kuma_heartbeat_tool.invoke(
                {"base_url": base_url, "slug": slug}, config=_cb
            )
            heartbeat_list = (heartbeat_resp or {}).get("heartbeatList") or {}
            uptime_list = (heartbeat_resp or {}).get("uptimeList") or {}
        except Exception as exc:
            logger.warning("UptimeKumaAgent: heartbeat feed for '%s' fetch failed: %s", slug, exc)
            errors.append(f"heartbeat feed fetch failed: {exc}")

        items = [
            self._monitor_to_item(monitor, heartbeat_list, uptime_list) for monitor in monitors
        ]
        return CollectOutcome(items=items, errors=errors)

    @staticmethod
    def _extract_monitors(status_page) -> list[dict]:
        """Flatten ``publicGroupList[*].monitorList`` into one list of monitor dicts.

        Tolerant of a malformed/unexpected response shape (missing keys,
        non-dict/non-list values) — returns an empty list rather than
        raising, since this runs after the tool invocation already succeeded.
        """
        if not isinstance(status_page, dict):
            return []
        groups = status_page.get("publicGroupList")
        if not isinstance(groups, list):
            return []
        monitors: list[dict] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            monitor_list = group.get("monitorList")
            if not isinstance(monitor_list, list):
                continue
            monitors.extend(m for m in monitor_list if isinstance(m, dict))
        return monitors

    @staticmethod
    def _monitor_to_item(monitor: dict, heartbeat_list: dict, uptime_list: dict) -> dict:
        """One Resource item per monitor: name/type/data per the task's data contract.

        ``heartbeat_list``/``uptime_list`` are keyed by the monitor's Uptime
        Kuma id (heartbeats as a string key, matching the raw JSON; uptime as
        ``"<id>_24"``). A monitor id missing from either map (heartbeat feed
        failed, or this monitor has no beats yet) degrades to
        ``status="pending"`` with null uptime/last-heartbeat fields rather
        than raising.
        """
        monitor_id = monitor.get("id")
        name = monitor.get("name") or f"monitor-{monitor_id}"

        beats = heartbeat_list.get(str(monitor_id)) or []
        last_beat = beats[-1] if beats else None
        status = _STATUS_MAP.get(
            last_beat.get("status") if isinstance(last_beat, dict) else None, "pending"
        )
        last_heartbeat_time = last_beat.get("time") if isinstance(last_beat, dict) else None
        uptime_percent = uptime_list.get(f"{monitor_id}_24")

        return {
            "name": name,
            "type": "uptime_kuma_monitor",
            "data": {
                "status": status,
                "uptime_percent": uptime_percent,
                "last_heartbeat_time": last_heartbeat_time,
            },
        }
