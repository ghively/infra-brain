"""Tests for the Rapid7-backed dashboard endpoints feeding the Fleet Assets,
Software Inventory, and CVE Detail pages.

Mirrors the existing dashboard_api test conventions: in-memory SQLite, the ORM
schema via ``Base.metadata.create_all``, and ``get_session`` patched to the test
engine. Auth-on vs auth-off is exercised via UI_COOKIE_SECRET.
"""

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    R7Asset,
    R7AssetConfig,
    R7Software,
    R7Vulnerability,
    R7VulnCve,
    Resource,
    VulnQueueItem,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def client(engine, monkeypatch):
    """Auth-off client (explicit dev-mode) for the data router.

    Item 1.5b (F-027/F-031) made dev-mode explicit: it used to be implied by
    "no UI_COOKIE_SECRET configured", now it requires INFRA_BRAIN_DEV=1.
    """
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.cve import cve_router
    from infra_brain.api.routers.fleet import fleet_router
    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.dashboard_api import router

    app = FastAPI()
    app.include_router(router)
    app.include_router(fleet_router)
    app.include_router(resources_router)
    app.include_router(cve_router)
    with (
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.fleet.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
        patch("infra_brain.api.routers.cve.get_session", _get_session),
    ):
        yield TestClient(app)


@pytest.fixture
def gated_client(engine):
    """Auth-enforced client (UI_COOKIE_SECRET set) for the data router."""

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.cve import cve_router
    from infra_brain.api.routers.fleet import fleet_router
    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.dashboard_api import router
    from infra_brain.dashboard_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(fleet_router)
    app.include_router(resources_router)
    app.include_router(cve_router)
    with (
        patch.dict(os.environ, {"UI_COOKIE_SECRET": "unit-test-secret"}),
        patch("infra_brain.dashboard_auth.get_session", _get_session),
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.fleet.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
    ):
        yield TestClient(app, raise_server_exceptions=True)


def _seed_r7(engine):
    """Two assets, software on each, two vuln defs + CVE bridge + vuln_queue."""
    with Session(engine) as s:
        a1 = R7Asset(
            r7_asset_id=101,
            ip="10.0.0.10",
            hostname="prod-web-01",
            os="Ubuntu Linux 22.04",
            os_product="Ubuntu Linux",
            os_version="22.04",
            os_vendor="Canonical",
            asset_type="host",
            risk_score=30000.0,
            vuln_critical=5,
            vuln_severe=3,
            vuln_moderate=2,
            vuln_total=10,
            vuln_exploits=1,
            assessed_for_vulnerabilities=True,
        )
        a2 = R7Asset(
            r7_asset_id=102,
            ip="10.0.0.11",
            hostname="win-db-02",
            os="Windows Server 2019",
            os_product="Windows Server",
            os_version="2019",
            os_vendor="Microsoft",
            asset_type="host",
            risk_score=500.0,
            vuln_critical=0,
            vuln_severe=1,
            vuln_moderate=4,
            vuln_total=5,
            vuln_exploits=0,
            assessed_for_vulnerabilities=False,
        )
        s.add_all([a1, a2])
        s.flush()

        s.add_all(
            [
                R7AssetConfig(asset_id=a1.id, name="cpu", value="8 cores"),
                R7AssetConfig(asset_id=a1.id, name="memory", value="32 GB"),
                # Shared product across both hosts (aggregation host_count == 2)
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
                # Product unique to a1
                R7Software(
                    asset_id=a1.id, r7_asset_id=101, product="nginx", vendor="F5", version="1.18.0"
                ),
            ]
        )

        v1 = R7Vulnerability(
            r7_vuln_id="openssl-cve-2023-0001",
            title="OpenSSL buffer overflow",
            severity="critical",
            cvss_v3_score=9.8,
            cvss_v3_vector="CVSS:3.1/AV:N",
            exploits=2,
            malware_kits=0,
            fix_available=True,
            pci_status="fail",
        )
        v2 = R7Vulnerability(
            r7_vuln_id="nginx-cve-2022-0002",
            title="nginx info disclosure",
            severity="moderate",
            cvss_v3_score=5.3,
            exploits=0,
            fix_available=False,
        )
        s.add_all([v1, v2])
        s.add_all(
            [
                R7VulnCve(r7_vuln_id="openssl-cve-2023-0001", cve_id="CVE-2023-0001"),
                R7VulnCve(r7_vuln_id="nginx-cve-2022-0002", cve_id="CVE-2022-0002"),
            ]
        )

        # Canonical vuln_queue rows for affected-host counts.
        r1 = Resource(domain="linux", type="host", name="prod-web-01", source="x", zone="corp")
        r2 = Resource(domain="linux", type="host", name="win-db-02", source="x", zone="corp")
        s.add_all([r1, r2])
        s.flush()
        now = datetime.now(timezone.utc)
        s.add_all(
            [
                VulnQueueItem(
                    resource_id=r1.id,
                    cve_id="CVE-2023-0001",
                    severity="critical",
                    sla_due=now - timedelta(days=1),
                    status="open",
                ),
                VulnQueueItem(
                    resource_id=r2.id,
                    cve_id="CVE-2023-0001",
                    severity="critical",
                    sla_due=now + timedelta(days=5),
                    status="open",
                ),
                # nginx CVE — one open queue row so it stays in the default
                # (open-backed) /vulns view alongside the OpenSSL CVE.
                VulnQueueItem(
                    resource_id=r1.id,
                    cve_id="CVE-2022-0002",
                    severity="moderate",
                    sla_due=now + timedelta(days=10),
                    status="open",
                ),
            ]
        )
        s.commit()


