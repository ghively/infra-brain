"""Comprehensive endpoint-to-shape contract test (Task 5.7).

ONE parametrized test hits every GET endpoint across ALL router files via
TestClient and asserts its top-level shape matches a shared, explicit map:

  "page"   → JSON object with items (list), total (int), limit (int), offset (int)
  "list"   → bare JSON array   (documented exceptions: /software/vendors, /drift_events/domains)
  "object" → JSON object WITHOUT items/total/limit/offset at the top level
              (overviews, detail records, /version, etc.)

Routes excluded from the map (different auth surface or non-GET write operations):
  - webhooks.py GET /scan_points — webhook-secret auth, no response_model
  - All POST/PATCH/DELETE routes — write operations, not collection shapes

Routes that require path parameters use a SHARED seeded DB so that:
  - /resources/{resource_id}/snapshots  → seeded Resource ID
  - /resources/{resource_id}/linux      → 404 expected (no LinuxHost seeded) — object endpoints that
  - /resources/{resource_id}/windows    → 404 expected (no WindowsPatchState seeded)  return 404 on
  - /cves/{cve_id}                      → 404 expected (no R7VulnCve seeded)           missing data
  - /hosts/{hostname}/vulns             → seeded HostIdentity                          are still valid
  - /hosts/{hostname}                   → seeded HostIdentity                          contract tests
  - /views/{share_token}               → seeded CustomView (public)                  (shape=object)

The test accepts HTTP 200 or 404 for detail "object" routes (a 404 is a valid JSON
object response with {"detail": "..."}).  It fails on any 5xx or on a shape mismatch.

Duplication note:
  test_contract.py (Task 5.3/5.4) and test_envelope.py (Task 5.2) cover subsets of
  the routes and the _degraded_body_for() unit logic.  This test is additive — it
  does NOT replace those tests.  The narrower tests provide sharper diagnostic messages
  for specific failures; this map-test provides exhaustive breadth.  Both are kept.
"""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from unittest.mock import patch

from infra_brain.db.models import (
    CollectionRun,
    ComplianceViolation,
    CustomView,
    DriftEvent,
    EolRegistry,
    GeneratedScript,
    HostIdentity,
    Instinct,
    IntegrationProposal,
    InventoryReconcileEvent,
    Observation,
    ProposedAction,
    R7Asset,
    R7Software,
    Resource,
    ScanPoint,
    AgentActionLog,
    AgentDecisionLog,
)

from tests.support.pg import make_engine


# ---------------------------------------------------------------------------
# ENDPOINT_SHAPES map
#
# Format: (method, full_path_template, shape)
#   shape:  "page"   = {items, total, limit, offset}
#           "list"   = bare JSON array (documented exceptions)
#           "object" = JSON object without items/total/limit/offset
#
# path_template uses Python .format() keys that match SEED_KEYS below.
# ---------------------------------------------------------------------------

