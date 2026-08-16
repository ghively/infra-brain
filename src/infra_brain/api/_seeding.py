"""Single canonical Resource write path for HTTP, MCP, and collectors.

Task 4.4 (Phase 4): there were THREE divergent Resource-write helpers
(``agents/base.py::BaseAgent._upsert_resource``, ``api/_helpers.py::_seed_resource_row``,
``mcp_server.py::_seed_one_resource``) each with a different natural key and a
different metadata_ merge policy. ``upsert_resource`` is the ONE place that
creates/updates a ``resources`` row; every ingress routes through it.

Canonical decisions (see docs/... Phase 4 plan):
  * Natural key = ``(domain, resource_type, name)`` — the base-agent superset.
  * ``metadata_`` is always a shallow MERGE: ``{**(existing or {}), **new}``.
    (The old base-agent OVERWRITE behavior is intentionally dropped.)
  * ``zone`` is always enforced — defaults to ``ZONE_CORPORATE``, never ``None``.

Import rules: mirrors api/_helpers.py — may import from infra_brain.db.models,
infra_brain.db.session; must NOT import from infra_brain.dashboard_api or
infra_brain.mcp_server (would be circular).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from infra_brain.db.models import Resource, ZONE_CORPORATE

__all__ = ["upsert_resource"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upsert_resource(
    session: Any,
    *,
    name: str,
    domain: str,
    resource_type: str = "host",
    zone: str = ZONE_CORPORATE,
    metadata: dict | None = None,
    **cols: Any,
) -> Resource:
    """Create or update the ONE ``resources`` row for ``(domain, resource_type, name)``.

    On insert: sets all fields, including ``zone`` (never ``None``).
    On update: shallow-MERGES ``metadata_`` (``{**existing, **new}``) and applies
    any additional ``**cols`` (e.g. ``source``, ``last_seen``) onto the existing row.

    ``resource_type`` defaults to ``"host"`` to preserve the existing public seed
    request contract (``SeedResourceBody.resource_type: str = "host"``) — callers
    that always know their type (collectors) should pass it explicitly.
    """
    zone = zone or ZONE_CORPORATE
    new_meta = metadata or {}

    existing = (
        session.query(Resource).filter_by(domain=domain, type=resource_type, name=name).first()
    )

    if existing is not None:
        existing.last_seen = cols.pop("last_seen", None) or _now()
        existing.metadata_ = {**(existing.metadata_ or {}), **new_meta}
        for key, value in cols.items():
            if value is not None:
                setattr(existing, key, value)
        return existing

    resource = Resource(
        id=cols.pop("id", None) or uuid.uuid4(),
        domain=domain,
        type=resource_type,
        name=name,
        source=cols.pop("source", None) or "manual",
        zone=zone,
        last_seen=cols.pop("last_seen", None) or _now(),
        metadata_=new_meta,
    )
    for key, value in cols.items():
        if value is not None:
            setattr(resource, key, value)
    session.add(resource)
    session.flush()
    return resource
