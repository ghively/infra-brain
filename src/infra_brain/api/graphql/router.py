"""Mount the read-only GraphQL schema at ``POST /api/graphql``.

Session-gated exactly like every other dashboard router (CLAUDE.md
Architecture Constraint #1 note on `dashboard_auth.py` reuse) — no parallel
API-key/auth scheme is introduced. Dev-mode (``INFRA_BRAIN_DEV=1``) opens the
route the same way it opens every other dashboard route, via
``require_session``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from strawberry.dataloader import DataLoader
from strawberry.fastapi import GraphQLRouter

from infra_brain.api.graphql.schema import _batch_load_drift_events, schema
from infra_brain.dashboard_auth import require_session


async def _context_getter(request: Request) -> dict[str, Any]:
    """Fresh per-request context: a new DataLoader per request means batching
    happens within one GraphQL execution/event-loop tick, never leaking
    cached rows across unrelated requests."""
    return {
        "request": request,
        "drift_loader": DataLoader(load_fn=_batch_load_drift_events),
    }


graphql_router = GraphQLRouter(
    schema,
    context_getter=_context_getter,
    dependencies=[Depends(require_session)],
    prefix="/api/graphql",
)

__all__ = ["graphql_router"]
