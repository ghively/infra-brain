"""Shared helper functions and constants for the infra-brain dashboard API.

Extracted from dashboard_api.py (Task 2 — Phase 3 god-file split).
Bodies are byte-identical to the originals; no logic changes.

Import rules:
  * MAY import from infra_brain.db.models, infra_brain.db.session,
    infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * infra_brain.dashboard_api imports FROM this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from fastapi import Request

from infra_brain.db.models import Resource
from infra_brain.etl.base import (
    RUN_STATUS_INTERRUPT_PENDING,
    RUN_STATUS_RETRY_EXHAUSTED,
    scrub_dsn,
)

if TYPE_CHECKING:
    from infra_brain.db.models import R7Vulnerability

__all__ = [
    "_SECRET_HINTS",
    "mask_secret",
    "redact_setting",
    "_s",
    "_now",
    "_sla_string",
    "_CRON_HUMAN",
    "_humanize_cron",
    "_next_run_for",
    "_RUN_STATUS",
    "_setting_row",
    "_audit_category",
    "_AUDIT_CATEGORY_KEYWORDS",
    "_best_vuln_by_cve",
    "_seed_resource_row",
    "require_session_optional",
    "_DOMAIN_REQUIREMENTS",
    "_SECRET_KEYS",
    "_is_secret",
    "missing_requirements",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Substrings that mark a Settings/config key NAME as secret-bearing. Consumed
# ONLY through redact_setting() (TRK-342), so adding an entry here lands on
# every full-settings surface at once — GET /api/dashboard/settings, the MCP
# `get_settings` tool, and the derived settings catalog.
#
# "community" was added after the settings-catalog work: an SNMP community
# string IS a shared credential (it is the entire authentication story in
# SNMP v1/v2c), and `snmp_community` matches none of the other four hints, so
# it was rendering in cleartext on both full-settings surfaces. Note the check
# is a plain substring test — the goal is to over-match rather than miss, and a
# non-secret field that happens to contain one of these words only loses its
# value's visibility, never its behaviour.
_SECRET_HINTS = ("key", "token", "password", "secret", "community")

_RUN_STATUS = {
    "completed": "success",
    "failed": "failure",
    "in_progress": "running",
    # Phase 2 sweep graph run-state constants (etl/base.py): retry_exhausted
    # is failed-equivalent (RetryPolicy exhausted, swallowed rather than
    # crashing the sweep); interrupt_pending is in-progress-equivalent (a
    # Phase 3 sweep paused awaiting human approval, not stuck/failed).
    RUN_STATUS_RETRY_EXHAUSTED: "failure",
    RUN_STATUS_INTERRUPT_PENDING: "running",
}

_CRON_HUMAN = {
    "0 */6 * * *": "every 6h",
    "0 2 * * *": "daily 02:00 UTC",
    "0 5 * * *": "daily 05:00 UTC",
    "0 3 * * 0": "weekly Sun 03:00",
    "0 4 * * 0": "weekly Sun 04:00",
}

_CRON_DOW_NAMES = {
    "0": "Sun",
    "1": "Mon",
    "2": "Tue",
    "3": "Wed",
    "4": "Thu",
    "5": "Fri",
    "6": "Sat",
    "7": "Sun",
}

# SQL-side pre-filter fragments matching _audit_category, so the category filter
# narrows in the DB rather than scanning the full 500-row cap in Python.
_AUDIT_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "dlp": ["pan", "card", "luhn", "dlp"],
    "allowlist": ["allowlist", "allowed", "allow"],
}

# ---------------------------------------------------------------------------
# Per-domain configuration requirements
# ---------------------------------------------------------------------------
#
# Maps each domain to the env vars it requires + human-readable labels; used to
# tell the operator what is missing before a collector can run.
#
# MOVED HERE from `routers/governance_ops.py` (unchanged, byte-for-byte) when
# `routers/sources.py` was added. Two routers now need "is this domain
# configured?" and a router importing a private name out of a sibling router is
# the import cycle `routers/cloudnet.py`'s header explicitly forbids. This
# module is already the shared-helper home both may import from, so the table
# lives here and `governance_ops` re-imports it — one copy, same answer on both
# surfaces. `governance_ops._DOMAIN_REQUIREMENTS` still resolves (the import
# binds the name in that module), so nothing that referenced it moved.

_DOMAIN_REQUIREMENTS: dict[str, list[dict]] = {
    "linux": [
        {
            "key": "ANSIBLE_INVENTORY_PATH",
            "label": "Ansible inventory file path",
            "attr": "ansible_inventory_path",
        },
    ],
    "windows": [
        {
            "key": "ANSIBLE_INVENTORY_PATH",
            "label": "Ansible inventory file path",
            "attr": "ansible_inventory_path",
        },
    ],
    "onprem": [
        {
            "key": "ANSIBLE_INVENTORY_PATH",
            "label": "Ansible inventory file path",
            "attr": "ansible_inventory_path",
        },
    ],
    "vsphere": [
        {"key": "VSPHERE_HOST", "label": "vCenter hostname", "attr": "vsphere_host"},
        {"key": "VSPHERE_USER", "label": "vCenter username", "attr": "vsphere_user"},
        {"key": "VSPHERE_PASSWORD", "label": "vCenter password", "attr": "vsphere_password"},
    ],
    "cloud": [
        {"key": "AWS_ACCESS_KEY_ID", "label": "AWS access key", "attr": None},
    ],
    "net": [
        {
            "key": "SNMP_TARGETS",
            "label": "SNMP target hosts (comma-separated)",
            "attr": "snmp_targets",
        },
        {"key": "SNMP_COMMUNITY", "label": "SNMP community string", "attr": "snmp_community"},
    ],
    "cicd": [
        {"key": "GITLAB_URL", "label": "GitLab base URL", "attr": "gitlab_url"},
        {"key": "GITLAB_TOKEN", "label": "GitLab access token", "attr": "gitlab_token"},
    ],
    "octopus": [
        {"key": "OCTOPUS_URL", "label": "Octopus Deploy URL", "attr": "octopus_url"},
        {"key": "OCTOPUS_API_KEY", "label": "Octopus API key", "attr": "octopus_api_key"},
    ],
    "vuln": [
        {"key": "RAPID7_URL", "label": "Rapid7 InsightVM URL", "attr": "rapid7_url"},
        {"key": "RAPID7_API_KEY", "label": "Rapid7 API key", "attr": "rapid7_api_key"},
    ],
    "k8s": [
        {"key": "KUBECONFIG", "label": "Kubernetes config file path", "attr": None},
    ],
    "eol": [],  # auto-discovers from eol_registry table; no config needed
    "iac": [
        {"key": "GITLAB_URL", "label": "GitLab base URL", "attr": "gitlab_url"},
        {"key": "GITLAB_TOKEN", "label": "GitLab access token", "attr": "gitlab_token"},
    ],
}

_SECRET_KEYS = {"token", "password", "key", "secret"}


def _is_secret(key: str) -> bool:
    return any(s in key.lower() for s in _SECRET_KEYS)


def missing_requirements(domain: str, settings: Any) -> list[str]:
    """Env-var keys `domain` declares but does not have a value for.

    Empty list means "fully configured", which for a domain with no declared
    requirements at all (the common case — most collectors read no credentials)
    is also empty. Callers that need to tell those two apart check
    ``_DOMAIN_REQUIREMENTS`` directly, exactly as `list_agent_config` does.

    Same resolution order as `list_agent_config`: a requirement with an ``attr``
    reads the (runtime-overlayable) Settings field, one without falls back to
    the raw process environment. Values are never returned — only which keys are
    unset — so this is safe to call from a surface that does not mask secrets.
    """
    import os  # noqa: PLC0415

    missing: list[str] = []
    for req in _DOMAIN_REQUIREMENTS.get(domain, []):
        attr = req.get("attr")
        raw = (getattr(settings, attr, "") or "") if attr else os.environ.get(req["key"], "")
        if not raw:
            missing.append(req["key"])
    return missing


# ---------------------------------------------------------------------------
# Simple value helpers
# ---------------------------------------------------------------------------


def mask_secret(value: str | None) -> str:
    if not value:
        return "(unset)"
    tail = value[-4:] if len(value) >= 4 else ""
    return f"••••••{tail}"


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sla_string(due: datetime | None) -> str:
    if due is None:
        return "—"
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    delta = due - _now()
    seconds = delta.total_seconds()
    # Compute the day count from the magnitude, not via floor division on a
    # (possibly negative) delta: `//` floors toward -inf, so a 1-hour-overdue
    # delta (-3600s) would floor to -1 and misreport "overdue 1d" instead of
    # "overdue 0d". Flooring the absolute value truncates toward zero for
    # both branches, matching the "whole days elapsed/remaining" semantics.
    days = int(abs(seconds) // 86400)
    if seconds < 0:
        return f"overdue {days}d"
    return f"due in {days}d"


def _humanize_cron(cron: str) -> str:
    """Render a 5-field crontab string as a short, uniformly-phrased string.

    The Scan Schedule dashboard page was showing raw crontab syntax for
    every schedule except the handful of exact strings hardcoded in
    ``_CRON_HUMAN`` above, making the column an inconsistent mix of
    "50 2 * * *" and "weekly Sun 03:00" — this parses the standard minute/
    hour/day-of-week fields so every agent's schedule renders the same way.
    Falls back to the raw string for anything not recognized (comma/step
    lists on day-of-month or month, or non-crontab text like "on-demand"),
    and never raises.
    """
    if cron in _CRON_HUMAN:
        return _CRON_HUMAN[cron]
    parts = cron.split() if cron else []
    if len(parts) != 5:
        return cron
    minute, hour, dom, month, dow = parts
    if dom != "*" or month != "*":
        return cron

    if minute.startswith("*/") and minute[2:].isdigit() and hour == "*" and dow == "*":
        return f"every {minute[2:]}m"

    if hour.startswith("*/") and hour[2:].isdigit() and minute.isdigit() and dow == "*":
        step = hour[2:]
        return f"every {step}h" if minute == "0" else f"every {step}h at :{int(minute):02d}"

    if hour == "*" and minute.isdigit() and dow == "*":
        return f"hourly at :{int(minute):02d}"

    if minute.isdigit() and hour.isdigit():
        hh, mm = int(hour), int(minute)
        if dow == "*":
            return f"daily {hh:02d}:{mm:02d} UTC"
        if dow in _CRON_DOW_NAMES:
            return f"weekly {_CRON_DOW_NAMES[dow]} {hh:02d}:{mm:02d}"

    return cron


def _next_run_for(cron: str, after: Optional[datetime] = None) -> str:
    """Compute the next fire time for a crontab string as an ISO-8601 string.

    FE-16: the Scan Schedule page's ``next_run`` column used to be a hardcoded
    ``"—"`` placeholder — the backend never populated it even though the cron
    string needed to compute a real value (``_DEFAULT_SCHEDULES``) was already
    available. Uses APScheduler's ``CronTrigger`` (already a project
    dependency — see ``scheduler.py``) to parse the crontab and find the next
    fire time after ``after`` (defaults to now). Returns ``"—"`` if the cron
    string can't be parsed (defensive — never raises into a dashboard route).
    """
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415

    try:
        trigger = CronTrigger.from_crontab(cron, timezone=timezone.utc)
        nxt = trigger.get_next_fire_time(None, after or _now())
        return nxt.isoformat() if nxt else "—"
    except Exception:
        return "—"


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _audit_category(reason: str) -> str:
    """Derive the audit taxonomy bucket from the denial_reason text.

    dlp       — PAN / cardholder / Luhn DLP denials
    allowlist — allow-list / allowed decisions
    readonly  — everything else (read-only operations)
    """
    low = (reason or "").lower()
    if any(k in low for k in ("pan", "card", "luhn", "dlp")):
        return "dlp"
    if "allowlist" in low or "allowed" in low or "allow" in low:
        return "allowlist"
    return "readonly"


# ---------------------------------------------------------------------------
# Setting helpers
# ---------------------------------------------------------------------------


# SettingRow is defined in api/schemas.py; import lazily to avoid circular
# import at module load time (schemas imports nothing from _helpers or
# dashboard_api; _helpers only needs SettingRow at function call time).


def redact_setting(key: str, value: Any) -> tuple[str, Any]:
    """THE settings-redaction rule. Returns ``(kind, redacted_value)``.

    ``kind`` is ``"bool"`` | ``"secret"`` | ``"text"``.

    TRK-342 — THIS IS THE ONLY IMPLEMENTATION, ON PURPOSE. Two separate
    surfaces dump the entire ``Settings`` model to a caller:

      * ``GET /api/dashboard/settings`` (admin-gated since TRK-321), via
        :func:`_setting_row` below;
      * the ``get_settings`` MCP tool (token-gated),
        ``infra_brain.mcp_server.get_settings``.

    Each used to carry its own copy of the rule — the same ``_SECRET_HINTS``
    name check, but two DIFFERENT value passes (``scrub_dsn`` here, a locally
    defined anchored DSN regex plus whole-value ``mask_secret`` there). Two
    copies of a security rule drift: a newly added sensitive field gets masked
    on whichever surface its author happened to be looking at.
    ``tests/test_settings_redaction_parity.py`` fails if either call site ever
    names a masking primitive directly again, so the only way to add a rule is
    to add it here — where it lands on both surfaces at once.

    THE RULES, in order:

    1. ``bool`` — passed through. A feature flag is not a secret, and
       stringifying it would destroy the dashboard's toggle rendering and the
       MCP consumer's JSON type.
    2. NAME HINT — a key containing "key"/"token"/"password"/"secret" is
       replaced wholesale by :func:`mask_secret`. The *name* is the signal
       here; nothing about the value is assumed.
    3. VALUE SHAPE — everything else is passed through
       :func:`~infra_brain.etl.base.scrub_dsn`, the SAME redactor already
       applied to every collector error message that reaches the dashboard,
       rather than a second divergent implementation. H-1: masking used to be
       by name only, and ``postgres_url`` / ``postgres_readonly_url`` /
       ``redis_url`` are plain ``str`` carrying ``scheme://user:password@host``
       DSNs that match NO name hint — so the full credentials rendered as plain
       text rows. ``scrub_dsn`` is deliberately scoped to the
       ``scheme://user[:pass]@`` segment, so a credential-free URL
       (``https://gitlab.example.com``, ``redis://localhost:6379/0``) survives
       verbatim and the host/database of a real DSN stays visible for triage.
       Applied to EVERY value's rendering, not to a hardcoded list of DSN field
       names, so a future field carrying an embedded credential is covered by
       default instead of leaking until someone remembers to add it.

    TYPE FIDELITY: rule 3 returns the ORIGINAL value untouched when scrubbing
    changed nothing, so an ``int``/``list``/``None`` stays itself for the
    machine-facing MCP consumer. When scrubbing DID change something the
    scrubbed STRING is returned even for a non-``str`` field — a typed value
    that embeds a live credential cannot be handed back in its typed form, and
    a redaction that silently didn't apply because the field wasn't a ``str``
    is precisely the class of hole this function exists to close.
    """
    if isinstance(value, bool):
        return "bool", value
    if any(h in key.lower() for h in _SECRET_HINTS):
        return "secret", mask_secret(str(value) if value else "")
    rendered = _s(value)
    scrubbed = scrub_dsn(rendered)
    if scrubbed != rendered:
        return "text", scrubbed
    return "text", value


def _setting_row(key: str, value: Any) -> "SettingRow":  # noqa: F821
    """Render one Settings field as a dashboard row, masking anything sensitive.

    All redaction is delegated to :func:`redact_setting` (TRK-342) — this
    function owns only the row SHAPE. Do not reintroduce a masking branch here:
    ``tests/test_settings_redaction_parity.py`` AST-inspects this function and
    fails if it names a masking primitive.
    """
    from infra_brain.api.schemas import SettingRow  # noqa: PLC0415

    kind, redacted = redact_setting(key, value)
    if kind == "bool":
        return SettingRow(k=key.upper(), type="bool", on=redacted)
    if kind == "secret":
        return SettingRow(k=key.upper(), type="secret", v=redacted)
    # The dashboard contract is text-typed rows, so the (possibly still-typed)
    # value is rendered here rather than inside the shared helper.
    return SettingRow(k=key.upper(), type="text", v=_s(redacted))


# ---------------------------------------------------------------------------
# Vuln helpers
# ---------------------------------------------------------------------------


def _best_vuln_by_cve(s: Any, cve_ids: list[str]) -> "dict[str, R7Vulnerability]":
    """Resolve each canonical CVE id to the richest backing R7Vulnerability
    (highest CVSS v3) via the r7_vuln_cves bridge. Returns {cve_id: vuln}."""
    from infra_brain.db.models import R7VulnCve, R7Vulnerability  # noqa: PLC0415

    if not cve_ids:
        return {}
    bridge = s.query(R7VulnCve).filter(R7VulnCve.cve_id.in_(cve_ids)).all()
    slug_map: dict[str, list[str]] = {}
    for br in bridge:
        slug_map.setdefault(br.cve_id, []).append(br.r7_vuln_id)
    all_slugs = list({sl for slugs in slug_map.values() for sl in slugs})
    if not all_slugs:
        return {}
    rv_rows = s.query(R7Vulnerability).filter(R7Vulnerability.r7_vuln_id.in_(all_slugs)).all()
    rv_by_slug = {v.r7_vuln_id: v for v in rv_rows}
    best: dict[str, Any] = {}
    for cid, slugs in slug_map.items():
        candidates = [rv_by_slug[sl] for sl in slugs if sl in rv_by_slug]
        if candidates:
            best[cid] = max(candidates, key=lambda v: v.cvss_v3_score or 0)
    return best


# ---------------------------------------------------------------------------
# Resource seeding helper
# ---------------------------------------------------------------------------


def _seed_resource_row(session: Any, body: Any) -> tuple[str, bool]:
    """Upsert a Resource row. Returns (resource_id, created).

    Task 4.4: thin delegating shim over the canonical
    ``infra_brain.api._seeding.upsert_resource``. Kept (rather than deleted) because
    it is a documented, tested import in ``api/_helpers.__all__`` /
    ``tests/test_api_scaffold.py`` (Task 2 import-parity guard).
    """
    from infra_brain.api._seeding import upsert_resource  # noqa: PLC0415

    meta_payload: dict = {
        "_seed_source": body.source,
        "_seed_at": _now().isoformat(),
    }
    if body.ip_address:
        meta_payload["ip_address"] = body.ip_address
    if body.os_name:
        meta_payload["os_name"] = body.os_name
    if body.environment:
        meta_payload["environment"] = body.environment
    if body.tags:
        meta_payload["tags"] = body.tags
    if body.metadata:
        meta_payload.update(body.metadata)

    existing = (
        session.query(Resource)
        .filter_by(domain=body.domain, type=body.resource_type, name=body.hostname)
        .first()
    )
    created = existing is None

    resource = upsert_resource(
        session,
        name=body.hostname,
        domain=body.domain,
        resource_type=body.resource_type,
        metadata=meta_payload,
        source=body.source,
    )
    return str(resource.id), created


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def require_session_optional(request: Request) -> Optional[dict]:
    """Like require_session but returns None instead of 401 for missing session."""
    from infra_brain.dashboard_auth import current_user  # noqa: PLC0415

    return current_user(request)
