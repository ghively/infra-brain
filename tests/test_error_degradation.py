"""Tests for the DB-error degrade boundary in main.py.

Phase 1.1 regime (catch-all REMOVED):
  * OperationalError / ProgrammingError on a bare-list GET route
    → HTTP 200, body ``[]``
  * OperationalError / ProgrammingError on a PageOut GET route
    → HTTP 200, body ``{items:[],total:0,limit:0,offset:0,_degraded:True}``
  * OperationalError / ProgrammingError on a non-GET or unmodelled route
    → HTTP 503
  * HTTPException semantics (404) are preserved.
  * Non-DB exceptions (ValueError, KeyError) → HTTP 500 (no masking).
"""


from contextlib import contextmanager as _contextmanager
from unittest.mock import patch as _patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError

from infra_brain.api._envelope import PageEnvelope
from infra_brain.api.routers.vuln import vuln_router
from infra_brain.dashboard_auth import require_session
from infra_brain.main import _register_db_error_handlers

# ---------------------------------------------------------------------------
# Minimal Pydantic models that mimic real route shapes
# ---------------------------------------------------------------------------


class ItemOut(BaseModel):
    name: str


# Task 5.2: ItemPageOut now inherits PageEnvelope (the real marker) rather than
# BaseModel directly.  Previously, _degraded_body_for detected page routes via
# model_name.endswith("PageOut") — a string-suffix heuristic.  Detection now
# uses issubclass(response_model, PageEnvelope), so test fixtures must also
# inherit the marker to remain representative of the real route shape contract.
class ItemPageOut(PageEnvelope):
    items: list[ItemOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# App factory helpers
# ---------------------------------------------------------------------------


def _app_with_handlers() -> FastAPI:
    """Mini app with the real error handlers + representative routes.

    Routes intentionally carry explicit ``response_model`` annotations so that
    ``_degraded_body_for`` can inspect the shape — matching how the real
    dashboard router is configured.
    """
    app = FastAPI()
    _register_db_error_handlers(app)

    @app.get("/api/dashboard/list-route", response_model=list[ItemOut])
    def list_route():
        raise ProgrammingError("SELECT ...", {}, Exception("column x does not exist"))

    @app.get("/api/dashboard/page-route", response_model=ItemPageOut)
    def page_route():
        raise ProgrammingError("SELECT ...", {}, Exception("table y does not exist"))

    @app.post("/api/dashboard/mutate", response_model=list[ItemOut])
    def mutate():
        raise ProgrammingError("INSERT ...", {}, Exception("relation y does not exist"))

    @app.get("/api/dashboard/missing")
    def missing():
        raise HTTPException(status_code=404, detail="nope")

    @app.get("/api/dashboard/oops", response_model=list[ItemOut])
    def oops():
        raise ValueError("some unexpected bug")

    return app


def test_db_error_on_bare_list_route_degrades_to_empty_list():
    """ProgrammingError on a ``list[Model]`` route → 200 ``[]``."""
    client = TestClient(_app_with_handlers(), raise_server_exceptions=False)
    resp = client.get("/api/dashboard/list-route")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list), f"Expected JSON array, got: {body!r}"
    assert body == []


def test_db_error_on_pageout_route_degrades_to_empty_page():
    """ProgrammingError on a ``*PageOut`` route → 200 page-shaped body."""
    client = TestClient(_app_with_handlers(), raise_server_exceptions=False)
    resp = client.get("/api/dashboard/page-route")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert body.get("items") == []
    assert body.get("total") == 0
    assert body.get("_degraded") is True


def test_non_get_db_error_degrades_to_503():
    """POST request with a DB error → 503 (cannot determine page shape)."""
    client = TestClient(_app_with_handlers(), raise_server_exceptions=False)
    resp = client.post("/api/dashboard/mutate")
    assert resp.status_code == 503


def test_httpexception_is_preserved():
    """The DB error handlers must NOT swallow HTTPException (auth/not-found semantics)."""
    client = TestClient(_app_with_handlers(), raise_server_exceptions=False)
    resp = client.get("/api/dashboard/missing")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "nope"


def test_non_db_exception_returns_500():
    """A non-DB unhandled error (ValueError) must 500 — no silent masking.

    The old catch-all degraded these to 200 {items:[]}, hiding real bugs.
    Under the new regime only DB errors degrade; all other exceptions bubble
    as 500 so monitoring and alerting fire correctly.
    """
    client = TestClient(_app_with_handlers(), raise_server_exceptions=False)
    resp = client.get("/api/dashboard/oops")
    assert resp.status_code == 500, (
        f"Non-DB exception must 500, not be masked as {resp.status_code}. "
        "The catch-all may have been re-introduced."
    )


# ---------------------------------------------------------------------------
# Union response_model (RemediationPageOut | RemediationRollupOut) — TRK
# degraded-body fix. `/api/dashboard/remediation` declares a `types.UnionType`
# response_model (a real `X | Y` annotation, not `Optional[X]`); before the
# fix, `_degraded_body_for` couldn't build a fallback body for a union and
# fell through to a 503 for BOTH `view=detail` and `view=rollup`.
# ---------------------------------------------------------------------------




def _app_with_real_vuln_router() -> FastAPI:
    """Mini app mounting the REAL vuln_router (not a fixture stand-in).

    The union response_model lives on the real route declaration in
    ``api/routers/vuln.py``, so the fixture-model pattern used by the other
    tests in this file can't reproduce it — this test must exercise the
    actual router.
    """
    app = FastAPI()
    _register_db_error_handlers(app)
    app.include_router(vuln_router)
    app.dependency_overrides[require_session] = lambda: None
    return app


@_contextmanager
def _raising_session():
    """Stand-in for ``get_session()`` that raises on ``__enter__``.

    Simulates a DB-unavailable condition (e.g. table/column missing) the same
    way the other tests in this file do via ``ProgrammingError`` raised
    directly in the route body — here it's raised from the session context
    manager instead, since that's the real call site in ``vuln.py``.
    """
    raise ProgrammingError(
        "SELECT ...", {}, Exception("relation proposed_actions does not exist")
    )
    yield  # pragma: no cover - unreachable; keeps this a generator-based CM


def test_remediation_detail_view_degrades_on_db_error():
    """``view=detail`` (default) — union resolves via its first member.

    ``RemediationPageOut`` is listed first in
    ``RemediationPageOut | RemediationRollupOut``, matching the "normal"
    response for a client hitting the endpoint with no ``view=`` param.
    """
    client = TestClient(_app_with_real_vuln_router(), raise_server_exceptions=False)
    with _patch("infra_brain.api.routers.vuln.get_session", _raising_session):
        resp = client.get("/api/dashboard/remediation")
    assert resp.status_code == 200, (
        f"Expected degraded 200, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("items") == []
    assert body.get("total") == 0
    assert body.get("_degraded") is True


def test_remediation_rollup_view_degrades_on_db_error():
    """``view=rollup`` — same union, same degraded shape (not a 503)."""
    client = TestClient(_app_with_real_vuln_router(), raise_server_exceptions=False)
    with _patch("infra_brain.api.routers.vuln.get_session", _raising_session):
        resp = client.get("/api/dashboard/remediation?view=rollup")
    assert resp.status_code == 200, (
        f"Expected degraded 200, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("items") == []
    assert body.get("total") == 0
    assert body.get("_degraded") is True
