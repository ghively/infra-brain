"""Tests for the GitLab #118 policy-as-code rule engine
(``infra_brain.agents.compliance_rules``).

Confirms:
1. ``DEFAULT_RULES`` evaluated through ``evaluate_rules()`` reproduces the
   exact same (rule, host, severity, detail, resource_id) tuples as the
   original 4 hardcoded Python blocks, across all 4 rule shapes.
2. A 5th rule can be added purely via a new dict in the rules list (no code
   change) and is picked up correctly — proving the DSL is genuinely
   operator-extensible, not just a refactor with the same fixed rule count.
3. An unknown rule type / a single bad rule config is skipped, not fatal to
   the rest of the pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from infra_brain.agents.compliance_rules import DEFAULT_RULES, evaluate_rules
from infra_brain.db.models import (
    DriftEvent,
    EolRegistry,
    ProposedAction,
    R7Asset,
    Resource,
    VulnQueueItem,
)

from tests.support.pg import make_engine

# tests/agents/test_compliance_rule_engine.py -> repo root is parents[2].
_COMPLIANCE_YAML = Path(__file__).resolve().parents[2] / "rules" / "enforcement" / "compliance.yml"


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


def test_default_rules_reproduce_all_four_original_shapes(engine):
    """One session seeded with all 4 violation conditions must, through the
    generic engine + DEFAULT_RULES, produce the exact same rule set the
    original 4 hardcoded blocks produced."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        eol_host = Resource(domain="linux", type="host", name="eol-host", source="LinuxAgent")
        vuln_host = Resource(domain="linux", type="host", name="vuln-host", source="LinuxAgent")
        drift_host = Resource(domain="linux", type="host", name="drift-host", source="LinuxAgent")
        cve_host = Resource(domain="vuln", type="r7_asset", name="cve-host", source="VulnAgent")
        s.add_all([eol_host, vuln_host, drift_host, cve_host])
        s.flush()

        s.add(
            EolRegistry(
                resource_id=eol_host.id,
                asset_name="Ubuntu 18.04",
                eol_date=now - timedelta(days=10),
                pci_risk_score=8,
            )
        )
        s.add(
            VulnQueueItem(
                resource_id=vuln_host.id,
                cve_id="CVE-2026-1",
                severity="critical",
                sla_due=now - timedelta(days=1),
                status="open",
            )
        )
        s.add(
            DriftEvent(
                resource_id=drift_host.id,
                drift_type="config_drift",
                field="kernel",
                status="open",
                detected_at=now - timedelta(days=20),
            )
        )
        s.add(R7Asset(resource_id=cve_host.id, r7_asset_id=1, hostname="cve-host", vuln_critical=2))
        s.commit()

        thresholds = {
            "eol_check": {"enabled": True},
            "stale_drift_days": 14,
            "unaddressed_critical_cve_days": 30,
        }
        found = evaluate_rules(s, DEFAULT_RULES, thresholds, now)

    by_rule = {f[0]: f for f in found}
    assert set(by_rule) == {
        "eol_overdue",
        "vuln_sla_breach",
        "stale_drift",
        "unaddressed_critical_cve",
    }

    eol = by_rule["eol_overdue"]
    assert eol == (
        "eol_overdue",
        "eol-host",
        "high",
        "Ubuntu 18.04 reached EOL on " + str((now - timedelta(days=10)).date()),
        eol_host.id,
    )

    vuln = by_rule["vuln_sla_breach"]
    assert vuln == (
        "vuln_sla_breach",
        "vuln-host",
        "critical",
        "CVE-2026-1 past SLA (critical)",
        vuln_host.id,
    )

    drift = by_rule["stale_drift"]
    assert drift == ("stale_drift", "drift-host", "medium", "kernel drift open >14d", drift_host.id)

    cve = by_rule["unaddressed_critical_cve"]
    assert cve == (
        "unaddressed_critical_cve",
        "cve-host",
        "critical",
        "CRITICAL vuln band with no ProposedAction in 30d (vuln_critical=2)",
        cve_host.id,
    )


def test_eol_check_disabled_via_enabled_key(engine):
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="eol-host", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(EolRegistry(resource_id=r.id, asset_name="Old OS", eol_date=now - timedelta(days=10)))
        s.commit()

        found = evaluate_rules(s, DEFAULT_RULES, {"eol_check": {"enabled": False}}, now)

    assert "eol_overdue" not in {f[0] for f in found}


