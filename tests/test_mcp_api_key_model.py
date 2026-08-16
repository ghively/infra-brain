"""McpApiKey ORM model — schema shape and defaults."""
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from infra_brain.db.models import McpApiKey

from tests.support.pg import make_engine


def _engine():
    eng = make_engine()
    return eng


def test_mcp_api_key_columns_and_defaults():
    eng = _engine()
    with Session(eng) as s:
        key = McpApiKey(
            name="infra-ops-prod-subnet-a",
            token_hash="a" * 64,
            allowed_tools=["query_resources", "get_drift_events"],
            created_by="operator",
        )
        s.add(key)
        s.commit()
        s.refresh(key)
        assert isinstance(key.id, uuid.UUID)
        assert key.allowed_tools == ["query_resources", "get_drift_events"]
        assert isinstance(key.created_at, datetime)
        assert key.revoked_at is None
        assert key.last_used_at is None
        # TRK-160: expiry is opt-in. A key created without one has NULL here,
        # which mcp_auth.is_expired reads as "never expires" — the property
        # that makes adding this column a non-breaking change for every key
        # minted before it existed.
        assert key.expires_at is None


def test_mcp_api_key_expires_at_is_nullable_and_roundtrips():
    from datetime import UTC, timedelta

    eng = _engine()
    when = datetime.now(UTC) + timedelta(days=30)
    with Session(eng) as s:
        key = McpApiKey(
            name="expiring",
            token_hash="c" * 64,
            allowed_tools=[],
            expires_at=when,
        )
        s.add(key)
        s.commit()
        s.refresh(key)
        assert key.expires_at is not None
        # SQLite drops the tzinfo on the way back out (PostgreSQL does not) —
        # exactly the naive/aware asymmetry mcp_auth._as_utc exists to absorb.
        # Compare on the wall-clock fields, which survive both dialects.
        assert key.expires_at.replace(tzinfo=None) == when.replace(tzinfo=None)


def test_mcp_api_key_token_hash_unique():
    import pytest
    from sqlalchemy.exc import IntegrityError

    eng = _engine()
    with Session(eng) as s:
        s.add(McpApiKey(name="k1", token_hash="b" * 64, allowed_tools=[]))
        s.commit()
        s.add(McpApiKey(name="k2", token_hash="b" * 64, allowed_tools=[]))
        with pytest.raises(IntegrityError):
            s.commit()
