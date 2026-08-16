import asyncio
import uuid
import hmac
import logging
import threading
from types import SimpleNamespace
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy import text
from infra_brain.api.schemas import (
    ActionApproveBody,
    ActionApproveResult,
    ActionRejectResult,
    HealthOut,
    HealthzOut,
    HeartbeatOut,
    IncidentAckBody,
    IncidentAckResult,
    OpsAlertIn,
    OpsAlertReceivedOut,
    ProposalApproveResult,
    ScanPointItemOut,
    SweepAcceptedOut,
    WebhookCloudAcceptedOut,
)
from infra_brain.dedup import try_acquire, release
from infra_brain.supervisor import dispatch, AGENT_REGISTRY
from infra_brain.db.models import (
    ComplianceViolation,
    DriftEvent,
    IntegrationProposal,
    ScanPoint,
)
from infra_brain.db.session import get_session, get_engine
from infra_brain.config import get_settings
from infra_brain.remediation_graph import resume_remediation_action
from infra_brain.action_decisions import ActionDecisionError, approve_action as _approve_action_row
from infra_brain.action_decisions import reject_action as _reject_action_row
from datetime import datetime, timezone

router = APIRouter()
log = logging.getLogger(__name__)

KNOWN_DOMAINS = set(AGENT_REGISTRY.keys())


def _verify_token(provided: str | None, expected: str, header_name: str) -> None:
    """Raise 403 unless the request is authenticated OR we are explicitly in dev-mode.

    F-034: default is fail-closed. An empty secret is only allowed when
    INFRA_BRAIN_DEV=1 (get_settings().infra_brain_dev). Otherwise a missing secret
    with webhook_auth_required (now default True) rejects the request.
    """
    settings = get_settings()
    if not expected:
        if settings.infra_brain_dev and not settings.webhook_auth_required:
            # Explicit dev-mode: empty secret → allow (log so operators notice).
            log.warning(
                "[webhook] %s has no secret configured (INFRA_BRAIN_DEV=1) — allowing in dev mode",
                header_name,
            )
            return
        # Fail closed: no secret configured and not in dev → reject.
        log.warning(
            "[webhook] %s hit with no secret configured — rejecting (fail closed)",
            header_name,
        )
        raise HTTPException(status_code=403, detail=f"No secret configured for {header_name}")
    if not hmac.compare_digest(provided or "", expected):
        raise HTTPException(status_code=403, detail=f"Invalid {header_name}")


def _verify_ops_alert_bearer(authorization: str | None) -> None:
    """Bearer auth for the inbound ops-alert receiver (TRK-135 / TRK-329).

    Deliberately NOT ``_verify_token`` above, for two reasons:

    1. Scheme. The two producers that will call this endpoint already send
       ``Authorization: Bearer <OPS_WEBHOOK_TOKEN>`` (tools/ops_webhook.py and
       docker/deadman-prober/prober.py both build that header today). Reusing
       an ``X-*``-header check would have meant changing a producer that is
       already correct.
    2. Fail-closed with no dev-mode escape. ``_verify_token`` has an
       ``infra_brain_dev`` branch that ALLOWS the request when the secret is
       unset. That is defensible for a sweep-trigger webhook; it is not
       defensible here. An unconfigured receiver must not be an
       unauthenticated one — this endpoint's whole purpose is to be a
       destination you can point at with no external services, which makes an
       accidentally-open version of it exactly the wrong artifact to ship. No
       token configured => 503 Service Unavailable (the feature is off), never
       200 and never "allowed because dev".

    401 (not 403) on a bad/missing token, with WWW-Authenticate, because this
    is bearer-scheme authentication rather than the shared-secret-header
    convention the 403 routes use.

    Comparison is constant-time (``hmac.compare_digest`` over utf-8 bytes —
    bytes, not str, so a non-ASCII header value cannot raise TypeError and
    turn an auth failure into a 500).
    """
    expected = (get_settings().ops_webhook_receiver_token or "").strip()
    if not expected:
        log.warning(
            "[ops-alert-receiver] rejected: OPS_WEBHOOK_RECEIVER_TOKEN is not set — "
            "the receiver is OFF (503), not open"
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "ops-alert receiver is not configured — set OPS_WEBHOOK_RECEIVER_TOKEN "
                "to enable POST /webhooks/ops-alert"
            ),
        )
    provided = ""
    if authorization:
        scheme, _, rest = authorization.partition(" ")
        if scheme.lower() == "bearer":
            provided = rest.strip()
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# B5: webhook-triggered sweeps run inside the API pod's Starlette
# BackgroundTasks threadpool (a plain sync function run in a worker thread,
# NOT on the asyncio event loop) — competing with interactive chat for the
# same DB pool. Redis dedup (dedup.py's try_acquire) already caps
# concurrency at 1 per (domain, scope), but that only guards repeats of the
# SAME domain/scope; a webhook storm hitting *distinct* domains/scopes (e.g.
# the ansible webhook's scope=host path) could still run unbounded
# concurrent sweeps in this process. Cap that with a small bounded semaphore.
#
# _dispatch_bg is a SYNC function run by BackgroundTasks in its threadpool,
# not on the event loop, so an asyncio.Semaphore cannot be acquired here (it
# has no running loop to attach to in that thread) — use
# threading.BoundedSemaphore instead, with a non-blocking acquire so a
# request that hits the cap gets skipped/logged instead of blocking the
# worker thread waiting for a slot.
#
# The correct long-term fix is routing this work to the dedicated scheduler
# container via the existing Redis dedup infra instead of running it in the
# API pod at all — that is a larger change than this pass (real risk of
# introducing a new async coordination bug under time pressure) and is
# intentionally NOT attempted here.
_MAX_CONCURRENT_WEBHOOK_SWEEPS = 2
_webhook_sweep_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_WEBHOOK_SWEEPS)


