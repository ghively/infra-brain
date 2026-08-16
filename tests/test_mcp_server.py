"""Tests for MCP server get_seeded_resources count accuracy.

Guards the anti-pattern where `count` reflected `len(resources)` after a
`.limit()` was applied, making the count always <= limit, not the true total.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    CollectionRun,
    DriftEvent,
    EolRegistry,
    HostCertificate,
    HostFirewallRule,
    HostSecurityPosture,
    HostShare,
    Instinct,
    InventoryReconcileEvent,
    ProposedAction,
    R7Asset,
    R7AssetAddress,
    R7AssetConfig,
    R7AssetUser,
    R7Software,
    R7Solution,
    R7VulnCve,
    R7Vulnerability,
    R7VulnSolution,
    Resource,
    RootCauseNote,
    VsphereAlarm,
    VsphereCluster,
    VsphereDatastore,
    VsphereHost,
    VspherePermission,
    VsphereSnapshot,
    VsphereVm,
    VulnQueueItem,
    WindowsLocalGroupMember,
    WindowsLocalUser,
)
from infra_brain.mcp_server import (
    get_asset_detail,
    get_collection_health,
    get_cve_detail,
    get_drift_events,
    get_eol_status,
    get_host_certificates,
    get_host_firewall_rules,
    get_host_security_posture,
    get_host_shares,
    get_instincts,
    get_sweep_status,
    get_inventory_gaps,
    get_remediation_solutions,
    get_remediation_suggestions,
    get_seeded_resources,
    get_software_inventory,
    get_vsphere_alarms,
    get_vsphere_clusters,
    get_vsphere_datastores,
    get_vsphere_hosts,
    get_vsphere_overview,
    get_vsphere_permissions,
    get_vsphere_snapshots,
    get_vsphere_vms,
    get_windows_local_admins,
    search_knowledge,
)

from tests.support.pg import make_engine


def _make_mock_resource(name: str, domain: str = "linux") -> MagicMock:
    r = MagicMock()
    r.id = uuid.uuid4()
    r.name = name
    r.domain = domain
    r.type = "host"
    r.metadata_ = {"ip_address": "10.0.0.1"}
    r.last_seen = None
    r.source = "manual"
    return r


def _make_mock_session(all_resources: list, total_count: int) -> MagicMock:
    """Build a mock session whose query chain produces the given resources and total."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    # The query chain: s.query(Resource).filter(...)[.filter(...)].count()
    # and then separately .order_by(...).limit(...).all()
    mock_query = MagicMock()
    mock_session.query.return_value = mock_query

    # filter() returns itself so chained .filter() calls work
    mock_query.filter.return_value = mock_query

    # .count() is called on the pre-limit query for total
    mock_query.count.return_value = total_count

    # .order_by().limit().all() returns the paged slice
    mock_ordered = MagicMock()
    mock_limited = MagicMock()
    mock_limited.all.return_value = all_resources
    mock_ordered.limit.return_value = mock_limited
    mock_query.order_by.return_value = mock_ordered

    return mock_session


class TestGetSeededResourcesCount:
    """get_seeded_resources must return the true total, not the page size."""

    def test_count_reflects_true_total_not_page_size(self):
        """When more resources exist than the limit, count must be the true total.

        This is the regression: previously count == len(resources) after .limit()
        was applied, so count could never exceed limit.
        """
        # Simulate 5 total resources in the DB, but limit=2
        all_resources_in_page = [_make_mock_resource(f"host-{i}") for i in range(2)]
        true_total = 5

        mock_session = _make_mock_session(
            all_resources=all_resources_in_page,
            total_count=true_total,
        )

        with patch("infra_brain.mcp_server.get_session", return_value=mock_session):
            result = get_seeded_resources(limit=2)

        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert len(result["resources"]) == 2, "Page should contain only limit=2 items"
        assert result["count"] == true_total, (
            f"count should be the true total ({true_total}), "
            f"not the page size ({len(result['resources'])})"
        )

    def test_count_equals_total_when_under_limit(self):
        """When fewer resources exist than limit, count should still be accurate."""
        all_resources_in_page = [_make_mock_resource("only-host")]
        true_total = 1

        mock_session = _make_mock_session(
            all_resources=all_resources_in_page,
            total_count=true_total,
        )

        with patch("infra_brain.mcp_server.get_session", return_value=mock_session):
            result = get_seeded_resources(limit=50)

        assert "error" not in result
        assert result["count"] == 1
        assert len(result["resources"]) == 1

    def test_count_with_domain_filter(self):
        """Domain filter is applied to both the total count and the page query."""
        paged = [_make_mock_resource("linux-host", domain="linux")]
        true_total = 10

        mock_session = _make_mock_session(
            all_resources=paged,
            total_count=true_total,
        )

        with patch("infra_brain.mcp_server.get_session", return_value=mock_session):
            result = get_seeded_resources(domain="linux", limit=1)

        assert "error" not in result
        assert result["count"] == 10
        assert len(result["resources"]) == 1

    def test_empty_result_returns_zero_count(self):
        """Empty DB for the given filter returns count=0."""
        mock_session = _make_mock_session(all_resources=[], total_count=0)

        with patch("infra_brain.mcp_server.get_session", return_value=mock_session):
            result = get_seeded_resources(limit=50)

        assert "error" not in result
        assert result["count"] == 0
        assert result["resources"] == []


