"""GET-only HTTP client — R2 layer 1: structural read-only for collectors.

Collectors must hold clients that PHYSICALLY refuse non-GET requests
(ARCHITECTURE-RECOMMENDATIONS.md §2). If the code can't mutate, we don't need
to check that it didn't. Sanctioned write paths (gitlab_mr / jira / confluence)
do NOT use this module — they go through callbacks/write_gate.py instead.
"""

from __future__ import annotations

from functools import wraps

import httpx

_SAFE_METHODS = frozenset({"GET", "HEAD"})


class ReadOnlyHTTPError(PermissionError):
    """A non-GET/HEAD request was attempted on a read-only client."""


class ReadOnlyClient(httpx.Client):
    """httpx.Client that refuses every non-GET/HEAD request at the client layer.

    All verb helpers (.post/.put/.patch/.delete) funnel through .request(),
    so overriding request() closes every path.
    """

    def request(self, method: str, url, **kwargs):
        if str(method).upper() not in _SAFE_METHODS:
            raise ReadOnlyHTTPError(
                f"[read-only] {method} {url} refused: collectors hold GET-only clients"
            )
        return super().request(method, url, **kwargs)


def readonly_get(
    url,
    *,
    params=None,
    headers=None,
    auth=None,
    verify=True,
    timeout=None,
    follow_redirects=False,
) -> httpx.Response:
    """Drop-in replacement for module-level ``httpx.get`` in collector code."""
    if timeout is None:
        from infra_brain.config import get_settings

        timeout = get_settings().api_timeout_seconds
    with ReadOnlyClient(
        auth=auth, verify=verify, timeout=timeout, follow_redirects=follow_redirects
    ) as client:
        return client.get(url, params=params, headers=headers)


def tool_http_errors(fn):
    """Decorator for ``@tool`` bodies: convert httpx transport/HTTP errors into
    ``ToolException`` so error handling is uniform across the HTTP-backed tools
    (LG-2 — matches ansible.py's existing ToolException usage).

    Deliberately re-raises ``ReadOnlyHTTPError`` (a ``PermissionError`` read-only
    denial) unchanged: the R2 boundary gate relies on ``PermissionError`` to
    signal a denial, so a read-only refusal must NOT be downgraded to a generic
    ``ToolException``. Applied UNDER ``@tool`` (``functools.wraps`` preserves the
    signature/docstring the tool schema is built from)."""
    from langchain_core.tools import ToolException

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ReadOnlyHTTPError:
            raise
        except httpx.HTTPError as exc:
            # Carry the HTTP status through onto the ToolException. Only the
            # message survived before, so a caller that needed to tell "the
            # file does not exist" (404) from "the server broke" (5xx) had no
            # option but to substring-match the httpx text. Additive: the
            # message is unchanged, and the attribute is simply absent for
            # transport-level errors that never got a response.
            tool_exc = ToolException(str(exc))
            response = getattr(exc, "response", None)
            if response is not None:
                tool_exc.status_code = getattr(response, "status_code", None)
            raise tool_exc from exc

    return wrapper
