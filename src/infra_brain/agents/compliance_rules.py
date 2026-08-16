"""Policy-as-code rule engine for :mod:`infra_brain.agents.compliance` (GitLab #118).

Before this module, ``ComplianceAgent`` evaluated exactly 4 hardcoded Python
if/query blocks. An operator who wanted a 5th rule needed a code change and a
PR. This module externalizes rule *logic* (not just thresholds, which were
already YAML-driven) into ``rules/enforcement/compliance.yml``'s ``rules:``
list — each entry declares one rule as an instance of one of 4 generic
*shapes*:

- ``field_overdue``    — a date field on a row has passed (e.g. EOL date).
- ``sla_breach``       — a date field has passed, scoped to open/in-progress
  status, with a severity derived from a source field.
- ``staleness``        — a status="open" row has sat unresolved longer than a
  configurable day threshold.
- ``unaddressed_action`` — a row matches a threshold criterion AND no
  qualifying ``ProposedAction`` addressed it within a day window.

Adding a 5th rule that fits one of these shapes is a YAML edit only. A rule
needing a genuinely new shape still needs a new function + registry entry
here — this is intentionally NOT a general-purpose Rego/OPA engine (see
GitLab #118's scoping note): a 4-shape DSL covers the real surface without
that overhead.

Byte-for-byte compatibility: :data:`DEFAULT_RULES` encodes the original 4
hardcoded blocks so the engine reproduces their exact (rule, host, severity,
detail, resource_id) tuples — see
``tests/agents/test_compliance_rule_engine.py``.
"""

from __future__ import annotations

import logging
import operator as _op
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from infra_brain.db.models import (
    DriftEvent,
    EolRegistry,
    ProposedAction,
    R7Asset,
    Resource,
    VulnQueueItem,
)
from infra_brain.db.severity import CRITICAL, normalize_severity
from infra_brain.drift_taxonomy import (
    DERIVED_METRIC_FIELDS,
    TELEMETRY_FIELD_PREFIXES,
    TELEMETRY_FIELDS,
)
from infra_brain.etl.base import ReconcileScope

logger = logging.getLogger(__name__)

# A rule-type function: (session, rule_cfg, now, thresholds) -> found tuples,
# each tuple shaped (rule_name, host, severity, detail, resource_id | None) —
# the exact shape ComplianceAgent._reconcile()/_COMPLIANCE_UPSERT expects.
RuleFn = Callable[[Any, dict, datetime, dict], list[tuple[str, str, str, str, Any]]]

# Only the models the 4 existing rule shapes need to reference by name from
# YAML. Extend this when a new shape needs a new model.
_MODEL_REGISTRY: dict[str, type] = {
    "EolRegistry": EolRegistry,
    "VulnQueueItem": VulnQueueItem,
    "DriftEvent": DriftEvent,
    "R7Asset": R7Asset,
    "Resource": Resource,
    "ProposedAction": ProposedAction,
}

_OPS: dict[str, Callable[[Any, Any], Any]] = {
    "gt": _op.gt,
    "gte": _op.ge,
    "lt": _op.lt,
    "lte": _op.le,
    "eq": _op.eq,
}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _get_nested(d: dict, dotted_key: str, default: Any = None) -> Any:
    """Look up ``a.b.c`` in a nested dict, returning ``default`` the moment a
    non-dict is hit before the path is exhausted (mirrors the original
    ``eol_check.enabled`` guard's "not a dict → treat as default" behavior)."""
    node: Any = d
    for part in dotted_key.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node


def _template_ctx(row: Any, extra: dict | None = None) -> dict:
    """Build a ``str.format`` context from a row's mapped columns, plus any
    rule-type-specific computed values (e.g. a formatted date, a resolved
    threshold)."""
    ctx: dict[str, Any] = {}
    table = getattr(row, "__table__", None)
    if table is not None:
        for col in table.columns:
            ctx[col.name] = getattr(row, col.name, None)
    if extra:
        ctx.update(extra)
    return ctx


def _model(cfg: dict) -> type:
    name = cfg["model"]
    try:
        return _MODEL_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"Unknown compliance rule model {name!r}") from exc


_LIKE_ESCAPE_CHAR = "\\"


def _escape_like(value: str, escape_char: str = _LIKE_ESCAPE_CHAR) -> str:
    """Escape SQL LIKE wildcard metacharacters (``%``, ``_``) — and the escape
    character itself — in ``value`` so it can be embedded in a LIKE pattern as
    a literal substring rather than as wildcard syntax.

    Without this, a hostname containing ``_`` or ``%`` (both common in real
    hostnames — docker/K8s container names, homelab hosts like ``web_01``)
    would have those characters interpreted as "match any single character" /
    "match any sequence" by the database, letting an unrelated host's target
    string spuriously satisfy a same-suffix LIKE match."""
    return (
        value.replace(escape_char, escape_char * 2)
        .replace("%", f"{escape_char}%")
        .replace("_", f"{escape_char}_")
    )