ENDPOINT_SHAPES: list[tuple[str, str, str]] = [
    # -------------------------------------------------------------------
    # cve.py  (prefix: /api/dashboard)
    # -------------------------------------------------------------------
    ("GET", "/api/dashboard/cves", "page"),
    ("GET", "/api/dashboard/cves/{cve_id}", "object"),  # 404 when no CVE seeded → object
    # -------------------------------------------------------------------
    # fleet.py  (prefix: /api/dashboard)
    # -------------------------------------------------------------------
    ("GET", "/api/dashboard/counts", "object"),
    ("GET", "/api/dashboard/collection_runs", "page"),
    ("GET", "/api/dashboard/scan_points", "page"),
    ("GET", "/api/dashboard/system_health", "page"),
    ("GET", "/api/dashboard/version", "object"),
    ("GET", "/api/dashboard/fleet", "page"),
    ("GET", "/api/dashboard/software/vendors", "list"),  # documented exception: flat str list
    ("GET", "/api/dashboard/software", "page"),
    # -------------------------------------------------------------------
    # governance.py  (prefix: /api/dashboard)
    # -------------------------------------------------------------------
    ("GET", "/api/dashboard/drift_events/trend", "object"),  # DriftTrendOut (not a collection)
    ("GET", "/api/dashboard/drift_events/domains", "list"),  # documented exception: flat str list
    ("GET", "/api/dashboard/drift_events", "page"),
    ("GET", "/api/dashboard/notifications", "page"),
    ("GET", "/api/dashboard/instincts", "page"),
    ("GET", "/api/dashboard/observations", "page"),
    ("GET", "/api/dashboard/generated_scripts", "page"),
    ("GET", "/api/dashboard/integration_proposals", "page"),
    ("GET", "/api/dashboard/activity", "page"),
    ("GET", "/api/dashboard/decisions", "page"),
    ("GET", "/api/dashboard/audit_log", "page"),
    ("GET", "/api/dashboard/inventory_reconcile", "page"),
    ("GET", "/api/dashboard/compliance", "page"),
    ("GET", "/api/dashboard/agents", "page"),
    ("GET", "/api/dashboard/settings", "page"),
    ("GET", "/api/dashboard/settings/ui", "page"),  # TRK-321 narrow subset
    ("GET", "/api/dashboard/agent-config", "page"),
    # -------------------------------------------------------------------
    # hosts.py — resources_router  (prefix: /api/dashboard)
    # -------------------------------------------------------------------
    ("GET", "/api/dashboard/resources", "page"),
    ("GET", "/api/dashboard/resources/{resource_id}/snapshots", "page"),
    # linux/windows detail: returns object; 404 acceptable if no LinuxHost/WindowsPatchState seeded
    ("GET", "/api/dashboard/resources/{resource_id}/linux", "object"),
    ("GET", "/api/dashboard/resources/{resource_id}/windows", "object"),
    ("GET", "/api/dashboard/eol", "page"),
    # -------------------------------------------------------------------
    # hosts.py — hosts_router  (prefix: /api/dashboard/hosts)
    # -------------------------------------------------------------------
    ("GET", "/api/dashboard/hosts", "page"),
    ("GET", "/api/dashboard/hosts/{hostname}/vulns", "page"),
    ("GET", "/api/dashboard/hosts/{hostname}", "object"),
    # -------------------------------------------------------------------
    # iac.py  (prefix: /api/iac)
    # -------------------------------------------------------------------
    ("GET", "/api/iac/overview", "object"),
    # -------------------------------------------------------------------
    # octopus.py — removed (P7.1a, D6/D11): the agent-facing Octopus
    # dashboard router was deleted; the backend collector/models stay
    # dormant but no longer serve any GET routes.
    # -------------------------------------------------------------------
    # ui.py  (prefix: /api/dashboard)
    # (POST /chat, POST /custom-view → streaming, excluded as non-GET)
    # -------------------------------------------------------------------
    ("GET", "/api/dashboard/views", "page"),
    ("GET", "/api/dashboard/views/{share_token}", "object"),
    # -------------------------------------------------------------------
    # vsphere.py  (prefix: /api/vsphere)
    # -------------------------------------------------------------------
    ("GET", "/api/vsphere/overview", "object"),
    # -------------------------------------------------------------------
    # vuln.py  (prefix: /api/dashboard)
    # -------------------------------------------------------------------
    ("GET", "/api/dashboard/vulnerabilities", "page"),
    ("GET", "/api/dashboard/remediation", "page"),
    # -------------------------------------------------------------------
    # graph_api.py  (prefix: /api/graph)
    # -------------------------------------------------------------------
    ("GET", "/api/graph/stats", "page"),
    ("GET", "/api/graph/kg/stats", "object"),  # KgStatsOut (node_types + edge_types)
    ("GET", "/api/graph/kg/search?q=zzz_absolutely_no_match_zzz", "object"),  # KgSearchOut
    ("GET", "/api/graph/kg/edges", "page"),
]


# ---------------------------------------------------------------------------
# Shared seeded engine fixture (module-scoped for performance)
# ---------------------------------------------------------------------------

SEEDED: dict = {}  # populated by seeded_engine fixture


