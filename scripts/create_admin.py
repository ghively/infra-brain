"""Idempotent dashboard admin bootstrap.

Creates — or resets — a dashboard admin row in the ``ui_users`` table so the
gated FastAPI dashboard (``/dashboard``) can be logged into. This is the
fallback for an already-running stack where ``ADMIN_PASSWORD`` was never set,
so ``scripts/seed_db.py`` never seeded an admin and every login returns 401.

Reuses ``get_session()`` and the ``UIUser`` model, and mirrors the bcrypt /
``role="admin"`` / ``active=True`` pattern in ``seed_db.py``.

Idempotent: if the user already exists, its password, role, and active flag are
reset (not duplicated). Safe to re-run.

Usage (inside the app container of the live stack)::

    docker compose -p infra-brain -f docker/docker-compose.yml \
        -f docker/docker-compose.deploy.yml exec app \
        python scripts/create_admin.py --username admin --password '<pw>'

Credentials resolve from CLI args first, then ``ADMIN_USERNAME`` /
``ADMIN_PASSWORD`` (via settings). A password is required; an empty password is
rejected so we never create an unusable / trivially-guessable login.
"""

from __future__ import annotations

import argparse
import sys
import uuid


def upsert_admin(session, username: str, password: str) -> str:
    """Create or reset an admin ``UIUser``. Returns "created" or "reset".

    The caller owns the transaction commit so this is unit-testable against an
    in-memory session.
    """
    import bcrypt

    from infra_brain.db.models import UIUser

    if not password:
        raise ValueError("password must not be empty")

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    existing = session.query(UIUser).filter_by(username=username).one_or_none()
    if existing is None:
        session.add(
            UIUser(
                id=uuid.uuid4(),
                username=username,
                name=username,
                password_hash=pw_hash,
                role="admin",
                active=True,
            )
        )
        return "created"

    # Idempotent reset: re-hash the password and restore admin/active state.
    existing.password_hash = pw_hash
    existing.role = "admin"
    existing.active = True
    return "reset"


def main(argv: list[str] | None = None) -> int:
    from infra_brain.config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(description="Create or reset a dashboard admin user.")
    parser.add_argument(
        "--username",
        default=settings.admin_username,
        help="dashboard admin username (default: ADMIN_USERNAME / 'admin')",
    )
    parser.add_argument(
        "--password",
        default=settings.admin_password,
        help="dashboard admin password (default: ADMIN_PASSWORD env)",
    )
    args = parser.parse_args(argv)

    if not args.password:
        parser.error(
            "no password provided — pass --password '<pw>' or set ADMIN_PASSWORD in the environment"
        )

    from infra_brain.db.session import get_session

    with get_session() as session:
        action = upsert_admin(session, args.username, args.password)
        session.commit()

    print(f"{action} admin user: {args.username}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
