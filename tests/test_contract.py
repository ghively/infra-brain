"""Contract tests: every bare-list collection route must return {items, total, limit, offset}.

Task 5.3 — Phase 5 contract rework.

Each test hits a real in-memory DB via TestClient and asserts the envelope shape.
Routes that are documented exceptions (flat options-list strings) are noted inline.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from infra_brain.db.models import (
    AgentActionLog,
    AgentDecisionLog,
    Base,
    CollectionRun,
    ComplianceViolation,
    DriftEvent,
    EolRegistry,
    GeneratedScript,
    Instinct,
    IntegrationProposal,
    InventoryReconcileEvent,
    Observation,
    ProposedAction,
    R7Asset,
    R7Software,
    Resource,
    ScanPoint,
)

# ---------------------------------------------------------------------------
# Shared test infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture(scope="module")
def seeded_engine(engine):
    """Seed one row of each relevant model type."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(
            domain="linux",
            type="host",
            name="contract-test-host",
            source="LinuxAgent",
            zone="corpor",
        )
        s.add(r)
        s.flush()

        s.add(
            DriftEvent(
                resource_id=r.id,
                drift_type="config_drift",
                field="kernel",
                old_value={"v": "a"},
                new_value={"v": "b"},
                status="open",
            )
        )
        s.add(
            EolRegistry(
                resource_id=r.id,
                asset_name="Ubuntu 18.04",
                eol_date=now - timedelta(days=30),
            )
        )
        s.add(
            InventoryReconcileEvent(
                host="contract-host-02",
                domain="linux",
                target_group="discovered",
                status="proposed",
            )
        )
        s.add(
            Instinct(
                domain="linux",
                zone="corpor",
                pattern="test-pattern",
                confidence=0.9,
                promoted_by="TestAgent",
            )
        )
        s.add(
            Observation(
                agent="linux",
                tool="ansible",
                domain="linux",
                pattern="reboot",
                count=3,
                last_seen=now,
            )
        )
        s.add(
            GeneratedScript(
                name="test.sh",
                language="bash",
                purpose="test",
                content="echo hi",
                content_sha256="abc123",
                created_by_agent="linux",
                domain="linux",
            )
        )
        s.add(
            IntegrationProposal(
                type="vsphere",
                endpoint="https://vc01",
                confidence=0.8,
                status="pending",
                source="DiscoveryAgent",
            )
        )
        s.add(
            AgentActionLog(
                agent="linux",
                domain="linux",
                tool="ssh_run",
                verdict="allow",
                status="ok",
            )
        )
        s.add(
            AgentDecisionLog(
                agent="linux",
                domain="linux",
                decision_summary="decided",
            )
        )
        s.add(
            CollectionRun(
                domain="linux",
                trigger_type="cron",
                status="completed",
                started_at=now - timedelta(hours=1),
            )
        )
        s.add(ScanPoint(domain="linux", method="ansible", endpoint="/inv", schedule="0 */6 * * *"))
        s.add(
            ComplianceViolation(
                rule="pci-req-6",
                severity="high",
                host="contract-test-host",
                detail="outdated OS",
                status="open",
                detected_at=now,
            )
        )
        s.add(
            ProposedAction(
                agent="remediation",
                action_type="patch",
                target="contract-test-host",
                confidence=0.8,
                status="pending",
            )
        )
        s.commit()
    return engine


