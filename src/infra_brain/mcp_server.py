"""Infra Brain MCP server — streamable-http transport on port 8002.

Query tools: read-only access to all Infra Brain collected data.
Management tools: secrets, agent deployment, collection triggers, proposal approvals.

Auth: DB-backed, per-key scoped API keys (see mcp_auth + _ApiKeyAuthMiddleware).
Run: python -m infra_brain.mcp_server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import uvicorn
import yaml
from fastmcp import FastMCP
from pydantic import BaseModel
from sqlalchemy import func, or_, text, tuple_
from sqlalchemy import inspect as sa_inspect
from starlette.middleware import Middleware

from infra_brain.callbacks.dlp import redact_pans_preserving_uuids
from infra_brain.db.models import (
    AgentActionLog,
    AgentConfigSetting,
    AgentDecisionLog,
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    AuditLog,
    BackupJob,
    CiPipelineRun,
    CiSchedule,
    CloudResource,
    CollectionRun,
    ComplianceViolation,
    ComposeService,
    ConfluencePage,
    Document,
    DriftEvent,
    EolRegistry,
    GitlabProject,
    HostCertificate,
    HostFirewallRule,
    HostIdentity,
    HostPurposeMap,
    HostSecurityPosture,
    HostShare,
    IacFile,
    Instinct,
    InventoryReconcileEvent,
    JiraTicket,
    K8sDeployment,
    K8sManifestResource,
    K8sNode,
    K8sPod,
    LinuxCron,
    LinuxHost,
    LinuxMount,
    LinuxNic,
    LinuxPackage,
    LinuxPendingUpdate,
    LinuxPort,
    LinuxUser,
    NetDevice,
    NetDiscoveryHost,
    Observation,
    ProposedAction,
    R7Asset,
    R7AssetAddress,
    R7AssetConfig,
    R7AssetUser,
    R7Software,
    R7Solution,
    R7VulnCve,
    R7Vulnerability,
    R7VulnSolution,
    Resource,
    RootCauseNote,
    ScanPoint,
    Snapshot,
    TerraformResource,
    VsphereAlarm,
    VsphereCluster,
    VsphereDatastore,
    VsphereHost,
    VspherePermission,
    VsphereSnapshot,
    VsphereVm,
    VulnQueueItem,
    WindowsLocalGroupMember,
    WindowsLocalUser,
    WindowsService,
    WindowsSoftware,
    drift_recency,
)
from infra_brain.db.session import get_session
from infra_brain.db.vuln_status import OPEN_VULN_STATUSES
from infra_brain.tools.client_state import record_client_state as _record_client_state
from infra_brain.tools.client_state import record_observation as _record_observation
from infra_brain.tools.documents import ingest_document as _ingest_document
from infra_brain.tools.documents import (
    update_document_metadata as _update_document_metadata,
)
from infra_brain.tools.environment_notes import (
    get_environment_notes as _get_environment_notes,
)
from infra_brain.tools.environment_notes import (
    record_environment_note as _record_environment_note,
)
from infra_brain.tools.environment_notes import (
    resolve_environment_note as _resolve_environment_note,
)
from infra_brain.tools.gitlab_issue import (
    comment_on_gitlab_issue as _comment_on_gitlab_issue,
)
from infra_brain.tools.gitlab_issue import create_gitlab_issue as _create_gitlab_issue
from infra_brain.tools.governance_events import (
    get_governance_events as _get_governance_events,
)
from infra_brain.tools.governance_events import (
    record_governance_event as _record_governance_event,
)
from infra_brain.tools.governance_events import (
    verify_governance_chain as _verify_governance_chain,
)
from infra_brain.tools.instinct_governance import (
    get_instinct_history as _get_instinct_history,
)
from infra_brain.tools.instinct_governance import (
    promote_instinct_v2 as _promote_instinct_v2,
)
from infra_brain.tools.instinct_governance import propose_instinct as _propose_instinct
from infra_brain.tools.instinct_governance import rollback_instinct as _rollback_instinct
from infra_brain.logging_config import configure_logging
from infra_brain.mcp_auth import (
    hash_token,
    lookup_active_key,
    touch_last_used,
)
from infra_brain.provenance import MANUAL_PROVENANCE_SOURCE as _MANUAL_PROVENANCE_SOURCE
from infra_brain.provenance import manual_banner
from infra_brain.tools.hostmatch import normalize_host

logger = logging.getLogger(__name__)

mcp = FastMCP("infra-brain")

from infra_brain.mcp_audit_middleware import McpAuditMiddleware  # noqa: E402

mcp.add_middleware(McpAuditMiddleware())


# ── Mutation gate ─────────────────────────────────────────────────────────────
# F-025: the MCP surface is externally reachable. Read-only query tools are always
# available; state-changing tools (seed_*, promote_instinct, approve_proposal,
# add_eol_product, trigger_collection) are DISABLED unless an operator explicitly
# opts in with INFRA_BRAIN_MCP_ENABLE_MUTATIONS=true. The RCE tools (deploy_agent /
# set_secret / update_config / get_agent_logs) were removed outright.


def _mutations_enabled() -> bool:
    """Also triggers the TRK-247 direct-invocation detect-and-log, so every
    mutating tool gets that coverage structurally through the ``if not
    _mutations_enabled(): return _mutation_disabled_response()`` gate every
    one of them already opens with -- not just the subset that separately
    calls ``_caller_identity()`` for attribution. lc-safety-reviewer found
    several mutating tools (``seed_resource``, ``seed_resources_bulk``,
    ``seed_drift_event``, ``seed_vulnerability``, ``promote_instinct``,
    ``add_eol_product``, ``confirm_same_as``) had no coverage at all before
    this -- ``confirm_same_as`` in particular writes a caller-suppliable
    ``approver`` string with no independent check.
    """
    from infra_brain.config import get_settings

    enabled = os.getenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "").lower() in {"1", "true", "yes"}
    if not enabled:
        return False
    is_http = _has_active_http_request()
    # P4.4a residual gap (b), opt-in: _has_active_http_request() detect-and-
    # stamps the TRK-247 direct-invocation shape but deliberately never blocks
    # it (documented above — a legitimate ops/debug path). That default is
    # unchanged. This flag gives an operator who wants a harder boundary (e.g.
    # a genuinely production deployment) a real lever to close the gap
    # instead of only auditing it — default False preserves today's behavior
    # exactly.
    if not is_http and get_settings().infra_brain_mcp_deny_direct_invocation:
        return False
    return True


def _mutation_disabled_response() -> dict:
    from infra_brain.config import get_settings

    if get_settings().infra_brain_mcp_deny_direct_invocation and not _has_active_http_request():
        return {
            "error": "direct in-process MCP tool invocation is denied "
            "(INFRA_BRAIN_MCP_DENY_DIRECT_INVOCATION=true) — dispatch through "
            "the real MCP HTTP transport instead"
        }
    return {
        "error": "mutating MCP tools are disabled; "
        "set INFRA_BRAIN_MCP_ENABLE_MUTATIONS=true to enable"
    }


# ── Caller identity (unforgeable attribution) ────────────────────────────────
# Attribution on a human gate must come from the AUTHENTICATED KEY, never from
# a caller-supplied string: a key scoped only to approve_proposal could
# otherwise pass approved_by="youruser" and spoof the one human gate in front of
# a sanctioned external write (RemediationAgent opening a real GitLab MR once
# status='approved'), or write authored_by="RootCauseAgent" onto a manual note.
#
# The mechanism is the same one McpAuditMiddleware already uses to derive
# caller_key_hash inside a tool call's execution context:
# fastmcp.server.dependencies.get_http_headers(), which is request-scoped and
# valid for the duration of the dispatched call. We read the bearer token from
# there and resolve it to the McpApiKey row's ``name``
# (mcp_auth.lookup_active_key_name) — the same token the ASGI
# _ApiKeyAuthMiddleware already authenticated and tool-scoped upstream, so by
# the time a tool body runs the token is known-good.
#
# Callers may still pass a free-text LABEL, but it is only ever APPENDED to the
# derived identity ("mcp:key-name (says: label)") and can never replace or
# forge the identity portion under any input.

CALLER_IDENTITY_PREFIX = "mcp:"
# Dev-mode (INFRA_BRAIN_DEV=1) runs the MCP server with no auth at all, and
# assert_dev_not_in_hardened_env() makes that a hard startup failure on a
# deployed stack. An unresolvable caller is attributed to this sentinel rather
# than to anything that could be mistaken for a real operator or agent.
UNAUTHENTICATED_CALLER_IDENTITY = "mcp:unauthenticated"

# TRK-247 (Phase 3.1, decided 2026-07-29): mcp_server.py's tool bodies can be
# invoked directly in-process (e.g. `docker exec ... python -c "from
# infra_brain.mcp_server import record_rootcause_note; ..."`), bypassing BOTH
# the ASGI auth layer and the HTTP-request-scoped McpAuditMiddleware entirely.
# Before this guard, that shape was indistinguishable from a genuine
# unauthenticated HTTP call -- both fell through to UNAUTHENTICATED_CALLER_IDENTITY
# -- which is exactly how 928 root-cause notes ended up silently misattributed.
# This sentinel marks the DIRECT-INVOCATION shape specifically, so it reads
# differently in the DB than a real (if unauthenticated) HTTP call.
#
# This is detect-and-stamp ONLY: it does not block direct invocation, which
# remains a legitimate ops/debug path. It is complementary to, not a
# replacement for, the identity derivation below -- a real HTTP call (with or
# without a bearer token) is unaffected and still resolves exactly as before.
DIRECT_INVOCATION_IDENTITY = "direct:unattributed"

# ProposedAction.approved_by is String(128); keep the composed value inside it.
_AUTHOR_MAX_LEN = 128
_AUTHOR_LABEL_MAX_LEN = 64


def _has_active_http_request() -> bool:
    """True iff this call is running inside a real ASGI/MCP HTTP request.

    ``get_http_headers()`` (used below) never raises and returns ``{}`` both
    when there's no active HTTP request AND when there is one but it just
    lacks an Authorization header -- so it can't tell "direct in-process call"
    apart from "genuine unauthenticated HTTP call". ``get_http_request()`` can:
    it raises ``RuntimeError`` only when there is no request context at all
    (see fastmcp.server.dependencies), which is precisely the TRK-247
    docker-exec-bypass shape.

    The detect-and-log for that shape lives HERE, not in each caller: this is
    the single source of truth for the detection, called both from
    ``_caller_identity()`` (tools that attribute writes) and from
    ``_mutations_enabled()`` (every mutating tool, including the several that
    never call ``_caller_identity()`` at all -- lc-safety-reviewer found the
    guard was previously only reachable through the attribution path, so
    e.g. ``confirm_same_as``'s caller-suppliable ``approver`` field got no
    coverage whatsoever). A call site that legitimately checks both ends up
    logging twice per request -- harmless duplication, not a correctness gap.
    """
    from fastmcp.server.dependencies import get_http_request

    try:
        get_http_request()
        return True
    except RuntimeError:
        logger.warning(
            "TRK-247 direct-invocation shape detected: this MCP tool call has no "
            "active HTTP request context, meaning it was NOT dispatched through "
            "the real ASGI/MCP HTTP transport (e.g. a direct in-process/docker-exec "
            "call). This does not block the call -- direct invocation remains a "
            "permitted ops/debug path -- but McpAuditMiddleware never runs for it "
            "either (it is HTTP-request-scoped), so no agent_action_log row will "
            "exist for this write. Any attribution field this tool sets will be "
            "stamped %r instead of a real caller identity.",
            DIRECT_INVOCATION_IDENTITY,
        )
        return False
    except Exception:
        # Any other resolution failure is not evidence of direct invocation --
        # don't misclassify a transport quirk as a bypass. Log it (silently
        # falling through here previously meant a future fastmcp/contextvar
        # change could quietly restore the pre-guard blind spot with no
        # operator-visible signal -- lc-safety-reviewer finding) and fall
        # through to the normal header-based path (whose own broad except
        # below already degrades safely to UNAUTHENTICATED_CALLER_IDENTITY).
        logger.warning(
            "TRK-247 guard could not determine HTTP request context "
            "(treating as HTTP path, not direct invocation)",
            exc_info=True,
        )
        return True


def _caller_identity() -> str:
    """Identity of the authenticated MCP key making THIS call. Never caller-set.

    Returns ``mcp:<key name>`` for an authenticated call, ``mcp:unauthenticated``
    when no usable bearer token is present (dev mode) or the token cannot be
    resolved to a live key, or ``direct:unattributed`` (loudly logged by
    ``_has_active_http_request()``) when there is no active HTTP request
    context at all -- i.e. this tool function was called directly in-process
    rather than dispatched over the real ASGI/MCP HTTP transport (TRK-247).
    Never raises — attribution failing must not turn a valid tool call into
    an error, and every fallback path lands on a sentinel rather than on a
    forgeable value.
    """
    if not _has_active_http_request():
        return DIRECT_INVOCATION_IDENTITY

    from fastmcp.server.dependencies import get_http_headers

    from infra_brain.mcp_auth import lookup_active_key_name

    try:
        headers = get_http_headers(include={"authorization"})
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return UNAUTHENTICATED_CALLER_IDENTITY
        token = auth[len("bearer ") :].strip()
        if not token:
            return UNAUTHENTICATED_CALLER_IDENTITY
        with get_session() as s:
            name = lookup_active_key_name(s, token)
    except Exception:
        logger.warning("could not resolve MCP caller identity", exc_info=True)
        return UNAUTHENTICATED_CALLER_IDENTITY
    if not name:
        return UNAUTHENTICATED_CALLER_IDENTITY
    return f"{CALLER_IDENTITY_PREFIX}{name}"


def _attributed_author(label: str | None = None) -> str:
    """Compose the recorded author: server-derived identity + optional label.

    The identity prefix is ALWAYS the authenticated key's, so the value written
    to the DB cannot be forged. *label* is an optional human hint from the
    caller and is clearly quoted as a claim ("says:"), never presented as the
    identity itself.
    """
    identity = _caller_identity()
    text = (label or "").strip()
    if not text:
        return identity[:_AUTHOR_MAX_LEN]
    return f"{identity} (says: {text[:_AUTHOR_LABEL_MAX_LEN]})"[:_AUTHOR_MAX_LEN]


# ── Caller-input bounds + DLP scrubbing for free-text writes ─────────────────
# The manual-write tools persist caller free text that is later re-served
# (dashboard, get_audit_log, Instinct promotion). Two guards apply at WRITE
# time: a size/depth bound so an oversized payload never reaches the DB, and
# redact_pans() so a card number pasted into an explanation is not stored and
# replayed in the clear (same convention as agents/drift_learning.py).

_FREE_TEXT_MAX_LEN = 8000
_CORRELATED_MAX_BYTES = 16384
_CORRELATED_MAX_DEPTH = 10


def _redact_deep(value: Any, _depth: int = 0) -> Any:
    """PAN scrub applied recursively over every string in a JSON blob.

    Uses the UUID-preserving variant (TRK-347): these blobs are operator
    notes that routinely reference drift/resource ids, and a PAN-shaped
    UUID (~1 in 40,000) must not be mangled into an unusable reference.
    """
    if _depth > _CORRELATED_MAX_DEPTH:
        return value
    if isinstance(value, str):
        return redact_pans_preserving_uuids(value)
    if isinstance(value, dict):
        return {k: _redact_deep(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_deep(v, _depth + 1) for v in value]
    return value


def _json_depth(value: Any, _depth: int = 0) -> int:
    if _depth > _CORRELATED_MAX_DEPTH:
        return _depth
    if isinstance(value, dict):
        return max((_json_depth(v, _depth + 1) for v in value.values()), default=_depth)
    if isinstance(value, list):
        return max((_json_depth(v, _depth + 1) for v in value), default=_depth)
    return _depth


def _check_free_text(name: str, value: str) -> dict | None:
    """Return an error dict when *value* is blank or over the length cap."""
    if not value or not value.strip():
        return {"error": f"{name} must be non-empty (whitespace-only is rejected)"}
    if len(value) > _FREE_TEXT_MAX_LEN:
        return {
            "error": (
                f"{name} exceeds the {_FREE_TEXT_MAX_LEN}-character limit "
                f"(got {len(value)}); nothing was written"
            )
        }
    return None


def _check_correlated(correlated: Any) -> dict | None:
    """Return an error dict when the ``correlated`` JSONB blob is unusable —
    wrong type, too large serialized, or nested too deeply."""
    if correlated is None:
        return None
    if not isinstance(correlated, dict):
        return {"error": "correlated must be an object/dict when provided"}
    try:
        encoded = json.dumps(correlated, default=str).encode()
    except (TypeError, ValueError) as exc:
        return {"error": f"correlated must be JSON-serializable: {exc}"}
    if len(encoded) > _CORRELATED_MAX_BYTES:
        return {
            "error": (
                f"correlated exceeds the {_CORRELATED_MAX_BYTES}-byte limit "
                f"(got {len(encoded)}); nothing was written"
            )
        }
    if _json_depth(correlated) > _CORRELATED_MAX_DEPTH:
        return {
            "error": (
                f"correlated is nested deeper than {_CORRELATED_MAX_DEPTH} levels; "
                "nothing was written"
            )
        }
    return None


# ── Hardened-environment dev-mode guard (F23) ─────────────────────────────────


def assert_dev_not_in_hardened_env() -> None:
    """Refuse to boot the MCP server when dev-mode is on in a hardened env.

    INFRA_BRAIN_DEV=1 lets the MCP server run WITHOUT authentication (see
    _ApiKeyAuthMiddleware). That bypass must never be active on a deployed stack.
    Mirrors main._assert_dev_not_in_hardened_env(): ENVIRONMENT defaults to
    "development"; hardened deployments set ENVIRONMENT=deployed ("production"
    is a legacy alias), which makes the dev bypass a hard startup failure here.
    """
    from infra_brain.config import get_settings, is_hardened_environment

    settings = get_settings()
    if settings.infra_brain_dev and is_hardened_environment(settings.environment):
        raise RuntimeError(
            f"Refusing to start the MCP server: INFRA_BRAIN_DEV is enabled while "
            f"ENVIRONMENT={settings.environment.strip()} (a hardened deployment). "
            "Dev mode runs the MCP server without authentication — it must never run "
            "on a deployed stack. Unset INFRA_BRAIN_DEV on the deployed stack, or set "
            "ENVIRONMENT=development for a local dev run."
        )


# ── Auth middleware (scoped, DB-backed API keys) ─────────────────────────────
# Replaces the single global INFRA_BRAIN_MCP_TOKEN bearer check. Pure ASGI (not
# BaseHTTPMiddleware) so it can buffer + replay the JSON-RPC body: per-key tool
# scoping requires reading params.name from a tools/call request BEFORE dispatch.
# The sync DB lookup runs off the event loop via asyncio.to_thread (CLAUDE.md
# #2/#3). INFRA_BRAIN_MCP_ENABLE_MUTATIONS is unchanged and independent — a key
# scoped to a mutating tool still can't call it unless the flag is set.


# Deny reason codes emitted by _authorize() and surfaced BOTH in the JSON error
# body and in the AgentActionLog.error column of the audit row written on deny
# (GitLab #167 — denials at this layer used to leave no audit trail at all,
# because the middleware returns before McpAuditMiddleware.on_call_tool ever
# runs). Keep these three stable: dashboards/queries filter on them.
DENY_KEY_INVALID = "key_invalid_or_expired"  # 401: no active McpApiKey for this token
DENY_KEY_LACKS_SCOPE = "key_lacks_scope"  # 403: valid key, tool not in allowed_tools
DENY_UNPARSEABLE = "unparseable_request"  # 403: body shape can't be classified (fail closed)

_DENY_MESSAGES = {
    DENY_KEY_INVALID: "invalid or expired MCP API key",
    DENY_KEY_LACKS_SCOPE: (
        "this MCP API key's allowed_tools does not include the requested tool "
        "(this can happen for a READ-only tool too, e.g. a key issued before "
        "the tool existed — amend the key's allowed_tools rather than assuming "
        "write scope is required; see mcp_auth.MUTATION_TOOL_NAMES for the "
        "tools that actually require write scope)"
    ),
    DENY_UNPARSEABLE: "request body could not be classified for tool-scope checking",
}


async def _send_status(send, code: int, text: str, reason: str | None = None) -> None:
    """Emit a JSON-shaped error body so a client can discriminate deny causes.

    Shape is deliberately consistent with _mutation_disabled_response() (an
    ``error`` string) but NOT merged with it: that path is a separate,
    per-tool-body gate on INFRA_BRAIN_MCP_ENABLE_MUTATIONS and returns 200 with
    an error payload, whereas this is the transport-level auth boundary
    returning 401/403. ``reason`` is one of the DENY_* codes above.
    """
    payload: dict[str, str] = {"error": _DENY_MESSAGES.get(reason or "", text)}
    if reason:
        payload["reason"] = reason
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _requested_tool_names(body: bytes) -> list[str] | None:
    """Extract every requested tool name from a JSON-RPC body.

    Returns [] when the body clearly contains no tools/call request (a
    single-object request for another method, or an empty/no-op body) — no
    per-tool scope check needed. Returns a list of one-or-more tool names
    for a normal single tools/call request, or for a JSON-RPC batch array
    (each tools/call entry contributes its name) — ALL must be in the
    caller's allowed_tools. Returns None for anything that can't be
    confidently classified (malformed batch entries, non-JSON/non-dict/
    non-list top-level shape that isn't empty) — callers must fail closed
    (deny) on None, since silently skipping the scope check here is exactly
    the bug this replaces.
    """
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        if payload.get("method") != "tools/call":
            return []
        params = payload.get("params")
        if isinstance(params, dict):
            name = params.get("name")
            return [name] if isinstance(name, str) else None
        return None
    if isinstance(payload, list):
        names: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                return None
            if item.get("method") != "tools/call":
                continue
            params = item.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return None
            names.append(params["name"])
        return names
    return None


def _authorize(token: str, tool_names: list[str] | None) -> tuple[bool, int, str | None]:
    """Sync auth: (ok, status, reason_code). Runs in a worker thread.

    status is 401/403/200; reason_code is one of the DENY_* constants above on
    a deny and None on allow. Tools requiring WRITE scope are exactly
    ``mcp_auth.MUTATION_TOOL_NAMES`` — that list is the single source of truth
    (see ``write_scope_tool_table()`` for a rendered table); this function does
    not re-encode it, it only checks membership in the key's ``allowed_tools``.
    """
    with get_session() as s:
        found = lookup_active_key(s, token)
        if found is None:
            return False, 401, DENY_KEY_INVALID
        key_id, allowed_tools = found
        if tool_names is None:
            # ambiguous/unparseable body shape — fail closed
            return False, 403, DENY_UNPARSEABLE
        if any(name not in allowed_tools for name in tool_names):
            return False, 403, DENY_KEY_LACKS_SCOPE
        # P4.4a residual gap (a): a full-access key (allowed_tools == every
        # known tool, minted by --bootstrap) authorizing a WRITE is not itself
        # denied here — that would break the documented "so nothing breaks
        # mid-cutover" bootstrap path — but it IS made visible, which the
        # audit found missing entirely. Scoped keys performing the identical
        # mutation are unaffected (this only fires when the key's scope is
        # the full catalog, not merely "happens to include this one tool").
        from infra_brain.mcp_auth import ALL_TOOL_NAMES as _ALL_TOOL_NAMES
        from infra_brain.mcp_auth import MUTATION_TOOL_NAMES as _MUTATION_TOOL_NAMES

        if set(allowed_tools) == set(_ALL_TOOL_NAMES) and any(
            n in _MUTATION_TOOL_NAMES for n in tool_names
        ):
            logger.warning(
                "full-access MCP key (key_id=%s) authorized a write (%s) — "
                "consider minting a narrowly-scoped key for routine mutation "
                "traffic instead of using the bootstrap/full-access key",
                key_id,
                tool_names,
            )
        touch_last_used(s, key_id)  # best-effort; never fails the call
        s.commit()
        return True, 200, None


_AUTH_DENIAL_WINDOW_SECONDS = 60.0
_AUTH_DENIAL_WINDOW_MAX_WRITES = 200
_auth_denial_state_lock = threading.Lock()
# (caller_key_hash-or-None, reason) -> monotonic time of the last write for
# that pair. A caller repeating the SAME token gets at most one row per
# window per reason instead of one per request.
_auth_denial_last_write: dict[tuple[str | None, str], float] = {}
# Separate GLOBAL cap: an attacker rotating a DIFFERENT bogus token per
# request defeats the per-key dedup above entirely (every key is "new"), so
# this bounds TOTAL denial-audit writes across all callers/reasons within the
# same window regardless of key identity.
_auth_denial_window_start = 0.0
_auth_denial_window_count = 0


def _auth_denial_should_write(key_hash: str | None, reason: str) -> bool:
    """GitLab #171: bound agent_action_log write-amplification from denials.

    Best-effort rate limiter, in-process (per worker, not cross-process --
    acceptable here since the goal is capping amplification, not exact
    counting). Returns False to suppress a write that would otherwise happen
    for every single denied request, which is itself an unauthenticated,
    attacker-triggerable DB-write and thread-pool-exhaustion surface (a burst
    of bogus-token requests floods this table one INSERT per request).
    Suppressed denials are still 401/403'd as before -- only the audit ROW is
    skipped, never the deny decision itself.
    """
    global _auth_denial_window_start, _auth_denial_window_count  # noqa: PLW0603
    now = time.monotonic()
    with _auth_denial_state_lock:
        # Global window: reset the counter once the window rolls over.
        if now - _auth_denial_window_start > _AUTH_DENIAL_WINDOW_SECONDS:
            _auth_denial_window_start = now
            _auth_denial_window_count = 0
        if _auth_denial_window_count >= _AUTH_DENIAL_WINDOW_MAX_WRITES:
            return False
        # Per-(key, reason) window: skip a repeat within the window.
        pair = (key_hash, reason)
        last = _auth_denial_last_write.get(pair)
        if last is not None and now - last <= _AUTH_DENIAL_WINDOW_SECONDS:
            return False
        _auth_denial_last_write[pair] = now
        _auth_denial_window_count += 1
        # Opportunistic cleanup so this dict cannot grow unbounded across the
        # process lifetime from distinct-token probing (the scenario the
        # global cap above already rate-limits writes for, but the dict
        # itself would otherwise still accumulate one entry per distinct
        # token forever).
        if len(_auth_denial_last_write) > 10_000:
            cutoff = now - _AUTH_DENIAL_WINDOW_SECONDS
            for k, v in list(_auth_denial_last_write.items()):
                if v < cutoff:
                    del _auth_denial_last_write[k]
        return True


def _record_auth_denial(token: str, tool_names: list[str] | None, reason: str) -> None:
    """Write ONE AgentActionLog row for a denial at the auth boundary (#167).

    Best-effort by contract: any failure here is logged and swallowed, because a
    DB blip must never turn a 403 into a 500 (and must never turn a deny into an
    allow — the caller has already decided to deny before calling this).

    Only the sha256 of the bearer token is stored (``caller_key_hash``, the same
    convention as McpAuditMiddleware and McpApiKey.token_hash) — the raw token is
    NEVER written anywhere.

    NOTE: rows land in ``agent_action_log`` (surfaced by the ``get_agent_activity``
    MCP tool), NOT in ``audit_log`` (``get_audit_log``) — those are two different
    tables and no MCP call, allowed or denied, appears in the latter.

    GitLab #171: rate-limited via :func:`_auth_denial_should_write` before
    touching the DB at all — an unauthenticated caller hammering this path
    (a fixed bad token, or worse, a fresh bogus token per request) would
    otherwise turn the #167 audit fix itself into a write-amplification /
    thread-pool-exhaustion surface, one INSERT per request with no cap.

    The rate limiter suppresses most of these requests' agent_action_log ROW,
    but total denial VOLUME must stay observable regardless — otherwise an
    operator cannot tell "denials stopped" from "denials stopped being
    recorded" during exactly the flood the limiter exists to survive. Count
    every denial via Prometheus BEFORE the rate-limit check, labeled with
    whether this particular one got an audit row.
    """
    key_hash = hash_token(token) if token else None
    audited = _auth_denial_should_write(key_hash, reason)
    from infra_brain.mcp_metrics import MCP_AUTH_DENIALS_TOTAL  # noqa: PLC0415

    MCP_AUTH_DENIALS_TOTAL.labels(reason=reason, audited=str(audited).lower()).inc()
    if not audited:
        return
    try:
        requested = list(tool_names) if tool_names else []
        with get_session() as s:
            s.add(
                AgentActionLog(
                    id=uuid.uuid4(),
                    agent="mcp",
                    domain="mcp",
                    # Normally exactly one name. A JSON-RPC batch can name
                    # several, and scope is all-or-nothing across the batch, so
                    # record ALL of them comma-joined rather than an arbitrary
                    # one that may not even be the offender. "unknown" when the
                    # body was unparseable. args_summary carries the same list
                    # structurally, un-truncated by the 128-char column limit.
                    # requested_tools is caller-controlled and unauthenticated
                    # at this point in the pipeline (this fires before scope
                    # is granted) -- redact PANs the same as every other MCP
                    # audit writer (_record_closure_audit, McpAuditMiddleware).
                    # Redact BEFORE truncating (not after) so a PAN-shaped
                    # digit run straddling the 128-char cut can never survive
                    # as an unmatched fragment — same order as args_summary
                    # below and mcp_audit_middleware._summarize_args().
                    tool=redact_pans_preserving_uuids(",".join(requested) if requested else "unknown")[:128],
                    args_summary=redact_pans_preserving_uuids(json.dumps({"requested_tools": requested}))[:2000],
                    verdict="deny",
                    status="denied",
                    error=reason,
                    caller_key_hash=key_hash,
                )
            )
            s.commit()
    except Exception:
        logger.exception(
            "MCP auth: failed to record denial audit row (reason=%s) — denying anyway",
            reason,
        )


def _authorize_and_audit(token: str, tool_names: list[str] | None) -> tuple[bool, int, str | None]:
    """_authorize() plus a best-effort audit row on deny, in one worker thread.

    Kept in the SAME asyncio.to_thread call as the auth lookup so the extra
    blocking DB write never touches the event loop (CLAUDE.md #2/#3). Note the
    denial write is deliberately OUTSIDE _authorize's own try-surface: if the
    auth lookup itself raises (DB unreachable), that exception propagates
    unchanged and the request still fails closed — see
    tests/test_mcp_auth_middleware.py::test_db_error_during_authorize_denies_not_allows.
    """
    ok, status, reason = _authorize(token, tool_names)
    if not ok and reason is not None:
        _record_auth_denial(token, tool_names, reason)
    return ok, status, reason


class _ApiKeyAuthMiddleware:
    """ASGI bearer-key auth + per-key tool-scope enforcement for the MCP server.

    Every DENY decided here writes one best-effort ``agent_action_log`` row with
    ``verdict="deny"``, ``status="denied"`` and ``error=<DENY_* reason code>``
    before the 401/403 is sent (GitLab #167): this middleware returns WITHOUT
    calling the wrapped app, so ``McpAuditMiddleware.on_call_tool`` — the only
    other writer of MCP audit rows — never runs on a denied call and denials
    were previously invisible. The one exception is a request with NO bearer
    header at all: it is rejected before the body is even buffered and has no
    key identity to attribute, so it is intentionally NOT persisted (otherwise
    any unauthenticated prober could drive unbounded DB writes).

    Tools that require WRITE scope are exactly ``mcp_auth.MUTATION_TOOL_NAMES``;
    call ``mcp_auth.write_scope_tool_table()`` for the rendered list rather than
    maintaining a second copy of it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # No non-HTTP routes (e.g. websocket) exist in this app today. Refuse
            # rather than forwarding unauthenticated, so one added later can't
            # silently bypass auth via this middleware.
            return

        # /metrics and /healthz are no-auth, zero-extra-I/O — same trust tier
        # as main.py's /healthz (mcp_metrics.py's own docstring states this
        # convention). They must be exempted here since this middleware wraps
        # the entire app, or Prometheus scraping / the container healthcheck
        # 401s in every non-dev environment (TRK-258(1): the healthcheck used
        # to probe the auth-gated /mcp endpoint, producing a 401 on every
        # check and burying real 401s in that noise).
        if scope.get("path") in ("/metrics", "/healthz"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        authorization = headers.get(b"authorization", b"").decode()
        token = authorization[7:] if authorization.startswith("Bearer ") else ""

        if not token:
            if os.getenv("INFRA_BRAIN_DEV", "") == "1":
                logger.warning(
                    "INFRA_BRAIN_DEV=1 — MCP server running WITHOUT authentication (dev mode only)"
                )
                await self.app(scope, receive, send)
                return
            await _send_status(send, 401, "Unauthorized")
            return

        # Buffer the full request body so we can inspect the tool name AND replay
        # it downstream unchanged.
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                more = message.get("more_body", False)
            else:  # http.disconnect
                more = False

        tool_names = _requested_tool_names(body)
        ok, status, reason = await asyncio.to_thread(_authorize_and_audit, token, tool_names)
        if not ok:
            await _send_status(
                send, status, "Unauthorized" if status == 401 else "Forbidden", reason
            )
            return

        replayed = False

        async def _replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            # After the buffered body is replayed once, delegate to the REAL
            # receive() channel for everything after — do NOT fabricate an
            # immediate http.disconnect here. The streamable-http session
            # handler awaits receive() again later in a long-lived
            # POST/GET connection (e.g. to detect a genuine client
            # disconnect); an immediately-fabricated disconnect makes it
            # think the client hung up right after the request, aborting
            # the response before it's written ("ASGI callable returned
            # without completing response") — this was breaking every
            # tools/call and the SSE GET stream once this middleware
            # shipped, never caught because no dispatched batch actually
            # exercised the full protocol end-to-end with a real client.
            return await receive()

        await self.app(scope, _replay, send)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _row_to_dict(obj: Any) -> dict:
    # Serialize by mapped ATTRIBUTE name, not raw column name. Resource.metadata_
    # maps to a DB column literally named "metadata"; iterating __table__.columns
    # would do getattr(obj, "metadata"), which returns SQLAlchemy's reserved
    # declarative MetaData object (unserializable) instead of the JSONB payload —
    # breaking structured MCP output for query_resources only.
    d: dict = {}
    for attr in sa_inspect(obj).mapper.column_attrs:
        v = getattr(obj, attr.key)
        d[attr.key] = v.isoformat() if isinstance(v, datetime) else v
    return d


def _now_utc() -> datetime:
    return datetime.now(UTC)


# ── Query tools ──────────────────────────────────────────────────────────────


@mcp.tool
def query_resources(
    domain: str | None = None, type: str | None = None, limit: int = 50
) -> list[dict]:
    """Return collected infrastructure resources (hosts, projects, deployments, etc.)."""
    with get_session() as s:
        q = s.query(Resource)
        if domain:
            q = q.filter(Resource.domain == domain)
        if type:
            q = q.filter(Resource.type == type)
        return [_row_to_dict(r) for r in q.order_by(Resource.last_seen.desc()).limit(limit).all()]


@mcp.tool
def get_drift_events(
    status: str = "open",
    hours: int = 24,
    domain: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_graph_maintenance: bool = False,
    has_note: bool | None = None,
) -> dict:
    """Return config drift events, optionally filtered by status and recency.

    TRK-191: graph_maintenance's own "graph-health" report resource is
    excluded by default — it captures the maintenance agent's own
    ever-changing internal stats (timings, typed-edge counts, ...), never real
    fleet drift, and used to pollute this feed with non-fleet noise. Pass
    ``domain="graph_maintenance"`` (an explicit include filter) or
    ``include_graph_maintenance=True`` (to see it mixed in alongside every
    other domain) to opt back in.

    ``has_note`` (Phase 2, 2026-07-29 plan): optional narrowing on whether a
    ``RootCauseNote`` already exists for the event. ``False`` returns only
    events with NO note (an anti-join via ``~exists()``, index-backed by the
    ``uq_rootcause_drift`` unique index on ``root_cause_notes.drift_event_id``
    — the actual "un-noted work queue"). ``True`` returns only events WITH a
    note (a semi-join). The default, ``None``, applies no filter at all and
    preserves byte-identical existing behavior for every caller that predates
    this param.

    ``offset`` (TRK-272 / GitLab #145) pages through result sets larger than
    ``limit`` (rows are stably ordered by ``detected_at`` descending). The
    response is a dict of ``{"items": [...], "total_count": N}`` — ``items``
    holds exactly what this tool used to return as a bare list; ``total_count``
    is the count of matching rows BEFORE ``offset``/``limit`` are applied, via
    a separate ``.count()`` query so the full result set is never materialized
    just to size it.
    """
    cutoff = _now_utc() - timedelta(hours=hours)
    with get_session() as s:
        q = (
            s.query(
                DriftEvent,
                Resource.name.label("resource_name"),
                Resource.domain.label("resource_domain"),
            )
            .join(Resource, Resource.id == DriftEvent.resource_id)
            .filter(DriftEvent.detected_at >= cutoff)
        )
        if status:
            q = q.filter(DriftEvent.status == status)
        if domain:
            q = q.filter(Resource.domain == domain)
        elif not include_graph_maintenance:
            q = q.filter(Resource.domain != "graph_maintenance")
        if has_note is not None:
            note_exists = (
                s.query(RootCauseNote)
                .filter(RootCauseNote.drift_event_id == DriftEvent.id)
                .exists()
            )
            q = q.filter(note_exists if has_note else ~note_exists)
        total_count = q.count()
        rows = []
        # GitLab #163/#164: coalesce(last_seen_at, detected_at) — detected_at is
        # now the immutable FIRST-observation stamp, so ordering by it alone
        # buries a finding that is still being re-observed every sweep.
        page = q.order_by(drift_recency().desc()).offset(offset).limit(limit).all()
        for de, rname, rdomain in page:
            d = _row_to_dict(de)
            d["resource_name"] = rname
            d["resource_domain"] = rdomain
            rows.append(d)
        return {"items": rows, "total_count": total_count}


@mcp.tool
def get_vulnerabilities(
    severity: str | None = None, status: str = "open", limit: int = 50, offset: int = 0
) -> dict:
    """Return vulnerability queue items (CVEs mapped to hosts).

    ``status="open"`` means "still actionable" — it matches every
    system-managed open state (``open`` AND ``triage``), per
    ``db/vuln_status.py``. GitLab #136: the old exact ``== "open"`` match
    silently hid every CVE VulnTriageAgent had promoted to ``triage`` — which
    is ALL criticals, so ``severity="critical"`` returned zero rows. Pass an
    explicit status (e.g. ``"triage"``, ``"resolved"``) for an exact match,
    or an empty string for all statuses. ``offset`` pages through result sets
    larger than ``limit`` (rows are stably ordered by sla_due, then id).

    TRK-272 / GitLab #145: the response is a dict of ``{"items": [...],
    "total_count": N}`` — ``items`` holds exactly what this tool used to
    return as a bare list; ``total_count`` is the count of matching rows
    BEFORE ``offset``/``limit`` are applied, via a separate ``.count()``
    query so the full result set is never materialized just to size it.
    """
    with get_session() as s:
        q = s.query(VulnQueueItem, Resource.name.label("host")).join(
            Resource, Resource.id == VulnQueueItem.resource_id
        )
        if severity:
            # vuln_queue stores canonical lowercase bands; normalize so a
            # capitalized param can't silently match zero rows.
            from infra_brain.db.severity import normalize_severity

            q = q.filter(VulnQueueItem.severity == (normalize_severity(severity) or severity))
        if status == "open":
            q = q.filter(VulnQueueItem.status.in_(OPEN_VULN_STATUSES))
        elif status:
            q = q.filter(VulnQueueItem.status == status)
        total_count = q.count()
        rows = []
        page = (
            q.order_by(VulnQueueItem.sla_due.asc().nullslast(), VulnQueueItem.id.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        for v, host in page:
            d = _row_to_dict(v)
            d["host"] = host
            rows.append(d)
        return {"items": rows, "total_count": total_count}


@mcp.tool
def get_eol_status(days_until_eol: int | None = None, limit: int = 50) -> list[dict]:
    """Return EOL registry entries. Pass days_until_eol to filter by proximity.

    GitLab #186: rows with a NULL ``eol_date`` are INCLUDED by the
    ``days_until_eol`` filter, not excluded. A bare ``eol_date <= cutoff``
    silently drops them (SQL NULL never satisfies a comparison), which made
    genuinely overdue products — SLES 11, Windows 10 — invisible to the exact
    call meant to answer "what is urgent right now". A missing date means
    "unknown, must be reviewed", not "not applicable", so it belongs in the
    urgent window rather than outside it.
    """
    with get_session() as s:
        q = s.query(EolRegistry, Resource.name.label("resource_name")).join(
            Resource, Resource.id == EolRegistry.resource_id
        )
        if days_until_eol is not None:
            cutoff = _now_utc() + timedelta(days=days_until_eol)
            q = q.filter(or_(EolRegistry.eol_date <= cutoff, EolRegistry.eol_date.is_(None)))
        rows = []
        for e, rname in q.order_by(EolRegistry.eol_date.asc().nullslast()).limit(limit).all():
            d = _row_to_dict(e)
            d["resource_name"] = rname
            rows.append(d)
        return rows


@mcp.tool
def get_remediation_suggestions(status: str = "pending", limit: int = 50, offset: int = 0) -> dict:
    """Return proposed remediation actions from the RemediationAgent.

    TRK-272 / GitLab #145: ``offset`` pages through result sets larger than
    ``limit`` (rows are stably ordered by ``id`` descending). The response is
    a dict of ``{"items": [...], "total_count": N}`` — ``items`` holds
    exactly what this tool used to return as a bare list; ``total_count`` is
    the count of matching rows BEFORE ``offset``/``limit`` are applied, via a
    separate ``.count()`` query so the full result set is never materialized
    just to size it.
    """
    with get_session() as s:
        q = s.query(ProposedAction)
        if status:
            q = q.filter(ProposedAction.status == status)
        total_count = q.count()
        page = q.order_by(ProposedAction.id.desc()).offset(offset).limit(limit).all()
        return {"items": [_row_to_dict(a) for a in page], "total_count": total_count}


@mcp.tool
def get_inventory_gaps(status: str = "proposed", limit: int = 50, offset: int = 0) -> dict:
    """Return hosts discovered in the environment but missing from the Ansible inventory.

    TRK-272 / GitLab #145: ``offset`` pages through result sets larger than
    ``limit`` (rows are stably ordered by ``detected_at`` descending). The
    response is a dict of ``{"items": [...], "total_count": N}`` — ``items``
    holds exactly what this tool used to return as a bare list; ``total_count``
    is the count of matching rows BEFORE ``offset``/``limit`` are applied, via
    a separate ``.count()`` query so the full result set is never materialized
    just to size it.
    """
    with get_session() as s:
        q = s.query(InventoryReconcileEvent)
        if status:
            q = q.filter(InventoryReconcileEvent.status == status)
        total_count = q.count()
        page = (
            q.order_by(InventoryReconcileEvent.detected_at.desc()).offset(offset).limit(limit).all()
        )
        return {"items": [_row_to_dict(e) for e in page], "total_count": total_count}


@mcp.tool
def get_instincts(
    domain: str | None = None,
    zone: str = "corpor",
    min_confidence: float = 0.7,
    limit: int = 50,
) -> list[dict]:
    """Return learned instincts from the knowledge base."""
    with get_session() as s:
        q = s.query(Instinct).filter(Instinct.zone == zone, Instinct.confidence >= min_confidence)
        if domain:
            q = q.filter(Instinct.domain == domain)
        return [_row_to_dict(i) for i in q.order_by(Instinct.confidence.desc()).limit(limit).all()]


@mcp.tool
def query_nl(question: str) -> dict:
    """Answer natural-language questions about the infrastructure database using SQL.

    Bounded (GitLab #165): the whole reasoning loop runs under a total
    wall-clock ceiling of ``MCP_TOOL_TIMEOUT_SECONDS`` (default 60s), enforced
    inside ``LLMAgent.reason()``. Exceeding it returns an explicit timeout error
    rather than hanging until YOUR transport timeout fires. If no reasoner model
    is configured, this returns a config error immediately without calling the
    model at all. Set MCP_TOOL_TIMEOUT_SECONDS comfortably below your client's
    own request timeout.
    """
    try:
        from infra_brain.agents.query import QueryAgent  # type: ignore[import]

        agent = QueryAgent()
        return agent.query(question)
    except ImportError:
        return {"error": "QueryAgent is not available in this deployment"}
    except Exception as exc:
        logger.warning("query_nl failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@mcp.tool
def get_collection_health(hours: int = 24, limit: int = 100) -> list[dict]:
    """Return recent collection run results — domain, status, resource counts, drift counts."""
    cutoff = _now_utc() - timedelta(hours=hours)
    with get_session() as s:
        runs = (
            s.query(CollectionRun)
            .filter(
                CollectionRun.finished_at.isnot(None),
                CollectionRun.finished_at >= cutoff,
            )
            .order_by(CollectionRun.finished_at.desc())
            .limit(limit)
            .all()
        )
        return [_row_to_dict(r) for r in runs]


@mcp.tool
def get_vsphere_overview() -> dict:
    """Return the full vSphere estate: datacenters, clusters, hosts, VMs,
    datastores, plus a rollup summary (counts + capacity) — the same data
    backing the dashboard's /api/vsphere/overview Virtualization view.
    Renders an empty state cleanly when the vSphere connector is paused
    (no host configured, every table empty).
    """
    from infra_brain.api.routers.vsphere import vsphere_overview

    return vsphere_overview().model_dump(mode="json")


@mcp.tool
def get_vsphere_vms(
    esxi_host: str | None = None,
    power_state: str | None = None,
    is_template: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return vSphere virtual machines, optionally filtered by ESXi host,
    power state, or template flag."""
    with get_session() as s:
        q = s.query(VsphereVm)
        if esxi_host:
            q = q.filter(VsphereVm.esxi_host == esxi_host)
        if power_state:
            q = q.filter(VsphereVm.power_state == power_state)
        if is_template is not None:
            q = q.filter(VsphereVm.is_template.is_(is_template))
        return [_row_to_dict(v) for v in q.order_by(VsphereVm.name).limit(limit).all()]


@mcp.tool
def get_vsphere_hosts(
    cluster_name: str | None = None,
    connection_state: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return ESXi hosts, optionally filtered by cluster name or connection state."""
    with get_session() as s:
        q = s.query(VsphereHost)
        if cluster_name:
            q = q.filter(VsphereHost.cluster_name == cluster_name)
        if connection_state:
            q = q.filter(VsphereHost.connection_state == connection_state)
        return [_row_to_dict(h) for h in q.order_by(VsphereHost.name).limit(limit).all()]


@mcp.tool
def get_vsphere_datastores(
    datastore_type: str | None = None,
    accessible: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return vSphere datastores, optionally filtered by type (VMFS/NFS/etc.)
    or accessibility."""
    with get_session() as s:
        q = s.query(VsphereDatastore)
        if datastore_type:
            q = q.filter(VsphereDatastore.datastore_type == datastore_type)
        if accessible is not None:
            q = q.filter(VsphereDatastore.accessible.is_(accessible))
        return [_row_to_dict(d) for d in q.order_by(VsphereDatastore.name).limit(limit).all()]


@mcp.tool
def get_vsphere_snapshots(
    vm_name: str | None = None,
    min_age_days: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return VM snapshots, optionally filtered by VM name or minimum age in
    days (use min_age_days to surface old-snapshot hygiene findings)."""
    with get_session() as s:
        q = s.query(VsphereSnapshot)
        if vm_name:
            q = q.filter(VsphereSnapshot.vm_name == vm_name)
        if min_age_days is not None:
            q = q.filter(VsphereSnapshot.age_days >= min_age_days)
        return [
            _row_to_dict(sn)
            for sn in q.order_by(VsphereSnapshot.age_days.desc().nullslast()).limit(limit).all()
        ]


@mcp.tool
def get_vsphere_clusters(
    datacenter_name: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return vSphere clusters, optionally filtered by datacenter name."""
    with get_session() as s:
        q = s.query(VsphereCluster)
        if datacenter_name:
            q = q.filter(VsphereCluster.datacenter_name == datacenter_name)
        return [_row_to_dict(c) for c in q.order_by(VsphereCluster.name).limit(limit).all()]


@mcp.tool
def get_vsphere_alarms(
    vcenter: str | None = None,
    acknowledged: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return currently-triggered vCenter alarms, optionally filtered by
    vCenter or acknowledged state."""
    with get_session() as s:
        q = s.query(VsphereAlarm)
        if vcenter:
            q = q.filter(VsphereAlarm.vcenter == vcenter)
        if acknowledged is not None:
            q = q.filter(VsphereAlarm.acknowledged.is_(acknowledged))
        return [
            _row_to_dict(a)
            for a in q.order_by(VsphereAlarm.triggered_at.desc().nullslast()).limit(limit).all()
        ]


@mcp.tool
def get_vsphere_permissions(
    vcenter: str | None = None,
    principal: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return vCenter permissions (principal to role-on-entity assignments),
    optionally filtered by vCenter or principal."""
    with get_session() as s:
        q = s.query(VspherePermission)
        if vcenter:
            q = q.filter(VspherePermission.vcenter == vcenter)
        if principal:
            q = q.filter(VspherePermission.principal == principal)
        return [_row_to_dict(p) for p in q.order_by(VspherePermission.principal).limit(limit).all()]


@mcp.tool
def search_knowledge(query: str, k: int = 5, include_personal: bool = False) -> list[dict] | dict:
    """Semantic search over the internal knowledge base (RAG).

    Covers institutional documentation — runbooks, decision records, architecture
    docs, and other indexed sources. Returns the top-k most relevant chunks with
    provenance (title, space, url) and a numbered ``similarity`` score to cite.
    An empty list means the knowledge base has nothing on that topic (or RAG is
    disabled). Read-only.

    By default this searches infra-scoped content ONLY. A separate personal-wiki
    knowledge domain (personal notes/journal content, not infrastructure
    documentation) is excluded from results unless you pass
    ``include_personal=True`` — only do so when the caller has explicitly asked
    for personal-domain context; never default to it or infer it from a query
    that merely sounds personal.
    """
    try:
        from infra_brain.embeddings import search_knowledge as _search

        return _search(query, k=k, include_personal=include_personal)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("search_knowledge failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


# ── Batch H: GitLab / IaC / CI-CD query tools ─────────────────────────────────
# GitLab issue #52, spec docs/superpowers/specs/2026-07-23-mcp-auth-and-tool-
# expansion-design.md. Covers db/models/ansible.py (GitlabProject, IacFile,
# CiPipelineRun, CiSchedule, TerraformResource, ComposeService,
# K8sManifestResource, AnsibleInventoryGroup, AnsibleInventoryHost). All
# read-only; no mutation path.


@mcp.tool
def get_cicd_overview() -> dict:
    """Return a one-shot CI/CD aggregate: project count, pipeline status mix, IaC file mix. Read-only."""
    with get_session() as s:
        gitlab_projects = s.query(GitlabProject).count()
        pipeline_rows = (
            s.query(CiPipelineRun.status, func.count().label("n"))
            .group_by(CiPipelineRun.status)
            .all()
        )
        iac_rows = (
            s.query(IacFile.file_type, func.count().label("n")).group_by(IacFile.file_type).all()
        )
        return {
            "gitlab_projects": gitlab_projects,
            "pipelines_by_status": {status: n for status, n in pipeline_rows if status is not None},
            "iac_files_by_type": {ftype: n for ftype, n in iac_rows if ftype is not None},
        }


@mcp.tool
def get_iac_files(
    file_type: str | None = None,
    project_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return parsed infrastructure-as-code files (compose/k8s/terraform/playbook/inventory/ci). Read-only."""
    with get_session() as s:
        q = s.query(IacFile)
        if file_type:
            q = q.filter(IacFile.file_type == file_type)
        if project_id is not None:
            q = q.filter(IacFile.gitlab_project_id == project_id)
        return [_row_to_dict(f) for f in q.order_by(IacFile.path).limit(limit).all()]


@mcp.tool
def get_ci_schedules(
    project_id: int | None = None,
    active: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return GitLab CI pipeline schedules with the owning IaC file's resource name. Read-only."""
    with get_session() as s:
        q = s.query(CiSchedule, Resource.name.label("resource_name")).join(
            Resource, Resource.id == CiSchedule.resource_id
        )
        if project_id is not None:
            q = q.filter(CiSchedule.project_id == project_id)
        if active is not None:
            q = q.filter(CiSchedule.active.is_(active))
        rows = []
        for sched, rname in (
            q.order_by(CiSchedule.project_id, CiSchedule.schedule_id).limit(limit).all()
        ):
            d = _row_to_dict(sched)
            d["resource_name"] = rname
            rows.append(d)
        return rows


@mcp.tool
def get_parsed_iac_resources(
    kind: str = "terraform",
    project_id: int | None = None,
    limit: int = 50,
) -> list[dict] | dict:
    """Return parsed IaC child resources by kind: 'terraform' | 'compose' | 'k8s_manifest'. Read-only."""
    models = {
        "terraform": TerraformResource,
        "compose": ComposeService,
        "k8s_manifest": K8sManifestResource,
    }
    model = models.get(kind)
    if model is None:
        return {"error": f"kind must be one of {sorted(models)}; got {kind!r}"}
    with get_session() as s:
        q = s.query(model)
        if project_id is not None:
            # child rows link to IacFile, which carries gitlab_project_id.
            q = q.join(IacFile, IacFile.id == model.iac_file_id).filter(
                IacFile.gitlab_project_id == project_id
            )
        return [_row_to_dict(r) for r in q.order_by(model.id).limit(limit).all()]


@mcp.tool
def get_ansible_inventory(group: str | None = None, limit: int = 50) -> list[dict]:
    """Return Ansible inventory groups (from parsed inventory files) with their host names. Read-only."""
    with get_session() as s:
        gq = s.query(AnsibleInventoryGroup)
        if group:
            gq = gq.filter(AnsibleInventoryGroup.name == group)
        groups = gq.order_by(AnsibleInventoryGroup.name).limit(limit).all()
        out: list[dict] = []
        for g in groups:
            hosts = (
                s.query(AnsibleInventoryHost.name)
                .filter(AnsibleInventoryHost.group_id == g.id)
                .order_by(AnsibleInventoryHost.name)
                .all()
            )
            host_names = [h.name for h in hosts]
            out.append(
                {
                    "group": g.name,
                    "iac_file_id": str(g.iac_file_id),
                    "hosts": host_names,
                    "host_count": len(host_names),
                }
            )
        return out


# ── Batch K: knowledge / learning query tools ────────────────────────────────


@mcp.tool
def get_documents(
    space: str | None = None,
    status: str | None = None,
    sensitivity: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return indexed-document metadata/freshness (title, source, space, status). Read-only.

    Document-level rows only — distinct from search_knowledge's semantic RAG results.
    """
    with get_session() as s:
        q = s.query(Document)
        if space:
            q = q.filter(Document.space == space)
        if status:
            q = q.filter(Document.status == status)
        if sensitivity:
            q = q.filter(Document.sensitivity == sensitivity)
        return [_row_to_dict(d) for d in q.order_by(Document.indexed_at.desc()).limit(limit).all()]


@mcp.tool
def get_observations(
    domain: str | None = None,
    agent: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return unpromoted learned patterns (trending toward instincts), highest count first. Read-only."""
    with get_session() as s:
        q = s.query(Observation)
        if domain:
            q = q.filter(Observation.domain == domain)
        if agent:
            q = q.filter(Observation.agent == agent)
        return [_row_to_dict(o) for o in q.order_by(Observation.count.desc()).limit(limit).all()]


# ── Batch A: cross-domain tools ──────────────────────────────────────────────
# Mirror api/routers/hosts.py (get_host, get_host_vulns, list_host_purpose_map)
# and api/routers/fleet.py (get_counts) query logic — see spec Batch A
# (docs/superpowers/specs/2026-07-23-mcp-auth-and-tool-expansion-design.md).


def _host_identity_dict(row: HostIdentity) -> dict:
    """Same shape as api/routers/hosts.py's _host_identity_dict — kept as a
    separate copy here (not a cross-module import) so the MCP tool surface has
    no dependency on the FastAPI router layer, matching every other tool in
    this file."""
    return {
        "id": str(row.id),
        "short_hostname": row.short_hostname,
        "fqdn": row.fqdn,
        "ip_addresses": row.ip_addresses or [],
        "r7_resource_id": str(row.r7_resource_id) if row.r7_resource_id else None,
        "vsphere_resource_id": str(row.vsphere_resource_id) if row.vsphere_resource_id else None,
        "octopus_resource_id": str(row.octopus_resource_id) if row.octopus_resource_id else None,
        "linux_resource_id": str(row.linux_resource_id) if row.linux_resource_id else None,
        "windows_resource_id": str(row.windows_resource_id) if row.windows_resource_id else None,
        "os_family": row.os_family,
        "risk_score": row.risk_score,
        "vuln_count": row.vuln_count,
        "patch_status": row.patch_status,
        "vsphere_power_state": row.vsphere_power_state,
        "octopus_machine_status": row.octopus_machine_status,
        "last_reconciled": row.last_reconciled.isoformat() if row.last_reconciled else None,
    }


@mcp.tool
def get_host_profile(hostname: str) -> dict:
    """Cross-domain identity for one host — the host_identities join across
    Rapid7 + vSphere + Octopus + Linux + Windows (HostReconcileAgent's
    canonical merge). Returns an {"error": ...} dict if the hostname is
    unknown rather than raising, matching query_nl's convention.
    """
    with get_session() as s:
        row = s.query(HostIdentity).filter_by(short_hostname=normalize_host(hostname)).first()
        if row is None:
            return {"error": f"Host '{hostname}' not found"}
        return _host_identity_dict(row)


@mcp.tool
def get_host_vulns(
    hostname: str,
    severity: str | None = None,
    status: str = "open",
    sla_overdue: bool = False,
    limit: int = 100,
) -> dict:
    """Per-host CVE walk with remediation summary. Mirrors
    GET /api/dashboard/hosts/{hostname}/vulns: a risk-band header (from
    r7_assets, falling back to the denormalized host_identities risk score)
    plus a filterable list of the host's CVEs enriched with CVSS,
    exploit/PCI flags, SLA status, and a remediation summary (first solution
    found via any r7_vuln slug). Returns {"error": ...} if the hostname is
    unknown.

    ``status="open"`` (the default) means "still actionable" — it matches
    every system-managed open state (``open`` AND ``triage``), per
    ``db/vuln_status.py`` and the same GitLab #136 union ``get_vulnerabilities``
    uses (GitLab #188 Bug 1: this tool's default previously did an exact
    ``== "open"`` match, so a host with criticals sitting in ``triage``
    reported zero vulnerabilities by default). Pass an explicit status
    (e.g. ``"triage"``, ``"resolved"``) for an exact match, or an empty
    string for all statuses.

    ``header["vuln_queue_coverage_gap"]`` (GitLab #188 Bug 2): True when the
    uncapped Rapid7 ``r7_assets`` rollup reports more critical/severe/
    moderate CVEs than exist in ``vuln_queue`` for this host across ALL
    statuses. ``vuln_queue`` is fed by a per-run scan capped to the top-N
    assets by risk score (``RAPID7_VULN_ASSET_CAP``, default 750) — a host
    outside that cap on a given run gets no refresh, so this tool's
    ``items``/``total`` can silently under-represent the host's real
    exposure. This is a capacity limit, not fixed by this tool; when the
    flag is set, ``header["coverage_note"]`` explains it — treat the item
    list as possibly incomplete rather than authoritative for that host.
    """
    from infra_brain.api._helpers import _now, _sla_string

    limit = max(1, min(limit, 500))
    hostname_lower = normalize_host(hostname)

    with get_session() as s:
        host_id = (
            s.query(HostIdentity).filter(HostIdentity.short_hostname == hostname_lower).first()
        )
        if not host_id:
            return {"error": f"Host '{hostname}' not found"}

        resource_id = host_id.r7_resource_id

        r7_asset = (
            s.query(R7Asset).filter(R7Asset.resource_id == resource_id).first()
            if resource_id
            else None
        )
        header = {
            "hostname": hostname,
            "risk_score": float(r7_asset.risk_score or 0)
            if r7_asset
            else float(host_id.risk_score or 0),
            "vuln_critical": int(r7_asset.vuln_critical or 0) if r7_asset else 0,
            "vuln_severe": int(r7_asset.vuln_severe or 0) if r7_asset else 0,
            "vuln_moderate": int(r7_asset.vuln_moderate or 0) if r7_asset else 0,
        }

        if not resource_id:
            return {"header": header, "items": [], "total": 0}

        # GitLab #188 Bug 2 (structural, not fully fixable here — see #173):
        # r7_assets.vuln_critical/severe/moderate is Rapid7's own rollup for
        # this asset, uncapped. vuln_queue is fed by VulnAgent's per-run scan,
        # which is capped to the top RAPID7_VULN_ASSET_CAP (default 750)
        # assets by risk score (agents/vuln.py::_bounded_assets) — a host that
        # falls outside that cap on a given run gets no vuln_queue
        # inserts/refresh that run, so the two sources can silently disagree
        # on coverage for that host. This is a capacity limit, not something
        # fixable by this tool alone; the mitigation here is to make the
        # disagreement VISIBLE (an unfiltered count independent of the
        # caller's status/severity filters) rather than silently returning a
        # partial/stale item list with no signal that it may be incomplete.
        vq_all_count = (
            s.query(VulnQueueItem).filter(VulnQueueItem.resource_id == resource_id).count()
        )
        header_vuln_total = (
            header["vuln_critical"] + header["vuln_severe"] + header["vuln_moderate"]
        )
        header["vuln_queue_coverage_gap"] = (
            header_vuln_total > 0 and vq_all_count < header_vuln_total
        )
        if header["vuln_queue_coverage_gap"]:
            header["coverage_note"] = (
                f"r7_assets reports {header_vuln_total} critical/severe/moderate CVE(s) "
                f"for this host, but only {vq_all_count} row(s) exist in vuln_queue across "
                "ALL statuses. vuln_queue ingest is capped to the top-N assets by risk "
                "score per run (RAPID7_VULN_ASSET_CAP, default 750) — this host's rows may "
                "be missing or stale because it fell outside that cap on a recent scan. "
                "Treat 'items'/'total' below as possibly incomplete for this host."
            )

        vq = s.query(VulnQueueItem).filter(VulnQueueItem.resource_id == resource_id)
        # GitLab #136 / #188 Bug 1: "open" must mean "still actionable" — union
        # every system-managed open state (OPEN_VULN_STATUSES = open + triage),
        # matching get_vulnerabilities. The old exact ``== "open"`` match
        # silently hid every CVE VulnTriageAgent had promoted to "triage" —
        # which is where promoted criticals land, so a host with criticals
        # sitting in triage reported zero vulnerabilities under the default
        # call. Pass an explicit status (e.g. "triage", "resolved") for an
        # exact match, or an empty string for all statuses.
        if status == "open":
            vq = vq.filter(VulnQueueItem.status.in_(OPEN_VULN_STATUSES))
        elif status:
            vq = vq.filter(VulnQueueItem.status == status)
        if severity:
            vq = vq.filter(VulnQueueItem.severity.ilike(f"%{severity}%"))
        if sla_overdue:
            vq = vq.filter(VulnQueueItem.sla_due.isnot(None)).filter(VulnQueueItem.sla_due < _now())

        total = vq.count()
        vuln_rows = vq.order_by(VulnQueueItem.sla_due.asc().nullslast()).limit(limit).all()

        cve_ids = [v.cve_id for v in vuln_rows]
        best: dict[str, R7Vulnerability] = {}
        slug_map: dict[str, list[str]] = {}
        solutions_map: dict[str, str] = {}

        if cve_ids:
            bridge = s.query(R7VulnCve).filter(R7VulnCve.cve_id.in_(cve_ids)).all()
            for br in bridge:
                slug_map.setdefault(br.cve_id, []).append(br.r7_vuln_id)
            all_slugs = list({sl for ss in slug_map.values() for sl in ss})
            if all_slugs:
                rv_rows = (
                    s.query(R7Vulnerability).filter(R7Vulnerability.r7_vuln_id.in_(all_slugs)).all()
                )
                rv_by_slug = {v.r7_vuln_id: v for v in rv_rows}
                for cid, slugs in slug_map.items():
                    candidates = [rv_by_slug[sl] for sl in slugs if sl in rv_by_slug]
                    if candidates:
                        best[cid] = max(candidates, key=lambda v: v.cvss_v3_score or 0)

                sol_links = (
                    s.query(R7VulnSolution).filter(R7VulnSolution.r7_vuln_id.in_(all_slugs)).all()
                )
                sol_ids = list({sl.r7_solution_id for sl in sol_links})
                if sol_ids:
                    sol_rows = (
                        s.query(R7Solution).filter(R7Solution.r7_solution_id.in_(sol_ids)).all()
                    )
                    sol_by_id = {sol.r7_solution_id: sol for sol in sol_rows}
                    link_by_slug = {sl.r7_vuln_id: sl.r7_solution_id for sl in sol_links}
                    for cid, slugs in slug_map.items():
                        for slug in slugs:
                            sol_id = link_by_slug.get(slug)
                            if sol_id and sol_id in sol_by_id:
                                solutions_map[cid] = sol_by_id[sol_id].summary or ""
                                break

        items: list[dict] = []
        for v in vuln_rows:
            rv = best.get(v.cve_id)
            slug = (slug_map.get(v.cve_id) or [""])[0]
            items.append(
                {
                    "cve_id": v.cve_id,
                    "kb_id": v.kb_id or "",
                    "severity": v.severity or "",
                    "cvss_v3": float(rv.cvss_v3_score or 0) if rv else 0.0,
                    "title": (rv.title or "") if rv else "",
                    "exploits": int(rv.exploits or 0) if rv else 0,
                    "fix_available": bool(rv.fix_available) if rv else False,
                    "pci_fail": bool(rv.pci_fail) if rv else False,
                    "sla": _sla_string(v.sla_due),
                    "sla_due": v.sla_due.isoformat() if v.sla_due else None,
                    "status": v.status or "open",
                    "last_updated": v.last_updated.isoformat() if v.last_updated else None,
                    "r7_vuln_id": (rv.r7_vuln_id if rv else slug) or "",
                    "solution_summary": solutions_map.get(v.cve_id, ""),
                }
            )

        return {"header": header, "items": items, "total": total}


@mcp.tool
def get_host_context(
    hostname: str,
    top_cves_limit: int = 10,
    drift_limit: int = 50,
    compliance_limit: int = 20,
) -> dict:
    """One-shot cross-domain context for a single host (GitLab #125).

    Replaces the ~9 separate MCP calls + manual post-processing that
    assembling "everything around one host" used to cost. Server-side join
    over host_identities, vSphere, Rapid7 vuln, drift, and compliance tables
    — no new tables, no writes. Returns:

    - ``identity``: the host_identities row (same shape as get_host_profile).
    - ``vsphere_placement``: esxi_host, datastores, and co_resident_vm_count
      (other VMs sharing the same esxi_host) — empty/zeroed defaults when the
      host has no vsphere_resource_id or no matching VsphereVm row.
    - ``top_cves``: this host's open CVEs, worst-CVSS-first, capped at
      ``top_cves_limit`` (mirrors get_host_vulns' vuln_queue -> r7_vuln_cves
      -> r7_vulnerabilities walk, TRK-180 limit-param convention).
    - ``non_telemetry_drift``: open drift events across every domain resource
      tied to this host, with graph_maintenance's own self-telemetry excluded
      by construction (TRK-191 — same exclusion get_drift_events applies via
      its ``include_graph_maintenance`` param, reused here rather than
      reinvented), capped at ``drift_limit``.
    - ``compliance_status``: open ComplianceViolation rows matching this host
      by resource_id (when linked) or by host-name string, capped at
      ``compliance_limit``, plus an ``open_violation_count`` total unaffected
      by the cap.

    Hostname is normalized the same way as get_host_profile/get_host_vulns
    (TRK-189's normalize_host()) so a bare short hostname or a full FQDN both
    resolve to the same host. Returns {"error": ...} if the hostname is
    unknown — never raises. A domain with no data returns its natural empty
    container ({}/[]/0), never omits the key.
    """
    hostname_norm = normalize_host(hostname)

    with get_session() as s:
        host_id = s.query(HostIdentity).filter(HostIdentity.short_hostname == hostname_norm).first()
        if host_id is None:
            return {"error": f"Host '{hostname}' not found"}

        identity = _host_identity_dict(host_id)

        # ── vsphere_placement ────────────────────────────────────────────
        vsphere_placement: dict = {
            "esxi_host": None,
            "datastores": [],
            "co_resident_vm_count": 0,
        }
        vm = (
            s.query(VsphereVm).filter(VsphereVm.resource_id == host_id.vsphere_resource_id).first()
            if host_id.vsphere_resource_id
            else None
        )
        if vm is not None:
            co_resident_vm_count = 0
            if vm.esxi_host:
                co_resident_vm_count = (
                    s.query(VsphereVm)
                    .filter(VsphereVm.esxi_host == vm.esxi_host, VsphereVm.id != vm.id)
                    .count()
                )
            vsphere_placement = {
                "esxi_host": vm.esxi_host,
                "datastores": list(vm.datastore_names or []),
                "co_resident_vm_count": co_resident_vm_count,
            }

        # ── top_cves ─────────────────────────────────────────────────────
        # Mirrors get_host_vulns' vuln_queue -> r7_vuln_cves -> r7_vulnerabilities
        # walk, then re-ranked worst-CVSS-first and capped at top_cves_limit.
        top_cves: list[dict] = []
        resource_id = host_id.r7_resource_id
        if resource_id:
            vuln_rows = (
                s.query(VulnQueueItem)
                .filter(
                    VulnQueueItem.resource_id == resource_id,
                    VulnQueueItem.status.in_(OPEN_VULN_STATUSES),
                )
                .all()
            )
            cve_ids = [v.cve_id for v in vuln_rows]
            best: dict[str, R7Vulnerability] = {}
            if cve_ids:
                bridge = s.query(R7VulnCve).filter(R7VulnCve.cve_id.in_(cve_ids)).all()
                slug_map: dict[str, list[str]] = {}
                for br in bridge:
                    slug_map.setdefault(br.cve_id, []).append(br.r7_vuln_id)
                all_slugs = list({sl for ss in slug_map.values() for sl in ss})
                if all_slugs:
                    rv_rows = (
                        s.query(R7Vulnerability)
                        .filter(R7Vulnerability.r7_vuln_id.in_(all_slugs))
                        .all()
                    )
                    rv_by_slug = {v.r7_vuln_id: v for v in rv_rows}
                    for cid, slugs in slug_map.items():
                        candidates = [rv_by_slug[sl] for sl in slugs if sl in rv_by_slug]
                        if candidates:
                            best[cid] = max(candidates, key=lambda v: v.cvss_v3_score or 0)
            for v in vuln_rows:
                rv = best.get(v.cve_id)
                top_cves.append(
                    {
                        "cve_id": v.cve_id,
                        "severity": v.severity or "",
                        "cvss_v3": float(rv.cvss_v3_score or 0) if rv else 0.0,
                        "fix_available": bool(rv.fix_available) if rv else False,
                        "sla_due": v.sla_due.isoformat() if v.sla_due else None,
                        "status": v.status or "open",
                    }
                )
            top_cves.sort(key=lambda d: d["cvss_v3"], reverse=True)
            top_cves = top_cves[: max(1, top_cves_limit)]

        # ── non_telemetry_drift ──────────────────────────────────────────
        # Reuses get_drift_events's TRK-191 exclusion: gather every domain
        # resource_id this host identity links to and exclude graph_maintenance
        # by construction (not reinvented — same domain != "graph_maintenance"
        # rule get_drift_events(include_graph_maintenance=False) applies).
        host_resource_ids = [
            rid
            for rid in (
                host_id.r7_resource_id,
                host_id.vsphere_resource_id,
                host_id.octopus_resource_id,
                host_id.linux_resource_id,
                host_id.windows_resource_id,
                getattr(host_id, "net_resource_id", None),
                getattr(host_id, "cloud_resource_id", None),
                getattr(host_id, "k8s_resource_id", None),
            )
            if rid
        ]
        non_telemetry_drift: list[dict] = []
        if host_resource_ids:
            drift_rows = (
                s.query(DriftEvent, Resource.domain.label("resource_domain"))
                .join(Resource, Resource.id == DriftEvent.resource_id)
                .filter(DriftEvent.resource_id.in_(host_resource_ids))
                .filter(DriftEvent.status == "open")
                .filter(Resource.domain != "graph_maintenance")
                # GitLab #163/#164: see drift_recency().
                .order_by(drift_recency().desc())
                .limit(max(1, drift_limit))
                .all()
            )
            for de, rdomain in drift_rows:
                d = _row_to_dict(de)
                d["resource_domain"] = rdomain
                non_telemetry_drift.append(d)

        # ── compliance_status ────────────────────────────────────────────
        # ComplianceViolation.host is a free-text string (matched against
        # Resource.name at write time, see agents/compliance.py's
        # reconcile_compliance_resource_links) — not guaranteed to already be
        # normalize_host()'d the way host_identities is. Match on resource_id
        # (when the write-time reconcile linked one) OR a case-insensitive
        # match against this host's known name spellings, so a lookup by
        # short hostname still finds violations recorded against the FQDN.
        host_names = {n for n in (host_id.short_hostname, host_id.fqdn) if n}
        compliance_filters = []
        if host_resource_ids:
            compliance_filters.append(ComplianceViolation.resource_id.in_(host_resource_ids))
        if host_names:
            compliance_filters.append(
                func.lower(ComplianceViolation.host).in_([n.lower() for n in host_names])
            )
        compliance_status: dict = {"open_violation_count": 0, "violations": []}
        if compliance_filters:
            base_q = s.query(ComplianceViolation).filter(
                ComplianceViolation.status == "open", or_(*compliance_filters)
            )
            compliance_status["open_violation_count"] = base_q.count()
            compliance_status["violations"] = [
                _row_to_dict(v)
                for v in base_q.order_by(ComplianceViolation.detected_at.desc())
                .limit(max(1, compliance_limit))
                .all()
            ]

        return {
            "identity": identity,
            "vsphere_placement": vsphere_placement,
            "top_cves": top_cves,
            "non_telemetry_drift": non_telemetry_drift,
            "compliance_status": compliance_status,
        }


@mcp.tool
def get_host_purpose_map() -> list[dict]:
    """Return the full curated hostname→purpose/VLAN/subnet mapping. Mirrors
    GET /api/dashboard/host-purpose-map — no other access path exists for
    this data (populated by InventoryReconcileAgent's Ansible-inventory sync).
    """
    with get_session() as s:
        rows = s.query(HostPurposeMap).order_by(HostPurposeMap.hostname.asc()).all()
        return [
            {
                "hostname": r.hostname,
                "purpose": r.purpose,
                "vlan": r.vlan,
                "subnet": r.subnet,
                "source": r.source,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


@mcp.tool
def get_fleet_counts() -> dict:
    """One-shot aggregate counts for the fleet: open drift events, distinct
    open CVEs, overdue EOL assets, and pending inventory-reconcile (Ansible
    integration) proposals. Mirrors GET /api/dashboard/counts — server-side
    COUNT queries, never affected by any list endpoint's .limit(N) cap.
    """
    with get_session() as s:
        # TRK-191: join Resource so graph_maintenance's own internal
        # "graph-health" report resource (never real fleet infrastructure) is
        # excluded from the fleet-wide open-drift count. Defense in depth —
        # the primary fix stops graph_maintenance from ever writing a Snapshot
        # for that resource at all (see agents/graph_maintenance.py collect()),
        # but this join also protects against any other non-fleet domain that
        # might someday route drift the same way.
        open_drift = (
            s.query(DriftEvent)
            .join(Resource, Resource.id == DriftEvent.resource_id)
            .filter(DriftEvent.status == "open")
            .filter(Resource.domain != "graph_maintenance")
            .count()
        )
        open_cves = (
            s.query(func.count(func.distinct(VulnQueueItem.cve_id)))
            .filter(VulnQueueItem.status.in_(OPEN_VULN_STATUSES))
            .scalar()
        ) or 0
        eol_overdue = (
            s.query(EolRegistry)
            .filter(EolRegistry.eol_date.isnot(None))
            .filter(EolRegistry.eol_date < _now_utc())
            .count()
        )
        invrec_proposed = (
            s.query(InventoryReconcileEvent)
            .filter(InventoryReconcileEvent.status == "proposed")
            .count()
        )
        return {
            "open_drift": open_drift,
            "open_cves": open_cves,
            "eol_overdue": eol_overdue,
            "invrec_proposed": invrec_proposed,
        }


def _vuln_for_cve(s: Any, cve_id: str) -> R7Vulnerability | None:
    """Resolve the richest R7Vulnerability backing a canonical CVE id via the
    r7_vuln_cves bridge (pick the highest CVSS when several slugs map to it).
    Mirrors api/routers/cve.py::_vuln_for_cve."""
    slugs = [r[0] for r in s.query(R7VulnCve.r7_vuln_id).filter(R7VulnCve.cve_id == cve_id).all()]
    if not slugs:
        return None
    return (
        s.query(R7Vulnerability)
        .filter(R7Vulnerability.r7_vuln_id.in_(slugs))
        .order_by(R7Vulnerability.cvss_v3_score.desc().nullslast())
        .first()
    )


@mcp.tool
def get_cve_detail(cve_id: str) -> dict:
    """Return full detail for one canonical CVE id: severity/CVSS, affected
    hosts (from vuln_queue), and remediation solutions.

    Mirrors GET /api/dashboard/cves/{cve_id}. Returns {"error": ...} when the
    CVE id is not present in the Rapid7 bridge (r7_vuln_cves) — never raises.
    """
    with get_session() as s:
        slugs = [
            r[0] for r in s.query(R7VulnCve.r7_vuln_id).filter(R7VulnCve.cve_id == cve_id).all()
        ]
        if not slugs:
            return {"error": f"CVE {cve_id} not found"}
        vuln = _vuln_for_cve(s, cve_id)

        host_rows = (
            s.query(VulnQueueItem, Resource)
            .outerjoin(Resource, Resource.id == VulnQueueItem.resource_id)
            .filter(VulnQueueItem.cve_id == cve_id)
            .all()
        )
        affected_hosts = [
            {
                "hostname": r.name if r else "—",
                "resource_id": str(v.resource_id) if v.resource_id else "",
                "status": v.status or "",
                "sla_due": v.sla_due.isoformat() if v.sla_due else None,
                "kb_id": v.kb_id or "",
                "last_updated": v.last_updated.isoformat() if v.last_updated else None,
            }
            for v, r in host_rows
        ]

        vuln_sol_rows = (
            s.query(R7VulnSolution).filter(R7VulnSolution.r7_vuln_id.in_(slugs)).all()
            if slugs
            else []
        )
        sol_ids = list({vs.r7_solution_id for vs in vuln_sol_rows})
        sol_rows = (
            s.query(R7Solution).filter(R7Solution.r7_solution_id.in_(sol_ids)).all()
            if sol_ids
            else []
        )
        solutions = [
            {
                "summary": sol.summary or "",
                "steps": sol.steps or "",
                "solution_type": sol.solution_type or "",
                "estimate": sol.estimate or "",
            }
            for sol in sol_rows
        ]

        sla_dues = [h.sla_due for h in (v for v, _ in host_rows) if h.sla_due]
        now = _now_utc()

        def _aware(dt):
            if dt is None:
                return None
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

        aware_dues = [_aware(d) for d in sla_dues]
        sla_deadline = min(aware_dues, default=None)
        sla_overdue_count = sum(1 for d in aware_dues if d < now)

        return {
            "cve_id": cve_id,
            "severity": (vuln.severity if vuln else "") or "",
            "cvss": float((vuln.cvss_v3_score if vuln else 0.0) or 0.0),
            "cvss_vector": (vuln.cvss_v3_vector if vuln else "") or "",
            "cvss_v2": float((vuln.cvss_v2_score if vuln else 0.0) or 0.0),
            "risk_score": float((vuln.risk_score if vuln else 0.0) or 0.0),
            "title": (vuln.title if vuln else "") or "",
            "exploits": vuln.exploits if vuln else 0,
            "malware_kits": vuln.malware_kits if vuln else 0,
            "fix_available": bool(vuln.fix_available) if vuln else False,
            "pci_status": (vuln.pci_status if vuln else "") or "",
            "pci_fail": bool(vuln.pci_fail) if vuln else False,
            "published": vuln.published.isoformat() if vuln and vuln.published else None,
            "denial_of_service": bool(vuln.denial_of_service) if vuln else False,
            "categories": list(vuln.categories or []) if vuln else [],
            "r7_vuln_ids": slugs,
            "affected_hosts": affected_hosts,
            "affected_host_count": len(affected_hosts),
            "solutions": solutions,
            "sla_deadline": sla_deadline.isoformat() if sla_deadline else None,
            "sla_overdue_count": sla_overdue_count,
        }


@mcp.tool
def get_remediation_solutions(cve_id: str) -> list[dict] | dict:
    """Return remediation solutions (summary/steps/type/estimate) for a CVE.

    Walks r7_vuln_cves -> r7_vuln_solutions -> r7_solutions, the same bridge
    used by get_cve_detail's "solutions" field. Returns {"error": ...} when the
    CVE id is not present in the Rapid7 bridge — never raises.
    """
    with get_session() as s:
        slugs = [
            r[0] for r in s.query(R7VulnCve.r7_vuln_id).filter(R7VulnCve.cve_id == cve_id).all()
        ]
        if not slugs:
            return {"error": f"CVE {cve_id} not found"}
        vuln_sol_rows = s.query(R7VulnSolution).filter(R7VulnSolution.r7_vuln_id.in_(slugs)).all()
        sol_ids = list({vs.r7_solution_id for vs in vuln_sol_rows})
        if not sol_ids:
            return []
        sol_rows = s.query(R7Solution).filter(R7Solution.r7_solution_id.in_(sol_ids)).all()
        return [
            {
                "r7_solution_id": sol.r7_solution_id,
                "summary": sol.summary or "",
                "steps": sol.steps or "",
                "solution_type": sol.solution_type or "",
                "estimate": sol.estimate or "",
                "fix_available": bool(sol.fix_available) if sol.fix_available is not None else None,
            }
            for sol in sol_rows
        ]


@mcp.tool
def get_software_inventory(product: str | None = None, limit: int = 50) -> list[dict]:
    """Return the Rapid7 installed-software inventory, aggregated by
    (product, version) with a distinct-host count. Mirrors the ``view=aggregated``
    shape of GET /api/dashboard/software (r7_software has ~335k rows — this
    never bulk-loads the table). Pass ``product`` to filter (case-insensitive
    substring match); ordered by host count desc.
    """
    from sqlalchemy import func as _func

    with get_session() as s:
        q = s.query(
            R7Software.product,
            R7Software.version,
            _func.max(R7Software.vendor).label("vendor"),
            _func.count(_func.distinct(R7Software.r7_asset_id)).label("host_count"),
        )
        if product:
            q = q.filter(R7Software.product.ilike(f"%{product}%"))
        q = q.group_by(R7Software.product, R7Software.version)
        q = q.order_by(_func.count(_func.distinct(R7Software.r7_asset_id)).desc())
        return [
            {
                "product": row.product,
                "version": row.version or "",
                "vendor": row.vendor or "",
                "host_count": int(row.host_count),
            }
            for row in q.limit(limit).all()
        ]


@mcp.tool
def get_asset_detail(hostname: str) -> dict:
    """Return per-asset Rapid7 detail for a host: system/hardware config,
    local users, and IP/MAC addresses.

    Mirrors GET /api/dashboard/fleet/{asset_id}/detail, but resolves the
    asset by ``hostname`` (an exact match against r7_assets.hostname) rather
    than the internal asset UUID, since MCP callers know hosts by name.
    Returns {"error": ...} when no R7Asset matches — never raises. Children
    default to empty lists when Rapid7 has not collected that sub-table yet.
    """
    with get_session() as s:
        asset = s.query(R7Asset).filter(R7Asset.hostname == hostname).first()
        if asset is None:
            return {"error": f"asset not found for hostname {hostname!r}"}
        configs = (
            s.query(R7AssetConfig)
            .filter(R7AssetConfig.asset_id == asset.id)
            .order_by(R7AssetConfig.name)
            .all()
        )
        users = (
            s.query(R7AssetUser)
            .filter(R7AssetUser.asset_id == asset.id)
            .order_by(R7AssetUser.username)
            .all()
        )
        addresses = (
            s.query(R7AssetAddress)
            .filter(R7AssetAddress.asset_id == asset.id)
            .order_by(R7AssetAddress.ip)
            .all()
        )
        return {
            "id": str(asset.id),
            "hostname": asset.hostname or "",
            "ip": asset.ip or "",
            "os": asset.os or "",
            "os_product": asset.os_product or "",
            "risk_score": float(asset.risk_score or 0.0),
            "configs": [{"name": c.name, "value": c.value or ""} for c in configs],
            "users": [{"username": u.username, "full_name": u.full_name or ""} for u in users],
            "addresses": [{"ip": a.ip, "mac": a.mac or ""} for a in addresses],
        }


# ── Host posture tools (Batch E / GitLab #49, PCI-relevant) ──────────────────
# Mirror api/routers/hosts.py's get_host_posture (GET /resources/{id}/posture)
# query-by-query, split across five tools instead of one combined response, and
# keyed on hostname (Resource.name) rather than a resource_id path param — the
# existing convention for hostname-taking tools (see seed_drift_event /
# seed_vulnerability above). A host that doesn't exist, or exists but has no
# posture rows, returns an empty result (list or None) — never a raised error —
# matching get_host_posture's own "no single required root row" behavior.


@mcp.tool
def get_host_certificates(hostname: str) -> list[dict]:
    """Return certificate-store entries collected for a host (PCI Req 4)."""
    with get_session() as s:
        resource = s.query(Resource).filter(Resource.name == hostname).first()
        if resource is None:
            return []
        rows = (
            s.query(HostCertificate)
            .filter(HostCertificate.resource_id == resource.id)
            .order_by(HostCertificate.not_after.asc().nullslast())
            .all()
        )
        return [_row_to_dict(c) for c in rows]


@mcp.tool
def get_host_security_posture(hostname: str) -> dict | None:
    """Return the firewall/AV/RDP/UAC/SSH/SELinux posture summary for a host.

    One row per host (unique on resource_id). None if the host or its posture
    row doesn't exist.
    """
    with get_session() as s:
        resource = s.query(Resource).filter(Resource.name == hostname).first()
        if resource is None:
            return None
        posture = (
            s.query(HostSecurityPosture)
            .filter(HostSecurityPosture.resource_id == resource.id)
            .first()
        )
        return _row_to_dict(posture) if posture is not None else None


@mcp.tool
def get_host_firewall_rules(hostname: str) -> list[dict]:
    """Return firewall rules (iptables/nftables/firewalld) collected for a host."""
    with get_session() as s:
        resource = s.query(Resource).filter(Resource.name == hostname).first()
        if resource is None:
            return []
        rows = s.query(HostFirewallRule).filter(HostFirewallRule.resource_id == resource.id).all()
        return [_row_to_dict(r) for r in rows]


@mcp.tool
def get_host_shares(hostname: str) -> list[dict]:
    """Return SMB/NFS shares exposed by a host."""
    with get_session() as s:
        resource = s.query(Resource).filter(Resource.name == hostname).first()
        if resource is None:
            return []
        rows = s.query(HostShare).filter(HostShare.resource_id == resource.id).all()
        return [_row_to_dict(sh) for sh in rows]


@mcp.tool
def get_windows_local_admins(hostname: str) -> list[dict]:
    """Return Windows local-admin accounts for a host.

    Combines two source tables (each row tagged with 'source'): local user
    accounts with is_admin=True, and Administrators-group membership rows —
    the same two admin signals get_host_posture surfaces, filtered here to
    admin-only instead of returning every local user/group.
    """
    with get_session() as s:
        resource = s.query(Resource).filter(Resource.name == hostname).first()
        if resource is None:
            return []
        admin_users = (
            s.query(WindowsLocalUser)
            .filter(
                WindowsLocalUser.resource_id == resource.id,
                WindowsLocalUser.is_admin.is_(True),
            )
            .all()
        )
        admin_group_members = (
            s.query(WindowsLocalGroupMember)
            .filter(
                WindowsLocalGroupMember.resource_id == resource.id,
                WindowsLocalGroupMember.group_name.ilike("administrators"),
            )
            .all()
        )
        rows: list[dict] = []
        for u in admin_users:
            d = _row_to_dict(u)
            d["source"] = "local_user"
            rows.append(d)
        for g in admin_group_members:
            d = _row_to_dict(g)
            d["source"] = "group_member"
            rows.append(d)
        return rows


# ── OS inventory tools (Batch F, GitLab #50) ─────────────────────────────────


@mcp.tool
def get_linux_packages(
    hostname: str | None = None, name: str | None = None, limit: int = 100
) -> list[dict]:
    """Installed Linux packages. Two-hop join (linux_packages → linux_hosts →
    resources) surfaces the host name. Filter by hostname and/or package name."""
    with get_session() as s:
        q = (
            s.query(LinuxPackage, Resource.name.label("host"))
            .join(LinuxHost, LinuxHost.id == LinuxPackage.host_id)
            .join(Resource, Resource.id == LinuxHost.resource_id)
        )
        if hostname:
            q = q.filter(Resource.name == hostname)
        if name:
            q = q.filter(LinuxPackage.name.ilike(f"%{name}%"))
        rows = []
        for pkg, host in q.order_by(LinuxPackage.name.asc()).limit(limit).all():
            d = _row_to_dict(pkg)
            d["host"] = host
            rows.append(d)
        return rows


@mcp.tool
def get_linux_pending_updates(
    hostname: str | None = None, security_only: bool = False, limit: int = 100
) -> list[dict]:
    """Pending Linux package updates. Set security_only for just the
    security-channel updates. Filter by hostname."""
    with get_session() as s:
        q = (
            s.query(LinuxPendingUpdate, Resource.name.label("host"))
            .join(LinuxHost, LinuxHost.id == LinuxPendingUpdate.host_id)
            .join(Resource, Resource.id == LinuxHost.resource_id)
        )
        if hostname:
            q = q.filter(Resource.name == hostname)
        if security_only:
            q = q.filter(LinuxPendingUpdate.security.is_(True))
        rows = []
        for upd, host in q.order_by(LinuxPendingUpdate.package.asc()).limit(limit).all():
            d = _row_to_dict(upd)
            d["host"] = host
            rows.append(d)
        return rows


@mcp.tool
def get_linux_ports(hostname: str | None = None, limit: int = 100) -> list[dict]:
    """Listening Linux ports. Joins through linux_hosts to surface the host
    name. Filter by hostname."""
    with get_session() as s:
        q = (
            s.query(LinuxPort, Resource.name.label("host"))
            .join(LinuxHost, LinuxHost.id == LinuxPort.host_id)
            .join(Resource, Resource.id == LinuxHost.resource_id)
        )
        if hostname:
            q = q.filter(Resource.name == hostname)
        rows = []
        for port, host in q.order_by(LinuxPort.port.asc()).limit(limit).all():
            d = _row_to_dict(port)
            d["host"] = host
            rows.append(d)
        return rows


@mcp.tool
def get_linux_mounts_and_nics(hostname: str | None = None, limit: int = 100) -> dict:
    """Per-filesystem mounts and per-interface NICs for Linux hosts, returned as
    {"mounts": [...], "nics": [...]}. Each row carries the host name; filter by
    hostname."""
    with get_session() as s:
        mq = (
            s.query(LinuxMount, Resource.name.label("host"))
            .join(LinuxHost, LinuxHost.id == LinuxMount.host_id)
            .join(Resource, Resource.id == LinuxHost.resource_id)
        )
        nq = (
            s.query(LinuxNic, Resource.name.label("host"))
            .join(LinuxHost, LinuxHost.id == LinuxNic.host_id)
            .join(Resource, Resource.id == LinuxHost.resource_id)
        )
        if hostname:
            mq = mq.filter(Resource.name == hostname)
            nq = nq.filter(Resource.name == hostname)
        mounts = []
        for mount, host in mq.order_by(LinuxMount.mount.asc()).limit(limit).all():
            d = _row_to_dict(mount)
            d["host"] = host
            mounts.append(d)
        nics = []
        for nic, host in nq.order_by(LinuxNic.name.asc()).limit(limit).all():
            d = _row_to_dict(nic)
            d["host"] = host
            nics.append(d)
        return {"mounts": mounts, "nics": nics}


@mcp.tool
def get_linux_users_and_crons(hostname: str | None = None, limit: int = 100) -> dict:
    """Linux local users and cron jobs, returned as {"users": [...],
    "crons": [...]}. Each row carries the host name; filter by hostname."""
    with get_session() as s:
        uq = (
            s.query(LinuxUser, Resource.name.label("host"))
            .join(LinuxHost, LinuxHost.id == LinuxUser.host_id)
            .join(Resource, Resource.id == LinuxHost.resource_id)
        )
        cq = (
            s.query(LinuxCron, Resource.name.label("host"))
            .join(LinuxHost, LinuxHost.id == LinuxCron.host_id)
            .join(Resource, Resource.id == LinuxHost.resource_id)
        )
        if hostname:
            uq = uq.filter(Resource.name == hostname)
            cq = cq.filter(Resource.name == hostname)
        users = []
        for user, host in uq.order_by(LinuxUser.username.asc()).limit(limit).all():
            d = _row_to_dict(user)
            d["host"] = host
            users.append(d)
        crons = []
        for cron, host in cq.order_by(LinuxCron.owner.asc()).limit(limit).all():
            d = _row_to_dict(cron)
            d["host"] = host
            crons.append(d)
        return {"users": users, "crons": crons}


@mcp.tool
def get_windows_services(
    hostname: str | None = None, state: str | None = None, limit: int = 200
) -> list[dict]:
    """Windows services (per host). Joins to Resource for the host name. Filter
    by hostname and/or state (Running/Stopped)."""
    with get_session() as s:
        q = s.query(WindowsService, Resource.name.label("host")).join(
            Resource, Resource.id == WindowsService.resource_id
        )
        if hostname:
            q = q.filter(Resource.name == hostname)
        if state:
            q = q.filter(WindowsService.state == state)
        rows = []
        for svc, host in q.order_by(WindowsService.name.asc()).limit(limit).all():
            d = _row_to_dict(svc)
            d["host"] = host
            rows.append(d)
        return rows


@mcp.tool
def get_windows_software(
    hostname: str | None = None, name: str | None = None, limit: int = 200
) -> list[dict]:
    """Installed Windows software (per host). Joins to Resource for the host
    name. Filter by hostname and/or product name (substring)."""
    with get_session() as s:
        q = s.query(WindowsSoftware, Resource.name.label("host")).join(
            Resource, Resource.id == WindowsSoftware.resource_id
        )
        if hostname:
            q = q.filter(Resource.name == hostname)
        if name:
            q = q.filter(WindowsSoftware.name.ilike(f"%{name}%"))
        rows = []
        for sw, host in q.order_by(WindowsSoftware.name.asc()).limit(limit).all():
            d = _row_to_dict(sw)
            d["host"] = host
            rows.append(d)
        return rows


# ── Batch G: network / cloud / k8s query tools ────────────────────────────────
# GitLab #51 — expose the netdiscovery/net/cloud/k8s relational tables
# (db/models/cloud_k8s_net.py). All four are pure SELECT, no mutation gate.
# resource_id is nullable on every table here (collectors idle until enabled)
# and every table carries its own native name/ip/hostname column, so no
# Resource join is forced — see the plan's "resource_id join convention" note.


@mcp.tool
def get_network_discoveries(
    shadow_it_only: bool = False,
    threat_level: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return netdiscovery hosts (shadow-IT / unknown-host detection). Read-only."""
    with get_session() as s:
        q = s.query(NetDiscoveryHost)
        if shadow_it_only:
            q = q.filter(NetDiscoveryHost.is_shadow_it.is_(True))
        if threat_level:
            q = q.filter(NetDiscoveryHost.threat_level == threat_level)
        return [
            _row_to_dict(h)
            for h in q.order_by(NetDiscoveryHost.last_seen.desc()).limit(limit).all()
        ]


@mcp.tool
def get_network_devices(limit: int = 50) -> list[dict]:
    """Return SNMP-discovered network devices (switches/routers/etc.). Read-only."""
    with get_session() as s:
        q = s.query(NetDevice).order_by(NetDevice.ip)
        return [_row_to_dict(d) for d in q.limit(limit).all()]


@mcp.tool
def get_cloud_resources(
    provider: str | None = None,
    cloud_type: str | None = None,
    region: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return collected cloud (AWS) resources — ec2/vpc/security_group/etc. Read-only."""
    with get_session() as s:
        q = s.query(CloudResource)
        if provider:
            q = q.filter(CloudResource.provider == provider)
        if cloud_type:
            q = q.filter(CloudResource.cloud_type == cloud_type)
        if region:
            q = q.filter(CloudResource.region == region)
        return [_row_to_dict(c) for c in q.order_by(CloudResource.name).limit(limit).all()]


@mcp.tool
def get_k8s_resources(
    kind: str = "pod",
    namespace: str | None = None,
    cluster: str | None = None,
    limit: int = 50,
) -> list[dict] | dict:
    """Return Kubernetes objects by kind: 'node' | 'pod' | 'deployment'. Read-only."""
    models = {"node": K8sNode, "pod": K8sPod, "deployment": K8sDeployment}
    model = models.get(kind)
    if model is None:
        return {"error": f"kind must be one of {sorted(models)}; got {kind!r}"}
    with get_session() as s:
        q = s.query(model)
        if cluster:
            q = q.filter(model.cluster == cluster)
        # K8sNode has no namespace column — only filter where the attribute exists.
        if namespace and hasattr(model, "namespace"):
            q = q.filter(model.namespace == namespace)
        return [_row_to_dict(r) for r in q.order_by(model.name).limit(limit).all()]


# ── Batch J: internal governance query tools (GitLab #54) ────────────────────
# Exposes infra-brain's own safety/governance internals (not collected infra
# data) to scoped MCP clients. Scoping is enforced by per-key allowed_tools
# (mcp_auth.py), not by anything in this file. get_agent_config_status masks
# secret-hinted keys via the dashboard's own _SECRET_HINTS/mask_secret helpers
# (api/_helpers.py); get_settings goes further and delegates its ENTIRE
# redaction to api/_helpers.redact_setting, the single shared implementation
# /api/dashboard/settings also uses (TRK-342 — see the block comment above
# get_settings for why that is one function and not two). audit_log/
# agent_action_log store only hashes/summaries, never raw payloads. NOTE:
# agent_decision_log is the exception — its reasoning_text/decision_summary
# columns hold raw LLM output and are NOT scanned by DLPCallbackHandler on
# this read path (that handler only scans in-agent tool I/O). get_agent_decisions
# returns them verbatim, same exposure as the existing session-gated dashboard
# route — not a new gap, but do not extend the "hashes/summaries only" claim
# to this tool if it's ever revisited.


@mcp.tool
def get_audit_log(
    agent: str | None = None,
    allowed: bool | None = None,
    hours: int = 24,
    limit: int = 100,
) -> list[dict]:
    """Return the immutable per-tool-call audit trail (hashes only, no raw payloads). Read-only."""
    cutoff = _now_utc() - timedelta(hours=hours)
    with get_session() as s:
        q = s.query(AuditLog).filter(AuditLog.ts >= cutoff)
        if agent:
            q = q.filter(AuditLog.agent == agent)
        if allowed is not None:
            q = q.filter(AuditLog.allowed.is_(allowed))
        return [_row_to_dict(r) for r in q.order_by(AuditLog.ts.desc()).limit(limit).all()]


@mcp.tool
def get_agent_activity(
    agent: str | None = None,
    verdict: str | None = None,
    hours: int = 24,
    limit: int = 100,
) -> list[dict]:
    """Return the durable per-tool-call agent action log (verdict/status/latency). Read-only."""
    cutoff = _now_utc() - timedelta(hours=hours)
    with get_session() as s:
        q = s.query(AgentActionLog).filter(AgentActionLog.ts >= cutoff)
        if agent:
            q = q.filter(AgentActionLog.agent == agent)
        if verdict:
            q = q.filter(AgentActionLog.verdict == verdict)
        return [_row_to_dict(r) for r in q.order_by(AgentActionLog.ts.desc()).limit(limit).all()]


@mcp.tool
def get_agent_decisions(
    agent: str | None = None,
    hours: int = 24,
    limit: int = 50,
) -> list[dict]:
    """Return the per-iteration LLM reasoning/decision log. Read-only."""
    cutoff = _now_utc() - timedelta(hours=hours)
    with get_session() as s:
        q = s.query(AgentDecisionLog).filter(AgentDecisionLog.ts >= cutoff)
        if agent:
            q = q.filter(AgentDecisionLog.agent == agent)
        return [_row_to_dict(r) for r in q.order_by(AgentDecisionLog.ts.desc()).limit(limit).all()]


@mcp.tool
def get_recent_changes(
    resource: str,
    hours: int = 24,
    domain: str | None = None,
    include_graph_maintenance: bool = False,
    limit: int = 50,
) -> dict:
    """Pre-assemble "what changed for X recently" for one resource/host —
    GitLab #131's recent-changes-in-window correlation (forecast/trend half
    of #131 is a separate, deferred effort; this tool only covers the
    correlation half). Today an operator investigating an incident has to
    manually cross-reference `get_drift_events` + `get_agent_activity`
    themselves; this returns both, pre-filtered to one resource and one
    window, in a single call.

    Correlates by resource *name*: a case-insensitive substring match against
    `resources.name` for drift events, and the same substring match against
    `agent_action_log.args_summary` (its free-text tool-call-args summary)
    for activity. Neither table has a dedicated host/resource-id column
    usable here, so this is best-effort text correlation, not a structural
    join — pass the resource's exact `resources.name` value (or a unique
    substring of it) for a clean match.

    `get_audit_log` is deliberately NOT included: `audit_log` stores only a
    SHA-256 hash of each tool call's raw input (`input_hash`), never the raw
    args or any resource identifier, so it structurally cannot be filtered by
    resource/host. Only `agent_action_log` (which carries a free-text
    `args_summary`) supports this correlation.

    Reuses TRK-191's `include_graph_maintenance` exclusion (default False,
    same semantics as `get_drift_events`): the graph_maintenance agent's own
    internal self-telemetry drift ("graph-health") is excluded by default so
    it can't masquerade as real change for some unrelated resource whose name
    happens to substring-match. Pass `domain="graph_maintenance"` or
    `include_graph_maintenance=True` to opt back in.
    """
    cutoff = _now_utc() - timedelta(hours=hours)
    limit = max(1, min(limit, 200))
    needle = resource.strip().lower()

    with get_session() as s:
        dq = (
            s.query(
                DriftEvent,
                Resource.name.label("resource_name"),
                Resource.domain.label("resource_domain"),
            )
            .join(Resource, Resource.id == DriftEvent.resource_id)
            .filter(DriftEvent.detected_at >= cutoff)
            .filter(func.lower(Resource.name).contains(needle))
        )
        if domain:
            dq = dq.filter(Resource.domain == domain)
        elif not include_graph_maintenance:
            dq = dq.filter(Resource.domain != "graph_maintenance")
        drift_rows: list[dict] = []
        # GitLab #163/#164: see drift_recency() — most-recently-OBSERVED first.
        for de, rname, rdomain in dq.order_by(drift_recency().desc()).limit(limit).all():
            d = _row_to_dict(de)
            d["resource_name"] = rname
            d["resource_domain"] = rdomain
            drift_rows.append(d)

        aq = (
            s.query(AgentActionLog)
            .filter(AgentActionLog.ts >= cutoff)
            .filter(AgentActionLog.args_summary.isnot(None))
            .filter(func.lower(AgentActionLog.args_summary).contains(needle))
        )
        if domain:
            aq = aq.filter(AgentActionLog.domain == domain)
        activity_rows = [
            _row_to_dict(a) for a in aq.order_by(AgentActionLog.ts.desc()).limit(limit).all()
        ]

    return {
        "resource": resource,
        "window_hours": hours,
        "domain": domain,
        "drift_events": drift_rows,
        "agent_activity": activity_rows,
        "counts": {
            "drift_events": len(drift_rows),
            "agent_activity": len(activity_rows),
        },
    }


def _linear_forecast(
    points: list[tuple[float, float]],
) -> tuple[float, float]:
    """Ordinary-least-squares slope/intercept for ``(seconds_since_first, value)``
    pairs. Pure-Python (no numpy in this project's dependency set) — fine at
    the row counts a single resource's snapshot history produces.
    """
    n = len(points)
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_xx = sum(p[0] * p[0] for p in points)
    denom = (n * sum_xx) - (sum_x * sum_x)
    if denom == 0:
        return 0.0, sum_y / n
    slope = ((n * sum_xy) - (sum_x * sum_y)) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


@mcp.tool
def get_utilization_forecast(
    resource: str,
    metric: str,
    threshold: float,
    hours: int = 168,
    domain: str | None = None,
    min_points: int = 3,
    limit: int = 500,
) -> dict:
    """Forecast/lead-time detection — GitLab #131's forecast half (the
    correlation half shipped separately as ``get_recent_changes``/TRK-195).

    Extrapolates a linear trend from the per-sweep ``snapshots`` history
    every collector already writes (``etl/base.py``'s ``_write_snapshot``,
    append-only via ``collected_at`` — a new row every sweep, never an
    overwrite) to estimate how long until a numeric ``metric`` crosses
    ``threshold``, e.g. "disk exhausts in ~6 days" (the Datadog Watchdog-
    style forecast alert the issue cites). No new table was needed: the
    existing ``snapshots.snapshot`` JSONB blob already carries the full
    per-resource collected dict every sweep, including vSphere's
    ``cpu_usage_mhz``/``memory_usage_mb`` fields named in the issue.

    Resource matching is a case-insensitive substring against
    ``resources.name`` (same convention as ``get_recent_changes``); zero
    matches is ``"not_found"``, more than one is ``"ambiguous"`` (returns
    the candidate names rather than guessing).

    ``status`` in the response is one of:
      - ``"not_found"`` / ``"ambiguous"`` — resource resolution failed.
      - ``"insufficient_data"`` — fewer than ``min_points`` snapshots in the
        window carry a numeric value for ``metric``.
      - ``"flat_or_diverging"`` — the fitted trend is flat, or moving away
        from (or already past) ``threshold`` — no ``forecast_at``.
      - ``"forecast"`` — a projected crossing timestamp is returned along
        with the fitted slope and the sample count used.

    Trend-only: the issue's "trend+seasonality extrapolation" is scoped down
    to trend here, same precedent as TRK-195 scoping ``get_recent_changes``
    down to just the correlation half — seasonality modeling is deferred,
    not silently dropped.
    """
    cutoff = _now_utc() - timedelta(hours=hours)
    limit = max(1, min(limit, 2000))
    needle = resource.strip().lower()

    with get_session() as s:
        rq = s.query(Resource).filter(func.lower(Resource.name).contains(needle))
        if domain:
            rq = rq.filter(Resource.domain == domain)
        matches = rq.order_by(Resource.name).limit(10).all()

        if not matches:
            return {"status": "not_found", "resource": resource, "metric": metric}
        # TRK-195/#179: an exact name match takes priority over the broader
        # substring filter above -- without this, a decoy row (e.g.
        # "stale_drift:<host>") that merely CONTAINS the real host's name
        # permanently shadows it behind an "ambiguous" result the caller can
        # never resolve past (the decoy always substring-matches too).
        exact = [m for m in matches if m.name.lower() == needle]
        if len(exact) == 1:
            target = exact[0]
        elif len(matches) > 1:
            return {
                "status": "ambiguous",
                "resource": resource,
                "candidates": [m.name for m in matches],
            }
        else:
            target = matches[0]
        rows = (
            s.query(Snapshot)
            .filter(Snapshot.resource_id == target.id)
            .filter(Snapshot.collected_at >= cutoff)
            .order_by(Snapshot.collected_at.asc())
            .limit(limit)
            .all()
        )

        samples: list[tuple[float, float]] = []
        first_ts: datetime | None = None
        for row in rows:
            value = (row.snapshot or {}).get(metric)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            ts = row.collected_at
            if ts is None:
                continue
            if first_ts is None:
                first_ts = ts
            samples.append(((ts - first_ts).total_seconds(), float(value)))

    if len(samples) < max(2, min_points):
        return {
            "status": "insufficient_data",
            "resource": target.name,
            "metric": metric,
            "samples_found": len(samples),
            "min_points": min_points,
        }

    slope, intercept = _linear_forecast(samples)
    latest_t, latest_value = samples[-1]

    moving_toward = (slope > 0 and threshold > latest_value) or (
        slope < 0 and threshold < latest_value
    )
    if slope == 0 or not moving_toward:
        return {
            "status": "flat_or_diverging",
            "resource": target.name,
            "metric": metric,
            "threshold": threshold,
            "latest_value": latest_value,
            "slope_per_hour": slope * 3600,
            "samples_used": len(samples),
        }

    t_cross = (threshold - intercept) / slope
    seconds_until = t_cross - latest_t
    forecast_at = first_ts + timedelta(seconds=t_cross)

    return {
        "status": "forecast",
        "resource": target.name,
        "metric": metric,
        "threshold": threshold,
        "latest_value": latest_value,
        "slope_per_hour": slope * 3600,
        "hours_until_threshold": round(seconds_until / 3600, 2),
        "forecast_at": forecast_at.isoformat(),
        "samples_used": len(samples),
        "window_hours": hours,
    }


@mcp.tool
def get_agent_config_status(limit: int = 100) -> list[dict]:
    """Return dashboard-set agent config key/values, secrets masked. Read-only."""
    from infra_brain.api._helpers import _SECRET_HINTS, mask_secret

    with get_session() as s:
        rows = s.query(AgentConfigSetting).order_by(AgentConfigSetting.key).limit(limit).all()
        out: list[dict] = []
        for row in rows:
            value = row.value
            if any(h in row.key.lower() for h in _SECRET_HINTS):
                value = mask_secret(value)
            out.append(
                {
                    "key": row.key,
                    "value": value,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
            )
        return out


# ── The settings dump (TRK-342) ──────────────────────────────────────────────
# WHY THIS IS STILL A FULL MASKED DUMP, AND NOT AN ALLOWLIST LIKE
# `GET /api/dashboard/settings/ui`.
#
# TRK-321 split the DASHBOARD settings surface in two: the full dump became
# `require_admin`, and a narrow `_UI_SETTINGS_ALLOWLIST` route
# (`/settings/ui`, one field) serves the handful of values the UI needs so a
# non-admin session isn't handed the whole configuration surface. TRK-342
# asked whether this MCP tool should follow. The call is NO, deliberately:
#
#   * The threat TRK-321 addressed was a PRIVILEGE-TIER one — a signed-in
#     NON-admin human enumerating config. This path has no equivalent tier: an
#     MCP key is minted by an admin (`api/routers/mcp_keys.py` is entirely
#     `require_admin`-gated) and scoped by TOOL, not by field. If a key is not
#     supposed to read configuration, the correct control is to leave
#     `get_settings` out of its tool scope — a control that already exists and
#     is enforced before this function is ever entered — not to hollow out the
#     tool for every key that WAS granted it.
#   * The consumer is machine-facing. The dashboard's UI allowlist could be one
#     field because exactly one field was needed to render a page; an agent
#     asked "why is this collector skipped / what is this deployment configured
#     to do" needs the config surface, and an allowlist would have to be
#     widened on every such question until it was a denylist by attrition —
#     which is the failure mode TRK-321's own comment warns about.
#   * The class of leak TRK-318 found (cleartext DSNs) is closed here by the
#     SAME redactor the dashboard uses, not by narrowing the field list.
#
# What DID change: the redaction is no longer implemented here. It was a second
# copy — same `_SECRET_HINTS` name check, but a locally-defined anchored DSN
# regex plus whole-value `mask_secret`, where the dashboard used `scrub_dsn`.
# Both masked, but differently, and nothing kept them in step. Now both call
# `api/_helpers.redact_setting`; `tests/test_settings_redaction_parity.py`
# fails if either surface grows its own masking branch again.
#
# Behaviour delta for MCP callers, stated plainly: a credential-bearing DSN
# used to come back as `••••••rain` and now comes back as
# `postgresql+psycopg://[REDACTED]@db.internal:5432/infra_brain`. That is not a
# weakening — the shared redactor strips the username AND the password, where
# the old local regex only fired on values whose DSN started at position 0 —
# and it leaves the host/database visible, which is the whole point of an agent
# reading settings during triage.


@mcp.tool
def get_settings() -> dict:
    """Return application settings with sensitive fields masked (identical redaction to the dashboard). Read-only."""
    from infra_brain.api._helpers import redact_setting
    from infra_brain.config import get_settings as _load_settings

    fields = _load_settings().model_dump()
    return {key: redact_setting(key, value)[1] for key, value in fields.items()}


# ── Management tools ─────────────────────────────────────────────────────────


@mcp.tool
def trigger_collection(domain: str, force: bool = False) -> dict:
    """Trigger an immediate collection sweep for a domain.
    Examples: cicd, octopus, linux, windows, inventory_reconcile, remediation.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    app_url = os.getenv("INFRA_BRAIN_APP_URL", "http://app:8000")
    params = {"force": "true"} if force else {}
    try:
        resp = httpx.post(f"{app_url}/sweeps/{domain}", params=params, timeout=15)
        return {"status": resp.status_code, "body": resp.text[:500]}
    except Exception as exc:
        return {"error": str(exc)}


# ── Seeding helpers ───────────────────────────────────────────────────────────


def _seed_one_resource(session: Any, item: dict) -> tuple[str, bool]:
    """Upsert a single resource from a dict of seed_resource parameters.

    Returns (resource_id, created) where created is True if a new row was
    inserted and False if an existing row was updated.

    Task 4.4: thin delegating shim over the canonical
    ``infra_brain.api._seeding.upsert_resource`` (the ONE Resource write path
    shared by HTTP, MCP, and collectors).
    """
    from infra_brain.api._seeding import upsert_resource

    hostname = item.get("hostname") or ""
    domain = item.get("domain") or "manual"
    resource_type = item.get("resource_type", "host")
    ip_address = item.get("ip_address")
    os_name = item.get("os_name")
    environment = item.get("environment")
    tags = item.get("tags") or []
    metadata = item.get("metadata") or {}
    source = item.get("source", "manual")

    if not hostname:
        raise ValueError("hostname is required")

    # Build metadata_ payload merging caller-supplied metadata with seed extras.
    meta_payload: dict = {
        "_seed_source": source,
        "_seed_at": _now_utc().isoformat(),
    }
    if ip_address:
        meta_payload["ip_address"] = ip_address
    if os_name:
        meta_payload["os_name"] = os_name
    if environment:
        meta_payload["environment"] = environment
    if tags:
        meta_payload["tags"] = tags
    meta_payload.update(metadata)

    existing = (
        session.query(Resource).filter_by(domain=domain, type=resource_type, name=hostname).first()
    )
    created = existing is None

    resource = upsert_resource(
        session,
        name=hostname,
        domain=domain,
        resource_type=resource_type,
        metadata=meta_payload,
        source=source,
    )
    return str(resource.id), created


@mcp.tool
def seed_resource(
    hostname: str,
    domain: str,
    resource_type: str = "host",
    ip_address: str | None = None,
    os_name: str | None = None,
    environment: str | None = None,
    tags: list | None = None,
    metadata: dict | None = None,
    source: str = "manual",
) -> dict:
    """Manually seed a Resource into infra-brain (no collector required).

    Use to pre-populate hosts/assets before a collector comes online,
    or for resources you don't want a full collector for.
    domain: linux | windows | vsphere | octopus | iac | eol | manual
    resource_type: host | vm | project | file | product | device
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        with get_session() as s:
            resource_id, created = _seed_one_resource(
                s,
                {
                    "hostname": hostname,
                    "domain": domain,
                    "resource_type": resource_type,
                    "ip_address": ip_address,
                    "os_name": os_name,
                    "environment": environment,
                    "tags": tags,
                    "metadata": metadata,
                    "source": source,
                },
            )
            s.commit()
        return {"resource_id": resource_id, "created": created, "hostname": hostname}
    except Exception as exc:
        logger.warning("seed_resource failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@mcp.tool
def seed_resources_bulk(resources_yaml: str) -> dict:
    """Bulk-seed multiple resources from a YAML string.

    Each item in the YAML list should match the seed_resource parameters.
    Returns count of created/updated resources and any errors.

    Example YAML:
      - hostname: server01.corp.local
        domain: linux
        ip_address: 10.1.2.3
        environment: production
      - hostname: server02.corp.local
        domain: windows
        ip_address: 10.1.2.4
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        items = yaml.safe_load(resources_yaml)
    except Exception as exc:
        return {"error": f"YAML parse error: {exc}"}

    if not isinstance(items, list):
        return {"error": "YAML must be a list of resource objects"}

    created_count = 0
    updated_count = 0
    errors: list[dict] = []

    with get_session() as s:
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append({"index": i, "error": "item is not a dict"})
                continue
            try:
                _, created = _seed_one_resource(s, item)
                if created:
                    created_count += 1
                else:
                    updated_count += 1
            except Exception as exc:
                errors.append({"index": i, "hostname": item.get("hostname"), "error": str(exc)})
        s.commit()

    return {"created": created_count, "updated": updated_count, "errors": errors}


@mcp.tool
def seed_drift_event(
    hostname: str,
    drift_type: str,
    field: str,
    old_value: str | None = None,
    new_value: str | None = None,
    severity: str = "medium",
    source: str = "manual",
    note: str | None = None,
) -> dict:
    """Manually record a drift event for a host.

    Use to note known deviations, config drift, or policy violations
    discovered outside automated collection.
    drift_type: config_drift | new_listening_port | service_stopped | policy_violation | manual_observation
    severity: low | medium | high | critical
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        with get_session() as s:
            resource = s.query(Resource).filter(Resource.name == hostname).first()
            if resource is None:
                return {"error": "resource not found — seed it first with seed_resource"}

            # Encode severity in new_value metadata if note or severity provided.
            nv: dict | None = None
            if new_value is not None or severity or note or source:
                nv = {}
                if new_value is not None:
                    nv["value"] = new_value
                nv["severity"] = severity
                nv["source"] = source
                if note:
                    nv["note"] = note

            ov: dict | None = None
            if old_value is not None:
                ov = {"value": old_value}

            event = DriftEvent(
                resource_id=resource.id,
                drift_type=drift_type,
                field=field,
                old_value=ov,
                new_value=nv,
                status="open",
            )
            s.add(event)
            s.commit()
            s.refresh(event)
            return {"event_id": str(event.id), "resource_id": str(resource.id)}
    except Exception as exc:
        logger.warning("seed_drift_event failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@mcp.tool
def seed_vulnerability(
    hostname: str,
    cve_id: str,
    severity: str,
    description: str | None = None,
    solution: str | None = None,
    cvss_score: float | None = None,
    source: str = "manual",
) -> dict:
    """Manually record a known vulnerability for a host.

    Use to note CVEs discovered outside Rapid7 or before Rapid7 collection is live.
    severity: Low | Medium | High | Critical
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        with get_session() as s:
            resource = s.query(Resource).filter(Resource.name == hostname).first()
            if resource is None:
                return {"error": "resource not found — seed it first with seed_resource"}

            # VulnQueueItem has no description/solution/cvss_score columns;
            # store them in the Resource.metadata_ so they are not lost.
            if any(v is not None for v in (description, solution, cvss_score, source)):
                extra: dict = {"_vuln_source": source}
                if description:
                    extra["description"] = description
                if solution:
                    extra["solution"] = solution
                if cvss_score is not None:
                    extra["cvss_score"] = cvss_score
                existing_meta = resource.metadata_ or {}
                vuln_extras = existing_meta.get("_vuln_extras", {})
                vuln_extras[cve_id] = extra
                existing_meta["_vuln_extras"] = vuln_extras
                resource.metadata_ = existing_meta
                s.flush()

            # Upsert by (resource_id, cve_id) — the natural key index.
            existing = (
                s.query(VulnQueueItem)
                .filter(
                    VulnQueueItem.resource_id == resource.id,
                    VulnQueueItem.cve_id == cve_id,
                )
                .first()
            )
            if existing:
                existing.severity = severity
                existing.status = "open"
                existing.last_updated = _now_utc()
                s.commit()
                return {"vuln_id": str(existing.id), "upserted": True}

            item = VulnQueueItem(
                resource_id=resource.id,
                cve_id=cve_id,
                severity=severity,
                status="open",
                last_updated=_now_utc(),
            )
            s.add(item)
            s.commit()
            s.refresh(item)
            return {"vuln_id": str(item.id), "upserted": False}
    except Exception as exc:
        logger.warning("seed_vulnerability failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@mcp.tool
def get_seeded_resources(
    domain: str | None = None,
    source: str = "manual",
    limit: int = 50,
) -> dict:
    """List resources that were manually seeded (source='manual').

    Use to review what's been pre-populated before collectors come online.
    """
    try:
        with get_session() as s:
            q = s.query(Resource).filter(Resource.source == source)
            if domain:
                q = q.filter(Resource.domain == domain)
            # Compute true total BEFORE applying limit so count is not capped at
            # the page size (mirrors the pattern used in fleet.py / hosts.py).
            total_count = q.count()
            rows = q.order_by(Resource.last_seen.desc()).limit(limit).all()
            resources = [
                {
                    "id": str(r.id),
                    "hostname": r.name,
                    "domain": r.domain,
                    "type": r.type,
                    "ip": (r.metadata_ or {}).get("ip_address"),
                    "last_seen": r.last_seen.isoformat() if r.last_seen else None,
                }
                for r in rows
            ]
        return {"resources": resources, "count": total_count}
    except Exception as exc:
        logger.warning("get_seeded_resources failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@mcp.tool
def approve_proposal(action_id: str, approver_label: str | None = None) -> dict:
    """Approve a pending ProposedAction by ID — full parity with the dashboard.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS like every other
    mutating tool here. Writes ONLY to infra-brain's own ``proposed_actions``
    row: ``status='approved'`` plus ``approved_by``/``approved_at``. It does
    NOT contact GitLab/Jira/Confluence and does NOT touch managed
    infrastructure — with INFRA_BRAIN_MR_ENABLED=true the RemediationAgent
    opens the MR later on its own execution path, still behind the write gate.

    Guards are shared verbatim with the dashboard route
    (``action_decisions.approve_action``) so the two surfaces can never drift:
    refuses (writing nothing) an unknown action, an ``entity_resolution_same_as``
    review row (those have their own confirm path —
    ``POST /api/graph/entity-resolution/{action_id}/confirm``), a non-pending
    action, or ``confidence < 0.7``.

    ATTRIBUTION IS NOT CALLER-CONTROLLED. ``approved_by`` is derived
    server-side from the authenticated API key making the call
    (``mcp:<key name>``) — a key scoped to this tool cannot claim to be
    somebody else on the one human gate in front of a sanctioned external
    write. ``approver_label`` is an OPTIONAL free-text hint appended as a
    quoted claim (``mcp:<key name> (says: <label>)``); it can never replace or
    forge the key-identity portion.

    After the commit, any parked remediation-interrupt LangGraph for this
    action is resumed immediately instead of waiting for the next scheduled
    poll. That resume is NON-fatal: the row is already flipped, so a failure
    only defers execution back to the poll.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        aid = uuid.UUID(action_id)
    except (ValueError, TypeError):
        return {"error": "action_id must be a UUID"}
    # Derived from the authenticated key, NOT from a caller-supplied string.
    approved_by = _attributed_author(approver_label)

    from infra_brain.action_decisions import (
        ActionDecisionError,
        approve_action,
    )
    from infra_brain.remediation_graph import (
        resume_remediation_action_sync,
    )

    with get_session() as s:
        try:
            snapshot = approve_action(s, aid, approved_by)
        except ActionDecisionError as exc:
            s.rollback()
            return {"error": exc.detail}
        row = s.get(ProposedAction, aid)
        target = row.target if row is not None else None

    resumed = resume_remediation_action_sync(snapshot, approved=True)
    return {
        "approved": action_id,
        "target": target,
        "approved_by": approved_by,
        "graph_resumed": resumed,
    }


@mcp.tool
def reject_proposal(action_id: str) -> dict:
    """Reject a pending ProposedAction by ID — mirror of the dashboard route.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS like every other
    mutating tool here. Writes ONLY to infra-brain's own ``proposed_actions``
    row: ``status='rejected'``. Nothing external is contacted and no managed
    infrastructure is touched; a rejected action is simply never executed.

    Guards are shared verbatim with the dashboard route
    (``action_decisions.reject_action``): refuses (writing nothing) an unknown
    action or one that is not ``pending``.

    After the commit, any parked remediation-interrupt LangGraph for this
    action is resumed with the rejection so its thread finishes cleanly. That
    resume is NON-fatal — the row is already flipped either way.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        aid = uuid.UUID(action_id)
    except (ValueError, TypeError):
        return {"error": "action_id must be a UUID"}

    from infra_brain.action_decisions import (
        ActionDecisionError,
        reject_action,
    )
    from infra_brain.remediation_graph import (
        resume_remediation_action_sync,
    )

    with get_session() as s:
        try:
            snapshot = reject_action(s, aid)
        except ActionDecisionError as exc:
            s.rollback()
            return {"error": exc.detail}
        row = s.get(ProposedAction, aid)
        target = row.target if row is not None else None

    resumed = resume_remediation_action_sync(snapshot, approved=False)
    return {"rejected": action_id, "target": target, "graph_resumed": resumed}


@mcp.tool
def promote_instinct(
    pattern: str,
    domain: str,
    citation: str,
    approved_by: str | None = None,
    zone: str = "corpor",
    confidence: float = 0.8,
) -> dict:
    """Promote a new learned pattern to the instincts knowledge base.

    ATTRIBUTION IS NOT CALLER-CONTROLLED. ``promoted_by`` is derived
    server-side from the authenticated MCP key making this call
    (``mcp:<key name>``), the same mechanism ``approve_proposal`` and every
    other mutation tool in this file use (see the "Caller identity
    (unforgeable attribution)" comment block above ``_caller_identity()``) —
    a key scoped only to this one tool cannot claim to be an arbitrary human
    or agent when writing into the live instincts knowledge base downstream
    agents consume. ``approved_by`` is kept as a parameter name for backward
    compatibility with existing callers, but it is now treated as an
    OPTIONAL free-text label appended as a quoted claim
    (``mcp:<key name> (says: <label>)``); it can never replace or forge the
    key-identity portion. (This was previously a raw caller-supplied string
    written verbatim to ``Instinct.promoted_by`` — TRK-136 fixed missing
    input validation but left this attribution gap; fixed here.)
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    # TRK-136: beyond the blanket mutation gate above, promote_instinct had no
    # server-side validation at all — an empty citation or an out-of-range
    # confidence would be written straight to the instincts table. This is a
    # deliberately pragmatic stopgap (not the full InstinctPromotionGate/
    # DualControlGate design) — simple, non-silent input validation on top of
    # the existing gate, which stays unchanged.
    if not citation or not citation.strip():
        return {"error": "citation must be non-empty (whitespace-only is rejected)"}
    if not (0 < confidence <= 1):
        return {"error": f"confidence must satisfy 0 < confidence <= 1; got {confidence!r}"}
    # Derived from the authenticated key, NOT from the caller-supplied
    # approved_by string — see docstring above.
    promoted_by = _attributed_author(approved_by)
    with get_session() as s:
        instinct = Instinct(
            zone=zone,
            domain=domain,
            pattern=pattern,
            confidence=confidence,
            promoted_by=promoted_by,
            citation=citation,
        )
        s.add(instinct)
        s.commit()
        s.refresh(instinct)
        return {"promoted": str(instinct.id), "domain": domain, "confidence": confidence}


@mcp.tool
def add_eol_product(
    asset_name: str,
    eol_date: str,
    pci_risk_score: int | None = None,
    migration_path: str | None = None,
    resource_id: str | None = None,
) -> dict:
    """Register a product in the EOL registry. eol_date format: YYYY-MM-DD."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        eol_dt = datetime.fromisoformat(eol_date).replace(tzinfo=UTC)
    except ValueError:
        return {"error": f"Invalid eol_date '{eol_date}' — use YYYY-MM-DD"}

    with get_session() as s:
        if resource_id:
            try:
                rid = uuid.UUID(resource_id)
            except (ValueError, TypeError):
                return {"error": "resource_id must be a UUID"}
            resource = s.query(Resource).filter(Resource.id == rid).first()
            if not resource:
                return {"error": f"Resource {resource_id} not found"}
        else:
            resource = None

        # Upsert by asset_name so a manual entry MERGES with an auto-derived one
        # (EOLAgent derives the registry from OS inventory keyed on asset_name).
        entry = s.query(EolRegistry).filter(EolRegistry.asset_name == asset_name).first()
        if entry is not None:
            entry.eol_date = eol_dt
            if pci_risk_score is not None:
                entry.pci_risk_score = pci_risk_score
            if migration_path is not None:
                entry.migration_path = migration_path
            if resource is not None:
                entry.resource_id = resource.id
            entry.last_updated = datetime.now(UTC)
            s.commit()
            s.refresh(entry)
            return {"updated": str(entry.id), "asset": asset_name, "eol_date": eol_date}

        if resource is None:
            from infra_brain.api._seeding import upsert_resource

            resource = upsert_resource(
                s,
                name=asset_name,
                domain="eol",
                resource_type="product",
                source="mcp",
            )

        entry = EolRegistry(
            resource_id=resource.id,
            asset_name=asset_name,
            eol_date=eol_dt,
            pci_risk_score=pci_risk_score,
            migration_path=migration_path,
        )
        s.add(entry)
        s.commit()
        s.refresh(entry)
        return {"registered": str(entry.id), "asset": asset_name, "eol_date": eol_date}


# ── Batch I: governance / compliance query tools ──────────────────────────────
# Mirrors api/routers/governance_drift.py (get_drift_trend grouping) and
# list_notifications (JiraTicket + ConfluencePage merge), see GitLab #53. All
# read-only — no mutation gate needed, none call a write path.


@mcp.tool
def get_compliance_violations(
    status: str = "open",
    severity: str | None = None,
    rule: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return policy-as-code compliance violations (ComplianceAgent). Read-only."""
    with get_session() as s:
        q = s.query(ComplianceViolation)
        if status:
            q = q.filter(ComplianceViolation.status == status)
        if severity:
            q = q.filter(ComplianceViolation.severity == severity)
        if rule:
            q = q.filter(ComplianceViolation.rule == rule)
        return [
            _row_to_dict(v)
            for v in q.order_by(ComplianceViolation.detected_at.desc()).limit(limit).all()
        ]


@mcp.tool
def get_drift_trend(days: int = 30, domain: str | None = None) -> dict:
    """Return date-grouped drift-event counts over the last N days (per domain). Read-only."""
    cutoff = _now_utc() - timedelta(days=days)
    with get_session() as s:
        q = (
            s.query(
                func.date(DriftEvent.detected_at).label("date"),
                Resource.domain.label("domain"),
                func.count().label("count"),
            )
            .join(Resource, Resource.id == DriftEvent.resource_id)
            .filter(DriftEvent.detected_at >= cutoff)
        )
        if domain:
            q = q.filter(Resource.domain == domain)
        q = q.group_by(func.date(DriftEvent.detected_at), Resource.domain).order_by(
            func.date(DriftEvent.detected_at)
        )
        rows = q.all()
    points = [{"date": str(r.date), "domain": r.domain, "count": r.count} for r in rows]
    return {
        "points": points,
        "total": sum(p["count"] for p in points),
        "days": days,
        "domain_filter": domain or "",
    }


@mcp.tool
def get_notifications(type: str | None = None, limit: int = 50) -> list[dict]:
    """Return recent Jira ticket + Confluence page notifications, newest first. Read-only."""
    out: list[dict] = []
    with get_session() as s:
        if type in (None, "jira"):
            for jt in s.query(JiraTicket).order_by(JiraTicket.created_at.desc()).limit(limit):
                out.append(
                    {
                        "type": "jira",
                        "target": jt.jira_key,
                        "title": jt.jira_key,
                        "domain": "—",
                        "created": jt.created_at.isoformat() if jt.created_at else None,
                        "status": "open",
                    }
                )
        if type in (None, "confluence"):
            for cp in (
                s.query(ConfluencePage).order_by(ConfluencePage.last_updated.desc()).limit(limit)
            ):
                out.append(
                    {
                        "type": "confluence",
                        "target": cp.page_id,
                        "title": f"{cp.domain} page",
                        "domain": cp.domain,
                        "created": cp.last_updated.isoformat() if cp.last_updated else None,
                        "status": "synced",
                    }
                )
    out.sort(key=lambda n: n["created"] or "", reverse=True)
    return out[:limit]


@mcp.tool
def get_agent_roster() -> list[dict]:
    """Return the registered domain-agent roster with each agent's last collection run. Read-only."""
    from infra_brain.etl.spec import retired_domains
    from infra_brain.runtime_flags import paused_domains
    from infra_brain.supervisor import AGENT_REGISTRY

    # One fleet-wide query rather than a per-domain override lookup in the loop.
    paused = paused_domains()
    retired = retired_domains()

    with get_session() as s:
        out: list[dict] = []
        for domain, cls in AGENT_REGISTRY.items():
            last = (
                s.query(CollectionRun)
                .filter(CollectionRun.domain == domain)
                .order_by(CollectionRun.started_at.desc())
                .first()
            )
            out.append(
                {
                    "domain": domain,
                    "agent": cls.__name__,
                    "last_run": last.started_at.isoformat() if last and last.started_at else None,
                    "last_status": last.status if last else None,
                    "resources_found": last.resources_found if last else None,
                    # TRK-271: schedule=None means this agent is hook-/graph-driven
                    # (invoked by _post_collection_hook, never goes through
                    # dispatch(), so it never writes a CollectionRun row) — a null
                    # last_run here is expected, not a sign of being unwired.
                    "hook_driven": cls.spec.schedule is None,
                    # A live dispatchable__<domain>=false override (the
                    # dashboard's Disable Collector toggle). Without this a
                    # deliberately paused domain is indistinguishable from a
                    # dormant/broken one — the same false signal hook_driven
                    # above exists to prevent.
                    "paused": domain in paused,
                    # Switched off by standing decision (AgentSpec.retired):
                    # its upstream system does not exist in this environment.
                    # Reported SEPARATELY from `paused` because the two mean
                    # different things to an operator — paused is a temporary
                    # toggle on a collector that is expected back, retired is
                    # "not coming back unless you decide otherwise". A retired
                    # domain's last_run is frozen at whatever it last did and
                    # will not advance; that is correct, not dormancy.
                    "retired": domain in retired,
                }
            )
        return out


@mcp.tool
def get_tool_catalog() -> dict:
    """Machine-readable catalog of every MCP tool: which are read-only vs.
    mutation-scoped, and which dashboard group each belongs to (#197).

    Exists for headless enforcement/consumers that need to answer "what
    actions exist and what scope do they need" programmatically, instead of
    parsing prose docs — ``MUTATION_TOOL_NAMES``/``TOOL_GROUPS`` in
    ``mcp_auth.py`` were already the enforcement source of truth, but were
    never exposed as a queryable tool. ``version`` is a content hash of the
    catalog itself, so a consumer can detect a policy change (a tool added,
    rescoped, or regrouped) without diffing the whole payload every call.
    ``mutations_globally_enabled`` reflects the separate
    ``INFRA_BRAIN_MCP_ENABLE_MUTATIONS`` gate — a tool being scoped as
    "mutation" does not by itself mean it is currently callable. Read-only.
    """
    from infra_brain.mcp_auth import MUTATION_TOOL_NAMES, READONLY_TOOL_NAMES, TOOL_GROUPS
    from infra_brain.mcp_auth import catalog_version as _catalog_version

    return {
        "version": _catalog_version(),
        "readonly_tools": list(READONLY_TOOL_NAMES),
        "mutation_tools": list(MUTATION_TOOL_NAMES),
        "tool_groups": TOOL_GROUPS,
        "mutations_globally_enabled": _mutations_enabled(),
    }


@mcp.tool
def get_sweep_status() -> list[dict]:
    """Return the latest collection-run status per domain (sweep health at a glance). Read-only."""
    with get_session() as s:
        domains = [d for (d,) in s.query(CollectionRun.domain).distinct().all()]
        out: list[dict] = []
        for domain in sorted(domains):
            last = (
                s.query(CollectionRun)
                .filter(CollectionRun.domain == domain)
                .order_by(CollectionRun.started_at.desc())
                .first()
            )
            out.append(
                {
                    "domain": domain,
                    "status": last.status,
                    "started_at": last.started_at.isoformat() if last.started_at else None,
                    "finished_at": last.finished_at.isoformat() if last.finished_at else None,
                    "resources_found": last.resources_found,
                    "drift_count": last.drift_count,
                    # GitLab #159/#160: collection_runs.error_message IS persisted
                    # (get_collection_health has always returned it), but this
                    # hand-built dict silently dropped it. That is precisely what
                    # made a failed 0-resource run look mysterious here: status
                    # said "failed", and the reason why was one column away.
                    "error_message": last.error_message,
                }
            )
        return out


@mcp.tool
def get_scan_schedule(domain: str | None = None, limit: int = 50) -> list[dict]:
    """Return COVERAGE-WIRED scan points, NOT agent cron schedules. Read-only.

    Despite the name, this does not tell you when any agent runs. It returns
    rows from the ``scan_points`` table, which is populated ONLY by
    ``agents/coverage.py``'s ``wire()`` — a record of which monitoring/scanning
    surfaces coverage analysis has mapped to a domain.

    **An empty result for a built-in domain agent (including ``discovery``) is
    normal and is not a bug signal** — it just means coverage wiring has not
    recorded a scan point for that domain. Reading emptiness here as "the agent
    is not scheduled" is a misdiagnosis this docstring previously invited.

    For the actual agent cron schedules, use ``get_agent_roster``.
    """
    with get_session() as s:
        q = s.query(ScanPoint)
        if domain:
            q = q.filter(ScanPoint.domain == domain)
        return [_row_to_dict(p) for p in q.order_by(ScanPoint.domain).limit(limit).all()]


# ── Batch M: backup / DR-drill posture (GitLab #96) ────────────────────────────
# One tool over backup_jobs (db/models/backup.py) — BackupAgent's freshness
# collector. Chaos/DR-test tracking folds into the same row
# (last_restore_test_at) rather than a separate tool, per backup.py's own
# issue #96 scope decision.


@mcp.tool
def get_backup_status(days_overdue: int | None = None, limit: int = 100) -> list[dict]:
    """Return backup_jobs rows — per-(resource, backend, job_name) backup
    verification / DR-drill freshness (GitLab #96). Read-only.

    Every row carries two COMPUTED booleans, ``backup_overdue`` and
    ``restore_test_overdue``, using config.py's ``backup_overdue_days`` (2)
    and ``backup_restore_test_overdue_days`` (90) thresholds — the same
    freshness facts ``BackupAgent.collect()`` derives at collection time.
    Per db/models/backup.py's design notes, "overdue" is deliberately NOT a
    stored column — it is recomputed here at query time from
    ``last_success_at`` / ``last_restore_test_at``, same as
    ``get_eol_status``'s NULL-inclusive convention: a job that has never
    succeeded (``last_success_at IS NULL``) counts as overdue, not
    "unknown, skip it" — the never-backed-up case is the most urgent one,
    not the least.

    Pass ``days_overdue`` to filter results to only jobs overdue by MORE than
    that many days since ``last_success_at`` (NULL is included in the
    filter). Omit it (default) to return every ``backup_jobs`` row,
    unfiltered.

    Empty ``backup_jobs`` (BackupAgent dormant/unconfigured — no live Veeam/
    Bacula/cloud-snapshot target set, same pattern as vSphere) returns an
    empty list, not an error.
    """
    from infra_brain.config import get_settings

    settings = get_settings()
    now = _now_utc()
    overdue_window = timedelta(days=settings.backup_overdue_days)
    restore_test_window = timedelta(days=settings.backup_restore_test_overdue_days)

    def _aware(dt):
        # A DateTime(timezone=True) column can round-trip as tz-naive under
        # sqlite (the test suite's dialect) even though Postgres preserves
        # tzinfo — normalize before subtracting from `now`, same fix as
        # get_cve_detail's sla_due handling above.
        if dt is None:
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    with get_session() as s:
        q = s.query(BackupJob, Resource.name.label("resource_name")).join(
            Resource, Resource.id == BackupJob.resource_id
        )
        if days_overdue is not None:
            cutoff = now - timedelta(days=days_overdue)
            q = q.filter(
                or_(BackupJob.last_success_at <= cutoff, BackupJob.last_success_at.is_(None))
            )
        rows = []
        for job, rname in (
            q.order_by(BackupJob.last_success_at.asc().nullsfirst()).limit(limit).all()
        ):
            last_success = _aware(job.last_success_at)
            last_restore_test = _aware(job.last_restore_test_at)
            d = _row_to_dict(job)
            d["resource_name"] = rname
            d["backup_overdue"] = last_success is None or (now - last_success) > overdue_window
            d["restore_test_overdue"] = (
                last_restore_test is None or (now - last_restore_test) > restore_test_window
            )
            rows.append(d)
        return rows


# ── Batch N: home-lab service status by category ───────────────────────────────
# HomelabServicesAgent (agents/homelab_services.py) probes every URL-bearing
# manifest entry with a bare GET and writes each as a single generic
# Resource(domain="homelab_services", type="homelab_service") -- there is
# deliberately no separate per-category AgentSpec/table (it is one lightweight
# reachability sweep, not independent collectors per category). Category lives
# inside the JSONB metadata_ column (DB column "metadata"), alongside url/
# status/http_status, not as its own resources column. Before this tool,
# nothing let a caller ask "what's my media stack's status" -- query_resources
# could filter to domain="homelab_services" but not narrow further by
# category, so a Buzz/chat message like "how's Sonarr doing" had no read path
# that could actually answer using this data.


@mcp.tool
def get_homelab_service_category(category: str | None = None, limit: int = 200) -> list[dict]:
    """Return HomelabServicesAgent-collected resources (domain="homelab_services"),
    optionally filtered by category. Read-only.

    This is the read path for "what's my media stack's status" / "is Sonarr
    up" style questions: pass ``category="media-management"`` (sonarr/radarr/
    romm/tdarr/disc-ripper/media-agent in the current manifest) or
    ``category="media-server"`` (emby/synology-photos), or any of the ~30
    other categories the manifest declares (ai-agent, ai-inference,
    observability-monitoring, security-siem, storage-nas, home-automation,
    etc.) -- read ``homelab_services_manifest.json`` for the authoritative,
    current set; it is intentionally not hardcoded or validated here.

    Each row is a flattened Resource: the top-level fields plus ``category``,
    ``url``, ``status`` (``"up"``/``"down"``), and ``http_status`` pulled out
    of the JSONB ``metadata_`` payload for convenience, alongside the raw
    ``metadata_`` dict itself (unchanged, for callers that want the full
    payload).

    ``category`` is collector-supplied free text, not an enum -- there is no
    server-side validation against a known list. An unknown/misspelled value
    returns an empty list, not an error (same "empty result over exception
    for a bad filter" convention as the rest of this module). Omit it
    (default) to return every homelab_services resource across all
    categories, unfiltered -- same "no filter = no restriction" convention as
    ``get_backup_status``'s ``days_overdue``.

    Filtering uses ``Resource.metadata_["category"].as_string()``, the same
    cross-dialect JSONB-on-PG/JSON-on-SQLite construct already used by
    ``get_manual_writes``/the bulk-proposal selector for ``payload["field"]``
    (see ``db/models/_base.py``'s ``JSONB`` type) -- it renders ``->>`` on
    PostgreSQL and ``json_extract`` on SQLite, so this tool is exercised by
    the sqlite test suite exactly like production.

    32 of the manifest's current 92 services are intentionally excluded from
    this table -- they carry ``url: null`` in the manifest (no confirmed
    working URL, honestly flagged rather than guessed) and
    ``HomelabServicesAgent.collect()`` skips any entry missing a url, so
    those are never probed or persisted as resources. That is expected
    behavior, not missing/stale data -- see the manifest's own ``$comment``
    and methodology notes.

    Writes: none.
    """
    with get_session() as s:
        q = s.query(Resource).filter(Resource.domain == "homelab_services")
        if category:
            q = q.filter(Resource.metadata_["category"].as_string() == category)
        rows = []
        for r in q.order_by(Resource.name).limit(limit).all():
            d = _row_to_dict(r)
            meta = d.get("metadata_") or {}
            d["category"] = meta.get("category")
            d["url"] = meta.get("url")
            d["status"] = meta.get("status")
            d["http_status"] = meta.get("http_status")
            rows.append(d)
        return rows


# ── Batch K: relationship-graph traversal (GitLab #127, Phase 3) ──────────────
# Read-only traversal over graph_edges — the only edge store since P5 dropped
# resource_relationships (identity is SAME_AS in the graph; containment facts
# are read from the detail tables, not edges) — plus the entity-resolution
# review queue. Every
# response is capped/ranked/summarised server-side per TRK-180's limit-param
# convention — an LLM never receives a raw subgraph dump.
#
# confirm_same_as is the ONLY mutating tool here and sits behind the same
# _mutations_enabled() gate as every other mutating MCP tool (approve_proposal,
# promote_instinct, add_eol_product, …). It writes ONLY to infra-brain's own
# Postgres (a graph_edges row + a proposed_actions status transition), never to
# an external system, so callbacks/write_gate.py — which gates outbound GitLab
# MR / Jira / Confluence payloads — is deliberately NOT involved. See the
# "Review queue" and read-only notes in infra_brain/graph_phase3.py.


@mcp.tool
def get_blast_radius(
    node_id: str,
    max_hops: int = 2,
    min_confidence: float = 1.0,
    top_n: int = 25,
) -> dict:
    """What else is affected if this graph entity breaks? Read-only.

    ``node_id`` is a ``graph_nodes.id`` UUID (Phase 2 / #126 store). Walks
    BOTH edge stores — ``graph_edges`` from the node, and the older
    ``resource_relationships`` (STORED_ON, VULNERABLE_TO, HAS_SOFTWARE,
    IS_SAME_AS, …) from that node's ``resource_id`` when it has one — via
    bounded, cycle-safe recursive CTEs. Graph-first P4: a relationship the
    collectors DECLARE (BELONGS_TO / DEFINED_IN / RUNS_IMAGE) is answered
    ONCE, from ``graph_edges``; the legacy store's frozen copy of it is
    skipped, so a declared neighbour no longer appears twice under two
    different ids.

    ``min_confidence`` defaults to 1.0, i.e. **declared edges only**. This is
    deliberate: a blast-radius answer is used to decide what to touch during
    an incident, so derived edges must be opted into. Pass 0.99 to include
    deterministic name matches, 0.8 to include fuzzy identity links.

    ``max_hops`` is clamped to 3 and ``top_n`` to 100. Neighbours are
    deduplicated keeping the SHORTEST hop distance, ranked by (hop asc,
    confidence desc, name), and each carries a one-line ``why`` instead of raw
    evidence JSON. ``truncated``/``total_found`` report what the cap hid.
    Returns {"error": ...} for an unknown node — never raises.
    """
    try:
        nid = uuid.UUID(node_id)
    except (ValueError, TypeError):
        return {"error": "node_id must be a UUID (a graph_nodes.id)"}
    from infra_brain.graph_phase3 import blast_radius

    with get_session() as s:
        return blast_radius(s, nid, max_hops=max_hops, min_confidence=min_confidence, top_n=top_n)


@mcp.tool
def get_root_cause_candidates(node_id: str, since: str, top_n: int = 10) -> dict:
    """Recent nearby changes that could explain a problem at this node. Read-only.

    ``node_id`` is a ``graph_nodes.id`` UUID; ``since`` is an ISO-8601
    timestamp (naive values are read as UTC). Walks 2 hops over both edge
    stores, maps the reached entities to ``resources.id``, and returns
    ``drift_events`` at/after ``since`` on those resources.

    Unlike ``get_blast_radius`` this walk includes LOW-confidence edges on
    purpose: a merely-probable identity link is a perfectly good investigative
    lead, and every candidate reports the ``hop_distance``/``why`` it came from
    so the reader can discount it. Blast radius decides what to touch; this
    decides what to look at.

    ``graph_maintenance`` self-telemetry drift is excluded (TRK-191).
    ``delta`` is a one-line ``field: old -> new`` summary, not raw JSONB.
    ``top_n`` is clamped to 100. Returns {"error": ...} on a bad node/timestamp.
    """
    try:
        nid = uuid.UUID(node_id)
    except (ValueError, TypeError):
        return {"error": "node_id must be a UUID (a graph_nodes.id)"}
    try:
        since_dt = datetime.fromisoformat(since)
    except (ValueError, TypeError):
        return {"error": f"since must be an ISO-8601 timestamp; got {since!r}"}
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=UTC)

    from infra_brain.graph_phase3 import root_cause_candidates

    with get_session() as s:
        return root_cause_candidates(s, nid, since_dt, top_n=top_n)


@mcp.tool
def get_reconciliation_state(domain: str | None = None, limit: int = 50) -> list[dict]:
    """Return the entity-resolution review queue — ambiguous identity matches. Read-only.

    These are cross-source host matches the resolver refused to auto-merge:
    either a normalized-name key that matched more than one node inside a
    single source, or a fuzzy score in the ambiguous band. Each row carries
    the ``source_node``, its ranked ``candidate_matches[]`` (with per-candidate
    score and reason), and ``status`` (pending | approved | rejected). Pending
    rows sort first.

    ``domain`` filters on the source node's collector ("vsphere", "rapid7",
    "ansible", "octopus"). ``limit`` caps rows (TRK-180 convention).

    Resolve a row with ``confirm_same_as``. Nothing here has been merged —
    that is the point.
    """
    from infra_brain.graph_phase3 import get_reconciliation_state as _state

    limit = max(1, min(limit, 200))
    with get_session() as s:
        return _state(s, domain=domain)[:limit]


@mcp.tool
def confirm_same_as(source_node_id: str, target_node_id: str, approver: str) -> dict:
    """Human-in-the-loop: confirm two graph nodes are the SAME machine.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS like every other
    mutating tool here. Writes only to infra-brain's own database: a confirmed
    ``SAME_AS`` edge pair in ``graph_edges`` (method='declared', confidence
    1.000, approver recorded in the edge's evidence) plus, when a matching
    review-queue row is pending, its transition to ``status='approved'`` with
    ``approved_by``/``approved_at``. No external system is contacted, so the
    read-only-to-infrastructure guarantee is untouched.

    ``declared`` at 1.000 is justified by the human: an accountable, named
    operator assertion is stronger evidence than any string match the resolver
    can compute, and it is attributable because the name is stored.

    Refuses (returns ``error``, writes nothing) for an unknown node, the same
    node twice, two nodes of the SAME source type (that is within-source
    dedup, not cross-source identity), or a blank ``approver``.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        src = uuid.UUID(source_node_id)
        tgt = uuid.UUID(target_node_id)
    except (ValueError, TypeError):
        return {"error": "source_node_id and target_node_id must be UUIDs (graph_nodes.id)"}

    from infra_brain.graph_phase3 import confirm_same_as as _confirm

    with get_session() as s:
        result = _confirm(s, src, tgt, approver)
        if "error" in result:
            s.rollback()
            return result
        s.commit()
        return result


@mcp.tool
def reject_same_as(
    action_id: str, rejector: str, target_node_id: str = "", reason: str = ""
) -> dict:
    """Human-in-the-loop: reject an identity question — these are NOT the same.

    The mirror of ``confirm_same_as``, and route-parity with
    ``POST /api/graph/entity-resolution/{action_id}/reject``.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS like every other
    mutating tool here. Writes only to infra-brain's own database: an
    attributed, symmetric ``NOT_SAME_AS`` edge pair (authority='human',
    method='declared', confidence 1.000, rejector recorded in the edge's
    evidence) plus the review row's candidate-list/status update. No external
    system is contacted.

    A rejection means: *this named human asserts that this specific PAIR are
    not the same entity, judged against the evidence presented at rejection
    time.* Pair-scoped, never node-scoped. The veto blocks every automatic
    identity emitter from ever auto-merging that pair, and — unlike the old
    bare status flip — does NOT silence future questions about the node; a
    later pass may re-ASK on strictly stronger evidence (the
    fuzzy < exact_name < hard_identifier ladder), never re-emit.

    ``target_node_id`` (optional) must be one of the row's own
    ``candidate_matches``; omit it to reject every listed candidate. Refuses
    (returns ``error``, writes nothing) for an unknown/non-review/non-pending
    action, a blank ``rejector``, or a target outside the candidate list.
    Undo with ``retract_not_same_as``.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        act = uuid.UUID(action_id)
    except (ValueError, TypeError):
        return {"error": "action_id must be a UUID (proposed_actions.id)"}
    tgt: uuid.UUID | None = None
    if target_node_id:
        try:
            tgt = uuid.UUID(target_node_id)
        except (ValueError, TypeError):
            return {"error": "target_node_id must be a UUID (graph_nodes.id)"}

    from infra_brain.graph_phase3 import reject_same_as as _reject

    with get_session() as s:
        result = _reject(s, act, rejector, target_node_id=tgt, reason=reason or None)
        if "error" in result:
            s.rollback()
            return result
        s.commit()
        return result


@mcp.tool
def retract_not_same_as(
    source_node_id: str, target_node_id: str, retractor: str, reason: str = ""
) -> dict:
    """Undo a rejection (see ``reject_same_as``) — withdraw a human veto.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS. Closes the validity
    interval on both directed ``NOT_SAME_AS`` edges (never DELETE — a
    withdrawn rejection is still a thing that happened, and who withdrew it is
    stamped into each edge's evidence). Writes only to infra-brain's own
    database.

    Refuses (returns ``error``, writes nothing) when no active veto exists
    between the two nodes, they are the same node, or ``retractor`` is blank.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        src = uuid.UUID(source_node_id)
        tgt = uuid.UUID(target_node_id)
    except (ValueError, TypeError):
        return {"error": "source_node_id and target_node_id must be UUIDs (graph_nodes.id)"}

    from infra_brain.graph_phase3 import retract_not_same_as as _retract_veto

    with get_session() as s:
        result = _retract_veto(s, src, tgt, retractor, reason=reason or None)
        if "error" in result:
            s.rollback()
            return result
        s.commit()
        return result


@mcp.tool
def retract_same_as(
    source_node_id: str, target_node_id: str, retractor: str, reason: str = ""
) -> dict:
    """Undo a confirmed SAME_AS pairing (see confirm_same_as) -- correct a
    mistaken human confirmation.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS like every other
    mutating tool here. Writes only to infra-brain's own database: closes the
    validity interval on both directed SAME_AS edges (never DELETE -- the
    historical record, including who confirmed it and who retracted it, is
    preserved in each edge's evidence) and, if a matching review-queue row was
    resolved by that confirmation, reopens it to status='pending' so the
    identity question can be re-decided. No external system is contacted.

    Refuses (returns ``error``, writes nothing) when no active SAME_AS edge
    exists between the two nodes, they are the same node, or ``retractor`` is
    blank.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        src = uuid.UUID(source_node_id)
        tgt = uuid.UUID(target_node_id)
    except (ValueError, TypeError):
        return {"error": "source_node_id and target_node_id must be UUIDs (graph_nodes.id)"}

    from infra_brain.graph_phase3 import retract_same_as as _retract

    with get_session() as s:
        result = _retract(s, src, tgt, retractor, reason=reason or None)
        if "error" in result:
            s.rollback()
            return result
        s.commit()
        return result


# ── Manual reasoner-tier writes (provenance-marked) ──────────────────────────
# The two reasoner-tier LLM features (rootcause_llm_enabled,
# compliance_gap_finder_enabled) default OFF and stay blocked on real model
# access. These two tools let an operator record the SAME KIND of row by hand
# through the MCP surface — but every row they write is permanently and
# visibly marked as manual/MCP-authored, never presented as if RootCauseAgent
# or ComplianceAgent produced it via real LLM reasoning:
#
#   * RootCauseNote  -> the JSONB ``correlated`` column carries
#     {"source": "manual_mcp", "authored_by": ...} (marker keys written LAST so
#     caller-supplied data can never overwrite them), AND the free-text
#     ``explanation`` is prefixed with the human-readable banner below.
#   * ProposedAction -> the ``agent`` column is "manual_mcp" (not "compliance"),
#     the JSONB ``payload`` carries source/authored_by, and the payload's
#     ``description`` prose carries the same banner.
#
# The ``authored_by`` value in both is DERIVED FROM THE AUTHENTICATED KEY
# (``mcp:<key name>``), never taken from a caller-supplied string — see
# _caller_identity/_attributed_author above. Consumers must branch on the
# STRUCTURED marker (``correlated["source"]`` / ``payload["source"]``), not on
# the prose banner, which is for human readers only and does not survive
# truncation (see agents/drift_learning.py's Instinct promotion).
#
# Both write ONLY to infra-brain's own tables. Neither can reach GitLab/Jira/
# Confluence: compliance_rule_gap rows are inert by design (the remediation
# executor's action-type filter never picks them up) and RootCauseNote has no
# execution path at all.

# Re-exported from infra_brain.provenance so non-MCP consumers (drift_learning)
# can check the marker without importing this FastMCP module.
MANUAL_PROVENANCE_SOURCE = _MANUAL_PROVENANCE_SOURCE
_manual_banner = manual_banner

# Phase 2 (2026-07-29 implementation plan, TRK-247 mitigation): hard cap for
# get_manual_writes(). A request above this is CLAMPED, never silently
# truncated without the response saying so (see limit_clamped below).
_MANUAL_WRITES_LIMIT_CAP = 500  # hard cap


@mcp.tool
def get_manual_writes(
    kind: str = "all",
    authored_by: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> dict:
    """Return every manual/MCP-authored write — read-only, purely provenance-driven.

    Surfaces exactly the rows ``record_rootcause_note``/``record_compliance_gap``
    write: ``root_cause_notes`` where ``correlated->>'source' == 'manual_mcp'``,
    and ``proposed_actions`` where ``agent == 'manual_mcp'``. Both markers are
    server-generated and written LAST by those tools (see their docstrings) —
    a caller can never mask a manual write to hide it from this query. This
    makes the 928 ``mcp:unauthenticated`` backfill root-cause notes (and any
    future direct-invocation write) findable retroactively, which is the
    single highest-leverage TRK-247 mitigation.

    Args:
        kind: ``"rootcause"`` (root_cause_notes only), ``"compliance_gap"``
            (proposed_actions only), or ``"all"`` (default — both).
        authored_by: optional substring filter against the ``authored_by``
            marker (e.g. ``"reporting-bot"`` or ``"mcp:unauthenticated"``).
        since: optional ISO-8601 timestamp; only rows created/recorded at or
            after it.
        limit: default 100, per result set, hard-capped at 500. A request
            above the cap is CLAMPED rather than silently truncated — the
            response's ``limit_applied``/``limit_clamped`` fields make that
            explicit instead of leaving the caller to guess why fewer rows
            than expected came back.

    Writes: none.
    """
    if kind not in ("rootcause", "compliance_gap", "all"):
        return {"error": f"kind must be one of rootcause|compliance_gap|all, got {kind!r}"}

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            return {"error": f"since must be an ISO-8601 timestamp, got {since!r}"}
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)

    limit_requested = limit
    limit_applied = max(0, min(limit, _MANUAL_WRITES_LIMIT_CAP))

    rootcause: list[dict] = []
    compliance_gap: list[dict] = []

    with get_session() as s:
        if kind in ("rootcause", "all") and limit_applied:
            q = s.query(RootCauseNote).filter(
                RootCauseNote.correlated["source"].as_string() == MANUAL_PROVENANCE_SOURCE
            )
            if authored_by:
                q = q.filter(
                    RootCauseNote.correlated["authored_by"].as_string().contains(authored_by)
                )
            if since_dt is not None:
                q = q.filter(RootCauseNote.created_at >= since_dt)
            rootcause = [
                _row_to_dict(n)
                for n in q.order_by(RootCauseNote.created_at.desc()).limit(limit_applied).all()
            ]

        if kind in ("compliance_gap", "all") and limit_applied:
            q = s.query(ProposedAction).filter(ProposedAction.agent == MANUAL_PROVENANCE_SOURCE)
            if authored_by:
                q = q.filter(
                    ProposedAction.payload["authored_by"].as_string().contains(authored_by)
                )
            if since_dt is not None:
                q = q.filter(ProposedAction.created_at >= since_dt)
            compliance_gap = [
                _row_to_dict(a)
                for a in q.order_by(ProposedAction.created_at.desc()).limit(limit_applied).all()
            ]

    return {
        "kind": kind,
        "rootcause": rootcause,
        "compliance_gap": compliance_gap,
        "total": len(rootcause) + len(compliance_gap),
        "limit_requested": limit_requested,
        "limit_applied": limit_applied,
        "limit_clamped": limit_requested > _MANUAL_WRITES_LIMIT_CAP,
    }


# Shared by record_rootcause_note AND record_rootcause_notes_bulk (Phase 2.3,
# 2026-07-29 implementation plan §4.3) so the validation/marker/banner/scrub
# code path exists in exactly ONE place — the bulk tool is deliberately "this
# tool run N times", not a second, divergent implementation.


def _validate_rootcause_note_input(
    drift_event_id: str, explanation: str, correlated: dict | None
) -> tuple[uuid.UUID | None, dict | None]:
    """Validate one note's shape: UUID first, then free-text/correlated bounds.

    Returns ``(parsed_uuid, None)`` on success or ``(None, error_dict)`` on the
    first failure — order matches ``record_rootcause_note``'s original checks.
    """
    try:
        deid = uuid.UUID(drift_event_id)
    except (ValueError, TypeError):
        return None, {"error": "drift_event_id must be a UUID"}
    err = _check_free_text("explanation", explanation) or _check_correlated(correlated)
    if err is not None:
        return None, err
    return deid, None


def _build_rootcause_note(
    deid: uuid.UUID, explanation: str, correlated: dict | None, author: str
) -> RootCauseNote:
    """Construct (but do not add/commit) a manually-authored RootCauseNote row.

    Marker keys are written LAST — a caller-supplied ``source``/``authored_by``
    in ``correlated`` can never disguise a manual write as agent output. Caller
    data is PAN-scrubbed first; the markers themselves are server-generated and
    not scrubbed.
    """
    payload = _redact_deep(dict(correlated or {}))
    payload.update(
        {
            "source": MANUAL_PROVENANCE_SOURCE,
            "authored_by": author,
            "recorded_at": _now_utc().isoformat(),
        }
    )
    return RootCauseNote(
        drift_event_id=deid,
        explanation=_manual_banner(author) + redact_pans_preserving_uuids(explanation.strip()),
        correlated=payload,
    )


@mcp.tool
def record_rootcause_note(
    drift_event_id: str,
    explanation: str,
    author_label: str | None = None,
    correlated: dict | None = None,
) -> dict:
    """Manually record a root-cause explanation for one drift event.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS like every other
    mutating tool here. Writes ONE row to infra-brain's own
    ``root_cause_notes`` table and nothing else: no external system is
    contacted, no managed infrastructure is touched, and no agent execution is
    triggered.

    This is NOT RootCauseAgent output. Every row written here is marked as
    manual: ``correlated`` always ends up containing
    ``{"source": "manual_mcp", "authored_by": <authored_by>, "recorded_at":
    <iso8601>}`` (those keys are written last, so caller-supplied ``correlated``
    data can never mask them), and ``explanation`` is prefixed with
    ``[MANUAL/MCP-authored by <authored_by>]``.

    ATTRIBUTION IS NOT CALLER-CONTROLLED: ``authored_by`` is derived from the
    authenticated API key (``mcp:<key name>``), so a caller cannot write
    ``authored_by="RootCauseAgent"``. ``author_label`` is an OPTIONAL free-text
    hint appended as a quoted claim and can never forge the identity portion.

    Caller input is bounded: ``explanation`` is capped at 8000 characters and
    ``correlated`` at 16 KiB serialized / 10 levels of nesting; an oversized
    payload is refused before any DB write. Both are PAN-scrubbed
    (``redact_pans``) at write time, since they are re-served later.

    One note per drift event (``uq_rootcause_drift``). RootCauseAgent itself
    only ever writes notes for events that have none — it never overwrites an
    existing note — so this tool mirrors that exactly: if a note already
    exists it writes NOTHING and returns an error naming the existing note.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    deid, err = _validate_rootcause_note_input(drift_event_id, explanation, correlated)
    if err is not None:
        return err

    author = _attributed_author(author_label)
    with get_session() as s:
        drift = s.get(DriftEvent, deid)
        if drift is None:
            return {"error": f"DriftEvent {drift_event_id} not found"}

        existing = s.query(RootCauseNote).filter(RootCauseNote.drift_event_id == deid).first()
        if existing is not None:
            return {
                "error": (
                    f"a RootCauseNote already exists for drift event {drift_event_id} "
                    "(one note per event); nothing was written"
                ),
                "existing_note_id": str(existing.id),
            }

        note = _build_rootcause_note(deid, explanation, correlated, author)
        s.add(note)
        s.commit()
        s.refresh(note)
        return {
            "note_id": str(note.id),
            "drift_event_id": drift_event_id,
            "source": MANUAL_PROVENANCE_SOURCE,
            "authored_by": author,
        }


class NoteItem(BaseModel):
    """One item of a ``record_rootcause_notes_bulk`` call.

    Same fields as ``record_rootcause_note``'s individual arguments, typed via
    Pydantic so FastMCP publishes a real per-item JSON schema instead of an
    untyped dict.
    """

    drift_event_id: str
    explanation: str
    author_label: str | None = None
    correlated: dict | None = None


# Hard cap on notes-per-call (Phase 2.3, 2026-07-29 implementation plan §4.3).
# An oversized list is refused WHOLE, before any DB work — split the batch.
_BULK_NOTES_MAX_ITEMS = 100


def _coerce_note_item(raw: NoteItem | dict) -> NoteItem:
    """Accept either a real ``NoteItem`` (FastMCP JSON-Schema dispatch) or a
    plain dict (direct in-process/test calls, which bypass that coercion)."""
    if isinstance(raw, NoteItem):
        return raw
    return NoteItem.model_validate(raw)


@mcp.tool
def record_rootcause_notes_bulk(notes: list[NoteItem], dry_run: bool = True) -> dict:
    """Bulk-record root-cause explanations for many drift events. Preview by default.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS like every other
    mutating tool here. This is ``record_rootcause_note`` run N times: it
    shares that tool's EXACT validation/marker/banner/scrub code path (the
    ``_validate_rootcause_note_input``/``_build_rootcause_note`` helpers
    above), so provenance is identical — ``correlated["source"] ==
    "manual_mcp"``, ``authored_by`` derived from the authenticated key, marker
    keys written last, ``redact_pans`` on all caller text, one note per event
    enforced by ``uq_rootcause_drift``. It adds no new write KIND, only a new
    write CADENCE — the legitimate bulk path (TRK-247(b)) that removes the
    reason to reach for ``docker exec``.

    HARD CAP: at most 100 items per call. A call with MORE than 100 items is
    refused WHOLE, before any DB work — split the batch instead.

    ``dry_run=True`` (THE DEFAULT) validates every item — UUID shape, the
    drift event exists, no existing note, free-text/``correlated`` size caps —
    and returns a per-item verdict (``valid`` / ``skipped`` / ``error``)
    without writing anything.

    ``dry_run=False`` executes. Each item is applied inside its OWN SAVEPOINT
    (``session.begin_nested()``): one item failing — e.g. a concurrent note
    for the same drift event landing between a dry-run preview and the real
    call — rolls back ONLY that item; every other item in the same call is
    unaffected. (A duplicate *within* the same batch, targeting the same
    drift event twice, is caught by the plain "does a note exist" check
    against this session's own pending state — no exception needed — and
    reported as ``skipped``, same as a pre-existing note.) The response's
    ``results`` list reports a per-item ``written`` / ``skipped`` / ``error``
    outcome so partial success is always explicit, never silent; the
    top-level ``written`` / ``skipped`` / ``errors`` counts are the totals.

    Attribution (``authored_by``) is derived from the authenticated key, same
    as ``record_rootcause_note`` — ``author_label`` is only ever an optional
    quoted hint per item, never the identity itself.

    Writes: rows in ``root_cause_notes`` only. Like ``record_rootcause_note``,
    it cannot touch ``drift_events`` status, cannot create proposals, cannot
    reach any external system, and cannot execute anything.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()

    if len(notes) > _BULK_NOTES_MAX_ITEMS:
        return {
            "error": (
                f"notes has {len(notes)} items, over the {_BULK_NOTES_MAX_ITEMS}-per-call "
                "cap; split the batch. Nothing was checked or written."
            )
        }

    try:
        items = [_coerce_note_item(n) for n in notes]
    except Exception as exc:  # malformed item shape (e.g. pydantic ValidationError)
        return {"error": f"notes contains a malformed item: {_clamp_error(exc)}"}

    results: list[dict] = []
    written = skipped = errored = 0
    written_event_ids: list[str] = []

    # Resolve the caller's identity ONCE for the whole batch, not per item.
    # _attributed_author() calls _caller_identity(), which re-queries
    # McpApiKey per call and falls back to the unauthenticated sentinel on
    # any lookup failure -- calling it per-item let a mid-batch key
    # revocation or transient DB hiccup silently split one call's attribution
    # across two different identities with no error surfaced (lc-safety-
    # reviewer finding). One identity, composed per item with each item's own
    # optional label.
    _identity = _caller_identity()

    def _author(label: str | None) -> str:
        text = (label or "").strip()
        if not text:
            return _identity[:_AUTHOR_MAX_LEN]
        return f"{_identity} (says: {text[:_AUTHOR_LABEL_MAX_LEN]})"[:_AUTHOR_MAX_LEN]

    # Dry-run preview must dedupe within the batch itself, not just against
    # pre-existing notes -- two items naming the same drift_event_id both
    # reported "valid" (lc-safety-reviewer finding), telling an operator N
    # will be written when fewer will be.
    _dry_run_seen: set[str] = set()

    with get_session() as s:
        for item in items:
            author = _author(item.author_label)
            deid, err = _validate_rootcause_note_input(
                item.drift_event_id, item.explanation, item.correlated
            )
            if err is not None:
                errored += 1
                results.append({"drift_event_id": item.drift_event_id, "status": "error", **err})
                continue

            if dry_run:
                drift = s.get(DriftEvent, deid)
                if drift is None:
                    errored += 1
                    results.append(
                        {
                            "drift_event_id": item.drift_event_id,
                            "status": "error",
                            "error": f"DriftEvent {item.drift_event_id} not found",
                        }
                    )
                    continue
                existing = (
                    s.query(RootCauseNote).filter(RootCauseNote.drift_event_id == deid).first()
                )
                if existing is not None:
                    skipped += 1
                    results.append(
                        {
                            "drift_event_id": item.drift_event_id,
                            "status": "skipped",
                            "error": (
                                f"a RootCauseNote already exists for drift event "
                                f"{item.drift_event_id} (one note per event); nothing "
                                "would be written"
                            ),
                            "existing_note_id": str(existing.id),
                        }
                    )
                    continue
                if deid in _dry_run_seen:
                    # Two items in THIS batch target the same drift event --
                    # only the first would actually be written on execute
                    # (the second hits the same pre-existing-note check
                    # above, since dry_run=False writes are sequential and
                    # this session would see its own prior flush). Report
                    # the second occurrence as skipped rather than valid, so
                    # the preview's "would be written" count matches reality.
                    skipped += 1
                    results.append(
                        {
                            "drift_event_id": item.drift_event_id,
                            "status": "skipped",
                            "error": (
                                "duplicate drift_event_id within this batch; only "
                                "the first occurrence would be written"
                            ),
                        }
                    )
                    continue
                _dry_run_seen.add(deid)
                results.append(
                    {
                        "drift_event_id": item.drift_event_id,
                        "status": "valid",
                        "would_be_authored_by": author,
                    }
                )
                continue

            # dry_run=False: one SAVEPOINT per item so a single failure (e.g. a
            # concurrent duplicate note) rolls back only this item. A
            # within-batch duplicate targeting the same drift event is caught
            # by the plain existence check below (this session already sees
            # its own prior flush) — no exception, no rollback needed, just a
            # clean "skipped".
            note_id: str | None = None
            existing_note_id: str | None = None
            not_found = False
            try:
                with s.begin_nested():
                    drift = s.get(DriftEvent, deid)
                    if drift is None:
                        not_found = True
                    else:
                        existing = (
                            s.query(RootCauseNote)
                            .filter(RootCauseNote.drift_event_id == deid)
                            .first()
                        )
                        if existing is not None:
                            existing_note_id = str(existing.id)
                        else:
                            note = _build_rootcause_note(
                                deid, item.explanation, item.correlated, author
                            )
                            s.add(note)
                            s.flush()
                            note_id = str(note.id)
            except Exception as exc:  # per-item savepoint: isolate, never drop silently
                logger.warning(
                    "record_rootcause_notes_bulk: item %s failed",
                    item.drift_event_id,
                    exc_info=True,
                )
                errored += 1
                results.append(
                    {
                        "drift_event_id": item.drift_event_id,
                        "status": "error",
                        "error": _clamp_error(exc),
                    }
                )
                continue

            if not_found:
                errored += 1
                results.append(
                    {
                        "drift_event_id": item.drift_event_id,
                        "status": "error",
                        "error": f"DriftEvent {item.drift_event_id} not found",
                    }
                )
            elif existing_note_id is not None:
                skipped += 1
                results.append(
                    {
                        "drift_event_id": item.drift_event_id,
                        "status": "skipped",
                        "error": (
                            f"a RootCauseNote already exists for drift event "
                            f"{item.drift_event_id} (one note per event); nothing was written"
                        ),
                        "existing_note_id": existing_note_id,
                    }
                )
            else:
                written += 1
                written_event_ids.append(item.drift_event_id)
                results.append(
                    {
                        "drift_event_id": item.drift_event_id,
                        "status": "written",
                        "note_id": note_id,
                        "authored_by": author,
                        "source": MANUAL_PROVENANCE_SOURCE,
                    }
                )

        if not dry_run:
            # Dedicated closure-audit row carrying the item count and
            # written/skipped/error tallies -- McpAuditMiddleware's
            # args_summary alone is capped at 2000 bytes (mcp_audit_middleware
            # _ARGS_SUMMARY_MAX_LEN), so a 100-item call was previously
            # truncating to roughly its first item with no total visible.
            # This closes the same TRK-247 in-process-invocation-bypass gap
            # resolve_drift_events/close_compliance_violations already
            # address via _record_closure_audit, committed in the SAME
            # transaction as the notes themselves so a failure here rolls
            # back the whole batch rather than leaving unaudited writes.
            _record_closure_audit(
                s,
                "record_rootcause_notes_bulk",
                {
                    "action": "record_rootcause_notes_bulk",
                    "total": len(items),
                    "written": written,
                    "skipped": skipped,
                    "errors": errored,
                    "written_drift_event_ids": written_event_ids,
                },
            )
        # dry_run=True writes nothing above, so committing here would be a
        # no-op today -- but guarding it keeps that a structural guarantee
        # (a preview can never write) rather than an incidental one.
        if not dry_run:
            s.commit()

    return {
        "dry_run": dry_run,
        "total": len(items),
        "written": written,
        "skipped": skipped,
        "errors": errored,
        "results": results,
    }


@mcp.tool
def record_compliance_gap(
    rule_domain: str,
    condition_type: str,
    description: str,
    author_label: str | None = None,
) -> dict:
    """Manually propose a compliance rule GAP (a check the system does not do).

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS like every other
    mutating tool here. Writes ONE inert ``proposed_actions`` row in
    infra-brain's own database, in exactly the shape ComplianceAgent's
    gap-finder uses (``action_type='compliance_rule_gap'``,
    ``target='rule-gap:<stable hash of rule_domain+condition_type>'``,
    ``confidence=0.5``, ``status='pending'``). No external system is contacted
    and nothing is executed: the remediation executor filters to
    ``("config_fix", "vuln_patch", "eol_migration")`` (agents/remediation.py),
    and ``compliance_rule_gap`` is none of those — so the row is propose-only
    by construction even once approved.

    NOTE: it does NOT write a ``ComplianceViolation`` — violations are produced
    only by the deterministic rules-as-code pass. This records a proposed *new
    rule*, which is what the gap-finder actually emits.

    This is NOT ComplianceAgent output. Provenance is marked three ways:
    ``agent='manual_mcp'`` (the agent column, not "compliance"), the JSONB
    ``payload`` carries ``source``/``authored_by``, and the payload's
    ``description`` prose is prefixed with
    ``[MANUAL/MCP-authored by <authored_by>]``.

    ATTRIBUTION IS NOT CALLER-CONTROLLED: ``authored_by`` is derived from the
    authenticated API key (``mcp:<key name>``). ``author_label`` is an OPTIONAL
    free-text hint appended as a quoted claim and can never forge the identity
    portion. ``description`` is capped at 8000 characters and PAN-scrubbed at
    write time, since it is re-served later.

    Idempotent the same way the gap-finder is: if a row already exists for the
    same (rule_domain, condition_type) target in ANY live status — pending,
    approved, executed, or rejected (rejected included deliberately, so an
    operator-rejected gap is never re-proposed) — nothing is written.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    for name, value in (
        ("rule_domain", rule_domain),
        ("condition_type", condition_type),
        ("description", description),
    ):
        err = _check_free_text(name, value)
        if err is not None:
            return err

    from infra_brain.agents.compliance import (
        _GAP_PROPOSAL_LIVE_STATUSES,
        _stable_gap_hash,
    )

    author = _attributed_author(author_label)
    target = f"rule-gap:{_stable_gap_hash(rule_domain, condition_type)}"

    with get_session() as s:
        existing = (
            s.query(ProposedAction)
            .filter(
                ProposedAction.action_type == "compliance_rule_gap",
                ProposedAction.target == target,
                ProposedAction.status.in_(_GAP_PROPOSAL_LIVE_STATUSES),
            )
            .first()
        )
        if existing is not None:
            return {
                "skipped": True,
                "reason": (
                    f"a compliance_rule_gap proposal already exists for target {target} "
                    f"with status '{existing.status}'; nothing was written"
                ),
                "existing_action_id": str(existing.id),
                "target": target,
            }

        action = ProposedAction(
            id=uuid.uuid4(),
            agent=MANUAL_PROVENANCE_SOURCE,
            action_type="compliance_rule_gap",
            target=target,
            payload={
                "rule_domain": rule_domain,
                "condition_type": condition_type,
                "description": _manual_banner(author) + redact_pans_preserving_uuids(description.strip()),
                "source": MANUAL_PROVENANCE_SOURCE,
                "authored_by": author,
            },
            confidence=0.5,
            status="pending",
            created_at=_now_utc(),
        )
        s.add(action)
        s.commit()
        s.refresh(action)
        return {
            "action_id": str(action.id),
            "target": target,
            "status": "pending",
            "source": MANUAL_PROVENANCE_SOURCE,
            "authored_by": author,
        }


# ── Batch L: audited, predicate-scoped batch closure (GitLab #144) ────────────
# Before this batch the finding queues could only grow: nothing in the MCP
# surface touched drift_events.status or compliance_violations.status lifecycle
# state, so 63,704 open drift events and 45,020 open violations (many of them
# artifacts of the write-path defects in #137/#142) had no removal path short of
# direct DB surgery on the deployed host — which CI reverts on the next deploy
# and which has already caused one outage.
#
# The two tools below are the sanctioned replacement. Design constraints, all of
# them load-bearing (see docs/decisions/2026-07-29-ADR-mcp-batch-closure.md):
#
#   1. A PREDICATE IS MANDATORY. There is deliberately no way to express "close
#      everything open" — `_require_predicate` refuses a call whose only filter
#      is `from_status`, because an unscoped clear would erase real findings
#      alongside artifacts and leave an audit trail showing a clean fleet that
#      was never verified. `from_status` is NOT a qualifying predicate; neither
#      is an empty `event_ids`/`violation_ids` list.
#   2. dry_run=True IS THE DEFAULT (same as every other batch write here). The
#      preview reports matched_total, the bounded selection, what the cap hid,
#      how many selected drift events already carry a RootCauseNote (i.e. were
#      investigated by a human/agent — closing those is usually a mistake), and
#      for compliance the exact tombstone rows a flip would DELETE.
#   3. A HARD PER-CALL CAP (_CLOSURE_BATCH_CAP) bounds blast radius. Over-cap
#      matches are not refused outright — that would make a 63k class
#      permanently unclearable — they are truncated deterministically (oldest
#      first) and `remaining` is reported, so clearing a large known-bad class
#      is ~N bounded, individually audited calls rather than one unbounded one.
#   4. THE AUDIT ROW IS IN THE SAME TRANSACTION AS THE FLIPS. Unlike
#      McpAuditMiddleware (best-effort by design, and HTTP-scoped so a direct
#      in-process invocation bypasses it — TRK-247), `_record_closure_audit`
#      writes an AgentActionLog row inside the tool body and inside the same
#      commit: if the audit write fails the closure rolls back. There is no
#      such thing as an unrecorded batch closure, even under direct invocation.
#      Read it back with `get_agent_activity(agent="manual_mcp")`.
#   5. ATTRIBUTION IS SERVER-DERIVED (`_caller_identity()`), never taken from a
#      caller string — identical to approve_proposal/record_rootcause_note.
#
# Both write ONLY to infra-brain's own Postgres. Neither can reach GitLab/Jira/
# Confluence and neither triggers any execution path: a resolved drift event is
# read by digest/fleet counters and made retention-eligible, nothing more.
#
# WHY THE REASON LIVES IN THE AUDIT ROW AND NOT ON THE FINDING ROW: neither
# drift_events nor compliance_violations has a resolution-reason column, and
# adding one is a db/models/ schema change (Critical Files: migration +
# /pg-gate-check + lc-migration-reviewer) that this batch deliberately does not
# make. The audit location is also strictly more durable — retention reaps
# non-open drift_events at retention_drift_events_days (180) while
# agent_action_log is kept retention_agent_action_log_days (400), so the record
# of WHY a row was closed outlives the row itself. The accepted trade-off: you
# cannot SQL-filter the finding tables by reason, only reconstruct a batch from
# its audit row's recorded id list. See the ADR for the deferred column.

# The single closed state both tools write. Deliberately the EXISTING vocabulary
# value ("resolved", already produced by agents/drift.py and agents/compliance.py)
# rather than a new one: every reader — retention.py's `status != "open"`,
# digest.py/fleet.py's open counters, the governance routes' status filter — keeps
# working unchanged, and no reader has to learn a new state to be correct.
CLOSURE_STATUS = "resolved"

# The reason vocabulary. The issue's requirement is that "resolved because fixed"
# and "closed as never-valid-data" be DISTINGUISHABLE in the audit trail; this
# enum is how. It is closed (an unknown value is refused) so the audit trail
# stays aggregatable instead of accumulating free-text synonyms — `note` is
# where per-batch prose goes.
CLOSURE_REASONS: dict[str, str] = {
    "fixed": "the underlying condition was verified remediated",
    "never_valid": "a data artifact; this was never a real finding",
    "wont_fix": "a real finding whose risk is explicitly accepted",
    "superseded": "replaced by a newer, more accurate finding",
}

# Hard per-call cap on rows touched. Sized so a 63k class is ~128 audited calls.
_CLOSURE_BATCH_CAP = 500
# Cap on an explicit id list, checked BEFORE any DB work (refused whole).
_CLOSURE_MAX_IDS = _CLOSURE_BATCH_CAP
# agent_action_log.agent value for closure records — the same marker the manual
# write tools use, so get_agent_activity(agent="manual_mcp") is the one place to
# look for everything that entered by hand.
_CLOSURE_AUDIT_AGENT = MANUAL_PROVENANCE_SOURCE
# args_summary is Text; this bounds a pathological payload without truncating a
# normal full-cap id list (500 UUIDs ~= 19 KiB).
_CLOSURE_AUDIT_MAX_LEN = 65536
_CLOSURE_PREVIEW_SAMPLE = 10

# ── auto_continue ceilings (GitLab #166) ────────────────────────────────────
# _CLOSURE_BATCH_CAP is deliberately NOT raised (see 3. above): one 63k-row
# transaction is strictly worse than 128 bounded ones — a longer lock, a bigger
# rollback, and an all-or-nothing audit row. What #166 actually needs is not a
# bigger transaction but the ability to make the N bounded calls WITHOUT N
# round-trips. `auto_continue=True` loops the identical capped+audited
# transaction server-side, one audit row per iteration, so every per-transaction
# safety property above is preserved verbatim and only the round-trip count
# changes. These two ceilings bound the loop itself: without them a predicate
# matching a table that another writer keeps refilling would never terminate.
_AUTO_CONTINUE_MAX_ITERATIONS = 50
_AUTO_CONTINUE_MAX_ROWS = 25_000

# The status vocabulary both finding tables actually use: agents write "open",
# NotificationAgent flips to "acknowledged" (agents/notification.py) and the
# incident-ack webhook writes either. `from_status` is validated against this
# CLOSED set rather than being passed through, for two reasons:
#   * an empty/unrecognized value must not silently mean "no status filter" —
#     that would widen a predicate to every row of every status, which is the
#     unscoped bulk close that _require_predicate exists to prevent; and
#   * it keeps a typo (`from_status="opne"`) a refusal instead of a silent
#     zero-match "success" that looks like the queue was already clean.
#
# CLOSURE_STATUS itself is deliberately NOT in the set: "close the already-closed"
# is a no-op flip that would still write an audit row, and excluding it also
# removes the from_status == CLOSURE_STATUS tombstone edge case entirely.
_CLOSURE_FROM_STATUSES = ("open", "acknowledged")


def _check_from_status(from_status: str) -> dict | None:
    if from_status not in _CLOSURE_FROM_STATUSES:
        return {
            "error": (
                f"from_status must be one of {list(_CLOSURE_FROM_STATUSES)}; "
                f"got {from_status!r}. An unfiltered status is not accepted — it would "
                "widen the predicate to every row regardless of state. Nothing was changed."
            )
        }
    return None


# LIKE metacharacters. A prefix predicate is only a NARROWING predicate if the
# caller's text is matched literally: unescaped, `field_prefix="%"` (or "_", or
# any value containing them) would expand to "match everything", turning the
# mandatory-predicate check into a formality and permitting exactly the
# unscoped bulk close #144 says must not be expressible. Escaped, a prefix can
# only ever narrow.
_LIKE_ESCAPE = "\\"


def _like_prefix(raw: str) -> str:
    """Return a LIKE pattern matching *raw* literally as a prefix.

    Use with ``.like(pattern, escape=_LIKE_ESCAPE)``. The backslash replacement
    must come first or it would double-escape the escapes added after it.
    """
    escaped = raw.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
    for meta in ("%", "_"):
        escaped = escaped.replace(meta, _LIKE_ESCAPE + meta)
    return f"{escaped}%"


def _check_resolution(resolution: str) -> dict | None:
    """Refuse anything outside CLOSURE_REASONS. No default — the caller must say why."""
    if resolution not in CLOSURE_REASONS:
        return {
            "error": (
                f"resolution must be one of {sorted(CLOSURE_REASONS)}; got {resolution!r}. "
                "Nothing was changed."
            ),
            "resolution_meanings": CLOSURE_REASONS,
        }
    return None


def _require_predicate(qualifying: dict[str, Any]) -> dict | None:
    """Refuse a call with no narrowing filter — the core #144 safety property.

    *qualifying* maps parameter name -> whether that parameter narrows the
    result set. ``from_status`` is never passed in here: filtering to "open" is
    not scoping, it IS the whole queue.
    """
    if any(qualifying.values()):
        return None
    return {
        "error": (
            "a narrowing predicate is REQUIRED: pass an explicit id list or at least one of "
            f"{sorted(qualifying)}. Filtering by status alone is not a predicate — an "
            "unscoped bulk close would erase real findings alongside artifacts, so it is "
            "not expressible by this tool. Nothing was changed."
        )
    }


def _parse_ts(name: str, raw: str) -> tuple[datetime | None, dict | None]:
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None, {"error": f"{name} must be an ISO-8601 timestamp; got {raw!r}"}
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed, None


def _parse_id_list(name: str, raw: list[str]) -> tuple[list[uuid.UUID] | None, dict | None]:
    """Validate an explicit id list whole — one bad UUID refuses the call."""
    if len(raw) > _CLOSURE_MAX_IDS:
        return None, {
            "error": (
                f"{name} has {len(raw)} entries, over the {_CLOSURE_MAX_IDS}-per-call cap; "
                "split the batch. Nothing was changed."
            )
        }
    out: list[uuid.UUID] = []
    for item in raw:
        try:
            out.append(uuid.UUID(str(item)))
        except (ValueError, TypeError):
            return None, {
                "error": f"{name} contains a non-UUID entry {item!r}; nothing was changed"
            }
    return out, None


def _clamp_error(exc: BaseException) -> str:
    """Render a per-row failure as a bounded, driver-dump-free string.

    NEVER report a raw ``str(exc)`` for a DB error: SQLAlchemy's DBAPIError
    subclasses (IntegrityError etc.) append ``[SQL: ...] [parameters: ...]`` to
    their message on real PostgreSQL, i.e. the whole statement plus every bound
    parameter — which for these tables means violation ``detail``/``host`` text
    verbatim. That string reaches the tool's return value (not scrubbed at all)
    and the audit row (``redact_pans`` only, so a non-PAN secret pasted into a
    detail field would survive). The type name plus a clamped prefix is enough to
    classify the failure; the full traceback is already in the logs.
    """
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _record_closure_audit(session, tool: str, record: dict) -> uuid.UUID:
    """Write the batch-closure audit row IN THE CALLER'S TRANSACTION.

    Not best-effort on purpose (contrast McpAuditMiddleware): the caller commits
    this row together with the status flips, so a failure here rolls the closure
    back rather than leaving unattributed state changes behind. PAN-scrubbed
    because it carries operator free text.

    RETURNS the new ``agent_action_log.id`` — the BATCH ID (GitLab #166). It was
    previously minted and discarded, which left a caller with no handle on the
    row recording its own batch: reconstructing what a call closed meant
    scanning ``get_agent_activity(agent="manual_mcp")`` by timestamp and hoping
    no concurrent batch interleaved. Surfacing it makes the audit row addressable
    (``resolved_ids``/``closed_ids`` inside it reconstruct the exact batch), and
    is what lets ``auto_continue`` report one id per iteration.

    SCRUBBED WITH ``redact_pans_preserving_uuids``, NOT ``redact_pans`` (TRK-346):
    ``_PAN_RE`` treats ``-`` as a digit-group separator, so a UUID whose first
    three groups (8+4+4 = 16 characters) are all-digits, Luhn-valid and
    IIN-matching gets its middle rewritten as ``****-REDACTED-****``. That is
    ~1 in 40,000 random ids — enough that a 1,200-id ``auto_continue`` batch was
    corrupted ~3% of the time, and a corrupted id list defeats the entire point
    of this row. Operator free text in the same blob is still scrubbed verbatim;
    only complete UUID tokens are exempt, and a PAN cannot be one.
    """
    audit_id = uuid.uuid4()
    session.add(
        AgentActionLog(
            id=audit_id,
            agent=_CLOSURE_AUDIT_AGENT,
            domain="mcp",
            tool=tool,
            args_summary=redact_pans_preserving_uuids(json.dumps(record, default=str))[
                :_CLOSURE_AUDIT_MAX_LEN
            ],
            verdict="allow",
            status="ok",
            ts=_now_utc(),
        )
    )
    return audit_id


# TRK-258 (2): pg_stat_user_tables.n_live_tup for proposed_actions was found
# 11x stale (1,784 vs an actual 20,161 rows) after a 12,948-row bulk status
# flip -- autovacuum's own analyze-scale-factor threshold (10% of the table +
# a small base) can lag well behind a single large bulk decide/close call, so
# the planner runs on stale cardinality estimates until the next autoanalyze
# eventually catches up. FIX AT THE SOURCE rather than a one-time ANALYZE:
# every bulk-write tool that can flip a large slice of a table in one
# transaction (bulk_reject_proposals / bulk_approve_proposals below, and
# resolve_drift_events' _resolve_one_page above) calls this immediately after
# its own commit whenever the transaction actually flipped more than
# _BULK_ANALYZE_THRESHOLD rows. close_compliance_violations shares the same
# per-call cap but was NOT wired in here: its table is small in every observed
# environment and it wasn't implicated in the original 11x-stale finding --
# add it the same way if that ever changes.
_BULK_ANALYZE_THRESHOLD = 200

# Precompiled, allow-listed ANALYZE statements -- deliberately NOT an f-string
# over a caller-influenced table name (this project's raw-SQL rule requires
# every raw statement to reference only real, hardcoded column/table names).
_BULK_ANALYZE_STATEMENTS: dict[str, Any] = {
    "proposed_actions": text("ANALYZE proposed_actions"),
    "drift_events": text("ANALYZE drift_events"),
}


def _maybe_analyze_after_bulk_write(session, table: str, flipped: int) -> None:
    """Best-effort ``ANALYZE <table>`` right after a large bulk status flip.

    Postgres-only: skipped entirely on sqlite (the full test suite's dialect),
    where ANALYZE has different semantics and pg_stat_user_tables-style
    staleness doesn't apply. Never raises -- a failed or skipped ANALYZE just
    means the planner stays stale a little longer, which is the pre-existing
    condition this call exists to shorten, not a reason to fail a bulk decision
    that has already committed successfully.

    Explicitly committed: this runs AFTER the caller's own ``session.commit()``
    for the status flips, in a fresh (auto-begun) transaction on the same
    session. ``get_session()``'s context manager only closes the session on
    exit -- it does not commit -- so an uncommitted ANALYZE here would be
    silently discarded rather than landing in pg_statistic.
    """
    if flipped <= _BULK_ANALYZE_THRESHOLD:
        return
    stmt = _BULK_ANALYZE_STATEMENTS.get(table)
    if stmt is None:
        return
    try:
        if session.bind.dialect.name != "postgresql":
            return
        session.execute(stmt)
        session.commit()
    except Exception:
        logger.warning(
            "bulk write: ANALYZE %s failed after flipping %d rows", table, flipped, exc_info=True
        )


@mcp.tool
def resolve_drift_events(
    resolution: str,
    event_ids: list[str] | None = None,
    domain: str | None = None,
    field: str | None = None,
    field_prefix: str | None = None,
    drift_type: str | None = None,
    unlinked_only: bool = False,
    detected_before: str | None = None,
    detected_after: str | None = None,
    from_status: str = "open",
    note: str | None = None,
    dry_run: bool = True,
    limit: int = _CLOSURE_BATCH_CAP,
    auto_continue: bool = False,
) -> dict:
    """Batch-resolve drift events matching an EXPLICIT predicate. Preview by default.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS like every other
    mutating tool here. Flips ``drift_events.status`` to ``"resolved"`` for the
    matched rows and writes one ``agent_action_log`` closure record in the same
    transaction. Nothing else: no external system is contacted, no managed
    infrastructure is touched, no agent execution is triggered, and no row is
    deleted (a resolved event simply becomes retention-eligible, like one
    DriftAgent itself resolved).

    A NARROWING PREDICATE IS MANDATORY. Pass ``event_ids`` (an explicit list) or
    at least one of ``domain``, ``field``, ``field_prefix``, ``drift_type``,
    ``unlinked_only=True``, ``detected_before``, ``detected_after``.
    ``from_status`` alone is REFUSED — "everything open" is not a predicate, and
    an unscoped clear is not expressible by this tool by design. ``field_prefix``
    matches LITERALLY (``%``/``_`` are escaped, so it can only ever narrow) and
    must be non-blank. ``from_status`` must be ``open`` or ``acknowledged``; any
    other value — including an empty string — is refused rather than treated as
    "no status filter".

    ``resolution`` is required and must be one of ``fixed`` (verified
    remediated), ``never_valid`` (a data artifact, never a real finding),
    ``wont_fix`` (real, risk accepted), ``superseded`` (replaced by a newer
    finding). All four land on ``status="resolved"``; the distinction lives in
    the audit record, which is what makes "we fixed it" and "that data was
    junk" tellable apart later. ``note`` is optional prose (8000 chars,
    PAN-scrubbed) recorded alongside it.

    ``unlinked_only=True`` selects ``collection_run_id IS NULL`` — the shape of
    the #137 retirement rows and of events whose run was reaped by retention.

    COUNT WITHOUT MUTATING is the DEFAULT, not an extra mode: ``dry_run=True``
    changes nothing and returns the full plan — ``matched_total`` (how many rows
    the predicate hits, uncapped), ``selected``/``would_resolve`` (how many this
    call would actually flip, bounded by the cap), ``remaining`` (the rest),
    ``truncated``, ``with_root_cause_note`` (selected events that were already
    investigated — a non-zero value here is usually a sign the predicate is too
    broad), and a small ``sample``. Re-call with ``dry_run=False`` to execute.

    At most 500 rows per TRANSACTION (``limit`` is clamped to
    ``_CLOSURE_BATCH_CAP``). This bound is on the transaction, not on what you
    can clear: an over-cap match is truncated oldest-first and deterministically
    — never refused — so repeated calls with the same predicate make monotonic
    progress through the class, and ``remaining`` tells you how many are left.

    ``auto_continue=True`` (GitLab #166) makes those repeat calls for you,
    SERVER-SIDE: the same capped, individually-audited transaction is looped
    until ``remaining`` reaches 0, and the result reports ``batch_ids`` (one per
    iteration), ``iterations``, the cumulative ``resolved``, and
    ``stopped_because``. Each iteration is still its own transaction with its own
    ``agent_action_log`` row — the cap is NOT raised and no single giant
    transaction is ever opened, so every safety property above holds unchanged.
    The loop itself is bounded by 50
    iterations / 25,000 rows,
    whichever comes first; hitting either stops cleanly with ``remaining``
    reported and ``stopped_because`` saying which ceiling bit, so a
    predicate a concurrent writer keeps refilling can never spin forever. With
    ``dry_run=True`` it does NOT loop (there would be nothing to make progress
    against) — it previews one page and adds ``projected_iterations``.

    Every executed transaction returns a ``batch_id``: the ``agent_action_log.id``
    of the audit row written alongside the flips. That row's ``resolved_ids``
    reconstructs the exact batch. Attribution is derived from the authenticated
    key and cannot be supplied by the caller.

    Inspect what was closed with ``get_agent_activity(agent="manual_mcp")``.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    err = _check_resolution(resolution) or _check_from_status(from_status)
    if err is not None:
        return err
    for _name, _value in (("note", note), ("field_prefix", field_prefix)):
        if _value is not None:
            err = _check_free_text(_name, _value)
            if err is not None:
                return err

    ids: list[uuid.UUID] | None = None
    if event_ids:
        ids, err = _parse_id_list("event_ids", event_ids)
        if err is not None:
            return err
    before = after = None
    if detected_before:
        before, err = _parse_ts("detected_before", detected_before)
        if err is not None:
            return err
    if detected_after:
        after, err = _parse_ts("detected_after", detected_after)
        if err is not None:
            return err

    err = _require_predicate(
        {
            "event_ids": bool(ids),
            "domain": bool(domain),
            "field": bool(field),
            "field_prefix": bool(field_prefix),
            "drift_type": bool(drift_type),
            "unlinked_only": bool(unlinked_only),
            "detected_before": before is not None,
            "detected_after": after is not None,
        }
    )
    if err is not None:
        return err

    limit = max(1, min(int(limit), _CLOSURE_BATCH_CAP))
    actor = _caller_identity()
    predicate = {
        "event_ids": [str(i) for i in ids] if ids else None,
        "domain": domain,
        "field": field,
        "field_prefix": field_prefix,
        "drift_type": drift_type,
        "unlinked_only": unlinked_only,
        "detected_before": detected_before,
        "detected_after": detected_after,
        "from_status": from_status,
    }

    def _resolve_one_page() -> dict:
        """One capped, individually-audited transaction. THE unit of work.

        ``auto_continue`` calls this repeatedly rather than widening it: the
        transaction boundary, the cap, the audit row and the oldest-first
        determinism are all inside here, so looping cannot weaken any of them.
        """
        with get_session() as s:
            q = s.query(DriftEvent).join(Resource, Resource.id == DriftEvent.resource_id)
            q = q.filter(DriftEvent.status == from_status)
            if ids:
                q = q.filter(DriftEvent.id.in_(ids))
            if domain:
                q = q.filter(Resource.domain == domain)
            if field:
                q = q.filter(DriftEvent.field == field)
            if field_prefix:
                q = q.filter(DriftEvent.field.like(_like_prefix(field_prefix), escape=_LIKE_ESCAPE))
            if drift_type:
                q = q.filter(DriftEvent.drift_type == drift_type)
            if unlinked_only:
                q = q.filter(DriftEvent.collection_run_id.is_(None))
            if before is not None:
                q = q.filter(DriftEvent.detected_at < before)
            if after is not None:
                q = q.filter(DriftEvent.detected_at >= after)

            matched_total = q.count()
            # Deterministic, oldest-first so repeated capped calls make progress and
            # two callers previewing the same predicate see the same selection.
            selected = (
                q.order_by(DriftEvent.detected_at.asc(), DriftEvent.id.asc()).limit(limit).all()
            )
            selected_ids = [e.id for e in selected]
            with_note = (
                s.query(func.count(RootCauseNote.id))
                .filter(RootCauseNote.drift_event_id.in_(selected_ids))
                .scalar()
                or 0
                if selected_ids
                else 0
            )
            sample = [
                {
                    "id": str(e.id),
                    "field": e.field,
                    "drift_type": e.drift_type,
                    "detected_at": e.detected_at.isoformat() if e.detected_at else None,
                    "collection_run_id": str(e.collection_run_id) if e.collection_run_id else None,
                }
                for e in selected[:_CLOSURE_PREVIEW_SAMPLE]
            ]
            result = {
                "dry_run": dry_run,
                "resolution": resolution,
                "resolution_meaning": CLOSURE_REASONS[resolution],
                "note": note,
                "resolved_by": actor,
                "predicate": predicate,
                "matched_total": matched_total,
                "selected": len(selected_ids),
                "remaining": max(0, matched_total - len(selected_ids)),
                "truncated": matched_total > len(selected_ids),
                "with_root_cause_note": int(with_note),
                "cap": limit,
                "sample": sample,
            }
            if dry_run:
                result["would_resolve"] = len(selected_ids)
                result["hint"] = "re-call with dry_run=False to apply; nothing was changed"
                return result
            if not selected_ids:
                result["resolved"] = 0
                result["hint"] = "predicate matched nothing; nothing was changed"
                return result

            # drift_events has no unique constraint involving status, so a plain bulk
            # UPDATE is safe here (unlike compliance_violations below).
            s.query(DriftEvent).filter(DriftEvent.id.in_(selected_ids)).update(
                {DriftEvent.status: CLOSURE_STATUS}, synchronize_session=False
            )
            audit = dict(result)
            audit.pop("sample", None)
            audit.update(
                {
                    "action": "resolve_drift_events",
                    "to_status": CLOSURE_STATUS,
                    "resolved_ids": [str(i) for i in selected_ids],
                }
            )
            batch_id = _record_closure_audit(s, "resolve_drift_events", audit)
            s.commit()
            _maybe_analyze_after_bulk_write(s, "drift_events", len(selected_ids))
            result["resolved"] = len(selected_ids)
            result["to_status"] = CLOSURE_STATUS
            # Committed together with the flips above, so this id always names a
            # row that really exists (#166).
            result["batch_id"] = str(batch_id)
            return result

    if not auto_continue:
        return _resolve_one_page()

    first = _resolve_one_page()
    if dry_run:
        # Deliberately does NOT loop: a dry run flips nothing, so every
        # iteration would re-select the identical oldest-first page forever.
        # Project the work instead.
        per_page = max(1, first.get("selected") or limit)
        matched = first.get("matched_total", 0)
        capped = min(matched, _AUTO_CONTINUE_MAX_ROWS)
        first["auto_continue"] = True
        first["projected_iterations"] = min(_AUTO_CONTINUE_MAX_ITERATIONS, -(-capped // per_page))
        first["would_resolve_total"] = capped
        first["hint"] = (
            "re-call with dry_run=False to apply; auto_continue does not loop in "
            "dry-run (nothing is flipped, so there is no progress to make). "
            "Nothing was changed."
        )
        return first

    batch_ids: list[str] = [first["batch_id"]] if first.get("batch_id") else []
    total = int(first.get("resolved", 0) or 0)
    page = first
    iterations = 1
    stopped = "complete" if page.get("remaining", 0) <= 0 else None
    while stopped is None:
        if iterations >= _AUTO_CONTINUE_MAX_ITERATIONS:
            stopped = "iteration_ceiling"
            break
        if total >= _AUTO_CONTINUE_MAX_ROWS:
            stopped = "row_ceiling"
            break
        page = _resolve_one_page()
        iterations += 1
        if page.get("batch_id"):
            batch_ids.append(page["batch_id"])
        flipped = int(page.get("resolved", 0) or 0)
        total += flipped
        if flipped == 0:
            # Defensive: `remaining` said there was more but nothing moved
            # (a concurrent writer, or a row the predicate can see but the
            # UPDATE cannot). Stop rather than spin.
            stopped = "no_progress"
        elif page.get("remaining", 0) <= 0:
            stopped = "complete"

    return {
        "dry_run": False,
        "auto_continue": True,
        "resolution": resolution,
        "resolution_meaning": CLOSURE_REASONS[resolution],
        "note": note,
        "resolved_by": actor,
        "predicate": predicate,
        "to_status": CLOSURE_STATUS,
        "matched_total": first.get("matched_total", 0),
        "resolved": total,
        "remaining": page.get("remaining", 0),
        "iterations": iterations,
        "batch_ids": batch_ids,
        "cap": limit,
        "stopped_because": stopped,
        "sample": first.get("sample", []),
        "hint": (
            "every batch_id is an agent_action_log row; its resolved_ids "
            "reconstructs that iteration's batch"
        ),
    }


@mcp.tool
def close_compliance_violations(
    resolution: str,
    violation_ids: list[str] | None = None,
    rule: str | None = None,
    rule_prefix: str | None = None,
    host: str | None = None,
    severity: str | None = None,
    detected_before: str | None = None,
    detected_after: str | None = None,
    from_status: str = "open",
    note: str | None = None,
    dry_run: bool = True,
    limit: int = _CLOSURE_BATCH_CAP,
) -> dict:
    """Batch-close compliance violations matching an EXPLICIT predicate. Preview by default.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS like every other
    mutating tool here. Flips ``compliance_violations.status`` to ``"resolved"``
    for the matched rows and writes one ``agent_action_log`` closure record in
    the same transaction. Writes only to infra-brain's own database; no external
    system is contacted and nothing is executed.

    A NARROWING PREDICATE IS MANDATORY — ``violation_ids`` or at least one of
    ``rule``, ``rule_prefix``, ``host``, ``severity``, ``detected_before``,
    ``detected_after``. ``from_status`` alone is REFUSED, same as
    ``resolve_drift_events``; ``rule_prefix`` likewise matches LITERALLY
    (``%``/``_`` escaped) and must be non-blank, and ``from_status`` must be
    ``open`` or ``acknowledged``.

    ``resolution`` is required, from the same four-value vocabulary
    (``fixed`` / ``never_valid`` / ``wont_fix`` / ``superseded``); the reason
    and optional ``note`` are recorded in the audit row.

    THIS TOOL CAN DELETE ROWS, and that is not avoidable:
    ``uq_compliance_rule_host_status`` permits one row per
    (rule, host, status), so flipping an open violation to ``resolved`` collides
    with any stale ``resolved`` tombstone left by an earlier
    resolve→reopen→resolve cycle. ComplianceAgent already handles this by
    dropping the stale tombstone first (latest resolved row wins — history is a
    single tombstone by design) and this tool reproduces that OUTCOME rather than
    inventing a second, divergent one. It does not copy compliance.py's
    all-deletes-before-any-flip ordering: see the comment on the per-row loop for
    why interleaving is safe here. Every such deletion is
    previewed in ``dry_run`` as ``tombstones_to_delete`` and recorded in the
    audit row as ``tombstones_deleted``.

    ``dry_run=True`` (THE DEFAULT) changes nothing and returns the plan:
    ``matched_total``, ``would_close``, ``remaining``, ``tombstones_to_delete``,
    and a small ``sample``. Re-call with ``dry_run=False`` to execute.

    At most 500 rows per call (``limit`` is clamped); over-cap matches are
    truncated oldest-first, never refused, and ``remaining`` reports the rest.
    Each row is flipped inside its own SAVEPOINT, so one row colliding
    concurrently reports an ``error`` for that row alone instead of losing the
    whole batch — per-row outcomes are always explicit, never silent.

    An executed call returns a ``batch_id``: the ``agent_action_log.id`` of the
    audit row committed alongside the flips (#166). That row's ``closed_ids``
    and ``tombstones_deleted`` reconstruct the exact batch.

    Inspect what was closed with ``get_agent_activity(agent="manual_mcp")``.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    err = _check_resolution(resolution) or _check_from_status(from_status)
    if err is not None:
        return err
    for _name, _value in (("note", note), ("rule_prefix", rule_prefix)):
        if _value is not None:
            err = _check_free_text(_name, _value)
            if err is not None:
                return err

    ids: list[uuid.UUID] | None = None
    if violation_ids:
        ids, err = _parse_id_list("violation_ids", violation_ids)
        if err is not None:
            return err
    before = after = None
    if detected_before:
        before, err = _parse_ts("detected_before", detected_before)
        if err is not None:
            return err
    if detected_after:
        after, err = _parse_ts("detected_after", detected_after)
        if err is not None:
            return err

    err = _require_predicate(
        {
            "violation_ids": bool(ids),
            "rule": bool(rule),
            "rule_prefix": bool(rule_prefix),
            "host": bool(host),
            "severity": bool(severity),
            "detected_before": before is not None,
            "detected_after": after is not None,
        }
    )
    if err is not None:
        return err

    limit = max(1, min(int(limit), _CLOSURE_BATCH_CAP))
    actor = _caller_identity()
    predicate = {
        "violation_ids": [str(i) for i in ids] if ids else None,
        "rule": rule,
        "rule_prefix": rule_prefix,
        "host": host,
        "severity": severity,
        "detected_before": detected_before,
        "detected_after": detected_after,
        "from_status": from_status,
    }

    with get_session() as s:
        q = s.query(ComplianceViolation)
        q = q.filter(ComplianceViolation.status == from_status)
        if ids:
            q = q.filter(ComplianceViolation.id.in_(ids))
        if rule:
            q = q.filter(ComplianceViolation.rule == rule)
        if rule_prefix:
            q = q.filter(
                ComplianceViolation.rule.like(_like_prefix(rule_prefix), escape=_LIKE_ESCAPE)
            )
        if host:
            q = q.filter(ComplianceViolation.host == host)
        if severity:
            q = q.filter(ComplianceViolation.severity == severity)
        if before is not None:
            q = q.filter(ComplianceViolation.detected_at < before)
        if after is not None:
            q = q.filter(ComplianceViolation.detected_at >= after)

        matched_total = q.count()
        selected = (
            q.order_by(ComplianceViolation.detected_at.asc(), ComplianceViolation.id.asc())
            .limit(limit)
            .all()
        )
        selected_ids = [v.id for v in selected]

        # Tombstones the flip will have to remove (see the uq_ note in the
        # docstring). Computed for BOTH dry-run and execute so the preview is
        # honest about the destructive part.
        tombstones: list[dict] = []
        if selected and from_status != CLOSURE_STATUS:
            pairs = {(v.rule, v.host) for v in selected}
            for t_rule, t_host in sorted(pairs):
                for tomb in (
                    s.query(ComplianceViolation)
                    .filter(
                        ComplianceViolation.rule == t_rule,
                        ComplianceViolation.host == t_host,
                        ComplianceViolation.status == CLOSURE_STATUS,
                    )
                    .all()
                ):
                    if tomb.id in selected_ids:
                        continue
                    tombstones.append({"id": str(tomb.id), "rule": tomb.rule, "host": tomb.host})

        sample = [
            {
                "id": str(v.id),
                "rule": v.rule,
                "host": v.host,
                "severity": v.severity,
                "detected_at": v.detected_at.isoformat() if v.detected_at else None,
            }
            for v in selected[:_CLOSURE_PREVIEW_SAMPLE]
        ]
        result = {
            "dry_run": dry_run,
            "resolution": resolution,
            "resolution_meaning": CLOSURE_REASONS[resolution],
            "note": note,
            "closed_by": actor,
            "predicate": predicate,
            "matched_total": matched_total,
            "selected": len(selected_ids),
            "remaining": max(0, matched_total - len(selected_ids)),
            "truncated": matched_total > len(selected_ids),
            "cap": limit,
            "sample": sample,
        }
        if dry_run:
            result["would_close"] = len(selected_ids)
            result["tombstones_to_delete"] = tombstones
            result["hint"] = "re-call with dry_run=False to apply; nothing was changed"
            return result
        if not selected_ids:
            result["closed"] = 0
            result["tombstones_deleted"] = []
            result["hint"] = "predicate matched nothing; nothing was changed"
            return result

        tombstone_ids = {uuid.UUID(t["id"]) for t in tombstones}
        deleted: list[dict] = []
        closed_ids: list[str] = []
        errors: list[dict] = []
        for v in selected:
            # Staged per row and merged into `deleted` only AFTER this row's
            # SAVEPOINT commits. Appending directly would be a false claim: if
            # the flip below raises, begin_nested() rolls the DELETE back and the
            # tombstone still exists, but the id would already be in the response
            # and in the audit row's `tombstones_deleted`. Same discipline as
            # closed_ids.append() at the bottom of the loop.
            row_deleted: list[dict] = []
            row_claimed: set[uuid.UUID] = set()
            try:
                # WHY DELETE-THEN-FLIP INTERLEAVED PER ROW IS SAFE HERE, even
                # though agents/compliance.py:408-424 deliberately does ALL
                # deletes before ANY flip: that code's ordering exists to stop
                # autoflush pushing a flipped row into a collision with a
                # tombstone it has not deleted yet. That cannot happen here.
                # uq_compliance_rule_host_status is (rule, host, status) and
                # every row in `selected` shares one from_status, so the
                # constraint permits at most ONE selected row per (rule, host) —
                # no other selected row can collide with this row's flip, so
                # there is no cross-row autoflush hazard for the ordering
                # discipline to protect against. The per-row SAVEPOINT is what
                # matters instead: it keeps each (delete, flip) pair atomic so a
                # concurrent writer's collision costs one row, not the batch.
                with s.begin_nested():
                    for tomb in (
                        s.query(ComplianceViolation)
                        .filter(
                            ComplianceViolation.rule == v.rule,
                            ComplianceViolation.host == v.host,
                            ComplianceViolation.status == CLOSURE_STATUS,
                            ComplianceViolation.id.in_(tombstone_ids),
                        )
                        .all()
                    ):
                        row_deleted.append(
                            {"id": str(tomb.id), "rule": tomb.rule, "host": tomb.host}
                        )
                        row_claimed.add(tomb.id)
                        s.delete(tomb)
                    s.flush()
                    v.status = CLOSURE_STATUS
                    s.flush()
            except Exception as exc:  # per-row savepoint: isolate, never silently drop
                logger.warning("close_compliance_violations: row %s failed", v.id, exc_info=True)
                errors.append({"id": str(v.id), "error": _clamp_error(exc)})
                continue
            deleted.extend(row_deleted)
            tombstone_ids -= row_claimed
            closed_ids.append(str(v.id))

        audit = dict(result)
        audit.pop("sample", None)
        audit.update(
            {
                "action": "close_compliance_violations",
                "to_status": CLOSURE_STATUS,
                "closed_ids": closed_ids,
                "tombstones_deleted": deleted,
                "errors": errors,
            }
        )
        batch_id = _record_closure_audit(s, "close_compliance_violations", audit)
        s.commit()
        result["closed"] = len(closed_ids)
        result["to_status"] = CLOSURE_STATUS
        result["tombstones_deleted"] = deleted
        result["errors"] = errors
        # Committed with the flips, so it always names a real row (#166).
        result["batch_id"] = str(batch_id)
        return result


# ── Bulk approve/reject for ProposedAction (GitLab #161) ─────────────────────
#
# get_remediation_suggestions(status="pending") returns 7,471 rows, and
# approve_proposal/reject_proposal are one-at-a-time. That is not a queue an
# operator can triage; it is a queue that gets ignored, which is worse than an
# empty one because the genuinely-actionable proposals are buried in it.
#
# A large share of the queue is one identifiable class: RemediationAgent drafts
# a `config_fix` against a SCANNER-DERIVED metric field (Rapid7
# risk_score/vulnerabilities — agents/remediation.py DERIVED_METRIC_FIELDS) and
# already scores it 0.35 instead of 0.8, precisely because "revert this scan
# result" is a category error rather than a real remediation. So
# `agent="RemediationAgent" AND action_type="config_fix" AND max_confidence=0.5`
# isolates that class exactly, and `payload_field` narrows it further per field.
#
# These two tools reuse Batch L's machinery verbatim (_require_predicate,
# _check_free_text, _parse_id_list, _parse_ts, _like_prefix, _CLOSURE_BATCH_CAP,
# _record_closure_audit + #166's batch_id) rather than growing a parallel one,
# so every property #144 established holds here too:
#
#   1. A NARROWING PREDICATE IS MANDATORY — `status` is always "pending" and is
#      never a predicate, exactly as `from_status` is not one above. "Reject the
#      whole queue" is not expressible.
#   2. dry_run=True IS THE DEFAULT.
#   3. THE SAME PER-CALL CAP bounds blast radius; over-cap matches are truncated
#      oldest-first, and `remaining` reports the rest.
#   4. THE AUDIT ROW IS IN THE SAME TRANSACTION AS THE FLIPS.
#   5. ATTRIBUTION IS SERVER-DERIVED.
#
# Two things are specific to this table and are NOT optional:
#
# * uq_proposed_action_target_status is (action_type, target, status), so
#   flipping N pending rows to "rejected" can collide with an already-rejected
#   tombstone sharing (action_type, target) — a plain bulk UPDATE would fail the
#   WHOLE batch on one collision. Each item therefore runs in its own SAVEPOINT
#   (same discipline as close_compliance_violations), and unlike the compliance
#   path this one does NOT resolve the collision by deleting the tombstone: an
#   already-rejected proposal is a decision that was already made, so the
#   colliding item is SKIPPED and reported, never overwritten. dry_run predicts
#   the collisions up front.
#
# * entity_resolution_same_as rows are HARD-REFUSED and can never be selected,
#   no matter what the rest of the predicate says. approve_action already
#   refuses them individually (approving one permanently closes an identity
#   question with no SAME_AS edge ever written and no way to recover), and a
#   bulk path that could sweep them up would be a loophole around that guard.
#   The exclusion is applied to the QUERY, not just to the caller's argument, so
#   it holds for every predicate shape.

_PROPOSAL_PENDING_STATUS = "pending"
_PROPOSAL_REJECTED_STATUS = "rejected"
_PROPOSAL_APPROVED_STATUS = "approved"


def _bulk_proposal_query(
    session,
    *,
    ids: list[uuid.UUID] | None,
    agent: str | None,
    action_type: str | None,
    target_prefix: str | None,
    payload_field: str | None,
    max_confidence: float | None,
    before: datetime | None,
    after: datetime | None,
):
    """The shared selection for both bulk tools. Pending-only, review-rows excluded."""
    from infra_brain.graph_phase3 import REVIEW_ACTION_TYPE

    q = session.query(ProposedAction).filter(
        ProposedAction.status == _PROPOSAL_PENDING_STATUS,
        # HARD, unconditional. Not a filter the caller can turn off, and applied
        # to the query rather than only to the action_type argument so no
        # predicate shape can reach these rows.
        ProposedAction.action_type != REVIEW_ACTION_TYPE,
    )
    if ids:
        q = q.filter(ProposedAction.id.in_(ids))
    if agent:
        q = q.filter(ProposedAction.agent == agent)
    if action_type:
        q = q.filter(ProposedAction.action_type == action_type)
    if target_prefix:
        q = q.filter(ProposedAction.target.like(_like_prefix(target_prefix), escape=_LIKE_ESCAPE))
    if payload_field:
        # payload is JSONB on PostgreSQL / JSON on SQLite (db/models/_base.py
        # with_variant); as_string() renders ->> on PG and json_extract on
        # SQLite. Same construct already used by get_manual_writes.
        q = q.filter(ProposedAction.payload["field"].as_string() == payload_field)
    if max_confidence is not None:
        q = q.filter(ProposedAction.confidence < max_confidence)
    if before is not None:
        q = q.filter(ProposedAction.created_at < before)
    if after is not None:
        q = q.filter(ProposedAction.created_at >= after)
    return q


def _bulk_proposal_prepare(
    *,
    decision: str,
    action_ids: list[str] | None,
    agent: str | None,
    action_type: str | None,
    target_prefix: str | None,
    payload_field: str | None,
    max_confidence: float | None,
    created_before: str | None,
    created_after: str | None,
    note: str | None,
    limit: int,
) -> tuple[dict | None, dict]:
    """Validate + normalise every argument BEFORE any DB work.

    Returns ``(error, context)``. Identical guard ordering to the Batch L tools:
    a bad argument refuses the whole call and writes nothing.
    """
    from infra_brain.graph_phase3 import REVIEW_ACTION_TYPE

    ctx: dict = {}
    if action_type == REVIEW_ACTION_TYPE:
        return {
            "error": (
                f"{REVIEW_ACTION_TYPE} rows cannot be {decision} in bulk — they are "
                "identity-review rows whose only sanctioned path is "
                "POST /api/graph/entity-resolution/{action_id}/confirm. Bulk-deciding "
                "one would permanently close the identity question with no SAME_AS "
                "edge ever written and no way to recover. Nothing was changed."
            )
        }, ctx

    for _name, _value in (("note", note), ("target_prefix", target_prefix)):
        if _value is not None:
            err = _check_free_text(_name, _value)
            if err is not None:
                return err, ctx

    ids: list[uuid.UUID] | None = None
    if action_ids:
        ids, err = _parse_id_list("action_ids", action_ids)
        if err is not None:
            return err, ctx

    before = after = None
    if created_before:
        before, err = _parse_ts("created_before", created_before)
        if err is not None:
            return err, ctx
    if created_after:
        after, err = _parse_ts("created_after", created_after)
        if err is not None:
            return err, ctx

    if max_confidence is not None:
        try:
            max_confidence = float(max_confidence)
        except (TypeError, ValueError):
            return {"error": f"max_confidence must be a number; got {max_confidence!r}"}, ctx

    err = _require_predicate(
        {
            "action_ids": bool(ids),
            "agent": bool(agent),
            "action_type": bool(action_type),
            "target_prefix": bool(target_prefix),
            "payload_field": bool(payload_field),
            "max_confidence": max_confidence is not None,
            "created_before": before is not None,
            "created_after": after is not None,
        }
    )
    if err is not None:
        return err, ctx

    ctx["ids"] = ids
    ctx["before"] = before
    ctx["after"] = after
    ctx["max_confidence"] = max_confidence
    ctx["limit"] = max(1, min(int(limit), _CLOSURE_BATCH_CAP))
    ctx["predicate"] = {
        "action_ids": [str(i) for i in ids] if ids else None,
        "agent": agent,
        "action_type": action_type,
        "target_prefix": target_prefix,
        "payload_field": payload_field,
        "max_confidence": max_confidence,
        "created_before": created_before,
        "created_after": created_after,
        "status": _PROPOSAL_PENDING_STATUS,
    }
    return None, ctx


def _tombstone_collisions(session, selected, to_status: str) -> dict[uuid.UUID, str]:
    """Map selected-row id -> the colliding tombstone id, for *to_status*.

    uq_proposed_action_target_status is (action_type, target, status): a row
    flipping to *to_status* collides with any EXISTING row already in that
    status sharing (action_type, target). Computed for BOTH dry-run and execute
    so the preview is honest about what will be skipped.
    """
    if not selected:
        return {}
    pairs = {(a.action_type, a.target) for a in selected}
    existing: dict[tuple[str, str], uuid.UUID] = {}
    for row in (
        session.query(ProposedAction)
        .filter(
            ProposedAction.status == to_status,
            tuple_(ProposedAction.action_type, ProposedAction.target).in_(sorted(pairs)),
        )
        .all()
    ):
        existing.setdefault((row.action_type, row.target), row.id)
    return {
        a.id: str(existing[(a.action_type, a.target)])
        for a in selected
        if (a.action_type, a.target) in existing
    }


def _bulk_proposal_result(
    *,
    decision: str,
    ctx: dict,
    actor: str,
    note: str | None,
    dry_run: bool,
    matched_total: int,
    selected,
    collisions: dict[uuid.UUID, str],
) -> dict:
    return {
        "dry_run": dry_run,
        "decision": decision,
        "note": note,
        "decided_by": actor,
        "predicate": ctx["predicate"],
        "matched_total": matched_total,
        "selected": len(selected),
        "remaining": max(0, matched_total - len(selected)),
        "truncated": matched_total > len(selected),
        "cap": ctx["limit"],
        "tombstone_collisions": [
            {"action_id": str(aid), "conflicts_with": tid} for aid, tid in collisions.items()
        ],
        "sample": [
            {
                "id": str(a.id),
                "agent": a.agent,
                "action_type": a.action_type,
                "target": a.target,
                "confidence": a.confidence,
                "field": (a.payload or {}).get("field"),
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in selected[:_CLOSURE_PREVIEW_SAMPLE]
        ],
    }


@mcp.tool
def bulk_reject_proposals(
    action_ids: list[str] | None = None,
    agent: str | None = None,
    action_type: str | None = None,
    target_prefix: str | None = None,
    payload_field: str | None = None,
    max_confidence: float | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    note: str | None = None,
    dry_run: bool = True,
    limit: int = _CLOSURE_BATCH_CAP,
) -> dict:
    """Bulk-reject pending ProposedActions matching an EXPLICIT predicate. Preview by default.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS. Flips
    ``proposed_actions.status`` to ``"rejected"`` for matched rows and writes one
    ``agent_action_log`` record in the same transaction. Nothing external is
    contacted and nothing is executed: a rejected proposal is simply never run.
    Per-row guards are shared VERBATIM with the dashboard route
    (``action_decisions.reject_action``), so the bulk surface can never be more
    permissive than the single-row one.

    A NARROWING PREDICATE IS MANDATORY — pass ``action_ids`` or at least one of
    ``agent``, ``action_type``, ``target_prefix``, ``payload_field``,
    ``max_confidence``, ``created_before``, ``created_after``. "Everything
    pending" is NOT a predicate and is refused: the 7k queue contains real
    proposals alongside the junk, and an unscoped reject would bury them
    together. ``target_prefix`` matches LITERALLY (``%``/``_`` escaped, so it can
    only ever narrow).

    ``entity_resolution_same_as`` rows are NEVER selectable, whatever the
    predicate — they have their own confirm path
    (``POST /api/graph/entity-resolution/{action_id}/confirm``) and bulk-deciding
    one would close an identity question irrecoverably.

    THE INTENDED USE, concretely: RemediationAgent drafts ``config_fix``
    proposals against scanner-DERIVED metric fields (``risk_score`` /
    ``vulnerabilities``) and already self-scores them 0.35 rather than 0.8
    because "revert this scan result" is a category error, not a remediation.
    ``agent="RemediationAgent", action_type="config_fix", max_confidence=0.5``
    isolates exactly that class; add ``payload_field="risk_score"`` to go
    field-by-field.

    TOMBSTONE COLLISIONS ARE SKIPPED, NOT OVERWRITTEN.
    ``uq_proposed_action_target_status`` is (action_type, target, status), so a
    pending row can collide with an already-``rejected`` row for the same
    (action_type, target). Each item runs in its own SAVEPOINT, so a collision
    costs that ONE item and the rest of the batch still commits — and because an
    already-rejected proposal is a decision someone already made, the colliding
    item is skipped and reported rather than having its tombstone deleted.
    ``dry_run`` lists them in ``tombstone_collisions`` before you commit.

    ``dry_run=True`` (THE DEFAULT) changes nothing: ``matched_total``,
    ``selected``, ``remaining``, ``tombstone_collisions``, ``sample``. At most
    500 rows per call; over-cap matches are truncated oldest-first, never
    refused, and ``remaining`` reports the rest. An executed call returns
    ``batch_id`` — the ``agent_action_log.id`` whose ``rejected_ids``
    reconstructs the batch. Attribution is server-derived.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()

    err, ctx = _bulk_proposal_prepare(
        decision="rejected",
        action_ids=action_ids,
        agent=agent,
        action_type=action_type,
        target_prefix=target_prefix,
        payload_field=payload_field,
        max_confidence=max_confidence,
        created_before=created_before,
        created_after=created_after,
        note=note,
        limit=limit,
    )
    if err is not None:
        return err

    from infra_brain.action_decisions import ActionDecisionError, reject_action

    actor = _caller_identity()
    with get_session() as s:
        q = _bulk_proposal_query(
            s,
            ids=ctx["ids"],
            agent=agent,
            action_type=action_type,
            target_prefix=target_prefix,
            payload_field=payload_field,
            max_confidence=ctx["max_confidence"],
            before=ctx["before"],
            after=ctx["after"],
        )
        matched_total = q.count()
        selected = (
            q.order_by(ProposedAction.created_at.asc(), ProposedAction.id.asc())
            .limit(ctx["limit"])
            .all()
        )
        collisions = _tombstone_collisions(s, selected, _PROPOSAL_REJECTED_STATUS)
        result = _bulk_proposal_result(
            decision="rejected",
            ctx=ctx,
            actor=actor,
            note=note,
            dry_run=dry_run,
            matched_total=matched_total,
            selected=selected,
            collisions=collisions,
        )
        if dry_run:
            result["would_reject"] = len(selected) - len(collisions)
            result["hint"] = "re-call with dry_run=False to apply; nothing was changed"
            return result
        if not selected:
            result["rejected"] = 0
            result["hint"] = "predicate matched nothing; nothing was changed"
            return result

        rejected_ids: list[str] = []
        skipped: list[dict] = []
        for a in selected:
            aid = a.id
            try:
                # One SAVEPOINT per item: a unique-constraint collision (or any
                # per-row guard failure) costs this item alone, never the batch.
                # commit=False keeps the flip inside OUR transaction so the audit
                # row below commits atomically with it.
                with s.begin_nested():
                    reject_action(s, aid, commit=False)
                    s.flush()
            except ActionDecisionError as exc:
                skipped.append({"id": str(aid), "reason": exc.detail})
                continue
            except Exception as exc:
                logger.warning("bulk_reject_proposals: row %s failed", aid, exc_info=True)
                skipped.append({"id": str(aid), "reason": _clamp_error(exc)})
                continue
            rejected_ids.append(str(aid))

        audit = dict(result)
        audit.pop("sample", None)
        audit.update(
            {
                "action": "bulk_reject_proposals",
                "to_status": _PROPOSAL_REJECTED_STATUS,
                "rejected_ids": rejected_ids,
                "skipped": skipped,
            }
        )
        batch_id = _record_closure_audit(s, "bulk_reject_proposals", audit)
        s.commit()
        _maybe_analyze_after_bulk_write(s, "proposed_actions", len(rejected_ids))
        result["rejected"] = len(rejected_ids)
        result["to_status"] = _PROPOSAL_REJECTED_STATUS
        result["skipped"] = skipped
        result["batch_id"] = str(batch_id)
        return result


@mcp.tool
def bulk_approve_proposals(
    action_ids: list[str] | None = None,
    agent: str | None = None,
    action_type: str | None = None,
    target_prefix: str | None = None,
    payload_field: str | None = None,
    max_confidence: float | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    approver_label: str | None = None,
    note: str | None = None,
    dry_run: bool = True,
    limit: int = _CLOSURE_BATCH_CAP,
) -> dict:
    """Bulk-approve pending ProposedActions matching an EXPLICIT predicate. Preview by default.

    MUTATING — gated by INFRA_BRAIN_MCP_ENABLE_MUTATIONS. Same predicate set,
    mandatory-narrowing rule, cap, per-item SAVEPOINT, in-transaction audit row
    and ``batch_id`` as ``bulk_reject_proposals``; read that docstring for those.
    This one only differs where approval differs.

    APPROVAL IS THE HUMAN GATE IN FRONT OF A SANCTIONED EXTERNAL WRITE, so its
    per-item guards are the strictly stricter set and are shared VERBATIM with
    the dashboard route (``action_decisions.approve_action``). In particular
    ``confidence < 0.7`` is REFUSED per item — which means this tool cannot be
    used on the low-confidence derived-metric class that
    ``bulk_reject_proposals`` exists for. That is deliberate: the bulk path must
    not be a way to approve in batch what the single-row path refuses one at a
    time. ``entity_resolution_same_as`` rows are likewise never selectable.

    Refused items are reported in ``skipped`` with their reason and are NOT
    counted as approved; the rest of the batch still commits.

    ``approver_label`` is an OPTIONAL free-text hint appended as a quoted claim
    (``mcp:<key name> (says: <label>)``). The identity portion is always derived
    from the authenticated key and can never be forged or replaced.

    AFTER THE COMMIT, any parked remediation-interrupt LangGraph for each
    approved action is resumed, one at a time. Those resumes happen strictly
    after the transaction commits, and each failure is NON-fatal to the batch —
    the row is already flipped, so a failed resume only defers that one action's
    execution back to the ``_execute_approved()`` poll. Per-item resume outcomes
    are reported in ``resumed``.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()

    err, ctx = _bulk_proposal_prepare(
        decision="approved",
        action_ids=action_ids,
        agent=agent,
        action_type=action_type,
        target_prefix=target_prefix,
        payload_field=payload_field,
        max_confidence=max_confidence,
        created_before=created_before,
        created_after=created_after,
        note=note,
        limit=limit,
    )
    if err is not None:
        return err
    if approver_label is not None:
        err = _check_free_text("approver_label", approver_label)
        if err is not None:
            return err

    # MIN_APPROVE_CONFIDENCE is IMPORTED, never re-declared: the dry-run preview
    # must predict the SAME floor the per-item approve_action() enforces, or it
    # would overstate what a real call will land.
    from infra_brain.action_decisions import (
        MIN_APPROVE_CONFIDENCE,
        ActionDecisionError,
        approve_action,
    )
    from infra_brain.remediation_graph import resume_remediation_action_sync

    actor = _caller_identity()
    approved_by = _attributed_author(approver_label)
    with get_session() as s:
        q = _bulk_proposal_query(
            s,
            ids=ctx["ids"],
            agent=agent,
            action_type=action_type,
            target_prefix=target_prefix,
            payload_field=payload_field,
            max_confidence=ctx["max_confidence"],
            before=ctx["before"],
            after=ctx["after"],
        )
        matched_total = q.count()
        selected = (
            q.order_by(ProposedAction.created_at.asc(), ProposedAction.id.asc())
            .limit(ctx["limit"])
            .all()
        )
        collisions = _tombstone_collisions(s, selected, _PROPOSAL_APPROVED_STATUS)
        result = _bulk_proposal_result(
            decision="approved",
            ctx=ctx,
            actor=actor,
            note=note,
            dry_run=dry_run,
            matched_total=matched_total,
            selected=selected,
            collisions=collisions,
        )
        result["approved_by"] = approved_by
        # Predicted here too, so a dry run does not overstate what will land.
        below_floor = [a for a in selected if a.confidence < MIN_APPROVE_CONFIDENCE]
        result["below_confidence_floor"] = len(below_floor)
        if dry_run:
            # GitLab #172: a row can be BOTH a tombstone collision and below the
            # confidence floor, so subtracting the two lengths independently
            # double-counts that overlap and can undercount (even go negative).
            # Set-difference on ids counts each excluded row exactly once.
            excluded_ids = set(collisions) | {a.id for a in below_floor}
            result["would_approve"] = len(selected) - len(excluded_ids)
            result["hint"] = (
                "re-call with dry_run=False to apply; nothing was changed. "
                f"Items under confidence {MIN_APPROVE_CONFIDENCE} are refused per-item, "
                "same as the single-row path."
            )
            return result
        if not selected:
            result["approved"] = 0
            result["hint"] = "predicate matched nothing; nothing was changed"
            return result

        approved_ids: list[str] = []
        skipped: list[dict] = []
        snapshots = []
        for a in selected:
            aid = a.id
            try:
                with s.begin_nested():
                    snapshot = approve_action(s, aid, approved_by, commit=False)
                    s.flush()
            except ActionDecisionError as exc:
                skipped.append({"id": str(aid), "reason": exc.detail})
                continue
            except Exception as exc:
                logger.warning("bulk_approve_proposals: row %s failed", aid, exc_info=True)
                skipped.append({"id": str(aid), "reason": _clamp_error(exc)})
                continue
            approved_ids.append(str(aid))
            snapshots.append(snapshot)

        audit = dict(result)
        audit.pop("sample", None)
        audit.update(
            {
                "action": "bulk_approve_proposals",
                "to_status": _PROPOSAL_APPROVED_STATUS,
                "approved_ids": approved_ids,
                "skipped": skipped,
            }
        )
        batch_id = _record_closure_audit(s, "bulk_approve_proposals", audit)
        s.commit()
        _maybe_analyze_after_bulk_write(s, "proposed_actions", len(approved_ids))

    # STRICTLY AFTER THE COMMIT, and never fatal: the rows are already flipped,
    # so a resume failure defers that one action to the _execute_approved()
    # poll instead of undoing an approval that already happened.
    resumed: list[dict] = []
    for snapshot in snapshots:
        try:
            ok = resume_remediation_action_sync(snapshot, approved=True)
        except Exception as exc:
            logger.warning("bulk_approve_proposals: resume %s failed", snapshot.id, exc_info=True)
            resumed.append({"id": str(snapshot.id), "resumed": False, "error": _clamp_error(exc)})
            continue
        resumed.append({"id": str(snapshot.id), "resumed": bool(ok)})

    result["approved"] = len(approved_ids)
    result["to_status"] = _PROPOSAL_APPROVED_STATUS
    result["skipped"] = skipped
    result["resumed"] = resumed
    result["batch_id"] = str(batch_id)
    return result


# ── Phase 4 state backend (convergence plan P4.1-P4.3) ───────────────────────
# Thin @mcp.tool wrappers over the plain functions built in tools/*.py (each
# imported at module top — see the infra_brain.tools.* block above) — the
# underlying module already validates its own inputs; this layer's job is
# only the two things every mutating tool here needs: the _mutations_enabled()
# gate, and deriving attribution server-side via _attributed_author() /
# _caller_identity() rather than trusting a caller-supplied identity string
# (matching approve_proposal's convention above).


@mcp.tool
def record_environment_note(note: str, author_label: str | None = None) -> dict:
    """Record a human-written note about the environment (the narrative layer
    alongside structured resource/drift/eol queries). Attribution is derived
    server-side from the authenticated MCP key, not from a caller-supplied
    string — see promote_instinct's docstring for why."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    author = _attributed_author(author_label)
    try:
        return _record_environment_note(note=note, author=author)
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool
def get_environment_notes(status: str | None = "open", limit: int = 50) -> list[dict]:
    """List environment notes, most recent first. status=None returns all statuses."""
    return _get_environment_notes(status=status, limit=limit)


@mcp.tool
def resolve_environment_note(note_id: str, resolver_label: str | None = None) -> dict:
    """Mark an environment note resolved. Attribution is server-derived."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    resolved_by = _attributed_author(resolver_label)
    try:
        return _resolve_environment_note(note_id=note_id, resolved_by=resolved_by)
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool
def promote_instinct_v2(
    pattern: str,
    domain: str,
    citation: str,
    evidence: str,
    zone: str = "corpor",
    confidence: float = 0.8,
    approver_label: str | None = None,
) -> dict:
    """Promote a new instinct with full version/approval history (P4.3b).

    Unlike ``promote_instinct`` (kept unchanged for existing callers), this
    requires ``evidence`` and writes an InstinctVersion + InstinctApproval
    row alongside the Instinct itself, so ``get_instinct_history`` has a real
    trail from day one. Attribution is server-derived, never a raw argument.
    """
    if not _mutations_enabled():
        return _mutation_disabled_response()
    approved_by = _attributed_author(approver_label)
    try:
        return _promote_instinct_v2(
            pattern=pattern,
            domain=domain,
            citation=citation,
            evidence=evidence,
            approved_by=approved_by,
            zone=zone,
            confidence=confidence,
        )
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool
def rollback_instinct(instinct_id: str, reason: str, approver_label: str | None = None) -> dict:
    """Roll back a promoted instinct (status -> rolled_back), logging why."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    approved_by = _attributed_author(approver_label)
    try:
        return _rollback_instinct(instinct_id=instinct_id, approved_by=approved_by, reason=reason)
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool
def get_instinct_history(instinct_id: str) -> dict:
    """Full version + approval history for one instinct."""
    try:
        return _get_instinct_history(instinct_id=instinct_id)
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool
def propose_instinct(
    zone: str,
    domain: str,
    pattern: str,
    confidence: float,
    evidence: str = "",
    citation: str = "",
    proposer_label: str | None = None,
) -> dict:
    """Draft a proposed instinct (status=pending) for later human approval —
    does not touch the live instincts table. Attribution is server-derived."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    proposed_by = _attributed_author(proposer_label)
    return _propose_instinct(
        zone=zone,
        domain=domain,
        pattern=pattern,
        confidence=confidence,
        proposed_by=proposed_by,
        evidence=evidence,
        citation=citation,
    )


@mcp.tool
def record_client_state(collection: str, entry_id: str, payload: dict, client_id: str) -> dict:
    """Persist one client-local state entry server-side (collection is
    restricted to a fixed allow-list — see tools/client_state.py)."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        return _record_client_state(
            collection=collection, entry_id=entry_id, payload=payload, client_id=client_id
        )
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool
def record_observation(
    client_id: str, agent: str, tool: str, domain: str, event_data: dict | None = None
) -> dict:
    """Record one per-event client observation (distinct from the aggregate
    ``observations`` counter — every call here is its own row)."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        return _record_observation(
            client_id=client_id, agent=agent, tool=tool, domain=domain, event_data=event_data
        )
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool
def record_governance_event(client_id: str, event_type: str, payload: dict | None = None) -> dict:
    """Append one hash-chained governance event (tamper-evident — see
    ``verify_governance_chain``). Never updates or deletes."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        return _record_governance_event(client_id=client_id, event_type=event_type, payload=payload)
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool
def get_governance_events(client_id: str | None = None, limit: int = 100) -> list[dict]:
    """List governance events, most recent first."""
    return _get_governance_events(client_id=client_id, limit=limit)


@mcp.tool
def verify_governance_chain() -> dict:
    """Verify the governance-events hash chain is untampered end to end."""
    return _verify_governance_chain()


@mcp.tool
def ingest_document(
    title: str,
    content_hash: str,
    source: str,
    url: str | None = None,
    external_id: str | None = None,
    ingester_label: str | None = None,
) -> dict:
    """Push a document a client already has in hand (idempotent by
    content_hash). ``client_origin`` is derived from the caller's MCP key
    identity, not a free-form argument."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    identity = _caller_identity()
    try:
        return _ingest_document(
            title=title,
            content_hash=content_hash,
            source=source,
            client_origin=identity,
            ingested_by=_attributed_author(ingester_label),
            url=url,
            external_id=external_id,
        )
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool
def update_document_metadata(
    document_id: str,
    url: str | None = None,
    external_id: str | None = None,
    client_origin: str | None = None,
    ingested_by: str | None = None,
) -> dict:
    """Update url/external_id/client_origin/ingested_by on an existing
    document. Only fields explicitly passed (non-None) are changed."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    fields = {
        k: v
        for k, v in {
            "url": url,
            "external_id": external_id,
            "client_origin": client_origin,
            "ingested_by": ingested_by,
        }.items()
        if v is not None
    }
    try:
        return _update_document_metadata(document_id=document_id, **fields)
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool
def create_gitlab_issue(
    project_id: int, title: str, description: str = "", labels: list[str] | None = None
) -> dict:
    """Open a GitLab issue (idempotent by exact open-issue title match).
    The only sanctioned GitLab issue-creation path in infra-brain (P4.1)."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        return _create_gitlab_issue(
            project_id=project_id, title=title, description=description, labels=labels
        )
    except PermissionError as exc:
        return {"error": str(exc)}


@mcp.tool
def comment_on_gitlab_issue(project_id: int, issue_iid: int, body: str) -> dict:
    """Add a comment to an existing GitLab issue."""
    if not _mutations_enabled():
        return _mutation_disabled_response()
    try:
        return _comment_on_gitlab_issue(project_id=project_id, issue_iid=issue_iid, body=body)
    except PermissionError as exc:
        return {"error": str(exc)}


# ── Entrypoint ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    configure_logging()
    assert_dev_not_in_hardened_env()
    port = int(os.getenv("MCP_PORT", "8002"))
    app = mcp.http_app(middleware=[Middleware(_ApiKeyAuthMiddleware)])

    from infra_brain.mcp_metrics import healthz_endpoint, metrics_endpoint

    app.add_route("/metrics", metrics_endpoint)
    app.add_route("/healthz", healthz_endpoint)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
