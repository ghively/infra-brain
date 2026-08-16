"""Strawberry GraphQL schema — query-only.

GitLab issue #114: a read-only GraphQL layer over the knowledge-graph-shaped
ORM (Resource -> Snapshot/DriftEvent/HostIdentity joins) for flexible
external querying, similar in spirit to how Backstage exposes its service
catalog. Reuses the same ORM reads the REST routers under
``api/routers/hosts.py`` and ``api/routers/governance.py`` already perform —
no new query logic, no raw SQL.

Read-only guarantee: this module defines a ``Query`` type ONLY. No
``Mutation`` type is defined anywhere in ``api/graphql/``, so there is no
GraphQL write path to gate — the absence of a mutation type IS the
structural enforcement (mirrors the "structural" layer in
docs/READONLY-MODEL.md: collectors hold GET-only clients; this surface holds
no mutation resolvers at all).

Async correctness (CLAUDE.md Architecture Constraint #2 — no sync
``invoke()``/DB call inside a FastAPI route path): every resolver is
``async`` and offloads the synchronous SQLAlchemy ORM read via
``asyncio.to_thread`` so the event loop is never blocked by a DB call.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

import strawberry
from strawberry.dataloader import DataLoader

from infra_brain.db.models import (
    ComplianceViolation as _ComplianceViolation,
)
from infra_brain.db.models import (
    DriftEvent as _DriftEvent,
)
from infra_brain.db.models import (
    EolRegistry as _EolRegistry,
)
from infra_brain.db.models import (
    HostIdentity as _HostIdentity,
)
from infra_brain.db.models import (
    Resource as _Resource,
)
from infra_brain.db.session import get_session

# Matches the REST routers' cap (api/routers/hosts.py::list_resources).
_MAX_LIMIT = 1000


@strawberry.type
class DriftEvent:
    id: str
    drift_type: str
    field: str
    status: str
    detected_at: datetime

    @classmethod
    def from_orm(cls, row: _DriftEvent) -> "DriftEvent":
        return cls(
            id=str(row.id),
            drift_type=row.drift_type,
            field=row.field,
            status=row.status,
            detected_at=row.detected_at,
        )


def _load_drift_events_by_resource(resource_ids: list[str]) -> dict[str, list[_DriftEvent]]:
    """Sync batch-load, run inside asyncio.to_thread by the DataLoader's load_fn.

    Single ``resource_id IN (...)`` query for however many resource ids the
    DataLoader has batched together within one event-loop tick — this is the
    N+1 fix (Task 5 of the implementation plan): N resources previously meant
    N separate DriftEvent queries, now it's one. ``resource_ids`` are the
    string forms of the GraphQL ``Resource.id`` field; cast back to UUID for
    the ORM's typed primary-key column.
    """
    import uuid as _uuid  # noqa: PLC0415

    uuids = [_uuid.UUID(str(i)) for i in resource_ids]
    by_resource: dict[str, list[_DriftEvent]] = {str(i): [] for i in uuids}
    with get_session() as s:
        rows = s.query(_DriftEvent).filter(_DriftEvent.resource_id.in_(uuids)).all()
        for row in rows:
            by_resource.setdefault(str(row.resource_id), []).append(row)
    return by_resource


async def _batch_load_drift_events(resource_ids: list[str]) -> list[list[_DriftEvent]]:
    by_resource = await asyncio.to_thread(_load_drift_events_by_resource, resource_ids)
    return [by_resource.get(str(rid), []) for rid in resource_ids]


@strawberry.type
class Resource:
    id: str
    domain: str
    type: str
    name: str
    zone: str
    last_seen: datetime

    @strawberry.field
    async def drift_events(self, info: strawberry.Info) -> list[DriftEvent]:
        loader: DataLoader = info.context["drift_loader"]
        rows = await loader.load(self.id)
        return [DriftEvent.from_orm(r) for r in rows]

    @classmethod
    def from_orm(cls, row: _Resource) -> "Resource":
        return cls(
            id=str(row.id),
            domain=row.domain,
            type=row.type,
            name=row.name,
            zone=row.zone,
            last_seen=row.last_seen,
        )


@strawberry.type
class HostIdentity:
    id: str
    short_hostname: str
    fqdn: Optional[str]

    @classmethod
    def from_orm(cls, row: _HostIdentity) -> "HostIdentity":
        return cls(id=str(row.id), short_hostname=row.short_hostname, fqdn=row.fqdn)


@strawberry.type
class ComplianceViolationType:
    id: str
    rule: str
    severity: str
    host: str
    status: str
    detected_at: datetime

    @classmethod
    def from_orm(cls, row: _ComplianceViolation) -> "ComplianceViolationType":
        return cls(
            id=str(row.id),
            rule=row.rule,
            severity=row.severity,
            host=row.host,
            status=row.status,
            detected_at=row.detected_at,
        )


@strawberry.type
class EolEntry:
    id: str
    asset_name: str
    eol_date: Optional[datetime]
    pci_risk_score: Optional[int]

    @classmethod
    def from_orm(cls, row: _EolRegistry) -> "EolEntry":
        return cls(
            id=str(row.id),
            asset_name=row.asset_name,
            eol_date=row.eol_date,
            pci_risk_score=row.pci_risk_score,
        )


def _query_resources(domain: Optional[str], limit: int) -> list[_Resource]:
    with get_session() as s:
        q = s.query(_Resource)
        if domain:
            q = q.filter(_Resource.domain == domain)
        # Clamp both bounds: a negative/zero ``limit`` (e.g. GraphQL
        # ``resources(limit: -1)``) would otherwise pass straight through to
        # PostgreSQL's LIMIT clause, which rejects negative values with a 500
        # instead of degrading gracefully.
        safe_limit = max(1, min(limit, _MAX_LIMIT))
        return q.order_by(_Resource.last_seen.desc()).limit(safe_limit).all()


def _query_hosts(q: Optional[str]) -> list[_HostIdentity]:
    with get_session() as s:
        query = s.query(_HostIdentity)
        if q:
            query = query.filter(_HostIdentity.short_hostname.ilike(f"%{q}%"))
        return query.order_by(_HostIdentity.short_hostname.asc()).limit(_MAX_LIMIT).all()


def _query_compliance_violations(status: Optional[str]) -> list[_ComplianceViolation]:
    with get_session() as s:
        query = s.query(_ComplianceViolation)
        if status:
            query = query.filter(_ComplianceViolation.status == status)
        return query.order_by(_ComplianceViolation.detected_at.desc()).limit(_MAX_LIMIT).all()


def _query_eol(overdue_only: bool) -> list[_EolRegistry]:
    with get_session() as s:
        query = s.query(_EolRegistry)
        if overdue_only:
            from infra_brain.api._helpers import _now  # noqa: PLC0415

            query = query.filter(_EolRegistry.eol_date.isnot(None), _EolRegistry.eol_date < _now())
        return query.order_by(_EolRegistry.eol_date.asc().nullslast()).limit(_MAX_LIMIT).all()


@strawberry.type
class Query:
    @strawberry.field
    def health(self) -> str:
        return "ok"

    @strawberry.field
    async def resources(self, domain: Optional[str] = None, limit: int = 200) -> list[Resource]:
        rows = await asyncio.to_thread(_query_resources, domain, limit)
        return [Resource.from_orm(r) for r in rows]

    @strawberry.field
    async def hosts(self, q: Optional[str] = None) -> list[HostIdentity]:
        rows = await asyncio.to_thread(_query_hosts, q)
        return [HostIdentity.from_orm(r) for r in rows]

    @strawberry.field
    async def compliance_violations(
        self, status: Optional[str] = None
    ) -> list[ComplianceViolationType]:
        rows = await asyncio.to_thread(_query_compliance_violations, status)
        return [ComplianceViolationType.from_orm(r) for r in rows]

    @strawberry.field
    async def eol(self, overdue_only: bool = False) -> list[EolEntry]:
        rows = await asyncio.to_thread(_query_eol, overdue_only)
        return [EolEntry.from_orm(r) for r in rows]


# No Mutation type is defined or passed below — this is the structural
# read-only guarantee for the surface (see module docstring / test_schema.py).
schema = strawberry.Schema(query=Query)