# ---------------------------------------------------------------------------
# Shape 1: field_overdue — a date field on a row has passed `now`.
# Reproduces the original Rule 1 (eol_overdue).
# ---------------------------------------------------------------------------
def _rule_field_overdue(session, cfg: dict, now: datetime, thresholds: dict) -> list[tuple]:
    model = _model(cfg)
    date_field = cfg["date_field"]
    resource_fk = cfg["resource_fk"]
    host_field = cfg.get("host_field")
    severity = cfg.get("severity", "medium")
    template = cfg["detail_template"]
    name = cfg["name"]

    found: list[tuple] = []
    query = session.query(model, Resource).outerjoin(
        Resource, Resource.id == getattr(model, resource_fk)
    )
    for row, r in query.all():
        date_val = getattr(row, date_field)
        if date_val and _aware(date_val) < now:
            host = r.name if r else getattr(row, host_field)
            ctx = _template_ctx(row, {f"{date_field}_date": date_val.date()})
            found.append((name, host, severity, template.format(**ctx), r.id if r else None))
    return found


# ---------------------------------------------------------------------------
# Shape 2: sla_breach — a date field has passed, scoped to an "in-flight"
# status set, severity derived from a source field.
# Reproduces the original Rule 2 (vuln_sla_breach).
# ---------------------------------------------------------------------------
def _rule_sla_breach(session, cfg: dict, now: datetime, thresholds: dict) -> list[tuple]:
    model = _model(cfg)
    date_field = cfg["date_field"]
    resource_fk = cfg["resource_fk"]
    status_field = cfg.get("status_field")
    status_in = cfg.get("status_in")
    severity_field = cfg.get("severity_field")
    critical_severity = cfg.get("critical_severity", CRITICAL)
    default_severity = cfg.get("default_severity", "high")
    host_fallback = cfg.get("host_fallback_literal", "unknown")
    template = cfg["detail_template"]
    name = cfg["name"]

    query = session.query(model, Resource).outerjoin(
        Resource, Resource.id == getattr(model, resource_fk)
    )
    if status_field and status_in:
        query = query.filter(getattr(model, status_field).in_(status_in))

    found: list[tuple] = []
    for row, r in query.all():
        date_val = getattr(row, date_field)
        if date_val and _aware(date_val) < now:
            host = r.name if r else host_fallback
            raw_sev = getattr(row, severity_field) if severity_field else None
            sev = critical_severity if normalize_severity(raw_sev) == CRITICAL else default_severity
            ctx = _template_ctx(row)
            found.append((name, host, sev, template.format(**ctx), r.id if r else None))
    return found


# ---------------------------------------------------------------------------
# Shape 3: staleness — a status="open" row unresolved longer than a
# configurable day threshold, with optional Resource.domain exclusions.
# Reproduces the original Rule 3 (stale_drift).
# ---------------------------------------------------------------------------
def _rule_staleness(session, cfg: dict, now: datetime, thresholds: dict) -> list[tuple]:
    model = _model(cfg)
    date_field = cfg["date_field"]
    resource_fk = cfg["resource_fk"]
    status_field = cfg.get("status_field")
    status_value = cfg.get("status_value")
    exclude_domains = tuple(cfg.get("exclude_resource_domains", ()))
    # GitLab #162: field-level exclusions — live vSphere telemetry fields
    # (uptime_seconds, memory_usage_mb, cpu_usage_mhz, ...) change every
    # collection cycle by design and will therefore never naturally resolve,
    # so a domain-only exclusion is insufficient; a row must be skippable by
    # its own ``field`` value too.
    exclude_fields = tuple(cfg.get("exclude_fields", ()))
    exclude_field_prefixes = tuple(cfg.get("exclude_field_prefixes", ()))
    days_threshold = thresholds.get(cfg["days_threshold_key"], cfg.get("default_days", 14))
    severity = cfg.get("severity", "medium")
    template = cfg["detail_template"]
    name = cfg["name"]

    cutoff = now - timedelta(days=days_threshold)
    query = session.query(model, Resource).join(
        Resource, Resource.id == getattr(model, resource_fk)
    )
    if status_field and status_value:
        query = query.filter(getattr(model, status_field) == status_value)
    if exclude_domains:
        query = query.filter(Resource.domain.notin_(exclude_domains))

    found: list[tuple] = []
    for row, r in query.all():
        date_val = getattr(row, date_field)
        if not date_val or _aware(date_val) >= cutoff:
            continue
        row_field = getattr(row, "field", None)
        if row_field is not None:
            if row_field in exclude_fields:
                continue
            if exclude_field_prefixes and row_field.startswith(exclude_field_prefixes):
                continue
        ctx = _template_ctx(row, {"days_threshold": days_threshold})
        found.append((name, r.name, severity, template.format(**ctx), r.id))
    return found


