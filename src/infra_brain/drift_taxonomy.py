"""Shared taxonomy of DriftEvent field classifications, plus the human-readable
rendering of a drift event (``summarize_drift`` / ``describe_drift_rule``).

GitLab #162: this taxonomy previously existed in two independent, drifted
forms:

- ``agents/remediation.py`` (GitLab #142) defined ``DERIVED_METRIC_FIELDS``,
  ``TELEMETRY_FIELDS``, ``_TELEMETRY_FIELD_PREFIXES`` and
  ``_is_telemetry_drift()`` to suppress remediation-proposal drafting for
  scanner-computed metrics and live operational telemetry.
- ``agents/compliance_rules.py``'s ``stale_drift`` rule (the ``staleness``
  shape / ``_rule_staleness``) had **no field-level exclusion at all** — only
  a resource-domain exclusion (``exclude_resource_domains``) — so it fired
  permanently on the exact same live-telemetry fields remediation.py already
  knew to ignore (``uptime_seconds``, ``memory_usage_mb``, ``cpu_usage_mhz``),
  because those fields change every collection cycle by design and can never
  naturally "resolve".

Also fixed here: ``remediation.py``'s ``TELEMETRY_FIELDS`` used unsuffixed
spellings (``uptime``, ``memory_usage``) that never matched the real
``DriftEvent.field`` values written by ``agents/vsphere.py``
(``uptime_seconds``, ``memory_usage_mb``, ``cpu_usage_mhz``, ...) — so
remediation's own telemetry filter was silently missing its intended targets
too. This module is now the single source of truth for both consumers.
"""

from __future__ import annotations

import json
from typing import Any

# Fields whose value is COMPUTED by a scanner from scan results, never SET by
# a human/IaC — a decrease is often an improvement, not something to "revert".
DERIVED_METRIC_FIELDS: frozenset[str] = frozenset({"risk_score", "vulnerabilities"})

# Live vSphere operational telemetry that naturally varies every collection
# cycle by design — these are the actual DriftEvent.field / column names
# written by agents/vsphere.py's _upsert_resource_pool / _append_vm_metric /
# _append_host_metric (cpu_usage_mhz, memory_usage_mb, uptime_seconds,
# overall_status), plus esxi_host (VM host placement, changes on vMotion).
# NOT configuration drift in the remediation or compliance sense — it will
# never "resolve" because it is expected to keep changing forever.
TELEMETRY_FIELDS: frozenset[str] = frozenset(
    {
        "uptime_seconds",
        "cpu_usage_mhz",
        "memory_usage_mb",
        "overall_status",
        "esxi_host",
    }
)

# GitLab #142: field-name prefixes that identify collector-health bookkeeping
# (freshness/age/heartbeat counters) regardless of which resource they appear
# on — belt-and-braces on top of any domain-level exclusion.
TELEMETRY_FIELD_PREFIXES: tuple[str, ...] = ("domain_freshness",)


def is_derived_metric_field(field: str) -> bool:
    """Is *field* a scanner-computed derived metric (e.g. Rapid7 risk_score),
    as opposed to a human/IaC-managed configuration setting?"""
    return field in DERIVED_METRIC_FIELDS


def is_telemetry_field(field: str) -> bool:
    """Is *field* live operational telemetry that naturally varies every
    collection cycle by design (uptime, cpu/memory usage, host placement,
    collector-health heartbeat counters) — never "configuration drift" that
    could resolve on its own?
    """
    return field in TELEMETRY_FIELDS or field.startswith(TELEMETRY_FIELD_PREFIXES)


# Combined set of fields that should never generate a remediation proposal or
# a stale_drift compliance finding — telemetry OR scanner-derived metrics.
NEVER_ACTIONABLE_FIELDS: frozenset[str] = TELEMETRY_FIELDS | DERIVED_METRIC_FIELDS


