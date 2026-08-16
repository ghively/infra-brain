"""Alertmanager HTTP API tools — read-only (R2 layer 1: GET-only by construction).

Alertmanager exposes a small, stable v2 REST API (OpenAPI spec:
https://github.com/prometheus/alertmanager/blob/main/api/v2/openapi.yaml).
This module wraps the three read endpoints the collector needs:

  GET /api/v2/status    -- server/cluster liveness + version info
  GET /api/v2/alerts    -- currently active alerts
  GET /api/v2/silences  -- currently active/pending/expired silences

Follows the same shape as ``tools/octopus_tool.py``: a bare ``readonly_get``
call wrapped in a tenacity retry that only retries transient errors (timeouts,
connect errors, 5xx) and never 4xx, plus ``@tool`` + ``tool_http_errors`` so
LangChain tool-calling error handling stays uniform across every HTTP-backed
collector tool.
"""

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
    """Transient: network timeouts, connect errors, and 5xx server errors.

    Never retry 4xx (auth failures, not-found, bad request) — same predicate
    as octopus_tool._is_transient_http.
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


def _settings():
    return get_settings()


def _headers() -> dict:
    """Optional bearer auth — this home lab's Alertmanager is localhost-only
    with no auth today, but a reverse-proxied deployment may require a token.
    Empty ``alertmanager_token`` (the default) sends no Authorization header.
    """
    token = getattr(_settings(), "alertmanager_token", "") or ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _api_path(path: str) -> str:
    """Build the full URL, or raise a clean httpx.HTTPError if unconfigured.

    Mirrors octopus_tool._api_path's rationale: an empty base URL must never
    reach httpx as a schemeless fragment (that raises an opaque
    ``UnsupportedProtocol`` deep inside the retry loop) — the collector's own
    ``CollectorSkipped`` check is the primary guard, this is a defensive
    second layer for any direct tool invocation that bypasses collect().
    """
    base = _settings().alertmanager_url
    if not base:
        raise httpx.HTTPError("Alertmanager is not configured (ALERTMANAGER_URL unset)")
    return f"{base.rstrip('/')}{path}"


@_http_retry
def _alertmanager_raw_get(path: str) -> httpx.Response:
    """Authenticated GET to the Alertmanager API with retry on transient errors.

    Returns the raw httpx.Response. Raises httpx.HTTPStatusError on non-2xx;
    tenacity retries 5xx transparently. 4xx propagates immediately.
    """
    resp = readonly_get(
        _api_path(path),
        headers=_headers(),
        timeout=_settings().api_timeout_seconds,
    )
    resp.raise_for_status()
    return resp


def alertmanager_get(path: str) -> dict | list:
    """Authenticated GET to the Alertmanager API (read-only by construction)."""
    return _alertmanager_raw_get(path).json()


@tool
@tool_http_errors
def alertmanager_status_tool() -> dict:
    """Get Alertmanager server status: version info, uptime, cluster status (read-only)."""
    return alertmanager_get("/api/v2/status")


@tool
@tool_http_errors
def alertmanager_alerts_tool() -> list:
    """List currently active Alertmanager alerts (read-only). Empty list is normal/success."""
    return alertmanager_get("/api/v2/alerts")


@tool
@tool_http_errors
def alertmanager_silences_tool() -> list:
    """List Alertmanager silences (active/pending/expired) (read-only)."""
    return alertmanager_get("/api/v2/silences")
