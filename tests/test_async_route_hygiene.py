"""F-019 regression: async route handlers must not do blocking sync DB/LLM work.

The five verified blockers were webhooks get_scan_points / approve_integration /
approve_action / reject_action (sync get_session inside `async def`) and
chat/agent.py agent_node (sync llm.invoke inside the graph). The four webhook
handlers are now plain `def` (FastAPI runs those in its threadpool); only the
dispatch-only webhook routes remain coroutines.
"""

import inspect

ALLOWED_ASYNC = {
    "webhook_gitlab",
    "webhook_octopus",
    "webhook_ansible",
    "webhook_cloud",
    "webhook_generic",
    "manual_sweep",
    # Phase 3 Task 4: async again, but hygienic this time — their sync
    # get_session() work runs via asyncio.to_thread and the interrupt-graph
    # resume is awaited (cross-thread future), never blocking the loop.
    # test_approve_reject_handlers_offload_db_work pins the to_thread usage.
    "approve_action",
    "reject_action",
    # TRK-242 (GitLab #113): same asyncio.to_thread offload pattern as
    # approve_action/reject_action above — the ack lookup/update runs in
    # _ack(), which webhook_incident_ack awaits via asyncio.to_thread.
    "webhook_incident_ack",
    # TRK-135/TRK-329: the in-app ops-alert receiver. Its single AuditLog
    # INSERT runs in _persist(), awaited via asyncio.to_thread — same offload
    # pattern, pinned by test_webhook_ops_alert_offloads_db_work below.
    "webhook_ops_alert",
}


def test_approve_reject_handlers_offload_db_work():
    """The two re-asyncified approval handlers must keep their DB work off the
    event loop (asyncio.to_thread) — the exact regression F-019 was about."""
    from infra_brain import webhooks

    for name in ("approve_action", "reject_action"):
        src = inspect.getsource(getattr(webhooks, name))
        assert "asyncio.to_thread" in src, f"{name} must offload sync DB work via to_thread"
        assert "await resume_remediation_action" in src


def test_webhook_incident_ack_offloads_db_work():
    """TRK-242 (GitLab #113): same to_thread offload pattern as approve/reject."""
    from infra_brain import webhooks

    src = inspect.getsource(webhooks.webhook_incident_ack)
    assert "asyncio.to_thread" in src, "webhook_incident_ack must offload sync DB work via to_thread"


def test_webhook_ops_alert_offloads_db_work():
    """TRK-135/TRK-329: the ops-alert receiver's AuditLog write must stay off
    the event loop, same to_thread offload as the handlers above."""
    from infra_brain import webhooks

    src = inspect.getsource(webhooks.webhook_ops_alert)
    assert "asyncio.to_thread" in src, "webhook_ops_alert must offload sync DB work via to_thread"


def test_webhook_async_handlers_are_dispatch_only():
    from infra_brain import webhooks

    for route in webhooks.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        if inspect.iscoroutinefunction(endpoint):
            assert endpoint.__name__ in ALLOWED_ASYNC, (
                f"async handler {endpoint.__name__!r} is not dispatch-only; sync DB "
                "work inside `async def` blocks the event loop (F-019)"
            )


def test_chat_agent_node_uses_ainvoke():
    from infra_brain.chat import agent as agent_mod

    src = inspect.getsource(agent_mod._build)
    assert "await llm.ainvoke(" in src, "agent_node must await llm.ainvoke (F-019)"
    assert "llm.invoke(" not in src, "sync llm.invoke must not remain in agent_node"


def test_trigger_sweep_is_sync_not_async():
    """M-5: ``fleet.trigger_sweep`` was declared ``async def`` but performs
    zero awaits and one SYNC, blocking Redis call (``dedup.try_acquire`` ->
    ``get_redis().set(...)``, bounded at up to 5s by
    ``dedup.get_redis``'s ``socket_timeout``/``socket_connect_timeout``).

    Because FastAPI runs ``async def`` handlers directly on the event loop
    (never in the threadpool), that blocking call stalls the ENTIRE process
    -- every other in-flight request -- for up to 5s whenever Redis is
    partitioned/unreachable. Declaring it plain ``def`` instead makes
    FastAPI dispatch it through its threadpool (like the four webhook
    handlers in ALLOWED_ASYNC's sibling fix above and hosts.py's/etc. sync
    routes), so the blocking wait only ties up one worker thread, not the
    loop that every other request depends on.
    """
    from infra_brain.api.routers import fleet

    assert not inspect.iscoroutinefunction(fleet.trigger_sweep), (
        "trigger_sweep must be a plain `def` (not `async def`) -- it performs a "
        "blocking sync Redis call (dedup.try_acquire) with zero awaits in its body, "
        "which would otherwise block the FastAPI event loop for up to 5s "
        "(dedup.get_redis's socket_timeout) on EVERY in-flight request in the "
        "process whenever Redis is partitioned"
    )
