"""MCP scoped-API-key management routes (session/admin-gated dashboard CRUD).

NOTE: these routes use the DASHBOARD session auth (require_session/require_admin
from dashboard_auth.py), NOT the MCP bearer auth — two separate, unrelated auth
systems that happen to both gate this one app. The raw token is returned exactly
once on create and never stored raw (only its sha256 hash lives in the DB).
Revoke is soft (sets revoked_at) — no hard delete, to keep the audit trail.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from infra_brain import mcp_auth
from infra_brain.api.schemas import (
    McpKeyCreateBody,
    McpKeyCreateResult,
    McpKeyOut,
    McpKeyPageOut,
    McpKeyRevokeResult,
    McpKeyUpdateBody,
    McpKeyUpdateResult,
    McpToolCatalogOut,
)
from infra_brain.dashboard_auth import current_user, require_admin, require_session
from infra_brain.db.models import McpApiKey
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

mcp_keys_router = APIRouter(
    prefix="/api/dashboard/mcp-keys",
    tags=["mcp-keys"],
    dependencies=[Depends(require_session)],
)


def _key_out(row: McpApiKey) -> McpKeyOut:
    return McpKeyOut(
        id=str(row.id),
        name=row.name,
        created_at=row.created_at,
        created_by=row.created_by or "",
        last_used_at=row.last_used_at,
        revoked=row.revoked_at is not None,
        allowed_tools_count=len(row.allowed_tools or []),
        expires_at=row.expires_at,
        # Computed with the SAME predicate auth uses (mcp_auth.is_expired), not
        # a re-implementation — the list view can never claim a key is usable
        # when lookup_active_key would reject it, or vice versa. "expired" is
        # reported independently of "revoked": a key can be both, and an
        # operator needs to see which one actually happened.
        expired=mcp_auth.is_expired(row.expires_at),
    )


@mcp_keys_router.get("/tools", response_model=McpToolCatalogOut)
def list_tool_catalog():
    """The tool-name catalog the create form's multi-select is built from,
    grouped so the UI can offer 'all read-only' / 'all mutation' toggles."""
    return McpToolCatalogOut(
        readonly=list(mcp_auth.READONLY_TOOL_NAMES),
        mutation=list(mcp_auth.MUTATION_TOOL_NAMES),
        groups={k: list(v) for k, v in mcp_auth.TOOL_GROUPS.items()},
    )


@mcp_keys_router.get("", response_model=McpKeyPageOut)
def list_mcp_keys(_: None = Depends(require_admin)):
    with get_session() as s:
        rows = mcp_auth.list_keys(s)
        items = [_key_out(r) for r in rows]
    return McpKeyPageOut(items=items, total=len(items))


@mcp_keys_router.post("", response_model=McpKeyCreateResult)
def create_mcp_key(body: McpKeyCreateBody, request: Request, _: None = Depends(require_admin)):
    user = current_user(request) or {}
    created_by = user.get("username") or "dashboard"
    with get_session() as s:
        row, raw = mcp_auth.create_key(
            s,
            name=body.name,
            allowed_tools=body.allowed_tools,
            created_by=created_by,
            # None (the default) = never expires; the range is already
            # validated by McpKeyCreateBody, so a bad value 422s before here.
            expires_in_days=body.expires_days,
        )
        s.commit()
        result = McpKeyCreateResult(
            id=str(row.id),
            name=row.name,
            token=raw,
            allowed_tools=list(row.allowed_tools or []),
            expires_at=row.expires_at,
        )
    return result


@mcp_keys_router.post("/{key_id}/revoke", response_model=McpKeyRevokeResult)
def revoke_mcp_key(key_id: str, _: None = Depends(require_admin)):
    try:
        kid = uuid.UUID(key_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="key_id must be a UUID")
    with get_session() as s:
        ok = mcp_auth.revoke_key(s, kid)
        if not ok:
            raise HTTPException(status_code=404, detail="active key not found")
        s.commit()
    return McpKeyRevokeResult(id=key_id, revoked=True)


@mcp_keys_router.patch("/{key_id}", response_model=McpKeyUpdateResult)
def update_mcp_key(
    key_id: str,
    body: McpKeyUpdateBody,
    request: Request,
    _: None = Depends(require_admin),
):
    """Amend a live key's allowed_tools in place. Never touches token_hash or
    re-issues a token. Refuses to amend a revoked key (409). The amendment is
    logged at WARNING (id/name, before/after tool counts, acting admin) as the
    v1 audit trail — a persisted amendment-history table is deferred, see
    docs/decisions/2026-07-29-implementation-plan.md section 4.4."""
    try:
        kid = uuid.UUID(key_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="key_id must be a UUID")
    user = current_user(request) or {}
    acting_admin = user.get("username") or "dashboard"
    with get_session() as s:
        try:
            outcome = mcp_auth.update_allowed_tools(s, kid, body.allowed_tools)
        except ValueError:
            raise HTTPException(status_code=409, detail="cannot amend a revoked key")
        if outcome is None:
            raise HTTPException(status_code=404, detail="key not found")
        row, before_count = outcome
        after_count = len(row.allowed_tools or [])
        s.commit()
        logger.warning(
            "MCP key amended: id=%s name=%s before_tools=%d after_tools=%d amended_by=%s",
            row.id,
            row.name,
            before_count,
            after_count,
            acting_admin,
        )
        result = McpKeyUpdateResult(
            id=str(row.id), name=row.name, allowed_tools=list(row.allowed_tools or [])
        )
    return result
