"""The operator-facing configuration surface (`/api/dashboard/settings-catalog`).

WHY THIS EXISTS. The Settings page used to offer exactly two things: a
read-only dump of `Settings.model_dump()` (grouped, masked — `GET /settings`,
governance_ops.py) and a RAW key/value editor over `runtime_config`
(runtime_config.py). The editor required you to already know the exact field
name to type, and `runtime_config` starts empty, so the page showed nothing and
taught nothing. ~270 real settings existed and none of them were discoverable.

This router turns the `Settings` model itself into a browsable, searchable,
editable catalog. Three properties matter:

1. DERIVED, NEVER DUPLICATED. Every entry — name, type, default, description,
   grouping — is computed from the live `Settings` pydantic model and from
   config.py's own source text at request time. There is deliberately no
   hand-maintained list of settings in this file; such a list rots on the first
   field someone adds. `_field_descriptions()` parses the `#` comment block
   above each field declaration (config.py documents every field that way and
   uses no `Field(description=...)` — 0 of 273 fields carry one).

2. SOURCE ATTRIBUTION (TRK-314). For each key the catalog reports whether the
   effective value came from `db-override`, `env`, or `default`, and — when a
   DB override is winning — what env/default value it is currently masking
   (`shadowed_value`). TRK-314 was precisely a `runtime_config` row silently
   masking a wrong `.env` value with nothing in any UI showing that was
   happening. It also reports `override_ignored_reason` for the inverse case: a
   `runtime_config` row that EXISTS but was not applied (denylisted, failed
   type validation, undecryptable) — a silent no-op that used to be visible
   only in the process log.

3. SECRETS ARE WITHHELD ENTIRELY. See the block comment on `_classify` below.
   This is the hard constraint; `tests/test_settings_catalog_secrets.py` is the
   proof and must never be relaxed.

WHAT THIS ROUTER DOES NOT OWN:
  * `dispatchable__<domain>` collector pause levers — those are not `Settings`
    fields and are managed from the Agents page (`AgentConfig.tsx`, backed by
    `runtime_flags.py`). The catalog covers `Settings` fields only, so they
    never appear here and cannot be un-paused from a surface that does not
    explain what they do.
  * Scan cadence — `ScanSchedule.tsx`.
  * The raw key/value escape hatch — still `runtime_config.py`, unchanged. The
    catalog is the guided path, not a replacement: a power user (or a
    non-`Settings` key) still needs the raw editor.
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic_core import PydanticUndefined

from infra_brain.api._helpers import _s, redact_setting
from infra_brain.api.schemas import (
    SettingCatalogEntry,
    SettingsCatalogPageOut,
    SettingsCatalogWriteBody,
)
from infra_brain.config import (
    _NON_OVERRIDABLE,
    Settings,
    _load_runtime_overrides,
    _validated_override,
    get_settings,
)
from infra_brain.dashboard_auth import current_user, require_admin, require_session
from infra_brain.db.models import RuntimeConfig
from infra_brain.db.session import get_session

log = logging.getLogger(__name__)

settings_catalog_router = APIRouter(
    prefix="/api/dashboard/settings-catalog",
    tags=["settings-catalog"],
    dependencies=[Depends(require_session)],
)

# Maximum rendered description length. The comment blocks in config.py are
# genuinely long (several are 800+ characters of rationale); the full text is
# always available in the source, and an unbounded cap would make a ~270-entry
# payload needlessly heavy on every page load.
_MAX_DESCRIPTION = 700

# Where a secret actually belongs. Per CLAUDE.md's "Secrets and Configuration":
# BWS_ACCESS_TOKEN is the ONE secret that lives outside Bitwarden (k8s Secret /
# Docker env var); everything else is pulled at startup by
# `secrets.py::load_secrets_into_env()`.
_BOOTSTRAP_SECRET_KEYS = frozenset({"bws_access_token"})


# ---------------------------------------------------------------------------
# Secret classification — THE hard constraint
#
# The rule reused here is `_helpers.redact_setting`, which TRK-342 established
# as THE single settings-redaction implementation (both `GET /settings` and the
# MCP `get_settings` tool go through it, and a parity test fails if either
# names a masking primitive directly). This module deliberately does not invent
# a second rule; it CONSUMES that one and then applies a STRICTER output
# contract on top:
#
#   * `redact_setting` renders a secret via `mask_secret`, which preserves the
#     LAST FOUR CHARACTERS. That is a reasonable triage affordance on the
#     existing admin dump. It is not acceptable here — this catalog is the
#     surface an operator browses casually and screenshots — so a classified
#     secret gets `value=None` / `default=None` and a bare "set" / "not set"
#     state instead. Nothing derived from the value is emitted at all.
#
#   * GAP CLOSED (value-shape): `redact_setting`'s NAME hint
#     (`_SECRET_HINTS` = key/token/password/secret) misses a credential embedded
#     in a DSN — `postgres_url`, `redis_url`, `haproxy_stats_url` carry
#     `scheme://user:pass@host` and match no hint. `redact_setting` still scrubs
#     those by VALUE shape (`scrub_dsn`), so they do not leak on the existing
#     surface; but a name-only classification would mark them editable and
#     printable here. So: if scrubbing CHANGED the rendering, the field is
#     treated as secret-bearing ("embedded-credential") for this surface's
#     purposes. That is value-derived, so a field carrying a credential today
#     and not tomorrow is classified correctly on both days.
# ---------------------------------------------------------------------------


def _classify(key: str, value: Any) -> tuple[bool, str | None]:
    """Return ``(is_secret, reason)`` for one field/value pair.

    ``reason`` is ``"name-hint"`` or ``"embedded-credential"``. Never returns
    the value or anything derived from it — callers must not reconstruct one.
    """
    kind, redacted = redact_setting(key, value)
    if kind == "secret":
        return True, "name-hint"
    if kind == "bool":
        return False, None
    if _s(redacted) != _s(value):
        # scrub_dsn removed a `user:pass@` segment — a live credential.
        return True, "embedded-credential"
    return False, None


def _managed_in(key: str) -> str:
    env_var = key.upper()
    if key in _BOOTSTRAP_SECRET_KEYS:
        return (
            f"Environment / Kubernetes Secret (`{env_var}`) — the one secret that "
            "lives outside Bitwarden, because it is what unlocks Bitwarden."
        )
    return (
        f"Bitwarden Secrets Manager, injected into `{env_var}` at startup by "
        "secrets.py. For local development, set it in `.env` (never edited from "
        "this UI)."
    )


# ---------------------------------------------------------------------------
# Descriptions — parsed out of config.py's own source comments
# ---------------------------------------------------------------------------

# A "divider" comment line: `# ------`, `# ======`, `# ─────`. These bracket a
# section rather than describing the field, so they are skipped on the way up
# (not treated as the end of the block) — otherwise every field whose docs sit
# above a closing divider would come back undocumented.
_DIVIDER_CHARS = set("-=─═_# \t")


def _is_divider(text: str) -> bool:
    return not text or set(text) <= _DIVIDER_CHARS


@lru_cache(maxsize=1)
def _field_descriptions() -> dict[str, str]:
    """Map each ``Settings`` field to the ``#`` comment block above it.

    config.py documents its fields exclusively with preceding comments — no
    field uses ``Field(description=...)``. Rather than transcribe ~270 of them
    into a dict that would immediately start rotting, this reads the class
    source and walks upward from each annotated assignment, collecting
    contiguous comment lines. Cached: the source cannot change without a
    process restart.

    Fail-open — any parsing problem yields an empty map, and the catalog simply
    renders without descriptions rather than 500-ing.
    """
    try:
        src = textwrap.dedent(inspect.getsource(Settings))
        lines = src.splitlines()
        cls = ast.parse(src).body[0]
        if not isinstance(cls, ast.ClassDef):
            return {}
        out: dict[str, str] = {}
        for node in cls.body:
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
                continue
            block: list[str] = []
            idx = node.lineno - 2  # 0-based index of the line above the field
            while idx >= 0:
                stripped = lines[idx].strip()
                if not stripped.startswith("#"):
                    break
                text = stripped.lstrip("#").strip()
                if _is_divider(text):
                    if block:
                        break  # a divider ABOVE real text closes the block
                    idx -= 1  # a trailing divider directly above the field
                    continue
                block.append(text)
                idx -= 1
            if block:
                joined = " ".join(reversed(block))
                if len(joined) > _MAX_DESCRIPTION:
                    joined = joined[: _MAX_DESCRIPTION - 1].rstrip() + "…"
                out[node.target.id] = joined
        return out
    except Exception:  # noqa: BLE001 — cosmetic layer, never break the endpoint
        log.warning("could not derive Settings field descriptions from source", exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# Grouping — prefix rules, evaluated in order, first match wins
#
# Presentation only. Prefix-driven rather than per-field so a newly added field
# lands in a sensible bucket without anyone editing this table; anything
# unmatched falls into "Other", which is a visible prompt to add a rule rather
# than a silent hiding place.
# ---------------------------------------------------------------------------

_GROUP_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("LLM & models", ("llm_", "anthropic_", "bedrock_", "openai_")),
    ("Embeddings & RAG", ("embedding_", "rag_", "confluence_rag")),
    ("Database & cache", ("postgres_", "redis_", "db_pool", "db_max")),
    (
        "Security & read-only boundary",
        (
            "scan_readonly",
            "dlp_",
            "scripts_",
            "integration_approval",
            "environment",
            "infra_brain_dev",
            "infra_brain_mcp",
            "internal_service_token",
            "mcp_",
            "runtime_config_",
        ),
    ),
    (
        "Dashboard & auth",
        (
            "ui_",
            "admin_",
            "cookie_",
            "trusted_",
            "chat_rate_limit",
            "daily_token_budget",
        ),
    ),
    ("Webhooks", ("webhook_", "ops_webhook")),
    ("ChatOps", ("chatops_", "slack_", "teams_")),
    ("Notifications", ("jira_", "confluence_", "notification_")),
    (
        "Observability",
        ("langsmith", "langfuse", "prometheus_", "grafana_", "alertmanager_", "uptime_kuma"),
    ),
    ("Retention & pruning", ("retention_",)),
    (
        "Scheduling & sweeps",
        (
            "sweep_",
            "embedded_scheduler",
            "collect_timeout",
            "collection_",
            "api_page_size",
            "api_timeout",
        ),
    ),
    (
        "Collector — GitLab, IaC & remediation",
        (
            "gitlab_",
            "iac_",
            "compliance_rules_",
            "script_library_",
            "remediation_",
            "inventory_",
            "host_purpose_map_",
            "repo_docs_root",
        ),
    ),
    ("Collector — Octopus Deploy", ("octopus_",)),
    ("Collector — Rapid7 & vulnerabilities", ("rapid7_",)),
    ("Collector — vSphere", ("vsphere_",)),
    ("Collector — Ansible, Linux & Windows", ("ansible_", "linux_")),
    ("Collector — network discovery & DNS", ("netdiscovery_", "dns_", "snmp_", "default_zone")),
    (
        "Collector — load balancers & edge",
        ("lb_enabled", "f5_", "nginx_plus", "haproxy_", "cloudflare_"),
    ),
    ("Collector — Wazuh, Okta & secrets inventory", ("wazuh_", "okta_", "secrets_inventory_")),
    ("Collector — backup", ("backup_",)),
    ("Collector — cloud & Kubernetes", ("aws_", "k8s_")),
    ("Collector — SaaS & container registries", ("saas_", "container_registr")),
    (
        "Collector — homelab services & docs",
        ("homelab_", "personal_wiki", "headless_runner"),
    ),
    (
        "Reasoner-tier features & feature flags",
        (
            "rootcause_llm",
            "compliance_gap_finder",
            "capacity_forecast",
            "coverage_gap",
            "graph_edge_decay",
            "is_same_as_decay",
            "integration_confidence_gate",
            "infra_ops_observe",
        ),
    ),
    ("Misc", ("context7_", "script_timeout")),
)

_FALLBACK_GROUP = "Other"


def _group_for(key: str) -> str:
    for group, prefixes in _GROUP_RULES:
        if key.startswith(prefixes):
            return group
    return _FALLBACK_GROUP


_GROUP_ORDER: tuple[str, ...] = tuple(g for g, _ in _GROUP_RULES) + (_FALLBACK_GROUP,)


# ---------------------------------------------------------------------------
# Type + value rendering
# ---------------------------------------------------------------------------


def _type_name(key: str) -> str:
    ann = Settings.model_fields[key].annotation
    # bool BEFORE int — bool is a subclass of int and would otherwise be "int".
    for typ, name in ((bool, "bool"), (int, "int"), (float, "float"), (str, "str")):
        if ann is typ:
            return name
    return "other"


def _render(value: Any) -> str | None:
    """Render a NON-secret value for display. Callers must classify first."""
    if value is None:
        return None
    if isinstance(value, bool):
        # Lowercase so what is displayed is also what you would type into an
        # override (pydantic parses "true"/"false").
        return "true" if value else "false"
    _kind, redacted = redact_setting("", value)  # value-shape scrub only
    return _s(redacted)


def _default_of(key: str) -> Any:
    default = Settings.model_fields[key].default
    return None if default is PydanticUndefined else default


def _db_override_keys() -> set[str]:
    """Keys with a `runtime_config` row, applied or not. Fail-open to empty."""
    try:
        with get_session() as s:
            return {row[0] for row in s.query(RuntimeConfig.key).all()}
    except Exception:  # noqa: BLE001 — a bookkeeping read must not break the page
        log.debug("runtime_config key listing failed for the settings catalog", exc_info=True)
        return set()


def _entry(
    key: str,
    *,
    base: Settings,
    effective: Settings,
    overrides: dict[str, object],
    db_keys: set[str],
    descriptions: dict[str, str],
) -> SettingCatalogEntry:
    env_value = getattr(base, key)
    eff_value = getattr(effective, key)
    default_value = _default_of(key)

    # Classify against EVERY value this field can expose, not just the
    # effective one: a DB override could be a harmless string while the env
    # value underneath is a live credential, and `shadowed_value` would print
    # it. First hit wins.
    secret, reason = False, None
    for candidate in (eff_value, env_value, default_value):
        secret, reason = _classify(key, candidate)
        if secret:
            break

    applied = key in overrides
    if applied:
        source = "db-override"
    elif env_value != default_value:
        source = "env"
    else:
        source = "default"

    denylisted = key in _NON_OVERRIDABLE
    editable = not secret and not denylisted
    locked_reason = None
    if secret:
        locked_reason = "Secret — set it where it belongs, never from this UI."
    elif denylisted:
        locked_reason = (
            "Non-overridable: this field gates a read-only/auth guarantee or the "
            "config resolver's own bootstrap. Change it in the environment and "
            "restart."
        )

    override_ignored_reason = None
    if key in db_keys and not applied:
        override_ignored_reason = (
            "A runtime_config row exists for this key but is IGNORED — it is on the "
            "safety denylist, so the environment/default value stands."
            if denylisted
            else (
                "A runtime_config row exists for this key but was NOT applied — it "
                "failed validation against this field's type, could not be "
                "decrypted, or has no value. Check the application log."
            )
        )

    return SettingCatalogEntry(
        key=key,
        env_var=key.upper(),
        group=_group_for(key),
        type=_type_name(key),
        description=descriptions.get(key, ""),
        value=None if secret else _render(eff_value),
        default=None if secret else _render(default_value),
        source=source,
        shadowed_value=(None if secret or not applied else _render(env_value)),
        secret=secret,
        secret_state=None if not secret else ("set" if _s(eff_value) else "not set"),
        secret_reason=reason,
        managed_in=_managed_in(key) if secret else None,
        editable=editable,
        locked_reason=locked_reason,
        db_row=key in db_keys,
        override_ignored_reason=override_ignored_reason,
    )


def _degraded_entry(key: str, exc: Exception) -> SettingCatalogEntry:
    """A metadata-only row for a field that could not be rendered.

    NOTHING derived from the exception or the value goes into this row — only
    the exception CLASS name. The same discipline config.py applies when a
    secret row fails validation: pydantic/ValueError messages routinely embed
    the offending input, so echoing `str(exc)` would leak exactly the value
    this surface exists to withhold.
    """
    return SettingCatalogEntry(
        key=key,
        env_var=key.upper(),
        group=_group_for(key),
        type="other",
        description="",
        value=None,
        default=None,
        source="unknown",
        editable=False,
        locked_reason=(
            f"Could not be rendered safely ({type(exc).__name__}); withheld rather "
            "than shown partially."
        ),
        degraded=True,
    )


@settings_catalog_router.get("", response_model=SettingsCatalogPageOut)
def list_settings_catalog(_: None = Depends(require_admin)) -> SettingsCatalogPageOut:
    """The full derived catalog.

    `require_admin`, matching `GET /settings` (TRK-321): this enumerates every
    configuration key in the system, which is an elevated view even with every
    secret value withheld. Non-admin sessions get 403 here and keep using
    `GET /settings/ui`.

    A plain `def` (FastAPI threadpool) — `_load_runtime_overrides` does blocking
    DB I/O with its own bounded connect/statement timeouts, and must not run on
    the event loop.
    """
    descriptions = _field_descriptions()
    base = Settings()
    overrides = _load_runtime_overrides(base)
    # Compute the effective snapshot the SAME way get_settings() does, from the
    # SAME overrides dict used for attribution — rather than reading the
    # TTL-cached get_settings(), whose overrides may be up to 15s staler and
    # would make the reported value and its reported source disagree.
    effective = base.model_copy(update=overrides) if overrides else base
    db_keys = _db_override_keys()

    items: list[SettingCatalogEntry] = []
    for key in sorted(Settings.model_fields):
        try:
            items.append(
                _entry(
                    key,
                    base=base,
                    effective=effective,
                    overrides=overrides,
                    db_keys=db_keys,
                    descriptions=descriptions,
                )
            )
        except Exception as exc:  # noqa: BLE001 — one bad field must not 500 the page
            log.warning(
                "settings-catalog entry for %r could not be rendered (%s); "
                "emitting a degraded row (detail withheld: may embed the value)",
                key,
                type(exc).__name__,
            )
            items.append(_degraded_entry(key, exc))

    present = {it.group for it in items}
    groups = [g for g in _GROUP_ORDER if g in present]
    return SettingsCatalogPageOut(items=items, total=len(items), groups=groups)


def _guard_writable(key: str) -> None:
    """Server-side enforcement of the catalog's own editability contract.

    The UI renders secrets and denylisted fields as non-editable, but UI
    read-only-ness is not authorization — a curl/devtools caller reaches the
    same route. Every rule the catalog advertises is re-checked here.
    """
    if key not in Settings.model_fields:
        raise HTTPException(
            404,
            f"{key!r} is not a Settings field. Non-Settings runtime_config keys "
            "(for example the dispatchable__<domain> collector pause levers) are "
            "managed elsewhere — see the Agents page — or through the advanced "
            "raw runtime-config editor.",
        )
    if key in _NON_OVERRIDABLE:
        raise HTTPException(
            403,
            f"{key!r} is not runtime-overridable: it gates a read-only/auth "
            "guarantee or the config resolver's own bootstrap. Change it via "
            "environment configuration and restart.",
        )
    # Classify against the CURRENT value, exactly as the GET path does, so a
    # field the catalog shows as a locked secret cannot be written here.
    settings = get_settings()
    for candidate in (getattr(settings, key, None), _default_of(key)):
        secret, _reason = _classify(key, candidate)
        if secret:
            raise HTTPException(
                403,
                f"{key!r} is a secret-bearing setting and is never editable from "
                f"the dashboard. {_managed_in(key)}",
            )


@settings_catalog_router.put("/{key}", response_model=SettingCatalogEntry)
def upsert_setting(
    key: str,
    body: SettingsCatalogWriteBody,
    request: Request,
    _: None = Depends(require_admin),
) -> SettingCatalogEntry:
    """Set a `runtime_config` override for one editable, non-secret field."""
    _guard_writable(key)
    try:
        _validated_override(key, body.value)
    except Exception as exc:  # noqa: BLE001
        # Deliberately NOT `str(exc)`: pydantic's ValidationError embeds the
        # offending input, so echoing it would reflect a mis-pasted credential
        # straight back into the response body.
        raise HTTPException(
            400,
            f"The submitted value is not valid for {key!r} (expected "
            f"{_type_name(key)}; rejected by {type(exc).__name__}). The value is "
            "not echoed back.",
        ) from None

    value_type = _type_name(key)
    if value_type == "other":
        value_type = "str"
    user = current_user(request) or {}
    with get_session() as s:
        row = s.get(RuntimeConfig, key)
        if row is None:
            row = RuntimeConfig(key=key)
            s.add(row)
        row.value = body.value
        row.encrypted_value = None
        row.is_secret = False
        row.value_type = value_type
        row.category = "tuning"
        row.updated_by = user.get("username") or "dashboard"
        s.commit()
    get_settings.cache_clear()
    return _one(key)


@settings_catalog_router.delete("/{key}", response_model=SettingCatalogEntry)
def revert_setting(key: str, _: None = Depends(require_admin)) -> SettingCatalogEntry:
    """Revert to default: delete the `runtime_config` row for this key.

    Returns the recomputed entry rather than 204 so the UI can render the value
    that took over (and its new source) without a second round-trip — the point
    of the button is to show you what you fell back TO.
    """
    _guard_writable(key)
    with get_session() as s:
        row = s.get(RuntimeConfig, key)
        if row is None:
            raise HTTPException(404, f"no runtime override is set for {key!r}")
        s.delete(row)
        s.commit()
    get_settings.cache_clear()
    return _one(key)


def _one(key: str) -> SettingCatalogEntry:
    base = Settings()
    overrides = _load_runtime_overrides(base)
    effective = base.model_copy(update=overrides) if overrides else base
    return _entry(
        key,
        base=base,
        effective=effective,
        overrides=overrides,
        db_keys=_db_override_keys(),
        descriptions=_field_descriptions(),
    )