@pytest.fixture(scope="module")
def map_engine():
    """Single in-memory SQLite engine with rows seeded for every path param."""
    eng = make_engine()
    now = datetime.now(timezone.utc)

    with Session(eng) as s:
        # Resource (used by resources, snapshots, linux/windows detail, eol, drift, etc.)
        r = Resource(
            domain="linux",
            type="host",
            name="map-contract-host",
            source="LinuxAgent",
            zone="corpor",
        )
        s.add(r)
        s.flush()
        resource_id = str(r.id)

        # Seed enough data so the paged routes return at least one item
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
                eol_date=now,
            )
        )
        s.add(
            InventoryReconcileEvent(
                host="map-host-02",
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
                count=1,
                last_seen=now,
            )
        )
        s.add(
            GeneratedScript(
                name="map-test.sh",
                language="bash",
                purpose="test",
                content="echo hi",
                content_sha256="def456",
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
                started_at=now,
            )
        )
        s.add(
            ScanPoint(
                domain="linux",
                method="ansible",
                endpoint="/inv",
                schedule="0 */6 * * *",
            )
        )
        s.add(
            ComplianceViolation(
                rule="pci-req-6",
                severity="high",
                host="map-contract-host",
                detail="outdated OS",
                status="open",
                detected_at=now,
            )
        )
        s.add(
            ProposedAction(
                agent="remediation",
                action_type="patch",
                target="map-contract-host",
                confidence=0.8,
                status="pending",
            )
        )

        # R7Asset for fleet/software routes
        r7_asset = R7Asset(r7_asset_id=7001, hostname="map-fleet-host")
        s.add(r7_asset)
        s.flush()
        s.add(
            R7Software(
                asset_id=r7_asset.id,
                r7_asset_id=7001,
                product="MapTestPkg",
                vendor="ACME",
                version="1.0.0",
                software_type="application",
            )
        )

        # HostIdentity for /hosts/{hostname}/* routes
        host = HostIdentity(short_hostname="map-host-01")
        s.add(host)

        # CustomView (public, no user_id required) for /views/{share_token}
        view = CustomView(
            title="Map Contract View",
            prompt="show me stuff",
            openui_lang="react",
            share_token="map-contract-token-01",
            is_public=True,
        )
        s.add(view)

        s.commit()

    # Store seed values for path param substitution
    SEEDED["resource_id"] = resource_id
    SEEDED["hostname"] = "map-host-01"
    SEEDED["share_token"] = "map-contract-token-01"
    SEEDED["resource_uuid"] = resource_id  # also used for /api/graph/{resource_id}
    # cve_id: no R7VulnCve seeded → /cves/{cve_id} will 404 (object shape: {"detail": ...})
    SEEDED["cve_id"] = "CVE-9999-99999"

    return eng


@pytest.fixture(scope="module")
def map_client(map_engine):
    """TestClient wired to ALL routers with get_session patched and require_session
    bypassed via FastAPI's dependency_overrides (not via mock.patch — see below).
    """
    from infra_brain.api.routers.cve import cve_router
    from infra_brain.api.routers.fleet import fleet_router
    from infra_brain.api.routers.governance import (
        governance_audit_router,
        governance_drift_router,
        governance_intelligence_router,
        governance_ops_router,
    )
    from infra_brain.api.routers.hosts import hosts_router, resources_router
    from infra_brain.api.routers.iac import iac_router
    from infra_brain.api.routers.ui import ui_public_router, ui_router
    from infra_brain.api.routers.vsphere import vsphere_router
    from infra_brain.api.routers.vuln import vuln_router
    from infra_brain.dashboard_auth import require_admin, require_session
    from infra_brain.graph_api import graph_router

    @contextmanager
    def _get_session():
        with Session(map_engine) as s:
            yield s

    app = FastAPI()
    app.include_router(cve_router)
    app.include_router(fleet_router)
    app.include_router(governance_drift_router)
    app.include_router(governance_audit_router)
    app.include_router(governance_intelligence_router)
    app.include_router(governance_ops_router)
    app.include_router(resources_router)
    app.include_router(hosts_router)
    app.include_router(iac_router)
    app.include_router(ui_router)
    # TRK-306: GET /views/{share_token} moved to its own un-gated router so a
    # logged-out recipient can open a public share link. Without mounting it the
    # route is absent and PATCH/DELETE on the same path answer with 405.
    app.include_router(ui_public_router)
    app.include_router(vsphere_router)
    app.include_router(vuln_router)
    app.include_router(graph_router)

    _patches = [
        # get_session patches — route each router to the shared in-memory DB
        patch("infra_brain.api.routers.cve.get_session", _get_session),
        patch("infra_brain.api.routers.fleet.get_session", _get_session),
        patch("infra_brain.api.routers.governance_drift.get_session", _get_session),
        patch("infra_brain.api.routers.governance_audit.get_session", _get_session),
        patch("infra_brain.api.routers.governance_intelligence.get_session", _get_session),
        patch("infra_brain.api.routers.governance_ops.get_session", _get_session),
        patch("infra_brain.api.routers.hosts.get_session", _get_session),
        patch("infra_brain.api.routers.iac.get_session", _get_session),
        patch("infra_brain.api.routers.ui.get_session", _get_session),
        patch("infra_brain.api.routers.vsphere.get_session", _get_session),
        patch("infra_brain.api.routers.vuln.get_session", _get_session),
        patch("infra_brain.graph_api.get_session", _get_session),
    ]

    # Bypass the session gate so the TestClient doesn't hit auth redirects.
    # NOTE: mock.patch("<router module>.require_session", ...) does NOT work here —
    # every router does `dependencies=[Depends(require_session)]` at router-definition
    # time, so FastAPI already holds a direct reference to the real function object;
    # patching the module attribute afterward never reaches it. dependency_overrides
    # is FastAPI's supported mechanism: it matches by the callable object itself
    # (all routers import the same infra_brain.dashboard_auth.require_session), so one
    # override here covers every router regardless of which module imported it.
    app.dependency_overrides[require_session] = lambda: None
    # TRK-321 made GET /settings admin-gated. require_admin is a DIFFERENT
    # callable, so the require_session override above no longer reaches it —
    # bypass both. This fixture asserts response SHAPE, not auth; the gating
    # itself is asserted in tests/test_dashboard_auth.py.
    app.dependency_overrides[require_admin] = lambda: None

    with ExitStack() as stack:
        for p in _patches:
            stack.enter_context(p)
        yield TestClient(app)