@pytest.fixture(scope="module")
def client(seeded_engine):
    engine = seeded_engine

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.fleet import fleet_router
    from infra_brain.api.routers.governance import (
        governance_audit_router,
        governance_drift_router,
        governance_intelligence_router,
        governance_ops_router,
    )
    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.api.routers.ui import ui_router
    from infra_brain.api.routers.vuln import vuln_router
    from infra_brain.dashboard_auth import require_admin, require_session

    app = FastAPI()
    app.include_router(fleet_router)
    # governance split: register all four sub-routers so every governance
    # endpoint is reachable in the contract test client.
    app.include_router(governance_drift_router)
    app.include_router(governance_audit_router)
    app.include_router(governance_intelligence_router)
    app.include_router(governance_ops_router)
    app.include_router(resources_router)
    app.include_router(ui_router)
    app.include_router(vuln_router)

    # Not an auth test — bypass require_session via dependency_overrides (module
    # scope can't use monkeypatch/INFRA_BRAIN_DEV, and dependency_overrides is
    # the robust FastAPI-native way to bypass a Depends() regardless of which
    # module imported the shared require_session function).
    app.dependency_overrides[require_session] = lambda: None
    # TRK-321 made GET /settings admin-gated. require_admin is a DIFFERENT
    # callable, so overriding require_session alone no longer reaches it — this
    # shape/contract client must bypass both. Auth behaviour itself is asserted
    # in tests/test_dashboard_auth.py, not here.
    app.dependency_overrides[require_admin] = lambda: None

    with (
        patch("infra_brain.api.routers.fleet.get_session", _get_session),
        # governance split: patch get_session in each sub-router module
        patch("infra_brain.api.routers.governance_drift.get_session", _get_session),
        patch("infra_brain.api.routers.governance_audit.get_session", _get_session),
        patch("infra_brain.api.routers.governance_intelligence.get_session", _get_session),
        patch("infra_brain.api.routers.governance_ops.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
        patch("infra_brain.api.routers.ui.get_session", _get_session),
        patch("infra_brain.api.routers.vuln.get_session", _get_session),
    ):
        yield TestClient(app)


def _assert_envelope(data: dict, *, min_items: int = 0) -> None:
    """Assert the response is a valid {items, total, limit, offset} envelope."""
    assert isinstance(data, dict), f"Expected dict envelope, got {type(data).__name__}: {data!r}"
    assert "items" in data, f"Missing 'items' key: {data.keys()}"
    assert "total" in data, f"Missing 'total' key: {data.keys()}"
    assert "limit" in data, f"Missing 'limit' key: {data.keys()}"
    assert "offset" in data, f"Missing 'offset' key: {data.keys()}"
    assert isinstance(data["items"], list), "'items' must be a list"
    assert isinstance(data["total"], int), "'total' must be an int"
    assert isinstance(data["limit"], int), "'limit' must be an int"
    assert isinstance(data["offset"], int), "'offset' must be an int"
    assert len(data["items"]) >= min_items, (
        f"Expected at least {min_items} items, got {len(data['items'])}"
    )


# ---------------------------------------------------------------------------
# fleet.py routes
# ---------------------------------------------------------------------------


def test_collection_runs_envelope(client):
    resp = client.get("/api/dashboard/collection_runs")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_scan_points_envelope(client):
    resp = client.get("/api/dashboard/scan_points")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_system_health_envelope(client):
    resp = client.get("/api/dashboard/system_health")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_software_vendors_is_bare_list_exception(client):
    """DOCUMENTED EXCEPTION: /software/vendors returns list[str] (options list, not row data).

    This is a simple options-list endpoint used to populate a filter chip bar.
    Each item is a bare string (vendor name), not a row object. Converting to an
    envelope would break the filter-chip UI pattern with no benefit. Left as
    list[str] per Task 5.3 spec exception logic.
    """
    resp = client.get("/api/dashboard/software/vendors")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list), "software/vendors must remain a bare list[str]"


# ---------------------------------------------------------------------------
# hosts.py routes
# ---------------------------------------------------------------------------


def test_resource_snapshots_envelope(client, seeded_engine):
    # Get a resource_id from the seeded DB
    with Session(seeded_engine) as s:
        r = s.query(Resource).filter_by(name="contract-test-host").first()
        resource_id = str(r.id)

    # No snapshots seeded (Snapshot requires a FK to collection_runs), so total=0 is fine.
    # The key assertion is the envelope shape is returned rather than a bare list.
    resp = client.get(f"/api/dashboard/resources/{resource_id}/snapshots")
    assert resp.status_code == 200
    _assert_envelope(resp.json())


def test_eol_envelope(client):
    resp = client.get("/api/dashboard/eol")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


# ---------------------------------------------------------------------------
# governance.py routes — part A: drift/notifications/instincts/observations
# ---------------------------------------------------------------------------


