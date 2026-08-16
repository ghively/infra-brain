"""Tests for the idempotent dashboard admin bootstrap (scripts/create_admin.py)."""

import bcrypt
import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import UIUser

from tests.support.pg import make_engine


# scripts/ is not a package; load the module by path.
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "create_admin",
    Path(__file__).resolve().parents[1] / "scripts" / "create_admin.py",
)
create_admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(create_admin)


@pytest.fixture
def session():
    eng = make_engine()
    with Session(eng) as s:
        yield s


def _get(session, username):
    return session.query(UIUser).filter_by(username=username).one()


def test_creates_new_admin(session):
    action = create_admin.upsert_admin(session, "admin", "s3cret")
    session.commit()

    assert action == "created"
    user = _get(session, "admin")
    assert user.role == "admin"
    assert user.active is True
    assert bcrypt.checkpw(b"s3cret", user.password_hash.encode())


def test_resets_existing_user_without_duplicating(session):
    # Pre-existing, de-activated, non-admin user with an old password.
    session.add(
        UIUser(
            username="admin",
            name="admin",
            password_hash=bcrypt.hashpw(b"old-pw", bcrypt.gensalt()).decode(),
            role="viewer",
            active=False,
        )
    )
    session.commit()

    action = create_admin.upsert_admin(session, "admin", "new-pw")
    session.commit()

    assert action == "reset"
    # Exactly one row — no duplicate.
    rows = session.query(UIUser).filter_by(username="admin").all()
    assert len(rows) == 1
    user = rows[0]
    # Password rotated, role/active restored.
    assert bcrypt.checkpw(b"new-pw", user.password_hash.encode())
    assert not bcrypt.checkpw(b"old-pw", user.password_hash.encode())
    assert user.role == "admin"
    assert user.active is True


def test_empty_password_rejected(session):
    with pytest.raises(ValueError, match="password must not be empty"):
        create_admin.upsert_admin(session, "admin", "")


def test_main_errors_without_password(monkeypatch, capsys):
    # No CLI password and no ADMIN_PASSWORD → argparse error (exit 2), no DB touch.
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    class _Settings:
        admin_username = "admin"
        admin_password = ""

    monkeypatch.setattr(create_admin, "get_settings", lambda: _Settings(), raising=False)
    # Ensure config import inside main() resolves to our stub.
    import infra_brain.config

    monkeypatch.setattr(infra_brain.config, "get_settings", lambda: _Settings())

    with pytest.raises(SystemExit) as exc:
        create_admin.main(["--username", "admin"])
    assert exc.value.code == 2
    assert "no password provided" in capsys.readouterr().err
