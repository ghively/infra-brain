"""Wazuh Manager REST API tools — read-only by construction, with one narrow,
clearly-scoped exception for the JWT auth exchange the Wazuh API itself
requires (see ``_wazuh_authenticate`` below).

Wazuh 4.x (this deployment is 4.14.5 — see
``~/.hermes/wiki/entities/services/wazuh.md``) gates every Manager REST API
call (port 55000) behind a short-lived JWT obtained via
``POST /security/user/authenticate`` (HTTP Basic auth in, JWT out). There is
no GET-based login path — this is the one call in this module NOT issued
through ``tools.http_readonly.readonly_get``/``ReadOnlyClient``. It does not
mutate any monitored resource; it is a session/token exchange, structurally
identical to an OAuth client-credentials grant. Every subsequent Wazuh call
(agents, alerts) is a plain GET issued through ``readonly_get`` with the
resulting token as a Bearer header. Do not widen this exception to any other
endpoint.

F-02-style retry (matches ``octopus_tool.py``/``rapid7.py``): tenacity retry
on transient errors (timeout, connect error, 5xx). 4xx errors (bad
credentials, not-found) are never retried.

H-3 — NO TOOL IN THIS MODULE TAKES A CREDENTIAL AS AN ARGUMENT. The Manager
JWT and the Indexer username/password are resolved inside the tool bodies
(``_active_manager_token`` / ``settings.wazuh_indexer_*``). ``callbacks/
audit.py`` persists ``args_summary = input_str[:256]`` VERBATIM into
``AgentActionLog``/``AuditLog``, both queryable from the dashboard, and DLP
only scans for PANs — so credentials passed as tool arguments were written to
the audit trail in cleartext on every run. This mirrors
``tools/prometheus_tool.py`` / ``tools/alertmanager_tool.py``, and the rule
``agents/secrets_inventory.py`` documents. Do not re-add a credential
parameter to any tool here.
"""

import logging
import threading
import time

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


@_http_retry
def _wazuh_authenticate(
    base_url: str, username: str, password: str, ssl_verify: bool, timeout: float
) -> str:
    """POST /security/user/authenticate — Basic auth in, JWT out.

    See module docstring for why this is the sole non-GET call here. Raises
    httpx.HTTPError (propagated to the caller, ``get_wazuh_token``, which
    wraps it in a RuntimeError) on any transport/HTTP failure.
    """
    url = f"{base_url.rstrip('/')}/security/user/authenticate"
    with httpx.Client(verify=ssl_verify, timeout=timeout) as client:
        resp = client.post(url, auth=(username, password))
        resp.raise_for_status()
    payload = resp.json()
    token = (payload.get("data") or {}).get("token")
    if not token:
        raise httpx.HTTPError(f"Wazuh authentication response missing token: {payload!r}")
    return token


# H-3 in-process token handoff.
#
# The JWT must reach the Manager tools WITHOUT travelling as a tool argument
# (see the module docstring). It is therefore stashed here by
# ``get_wazuh_token`` and read back by ``_active_manager_token``.
#
# Why this rather than "let each tool authenticate itself": WazuhAgent's domain
# spec requires an authentication failure to propagate OUT of ``collect()`` so
# ``BaseAgent.run()`` records status="failed". If the tools authenticated
# internally, that failure would surface inside the per-fetch ``try`` blocks
# and be downgraded to a partial run — a security collector silently reporting
# "partial" instead of "failed" on bad credentials is exactly the blind spot
# the spec forbids. ``collect()`` therefore still calls ``get_wazuh_token``
# up-front (outside the try), which authenticates AND refreshes this value, so
# the "one auth exchange per run" property is preserved unchanged too.
#
# The TTL is a safety net for a direct tool call outside a collect() run, not
# a cache the normal path relies on: every run overwrites it with a fresh
# token, so a stale JWT can never be reused across runs.
_MANAGER_TOKEN_TTL_SECONDS = 600  # Wazuh JWTs are ~15 min; refresh well inside that.
_manager_token_lock = threading.Lock()
_manager_token: dict[str, object] = {"token": "", "expires_at": 0.0}