def _dispatch_sweep_bg(domain: str) -> None:
    """Background sweep-graph dispatch for a single-domain, scope=="all" trigger.

    Phase 2 Task 4: routed here instead of ``_dispatch_bg`` when
    ``sweep_graph_enabled`` — the sweep graph's collector node owns
    ``domain:all`` for the duration of the run, so this path must NOT also
    hold the route-level Redis lock (double-acquire would deadlock the
    legacy release()/try_acquire() dance or silently skip a sweep that
    already holds the lock). ``run_sweep_sync`` is the dedicated-loop sync
    wrapper (graph.py) — never ``asyncio.run()`` here (would corrupt the
    shared AsyncPostgresSaver pool's bound event loop).
    """
    if not _webhook_sweep_semaphore.acquire(blocking=False):
        log.warning(
            "[dispatch_sweep_bg] skipped domain=%s: %d webhook sweeps already running",
            domain,
            _MAX_CONCURRENT_WEBHOOK_SWEEPS,
        )
        return
    try:
        from infra_brain.graph import run_sweep_sync  # noqa: PLC0415

        run_sweep_sync(domains=[domain], run_reasoners=False, trigger_type="webhook")
    except Exception as exc:  # noqa: BLE001
        log.warning("[dispatch_sweep_bg] error for domain=%s: %s", domain, exc, exc_info=True)
    finally:
        _webhook_sweep_semaphore.release()


def _trigger_domain(
    domain: str, trigger_type: str, scope: str, background_tasks: BackgroundTasks
) -> None:
    """Route a single-domain webhook/manual trigger to the sweep graph or the
    legacy dispatch() path, and enqueue the matching background task.

    Graph path (``sweep_graph_enabled`` AND ``scope == "all"``): no
    route-level Redis lock is acquired — the sweep graph's collector node
    owns ``domain:all`` itself. Scoped triggers (e.g. per-host ansible
    dispatch) always stay on the legacy path in both modes — the graph has
    no scope channel and a host-scoped trigger must not silently widen to a
    full-domain collection.

    Raises ``HTTPException(409)`` on the legacy path when the route-level
    lock is already held (unchanged behavior), and also when *domain* is
    retired (``AgentSpec.retired``).

    The retired check belongs HERE rather than in each route because several
    routes name a retired domain as a literal (``/webhooks/octopus``,
    ``/webhooks/cloud/{provider}``, and ``/webhooks/ansible`` for a Windows
    host). Without it those routes still return 202 Accepted and then fail
    inside the background task, where ``dispatch()``'s ValueError is logged and
    never reaches the caller — the sender would keep firing a webhook that can
    never do anything. Answering 409 immediately tells it why.
    """
    # is_retired, not retired_domains: one class resolved per request rather
    # than every agent module imported on the first webhook.
    from infra_brain.etl.spec import is_retired

    if is_retired(domain):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{domain} is retired and is not collected — "
                f"set COLLECTION_REVIVED_DOMAINS={domain} to turn it back on"
            ),
        )
    settings = get_settings()
    if settings.sweep_graph_enabled and scope == "all":
        background_tasks.add_task(_dispatch_sweep_bg, domain)
        return
    token = try_acquire(domain, scope)
    if not token:
        raise HTTPException(status_code=409, detail=f"{domain} collection already in progress")
    background_tasks.add_task(_dispatch_bg, domain, trigger_type, scope, token)


