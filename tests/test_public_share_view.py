"""A public share link must be openable by a logged-out recipient (TRK-306).

The backend builds a ``share_url`` for every custom view and exposes
``GET /api/dashboard/views/{share_token}``, whose handler was always written to
serve anonymous callers: it takes ``require_session_optional``, branches on
``is_public``, and only demands a session for private views.

None of that ever ran. ``ui_router`` carries a router-level
``dependencies=[Depends(require_session)]``, which fires BEFORE any handler body,
so every anonymous request got 401 and the share feature never worked end-to-end.
A live probe against the deployed host confirmed it: `GET .../views/<token>`
unauthenticated returned 401.

That is also why the frontend half of this (moving `/views/:token` outside
``AuthenticatedLayout``) was not sufficient on its own — the page loaded and then
its data fetch was refused.

These tests pin both halves of the contract, so the route can never be quietly
folded back onto the gated router:
  * a PUBLIC view is served to an anonymous caller
  * a PRIVATE view still 401s an anonymous caller (unchanged behaviour, now
    enforced in the handler body rather than one layer up)
"""

import uuid
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import CustomView

from tests.support.pg import make_engine


@pytest.fixture
def anon_client(monkeypatch):
    """A client with NO session cookie and dev-mode OFF, so require_session really gates."""
    from infra_brain.config import get_settings

    monkeypatch.delenv("INFRA_BRAIN_DEV", raising=False)
    monkeypatch.setenv("UI_COOKIE_SECRET", "x" * 64)
    get_settings.cache_clear()

    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    import infra_brain.api.routers.ui as ui

    monkeypatch.setattr(ui, "get_session", _get_session)

    app = FastAPI()
    app.include_router(ui.ui_router)
    app.include_router(ui.ui_public_router)

    def _seed(is_public: bool) -> str:
        token = uuid.uuid4().hex
        with Session(eng) as s:
            s.add(
                CustomView(
                    id=uuid.uuid4(),
                    user_id=None,
                    title="Shared",
                    prompt="p",
                    openui_lang="c",
                    share_token=token,
                    is_public=is_public,
                )
            )
            s.commit()
        return token

    client = TestClient(app)
    client.seed_view = _seed  # type: ignore[attr-defined]
    yield client
    get_settings.cache_clear()


def test_public_share_link_opens_for_a_logged_out_visitor(anon_client):
    """The case that was impossible before: no session, public view, 200."""
    token = anon_client.seed_view(is_public=True)

    resp = anon_client.get(f"/api/dashboard/views/{token}")

    assert resp.status_code == 200, (
        "a public share link must be openable by the logged-out recipient it was "
        f"generated for — got {resp.status_code} {resp.text[:120]}"
    )
    body = resp.json()
    assert body["is_public"] is True
    assert body["share_token"] == token
    assert body["share_url"] == f"/dashboard2/views/{token}"


def test_private_view_still_401s_an_anonymous_caller(anon_client):
    """Unchanged behaviour — the gate moved into the handler, it did not disappear."""
    token = anon_client.seed_view(is_public=False)

    resp = anon_client.get(f"/api/dashboard/views/{token}")

    assert resp.status_code == 401, (
        "splitting the route off the session-gated router must NOT expose private "
        f"views — got {resp.status_code}"
    )


def test_only_the_share_route_lives_on_the_unguarded_router():
    """Structural guard: ui_public_router must stay a single audited route.

    An APIRouter with no auth dependency is only safe while every route on it has
    been reasoned about individually. This fails the moment someone adds a second.
    """
    import infra_brain.api.routers.ui as ui

    paths = sorted({r.path for r in ui.ui_public_router.routes})
    assert paths == ["/api/dashboard/views/{share_token}"], (
        "ui_public_router has NO router-level auth gate. Only the public share "
        f"route belongs on it; found {paths}. Put new routes on ui_router."
    )
