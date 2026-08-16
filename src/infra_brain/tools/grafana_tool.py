"""Grafana HTTP API tools — read-only.

New collector domain (operator-requested, additive beyond the convergence
plan): real observability data from the home lab's Grafana instance
(monitoring-grafana container on node_a, port 3002 — see
~/.hermes/wiki/entities/services/grafana.md).

Endpoints used (all GET, read-only):
  GET /api/health                              -> {"commit", "database", "version"}
      Anonymous-accessible on a standard Grafana install — no auth attempted.
  GET /api/search?type=dash-db                  -> list of dashboard search hits,
      each carrying at least uid/title/tags/folderTitle. Requires auth on a
      hardened instance (anonymous org viewer access is not assumed).
  GET /api/alertmanager/grafana/api/v2/alerts    -> list of GettableAlert objects
      (Alertmanager API v2 shape: labels/annotations/status/startsAt/fingerprint).
      This is Grafana's embedded Alertmanager-compatible endpoint for its
      unified alerting engine (default since Grafana 9). The legacy dashboard-
      alerting endpoint (``GET /api/alerts``) was deprecated well before that
      and removed outright in Grafana 11 — picked the unified-alerting path as
      the more likely shape for "a recent self-hosted instance" per the task's
      instruction; see agents/grafana.py's module docstring for the note to
      the integration pass if the deployed version turns out to still be on
      legacy alerting.

Auth: a Grafana service-account/API token, sent as ``Authorization: Bearer
<token>``. Only /api/health is ever called without one — see
agents/grafana.py's two-tier collect() for the self-skip logic.

H-3 — the token is read from ``settings.grafana_api_token`` INSIDE the tool
body and is NEVER a tool argument. ``callbacks/audit.py`` persists
``args_summary = input_str[:256]`` verbatim into ``AgentActionLog``/
``AuditLog``, both queryable from the dashboard, and DLP only scans for PANs —
so a token passed as an argument was written to the audit trail in cleartext
on every single run. This is the same pattern ``tools/prometheus_tool.py`` and
``tools/alertmanager_tool.py`` already use, and the one
``agents/secrets_inventory.py`` documents as mandatory for exactly this
reason. Do not re-add a credential parameter to any tool in this module.

F-02 pattern reuse (per tools/octopus_tool.py): tenacity retry on transient
HTTP errors (timeout, connect error, 5xx) only. 4xx (including 401/403 from a
missing/invalid token) is never retried — retrying an auth failure wastes
four attempts for a result that will never change.
"""

from __future__ import annotations

import logging

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

log = logging.getLogger(__name__)


def _is_transient_http(exc: BaseException) -> bool:
    """Return True for transient errors that warrant a retry.

    Transient: network timeouts, connect errors, and 5xx server errors.
    Never retry 4xx (bad/missing token, not-found).
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


def _require_configured_host(passed_url: str, configured_url: str, *, setting_name: str) -> None:
    """Refuse to proceed unless ``passed_url`` targets the credential's own
    configured host (MEDIUM-1 credential/host-binding fix — mirrors
    ``tools/wazuh_tool.py``'s helper of the same name).

    H-3 moved the token resolution inside the tool bodies so it never crosses
    a tool boundary into the (dashboard-queryable) audit trail. That fix left
    no relationship enforced between the resolved token and whatever
    ``base_url`` the caller passed. This module's tools aren't reachable by
    any LLM (not in ``chat/agent.py::TOOLS`` or ``runner/agent_task.py::TOOLS``)
    and are only ever invoked by ``GrafanaAgent.collect()`` with a
    ``base_url`` read straight from settings — but that is a convention, not
    a structural guarantee, and ``ReadOnlyToolValidator`` cannot backstop it
    either (it inspects ``inp["url"]``, not ``inp["base_url"]``).

    Trailing slashes are insignificant. An unconfigured setting never matches
    anything. Raises ``RuntimeError`` (loud, never a silent skip).
    """
    normalized_passed = (passed_url or "").strip().rstrip("/")
    normalized_configured = (configured_url or "").strip().rstrip("/")
    if not normalized_configured or normalized_passed != normalized_configured:
        raise RuntimeError(
            f"Refusing to attach {setting_name} credential: the target URL "
            f"{passed_url!r} does not match the configured {setting_name} "
            f"({configured_url!r}). This tool only attaches its credential "
            f"to the host it was issued for."
        )


def _auth_headers(base_url: str) -> dict:
    """Bearer header built from settings — never from a caller-supplied value.

    Returns an empty dict when no token is configured, so the anonymous
    /api/health call works unchanged on a token-less deployment. When a token
    IS configured, ``base_url`` must match ``settings.grafana_url`` (MEDIUM-1
    credential/host binding) or this raises rather than attaching the token
    to an unexpected host.
    """
    token = (getattr(get_settings(), "grafana_api_token", "") or "").strip()
    if not token:
        return {}
    _require_configured_host(
        base_url, getattr(get_settings(), "grafana_url", ""), setting_name="grafana_url"
    )
    return {"Authorization": f"Bearer {token}"}


@_http_retry
def _grafana_raw_get(base_url: str, path: str, authenticated: bool = False) -> httpx.Response:
    """GET against the Grafana HTTP API (read-only by construction via readonly_get).

    ``authenticated`` selects whether an ``Authorization`` header is attached;
    the token itself is resolved from settings by ``_auth_headers`` and never
    crosses a tool boundary (H-3 — see the module docstring). When
    authenticated, ``_auth_headers`` also enforces that ``base_url`` matches
    ``settings.grafana_url`` before attaching the token (MEDIUM-1
    credential/host binding).
    """
    resp = readonly_get(
        f"{base_url.rstrip('/')}{path}",
        headers=_auth_headers(base_url) if authenticated else {},
        timeout=get_settings().api_timeout_seconds,
    )
    resp.raise_for_status()
    return resp


@tool
@tool_http_errors
def grafana_health_tool(base_url: str) -> dict:
    """GET /api/health — Grafana liveness/version (read-only, anonymous-accessible).

    Returns the raw JSON body, typically ``{"commit": ..., "database": "ok",
    "version": "10.4.1"}``.
    """
    return _grafana_raw_get(base_url, "/api/health").json()


@tool
@tool_http_errors
def grafana_dashboards_tool(base_url: str) -> list:
    """GET /api/search?type=dash-db — dashboard inventory (read-only, requires a token).

    The Grafana API token is read from ``settings.grafana_api_token`` inside
    this tool — it is deliberately NOT a parameter (H-3; see module docstring).

    Returns a list of dashboard search-hit dicts, each with at least
    ``uid``, ``title``, ``tags``, and ``folderTitle``.
    """
    return _grafana_raw_get(base_url, "/api/search?type=dash-db", authenticated=True).json()


@tool
@tool_http_errors
def grafana_alerts_tool(base_url: str) -> list:
    """GET /api/alertmanager/grafana/api/v2/alerts — active alerts (read-only, requires a token).

    The Grafana API token is read from ``settings.grafana_api_token`` inside
    this tool — it is deliberately NOT a parameter (H-3; see module docstring).

    Returns a list of Alertmanager-v2-shaped alert dicts: ``labels``,
    ``annotations``, ``status`` ({"state": "active"|"suppressed"|...}),
    ``startsAt``, ``fingerprint``.
    """
    return _grafana_raw_get(
        base_url, "/api/alertmanager/grafana/api/v2/alerts", authenticated=True
    ).json()