# ---------------------------------------------------------------------------
# Shape 4: unaddressed_action — a row matches a threshold criterion AND no
# qualifying ProposedAction addressed its host within a day window.
# Reproduces the original Rule 4 (unaddressed_critical_cve), including the
# AA-C-5 anchored ``:{host}`` suffix match (never an unanchored substring).
# ``host_name`` is escaped via ``_escape_like()`` before being embedded in the
# LIKE pattern so a literal ``%``/``_`` in a hostname (docker/K8s container
# names, homelab hosts like ``web_01``) can't be misread as a wildcard and
# match an unrelated host's target.
# ---------------------------------------------------------------------------
def _rule_unaddressed_action(session, cfg: dict, now: datetime, thresholds: dict) -> list[tuple]:
    model = _model(cfg)
    criterion_field = cfg["criterion_field"]
    op_fn = _OPS[cfg.get("criterion_op", "gt")]
    criterion_value = cfg["criterion_value"]
    resource_fk = cfg["resource_fk"]
    hostname_field = cfg.get("hostname_field")
    action_type = cfg["action_type"]
    days_threshold = thresholds.get(cfg["days_threshold_key"], cfg.get("default_days", 30))
    severity = cfg.get("severity", "high")
    template = cfg["detail_template"]
    name = cfg["name"]

    action_cutoff = now - timedelta(days=days_threshold)
    rows = (
        session.query(model).filter(op_fn(getattr(model, criterion_field), criterion_value)).all()
    )

    found: list[tuple] = []
    for row in rows:
        resource_id = getattr(row, resource_fk)
        if not resource_id:
            continue
        resource = session.get(Resource, resource_id)
        fallback = (getattr(row, hostname_field, None) if hostname_field else None) or str(
            resource_id
        )
        host_name = resource.name if resource else fallback
        has_action = (
            session.query(ProposedAction)
            .filter(
                ProposedAction.action_type == action_type,
                ProposedAction.target.like(
                    f"%:{_escape_like(host_name)}", escape=_LIKE_ESCAPE_CHAR
                ),
                ProposedAction.created_at >= action_cutoff,
            )
            .first()
        )
        if not has_action:
            ctx = _template_ctx(row, {"days_threshold": days_threshold})
            found.append(
                (
                    name,
                    host_name,
                    severity,
                    template.format(**ctx),
                    resource.id if resource else None,
                )
            )
    return found


_RULE_TYPES: dict[str, RuleFn] = {
    "field_overdue": _rule_field_overdue,
    "sla_breach": _rule_sla_breach,
    "staleness": _rule_staleness,
    "unaddressed_action": _rule_unaddressed_action,
}


def evaluate_rules(
    session,
    rules_cfg: list[dict],
    thresholds: dict,
    now: datetime,
    scope: ReconcileScope | None = None,
) -> list[tuple[str, str, str, str, Any]]:
    """Evaluate every declared rule and return the combined ``found`` tuples.

    A single rule's config error or query failure is logged and skipped —
    never lets one bad rule take down the whole compliance pass (mirrors the
    existing fail-open posture of the gap-finder LLM path).

    H-2: pass a :class:`~infra_brain.etl.base.ReconcileScope` (keyed by rule
    ``name``) as ``scope`` and every rule that evaluates WITHOUT raising is
    recorded via ``scope.observed(name)``; a rule whose ``RuleFn`` raises is
    recorded via ``scope.failed(name, exc)`` instead of just being logged and
    silently dropped. The caller (``ComplianceAgent._reconcile``) must then
    scope its destructive resolution pass to ``scope.safe_scope`` — an empty
    ``found`` for a rule that raised means "this rule never ran", not "this
    rule found nothing", and must never be read as license to resolve every
    one of that rule's existing open violations. A rule that is cleanly
    skipped (``enabled: false``, a false ``enabled_key``, or an unknown
    ``type``) is left unmarked — that is an intentional, non-exceptional skip,
    not the swallowed-failure case this scope exists to guard against.
    ``scope`` is optional so direct callers/tests that don't reconcile need
    not construct one.
    """
    found: list[tuple[str, str, str, str, Any]] = []
    for cfg in rules_cfg:
        name = cfg.get("name", "<unnamed>")
        if not cfg.get("enabled", True):
            continue
        enabled_key = cfg.get("enabled_key")
        if enabled_key is not None and not _get_nested(thresholds, enabled_key, True):
            continue
        rule_type = cfg.get("type")
        fn = _RULE_TYPES.get(rule_type)
        if fn is None:
            logger.warning("Compliance rule %r: unknown type %r — skipping", name, rule_type)
            continue
        try:
            found.extend(fn(session, cfg, now, thresholds))
        except Exception as exc:
            logger.exception("Compliance rule %r failed to evaluate — skipping", name)
            if scope is not None:
                scope.failed(name, exc)
            continue
        if scope is not None:
            scope.observed(name)
    return found