def get_wazuh_token(settings) -> str:
    """Authenticate against the Wazuh manager and return the raw JWT string.

    Called once per ``collect()`` run (Wazuh tokens are typically valid
    ~15 minutes — this always performs a FRESH authentication, never returning
    a cached token). Raises ``RuntimeError`` on ANY authentication failure
    (bad credentials, manager unreachable, malformed response). Callers must
    NOT swallow this into a self-skip — per the WazuhAgent domain spec, an
    auth failure here is a real collection failure, not an "unconfigured
    integration" no-op.

    Also publishes the token for in-process use by the Manager tools, so they
    never have to take it as an argument (H-3).
    """
    try:
        token = _wazuh_authenticate(
            settings.wazuh_url,
            settings.wazuh_username,
            settings.wazuh_password,
            getattr(settings, "wazuh_ssl_verify", True),
            getattr(settings, "api_timeout_seconds", 30),
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Wazuh authentication failed: {exc}") from exc
    with _manager_token_lock:
        _manager_token["token"] = token
        _manager_token["expires_at"] = time.monotonic() + _MANAGER_TOKEN_TTL_SECONDS
    return token


def _active_manager_token() -> str:
    """Return the JWT published by the current run's ``get_wazuh_token`` call.

    Falls back to authenticating from settings if nothing valid is published
    (a direct tool invocation outside a collect() run). Never accepts a token
    from the caller.
    """
    with _manager_token_lock:
        token = _manager_token.get("token") or ""
        expires_at = _manager_token.get("expires_at") or 0.0
    if token and time.monotonic() < expires_at:
        return str(token)
    return get_wazuh_token(get_settings())


def _require_configured_host(passed_url: str, configured_url: str, *, setting_name: str) -> None:
    """Refuse to proceed unless ``passed_url`` targets the credential's own
    configured host (MEDIUM-1 credential/host-binding fix).

    H-3 moved credential resolution inside the tool bodies so the raw
    credential never crosses a tool boundary into the (dashboard-queryable)
    audit trail. That fix left no relationship enforced between the resolved
    credential and whatever ``base_url``/``indexer_url`` the caller passed —
    a caller (or a bug) could attach the real Wazuh JWT to an arbitrary host.
    These tools currently aren't reachable by any LLM (not in
    ``chat/agent.py::TOOLS`` or ``runner/agent_task.py::TOOLS``) and are only
    ever invoked by ``WazuhAgent.collect()`` with a ``base_url`` it read
    straight from settings — but that is a convention, not a structural
    guarantee, and ``ReadOnlyToolValidator`` cannot backstop it either (it
    inspects ``inp["url"]``, not ``inp["base_url"]``/``inp["indexer_url"]``).

    Trailing slashes are insignificant (mirrors ``_url_matches_whitelist_prefix``
    in callbacks/readonly.py). An unconfigured setting (empty/blank) never
    matches anything, including an empty ``passed_url`` — there being nothing
    configured is exactly the case where no credential should ever be handed
    out. Raises ``RuntimeError`` (loud, never a silent skip).
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
    _require_configured_host(base_url, get_settings().wazuh_url, setting_name="wazuh_url")
    return {"Authorization": f"Bearer {_active_manager_token()}"}


@tool
@tool_http_errors
def wazuh_agents_tool(base_url: str, ssl_verify: bool, timeout: float, limit: int = 500) -> dict:
    """List registered Wazuh agents (read-only GET /agents).

    The Manager JWT is resolved inside this tool — it is deliberately NOT a
    parameter (H-3; see module docstring). The credential is only ever
    attached when ``base_url`` matches ``settings.wazuh_url`` (MEDIUM-1
    credential/host binding — see ``_require_configured_host``).

    Returns the raw Wazuh 4.x response envelope:
    ``{"data": {"affected_items": [...], "total_affected_items": N}, "error": 0}``.
    """
    url = f"{base_url.rstrip('/')}/agents?limit={limit}"
    resp = readonly_get(url, headers=_auth_headers(base_url), verify=ssl_verify, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


@tool
@tool_http_errors
def wazuh_alerts_tool(
    base_url: str,
    ssl_verify: bool,
    timeout: float,
    since_iso: str,
    limit: int = 200,
) -> dict:
    """List recent alerts (read-only GET /alerts), filtered to ``timestamp > since_iso``.

    The Manager JWT is resolved inside this tool — it is deliberately NOT a
    parameter (H-3; see module docstring). The credential is only ever
    attached when ``base_url`` matches ``settings.wazuh_url`` (MEDIUM-1
    credential/host binding — see ``_require_configured_host``).

    ENDPOINT CHOICE — see ``agents/wazuh.py``'s module docstring for the full
    rationale: Wazuh 4.x's Manager REST API (port 55000) is primarily
    inventory/config/rules; full alert *search* moved to the Wazuh Indexer
    (OpenSearch, ``wazuh-alerts-*``, port 9200 in this deployment) with the
    Elastic/OpenSearch-stack architecture. ``/alerts`` is targeted here
    because its response shape (``rule.id``/``rule.level``/``rule.description``
    + ``agent.id``/``agent.name`` + ``timestamp``) is exactly the per-alert
    item shape the domain spec calls for, and some 4.x deployments still
    expose it directly or via a reverse proxy in front of the indexer. If a
    real run 404s here, the caller (``WazuhAgent.collect``) treats this as a
    partial failure — agent inventory still collects successfully.

    ``since_iso`` uses the Wazuh ``q=`` filter DSL (``field>value``), e.g.
    ``"timestamp>2026-07-30T00:00:00"``.
    """
    q = f"timestamp>{since_iso}"
    url = f"{base_url.rstrip('/')}/alerts?limit={limit}&sort=-timestamp&q={q}"
    resp = readonly_get(url, headers=_auth_headers(base_url), verify=ssl_verify, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


@tool
@tool_http_errors
def wazuh_indexer_alerts_tool(
    indexer_url: str,
    ssl_verify: bool,
    timeout: float,
    since_iso: str,
    index_pattern: str = "wazuh-alerts-*",
    limit: int = 200,
) -> dict:
    """Search recent alerts in the Wazuh Indexer (OpenSearch), newest first.

    The Indexer basic-auth username/password are read from
    ``settings.wazuh_indexer_username`` / ``wazuh_indexer_password`` inside
    this tool — they are deliberately NOT parameters (H-3; see the module
    docstring: tool arguments are persisted verbatim into the
    dashboard-queryable audit trail). The credential is only ever attached
    when ``indexer_url`` matches ``settings.wazuh_indexer_url`` (MEDIUM-1
    credential/host binding — see ``_require_configured_host``).

    This is where alert search actually lives on Wazuh 4.x. The Manager REST
    API on port 55000 has no ``/alerts`` route — ``wazuh_alerts_tool`` above
    targeted it anyway and 404'd on every run against a stock install. See
    ``agents/wazuh.py``'s module docstring, which prescribed exactly this
    follow-up.

    Issued as a GET, deliberately. OpenSearch's ``_search`` is usually shown as
    a POST, but ``tools.http_readonly`` exposes no POST by design — collectors
    hold GET-only clients as a STRUCTURAL read-only guarantee, not a
    convention. OpenSearch accepts the identical request body via the ``source``
    /``source_content_type`` query parameters, so the full query DSL is
    available without widening that boundary. Do not "fix" this into a POST.

    Returns the raw OpenSearch response. Callers must treat
    ``_shards.total == 0`` as "the index pattern matches nothing" — a live
    finding (alerts are not being indexed at all), NOT an empty-but-healthy
    result. See ``WazuhAgent._collect_alerts``.
    """
    import json as _json
    from urllib.parse import quote

    body = {
        "size": limit,
        "sort": [{"timestamp": {"order": "desc"}}],
        "query": {"range": {"timestamp": {"gt": since_iso}}},
        # Fetch ONLY the fields WazuhAgent actually reads. Everything else in a
        # Wazuh alert — most significantly ``full_log`` — was being pulled over
        # the wire, scanned, and then discarded unused.
        #
        # That surplus is not free. ``full_log`` carries raw command lines, and
        # the DLP scanner (callbacks/dlp.py) fail-closes on any Luhn-valid
        # 13-19 digit run it sees in tool output. An Ansible temp directory
        #   ansible-tmp-<epoch>.<frac>-<pid>-<13 random digits>
        # yields pid+suffix = 16 digits beginning "65" — a real Discover IIN
        # band — so it satisfied both Luhn AND the _matches_card_network
        # corroboration TRK-098 added, and blocked alert ingestion outright
        # (wazuh stuck at "partial", zero alerts, from 2026-08-10 01:12).
        # Triaged against the live indexer: chance collision, no cardholder data
        # anywhere in the 24h window — the same false-positive class as TRK-098.
        #
        # Not fetching the field is the right fix, and strictly better than the
        # alternatives (a dlp.py carve-out, which is a bypass waiting to be
        # abused, or redacting at the collector, which still ingests the data
        # first). DLP is unweakened and still scans everything this tool
        # returns; there is simply no longer a payload we never wanted.
        "_source": [
            "timestamp",
            "id",
            "rule.level",
            "rule.description",
            "agent.name",
        ],
    }
    url = (
        f"{indexer_url.rstrip('/')}/{index_pattern}/_search"
        f"?source={quote(_json.dumps(body))}&source_content_type=application/json"
    )
    _settings = get_settings()
    _require_configured_host(
        indexer_url,
        getattr(_settings, "wazuh_indexer_url", "") or "",
        setting_name="wazuh_indexer_url",
    )
    username = (getattr(_settings, "wazuh_indexer_username", "") or "").strip()
    password = getattr(_settings, "wazuh_indexer_password", "") or ""
    resp = readonly_get(
        url,
        auth=(username, password),
        verify=ssl_verify,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()
