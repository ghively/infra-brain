import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import httpx

from infra_brain.db.models import CiPipelineRun, GitlabProject, Resource
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectOutcome, ETLConnector
from infra_brain.pool_metrics import observe_pool
from infra_brain.tools.gitlab import gitlab_get, gitlab_get_paginated
from infra_brain.etl.spec import AgentSpec, NodeSpec, Tier

logger = logging.getLogger(__name__)

_PIPELINE_WORKERS = 10


def _classify_gitlab_error(exc: BaseException) -> str:
    """Classify a failed GitLab API call into an operator-facing failure reason.

    TRK-191: a 403 (token lacks the project role needed for ``/pipelines``) was
    previously lumped into the same generic "pipeline fetch failed" bucket as a
    429/timeout/5xx, so an operator reading ``CollectionRun.error_message`` (the
    dashboard's partial-failure detail — see ``etl/base.py``'s ``scrub_dsn``
    docstring) couldn't tell "this needs a token permission fix" apart from
    "this will probably resolve itself on retry" without reading raw logs.

    Mirrors the transient/permanent status-code boundaries every other
    collector's ``_is_transient_http`` retry predicate already uses (see
    ``tools/gitlab.py``, ``tools/octopus_tool.py``, ``tools/rapid7.py``, etc.)
    — this reuses that same classification but records *why*, for display,
    instead of only deciding whether to retry.

    Returns one of: "forbidden" (403 — permanent, needs a token/role fix),
    "rate_limited" (429 — transient), "timeout" (transient), "server_error"
    (5xx — transient), "http_error" (some other 4xx — permanent), or
    "unknown" (non-HTTP exception, e.g. a connection error).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 403:
            return "forbidden"
        if status == 429:
            return "rate_limited"
        if status >= 500:
            return "server_error"
        return "http_error"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return "unknown"


def _parse_dt(value) -> datetime | None:
    """Parse a GitLab ISO-8601 timestamp into an aware datetime, or None.

    GitLab emits e.g. ``2026-06-18T00:00:00.000Z``; the trailing ``Z`` is
    normalized to ``+00:00`` for ``fromisoformat``.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fetch_project_item(project: dict) -> dict:
    """Fetch a project's recent pipelines and build the generic Resource item.

    The full GitLab project dict and the recent pipelines list are stashed on the
    item under ``_project`` / ``_pipelines`` so ``run()`` can write the typed
    detail rows without re-fetching. ``collect()`` strips these private keys back
    out before returning the generic items (BaseAgent only wants name/type/data).
    """
    # #77: the initial pipeline-listing call is NOT wrapped in a try/except here
    # anymore. Letting it propagate means collect()'s existing per-project
    # error handler (as_completed loop) logs a warning AND appends to errors
    # (F-007's documented contract), producing status="partial" instead of a
    # silently-succeeding "unknown" status row.
    pipelines = gitlab_get(f"/api/v4/projects/{project['id']}/pipelines?per_page=5")
    if not isinstance(pipelines, list):
        pipelines = []
    # The pipelines list endpoint returns a summary (id/iid/ref/sha/status/
    # web_url/source) but omits created_at/updated_at/duration. Fetch the
    # single-pipeline detail per recent pipeline to fill the typed columns;
    # on failure keep the summary row so the run still records something.
    detailed: list[dict] = []
    for p in pipelines:
        if not isinstance(p, dict) or p.get("id") is None:
            continue
        try:
            full = gitlab_get(f"/api/v4/projects/{project['id']}/pipelines/{p['id']}")
            detailed.append(full if isinstance(full, dict) else p)
        except Exception:
            detailed.append(p)
    pipelines = detailed
    last_pipeline = pipelines[0] if pipelines else {}

    return {
        # L-8b: the generic Resource identity (domain, type, name) that
        # run()'s base upsert keys on used to be the BARE project name —
        # two same-named projects in different GitLab groups/subgroups
        # (a routine layout, e.g. "team-a/backend" and "team-b/backend")
        # collapsed into one Resource row, silently overwriting each
        # other's snapshot every run. ``path_with_namespace`` is unique
        # per GitLab instance, so key on that when present; fall back to
        # the bare name only for older/incomplete API shapes that omit
        # it (still collision-prone in that fallback case, but no worse
        # than before). NOTE: this changes resource identity for every
        # existing gitlab_project Resource row — the next sweep after
        # this deploys will show every project as a "new" resource (old
        # bare-name row orphaned, new path-qualified row created), which
        # is expected one-time churn, not real infra drift.
        "name": project.get("path_with_namespace") or project["name"],
        "type": "gitlab_project",
        "data": {
            "project_id": project["id"],
            "default_branch": project.get("default_branch", ""),
            "last_activity_at": project.get("last_activity_at", ""),
            "last_pipeline_status": last_pipeline.get("status", "unknown"),
            "last_pipeline_ref": last_pipeline.get("ref", ""),
            "last_pipeline_id": last_pipeline.get("id"),
        },
        # Private payload for run()'s detail-write phase (stripped by collect()).
        "_project": project,
        "_pipelines": pipelines,
    }


