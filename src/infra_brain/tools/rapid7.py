"""Rapid7 InsightVM API tools — read-only by construction (GET-only client).

F-02 (audit 2026-07-06): Added tenacity retry on transient HTTP errors (timeout,
connect error, 5xx). 4xx errors (auth, not-found) are never retried.

TRK-110: every payload is passed through ``_sanitize_payload`` before leaving
the tool boundary — PAN-shaped digit runs (Luhn-valid AND card-network IIN,
the same corroborated predicate the DLP callback enforces) are redacted via
``dlp.redact_pans``. Live false positive that motivated this: one software
inventory entry whose bare 13-digit build/version number starts with 4 and
happens to Luhn-validate, which fail-closed the entire vuln collection daily.
Redacting up front neutralizes both false positives AND any genuine PAN, while
the fail-closed DLP scanner stays untouched downstream as the backstop.
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

from infra_brain.callbacks.dlp import redact_pans_preserving_uuids
from infra_brain.config import get_settings
from infra_brain.tools.http_readonly import ReadOnlyClient
from infra_brain.tools.http_readonly import tool_http_errors

logger = logging.getLogger(__name__)


def _is_transient_http(exc: BaseException) -> bool:
    """Return True for transient errors that warrant a retry.

    Transient: network timeouts, connect errors, and 5xx server errors.
    Never retry 4xx (auth failures, not-found — permanent/caller errors).
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


def _unconfigured() -> bool:
    s = _settings()
    return not s.rapid7_url or not s.rapid7_api_key


@_http_retry
def _r7_get(endpoint: str, params: dict | None = None) -> httpx.Response:
    """Authenticated GET to Rapid7 API with retry on transient errors.

    Creates a fresh ReadOnlyClient per attempt (idempotent GET — safe to retry).
    Raises httpx.HTTPStatusError for all non-2xx responses; tenacity retries 5xx
    transparently. 4xx errors propagate immediately (no retry).
    """
    s = _settings()
    with ReadOnlyClient(
        base_url=s.rapid7_url.rstrip("/"),
        auth=(s.rapid7_username, s.rapid7_api_key),
        verify=s.rapid7_ssl_verify,
        timeout=s.api_timeout_seconds,
    ) as client:
        resp = client.get(endpoint, params=params)
        resp.raise_for_status()
        return resp


def _sanitize_payload(obj, _stats: list | None = None):
    """Recursively redact PAN-shaped digit runs in every string of *obj* (TRK-110).

    Uses ``dlp.redact_pans`` — the same Luhn+IIN corroborated predicate the DLP
    callback fail-closes on — so nothing that would trip the scanner survives
    past the tool boundary. Non-matching strings pass through unchanged (and
    un-copied where possible).
    """
    if isinstance(obj, str):
        redacted = redact_pans_preserving_uuids(obj)
        if redacted != obj and _stats is not None:
            _stats.append(1)
        return redacted
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        # A PAN-shaped value can arrive as a bare JSON number; the downstream
        # DLP callback stringifies the whole payload, so leaving it numeric
        # would re-trigger the fail-close this boundary exists to prevent.
        redacted = redact_pans_preserving_uuids(str(obj))
        if redacted != str(obj):
            if _stats is not None:
                _stats.append(1)
            return redacted
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_payload(v, _stats) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_payload(v, _stats) for v in obj]
    return obj


def _r7_json(endpoint: str, params: dict | None = None):
    """GET + parse + sanitize a Rapid7 payload (see _sanitize_payload)."""
    payload = _r7_get(endpoint, params=params).json()
    stats: list = []
    payload = _sanitize_payload(payload, stats)
    if stats:
        logger.warning(
            "rapid7: redacted %d PAN-shaped digit run(s) in %s payload before "
            "the tool boundary (TRK-110)",
            len(stats),
            endpoint,
        )
    return payload


def _get_all_pages(endpoint: str, page_size: int = 500) -> list:
    """Paginate through a Rapid7 endpoint looping on page.totalPages.

    Each page request retries independently via _r7_get.
    """
    results: list = []
    page = 0
    while True:
        payload = _r7_json(endpoint, params={"page": page, "size": page_size})
        resources = payload.get("resources", [])
        results.extend(resources)
        total_pages = payload.get("page", {}).get("totalPages", 1)
        page += 1
        if page >= total_pages:
            break
    return results


