"""Prometheus HTTP API tools — read-only.

Homelab monitoring domain (operator-requested, additive to the convergence
plan — see `docs/entities/services/prometheus.md` in the Hermes wiki for the
deployment this was written against: `monitoring-prometheus` on node_a,
port 9090, scraping Grafana Alloy, blackbox-exporter, cAdvisor, and node
exporters across the fleet).

Only the two read endpoints PrometheusAgent needs are wrapped here, both part
of Prometheus's stable HTTP API (https://prometheus.io/docs/prometheus/latest/querying/api/):

  GET /api/v1/targets  -> {"status": "success", "data": {"activeTargets": [...],
                            "droppedTargets": [...]}}
                          Each activeTargets entry carries (at least)
                          ``labels`` (job/instance/...), ``health``
                          ("up"/"down"/"unknown"), ``lastScrape``, and
                          ``scrapePool``.
  GET /api/v1/alerts   -> {"status": "success", "data": {"alerts": [...]}}
                          Each alert entry carries ``labels`` (incl.
                          ``alertname``), ``annotations``, ``state``
                          ("firing"/"pending"), and ``activeAt``. An empty
                          list is a normal, healthy response — not an error.

No authentication is attempted: this mirrors the target deployment (no
``--web.enable-admin-api`` write surface exposed, no auth configured on a
LAN-internal Prometheus). Every request funnels through
``tools/http_readonly.py``'s ``readonly_get`` (GET-only by construction — R2
structural read-only), never a plain ``httpx`` call.
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

# Per the operator's ~10s-per-call requirement — deliberately its own
# constant rather than reusing the global ``api_timeout_seconds`` (30s
# default): a home-lab Prometheus instance on the LAN should answer near-
# instantly, and a collector polling target/alert health every few minutes
# should fail fast rather than tie up a collect() slot for 30s per call.
PROMETHEUS_TIMEOUT_SECONDS = 10.0


def _is_transient_http(exc: BaseException) -> bool:
    """Return True for transient errors that warrant a retry.

    Transient: network timeouts, connect errors, and 5xx server errors.
    Never retry 4xx (config/auth problems) — mirrors octopus_tool.py's
    ``_is_transient_http`` predicate exactly.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


_http_retry = retry(
    retry=retry_if_exception(_is_transient_http),
    wait=wait_exponential_jitter(initial=1, max=10),
    stop=stop_after_attempt(3),
)


def _base_url() -> str:
    """Base URL for the configured Prometheus server, or raise if unconfigured.

    Raising ``httpx.HTTPError`` here (rather than e.g. ``ValueError``) means
    ``tool_http_errors`` converts it into a ``ToolException`` uniformly like
    any other HTTP failure, and ``_is_transient_http`` correctly does not
    retry a config error. PrometheusAgent.collect() checks
    ``settings.prometheus_url`` itself before calling any tool (so the normal
    unconfigured path is a clean ``CollectorSkipped``, never reaching this
    branch) — this is defense-in-depth for direct tool callers, mirroring
    octopus_tool.py's ``_api_path``.
    """
    base = get_settings().prometheus_url
    if not base:
        raise httpx.HTTPError("Prometheus is not configured (PROMETHEUS_URL unset)")
    return base.rstrip("/")


@_http_retry
def _prometheus_raw_get(path: str) -> httpx.Response:
    """Authenticated-if-needed GET to the Prometheus HTTP API with transient retry."""
    resp = readonly_get(
        f"{_base_url()}{path}",
        verify=get_settings().prometheus_ssl_verify,
        timeout=PROMETHEUS_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp


@tool
@tool_http_errors
def prometheus_targets_tool() -> dict:
    """Fetch scrape target health from Prometheus (GET /api/v1/targets, read-only).

    Returns the raw Prometheus API envelope:
    ``{"status": "success", "data": {"activeTargets": [...], "droppedTargets": [...]}}``.
    """
    return _prometheus_raw_get("/api/v1/targets").json()


@tool
@tool_http_errors
def prometheus_alerts_tool() -> dict:
    """Fetch currently firing/pending alerts from Prometheus (GET /api/v1/alerts, read-only).

    Returns the raw Prometheus API envelope:
    ``{"status": "success", "data": {"alerts": [...]}}``. An empty ``alerts``
    list is a normal healthy response, not an error.
    """
    return _prometheus_raw_get("/api/v1/alerts").json()