class TestGetRemediationSuggestionsLimit:
    """get_remediation_suggestions had no limit param at all, so an MCP client
    calling it against a DB with thousands of pending ProposedAction rows would
    get every single one back unbounded — the exact failure a live infra-ops
    call hit against 18k+ pending rows. Real in-memory sqlite (not MagicMock
    chains), matching the Batch B vSphere tests' pattern — _row_to_dict() calls
    sqlalchemy.inspect() on each row, which only works on real mapped objects."""

    @pytest.fixture
    def remediation_db(self):
        eng = make_engine()
        with Session(eng) as s:
            yield s

    def _seed(self, session, count: int) -> None:
        session.add_all(
            [
                ProposedAction(
                    agent="remediation",
                    action_type="config_fix",
                    target=f"host-{i}",
                    status="pending",
                )
                for i in range(count)
            ]
        )
        session.commit()

    def test_default_limit_is_applied_not_unbounded(self, remediation_db):
        """Default call must cap the query — never return every row in the table."""
        self._seed(remediation_db, 75)

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(remediation_db)):
            result = get_remediation_suggestions()

        assert len(result["items"]) == 50, "default limit must cap the result, not return all 75 rows"
        assert result["total_count"] == 75

    def test_explicit_limit_is_passed_through(self, remediation_db):
        self._seed(remediation_db, 10)

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(remediation_db)):
            result = get_remediation_suggestions(limit=3)

        assert len(result["items"]) == 3
        assert result["total_count"] == 10

    def test_under_limit_returns_all_available(self, remediation_db):
        self._seed(remediation_db, 2)

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(remediation_db)):
            result = get_remediation_suggestions(limit=50)

        assert len(result["items"]) == 2
        assert result["total_count"] == 2

    def test_page_two_does_not_repeat_page_one(self, remediation_db):
        """TRK-272 / GitLab #145: paging with offset must not repeat rows
        and total_count must reflect the full matching set."""
        self._seed(remediation_db, 5)

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(remediation_db)):
            page1 = get_remediation_suggestions(limit=2, offset=0)
            page2 = get_remediation_suggestions(limit=2, offset=2)

        ids1 = {a["id"] for a in page1["items"]}
        ids2 = {a["id"] for a in page2["items"]}
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 2
        assert ids1.isdisjoint(ids2), "page 2 must not repeat any row from page 1"
        assert page1["total_count"] == 5
        assert page2["total_count"] == 5


class TestSearchKnowledge:
    """search_knowledge wraps infra_brain.embeddings.search_knowledge (RAG)."""

    def test_returns_helper_results_unchanged(self):
        """A patched helper's result list is returned verbatim."""
        sample = [
            {
                "title": "Disk usage runbook",
                "space": "OPS",
                "url": "https://confluence/x",
                "chunk_index": 0,
                "text": "check df -h",
                "source": "confluence",
            }
        ]
        # The tool imports `from infra_brain.embeddings import search_knowledge`
        # inside the function, so patch it at its definition site.
        with patch("infra_brain.embeddings.search_knowledge", return_value=sample):
            result = search_knowledge(query="disk usage", k=5)
        assert result == sample

    def test_disabled_rag_returns_empty_list(self):
        """With the real helper and rag_enabled False (default), returns []
        without touching the DB — the helper short-circuits."""
        result = search_knowledge(query="disk usage", k=5)
        assert result == []

    def test_error_is_captured_not_propagated(self):
        """A raising helper yields {'error': ...} rather than propagating."""
        with patch(
            "infra_brain.embeddings.search_knowledge",
            side_effect=RuntimeError("boom"),
        ):
            result = search_knowledge(query="disk usage", k=5)
        assert isinstance(result, dict)
        assert result["error"] == "boom"


# NOTE: the former _BearerAuth token tests (accepts/rejects/constant-time) were
# removed in the MCP auth overhaul — _BearerAuth no longer exists. Their
# replacement is the scoped-key middleware suite in tests/test_mcp_auth_middleware.py.



# ── Batch B — vSphere tools (GitLab #46) ──────────────────────────────────────
# Real in-memory sqlite (not MagicMock chains) so the ORM filters/joins are
# actually exercised, matching tests/test_mcp_auth_helpers.py's pattern.


@pytest.fixture
def vsphere_db():
    eng = make_engine()
    with Session(eng) as s:
        yield s


def _fake_get_session(session):
    """Wrap a live Session as the context-manager `get_session()` returns."""

    @contextmanager
    def _get_session():
        yield session

    return _get_session


