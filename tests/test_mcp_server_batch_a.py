"""Tests for MCP Batch A — cross-domain tools (GitLab #45).

get_host_profile, get_host_vulns, get_host_purpose_map, get_fleet_counts,
get_host_context — each mirrors an existing dashboard route's query logic
(api/routers/hosts.py, api/routers/fleet.py) or, for get_host_context
(GitLab #125), composes several of those same joins into one cross-domain
response. Uses a real in-memory sqlite engine (not MagicMock chains) because
these tools do multi-table joins that are far more reliably exercised against
a real session than hand-mocked query chains — matching the pattern in
tests/test_host_purpose_map_api.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    ComplianceViolation,
    DriftEvent,
    EolRegistry,
    HostIdentity,
    HostPurposeMap,
    InventoryReconcileEvent,
    R7Asset,
    R7Solution,
    R7VulnCve,
    R7Vulnerability,
    R7VulnSolution,
    Resource,
    VsphereVm,
    VulnQueueItem,
)

from tests.support.pg import make_engine


def _now():
    return datetime.now(UTC)


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def session_patch(engine, monkeypatch):
    """Patch infra_brain.mcp_server.get_session to yield a real sqlite Session."""

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr("infra_brain.mcp_server.get_session", _get_session)
    return engine


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        yield s
        s.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# get_host_profile
# ─────────────────────────────────────────────────────────────────────────────


class TestGetHostProfile:
    def test_success_returns_full_identity_row(self, session_patch, db):
        from infra_brain.mcp_server import get_host_profile

        r7_res = Resource(domain="rapid7", type="asset", name="web-01", source="r7")
        vsphere_res = Resource(domain="vsphere", type="vm", name="web-01", source="vsphere")
        db.add_all([r7_res, vsphere_res])
        db.flush()

        db.add(
            HostIdentity(
                short_hostname="web-01",
                fqdn="web-01.corp.example.com",
                ip_addresses=["10.0.0.5"],
                r7_resource_id=r7_res.id,
                vsphere_resource_id=vsphere_res.id,
                os_family="linux",
                risk_score=42,
                vuln_count=3,
                patch_status="current",
                vsphere_power_state="poweredOn",
                octopus_machine_status=None,
                last_reconciled=_now(),
            )
        )
        db.commit()

        result = get_host_profile("web-01")

        assert "error" not in result
        assert result["short_hostname"] == "web-01"
        assert result["fqdn"] == "web-01.corp.example.com"
        assert result["r7_resource_id"] == str(r7_res.id)
        assert result["vsphere_resource_id"] == str(vsphere_res.id)
        assert result["octopus_resource_id"] is None
        assert result["os_family"] == "linux"
        assert result["risk_score"] == 42

    def test_case_insensitive_hostname_lookup(self, session_patch, db):
        from infra_brain.mcp_server import get_host_profile

        db.add(HostIdentity(short_hostname="db-01", ip_addresses=[]))
        db.commit()

        result = get_host_profile("DB-01")
        assert "error" not in result
        assert result["short_hostname"] == "db-01"

    def test_not_found_returns_error_dict(self, session_patch, db):
        from infra_brain.mcp_server import get_host_profile

        result = get_host_profile("does-not-exist")
        assert result == {"error": "Host 'does-not-exist' not found"}

    def test_fqdn_input_matches_short_hostname_row(self, session_patch, db):
        """GitLab #121 Bug A: short_hostname is always stored in first-DNS-label
        form; passing a full FQDN must truncate at query time (normalize_host())
        the same way the write side does, not silently return zero results."""
        from infra_brain.mcp_server import get_host_profile

        db.add(HostIdentity(short_hostname="web01", ip_addresses=[]))
        db.commit()

        result = get_host_profile("web01.corp.example.com")
        assert "error" not in result
        assert result["short_hostname"] == "web01"


# ─────────────────────────────────────────────────────────────────────────────
# get_host_vulns
# ─────────────────────────────────────────────────────────────────────────────


class TestGetHostVulns:
    def test_success_walks_full_join_to_solution_summary(self, session_patch, db):
        from infra_brain.mcp_server import get_host_vulns

        r7_res = Resource(domain="rapid7", type="asset", name="app-01", source="r7")
        db.add(r7_res)
        db.flush()

        db.add(
            HostIdentity(
                short_hostname="app-01",
                ip_addresses=[],
                r7_resource_id=r7_res.id,
                risk_score=10,
            )
        )
        db.add(
            R7Asset(
                resource_id=r7_res.id,
                r7_asset_id=1001,
                risk_score=88.5,
                vuln_critical=1,
                vuln_severe=2,
                vuln_moderate=0,
            )
        )
        db.add(
            VulnQueueItem(
                resource_id=r7_res.id,
                cve_id="CVE-2024-0001",
                kb_id="KB123",
                severity="critical",
                status="open",
                sla_due=_now() + timedelta(days=5),
                last_updated=_now(),
            )
        )
        db.add(
            R7VulnCve(r7_vuln_id="vendor-product-cve-2024-0001", cve_id="CVE-2024-0001")
        )
        db.add(
            R7Vulnerability(
                r7_vuln_id="vendor-product-cve-2024-0001",
                title="Sample RCE",
                cvss_v3_score=9.8,
                exploits=2,
                fix_available=True,
                pci_fail=True,
            )
        )
        db.add(
            R7VulnSolution(
                r7_vuln_id="vendor-product-cve-2024-0001", r7_solution_id="fix-001"
            )
        )
        db.add(
            R7Solution(r7_solution_id="fix-001", summary="Apply vendor patch v2.3")
        )
        db.commit()

        result = get_host_vulns("app-01")

        assert "error" not in result
        assert result["header"]["risk_score"] == 88.5
        assert result["header"]["vuln_critical"] == 1
        assert result["total"] == 1
        item = result["items"][0]
        assert item["cve_id"] == "CVE-2024-0001"
        assert item["cvss_v3"] == 9.8
        assert item["title"] == "Sample RCE"
        assert item["pci_fail"] is True
        assert item["solution_summary"] == "Apply vendor patch v2.3"

    def test_host_with_no_r7_resource_returns_empty_items(self, session_patch, db):
        """A host_identities row with no r7_resource_id (never scanned by
        Rapid7) must not raise — join simply has nothing to walk."""
        from infra_brain.mcp_server import get_host_vulns

        db.add(HostIdentity(short_hostname="octo-only-01", ip_addresses=[], risk_score=0))
        db.commit()

        result = get_host_vulns("octo-only-01")

        assert "error" not in result
        assert result["items"] == []
        assert result["total"] == 0
        assert result["header"]["hostname"] == "octo-only-01"

    def test_fqdn_input_matches_short_hostname_row(self, session_patch, db):
        """GitLab #121 Bug A twin: get_host_vulns must also truncate an FQDN
        input to first-DNS-label form before matching short_hostname."""
        from infra_brain.mcp_server import get_host_vulns

        db.add(HostIdentity(short_hostname="octo-only-01", ip_addresses=[], risk_score=0))
        db.commit()

        result = get_host_vulns("octo-only-01.corp.example.com")

        assert "error" not in result
        assert result["items"] == []
        assert result["total"] == 0

    def test_not_found_returns_error_dict(self, session_patch, db):
        from infra_brain.mcp_server import get_host_vulns

        result = get_host_vulns("ghost-host")
        assert result == {"error": "Host 'ghost-host' not found"}

    def test_default_status_unions_open_and_triage(self, session_patch, db):
        """GitLab #136 / #188 Bug 1 regression: the old default ``status="open"``
        did an EXACT match, so a CVE promoted to "triage" (where
        VulnTriageAgent moves promoted criticals) was silently excluded from
        the default call. It must union open+triage, matching
        get_vulnerabilities' #136 fix."""
        from infra_brain.mcp_server import get_host_vulns

        r7_res = Resource(domain="rapid7", type="asset", name="triaged-01", source="r7")
        db.add(r7_res)
        db.flush()
        db.add(HostIdentity(short_hostname="triaged-01", ip_addresses=[], r7_resource_id=r7_res.id))
        db.add(
            VulnQueueItem(
                resource_id=r7_res.id,
                cve_id="CVE-2026-1111",
                severity="critical",
                status="triage",
                sla_due=_now() + timedelta(days=1),
                last_updated=_now(),
            )
        )
        db.add(
            VulnQueueItem(
                resource_id=r7_res.id,
                cve_id="CVE-2026-1112",
                severity="high",
                status="open",
                sla_due=_now() + timedelta(days=5),
                last_updated=_now(),
            )
        )
        db.add(
            VulnQueueItem(
                resource_id=r7_res.id,
                cve_id="CVE-2026-1113",
                severity="low",
                status="resolved",
                sla_due=_now() + timedelta(days=30),
                last_updated=_now(),
            )
        )
        db.commit()

        result = get_host_vulns("triaged-01")

        assert "error" not in result
        assert result["total"] == 2, "default status must union open+triage, not exact-match open"
        cve_ids = {item["cve_id"] for item in result["items"]}
        assert cve_ids == {"CVE-2026-1111", "CVE-2026-1112"}
        assert "CVE-2026-1113" not in cve_ids  # resolved is correctly excluded

    def test_explicit_triage_status_still_exact_matches(self, session_patch, db):
        """Passing an explicit non-default status must still be an exact
        match (only the "open" default gets the open+triage union)."""
        from infra_brain.mcp_server import get_host_vulns

        r7_res = Resource(domain="rapid7", type="asset", name="triaged-02", source="r7")
        db.add(r7_res)
        db.flush()
        db.add(HostIdentity(short_hostname="triaged-02", ip_addresses=[], r7_resource_id=r7_res.id))
        db.add(
            VulnQueueItem(
                resource_id=r7_res.id,
                cve_id="CVE-2026-1121",
                severity="critical",
                status="triage",
                sla_due=_now() + timedelta(days=1),
                last_updated=_now(),
            )
        )
        db.add(
            VulnQueueItem(
                resource_id=r7_res.id,
                cve_id="CVE-2026-1122",
                severity="high",
                status="open",
                sla_due=_now() + timedelta(days=5),
                last_updated=_now(),
            )
        )
        db.commit()

        result = get_host_vulns("triaged-02", status="triage")

        assert result["total"] == 1
        assert result["items"][0]["cve_id"] == "CVE-2026-1121"

    def test_coverage_gap_flagged_when_rollup_exceeds_vuln_queue(self, session_patch, db):
        """GitLab #188 Bug 2 (750-asset-cap coverage gap, visibility
        mitigation): when the uncapped r7_assets rollup reports more
        critical/severe/moderate CVEs than exist in vuln_queue across ALL
        statuses, the header must flag it rather than silently returning a
        partial item list with no signal."""
        from infra_brain.mcp_server import get_host_vulns

        r7_res = Resource(domain="rapid7", type="asset", name="capped-out-01", source="r7")
        db.add(r7_res)
        db.flush()
        db.add(
            HostIdentity(short_hostname="capped-out-01", ip_addresses=[], r7_resource_id=r7_res.id)
        )
        db.add(
            R7Asset(
                resource_id=r7_res.id,
                r7_asset_id=2002,
                risk_score=10366.0,
                vuln_critical=10,
                vuln_severe=0,
                vuln_moderate=0,
            )
        )
        # No VulnQueueItem rows at all — this host fell outside the
        # 750-asset cap on the run(s) that would have populated vuln_queue.
        db.commit()

        result = get_host_vulns("capped-out-01", status="")

        assert result["items"] == []
        assert result["total"] == 0
        assert result["header"]["vuln_queue_coverage_gap"] is True
        assert "coverage_note" in result["header"]
        assert "750" in result["header"]["coverage_note"] or "cap" in result["header"]["coverage_note"]

    def test_coverage_gap_not_flagged_when_counts_agree(self, session_patch, db):
        """The visibility flag must not fire when vuln_queue coverage is
        consistent with the rollup — no false positives on healthy hosts."""
        from infra_brain.mcp_server import get_host_vulns

        r7_res = Resource(domain="rapid7", type="asset", name="healthy-01", source="r7")
        db.add(r7_res)
        db.flush()
        db.add(HostIdentity(short_hostname="healthy-01", ip_addresses=[], r7_resource_id=r7_res.id))
        db.add(
            R7Asset(
                resource_id=r7_res.id,
                r7_asset_id=2003,
                risk_score=50.0,
                vuln_critical=1,
                vuln_severe=0,
                vuln_moderate=0,
            )
        )
        db.add(
            VulnQueueItem(
                resource_id=r7_res.id,
                cve_id="CVE-2026-1131",
                severity="critical",
                status="open",
                sla_due=_now() + timedelta(days=3),
                last_updated=_now(),
            )
        )
        db.commit()

        result = get_host_vulns("healthy-01")

        assert result["header"]["vuln_queue_coverage_gap"] is False
        assert "coverage_note" not in result["header"]


