"""Tests for the read-only dashboard API contract (/api/dashboard/*)."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.config import get_settings
from infra_brain.db.models import (
    AgentActionLog,
    AgentDecisionLog,
    CollectionRun,
    ComplianceViolation,
    DriftEvent,
    EolRegistry,
    GeneratedScript,
    HostIdentity,
    HostPurposeMap,
    Instinct,
    IntegrationProposal,
    InventoryReconcileEvent,
    JiraTicket,
    ProposedAction,
    R7VulnCve,
    R7Vulnerability,
    Resource,
    ResourceOwnership,
    RootCauseNote,
    ScanPoint,
    VulnQueueItem,
)

from tests.support.pg import make_engine


def _real_drift_event_id(session) -> uuid.UUID:
    """A committed DriftEvent id usable as a JiraTicket.drift_event_id FK.

    ``jira_tickets.drift_event_id`` is a real FK into ``drift_events``. SQLite
    does not enforce it, so seeding a bare ``uuid4()`` looked fine; PostgreSQL
    rejects it with ForeignKeyViolation. Seed the parent chain instead.
    """
    res = Resource(domain="linux", type="host", name=f"fk-parent-{uuid.uuid4()}", source="test")
    session.add(res)
    session.flush()
    ev = DriftEvent(resource_id=res.id, drift_type="config", field="pkg")
    session.add(ev)
    session.flush()
    return ev.id


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def client(engine, monkeypatch):
    """Auth-off client (explicit dev-mode) for the full dashboard API surface.

    Item 1.5b (F-027/F-031) made dev-mode explicit: it used to be implied by
    "no UI_COOKIE_SECRET configured", now it requires INFRA_BRAIN_DEV=1.
    """
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

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
    from infra_brain.dashboard_api import hosts_router, router

    app = FastAPI()
    app.include_router(router)
    app.include_router(governance_drift_router)
    app.include_router(governance_audit_router)
    app.include_router(governance_intelligence_router)
    app.include_router(governance_ops_router)
    app.include_router(fleet_router)
    app.include_router(resources_router)
    app.include_router(hosts_router)
    app.include_router(vuln_router)
    app.include_router(ui_router)
    with (
        patch("infra_brain.dashboard_api.get_session", _get_session),
        patch("infra_brain.api.routers.governance_drift.get_session", _get_session),
        patch("infra_brain.api.routers.governance_audit.get_session", _get_session),
        patch("infra_brain.api.routers.governance_intelligence.get_session", _get_session),
        patch("infra_brain.api.routers.governance_ops.get_session", _get_session),
        patch("infra_brain.api.routers.fleet.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
        patch("infra_brain.api.routers.vuln.get_session", _get_session),
        patch("infra_brain.api.routers.ui.get_session", _get_session),
    ):
        yield TestClient(app)


def _seed(engine):
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(
            domain="linux",
            type="host",
            name="prod-web-01",
            source="LinuxAgent",
            zone="corpor",
            metadata_={"distro": "Ubuntu 22.04", "packages": 412},
        )
        s.add(r)
        s.flush()
        s.add(
            DriftEvent(
                resource_id=r.id,
                drift_type="config_drift",
                field="kernel",
                old_value={"v": "old"},
                new_value={"v": "new"},
                status="open",
            )
        )
        s.add(
            VulnQueueItem(
                resource_id=r.id,
                cve_id="CVE-2026-1",
                severity="critical",
                sla_due=now - timedelta(days=1),
                status="open",
            )
        )
        s.add(
            EolRegistry(
                resource_id=r.id,
                asset_name="Ubuntu 18.04",
                eol_date=now - timedelta(days=30),
                pci_risk_score=8,
                migration_path="→ 22.04",
            )
        )
        s.add(
            InventoryReconcileEvent(
                host="prod-web-09",
                domain="linux",
                target_group="discovered",
                status="proposed",
                mr_url="http://gitlab/mr/9",
            )
        )
        s.commit()


def test_resources_endpoint(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/resources")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data and "total" in data
    assert data["total"] == 1
    assert len(data["items"]) == 1
    row = data["items"][0]
    assert row["hostname"] == "prod-web-01"
    assert row["drift_count"] == 1
    assert row["status"] == "eol"  # has an eol_registry row
    assert any(kv["k"] == "distro" for kv in row["meta"])


def test_resources_endpoint_homelab_services_domain_filter_and_category_meta(client, engine):
    """Dashboard-side regression for the T5 Homelab Services page
    (dashboard-app/src/pages/Homelab.tsx). That page groups
    Resource(domain="homelab_services") rows by the `category` field HomelabServicesAgent
    writes into metadata_ (agents/homelab_services.py) alongside url/status/http_status —
    the same fields the get_homelab_service_category MCP tool (mcp_server.py) already
    surfaces for chat. No new backend route was added for the dashboard: this pins down
    that GET /api/dashboard/resources?domain=homelab_services already (a) filters to just
    that domain and (b) flattens category/url/status/http_status into each row's `meta`
    KV list via list_resources' existing metadata_-flattening (api/routers/hosts.py) —
    exactly the shape Homelab.tsx's `toServiceRow` expects. If this ever drifts (e.g. a
    metadata_ key rename in the collector), this test — not just the frontend build —
    should catch it.
    """
    with Session(engine) as s:
        s.add(
            Resource(
                domain="homelab_services",
                type="homelab_service",
                name="sonarr",
                source="HomelabServicesAgent",
                zone="ai_node",
                metadata_={
                    "category": "media-management",
                    "url": "http://127.0.0.1:8989",
                    "status": "up",
                    "http_status": 200,
                },
            )
        )
        s.add(
            Resource(
                domain="linux",
                type="host",
                name="other-domain-host",
                source="LinuxAgent",
            )
        )
        s.commit()

    resp = client.get("/api/dashboard/resources?domain=homelab_services")
    assert resp.status_code == 200
    data = resp.json()
    # Domain filter excludes the linux-domain row seeded above.
    assert data["total"] == 1
    assert len(data["items"]) == 1
    row = data["items"][0]
    assert row["hostname"] == "sonarr"
    assert row["domain"] == "homelab_services"
    meta = {kv["k"]: kv["v"] for kv in row["meta"]}
    assert meta["category"] == "media-management"
    assert meta["url"] == "http://127.0.0.1:8989"
    assert meta["status"] == "up"
    assert meta["http_status"] == "200"


def test_drift_events_join(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/drift_events").json()
    data = resp["items"]
    assert len(data) == 1
    assert data[0]["hostname"] == "prod-web-01"
    assert data[0]["field_name"] == "kernel"
    assert data[0]["drift_type"] == "config_drift"


def test_drift_events_limit_and_offset_are_clamped(client, engine):
    """FIX: an oversized ?limit is clamped to the 500 ceiling and a negative
    ?offset to 0 before hitting the DB — matches the sibling handlers
    (list_notifications in this same file, fleet.py, hosts.py) so a caller
    cannot force unbounded row materialization into Python.
    """
    _seed(engine)
    clamped = client.get("/api/dashboard/drift_events?limit=100000&offset=-5").json()
    assert clamped["limit"] == 500
    assert clamped["offset"] == 0
    assert len(clamped["items"]) <= 500


def test_drift_events_jira_and_notes_lookup_is_bounded_to_the_page(client, engine):
    """M-4: list_drift used to fetch EVERY JiraTicket and EVERY RootCauseNote
    in the whole table (``s.query(JiraTicket).all()`` /
    ``s.query(RootCauseNote).all()``) just to build a dict keyed by
    drift_event_id, even though only the current page's rows (<= ``limit``)
    can ever be looked up in it. Real-behavior check (not the shape of the
    patch): capture the actual SQL sent to the DB and assert the
    jira_tickets/root_cause_notes lookups carry a WHERE clause scoping them
    to the page's drift_event_ids, instead of an unfiltered full-table scan.
    """
    from sqlalchemy import event

    now = datetime.now(UTC)
    with Session(engine) as s:
        # Seed far more drift events (each with a jira ticket + root cause
        # note) than the page size requested below, so an unfiltered
        # full-table fetch is observably different from a bounded one.
        for i in range(10):
            r = Resource(domain="linux", type="host", name=f"host-{i}", source="LinuxAgent")
            s.add(r)
            s.flush()
            de = DriftEvent(
                resource_id=r.id,
                drift_type="config_drift",
                field="kernel",
                old_value={"v": "old"},
                new_value={"v": "new"},
                status="open",
                detected_at=now - timedelta(minutes=i),
            )
            s.add(de)
            s.flush()
            s.add(JiraTicket(drift_event_id=de.id, jira_key=f"OPS-{i}"))
            s.add(RootCauseNote(drift_event_id=de.id, explanation=f"note-{i}"))
        s.commit()

    captured_sql: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured_sql.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        resp = client.get("/api/dashboard/drift_events?limit=2")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2

    def _selects_against(table: str) -> list[str]:
        return [
            sql
            for sql in captured_sql
            if sql.strip().lower().startswith("select") and table in sql.lower()
        ]

    jira_selects = _selects_against("jira_tickets")
    notes_selects = _selects_against("root_cause_notes")
    assert jira_selects, "expected list_drift to query jira_tickets at all"
    assert notes_selects, "expected list_drift to query root_cause_notes at all"
    assert any("where" in sql.lower() for sql in jira_selects), (
        "list_drift must scope the jira_tickets lookup to the current page's "
        "drift_event_ids (a WHERE clause), not fetch the whole table"
    )
    assert any("where" in sql.lower() for sql in notes_selects), (
        "list_drift must scope the root_cause_notes lookup to the current page's "
        "drift_event_ids (a WHERE clause), not fetch the whole table"
    )


def test_vulnerabilities_sla(client, engine):
    _seed(engine)
    data = client.get("/api/dashboard/vulnerabilities").json()
    assert "items" in data
    assert data["items"][0]["cve"] == "CVE-2026-1"
    assert "overdue" in data["items"][0]["sla"]


def test_vulnerabilities_with_r7_cvss_join(client, engine):
    """Exercise list_vulns + _best_vuln_by_cve through a real R7VulnCve bridge.

    Regression guard for the production 500:

        column r7_vulnerabilities.resource_id does not exist

    The bug only fires when a matching r7_vuln_cves bridge row exists, so
    _best_vuln_by_cve actually runs ``s.query(R7Vulnerability).filter(...)`` and
    SQLAlchemy SELECTs every mapped column — including ``resource_id``. The base
    test_vulnerabilities_sla seeds no bridge row, so it short-circuits before the
    R7Vulnerability query and never touched this path. With the column present in
    the schema the join resolves and the endpoint must return 200 with the
    enriched CVSS / exploit / pci_fail fields populated from the R7 vuln.
    """
    _seed(engine)
    with Session(engine) as s:
        # Bridge the queued CVE to a Rapid7 vuln slug, then seed the rich vuln.
        s.add(R7VulnCve(r7_vuln_id="ubuntu-cve-2026-1", cve_id="CVE-2026-1"))
        s.add(
            R7Vulnerability(
                r7_vuln_id="ubuntu-cve-2026-1",
                title="Test critical kernel CVE",
                severity="critical",
                cvss_v3_score=9.8,
                exploits=2,
                pci_fail=True,
            )
        )
        s.commit()

    resp = client.get("/api/dashboard/vulnerabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data and "total" in data
    assert data["total"] == 1
    row = data["items"][0]
    assert row["cve"] == "CVE-2026-1"
    # Enrichment resolved through the r7_vuln_cves bridge -> R7Vulnerability.
    assert row["cvss"] == pytest.approx(9.8)
    assert row["exploits"] == 2
    assert row["pci_fail"] is True


def test_vulnerabilities_has_exploit_and_pci_filters(client, engine):
    """has_exploit / pci_only filters operate on the R7-enriched values, and
    `total` must always equal `len(items)` -- these filters are resolved
    per-CVE (via _best_vuln_by_cve), not at the SQL layer, so a second,
    non-matching vuln row is required here to catch a regression where `total`
    is computed before the enrichment filter is applied (it silently was,
    for a while: total counted every SQL-matching row while items only
    counted the ones surviving the filter on a single SQL-level page)."""
    _seed(engine)
    with Session(engine) as s:
        s.add(R7VulnCve(r7_vuln_id="ubuntu-cve-2026-1", cve_id="CVE-2026-1"))
        s.add(
            R7Vulnerability(
                r7_vuln_id="ubuntu-cve-2026-1",
                severity="critical",
                cvss_v3_score=9.8,
                exploits=3,
                pci_fail=True,
            )
        )
        # Second vuln queue row with no backing R7Vulnerability -- resolves to
        # exploits=0/pci_fail=False, so it must be excluded by both filters.
        s.add(
            VulnQueueItem(
                resource_id=(s.query(Resource).one()).id,
                cve_id="CVE-2026-2",
                severity="medium",
                sla_due=None,
                status="open",
            )
        )
        s.commit()

    unfiltered = client.get("/api/dashboard/vulnerabilities").json()
    assert unfiltered["total"] == 2
    assert len(unfiltered["items"]) == 2

    exploit_filtered = client.get("/api/dashboard/vulnerabilities?has_exploit=true").json()
    assert len(exploit_filtered["items"]) == 1
    assert exploit_filtered["total"] == 1, "total must match items after has_exploit filtering"

    pci_filtered = client.get("/api/dashboard/vulnerabilities?pci_only=true").json()
    assert len(pci_filtered["items"]) == 1
    assert pci_filtered["total"] == 1, "total must match items after pci_only filtering"


def test_vulnerabilities_candidate_fetch_is_capped(client, engine, monkeypatch):
    """M-4: list_vulns' pre-enrichment candidate fetch
    (``q.order_by(...).all()``) used to have NO upper bound -- has_exploit/
    pci_only filtering happens in Python, so an unfiltered request against a
    large host x CVE vuln_queue could materialize the ENTIRE table into one
    request. ``_VULN_CANDIDATE_CEILING`` caps that fetch; this pins the cap
    is actually applied (monkeypatched small so the test doesn't need to
    seed tens of thousands of rows to observe it)."""
    from infra_brain.api.routers import vuln as vuln_router_mod

    monkeypatch.setattr(vuln_router_mod, "_VULN_CANDIDATE_CEILING", 3)

    with Session(engine) as s:
        resource = Resource(domain="linux", type="host", name="capped-host", source="LinuxAgent")
        s.add(resource)
        s.flush()
        for i in range(5):
            s.add(
                VulnQueueItem(
                    resource_id=resource.id,
                    cve_id=f"CVE-2026-{100 + i}",
                    severity="high",
                    sla_due=None,
                    status="open",
                )
            )
        s.commit()

    data = client.get("/api/dashboard/vulnerabilities").json()
    assert data["total"] == 3, (
        f"expected the candidate fetch capped at 3, got total={data['total']} -- "
        "the pre-enrichment query must not be unbounded"
    )
    assert len(data["items"]) == 3


def test_eol_overdue(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/eol").json()
    data = resp["items"]
    assert data[0]["status"] == "overdue"
    assert data[0]["pci_risk_score"] == 8


def test_eol_proximity_bands(client, engine):
    """Bug 1 (2026-07-24 UX audit, T6): "approaching" must be a real proximity
    band, not a binary "anything non-overdue" flag. An EOL a decade out is
    "tracked", one within the 90-day window is "approaching", and a NULL
    eol_date is "unknown" — none of these should collapse into "approaching".
    """
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="eol-band-host", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(
            EolRegistry(
                resource_id=r.id,
                asset_name="near-term-asset",
                eol_date=now + timedelta(days=30),
                pci_risk_score=6,
            )
        )
        s.add(
            EolRegistry(
                resource_id=r.id,
                asset_name="decade-out-asset",
                eol_date=now + timedelta(days=3650),
                pci_risk_score=2,
            )
        )
        s.add(
            EolRegistry(
                resource_id=r.id,
                asset_name="undated-asset",
                eol_date=None,
                pci_risk_score=None,
            )
        )
        s.commit()

    data = client.get("/api/dashboard/eol").json()["items"]
    by_asset = {row["asset"]: row for row in data}

    assert by_asset["near-term-asset"]["status"] == "approaching"
    assert by_asset["decade-out-asset"]["status"] == "tracked"
    assert by_asset["undated-asset"]["status"] == "unknown"
    # NULL pci_risk_score must round-trip as null, not be coerced to 0 — the
    # frontend needs this to exclude unscored assets from the risk average.
    assert by_asset["undated-asset"]["pci_risk_score"] is None


def test_eol_limit_and_offset_are_clamped(client, engine):
    """Unlike every sibling paged endpoint (list_hosts, get_host_vulns,
    /notifications — see test_notifications_total_is_true_count_and_limit_is_clamped),
    /eol used to pass limit/offset straight to SQL with no clamping at all. A
    negative ?limit or ?offset reaches PostgreSQL as a negative LIMIT/OFFSET
    literal, which it rejects at execution time — an unhandled 500 instead of
    the clean degraded 200 every paged endpoint's docstring promises. The fix
    clamps both, echoed back in the envelope."""
    resp = client.get("/api/dashboard/eol?limit=-1&offset=-5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 1
    assert data["offset"] == 0

    oversized = client.get("/api/dashboard/eol?limit=100000").json()
    assert oversized["limit"] == 500

    zero = client.get("/api/dashboard/eol?limit=0").json()
    assert zero["limit"] == 1


def test_resources_limit_and_offset_are_clamped(client, engine):
    """list_resources previously clamped only the upper bound
    (``min(limit, 1000)``), leaving a negative ?limit or ?offset to reach
    PostgreSQL unclamped and raise an unhandled 500."""
    resp = client.get("/api/dashboard/resources?limit=-1&offset=-5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 1
    assert data["offset"] == 0

    oversized = client.get("/api/dashboard/resources?limit=100000").json()
    assert oversized["limit"] == 1000


def test_resource_snapshots_limit_and_offset_are_clamped(client, engine):
    """resource_snapshots passed limit/offset straight into paginate() with no
    clamping at all, so a negative ?limit/?offset reached PostgreSQL
    unclamped and raised an unhandled 500."""
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="snap-host", source="LinuxAgent")
        s.add(r)
        s.commit()
        resource_id = str(r.id)

    resp = client.get(f"/api/dashboard/resources/{resource_id}/snapshots?limit=-1&offset=-5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 1
    assert data["offset"] == 0

    oversized = client.get(
        f"/api/dashboard/resources/{resource_id}/snapshots?limit=100000"
    ).json()
    assert oversized["limit"] == 500


def test_inventory_reconcile_endpoint(client, engine):
    _seed(engine)
    resp = client.get("/api/dashboard/inventory_reconcile").json()
    data = resp["items"]
    assert len(data) == 1
    assert data[0]["host"] == "prod-web-09"
    assert data[0]["target_group"] == "discovered"
    assert data[0]["mr_url"] == "http://gitlab/mr/9"


def test_settings_masks_secrets(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-supersecret-1234")
    get_settings.cache_clear()
    try:
        resp = client.get("/api/dashboard/settings").json()
    finally:
        get_settings.cache_clear()
    groups = resp["items"]
    rows = [r for g in groups for r in g["rows"]]
    secret = next(r for r in rows if r["k"] == "ANTHROPIC_API_KEY")
    assert secret["type"] == "secret"
    assert "sk-supersecret" not in (secret["v"] or "")
    assert secret["v"].endswith("1234")  # masked but shows tail


def test_settings_never_serves_dsn_credentials(client, monkeypatch):
    """H-1: postgres_url / postgres_readonly_url / redis_url are plain ``str``
    carrying ``user:password@host`` DSNs and match NONE of ``_SECRET_HINTS``,
    so they used to render as cleartext type-"text" rows to any logged-in
    session. The password must not appear anywhere in the response body."""
    dsn_password = "hunter2SuperSecret"  # noqa: S105 — test fixture
    pg_dsn = f"postgresql+psycopg://ibuser:{dsn_password}@db.internal:5432/infra_brain"
    redis_dsn = f"redis://:{dsn_password}@redis.internal:6379/0"

    monkeypatch.setenv("POSTGRES_URL", pg_dsn)
    monkeypatch.setenv("POSTGRES_READONLY_URL", pg_dsn)
    monkeypatch.setenv("REDIS_URL", redis_dsn)
    get_settings.cache_clear()
    try:
        resp = client.get("/api/dashboard/settings")
        assert resp.status_code == 200
        body = resp.text
        payload = resp.json()
    finally:
        get_settings.cache_clear()

    assert dsn_password not in body
    assert "ibuser" not in body

    rows = [r for g in payload["items"] for r in g["rows"]]
    pg = next(r for r in rows if r["k"] == "POSTGRES_URL")
    assert dsn_password not in (pg["v"] or "")
    # ...but the row must stay useful: which host/db is configured survives.
    assert "db.internal:5432/infra_brain" in (pg["v"] or "")


def test_settings_exposes_integration_confidence_gate(client):
    """TRK-182 follow-up: the confidence-gate threshold is a real Settings
    field, so it must show up in the /settings rows exactly like any other
    tunable, not be silently excluded."""
    resp = client.get("/api/dashboard/settings").json()
    groups = resp["items"]
    rows = [r for g in groups for r in g["rows"]]
    row = next(r for r in rows if r["k"] == "INTEGRATION_CONFIDENCE_GATE")
    assert row["type"] == "text"
    assert row["v"] == "0.7"


def test_ui_settings_allowlist_entries_are_real_settings_fields():
    """TRK-321: the narrow subset is keyed by an explicit allowlist of Settings
    field names. If a field is renamed or removed, the allowlist entry goes
    stale and the endpoint silently stops serving it — catch that here rather
    than in a blank UI."""
    from infra_brain.api.routers.governance_ops import _UI_SETTINGS_ALLOWLIST
    from infra_brain.config import Settings

    unknown = _UI_SETTINGS_ALLOWLIST - set(Settings.model_fields)
    assert not unknown, f"allowlisted keys are not Settings fields: {sorted(unknown)}"


def test_ui_settings_allowlist_contains_nothing_secret_shaped():
    """TRK-321: the subset must stay an ALLOWLIST of non-sensitive keys, not
    'everything except things that look secret'. This pins that no entry is
    secret-hinted, so the route's defensive secret-row drop stays unreachable
    rather than becoming the thing actually holding the line."""
    from infra_brain.api._helpers import _SECRET_HINTS
    from infra_brain.api.routers.governance_ops import _UI_SETTINGS_ALLOWLIST

    for key in _UI_SETTINGS_ALLOWLIST:
        assert not any(h in key.lower() for h in _SECRET_HINTS), key


def test_ui_settings_subset_is_narrow(client):
    """TRK-321: the subset endpoint serves only allowlisted keys — not the full
    ``model_dump()``. Compare against the admin view to prove it is a strict,
    much smaller subset rather than the same dump behind a different path."""
    from infra_brain.api.routers.governance_ops import _UI_SETTINGS_ALLOWLIST

    narrow = client.get("/api/dashboard/settings/ui")
    assert narrow.status_code == 200, narrow.text
    narrow_keys = {r["k"] for r in narrow.json()["items"]}

    full = client.get("/api/dashboard/settings").json()
    full_keys = {r["k"] for g in full["items"] for r in g["rows"]}

    assert narrow_keys == {k.upper() for k in _UI_SETTINGS_ALLOWLIST}
    assert narrow_keys < full_keys
    assert len(narrow_keys) < len(full_keys) / 10


def test_agents_roster(client, engine):
    resp = client.get("/api/dashboard/agents").json()
    data = resp["items"]
    names = {a["name"] for a in data}
    assert "LinuxAgent" in names
    assert "InventoryReconcileAgent" in names
    disc = next(a for a in data if a["name"] == "DiscoveryAgent")
    assert disc["kind"] == "llm"


def test_agents_roster_maps_partial_and_skipped_statuses(client, engine):
    """T8: a `partial` run (detail-write failure — see etl/base.py's R3 status
    mapping) or a `skipped` run (CollectorSkipped — dependency unconfigured)
    must NOT fall through to the same "idle" default as a never-run agent.
    Regression guard for the bug where half a fleet landing `partial` runs
    showed grey "idle" dots and Degraded: 0 with no visible signal."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(
            CollectionRun(
                domain="linux",
                trigger_type="manual",
                status="partial",
                started_at=now,
                finished_at=now,
                resources_found=3,
                detail_rows_written=1,
            )
        )
        s.add(
            CollectionRun(
                domain="netdiscovery",
                trigger_type="manual",
                status="skipped",
                started_at=now,
                finished_at=now,
                error_message="unconfigured",
            )
        )
        s.commit()

    resp = client.get("/api/dashboard/agents").json()
    data = resp["items"]
    linux = next(a for a in data if a["domain"] == "linux")
    netdiscovery = next(a for a in data if a["domain"] == "netdiscovery")

    assert linux["status"] == "partial"
    assert linux["status"] not in ("idle", "healthy")

    assert netdiscovery["status"] == "skipped"
    assert netdiscovery["status"] not in ("idle", "healthy", "degraded")


