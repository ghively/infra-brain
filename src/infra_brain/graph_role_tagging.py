"""Role and environment tagging — GitLab issue #128.

Builds on Phase 2 (issue #126, TRK-196, ``graph_phase2.py`` /
``db/models/graph.py``): reuses ``graph_phase2.upsert_node`` /
``graph_phase2.upsert_edge`` for all writes, and writes into the SAME
``graph_nodes`` / ``graph_edges`` tables. See ``graph_phase2.py``'s module
docstring for why those tables exist separately from
``resource_relationships``.

Problem statement
------------------
Server roles (domain controller, DNS, DHCP, SQL, web, file server, backup,
monitoring, CA, jump box, load balancer) have NO structured field anywhere in
infra-brain. The only place role data exists today is the vSphere VM
``annotation`` free-text field (``vsphere_vms.annotation``). A full-population
mining pass (847 VMs, done by the issue's author) found 79.3% coverage (672
VMs) across this taxonomy:

    SQL/Database (42), Web/IIS/Proxy (26), Domain Controller (28), Backup (19),
    Certificate Authority (18), File Server (16), Monitoring (13), DNS (11,
    incl. 4 dedicated PowerDNS recursors), Print Server (10), DHCP (8),
    Jump/Bastion (6), Load Balancer (4)

Caveat carried over verbatim from the issue: the annotation text is freeform
prose from 4+ authors over ~10 years — only 3/672 annotations use any
consistent prefix. A redundancy check found 4/5 clear-role VMs already had the
role recoverable from **hostname pattern alone** (e.g. ``prod-win-dc-03`` -> DC).

Pipeline (implemented in exactly this order, per the issue)
------------------------------------------------------------
1. ``emit_roles_from_hostname`` — cheap regex pass over ``vsphere_vms.name``
   (the vCenter inventory name, which is where the ``prod-win-dc-03``-style
   convention lives). Catches the obvious majority.
2. ``emit_roles_from_annotation`` — keyword/phrase pass over
   ``vsphere_vms.annotation``, run SECOND and only for ``(vm, role)`` pairs
   the hostname pass did not already claim for that VM. This is what recovers
   ambiguous or combined-role boxes (a VM whose hostname suggests only one
   role but whose annotation states it also serves a second).
3. ``emit_environments`` — a separate, independent extraction (prod / dev /
   test / cao) from ``vsphere_vms.guest_hostname`` FQDN suffix. Not part of
   the role taxonomy; kept as its own pass/edge type.

Why a ``Role`` / ``Environment`` GraphNode instead of an edge attribute
------------------------------------------------------------------------
The issue leaves this an open call. Chosen: a lightweight, shared
``GraphNode`` per role/environment value (natural_key = the taxonomy slug,
e.g. ``"domain_controller"`` / ``"prod"``), mirroring how Phase 2's ``Cve``
node works (small, high-fan-in, no owning ``resource_id``) — NOT a new
``GraphNodeType`` enum member: ``GraphNode.node_type`` is a plain
``String(64)`` column with no DB-level enum constraint, so introducing
``"Role"`` / ``"Environment"`` values here is additive data, not a schema
change. No migration is required by this module for that reason.

Rationale for the node (not a plain string in ``evidence``):
  * Cardinality is small and closed (~12 roles, 4 environments) — a real
    "browse all hosts with role X" or "browse all prod hosts" query is a
    single edge-target join, exactly the shape Phase 3's traversal API (issue
    #127) is built to answer. Burying the value in ``evidence`` JSONB would
    make that a table scan / JSON containment query instead.
  * A future corroborating signal (e.g. Octopus ``environment_name``, see
    below) can converge onto the SAME ``Environment`` node from a second
    emitter, which an edge-attribute design could not support without
    re-deriving the set of distinct values from scratch.

Edge types (new, not in Phase 2's ``GraphEdgeType`` enum for the same
reason — ``GraphEdge.edge_type`` is also an unconstrained ``String(64)``):
  * ``HAS_ROLE``        VsphereVM -> Role
  * ``IN_ENVIRONMENT``  VsphereVM -> Environment

Methods and confidence (the honesty rule from Phase 2, reused verbatim:
``graph_phase2.upsert_edge`` raises if confidence >= 1.000 is claimed for
anything but ``method="declared"`` — enforced there, not re-implemented
here). None of the methods below is ``"declared"``, so none may ever claim
1.000; the actual values chosen:

  * ``inferred_from_hostname_pattern`` — confidence 0.850. Higher than the
    annotation-mining confidence: the issue's own redundancy check found
    hostname pattern alone recovers 4/5 clear-role VMs, and a regex match on
    a structured naming convention is far more reproducible than free prose.
    Still well below 1.000 — short role codes (``DC``, ``FS``, ``CA``, ``LB``)
    can coincidentally match the tail of an unrelated hostname (see "Known
    false-positive risk" below), and the convention itself is inferred, not
    documented anywhere as a rule this org promises to follow.
  * ``inferred_from_annotation`` — confidence 0.650. Lower than hostname:
    freeform prose from 4+ authors over ~10 years with no consistent prefix
    (3/672 use one), keyword/phrase matching is inherently fuzzier than a
    regex on a structured token, and this pass exists specifically to catch
    the ambiguous/combined-role cases the cheap pass could not resolve
    cleanly.
  * ``inferred_from_fqdn_suffix`` (environment) — confidence 0.900.
    Higher than either role method: the four suffixes
    (``example.internal`` / ``dev.example.internal`` / ``test.example.internal`` /
    ``cao.example.internal``) are an exact, case-insensitive string match against
    a small closed list with no ambiguity between categories (unlike the
    2-3 letter role codes) — but still not "declared": vCenter/DNS did not
    assert "this is prod", we are reading an internal naming *convention* and
    trusting it holds, so it stays below 1.000.

Known false-positive risk (hostname pass)
--------------------------------------------
Several role codes are short (``DC``, ``FS``, ``CA``, ``LB``) and the
observed naming convention has no delimiter before the trailing digits
(``prod-win-dc-03``, not ``prod-win-DC-03``), so a hostname that coincidentally
ends in one of these 2-letter sequences plus digits (e.g. a hypothetical
``...ORCA01``) would false-positive as Certificate Authority. This is a real,
accepted limitation of a pure end-anchored-regex approach on undelimited
hostnames — not something this pass tries to fully disambiguate — which is
part of why the hostname-pass confidence (0.850) is well below a "declared"
fact and why role rules are matched in a fixed priority order (longer,
less-ambiguous codes first) rather than all-at-once.

Environment / Octopus follow-up (out of scope here, noted per the issue)
----------------------------------------------------------------------------
Octopus's own ``environment_name`` field (42 real environments, e.g.
``Fleet-Development``, ``Production-SITEB``) is an independent corroborating
signal for environment, but it only joins by naming convention, not a shared
identifier with vSphere — cross-domain matching is a follow-up correlation
opportunity, not built here. Also note: ``host_purpose_map`` and the Ansible
inventory have no environment field at all today, so vSphere-derived
environment tagging only covers hosts vSphere itself knows about; hosts
absent from vSphere (if any) get no environment edge from this module.

Read-only guarantee: identical to ``graph_phase2.py`` — every function here
READS ``vsphere_vms`` and WRITES only ``graph_nodes`` / ``graph_edges``. No
external infrastructure is contacted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from infra_brain.db.models import VsphereVm
from infra_brain.graph_phase2 import upsert_edge, upsert_node

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identity constants (see module docstring for why these are plain strings,
# not new GraphNodeType / GraphEdgeType enum members)
# ---------------------------------------------------------------------------

NODE_TYPE_ROLE = "Role"
NODE_TYPE_ENVIRONMENT = "Environment"

EDGE_TYPE_HAS_ROLE = "HAS_ROLE"
EDGE_TYPE_IN_ENVIRONMENT = "IN_ENVIRONMENT"

METHOD_HOSTNAME_PATTERN = "inferred_from_hostname_pattern"
METHOD_ANNOTATION = "inferred_from_annotation"
# NOTE on length: GraphEdge.method is String(32) — enforced by real Postgres,
# NOT by the sqlite test suite. "inferred_from_vsphere_fqdn_suffix" (33 chars)
# overflowed it and failed every graph_maintenance run (2026-07-29 regression).
# The vSphere provenance lives in EMITTER_ENVIRONMENT / the evidence payload,
# so the method name can stay short. tests/test_graph_role_tagging.py asserts
# every constant here fits its column.
METHOD_VSPHERE_FQDN_SUFFIX = "inferred_from_fqdn_suffix"

#: Decimal (not float) to match the ``Numeric(4, 3)`` column and avoid binary
#: float imprecision feeding ``graph_phase2.upsert_edge``'s confidence guard.
CONFIDENCE_HOSTNAME_PATTERN = Decimal("0.850")
CONFIDENCE_ANNOTATION = Decimal("0.650")
CONFIDENCE_ENVIRONMENT = Decimal("0.900")

EMITTER_ROLE_HOSTNAME = "graph_role_tagging.roles_from_hostname"
EMITTER_ROLE_ANNOTATION = "graph_role_tagging.roles_from_annotation"
EMITTER_ENVIRONMENT = "graph_role_tagging.environment_from_fqdn"


# ---------------------------------------------------------------------------
# Role taxonomy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleRule:
    """One role in the taxonomy: its slug/label plus both detection signals."""

    slug: str
    label: str
    #: Compiled, case-insensitive, end-anchored: matches the role code
    #: immediately followed by 0-3 trailing digits at the END of the
    #: hostname (the ``prod-win-dc-03`` convention: prefix + role code + number).
    hostname_pattern: re.Pattern[str]
    #: Lowercase substrings/phrases checked against the lowercased
    #: annotation text. Any one hit is a match for this role.
    annotation_keywords: tuple[str, ...]


def _hostname_re(*codes: str) -> re.Pattern[str]:
    alt = "|".join(codes)
    return re.compile(rf"(?:{alt})[-_]?\d{{0,3}}$", re.IGNORECASE)


#: Checked in this order for the hostname pass — most distinctive / longest
#: codes first, so e.g. "DHCP" is tried before the unrelated shorter "DC"
#: could ever be considered, and a hostname stops at its FIRST match.
ROLE_TAXONOMY: tuple[RoleRule, ...] = (
    RoleRule(
        slug="dhcp",
        label="DHCP",
        hostname_pattern=_hostname_re("DHCP"),
        annotation_keywords=("dhcp",),
    ),
    RoleRule(
        slug="dns",
        label="DNS",
        hostname_pattern=_hostname_re("DNS"),
        annotation_keywords=("dns server", "dns recursor", "powerdns", "recursive resolver"),
    ),
    RoleRule(
        slug="domain_controller",
        label="Domain Controller",
        hostname_pattern=_hostname_re("DC"),
        annotation_keywords=("domain controller", "active directory", "ad dc"),
    ),
    RoleRule(
        slug="sql_database",
        label="SQL/Database",
        hostname_pattern=_hostname_re("SQL"),
        annotation_keywords=(
            "sql server",
            "database server",
            "mssql",
            "postgres",
            "postgresql",
            "oracle db",
            "db server",
        ),
    ),
    RoleRule(
        slug="web_iis_proxy",
        label="Web/IIS/Proxy",
        hostname_pattern=_hostname_re("WEB", "WWW", "IIS"),
        annotation_keywords=(
            "iis",
            "web server",
            "reverse proxy",
            "nginx",
            "apache",
            "web app",
            "webserver",
        ),
    ),
    RoleRule(
        slug="file_server",
        label="File Server",
        hostname_pattern=_hostname_re("FS"),
        annotation_keywords=("file server", "fileserver", "smb share", "file share"),
    ),
    RoleRule(
        slug="backup",
        label="Backup",
        hostname_pattern=_hostname_re("BAK", "BKP"),
        annotation_keywords=("backup server", "veeam", "backup target", "backup repository"),
    ),
    RoleRule(
        slug="certificate_authority",
        label="Certificate Authority",
        hostname_pattern=_hostname_re("CA"),
        annotation_keywords=("certificate authority", "issuing ca", "root ca", "pki server"),
    ),
    RoleRule(
        slug="monitoring",
        label="Monitoring",
        hostname_pattern=_hostname_re("MON"),
        annotation_keywords=("monitoring server", "nagios", "zabbix", "solarwinds", "prtg"),
    ),
    RoleRule(
        slug="print_server",
        label="Print Server",
        hostname_pattern=_hostname_re("PRT", "PRN"),
        annotation_keywords=("print server", "printer server", "print spooler"),
    ),
    RoleRule(
        slug="jump_bastion",
        label="Jump/Bastion",
        hostname_pattern=_hostname_re("JMP", "BAST"),
        annotation_keywords=("jump box", "jumpbox", "bastion host", "jump server"),
    ),
    RoleRule(
        slug="load_balancer",
        label="Load Balancer",
        hostname_pattern=_hostname_re("LB"),
        annotation_keywords=("load balancer", "haproxy", "netscaler", "f5 lb", "loadbalancer"),
    ),
)

_ROLES_BY_SLUG: dict[str, RoleRule] = {r.slug: r for r in ROLE_TAXONOMY}


# ---------------------------------------------------------------------------
# Environment taxonomy — vSphere guest_hostname FQDN suffix
# ---------------------------------------------------------------------------

#: Checked in this order: longest / most-specific suffix first, since the
#: bare domain is itself a suffix of every subdomain form
#: ("example.internal" is a suffix of "dev.example.internal"). The bare domain
#: matches only if none of the subdomain forms did, and is treated as
#: production per this org's naming convention (prod hosts carry no
#: environment subdomain prefix).
ENVIRONMENT_SUFFIXES: tuple[tuple[str, str, str], ...] = (
    ("dev", "Development", "dev.example.internal"),
    ("test", "Test", "test.example.internal"),
    ("cao", "CAO", "cao.example.internal"),
    ("prod", "Production", "example.internal"),
)


# ---------------------------------------------------------------------------
# Pass 1 — hostname-regex role extraction
# ---------------------------------------------------------------------------


def _match_hostname_role(hostname: str) -> RoleRule | None:
    for rule in ROLE_TAXONOMY:
        if rule.hostname_pattern.search(hostname):
            return rule
    return None


def emit_roles_from_hostname(session: Any) -> tuple[int, dict[Any, str]]:
    """Pass 1: regex the VM's inventory name against ``ROLE_TAXONOMY``.

    Returns ``(edges_written, claimed)`` where ``claimed`` maps
    ``vm.id -> role_slug`` for every VM this pass tagged, so
    :func:`emit_roles_from_annotation` can skip re-claiming the SAME role at
    a lower confidence for a VM already resolved here.

    Templates are skipped (not a running workload; naming on a template is
    frequently the source image's name, not the deployed role). At most ONE
    role edge per VM from this pass — the observed convention encodes a
    single role per hostname, and taking the first (highest-priority) match
    avoids emitting several low-value guesses off one ambiguous short code.
    """
    claimed: dict[Any, str] = {}
    written = 0
    for vm in session.execute(select(VsphereVm)).scalars():
        if vm.is_template:
            continue
        hostname = (vm.name or "").strip()
        if not hostname:
            continue
        rule = _match_hostname_role(hostname)
        if rule is None:
            continue
        vm_node = upsert_node(
            session,
            node_type="VsphereVM",
            natural_key=f"{vm.vcenter}:{vm.moref}",
            name=vm.name,
            source="vsphere",
            resource_id=vm.resource_id,
        )
        role_node = upsert_node(
            session,
            node_type=NODE_TYPE_ROLE,
            natural_key=rule.slug,
            name=rule.label,
            source="graph_role_tagging",
        )
        upsert_edge(
            session,
            source_id=vm_node.id,
            target_id=role_node.id,
            edge_type=EDGE_TYPE_HAS_ROLE,
            method=METHOD_HOSTNAME_PATTERN,
            confidence=CONFIDENCE_HOSTNAME_PATTERN,
            source=EMITTER_ROLE_HOSTNAME,
            evidence={
                "hostname": hostname,
                "role_slug": rule.slug,
                "pattern": rule.hostname_pattern.pattern,
            },
        )
        claimed[vm.id] = rule.slug
        written += 1
    logger.info("graph_role_tagging: hostname pass emitted %d HAS_ROLE edges", written)
    return written, claimed


# ---------------------------------------------------------------------------
# Pass 2 — annotation keyword/phrase mining
# ---------------------------------------------------------------------------


def emit_roles_from_annotation(
    session: Any, already_claimed: dict[Any, str] | None = None
) -> int:
    """Pass 2: keyword-match ``vsphere_vms.annotation`` against the taxonomy.

    ``already_claimed`` (typically the ``claimed`` dict returned by
    :func:`emit_roles_from_hostname`) prevents this pass from re-asserting,
    at a LOWER confidence, a role the hostname pass already resolved for
    that VM — the whole point of running hostname first. A VM can still
    pick up an ADDITIONAL, different role here (the "combined-role box"
    case: hostname implies one role, annotation reveals a second), because
    each role is a distinct target node and ``HAS_ROLE`` edges are unique
    per ``(vm, role)`` pair, not per VM.

    Unlike the hostname pass, a single annotation may match MULTIPLE role
    categories and all are emitted — free text genuinely can describe a
    combined-role box ("primary DC, also runs DNS/DHCP for the site").
    """
    already_claimed = already_claimed or {}
    written = 0
    for vm in session.execute(select(VsphereVm)).scalars():
        if vm.is_template:
            continue
        text = (vm.annotation or "").strip().lower()
        if not text:
            continue
        skip_slug = already_claimed.get(vm.id)
        for rule in ROLE_TAXONOMY:
            if rule.slug == skip_slug:
                continue
            hit = next((kw for kw in rule.annotation_keywords if kw in text), None)
            if hit is None:
                continue
            vm_node = upsert_node(
                session,
                node_type="VsphereVM",
                natural_key=f"{vm.vcenter}:{vm.moref}",
                name=vm.name,
                source="vsphere",
                resource_id=vm.resource_id,
            )
            role_node = upsert_node(
                session,
                node_type=NODE_TYPE_ROLE,
                natural_key=rule.slug,
                name=rule.label,
                source="graph_role_tagging",
            )
            upsert_edge(
                session,
                source_id=vm_node.id,
                target_id=role_node.id,
                edge_type=EDGE_TYPE_HAS_ROLE,
                method=METHOD_ANNOTATION,
                confidence=CONFIDENCE_ANNOTATION,
                source=EMITTER_ROLE_ANNOTATION,
                evidence={
                    "annotation_excerpt": (vm.annotation or "")[:256],
                    "role_slug": rule.slug,
                    "matched_keyword": hit,
                },
            )
            written += 1
    logger.info("graph_role_tagging: annotation pass emitted %d HAS_ROLE edges", written)
    return written


# ---------------------------------------------------------------------------
# Environment segmentation — vSphere guest_hostname FQDN suffix
# ---------------------------------------------------------------------------


def _match_environment(guest_hostname: str) -> tuple[str, str] | None:
    hn = guest_hostname.strip().lower()
    for slug, label, suffix in ENVIRONMENT_SUFFIXES:
        if hn.endswith(suffix):
            return slug, label
    return None


def emit_environments(session: Any) -> int:
    """Extract prod/dev/test/cao environment from ``guest_hostname`` FQDN.

    Only vSphere-known VMs with a populated ``guest_hostname`` get an edge;
    VMs with no guest OS reporting (VMware Tools not running/installed) get
    none — no guess is emitted in that case. See module docstring for the
    ``host_purpose_map`` / Ansible / Octopus coverage-gap caveats.
    """
    written = 0
    for vm in session.execute(select(VsphereVm)).scalars():
        if vm.is_template:
            continue
        guest_hostname = (vm.guest_hostname or "").strip()
        if not guest_hostname:
            continue
        match = _match_environment(guest_hostname)
        if match is None:
            continue
        slug, label = match
        vm_node = upsert_node(
            session,
            node_type="VsphereVM",
            natural_key=f"{vm.vcenter}:{vm.moref}",
            name=vm.name,
            source="vsphere",
            resource_id=vm.resource_id,
        )
        env_node = upsert_node(
            session,
            node_type=NODE_TYPE_ENVIRONMENT,
            natural_key=slug,
            name=label,
            source="graph_role_tagging",
        )
        upsert_edge(
            session,
            source_id=vm_node.id,
            target_id=env_node.id,
            edge_type=EDGE_TYPE_IN_ENVIRONMENT,
            method=METHOD_VSPHERE_FQDN_SUFFIX,
            confidence=CONFIDENCE_ENVIRONMENT,
            source=EMITTER_ENVIRONMENT,
            evidence={
                "guest_hostname": guest_hostname,
                "environment_slug": slug,
            },
        )
        written += 1
    logger.info("graph_role_tagging: environment pass emitted %d IN_ENVIRONMENT edges", written)
    return written


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def emit_all(session: Any) -> tuple[dict[str, int], list[str]]:
    """Run hostname roles, then annotation roles, then environments.

    Mirrors ``graph_phase2.emit_all``: each stage is independently guarded so
    one failing stage does not abort the others; failures are logged AND
    surfaced in the returned ``errors`` list rather than reported as a silent
    zero.

    Each stage also runs inside its own ``session.begin_nested()`` SAVEPOINT,
    for the same reason as ``graph_phase2.emit_all``: a failed flush on real
    PostgreSQL poisons the session until a savepoint rollback recovers it, so
    "one failing stage does not abort the others" would otherwise be false.
    """
    counts: dict[str, int] = {}
    errors: list[str] = []
    claimed: dict[Any, str] = {}

    try:
        with session.begin_nested():
            counts["roles_from_hostname"], claimed = emit_roles_from_hostname(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph_role_tagging: hostname pass failed: %s", exc)
        counts["roles_from_hostname"] = 0
        errors.append(f"roles_from_hostname: {exc}")

    try:
        with session.begin_nested():
            counts["roles_from_annotation"] = emit_roles_from_annotation(session, claimed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph_role_tagging: annotation pass failed: %s", exc)
        counts["roles_from_annotation"] = 0
        errors.append(f"roles_from_annotation: {exc}")

    try:
        with session.begin_nested():
            counts["environments"] = emit_environments(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph_role_tagging: environment pass failed: %s", exc)
        counts["environments"] = 0
        errors.append(f"environments: {exc}")

    return counts, errors


__all__ = [
    "CONFIDENCE_ANNOTATION",
    "CONFIDENCE_ENVIRONMENT",
    "CONFIDENCE_HOSTNAME_PATTERN",
    "EDGE_TYPE_HAS_ROLE",
    "EDGE_TYPE_IN_ENVIRONMENT",
    "EMITTER_ENVIRONMENT",
    "EMITTER_ROLE_ANNOTATION",
    "EMITTER_ROLE_HOSTNAME",
    "ENVIRONMENT_SUFFIXES",
    "METHOD_ANNOTATION",
    "METHOD_HOSTNAME_PATTERN",
    "METHOD_VSPHERE_FQDN_SUFFIX",
    "NODE_TYPE_ENVIRONMENT",
    "NODE_TYPE_ROLE",
    "ROLE_TAXONOMY",
    "RoleRule",
    "emit_all",
    "emit_environments",
    "emit_roles_from_annotation",
    "emit_roles_from_hostname",
]
