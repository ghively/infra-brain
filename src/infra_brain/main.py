import asyncio
import logging
import time
import typing
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import UnionType
from typing import Union

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from sqlalchemy.exc import OperationalError, ProgrammingError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from infra_brain.api._envelope import PageEnvelope
from infra_brain.api.graphql import graphql_router
from infra_brain.api.routers.chatops import chatops_router
from infra_brain.api.routers.cloudnet import cloudnet_router
from infra_brain.api.routers.cve import cve_router
from infra_brain.api.routers.documents import documents_router
from infra_brain.api.routers.internal_state import internal_state_router
from infra_brain.api.routers.state_backend import state_backend_router
from infra_brain.api.routers.fleet import fleet_router
from infra_brain.api.routers.governance import (
    governance_audit_router,
    governance_drift_router,
    governance_intelligence_router,
    governance_ops_router,
)
from infra_brain.api.routers.hosts import hosts_router, resources_router
from infra_brain.api.routers.iac import iac_router
from infra_brain.api.routers.llm_observability import llm_observability_router
from infra_brain.api.routers.mcp_keys import mcp_keys_router
from infra_brain.api.routers.netdiscovery import netdiscovery_router
from infra_brain.api.routers.runtime_config import runtime_config_router
from infra_brain.api.routers.settings_catalog import settings_catalog_router
from infra_brain.api.routers.sources import sources_router
from infra_brain.api.routers.ui import ui_public_router, ui_router
from infra_brain.api.routers.vsphere import vsphere_router
from infra_brain.api.routers.vuln import vuln_router
from infra_brain.api.routers.webhook_subscriptions import webhook_subscriptions_router
from infra_brain.callbacks.freshness import check_freshness
from infra_brain.checkpointer import get_async_checkpointer
from infra_brain.dashboard_api import router as dashboard_router
from infra_brain.dashboard_auth import auth_router as dashboard_auth_router
from infra_brain.graph_api import graph_router
from infra_brain.logging_config import configure_logging
from infra_brain.observability import init_tracing
from infra_brain.scheduler import SchedulerService
from infra_brain.secrets import load_secrets_into_env
from infra_brain.webhooks import router as webhook_router

log = logging.getLogger(__name__)
configure_logging()

_scheduler = SchedulerService()

# TRK-093: RED-style HTTP metrics (Request rate, Errors, Duration) exposed at
# /metrics via prometheus_client. Instrumentation lives at the HTTP layer only
# (an @app.middleware("http") wrapper in create_app), not inside every internal
# function. Labels are deliberately low-cardinality: the request counter and the
# latency histogram key on the MATCHED ROUTE TEMPLATE (e.g. /api/hosts/{id}),
# never the raw path, so path params can't explode the series count; unmatched /
# 404 paths collapse to a single "__unmatched__" bucket. 5xx (and any unhandled
# exception, recorded as status 500) are countable via the status label, which
# satisfies the "error count" arm of RED. Defined once at module scope so the
# per-app create_app() factory (called repeatedly in tests) reuses the same
# singletons on the default global REGISTRY rather than re-registering them.
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests, labeled by method, matched route template, and status code.",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds, labeled by method and matched route template.",
    ["method", "path"],
)

# OB-1 scheduler dead-man switch: checked every 15 minutes. The first check is
# delayed by the same interval (see the leading sleep in the loop below) so a
# fresh deploy has time for the first hourly _collection_health_job heartbeat
# before being flagged as dead.
_DEADMAN_CHECK_INTERVAL_SECONDS = 15 * 60