def test_chat_streams_tokens_and_thread_id(client):
    """/chat is an SSE stream: a meta frame carrying thread_id, then token frames.

    Patches ``stream_chat_events`` (the single chat-lane driver) rather than the
    older ``stream_response``: the route now consumes typed events so it can
    also emit ``tool``/``provenance`` frames. ``stream_response`` still exists
    as the token-only view of the same driver — pinned separately by
    ``tests/test_chat_agent.py::test_stream_response_is_token_view_of_events``.
    """

    async def fake_events(graph, thread_id, message):
        for tok in ["42 ", "linux ", "hosts."]:
            yield {"type": "token", "text": tok}

    async def fake_build_chat_agent():
        return object()

    with (
        patch("infra_brain.chat.agent.build_chat_agent", fake_build_chat_agent),
        patch("infra_brain.chat.agent.stream_chat_events", fake_events),
    ):
        resp = client.post(
            "/api/dashboard/chat",
            json={"message": "how many linux hosts?", "thread_id": "t-1"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert '"thread_id": "t-1"' in body  # meta frame echoes the thread
    assert '"token": "42 "' in body
    assert '"token": "hosts."' in body
    assert "event: done" in body


def test_chat_streams_tool_and_provenance_frames(client):
    """Tool calls and knowledge-graph citations reach the browser as their own
    SSE frames, so an operator can tell an answer from a guess."""

    async def fake_events(graph, thread_id, message):
        yield {"type": "tool", "name": "query_graph_neighborhood", "args": {"resource_id": "node_a"}}
        yield {
            "type": "provenance",
            "tool": "query_graph_neighborhood",
            "provenance": {
                "found": True,
                "root": {"name": "node_a", "type": "host"},
                "nodes": [{"name": "litellm", "type": "service", "source": "homelab"}],
                "edges": [
                    {
                        "from": "litellm",
                        "to": "node_a",
                        "edge_type": "RUNS_ON",
                        "method": "compose_file",
                        "confidence": 0.95,
                        "source": "homelab",
                    }
                ],
                "node_total": 2,
                "edge_total": 1,
                "truncated": False,
            },
        }
        yield {"type": "token", "text": "litellm runs on node_a."}

    async def fake_build_chat_agent():
        return object()

    with (
        patch("infra_brain.chat.agent.build_chat_agent", fake_build_chat_agent),
        patch("infra_brain.chat.agent.stream_chat_events", fake_events),
    ):
        resp = client.post("/api/dashboard/chat", json={"message": "what runs on node_a?"})
    body = resp.text
    assert "event: tool" in body
    assert "event: provenance" in body
    assert "RUNS_ON" in body
    assert '"token": "litellm runs on node_a."' in body


def test_chat_rejects_empty_message(client):
    resp = client.post("/api/dashboard/chat", json={"message": "   "})
    assert resp.status_code == 422


def test_chat_rejects_oversized_message(client):
    """Bounded input: an oversized paste is refused at the schema layer (422),
    before it can be sent to — and billed by — the provider."""
    from infra_brain.chat.limits import CHAT_MAX_MESSAGE_CHARS

    resp = client.post(
        "/api/dashboard/chat", json={"message": "x" * (CHAT_MAX_MESSAGE_CHARS + 1)}
    )
    assert resp.status_code == 422


def test_chat_refuses_thread_past_turn_limit(client):
    """Bounded conversation: the checkpointer replays the whole thread into
    every turn, so a thread past its turn cap is refused with a renderable
    error rather than silently truncated."""
    from infra_brain.chat.limits import CHAT_MAX_TURNS

    async def fake_build_chat_agent():
        return object()

    async def fake_count(graph, thread_id):
        return CHAT_MAX_TURNS

    with (
        patch("infra_brain.chat.agent.build_chat_agent", fake_build_chat_agent),
        patch("infra_brain.chat.agent.count_user_turns", fake_count),
    ):
        resp = client.post("/api/dashboard/chat", json={"message": "one more question"})
    body = resp.text
    assert "event: error" in body
    assert "conversation_too_long" in body
    assert "event: done" in body


def test_chat_unreachable_model_returns_structured_error(client):
    """An unreachable/misconfigured model endpoint must render as
    'the model isn't reachable — here's what to check', not a 500 or a
    silent empty reply."""

    async def fake_build_chat_agent():
        raise ConnectionError("[Errno 111] Connection refused")

    with patch("infra_brain.chat.agent.build_chat_agent", fake_build_chat_agent):
        resp = client.post("/api/dashboard/chat", json={"message": "hello"})
    assert resp.status_code == 200
    body = resp.text
    assert "event: error" in body
    assert "llm_unreachable" in body
    assert "hints" in body
    # The classified copy, not the exception's own text.
    assert "Errno 111" not in body
    assert "event: done" in body


def test_chat_stream_failure_does_not_leak_raw_exception_text(client):
    """N-3: a chat-graph failure must not leak raw exception text (which can
    carry internal detail) to the client — only a generic error token, with
    the real exception logged server-side via logger.exception."""

    async def fake_build_chat_agent():
        raise RuntimeError("super-secret-internal-detail")

    with patch("infra_brain.chat.agent.build_chat_agent", fake_build_chat_agent):
        resp = client.post(
            "/api/dashboard/chat",
            json={"message": "hello", "thread_id": "t-err"},
        )
    assert resp.status_code == 200
    assert "super-secret-internal-detail" not in resp.text
    assert "error" in resp.text.lower()
    assert "event: done" in resp.text


def test_sweep_wrapper_queues_known_domain(client):
    """POST /sweeps/{domain} acquires the lock and queues a background dispatch."""
    with (
        patch("infra_brain.webhooks.KNOWN_DOMAINS", {"linux"}),
        patch("infra_brain.dedup.try_acquire", return_value="tok") as acq,
        patch("infra_brain.webhooks._dispatch_bg") as disp,
    ):
        resp = client.post("/api/dashboard/sweeps/linux")
    assert resp.status_code == 202
    assert resp.json() == {"accepted": True, "domain": "linux"}
    acq.assert_called_once()
    disp.assert_called_once()


def test_sweep_wrapper_rejects_unknown_domain(client):
    with patch("infra_brain.webhooks.KNOWN_DOMAINS", {"linux"}):
        resp = client.post("/api/dashboard/sweeps/bogus")
    assert resp.status_code == 404


def test_sweep_wrapper_conflict_when_locked(client):
    with (
        patch("infra_brain.webhooks.KNOWN_DOMAINS", {"linux"}),
        patch("infra_brain.dedup.try_acquire", return_value=None),
    ):
        resp = client.post("/api/dashboard/sweeps/linux")
    assert resp.status_code == 409


def _seed_action(engine, *, status="pending", confidence=0.9):
    with Session(engine) as s:
        a = ProposedAction(
            agent="remediation",
            action_type="restart_service",
            target="prod-web-01:nginx",
            confidence=confidence,
            status=status,
        )
        s.add(a)
        s.commit()
        return str(a.id)


def test_approve_action_session_route(client, engine):
    """Session-gated approve flips a pending, high-confidence action to approved."""
    aid = _seed_action(engine, status="pending", confidence=0.9)
    resp = client.post(f"/api/dashboard/actions/{aid}/approve", json={})
    assert resp.status_code == 200
    assert resp.json() == {"approved": True, "action_id": aid}
    with Session(engine) as s:
        import uuid as _uuid

        row = s.get(ProposedAction, _uuid.UUID(aid))
        assert row.status == "approved"
        assert row.approved_by  # populated (dev user or "dashboard")


def test_approve_action_rejects_low_confidence(client, engine):
    aid = _seed_action(engine, status="pending", confidence=0.5)
    resp = client.post(f"/api/dashboard/actions/{aid}/approve", json={})
    assert resp.status_code == 422


def test_approve_action_conflict_when_not_pending(client, engine):
    aid = _seed_action(engine, status="approved", confidence=0.9)
    resp = client.post(f"/api/dashboard/actions/{aid}/approve", json={})
    assert resp.status_code == 409


def test_reject_action_session_route(client, engine):
    aid = _seed_action(engine, status="pending", confidence=0.4)
    resp = client.post(f"/api/dashboard/actions/{aid}/reject")
    assert resp.status_code == 200
    assert resp.json() == {"rejected": True, "action_id": aid}
    with Session(engine) as s:
        import uuid as _uuid

        row = s.get(ProposedAction, _uuid.UUID(aid))
        assert row.status == "rejected"


def test_reject_action_conflict_when_not_pending(client, engine):
    """MEDIUM-1: rejecting an already-executed row would corrupt the audit
    record of the external-write agent — must 409, row left unchanged."""
    aid = _seed_action(engine, status="executed", confidence=0.9)
    resp = client.post(f"/api/dashboard/actions/{aid}/reject")
    assert resp.status_code == 409
    with Session(engine) as s:
        import uuid as _uuid

        row = s.get(ProposedAction, _uuid.UUID(aid))
        assert row.status == "executed"


def test_approve_action_not_found(client):
    import uuid as _uuid

    resp = client.post(f"/api/dashboard/actions/{_uuid.uuid4()}/approve", json={})
    assert resp.status_code == 404


def test_system_health(client):
    resp = client.get("/api/dashboard/system_health").json()
    data = resp["items"]
    names = {h["name"] for h in data}
    assert "PostgreSQL" in names


def test_counts_endpoint(client, engine):
    _seed(engine)
    data = client.get("/api/dashboard/counts").json()
    assert "open_drift" in data and "open_cves" in data
    assert "eol_overdue" in data and "invrec_proposed" in data
    assert "total_resources" in data
    # _seed adds 1 open drift event, 1 open cve, 1 eol (overdue), 1 invrec (proposed), 1 resource
    assert data["open_drift"] == 1
    assert data["open_cves"] == 1
    assert data["eol_overdue"] == 1
    assert data["invrec_proposed"] == 1
    assert data["total_resources"] == 1


# ─── Phase 1 parity: filters & fields added to reach Streamlit parity ──────────


def _seed_parity(engine):
    """Seed the rows the parity-gap tests below depend on."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="p1", source="LinuxAgent", zone="corpor")
        s.add(r)
        s.flush()
        # Two drift events, one fresh, one old → exercises the `hours` window.
        s.add(
            DriftEvent(
                resource_id=r.id,
                drift_type="config_drift",
                field="recent",
                old_value={"v": "a"},
                new_value={"v": "b"},
                status="open",
                detected_at=now - timedelta(hours=2),
            )
        )
        s.add(
            DriftEvent(
                resource_id=r.id,
                drift_type="config_drift",
                field="stale",
                old_value={"v": "a"},
                new_value={"v": "b"},
                status="open",
                detected_at=now - timedelta(hours=100),
            )
        )
        # Instincts in two zones, one above and one below the 0.7 applied threshold.
        s.add(
            Instinct(
                domain="linux",
                zone="corpor",
                pattern="high-conf",
                confidence=0.9,
                promoted_by="DriftLearningAgent",
                citation="run-123",
            )
        )
        s.add(
            Instinct(
                domain="linux",
                zone="in-zone",
                pattern="low-conf",
                confidence=0.4,
                promoted_by="DriftLearningAgent",
            )
        )
        # Decisions under two run_ids.
        import uuid as _uuid

        rid_a = _uuid.uuid4()
        s.add(AgentDecisionLog(run_id=rid_a, agent="linux", decision_summary="A"))
        s.add(AgentDecisionLog(run_id=_uuid.uuid4(), agent="linux", decision_summary="B"))
        # Scan point + collection runs (one completed, one failed) for the same domain.
        s.add(ScanPoint(domain="linux", method="ansible", endpoint="/inv", schedule="0 */6 * * *"))
        s.add(
            CollectionRun(
                domain="linux",
                trigger_type="cron",
                status="completed",
                started_at=now - timedelta(hours=1),
            )
        )
        s.add(
            CollectionRun(
                domain="linux",
                trigger_type="cron",
                status="failed",
                started_at=now - timedelta(minutes=10),
            )
        )
        # Generated script with git/domain/created_at metadata.
        s.add(
            GeneratedScript(
                name="rotate.sh",
                language="bash",
                purpose="rotate logs",
                content="echo hi",
                content_sha256="deadbeef",
                created_by_agent="linux",
                domain="linux",
                git_path="scripts/rotate.sh",
            )
        )
        # Action log row with a status.
        s.add(
            AgentActionLog(
                agent="linux", domain="linux", tool="ssh_run", verdict="allow", status="ok"
            )
        )
        s.commit()
        return str(rid_a)


def test_drift_events_hours_filter(client, engine):
    _seed_parity(engine)
    all_rows = client.get("/api/dashboard/drift_events").json()["items"]
    assert {r["field_name"] for r in all_rows} == {"recent", "stale"}
    windowed = client.get("/api/dashboard/drift_events", params={"hours": 24}).json()["items"]
    assert {r["field_name"] for r in windowed} == {"recent"}


def test_instincts_zone_filter_and_fields(client, engine):
    _seed_parity(engine)
    corpor = client.get("/api/dashboard/instincts", params={"zone": "corpor"}).json()["items"]
    assert len(corpor) == 1
    row = corpor[0]
    assert row["promoted_by"] == "DriftLearningAgent"
    assert row["citation"] == "run-123"
    assert row["applied"] is True  # confidence 0.9 >= 0.7
    low = client.get("/api/dashboard/instincts", params={"zone": "in-zone"}).json()["items"]
    assert low[0]["applied"] is False


def test_instincts_applied_uses_configurable_confidence_gate(client, engine, monkeypatch):
    """TRK-182 follow-up: list_instincts()'s per-row `applied` flag must track
    the same Settings.integration_confidence_gate fleet.py's get_counts() uses
    for the applied_instincts badge, not a hardcoded 0.7 — otherwise the two
    dashboard surfaces (Instincts page vs. home-page count) disagree the
    moment an operator changes the gate."""
    _seed_parity(engine)
    # Default gate (0.70): confidence 0.4 is not applied.
    low = client.get("/api/dashboard/instincts", params={"zone": "in-zone"}).json()["items"]
    assert low[0]["confidence"] == pytest.approx(0.4)
    assert low[0]["applied"] is False

    # Lower the gate below 0.4 — the same row must now flip to applied,
    # matching what fleet.py's applied_instincts count would do.
    monkeypatch.setenv("INTEGRATION_CONFIDENCE_GATE", "0.3")
    get_settings.cache_clear()
    try:
        low_after = client.get("/api/dashboard/instincts", params={"zone": "in-zone"}).json()[
            "items"
        ]
        assert low_after[0]["applied"] is True
    finally:
        get_settings.cache_clear()


def test_decisions_run_id_filter(client, engine):
    rid_a = _seed_parity(engine)
    everything = client.get("/api/dashboard/decisions").json()["items"]
    assert len(everything) == 2
    filtered = client.get("/api/dashboard/decisions", params={"run_id": rid_a}).json()["items"]
    assert len(filtered) == 1
    assert filtered[0]["decision_summary"] == "A"


def test_scan_points_last_run_and_success(client, engine):
    _seed_parity(engine)
    rows = client.get("/api/dashboard/scan_points").json()["items"]
    sp = next(r for r in rows if r["domain"] == "linux")
    assert sp["last_run"] is not None
    assert sp["last_success"] is not None
    # last_run reflects the most recent (failed) run; last_success the completed one.
    assert sp["last_run"] >= sp["last_success"]


def test_generated_scripts_metadata(client, engine):
    _seed_parity(engine)
    rows = client.get("/api/dashboard/generated_scripts").json()["items"]
    g = rows[0]
    assert g["git_path"] == "scripts/rotate.sh"
    assert g["domain"] == "linux"
    assert g["created_at"] is not None


def test_activity_status_field(client, engine):
    _seed_parity(engine)
    rows = client.get("/api/dashboard/activity").json()["items"]
    assert rows[0]["status"] == "ok"
    assert rows[0]["domain"] == "linux"


def test_version_endpoint(client):
    resp = client.get("/api/dashboard/version")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert "environment" in data
    assert data["version"]  # non-empty


def test_version_env_override(client, monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_ENV", "production")
    resp = client.get("/api/dashboard/version")
    assert resp.json()["environment"] == "production"


# ─── Drift noise-filter: domain chip bar + suppress_telemetry ──────────────


def _seed_drift_noise(engine):
    """Seed a vuln-domain resource with both telemetry-noise fields and a
    meaningful field so suppress_telemetry can be tested against real rows."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        r_vuln = Resource(
            domain="vuln",
            type="asset",
            name="vuln-asset-01",
            source="rapid7",
            zone="corpor",
        )
        r_linux = Resource(
            domain="linux",
            type="host",
            name="linux-host-01",
            source="LinuxAgent",
            zone="corpor",
        )
        s.add(r_vuln)
        s.add(r_linux)
        s.flush()
        # Telemetry-noise events on vuln domain
        for noisy_field in ("ip", "risk_score", "vulnerabilities", "risk_factors", "last_seen"):
            s.add(
                DriftEvent(
                    resource_id=r_vuln.id,
                    drift_type="state_drift",
                    field=noisy_field,
                    old_value={"v": "old"},
                    new_value={"v": "new"},
                    status="open",
                    detected_at=now,
                )
            )
        # A meaningful vuln-domain event (field not in telemetry set)
        s.add(
            DriftEvent(
                resource_id=r_vuln.id,
                drift_type="state_drift",
                field="criticality_override",
                old_value={"v": "low"},
                new_value={"v": "high"},
                status="open",
                detected_at=now,
            )
        )
        # A linux-domain event
        s.add(
            DriftEvent(
                resource_id=r_linux.id,
                drift_type="config_drift",
                field="kernel",
                old_value={"v": "5.15"},
                new_value={"v": "5.19"},
                status="open",
                detected_at=now,
            )
        )
        s.commit()


def test_drift_domains_endpoint(client, engine):
    """GET /drift_events/domains returns distinct domains that have drift events."""
    _seed_drift_noise(engine)
    resp = client.get("/api/dashboard/drift_events/domains")
    assert resp.status_code == 200
    domains = resp.json()
    assert isinstance(domains, list)
    assert set(domains) == {"vuln", "linux"}


def test_drift_suppress_telemetry_removes_noisy_fields(client, engine):
    """suppress_telemetry=true drops vuln-domain events where field is one of
    the known telemetry noise fields (ip, risk_score, vulnerabilities,
    risk_factors, last_seen)."""
    _seed_drift_noise(engine)
    all_rows = client.get("/api/dashboard/drift_events").json()["items"]
    suppressed = client.get(
        "/api/dashboard/drift_events", params={"suppress_telemetry": "true"}
    ).json()["items"]
    # All rows: 5 noise + 1 meaningful vuln + 1 linux = 7
    assert len(all_rows) == 7
    # Suppressed: 1 meaningful vuln + 1 linux = 2
    assert len(suppressed) == 2
    suppressed_fields = {r["field_name"] for r in suppressed}
    assert suppressed_fields == {"criticality_override", "kernel"}


def test_drift_suppress_telemetry_with_domain_filter(client, engine):
    """suppress_telemetry combined with domain=linux should only keep
    the linux-domain event (no vuln events at all)."""
    _seed_drift_noise(engine)
    resp = client.get(
        "/api/dashboard/drift_events",
        params={"suppress_telemetry": "true", "domain": "linux"},
    )
    rows = resp.json()["items"]
    assert len(rows) == 1
    assert rows[0]["field_name"] == "kernel"
    assert rows[0]["domain"] == "linux"


def test_drift_suppress_false_returns_all(client, engine):
    """suppress_telemetry=false (or absent) returns all rows including noise."""
    _seed_drift_noise(engine)
    rows = client.get("/api/dashboard/drift_events", params={"suppress_telemetry": "false"}).json()[
        "items"
    ]
    assert len(rows) == 7


def test_get_host_lowercase_lookup(client, engine):
    """Fix 1.3: GET /api/dashboard/hosts/{hostname} must lowercase the lookup key."""
    # Seed a host with lowercase short_hostname
    with Session(engine) as s:
        host = HostIdentity(
            short_hostname="esxi-prod-04",
            fqdn="esxi-prod-04.corp.example.com",
            os_family="vmware",
        )
        s.add(host)
        s.commit()

    # Query with uppercase — should still find it
    resp = client.get("/api/dashboard/hosts/esxi-prod-04")
    assert resp.status_code == 200
    data = resp.json()
    assert data["short_hostname"] == "esxi-prod-04"


def test_get_host_404_not_found(client, engine):
    """GET /api/dashboard/hosts/{hostname} returns 404 for unknown host."""
    resp = client.get("/api/dashboard/hosts/nonexistent")
    assert resp.status_code == 404


def test_get_host_fqdn_input_matches_short_hostname_row(client, engine):
    """GitLab #121 Bug A: short_hostname is always stored in first-DNS-label
    form. Passing a full FQDN must truncate at query time (normalize_host())
    the same way the write side does — previously this silently 404'd."""
    with Session(engine) as s:
        host = HostIdentity(
            short_hostname="esxi-prod-04",
            fqdn="esxi-prod-04.corp.example.com",
            os_family="vmware",
        )
        s.add(host)
        s.commit()

    resp = client.get("/api/dashboard/hosts/esxi-prod-04.corp.example.com")
    assert resp.status_code == 200
    assert resp.json()["short_hostname"] == "esxi-prod-04"


def test_get_host_vulns_fqdn_input_matches_short_hostname_row(client, engine):
    """GitLab #121 Bug A twin: GET /hosts/{hostname}/vulns must also truncate
    an FQDN input to first-DNS-label form before matching short_hostname."""
    with Session(engine) as s:
        s.add(HostIdentity(short_hostname="octo-only-01", os_family="linux", risk_score=0))
        s.commit()

    resp = client.get("/api/dashboard/hosts/octo-only-01.corp.example.com/vulns")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


# ─── Regression tests for TRK-019 count-integrity fixes (+ lc-api-reviewer FIX #1) ──


def test_notifications_total_is_true_count_and_limit_is_clamped(engine, client):
    """FE-6: /notifications ``total`` is the true DB row count (JiraTicket +
    ConfluencePage), respecting the type filter — not the capped page length.
    FIX #1: an oversized ?limit is clamped to the 500 ceiling and a negative
    ?offset to 0, so the handler cannot pull unbounded rows into memory.
    """

    from infra_brain.db.models import ConfluencePage, JiraTicket

    now = datetime.now(UTC)
    with Session(engine) as s:
        for i in range(3):
            s.add(
                JiraTicket(
                    drift_event_id=_real_drift_event_id(s),
                    jira_key=f"JIRA-{i}",
                    created_at=now - timedelta(minutes=i),
                )
            )
        for i in range(2):
            s.add(
                ConfluencePage(
                    domain="linux",
                    page_id=f"page-{i}",
                    last_updated=now - timedelta(minutes=i),
                )
            )
        s.commit()

    both = client.get("/api/dashboard/notifications").json()
    assert both["total"] == 5
    assert len(both["items"]) == 5

    jira = client.get("/api/dashboard/notifications?type=jira").json()
    assert jira["total"] == 3
    conf = client.get("/api/dashboard/notifications?type=confluence").json()
    assert conf["total"] == 2

    # FIX #1: pagination is clamped (echoed back in the envelope) before any row
    # materialization — bounds memory regardless of caller-supplied values.
    clamped = client.get("/api/dashboard/notifications?limit=100000&offset=-5").json()
    assert clamped["limit"] == 500
    assert clamped["offset"] == 0
    assert len(clamped["items"]) <= 500


def test_notifications_include_deep_link_urls(engine, client, monkeypatch):
    """TRK-178 follow-up: NotificationOut carries jira_url/confluence_url deep
    links built from settings.jira_url / settings.confluence_url — base URLs
    only, never tokens/credentials. Unset base -> field stays None rather than
    a malformed URL with an empty prefix."""

    from infra_brain.db.models import ConfluencePage, JiraTicket

    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("CONFLUENCE_URL", "https://confluence.example.com")
    get_settings.cache_clear()

    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(
            JiraTicket(
                drift_event_id=_real_drift_event_id(s),
                jira_key="JIRA-42",
                created_at=now,
            )
        )
        s.add(
            ConfluencePage(
                domain="linux",
                page_id="page-99",
                last_updated=now,
            )
        )
        s.commit()

    try:
        resp = client.get("/api/dashboard/notifications?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        jira_row = next(r for r in body["items"] if r["type"] == "jira")
        assert jira_row["jira_url"] == "https://jira.example.com/browse/JIRA-42"
        assert jira_row["confluence_url"] is None

        conf_row = next(r for r in body["items"] if r["type"] == "confluence")
        assert (
            conf_row["confluence_url"]
            == "https://confluence.example.com/pages/viewpage.action?pageId=page-99"
        )
        assert conf_row["jira_url"] is None
    finally:
        get_settings.cache_clear()


def test_notifications_deep_link_urls_none_when_unconfigured(engine, client, monkeypatch):
    """Empty-string-safe: an unset JIRA_URL/CONFLUENCE_URL must leave the
    field None, not a malformed URL with an empty base."""

    from infra_brain.db.models import JiraTicket

    monkeypatch.setenv("JIRA_URL", "")
    monkeypatch.setenv("CONFLUENCE_URL", "")
    get_settings.cache_clear()

    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(
            JiraTicket(
                drift_event_id=_real_drift_event_id(s),
                jira_key="JIRA-1",
                created_at=now,
            )
        )
        s.commit()

    try:
        resp = client.get("/api/dashboard/notifications?type=jira&limit=10")
        row = resp.json()["items"][0]
        assert row["jira_url"] is None
    finally:
        get_settings.cache_clear()


def test_counts_open_cves_counts_distinct_open_cve_ids(engine, client):
    """FE-9: /counts ``open_cves`` counts DISTINCT cve_id among OPEN vuln_queue
    rows, not raw (host, CVE) rows — and excludes closed rows. Seed the same
    CVE on two hosts (two rows, one distinct id), a second distinct open CVE,
    and a closed CVE; expect 2.
    """
    with Session(engine) as s:
        r1 = Resource(domain="linux", type="host", name="h1", source="LinuxAgent")
        r2 = Resource(domain="linux", type="host", name="h2", source="LinuxAgent")
        s.add_all([r1, r2])
        s.flush()
        # Same CVE affecting two hosts -> two rows but ONE distinct open cve id.
        s.add(VulnQueueItem(resource_id=r1.id, cve_id="CVE-A", severity="high", status="open"))
        s.add(VulnQueueItem(resource_id=r2.id, cve_id="CVE-A", severity="high", status="open"))
        # A second distinct open cve (triage is also an OPEN status).
        s.add(VulnQueueItem(resource_id=r1.id, cve_id="CVE-B", severity="high", status="triage"))
        # A closed cve -> excluded.
        s.add(VulnQueueItem(resource_id=r1.id, cve_id="CVE-C", severity="high", status="resolved"))
        s.commit()

    data = client.get("/api/dashboard/counts").json()
    assert data["open_cves"] == 2


def test_counts_includes_per_severity_vuln_breakdown(engine, client):
    """TRK-166 follow-up: /counts exposes fleet-wide distinct-CVE counts broken
    down by severity (critical, severe/high) so pages that paginate the vuln
    list can show real fleet totals instead of page-limited client counts.
    """
    with Session(engine) as s:
        r1 = Resource(domain="linux", type="host", name="h1", source="LinuxAgent")
        s.add(r1)
        s.flush()
        s.add(
            VulnQueueItem(resource_id=r1.id, cve_id="CVE-CRIT", severity="critical", status="open")
        )
        s.add(VulnQueueItem(resource_id=r1.id, cve_id="CVE-SEV", severity="severe", status="open"))
        s.add(VulnQueueItem(resource_id=r1.id, cve_id="CVE-HIGH", severity="high", status="triage"))
        # Closed critical CVE -> excluded from the count.
        s.add(
            VulnQueueItem(
                resource_id=r1.id, cve_id="CVE-CLOSED", severity="critical", status="resolved"
            )
        )
        s.commit()

    resp = client.get("/api/dashboard/counts")
    body = resp.json()
    assert "critical_cves" in body
    assert "severe_cves" in body
    assert body["critical_cves"] == 1
    assert body["severe_cves"] == 2


def test_counts_includes_compliance_open_resolved_aggregate(engine, client):
    """TRK-167 follow-up: /counts exposes fleet-wide open/resolved
    ComplianceViolation counts so pages that paginate the compliance list
    (Compl.tsx) can show real fleet totals instead of page-limited client
    counts (see the "(page)" tiles this replaces)."""
    with Session(engine) as s:
        s.add(ComplianceViolation(rule="r1", host="h1", status="open"))
        s.add(ComplianceViolation(rule="r2", host="h1", status="open"))
        s.add(ComplianceViolation(rule="r3", host="h1", status="resolved"))
        s.commit()

    resp = client.get("/api/dashboard/counts")
    body = resp.json()
    assert "compliance_open" in body
    assert "compliance_resolved" in body
    assert body["compliance_open"] == 2
    assert body["compliance_resolved"] == 1


def test_resources_eol_status_gated_on_past_eol_date(engine, client):
    """FE-8: /resources marks status='eol' ONLY when the registry row's
    eol_date is set AND in the past. A NULL eol_date and a FUTURE eol_date are
    'healthy' — registry membership alone is not end-of-life (boundary).
    """
    now = datetime.now(UTC)
    with Session(engine) as s:
        past = Resource(domain="linux", type="host", name="past-eol", source="LinuxAgent")
        future = Resource(domain="linux", type="host", name="future-eol", source="LinuxAgent")
        nulld = Resource(domain="linux", type="host", name="null-eol", source="LinuxAgent")
        s.add_all([past, future, nulld])
        s.flush()
        s.add(EolRegistry(resource_id=past.id, asset_name="past", eol_date=now - timedelta(days=1)))
        s.add(
            EolRegistry(
                resource_id=future.id, asset_name="future", eol_date=now + timedelta(days=365)
            )
        )
        s.add(EolRegistry(resource_id=nulld.id, asset_name="nulld", eol_date=None))
        s.commit()

    items = client.get("/api/dashboard/resources?limit=100").json()["items"]
    status_by_host = {i["hostname"]: i["status"] for i in items}
    assert status_by_host["past-eol"] == "eol"
    assert status_by_host["future-eol"] == "healthy"
    assert status_by_host["null-eol"] == "healthy"


# ─── Phase 2: host-purpose-map GET/PUT — provenance + trailing persistence MR ──
#
# GET /api/dashboard/hosts/{hostname}/purpose (read-only, unset row -> 200/null).
# PUT ...                                     (DB write is authoritative + COMMITs
# BEFORE the trailing open_host_purpose_map_mr; an MR failure must NOT roll the
# committed edit back). open_host_purpose_map_mr is patched at the hosts-module
# path (where hosts.py imports it), not at the tool module.


def test_get_host_purpose_ui_provenance(client, engine):
    """A ``ui:<user>`` source (a human dashboard edit) -> provenance 'ui'."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(
            HostPurposeMap(
                hostname="web-01",
                purpose="Primary web frontend",
                vlan="VLAN20-App",
                subnet="10.90.12.0/24",
                source="ui:alice",
                updated_at=now,
            )
        )
        s.commit()

    resp = client.get("/api/dashboard/hosts/web-01/purpose")
    assert resp.status_code == 200
    data = resp.json()
    assert data["hostname"] == "web-01"
    assert data["purpose"] == "Primary web frontend"
    assert data["vlan"] == "VLAN20-App"
    assert data["subnet"] == "10.90.12.0/24"
    assert data["source"] == "ui:alice"
    assert data["provenance"] == "ui"


def test_get_host_purpose_repo_provenance(client, engine):
    """A repo-sync source (``<project_id>:<file>``) -> provenance 'repo'."""
    with Session(engine) as s:
        s.add(
            HostPurposeMap(
                hostname="db-01",
                purpose="Primary database",
                vlan="VLAN30",
                subnet=None,
                source="6:host_purpose_map.yml",
            )
        )
        s.commit()

    data = client.get("/api/dashboard/hosts/db-01/purpose").json()
    assert data["source"] == "6:host_purpose_map.yml"
    assert data["vlan"] == "VLAN30"
    assert data["provenance"] == "repo"


def test_get_host_purpose_missing_row_is_unset(client, engine):
    """A missing row returns 200 with all-null fields + provenance 'unset' (not
    404), so the dashboard can render an empty editable form."""
    resp = client.get("/api/dashboard/hosts/ghost-99/purpose")
    assert resp.status_code == 200
    data = resp.json()
    assert data["hostname"] == "ghost-99"
    assert data["purpose"] is None
    assert data["vlan"] is None
    assert data["subnet"] is None
    assert data["provenance"] == "unset"


def test_put_host_purpose_happy_path_persists_and_opens_mr(client, engine):
    """PUT commits the authoritative edit and opens the trailing persistence MR;
    the response echoes the ui:<user> source and the MR url, and the row is
    persisted (proven by re-querying the DB)."""
    fake_result = {
        "mr_url": "https://gl/mr/1",
        "branch": "b",
        "action": "created",
        "file_path": "host_purpose_map.yml",
    }
    with patch(
        "infra_brain.api.routers.hosts.open_host_purpose_map_mr",
        return_value=fake_result,
    ) as mock_mr:
        resp = client.put(
            "/api/dashboard/hosts/web-01/purpose",
            json={"purpose": "Edge proxy", "vlan": "VLAN10", "subnet": "10.0.0.0/24"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["purpose"] == "Edge proxy"
    assert data["vlan"] == "VLAN10"
    assert data["subnet"] == "10.0.0.0/24"
    assert data["source"].startswith("ui:")
    assert data["provenance"] == "ui"
    assert data["mr_url"] == "https://gl/mr/1"
    assert data["mr_error"] is None
    mock_mr.assert_called_once()

    # Re-query the DB: the authoritative edit must have persisted.
    with Session(engine) as s:
        row = s.query(HostPurposeMap).filter(HostPurposeMap.hostname == "web-01").one()
        assert row.purpose == "Edge proxy"
        assert row.vlan == "VLAN10"
        assert row.subnet == "10.0.0.0/24"
        assert row.source.startswith("ui:")


def test_put_host_purpose_mixed_case_does_not_fork_row(client, engine):
    """GitLab #121 Bug B: a PUT for 'WEB01' followed by a GET for 'web01' (or
    vice versa) must resolve to the SAME row, not silently create a second
    row via the case-sensitive ON CONFLICT (hostname) upsert."""
    fake_result = {
        "mr_url": "https://gl/mr/3",
        "branch": "b",
        "action": "created",
        "file_path": "host_purpose_map.yml",
    }
    with patch(
        "infra_brain.api.routers.hosts.open_host_purpose_map_mr",
        return_value=fake_result,
    ):
        put_resp = client.put(
            "/api/dashboard/hosts/WEB01/purpose",
            json={"purpose": "Edge proxy", "vlan": "VLAN10", "subnet": "10.0.0.0/24"},
        )
    assert put_resp.status_code == 200

    # A GET under a different casing must find the same row's data — not
    # "unset" — and there must be exactly one row in the table.
    get_resp = client.get("/api/dashboard/hosts/web01/purpose")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["purpose"] == "Edge proxy"
    assert data["provenance"] == "ui"

    with Session(engine) as s:
        rows = s.query(HostPurposeMap).filter(HostPurposeMap.hostname.ilike("web01")).all()
        assert len(rows) == 1

    # A second PUT under yet another casing updates the same row in place.
    with patch(
        "infra_brain.api.routers.hosts.open_host_purpose_map_mr",
        return_value=fake_result,
    ):
        second_put = client.put(
            "/api/dashboard/hosts/Web01/purpose",
            json={"purpose": "Edge proxy v2", "vlan": "VLAN10", "subnet": "10.0.0.0/24"},
        )
    assert second_put.status_code == 200

    with Session(engine) as s:
        rows = s.query(HostPurposeMap).filter(HostPurposeMap.hostname.ilike("web01")).all()
        assert len(rows) == 1
        assert rows[0].purpose == "Edge proxy v2"


def test_put_host_purpose_overwrites_existing_row(client, engine):
    """Second PUT for the same hostname updates the existing row in place —
    exactly one row remains, its id is stable, and all mutable fields plus the
    ui:<user> source reflect the latest edit (the ON CONFLICT DO UPDATE path)."""
    fake_result = {
        "mr_url": "https://gl/mr/2",
        "branch": "b",
        "action": "updated",
        "file_path": "host_purpose_map.yml",
    }
    with patch(
        "infra_brain.api.routers.hosts.open_host_purpose_map_mr",
        return_value=fake_result,
    ):
        first = client.put(
            "/api/dashboard/hosts/db-01/purpose",
            json={"purpose": "Postgres primary", "vlan": "VLAN20", "subnet": "10.0.2.0/24"},
        )
        with Session(engine) as s:
            first_id = s.query(HostPurposeMap).filter(HostPurposeMap.hostname == "db-01").one().id
        second = client.put(
            "/api/dashboard/hosts/db-01/purpose",
            json={"purpose": "Postgres replica", "vlan": "VLAN21", "subnet": None},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    data = second.json()
    assert data["purpose"] == "Postgres replica"
    assert data["vlan"] == "VLAN21"
    assert data["subnet"] is None
    assert data["provenance"] == "ui"

    with Session(engine) as s:
        rows = s.query(HostPurposeMap).filter(HostPurposeMap.hostname == "db-01").all()
        assert len(rows) == 1
        assert rows[0].id == first_id
        assert rows[0].purpose == "Postgres replica"
        assert rows[0].vlan == "VLAN21"
        assert rows[0].subnet is None


def test_put_host_purpose_mr_failure_does_not_rollback_db(client, engine):
    """CRITICAL: if the trailing MR raises, the response carries mr_url=None and
    mr_error, and the already-COMMITted DB edit is NOT rolled back."""
    with patch(
        "infra_brain.api.routers.hosts.open_host_purpose_map_mr",
        side_effect=PermissionError("gate denied"),
    ):
        resp = client.put(
            "/api/dashboard/hosts/keep-01/purpose",
            json={"purpose": "Kept purpose", "vlan": "VLAN99", "subnet": None},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["mr_url"] is None
    assert "gate denied" in (data["mr_error"] or "")
    assert data["purpose"] == "Kept purpose"

    # The committed edit survives the MR failure (no rollback).
    with Session(engine) as s:
        row = s.query(HostPurposeMap).filter(HostPurposeMap.hostname == "keep-01").one()
        assert row.purpose == "Kept purpose"
        assert row.vlan == "VLAN99"
        assert row.source.startswith("ui:")


def test_host_purpose_endpoints_require_auth():
    """Unauthenticated GET/PUT on the purpose endpoints -> 401 (the hosts_router
    is session-gated), matching every other dashboard endpoint's auth contract."""
    import os

    os.environ.pop("INFRA_BRAIN_DEV", None)
    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    from infra_brain.api.routers.hosts import hosts_router
    from infra_brain.dashboard_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(hosts_router)
    try:
        with (
            patch.dict(os.environ, {"UI_COOKIE_SECRET": "unit-test-secret"}),
            patch("infra_brain.dashboard_auth.get_session", _get_session),
            patch("infra_brain.api.routers.hosts.get_session", _get_session),
        ):
            get_settings.cache_clear()
            c = TestClient(app)
            assert c.get("/api/dashboard/hosts/web-01/purpose").status_code == 401
            assert (
                c.put("/api/dashboard/hosts/web-01/purpose", json={"purpose": "x"}).status_code
                == 401
            )
    finally:
        get_settings.cache_clear()


# ─── Resource ownership / on-call / criticality (issue #116) ─────────────────
#
# GET /api/dashboard/resources/{resource_id}/ownership (read-only, missing row
# -> 200/nulls; unknown/malformed resource_id -> 404).
# PUT ...                                              (human-authoritative,
# ON CONFLICT (resource_id) DO UPDATE upsert — no trailing GitLab MR, unlike
# put_host_purpose, since no curated source-of-truth file exists for this data).


def _seed_resource(engine, *, name="prod-web-01"):
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name=name, source="LinuxAgent")
        s.add(r)
        s.commit()
        return str(r.id)