# ===========================================================================
# Human-readable rendering of a drift event
# ===========================================================================
#
# Motivating complaint, from the maintainer, verbatim: *"I don't understand
# what drifted or why they are flagged as drift."* The dashboard rendered
# ``drift_type`` / ``field`` / ``old_value`` / ``new_value`` raw, so a row read
# ``config_drift / agent_name / {'v': 'vmi-example-deploy'} / {'v': 'node_a'}``.
#
# Two answers live here, deliberately together:
#   * ``summarize_drift`` — WHAT changed, as one plain sentence.
#   * ``describe_drift_rule`` — WHY it was flagged, from a static rule map.
#
# ## Why this is a READ-time pure function, not a stored column
#
# A ``drift_events.summary`` column would need a migration, would need a
# backfill for the ~990 existing rows, and — the decisive objection — would go
# permanently stale the moment any phrasing here improves, since nothing
# rewrites historical rows. Deriving on read costs one dict lookup per row and
# is always current. It also keeps this module dependency-free (stdlib only,
# no SQLAlchemy import), so both ``api/routers/governance_drift.py`` and
# ``agents/notification.py`` can call it without an import cycle.
#
# ## Every payload shape below was inventoried, not guessed
#
# Sources: the live ``drift_events`` table (990 rows: config_drift 963,
# new_listening_port 26, potential_secret_in_iac 1) AND every ``DriftEvent(...)``
# construction site in ``src/`` — the live table only exercises 3 of the 12
# types the code can emit. Writers, with their exact payloads:
#
#   agents/drift.py                config_drift          {"v": <scalar>} both sides
#   agents/linux.py                new_listening_port    None / {"port": int}
#   agents/windows.py              new_windows_service   None / {"service_name": str}
#   agents/windows.py              service_stopped       {"state": str} both sides,
#                                                        name lives in field
#                                                        ("service:<name>")
#   agents/netdiscovery.py         threat_escalation     {"threat_level": str} both
#   agents/netdiscovery.py         shadow_it_discovered  None / {"ip", "hostname"}
#   agents/netdiscovery.py         dangerous_service_discovered   None /
#   agents/netdiscovery.py         suspicious_service_discovered  {"port","proto",
#                                                                  "service","banner"}
#   agents/iac.py                  potential_secret_in_iac  None / {"file_path",
#                                                        "secret_type","confidence_tier"}
#   agents/host_reconcile.py       identity_conflict     TWO shapes under ONE type:
#                                                        ip:  {"ip","source"} both
#                                                        host:{"short_hostname","source",
#                                                              "kept_*"/"dropped_*",
#                                                              "conflict_class"}
#   agents/host_reconcile.py       identity_conflict_suffix_variant   host shape
#   agents/host_reconcile.py       identity_conflict_distinct_object  host shape
#   (legacy, no writer)            state_drift/presence  bulk-resolved by
#                                                        drift.py::detect_state_drift
#   mcp_server.py::seed_drift_event  <caller-chosen>     {"value": ...} + severity/
#                                                        source/note
#
# Anything not on that list — a future collector, a hand-seeded row, a
# malformed payload — takes the honest generic fallback at the bottom
# ("<field> changed" plus the raw values). ``summarize_drift`` never raises:
# it is called once per row inside a response serialiser, where an exception
# would blank the whole Drift page rather than one cell.

# Longest scalar rendered inside a summary sentence before it is elided. The
# summary is a one-line table cell, not a value dump — the drawer's before/
# after diff (fed by ``render_drift_value``) is where full values belong.
_SUMMARY_VALUE_CHARS = 80

# Ceiling for ``render_drift_value``. Generous (the drawer shows full values)
# but not unbounded — old_value/new_value are free-form JSONB.
_DISPLAY_VALUE_CHARS = 4000

# Distinguishes "this payload carries no writer envelope" from "the envelope's
# value is None" — a config_drift key ADDITION legitimately stores {"v": None}.
_NO_ENVELOPE = object()


def _render(value: Any, limit: int) -> str:
    """Render an arbitrary JSONB leaf as a bounded single-line string.

    Total-function by construction: every branch is guarded, because the input
    is whatever a collector once wrote into a JSONB column (including values
    whose own ``__str__`` raises).
    """
    try:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, default=str, sort_keys=True)
            except Exception:
                text = str(value)
        text = " ".join(text.split())
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return text
    except Exception:
        return "(unrenderable value)"


def _scalar(value: Any) -> str:
    """``_render`` at summary-sentence width."""
    return _render(value, _SUMMARY_VALUE_CHARS)


def _unwrap(raw: Any) -> Any:
    """Peel the writer-specific envelope off a drift payload.

    ``{"v": x}`` is DriftDetector's signature shape (``agents/drift.py`` relies
    on its uniqueness for the TRK-256 auto-resolve check); ``{"value": x, ...}``
    is what ``mcp_server.py::seed_drift_event`` writes. Returns ``_NO_ENVELOPE``
    when neither applies, so callers can fall through to the generic branch
    instead of mistaking a structured payload for a scalar.
    """
    if isinstance(raw, dict):
        if len(raw) == 1 and "v" in raw:
            return raw["v"]
        if "value" in raw:
            return raw["value"]
    return _NO_ENVELOPE


