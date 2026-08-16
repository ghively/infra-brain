"""Shared base objects for all db/models bucket files.

Provides:
- ``Base``  — re-imported from ``infra_brain.db.base`` (avoids circular imports)
- ``JSONB`` — cross-dialect JSON column type (JSONB on PostgreSQL, JSON/TEXT on SQLite)
- ``_now``  — timezone-aware UTC datetime factory
- ``_uuid`` — UUID4 factory
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

# Cross-dialect JSON type: renders as JSONB on PostgreSQL, JSON/TEXT on SQLite.
# Uses SQLAlchemy's with_variant to map the core JSON type to JSONB when the
# dialect is postgresql, preserving all JSONB operator support in production.
JSONB = JSON().with_variant(_PG_JSONB(), "postgresql")

# Base is defined in base.py to avoid circular imports with relationships.py.
from infra_brain.db.base import Base  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


__all__ = ["Base", "JSONB", "_now", "_uuid"]