async def _scheduler_deadman_loop() -> None:
    """Independent (non-APScheduler) liveness check for the scheduler (OB-1).

    Runs as a plain asyncio task inside this FastAPI process — deliberately
    NOT an APScheduler job — so a fully-stopped scheduler (which by
    definition cannot self-report) can still be detected and alerted on.
    check_scheduler_deadman() reads the heartbeat that
    scheduler._collection_health_job's record_scheduler_heartbeat() writes on
    its hourly cron; if that heartbeat goes stale, the scheduler has stopped
    running jobs even though this process (and thus /healthz) may still be up.
    """
    from infra_brain.callbacks.freshness import check_scheduler_deadman

    while True:
        try:
            await asyncio.sleep(_DEADMAN_CHECK_INTERVAL_SECONDS)
            alert = check_scheduler_deadman()
            if alert:
                log.error("[scheduler-deadman] %s", alert)
                try:
                    from infra_brain.agents.notification import NotificationAgent

                    await asyncio.to_thread(
                        NotificationAgent().notify_ops_alerts, "scheduler_deadman", [alert]
                    )
                except Exception:
                    log.exception("[scheduler-deadman] failed to deliver ops-webhook alert")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[scheduler-deadman] check failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_secrets_into_env()
    init_tracing()
    # Schema-drift keystone: refuse to come up against a DB whose schema does not
    # match the migrated/declared schema (the exact failure that caused the
    # r7_vulnerabilities.resource_id 500). No-op on SQLite (test/TestClient), so
    # this only enforces against the real Postgres deploy — on drift it raises and
    # the process exits non-zero, turning the deploy health gate red.
    try:
        from infra_brain.db.schema_check import assert_schema_current
        from infra_brain.db.session import get_engine

        assert_schema_current(get_engine())
    except Exception:
        # Any drift (or inability to verify against a real DB) must abort startup
        # loudly — do NOT swallow. SchemaDriftError carries the offending
        # table/column; re-raise so uvicorn exits non-zero.
        log.exception("[schema-check] startup schema verification failed — aborting")
        raise
    try:
        stale = check_freshness()
        if stale:
            log.warning("[freshness] Stale domains on startup: %s", stale)
    except Exception as exc:  # noqa: BLE001
        log.warning("[freshness] Could not check freshness on startup: %s", exc)
    # Eagerly initialise the async checkpointer so Postgres checkpoint tables are
    # created (via setup()) before the first chat request, connection failures are
    # visible in startup logs rather than per-user, and cold-start latency is not
    # paid by the first chat user.  Non-fatal: get_async_checkpointer() already
    # degrades to MemorySaver on any error and emits a warning.
    try:
        await get_async_checkpointer()
        log.info("[checkpointer] Async checkpointer initialised at startup")
    except Exception as exc:  # noqa: BLE001
        log.warning("[checkpointer] Async checkpointer init failed at startup: %s", exc)
    # B2: start the async audit/observation batch writers once per process.
    # They drain queued events from AsyncAuditCallbackHandler/
    # AsyncObservationCallbackHandler (chat + custom-view async call sites) so
    # tool-event DB writes never block the event loop with a per-event commit.
    from infra_brain.callbacks.audit import start_audit_writer, stop_audit_writer
    from infra_brain.callbacks.observation import (
        start_observation_writer,
        stop_observation_writer,
    )

    start_audit_writer()
    start_observation_writer()
    # F18: the embedded APScheduler is OFF by default — the dedicated scheduler
    # service (compose `scheduler` / k8s/scheduler.yaml, replicas:1) owns all
    # cron jobs. Starting it here too would fan every unlocked job out once per
    # API replica (agent-core runs replicas:2 behind an HPA), multiplying alerts,
    # notifications, and LLM spend (CLAUDE.md constraint #9). The dead-man
    # heartbeat CHECK loop below is a plain asyncio task, independent of the
    # scheduler — it always runs so a stalled dedicated scheduler is still
    # detected even when the embedded scheduler is off.
    from infra_brain.config import get_settings

    embedded_scheduler_on = get_settings().embedded_scheduler_enabled
    if embedded_scheduler_on:
        log.warning(
            "[scheduler] EMBEDDED_SCHEDULER_ENABLED=true — running APScheduler jobs "
            "inside this API process. Do NOT enable this alongside the dedicated "
            "scheduler service or every unlocked job runs once per API replica."
        )
        _scheduler.start()
    else:
        log.info(
            "[scheduler] Embedded scheduler disabled (default) — the dedicated "
            "scheduler service owns cron jobs; only the dead-man heartbeat check runs here."
        )
    deadman_task = asyncio.create_task(_scheduler_deadman_loop())
    yield
    deadman_task.cancel()
    with suppress(asyncio.CancelledError):
        await deadman_task
    if embedded_scheduler_on:
        _scheduler.stop()
    # Flush whatever is still queued before the process exits — don't drop
    # pending audit rows on shutdown.
    await stop_audit_writer()
    await stop_observation_writer()