def render_drift_value(raw: Any) -> str:
    """Render one side of a drift event for the drawer's before/after diff.

    Unwraps the writer envelope so the operator sees ``vmi-example-deploy`` rather than
    ``{'v': 'vmi-example-deploy'}``; a structured payload with no envelope (e.g.
    ``{"port": 8443}``) renders as compact JSON, which is the honest rendering
    of a genuinely structured value. ``None`` → ``""``.
    """
    unwrapped = _unwrap(raw)
    return _render(raw if unwrapped is _NO_ENVELOPE else unwrapped, _DISPLAY_VALUE_CHARS)


def _on(hostname: str) -> str:
    """The trailing `` on <host>`` clause, omitted when there is no hostname."""
    return f" on {hostname}" if hostname else ""


def _dict(value: Any) -> dict:
    """*value* if it is a dict, else an empty dict — lets every shape-specific
    branch below read keys without a type check at each access."""
    return value if isinstance(value, dict) else {}


# conflict_class → the parenthetical that tells an operator what KIND of
# collision this is (agents/host_reconcile.py's own classification).
_CONFLICT_CLASS_NOTES = {
    "suffix_variant": "same host spelled two ways",
    "distinct_object": "genuinely different objects — a clone, replica or restore",
    "unclassified": "collision could not be classified",
}


def _summarize_config_drift(field: str, old: Any, new: Any, hostname: str) -> str | None:
    """The 963-of-990 case. ``None`` means "fall through to the generic branch"."""
    old_v, new_v = _unwrap(old), _unwrap(new)
    if old_v is _NO_ENVELOPE or new_v is _NO_ENVELOPE:
        return None

    # A 64-char hex digest pair is pure noise; what the operator needs to know
    # is that the file's content moved. (local_docs/content_hash, live.)
    if (field == "content_hash" or field.endswith("_hash")) and (
        isinstance(old_v, str) and isinstance(new_v, str) and old_v and new_v
    ):
        return f"{hostname or field} content changed (hash {old_v[:8]}… → {new_v[:8]}…)"

    # A bare "200 → 525" means nothing unless you already know the field holds
    # an HTTP status code. (homelab_services/http_status, live.)
    if field == "http_status" and hostname and old_v is not None and new_v is not None:
        return f"{hostname} started returning HTTP {_scalar(new_v)} (was HTTP {_scalar(old_v)})"

    # DeepDiff dictionary_item_added / dictionary_item_removed both arrive as a
    # {"v": None} on the absent side — phrase them as addition/removal rather
    # than as a change "from nothing".
    if old_v is None and new_v is not None:
        return f"{field} was added{_on(hostname)} (now {_scalar(new_v)})"
    if old_v is not None and new_v is None:
        return f"{field} was removed{_on(hostname)} (was {_scalar(old_v)})"
    if old_v is None and new_v is None:
        return f"{field} changed{_on(hostname)}"

    return f"{field} changed from {_scalar(old_v)} to {_scalar(new_v)}{_on(hostname)}"


def _summarize_identity_conflict(
    drift_type: str, field: str, old: Any, new: Any, hostname: str
) -> str | None:
    """``identity_conflict`` covers TWO structurally different payloads.

    ``host_reconcile.py::_upsert_ip_conflict_event`` writes the IP shape under
    the flat type; ``_emit_identity_conflict_events`` writes the hostname shape
    under the flat type AND under both sub-classed types. Dispatch on the
    payload keys, not on the type string alone.
    """
    old_d, new_d = _dict(old), _dict(new)

    if field == "ip_address" or ("ip" in new_d and "short_hostname" not in new_d):
        old_ip, new_ip = old_d.get("ip"), new_d.get("ip")
        if old_ip is None or new_ip is None:
            return None
        return (
            f"{hostname or 'this host'} has conflicting IP addresses across sources: "
            f"{_scalar(old_d.get('source')) or 'one source'} reports {_scalar(old_ip)}, "
            f"{_scalar(new_d.get('source')) or 'another'} reports {_scalar(new_ip)}"
        )

    if "short_hostname" not in new_d:
        return None

    short = _scalar(new_d.get("short_hostname")) or hostname or "this host"
    source = _scalar(new_d.get("source") or old_d.get("source")) or "one collector's"
    kept = _scalar(old_d.get("kept_hostname")) or "one record"
    dropped = _scalar(new_d.get("dropped_hostname")) or "another"
    conflict_class = new_d.get("conflict_class") or drift_type.rsplit("identity_conflict_", 1)[-1]
    note = _CONFLICT_CLASS_NOTES.get(conflict_class, _CONFLICT_CLASS_NOTES["unclassified"])
    return (
        f"{short} matched two different {source} records — kept {kept}, dropped {dropped} ({note})"
    )