def test_get_resource_ownership_missing_row_is_null(client, engine):
    """A resource with no ownership row yet returns 200 with all-null fields
    (not 404), so the dashboard can render an empty editable form."""
    resource_id = _seed_resource(engine)
    resp = client.get(f"/api/dashboard/resources/{resource_id}/ownership")
    assert resp.status_code == 200
    data = resp.json()
    assert data["resource_id"] == resource_id
    assert data["owner_team"] is None
    assert data["on_call_rotation"] is None
    assert data["criticality_tier"] is None
    assert data["source"] is None
    assert data["updated_at"] is None


def test_get_resource_ownership_unknown_resource_404s(client, engine):
    """An unknown (but well-formed) resource_id 404s — unlike HostPurposeMap's
    hostname key, resource_id is a real FK, so a nonexistent resource has no
    identity to hang an ownership row off of."""
    resp = client.get(f"/api/dashboard/resources/{uuid.uuid4()}/ownership")
    assert resp.status_code == 404


def test_get_resource_ownership_malformed_id_404s(client, engine):
    resp = client.get("/api/dashboard/resources/not-a-uuid/ownership")
    assert resp.status_code == 404


def test_get_resource_ownership_returns_persisted_row(client, engine):
    resource_id = _seed_resource(engine)
    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(
            ResourceOwnership(
                resource_id=uuid.UUID(resource_id),
                owner_team="platform-infra",
                on_call_rotation="pagerduty:infra-oncall",
                criticality_tier="tier-1",
                source="ui:alice",
                updated_at=now,
            )
        )
        s.commit()

    resp = client.get(f"/api/dashboard/resources/{resource_id}/ownership")
    assert resp.status_code == 200
    data = resp.json()
    assert data["owner_team"] == "platform-infra"
    assert data["on_call_rotation"] == "pagerduty:infra-oncall"
    assert data["criticality_tier"] == "tier-1"
    assert data["source"] == "ui:alice"


