"""Tests for CustomView ownership checks on PATCH/DELETE (L-5).

``view.user_id != user_id`` PASSES when both sides are ``None`` — a session
whose UIUser row was since deleted (cookie stays valid up to 1 day per
dashboard_auth._MAX_AGE) resolves to ``user_id=None`` and would then match
any NULL-owner view. These tests exercise that exact failure mode: a
caller authenticated as a *deleted* user hitting a NULL-owner view must get
403, not be allowed to mutate it.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from infra_brain.api.routers.ui import delete_custom_view, patch_custom_view
from infra_brain.api.schemas import CustomViewUpdate
from infra_brain.db.models import CustomView

from tests.support.pg import make_engine


MODULE = "infra_brain.api.routers.ui"


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def null_owner_view(engine):
    """A NULL-owner CustomView row (e.g. created anonymously, or whose
    original owner's UIUser row was later deleted)."""
    with Session(engine) as s:
        view = CustomView(
            user_id=None,
            title="orphaned view",
            prompt="show me hosts",
            openui_lang="<div>rendered</div>",
            share_token="orphantok01",
            is_public=False,
        )
        s.add(view)
        s.commit()
        s.refresh(view)
        vid = view.id
    return vid


def _session_ctx(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    return _get_session


def test_patch_custom_view_rejects_deleted_user_on_null_owner_view(engine, null_owner_view):
    """A caller whose UIUser row was deleted (current_user still returns a
    valid session payload from the signed cookie, but _get_user_id -> None)
    must NOT be able to patch a NULL-owner view."""
    with (
        patch(f"{MODULE}.get_session", _session_ctx(engine)),
        patch(f"{MODULE}.current_user", return_value={"username": "ghost-user"}),
    ):
        with pytest.raises(HTTPException) as exc_info:
            patch_custom_view(
                view_id=str(null_owner_view),
                body=CustomViewUpdate(title="hijacked"),
                request=None,
            )
    assert exc_info.value.status_code == 403

    with Session(engine) as s:
        view = s.get(CustomView, null_owner_view)
        assert view.title == "orphaned view"  # unchanged


def test_delete_custom_view_rejects_deleted_user_on_null_owner_view(engine, null_owner_view):
    """Same failure mode as above, for DELETE: a deleted-user session must
    not be able to delete a NULL-owner view."""
    with (
        patch(f"{MODULE}.get_session", _session_ctx(engine)),
        patch(f"{MODULE}.current_user", return_value={"username": "ghost-user"}),
    ):
        with pytest.raises(HTTPException) as exc_info:
            delete_custom_view(view_id=str(null_owner_view), request=None)
    assert exc_info.value.status_code == 403

    with Session(engine) as s:
        assert s.get(CustomView, null_owner_view) is not None  # not deleted