def test_drift_events_domains_is_bare_list_exception(client):
    """DOCUMENTED EXCEPTION: /drift_events/domains returns list[str] (domain names).

    This is a flat options-list of domain name strings used to populate a filter
    chip bar. Each item is a plain string, not a row object. Converting to an
    envelope would be inappropriate for this options-list pattern. Left as
    list[str] per Task 5.3 spec exception logic.
    """
    resp = client.get("/api/dashboard/drift_events/domains")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list), "drift_events/domains must remain a bare list[str]"


def test_drift_events_envelope(client):
    resp = client.get("/api/dashboard/drift_events")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_notifications_envelope(client):
    resp = client.get("/api/dashboard/notifications")
    assert resp.status_code == 200
    _assert_envelope(resp.json())


def test_instincts_envelope(client):
    resp = client.get("/api/dashboard/instincts")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_observations_envelope(client):
    resp = client.get("/api/dashboard/observations")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


# ---------------------------------------------------------------------------
# governance.py routes — part B: scripts/proposals/activity/decisions/reconcile/compliance/agents/settings/agent-config
# ---------------------------------------------------------------------------


def test_generated_scripts_envelope(client):
    resp = client.get("/api/dashboard/generated_scripts")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_integration_proposals_envelope(client):
    resp = client.get("/api/dashboard/integration_proposals")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_activity_envelope(client):
    resp = client.get("/api/dashboard/activity")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_decisions_envelope(client):
    resp = client.get("/api/dashboard/decisions")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_inventory_reconcile_envelope(client):
    resp = client.get("/api/dashboard/inventory_reconcile")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_compliance_envelope(client):
    resp = client.get("/api/dashboard/compliance")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


def test_agents_envelope(client):
    resp = client.get("/api/dashboard/agents")
    assert resp.status_code == 200
    _assert_envelope(resp.json())


def test_settings_envelope(client):
    """DOCUMENTED: /settings returns list[SettingGroup] — grouped config objects.

    SettingGroup is genuine row data (each group has a name and rows of setting
    key-value pairs). Converted to envelope per Task 5.3.
    """
    resp = client.get("/api/dashboard/settings")
    assert resp.status_code == 200
    _assert_envelope(resp.json())


def test_agent_config_envelope(client):
    resp = client.get("/api/dashboard/agent-config")
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    # Every row echoes the live dispatchable__<domain> pause state so the UI can
    # render it on load rather than only knowing about a pause the user
    # performed in this browser session.
    assert all(isinstance(row["paused"], bool) for row in body["items"])


# ---------------------------------------------------------------------------
# vuln.py routes
# ---------------------------------------------------------------------------


def test_remediation_envelope(client):
    resp = client.get("/api/dashboard/remediation")
    assert resp.status_code == 200
    _assert_envelope(resp.json(), min_items=1)


# ---------------------------------------------------------------------------
# ui.py routes
# ---------------------------------------------------------------------------


def test_custom_views_envelope(client):
    resp = client.get("/api/dashboard/views")
    assert resp.status_code == 200
    _assert_envelope(resp.json())


# ---------------------------------------------------------------------------
# Critical fix #2: list_runs total/items agreement with status filter
# ---------------------------------------------------------------------------


@pytest.fixture()
def runs_engine():
    """Isolated in-memory DB with mixed-status CollectionRun rows for status-filter tests."""
    from datetime import datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    now = datetime.now(UTC)
    with Session(eng) as s:
        # 3 completed → display "success"
        for i in range(3):
            s.add(
                CollectionRun(
                    domain=f"linux{i}",
                    trigger_type="cron",
                    status="completed",
                    started_at=now - timedelta(hours=i + 1),
                )
            )
        # 2 failed → display "failure"
        for i in range(2):
            s.add(
                CollectionRun(
                    domain=f"win{i}",
                    trigger_type="manual",
                    status="failed",
                    started_at=now - timedelta(hours=i + 10),
                )
            )
        # 1 in_progress → display "running"
        s.add(
            CollectionRun(
                domain="vsphere",
                trigger_type="webhook",
                status="in_progress",
                started_at=now - timedelta(minutes=5),
            )
        )
        s.commit()
    return eng