# ---------------------------------------------------------------------------
# Shape assertion helpers
# ---------------------------------------------------------------------------


def _assert_page_shape(data, path: str) -> None:
    """Assert the response is a valid {items, total, limit, offset} envelope."""
    assert isinstance(data, dict), (
        f"[{path}] Expected 'page' dict envelope, got {type(data).__name__}: {data!r}"
    )
    for key in ("items", "total", "limit", "offset"):
        assert key in data, f"[{path}] Missing '{key}' in page envelope; keys: {list(data.keys())}"
    assert isinstance(data["items"], list), f"[{path}] 'items' must be a list"
    assert isinstance(data["total"], int), f"[{path}] 'total' must be an int"
    assert isinstance(data["limit"], int), f"[{path}] 'limit' must be an int"
    assert isinstance(data["offset"], int), f"[{path}] 'offset' must be an int"


def _assert_list_shape(data, path: str) -> None:
    """Assert the response is a bare JSON array."""
    assert isinstance(data, list), (
        f"[{path}] Expected 'list' (bare array), got {type(data).__name__}: {data!r}"
    )


def _assert_object_shape(data, path: str) -> None:
    """Assert the response is a JSON object (dict) but NOT a page envelope.

    A 404 {"detail": "..."} is a valid JSON object — detail routes that return
    404 when seed data is absent still satisfy the 'object' shape contract.
    """
    assert isinstance(data, dict), (
        f"[{path}] Expected 'object' (dict), got {type(data).__name__}: {data!r}"
    )
    # A page-shaped object masquerading as an "object" is a mis-classification bug.
    # If both items+total+limit+offset are present this entry belongs in "page".
    page_keys = {"items", "total", "limit", "offset"}
    if page_keys.issubset(data.keys()):
        raise AssertionError(
            f"[{path}] Response has page-envelope keys but shape is declared 'object'. "
            "Either the route should be 'page' in ENDPOINT_SHAPES, or the route was "
            "accidentally converted to a page envelope when it should remain an object.\n"
            f"Keys: {list(data.keys())}"
        )


# ---------------------------------------------------------------------------
# Parametrized map test — the core deliverable for Task 5.7
# ---------------------------------------------------------------------------


def _resolve_path(path_template: str) -> str:
    """Substitute seed values into path template placeholders."""
    return path_template.format(**SEEDED)