# ─────────────────────────────────────────────────────────────────────────────
# get_host_purpose_map
# ─────────────────────────────────────────────────────────────────────────────


class TestGetHostPurposeMap:
    def test_success_returns_rows_sorted_by_hostname(self, session_patch, db):
        from infra_brain.mcp_server import get_host_purpose_map

        db.add(
            HostPurposeMap(
                hostname="web-01",
                purpose="Primary web frontend",
                vlan=None,
                subnet=None,
                source="5:playbooks/system_inventory.yml",
            )
        )
        db.add(
            HostPurposeMap(
                hostname="db-01",
                purpose="Primary database",
                vlan="VLAN20-App",
                subnet="10.90.12.0/24",
                source="5:playbooks/system_inventory.yml",
            )
        )
        db.commit()

        result = get_host_purpose_map()

        assert [r["hostname"] for r in result] == ["db-01", "web-01"]
        assert result[0]["vlan"] == "VLAN20-App"
        assert result[1]["vlan"] is None

    def test_empty_returns_empty_list(self, session_patch, db):
        from infra_brain.mcp_server import get_host_purpose_map

        result = get_host_purpose_map()
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# get_fleet_counts
# ─────────────────────────────────────────────────────────────────────────────


class TestGetFleetCounts:
    def test_success_aggregates_all_four_counts(self, session_patch, db):
        from infra_brain.mcp_server import get_fleet_counts

        res = Resource(domain="linux", type="host", name="host-01", source="linux")
        res2 = Resource(domain="linux", type="host", name="host-02", source="linux")
        db.add_all([res, res2])
        db.flush()

        # Open + resolved drift — only the open one should count.
        db.add(DriftEvent(resource_id=res.id, drift_type="config", field="x", status="open"))
        db.add(DriftEvent(resource_id=res.id, drift_type="config", field="y", status="resolved"))

        # Same CVE affecting two different hosts (open + triage, both
        # "open-ish") -> counted ONCE by distinct cve_id, plus one resolved
        # row (different CVE) that must not count.
        db.add(
            VulnQueueItem(
                resource_id=res.id, cve_id="CVE-2024-0002", severity="high", status="open"
            )
        )
        db.add(
            VulnQueueItem(
                resource_id=res2.id, cve_id="CVE-2024-0002", severity="high", status="triage"
            )
        )
        db.add(
            VulnQueueItem(
                resource_id=res.id, cve_id="CVE-2024-0003", severity="low", status="resolved"
            )
        )

        # One overdue EOL, one not-yet-due.
        db.add(
            EolRegistry(
                resource_id=res.id, asset_name="old-os", eol_date=_now() - timedelta(days=30)
            )
        )
        db.add(
            EolRegistry(
                resource_id=res.id, asset_name="new-os", eol_date=_now() + timedelta(days=365)
            )
        )

        # One proposed, one merged inventory-reconcile event.
        db.add(
            InventoryReconcileEvent(
                host="new-host-01", domain="linux", target_group="webservers", status="proposed"
            )
        )
        db.add(
            InventoryReconcileEvent(
                host="new-host-02", domain="linux", target_group="webservers", status="merged"
            )
        )
        db.commit()

        result = get_fleet_counts()

        assert result["open_drift"] == 1
        assert result["open_cves"] == 1  # distinct cve_id among open-ish statuses
        assert result["eol_overdue"] == 1
        assert result["invrec_proposed"] == 1

    def test_empty_db_returns_all_zeros(self, session_patch, db):
        from infra_brain.mcp_server import get_fleet_counts

        result = get_fleet_counts()

        assert result == {
            "open_drift": 0,
            "open_cves": 0,
            "eol_overdue": 0,
            "invrec_proposed": 0,
        }

    def test_graph_maintenance_open_drift_excluded_from_fleet_count(self, session_patch, db):
        """TRK-191: graph_maintenance's own "graph-health" report resource is
        never real fleet infrastructure — an open DriftEvent against it must
        not inflate the fleet-wide open_drift count."""
        from infra_brain.mcp_server import get_fleet_counts

        fleet_res = Resource(domain="linux", type="host", name="host-01", source="linux")
        gm_res = Resource(
            domain="graph_maintenance",
            type="graph_maintenance_report",
            name="graph-health",
            source="GraphMaintenanceAgent",
        )
        db.add_all([fleet_res, gm_res])
        db.flush()

        db.add(DriftEvent(resource_id=fleet_res.id, drift_type="config", field="x", status="open"))
        db.add(
            DriftEvent(
                resource_id=gm_res.id, drift_type="config", field="timings.prune", status="open"
            )
        )
        db.commit()

        result = get_fleet_counts()

        assert result["open_drift"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# get_host_context (GitLab #125)
# ─────────────────────────────────────────────────────────────────────────────


class TestGetHostContext:
    def test_success_full_data_across_all_domains(self, session_patch, db):
        """Host with vSphere placement (plus a co-resident VM on the same
        esxi_host), an open CVE, non-graph_maintenance drift, and an open
        compliance violation — every section of the join populated."""
        from infra_brain.mcp_server import get_host_context

        r7_res = Resource(domain="rapid7", type="asset", name="web-01", source="r7")
        vsphere_res = Resource(domain="vsphere", type="vm", name="web-01", source="vsphere")
        db.add_all([r7_res, vsphere_res])
        db.flush()

        db.add(
            HostIdentity(
                short_hostname="web-01",
                fqdn="web-01.corp.example.com",
                ip_addresses=["10.0.0.5"],
                r7_resource_id=r7_res.id,
                vsphere_resource_id=vsphere_res.id,
                os_family="linux",
                risk_score=42,
            )
        )

        db.add(
            VsphereVm(
                resource_id=vsphere_res.id,
                vcenter="vc-01",
                moref="vm-100",
                name="web-01",
                esxi_host="esxi-a",
                datastore_names=["ds-01", "ds-02"],
            )
        )
        # Co-resident VM on the same esxi_host — should count, but not
        # itself appear in the placement dict.
        db.add(
            VsphereVm(
                resource_id=None,
                vcenter="vc-01",
                moref="vm-101",
                name="other-vm",
                esxi_host="esxi-a",
            )
        )

        db.add(
            VulnQueueItem(
                resource_id=r7_res.id,
                cve_id="CVE-2024-0001",
                severity="critical",
                status="open",
            )
        )
        db.add(R7VulnCve(r7_vuln_id="vendor-cve-2024-0001", cve_id="CVE-2024-0001"))
        db.add(
            R7Vulnerability(
                r7_vuln_id="vendor-cve-2024-0001",
                title="Sample RCE",
                cvss_v3_score=9.8,
                fix_available=True,
            )
        )

        db.add(
            DriftEvent(
                resource_id=r7_res.id, drift_type="config", field="risk_score", status="open"
            )
        )

        db.add(
            ComplianceViolation(
                rule="disk-encryption",
                host="web-01",
                severity="high",
                status="open",
            )
        )
        db.commit()

        result = get_host_context("web-01")

        assert "error" not in result
        assert result["identity"]["short_hostname"] == "web-01"

        assert result["vsphere_placement"]["esxi_host"] == "esxi-a"
        assert result["vsphere_placement"]["datastores"] == ["ds-01", "ds-02"]
        assert result["vsphere_placement"]["co_resident_vm_count"] == 1

        assert len(result["top_cves"]) == 1
        assert result["top_cves"][0]["cve_id"] == "CVE-2024-0001"
        assert result["top_cves"][0]["cvss_v3"] == 9.8

        assert len(result["non_telemetry_drift"]) == 1
        assert result["non_telemetry_drift"][0]["field"] == "risk_score"

        assert result["compliance_status"]["open_violation_count"] == 1
        assert result["compliance_status"]["violations"][0]["rule"] == "disk-encryption"

    def test_success_partial_data_some_domains_empty(self, session_patch, db):
        """Host with only an identity row — no vSphere, no CVEs, no drift, no
        compliance rows. Every section must return its empty default rather
        than raising or being omitted."""
        from infra_brain.mcp_server import get_host_context

        db.add(HostIdentity(short_hostname="octo-only-01", ip_addresses=[], risk_score=0))
        db.commit()

        result = get_host_context("octo-only-01")

        assert "error" not in result
        assert result["identity"]["short_hostname"] == "octo-only-01"
        assert result["vsphere_placement"] == {
            "esxi_host": None,
            "datastores": [],
            "co_resident_vm_count": 0,
        }
        assert result["top_cves"] == []
        assert result["non_telemetry_drift"] == []
        assert result["compliance_status"] == {"open_violation_count": 0, "violations": []}

    def test_graph_maintenance_drift_excluded(self, session_patch, db):
        """TRK-191's exclusion applies here too — a host's own graph_maintenance
        telemetry (if it were somehow linked) must never surface as real drift."""
        from infra_brain.mcp_server import get_host_context

        r7_res = Resource(domain="rapid7", type="asset", name="app-01", source="r7")
        gm_res = Resource(
            domain="graph_maintenance",
            type="graph_maintenance_report",
            name="graph-health",
            source="GraphMaintenanceAgent",
        )
        db.add_all([r7_res, gm_res])
        db.flush()

        db.add(HostIdentity(short_hostname="app-01", ip_addresses=[], r7_resource_id=r7_res.id))
        db.add(
            DriftEvent(resource_id=r7_res.id, drift_type="config", field="real", status="open")
        )
        db.commit()

        result = get_host_context("app-01")

        assert len(result["non_telemetry_drift"]) == 1
        assert result["non_telemetry_drift"][0]["field"] == "real"

    def test_fqdn_input_normalized_to_short_hostname(self, session_patch, db):
        """TRK-189's normalize_host() reused — an FQDN input must resolve the
        same host_identities row as its short hostname."""
        from infra_brain.mcp_server import get_host_context

        db.add(HostIdentity(short_hostname="web02", ip_addresses=[]))
        db.commit()

        result = get_host_context("web02.corp.example.com")

        assert "error" not in result
        assert result["identity"]["short_hostname"] == "web02"

    def test_not_found_returns_error_dict(self, session_patch, db):
        from infra_brain.mcp_server import get_host_context

        result = get_host_context("ghost-host")
        assert result == {"error": "Host 'ghost-host' not found"}


# ─────────────────────────────────────────────────────────────────────────────
# get_vulnerabilities (GitLab #136 — status semantics + pagination)
# ─────────────────────────────────────────────────────────────────────────────


class TestGetVulnerabilities:
    def _seed(self, db, n_open=0, n_triage=0, severity="high"):
        res = Resource(domain="vuln", type="r7_asset", name="srv-01", source="VulnAgent")
        db.add(res)
        db.flush()
        i = 0
        for status, count in (("open", n_open), ("triage", n_triage)):
            for _ in range(count):
                i += 1
                db.add(
                    VulnQueueItem(
                        resource_id=res.id,
                        cve_id=f"CVE-2026-{10000 + i}",
                        severity=severity,
                        status=status,
                        sla_due=_now() + timedelta(days=i),
                        last_updated=_now(),
                    )
                )
        db.commit()
        return res

    def test_default_open_status_includes_triaged_criticals(self, session_patch, db):
        """GitLab #136 finding 1 regression: VulnTriageAgent flips EVERY
        critical to status="triage" (is_high_priority is unconditionally True
        for criticals), so the old exact ``status == "open"`` filter made
        ``get_vulnerabilities(severity="critical")`` return zero rows fleet-
        wide while the remediation queue still carried critical proposals.
        The default "open" filter must mean OPEN_VULN_STATUSES (open+triage)."""
        from infra_brain.mcp_server import get_vulnerabilities

        self._seed(db, n_open=0, n_triage=3, severity="critical")

        result = get_vulnerabilities(severity="critical")
        rows = result["items"]
        assert len(rows) == 3, "triaged criticals must be visible under the default filter"
        assert result["total_count"] == 3
        assert all(r["status"] == "triage" for r in rows)

    def test_explicit_status_is_exact_match(self, session_patch, db):
        from infra_brain.mcp_server import get_vulnerabilities

        self._seed(db, n_open=2, n_triage=1)

        assert len(get_vulnerabilities(status="triage")["items"]) == 1
        assert len(get_vulnerabilities(status="resolved")["items"]) == 0
        # Default "open" = open + triage.
        assert len(get_vulnerabilities()["items"]) == 3

    def test_offset_pages_through_full_result_set(self, session_patch, db):
        """GitLab #136 finding 2: with only ``limit`` and no offset, at most
        one page of the queue was ever retrievable. Paging must cover the
        whole set with stable, non-overlapping pages."""
        from infra_brain.mcp_server import get_vulnerabilities

        self._seed(db, n_open=7)

        result1 = get_vulnerabilities(limit=3, offset=0)
        result2 = get_vulnerabilities(limit=3, offset=3)
        result3 = get_vulnerabilities(limit=3, offset=6)
        page1, page2, page3 = result1["items"], result2["items"], result3["items"]
        ids = [r["cve_id"] for r in page1 + page2 + page3]
        assert len(page1) == 3 and len(page2) == 3 and len(page3) == 1
        assert len(ids) == len(set(ids)) == 7, "pages must not overlap or drop rows"
        # Stable sla_due ordering across pages.
        assert ids == sorted(ids)
        # TRK-272 / GitLab #145: total_count reflects the full matching set,
        # not just the current page, and is identical across every page.
        assert result1["total_count"] == result2["total_count"] == result3["total_count"] == 7

    def test_page_two_does_not_repeat_page_one(self, session_patch, db):
        """TRK-272 / GitLab #145: page 2 must be disjoint from page 1 and
        total_count must reflect the full result set, not the page size."""
        from infra_brain.mcp_server import get_vulnerabilities

        self._seed(db, n_open=5)

        page1 = get_vulnerabilities(limit=2, offset=0)
        page2 = get_vulnerabilities(limit=2, offset=2)

        ids1 = {r["cve_id"] for r in page1["items"]}
        ids2 = {r["cve_id"] for r in page2["items"]}
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 2
        assert ids1.isdisjoint(ids2), "page 2 must not repeat any row from page 1"
        assert page1["total_count"] == 5
        assert page2["total_count"] == 5
