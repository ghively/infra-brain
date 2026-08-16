"""Uptime Kuma status-page API tools — read-only.

New collector domain (operator-requested, additive beyond the convergence
plan): real uptime-monitoring data from the home lab's Uptime Kuma instance
(monitoring-uptime-kuma container on node_a, port 3001 — see
~/.hermes/wiki/entities/services/uptime-kuma.md).

Uptime Kuma's primary interface for live monitor state is a Socket.IO push
feed (the dashboard's websocket connection) — not GET-friendly and not
something a read-only collector should hold a persistent connection open
for. Every status page Uptime Kuma serves, however, also exposes two
lightweight public REST endpoints requiring no authentication (status pages
are the product's intentionally-public surface, meant to be embedded/shared
without a login):

Endpoints used (all GET, read-only):
  GET /api/status-page/<slug>            -> monitor list + grouping
  GET /api/status-page/heartbeat/<slug>  -> per-monitor heartbeat history + uptime

Real API response shape (Uptime Kuma's ``server/routers/api-router.js`` —
the ``/api/status-page/:slug`` and ``/api/status-page/heartbeat/:slug``
routes), current as of the 1.19+ series this home lab runs:

    GET /api/status-page/<slug>::

        {
          "config": {"slug": "default", "title": "...", ...},
          "incident": null,
          "publicGroupList": [
            {
              "id": 1,
              "name": "Group name",
              "weight": 1,
              "monitorList": [
                {"id": 1, "name": "Monitor name", "sendUrl": 0, "type": "http"}
              ]
            }
          ]
        }

    GET /api/status-page/heartbeat/<slug>::

        {
          "heartbeatList": {
            "1": [
              {"status": 1, "time": "2026-07-31 12:00:00", "msg": "", "ping": 42}
            ]
          },
          "uptimeList": {
            "1_24": 0.9931
          }
        }

``heartbeatList`` is keyed by monitor id (string) -> list of heartbeat rows,
oldest first; the last element is the most recent beat. ``uptimeList`` is
keyed ``"<monitorId>_24"`` for the rolling 24h uptime ratio (0.0-1.0) — the
only window the public status-page feed exposes (the dashboard-only,
authenticated Socket.IO API adds longer windows, not reachable here).

Heartbeat ``status`` codes (Uptime Kuma's ``UP``/``DOWN``/``PENDING``/
``MAINTENANCE`` constants in ``server/uptime-kuma-server.js``):
0=DOWN, 1=UP, 2=PENDING, 3=MAINTENANCE.

No authentication is attempted — status pages are Uptime Kuma's
intentionally-public, unauthenticated surface, unlike the dashboard API.

F-02 pattern reuse (per tools/octopus_tool.py): tenacity retry on transient
HTTP errors (timeout, connect error, 5xx) only. 4xx (bad/unknown slug) is
never retried.
"""

from __future__ import annotations

import httpx
from langchain_core.tools import tool
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from infra_brain.config import get_settings
from infra_brain.tools.http_readonly import readonly_get, tool_http_errors


def _is_transient_http(exc: BaseException) -> bool:
    """Return True for transient errors that warrant a retry.

    Transient: network timeouts, connect errors, and 5xx server errors.
    Never retry 4xx (unknown/misconfigured status-page slug).
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


_http_retry = retry(
    retry=retry_if_exception(_is_transient_http),
    wait=wait_exponential_jitter(initial=1, max=60),
    stop=stop_after_attempt(4),
)


@_http_retry
def _uptime_kuma_raw_get(base_url: str, path: str) -> httpx.Response:
    """GET against the Uptime Kuma public status-page API (read-only by
    construction via ``readonly_get``). No auth header — status pages are
    unauthenticated by design."""
    resp = readonly_get(
        f"{base_url.rstrip('/')}{path}",
        timeout=get_settings().api_timeout_seconds,
    )
    resp.raise_for_status()
    return resp


@tool
@tool_http_errors
def uptime_kuma_status_page_tool(base_url: str, slug: str) -> dict:
    """GET /api/status-page/<slug> — monitor list + grouping (read-only, unauthenticated).

    Returns the raw status-page JSON: ``publicGroupList``, each group
    carrying a ``monitorList`` of ``{id, name, ...}`` dicts.
    """
    return _uptime_kuma_raw_get(base_url, f"/api/status-page/{slug}").json()


@tool
@tool_http_errors
def uptime_kuma_heartbeat_tool(base_url: str, slug: str) -> dict:
    """GET /api/status-page/heartbeat/<slug> — heartbeat history + uptime (read-only).

    Returns the raw heartbeat JSON: ``heartbeatList`` (keyed by monitor id,
    most-recent beat last) and ``uptimeList`` (keyed ``"<monitorId>_24"``,
    the rolling 24h uptime ratio 0.0-1.0).
    """
    return _uptime_kuma_raw_get(base_url, f"/api/status-page/heartbeat/{slug}").json()
