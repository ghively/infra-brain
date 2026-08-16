"""infra_brain.api.routers.sources — the source inventory the dashboard navigates by.

WHAT THIS IS FOR
----------------
The sidebar used to be a hardcoded list of pages. That list was written for the
enterprise deployment this codebase came from, so it offered Rapid7, vSphere,
Octopus, AWS and Kubernetes to a homelab operator who has none of them and never
will — while giving no signal at all about which sources *are* real. Worse, it
could only ever be as current as the last person who remembered to edit it.

This endpoint replaces that list with a derivation. It answers, per collector
domain: is it live or retired, is it configured, how much data does it hold, and
therefore — via the single rule in ``api/source_visibility.py`` — should the UI
show it, show it as historical, or hide it.

NO HARDCODED DOMAIN LIST. The roster comes from ``etl.spec.agent_specs()``
(i.e. ``AGENT_REGISTRY``), retirement from ``etl.spec.retired_domains()``, and
the counts from the database. Register a collector and it shows up here, and in
the sidebar, with nothing else edited. That property is the entire point; do not
"optimise" it into a literal.

THIS ENDPOINT DOES NOT CHANGE ANY STATE. Retiring or reviving a collector is
``AgentSpec.retired`` plus ``COLLECTION_REVIVED_DOMAINS`` (see
``etl.spec.retired_domains``). This is the UI *reflecting* that state, never
setting it — there is deliberately no write route in this module.

Import rules (same as the sibling routers):
  * MAY import from infra_brain.api.schemas / _helpers / source_visibility,
    infra_brain.db.models, infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api or from another router module.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func

# `missing_requirements` reads the shared `_DOMAIN_REQUIREMENTS` table, so
# `configured` here and the Agent Config page's `ready` flag can never disagree
# about what a domain needs — there is one copy, in `api/_helpers`.
from infra_brain.api._helpers import missing_requirements
from infra_brain.api.schemas import SourceInventoryPageOut, SourceOut
from infra_brain.api.source_visibility import SourceVisibility, source_visibility, visibility_reason
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import CollectionRun, Resource
from infra_brain.db.session import get_session

sources_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)


@sources_router.get("/sources", response_model=SourceInventoryPageOut)
def list_sources(limit: int = 500, offset: int = 0) -> SourceInventoryPageOut:
    """Per-domain source inventory + the UI visibility verdict for each.

    Sync ``def`` on purpose, matching every sibling router: `get_session()` is a
    sync SQLAlchemy session, so FastAPI runs this handler in its worker
    threadpool and the event loop is never blocked. Declaring it ``async`` would
    be the actual bug (see CLAUDE.md, "No sync invoke() inside FastAPI routes").

    THREE AGGREGATE QUERIES, NOT N+1. The obvious shape here — loop the ~46
    domains, run "latest run for this domain" per iteration — is what
    `list_agents` does and it costs a query per domain. This is nav chrome
    fetched on every page load, so it does three GROUP BYs over the whole table
    instead and joins them in Python.

    `last_status` is deliberately absent: getting "the status of the most recent
    run" needs either a window function (not uniformly available on the sqlite
    the suite runs against) or the per-domain query this is avoiding — and the
    caller is a sidebar that needs "does this have data / did it ever succeed",
    which `resource_count` and `last_success_at` already answer. The Agents page
    (`GET /api/dashboard/agents`) is the surface for per-run status detail.
    """
    from infra_brain.config import get_settings  # noqa: PLC0415
    from infra_brain.etl.spec import agent_specs, retired_domains  # noqa: PLC0415

    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    specs = agent_specs()
    retired = retired_domains()
    settings = get_settings()

    with get_session() as s:
        counts: dict[str, int] = dict(
            s.query(Resource.domain, func.count(Resource.id)).group_by(Resource.domain).all()
        )
        last_run: dict[str, object] = dict(
            s.query(CollectionRun.domain, func.max(CollectionRun.started_at))
            .group_by(CollectionRun.domain)
            .all()
        )
        last_success: dict[str, object] = dict(
            s.query(CollectionRun.domain, func.max(CollectionRun.started_at))
            .filter(CollectionRun.status == "completed")
            .group_by(CollectionRun.domain)
            .all()
        )

    items: list[SourceOut] = []
    by_visibility: dict[str, int] = {v.value: 0 for v in SourceVisibility}

    for domain in sorted(specs):
        is_retired = domain in retired
        row_count = int(counts.get(domain, 0))
        verdict = source_visibility(retired=is_retired, row_count=row_count)
        by_visibility[verdict.value] += 1
        missing = missing_requirements(domain, settings)
        items.append(
            SourceOut(
                domain=domain,
                retired=is_retired,
                live=not is_retired,
                # A domain that declares no requirements is configured by
                # definition — most collectors read no credentials at all, and
                # reporting them as "unconfigured" would make the field useless.
                configured=not missing,
                missing_config=missing,
                resource_count=row_count,
                last_run_at=last_run.get(domain),  # type: ignore[arg-type]
                last_success_at=last_success.get(domain),  # type: ignore[arg-type]
                visibility=verdict.value,
                visibility_reason=visibility_reason(retired=is_retired, row_count=row_count),
            )
        )

    total = len(items)
    return SourceInventoryPageOut(
        items=items[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
        # Whole-inventory counts, not page counts: the client uses these for a
        # "6 sources hidden" affordance, which a paged subtotal would misstate.
        by_visibility=by_visibility,
    )