def _dispatch_bg(domain: str, trigger_type: str, scope: str, token: str | None = None):
    """Background dispatch; releases the dedup lock with token-safe compare-and-delete.

    B5: bounded by ``_webhook_sweep_semaphore`` so at most
    ``_MAX_CONCURRENT_WEBHOOK_SWEEPS`` webhook-triggered sweeps run
    concurrently in this process. A request that arrives when the cap is
    already held is skipped (not queued/blocked) — the Redis dedup lock is
    released immediately so a retry (or the domain's own scheduled cadence)
    can pick it up instead of piling up worker threads.
    """
    acquired = _webhook_sweep_semaphore.acquire(blocking=False)
    if not acquired:
        log.warning(
            "[dispatch_bg] concurrent webhook-sweep cap (%d) reached — skipping "
            "domain=%s scope=%s this cycle",
            _MAX_CONCURRENT_WEBHOOK_SWEEPS,
            domain,
            scope,
        )
        try:
            release(domain, scope, token=token)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[dispatch_bg] release() failed for domain=%s scope=%s: %s", domain, scope, exc
            )
        return
    try:
        dispatch(domain=domain, trigger_type=trigger_type, scope=scope)
    except Exception as exc:  # noqa: BLE001
        log.warning("[dispatch_bg] error for domain=%s: %s", domain, exc, exc_info=True)
    finally:
        _webhook_sweep_semaphore.release()
        try:
            release(domain, scope, token=token)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[dispatch_bg] release() failed for domain=%s scope=%s: %s", domain, scope, exc
            )


@router.post("/webhooks/gitlab", status_code=202, response_model=SweepAcceptedOut)
async def webhook_gitlab(request: Request, background_tasks: BackgroundTasks):
    _verify_token(
        request.headers.get("X-Gitlab-Token"),
        get_settings().webhook_gitlab_secret,
        "X-Gitlab-Token",
    )
    _trigger_domain("cicd", "webhook", "all", background_tasks)
    return {"accepted": True, "domain": "cicd"}


@router.post("/webhooks/octopus", status_code=202, response_model=SweepAcceptedOut)
async def webhook_octopus(request: Request, background_tasks: BackgroundTasks):
    _verify_token(
        request.headers.get("X-Octopus-Webhook-Token"),
        get_settings().webhook_octopus_secret,
        "X-Octopus-Webhook-Token",
    )
    _trigger_domain("octopus", "webhook", "all", background_tasks)
    return {"accepted": True, "domain": "octopus"}


@router.post("/webhooks/ansible", status_code=202, response_model=SweepAcceptedOut)
async def webhook_ansible(request: Request, background_tasks: BackgroundTasks):
    _verify_token(
        request.headers.get("X-Ansible-Token"),
        get_settings().webhook_ansible_secret,
        "X-Ansible-Token",
    )
    payload = await request.json()
    host = payload.get("host", "all")
    domain = "windows" if "win" in host.lower() else "linux"
    _trigger_domain(domain, "webhook", host, background_tasks)
    return {"accepted": True, "domain": domain}


@router.post("/webhooks/cloud/{provider}", status_code=202, response_model=WebhookCloudAcceptedOut)
async def webhook_cloud(provider: str, request: Request, background_tasks: BackgroundTasks):
    _verify_token(
        request.headers.get("X-Infra-Token"), get_settings().webhook_generic_secret, "X-Infra-Token"
    )
    _trigger_domain("cloud", "webhook", "all", background_tasks)
    return {"accepted": True, "domain": "cloud", "provider": provider}


