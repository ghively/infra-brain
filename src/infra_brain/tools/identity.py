"""Okta IdP API tools — read-only by construction (GET-only client).

GitLab issue #102: backs ``IdentityAgent``'s IdP principal / group-membership
audit. Mirrors the ``tools/rapid7.py`` shape (retry on transient 5xx, never on
4xx, cursor/Link-header pagination like Okta's REST API actually uses — Okta
paginates via an RFC-5988 ``Link: <url>; rel="next"`` response header, not an
offset/page-count body like Rapid7).

All calls are plain GETs against ``{okta_url}/api/v1/...`` with an SSWS
(API-token) bearer header — no write verbs exist in this module, and
``ReadOnlyClient`` refuses non-GET/HEAD at the transport layer regardless.
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
from infra_brain.tools.http_readonly import ReadOnlyClient, tool_http_errors

logger = logging.getLogger(__name__)


def _is_transient_http(exc: BaseException) -> bool:
    """Transient: network timeouts/connect errors and 5xx. Never retry 4xx
    (bad token, 404 group id, etc — permanent/caller errors)."""
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
    return not s.okta_url or not s.okta_api_token


@_http_retry
def _okta_get(endpoint: str, params: dict | None = None) -> httpx.Response:
    """Authenticated GET to the Okta API with retry on transient errors.

    Creates a fresh ReadOnlyClient per attempt (idempotent GET — safe to
    retry). Raises httpx.HTTPStatusError for all non-2xx responses; tenacity
    retries 5xx transparently, 4xx propagates immediately.
    """
    s = _settings()
    with ReadOnlyClient(
        base_url=s.okta_url.rstrip("/"),
        headers={"Authorization": f"SSWS {s.okta_api_token}", "Accept": "application/json"},
        verify=s.okta_ssl_verify,
        timeout=s.api_timeout_seconds,
    ) as client:
        resp = client.get(endpoint, params=params)
        resp.raise_for_status()
        return resp


def _next_link(resp: httpx.Response) -> str | None:
    """Parse Okta's RFC-5988 ``Link: <url>; rel="next"`` pagination header."""
    link_header = resp.headers.get("link") or resp.headers.get("Link")
    if not link_header:
        return None
    for part in link_header.split(","):
        segment = part.strip()
        if 'rel="next"' in segment:
            start = segment.find("<")
            end = segment.find(">", start)
            if start != -1 and end != -1:
                return segment[start + 1 : end]
    return None


def _is_same_okta_host(url: str) -> bool:
    """True iff *url* targets the same scheme+host+port as the configured Okta org.

    ``_okta_get`` binds ``Authorization: SSWS <okta_api_token>`` to whatever
    URL it's given via ``ReadOnlyClient(base_url=...)`` + a relative path —
    but Okta's own ``Link`` response header hands back an ABSOLUTE URL, and
    httpx sends the client's auth header to an absolute URL unchanged even
    when it points somewhere else entirely. A compromised/spoofed response
    (or a MITM when ``okta_ssl_verify=False``) could hand back a next-link
    pointing at an attacker host — or the same hostname on a different port
    — and walk off with the org's admin API token. Reject anything that
    isn't the configured Okta host (scheme, hostname, AND port — a same-host
    different-port target is not the same origin) before it is ever passed
    back into ``_okta_get``.
    """
    from urllib.parse import urlparse

    configured = urlparse(_settings().okta_url)
    target = urlparse(url)
    try:
        target_port = target.port
    except ValueError:
        # Malformed/non-numeric port in the untrusted Link-header URL —
        # treat as not matching rather than raising out of a guard function.
        return False
    return (target.scheme, target.hostname, target_port) == (
        configured.scheme,
        configured.hostname,
        configured.port,
    )


def _get_all_pages(endpoint: str, params: dict | None = None, page_cap: int = 200) -> list:
    """Follow Okta's Link-header pagination until exhausted or *page_cap* hit.

    ``page_cap`` is a hard safety bound (Okta defaults to 200 records/page —
    200 pages is 40k principals, comfortably above any real Okta org this
    agent is expected to run against) so a malformed/looping Link header can
    never spin forever.
    """
    results: list = []
    url: str | None = endpoint
    call_params: dict | None = params
    pages = 0
    while url and pages < page_cap:
        resp = _okta_get(url, params=call_params)
        results.extend(resp.json())
        next_url = _next_link(resp)
        if next_url and not _is_same_okta_host(next_url):
            logger.warning(
                "identity: Okta Link header pointed off-host (%r) — stopping pagination "
                "rather than sending the API token there",
                next_url,
            )
            break
        url = next_url
        call_params = None  # the next-link URL already embeds the cursor/query
        pages += 1
    return results


@tool
@tool_http_errors
def okta_users_tool() -> list[dict]:
    """List all Okta users (id, status, profile) — read-only."""
    if _unconfigured():
        return []
    return _get_all_pages("/api/v1/users", params={"limit": 200})


@tool
@tool_http_errors
def okta_groups_tool() -> list[dict]:
    """List all Okta groups (id, profile, type) — read-only."""
    if _unconfigured():
        return []
    return _get_all_pages("/api/v1/groups", params={"limit": 200})


@tool
@tool_http_errors
def okta_group_members_tool(group_id: str) -> list[dict]:
    """List the member users of one Okta group by id — read-only."""
    if _unconfigured():
        return []
    return _get_all_pages(f"/api/v1/groups/{group_id}/users", params={"limit": 200})
