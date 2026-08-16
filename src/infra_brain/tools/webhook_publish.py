"""Outbound webhook subscription/event-publish system (GitLab #112).

Before this module, ``config.py``'s ``ops_webhook_url`` was a single
configured destination for ALL ``send_ops_alert`` (tools/ops_webhook.py)
calls — no per-event-type registry, no multiple subscribers. This is the
delivery-with-retry half of the fix: multiple third parties register their
own endpoint + event-type filter (``WebhookSubscription``, matching how
Datadog/PagerDuty do outbound webhooks) instead of one hardcoded
destination for everything.

Plain functions (not LangChain @tools, mirroring tools/ops_webhook.py) —
every outbound POST is gated by ``gate_external_write`` (F-004.3 / R2 layer
2) *per destination* before it leaves the process, same as every other
sanctioned external write.

Public entry points:
  * ``publish_event(category, messages, domain=None, dedup_key=None,
    agent_name=...)`` — call this instead of (or alongside)
    ``send_ops_alert`` to fan an event out to every matching active
    subscription. Best-effort per subscription: one failing destination
    never blocks delivery to the others. Failures are persisted as
    ``WebhookDelivery`` rows with an exponential-backoff ``next_attempt_at``
    for the retry worker to pick up later.
  * ``retry_pending_deliveries(now=None, batch_size=50)`` — processes due
    ``WebhookDelivery`` rows (status="retrying", next_attempt_at <= now),
    re-attempting delivery up to ``max_attempts`` before marking a row
    "exhausted". Intended to be called from a scheduled job (see
    scheduler.py), but is a plain function so tests can call it directly.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from infra_brain.callbacks.write_gate import gate_external_write
from infra_brain.db.models import (
    DELIVERY_DELIVERED,
    DELIVERY_EXHAUSTED,
    DELIVERY_RETRYING,
    RETRY_BACKOFF_MINUTES,
    WebhookDelivery,
    WebhookSubscription,
)
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)


def _matches(subscription: WebhookSubscription, category: str, domain: str | None) -> bool:
    """Glob-style match: '*' matches everything, 'prefix.*' matches any
    category starting with 'prefix.', anything else must match exactly.
    domain_filter (if set) is an additional AND — must equal *domain*
    (case-insensitive) or the subscription does not match."""
    pattern = (subscription.event_pattern or "*").strip()
    if pattern == "*":
        matched = True
    elif pattern.endswith(".*"):
        matched = category.startswith(pattern[:-1])
    else:
        matched = category == pattern

    if not matched:
        return False

    domain_filter = (subscription.domain_filter or "").strip()
    if not domain_filter:
        return True
    return bool(domain) and domain_filter.lower() == domain.lower()



def _is_ssrf_safe_target(url: str) -> bool:
    """Reject targets that resolve to a private/loopback/link-local/reserved
    IP -- the subscription's target_url is admin-supplied via the dashboard
    (create_webhook_subscription, require_admin-gated) but gate_external_write
    only DLP-scans payload text, never validates the destination itself.
    Without this, a malicious or compromised admin session (or an innocent
    typo pointing at an internal service) turns this feature into an SSRF
    proxy able to reach cloud metadata endpoints (169.254.169.254) or
    internal-only services from the server's own network position.

    Best-effort, not adversarial-proof: resolves the hostname once and
    checks that address, which does not close a DNS-rebinding TOCTOU window
    (the record could change between this check and httpx's own connect).
    That is an accepted, documented gap for an admin-gated feature -- full
    protection would need to pin the resolved IP through to the actual
    connection (e.g. a custom transport), which is out of scope here.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def _next_attempt_at(attempt_count: int, now: datetime) -> datetime:
    idx = min(max(attempt_count - 1, 0), len(RETRY_BACKOFF_MINUTES) - 1)
    return now + timedelta(minutes=RETRY_BACKOFF_MINUTES[idx])


_PAYLOAD_SUMMARY_LIMIT = 2000


def _summarize_payload(messages: list[str], limit: int = _PAYLOAD_SUMMARY_LIMIT) -> str:
    """Serialize *messages* as JSON so ``_parse_payload_summary`` can
    reconstruct the exact original list on retry — a naive
    ``" | ".join(messages)`` is ambiguous whenever a message itself
    contains " | " and unsafe to truncate (mid-message truncation would
    silently corrupt the retried payload). Trims whole trailing messages
    (never mid-message) to stay under *limit* chars, so the result is
    always valid JSON that round-trips to a prefix of the original list.
    """
    encoded = json.dumps(messages)
    if len(encoded) <= limit:
        return encoded

    truncated = list(messages)
    while truncated and len(json.dumps(truncated)) > limit:
        truncated.pop()
    if truncated:
        return json.dumps(truncated)

    # Even the first message alone exceeds the limit — truncate its text
    # (not the JSON structure) so we still return valid, parseable JSON.
    return json.dumps([messages[0][: max(limit - 20, 0)]])


def _parse_payload_summary(payload_summary: str | None) -> list[str]:
    """Inverse of ``_summarize_payload``. Falls back to the legacy
    ``" | ".join`` splitting for rows written before this module encoded
    payload_summary as JSON."""
    if not payload_summary:
        return []
    try:
        parsed = json.loads(payload_summary)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return [str(m) for m in parsed]
    # Legacy format (pre-JSON encoding): best-effort, may still be
    # ambiguous/truncated for old rows, but that is unavoidable for data
    # already written before this fix.
    return payload_summary.split(" | ")


def _deliver(
    subscription: WebhookSubscription,
    category: str,
    messages: list[str],
    dedup_key: str | None,
    agent_name: str,
    timeout_seconds: float,
) -> tuple[bool, str | None]:
    """One HTTP attempt to one subscription's target_url. Returns
    (delivered, error_str_or_None). Raises only PermissionError (write-gate
    deny) — callers must let that propagate, it is not a delivery failure."""
    if not _is_ssrf_safe_target(subscription.target_url):
        logger.warning(
            "[webhook-publish] refusing delivery to subscription=%s: target_url "
            "resolves to a private/loopback/reserved address",
            subscription.id,
        )
        return False, "target_url resolves to a private/loopback/reserved address"

    headers = {"Content-Type": "application/json"}
    if subscription.secret_token:
        headers["Authorization"] = f"Bearer {subscription.secret_token}"

    payload: dict = {"category": category, "messages": messages}
    if dedup_key:
        payload["dedup_key"] = dedup_key

    # F-004.3: pre-write DLP + audit gate — refuses BEFORE any httpx.post,
    # applied per destination (this is the "reusing gate_external_write per
    # destination" requirement from GitLab #112).
    gate_external_write(
        system="webhook_subscription",
        operation=f"publish_event:{category}:{subscription.id}",
        payload=f"{category}: " + " | ".join(messages),
        agent_name=agent_name,
    )

    try:
        resp = httpx.post(
            subscription.target_url,
            json=payload,
            headers=headers,
            timeout=timeout_seconds,
        )
        resp.raise_for_status()
        return True, None
    except Exception as exc:  # noqa: BLE001 — best-effort per destination
        logger.warning(
            "[webhook-publish] delivery failed subscription=%s category=%s: %s",
            subscription.id,
            category,
            exc,
        )
        return False, str(exc)


def publish_event(
    category: str,
    messages: list[str],
    domain: str | None = None,
    dedup_key: str | None = None,
    agent_name: str = "NotificationAgent",
) -> dict:
    """Fan *messages* out to every active WebhookSubscription whose
    event_pattern/domain_filter matches (category, domain).

    Returns a summary dict: {"matched": N, "delivered": N, "failed": N}.
    A clean no-op (all zeros) when there are no messages or no matching
    active subscriptions — mirrors the "unconfigured is a no-op" pattern in
    tools/ops_webhook.py.
    """
    summary = {"matched": 0, "delivered": 0, "failed": 0}
    if not messages:
        return summary

    from infra_brain.config import get_settings

    timeout_seconds = get_settings().api_timeout_seconds

    with get_session() as session:
        subs = session.query(WebhookSubscription).filter_by(active=True).all()
        matching = [s for s in subs if _matches(s, category, domain)]
        summary["matched"] = len(matching)

        for sub in matching:
            now = datetime.now(UTC)
            try:
                delivered, error = _deliver(
                    sub, category, messages, dedup_key, agent_name, timeout_seconds
                )
            except PermissionError as exc:
                # Write-gate denied this destination outright (e.g. DLP hit
                # on the payload) -- do not let that abort the fan-out to
                # every other subscription (module docstring: "one failing
                # destination never blocks delivery to the others"), and do
                # not schedule a retry since the same payload will trip the
                # same gate again (matches retry_pending_deliveries()'s
                # handling of this exact error).
                summary["failed"] += 1
                session.add(
                    WebhookDelivery(
                        subscription_id=sub.id,
                        category=category,
                        domain=domain,
                        dedup_key=dedup_key,
                        payload_summary=" | ".join(messages)[:2000],
                        status=DELIVERY_EXHAUSTED,
                        attempt_count=1,
                        last_error=str(exc)[:2000],
                    )
                )
                session.commit()
                continue

            if delivered:
                summary["delivered"] += 1
                session.add(
                    WebhookDelivery(
                        subscription_id=sub.id,
                        category=category,
                        domain=domain,
                        dedup_key=dedup_key,
                        payload_summary=_summarize_payload(messages),
                        status=DELIVERY_DELIVERED,
                        attempt_count=1,
                        delivered_at=now,
                    )
                )
            else:
                summary["failed"] += 1
                session.add(
                    WebhookDelivery(
                        subscription_id=sub.id,
                        category=category,
                        domain=domain,
                        dedup_key=dedup_key,
                        payload_summary=_summarize_payload(messages),
                        status=DELIVERY_RETRYING,
                        attempt_count=1,
                        last_error=(error or "")[:2000],
                        next_attempt_at=_next_attempt_at(1, now),
                    )
                )
            session.commit()

    return summary


def retry_pending_deliveries(now: datetime | None = None, batch_size: int = 50) -> dict:
    """Re-attempt delivery for due WebhookDelivery rows (status='retrying',
    next_attempt_at <= now). Marks a row 'delivered' on success, reschedules
    with backoff on another failure, or 'exhausted' once attempt_count
    reaches the subscription's max_attempts.

    Returns {"attempted": N, "delivered": N, "exhausted": N, "rescheduled": N}.
    Intended to be called periodically (see scheduler.py's
    ``_webhook_retry_job``), but is a plain function precisely so it can
    also be invoked directly (tests, manual ops).
    """
    result = {"attempted": 0, "delivered": 0, "exhausted": 0, "rescheduled": 0}
    now = now or datetime.now(UTC)

    from infra_brain.config import get_settings

    timeout_seconds = get_settings().api_timeout_seconds

    with get_session() as session:
        due = (
            session.query(WebhookDelivery)
            .filter(WebhookDelivery.status == DELIVERY_RETRYING)
            .filter(WebhookDelivery.next_attempt_at <= now)
            .order_by(WebhookDelivery.next_attempt_at.asc())
            .limit(batch_size)
            .all()
        )

        for row in due:
            sub = session.get(WebhookSubscription, row.subscription_id)
            result["attempted"] += 1

            if sub is None or not sub.active:
                # Subscription deleted/deactivated since this delivery was
                # queued — stop retrying, nothing to deliver to.
                row.status = DELIVERY_EXHAUSTED
                row.last_error = "subscription no longer active"
                result["exhausted"] += 1
                session.commit()
                continue

            messages = _parse_payload_summary(row.payload_summary)
            try:
                delivered, error = _deliver(
                    sub, row.category, messages, row.dedup_key, "webhook-retry-worker",
                    timeout_seconds,
                )
            except PermissionError as exc:
                # Write-gate denied this destination outright (e.g. DLP hit
                # on stored payload) — do not keep retrying a blocked write.
                delivered, error = False, str(exc)
                row.status = DELIVERY_EXHAUSTED
                row.last_error = error[:2000]
                result["exhausted"] += 1
                session.commit()
                continue

            row.attempt_count += 1
            if delivered:
                row.status = DELIVERY_DELIVERED
                row.delivered_at = now
                row.last_error = None
                result["delivered"] += 1
            elif row.attempt_count >= row.max_attempts:
                row.status = DELIVERY_EXHAUSTED
                row.last_error = (error or "")[:2000]
                result["exhausted"] += 1
            else:
                row.status = DELIVERY_RETRYING
                row.last_error = (error or "")[:2000]
                row.next_attempt_at = _next_attempt_at(row.attempt_count, now)
                result["rescheduled"] += 1
            session.commit()

    return result