@router.post("/webhooks/generic", status_code=202, response_model=SweepAcceptedOut)
async def webhook_generic(request: Request, background_tasks: BackgroundTasks):
    _verify_token(
        request.headers.get("X-Infra-Token"), get_settings().webhook_generic_secret, "X-Infra-Token"
    )
    payload = await request.json()
    domain = payload.get("domain")
    scope = payload.get("scope", "all")
    if domain not in KNOWN_DOMAINS:
        raise HTTPException(status_code=400, detail=f"Unknown domain: {domain}")
    _trigger_domain(domain, "webhook", scope, background_tasks)
    return {"accepted": True, "domain": domain}


@router.get("/healthz", response_model=HealthzOut)
def healthz() -> dict:
    """Liveness probe — zero I/O. Restarts if this fails."""
    return {"status": "ok"}


@router.get("/health", response_model=HealthOut)
def health() -> dict | JSONResponse:
    result: dict = {"status": "ok"}

    # Postgres connectivity check
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        result["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.warning("health check: postgres failed: %s", exc, exc_info=True)
        result["postgres"] = "error"
        result["status"] = "degraded"

    # Redis connectivity check — reuse the shared client from dedup
    try:
        from infra_brain.dedup import get_redis

        get_redis().ping()
        result["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.warning("health check: redis failed: %s", exc, exc_info=True)
        result["redis"] = "error"
        result["status"] = "degraded"

    # Readiness contract: a degraded pod must be pulled from LB rotation. k8s keys
    # readiness off the HTTP status code (k8s/agent-core.yaml), so a "degraded" body
    # returned with implicit 200 would keep a pod with a dead dependency in rotation.
    # Return 503 to signal not-ready; 200 only when all dependency checks pass.
    if result["status"] == "degraded":
        return JSONResponse(content=result, status_code=503)
    return result


@router.get("/api/ops/heartbeat", response_model=HeartbeatOut)
def ops_heartbeat() -> dict:
    """Scheduler dead-man heartbeat surface (Phase 4 OB-1 external prober).

    Unauthenticated — same trust tier as /healthz and /health, which this
    endpoint is polled alongside by the standalone deadman-prober sidecar
    (docker/deadman-prober/prober.py). Carries only a heartbeat age and a
    threshold; no infrastructure data. The scheduler heartbeat is written
    HOURLY (record_scheduler_heartbeat via the hourly _collection_health_job),
    so ``stale`` only flips once age exceeds the multi-hour threshold — never
    on a few minutes of staleness.
    """
    from infra_brain.callbacks.freshness import get_scheduler_heartbeat_status  # noqa: PLC0415

    age_seconds, max_age_seconds, stale = get_scheduler_heartbeat_status()
    return {
        "heartbeat_age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "stale": stale,
    }


@router.get("/scan_points", response_model=list[ScanPointItemOut])
def get_scan_points(request: Request):
    _verify_token(
        request.headers.get("X-Infra-Token"), get_settings().webhook_generic_secret, "X-Infra-Token"
    )
    with get_session() as session:
        points = session.query(ScanPoint).filter_by(status="active").all()
        return [
            {
                "id": str(p.id),
                "domain": p.domain,
                "method": p.method,
                "schedule": p.schedule,
                "last_run": str(p.last_run),
            }
            for p in points
        ]


@router.post(
    "/integrations/{proposal_id}/approve", status_code=200, response_model=ProposalApproveResult
)
def approve_integration(proposal_id: str, request: Request, body: ActionApproveBody | None = None):
    """Approve a pending IntegrationProposal and immediately wire() it.

    Mirrors the dashboard's ``governance_intelligence.approve_integration_proposal``
    (#93): approval and wiring happen in the SAME request — CoverageAgent.wire()
    is fast (one LLM call + a handful of DB rows) and safe to run inline. This
    stays a plain (sync) ``def`` route — FastAPI already runs sync handlers in
    its threadpool, off the event loop, so no asyncio.to_thread wrapping is
    needed here (unlike the async ``approve_action``/``reject_action`` handlers,
    which offload sync work explicitly because they are ``async def``).

    A ``pending``-status guard is required (not just the pre-existing
    confidence floor) so this route can't re-approve an already-approved/wired
    proposal, and a wire() failure (exception or a clean ``False`` return)
    reverts status back to ``pending`` so the proposal isn't stranded —
    without this, approving here but never wiring left the row
    un-activatable both from this route (no retry) and from the dashboard's
    own approve-and-wire endpoint (409, not pending).
    """
    _verify_token(
        request.headers.get("X-Infra-Token"), get_settings().webhook_generic_secret, "X-Infra-Token"
    )
    approved_by = (body.approved_by if body else None) or "api"

    with get_session() as session:
        proposal = session.get(IntegrationProposal, uuid.UUID(proposal_id))
        if not proposal:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if proposal.status != "pending":
            raise HTTPException(
                status_code=409, detail=f"Proposal is {proposal.status}, not pending"
            )
        if proposal.confidence < 0.7:
            raise HTTPException(status_code=422, detail="Confidence < 0.7 — cannot approve")
        proposal.status = "approved"
        proposal.approved_by = approved_by
        proposal.approved_at = datetime.now(timezone.utc)
        session.commit()

    from infra_brain.agents.coverage import CoverageAgent  # noqa: PLC0415

    def _revert_to_pending() -> None:
        with get_session() as session:
            proposal = session.get(IntegrationProposal, uuid.UUID(proposal_id))
            if proposal and proposal.status == "approved":
                proposal.status = "pending"
                proposal.approved_by = None
                proposal.approved_at = None
                session.commit()

    try:
        wired = CoverageAgent().wire(proposal_id)
    except Exception:
        log.exception("CoverageAgent.wire() raised for proposal %s", proposal_id)
        _revert_to_pending()
        raise HTTPException(
            status_code=500,
            detail="Proposal approved but wiring raised an exception — reverted to "
            "pending, retry via this endpoint",
        )
    if not wired:
        _revert_to_pending()
        raise HTTPException(
            status_code=500,
            detail="Proposal approved but wiring failed — reverted to pending, "
            "retry via this endpoint",
        )
    return {"approved": True, "proposal_id": proposal_id}


@router.post("/actions/{action_id}/approve", status_code=200, response_model=ActionApproveResult)
async def approve_action(action_id: str, request: Request, body: ActionApproveBody | None = None):
    """Approve a human-gated ProposedAction (e.g. a remediation). Confidence ≥0.7
    required; execution happens on the proposing agent's next run.

    Guards + field writes live in ``action_decisions.approve_action`` — shared
    with the dashboard's mirror route (``governance_ops.approve_remediation_action``)
    and the MCP tools, so this route cannot silently drift from them again.
    Notably this rejects entity_resolution_same_as review rows (409, pointing
    at POST /api/graph/entity-resolution/{id}/confirm instead) — approving one
    generically here would flip status with no SAME_AS edge ever written and no
    way to recover (TRK-226).

    Phase 3 Task 4: async handler — sync DB work runs via asyncio.to_thread
    (never on the event loop), then the parked interrupt graph (if any) is
    resumed via the shared NON-fatal helper; a resume failure only defers
    execution to the _execute_approved poll (the row is already flipped)."""
    _verify_token(
        request.headers.get("X-Infra-Token"), get_settings().webhook_generic_secret, "X-Infra-Token"
    )
    approved_by = (body.approved_by if body else None) or "api"

    def _approve() -> SimpleNamespace:
        with get_session() as session:
            try:
                return _approve_action_row(session, uuid.UUID(action_id), approved_by)
            except ActionDecisionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    snapshot = await asyncio.to_thread(_approve)
    await resume_remediation_action(snapshot, approved=True)
    return {"approved": True, "action_id": action_id}


@router.post("/actions/{action_id}/reject", status_code=200, response_model=ActionRejectResult)
async def reject_action(action_id: str, request: Request):
    """Phase 3 Task 4: async handler — DB work off-loop, then resume the parked
    interrupt graph (if any) with a rejection so its thread finishes cleanly.

    Guards + field writes live in ``action_decisions.reject_action`` — shared
    with the dashboard's mirror route and the MCP tools, exactly as the approve
    handler above already was. This route used to carry its OWN inline copy of
    the guards, which is precisely how it missed the entity-resolution guard:
    it now 409s an ``entity_resolution_same_as`` review row, pointing at
    POST /api/graph/entity-resolution/{id}/reject, because rejecting one of
    those is an attributed pair-scoped veto write, not a status flip (KG-3).
    """
    _verify_token(
        request.headers.get("X-Infra-Token"), get_settings().webhook_generic_secret, "X-Infra-Token"
    )

    def _reject() -> SimpleNamespace:
        with get_session() as session:
            try:
                return _reject_action_row(session, uuid.UUID(action_id))
            except ActionDecisionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    snapshot = await asyncio.to_thread(_reject)
    await resume_remediation_action(snapshot, approved=False)
    return {"rejected": True, "action_id": action_id}


@router.post("/webhooks/incident/ack", status_code=200, response_model=IncidentAckResult)
async def webhook_incident_ack(request: Request, body: IncidentAckBody):
    """Ack-in callback (TRK-242 / GitLab #113) for a PagerDuty/Opsgenie-style
    incident-management tool. Closes the loop on the outbound ops-alert
    webhook (tools/ops_webhook.py send_ops_alert's dedup_key) by letting the
    external tool tell infra-brain an incident was seen/acked or resolved.

    This mutates only infra-brain's OWN state (DriftEvent/ComplianceViolation
    ``status``) — no outbound call to any external system is made here, so
    ``gate_external_write`` (which gates infra-brain's own writes TO GitLab/
    Jira/Confluence/the ops webhook) does not apply. It is authenticated the
    same fail-closed way as every other inbound webhook in this file.
    """
    _verify_token(
        request.headers.get("X-Incident-Token"),
        get_settings().webhook_incident_secret,
        "X-Incident-Token",
    )
    new_status = "acknowledged" if body.action == "acknowledge" else "resolved"

    def _ack() -> int:
        with get_session() as session:
            updated = 0
            for row in session.query(DriftEvent).filter_by(incident_key=body.incident_key).all():
                row.status = new_status
                updated += 1
            for row in (
                session.query(ComplianceViolation).filter_by(incident_key=body.incident_key).all()
            ):
                row.status = new_status
                updated += 1
            if updated == 0:
                raise HTTPException(
                    status_code=404, detail=f"Unknown incident_key: {body.incident_key}"
                )
            session.commit()
        return updated

    updated = await asyncio.to_thread(_ack)
    return {"acknowledged": True, "incident_key": body.incident_key, "updated": updated}


@router.post("/webhooks/ops-alert", status_code=200, response_model=OpsAlertReceivedOut)
async def webhook_ops_alert(request: Request, body: OpsAlertIn):
    """In-app ops-alert RECEIVER (TRK-135 + the operator half of TRK-329).

    The alerting system in this deployment has had complete delivery wiring and
    no destination: ``OPS_WEBHOOK_URL`` was never set anywhere, and
    ``webhook_subscriptions`` has zero rows, so every escalation the hourly
    freshness monitor raised existed only as a log line in a container nothing
    consumes. That is how a broken ``linux`` collector stayed broken for 8 days
    across 34 consecutive failed runs (TRK-328). The missing piece was never
    the wiring — it was somewhere for the wiring to point.

    This endpoint is that somewhere. With::

        OPS_WEBHOOK_URL=http://app:8000/webhooks/ops-alert
        OPS_WEBHOOK_RECEIVER_TOKEN=<a random value>
        OPS_WEBHOOK_TOKEN=<the same random value — what we send == what we accept>

    BOTH producers become live with zero external services: the in-app path
    (``tools/ops_webhook.py::send_ops_alert`` — freshness monitor, deadman
    checks, entity-retirement alerts) and, more importantly, the standalone
    ``docker/deadman-prober`` container, whose alerts previously had no
    persistence fallback at all and simply vanished.

    This is the FLOOR, not a pager. It guarantees an alert is *durable and
    visible in the dashboard*; it does not wake anyone up. A real external
    channel is still strictly better, and ``OPS_WEBHOOK_URL`` remains a plain
    setting — repoint it at PagerDuty/Opsgenie/Slack any time and this
    endpoint simply stops receiving.

    LOOP SAFETY (the obvious hazard when a producer's destination is its own
    process — traced end to end, not assumed):

    1. *One hop, by construction.* This handler's only side effect is a single
       ``AuditLog`` INSERT. It never calls ``send_ops_alert``, never dispatches
       a sweep, and never invokes any monitor — directly or transitively. So
       "app alerts itself" terminates here: POST → row → 200 → ``send_ops_alert``
       returns True. There is no second hop to recurse from.
    2. *A failure inside the receiver cannot alert.* If the INSERT raises we
       return 500. ``send_ops_alert``'s caller-side ``except`` logs via
       ``logger.exception`` and returns False; it does not retry and does not
       raise a new alert about the failed alert. The prober's ``send_alert``
       likewise logs and returns False, and its ``_deliver`` ignores that
       return value entirely. A broken receiver therefore degrades to
       "alerts are logged only" — precisely the pre-existing status quo — and
       never to an alert storm.
    3. *The rows we write are terminals, not triggers.* We persist to
       ``AuditLog`` with ``allowed=False``. The only monitor in this codebase
       that watches for a denial spike is
       ``callbacks/anomaly.py::check_agent_anomalies``, and it queries
       ``AgentActionLog.verdict == "deny"`` — a DIFFERENT table. Nothing reads
       ``AuditLog.allowed`` to raise an alert; ``get_audit_log(allowed=False)``
       is a read-only dashboard view. So a persisted alert cannot feed back
       into the anomaly detector and manufacture the next alert.
    4. *No double-persist.* ``_persist_undelivered_alert`` only runs on the
       ``OPS_WEBHOOK_URL``-unset branch, which by definition is not taken when
       the URL points here. One alert => exactly one row.

    ``gate_external_write`` does not apply: that gate covers infra-brain's own
    writes OUT to GitLab/Jira/Confluence/the ops webhook. This is inbound, and
    it mutates only infra-brain's own audit table — same reasoning as
    ``webhook_incident_ack`` above.

    Async handler with the sync DB write offloaded via ``asyncio.to_thread``,
    the same hygiene as ``webhook_incident_ack`` / ``approve_action`` (F-019).
    """
    _verify_ops_alert_bearer(request.headers.get("Authorization"))

    from infra_brain.tools.ops_webhook import record_alert_audit_row  # noqa: PLC0415

    def _persist() -> None:
        record_alert_audit_row(
            tool=f"ops_alert_received:{body.category}",
            category=body.category,
            messages=body.messages,
            agent_name="ops_alert_receiver",
            dedup_key=body.dedup_key,
            reason_prefix="Ops alert received via POST /webhooks/ops-alert.",
        )

    try:
        await asyncio.to_thread(_persist)
    except Exception as exc:  # noqa: BLE001
        # Tell the sender the alert was NOT stored (see loop-safety note 2:
        # neither producer escalates on a delivery failure, so this cannot
        # recurse). Silently 200-ing here would recreate the exact defect this
        # endpoint exists to fix — a delivery path that reports success while
        # the alert goes nowhere.
        log.exception("[ops-alert-receiver] failed to persist %s alert", body.category)
        raise HTTPException(status_code=500, detail="Failed to persist ops alert") from exc

    log.warning(
        "[ops-alert-receiver] stored %s alert with %d message(s): %s",
        body.category,
        len(body.messages),
        " | ".join(body.messages)[:500],
    )
    return {"received": True, "category": body.category, "messages": len(body.messages)}


@router.post("/sweeps/{domain}", status_code=202, response_model=SweepAcceptedOut)
async def manual_sweep(domain: str, request: Request, background_tasks: BackgroundTasks):
    _verify_token(
        request.headers.get("X-Infra-Token"), get_settings().webhook_generic_secret, "X-Infra-Token"
    )
    if domain not in KNOWN_DOMAINS:
        raise HTTPException(status_code=404, detail=f"Unknown domain: {domain}")
    _trigger_domain(domain, "manual", "all", background_tasks)
    return {"accepted": True, "domain": domain}
