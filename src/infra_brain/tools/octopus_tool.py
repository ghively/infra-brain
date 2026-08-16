"""
Octopus Deploy API tools — read-only.
Handles pre-Spaces API (< 2019.1) which uses /api/ flat paths
instead of /api/Spaces-{id}/ paths used in modern Octopus.
The deployment target (3.3.8) is pre-Spaces, so flat /api/ paths are used.

F-02 (audit 2026-07-06): Added tenacity retry on transient HTTP errors (timeout,
connect error, 5xx). 4xx errors are never retried. JSON decode errors (Octopus
3.3.8 truncated-response known issue) are not HTTP errors — handled separately
in octopus_get_paginated.
"""

import json
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
from infra_brain.tools.http_readonly import readonly_get
from infra_brain.tools.http_readonly import tool_http_errors

log = logging.getLogger(__name__)


def _is_transient_http(exc: BaseException) -> bool:
    """Return True for transient errors that warrant a retry.

    Transient: network timeouts, connect errors, and 5xx server errors.
    Never retry 4xx (auth failures, not-found) or JSONDecodeError (data issue).
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
    return {"X-Octopus-ApiKey": _settings().octopus_api_key}


def _api_path(path: str) -> str:
    """Build full URL. Pre-Spaces Octopus uses flat /api/ paths — no Spaces prefix injected.

    Found via a live DiscoveryAgent run (P3.4 retest): with OCTOPUS_URL unset,
    this silently built a schemeless URL (just the path fragment), which
    httpx/httpcore reject deep inside the retry loop as
    ``UnsupportedProtocol`` — an opaque low-level error, not the clear
    "Octopus isn't configured" every other unconfigured-integration collector
    reports (WindowsAgent's CollectorSkipped, ansible.py's
    ZERO_HOST_INVENTORY_MARKER, ...). Raising httpx.HTTPError here instead:
    tool_http_errors (which every octopus_*_tool already applies) converts it
    to a clean ToolException, and _is_transient_http correctly does not retry
    a config error 4 times before giving up.
    """
    base = _settings().octopus_url
    if not base:
        raise httpx.HTTPError("Octopus Deploy is not configured (OCTOPUS_URL unset)")
    return f"{base.rstrip('/')}{path}"


@_http_retry
def _octopus_raw_get(path: str) -> httpx.Response:
    """Authenticated GET to Octopus Deploy API with retry on transient errors.

    Returns the raw httpx.Response. Raises httpx.HTTPStatusError on non-2xx;
    tenacity retries 5xx transparently. 4xx propagates immediately.
    JSON decode errors (Octopus truncated-body bug) are NOT retried here —
    they are handled in octopus_get_paginated with the skip-and-continue strategy.
    """
    resp = readonly_get(
        _api_path(path),
        headers=_headers(),
        verify=_settings().octopus_ssl_verify,
        timeout=_settings().api_timeout_seconds,
    )
    resp.raise_for_status()
    return resp


def octopus_get(path: str) -> dict | list:
    """Authenticated GET to Octopus Deploy API (read-only by construction)."""
    return _octopus_raw_get(path).json()


def octopus_get_paginated(resource_path: str, page_size: int | None = None) -> list:
    """Fetch all pages from an Octopus paginated endpoint (Items/TotalResults envelope).

    Advance ``skip`` by the number of items the server actually returned, never by
    the requested page size. Pre-Spaces Octopus 3.3.8 ignores a large ``take`` and
    clamps every page to its server-default ``ItemsPerPage`` (30). Advancing by the
    *requested* size (e.g. 100) skipped over items 30-99 of every page, collecting
    only ~90 of 241 projects — the silent truncation this fixes. We follow the
    server's own ``Links.Page.Next`` only as a has-more signal (relative, scheme-less
    URLs are never fetched directly — every request still goes through ``octopus_get``,
    which prepends the base URL).

    Octopus 3.3.8 occasionally returns a truncated HTTP response for a specific page
    (the server cuts the body mid-JSON for pages that contain events with very large
    ``Details`` fields, e.g. HTML task logs). When this happens, ``resp.json()`` raises
    ``JSONDecodeError``.  Rather than aborting the entire paginated fetch and losing all
    subsequent pages, we catch the error, warn with the skip offset, advance by one full
    page's worth of items to jump past the problematic window, and continue.  The bad
    page is skipped; all other pages are still collected.

    The "one full page" jump on error uses the server's *actual* observed page width
    (from the most recent successful page), not the requested ``size`` — Octopus 3.3.8
    clamps every page to its own ``ItemsPerPage`` (30) regardless of the requested
    ``take``, so jumping by the requested ``size`` (commonly 100) after a corrupted page
    would skip ~70 additional, never-fetched items. Before any page has succeeded, we
    have no observed width yet and fall back to ``size`` as a best-effort guess.
    """
    size = page_size or _settings().api_page_size
    results: list = []
    skip = 0
    _consecutive_bad = 0  # guard against an infinite bad-page loop
    _observed_page_width = size  # best guess until a real page succeeds
    while True:
        sep = "&" if "?" in resource_path else "?"
        url_fragment = f"{resource_path}{sep}skip={skip}&take={size}"
        try:
            data = octopus_get(url_fragment)
            _consecutive_bad = 0  # reset on success
        except json.JSONDecodeError as exc:
            _consecutive_bad += 1
            log.warning(
                "octopus_get_paginated: bad JSON at skip=%d (%s) — jumping %d items to clear"
                " bad window (consecutive_bad=%d, detail=%s)",
                skip,
                resource_path,
                _observed_page_width,
                _consecutive_bad,
                exc,
            )
            if _consecutive_bad >= 5:
                log.error(
                    "octopus_get_paginated: %d consecutive bad pages at skip=%d for %s — aborting",
                    _consecutive_bad,
                    skip,
                    resource_path,
                )
                break
            # Jump forward by the server's actual (possibly clamped) page width — the
            # offending event is contained in this page window; advancing by the real
            # width clears it in one step without skipping unfetched items.
            skip += _observed_page_width
            continue

        if isinstance(data, dict):
            items = data.get("Items", [])
            total = data.get("TotalResults")
            links = data.get("Links", {}) or {}
        else:
            items = data
            total = None
            links = {}
        results.extend(items)
        # Stop on an empty page (defends against a bad total / infinite loop).
        if not items:
            break
        # Advance by the count actually returned — the server clamps page size,
        # so the requested ``size`` can exceed what we got.
        skip += len(items)
        # Remember the real (possibly clamped) page width for the error-recovery jump.
        _observed_page_width = len(items)
        # Prefer the authoritative total; fall back to the Next link presence.
        if total is not None:
            if skip >= total:
                break
        elif not links.get("Page.Next"):
            break
    return results


@tool
@tool_http_errors
def octopus_server_info_tool() -> dict:
    """Get Octopus Deploy server version, edition, and license info (read-only)."""
    return octopus_get("/api/")


@tool
@tool_http_errors
def octopus_projects_tool() -> list:
    """List all Octopus Deploy projects (read-only, paginated)."""
    return octopus_get_paginated("/api/projects")


@tool
@tool_http_errors
def octopus_environments_tool() -> list:
    """List all Octopus Deploy environments (read-only, paginated)."""
    return octopus_get_paginated("/api/environments")


@tool
@tool_http_errors
def octopus_machines_tool() -> list:
    """List all Octopus Deploy Tentacle machines with connection status (read-only, paginated)."""
    return octopus_get_paginated("/api/machines")


@tool
@tool_http_errors
def octopus_lifecycles_tool() -> list:
    """List all Octopus Deploy lifecycles including phase definitions (read-only, paginated)."""
    return octopus_get_paginated("/api/lifecycles")


@tool
@tool_http_errors
def octopus_projectgroups_tool() -> list:
    """List all Octopus Deploy project groups (read-only, paginated)."""
    return octopus_get_paginated("/api/projectgroups")


@tool
@tool_http_errors
def octopus_channels_tool(project_id: str) -> list:
    """List channels for an Octopus project (read-only, paginated). Pre-Spaces flat path."""
    return octopus_get_paginated(f"/api/projects/{project_id}/channels")


@tool
@tool_http_errors
def octopus_releases_tool(project_id: str, take: int = 5) -> dict:
    """List recent releases for an Octopus project (read-only). Pre-Spaces flat path."""
    return octopus_get(f"/api/projects/{project_id}/releases?take={take}")


@tool
@tool_http_errors
def octopus_dashboard_tool() -> dict:
    """Get the Octopus dashboard: latest deployment per project x environment in bulk (read-only).

    Returns the dashboard envelope with keys Projects, ProjectGroups, Environments,
    Items, PreviousItems, Links. ``Items`` are the current deployments and carry
    ProjectId, EnvironmentId, ReleaseId, ReleaseVersion, TaskId, State, Created,
    CompletedTime, Duration, FinishedSuccessfully-equivalents (HasWarningsOrErrors),
    avoiding a per-deployment Task fetch.
    """
    return octopus_get("/api/dashboard")


@tool
@tool_http_errors
def octopus_deployments_tool(project_id: str, take: int = 20) -> dict:
    """List recent deployments for an Octopus project (read-only)."""
    return octopus_get(f"/api/deployments?projects={project_id}&take={take}")


# --- Deep capture (MR9) -----------------------------------------------------
#
# All read-only GETs. Pre-Spaces flat /api/ paths. The deep collector calls the
# raw octopus_get / octopus_get_paginated helpers above for per-project fetches
# (variable sets, deployment processes) since those are keyed by an id supplied
# at call time; the estate-wide endpoints get a thin @tool wrapper here so they
# are mockable in tests the same way the MR2 tools are.


@tool
@tool_http_errors
def octopus_deployment_process_tool(process_id: str) -> dict:
    """Get a deployment process (its ordered Steps/Actions) by id (read-only)."""
    return octopus_get(f"/api/deploymentprocesses/{process_id}")


def _strip_variable_values(payload: dict | list) -> dict | list:
    """Strip the ``Value`` field from every entry in a variable set's ``Variables`` list.

    TRK-152: the collector (``agents/octopus.py``) was always going to drop
    ``Value`` unconditionally before persisting anything — it only reads Name,
    Scope, IsSensitive, IsEditable. But the DLP callback scans RAW tool output
    *before* the agent's own drop logic ever runs, so a Value containing a
    Luhn-valid digit run fails the whole tool call closed (confirmed live
    2026-07-23: "Projects-303" and "Projects-505" variable sets). Stripping
    Value at the tool boundary — same "mask/strip before DLP sees it" pattern
    as TRK-110's ``rapid7._sanitize_payload`` — means the sensitive/PAN-shaped
    data never reaches the DLP scanner at all, rather than tripping it.
    """
    if not isinstance(payload, dict):
        return payload
    variables = payload.get("Variables")
    if not isinstance(variables, list):
        return payload
    payload["Variables"] = [
        {k: v for k, v in var.items() if k != "Value"} if isinstance(var, dict) else var
        for var in variables
    ]
    return payload


@tool
@tool_http_errors
def octopus_variableset_tool(variable_set_id: str) -> dict:
    """Get a variable set (project or library) by id — METADATA used; values dropped.

    The collector NEVER stores the ``Value`` field. Values are stripped here at
    the tool boundary (TRK-152) before the response ever reaches the DLP
    callback — previously the collector dropped Value itself, but the DLP scan
    of raw tool output happens first and Luhn-valid-looking values fail-closed
    the whole call.
    """
    return _strip_variable_values(octopus_get(f"/api/variables/{variable_set_id}"))


@tool
@tool_http_errors
def octopus_library_variable_sets_tool() -> list:
    """List all Octopus library variable sets (read-only, paginated)."""
    return octopus_get_paginated("/api/libraryvariablesets")


@tool
@tool_http_errors
def octopus_action_templates_tool() -> list:
    """List all Octopus action (step) templates (read-only, paginated)."""
    return octopus_get_paginated("/api/actiontemplates")


@tool
@tool_http_errors
def octopus_machine_roles_tool() -> list:
    """List all machine role names as a flat string array (read-only)."""
    return octopus_get("/api/machineroles/all")


@tool
@tool_http_errors
def octopus_feeds_tool() -> list:
    """List all Octopus package feeds (read-only, paginated)."""
    return octopus_get_paginated("/api/feeds")


@tool
@tool_http_errors
def octopus_interruptions_tool() -> list:
    """List Octopus interruptions / manual-intervention prompts (read-only, paginated)."""
    return octopus_get_paginated("/api/interruptions")


@tool
@tool_http_errors
def octopus_tasks_tool() -> list:
    """List Octopus server tasks newest-first (read-only, paginated).

    The collector walks these and STOPS once a task is older than the configured
    history window, so the whole task history is never materialized.
    """
    return octopus_get_paginated("/api/tasks")


@tool
@tool_http_errors
def octopus_accounts_tool() -> list:
    """List Octopus accounts — METADATA only; NO secret/key/token material (read-only)."""
    return octopus_get_paginated("/api/accounts")


@tool
@tool_http_errors
def octopus_teams_tool() -> list:
    """List all Octopus teams (read-only, paginated)."""
    return octopus_get_paginated("/api/teams")


@tool
@tool_http_errors
def octopus_users_tool() -> list:
    """List all Octopus users (read-only, paginated)."""
    return octopus_get_paginated("/api/users")


@tool
@tool_http_errors
def octopus_events_tool(from_iso: str | None = None) -> list:
    """List Octopus audit events, optionally filtered to events on/after ``from_iso``.

    Uses the API's ``from=`` date filter when supplied (paginated). The collector
    additionally enforces a hard row cap and a newest-first cutoff defensively in
    case ``from=`` is ignored by this server version.
    """
    path = "/api/events"
    if from_iso:
        path = f"{path}?from={from_iso}"
    return octopus_get_paginated(path)