def test_stale_drift_excludes_bookkeeping_domains(engine):
    now = datetime.now(UTC)
    with Session(engine) as s:
        shadow = Resource(
            domain="compliance", type="violation", name="stale_drift:x", source="graph_maintenance"
        )
        real = Resource(domain="linux", type="host", name="real-host", source="LinuxAgent")
        s.add_all([shadow, real])
        s.flush()
        for res in (shadow, real):
            s.add(
                DriftEvent(
                    resource_id=res.id,
                    drift_type="state_drift",
                    field="presence",
                    status="open",
                    detected_at=now - timedelta(days=20),
                )
            )
        s.commit()

        found = evaluate_rules(s, DEFAULT_RULES, {"stale_drift_days": 14}, now)

    hosts = {f[1] for f in found if f[0] == "stale_drift"}
    assert hosts == {"real-host"}


def test_stale_drift_excludes_homelab_services(engine):
    """A home-lab media-server row (HomelabServicesAgent, domain=
    "homelab_services" — radarr/sonarr/emby/etc, confirmed live in
    ``resources`` as ``type="homelab_service"``) must never surface as a
    ComplianceViolation just because it sat unreachable/drifted past
    ``stale_drift_days``. This was a REAL live gap (not theoretical):
    HomelabServicesAgent is not a SKIP_HOOK collector, so its resources DO
    flow through the generic full-fleet DriftDetector like any other domain,
    and the ``stale_drift`` rule's ``exclude_resource_domains`` previously
    only listed ``["compliance", "graph_maintenance"]`` — a media-server
    outage lasting >stale_drift_days would genuinely have minted a
    ComplianceViolation before this fix."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        homelab = Resource(
            domain="homelab_services",
            type="homelab_service",
            name="radarr",
            source="HomelabServicesAgent",
        )
        real = Resource(domain="linux", type="host", name="real-host-2", source="LinuxAgent")
        s.add_all([homelab, real])
        s.flush()
        for res in (homelab, real):
            s.add(
                DriftEvent(
                    resource_id=res.id,
                    drift_type="config_drift",
                    field="data.status",
                    status="open",
                    detected_at=now - timedelta(days=20),
                )
            )
        s.commit()

        found = evaluate_rules(s, DEFAULT_RULES, {"stale_drift_days": 14}, now)

    hosts = {f[1] for f in found if f[0] == "stale_drift"}
    assert "radarr" not in hosts
    assert hosts == {"real-host-2"}


def test_stale_drift_excludes_fleet_health(engine):
    """A fleet_health resource (FleetHealthAgent's own collector-heartbeat
    snapshot) must never surface as a ComplianceViolation just because it sat
    with an open drift event past ``stale_drift_days`` — its entire snapshot
    IS collector telemetry, not fleet infrastructure. Mirrors remediation.py's
    ``_BOOKKEEPING_DOMAINS`` fix (GitLab #142), which was never backported to
    this rule's ``exclude_resource_domains`` before this fix — a genuine gap:
    a fleet_health resource with an open drift event older than
    stale_drift_days would previously have minted a real ComplianceViolation.
    """
    now = datetime.now(UTC)
    with Session(engine) as s:
        health = Resource(
            domain="fleet_health",
            type="health_snapshot",
            name="fleet-health-snapshot",
            source="FleetHealthAgent",
        )
        real = Resource(domain="linux", type="host", name="real-host-3", source="LinuxAgent")
        s.add_all([health, real])
        s.flush()
        for res in (health, real):
            s.add(
                DriftEvent(
                    resource_id=res.id,
                    drift_type="state_drift",
                    field="collector_status",
                    status="open",
                    detected_at=now - timedelta(days=20),
                )
            )
        s.commit()

        found = evaluate_rules(s, DEFAULT_RULES, {"stale_drift_days": 14}, now)

    hosts = {f[1] for f in found if f[0] == "stale_drift"}
    assert "fleet-health-snapshot" not in hosts
    assert hosts == {"real-host-3"}


def test_stale_drift_excludes_domain_freshness_field_prefix(engine):
    """Belt-and-braces field-prefix exclusion (GitLab #142/#162): a
    ``domain_freshness.*`` collector-heartbeat field must be skipped by field
    name alone, even on a resource whose domain is NOT in
    ``exclude_resource_domains`` — mirrors remediation.py's
    ``is_telemetry_field()`` prefix check via ``TELEMETRY_FIELD_PREFIXES``.
    A genuine config-drift field on the same resource still fires.
    """
    now = datetime.now(UTC)
    with Session(engine) as s:
        host = Resource(domain="linux", type="host", name="freshness-host", source="LinuxAgent")
        s.add(host)
        s.flush()
        s.add(
            DriftEvent(
                resource_id=host.id,
                drift_type="state_drift",
                field="domain_freshness.homelab_services",
                status="open",
                detected_at=now - timedelta(days=20),
            )
        )
        s.add(
            DriftEvent(
                resource_id=host.id,
                drift_type="config_drift",
                field="ssh_permit_root_login",
                status="open",
                detected_at=now - timedelta(days=20),
            )
        )
        s.commit()

        found = evaluate_rules(s, DEFAULT_RULES, {"stale_drift_days": 14}, now)

    stale_drift_findings = [f for f in found if f[0] == "stale_drift"]
    assert len(stale_drift_findings) == 1
    assert "ssh_permit_root_login" in stale_drift_findings[0][3]


def test_fifth_rule_added_via_yaml_only_no_code_change(engine):
    """Proves the DSL is genuinely extensible: a 5th rule (a second
    ``field_overdue`` instance targeting a different model/field) is added as
    a plain dict, with zero changes to compliance_rules.py, and fires
    correctly alongside the original 4."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="stale-sla-host", source="LinuxAgent")
        s.add(r)
        s.flush()
        # Reuse VulnQueueItem.sla_due as a stand-in "some other overdue date
        # field" to prove genericity — a real 5th rule would target whatever
        # new model/field an operator needs.
        s.add(
            VulnQueueItem(
                resource_id=r.id,
                cve_id="CVE-2026-9",
                severity="low",
                sla_due=now - timedelta(days=100),
                status="triage",
            )
        )
        s.commit()

        fifth_rule = {
            "name": "custom_fifth_rule",
            "type": "field_overdue",
            "model": "VulnQueueItem",
            "date_field": "sla_due",
            "resource_fk": "resource_id",
            "host_field": "cve_id",
            "severity": "low",
            "detail_template": "custom rule fired for {cve_id}",
        }
        rules_cfg = [*DEFAULT_RULES, fifth_rule]
        found = evaluate_rules(s, rules_cfg, {}, now)

    names = {f[0] for f in found}
    assert "custom_fifth_rule" in names
    custom = next(f for f in found if f[0] == "custom_fifth_rule")
    assert custom == (
        "custom_fifth_rule",
        "stale-sla-host",
        "low",
        "custom rule fired for CVE-2026-9",
        r.id,
    )


def test_unknown_rule_type_is_skipped_not_fatal(engine):
    with Session(engine) as s:
        rules_cfg = [*DEFAULT_RULES, {"name": "bogus", "type": "not_a_real_shape"}]
        found = evaluate_rules(s, rules_cfg, {}, datetime.now(UTC))
    assert "bogus" not in {f[0] for f in found}


def test_disabled_rule_entry_skipped(engine):
    with Session(engine) as s:
        now = datetime.now(UTC)
        r = Resource(domain="linux", type="host", name="disabled-host", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(EolRegistry(resource_id=r.id, asset_name="Old OS", eol_date=now - timedelta(days=5)))
        s.commit()

        rules_cfg = [{**DEFAULT_RULES[0], "enabled": False}]
        found = evaluate_rules(s, rules_cfg, {}, now)
    assert found == []


def test_stale_drift_excludes_vsphere_telemetry_fields(engine):
    """GitLab #162: live vSphere telemetry fields (uptime_seconds,
    memory_usage_mb, cpu_usage_mhz, ...) change every collection cycle by
    design and will therefore never naturally resolve — the stale_drift rule
    must skip them by field name even when the resource's domain isn't
    excluded, while a genuine config-drift field on the same resource still
    fires.
    """
    now = datetime.now(UTC)
    with Session(engine) as s:
        host = Resource(domain="vsphere", type="host", name="esx-01", source="VsphereAgent")
        s.add(host)
        s.flush()
        s.add(
            DriftEvent(
                resource_id=host.id,
                drift_type="state_drift",
                field="uptime_seconds",
                status="open",
                detected_at=now - timedelta(days=30),
            )
        )
        s.add(
            DriftEvent(
                resource_id=host.id,
                drift_type="config_drift",
                field="ssh_permit_root_login",
                status="open",
                detected_at=now - timedelta(days=30),
            )
        )
        s.commit()

        found = evaluate_rules(s, DEFAULT_RULES, {"stale_drift_days": 14}, now)

    stale_drift_findings = [f for f in found if f[0] == "stale_drift"]
    assert len(stale_drift_findings) == 1
    assert "ssh_permit_root_login" in stale_drift_findings[0][3]


def test_yaml_and_default_rules_exclude_fields_are_identical():
    """GitLab #162: rules/enforcement/compliance.yml's stale_drift entry and
    compliance_rules.DEFAULT_RULES's stale_drift entry must declare the exact
    same field-exclusion list — this rule has always had a two-places-in-
    lockstep hazard (see the module docstring / exclude_resource_domains),
    and a field-level exclusion list is just as easy to let drift apart.
    """
    yaml_cfg = yaml.safe_load(_COMPLIANCE_YAML.read_text())
    yaml_stale_drift = next(r for r in yaml_cfg["rules"] if r["name"] == "stale_drift")
    default_stale_drift = next(r for r in DEFAULT_RULES if r["name"] == "stale_drift")

    assert sorted(yaml_stale_drift.get("exclude_fields", [])) == sorted(
        default_stale_drift.get("exclude_fields", [])
    )
    assert sorted(yaml_stale_drift.get("exclude_resource_domains", [])) == sorted(
        default_stale_drift.get("exclude_resource_domains", [])
    )


def test_unaddressed_action_like_wildcards_are_escaped_not_interpreted(engine):
    """A hostname containing a literal ``_`` (e.g. ``web_01``, a common
    docker/K8s/homelab naming pattern) must not have that ``_`` interpreted
    as a SQL LIKE "match any single character" wildcard.

    Without escaping, ``ProposedAction.target.like(f"%:{host_name}")`` for
    ``host_name="web_01"`` builds the pattern ``%:web_01`` which, per LIKE
    semantics, also matches ``...:webA01`` for ANY character in place of the
    underscore — so an unrelated host's ProposedAction could spuriously
    suppress ``web_01``'s own unaddressed_critical_cve violation. This test
    seeds exactly that decoy action (targeting a *different* host,
    ``webA01``, that merely collides under wildcard interpretation) and
    asserts the violation still fires."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        cve_host = Resource(
            domain="vuln", type="r7_asset", name="web_01", source="VulnAgent"
        )
        s.add(cve_host)
        s.flush()
        s.add(
            R7Asset(
                resource_id=cve_host.id,
                r7_asset_id=1,
                hostname="web_01",
                vuln_critical=3,
            )
        )
        # Decoy: a ProposedAction for a DIFFERENT host ("webA01") that only
        # collides with "web_01" if '_' is treated as a wildcard rather than
        # a literal character. This must NOT be treated as addressing
        # web_01's CVE.
        s.add(
            ProposedAction(
                agent="remediation",
                action_type="vuln_patch",
                target="vm:webA01",
                created_at=now - timedelta(days=1),
            )
        )
        s.commit()

        thresholds = {"unaddressed_critical_cve_days": 30}
        found = evaluate_rules(s, DEFAULT_RULES, thresholds, now)

    by_rule = {f[0]: f for f in found}
    assert "unaddressed_critical_cve" in by_rule, (
        "decoy ProposedAction for an unrelated host ('webA01') incorrectly "
        "suppressed the violation for 'web_01' — LIKE wildcard not escaped"
    )
    cve = by_rule["unaddressed_critical_cve"]
    assert cve[1] == "web_01"


def test_unaddressed_action_still_matches_real_action_for_underscore_host(engine):
    """Sanity check the escaping fix doesn't break the legitimate case: a
    real ProposedAction whose target genuinely ends with ``:{host_name}``
    (underscore and all) must still suppress the violation."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        cve_host = Resource(
            domain="vuln", type="r7_asset", name="web_01", source="VulnAgent"
        )
        s.add(cve_host)
        s.flush()
        s.add(
            R7Asset(
                resource_id=cve_host.id,
                r7_asset_id=1,
                hostname="web_01",
                vuln_critical=3,
            )
        )
        s.add(
            ProposedAction(
                agent="remediation",
                action_type="vuln_patch",
                target="vm:web_01",
                created_at=now - timedelta(days=1),
            )
        )
        s.commit()

        thresholds = {"unaddressed_critical_cve_days": 30}
        found = evaluate_rules(s, DEFAULT_RULES, thresholds, now)

    assert "unaddressed_critical_cve" not in {f[0] for f in found}