def _summarize(drift_type: str, field: str, old: Any, new: Any, hostname: str) -> str:
    new_d = _dict(new)
    old_d = _dict(old)

    if drift_type == "config_drift":
        sentence = _summarize_config_drift(field, old, new, hostname)
        if sentence:
            return sentence

    elif drift_type == "new_listening_port":
        port = new_d.get("port")
        if port is not None:
            return (
                f"{hostname or 'this host'} started listening on port "
                f"{_scalar(port)} (was not listening)"
            )

    elif drift_type == "new_windows_service":
        name = new_d.get("service_name")
        if name:
            return (
                f"new Windows service {_scalar(name)} appeared{_on(hostname)} (was not installed)"
            )

    elif drift_type == "service_stopped":
        old_state, new_state = old_d.get("state"), new_d.get("state")
        if old_state is not None and new_state is not None:
            # The service name is carried in `field` as "service:<name>", not
            # in the payload (agents/windows.py::_emit_service_drift).
            name = field.split(":", 1)[1] if ":" in field else field
            return (
                f"Windows service {name} is no longer running{_on(hostname)} "
                f"({_scalar(old_state)} → {_scalar(new_state)})"
            )

    elif drift_type == "threat_escalation":
        old_t, new_t = old_d.get("threat_level"), new_d.get("threat_level")
        if old_t is not None and new_t is not None:
            return (
                f"threat level for {hostname or 'this host'} escalated from "
                f"{_scalar(old_t)} to {_scalar(new_t)}"
            )

    elif drift_type == "shadow_it_discovered":
        ip = new_d.get("ip")
        if ip:
            resolved = new_d.get("hostname")
            named = f" ({_scalar(resolved)})" if resolved and resolved != ip else ""
            return (
                f"shadow-IT host discovered at {_scalar(ip)}{named} — responding on "
                f"the network but not in inventory"
            )

    elif drift_type in ("dangerous_service_discovered", "suspicious_service_discovered"):
        port = new_d.get("port")
        if port is not None:
            kind = "dangerous" if drift_type.startswith("dangerous") else "suspicious"
            # nmap frequently reports no service name for an unknown port.
            service = _scalar(new_d.get("service"))
            named = f" {service}" if service else ""
            proto = _scalar(new_d.get("proto")) or "tcp"
            return f"{kind} service{named} found on port {_scalar(port)}/{proto}{_on(hostname)}"

    elif drift_type == "potential_secret_in_iac":
        path, secret_type = new_d.get("file_path"), new_d.get("secret_type")
        if path and secret_type:
            # The file path already identifies the resource, so no `on <host>`
            # clause. The matched literal is NEVER recorded by agents/iac.py and
            # is never read back here, even if a payload carries a stray key.
            tier = _scalar(new_d.get("confidence_tier")) or "unrated"
            return (
                f"possible {_scalar(secret_type)} committed in IaC file "
                f"{_scalar(path)} ({tier} confidence)"
            )

    elif drift_type.startswith("identity_conflict"):
        sentence = _summarize_identity_conflict(drift_type, field, old, new, hostname)
        if sentence:
            return sentence

    elif drift_type == "state_drift" and field == "presence":
        # GitLab #137: no longer written, and bulk-resolved on every pass — but
        # historical rows are still reachable via the "All" status tab, and an
        # unexplained one reads as a live finding.
        return (
            f"{hostname or 'this resource'} was absent from its domain's last "
            f"collection run (legacy retirement bookkeeping — no longer written)"
        )

    # ---- Honest generic fallback -------------------------------------------
    # Reached by an unknown drift_type, or by a known type whose payload did
    # not carry the keys its writer normally supplies (a hand-seeded row, a
    # collector mid-change). Says only what is certainly true — the field
    # changed — and shows the raw values rather than inventing a reading.
    label = field or "a field"
    old_raw, new_raw = _scalar(old), _scalar(new)
    if old is not None and new is not None:
        return f"{label} changed{_on(hostname)}: {old_raw} → {new_raw}"
    if new is not None:
        return f"{label} recorded{_on(hostname)}: {new_raw}"
    if old is not None:
        return f"{label} cleared{_on(hostname)} (was {old_raw})"
    if drift_type:
        return f"{label} changed{_on(hostname)} ({drift_type})"
    return f"{label} changed{_on(hostname)}"


