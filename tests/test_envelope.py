"""Tests for api/_envelope.py: PageEnvelope marker + paginate() helper.

TDD for Task 5.2 — Phase 5 contract rework.

Coverage:
  1. paginate() returns (items, total) where total is computed before slicing.
  2. _degraded_body_for() returns the shape-correct degraded body (not None)
     for the 4 non-"PageOut"-suffixed page classes: CveListOut, HostVulnsOut,
     FleetAssetsOut.  (HostsPageOut already ended in PageOut and worked before.)
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from infra_brain.api._envelope import PageEnvelope, paginate
from infra_brain.api.schemas import (
    CveListOut,
    EolPageOut,
    FleetAssetsOut,
    HostsPageOut,
    HostVulnsOut,
    ResourcePageOut,
    VulnPageOut,
)
from infra_brain.main import _degraded_body_for, _register_db_error_handlers


# ---------------------------------------------------------------------------
# paginate() helper tests
# ---------------------------------------------------------------------------


def _make_mock_query(all_rows: list, total_count: int) -> MagicMock:
    """Build a mock SQLAlchemy query that mimics count() + offset().limit().all()."""
    q = MagicMock()
    q.count.return_value = total_count

    # chain: q.offset(...).limit(...).all()
    mock_offset = MagicMock()
    mock_limited = MagicMock()
    mock_limited.all.return_value = all_rows
    mock_offset.limit.return_value = mock_limited
    q.offset.return_value = mock_offset

    return q


def test_paginate_returns_correct_items_and_total():
    """paginate() must call count() on the un-sliced query, then apply offset+limit."""
    rows = [object() for _ in range(3)]  # 3 rows returned for the page
    total = 20  # but 20 exist in DB

    q = _make_mock_query(rows, total)
    items, got_total = paginate(q, limit=3, offset=0)

    assert got_total == 20, "total must reflect the full DB count, not len(items)"
    assert items is rows, "items must be the sliced page from .offset().limit().all()"

    # Confirm count() was called on the un-sliced query
    q.count.assert_called_once()
    # Confirm offset and limit were applied in the right order
    q.offset.assert_called_once_with(0)
    q.offset.return_value.limit.assert_called_once_with(3)


def test_paginate_with_offset():
    """paginate() passes offset correctly to the query chain."""
    rows = [object(), object()]
    q = _make_mock_query(rows, total_count=10)
    items, total = paginate(q, limit=2, offset=5)

    assert total == 10
    assert len(items) == 2
    q.offset.assert_called_once_with(5)
    q.offset.return_value.limit.assert_called_once_with(2)


# ---------------------------------------------------------------------------
# M-3: paginate() must clamp caller-supplied limit/offset, not apply them
# verbatim. A huge limit must not be passed straight to SQL .limit(), and a
# negative offset must not be passed straight to SQL .offset() (Postgres
# raises on a negative OFFSET, which several routers were letting bubble up
# as a masked empty 200 rather than a real 4xx).
# ---------------------------------------------------------------------------


def test_paginate_clamps_huge_limit_before_hitting_the_query():
    """A caller-supplied limit of 100_000_000 must never reach .limit() verbatim.

    This is the literal M-3 failure mode: '?limit=100000000 materializes the
    whole audit log in one request'. paginate() must cap it at a sane max.
    """
    rows = [object() for _ in range(3)]
    q = _make_mock_query(rows, total_count=50_000_000)

    paginate(q, limit=100_000_000, offset=0)

    called_limit = q.offset.return_value.limit.call_args[0][0]
    assert called_limit <= 1000, (
        f"paginate() passed an unclamped limit ({called_limit}) straight through to "
        "the query — a huge ?limit= would materialize the whole table"
    )
    assert called_limit >= 1, "clamped limit must still be a usable positive page size"


def test_paginate_clamps_negative_offset_to_zero():
    """A negative offset must never reach .offset() verbatim.

    Postgres raises on OFFSET < 0; letting that bubble up surfaces (per M-3)
    as a masked empty 200 rather than a clamped, correct first page.
    """
    rows = [object(), object()]
    q = _make_mock_query(rows, total_count=10)

    paginate(q, limit=10, offset=-5)

    called_offset = q.offset.call_args[0][0]
    assert called_offset >= 0, (
        f"paginate() passed a negative offset ({called_offset}) straight through to "
        "the query — Postgres raises on OFFSET < 0"
    )


# ---------------------------------------------------------------------------
# PageEnvelope marker tests
# ---------------------------------------------------------------------------


def test_page_envelope_is_base_class_of_retrofitted_classes():
    """All 8 retrofitted page classes must inherit from PageEnvelope."""
    from infra_brain.api.schemas import (
        AuditPageOut,
        EolPageOut,
        FleetAssetsOut,
        HostsPageOut,
        HostVulnsOut,
        ResourcePageOut,
        VulnPageOut,
    )

    classes = [
        ResourcePageOut,
        VulnPageOut,
        AuditPageOut,
        EolPageOut,
        HostsPageOut,
        CveListOut,
        HostVulnsOut,
        FleetAssetsOut,
    ]
    for cls in classes:
        assert issubclass(cls, PageEnvelope), f"{cls.__name__} must inherit from PageEnvelope"


def test_software_inventory_out_inherits_page_envelope():
    """Task 5.4: SoftwareInventoryOut now inherits PageEnvelope (aggregated/detail → items)."""
    from infra_brain.api.schemas import SoftwareInventoryOut

    assert issubclass(SoftwareInventoryOut, PageEnvelope), (
        "SoftwareInventoryOut must inherit PageEnvelope after Task 5.4 (items field replaces aggregated/detail)"
    )
    # Confirm items field exists and aggregated/detail fields are gone
    fields = SoftwareInventoryOut.model_fields
    assert "items" in fields, "SoftwareInventoryOut must have 'items' field"
    assert "aggregated" not in fields, "SoftwareInventoryOut must NOT have 'aggregated' field"
    assert "detail" not in fields, "SoftwareInventoryOut must NOT have 'detail' field"
    assert "view" in fields, "SoftwareInventoryOut must retain 'view' sidecar field"
    assert "summary" in fields, "SoftwareInventoryOut must retain 'summary' sidecar field"


# ---------------------------------------------------------------------------
# _degraded_body_for() — direct unit tests (no HTTP round-trip needed)
# ---------------------------------------------------------------------------


def _make_request_with_model(response_model):
    """Build a minimal mock Request whose scope contains a route with response_model."""
    mock_route = MagicMock()
    mock_route.response_model = response_model

    # scope must be a MagicMock (not a real dict) so we can control .get()
    mock_scope = MagicMock()
    mock_scope.get = lambda key, default=None: mock_route if key == "route" else default

    mock_request = MagicMock()
    mock_request.method = "GET"
    mock_request.scope = mock_scope

    return mock_request


@pytest.mark.parametrize(
    "model_cls",
    [
        ResourcePageOut,  # ends in PageOut — was already working
        VulnPageOut,  # ends in PageOut — was already working
        HostsPageOut,  # ends in PageOut — was already working
        EolPageOut,  # ends in PageOut — was already working
        CveListOut,  # does NOT end in PageOut — was broken before 5.2
        FleetAssetsOut,  # does NOT end in PageOut — was broken before 5.2
        HostVulnsOut,  # does NOT end in PageOut — was broken before 5.2
    ],
)
def test_degraded_body_for_returns_page_shape_for_all_page_classes(model_cls):
    """_degraded_body_for must return a non-None page-shaped body for all
    PageEnvelope subclasses, including the 3 that lack the 'PageOut' suffix."""
    req = _make_request_with_model(model_cls)
    body = _degraded_body_for(req)

    assert body is not None, (
        f"_degraded_body_for returned None for {model_cls.__name__}; "
        "expected a page-shaped degraded body"
    )
    assert isinstance(body, dict), f"Expected dict, got {type(body)} for {model_cls.__name__}"
    assert body.get("items") == [], f"items must be [] in degraded body for {model_cls.__name__}"
    assert body.get("total") == 0, f"total must be 0 in degraded body for {model_cls.__name__}"
    assert body.get("_degraded") is True, f"_degraded must be True for {model_cls.__name__}"


def test_degraded_body_for_bare_list_route_returns_empty_list():
    """Bare list[X] routes must still degrade to []."""
    from infra_brain.api.schemas import ResourceOut

    req = _make_request_with_model(list[ResourceOut])
    body = _degraded_body_for(req)
    assert body == []


def test_degraded_body_for_non_page_object_returns_none():
    """Detail/object endpoints must still return None (→ 503)."""
    from infra_brain.api.schemas import ResourceOut

    req = _make_request_with_model(ResourceOut)
    body = _degraded_body_for(req)
    assert body is None


# ---------------------------------------------------------------------------
# Integration: TestClient confirms HTTP-level behaviour for non-suffixed classes
# ---------------------------------------------------------------------------


def _make_app_with_cve_route():
    """Mini FastAPI app that raises ProgrammingError on the CveListOut route."""
    app = FastAPI()
    _register_db_error_handlers(app)

    @app.get("/cves", response_model=CveListOut)
    def cve_list():
        raise ProgrammingError("SELECT ...", {}, Exception("table missing"))

    return app


def _make_app_with_fleet_route():
    app = FastAPI()
    _register_db_error_handlers(app)

    @app.get("/fleet", response_model=FleetAssetsOut)
    def fleet_list():
        raise ProgrammingError("SELECT ...", {}, Exception("table missing"))

    return app


def _make_app_with_hostvulns_route():
    app = FastAPI()
    _register_db_error_handlers(app)

    @app.get("/host-vulns", response_model=HostVulnsOut)
    def host_vulns():
        raise ProgrammingError("SELECT ...", {}, Exception("table missing"))

    return app


def test_cve_list_out_db_error_degrades_to_page_shape():
    """CveListOut route must degrade to page envelope on DB error, not 503."""
    client = TestClient(_make_app_with_cve_route(), raise_server_exceptions=False)
    resp = client.get("/cves")
    assert resp.status_code == 200, f"Expected 200 degraded, got {resp.status_code}"
    body = resp.json()
    assert isinstance(body, dict)
    assert body.get("items") == []
    assert body.get("total") == 0
    assert body.get("_degraded") is True


def test_fleet_assets_out_db_error_degrades_to_page_shape():
    """FleetAssetsOut route must degrade to page envelope on DB error, not 503."""
    client = TestClient(_make_app_with_fleet_route(), raise_server_exceptions=False)
    resp = client.get("/fleet")
    assert resp.status_code == 200, f"Expected 200 degraded, got {resp.status_code}"
    body = resp.json()
    assert isinstance(body, dict)
    assert body.get("items") == []
    assert body.get("_degraded") is True


def test_host_vulns_out_db_error_degrades_to_page_shape():
    """HostVulnsOut route must degrade to page envelope on DB error, not 503."""
    client = TestClient(_make_app_with_hostvulns_route(), raise_server_exceptions=False)
    resp = client.get("/host-vulns")
    assert resp.status_code == 200, f"Expected 200 degraded, got {resp.status_code}"
    body = resp.json()
    assert isinstance(body, dict)
    assert body.get("items") == []
    assert body.get("_degraded") is True