def _degraded_body_for_model(response_model: object) -> dict | list | None:
    """Resolve the degraded body for a single ``response_model`` value.

    Handles three shapes:

    * ``PageEnvelope`` subclass  →  ``{items:[],total:0,limit:0,offset:0,_degraded:True}``
    * ``list[...]`` (``__origin__ is list``) → ``[]``
    * ``X | Y`` / ``Union[X, Y]`` (``types.UnionType`` or ``typing.Union``) →
      resolve using the FIRST member (``typing.get_args()[0]``) and recurse.
      FastAPI routes that declare a union response_model (e.g.
      ``RemediationPageOut | RemediationRollupOut`` for a ``view=detail|rollup``
      toggle) put the "normal"/default view first, matching what a client
      hitting the endpoint without an explicit ``view=`` param would expect.
      ``isinstance(x, type)`` is ``False`` for a union object, so without this
      branch a union response_model falls through to ``None`` → a hard 503
      instead of a degraded 200.
    * Anything else (detail endpoint, non-GET, unmatched) → ``None``

    Detection uses ``issubclass(response_model, PageEnvelope)`` rather than the
    old ``model_name.endswith("PageOut")`` string check, which missed the four
    non-suffixed page classes: ``CveListOut``, ``HostVulnsOut``, ``FleetAssetsOut``,
    and ``SoftwareInventoryOut`` (the last is deferred to Task 5.4).

    All page classes with sidecar required fields (``HostVulnsOut.header``,
    ``FleetAssetsOut.summary``, ``CveListOut.by_severity``) carry defaults, so
    the minimal degraded literal ``{"items": [], "total": 0, "limit": 0,
    "offset": 0, "_degraded": True}`` validates against every PageEnvelope
    subclass without special-casing.

    ``None`` callers should fall back to a 503 response.
    """
    # Pydantic model that inherits PageEnvelope (marker-based, not string-suffix)
    if isinstance(response_model, type) and issubclass(response_model, PageEnvelope):
        return {"items": [], "total": 0, "limit": 0, "offset": 0, "_degraded": True}

    # Generic alias: list[SomeModel]
    origin = getattr(response_model, "__origin__", None)
    if origin is list:
        return []

    # Union type: `X | Y` (types.UnionType) or typing.Union[X, Y]. Both report
    # a `typing.get_origin()` of `types.UnionType`/`typing.Union` respectively;
    # `typing.get_args()` handles both uniformly.
    union_origin = typing.get_origin(response_model)
    if union_origin is Union or union_origin is UnionType:
        args = typing.get_args(response_model)
        if args:
            return _degraded_body_for_model(args[0])

    return None


def _degraded_body_for(request: Request) -> dict | list | None:
    """Return the correct degraded body for the matched route, or None.

    Reads the route object from the ASGI scope (populated by Starlette's router
    before the handler is called, so it is available in exception handlers for
    matched routes), then delegates to :func:`_degraded_body_for_model`.
    """
    if request.method != "GET":
        return None

    route = request.scope.get("route")
    if route is None:
        return None

    response_model = getattr(route, "response_model", None)
    if response_model is None:
        return None

    return _degraded_body_for_model(response_model)