def summarize_drift(
    drift_type: str,
    field: str,
    old_value: Any,
    new_value: Any,
    hostname: str = "",
) -> str:
    """One plain-English sentence describing what this drift event means.

    Pure, total, and free of I/O — safe to call once per row inside a response
    serialiser. Arguments map 1:1 onto ``DriftEvent`` columns plus the joined
    ``Resource.name``.

    Never raises. It is called in a loop over a page of rows; letting a single
    malformed JSONB payload propagate would blank the entire Drift page — the
    exact failure mode this function exists to fix.
    """
    try:
        return _summarize(
            str(drift_type or ""),
            str(field or ""),
            old_value,
            new_value,
            str(hostname or ""),
        )
    except Exception:
        try:
            return f"{field or 'a field'} changed (details unavailable)"
        except Exception:
            return "a field changed (details unavailable)"


# ---------------------------------------------------------------------------
# "Why is this flagged as drift?" — the other half of the complaint.
#
# One line per drift_type, naming the RULE and the collector that applied it.
# Keyed by every drift_type any writer in src/ can emit; the parity guard is
# tests/test_drift_summary.py::test_every_writable_drift_type_has_a_rule_explanation,
# which fails if a collector gains a new type without an entry here.
# ---------------------------------------------------------------------------

DRIFT_RULE_EXPLANATIONS: dict[str, str] = {
    "config_drift": (
        "Config drift — DriftDetector diffed this resource's newest collected snapshot "
        "against its previous one and found this tracked field had a different value."
    ),
    "new_listening_port": (
        "Port drift — the Linux collector saw this TCP/UDP port listening in the current "
        "scan of this host, and it was not listening in the previous scan."
    ),
    "new_windows_service": (
        "Service inventory drift — the Windows collector found a service installed on this "
        "host that was absent from the previous scan."
    ),
    "service_stopped": (
        "Service state drift — the Windows collector found a service that was Running in "
        "the previous scan is no longer running."
    ),
    "threat_escalation": (
        "Threat escalation — netdiscovery re-scored this discovered host into a strictly "
        "higher threat level than it previously held."
    ),
    "shadow_it_discovered": (
        "Shadow IT — netdiscovery found a host responding on the network with no matching "
        "record in inventory, on its first observation."
    ),
    "dangerous_service_discovered": (
        "Dangerous service — netdiscovery found a newly-exposed service on a port from its "
        "known-dangerous port list."
    ),
    "suspicious_service_discovered": (
        "Suspicious service — netdiscovery found a newly-exposed service whose banner or "
        "fingerprint matched one of its suspicious signatures."
    ),
    "potential_secret_in_iac": (
        "Secret scan — the IaC collector's scanner matched a credential-shaped literal in a "
        "committed infrastructure file. The matched value itself is never recorded."
    ),
    "identity_conflict": (
        "Identity conflict — host_reconcile found two collected records that resolve to the "
        "same host but disagree, and could not classify how they differ."
    ),
    "identity_conflict_suffix_variant": (
        "Identity conflict (suffix variant) — host_reconcile found two records for one host "
        "whose DNS suffixes differ; almost always one machine spelled two ways."
    ),
    "identity_conflict_distinct_object": (
        "Identity conflict (distinct object) — host_reconcile found two records agreeing on "
        "the guest hostname but backed by different source objects (clone, DR replica, restore)."
    ),
    "state_drift": (
        "Legacy retirement bookkeeping — no longer written; DriftDetector bulk-resolves any "
        "remaining open rows of this type on every pass (GitLab #137)."
    ),
}


def describe_drift_rule(drift_type: str) -> str:
    """The one-line answer to "why is this flagged as drift?".

    Falls back honestly for a type not in the map rather than inventing a rule
    — a hand-seeded row (``mcp_server.py::seed_drift_event`` accepts any
    ``drift_type``) or a collector added after this map was last touched.
    """
    key = str(drift_type or "")
    known = DRIFT_RULE_EXPLANATIONS.get(key)
    if known:
        return known
    if key:
        return (
            f"Raised as '{key}' by a collector with no entry in the drift rule map — "
            f"the drift_type is the only rule name available for this event."
        )
    return "This event carries no drift_type, so the rule that raised it cannot be identified."