@pytest.fixture()
def runs_client(runs_engine):
    from contextlib import contextmanager
    from unittest.mock import patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from infra_brain.api.routers.fleet import fleet_router
    from infra_brain.dashboard_auth import require_session

    @contextmanager
    def _get_session():
        with Session(runs_engine) as s:
            yield s

    app = FastAPI()
    app.include_router(fleet_router)
    # NOTE: mock.patch("infra_brain.api.routers.fleet.require_session", ...) does not
    # actually bypass auth — fleet_router uses dependencies=[Depends(require_session)]
    # at router-definition time, so FastAPI already holds a direct reference to the
    # real function; patching the module attribute afterward never reaches it.
    # dependency_overrides is FastAPI's supported mechanism and matches by the
    # callable object itself.
    app.dependency_overrides[require_session] = lambda: None
    with patch("infra_brain.api.routers.fleet.get_session", new=_get_session):
        yield TestClient(app)


def test_list_runs_status_filter_total_matches_items(runs_client):
    """total must reflect only status-matching rows; items must match that count.

    Seeds 3 completed + 2 failed + 1 in_progress.
    Requests status=success (maps to DB 'completed') with limit=2 (smaller than 3).
    Expected: total=3, len(items)=2 (paginated), NOT total=6 (unfiltered).
    """
    resp = runs_client.get("/api/dashboard/collection_runs?status=success&limit=2&offset=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3, (
        f"total should be 3 (only 'success' rows), got {body['total']} — "
        "status filter was applied in Python instead of SQL"
    )
    assert len(body["items"]) == 2, f"Expected 2 items (limit=2), got {len(body['items'])}"
    assert all(r["status"] == "success" for r in body["items"]), (
        f"All items must have status='success', got: {[r['status'] for r in body['items']]}"
    )


def test_list_runs_status_filter_second_page(runs_client):
    """Pagination must not skip rows — offset=2 should return the remaining 1 success row."""
    resp = runs_client.get("/api/dashboard/collection_runs?status=success&limit=2&offset=2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 1, (
        f"Second page should have 1 remaining success row, got {len(body['items'])}"
    )
    assert body["items"][0]["status"] == "success"


