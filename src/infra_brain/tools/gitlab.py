import base64
import logging
import urllib.parse

import httpx
from langchain_core.tools import tool
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
)

from infra_brain.config import get_settings
from infra_brain.tools.http_readonly import readonly_get, tool_http_errors

logger = logging.getLogger(__name__)


def _is_transient_http(exc: BaseException) -> bool:
    """Return True for transient errors that warrant a retry.

    429 (rate limit) and 5xx server errors are transient.
    4xx (except 429) are permanent caller errors — never retried.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _gitlab_wait(retry_state) -> float:
    """Wait strategy: respect Retry-After header on 429, else exponential jitter.

    The wait is always bounded at 60 s to prevent runaway sleeps from a
    misbehaving server sending a huge Retry-After value.
    """
    exc = retry_state.outcome.exception()
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        ra_raw = exc.response.headers.get("Retry-After", "0")
        try:
            ra_seconds = int(ra_raw)
        except ValueError:
            ra_seconds = 0
        if ra_seconds > 0:
            return min(float(ra_seconds), 60.0)
    # Exponential jitter fallback (same cap).
    import random

    base = min(2.0**retry_state.attempt_number, 60.0)
    return base + random.uniform(0.0, 1.0)


_gitlab_retry = retry(
    retry=retry_if_exception(_is_transient_http),
    wait=_gitlab_wait,
    stop=stop_after_attempt(4),
)


@_gitlab_retry
def _gitlab_request(
    url: str, headers: dict, verify: bool, timeout: float, params: dict | None = None
) -> httpx.Response:
    """GET with tenacity retry on 429/5xx (max 4 attempts, bounded backoff).

    Pass params through as-is (None, not {}). httpx REPLACES the URL's existing
    query string whenever a params dict — even an empty one — is supplied, so
    forcing {} here would silently drop embedded query like ?ref= or ?membership=.
    """
    resp = readonly_get(url, headers=headers, verify=verify, timeout=timeout, params=params)
    resp.raise_for_status()
    return resp


def gitlab_get(path: str) -> dict | list:
    """Authenticated GET to GitLab API. path = /api/v4/..."""
    s = get_settings()
    url = f"{s.gitlab_url.rstrip('/')}{path}"
    # _gitlab_request already calls raise_for_status() inside; no double-call needed.
    resp = _gitlab_request(
        url, {"PRIVATE-TOKEN": s.gitlab_token}, s.gitlab_ssl_verify, s.api_timeout_seconds
    )
    return resp.json()


def gitlab_get_paginated(path: str, params: dict | None = None) -> list:
    """Authenticated GET with automatic pagination via X-Next-Page header."""
    s = get_settings()
    results: list = []
    page = 1
    page_size = s.api_page_size
    # httpx REPLACES (does not merge) the URL query when a params dict is passed, so
    # any query embedded in `path` (e.g. ?membership=true, ?ref=, ?recursive=true)
    # must be split out and carried through the params dict alongside per_page/page —
    # otherwise it is silently dropped and the API returns the wrong result set.
    split = urllib.parse.urlsplit(path)
    base_url = f"{s.gitlab_url.rstrip('/')}{split.path}"
    embedded = dict(urllib.parse.parse_qsl(split.query))
    seen_pages: set[int] = set()
    while True:
        if page in seen_pages:
            logger.warning("gitlab_get_paginated: loop at page %d for %s, breaking", page, path)
            break
        seen_pages.add(page)
        page_params = {**embedded, "per_page": page_size, "page": page}
        if params:
            page_params.update(params)
        # _gitlab_request already calls raise_for_status() inside.
        resp = _gitlab_request(
            base_url,
            {"PRIVATE-TOKEN": s.gitlab_token},
            s.gitlab_ssl_verify,
            s.api_timeout_seconds,
            page_params,
        )
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)
        next_page = resp.headers.get("X-Next-Page", "")
        if not next_page:
            break
        page = int(next_page)
    return results


@tool
@tool_http_errors
def gitlab_projects_tool() -> list:
    """List all accessible GitLab projects (read-only GET, paginated)."""
    return gitlab_get_paginated("/api/v4/projects?membership=true")


@tool
@tool_http_errors
def gitlab_pipelines_tool(project_id: int) -> list:
    """List recent pipelines for a GitLab project (read-only GET)."""
    return gitlab_get(f"/api/v4/projects/{project_id}/pipelines?per_page=20")


@tool
@tool_http_errors
def gitlab_runners_tool() -> list:
    """List all GitLab runners (read-only GET)."""
    return gitlab_get("/api/v4/runners/all")


@tool
@tool_http_errors
def gitlab_file_tool(project_id: int, file_path: str, ref: str = "main") -> str:
    """Read a file from a GitLab repository. Returns decoded file content (read-only).

    project_id: the numeric "id" field from a project in gitlab_projects_tool's
    output — call that tool first if you don't already have it.
    """
    encoded_path = urllib.parse.quote(file_path, safe="")
    data = gitlab_get(f"/api/v4/projects/{project_id}/repository/files/{encoded_path}?ref={ref}")
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return data.get("content", "")


@tool
@tool_http_errors
def gitlab_repository_tree_tool(
    project_id: int, path: str = "", ref: str = "main", recursive: bool = False
) -> list:
    """List files and directories in a GitLab repository path (read-only).

    project_id: the numeric "id" field from a project in gitlab_projects_tool's
    output — call that tool first if you don't already have it.
    """
    encoded_path = urllib.parse.quote(path, safe="")
    encoded_ref = urllib.parse.quote(ref, safe="")
    suffix = "&recursive=true" if recursive else ""
    return gitlab_get_paginated(
        f"/api/v4/projects/{project_id}/repository/tree?path={encoded_path}&ref={encoded_ref}{suffix}"
    )


@tool
@tool_http_errors
def gitlab_schedules_tool(project_id: int) -> list:
    """List pipeline schedules for a GitLab project (read-only)."""
    return gitlab_get_paginated(f"/api/v4/projects/{project_id}/pipeline_schedules")


@tool
@tool_http_errors
def gitlab_branches_tool(project_id: int) -> list:
    """List branches for a GitLab project (read-only)."""
    return gitlab_get_paginated(f"/api/v4/projects/{project_id}/repository/branches")