class TestGetVsphereVms:
    def test_returns_seeded_vms(self, vsphere_db):
        vsphere_db.add(
            VsphereVm(
                vcenter="vc1",
                moref="vm-1",
                name="app01",
                esxi_host="esx01",
                power_state="poweredOn",
                is_template=False,
            )
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_vms()

        assert len(result) == 1
        assert result[0]["name"] == "app01"

    def test_filters_by_esxi_host(self, vsphere_db):
        vsphere_db.add_all(
            [
                VsphereVm(
                    vcenter="vc1",
                    moref="vm-1",
                    name="app01",
                    esxi_host="esx01",
                ),
                VsphereVm(
                    vcenter="vc1",
                    moref="vm-2",
                    name="app02",
                    esxi_host="esx02",
                ),
            ]
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_vms(esxi_host="esx02")

        assert len(result) == 1
        assert result[0]["name"] == "app02"

    def test_empty_db_returns_empty_list(self, vsphere_db):
        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_vms()
        assert result == []


class TestGetVsphereHosts:
    def test_returns_seeded_hosts(self, vsphere_db):
        vsphere_db.add(
            VsphereHost(vcenter="vc1", moref="host-1", name="esx01", cluster_name="prod")
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_hosts()

        assert len(result) == 1
        assert result[0]["cluster_name"] == "prod"

    def test_filters_by_cluster_name(self, vsphere_db):
        vsphere_db.add_all(
            [
                VsphereHost(vcenter="vc1", moref="host-1", name="esx01", cluster_name="prod"),
                VsphereHost(vcenter="vc1", moref="host-2", name="esx02", cluster_name="dev"),
            ]
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_hosts(cluster_name="dev")

        assert len(result) == 1
        assert result[0]["name"] == "esx02"

    def test_empty_db_returns_empty_list(self, vsphere_db):
        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_hosts()
        assert result == []


class TestGetVsphereDatastores:
    def test_returns_seeded_datastores(self, vsphere_db):
        vsphere_db.add(
            VsphereDatastore(
                vcenter="vc1",
                moref="ds-1",
                name="datastore1",
                datastore_type="VMFS",
                accessible=True,
            )
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_datastores()

        assert len(result) == 1
        assert result[0]["name"] == "datastore1"

    def test_filters_by_datastore_type(self, vsphere_db):
        vsphere_db.add_all(
            [
                VsphereDatastore(
                    vcenter="vc1", moref="ds-1", name="vmfs-ds", datastore_type="VMFS"
                ),
                VsphereDatastore(vcenter="vc1", moref="ds-2", name="nfs-ds", datastore_type="NFS"),
            ]
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_datastores(datastore_type="NFS")

        assert len(result) == 1
        assert result[0]["name"] == "nfs-ds"

    def test_empty_db_returns_empty_list(self, vsphere_db):
        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_datastores()
        assert result == []


class TestGetVsphereSnapshots:
    def test_returns_seeded_snapshots(self, vsphere_db):
        vsphere_db.add(
            VsphereSnapshot(
                vcenter="vc1",
                vm_moref="vm-1",
                vm_name="app01",
                snapshot_id=1,
                name="pre-patch",
                age_days=10,
            )
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_snapshots()

        assert len(result) == 1
        assert result[0]["vm_name"] == "app01"

    def test_filters_by_min_age_days(self, vsphere_db):
        vsphere_db.add_all(
            [
                VsphereSnapshot(
                    vcenter="vc1", vm_moref="vm-1", vm_name="app01", snapshot_id=1, age_days=5
                ),
                VsphereSnapshot(
                    vcenter="vc1", vm_moref="vm-2", vm_name="app02", snapshot_id=1, age_days=90
                ),
            ]
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_snapshots(min_age_days=30)

        assert len(result) == 1
        assert result[0]["vm_name"] == "app02"

    def test_empty_db_returns_empty_list(self, vsphere_db):
        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_snapshots()
        assert result == []


class TestGetVsphereClusters:
    def test_returns_seeded_clusters(self, vsphere_db):
        vsphere_db.add(
            VsphereCluster(vcenter="vc1", moref="cl-1", name="prod-cluster", datacenter_name="dc1")
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_clusters()

        assert len(result) == 1
        assert result[0]["name"] == "prod-cluster"

    def test_filters_by_datacenter_name(self, vsphere_db):
        vsphere_db.add_all(
            [
                VsphereCluster(
                    vcenter="vc1", moref="cl-1", name="cluster-a", datacenter_name="dc1"
                ),
                VsphereCluster(
                    vcenter="vc1", moref="cl-2", name="cluster-b", datacenter_name="dc2"
                ),
            ]
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_clusters(datacenter_name="dc2")

        assert len(result) == 1
        assert result[0]["name"] == "cluster-b"

    def test_empty_db_returns_empty_list(self, vsphere_db):
        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_clusters()
        assert result == []


class TestGetVsphereAlarms:
    def test_returns_seeded_alarms(self, vsphere_db):
        vsphere_db.add(
            VsphereAlarm(
                vcenter="vc1",
                alarm_name="Host connection failure",
                entity_name="esx01",
                entity_type="HostSystem",
                acknowledged=False,
            )
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_alarms()

        assert len(result) == 1
        assert result[0]["alarm_name"] == "Host connection failure"

    def test_filters_by_acknowledged(self, vsphere_db):
        vsphere_db.add_all(
            [
                VsphereAlarm(vcenter="vc1", alarm_name="alarm-a", acknowledged=True),
                VsphereAlarm(vcenter="vc1", alarm_name="alarm-b", acknowledged=False),
            ]
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_alarms(acknowledged=False)

        assert len(result) == 1
        assert result[0]["alarm_name"] == "alarm-b"

    def test_empty_db_returns_empty_list(self, vsphere_db):
        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_alarms()
        assert result == []


class TestGetVspherePermissions:
    def test_returns_seeded_permissions(self, vsphere_db):
        vsphere_db.add(
            VspherePermission(vcenter="vc1", principal="CORP\\jdoe", role_name="Administrator")
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_permissions()

        assert len(result) == 1
        assert result[0]["principal"] == "CORP\\jdoe"

    def test_filters_by_principal(self, vsphere_db):
        vsphere_db.add_all(
            [
                VspherePermission(vcenter="vc1", principal="CORP\\alice"),
                VspherePermission(vcenter="vc1", principal="CORP\\bob"),
            ]
        )
        vsphere_db.commit()

        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_permissions(principal="CORP\\bob")

        assert len(result) == 1
        assert result[0]["principal"] == "CORP\\bob"

    def test_empty_db_returns_empty_list(self, vsphere_db):
        with patch("infra_brain.mcp_server.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_permissions()
        assert result == []


class TestGetVsphereOverview:
    """get_vsphere_overview delegates to the dashboard router's vsphere_overview()
    (reused query logic, not re-derived) — patch get_session where that function
    reads it: infra_brain.api.routers.vsphere."""

    def test_returns_populated_overview(self, vsphere_db):
        vsphere_db.add(VsphereHost(vcenter="vc1", moref="host-1", name="esx01"))
        vsphere_db.add(VsphereVm(vcenter="vc1", moref="vm-1", name="app01"))
        vsphere_db.commit()

        with patch("infra_brain.api.routers.vsphere.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_overview()

        assert result["summary"]["host_count"] == 1
        assert result["summary"]["vm_count"] == 1
        assert result["hosts"][0]["name"] == "esx01"
        assert result["vms"][0]["name"] == "app01"

    def test_empty_db_renders_empty_state(self, vsphere_db):
        with patch("infra_brain.api.routers.vsphere.get_session", _fake_get_session(vsphere_db)):
            result = get_vsphere_overview()

        assert result["datacenters"] == []
        assert result["clusters"] == []
        assert result["hosts"] == []
        assert result["vms"] == []
        assert result["datastores"] == []
        assert result["summary"]["host_count"] == 0
        assert result["summary"]["vm_count"] == 0

# Batch D (GitLab #48) — vuln tools: get_cve_detail, get_software_inventory,
# get_remediation_solutions, get_asset_detail. (get_r7_sites, get_r7_tags, and
# all Octopus-branded tools removed — P7.1a/D6/D11.)
#
# Real in-memory SQLite session (not mocks) — these tools do multi-table
# joins/bridges (r7_vuln_cves, r7_vuln_solutions) that are much clearer to
# exercise against real ORM relationships than a MagicMock query chain.
# ---------------------------------------------------------------------------


def _make_sqlite_engine():
    engine = make_engine()
    return engine


def _get_session_ctx(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    return _get_session


def _seed_cve_data(engine):
    """One asset, one vuln def + CVE bridge + vuln_queue row + solution."""
    with Session(engine) as s:
        r1 = Resource(domain="linux", type="host", name="prod-web-01", source="x", zone="corp")
        s.add(r1)
        s.flush()

        v1 = R7Vulnerability(
            r7_vuln_id="openssl-cve-2023-0001",
            title="OpenSSL buffer overflow",
            severity="critical",
            cvss_v3_score=9.8,
            cvss_v3_vector="CVSS:3.1/AV:N",
            cvss_v2_score=7.0,
            risk_score=850.0,
            exploits=2,
            malware_kits=0,
            fix_available=True,
            pci_status="fail",
            pci_fail=True,
            denial_of_service=False,
            categories=["remote", "buffer overflow"],
        )
        s.add(v1)
        s.add(R7VulnCve(r7_vuln_id="openssl-cve-2023-0001", cve_id="CVE-2023-0001"))

        s.add(
            VulnQueueItem(
                resource_id=r1.id,
                cve_id="CVE-2023-0001",
                severity="critical",
                sla_due=datetime.now(UTC) - timedelta(days=1),
                status="open",
                kb_id="KB123456",
            )
        )

        sol = R7Solution(
            r7_solution_id="sol-openssl-1",
            summary="Upgrade OpenSSL to 3.0.9",
            steps="apt-get install openssl=3.0.9",
            solution_type="patch",
            estimate="30 minutes",
            fix_available=True,
        )
        s.add(sol)
        s.add(R7VulnSolution(r7_vuln_id="openssl-cve-2023-0001", r7_solution_id="sol-openssl-1"))
        s.commit()


class TestGetCveDetail:
    def test_success_returns_full_detail(self):
        engine = _make_sqlite_engine()
        _seed_cve_data(engine)
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_cve_detail("CVE-2023-0001")

        assert "error" not in result
        assert result["cve_id"] == "CVE-2023-0001"
        assert result["cvss"] == 9.8
        assert result["fix_available"] is True
        assert result["affected_host_count"] == 1
        assert result["affected_hosts"][0]["hostname"] == "prod-web-01"
        assert result["affected_hosts"][0]["kb_id"] == "KB123456"
        assert result["r7_vuln_ids"] == ["openssl-cve-2023-0001"]
        assert result["solutions"] == [
            {
                "summary": "Upgrade OpenSSL to 3.0.9",
                "steps": "apt-get install openssl=3.0.9",
                "solution_type": "patch",
                "estimate": "30 minutes",
            }
        ]
        assert result["sla_overdue_count"] == 1

    def test_not_found_returns_error_dict(self):
        engine = _make_sqlite_engine()
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_cve_detail("CVE-9999-9999")
        assert result == {"error": "CVE CVE-9999-9999 not found"}


class TestGetRemediationSolutions:
    def test_success_returns_solutions(self):
        engine = _make_sqlite_engine()
        _seed_cve_data(engine)
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_remediation_solutions("CVE-2023-0001")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["r7_solution_id"] == "sol-openssl-1"
        assert result[0]["summary"] == "Upgrade OpenSSL to 3.0.9"

    def test_empty_when_cve_known_but_no_solution_linked(self):
        engine = _make_sqlite_engine()
        with Session(engine) as s:
            s.add(R7Vulnerability(r7_vuln_id="slug-nosol", title="t", severity="low"))
            s.add(R7VulnCve(r7_vuln_id="slug-nosol", cve_id="CVE-0000-0001"))
            s.commit()
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_remediation_solutions("CVE-0000-0001")
        assert result == []

    def test_not_found_returns_error_dict(self):
        engine = _make_sqlite_engine()
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_remediation_solutions("CVE-9999-9999")
        assert result == {"error": "CVE CVE-9999-9999 not found"}


def _seed_software(engine):
    with Session(engine) as s:
        a1 = R7Asset(r7_asset_id=101, hostname="prod-web-01")
        a2 = R7Asset(r7_asset_id=102, hostname="win-db-02")
        s.add_all([a1, a2])
        s.flush()
        s.add_all(
            [
                R7Software(
                    asset_id=a1.id,
                    r7_asset_id=101,
                    product="OpenSSL",
                    vendor="OpenSSL",
                    version="3.0.2",
                ),
                R7Software(
                    asset_id=a2.id,
                    r7_asset_id=102,
                    product="OpenSSL",
                    vendor="OpenSSL",
                    version="3.0.2",
                ),
                R7Software(
                    asset_id=a1.id, r7_asset_id=101, product="nginx", vendor="F5", version="1.18.0"
                ),
            ]
        )
        s.commit()


class TestGetSoftwareInventory:
    def test_success_aggregates_by_product_version(self):
        engine = _make_sqlite_engine()
        _seed_software(engine)
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_software_inventory()
        assert len(result) == 2
        top = result[0]
        assert top["product"] == "OpenSSL"
        assert top["host_count"] == 2

    def test_product_filter(self):
        engine = _make_sqlite_engine()
        _seed_software(engine)
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_software_inventory(product="nginx")
        assert len(result) == 1
        assert result[0]["product"] == "nginx"

    def test_empty_table_returns_empty_list(self):
        engine = _make_sqlite_engine()
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_software_inventory()
        assert result == []


def _seed_asset_detail(engine):
    with Session(engine) as s:
        a1 = R7Asset(
            r7_asset_id=101,
            hostname="prod-web-01",
            ip="10.0.0.10",
            os="Ubuntu Linux 22.04",
            os_product="Ubuntu Linux",
            risk_score=30000.0,
        )
        s.add(a1)
        s.flush()
        s.add_all(
            [
                R7AssetConfig(asset_id=a1.id, name="cpu", value="8 cores"),
                R7AssetConfig(asset_id=a1.id, name="memory", value="32 GB"),
            ]
        )
        s.add(R7AssetUser(asset_id=a1.id, username="root", full_name="root"))
        s.add(R7AssetAddress(asset_id=a1.id, ip="10.0.0.10", mac="AA:BB:CC:DD:EE:FF"))
        s.commit()


class TestGetAssetDetail:
    def test_success_returns_configs_users_addresses(self):
        engine = _make_sqlite_engine()
        _seed_asset_detail(engine)
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_asset_detail("prod-web-01")

        assert "error" not in result
        assert result["hostname"] == "prod-web-01"
        assert result["ip"] == "10.0.0.10"
        assert {c["name"] for c in result["configs"]} == {"cpu", "memory"}
        assert result["users"] == [{"username": "root", "full_name": "root"}]
        assert result["addresses"] == [{"ip": "10.0.0.10", "mac": "AA:BB:CC:DD:EE:FF"}]

    def test_asset_with_no_children_returns_empty_lists(self):
        engine = _make_sqlite_engine()
        with Session(engine) as s:
            s.add(R7Asset(r7_asset_id=201, hostname="bare-host"))
            s.commit()
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_asset_detail("bare-host")
        assert result["configs"] == []
        assert result["users"] == []
        assert result["addresses"] == []

    def test_not_found_returns_error_dict(self):
        engine = _make_sqlite_engine()
        with patch("infra_brain.mcp_server.get_session", _get_session_ctx(engine)):
            result = get_asset_detail("does-not-exist")
        assert result == {"error": "asset not found for hostname 'does-not-exist'"}


# ── Host posture tools (Batch E / GitLab #49) ────────────────────────────────
# Real in-memory SQLite + ORM schema, same shape as
# tests/test_dashboard_host_posture.py (which these tools mirror query-for-
# query) rather than the MagicMock session pattern above, since these tools do
# real joins/filters worth exercising against real SQL.


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.mcp_server.get_session", _get_session):
        yield


def _seed_resource(engine, name: str = "win-app-01") -> uuid.UUID:
    with Session(engine) as s:
        r = Resource(domain="windows", type="host", name=name, source="WindowsAgent")
        s.add(r)
        s.commit()
        return r.id


class TestGetHostCertificates:
    def test_success_returns_seeded_certificates(self, engine, patched_session):
        rid = _seed_resource(engine)
        with Session(engine) as s:
            s.add(
                HostCertificate(
                    resource_id=rid,
                    store="LocalMachine/My",
                    subject="CN=win-app-01",
                    issuer="CN=Internal CA",
                    thumbprint="ABC123",
                    is_expired=False,
                )
            )
            s.commit()

        result = get_host_certificates("win-app-01")
        assert len(result) == 1
        assert result[0]["thumbprint"] == "ABC123"

    def test_unknown_hostname_returns_empty_list(self, patched_session):
        assert get_host_certificates("does-not-exist") == []

    def test_known_host_with_no_certs_returns_empty_list(self, engine, patched_session):
        _seed_resource(engine)
        assert get_host_certificates("win-app-01") == []


class TestGetHostSecurityPosture:
    def test_success_returns_posture_row(self, engine, patched_session):
        rid = _seed_resource(engine)
        with Session(engine) as s:
            s.add(
                HostSecurityPosture(
                    resource_id=rid,
                    firewall_enabled=True,
                    av_enabled=True,
                    rdp_enabled=False,
                    uac_enabled=True,
                )
            )
            s.commit()

        result = get_host_security_posture("win-app-01")
        assert result is not None
        assert result["firewall_enabled"] is True
        assert result["rdp_enabled"] is False

    def test_unknown_hostname_returns_none(self, patched_session):
        assert get_host_security_posture("does-not-exist") is None

    def test_known_host_with_no_posture_row_returns_none(self, engine, patched_session):
        _seed_resource(engine)
        assert get_host_security_posture("win-app-01") is None


class TestGetHostFirewallRules:
    def test_success_returns_seeded_rules(self, engine, patched_session):
        rid = _seed_resource(engine)
        with Session(engine) as s:
            s.add(
                HostFirewallRule(
                    resource_id=rid,
                    chain="INPUT",
                    rule_text="-A INPUT -p tcp --dport 22 -j ACCEPT",
                    action="ACCEPT",
                    source="iptables",
                )
            )
            s.commit()

        result = get_host_firewall_rules("win-app-01")
        assert len(result) == 1
        assert result[0]["action"] == "ACCEPT"

    def test_unknown_hostname_returns_empty_list(self, patched_session):
        assert get_host_firewall_rules("does-not-exist") == []

    def test_known_host_with_no_rules_returns_empty_list(self, engine, patched_session):
        _seed_resource(engine)
        assert get_host_firewall_rules("win-app-01") == []


class TestGetHostShares:
    def test_success_returns_seeded_shares(self, engine, patched_session):
        rid = _seed_resource(engine)
        with Session(engine) as s:
            s.add(HostShare(resource_id=rid, share_type="smb", name="C$", path="C:\\"))
            s.commit()

        result = get_host_shares("win-app-01")
        assert len(result) == 1
        assert result[0]["share_type"] == "smb"
        assert result[0]["name"] == "C$"

    def test_unknown_hostname_returns_empty_list(self, patched_session):
        assert get_host_shares("does-not-exist") == []

    def test_known_host_with_no_shares_returns_empty_list(self, engine, patched_session):
        _seed_resource(engine)
        assert get_host_shares("win-app-01") == []


class TestGetWindowsLocalAdmins:
    def test_success_combines_admin_users_and_group_members(self, engine, patched_session):
        rid = _seed_resource(engine)
        with Session(engine) as s:
            s.add(WindowsLocalUser(resource_id=rid, username="Administrator", is_admin=True))
            s.add(WindowsLocalUser(resource_id=rid, username="bob", is_admin=False))
            s.add(
                WindowsLocalGroupMember(
                    resource_id=rid, group_name="Administrators", member_name="Administrator"
                )
            )
            s.add(
                WindowsLocalGroupMember(
                    resource_id=rid, group_name="Users", member_name="bob"
                )
            )
            s.commit()

        result = get_windows_local_admins("win-app-01")
        # Only the admin user + the Administrators-group member — non-admin
        # user "bob" and the "Users" group membership must be filtered out.
        assert len(result) == 2
        sources = {r["source"] for r in result}
        assert sources == {"local_user", "group_member"}
        usernames = {r.get("username") for r in result if r["source"] == "local_user"}
        assert usernames == {"Administrator"}
        members = {r.get("member_name") for r in result if r["source"] == "group_member"}
        assert members == {"Administrator"}

    def test_unknown_hostname_returns_empty_list(self, patched_session):
        assert get_windows_local_admins("does-not-exist") == []

    def test_known_host_with_no_admin_rows_returns_empty_list(self, engine, patched_session):
        rid = _seed_resource(engine)
        with Session(engine) as s:
            # Non-admin user + non-admin group only — must yield [].
            s.add(WindowsLocalUser(resource_id=rid, username="bob", is_admin=False))
            s.add(WindowsLocalGroupMember(resource_id=rid, group_name="Users", member_name="bob"))
            s.commit()

        assert get_windows_local_admins("win-app-01") == []


# ── Overnight audit: unbounded-list-query sweep (T1) ─────────────────────────
# get_remediation_suggestions had no limit param at all, so an MCP client
# calling it against a DB with 18k+ pending ProposedAction rows got every row
# back unbounded (the live infra-ops "failed to work" report). Sweeping the
# rest of this file found five more list-returning tools with the same bug
# class: no limit param, no .limit() applied. Same real in-memory sqlite
# pattern as TestGetRemediationSuggestionsLimit / the host-posture tools above
# — MagicMock chains don't exercise _row_to_dict()'s sqlalchemy.inspect() call.


class TestGetEolStatusLimit:
    """get_eol_status joined EolRegistry -> Resource with no limit and no
    .limit() — a growing EOL registry (populated per-product-per-host by
    EOLAgent) would return unbounded."""

    def _seed(self, engine, count: int) -> None:
        with Session(engine) as s:
            for i in range(count):
                r = Resource(domain="eol", type="product", name=f"asset-{i}", source="EOLAgent")
                s.add(r)
                s.flush()
                s.add(EolRegistry(resource_id=r.id, asset_name=f"asset-{i}"))
            s.commit()

    def test_default_limit_is_applied_not_unbounded(self, engine, patched_session):
        self._seed(engine, 75)
        result = get_eol_status()
        assert len(result) == 50, "default limit must cap the result, not return all 75 rows"

    def test_explicit_limit_is_passed_through(self, engine, patched_session):
        self._seed(engine, 10)
        result = get_eol_status(limit=3)
        assert len(result) == 3

    def test_under_limit_returns_all_available(self, engine, patched_session):
        self._seed(engine, 2)
        result = get_eol_status(limit=50)
        assert len(result) == 2


class TestGetEolStatusNullDate:
    """GitLab #186: get_eol_status(days_until_eol=N) used a bare
    `eol_date <= cutoff`, so rows with a NULL eol_date never satisfied the
    comparison and vanished from the urgency-filtered result — the exact call
    meant to answer 'what is urgent right now'. A null date is unknown, which
    must be reviewed, so it belongs INSIDE the urgent window."""

    def _seed(self, engine) -> None:
        now = datetime.now(UTC)
        rows = [
            ("sles-11", None),  # unknown EOL date — must never be dropped
            ("centos-7", now - timedelta(days=400)),  # already past EOL
            ("rhel-8", now + timedelta(days=100)),  # inside a 365-day window
            ("rhel-9", now + timedelta(days=2000)),  # outside a 365-day window
        ]
        with Session(engine) as s:
            for name, eol in rows:
                r = Resource(domain="eol", type="product", name=name, source="EOLAgent")
                s.add(r)
                s.flush()
                s.add(EolRegistry(resource_id=r.id, asset_name=name, eol_date=eol))
            s.commit()

    def test_null_date_row_survives_the_urgency_filter(self, engine, patched_session):
        self._seed(engine)
        names = {row["asset_name"] for row in get_eol_status(days_until_eol=365)}
        assert "sles-11" in names, "null-eol_date row must not be dropped by days_until_eol"

    def test_urgency_filter_still_excludes_far_future_dates(self, engine, patched_session):
        self._seed(engine)
        names = {row["asset_name"] for row in get_eol_status(days_until_eol=365)}
        assert names == {"sles-11", "centos-7", "rhel-8"}

    def test_null_date_row_still_present_unfiltered(self, engine, patched_session):
        self._seed(engine)
        names = {row["asset_name"] for row in get_eol_status()}
        assert "sles-11" in names


class TestGetInventoryGapsLimit:
    """get_inventory_gaps queried InventoryReconcileEvent with no limit
    param at all and no .limit() — same bug class as get_remediation_suggestions."""

    def _seed(self, engine, count: int) -> None:
        with Session(engine) as s:
            s.add_all(
                [
                    InventoryReconcileEvent(
                        host=f"host-{i}",
                        domain="linux",
                        target_group="webservers",
                        status="proposed",
                    )
                    for i in range(count)
                ]
            )
            s.commit()

    def test_default_limit_is_applied_not_unbounded(self, engine, patched_session):
        self._seed(engine, 75)
        result = get_inventory_gaps()
        assert len(result["items"]) == 50, "default limit must cap the result, not return all 75 rows"
        assert result["total_count"] == 75

    def test_explicit_limit_is_passed_through(self, engine, patched_session):
        self._seed(engine, 10)
        result = get_inventory_gaps(limit=3)
        assert len(result["items"]) == 3
        assert result["total_count"] == 10

    def test_under_limit_returns_all_available(self, engine, patched_session):
        self._seed(engine, 2)
        result = get_inventory_gaps(limit=50)
        assert len(result["items"]) == 2
        assert result["total_count"] == 2

    def test_page_two_does_not_repeat_page_one(self, engine, patched_session):
        """TRK-272 / GitLab #145: paging with offset must not repeat rows
        and total_count must reflect the full matching set."""
        self._seed(engine, 5)

        page1 = get_inventory_gaps(limit=2, offset=0)
        page2 = get_inventory_gaps(limit=2, offset=2)

        ids1 = {e["id"] for e in page1["items"]}
        ids2 = {e["id"] for e in page2["items"]}
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 2
        assert ids1.isdisjoint(ids2), "page 2 must not repeat any row from page 1"
        assert page1["total_count"] == 5
        assert page2["total_count"] == 5


class TestGetDriftEventsLimit:
    """get_drift_events had an hours-window filter but no limit param and no
    .limit() — a busy domain within the window could still return unbounded."""

    def _seed(self, engine, count: int) -> uuid.UUID:
        with Session(engine) as s:
            r = Resource(domain="linux", type="host", name="drift-host", source="LinuxAgent")
            s.add(r)
            s.flush()
            s.add_all(
                [
                    DriftEvent(resource_id=r.id, drift_type="config", field=f"field-{i}")
                    for i in range(count)
                ]
            )
            s.commit()
            return r.id

    def test_default_limit_is_applied_not_unbounded(self, engine, patched_session):
        self._seed(engine, 150)
        result = get_drift_events()
        assert len(result["items"]) == 100, "default limit must cap the result, not return all 150 rows"
        assert result["total_count"] == 150

    def test_explicit_limit_is_passed_through(self, engine, patched_session):
        self._seed(engine, 10)
        result = get_drift_events(limit=3)
        assert len(result["items"]) == 3
        assert result["total_count"] == 10

    def test_under_limit_returns_all_available(self, engine, patched_session):
        self._seed(engine, 2)
        result = get_drift_events(limit=100)
        assert len(result["items"]) == 2
        assert result["total_count"] == 2

    def test_page_two_does_not_repeat_page_one(self, engine, patched_session):
        """TRK-272 / GitLab #145: paging with offset must not repeat rows
        and total_count must reflect the full matching set."""
        self._seed(engine, 5)

        page1 = get_drift_events(limit=2, offset=0)
        page2 = get_drift_events(limit=2, offset=2)

        ids1 = {e["id"] for e in page1["items"]}
        ids2 = {e["id"] for e in page2["items"]}
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 2
        assert ids1.isdisjoint(ids2), "page 2 must not repeat any row from page 1"
        assert page1["total_count"] == 5
        assert page2["total_count"] == 5


class TestGetDriftEventsExcludesGraphMaintenance:
    """TRK-191: graph_maintenance's own "graph-health" report resource

    (domain="graph_maintenance") captures the maintenance agent's own
    ever-changing internal stats, never real fleet drift, and must be
    excluded from get_drift_events() by default.
    """

    def _seed(self, engine) -> None:
        with Session(engine) as s:
            fleet = Resource(domain="linux", type="host", name="drift-host", source="LinuxAgent")
            gm = Resource(
                domain="graph_maintenance",
                type="graph_maintenance_report",
                name="graph-health",
                source="GraphMaintenanceAgent",
            )
            s.add_all([fleet, gm])
            s.flush()
            s.add(DriftEvent(resource_id=fleet.id, drift_type="config", field="real-field"))
            s.add(
                DriftEvent(
                    resource_id=gm.id, drift_type="config", field="timings.prune"
                )
            )
            s.commit()

    def test_default_excludes_graph_maintenance(self, engine, patched_session):
        self._seed(engine)
        result = get_drift_events()
        assert len(result["items"]) == 1
        assert result["items"][0]["resource_domain"] == "linux"

    def test_explicit_domain_filter_can_still_request_graph_maintenance(
        self, engine, patched_session
    ):
        self._seed(engine)
        result = get_drift_events(domain="graph_maintenance")
        assert len(result["items"]) == 1
        assert result["items"][0]["resource_domain"] == "graph_maintenance"

    def test_include_graph_maintenance_flag_mixes_it_back_in(self, engine, patched_session):
        self._seed(engine)
        result = get_drift_events(include_graph_maintenance=True)
        assert len(result["items"]) == 2


class TestGetDriftEventsHasNote:
    """Phase 2 (2026-07-29 plan) has_note filter: False -> anti-join (no
    RootCauseNote), True -> semi-join (has a RootCauseNote), None (default)
    must be a byte-identical no-op vs. every pre-existing caller."""

    def _seed(self, engine) -> tuple[uuid.UUID, uuid.UUID]:
        with Session(engine) as s:
            r = Resource(domain="linux", type="host", name="drift-host", source="LinuxAgent")
            s.add(r)
            s.flush()
            noted = DriftEvent(resource_id=r.id, drift_type="config", field="noted-field")
            unnoted = DriftEvent(resource_id=r.id, drift_type="config", field="unnoted-field")
            s.add_all([noted, unnoted])
            s.flush()
            s.add(
                RootCauseNote(
                    drift_event_id=noted.id, explanation="why", correlated={"source": "x"}
                )
            )
            s.commit()
            return noted.id, unnoted.id

    def test_default_none_preserves_existing_behavior(self, engine, patched_session):
        """Regression guard: omitting has_note must return exactly what the
        tool returned before this param existed — every field on every row."""
        self._seed(engine)
        with_default = get_drift_events()
        without_param = get_drift_events(
            status="open",
            hours=24,
            domain=None,
            limit=100,
            offset=0,
            include_graph_maintenance=False,
        )
        assert with_default == without_param
        assert len(with_default["items"]) == 2

    def test_has_note_true_returns_only_noted_events(self, engine, patched_session):
        noted_id, _unnoted_id = self._seed(engine)
        result = get_drift_events(has_note=True)
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == noted_id

    def test_has_note_false_returns_only_unnoted_events(self, engine, patched_session):
        _noted_id, unnoted_id = self._seed(engine)
        result = get_drift_events(has_note=False)
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == unnoted_id

    def test_has_note_composes_with_existing_filters(self, engine, patched_session):
        self._seed(engine)
        result = get_drift_events(has_note=False, limit=1)
        assert len(result["items"]) == 1


class TestGetCollectionHealthLimit:
    """get_collection_health had an hours-window filter but no limit param and
    no .limit() — the collection_runs table grows with every sweep across ~25
    domains and the hours window is caller-controlled, so a wide window (or a
    busy environment) could still return unbounded."""

    def _seed(self, engine, count: int) -> None:
        now = datetime.now(UTC)
        with Session(engine) as s:
            s.add_all(
                [
                    CollectionRun(
                        domain="linux",
                        trigger_type="scheduled",
                        finished_at=now - timedelta(minutes=i),
                        status="success",
                    )
                    for i in range(count)
                ]
            )
            s.commit()

    def test_default_limit_is_applied_not_unbounded(self, engine, patched_session):
        self._seed(engine, 150)
        result = get_collection_health()
        assert len(result) == 100, "default limit must cap the result, not return all 150 rows"

    def test_explicit_limit_is_passed_through(self, engine, patched_session):
        self._seed(engine, 10)
        result = get_collection_health(limit=3)
        assert len(result) == 3

    def test_under_limit_returns_all_available(self, engine, patched_session):
        self._seed(engine, 2)
        result = get_collection_health(limit=100)
        assert len(result) == 2


class TestGetInstinctsLimit:
    """get_instincts queried Instinct with no limit param at all and no
    .limit() — Instinct rows are written by multiple agents (coverage.py,
    drift_learning.py), not just human-approved promote_instinct calls, so the
    table is not inherently small."""

    def _seed(self, engine, count: int) -> None:
        with Session(engine) as s:
            s.add_all(
                [
                    Instinct(
                        zone="corpor",
                        domain="linux",
                        pattern=f"pattern-{i}",
                        confidence=0.9,
                        promoted_by="tester",
                    )
                    for i in range(count)
                ]
            )
            s.commit()

    def test_default_limit_is_applied_not_unbounded(self, engine, patched_session):
        self._seed(engine, 75)
        result = get_instincts()
        assert len(result) == 50, "default limit must cap the result, not return all 75 rows"

    def test_explicit_limit_is_passed_through(self, engine, patched_session):
        self._seed(engine, 10)
        result = get_instincts(limit=3)
        assert len(result) == 3

    def test_under_limit_returns_all_available(self, engine, patched_session):
        self._seed(engine, 2)
        result = get_instincts(limit=50)
        assert len(result) == 2


class TestGetSweepStatusSurfacesErrorMessage:
    """GitLab #159/#160: get_sweep_status hand-builds its output dict and used to
    omit error_message, even though collection_runs.error_message is persisted
    and get_collection_health has always returned it.

    That single omission is what made #159 look mysterious: the tool reported a
    'failed' run with 0 resources and gave no reason, while the reason sat one
    column away in the same row that was already loaded."""

    def _seed(self, engine) -> None:
        now = datetime.now(UTC)
        with Session(engine) as s:
            s.add_all(
                [
                    CollectionRun(
                        domain="discovery",
                        trigger_type="scheduled",
                        started_at=now - timedelta(minutes=5),
                        finished_at=now - timedelta(minutes=1),
                        status="failed",
                        resources_found=0,
                        error_message="collect() timed out after 300s",
                    ),
                    CollectionRun(
                        domain="linux",
                        trigger_type="scheduled",
                        started_at=now - timedelta(minutes=5),
                        finished_at=now - timedelta(minutes=2),
                        status="success",
                        resources_found=42,
                    ),
                ]
            )
            s.commit()

    def test_error_message_key_is_present_for_every_domain(self, engine, patched_session):
        self._seed(engine)
        rows = get_sweep_status()
        assert rows, "expected one row per domain"
        for row in rows:
            assert "error_message" in row, f"{row['domain']} row omits error_message"

    def test_failed_run_reports_why_it_failed(self, engine, patched_session):
        self._seed(engine)
        rows = {r["domain"]: r for r in get_sweep_status()}
        failed = rows["discovery"]
        assert failed["status"] == "failed"
        assert failed["resources_found"] == 0
        # The whole point: a 0-resource failed run is no longer unexplained.
        assert failed["error_message"] == "collect() timed out after 300s"

    def test_successful_run_reports_no_error(self, engine, patched_session):
        self._seed(engine)
        rows = {r["domain"]: r for r in get_sweep_status()}
        assert rows["linux"]["error_message"] is None