def test_list_runs_no_status_filter_returns_all(runs_client):
    """Without a status filter, total must reflect all 6 seeded rows."""
    resp = runs_client.get("/api/dashboard/collection_runs?limit=100&offset=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 6, f"Expected total=6 (all rows), got {body['total']}"


def test_list_runs_unknown_display_status_returns_empty(runs_client):
    """A status value with no _RUN_STATUS mapping returns empty items and total=0."""
    resp = runs_client.get("/api/dashboard/collection_runs?status=nonexistent")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


# ---------------------------------------------------------------------------
# Task 5.4 — /software: items + view discriminator
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def software_engine():
    """In-memory DB seeded with R7Asset + R7Software rows for software view tests."""
    import uuid as _uuid

    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        asset_id = _uuid.uuid4()
        asset = R7Asset(
            id=asset_id,
            r7_asset_id=9001,
            hostname="sw-test-host",
        )
        s.add(asset)
        s.flush()
        # Three distinct software rows
        for i in range(3):
            s.add(
                R7Software(
                    asset_id=asset_id,
                    r7_asset_id=9001,
                    product=f"TestPkg{i}",
                    vendor="ACME",
                    version=f"1.{i}.0",
                    software_type="application",
                )
            )
        s.commit()
    return eng


@pytest.fixture(scope="module")
def software_client(software_engine):
    from contextlib import contextmanager

    @contextmanager
    def _get_session():
        with Session(software_engine) as s:
            yield s

    from infra_brain.api.routers.fleet import fleet_router
    from infra_brain.dashboard_auth import require_session

    app = FastAPI()
    app.include_router(fleet_router)
    # Not an auth test — bypass require_session (see the `client` fixture above
    # for why dependency_overrides, not mock.patch, is required here).
    app.dependency_overrides[require_session] = lambda: None
    with patch("infra_brain.api.routers.fleet.get_session", _get_session):
        yield TestClient(app)


def test_software_aggregated_returns_items_field(software_client):
    """GET /software?view=aggregated must return items (not aggregated) + view + total/limit/offset."""
    resp = software_client.get("/api/dashboard/software?view=aggregated")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Must have items, not aggregated
    assert "items" in body, f"Response must have 'items' key, got: {list(body.keys())}"
    assert "aggregated" not in body, (
        f"'aggregated' field must be removed; found keys: {list(body.keys())}"
    )
    assert "detail" not in body, f"'detail' field must be removed; found keys: {list(body.keys())}"

    # view echoes the requested value
    assert body.get("view") == "aggregated", f"view must echo 'aggregated', got: {body.get('view')}"

    # Standard envelope fields present
    assert "total" in body and isinstance(body["total"], int)
    assert "limit" in body and isinstance(body["limit"], int)
    assert "offset" in body and isinstance(body["offset"], int)

    # Items are populated (3 seeded rows → 3 distinct groups)
    assert isinstance(body["items"], list)
    assert len(body["items"]) >= 1


def test_software_detail_returns_items_field(software_client):
    """GET /software?view=detail must return items (not detail) + view + total/limit/offset."""
    resp = software_client.get("/api/dashboard/software?view=detail")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Must have items, not detail
    assert "items" in body, f"Response must have 'items' key, got: {list(body.keys())}"
    assert "aggregated" not in body, (
        f"'aggregated' field must be removed; found keys: {list(body.keys())}"
    )
    assert "detail" not in body, f"'detail' field must be removed; found keys: {list(body.keys())}"

    # view echoes the requested value
    assert body.get("view") == "detail", f"view must echo 'detail', got: {body.get('view')}"

    # Standard envelope fields present
    assert "total" in body and isinstance(body["total"], int)
    assert "limit" in body and isinstance(body["limit"], int)
    assert "offset" in body and isinstance(body["offset"], int)

    # Items are populated (3 seeded rows)
    assert isinstance(body["items"], list)
    assert len(body["items"]) >= 1


def test_software_default_view_is_aggregated(software_client):
    """GET /software (no view param) defaults to aggregated view."""
    resp = software_client.get("/api/dashboard/software")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("view") == "aggregated"
    assert "items" in body
    assert "aggregated" not in body


def test_software_summary_sidecar_present(software_client):
    """summary sidecar field is present alongside items in both views."""
    for view in ("aggregated", "detail"):
        resp = software_client.get(f"/api/dashboard/software?view={view}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "summary" in body, f"summary sidecar missing in view={view}"
        summary = body["summary"]
        assert "total_records" in summary
        assert "unique_products" in summary
        assert "hosts_covered" in summary


def test_software_items_shape_per_view(software_client):
    """items contain the correct fields for each view (union discrimination check).

    aggregated view: items must carry host_count; must NOT carry hostname/id/r7_asset_id.
    detail view: items must carry hostname, id, r7_asset_id; must NOT carry host_count.
    This directly tests that the SoftwareAggRowOut|SoftwareDetailRowOut union
    produces the right shape per view, not merely that 'items' exists.
    """
    # --- aggregated ---
    resp = software_client.get("/api/dashboard/software?view=aggregated")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    assert len(items) >= 1, "aggregated view must return at least one item"
    for row in items:
        assert "host_count" in row, f"aggregated row missing host_count: {row}"
        assert "hostname" not in row, f"aggregated row must not carry hostname: {row}"
        assert "id" not in row, f"aggregated row must not carry id: {row}"
        assert "r7_asset_id" not in row, f"aggregated row must not carry r7_asset_id: {row}"

    # --- detail ---
    resp = software_client.get("/api/dashboard/software?view=detail")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    assert len(items) >= 1, "detail view must return at least one item"
    for row in items:
        assert "hostname" in row, f"detail row missing hostname: {row}"
        assert "id" in row, f"detail row missing id: {row}"
        assert "r7_asset_id" in row, f"detail row missing r7_asset_id: {row}"
        assert "host_count" not in row, f"detail row must not carry host_count: {row}"
