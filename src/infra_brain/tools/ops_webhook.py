"""Generic outbound ops-alert webhook (OB-1).

Collection-health alerts (``check_collection_health``), the scheduler
dead-man switch (``check_scheduler_deadman``), and agent-anomaly alerts
(``check_agent_anomalies`` — deny-verdict spikes, runaway recursion loops)
all funnel through here to actually notify someone, instead of only being
logged at ERROR level with nothing watching the log.

Plain function (not a LangChain @tool, mirroring tools/jira.py and
tools/confluence.py) — gated by ``gate_external_write`` (F-004.3 / R2 layer 2)
before any ``httpx.post`` leaves the process, same as every other sanctioned
external write.
"""

import logging

import httpx

from infra_brain.callbacks.write_gate import gate_external_write
from infra_brain.config import get_settings

logger = logging.getLogger(__name__)

# Cap the persisted body so one enormous alert cannot bloat the audit table.
_MAX_PERSISTED_CHARS = 2000

# AuditLog column widths (db/models/core.py). Postgres enforces these; sqlite
# (what the test suite runs on) does NOT, so an over-long value would pass
# every local test and then raise StringDataRightTruncation in production.
# The receiver takes `category`/`agent` straight off an inbound HTTP payload,
# so these are the untrusted-input caps, not cosmetic ones.
_AGENT_COL_CHARS = 64
_TOOL_COL_CHARS = 128
_INPUT_HASH_COL_CHARS = 64


def record_alert_audit_row(
    *,
    tool: str,
    category: str,
    messages: list[str],
    agent_name: str,
    dedup_key: str | None,
    reason_prefix: str,
) -> None:
    """Write ONE ops-alert row to ``AuditLog``. Raises on failure.

    The single shared audit-write for ops alerts, used by both halves of the
    alerting path so there is one convention rather than two:

      * ``_persist_undelivered_alert`` below — an OUTBOUND alert that had no
        webhook URL to go to (``tool="send_ops_alert:<category>"``);
      * ``webhooks.py::webhook_ops_alert`` — an INBOUND alert that arrived at
        the in-app receiver (``tool="ops_alert_received:<category>"``).

    Both write ``allowed=False`` deliberately: that is what makes the row show
    up in ``get_audit_log(allowed=False)`` and therefore on the dashboard
    Activity page. It does not mean "denied" here — it means "needs a human's
    eyes", the same way ``NotificationAgent._record_truncation`` uses it.

    This function deliberately does NOT swallow exceptions. The two callers
    want opposite failure behavior (the outbound fallback must never abort the
    monitor that raised the alert; the inbound receiver must fail the HTTP
    request so the sender learns the alert was not stored), so the choice is
    left to them.
    """
    from infra_brain.db.models import AuditLog  # noqa: PLC0415
    from infra_brain.db.session import get_session  # noqa: PLC0415

    body = " | ".join(messages)
    if len(body) > _MAX_PERSISTED_CHARS:
        body = f"{body[:_MAX_PERSISTED_CHARS]}… ({len(messages)} message(s) truncated)"
    with get_session() as session:
        session.add(
            AuditLog(
                agent=(agent_name or "unknown")[:_AGENT_COL_CHARS],
                tool=tool[:_TOOL_COL_CHARS],
                input_hash=(dedup_key or "")[:_INPUT_HASH_COL_CHARS],
                allowed=False,
                denial_reason=f"{reason_prefix} {category}: {body}",
            )
        )
        session.commit()