# ── Auth gating ─────────────────────────────────────────────────────────────


def test_fleet_requires_auth(gated_client):
    assert gated_client.get("/api/dashboard/fleet").status_code == 401


def test_software_requires_auth(gated_client):
    assert gated_client.get("/api/dashboard/software").status_code == 401


def test_cves_requires_auth(gated_client):
    assert gated_client.get("/api/dashboard/cves").status_code == 401


# ── Fleet Assets ──────────────────────────────────────────────────────────


def test_fleet_list_and_summary(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/fleet")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    # Highest risk first.
    assert body["items"][0]["hostname"] == "prod-web-01"
    assert body["items"][0]["risk_band"] == "critical"
    assert body["items"][0]["config_count"] == 2
    s = body["summary"]
    assert s["total_assets"] == 2
    assert s["assessed_assets"] == 1
    assert s["total_critical"] == 5
    assert {o["os_product"] for o in s["by_os"]} == {"Ubuntu Linux", "Windows Server"}
    bands = {b["band"]: b["count"] for b in s["by_risk_band"]}
    assert bands["critical"] == 1 and bands["low"] == 1


def test_fleet_search_and_os_filter(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/fleet", params={"q": "win-db"})
    assert r.status_code == 200
    assert [i["hostname"] for i in r.json()["items"]] == ["win-db-02"]
    r2 = client.get("/api/dashboard/fleet", params={"os": "Ubuntu"})
    assert [i["hostname"] for i in r2.json()["items"]] == ["prod-web-01"]


def test_fleet_pagination(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/fleet", params={"limit": 1, "offset": 1})
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["items"][0]["hostname"] == "win-db-02"


def test_fleet_risk_band_filter(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/fleet", params={"risk_band": "critical"})
    assert [i["hostname"] for i in r.json()["items"]] == ["prod-web-01"]


# ── Software Inventory ────────────────────────────────────────────────────


def test_software_aggregated(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/software")
    assert r.status_code == 200
    body = r.json()
    assert body["view"] == "aggregated"
    # 2 distinct (product, version) groups: OpenSSL/3.0.2 and nginx/1.18.0
    assert body["total"] == 2
    top = body["items"][0]
    assert top["product"] == "OpenSSL"
    assert top["host_count"] == 2
    sm = body["summary"]
    assert sm["total_records"] == 3
    assert sm["unique_products"] == 2
    assert sm["hosts_covered"] == 2


def test_software_detail_view(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/software", params={"view": "detail"})
    body = r.json()
    assert body["view"] == "detail"
    assert body["total"] == 3
    assert len(body["items"]) == 3
    assert all("hostname" in row for row in body["items"])


def test_software_search(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/software", params={"q": "nginx"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["product"] == "nginx"


def test_software_never_unbounded(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/software", params={"limit": 9999})
    # limit clamped to 200.
    assert r.json()["limit"] == 200


def test_software_vendors_list(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/software/vendors")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    # Seed data has vendors "F5" and "OpenSSL"; expect both, sorted.
    assert "F5" in body
    assert "OpenSSL" in body
    assert body == sorted(body)


def test_software_vendors_requires_auth(gated_client):
    assert gated_client.get("/api/dashboard/software/vendors").status_code == 401


def test_software_vendors_empty_table(client):
    """Vendors endpoint returns empty list when r7_software table is empty."""
    r = client.get("/api/dashboard/software/vendors")
    assert r.status_code == 200
    assert r.json() == []


# ── CVE Detail ────────────────────────────────────────────────────────────


def test_cve_list(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/cves")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    # Highest CVSS first.
    first = body["items"][0]
    assert first["cve_id"] == "CVE-2023-0001"
    assert first["cvss"] == 9.8
    assert first["affected_hosts"] == 2
    assert body["by_severity"]["critical"] == 1
    assert body["by_severity"]["moderate"] == 1


def test_cve_severity_filter(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/cves", params={"severity": "critical"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["cve_id"] == "CVE-2023-0001"


def _seed_open_vs_closed_cve(engine):
    """Two CVEs in the r7_vuln_cves bridge: one whose ONLY vuln_queue row is
    CLOSED (status="resolved"), one with an OPEN vuln_queue row. Used to prove
    the /vulns default counts only the open-backed CVE (TRK-113)."""
    with Session(engine) as s:
        s.add_all(
            [
                R7Vulnerability(
                    r7_vuln_id="slug-open-1",
                    title="open-backed cve",
                    severity="high",
                    cvss_v3_score=7.5,
                ),
                R7Vulnerability(
                    r7_vuln_id="slug-closed-1",
                    title="closed-only cve",
                    severity="critical",
                    cvss_v3_score=9.1,
                ),
            ]
        )
        s.add_all(
            [
                R7VulnCve(r7_vuln_id="slug-open-1", cve_id="CVE-OPEN-0001"),
                R7VulnCve(r7_vuln_id="slug-closed-1", cve_id="CVE-CLOSED-0002"),
            ]
        )
        r1 = Resource(domain="linux", type="host", name="h1", source="x", zone="corp")
        s.add(r1)
        s.flush()
        s.add_all(
            [
                VulnQueueItem(
                    resource_id=r1.id, cve_id="CVE-OPEN-0001", severity="high", status="open"
                ),
                VulnQueueItem(
                    resource_id=r1.id,
                    cve_id="CVE-CLOSED-0002",
                    severity="critical",
                    status="resolved",
                ),
            ]
        )
        s.commit()


def test_cve_list_defaults_to_open_backed(client, engine):
    """TRK-113: the /vulns default total must count only CVEs with >=1 open
    vuln_queue row, matching the /counts open_cves badge — not every CVE in
    the r7_vuln_cves bridge (which is not pruned when queue rows auto-close)."""
    _seed_open_vs_closed_cve(engine)

    # The badge counts distinct open cve_id → only CVE-OPEN-0001.
    counts = client.get("/api/dashboard/counts").json()
    assert counts["open_cves"] == 1

    body = client.get("/api/dashboard/cves").json()
    assert body["total"] == counts["open_cves"] == 1
    assert [it["cve_id"] for it in body["items"]] == ["CVE-OPEN-0001"]
    assert "critical" not in body["by_severity"]  # closed-only CVE excluded


def test_cve_detail(client, engine):
    _seed_r7(engine)
    r = client.get("/api/dashboard/cves/CVE-2023-0001")
    assert r.status_code == 200
    body = r.json()
    assert body["cve_id"] == "CVE-2023-0001"
    assert body["cvss"] == 9.8
    assert body["fix_available"] is True
    assert body["affected_host_count"] == 2
    assert {h["hostname"] for h in body["affected_hosts"]} == {"prod-web-01", "win-db-02"}
    assert body["r7_vuln_ids"] == ["openssl-cve-2023-0001"]


def test_cve_detail_404(client, engine):
    _seed_r7(engine)
    assert client.get("/api/dashboard/cves/CVE-9999-0000").status_code == 404


def test_cve_detail_db_error_degrades_instead_of_500(client, engine):
    """A DB/migration hiccup (OperationalError/ProgrammingError) on the detail
    route must degrade to a 200 with an empty-but-valid CveDetailOut, matching
    the list_cves convention — not bubble up as an unhandled 500."""
    _seed_r7(engine)

    class _BrokenSession:
        def query(self, *args, **kwargs):
            raise OperationalError("SELECT ...", {}, Exception("relation does not exist"))

    @contextmanager
    def _broken_get_session():
        yield _BrokenSession()

    with patch("infra_brain.api.routers.cve.get_session", _broken_get_session):
        r = client.get("/api/dashboard/cves/CVE-2023-0001")

    assert r.status_code == 200, f"Expected degraded 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["cve_id"] == "CVE-2023-0001"
    assert body["affected_hosts"] == []
    assert body["affected_host_count"] == 0
    assert body["solutions"] == []


# ── EOL PATCH /api/dashboard/eol/{eol_id}/migration ───────────────────────

from infra_brain.db.models import EolRegistry  # noqa: E402 — appended block


def _seed_eol_asset(engine, migration_path=None):
    """Seed a minimal Resource + EolRegistry row and return the EolRegistry id."""
    with Session(engine) as s:
        res = Resource(
            id=uuid.uuid4(),
            domain="eol",
            type="product",
            name="Windows Server 2012 R2",
            source="test",
            zone="corpor",
        )
        s.add(res)
        s.flush()
        asset = EolRegistry(
            id=uuid.uuid4(),
            resource_id=res.id,
            asset_name="Windows Server 2012 R2",
            eol_date=datetime(2023, 10, 10, tzinfo=timezone.utc),
            pci_risk_score=90,
            migration_path=migration_path,
        )
        s.add(asset)
        s.commit()
        return asset.id


def test_eol_migration_patch(client, engine):
    """PATCHing an existing EOL asset sets migration_path and returns it."""
    asset_id = _seed_eol_asset(engine)
    r = client.patch(
        f"/api/dashboard/eol/{asset_id}/migration",
        json={"migration_path": "Upgrade to Windows Server 2022"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["migration_path"] == "Upgrade to Windows Server 2022"
    assert body["id"] == str(asset_id)

    # Verify the value is persisted — re-fetch via list endpoint (now envelope).
    r2 = client.get("/api/dashboard/eol")
    assert r2.status_code == 200
    rows = r2.json()["items"]
    row = next((e for e in rows if e["id"] == str(asset_id)), None)
    assert row is not None
    assert row["migration"] == "Upgrade to Windows Server 2022"


def test_eol_migration_patch_404(client, engine):
    """PATCHing a non-existent EOL id returns 404."""
    fake_id = uuid.uuid4()
    r = client.patch(
        f"/api/dashboard/eol/{fake_id}/migration",
        json={"migration_path": "Does not matter"},
    )
    assert r.status_code == 404


def test_eol_migration_patch_requires_auth(gated_client, engine):
    """Without a valid session cookie the PATCH endpoint returns 401."""
    fake_id = uuid.uuid4()
    r = gated_client.patch(
        f"/api/dashboard/eol/{fake_id}/migration",
        json={"migration_path": "Should be blocked"},
    )
    assert r.status_code == 401
