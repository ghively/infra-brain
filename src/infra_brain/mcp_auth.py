"""Scoped MCP API-key auth: hashing, token generation, and CRUD helpers.

Replaces the single global INFRA_BRAIN_MCP_TOKEN. The raw token is NEVER
stored — only its sha256 hex digest (matches the fingerprinting convention in
dashboard_auth.py; the right tool for a high-entropy random token, unlike
bcrypt which is for low-entropy human passwords). Every helper takes an
explicit Session so it composes with both the MCP auth middleware and the
dashboard router.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from infra_brain.db.models import McpApiKey

logger = logging.getLogger(__name__)

# ── Canonical tool-name catalog ───────────────────────────────────────────────
# Enforcement (Task 4) only needs "is this name in allowed_tools" — this catalog
# exists so the dashboard multi-select can group/offer "all read-only" / "all
# mutation" toggles. Tool-expansion batches (spec Part 2) append their new tool
# names here as they land.
#
# This catalog is ALSO load-bearing for enforcement: mcp_server's auth
# middleware 403s any tool whose name is absent, and api/schemas.py refuses to
# create a key naming one. A tool registered in mcp_server.py but missing here
# is therefore unreachable by every key, including the full-access bootstrap
# key. `tests/test_mcp_auth_helpers.py::test_catalog_matches_registered_mcp_tools`
# fails the build if the two ever diverge again (see TRK-231).
READONLY_TOOL_NAMES: list[str] = [
    "query_resources",
    "get_drift_events",
    "get_vulnerabilities",
    "get_eol_status",
    "get_remediation_suggestions",
    "get_inventory_gaps",
    "get_instincts",
    "query_nl",
    "get_collection_health",
    "search_knowledge",
    # Batch A — cross-domain (spec Part 2, 2026-07-23-mcp-auth-and-tool-expansion-design.md)
    "get_host_profile",
    "get_host_vulns",
    "get_host_purpose_map",
    "get_fleet_counts",
    # Batch B — vSphere
    "get_vsphere_overview",
    "get_vsphere_vms",
    "get_vsphere_hosts",
    "get_vsphere_datastores",
    "get_vsphere_snapshots",
    "get_vsphere_clusters",
    "get_vsphere_alarms",
    "get_vsphere_permissions",
    # Batch D — Rapid7 / vuln (GitLab #48). Octopus/Rapid7-branded tools
    # (get_octopus_*, get_r7_sites, get_r7_tags) removed from the
    # agent-facing surface (P7.1a, D6/D11) -- the backend collectors/models
    # stay dormant; these four are domain-agnostic vuln tools, kept.
    "get_cve_detail",
    "get_software_inventory",
    "get_remediation_solutions",
    "get_asset_detail",
    # Batch E — host posture (PCI-relevant)
    "get_host_certificates",
    "get_host_security_posture",
    "get_host_firewall_rules",
    "get_host_shares",
    "get_windows_local_admins",
    # Batch F — OS inventory (GitLab #50)
    "get_linux_packages",
    "get_linux_pending_updates",
    "get_linux_ports",
    "get_linux_mounts_and_nics",
    "get_linux_users_and_crons",
    "get_windows_services",
    "get_windows_software",
    # Batch G — network / cloud / k8s (GitLab #51)
    "get_network_discoveries",
    "get_network_devices",
    "get_cloud_resources",
    "get_k8s_resources",
    # Batch H — GitLab / IaC / CI-CD (GitLab #52)
    "get_cicd_overview",
    "get_iac_files",
    "get_ci_schedules",
    "get_parsed_iac_resources",
    "get_ansible_inventory",
    # Batch I — governance / compliance (GitLab #53)
    "get_compliance_violations",
    "get_drift_trend",
    "get_notifications",
    "get_agent_roster",
    "get_sweep_status",
    "get_scan_schedule",
    # Batch J — internal governance (GitLab #54)
    "get_audit_log",
    "get_agent_activity",
    "get_agent_decisions",
    "get_agent_config_status",
    "get_settings",
    "get_tool_catalog",  # #197: machine-readable allowed-actions catalog
    # Batch K — knowledge / learning (issue #55)
    "get_documents",
    "get_observations",
    # Batch A (cont.) — pre-assembled host context (GitLab #131)
    "get_host_context",
    "get_recent_changes",
    # Batch A (cont.) — forecast/lead-time detection (GitLab #131, other half)
    "get_utilization_forecast",
    # Batch K (cont.) — relationship-graph traversal (GitLab #127, Phase 3)
    "get_blast_radius",
    "get_root_cause_candidates",
    "get_reconciliation_state",
    # Phase 2 (2026-07-29 implementation plan, TRK-247 mitigation) — surfaces
    # every manual/MCP-authored write (root_cause_notes + proposed_actions
    # compliance-gap rows) so direct-invocation writes are findable
    # retroactively, purely by reading server-generated provenance markers.
    "get_manual_writes",
    # Phase 4 state backend (convergence plan P4.1-P4.3) — read paths.
    "get_environment_notes",
    "get_instinct_history",
    "get_governance_events",
    "verify_governance_chain",
    # Batch M — backup / DR-drill posture (GitLab #96)
    "get_backup_status",
    # Batch N — home-lab service status by category
    "get_homelab_service_category",
]

MUTATION_TOOL_NAMES: list[str] = [
    "trigger_collection",
    "seed_resource",
    "seed_resources_bulk",
    "seed_drift_event",
    "seed_vulnerability",
    "get_seeded_resources",
    "approve_proposal",
    "reject_proposal",
    "promote_instinct",
    "add_eol_product",
    # Phase 3 relationship graph (GitLab #127) — human-in-the-loop identity
    # decisions, positive and negative; each is additionally gated by
    # _mutations_enabled() in its own tool body in mcp_server.py.
    "confirm_same_as",
    "retract_same_as",
    "reject_same_as",
    "retract_not_same_as",
    # Manual reasoner-tier writes — every row they write is permanently marked
    # as manual/MCP-authored (never presented as agent LLM output); each is
    # additionally gated by _mutations_enabled() in its own tool body.
    "record_rootcause_note",
    "record_compliance_gap",
    # Phase 2.3 (2026-07-29 implementation plan, TRK-247(b) mitigation) — the
    # bulk sibling of record_rootcause_note: N items, previewed by default
    # (dry_run=True), hard-capped at 100/call, one savepoint per item on
    # execute. Same provenance/attribution guarantees as record_rootcause_note.
    "record_rootcause_notes_bulk",
    # Batch L — audited, predicate-scoped batch closure (GitLab #144). The ONLY
    # sanctioned way to reduce open_drift / open compliance violations. Each
    # refuses an unscoped call (a narrowing predicate is mandatory), previews by
    # default (dry_run=True), caps rows per call, and writes its own
    # agent_action_log record in the same transaction as the status flips; each
    # is additionally gated by _mutations_enabled() in its own tool body.
    "resolve_drift_events",
    "close_compliance_violations",
    # GitLab #161 — the bulk siblings of approve_proposal/reject_proposal, for
    # the 7k-row pending queue that is untriageable one row at a time. Same
    # Batch L machinery (mandatory narrowing predicate, dry_run=True default,
    # per-call cap, per-item SAVEPOINT, in-transaction agent_action_log row);
    # per-item guards are shared verbatim with action_decisions, so the bulk
    # path is never more permissive than the single-row one, and
    # entity_resolution_same_as rows are hard-excluded from both. Each is
    # additionally gated by _mutations_enabled() in its own tool body.
    "bulk_reject_proposals",
    "bulk_approve_proposals",
    # Phase 4 state backend (convergence plan P4.1-P4.3) — each additionally
    # gated by _mutations_enabled() in its own tool body in mcp_server.py.
    "record_environment_note",
    "resolve_environment_note",
    "promote_instinct_v2",
    "rollback_instinct",
    "propose_instinct",
    "record_client_state",
    "record_observation",
    "record_governance_event",
    "ingest_document",
    "update_document_metadata",
    "create_gitlab_issue",
    "comment_on_gitlab_issue",
]

ALL_TOOL_NAMES: list[str] = READONLY_TOOL_NAMES + MUTATION_TOOL_NAMES


def write_scope_tool_table() -> str:
    """Render MUTATION_TOOL_NAMES as a markdown table (GitLab #167).

    "Which tools need write scope?" is answered by exactly one place —
    ``MUTATION_TOOL_NAMES`` above — and this renders it on demand rather than
    restating it in prose that would silently drift. A key whose
    ``allowed_tools`` omits a name below gets a 403 with
    ``reason="key_lacks_scope"`` from ``mcp_server._authorize``; note that even
    a key scoped to one of these still cannot call it unless
    ``INFRA_BRAIN_MCP_ENABLE_MUTATIONS`` is set (a separate, independent gate
    inside each tool body — see ``mcp_server._mutation_disabled_response``).
    """
    lines = ["| Tool | Required scope |", "| --- | --- |"]
    lines += [f"| `{name}` | write (mutation) |" for name in MUTATION_TOOL_NAMES]
    return "\n".join(lines)


# ── Domain groups (dashboard tool-picker UI only, not an enforcement layer) ──
# Mirrors the "Batch" comments above — every name in ALL_TOOL_NAMES appears in
# exactly one group here. `test_tool_groups_cover_all_tool_names` fails the
# build if this ever drifts from READONLY_TOOL_NAMES/MUTATION_TOOL_NAMES.
TOOL_GROUPS: dict[str, list[str]] = {
    "Core": [
        "query_resources",
        "get_drift_events",
        "get_vulnerabilities",
        "get_eol_status",
        "get_remediation_suggestions",
        "get_inventory_gaps",
        "get_instincts",
        "query_nl",
        "get_collection_health",
        "search_knowledge",
    ],
    "Cross-domain host context": [
        "get_host_profile",
        "get_host_vulns",
        "get_host_purpose_map",
        "get_fleet_counts",
        "get_host_context",
        "get_recent_changes",
        "get_utilization_forecast",
    ],
    "vSphere": [
        "get_vsphere_overview",
        "get_vsphere_vms",
        "get_vsphere_hosts",
        "get_vsphere_datastores",
        "get_vsphere_snapshots",
        "get_vsphere_clusters",
        "get_vsphere_alarms",
        "get_vsphere_permissions",
    ],
    "Vulnerabilities (CVE)": [
        "get_cve_detail",
        "get_software_inventory",
        "get_remediation_solutions",
        "get_asset_detail",
    ],
    "Host posture (PCI)": [
        "get_host_certificates",
        "get_host_security_posture",
        "get_host_firewall_rules",
        "get_host_shares",
        "get_windows_local_admins",
    ],
    "OS inventory": [
        "get_linux_packages",
        "get_linux_pending_updates",
        "get_linux_ports",
        "get_linux_mounts_and_nics",
        "get_linux_users_and_crons",
        "get_windows_services",
        "get_windows_software",
    ],
    "Network / cloud / k8s": [
        "get_network_discoveries",
        "get_network_devices",
        "get_cloud_resources",
        "get_k8s_resources",
    ],
    "GitLab / IaC / CI-CD": [
        "get_cicd_overview",
        "get_iac_files",
        "get_ci_schedules",
        "get_parsed_iac_resources",
        "get_ansible_inventory",
    ],
    "Governance / compliance": [
        "get_compliance_violations",
        "get_drift_trend",
        "get_notifications",
        "get_agent_roster",
        "get_sweep_status",
        "get_scan_schedule",
    ],
    "Internal governance": [
        "get_audit_log",
        "get_agent_activity",
        "get_agent_decisions",
        "get_agent_config_status",
        "get_settings",
        "get_tool_catalog",
        # Moved here from "State backend..." below: both are pure reads
        # (READONLY_TOOL_NAMES already lists them) that were previously
        # bucketed with that group's mutation tools, so a group-based key
        # grant in the dashboard could omit them while looking complete.
        "get_governance_events",
        "verify_governance_chain",
    ],
    "Knowledge / relationship graph": [
        "get_documents",
        "get_observations",
        "get_blast_radius",
        "get_root_cause_candidates",
        "get_reconciliation_state",
    ],
    "Manual-write provenance": [
        "get_manual_writes",
    ],
    "Backup / DR": [
        "get_backup_status",
    ],
    "Home-lab services": [
        "get_homelab_service_category",
    ],
    "Collection control": [
        "trigger_collection",
    ],
    "Resource seeding": [
        "seed_resource",
        "seed_resources_bulk",
        "seed_drift_event",
        "seed_vulnerability",
        "get_seeded_resources",
    ],
    "Governance actions": [
        "approve_proposal",
        "reject_proposal",
        "bulk_reject_proposals",
        "bulk_approve_proposals",
        "promote_instinct",
        "add_eol_product",
    ],
    "Graph identity decisions": [
        "confirm_same_as",
        "retract_same_as",
        "reject_same_as",
        "retract_not_same_as",
    ],
    "Reasoner writes": [
        "record_rootcause_note",
        "record_compliance_gap",
        "record_rootcause_notes_bulk",
    ],
    "Batch closure": [
        "resolve_drift_events",
        "close_compliance_violations",
    ],
    "State backend (environment notes / instincts / governance / documents)": [
        "record_environment_note",
        "get_environment_notes",
        "resolve_environment_note",
        "promote_instinct_v2",
        "rollback_instinct",
        "get_instinct_history",
        "propose_instinct",
        "record_client_state",
        "record_observation",
        "record_governance_event",
        "ingest_document",
        "update_document_metadata",
        "create_gitlab_issue",
        "comment_on_gitlab_issue",
    ],
}


def catalog_version() -> str:
    """Stable content hash of the tool catalog (#197).

    A headless consumer of ``get_tool_catalog`` (mcp_server.py) needs a way to
    detect that the allowed-actions catalog changed without diffing the whole
    payload every time. Hashing the actual data means the version is
    self-updating — a future tool-catalog edit changes it automatically, with
    nothing to remember to bump by hand (the exact bug class this catalog
    itself exists to avoid: enforcement drifting silently from prose).
    Truncated to 16 hex chars — a content-drift signal, not a security token.
    """
    payload = json.dumps(
        {"readonly": READONLY_TOOL_NAMES, "mutation": MUTATION_TOOL_NAMES, "groups": TOOL_GROUPS},
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


_TOKEN_PREFIX = "ibmcp_"


def _now_utc() -> datetime:
    return datetime.now(UTC)


# Upper bound on --expires-days / expires_days (TRK-160). Ten years — high
# enough never to obstruct a legitimate long-lived key, low enough that a
# fat-fingered value can't silently produce a datetime that overflows or is
# indistinguishable from "never expires". The API layer (api/schemas.py) and
# the bootstrap CLI both validate against this same constant.
MAX_EXPIRES_DAYS = 3650


def hash_token(token: str) -> str:
    """sha256 hex digest of a raw token (the value stored at rest)."""
    return hashlib.sha256(token.encode()).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """Coerce a possibly-naive DB timestamp to an aware UTC datetime.

    SECURITY-LOAD-BEARING, not a tidiness helper. ``expires_at`` is declared
    ``DateTime(timezone=True)``, but what comes back depends on the dialect:
    PostgreSQL returns an aware datetime, SQLite (the test/dev backend) returns
    a NAIVE one. Comparing a naive value against an aware ``datetime.now(UTC)``
    raises ``TypeError: can't compare offset-naive and offset-aware datetimes``
    — and every caller of this module is inside an auth path where an
    unexpected exception is the difference between a clean deny and a 500 (or,
    if some future caller wraps it in a bare ``except``, a fail-OPEN). Every
    value the DB hands back is UTC by construction (we only ever write
    ``datetime.now(UTC)``-derived values), so stamping UTC on a naive one is
    correct rather than a guess. Covered by
    ``test_naive_expires_at_is_treated_as_utc_not_a_typeerror``.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_expired(expires_at: datetime | None, now: datetime | None = None) -> bool:
    """True if ``expires_at`` has passed. NULL never expires.

    Boundary policy: expiry is INCLUSIVE (``<=``) — a key whose ``expires_at``
    is exactly the current instant is already expired. Fail-closed is the only
    defensible tie-break in an auth check.
    """
    if expires_at is None:
        return False
    return _as_utc(expires_at) <= (now or _now_utc())


def expiry_from_days(days: int | None, now: datetime | None = None) -> datetime | None:
    """Convert an ``--expires-days``/``expires_days`` value to an absolute UTC
    instant. ``None`` (the default everywhere) means "never expires".

    Raises ``ValueError`` for a non-positive or out-of-range value so a bad
    input can never quietly become a key that is already expired at birth, or
    one whose expiry is so far out it is effectively no expiry at all.
    """
    if days is None:
        return None
    if days <= 0 or days > MAX_EXPIRES_DAYS:
        raise ValueError(f"expires_days must be between 1 and {MAX_EXPIRES_DAYS}, got {days}")
    return (now or _now_utc()) + timedelta(days=days)


def generate_token() -> str:
    """Return a new high-entropy raw API token. Shown to the operator once."""
    return f"{_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def create_key(
    session: Session,
    name: str,
    allowed_tools: list[str],
    created_by: str,
    expires_in_days: int | None = None,
) -> tuple[McpApiKey, str]:
    """Insert a new key; return (row, raw_token). Only the hash is persisted.

    ``expires_in_days`` defaults to ``None`` = never expires, preserving the
    exact pre-TRK-160 behavior for every existing caller.
    """
    raw = generate_token()
    row = McpApiKey(
        name=name,
        token_hash=hash_token(raw),
        allowed_tools=list(allowed_tools),
        created_by=created_by,
        expires_at=expiry_from_days(expires_in_days),
    )
    session.add(row)
    session.flush()
    return row, raw


def create_bootstrap_key(
    session: Session, expires_in_days: int | None = None
) -> tuple[McpApiKey, str]:
    """Create the one initial full-access key so nothing breaks mid-cutover."""
    return create_key(
        session,
        name="bootstrap",
        allowed_tools=list(ALL_TOOL_NAMES),
        created_by="bootstrap",
        expires_in_days=expires_in_days,
    )


def lookup_active_key(session: Session, token: str) -> tuple[uuid.UUID, list[str]] | None:
    """Return (id, allowed_tools) for an ACTIVE key matching ``token`` else None.

    "Active" means: the hash matches, ``revoked_at IS NULL``, and the key has
    not passed ``expires_at`` (TRK-160). This is THE auth choke point —
    ``mcp_server._authorize`` turns a ``None`` here into 401 /
    ``DENY_KEY_INVALID`` (reason code ``key_invalid_or_expired``) and
    ``_record_auth_denial`` writes the audit row — so an expired key is
    rejected by exactly the same code path, with exactly the same status,
    error body and audit record, as a revoked one.

    Expiry is filtered in Python rather than SQL on purpose: the comparison has
    to be timezone-correct on BOTH dialects (PostgreSQL returns aware
    datetimes, SQLite naive), which ``is_expired``/``_as_utc`` guarantee and a
    pushed-down ``expires_at > :now`` predicate does not. The row is already
    being fetched by unique-indexed ``token_hash``, so this costs nothing.

    Returns plain values (not the ORM row) so callers can use them after the
    session closes without a DetachedInstanceError.
    """
    row = (
        session.query(McpApiKey)
        .filter(McpApiKey.token_hash == hash_token(token), McpApiKey.revoked_at.is_(None))
        .first()
    )
    if row is None or is_expired(row.expires_at):
        return None
    return row.id, list(row.allowed_tools or [])


def lookup_active_key_name(session: Session, token: str) -> str | None:
    """Return the ACTIVE key's human-readable ``name`` for ``token``, else None.

    Identity lookup, not an auth decision — the ASGI auth middleware has
    already authenticated (and tool-scoped) the request by the time a tool body
    calls this. It exists so a tool can attribute what it writes to the REAL
    calling key instead of trusting a caller-supplied ``approved_by`` /
    ``authored_by`` string (see mcp_server._caller_identity). Returns a plain
    str so callers can use it after the session closes.

    Applies the same expiry filter as ``lookup_active_key`` (TRK-160) so the
    two never disagree about whether a key is active — an expired key that
    somehow reached a tool body must not be able to attribute a write.
    """
    row = (
        session.query(McpApiKey)
        .filter(McpApiKey.token_hash == hash_token(token), McpApiKey.revoked_at.is_(None))
        .first()
    )
    if row is None or is_expired(row.expires_at):
        return None
    return row.name


def touch_last_used(session: Session, key_id: uuid.UUID) -> None:
    """Best-effort update of last_used_at. Never raises — a failure here must
    never fail the authenticated call it is bookkeeping for."""
    try:
        row = session.get(McpApiKey, key_id)
        if row is not None:
            row.last_used_at = _now_utc()
    except Exception:
        logger.warning("touch_last_used failed for key %s", key_id, exc_info=True)


def list_keys(session: Session) -> list[McpApiKey]:
    return session.query(McpApiKey).order_by(McpApiKey.created_at.desc()).all()


def revoke_key(session: Session, key_id: uuid.UUID) -> bool:
    """Revoke a live key (soft — never hard-deleted, keeps the audit trail).
    Returns True if a live key was revoked, False if missing/already revoked."""
    row = session.get(McpApiKey, key_id)
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = _now_utc()
    return True


def update_allowed_tools(
    session: Session, key_id: uuid.UUID, allowed_tools: list[str]
) -> tuple[McpApiKey, int] | None:
    """Amend a live key's ``allowed_tools`` in place (metadata only — never
    touches ``token_hash``, never re-issues).

    Returns ``(row, before_count)`` on success so the caller can log a
    before/after amendment summary, or ``None`` if no key exists with this id.
    Raises ``ValueError`` if the key exists but is already revoked — revoked
    keys cannot be amended.
    """
    row = session.get(McpApiKey, key_id)
    if row is None:
        return None
    if row.revoked_at is not None:
        raise ValueError("cannot amend a revoked key")
    before_count = len(row.allowed_tools or [])
    row.allowed_tools = list(allowed_tools)
    return row, before_count


CLI_USAGE = "usage: python -m infra_brain.mcp_auth --bootstrap [--expires-days N]"


def parse_bootstrap_argv(argv: list[str]) -> int | None:
    """Extract ``--expires-days N`` from a ``--bootstrap`` argv. None = no expiry.

    Split out of ``__main__`` purely so the flag is testable without a DB or a
    subprocess (``tests/test_mcp_auth_helpers.py``). Accepts both
    ``--expires-days 30`` and ``--expires-days=30``. Raises ``ValueError`` on a
    missing, non-integer, or out-of-range value — the operational one-shot that
    mints a full-access key is the last place to silently reinterpret a typo.
    """
    for i, arg in enumerate(argv):
        raw: str | None = None
        if arg == "--expires-days":
            if i + 1 >= len(argv):
                raise ValueError("--expires-days requires a value")
            raw = argv[i + 1]
        elif arg.startswith("--expires-days="):
            raw = arg.split("=", 1)[1]
        else:
            continue
        try:
            days = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"--expires-days must be an integer, got {raw!r}") from None
        # Reuse the single validation rule rather than re-encoding the bounds.
        expiry_from_days(days)
        return days
    return None


if __name__ == "__main__":  # pragma: no cover - operational one-shot
    # Cutover bootstrap: `python -m infra_brain.mcp_auth --bootstrap` prints the
    # one full-access key's raw value ONCE. Same "shown once, never stored raw"
    # convention as any dashboard-created key. `--expires-days N` optionally
    # bounds its lifetime; omitting it keeps the historical never-expires
    # behavior, so this flag can never break an existing bootstrap runbook.
    import sys

    from infra_brain.db.session import get_session

    if "--bootstrap" in sys.argv:
        try:
            days = parse_bootstrap_argv(sys.argv[1:])
        except ValueError as exc:
            print(f"error: {exc}")
            print(CLI_USAGE)
            sys.exit(2)
        with get_session() as s:
            row, raw = create_bootstrap_key(s, expires_in_days=days)
            s.commit()
            print(f"bootstrap key id={row.id}")
            print(f"expires_at: {row.expires_at.isoformat() if row.expires_at else 'never'}")
            print(f"RAW TOKEN (shown once, not stored): {raw}")
    else:
        print(CLI_USAGE)