class CICDAgent(ETLConnector):
    spec = AgentSpec(
        domain="cicd",
        tier=Tier.COLLECTOR,
        schedule="5 2 * * *",
        max_staleness=timedelta(hours=26),
        # P2 of docs/decisions/2026-08-11-graph-first-architecture.md: cicd owns
        # the GitLab project ENTITY. It declares no edges of its own — the
        # relationship that needs this node (iac's BELONGS_TO) is declared by
        # iac, which is the side that knows about it. A collector may only emit
        # edges out of entities it owns, so contributing a target for someone
        # else's edge is exactly this shape: nodes, no edges.
        emits_nodes=(
            NodeSpec(
                type="GitlabProject",
                resource_type="gitlab_project",
                # THE NUMERIC ID, NOT THE NAME, and the difference is
                # load-bearing rather than stylistic. ``resources.name`` is
                # ``path_with_namespace``, which is unique but MUTABLE — moving
                # a repo between groups renames it, and cicd's L-8b change
                # already renamed every project row once. Keying the node on the
                # name means a rename mints a SECOND GitlabProject node carrying
                # the same ``project_id``; the old one is never retired (node
                # retirement is deliberately deferred — see graph_engine's
                # "WHAT IT DOES NOT DO YET"), so the BELONGS_TO target index
                # would then see one key claimed by two nodes, correctly refuse
                # to guess, and drop EVERY BELONGS_TO edge for that project.
                # GitLab's numeric id never changes, so a rename updates this
                # node in place instead. ``metadata.project_id`` is what
                # ``resources`` actually carries (see _project_item).
                natural_key="metadata.project_id",
                # Display name stays the human-readable path.
                name="name",
                # ``project_id`` also rides along as an attribute so a reader of
                # the node does not have to know that the natural key happens to
                # be it.
                attributes=("project_id", "default_branch", "last_pipeline_status"),
            ),
        ),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        # Total listing failure raises -> BaseAgent.run() records status="failed".
        # Per-project fetch failures (e.g. 403 Forbidden) are recorded in
        # errors -> status="partial" (F-007: never silent-drop projects).
        errors: list[str] = []
        self._last_items = []
        projects = gitlab_get_paginated("/api/v4/projects?membership=true")
        raw_items: list[dict] = []
        # TRK-083: record queue depth / worker bound before fan-out (additive).
        observe_pool("cicd", "pipelines", len(projects), _PIPELINE_WORKERS)
        with ThreadPoolExecutor(max_workers=_PIPELINE_WORKERS) as pool:
            futures = {pool.submit(_fetch_project_item, p): p for p in projects}
            for future in as_completed(futures):
                try:
                    raw_items.append(future.result())
                except Exception as exc:
                    project = futures[future]
                    reason = _classify_gitlab_error(exc)
                    # TRK-191: reason= is embedded in the message text (there is no
                    # separate structured failure_reason column on CollectionRun)
                    # so it rides the SAME surfacing path every other collector
                    # already uses for partial-failure detail — CollectionRun.
                    # error_message, returned as-is by get_collection_health() and
                    # rendered verbatim in the dashboard's CollRuns error snippet/
                    # drawer (dashboard-app/src/pages/CollRuns.tsx). "reason=forbidden"
                    # is the operator's signal to fix the token's project role
                    # rather than wait for a retry.
                    msg = (
                        f"pipeline fetch failed for project {project.get('name')} "
                        f"[reason={reason}]: {exc}"
                    )
                    logger.warning("CICDAgent: %s", msg)
                    errors.append(msg)
        # Cache the full payload for run()'s detail-write phase, then return
        # generic items only (drop the private _project / _pipelines keys).
        self._last_items = raw_items
        items = [{"name": i["name"], "type": i["type"], "data": i["data"]} for i in raw_items]
        return CollectOutcome(items=items, errors=errors)

    # --- run: populate gitlab_projects + ci_pipeline_runs after base upserts ---

    def _detail_writers(self, scope, result):
        # Surface any structural detail-write failure on the run (never silent)
        # via ETLConnector.run()'s _write_details.
        return [self._write_cicd_details]

    def _write_cicd_details(self) -> int:
        items = getattr(self, "_last_items", None) or []
        rows_written = 0
        with get_session() as session:
            for item in items:
                project = item.get("_project") or {}
                pid = project.get("id")
                if pid is None:
                    continue
                pname = project.get("name", "")
                # L-8b: must match the identity key `_fetch_project_item`
                # used for the generic Resource row above (path_with_namespace
                # when present, else the bare name) — `pname` itself stays the
                # bare display name for GitlabProject.name/CiPipelineRun.
                # project_name below, which are just descriptive columns, not
                # the identity key.
                resource_name = project.get("path_with_namespace") or pname
                pipelines = item.get("_pipelines") or []
                last_pipeline = pipelines[0] if pipelines else {}

                resource = (
                    session.query(Resource)
                    .filter_by(domain=self.domain, type="gitlab_project", name=resource_name)
                    .first()
                )
                rid = resource.id if resource else None

                # GitLab projects use ``namespace.id`` as the group id; ``namespace``
                # may be absent in some API shapes, so guard it.
                namespace = project.get("namespace") or {}
                group_id = namespace.get("id") if isinstance(namespace, dict) else None

                # Stash the less-queried project metadata in the JSONB details column.
                details = {
                    "ssh_url_to_repo": project.get("ssh_url_to_repo"),
                    "http_url_to_repo": project.get("http_url_to_repo"),
                    "web_url": project.get("web_url"),
                    "namespace": namespace if isinstance(namespace, dict) else None,
                    "description": project.get("description"),
                    "topics": project.get("topics") or project.get("tag_list"),
                    "star_count": project.get("star_count"),
                    "forks_count": project.get("forks_count"),
                }

                gp_row = {
                    "gitlab_project_id": pid,
                    "name": pname,
                    "path_with_namespace": project.get("path_with_namespace"),
                    "default_branch": project.get("default_branch"),
                    "visibility": project.get("visibility"),
                    "archived": bool(project.get("archived", False)),
                    "group_id": group_id,
                    "last_activity_at": _parse_dt(project.get("last_activity_at")),
                    "last_pipeline_id": last_pipeline.get("id"),
                    "last_pipeline_status": last_pipeline.get("status"),
                    "last_pipeline_ref": last_pipeline.get("ref"),
                    "details": details,
                }
                if rid is not None:
                    gp_row["resource_id"] = rid
                self._upsert_detail(session, GitlabProject, gp_row, ["gitlab_project_id"])
                rows_written += 1

                # ci_pipeline_runs — one row per recent pipeline.
                for p in pipelines:
                    if not isinstance(p, dict) or p.get("id") is None:
                        continue
                    run_row = {
                        "gitlab_project_id": pid,
                        "project_name": pname,
                        "pipeline_id": p["id"],
                        "ref": p.get("ref"),
                        "sha": p.get("sha"),
                        "status": p.get("status"),
                        "source": p.get("source"),
                        "created_at": _parse_dt(p.get("created_at")),
                        "updated_at": _parse_dt(p.get("updated_at")),
                        "duration": p.get("duration"),
                        "web_url": p.get("web_url"),
                    }
                    if rid is not None:
                        run_row["resource_id"] = rid
                    self._upsert_detail(
                        session,
                        CiPipelineRun,
                        run_row,
                        ["gitlab_project_id", "pipeline_id"],
                    )
                    rows_written += 1

            session.commit()
        return rows_written