# ---------------------------------------------------------------------------
# Default rule set — the original 4 hardcoded blocks, expressed declaratively.
# Used whenever compliance.yml has no ``rules:`` list (older config on disk),
# so upgrading this module never breaks an existing deployment's behavior.
# ---------------------------------------------------------------------------
DEFAULT_RULES: list[dict] = [
    {
        "name": "eol_overdue",
        "type": "field_overdue",
        "enabled_key": "eol_check.enabled",
        "model": "EolRegistry",
        "date_field": "eol_date",
        "resource_fk": "resource_id",
        "host_field": "asset_name",
        "severity": "high",
        "detail_template": "{asset_name} reached EOL on {eol_date_date}",
    },
    {
        "name": "vuln_sla_breach",
        "type": "sla_breach",
        "model": "VulnQueueItem",
        "date_field": "sla_due",
        "resource_fk": "resource_id",
        "status_field": "status",
        "status_in": ["open", "triage"],
        "severity_field": "severity",
        "critical_severity": "critical",
        "default_severity": "high",
        "host_fallback_literal": "unknown",
        "detail_template": "{cve_id} past SLA ({severity})",
    },
    {
        "name": "stale_drift",
        "type": "staleness",
        "model": "DriftEvent",
        "date_field": "detected_at",
        "resource_fk": "resource_id",
        "status_field": "status",
        "status_value": "open",
        "days_threshold_key": "stale_drift_days",
        "default_days": 14,
        # Home-lab media-server rows (HomelabServicesAgent, domain=
        # "homelab_services") are intentionally excluded here: a media-server
        # reachability blip (radarr/sonarr/emby/... briefly down) must never
        # read as a security/compliance/PCI signal. Structurally this domain
        # is NOT a SKIP_HOOK collector, so its resources DO flow through the
        # generic full-fleet DriftDetector (agents/drift.py) like any other
        # domain — without this exclusion, a stuck-down homelab_service
        # resource with an open drift event older than stale_drift_days would
        # genuinely mint a ComplianceViolation. Verified live (not
        # theoretical) before this fix; see
        # tests/agents/test_compliance_rule_engine.py::test_stale_drift_excludes_homelab_services.
        #
        # ``fleet_health`` is excluded for the same reason remediation.py's
        # ``_BOOKKEEPING_DOMAINS`` added it (GitLab #142): its entire snapshot
        # IS collector telemetry, not fleet infrastructure — 72 pending
        # proposals were found against fleet_health's own health-snapshot
        # resource on ``domain_freshness.*`` collector-heartbeat counters
        # alone. Without this exclusion, an open drift event on a
        # fleet_health resource older than stale_drift_days would genuinely
        # mint a ComplianceViolation for pure collector-health bookkeeping;
        # see
        # tests/agents/test_compliance_rule_engine.py::test_stale_drift_excludes_fleet_health.
        "exclude_resource_domains": [
            "compliance",
            "graph_maintenance",
            "homelab_services",
            "fleet_health",
        ],
        # GitLab #162: live vSphere telemetry fields change every collection
        # cycle by design and will therefore never naturally resolve — must
        # match rules/enforcement/compliance.yml's stale_drift entry exactly
        # (see tests/agents/test_compliance_rule_engine.py's lockstep test).
        "exclude_fields": sorted(TELEMETRY_FIELDS | DERIVED_METRIC_FIELDS),
        # GitLab #142/#162: belt-and-braces field-prefix exclusion — mirrors
        # remediation.py's is_telemetry_field() prefix check so fleet_health's
        # own domain_freshness.* collector-heartbeat counters are skipped by
        # field name too, not just by the domain exclusion above (matches the
        # ``field.startswith(...)`` behavior _rule_staleness already supports).
        "exclude_field_prefixes": list(TELEMETRY_FIELD_PREFIXES),
        "severity": "medium",
        "detail_template": "{field} drift open >{days_threshold}d",
    },
    {
        "name": "unaddressed_critical_cve",
        "type": "unaddressed_action",
        "model": "R7Asset",
        "criterion_field": "vuln_critical",
        "criterion_op": "gt",
        "criterion_value": 0,
        "resource_fk": "resource_id",
        "hostname_field": "hostname",
        "action_type": "vuln_patch",
        "days_threshold_key": "unaddressed_critical_cve_days",
        "default_days": 30,
        "severity": "critical",
        "detail_template": (
            "CRITICAL vuln band with no ProposedAction in {days_threshold}d"
            " (vuln_critical={vuln_critical})"
        ),
    },
]