def _persist_undelivered_alert(
    category: str,
    messages: list[str],
    agent_name: str,
    dedup_key: str | None,
) -> None:
    """Record an alert that had no external delivery channel (TRK-329).

    Best-effort by construction: alerting is the LAST line of defence, so a
    failure to persist must never propagate into — and abort — the monitor that
    raised the alert. It is logged at ERROR and swallowed. That is the one
    place in this codebase where swallowing is the correct call, because the
    alternative is an alerting failure taking down the health check itself.
    """
    try:
        record_alert_audit_row(
            tool=f"send_ops_alert:{category}",
            category=category,
            messages=messages,
            agent_name=agent_name,
            dedup_key=dedup_key,
            reason_prefix="OPS_WEBHOOK_URL not configured — alert not delivered externally.",
        )
    except Exception as exc:  # pragma: no cover - defensive, see docstring
        logger.error(
            "[ops-webhook] failed to persist undelivered %s alert to AuditLog: %s",
            category,
            exc,
        )


def send_ops_alert(
    category: str,
    messages: list[str],
    agent_name: str = "NotificationAgent",
    dedup_key: str | None = None,
) -> bool:
    """POST a JSON alert payload ``{"category": ..., "messages": [...]}`` to
    the configured ops webhook.

    ``dedup_key`` (TRK-242 / GitLab #113) is optional and, when provided, is
    included in the payload as-is. PagerDuty's Events API v2 and Opsgenie's
    alerts API both use a field with this exact name to correlate repeat
    alerts into a single incident — this webhook is already a generic
    authenticated POST that can be pointed at either with zero further code
    changes, so no new outbound mechanism is added here. The caller is
    responsible for persisting ``dedup_key`` onto the originating
    ``DriftEvent``/``ComplianceViolation`` row (its ``incident_key`` column)
    so the ack-in webhook (``POST /webhooks/incident/ack``) can look it back
    up later.

    Returns True when delivered OR when the webhook is unconfigured (a clean
    no-op — matches the Jira/Confluence "unconfigured" pattern in
    ``NotificationAgent.notify_all``). Returns False only when a delivery
    attempt was made and failed; the caller should treat that as best-effort
    (the alert is already logged at ERROR level by the caller regardless).
    """
    if not messages:
        return True

    settings = get_settings()
    url = (settings.ops_webhook_url or "").strip()
    if not url:
        # TRK-329. This branch is NOT a harmless no-op: with OPS_WEBHOOK_URL
        # unset and zero rows in webhook_subscriptions, this process has no
        # alert delivery channel at all, so every escalation it raises used to
        # exist ONLY as a log line inside a container that nothing consumes.
        # That is exactly how the `linux` collector stayed broken for 8 days
        # across 34 consecutive failed runs while the hourly freshness monitor
        # dutifully escalated every single one of them (TRK-328).
        #
        # An alert with nowhere to go is not an alert. Persist it to AuditLog,
        # which IS queryable from the dashboard (the Activity page reads it, and
        # get_audit_log(allowed=False) surfaces it), so an unconfigured webhook
        # degrades to "visible in the UI" instead of "invisible". Same mechanism
        # NotificationAgent._record_truncation already uses for the same reason.
        #
        # The return value stays True: callers treat False as "delivery was
        # attempted and failed", and this is not that.
        logger.warning(
            "[ops-webhook] OPS_WEBHOOK_URL not configured — %s alert has no external "
            "delivery channel; persisting %d message(s) to AuditLog so they remain "
            "visible in the dashboard",
            category,
            len(messages),
        )
        _persist_undelivered_alert(category, messages, agent_name, dedup_key)
        return True

    # F-004.3: pre-write DLP + audit gate — refuses BEFORE any httpx.post.
    gate_external_write(
        system="ops_webhook",
        operation=f"send_ops_alert:{category}",
        payload=f"{category}: " + " | ".join(messages),
        agent_name=agent_name,
    )

    headers = {"Content-Type": "application/json"}
    token = (settings.ops_webhook_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload: dict = {"category": category, "messages": messages}
    if dedup_key:
        payload["dedup_key"] = dedup_key

    try:
        resp = httpx.post(
            url,
            json=payload,
            headers=headers,
            timeout=settings.api_timeout_seconds,
        )
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("[ops-webhook] delivery failed for category=%s", category)
        return False
