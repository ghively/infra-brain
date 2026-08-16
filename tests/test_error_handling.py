"""TDD tests for Phase 1.1: remove failure-masking catch-all (main.py).

Behaviour under the new regime:
  - A non-DB error (KeyError, RuntimeError, …) raised inside a handler
    → 500, NO `items` key in body (not the old silent 200-mask).
  - A ProgrammingError in a bare-list route (response_model=list[DriftOut])
    → 200 with body [] (NOT {items:[]}).
  - A ProgrammingError in a PageOut route (response_model=ResourcePageOut)
    → 200 with {items:[], total:0, limit:0, offset:0, _degraded:True}.
  - HTTPException (404 / 401 / 422) → FastAPI's own handling, untouched.

Monkeypatch target: `infra_brain.dashboard_api.get_session`

Why this symbol?
  Every route handler opens its DB session with `with get_session() as s:`.
  The name `get_session` is imported at module level in `dashboard_api` from
  `infra_brain.db.session`, so patching `infra_brain.dashboard_api.get_session`
  replaces the callable the handler actually calls.  Returning a context manager
  that raises a DB error means the exception is thrown *inside* the handler body
  — the same path as a real DB fault — so the exception handler chain fires
  exactly as in production.

  Using `raise_server_exceptions=False` on the TestClient is required so that
  unhandled 500s are returned as HTTP responses rather than re-raised in the
  test process.
"""

from contextlib import contextmanager
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from infra_brain.api.routers.governance import governance_drift_router, governance_router
from infra_brain.api.routers.hosts import resources_router
from infra_brain.dashboard_api import router
from infra_brain.main import _register_db_error_handlers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(monkeypatch) -> TestClient:
    """Build a TestClient wired with the real dashboard router + error handlers.

    Not an auth test — exercises error-degradation behavior, so it needs the
    explicit dev-mode bypass (item 1.5b made dev-mode require INFRA_BRAIN_DEV=1
    rather than "no UI_COOKIE_SECRET set").
    """
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    app = FastAPI()
    _register_db_error_handlers(app)
    app.include_router(router)
    app.include_router(governance_drift_router)  # drift_events route lives here
    app.include_router(governance_router)
    app.include_router(resources_router)
    return TestClient(app, raise_server_exceptions=False)


@contextmanager
def _raising_session(exc: Exception):
    """Context manager that immediately raises *exc* — simulates a DB fault."""
    raise exc
    yield  # unreachable; needed to satisfy @contextmanager protocol


# ---------------------------------------------------------------------------
# Test 1: non-DB error → 500, no `items` key
# ---------------------------------------------------------------------------


def test_non_db_error_returns_500_without_items_mask(monkeypatch):
    """A KeyError (non-DB) in a handler must 500, NOT degrade to {items:[]}.

    Under the old catch-all the endpoint silently returned 200 {items:[]},
    masking real bugs.  Under the new regime any Exception that is not an
    OperationalError / ProgrammingError / HTTPException must produce a 500.
    """
    client = _make_client(monkeypatch)

    # /api/dashboard/drift_events calls get_session — inject a KeyError.
    with patch(
        "infra_brain.api.routers.governance_drift.get_session",
        side_effect=KeyError("injected non-db error"),
    ):
        resp = client.get("/api/dashboard/drift_events")

    assert resp.status_code == 500, (
        f"Expected 500 for a non-DB error, got {resp.status_code}. "
        "The catch-all may still be masking errors as 200."
    )
    # The default Starlette 500 returns text/plain "Internal Server Error" — it is
    # NOT the old failure-masking {items:[],_degraded:True} JSON shape.  Verify
    # that the response is not JSON or does not carry `items`.
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        body = resp.json()
        assert "items" not in body, (
            "The 500 body must NOT contain an `items` key — that would be the old "
            "failure-masking 200 shape leaking into a 500 response."
        )


# ---------------------------------------------------------------------------
# Test 2: ProgrammingError in a bare-list route → 200 []
# ---------------------------------------------------------------------------


def test_programming_error_in_bare_list_route_degrades_to_empty_list(monkeypatch):
    """/api/dashboard/drift_events was previously response_model=list[DriftOut].

    After Task 5.3, drift_events was migrated to DriftPageOut(PageEnvelope).
    A ProgrammingError must now degrade to HTTP 200 with the correct page shape:
    {items:[], total:0, limit:0, offset:0, _degraded:True} — NOT a bare array.
    This test is updated to reflect the new envelope shape.
    """
    client = _make_client(monkeypatch)

    db_error = ProgrammingError(
        "SELECT ...", {}, Exception("column drift_events.field does not exist")
    )

    with patch(
        "infra_brain.api.routers.governance_drift.get_session",
        return_value=_raising_session(db_error),
    ):
        resp = client.get("/api/dashboard/drift_events")

    assert resp.status_code == 200, (
        f"ProgrammingError on a DriftPageOut route should degrade to 200, got {resp.status_code}"
    )
    body = resp.json()
    # DriftPageOut is a PageEnvelope subclass — degraded body is the envelope shape.
    assert isinstance(body, dict), (
        f"Body must be a JSON object for a PageEnvelope route, got: {body!r}"
    )
    assert "items" in body, f"Degraded PageEnvelope body must have 'items', got: {body!r}"
    assert body["items"] == [], f"Degraded items must be empty, got: {body['items']!r}"
    assert body.get("_degraded") is True, f"Degraded body must carry _degraded=True: {body!r}"


# ---------------------------------------------------------------------------
# Test 3: ProgrammingError in a PageOut route → 200 {items:[], ...}
# ---------------------------------------------------------------------------


def test_programming_error_in_pageout_route_degrades_to_empty_page(monkeypatch):
    """/api/dashboard/resources has response_model=ResourcePageOut (ends with PageOut).

    A ProgrammingError must degrade to HTTP 200 with the correct page shape:
    {items:[], total:0, limit:0, offset:0, _degraded:True}.
    """
    client = _make_client(monkeypatch)

    db_error = ProgrammingError(
        "SELECT ...", {}, Exception("column resources.some_col does not exist")
    )

    with (
        patch(
            "infra_brain.dashboard_api.get_session",
            return_value=_raising_session(db_error),
        ),
        patch(
            "infra_brain.api.routers.hosts.get_session",
            return_value=_raising_session(db_error),
        ),
    ):
        resp = client.get("/api/dashboard/resources")

    assert resp.status_code == 200, (
        f"ProgrammingError on a PageOut route should degrade to 200, got {resp.status_code}"
    )
    body = resp.json()
    assert isinstance(body, dict), f"Body must be a dict for a PageOut route, got: {body!r}"
    assert "items" in body, f"Degraded PageOut body must have `items` key, got: {body!r}"
    assert body["items"] == [], f"Degraded items must be [], got: {body['items']!r}"
    assert body.get("total") == 0
    assert body.get("_degraded") is True
