"""SaaS admin/management API tools — read-only, METADATA-ONLY (GitLab #103).

Mirrors ``tools/octopus_tool.py``'s GET-only pattern: every call routes
through ``readonly_get`` (a ``ReadOnlyClient`` that structurally refuses any
non-GET/HEAD verb), so this module cannot mutate the SaaS admin API even if
asked to.

**METADATA-ONLY (hard safety rule, same bar as the secrets-manager
candidate #98):** every tool in this module strips any secret-shaped field
from every record it returns, at the tool boundary, BEFORE the agent ever
sees it — defense in depth on top of the agent's own metadata-only row
build. A secret VALUE must never leave this function.
"""

import logging

from langchain_core.tools import tool

from infra_brain.config import get_settings
from infra_brain.tools.http_readonly import readonly_get, tool_http_errors

log = logging.getLogger(__name__)

# Denylist of field names (case-insensitive) that could hold a secret/key
# value. Any such field is dropped from every record before it is returned —
# nothing beyond this function ever sees the raw value. Deliberately broad:
# a denylist here is inherently a best-effort compensating control, not a
# guarantee, so it errs toward over-stripping rather than under-stripping.
_SECRET_FIELDS = {
    "value",
    "secret",
    "secret_key",
    "client_secret",
    "key",
    "private_key",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "bearer",
    "pass",
    "cred",
    "credential",
    "credentials",
    "connection_string",
}


def _settings():
    return get_settings()


def _unconfigured() -> bool:
    """True when no SaaS admin API is configured — the layer-wide convention
    (see ``backup.py``, ``identity.py``, ``context7.py``) is for a tool to
    treat this as a clean no-op rather than let an unset base URL reach
    httpx as a schemeless/invalid request."""
    return not _settings().saas_admin_url


def _headers() -> dict:
    token = _settings().saas_admin_token
    return {"Authorization": f"Bearer {token}"} if token else {}


def _api_path(path: str) -> str:
    base = _settings().saas_admin_url.rstrip("/")
    return f"{base}{path}"


def _get(path: str) -> dict | list:
    """Authenticated GET to the configured SaaS admin API (read-only by construction)."""
    resp = readonly_get(
        _api_path(path),
        headers=_headers(),
        timeout=_settings().api_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()


def _strip_secret_fields(record):
    """Return *record* with every secret-shaped field removed, recursively.

    Case-insensitive match against ``_SECRET_FIELDS`` so a differently-cased
    upstream field name (``Value``, ``API_KEY``, ...) is still caught, and a
    secret nested under an innocuous parent key (``{"credentials": {"value":
    "..."}}``) is caught too since the check recurses into every dict/list
    value rather than only matching top-level keys. Non-dict/list input
    (e.g. an upstream shape change to a bare string) is returned unchanged —
    there is nothing to strip and the caller must not crash on it.
    """
    if isinstance(record, dict):
        return {
            k: _strip_secret_fields(v)
            for k, v in record.items()
            if k.lower() not in _SECRET_FIELDS
        }
    if isinstance(record, list):
        return [_strip_secret_fields(v) for v in record]
    return record


@tool
@tool_http_errors
def saas_applications_tool() -> list:
    """List third-party SaaS applications known to the configured admin API.

    Read-only GET. Returns application metadata only (name, vendor, category,
    owning team) — never credentials. Every record has any secret-shaped
    field (see ``_SECRET_FIELDS``) stripped before this function returns, the
    same defense-in-depth applied to ``saas_api_keys_tool`` — an application
    record can carry an embedded integration credential (``client_secret``,
    a webhook secret, an OAuth token) just as readily as a key record can.

    Returns ``[]`` if ``saas_admin_url`` is unset (nothing to poll) rather
    than raising — matches the unconfigured-is-a-no-op convention used by
    every sibling read-only tool module in this layer.
    """
    if _unconfigured():
        return []
    data = _get("/applications")
    records = data.get("items", data) if isinstance(data, dict) else data
    return [_strip_secret_fields(r) for r in records]


@tool
@tool_http_errors
def saas_api_keys_tool(app: str) -> list:
    """List API-key METADATA for one SaaS application — never the key value.

    Every record returned by the upstream API has any secret-shaped field
    (see ``_SECRET_FIELDS``) stripped before this function returns, so the
    key/token value never leaves this module.

    Returns ``[]`` if ``saas_admin_url`` is unset (nothing to poll) rather
    than raising — matches the unconfigured-is-a-no-op convention used by
    every sibling read-only tool module in this layer.
    """
    if _unconfigured():
        return []
    data = _get(f"/applications/{app}/api-keys")
    records = data.get("items", data) if isinstance(data, dict) else data
    return [_strip_secret_fields(r) for r in records]