@tool
@tool_http_errors
def rapid7_assets_tool(page: int = 0, size: int = 100) -> dict:
    """List Rapid7 InsightVM assets with risk scores (read-only)."""
    if _unconfigured():
        return {"resources": [], "error": "rapid7_url not configured"}
    return _r7_json("/api/3/assets", params={"page": page, "size": size})


@tool
@tool_http_errors
def rapid7_asset_detail_tool(asset_id: int) -> dict:
    """Get full detail for a single Rapid7 asset including vulnerability counts (read-only)."""
    if _unconfigured():
        return {"error": "rapid7_url not configured"}
    return _r7_json(f"/api/3/assets/{asset_id}")


@tool
@tool_http_errors
def rapid7_vulnerabilities_tool(page: int = 0, size: int = 100) -> dict:
    """List Rapid7 vulnerabilities with CVSS scores (read-only)."""
    if _unconfigured():
        return {"resources": [], "error": "rapid7_url not configured"}
    return _r7_json("/api/3/vulnerabilities", params={"page": page, "size": size})


@tool
@tool_http_errors
def rapid7_asset_vulnerabilities_tool(asset_id: int) -> dict:
    """Get vulnerabilities affecting a specific Rapid7 asset (read-only)."""
    if _unconfigured():
        return {"error": "rapid7_url not configured"}
    return _r7_json(f"/api/3/assets/{asset_id}/vulnerabilities")


@tool
@tool_http_errors
def rapid7_assets_all_tool(page_size: int = 500) -> list:
    """List ALL Rapid7 InsightVM assets with risk scores (read-only, fully paginated)."""
    if _unconfigured():
        return []
    return _get_all_pages("/api/3/assets", page_size=page_size)


@tool
@tool_http_errors
def rapid7_vulns_all_tool(page_size: int = 500) -> list:
    """List ALL Rapid7 vulnerabilities with CVSS scores (read-only, fully paginated)."""
    if _unconfigured():
        return []
    return _get_all_pages("/api/3/vulnerabilities", page_size=page_size)


@tool
@tool_http_errors
def rapid7_vuln_detail_tool(vuln_id: str) -> dict:
    """Get full detail for a single Rapid7 vulnerability by its SLUG id (read-only).

    ``vuln_id`` is the Rapid7 slug (e.g. ``microsoft-asp_net_core-cve-2023-36038``).
    Returns the vulnerability definition: {id, title, severity, cvss{...}, riskScore,
    exploits, malwareKits, denialOfService, published, categories, cves, pci{...}, ...}.
    """
    if _unconfigured():
        return {"error": "rapid7_url not configured"}
    return _r7_json(f"/api/3/vulnerabilities/{vuln_id}")


@tool
@tool_http_errors
def rapid7_vuln_solutions_tool(vuln_id: str) -> list:
    """List the solution ids that remediate a Rapid7 vulnerability SLUG (read-only).

    GET /api/3/vulnerabilities/{id}/solutions. Rapid7 returns the linked solution
    ids under ``resources`` (a list of solution-id strings). Returns that list.
    """
    if _unconfigured():
        return []
    payload = _r7_json(f"/api/3/vulnerabilities/{vuln_id}/solutions")
    if isinstance(payload, dict):
        return payload.get("resources", []) or []
    return payload or []


@tool
@tool_http_errors
def rapid7_solution_tool(solution_id: str) -> dict:
    """Get full detail for a single Rapid7 remediation solution (read-only).

    GET /api/3/solutions/{id}. Returns {id, summary{...}, steps{...}, type,
    estimate, ...} where summary/steps carry both ``text`` and ``html`` variants.
    """
    if _unconfigured():
        return {"error": "rapid7_url not configured"}
    return _r7_json(f"/api/3/solutions/{solution_id}")


@tool
@tool_http_errors
def rapid7_sites_tool(page_size: int = 500) -> list:
    """List ALL Rapid7 InsightVM sites (read-only, fully paginated).

    Each site: {id, name, assets, riskScore, vulnerabilities{...}, importance,
    type, lastScanTime}. Global + cheap (6 sites in the live env).
    """
    if _unconfigured():
        return []
    return _get_all_pages("/api/3/sites", page_size=page_size)


@tool
@tool_http_errors
def rapid7_tags_tool(page_size: int = 500) -> list:
    """List ALL Rapid7 InsightVM tags (read-only, fully paginated).

    Each tag: {id, name, type, color, source}. Global + cheap (~251 tags).
    """
    if _unconfigured():
        return []
    return _get_all_pages("/api/3/tags", page_size=page_size)
