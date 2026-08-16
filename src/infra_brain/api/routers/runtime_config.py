"""Live runtime configuration CRUD (TRK-303). Dashboard-session-gated (NOT MCP
auth) — mirrors mcp_keys.py's admin-for-mutation, session-for-read split.
Read/mutation of RuntimeConfig rows only, never touches infrastructure.

Secret path (TRK-303 part 4/7): a write with ``category="secret"`` is
Fernet-encrypted into ``encrypted_value`` using ``RUNTIME_CONFIG_ENCRYPTION_KEY``;
``value`` stays NULL and no read path ever serializes plaintext or ciphertext
back out (``_row_out`` always reports ``value: null`` for secret rows). That key
is a bootstrap-only Settings field and is genuinely never DB-overridable: it is
in ``config._NON_OVERRIDABLE``, and both this encrypt path and the resolver's
decrypt path read it through ``config.runtime_config_encryption_key()``, which
sources it from env/defaults only — never from the resolved (DB-layered)
Settings.

Once a key is stored as a secret it stays one: a PUT that would demote an
existing ``is_secret=True`` row to a plaintext category is rejected 400 (the
value would otherwise land in ``value``, which the list endpoint serves to any
session). Updating it as a secret, or DELETEing it to fall back to the
env-sourced default, are the two supported paths.

Denylist (safety): ``config._NON_OVERRIDABLE`` names the Settings fields that
gate infra-brain's read-only / auth guarantees or the resolver's own DB
bootstrap. A PUT to one of those keys is rejected 403 here, before it can be
persisted; the resolver independently ignores such a row if one reaches the
table by some other route.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from infra_brain.api.schemas import (
    RuntimeConfigOut,
    RuntimeConfigPageOut,
    RuntimeConfigWriteBody,
)
from infra_brain.config import (
    _NON_OVERRIDABLE,
    get_settings,
    runtime_config_encryption_key,
)
from infra_brain.dashboard_auth import current_user, require_admin, require_session
from infra_brain.db.models import RuntimeConfig
from infra_brain.db.session import get_session

runtime_config_router = APIRouter(
    prefix="/api/dashboard/runtime-config",
    tags=["runtime-config"],
    dependencies=[Depends(require_session)],
)


def _row_out(row: RuntimeConfig) -> RuntimeConfigOut:
    return RuntimeConfigOut(
        key=row.key,
        value_type=row.value_type,
        value=None if row.is_secret else row.value,  # write-only secret contract
        is_secret=row.is_secret,
        category=row.category,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


@runtime_config_router.get("", response_model=RuntimeConfigPageOut)
def list_runtime_config():
    with get_session() as s:
        rows = s.query(RuntimeConfig).order_by(RuntimeConfig.key).all()
        return RuntimeConfigPageOut(items=[_row_out(r) for r in rows])


@runtime_config_router.put("/{key}", response_model=RuntimeConfigOut)
def upsert_runtime_config(
    key: str,
    body: RuntimeConfigWriteBody,
    request: Request,
    _: None = Depends(require_admin),
):
    # Safety denylist, enforced BEFORE the DB is touched so a rejected write
    # leaves no row behind. config._load_runtime_overrides() enforces the same
    # set again at read time for rows that never came through this API.
    if key in _NON_OVERRIDABLE:
        raise HTTPException(
            403,
            f"{key!r} is not runtime-overridable: it gates a read-only/auth guarantee "
            "or the config resolver's own bootstrap. Change it via environment "
            "configuration and restart.",
        )
    user = current_user(request) or {}
    with get_session() as s:
        row = s.get(RuntimeConfig, key)
        # No silent secret -> plaintext demotion. Without this, any session-gated
        # caller (curl, devtools — the dashboard's read-only rendering of secret
        # rows is UI-side only and enforces nothing) could PUT a non-secret
        # category over an existing secret row, moving the value into the
        # plaintext `value` column where the list endpoint serves it back to
        # every session. Checked BEFORE any attribute is touched so the rejected
        # request cannot leave a partially-mutated row behind. A secret may be
        # updated (staying encrypted) or DELETEd to fall back to the env value —
        # never converted in place.
        if (
            row is not None
            and (row.is_secret or row.category == "secret")
            and body.category != "secret"
        ):
            raise HTTPException(
                400,
                f"{key!r} is stored as a secret and cannot be converted to a plaintext "
                f"{body.category!r} value. Update it with category='secret', or DELETE "
                "it to revert to the environment-sourced default.",
            )
        if row is None:
            row = RuntimeConfig(key=key)
            s.add(row)
        row.value_type = body.value_type
        row.category = body.category
        if body.category == "secret":
            # Env-only, explicitly NOT get_settings() — the resolver's decrypt
            # path uses this exact function, so the two can never split-brain.
            enc_key = runtime_config_encryption_key()
            if not enc_key:
                raise HTTPException(500, "RUNTIME_CONFIG_ENCRYPTION_KEY not configured")
            row.encrypted_value = Fernet(enc_key.encode()).encrypt(body.value.encode())
            row.value = None
            row.is_secret = True
        else:
            row.value = body.value
            row.encrypted_value = None
            row.is_secret = False
        row.updated_by = user.get("username") or "dashboard"
        s.commit()
        s.refresh(row)
        out = _row_out(row)
    get_settings.cache_clear()
    return out


@runtime_config_router.delete("/{key}", status_code=204)
def delete_runtime_config(key: str, _: None = Depends(require_admin)):
    with get_session() as s:
        row = s.get(RuntimeConfig, key)
        if row is None:
            raise HTTPException(404, "no override set for this key")
        s.delete(row)
        s.commit()
    get_settings.cache_clear()
    return Response(status_code=204)