def _register_db_error_handlers(app: FastAPI) -> None:
    """Shape-correct degrade boundary for DB faults only.

    ``OperationalError`` and ``ProgrammingError`` (missing column / unavailable
    datastore) degrade the ONE affected request to an empty response whose shape
    matches the route's declared ``response_model``:

    * ``PageEnvelope`` subclass routes  â†’ HTTP 200 ``{items:[],total:0,limit:0,offset:0,_degraded:True}``
    * bare-list routes   â†’ HTTP 200 ``[]``
    * everything else    â†’ HTTP 503 ``{detail:"data temporarily unavailable"}``

    All other exceptions (``KeyError``, ``RuntimeError``, â€¦) are NOT caught here
    — FastAPI surfaces them as HTTP 500 with a traceback in the server log.
    ``HTTPException`` continues to use FastAPI's own handler (401/404/422
    semantics are preserved).

    The offending error (table/column) is always logged at ERROR level.
    """

    def _log_db_error(request: Request, exc: Exception) -> None:
        log.error(
            "[db-guard] %s on %s %s: %s",
            type(exc).__name__,
            request.method,
            request.url.path,
            str(exc).splitlines()[0] if str(exc) else exc,
        )

    def _degraded_response(request: Request) -> JSONResponse:
        body = _degraded_body_for(request)
        if body is not None:
            return JSONResponse(status_code=200, content=body)
        return JSONResponse(
            status_code=503,
            content={"detail": "data temporarily unavailable"},
        )

    @app.exception_handler(OperationalError)
    async def _operational_error_handler(request: Request, exc: OperationalError):
        _log_db_error(request, exc)
        return _degraded_response(request)

    @app.exception_handler(ProgrammingError)
    async def _programming_error_handler(request: Request, exc: ProgrammingError):
        # ProgrammingError covers "column/table does not exist" — the schema-drift
        # 500 class. Degrade the single endpoint instead of crashing the surface.
        _log_db_error(request, exc)
        return _degraded_response(request)


class SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to index.html for client-side routes.

    The /dashboard2 React app uses BrowserRouter with basename="/dashboard2", so
    deep-links / reloads like /dashboard2/vulns have no matching static file and
    would 404 (F13). This override serves index.html for any unmatched NON-ASSET
    path (a path whose final segment has no file extension), letting the SPA router
    take over. Requests for missing assets (paths with a file extension, e.g.
    /dashboard2/assets/foo.js) still 404 as they should.
    """

    async def get_response(self, path: str, scope) -> Response:  # type: ignore[override]
        # NOTE: the fallback keys on StarletteHTTPException(404). If a 404.html is
        # ever shipped into static2/, Starlette (html=True) returns it directly as a
        # FileResponse instead of raising, which would silently bypass this fallback.
        # The "no dot in final segment" heuristic is a proxy: it keeps real missing
        # assets (foo.js) 404-ing while serving the SPA shell for router paths. A
        # client route with a dot in its last segment would 404 — none exist today.
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
                return await super().get_response("index.html", scope)
            raise


def _assert_dev_not_in_hardened_env() -> None:
    """F23: refuse to boot when the dev master kill-switch is on in a hardened env.

    INFRA_BRAIN_DEV disables dashboard auth, webhook auth, and MCP auth and
    bypasses assert_cookie_secret_ok. It must NEVER be active on a deployed
    stack. ENVIRONMENT defaults to "development" (dev/test/local-compose boot
    with no extra config); hardened deployments set ENVIRONMENT=deployed
    (k8s/configmap.yaml, compose deploy overlay; "production" is a legacy alias),
    which turns this into a hard, loud startup failure if INFRA_BRAIN_DEV is
    also truthy.
    """
    from infra_brain.config import get_settings, is_hardened_environment

    settings = get_settings()
    if settings.infra_brain_dev and is_hardened_environment(settings.environment):
        raise RuntimeError(
            f"Refusing to start: INFRA_BRAIN_DEV is enabled while "
            f"ENVIRONMENT={settings.environment.strip()} (a hardened deployment). "
            "Dev mode disables dashboard/webhook/MCP auth and bypasses the cookie-secret "
            "check — it must never run on a deployed stack. Unset INFRA_BRAIN_DEV (or set "
            "it to 0) on the deployed stack, or set ENVIRONMENT=development for a "
            "local dev run."
        )


def create_app() -> FastAPI:
    _assert_dev_not_in_hardened_env()

    # Every other surface in this app is gated (session cookie, HMAC, or bearer
    # token — see the include_router comments below); Swagger UI (/docs, /redoc)
    # and the raw OpenAPI schema (/openapi.json) were the one exception, reachable
    # with zero auth. That's schema-only exposure (route paths, param names/types,
    # response_model shapes), not a data leak, but it's an unintentional asymmetry
    # on a deployed/hardened stack. Reuse the same is_hardened_environment() gate
    # _assert_dev_not_in_hardened_env() uses above: disable docs_url/openapi_url
    # (which also disables /redoc, since FastAPI derives it from openapi_url) in a
    # hardened environment, leave them on for local dev as today.
    from infra_brain.config import get_settings, is_hardened_environment

    hardened = is_hardened_environment(get_settings().environment)
    app = FastAPI(
        title="Infra Brain",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if hardened else "/docs",
        openapi_url=None if hardened else "/openapi.json",
    )
    _register_db_error_handlers(app)

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next):
        """TRK-093: RED-style HTTP instrumentation at the request boundary.

        Times every request, records its latency, and increments the request
        counter labeled by method + matched route template + status. An
        unhandled exception is recorded as status 500 (and re-raised) so errors
        remain countable. The route template is read from ``request.scope`` after
        routing (the same mechanism ``_degraded_body_for`` uses); a request that
        never matched a route collapses to ``__unmatched__`` to bound cardinality.
        """
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            duration = time.perf_counter() - start
            route = request.scope.get("route")
            path_template = getattr(route, "path", None) or "__unmatched__"
            HTTP_REQUEST_DURATION_SECONDS.labels(method=request.method, path=path_template).observe(
                duration
            )
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method, path=path_template, status=str(status)
            ).inc()

    # #88: per-request correlation ID, mounted directly after metrics_middleware
    # (in source order) so it wraps (is outermost relative to) it — Starlette's
    # @app.middleware("http") decorator registers middleware in
    # reverse-declaration order relative to request flow, so the LAST-declared
    # layer runs FIRST on the way in. Declaring this after metrics_middleware
    # means the request ID is set before metrics_middleware runs, and thus
    # before any route handler logs anything.
    from infra_brain.request_context import request_id_middleware_factory

    app.middleware("http")(request_id_middleware_factory())

    # TRK-095 (residual): Host-header validation via Starlette's
    # TrustedHostMiddleware. OPT-IN and safe-by-default — trusted_hosts is ""
    # by default, which parse_trusted_hosts() maps to ["*"] (accept any Host),
    # preserving pre-TRK-095 behavior so existing deployments are not broken.
    # Set TRUSTED_HOSTS to the deployment's public hostname(s) to enforce.
    # Added AFTER the metrics middleware so it is the OUTERMOST layer: a request
    # with a disallowed Host is rejected with HTTP 400 before any routing or
    # per-request work. The middleware is pure ASGI (no blocking I/O), so it does
    # not touch the event loop.
    from infra_brain.config import get_settings, parse_trusted_hosts

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=parse_trusted_hosts(get_settings().trusted_hosts),
    )

    @app.get("/metrics")
    def metrics():
        # No-auth, zero-extra-I/O scrape endpoint — same trust tier as /healthz
        # (CLAUDE.md constraint #8). Serializes the default global REGISTRY.
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(webhook_router)
    app.include_router(chatops_router)  # inbound Slack/Teams chatops bot (GitLab #111; HMAC-gated, not session)
    app.include_router(dashboard_auth_router)  # login/logout/me (ungated)
    app.include_router(dashboard_router)  # data + actions (session-gated)
    app.include_router(governance_drift_router)  # drift events + notifications (session-gated)
    app.include_router(governance_audit_router)  # audit log, activity, decisions (session-gated)
    app.include_router(
        llm_observability_router
    )  # T7: LLM usage/cost/outcome audit surface (session-gated)
    app.include_router(
        governance_intelligence_router
    )  # instincts, observations, scripts, proposals (session-gated)
    app.include_router(
        governance_ops_router
    )  # agents, settings, config, compliance, approvals (session-gated)
    app.include_router(
        sources_router
    )  # per-domain source inventory + UI visibility verdict (session-gated)
    app.include_router(fleet_router)  # fleet / health / version routes (session-gated)
    app.include_router(resources_router)  # resources + eol routes (session-gated)
    app.include_router(netdiscovery_router)  # shadow-IT / network discovery view (session-gated)
    app.include_router(ui_router)  # Chat + Custom Views (session-gated)
    # TRK-306: the single public share route. Mounted separately because
    # ui_router's router-level require_session gate would otherwise 401 an
    # anonymous recipient of a share link — the one visitor it exists for.
    app.include_router(ui_public_router)
    app.include_router(documents_router)  # RAG documents / knowledge-store browser (session-gated)
    app.include_router(cve_router)  # CVE Detail view (session-gated)
    app.include_router(vuln_router)  # Vulnerability queue + remediation (session-gated)
    app.include_router(iac_router)  # IaC / Pipelines view (session-gated)
    app.include_router(vsphere_router)  # vSphere / Virtualization view (session-gated)
    app.include_router(
        cloudnet_router
    )  # Cloud/K8s/Net typed views (session-gated; POC-disabled domains)
    app.include_router(hosts_router)  # Unified host identity view (session-gated)
    app.include_router(graph_router)  # Knowledge graph traversal (session-gated)
    app.include_router(mcp_keys_router)  # MCP scoped-key management (session/admin-gated)
    app.include_router(runtime_config_router)  # RuntimeConfig tuning CRUD (session/admin-gated, TRK-303)
    app.include_router(settings_catalog_router)  # Derived Settings catalog (admin-gated)
    app.include_router(graphql_router)  # Read-only GraphQL query layer (session-gated, GitLab #114)
    app.include_router(
        webhook_subscriptions_router
    )  # outbound webhook subscription mgmt (session/admin-gated, issue #112)
    app.include_router(state_backend_router)  # Phase 4 state-backend REST counterparts (P4.3e)
    app.include_router(internal_state_router)  # infra-ops hook write path (bearer-token-gated, P5.1/P5.2)

    @app.get("/")
    def root():
        # Wave 6.1 B7 (DR-6.1): legacy DC-framework dashboard retired; Vite+React
        # rebuild is now the sole dashboard (wave-6-plan.md Part 3 B7).
        return RedirectResponse(url="/dashboard2", status_code=302)

    # Fail closed on a missing/weak dashboard cookie secret in non-dev (F-027).
    from infra_brain.dashboard_auth import assert_cookie_secret_ok

    assert_cookie_secret_ok()

    # Wave 6.1 B7: legacy /dashboard static tree deleted — mount removed.
    # The Vite+React dashboard (static2/) is now the sole UI served at /dashboard2.
    # F-039 DECISION (Wave 1.5f) carried forward: the mount stays UNAUTHENTICATED.
    # It serves only the SPA shell/assets; every /api/dashboard/* call the SPA makes
    # is gated by require_session. Gating the shell would only move the login screen.
    static2_dir = Path(__file__).parent / "dashboard" / "static2"
    if static2_dir.is_dir():
        app.mount(
            "/dashboard2",
            SPAStaticFiles(directory=str(static2_dir), html=True),
            name="dashboard2",
        )
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("infra_brain.main:create_app", factory=True, host="0.0.0.0", port=8000)