def test_put_resource_ownership_creates_row(client, engine):
    resource_id = _seed_resource(engine)
    resp = client.put(
        f"/api/dashboard/resources/{resource_id}/ownership",
        json={
            "owner_team": "platform-infra",
            "on_call_rotation": "pagerduty:infra-oncall",
            "criticality_tier": "tier-1",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["owner_team"] == "platform-infra"
    assert data["on_call_rotation"] == "pagerduty:infra-oncall"
    assert data["criticality_tier"] == "tier-1"
    assert data["source"].startswith("ui:")

    with Session(engine) as s:
        row = (
            s.query(ResourceOwnership)
            .filter(ResourceOwnership.resource_id == uuid.UUID(resource_id))
            .one()
        )
        assert row.owner_team == "platform-infra"
        assert row.criticality_tier == "tier-1"
        assert row.source.startswith("ui:")


def test_put_resource_ownership_unknown_resource_404s(client, engine):
    resp = client.put(
        f"/api/dashboard/resources/{uuid.uuid4()}/ownership",
        json={"owner_team": "x"},
    )
    assert resp.status_code == 404


def test_put_resource_ownership_overwrites_existing_row(client, engine):
    """Second PUT for the same resource_id updates the existing row in
    place — exactly one row remains, its id is stable, and all mutable
    fields reflect the latest edit (the ON CONFLICT DO UPDATE path)."""
    resource_id = _seed_resource(engine)
    first = client.put(
        f"/api/dashboard/resources/{resource_id}/ownership",
        json={
            "owner_team": "platform-infra",
            "on_call_rotation": "pagerduty:infra-oncall",
            "criticality_tier": "tier-1",
        },
    )
    assert first.status_code == 200
    with Session(engine) as s:
        first_id = (
            s.query(ResourceOwnership)
            .filter(ResourceOwnership.resource_id == uuid.UUID(resource_id))
            .one()
            .id
        )

    second = client.put(
        f"/api/dashboard/resources/{resource_id}/ownership",
        json={
            "owner_team": "platform-infra-2",
            "on_call_rotation": None,
            "criticality_tier": "tier-2",
        },
    )
    assert second.status_code == 200
    data = second.json()
    assert data["owner_team"] == "platform-infra-2"
    assert data["on_call_rotation"] is None
    assert data["criticality_tier"] == "tier-2"

    with Session(engine) as s:
        rows = (
            s.query(ResourceOwnership)
            .filter(ResourceOwnership.resource_id == uuid.UUID(resource_id))
            .all()
        )
        assert len(rows) == 1
        assert rows[0].id == first_id
        assert rows[0].owner_team == "platform-infra-2"
        assert rows[0].criticality_tier == "tier-2"


def test_resource_ownership_endpoints_require_auth():
    """Unauthenticated GET/PUT on the ownership endpoints -> 401 (the
    resources_router is session-gated), matching every other dashboard
    endpoint's auth contract."""
    import os

    os.environ.pop("INFRA_BRAIN_DEV", None)
    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    from infra_brain.api.routers.hosts import resources_router
    from infra_brain.dashboard_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(resources_router)
    try:
        with (
            patch.dict(os.environ, {"UI_COOKIE_SECRET": "unit-test-secret"}),
            patch("infra_brain.dashboard_auth.get_session", _get_session),
            patch("infra_brain.api.routers.hosts.get_session", _get_session),
        ):
            get_settings.cache_clear()
            c = TestClient(app)
            fake_id = str(uuid.uuid4())
            assert c.get(f"/api/dashboard/resources/{fake_id}/ownership").status_code == 401
            assert (
                c.put(
                    f"/api/dashboard/resources/{fake_id}/ownership", json={"owner_team": "x"}
                ).status_code
                == 401
            )
    finally:
        get_settings.cache_clear()


def _seed_proposal(engine, *, status="pending"):
    with Session(engine) as s:
        p = IntegrationProposal(
            source="coverage-gap:cloud",
            type="domain-agent",
            endpoint="all",
            confidence=0.85,
            status=status,
        )
        s.add(p)
        s.commit()
        return str(p.id)


def test_approve_integration_proposal_wires_it(client, engine, monkeypatch):
    """#93: approving a pending IntegrationProposal must actually call
    CoverageAgent.wire() — today there is no approve endpoint at all, so
    wire() (fully built, confirmed at coverage.py:269-368) has zero
    non-test callers."""
    from unittest.mock import patch

    pid = _seed_proposal(engine, status="pending")
    with patch("infra_brain.agents.coverage.CoverageAgent.wire", return_value=True) as mock_wire:
        resp = client.post(f"/api/dashboard/integration_proposals/{pid}/approve", json={})
    assert resp.status_code == 200
    assert resp.json()["approved"] is True
    # Regression guard: IntegrationProposalApproveResult collided with a
    # pre-existing, differently-shaped ProposalApproveResult in webhooks.py
    # (approved/proposal_id only) — that collision silently dropped `wired`
    # from the response via FastAPI's response_model field filtering.
    assert resp.json()["wired"] is True
    mock_wire.assert_called_once_with(pid)
    with Session(engine) as s:
        row = s.get(IntegrationProposal, uuid.UUID(pid))
        assert row.status == "approved"  # wire() itself flips to "wired" — mocked here
        assert row.approved_by


def test_approve_integration_proposal_reverts_to_pending_on_wire_exception(
    client, engine, monkeypatch
):
    """A wire() exception must not strand the proposal at status="approved"
    with no recovery path — it should revert to "pending" so the same
    approve endpoint can be retried."""
    from unittest.mock import patch

    pid = _seed_proposal(engine, status="pending")
    with patch("infra_brain.agents.coverage.CoverageAgent.wire", side_effect=RuntimeError("boom")):
        resp = client.post(f"/api/dashboard/integration_proposals/{pid}/approve", json={})
    assert resp.status_code == 500
    with Session(engine) as s:
        row = s.get(IntegrationProposal, uuid.UUID(pid))
        assert row.status == "pending"
        assert row.approved_by is None
        assert row.approved_at is None


def test_approve_integration_proposal_reverts_to_pending_on_wire_false(client, engine):
    """Same recovery guarantee when wire() returns False cleanly instead of
    raising."""
    pid = _seed_proposal(engine, status="pending")
    with patch("infra_brain.agents.coverage.CoverageAgent.wire", return_value=False):
        resp = client.post(f"/api/dashboard/integration_proposals/{pid}/approve", json={})
    assert resp.status_code == 500
    with Session(engine) as s:
        row = s.get(IntegrationProposal, uuid.UUID(pid))
        assert row.status == "pending"


def test_approve_integration_proposal_conflict_when_not_pending(client, engine):
    pid = _seed_proposal(engine, status="wired")
    resp = client.post(f"/api/dashboard/integration_proposals/{pid}/approve", json={})
    assert resp.status_code == 409


def test_reject_integration_proposal(client, engine):
    pid = _seed_proposal(engine, status="pending")
    resp = client.post(f"/api/dashboard/integration_proposals/{pid}/reject")
    assert resp.status_code == 200
    with Session(engine) as s:
        row = s.get(IntegrationProposal, uuid.UUID(pid))
        assert row.status == "rejected"


def test_agent_roster_dicts_cover_every_registered_domain():
    """Regression guard for #74: _AGENT_DESC/_AGENT_TOOLS must have a
    non-empty entry for every domain in AGENT_REGISTRY, and no dead entries
    for domains no longer in it."""
    from infra_brain.api.routers.governance_ops import _AGENT_DESC, _AGENT_TOOLS
    from infra_brain.supervisor import AGENT_REGISTRY

    registry_domains = set(AGENT_REGISTRY.keys())

    missing_desc = registry_domains - set(_AGENT_DESC)
    missing_tools = registry_domains - set(_AGENT_TOOLS)
    assert not missing_desc, f"_AGENT_DESC missing domains: {sorted(missing_desc)}"
    assert not missing_tools, f"_AGENT_TOOLS missing domains: {sorted(missing_tools)}"

    dead_desc = set(_AGENT_DESC) - registry_domains
    dead_tools = set(_AGENT_TOOLS) - registry_domains
    assert not dead_desc, f"_AGENT_DESC has dead entries: {sorted(dead_desc)}"
    assert not dead_tools, f"_AGENT_TOOLS has dead entries: {sorted(dead_tools)}"

    for domain in registry_domains:
        assert _AGENT_DESC[domain].strip(), f"{domain}: empty description"
        assert _AGENT_TOOLS[domain], f"{domain}: empty tools list"


# ---------------------------------------------------------------------------
# Drift readability (maintainer: "I don't understand what drifted or why they
# are flagged as drift"). GET /api/dashboard/drift_events must carry a
# server-computed plain-English `summary`, a `rule` line explaining WHY the
# event was raised, and unwrapped `old_display`/`new_display` values for the
# drawer's before/after diff — derived at READ time from
# infra_brain.drift_taxonomy, with no schema change.
# ---------------------------------------------------------------------------


def test_drift_events_carry_a_human_readable_summary(client, engine):
    _seed(engine)
    item = client.get("/api/dashboard/drift_events").json()["items"][0]
    assert item["summary"] == "kernel changed from old to new on prod-web-01"


def test_drift_events_carry_the_why_is_this_flagged_rule_line(client, engine):
    from infra_brain.drift_taxonomy import DRIFT_RULE_EXPLANATIONS

    _seed(engine)
    item = client.get("/api/dashboard/drift_events").json()["items"][0]
    assert item["rule"] == DRIFT_RULE_EXPLANATIONS["config_drift"]


def test_drift_events_expose_unwrapped_display_values_for_the_diff_view(client, engine):
    """The drawer's before/after view must not have to parse ``"{'v': 'old'}"``
    — the raw ``old_value``/``new_value`` strings stay exactly as they were
    (contract stability), and the unwrapped scalars arrive alongside them."""
    _seed(engine)
    item = client.get("/api/dashboard/drift_events").json()["items"][0]
    assert item["old_display"] == "old"
    assert item["new_display"] == "new"
    # Pre-existing fields are untouched.
    assert item["old_value"] == "{'v': 'old'}"
    assert item["new_value"] == "{'v': 'new'}"


def test_every_drift_event_on_the_page_has_a_non_empty_summary_and_rule(client, engine):
    """Load-bearing: the API returns `summary` on EVERY event, across every
    drift_type/payload shape — including the ones no live row exercises and a
    deliberately malformed payload."""
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web01", source="LinuxAgent")
        s.add(r)
        s.flush()
        shapes = [
            ("config_drift", "kernel", {"v": "5.15"}, {"v": "6.8"}),
            ("new_listening_port", "port", None, {"port": 8443}),
            ("new_windows_service", "service_name", None, {"service_name": "Spooler"}),
            ("service_stopped", "service:Spooler", {"state": "Running"}, {"state": "Stopped"}),
            (
                "threat_escalation",
                "threat_level",
                {"threat_level": "low"},
                {"threat_level": "high"},
            ),
            ("shadow_it_discovered", "ip", None, {"ip": "10.0.0.9", "hostname": None}),
            (
                "dangerous_service_discovered",
                "port/tcp/23",
                None,
                {"port": 23, "proto": "tcp", "service": "telnet", "banner": ""},
            ),
            (
                "potential_secret_in_iac",
                "file_path",
                None,
                {"file_path": "g.yml", "secret_type": "token", "confidence_tier": "literal"},
            ),
            (
                "identity_conflict",
                "ip_address",
                {"ip": "10.0.0.5", "source": "vsphere"},
                {"ip": "10.0.0.9", "source": "linux"},
            ),
            ("state_drift", "presence", None, None),
            ("a_type_from_the_future", "mystery", ["not", "a", "dict"], 12345),
            ("config_drift", "broken", None, None),
        ]
        for drift_type, field, old, new in shapes:
            s.add(
                DriftEvent(
                    resource_id=r.id,
                    drift_type=drift_type,
                    field=field,
                    old_value=old,
                    new_value=new,
                    status="open",
                )
            )
        s.commit()

    items = client.get("/api/dashboard/drift_events").json()["items"]
    assert len(items) == len(shapes)
    for item in items:
        assert item["summary"].strip(), item
        assert item["rule"].strip(), item
        # The raw dict/JSON envelope must never be what an operator reads first.
        assert not item["summary"].startswith("{"), item