@pytest.mark.parametrize(
    "method,path_template,expected_shape",
    ENDPOINT_SHAPES,
    ids=[f"{m} {p}" for m, p, _ in ENDPOINT_SHAPES],
)
def test_endpoint_shape(map_client, method, path_template, expected_shape):
    """Every endpoint must return the declared top-level shape.

    Accepted status codes per shape:
      "page"   → 200 only (a paged collection must always succeed with empty results)
      "list"   → 200 only
      "object" → 200 or 404 (detail routes may 404 when seed data is absent)
                          or 422 (graph /{resource_id} with invalid UUID placeholder,
                          not applicable here since we use real UUIDs)
    """
    path = _resolve_path(path_template)
    assert method == "GET", f"Only GET routes in ENDPOINT_SHAPES; got {method} for {path}"
    resp = map_client.get(path)

    if expected_shape == "page":
        assert resp.status_code == 200, (
            f"[{path}] Expected 200 for 'page' route, got {resp.status_code}: {resp.text[:200]}"
        )
        _assert_page_shape(resp.json(), path)

    elif expected_shape == "list":
        assert resp.status_code == 200, (
            f"[{path}] Expected 200 for 'list' route, got {resp.status_code}: {resp.text[:200]}"
        )
        _assert_list_shape(resp.json(), path)

    elif expected_shape == "object":
        assert resp.status_code in (200, 404), (
            f"[{path}] Expected 200 or 404 for 'object' route, got {resp.status_code}: {resp.text[:200]}"
        )
        _assert_object_shape(resp.json(), path)

    else:
        raise ValueError(f"Unknown expected_shape {expected_shape!r} for {path}")


# ---------------------------------------------------------------------------
# Shape-map completeness self-check
# ---------------------------------------------------------------------------


def test_endpoint_shapes_map_has_no_duplicates():
    """No (method, path) pair should appear twice in ENDPOINT_SHAPES."""
    seen: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    for method, path, _ in ENDPOINT_SHAPES:
        key = (method, path)
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    assert not duplicates, f"Duplicate entries in ENDPOINT_SHAPES: {duplicates}"


def test_endpoint_shapes_map_covers_expected_count():
    """Smoke-check: the map covers ALL 44 GET routes enumerated by the Task 5.7 live-code audit.

    Count: 2 (cve) + 8 (fleet) + 16 (governance) + 8 (hosts) + 1 (iac)
           + 2 (ui) + 1 (vsphere) + 2 (vuln) + 4 (graph) = 44.
    (octopus.py's 3 routes were removed in P7.1a, D6/D11 — was 47.)

    This guards against accidental truncation of the map.  If a new GET route is
    added to any router file, this test will NOT catch it (re-grep is manual) — but
    it will catch removing entries from the map without a corresponding route removal.
    """
    count = len(ENDPOINT_SHAPES)
    assert count >= 44, (
        f"ENDPOINT_SHAPES covers {count} routes — expected at least 44 (full live-code audit count). "
        "Re-grep route decorators in all router files and add missing entries."
    )


def test_page_routes_count():
    """At least 30 routes in the map are 'page' shaped (Tasks 5.1-5.6 converted all collections).

    Exact count per router:
      cve: 1, fleet: 5, governance: 13, hosts: 5, ui: 1, vuln: 2, graph: 2 = 29
      (plus /audit_log) = 30.
      (octopus.py's 2 'page' routes were removed in P7.1a, D6/D11 — was 32.)
    """
    page_count = sum(1 for _, _, shape in ENDPOINT_SHAPES if shape == "page")
    assert page_count >= 30, (
        f"Only {page_count} 'page' routes found — expected at least 30 after Tasks 5.1-5.6 "
        "converted all bare-list collection routes."
    )


def test_list_routes_are_documented_exceptions_only():
    """Only the two documented bare-list exceptions should remain as 'list' shape.

    Task 5.3 spec: /software/vendors (flat string options list) and
    /drift_events/domains (flat string options list) are the only permitted
    'list'-shaped endpoints.  Any other 'list' entry is a missed conversion.
    """
    list_routes = [(m, p) for m, p, shape in ENDPOINT_SHAPES if shape == "list"]
    expected_exceptions = {
        ("GET", "/api/dashboard/software/vendors"),
        ("GET", "/api/dashboard/drift_events/domains"),
    }
    actual = {(m, p) for m, p in list_routes}
    unexpected = actual - expected_exceptions
    assert not unexpected, (
        "Unexpected 'list' routes found — these should have been converted to 'page' in Task 5.3:\n"
        + "\n".join(f"  {m} {p}" for m, p in sorted(unexpected))
    )
